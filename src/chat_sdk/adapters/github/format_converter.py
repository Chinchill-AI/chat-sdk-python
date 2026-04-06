"""GitHub-specific format conversion.

GitHub uses GitHub Flavored Markdown (GFM) which is very close to standard markdown.
This converter primarily passes through standard markdown, with special handling for:
- @mentions (user references)
- #refs (issue/PR references)
- SHA references (commit links)
"""

from __future__ import annotations

from chat_sdk.shared.base_format_converter import (
    BaseFormatConverter,
    Root,
    parse_markdown,
    stringify_markdown,
)


class GitHubFormatConverter(BaseFormatConverter):
    """Format converter for GitHub-flavored markdown.

    GitHub uses standard GFM, so conversion is mostly a pass-through.
    The main task is preserving @mentions and rendering postable messages.
    """

    def from_ast(self, ast: Root) -> str:
        """Convert an AST to GitHub-flavored markdown.

        GitHub uses standard GFM, so we serialize the AST using the
        shared stringify function.
        """
        if not ast:
            return ""
        return stringify_markdown(ast).strip()

    def to_ast(self, markdown: str) -> Root:
        """Parse GitHub markdown into an AST.

        GitHub uses standard GFM, so we use the shared parser directly.
        """
        return parse_markdown(markdown)
