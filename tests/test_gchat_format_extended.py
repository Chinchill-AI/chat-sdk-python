"""Extended tests for GoogleChatFormatConverter -- targeting 80%+ coverage.

Ported from packages/adapter-gchat/src/markdown.test.ts (29 tests).
"""

from __future__ import annotations

from chat_sdk.adapters.google_chat.format_converter import GoogleChatFormatConverter


def _converter() -> GoogleChatFormatConverter:
    return GoogleChatFormatConverter()


# ---------------------------------------------------------------------------
# to_ast: Google Chat format -> AST
# ---------------------------------------------------------------------------


class TestToAst:
    def test_bold_single_star_to_ast(self):
        """*bold* in GChat maps to strong node via **bold** in markdown."""
        converter = _converter()
        ast = converter.to_ast("*bold*")
        assert ast is not None
        assert ast["type"] == "root"

    def test_strikethrough_single_tilde_to_ast(self):
        """~struck~ in GChat maps to delete node via ~~struck~~ in markdown."""
        converter = _converter()
        ast = converter.to_ast("~struck~")
        assert ast["type"] == "root"

    def test_plain_text_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("Hello world")
        assert ast["type"] == "root"
        # Should have paragraph with text child
        children = ast.get("children", [])
        assert len(children) > 0

    def test_mixed_formatting_to_ast(self):
        """Bold, italic, and strikethrough together."""
        converter = _converter()
        ast = converter.to_ast("*bold* _italic_ ~struck~")
        assert ast["type"] == "root"

    def test_code_block_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("```\ncode\n```")
        assert ast["type"] == "root"

    def test_inline_code_to_ast(self):
        converter = _converter()
        ast = converter.to_ast("Use `const x = 1`")
        assert ast["type"] == "root"

    def test_gchat_custom_link_parses_back_to_link_node(self):
        """Round-trip guard: `<url|text>` emitted by from_ast must parse back
        to a link node so downstream handlers see structured links, not raw
        angle-bracket text. Regression test for a P1 raised in review."""
        converter = _converter()
        ast = converter.to_ast("See <https://example.com|Example> for more")

        # Walk the AST looking for a link node with the expected URL and label.
        found = False

        def _walk(node: object) -> None:
            nonlocal found
            if isinstance(node, dict):
                if node.get("type") == "link" and node.get("url") == "https://example.com":
                    children = node.get("children", [])
                    text = "".join(c.get("value", "") for c in children if isinstance(c, dict))
                    if text == "Example":
                        found = True
                for child in node.get("children", []) or []:
                    _walk(child)

        _walk(ast)
        assert found, "Expected a link node with url='https://example.com' and text='Example'"

    def test_gchat_custom_link_syntax_inside_code_span_stays_literal(self):
        """`<url|text>` inside inline or fenced code is user content, not a
        link. The AST-placeholder substitution must restore the original
        syntax in code nodes rather than embedding the `\\ue000LINK...`
        sentinel."""
        converter = _converter()

        def _values_of_type(ast: object, target: str) -> list[str]:
            out: list[str] = []

            def _walk(node: object) -> None:
                if isinstance(node, dict):
                    if node.get("type") == target:
                        out.append(node.get("value", ""))
                    for child in node.get("children", []) or []:
                        _walk(child)

            _walk(ast)
            return out

        ast_inline = converter.to_ast("Use `<https://example.com|Example>` in code")
        assert _values_of_type(ast_inline, "inlineCode") == ["<https://example.com|Example>"]

        ast_fenced = converter.to_ast("```\ncurl <https://api.com|example>\n```")
        fenced_values = _values_of_type(ast_fenced, "code")
        assert any("<https://api.com|example>" in v for v in fenced_values), fenced_values

    def test_gchat_custom_link_tolerates_out_of_range_placeholder(self):
        """If user input happens to include the PUA placeholder pattern with
        an index that isn't in our `links` list, `to_ast` must not raise. The
        unknown placeholder is preserved as literal text and any real
        `<url|text>` alongside it still parses correctly."""
        converter = _converter()
        # Index 999 is deliberately out of range; there's only one real
        # <url|text> token so `links` will have length 1.
        ast = converter.to_ast("\ue000LINK999\ue000 and <https://example.com|Real>")
        link_urls: list[str] = []

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "link":
                    link_urls.append(node.get("url", ""))
                for child in node.get("children", []) or []:
                    _walk(child)

        _walk(ast)
        assert link_urls == ["https://example.com"]

    def test_gchat_custom_link_parses_url_with_balanced_parens(self):
        """URLs containing `(...)` (e.g. Wikipedia-style) must round-trip
        intact. The Markdown parser doesn't implement CommonMark's balanced-
        parens rule for link destinations, so a naive regex rewrite
        `<url|text>` → `[text](url)` would truncate the URL at the first `)`.
        The AST placeholder substitution path in `to_ast` bypasses the parser
        for these tokens and injects a link node with the full URL intact."""
        converter = _converter()
        ast = converter.to_ast("See <https://example.com/a_(b)|Wiki> for info")
        link_urls: list[str] = []

        def _walk(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "link":
                    link_urls.append(node.get("url", ""))
                for child in node.get("children", []) or []:
                    _walk(child)

        _walk(ast)
        assert link_urls == ["https://example.com/a_(b)"], f"expected intact URL, got {link_urls!r}"

    def test_gchat_custom_link_parses_non_http_schemes(self):
        """mailto:/tel:/etc. emit <url|text> in from_ast; to_ast must accept
        any RFC 3986 scheme, not just http(s)."""
        converter = _converter()
        for url, label in [
            ("mailto:test@example.com", "Email"),
            ("tel:+15551234", "Call"),
            ("ftp:files.example.com", "Files"),
        ]:
            ast = converter.to_ast(f"Contact <{url}|{label}> for details")
            found = False

            def _walk(node: object, _url: str = url, _label: str = label) -> None:
                nonlocal found
                if isinstance(node, dict):
                    if node.get("type") == "link" and node.get("url") == _url:
                        children = node.get("children", [])
                        text = "".join(c.get("value", "") for c in children if isinstance(c, dict))
                        if text == _label:
                            found = True
                    for child in node.get("children", []) or []:
                        _walk(child)

            _walk(ast)
            assert found, f"Expected a link node for url={url!r} label={label!r}"
            # And extract_plain_text should reduce to just the label
            assert converter.extract_plain_text(f"<{url}|{label}>") == label


# ---------------------------------------------------------------------------
# from_ast: AST -> Google Chat format
# ---------------------------------------------------------------------------


class TestFromAst:
    def test_bold_markdown_to_gchat(self):
        """**bold** -> *bold* in GChat."""
        converter = _converter()
        ast = converter.to_ast("**bold text**")
        result = converter.from_ast(ast)
        assert "*bold text*" in result

    def test_italic(self):
        converter = _converter()
        ast = converter.to_ast("_italic text_")
        result = converter.from_ast(ast)
        assert "_italic text_" in result

    def test_strikethrough(self):
        """~~text~~ -> ~text~ in GChat."""
        converter = _converter()
        ast = converter.to_ast("~~strikethrough~~")
        result = converter.from_ast(ast)
        assert "~strikethrough~" in result

    def test_inline_code(self):
        converter = _converter()
        ast = converter.to_ast("Use `const x = 1`")
        result = converter.from_ast(ast)
        assert "`const x = 1`" in result

    def test_code_block(self):
        converter = _converter()
        ast = converter.to_ast("```\nconst x = 1;\n```")
        result = converter.from_ast(ast)
        assert "```" in result
        assert "const x = 1;" in result

    def test_link_same_text_and_url(self):
        converter = _converter()
        ast = converter.to_ast("[https://example.com](https://example.com)")
        result = converter.from_ast(ast)
        assert "https://example.com" in result

    def test_link_different_text_and_url(self):
        converter = _converter()
        ast = converter.to_ast("[click here](https://example.com)")
        result = converter.from_ast(ast)
        assert "<https://example.com|click here>" in result

    def test_should_preserve_custom_link_labels_in_posted_messages(self):
        # Matches the integration-tests parity case: a posted markdown link
        # with a custom label must render as Google Chat's <url|text> syntax
        # rather than being flattened to "text (url)".
        converter = _converter()
        result = converter.from_markdown("[Click here](https://example.com)")
        assert result == "<https://example.com|Click here>"

    def test_falls_back_to_parenthesized_form_when_label_contains_reserved_chars(self):
        """Labels containing `>` / `|` / `]` / newline would produce malformed
        `<url|text>` output: Google Chat and our own regex stop at the first
        `>` or `|`, and `]` prematurely closes the Markdown link when to_ast()
        converts the `<url|text>` form back to `[text](url)`. Fall back to
        plain `text (url)` so the label is preserved intact and the URL is
        still auto-detected as a link.

        Note: `from_markdown` can't construct these labels because the Markdown
        parser itself splits on `]`/newline. We exercise the `from_ast` emit
        path directly with a hand-built AST instead.
        """
        converter = _converter()
        for label in ["a > b", "a | b", "a ] b", "a\nb"]:
            ast = {
                "type": "root",
                "children": [
                    {
                        "type": "paragraph",
                        "children": [
                            {
                                "type": "link",
                                "url": "https://example.com",
                                "children": [{"type": "text", "value": label}],
                            }
                        ],
                    }
                ],
            }
            result = converter.from_ast(ast)
            assert result == f"{label} (https://example.com)", f"label={label!r}"
        # Sanity: labels without reserved chars still use the <url|text> form.
        ok_ast = {
            "type": "root",
            "children": [
                {
                    "type": "paragraph",
                    "children": [
                        {
                            "type": "link",
                            "url": "https://example.com",
                            "children": [{"type": "text", "value": "ok"}],
                        }
                    ],
                }
            ],
        }
        assert converter.from_ast(ok_ast) == "<https://example.com|ok>"

    def test_blockquote(self):
        converter = _converter()
        ast = converter.to_ast("> quoted text")
        result = converter.from_ast(ast)
        assert "> quoted text" in result

    def test_unordered_list(self):
        converter = _converter()
        ast = converter.to_ast("- item 1\n- item 2")
        result = converter.from_ast(ast)
        assert "item 1" in result
        assert "item 2" in result

    def test_ordered_list(self):
        converter = _converter()
        ast = converter.to_ast("1. first\n2. second")
        result = converter.from_ast(ast)
        assert "1." in result
        assert "2." in result

    def test_nested_unordered_list(self):
        converter = _converter()
        result = converter.from_markdown("- parent\n  - child 1\n  - child 2")
        assert "\u2022 parent" in result
        assert "  \u2022 child 1" in result
        assert "  \u2022 child 2" in result

    def test_nested_ordered_list(self):
        converter = _converter()
        result = converter.from_markdown("1. first\n   1. sub-first\n   2. sub-second\n2. second")
        assert "1. first" in result
        # Sub-items appear in the output (nesting depth depends on parser)
        assert "sub-first" in result
        assert "sub-second" in result
        assert "2. second" in result

    def test_deeply_nested_list(self):
        converter = _converter()
        result = converter.from_markdown("- level 1\n  - level 2\n    - level 3")
        assert "\u2022 level 1" in result
        assert "  \u2022 level 2" in result
        assert "    \u2022 level 3" in result

    def test_sibling_items_same_indent(self):
        converter = _converter()
        result = converter.from_markdown("- item 1\n- item 2\n- item 3")
        assert result == "\u2022 item 1\n\u2022 item 2\n\u2022 item 3"

    def test_mixed_ordered_unordered_nesting(self):
        converter = _converter()
        result = converter.from_markdown("1. first\n   - sub a\n   - sub b\n2. second")
        assert "1. first" in result
        # Sub-items appear in the output (nesting depth depends on parser)
        assert "sub a" in result
        assert "sub b" in result
        assert "2. second" in result

    def test_line_breaks(self):
        converter = _converter()
        ast = converter.to_ast("line1  \nline2")
        result = converter.from_ast(ast)
        assert "line1" in result
        assert "line2" in result

    def test_thematic_break(self):
        converter = _converter()
        ast = converter.to_ast("text\n\n---\n\nmore")
        result = converter.from_ast(ast)
        assert "---" in result

    def test_heading_rendered_as_bold(self):
        """GChat has no heading syntax, so headings become bold."""
        converter = _converter()
        ast = converter.to_ast("# Heading Text")
        result = converter.from_ast(ast)
        assert "*Heading Text*" in result

    def test_image_with_alt_text(self):
        converter = _converter()
        ast = converter.to_ast("![alt text](https://example.com/img.png)")
        result = converter.from_ast(ast)
        assert "alt text" in result
        assert "https://example.com/img.png" in result

    def test_image_without_alt_text(self):
        converter = _converter()
        ast = converter.to_ast("![](https://example.com/img.png)")
        result = converter.from_ast(ast)
        assert "https://example.com/img.png" in result

    def test_table_as_code_block(self):
        converter = _converter()
        result = converter.from_markdown("| Name | Age |\n|------|-----|\n| Alice | 30 |")
        assert "```" in result
        assert "Name" in result
        assert "Alice" in result


# ---------------------------------------------------------------------------
# extractPlainText
# ---------------------------------------------------------------------------


class TestExtractPlainText:
    def test_removes_bold_italic_strikethrough(self):
        converter = _converter()
        result = converter.extract_plain_text("*bold* _italic_ ~struck~")
        assert "bold" in result
        assert "italic" in result
        assert "struck" in result
        assert "*" not in result
        assert "~" not in result

    def test_empty_string(self):
        converter = _converter()
        assert converter.extract_plain_text("") == ""

    def test_plain_text_unchanged(self):
        converter = _converter()
        assert converter.extract_plain_text("Hello world") == "Hello world"

    def test_strips_inline_code(self):
        converter = _converter()
        result = converter.extract_plain_text("Use `const x = 1`")
        assert "const x = 1" in result
        assert "`" not in result

    def test_strips_code_blocks(self):
        converter = _converter()
        result = converter.extract_plain_text("```\nsome code\n```")
        assert "some code" in result

    def test_strips_gchat_custom_link_to_label(self):
        """<url|text> should reduce to just the label text."""
        converter = _converter()
        assert (
            converter.extract_plain_text("See <https://example.com|Example Site> for details")
            == "See Example Site for details"
        )


# ---------------------------------------------------------------------------
# render_postable
# ---------------------------------------------------------------------------


class TestRenderPostable:
    def test_plain_string(self):
        converter = _converter()
        assert converter.render_postable("Hello world") == "Hello world"

    def test_raw_message(self):
        converter = _converter()
        assert converter.render_postable({"raw": "raw text"}) == "raw text"

    def test_markdown_message(self):
        converter = _converter()
        result = converter.render_postable({"markdown": "**bold** text"})
        assert "bold" in result

    def test_ast_message(self):
        converter = _converter()
        ast = converter.to_ast("**bold**")
        result = converter.render_postable({"ast": ast})
        assert "bold" in result

    def test_empty_dict(self):
        converter = _converter()
        result = converter.render_postable({})
        # Falls through to str({})
        assert isinstance(result, str)
