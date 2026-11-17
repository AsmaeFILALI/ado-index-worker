"""Identity, citation, source-read, embedding, and Search write-path checks."""

import json
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from azure.core.pipeline.transport import HttpRequest
from azure_fakes import Response, Transport
from conftest import GitRepo
from openai import AzureOpenAI
from test_search import make_search, record

from ado_markdown_ingestion.contract import REQUIRED_NAMES
from ado_markdown_ingestion.embedding_text import embedding_text
from ado_markdown_ingestion.embeddings import Embeddings, embedding_retry
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.pipeline import validate_context
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.source import read_snapshot, validate_path
from ado_markdown_ingestion.sync import create_plan

from .fixtures_catalog import FIXTURES, PATH_FIXTURES
from .review_support import KEY_PATTERN, ROOT, drafts, encoder, run_text

# ---------------------------------------------------------------- keys and citations


@pytest.mark.parametrize("path", PATH_FIXTURES)
def test_document_keys_and_citations_are_valid_for_awkward_paths(path: str) -> None:
    run = run_text("# T\n\n## S\n\nBody.\n", path=path)
    documents = drafts(run, path=path)
    assert KEY_PATTERN.fullmatch(documents[0].id)
    query = parse_qs(urlsplit(documents[0].url).query)
    assert query["path"] == ["/" + path]
    assert query["version"][0].startswith("GC") and query["_a"] == ["contents"]
    assert documents[0].url == documents[0].filepath


def test_same_file_name_in_different_folders_or_case_gets_different_ids() -> None:
    ids = set()
    for path in ("docs/a/readme.md", "docs/b/readme.md", "docs/readme.md", "Docs/Readme.md"):
        run = run_text("# T\n\n## S\n\nBody.\n", path=path)
        ids.add(drafts(run, path=path)[0].id)
    assert len(ids) == 4


@pytest.mark.parametrize("path", ["docs/Q: A.md", "docs\\x.md", "docs/./x.md", "docs/.git/x.md"])
def test_unsafe_or_nonportable_paths_are_rejected(path: str) -> None:
    with pytest.raises(IngestionError):
        validate_path(path)


def test_citation_has_no_heading_anchor_so_every_chunk_cites_the_whole_file() -> None:
    run = run_text("# T\n\n## A\n\na\n\n## B\n\nb\n")
    urls = {d.url for d in drafts(run)}
    assert len(urls) == 1 and "#" not in next(iter(urls)) and "anchor" not in next(iter(urls))


# ---------------------------------------------------------------- one bad file


def test_one_bad_file_does_not_block_the_snapshot(repo: GitRepo) -> None:
    repo.write("docs/good.md", "# Good\n\n## S\n\nbody\n")
    (repo.path / "docs" / "bad.md").write_bytes(FIXTURES["file-cp1252-french"])
    repo.commit()
    snapshot = read_snapshot(repo.settings())
    assert [file.path for file in snapshot.files] == ["docs/good.md"]
    assert [(q.path, q.reason) for q in snapshot.quarantined] == [("docs/bad.md", "invalid-utf8")]


def test_one_unchunkable_file_does_not_block_the_plan(repo: GitRepo) -> None:
    repo.write("docs/good.md", "# Good\n\n## S\n\nbody\n")
    repo.write("docs/giant.md", "# T\n\n## " + "H" * 5000 + "\n\nbody\n")
    repo.commit()
    plan = create_plan(replace(repo.settings(), max_content_chars=60), encoder())
    counts = plan["counts"]
    assert isinstance(counts, dict) and counts["quarantined"] >= 1 and counts["documents"] >= 0


def test_pr_builds_run_the_offline_check_but_nothing_else() -> None:
    with pytest.raises(IngestionError, match="PR/untrusted"):
        validate_context({"BUILD_REASON": "PullRequest"}, publishing=False, operation="plan")
    with pytest.raises(IngestionError, match="INGEST_WORKER_ROOT"):
        validate_context(
            {"BUILD_REASON": "PullRequest"}, publishing=False, operation="plan", check=True
        )


# ---------------------------------------------------------------- embeddings


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def embeddings(
    handler: Callable[[httpx.Request], httpx.Response], retry: Retry | None = None
) -> Embeddings:
    client = AzureOpenAI(
        azure_endpoint="https://synthetic.openai.azure.com",
        api_version="2024-10-21",
        azure_ad_token_provider=lambda: "synthetic-test-token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_retries=0,
    )
    return Embeddings(client, encoder(), "text-embedding", retry=retry)


def ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "text-embedding-3-large",
            "data": [
                {"object": "embedding", "index": i, "embedding": [0.1] * 3072}
                for i in range(len(body["input"]))
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


def test_embedding_request_pins_dimensions_and_float_encoding() -> None:
    sent: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return ok(request)

    with embeddings(handle) as model:
        model.embed(["Title: T\nHeading: T > S\n\nBody.\n"])
    assert sent[0]["dimensions"] == 3072 and sent[0]["encoding_format"] == "float"
    assert sent[0]["input"] == ["Title: T\nHeading: T > S\n\nBody.\n"]


def test_embedding_text_drops_base64_payloads_and_leaves_other_content_unchanged() -> None:
    plain = "T > S\n\nSee [link](https://example.invalid/a) and ![alt](img/a.png).\n"
    assert embedding_text(plain) == plain
    for chunk in run_text(FIXTURES["images"].decode("utf-8")).chunks:
        assert "base64," not in embedding_text(chunk.content) or "AAAA" not in embedding_text(
            chunk.content
        )
    payload = "A" * 6000
    derived = embedding_text(f"x ![b](data:image/png;base64,{payload}) y")
    assert payload not in derived and derived == "x ![b](data:[omitted]) y"


def test_embedding_input_uses_the_derived_text_in_sync() -> None:
    text = (ROOT / "src" / "ado_markdown_ingestion" / "sync.py").read_text(encoding="utf-8")
    assert 'embedding_text(str(record["content"]))' in text


def test_sustained_throttling_is_survivable() -> None:
    clock = Clock()
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 4:
            return httpx.Response(
                429,
                headers={"retry-after": "30"},
                json={"error": {"code": "429", "message": "rate limited"}},
            )
        return ok(request)

    retry = embedding_retry(sleep=clock.sleep, clock=clock, jitter=lambda: 0.0)
    with embeddings(handle, retry) as model:
        assert len(model.embed(["synthetic"])) == 1
    assert clock.now >= 120


def test_single_throttle_with_retry_after_is_honoured() -> None:
    clock = Clock()
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429, headers={"retry-after": "7"}, json={"error": {"code": "429"}}
            )
        return ok(request)

    retry = Retry(sleep=clock.sleep, clock=clock, jitter=lambda: 0.0)
    with embeddings(handle, retry) as model:
        assert len(model.embed(["synthetic"])) == 1
    assert clock.now >= 7


def test_vectors_are_python_float_lists_costing_about_100_kilobytes_each() -> None:
    import sys

    from ado_markdown_ingestion.contract import validate_vector

    vector = validate_vector([0.123456789 + i * 1e-6 for i in range(3072)])
    size = sys.getsizeof(vector) + sum(sys.getsizeof(value) for value in vector)
    assert 90_000 < size < 110_000  # 10,000 chunks held in memory before upload is about 1 GB


# ---------------------------------------------------------------- Search write path


def counting_transport(counts: dict[str, int], status: int = 201) -> Transport:
    def handle(request: HttpRequest) -> Response:
        counts[request.method] = counts.get(request.method, 0) + 1
        if request.method == "GET":
            return Response(request, 404, {"error": {"code": "NotFound"}})
        body = json.loads(str(request.body))
        return Response(
            request,
            value={
                "value": [
                    {"key": action["id"], "status": True, "statusCode": status}
                    for action in body["value"]
                ]
            },
        )

    return Transport(handle)


def test_upload_keeps_the_ownership_read_before_each_document() -> None:
    # Decision: the guard stays (it protects documents of another scope with the same key).
    counts: dict[str, int] = {}
    search = make_search(counting_transport(counts))
    search.write([record(f"key-{i}") for i in range(25)])
    assert counts.get("GET", 0) == 25


def test_delete_keeps_the_ownership_read_before_each_key() -> None:
    counts: dict[str, int] = {}
    search = make_search(counting_transport(counts, 200))
    search.delete([f"key-{i}" for i in range(25)])
    assert counts.get("GET", 0) == 25


def test_inventory_scan_has_a_hard_ceiling_of_10000_documents() -> None:
    def handle(request: HttpRequest) -> Response:
        return Response(request, value={"@odata.count": 10_001, "value": []})

    with pytest.raises(IngestionError, match="supported scan bound"):
        make_search(Transport(handle)).scan()


# ---------------------------------------------------------------- schema facts


def test_example_schema_facts_used_by_the_design_recommendations() -> None:
    schema = json.loads((ROOT / "examples" / "index-schema.example.json").read_text("utf-8"))
    fields = {field["name"]: field for field in schema["fields"]}
    assert fields["content"]["analyzer"] == "standard.lucene"  # no en/fr language analyzer
    assert fields["contentVector"]["dimensions"] == 3072
    assert fields["url"]["searchable"] is False and fields["url"]["retrievable"] is True
    semantic = schema["semantic"]["configurations"][0]["prioritizedFields"]
    assert semantic["titleField"]["fieldName"] == "title"  # same value for every chunk of a file
    assert [f["fieldName"] for f in semantic["prioritizedKeywordsFields"]] == [
        "heading_path",
        "category",
    ]
    assert schema["vectorSearch"]["algorithms"][0]["hnswParameters"]["m"] == 4


def test_no_language_or_front_matter_metadata_fields_exist_in_the_consumer_contract() -> None:
    assert not {"language", "category", "owner", "classification", "review_by"} & REQUIRED_NAMES
