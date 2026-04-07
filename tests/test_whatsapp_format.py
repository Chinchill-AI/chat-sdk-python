"""Port of adapter-whatsapp/src/markdown.test.ts -- WhatsApp format converter tests.

Tests WhatsAppFormatConverter's toAst, fromAst, and renderPostable methods.
"""

from __future__ import annotations

from chat_sdk.adapters.whatsapp.format_converter import WhatsAppFormatConverter

# ---------------------------------------------------------------------------
# Shared instance
# ---------------------------------------------------------------------------

converter = WhatsAppFormatConverter()


# ---------------------------------------------------------------------------
# toAst
# ---------------------------------------------------------------------------


class TestWhatsAppToAst:
    """Tests for WhatsAppFormatConverter.to_ast."""

    def test_plain_text(self):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) > 0

    def test_whatsapp_bold(self):
        ast = converter.to_ast("*bold text*")
        assert ast["type"] == "root"

    def test_italic(self):
        ast = converter.to_ast("_italic text_")
        assert ast["type"] == "root"

    def test_strikethrough(self):
        ast = converter.to_ast("~strikethrough~")
        assert ast["type"] == "root"

    def test_bold_spans_not_merged_across_newlines(self):
        ast = converter.to_ast("*bold1*\nsome text\n*bold2*")
        result = converter.from_ast(ast)
        assert "*bold1*" in result
        assert "*bold2*" in result

    def test_code_blocks(self):
        ast = converter.to_ast("```\ncode\n```")
        assert ast["type"] == "root"

    def test_lists(self):
        ast = converter.to_ast("- item 1\n- item 2\n- item 3")
        assert ast["type"] == "root"


# ---------------------------------------------------------------------------
# fromAst
# ---------------------------------------------------------------------------


class TestWhatsAppFromAst:
    """Tests for WhatsAppFormatConverter.from_ast."""

    def test_simple_ast(self):
        ast = converter.to_ast("Hello world")
        result = converter.from_ast(ast)
        assert "Hello world" in result

    def test_standard_bold_to_whatsapp(self):
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "*bold text*" in result
        assert "**bold text**" not in result

    def test_standard_strikethrough_to_whatsapp(self):
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~strikethrough~" in result
        assert "~~strikethrough~~" not in result

    def test_italic_underscore(self):
        result = converter.render_postable({"markdown": "_italic text_"})
        assert "_italic text_" in result
        assert "*italic text*" not in result

    def test_bold_and_italic_together(self):
        result = converter.render_postable({"markdown": "**bold** and _italic_"})
        assert "*bold*" in result
        assert "_italic_" in result

    def test_heading_to_bold(self):
        ast = converter.to_ast("# Main heading")
        result = converter.from_ast(ast)
        assert "*Main heading*" in result
        assert "#" not in result

    def test_flatten_bold_inside_heading(self):
        result = converter.render_postable({"markdown": "## **Choose React if:**"})
        assert "*Choose React if:*" in result
        assert "***" not in result

    def test_heading_with_mixed_text_and_bold(self):
        result = converter.render_postable(
            {"markdown": "# The Honest Answer: **It Depends!** \U0001f937\u200d\u2642\ufe0f"}
        )
        # Title should be a single bold span; no double-asterisks
        assert "**" not in result

    def test_thematic_break_to_separator(self):
        ast = converter.to_ast("above\n\n---\n\nbelow")
        result = converter.from_ast(ast)
        assert "\u2501\u2501\u2501" in result
        assert "above" in result
        assert "below" in result

    def test_tables_to_code_blocks(self):
        ast = converter.to_ast("| A | B |\n| --- | --- |\n| 1 | 2 |")
        result = converter.from_ast(ast)
        assert "```" in result


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestWhatsAppRenderPostable:
    """Tests for WhatsAppFormatConverter.render_postable."""

    def test_plain_string(self):
        result = converter.render_postable("Hello world")
        assert result == "Hello world"

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

    def test_complex_ai_style_response(self):
        markdown = "\n".join(
            [
                "# The Answer: **It Depends!**",
                "",
                "There's no universal *better* choice.",
                "",
                "## **Choose React if:**",
                "- Building **large-scale** apps",
                "- Need the biggest *ecosystem*",
                "- **Examples:** Facebook, Netflix",
                "",
                "## **Choose Vue if:**",
                "- Want *faster* learning curve",
                "- Prefer ~~complex~~ cleaner templates",
                "",
                "---",
                "",
                "## Real Talk:",
                "**All three are excellent.** Learn *React* first!",
            ]
        )

        result = converter.render_postable({"markdown": markdown})

        # Core structure checks -- the exact formatting can vary slightly
        # from the TS output but the key conversions must hold:
        assert "*The Answer: It Depends!*" in result
        assert "_better_" in result
        assert "*Choose React if:*" in result
        assert "*large-scale*" in result
        assert "_ecosystem_" in result
        assert "*Choose Vue if:*" in result
        assert "~complex~" in result
        assert "\u2501\u2501\u2501" in result
        assert "*Real Talk:*" in result
        assert "_React_" in result
        # No double-asterisks anywhere (WhatsApp bold is single-asterisk)
        assert "**" not in result
        # No double-tildes anywhere (WhatsApp strikethrough is single-tilde)
        assert "~~" not in result
