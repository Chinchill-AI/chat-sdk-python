"""Port of adapter-telegram/src/index.test.ts -- webhook handling, message processing,
postMessage, editMessage, deleteMessage, reactions, stream, parseMessage, fetchMessages,
and factory tests.

Tests that duplicate the existing ``test_telegram_adapter.py`` are intentionally
omitted; this file covers the *remaining* TypeScript tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TELEGRAM_FILE_LIMIT,
    TelegramAdapter,
    _js_number_str,
    apply_telegram_entities,
    create_telegram_adapter,
)
from chat_sdk.adapters.telegram.cards import encode_telegram_callback_data
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig, TelegramThreadId
from chat_sdk.shared.errors import NetworkError, ValidationError
from chat_sdk.shared.mock_adapter import MockStateAdapter, create_mock_state
from chat_sdk.types import Message, WebhookOptions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(**overrides: Any) -> TelegramAdapter:
    """Create a TelegramAdapter with minimal valid config."""
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        **overrides,
    )
    return TelegramAdapter(config)


def _sample_message(**overrides: Any) -> dict[str, Any]:
    """Build a representative Telegram message."""
    base: dict[str, Any] = {
        "message_id": 11,
        "date": 1735689600,
        "chat": {"id": 123, "type": "private", "first_name": "User"},
        "from": {
            "id": 456,
            "is_bot": False,
            "first_name": "User",
            "username": "user",
        },
        "text": "hello",
    }
    base.update(overrides)
    return base


@dataclass
class _FakeRequest:
    """Minimal request-like object accepted by TelegramAdapter.handle_webhook."""

    url: str
    method: str
    _body: str
    headers: dict[str, str]

    async def text(self) -> str:  # noqa: D102
        return self._body


def _clear_telegram_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("TELEGRAM_"):
            monkeypatch.delenv(key)


def _make_request(body: str, *, secret_token: str | None = None) -> _FakeRequest:
    headers: dict[str, str] = {"content-type": "application/json"}
    if secret_token is not None:
        headers["x-telegram-bot-api-secret-token"] = secret_token
    return _FakeRequest(
        url="https://example.com/webhook",
        method="POST",
        _body=body,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# createTelegramAdapter
# ---------------------------------------------------------------------------


class TestCreateTelegramAdapterExtended:
    """Extended factory tests from the TS suite."""

    def test_throws_when_bot_token_missing(self):
        old = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            with pytest.raises(ValidationError):
                create_telegram_adapter(TelegramAdapterConfig())
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_uses_env_vars(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ["TELEGRAM_BOT_TOKEN"] = "token-from-env"
        try:
            adapter = create_telegram_adapter(TelegramAdapterConfig())
            assert isinstance(adapter, TelegramAdapter)
            assert adapter.name == "telegram"
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_requires_verification_in_webhook_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            create_telegram_adapter(
                TelegramAdapterConfig(allow_unverified_webhooks=False, bot_token="token", mode="webhook")
            )

    def test_allows_explicit_unverified_webhook_mode(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        adapter = create_telegram_adapter(
            TelegramAdapterConfig(allow_unverified_webhooks=True, bot_token="token", mode="webhook")
        )
        assert isinstance(adapter, TelegramAdapter)

    def test_allows_polling_mode_without_webhook_verification(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", raising=False)
        adapter = create_telegram_adapter(TelegramAdapterConfig(bot_token="token", mode="polling"))
        assert isinstance(adapter, TelegramAdapter)


# ---------------------------------------------------------------------------
# Constructor env var resolution
# ---------------------------------------------------------------------------


class TestTelegramConstructorEnvVars:
    """Constructor env var resolution tests from the TS suite."""

    def test_throws_when_bot_token_missing(self):
        old_keys = {}
        for key in list(os.environ):
            if key.startswith("TELEGRAM_"):
                old_keys[key] = os.environ.pop(key)
        try:
            with pytest.raises(ValidationError, match="botToken"):
                TelegramAdapter(TelegramAdapterConfig())
        finally:
            os.environ.update(old_keys)

    def test_resolve_from_env(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ["TELEGRAM_BOT_TOKEN"] = "env-bot-token"
        try:
            adapter = TelegramAdapter()
            assert isinstance(adapter, TelegramAdapter)
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_resolve_user_name_from_env(self):
        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_name = os.environ.get("TELEGRAM_BOT_USERNAME")
        os.environ["TELEGRAM_BOT_TOKEN"] = "env-bot-token"
        os.environ["TELEGRAM_BOT_USERNAME"] = "env_bot_name"
        try:
            adapter = TelegramAdapter()
            assert adapter.user_name == "env_bot_name"
        finally:
            for k, v in [("TELEGRAM_BOT_TOKEN", old_token), ("TELEGRAM_BOT_USERNAME", old_name)]:
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_config_values_override_env(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ["TELEGRAM_BOT_TOKEN"] = "env-token"
        try:
            adapter = TelegramAdapter(
                TelegramAdapterConfig(
                    bot_token="config-token",
                    user_name="config-name",
                )
            )
            assert adapter.user_name == "config-name"
        finally:
            if old is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old

    def test_should_resolve_allow_unverified_webhooks_from_telegram_allow_unverified_webhooks(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_telegram_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-token")
        monkeypatch.setenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", "true")
        adapter = TelegramAdapter(TelegramAdapterConfig(mode="webhook"))
        assert isinstance(adapter, TelegramAdapter)

    def test_should_reject_telegram_allow_unverified_webhooks_false_in_webhook_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _clear_telegram_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-token")
        monkeypatch.setenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", "false")
        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            TelegramAdapter(TelegramAdapterConfig(mode="webhook"))

    # -- Python-specific: ``??`` / exact-"true" resolution edges ------------

    @pytest.mark.parametrize("env_value", ["1", "True", "TRUE", "yes", " true", ""])
    def test_only_the_exact_string_true_opts_out_of_verification(self, monkeypatch: pytest.MonkeyPatch, env_value: str):
        _clear_telegram_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", env_value)
        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            TelegramAdapter(TelegramAdapterConfig(bot_token="token", mode="webhook"))

    def test_explicit_false_config_wins_over_env_opt_out(self, monkeypatch: pytest.MonkeyPatch):
        _clear_telegram_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", "true")
        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            TelegramAdapter(TelegramAdapterConfig(allow_unverified_webhooks=False, bot_token="token", mode="webhook"))

    def test_explicit_empty_secret_does_not_fall_back_to_env_secret(self, monkeypatch: pytest.MonkeyPatch):
        # ``config.secretToken ?? env``: an explicit "" is kept (and is falsy),
        # so it neither verifies requests nor silently picks up the env secret.
        _clear_telegram_env(monkeypatch)
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "env-secret")
        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            TelegramAdapter(TelegramAdapterConfig(bot_token="token", mode="webhook", secret_token=""))


# ---------------------------------------------------------------------------
# Thread ID encode / decode
# ---------------------------------------------------------------------------


class TestTelegramThreadIdExtended:
    """Extended thread ID tests from the TS suite."""

    def test_encode_and_decode(self):
        adapter = _make_adapter()
        assert adapter.encode_thread_id(TelegramThreadId(chat_id="-100123")) == "telegram:-100123"
        assert (
            adapter.encode_thread_id(TelegramThreadId(chat_id="-100123", message_thread_id=42)) == "telegram:-100123:42"
        )
        decoded = adapter.decode_thread_id("telegram:-100123:42")
        assert decoded.chat_id == "-100123"
        assert decoded.message_thread_id == 42


# ---------------------------------------------------------------------------
# handleWebhook
# ---------------------------------------------------------------------------


class TestTelegramWebhook:
    """Webhook handling tests."""

    @pytest.mark.asyncio
    async def test_rejects_invalid_secret_token(self):
        adapter = _make_adapter(secret_token="expected-secret")
        body = json.dumps({"update_id": 1})
        request = _make_request(body, secret_token="wrong-secret")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_returns_400_for_invalid_json(self):
        adapter = _make_adapter(allow_unverified_webhooks=True)
        request = _make_request("{invalid-json")
        response = await adapter.handle_webhook(request)
        assert response["status"] == 400


# ---------------------------------------------------------------------------
# Slash command routing (chat@4.31 9c936f8)
# ---------------------------------------------------------------------------


def _slash_adapter_and_chat() -> tuple[TelegramAdapter, Any]:
    """Wire a ``userName=mybot`` adapter to a mock chat.

    ``Chat.process_slash_command`` / ``process_message`` are *synchronous*
    methods (they spawn fire-and-forget tasks internally), and the adapter
    calls them synchronously, so the mock uses ``MagicMock`` — an
    ``AsyncMock`` would hand the adapter an unawaited coroutine that never
    reflects the real (sync) call.
    """
    from unittest.mock import AsyncMock, MagicMock

    adapter = _make_adapter(user_name="mybot", allow_unverified_webhooks=True)
    chat = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.process_message = MagicMock()
    # handle_webhook claims each update_id before dispatch (vercel/chat#799);
    # the claim itself is async, so the state method must be an AsyncMock.
    chat.get_state.return_value.set_if_not_exists = AsyncMock(return_value=True)
    adapter._chat = chat
    adapter._bot_user_id = "999"
    adapter._webhook_scope = hashlib.sha256(b"999").hexdigest()
    # Private messages also fire a typing chat action (vercel/chat#612); keep
    # it offline.
    adapter.telegram_fetch = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return adapter, chat


class TestTelegramSlashCommandRouting:
    """Bot-command routing ported from the TS index.test.ts blocks."""

    @pytest.mark.asyncio
    async def test_routes_bot_command_messages_to_slash_handlers(self):
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 2,
                "message": _sample_message(
                    text="/ping@mybot hello world",
                    entities=[{"type": "bot_command", "offset": 0, "length": 11}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        assert chat.process_slash_command.call_count == 1
        chat.process_message.assert_not_called()

        event = chat.process_slash_command.call_args.args[0]
        assert event.channel_id == "telegram:123"
        assert event.command == "/ping"
        assert event.text == "hello world"
        assert event.user.full_name == "User"
        assert event.user.user_id == "456"

    @pytest.mark.asyncio
    async def test_routes_bot_command_captions_to_slash_handlers(self):
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 3,
                "message": _sample_message(
                    caption="/ping hello world",
                    text=None,
                    caption_entities=[{"type": "bot_command", "offset": 0, "length": 5}],
                    photo=[
                        {
                            "file_id": "photo-1",
                            "file_unique_id": "photo-unique-1",
                            "height": 100,
                            "width": 100,
                        }
                    ],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        assert chat.process_slash_command.call_count == 1
        chat.process_message.assert_not_called()

        event = chat.process_slash_command.call_args.args[0]
        assert event.command == "/ping"
        assert event.text == "hello world"

    @pytest.mark.asyncio
    async def test_ignores_bot_commands_addressed_to_another_bot(self):
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 3,
                "message": _sample_message(
                    text="/ping@otherbot hello world",
                    entities=[{"type": "bot_command", "offset": 0, "length": 14}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_only_treats_leading_bot_command_entities_as_slash_commands(self):
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 4,
                "message": _sample_message(
                    text="please /ping",
                    entities=[{"type": "bot_command", "offset": 7, "length": 5}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_string_text_takes_text_branch_not_caption(self):
        """``has_text = text is not None``: empty ``text`` still uses the
        text branch, so a caption-side ``bot_command`` entity is ignored and
        the update routes to ``process_message`` (input-sweep regression)."""
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 5,
                "message": _sample_message(
                    text="",
                    caption="/ping hello",
                    caption_entities=[{"type": "bot_command", "offset": 0, "length": 5}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    def test_trailing_text_split_uses_utf16_offsets(self):
        """The command/trailing-text split is computed on UTF-16 code-unit
        offsets, not Python code points.

        Telegram reports ``length`` in UTF-16 code units. The command token
        ``/p😀g`` spans 4 code points but 5 UTF-16 units (the astral emoji is
        a surrogate pair). The trailing text abuts the token with **no
        separating space** (``/p😀ghello``), so the split offset cannot be
        masked by a ``lstrip`` after the fact:

        * UTF-16-aware split at unit ``offset + length == 5`` lands exactly on
          the ``h`` and yields ``"hello"``.
        * A naive Python code-point slice ``text[5:]`` over-advances (the
          emoji counts as one code point, not two) and yields ``"ello"`` —
          the leading ``h`` is silently dropped.

        Because there is no whitespace at the boundary, the two paths diverge
        in the final result, so this test FAILS against a naive
        ``text[entity_length:]`` slice.
        """
        adapter, _ = _slash_adapter_and_chat()
        # "/p😀g" = / p <emoji=2 units> g = 4 code points but 5 UTF-16 units;
        # "hello" follows immediately with no separator.
        result = adapter.parse_slash_command(
            _sample_message(
                text="/p😀ghello",
                entities=[{"type": "bot_command", "offset": 0, "length": 5}],
            )
        )
        assert result == {"command": "/p😀g", "text": "hello"}

    def test_entity_text_split_naive_codepoint_would_diverge(self):
        """Guards the UTF-16 split against a naive ``str`` slice regression.

        ``_slice_utf16`` and a naive code-point slice must diverge for astral
        text, proving the helper is load-bearing (not a no-op on ASCII).
        With the trailing text abutting the token (no separator), the two
        slices return different strings that no ``lstrip`` can reconcile."""
        adapter, _ = _slash_adapter_and_chat()
        text = "/p😀ghello"
        # UTF-16-aware slice at code-unit 5 lands on the "h".
        assert adapter._slice_utf16(text, 5) == "hello"
        # The naive code-point slice over-advances and eats the leading "h".
        assert text[5:] == "ello"

    @pytest.mark.asyncio
    async def test_at_bot_targeting_is_case_insensitive(self):
        """``/ping@<MixedCase>`` still routes to the slash handler when the
        casing differs from ``user_name`` — the ``.lower()`` normalization on
        both sides is load-bearing (mutating it to a case-sensitive ``!=``
        drops this command to ``process_message``)."""
        adapter, chat = _slash_adapter_and_chat()  # user_name == "mybot"
        body = json.dumps(
            {
                "update_id": 7,
                "message": _sample_message(
                    text="/ping@MyBot hello",
                    entities=[{"type": "bot_command", "offset": 0, "length": 11}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        assert chat.process_slash_command.call_count == 1
        chat.process_message.assert_not_called()
        event = chat.process_slash_command.call_args.args[0]
        assert event.command == "/ping"
        assert event.text == "hello"

    @pytest.mark.asyncio
    async def test_edited_message_with_bot_command_does_not_route_to_slash(self):
        """Slash gating is scoped to ``update.message`` only: an
        ``edited_message`` carrying a leading ``bot_command`` entity routes to
        the regular message path, never the slash handler (mutating the gate
        to read ``edited_message`` would mis-route the edit)."""
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 8,
                "edited_message": _sample_message(
                    text="/ping@mybot hello",
                    entities=[{"type": "bot_command", "offset": 0, "length": 11}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_channel_post_with_bot_command_does_not_route_to_slash(self):
        """A ``channel_post`` carrying a leading ``bot_command`` entity routes
        to the regular message path, not the slash handler — slash gating only
        reads ``update.message`` (mirrors upstream ``update.message``)."""
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 9,
                "channel_post": _sample_message(
                    text="/ping@mybot hello",
                    entities=[{"type": "bot_command", "offset": 0, "length": 11}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_command_addressed_only_to_another_bot_routes_to_message(self):
        """``/@bot`` (empty command name) yields no slash command — the
        ``if not command_name: return None`` guard sends it to
        ``process_message`` (matches upstream's ``if (!commandName)``)."""
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 10,
                "message": _sample_message(
                    text="/@mybot hello",
                    entities=[{"type": "bot_command", "offset": 0, "length": 7}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1

    @pytest.mark.asyncio
    async def test_bare_slash_routes_to_message(self):
        """A bare ``/`` (no command name) yields no slash command and routes
        to ``process_message`` — the empty-``command_name`` guard, plus the
        ``startswith('/')`` / ``offset == 0`` gating, all hold."""
        adapter, chat = _slash_adapter_and_chat()
        body = json.dumps(
            {
                "update_id": 11,
                "message": _sample_message(
                    text="/ hello",
                    entities=[{"type": "bot_command", "offset": 0, "length": 1}],
                ),
            }
        )

        response = await adapter.handle_webhook(_make_request(body))
        assert response["status"] == 200

        chat.process_slash_command.assert_not_called()
        assert chat.process_message.call_count == 1


# ---------------------------------------------------------------------------
# isDM
# ---------------------------------------------------------------------------


class TestTelegramIsDM:
    """isDM tests from the TS suite."""

    def test_private_chat_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:456") is True

    def test_group_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:-100123") is False

    def test_group_with_topic_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:-100123:42") is False


# ---------------------------------------------------------------------------
# parseMessage -- attachments
# ---------------------------------------------------------------------------


class TestTelegramParseMessageAttachments:
    """Attachment extraction from Telegram messages."""

    def test_photo_attachment(self):
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            photo=[
                {"file_id": "photo1", "file_unique_id": "u1", "width": 100, "height": 100},
                {"file_id": "photo2", "file_unique_id": "u2", "width": 800, "height": 600},
            ],
            caption="Nice photo",
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "image"
        assert parsed.attachments[0].width == 800
        assert parsed.attachments[0].height == 600
        assert parsed.text == "Nice photo"

    def test_document_attachment(self):
        adapter = _make_adapter()
        msg = _sample_message(
            document={
                "file_id": "doc1",
                "file_unique_id": "u1",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
            }
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "file"
        assert parsed.attachments[0].name == "report.pdf"
        assert parsed.attachments[0].mime_type == "application/pdf"

    def test_audio_attachment(self):
        adapter = _make_adapter()
        msg = _sample_message(
            audio={
                "file_id": "audio1",
                "file_unique_id": "ua1",
                "duration": 120,
                "file_name": "track.mp3",
                "mime_type": "audio/mpeg",
                "file_size": 2048000,
            }
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "audio"
        assert parsed.attachments[0].name == "track.mp3"
        assert parsed.attachments[0].mime_type == "audio/mpeg"

    def test_video_attachment(self):
        adapter = _make_adapter()
        msg = _sample_message(
            video={
                "file_id": "vid1",
                "file_unique_id": "uv1",
                "width": 1920,
                "height": 1080,
                "duration": 60,
                "file_name": "clip.mp4",
                "mime_type": "video/mp4",
                "file_size": 10485760,
            }
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "video"
        assert parsed.attachments[0].width == 1920
        assert parsed.attachments[0].height == 1080
        assert parsed.attachments[0].mime_type == "video/mp4"

    def test_video_note_attachment(self):
        # Port of vercel/chat#457: round video messages (video_note) extract
        # as a "video" attachment with width/height set to the clip's length.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            video_note={
                "file_id": "vn1",
                "file_unique_id": "uvn1",
                "length": 240,
                "duration": 10,
                "file_size": 512000,
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.type == "video"
        assert attachment.width == 240
        assert attachment.height == 240
        assert attachment.size == 512000

    def test_video_note_attachment_stores_file_id(self):
        # video_note must round-trip its file_id into fetch_metadata so the
        # lazy download closure can be rebuilt after serialization.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            video_note={"file_id": "vn2", "file_unique_id": "uvn2", "length": 120},
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].fetch_metadata == {"fileId": "vn2", "fileUniqueId": "uvn2"}

    def test_video_note_attachment_without_optional_fields(self):
        # Edge case not covered upstream: video_note with no length and no
        # file_size must still extract a video attachment without raising,
        # leaving width/height/size as None.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            video_note={"file_id": "vn3", "file_unique_id": "uvn3"},
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.type == "video"
        assert attachment.width is None
        assert attachment.height is None
        assert attachment.size is None

    def test_video_note_attachment_zero_length(self):
        # Edge case not covered upstream: a zero length must propagate as
        # width/height == 0 (not be dropped by a truthiness check).
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            video_note={"file_id": "vn4", "file_unique_id": "uvn4", "length": 0},
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.width == 0
        assert attachment.height == 0


# ---------------------------------------------------------------------------
# Inbound rich-message parsing (Bot API 10.1 -- chat@4.31 4662309)
# ---------------------------------------------------------------------------


class TestTelegramParseInboundRichMessage:
    """parseTelegramMessage handling of inbound ``rich_message`` payloads."""

    def test_normalizes_inbound_rich_message_text_and_ast(self):
        # Port of "normalizes inbound rich messages": a rich_message with no
        # text/caption renders its plain text (for `.text`) and its markdown AST
        # (for `.formatted`).
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {"type": "heading", "size": 2, "text": "Release"},
                    {
                        "type": "table",
                        "cells": [
                            [
                                {"align": "left", "is_header": True, "text": "Package", "valign": "top"},
                                {"align": "left", "is_header": True, "text": "Status", "valign": "top"},
                            ],
                            [
                                {"align": "left", "text": "chat", "valign": "top"},
                                {"align": "left", "text": "ready", "valign": "top"},
                            ],
                        ],
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert "Release" in parsed.text
        assert "chat" in parsed.text
        assert "ready" in parsed.text
        child_types = [node.get("type") for node in parsed.formatted["children"]]
        assert "heading" in child_types
        assert "table" in child_types

    def test_rich_message_text_falls_back_when_no_text_or_caption(self):
        # plainText chains `??`: with text/caption absent (nullish), the rich
        # message's plain-text rendering supplies `.text`. A mutation that
        # swapped the rich fallback for "" would leave text empty.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={"blocks": [{"type": "paragraph", "text": "rich body"}]},
        )
        parsed = adapter.parse_message(msg)
        assert parsed.text == "rich body"

    def test_plain_text_caption_wins_over_rich_when_present(self):
        # `?? raw.caption ?? (rich ? richMessageToText : "")` -- a present caption
        # short-circuits the rich fallback. Guards against reordering the chain.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            caption="caption wins",
            rich_message={"blocks": [{"type": "paragraph", "text": "rich body"}]},
        )
        parsed = adapter.parse_message(msg)
        assert parsed.text == "caption wins"

    def test_rich_markdown_drives_formatted_ast(self):
        # `formatted: ... toAst(richMarkdown || text)` -- when a rich message is
        # present its rendered markdown (not the bare plain text) seeds the AST.
        # A bold paragraph must surface a `strong` inline node.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [{"type": "paragraph", "text": [{"type": "bold", "text": "loud"}]}],
            },
        )
        parsed = adapter.parse_message(msg)
        paragraph = parsed.formatted["children"][0]
        assert paragraph["type"] == "paragraph"
        inline_types = [child.get("type") for child in paragraph["children"]]
        assert "strong" in inline_types

    def test_message_without_rich_message_is_unaffected(self):
        # Regression guard: a plain text message keeps deriving `.formatted`
        # from its own text (richMarkdown is "", so `richMarkdown || text` == text).
        adapter = _make_adapter()
        parsed = adapter.parse_message(_sample_message(text="just text"))
        assert parsed.text == "just text"
        assert parsed.formatted["children"][0]["type"] == "paragraph"

    def test_empty_entities_list_does_not_fall_through_to_caption_entities(self):
        # TG3 follow-up: `raw.entities ?? raw.caption_entities ?? []` is a
        # NULLISH ladder. A present-but-EMPTY `entities: []` is a real value
        # and short-circuits the chain, so the populated `caption_entities`
        # are NOT applied. With the correct nullish ladder the bold entity
        # never fires and `.text` stays the bare plain text.
        #
        # A truthy-`or` mutation (`entities or caption_entities`) would treat
        # the empty list as falsy, reach for `caption_entities`, and wrap the
        # span in `**...**` — so this assertion FAILS on that mutation.
        adapter = _make_adapter()
        msg = _sample_message(
            text="bold body",
            entities=[],
            caption_entities=[{"type": "bold", "offset": 0, "length": 9}],
        )
        parsed = adapter.parse_message(msg)
        assert parsed.text == "bold body"
        # Sanity anchor: the SAME entity, supplied via `entities`, DOES apply
        # — proving the bold entity is load-bearing and the test above is not
        # passing for an unrelated reason.
        msg_applied = _sample_message(
            text="bold body",
            entities=[{"type": "bold", "offset": 0, "length": 9}],
        )
        assert adapter.parse_message(msg_applied).text == "**bold body**"


class TestTelegramParseInboundRichMedia:
    """extractAttachments handling of media nested in ``rich_message`` blocks."""

    def test_normalizes_nested_rich_media_as_attachments(self):
        # Port of "normalizes nested rich media as attachments": media nested in
        # a collage is recursed into; the photo picks its LARGEST size and the
        # video carries name/mime/dimensions.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {
                        "type": "collage",
                        "blocks": [
                            {
                                "type": "photo",
                                "photo": [
                                    {"file_id": "small", "file_unique_id": "small-unique", "height": 100, "width": 100},
                                    {
                                        "file_id": "large",
                                        "file_unique_id": "large-unique",
                                        "file_size": 2048,
                                        "height": 800,
                                        "width": 1200,
                                    },
                                ],
                            },
                            {
                                "type": "video",
                                "video": {
                                    "file_id": "video",
                                    "file_unique_id": "video-unique",
                                    "duration": 10,
                                    "file_name": "clip.mp4",
                                    "file_size": 4096,
                                    "height": 720,
                                    "mime_type": "video/mp4",
                                    "width": 1280,
                                },
                            },
                        ],
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 2

        image = parsed.attachments[0]
        assert image.type == "image"
        assert image.fetch_metadata == {"fileId": "large", "fileUniqueId": "large-unique"}  # LARGEST size, not "small"
        assert image.mime_type == "image/jpeg"
        assert image.size == 2048
        assert image.width == 1200
        assert image.height == 800

        video = parsed.attachments[1]
        assert video.type == "video"
        assert video.fetch_metadata == {"fileId": "video", "fileUniqueId": "video-unique"}
        assert video.size == 4096
        assert video.width == 1280
        assert video.height == 720
        assert video.name == "clip.mp4"
        assert video.mime_type == "video/mp4"

    def test_rich_animation_classified_as_image_by_mime(self):
        # `animation` with an image/* mime maps to type "image" (a GIF rendered
        # as a still). A mutation defaulting to "video" would fail here.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {
                        "type": "animation",
                        "animation": {
                            "file_id": "anim-img",
                            "file_unique_id": "anim-img-u",
                            "duration": 3,
                            "file_name": "loop.gif",
                            "height": 240,
                            "mime_type": "image/gif",
                            "width": 320,
                        },
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.type == "image"
        assert attachment.fetch_metadata == {"fileId": "anim-img", "fileUniqueId": "anim-img-u"}
        assert attachment.mime_type == "image/gif"
        assert attachment.name == "loop.gif"
        assert attachment.width == 320
        assert attachment.height == 240

    def test_rich_animation_classified_as_video_by_mime(self):
        # The same `animation` block with a video/* mime maps to type "video".
        # This pins the mime-driven branch (truthiness of `startswith("image/")`).
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {
                        "type": "animation",
                        "animation": {
                            "file_id": "anim-vid",
                            "file_unique_id": "anim-vid-u",
                            "duration": 5,
                            "mime_type": "video/mp4",
                            "height": 480,
                            "width": 640,
                        },
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "video"
        assert parsed.attachments[0].mime_type == "video/mp4"

    def test_rich_voice_note_classified_as_audio(self):
        # `voice_note` maps to type "audio" (no width/height/name fields).
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {
                        "type": "voice_note",
                        "voice_note": {
                            "file_id": "voice",
                            "file_unique_id": "voice-u",
                            "duration": 7,
                            "file_size": 9001,
                            "mime_type": "audio/ogg",
                        },
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.type == "audio"
        assert attachment.fetch_metadata == {"fileId": "voice", "fileUniqueId": "voice-u"}
        assert attachment.size == 9001
        assert attachment.mime_type == "audio/ogg"
        assert attachment.width is None
        assert attachment.height is None
        assert attachment.name is None

    def test_rich_media_recurses_through_list_blocks(self):
        # `media()` recurses into list item blocks; a photo nested inside a list
        # entry must still be extracted (guards against dropping list recursion).
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            rich_message={
                "blocks": [
                    {
                        "type": "list",
                        "style": "bullet",
                        "items": [
                            {
                                "label": "-",
                                "blocks": [
                                    {
                                        "type": "photo",
                                        "photo": [
                                            {
                                                "file_id": "listed-photo",
                                                "file_unique_id": "listed-u",
                                                "height": 50,
                                                "width": 60,
                                            },
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert len(parsed.attachments) == 1
        assert parsed.attachments[0].type == "image"
        assert parsed.attachments[0].fetch_metadata == {"fileId": "listed-photo", "fileUniqueId": "listed-u"}

    def test_rich_media_appends_after_top_level_attachments(self):
        # Top-level media (a document) and rich-block media coexist: the rich
        # media is appended *after* the document, preserving upstream ordering.
        adapter = _make_adapter()
        msg = _sample_message(
            text=None,
            document={
                "file_id": "doc1",
                "file_unique_id": "udoc1",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
            },
            rich_message={
                "blocks": [
                    {
                        "type": "photo",
                        "photo": [{"file_id": "rich-photo", "file_unique_id": "rp-u", "height": 10, "width": 10}],
                    },
                ],
            },
        )
        parsed = adapter.parse_message(msg)
        assert [a.type for a in parsed.attachments] == ["file", "image"]
        assert parsed.attachments[0].fetch_metadata == {"fileId": "doc1", "fileUniqueId": "udoc1"}
        assert parsed.attachments[1].fetch_metadata == {"fileId": "rich-photo", "fileUniqueId": "rp-u"}


# ---------------------------------------------------------------------------
# applyTelegramEntities (complementary to existing tests)
# ---------------------------------------------------------------------------


class TestApplyTelegramEntitiesExtended:
    """Additional entity application tests from the TS suite."""

    def test_text_link(self):
        result = apply_telegram_entities(
            "Visit our website for details",
            [{"type": "text_link", "offset": 10, "length": 7, "url": "https://example.com"}],
        )
        assert result == "Visit our [website](https://example.com) for details"

    def test_bold(self):
        result = apply_telegram_entities(
            "hello world",
            [{"type": "bold", "offset": 6, "length": 5}],
        )
        assert result == "hello **world**"

    def test_italic(self):
        result = apply_telegram_entities(
            "hello world",
            [{"type": "italic", "offset": 0, "length": 5}],
        )
        assert result == "*hello* world"

    def test_code(self):
        result = apply_telegram_entities(
            "use the console.log function",
            [{"type": "code", "offset": 8, "length": 11}],
        )
        assert result == "use the `console.log` function"

    def test_pre(self):
        result = apply_telegram_entities(
            "const x = 1",
            [{"type": "pre", "offset": 0, "length": 11}],
        )
        assert result == "```\nconst x = 1\n```"

    def test_pre_with_language(self):
        result = apply_telegram_entities(
            "const x = 1",
            [{"type": "pre", "offset": 0, "length": 11, "language": "typescript"}],
        )
        assert result == "```typescript\nconst x = 1\n```"

    def test_strikethrough(self):
        result = apply_telegram_entities(
            "old text here",
            [{"type": "strikethrough", "offset": 0, "length": 8}],
        )
        assert result == "~~old text~~ here"

    def test_url_unchanged(self):
        result = apply_telegram_entities(
            "check https://example.com out",
            [{"type": "url", "offset": 6, "length": 19}],
        )
        assert result == "check https://example.com out"

    def test_mention_unchanged(self):
        result = apply_telegram_entities(
            "hey @user check this",
            [{"type": "mention", "offset": 4, "length": 5}],
        )
        assert result == "hey @user check this"

    def test_multiple_non_overlapping(self):
        result = apply_telegram_entities(
            "hello world foo",
            [
                {"type": "bold", "offset": 0, "length": 5},
                {"type": "italic", "offset": 6, "length": 5},
            ],
        )
        assert result == "**hello** *world* foo"

    def test_text_link_with_special_chars(self):
        result = apply_telegram_entities(
            "click [here]",
            [{"type": "text_link", "offset": 6, "length": 6, "url": "https://example.com"}],
        )
        assert result == "click [\\[here\\]](https://example.com)"


# ---------------------------------------------------------------------------
# Webhook verification by default + update_id deduplication
# (vercel/chat#799, #813, #858)
# ---------------------------------------------------------------------------

_BOT_ME = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "mybot"}
_SCOPE_999 = hashlib.sha256(b"999").hexdigest()


def _spy_state() -> MockStateAdapter:
    """In-memory state whose ``set_if_not_exists`` records calls."""
    state = create_mock_state()
    state.set_if_not_exists = AsyncMock(side_effect=state.set_if_not_exists)  # type: ignore[method-assign]
    return state


def _mock_chat(state: MockStateAdapter) -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="mybot")
    return chat


def _dedupe_adapter(*, get_me: Any = None, **overrides: Any) -> TelegramAdapter:
    config: dict[str, Any] = {
        "bot_token": "token",
        "mode": "webhook",
        "secret_token": "secret",
        "user_name": "mybot",
    }
    config.update(overrides)
    adapter = TelegramAdapter(TelegramAdapterConfig(**config))
    adapter.telegram_fetch = get_me if get_me is not None else AsyncMock(return_value=dict(_BOT_ME))  # type: ignore[method-assign]
    return adapter


def _update_request(update: dict[str, Any], *, secret_token: str | None = "secret") -> _FakeRequest:
    return _make_request(json.dumps(update), secret_token=secret_token)


class TestTelegramWebhookUpdateDeduplication:
    """Ports of the upstream webhook verification / dedupe cases."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Adapters here resolve secret/opt-out from TELEGRAM_* when a config
        # value is None; keep a developer's exported vars out of the result.
        _clear_telegram_env(monkeypatch)

    @pytest.mark.asyncio
    async def test_deduplicates_sequential_and_concurrent_webhook_updates(self):
        state = _spy_state()
        adapters = [_dedupe_adapter(), _dedupe_adapter()]
        chats = [_mock_chat(state), _mock_chat(state)]
        await asyncio.gather(*(a.initialize(c) for a, c in zip(adapters, chats, strict=True)))

        def dispatch_count() -> int:
            return sum(c.process_message.call_count for c in chats)

        first = await adapters[0].handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}))
        duplicate = await adapters[1].handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}))
        assert first["status"] == 200
        assert duplicate["status"] == 200
        assert dispatch_count() == 1

        concurrent = await asyncio.gather(
            *(a.handle_webhook(_update_request({"update_id": 2, "message": _sample_message()})) for a in adapters)
        )
        assert [r["status"] for r in concurrent] == [200, 200]
        assert dispatch_count() == 2

    @pytest.mark.asyncio
    async def test_dispatches_distinct_and_missing_webhook_update_ids(self):
        state = _spy_state()
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        updates = [
            {"update_id": 1, "message": _sample_message()},
            {"update_id": 2, "message": _sample_message()},
            {"message": _sample_message()},
        ]
        await asyncio.gather(*(adapter.handle_webhook(_update_request(u)) for u in updates))

        assert chat.process_message.call_count == 3
        assert state.set_if_not_exists.await_count == 2
        state.set_if_not_exists.assert_any_await(f"telegram:webhook-update:{_SCOPE_999}:1", True, 86_400_000)

    @pytest.mark.asyncio
    async def test_deduplicates_explicitly_allowed_unverified_updates(self):
        state = _spy_state()
        chat = _mock_chat(state)
        adapter = _dedupe_adapter(secret_token=None, allow_unverified_webhooks=True)
        await adapter.initialize(chat)

        await adapter.handle_webhook(_update_request({"update_id": 1}, secret_token=None))
        await adapter.handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}, secret_token=None))

        chat.process_message.assert_not_called()
        assert state.set_if_not_exists.await_count == 2

    @pytest.mark.asyncio
    async def test_scopes_webhook_update_claims_by_bot_identity(self):
        state = _spy_state()
        adapters = [
            _dedupe_adapter(bot_token="token-a", get_me=AsyncMock(return_value={**_BOT_ME, "id": 100})),
            _dedupe_adapter(bot_token="token-b", get_me=AsyncMock(return_value={**_BOT_ME, "id": 200})),
        ]
        chats = [_mock_chat(state), _mock_chat(state)]
        await asyncio.gather(*(a.initialize(c) for a, c in zip(adapters, chats, strict=True)))

        await asyncio.gather(
            *(a.handle_webhook(_update_request({"update_id": 1, "message": _sample_message()})) for a in adapters)
        )

        assert sum(c.process_message.call_count for c in chats) == 2
        keys = {call.args[0] for call in state.set_if_not_exists.await_args_list}
        assert keys == {
            f"telegram:webhook-update:{hashlib.sha256(b'100').hexdigest()}:1",
            f"telegram:webhook-update:{hashlib.sha256(b'200').hexdigest()}:1",
        }

    @pytest.mark.asyncio
    async def test_returns_503_without_dispatch_when_the_deduplication_state_fails(self):
        state = create_mock_state()
        state.set_if_not_exists = AsyncMock(side_effect=RuntimeError("state unavailable"))  # type: ignore[method-assign]
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}))

        assert response["status"] == 503
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_unverified_callback_queries_before_dispatch(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        adapter = _dedupe_adapter(allow_unverified_webhooks=False, mode="auto", secret_token=None)
        chat = _mock_chat(_spy_state())
        adapter._chat = chat

        response = await adapter.handle_webhook(
            _update_request(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "callback-1",
                        "from": {"id": 456, "is_bot": False, "first_name": "User", "username": "user"},
                        "message": _sample_message(),
                        "chat_instance": "ci_1",
                        "data": encode_telegram_callback_data("eve_input", "request-123"),
                    },
                },
                secret_token=None,
            )
        )

        assert response["status"] == 401
        chat.process_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_mode_requires_verification_when_a_webhook_is_registered(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        responses = {
            "getMe": dict(_BOT_ME),
            "getWebhookInfo": {
                "allowed_updates": [],
                "has_custom_certificate": False,
                "pending_update_count": 0,
                "url": "https://example.com/webhook/telegram",
            },
        }
        get_me = AsyncMock(side_effect=lambda method, *args, **kwargs: responses[method])
        adapter = _dedupe_adapter(get_me=get_me, allow_unverified_webhooks=False, mode="auto", secret_token=None)

        with pytest.raises(ValidationError, match="secret_token is required in webhook mode"):
            await adapter.initialize(_mock_chat(_spy_state()))
        assert [c.args[0] for c in get_me.await_args_list] == ["getMe", "getWebhookInfo"]
        assert adapter.is_polling is False

    # -- describe("bot token resolver"): scope assertions, static tokens ----

    @pytest.mark.asyncio
    async def test_scopes_webhook_deduplication_with_the_stable_bot_identity(self):
        state = _spy_state()
        adapter = _dedupe_adapter()
        await adapter.initialize(_mock_chat(state))

        await adapter.handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}))

        state.set_if_not_exists.assert_awaited_once_with(f"telegram:webhook-update:{_SCOPE_999}:1", True, 86_400_000)

    @pytest.mark.asyncio
    async def test_keeps_the_webhook_scope_stable_across_token_rotation_and_instances(self):
        state = _spy_state()
        adapters = [_dedupe_adapter(bot_token=token) for token in ("rotated-token-a", "rotated-token-b")]
        chats = [_mock_chat(state) for _ in adapters]
        await asyncio.gather(*(a.initialize(c) for a, c in zip(adapters, chats, strict=True)))

        await asyncio.gather(*(a.handle_webhook(_update_request({"update_id": 1})) for a in adapters))

        keys = [call.args[0] for call in state.set_if_not_exists.await_args_list]
        assert len(keys) == 2
        assert set(keys) == {f"telegram:webhook-update:{_SCOPE_999}:1"}

    @pytest.mark.asyncio
    async def test_retries_bot_identity_resolution_on_a_later_webhook(self):
        get_me = AsyncMock(side_effect=[NetworkError("telegram", "temporary getMe failure"), dict(_BOT_ME)])
        state = _spy_state()
        adapter = _dedupe_adapter(get_me=get_me)
        await adapter.initialize(_mock_chat(state))
        assert adapter.bot_user_id is None

        assert (await adapter.handle_webhook(_update_request({"update_id": 1})))["status"] == 200
        assert (await adapter.handle_webhook(_update_request({"update_id": 2})))["status"] == 200

        state.set_if_not_exists.assert_any_await(f"telegram:webhook-update:{_SCOPE_999}:1", True, 86_400_000)
        assert get_me.await_count == 2
        assert adapter.bot_user_id == "999"


class TestTelegramWebhookDeduplicationPythonEdges:
    """Python-specific edges of the upstream dedupe port."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Adapters here resolve secret/opt-out from TELEGRAM_* when a config
        # value is None; keep a developer's exported vars out of the result.
        _clear_telegram_env(monkeypatch)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("update_id", [True, False, "1", 1.5, None])
    async def test_non_integer_update_ids_are_dispatched_without_a_claim(self, update_id: Any):
        # ``Number.isInteger`` parity: ``bool`` is an ``int`` subclass in Python
        # but must not be claimed; strings/fractional floats/null skip the claim too.
        state = _spy_state()
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_update_request({"update_id": update_id, "message": _sample_message()}))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        state.set_if_not_exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_integral_float_update_ids_share_the_integer_claim_key(self):
        # ``Number.isInteger(7.0)`` is true in JS and ``${7.0}`` renders "7", so
        # upstream dedupes ``7`` / ``7.0`` / ``7e0`` as one update. ``json.loads``
        # keeps the latter two as floats; they must normalise to the same key.
        state = _spy_state()
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)
        message = json.dumps(_sample_message())

        statuses = [
            (
                await adapter.handle_webhook(
                    _make_request(f'{{"update_id": {raw}, "message": {message}}}', secret_token="secret")
                )
            )["status"]
            for raw in ("7", "7.0", "7e0")
        ]

        assert statuses == [200, 200, 200]
        assert chat.process_message.call_count == 1
        key = f"telegram:webhook-update:{_SCOPE_999}:7"
        assert [c.args[0] for c in state.set_if_not_exists.await_args_list] == [key, key, key]

    @pytest.mark.asyncio
    async def test_authenticated_non_object_body_is_acknowledged_without_raising(self):
        # A verified body that parses to a JSON array has no ``update_id`` and
        # makes ``process_update`` fail; the failure log must not itself raise
        # (``list.get``) — upstream reads ``update.update_id`` as undefined.
        state = _spy_state()
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_make_request("[1]", secret_token="secret"))

        assert response["status"] == 200
        chat.process_message.assert_not_called()
        state.set_if_not_exists.assert_not_awaited()

    @pytest.mark.parametrize("value", ["false", "true", 0, 1])
    def test_non_bool_allow_unverified_webhooks_is_rejected(self, value: Any):
        # ``bool("false")`` is True: coercing would silently disable
        # verification, so a non-bool opt-out fails loudly instead.
        with pytest.raises(ValidationError, match="allow_unverified_webhooks must be a bool"):
            TelegramAdapter(
                TelegramAdapterConfig(bot_token="t", mode="webhook", secret_token=None, allow_unverified_webhooks=value)
            )

    @pytest.mark.asyncio
    async def test_returns_503_without_dispatch_when_bot_identity_cannot_be_resolved(self):
        get_me = AsyncMock(side_effect=NetworkError("telegram", "getMe unavailable"))
        state = _spy_state()
        adapter = _dedupe_adapter(get_me=get_me)
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_update_request({"update_id": 1, "message": _sample_message()}))

        assert response["status"] == 503
        chat.process_message.assert_not_called()
        state.set_if_not_exists.assert_not_awaited()
        # initialize + the webhook each attempted getMe: a failure is not cached.
        assert get_me.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_identity_waiters_share_one_get_me_call(self):
        release = asyncio.Event()

        async def slow_get_me(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            await release.wait()
            return dict(_BOT_ME)

        get_me = AsyncMock(side_effect=slow_get_me)
        adapter = _dedupe_adapter(get_me=get_me)
        waiters = [asyncio.get_running_loop().create_task(adapter._ensure_bot_identity()) for _ in range(3)]
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(*waiters)

        assert get_me.await_count == 1
        assert adapter._webhook_scope == _SCOPE_999
        assert adapter._bot_identity_task is None

    @pytest.mark.asyncio
    async def test_cancelling_one_identity_waiter_leaves_the_other_resolved(self):
        release = asyncio.Event()

        async def slow_get_me(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
            await release.wait()
            return dict(_BOT_ME)

        get_me = AsyncMock(side_effect=slow_get_me)
        adapter = _dedupe_adapter(get_me=get_me)
        cancelled = asyncio.get_running_loop().create_task(adapter._ensure_bot_identity())
        survivor = asyncio.get_running_loop().create_task(adapter._ensure_bot_identity())
        await asyncio.sleep(0)

        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        release.set()
        await survivor

        assert get_me.await_count == 1
        assert adapter.bot_user_id == "999"
        assert adapter._webhook_scope == _SCOPE_999

    @pytest.mark.asyncio
    async def test_reinitialize_keeps_the_get_me_username_over_the_chat_name(self):
        # The cached identity skips ``getMe`` on a second ``initialize``
        # (``Chat.shutdown`` + ``Chat.initialize``); the Telegram username must
        # still win over ``Chat.user_name`` so ``/ping@real_bot`` keeps routing.
        get_me = AsyncMock(return_value={**_BOT_ME, "username": "real_bot"})
        adapter = _dedupe_adapter(get_me=get_me, user_name=None)
        chat = _mock_chat(_spy_state())
        chat.get_user_name = MagicMock(return_value="chat_level_name")

        await adapter.initialize(chat)
        await adapter.initialize(chat)

        assert get_me.await_count == 1
        assert adapter.user_name == "real_bot"
        await adapter.handle_webhook(
            _update_request(
                {
                    "update_id": 1,
                    "message": _sample_message(
                        text="/ping@real_bot hi",
                        entities=[{"type": "bot_command", "offset": 0, "length": 14}],
                    ),
                }
            )
        )
        chat.process_slash_command.assert_called_once()
        assert chat.process_slash_command.call_args.args[0].command == "/ping"

    def test_positional_config_arguments_keep_their_pre_opt_out_binding(self):
        # ``allow_unverified_webhooks`` is appended last so existing positional
        # callers (api_base_url, bot_token, logger, long_polling, mode, ...)
        # are not shifted by the new field.
        config = TelegramAdapterConfig(None, "token", None, None, "polling", "secret", "named_bot")

        assert config.api_base_url is None
        assert config.bot_token == "token"
        assert config.mode == "polling"
        assert config.secret_token == "secret"
        assert config.user_name == "named_bot"
        assert config.allow_unverified_webhooks is None

    @pytest.mark.asyncio
    async def test_polling_mode_initialize_does_not_require_verification(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", raising=False)
        adapter = _dedupe_adapter(mode="polling", secret_token=None)
        adapter.start_polling = AsyncMock()  # type: ignore[method-assign]

        await adapter.initialize(_mock_chat(_spy_state()))

        assert adapter.runtime_mode == "polling"
        adapter.start_polling.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4.41 inbound (#225): allowlist, early typing, mention regex, media identity,
# stickers/animations, non-file content, download cap
# ---------------------------------------------------------------------------


def _message_without_text(**overrides: Any) -> dict[str, Any]:
    """``sampleMessage({ text: undefined, ...overrides })``: no ``text`` key."""
    message = _sample_message(**overrides)
    if "text" not in overrides:
        del message["text"]
    return message


async def _dispatch(message: dict[str, Any]) -> Any:
    """Deliver ``message`` through ``handle_webhook``; return the parsed Message."""
    adapter, chat = _slash_adapter_and_chat()
    response = await adapter.handle_webhook(_make_request(json.dumps({"update_id": 1, "message": message})))
    assert response["status"] == 200
    assert chat.process_message.call_count == 1
    return chat.process_message.call_args.args[2]


class TestTelegramAllowedUserIds:
    """Ports of the upstream allowlist cases (vercel/chat#742)."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _clear_telegram_env(monkeypatch)

    def test_should_resolve_allowed_user_ids_from_telegram_allowed_user_ids_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-token")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123, 456")
        adapter = TelegramAdapter()
        assert adapter._allowed_user_ids == {"123", "456"}

    def test_should_allow_all_users_when_telegram_allowed_user_ids_is_empty(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-token")
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", " , ")
        adapter = TelegramAdapter()
        assert adapter._allowed_user_ids is None

    def test_explicit_config_list_wins_over_env_and_normalizes_ids(self, monkeypatch: pytest.MonkeyPatch):
        # ``allowedUserIds ?? env``: an explicit list (even empty) is not
        # nullish, so the env var is ignored; ints and floats stringify like
        # JS ``String()`` (``456.0`` -> ``"456"``).
        monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "999")
        assert _make_adapter(allowed_user_ids=[]).__dict__["_allowed_user_ids"] is None
        adapter = _make_adapter(allowed_user_ids=[123, 456.0, " 789 ", ""])
        assert adapter._allowed_user_ids == {"123", "456", "789"}

    @pytest.mark.asyncio
    async def test_rejects_disallowed_and_identityless_updates_before_dispatch(self):
        adapter = _make_adapter(user_name="mybot", allow_unverified_webhooks=True, allowed_user_ids=[456])
        chat = MagicMock()
        adapter._chat = chat
        adapter.telegram_fetch = AsyncMock(return_value=True)  # type: ignore[method-assign]

        disallowed_user = {"id": 789, "is_bot": False, "first_name": "Other User"}
        group_message = _sample_message(chat={"id": -100123, "type": "supergroup", "title": "General"})
        channel_post = dict(group_message)
        del channel_post["from"]
        updates: list[dict[str, Any]] = [
            {"update_id": 1, "message": {**group_message, "from": disallowed_user}},
            {
                "update_id": 2,
                "callback_query": {
                    "id": "callback-1",
                    "from": disallowed_user,
                    "message": group_message,
                    "chat_instance": "ci_1",
                    "data": "approve",
                },
            },
            {
                "update_id": 3,
                "message_reaction": {
                    "chat": group_message["chat"],
                    "message_id": group_message["message_id"],
                    "date": group_message["date"],
                    "old_reaction": [],
                    "new_reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
                    "user": disallowed_user,
                },
            },
            {"update_id": 4, "channel_post": channel_post},
        ]
        for update in updates:
            adapter.process_update(update)  # type: ignore[arg-type]
        adapter.process_update({"update_id": 5, "message": group_message})  # type: ignore[typeddict-item]

        assert chat.process_message.call_count == 1
        chat.process_slash_command.assert_not_called()
        chat.process_action.assert_not_called()
        chat.process_reaction.assert_not_called()

    def test_allowed_callback_and_reaction_users_are_dispatched(self):
        # The acting user is ``callback_query.from`` / ``message_reaction.user``
        # for those updates, so an allowlisted clicker or reactor still routes.
        adapter = _make_adapter(allowed_user_ids=["456"])
        chat = MagicMock()
        adapter._chat = chat
        allowed_user = {"id": 456, "is_bot": False, "first_name": "User"}
        group_message = _sample_message(chat={"id": -100123, "type": "supergroup", "title": "General"})
        adapter.process_update(
            {  # type: ignore[typeddict-item]
                "update_id": 1,
                "callback_query": {
                    "id": "cb",
                    "from": allowed_user,
                    "message": group_message,
                    "chat_instance": "ci",
                    "data": "approve",
                },
            }
        )
        adapter.process_update(
            {  # type: ignore[typeddict-item]
                "update_id": 2,
                "message_reaction": {
                    "chat": group_message["chat"],
                    "message_id": 11,
                    "date": 1,
                    "old_reaction": [],
                    "new_reaction": [{"type": "emoji", "emoji": "\U0001f44d"}],
                    "user": allowed_user,
                },
            }
        )
        assert chat.process_action.call_count == 1
        assert chat.process_reaction.call_count == 1


def _record_process(events: list[str], label: str) -> Any:
    """A ``Chat.process_*`` stand-in that, like the real one, schedules its work
    as a task and returns synchronously."""

    async def _run() -> None:
        events.append(label)

    def _process(*_args: Any, **_kwargs: Any) -> asyncio.Task[None]:
        return asyncio.get_running_loop().create_task(_run())

    return MagicMock(side_effect=_process)


class TestTelegramTypingOnReceipt:
    """Ports of vercel/chat#612: private messages start typing on receipt."""

    @staticmethod
    def _typing_fetch(events: list[str]) -> AsyncMock:
        async def _fetch(method: str, _payload: Any = None, **_kwargs: Any) -> Any:
            assert method == "sendChatAction"
            events.append("typing")
            return True

        return AsyncMock(side_effect=_fetch)

    @pytest.mark.asyncio
    async def test_starts_typing_before_processing_private_message_updates(self):
        events: list[str] = []
        adapter, chat = _slash_adapter_and_chat()
        adapter.telegram_fetch = self._typing_fetch(events)  # type: ignore[method-assign]
        chat.process_message = _record_process(events, "processMessage")
        wait_until = MagicMock()

        response = await adapter.handle_webhook(
            _make_request(json.dumps({"update_id": 1, "message": _sample_message(text="hello")})),
            WebhookOptions(wait_until=wait_until),
        )
        assert response["status"] == 200
        await asyncio.gather(*(call.args[0] for call in wait_until.call_args_list))
        await asyncio.sleep(0)

        assert events == ["typing", "processMessage"]
        assert wait_until.call_count == 1
        adapter.telegram_fetch.assert_awaited_once_with(
            "sendChatAction", {"chat_id": "123", "message_thread_id": None, "action": "typing"}
        )

    @pytest.mark.asyncio
    async def test_starts_typing_before_processing_private_slash_command_updates(self):
        events: list[str] = []
        adapter, chat = _slash_adapter_and_chat()
        adapter.telegram_fetch = self._typing_fetch(events)  # type: ignore[method-assign]
        chat.process_slash_command = _record_process(events, "processSlashCommand")
        wait_until = MagicMock()

        response = await adapter.handle_webhook(
            _make_request(
                json.dumps(
                    {
                        "update_id": 2,
                        "message": _sample_message(
                            text="/ping@mybot hello world",
                            entities=[{"type": "bot_command", "offset": 0, "length": 11}],
                        ),
                    }
                )
            ),
            WebhookOptions(wait_until=wait_until),
        )
        assert response["status"] == 200
        await asyncio.gather(*(call.args[0] for call in wait_until.call_args_list))
        await asyncio.sleep(0)

        assert events == ["typing", "processSlashCommand"]
        assert wait_until.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"chat": {"id": -100123, "type": "supergroup", "title": "General"}},
            {"from": {"id": 777, "is_bot": True, "first_name": "Other Bot"}},
        ],
        ids=["group-chat", "bot-sender"],
    )
    async def test_does_not_start_typing_for_group_chats_or_bot_senders(self, overrides: dict[str, Any]):
        adapter, chat = _slash_adapter_and_chat()
        wait_until = MagicMock()
        adapter.process_update(
            {"update_id": 1, "message": _sample_message(**overrides)},  # type: ignore[typeddict-item]
            WebhookOptions(wait_until=wait_until),
        )
        await asyncio.sleep(0)

        assert chat.process_message.call_count == 1
        wait_until.assert_not_called()
        adapter.telegram_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failing_typing_action_is_logged_and_does_not_block_dispatch(self):
        adapter, chat = _slash_adapter_and_chat()
        adapter.telegram_fetch = AsyncMock(side_effect=NetworkError("telegram", "boom"))  # type: ignore[method-assign]
        logger = MagicMock()
        adapter._logger = logger

        adapter.process_update({"update_id": 1, "message": _sample_message()})  # type: ignore[typeddict-item]
        assert chat.process_message.call_count == 1
        # No ``wait_until``: the adapter holds the task itself until it settles.
        (typing_task,) = adapter._typing_tasks
        await typing_task

        logger.warn.assert_called_once_with(
            "Failed to send Telegram typing action",
            {"error": "boom", "threadId": "telegram:123"},
        )
        assert adapter._typing_tasks == set()


class TestTelegramMentionRegex:
    """Ports of vercel/chat#621 / #706 (mention boundary + caching)."""

    @pytest.mark.asyncio
    async def test_does_not_mark_a_mention_when_a_hyphen_suffixed_name_is_mentioned(self):
        parsed = await _dispatch(
            _sample_message(
                chat={"id": -100123, "type": "supergroup", "title": "General"},
                text="see @mybot-dev for details",
            )
        )
        assert parsed.is_mention is False

    def test_matches_with_the_cached_regex_and_recompiles_when_the_username_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import re

        adapter = _make_adapter(user_name="first_bot", allow_unverified_webhooks=True, mode="webhook")
        compiled: list[str] = []
        real_compile = re.compile

        def counting_compile(pattern: Any, flags: int = 0) -> Any:
            if isinstance(pattern, str) and pattern.startswith("@"):
                compiled.append(pattern)
            return real_compile(pattern, flags)

        monkeypatch.setattr(re, "compile", counting_compile)

        assert adapter.is_bot_mentioned({}, "hi @first_bot") is True  # type: ignore[typeddict-item]
        # Second call exercises the cached-regex path.
        assert adapter.is_bot_mentioned({}, "hi @first_bot, again") is True  # type: ignore[typeddict-item]
        assert adapter.is_bot_mentioned({}, "hi @second_bot") is False  # type: ignore[typeddict-item]
        assert len(compiled) == 1

        adapter._user_name = "second_bot"
        assert adapter.is_bot_mentioned({}, "hi @second_bot") is True  # type: ignore[typeddict-item]
        assert adapter.is_bot_mentioned({}, "hi @first_bot") is False  # type: ignore[typeddict-item]
        assert len(compiled) == 2

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # JS ``\w`` is ASCII-only, so a non-ASCII letter ends the handle.
            ("hi @mybotж", True),
            ("hi @mybot_x", False),
            ("hi @mybot2", False),
            ("hi @MYBOT!", True),
            ("@mybot", True),
        ],
    )
    def test_mention_boundary_uses_ascii_word_characters(self, text: str, expected: bool):
        adapter = _make_adapter(user_name="mybot")
        assert adapter.is_bot_mentioned({}, text) is expected  # type: ignore[typeddict-item]


class TestTelegramMediaIdentity:
    """Ports of vercel/chat#752 (``fileUniqueId`` + photo MIME)."""

    def test_preserves_stable_photo_identity_and_jpeg_metadata_across_resends_and_serialization(self):
        adapter = _make_adapter(user_name="mybot")
        parsed = adapter.parse_message(
            _message_without_text(
                photo=[
                    {"file_id": "photo-small", "file_unique_id": "photo-small-unique", "width": 100, "height": 100},
                    {"file_id": "photo-download-1", "file_unique_id": "photo-stable", "width": 800, "height": 600},
                ],
                caption="Nice photo",
            )
        )
        resent = adapter.parse_message(
            _sample_message(
                photo=[{"file_id": "photo-download-2", "file_unique_id": "photo-stable", "width": 800, "height": 600}]
            )
        )

        assert len(parsed.attachments) == 1
        attachment = parsed.attachments[0]
        assert attachment.type == "image"
        assert attachment.width == 800
        assert attachment.height == 600
        assert attachment.mime_type == "image/jpeg"
        assert attachment.fetch_metadata == {"fileId": "photo-download-1", "fileUniqueId": "photo-stable"}
        assert resent.attachments[0].fetch_metadata == {"fileId": "photo-download-2", "fileUniqueId": "photo-stable"}
        assert parsed.text == "Nice photo"

        restored = Message.from_json(json.loads(json.dumps(parsed.to_json())))
        (restored_attachment,) = [adapter.rehydrate_attachment(a) for a in restored.attachments]
        assert restored_attachment.fetch_metadata == {"fileId": "photo-download-1", "fileUniqueId": "photo-stable"}
        assert callable(restored_attachment.fetch_data)

    def test_preserves_stable_identity_for_voice_attachments(self):
        adapter = _make_adapter(user_name="mybot")
        parsed = adapter.parse_message(
            _sample_message(
                voice={
                    "file_id": "voice1",
                    "file_unique_id": "voice-stable",
                    "duration": 30,
                    "mime_type": "audio/ogg",
                    "file_size": 512000,
                }
            )
        )
        assert parsed.attachments[0].fetch_metadata == {"fileId": "voice1", "fileUniqueId": "voice-stable"}

    def test_empty_file_unique_id_is_omitted_from_fetch_metadata(self):
        # Upstream spreads ``fileUniqueId`` only when truthy.
        adapter = _make_adapter()
        parsed = adapter.parse_message(_sample_message(document={"file_id": "doc", "file_unique_id": ""}))
        assert parsed.attachments[0].fetch_metadata == {"fileId": "doc"}


class TestTelegramStickerMessages:
    """Ports of ``describe("sticker messages")`` (vercel/chat#835)."""

    @pytest.mark.asyncio
    async def test_represents_a_sticker_by_the_emoji_it_stands_for(self):
        parsed = await _dispatch(
            _message_without_text(
                sticker={"emoji": "\U0001f600", "file_id": "sticker-file", "file_unique_id": "sticker-unique"}
            )
        )
        assert parsed.text == "\U0001f600"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_set_name_when_the_emoji_is_missing(self):
        parsed = await _dispatch(
            _message_without_text(
                sticker={"set_name": "CorgiPack", "file_id": "sticker-file", "file_unique_id": "sticker-unique"}
            )
        )
        assert parsed.text == "CorgiPack"

    @pytest.mark.asyncio
    async def test_never_delivers_a_sticker_as_an_empty_message(self):
        parsed = await _dispatch(
            _message_without_text(sticker={"file_id": "sticker-file", "file_unique_id": "sticker-unique"})
        )
        assert parsed.text == "sticker"


class TestTelegramStickerAndAnimationAttachments:
    """Ports of ``describe("sticker and animation attachments")`` (vercel/chat#835)."""

    @pytest.mark.asyncio
    async def test_carries_a_sticker_through_as_an_image(self):
        parsed = await _dispatch(
            _message_without_text(
                sticker={
                    "emoji": "\U0001f600",
                    "file_id": "sticker-file",
                    "file_unique_id": "sticker-unique",
                    "width": 512,
                    "height": 512,
                }
            )
        )
        assert [(a.type, a.mime_type, a.width) for a in parsed.attachments] == [("image", "image/webp", 512)]

    @pytest.mark.asyncio
    async def test_carries_a_video_sticker_through_as_a_video(self):
        parsed = await _dispatch(
            _message_without_text(
                sticker={
                    "emoji": "\U0001f525",
                    "file_id": "sticker-file",
                    "file_unique_id": "sticker-unique",
                    "is_video": True,
                }
            )
        )
        assert [(a.type, a.mime_type) for a in parsed.attachments] == [("video", "video/webm")]

    @pytest.mark.asyncio
    async def test_carries_a_lottie_sticker_through_as_a_file(self):
        parsed = await _dispatch(
            _message_without_text(
                sticker={
                    "emoji": "\U0001f389",
                    "file_id": "sticker-file",
                    "file_unique_id": "sticker-unique",
                    "is_animated": True,
                }
            )
        )
        assert [(a.type, a.mime_type) for a in parsed.attachments] == [("file", "application/x-tgsticker")]

    @pytest.mark.asyncio
    async def test_carries_an_animation_through_as_a_single_video_attachment(self):
        # Telegram sets ``document`` alongside ``animation`` for backward
        # compatibility; the same file must not surface twice.
        media = {
            "file_id": "animation-file",
            "file_unique_id": "animation-unique",
            "mime_type": "video/mp4",
            "file_name": "cat.mp4",
        }
        parsed = await _dispatch(_message_without_text(animation=dict(media), document=dict(media)))
        assert [(a.type, a.mime_type, a.name) for a in parsed.attachments] == [("video", "video/mp4", "cat.mp4")]
        assert parsed.attachments[0].fetch_metadata == {"fileId": "animation-file", "fileUniqueId": "animation-unique"}

    @pytest.mark.asyncio
    async def test_still_carries_a_plain_document_through_as_a_file(self):
        parsed = await _dispatch(
            _message_without_text(
                document={
                    "file_id": "document-file",
                    "file_unique_id": "document-unique",
                    "mime_type": "application/pdf",
                    "file_name": "report.pdf",
                }
            )
        )
        assert [(a.type, a.mime_type) for a in parsed.attachments] == [("file", "application/pdf")]


class TestTelegramNonFileContent:
    """Ports of ``describe("non-file content")`` (vercel/chat#836)."""

    @pytest.mark.asyncio
    async def test_describes_a_shared_location(self):
        parsed = await _dispatch(_message_without_text(location={"latitude": 55.75, "longitude": 37.61}))
        assert parsed.text == "\U0001f4cd 55.75, 37.61"

    @pytest.mark.asyncio
    async def test_describes_a_venue_by_name_and_address(self):
        # Telegram sets the top-level location on every venue message for
        # backward compatibility; the venue description must still win.
        location = {"latitude": 55.75, "longitude": 37.61}
        parsed = await _dispatch(
            _message_without_text(
                venue={"title": "Central Library", "address": "12 Main St", "location": location},
                location=location,
            )
        )
        assert parsed.text == "\U0001f4cd Central Library, 12 Main St"

    @pytest.mark.asyncio
    async def test_describes_a_shared_contact(self):
        parsed = await _dispatch(
            _message_without_text(
                contact={"first_name": "Ada", "last_name": "Lovelace", "phone_number": "+15551234567"}
            )
        )
        assert parsed.text == "\U0001f464 Ada Lovelace +15551234567"

    @pytest.mark.asyncio
    async def test_describes_a_poll_by_its_question(self):
        parsed = await _dispatch(_message_without_text(poll={"id": "1", "question": "Lunch or dinner?"}))
        assert parsed.text == "\U0001f4ca Lunch or dinner?"

    @pytest.mark.asyncio
    async def test_describes_a_dice_roll(self):
        parsed = await _dispatch(_message_without_text(dice={"emoji": "\U0001f3b2", "value": 4}))
        assert parsed.text == "\U0001f3b2 4"

    @pytest.mark.asyncio
    async def test_describes_a_game_by_its_title(self):
        parsed = await _dispatch(_message_without_text(game={"title": "Corsairs"}))
        assert parsed.text == "\U0001f3ae Corsairs"

    @pytest.mark.asyncio
    async def test_describes_an_invoice_with_its_amount(self):
        parsed = await _dispatch(
            _message_without_text(invoice={"title": "Yearly plan", "total_amount": 4999, "currency": "USD"})
        )
        assert parsed.text == "\U0001f9fe Yearly plan — 49.99 USD"

    @pytest.mark.asyncio
    async def test_keeps_zero_exponent_invoice_currencies_in_whole_units(self):
        jpy = await _dispatch(
            _message_without_text(invoice={"title": "Yearly plan", "total_amount": 5000, "currency": "JPY"})
        )
        xtr = await _dispatch(_message_without_text(invoice={"title": "Boost", "total_amount": 250, "currency": "XTR"}))
        assert jpy.text == "\U0001f9fe Yearly plan — 5000 JPY"
        assert xtr.text == "\U0001f9fe Boost — 250 XTR"

    @pytest.mark.asyncio
    async def test_scales_three_exponent_invoice_currencies_by_a_thousand(self):
        parsed = await _dispatch(
            _message_without_text(invoice={"title": "Yearly plan", "total_amount": 5000, "currency": "BHD"})
        )
        assert parsed.text == "\U0001f9fe Yearly plan — 5.000 BHD"

    @pytest.mark.asyncio
    async def test_marks_a_shared_story(self):
        parsed = await _dispatch(_message_without_text(story={"id": 7}))
        assert parsed.text == "\U0001f4d6 Story"

    @pytest.mark.asyncio
    async def test_integral_and_tiny_float_coordinates_render_like_js_string(self):
        # ``json.loads`` keeps ``51.0`` a float; JS ``String(51.0)`` is "51",
        # and ``String(0.00001)`` is "0.00001" where Python prints "1e-05".
        parsed = await _dispatch(_message_without_text(location={"latitude": 51.0, "longitude": 0.00001}))
        assert parsed.text == "\U0001f4cd 51, 0.00001"

    def test_caption_still_wins_over_non_file_description(self):
        # ``text ?? caption ?? sticker ?? describeNonFileContent``: an empty
        # caption is a real value and short-circuits the fallbacks.
        adapter = _make_adapter()
        parsed = adapter.parse_message(_message_without_text(caption="", poll={"id": "1", "question": "Q?"}))
        assert parsed.text == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (51.0, "51"),
        (-0.0, "0"),
        (0.000001, "0.000001"),
        (1.5e-7, "1.5e-7"),
        (-1e-7, "-1e-7"),
        (123.456, "123.456"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (2.5e22, "2.5e+22"),
        (4, "4"),
    ],
)
def test_js_number_str_matches_javascript_string_conversion(value: float, expected: str):
    assert _js_number_str(value) == expected


class _FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.consumed = 0

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes], content_length: int | None, status: int = 200) -> None:
        self.content = _FakeContent(chunks)
        self.content_length = content_length
        self.status = status
        self.ok = status < 400

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _FakeSession:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.get_calls: list[tuple[str, Any]] = []

    def get(self, url: str, *, timeout: Any = None) -> Any:
        self.get_calls.append((url, timeout))
        return self.response


def _download_adapter(session: _FakeSession) -> TelegramAdapter:
    adapter = _make_adapter(bot_token="token")
    adapter.telegram_fetch = AsyncMock(return_value={"file_id": "f1", "file_path": "photos/a.jpg"})  # type: ignore[method-assign]
    adapter._get_http_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    return adapter


class TestTelegramDownloadLimits:
    """25 MB / 30 s download guard (vercel/chat#865, Telegram half)."""

    @pytest.mark.asyncio
    async def test_downloads_body_within_limit_with_a_total_timeout(self):
        import aiohttp

        session = _FakeSession(_FakeResponse([b"abc", b"def"], content_length=6))
        adapter = _download_adapter(session)

        assert await adapter.download_file("f1") == b"abcdef"
        ((url, timeout),) = session.get_calls
        assert url == "https://api.telegram.org/file/bottoken/photos/a.jpg"
        assert timeout == aiohttp.ClientTimeout(total=30)

    @pytest.mark.asyncio
    async def test_rejects_a_declared_content_length_over_the_limit_without_reading(self):
        response = _FakeResponse([b"x"], content_length=TELEGRAM_FILE_LIMIT + 1)
        adapter = _download_adapter(_FakeSession(response))

        with pytest.raises(NetworkError, match="Telegram file f1 exceeds the download limit"):
            await adapter.download_file("f1")
        assert response.content.consumed == 0

    @pytest.mark.asyncio
    async def test_rejects_an_undeclared_body_once_the_running_count_passes_the_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import chat_sdk.adapters.telegram.adapter as telegram_adapter_module

        monkeypatch.setattr(telegram_adapter_module, "TELEGRAM_FILE_LIMIT", 10)
        response = _FakeResponse([b"12345", b"67890", b"1", b"never-read"], content_length=None)
        adapter = _download_adapter(_FakeSession(response))

        with pytest.raises(NetworkError, match="exceeds the download limit"):
            await adapter.download_file("f1")
        # Stopped at the chunk that crossed the cap; nothing after it is read.
        assert response.content.consumed == 3

    @pytest.mark.asyncio
    async def test_accepts_a_body_exactly_at_the_limit(self, monkeypatch: pytest.MonkeyPatch):
        import chat_sdk.adapters.telegram.adapter as telegram_adapter_module

        monkeypatch.setattr(telegram_adapter_module, "TELEGRAM_FILE_LIMIT", 10)
        adapter = _download_adapter(_FakeSession(_FakeResponse([b"12345", b"67890"], content_length=10)))
        assert await adapter.download_file("f1") == b"1234567890"

    @pytest.mark.asyncio
    async def test_unparseable_content_length_falls_back_to_the_running_count(self, monkeypatch: pytest.MonkeyPatch):
        # aiohttp's ``content_length`` raises ``ValueError`` on a malformed
        # header; upstream's ``Number(...)`` gives NaN and skips the check.
        import chat_sdk.adapters.telegram.adapter as telegram_adapter_module

        class _BadLength(_FakeResponse):
            @property  # type: ignore[override]
            def content_length(self) -> int | None:
                raise ValueError("invalid literal for int()")

            @content_length.setter
            def content_length(self, _value: Any) -> None:
                pass

        monkeypatch.setattr(telegram_adapter_module, "TELEGRAM_FILE_LIMIT", 4)
        ok = _download_adapter(_FakeSession(_BadLength([b"ab"], content_length=None)))
        assert await ok.download_file("f1") == b"ab"
        too_big = _download_adapter(_FakeSession(_BadLength([b"abc", b"de"], content_length=None)))
        with pytest.raises(NetworkError, match="exceeds the download limit"):
            await too_big.download_file("f1")

    @pytest.mark.asyncio
    async def test_timeout_raises_network_error(self):
        class _TimingOut:
            async def __aenter__(self) -> Any:
                raise TimeoutError

            async def __aexit__(self, *_exc: Any) -> None:
                return None

        adapter = _download_adapter(_FakeSession(_TimingOut()))
        with pytest.raises(NetworkError, match="Failed to download Telegram file f1") as info:
            await adapter.download_file("f1")
        assert isinstance(info.value.original_error, TimeoutError)
