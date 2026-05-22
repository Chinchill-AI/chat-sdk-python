"""Markdown parser producing mdast-compatible AST dicts.

This is the shared parsing infrastructure for the Python Chat SDK.
All format converters use these functions instead of duplicating parsing logic.

The AST follows the mdast specification:
  https://github.com/syntax-tree/mdast

Node types produced:
  Block-level: root, paragraph, heading, code, thematicBreak, blockquote,
               list, listItem, table, tableRow, tableCell, image (block)
  Inline-level: text, strong, emphasis, delete, inlineCode, link, image, break
"""

from __future__ import annotations

import copy
import re
from typing import Any

# ---------------------------------------------------------------------------
# Type aliases (mdast-compatible dicts)
# ---------------------------------------------------------------------------

Content = dict[str, Any]
Root = dict[str, Any]

# ---------------------------------------------------------------------------
# Node constructors
# ---------------------------------------------------------------------------


def make_root(children: list[Content]) -> Root:
    """Create a root AST node."""
    return {"type": "root", "children": children}


def make_text(value: str) -> Content:
    """Create a text leaf node."""
    return {"type": "text", "value": value}


def make_paragraph(children: list[Content]) -> Content:
    """Create a paragraph node."""
    return {"type": "paragraph", "children": children}


def make_heading(depth: int, children: list[Content]) -> Content:
    """Create a heading node (depth 1-6)."""
    return {"type": "heading", "depth": depth, "children": children}


def make_code(value: str, lang: str | None = None) -> Content:
    """Create a fenced code block node."""
    return {"type": "code", "value": value, "lang": lang}


def make_inline_code(value: str) -> Content:
    """Create an inline code node."""
    return {"type": "inlineCode", "value": value}


def make_strong(children: list[Content]) -> Content:
    """Create a strong (bold) node."""
    return {"type": "strong", "children": children}


def make_emphasis(children: list[Content]) -> Content:
    """Create an emphasis (italic) node."""
    return {"type": "emphasis", "children": children}


def make_delete(children: list[Content]) -> Content:
    """Create a delete (strikethrough) node."""
    return {"type": "delete", "children": children}


def make_link(url: str, children: list[Content], title: str | None = None) -> Content:
    """Create a link node."""
    node: Content = {"type": "link", "url": url, "children": children}
    if title is not None:
        node["title"] = title
    return node


def make_image(url: str, alt: str = "", title: str | None = None) -> Content:
    """Create an image node."""
    node: Content = {"type": "image", "url": url, "alt": alt}
    if title is not None:
        node["title"] = title
    return node


def make_blockquote(children: list[Content]) -> Content:
    """Create a blockquote node."""
    return {"type": "blockquote", "children": children}


def make_list(children: list[Content], *, ordered: bool = False, start: int = 1) -> Content:
    """Create a list node."""
    node: Content = {"type": "list", "ordered": ordered, "children": children}
    if ordered:
        node["start"] = start
    return node


def make_list_item(children: list[Content], *, checked: bool | None = None) -> Content:
    """Create a list item node.

    *checked* follows the mdast GFM task-list extension: ``True`` for
    ``- [x]``, ``False`` for ``- [ ]``, ``None`` for a regular list item.
    """
    node: Content = {"type": "listItem", "children": children}
    if checked is not None:
        node["checked"] = checked
    return node


def make_thematic_break() -> Content:
    """Create a thematic break (horizontal rule) node."""
    return {"type": "thematicBreak"}


def make_table(children: list[Content], align: list[str | None] | None = None) -> Content:
    """Create a table node."""
    node: Content = {"type": "table", "children": children}
    if align is not None:
        node["align"] = align
    return node


def make_table_row(children: list[Content]) -> Content:
    """Create a table row node."""
    return {"type": "tableRow", "children": children}


def make_table_cell(children: list[Content]) -> Content:
    """Create a table cell node."""
    return {"type": "tableCell", "children": children}


def make_break() -> Content:
    """Create a hard line break node."""
    return {"type": "break"}


# ---------------------------------------------------------------------------
# Node helper functions
# ---------------------------------------------------------------------------


def get_node_children(node: Content) -> list[Content]:
    """Get children from a node, returning empty list if none."""
    children = node.get("children")
    if isinstance(children, list):
        return children
    return []


def get_node_value(node: Content) -> str:
    """Get the value from a node, returning empty string if none."""
    value = node.get("value")
    if isinstance(value, str):
        return value
    return ""


# ---------------------------------------------------------------------------
# Inline parser
# ---------------------------------------------------------------------------

# ASCII-punctuation characters that may be backslash-escaped per
# CommonMark. A preceding `\` makes the next character literal -- not a
# delimiter -- which the lookbehinds in the patterns below honour.
_ESCAPABLE_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")

# Regex patterns for inline elements, ordered by priority.
# Each pattern captures a full inline construct. Every opening delimiter
# uses a ``(?<!\\)`` lookbehind so an escaped opener (``\*``, ``\[``, etc.)
# is treated as literal text rather than as a delimiter run. Bracket
# contents (link / image) also tolerate inner ``\]`` / ``\[`` via the
# ``(?:[^\]\\]|\\.)*`` pattern.
_INLINE_PATTERNS = [
    # Images: ![alt](url) or ![alt](url "title")
    ("image", re.compile(r'(?<!\\)!\[((?:[^\]\\]|\\.)*)\]\((\S+?)(?:\s+"([^"]*)")?\)')),
    # Links: [text](url) or [text](url "title")
    ("link", re.compile(r'(?<!\\)\[((?:[^\]\\]|\\.)*)\]\((\S+?)(?:\s+"([^"]*)")?\)')),
    # Inline code: `code`
    ("inlineCode", re.compile(r"(?<!\\)`([^`]+)`")),
    # Bold: **text**
    ("strong_star", re.compile(r"(?<!\\)\*\*((?:[^\\]|\\.)+?)\*\*")),
    # Bold: __text__
    ("strong_under", re.compile(r"(?<!\\)__((?:[^\\]|\\.)+?)__")),
    # Strikethrough: ~~text~~
    ("delete", re.compile(r"(?<!\\)~~((?:[^\\]|\\.)+?)~~")),
    # Emphasis: *text*  (not preceded/followed by *)
    ("emphasis_star", re.compile(r"(?<![*\\])\*(?!\*)((?:[^\\]|\\.)+?)(?<!\*)\*(?!\*)")),
    # Emphasis: _text_  (not preceded/followed by _)
    ("emphasis_under", re.compile(r"(?<![_\\])_(?!_)((?:[^\\]|\\.)+?)(?<!_)_(?!_)")),
]


def _unescape_punct(text: str) -> str:
    """Resolve backslash-escapes of ASCII punctuation to their literal char.

    Applied at text-leaf emission only -- inline delimiters were already
    consumed by the patterns above with escape-aware lookbehinds, so by
    the time we reach a text leaf, any remaining ``\\X`` pair where X is
    ASCII punct is unambiguously a literal X.
    """
    if "\\" not in text:
        return text
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPABLE_PUNCT:
            out.append(text[i + 1])
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# Inverse of `_unescape_punct`: characters that, if present literally in
# a text leaf, would be parsed as the start of a markdown construct on
# the next ``parse_markdown`` call. Re-escape these at stringify time so
# the AST round-trips. Over-escaping ordinary punctuation produces noisy
# output without improving correctness -- only delimiters need it.
_TEXT_DELIMITERS = frozenset("*_~`[]\\")


def _escape_text_leaf(value: str) -> str:
    """Re-escape delimiter characters so a text leaf round-trips through
    parse_markdown unchanged.

    Inverse of :func:`_unescape_punct` -- a text node carrying the value
    ``"*literal*"`` came from input ``\\*literal\\*``; stringify must
    emit the backslashes or re-parse will form an emphasis node.
    """
    if not any(c in _TEXT_DELIMITERS for c in value):
        return value
    out: list[str] = []
    for c in value:
        if c in _TEXT_DELIMITERS:
            out.append("\\")
        out.append(c)
    return "".join(out)


# Block-level constructs only fire at line start. When a paragraph's
# joined text begins with one of these patterns -- typically after
# `_unescape_punct` resolved a ``\#`` or ``\>`` -- naive stringify would
# re-form the construct on the next parse. These regexes match the
# minimal opener pattern; `_escape_paragraph_block_markers` inserts the
# backslash at the right position.
_BLOCK_HEADING_RE = re.compile(r"^#{1,6}(?=\s|$)")
_BLOCK_BLOCKQUOTE_RE = re.compile(r"^>")
_BLOCK_UNORDERED_RE = re.compile(r"^[-+](?=\s)")
_BLOCK_ORDERED_RE = re.compile(r"^(\d+)([.)])(?=\s)")
_BLOCK_THEMATIC_DASH_RE = re.compile(r"^(-\s*){3,}\s*$")


def _escape_block_marker_line(line: str) -> str:
    """Re-escape a block-level marker at the start of a single line."""
    if not line:
        return line
    if _BLOCK_HEADING_RE.match(line):
        return "\\" + line
    if _BLOCK_BLOCKQUOTE_RE.match(line):
        return "\\" + line
    if _BLOCK_UNORDERED_RE.match(line):
        return "\\" + line
    m = _BLOCK_ORDERED_RE.match(line)
    if m:
        # Escape the . or ) marker, not the digits -- `\1` isn't a
        # CommonMark escape (digits aren't in escapable punct), so a
        # leading-backslash placement wouldn't survive `_unescape_punct`.
        return f"{m.group(1)}\\{m.group(2)}{line[m.end() :]}"
    if _BLOCK_THEMATIC_DASH_RE.match(line):
        return "\\" + line
    return line


def _escape_paragraph_block_markers(text: str) -> str:
    """Re-escape block-level markers at the start of every line in a
    paragraph's joined output.

    Applied to the joined output of a `paragraph` node. Block-level
    constructs (heading, blockquote, list, thematic break) only fire at
    line start, so mid-line occurrences of ``#`` / ``>`` / ``-`` / ``+``
    are safe. But a paragraph text leaf may contain ``\\n`` (from
    `_unescape_punct` resolving an escaped marker after a soft line
    break, or from joining text leaves around a hard break); each line
    of the joined output needs the same escape treatment as line 1, or
    re-parse will split the paragraph at the embedded marker.

    ``*``-based and ``_``-based thematic breaks are already handled by
    :func:`_escape_text_leaf` since both characters are in
    `_TEXT_DELIMITERS`.
    """
    if not text:
        return text
    if "\n" not in text:
        return _escape_block_marker_line(text)
    return "\n".join(_escape_block_marker_line(line) for line in text.split("\n"))


def _parse_inline_plain(text: str) -> list[Content]:
    """Parse plain text that contains no inline formatting.

    Handles hard line breaks (two trailing spaces + newline) and resolves
    backslash escapes of ASCII punctuation to their literal character.
    """
    if "  \n" in text:
        parts: list[Content] = []
        segments = text.split("  \n")
        for i, seg in enumerate(segments):
            if seg:
                parts.append(make_text(_unescape_punct(seg)))
            if i < len(segments) - 1:
                parts.append(make_break())
        return parts if parts else [make_text(_unescape_punct(text))]
    return [make_text(_unescape_punct(text))]


def _parse_inline(text: str) -> list[Content]:
    """Parse inline markdown elements into AST nodes.

    Handles: strong, emphasis, delete, inlineCode, link, image.
    Returns a list of inline Content nodes.

    The suffix (text after a match) is processed iteratively to avoid
    unbounded recursion on long strings.  Content *inside* a match
    (e.g. bold text, link text) still recurses, but that depth is
    bounded by the match length.
    """
    if not text:
        return []

    nodes: list[Content] = []
    remaining = text

    while remaining:
        # Find the earliest match across all patterns
        best_match = None
        best_kind = ""
        best_start = len(remaining)

        for kind, pattern in _INLINE_PATTERNS:
            m = pattern.search(remaining)
            if m and m.start() < best_start:
                best_match = m
                best_kind = kind
                best_start = m.start()

        # No inline formatting found -- return plain text for remainder
        if best_match is None:
            nodes.extend(_parse_inline_plain(remaining))
            break

        # Text before the match (no formatting, so no recursion needed)
        if best_start > 0:
            nodes.extend(_parse_inline_plain(remaining[:best_start]))

        # The matched construct (content recursion is bounded by match length)
        if best_kind == "image":
            alt = _unescape_punct(best_match.group(1))
            url = best_match.group(2)
            title = best_match.group(3)
            nodes.append(make_image(url, alt, title))
        elif best_kind == "link":
            link_text = best_match.group(1)
            url = best_match.group(2)
            title = best_match.group(3)
            nodes.append(make_link(url, _parse_inline(link_text), title))
        elif best_kind == "inlineCode":
            nodes.append(make_inline_code(best_match.group(1)))
        elif best_kind in ("strong_star", "strong_under"):
            nodes.append(make_strong(_parse_inline(best_match.group(1))))
        elif best_kind == "delete":
            nodes.append(make_delete(_parse_inline(best_match.group(1))))
        elif best_kind in ("emphasis_star", "emphasis_under"):
            nodes.append(make_emphasis(_parse_inline(best_match.group(1))))

        # Advance past the match (iterative, not recursive)
        remaining = remaining[best_match.end() :]

    return nodes


# ---------------------------------------------------------------------------
# Block parser
# ---------------------------------------------------------------------------

# Patterns used by the block parser
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_THEMATIC_BREAK_RE = re.compile(r"^([-*_]\s*){3,}\s*$")
_FENCED_CODE_START_RE = re.compile(r"^(`{3,}|~{3,})(.*)")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)")
_ORDERED_LIST_RE = re.compile(r"^(\d+)[.)]\s+(.*)")
_UNORDERED_LIST_RE = re.compile(r"^[-*+]\s+(.*)")
_TABLE_SEPARATOR_RE = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|?\s*$")


def _parse_table_row(line: str) -> list[str]:
    """Extract cell contents from a pipe-delimited table row."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def _parse_table_alignment(line: str) -> list[str | None]:
    """Parse the alignment row of a GFM table."""
    cells = _parse_table_row(line)
    aligns: list[str | None] = []
    for cell in cells:
        cell = cell.strip()
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            aligns.append("center")
        elif right:
            aligns.append("right")
        elif left:
            aligns.append("left")
        else:
            aligns.append(None)
    return aligns


def _collect_list_items(lines: list[str], start: int, ordered: bool) -> tuple[list[Content], int]:
    """Collect consecutive list items starting at *start*.

    Returns (list_item_nodes, next_line_index).
    Handles continuation lines (indented by 2+ spaces) and nested lists.
    """
    items: list[Content] = []
    i = start
    item_re = re.compile(r"^(\d+)[.)]\s+(.*)") if ordered else re.compile(r"^[-*+]\s+(.*)")
    # GFM task list extension: `- [ ]` or `- [x]` at the start of an
    # unordered item maps to `listItem.checked = False` / `True`. The
    # trailing whitespace + content group is optional so an empty task
    # item (``- [ ]`` with nothing after) still produces ``checked``.
    task_re = re.compile(r"^\[([ xX])\](?:\s+(.*))?$")

    while i < len(lines):
        line = lines[i]
        m = item_re.match(line)
        if m:
            item_text = m.group(2) if ordered else m.group(1)

            checked: bool | None = None
            if not ordered:
                task_m = task_re.match(item_text)
                if task_m:
                    checked = task_m.group(1) in ("x", "X")
                    # group(2) is None for an empty task item (`- [ ]`)
                    # with no trailing whitespace/content.
                    item_text = task_m.group(2) or ""

            item_children_lines = [item_text]
            i += 1

            # Collect continuation / nested lines (indented by 2+ spaces)
            while i < len(lines):
                next_line = lines[i]
                if next_line.startswith("  "):
                    item_children_lines.append(next_line[2:])
                    i += 1
                elif not next_line.strip():
                    # Blank line might separate items or end the list
                    # Peek ahead: if the next non-blank line is a list item, continue
                    if i + 1 < len(lines):
                        peek = lines[i + 1]
                        if item_re.match(peek) or peek.startswith("  "):
                            item_children_lines.append("")
                            i += 1
                            continue
                    break
                else:
                    break

            # Parse nested content in the item
            nested_children = _parse_list_item_content(item_children_lines)
            items.append(make_list_item(nested_children, checked=checked))
        else:
            break

    return items, i


def _parse_list_item_content(lines: list[str]) -> list[Content]:
    """Parse the content lines of a single list item.

    This can contain paragraphs and nested lists.
    """
    children: list[Content] = []
    text_lines: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for nested ordered list
        ol_match = _ORDERED_LIST_RE.match(line)
        if ol_match:
            # Flush accumulated text
            if text_lines:
                children.append(make_paragraph(_parse_inline("\n".join(text_lines))))
                text_lines = []
            nested_items, i = _collect_list_items(lines, i, ordered=True)
            start_num = int(ol_match.group(1))
            children.append(make_list(nested_items, ordered=True, start=start_num))
            continue

        # Check for nested unordered list
        ul_match = _UNORDERED_LIST_RE.match(line)
        if ul_match:
            if text_lines:
                children.append(make_paragraph(_parse_inline("\n".join(text_lines))))
                text_lines = []
            nested_items, i = _collect_list_items(lines, i, ordered=False)
            children.append(make_list(nested_items, ordered=False))
            continue

        # Regular text
        if line.strip():
            text_lines.append(line)
        elif text_lines:
            children.append(make_paragraph(_parse_inline("\n".join(text_lines))))
            text_lines = []
        i += 1

    if text_lines:
        children.append(make_paragraph(_parse_inline("\n".join(text_lines))))

    return children


def parse_markdown(text: str) -> Root:
    """Parse a markdown string into an mdast-compatible AST.

    Supports:
      Block: paragraphs, headings (#-######), fenced code blocks (```/~~~),
             thematic breaks (---/***/___ ), blockquotes (>), ordered lists
             (1.), unordered lists (-/*/+), GFM tables (| ... |)
      Inline: strong (**), emphasis (*/_), delete (~~), inline code (`),
              links ([text](url)), images (![alt](url))

    Returns a Root dict ``{"type": "root", "children": [...]}``.
    """
    children: list[Content] = []
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # -- Fenced code blocks -----------------------------------------------
        code_match = _FENCED_CODE_START_RE.match(line)
        if code_match:
            fence_char = code_match.group(1)[0]
            fence_len = len(code_match.group(1))
            lang = code_match.group(2).strip() or None
            code_lines: list[str] = []
            i += 1
            while i < len(lines):
                close_match = re.match(rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$", lines[i])
                if close_match:
                    i += 1
                    break
                code_lines.append(lines[i])
                i += 1
            children.append(make_code("\n".join(code_lines), lang))
            continue

        # -- Thematic break ----------------------------------------------------
        if _THEMATIC_BREAK_RE.match(line):
            children.append(make_thematic_break())
            i += 1
            continue

        # -- Heading -----------------------------------------------------------
        heading_match = _HEADING_RE.match(line)
        if heading_match:
            depth = len(heading_match.group(1))
            heading_text = heading_match.group(2).rstrip().rstrip("#").rstrip()
            children.append(make_heading(depth, _parse_inline(heading_text)))
            i += 1
            continue

        # -- GFM Table ---------------------------------------------------------
        # A table needs at least a header row and a separator row
        if "|" in line and i + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
            align = _parse_table_alignment(lines[i + 1])
            table_rows: list[Content] = []

            # Header row
            header_cells = _parse_table_row(line)
            table_rows.append(make_table_row([make_table_cell(_parse_inline(c)) for c in header_cells]))
            i += 2  # skip header + separator

            # Data rows
            while i < len(lines) and "|" in lines[i] and not _THEMATIC_BREAK_RE.match(lines[i]):
                row_cells = _parse_table_row(lines[i])
                table_rows.append(make_table_row([make_table_cell(_parse_inline(c)) for c in row_cells]))
                i += 1

            children.append(make_table(table_rows, align))
            continue

        # -- Blockquote --------------------------------------------------------
        bq_match = _BLOCKQUOTE_RE.match(line)
        if bq_match:
            bq_lines: list[str] = []
            while i < len(lines):
                bq_m = _BLOCKQUOTE_RE.match(lines[i])
                if bq_m:
                    bq_lines.append(bq_m.group(1))
                    i += 1
                elif lines[i].strip() == "" and i + 1 < len(lines) and _BLOCKQUOTE_RE.match(lines[i + 1]):
                    # Allow blank line between blockquote continuation
                    bq_lines.append("")
                    i += 1
                else:
                    break
            # Recursively parse blockquote content
            bq_ast = parse_markdown("\n".join(bq_lines))
            children.append(make_blockquote(bq_ast.get("children", [])))
            continue

        # -- Ordered list ------------------------------------------------------
        ol_match = _ORDERED_LIST_RE.match(line)
        if ol_match:
            start_num = int(ol_match.group(1))
            items, i = _collect_list_items(lines, i, ordered=True)
            children.append(make_list(items, ordered=True, start=start_num))
            continue

        # -- Unordered list ----------------------------------------------------
        ul_match = _UNORDERED_LIST_RE.match(line)
        if ul_match:
            items, i = _collect_list_items(lines, i, ordered=False)
            children.append(make_list(items, ordered=False))
            continue

        # -- Empty line --------------------------------------------------------
        if not line.strip():
            i += 1
            continue

        # -- Paragraph (default) -----------------------------------------------
        para_lines = [line]
        i += 1
        while i < len(lines):
            next_line = lines[i]
            # Stop paragraph at blank line, heading, code fence, thematic break,
            # list item, blockquote, or table separator
            if (
                not next_line.strip()
                or _HEADING_RE.match(next_line)
                or _FENCED_CODE_START_RE.match(next_line)
                or _THEMATIC_BREAK_RE.match(next_line)
                or _BLOCKQUOTE_RE.match(next_line)
                or _ORDERED_LIST_RE.match(next_line)
                or _UNORDERED_LIST_RE.match(next_line)
            ):
                break
            # Also stop if the *next* line is a table separator (current line is header)
            if i + 1 < len(lines) and "|" in next_line and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
                # Put this line back so the table parser handles it
                break
            para_lines.append(next_line)
            i += 1

        children.append(make_paragraph(_parse_inline("\n".join(para_lines))))

    return make_root(children)


# ---------------------------------------------------------------------------
# Stringify (AST -> markdown text)
# ---------------------------------------------------------------------------


def stringify_markdown(
    ast: Root,
    *,
    emphasis: str = "*",
    bullet: str = "*",
) -> str:
    """Stringify an AST back to markdown text.

    Args:
        ast: Root AST node.
        emphasis: Character to use for emphasis markers (* or _).
        bullet: Character to use for unordered list bullets (*, -, +).
    """
    children = ast.get("children", [])
    parts: list[str] = []
    for child in children:
        part = _stringify_node(child, emphasis=emphasis, bullet=bullet)
        if part is not None:
            parts.append(part)
    result = "\n\n".join(parts)
    return result + "\n" if result else ""


def _stringify_node(node: Content, *, emphasis: str = "*", bullet: str = "*") -> str | None:
    """Stringify a single AST node to markdown."""
    node_type = node.get("type")

    if node_type == "text":
        return _escape_text_leaf(node.get("value", ""))

    if node_type == "break":
        return "\n"

    if node_type == "paragraph":
        children = node.get("children", [])
        joined = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        return _escape_paragraph_block_markers(joined)

    if node_type == "heading":
        depth = node.get("depth", 1)
        children = node.get("children", [])
        text = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        return f"{'#' * depth} {text}"

    if node_type == "strong":
        children = node.get("children", [])
        text = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        return f"**{text}**"

    if node_type == "emphasis":
        children = node.get("children", [])
        text = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        return f"{emphasis}{text}{emphasis}"

    if node_type == "delete":
        children = node.get("children", [])
        text = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        return f"~~{text}~~"

    if node_type == "code":
        value = node.get("value", "")
        lang = node.get("lang") or ""
        return f"```{lang}\n{value}\n```"

    if node_type == "inlineCode":
        return f"`{node.get('value', '')}`"

    if node_type == "link":
        children = node.get("children", [])
        text = "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
        url = node.get("url", "")
        title = node.get("title")
        if title:
            return f'[{text}]({url} "{title}")'
        return f"[{text}]({url})"

    if node_type == "image":
        alt = _escape_text_leaf(node.get("alt", ""))
        url = node.get("url", "")
        title = node.get("title")
        if title:
            return f'![{alt}]({url} "{title}")'
        return f"![{alt}]({url})"

    if node_type == "list":
        items = node.get("children", [])
        ordered = node.get("ordered", False)
        start = node.get("start", 1)
        lines: list[str] = []
        for idx, item in enumerate(items):
            prefix = f"{start + idx}." if ordered else bullet
            # GFM task-list marker, only on unordered items with `checked` set.
            checked = item.get("checked") if not ordered else None
            task_marker = ""
            if checked is True:
                task_marker = "[x] "
            elif checked is False:
                task_marker = "[ ] "
            item_children = item.get("children", [])
            if not item_children:
                # Empty item -- still emit the prefix (and any task marker)
                # so the round-trip preserves an empty `- [ ]` task.
                lines.append(f"{prefix} {task_marker}".rstrip())
                continue
            for ci, child in enumerate(item_children):
                child_type = child.get("type")
                if child_type == "list":
                    nested = _stringify_node(child, emphasis=emphasis, bullet=bullet)
                    if nested:
                        # Indent nested list
                        for nl in nested.split("\n"):
                            lines.append(f"  {nl}")
                else:
                    text = _stringify_node(child, emphasis=emphasis, bullet=bullet) or ""
                    if ci == 0:
                        lines.append(f"{prefix} {task_marker}{text}")
                    else:
                        lines.append(f"  {text}")
        return "\n".join(lines)

    if node_type == "listItem":
        children = node.get("children", [])
        return "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)

    if node_type == "blockquote":
        children = node.get("children", [])
        inner_parts: list[str] = []
        for child in children:
            part = _stringify_node(child, emphasis=emphasis, bullet=bullet)
            if part is not None:
                inner_parts.append(part)
        inner = "\n\n".join(inner_parts)
        return "\n".join(f"> {line}" for line in inner.split("\n"))

    if node_type == "thematicBreak":
        return "---"

    if node_type == "table":
        return _stringify_table(node)

    # Fallback: stringify children or return value
    children = node.get("children", [])
    if children:
        return "".join(_stringify_node(c, emphasis=emphasis, bullet=bullet) or "" for c in children)
    return node.get("value")


def _stringify_table(node: Content) -> str:
    """Stringify a table node to GFM pipe table format."""
    rows = node.get("children", [])
    if not rows:
        return ""

    align = node.get("align", [])

    def cell_text(cell: Content) -> str:
        children = cell.get("children", [])
        return "".join(_stringify_node(c) or "" for c in children)

    # Extract all cell texts to calculate widths
    all_text_rows: list[list[str]] = []
    for row in rows:
        cells = row.get("children", [])
        all_text_rows.append([cell_text(c) for c in cells])

    if not all_text_rows:
        return ""

    num_cols = max(len(r) for r in all_text_rows)

    # Calculate column widths (min 3 for separator)
    widths = [3] * num_cols
    for row in all_text_rows:
        for j, text in enumerate(row):
            if j < num_cols:
                widths[j] = max(widths[j], len(text))

    lines: list[str] = []

    # Header row
    header = all_text_rows[0] if all_text_rows else []
    header_cells = [(header[j] if j < len(header) else "").ljust(widths[j]) for j in range(num_cols)]
    lines.append("| " + " | ".join(header_cells) + " |")

    # Separator row
    sep_cells: list[str] = []
    for j in range(num_cols):
        a = align[j] if j < len(align) else None
        dash = "-" * widths[j]
        if a == "center":
            sep_cells.append(f":{dash[1:-1]}:")
        elif a == "right":
            sep_cells.append(f"{dash[:-1]}:")
        elif a == "left":
            sep_cells.append(f":{dash[1:]}")
        else:
            sep_cells.append(dash)
    lines.append("| " + " | ".join(sep_cells) + " |")

    # Data rows
    for row_text in all_text_rows[1:]:
        data_cells = [(row_text[j] if j < len(row_text) else "").ljust(widths[j]) for j in range(num_cols)]
        lines.append("| " + " | ".join(data_cells) + " |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Walk (AST visitor/transformer)
# ---------------------------------------------------------------------------


def walk_ast(node: Content, visitor: Any) -> Content:
    """Walk an AST tree, applying *visitor* to each child node.

    The visitor receives each child Content node and should return:
      - The original node (no change)
      - A replacement node
      - ``None`` to remove the node

    Children are walked recursively *after* the visitor is applied.

    Returns a new tree; the input *node* is not mutated.
    """
    node = copy.deepcopy(node)  # Don't mutate the input
    children = node.get("children")
    if isinstance(children, list):
        new_children: list[Content] = []
        for child in children:
            result = visitor(child)
            if result is None:
                continue
            new_children.append(walk_ast(result, visitor))
        node["children"] = new_children
    return node


# ---------------------------------------------------------------------------
# Plain-text extraction
# ---------------------------------------------------------------------------


def ast_to_plain_text(node: Content) -> str:
    """Extract plain text from an AST node, stripping all formatting."""
    node_type = node.get("type")

    if node_type == "text":
        return node.get("value", "")

    if node_type in ("inlineCode", "code"):
        return node.get("value", "")

    if node_type == "break":
        return "\n"

    if node_type == "thematicBreak":
        return ""

    if node_type == "image":
        return node.get("alt", "")

    children = node.get("children", [])
    if children:
        parts = [ast_to_plain_text(c) for c in children]
        # Block-level nodes get newline separation
        if node_type in ("root", "blockquote"):
            return "\n".join(p for p in parts if p)
        if node_type == "list":
            return "\n".join(p for p in parts if p)
        if node_type == "listItem":
            return " ".join(p for p in parts if p)
        return "".join(parts)

    return node.get("value", "")


# ---------------------------------------------------------------------------
# ASCII table helper (for adapters that lack native table support)
# ---------------------------------------------------------------------------


def table_to_ascii(node: Content) -> str:
    """Render an mdast table node as a padded ASCII table string.

    Output format::

        Name  | Age | Role
        ------|-----|--------
        Alice | 30  | Engineer
        Bob   | 25  | Designer

    Shared by adapters that lack native table support (Slack, Google Chat,
    Telegram, WhatsApp).
    """
    rows: list[list[str]] = []
    for row_node in node.get("children", []):
        cells: list[str] = []
        for cell_node in row_node.get("children", []):
            cells.append(ast_to_plain_text(cell_node))
        rows.append(cells)

    if not rows:
        return ""

    headers = rows[0]
    data_rows = rows[1:]
    return table_element_to_ascii(headers, data_rows)


def table_element_to_ascii(headers: list[str], rows: list[list[str]]) -> str:
    """Render headers + rows as a padded ASCII table.

    Used for card TableElement fallback rendering and mdast table nodes.
    """
    if not headers:
        return ""

    all_rows = [headers, *rows]
    col_count = max((len(r) for r in all_rows), default=0)
    if col_count == 0:
        return ""

    widths = [0] * col_count
    for row in all_rows:
        for i, cell in enumerate(row):
            if i < col_count:
                widths[i] = max(widths[i], len(cell))

    def format_row(cells: list[str]) -> str:
        parts = [(cells[i] if i < len(cells) else "").ljust(widths[i]) for i in range(col_count)]
        return " | ".join(parts).rstrip()

    lines: list[str] = [format_row(headers)]
    lines.append("-|-".join("-" * w for w in widths))
    for row in rows:
        lines.append(format_row(row))
    return "\n".join(lines)
