"""Parser-replacement spike for issue #69 Option B.

Three candidate libraries are evaluated as drop-in replacements for the
hand-rolled ``shared/markdown_parser.py``:

- ``mistune`` (3.x)
- ``markdown-it-py`` (4.x)
- ``marko`` (2.x)

Each gets a thin translator that converts the library's native token /
AST format into the mdast-compatible dict shape produced by
``shared.markdown_parser.parse_markdown``. The contract: same input
markdown should produce the same mdast tree across all four parsers
(the existing hand-rolled one + the three candidates), modulo
documented divergences.

This module is NOT imported by the runtime SDK. It exists purely so
the bake-off harness in ``tests/parser_spike/`` and
``scripts/parser_spike/`` can exercise the candidates side-by-side
without touching production code paths.

The decision criteria (per the issue #69 follow-up plan):
  1. mdast fidelity vs the existing parser on the fixture corpus
  2. Translator LOC (target: <250 per library)
  3. Parse-and-translate time (target: <5ms on 10KB mixed content)
  4. GFM coverage (tables, strikethrough, task lists)
  5. Extensibility surface for the gaps in #69 (setext, footnotes,
     escaped chars, multi-backtick code spans, raw HTML, indented code)
"""

from __future__ import annotations
