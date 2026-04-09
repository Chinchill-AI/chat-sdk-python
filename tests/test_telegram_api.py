"""Tests for Telegram adapter API-calling methods.

Covers: post_message (text, card, parse mode), edit_message, delete_message,
add_reaction, remove_reaction, start_typing, callback query dispatch,
reaction update dispatch, error mapping (401, 429, 403),
fetch_thread, fetch_channel_info.

Mocks telegram_fetch to intercept all Bot API calls without network access.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.telegram.adapter import TelegramAdapter
from chat_sdk.adapters.telegram.types import (
    TelegramAdapterConfig,
)
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    ValidationError,
)

# =============================================================================
# Helpers
# =============================================================================

CHAT_ID = "-1001234567890"
THREAD_ID = f"telegram:{CHAT_ID}"
MESSAGE_ID_INT = 42
COMPOSITE_MESSAGE_ID = f"{CHAT_ID}:{MESSAGE_ID_INT}"


def _make_adapter(**overrides: Any) -> TelegramAdapter:
    """Create a TelegramAdapter with minimal valid config."""
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        **overrides,
    )
    return TelegramAdapter(config)


def _make_telegram_message(
    chat_id: str = CHAT_ID,
    message_id: int = MESSAGE_ID_INT,
    text: str = "Hello",
) -> dict[str, Any]:
    """Return a minimal Telegram message dict."""
    return {
        "message_id": message_id,
        "chat": {"id": int(chat_id), "type": "supergroup"},
        "from": {"id": 111, "is_bot": False, "first_name": "Alice"},
        "date": 1700000000,
        "text": text,
    }


def _init_adapter(adapter: TelegramAdapter) -> MagicMock:
    """Wire up a mock ChatInstance so dispatch methods work."""
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.process_action = MagicMock()
    chat.process_reaction = MagicMock()
    adapter._chat = chat
    adapter._bot_user_id = "999"
    return chat


# =============================================================================
# Tests -- post_message
# =============================================================================


class TestPostMessageSendsText:
    """post_message with plain markdown sends sendMessage."""

    @pytest.mark.asyncio
    async def test_post_message_sends_text(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Hi"))

        result = await adapter.post_message(THREAD_ID, {"markdown": "Hi"})

        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        method = call_args[0][0]
        payload = call_args[0][1]

        assert method == "sendMessage"
        assert payload["chat_id"] == CHAT_ID
        assert payload["text"] == "Hi"
        assert result.id is not None


class TestPostMessageWithCard:
    """post_message with a card includes reply_markup with inline keyboard."""

    @pytest.mark.asyncio
    async def test_post_message_with_card(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Pick"))

        # Use the proper CardElement structure with Actions and Buttons
        card_msg = {
            "card": {
                "type": "card",
                "title": "Question",
                "children": [
                    {"type": "text", "content": "Pick one"},
                    {
                        "type": "actions",
                        "children": [
                            {"type": "button", "id": "yes", "label": "Yes"},
                            {"type": "button", "id": "no", "label": "No"},
                        ],
                    },
                ],
            }
        }

        await adapter.post_message(THREAD_ID, card_msg)

        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        method = call_args[0][0]
        payload = call_args[0][1]

        assert method == "sendMessage"
        # reply_markup should be present for the card buttons
        assert payload.get("reply_markup") is not None
        keyboard = payload["reply_markup"]["inline_keyboard"]
        assert len(keyboard) > 0
        # With a card, parse_mode should be Markdown
        assert payload.get("parse_mode") == "Markdown"


class TestPostMessageParseMode:
    """post_message with markdown content sends Markdown parse_mode."""

    @pytest.mark.asyncio
    async def test_post_message_parse_mode_markdown(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="**bold**"))

        await adapter.post_message(THREAD_ID, {"markdown": "**bold**"})

        payload = adapter.telegram_fetch.call_args[0][1]
        assert payload.get("parse_mode") == "Markdown"


# =============================================================================
# Tests -- edit_message
# =============================================================================


class TestEditMessage:
    """edit_message calls editMessageText with the correct payload."""

    @pytest.mark.asyncio
    async def test_edit_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="Updated"))

        await adapter.edit_message(
            THREAD_ID,
            COMPOSITE_MESSAGE_ID,
            {"markdown": "Updated"},
        )

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "editMessageText"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT
        assert payload["text"] == "Updated"


# =============================================================================
# Tests -- delete_message
# =============================================================================


class TestDeleteMessage:
    """delete_message calls deleteMessage with chat_id and message_id."""

    @pytest.mark.asyncio
    async def test_delete_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.delete_message(THREAD_ID, COMPOSITE_MESSAGE_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "deleteMessage"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT


# =============================================================================
# Tests -- add_reaction / remove_reaction
# =============================================================================


class TestAddReaction:
    """add_reaction sends setMessageReaction with a reaction array."""

    @pytest.mark.asyncio
    async def test_add_reaction(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.add_reaction(THREAD_ID, COMPOSITE_MESSAGE_ID, "thumbs_up")

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "setMessageReaction"
        assert payload["chat_id"] == CHAT_ID
        assert payload["message_id"] == MESSAGE_ID_INT
        # reaction should be a non-empty list
        assert isinstance(payload["reaction"], list)
        assert len(payload["reaction"]) == 1
        assert payload["reaction"][0]["type"] == "emoji"


class TestRemoveReaction:
    """remove_reaction sends setMessageReaction with an empty array."""

    @pytest.mark.asyncio
    async def test_remove_reaction(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.remove_reaction(THREAD_ID, COMPOSITE_MESSAGE_ID, "thumbs_up")

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "setMessageReaction"
        assert payload["reaction"] == []


# =============================================================================
# Tests -- start_typing
# =============================================================================


class TestStartTyping:
    """start_typing sends sendChatAction with action=typing."""

    @pytest.mark.asyncio
    async def test_start_typing(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        await adapter.start_typing(THREAD_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "sendChatAction"
        assert payload["chat_id"] == CHAT_ID
        assert payload["action"] == "typing"


# =============================================================================
# Tests -- callback query dispatch
# =============================================================================


class TestCallbackQueryDispatch:
    """Callback query dispatches process_action with the correct action_id."""

    def test_callback_query_dispatch(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        callback_query = {
            "id": "cq-001",
            "from": {"id": 111, "is_bot": False, "first_name": "Alice"},
            "message": {
                "message_id": 42,
                "chat": {"id": int(CHAT_ID), "type": "supergroup"},
                "date": 1700000000,
                "text": "Prompt",
            },
            "data": "approve",
        }

        adapter.handle_callback_query(callback_query)

        chat.process_action.assert_called_once()
        action_payload = chat.process_action.call_args[0][0]
        assert action_payload.action_id == "approve"
        assert action_payload.adapter is adapter


# =============================================================================
# Tests -- reaction update dispatch
# =============================================================================


class TestReactionUpdateDispatch:
    """Reaction updates dispatch process_reaction for added AND removed."""

    def test_reaction_update_dispatch(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        reaction_update = {
            "chat": {"id": int(CHAT_ID), "type": "supergroup"},
            "message_id": MESSAGE_ID_INT,
            "date": 1700000000,
            "old_reaction": [{"type": "emoji", "emoji": "\ud83d\udc4d"}],
            "new_reaction": [{"type": "emoji", "emoji": "\u2764\ufe0f"}],
        }

        adapter.handle_message_reaction_update(reaction_update)

        # Should fire twice: once for the added reaction, once for the removed
        assert chat.process_reaction.call_count == 2

        calls = chat.process_reaction.call_args_list
        # One should be added=True, one added=False
        added_flags = {c[0][0].added for c in calls}
        assert added_flags == {True, False}


# =============================================================================
# Tests -- error mapping
# =============================================================================


class TestErrorMapping401:
    """401 response maps to AuthenticationError."""

    def test_error_mapping_401(self):
        adapter = _make_adapter()
        with pytest.raises(AuthenticationError):
            adapter.throw_telegram_api_error(
                "getMe",
                401,
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
            )


class TestErrorMapping429:
    """429 response maps to AdapterRateLimitError."""

    def test_error_mapping_429(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterRateLimitError):
            adapter.throw_telegram_api_error(
                "sendMessage",
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 30},
                },
            )


class TestErrorMapping403:
    """403 response maps to PermissionError."""

    def test_error_mapping_403(self):
        adapter = _make_adapter()
        with pytest.raises(AdapterPermissionError):
            adapter.throw_telegram_api_error(
                "sendMessage",
                403,
                {"ok": False, "error_code": 403, "description": "Forbidden"},
            )


# =============================================================================
# Tests -- fetch_thread
# =============================================================================


class TestFetchThread:
    """fetch_thread calls getChat and returns ThreadInfo."""

    @pytest.mark.asyncio
    async def test_fetch_thread(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(
            return_value={
                "id": int(CHAT_ID),
                "type": "supergroup",
                "title": "My Group",
            }
        )

        info = await adapter.fetch_thread(THREAD_ID)

        adapter.telegram_fetch.assert_called_once()
        method = adapter.telegram_fetch.call_args[0][0]
        payload = adapter.telegram_fetch.call_args[0][1]

        assert method == "getChat"
        assert payload["chat_id"] == CHAT_ID
        assert info.channel_name == "My Group"
        assert info.is_dm is False


# =============================================================================
# Tests -- fetch_channel_info
# =============================================================================


class TestFetchChannelInfo:
    """fetch_channel_info calls getChat + getChatMemberCount."""

    @pytest.mark.asyncio
    async def test_fetch_channel_info(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # telegram_fetch is called twice: first getChat, then getChatMemberCount
        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                {
                    "id": int(CHAT_ID),
                    "type": "supergroup",
                    "title": "Dev Team",
                },
                150,  # member count
            ]
        )

        info = await adapter.fetch_channel_info(CHAT_ID)

        assert adapter.telegram_fetch.call_count == 2
        first_call = adapter.telegram_fetch.call_args_list[0]
        second_call = adapter.telegram_fetch.call_args_list[1]

        assert first_call[0][0] == "getChat"
        assert second_call[0][0] == "getChatMemberCount"
        assert info.name == "Dev Team"
        assert info.member_count == 150
        assert info.is_dm is False


# =============================================================================
# Tests -- fetch_channel_info member count failure
# =============================================================================


class TestFetchChannelInfoMemberCountFails:
    """getChatMemberCount failure is swallowed and member_count is None."""

    @pytest.mark.asyncio
    async def test_member_count_failure(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        adapter.telegram_fetch = AsyncMock(
            side_effect=[
                {"id": int(CHAT_ID), "type": "supergroup", "title": "Grp"},
                Exception("count failed"),
            ]
        )

        info = await adapter.fetch_channel_info(CHAT_ID)
        assert info.name == "Grp"
        assert info.member_count is None


# =============================================================================
# Tests -- handle_webhook
# =============================================================================


class TestHandleWebhook:
    """Webhook handling including secret token verification."""

    @pytest.mark.asyncio
    async def test_webhook_rejects_invalid_secret(self):
        adapter = _make_adapter(secret_token="correct-secret")
        _init_adapter(adapter)

        class FakeReq:
            headers = {"x-telegram-bot-api-secret-token": "wrong-secret"}

            async def text(self):
                return '{"update_id": 1}'

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 401

    @pytest.mark.asyncio
    async def test_webhook_accepts_valid_secret(self):
        adapter = _make_adapter(secret_token="my-secret")
        _init_adapter(adapter)

        class FakeReq:
            headers = {"x-telegram-bot-api-secret-token": "my-secret"}

            async def text(self):
                return (
                    '{"update_id": 1, "message": {"message_id": 1,'
                    ' "chat": {"id": 123, "type": "private"},'
                    ' "from": {"id": 111, "is_bot": false, "first_name": "A"},'
                    ' "date": 1700000000, "text": "hi"}}'
                )

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_webhook_warns_no_verification(self):
        adapter = _make_adapter()  # no secret_token
        _init_adapter(adapter)

        class FakeReq:
            headers = {}

            async def text(self):
                return '{"update_id": 1}'

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 200

    @pytest.mark.asyncio
    async def test_webhook_invalid_json(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        class FakeReq:
            headers = {}

            async def text(self):
                return "not-json"

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 400

    @pytest.mark.asyncio
    async def test_webhook_no_chat_instance(self):
        adapter = _make_adapter()
        # _chat is None (not initialized)

        class FakeReq:
            headers = {}

            async def text(self):
                return '{"update_id": 1}'

        result = await adapter.handle_webhook(FakeReq())
        assert result["status"] == 200


# =============================================================================
# Tests -- process_update dispatch
# =============================================================================


class TestProcessUpdateDispatch:
    """process_update dispatches to correct handlers."""

    def test_dispatches_edited_message(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "edited_message": _make_telegram_message(text="edited"),
        }
        adapter.process_update(update)
        assert chat.process_message.call_count == 1

    def test_dispatches_channel_post(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "channel_post": _make_telegram_message(text="channel"),
        }
        adapter.process_update(update)
        assert chat.process_message.call_count == 1

    def test_dispatches_reaction(self):
        adapter = _make_adapter()
        chat = _init_adapter(adapter)

        update = {
            "update_id": 1,
            "message_reaction": {
                "chat": {"id": int(CHAT_ID), "type": "supergroup"},
                "message_id": 42,
                "date": 1700000000,
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": "\ud83d\udc4d"}],
            },
        }
        adapter.process_update(update)
        assert chat.process_reaction.call_count == 1

    def test_handle_incoming_message_no_chat(self):
        adapter = _make_adapter()
        # When _chat is None, handle_incoming_message_update returns early without error
        result = adapter.handle_incoming_message_update(_make_telegram_message())
        assert result is None
        assert adapter._chat is None


# =============================================================================
# Tests -- edit_message inline result (True)
# =============================================================================


class TestEditMessageInlineResult:
    """When Telegram returns True for inline message edits."""

    @pytest.mark.asyncio
    async def test_edit_inline_message_with_cache(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # First post a message to populate cache
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="original"))
        await adapter.post_message(THREAD_ID, {"markdown": "original"})

        # Now edit - Telegram returns True for inline edits
        adapter.telegram_fetch = AsyncMock(return_value=True)

        result = await adapter.edit_message(
            THREAD_ID,
            COMPOSITE_MESSAGE_ID,
            {"markdown": "Updated"},
        )
        assert result.id == COMPOSITE_MESSAGE_ID

    @pytest.mark.asyncio
    async def test_edit_inline_no_cache_raises(self):
        from chat_sdk.errors import ChatNotImplementedError

        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=True)

        with pytest.raises(ChatNotImplementedError):
            await adapter.edit_message(
                THREAD_ID,
                COMPOSITE_MESSAGE_ID,
                {"markdown": "fail"},
            )


# =============================================================================
# Tests -- edit_message empty text raises
# =============================================================================


class TestEditMessageEmpty:
    """Editing with empty text raises ValidationError."""

    @pytest.mark.asyncio
    async def test_edit_empty_text_raises(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock()

        with pytest.raises(ValidationError):
            await adapter.edit_message(
                THREAD_ID,
                COMPOSITE_MESSAGE_ID,
                {"markdown": ""},
            )


# =============================================================================
# Tests -- post_message empty text raises
# =============================================================================


class TestPostMessageEmptyText:
    """Posting with empty text raises ValidationError."""

    @pytest.mark.asyncio
    async def test_post_empty_text_raises(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock()

        with pytest.raises(ValidationError):
            await adapter.post_message(THREAD_ID, {"markdown": ""})


# =============================================================================
# Tests -- to_telegram_reaction
# =============================================================================


class TestToTelegramReaction:
    """to_telegram_reaction handles different emoji input types."""

    def test_emoji_value_input(self):
        from chat_sdk.types import EmojiValue

        adapter = _make_adapter()
        result = adapter.to_telegram_reaction(EmojiValue(name="thumbs_up"))
        assert result["type"] == "emoji"

    def test_custom_emoji_prefix(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("custom:12345")
        assert result["type"] == "custom_emoji"
        assert result["custom_emoji_id"] == "12345"

    def test_emoji_placeholder(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("{{emoji:thumbs_up}}")
        assert result["type"] == "emoji"

    def test_emoji_name_string(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("thumbs_up")
        assert result["type"] == "emoji"

    def test_raw_emoji_passthrough(self):
        adapter = _make_adapter()
        result = adapter.to_telegram_reaction("\ud83d\ude00")  # grinning face
        assert result["type"] == "emoji"
        assert result["emoji"] == "\ud83d\ude00"


# =============================================================================
# Tests -- reaction_key / reaction_to_emoji_value
# =============================================================================


class TestReactionHelpers:
    def test_reaction_key_emoji(self):
        adapter = _make_adapter()
        assert adapter.reaction_key({"type": "emoji", "emoji": "\ud83d\udc4d"}) == "\ud83d\udc4d"

    def test_reaction_key_custom(self):
        adapter = _make_adapter()
        result = adapter.reaction_key({"type": "custom_emoji", "custom_emoji_id": "123"})
        assert result == "custom:123"

    def test_reaction_to_emoji_value_emoji(self):
        adapter = _make_adapter()
        result = adapter.reaction_to_emoji_value({"type": "emoji", "emoji": "\ud83d\udc4d"})
        assert result.name == "\ud83d\udc4d"

    def test_reaction_to_emoji_value_custom(self):
        adapter = _make_adapter()
        result = adapter.reaction_to_emoji_value({"type": "custom_emoji", "custom_emoji_id": "456"})
        assert result.name == "custom:456"


# =============================================================================
# Tests -- parse_telegram_message author variants
# =============================================================================


class TestParseTelegramMessageAuthors:
    """parse_telegram_message handles different author source fields."""

    def test_sender_chat_author(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "sender_chat": {"id": 456, "type": "channel", "title": "News"},
            "date": 1700000000,
            "text": "Channel post",
        }
        result = adapter.parse_telegram_message(msg, THREAD_ID)
        assert result.author.user_id == "chat:456"

    def test_fallback_author(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 789, "type": "supergroup", "title": "Group"},
            "date": 1700000000,
            "text": "Anonymous",
        }
        result = adapter.parse_telegram_message(msg, THREAD_ID)
        assert result.author.user_id == "789"
        assert result.author.user_name == "Group"


# =============================================================================
# Tests -- is_bot_mentioned
# =============================================================================


class TestIsBotMentioned:
    """Bot mention detection covers various entity types."""

    def test_mention_entity(self):
        adapter = _make_adapter(user_name="testbot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "@testbot hello",
            "entities": [{"type": "mention", "offset": 0, "length": 8}],
        }
        assert adapter.is_bot_mentioned(msg, "@testbot hello")

    def test_text_mention_entity(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter._bot_user_id = "999"

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "hello bot",
            "entities": [
                {"type": "text_mention", "offset": 6, "length": 3, "user": {"id": 999}},
            ],
        }
        assert adapter.is_bot_mentioned(msg, "hello bot")

    def test_bot_command_with_mention(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "from": {"id": 111, "is_bot": False, "first_name": "A"},
            "date": 1700000000,
            "text": "/start@mybot",
            "entities": [{"type": "bot_command", "offset": 0, "length": 12}],
        }
        assert adapter.is_bot_mentioned(msg, "/start@mybot")

    def test_regex_mention_fallback(self):
        adapter = _make_adapter(user_name="fallbot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "date": 1700000000,
            "text": "hey @fallbot check this",
        }
        assert adapter.is_bot_mentioned(msg, "hey @fallbot check this")

    def test_no_mention(self):
        adapter = _make_adapter(user_name="mybot")
        _init_adapter(adapter)

        msg = {
            "message_id": 1,
            "chat": {"id": 123, "type": "supergroup"},
            "date": 1700000000,
            "text": "hello world",
        }
        assert not adapter.is_bot_mentioned(msg, "hello world")

    def test_empty_text(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        msg = {"message_id": 1, "chat": {"id": 123}, "date": 1700000000, "text": ""}
        assert not adapter.is_bot_mentioned(msg, "")


# =============================================================================
# Tests -- resolve_parse_mode
# =============================================================================


class TestResolveParseMode:
    def test_card_returns_markdown(self):
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({}, {"type": "card"}) == "Markdown"

    def test_markdown_key_returns_markdown(self):
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({"markdown": "**bold**"}, None) == "Markdown"

    def test_plain_text_returns_none(self):
        adapter = _make_adapter()
        assert adapter.resolve_parse_mode({"text": "hello"}, None) is None


# =============================================================================
# Tests -- truncate_message / truncate_caption
# =============================================================================


class TestTruncation:
    def test_truncate_short_message(self):
        adapter = _make_adapter()
        assert adapter.truncate_message("short") == "short"

    def test_truncate_long_message(self):
        adapter = _make_adapter()
        long_text = "x" * 5000
        result = adapter.truncate_message(long_text)
        assert len(result) <= 4096

    def test_truncate_caption(self):
        adapter = _make_adapter()
        long_text = "x" * 2000
        result = adapter.truncate_caption(long_text)
        assert len(result) <= 1024


# =============================================================================
# Tests -- decode_composite_message_id edge cases
# =============================================================================


class TestDecodeCompositeMessageId:
    def test_simple_numeric_with_expected_chat_id(self):
        adapter = _make_adapter()
        result = adapter.decode_composite_message_id("42", CHAT_ID)
        assert result["chat_id"] == CHAT_ID
        assert result["message_id"] == 42

    def test_invalid_message_id_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_composite_message_id("not-a-number", CHAT_ID)

    def test_no_expected_chat_id_no_composite_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.decode_composite_message_id("just-text")

    def test_chat_id_mismatch_raises(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError, match="mismatch"):
            adapter.decode_composite_message_id(f"wrong:{MESSAGE_ID_INT}", CHAT_ID)


# =============================================================================
# Tests -- throw_telegram_api_error additional branches
# =============================================================================


class TestThrowTelegramApiErrorBranches:
    def test_error_404_raises_not_found(self):
        from chat_sdk.shared.errors import ResourceNotFoundError

        adapter = _make_adapter()
        with pytest.raises(ResourceNotFoundError):
            adapter.throw_telegram_api_error("getChat", 404, {"ok": False, "error_code": 404})

    def test_error_400_raises_validation(self):
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            adapter.throw_telegram_api_error(
                "sendMessage", 400, {"ok": False, "error_code": 400, "description": "Bad Request"}
            )

    def test_error_500_raises_network(self):
        from chat_sdk.shared.errors import NetworkError

        adapter = _make_adapter()
        with pytest.raises(NetworkError):
            adapter.throw_telegram_api_error(
                "sendMessage", 500, {"ok": False, "error_code": 500, "description": "Server error"}
            )


# =============================================================================
# Tests -- fetch_channel_messages
# =============================================================================


class TestFetchChannelMessages:
    """fetch_channel_messages aggregates from cache."""

    @pytest.mark.asyncio
    async def test_fetch_channel_messages_from_cache(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        # Populate cache with messages in the thread
        msg1 = _make_telegram_message(text="Msg 1", message_id=1)
        msg2 = _make_telegram_message(text="Msg 2", message_id=2)
        adapter.parse_message(msg1)
        adapter.parse_message(msg2)

        result = await adapter.fetch_channel_messages(CHAT_ID)
        assert len(result.messages) == 2


# =============================================================================
# Tests -- normalize_user_name
# =============================================================================


class TestNormalizeUserName:
    def test_strips_leading_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@mybot") == "mybot"

    def test_strips_multiple_at(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@@@@mybot") == "mybot"

    def test_non_string_returns_bot(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name(None) == "bot"
        assert adapter.normalize_user_name(123) == "bot"

    def test_empty_string_returns_bot(self):
        adapter = _make_adapter()
        assert adapter.normalize_user_name("@") == "bot"


# =============================================================================
# Tests -- resolve_polling_config
# =============================================================================


class TestResolvePollingConfig:
    def test_default_config(self):
        adapter = _make_adapter()
        config = adapter.resolve_polling_config()
        assert config.limit == 100
        assert config.timeout == 30
        assert config.delete_webhook is True
        assert config.drop_pending_updates is False

    def test_override_config(self):
        from chat_sdk.adapters.telegram.types import TelegramLongPollingConfig

        adapter = _make_adapter()
        config = adapter.resolve_polling_config(TelegramLongPollingConfig(limit=50, timeout=10, delete_webhook=False))
        assert config.limit == 50
        assert config.timeout == 10
        assert config.delete_webhook is False


# =============================================================================
# Tests -- clamp_integer
# =============================================================================


class TestClampInteger:
    def test_clamps_too_high(self):
        assert TelegramAdapter.clamp_integer(200, 100, 1, 100) == 100

    def test_clamps_too_low(self):
        assert TelegramAdapter.clamp_integer(-5, 100, 1, 100) == 1

    def test_none_returns_fallback(self):
        assert TelegramAdapter.clamp_integer(None, 42, 0, 100) == 42

    def test_float_truncated(self):
        assert TelegramAdapter.clamp_integer(3.7, 10, 0, 100) == 3

    def test_nan_returns_fallback(self):
        assert TelegramAdapter.clamp_integer(float("nan"), 10, 0, 100) == 10


# =============================================================================
# Tests -- _resolve_thread_id
# =============================================================================


class TestResolveThreadId:
    def test_with_prefix(self):
        adapter = _make_adapter()
        result = adapter._resolve_thread_id(THREAD_ID)
        assert result.chat_id == CHAT_ID

    def test_without_prefix(self):
        adapter = _make_adapter()
        result = adapter._resolve_thread_id(CHAT_ID)
        assert result.chat_id == CHAT_ID


# =============================================================================
# Tests -- channel_id_from_thread_id
# =============================================================================


class TestChannelIdFromThreadId:
    def test_strips_topic(self):
        adapter = _make_adapter()
        thread = f"telegram:{CHAT_ID}:42"
        result = adapter.channel_id_from_thread_id(thread)
        assert result == f"telegram:{CHAT_ID}"


# =============================================================================
# Tests -- open_dm
# =============================================================================


class TestOpenDm:
    @pytest.mark.asyncio
    async def test_open_dm(self):
        adapter = _make_adapter()
        result = await adapter.open_dm("12345")
        assert result == "telegram:12345"


# =============================================================================
# Tests -- is_dm
# =============================================================================


class TestIsDm:
    def test_positive_chat_id_is_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm("telegram:12345") is True

    def test_negative_chat_id_is_not_dm(self):
        adapter = _make_adapter()
        assert adapter.is_dm(THREAD_ID) is False


# =============================================================================
# Tests -- post_channel_message
# =============================================================================


class TestPostChannelMessage:
    @pytest.mark.asyncio
    async def test_delegates_to_post_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        adapter.telegram_fetch = AsyncMock(return_value=_make_telegram_message(text="chan"))

        result = await adapter.post_channel_message(THREAD_ID, {"markdown": "chan"})
        assert result.id == COMPOSITE_MESSAGE_ID
        assert result.thread_id == THREAD_ID
        # Verify it delegated to telegram_fetch (i.e. post_message internally)
        adapter.telegram_fetch.assert_called_once()
        call_args = adapter.telegram_fetch.call_args
        assert call_args[0][0] == "sendMessage"


# =============================================================================
# Tests -- _get_request_body / _get_header helpers
# =============================================================================


class TestRequestHelpers:
    @pytest.mark.asyncio
    async def test_get_body_from_body_bytes(self):
        class Req:
            body = b"raw bytes"

        result = await TelegramAdapter._get_request_body(Req())
        assert result == "raw bytes"

    @pytest.mark.asyncio
    async def test_get_body_from_body_callable(self):
        class Req:
            async def body(self):
                return b"async bytes"

        # body is callable
        result = await TelegramAdapter._get_request_body(Req())
        assert result == "async bytes"

    @pytest.mark.asyncio
    async def test_get_body_empty(self):
        class Req:
            pass

        result = await TelegramAdapter._get_request_body(Req())
        assert result == ""

    def test_get_header_dict(self):
        class Req:
            headers = {"X-Custom": "value"}

        result = TelegramAdapter._get_header(Req(), "x-custom")
        assert result == "value"

    def test_get_header_none(self):
        class Req:
            pass

        assert TelegramAdapter._get_header(Req(), "x-any") is None

    def test_get_header_mapping(self):
        class Headers:
            def get(self, name):
                return "mapped" if name == "x-test" else None

        class Req:
            headers = Headers()

        assert TelegramAdapter._get_header(Req(), "x-test") == "mapped"


# =============================================================================
# Tests -- compare_messages / message_sequence
# =============================================================================


class TestMessageHelpers:
    def test_message_sequence(self):
        adapter = _make_adapter()
        assert adapter.message_sequence(f"{CHAT_ID}:42") == 42
        assert adapter.message_sequence("no-sequence") == 0

    def test_compare_messages_by_time(self):
        from datetime import UTC, datetime

        from chat_sdk.types import Author, FormattedContent, Message, MessageMetadata

        adapter = _make_adapter()
        fmt: FormattedContent = {"type": "root", "children": []}
        author = Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False)
        a = Message(
            id="1",
            thread_id=THREAD_ID,
            text="a",
            formatted=fmt,
            raw={},
            author=author,
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 1, tzinfo=UTC)),
        )
        b = Message(
            id="2",
            thread_id=THREAD_ID,
            text="b",
            formatted=fmt,
            raw={},
            author=author,
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 2, tzinfo=UTC)),
        )
        assert adapter.compare_messages(a, b) == -1
        assert adapter.compare_messages(b, a) == 1


# =============================================================================
# Tests -- paginate_messages
# =============================================================================


class TestPaginateMessages:
    def test_empty_messages(self):
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        result = adapter.paginate_messages([], FetchOptions())
        assert result.messages == []

    def test_backward_pagination(self):
        from datetime import UTC, datetime

        from chat_sdk.types import Author, FetchOptions, Message, MessageMetadata

        adapter = _make_adapter()

        fmt: Any = {"type": "root", "children": []}

        def _msg(i: int) -> Message:
            return Message(
                id=f"{CHAT_ID}:{i}",
                thread_id=THREAD_ID,
                text=f"msg{i}",
                formatted=fmt,
                raw={},
                author=Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, i + 1, tzinfo=UTC)),
            )

        msgs = [_msg(i) for i in range(5)]
        result = adapter.paginate_messages(msgs, FetchOptions(limit=2, direction="backward"))
        assert len(result.messages) == 2
        # Should have next_cursor since there are more
        assert result.next_cursor is not None

    def test_forward_pagination(self):
        from datetime import UTC, datetime

        from chat_sdk.types import Author, FetchOptions, Message, MessageMetadata

        adapter = _make_adapter()

        fmt: Any = {"type": "root", "children": []}

        def _msg(i: int) -> Message:
            return Message(
                id=f"{CHAT_ID}:{i}",
                thread_id=THREAD_ID,
                text=f"msg{i}",
                formatted=fmt,
                raw={},
                author=Author(user_id="u", user_name="u", full_name="u", is_bot=False, is_me=False),
                metadata=MessageMetadata(date_sent=datetime(2024, 1, i + 1, tzinfo=UTC)),
            )

        msgs = [_msg(i) for i in range(5)]
        result = adapter.paginate_messages(msgs, FetchOptions(limit=2, direction="forward"))
        assert len(result.messages) == 2
        assert result.next_cursor is not None


# =============================================================================
# Tests -- cache operations
# =============================================================================


class TestCacheOperations:
    def test_cache_update_existing(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="v1")
        parsed = adapter.parse_message(msg)

        # Update same message
        msg2 = _make_telegram_message(text="v2")
        parsed2 = adapter.parse_telegram_message(msg2, THREAD_ID)
        adapter.cache_message(parsed2)

        found = adapter.find_cached_message(parsed.id)
        assert found is not None
        assert found.text == "v2"

    def test_delete_cached_message(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="to delete")
        parsed = adapter.parse_message(msg)

        adapter.delete_cached_message(parsed.id)
        assert adapter.find_cached_message(parsed.id) is None

    def test_delete_last_message_removes_thread(self):
        adapter = _make_adapter()
        _init_adapter(adapter)

        msg = _make_telegram_message(text="only one")
        parsed = adapter.parse_message(msg)

        adapter.delete_cached_message(parsed.id)
        assert THREAD_ID not in adapter._message_cache

    def test_find_cached_message_not_found(self):
        adapter = _make_adapter()
        assert adapter.find_cached_message("nonexistent") is None


# =============================================================================
# Tests -- disconnect
# =============================================================================


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_when_not_polling(self):
        adapter = _make_adapter()
        _init_adapter(adapter)
        # Disconnect completes without raising when not in polling mode
        await adapter.disconnect()


# =============================================================================
# Tests -- chat_display_name
# =============================================================================


class TestChatDisplayName:
    def test_title(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "group", "title": "My Group"}) == "My Group"

    def test_private_name(self):
        adapter = _make_adapter()
        assert (
            adapter.chat_display_name({"id": 1, "type": "private", "first_name": "John", "last_name": "Doe"})
            == "John Doe"
        )

    def test_username_fallback(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "private", "username": "jdoe"}) == "jdoe"

    def test_none_fallback(self):
        adapter = _make_adapter()
        assert adapter.chat_display_name({"id": 1, "type": "private"}) is None
