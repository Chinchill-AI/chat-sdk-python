"""Tests for Google Chat webhook verification behaviour.

Covers: the constructor fail-closed verification gate (google_chat_project_number,
pubsub_audience, or the disable_signature_verification escape hatch, with env
fallback), rejecting webhooks without auth header, rejecting invalid tokens,
warning when no project number is configured, and allowing webhooks
when verification is unconfigured.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.shared.errors import ValidationError

# Env vars that gate the constructor's fail-closed check. Cleared on a
# per-test basis with the `clear_verification_env` fixture below so suite
# ordering doesn't leak state into construction tests.
_VERIFICATION_ENV_KEYS = (
    "GOOGLE_CHAT_PROJECT_NUMBER",
    "GOOGLE_CHAT_PUBSUB_AUDIENCE",
    "GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION",
)


@pytest.fixture
def clear_verification_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove gating env vars for the test, restore on teardown.

    Uses monkeypatch so both was-set and was-absent cases are handled --
    leaks here would silently satisfy the fail-closed gate in unrelated tests.
    """
    for key in _VERIFICATION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# =============================================================================
# Helpers
# =============================================================================


def _make_credentials() -> ServiceAccountCredentials:
    return ServiceAccountCredentials(
        client_email="test@test.iam.gserviceaccount.com",
        private_key="-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        project_id="test-project",
    )


def _make_adapter(**overrides: Any) -> GoogleChatAdapter:
    # The adapter now fails closed at construction unless one of
    # google_chat_project_number, pubsub_audience, or
    # disable_signature_verification is set. Tests that want the unconfigured
    # runtime path default to the explicit opt-out; verification-gated tests
    # pass google_chat_project_number / pubsub_audience to override it.
    overrides.setdefault("disable_signature_verification", True)
    config = GoogleChatAdapterConfig(
        credentials=overrides.pop("credentials", _make_credentials()),
        **overrides,
    )
    return GoogleChatAdapter(config)


def _make_mock_state() -> MagicMock:
    storage: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: storage.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: storage.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: storage.pop(k, None))
    return state


def _make_mock_chat(state: MagicMock | None = None) -> MagicMock:
    if state is None:
        state = _make_mock_state()
    chat = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.process_message = MagicMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    return chat


def _make_message_event(
    *,
    message_text: str = "Hello",
    space_name: str = "spaces/ABC123",
    sender_name: str = "users/100",
) -> dict[str, Any]:
    """Build a minimal Google Chat direct webhook event."""
    return {
        "chat": {
            "messagePayload": {
                "space": {"name": space_name, "type": "ROOM"},
                "message": {
                    "name": f"{space_name}/messages/msg1",
                    "sender": {
                        "name": sender_name,
                        "displayName": "Test User",
                        "type": "HUMAN",
                    },
                    "text": message_text,
                    "createTime": "2024-01-01T00:00:00Z",
                },
            },
        },
    }


class FakeRequest:
    """Minimal request object for webhook testing."""

    def __init__(
        self,
        body: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self.body.decode("utf-8")


# =============================================================================
# Tests -- rejects webhook without auth header
# =============================================================================


class TestRejectsWithoutAuthHeader:
    """When google_chat_project_number is set, webhooks without Authorization are rejected."""

    @pytest.mark.asyncio
    async def test_rejects_webhook_without_auth_header(self):
        adapter = _make_adapter(google_chat_project_number="123456789")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event()
        request = FakeRequest(json.dumps(event), headers={})

        result = await adapter.handle_webhook(request)

        assert result["status"] == 401
        assert "Unauthorized" in result["body"]
        # process_message should NOT have been called
        chat.process_message.assert_not_called()


# =============================================================================
# Tests -- rejects webhook with invalid token
# =============================================================================


class TestRejectsWithInvalidToken:
    """When google_chat_project_number is set, invalid Bearer tokens are rejected."""

    @pytest.mark.asyncio
    async def test_rejects_webhook_with_invalid_token(self):
        adapter = _make_adapter(google_chat_project_number="123456789")
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event()
        request = FakeRequest(
            json.dumps(event),
            headers={"Authorization": "Bearer invalid.jwt.token"},
        )

        # The _verify_bearer_token will attempt JWT verification which will fail
        # on an invalid token -- the adapter should return 401
        result = await adapter.handle_webhook(request)

        assert result["status"] == 401
        chat.process_message.assert_not_called()


# =============================================================================
# Tests -- warns when no project number configured
# =============================================================================


class TestWarnsWhenNoProjectNumber:
    """When no google_chat_project_number is set, a warning is logged on first request."""

    @pytest.mark.asyncio
    async def test_warns_when_no_project_number_configured(self):
        logger = MagicMock()
        logger.info = MagicMock()
        logger.warn = MagicMock()
        logger.debug = MagicMock()
        logger.error = MagicMock()
        logger.child = MagicMock(return_value=logger)

        adapter = _make_adapter(logger=logger)
        # The constructor emits a dev-only warning when the escape hatch is the
        # sole gate; reset the mock so this test asserts only the *runtime*
        # warn path on the first unconfigured webhook.
        logger.warn.reset_mock()
        # Explicitly clear project number
        adapter._google_chat_project_number = None
        adapter._warned_no_webhook_verification = False

        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event()
        request = FakeRequest(json.dumps(event), headers={})

        await adapter.handle_webhook(request)

        # Should have warned about verification being disabled
        warn_messages = [str(call) for call in logger.warn.call_args_list]
        found_warning = any(
            "verification" in str(call).lower() or "project" in str(call).lower() for call in logger.warn.call_args_list
        )
        assert found_warning, f"Expected a warning about disabled verification, but got: {warn_messages}"

        # The flag should now be set so it only warns once
        assert adapter._warned_no_webhook_verification is True


# =============================================================================
# Tests -- allows webhook without verification when unconfigured
# =============================================================================


class TestAllowsWithoutVerificationWhenUnconfigured:
    """When no project number is configured, webhooks are allowed through (just warned)."""

    @pytest.mark.asyncio
    async def test_allows_webhook_without_verification_when_unconfigured(self):
        adapter = _make_adapter()
        # No project number set -- verification is disabled
        adapter._google_chat_project_number = None

        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        event = _make_message_event()
        request = FakeRequest(json.dumps(event), headers={})

        result = await adapter.handle_webhook(request)

        # The webhook should succeed (200) despite no auth header
        assert result["status"] == 200
        # process_message should have been called since the event was valid
        chat.process_message.assert_called_once()


# =============================================================================
# Tests -- constructor fail-closed verification gate
#
# Ports the gchat slice of upstream 9824d33 (PR #441): the constructor refuses
# to start unless webhook signature verification can be performed for at least
# one transport, or the operator explicitly opts out.
# =============================================================================


def _config(**overrides: Any) -> GoogleChatAdapterConfig:
    """Build a config with valid auth but no gating field unless overridden."""
    return GoogleChatAdapterConfig(credentials=_make_credentials(), **overrides)


class TestConstructorFailsClosed:
    """The constructor must fail closed when no verifier is configured."""

    def test_raises_when_no_gating_field_set(self, clear_verification_env: pytest.MonkeyPatch):
        with pytest.raises(ValidationError, match="signature verification is required"):
            GoogleChatAdapter(_config())

    def test_explicit_disable_false_still_fails_closed(self, clear_verification_env: pytest.MonkeyPatch):
        # An explicit False must be treated as "verification required", NOT as
        # unset -- otherwise the env fallback / fail-closed logic would be wrong.
        with pytest.raises(ValidationError, match="signature verification is required"):
            GoogleChatAdapter(_config(disable_signature_verification=False))


class TestConstructorEachGatingFieldSatisfiesIndividually:
    """Any one of the three gating fields must allow construction."""

    def test_google_chat_project_number_satisfies(self, clear_verification_env: pytest.MonkeyPatch):
        adapter = GoogleChatAdapter(_config(google_chat_project_number="123456789"))
        assert adapter.name == "gchat"
        assert adapter._google_chat_project_number == "123456789"

    def test_pubsub_audience_satisfies(self, clear_verification_env: pytest.MonkeyPatch):
        adapter = GoogleChatAdapter(_config(pubsub_audience="https://example.com/webhook"))
        assert adapter.name == "gchat"
        assert adapter._pubsub_audience == "https://example.com/webhook"

    def test_disable_signature_verification_satisfies(self, clear_verification_env: pytest.MonkeyPatch):
        adapter = GoogleChatAdapter(_config(disable_signature_verification=True))
        assert adapter.name == "gchat"
        assert adapter._disable_signature_verification is True


class TestEscapeHatchEmitsWarning:
    """The dev-only escape hatch must construct AND log a warning."""

    def test_escape_hatch_logs_warning(self, clear_verification_env: pytest.MonkeyPatch):
        logger = MagicMock()
        logger.child = MagicMock(return_value=logger)
        adapter = GoogleChatAdapter(_config(disable_signature_verification=True, logger=logger))
        assert adapter._disable_signature_verification is True
        warn_messages = [str(call) for call in logger.warn.call_args_list]
        assert any("disabled" in m.lower() for m in warn_messages), (
            f"Expected a dev-only warning when the escape hatch is used, got: {warn_messages}"
        )

    def test_no_warning_when_real_verifier_configured(self, clear_verification_env: pytest.MonkeyPatch):
        # The warning is specific to the escape hatch; a real verifier must not
        # trigger it even if disable_signature_verification is also set.
        logger = MagicMock()
        logger.child = MagicMock(return_value=logger)
        GoogleChatAdapter(_config(google_chat_project_number="123456789", logger=logger))
        warn_messages = [str(call) for call in logger.warn.call_args_list]
        assert not any("disabled" in m.lower() for m in warn_messages), (
            f"Did not expect an escape-hatch warning with a real verifier, got: {warn_messages}"
        )


class TestDisableSignatureVerificationEnvFallback:
    """The GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION env var must gate.

    Uses the ``clear_verification_env`` fixture which is built on pytest's
    ``monkeypatch``; the previous manual try/finally pattern only restored
    env vars that were SET before the test, leaking any newly-set var to
    later tests and silently satisfying their fail-closed gate.
    """

    def test_env_true_satisfies_construction(self, clear_verification_env: pytest.MonkeyPatch):
        clear_verification_env.setenv("GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION", "true")
        adapter = GoogleChatAdapter(_config())
        assert adapter._disable_signature_verification is True

    def test_env_non_true_value_does_not_satisfy(self, clear_verification_env: pytest.MonkeyPatch):
        # Only the literal "true" enables the opt-out; anything else fails closed.
        clear_verification_env.setenv("GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION", "false")
        with pytest.raises(ValidationError, match="signature verification is required"):
            GoogleChatAdapter(_config())

    def test_explicit_config_false_overrides_env_true(self, clear_verification_env: pytest.MonkeyPatch):
        # An explicit config value wins over the env var, so a config False must
        # fail closed even when the env var says "true".
        clear_verification_env.setenv("GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION", "true")
        with pytest.raises(ValidationError, match="signature verification is required"):
            GoogleChatAdapter(_config(disable_signature_verification=False))

    def test_env_does_not_leak_to_subsequent_construction(self, clear_verification_env: pytest.MonkeyPatch):
        # Load-bearing for the monkeypatch fix: set the env var via monkeypatch,
        # construct successfully, then simulate a fresh test by undoing the env
        # var with the same fixture's API. A subsequent construction with no
        # gating field must STILL raise -- proving the env var didn't leak.
        clear_verification_env.setenv("GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION", "true")
        GoogleChatAdapter(_config())
        clear_verification_env.delenv("GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION", raising=False)
        with pytest.raises(ValidationError, match="signature verification is required"):
            GoogleChatAdapter(_config())


# =============================================================================
# Tests -- per-shape verification gap (Finding 1 / upstream parity)
#
# handle_webhook accepts BOTH the direct webhook shape AND the Pub/Sub push
# shape on a single endpoint. If only one verifier is configured, the OTHER
# shape must be REJECTED (not warned-but-processed) -- otherwise an attacker
# could pick the unconfigured shape to bypass the configured verifier.
# Mirrors upstream adapter-gchat/src/index.ts.
# =============================================================================


def _make_pubsub_push(*, space_name: str = "spaces/ABC123") -> dict[str, Any]:
    """Build a minimal Pub/Sub push envelope.

    The body content doesn't matter -- _handle_pub_sub_message is reached only
    after verification, so a payload that would fail to decode is fine as long
    as we reach the rejection branch first.
    """
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": "eyJmYWtlIjogInBheWxvYWQifQ==",  # base64 of {"fake": "payload"}
            "attributes": {"ce-type": "google.workspace.chat.message.v1.created"},
            "messageId": "1",
            "publishTime": "2024-01-01T00:00:00Z",
        },
    }


class TestPerShapeVerificationRejection:
    """Each webhook shape must be rejected unless ITS verifier (or the explicit
    escape hatch) is configured."""

    @pytest.mark.asyncio
    async def test_pubsub_push_rejected_when_only_project_number_configured(self):
        # Only the direct-webhook verifier is set; a Pub/Sub push must be
        # rejected -- under the previous code this returned 200 with just a
        # warning, allowing an attacker to forge Pub/Sub-shaped payloads.
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=_make_credentials(),
                google_chat_project_number="123456789",
            )
        )
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        request = FakeRequest(json.dumps(_make_pubsub_push()), headers={})
        result = await adapter.handle_webhook(request)

        assert result["status"] == 401
        assert "Unauthorized" in result["body"]
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_direct_webhook_rejected_when_only_pubsub_audience_configured(self):
        # Symmetric to the above: only the Pub/Sub verifier is set; a direct
        # webhook payload must be rejected.
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=_make_credentials(),
                pubsub_audience="https://example.com/webhook",
            )
        )
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        request = FakeRequest(json.dumps(_make_message_event()), headers={})
        result = await adapter.handle_webhook(request)

        assert result["status"] == 401
        assert "Unauthorized" in result["body"]
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_signature_verification_allows_both_shapes(self):
        # The escape hatch is the explicit "accept unverified" mode -- it must
        # let BOTH shapes through (with warnings) so the operator's opt-out
        # actually covers both transports.
        adapter = GoogleChatAdapter(
            GoogleChatAdapterConfig(
                credentials=_make_credentials(),
                disable_signature_verification=True,
            )
        )
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Direct webhook path
        direct_result = await adapter.handle_webhook(FakeRequest(json.dumps(_make_message_event()), headers={}))
        assert direct_result["status"] == 200

        # Pub/Sub path (decoding may fail downstream; we only care that the
        # 401 rejection branch wasn't taken)
        pubsub_result = await adapter.handle_webhook(FakeRequest(json.dumps(_make_pubsub_push()), headers={}))
        assert pubsub_result["status"] != 401


# =============================================================================
# Tests -- GoogleChatAdapterConfig field order (Finding 2)
#
# GoogleChatAdapterConfig is a positional-args dataclass. Inserting a new
# optional field in the MIDDLE silently shifts every later positional arg for
# existing callers (e.g. `Config("creds", "audience", impersonate, logger)`
# would put `impersonate` into `disable_signature_verification`). Pin the new
# field to the END of the field list to keep old positional callers working.
# =============================================================================


class TestDisableSignatureVerificationFieldOrder:
    def test_disable_signature_verification_is_last_field(self):
        # Load-bearing: this test fails if a future change re-inserts the field
        # in the middle of the dataclass.
        field_names = [f.name for f in dataclasses.fields(GoogleChatAdapterConfig)]
        assert field_names[-1] == "disable_signature_verification", (
            f"disable_signature_verification must be the LAST field of "
            f"GoogleChatAdapterConfig (positional-args back-compat); got order: {field_names}"
        )

    def test_old_positional_call_does_not_misalign(self, clear_verification_env: pytest.MonkeyPatch):
        # Simulates a pre-fail-closed-PR caller using the OLD positional order:
        #   (credentials, use_adc, endpoint_url, project_number, impersonate, logger, pubsub_audience, ...)
        # The new field must not steal any of these positions. We assert each
        # value lands in its named field.
        logger_sentinel = MagicMock()
        creds = _make_credentials()
        config = GoogleChatAdapterConfig(
            creds,  # credentials
            False,  # use_application_default_credentials
            "https://example.com/endpoint",  # endpoint_url
            "123456789",  # google_chat_project_number
            "alice@example.com",  # impersonate_user
            logger_sentinel,  # logger
            "https://example.com/audience",  # pubsub_audience
        )
        assert config.credentials is creds
        assert config.use_application_default_credentials is False
        assert config.endpoint_url == "https://example.com/endpoint"
        assert config.google_chat_project_number == "123456789"
        assert config.impersonate_user == "alice@example.com"
        assert config.logger is logger_sentinel
        assert config.pubsub_audience == "https://example.com/audience"
        # New field falls back to its default rather than absorbing any of the
        # above positional args.
        assert config.disable_signature_verification is None
