import json
from pathlib import Path

from conftest import ROOT, GitRepo
from test_contract import synthetic_schema
from test_offline_workflow import guarded_cli, plan_args

from ado_markdown_ingestion.util import digest, fingerprint


def test_output_is_explicit_and_never_overwrites_source(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("guide.md", "# Synthetic\nSecret-like fictional marker, not a credential.\n")
    repo.commit()
    output = tmp_path / "plan.json"
    result = guarded_cli(tmp_path, *plan_args(repo), "--output", str(output))
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text())["publishable"] is False
    assert "documents" not in json.loads(output.read_text())
    assert "Secret-like fictional marker" not in result.stderr + result.stdout
    forbidden = repo.path / "guide.md"
    before = forbidden.read_bytes()
    failed = guarded_cli(tmp_path, *plan_args(repo), "--output", str(forbidden))
    assert failed.returncode == 1
    assert forbidden.read_bytes() == before


def test_local_pinned_schema_is_not_live_evidence(repo: GitRepo, tmp_path: Path) -> None:
    artifact = tmp_path / "synthetic-schema.json"
    artifact.write_text(json.dumps(synthetic_schema()), encoding="utf-8")
    result = guarded_cli(
        tmp_path,
        *plan_args(repo),
        "--platform-schema",
        str(artifact),
        "--schema-sha256",
        digest(artifact.read_bytes()),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_validation"] == "offline-artifact-only"
    failed = guarded_cli(tmp_path, *plan_args(repo), "--platform-schema", str(artifact))
    assert failed.returncode == 1 and "SHA-256" in failed.stderr


def test_config_file_and_invalid_input_exit_codes(repo: GitRepo, tmp_path: Path) -> None:
    settings = repo.settings()
    config = tmp_path / "worker.toml"
    config.write_text(
        "\n".join(
            [
                "source_root = " + json.dumps(str(repo.path)),
                'repo_id = "synthetic-repository"',
                'source_ref = "refs/heads/approved"',
                "commit = " + json.dumps(settings.commit),
                "repo_url = " + json.dumps(settings.repo_url),
                'include = ["**/*.md"]',
                "contract = " + json.dumps(str(settings.contract)),
                "tokenizer_cache = " + json.dumps(str(ROOT / ".cache" / "tiktoken")),
            ]
        ),
        encoding="utf-8",
    )
    result = guarded_cli(tmp_path, "plan", "--config", str(config))
    assert result.returncode == 0, result.stderr
    for args in [
        ["plan"],
        ["plan", "--config", str(config), "--commit", "HEAD"],
        ["plan", "--config", str(tmp_path / "missing.toml")],
    ]:
        failed = guarded_cli(tmp_path, *args)
        assert failed.returncode == 1
        assert "Traceback" not in failed.stderr
    assert guarded_cli(tmp_path, "plan", "--target-tokens", "invalid").returncode == 2


def test_unchanged_files_retain_projected_document_revision(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("guide.md", "# Synthetic\nBody\n")
    first_commit = repo.commit()
    first = guarded_cli(tmp_path, *plan_args(repo), "--documents")
    previous = tmp_path / "prior.json"
    previous.write_text(first.stdout, encoding="utf-8")
    next_commit = repo.commit("another approved snapshot")
    result = guarded_cli(
        tmp_path,
        *plan_args(repo),
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
        "--documents",
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["inventory"]["source_version_id"] == next_commit
    assert data["inventory"]["files"][0]["documents"][0]["source_version_id"] == first_commit
    assert data["documents"][0]["source_version_id"] == next_commit
    assert data["document_stage"] == "target-snapshot-drafts-not-indexed"
    assert data["changes"]["upsert_document_ids"] == []


def test_corrupt_or_incompatible_inventory_is_not_unknown(repo: GitRepo, tmp_path: Path) -> None:
    first = guarded_cli(tmp_path, *plan_args(repo))
    data = json.loads(first.stdout)
    data["inventory"]["processing_fingerprint"] = "0" * 64
    data["inventory"]["integrity_sha256"] = fingerprint(
        {key: value for key, value in data["inventory"].items() if key != "integrity_sha256"}
    )
    previous = tmp_path / "incompatible.json"
    previous.write_text(json.dumps(data), encoding="utf-8")
    failed = guarded_cli(
        tmp_path,
        *plan_args(repo),
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    )
    assert failed.returncode == 1 and "fingerprint" in failed.stderr
    previous.write_bytes(b'{"duplicate":1,"duplicate":2}')
    failed = guarded_cli(
        tmp_path,
        *plan_args(repo),
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    )
    assert failed.returncode == 1 and "duplicate keys" in failed.stderr


def test_read_failure_never_becomes_a_deletion_plan(repo: GitRepo, tmp_path: Path) -> None:
    repo.write("guide.md", "# Synthetic\nDo not invent an empty source\n")
    repo.commit()
    first = guarded_cli(tmp_path, *plan_args(repo))
    previous = tmp_path / "source-prior.json"
    previous.write_text(first.stdout, encoding="utf-8")
    args = plan_args(repo)
    args[args.index("--commit") + 1] = "f" * 40
    failed = guarded_cli(
        tmp_path,
        *args,
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    )
    assert failed.returncode == 1 and not failed.stdout
    assert "history" in failed.stderr
