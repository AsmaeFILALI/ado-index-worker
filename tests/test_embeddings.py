import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import ROOT
from openai import AzureOpenAI

from ado_markdown_ingestion.embeddings import Embeddings, make_credential, make_openai
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import PublishSettings
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.tokenizer import load_encoder


def target(tmp_path: Path) -> PublishSettings:
    return PublishSettings(
        "https://synthetic.search.windows.net",
        "synthetic-index",
        "https://synthetic.openai.azure.com",
        "2024-10-21",
        "https://synthetic.blob.core.windows.net",
        "synthetic-state",
        tmp_path / "schema.json",
        "0" * 64,
        tmp_path / "admission.json",
        "0" * 64,
        "00000000-0000-0000-0000-000000000001",
        "cli",
    )


def adapter(handler: httpx.MockTransport) -> Embeddings:
    client = AzureOpenAI(
        azure_endpoint="https://synthetic.openai.azure.com",
        api_version="2024-10-21",
        azure_ad_token_provider=lambda: "synthetic-test-token",
        http_client=httpx.Client(transport=handler),
        max_retries=0,
    )
    return Embeddings(
        client,
        load_encoder(ROOT / ".cache" / "tiktoken"),
        "text-embedding",
        retry=Retry(sleep=lambda _: None, jitter=lambda: 0),
    )


def test_real_client_batches_reorders_and_uses_deployment() -> None:
    requests: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert "text-embedding/embeddings" in str(request.url)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-large",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1] * 3072}
                    for i in reversed(range(len(body["input"])))
                ],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            },
        )

    embedding = adapter(httpx.MockTransport(handle))
    assert len(embedding.embed(["synthetic"] * 17)) == 17
    sizes: list[int] = []
    for body in requests:
        values = body["input"]
        assert isinstance(values, list)
        sizes.append(len(values))
    assert sizes == [16, 1]
    assert embedding.model_revision is None
    embedding.close()


@pytest.mark.parametrize(
    "kind", ["dimensions", "model", "index", "nan", "empty", "revision", "bad-json"]
)
def test_invalid_embedding_responses_fail(kind: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if kind == "bad-json":
            return httpx.Response(
                200, content=b"not-json", headers={"Content-Type": "application/json"}
            )
        data: dict[str, object] = {
            "object": "list",
            "model": "wrong" if kind == "model" else "text-embedding-3-large",
            "data": [
                {
                    "object": "embedding",
                    "index": 1 if kind == "index" else 0,
                    "embedding": [0.0 if kind == "empty" else 0.1]
                    * (2 if kind == "dimensions" else 3072),
                }
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
        if kind == "nan":
            entries = data["data"]
            assert isinstance(entries, list)
            entries[0]["embedding"][0] = float("nan")
            return httpx.Response(200, content=json.dumps(data))
        if kind == "revision":
            data["model_version"] = 123
        return httpx.Response(200, json=data)

    with adapter(httpx.MockTransport(handle)) as embedding, pytest.raises(IngestionError):
        embedding.embed(["synthetic"])


def test_transient_throttle_but_not_authentication_retry() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(
                429, json={"error": {"message": "synthetic throttle"}}, headers={"Retry-After": "0"}
            )
        return httpx.Response(401, json={"error": {"message": "synthetic denied"}})

    embedding = adapter(httpx.MockTransport(handle))
    with pytest.raises(IngestionError, match="permanently"):
        embedding.embed(["synthetic"])
    assert calls == 3 and embedding.retry.retries == 2
    embedding.close()


def test_explicit_credentials_construct_without_login(tmp_path: Path) -> None:
    credential = make_credential(target(tmp_path), {})
    assert type(credential).__name__ == "AzureCliCredential"
    credential.close()
    pipeline = replace(
        target(tmp_path),
        auth_mode="pipeline",
        client_id="synthetic-client",
        service_connection_id="synthetic-connection",
    )
    with pytest.raises(IngestionError, match="job token"):
        make_credential(pipeline, {})
    credential = make_credential(pipeline, {"SYSTEM_ACCESSTOKEN": "synthetic-job-token"})
    assert type(credential).__name__ == "AzurePipelinesCredential"
    credential.close()


def test_production_client_factory_does_not_acquire_a_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = target(tmp_path)
    for name in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    credential = make_credential(config, {})

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Construction must not acquire a real Entra token")

    monkeypatch.setattr(credential, "get_token", forbidden)
    client = make_openai(config, credential)
    assert client.max_retries == 0
    client.close()
    credential.close()


def test_ambient_static_credentials_cannot_override_federated_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = make_credential(target(tmp_path), {})
    monkeypatch.setenv("AZURE_OPENAI_AD_TOKEN", "synthetic-static-token")
    with pytest.raises(IngestionError, match="ambient"):
        make_openai(target(tmp_path), credential)
    credential.close()


def test_token_batches_and_input_limit_are_enforced_before_requests() -> None:
    sizes: list[int] = []
    token_counts: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        values = json.loads(request.content)["input"]
        sizes.append(len(values))
        token_counts.append(sum(embedding.encoder.count(value) for value in values))
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-large",
                "data": [
                    {"object": "embedding", "index": i, "embedding": [0.1] * 3072}
                    for i in range(len(values))
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    embedding = adapter(httpx.MockTransport(handle))
    with pytest.raises(IngestionError, match="8000"):
        embedding.embed(["synthetic " * 9000])
    assert not sizes
    embedding.embed(["synthetic " * 3000] * 6)
    assert len(sizes) >= 2 and sum(sizes) == 6 and max(sizes) <= 16
    assert max(token_counts) <= 16000
    embedding.close()


def test_permanent_quota_is_not_blindly_retried() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "synthetic capacity failure", "code": "insufficient_quota"}},
        )

    embedding = adapter(httpx.MockTransport(handle))
    with pytest.raises(IngestionError, match="permanently"):
        embedding.embed(["synthetic"])
    assert embedding.calls == 1 and embedding.retry.retries == 0
    embedding.close()
