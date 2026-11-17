"""Adversarial Markdown corpus for the chunking/embedding/indexing review.

Every entry is synthetic. `materialize` writes one file per construct to
`tests/review/fixtures/` so the exact bytes (BOM, CRLF, Windows-1252, NFD) are reviewable and
stable. `test_fixture_files_match_catalog` keeps the files and this catalog in sync.
"""

# ruff: noqa: E501
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


def _long_paragraph(sentences: int, tag: str = "") -> str:
    return " ".join(
        f"Sentence {i}{tag} explains the synthetic escalation rule number {i} in plain words."
        for i in range(sentences)
    )


def _table(rows: int, columns: int = 3, cell: str = "value") -> str:
    head = "| " + " | ".join(f"Col{c}" for c in range(columns)) + " |\n"
    rule = "| " + " | ".join("---" for _ in range(columns)) + " |\n"
    body = "".join(
        "| " + " | ".join(f"{cell}-{r}-{c}" for c in range(columns)) + " |\n" for r in range(rows)
    )
    return head + rule + body


def _text(value: str) -> bytes:
    return value.encode("utf-8")


def _build() -> dict[str, bytes]:
    f: dict[str, bytes] = {}

    # File level and encoding.
    f["file-empty"] = b""
    f["file-whitespace-only"] = _text("   \n\n\t\n  \n")
    f["file-front-matter-only"] = _text("---\ntitle: Only Front Matter\nowner: team\n---\n")
    f["file-bom"] = b"\xef\xbb\xbf" + _text("# Title\n\n## Section\n\nBody after a BOM.\n")
    f["file-crlf"] = _text(
        "# Title\r\n\r\n## Section A\r\n\r\nLine one.\r\nLine two.\r\n\r\n## Section B\r\n\r\nBody B.\r\n"
    )
    f["file-mixed-eol"] = _text(
        "# Title\r\n\r\n## A\n\nCR only\rstill A\r\n\r\n## B\r\n\nBody B.\n"
    )
    f["file-cp1252-french"] = "# Procédure\n\n## Résumé\n\nLe café est prêt à être servi.\n".encode(
        "cp1252"
    )
    f["file-nfd-accents"] = _text(
        "# Proce\u0301dure\n\n## Re\u0301sume\u0301\n\nLe cafe\u0301 est pre\u0302t.\n"
    )
    f["file-nfc-accents"] = _text("# Procédure\n\n## Résumé\n\nLe café est prêt.\n")
    f["file-invisibles"] = _text(
        "# Invisibles\n\n## Zero\u200bwidth\n\n"
        "Zero\u200bwidth\u00a0nbsp\ttab soft\u00adhyphen emoji 😀👍🏽 "
        "and RTL \u0645\u0631\u062d\u0628\u0627 \u05e9\u05dc\u05d5\u05dd text.\n"
    )
    f["file-no-trailing-newline"] = _text("# Title\n\n## Section\n\nLast line without newline")
    f["file-long-line"] = _text("# Long\n\n## Section\n\n" + "word " * 3000 + "\n")
    f["file-long-line-no-spaces"] = _text("# Long\n\n## Section\n\n" + "x" * 12000 + "\n")
    f["file-large-1mb"] = _text(
        "# Large\n\n"
        + "".join(f"## Section {i}\n\n{_long_paragraph(6, f'-{i}')}\n\n" for i in range(1, 2200))
    )
    f["file-500-headings"] = _text(
        "# Many\n\n" + "".join(f"## H{i}\n\nBody {i}.\n\n" for i in range(1, 601))
    )

    # Front matter.
    f["fm-valid"] = _text(
        "---\ntitle: Valid Title\ncategory: policy\nowner: sec-team\nreview_by: 2027-01-01\n"
        "classification: internal\n---\n\n# Heading Title\n\n## Section\n\nBody.\n"
    )
    f["fm-malformed"] = _text(
        "---\ntitle: [unclosed\nowner: : :\n---\n\n# Heading\n\n## S\n\nBody.\n"
    )
    f["fm-empty"] = _text("---\n---\n\n# Heading\n\n## S\n\nBody.\n")
    f["fm-unknown-keys"] = _text(
        "---\ntitle: T\nsource_system: onenote\nsource_id: 42\nsource_url: https://x.example/1\n"
        "weird: yes\n---\n\n# Heading\n\n## S\n\nBody.\n"
    )
    f["fm-toml"] = _text(
        '+++\ntitle = "Toml Title"\nowner = "team"\n+++\n\n# Heading\n\n## S\n\nBody.\n'
    )
    f["fm-thematic-break-top"] = _text(
        "---\n\nIntro paragraph after a thematic break.\n\n# Heading\n\n## S\n\nBody.\n\n---\n\nTail.\n"
    )
    f["fm-title-differs"] = _text(
        "---\ntitle: Front Matter Title\n---\n\n# Different H1\n\n## S\n\nBody.\n"
    )
    f["fm-no-title-anywhere"] = _text("## Only H2\n\nBody without any H1 or title.\n")
    f["fm-crlf"] = _text("---\r\ntitle: CRLF\r\n---\r\n\r\n# H\r\n\r\n## S\r\n\r\nBody.\r\n")
    f["fm-bom"] = b"\xef\xbb\xbf" + _text("---\ntitle: BOM\n---\n\n# H\n\n## S\n\nBody.\n")
    f["fm-hash-and-heading-like"] = _text(
        "---\ntitle: T\n# yaml comment that looks like H1\ndescription: |\n  ## not a heading\n---\n\n"
        "# Real\n\n## S\n\nBody.\n"
    )
    f["fm-dots-terminator"] = _text("---\ntitle: T\n...\n\n# H\n\n## S\n\nBody.\n")

    # Headings.
    f["h-atx-setext"] = _text(
        "Setext One\n==========\n\nUnder one.\n\nSetext Two\n----------\n\nUnder two.\n\n"
        "## ATX closed ##\n\nUnder closed.\n\n### Deeper ###   \n\nUnder deeper.\n"
    )
    f["h-no-space"] = _text("#NoSpace\n\n##AlsoNoSpace\n\nBody that is not under a heading.\n")
    f["h-none"] = _text("Just a paragraph.\n\nAnother paragraph without any heading.\n")
    f["h-preamble"] = _text("Preamble text before any heading.\n\n# Title\n\n## Section\n\nBody.\n")
    f["h-multiple-h1"] = _text("# One\n\nA.\n\n# Two\n\nB.\n\n# Three\n\nC.\n")
    f["h-starts-h2"] = _text("## Starts At H2\n\nBody.\n\n### Then H3\n\nDeeper.\n")
    f["h-starts-h3"] = _text("### Starts At H3\n\nBody.\n\n## Then H2\n\nHigher.\n")
    f["h-skipped-levels"] = _text("# T\n\n## Two\n\nA.\n\n#### Four\n\nB.\n\n## Two b\n\nC.\n")
    f["h-deep-oversize"] = _text(
        "# T\n\n## Two\n\n### Three\n\n"
        + "".join(
            f"{'#' * level} Level {level}\n\n{_long_paragraph(25, f'-L{level}')}\n\n"
            for level in (4, 5, 6)
        )
    )
    f["h-duplicates"] = _text(
        "# T\n\n## Notes\n\nFirst notes.\n\n## Notes\n\nSecond notes.\n\n### Detail\n\nA.\n\n"
        "## Parent\n\n### Detail\n\nB.\n\n### Detail\n\nC.\n"
    )
    f["h-markup"] = _text(
        "# T\n\n## Using [kubectl](https://example.invalid/very/long/url) and `code`\n\nA.\n\n"
        "## **Bold** _italic_ 😀 emoji!\n\nB.\n\n## <span>HTML</span> heading?\n\nC.\n\n"
        "## Q&amp;A \\# escaped &copy; entity\n\nD.\n\n## Image ![alt text](a.png) heading\n\nE.\n"
    )
    f["h-very-long"] = _text("# T\n\n## " + "Very long heading " * 20 + "\n\nBody.\n")
    f["h-in-fence"] = _text(
        "# T\n\n## Real\n\n```md\n## Fake heading in fence\n# Another\n```\n\nAfter.\n"
    )
    f["h-in-html-block"] = _text(
        "# T\n\n## Real\n\n<div>\n## Fake heading in html\n</div>\n\nAfter.\n\n"
        "<pre>\n# Fake in pre\n</pre>\n"
    )
    f["h-empty-and-adjacent"] = _text(
        "# T\n\n##\n\nBody under empty heading.\n\n## A\n## B\n## C\n\nOnly C has body.\n"
    )
    f["h-in-quote-and-list"] = _text(
        "# T\n\n> ## Quoted heading\n> quote body\n\n- ## List heading\n  list body\n\nTail.\n"
    )
    f["h-indented-four"] = _text("# T\n\n    ## indented code, not a heading\n\n## Real\n\nBody.\n")

    # Block content.
    f["code-backtick"] = _text("# T\n\n## S\n\n```python title=x\nprint('a')\n```\n\nAfter.\n")
    f["code-tilde"] = _text("# T\n\n## S\n\n~~~bash\necho hi\n~~~\n\nAfter.\n")
    f["code-nested-fence"] = _text("# T\n\n## S\n\n````md\n```python\nx = 1\n```\n````\n\nAfter.\n")
    f["code-unclosed"] = _text(
        "# T\n\n## S\n\n```python\nx = 1\n\n## Looks like heading\n\nstill code\n"
    )
    f["code-oversize"] = _text(
        "# T\n\n## S\n\n```python\n"
        + "".join(f"value_{i} = compute({i})\n" for i in range(400))
        + "```\n"
    )
    f["code-indented"] = _text(
        "# T\n\n## S\n\nText.\n\n    indented_code()\n    more()\n\nAfter.\n"
    )
    f["table-wide"] = _text("# T\n\n## S\n\n" + _table(5, 40, "w"))
    f["table-oversize"] = _text("# T\n\n## S\n\n" + _table(250, 4, "cell"))
    f["table-escaped-pipes"] = _text(
        "# T\n\n## S\n\n| A | B |\n| --- | --- |\n| a \\| b | c |\n| `x | y` | d |\n"
    )
    f["table-no-edge-pipes"] = _text("# T\n\n## S\n\nA | B\n--- | ---\n1 | 2\n3 | 4\n")
    f["table-alignment-empty-cells"] = _text(
        "# T\n\n## S\n\n| L | C | R |\n|:--|:-:|--:|\n| | x | |\n| a | | c |\n"
    )
    f["html-table"] = _text(
        "# T\n\n## S\n\n<table>\n<tr><th>A</th><th>B</th></tr>\n<tr><td>1</td><td>2</td></tr>\n</table>\n"
    )
    f["html-details"] = _text(
        "# T\n\n## S\n\n<details>\n<summary>Click for the answer</summary>\n\n"
        "The hidden answer is 42.\n\n</details>\n"
    )
    f["html-br-div-inline"] = _text(
        "# T\n\n## S\n\nLine one<br>line two<br/>line three.\n\n"
        '<div class="x">Wrapped <b>bold</b> text</div>\n\n'
        "Inline <kbd>Ctrl</kbd>+<kbd>C</kbd> usage.\n"
    )
    f["list-ordered-start-5"] = _text("# T\n\n## S\n\n5. Fifth\n6. Sixth\n7. Seventh\n")
    f["list-nested-mixed"] = _text(
        "# T\n\n## S\n\n1. Top\n   - sub a\n     1. deep one\n        - deeper\n   - sub b\n2. Next\n"
    )
    f["list-tasks"] = _text("# T\n\n## S\n\n- [x] done item\n- [ ] open item\n")
    f["list-with-code-and-table"] = _text(
        "# T\n\n## S\n\n- Item with code:\n\n  ```sh\n  run --flag\n  ```\n\n- Item with table:\n\n"
        "  | A | B |\n  | - | - |\n  | 1 | 2 |\n"
    )
    f["quote-nested-and-alerts"] = _text(
        "# T\n\n## S\n\n> quote\n>> nested quote\n\n> [!NOTE]\n> Useful note.\n\n"
        "> [!WARNING]\n> Dangerous step.\n"
    )
    f["footnotes"] = _text(
        "# T\n\n## S\n\nClaim with note[^1].\n\n## Other\n\nBody.\n\n"
        "[^1]: The footnote text lives at the end.\n"
    )
    f["ref-links-defs-at-end"] = _text(
        "# T\n\n## S\n\nSee [the policy][pol] and <https://example.invalid/auto>.\n\n## Other\n\n"
        'Body.\n\n[pol]: https://example.invalid/policy "Policy"\n'
    )
    f["images"] = _text(
        "# T\n\n## S\n\n![Alt text](img/a.png)\n\n![](img/no-alt.png)\n\n![Ref alt][r]\n\n"
        '<img src="img/b.png" alt="Html alt">\n\n'
        "![b64](data:image/png;base64," + "A" * 6000 + ")\n\n[r]: img/ref.png\n"
    )
    f["math"] = _text(
        "# T\n\n## S\n\nInline $a^2 + b^2 = c^2$ and block:\n\n$$\n\\int_0^1 x\\,dx = \\frac12\n$$\n"
    )
    f["mermaid-fenced"] = _text("# T\n\n## S\n\n```mermaid\ngraph TD\n  A[Start] --> B[End]\n```\n")
    f["mermaid-ado"] = _text("# T\n\n## S\n\n::: mermaid\ngraph TD\n  A --> B\n:::\n")
    f["hr"] = _text("# T\n\n## S\n\nAbove.\n\n***\n\nBelow.\n\n___\n\nEnd.\n")

    # Azure DevOps wiki syntax.
    f["ado-toc-tosp"] = _text("# T\n\n[[_TOC_]]\n\n[[_TOSP_]]\n\n## S\n\nBody.\n")
    f["ado-container-mention-workitem"] = _text(
        "# T\n\n## S\n\n::: query-table 1234-abcd\n:::\n\n"
        "Ask @<A1B2C3D4-0000-0000-0000-000000000000> about #123.\n\n"
        "See [attachment](/.attachments/spec%20v1.pdf) and ![shot](/.attachments/shot-1.png).\n"
    )
    f["ado-order-file-like"] = _text("# T\n\n## S\n\nhome\nsetup\nfaq\n")
    f["html-comment"] = _text(
        "# T\n\n<!-- hidden editor note: do not publish -->\n\n## S\n\n"
        "Visible <!-- inline note --> text.\n\n"
        "<!--\nmulti-line\ncomment with ## heading\n-->\n\nAfter.\n"
    )

    # OneNote / Confluence style.
    f["onenote-layout-table"] = _text(
        "# T\n\n## S\n\n| | |\n|-|-|\n| **Left column text** | Right column text |\n| | |\n"
    )
    f["onenote-empty-paragraphs"] = _text(
        "# T\n\n## S\n\nPara one.\n\n&nbsp;\n\n\n\n&nbsp;\n\nPara two.\n"
    )
    f["onenote-hard-breaks"] = _text(
        "# T\n\n## S\n\nLine one  \nLine two  \nLine three\\\nLine four\n"
    )
    f["onenote-bold-headings"] = _text(
        "**Procedure overview**\n\nSteps here.\n\n**Escalation contacts**\n\nCall the desk.\n\n"
        "__Rollback__\n\nUndo it.\n"
    )
    f["onenote-deep-lists"] = _text(
        "# T\n\n## S\n\n" + "".join("  " * d + f"- level {d}\n" for d in range(12))
    )

    # Bilingual.
    f["bilingual-mixed"] = _text(
        "# Politique de sécurité / Security Policy\n\n## Correctifs / Patching\n\n"
        "Les vulnérabilités critiques doivent être corrigées dans les 14 jours. "
        "Critical vulnerabilities must be fixed within 14 days.\n"
    )
    f["bilingual-en"] = _text(
        "# Patching Policy\n\n## Deadlines\n\nCritical vulnerabilities must be fixed within 14 days.\n"
    )
    f["bilingual-fr"] = _text(
        "# Politique de correctifs\n\n## Délais\n\n"
        "Les vulnérabilités critiques doivent être corrigées dans les 14 jours.\n"
    )

    # Size handling.
    f["prose-oversize-one-section"] = _text("# T\n\n## S\n\n" + _long_paragraph(150) + "\n")
    f["prose-oversize-paragraphs"] = _text(
        "# T\n\n## S\n\n" + "\n\n".join(_long_paragraph(8, f"-p{i}") for i in range(12)) + "\n"
    )
    f["tiny-sections"] = _text("# T\n\n## A\n\nOne line.\n\n## B\n\nTwo.\n\n## C\n\nThree.\n")
    f["link-split-risk"] = _text(
        "# T\n\n## S\n\n"
        + "filler words " * 230
        + "[link text that must not be cut]("
        + "https://example.invalid/"
        + "p" * 300
        + ") trailing.\n"
    )
    f["combining-marks-oversize"] = _text("# T\n\n## S\n\n" + "e\u0301" * 3000 + "\n")
    f["table-long-row"] = _text(
        "# T\n\n## S\n\n| A | B |\n| - | - |\n| " + "long cell text " * 400 + " | x |\n"
    )
    f["table-header-exceeds-cap"] = _text(
        "# T\n\n## S\n\n| "
        + " | ".join(f"Column name number {i} xxxxxxxxxx" for i in range(200))
        + " |\n| "
        + " | ".join("---" for _ in range(200))
        + " |\n| "
        + " | ".join("v" for _ in range(200))
        + " |\n"
    )
    f["h-gigantic-5000"] = _text("# T\n\n## " + "H" * 5000 + "\n\nBody.\n")
    f["html-comment-heading"] = _text(
        "# T\n\n## S\n\nBefore.\n\n<!--\n## Hidden heading\nhidden editor text\n-->\n\nAfter.\n"
    )
    f["h-html-block-heading"] = _text(
        "# T\n\n## Real\n\n<details>\n<summary>More</summary>\n## Not a section\n\nInside.\n\n"
        "</details>\n\n<div>\n## Also not a section\n</div>\n"
    )
    f["h4-under-h3-small"] = _text(
        "# T\n\n## Two\n\n### Three\n\nIntro.\n\n#### Four a\n\nA.\n\n#### Four b\n\nB.\n"
    )
    f["ref-definition-same-section-far"] = _text(
        "# T\n\n## S\n\nSee [policy][pol].\n\n## Other\n\nText.\n\n[pol]: https://example.invalid/p\n"
    )
    f["near-duplicate-old"] = _text(
        "# Vulnerability Handling (2021)\n\n## Deadlines\n\n"
        "Critical vulnerabilities must be remediated within 30 days of disclosure.\n"
    )
    f["near-duplicate-new"] = _text(
        "# Vulnerability Handling (2024)\n\n## Deadlines\n\n"
        "Critical vulnerabilities must be remediated within 14 days of disclosure. "
        "This replaces the 30 day rule of the 2021 policy.\n"
    )
    return f


FIXTURES: dict[str, bytes] = _build()

# Repository-relative paths for identity and citation checks (content is irrelevant).
PATH_FIXTURES = [
    "docs/with space.md",
    "docs/café.md",
    "docs/wiki%2Dstyle%2Dname.md",
    "docs/a/readme.md",
    "docs/b/readme.md",
    "Docs/Readme.md",
    "docs/" + "very-long-segment-" * 20 + ".md",
]


def materialize(directory: Path = FIXTURE_DIR) -> None:
    """Write `<name>.md.txt` so linters that now format `.md` files leave the bytes alone."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in FIXTURES.items():
        (directory / f"{name}.md.txt").write_bytes(data)


if __name__ == "__main__":
    materialize()
