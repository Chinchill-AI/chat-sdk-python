"""Teams adapter for chat-sdk."""

from chat_sdk.adapters.teams.adapter import TeamsAdapter, create_teams_adapter
from chat_sdk.adapters.teams.types import (
    TeamsAdapterConfig,
    TeamsAuthCertificate,
)

__all__ = [
    "TeamsAdapter",
    "TeamsAdapterConfig",
    "TeamsAuthCertificate",
    "create_teams_adapter",
]
