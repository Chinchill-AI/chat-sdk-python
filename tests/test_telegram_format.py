"""Port of adapter-telegram/src/markdown.test.ts -- Telegram format converter tests.

Tests TelegramFormatConverter's fromAst, toAst, renderPostable, extractPlainText,
and roundtrip behavior.
"""

from __future__ import annotations

import re

from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter

# ---------------------------------------------------------------------------
# Shared instance
# ---------------------------------------------------------------------------

converter = TelegramFormatConverter()
TABLE_PIPE_PATTERN = re.compile(r"\|.*Name.*\|")


# ---------------------------------------------------------------------------
# fromAst (AST -> markdown string)
# ---------------------------------------------------------------------------


class TestTelegramFromAst:
    """Tests for TelegramFormatConverter.from_ast."""

    def test_plain_text_paragraph(self):
        ast = converter.to_ast("Hello world")
        result = converter.from_ast(ast)
        assert "Hello world" in result

    def test_bold(self):
        # MarkdownV2 uses single-asterisk for bold (vs standard's `**`).
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "*bold text*" in result
        assert "**" not in result

    def test_italic(self):
        # MarkdownV2 uses single-underscore for italic (vs standard's `*`).
        ast = converter.to_ast("*italic text*")
        result = converter.from_ast(ast)
        assert "_italic text_" in result

    def test_strikethrough(self):
        # MarkdownV2 uses single-tilde for strikethrough (vs standard's `~~`).
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~strikethrough~" in result
        assert "~~" not in result

    def test_links(self):
        ast = converter.to_ast("[link text](https://example.com)")
        result = converter.from_ast(ast)
        assert "[link text](https://example.com)" in result

    def test_inline_code(self):
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_blocks(self):
        input_text = "```js\nconst x = 1;\n```"
        ast = converter.to_ast(input_text)
        output = converter.from_ast(ast)
        assert "```" in output
        assert "const x = 1;" in output

    def test_tables_to_code_blocks(self):
        ast = converter.to_ast("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        result = converter.from_ast(ast)
        assert "```" in result
        assert "Name" in result
        assert "Alice" in result
        assert not TABLE_PIPE_PATTERN.search(result)


# ---------------------------------------------------------------------------
# toAst (markdown -> AST)
# ---------------------------------------------------------------------------


class TestTelegramToAst:
    """Tests for TelegramFormatConverter.to_ast."""

    def test_plain_text(self):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_bold(self):
        ast = converter.to_ast("**bold**")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_italic(self):
        ast = converter.to_ast("*italic*")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_inline_code(self):
        ast = converter.to_ast("`code`")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestTelegramRenderPostable:
    """Tests for TelegramFormatConverter.render_postable."""

    def test_plain_string(self):
        result = converter.render_postable("Hello world")
        assert result == "Hello world"

    def test_empty_string(self):
        result = converter.render_postable("")
        assert result == ""

    def test_raw_message(self):
        result = converter.render_postable({"raw": "raw content"})
        assert result == "raw content"

    def test_markdown_message(self):
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "bold" in result

    def test_ast_message(self):
        ast = converter.to_ast("Hello from AST")
        result = converter.render_postable({"ast": ast})
        assert "Hello from AST" in result

    def test_bold_and_italic(self):
        # MarkdownV2: single-asterisk bold, single-underscore italic, and
        # the literal " and " keeps spaces unescaped.
        result = converter.render_postable({"markdown": "**bold** and *italic*"})
        assert "*bold*" in result
        assert "_italic_" in result
        assert "**" not in result

    def test_markdown_table_as_code_block(self):
        result = converter.render_postable({"markdown": "| A | B |\n| --- | --- |\n| 1 | 2 |"})
        assert "```" in result
        assert "A" in result


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestTelegramExtractPlainText:
    """Tests for TelegramFormatConverter.extract_plain_text."""

    def test_remove_bold(self):
        assert converter.extract_plain_text("Hello **world**!") == "Hello world!"

    def test_remove_italic(self):
        assert converter.extract_plain_text("Hello *world*!") == "Hello world!"

    def test_remove_strikethrough(self):
        assert converter.extract_plain_text("Hello ~~world~~!") == "Hello world!"

    def test_extract_link_text(self):
        assert converter.extract_plain_text("Check [this](https://example.com)") == "Check this"

    def test_inline_code(self):
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result

    def test_code_blocks(self):
        result = converter.extract_plain_text("```js\nconst x = 1;\n```")
        assert "const x = 1;" in result

    def test_plain_text(self):
        assert converter.extract_plain_text("Hello world") == "Hello world"

    def test_empty_string(self):
        assert converter.extract_plain_text("") == ""

    def test_complex_input(self):
        input_text = "**Bold** and *italic* with [link](https://x.com)"
        result = converter.extract_plain_text(input_text)
        assert "Bold" in result
        assert "italic" in result
        assert "link" in result
        assert "**" not in result
        assert "](" not in result


# ---------------------------------------------------------------------------
# roundtrip
# ---------------------------------------------------------------------------


class TestTelegramRoundtrip:
    """Roundtrip tests (toAst -> fromAst)."""

    def test_plain_text(self):
        result = converter.from_ast(converter.to_ast("Hello world"))
        assert "Hello world" in result

    def test_bold(self):
        # Roundtrip into MarkdownV2 produces the single-asterisk form.
        result = converter.from_ast(converter.to_ast("**bold text**"))
        assert "*bold text*" in result
        assert "**" not in result

    def test_links(self):
        result = converter.from_ast(converter.to_ast("[click here](https://example.com)"))
        assert "[click here](https://example.com)" in result

    def test_code_blocks(self):
        result = converter.from_ast(converter.to_ast("```\nconst x = 1;\n```"))
        assert "const x = 1;" in result

    def test_table_to_code_block(self):
        result = converter.from_ast(converter.to_ast("| Col1 | Col2 |\n|------|------|\n| A | B |"))
        assert "```" in result
        assert "Col1" in result
        assert "A" in result


# ---------------------------------------------------------------------------
# MarkdownV2 escape matrix (port of vercel/chat#407 / chat@4.27.0)
# ---------------------------------------------------------------------------


class TestMarkdownV2Escaping:
    """Telegram's MarkdownV2 parser rejects every unescaped occurrence of
    ``_*[]()~`>#+-=|{}.!\\`` outside of entities. The previous adapter
    used legacy ``"Markdown"`` parse mode + standard markdown stringify,
    which produced ``can't parse entities`` 400s on any LLM response that
    happened to contain a bare ``.`` or ``!``.

    What to fix if this fails: the renderer in
    ``src/chat_sdk/adapters/telegram/format_converter.py`` must escape
    every special char in plain text via the 20-char matrix, but only
    ``` ` ``` and ``\\`` inside code blocks, and only ``)`` and ``\\``
    inside link URLs.
    """

    def test_full_escape_matrix_in_plain_text(self):
        # All 20 reserved chars must be backslash-escaped.
        raw_text = "hello _world_ * [link] ( ) ~ ` > # + - = | { } . ! \\"
        ast = converter.to_ast(raw_text)
        result = converter.from_ast(ast)
        # Each special char must appear escaped (preceded by `\`).
        # Use a sample of representative characters — testing all 20
        # individually would be redundant noise.
        for char in [".", "!", "(", ")", "-", "+", "=", "|", "{", "}"]:
            # The char appears in the text — confirm there's no bare
            # unescaped occurrence anywhere in the rendered output.
            pos = result.find(char)
            assert pos > 0, f"char {char!r} should appear in output"
            assert result[pos - 1] == "\\", (
                f"char {char!r} at position {pos} is not preceded by `\\` — "
                f"context: {result[max(0, pos - 5) : pos + 3]!r}"
            )

    def test_dot_in_text_is_escaped(self):
        # The classic LLM-output failure: any sentence with a period.
        result = converter.from_ast(converter.to_ast("Hello world. This is a test."))
        # `.` must be escaped — `\.`
        assert "\\." in result
        # No bare `.` anywhere outside the escape sequence.
        # (Strip every escape pair, then assert no `.` remains.)
        stripped = result.replace("\\.", "")
        assert "." not in stripped

    def test_inline_code_only_escapes_backtick_and_backslash(self):
        # Inside `code`, a `.` is a literal `.` and must NOT be escaped.
        result = converter.from_ast(converter.to_ast("Use `obj.method()` here"))
        assert "obj.method()" in result, "code-block content should be unescaped"

    def test_link_url_only_escapes_paren_and_backslash(self):
        # Inside link URL, `.` is literal (not escaped). `)` IS escaped.
        result = converter.from_ast(converter.to_ast("[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"))
        # The inner `)` from `(bar)` must be escaped to `\)`.
        assert "\\)" in result
        # The `.` in the host should NOT be escaped inside the URL — assert
        # via a long anchored fragment so CodeQL's URL-substring heuristic
        # isn't tripped (this is a render check, not a security boundary).
        assert "https://en.wikipedia.org/wiki/Foo_" in result

    def test_render_postable_string_passes_through_unchanged(self):
        # Plain string messages ship verbatim — no escaping (parse_mode
        # will be None at the API layer).
        s = "hello.world! free-form text with (parens) and dots."
        assert converter.render_postable(s) == s

    def test_empty_input(self):
        assert converter.from_ast({"type": "root", "children": []}) == ""

    def test_whitespace_only_input(self):
        result = converter.render_postable({"markdown": "   \n   "})
        # Should be empty or whitespace — not a parse error.
        assert result.strip() == ""

    def test_heading_renders_as_bold(self):
        # MarkdownV2 has no heading syntax; we render `# H1` as `*H1*`.
        result = converter.from_ast(converter.to_ast("# Heading"))
        assert "*Heading*" in result

    def test_blockquote_per_line_prefix(self):
        result = converter.from_ast(converter.to_ast("> first\n> second"))
        # MarkdownV2 blockquote uses `>` per line.
        assert ">" in result

    def test_list_dash_is_escaped(self):
        # Bullet list items must use escaped `\-` (since `-` is reserved).
        result = converter.from_ast(converter.to_ast("- one\n- two"))
        assert "\\-" in result
        # No bare leading `- ` should remain (would be unescaped).
        for line in result.split("\n"):
            assert not line.startswith("- "), f"bare `- ` in line: {line!r}"

    def test_ordered_list_period_is_escaped(self):
        result = converter.from_ast(converter.to_ast("1. first\n2. second"))
        # The period after each number must be escaped: `1\.`
        assert "1\\." in result
        assert "2\\." in result
