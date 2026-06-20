"""Tests for the Telegram rich-message renderer.

Ports ``packages/adapter-telegram/src/rich.test.ts`` (chat@4.31.0, commit
4662309) plus adversarial coverage for the substitution / regex passes:
a full-punctuation escape sweep, all-backtick fence run-sizing, and a
surrogate-pair (non-BMP) truncation-boundary check.

Fixtures are annotated to the concrete TG1 rich-block / rich-text TypedDict
variants so the file stays pyrefly-sound.
"""

from __future__ import annotations

from chat_sdk.adapters.telegram.rich import (
    TELEGRAM_RICH_MESSAGE_LIMIT,
    RichMedia,
    rich_message_media,
    rich_message_to_markdown,
    rich_message_to_text,
    truncate_rich_markdown,
)
from chat_sdk.adapters.telegram.rich import (
    _escape_text as escape_text,
)
from chat_sdk.adapters.telegram.rich import (
    _inline_code as inline_code,
)
from chat_sdk.adapters.telegram.types import (
    TelegramPhotoSize,
    TelegramRichBlockAnimation,
    TelegramRichBlockHeading,
    TelegramRichBlockList,
    TelegramRichBlockPhoto,
    TelegramRichBlockPre,
    TelegramRichBlockPullquote,
    TelegramRichBlockTable,
    TelegramRichBlockText,
    TelegramRichCell,
    TelegramRichItem,
    TelegramRichMessage,
    TelegramRichText,
    TelegramRichTextHashtag,
    TelegramRichTextMention,
    TelegramRichTextStyled,
    TelegramRichTextUrl,
)
from chat_sdk.shared.markdown_parser import ast_to_plain_text, parse_markdown


def test_normalizes_structured_rich_blocks_to_markdown() -> None:
    """Headings, links, bold spans, and tables render to Markdown."""
    heading: TelegramRichBlockHeading = {"type": "heading", "size": 2, "text": "Summary"}
    guide_url: TelegramRichTextUrl = {
        "type": "url",
        "text": "the guide",
        "url": "https://example.com",
    }
    continue_bold: TelegramRichTextStyled = {"type": "bold", "text": "continue"}
    paragraph_text: TelegramRichText = ["Read ", guide_url, " and ", continue_bold]
    paragraph: TelegramRichBlockText = {"type": "paragraph", "text": paragraph_text}
    header_row: list[TelegramRichCell] = [
        {"align": "left", "is_header": True, "text": "Name", "valign": "top"},
        {"align": "right", "is_header": True, "text": "Status", "valign": "top"},
    ]
    body_row: list[TelegramRichCell] = [
        {"align": "left", "text": "Build", "valign": "top"},
        {"align": "right", "text": "Ready", "valign": "top"},
    ]
    table: TelegramRichBlockTable = {"type": "table", "cells": [header_row, body_row]}
    message: TelegramRichMessage = {"blocks": [heading, paragraph, table]}

    assert rich_message_to_markdown(message) == "\n\n".join(
        [
            "## Summary",
            "Read [the guide](<https://example.com>) and **continue**",
            "| Name | Status |\n| --- | --- |\n| Build | Ready |",
        ]
    )
    assert "Read the guide and continue" in rich_message_to_text(message)


def test_preserves_displayed_text_for_detected_entities() -> None:
    """Mention/hashtag entities keep their displayed text through escape + parse."""
    mention: TelegramRichTextMention = {
        "type": "mention",
        "text": "@chat_sdk",
        "username": "chat_sdk",
    }
    hashtag: TelegramRichTextHashtag = {
        "type": "hashtag",
        "hashtag": "release",
        "text": "#release",
    }
    paragraph_text: TelegramRichText = [mention, " ", hashtag]
    paragraph: TelegramRichBlockText = {"type": "paragraph", "text": paragraph_text}
    message: TelegramRichMessage = {"blocks": [paragraph]}

    assert rich_message_to_text(message) == "@chat_sdk #release"
    assert ast_to_plain_text(parse_markdown(rich_message_to_markdown(message))) == "@chat_sdk #release"


def test_escapes_literal_rich_text_without_changing_displayed_content() -> None:
    """Inline code, links, literal markdown, and fenced code escape correctly.

    Asserts on the renderer's emitted Markdown string (the load-bearing,
    char-for-char port). Upstream's CommonMark round-trip via
    ``mdast-util-from-markdown`` is not replicated here because the Python
    markdown parser is intentionally not full CommonMark (it does not handle
    angle-bracket link destinations or fence widening); see
    ``docs/SELF_REVIEW.md`` / CLAUDE.md "Known Limitations".
    """
    code_span: TelegramRichTextStyled = {"type": "code", "text": " a ` b "}
    empty_code: TelegramRichTextStyled = {"type": "code", "text": ""}
    link_span: TelegramRichTextUrl = {
        "type": "url",
        "text": "label ] x",
        "url": "https://example.com/a_(b)",
    }
    paragraph_text: TelegramRichText = [
        code_span,
        empty_code,
        " ",
        link_span,
        "\n# literal\n- item\n~~plain~~",
    ]
    paragraph: TelegramRichBlockText = {"type": "paragraph", "text": paragraph_text}
    pre: TelegramRichBlockPre = {
        "type": "pre",
        "language": "type`script",
        "text": "const fence = ```;",
    }
    message: TelegramRichMessage = {"blocks": [paragraph, pre]}

    markdown = rich_message_to_markdown(message)

    # Inline code: backtick run of 1 -> 2-backtick fence; boundary spaces pad.
    assert "``  a ` b  ``" in markdown
    # Empty inline code renders to nothing (no stray fence).
    assert "````" not in markdown.split("\n\n")[0]
    # Link: display text has its ``]`` escaped, url wrapped in angle brackets.
    assert "[label \\] x](<https://example.com/a_(b)>)" in markdown
    # Literal markdown is backslash-escaped, not interpreted.
    assert "\\# literal\n\\- item\n\\~\\~plain\\~\\~" in markdown
    # Pre block: inner ``` run (3) widens the fence to 4, language sanitized
    # (backticks stripped from ``type`script`` -> ``typescript``).
    assert markdown.endswith("````typescript\nconst fence = ```;\n````")


def test_normalizes_rich_formatting_to_plain_text() -> None:
    """Styled spans and tables collapse to tab/newline-joined plain text."""
    underline: TelegramRichTextStyled = {"type": "underline", "text": "underlined"}
    subscript: TelegramRichTextStyled = {"type": "subscript", "text": "subscript"}
    marked: TelegramRichTextStyled = {"type": "marked", "text": "marked"}
    paragraph_text: TelegramRichText = [underline, " ", subscript, " ", marked]
    paragraph: TelegramRichBlockText = {"type": "paragraph", "text": paragraph_text}
    header_row: list[TelegramRichCell] = [
        {"align": "left", "text": "Name", "valign": "top"},
        {"align": "left", "text": "Status", "valign": "top"},
    ]
    body_row: list[TelegramRichCell] = [
        {"align": "left", "text": "Build", "valign": "top"},
        {"align": "left", "text": "Ready", "valign": "top"},
    ]
    table: TelegramRichBlockTable = {"type": "table", "cells": [header_row, body_row]}
    message: TelegramRichMessage = {"blocks": [paragraph, table]}

    assert rich_message_to_text(message) == "underlined subscript marked\n\nName\tStatus\nBuild\tReady"


def test_truncates_markdown_at_the_rich_message_limit() -> None:
    """Over-limit markdown is truncated to the limit and ends with ``...``."""
    markdown = truncate_rich_markdown("a" * (TELEGRAM_RICH_MESSAGE_LIMIT + 100))

    assert len(markdown) <= TELEGRAM_RICH_MESSAGE_LIMIT
    assert markdown.endswith("...")


def test_preserves_a_table_like_trailing_line_when_truncating() -> None:
    """A trailing table-like line survives the renderer's truncation pass."""
    prefix = f"{'a' * (TELEGRAM_RICH_MESSAGE_LIMIT - 12)}\n| tail |"
    markdown = f"{prefix}{'b' * 100}"

    assert "| tail |" in truncate_rich_markdown(markdown)


# ---------------------------------------------------------------------------
# Media extraction (richMessageMedia)
# ---------------------------------------------------------------------------


def test_extracts_media_and_picks_largest_photo() -> None:
    """``rich_message_media`` recurses lists and picks the last photo size."""
    small: TelegramPhotoSize = {
        "file_id": "small",
        "file_unique_id": "s",
        "height": 10,
        "width": 10,
    }
    large: TelegramPhotoSize = {
        "file_id": "large",
        "file_unique_id": "l",
        "height": 200,
        "width": 300,
    }
    photo_block: TelegramRichBlockPhoto = {"type": "photo", "photo": [small, large]}
    item: TelegramRichItem = {"label": "1", "blocks": [photo_block]}
    list_block: TelegramRichBlockList = {"type": "list", "items": [item]}
    message: TelegramRichMessage = {"blocks": [list_block]}

    media = rich_message_media(message)
    assert media == [RichMedia(file=large, type="image", height=200, width=300)]


def test_animation_image_mime_maps_to_image_type() -> None:
    """An animation with an ``image/*`` mime is classified as ``image``."""
    gif: TelegramRichBlockAnimation = {
        "type": "animation",
        "animation": {
            "file_id": "gif",
            "file_unique_id": "g",
            "duration": 1,
            "height": 5,
            "width": 6,
            "mime_type": "image/gif",
            "file_name": "a.gif",
        },
    }
    mp4: TelegramRichBlockAnimation = {
        "type": "animation",
        "animation": {
            "file_id": "mp4",
            "file_unique_id": "m",
            "duration": 1,
            "height": 5,
            "width": 6,
            "mime_type": "video/mp4",
        },
    }
    message: TelegramRichMessage = {"blocks": [gif, mp4]}

    media = rich_message_media(message)
    assert [m.type for m in media] == ["image", "video"]


# ---------------------------------------------------------------------------
# Adversarial: escape char-class sweep
# ---------------------------------------------------------------------------

# The four ASCII punctuation ranges of /[!-/:-@[-`{-~]/ enumerated explicitly.
PUNCTUATION_IN_CLASS = (
    "".join(chr(c) for c in range(ord("!"), ord("/") + 1))
    + "".join(chr(c) for c in range(ord(":"), ord("@") + 1))
    + "".join(chr(c) for c in range(ord("["), ord("`") + 1))
    + "".join(chr(c) for c in range(ord("{"), ord("~") + 1))
)


def test_escape_sweep_every_in_class_char_is_backslash_escaped() -> None:
    """Every punctuation char in the class is escaped to ``\\<char>``."""
    for ch in PUNCTUATION_IN_CLASS:
        assert escape_text(ch) == f"\\{ch}", f"char {ch!r} should be escaped"


def test_escape_sweep_out_of_class_chars_are_untouched() -> None:
    """Letters, digits, whitespace, and space are left untouched."""
    # Build a set of chars deliberately OUTSIDE the punctuation class.
    out_of_class = (
        "abcXYZ0123456789"
        + " \t\n\r"
        + "".join(chr(c) for c in range(ord("0"), ord("9") + 1))
        + "éñ漢字😀"  # non-ASCII: Python Unicode regex must NOT escape these
    )
    for ch in out_of_class:
        assert escape_text(ch) == ch, f"char {ch!r} should NOT be escaped"


def test_escape_does_not_touch_the_space_between_punctuation_ranges() -> None:
    """The gaps between ranges (e.g. digits 0-9, uppercase A-Z) stay literal.

    The char class has holes: ``0``-``9`` sit between ``/`` and ``:``; ``A``-``Z``
    sit between ``@`` and ``[``; ``a``-``z`` sit between ``\\`` and ``{``. A
    wrong range bound would silently escape these.
    """
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
        assert escape_text(ch) == ch


# ---------------------------------------------------------------------------
# Adversarial: all-backtick inline-code fence run sizing
# ---------------------------------------------------------------------------


def test_inline_code_fence_grows_with_longest_backtick_run() -> None:
    """The fence marker is ``max(1, longest_run + 1)`` backticks long."""
    # No backticks -> minimal 1-backtick fence.
    assert inline_code("x") == "`x`"
    # One backtick -> fence of 2.
    assert inline_code("a`b") == "``a`b``"
    # A run of three backticks -> fence of 4.
    assert inline_code("a```b") == "````a```b````"
    # Two separate runs (1 and 3): the longest (3) wins -> fence of 4.
    assert inline_code("a`b```c") == "````a`b```c````"


def test_inline_code_pads_when_value_touches_a_backtick() -> None:
    """Boundary backticks/spaces trigger one space of padding each side."""
    # Leading backtick -> pad both sides.
    assert inline_code("`x") == "`` `x ``"
    # Pure spaces around content -> pad.
    assert inline_code(" x ") == "`  x  `"
    # Empty value renders to empty string (no fence).
    assert inline_code("") == ""


# ---------------------------------------------------------------------------
# Adversarial: surrogate-pair / non-BMP truncation boundary
# ---------------------------------------------------------------------------


def test_truncation_never_splits_a_non_bmp_code_point() -> None:
    """Truncating a long run of emoji cuts on code-point boundaries only.

    Each emoji is a single Python code point (matching JS ``Array.from``),
    so the truncated output must contain only whole emoji plus the ``...``
    suffix -- never a lone surrogate or a partial code point.
    """
    emoji = "😀"
    markdown = emoji * (TELEGRAM_RICH_MESSAGE_LIMIT + 50)

    result = truncate_rich_markdown(markdown)

    assert len(result) <= TELEGRAM_RICH_MESSAGE_LIMIT
    assert result.endswith("...")
    body = result[:-3]
    # Body is non-empty and composed solely of whole emoji code points.
    assert body
    assert set(body) == {emoji}
    # The body is an exact multiple of the (single) emoji code point: no
    # partial code point or lone surrogate could survive a code-point slice.
    assert body == emoji * len(body)
    # No lone surrogate code units leaked in (Python strings never hold them
    # from a slice, but assert the invariant explicitly).
    assert all(not (0xD800 <= ord(c) <= 0xDFFF) for c in result)


def test_truncation_passes_through_under_limit_text_unchanged() -> None:
    """At or under the limit, the input is returned verbatim."""
    text = "😀" * 5
    assert truncate_rich_markdown(text) == text
    assert truncate_rich_markdown("plain") == "plain"


# ---------------------------------------------------------------------------
# Fidelity: empty-LIST rich-text field renders (JS arrays are truthy)
# ---------------------------------------------------------------------------


def test_empty_list_credit_renders_like_a_present_credit() -> None:
    """An empty-list ``credit`` ([]) renders, matching JS array truthiness.

    Upstream gates the credit with ``value.credit ? ... : ""``; a JS array is
    ALWAYS truthy, so ``credit: []`` produces a blank credit (a leading-newline
    empty line in the quote), whereas absent / empty-string skip it. A bare
    Python-truthiness gate (``if value.get("credit")``) would wrongly skip
    ``[]`` -- this test fails under that regression.
    """
    # credit=[] -> truthy in JS -> credit segment "\n\n" + text([]) ("") renders.
    # quote("Quote\n\n") -> "> Quote\n> \n> " -> trailing ws trimmed by .strip().
    with_empty_list: TelegramRichBlockPullquote = {"type": "pullquote", "text": "Quote", "credit": []}
    assert rich_message_to_markdown({"blocks": [with_empty_list]}) == "> Quote\n> \n>"

    # Absent credit -> JS undefined is falsy -> skipped entirely.
    absent: TelegramRichBlockPullquote = {"type": "pullquote", "text": "Quote"}
    assert rich_message_to_markdown({"blocks": [absent]}) == "> Quote"

    # Empty-string credit -> JS "" is falsy -> skipped (the ONLY falsy RichText).
    empty_string: TelegramRichBlockPullquote = {"type": "pullquote", "text": "Quote", "credit": ""}
    assert rich_message_to_markdown({"blocks": [empty_string]}) == "> Quote"

    # A real credit span renders the same blank-line-separated shape as [].
    with_author: TelegramRichBlockPullquote = {"type": "pullquote", "text": "Quote", "credit": "Author"}
    assert rich_message_to_markdown({"blocks": [with_author]}) == "> Quote\n> \n> Author"


# ---------------------------------------------------------------------------
# Mutation coverage: fence default width, truncation reserve, exact boundary
# ---------------------------------------------------------------------------


def test_pre_block_without_internal_backticks_emits_a_three_backtick_fence() -> None:
    """A code/pre block with NO internal backticks uses the default 3-fence.

    ``_code_block`` sizes the fence as ``max(3, longest_run + 1)`` and falls
    back to ``3`` when there are no backtick runs. This pins the no-run default:
    the mutations ``else 3`` -> ``else 2`` and ``max(3, ...)`` -> ``max(2, ...)``
    both shrink the fence to 2 backticks and fail this assertion.
    """
    fence = "`" * 3
    pre: TelegramRichBlockPre = {"type": "pre", "language": "python", "text": "hello world"}
    markdown = rich_message_to_markdown({"blocks": [pre]})

    assert markdown == f"{fence}python\nhello world\n{fence}"
    # Exactly three opening backticks -- never two.
    assert len(markdown) - len(markdown.lstrip("`")) == 3


def test_truncation_reserves_room_for_the_ellipsis() -> None:
    """Truncation reserves 3 chars for ``...`` so the result stays at the limit.

    With an unclosed bold marker straddling the boundary, the correct reserve
    (``end = LIMIT - 3``) returns a result of length EXACTLY ``LIMIT`` on the
    first iteration. The mutation ``end = LIMIT`` over-shoots, the shrink loop
    diverges, and it returns a shorter (length ``LIMIT - 2``) result -- so an
    exact-length assertion fails under the mutation.
    """
    # 'a' x (LIMIT-1) + '**' opens an unclosed bold span right at the boundary;
    # the trailing run keeps the input over the limit.
    over_limit = ("a" * (TELEGRAM_RICH_MESSAGE_LIMIT - 1)) + "**" + ("b" * 200)
    result = truncate_rich_markdown(over_limit)

    # The ellipsis fits within the limit -- length is exactly the limit here,
    # never over it (mutation would yield LIMIT - 2, killing the equality).
    assert len(result) == TELEGRAM_RICH_MESSAGE_LIMIT
    assert result.endswith("...")


def test_exact_limit_length_passes_through_unchanged() -> None:
    """A string of length EXACTLY ``LIMIT`` is returned verbatim (no ``...``).

    The early-return guard is ``len(characters) <= LIMIT``. At the boundary
    ``len == LIMIT`` the input must pass through; the mutation ``<=`` -> ``<``
    would instead truncate it (appending ``...``), failing this assertion.
    """
    exact = "a" * TELEGRAM_RICH_MESSAGE_LIMIT
    result = truncate_rich_markdown(exact)

    assert result == exact
    assert not result.endswith("...")
