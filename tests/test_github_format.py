"""Port of adapter-github/src/markdown.test.ts -- GitHub format converter tests.

Tests GitHubFormatConverter's toAst, fromAst, extractPlainText, and renderPostable.
"""

from __future__ import annotations

from chat_sdk.adapters.github.format_converter import GitHubFormatConverter

# ---------------------------------------------------------------------------
# Shared instance
# ---------------------------------------------------------------------------

converter = GitHubFormatConverter()


# ---------------------------------------------------------------------------
# toAst
# ---------------------------------------------------------------------------


class TestGitHubToAst:
    """Tests for GitHubFormatConverter.to_ast."""

    def test_plain_text(self):
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        assert len(ast.get("children", [])) == 1

    def test_bold_text(self):
        ast = converter.to_ast("**bold text**")
        assert ast["type"] == "root"
        paragraph = ast["children"][0]
        assert paragraph["type"] == "paragraph"

    def test_mentions(self):
        text = converter.extract_plain_text("Hey @username, check this out")
        assert "@username" in text

    def test_code_blocks(self):
        ast = converter.to_ast("```javascript\nconsole.log('hello');\n```")
        assert ast["type"] == "root"

    def test_links(self):
        ast = converter.to_ast("[link text](https://example.com)")
        assert ast["type"] == "root"

    def test_strikethrough(self):
        ast = converter.to_ast("~~deleted~~")
        assert ast["type"] == "root"


# ---------------------------------------------------------------------------
# fromAst
# ---------------------------------------------------------------------------


class TestGitHubFromAst:
    """Tests for GitHubFormatConverter.from_ast."""

    def test_plain_text(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "Hello world"}],
                }
            ],
        }
        result = converter.from_ast(ast)
        assert result == "Hello world"

    def test_bold_text(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "strong",
                            "children": [{"type": "text", "value": "bold"}],
                        }
                    ],
                }
            ],
        }
        result = converter.from_ast(ast)
        assert result == "**bold**"

    def test_italic_text(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "emphasis",
                            "children": [{"type": "text", "value": "italic"}],
                        }
                    ],
                }
            ],
        }
        result = converter.from_ast(ast)
        assert result == "*italic*"


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestGitHubExtractPlainText:
    """Tests for GitHubFormatConverter.extract_plain_text."""

    def test_strips_formatting(self):
        result = converter.extract_plain_text("**bold** and _italic_")
        assert result == "bold and italic"

    def test_preserves_mentions(self):
        result = converter.extract_plain_text("Hey @user, **thanks**!")
        assert "@user" in result
        assert "thanks" in result

    def test_extracts_from_code_blocks(self):
        result = converter.extract_plain_text("```\ncode\n```")
        assert "code" in result


# ---------------------------------------------------------------------------
# renderPostable
# ---------------------------------------------------------------------------


class TestGitHubRenderPostable:
    """Tests for GitHubFormatConverter.render_postable."""

    def test_string_directly(self):
        result = converter.render_postable("Hello world")
        assert result == "Hello world"

    def test_raw_message(self):
        result = converter.render_postable({"raw": "Raw content"})
        assert result == "Raw content"

    def test_markdown_message(self):
        result = converter.render_postable({"markdown": "**bold**"})
        assert result == "**bold**"

    def test_ast_message(self):
        ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [{"type": "text", "value": "AST content"}],
                }
            ],
        }
        result = converter.render_postable({"ast": ast})
        assert result == "AST content"


# ---------------------------------------------------------------------------
# roundtrip
# ---------------------------------------------------------------------------


class TestGitHubRoundtrip:
    """Roundtrip tests (toAst -> fromAst)."""

    def test_simple_text(self):
        original = "Hello world"
        ast = converter.to_ast(original)
        result = converter.from_ast(ast)
        assert result.strip() == original

    def test_markdown_with_formatting(self):
        original = "**bold** and *italic*"
        ast = converter.to_ast(original)
        result = converter.from_ast(ast)
        assert "bold" in result
        assert "italic" in result
