"""Teams ``installationUpdate`` action parsing.

Port of ``packages/adapter-teams/src/installation.ts`` (vercel/chat
``2e2426d1`` #914, chat@4.41.0). SDK-free: imports nothing from the
Microsoft Teams SDK.
"""

from __future__ import annotations

from typing import Any, Literal, TypeGuard

from chat_sdk.types import InstallationAction

# The Teams SDK types only ``add`` and ``remove``; Microsoft also documents the
# upgrade variants, so validate the wire value instead of trusting the union.
INSTALLATION_ACTIONS: tuple[InstallationAction, ...] = (
    "add",
    "add-upgrade",
    "remove",
    "remove-upgrade",
)


def parse_installation_action(value: Any) -> InstallationAction | None:
    """Return ``value`` when it is a known installation action, else ``None``."""
    if not isinstance(value, str):
        return None
    for action in INSTALLATION_ACTIONS:
        if action == value:
            return action
    return None


def is_install_action(action: InstallationAction) -> TypeGuard[Literal["add", "add-upgrade"]]:
    """Whether ``action`` installs the bot (``add`` / ``add-upgrade``)."""
    return action in ("add", "add-upgrade")
