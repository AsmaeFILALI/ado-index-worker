import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from ado_markdown_ingestion import __version__
from ado_markdown_ingestion.embedding_text import EMBEDDING_TEXT_VERSION
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.util import fingerprint, integer_value, string_value

COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
FIELDS = {
    "source_root",
    "repo_id",
    "source_ref",
    "commit",
    "repo_url",
    "include",
    "exclude",
    "contract",
    "tokenizer_cache",
    "target_tokens",
    "max_content_chars",
    "max_input_tokens",
    "overlap_tokens",
    "model",
    "deployment",
    "dimensions",
}
PATH_FIELDS = {"source_root", "contract", "tokenizer_cache"}
# The orchestrator truncates content at 4,000 characters; the cap includes the breadcrumb.
MAX_CONTENT_CHARS = 3800
MAX_TARGET_TOKENS = 900
INT_FIELDS = {
    "target_tokens",
    "max_content_chars",
    "max_input_tokens",
    "overlap_tokens",
    "dimensions",
}


def validate_ref(ref: str) -> None:
    parts = ref.split("/")
    if (
        len(parts) < 3
        or parts[0] != "refs"
        or parts[1] not in {"heads", "tags"}
        or any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in parts)
        or any(item in ref for item in ("..", "@{", "//"))
        or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", ref)
    ):
        raise IngestionError("source_ref must be a valid approved full heads/tags Git ref")


def validate_repo_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise IngestionError("Invalid Azure DevOps repository URL") from exc
    if (
        parsed.scheme != "https"
        or not (
            host == "dev.azure.com" or (host.endswith(".visualstudio.com") and host.count(".") == 2)
        )
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[^/]+/(?:[^/]+/)?_git/[^/]+/?", parsed.path)
        or re.search(r"[\x00-\x20\x7f<>]", url)
    ):
        raise IngestionError("repo_url must be an approved HTTPS Azure DevOps Git repository URL")


def _patterns(data: Mapping[str, object], key: str, *, required: bool = False) -> tuple[str, ...]:
    values = data.get(key, [])
    if not isinstance(values, list) or (required and not values):
        raise IngestionError(f"{key} must be a {'nonempty ' if required else ''}list of Git globs")
    result: list[str] = []
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith("/")
            or "\\" in value
            or ".." in value.split("/")
            or "\0" in value
        ):
            raise IngestionError(f"{key} contains an invalid repository-relative Git glob")
        result.append(value)
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    source_root: Path
    repo_id: str
    source_ref: str
    commit: str
    repo_url: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    contract: Path
    tokenizer_cache: Path
    target_tokens: int = 900
    max_content_chars: int = 3800
    max_input_tokens: int = 8000
    overlap_tokens: int = 0
    model: str = "text-embedding-3-large"
    deployment: str = "text-embedding"
    dimensions: int = 3072

    @classmethod
    def from_mapping(cls, data: Mapping[str, object], base: Path) -> "Settings":
        if set(data) - FIELDS:
            raise IngestionError("Unknown configuration setting")
        repo_id = string_value(data, "repo_id")
        if any(char.isspace() for char in repo_id) or any(char in repo_id for char in "<>\0"):
            raise IngestionError("repo_id must be an explicit immutable repository identifier")
        ref = string_value(data, "source_ref")
        validate_ref(ref)
        commit = string_value(data, "commit").lower()
        if not COMMIT.fullmatch(commit):
            raise IngestionError(
                "commit must be an immutable full Git object ID, not HEAD or a ref"
            )
        url = string_value(data, "repo_url")
        validate_repo_url(url)
        target = integer_value(data, "target_tokens", 900)
        chars = integer_value(data, "max_content_chars", 3800)
        hard = integer_value(data, "max_input_tokens", 8000)
        overlap = integer_value(data, "overlap_tokens", 0)
        dimensions = integer_value(data, "dimensions", 3072)
        model = data.get("model", "text-embedding-3-large")
        deployment = data.get("deployment", "text-embedding")
        if model != "text-embedding-3-large" or dimensions != 3072:
            raise IngestionError("This consumer contract requires text-embedding-3-large / 3072")
        if not isinstance(deployment, str) or not deployment.strip() or "<" in deployment:
            raise IngestionError("deployment must be an explicit nonsecret deployment name")
        if not (
            0 < target <= min(hard, MAX_TARGET_TOKENS)
            and hard <= 8000
            and 0 < chars <= MAX_CONTENT_CHARS
            and 0 <= overlap < target
        ):
            raise IngestionError(
                "Invalid token, character, or overlap budgets "
                f"(at most {MAX_TARGET_TOKENS} tokens and {MAX_CONTENT_CHARS} characters)"
            )

        def path(key: str, default: str | None = None) -> Path:
            value = data.get(key, default)
            if not isinstance(value, str) or not value or "<" in value:
                raise IngestionError(f"{key} must be an explicit filesystem path")
            item = Path(value).expanduser()
            return (base / item).resolve() if not item.is_absolute() else item.resolve()

        return cls(
            source_root=path("source_root"),
            repo_id=repo_id,
            source_ref=ref,
            commit=commit,
            repo_url=url.rstrip("/"),
            include=_patterns(data, "include", required=True),
            exclude=_patterns(data, "exclude"),
            contract=path("contract"),
            tokenizer_cache=path("tokenizer_cache", ".cache/tiktoken"),
            target_tokens=target,
            max_content_chars=chars,
            max_input_tokens=hard,
            overlap_tokens=overlap,
            model=model,
            deployment=deployment,
            dimensions=dimensions,
        )

    def processing_fingerprint(self) -> str:
        return fingerprint(
            {
                "pipeline": __version__,
                "parser": version("markdown-it-py"),
                "parser_options": "commonmark+table;html-blocks-inert;front-matter-stripped",
                "tokenizer": version("tiktoken"),
                "encoding": "cl100k_base",
                "canonical": "breadcrumb-line;nfc;LF;strip-edge-newlines;h1-h3-sections;"
                "merge-small-children-under-80-tokens;definition-footers;"
                "comment-and-toc-removed",
                "target_tokens": self.target_tokens,
                "max_input_tokens": self.max_input_tokens,
                "max_content_chars": self.max_content_chars,
                "overlap_tokens": self.overlap_tokens,
                "embedding_text": EMBEDDING_TEXT_VERSION,
                "model": self.model,
                "deployment": self.deployment,
                "dimensions": self.dimensions,
            }
        )

    def selection_fingerprint(self) -> str:
        return fingerprint(
            {
                "repo_url": self.repo_url,
                "include": self.include,
                "exclude": self.exclude,
            }
        )


def load_settings(
    config: Path | None, overrides: Mapping[str, object], environ: Mapping[str, str] | None = None
) -> Settings:
    data: dict[str, object] = {}
    base = Path.cwd()
    if config is not None:
        try:
            with config.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise IngestionError("Cannot read valid TOML configuration") from exc
        base = config.resolve().parent
    env = os.environ if environ is None else environ
    merged: dict[str, object] = {}
    for key, value in env.items():
        if not key.startswith("ADO_INGEST_"):
            continue
        name = key.removeprefix("ADO_INGEST_").lower()
        if name not in FIELDS:
            raise IngestionError("Unknown ingestion environment setting")
        if name in INT_FIELDS:
            try:
                merged[name] = int(value)
            except ValueError as exc:
                raise IngestionError("Invalid integer environment setting") from exc
        elif name in {"include", "exclude"}:
            merged[name] = value.split(";")
        else:
            merged[name] = value
    merged.update(overrides)
    for name, override_value in merged.items():
        if name in PATH_FIELDS and isinstance(override_value, str):
            merged[name] = str(Path(override_value).expanduser().resolve())
    data.update(merged)
    return Settings.from_mapping(data, base)
