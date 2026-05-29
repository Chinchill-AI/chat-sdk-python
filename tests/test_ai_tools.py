"""Tests for ``chat_sdk.ai.tools``.

Mirrors the upstream Vitest suite in
``packages/chat/src/ai/index.test.ts`` (vercel/chat#492). Each test is
load-bearing — exercising a specific contract of either the
``create_chat_tools`` orchestrator (presets, approval config, override
filtering) or a specific tool factory's ``execute`` path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk import Chat
from chat_sdk.ai import (
    ChatTool,
    create_chat_tools,
)
from chat_sdk.errors import ChatError, ChatNotImplementedError
from chat_sdk.shared.mock_adapter import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
    mock_logger,
)
from chat_sdk.types import (
    ChannelInfo,
    FetchResult,
    ListThreadsResult,
    PostableMarkdown,
    PostableRaw,
    ThreadInfo,
    ThreadSummary,
    UserInfo,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Harness:
    chat: Chat
    adapter: MockAdapter
    state: MockStateAdapter


@pytest.fixture
async def harness() -> _Harness:
    adapter = create_mock_adapter("slack")
    state = create_mock_state()
    chat = Chat(
        user_name="testbot",
        adapters={"slack": adapter},
        state=state,
        logger=mock_logger,
    )
    return _Harness(chat=chat, adapter=adapter, state=state)


# ---------------------------------------------------------------------------
# Orchestrator: createChatTools
# ---------------------------------------------------------------------------


class TestCreateChatToolsShape:
    """Tests for the ``create_chat_tools`` return shape, presets, and validation."""

    async def test_returns_full_toolset_when_no_preset_supplied(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        assert sorted(tools.keys()) == sorted(
            [
                "addReaction",
                "deleteMessage",
                "editMessage",
                "fetchChannelMessages",
                "fetchMessages",
                "fetchThread",
                "getChannelInfo",
                "getThreadParticipants",
                "getUser",
                "listThreads",
                "postChannelMessage",
                "postMessage",
                "removeReaction",
                "sendDirectMessage",
                "startTyping",
                "subscribeThread",
                "unsubscribeThread",
            ]
        )

    async def test_requires_a_chat_instance(self):
        with pytest.raises(ChatError, match="requires a `chat` instance"):
            create_chat_tools(chat=None)

    async def test_scopes_tools_to_single_preset(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, preset="reader")
        names = sorted(tools.keys())
        assert names == sorted(
            [
                "fetchChannelMessages",
                "fetchMessages",
                "fetchThread",
                "getChannelInfo",
                "getThreadParticipants",
                "getUser",
                "listThreads",
            ]
        )
        # No write tools at all
        assert "postMessage" not in names
        assert "deleteMessage" not in names

    async def test_composes_multiple_presets(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, preset=["reader", "messenger"])
        names = set(tools.keys())
        assert "postMessage" in names
        assert "fetchMessages" in names
        assert "listThreads" in names
        # Neither preset includes deleteMessage / editMessage
        assert "deleteMessage" not in names
        assert "editMessage" not in names

    async def test_rejects_unknown_preset_name(self, harness: _Harness):
        with pytest.raises(ChatError, match="Unknown preset"):
            create_chat_tools(chat=harness.chat, preset="superuser")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Approval semantics
# ---------------------------------------------------------------------------


class TestRequireApproval:
    """Tests for the ``require_approval`` config (bool + per-tool mapping)."""

    async def test_every_write_tool_defaults_to_needs_approval_true(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        write_tools = [
            "postMessage",
            "postChannelMessage",
            "sendDirectMessage",
            "editMessage",
            "deleteMessage",
            "addReaction",
            "removeReaction",
            "subscribeThread",
            "unsubscribeThread",
        ]
        for name in write_tools:
            assert tools[name].needs_approval is True, name

    async def test_read_only_tools_never_gate_on_approval(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        read_tools = [
            "fetchMessages",
            "fetchChannelMessages",
            "fetchThread",
            "listThreads",
            "getThreadParticipants",
            "getChannelInfo",
            "getUser",
            # Typing indicator is harmless and never gated
            "startTyping",
        ]
        for name in read_tools:
            assert tools[name].needs_approval is None, name

    async def test_require_approval_false_disables_all(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        write_tools = [
            "postMessage",
            "postChannelMessage",
            "sendDirectMessage",
            "editMessage",
            "deleteMessage",
            "addReaction",
            "removeReaction",
            "subscribeThread",
            "unsubscribeThread",
        ]
        for name in write_tools:
            assert tools[name].needs_approval is False, name

    async def test_per_tool_approval_overrides(self, harness: _Harness):
        tools = create_chat_tools(
            chat=harness.chat,
            require_approval={
                "postMessage": False,
                "deleteMessage": True,
                "subscribeThread": False,
            },
        )
        assert tools["postMessage"].needs_approval is False
        assert tools["deleteMessage"].needs_approval is True
        assert tools["subscribeThread"].needs_approval is False
        # Unspecified write tools fall back to True
        assert tools["editMessage"].needs_approval is True
        assert tools["unsubscribeThread"].needs_approval is True


# ---------------------------------------------------------------------------
# Override semantics
# ---------------------------------------------------------------------------


class TestOverrides:
    """Tests for per-tool overrides (descriptions, extras, protected fields)."""

    async def test_applies_overrides_without_breaking_execution(self, harness: _Harness):
        tools = create_chat_tools(
            chat=harness.chat,
            overrides={
                "postMessage": {
                    "description": "Reply in the active support thread",
                    "needs_approval": False,
                },
            },
        )
        assert tools["postMessage"].description == "Reply in the active support thread"
        assert tools["postMessage"].needs_approval is False

    async def test_overrides_cannot_replace_core_tool_fields(self, harness: _Harness):
        # Stash sentinels; if any of these leak through, the tool can't run.
        hijack_execute = AsyncMock(return_value={"hijacked": True})
        hijack_input_schema: dict[str, Any] = {"sentinel": "input"}
        hijack_output_schema: dict[str, Any] = {"sentinel": "output"}
        input_examples = [
            {"input": {"threadId": "slack:C123:1234.5678", "message": "hello"}},
        ]
        metadata = {"source": "chat-sdk"}

        tools = create_chat_tools(
            chat=harness.chat,
            require_approval=False,
            overrides={
                "postMessage": {
                    "args": {"name": "custom"},
                    "description": "Reply in the active support thread",
                    "execute": hijack_execute,
                    "id": "openai.custom",
                    "input_examples": input_examples,
                    "input_schema": hijack_input_schema,
                    "metadata": metadata,
                    "output_schema": hijack_output_schema,
                    "supports_deferred_results": True,
                    "type": "provider",
                },
            },
        )
        tool = tools["postMessage"]

        # Description does come from overrides...
        assert tool.description == "Reply in the active support thread"
        # ...but the protected fields are filtered out so the real tool is intact.
        assert tool.execute is not hijack_execute
        assert tool.input_schema is not hijack_input_schema
        # `args`, `id`, `output_schema`, `supports_deferred_results`, and `type`
        # are protected fields — they never make it into `extras`.
        for protected in (
            "args",
            "id",
            "output_schema",
            "supports_deferred_results",
            "type",
        ):
            assert protected not in tool.extras, protected
        # Non-protected fields pass through to `extras` for the agent runtime.
        assert tool.extras["input_examples"] == input_examples
        assert tool.extras["metadata"] == metadata

        # The real execute still dispatches to the adapter.
        result = await tool.execute({"threadId": "slack:C123:1234.5678", "message": "hello"})
        hijack_execute.assert_not_awaited()
        assert harness.adapter._post_calls == [("slack:C123:1234.5678", "hello")]
        assert result == {"messageId": "msg-1", "threadId": "slack:C123:1234.5678"}


# ---------------------------------------------------------------------------
# Tool execute() paths
# ---------------------------------------------------------------------------


class TestExecutePaths:
    """Each tool's ``execute()`` dispatches through to the right adapter call."""

    async def test_post_message_dispatches_via_post_message(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["postMessage"].execute(
            {"threadId": "slack:C123:1234.5678", "message": "hello"},
        )
        assert harness.adapter._post_calls == [("slack:C123:1234.5678", "hello")]
        assert result["messageId"] == "msg-1"

    async def test_post_message_forwards_raw_postable_unchanged(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        await tools["postMessage"].execute(
            {
                "threadId": "slack:C123:1234.5678",
                "message": {"raw": "<blocks>...</blocks>"},
            },
        )
        # The raw body must reach the adapter as a PostableRaw (not flattened to str).
        assert len(harness.adapter._post_calls) == 1
        thread_id, sent = harness.adapter._post_calls[0]
        assert thread_id == "slack:C123:1234.5678"
        assert isinstance(sent, PostableRaw)
        assert sent.raw == "<blocks>...</blocks>"

    async def test_post_channel_message_dispatches_with_markdown(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["postChannelMessage"].execute(
            {"channelId": "slack:C123", "message": {"markdown": "**hi**"}},
        )
        # ChannelImpl uses post_channel_message on adapters that support it
        # (which MockAdapter does), so this must produce a SentMessage.
        assert result["messageId"] == "msg-1"
        assert result["threadId"] == "slack:C123"

    async def test_send_direct_message_opens_dm_then_posts(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        await tools["sendDirectMessage"].execute(
            {"userId": "U123456", "message": "ping"},
        )
        # MockAdapter.open_dm produces `slack:DU123456:` — the DM thread id.
        assert harness.adapter._post_calls == [("slack:DU123456:", "ping")]

    async def test_add_reaction_dispatches_via_adapter(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["addReaction"].execute(
            {
                "threadId": "slack:C123:1234.5678",
                "messageId": "msg-1",
                "emoji": "thumbs_up",
            },
        )
        assert harness.adapter._add_reaction_calls == [
            ("slack:C123:1234.5678", "msg-1", "thumbs_up"),
        ]
        assert result["added"] is True
        assert result["emoji"] == "thumbs_up"

    async def test_remove_reaction_dispatches_via_adapter(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["removeReaction"].execute(
            {
                "threadId": "slack:C123:1234.5678",
                "messageId": "msg-1",
                "emoji": "thumbs_up",
            },
        )
        assert harness.adapter._remove_reaction_calls == [
            ("slack:C123:1234.5678", "msg-1", "thumbs_up"),
        ]
        assert result == {
            "removed": True,
            "emoji": "thumbs_up",
            "messageId": "msg-1",
            "threadId": "slack:C123:1234.5678",
        }

    async def test_delete_message_dispatches_via_adapter(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["deleteMessage"].execute(
            {"threadId": "slack:C123:1234.5678", "messageId": "msg-1"},
        )
        assert harness.adapter._delete_calls == [("slack:C123:1234.5678", "msg-1")]
        assert result == {
            "deleted": True,
            "messageId": "msg-1",
            "threadId": "slack:C123:1234.5678",
        }

    async def test_edit_message_dispatches_with_markdown(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["editMessage"].execute(
            {
                "threadId": "slack:C123:1234.5678",
                "messageId": "msg-1",
                "message": {"markdown": "**updated**"},
            },
        )
        assert len(harness.adapter._edit_calls) == 1
        thread_id, msg_id, postable = harness.adapter._edit_calls[0]
        assert thread_id == "slack:C123:1234.5678"
        assert msg_id == "msg-1"
        assert isinstance(postable, PostableMarkdown)
        assert postable.markdown == "**updated**"
        assert result["messageId"] == "msg-1"

    async def test_subscribe_thread_persists_subscription(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        await tools["subscribeThread"].execute({"threadId": "slack:C123:1234.5678"})
        assert await harness.state.is_subscribed("slack:C123:1234.5678") is True

    async def test_unsubscribe_thread_clears_subscription(self, harness: _Harness):
        # Seed the state so we can prove unsubscribe clears it.
        await harness.state.subscribe("slack:C123:1234.5678")
        assert await harness.state.is_subscribed("slack:C123:1234.5678") is True

        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        result = await tools["unsubscribeThread"].execute(
            {"threadId": "slack:C123:1234.5678"},
        )
        assert await harness.state.is_subscribed("slack:C123:1234.5678") is False
        assert result == {"subscribed": False, "threadId": "slack:C123:1234.5678"}

    async def test_start_typing_dispatches_via_adapter(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        await tools["startTyping"].execute(
            {"threadId": "slack:C123:1234.5678", "status": "Searching..."},
        )
        assert harness.adapter._start_typing_calls == [
            ("slack:C123:1234.5678", "Searching..."),
        ]

    async def test_fetch_messages_projects_model_friendly_shape(self, harness: _Harness):
        stub_message = create_test_message("m1", "hello")
        harness.adapter.fetch_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(messages=[stub_message], next_cursor=None),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["fetchMessages"].execute(
            {"threadId": "slack:C123:1234.5678", "limit": 5, "direction": "backward"},
        )
        assert len(result["messages"]) == 1
        assert result["messages"][0]["id"] == "m1"
        assert result["messages"][0]["text"] == "hello"
        # Author is flattened into camelCase keys that match the wire shape.
        assert result["messages"][0]["author"]["userName"] == "testuser"

    async def test_get_channel_info_returns_flattened_metadata(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["getChannelInfo"].execute({"channelId": "slack:C123"})
        assert result == {
            "id": "slack:C123",
            "name": "#slack:C123",
            "isDM": False,
            "memberCount": None,
            "channelVisibility": None,
        }

    async def test_fetch_channel_messages_dispatches_and_projects(self, harness: _Harness):
        stub_message = create_test_message("m1", "channel hello")
        harness.adapter.fetch_channel_messages = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(messages=[stub_message], next_cursor="next"),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["fetchChannelMessages"].execute(
            {"channelId": "slack:C123", "limit": 5, "direction": "backward"},
        )
        harness.adapter.fetch_channel_messages.assert_awaited_once()
        call_args = harness.adapter.fetch_channel_messages.await_args
        assert call_args.args[0] == "slack:C123"
        # The FetchOptions are forwarded verbatim from the tool's inputs.
        opts = call_args.args[1]
        assert opts.limit == 5
        assert opts.cursor is None
        assert opts.direction == "backward"

        assert len(result["messages"]) == 1
        assert result["messages"][0]["id"] == "m1"
        assert result["messages"][0]["text"] == "channel hello"
        assert result["nextCursor"] == "next"

    async def test_fetch_channel_messages_raises_when_adapter_unsupported(self, harness: _Harness):
        # Remove the adapter's fetch_channel_messages so the tool path's
        # "does not support" branch fires.
        harness.adapter.fetch_channel_messages = None  # type: ignore[method-assign,assignment]
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not support fetching channel messages"):
            await tools["fetchChannelMessages"].execute({"channelId": "slack:C123"})

    async def test_fetch_channel_messages_wraps_not_implemented(self, harness: _Harness):
        # BaseAdapter's default stub for optional methods raises
        # ChatNotImplementedError. The tool must wrap that into ChatError so
        # callers see one consistent failure mode, preserving the cause chain.
        harness.adapter.fetch_channel_messages = AsyncMock(  # type: ignore[method-assign]
            side_effect=ChatNotImplementedError("slack", "fetch_channel_messages"),
        )
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not support fetching channel messages") as exc_info:
            await tools["fetchChannelMessages"].execute({"channelId": "slack:C123"})
        assert isinstance(exc_info.value.__cause__, ChatNotImplementedError)

    async def test_fetch_thread_returns_flattened_thread_info(self, harness: _Harness):
        harness.adapter.fetch_thread = AsyncMock(  # type: ignore[method-assign]
            return_value=ThreadInfo(
                id="slack:C123:1234.5678",
                channel_id="C123",
                channel_name="#general",
                channel_visibility="public",
                is_dm=False,
                metadata={},
            ),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["fetchThread"].execute({"threadId": "slack:C123:1234.5678"})
        assert result == {
            "id": "slack:C123:1234.5678",
            "channelId": "C123",
            "channelName": "#general",
            "channelVisibility": "public",
            "isDM": False,
        }

    async def test_list_threads_projects_summaries(self, harness: _Harness):
        root_message = create_test_message("m1", "root")
        from datetime import datetime, timezone

        last_reply = datetime(2026, 3, 2, tzinfo=timezone.utc)
        harness.adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            return_value=ListThreadsResult(
                threads=[
                    ThreadSummary(
                        id="slack:C123:1234.5678",
                        reply_count=4,
                        last_reply_at=last_reply,
                        root_message=root_message,
                    ),
                ],
                next_cursor=None,
            ),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["listThreads"].execute(
            {"channelId": "slack:C123", "limit": 10},
        )
        assert len(result["threads"]) == 1
        summary = result["threads"][0]
        assert summary["id"] == "slack:C123:1234.5678"
        assert summary["replyCount"] == 4
        assert summary["lastReplyAt"] == last_reply.isoformat()
        assert summary["rootMessage"]["id"] == "m1"
        assert summary["rootMessage"]["text"] == "root"

    async def test_list_threads_raises_when_adapter_unsupported(self, harness: _Harness):
        harness.adapter.list_threads = None  # type: ignore[method-assign,assignment]
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not support listing threads"):
            await tools["listThreads"].execute({"channelId": "slack:C123"})

    async def test_list_threads_wraps_not_implemented(self, harness: _Harness):
        harness.adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            side_effect=ChatNotImplementedError("slack", "list_threads"),
        )
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not support listing threads") as exc_info:
            await tools["listThreads"].execute({"channelId": "slack:C123"})
        assert isinstance(exc_info.value.__cause__, ChatNotImplementedError)

    async def test_get_thread_participants_delegates_to_thread(self, harness: _Harness):
        # Stub `chat.thread(...)` directly so we don't drag in the cursor
        # pagination / current-message machinery just to test the projection.
        from chat_sdk.types import Author as AuthorType

        participants_stub = [
            AuthorType(
                user_id="UALICE1",
                user_name="alice",
                full_name="Alice",
                is_bot=False,
                is_me=False,
            ),
            AuthorType(
                user_id="UBOB1",
                user_name="bob",
                full_name="Bob",
                is_bot=False,
                is_me=False,
            ),
        ]

        class _FakeThread:
            async def get_participants(self) -> list[AuthorType]:
                return participants_stub

        original_thread = harness.chat.thread
        harness.chat.thread = lambda thread_id, **kwargs: _FakeThread()  # type: ignore[assignment]
        try:
            tools = create_chat_tools(chat=harness.chat)
            result = await tools["getThreadParticipants"].execute(
                {"threadId": "slack:C123:1234.5678"},
            )
        finally:
            harness.chat.thread = original_thread  # type: ignore[assignment]

        assert result == {
            "participants": [
                {"userId": "UALICE1", "userName": "alice", "fullName": "Alice", "isBot": False},
                {"userId": "UBOB1", "userName": "bob", "fullName": "Bob", "isBot": False},
            ],
        }

    async def test_get_user_projects_user_info_when_found(self, harness: _Harness):
        harness.adapter.get_user = AsyncMock(  # type: ignore[method-assign]
            return_value=UserInfo(
                user_id="U123456",
                user_name="alice",
                full_name="Alice Doe",
                email="alice@example.com",
                is_bot=False,
                avatar_url="https://example.com/a.png",
            ),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["getUser"].execute({"userId": "U123456"})
        assert result == {
            "userId": "U123456",
            "userName": "alice",
            "fullName": "Alice Doe",
            "email": "alice@example.com",
            "isBot": False,
            "avatarUrl": "https://example.com/a.png",
        }

    async def test_get_user_returns_none_when_adapter_returns_none(self, harness: _Harness):
        harness.adapter.get_user = AsyncMock(return_value=None)  # type: ignore[method-assign]
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["getUser"].execute({"userId": "UMISSING"})
        assert result is None


# ---------------------------------------------------------------------------
# Schema sanity checks
# ---------------------------------------------------------------------------


class TestInputSchemas:
    """Schemas are part of the public contract — break them, break agent runtimes."""

    async def test_every_tool_declares_an_input_schema_with_a_description(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        for name, tool in tools.items():
            assert isinstance(tool, ChatTool), name
            assert tool.description, f"{name} is missing a description"
            assert tool.input_schema.get("type") == "object", name
            assert "properties" in tool.input_schema, name

    async def test_postable_input_schema_is_a_oneof_union(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        message_schema = tools["postMessage"].input_schema["properties"]["message"]
        # The union must include all three branches: string, markdown, raw.
        assert "oneOf" in message_schema
        kinds: list[Any] = []
        for branch in message_schema["oneOf"]:
            if branch.get("type") == "string":
                kinds.append("string")
            elif "properties" in branch and "markdown" in branch["properties"]:
                kinds.append("markdown")
            elif "properties" in branch and "raw" in branch["properties"]:
                kinds.append("raw")
        assert sorted(kinds) == ["markdown", "raw", "string"]


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class TestReexports:
    """The ``chat_sdk.ai`` package re-exports the tool factory surface."""

    async def test_can_import_individual_factory(self, harness: _Harness):
        # If individual tool factories aren't exported, downstream code that
        # cherry-picks (``from chat_sdk.ai import post_message``) breaks.
        from chat_sdk.ai import add_reaction, post_message

        tool = post_message(harness.chat)
        assert isinstance(tool, ChatTool)
        assert tool.needs_approval is True
        # The needs_approval default propagates through the factory's ToolOptions.

        from chat_sdk.ai.tools import ToolOptions

        relaxed = add_reaction(harness.chat, ToolOptions(needs_approval=False))
        assert relaxed.needs_approval is False

    async def test_messages_helpers_still_importable_from_ai(self):
        # PR 1 of the chat/ai port moved to_ai_messages here; if the new
        # tool exports clobber that, the re-export goes silently stale.
        from chat_sdk.ai import to_ai_messages

        assert callable(to_ai_messages)


# ---------------------------------------------------------------------------
# Approval gating quirks
# ---------------------------------------------------------------------------


class TestApprovalEdgeCases:
    """Edge cases for the approval mapping that aren't covered above."""

    async def test_partial_mapping_falls_back_to_true_for_unspecified_writes(self, harness: _Harness):
        # Only override one tool — every other write tool should keep the
        # default needs_approval=True. A regression that flips the default
        # to False would silently let untrusted models post messages.
        tools = create_chat_tools(
            chat=harness.chat,
            require_approval={"postMessage": False},
        )
        assert tools["postMessage"].needs_approval is False
        # Every other write tool still needs approval.
        for name in (
            "postChannelMessage",
            "sendDirectMessage",
            "editMessage",
            "deleteMessage",
            "addReaction",
            "removeReaction",
            "subscribeThread",
            "unsubscribeThread",
        ):
            assert tools[name].needs_approval is True, name


# ---------------------------------------------------------------------------
# Channel info edge cases (covers ChannelInfo.is_dm branching)
# ---------------------------------------------------------------------------


class TestChannelInfoEdgeCases:
    async def test_get_channel_info_defaults_is_dm_false_when_adapter_returns_none(self, harness: _Harness):
        # ChannelInfo.is_dm is Optional; the tool must coerce None → False
        # so the model sees a plain boolean instead of a missing field.
        harness.adapter.fetch_channel_info = AsyncMock(  # type: ignore[method-assign]
            return_value=ChannelInfo(id="slack:C999", name=None, is_dm=None, metadata={}),
        )
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["getChannelInfo"].execute({"channelId": "slack:C999"})
        assert result["isDM"] is False
        assert result["name"] is None
