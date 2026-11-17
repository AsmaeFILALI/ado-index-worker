import json

import pytest
from azure.storage.blob import ContainerClient
from azure_fakes import Credential, Transport
from fakes import Azure, Clock

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.retry import Retry
from ado_markdown_ingestion.state import BlobState


def store(
    backend: Azure, clock: Clock | None = None, compatibility: str = "compatible"
) -> BlobState:
    clock = clock or Clock()
    return BlobState(
        ContainerClient(
            "https://synthetic.blob.core.windows.net",
            "synthetic-state",
            credential=Credential(),
            transport=Transport(backend),
            retry_total=0,
        ),
        "physical-index",
        compatibility,
        retry=Retry(sleep=clock.sleep, clock=clock, jitter=lambda: 0),
        clock=clock,
        start_renew_thread=False,
    )


def test_real_blob_sdk_journal_commit_and_scope_isolation() -> None:
    backend = Azure()
    state = store(backend)
    with state.owned():
        assert state.load("scope-a", "checkpoint") is None
        state.journal("scope-a", {"keys": ["one"], "commit": "synthetic"})
        assert state.load("scope-a", "pending") == {"keys": ["one"], "commit": "synthetic"}
        state.commit("scope-a", {"keys": ["one"], "commit": "synthetic"})
        assert state.load("scope-a", "pending") is None
        assert state.load("scope-a", "checkpoint") is not None
        state.commit("scope-b", {"keys": ["two"]})
        checkpoint = state.load("scope-a", "checkpoint")
        assert checkpoint is not None and checkpoint["keys"] == ["one"]
        state.renew_once()
    assert backend.events[0] == "lease-acquire"
    assert backend.events[-1] == "lease-release"
    for raw in backend.blobs.values():
        assert "contentVector" not in json.loads(raw)


def test_lost_lease_expiry_and_etag_conflict_fail_closed() -> None:
    backend, clock = Azure(), Clock()
    state = store(backend, clock)
    with pytest.raises(IngestionError, match="lease"):
        with state.owned():
            backend.leases.clear()
            with pytest.raises(IngestionError):
                state.renew_once()
            state.guard()
    state = store(Azure(), clock)
    with state.owned(), pytest.raises(IngestionError, match="expired"):
        clock.value += 60
        state.guard()
    backend = Azure()
    state = store(backend)
    with state.owned():
        backend.conflict_next_control = True
        with pytest.raises(IngestionError):
            state.journal("scope", {"keys": []})
        assert state.load("scope", "pending") is None


def test_failed_checkpoint_preserves_pending_and_prior_success() -> None:
    backend = Azure()
    state = store(backend)
    with state.owned():
        state.commit("scope", {"commit": "old"})
        state.journal("scope", {"commit": "new"})
        backend.fail_checkpoint = True
        with pytest.raises(IngestionError):
            state.commit("scope", {"commit": "new"})
        assert state.load("scope", "checkpoint") == {"commit": "old"}
        assert state.load("scope", "pending") == {"commit": "new"}


def test_public_container_and_configuration_mismatch_are_not_bootstraps() -> None:
    backend = Azure()
    backend.public_container = True
    with pytest.raises(IngestionError, match="private"), store(backend).owned():
        pass
    backend.public_container = False
    with store(backend).owned():
        pass
    with (
        pytest.raises(IngestionError, match="new pre-provisioned"),
        store(backend, compatibility="incompatible").owned(),
    ):
        pass


def test_raw_content_is_never_written_into_state() -> None:
    state = store(Azure())
    with state.owned(), pytest.raises(IngestionError, match="Raw content"):
        state.journal("scope", {"content": "not allowed"})


def test_one_publisher_for_every_scope_on_the_physical_index() -> None:
    backend = Azure()
    first, second = store(backend), store(backend)
    with first.owned():
        with pytest.raises(IngestionError), second.owned():
            pass
        first.guard()


def test_renewal_thread_is_stopped_when_context_exits() -> None:
    state = store(Azure())
    state._start_renew_thread = True
    with state.owned():
        assert state._thread is not None and state._thread.is_alive()
    assert state._thread is None
