"""Per-file quarantine, the offline pull-request check, and the quarantine policy switch."""

import json
from pathlib import Path

from conftest import GitRepo
from test_offline_workflow import guarded_cli, plan_args
from test_sync import harness

from ado_markdown_ingestion.util import digest


def test_plan_quarantines_unreadable_and_unchunkable_files_but_still_plans(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("good.md", "# Good\n\n## S\n\nbody\n")
    (repo.path / "latin.md").write_bytes("# Procédure\n".encode("cp1252"))
    repo.write("long-title.md", "# " + "T" * 100 + "\n\n## S\n\nbody\n")
    repo.commit()
    args = [*plan_args(repo), "--max-content-chars", "60"]
    result = guarded_cli(tmp_path, *args)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    reasons = {item["path"]: item["reason"] for item in plan["quarantined"]}
    assert reasons["latin.md"] == "invalid-utf8"
    assert reasons["long-title.md"].startswith("chunking-failed")
    assert plan["counts"]["quarantined"] == 2
    assert "Proc" not in result.stderr and "file_quarantined" in result.stderr

    strict = guarded_cli(tmp_path, *args, "--fail-on-quarantine")
    assert strict.returncode == 3, strict.stderr


def test_a_quarantined_file_keeps_its_previously_planned_documents(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("keep.md", "# Keep\n\n## S\n\noriginal body\n")
    repo.write("other.md", "# Other\n\n## S\n\nother body\n")
    repo.commit()
    first = guarded_cli(tmp_path, *plan_args(repo))
    assert first.returncode == 0, first.stderr
    original = json.loads(first.stdout)
    previous = tmp_path / "previous.json"
    previous.write_text(first.stdout, encoding="utf-8")
    trust = [
        "--previous-inventory",
        str(previous),
        "--previous-sha256",
        digest(previous.read_bytes()),
    ]

    (repo.path / "keep.md").write_bytes(b"\xff\xfe broken")
    repo.commit()
    second = guarded_cli(tmp_path, *plan_args(repo), *trust)
    assert second.returncode == 0, second.stderr
    plan = json.loads(second.stdout)
    assert plan["quarantined"] == [
        {"path": "keep.md", "reason": "invalid-utf8", "retained_previous": True}
    ]
    assert plan["changes"]["deletions"] == []
    assert "keep.md" not in plan["changes"]["deleted_files"]
    paths = [entry["path"] for entry in plan["inventory"]["files"]]
    assert paths == ["keep.md", "other.md"]
    assert plan["inventory"]["files"][0] == original["inventory"]["files"][0]


def test_check_reports_problems_offline_for_a_commit_that_is_not_the_approved_ref(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.git("checkout", "-b", "feature/pr")
    repo.write("good.md", "# Good\n\n## S\n\nbody\n")
    repo.commit()
    args = ["check", *plan_args(repo)[1:]]
    ok = guarded_cli(tmp_path, *args)
    assert ok.returncode == 0, ok.stderr
    report = json.loads(ok.stdout)
    assert report["problems"] == [] and report["counts"]["documents"] == 1

    (repo.path / "bad.md").write_bytes(b"\xff\xfe")
    repo.commit()
    failing = guarded_cli(tmp_path, *["check", *plan_args(repo)[1:]])
    assert failing.returncode == 1
    assert json.loads(failing.stdout)["problems"] == [{"path": "bad.md", "problem": "invalid-utf8"}]


def test_publication_keeps_indexed_documents_of_a_file_that_becomes_unreadable(
    repo: GitRepo, tmp_path: Path
) -> None:
    repo.write("keep.md", "# Keep\n\n## S\n\noriginal body\n")
    repo.commit()
    first = harness(repo, tmp_path)
    first.run()
    docs = first.backend.docs["synthetic-index"]
    keep_keys = set(docs)
    assert len(keep_keys) == 1

    (repo.path / "keep.md").write_bytes(b"\xff\xfe broken")
    repo.write("new.md", "# New\n\n## S\n\nfresh body\n")
    repo.commit()
    second = harness(repo, tmp_path, first.backend)
    result = second.run()
    assert result["quarantined"] == [
        {"path": "keep.md", "reason": "invalid-utf8", "retained_previous": True}
    ]
    assert result["counts"]["quarantined"] == 1 and result["counts"]["deletions"] == 0
    assert keep_keys <= set(docs) and len(docs) == 2

    repo.write("keep.md", "# Keep\n\n## S\n\nfixed body\n")
    repo.commit()
    third = harness(repo, tmp_path, first.backend)
    fixed = third.run()
    assert fixed["quarantined"] == []
    assert any("fixed body" in str(doc["content"]) for doc in docs.values())
    for item in (first, second, third):
        item.close()
