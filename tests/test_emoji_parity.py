"""Emoji parity test -- verify Python DEFAULT_EMOJI_MAP matches the TS
DEFAULT_EMOJI_MAP entry-by-entry.

For every emoji in the TS map, assert:
- emoji_to_slack(name) == TS slack[0]
- emoji_to_gchat(name) == TS gchat (or gchat[0] if array)

The TS emoji map is parsed from /tmp/vercel-chat/packages/chat/src/emoji.ts
and compared against Python's DEFAULT_EMOJI_MAP.

References issue #18 (cross-SDK parity).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chat_sdk.emoji import DEFAULT_EMOJI_MAP, EmojiResolver

# ---------------------------------------------------------------------------
# Parse TS emoji map
# ---------------------------------------------------------------------------

_TS_EMOJI_PATH = Path("/tmp/vercel-chat/packages/chat/src/emoji.ts")

# Matches lines like: thumbs_up: { slack: ["+1", "thumbsup"], gchat: "..." },
# or:  "100": { slack: "100", gchat: "..." },
_ENTRY_RE = re.compile(
    r'^\s+"?(\w+)"?\s*:\s*\{',
    re.MULTILINE,
)

# Matches slack: "value" or slack: ["v1", "v2"]
_SLACK_RE = re.compile(r'slack:\s*(?:\[([^\]]+)\]|"([^"]+)")')
# Matches gchat: "value" or gchat: ["v1", "v2"]
_GCHAT_RE = re.compile(r'gchat:\s*(?:\[([^\]]+)\]|"([^"]+)")')


def _parse_quoted_values(raw: str) -> list[str]:
    """Extract quoted strings from a comma-separated list."""
    return re.findall(r'"([^"]+)"', raw)


def _parse_ts_emoji_map() -> dict[str, dict[str, list[str]]]:
    """Parse the TS DEFAULT_EMOJI_MAP from emoji.ts source text.

    Returns a dict mapping emoji name -> {"slack": [...], "gchat": [...]}.
    """
    if not _TS_EMOJI_PATH.exists():
        pytest.skip(f"TS emoji source not found: {_TS_EMOJI_PATH}")

    text = _TS_EMOJI_PATH.read_text()

    # Find the DEFAULT_EMOJI_MAP block
    start = text.find("DEFAULT_EMOJI_MAP")
    if start == -1:
        pytest.skip("DEFAULT_EMOJI_MAP not found in TS source")

    # Find the closing brace
    block_start = text.find("{", start)
    if block_start == -1:
        pytest.skip("Could not find opening brace for DEFAULT_EMOJI_MAP")

    # Simple brace-counting to find the end of the map
    depth = 0
    block_end = block_start
    for i in range(block_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                block_end = i + 1
                break

    map_text = text[block_start:block_end]

    # Parse each entry
    ts_map: dict[str, dict[str, list[str]]] = {}

    # Split into individual entries (between matching braces for each key)
    # Use regex to find key-value pairs
    entries = re.finditer(
        r'(?:"(\w+)"|(\w+))\s*:\s*\{([^}]+)\}',
        map_text,
    )

    for match in entries:
        name = match.group(1) or match.group(2)
        body = match.group(3)

        # Parse slack value
        slack_match = _SLACK_RE.search(body)
        slack_values: list[str] = []
        if slack_match:
            if slack_match.group(1):
                slack_values = _parse_quoted_values(slack_match.group(1))
            elif slack_match.group(2):
                slack_values = [slack_match.group(2)]

        # Parse gchat value
        gchat_match = _GCHAT_RE.search(body)
        gchat_values: list[str] = []
        if gchat_match:
            if gchat_match.group(1):
                gchat_values = _parse_quoted_values(gchat_match.group(1))
            elif gchat_match.group(2):
                gchat_values = [gchat_match.group(2)]

        if slack_values or gchat_values:
            ts_map[name] = {"slack": slack_values, "gchat": gchat_values}

    return ts_map


# Cache parsed TS map
_TS_MAP: dict[str, dict[str, list[str]]] | None = None


def _get_ts_map() -> dict[str, dict[str, list[str]]]:
    global _TS_MAP
    if _TS_MAP is None:
        _TS_MAP = _parse_ts_emoji_map()
    return _TS_MAP


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEmojiParity:
    """Verify Python DEFAULT_EMOJI_MAP matches TS DEFAULT_EMOJI_MAP."""

    @pytest.fixture(autouse=True)
    def setup_resolver(self):
        self.resolver = EmojiResolver()
        self.ts_map = _get_ts_map()

    def test_ts_map_parsed_successfully(self):
        """Ensure we parsed a reasonable number of entries from TS source."""
        assert len(self.ts_map) > 50, f"Expected > 50 TS emoji entries, got {len(self.ts_map)}"

    def test_all_ts_keys_exist_in_python(self):
        """Every key in the TS map should exist in Python's DEFAULT_EMOJI_MAP."""
        missing = set(self.ts_map.keys()) - set(DEFAULT_EMOJI_MAP.keys())
        assert not missing, f"TS keys missing from Python DEFAULT_EMOJI_MAP: {sorted(missing)}"

    def test_slack_first_value_matches(self):
        """For each shared key, emoji_to_slack(name) should match TS slack[0]."""
        mismatches: list[str] = []
        for name, ts_entry in self.ts_map.items():
            if name not in DEFAULT_EMOJI_MAP:
                continue
            ts_slack = ts_entry["slack"]
            if not ts_slack:
                continue

            py_slack = self.resolver.to_slack(name)
            ts_first = ts_slack[0]

            if py_slack != ts_first:
                mismatches.append(f"  {name}: Python={py_slack!r}, TS={ts_first!r} (TS full={ts_slack})")

        assert not mismatches, "Slack emoji mismatches between Python and TS:\n" + "\n".join(mismatches)

    def test_gchat_first_value_matches(self):
        """For each shared key, emoji_to_gchat(name) should match TS gchat[0]."""
        mismatches: list[str] = []
        for name, ts_entry in self.ts_map.items():
            if name not in DEFAULT_EMOJI_MAP:
                continue
            ts_gchat = ts_entry["gchat"]
            if not ts_gchat:
                continue

            py_gchat = self.resolver.to_gchat(name)
            ts_first = ts_gchat[0]

            if py_gchat != ts_first:
                mismatches.append(f"  {name}: Python={py_gchat!r}, TS={ts_first!r} (TS full={ts_gchat})")

        assert not mismatches, "GChat emoji mismatches between Python and TS:\n" + "\n".join(mismatches)

    def test_slack_all_values_present(self):
        """All Slack aliases in TS should appear somewhere in Python's slack list."""
        missing_aliases: list[str] = []
        for name, ts_entry in self.ts_map.items():
            if name not in DEFAULT_EMOJI_MAP:
                continue

            py_formats = DEFAULT_EMOJI_MAP[name]
            py_slack = py_formats.slack if isinstance(py_formats.slack, list) else [py_formats.slack]

            for ts_alias in ts_entry["slack"]:
                if ts_alias not in py_slack:
                    missing_aliases.append(f"  {name}: TS alias {ts_alias!r} not in Python {py_slack}")

        assert not missing_aliases, "Slack aliases in TS missing from Python:\n" + "\n".join(missing_aliases)

    def test_gchat_all_values_present(self):
        """All GChat values in TS should appear somewhere in Python's gchat list."""
        missing_values: list[str] = []
        for name, ts_entry in self.ts_map.items():
            if name not in DEFAULT_EMOJI_MAP:
                continue

            py_formats = DEFAULT_EMOJI_MAP[name]
            py_gchat = py_formats.gchat if isinstance(py_formats.gchat, list) else [py_formats.gchat]

            for ts_val in ts_entry["gchat"]:
                if ts_val not in py_gchat:
                    missing_values.append(f"  {name}: TS gchat {ts_val!r} not in Python {py_gchat}")

        assert not missing_values, "GChat values in TS missing from Python:\n" + "\n".join(missing_values)

    def test_python_only_keys_documented(self):
        """Report Python-only keys (not in TS) -- these are allowed extensions."""
        python_only = set(DEFAULT_EMOJI_MAP.keys()) - set(self.ts_map.keys())
        # These are expected Python-only extensions (aliases/extras)
        # This test documents them rather than failing
        if python_only:
            # Just verify they are valid (have slack and gchat values)
            for name in python_only:
                formats = DEFAULT_EMOJI_MAP[name]
                assert formats.slack, f"Python-only key {name!r} has empty slack"
                assert formats.gchat, f"Python-only key {name!r} has empty gchat"

    def test_emoji_resolver_roundtrip(self):
        """Verify that to_slack/from_slack and to_gchat/from_gchat round-trip
        for all entries in the shared map.

        Note: Some emoji share the same platform value (e.g., megaphone and
        loudspeaker both map to gchat '📢'). In these cases, reverse lookup
        returns whichever was registered first. We accept any name that maps
        to the same platform value as a valid roundtrip.
        """
        for name in self.ts_map:
            if name not in DEFAULT_EMOJI_MAP:
                continue

            # Slack roundtrip: name -> to_slack -> from_slack -> name
            slack_format = self.resolver.to_slack(name)
            back_from_slack = self.resolver.from_slack(slack_format)
            # The reverse-resolved name should map back to the same Slack value
            re_slack = self.resolver.to_slack(back_from_slack.name)
            assert re_slack == slack_format, (
                f"Slack roundtrip failed for {name}: "
                f"{name} -> {slack_format!r} -> {back_from_slack.name} -> {re_slack!r}"
            )

            # GChat roundtrip: name -> to_gchat -> from_gchat -> name
            gchat_format = self.resolver.to_gchat(name)
            back_from_gchat = self.resolver.from_gchat(gchat_format)
            # The reverse-resolved name should map back to the same GChat value
            re_gchat = self.resolver.to_gchat(back_from_gchat.name)
            assert re_gchat == gchat_format, (
                f"GChat roundtrip failed for {name}: "
                f"{name} -> {gchat_format!r} -> {back_from_gchat.name} -> {re_gchat!r}"
            )

    @pytest.mark.parametrize("name", list(_get_ts_map().keys()) if _TS_EMOJI_PATH.exists() else [])
    def test_individual_emoji_parity(self, name: str):
        """Each TS emoji entry individually matches Python."""
        ts_entry = self.ts_map[name]
        assert name in DEFAULT_EMOJI_MAP, f"TS emoji {name!r} not in Python map"

        DEFAULT_EMOJI_MAP[name]

        # Check slack[0] matches
        if ts_entry["slack"]:
            py_slack = self.resolver.to_slack(name)
            assert py_slack == ts_entry["slack"][0], (
                f"{name} slack mismatch: Python={py_slack!r}, TS={ts_entry['slack'][0]!r}"
            )

        # Check gchat[0] matches
        if ts_entry["gchat"]:
            py_gchat = self.resolver.to_gchat(name)
            assert py_gchat == ts_entry["gchat"][0], (
                f"{name} gchat mismatch: Python={py_gchat!r}, TS={ts_entry['gchat'][0]!r}"
            )
