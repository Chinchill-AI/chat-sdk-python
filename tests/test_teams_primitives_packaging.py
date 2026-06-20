"""Packaging boundary for the Teams primitive subpaths (Wave-B T7).

The six SDK-free Teams primitive subpaths --

    chat_sdk.adapters.teams.format
    chat_sdk.adapters.teams.webhook
    chat_sdk.adapters.teams.api
    chat_sdk.adapters.teams.cards_input
    chat_sdk.adapters.teams.graph
    chat_sdk.adapters.teams.modals

-- each landed with a *static source-scan* boundary test that asserted the
module's own source never imports the Teams SDK, an HTTP client, or the
high-level adapter. Those per-PR tests explicitly *deferred* the runtime
``sys.modules`` guarantee to this packaging PR, because importing any subpath
ran the package's ``teams/__init__.py``, which (while still eager) pulled the
adapter in transitively.

Now that ``teams/__init__.py`` is PEP-562 lazy (mirroring the 0.4.30 Slack
subpath pattern), importing a primitive subpath in a *fresh interpreter* must
leave the Teams SDK, the HTTP clients, and the adapter module entirely out of
``sys.modules``. This is the runtime-free guarantee the source-scans deferred,
proven once here for all six subpaths.

It also pins the public-API contract: making ``teams/__init__.py`` lazy must
not change that ``TeamsAdapter`` / ``create_teams_adapter`` /
``TeamsAdapterConfig`` / ``TeamsAuthCertificate`` still import from the package
root (the 0.4.30 Teams SDK migration's public surface is coupled downstream).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# The six SDK-free primitive subpaths packaged by T7.
_PRIMITIVE_SUBPATHS = [
    "chat_sdk.adapters.teams.format",
    "chat_sdk.adapters.teams.webhook",
    "chat_sdk.adapters.teams.api",
    "chat_sdk.adapters.teams.cards_input",
    "chat_sdk.adapters.teams.graph",
    "chat_sdk.adapters.teams.modals",
]

# Modules that must NEVER load when a primitive subpath is imported: the
# Microsoft Teams SDK (root + every submodule the adapter reaches for), both
# HTTP clients, and the high-level adapter module itself.
_FORBIDDEN_MODULES = [
    "microsoft_teams",
    "microsoft_teams.apps",
    "microsoft_teams.api",
    "microsoft_teams.common",
    "httpx",
    "aiohttp",
    "chat_sdk.adapters.teams.adapter",
]


def _import_in_fresh_interpreter(subpath: str) -> subprocess.CompletedProcess[str]:
    """Import ``subpath`` in a brand-new interpreter and assert the forbidden
    runtime modules are absent from ``sys.modules`` afterwards."""
    forbidden = repr(_FORBIDDEN_MODULES)
    code = (
        "import sys\n"
        f"import {subpath}\n"
        f"forbidden = {forbidden}\n"
        "loaded = [name for name in forbidden if name in sys.modules]\n"
        f"assert not loaded, "
        f"'{subpath} eagerly imported runtime modules: ' + repr(loaded)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )


class TestTeamsPrimitiveSubpathBoundary:
    """The runtime-free guarantee the per-PR source-scans deferred to T7."""

    @pytest.mark.parametrize("subpath", _PRIMITIVE_SUBPATHS)
    def test_subpath_import_is_runtime_free(self, subpath: str) -> None:
        result = _import_in_fresh_interpreter(subpath)
        assert result.returncode == 0, (
            f"{subpath} pulled in a forbidden runtime module\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_all_six_subpaths_are_covered(self) -> None:
        # Guard against the list silently shrinking: T7 packages exactly the
        # six SDK-free primitive subpaths.
        assert len(_PRIMITIVE_SUBPATHS) == 6
        assert len(set(_PRIMITIVE_SUBPATHS)) == 6


class TestTeamsPublicApiSurvivesLazyInit:
    """The lazy ``teams/__init__.py`` must preserve the public contract."""

    def test_public_adapter_api_still_imports_from_package_root(self) -> None:
        # Fresh interpreter: the four public names must resolve from the
        # package root exactly as they did when __init__ imported eagerly.
        code = (
            "from chat_sdk.adapters.teams import (\n"
            "    TeamsAdapter,\n"
            "    create_teams_adapter,\n"
            "    TeamsAdapterConfig,\n"
            "    TeamsAuthCertificate,\n"
            ")\n"
            "assert TeamsAdapter.__name__ == 'TeamsAdapter'\n"
            "assert create_teams_adapter.__name__ == 'create_teams_adapter'\n"
            "assert TeamsAdapterConfig.__name__ == 'TeamsAdapterConfig'\n"
            "assert TeamsAuthCertificate.__name__ == 'TeamsAuthCertificate'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_lazily_resolved_objects_are_identical_to_direct_imports(self) -> None:
        from chat_sdk.adapters.teams import (
            TeamsAdapter,
            TeamsAdapterConfig,
            TeamsAuthCertificate,
            create_teams_adapter,
        )
        from chat_sdk.adapters.teams.adapter import (
            TeamsAdapter as DirectAdapter,
        )
        from chat_sdk.adapters.teams.adapter import (
            create_teams_adapter as direct_factory,
        )
        from chat_sdk.adapters.teams.types import (
            TeamsAdapterConfig as DirectConfig,
        )
        from chat_sdk.adapters.teams.types import (
            TeamsAuthCertificate as DirectCert,
        )

        assert TeamsAdapter is DirectAdapter
        assert create_teams_adapter is direct_factory
        assert TeamsAdapterConfig is DirectConfig
        assert TeamsAuthCertificate is DirectCert

    def test_public_all_is_unchanged(self) -> None:
        import chat_sdk.adapters.teams as teams_pkg

        assert teams_pkg.__all__ == [
            "TeamsAdapter",
            "TeamsAdapterConfig",
            "TeamsAuthCertificate",
            "create_teams_adapter",
        ]

    def test_unknown_attribute_raises_attribute_error(self) -> None:
        import chat_sdk.adapters.teams as teams_pkg

        with pytest.raises(AttributeError):
            _ = teams_pkg.NoSuchExport  # type: ignore[attr-defined]

    def test_top_level_adapters_package_still_resolves_teams(self) -> None:
        from chat_sdk.adapters import TeamsAdapter, create_teams_adapter
        from chat_sdk.adapters.teams.adapter import (
            TeamsAdapter as DirectAdapter,
        )
        from chat_sdk.adapters.teams.adapter import (
            create_teams_adapter as direct_factory,
        )

        assert TeamsAdapter is DirectAdapter
        assert create_teams_adapter is direct_factory
