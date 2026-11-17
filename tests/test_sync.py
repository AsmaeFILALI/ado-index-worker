import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import ContainerClient
from azure_fakes import Credential, Transport
from conftest import ROOT, GitRepo
from fakes import Azure, Clock
from openai import AzureOpenAI
from test_contract import synthetic_schema
from test_embeddings import target

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.contract import vector_checksum
from ado_markdown_ingestion.embeddings import Embeddings
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.publishing import Prepared, prepare
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.search import Search
from ado_markdown_ingestion.state import BlobState
from ado_markdown_ingestion.sync import PublicationResult, create_plan, publish
from ado_markdown_ingestion.tokenizer import Encoder, load_encoder
from ado_markdown_ingestion.util import digest


@dataclass
class Harness:
    prepared: Prepared
    search: Search
    state: BlobState
    model: Embeddings
    encoder: Encoder
    backend: Azure
    clock: Clock

    def run(self) -> PublicationResult:
        return publish(self.prepared, self.search, self.state, self.model, self.encoder)

    def close(self) -> None:
        self.search.close()
        self.state.close()
        self.model.close()


ABSENT = object()


def harness(
    repo: GitRepo,
    tmp_path: Path,
    backend: Azure | None = None,
    *,
    source: Settings | None = None,
    operation: str = "sync",
    index: str = "synthetic-index",
    role: str = "serving",
    admission_updates: Mapping[str, object] | None = None,
    model_revision: str | None = None,
) -> Harness:
    backend = backend or Azure()
    source = source or repo.settings()
    config = replace(target(tmp_path), index_name=index, visibility_seconds=5)
    schema = synthetic_schema()
    schema["name"] = index
    vectors = schema["vectorSearch"]
    assert isinstance(vectors, dict)
    vectors["vectorizers"][0]["azureOpenAIParameters"]["resourceUri"] = config.embedding_endpoint
    config.platform_schema.write_text(json.dumps(schema), encoding="utf-8")
    admission: dict[str, object] = {
        "policy_version": 1,
        "target": config.target(),
        "repo_id": source.repo_id,
        "source_ref": source.source_ref,
        "approved_commit": source.commit,
        "approval_reference": "synthetic-test-review",
        "approved_audience_reference": "synthetic-test-only",
        "access_mode": "uniform-approved-corpus",
        "target_role": role,
        "allow_bootstrap": True,
        "selection_fingerprint": source.selection_fingerprint(),
        **(admission_updates or {}),
    }
    admission = {name: value for name, value in admission.items() if value is not ABSENT}
    config.admission.write_text(json.dumps(admission), encoding="utf-8")
    config = replace(
        config,
        schema_sha256=digest(config.platform_schema.read_bytes()),
        admission_sha256=digest(config.admission.read_bytes()),
    )
    prepared = prepare(source, config, operation)
    clock = Clock()

    def retry() -> Retry:
        return Retry(sleep=clock.sleep, clock=clock, jitter=lambda: 0)

    transport = Transport(backend)
    search = Search(
        SearchClient(
            config.search_endpoint, index, Credential(), transport=transport, retry_total=0
        ),
        SearchIndexClient(config.search_endpoint, Credential(), transport=transport, retry_total=0),
        index,
        source.repo_id,
        source.source_ref,
        retry=retry(),
        service_endpoint=config.search_endpoint,
    )
    state = BlobState(
        ContainerClient(
            config.state_account_url,
            config.state_container,
            Credential(),
            transport=transport,
            retry_total=0,
        ),
        config.physical_id(),
        prepared.compatibility,
        retry=retry(),
        clock=clock,
        start_renew_thread=False,
    )

    def embedding(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-large",
                "data": [
                    {"object": "embedding", "index": number, "embedding": [0.1] * 3072}
                    for number in range(len(data["input"]))
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
                **({"model_version": model_revision} if model_revision is not None else {}),
            },
        )

    encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    model = Embeddings(
        AzureOpenAI(
            azure_endpoint=config.embedding_endpoint,
            api_version=config.openai_api_version,
            azure_ad_token_provider=lambda: "synthetic-offline-token",
            http_client=httpx.Client(transport=httpx.MockTransport(embedding)),
            max_retries=0,
        ),
        encoder,
        source.deployment,
        retry=retry(),
    )
    return Harness(prepared, search, state, model, encoder, backend, clock)


def test_bootstrap_no_change_edit_rename_and_delete(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("guide.md", "# Synthetic\n## Reading\nA fictional sample.\n")
    first_commit = repo.commit()
    first = harness(repo, tmp_path)
    result = first.run()
    assert result["bootstrap"] is True
    docs = first.backend.docs["synthetic-index"]
    key = next(iter(docs))
    original = dict(docs[key])
    assert first.backend.events.index("journal") < first.backend.events.index("upload")
    again = harness(repo, tmp_path, first.backend)
    second = again.run()
    assert second["counts"]["embedding_calls"] == second["counts"]["upserts"] == 0
    repo.commit("new snapshot unchanged source")
    unchanged = harness(repo, tmp_path, first.backend)
    unchanged.run()
    assert docs[key]["source_version_id"] == first_commit
    assert docs[key]["ingest_run_id"] == original["ingest_run_id"]

    repo.write("guide.md", "# Synthetic\n\n## Reading\nA fictional sample.\n")
    repo.commit()
    metadata_only = harness(repo, tmp_path, first.backend)
    updated = metadata_only.run()
    assert updated["counts"]["embedding_calls"] == 0
    assert updated["counts"]["reused_vectors"] == 1
    assert docs[key]["start_line"] != original["start_line"]
    repo.git("mv", "guide.md", "renamed é.md")
    repo.commit()
    renamed = harness(repo, tmp_path, first.backend)
    renamed.run()
    assert key not in docs and len(docs) == 1
    repo.git("rm", "renamed é.md")
    repo.commit()
    deleted = harness(repo, tmp_path, first.backend, operation="reconcile")
    deleted.run()
    assert not docs
    for item in (first, again, unchanged, metadata_only, renamed, deleted):
        item.close()


def test_reconcile_repairs_drift_and_never_touches_another_source(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("guide.md", "# Synthetic\nBody\n")
    repo.commit()
    first = harness(repo, tmp_path)
    first.run()
    foreign = harness(
        repo, tmp_path, first.backend, source=replace(repo.settings(), repo_id="other-repo")
    )
    foreign.run()
    docs = first.backend.docs["synthetic-index"]
    own = next(key for key, doc in docs.items() if doc["repo_id"] == "synthetic-repository")
    other = next(key for key in docs if key != own)
    docs[own]["content"] = "corrupted synthetic data"
    repair = harness(repo, tmp_path, first.backend, operation="reconcile")
    repair.run()
    assert docs[own]["content"] != "corrupted synthetic data"
    repo.git("rm", "guide.md")
    repo.commit()
    empty = harness(repo, tmp_path, first.backend, operation="reconcile")
    empty.run()
    assert set(docs) == {other}


def test_partial_upsert_and_failed_checkpoint_recover_idempotently(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("guide.md", "# Synthetic\n## One\nFirst\n## Two\nSecond\n")
    repo.commit()
    first = harness(repo, tmp_path)
    first.backend.fail_checkpoint = True
    with pytest.raises(IngestionError):
        first.run()
    assert len(first.backend.docs["synthetic-index"]) == 2
    first.backend.fail_checkpoint = False
    recovery = harness(repo, tmp_path, first.backend)
    result = recovery.run()
    assert result["counts"]["embedding_calls"] == 0
    assert len(first.backend.docs["synthetic-index"]) == 2
    with recovery.state.owned():
        assert recovery.state.load(recovery.prepared.scope, "pending") is None
        assert recovery.state.load(recovery.prepared.scope, "checkpoint") is not None


def test_superseded_build_and_fingerprint_changes_are_rejected(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Old\nBody\n")
    old = repo.commit()
    repo.write("a.md", "# New\nBody\n")
    repo.commit()
    current = harness(repo, tmp_path)
    current.run()
    stale = harness(repo, tmp_path, current.backend, source=repo.settings(old))
    with pytest.raises(IngestionError, match="superseded"):
        stale.run()
    changed = harness(
        repo, tmp_path, current.backend, source=replace(repo.settings(), target_tokens=100)
    )
    with pytest.raises(IngestionError, match="fingerprint"):
        changed.run()


def test_nonserving_rebuild_and_explicit_bootstrap_admission(repo: GitRepo, tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="non-serving"):
        harness(repo, tmp_path, operation="rebuild")
    bootstrap = harness(repo, tmp_path, admission_updates={"allow_bootstrap": False})
    with pytest.raises(IngestionError, match="bootstrap"):
        bootstrap.run()
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    rebuilt = harness(
        repo, tmp_path, operation="rebuild", index="synthetic-generation", role="non-serving"
    )
    assert rebuilt.run()["operation"] == "rebuild"
    assert len(rebuilt.backend.docs["synthetic-generation"]) == 1


def test_global_model_revision_change_fails_before_publication(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Synthetic\nFirst\n")
    repo.commit()
    first = harness(repo, tmp_path, model_revision="synthetic-v1")
    first.run()
    other = harness(
        repo,
        tmp_path,
        first.backend,
        source=replace(repo.settings(), repo_id="other-repo"),
        model_revision="synthetic-v2",
    )
    with pytest.raises(IngestionError, match="revision"):
        other.run()
    assert all(
        doc["repo_id"] == "synthetic-repository"
        for doc in first.backend.docs["synthetic-index"].values()
    )


def test_authoritative_empty_source_is_different_from_failed_source(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    first = harness(repo, tmp_path)
    first.run()
    with pytest.raises(IngestionError):
        harness(repo, tmp_path, first.backend, source=repo.settings("f" * 40))
    assert len(first.backend.docs["synthetic-index"]) == 1


def test_partial_results_cleanup_survives_another_source_commit(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Synthetic\n## One\nFirst\n## Two\nSecond\n")
    repo.commit()
    failed = harness(repo, tmp_path)
    preview = create_plan(repo.settings(), failed.encoder, include_documents=True)
    drafts = preview["documents"]
    assert isinstance(drafts, list)
    failed.backend.permanent_key = drafts[1]["id"]
    with pytest.raises(IngestionError, match="permanently"):
        failed.run()
    assert len(failed.backend.docs["synthetic-index"]) == 1
    with failed.state.owned():
        assert failed.state.load(failed.prepared.scope, "checkpoint") is None
        assert failed.state.load(failed.prepared.scope, "pending") is not None
    failed.backend.permanent_key = None
    repo.write("a.md", "# Synthetic\n## Replacement\nThird\n")
    repo.commit()
    replay = harness(repo, tmp_path, failed.backend)
    replay.run()
    assert len(failed.backend.docs["synthetic-index"]) == 1
    for doc in failed.backend.docs["synthetic-index"].values():
        heading = doc["heading_path"]
        assert isinstance(heading, str) and heading.endswith("Replacement")


def test_journal_failure_prevents_all_search_writes(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    failed = harness(repo, tmp_path)
    failed.backend.fail_journal = True
    with pytest.raises(IngestionError):
        failed.run()
    assert not failed.backend.docs["synthetic-index"]
    assert "upload" not in failed.backend.events and "delete" not in failed.backend.events


def test_delete_failure_retains_checkpoint_and_recovers(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("a.md", "# Synthetic\nOld\n")
    repo.write("b.md", "# Synthetic\nKeep\n")
    initial = repo.commit()
    first = harness(repo, tmp_path)
    first.run()
    repo.git("rm", "a.md")
    repo.write("b.md", "# Synthetic\nUpdated\n")
    latest = repo.commit()
    failure = harness(repo, tmp_path, first.backend)
    first.backend.fail_delete = True
    with pytest.raises(IngestionError):
        failure.run()
    with failure.state.owned():
        previous = failure.state.load(failure.prepared.scope, "checkpoint")
        assert previous is not None and previous["source_version_id"] == initial
    first.backend.fail_delete = False
    recovery = harness(repo, tmp_path, first.backend)
    assert recovery.run()["counts"]["embedding_calls"] == 0
    with recovery.state.owned():
        current = recovery.state.load(recovery.prepared.scope, "checkpoint")
        assert current is not None and current["source_version_id"] == latest
    assert len(first.backend.docs["synthetic-index"]) == 1


def test_visibility_delay_and_timeout_do_not_advance_early(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    delayed = harness(repo, tmp_path)
    delayed.backend.visibility_lag = 2
    delayed.run()
    assert delayed.clock.value >= 4
    isolated = harness(repo, tmp_path)
    isolated.backend.visibility_lag = 100
    with pytest.raises(IngestionError, match="visibility"):
        isolated.run()
    with isolated.state.owned():
        assert isolated.state.load(isolated.prepared.scope, "checkpoint") is None
        assert isolated.state.load(isolated.prepared.scope, "pending") is not None
    isolated.backend.visibility_lag = 0
    recovery = harness(repo, tmp_path, isolated.backend)
    assert recovery.run()["counts"]["embedding_calls"] == 0


def test_lease_loss_after_upserts_leaves_recoverable_intent(
    repo: GitRepo,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    failed = harness(repo, tmp_path)
    write = failed.search.write

    def losing(documents: list[dict[str, object]]) -> None:
        write(documents)
        failed.backend.leases.clear()
        failed.state.renew_once()

    monkeypatch.setattr(failed.search, "write", losing)
    with pytest.raises(IngestionError):
        failed.run()
    assert len(failed.backend.docs["synthetic-index"]) == 1
    recovery = harness(repo, tmp_path, failed.backend)
    assert recovery.run()["counts"]["embedding_calls"] == 0


def test_existing_index_without_checkpoint_is_not_assumed_empty(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    first = harness(repo, tmp_path)
    first.backend.docs["synthetic-index"] = {
        "legacy": {
            "id": "legacy",
            "source": "azure_devops",
            "repo_id": "synthetic-repository",
            "source_ref": "refs/heads/approved",
        }
    }
    with pytest.raises(IngestionError, match="existing source"):
        first.run()
    admitted = harness(
        repo, tmp_path, first.backend, admission_updates={"allow_bootstrap_existing": True}
    )
    admitted.run()
    assert "legacy" not in first.backend.docs["synthetic-index"]
    assert len(first.backend.docs["synthetic-index"]) == 1


def test_reviewed_divergent_reconciliation_and_removed_optional_metadata(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("a.md", "# Synthetic\nBefore\n")
    base = repo.commit()
    repo.write("a.md", "# Synthetic\nPublished\n")
    published = repo.commit()
    first = harness(repo, tmp_path)
    first.run()
    key = next(iter(first.backend.docs["synthetic-index"]))
    first.backend.docs["synthetic-index"][key]["category"] = "synthetic removed metadata"
    repo.git("checkout", "-b", "replacement", base)
    repo.write("a.md", "# Synthetic\nAfter reset\n")
    replacement = repo.commit()
    repo.git("branch", "-f", "approved", replacement)
    denied = harness(repo, tmp_path, first.backend)
    with pytest.raises(IngestionError, match="divergent"):
        denied.run()
    accepted = harness(
        repo,
        tmp_path,
        first.backend,
        operation="reconcile",
        admission_updates={"allow_history_reset": True},
    )
    accepted.run()
    assert "category" not in first.backend.docs["synthetic-index"][key]
    with accepted.state.owned():
        checkpoint = accepted.state.load(accepted.prepared.scope, "checkpoint")
        assert checkpoint is not None and checkpoint["previous_successful_commit"] == published


def test_reconciliation_repairs_missing_or_corrupt_vectors(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("a.md", "# Synthetic\nBody\n")
    repo.commit()
    initial = harness(repo, tmp_path)
    initial.run()
    key = next(iter(initial.backend.docs["synthetic-index"]))
    initial.backend.docs["synthetic-index"][key]["contentVector"] = [0.2] * 3072
    repair = harness(repo, tmp_path, initial.backend, operation="reconcile")
    assert repair.run()["counts"]["embedding_calls"] == 1
    assert vector_checksum(
        initial.backend.docs["synthetic-index"][key]["contentVector"]
    ) == vector_checksum([0.1] * 3072)


def test_newer_pending_run_rejects_an_older_queued_target(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("a.md", "# Synthetic\nOld\n")
    old = repo.commit()
    repo.write("a.md", "# Synthetic\nNewer\n")
    repo.commit()
    pending = harness(repo, tmp_path)
    pending.backend.fail_checkpoint = True
    with pytest.raises(IngestionError):
        pending.run()
    pending.backend.fail_checkpoint = False
    stale = harness(repo, tmp_path, pending.backend, source=repo.settings(old))
    with pytest.raises(IngestionError, match="superseded"):
        stale.run()


def test_scope_admission_approves_new_commits_but_never_rebuilds(
    repo: GitRepo, tmp_path: Path
) -> None:
    scope = {"policy_version": 2, "approved_commit": ABSENT}
    repo.write("a.md", "# Synthetic\nOne\n")
    repo.commit()
    first = harness(repo, tmp_path, admission_updates=scope)
    first.run()
    repo.write("a.md", "# Synthetic\nTwo\n")
    repo.commit()
    second = harness(repo, tmp_path, first.backend, admission_updates=scope)
    second.run()
    with pytest.raises(IngestionError, match="Admission"):
        harness(
            repo,
            tmp_path,
            second.backend,
            operation="rebuild",
            role="non-serving",
            admission_updates=scope,
        )
    with pytest.raises(IngestionError, match="Admission"):
        harness(repo, tmp_path, admission_updates={"policy_version": 2})
    with pytest.raises(IngestionError, match="[Aa]dmission"):
        harness(repo, tmp_path, admission_updates={"policy_version": 3})


def test_admission_blocks_unresolved_permissions_and_placeholders(
    repo: GitRepo, tmp_path: Path
) -> None:
    for update in (
        {"access_mode": "protected-without-mapping"},
        {"approval_reference": "<REVIEW_REQUIRED>"},
        {"repo_id": "wrong-source"},
        {"allow_bootstrap": "yes"},
    ):
        with pytest.raises(IngestionError):
            harness(repo, tmp_path, admission_updates=update)


def test_source_selection_change_needs_a_bound_review_and_reconciliation(
    repo: GitRepo,
    tmp_path: Path,
) -> None:
    repo.write("a.md", "# Synthetic\nKeep\n")
    repo.write("excluded/b.md", "# Synthetic\nRemove after review\n")
    repo.commit()
    initial = harness(repo, tmp_path)
    initial.run()
    changed = replace(repo.settings(), exclude=("excluded/**",))
    with pytest.raises(IngestionError, match="admission|Admission"):
        harness(
            repo,
            tmp_path,
            initial.backend,
            source=changed,
            admission_updates={"selection_fingerprint": repo.settings().selection_fingerprint()},
        )
    refused = harness(repo, tmp_path, initial.backend, source=changed)
    with pytest.raises(IngestionError, match="selection"):
        refused.run()
    accepted = harness(
        repo,
        tmp_path,
        initial.backend,
        source=changed,
        operation="reconcile",
        admission_updates={"allow_selection_reset": True},
    )
    accepted.run()
    assert len(initial.backend.docs["synthetic-index"]) == 1
