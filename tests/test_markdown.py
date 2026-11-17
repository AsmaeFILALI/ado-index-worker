from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ROOT, GitRepo

from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.markdown import parse_chunks
from ado_markdown_ingestion.tokenizer import load_encoder


def test_headings_fences_lists_tables_and_empty_sections(repo: GitRepo) -> None:
    text = (
        "# Field Guide\n\n## Empty\n\n## Steps\n"
        "- Prepare the synthetic kit\n- Read the gauge\n\n"
        "```python\n# This is code, not a heading\nprint('synthetic')\n```\n\n"
        "| Item | State |\n| --- | --- |\n| Sample | Ready |\n\n"
        "## Steps\nA different repeated section.\n"
    )
    chunks = parse_chunks(text, "guide.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
    assert len(chunks) == 2
    assert all(c.title == "Field Guide" and c.heading_path == "Field Guide > Steps" for c in chunks)
    assert chunks[0].section_identity != chunks[1].section_identity
    assert "# This is code" in chunks[0].content
    assert "| Sample | Ready |" in chunks[0].content
    assert "- Prepare" in chunks[0].content
    assert chunks[0].start_line == 6
    assert chunks[1].end_line == len(text.splitlines())


@pytest.mark.parametrize("text", ["", " \n\n", "# Header only\n\n## Child\n", "\r\n"])
def test_empty_and_heading_only_emit_no_chunks(repo: GitRepo, text: str) -> None:
    assert (
        parse_chunks(text, "empty.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
        == []
    )


def test_preamble_setext_and_crlf(repo: GitRepo) -> None:
    text = "Preamble é.\r\n\r\nHeading\r\n=======\r\nBody 😀.\r\n"
    chunks = parse_chunks(
        text, "fallback.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken")
    )
    assert [c.start_line for c in chunks] == [1, 5]
    assert all(c.title == "Heading" for c in chunks)
    assert "\r" not in "".join(c.content for c in chunks)


def test_yaml_front_matter_is_removed_and_supplies_the_title(repo: GitRepo) -> None:
    text = "---\ntitle: My Page\nowner: bob\n---\n\n# Real\n\nbody\n"
    chunks = parse_chunks(text, "fm.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
    assert [c.heading_path for c in chunks] == ["Real"]
    assert "owner" not in chunks[0].content and "---" not in chunks[0].content
    assert chunks[0].title == "My Page"
    assert chunks[0].content == "My Page > Real\n\nbody\n"
    assert chunks[0].start_line == 8 == chunks[0].end_line


def test_front_matter_only_file_has_no_chunks(repo: GitRepo) -> None:
    text = "---\ntitle: Only\n---\n"
    assert (
        parse_chunks(text, "fm.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken")) == []
    )


def test_unclosed_front_matter_marker_is_ordinary_markdown(repo: GitRepo) -> None:
    text = "---\n\n# Real\n\nbody\n"
    chunks = parse_chunks(text, "fm.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
    assert [c.heading_path for c in chunks] == ["", "Real"]


def test_heading_image_alt_text_is_kept(repo: GitRepo) -> None:
    text = "# Guide\n\n## Logo ![Company mark](x.png) usage\n\nbody\n"
    chunks = parse_chunks(text, "img.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
    assert chunks[0].heading_path == "Guide > Logo Company mark usage"


@pytest.mark.parametrize("separator", ["\x0c", "\x0b", "\x1c", "\x85", "\u2028", "\u2029"])
def test_non_lf_line_separators_do_not_shift_sections(repo: GitRepo, separator: str) -> None:
    text = f"# T\n\n## A\n\nfoo{separator}bar\n\n## B\n\nbody B\n\n## C\n\nbody C\n"
    chunks = parse_chunks(text, "sep.md", repo.settings(), load_encoder(ROOT / ".cache/tiktoken"))
    assert [c.heading_path for c in chunks] == ["T > A", "T > B", "T > C"]
    assert f"foo{separator}bar" in chunks[0].content
    assert all("## " not in c.content for c in chunks)
    assert [c.start_line for c in chunks] == [5, 9, 13]


def test_long_unicode_single_line_and_overlap_are_bounded(repo: GitRepo) -> None:
    encoder = load_encoder(ROOT / ".cache/tiktoken")
    settings = replace(repo.settings(), target_tokens=100, max_content_chars=220, overlap_tokens=12)
    text = "# Synthetic\n" + ("Gauge é😀 measurement. " * 400)
    chunks = parse_chunks(text, "long.md", settings, encoder)
    assert len(chunks) > 10
    assert [c.part for c in chunks] == list(range(1, len(chunks) + 1))
    for chunk in chunks:
        assert encoder.count(chunk.content) <= settings.target_tokens
        assert len(chunk.content) <= settings.max_content_chars
        assert chunk.start_line == chunk.end_line == 2
        assert chunk.part_count == len(chunks)
        assert "\ufffd" not in chunk.content
    assert chunks == parse_chunks(text, "long.md", settings, encoder)


@pytest.mark.parametrize("kind", ["code", "table", "paragraph"])
def test_large_blocks_preserve_source_lines_and_bounds(repo: GitRepo, kind: str) -> None:
    encoder = load_encoder(ROOT / ".cache/tiktoken")
    settings = replace(repo.settings(), target_tokens=90, max_content_chars=200)
    body = "".join(f"sample_{i} = 'synthetic reading'\n" for i in range(60))
    if kind == "code":
        body = "```python\n" + body + "```\n"
    elif kind == "table":
        body = "| Item | Reading |\n| --- | --- |\n" + "".join(
            f"| synthetic {i} | nominal |\n" for i in range(60)
        )
    text = "# Guide\n" + body
    chunks = parse_chunks(text, "blocks.md", settings, encoder)
    assert len(chunks) > 1
    lines = text.splitlines()
    for chunk in chunks:
        assert 1 <= chunk.start_line <= chunk.end_line <= len(lines)
        assert len(chunk.content) <= 200 and encoder.count(chunk.content) <= 90
        if kind == "code":
            assert chunk.content.count("```") == 2
        if kind == "table":
            assert "| Item | Reading |" in chunk.content
    assert chunks[0].start_line == 2
    assert chunks[-1].end_line == len(lines)


def test_prefix_cannot_be_silently_truncated(repo: GitRepo) -> None:
    with pytest.raises(IngestionError, match="budget"):
        parse_chunks(
            "# " + "LongTitle" * 100 + "\nbody",
            "long.md",
            replace(repo.settings(), max_content_chars=80),
            load_encoder(ROOT / ".cache/tiktoken"),
        )


def test_missing_or_corrupt_tokenizer_never_downloads(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="prepare-tokenizer"):
        load_encoder(tmp_path)
    (tmp_path / "cl100k_base.tiktoken").write_bytes(b"corrupt")
    with pytest.raises(IngestionError, match="integrity"):
        load_encoder(tmp_path)


def test_tokenizer_matches_known_encoding_values() -> None:
    encoder = load_encoder(ROOT / ".cache/tiktoken")
    assert encoder.count("hello world") == 2
    assert encoder.encoding.n_vocab == 100277
    text = "é 😀 <|endoftext|>"
    assert encoder.encoding.decode(encoder.encoding.encode(text, disallowed_special=())) == text


def test_nested_empty_and_unclosed_fence_sections(repo: GitRepo) -> None:
    encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    chunks = parse_chunks(
        "#\nBody before a nonempty heading.\n## Parent\n### Child\n"
        "```text\n# Fenced, not structural\n",
        "fallback.md",
        repo.settings(),
        encoder,
    )
    assert chunks[0].title == "fallback"
    assert chunks[1].heading_path == "Parent > Child"
    assert chunks[1].content.count("```") == 2
    assert chunks[1].start_line == 5 and chunks[1].end_line == 6


def test_closed_quoted_fences_do_not_gain_spurious_closers(repo: GitRepo) -> None:
    encoder = load_encoder(ROOT / ".cache" / "tiktoken")
    for prefix in ("> ", "> > ", "  "):
        body = prefix + "```python\n" + prefix + "# Not a heading\n" + prefix + "```\n"
        chunks = parse_chunks("# Guide\n" + body, "quoted.md", repo.settings(), encoder)
        assert len(chunks) == 1
        assert chunks[0].content.count("```") == 2
        assert chunks[0].content.endswith(body)
