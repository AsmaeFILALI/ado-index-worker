"""Offline planning and scoped publication; Azure imports stay off the plan path."""

import json
import logging
import re
import time
import uuid
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from ado_markdown_ingestion import __version__
from ado_markdown_ingestion.config import COMMIT, Settings
from ado_markdown_ingestion.contract import Contract, validate_schema
from ado_markdown_ingestion.documents import build_documents
from ado_markdown_ingestion.embedding_text import embedding_text
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import parse_chunks
from ado_markdown_ingestion.source import read_snapshot, validate_history, validate_path
from ado_markdown_ingestion.tokenizer import ASSET_SHA256, Encoder
from ado_markdown_ingestion.util import digest, fingerprint, object_dict, pinned_json

if TYPE_CHECKING:
    from ado_markdown_ingestion.embeddings import Embeddings
    from ado_markdown_ingestion.publishing import Prepared
    from ado_markdown_ingestion.search import Search
    from ado_markdown_ingestion.state import BlobState


class PublicationResult(TypedDict):
    operation: str
    run_id: str
    source_version_id: str
    bootstrap: bool
    checkpoint: str
    counts: dict[str, int]
    duration_ms: int
    quarantined: list[dict[str, object]]


def _prior_files(
    previous: dict[str, object], settings: Settings, processing: str
) -> tuple[dict[str, dict[str, object]], set[str]]:
    payload = object_dict(previous.get("inventory", previous), "prior inventory")
    integrity = payload.get("integrity_sha256")
    if integrity != fingerprint(
        {key: value for key, value in payload.items() if key != "integrity_sha256"}
    ):
        raise IngestionError("Prior inventory integrity mismatch")
    if (
        type(payload.get("format_version")) is not int
        or payload.get("format_version") != 1
        or payload.get("evidence") != "offline-projection-not-published"
    ):
        raise IngestionError("Unsupported prior inventory format/evidence")
    if (
        payload.get("repo_id") != settings.repo_id
        or payload.get("source_ref") != settings.source_ref
    ):
        raise IngestionError("Prior inventory belongs to a different source scope")
    if (
        payload.get("processing_fingerprint") != processing
        or payload.get("selection_fingerprint") != settings.selection_fingerprint()
    ):
        raise IngestionError(
            "Prior configuration fingerprint mismatch; explicit rebuild strategy required"
        )
    commit = payload.get("source_version_id")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise IngestionError("Prior inventory has an invalid source snapshot")
    validate_history(settings.source_root, commit, settings.commit)
    entries = payload.get("files")
    if not isinstance(entries, list):
        raise IngestionError("Prior file inventory must be complete and valid")
    files: dict[str, dict[str, object]] = {}
    ids: set[str] = set()
    for entry in entries:
        file = object_dict(entry, "prior file")
        path = file.get("path")
        documents = file.get("documents")
        if not isinstance(path, str) or path in files or not isinstance(documents, list):
            raise IngestionError("Invalid or duplicate prior source file")
        validate_path(path)
        if not isinstance(file.get("checksum_sha256"), str) or not re.fullmatch(
            r"[a-f0-9]{64}", str(file["checksum_sha256"])
        ):
            raise IngestionError("Invalid prior source checksum")
        for item in documents:
            doc = object_dict(item, "prior document")
            key = doc.get("id")
            if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) or key in ids:
                raise IngestionError("Invalid or duplicate prior document identity")
            for name in ("content_checksum_sha256", "embedding_input_sha256"):
                if not isinstance(doc.get(name), str) or not re.fullmatch(
                    r"[a-f0-9]{64}", str(doc[name])
                ):
                    raise IngestionError("Invalid prior document checksum")
            revision = doc.get("source_version_id")
            if not isinstance(revision, str) or not COMMIT.fullmatch(revision):
                raise IngestionError("Invalid retained prior source revision")
            ids.add(key)
        files[path] = file
    return files, ids


def create_plan(
    settings: Settings,
    encoder: Encoder,
    *,
    previous_path: Path | None = None,
    previous_sha256: str | None = None,
    include_documents: bool = False,
    schema_path: Path | None = None,
    schema_sha256: str | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    if previous_path is None and previous_sha256 is not None:
        raise IngestionError("Previous SHA-256 supplied without a prior inventory")
    if schema_path is None and schema_sha256 is not None:
        raise IngestionError("Schema SHA-256 supplied without a schema artifact")
    contract = Contract.load(settings.contract)
    processing = fingerprint(
        {
            "processing": settings.processing_fingerprint(),
            "consumer": contract.sha256,
            "tokenizer_asset": ASSET_SHA256,
        }
    )
    if schema_path is not None:
        validate_schema(
            pinned_json(schema_path, schema_sha256),
            contract,
            deployment=settings.deployment,
            model=settings.model,
        )
    snapshot = read_snapshot(settings)
    previous_files: dict[str, dict[str, object]] = {}
    previous_ids: set[str] = set()
    if previous_path is not None:
        previous_files, previous_ids = _prior_files(
            pinned_json(previous_path, previous_sha256), settings, processing
        )
    inventory_files: list[dict[str, object]] = []
    documents: list[dict[str, object]] = []
    current_ids: set[str] = set()
    upserts: list[str] = []
    added: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    quarantined = [(item.path, item.reason) for item in snapshot.quarantined]
    for file in snapshot.files:
        try:
            drafts = build_documents(
                file, parse_chunks(file.content, file.path, settings, encoder), settings, contract
            )
        except IngestionError as exc:
            quarantined.append((file.path, f"chunking-failed: {exc}"))
            continue
        previous = previous_files.get(file.path)
        same = previous is not None and previous["checksum_sha256"] == file.checksum_sha256
        if same:
            unchanged.append(file.path)
        elif previous is not None:
            changed.append(file.path)
        else:
            added.append(file.path)
        planned_records: list[dict[str, object]] = []
        for draft in drafts:
            fields = draft.to_dict()
            documents.append(fields)
            current_ids.add(draft.id)
            if not same:
                upserts.append(draft.id)
            planned_records.append(
                {
                    "id": draft.id,
                    "content_checksum_sha256": draft.content_checksum_sha256,
                    "embedding_input_sha256": digest(embedding_text(draft.content)),
                    "source_version_id": draft.source_version_id,
                }
            )
        if same and previous is not None:
            previous_documents = previous["documents"]
            if not isinstance(previous_documents, list) or {
                (record["id"], record["content_checksum_sha256"])
                for record in (object_dict(item, "prior document") for item in previous_documents)
            } != {(record["id"], record["content_checksum_sha256"]) for record in planned_records}:
                raise IngestionError(
                    "Unchanged source has an inconsistent prior document inventory"
                )
        inventory_files.append(
            {
                "path": file.path,
                "blob_id": file.blob_id,
                "checksum_sha256": file.checksum_sha256,
                "documents": previous["documents"]
                if same and previous is not None
                else planned_records,
            }
        )
    quarantine_report: list[dict[str, object]] = []
    for path, reason in sorted(quarantined):
        retained_entry = previous_files.get(path)
        if retained_entry is not None:
            # Keep what is already indexed for a file that is currently unprocessable.
            inventory_files.append(retained_entry)
            retained_documents = retained_entry["documents"]
            if isinstance(retained_documents, list):
                current_ids.update(
                    str(object_dict(item, "prior document")["id"]) for item in retained_documents
                )
        quarantine_report.append(
            {"path": path, "reason": reason, "retained_previous": retained_entry is not None}
        )
        logging.getLogger("ado_ingest").warning(
            json.dumps({"event": "file_quarantined", "path": path, "reason": reason})
        )
    inventory_files.sort(key=lambda entry: str(entry["path"]))
    quarantined_paths = {path for path, _ in quarantined}
    inventory: dict[str, object] = {
        "format_version": 1,
        "evidence": "offline-projection-not-published",
        "repo_id": settings.repo_id,
        "source_ref": settings.source_ref,
        "source_version_id": settings.commit,
        "processing_fingerprint": processing,
        "selection_fingerprint": settings.selection_fingerprint(),
        "files": inventory_files,
    }
    inventory["integrity_sha256"] = fingerprint(inventory)
    result: dict[str, object] = {
        "format_version": 1,
        "operation": "plan",
        "evidence": "offline-only",
        "publishable": False,
        "run_id": run_id or str(uuid.uuid4()),
        "configuration": {
            "pipeline_version": __version__,
            "processing_fingerprint": processing,
            "consumer_contract_version": contract.data.get("contractVersion"),
            "consumer_contract_sha256": contract.sha256,
            "model": settings.model,
            "deployment": settings.deployment,
            "dimensions": settings.dimensions,
            "effective_model_revision": None,
            "embedding_evidence": "configured-only-not-service-validated",
            "tokenizer_encoding": "cl100k_base",
            "tokenizer_asset_sha256": ASSET_SHA256,
            "target_tokens": settings.target_tokens,
            "max_input_tokens": settings.max_input_tokens,
            "max_content_chars": settings.max_content_chars,
            "overlap_tokens": settings.overlap_tokens,
        },
        "source": {
            "repo_id": settings.repo_id,
            "source_ref": settings.source_ref,
            "source_version_id": settings.commit,
        },
        "counts": {
            "files": len(snapshot.files),
            "quarantined": len(quarantine_report),
            "documents": len(documents),
            "azure_calls": 0,
            "embedding_calls": 0,
            "upsert_candidates": len(upserts),
        },
        "deletion_status": "unknown" if previous_path is None else "trusted-local-projection",
        "quarantined": quarantine_report,
        "schema_validation": "not_supplied" if schema_path is None else "offline-artifact-only",
        "changes": {
            "added_file_candidates": added,
            "changed_files": changed,
            "unchanged_files": unchanged,
            "deleted_files": None
            if previous_path is None
            else sorted(
                previous_files.keys() - {file.path for file in snapshot.files} - quarantined_paths
            ),
            "upsert_document_ids": sorted(upserts),
            "deletions": None if previous_path is None else sorted(previous_ids - current_ids),
        },
        "inventory": inventory,
    }
    if include_documents:
        result["documents"] = documents
        result["document_stage"] = "target-snapshot-drafts-not-indexed"
    return result


def _publication_state(
    payload: dict[str, object] | None,
    prepared: "Prepared",
    kind: str,
) -> dict[str, object] | None:
    if payload is None:
        return None
    if (
        type(payload.get("state_version")) is not int
        or payload.get("state_version") != 1
        or payload.get("evidence")
        != ("verified-publication" if kind == "checkpoint" else "publication-intent")
        or payload.get("repo_id") != prepared.source.repo_id
        or payload.get("source_ref") != prepared.source.source_ref
    ):
        raise IngestionError("Published control state has invalid scope/version/evidence")
    if payload.get("processing_fingerprint") != prepared.processing:
        raise IngestionError(
            "Source processing fingerprint changed; select a compatible new generation"
        )
    if payload.get("selection_fingerprint") != prepared.source.selection_fingerprint() and not (
        prepared.operation == "reconcile" and prepared.policy.get("allow_selection_reset") is True
    ):
        raise IngestionError(
            "Source selection fingerprint changed; an admitted reconciliation is required"
        )
    commit = payload.get("source_version_id")
    if not isinstance(commit, str):
        raise IngestionError("Control state source revision is missing")
    validate_history(
        prepared.source.source_root,
        commit,
        prepared.source.commit,
        allow_divergence=prepared.operation == "reconcile"
        and prepared.policy.get("allow_history_reset") is True,
    )
    files = object_dict(payload.get("files"), "published file inventory")
    documents = object_dict(payload.get("documents"), "published document inventory")
    file_keys: set[str] = set()
    for path, entry in files.items():
        validate_path(path)
        file = object_dict(entry, "published source file")
        if (
            not isinstance(file.get("checksum_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", str(file["checksum_sha256"]))
            or not isinstance(file.get("document_ids"), list)
        ):
            raise IngestionError("Invalid published file checksum/key inventory")
        file_ids = file["document_ids"]
        if not isinstance(file_ids, list):
            raise IngestionError("Invalid published file key list")
        for key in file_ids:
            if not isinstance(key, str) or key in file_keys or key not in documents:
                raise IngestionError("Published file-to-document inventory is incomplete")
            file_keys.add(key)
    if file_keys != set(documents) or type(payload.get("embedding_observed")) is not bool:
        raise IngestionError("Published source inventory is inconsistent")
    for key, value in documents.items():
        meta = object_dict(value, "published document metadata")
        if not re.fullmatch(r"[a-f0-9]{64}", key) or any(
            not isinstance(meta.get(name), str)
            or not re.fullmatch(r"[a-f0-9]{64}", str(meta[name]))
            for name in ("signature", "input_sha256", "vector_sha256")
        ):
            raise IngestionError("Invalid published document metadata")
    return payload


def publish(
    prepared: "Prepared",
    search: "Search",
    state: "BlobState",
    embeddings: "Embeddings",
    encoder: Encoder,
    *,
    run_id: str | None = None,
) -> PublicationResult:
    """Publish a fixed admitted snapshot. Tests provide real SDKs with fake transports."""
    from datetime import datetime

    from ado_markdown_ingestion.contract import (
        compare_declared,
        validate_document,
        validate_vector,
        vector_checksum,
    )
    from ado_markdown_ingestion.search import signature

    source, target = prepared.source, prepared.target
    started = time.perf_counter()
    start_embedding_calls, start_search_requests = embeddings.calls, search.requests
    start_retries = search.retry.retries + embeddings.retry.retries + state.retry.retries
    if (
        search.index_name != target.index_name
        or search.repo_id != source.repo_id
        or search.source_ref != source.source_ref
        or search.service_endpoint != target.search_endpoint
        or state.physical_id != target.physical_id()
        or state.compatibility != prepared.compatibility
        or state.container.url.rstrip("/")
        != target.state_account_url + "/" + target.state_container
        or embeddings.deployment != source.deployment
    ):
        raise IngestionError(
            "Publishing adapter identity differs from the admitted physical target"
        )
    search.guard = state.guard
    embeddings.guard = state.guard
    run_id = run_id or str(uuid.uuid4())
    with state.owned():
        actual_schema = search.schema()
        validate_schema(
            actual_schema,
            prepared.contract,
            deployment=source.deployment,
            model=source.model,
            embedding_resource=target.embedding_endpoint,
        )
        compare_declared(prepared.schema, actual_schema)
        checkpoint = _publication_state(
            state.load(prepared.scope, "checkpoint"), prepared, "checkpoint"
        )
        pending = _publication_state(state.load(prepared.scope, "pending"), prepared, "pending")
        if checkpoint is None and prepared.policy.get("allow_bootstrap") is not True:
            raise IngestionError(
                "Missing successful checkpoint is an explicit bootstrap admission gate"
            )
        live = search.scan()
        if (
            checkpoint is None
            and pending is None
            and live
            and (prepared.policy.get("allow_bootstrap_existing") is not True)
        ):
            raise IngestionError(
                "Bootstrap found existing source documents; explicit admission is required"
            )
        prior_files = object_dict((checkpoint or {}).get("files", {}), "checkpoint files")
        prior_meta = object_dict((checkpoint or {}).get("documents", {}), "checkpoint documents")
        pending_files = object_dict((pending or {}).get("files", {}), "pending files")
        pending_meta = object_dict((pending or {}).get("documents", {}), "pending documents")
        trusted_meta = {**prior_meta, **pending_meta}

        def reusable(key: str, meta: dict[str, object]) -> bool:
            if key not in live:
                return False
            try:
                if signature(live[key]) != meta["signature"]:
                    return False
            except IngestionError:
                logging.getLogger("ado_ingest").warning('{"event":"document_drift_detected"}')
                return False
            if prepared.operation == "reconcile":
                full = search.read(key, vector=True)
                try:
                    if (
                        full is None
                        or vector_checksum(full.get("contentVector")) != meta["vector_sha256"]
                    ):
                        logging.getLogger("ado_ingest").warning('{"event":"vector_drift_detected"}')
                        return False
                except IngestionError:
                    logging.getLogger("ado_ingest").warning('{"event":"vector_drift_detected"}')
                    return False
            return True

        donors: dict[str, str] = {}
        for key, item in trusted_meta.items():
            meta = object_dict(item, "trusted document metadata")
            if reusable(key, meta):
                donors[str(meta["input_sha256"])] = key
        intended: dict[str, dict[str, object]] = {}
        next_files: dict[str, object] = {}
        next_meta: dict[str, object] = {}
        upserts: list[dict[str, object]] = []
        needs_vectors: list[dict[str, object]] = []
        skipped_files = reused_vectors = 0
        quarantine_report: list[dict[str, object]] = []
        stamp = datetime.now(UTC).isoformat(timespec="milliseconds")

        def find_retained(path: str, checksum: str | None) -> dict[str, object] | None:
            """Reuse the live documents of a file; checksum None accepts any prior revision."""
            for files, metadata in ((pending_files, pending_meta), (prior_files, prior_meta)):
                if any(
                    prior is not None
                    and prior.get("selection_fingerprint") != source.selection_fingerprint()
                    for prior in (pending, checkpoint)
                ):
                    break
                entry = files.get(path)
                if entry is None:
                    continue
                entry = object_dict(entry, "source file metadata")
                retained_ids = entry.get("document_ids")
                if (
                    checksum is not None and entry.get("checksum_sha256") != checksum
                ) or not isinstance(retained_ids, list):
                    continue
                if all(
                    isinstance(key, str)
                    and key in metadata
                    and reusable(key, object_dict(metadata[key], "retained metadata"))
                    for key in retained_ids
                ):
                    for key in retained_ids:
                        assert isinstance(key, str)
                        intended[key] = live[key]
                        next_meta[key] = metadata[key]
                    return entry
            return None

        def quarantine(path: str, reason: str) -> None:
            # A file that cannot be processed keeps whatever is already indexed for it.
            entry = find_retained(path, None)
            if entry is not None:
                next_files[path] = entry
            quarantine_report.append(
                {"path": path, "reason": reason, "retained_previous": entry is not None}
            )
            logging.getLogger("ado_ingest").warning(
                json.dumps({"event": "file_quarantined", "path": path, "reason": reason})
            )

        for item in prepared.snapshot.quarantined:
            quarantine(item.path, item.reason)
        for file in prepared.snapshot.files:
            retained = find_retained(file.path, file.checksum_sha256)
            if retained is not None:
                next_files[file.path] = retained
                skipped_files += 1
                continue
            try:
                drafts = build_documents(
                    file,
                    parse_chunks(file.content, file.path, source, encoder),
                    source,
                    prepared.contract,
                )
            except IngestionError as exc:
                quarantine(file.path, f"chunking-failed: {exc}")
                continue
            ids: list[str] = []
            for draft in drafts:
                if draft.id in intended:
                    raise IngestionError("Duplicate intended document identity")
                record = {
                    **draft.to_dict(),
                    "ingested_at": stamp,
                    "ingest_run_id": run_id,
                }
                donor = donors.get(draft.content_checksum_sha256)
                previous = search.read(donor, vector=True) if donor is not None else None
                if previous is not None and donor is not None:
                    meta = object_dict(trusted_meta[donor], "vector provenance")
                    if signature(previous) != meta["signature"]:
                        raise IngestionError("Vector donor provenance changed during publication")
                    try:
                        if vector_checksum(previous.get("contentVector")) != meta["vector_sha256"]:
                            raise IngestionError("Vector donor checksum changed")
                        record["contentVector"] = validate_vector(previous.get("contentVector"))
                        reused_vectors += 1
                    except IngestionError:
                        logging.getLogger("ado_ingest").warning('{"event":"vector_reuse_rejected"}')
                if "contentVector" not in record:
                    needs_vectors.append(record)
                intended[draft.id] = record
                upserts.append(record)
                ids.append(draft.id)
            next_files[file.path] = {
                "checksum_sha256": file.checksum_sha256,
                "blob_id": file.blob_id,
                "document_ids": ids,
            }
        inputs = [embedding_text(str(record["content"])) for record in needs_vectors]
        vectors = embeddings.embed(inputs)
        for record, vector in zip(needs_vectors, vectors, strict=True):
            record["contentVector"] = vector
        observed = next(
            (
                item
                for item in (pending, checkpoint)
                if item and item.get("embedding_observed") is True
            ),
            None,
        )
        model_revision = observed.get("model_revision") if observed is not None else None
        if model_revision is not None and not isinstance(model_revision, str):
            raise IngestionError("Invalid persisted embedding model revision")
        if inputs:
            if observed is not None and model_revision != embeddings.model_revision:
                raise IngestionError("Source embedding revision changed; select a new generation")
            model_revision = embeddings.model_revision
        embedding_observed = bool(observed is not None or inputs)
        if embedding_observed:
            state.observe_revision(model_revision)
        for record in upserts:
            validate_document(record, prepared.contract, max_content_chars=source.max_content_chars)
            key = str(record["id"])
            next_meta[key] = {
                "signature": signature(record),
                "input_sha256": record["content_checksum_sha256"],
                "source_version_id": record["source_version_id"],
                "vector_sha256": vector_checksum(record["contentVector"]),
            }
        expected = {
            key: str(object_dict(next_meta[key], "intended document metadata")["signature"])
            for key in intended
        }
        previous_keys = set(prior_meta) | set(pending_meta) | set(live)
        if pending is not None:
            keys = pending.get("keys")
            if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                raise IngestionError("Pending recovery key inventory is invalid")
            previous_keys.update(str(key) for key in keys)
        obsolete = previous_keys - set(intended)
        payload: dict[str, object] = {
            "state_version": 1,
            "evidence": "publication-intent",
            "repo_id": source.repo_id,
            "source_ref": source.source_ref,
            "source_version_id": source.commit,
            "run_id": run_id,
            "operation": prepared.operation,
            "processing_fingerprint": prepared.processing,
            "schema_sha256": target.schema_sha256,
            "admission_sha256": target.admission_sha256
            if not target.admission_waiver
            else "waived",
            "index_compatibility": prepared.compatibility,
            "previous_successful_commit": (checkpoint or {}).get("source_version_id"),
            "previous_pending_commit": (pending or {}).get("source_version_id"),
            "history_reset_admitted": (
                prepared.operation == "reconcile"
                and prepared.policy.get("allow_history_reset") is True
            ),
            "selection_reset_admitted": (
                prepared.operation == "reconcile"
                and prepared.policy.get("allow_selection_reset") is True
            ),
            "selection_fingerprint": source.selection_fingerprint(),
            "model_revision": model_revision,
            "embedding_observed": embedding_observed,
            "files": next_files,
            "documents": next_meta,
            "keys": sorted(previous_keys | set(intended)),
        }
        state.journal(prepared.scope, payload)
        search.write(upserts)
        search.delete(sorted(obsolete))
        search.verify(expected, obsolete, target.visibility_seconds)
        payload["evidence"] = "verified-publication"
        payload["keys"] = sorted(intended)
        payload["counts"] = {
            "files": len(prepared.snapshot.files),
            "documents": len(intended),
            "upserts": len(upserts),
            "deletions": len(obsolete),
            "skipped_files": skipped_files,
            "quarantined": len(quarantine_report),
            "embedding_calls": embeddings.calls - start_embedding_calls,
            "reused_vectors": reused_vectors,
        }
        payload["verified_at"] = datetime.now(UTC).isoformat()
        state.commit(prepared.scope, payload)
        return {
            "operation": prepared.operation,
            "run_id": run_id,
            "source_version_id": source.commit,
            "bootstrap": checkpoint is None,
            "checkpoint": "advanced-after-verification",
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "quarantined": quarantine_report,
            "counts": {
                "files": len(prepared.snapshot.files),
                "documents": len(intended),
                "upserts": len(upserts),
                "deletions": len(obsolete),
                "skipped_files": skipped_files,
                "quarantined": len(quarantine_report),
                "embedding_calls": embeddings.calls - start_embedding_calls,
                "reused_vectors": reused_vectors,
                "search_requests": search.requests - start_search_requests,
                "retries": search.retry.retries
                + embeddings.retry.retries
                + state.retry.retries
                - start_retries,
            },
        }
