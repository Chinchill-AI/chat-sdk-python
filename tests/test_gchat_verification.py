"""Tests for Google Chat webhook verification behaviour.

Covers: the constructor fail-closed verification gate (google_chat_project_number,
pubsub_audience, or the disable_signature_verification escape hatch, with env
fallback), rejecting webhooks without auth header, rejecting invalid tokens,
warning when no project number is configured, and allowing webhooks
when verification is unconfigured.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.google_chat.adapter import GoogleChatAdapter
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.shared.errors import ValidationError

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


def _clear_verification_env() -> dict[str, str]:
    """Remove the three gating env vars and return the saved values."""
    keys = (
        "GOOGLE_CHAT_PROJECT_NUMBER",
        "GOOGLE_CHAT_PUBSUB_AUDIENCE",
        "GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION",
    )
    saved = {k: os.environ[k] for k in keys if k in os.environ}
    for k in keys:
        os.environ.pop(k, None)
    return saved


def _config(**overrides: Any) -> GoogleChatAdapterConfig:
    """Build a config with valid auth but no gating field unless overridden."""
    return GoogleChatAdapterConfig(credentials=_make_credentials(), **overrides)


class TestConstructorFailsClosed:
    """The constructor must fail closed when no verifier is configured."""

    def test_raises_when_no_gating_field_set(self):
        saved = _clear_verification_env()
        try:
            with pytest.raises(ValidationError, match="signature verification is required"):
                GoogleChatAdapter(_config())
        finally:
            os.environ.update(saved)

    def test_explicit_disable_false_still_fails_closed(self):
        # An explicit False must be treated as "verification required", NOT as
        # unset -- otherwise the env fallback / fail-closed logic would be wrong.
        saved = _clear_verification_env()
        try:
            with pytest.raises(ValidationError, match="signature verification is required"):
                GoogleChatAdapter(_config(disable_signature_verification=False))
        finally:
            os.environ.update(saved)


class TestConstructorEachGatingFieldSatisfiesIndividually:
    """Any one of the three gating fields must allow construction."""

    def test_google_chat_project_number_satisfies(self):
        saved = _clear_verification_env()
        try:
            adapter = GoogleChatAdapter(_config(google_chat_project_number="123456789"))
            assert adapter.name == "gchat"
            assert adapter._google_chat_project_number == "123456789"
        finally:
            os.environ.update(saved)

    def test_pubsub_audience_satisfies(self):
        saved = _clear_verification_env()
        try:
            adapter = GoogleChatAdapter(_config(pubsub_audience="https://example.com/webhook"))
            assert adapter.name == "gchat"
            assert adapter._pubsub_audience == "https://example.com/webhook"
        finally:
            os.environ.update(saved)

    def test_disable_signature_verification_satisfies(self):
        saved = _clear_verification_env()
        try:
            adapter = GoogleChatAdapter(_config(disable_signature_verification=True))
            assert adapter.name == "gchat"
            assert adapter._disable_signature_verification is True
        finally:
            os.environ.update(saved)


class TestEscapeHatchEmitsWarning:
    """The dev-only escape hatch must construct AND log a warning."""

    def test_escape_hatch_logs_warning(self):
        saved = _clear_verification_env()
        try:
            logger = MagicMock()
            logger.child = MagicMock(return_value=logger)
            adapter = GoogleChatAdapter(_config(disable_signature_verification=True, logger=logger))
            assert adapter._disable_signature_verification is True
            warn_messages = [str(call) for call in logger.warn.call_args_list]
            assert any("disabled" in m.lower() for m in warn_messages), (
                f"Expected a dev-only warning when the escape hatch is used, got: {warn_messages}"
            )
        finally:
            os.environ.update(saved)

    def test_no_warning_when_real_verifier_configured(self):
        # The warning is specific to the escape hatch; a real verifier must not
        # trigger it even if disable_signature_verification is also set.
        saved = _clear_verification_env()
        try:
            logger = MagicMock()
            logger.child = MagicMock(return_value=logger)
            GoogleChatAdapter(_config(google_chat_project_number="123456789", logger=logger))
            warn_messages = [str(call) for call in logger.warn.call_args_list]
            assert not any("disabled" in m.lower() for m in warn_messages), (
                f"Did not expect an escape-hatch warning with a real verifier, got: {warn_messages}"
            )
        finally:
            os.environ.update(saved)


class TestDisableSignatureVerificationEnvFallback:
    """The GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION env var must gate."""

    def test_env_true_satisfies_construction(self):
        saved = _clear_verification_env()
        try:
            os.environ["GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION"] = "true"
            adapter = GoogleChatAdapter(_config())
            assert adapter._disable_signature_verification is True
        finally:
            os.environ.update(saved)

    def test_env_non_true_value_does_not_satisfy(self):
        # Only the literal "true" enables the opt-out; anything else fails closed.
        saved = _clear_verification_env()
        try:
            os.environ["GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION"] = "false"
            with pytest.raises(ValidationError, match="signature verification is required"):
                GoogleChatAdapter(_config())
        finally:
            os.environ.update(saved)

    def test_explicit_config_false_overrides_env_true(self):
        # An explicit config value wins over the env var, so a config False must
        # fail closed even when the env var says "true".
        saved = _clear_verification_env()
        try:
            os.environ["GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION"] = "true"
            with pytest.raises(ValidationError, match="signature verification is required"):
                GoogleChatAdapter(_config(disable_signature_verification=False))
        finally:
            os.environ.update(saved)
