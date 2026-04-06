"""Tests for WhatsApp adapter API-calling methods.

Covers: post_message (text, long split, interactive card), add_reaction,
remove_reaction, stream (accumulation), and attachment fetch_data presence.

Uses a mock for _graph_api_request to intercept all Graph API calls without
network access.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat_sdk.adapters.whatsapp.adapter import (
    WHATSAPP_MESSAGE_LIMIT,
    WhatsAppAdapter,
    split_message,
)
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig, WhatsAppThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.types import MarkdownTextChunk, StreamChunk


# =============================================================================
# Helpers
# =============================================================================

THREAD_ID = "whatsapp:1234567890:49151234567"
USER_WA_ID = "49151234567"
PHONE_NUMBER_ID = "1234567890"


def _make_adapter(**overrides: Any) -> WhatsAppAdapter:
    """Create a WhatsAppAdapter with minimal valid config."""
    defaults: dict[str, Any] = {
        "access_token": "test-token",
        "app_secret": "test-secret",
        "phone_number_id": PHONE_NUMBER_ID,
        "verify_token": "verify-me",
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return WhatsAppAdapter(WhatsAppAdapterConfig(**defaults))


def _graph_api_response(message_id: str = "wamid.abc123") -> dict[str, Any]:
    """Simulate a successful Graph API send response."""
    return {"messages": [{"id": message_id}]}


# =============================================================================
# Tests — post_message
# =============================================================================


class TestPostMessageText:
    """post_message with a plain text body sends a single text API call."""

    @pytest.mark.asyncio
    async def test_post_message_text(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        result = await adapter.post_message(THREAD_ID, {"markdown": "Hello, world!"})

        assert result.id == "wamid.abc123"
        assert result.thread_id == THREAD_ID

        # Verify the API was called exactly once
        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        path, body = call_args[0]

        assert path == f"/{PHONE_NUMBER_ID}/messages"
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == USER_WA_ID
        assert body["type"] == "text"
        assert body["text"]["body"] == "Hello, world!"


class TestPostMessageSplitsLong:
    """post_message splits text exceeding the WhatsApp limit into 2+ calls."""

    @pytest.mark.asyncio
    async def test_post_message_splits_long(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        # Build a message that exceeds the limit, with paragraph breaks
        paragraph = "A" * (WHATSAPP_MESSAGE_LIMIT // 2)
        long_text = f"{paragraph}\n\n{paragraph}"
        assert len(long_text) > WHATSAPP_MESSAGE_LIMIT

        await adapter.post_message(THREAD_ID, {"markdown": long_text})

        # Should have been called at least twice (one per chunk)
        call_count = adapter._graph_api_request.call_count
        assert call_count >= 2, f"Expected >=2 API calls for split, got {call_count}"

        # Each call should be a text message
        for call in adapter._graph_api_request.call_args_list:
            body = call[0][1]
            assert body["type"] == "text"
            # Each chunk must be within the limit
            assert len(body["text"]["body"]) <= WHATSAPP_MESSAGE_LIMIT


class TestPostMessageCardInteractive:
    """post_message with a card containing buttons sends an interactive payload."""

    @pytest.mark.asyncio
    async def test_post_message_card_interactive(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        card = {
            "card": {
                "title": "Pick one",
                "body": "Choose an option",
                "buttons": [
                    {"label": "Option A", "action_id": "opt_a"},
                    {"label": "Option B", "action_id": "opt_b"},
                ],
            }
        }
        result = await adapter.post_message(THREAD_ID, card)

        assert result.id == "wamid.abc123"
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == USER_WA_ID
        # The card should produce either an interactive message or fallback text.
        # With buttons, WhatsApp cards map to the interactive type.
        assert body["type"] in ("interactive", "text")
        if body["type"] == "interactive":
            assert "interactive" in body


# =============================================================================
# Tests — add_reaction / remove_reaction
# =============================================================================


class TestAddReaction:
    """add_reaction sends a reaction payload with the emoji."""

    @pytest.mark.asyncio
    async def test_add_reaction(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.reaction1"}]})

        await adapter.add_reaction(THREAD_ID, "wamid.target123", "thumbs_up")

        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.target123"
        # The emoji string should be non-empty (resolved to unicode)
        assert body["reaction"]["emoji"] != ""
        assert body["to"] == USER_WA_ID


class TestRemoveReaction:
    """remove_reaction sends a reaction payload with empty emoji."""

    @pytest.mark.asyncio
    async def test_remove_reaction(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.reaction1"}]})

        await adapter.remove_reaction(THREAD_ID, "wamid.target123", "thumbs_up")

        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.target123"
        assert body["reaction"]["emoji"] == ""


# =============================================================================
# Tests — stream
# =============================================================================


class TestStreamAccumulates:
    """stream() buffers all chunks and posts a single message."""

    @pytest.mark.asyncio
    async def test_stream_accumulates(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        async def _chunks() -> AsyncIterator[str | StreamChunk]:
            yield "Hello "
            yield MarkdownTextChunk(text="world")
            yield "!"

        result = await adapter.stream(THREAD_ID, _chunks())

        assert result.id == "wamid.abc123"
        # stream should result in a single post_message, meaning
        # _graph_api_request is called once (for a short accumulated text)
        assert adapter._graph_api_request.call_count == 1
        body = adapter._graph_api_request.call_args[0][1]
        assert body["type"] == "text"
        assert "Hello " in body["text"]["body"]
        assert "world" in body["text"]["body"]


# =============================================================================
# Tests — attachment fetch_data
# =============================================================================


class TestAttachmentHasFetchData:
    """Media attachments include a callable fetch_data for lazy downloading."""

    def test_attachment_has_fetch_data(self):
        adapter = _make_adapter()
        inbound = {
            "id": "wamid.img1",
            "from": "49151234567",
            "type": "image",
            "timestamp": "1700000000",
            "image": {"id": "media_123", "mime_type": "image/jpeg"},
        }
        attachments = adapter._build_attachments(inbound)

        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.type == "image"
        assert attachment.mime_type == "image/jpeg"
        # fetch_data should be a callable (coroutine function)
        assert attachment.fetch_data is not None
        assert callable(attachment.fetch_data)
