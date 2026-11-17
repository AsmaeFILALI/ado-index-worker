"""Offline validation of Markdown files with the same parser and invariants as ingestion.

Used by the pull-request check: it needs no Azure access, secrets or approved ref.
"""

import re
from collections.abc import Sequence

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.contract import Contract
from ado_markdown_ingestion.documents import build_documents
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import Chunk, canonical_lines, parse_chunks
from ado_markdown_ingestion.source import read_snapshot
from ado_markdown_ingestion.tokenizer import Encoder

_ATX = re.compile(r"^[ >]*(?:[-*+] +)?#{1,6}(?:[ \t]|$)")
_SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


def check_invariants(
    text: str, chunks: Sequence[Chunk], settings: Settings, encoder: Encoder
) -> list[str]:
    """Return violated invariants; never includes document text."""
    lines = canonical_lines(text)
    problems: list[str] = []
    claimed: set[int] = set()
    for chunk in chunks:
        if not 1 <= chunk.start_line <= chunk.end_line <= max(1, len(lines)):
            problems.append("chunk line range is outside the source file")
            continue
        claimed.update(range(chunk.start_line, chunk.end_line + 1))
        if len(chunk.content) > settings.max_content_chars:
            problems.append("chunk exceeds the character budget")
        if encoder.count(chunk.content) > min(settings.target_tokens, settings.max_input_tokens):
            problems.append("chunk exceeds the token budget")
    structural: set[int] = set()
    for number, line in enumerate(lines, start=1):
        if _ATX.match(line):
            structural.add(number)
        elif _SETEXT_UNDERLINE.match(line) and number > 1 and lines[number - 2].strip():
            structural.update((number - 1, number))
    dropped = [
        number
        for number, line in enumerate(lines, start=1)
        if line.strip() and number not in claimed and number not in structural
    ]
    if dropped:
        problems.append(f"source lines not covered by any chunk: {_ranges(dropped)}")
    return problems


def _ranges(numbers: list[int]) -> str:
    spans: list[str] = []
    start = previous = numbers[0]
    for number in [*numbers[1:], -1]:
        if number == previous + 1:
            previous = number
            continue
        spans.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    return ", ".join(spans[:10]) + (" ..." if len(spans) > 10 else "")


def check_snapshot(settings: Settings, encoder: Encoder) -> dict[str, object]:
    """Chunk every selected file of the checked-out commit and report what would be rejected."""
    contract = Contract.load(settings.contract)
    snapshot = read_snapshot(settings, verify_ref=False)
    problems: list[dict[str, str]] = [
        {"path": item.path, "problem": item.reason} for item in snapshot.quarantined
    ]
    documents = 0
    for file in snapshot.files:
        try:
            chunks = parse_chunks(file.content, file.path, settings, encoder)
            documents += len(build_documents(file, chunks, settings, contract))
        except IngestionError as exc:
            problems.append({"path": file.path, "problem": f"chunking-failed: {exc}"})
            continue
        problems.extend(
            {"path": file.path, "problem": problem}
            for problem in check_invariants(file.content, chunks, settings, encoder)
        )
    return {
        "operation": "check",
        "evidence": "offline-only",
        "publishable": False,
        "source_version_id": settings.commit,
        "counts": {
            "files": len(snapshot.files) + len(snapshot.quarantined),
            "documents": documents,
            "problems": len(problems),
        },
        "problems": problems,
    }
