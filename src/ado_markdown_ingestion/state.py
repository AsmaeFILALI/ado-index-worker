"""Private Blob manifests and an index-wide, lease-fenced control pointer."""

import logging
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobLeaseClient, ContainerClient

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.util import canonical_json, digest, object_dict, parse_json


def _safe(value: object) -> None:
    if isinstance(value, dict):
        if value.keys() & {"content", "contentVector", "system_access_token", "access_token"}:
            raise IngestionError("Raw content, vectors and tokens must not enter control state")
        for item in value.values():
            _safe(item)
    elif isinstance(value, list):
        for item in value:
            _safe(item)


class BlobState:
    def __init__(
        self,
        container: ContainerClient,
        physical_id: str,
        compatibility: str,
        *,
        retry: Retry | None = None,
        clock: Callable[[], float] = time.monotonic,
        start_renew_thread: bool = True,
    ) -> None:
        self.container, self.physical_id, self.compatibility = container, physical_id, compatibility
        self.retry, self.clock = retry or Retry(), clock
        self.prefix = f"indexes/{physical_id}/"
        self.blob = container.get_blob_client(self.prefix + "publisher.json")
        self.control: dict[str, object] = {}
        self._etag = ""
        self._lease: BlobLeaseClient | None = None
        self._lost: IngestionError | None = None
        self._last_renew = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_renew_thread = start_renew_thread

    def guard(self) -> None:
        if self._lost is not None:
            raise IngestionError("Publisher lease ownership was lost") from self._lost
        if self._lease is None or self._stop.is_set() or self.clock() - self._last_renew >= 55:
            raise IngestionError(
                "Publisher lease is unavailable or renewal safety interval expired"
            )

    def renew_once(self) -> None:
        lease = self._lease
        if lease is None:
            raise IngestionError("Publisher lease is not acquired")
        try:

            def renewing() -> None:
                if self._stop.is_set():
                    raise IngestionError("Lease renewal stopped")

            self.retry.call(
                lambda: lease.renew(connection_timeout=5, read_timeout=20),
                guard=renewing,
                label="lease-renew",
            )
        except IngestionError as exc:
            self._lost = exc
            raise
        self._last_renew = self.clock()

    def _renew(self) -> None:
        try:
            while not self._stop.wait(20):
                try:
                    self.renew_once()
                except IngestionError:
                    return
        finally:
            if not self._stop.is_set() and self._lost is None:
                self._lost = IngestionError("Publisher renewal worker stopped unexpectedly")

    @contextmanager
    def owned(self) -> Iterator[None]:
        if self._lease is not None:
            raise IngestionError("Publisher lease context is already active")
        properties = self.retry.call(
            self.container.get_container_properties, label="state-container"
        )
        if properties.public_access is not None:
            raise IngestionError("Control state container must be private")
        initial = {
            "format_version": 1,
            "physical_id": self.physical_id,
            "compatibility": self.compatibility,
            "scopes": {},
        }

        def create() -> None:
            try:
                self.blob.upload_blob(canonical_json(initial), overwrite=False)
            except ResourceExistsError:
                return

        self.retry.call(create, label="state-bootstrap")
        self._lease = self.retry.call(
            lambda: self.blob.acquire_lease(lease_duration=60), label="publisher-acquire"
        )
        self._last_renew = self.clock()
        self._lost = None
        self._stop.clear()
        try:
            raw = self.retry.call(
                lambda: self.blob.download_blob(lease=self._lease).readall(),
                guard=self.guard,
                label="control-read",
            )
            if not isinstance(raw, bytes):
                raise IngestionError("Invalid control state encoding")
            self.control = parse_json(raw)
            if (
                self.control.get("format_version") != 1
                or self.control.get("physical_id") != self.physical_id
                or self.control.get("compatibility") != self.compatibility
            ):
                raise IngestionError(
                    "Index configuration incompatible; select a new pre-provisioned generation"
                )
            object_dict(self.control.get("scopes"), "control scopes")
            props = self.retry.call(
                lambda: self.blob.get_blob_properties(lease=self._lease),
                guard=self.guard,
                label="control-properties",
            )
            self._etag = props.etag
            if not isinstance(self._etag, str) or not self._etag:
                raise IngestionError("Control state returned no valid ETag")
            if self._start_renew_thread:
                self._thread = threading.Thread(
                    target=self._renew, daemon=True, name="publisher-lease"
                )
                self._thread.start()
            yield
        finally:
            active_failure = sys.exc_info()[0] is not None
            self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=35)
                if self._thread.is_alive():
                    self._lost = IngestionError("Lease renewal worker did not stop")
                self._thread = None
            lease, self._lease = self._lease, None
            if lease is not None:
                try:
                    self.retry.call(lease.release, label="lease-release")
                except IngestionError:
                    if not active_failure:
                        raise
                    logging.getLogger("ado_ingest").error('{"event":"lease_cleanup_failed"}')
            if self._lost is not None and not active_failure:
                raise IngestionError("Publisher lease did not remain healthy") from self._lost

    def load(self, scope: str, kind: str) -> dict[str, object] | None:
        self.guard()
        scopes = object_dict(self.control["scopes"], "control scopes")
        entry = object_dict(scopes.get(scope, {}), "scope pointer")
        pointer = entry.get(kind)
        if pointer is None:
            return None
        pointer = object_dict(pointer, "manifest pointer")
        name, checksum = pointer.get("blob"), pointer.get("sha256")
        if (
            not isinstance(name, str)
            or not name.startswith(self.prefix + f"scopes/{scope}/")
            or ".." in name
            or not isinstance(checksum, str)
        ):
            raise IngestionError("Control pointer crosses a source scope or is malformed")
        client = self.container.get_blob_client(name)
        raw = self.retry.call(
            lambda: client.download_blob().readall(), guard=self.guard, label="manifest-read"
        )
        if not isinstance(raw, bytes) or digest(raw) != checksum:
            raise IngestionError("Control manifest integrity mismatch")
        return parse_json(raw)

    def _immutable(self, scope: str, kind: str, payload: dict[str, object]) -> dict[str, object]:
        self.guard()
        _safe(payload)
        raw = canonical_json(payload).encode("utf-8")
        if len(raw) > 16000000:
            raise IngestionError("Control manifest exceeds the supported size")
        name = self.prefix + f"scopes/{scope}/{kind}/{uuid.uuid4()}.json"
        client = self.container.get_blob_client(name)
        self.retry.call(
            lambda: client.upload_blob(raw, overwrite=False),
            guard=self.guard,
            label="manifest-write",
        )
        return {"blob": name, "sha256": digest(raw)}

    def _pointer(
        self, scope: str, kind: str, pointer: dict[str, object], *, clear_pending: bool
    ) -> None:
        scopes = object_dict(self.control["scopes"], "control scopes")
        entry = object_dict(scopes.get(scope, {}), "scope pointer")
        entry[kind] = pointer
        if clear_pending:
            entry.pop("pending", None)
        scopes[scope] = entry
        updated = {**self.control, "scopes": scopes}
        self._save(updated)

    def _save(self, updated: dict[str, object]) -> None:
        result = self.retry.call(
            lambda: self.blob.upload_blob(
                canonical_json(updated),
                overwrite=True,
                lease=self._lease,
                etag=self._etag,
                match_condition=MatchConditions.IfNotModified,
            ),
            guard=self.guard,
            label="control-commit",
        )
        etag = result.get("etag")
        if not isinstance(etag, str):
            raise IngestionError("Conditional control update returned no ETag")
        self._etag, self.control = etag, updated

    def observe_revision(self, revision: str | None) -> None:
        self.guard()
        observed = self.control.get("embedding_observation")
        if observed is not None:
            if object_dict(observed, "embedding observation").get("model_revision") != revision:
                raise IngestionError(
                    "Index-wide embedding revision changed; select a new generation"
                )
            return
        self._save({**self.control, "embedding_observation": {"model_revision": revision}})

    def journal(self, scope: str, payload: dict[str, object]) -> None:
        self._pointer(
            scope, "pending", self._immutable(scope, "runs", payload), clear_pending=False
        )

    def commit(self, scope: str, payload: dict[str, object]) -> None:
        self._pointer(
            scope, "checkpoint", self._immutable(scope, "checkpoints", payload), clear_pending=True
        )

    def close(self) -> None:
        self.container.close()
