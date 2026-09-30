"""Code-fence normalizer for Slack-style ``` fences.

Port of ``packages/adapter-shared/src/code-fences.ts`` (chat@4.41.1).

Platforms whose markdown-like formats use Slack-style ``` fences treat text
immediately after the opening fence as code, while CommonMark treats it as
the fence's info string, silently dropping the first line. Rewriting each
paired fence onto its own lines preserves the content through a CommonMark
parser.

Everything the platform renders literally stays plain text: an unpaired
```, a ``` inside an inline code span, and a ``` on a blockquote line. Text
that a closing fence pushes onto a new line gets its leading block marker
escaped so CommonMark cannot promote it to a blockquote, heading, list, or
nested fence.

Porting notes: the patterns use ``\\Z`` (JS ``$`` without the ``m`` flag;
Python's ``$`` also matches before a trailing newline) and ``[0-9]`` (Python's
``\\d`` matches every Unicode digit). ``trimStart`` uses the JS whitespace set.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from chat_sdk.shared._js_compat import JS_WHITESPACE

_CODE_FENCE = "```"
LEADING_WHITESPACE_PATTERN = re.compile(r"^[ \t]+")
# Line prefixes CommonMark promotes to a block construct (blockquote,
# heading, list item, fence, thematic break, HTML).
BLOCK_MARKER_PATTERN = re.compile(
    r"^(?:[<>]|#{1,6}(?=[ \t\n]|\Z)|[-+*](?=[ \t\n]|\Z)|`{3,}|~{3,}|(?:[-*_][ \t]*){3,}(?=\n|\Z))"
)
ORDERED_LIST_MARKER_PATTERN = re.compile(r"^([0-9]{1,9})([.)])(?=[ \t\n]|\Z)")


def _pass_through(value: str) -> str:
    return value


def normalize_code_fences(
    text: str,
    *,
    convert_text: Callable[[str], str] | None = None,
    convert_code: Callable[[str], str] | None = None,
) -> str:
    """Put every paired ``` fence on its own line.

    ``convert_text`` converts non-code text segments to standard Markdown;
    ``convert_code`` converts fenced code content (e.g. resolves platform
    tokens) and must not apply emphasis or other text-level rewrites, so code
    stays verbatim. Both default to the identity.
    """
    text_converter = convert_text if convert_text is not None else _pass_through
    code_converter = convert_code if convert_code is not None else _pass_through
    if _CODE_FENCE not in text:
        return text_converter(text)

    parts: list[str] = []
    # ``result.endsWith("\n")``/``result.length`` without re-joining ``parts``.
    result_len = 0
    result_ends_with_newline = False
    text_start = 0
    cursor = 0
    # Set when the closing fence splits a line: the text after it lands at
    # the start of a new line, where CommonMark would promote a leading
    # block marker the platform rendered inline.
    moved_to_own_line = False

    def append(value: str) -> None:
        nonlocal result_len, result_ends_with_newline
        if value:
            parts.append(value)
            result_len += len(value)
            result_ends_with_newline = value.endswith("\n")

    def flush_text_before(end: int) -> None:
        nonlocal moved_to_own_line
        segment = text[text_start:end]
        if moved_to_own_line:
            segment = _escape_leading_block_marker(segment)
            moved_to_own_line = False
        append(text_converter(segment))

    length = len(text)
    while cursor < length:
        if text[cursor] != "`":
            cursor += 1
            continue
        if not text.startswith(_CODE_FENCE, cursor):
            span_end = _find_inline_code_end(text, cursor)
            cursor = cursor + 1 if span_end == -1 else span_end
            continue

        content_start = cursor + len(_CODE_FENCE)
        content_end = text.find(_CODE_FENCE, content_start)
        if content_end == -1 or _is_on_blockquote_line(text, cursor):
            # The platform renders an unpaired or quoted ``` literally.
            cursor = content_start
            continue

        flush_text_before(cursor)
        if result_len > 0 and not result_ends_with_newline:
            append("\n")
        content = text[content_start:content_end]
        append(_CODE_FENCE)
        if not content.startswith("\n"):
            append("\n")
        append(code_converter(content))
        if not content.endswith("\n"):
            append("\n")
        append(_CODE_FENCE)

        cursor = content_end + len(_CODE_FENCE)
        text_start = cursor
        if cursor < length and text[cursor] != "\n":
            append("\n")
            moved_to_own_line = True

    flush_text_before(length)
    return "".join(parts)


# Platform inline code spans never cross line breaks.
def _find_inline_code_end(text: str, index: int) -> int:
    close = text.find("`", index + 1)
    if close == -1:
        return -1
    newline = text.find("\n", index + 1)
    if newline != -1 and newline < close:
        return -1
    return close + 1


def _is_on_blockquote_line(text: str, index: int) -> bool:
    line_start = text.rfind("\n", 0, index) + 1
    return text[line_start:index].lstrip(JS_WHITESPACE).startswith(">")


def _escape_leading_block_marker(text: str) -> str:
    match = LEADING_WHITESPACE_PATTERN.match(text)
    whitespace = match.group(0) if match else ""
    # Collapse the leading separator so it cannot become an indented code
    # block, then defuse any block marker now sitting at the line start.
    prefix = " " if whitespace else ""
    rest = text[len(whitespace) :]
    if BLOCK_MARKER_PATTERN.match(rest):
        return f"{prefix}\\{rest}"
    return prefix + ORDERED_LIST_MARKER_PATTERN.sub(r"\1\\\2", rest, count=1)
