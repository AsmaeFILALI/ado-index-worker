"""Run every review fixture through the real chunker and record the observable output.

Usage (from the repository root): uv run --locked python -m tests.review.run_review
Writes tests/review/output/matrix.json (per-fixture chunks) and prints the size distribution.
"""

import json
import statistics
from pathlib import Path

from .fixtures_catalog import FIXTURE_DIR, FIXTURES, materialize
from .review_support import ROOT, coverage, measure, run_bytes

OUTPUT = Path(__file__).resolve().parent / "output"


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def main() -> None:
    materialize(FIXTURE_DIR)
    OUTPUT.mkdir(exist_ok=True)
    matrix: dict[str, object] = {}
    token_sizes: list[int] = []
    char_sizes: list[int] = []
    for name, raw in FIXTURES.items():
        run = run_bytes(raw, name)
        rows = []
        for chunk in run.chunks:
            chars, tokens = measure(chunk)
            token_sizes.append(tokens)
            char_sizes.append(chars)
            rows.append(
                {
                    "heading_path": chunk.heading_path,
                    "title": chunk.title,
                    "start": chunk.start_line,
                    "end": chunk.end_line,
                    "part": f"{chunk.part}/{chunk.part_count}",
                    "chars": chars,
                    "tokens": tokens,
                    "head": chunk.content[:160],
                }
            )
        uncovered, claimed = coverage(run)
        matrix[name] = {
            "error": run.error,
            "chunks": len(run.chunks),
            "uncovered_lines": sorted(uncovered),
            "multiply_claimed_lines": sorted(n for n, c in claimed.items() if c > 1),
            "rows": rows if len(rows) <= 40 else rows[:5] + [{"...": len(rows) - 10}] + rows[-5:],
        }
    (OUTPUT / "matrix.json").write_text(
        json.dumps(matrix, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print("fixtures", len(FIXTURES), "chunks", len(token_sizes))
    report("all fixtures", token_sizes, char_sizes)
    small = [
        measure(chunk)
        for name, raw in FIXTURES.items()
        if name not in BULK
        for chunk in run_bytes(raw, name).chunks
    ]
    report("fixtures without the two bulk files", [t for _, t in small], [c for c, _ in small])
    corpus: list[tuple[int, int]] = []
    files = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    chunks_per_file: list[int] = []
    for path in files:
        run = run_bytes(path.read_bytes(), path.name, f"docs/{path.name}")
        chunks_per_file.append(len(run.chunks))
        corpus.extend(measure(chunk) for chunk in run.chunks)
    print("repository docs corpus:", len(files), "files,", sum(chunks_per_file), "chunks")
    report(
        "repository docs (realistic prose/tables)", [t for _, t in corpus], [c for c, _ in corpus]
    )
    print("average chunks per file:", round(sum(chunks_per_file) / len(files), 1))


def report(label: str, tokens: list[int], chars: list[int]) -> None:
    print(f"-- {label}: {len(tokens)} chunks, {sum(tokens)} tokens in total")
    for unit, values in (("tokens", tokens), ("chars", chars)):
        print(
            f"   {unit}: min {min(values)} median {int(statistics.median(values))} "
            f"p95 {percentile(values, 0.95)} max {max(values)}"
        )
    print("   chunks under 50 tokens:", sum(1 for value in tokens if value < 50))


BULK = {"file-large-1mb", "file-500-headings"}


if __name__ == "__main__":
    main()
