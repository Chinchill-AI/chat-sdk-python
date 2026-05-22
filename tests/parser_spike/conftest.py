"""Shared fixtures for the parser-replacement spike harness."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def mixed_content_markdown() -> str:
    return (FIXTURE_DIR / "mixed_content.md").read_text(encoding="utf-8")
