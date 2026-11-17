"""Thin pipeline boundary: bind reviewed settings to immutable agent checkouts."""

import argparse
import json
import logging
import os
import re
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from ado_markdown_ingestion.cli import main as worker_main
from ado_markdown_ingestion.config import Settings, load_settings, validate_ref
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import PublishSettings, prepare
from ado_markdown_ingestion.source import _git
from ado_markdown_ingestion.util import string_value

ALLOWED_REASONS = {"IndividualCI", "BatchedCI", "Manual", "Schedule"}


def _input_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise IngestionError("Reviewed pipeline configuration must be a file in the pinned worker")
    return path


def _checkout(root: Path) -> str:
    try:
        actual_root = Path(_git(root, "rev-parse", "--show-toplevel").stdout.decode().strip())
        commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
    except UnicodeError as exc:
        raise IngestionError("Invalid pipeline checkout identity encoding") from exc
    if actual_root.resolve() != root or not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise IngestionError("Pipeline paths must identify exact Git checkout roots")
    return commit


def validate_context(
    environ: Mapping[str, str],
    *,
    publishing: bool,
    operation: str,
    check: bool = False,
) -> tuple[Settings, Path, str]:
    if any(key.startswith("ADO_INGEST_") for key in environ):
        raise IngestionError(
            "Pipeline settings must not be overridden by ambient ADO_INGEST variables"
        )
    # The offline check job validates pull-request content; it has no Azure access or secrets.
    allowed_reasons = ALLOWED_REASONS | {"PullRequest"} if check else ALLOWED_REASONS
    if environ.get("BUILD_REASON") not in allowed_reasons:
        raise IngestionError("PR/untrusted build reasons may run synthetic offline tests only")
    branch = ""
    if not check:
        branch = string_value(environ, "INGEST_APPROVED_REF")
        validate_ref(branch)
        if not branch.startswith("refs/heads/") or environ.get("BUILD_SOURCEBRANCH") != branch:
            raise IngestionError("Pipeline source branch is not the approved full branch ref")
    if publishing and environ.get("INGEST_ALLOW_PUBLISH") != "true":
        raise IngestionError("Pipeline publishing must be explicitly enabled")
    if operation == "rebuild" and environ.get("BUILD_REASON") != "Manual":
        raise IngestionError("Pipeline rebuild requires an explicitly selected manual run")
    worker_root = Path(string_value(environ, "INGEST_WORKER_ROOT")).resolve()
    source_root = Path(string_value(environ, "INGEST_SOURCE_ROOT")).resolve()
    if worker_root.is_relative_to(source_root) or source_root.is_relative_to(worker_root):
        raise IngestionError("Content and worker checkouts must use distinct non-nested paths")
    expected = (environ.get("INGEST_WORKER_COMMIT") or "").strip()
    resolved = environ.get("INGEST_RESOLVED_WORKER_COMMIT")
    checked_out = _checkout(worker_root)
    if expected:
        if not re.fullmatch(r"[a-f0-9]{40}", expected):
            raise IngestionError("Worker commit pin must be a full 40-character lowercase commit")
        if resolved != expected or checked_out != expected:
            # Commit identifiers are not secrets; they identify which comparison failed.
            raise IngestionError(
                "Worker checkout/resource version does not match the reviewed full commit pin "
                f"(pin={expected}, resource_version={resolved!r}, checkout_head={checked_out})"
            )
    # With no pin the worker repository's checked-out ref (POC: main) is trusted as-is.
    commit = string_value(environ, "BUILD_SOURCEVERSION")
    if _checkout(source_root) != commit:
        raise IngestionError(
            "Content checkout does not match the pipeline's immutable source commit"
        )
    source = load_settings(
        _input_path(worker_root, string_value(environ, "INGEST_SOURCE_CONFIG")),
        {
            "source_root": str(source_root),
            "commit": commit,
            "contract": str(worker_root / "docs" / "index-contract.json"),
            "tokenizer_cache": str(worker_root / ".cache" / "tiktoken"),
        },
        {},
    )
    if (not check and source.source_ref != branch) or source.repo_id != environ.get(
        "BUILD_REPOSITORY_ID"
    ):
        raise IngestionError(
            "Reviewed source identity/ref differs from the checked-out pipeline repository"
        )
    if check:
        return source, worker_root, "check"
    # HEAD, BUILD_SOURCEVERSION and BUILD_SOURCEBRANCH were verified above. Persistent self-hosted
    # workspaces keep a stale local branch from an earlier run, so always point the approved ref
    # at this run's immutable commit (detached hosted checkouts have no local branch yet).
    _git(source_root, "update-ref", branch, commit)
    effective_operation = "reconcile" if environ["BUILD_REASON"] == "Schedule" else operation
    return source, worker_root, effective_operation


def _write_toml(path: Path, data: Mapping[str, object]) -> None:
    try:
        path.write_text(
            "\n".join(
                name
                + " = "
                + json.dumps(
                    str(value) if isinstance(value, Path) else value,
                    ensure_ascii=False,
                )
                for name, value in data.items()
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise IngestionError("Cannot write temporary pipeline settings") from exc


def run(
    mode: str,
    operation: str,
    environ: Mapping[str, str],
) -> int:
    source, worker_root, effective_operation = validate_context(
        environ,
        publishing=mode == "publish",
        operation=operation,
        check=mode == "check",
    )
    temp_root = Path(string_value(environ, "INGEST_TEMP_ROOT")).resolve()
    if (
        not temp_root.is_dir()
        or temp_root.is_relative_to(source.source_root)
        or temp_root.is_relative_to(worker_root)
    ):
        raise IngestionError("Pipeline temporary directory must be outside both checkouts")
    with tempfile.TemporaryDirectory(prefix="ado-ingest-", dir=temp_root) as directory:
        work = Path(directory)
        source_config = work / "source.toml"
        _write_toml(source_config, asdict(source))
        if mode == "check":
            return worker_main(["check", "--config", str(source_config)])
        policy = (
            ["--fail-on-quarantine"] if environ.get("INGEST_FAIL_ON_QUARANTINE") == "true" else []
        )
        if mode == "plan":
            return worker_main(
                [
                    "plan",
                    "--config",
                    str(source_config),
                    "--output",
                    str(work / "plan.json"),
                    *policy,
                ]
            )
        if mode != "publish":
            raise IngestionError("Unknown pipeline adapter mode")
        if not environ.get("SYSTEM_ACCESSTOKEN") or not environ.get("idToken"):
            raise IngestionError(
                "Publishing requires the authorized WIF task and in-memory job token"
            )
        if environ.get("servicePrincipalKey"):
            raise IngestionError("Secret-based service connections are not supported")
        original = _input_path(worker_root, string_value(environ, "INGEST_PUBLISHING_CONFIG"))
        try:
            with original.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise IngestionError("Cannot read reviewed publishing configuration") from exc
        admission_path = environ.get("INGEST_ADMISSION_PATH")
        admission_sha256 = environ.get("INGEST_ADMISSION_SHA256")
        if bool(admission_path) != bool(admission_sha256):
            raise IngestionError(
                "External admission path and SHA-256 pin must be supplied together"
            )
        if data.get("admission_waiver"):
            if admission_path or "admission" in data or "admission_sha256" in data:
                raise IngestionError("An admission waiver cannot be combined with an admission")
        elif admission_path and admission_sha256:
            data.update({"admission": admission_path, "admission_sha256": admission_sha256})
        else:
            # Reviewed in-repo admission: the pinned worker commit is the approval record.
            relative = data.get("admission")
            if not isinstance(relative, str) or "admission_sha256" not in data:
                raise IngestionError("Publishing requires an external or reviewed admission pin")
            data["admission"] = str(_input_path(worker_root, relative))
        schema_path = environ.get("INGEST_SCHEMA_PATH")
        if schema_path:
            data["platform_schema"] = schema_path
        else:
            # Reviewed in-repo schema copy: still byte-pinned by schema_sha256.
            relative_schema = data.get("platform_schema")
            if not isinstance(relative_schema, str):
                raise IngestionError("Publishing requires an external or reviewed schema file")
            data["platform_schema"] = str(_input_path(worker_root, relative_schema))
        publishing_config = work / "publishing.toml"
        _write_toml(publishing_config, data)
        target = PublishSettings.load(publishing_config)
        if (
            target.auth_mode != "pipeline"
            or target.tenant_id != environ.get("tenantId")
            or target.client_id != environ.get("servicePrincipalId")
            or target.service_connection_id != environ.get("INGEST_SERVICE_CONNECTION_ID")
        ):
            raise IngestionError(
                "Federated task identity does not match the reviewed publishing identity"
            )
        prepare(source, target, effective_operation)
        return worker_main(
            [
                effective_operation,
                "--config",
                str(source_config),
                "--publishing-config",
                str(publishing_config),
                "--authorize-publish",
                *policy,
            ]
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pinned pipeline adapter; never provisions resources"
    )
    parser.add_argument("mode", choices=("plan", "publish", "check"))
    parser.add_argument("--operation", choices=("sync", "reconcile", "rebuild"), default="sync")
    args = parser.parse_args(argv)
    try:
        return run(args.mode, args.operation, os.environ)
    except IngestionError as exc:
        logging.getLogger("ado_ingest").error(
            json.dumps(
                {
                    "event": "pipeline_admission_failed",
                    "error": str(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
