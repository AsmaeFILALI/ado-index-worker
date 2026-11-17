from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import GitRepo

from ado_markdown_ingestion import source
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.source import citation_url, matches, read_snapshot, validate_history


def test_reads_fixed_tracked_blobs_not_worktree(repo: GitRepo) -> None:
    repo.write("nested/a space # é.md", "# Approved\nOriginal source.\n")
    repo.write("not-markdown.txt", "ignored")
    commit = repo.commit()
    repo.write("nested/a space # é.md", "# Dirty\nDo not read.")
    repo.write("untracked.md", "# Not approved")
    snapshot = read_snapshot(repo.settings(commit))
    assert snapshot.commit == commit
    assert [item.path for item in snapshot.files] == ["nested/a space # é.md"]
    assert snapshot.files[0].content == "# Approved\nOriginal source.\n"
    repo.commit()
    assert read_snapshot(repo.settings(commit)).files == snapshot.files


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a.md", "**/*.md", True),
        ("x/a.md", "*.md", False),
        ("x/a.md", "**/*.md", True),
        ("x/y/a.md", "x/**/a.md", True),
        ("x/a.md", "x/**/a.md", True),
        ("a.MD", "**/*.md", False),
        ("a.md", "[ab].md", True),
        ("x/readme.md", "x/?eadme.md", True),
    ],
)
def test_git_globs(path: str, pattern: str, expected: bool) -> None:
    assert matches(path, (pattern,)) == expected


def test_includes_and_excludes(repo: GitRepo) -> None:
    repo.write("root.md", "# One\nBody")
    repo.write("private/a.md", "# Not selected\nBody")
    commit = repo.commit()
    assert [f.path for f in read_snapshot(repo.settings(commit, exclude=["private/**"])).files] == [
        "root.md"
    ]


def test_source_url_is_pinned_and_encoded(repo: GitRepo) -> None:
    settings = repo.settings()
    path = "space & #/é?.md"
    url = citation_url(settings.repo_url, path, settings.commit)
    query = parse_qs(urlsplit(url).query)
    assert query["path"] == ["/" + path]
    assert query["version"] == ["GC" + settings.commit]
    assert urlsplit(url).fragment == ""
    assert " " not in url and "#L" not in url


def test_missing_ref_commit_and_wrong_root_fail(repo: GitRepo) -> None:
    with pytest.raises(IngestionError, match="commit|history"):
        read_snapshot(repo.settings("f" * 40))
    with pytest.raises(IngestionError, match="ref"):
        read_snapshot(repo.settings(source_ref="refs/heads/unavailable"))
    subdir = repo.path / "child"
    subdir.mkdir()
    with pytest.raises(IngestionError, match="root"):
        read_snapshot(repo.settings(source_root=str(subdir)))


def test_history_forward_superseded_and_divergent(repo: GitRepo) -> None:
    first = repo.commit()
    repo.write("a.md", "# A\nnext")
    second = repo.commit()
    validate_history(repo.path, first, second)
    validate_history(repo.path, second, second)
    with pytest.raises(IngestionError, match="superseded"):
        validate_history(repo.path, second, first)
    repo.git("checkout", "-b", "other", first)
    repo.write("other.md", "# Other\nbranch")
    other = repo.commit()
    with pytest.raises(IngestionError, match="divergent"):
        validate_history(repo.path, second, other)
    with pytest.raises(IngestionError, match="history"):
        validate_history(repo.path, "f" * 40, second)


def test_empty_inventory_is_success_but_read_failure_is_not(repo: GitRepo) -> None:
    assert read_snapshot(repo.settings()).files == ()
    repo.git("config", "core.bare", "true")
    with pytest.raises(IngestionError):
        read_snapshot(repo.settings())


def test_symlink_blob_rejected_without_following_it(repo: GitRepo) -> None:
    blob = repo.git("hash-object", "-w", "--stdin", input_data=b"../../outside.md")
    oid = blob.stdout.decode().strip()
    repo.git("update-index", "--add", "--cacheinfo", f"120000,{oid},escape.md")
    repo.git("commit", "-m", "synthetic symlink")
    with pytest.raises(IngestionError, match="regular"):
        read_snapshot(repo.settings())


def test_invalid_utf8_markdown_is_quarantined_not_fatal(repo: GitRepo) -> None:
    (repo.path / "broken.md").write_bytes(b"\xff\xfe")
    repo.write("zero.md", "# Nul\n\x00\n")
    repo.write("good.md", "# Good\n\nbody\n")
    commit = repo.commit()
    snapshot = read_snapshot(repo.settings(commit))
    assert [file.path for file in snapshot.files] == ["good.md"]
    assert [(item.path, item.reason) for item in snapshot.quarantined] == [
        ("broken.md", "invalid-utf8"),
        ("zero.md", "nul-character"),
    ]


def test_unsafe_path_is_quarantined(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    # Git on Windows cannot even store colon or backslash names, so inject the validator verdict.
    original = source.validate_path

    def reject_unsafe(path: str) -> None:
        if "unsafe" in path:
            raise IngestionError("Unsafe or nonportable repository-relative source path")
        original(path)

    monkeypatch.setattr(source, "validate_path", reject_unsafe)
    repo.write("docs/unsafe name.md", "# x\n")
    repo.write("docs/fine.md", "# y\n")
    repo.commit()
    snapshot = read_snapshot(repo.settings())
    assert [(q.path, q.reason) for q in snapshot.quarantined] == [
        ("docs/unsafe name.md", "unsafe-or-nonportable-path")
    ]
    assert [file.path for file in snapshot.files] == ["docs/fine.md"]


def test_ref_verification_can_be_skipped_for_pull_request_checks(repo: GitRepo) -> None:
    repo.git("checkout", "-b", "feature")
    repo.write("branch.md", "# Branch\n\nbody\n")
    commit = repo.commit()
    snapshot = read_snapshot(repo.settings(commit), verify_ref=False)
    assert [file.path for file in snapshot.files] == ["branch.md"]


def test_submodule_markdown_is_not_treated_as_source(repo: GitRepo) -> None:
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{repo.settings().commit},module.md")
    repo.git("commit", "-m", "synthetic submodule entry")
    with pytest.raises(IngestionError, match="regular"):
        read_snapshot(repo.settings())


def test_nonapproved_branch_commit_fails(repo: GitRepo) -> None:
    repo.git("checkout", "-b", "unapproved")
    repo.write("branch.md", "# Unapproved\nSynthetic\n")
    commit = repo.commit()
    with pytest.raises(IngestionError, match="approved"):
        read_snapshot(repo.settings(commit))
