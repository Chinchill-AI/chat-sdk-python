"""The two version-mapping tables (CLAUDE.md and docs/UPSTREAM_SYNC.md) agree.

Each release adds a row to both by hand, and they drifted once (#203: the
UPSTREAM_SYNC table folded ``0.4.31.1`` into a ``0.4.31.3`` row describing the
wrong fixes). These tests keep the version sets and upstream versions in step,
and tie the current package version to ``UPSTREAM_PARITY``.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import chat_sdk

ROOT = Path(__file__).resolve().parent.parent

# CLAUDE.md is repository-only: the sdist allowlist (pyproject.toml) ships
# /tests and /docs but not CLAUDE.md, so skip there instead of failing.
pytestmark = pytest.mark.skipif(
    not (ROOT / "CLAUDE.md").is_file(), reason="CLAUDE.md is not shipped in the sdist (repository checkout only)"
)

_CLAUDE_ROW = re.compile(r"^- `(0\.\d+\.\d+(?:\.\d+)?)` = (.*)$")
_SYNC_ROW = re.compile(r"^\| `(0\.\d+\.\d+[^`]*)` \| `(\d+\.\d+\.\d+)` \| (.*) \|$")
_UPSTREAM = re.compile(r"`(\d+\.\d+\.\d+)`")


def _section(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start:end]


def _claude_rows() -> dict[str, str]:
    """Published versions in CLAUDE.md -> upstream version they name."""
    rows: dict[str, str] = {}
    for line in _section((ROOT / "CLAUDE.md").read_text(), "## Version Mapping"):
        m = _CLAUDE_ROW.match(line)
        if not m or "never published" in m.group(2):
            continue
        upstream = _UPSTREAM.search(m.group(2))
        assert upstream, f"CLAUDE.md row names no upstream version: {line}"
        rows[m.group(1)] = upstream.group(1)
    return rows


def _sync_rows() -> dict[str, str]:
    """Final (non-pre-release) versions in UPSTREAM_SYNC.md -> upstream version."""
    rows: dict[str, str] = {}
    for line in _section((ROOT / "docs" / "UPSTREAM_SYNC.md").read_text(), "## Version Mapping"):
        m = _SYNC_ROW.match(line)
        if m and re.fullmatch(r"[\d.]+", m.group(1)):
            rows[m.group(1)] = m.group(2)
    return rows


def test_version_mapping_tables_list_the_same_releases_and_upstreams():
    claude, sync = _claude_rows(), _sync_rows()
    assert claude, "parsed no CLAUDE.md version rows"
    assert sync, "parsed no UPSTREAM_SYNC.md version rows"
    assert sorted(claude) == sorted(sync)
    assert claude == sync


def test_current_version_maps_to_upstream_parity():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert _claude_rows()[version] == chat_sdk.UPSTREAM_PARITY
    assert _sync_rows()[version] == chat_sdk.UPSTREAM_PARITY
