"""Slack adapter for chat-sdk.

The high-level adapter is loaded lazily (PEP 562) so that the low-level
primitive subpaths (``chat_sdk.adapters.slack.webhook``) can be imported
without pulling in the full adapter runtime — mirroring upstream's
``@chat-adapter/slack/webhook`` subpath export boundary (vercel/chat#538).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat_sdk.adapters.slack.adapter import SlackAdapter as SlackAdapter
    from chat_sdk.adapters.slack.adapter import create_slack_adapter as create_slack_adapter

__all__ = ["SlackAdapter", "create_slack_adapter"]


def __getattr__(name: str) -> object:
    if name in __all__:
        module = importlib.import_module("chat_sdk.adapters.slack.adapter")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
