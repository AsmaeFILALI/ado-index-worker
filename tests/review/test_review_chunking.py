"""Chunking regression tests built from the adversarial review fixtures.

Every test documents behaviour agreed in the design. A few `xfail(strict=True)` marks remain for
open findings; they turn into errors once fixed.
"""

import re
import unicodedata

import pytest

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import MIN_SECTION_TOKENS

from .fixtures_catalog import FIXTURES
from .review_support import (
    ROOT,
    Run,
    deployed,
    drafts,
    encoder,
    run_bytes,
    run_text,
    settings,
)


def named(name: str) -> Run:
    return run_bytes(FIXTURES[name], name, config=deployed())


def heading_paths(run: Run) -> list[str]:
    return [chunk.heading_path for chunk in run.chunks]


# ---------------------------------------------------------------- boundaries and parsing


def test_thematic_break_at_top_is_not_front_matter() -> None:
    assert "Heading > S" in heading_paths(named("fm-thematic-break-top"))


@pytest.mark.parametrize(
    "text",
    [
        "---\ntitle: T\n---\n\n# H\n\nb\n",
        "---\n---\n\n# H\n\nb\n",
        "---\ntags:\n  - a\n  - b\n# comment\ndescription: |\n  text\n---\n\n# H\n\nb\n",
        '---\n"quoted key": v\n---\n\n# H\n\nb\n',
    ],
)
def test_real_front_matter_shapes_are_removed(text: str) -> None:
    run = run_text(text)
    assert heading_paths(run) == ["H"]
    assert "---" not in run.chunks[0].content


def test_prose_between_two_rules_is_not_front_matter() -> None:
    run = run_text("---\nSome intro: with a colon later\n\n# H\n\nb\n\n---\n\nTail\n")
    assert "H" in heading_paths(run)


@pytest.mark.parametrize(
    ("name", "fake"),
    [
        ("h-in-html-block", "Fake"),
        ("h-html-block-heading", "section"),
        ("html-comment-heading", "Hidden"),
    ],
)
def test_heading_like_lines_in_html_blocks_and_comments_never_split(name: str, fake: str) -> None:
    assert not [path for path in heading_paths(named(name)) if fake in path]


def test_heading_like_lines_in_fences_front_matter_and_indented_code_never_split() -> None:
    assert heading_paths(named("h-in-fence")) == ["T > Real"]
    assert heading_paths(named("fm-hash-and-heading-like")) == ["Real > S"]
    assert "    ## indented code, not a heading" in named("h-indented-four").chunks[0].content
    assert not [p for p in heading_paths(named("code-unclosed")) if "Looks like" in p]


def test_heading_inside_blockquote_or_list_currently_splits() -> None:
    # Documented behaviour: the AST reports these as headings. Low risk, confirm with owners.
    assert heading_paths(named("h-in-quote-and-list")) == ["T > Quoted heading", "T > List heading"]


def test_setext_atx_closing_hashes_skipped_levels_and_duplicates() -> None:
    paths = heading_paths(named("h-atx-setext"))
    assert paths[0] == "Setext One" and "Setext One > ATX closed > Deeper" in paths
    assert heading_paths(named("h-skipped-levels")) == ["T > Two", "T > Two b"]
    documents = drafts(named("h-duplicates"))
    assert len({d.section_id for d in documents}) == len(documents)
    assert len({d.id for d in documents}) == len(documents)


def test_no_heading_and_preamble_documents_still_produce_chunks() -> None:
    assert heading_paths(named("h-none")) == [""]
    assert named("h-none").chunks[0].content.startswith("fixture\n\nJust a paragraph.")
    assert heading_paths(named("h-preamble")) == ["", "Title > Section"]
    assert heading_paths(named("onenote-bold-headings")) == [""]  # bold paragraphs never split


def test_empty_and_adjacent_headings_emit_no_empty_chunks() -> None:
    assert heading_paths(named("h-empty-and-adjacent")) == ["T", "T > C"]
    assert named("file-empty").chunks == []
    assert named("file-whitespace-only").chunks == []


def test_h4_to_h6_stay_inside_their_h3_section() -> None:
    run = named("h4-under-h3-small")
    assert len(run.chunks) == 1
    content = run.chunks[0].content
    assert "A." in content and "B." in content and "#### Four a" in content


def test_oversize_deep_headings_split_at_the_next_heading_level_first() -> None:
    run = named("h-deep-oversize")
    assert len(run.chunks) > 1
    assert all(re.search(r"^#{4,6} Level", c.content.split("\n\n", 1)[1]) for c in run.chunks[1:])


def test_small_child_sections_merge_into_their_parent_chunk() -> None:
    text = "# T\n\n## Parent\n\nIntro text.\n\n### One\n\nShort.\n\n### Two\n\nAlso short.\n"
    run = run_text(text)
    assert heading_paths(run) == ["T > Parent"]
    assert "### One" in run.chunks[0].content and "Also short." in run.chunks[0].content
    assert (run.chunks[0].start_line, run.chunks[0].end_line) == (5, 13)


def test_small_siblings_are_not_merged_and_large_children_stay_separate() -> None:
    assert len(named("tiny-sections").chunks) == 3
    big = "word " * (MIN_SECTION_TOKENS + 20)
    run = run_text(f"# T\n\n## P\n\nIntro.\n\n### Big\n\n{big}\n")
    assert heading_paths(run) == ["T > P", "T > P > Big"]


# ---------------------------------------------------------------- front matter and title


@pytest.mark.parametrize(
    "name", ["fm-valid", "fm-unknown-keys", "fm-empty", "file-front-matter-only"]
)
def test_front_matter_is_removed_from_indexed_content(name: str) -> None:
    run = named(name)
    assert not any("title:" in c.content or "owner:" in c.content for c in run.chunks)
    assert not any("---" in c.content for c in run.chunks)


def test_front_matter_title_wins_over_h1_and_file_stem() -> None:
    assert run_text("---\ntitle: FM Title\n---\n\n## S\n\nBody.\n").chunks[0].title == "FM Title"
    assert named("fm-title-differs").chunks[0].title == "Front Matter Title"
    assert named("fm-no-title-anywhere").chunks[0].title == "fixture"
    assert named("fm-valid").chunks[0].title == "Valid Title"


@pytest.mark.parametrize(
    ("raw", "title"),
    [
        ('title: "Quoted: yes"', "Quoted: yes"),
        ("title: 'Single'", "Single"),
        ("title: Plain # comment", "Plain"),
        ("title: |", "H"),
        ("title:", "H"),
    ],
)
def test_front_matter_title_forms(raw: str, title: str) -> None:
    assert run_text(f"---\n{raw}\n---\n\n# H\n\n## S\n\nBody.\n").chunks[0].title == title


def test_line_numbers_still_refer_to_the_original_file() -> None:
    assert [c.start_line for c in named("fm-crlf").chunks] == [9]
    assert [c.start_line for c in named("fm-bom").chunks] == [9]
    assert [c.start_line for c in named("file-crlf").chunks] == [5, 10]
    assert [c.start_line for c in named("fm-valid").chunks] == [13]


# ---------------------------------------------------------------- normalization


def test_content_and_heading_path_are_nfc() -> None:
    for chunk in named("file-nfd-accents").chunks:
        assert unicodedata.is_normalized("NFC", chunk.content)
        assert unicodedata.is_normalized("NFC", chunk.heading_path)
        assert unicodedata.is_normalized("NFC", chunk.title)


def test_html_comments_are_removed_without_shifting_line_numbers() -> None:
    run = named("html-comment")
    assert not any(
        "hidden editor note" in c.content or "inline note" in c.content for c in run.chunks
    )
    assert not any("<!--" in c.content for c in run.chunks)
    after = run_text("# T\n\n## S\n\nA.\n\n<!--\nmulti\nline\n-->\n\nB.\n\n## U\n\nC.\n")
    assert [c.start_line for c in after.chunks] == [5, 16]


def test_comment_markers_inside_code_are_kept() -> None:
    run = run_text("# T\n\n## S\n\n```html\n<!-- keep me -->\n```\n")
    assert "<!-- keep me -->" in run.chunks[0].content


def test_wiki_toc_macro_is_removed_but_tosp_is_kept() -> None:
    content = " ".join(c.content for c in named("ado-toc-tosp").chunks)
    assert "[[_TOC_]]" not in content and "[[_TOSP_]]" in content


def test_bom_and_line_endings_are_normalized() -> None:
    assert "\ufeff" not in "".join(c.content for c in named("file-bom").chunks)
    assert "\r" not in "".join(c.content for c in named("file-mixed-eol").chunks)


# ---------------------------------------------------------------- breadcrumb and budgets


def test_content_starts_with_a_single_breadcrumb_line() -> None:
    chunk = named("h-skipped-levels").chunks[0]
    assert chunk.content.split("\n", 1)[0] == "T > Two"
    assert chunk.content.split("\n", 2)[1] == ""
    assert not chunk.content.startswith("Title:")


def test_breadcrumb_does_not_repeat_a_title_that_is_the_first_heading() -> None:
    assert named("h-multiple-h1").chunks[0].content.startswith("One\n\n")
    assert named("h-multiple-h1").chunks[1].content.startswith("One > Two\n\n")
    assert (
        named("fm-title-differs").chunks[0].content.startswith("Front Matter Title > Different H1")
    )


def test_default_character_cap_matches_the_agreed_design() -> None:
    assert (deployed().max_content_chars, deployed().target_tokens) == (3800, 900)


@pytest.mark.parametrize("override", [{"max_content_chars": 3801}, {"target_tokens": 901}])
def test_budgets_above_the_agreed_cap_are_rejected(override: dict[str, int]) -> None:
    with pytest.raises(IngestionError):
        settings(**override)


def test_the_cap_includes_breadcrumb_and_code_fences_and_table_headers() -> None:
    cfg = deployed()
    for name in ("code-oversize", "table-oversize", "table-wide"):
        for chunk in named(name).chunks:
            assert len(chunk.content) <= cfg.max_content_chars
            assert encoder().count(chunk.content) <= cfg.target_tokens


def test_oversize_code_is_refenced_and_tables_repeat_their_header() -> None:
    for chunk in named("code-oversize").chunks:
        assert chunk.content.count("```") == 2
        assert "```python\n" in chunk.content
    for chunk in named("table-oversize").chunks:
        assert "| Col0 | Col1 | Col2 | Col3 |\n| --- | --- | --- | --- |\n" in chunk.content


def test_legacy_token_limits_are_gone_from_the_chunker() -> None:
    source = (ROOT / "src" / "ado_markdown_ingestion" / "markdown.py").read_text(encoding="utf-8")
    config = (ROOT / "src" / "ado_markdown_ingestion" / "config.py").read_text(encoding="utf-8")
    for legacy in ("700", "1200", "1_200", "800 "):
        assert legacy not in source
        assert f"= {legacy}" not in config


def test_oversize_headings_and_wide_table_headers_no_longer_abort() -> None:
    giant = named("h-gigantic-5000")
    assert giant.error is None and len(giant.chunks[0].content.split("\n", 1)[0]) <= 300
    wide = named("table-header-exceeds-cap")
    assert wide.error is None and wide.chunks
    assert all(len(c.content) <= 3800 for c in wide.chunks)


# ---------------------------------------------------------------- fallback order and splits


def test_oversize_prose_splits_on_sentence_boundaries() -> None:
    run = named("prose-oversize-one-section")
    assert len(run.chunks) > 1
    assert all(body.startswith("Sentence") for body in run.bodies())
    assert all(body.rstrip().endswith(".") for body in run.bodies())


def test_a_split_never_cuts_through_a_link() -> None:
    full = "[link text that must not be cut](https://example.invalid/" + "p" * 300 + ")"
    assert any(full in body for body in named("link-split-risk").bodies())


def test_a_split_never_separates_a_combining_mark() -> None:
    for body in named("combining-marks-oversize").bodies():
        assert not unicodedata.combining(body[0])


def test_an_oversize_table_row_is_cut_on_whitespace_only() -> None:
    words = {"|", "A", "B", "-", "long", "cell", "text", "x"}
    for body in named("table-long-row").bodies():
        assert set(body.split()) <= words


def test_block_boundaries_are_preferred_over_character_cuts() -> None:
    run = named("prose-oversize-paragraphs")
    assert len(run.chunks) > 1
    assert all(body.startswith("Sentence 0-p") for body in run.bodies())


def test_very_long_single_lines_are_bounded_and_marked_as_parts() -> None:
    for name in ("file-long-line", "file-long-line-no-spaces"):
        run = named(name)
        assert [c.part for c in run.chunks] == list(range(1, len(run.chunks) + 1))
        assert {c.part_count for c in run.chunks} == {len(run.chunks)}
        assert all(len(c.content) <= 3800 for c in run.chunks)


# ---------------------------------------------------------------- references and plain text


@pytest.mark.parametrize(
    ("name", "marker", "definition"),
    [
        ("ref-links-defs-at-end", "][pol]", "[pol]:"),
        ("ref-definition-same-section-far", "][pol]", "[pol]:"),
        ("footnotes", "[^1]", "[^1]:"),
    ],
)
def test_references_resolve_within_their_chunk(name: str, marker: str, definition: str) -> None:
    chunks = [c for c in named(name).chunks if marker in c.content]
    assert chunks
    for chunk in chunks:
        assert definition in chunk.content


def test_definitions_inside_code_are_not_carried_and_own_chunk_is_not_duplicated() -> None:
    run = run_text("# T\n\n## S\n\nSee [a].\n\n```\n[a]: https://code.invalid\n```\n")
    assert run.chunks[0].content.count("[a]:") == 1
    own = named("ref-links-defs-at-end").chunks[-1].content
    assert own.count("[pol]:") == 1


def test_heading_path_is_plain_text() -> None:
    assert not [p for p in heading_paths(named("h-markup")) if re.search(r"</?[a-z]+>", p)]
    assert "\u200b" not in named("file-invisibles").chunks[0].heading_path


# ---------------------------------------------------------------- identity


def test_editing_one_section_does_not_change_other_sections_ids() -> None:
    before = run_text("# T\n\n## A\n\na\n\n## B\n\nb\n")
    after = run_text("# T\n\n## New\n\nn\n\n## A\n\na\n\n## B\n\nb\n")
    old = {d.section_id: d.chunk_id for d in drafts(before)}
    new = {d.section_id: d.chunk_id for d in drafts(after)}
    assert all(new[key] == value for key, value in old.items())
    assert all(0 <= value < 2**31 for value in new.values())


def test_document_ids_survive_text_edits_and_unrelated_insertions() -> None:
    before = {d.section_id: d.id for d in drafts(run_text("# T\n\n## A\n\na\n\n## B\n\nb\n"))}
    after = {d.section_id: d.id for d in drafts(run_text("# T\n\n## A\n\na edited\n\n## B\n\nb\n"))}
    assert before == after
    inserted = {
        d.section_id: d.id
        for d in drafts(run_text("# T\n\n## New\n\nn\n\n## A\n\na\n\n## B\n\nb\n"))
    }
    assert all(inserted[key] == value for key, value in before.items())
