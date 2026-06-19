"""Port of adapter-telegram/src/index.test.ts -- webhook handling, message processing,
postMessage, editMessage, deleteMessage, reactions, stream, parseMessage, fetchMessages,
and factory tests.

Tests that duplicate the existing ``test_telegram_adapter.py`` are intentionally
omitted; this file covers the *remaining* TypeScript tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TelegramAdapter,
    apply_telegram_entities,
    create_telegram_adapter,
)
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig, TelegramThreadId
from chat_sdk.shared.errors import ValidationError

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
        adapter = _make_adapter()
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
    from unittest.mock import MagicMock

    adapter = _make_adapter(user_name="mybot")
    chat = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.process_message = MagicMock()
    adapter._chat = chat
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
