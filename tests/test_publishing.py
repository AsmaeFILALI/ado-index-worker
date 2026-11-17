import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from conftest import GitRepo
from test_sync import harness

from ado_markdown_ingestion.cli import main
from ado_markdown_ingestion.embeddings import Embeddings
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import Prepared, PublishSettings
from ado_markdown_ingestion.search import Search
from ado_markdown_ingestion.state import BlobState
from ado_markdown_ingestion.sync import publish
from ado_markdown_ingestion.tokenizer import Encoder


def toml(path: Path, data: dict[str, object]) -> None:
    path.write_text(
        "\n".join(
            name + " = " + json.dumps(str(value) if isinstance(value, Path) else value)
            for name, value in data.items()
        ),
        encoding="utf-8",
    )


def test_real_cli_with_mocked_sdk_boundaries_and_safe_logs(
    repo: GitRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo.write("guide.md", "# Synthetic\nSYNTHETIC_DO_NOT_LOG\n")
    repo.commit()
    item = harness(repo, tmp_path)
    source_config, target_config = tmp_path / "source.toml", tmp_path / "publishing.toml"
    toml(source_config, asdict(item.prepared.source))
    toml(target_config, asdict(item.prepared.target))
    loaded = PublishSettings.load(target_config)
    assert loaded == item.prepared.target
    caplog.set_level(logging.DEBUG)

    @contextmanager
    def fake_clients(
        prepared: Prepared,
        encoder: Encoder,
    ) -> Iterator[tuple[Search, BlobState, Embeddings]]:
        assert any('"event": "publishing_target"' in record.message for record in caplog.records)
        yield item.search, item.state, item.model

    monkeypatch.setattr("ado_markdown_ingestion.clients.clients", fake_clients)
    assert (
        main(
            [
                "sync",
                "--config",
                str(source_config),
                "--publishing-config",
                str(target_config),
                "--authorize-publish",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["checkpoint"] == "advanced-after-verification"
    assert result["counts"]["documents"] == 1
    assert "SYNTHETIC_DO_NOT_LOG" not in captured.err + captured.out + caplog.text
    item.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("auth_mode", "default"),
        ("search_endpoint", "http://synthetic.search.windows.net"),
        ("state_account_url", "https://synthetic.blob.core.windows.net?token=forbidden"),
        ("tenant_id", "<UNKNOWN_TENANT>"),
        ("client_id", "<UNKNOWN_CLIENT>"),
        ("service_connection_id", "<UNKNOWN_CONNECTION>"),
        ("schema_sha256", "invalid"),
        ("inventory_limit", 10001),
        ("search_schema_api_version", "not-an-api-version"),
        ("search_schema_api_version", 20251101),
        ("unknown", "not ignored"),
    ],
)
def test_publishing_configuration_fails_before_client_construction(
    repo: GitRepo,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    item = harness(repo, tmp_path)
    data: dict[str, object] = asdict(item.prepared.target)
    data[field] = value
    path = tmp_path / "invalid.toml"
    toml(path, data)
    with pytest.raises(IngestionError):
        PublishSettings.load(path)
    assert not item.backend.events
    item.close()


def test_authorized_flag_alone_never_creates_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Invalid source/admission must not construct SDK clients")

    monkeypatch.setattr("ado_markdown_ingestion.clients.clients", forbidden)
    assert main(["sync", "--authorize-publish"]) == 1


def test_same_physical_endpoint_cannot_create_another_lock_namespace(
    repo: GitRepo,
    tmp_path: Path,
) -> None:
    item = harness(repo, tmp_path)
    data: dict[str, object] = asdict(item.prepared.target)
    data["search_endpoint"] = "https://SYNTHETIC.search.windows.net:443/"
    path = tmp_path / "canonical.toml"
    toml(path, data)
    canonical = PublishSettings.load(path)
    assert canonical.physical_id() == item.prepared.target.physical_id()
    data["search_endpoint"] = "https://synthetic.privatelink.search.windows.net"
    toml(path, data)
    with pytest.raises(IngestionError, match="HTTPS"):
        PublishSettings.load(path)
    item.close()


@pytest.mark.parametrize("schema_api_version", ["2025-09-01", "2025-11-01-preview"])
def test_production_composition_uses_only_injected_sdk_transports(
    repo: GitRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_api_version: str,
) -> None:
    from azure.search.documents import SearchClient
    from azure.search.documents.indexes import SearchIndexClient
    from azure.storage.blob import ContainerClient
    from azure_fakes import Credential, Transport

    import ado_markdown_ingestion.clients as composition

    repo.write("guide.md", "# Synthetic\nFactory integration\n")
    repo.commit()
    item = harness(repo, tmp_path)
    item.prepared = replace(
        item.prepared,
        target=replace(item.prepared.target, search_schema_api_version=schema_api_version),
    )
    credential = Credential()

    def search_client(
        endpoint: str, index_name: str, identity: Credential, **kwargs: object
    ) -> SearchClient:
        assert "api_version" not in kwargs  # Schema read version must not change document writes.
        kwargs["transport"] = Transport(item.backend)
        return SearchClient(endpoint, index_name, identity, **kwargs)

    def index_client(endpoint: str, identity: Credential, **kwargs: object) -> SearchIndexClient:
        assert kwargs["api_version"] == schema_api_version
        kwargs["transport"] = Transport(item.backend)
        return SearchIndexClient(endpoint, identity, **kwargs)

    def container_client(
        account_url: str,
        container_name: str,
        credential: Credential,
        **kwargs: object,
    ) -> ContainerClient:
        assert kwargs["retry_total"] == 0 and kwargs["logging_enable"] is False
        return ContainerClient(
            account_url,
            container_name,
            credential,
            transport=Transport(item.backend),
            retry_total=0,
            connection_timeout=5,
            read_timeout=30,
            logging_enable=False,
        )

    monkeypatch.setattr(composition, "SearchClient", search_client)
    monkeypatch.setattr(composition, "SearchIndexClient", index_client)
    monkeypatch.setattr(composition, "ContainerClient", container_client)
    monkeypatch.setattr(composition, "make_credential", lambda _: credential)
    monkeypatch.setattr(composition, "make_openai", lambda target, identity: item.model.client)
    with composition.clients(item.prepared, item.encoder) as (search, state, model):
        assert (
            publish(item.prepared, search, state, model, item.encoder)["counts"]["documents"] == 1
        )
        assert state._thread is None
    item.close()


def test_schema_read_api_version_is_optional_and_validated(repo: GitRepo, tmp_path: Path) -> None:
    item = harness(repo, tmp_path)
    data: dict[str, object] = asdict(item.prepared.target)
    data.pop("search_schema_api_version", None)
    path = tmp_path / "schema-version.toml"
    toml(path, data)
    assert PublishSettings.load(path).search_schema_api_version == "2025-09-01"
    data["search_schema_api_version"] = "2025-11-01-preview"
    toml(path, data)
    assert PublishSettings.load(path).search_schema_api_version == "2025-11-01-preview"
    item.close()
