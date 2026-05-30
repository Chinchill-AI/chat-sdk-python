"""Tests for the public ``SlackAdapter.web_client`` property and ``client`` alias.

Port of upstream vercel/chat's ``webClient getter`` test block
(``packages/adapter-slack/src/index.test.ts``), added by commits
``8366b8b`` / ``fdebde7`` / ``2f108bd`` (PRs #471 / #476 / #478): the Slack
adapter exposes a synchronous ``WebClient`` bound to the current
request-context token (multi-workspace) or the configured default token
(single-workspace), with a one-release deprecated ``client`` alias.

``web_client`` resolves its token via the standard 3-level resolver
(ContextVar token > static default token > ``AuthenticationError``) and
caches one ``WebClient`` per distinct token.
"""

from __future__ import annotations

import sys
import warnings
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Stub slack_sdk so tests run without the real dependency installed.
# Stub BOTH the sync ``slack_sdk.WebClient`` (backing ``web_client``) and the
# async ``slack_sdk.web.async_client.AsyncWebClient`` (backing the adapter's
# own API calls) so the deferred imports inside the adapter resolve here.
# ---------------------------------------------------------------------------


class _FakeWebClient:
    """Minimal stand-in for the synchronous ``slack_sdk.WebClient``."""

    def __init__(self, *, token: str = "") -> None:
        self.token = token


class _FakeAsyncWebClient:
    """Minimal stand-in for ``slack_sdk.web.async_client.AsyncWebClient``."""

    def __init__(self, *, token: str = "") -> None:
        self.token = token


# Reuse any ``slack_sdk`` stub already registered by a sibling test module
# (e.g. ``test_slack_client_cache.py``) when collected in the same process —
# ``setdefault`` would otherwise no-op and leave a stub that lacks the sync
# ``WebClient`` symbol. Attach the symbols we need either way so the deferred
# imports inside the adapter resolve regardless of collection order.
_fake_slack_sdk = sys.modules.setdefault("slack_sdk", ModuleType("slack_sdk"))
_fake_slack_sdk_web = sys.modules.setdefault("slack_sdk.web", ModuleType("slack_sdk.web"))
_fake_slack_sdk_web_async = sys.modules.setdefault(
    "slack_sdk.web.async_client", ModuleType("slack_sdk.web.async_client")
)

if not hasattr(_fake_slack_sdk, "WebClient"):
    _fake_slack_sdk.WebClient = _FakeWebClient  # type: ignore[attr-defined]
if not hasattr(_fake_slack_sdk_web_async, "AsyncWebClient"):
    _fake_slack_sdk_web_async.AsyncWebClient = _FakeAsyncWebClient  # type: ignore[attr-defined]
_fake_slack_sdk_web.async_client = _fake_slack_sdk_web_async  # type: ignore[attr-defined]
_fake_slack_sdk.web = _fake_slack_sdk_web  # type: ignore[attr-defined]

from chat_sdk.adapters.slack.adapter import SlackAdapter  # noqa: E402, I001
from chat_sdk.adapters.slack.types import SlackAdapterConfig  # noqa: E402
from chat_sdk.shared.errors import AuthenticationError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SECRET = "test-signing-secret"


def _single_workspace_adapter(bot_token: str = "xoxb-static-token") -> SlackAdapter:
    """Single-workspace adapter with a static default bot token."""
    return SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=bot_token))


def _multi_workspace_adapter() -> SlackAdapter:
    """Multi-workspace adapter (no default bot token; tokens are per-team)."""
    return SlackAdapter(
        SlackAdapterConfig(
            signing_secret=_SECRET,
            client_id="test-client-id",
            client_secret="test-client-secret",
        )
    )


# ---------------------------------------------------------------------------
# Single-workspace
# ---------------------------------------------------------------------------


class TestWebClientSingleWorkspace:
    def test_returns_client_bound_to_static_bot_token(self):
        """``web_client`` returns a WebClient bound to the configured token."""
        adapter = _single_workspace_adapter("xoxb-static-token")

        web_client = adapter.web_client

        # Constructed via ``slack_sdk.WebClient`` (whichever class is
        # registered in this process — real or sibling stub).
        assert isinstance(web_client, _fake_slack_sdk.WebClient)
        assert web_client.token == "xoxb-static-token"

    def test_returns_same_instance_per_token(self):
        """Repeated access returns the exact same cached object (identity)."""
        adapter = _single_workspace_adapter()

        # Bind each property access to a name so the identity check reads as a
        # caching assertion (two calls → same object), not a tautological
        # ``x is x`` self-comparison.
        first = adapter.web_client
        second = adapter.web_client

        assert first is second

    def test_caches_under_the_resolved_token(self):
        """The cached client is keyed by the resolved token."""
        adapter = _single_workspace_adapter("xoxb-cache-key")

        client = adapter.web_client

        assert adapter._web_client_cache["xoxb-cache-key"] is client

    def test_invalidate_client_clears_web_client_cache(self):
        """``_invalidate_client`` evicts the sync WebClient too (token revocation).

        Load-bearing: without the ``_web_client_cache.pop`` in
        ``_invalidate_client`` a revoked token's stale ``WebClient`` would
        survive eviction and this assertion fails.
        """
        adapter = _single_workspace_adapter("xoxb-revoke")
        client = adapter.web_client
        assert adapter._web_client_cache["xoxb-revoke"] is client

        adapter._invalidate_client("xoxb-revoke")

        assert "xoxb-revoke" not in adapter._web_client_cache


# ---------------------------------------------------------------------------
# Deprecated ``client`` alias
# ---------------------------------------------------------------------------


class TestClientAlias:
    def test_alias_returns_same_object_as_web_client(self):
        """The deprecated ``client`` alias returns the same instance."""
        adapter = _single_workspace_adapter()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert adapter.client is adapter.web_client

    def test_alias_emits_deprecation_warning(self):
        """Accessing ``client`` warns; ``web_client`` does not."""
        adapter = _single_workspace_adapter()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = adapter.client

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1
        assert "web_client" in str(deprecations[0].message)

    def test_web_client_does_not_warn(self):
        """The non-deprecated ``web_client`` property emits no warning."""
        adapter = _single_workspace_adapter()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = adapter.web_client

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations == []


# ---------------------------------------------------------------------------
# Multi-workspace (request-context token resolution)
# ---------------------------------------------------------------------------


class TestWebClientMultiWorkspace:
    def test_uses_request_context_token_under_with_bot_token(self):
        """Inside ``with_bot_token`` the context token wins."""
        adapter = _multi_workspace_adapter()

        observed: dict[str, str] = {}

        def capture() -> None:
            observed["token"] = adapter.web_client.token

        adapter.with_bot_token("xoxb-context-token", capture)

        assert observed["token"] == "xoxb-context-token"

    def test_context_token_overrides_static_default(self):
        """A context token takes precedence over the configured default."""
        adapter = _single_workspace_adapter("xoxb-default")

        observed: dict[str, str] = {}

        def capture() -> None:
            observed["token"] = adapter.web_client.token

        adapter.with_bot_token("xoxb-override", capture)

        assert observed["token"] == "xoxb-override"
        # Outside the context, resolution falls back to the static default.
        assert adapter.web_client.token == "xoxb-default"

    def test_raises_without_context_in_multi_workspace_mode(self):
        """No context + no default token raises on both accessors."""
        adapter = _multi_workspace_adapter()

        with pytest.raises(AuthenticationError):
            _ = adapter.web_client

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with pytest.raises(AuthenticationError):
                _ = adapter.client


# ---------------------------------------------------------------------------
# Async resolver: not-yet-resolved default token raises (sync property)
# ---------------------------------------------------------------------------


class TestWebClientAsyncResolver:
    def test_unresolved_async_resolver_raises(self):
        """A callable ``bot_token`` that has not run yet cannot resolve sync."""

        async def resolver() -> str:
            return "xoxb-async-resolved"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))

        with pytest.raises(AuthenticationError):
            _ = adapter.web_client
