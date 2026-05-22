"""Shared fixtures for the parser-replacement spike harness."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def mixed_content_markdown() -> str:
    return (FIXTURE_DIR / "mixed_content.md").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def gap_cases_markdown() -> str:
    """Constructs the hand-rolled parser explicitly doesn't support
    (setext headings, footnotes, escaped chars, multi-backtick spans,
    raw HTML, indented code blocks, math, task lists, autolinks,
    definition lists). Used to measure the *completeness* gap, not
    just the structural-equivalence gap.
    """
    return (FIXTURE_DIR / "gap_cases.md").read_text(encoding="utf-8")
