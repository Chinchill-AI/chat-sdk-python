"""mdast parity bake-off: hand-rolled parser vs library candidates.

For each library candidate, parse the fixture corpus and diff the
resulting mdast tree against the baseline hand-rolled parser. The
test does not fail on divergence -- this is a measurement harness,
not an acceptance gate. Divergences are recorded so the spike report
can show *which* node shapes each candidate gets wrong (and how badly).

Run with verbose output to see the full divergence report:

    uv run pytest tests/parser_spike/test_mdast_parity.py -s -v
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.shared.markdown_parser import parse_markdown as baseline_parse
from chat_sdk.shared.parser_spike.markdown_it_translator import (
    parse_markdown as markdown_it_parse,
)
from chat_sdk.shared.parser_spike.marko_translator import parse_markdown as marko_parse
from chat_sdk.shared.parser_spike.mistune_translator import parse_markdown as mistune_parse

CANDIDATES = [
    ("mistune", mistune_parse),
    ("markdown-it-py", markdown_it_parse),
    ("marko", marko_parse),
]


# ---------------------------------------------------------------------------
# Divergence reporter
# ---------------------------------------------------------------------------


def _walk(node: Any, path: str = "$") -> list[tuple[str, str, Any]]:
    """Yield (path, kind, value) for each node-shape signal we care about.

    Kind is one of: "type", "depth", "ordered", "start", "lang", "url",
    "alt", "title", "value", "align", "child_count".
    """
    out: list[tuple[str, str, Any]] = []
    if isinstance(node, dict):
        t = node.get("type")
        out.append((path, "type", t))
        for key in ("depth", "ordered", "start", "lang", "url", "alt", "title", "value", "align"):
            if key in node:
                out.append((path, key, node[key]))
        children = node.get("children")
        if isinstance(children, list):
            out.append((path, "child_count", len(children)))
            for i, child in enumerate(children):
                out.extend(_walk(child, f"{path}.children[{i}]"))
    return out


def _diff_trees(baseline: Any, candidate: Any) -> list[str]:
    base_walk = _walk(baseline)
    cand_walk = _walk(candidate)

    base_index = {(p, k): v for (p, k, v) in base_walk}
    cand_index = {(p, k): v for (p, k, v) in cand_walk}

    diffs: list[str] = []
    seen = set(base_index) | set(cand_index)
    for key in sorted(seen):
        path, kind = key
        b = base_index.get(key, "<missing>")
        c = cand_index.get(key, "<missing>")
        if b != c:
            diffs.append(f"  {path} [{kind}]: baseline={b!r} candidate={c!r}")
    return diffs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,parser", CANDIDATES)
def test_candidate_produces_root_node(name: str, parser, mixed_content_markdown: str) -> None:
    result = parser(mixed_content_markdown)
    assert result["type"] == "root", f"{name} did not produce a root node"
    assert isinstance(result.get("children"), list)


@pytest.mark.parametrize("name,parser", CANDIDATES)
def test_candidate_matches_top_level_block_types(name: str, parser, mixed_content_markdown: str) -> None:
    baseline = baseline_parse(mixed_content_markdown)
    candidate = parser(mixed_content_markdown)
    baseline_types = [c.get("type") for c in baseline["children"]]
    candidate_types = [c.get("type") for c in candidate["children"]]
    # Don't assert equality -- different parsers may split paragraphs
    # differently around HRs or trailing blank lines. We assert that the
    # important constructs are all present in both.
    important = {"heading", "table", "code", "list", "blockquote", "thematicBreak"}
    base_important = [t for t in baseline_types if t in important]
    cand_important = [t for t in candidate_types if t in important]
    assert base_important == cand_important, (
        f"{name} block-type sequence diverges:\n  baseline: {baseline_types}\n  {name}: {candidate_types}"
    )


def test_report_full_divergences(mixed_content_markdown: str) -> None:
    """Print a full divergence report for each candidate. Always passes.

    Run with ``pytest -s`` to see the report inline.
    """
    baseline = baseline_parse(mixed_content_markdown)
    print("\n" + "=" * 70)
    print("mdast divergence report")
    print("=" * 70)
    for name, parser in CANDIDATES:
        candidate = parser(mixed_content_markdown)
        diffs = _diff_trees(baseline, candidate)
        print(f"\n[{name}] {len(diffs)} divergence(s)")
        if diffs:
            for line in diffs[:30]:  # cap noise
                print(line)
            if len(diffs) > 30:
                print(f"  ... +{len(diffs) - 30} more")


def test_dump_baseline_tree_size(mixed_content_markdown: str) -> None:
    """Sanity: the fixture exercises enough of the AST to be meaningful."""
    baseline = baseline_parse(mixed_content_markdown)
    nodes = _walk(baseline)
    # ~200+ shape signals = at least a couple dozen non-trivial nodes.
    assert len(nodes) > 150, f"Fixture is too small to be a useful bake-off (only {len(nodes)} signals)"
    # Spot-check the constructs the fixture should contain.
    types = {sig for (_, kind, sig) in nodes if kind == "type"}
    required_types = (
        "heading",
        "paragraph",
        "code",
        "list",
        "table",
        "blockquote",
        "thematicBreak",
        "strong",
        "emphasis",
        "link",
    )
    for required in required_types:
        assert required in types, f"Fixture missing required node type: {required}"


# ---------------------------------------------------------------------------
# Completeness gap (what each parser actually recognises on hard constructs)
# ---------------------------------------------------------------------------


def _collect_recognised_types(node: Any) -> set[str]:
    """Set of all `type` values appearing anywhere in the tree."""
    found: set[str] = set()
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, str):
            found.add(t)
        for child in node.get("children") or []:
            found |= _collect_recognised_types(child)
    return found


# Construct -> expected mdast `type` (or set of types) when recognised.
# A parser that returns *none* of these for the gap fixture has silently
# flattened the construct to paragraph/text. The baseline is documented
# as not handling any of these, so it sets the floor.
GAP_CONSTRUCTS: dict[str, set[str]] = {
    "setext heading": {"heading"},  # heading must appear from a setext source
    "indented code block": {"code"},  # raw 4-space indented block
    "footnote definition": {"footnoteDefinition", "footnoteReference"},
    "inline HTML": {"html", "inlineHTML"},
    "task list item": {"listItem"},  # mdast: listItem with `checked` attr
    "definition list": {"definition", "descriptionList", "termTitle"},
}


def test_report_completeness_gap(gap_cases_markdown: str) -> None:
    """Print which gap constructs each parser actually recognised.

    The baseline parser is *known* to not handle these (see
    docs/UPSTREAM_SYNC.md non-parity table). This report quantifies how
    many it silently drops vs each library candidate.

    Run with ``pytest -s`` to see the report inline.
    """
    print("\n" + "=" * 70)
    print("Completeness gap report (gap_cases.md)")
    print("=" * 70)

    parsers = [("baseline (hand)", baseline_parse), *CANDIDATES]
    rows: list[tuple[str, set[str]]] = []
    for name, parser in parsers:
        types = _collect_recognised_types(parser(gap_cases_markdown))
        rows.append((name, types))

    # Construct table: rows = constructs, columns = parsers
    print(f"\n{'construct':<24}", end="")
    for name, _ in rows:
        print(f" {name[:14]:>15}", end="")
    print()
    print("-" * (24 + 16 * len(rows)))

    for construct, expected in GAP_CONSTRUCTS.items():
        print(f"{construct:<24}", end="")
        for _, types in rows:
            recognised = bool(expected & types)
            print(f" {'recognised' if recognised else 'silent drop':>15}", end="")
        print()

    print()
    # Per-parser unique-type counts on this fixture
    print("Distinct mdast types emitted on gap fixture:")
    for name, types in rows:
        type_list = sorted(types)
        print(f"  {name:<20} {len(type_list):>2}  -> {type_list}")
