"""Helpers that drive the real chunker and document builder without Git or network access."""

import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.contract import Contract
from ado_markdown_ingestion.documents import DraftDocument, build_documents
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import Chunk, canonical_lines, parse_chunks
from ado_markdown_ingestion.source import SourceFile
from ado_markdown_ingestion.tokenizer import Encoder, load_encoder
from ado_markdown_ingestion.util import digest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs" / "index-contract.json"
COMMIT = "a" * 40

# Budgets agreed in the design: 3,800 characters and 900 cl100k tokens including the breadcrumb.
DESIGN_MAX_CHARS = 3800
DESIGN_MAX_TOKENS = 900

PREFIX = re.compile(r"\A[^\n]*\n\n")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_=-]{1,1024}")

_encoder: Encoder | None = None


def encoder() -> Encoder:
    global _encoder
    if _encoder is None:
        _encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    return _encoder


def settings(**overrides: object) -> Settings:
    data: dict[str, object] = {
        "source_root": str(ROOT),
        "repo_id": "review-repository",
        "source_ref": "refs/heads/main",
        "commit": COMMIT,
        "repo_url": "https://dev.azure.com/example-org/example-project/_git/example-repo",
        "include": ["**/*.md"],
        "contract": str(CONTRACT),
        "tokenizer_cache": str(ROOT / ".cache" / "tiktoken"),
    }
    data.update(overrides)
    return Settings.from_mapping(data, ROOT)


def deployed() -> Settings:
    """Settings exactly at the worker defaults (500 tokens, 4000 chars)."""
    return settings()


def design() -> Settings:
    """Settings that implement the agreed design budget (3,800 chars, 900 tokens)."""
    return replace(settings(), max_content_chars=DESIGN_MAX_CHARS, target_tokens=DESIGN_MAX_TOKENS)


def decode_like_source(raw: bytes) -> str:
    """Mirror `source.read_snapshot`: strict UTF-8, NUL rejected."""
    text = raw.decode("utf-8")
    if "\0" in text:
        raise IngestionError("Selected Markdown contains invalid NUL text")
    return text


def normalize(text: str) -> str:
    """Normalization the pipeline applies before parsing: BOM, CRLF and CR to LF."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class Run:
    name: str
    text: str
    chunks: list[Chunk] = field(default_factory=list)
    error: str | None = None

    @property
    def lines(self) -> list[str]:
        """Source lines after the allowed normalizations; removed text keeps its line breaks."""
        return canonical_lines(self.text) or [""]

    def bodies(self) -> list[str]:
        return [PREFIX.sub("", chunk.content, count=1) for chunk in self.chunks]


def run_text(
    text: str, name: str = "fixture", path: str = "docs/fixture.md", config: Settings | None = None
) -> Run:
    chosen = config or deployed()
    try:
        chunks = parse_chunks(text, path, chosen, encoder())
    except IngestionError as exc:
        return Run(name, text, error=str(exc))
    return Run(name, text, chunks)


def run_bytes(
    raw: bytes, name: str = "fixture", path: str = "docs/fixture.md", config: Settings | None = None
) -> Run:
    try:
        text = decode_like_source(raw)
    except (UnicodeError, IngestionError) as exc:
        return Run(name, "", error=f"source read rejected: {type(exc).__name__}")
    return run_text(text, name, path, config)


def drafts(
    run: Run, path: str = "docs/fixture.md", config: Settings | None = None
) -> list[DraftDocument]:
    chosen = config or deployed()
    source = SourceFile(path, "b" * 40, digest(run.text), run.text)
    return build_documents(source, run.chunks, chosen, Contract.load(CONTRACT))


def measure(chunk: Chunk) -> tuple[int, int]:
    return len(chunk.content), encoder().count(chunk.content)


def coverage(run: Run) -> tuple[set[int], dict[int, int]]:
    """Return (uncovered nonblank source lines, line -> number of chunks that claim it).

    Heading lines are excluded because the breadcrumb carries them by design. Front matter,
    HTML comments and the TOC macro are allowed removals (blank in `Run.lines`).
    """
    source = run.lines
    claimed: dict[int, int] = {}
    for chunk in run.chunks:
        for number in range(chunk.start_line, chunk.end_line + 1):
            claimed[number] = claimed.get(number, 0) + 1
    atx = re.compile(r"^[ >]*(?:[-*+] +)?#{1,6}(?:[ \t]|$)")
    underline = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
    structural: set[int] = set()
    for number, line in enumerate(source, start=1):
        if atx.match(line):
            structural.add(number)
        elif underline.match(line) and number > 1 and source[number - 2].strip():
            structural.update((number - 1, number))
    uncovered = {
        number
        for number, line in enumerate(source, start=1)
        if line.strip() and number not in claimed and number not in structural
    }
    return uncovered, claimed


def squash(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", value))
