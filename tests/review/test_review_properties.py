"""Property checks that must hold for every fixture in the adversarial corpus."""

import json
import re
import unicodedata
from functools import cache

import pytest

from ado_markdown_ingestion.check import check_invariants

from .fixtures_catalog import FIXTURE_DIR, FIXTURES
from .review_support import (
    DESIGN_MAX_CHARS,
    DESIGN_MAX_TOKENS,
    ROOT,
    Run,
    coverage,
    deployed,
    design,
    drafts,
    encoder,
    normalize,
    run_bytes,
    squash,
)

ALL = sorted(FIXTURES)
SOURCE_READ_REJECTED = {"file-cp1252-french"}
KEY = re.compile(r"[A-Za-z0-9_=-]{1,1024}")
DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:")


@cache
def result(name: str, budget: str = "deployed") -> Run:
    return run_bytes(FIXTURES[name], name, config=design() if budget == "design" else deployed())


@pytest.mark.parametrize("name", ALL)
def test_fixture_is_chunked_without_aborting(name: str) -> None:
    run = result(name)
    if name in SOURCE_READ_REJECTED:
        pytest.skip("rejected at source read; the snapshot quarantines it (see pipeline tests)")
    assert run.error is None, run.error


@pytest.mark.parametrize("name", ALL)
def test_chunks_fit_the_agreed_design_budget(name: str) -> None:
    run = result(name, "design")
    if run.error:
        pytest.skip(run.error)
    for chunk in run.chunks:
        assert len(chunk.content) <= DESIGN_MAX_CHARS
        assert encoder().count(chunk.content) <= DESIGN_MAX_TOKENS


@pytest.mark.parametrize("name", ALL)
def test_chunking_is_deterministic_and_ids_are_valid_search_keys(name: str) -> None:
    run = result(name)
    if run.error:
        pytest.skip(run.error)
    again = run_bytes(FIXTURES[name], name)
    assert again.chunks == run.chunks
    documents = drafts(run)
    keys = [document.id for document in documents]
    assert len(set(keys)) == len(keys)
    assert all(KEY.fullmatch(key) for key in keys)
    assert all(document.content.strip() for document in documents)


@pytest.mark.parametrize("name", ALL)
def test_every_source_line_lands_in_a_chunk(name: str) -> None:
    run = result(name)
    if run.error:
        pytest.skip(run.error)
    uncovered, _ = coverage(run)
    assert not uncovered, f"{name}: source lines dropped {sorted(uncovered)}"


def comparable(body: str, cited: str) -> str:
    """Drop generated additions: carried definitions and a synthetic closing fence."""
    body_lines = body.rstrip("\n").split("\n")
    cited_lines = set(cited.split("\n"))
    while (
        body_lines
        and body_lines[-1] not in cited_lines
        and (DEFINITION.match(body_lines[-1]) or body_lines[-1].strip() in {"```", "~~~"})
    ):
        body_lines.pop()
    return "\n".join(body_lines)


@pytest.mark.parametrize("name", ALL)
def test_runtime_invariants_hold_for_every_fixture(name: str) -> None:
    run = result(name)
    if run.error:
        pytest.skip(run.error)
    assert check_invariants(run.text, run.chunks, deployed(), encoder()) == []


@pytest.mark.parametrize("name", ALL)
def test_unsplit_chunk_body_equals_the_cited_source_lines(name: str) -> None:
    run = result(name)
    if run.error:
        pytest.skip(run.error)
    lines = run.lines
    for chunk, body in zip(run.chunks, run.bodies(), strict=True):
        assert 1 <= chunk.start_line <= chunk.end_line <= len(lines)
        if chunk.part_count == 1:
            cited = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
            assert squash(comparable(body, cited)) == squash(cited), (
                chunk.heading_path,
                chunk.start_line,
            )


def test_normalization_matches_the_documented_subset() -> None:
    assert normalize("\ufeffa\r\nb\rc\n") == "a\nb\nc\n"
    assert unicodedata.is_normalized("NFC", "é")


def test_fixture_files_match_catalog() -> None:
    # Git may rewrite line endings on checkout; everything else must match byte for byte.
    def unified(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    on_disk = {
        path.name.removesuffix(".md.txt"): unified(path.read_bytes())
        for path in FIXTURE_DIR.glob("*")
    }
    expected = {name: unified(data) for name, data in FIXTURES.items()}
    assert on_disk == expected, "run: python -m tests.review.run_review"


def test_proposed_retrieval_queries_point_at_real_fixture_text() -> None:
    queries = json.loads((ROOT / "tests" / "review" / "retrieval-queries.json").read_text("utf-8"))
    assert queries["evidence"] == "proposed-not-run"
    for case in queries["cases"]:
        source = FIXTURES[case["expected_fixture"]].decode("utf-8")
        assert case["expected_text"] in source, case["id"]
