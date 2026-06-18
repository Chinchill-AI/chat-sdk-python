"""Error type for the Slack Block Kit primitives subpath.

Port of ``packages/adapter-slack/src/blocks/errors.ts`` (vercel/chat#555).
"""

from __future__ import annotations


class SlackBlockError(Exception):
    """Raised when a Slack card element cannot be converted to Block Kit."""
