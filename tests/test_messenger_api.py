"""Tests for the Messenger adapter — Graph API send & stream paths.

Covers ``post_message`` (text / markdown / AST / generic-template card /
button-template card / text fallback), ``stream`` accumulation,
``start_typing``, ``edit_message`` / ``delete_message`` /
``add_reaction`` / ``remove_reaction`` unsupported paths, message
truncation, message caching after send, and Graph API error mapping
(rate limit / auth / not-found / network).

Stubs the adapter's private ``_graph_api_fetch`` helper so we never hit
the network, mirroring the WhatsApp ``test_whatsapp_api.py`` pattern.

Pairs with ``tests/test_messenger_webhook.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.messenger.adapter import (
    MESSENGER_MESSAGE_LIMIT,
    MessengerAdapter,
)
from chat_sdk.adapters.messenger.types import MessengerAdapterConfig, MessengerThreadId
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import (
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.types import MarkdownTextChunk, StreamChunk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

THREAD_ID = "messenger:USER_123"
RECIPIENT_ID = "USER_123"


def _make_adapter(**overrides: Any) -> MessengerAdapter:
    defaults: dict[str, Any] = {
        "app_secret": "test-app-secret",
        "page_access_token": "test-page-token",
        "verify_token": "test-verify-token",
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return MessengerAdapter(MessengerAdapterConfig(**defaults))


def _send_api_response(message_id: str = "mid.sent") -> dict[str, Any]:
    return {"recipient_id": RECIPIENT_ID, "message_id": message_id}


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestInitialize:
    """``initialize`` fetches Page identity, but failure is non-fatal."""

    @pytest.mark.asyncio
    async def test_initialize_sets_bot_id_and_name(self) -> None:
        adapter = _make_adapter()
        # Drop explicit user_name so /me name is used.
        adapter._has_explicit_user_name = False
        adapter._user_name = "bot"
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "PAGE_456", "name": "My Cool Page"})

        chat = MagicMock()
        chat.get_user_name.return_value = "TestBot"
        await adapter.initialize(chat)

        assert adapter.bot_user_id == "PAGE_456"
        assert adapter.user_name == "My Cool Page"

    @pytest.mark.asyncio
    async def test_initialize_preserves_explicit_user_name(self) -> None:
        adapter = _make_adapter(user_name="CustomBot")
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "PAGE_456", "name": "Page Name"})

        chat = MagicMock()
        chat.get_user_name.return_value = "ChatName"
        await adapter.initialize(chat)

        # Explicit user_name wins over both chat.get_user_name() and /me name.
        assert adapter.user_name == "CustomBot"

    @pytest.mark.asyncio
    async def test_initialize_uses_chat_user_name_when_no_explicit(self) -> None:
        adapter = _make_adapter()
        adapter._has_explicit_user_name = False
        adapter._user_name = "bot"
        # /me fails — falls back to chat.get_user_name().
        adapter._graph_api_fetch = AsyncMock(side_effect=RuntimeError("API down"))

        chat = MagicMock()
        chat.get_user_name.return_value = "TestBot"
        await adapter.initialize(chat)

        assert adapter.bot_user_id is None
        assert adapter.user_name == "TestBot"

    @pytest.mark.asyncio
    async def test_initialize_continues_when_me_fails(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(side_effect=RuntimeError("API down"))
        chat = MagicMock()
        chat.get_user_name.return_value = "Bot"
        # Should not raise.
        await adapter.initialize(chat)
        assert adapter.bot_user_id is None


# ---------------------------------------------------------------------------
# post_message — text
# ---------------------------------------------------------------------------


class TestPostMessageText:
    """``post_message`` sends a single text payload via the Send API."""

    @pytest.mark.asyncio
    async def test_post_plain_string(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.s"))
        result = await adapter.post_message(THREAD_ID, "Hello!")

        assert result.id == "mid.s"
        assert result.thread_id == THREAD_ID
        endpoint, kwargs = adapter._graph_api_fetch.call_args.args, adapter._graph_api_fetch.call_args.kwargs
        assert endpoint[0] == "me/messages"
        assert kwargs["method"] == "POST"
        body = kwargs["body"]
        assert body["recipient"] == {"id": RECIPIENT_ID}
        assert body["message"]["text"] == "Hello!"
        assert body["messaging_type"] == "RESPONSE"

    @pytest.mark.asyncio
    async def test_post_markdown(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.md"))
        await adapter.post_message(THREAD_ID, {"markdown": "**bold** and *italic*"})
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        # Messenger doesn't render markdown — text contains the source.
        assert "bold" in body["message"]["text"]
        assert "italic" in body["message"]["text"]

    @pytest.mark.asyncio
    async def test_post_ast(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.ast"))
        await adapter.post_message(
            THREAD_ID,
            {
                "ast": {
                    "type": "root",
                    "children": [
                        {"type": "paragraph", "children": [{"type": "text", "value": "ast content"}]},
                    ],
                }
            },
        )
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert "ast content" in body["message"]["text"]

    @pytest.mark.asyncio
    async def test_rejects_empty_message(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response())
        with pytest.raises(ValidationError):
            await adapter.post_message(THREAD_ID, "  ")
        adapter._graph_api_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_exact_2000_chars_unchanged(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response())
        text = "x" * MESSENGER_MESSAGE_LIMIT
        await adapter.post_message(THREAD_ID, text)
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert body["message"]["text"] == text
        assert len(body["message"]["text"]) == MESSENGER_MESSAGE_LIMIT

    @pytest.mark.asyncio
    async def test_2001_chars_truncated_with_ellipsis(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response())
        text = "y" * (MESSENGER_MESSAGE_LIMIT + 1)
        await adapter.post_message(THREAD_ID, text)
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert len(body["message"]["text"]) == MESSENGER_MESSAGE_LIMIT
        assert body["message"]["text"].endswith("...")

    @pytest.mark.asyncio
    async def test_long_text_truncated(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response())
        await adapter.post_message(THREAD_ID, "a" * 3000)
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert len(body["message"]["text"]) <= MESSENGER_MESSAGE_LIMIT
        assert body["message"]["text"].endswith("...")

    @pytest.mark.asyncio
    async def test_sent_message_is_cached(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.cached"))
        await adapter.post_message(THREAD_ID, "cached msg")
        fetched = await adapter.fetch_message(THREAD_ID, "mid.cached")
        assert fetched is not None
        assert "cached msg" in fetched.text

    @pytest.mark.asyncio
    async def test_resolves_bare_psid(self) -> None:
        """A non-prefixed value is treated as a raw PSID."""
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.raw"))
        result = await adapter.post_message("USER_123", "hi")
        assert result.id == "mid.raw"


# ---------------------------------------------------------------------------
# post_message — card templates
# ---------------------------------------------------------------------------


class TestPostMessageCard:
    """``post_message`` routes cards to generic / button templates or text."""

    @pytest.mark.asyncio
    async def test_generic_template_for_card_with_title(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.tmpl"))
        await adapter.post_message(
            THREAD_ID,
            {
                "type": "card",
                "title": "Welcome",
                "children": [
                    {"type": "text", "content": "Hello!"},
                    {
                        "type": "actions",
                        "children": [
                            {"type": "button", "id": "start", "label": "Start"},
                            {"type": "button", "id": "help", "label": "Help"},
                        ],
                    },
                ],
            },
        )
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        attachment = body["message"]["attachment"]
        assert attachment["type"] == "template"
        assert attachment["payload"]["template_type"] == "generic"
        assert len(attachment["payload"]["elements"]) == 1
        assert attachment["payload"]["elements"][0]["title"] == "Welcome"
        assert len(attachment["payload"]["elements"][0]["buttons"]) == 2

    @pytest.mark.asyncio
    async def test_button_template_for_titleless_card(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.btn"))
        await adapter.post_message(
            THREAD_ID,
            {
                "type": "card",
                "children": [
                    {"type": "text", "content": "Please choose:"},
                    {
                        "type": "actions",
                        "children": [{"type": "button", "id": "opt1", "label": "Option 1"}],
                    },
                ],
            },
        )
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        payload = body["message"]["attachment"]["payload"]
        assert payload["template_type"] == "button"
        assert payload["text"] == "Please choose:"

    @pytest.mark.asyncio
    async def test_text_fallback_for_unsupported_card(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.txt"))
        await adapter.post_message(
            THREAD_ID,
            {
                "type": "card",
                "title": "With Table",
                "children": [
                    {"type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]},
                ],
            },
        )
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        # Falls back to text — no attachment field.
        assert "attachment" not in body["message"]
        assert "With Table" in body["message"]["text"]


# ---------------------------------------------------------------------------
# stream
# ---------------------------------------------------------------------------


class TestStream:
    """``stream`` buffers chunks and posts once."""

    @pytest.mark.asyncio
    async def test_buffers_str_chunks(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.s"))

        async def _chunks() -> AsyncIterator[str | StreamChunk]:
            yield "Hello"
            yield " "
            yield "world"

        result = await adapter.stream(THREAD_ID, _chunks())
        assert result.id == "mid.s"
        assert adapter._graph_api_fetch.call_count == 1
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert body["message"]["text"] == "Hello world"

    @pytest.mark.asyncio
    async def test_buffers_markdown_text_chunks(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response())

        async def _chunks() -> AsyncIterator[str | StreamChunk]:
            yield MarkdownTextChunk(text="Structured ")
            yield "plain "
            yield MarkdownTextChunk(text="content")

        await adapter.stream(THREAD_ID, _chunks())
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert body["message"]["text"] == "Structured plain content"

    @pytest.mark.asyncio
    async def test_ignores_thinking_chunk(self) -> None:
        """A ``ThinkingChunk`` is skipped (streaming-only reasoning, not message
        content); the buffered post excludes it and does not crash."""
        from chat_sdk.types import ThinkingChunk

        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=_send_api_response("mid.t"))

        async def _chunks() -> AsyncIterator[str | StreamChunk]:
            yield ThinkingChunk(content="reasoning")
            yield "Hello "
            yield ThinkingChunk(content="more")
            yield "world"

        result = await adapter.stream(THREAD_ID, _chunks())
        assert result.id == "mid.t"
        body = adapter._graph_api_fetch.call_args.kwargs["body"]
        assert body["message"]["text"] == "Hello world"


# ---------------------------------------------------------------------------
# Typing indicator
# ---------------------------------------------------------------------------


class TestStartTyping:
    @pytest.mark.asyncio
    async def test_sends_typing_on(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value={})
        await adapter.start_typing(THREAD_ID)
        kwargs = adapter._graph_api_fetch.call_args.kwargs
        assert kwargs["body"]["sender_action"] == "typing_on"
        assert kwargs["body"]["recipient"]["id"] == RECIPIENT_ID


# ---------------------------------------------------------------------------
# Unsupported operations
# ---------------------------------------------------------------------------


class TestUnsupportedOperations:
    """Operations that Messenger doesn't support — must raise ValidationError."""

    @pytest.mark.asyncio
    async def test_edit_message_raises(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            await adapter.edit_message(THREAD_ID, "mid.1", "new text")

    @pytest.mark.asyncio
    async def test_delete_message_raises(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            await adapter.delete_message(THREAD_ID, "mid.1")

    @pytest.mark.asyncio
    async def test_add_reaction_raises(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            await adapter.add_reaction(THREAD_ID, "mid.1", "thumbsup")

    @pytest.mark.asyncio
    async def test_remove_reaction_raises(self) -> None:
        adapter = _make_adapter()
        with pytest.raises(ValidationError):
            await adapter.remove_reaction(THREAD_ID, "mid.1", "thumbsup")


# ---------------------------------------------------------------------------
# Thread / channel info
# ---------------------------------------------------------------------------


class TestFetchThreadAndChannel:
    """``fetch_thread`` / ``fetch_channel_info`` use the Graph user profile."""

    @pytest.mark.asyncio
    async def test_thread_with_full_name(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "USER_123", "first_name": "John", "last_name": "Doe"})
        thread = await adapter.fetch_thread(THREAD_ID)
        assert thread.channel_name == "John Doe"
        assert thread.is_dm is True

    @pytest.mark.asyncio
    async def test_channel_info_with_full_name(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(
            return_value={"id": "USER_123", "first_name": "Jane", "last_name": "Smith"}
        )
        info = await adapter.fetch_channel_info(RECIPIENT_ID)
        assert info.name == "Jane Smith"
        assert info.is_dm is True

    @pytest.mark.asyncio
    async def test_falls_back_to_user_id_on_profile_error(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(side_effect=RuntimeError("err"))
        info = await adapter.fetch_channel_info(RECIPIENT_ID)
        assert info.name == RECIPIENT_ID

    @pytest.mark.asyncio
    async def test_first_name_only(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "USER_123", "first_name": "Alice"})
        info = await adapter.fetch_thread(THREAD_ID)
        assert info.channel_name == "Alice"

    @pytest.mark.asyncio
    async def test_last_name_only(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "USER_123", "last_name": "Smith"})
        info = await adapter.fetch_thread(THREAD_ID)
        assert info.channel_name == "Smith"

    @pytest.mark.asyncio
    async def test_caches_user_profile(self) -> None:
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value={"id": "USER_123", "first_name": "John"})
        await adapter.fetch_thread(THREAD_ID)
        await adapter.fetch_thread(THREAD_ID)
        # Second call hits the cache — no extra API call.
        assert adapter._graph_api_fetch.call_count == 1

    @pytest.mark.asyncio
    async def test_empty_dict_cache_entry_is_a_hit_not_a_miss(self) -> None:
        """Regression: a cached ``{}`` profile must NOT trigger a re-fetch.

        The old ``if cached:`` treated ``{}`` (falsy) as a miss and called the
        Graph API every time. The fix ``if cached is not None`` honors the
        cache entry regardless of dict contents. Without the fix this test
        would observe ``call_count == 1`` (the re-fetch).
        """
        adapter = _make_adapter()
        adapter._user_profile_cache[RECIPIENT_ID] = {}  # pre-populated empty hit
        adapter._graph_api_fetch = AsyncMock(return_value={"id": RECIPIENT_ID, "first_name": "Should-Not-Be-Called"})

        profile = await adapter._fetch_user_profile(RECIPIENT_ID)

        # Cache hit returns the empty dict as-is, no Graph API roundtrip.
        assert profile == {}
        assert adapter._graph_api_fetch.call_count == 0

    @pytest.mark.asyncio
    async def test_non_dict_profile_response_falls_back(self) -> None:
        """A successful Graph API call that returns a non-mapping must not poison the cache.

        ``_graph_api_fetch`` is typed loosely and could in principle yield
        ``None`` or a list (e.g. an API shape change, a stubbed test, a
        proxy returning an array). Without the ``isinstance(profile, dict)``
        guard, ``_profile_display_name`` then calls ``.get`` on whatever
        came back and raises ``AttributeError``, and the next call hits the
        same poisoned cache entry. Verify (a) the minimal ``{"id": uid}``
        fallback is returned, and (b) the cache stays empty.
        """
        adapter = _make_adapter()
        adapter._graph_api_fetch = AsyncMock(return_value=None)
        info = await adapter.fetch_thread(THREAD_ID)
        assert info.channel_name == RECIPIENT_ID
        assert RECIPIENT_ID not in adapter._user_profile_cache

        # Same again for a list, to pin the dict-only contract.
        adapter._graph_api_fetch = AsyncMock(return_value=[{"id": RECIPIENT_ID}])
        info = await adapter.fetch_thread(THREAD_ID)
        assert info.channel_name == RECIPIENT_ID
        assert RECIPIENT_ID not in adapter._user_profile_cache


# ---------------------------------------------------------------------------
# Message fetching / pagination
# ---------------------------------------------------------------------------


def _seed_messages(adapter: MessengerAdapter, count: int) -> None:
    for i in range(1, count + 1):
        adapter.parse_message(
            {
                "sender": {"id": "USER_123"},
                "recipient": {"id": "PAGE_456"},
                "timestamp": 1735689600000 + i * 1000,
                "message": {"mid": f"mid.{i}", "text": f"message {i}"},
            }
        )


class TestFetchMessages:
    """Local message cache backs ``fetch_messages`` / ``fetch_message``."""

    @pytest.mark.asyncio
    async def test_empty_unknown_thread(self) -> None:
        adapter = _make_adapter()
        result = await adapter.fetch_messages("messenger:UNKNOWN")
        assert result.messages == []

    @pytest.mark.asyncio
    async def test_backward_default(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=3))
        assert [m.id for m in result.messages] == ["mid.3", "mid.4", "mid.5"]
        assert result.next_cursor == "mid.3"

    @pytest.mark.asyncio
    async def test_backward_with_cursor(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(
            THREAD_ID,
            FetchOptions(limit=2, cursor="mid.3", direction="backward"),
        )
        assert [m.id for m in result.messages] == ["mid.1", "mid.2"]

    @pytest.mark.asyncio
    async def test_forward(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(
            THREAD_ID,
            FetchOptions(limit=2, direction="forward"),
        )
        assert [m.id for m in result.messages] == ["mid.1", "mid.2"]
        assert result.next_cursor == "mid.2"

    @pytest.mark.asyncio
    async def test_forward_with_cursor(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(
            THREAD_ID,
            FetchOptions(limit=2, cursor="mid.2", direction="forward"),
        )
        assert [m.id for m in result.messages] == ["mid.3", "mid.4"]

    @pytest.mark.asyncio
    async def test_no_next_cursor_when_exhausted(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=100))
        assert len(result.messages) == 5
        assert result.next_cursor is None

    @pytest.mark.asyncio
    async def test_clamps_negative_limit(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=-10))
        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_clamps_high_limit(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=500))
        assert len(result.messages) == 5

    @pytest.mark.asyncio
    async def test_explicit_zero_limit_is_not_swallowed_to_default(self) -> None:
        """Regression: ``FetchOptions(limit=0)`` must not silently become 50.

        The old ``limit = options.limit or 50`` treated ``0`` as falsy and
        substituted the default page size — a caller asking for zero messages
        got fifty. Switching to ``is not None`` preserves the explicit value
        (then ``max(1, ...)`` clamps to 1, matching ``limit=-N`` behavior).
        Without the fix, with 5 messages seeded, this test would observe
        ``len(result.messages) == 5`` (all of them, since 50 > 5).
        """
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 5)
        result = await adapter.fetch_messages(THREAD_ID, FetchOptions(limit=0))
        # New behavior: clamped to 1 via ``max(1, min(0, 100))``, NOT 50.
        assert len(result.messages) == 1

    @pytest.mark.asyncio
    async def test_unknown_cursor_backward(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 3)
        result = await adapter.fetch_messages(
            THREAD_ID,
            FetchOptions(cursor="mid.nonexistent", direction="backward", limit=2),
        )
        # Falls back to "from end".
        assert [m.id for m in result.messages] == ["mid.2", "mid.3"]

    @pytest.mark.asyncio
    async def test_unknown_cursor_forward(self) -> None:
        from chat_sdk.types import FetchOptions

        adapter = _make_adapter()
        _seed_messages(adapter, 3)
        result = await adapter.fetch_messages(
            THREAD_ID,
            FetchOptions(cursor="mid.nonexistent", direction="forward", limit=2),
        )
        # Falls back to "from start".
        assert [m.id for m in result.messages] == ["mid.1", "mid.2"]

    @pytest.mark.asyncio
    async def test_fetch_single_unknown_message_returns_none(self) -> None:
        adapter = _make_adapter()
        result = await adapter.fetch_message(THREAD_ID, "mid.nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_sort_by_timestamp_then_sequence(self) -> None:
        """Equal timestamps order by the ``:N`` suffix on the mid."""
        adapter = _make_adapter()
        adapter.parse_message(
            {
                "sender": {"id": "USER_123"},
                "recipient": {"id": "PAGE_456"},
                "timestamp": 1735689600000,
                "message": {"mid": "mid.abc:2", "text": "second"},
            }
        )
        adapter.parse_message(
            {
                "sender": {"id": "USER_123"},
                "recipient": {"id": "PAGE_456"},
                "timestamp": 1735689600000,
                "message": {"mid": "mid.abc:1", "text": "first"},
            }
        )
        result = await adapter.fetch_messages(THREAD_ID)
        assert result.messages[0].text == "first"
        assert result.messages[1].text == "second"

    @pytest.mark.asyncio
    async def test_reparsing_same_id_updates_cache(self) -> None:
        adapter = _make_adapter()
        event1 = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.dup", "text": "first"},
        }
        event2 = dict(event1)
        event2["message"] = {"mid": "mid.dup", "text": "updated"}
        adapter.parse_message(event1)
        updated = adapter.parse_message(event2)
        assert updated.text == "updated"


# ---------------------------------------------------------------------------
# parseMessage
# ---------------------------------------------------------------------------


class TestParseMessage:
    """``parse_message`` produces a normalized ``Message`` from raw events."""

    def test_basic_message(self) -> None:
        adapter = _make_adapter()
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.abc123", "text": "hello"},
        }
        parsed = adapter.parse_message(event)
        assert parsed.text == "hello"
        assert parsed.thread_id == "messenger:USER_123"
        assert parsed.id == "mid.abc123"

    def test_is_mention_true(self) -> None:
        adapter = _make_adapter()
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.x", "text": "hi"},
        }
        parsed = adapter.parse_message(event)
        # All inbound Messenger messages are 1:1 DMs — always a mention.
        assert parsed.is_mention is True

    def test_echo_marks_as_me_and_bot(self) -> None:
        adapter = _make_adapter()
        adapter._bot_user_id = "PAGE_456"
        event = {
            "sender": {"id": "PAGE_456"},
            "recipient": {"id": "USER_123"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.echo", "text": "bot says", "is_echo": True},
        }
        parsed = adapter.parse_message(event)
        assert parsed.author.is_me is True
        assert parsed.author.is_bot is True

    def test_echo_threads_by_recipient_psid(self) -> None:
        """Echo events flip sender/recipient: ``sender.id`` is the Page ID and
        ``recipient.id`` is the user's PSID. ``parse_message`` must thread the
        echo off the user PSID so it lands in the same conversation as the
        inbound user messages (matching ``_handle_echo``), not the page id.
        """
        adapter = _make_adapter()
        adapter._bot_user_id = "PAGE_456"
        event = {
            "sender": {"id": "PAGE_456"},
            "recipient": {"id": "USER_123"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.echo", "text": "bot says", "is_echo": True},
        }
        parsed = adapter.parse_message(event)
        # Threaded under the user's PSID, NOT the page id.
        assert parsed.thread_id == "messenger:USER_123"
        assert parsed.thread_id != "messenger:PAGE_456"
        # Must match what _handle_echo derives for the same event.
        expected = adapter.encode_thread_id(MessengerThreadId(recipient_id="USER_123"))
        assert parsed.thread_id == expected

    def test_non_echo_threads_by_sender_psid(self) -> None:
        """Non-echo inbound messages have ``sender.id`` == user PSID and must
        keep threading off the sender — guards against over-applying the echo
        fix to normal inbound events.
        """
        adapter = _make_adapter()
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "message": {"mid": "mid.abc", "text": "hi"},
        }
        parsed = adapter.parse_message(event)
        assert parsed.thread_id == "messenger:USER_123"

    def test_postback_uses_title_as_text(self) -> None:
        adapter = _make_adapter()
        event = {
            "sender": {"id": "USER_123"},
            "recipient": {"id": "PAGE_456"},
            "timestamp": 1735689600000,
            "postback": {"title": "Get Started", "payload": "START"},
        }
        parsed = adapter.parse_message(event)
        assert parsed.id == "event:1735689600000"
        assert parsed.text == "Get Started"

    def test_render_formatted_returns_text(self) -> None:
        adapter = _make_adapter()
        out = adapter.render_formatted(
            {
                "type": "root",
                "children": [
                    {"type": "paragraph", "children": [{"type": "text", "value": "hello world"}]},
                ],
            }
        )
        assert "hello world" in out


# ---------------------------------------------------------------------------
# Graph API error mapping
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Async-context-manager response stub for aiohttp-compatible callers."""

    def __init__(self, status: int, json_data: Any) -> None:
        self.status = status
        self._json_data = json_data

    async def json(self, content_type: Any = None) -> Any:
        return self._json_data

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    """Minimal aiohttp-like session stub. Records the last GET/POST call."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.closed = False
        self.last_method: str | None = None

    def get(self, url: str, **kwargs: Any) -> Any:
        self.last_method = "GET"
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def post(self, url: str, **kwargs: Any) -> Any:
        self.last_method = "POST"
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def _call_with_response(status: int, json_data: Any) -> None:
    adapter = _make_adapter()
    adapter._http_session = _FakeSession(_FakeResponse(status, json_data))
    await adapter.start_typing(THREAD_ID)


class TestGraphApiErrors:
    """``_graph_api_fetch`` maps Meta error codes to typed adapter errors."""

    @pytest.mark.asyncio
    async def test_rate_limit_on_429(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(429, {"error": {"message": "Rate limited"}}))
        with pytest.raises(AdapterRateLimitError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_rate_limit_on_code_4(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(400, {"error": {"message": "Too many calls", "code": 4}}))
        with pytest.raises(AdapterRateLimitError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_rate_limit_on_code_32(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(400, {"error": {"message": "Page rate limit", "code": 32}}))
        with pytest.raises(AdapterRateLimitError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_rate_limit_on_code_613(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(
            _FakeResponse(400, {"error": {"message": "Custom rate limit", "code": 613}})
        )
        with pytest.raises(AdapterRateLimitError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_auth_error_on_401(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(401, {"error": {"message": "Invalid token", "code": 190}}))
        with pytest.raises(AuthenticationError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_auth_error_on_code_190(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(400, {"error": {"message": "Token expired", "code": 190}}))
        with pytest.raises(AuthenticationError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_validation_on_403(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(
            _FakeResponse(403, {"error": {"message": "Permission denied", "code": 10}})
        )
        with pytest.raises(ValidationError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_validation_on_code_200(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(
            _FakeResponse(400, {"error": {"message": "Requires permission", "code": 200}})
        )
        with pytest.raises(ValidationError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_not_found_on_404(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(404, {"error": {"message": "Not found"}}))
        with pytest.raises(ResourceNotFoundError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_network_on_generic_5xx(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(500, {"error": {"message": "Internal error", "code": 2}}))
        with pytest.raises(NetworkError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_network_on_session_exception(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(RuntimeError("DNS failure"))
        with pytest.raises(NetworkError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_network_on_unparseable_response(self) -> None:
        class _BadResp(_FakeResponse):
            async def json(self, content_type: Any = None) -> Any:
                raise ValueError("not json")

        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_BadResp(200, None))
        with pytest.raises(NetworkError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_fallback_message_when_no_error_message(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(500, {"error": {"code": 999}}))
        with pytest.raises(NetworkError, match="Messenger API"):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_uses_status_as_code_when_missing(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(500, {"error": {"message": "Something failed"}}))
        with pytest.raises(NetworkError):
            await adapter.start_typing(THREAD_ID)

    @pytest.mark.asyncio
    async def test_no_error_object(self) -> None:
        adapter = _make_adapter()
        adapter._http_session = _FakeSession(_FakeResponse(500, {}))
        with pytest.raises(NetworkError):
            await adapter.start_typing(THREAD_ID)


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_closes_session(self) -> None:
        adapter = _make_adapter()
        session = _FakeSession(_FakeResponse(200, {}))
        adapter._http_session = session
        await adapter.disconnect()
        assert session.closed is True
        assert adapter._http_session is None

    @pytest.mark.asyncio
    async def test_disconnect_when_no_session(self) -> None:
        adapter = _make_adapter()
        # Should not raise.
        await adapter.disconnect()
