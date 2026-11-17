from dataclasses import replace

import pytest
from conftest import CONTRACT, ROOT, GitRepo

from ado_markdown_ingestion.contract import Contract, validate_document
from ado_markdown_ingestion.documents import build_documents
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import parse_chunks
from ado_markdown_ingestion.source import read_snapshot
from ado_markdown_ingestion.tokenizer import load_encoder
from ado_markdown_ingestion.util import digest


def draft(repo: GitRepo) -> dict[str, object]:
    repo.write("guide.md", "# Synthetic Guide\n## Reading\nThe fictional gauge reads 42.\n")
    settings = repo.settings(repo.commit())
    source = read_snapshot(settings).files[0]
    return build_documents(
        source,
        parse_chunks(
            source.content, source.path, settings, load_encoder(ROOT / ".cache" / "tiktoken")
        ),
        settings,
        Contract.load(CONTRACT),
    )[0].to_dict()


def test_draft_has_no_fake_vectors_or_ingestion_provenance(repo: GitRepo) -> None:
    document = draft(repo)
    assert len(document) == 20
    assert not document.keys() & {"contentVector", "ingested_at", "ingest_run_id"}
    assert document["content_checksum_sha256"] == digest(str(document["content"]))
    assert document["url"] == document["filepath"]
    validate_document(document, Contract.load(CONTRACT), draft=True)
    with pytest.raises(IngestionError, match="missing"):
        validate_document(document, Contract.load(CONTRACT))


def test_ids_stable_under_content_edits_not_scope_or_heading_changes(repo: GitRepo) -> None:
    first = draft(repo)
    repo.write("guide.md", "# Synthetic Guide\n## Reading\nA changed fictional reading.\n")
    settings = repo.settings(repo.commit())
    file = read_snapshot(settings).files[0]
    encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    chunks = parse_chunks(file.content, file.path, settings, encoder)
    second = build_documents(file, chunks, settings, Contract.load(CONTRACT))[0].to_dict()
    assert first["id"] == second["id"]
    assert first["content_checksum_sha256"] != second["content_checksum_sha256"]
    assert first["source_version_id"] != second["source_version_id"]
    foreign = build_documents(
        file, chunks, replace(settings, repo_id="other-repository"), Contract.load(CONTRACT)
    )[0]
    assert foreign.id != first["id"] and foreign.parent_id != first["parent_id"]
    renamed_chunks = parse_chunks("# Synthetic Guide\n## New\nBody", file.path, settings, encoder)
    assert (
        build_documents(file, renamed_chunks, settings, Contract.load(CONTRACT))[0].id
        != first["id"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_id", True),
        ("start_line", 0),
        ("end_line", -1),
        ("part", 0),
        ("part_count", 0),
        ("content_checksum_sha256", "bad"),
        ("filepath", "https://example.invalid"),
        ("source_version_id", "HEAD"),
        ("id", "invalid key"),
        ("content", " " * 4001),
    ],
)
def test_invalid_draft_fields_fail(repo: GitRepo, field: str, value: object) -> None:
    document = draft(repo)
    document[field] = value
    with pytest.raises(IngestionError):
        validate_document(document, Contract.load(CONTRACT), draft=True)


@pytest.mark.parametrize(
    "bad",
    [[0.1] * 1536, [float("nan")] * 3072, [0.0] * 3072, [True] * 3072, [10**500] * 3072],
)
def test_full_validator_rejects_bad_vectors(repo: GitRepo, bad: object) -> None:
    full = {
        **draft(repo),
        "contentVector": bad,
        "ingested_at": "2026-10-26T00:00:00Z",
        "ingest_run_id": "synthetic-validator-test",
    }
    with pytest.raises(IngestionError, match="contentVector"):
        validate_document(full, Contract.load(CONTRACT))


def test_full_validator_and_utc_provenance(repo: GitRepo) -> None:
    full = {
        **draft(repo),
        "contentVector": [0.1] * 3072,
        "ingested_at": "2026-10-26T00:00:00Z",
        "ingest_run_id": "synthetic-validator-test",
    }
    validate_document(full, Contract.load(CONTRACT))
    with pytest.raises(IngestionError, match="draft"):
        validate_document(full, Contract.load(CONTRACT), draft=True)
    for bad in ["not-a-time", "2026-10-26T00:00:00", "2026-10-26T00:00:00+05:00"]:
        full["ingested_at"] = bad
        with pytest.raises(IngestionError, match="UTC"):
            validate_document(full, Contract.load(CONTRACT))
