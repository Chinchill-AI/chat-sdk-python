"""Tests for Google Chat webhook verification behaviour.

Covers: the constructor fail-closed verification gate (google_chat_project_number,
endpoint_url, pubsub_audience, or the disable_signature_verification escape
hatch, with env fallback), rejecting webhooks without auth header, rejecting
invalid tokens, warning when no project number is configured, allowing
webhooks when verification is unconfigured, and the identity binding of each
transport (upstream 270b1c25 / 7a192235 / c3b5a08e, issue #222): endpoint-URL
OIDC tokens, project-number tokens self-signed by the Chat service account,
Workspace Add-on identities, Pub/Sub push identities, and button-click
endpoint inference.

Verification runs for real against locally generated RSA keys: the adapter's
``_fetch_json`` is replaced with an in-memory key server, so no test touches
the network and every signature/claim check executes unmocked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from jwt.algorithms import RSAAlgorithm

from chat_sdk.adapters.google_chat.adapter import (
    GOOGLE_CHAT_ISSUER_CERTS_URL,
    GOOGLE_OIDC_CERTS_URL,
    GoogleChatAdapter,
)
from chat_sdk.adapters.google_chat.thread_utils import GoogleChatThreadId
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.cards import Actions, Button, Card
from chat_sdk.shared.errors import ValidationError

# Env vars that gate the constructor's fail-closed check. Cleared on a
# per-test basis with the `clear_verification_env` fixture below so suite
# ordering doesn't leak state into construction tests.
_VERIFICATION_ENV_KEYS = (
    "GOOGLE_CHAT_PROJECT_NUMBER",
    "GOOGLE_CHAT_PUBSUB_AUDIENCE",
    "GOOGLE_CHAT_DISABLE_SIGNATURE_VERIFICATION",
)

# Identity env vars. Cleared for EVERY test in this module (autouse) so a
# developer shell that exports them can't turn a "no identity configured"
# rejection test into an accidental pass.
_IDENTITY_ENV_KEYS = (
    "GOOGLE_CHAT_WORKSPACE_ADDON_SERVICE_ACCOUNT_EMAIL",
    "GOOGLE_CHAT_PUBSUB_SERVICE_ACCOUNT_EMAIL",
)


@pytest.fixture(autouse=True)
def _clear_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _IDENTITY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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
        url: str | None = None,
    ) -> None:
        self.body = body.encode("utf-8")
        self.headers = headers or {}
        if url is not None:
            self.url = url

    async def text(self) -> str:
        return self.body.decode("utf-8")


# =============================================================================
# Local key infrastructure (no network)
#
# Three RSA keys stand in for: Google's OIDC signer (endpoint-URL and Pub/Sub
# tokens), the Chat service account's self-signed issuer (project-number
# tokens), and an attacker. The adapter's ``_fetch_json`` is replaced with an
# in-memory server that publishes the first two exactly as Google does: a JWKS
# document for OIDC, and a ``{kid: PEM certificate}`` map for the Chat issuer.
# =============================================================================

_OIDC_KID = "oidc-kid"
_CHAT_KID = "chat-kid"
_OIDC_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_CHAT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_CHAT_SA = "chat@system.gserviceaccount.com"
_ENDPOINT = "https://example.com/webhook"
_PUBSUB_AUDIENCE = "https://example.com/webhook/pubsub"
_PUSH_SA = "pubsub@my-project.iam.gserviceaccount.com"
_ADD_ON_SA = "service-111@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"


def _self_signed_cert_pem(key: rsa.RSAPrivateKey) -> str:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CHAT_SA)])
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _jwks_document() -> dict[str, Any]:
    jwk = RSAAlgorithm.to_jwk(_OIDC_KEY.public_key(), as_dict=True)
    jwk.update({"kid": _OIDC_KID, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


_CHAT_CERTS_DOCUMENT = {_CHAT_KID: _self_signed_cert_pem(_CHAT_KEY)}


class _KeyServer:
    """In-memory stand-in for Google's key endpoints; records every fetch."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.fail_next = False
        self.documents: dict[str, Any] = {
            GOOGLE_OIDC_CERTS_URL: _jwks_document(),
            GOOGLE_CHAT_ISSUER_CERTS_URL: _CHAT_CERTS_DOCUMENT,
        }

    async def fetch(self, url: str) -> Any:
        self.urls.append(url)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated key fetch failure")
        return self.documents[url]


def _install_key_server(adapter: GoogleChatAdapter) -> _KeyServer:
    server = _KeyServer()
    adapter._fetch_json = AsyncMock(side_effect=server.fetch)  # type: ignore[method-assign]
    return server


def _mint(key: rsa.RSAPrivateKey, kid: str, claims: dict[str, Any]) -> str:
    # Signed at the JWS layer so tests can mint claim shapes PyJWT's encoder
    # refuses (e.g. a list-valued `iss`) -- a forger isn't bound by it either.
    return pyjwt.api_jws.encode(json.dumps(claims).encode("utf-8"), key, algorithm="RS256", headers={"kid": kid})


def _oidc_claims(audience: str, **overrides: Any) -> dict[str, Any]:
    """Claims of a Google OIDC ID token (endpoint-URL / Pub/Sub tokens)."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "iat": now,
        "exp": now + 3600,
        "email": _CHAT_SA,
        "email_verified": True,
    }
    claims.update(overrides)
    return claims


def _chat_claims(project_number: str, **overrides: Any) -> dict[str, Any]:
    """Claims of a project-number token self-signed by the Chat issuer."""
    now = int(time.time())
    claims: dict[str, Any] = {"iss": _CHAT_SA, "aud": project_number, "iat": now, "exp": now + 3600}
    claims.update(overrides)
    return claims


def _oidc_token(audience: str, **overrides: Any) -> str:
    return _mint(_OIDC_KEY, _OIDC_KID, _oidc_claims(audience, **overrides))


def _logger_mock() -> MagicMock:
    logger = MagicMock()
    logger.child = MagicMock(return_value=logger)
    return logger


def _warn_messages(logger: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in logger.warn.call_args_list]


async def _verifying_adapter(**config: Any) -> tuple[GoogleChatAdapter, MagicMock, _KeyServer]:
    """Initialized adapter with verification ON and a local key server."""
    config.setdefault("disable_signature_verification", False)
    config.setdefault("logger", _logger_mock())
    adapter = GoogleChatAdapter(GoogleChatAdapterConfig(credentials=_make_credentials(), **config))
    server = _install_key_server(adapter)
    chat = _make_mock_chat()
    await adapter.initialize(chat)
    return adapter, chat, server


def _direct(token: str | None = None, *, url: str = _ENDPOINT) -> FakeRequest:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return FakeRequest(json.dumps(_make_message_event()), headers=headers, url=url)


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
        key_server = _install_key_server(adapter)
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        await adapter.initialize(chat)

        # Right kid, right claims, wrong signing key: the project-number path
        # (Chat issuer X.509 certs) must reject it on the signature.
        token = _mint(_ATTACKER_KEY, _CHAT_KID, _chat_claims("123456789"))
        request = FakeRequest(
            json.dumps(_make_message_event()),
            headers={"Authorization": f"Bearer {token}"},
        )

        result = await adapter.handle_webhook(request)

        assert result["status"] == 401
        chat.process_message.assert_not_called()
        # Project-number tokens are checked against the Chat issuer certs only.
        assert key_server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]


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

    def test_endpoint_url_satisfies(self, clear_verification_env: pytest.MonkeyPatch):
        # Upstream: "should not throw in constructor when only endpointUrl is
        # configured" -- apps whose authentication audience is "HTTP endpoint
        # URL" verify direct webhooks against it.
        adapter = GoogleChatAdapter(_config(endpoint_url="https://example.com/webhook"))
        assert adapter.name == "gchat"
        assert adapter._endpoint_url == "https://example.com/webhook"

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


class TestEndpointUrlOverridesSignatureOptOut:
    """Breaking change (#222, upstream index.ts branch order): a configured
    ``endpoint_url`` is a direct-webhook verifier and wins over
    ``disable_signature_verification``, which used to cover direct webhooks
    whenever no project number was set."""

    @pytest.mark.asyncio
    async def test_endpoint_url_verifies_direct_webhooks_despite_opt_out(self):
        adapter = _make_adapter(endpoint_url=_ENDPOINT, disable_signature_verification=True)
        chat = _make_mock_chat()
        await adapter.initialize(chat)

        result = await adapter.handle_webhook(_direct(None))

        assert result == {"body": "Unauthorized", "status": 401}
        chat.process_message.assert_not_called()

    def test_constructor_warns_that_opt_out_no_longer_covers_direct_webhooks(
        self, clear_verification_env: pytest.MonkeyPatch
    ):
        logger = _logger_mock()
        GoogleChatAdapter(_config(endpoint_url=_ENDPOINT, disable_signature_verification=True, logger=logger))

        assert any(
            m.startswith("disable_signature_verification does not cover direct Google Chat webhooks")
            for m in _warn_messages(logger)
        ), _warn_messages(logger)

    def test_no_precedence_warning_without_the_opt_out(self, clear_verification_env: pytest.MonkeyPatch):
        logger = _logger_mock()
        GoogleChatAdapter(_config(endpoint_url=_ENDPOINT, logger=logger))

        assert not any("does not cover direct" in m for m in _warn_messages(logger))


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
    def test_disable_signature_verification_is_last_positional_field(self):
        # Load-bearing: this test fails if a future change re-inserts the field
        # in the middle of the dataclass, or adds a new field positionally
        # after it. Fields added later (the identity emails, #222) are
        # ``kw_only`` and therefore don't count.
        positional = [f.name for f in dataclasses.fields(GoogleChatAdapterConfig) if not f.kw_only]
        assert positional[-1] == "disable_signature_verification", (
            f"disable_signature_verification must be the LAST positional field of "
            f"GoogleChatAdapterConfig (positional-args back-compat); got order: {positional}"
        )

    def test_identity_fields_are_keyword_only(self):
        fields = {f.name: f for f in dataclasses.fields(GoogleChatAdapterConfig)}
        assert fields["workspace_add_on_service_account_email"].kw_only is True
        assert fields["pubsub_service_account_email"].kw_only is True

    def test_extra_positional_arg_is_rejected_not_absorbed(self):
        # With the identity fields keyword-only, an 11th positional argument
        # has nowhere to go -- it must raise rather than silently become an
        # identity the verifier trusts.
        with pytest.raises(TypeError):
            GoogleChatAdapterConfig(
                None, False, None, None, None, None, None, None, None, None, "pubsub@p.iam.gserviceaccount.com"
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
        # New fields fall back to their defaults rather than absorbing any of
        # the above positional args.
        assert config.disable_signature_verification is None
        assert config.workspace_add_on_service_account_email is None
        assert config.pubsub_service_account_email is None


# =============================================================================
# Tests -- direct webhook identity binding (upstream 270b1c25, #518)
#
# Ports of the upstream `webhook verification` cases added in chat@4.35 and
# chat@4.37. Direct webhooks carry one of two token types depending on the
# Chat app's "Authentication audience": a Google OIDC ID token with
# aud=endpoint URL (checked for the Chat identity), or a JWT self-signed by
# chat@system.gserviceaccount.com with aud=project number.
# =============================================================================


def _pubsub_request(token: str | None = None) -> FakeRequest:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return FakeRequest(json.dumps(_make_pubsub_push()), headers=headers, url=_ENDPOINT)


class TestProjectNumberVerification:
    @pytest.mark.asyncio
    async def test_allows_direct_webhook_with_valid_token_when_project_number_configured(self):
        # Real project-number tokens are self-signed by chat@system and are
        # verifiable only against its X.509 certs -- never Google's OIDC keys.
        adapter, chat, server = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 200
        chat.process_message.assert_called_once()
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_rejects_google_oidc_token_with_project_number_audience(self):
        # Python-specific: a token that is genuinely Google-signed (OIDC key)
        # and names the project number as `aud` is still not a Chat token --
        # the project-number path only trusts the Chat issuer's own keys.
        adapter, chat, server = await _verifying_adapter(google_chat_project_number="123456789")
        token = _oidc_token("123456789")

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_rejects_chat_issuer_token_with_wrong_issuer_claim(self):
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789", iss="https://accounts.google.com"))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "iss",
        [
            pytest.param("chat", id="a substring"),
            pytest.param("", id="the empty string"),
            pytest.param("gserviceaccount.com", id="a domain suffix"),
            pytest.param([_CHAT_SA], id="a list holding the issuer"),
        ],
    )
    async def test_rejects_chat_issuer_token_whose_issuer_only_partially_matches(self, iss: Any):
        # PyJWT 2.10.0 (allowed by `pyjwt>=2.8`) matched `issuer=` as a
        # substring (CVE-2024-53861); the issuer is compared exactly by hand.
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789", iss=iss))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_chat_issuer_token_with_list_audience(self):
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789", aud=["123456789", "999999999"]))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_chat_issuer_token_for_a_different_project(self):
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("999999999"))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_signed_token_whose_payload_is_not_a_claims_object(self):
        # Port of upstream "should reject when verifyIdToken returns no
        # payload": a correctly signed JWS that carries no claims object.
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = pyjwt.api_jws.encode(b'"not-a-claims-object"', _CHAT_KEY, algorithm="RS256", headers={"kid": _CHAT_KID})

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()


class TestEndpointUrlVerification:
    @pytest.mark.asyncio
    async def test_allows_direct_webhook_with_valid_token_when_only_endpoint_url_configured(self):
        adapter, chat, server = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT)))

        assert result["status"] == 200
        chat.process_message.assert_called_once()
        assert server.urls == [GOOGLE_OIDC_CERTS_URL]

    @pytest.mark.asyncio
    async def test_rejects_endpoint_url_token_when_email_is_not_google_chat(self):
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, email="attacker@example.com")))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("email_verified", [False, "true", 1, None])
    async def test_rejects_endpoint_url_token_when_email_is_not_verified(self, email_verified: Any):
        # Upstream checks `email_verified !== true`; Python checks identity
        # (`is True`), so the string "true" and the integer 1 fail as well.
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, email_verified=email_verified)))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("iss", ["https://evil.example", ["https://accounts.google.com"]])
    async def test_rejects_endpoint_url_token_with_non_google_issuer(self, iss: Any):
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, iss=iss)))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_endpoint_url_token_with_list_audience(self):
        # `aud` must equal the endpoint URL exactly (google-auth-library's
        # strict comparison), not merely contain it.
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, aud=[_ENDPOINT, "https://other.example"])))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_bare_accounts_google_com_issuer(self):
        adapter, _, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, iss="accounts.google.com")))

        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_rejects_expired_endpoint_url_token(self):
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)
        past = int(time.time()) - 7200
        token = _oidc_token(_ENDPOINT, iat=past - 3600, exp=past)

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_endpoint_url_token_signed_by_unknown_key(self):
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)
        token = _mint(_ATTACKER_KEY, _OIDC_KID, _oidc_claims(_ENDPOINT))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 401
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_accepts_endpoint_url_token_when_both_verifiers_configured(self):
        adapter, _, server = await _verifying_adapter(google_chat_project_number="123456789", endpoint_url=_ENDPOINT)

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT)))

        assert result["status"] == 200
        # The project-number verifier is not needed when the OIDC path passes.
        assert server.urls == [GOOGLE_OIDC_CERTS_URL]

    @pytest.mark.asyncio
    async def test_falls_back_to_project_number_when_both_configured_and_oidc_check_fails(self):
        adapter, chat, server = await _verifying_adapter(google_chat_project_number="123456789", endpoint_url=_ENDPOINT)
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        result = await adapter.handle_webhook(_direct(token))

        assert result["status"] == 200
        chat.process_message.assert_called_once()
        assert server.urls == [GOOGLE_OIDC_CERTS_URL, GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_does_not_use_request_inferred_endpoint_url_as_verification_audience(self):
        # Defense in depth: even if the inferred routing URL were poisoned, a
        # Google-signed Chat-identity token naming it as `aud` must not verify.
        adapter, chat, server = await _verifying_adapter(google_chat_project_number="123456789")
        adapter._inferred_endpoint_url = "https://attacker.example/webhook"
        token = _oidc_token("https://attacker.example/webhook")

        result = await adapter.handle_webhook(_direct(token, url="https://attacker.example/webhook"))

        assert result["status"] == 401
        chat.process_message.assert_not_called()
        # Only the project-number verifier ran; the OIDC keys were never used.
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]


_DIRECT_TOKEN_PATHS = ("endpoint_url", "project_number")


async def _direct_webhook_with_claims(
    path: str, *, drop: str | None = None, **overrides: Any
) -> tuple[dict[str, Any], MagicMock]:
    """Send a direct webhook whose token is otherwise valid for ``path``."""
    if path == "endpoint_url":
        adapter, chat, _ = await _verifying_adapter(endpoint_url=_ENDPOINT)
        key, kid, claims = _OIDC_KEY, _OIDC_KID, _oidc_claims(_ENDPOINT, **overrides)
    else:
        adapter, chat, _ = await _verifying_adapter(google_chat_project_number="123456789")
        key, kid, claims = _CHAT_KEY, _CHAT_KID, _chat_claims("123456789", **overrides)
    if drop is not None:
        del claims[drop]
    return await adapter.handle_webhook(_direct(_mint(key, kid, claims))), chat


class TestDirectTokenTimeClaims:
    """google-auth-library's time checks, applied on both direct-token paths:
    300 s clock skew on ``iat``/``exp``, ``exp``/``iat``/``iss`` required, and
    ``exp`` less than 24 h ahead (DEFAULT_MAX_TOKEN_LIFETIME_SECS_)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _DIRECT_TOKEN_PATHS)
    @pytest.mark.parametrize(
        ("iat_offset", "exp_offset", "expected_status"),
        [
            pytest.param(200, 3600, 200, id="iat 200s ahead is within skew"),
            pytest.param(400, 3600, 401, id="iat 400s ahead is too early"),
            pytest.param(-3600, -200, 200, id="exp 200s past is within skew"),
            pytest.param(-3600, -400, 401, id="exp 400s past is too late"),
            pytest.param(0, 86400 - 60, 200, id="exp just under 24h ahead"),
            pytest.param(0, 86400 + 60, 401, id="exp over 24h ahead"),
            pytest.param(0, 400 * 86400, 401, id="exp 400 days ahead"),
        ],
    )
    async def test_time_claim_boundaries(self, path: str, iat_offset: int, exp_offset: int, expected_status: int):
        now = int(time.time())
        result, chat = await _direct_webhook_with_claims(path, iat=now + iat_offset, exp=now + exp_offset)

        assert result["status"] == expected_status
        assert chat.process_message.call_count == (1 if expected_status == 200 else 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", _DIRECT_TOKEN_PATHS)
    @pytest.mark.parametrize("claim", ["exp", "iat", "iss"])
    async def test_rejects_token_missing_a_required_claim(self, path: str, claim: str):
        result, chat = await _direct_webhook_with_claims(path, drop=claim)

        assert result["status"] == 401
        chat.process_message.assert_not_called()


class TestWorkspaceAddOnIdentity:
    """Upstream 7a192235 (#787): add-on tokens need the exact configured identity."""

    @staticmethod
    async def _add_on_webhook(token_email: str, configured: str | None = None) -> tuple[dict[str, Any], MagicMock]:
        config: dict[str, Any] = {"endpoint_url": _ENDPOINT}
        if configured is not None:
            config["workspace_add_on_service_account_email"] = configured
        adapter, _, _ = await _verifying_adapter(**config)
        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, email=token_email)))
        return result, adapter._logger  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_allows_add_on_token_matching_configured_service_account(self):
        result, _ = await self._add_on_webhook(_ADD_ON_SA, _ADD_ON_SA)
        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_rejects_add_on_token_from_a_different_project(self):
        result, _ = await self._add_on_webhook("service-999@gcp-sa-gsuiteaddons.iam.gserviceaccount.com", _ADD_ON_SA)
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_add_on_token_when_no_service_account_configured(self):
        result, logger = await self._add_on_webhook(_ADD_ON_SA)
        assert result["status"] == 401
        assert any("no add-on identity is configured" in m for m in _warn_messages(logger))

    @pytest.mark.asyncio
    async def test_still_allows_chat_system_service_account_without_add_on_config(self):
        result, _ = await self._add_on_webhook(_CHAT_SA)
        assert result["status"] == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "email",
        [
            pytest.param(f"{_ADD_ON_SA}.evil.test", id="a suffixed lookalike domain"),
            pytest.param(
                "service-111@evil.test/gcp-sa-gsuiteaddons.iam.gserviceaccount.com", id="a prefixed lookalike domain"
            ),
            pytest.param(_ADD_ON_SA.upper(), id="an uppercase variant"),
            pytest.param(f"{_ADD_ON_SA} ", id="trailing whitespace"),
            # Python-specific: `re.match(... $)` would accept a trailing "\n".
            pytest.param(f"{_ADD_ON_SA}\n", id="a trailing newline"),
        ],
    )
    async def test_rejects_lookalikes_of_the_configured_add_on_identity(self, email: str):
        result, _ = await self._add_on_webhook(email, _ADD_ON_SA)
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_configured_add_on_identity_when_email_verified_is_false(self):
        adapter, chat, _ = await _verifying_adapter(
            endpoint_url=_ENDPOINT, workspace_add_on_service_account_email=_ADD_ON_SA
        )

        result = await adapter.handle_webhook(_direct(_oidc_token(_ENDPOINT, email=_ADD_ON_SA, email_verified=False)))

        assert result["status"] == 401
        chat.process_message.assert_not_called()


# =============================================================================
# Tests -- Pub/Sub push identity binding (upstream c3b5a08e, #797)
# =============================================================================


class TestPubSubIdentity:
    @staticmethod
    async def _pubsub_webhook(
        claims: dict[str, Any], configured: str | None = None
    ) -> tuple[dict[str, Any], GoogleChatAdapter]:
        config: dict[str, Any] = {"pubsub_audience": _PUBSUB_AUDIENCE}
        if configured is not None:
            config["pubsub_service_account_email"] = configured
        adapter, _, _ = await _verifying_adapter(**config)
        now = int(time.time())
        token = _mint(
            _OIDC_KEY,
            _OIDC_KID,
            {"iss": "accounts.google.com", "aud": _PUBSUB_AUDIENCE, "iat": now, "exp": now + 3600, **claims},
        )
        return await adapter.handle_webhook(_pubsub_request(token)), adapter

    @pytest.mark.asyncio
    async def test_allows_pubsub_webhook_with_valid_token_when_pubsub_audience_configured(self):
        result, adapter = await self._pubsub_webhook({"email": _PUSH_SA, "email_verified": True}, _PUSH_SA)
        assert result["status"] == 200
        adapter._fetch_json.assert_awaited_once_with(GOOGLE_OIDC_CERTS_URL)  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_rejects_pubsub_webhook_with_invalid_token(self):
        adapter, _, _ = await _verifying_adapter(
            pubsub_audience=_PUBSUB_AUDIENCE, pubsub_service_account_email=_PUSH_SA
        )
        token = _mint(_ATTACKER_KEY, _OIDC_KID, _oidc_claims(_PUBSUB_AUDIENCE, email=_PUSH_SA))

        result = await adapter.handle_webhook(_pubsub_request(token))

        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_pubsub_token_from_a_different_service_account(self):
        result, _ = await self._pubsub_webhook(
            {"email": "attacker@evil-project.iam.gserviceaccount.com", "email_verified": True}, _PUSH_SA
        )
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_pubsub_token_when_no_service_account_configured(self):
        result, adapter = await self._pubsub_webhook({"email": _PUSH_SA, "email_verified": True})
        assert result["status"] == 401
        assert any("no push identity is configured" in m for m in _warn_messages(adapter._logger))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("email_verified", [False, "true"])
    async def test_rejects_pubsub_token_when_email_verified_is_not_true(self, email_verified: Any):
        result, _ = await self._pubsub_webhook({"email": _PUSH_SA, "email_verified": email_verified}, _PUSH_SA)
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_pubsub_token_with_no_email_claim(self):
        result, _ = await self._pubsub_webhook({"email_verified": True}, _PUSH_SA)
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_rejects_direct_webhook_token_replayed_as_pubsub(self):
        # A genuine Chat endpoint-URL token (email chat@system) is not a push
        # identity: transports are bound to different identities.
        adapter, _, _ = await _verifying_adapter(
            pubsub_audience=_PUBSUB_AUDIENCE, pubsub_service_account_email=_PUSH_SA
        )

        result = await adapter.handle_webhook(_pubsub_request(_oidc_token(_PUBSUB_AUDIENCE)))

        assert result["status"] == 401


class TestIdentityConfigResolution:
    def test_identities_fall_back_to_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GOOGLE_CHAT_WORKSPACE_ADDON_SERVICE_ACCOUNT_EMAIL", _ADD_ON_SA)
        monkeypatch.setenv("GOOGLE_CHAT_PUBSUB_SERVICE_ACCOUNT_EMAIL", _PUSH_SA)
        adapter = _make_adapter(pubsub_audience=_PUBSUB_AUDIENCE)
        assert adapter._workspace_add_on_service_account_email == _ADD_ON_SA
        assert adapter._pubsub_service_account_email == _PUSH_SA

    def test_explicit_config_wins_over_env_even_when_empty(self, monkeypatch: pytest.MonkeyPatch):
        # `??` semantics: an explicit value (even "") is not replaced by env.
        monkeypatch.setenv("GOOGLE_CHAT_WORKSPACE_ADDON_SERVICE_ACCOUNT_EMAIL", _ADD_ON_SA)
        monkeypatch.setenv("GOOGLE_CHAT_PUBSUB_SERVICE_ACCOUNT_EMAIL", _PUSH_SA)
        adapter = _make_adapter(workspace_add_on_service_account_email="", pubsub_service_account_email="")
        assert adapter._workspace_add_on_service_account_email == ""
        assert adapter._pubsub_service_account_email == ""


# =============================================================================
# Tests -- verification key cache (Python-specific)
# =============================================================================


class TestVerificationKeyCache:
    @pytest.mark.asyncio
    async def test_reuses_certs_within_ttl_and_refetches_after_expiry(self):
        adapter, _, server = await _verifying_adapter(google_chat_project_number="123456789")
        clock = [1000.0]
        adapter._clock = lambda: clock[0]
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        assert (await adapter.handle_webhook(_direct(token)))["status"] == 200
        clock[0] += 3599
        assert (await adapter.handle_webhook(_direct(token)))["status"] == 200
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]

        clock[0] += 1  # exactly one hour after the first fetch
        assert (await adapter.handle_webhook(_direct(token)))["status"] == 200
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL, GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_failed_fetch_does_not_poison_the_cache(self):
        adapter, _, server = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        server.fail_next = True
        assert (await adapter.handle_webhook(_direct(token)))["status"] == 401
        assert adapter._chat_issuer_keys is None
        # The next request fetches again and succeeds.
        assert (await adapter.handle_webhook(_direct(token)))["status"] == 200
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL, GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_empty_key_set_is_not_cached(self):
        adapter, _, server = await _verifying_adapter(google_chat_project_number="123456789")
        server.documents[GOOGLE_CHAT_ISSUER_CERTS_URL] = {}
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        assert (await adapter.handle_webhook(_direct(token)))["status"] == 401
        assert adapter._chat_issuer_keys is None

    @pytest.mark.asyncio
    async def test_unknown_kid_does_not_trigger_a_refetch(self):
        # A flood of tokens with random key ids must not amplify into key
        # fetches: within the TTL the cached set is authoritative.
        adapter, _, server = await _verifying_adapter(google_chat_project_number="123456789")
        good = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))
        assert (await adapter.handle_webhook(_direct(good)))["status"] == 200

        unknown = _mint(_CHAT_KEY, "some-other-kid", _chat_claims("123456789"))
        assert (await adapter.handle_webhook(_direct(unknown)))["status"] == 401
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]

    @pytest.mark.asyncio
    async def test_concurrent_webhooks_on_a_cold_cache_fetch_once(self):
        adapter, chat, server = await _verifying_adapter(google_chat_project_number="123456789")

        async def slow_fetch(url: str) -> Any:
            # Yield so every webhook reaches the key lookup before any fetch
            # completes; without the lock each one would fetch.
            await asyncio.sleep(0.01)
            return await server.fetch(url)

        adapter._fetch_json = AsyncMock(side_effect=slow_fetch)  # type: ignore[method-assign]
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        results = await asyncio.gather(*(adapter.handle_webhook(_direct(token)) for _ in range(5)))

        assert [r["status"] for r in results] == [200] * 5
        assert server.urls == [GOOGLE_CHAT_ISSUER_CERTS_URL]
        assert chat.process_message.call_count == 5


# =============================================================================
# Tests -- button-click endpoint inference (upstream 270b1c25, #518)
# =============================================================================


class TestButtonClickEndpointInference:
    @pytest.mark.asyncio
    async def test_infers_button_click_endpoint_url_but_never_exposes_it_as_audience(self):
        adapter = _make_adapter()  # explicit opt-out: verification disabled
        await adapter.initialize(_make_mock_chat())

        result = await adapter.handle_webhook(
            FakeRequest(json.dumps({"chat": {}}), url="https://my-app.vercel.app/api/webhooks/gchat")
        )

        assert result["status"] == 200
        # Explicit config field stays unset; the routing-only field is populated.
        assert adapter._endpoint_url is None
        assert adapter._inferred_endpoint_url == "https://my-app.vercel.app/api/webhooks/gchat"
        assert adapter._button_click_endpoint_url() == "https://my-app.vercel.app/api/webhooks/gchat"

    @pytest.mark.asyncio
    async def test_does_not_overwrite_explicitly_configured_endpoint_url(self):
        adapter, _, _ = await _verifying_adapter(endpoint_url="https://original.example.com/webhook")
        token = _oidc_token("https://original.example.com/webhook")

        result = await adapter.handle_webhook(_direct(token, url="https://other.example.com/webhook"))

        assert result["status"] == 200
        assert adapter._endpoint_url == "https://original.example.com/webhook"
        assert adapter._inferred_endpoint_url is None
        assert adapter._button_click_endpoint_url() == "https://original.example.com/webhook"

    @pytest.mark.asyncio
    async def test_does_not_infer_endpoint_url_from_request_that_fails_verification(self):
        adapter, _, _ = await _verifying_adapter(google_chat_project_number="123456789")

        result = await adapter.handle_webhook(_direct(None, url="https://attacker.example/webhook"))

        assert result["status"] == 401
        assert adapter._inferred_endpoint_url is None

    @pytest.mark.asyncio
    async def test_does_not_infer_endpoint_url_from_pubsub_push(self):
        # Behaviour change (#222): only verified *direct* webhooks feed routing.
        adapter = _make_adapter()
        await adapter.initialize(_make_mock_chat())

        result = await adapter.handle_webhook(_pubsub_request())

        assert result["status"] == 200
        assert adapter._inferred_endpoint_url is None

    @pytest.mark.asyncio
    async def test_infers_from_first_verified_request_only(self):
        adapter, _, _ = await _verifying_adapter(google_chat_project_number="123456789")
        token = _mint(_CHAT_KEY, _CHAT_KID, _chat_claims("123456789"))

        await adapter.handle_webhook(_direct(token, url="https://first.example/webhook"))
        await adapter.handle_webhook(_direct(token, url="https://second.example/webhook"))

        assert adapter._inferred_endpoint_url == "https://first.example/webhook"

    @pytest.mark.asyncio
    async def test_relative_request_url_is_not_inferred(self):
        adapter = _make_adapter()
        await adapter.initialize(_make_mock_chat())

        await adapter.handle_webhook(FakeRequest(json.dumps({"chat": {}}), url="/api/webhooks/gchat"))

        assert adapter._inferred_endpoint_url is None


def _card_with_button() -> Card:
    return Card(children=[Actions([Button(id="approve", label="Approve")])])


async def _post_card(adapter: GoogleChatAdapter, method: str) -> None:
    thread_id = adapter.encode_thread_id(GoogleChatThreadId(space_name="spaces/ABC123"))
    card = _card_with_button()
    if method == "post_message":
        await adapter.post_message(thread_id, card)
    elif method == "post_ephemeral":
        await adapter.post_ephemeral(thread_id, "users/100", card)
    elif method == "edit_message":
        await adapter.edit_message(thread_id, "spaces/ABC123/messages/m1", card)
    else:
        await adapter.post_channel_message("gchat:spaces/ABC123", card)


def _rendered_button_function(api: AsyncMock) -> str:
    bodies = [call.kwargs.get("body") for call in api.await_args_list]
    cards = [body["cardsV2"] for body in bodies if isinstance(body, dict) and "cardsV2" in body]
    assert len(cards) == 1, bodies
    button = cards[0][0]["card"]["sections"][0]["widgets"][0]["buttonList"]["buttons"][0]
    return button["onClick"]["action"]["function"]


_CARD_POSTING_METHODS = ("post_message", "post_ephemeral", "edit_message", "post_channel_message")


class TestButtonClickEndpointReachesRenderedCards:
    """Each card-rendering site routes buttons through
    ``_button_click_endpoint_url()``: the inferred URL is no longer written
    into ``_endpoint_url``, so a site reading ``_endpoint_url`` directly would
    silently lose button routing for inference-only deployments."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", _CARD_POSTING_METHODS)
    async def test_inferred_endpoint_url_is_rendered_into_card_buttons(self, method: str):
        inferred = "https://my-app.example/api/webhooks/gchat"
        adapter = _make_adapter()  # explicit opt-out; no endpoint_url configured
        await adapter.initialize(_make_mock_chat())
        assert (await adapter.handle_webhook(FakeRequest(json.dumps({"chat": {}}), url=inferred)))["status"] == 200
        api = AsyncMock(return_value={"name": "spaces/ABC123/messages/m1"})
        adapter._gchat_api_request = api  # type: ignore[method-assign]

        await _post_card(adapter, method)

        assert _rendered_button_function(api) == inferred

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", _CARD_POSTING_METHODS)
    async def test_configured_endpoint_url_wins_over_inferred_in_card_buttons(self, method: str):
        adapter = _make_adapter(endpoint_url=_ENDPOINT)
        await adapter.initialize(_make_mock_chat())
        adapter._inferred_endpoint_url = "https://inferred.example/webhook"
        api = AsyncMock(return_value={"name": "spaces/ABC123/messages/m1"})
        adapter._gchat_api_request = api  # type: ignore[method-assign]

        await _post_card(adapter, method)

        assert _rendered_button_function(api) == _ENDPOINT
