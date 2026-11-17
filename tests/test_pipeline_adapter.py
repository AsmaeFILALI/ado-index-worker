import json
import shutil
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import pytest
from conftest import CONTRACT, ROOT, GitRepo
from test_publishing import toml
from test_sync import Harness, harness

from ado_markdown_ingestion import pipeline
from ado_markdown_ingestion.config import load_settings
from ado_markdown_ingestion.embeddings import Embeddings
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import Prepared, PublishSettings
from ado_markdown_ingestion.search import Search
from ado_markdown_ingestion.state import BlobState
from ado_markdown_ingestion.tokenizer import Encoder


@dataclass
class Case:
    source: GitRepo
    worker: GitRepo
    env: dict[str, str]
    services: Harness


@pytest.fixture
def case(repo: GitRepo, tmp_path: Path) -> Iterator[Case]:
    repo.write("guide.md", "# Synthetic\nNo live pipeline calls.\n")
    repo.commit()
    services = harness(repo, tmp_path)
    worker = GitRepo(tmp_path / "pinned worker")
    worker.path.mkdir()
    worker.git("init")
    worker.git("symbolic-ref", "HEAD", "refs/heads/worker")
    worker.git("config", "user.name", "Synthetic Test")
    worker.git("config", "user.email", "synthetic@example.invalid")
    (worker.path / "config").mkdir()
    contract_path = worker.path / "docs" / CONTRACT.name
    contract_path.parent.mkdir(parents=True)
    shutil.copyfile(CONTRACT, contract_path)
    toml(worker.path / "config" / "source.toml", asdict(services.prepared.source))
    target = replace(
        services.prepared.target,
        auth_mode="pipeline",
        client_id="00000000-0000-0000-0000-000000000002",
        service_connection_id="00000000-0000-0000-0000-000000000003",
    )
    toml(worker.path / "config" / "publishing.toml", asdict(target))
    worker_commit = worker.commit()
    cache = worker.path / ".cache" / "tiktoken"
    cache.mkdir(parents=True)
    shutil.copyfile(
        ROOT / ".cache" / "tiktoken" / "cl100k_base.tiktoken", cache / "cl100k_base.tiktoken"
    )
    temp = tmp_path / "runs"
    temp.mkdir()
    env = {
        "INGEST_WORKER_ROOT": str(worker.path),
        "INGEST_SOURCE_ROOT": str(repo.path),
        "INGEST_WORKER_COMMIT": worker_commit,
        "INGEST_RESOLVED_WORKER_COMMIT": worker_commit,
        "INGEST_SOURCE_CONFIG": "config/source.toml",
        "INGEST_APPROVED_REF": services.prepared.source.source_ref,
        "INGEST_TEMP_ROOT": str(temp),
        "BUILD_REASON": "Manual",
        "BUILD_SOURCEBRANCH": services.prepared.source.source_ref,
        "BUILD_SOURCEVERSION": services.prepared.source.commit,
        "BUILD_REPOSITORY_ID": services.prepared.source.repo_id,
        "INGEST_ALLOW_PUBLISH": "true",
        "INGEST_PUBLISHING_CONFIG": "config/publishing.toml",
        "INGEST_SCHEMA_PATH": str(target.platform_schema),
        "INGEST_ADMISSION_PATH": str(target.admission),
        "INGEST_ADMISSION_SHA256": target.admission_sha256,
        "INGEST_SERVICE_CONNECTION_ID": target.service_connection_id,
        "tenantId": target.tenant_id,
        "servicePrincipalId": target.client_id,
        "idToken": "synthetic-oidc-token-not-a-real-credential",
        "SYSTEM_ACCESSTOKEN": "synthetic-job-token-not-a-real-credential",
    }
    yield Case(repo, worker, env, services)
    services.close()


def test_real_offline_pipeline_plan_is_portable_and_cleans_temp(
    case: Case,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert pipeline.run("plan", "sync", case.env) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["counts"]["files"] == output["counts"]["documents"] == 1
    assert output["counts"]["azure_calls"] == 0
    assert list(Path(case.env["INGEST_TEMP_ROOT"]).iterdir()) == []
    assert not case.services.backend.events


def test_detached_agent_checkout_gets_only_a_local_approved_ref(case: Case) -> None:
    case.source.git("checkout", "--detach")
    case.source.git("update-ref", "-d", case.env["INGEST_APPROVED_REF"])
    assert pipeline.run("plan", "sync", case.env) == 0
    assert case.source.git("show-ref", "--verify", case.env["INGEST_APPROVED_REF"]).returncode == 0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("BUILD_REASON", "PullRequest"),
        ("BUILD_SOURCEBRANCH", "refs/heads/unapproved"),
        ("BUILD_REPOSITORY_ID", "foreign-repository"),
        ("BUILD_SOURCEVERSION", "0" * 40),
        ("INGEST_WORKER_COMMIT", "refs/heads/main"),
        ("INGEST_RESOLVED_WORKER_COMMIT", "f" * 40),
        ("INGEST_ALLOW_PUBLISH", "false"),
        ("INGEST_SOURCE_CONFIG", "../outside.toml"),
        ("ADO_INGEST_COMMIT", "f" * 40),
    ],
)
def test_untrusted_or_mismatched_pipeline_context_fails_before_services(
    case: Case,
    name: str,
    value: str,
) -> None:
    env = {**case.env, name: value}
    with pytest.raises(IngestionError):
        pipeline.run("publish", "sync", env)
    assert not case.services.backend.events
    assert not case.services.backend.docs
    assert list(Path(case.env["INGEST_TEMP_ROOT"]).iterdir()) == []


@pytest.mark.parametrize("reason", ["IndividualCI", "BatchedCI", "Schedule"])
def test_rebuild_is_manual_only(case: Case, reason: str) -> None:
    with pytest.raises(IngestionError, match="manual"):
        pipeline.run("publish", "rebuild", {**case.env, "BUILD_REASON": reason})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SYSTEM_ACCESSTOKEN", ""),
        ("idToken", ""),
        ("servicePrincipalKey", "synthetic-forbidden-secret"),
        ("tenantId", "00000000-0000-0000-0000-000000000009"),
        ("servicePrincipalId", "00000000-0000-0000-0000-000000000009"),
        ("INGEST_SERVICE_CONNECTION_ID", "00000000-0000-0000-0000-000000000009"),
        ("INGEST_ADMISSION_SHA256", "0" * 64),
    ],
)
def test_federated_identity_and_external_admission_are_not_inferred(
    case: Case,
    name: str,
    value: str,
) -> None:
    with pytest.raises(IngestionError):
        pipeline.run("publish", "sync", {**case.env, name: value})
    assert not case.services.backend.events
    assert list(Path(case.env["INGEST_TEMP_ROOT"]).iterdir()) == []


def test_scheduled_pipeline_uses_real_mocked_reconciliation_without_token_files(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    @contextmanager
    def fake_clients(
        prepared: Prepared,
        encoder: Encoder,
    ) -> Iterator[tuple[Search, BlobState, Embeddings]]:
        assert prepared.operation == "reconcile"
        assert prepared.target.auth_mode == "pipeline"
        for path in Path(case.env["INGEST_TEMP_ROOT"]).rglob("*.toml"):
            text = path.read_text(encoding="utf-8")
            assert case.env["SYSTEM_ACCESSTOKEN"] not in text
            assert case.env["idToken"] not in text
            if path.name == "source.toml":
                assert load_settings(path, {}, {}).commit == case.env["BUILD_SOURCEVERSION"]
            else:
                assert (
                    PublishSettings.load(path).admission_sha256
                    == case.env["INGEST_ADMISSION_SHA256"]
                )
        yield case.services.search, case.services.state, case.services.model

    monkeypatch.setattr("ado_markdown_ingestion.clients.clients", fake_clients)
    assert pipeline.run("publish", "sync", {**case.env, "BUILD_REASON": "Schedule"}) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["operation"] == "reconcile"
    assert result["counts"]["documents"] == 1
    assert list(Path(case.env["INGEST_TEMP_ROOT"]).iterdir()) == []


def test_reviewed_in_repo_admission_replaces_external_pin(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = case.services.prepared.target
    shutil.copyfile(target.admission, case.worker.path / "config" / "admission.json")
    data = asdict(replace(target, auth_mode="pipeline", client_id=case.env["servicePrincipalId"]))
    data["service_connection_id"] = case.env["INGEST_SERVICE_CONNECTION_ID"]
    data["admission"] = "config/admission.json"
    toml(case.worker.path / "config" / "in-repo.toml", data)
    case.worker.git("add", "-A")
    commit = case.worker.commit()
    env = {
        name: value
        for name, value in case.env.items()
        if name not in ("INGEST_ADMISSION_PATH", "INGEST_ADMISSION_SHA256")
    }
    env.update(
        INGEST_PUBLISHING_CONFIG="config/in-repo.toml",
        INGEST_WORKER_COMMIT=commit,
        INGEST_RESOLVED_WORKER_COMMIT=commit,
    )

    @contextmanager
    def fake_clients(
        prepared: Prepared,
        encoder: Encoder,
    ) -> Iterator[tuple[Search, BlobState, Embeddings]]:
        assert prepared.target.admission_sha256 == target.admission_sha256
        yield case.services.search, case.services.state, case.services.model

    monkeypatch.setattr("ado_markdown_ingestion.clients.clients", fake_clients)
    assert pipeline.run("publish", "sync", env) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["documents"] == 1
    with pytest.raises(IngestionError, match="together"):
        pipeline.run("publish", "sync", {**env, "INGEST_ADMISSION_SHA256": target.admission_sha256})


def test_poc_waiver_publishes_new_commits_without_any_admission_file(
    case: Case,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = case.services.prepared.target
    data = asdict(replace(target, auth_mode="pipeline", client_id=case.env["servicePrincipalId"]))
    data["service_connection_id"] = case.env["INGEST_SERVICE_CONNECTION_ID"]
    for name in ("admission", "admission_sha256"):
        del data[name]
    data["admission_waiver"] = "POC owner decision: merge to main is sufficient; synthetic only"
    shutil.copyfile(target.platform_schema, case.worker.path / "config" / "schema.json")
    data["platform_schema"] = "config/schema.json"
    toml(case.worker.path / "config" / "waiver.toml", data)
    case.worker.git("add", "-A")
    commit = case.worker.commit()
    env = {
        name: value
        for name, value in case.env.items()
        if name not in ("INGEST_ADMISSION_PATH", "INGEST_ADMISSION_SHA256", "INGEST_SCHEMA_PATH")
    }
    env.update(
        INGEST_PUBLISHING_CONFIG="config/waiver.toml",
        INGEST_WORKER_COMMIT=commit,
        INGEST_RESOLVED_WORKER_COMMIT=commit,
    )
    case.source.write("guide.md", "# Synthetic\nA new merged commit with no admission file.\n")
    newer = case.source.commit()
    env["BUILD_SOURCEVERSION"] = newer

    @contextmanager
    def fake_clients(
        prepared: Prepared,
        encoder: Encoder,
    ) -> Iterator[tuple[Search, BlobState, Embeddings]]:
        assert prepared.target.admission_waiver
        assert prepared.target.platform_schema == case.worker.path / "config" / "schema.json"
        assert prepared.policy["target_role"] == "non-serving"
        yield case.services.search, case.services.state, case.services.model

    monkeypatch.setattr("ado_markdown_ingestion.clients.clients", fake_clients)
    assert pipeline.run("publish", "sync", env) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["documents"] == 1
    with pytest.raises(IngestionError, match="waiver"):
        pipeline.run(
            "publish",
            "sync",
            {**env, "INGEST_ADMISSION_PATH": "x", "INGEST_ADMISSION_SHA256": "0" * 64},
        )


@pytest.mark.parametrize("waiver", ["<REVIEW_REFERENCE>", "short", 7])
def test_waiver_must_be_a_recorded_non_placeholder_decision(
    case: Case, tmp_path: Path, waiver: object
) -> None:
    target = case.services.prepared.target
    data = asdict(target)
    for name in ("admission", "admission_sha256"):
        del data[name]
    data["admission_waiver"] = waiver
    path = tmp_path / "bad-waiver.toml"
    toml(path, data)
    with pytest.raises(IngestionError):
        PublishSettings.load(path)
    data["admission_waiver"] = "POC owner decision: merge to main is sufficient"
    data["admission"] = "admission.json"
    toml(path, data)
    with pytest.raises(IngestionError, match="waiver"):
        PublishSettings.load(path)


def test_worker_pin_mismatch_reports_which_commit_differs(case: Case) -> None:
    wrong = "f" * 40
    with pytest.raises(
        IngestionError, match=r"resource_version='f{40}'.*checkout_head=[a-f0-9]{40}"
    ):
        pipeline.run("plan", "sync", {**case.env, "INGEST_RESOLVED_WORKER_COMMIT": wrong})
    with pytest.raises(IngestionError, match=r"resource_version=None"):
        env = {k: v for k, v in case.env.items() if k != "INGEST_RESOLVED_WORKER_COMMIT"}
        pipeline.run("plan", "sync", env)


def test_unpinned_worker_tracks_the_checked_out_ref(case: Case) -> None:
    env = {k: v for k, v in case.env.items() if k != "INGEST_RESOLVED_WORKER_COMMIT"}
    assert pipeline.run("plan", "sync", {**env, "INGEST_WORKER_COMMIT": ""}) == 0
    env.pop("INGEST_WORKER_COMMIT")
    assert pipeline.run("plan", "sync", env) == 0
    with pytest.raises(IngestionError, match="40-character"):
        pipeline.run("plan", "sync", {**env, "INGEST_WORKER_COMMIT": "main"})


def test_stale_local_ref_in_a_persistent_workspace_is_advanced_to_the_build_commit(
    case: Case,
) -> None:
    ref = case.env["INGEST_APPROVED_REF"]
    old = case.source.git("rev-parse", "HEAD").stdout.decode().strip()
    case.source.write("guide.md", "# Synthetic\nA second commit after the first run.\n")
    newer = case.source.commit()
    case.source.git("checkout", "--detach")
    case.source.git("update-ref", ref, old)
    env = {**case.env, "BUILD_SOURCEVERSION": newer}
    assert pipeline.run("plan", "sync", env) == 0
    assert case.source.git("rev-parse", ref).stdout.decode().strip() == newer


def test_new_source_commit_cannot_reuse_old_admission(case: Case) -> None:
    case.source.write("guide.md", "# Synthetic\nA newly reviewed candidate, not yet admitted.\n")
    newer = case.source.commit()
    with pytest.raises(IngestionError, match="snapshot mismatch"):
        pipeline.run("publish", "sync", {**case.env, "BUILD_SOURCEVERSION": newer})
    assert not case.services.backend.events
    assert not case.services.backend.docs
    assert list(Path(case.env["INGEST_TEMP_ROOT"]).iterdir()) == []


def test_checkouts_cannot_be_nested_and_temp_cannot_target_source(case: Case) -> None:
    with pytest.raises(IngestionError, match="non-nested"):
        pipeline.run("plan", "sync", {**case.env, "INGEST_SOURCE_ROOT": str(case.worker.path)})
    with pytest.raises(IngestionError, match="outside both"):
        pipeline.run(
            "plan",
            "sync",
            {
                **case.env,
                "INGEST_TEMP_ROOT": str(case.source.path),
            },
        )
    assert not case.services.backend.events


def test_temporary_toml_preserves_unicode_checkout_paths(tmp_path: Path) -> None:
    data: dict[str, object] = {
        "source_root": "C:\\source \u00e9 \U0001f600",
        "include": ["documentation \u00e9/**/*.md"],
    }
    path = tmp_path / "unicode.toml"
    pipeline._write_toml(path, data)
    with path.open("rb") as stream:
        assert tomllib.load(stream) == data
