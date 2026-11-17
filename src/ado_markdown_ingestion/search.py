"""Scoped document operations on an existing index; no schema-management operations."""

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit

from azure.core.exceptions import ResourceNotFoundError
from azure.core.pipeline import PipelineRequest, PipelineResponse
from azure.core.pipeline.transport import HttpRequest, HttpResponse
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient

from ado_markdown_ingestion.contract import REQUIRED_NAMES
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.retry import Retry, RetryableResult, no_guard
from ado_markdown_ingestion.util import digest, fingerprint, object_dict, parse_json

SCALARS = sorted(REQUIRED_NAMES - {"contentVector"})
TRANSIENT = {408, 409, 422, 500, 502, 503, 504}


def signature(document: Mapping[str, object]) -> str:
    if set(SCALARS) - document.keys():
        raise IngestionError("Search record is missing required scalar provenance")
    values = {name: document[name] for name in SCALARS}
    content = values["content"]
    if not isinstance(content, str) or digest(content) != values["content_checksum_sha256"]:
        raise IngestionError("Search content/checksum mismatch")
    timestamp = values["ingested_at"]
    try:
        if not isinstance(timestamp, str):
            raise ValueError
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        values["ingested_at"] = parsed.astimezone(UTC).isoformat(timespec="microseconds")
    except ValueError as exc:
        raise IngestionError("Search ingestion timestamp is invalid") from exc
    return fingerprint(values)


class _PayloadTooLarge(IngestionError):
    pass


class InventoryUnstable(IngestionError):
    pass


class Search:
    def __init__(
        self,
        client: SearchClient,
        index_client: SearchIndexClient,
        index_name: str,
        repo_id: str,
        source_ref: str,
        *,
        retry: Retry | None = None,
        guard: Callable[[], None] = no_guard,
        inventory_limit: int = 10000,
        max_bytes: int = 16000000,
        batch_size: int = 100,
        service_endpoint: str | None = None,
    ) -> None:
        if (
            not 1 <= inventory_limit <= 10000
            or not 1 <= batch_size <= 1000
            or not 1 <= max_bytes <= 16000000
        ):
            raise IngestionError("Invalid bounded Search limits")
        self.client, self.index_client = client, index_client
        self.index_name, self.repo_id, self.source_ref = index_name, repo_id, source_ref
        self.retry, self.guard = retry or Retry(), guard
        self.inventory_limit, self.max_bytes, self.batch_size = (
            inventory_limit,
            max_bytes,
            batch_size,
        )
        self.requests = 0
        self.max_payload_bytes = 0
        self.service_endpoint = service_endpoint

    def _endpoint_guard(self, request: PipelineRequest[HttpRequest]) -> None:
        self.guard()
        if self.service_endpoint is not None:
            expected, actual = urlsplit(self.service_endpoint), urlsplit(request.http_request.url)
            if (expected.scheme, expected.netloc) != (actual.scheme, actual.netloc):
                raise IngestionError("SDK request targets a different physical Search service")

    def _owned(self, document: Mapping[str, object]) -> None:
        if (
            document.get("source") != "azure_devops"
            or document.get("repo_id") != self.repo_id
            or document.get("source_ref") != self.source_ref
        ):
            raise IngestionError("Search document belongs to another source scope")

    def schema(self) -> dict[str, object]:
        captured: dict[str, object] | None = None

        def capture(response: PipelineResponse[HttpRequest, HttpResponse]) -> None:
            nonlocal captured
            if response.http_response.status_code == 200:
                captured = parse_json(response.http_response.body())

        self.retry.call(
            lambda: self.index_client.get_index(
                self.index_name, raw_response_hook=capture, raw_request_hook=self._endpoint_guard
            ),
            guard=self.guard,
            label="schema-read",
        )
        if captured is None or captured.get("name") != self.index_name:
            raise IngestionError("Actual target schema could not be validated")
        return captured

    def scan(self) -> dict[str, dict[str, object]]:
        escaped_repo = self.repo_id.replace("'", "''")
        escaped_ref = self.source_ref.replace("'", "''")
        query_filter = (
            f"source eq 'azure_devops' and repo_id eq '{escaped_repo}' "
            f"and source_ref eq '{escaped_ref}'"
        )
        found: dict[str, dict[str, object]] = {}
        expected: int | None = None
        while True:
            skip = len(found)

            def page(offset: int = skip) -> tuple[int, list[dict[str, object]]]:
                result = self.client.search(
                    search_text="*",
                    filter=query_filter,
                    select=SCALARS,
                    include_total_count=True,
                    skip=offset,
                    top=min(1000, self.inventory_limit + 1 - offset),
                    raw_request_hook=self._endpoint_guard,
                )
                rows = [object_dict(row, "Search inventory row") for row in result]
                count = result.get_count()
                if type(count) is not int or count < 0:
                    raise IngestionError("Search inventory count is unavailable")
                return count, rows

            count, rows = self.retry.call(page, guard=self.guard, label="inventory-read")
            if count > self.inventory_limit:
                raise IngestionError("Search inventory exceeds the supported scan bound")
            if expected is not None and count != expected:
                raise InventoryUnstable("Search inventory changed during enumeration")
            expected = count
            for row in rows:
                self._owned(row)
                key = row.get("id")
                if not isinstance(key, str) or not key or key in found:
                    raise InventoryUnstable("Duplicate or invalid Search inventory key")
                found[key] = row
            if len(found) == count:
                return found
            if not rows or len(found) > count or len(found) > self.inventory_limit:
                raise InventoryUnstable("Incomplete Search source inventory")

    def read(self, key: str, *, vector: bool = False) -> dict[str, object] | None:
        def action() -> dict[str, object] | None:
            try:
                row = self.client.get_document(
                    key,
                    selected_fields=sorted(REQUIRED_NAMES) if vector else SCALARS,
                    raw_request_hook=self._endpoint_guard,
                )
            except ResourceNotFoundError:
                return None
            result = object_dict(row, "Search document")
            self._owned(result)
            return result

        return self.retry.call(action, guard=self.guard, label="document-read")

    def _request_guard(self, request: PipelineRequest[HttpRequest]) -> None:
        self._endpoint_guard(request)
        body = request.http_request.body
        if isinstance(body, str):
            raw = body.encode("utf-8")
        elif isinstance(body, bytes):
            raw = body
        else:
            raise IngestionError("Unsupported SDK indexing request serialization")
        if len(raw) > self.max_bytes:
            raise _PayloadTooLarge("Serialized Search action payload exceeds the byte limit")
        actions = parse_json(raw).get("value")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 1000:
            raise IngestionError("SDK indexing request violates the action-count limit")
        self.max_payload_bytes = max(self.max_payload_bytes, len(raw))
        self.requests += 1

    def _batch(self, documents: list[dict[str, object]], delete: bool) -> None:
        remaining = list(documents)

        def action() -> None:
            nonlocal remaining
            results = (
                self.client.delete_documents(remaining, raw_request_hook=self._request_guard)
                if delete
                else self.client.upload_documents(remaining, raw_request_hook=self._request_guard)
            )
            by_key = {result.key: result for result in results}
            requested = {doc["id"] for doc in remaining}
            if (
                len(results) != len(remaining)
                or len(by_key) != len(results)
                or set(by_key) != requested
            ):
                raise IngestionError("Search indexing result count/key mismatch")
            pending: list[dict[str, object]] = []
            for doc in remaining:
                result = by_key[str(doc["id"])]
                if type(result.status_code) is not int or type(result.succeeded) is not bool:
                    raise IngestionError("Search per-document status types are invalid")
                if result.succeeded is True and result.status_code in {200, 201}:
                    continue
                if result.status_code not in TRANSIENT:
                    raise IngestionError(
                        f"Search document failed permanently (status {result.status_code})"
                    )
                pending.append(doc)
            remaining = pending
            if remaining:
                raise RetryableResult(503)

        try:
            self.retry.call(
                action, guard=self.guard, label="search-delete" if delete else "search-upload"
            )
        except _PayloadTooLarge as exc:
            if len(documents) == 1:
                raise IngestionError(
                    "A single serialized Search document exceeds the payload limit"
                ) from exc
            middle = len(documents) // 2
            self._batch(documents[:middle], delete)
            self._batch(documents[middle:], delete)

    def write(self, documents: list[dict[str, object]]) -> None:
        keys: set[str] = set()
        for doc in documents:
            self._owned(doc)
            key = doc.get("id")
            if not isinstance(key, str) or not key or key in keys:
                raise IngestionError("Duplicate or invalid upsert key")
            keys.add(key)
            self.read(key)
        for start in range(0, len(documents), self.batch_size):
            self._batch(documents[start : start + self.batch_size], False)

    def delete(self, keys: list[str]) -> None:
        if len(set(keys)) != len(keys):
            raise IngestionError("Duplicate deletion keys")
        for key in keys:
            self.read(key)
        documents: list[dict[str, object]] = [{"id": key} for key in keys]
        for start in range(0, len(documents), self.batch_size):
            self._batch(documents[start : start + self.batch_size], True)

    def verify(self, expected: dict[str, str], obsolete: set[str], timeout: float = 120) -> None:
        started = self.retry.clock()
        previous_guard = self.guard

        def visibility_guard() -> None:
            previous_guard()
            if self.retry.clock() - started >= timeout:
                raise IngestionError(
                    "Search visibility verification timed out; checkpoint not advanced"
                )

        self.guard = visibility_guard
        try:
            while True:
                actual: dict[str, dict[str, object]] | None = None
                try:
                    actual = self.scan()
                except InventoryUnstable:
                    logging.getLogger("ado_ingest").info('{"event":"visibility_inventory_retry"}')
                self.guard()
                if (
                    actual is not None
                    and set(actual) == set(expected)
                    and not obsolete.intersection(actual)
                ):
                    if all(signature(actual[key]) == value for key, value in expected.items()):
                        return
                self.retry.sleep(min(2, max(0, timeout - (self.retry.clock() - started))))
        finally:
            self.guard = previous_guard

    def close(self) -> None:
        self.client.close()
        self.index_client.close()
