"""marko (2.x) -> mdast translator.

marko parses to a class-based AST (``marko.block.Document`` etc.). Each
node exposes ``children`` (list[Node] or str payload). The GFM extension
adds tables, strikethrough, task lists, autolinks.
"""

from __future__ import annotations

import marko
from marko.ext.gfm import GFM

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

_MD = marko.Markdown(extensions=[GFM])


def parse_markdown(text: str) -> Root:
    doc = _MD.parse(text)
    children = [_translate(c) for c in getattr(doc, "children", [])]
    return make_root([c for c in children if c is not None])


def _translate(node: object) -> Content | None:
    cls = type(node).__name__

    if cls == "Paragraph":
        return make_paragraph(_inline_children(node))
    if cls == "Heading":
        depth = int(getattr(node, "level", 1))
        return make_heading(depth, _inline_children(node))
    if cls == "SetextHeading":
        depth = int(getattr(node, "level", 1))
        return make_heading(depth, _inline_children(node))
    if cls == "ThematicBreak":
        return make_thematic_break()
    if cls in ("FencedCode", "CodeBlock"):
        lang = getattr(node, "lang", None) or None
        value = _gather_code_text(node)
        return make_code(value, lang=lang)
    if cls == "Quote":
        return make_blockquote(_block_children(node))
    if cls == "List":
        ordered = bool(getattr(node, "ordered", False))
        start = int(getattr(node, "start", 1)) if ordered else 1
        return make_list(_block_children(node), ordered=ordered, start=start)
    if cls == "ListItem":
        return make_list_item(_block_children(node))
    if cls == "Table":
        return _translate_table(node)
    if cls == "BlankLine":
        return None
    if cls == "HTMLBlock":
        return make_paragraph([make_text(getattr(node, "body", "") or "")])
    # Fallback: stringify if we can.
    return None


def _block_children(node: object) -> list[Content]:
    out: list[Content] = []
    for child in getattr(node, "children", []) or []:
        translated = _translate(child)
        if translated is not None:
            out.append(translated)
    return out


def _inline_children(node: object) -> list[Content]:
    out: list[Content] = []
    children = getattr(node, "children", None)
    if isinstance(children, str):
        return [make_text(children)]
    for child in children or []:
        translated = _translate_inline(child)
        if translated is not None:
            out.extend(translated) if isinstance(translated, list) else out.append(translated)
    return out


def _translate_inline(node: object) -> Content | list[Content] | None:
    cls = type(node).__name__

    if cls == "RawText":
        value = getattr(node, "children", "")
        return make_text(value if isinstance(value, str) else "")
    if cls == "Literal":
        return make_text(getattr(node, "children", "") or "")
    if cls == "LineBreak":
        # marko exposes a ``soft`` flag on the line-break node.
        soft = bool(getattr(node, "soft", False))
        return make_text("\n") if soft else make_break()
    if cls == "InlineHTML":
        return make_text(getattr(node, "children", "") or "")
    if cls == "CodeSpan":
        value = getattr(node, "children", "")
        return make_inline_code(value if isinstance(value, str) else "")
    if cls == "Emphasis":
        return make_emphasis(_inline_children(node))
    if cls == "StrongEmphasis":
        return make_strong(_inline_children(node))
    if cls == "Strikethrough":
        return make_delete(_inline_children(node))
    if cls == "Link":
        url = getattr(node, "dest", "") or ""
        title = getattr(node, "title", None) or None
        return make_link(url, _inline_children(node), title=title)
    if cls in ("AutoLink", "Url"):
        url = getattr(node, "dest", "") or ""
        return make_link(url, _inline_children(node))
    if cls == "Image":
        url = getattr(node, "dest", "") or ""
        title = getattr(node, "title", None) or None
        alt = "".join(_extract_text(c) for c in getattr(node, "children", []) or [])
        return make_image(url, alt=alt, title=title)
    # Fallback: any unrecognized inline -> stringify children if any.
    value = getattr(node, "children", None)
    if isinstance(value, str):
        return make_text(value)
    return None


def _translate_table(node: object) -> Content:
    rows: list[Content] = []
    align: list[str | None] = list(getattr(node, "alignment", []) or [])
    # marko stores alignments as ["left", "center", "right", None].
    align = [a if a in ("left", "center", "right") else None for a in align]
    for row in getattr(node, "children", []) or []:
        cells: list[Content] = []
        for cell in getattr(row, "children", []) or []:
            cells.append(make_table_cell(_inline_children(cell)))
        rows.append(make_table_row(cells))
    return make_table(rows, align=align if any(align) else None)


def _extract_text(node: object) -> str:
    cls = type(node).__name__
    if cls == "RawText":
        v = getattr(node, "children", "")
        return v if isinstance(v, str) else ""
    children = getattr(node, "children", None)
    if isinstance(children, str):
        return children
    return "".join(_extract_text(c) for c in children or [])


def _gather_code_text(node: object) -> str:
    children = getattr(node, "children", None)
    if isinstance(children, str):
        return children
    parts: list[str] = []
    for c in children or []:
        v = getattr(c, "children", "")
        if isinstance(v, str):
            parts.append(v)
        else:
            parts.append(_extract_text(c))
    return "".join(parts)
