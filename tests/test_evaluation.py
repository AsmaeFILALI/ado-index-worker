import json
from urllib.parse import parse_qs, urlsplit

from conftest import CONTRACT, ROOT, GitRepo

from ado_markdown_ingestion.contract import Contract
from ado_markdown_ingestion.documents import build_documents
from ado_markdown_ingestion.markdown import parse_chunks
from ado_markdown_ingestion.source import read_snapshot
from ado_markdown_ingestion.tokenizer import load_encoder


def test_original_questions_resolve_to_expected_deterministic_source_sections(
    repo: GitRepo,
) -> None:
    directory = ROOT / "tests" / "fixtures"
    fixture = json.loads((directory / "evaluation.json").read_text(encoding="utf-8"))
    assert fixture["evidence"] == "original-synthetic-expectations-not-retrieval-results"
    repo.write(
        fixture["source_path"],
        (directory / fixture["markdown_fixture"]).read_text(encoding="utf-8"),
    )
    commit = repo.commit()
    settings = repo.settings(
        commit,
        repo_id=fixture["repo_id"],
        source_ref=fixture["source_ref"],
        repo_url=fixture["repo_url"],
    )
    source = read_snapshot(settings).files[0]
    encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    chunks = parse_chunks(source.content, source.path, settings, encoder)
    documents = build_documents(source, chunks, settings, Contract.load(CONTRACT))
    by_id = {document.id: document for document in documents}
    assert len(fixture["cases"]) == len(documents) == 4
    for case in fixture["cases"]:
        assert case["question"] and case["id"]
        document = by_id[case["expected_document_id"]]
        assert document.section_id == case["expected_section_id"]
        assert document.heading_path == case["expected_heading_path"]
        assert case["expected_text"] in document.content
        assert document.url == document.filepath
        query = parse_qs(urlsplit(document.url).query)
        assert query["version"] == ["GC" + commit]
        assert query["path"] == ["/" + fixture["source_path"]]
        assert len(document.content) <= 3800 and encoder.count(document.content) <= 900
    assert len({case["expected_section_id"] for case in fixture["cases"]}) == 4
