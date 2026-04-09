"""Chat SDK adapters for various platforms.

Each adapter lives in its own sub-package and may require optional
dependencies (e.g. ``slack-sdk``, ``aiohttp``).  Import the adapter
you need directly::

    from chat_sdk.adapters.slack import SlackAdapter, create_slack_adapter

This top-level module uses lazy imports so that
``import chat_sdk.adapters`` never fails due to missing optional deps.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat_sdk.adapters.discord import DiscordAdapter as DiscordAdapter
    from chat_sdk.adapters.discord import create_discord_adapter as create_discord_adapter
    from chat_sdk.adapters.github import GitHubAdapter as GitHubAdapter
    from chat_sdk.adapters.github import create_github_adapter as create_github_adapter
    from chat_sdk.adapters.google_chat import GoogleChatAdapter as GoogleChatAdapter
    from chat_sdk.adapters.google_chat import create_google_chat_adapter as create_google_chat_adapter
    from chat_sdk.adapters.linear import LinearAdapter as LinearAdapter
    from chat_sdk.adapters.linear import create_linear_adapter as create_linear_adapter
    from chat_sdk.adapters.slack import SlackAdapter as SlackAdapter
    from chat_sdk.adapters.slack import create_slack_adapter as create_slack_adapter
    from chat_sdk.adapters.teams import TeamsAdapter as TeamsAdapter
    from chat_sdk.adapters.teams import create_teams_adapter as create_teams_adapter
    from chat_sdk.adapters.telegram import TelegramAdapter as TelegramAdapter
    from chat_sdk.adapters.telegram import create_telegram_adapter as create_telegram_adapter
    from chat_sdk.adapters.whatsapp import WhatsAppAdapter as WhatsAppAdapter
    from chat_sdk.adapters.whatsapp import create_whatsapp_adapter as create_whatsapp_adapter

_ADAPTER_MODULES: dict[str, str] = {
    "DiscordAdapter": "chat_sdk.adapters.discord",
    "create_discord_adapter": "chat_sdk.adapters.discord",
    "GitHubAdapter": "chat_sdk.adapters.github",
    "create_github_adapter": "chat_sdk.adapters.github",
    "GoogleChatAdapter": "chat_sdk.adapters.google_chat",
    "create_google_chat_adapter": "chat_sdk.adapters.google_chat",
    "LinearAdapter": "chat_sdk.adapters.linear",
    "create_linear_adapter": "chat_sdk.adapters.linear",
    "SlackAdapter": "chat_sdk.adapters.slack",
    "create_slack_adapter": "chat_sdk.adapters.slack",
    "TeamsAdapter": "chat_sdk.adapters.teams",
    "create_teams_adapter": "chat_sdk.adapters.teams",
    "TelegramAdapter": "chat_sdk.adapters.telegram",
    "create_telegram_adapter": "chat_sdk.adapters.telegram",
    "WhatsAppAdapter": "chat_sdk.adapters.whatsapp",
    "create_whatsapp_adapter": "chat_sdk.adapters.whatsapp",
}

__all__ = list(_ADAPTER_MODULES.keys())


def __getattr__(name: str) -> object:
    if name in _ADAPTER_MODULES:
        module = importlib.import_module(_ADAPTER_MODULES[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
