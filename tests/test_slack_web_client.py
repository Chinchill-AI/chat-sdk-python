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


# ---------------------------------------------------------------------------
# Sync resolver: invoked directly from ``web_client`` (Codex P2 fix)
# ---------------------------------------------------------------------------


class TestWebClientSyncResolver:
    """Cover the sync-callable ``bot_token`` branch in ``_get_token``.

    Before the fix, ``_get_token`` only handled the static-string and primed
    cache cases — sync callables (used e.g. for lazy secret-manager loads or
    rotation) raised ``AuthenticationError`` from ``web_client`` outside any
    webhook / ContextVar scope, so proactive sends were impossible until an
    async path had primed the cache. The sync-callable branch invokes the
    resolver fresh on every access to honor the rotation contract documented
    on :attr:`SlackAdapterConfig.bot_token`.
    """

    def test_sync_callable_invoked_fresh_each_access(self):
        """Sync ``bot_token`` callables are invoked on *every* sync read.

        Load-bearing for two contracts:

        1. The bot_token contract in ``types.py`` ("called on each use to
           support rotation").
        2. The audit fix that made sync resolvers reachable from
           ``web_client`` at all (reverting it makes the first access raise).

        If the resolver were cached after the first call, a rotating
        secret-manager-backed resolver would silently freeze on the
        original value.
        """
        tokens = iter(["xoxb-sync-1", "xoxb-sync-2", "xoxb-sync-3"])
        calls = {"n": 0}

        def resolver() -> str:
            calls["n"] += 1
            return next(tokens)

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))

        first = adapter.web_client
        assert first.token == "xoxb-sync-1"
        assert calls["n"] == 1

        # Second access must re-invoke the resolver and see the rotated
        # token — caching would break rotation.
        second = adapter.web_client
        assert second.token == "xoxb-sync-2"
        assert calls["n"] == 2
        # WebClient cache is keyed by token; a new token yields a new client.
        assert second is not first

        third = adapter.current_token
        assert third == "xoxb-sync-3"
        assert calls["n"] == 3

        # Crucially, the dynamic-resolver path must NOT prime the
        # process-wide cache — that would suppress future resolver calls.
        assert adapter._default_bot_token_cache is None

    def test_async_callable_in_sync_context_raises(self):
        """``async def`` resolvers cannot be awaited from the sync property."""

        async def resolver() -> str:
            return "xoxb-async-resolved"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))

        with pytest.raises(AuthenticationError) as excinfo:
            _ = adapter.web_client

        # Not brittle about exact wording — just confirm the message points at
        # the sync/async resolver mismatch rather than the generic
        # "no bot token" path.
        message = str(excinfo.value).lower()
        assert "resolver" in message
        assert "async" in message

    def test_sync_callable_returning_coroutine_raises(self):
        """Defensive: a sync callable that returns a coroutine must not be cached.

        The production code is responsible for closing the orphaned coroutine
        before raising — otherwise callers see a noisy ``coroutine was never
        awaited`` RuntimeWarning. We pin that behavior by capturing warnings
        and asserting none were emitted.
        """
        import warnings

        async def _coro() -> str:
            return "xoxb-would-be-resolved"

        def resolver():
            # ``iscoroutinefunction`` returns False for this — the awaitable
            # only appears at call time. Caching the coroutine would be a
            # latent bug; the defensive check must raise AND close the
            # coroutine to silence the warning.
            return _coro()

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with pytest.raises(AuthenticationError) as excinfo:
                _ = adapter.web_client

        message = str(excinfo.value).lower()
        assert "awaitable" in message or "async" in message
        # Cache must not have been poisoned with the coroutine object.
        assert adapter._default_bot_token_cache is None
        # No "coroutine was never awaited" RuntimeWarning leaked — production
        # code closed the coroutine before raising.
        unawaited = [
            str(w.message)
            for w in captured
            if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
        ]
        assert unawaited == [], f"unexpected un-awaited coroutine warnings: {unawaited}"

    def test_sync_callable_returning_empty_string_raises(self):
        """An empty/invalid resolver result raises rather than caching it."""

        def resolver() -> str:
            return ""

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))

        with pytest.raises(AuthenticationError):
            _ = adapter.web_client
        assert adapter._default_bot_token_cache is None


class TestInvalidateClientClearsTokenCaches:
    """Pin that ``_invalidate_client`` clears the resolved-token caches for
    dynamic-resolver configs.

    Before the fix, ``_invalidate_client`` only evicted the WebClient and
    AsyncWebClient caches — the next ``_get_token`` would still return the
    revoked token from ``_default_bot_token_cache`` (or the per-request
    ContextVar), so a 401-driven invalidation just rebuilt clients around the
    same revoked credential.
    """

    def test_invalidate_clears_default_cache_for_dynamic_resolver(self):
        """Async-resolver path: after invalidation the cache is cleared.

        Load-bearing: with the fix reverted, ``_default_bot_token_cache``
        still equals the revoked token after invalidation.
        """

        async def resolver() -> str:
            return "xoxb-revoked"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))
        # Simulate the async path having already primed the cache.
        adapter._default_bot_token_cache = "xoxb-revoked"

        adapter._invalidate_client("xoxb-revoked")

        assert adapter._default_bot_token_cache is None

    def test_invalidate_clears_per_request_token_for_dynamic_resolver(self):
        """ContextVar-primed per-request token is cleared on invalidation."""

        async def resolver() -> str:
            return "xoxb-revoked"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))
        adapter._resolved_default_token.set("xoxb-revoked")

        adapter._invalidate_client("xoxb-revoked")

        assert adapter._resolved_default_token.get() is None

    def test_invalidate_static_string_does_not_clear_token_cache(self):
        """Static-string ``bot_token`` configs have no refresh path.

        Clearing the cache for a static-string config would just make every
        subsequent sync access raise, with no way to recover. The fix
        intentionally guards on ``_is_dynamic_bot_token`` to preserve the
        static-string fast path.
        """
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token="xoxb-static"))
        assert adapter._default_bot_token_cache == "xoxb-static"

        adapter._invalidate_client("xoxb-static")

        # Client caches evicted but token cache retained.
        assert adapter._default_bot_token_cache == "xoxb-static"

    def test_invalidate_different_token_does_not_clear_cache(self):
        """Only invalidation of the *currently cached* token clears it."""

        async def resolver() -> str:
            return "xoxb-current"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret=_SECRET, bot_token=resolver))
        adapter._default_bot_token_cache = "xoxb-current"

        adapter._invalidate_client("xoxb-some-other-token")

        assert adapter._default_bot_token_cache == "xoxb-current"
