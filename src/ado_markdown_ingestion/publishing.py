"""Explicit publishing admission and settings; no clients or credentials are loaded here."""

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.contract import Contract, validate_schema
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.source import Snapshot, read_snapshot
from ado_markdown_ingestion.tokenizer import ASSET_SHA256
from ado_markdown_ingestion.util import fingerprint, pinned_json, string_value


def endpoint(value: str, suffix: str) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname
        if (
            url.scheme != "https"
            or not host
            or not host.endswith(suffix)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", host.removesuffix(suffix))
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/")
            or url.port not in (None, 443)
            or "<" in value
        ):
            raise ValueError
    except ValueError as exc:
        raise IngestionError(
            "Publishing requires explicit approved HTTPS service endpoints"
        ) from exc
    return "https://" + host


@dataclass(frozen=True)
class PublishSettings:
    search_endpoint: str
    index_name: str
    embedding_endpoint: str
    openai_api_version: str
    state_account_url: str
    state_container: str
    platform_schema: Path
    schema_sha256: str
    admission: Path
    admission_sha256: str
    tenant_id: str
    auth_mode: str
    client_id: str = ""
    service_connection_id: str = ""
    inventory_limit: int = 10000
    visibility_seconds: int = 120
    search_schema_api_version: str = "2025-09-01"
    # Owner-recorded decision that this non-serving POC target needs no separate admission file.
    admission_waiver: str = ""

    @classmethod
    def load(cls, path: Path) -> "PublishSettings":
        try:
            with path.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise IngestionError("Cannot read publishing TOML configuration") from exc
        fields = set(cls.__dataclass_fields__)
        if set(data) - fields:
            raise IngestionError("Unknown publishing setting")
        waiver = data.get("admission_waiver", "")
        if not isinstance(waiver, str):
            raise IngestionError("Admission waiver must be a recorded owner decision string")
        if waiver:
            if "admission" in data or "admission_sha256" in data:
                raise IngestionError("An admission waiver cannot be combined with an admission pin")
            if "<" in waiver or ">" in waiver or len(waiver.strip()) < 20:
                raise IngestionError("Admission waiver needs a recorded, non-placeholder reference")
            data["admission"] = "waived"
            data["admission_sha256"] = "0" * 64
        for name in fields - {
            "client_id",
            "service_connection_id",
            "inventory_limit",
            "visibility_seconds",
            "search_schema_api_version",
            "admission_waiver",
        }:
            string_value(data, name)
        for name in ("schema_sha256", "admission_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", data[name]):
                raise IngestionError("Publishing artifact pins must be SHA-256 references")
        data["search_endpoint"] = endpoint(data["search_endpoint"], ".search.windows.net")
        data["embedding_endpoint"] = endpoint(data["embedding_endpoint"], ".openai.azure.com")
        data["state_account_url"] = endpoint(data["state_account_url"], ".blob.core.windows.net")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,126}[a-z0-9]", data["index_name"]):
            raise IngestionError("Invalid physical index name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", data["state_container"]):
            raise IngestionError("Invalid private state container")
        if data["auth_mode"] not in ("cli", "pipeline"):
            raise IngestionError("Explicit cli or pipeline authentication is required")
        try:
            UUID(data["tenant_id"])
            date.fromisoformat(data["openai_api_version"].removesuffix("-preview"))
        except ValueError as exc:
            raise IngestionError(
                "Explicit tenant UUID and date-based OpenAI API version are required"
            ) from exc
        schema_version = data.get("search_schema_api_version", "2025-09-01")
        if not isinstance(schema_version, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}(?:-preview)?", schema_version
        ):
            raise IngestionError("Search schema API version must be a date or date-preview")
        try:
            date.fromisoformat(schema_version.removesuffix("-preview"))
        except ValueError as exc:
            raise IngestionError("Search schema API version has an invalid date") from exc
        if "--" in data["state_container"] or "--" in data["index_name"]:
            raise IngestionError("Container/index names cannot contain consecutive hyphens")
        for name in ("client_id", "service_connection_id"):
            if name in data and not isinstance(data[name], str):
                raise IngestionError("Publishing identity fields must be strings")
            if data.get(name):
                try:
                    UUID(data[name])
                except ValueError as exc:
                    raise IngestionError(
                        "Client/service connection identity must be an explicit UUID"
                    ) from exc
        if data["auth_mode"] == "pipeline" and (
            not data.get("client_id") or not data.get("service_connection_id")
        ):
            raise IngestionError("Pipeline client and service connection identity are required")
        for name in ("platform_schema", "admission"):
            data[name] = (path.resolve().parent / data[name]).resolve()
        result = cls(**data)
        if (
            type(result.inventory_limit) is not int
            or not 1 <= result.inventory_limit <= 10000
            or type(result.visibility_seconds) is not int
            or not 1 <= result.visibility_seconds <= 600
        ):
            raise IngestionError("Invalid bounded inventory/visibility limits")
        return result

    def physical_id(self) -> str:
        return fingerprint([self.search_endpoint, self.index_name])

    def target(self) -> dict[str, object]:
        return {
            "search_endpoint": self.search_endpoint,
            "index_name": self.index_name,
            "embedding_endpoint": self.embedding_endpoint,
            "state_account_url": self.state_account_url,
            "state_container": self.state_container,
        }

    def admit(self, source: Settings, operation: str) -> dict[str, object]:
        if self.admission_waiver:
            # POC decision: a reviewed merge to the approved ref and the pinned worker config
            # (target, selection) are the approval. No audience/ACL claim is made or verified.
            return {
                "policy_version": 0,
                "access_mode": "uniform-approved-corpus",
                "approval_reference": self.admission_waiver,
                "target_role": "non-serving",
                "allow_bootstrap": True,
                "allow_bootstrap_existing": False,
                "allow_history_reset": False,
                "allow_selection_reset": True,
            }
        policy = pinned_json(self.admission, self.admission_sha256)
        allowed = {
            "policy_version",
            "target",
            "repo_id",
            "source_ref",
            "approved_commit",
            "approval_reference",
            "approved_audience_reference",
            "access_mode",
            "target_role",
            "selection_fingerprint",
            "allow_bootstrap",
            "allow_bootstrap_existing",
            "allow_history_reset",
            "allow_selection_reset",
        }
        if set(policy) - allowed or any(
            name in policy and type(policy[name]) is not bool
            for name in (
                "allow_bootstrap",
                "allow_bootstrap_existing",
                "allow_history_reset",
                "allow_selection_reset",
            )
        ):
            raise IngestionError("Unsupported or invalid admission policy fields")
        version = policy.get("policy_version")
        if type(version) is not int or version not in (1, 2):
            raise IngestionError("Unsupported or invalid admission policy fields")
        if version == 1:
            commit_matches = policy.get("approved_commit") == source.commit
        else:
            # Version 2 approves the scope (repo, ref, selection, target), not one commit.
            # Rebuilds keep the per-commit approval.
            commit_matches = "approved_commit" not in policy and operation != "rebuild"
        if (
            not commit_matches
            or policy.get("target") != self.target()
            or policy.get("repo_id") != source.repo_id
            or policy.get("source_ref") != source.source_ref
            or policy.get("selection_fingerprint") != source.selection_fingerprint()
        ):
            raise IngestionError("Admission scope/target/approved source snapshot mismatch")
        for field in ("approval_reference", "approved_audience_reference"):
            reference = string_value(policy, field)
            if "<" in reference or ">" in reference:
                raise IngestionError("Admission review/audience references cannot be placeholders")
        if policy.get("access_mode") != "uniform-approved-corpus":
            raise IngestionError(
                "Protected/item-level content requires a supplied platform ACL contract"
            )
        if operation == "rebuild" and policy.get("target_role") != "non-serving":
            raise IngestionError("Rebuild requires an explicitly designated non-serving generation")
        if policy.get("target_role") not in ("serving", "non-serving"):
            raise IngestionError("Admission must designate the physical target role")
        return policy


@dataclass(frozen=True)
class Prepared:
    source: Settings
    target: PublishSettings
    operation: str
    contract: Contract
    policy: dict[str, object]
    schema: dict[str, object]
    snapshot: Snapshot
    processing: str
    compatibility: str
    scope: str


def prepare(source: Settings, target: PublishSettings, operation: str) -> Prepared:
    if operation not in {"sync", "reconcile", "rebuild"}:
        raise IngestionError("Unsupported publishing operation")
    policy = target.admit(source, operation)
    contract = Contract.load(source.contract)
    schema = pinned_json(target.platform_schema, target.schema_sha256)
    if schema.get("name") != target.index_name:
        raise IngestionError("Pinned platform schema names a different physical index")
    validate_schema(
        schema,
        contract,
        deployment=source.deployment,
        model=source.model,
        embedding_resource=target.embedding_endpoint,
    )
    processing = fingerprint(
        {
            "processing": source.processing_fingerprint(),
            "consumer": contract.sha256,
            "tokenizer_asset": ASSET_SHA256,
        }
    )
    compatibility = fingerprint(
        {
            "consumer": contract.sha256,
            "schema": target.schema_sha256,
            "model": source.model,
            "deployment": source.deployment,
            "dimensions": source.dimensions,
            "embedding_endpoint": target.embedding_endpoint,
            "api_version": target.openai_api_version,
        }
    )
    return Prepared(
        source,
        target,
        operation,
        contract,
        policy,
        schema,
        read_snapshot(source),
        processing,
        compatibility,
        fingerprint([source.repo_id, source.source_ref]),
    )
