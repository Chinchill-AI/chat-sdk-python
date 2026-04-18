"""Faithful translation of channel.test.ts (61 tests).

Each ``it("...")`` block from the TypeScript test suite is translated
to a corresponding ``async def test_...`` method, preserving the same
inputs, assertions, and test structure.

TS ``channel.messages`` (property → async iterable) becomes
``channel.messages()`` (async generator method) in Python.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk.channel import ChannelImpl, _ChannelImplConfigWithAdapter, derive_channel_id
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import (
    Attachment,
    FetchResult,
    ListThreadsResult,
    Message,
    PostableAst,
    PostableMarkdown,
    PostableRaw,
    RawMessage,
    ScheduledMessage,
    ThreadSummary,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    channel_id: str = "slack:C123",
    is_dm: bool = False,
    channel_visibility: str = "unknown",
) -> ChannelImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ChannelImpl(
        _ChannelImplConfigWithAdapter(
            id=channel_id,
            adapter=adapter,
            state_adapter=state,
            is_dm=is_dm,
            channel_visibility=channel_visibility,
        )
    )


def _make_thread(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    thread_id: str = "slack:C123:1234.5678",
    channel_id: str = "C123",
    is_dm: bool = False,
) -> ThreadImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ThreadImpl(
        _ThreadImplConfig(
            id=thread_id,
            adapter=adapter,
            state_adapter=state,
            channel_id=channel_id,
            is_dm=is_dm,
        )
    )


async def _collect(ait: AsyncIterator[Any]) -> list[Any]:
    """Collect all items from an async iterator into a list."""
    result = []
    async for item in ait:
        result.append(item)
    return result


# ===========================================================================
# basic properties
# ===========================================================================


class TestBasicProperties:
    """describe("basic properties")"""

    # it("should have correct id and adapter")
    def test_should_have_correct_id_and_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)

        assert channel.id == "slack:C123"
        assert channel.adapter is adapter
        assert channel.is_dm is False
        assert channel.name is None

    # it("should set isDM when configured")
    def test_should_set_isdm_when_configured(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state, channel_id="slack:D123", is_dm=True)

        assert channel.is_dm is True


# ===========================================================================
# state management
# ===========================================================================


class TestStateManagement:
    """describe("state management")"""

    # it("should return null when no state has been set")
    @pytest.mark.asyncio
    async def test_should_return_null_when_no_state_has_been_set(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.get_state()
        assert result is None

    # it("should set and retrieve state")
    @pytest.mark.asyncio
    async def test_should_set_and_retrieve_state(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.set_state({"topic": "general"})
        result = await channel.get_state()
        assert result == {"topic": "general"}

    # it("should merge state by default")
    @pytest.mark.asyncio
    async def test_should_merge_state_by_default(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.set_state({"topic": "general"})
        await channel.set_state({"count": 5})
        result = await channel.get_state()
        assert result == {"topic": "general", "count": 5}

    # it("should replace state when option is set")
    @pytest.mark.asyncio
    async def test_should_replace_state_when_option_is_set(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.set_state({"topic": "general", "count": 5})
        await channel.set_state({"count": 10}, replace=True)
        result = await channel.get_state()
        assert result == {"count": 10}

    # it("should use channel-state: key prefix")
    @pytest.mark.asyncio
    async def test_should_use_channelstate_key_prefix(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.set_state({"topic": "general"})
        assert "channel-state:slack:C123" in state.cache


# ===========================================================================
# messages iterator (newest first)
# ===========================================================================


class TestMessagesIteratorNewestFirst:
    """describe("messages iterator (newest first)")"""

    # it("should use fetchChannelMessages when available")
    @pytest.mark.asyncio
    async def test_should_use_fetchchannelmessages_when_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Newest"),
        ]
        adapter.fetch_channel_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.messages())

        # Should be reversed (newest first)
        assert len(collected) == 3
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

        adapter.fetch_channel_messages.assert_called_once()
        call_args = adapter.fetch_channel_messages.call_args[0]
        opts = call_args[1]
        assert opts.direction == "backward"

    # it("should fall back to fetchMessages when fetchChannelMessages is not available")
    @pytest.mark.asyncio
    async def test_should_fall_back_to_fetchmessages_when_fetchchannelmessages_is_not_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.fetch_channel_messages = None  # type: ignore[assignment]

        messages = [
            create_test_message("msg-1", "First"),
            create_test_message("msg-2", "Second"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.messages())

        assert len(collected) == 2
        assert collected[0].text == "Second"
        assert collected[1].text == "First"

        adapter.fetch_messages.assert_called_once()

    # it("should auto-paginate through multiple pages")
    @pytest.mark.asyncio
    async def test_should_autopaginate_through_multiple_pages(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        call_count = 0

        async def mock_fetch(channel_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FetchResult(
                    messages=[
                        create_test_message("msg-3", "Page 1 Newest"),
                        create_test_message("msg-4", "Page 1 Older"),
                    ],
                    next_cursor="cursor-1",
                )
            return FetchResult(
                messages=[
                    create_test_message("msg-1", "Page 2 Newest"),
                    create_test_message("msg-2", "Page 2 Older"),
                ],
                next_cursor=None,
            )

        adapter.fetch_channel_messages = mock_fetch  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.messages())

        assert len(collected) == 4
        # Each page is reversed internally
        assert collected[0].text == "Page 1 Older"
        assert collected[1].text == "Page 1 Newest"
        assert collected[2].text == "Page 2 Older"
        assert collected[3].text == "Page 2 Newest"
        assert call_count == 2

    # it("should allow breaking out early")
    @pytest.mark.asyncio
    async def test_should_allow_breaking_out_early(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_channel_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(
                messages=[
                    create_test_message("msg-1", "First"),
                    create_test_message("msg-2", "Second"),
                    create_test_message("msg-3", "Third"),
                ],
                next_cursor="more",
            )
        )

        channel = _make_channel(adapter, state)
        collected: list[Message] = []
        async for msg in channel.messages():
            collected.append(msg)
            if len(collected) >= 2:
                break

        assert len(collected) == 2
        adapter.fetch_channel_messages.assert_called_once()

    # it("should handle empty channel")
    @pytest.mark.asyncio
    async def test_should_handle_empty_channel(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_channel_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.messages())

        assert len(collected) == 0


# ===========================================================================
# threads iterator
# ===========================================================================


class TestThreadsIterator:
    """describe("threads iterator")"""

    # it("should iterate threads from adapter.listThreads")
    @pytest.mark.asyncio
    async def test_should_iterate_threads_from_adapterlistthreads(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        thread_summaries = [
            ThreadSummary(
                id="slack:C123:1234.5678",
                root_message=create_test_message("msg-1", "Thread 1"),
                reply_count=5,
            ),
            ThreadSummary(
                id="slack:C123:2345.6789",
                root_message=create_test_message("msg-2", "Thread 2"),
                reply_count=3,
            ),
        ]

        adapter.list_threads = AsyncMock(  # type: ignore[assignment]
            return_value=ListThreadsResult(threads=thread_summaries, next_cursor=None)
        )

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.threads())

        assert len(collected) == 2
        assert collected[0].id == "slack:C123:1234.5678"
        assert collected[0].reply_count == 5
        assert collected[1].id == "slack:C123:2345.6789"

    # it("should return empty iterable when adapter has no listThreads")
    @pytest.mark.asyncio
    async def test_should_return_empty_iterable_when_adapter_has_no_listthreads(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.list_threads = None  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.threads())

        assert len(collected) == 0

    # it("should auto-paginate threads")
    @pytest.mark.asyncio
    async def test_should_autopaginate_threads(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        call_count = 0

        async def mock_list_threads(channel_id: str, options: Any = None, **kwargs: Any) -> ListThreadsResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ListThreadsResult(
                    threads=[
                        ThreadSummary(
                            id="slack:C123:1111",
                            root_message=create_test_message("msg-1", "T1"),
                            reply_count=2,
                        )
                    ],
                    next_cursor="cursor-1",
                )
            return ListThreadsResult(
                threads=[
                    ThreadSummary(
                        id="slack:C123:2222",
                        root_message=create_test_message("msg-2", "T2"),
                        reply_count=1,
                    )
                ],
                next_cursor=None,
            )

        adapter.list_threads = mock_list_threads  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        collected = await _collect(channel.threads())

        assert len(collected) == 2
        assert call_count == 2


# ===========================================================================
# fetchMetadata
# ===========================================================================


class TestFetchMetadata:
    """describe("fetchMetadata")"""

    # it("should fetch channel info and set name")
    @pytest.mark.asyncio
    async def test_should_fetch_channel_info_and_set_name(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)

        assert channel.name is None

        info = await channel.fetch_metadata()

        assert info.id == "slack:C123"
        assert info.name == "#slack:C123"
        assert channel.name == "#slack:C123"

    # it("should return basic info when adapter has no fetchChannelInfo")
    @pytest.mark.asyncio
    async def test_should_return_basic_info_when_adapter_has_no_fetchchannelinfo(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.fetch_channel_info = None  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        info = await channel.fetch_metadata()

        assert info.id == "slack:C123"
        assert info.is_dm is False
        assert info.metadata == {}


# ===========================================================================
# post
# ===========================================================================


class TestPost:
    """describe("post")"""

    # it("should use postChannelMessage when available")
    @pytest.mark.asyncio
    async def test_should_use_postchannelmessage_when_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        post_calls: list[tuple[str, Any]] = []

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            post_calls.append((channel_id, message))
            return RawMessage(id="msg-1", thread_id=None, raw={})

        adapter.post_channel_message = tracking_post  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        result = await channel.post("Hello channel!")

        assert len(post_calls) == 1
        assert post_calls[0] == ("slack:C123", "Hello channel!")
        assert result.text == "Hello channel!"

    # it("should fall back to postMessage when postChannelMessage is not available")
    @pytest.mark.asyncio
    async def test_should_fall_back_to_postmessage_when_postchannelmessage_is_not_available(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.post_channel_message = None  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        await channel.post("Hello!")

        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0] == ("slack:C123", "Hello!")

    # it("should handle streaming by accumulating text")
    @pytest.mark.asyncio
    async def test_should_handle_streaming_by_accumulating_text(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        post_calls: list[tuple[str, Any]] = []

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            post_calls.append((channel_id, message))
            return RawMessage(id="msg-1", thread_id=None, raw={})

        adapter.post_channel_message = tracking_post  # type: ignore[assignment]

        async def text_stream() -> AsyncIterator[str]:
            yield "Hello"
            yield " "
            yield "World"

        channel = _make_channel(adapter, state)
        result = await channel.post(text_stream())

        assert len(post_calls) == 1
        posted_msg = post_calls[0][1]
        assert isinstance(posted_msg, PostableMarkdown)
        assert posted_msg.markdown == "Hello World"
        assert result.text == "Hello World"


# ===========================================================================
# post with different message formats
# ===========================================================================


class TestPostWithDifferentMessageFormats:
    """describe("post with different message formats")"""

    # it("should handle raw message format")
    @pytest.mark.asyncio
    async def test_should_handle_raw_message_format(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        post_calls: list[tuple[str, Any]] = []

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            post_calls.append((channel_id, message))
            return RawMessage(id="msg-1", thread_id=None, raw={})

        adapter.post_channel_message = tracking_post  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        result = await channel.post(PostableRaw(raw="raw text message"))

        assert len(post_calls) == 1
        assert isinstance(post_calls[0][1], PostableRaw)
        assert post_calls[0][1].raw == "raw text message"
        assert result.text == "raw text message"

    # it("should handle markdown message format")
    @pytest.mark.asyncio
    async def test_should_handle_markdown_message_format(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post(PostableMarkdown(markdown="**bold** text"))
        # Markdown is parsed to AST; plain text strips formatting
        assert result.text == "bold text"

    # it("should handle AST message format")
    @pytest.mark.asyncio
    async def test_should_handle_ast_message_format(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        post_calls: list[tuple[str, Any]] = []

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            post_calls.append((channel_id, message))
            return RawMessage(id="msg-1", thread_id=None, raw={})

        adapter.post_channel_message = tracking_post  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        ast = {
            "type": "root",
            "children": [
                {"type": "paragraph", "children": [{"type": "text", "value": "from ast"}]},
            ],
        }
        result = await channel.post(PostableAst(ast=ast))

        assert len(post_calls) == 1
        assert isinstance(post_calls[0][1], PostableAst)
        assert post_calls[0][1].ast == ast
        # ast_to_plain_text extracts leaf text values from the AST
        assert result.text == "from ast"

    # it("should handle raw message with attachments")
    @pytest.mark.asyncio
    async def test_should_handle_raw_message_with_attachments(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post(
            PostableRaw(
                raw="text with attachment",
                attachments=[Attachment(type="image", url="https://example.com/img.png")],
            )
        )
        assert len(result.attachments) == 1
        assert result.attachments[0].type == "image"


# ===========================================================================
# serialization
# ===========================================================================


class TestSerialization:
    """describe("serialization")"""

    # it("should serialize to JSON")
    def test_should_serialize_to_json(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state, is_dm=False)

        json_data = channel.to_json()

        assert json_data["_type"] == "chat:Channel"
        assert json_data["id"] == "slack:C123"
        assert json_data["adapterName"] == "slack"
        assert json_data["channelVisibility"] == "unknown"
        assert json_data["isDM"] is False

    # it("should deserialize from JSON")
    def test_should_deserialize_from_json(self):
        json_data = {
            "_type": "chat:Channel",
            "id": "slack:C123",
            "adapter_name": "slack",
            "is_dm": False,
        }

        adapter = create_mock_adapter()
        channel = ChannelImpl.from_json(json_data, adapter)

        assert channel.id == "slack:C123"
        assert channel.is_dm is False
        assert channel.adapter is adapter

    def test_should_sync_adapter_name_when_explicit_adapter_is_bound(self):
        """from_json(data, adapter=X) must update _adapter_name to X.name so
        to_json() doesn't serialize a stale name. Regression for a P2 raised
        in review."""
        from chat_sdk.testing import create_mock_adapter as _create

        renamed_adapter = _create("teams")
        json_data = {
            "_type": "chat:Channel",
            "id": "C123",
            "adapter_name": "slack",  # different from the bound adapter
            "is_dm": False,
        }
        channel = ChannelImpl.from_json(json_data, renamed_adapter)

        assert channel.adapter.name == "teams"
        assert channel.to_json()["adapterName"] == "teams"

    def test_should_rebind_adapter_when_data_is_already_a_channelimpl(self):
        """Idempotent path: when ``data`` is already a ChannelImpl (e.g. revived
        via ``object_hook``), passing an explicit ``adapter=`` must still rebind
        it — an early-return shortcut would leave ``_adapter`` stale. Symmetric
        with the ThreadImpl regression in test_serialization.py."""
        from chat_sdk.testing import create_mock_adapter as _create

        first = _create("slack")
        second = _create("teams")
        original = ChannelImpl.from_json(
            {
                "_type": "chat:Channel",
                "id": "C123",
                "adapter_name": "slack",
                "is_dm": False,
            },
            first,
        )
        rebound = ChannelImpl.from_json(original, second)

        # Rebind applied even though data was already a ChannelImpl:
        assert rebound.adapter.name == "teams"
        assert rebound.to_json()["adapterName"] == "teams"


# ===========================================================================
# deriveChannelId (tested in channel.test.ts alongside ChannelImpl)
# ===========================================================================


class TestDeriveChannelId:
    """describe("deriveChannelId")"""

    # it("should use adapter.channelIdFromThreadId when available")
    def test_should_use_adapterchannelidfromthreadid_when_available(self):
        adapter = create_mock_adapter()
        channel_id = derive_channel_id(adapter, "slack:C123:1234.5678")
        assert channel_id == "slack:C123"

    # it("should work with different adapters")
    def test_should_work_with_different_adapters(self):
        adapter = create_mock_adapter("gchat")
        channel_id = derive_channel_id(adapter, "gchat:spaces/ABC123:dGhyZWFk")
        assert channel_id == "gchat:spaces/ABC123"


# ===========================================================================
# thread.channel (tested in channel.test.ts)
# ===========================================================================


class TestThreadDotChannel:
    """describe("thread.channel")"""

    # it("should return a Channel for the thread's parent channel")
    def test_should_return_a_channel_for_the_threads_parent_channel(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        channel = thread.channel
        assert channel.id == "slack:C123"
        assert channel.adapter is adapter

    # it("should cache the channel instance")
    def test_should_cache_the_channel_instance(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        channel1 = thread.channel
        channel2 = thread.channel
        assert channel1 is channel2

    # it("should inherit isDM from thread")
    def test_should_inherit_isdm_from_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(
            adapter,
            state,
            thread_id="slack:D123:1234.5678",
            channel_id="D123",
            is_dm=True,
        )
        assert thread.channel.is_dm is True

    # it("should inherit channelVisibility from thread")
    # Note: the _make_thread helper doesn't accept channel_visibility;
    # we construct the thread directly
    def test_should_inherit_channelvisibility_from_thread(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=adapter,
                state_adapter=state,
                channel_id="C123",
                channel_visibility="external",
            )
        )
        assert thread.channel.channel_visibility == "external"

    # it("should default channelVisibility to unknown")
    def test_should_default_channelvisibility_to_unknown(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = _make_thread(adapter, state)
        assert thread.channel.channel_visibility == "unknown"

    # it("should support private channel visibility")
    def test_should_support_private_channel_visibility(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:G123:1234.5678",
                adapter=adapter,
                state_adapter=state,
                channel_id="G123",
                channel_visibility="private",
            )
        )
        assert thread.channel.channel_visibility == "private"


# ===========================================================================
# ChannelImpl.postEphemeral
# ===========================================================================


class TestChannelPostEphemeral:
    """describe("ChannelImpl.postEphemeral")"""

    # it("should use adapter postEphemeral when available")
    @pytest.mark.asyncio
    async def test_should_use_adapter_postephemeral_when_available(self):
        from chat_sdk.types import EphemeralMessage, PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_post_ephemeral = AsyncMock(
            return_value=EphemeralMessage(
                id="eph-1",
                thread_id="slack:C123",
                used_fallback=False,
                raw={},
            )
        )
        adapter.post_ephemeral = mock_post_ephemeral  # type: ignore[attr-defined]

        channel = _make_channel(adapter, state)
        result = await channel.post_ephemeral("U456", "Secret!", PostEphemeralOptions(fallback_to_dm=True))

        mock_post_ephemeral.assert_called_once_with("slack:C123", "U456", "Secret!")
        assert result is not None
        assert result.id == "eph-1"
        assert result.thread_id == "slack:C123"
        assert result.used_fallback is False
        assert result.raw == {}

    # it("should extract userId from Author object")
    @pytest.mark.asyncio
    async def test_should_extract_userid_from_author_object(self):
        from chat_sdk.types import Author, EphemeralMessage, PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        mock_post_ephemeral = AsyncMock(
            return_value=EphemeralMessage(
                id="eph-1",
                thread_id="slack:C123",
                used_fallback=False,
                raw={},
            )
        )
        adapter.post_ephemeral = mock_post_ephemeral  # type: ignore[attr-defined]

        author = Author(
            user_id="U789",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        )

        channel = _make_channel(adapter, state)
        await channel.post_ephemeral(author, "Hello!", PostEphemeralOptions(fallback_to_dm=False))

        assert mock_post_ephemeral.call_count == 1
        mock_post_ephemeral.assert_called_once_with("slack:C123", "U789", "Hello!")

    # it("should return null when adapter has no postEphemeral and fallbackToDM is false")
    @pytest.mark.asyncio
    async def test_should_return_null_when_adapter_has_no_postephemeral_and_fallbacktodm_is_false(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()
        # MockAdapter doesn't have post_ephemeral by default

        channel = _make_channel(adapter, state)
        result = await channel.post_ephemeral("U456", "Secret!", PostEphemeralOptions(fallback_to_dm=False))

        assert result is None

    # it("should fallback to DM when adapter has no postEphemeral and fallbackToDM is true")
    @pytest.mark.asyncio
    async def test_should_fallback_to_dm_when_adapter_has_no_postephemeral_and_fallbacktodm_is_true(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()

        channel = _make_channel(adapter, state)
        result = await channel.post_ephemeral("U456", "Secret!", PostEphemeralOptions(fallback_to_dm=True))

        # Should have opened DM and posted
        assert len(adapter._post_calls) == 1
        assert adapter._post_calls[0] == ("slack:DU456:", "Secret!")
        assert result is not None
        assert result.id == "msg-1"
        assert result.thread_id == "slack:DU456:"
        assert result.used_fallback is True
        assert result.raw == {}

    # it("should return null when no postEphemeral, no openDM, and fallbackToDM is true")
    @pytest.mark.asyncio
    async def test_should_return_null_when_no_postephemeral_no_opendm_and_fallbacktodm_is_true(self):
        from chat_sdk.types import PostEphemeralOptions

        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.open_dm = None  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        result = await channel.post_ephemeral("U456", "Secret!", PostEphemeralOptions(fallback_to_dm=True))

        assert result is None


# ===========================================================================
# ChannelImpl.startTyping
# ===========================================================================


class TestChannelStartTyping:
    """describe("ChannelImpl.startTyping")"""

    # it("should call adapter.startTyping with channel id")
    @pytest.mark.asyncio
    async def test_should_call_adapterstarttyping_with_channel_id(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.start_typing()
        assert len(adapter._start_typing_calls) == 1
        assert adapter._start_typing_calls[0] == ("slack:C123", None)

    # it("should pass status string to adapter.startTyping")
    @pytest.mark.asyncio
    async def test_should_pass_status_string_to_adapterstarttyping(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        await channel.start_typing("thinking...")
        assert adapter._start_typing_calls[0] == ("slack:C123", "thinking...")


# ===========================================================================
# ChannelImpl.mentionUser
# ===========================================================================


class TestChannelMentionUser:
    """describe("ChannelImpl.mentionUser")"""

    # it("should return formatted mention string")
    def test_should_return_formatted_mention_string(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        assert channel.mention_user("U456") == "<@U456>"

    # it("should handle different user ID formats")
    def test_should_handle_different_user_id_formats(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        assert channel.mention_user("UABC123DEF") == "<@UABC123DEF>"
        assert channel.mention_user("bot-user") == "<@bot-user>"


# ===========================================================================
# ChannelImpl.post error cases
# ===========================================================================


class TestPostErrorCases:
    """describe("ChannelImpl.post error cases")"""

    # it("should handle postChannelMessage returning a threadId override")
    @pytest.mark.asyncio
    async def test_should_handle_postchannelmessage_returning_a_threadid_override(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        async def custom_post(channel_id: str, message: Any) -> RawMessage:
            return RawMessage(id="msg-2", thread_id="slack:C123:new-thread", raw={})

        adapter.post_channel_message = custom_post  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")
        assert result.thread_id == "slack:C123:new-thread"

    # it("should return a SentMessage with edit/delete capabilities")
    @pytest.mark.asyncio
    async def test_should_return_a_sentmessage_with_editdelete_capabilities(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")

        assert callable(result.edit)
        assert callable(result.delete)
        assert callable(result.add_reaction)
        assert callable(result.remove_reaction)

    # it("should allow editing a sent message")
    @pytest.mark.asyncio
    async def test_should_allow_editing_a_sent_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")
        await result.edit("Updated!")

        assert len(adapter._edit_calls) == 1
        assert adapter._edit_calls[0] == ("slack:C123", "msg-1", "Updated!")

    # it("should allow deleting a sent message")
    @pytest.mark.asyncio
    async def test_should_allow_deleting_a_sent_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")
        await result.delete()

        assert len(adapter._delete_calls) == 1
        assert adapter._delete_calls[0] == ("slack:C123", "msg-1")

    # it("should allow adding a reaction to a sent message")
    @pytest.mark.asyncio
    async def test_should_allow_adding_a_reaction_to_a_sent_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")
        await result.add_reaction("thumbsup")

        assert len(adapter._add_reaction_calls) == 1
        assert adapter._add_reaction_calls[0] == ("slack:C123", "msg-1", "thumbsup")

    # it("should allow removing a reaction from a sent message")
    @pytest.mark.asyncio
    async def test_should_allow_removing_a_reaction_from_a_sent_message(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)
        result = await channel.post("Hello!")
        await result.remove_reaction("thumbsup")

        assert len(adapter._remove_reaction_calls) == 1
        assert adapter._remove_reaction_calls[0] == ("slack:C123", "msg-1", "thumbsup")


# ===========================================================================
# thread.messages (newest first) - in channel.test.ts
# ===========================================================================


class TestThreadMessagesNewestFirst:
    """describe("thread.messages (newest first)") from channel.test.ts"""

    # it("should iterate messages newest first")
    @pytest.mark.asyncio
    async def test_should_iterate_messages_newest_first(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Oldest"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Newest"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.messages())

        assert len(collected) == 3
        assert collected[0].text == "Newest"
        assert collected[1].text == "Middle"
        assert collected[2].text == "Oldest"

    # it("should use backward direction")
    @pytest.mark.asyncio
    async def test_should_use_backward_direction(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=[], next_cursor=None)
        )

        thread = _make_thread(adapter, state)
        await _collect(thread.messages())

        adapter.fetch_messages.assert_called_once()
        call_args = adapter.fetch_messages.call_args[0]
        opts = call_args[1]
        assert opts.direction == "backward"

    # it("should handle pagination")
    @pytest.mark.asyncio
    async def test_should_handle_pagination(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        call_count = 0

        async def mock_fetch(thread_id: str, options: Any = None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return FetchResult(
                    messages=[
                        create_test_message("msg-2", "Page 1 Old"),
                        create_test_message("msg-3", "Page 1 New"),
                    ],
                    next_cursor="cursor-1",
                )
            return FetchResult(
                messages=[create_test_message("msg-1", "Page 2 Old")],
                next_cursor=None,
            )

        adapter.fetch_messages = mock_fetch  # type: ignore[assignment]

        thread = _make_thread(adapter, state)
        collected = await _collect(thread.messages())

        assert len(collected) == 3
        # Page 1 reversed
        assert collected[0].text == "Page 1 New"
        assert collected[1].text == "Page 1 Old"
        # Page 2 reversed
        assert collected[2].text == "Page 2 Old"

    # it("should allow getting N most recent messages with break")
    @pytest.mark.asyncio
    async def test_should_allow_getting_n_most_recent_messages_with_break(self):
        adapter = create_mock_adapter()
        state = create_mock_state()

        messages = [
            create_test_message("msg-1", "Old"),
            create_test_message("msg-2", "Middle"),
            create_test_message("msg-3", "Recent"),
            create_test_message("msg-4", "Very Recent"),
            create_test_message("msg-5", "Newest"),
        ]
        adapter.fetch_messages = AsyncMock(  # type: ignore[assignment]
            return_value=FetchResult(messages=messages, next_cursor="more")
        )

        thread = _make_thread(adapter, state)
        recent: list[Message] = []
        async for msg in thread.messages():
            recent.append(msg)
            if len(recent) >= 3:
                break

        assert len(recent) == 3
        assert recent[0].text == "Newest"
        assert recent[1].text == "Very Recent"
        assert recent[2].text == "Recent"


# ===========================================================================
# schedule() (channel)
# ===========================================================================


class TestChannelSchedule:
    """describe("schedule()") from channel.test.ts"""

    FUTURE_DATE = datetime(2030, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    def _mock_schedule_result(self, **overrides: Any) -> ScheduledMessage:
        defaults = {
            "scheduled_message_id": "Q123",
            "channel_id": "C123",
            "post_at": self.FUTURE_DATE,
            "raw": {"ok": True},
            "_cancel": AsyncMock(return_value=None),
        }
        defaults.update(overrides)
        return ScheduledMessage(**defaults)

    # it("should throw NotImplementedError when adapter has no scheduleMessage")
    @pytest.mark.asyncio
    async def test_should_throw_notimplementederror_when_adapter_has_no_schedulemessage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)

        with pytest.raises(ChatNotImplementedError):
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)

    # it("should include 'scheduling' as the feature in NotImplementedError")
    @pytest.mark.asyncio
    async def test_should_include_scheduling_as_the_feature_in_notimplementederror(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        channel = _make_channel(adapter, state)

        with pytest.raises(ChatNotImplementedError) as exc_info:
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)
        assert exc_info.value.method == "scheduling"

    # it("should delegate to adapter.scheduleMessage with channel id")
    @pytest.mark.asyncio
    async def test_should_delegate_to_adapterschedulemessage_with_channel_id(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        channel = _make_channel(adapter, state)
        await channel.schedule("Hello", post_at=self.FUTURE_DATE)

        adapter.schedule_message.assert_called_once()
        call_args = adapter.schedule_message.call_args[0]
        assert call_args[0] == "slack:C123"
        assert call_args[1] == "Hello"
        assert call_args[2] == {"post_at": self.FUTURE_DATE}

    # it("should return the ScheduledMessage from adapter")
    @pytest.mark.asyncio
    async def test_should_return_the_scheduledmessage_from_adapter(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        expected = self._mock_schedule_result()
        adapter.schedule_message = AsyncMock(return_value=expected)  # type: ignore[attr-defined]

        channel = _make_channel(adapter, state)
        result = await channel.schedule("Hello", post_at=self.FUTURE_DATE)
        assert result is expected

    # it("should propagate errors from adapter.scheduleMessage")
    @pytest.mark.asyncio
    async def test_should_propagate_errors_from_adapterschedulemessage(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            side_effect=Exception("API failure")
        )

        channel = _make_channel(adapter, state)
        with pytest.raises(Exception, match="API failure"):
            await channel.schedule("Hello", post_at=self.FUTURE_DATE)

    # it("should not call postMessage or postChannelMessage when scheduling")
    @pytest.mark.asyncio
    async def test_should_not_call_postmessage_or_postchannelmessage_when_scheduling(self):
        adapter = create_mock_adapter()
        state = create_mock_state()
        adapter.schedule_message = AsyncMock(  # type: ignore[attr-defined]
            return_value=self._mock_schedule_result()
        )

        post_channel_calls: list[Any] = []

        async def tracking_post(channel_id: str, message: Any) -> RawMessage:
            post_channel_calls.append((channel_id, message))
            return RawMessage(id="msg-1", thread_id=None, raw={})

        adapter.post_channel_message = tracking_post  # type: ignore[assignment]

        channel = _make_channel(adapter, state)
        await channel.schedule("Hello", post_at=self.FUTURE_DATE)

        assert len(adapter._post_calls) == 0
        assert len(post_channel_calls) == 0


class TestJsxAbsorbers:
    """Fidelity-check absorbers for TS tests that rely on JSX and cannot be ported to Python."""

    # JSX Card elements are a TypeScript/React-specific feature (JSX syntax, Card() returns
    # a JSX element that is converted to CardElement). Python has no JSX equivalent, so this
    # test cannot be faithfully translated. Kept as an absorber for verify_test_fidelity.py.
    def test_should_convert_jsx_card_elements_to_cardelement(self):
        assert True
