import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import CONTRACT, ROOT, GitRepo

from ado_markdown_ingestion.util import digest


def guarded_cli(
    tmp_path: Path, *args: str, console: bool = False
) -> subprocess.CompletedProcess[str]:
    guard = tmp_path / "process guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(
        "import socket, sys\n"
        "def blocked(*args, **kwargs):\n"
        "    raise AssertionError('Network forbidden in offline subprocess')\n"
        "socket.socket.connect = blocked\n"
        "socket.create_connection = blocked\n"
        "socket.getaddrinfo = blocked\n"
        "class NoAzure:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0] in {'azure', 'openai'}:\n"
        "            raise AssertionError('Azure client import forbidden')\n"
        "sys.meta_path.insert(0, NoAzure())\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("ADO_INGEST_")}
    env["PYTHONPATH"] = str(guard)
    executable = shutil.which("ado-ingest")
    if console:
        assert executable is not None, "The packaged console entry point must be installed"
    command = (
        [executable]
        if console and executable is not None
        else [sys.executable, "-m", "ado_markdown_ingestion.cli"]
    )
    return subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )


def plan_args(repo: GitRepo, *, cache: Path | None = None) -> list[str]:
    return [
        "plan",
        "--source-root",
        str(repo.path),
        "--repo-id",
        "synthetic-repository",
        "--source-ref",
        "refs/heads/approved",
        "--commit",
        repo.settings().commit,
        "--repo-url",
        repo.settings().repo_url,
        "--include",
        "**/*.md",
        "--contract",
        str(CONTRACT),
        "--tokenizer-cache",
        str(cache or ROOT / ".cache" / "tiktoken"),
    ]


def test_offline_plan_retry_edit_rename_delete_workflow(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("guide.md", "# Synthetic Guide\n## Reading\nFictional gauge reading: 42.\n")
    repo.commit()
    initial = guarded_cli(tmp_path, *plan_args(repo), "--documents")
    assert initial.returncode == 0, initial.stderr
    first = json.loads(initial.stdout)
    assert first["publishable"] is False
    assert first["counts"]["azure_calls"] == first["counts"]["embedding_calls"] == 0
    assert first["changes"]["deletions"] is None
    assert first["deletion_status"] == "unknown"
    assert first["documents"] and len(first["documents"][0]) == 20
    assert not first["documents"][0].keys() & {"contentVector", "ingested_at", "ingest_run_id"}
    assert "Fictional gauge reading" not in initial.stderr

    previous = tmp_path / "previous.json"
    previous.write_text(initial.stdout, encoding="utf-8")
    trust = [
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    ]
    again = guarded_cli(tmp_path, *plan_args(repo), *trust)
    assert again.returncode == 0, again.stderr
    rerun = json.loads(again.stdout)
    assert rerun["changes"]["upsert_document_ids"] == []
    assert rerun["changes"]["deletions"] == []
    assert rerun["changes"]["unchanged_files"] == ["guide.md"]

    repo.write("guide.md", "# Synthetic Guide\n## Reading\nFictional gauge reading: 43.\n")
    repo.commit()
    edited = guarded_cli(tmp_path, *plan_args(repo), *trust, "--documents")
    assert edited.returncode == 0, edited.stderr
    changed = json.loads(edited.stdout)
    assert changed["documents"][0]["id"] == first["documents"][0]["id"]
    assert changed["changes"]["upsert_document_ids"] == [first["documents"][0]["id"]]
    previous.write_text(edited.stdout, encoding="utf-8")
    trust[-1] = digest(previous.read_bytes())

    repo.git("mv", "guide.md", "renamed é.md")
    repo.commit()
    renamed = guarded_cli(tmp_path, *plan_args(repo), *trust)
    assert renamed.returncode == 0, renamed.stderr
    assert json.loads(renamed.stdout)["changes"]["deletions"] == [first["documents"][0]["id"]]
    previous.write_text(renamed.stdout, encoding="utf-8")
    trust[-1] = digest(previous.read_bytes())
    repo.git("rm", "renamed é.md")
    repo.commit()
    deleted = guarded_cli(tmp_path, *plan_args(repo), *trust)
    assert deleted.returncode == 0, deleted.stderr
    final = json.loads(deleted.stdout)
    assert final["counts"]["files"] == final["counts"]["documents"] == 0
    assert len(final["changes"]["deletions"]) == 1
    assert final["inventory"]["evidence"] == "offline-projection-not-published"


def test_cold_cache_plan_fails_without_network(repo: GitRepo, tmp_path: Path) -> None:
    result = guarded_cli(tmp_path, *plan_args(repo, cache=tmp_path / "cold"))
    assert result.returncode == 1
    assert "prepare-tokenizer" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "cold").exists()


def test_trust_hash_and_scope_mismatch_fail(repo: GitRepo, tmp_path: Path) -> None:
    first = guarded_cli(tmp_path, *plan_args(repo))
    assert first.returncode == 0, first.stderr
    previous = tmp_path / "baseline.json"
    previous.write_text(first.stdout, encoding="utf-8")
    missing = guarded_cli(tmp_path, *plan_args(repo), "--previous-inventory", str(previous))
    assert missing.returncode == 1 and "SHA-256" in missing.stderr
    bad = guarded_cli(
        tmp_path,
        *plan_args(repo),
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        "0" * 64,
    )
    assert bad.returncode == 1 and "integrity" in bad.stderr
    args = plan_args(repo)
    args[args.index("--repo-id") + 1] = "foreign-repository"
    scoped = guarded_cli(
        tmp_path,
        *args,
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    )
    assert scoped.returncode == 1 and "scope" in scoped.stderr


@pytest.mark.parametrize("operation", ["sync", "reconcile", "rebuild"])
def test_publishing_commands_require_explicit_admission(tmp_path: Path, operation: str) -> None:
    result = guarded_cli(tmp_path, operation)
    assert result.returncode == 1
    assert "--authorize-publish" in result.stderr


def test_help_and_prepared_tokenizer_are_available_offline(tmp_path: Path) -> None:
    result = guarded_cli(tmp_path, "--help")
    assert result.returncode == 0 and "plan" in result.stdout
    prepared = guarded_cli(
        tmp_path, "prepare-tokenizer", "--cache-dir", str(ROOT / ".cache" / "tiktoken")
    )
    assert prepared.returncode == 0
    assert len(json.loads(prepared.stdout)["sha256"]) == 64


def test_installed_console_plans_the_original_fixture(repo: GitRepo, tmp_path: Path) -> None:
    fixture = ROOT / "tests" / "fixtures" / "markdown" / "synthetic-guide.md"
    repo.write("synthetic guide.md", fixture.read_text(encoding="utf-8"))
    repo.commit()
    result = guarded_cli(tmp_path, *plan_args(repo), "--documents", console=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["counts"]["documents"] == 4
    assert data["configuration"]["effective_model_revision"] is None
    for doc in data["documents"]:
        assert len(doc) == 20 and len(doc["content"]) <= 3800
        assert "contentVector" not in doc
