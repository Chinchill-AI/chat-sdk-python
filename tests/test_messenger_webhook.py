"""Tests for the Messenger adapter — webhook routing & signature verification.

Mirrors the upstream ``packages/adapter-messenger/src/index.test.ts`` suite
focused on the webhook surface: GET verification challenge, POST event
dispatch, X-Hub-Signature-256 verification (valid / invalid / missing /
replay), payload parsing, and event-type routing (messages, echoes,
postbacks, reactions, delivery / read confirmations, mixed batches).

Pairs with ``tests/test_messenger_api.py`` (Graph API send/stream/error
mapping) — same split used for Telegram and WhatsApp.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from chat_sdk.adapters.messenger.adapter import (
    MessengerAdapter,
    create_messenger_adapter,
)
from chat_sdk.adapters.messenger.types import (
    ENV_APP_SECRET,
    ENV_PAGE_ACCESS_TOKEN,
    ENV_VERIFY_TOKEN,
    MessengerAdapterConfig,
)
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

APP_SECRET = "test-app-secret"
PAGE_TOKEN = "test-page-token"
VERIFY_TOKEN = "test-verify-token"


def _make_adapter(**overrides: Any) -> MessengerAdapter:
    """Create a ``MessengerAdapter`` with minimal valid config."""
    config_kwargs: dict[str, Any] = {
        "app_secret": APP_SECRET,
        "page_access_token": PAGE_TOKEN,
        "verify_token": VERIFY_TOKEN,
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    config_kwargs.update(overrides)
    return MessengerAdapter(MessengerAdapterConfig(**config_kwargs))


def _make_chat() -> MagicMock:
    """Build a ChatInstance-shaped mock with the methods adapters use."""
    chat = MagicMock()
    chat.get_user_name.return_value = "TestBot"
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_reaction = MagicMock()
    return chat


@dataclass
class _FakeRequest:
    """Minimal request-like object accepted by ``handle_webhook``.

    Mirrors the framework-agnostic duck-typed shape used by the WhatsApp /
    Telegram adapters (``url``, ``method``, ``headers``, awaitable ``text``).
    """

    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:  # noqa: D102 — async body getter
        return self._body


def _sign(body: bytes | str, secret: str = APP_SECRET) -> str:
    """Compute the X-Hub-Signature-256 header value for ``body``."""
    body_bytes = body if isinstance(body, bytes) else body.encode("utf-8")
    return "sha256=" + hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


def _sample_event(**overrides: Any) -> dict[str, Any]:
    """Build a representative inbound Messenger messaging event."""
    base: dict[str, Any] = {
        "sender": {"id": "USER_123"},
        "recipient": {"id": "PAGE_456"},
        "timestamp": 1735689600000,
        "message": {"mid": "mid.abc123", "text": "hello"},
    }
    base.update(overrides)
    return base


def _webhook_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": "page",
        "entry": [
            {
                "id": "PAGE_456",
                "time": 1735689600000,
                "messaging": events,
            }
        ],
    }


def _post_request(payload: dict[str, Any], *, sign: bool = True, signature: str | None = None) -> _FakeRequest:
    body = json.dumps(payload)
    headers: dict[str, str] = {"content-type": "application/json"}
    if signature is not None:
        headers["x-hub-signature-256"] = signature
    elif sign:
        headers["x-hub-signature-256"] = _sign(body)
    return _FakeRequest(url="https://example.com/webhook", method="POST", _body=body, headers=headers)


# ---------------------------------------------------------------------------
# Factory and env-fallback behavior (Q1)
# ---------------------------------------------------------------------------


@pytest.fixture
def _clear_messenger_env() -> Any:
    """Save and clear FACEBOOK_* env vars for the duration of a test."""
    saved: dict[str, str | None] = {
        k: os.environ.pop(k, None) for k in (ENV_APP_SECRET, ENV_PAGE_ACCESS_TOKEN, ENV_VERIFY_TOKEN)
    }
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestFactory:
    """``create_messenger_adapter`` env fallbacks and Q1 init failure path."""

    def test_with_explicit_params(self, _clear_messenger_env: Any) -> None:
        adapter = create_messenger_adapter(
            app_secret="s",
            page_access_token="t",
            verify_token="v",
            user_name="mybot",
        )
        assert adapter.name == "messenger"
        assert adapter.user_name == "mybot"

    def test_uses_env_vars_when_omitted(self, _clear_messenger_env: Any) -> None:
        os.environ[ENV_APP_SECRET] = "secret"
        os.environ[ENV_PAGE_ACCESS_TOKEN] = "token"
        os.environ[ENV_VERIFY_TOKEN] = "verify"
        adapter = create_messenger_adapter()
        assert isinstance(adapter, MessengerAdapter)
        assert adapter.name == "messenger"

    def test_missing_app_secret_raises(self, _clear_messenger_env: Any) -> None:
        os.environ[ENV_PAGE_ACCESS_TOKEN] = "t"
        os.environ[ENV_VERIFY_TOKEN] = "v"
        with pytest.raises(ValidationError, match="appSecret"):
            create_messenger_adapter()

    def test_missing_page_access_token_raises(self, _clear_messenger_env: Any) -> None:
        os.environ[ENV_APP_SECRET] = "s"
        os.environ[ENV_VERIFY_TOKEN] = "v"
        with pytest.raises(ValidationError, match="pageAccessToken"):
            create_messenger_adapter()

    def test_missing_verify_token_raises(self, _clear_messenger_env: Any) -> None:
        os.environ[ENV_APP_SECRET] = "s"
        os.environ[ENV_PAGE_ACCESS_TOKEN] = "t"
        with pytest.raises(ValidationError, match="verifyToken"):
            create_messenger_adapter()

    def test_constructor_also_raises_on_missing_credentials(self, _clear_messenger_env: Any) -> None:
        """Q1: constructing the adapter directly with missing creds also fails.

        The constructor goes through the same ``resolved_*`` fallback chain as
        the factory, matching the WhatsApp adapter's behavior and surfacing
        config errors loudly at startup rather than at first webhook call.
        """
        with pytest.raises(ValidationError, match="appSecret"):
            MessengerAdapter(
                MessengerAdapterConfig(
                    page_access_token="t",
                    verify_token="v",
                    logger=ConsoleLogger("error"),
                )
            )

    def test_explicit_empty_user_name_is_respected(self, _clear_messenger_env: Any) -> None:
        """Regression: ``user_name=""`` must be treated as an explicit choice.

        The old ``config.user_name or "bot"`` (paired with
        ``bool(config.user_name)``) silently replaced an explicit empty
        string with the ``"bot"`` default and left ``_has_explicit_user_name``
        ``False`` — so ``initialize()`` would then overwrite it from
        ``chat.get_user_name()`` / ``/me``. Switching both sites to
        ``is not None`` honors the explicit empty string.

        Without the fix this test would observe ``_user_name == "bot"`` and
        ``_has_explicit_user_name is False``.
        """
        adapter = MessengerAdapter(
            MessengerAdapterConfig(
                app_secret=APP_SECRET,
                page_access_token=PAGE_TOKEN,
                verify_token=VERIFY_TOKEN,
                user_name="",
                logger=ConsoleLogger("error"),
            )
        )
        assert adapter._user_name == ""
        assert adapter._has_explicit_user_name is True


# ---------------------------------------------------------------------------
# Thread ID encoding
# ---------------------------------------------------------------------------


class TestThreadId:
    """Encode / decode of Messenger thread IDs."""

    def test_encode(self) -> None:
        from chat_sdk.adapters.messenger.types import MessengerThreadId

        adapter = _make_adapter()
        assert adapter.encode_thread_id(MessengerThreadId(recipient_id="USER_123")) == "messenger:USER_123"

    def test_decode(self) -> None:
        adapter = _make_adapter()
        decoded = adapter.decode_thread_id("messenger:USER_123")
        assert decoded.recipient_id == "USER_123"

    def test_decode_rejects_invalid_prefix(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("invalid")

    def test_decode_rejects_empty_recipient(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("messenger:")

    def test_decode_rejects_extra_colons(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("messenger:foo:bar")

    def test_decode_rejects_empty(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("")

    def test_decode_rejects_wrong_platform(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_thread_id("slack:C123:ts")

    def test_is_dm_always_true(self) -> None:
        adapter = _make_adapter()
        assert adapter.is_dm("messenger:anything") is True

    def test_channel_id_equals_thread_id(self) -> None:
        adapter = _make_adapter()
        assert adapter.channel_id_from_thread_id("messenger:USER_123") == "messenger:USER_123"

    @pytest.mark.asyncio
    async def test_open_dm_returns_encoded(self) -> None:
        adapter = _make_adapter()
        tid = await adapter.open_dm("USER_999")
        assert tid == "messenger:USER_999"


# ---------------------------------------------------------------------------
# GET verification challenge (Q3-adjacent — token equality check)
# ---------------------------------------------------------------------------


class TestGetVerification:
    """Webhook subscription verification (``hub.mode=subscribe``)."""

    @pytest.mark.asyncio
    async def test_valid_verification_request(self) -> None:
        adapter = _make_adapter()
        request = _FakeRequest(
            url=f"https://example.com/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=CHALLENGE_VALUE",
            method="GET",
            _body="",
            headers={},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == "CHALLENGE_VALUE"

    @pytest.mark.asyncio
    async def test_invalid_verify_token_rejected(self) -> None:
        adapter = _make_adapter()
        request = _FakeRequest(
            url="https://example.com/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=CHALLENGE",
            method="GET",
            _body="",
            headers={},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_missing_challenge_yields_empty_body(self) -> None:
        adapter = _make_adapter()
        request = _FakeRequest(
            url=f"https://example.com/webhook?hub.mode=subscribe&hub.verify_token={VERIFY_TOKEN}",
            method="GET",
            _body="",
            headers={},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == ""

    @pytest.mark.asyncio
    async def test_wrong_mode_rejected(self) -> None:
        adapter = _make_adapter()
        request = _FakeRequest(
            url=f"https://example.com/webhook?hub.mode=unsubscribe&hub.verify_token={VERIFY_TOKEN}&hub.challenge=CHALLENGE",
            method="GET",
            _body="",
            headers={},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403


# ---------------------------------------------------------------------------
# Signature verification (Q3)
# ---------------------------------------------------------------------------


class TestVerifySignature:
    """Unit tests for ``_verify_signature``.

    Q3: upstream pins X-Hub-Signature-256 with HMAC-SHA256 over the raw body
    using the App Secret. We mirror that exactly; a swappable verifier (like
    Slack's ``webhook_verifier``) would diverge from Meta's protocol and
    isn't justified for the single-secret Meta integration.
    """

    def test_valid_signature_accepted(self) -> None:
        adapter = _make_adapter(app_secret="my-secret")
        body = b'{"test": true}'
        sig = "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
        assert adapter._verify_signature(body, sig) is True

    def test_uppercase_hex_signature_accepted(self) -> None:
        """An uppercase-hex signature must still verify.

        ``hexdigest()`` is lowercase, but Node's ``Buffer.from(hex)`` is
        case-insensitive, so upstream accepts an uppercase-hex signature.
        We normalize the header hash to lowercase before the constant-time
        compare; without that, this exact-but-uppercased signature would be
        rejected. Pins parity with upstream's behavior.
        """
        adapter = _make_adapter(app_secret="my-secret")
        body = b'{"test": true}'
        sig = "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest().upper()
        assert adapter._verify_signature(body, sig) is True

    def test_invalid_signature_rejected(self) -> None:
        adapter = _make_adapter(app_secret="my-secret")
        assert adapter._verify_signature(b'{"a":1}', "sha256=deadbeef") is False

    def test_missing_signature_rejected(self) -> None:
        adapter = _make_adapter()
        assert adapter._verify_signature(b"body", None) is False

    def test_empty_signature_rejected(self) -> None:
        adapter = _make_adapter()
        assert adapter._verify_signature(b"body", "") is False

    def test_wrong_algo_rejected(self) -> None:
        adapter = _make_adapter()
        assert adapter._verify_signature(b"body", "sha1=abcdef") is False

    def test_missing_hash_after_algo_rejected(self) -> None:
        adapter = _make_adapter()
        assert adapter._verify_signature(b"body", "sha256=") is False

    def test_signature_without_equals_rejected(self) -> None:
        adapter = _make_adapter()
        # No "=" separator at all is malformed.
        assert adapter._verify_signature(b"body", "abc123") is False

    def test_signature_with_wrong_secret_rejected(self) -> None:
        """A signature computed with the wrong key must not validate.

        Equivalent of a "replay with attacker's secret" / forgery attempt —
        even with a valid-looking sha256 hex shape, mismatching HMAC fails.
        """
        adapter = _make_adapter(app_secret="real-secret")
        body = b'{"x":1}'
        forged = "sha256=" + hmac.new(b"other-secret", body, hashlib.sha256).hexdigest()
        assert adapter._verify_signature(body, forged) is False

    def test_replay_with_modified_body_rejected(self) -> None:
        """Capturing a signature and replaying it with a mutated body fails.

        Models the classic "attacker replays old signature against new body"
        scenario — HMAC binds the signature to the exact bytes that were
        signed, so any change to the body invalidates the signature.
        """
        adapter = _make_adapter()
        original_body = json.dumps(_webhook_payload([_sample_event()])).encode("utf-8")
        sig = _sign(original_body)
        # Same signature, body mutated (e.g. attacker injects another event).
        mutated_body = json.dumps(_webhook_payload([_sample_event(), _sample_event()])).encode("utf-8")
        assert adapter._verify_signature(mutated_body, sig) is False

    def test_verifies_raw_bytes_without_encoding_roundtrip(self) -> None:
        """Signature must be checked against the exact wire bytes.

        Meta signs the raw HTTP request body. If the adapter re-encodes the
        body (decode-to-str then encode-back-to-bytes) before HMAC, any byte
        sequence that's not valid UTF-8 gets replaced with U+FFFD and the
        computed HMAC diverges from Meta's — legitimate webhooks would fail
        verification. This regression pins the contract: feed a body whose
        bytes don't round-trip through UTF-8 cleanly, and verification still
        succeeds when the signature was computed over the original bytes.
        """
        adapter = _make_adapter(app_secret="my-secret")
        # Lone continuation byte (0x80) — invalid UTF-8. A decode+re-encode
        # round-trip via "utf-8" with errors="replace" would mutate this to
        # the 3-byte U+FFFD sequence, breaking HMAC parity.
        body = b'{"data":"\x80\xff"}'
        assert body.decode("utf-8", errors="replace").encode("utf-8") != body
        sig = "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
        assert adapter._verify_signature(body, sig) is True


class TestGetRequestBody:
    """Pin that ``_get_request_body`` returns raw bytes.

    The function feeds ``_verify_signature``; switching its return type
    would silently re-introduce the decode/re-encode hazard. These tests
    cover the four frameworks-shaped inputs the helper supports
    (bytes/str ``body`` attribute, bytes/str ``text`` attribute, async or
    sync callables) and pin the bytes contract on each path.
    """

    @pytest.mark.asyncio
    async def test_bytes_body_attribute_returned_unchanged(self) -> None:
        adapter = _make_adapter()
        raw = b'{"x":"\x80\xff"}'

        class Req:
            body = raw

        result = await adapter._get_request_body(Req())
        assert result == raw
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_str_body_attribute_encoded_utf8(self) -> None:
        adapter = _make_adapter()

        class Req:
            body = '{"hello": "world"}'

        result = await adapter._get_request_body(Req())
        assert result == b'{"hello": "world"}'
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_async_callable_body_returns_bytes(self) -> None:
        """Mirrors Starlette/FastAPI: ``await request.body()`` returns bytes."""
        adapter = _make_adapter()
        raw = b'{"async": true}'

        async def body() -> bytes:
            return raw

        class Req:
            pass

        req = Req()
        req.body = body  # type: ignore[attr-defined]
        result = await adapter._get_request_body(req)
        assert result == raw
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_text_attribute_fallback_returns_bytes(self) -> None:
        """aiohttp-style: ``await request.text()`` returns str — encode it."""
        adapter = _make_adapter()

        async def text() -> str:
            return '{"text": "ok"}'

        class Req:
            pass

        req = Req()
        req.text = text  # type: ignore[attr-defined]
        result = await adapter._get_request_body(req)
        assert result == b'{"text": "ok"}'
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_missing_body_returns_empty_bytes(self) -> None:
        adapter = _make_adapter()

        class Req:
            pass

        result = await adapter._get_request_body(Req())
        assert result == b""
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# POST webhook — payload validation & signature gate
# ---------------------------------------------------------------------------


class TestWebhookPostSignatureGate:
    """End-to-end signature gating in ``handle_webhook`` (POST)."""

    @pytest.mark.asyncio
    async def test_missing_signature_header_rejected(self) -> None:
        adapter = _make_adapter()
        request = _post_request(_webhook_payload([_sample_event()]), sign=False)
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_wrong_algo_rejected(self) -> None:
        adapter = _make_adapter()
        request = _post_request(_webhook_payload([_sample_event()]), signature="sha1=abc123")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_missing_hash_rejected(self) -> None:
        adapter = _make_adapter()
        request = _post_request(_webhook_payload([_sample_event()]), signature="sha256=")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_bad_hex_rejected(self) -> None:
        adapter = _make_adapter()
        request = _post_request(_webhook_payload([_sample_event()]), signature="sha256=not-valid-hex")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 403

    @pytest.mark.asyncio
    async def test_valid_signature_returns_event_received(self) -> None:
        adapter = _make_adapter()
        adapter._chat = _make_chat()  # bypass full init
        request = _post_request(_webhook_payload([_sample_event()]))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == "EVENT_RECEIVED"


class TestWebhookPostPayloadValidation:
    """Payload-shape validation after signature passes."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        adapter = _make_adapter()
        body = "not valid json{{{"
        request = _FakeRequest(
            url="https://example.com/webhook",
            method="POST",
            _body=body,
            headers={"content-type": "application/json", "x-hub-signature-256": _sign(body)},
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400

    @pytest.mark.asyncio
    async def test_non_page_object_returns_404(self) -> None:
        adapter = _make_adapter()
        request = _post_request({"object": "user", "entry": []})
        response = await adapter.handle_webhook(request)
        assert response["status"] == 404

    @pytest.mark.asyncio
    async def test_uninitialized_chat_returns_200_and_warns(self) -> None:
        """Match upstream: webhooks that arrive before ``initialize`` ack 200.

        Returning non-200 would cause Meta to retry — and the only thing the
        adapter knows at this point is that it's not ready to dispatch, not
        that the payload is bad. Acking matches upstream.
        """
        adapter = _make_adapter()
        request = _post_request(_webhook_payload([_sample_event()]))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert response["body"] == "EVENT_RECEIVED"


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


class TestWebhookMessageRouting:
    """Routing of inbound message events to ``chat.process_message``."""

    @pytest.mark.asyncio
    async def test_incoming_message_dispatched(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        request = _post_request(_webhook_payload([_sample_event()]))
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200
        assert chat.process_message.call_count == 1
        call_args = chat.process_message.call_args
        # Signature: (adapter, thread_id, message, options)
        assert call_args.args[1] == "messenger:USER_123"

    @pytest.mark.asyncio
    async def test_echo_message_not_dispatched(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = _sample_event(message={"mid": "mid.echo", "text": "bot", "is_echo": True})
        request = _post_request(_webhook_payload([event]))
        await adapter.handle_webhook(request)
        assert chat.process_message.call_count == 0

    @pytest.mark.asyncio
    async def test_echo_message_cached_for_history(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = _sample_event(
            sender={"id": "PAGE_456"},
            recipient={"id": "USER_123"},
            message={"mid": "mid.echo1", "text": "bot reply", "is_echo": True},
        )
        request = _post_request(_webhook_payload([event]))
        await adapter.handle_webhook(request)
        cached = await adapter.fetch_message("messenger:USER_123", "mid.echo1")
        assert cached is not None
        assert cached.text == "bot reply"

    @pytest.mark.asyncio
    async def test_multiple_messages_in_entry(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        payload = _webhook_payload(
            [
                _sample_event(message={"mid": "mid.1", "text": "first"}),
                _sample_event(message={"mid": "mid.2", "text": "second"}),
                _sample_event(message={"mid": "mid.3", "text": "third"}),
            ]
        )
        await adapter.handle_webhook(_post_request(payload))
        assert chat.process_message.call_count == 3

    @pytest.mark.asyncio
    async def test_multiple_entries_in_payload(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "PAGE_456",
                    "time": 1735689600000,
                    "messaging": [_sample_event(message={"mid": "mid.a", "text": "from 1"})],
                },
                {
                    "id": "PAGE_456",
                    "time": 1735689601000,
                    "messaging": [_sample_event(message={"mid": "mid.b", "text": "from 2"})],
                },
            ],
        }
        await adapter.handle_webhook(_post_request(payload))
        assert chat.process_message.call_count == 2

    @pytest.mark.asyncio
    async def test_delivery_confirmation_no_error(self) -> None:
        adapter = _make_adapter()
        adapter._chat = _make_chat()
        event = _sample_event(message=None, delivery={"watermark": 1735689600000, "mids": ["mid.abc"]})
        # The TypedDict allows None for missing keys; remove ``message`` key.
        event.pop("message")
        response = await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_read_confirmation_no_error(self) -> None:
        adapter = _make_adapter()
        adapter._chat = _make_chat()
        event = _sample_event(read={"watermark": 1735689600000})
        event.pop("message")
        response = await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        assert response["status"] == 200

    @pytest.mark.asyncio
    async def test_mixed_event_types(self) -> None:
        """Single payload with message + reaction + delivery + read."""
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        payload = _webhook_payload(
            [
                _sample_event(message={"mid": "mid.msg", "text": "hi"}),
                {
                    "sender": {"id": "USER_123"},
                    "recipient": {"id": "PAGE_456"},
                    "timestamp": 1735689600000,
                    "reaction": {
                        "mid": "mid.msg",
                        "action": "react",
                        "emoji": "❤",
                        "reaction": "love",
                    },
                },
                {
                    "sender": {"id": "USER_123"},
                    "recipient": {"id": "PAGE_456"},
                    "timestamp": 1735689600000,
                    "delivery": {"watermark": 1735689600000, "mids": ["mid.msg"]},
                },
                {
                    "sender": {"id": "USER_123"},
                    "recipient": {"id": "PAGE_456"},
                    "timestamp": 1735689600000,
                    "read": {"watermark": 1735689600000},
                },
            ]
        )
        response = await adapter.handle_webhook(_post_request(payload))
        assert response["status"] == 200
        assert chat.process_message.call_count == 1
        assert chat.process_reaction.call_count == 1


class TestPostbackRouting:
    """Postback events dispatch via ``chat.process_action``."""

    @pytest.mark.asyncio
    async def test_postback_dispatched(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "postback": {"title": "Get Started", "payload": "GET_STARTED"},
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        assert chat.process_action.call_count == 1

    @pytest.mark.asyncio
    async def test_postback_mid_used_when_present(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "postback": {"title": "Menu", "payload": "MENU_1", "mid": "mid.postback1"},
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        action = chat.process_action.call_args.args[0]
        assert action.message_id == "mid.postback1"
        assert action.action_id == "MENU_1"
        # Q2 (PR 1): callback-data passthrough — when no chat: prefix, both
        # action_id and value come from the raw payload.
        assert action.value == "MENU_1"

    @pytest.mark.asyncio
    async def test_postback_falls_back_to_timestamp_id(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689999000,
            "postback": {"title": "Get Started", "payload": "GET_STARTED"},
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        action = chat.process_action.call_args.args[0]
        assert action.message_id == "postback:1735689999000"

    @pytest.mark.asyncio
    async def test_postback_with_chat_prefixed_payload(self) -> None:
        """Postbacks with the ``chat:`` prefix decode into (action, value)."""
        from chat_sdk.adapters.messenger.cards import encode_messenger_callback_data

        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        encoded = encode_messenger_callback_data("approve", "yes")
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "postback": {"title": "Approve", "payload": encoded, "mid": "mid.p"},
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        action = chat.process_action.call_args.args[0]
        assert action.action_id == "approve"
        assert action.value == "yes"


class TestReactionRouting:
    """Reaction events dispatch via ``chat.process_reaction``."""

    @pytest.mark.asyncio
    async def test_react_event(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "reaction": {
                "mid": "m_reacted",
                "action": "react",
                "emoji": "❤",
                "reaction": "love",
            },
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        reaction = chat.process_reaction.call_args.args[0]
        assert reaction.message_id == "m_reacted"
        assert reaction.raw_emoji == "❤"
        assert reaction.added is True

    @pytest.mark.asyncio
    async def test_unreact_event(self) -> None:
        adapter = _make_adapter()
        chat = _make_chat()
        adapter._chat = chat
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "reaction": {
                "mid": "m_reacted",
                "action": "unreact",
                "emoji": "❤",
                "reaction": "love",
            },
        }
        await adapter.handle_webhook(_post_request(_webhook_payload([event])))
        reaction = chat.process_reaction.call_args.args[0]
        assert reaction.added is False


# ---------------------------------------------------------------------------
# Attachment parsing (no network)
# ---------------------------------------------------------------------------


class TestAttachmentParsing:
    """``parse_message`` extracts attachments from inbound events."""

    def test_extracts_image_video_audio_file_fallback(self) -> None:
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.attach",
                "text": "check",
                "attachments": [
                    {"type": "image", "payload": {"url": "https://example.com/img.jpg"}},
                    {"type": "video", "payload": {"url": "https://example.com/vid.mp4"}},
                    {"type": "audio", "payload": {"url": "https://example.com/aud.mp3"}},
                    {"type": "file", "payload": {"url": "https://example.com/doc.pdf"}},
                    {"type": "fallback", "payload": {"url": "https://example.com/fb"}},
                ],
            }
        )
        parsed = adapter.parse_message(event)
        assert len(parsed.attachments) == 5
        assert [a.type for a in parsed.attachments] == ["image", "video", "audio", "file", "file"]

    def test_skips_attachments_without_url(self) -> None:
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.nourl",
                "text": "sticker",
                "attachments": [
                    {"type": "image", "payload": {"sticker_id": 123}},
                    {"type": "image"},
                ],
            }
        )
        parsed = adapter.parse_message(event)
        assert parsed.attachments == []

    def test_location_attachment_mapped_to_file(self) -> None:
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.loc",
                "text": "location",
                "attachments": [
                    {"type": "location", "payload": {"url": "https://maps.example.com/x"}},
                ],
            }
        )
        parsed = adapter.parse_message(event)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "file"

    def test_no_attachments_field(self) -> None:
        adapter = _make_adapter()
        event = _sample_event(message={"mid": "mid.no", "text": "plain"})
        parsed = adapter.parse_message(event)
        assert parsed.attachments == []

    def test_attachment_has_fetch_data_callable(self) -> None:
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.fd",
                "text": "x",
                "attachments": [{"type": "image", "payload": {"url": "https://example.com/img.jpg"}}],
            }
        )
        parsed = adapter.parse_message(event)
        assert parsed.attachments[0].fetch_data is not None
        assert callable(parsed.attachments[0].fetch_data)

    @pytest.mark.asyncio
    async def test_attachment_download_uses_session(self) -> None:
        """``fetch_data`` reads bytes from the shared aiohttp session."""
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.dl",
                "text": "x",
                "attachments": [{"type": "image", "payload": {"url": "https://example.com/img.jpg"}}],
            }
        )
        parsed = adapter.parse_message(event)

        # Patch the shared session with a minimal context-manager mock.
        class _Resp:
            status = 200

            async def read(self) -> bytes:
                return b"image-bytes"

            async def __aenter__(self) -> _Resp:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        # aiohttp's session.get(url) is a sync call returning an
        # async-context-manager request handle. Wire side_effect (instead of
        # assigning a fresh mock to .get) so the call returns _Resp directly.
        def _session_get(url: str, **_kw: object) -> _Resp:
            return _Resp()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.side_effect = _session_get
        adapter._http_session = mock_session

        result = await parsed.attachments[0].fetch_data()
        assert result == b"image-bytes"


# ---------------------------------------------------------------------------
# Attachment rehydration (queue/debounce/burst concurrency)
# ---------------------------------------------------------------------------


class TestRehydrateAttachment:
    """``MessengerAdapter.rehydrate_attachment`` rebuilds dropped closures.

    Ports the WhatsApp adapter's ``rehydrate_attachment`` hook to Messenger
    so that messages bearing attachments survive the queue/debounce/burst
    concurrency paths (Codex P2 finding).  Without the hook, ``fetch_data``
    is ``None`` after dequeue and downstream handlers cannot download bytes.
    """

    def test_extraction_populates_fetch_metadata_with_url(self) -> None:
        """``_extract_attachments`` stores the download URL on ``fetch_metadata``."""
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.meta",
                "text": "x",
                "attachments": [
                    {"type": "image", "payload": {"url": "https://scontent.example.com/img.jpg"}},
                ],
            }
        )
        parsed = adapter.parse_message(event)
        assert parsed.attachments[0].fetch_metadata == {"url": "https://scontent.example.com/img.jpg"}

    @pytest.mark.asyncio
    async def test_rehydrate_rebuilds_fetch_data_after_queue_roundtrip(self) -> None:
        """Simulate the queue/serialize cycle and confirm rehydration restores downloads.

        This is the load-bearing test: it would FAIL if ``rehydrate_attachment``
        is left as the protocol default (no-op) because ``fetch_data`` would
        stay ``None`` and the ``await`` would raise ``TypeError``.
        """
        adapter = _make_adapter()
        event = _sample_event(
            message={
                "mid": "mid.rehy",
                "text": "x",
                "attachments": [
                    {"type": "image", "payload": {"url": "https://scontent.example.com/img.jpg"}},
                ],
            }
        )
        parsed = adapter.parse_message(event)
        original = parsed.attachments[0]

        # Simulate the JSON roundtrip: ``fetch_data`` closure is dropped,
        # ``fetch_metadata`` survives (it serializes as a plain dict).
        original.fetch_data = None

        rehydrated = adapter.rehydrate_attachment(original)
        assert rehydrated.fetch_data is not None
        assert callable(rehydrated.fetch_data)
        # Preserves the metadata so a second roundtrip would still work.
        assert rehydrated.fetch_metadata == {"url": "https://scontent.example.com/img.jpg"}

        # Wire a fake session to capture the URL the rebuilt closure hits.
        captured_urls: list[str] = []

        class _Resp:
            status = 200

            async def read(self) -> bytes:
                return b"rehydrated-bytes"

            async def __aenter__(self) -> _Resp:
                return self

            async def __aexit__(self, *_: object) -> None:
                pass

        def _session_get(url: str, **_kw: object) -> _Resp:
            captured_urls.append(url)
            return _Resp()

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get.side_effect = _session_get
        adapter._http_session = mock_session

        data = await rehydrated.fetch_data()
        assert data == b"rehydrated-bytes"
        assert captured_urls == ["https://scontent.example.com/img.jpg"]

    def test_rehydrate_no_metadata_returns_unchanged(self) -> None:
        """Degraded mode: attachment without ``fetch_metadata`` is returned as-is."""
        adapter = _make_adapter()
        from chat_sdk.types import Attachment

        bare = Attachment(type="image", url="https://example.com/x.jpg")
        # fetch_metadata is None by default — adapter cannot rebuild the closure.
        result = adapter.rehydrate_attachment(bare)
        assert result is bare
        assert result.fetch_data is None

    def test_rehydrate_metadata_missing_url_returns_unchanged(self) -> None:
        """Degraded mode: ``fetch_metadata`` without the ``url`` key is a no-op."""
        adapter = _make_adapter()
        from chat_sdk.types import Attachment

        bare = Attachment(
            type="image",
            url="https://example.com/x.jpg",
            fetch_metadata={"unrelated": "value"},
        )
        result = adapter.rehydrate_attachment(bare)
        assert result is bare
        assert result.fetch_data is None
