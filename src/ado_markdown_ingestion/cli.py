"""CLI with an Azure-free plan path and explicit admission before publishing."""

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from ado_markdown_ingestion import __version__
from ado_markdown_ingestion.config import FIELDS, INT_FIELDS, load_settings
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.sync import create_plan
from ado_markdown_ingestion.tokenizer import load_encoder, prepare_tokenizer


def _path(args: dict[str, object], key: str) -> Path | None:
    value = args.get(key)
    return Path(value) if isinstance(value, str) else None


def _source_options(command: argparse.ArgumentParser) -> None:
    for field in sorted(FIELDS):
        option = "--" + field.replace("_", "-")
        if field in {"include", "exclude"}:
            command.add_argument(option, action="append")
        elif field in INT_FIELDS:
            command.add_argument(option, type=int)
        else:
            command.add_argument(option)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ado-ingest",
        description="Markdown ingestion worker. Publishing needs explicit scoped admission.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_command = commands.add_parser(
        "prepare-tokenizer",
        help="Explicit dependency preparation; may download one pinned public asset",
    )
    prepare_command.add_argument("--cache-dir", required=True)
    plan = commands.add_parser(
        "plan", help="Parse a fixed Git snapshot without Azure or network access"
    )
    plan.add_argument("--config")
    _source_options(plan)
    plan.add_argument("--previous-inventory")
    plan.add_argument("--previous-sha256")
    plan.add_argument("--platform-schema")
    plan.add_argument("--schema-sha256")
    plan.add_argument(
        "--documents", action="store_true", help="Include canonical source draft text"
    )
    plan.add_argument("--output", help="Write local JSON artifact outside the source checkout")
    plan.add_argument(
        "--fail-on-quarantine",
        action="store_true",
        help="Exit with status 3 when any selected file was quarantined",
    )
    check = commands.add_parser(
        "check",
        help="Pull-request validation: chunk the checked-out commit offline; needs no ref or Azure",
    )
    check.add_argument("--config")
    _source_options(check)
    for operation in ("sync", "reconcile", "rebuild"):
        command = commands.add_parser(
            operation, help="Admitted existing-index publication; never provisions or promotes"
        )
        command.add_argument("--config", required=False)
        command.add_argument("--publishing-config")
        command.add_argument("--authorize-publish", action="store_true")
        command.add_argument("--fail-on-quarantine", action="store_true")
    args: dict[str, object] = vars(parser.parse_args(argv))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("ado_ingest")
    run_id = str(uuid.uuid4())
    started = time.perf_counter()
    try:
        if args["command"] == "prepare-tokenizer":
            cache = _path(args, "cache_dir")
            if cache is None:
                raise IngestionError("An explicit tokenizer cache directory is required")
            print(json.dumps({"encoding": "cl100k_base", "sha256": prepare_tokenizer(cache)}))
            return 0
        if args["command"] in {"sync", "reconcile", "rebuild"}:
            if args.get("authorize_publish") is not True:
                raise IngestionError(
                    "Publishing requires explicit --authorize-publish and scoped admission"
                )
            source_config, target_config = _path(args, "config"), _path(args, "publishing_config")
            if source_config is None or target_config is None:
                raise IngestionError(
                    "Publishing requires explicit source and publishing configuration"
                )
            from ado_markdown_ingestion.publishing import PublishSettings, prepare

            source = load_settings(source_config, {})
            target = PublishSettings.load(target_config)
            prepared = prepare(source, target, str(args["command"]))
            encoder = load_encoder(source.tokenizer_cache)
            logger.info(
                json.dumps(
                    {
                        "event": "publishing_target",
                        "operation": prepared.operation,
                        "run_id": run_id,
                        "target": target.target(),
                        "repo_id": source.repo_id,
                        "source_ref": source.source_ref,
                        "source_version_id": source.commit,
                        "schema_sha256": target.schema_sha256,
                        "processing_fingerprint": prepared.processing,
                        "admission_sha256": target.admission_sha256
                        if not target.admission_waiver
                        else "waived",
                        "selection_fingerprint": source.selection_fingerprint(),
                        "embedding_model": source.model,
                        "embedding_deployment": source.deployment,
                        "dimensions": source.dimensions,
                    }
                )
            )
            from ado_markdown_ingestion.clients import clients
            from ado_markdown_ingestion.sync import publish

            with clients(prepared, encoder) as (search, state, model):
                publication = publish(prepared, search, state, model, encoder, run_id=run_id)
            logger.info(
                json.dumps(
                    {
                        "event": "publication_completed",
                        "run_id": run_id,
                        "counts": publication["counts"],
                        "elapsed_ms": publication["duration_ms"],
                    }
                )
            )
            print(json.dumps(publication, indent=2))
            if args.get("fail_on_quarantine") is True and publication["quarantined"]:
                logger.error(json.dumps({"event": "quarantined_files", "run_id": run_id}))
                return 3
            return 0
        settings = load_settings(
            _path(args, "config"),
            {key: value for key, value in args.items() if key in FIELDS and value is not None},
        )
        if args["command"] == "check":
            from ado_markdown_ingestion.check import check_snapshot

            report = check_snapshot(settings, load_encoder(settings.tokenizer_cache))
            sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=True) + "\n")
            logger.info(
                json.dumps(
                    {"event": "check_completed", "run_id": run_id, "counts": report["counts"]}
                )
            )
            return 1 if report["problems"] else 0
        result = create_plan(
            settings,
            load_encoder(settings.tokenizer_cache),
            previous_path=_path(args, "previous_inventory"),
            previous_sha256=str(args["previous_sha256"]) if args["previous_sha256"] else None,
            include_documents=args["documents"] is True,
            schema_path=_path(args, "platform_schema"),
            schema_sha256=str(args["schema_sha256"]) if args["schema_sha256"] else None,
            run_id=run_id,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        result["timings"] = {"total_ms": elapsed_ms}
        output = _path(args, "output")
        encoded = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if output is not None:
            protected = {
                settings.contract,
                _path(args, "config"),
                _path(args, "previous_inventory"),
                _path(args, "platform_schema"),
            }
            if output.resolve().is_relative_to(settings.source_root) or (
                output.resolve() in {path.resolve() for path in protected if path is not None}
            ):
                raise IngestionError("Output must not overwrite source or input artifacts")
            try:
                output.write_text(encoded, encoding="utf-8")
            except OSError as exc:
                raise IngestionError("Cannot write the requested offline output artifact") from exc
            print(json.dumps({"run_id": result["run_id"], "counts": result["counts"]}))
        else:
            sys.stdout.write(encoded)
        logger.info(
            json.dumps(
                {
                    "event": "plan_completed",
                    "run_id": result["run_id"],
                    "counts": result["counts"],
                    "elapsed_ms": elapsed_ms,
                }
            )
        )
        if args.get("fail_on_quarantine") is True and result["quarantined"]:
            logger.error(json.dumps({"event": "quarantined_files", "run_id": run_id}))
            return 3
        return 0
    except IngestionError as exc:
        logger.error(
            json.dumps(
                {
                    "event": "ingestion_failed",
                    "run_id": run_id,
                    "error": str(exc),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
