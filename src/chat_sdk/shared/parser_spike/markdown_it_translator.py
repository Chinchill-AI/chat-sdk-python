"""markdown-it-py (4.x) -> mdast translator.

markdown-it tokenises into a flat list of ``Token`` objects (each with
``type``, ``tag``, ``content``, ``children``, ``markup``, ``attrs``,
``meta``). Block-level constructs use ``_open`` / ``_close`` pairs and
must be folded into a tree. Inline tokens (under ``inline`` parents)
are already nested.

GFM features (tables, strikethrough) are enabled by selecting the
``gfm-like`` preset and adding the strikethrough rule explicitly.
Task-list rendering would require ``mdit-py-plugins`` (deferred).
"""

from __future__ import annotations

from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

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

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


def parse_markdown(text: str) -> Root:
    tokens = _MD.parse(text)
    children, _ = _consume_blocks(tokens, 0, end_type=None)
    return make_root(children)


def _consume_blocks(tokens: list[Token], i: int, end_type: str | None) -> tuple[list[Content], int]:
    """Walk tokens until we hit *end_type* (or end of list). Return the
    list of mdast block children produced and the index after the closer.
    """
    children: list[Content] = []
    while i < len(tokens):
        tok = tokens[i]
        if end_type is not None and tok.type == end_type:
            return children, i + 1

        if tok.type == "paragraph_open":
            inline = tokens[i + 1]
            children.append(make_paragraph(_translate_inline(inline.children or [])))
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        if tok.type == "heading_open":
            depth = int(tok.tag[1])  # h1 -> 1, h2 -> 2, ...
            inline = tokens[i + 1]
            children.append(make_heading(depth, _translate_inline(inline.children or [])))
            i += 3
            continue

        if tok.type == "hr":
            children.append(make_thematic_break())
            i += 1
            continue

        if tok.type == "fence":
            lang = tok.info.split()[0] if tok.info and tok.info.strip() else None
            value = tok.content.rstrip("\n")
            children.append(make_code(value, lang=lang))
            i += 1
            continue

        if tok.type == "code_block":
            children.append(make_code(tok.content.rstrip("\n"), lang=None))
            i += 1
            continue

        if tok.type == "blockquote_open":
            inner, i = _consume_blocks(tokens, i + 1, "blockquote_close")
            children.append(make_blockquote(inner))
            continue

        if tok.type == "bullet_list_open":
            items, i = _consume_list(tokens, i + 1, "bullet_list_close")
            children.append(make_list(items, ordered=False))
            continue

        if tok.type == "ordered_list_open":
            start = int((tok.attrs or {}).get("start", 1))
            items, i = _consume_list(tokens, i + 1, "ordered_list_close")
            children.append(make_list(items, ordered=True, start=start))
            continue

        if tok.type == "table_open":
            table, i = _consume_table(tokens, i + 1)
            children.append(table)
            continue

        # Unknown / unhandled token: skip but don't crash.
        i += 1

    return children, i


def _consume_list(tokens: list[Token], i: int, end_type: str) -> tuple[list[Content], int]:
    items: list[Content] = []
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == end_type:
            return items, i + 1
        if tok.type == "list_item_open":
            inner, i = _consume_blocks(tokens, i + 1, "list_item_close")
            items.append(make_list_item(inner))
            continue
        i += 1
    return items, i


def _consume_table(tokens: list[Token], i: int) -> tuple[Content, int]:
    rows: list[Content] = []
    in_header = False
    header_aligns: list[str | None] = []
    current_row: list[Content] = []
    current_aligns: list[str | None] = []

    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "table_close":
            return make_table(rows, align=header_aligns if any(header_aligns) else None), i + 1
        if tok.type == "thead_open":
            in_header = True
        elif tok.type == "thead_close":
            in_header = False
        elif tok.type == "tr_open":
            current_row = []
            current_aligns = []
        elif tok.type == "tr_close":
            rows.append(make_table_row(current_row))
            if in_header:
                header_aligns = current_aligns
        elif tok.type in ("th_open", "td_open"):
            style = (tok.attrs or {}).get("style", "")
            cell_align: str | None = None
            if isinstance(style, str):
                if "text-align:left" in style:
                    cell_align = "left"
                elif "text-align:center" in style:
                    cell_align = "center"
                elif "text-align:right" in style:
                    cell_align = "right"
            current_aligns.append(cell_align)
            inline = tokens[i + 1]
            current_row.append(make_table_cell(_translate_inline(inline.children or [])))
            i += 3  # th/td_open, inline, th/td_close
            continue
        i += 1
    return make_table(rows, align=header_aligns if any(header_aligns) else None), i


def _translate_inline(tokens: list[Token]) -> list[Content]:
    out: list[Content] = []
    # Each stack frame holds (parent_list, meta) -- meta is None for plain
    # containers (strong/emphasis/delete) and a (href, title) tuple for
    # links. Using a tuple instead of pipe-stuffing a string sidesteps the
    # fragility of URLs/titles that contain pipe characters.
    stack: list[tuple[list[Content], tuple[str, str | None] | None]] = []
    current = out

    def open_container() -> None:
        nonlocal current
        new_children: list[Content] = []
        stack.append((current, None))
        current = new_children

    def close_container(make: Any) -> None:
        nonlocal current
        kids = current
        parent, _meta = stack.pop()
        current = parent
        current.append(make(kids))

    for tok in tokens:
        t = tok.type
        if t == "text":
            current.append(make_text(tok.content))
        elif t == "softbreak":
            current.append(make_text("\n"))
        elif t == "hardbreak":
            current.append(make_break())
        elif t == "code_inline":
            current.append(make_inline_code(tok.content))
        elif t == "strong_open":
            open_container()
        elif t == "strong_close":
            close_container(make_strong)
        elif t == "em_open":
            open_container()
        elif t == "em_close":
            close_container(make_emphasis)
        elif t == "s_open":
            open_container()
        elif t == "s_close":
            close_container(make_delete)
        elif t == "link_open":
            attrs = tok.attrs or {}
            href = str(attrs.get("href", ""))
            raw_title = attrs.get("title")
            title: str | None = str(raw_title) if raw_title is not None else None
            link_children: list[Content] = []
            stack.append((current, (href, title)))
            current = link_children
        elif t == "link_close":
            kids = current
            parent, meta = stack.pop()
            current = parent
            href, title = meta if meta else ("", None)
            current.append(make_link(href, kids, title=title or None))
        elif t == "image":
            attrs = tok.attrs or {}
            url = attrs.get("src", "")
            raw_title = attrs.get("title")
            title = str(raw_title) if raw_title is not None else None
            alt = tok.content  # markdown-it precomputes alt text
            current.append(make_image(str(url), alt=alt, title=title))
        elif t == "html_inline":
            current.append(make_text(tok.content))
        else:
            if tok.content:
                current.append(make_text(tok.content))
    return out
