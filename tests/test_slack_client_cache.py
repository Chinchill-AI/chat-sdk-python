"""Tests for the Slack adapter client cache (LRU eviction + auth-error invalidation)."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Stub slack_sdk so tests run without the real dependency installed
# ---------------------------------------------------------------------------

_fake_slack_sdk = ModuleType("slack_sdk")
_fake_slack_sdk_web = ModuleType("slack_sdk.web")
_fake_slack_sdk_web_async = ModuleType("slack_sdk.web.async_client")


class _FakeAsyncWebClient:
    """Minimal stand-in for slack_sdk.web.async_client.AsyncWebClient."""

    def __init__(self, *, token: str = "") -> None:
        self.token = token


_fake_slack_sdk_web_async.AsyncWebClient = _FakeAsyncWebClient  # type: ignore[attr-defined]
_fake_slack_sdk_web.async_client = _fake_slack_sdk_web_async  # type: ignore[attr-defined]
_fake_slack_sdk.web = _fake_slack_sdk_web  # type: ignore[attr-defined]

# Patch sys.modules *before* importing the adapter so the deferred
# ``from slack_sdk.web.async_client import AsyncWebClient`` inside
# ``_get_client`` resolves to our stub.
sys.modules.setdefault("slack_sdk", _fake_slack_sdk)
sys.modules.setdefault("slack_sdk.web", _fake_slack_sdk_web)
sys.modules.setdefault("slack_sdk.web.async_client", _fake_slack_sdk_web_async)

from chat_sdk.adapters.slack.adapter import SlackAdapter  # noqa: E402, I001
from chat_sdk.adapters.slack.types import SlackAdapterConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides) -> SlackAdapter:
    """Create a SlackAdapter with minimal valid config."""
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-default-token"),
        **overrides,
    )
    return SlackAdapter(config)


# ---------------------------------------------------------------------------
# _get_client caching behaviour
# ---------------------------------------------------------------------------


class TestGetClientCache:
    """Verify the LRU client cache inside SlackAdapter."""

    def test_get_client_caches_by_token(self):
        """Same token must return the exact same client object (identity check)."""
        adapter = _make_adapter()
        client_a = adapter._get_client("tok-A")
        client_b = adapter._get_client("tok-A")
        assert client_a is client_b

    def test_get_client_different_tokens(self):
        """Different tokens must produce distinct client objects."""
        adapter = _make_adapter()
        client_a = adapter._get_client("tok-A")
        client_b = adapter._get_client("tok-B")
        assert client_a is not client_b
        assert client_a.token == "tok-A"
        assert client_b.token == "tok-B"

    def test_get_client_lru_eviction(self):
        """Inserting 101 clients should evict the oldest (cache max = 100)."""
        adapter = _make_adapter()
        assert adapter._client_cache_max == 100  # sanity-check the bound

        # Fill to capacity + 1
        for i in range(101):
            adapter._get_client(f"tok-{i}")

        # Oldest token (tok-0) should have been evicted
        assert "tok-0" not in adapter._client_cache
        # Newest should still be present
        assert "tok-100" in adapter._client_cache
        assert len(adapter._client_cache) == 100

    def test_get_client_lru_touch(self):
        """Accessing an old entry should move it to the end, protecting it from eviction."""
        adapter = _make_adapter()

        # Insert tokens 0..99 (fills to capacity)
        for i in range(100):
            adapter._get_client(f"tok-{i}")

        # Touch the oldest entry so it becomes "recently used"
        adapter._get_client("tok-0")

        # Now insert one more — should evict tok-1 (the new oldest), not tok-0
        adapter._get_client("tok-new")
        assert "tok-0" in adapter._client_cache, "tok-0 was touched and should survive eviction"
        assert "tok-1" not in adapter._client_cache, "tok-1 should be evicted as the new oldest"

    def test_get_client_none_resolves_default(self):
        """_get_client(None) should fall back to _get_token() (the default bot token)."""
        adapter = _make_adapter(bot_token="xoxb-fallback")
        client = adapter._get_client(None)
        assert client.token == "xoxb-fallback"
        # Should be cached under that token
        assert "xoxb-fallback" in adapter._client_cache

    def test_get_client_empty_string_preserved(self):
        """_get_client('') must use the empty string as-is (no substitution)."""
        adapter = _make_adapter(bot_token="xoxb-default")
        client = adapter._get_client("")
        assert client.token == ""
        assert "" in adapter._client_cache
        # The default token should NOT have been used
        assert "xoxb-default" not in adapter._client_cache


# ---------------------------------------------------------------------------
# _invalidate_client
# ---------------------------------------------------------------------------


class TestInvalidateClient:
    """Verify explicit cache invalidation."""

    def test_invalidate_client_removes_entry(self):
        """After invalidation the next _get_client call must create a fresh client."""
        adapter = _make_adapter()
        original = adapter._get_client("tok-X")
        adapter._invalidate_client("tok-X")

        assert "tok-X" not in adapter._client_cache

        refreshed = adapter._get_client("tok-X")
        assert refreshed is not original, "A new client should have been created"


# ---------------------------------------------------------------------------
# _handle_slack_error — auth-error eviction
# ---------------------------------------------------------------------------


def _make_slack_api_error(error_code: str) -> Exception:
    """Build a mock SlackApiError whose response contains *error_code*."""
    err = Exception(f"Slack error: {error_code}")
    err.response = {"error": error_code}  # type: ignore[attr-defined]
    return err


class TestHandleSlackErrorEviction:
    """_handle_slack_error should evict the cached client on auth errors."""

    def test_handle_slack_error_invalid_auth_evicts(self):
        """invalid_auth error must remove the client from cache."""
        adapter = _make_adapter(bot_token="xoxb-tok")
        adapter._get_client("xoxb-tok")  # populate cache
        assert "xoxb-tok" in adapter._client_cache

        with pytest.raises(Exception, match="invalid_auth"):
            adapter._handle_slack_error(_make_slack_api_error("invalid_auth"))

        assert "xoxb-tok" not in adapter._client_cache

    def test_handle_slack_error_token_revoked_evicts(self):
        """token_revoked error must remove the client from cache."""
        adapter = _make_adapter(bot_token="xoxb-tok")
        adapter._get_client("xoxb-tok")
        assert "xoxb-tok" in adapter._client_cache

        with pytest.raises(Exception, match="token_revoked"):
            adapter._handle_slack_error(_make_slack_api_error("token_revoked"))

        assert "xoxb-tok" not in adapter._client_cache

    def test_handle_slack_error_non_auth_error_keeps_cache(self):
        """A non-auth error (e.g. channel_not_found) must NOT evict the client."""
        adapter = _make_adapter(bot_token="xoxb-tok")
        adapter._get_client("xoxb-tok")
        assert "xoxb-tok" in adapter._client_cache

        with pytest.raises(Exception, match="channel_not_found"):
            adapter._handle_slack_error(_make_slack_api_error("channel_not_found"))

        assert "xoxb-tok" in adapter._client_cache, "Non-auth error should not evict the client"
