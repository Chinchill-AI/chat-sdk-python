"""Google Chat-specific format conversion using AST-based parsing.

Google Chat supports a subset of text formatting:
- Bold: *text*
- Italic: _text_
- Strikethrough: ~text~
- Monospace: `text`
- Code blocks: ```text```
- Links are auto-detected

Very similar to Slack's mrkdwn format.
"""

from __future__ import annotations

import re
import secrets

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Content,
    Root,
    parse_markdown,
    table_to_ascii,
)

_GCHAT_LINK_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.\-]*:[^|\s>]+)\|([^>]+)>")
_HAS_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def _new_link_placeholder_token() -> tuple[str, re.Pattern[str]]:
    """Return a fresh (format-string, regex) pair for link placeholders.

    Each ``to_ast`` call generates its own random nonce so placeholder tokens
    can't be forged by user-supplied input that happens to contain a literal
    ``\\ue000LINK{N}\\ue000`` sequence. Without the nonce, a message like
    ``"\\ue000LINK0\\ue000 and <https://x|real>"`` would rewrite the user's
    literal placeholder into a duplicate link node.
    """
    nonce = secrets.token_hex(4)
    fmt = f"\ue000LINK{{idx}}-{nonce}\ue000"
    pattern = re.compile(rf"\ue000LINK(\d+)-{nonce}\ue000")
    return fmt, pattern


def _inject_link_placeholders(
    node: dict,
    links: list[tuple[str, str]],
    placeholder_re: re.Pattern[str],
) -> None:
    """Walk the AST in place and expand placeholder tokens in text nodes into
    real link nodes. Used by ``to_ast`` to bypass the Markdown parser for
    custom ``<url|text>`` tokens whose URLs may contain characters the parser
    can't round-trip (e.g. unescaped ``)``).

    Code spans (``inlineCode`` / ``code``) are handled specially: their
    content is literal, so placeholders in them are rewritten back to the
    original ``<url|text>`` syntax rather than being turned into link nodes.
    Out-of-range placeholder indices (which can happen with crafted input
    that guessed the nonce) are left as literal text rather than raising
    ``IndexError``.
    """

    def _placeholder_to_literal(value: str) -> str:
        def _sub(match: re.Match[str]) -> str:
            i = int(match.group(1))
            if 0 <= i < len(links):
                url, text = links[i]
                return f"<{url}|{text}>"
            return match.group(0)

        return placeholder_re.sub(_sub, value)

    children = node.get("children")
    if not isinstance(children, list):
        return
    new_children: list = []
    for child in children:
        if not isinstance(child, dict):
            new_children.append(child)
            continue
        ctype = child.get("type")
        if ctype == "text":
            value = child.get("value", "")
            if "\ue000" not in value:
                new_children.append(child)
                continue
            parts = placeholder_re.split(value)
            # parts alternates plain-text / placeholder-index / plain-text / ...
            for i, part in enumerate(parts):
                if i % 2 == 0:
                    if part:
                        new_children.append({"type": "text", "value": part})
                else:
                    idx = int(part)
                    if 0 <= idx < len(links):
                        url, text = links[idx]
                        new_children.append(
                            {
                                "type": "link",
                                "url": url,
                                "children": [{"type": "text", "value": text}],
                            }
                        )
                    else:
                        # Unknown placeholder (malformed / crafted input).
                        # Preserve as literal text rather than raising.
                        new_children.append({"type": "text", "value": f"\ue000LINK{idx}\ue000"})
        elif ctype in ("inlineCode", "code") and isinstance(child.get("value"), str):
            # Code spans are literal — a `<url|text>` inside them is user
            # content, not a link. Restore the original syntax rather than
            # leaving the placeholder embedded in the code value.
            if "\ue000" in child["value"]:
                child["value"] = _placeholder_to_literal(child["value"])
            new_children.append(child)
        else:
            _inject_link_placeholders(child, links, placeholder_re)
            new_children.append(child)
    node["children"] = new_children


class GoogleChatFormatConverter(BaseFormatConverter):
    """Format converter between standard markdown AST and Google Chat format.

    Uses the shared AST infrastructure with platform-specific node rendering.
    """

    def from_ast(self, ast: Root) -> str:
        """Render an AST to Google Chat format."""
        return self._from_ast_with_node_converter(ast, self._node_to_gchat)

    def to_ast(self, platform_text: str) -> Root:
        """Parse Google Chat message into an AST.

        Converts Google Chat format to standard markdown, then parses
        with the shared parser.
        """
        markdown = platform_text

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
        # Google Chat custom link syntax `<url|text>` has to survive our
        # Markdown parser, which doesn't implement CommonMark's balanced-
        # parens rule for link destinations. A naive regex substitution to
        # `[text](url)` would corrupt URLs containing `)` (e.g. Wikipedia-
        # style `https://en.wikipedia.org/wiki/Foo_(bar)`). Instead we
        # extract each match to a PUA placeholder, parse the rest as
        # Markdown, and inject real link nodes where the placeholders
        # land. Accepts any RFC 3986 scheme.
        #
        # The placeholder carries a random nonce so user-supplied text that
        # happens to look like a placeholder (e.g. literal `\ue000LINK0\ue000`
        # content) can't be rewritten as a fake link.
        links: list[tuple[str, str]] = []
        placeholder_fmt, placeholder_re = _new_link_placeholder_token()

        def _capture(match: re.Match[str]) -> str:
            idx = len(links)
            links.append((match.group(1), match.group(2)))
            return placeholder_fmt.format(idx=idx)

        markdown = _GCHAT_LINK_RE.sub(_capture, markdown)

        # Bold: *text* -> **text**
        markdown = re.sub(r"(?<![_*\\])\*([^*\n]+)\*(?![_*])", r"**\1**", markdown)

        # Strikethrough: ~text~ -> ~~text~~
        markdown = re.sub(r"(?<!~)~([^~\n]+)~(?!~)", r"~~\1~~", markdown)

        # Italic and code are the same format as markdown
        ast = parse_markdown(markdown)
        if links:
            _inject_link_placeholders(ast, links, placeholder_re)
        return ast

    def extract_plain_text(self, platform_text: str) -> str:
        """Extract plain text from Google Chat formatted text.

        Strips formatting markers while preserving the text content. Code
        spans are treated as literal — any `<url|text>` or formatting markers
        inside them are preserved verbatim, consistent with ``to_ast``.
        """
        # Stash code-span contents behind nonce'd placeholders BEFORE running
        # the other strips, then restore them at the end. Without this, the
        # link-syntax strip below would mangle code like `<url|text>` into
        # just `text`. Same unforgeable-nonce strategy as to_ast.
        nonce = secrets.token_hex(4)
        code_contents: list[str] = []
        code_placeholder_re = re.compile(rf"\ue001CODE(\d+)-{nonce}\ue001")

        def _stash_code(match: re.Match[str]) -> str:
            code_contents.append(match.group(1))
            return f"\ue001CODE{len(code_contents) - 1}-{nonce}\ue001"

        # Fenced first, then inline (order matters so we don't double-match).
        result = re.sub(r"```([\s\S]*?)```", _stash_code, platform_text)
        result = re.sub(r"`([^`]+)`", _stash_code, result)

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
        # Google Chat custom link syntax: <url|text> -> text (any RFC 3986 scheme)
        result = re.sub(r"<[a-zA-Z][a-zA-Z0-9+.\-]*:[^|\s>]+\|([^>]+)>", r"\1", result)
        # Bold markers (*text*)
        result = re.sub(r"\*([^*]+)\*", r"\1", result)
        # Italic markers (_text_)
        result = re.sub(r"_([^_]+)_", r"\1", result)
        # Strikethrough markers (~text|)
        result = re.sub(r"~([^~]+)~", r"\1", result)

        # Restore code contents literally.
        def _restore(match: re.Match[str]) -> str:
            i = int(match.group(1))
            return code_contents[i] if 0 <= i < len(code_contents) else match.group(0)

        result = code_placeholder_re.sub(_restore, result)
        return result.strip()

    def _node_to_gchat(self, node: Content) -> str:
        """Convert an AST node to Google Chat format."""
        node_type = node.get("type", "")

        if node_type == "paragraph":
            children = node.get("children", [])
            return "".join(self._node_to_gchat(child) for child in children)

        if node_type == "text":
            return node.get("value", "")

        if node_type == "strong":
            # Markdown **text** -> GChat *text*
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"*{content}*"

        if node_type == "emphasis":
            # Both use _text_
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"_{content}_"

        if node_type == "delete":
            # Markdown ~~text~~ -> GChat ~text~
            content = "".join(self._node_to_gchat(child) for child in node.get("children", []))
            return f"~{content}~"

        if node_type == "inlineCode":
            return f"`{node.get('value', '')}`"

        if node_type == "code":
            return f"```\n{node.get('value', '')}\n```"

        if node_type == "link":
            # Google Chat supports custom link labels using <url|text> syntax.
            children = node.get("children", [])
            link_text = "".join(self._node_to_gchat(child) for child in children)
            url = node.get("url", "")
            if link_text == url:
                return url
            # Divergence from upstream — see docs/UPSTREAM_SYNC.md.
            # Fall back to plain `text (url)` when the `<url|text>` form
            # can't safely round-trip:
            #   - labels containing `|`, `>`, `]`, or a newline would be
            #     truncated on the platform or by our own parser;
            #   - URLs without a scheme (e.g. relative paths like `/docs`)
            #     won't match the reverse parser's scheme-prefixed regex,
            #     so the link node would be lost on the read side.
            if any(c in link_text for c in ("|", ">", "]", "\n")) or not _HAS_SCHEME_RE.match(url):
                return f"{link_text} ({url})"
            return f"<{url}|{link_text}>"

        if node_type == "heading":
            # Intentional improvement over TS SDK: Google Chat has no heading
            # syntax, so we wrap headings in bold (*...*) for visual emphasis.
            children = node.get("children", [])
            content = "".join(self._node_to_gchat(child) for child in children)
            return f"*{content}*"

        if node_type == "blockquote":
            # Google Chat doesn't have native blockquote, use > prefix
            children = node.get("children", [])
            lines = []
            for child in children:
                lines.append(f"> {self._node_to_gchat(child)}")
            return "\n".join(lines)

        if node_type == "list":
            return self._render_list(node, 0, self._node_to_gchat, "\u2022")

        if node_type == "break":
            return "\n"

        if node_type == "thematicBreak":
            return "---"

        if node_type == "table":
            return f"```\n{table_to_ascii(node)}\n```"

        if node_type == "image":
            alt = node.get("alt", "")
            url = node.get("url", "")
            if alt:
                return f"{alt} ({url})"
            return url

        # Default: try to render children
        return self._default_node_to_text(node, self._node_to_gchat)
