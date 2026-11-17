import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from markdown_it import MarkdownIt
from markdown_it.token import Token

from ado_markdown_ingestion.config import Settings
from ado_markdown_ingestion.errors import IngestionError
from ado_markdown_ingestion.tokenizer import Encoder

# Sections are H1-H3. Deeper headings stay in the body of their section.
BOUNDARY_LEVEL = 3
# A subsection below this size is merged into its parent chunk when the result still fits.
MIN_SECTION_TOKENS = 80
TITLE_LIMIT = 200
LABEL_LIMIT = 200
BREADCRUMB_LIMIT = 300
# Reference/footnote definitions carried into a chunk that uses them (characters).
FOOTER_LIMIT = 600
# Allowed removals only: HTML comments and the wiki table-of-contents macro.
_REMOVALS = re.compile(r"<!--.*?-->|\[\[_TOC_\]\]", re.DOTALL)
_INVISIBLE = {ord(char): None for char in "\u200b\u2060\ufeff\u00ad"}
_SENTENCE_END = re.compile(r"""[.!?\u2026\u3002\uff01\uff1f]["'\u201d\u2019)\]]*\s+""")
_WHITESPACE = re.compile(r"\s+")
_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\n]*\)")
_BRACKET = re.compile(r"\[([^\]\n]+)\]")
_DEFINITION = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*\S")
_YAML_LINE = re.compile(
    r"""(?:[ \t]+\S.*|[ \t]*#.*|-(?:[ \t].*)?|[^\s#:'"-][^:]*:(?:[ \t].*)?|["'].*)\Z"""
)
_YAML_KEY = re.compile(r"""[^\s#:'"-][^:]*:|["'].*:""")
_TITLE_KEY = re.compile(r"""title[ \t]*:[ \t]*(.*?)[ \t]*\Z""")


@dataclass(frozen=True)
class Chunk:
    title: str
    heading_path: str
    section_identity: tuple[str, ...]
    content: str
    start_line: int
    end_line: int
    part: int
    part_count: int


@dataclass(frozen=True)
class _Block:
    kind: str
    start: int
    end: int
    body_start: int
    closing: int
    opening_text: str
    closing_text: str


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "\u2026"


def _label(token: Token) -> str:
    text = (
        "".join(
            child.content
            if child.type in {"text", "code_inline"}
            else child.content
            if child.type == "image"
            else " "
            if child.type in {"softbreak", "hardbreak"}
            else ""
            for child in (token.children or [])
        ).strip()
        or token.content.strip()
    )
    return _clip(text.translate(_INVISIBLE), LABEL_LIMIT)


def _looks_like_yaml_mapping(block: list[str]) -> bool:
    """Cheap structural check so a leading thematic break is not mistaken for front matter."""
    content = [line.rstrip("\n") for line in block if line.strip()]
    if not content:
        return True
    return (
        all(_YAML_LINE.fullmatch(line) for line in content)
        and _YAML_KEY.match(content[0]) is not None
    )


def _front_matter_lines(lines: list[str]) -> int:
    """Number of leading lines taken by YAML front matter (0 when there is none)."""
    if not lines or lines[0].rstrip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].rstrip() in {"---", "..."}:
            return index + 1 if _looks_like_yaml_mapping(lines[1:index]) else 0
    return 0


def _front_matter_title(lines: list[str], count: int) -> str | None:
    for line in lines[1 : count - 1]:
        match = _TITLE_KEY.match(line.rstrip("\n"))
        if match is None:
            continue
        value = match.group(1)
        if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        value = value.translate(_INVISIBLE).strip()
        return None if value in {"", "|", ">", "|-", ">-"} else _clip(value, TITLE_LIMIT)
    return None


def _layout(text: str) -> tuple[list[str], list[int]]:
    # Only LF separates lines, as in markdown-it; str.splitlines also splits on FF, VT, U+2028...
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    if not lines[-1]:
        lines.pop()
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return lines, offsets


def _parse(lines: list[str], front_matter: int) -> list[Token]:
    # Front matter is blanked for parsing only so that its lines never become headings.
    masked = ["\n"] * front_matter + lines[front_matter:]
    return MarkdownIt("commonmark", {"html": True}).enable("table").parse("".join(masked))


def _protected(tokens: list[Token], offsets: list[int], front_matter: int) -> list[tuple[int, int]]:
    ranges = [(0, offsets[front_matter])] if front_matter else []
    ranges.extend(
        (offsets[token.map[0]], offsets[token.map[1]])
        for token in tokens
        if token.type in {"fence", "code_block"} and token.map is not None
    )
    return ranges


def _strip_removals(text: str, protected: list[tuple[int, int]]) -> str:
    """Remove allowed constructs but keep their line breaks so source line numbers stay exact."""
    pieces: list[str] = []
    position = 0
    for match in _REMOVALS.finditer(text):
        if any(low <= match.start() < high for low, high in protected):
            continue
        pieces.append(text[position : match.start()])
        pieces.append("\n" * match.group(0).count("\n"))
        position = match.end()
    pieces.append(text[position:])
    return "".join(pieces)


def _norm_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _definitions(
    lines: list[str], offsets: list[int], protected: list[tuple[int, int]]
) -> dict[str, tuple[int, int, str]]:
    found: dict[str, tuple[int, int, str]] = {}
    index = 0
    while index < len(lines):
        match = _DEFINITION.match(lines[index])
        if match is None or any(low <= offsets[index] < high for low, high in protected):
            index += 1
            continue
        last = index
        while (
            last + 1 < len(lines)
            and lines[last + 1].startswith(("    ", "\t"))
            and lines[last + 1].strip()
        ):
            last += 1
        found.setdefault(
            _norm_label(match.group(1)),
            (offsets[index], offsets[last + 1], "".join(lines[index : last + 1]).rstrip("\n")),
        )
        index = last + 1
    return found


def _footer(text: str, left: int, right: int, definitions: dict[str, tuple[int, int, str]]) -> str:
    """Definitions used by [text][ref] / [^note] in this range but defined in another chunk."""
    used: dict[str, tuple[int, int, str]] = {}
    for match in _BRACKET.finditer(text, left, right):
        key = _norm_label(match.group(1))
        found = definitions.get(key)
        if found is not None and not (left <= found[0] and found[1] <= right):
            used[key] = found
    lines: list[str] = []
    total = 0
    for _, _, value in sorted(used.values()):
        if total + len(value) > FOOTER_LIMIT:
            break
        lines.append(value)
        total += len(value) + 1
    return "\n" + "\n".join(lines) + "\n" if lines else ""


def _crumb(title: str, breadcrumb: str) -> str:
    if not breadcrumb:
        line = title
    elif breadcrumb == title or breadcrumb.startswith(title + " > "):
        line = breadcrumb
    else:
        line = f"{title} > {breadcrumb}"
    if len(line) > BREADCRUMB_LIMIT:
        line = line[: BREADCRUMB_LIMIT // 2] + " \u2026 " + line[-(BREADCRUMB_LIMIT // 2 - 3) :]
    return line + "\n\n"


def _soft_cut(text: str, start: int, cut: int) -> int:
    """Last resort inside one over-long line: sentence, then word, never inside a link or a
    base-plus-combining-mark sequence."""
    window = text[start:cut]
    floor = (cut - start) // 2
    position = cut
    for pattern in (_SENTENCE_END, _WHITESPACE):
        ends = [match.end() for match in pattern.finditer(window) if match.end() > floor]
        if ends:
            position = start + ends[-1]
            break
    for link in _LINK.finditer(text, start, min(len(text), cut + 600)):
        if start < link.start() < position < link.end():
            position = link.start()
    while position > start + 1 and (
        unicodedata.combining(text[position]) or text[position] in "\u200d\ufe0e\ufe0f"
    ):
        position -= 1
    while position > start + 1 and text[position - 1] == "\u200d":
        position -= 1
    return position


def _prepare(
    text: str,
) -> tuple[str, list[str], list[int], int, list[Token]]:
    """Allowed normalizations shared by chunking and the line-coverage invariant."""
    text = unicodedata.normalize(
        "NFC", text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    )
    lines, offsets = _layout(text)
    front_matter = _front_matter_lines(lines)
    tokens = _parse(lines, front_matter)
    if _REMOVALS.search(text):
        text = _strip_removals(text, _protected(tokens, offsets, front_matter))
        lines, offsets = _layout(text)
        tokens = _parse(lines, front_matter)
    return text, lines, offsets, front_matter, tokens


def canonical_lines(text: str) -> list[str]:
    """Source lines after the allowed normalizations; removed text keeps its line breaks."""
    _, lines, _, front_matter, _ = _prepare(text)
    return ["" if index < front_matter else line.rstrip("\n") for index, line in enumerate(lines)]


def parse_chunks(text: str, source_path: str, settings: Settings, encoder: Encoder) -> list[Chunk]:
    text, lines, offsets, front_matter, tokens = _prepare(text)
    protected = _protected(tokens, offsets, front_matter)
    definitions = _definitions(lines, offsets, protected)
    headings: list[tuple[int, int, int, str]] = []
    boundaries = set(offsets)
    blocks: list[_Block] = []
    for index, token in enumerate(tokens):
        if token.map is None:
            continue
        start, end = token.map
        boundaries.update((offsets[start], offsets[end]))
        if token.type == "heading_open":
            headings.append((start, end, int(token.tag[1:]), _label(tokens[index + 1])))
        elif token.type == "fence":
            opening = lines[start]
            close = end - 1
            closing_marker = lines[close].lstrip(" \t>").rstrip()
            has_close = (
                close > start
                and closing_marker.startswith(token.markup)
                and set(closing_marker) == {token.markup[0]}
            )
            generated_close = opening[: opening.index(token.markup)] + token.markup + "\n"
            blocks.append(
                _Block(
                    "fence",
                    offsets[start],
                    offsets[end],
                    offsets[start + 1],
                    offsets[close] if has_close else offsets[end],
                    opening.rstrip("\n") + "\n",
                    lines[close].rstrip("\n") + "\n" if has_close else generated_close,
                )
            )
        elif token.type == "table_open":
            header_end = min(start + 2, end)
            opening_text = text[offsets[start] : offsets[header_end]]
            blocks.append(
                _Block(
                    "table",
                    offsets[start],
                    offsets[end],
                    offsets[header_end],
                    offsets[end],
                    # A header too wide to repeat would starve the rows; split without it.
                    opening_text if len(opening_text) <= settings.max_content_chars // 3 else "",
                    "",
                )
            )
    block_starts = [block.start for block in blocks]
    block_reach: list[int] = []
    for block in blocks:
        block_reach.append(max(block.end, block_reach[-1] if block_reach else 0))
    boundary_points = sorted(boundaries)
    deep_headings = sorted(
        offsets[start] for start, _, level, _ in headings if level > BOUNDARY_LEVEL
    )
    front_title = _front_matter_title(lines, front_matter) if front_matter else None
    title = _clip(
        front_title
        or next(
            (name for _, _, level, name in headings if level == 1 and name),
            PurePosixPath(source_path).stem,
        ),
        TITLE_LIMIT,
    )
    stack: list[tuple[int, str, str]] = []
    counts: Counter[tuple[str, ...]] = Counter()
    sections: list[tuple[int, int, tuple[str, ...], str]] = []
    cursor = offsets[front_matter]
    identity: tuple[str, ...] = ("preamble",)
    breadcrumb = ""
    for start, end, level, label in headings:
        if level > BOUNDARY_LEVEL:
            continue
        sections.append((cursor, offsets[start], identity, breadcrumb))
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = tuple(frame[2] for frame in stack)
        occurrence_key = (*parent, f"{level}:{label}")
        counts[occurrence_key] += 1
        stack.append((level, label, f"{level}:{label}#{counts[occurrence_key]}"))
        identity = tuple(frame[2] for frame in stack)
        breadcrumb = " > ".join(frame[1] for frame in stack if frame[1])
        cursor = offsets[end]
    sections.append((cursor, len(text), identity, breadcrumb))

    def render(prefix: str, left: int, right: int) -> str:
        lead = tail = ""
        found_lead = found_tail = False
        index = bisect_left(block_starts, right) - 1
        while index >= 0 and block_reach[index] >= left and not (found_lead and found_tail):
            block = blocks[index]
            index -= 1
            if block.kind == "fence":
                if not found_tail and block.body_start < right <= block.closing:
                    tail, found_tail = block.closing_text, True
                if (
                    not found_lead
                    and block.body_start <= left <= block.closing
                    and left > block.start
                ):
                    lead, found_lead = block.opening_text, True
            elif not found_lead and block.body_start <= left < block.end and left > block.start:
                lead, found_lead = block.opening_text, True
        body = text[left:right].rstrip("\n") + "\n"
        return prefix + lead + body + tail + _footer(text, left, right, definitions)

    def fits(prefix: str, left: int, right: int) -> bool:
        value = render(prefix, left, right)
        return len(value) <= settings.max_content_chars and encoder.count(value) <= min(
            settings.target_tokens, settings.max_input_tokens
        )

    merged: list[tuple[int, int, tuple[str, ...], str]] = []
    for section in sections:
        if not text[section[0] : section[1]].strip():
            continue
        if merged:
            previous = merged[-1]
            is_child = (
                section[2][:-1] == previous[2]
            )  # direct child only, so heading_path stays truthful
            if (
                is_child
                and encoder.count(text[section[0] : section[1]].strip()) < MIN_SECTION_TOKENS
                and fits(_crumb(title, previous[3]), previous[0], section[1])
            ):
                merged[-1] = (previous[0], section[1], previous[2], previous[3])
                continue
        merged.append(section)

    result: list[Chunk] = []
    for start, end, identity, breadcrumb in merged:
        prefix = _crumb(title, breadcrumb)
        parts: list[Chunk] = []
        while start < end:
            while start < end and text[start] == "\n":
                start += 1
            if not text[start:end].strip():
                break
            if fits(prefix, start, end):
                cut = end
            else:
                low, high = start + 1, min(end, start + settings.max_content_chars)
                cut = start
                while low <= high:
                    middle = (low + high) // 2
                    if fits(prefix, start, middle):
                        cut = middle
                        low = middle + 1
                    else:
                        high = middle - 1
                if cut == start:
                    raise IngestionError(
                        "Context/wrappers and source cannot fit the configured budget"
                    )
                chosen: int | None = None
                heading = bisect_right(deep_headings, cut) - 1
                line = bisect_right(boundary_points, cut) - 1
                for point in (
                    deep_headings[heading]
                    if heading >= 0 and deep_headings[heading] - start >= (cut - start) // 4
                    else None,
                    boundary_points[line] if line >= 0 else None,
                ):
                    if (
                        point is not None
                        and point > start
                        and text[start:point].strip()
                        and fits(prefix, start, point)
                    ):
                        chosen = point
                        break
                if chosen is None:
                    soft = _soft_cut(text, start, cut)
                    if soft > start and text[start:soft].strip() and fits(prefix, start, soft):
                        chosen = soft
                if chosen is not None:
                    cut = chosen
            actual_end = cut
            while actual_end > start and text[actual_end - 1] == "\n":
                actual_end -= 1
            value = render(prefix, start, cut)
            if not fits(prefix, start, cut):
                raise IngestionError("Chunk exceeds the configured budget")
            parts.append(
                Chunk(
                    title,
                    breadcrumb,
                    identity,
                    value,
                    bisect_right(offsets, start),
                    bisect_right(offsets, max(start, actual_end - 1)),
                    len(parts) + 1,
                    0,
                )
            )
            if cut == end:
                break
            next_start = cut
            if settings.overlap_tokens:
                low, high = start + 1, cut
                while low <= high:
                    middle = (low + high) // 2
                    if encoder.count(text[middle:cut]) <= settings.overlap_tokens:
                        next_start = middle
                        high = middle - 1
                    else:
                        low = middle + 1
            start = max(start + 1, next_start)
        result.extend(replace(part, part_count=len(parts)) for part in parts)
    return result
