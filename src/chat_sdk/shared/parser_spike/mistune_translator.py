"""mistune (3.x) -> mdast translator.

Uses ``mistune.create_markdown(renderer=None)`` to obtain the parser's
internal token list, then maps each token type to its mdast equivalent.

GFM plugins enabled: ``table``, ``strikethrough``, ``task_lists``,
``url``.

Notes on the token shape (mistune 3.x): each token is a dict with
``type`` (always), ``children`` (block tokens + some inline), ``raw``
(text leaves), ``attrs`` (heading levels, link urls, list metadata).
The inline parser is invoked lazily; we drive it explicitly via
``md.inline.parse`` for cells / list items where needed.
"""

from __future__ import annotations

from typing import Any

import mistune

from chat_sdk.shared.markdown_parser import (
    Content,
    Root,
    make_blockquote,
    make_break,
    make_code,
    make_delete,
    make_emphasis,
    make_heading,
    make_image,
    make_inline_code,
    make_link,
    make_list,
    make_list_item,
    make_paragraph,
    make_root,
    make_strong,
    make_table,
    make_table_cell,
    make_table_row,
    make_text,
    make_thematic_break,
)

# Single shared parser instance (mistune parsers are stateless after creation).
_MD = mistune.create_markdown(
    renderer=None,
    plugins=["table", "strikethrough", "task_lists", "url"],
)


def parse_markdown(text: str) -> Root:
    """Parse *text* and return an mdast-compatible root node."""
    tokens, _state = _MD.parse(text)
    # mistune's parse() return-type is `list[dict | str]` -- a bare str
    # token is the rare lazy-text node that the public API stringifies
    # directly. Narrow to dicts for the structural walker; lift any
    # bare-string tokens into paragraph(text(...)) so they're not lost.
    children: list[Content] = []
    for tok in tokens:
        if isinstance(tok, str):
            if tok:
                children.append(make_paragraph([make_text(tok)]))
            continue
        translated = _translate_block(tok)
        if translated is not None:
            children.append(translated)
    return make_root(children)


def _translate_block(tok: dict[str, Any]) -> Content | None:
    t = tok.get("type")
    if t == "blank_line":
        return None
    if t == "paragraph":
        return make_paragraph(_translate_inline_children(tok))
    if t == "heading":
        depth = int(tok.get("attrs", {}).get("level", 1))
        return make_heading(depth, _translate_inline_children(tok))
    if t == "thematic_break":
        return make_thematic_break()
    if t == "block_code":
        attrs = tok.get("attrs", {}) or {}
        info = attrs.get("info")
        lang = info.split()[0] if isinstance(info, str) and info.strip() else None
        return make_code(tok.get("raw", ""), lang=lang)
    if t == "block_quote":
        children = [_translate_block(c) for c in tok.get("children", [])]
        return make_blockquote([c for c in children if c is not None])
    if t == "list":
        attrs = tok.get("attrs", {}) or {}
        ordered = bool(attrs.get("ordered"))
        start = int(attrs.get("start", 1)) if ordered else 1
        items = [_translate_block(c) for c in tok.get("children", [])]
        items = [c for c in items if c is not None]
        return make_list(items, ordered=ordered, start=start)
    if t == "list_item":
        children = [_translate_block(c) for c in tok.get("children", [])]
        return make_list_item([c for c in children if c is not None])
    if t == "block_text":
        # Loose-list paragraph payload; mistune emits raw inline text.
        return make_paragraph(_translate_inline_children(tok))
    if t == "table":
        return _translate_table(tok)
    # Unknown block: render as a paragraph carrying its raw text so we
    # don't silently drop content. The bake-off harness will flag this.
    raw = tok.get("raw", "")
    if raw:
        return make_paragraph([make_text(raw)])
    return None


def _translate_table(tok: dict[str, Any]) -> Content:
    rows: list[Content] = []
    align: list[str | None] = []
    for child in tok.get("children", []):
        ctype = child.get("type")
        if ctype == "table_head":
            cells, head_align = _translate_table_row(child)
            rows.append(make_table_row(cells))
            align = head_align
        elif ctype == "table_body":
            for row in child.get("children", []):
                if row.get("type") == "table_row":
                    cells, _ = _translate_table_row(row)
                    rows.append(make_table_row(cells))
    return make_table(rows, align=align if any(align) else None)


def _translate_table_row(row: dict[str, Any]) -> tuple[list[Content], list[str | None]]:
    cells: list[Content] = []
    aligns: list[str | None] = []
    for cell in row.get("children", []):
        if cell.get("type") not in ("table_cell",):
            continue
        attrs = cell.get("attrs", {}) or {}
        align_val = attrs.get("align")
        aligns.append(align_val if align_val in ("left", "center", "right") else None)
        cells.append(make_table_cell(_translate_inline_children(cell)))
    return cells, aligns


def _translate_inline_children(tok: dict[str, Any]) -> list[Content]:
    children = tok.get("children")
    if children is None:
        # mistune defers inline parsing for some tokens (e.g. headings
        # built from setext logic). Parse the raw text now.
        raw = tok.get("raw", "")
        if not raw:
            return []
        children = _MD.inline.parse(raw, mistune.BlockState())  # type: ignore[arg-type]
    out: list[Content] = []
    for child in children or []:
        translated = _translate_inline(child)
        if translated is not None:
            out.extend(translated) if isinstance(translated, list) else out.append(translated)
    return out


def _translate_inline(tok: dict[str, Any]) -> Content | list[Content] | None:
    t = tok.get("type")
    if t == "text":
        return make_text(tok.get("raw", ""))
    if t == "softbreak":
        return make_text("\n")
    if t == "linebreak":
        return make_break()
    if t == "codespan":
        return make_inline_code(tok.get("raw", ""))
    if t in ("strong", "emphasis", "delete", "strikethrough"):
        kids = _translate_inline_children(tok)
        if t == "strong":
            return make_strong(kids)
        if t == "emphasis":
            return make_emphasis(kids)
        return make_delete(kids)
    if t == "link":
        attrs = tok.get("attrs", {}) or {}
        url = attrs.get("url", "")
        title = attrs.get("title")
        return make_link(url, _translate_inline_children(tok), title=title)
    if t == "image":
        attrs = tok.get("attrs", {}) or {}
        url = attrs.get("url", "")
        title = attrs.get("title")
        # mistune nests alt as inline children; flatten to plain string.
        alt = "".join(_extract_text(c) for c in tok.get("children", []) or [])
        return make_image(url, alt=alt, title=title)
    if t == "inline_html":
        # mdast has html nodes; the existing hand-rolled parser doesn't
        # emit them. Surface as plain text for parity with the baseline.
        return make_text(tok.get("raw", ""))
    raw = tok.get("raw")
    if raw:
        return make_text(raw)
    return None


def _extract_text(node: dict[str, Any]) -> str:
    if node.get("type") == "text":
        return node.get("raw", "")
    children = node.get("children") or []
    return "".join(_extract_text(c) for c in children)
