import fnmatch
import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from ado_markdown_ingestion.config import COMMIT, Settings, validate_repo_url
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.util import digest


@dataclass(frozen=True)
class SourceFile:
    path: str
    blob_id: str
    checksum_sha256: str
    content: str


@dataclass(frozen=True)
class Quarantined:
    """A selected file that cannot be processed; reported by path and fixed reason only."""

    path: str
    reason: str


@dataclass(frozen=True)
class Snapshot:
    commit: str
    files: tuple[SourceFile, ...]
    quarantined: tuple[Quarantined, ...] = ()


def _git(root: Path, *args: str, allow_false: bool = False) -> subprocess.CompletedProcess[bytes]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GIT_", "ADO_INGEST_"))
    }
    env.update({"GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"})
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=", *args],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IngestionError("Cannot read source checkout using Git") from exc
    if result.returncode != 0 and not (allow_false and result.returncode == 1):
        raise IngestionError(
            "Git source/ref/history read failed; verify checkout and fetch history"
        )
    return result


def validate_path(path: str) -> None:
    if (
        not path
        or path.startswith("/")
        or "\0" in path
        or "\\" in path
        or any(part in {"", ".", "..", ".git"} for part in path.split("/"))
        or ":" in path
    ):
        raise IngestionError("Unsafe or nonportable repository-relative source path")


def matches(path: str, patterns: tuple[str, ...]) -> bool:
    parts = tuple(path.split("/"))

    @cache
    def match(pattern: tuple[str, ...], position: int, index: int) -> bool:
        if position == len(pattern):
            return index == len(parts)
        if pattern[position] == "**":
            return match(pattern, position + 1, index) or (
                index < len(parts) and match(pattern, position, index + 1)
            )
        return (
            index < len(parts)
            and fnmatch.fnmatchcase(parts[index], pattern[position])
            and match(pattern, position + 1, index + 1)
        )

    return any(match(tuple(pattern.split("/")), 0, 0) for pattern in patterns)


def citation_url(repo_url: str, path: str, commit: str) -> str:
    validate_repo_url(repo_url)
    validate_path(path)
    if not COMMIT.fullmatch(commit):
        raise IngestionError("Citation requires an immutable full commit ID")
    return (
        repo_url.rstrip("/")
        + "?"
        + urlencode(
            {"path": "/" + path, "version": "GC" + commit, "_a": "contents"}, quote_via=quote
        )
    )


def validate_history(
    root: Path, previous: str, target: str, *, allow_divergence: bool = False
) -> None:
    for commit in (previous, target):
        if not COMMIT.fullmatch(commit):
            raise IngestionError("Invalid source history object ID")
        _git(root, "cat-file", "-e", commit + "^{commit}")
    if previous == target:
        return
    if (
        _git(root, "merge-base", "--is-ancestor", previous, target, allow_false=True).returncode
        == 0
    ):
        return
    if (
        _git(root, "merge-base", "--is-ancestor", target, previous, allow_false=True).returncode
        == 0
    ):
        raise IngestionError("Target snapshot is superseded by the prior successful inventory")
    if allow_divergence:
        if _git(root, "rev-parse", "--is-shallow-repository").stdout.strip() == b"true":
            raise IngestionError("Cannot authorize a history reset with incomplete shallow history")
        return
    raise IngestionError(
        "Source history is divergent or shallow; explicit reconciliation is required"
    )


def read_snapshot(settings: Settings, *, verify_ref: bool = True) -> Snapshot:
    root = settings.source_root
    if not root.is_dir():
        raise IngestionError("Source checkout root does not exist")
    try:
        top = Path(
            _git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8").strip()
        ).resolve()
    except UnicodeError as exc:
        raise IngestionError("Source checkout root must be UTF-8") from exc
    if top != root.resolve():
        raise IngestionError("source_root must be the exact source checkout root")
    _git(root, "cat-file", "-e", settings.commit + "^{commit}")
    if verify_ref:
        _git(root, "show-ref", "--verify", settings.source_ref)
        approved_tip = _git(root, "rev-parse", "--verify", settings.source_ref + "^{commit}")
        tip = approved_tip.stdout.decode("ascii").strip()
        if (
            _git(
                root, "merge-base", "--is-ancestor", settings.commit, tip, allow_false=True
            ).returncode
            != 0
        ):
            raise IngestionError("Target commit is not reachable from the approved source ref")
    entries = _git(root, "ls-tree", "-r", "-z", "--full-tree", settings.commit).stdout
    if entries and not entries.endswith(b"\0"):
        raise IngestionError("Incomplete source tree enumeration")
    files: list[SourceFile] = []
    quarantined: list[Quarantined] = []
    seen: set[str] = set()
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        try:
            header, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = header.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise IngestionError("Invalid UTF-8 source tree entry") from exc
        if PurePosixPath(path).suffix.lower() != ".md":
            continue
        if not matches(path, settings.include) or matches(path, settings.exclude):
            continue
        if path in seen:
            raise IngestionError("Duplicate source path in inventory")
        seen.add(path)
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise IngestionError("Selected Markdown must be a regular tracked blob, not a symlink")
        try:
            validate_path(path)
        except IngestionError:
            quarantined.append(Quarantined(path, "unsafe-or-nonportable-path"))
            continue
        raw = _git(root, "cat-file", "blob", oid).stdout
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            quarantined.append(Quarantined(path, "invalid-utf8"))
            continue
        if "\0" in text:
            quarantined.append(Quarantined(path, "nul-character"))
            continue
        files.append(SourceFile(path, oid, digest(raw), text))
    return Snapshot(
        settings.commit,
        tuple(sorted(files, key=lambda item: item.path)),
        tuple(sorted(quarantined, key=lambda item: item.path)),
    )
