"""Tests for Telegram adapter API-calling methods.

Covers: post_message (text, card, parse mode), edit_message, delete_message,
add_reaction, remove_reaction, start_typing, callback query dispatch,
reaction update dispatch, error mapping (401, 429, 403),
fetch_thread, fetch_channel_info.

Mocks telegram_fetch to intercept all Bot API calls without network access.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.telegram.adapter import TelegramAdapter
from chat_sdk.adapters.telegram.types import (
    TelegramAdapterConfig,
    TelegramThreadId,
)
from chat_sdk.shared.errors import (
    AdapterRateLimitError,
    AuthenticationError,
    PermissionError as AdapterPermissionError,
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

        result = await adapter.edit_message(
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
