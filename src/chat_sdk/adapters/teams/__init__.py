"""Teams adapter for chat-sdk.

The high-level adapter is loaded lazily (PEP 562) so that the low-level
primitive subpaths (``chat_sdk.adapters.teams.format`` / ``.webhook`` /
``.api`` / ``.cards_input`` / ``.graph`` / ``.modals``) can be imported
without pulling in the full adapter runtime or the Microsoft Teams SDK —
mirroring upstream's ``@chat-adapter/teams/*`` subpath export boundary
and the 0.4.30 Slack subpath pattern (vercel/chat#538).

The public contract below MUST stay byte-identical: every name in
``__all__`` resolves to exactly the same object it did when this module
imported eagerly.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat_sdk.adapters.teams.adapter import TeamsAdapter as TeamsAdapter
    from chat_sdk.adapters.teams.adapter import create_teams_adapter as create_teams_adapter
    from chat_sdk.adapters.teams.types import TeamsAdapterConfig as TeamsAdapterConfig
    from chat_sdk.adapters.teams.types import TeamsAuthCertificate as TeamsAuthCertificate

# Maps each public export to the module that defines it. Resolving lazily
# keeps the primitive subpaths free of the adapter/SDK runtime.
_EXPORT_MODULES: dict[str, str] = {
    "TeamsAdapter": "chat_sdk.adapters.teams.adapter",
    "create_teams_adapter": "chat_sdk.adapters.teams.adapter",
    "TeamsAdapterConfig": "chat_sdk.adapters.teams.types",
    "TeamsAuthCertificate": "chat_sdk.adapters.teams.types",
}

__all__ = [
    "TeamsAdapter",
    "TeamsAdapterConfig",
    "TeamsAuthCertificate",
    "create_teams_adapter",
]


def __getattr__(name: str) -> object:
    module_path = _EXPORT_MODULES.get(name)
    if module_path is not None:
        module = importlib.import_module(module_path)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
