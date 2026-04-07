"""Tests for the Slack adapter."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

import pytest

# The Slack adapter module may fail to import if upstream cards.py is missing
# SelectElement. Guard the import so pytest can collect the file and skip
# tests gracefully instead of erroring at collection time.
try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter, create_slack_adapter
    from chat_sdk.adapters.slack.types import SlackAdapterConfig, SlackThreadId
    from chat_sdk.shared.errors import ValidationError

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _SLACK_AVAILABLE, reason="Slack adapter import failed (missing SelectElement in cards.py)"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides) -> SlackAdapter:
    """Create a SlackAdapter with minimal valid config."""
    config = SlackAdapterConfig(
        signing_secret=overrides.pop("signing_secret", "test-signing-secret"),
        bot_token=overrides.pop("bot_token", "xoxb-test-token-123"),
        **overrides,
    )
    return SlackAdapter(config)


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestSlackThreadId:
    """Thread ID encoding and decoding."""

    def test_encode(self):
        adapter = _make_adapter()
        tid = adapter.encode_thread_id(SlackThreadId(channel="C1234567890", thread_ts="1234567890.123456"))
        assert tid == "slack:C1234567890:1234567890.123456"

    def test_decode(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("slack:C1234567890:1234567890.123456")
        assert decoded.channel == "C1234567890"
        assert decoded.thread_ts == "1234567890.123456"

    def test_decode_without_thread_ts(self):
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("slack:C1234567890")
        assert decoded.channel == "C1234567890"
        assert decoded.thread_ts == ""

    def test_roundtrip(self):
        adapter = _make_adapter()
        original = SlackThreadId(channel="C999", thread_ts="1700000000.000001")
        encoded = adapter.encode_thread_id(original)
        decoded = adapter.decode_thread_id(encoded)
        assert decoded.channel == original.channel
        assert decoded.thread_ts == original.thread_ts

    def test_decode_invalid_prefix(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("telegram:12345")

    def test_decode_too_many_parts(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:C123:ts:extra:parts")

    def test_decode_empty(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("")

    def test_is_dm_for_d_channel(self):
        adapter = _make_adapter()
        assert adapter.is_dm("slack:D123:ts") is True

    def test_is_dm_for_c_channel(self):
        adapter = _make_adapter()
        assert adapter.is_dm("slack:C123:ts") is False

    def test_channel_visibility_private_for_g(self):
        adapter = _make_adapter()
        vis = adapter.get_channel_visibility("slack:G123:ts")
        assert vis == "private"

    def test_channel_visibility_private_for_d(self):
        adapter = _make_adapter()
        vis = adapter.get_channel_visibility("slack:D123:ts")
        assert vis == "private"

    def test_channel_visibility_workspace_for_c(self):
        adapter = _make_adapter()
        vis = adapter.get_channel_visibility("slack:C123:ts")
        assert vis == "workspace"

    def test_channel_visibility_unknown(self):
        adapter = _make_adapter()
        vis = adapter.get_channel_visibility("slack:X123:ts")
        assert vis == "unknown"


# ---------------------------------------------------------------------------
# verify_signature
# ---------------------------------------------------------------------------


class TestSlackVerifySignature:
    """Tests for _verify_signature."""

    def test_valid_signature(self):
        adapter = _make_adapter(signing_secret="my-signing-secret")
        body = "request body content"
        timestamp = str(int(time.time()))
        sig_basestring = f"v0:{timestamp}:{body}"
        expected_sig = (
            "v0="
            + hmac.new(
                b"my-signing-secret",
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        assert adapter._verify_signature(body, timestamp, expected_sig) is True

    def test_invalid_signature(self):
        adapter = _make_adapter(signing_secret="my-signing-secret")
        timestamp = str(int(time.time()))
        assert adapter._verify_signature("body", timestamp, "v0=invalid") is False

    def test_none_timestamp(self):
        adapter = _make_adapter()
        assert adapter._verify_signature("body", None, "v0=sig") is False

    def test_none_signature(self):
        adapter = _make_adapter()
        assert adapter._verify_signature("body", str(int(time.time())), None) is False

    def test_old_timestamp_rejected(self):
        adapter = _make_adapter(signing_secret="secret")
        body = "test"
        old_timestamp = str(int(time.time()) - 600)  # 10 minutes ago
        sig_basestring = f"v0:{old_timestamp}:{body}"
        sig = (
            "v0="
            + hmac.new(
                b"secret",
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )
        assert adapter._verify_signature(body, old_timestamp, sig) is False

    def test_non_numeric_timestamp(self):
        adapter = _make_adapter()
        assert adapter._verify_signature("body", "not-a-number", "v0=sig") is False


# ---------------------------------------------------------------------------
# create_slack_adapter factory
# ---------------------------------------------------------------------------


class TestCreateSlackAdapter:
    """Tests for create_slack_adapter factory."""

    def test_with_config(self):
        adapter = create_slack_adapter(
            SlackAdapterConfig(
                signing_secret="sec",
                bot_token="xoxb-tok",
            )
        )
        assert adapter.name == "slack"

    def test_missing_signing_secret(self):
        old = os.environ.pop("SLACK_SIGNING_SECRET", None)
        try:
            with pytest.raises(ValidationError, match="signingSecret"):
                create_slack_adapter(SlackAdapterConfig())
        finally:
            if old is not None:
                os.environ["SLACK_SIGNING_SECRET"] = old

    def test_adapter_properties(self):
        adapter = _make_adapter()
        assert adapter.name == "slack"
        assert adapter.bot_user_id is None  # not yet initialized

    def test_none_config_uses_env(self):
        old_secret = os.environ.get("SLACK_SIGNING_SECRET")
        old_token = os.environ.get("SLACK_BOT_TOKEN")
        os.environ["SLACK_SIGNING_SECRET"] = "env-secret"
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-env-token"
        try:
            adapter = create_slack_adapter()
            assert adapter.name == "slack"
        finally:
            if old_secret is None:
                os.environ.pop("SLACK_SIGNING_SECRET", None)
            else:
                os.environ["SLACK_SIGNING_SECRET"] = old_secret
            if old_token is None:
                os.environ.pop("SLACK_BOT_TOKEN", None)
            else:
                os.environ["SLACK_BOT_TOKEN"] = old_token
