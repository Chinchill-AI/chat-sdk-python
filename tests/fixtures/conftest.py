"""Fixture helpers for loading replay test fixtures.

Fixtures are JSON files from the TS Chat SDK integration tests.
They live in tests/fixtures/replay/ (copied from the TS repo).
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "replay"

# Fallback to TS repo path if fixtures haven't been copied yet
_TS_FIXTURE_DIR = Path("/tmp/vercel-chat/packages/integration-tests/fixtures/replay")


def load_fixture(relative_path: str) -> dict:
    """Load a JSON fixture file by relative path (e.g., 'slack.json')."""
    local_path = FIXTURE_DIR / relative_path
    if local_path.exists():
        return json.loads(local_path.read_text())

    ts_path = _TS_FIXTURE_DIR / relative_path
    if ts_path.exists():
        return json.loads(ts_path.read_text())

    raise FileNotFoundError(
        f"Fixture not found: {relative_path}\n"
        f"  Looked in: {FIXTURE_DIR}\n"
        f"  Fallback:  {_TS_FIXTURE_DIR}\n"
        f"  Run: python tests/fixtures/copy_fixtures.py"
    )
