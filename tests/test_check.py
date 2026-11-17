from dataclasses import replace

from conftest import CONTRACT, ROOT

from ado_markdown_ingestion.check import check_invariants
from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.markdown import parse_chunks
from ado_markdown_ingestion.tokenizer import load_encoder

TEXT = (
    "---\ntitle: T\n---\n\n# Guide\n\nIntro.\n\n## A\n\nBody A.\n\n<!-- note -->\n\n## B\n\n"
    "Body B.\n\n### Deep\n\nDeep body.\n"
)


def test_invariants_hold_for_real_chunks_and_catch_dropped_or_oversize_chunks() -> None:
    config = Settings.from_mapping(
        {
            "source_root": str(ROOT),
            "repo_id": "r",
            "source_ref": "refs/heads/main",
            "commit": "a" * 40,
            "repo_url": "https://dev.azure.com/o/p/_git/r",
            "include": ["**/*.md"],
            "contract": str(CONTRACT),
            "tokenizer_cache": str(ROOT / ".cache" / "tiktoken"),
        },
        ROOT,
    )
    encoder = load_encoder(config.tokenizer_cache)
    chunks = parse_chunks(TEXT, "guide.md", config, encoder)
    assert chunks and check_invariants(TEXT, chunks, config, encoder) == []

    missing = check_invariants(TEXT, chunks[:-1], config, encoder)
    assert len(missing) == 1 and missing[0].startswith("source lines not covered")
    assert "Deep body" not in missing[0]

    oversize = replace(chunks[0], content=chunks[0].content + "x" * 4000)
    assert "chunk exceeds the character budget" in check_invariants(
        TEXT, [oversize, *chunks[1:]], config, encoder
    )
    outside = replace(chunks[0], end_line=999)
    assert "chunk line range is outside the source file" in check_invariants(
        TEXT, [outside], config, encoder
    )
