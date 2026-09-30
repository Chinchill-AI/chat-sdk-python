"""JavaScript string-semantics helpers shared by the ``chat_sdk.shared`` ports.

Upstream's text utilities lean on ``String.prototype.trim``/``trimStart``;
Python's bare ``str.strip()``/``str.lstrip()`` strip a different set:

- Python strips the C0 separators U+001C..U+001F and NEL U+0085, which JS
  keeps;
- JS strips the BOM U+FEFF, which Python keeps.

Passing :data:`JS_WHITESPACE` to ``strip``/``lstrip`` (or testing membership)
matches JS character-for-character.
"""

from __future__ import annotations

# ECMAScript WhiteSpace + LineTerminator: exactly what ``String.prototype.trim``
# removes.
JS_WHITESPACE = (
    "\t\n\v\f\r "  # TAB, LF, VT, FF, CR, SPACE
    " "  # NO-BREAK SPACE
    " "  # OGHAM SPACE MARK
    "           "  # EN QUAD..HAIR SPACE
    " "  # LINE SEPARATOR
    " "  # PARAGRAPH SEPARATOR
    " "  # NARROW NO-BREAK SPACE
    " "  # MEDIUM MATHEMATICAL SPACE
    "　"  # IDEOGRAPHIC SPACE
    "﻿"  # ZERO WIDTH NO-BREAK SPACE (BOM)
)

_JS_WHITESPACE_SET = frozenset(JS_WHITESPACE)


def is_js_whitespace(char: str) -> bool:
    """Return True when the single character *char* is JS whitespace."""
    return char in _JS_WHITESPACE_SET
