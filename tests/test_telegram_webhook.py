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
    TelegramAdapter,
    apply_telegram_entities,
    create_telegram_adapter,
)
from chat_sdk.adapters.telegram.cards import encode_telegram_callback_data
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig, TelegramThreadId
from chat_sdk.shared.errors import NetworkError, ValidationError
from chat_sdk.shared.mock_adapter import MockStateAdapter, create_mock_state

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
        assert parsed.attachments[0].fetch_metadata == {"fileId": "vn2"}

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
        assert image.fetch_metadata == {"fileId": "large"}  # LARGEST size, not "small"
        assert image.size == 2048
        assert image.width == 1200
        assert image.height == 800

        video = parsed.attachments[1]
        assert video.type == "video"
        assert video.fetch_metadata == {"fileId": "video"}
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
        assert attachment.fetch_metadata == {"fileId": "anim-img"}
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
        assert attachment.fetch_metadata == {"fileId": "voice"}
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
        assert parsed.attachments[0].fetch_metadata == {"fileId": "listed-photo"}

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
        assert parsed.attachments[0].fetch_metadata == {"fileId": "doc1"}
        assert parsed.attachments[1].fetch_metadata == {"fileId": "rich-photo"}


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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("update_id", [True, False, "1", 1.0, None])
    async def test_non_integer_update_ids_are_dispatched_without_a_claim(self, update_id: Any):
        # ``Number.isInteger`` parity: ``bool`` is an ``int`` subclass in Python
        # but must not be claimed; strings/floats/null skip the claim too.
        state = _spy_state()
        adapter = _dedupe_adapter()
        chat = _mock_chat(state)
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(_update_request({"update_id": update_id, "message": _sample_message()}))

        assert response["status"] == 200
        chat.process_message.assert_called_once()
        state.set_if_not_exists.assert_not_awaited()

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
        waiters = [asyncio.ensure_future(adapter._ensure_bot_identity()) for _ in range(3)]
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
        cancelled = asyncio.ensure_future(adapter._ensure_bot_identity())
        survivor = asyncio.ensure_future(adapter._ensure_bot_identity())
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
    async def test_polling_mode_initialize_does_not_require_verification(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS", raising=False)
        adapter = _dedupe_adapter(mode="polling", secret_token=None)
        adapter.start_polling = AsyncMock()  # type: ignore[method-assign]

        await adapter.initialize(_mock_chat(_spy_state()))

        assert adapter.runtime_mode == "polling"
        adapter.start_polling.assert_awaited_once()
