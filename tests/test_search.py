import pytest
from azure.core.pipeline.transport import HttpRequest
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure_fakes import Credential, Response, Transport, body
from test_contract import synthetic_schema

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.search import Search


def make_search(
    transport: Transport, *, max_bytes: int = 16000000, batch_size: int = 100
) -> Search:
    return Search(
        SearchClient(
            "https://synthetic.search.windows.net",
            "synthetic-index",
            Credential(),
            transport=transport,
            retry_total=0,
        ),
        SearchIndexClient(
            "https://synthetic.search.windows.net", Credential(), transport=transport, retry_total=0
        ),
        "synthetic-index",
        "synthetic-repository",
        "refs/heads/approved",
        retry=Retry(sleep=lambda _: None, jitter=lambda: 0),
        max_bytes=max_bytes,
        batch_size=batch_size,
    )


def record(key: str = "synthetic-key") -> dict[str, object]:
    return {
        "id": key,
        "source": "azure_devops",
        "repo_id": "synthetic-repository",
        "source_ref": "refs/heads/approved",
        "content": "synthetic 😀" * 10,
        "contentVector": [0.1] * 3072,
    }


def test_actual_sdk_serialization_and_exact_byte_guard() -> None:
    sent: list[int] = []

    def handle(request: HttpRequest) -> Response:
        if request.method == "GET":
            return Response(request, 404, {"error": {"code": "NotFound"}})
        actions = body(request)["value"]
        assert isinstance(actions, list)
        raw = request.body
        assert isinstance(raw, str)
        sent.append(len(raw.encode("utf-8")))
        assert all(action["@search.action"] == "upload" for action in actions)
        return Response(
            request,
            value={
                "value": [
                    {"key": action["id"], "status": True, "statusCode": 201} for action in actions
                ]
            },
        )

    transport = Transport(handle)
    first = make_search(transport)
    first.write([record()])
    size = sent[0]
    sent.clear()
    exact = make_search(transport, max_bytes=size)
    exact.write([record()])
    assert sent == [size]
    too_small = make_search(transport, max_bytes=size - 1)
    with pytest.raises(IngestionError, match="single"):
        too_small.write([record()])
    split = make_search(transport, max_bytes=size)
    split.write([record("a"), record("b")])
    assert max(sent) <= size


def test_partial_results_retry_only_failed_actions_and_capacity_fails() -> None:
    batches: list[list[str]] = []
    capacity = False

    def handle(request: HttpRequest) -> Response:
        if request.method == "GET":
            return Response(request, 404, {"error": {"code": "NotFound"}})
        actions = body(request)["value"]
        assert isinstance(actions, list)
        keys = [action["id"] for action in actions]
        batches.append(keys)
        return Response(
            request,
            value={
                "value": [
                    {
                        "key": key,
                        "status": key != "b" or len(batches) > 1,
                        "statusCode": 429
                        if capacity
                        else 503
                        if key == "b" and len(batches) == 1
                        else 201,
                    }
                    for key in keys
                ]
            },
        )

    search = make_search(Transport(handle))
    search.write([record("a"), record("b")])
    assert batches == [["a", "b"], ["b"]]
    capacity = True
    with pytest.raises(IngestionError, match="permanently"):
        search.write([record("b")])


def test_raw_schema_and_foreign_scope_never_deleted() -> None:
    def handle(request: HttpRequest) -> Response:
        if "/docs" not in request.url and request.method == "GET":
            schema = synthetic_schema()
            schema["name"] = "synthetic-index"
            return Response(request, value=schema)
        return Response(request, value={**record(), "repo_id": "foreign"})

    search = make_search(Transport(handle))
    assert search.schema()["name"] == "synthetic-index"
    with pytest.raises(IngestionError, match="another source"):
        search.delete(["synthetic-key"])


def test_incomplete_or_duplicate_results_are_not_success() -> None:
    def handle(request: HttpRequest) -> Response:
        if request.method == "GET":
            return Response(request, 404, {})
        return Response(request, value={"value": []})

    with pytest.raises(IngestionError, match="count"):
        make_search(Transport(handle)).write([record()])


def test_count_batches_and_empty_inventory_scope_filter() -> None:
    counts: list[int] = []

    def handle(request: HttpRequest) -> Response:
        if request.method == "GET":
            return Response(request, 404, {})
        data = body(request)
        if "search" in data:
            assert "repo_id eq 'synthetic-repository'" in str(data["filter"])
            return Response(request, value={"@odata.count": 0, "value": []})
        actions = data["value"]
        assert isinstance(actions, list)
        counts.append(len(actions))
        return Response(
            request,
            value={
                "value": [
                    {"key": action["id"], "status": True, "statusCode": 201} for action in actions
                ]
            },
        )

    search = make_search(Transport(handle), batch_size=1000)
    search.write([record(str(index)) for index in range(1001)])
    assert counts == [1000, 1]
    assert search.scan() == {}


@pytest.mark.parametrize("fault", ["none", "duplicate", "missing-page", "over-limit"])
def test_paginated_inventory_is_complete_or_fails_explicitly(fault: str) -> None:
    def handle(request: HttpRequest) -> Response:
        query = body(request)
        skip, top = query.get("skip", 0), query.get("top", 1000)
        assert isinstance(skip, int) and isinstance(top, int)
        count = 10001 if fault == "over-limit" else 1001
        rows = [record(str(index)) for index in range(skip, min(1001, skip + top))]
        for row in rows:
            row.pop("contentVector")
        if skip and fault == "duplicate":
            rows = [record("0")]
        if skip and fault == "missing-page":
            rows = []
        return Response(request, value={"@odata.count": count, "value": rows})

    search = make_search(Transport(handle))
    if fault == "none":
        assert len(search.scan()) == 1001
    else:
        with pytest.raises(IngestionError):
            search.scan()
