"""Bare ``@mention`` resolver shared across adapters.

Port of ``packages/adapter-shared/src/mentions.ts`` (chat@4.41.1).

Converting a bare ``@name`` into a platform mention with a naive
``re.sub(r"@(\\w+)", ...)`` mangles surrounding text: it rewrites email
addresses (``user@example.com``), ``@handles`` inside URLs
(``https://github.com/@org``), and mentions inside code spans.
:func:`replace_bare_mentions` scans the text character-by-character and skips:

- inline code and fenced code (`` `...` `` / `````` ``` ``````)
- URLs with a scheme (``http://...``, ``https://...``; case-insensitive)
- schemeless hosts followed by a path (``example.com/...``)
- existing angle-bracket tokens (``<@123>``, ``<at>...</at>``, ``<url|label>``)

Only an ``@`` at a word boundary that is followed by a word character is
handed to the ``replacer``, which decides how to render it for the target
platform.

:func:`mask_code_spans` exposes the same code-span scanner on its own, for
adapters that classify an existing mention token and need to ignore the
ones inside code.

Porting notes (keep in sync with upstream):

- Character classes are ASCII-only, matching upstream's char-code checks
  (``é`` is not a word character, unlike Python's ``\\w``/``str.isalpha``).
- The boundary test uses the JS ``String.prototype.trim`` whitespace set,
  not ``str.strip()`` (see :mod:`chat_sdk.shared._js_compat`).
- JS reads past either end of a string as ``undefined``; every
  look-behind/look-ahead here is bounds-checked so ``text[-1]`` never wraps.
"""

from __future__ import annotations

from collections.abc import Callable

from chat_sdk.shared._js_compat import is_js_whitespace

MentionReplacer = Callable[[str, str], str]
"""``(mention, name) -> replacement``: ``mention`` is ``"@alice"``, ``name`` is ``"alice"``."""

_HTTP = "http://"
_HTTPS = "https://"


def _char_at(text: str, index: int) -> str | None:
    """JS ``text[index]``: ``None`` (``undefined``) outside ``[0, len)``."""
    if 0 <= index < len(text):
        return text[index]
    return None


def _is_letter(char: str | None) -> bool:
    return char is not None and ("A" <= char <= "Z" or "a" <= char <= "z")


def _is_number(char: str | None) -> bool:
    return char is not None and "0" <= char <= "9"


def _is_word(char: str | None) -> bool:
    return char is not None and (_is_letter(char) or _is_number(char) or char == "_")


def _is_host(char: str | None) -> bool:
    return _is_letter(char) or _is_number(char) or char == "." or char == "-"


def _is_boundary(char: str) -> bool:
    # JS: ``char === "<" || char === ">" || char.trim() === ""``
    return char == "<" or char == ">" or is_js_whitespace(char)


def _starts_with(text: str, index: int, value: str) -> bool:
    """Case-insensitive prefix test at *index* (``value`` is lowercase)."""
    return text[index : index + len(value)].lower() == value


def _find_url_end(text: str, index: int, end: int) -> int:
    prefix = 0
    if _starts_with(text, index, _HTTPS):
        prefix = len(_HTTPS)
    elif _starts_with(text, index, _HTTP):
        prefix = len(_HTTP)

    if prefix == 0 or index + prefix >= end:
        return index

    cursor = index + prefix
    while cursor < end and not _is_boundary(text[cursor]):
        cursor += 1
    return cursor


def _find_host_end(text: str, index: int, end: int) -> int:
    first = _char_at(text, index)
    if not (_is_letter(first) or _is_number(first)):
        return index
    if index > 0 and _is_host(text[index - 1]):
        return index

    cursor = index
    while cursor < end and _is_host(text[cursor]):
        cursor += 1

    separator = _char_at(text, cursor)
    if separator != "/" and separator != "?" and separator != "#":
        return index

    host = text[index:cursor]
    dot = host.rfind(".")
    suffix = host[dot + 1 :]
    if dot <= 0 or len(suffix) < 2:
        return index
    for char in suffix:
        if not _is_letter(char):
            return index

    cursor += 1
    while cursor < end and not _is_boundary(text[cursor]):
        cursor += 1
    return cursor


def _find_code_end(text: str, index: int, end: int) -> int:
    if _char_at(text, index) != "`":
        return index

    fence = text.startswith("```", index)
    marker = "```" if fence else "`"
    start = index + len(marker)
    close = text.find(marker, start)

    if close == -1 or close >= end:
        return index
    if not fence:
        newline = text.find("\n", start)
        if newline != -1 and newline < close:
            return index
    return close + len(marker)


def _replace_range(
    text: str,
    start: int,
    end: int,
    replacer: MentionReplacer,
    angles: bool,
) -> str:
    parts: list[str] = []
    index = start

    while index < end:
        code_end = _find_code_end(text, index, end)
        if code_end > index:
            parts.append(text[index:code_end])
            index = code_end
            continue

        if angles and text[index] == "<":
            cursor = index + 1
            while cursor < end and text[cursor] not in (">", "\n", "\r"):
                cursor += 1

            if _char_at(text, cursor) == ">":
                parts.append(text[index : cursor + 1])
                index = cursor + 1
                continue

            # Unclosed ``<``: scan the rest of the line without re-entering
            # angle handling, so the ``<`` itself is emitted literally.
            parts.append(_replace_range(text, index, cursor, replacer, False))
            index = cursor
            continue

        url_end = _find_url_end(text, index, end)
        if url_end > index:
            parts.append(text[index:url_end])
            index = url_end
            continue

        host_end = _find_host_end(text, index, end)
        if host_end > index:
            parts.append(text[index:host_end])
            index = host_end
            continue

        previous = _char_at(text, index - 1)
        if text[index] == "@" and previous != "<" and not _is_word(previous) and _is_word(_char_at(text, index + 1)):
            cursor = index + 2
            while cursor < end and _is_word(text[cursor]):
                cursor += 1
            mention = text[index:cursor]
            parts.append(replacer(mention, mention[1:]))
            index = cursor
            continue

        parts.append(text[index])
        index += 1

    return "".join(parts)


def replace_bare_mentions(text: str, replacer: MentionReplacer) -> str:
    """Replace every bare ``@name`` outside code, URLs and ``<...>`` tokens.

    ``replacer(mention, name)`` receives the full mention (``"@alice"``) and
    the bare name (``"alice"``); its return value is inserted verbatim.
    """
    return _replace_range(text, 0, len(text), replacer, True)


def mask_code_spans(text: str, replacement: str = " ") -> str:
    """Replace every inline code span and fenced code block with *replacement*.

    A scan of the result cannot read a token inside code as a mention. Uses
    the same span rules as :func:`replace_bare_mentions`: an unterminated
    fence and an inline span that crosses a line break are plain text, not
    code.
    """
    parts: list[str] = []
    index = 0
    end = len(text)

    while index < end:
        code_end = _find_code_end(text, index, end)
        if code_end > index:
            parts.append(replacement)
            index = code_end
            continue
        parts.append(text[index])
        index += 1

    return "".join(parts)
