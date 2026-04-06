"""Chat SDK adapters for various platforms."""

from chat_sdk.adapters.discord import DiscordAdapter, create_discord_adapter
from chat_sdk.adapters.github import GitHubAdapter, create_github_adapter
from chat_sdk.adapters.google_chat import GoogleChatAdapter, create_google_chat_adapter
from chat_sdk.adapters.linear import LinearAdapter, create_linear_adapter
from chat_sdk.adapters.slack import SlackAdapter, create_slack_adapter
from chat_sdk.adapters.teams import TeamsAdapter, create_teams_adapter
from chat_sdk.adapters.telegram import TelegramAdapter, create_telegram_adapter
from chat_sdk.adapters.whatsapp import WhatsAppAdapter, create_whatsapp_adapter

__all__ = [
    "DiscordAdapter",
    "GitHubAdapter",
    "GoogleChatAdapter",
    "LinearAdapter",
    "SlackAdapter",
    "TeamsAdapter",
    "TelegramAdapter",
    "WhatsAppAdapter",
    "create_discord_adapter",
    "create_github_adapter",
    "create_google_chat_adapter",
    "create_linear_adapter",
    "create_slack_adapter",
    "create_teams_adapter",
    "create_telegram_adapter",
    "create_whatsapp_adapter",
]
