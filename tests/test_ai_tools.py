"""Tests for ``chat_sdk.ai.tools``.

Mirrors the upstream Vitest suite in
``packages/chat/src/ai/index.test.ts`` (vercel/chat#492). Each test is
load-bearing — exercising a specific contract of either the
``create_chat_tools`` orchestrator (presets, approval config, override
filtering) or a specific tool factory's ``execute`` path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from chat_sdk import Chat
from chat_sdk.ai import (
    ChatTool,
    ChatToolsOptions,
    create_chat_tools,
    get_user,
)
from chat_sdk.context import active_conversation, conversation
from chat_sdk.emoji import get_emoji
from chat_sdk.errors import ChatError, ChatNotImplementedError
from chat_sdk.shared.mock_adapter import (
    MockAdapter,
    MockLogger,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
    create_test_message,
    mock_logger,
)
from chat_sdk.types import (
    ActionEvent,
    AppHomeOpenedEvent,
    AssistantContextChangedEvent,
    AssistantThreadStartedEvent,
    Author,
    ChannelInfo,
    FetchResult,
    ListThreadsResult,
    MemberJoinedChannelEvent,
    ModalCloseEvent,
    ModalSubmitEvent,
    PostableMarkdown,
    PostableRaw,
    ReactionEvent,
    SlashCommandEvent,
    ThreadInfo,
    ThreadSummary,
    UserInfo,
    WebhookOptions,
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

    async def test_requires_approval_on_every_write_tool_and_getuser_by_default(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        # Every mutating tool (and getUser, upstream #875) must default to
        # needs_approval=True so a misnamed gated tool is caught.
        gated_tools = [
            "postMessage",
            "postChannelMessage",
            "sendDirectMessage",
            "editMessage",
            "deleteMessage",
            "addReaction",
            "removeReaction",
            "subscribeThread",
            "unsubscribeThread",
            "getUser",
        ]
        for name in gated_tools:
            assert tools[name].needs_approval is True, name
        # Conversation-scoped read tools do not gate on approval, and the
        # typing indicator is harmless and never gated.
        ungated_tools = [
            "fetchMessages",
            "fetchChannelMessages",
            "fetchThread",
            "listThreads",
            "getThreadParticipants",
            "getChannelInfo",
            "startTyping",
        ]
        for name in ungated_tools:
            assert tools[name].needs_approval is None, name

    async def test_requires_approval_for_standalone_getuser_by_default(self, harness: _Harness):
        assert get_user(harness.chat).needs_approval is True

    async def test_disables_approval_on_every_gated_tool_when_requireapproval_is_false(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, require_approval=False)
        gated_tools = [
            "postMessage",
            "postChannelMessage",
            "sendDirectMessage",
            "editMessage",
            "deleteMessage",
            "addReaction",
            "removeReaction",
            "subscribeThread",
            "unsubscribeThread",
            "getUser",
        ]
        for name in gated_tools:
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

    async def test_explicit_none_approval_override_still_requires_approval(self, harness: _Harness):
        # Python-specific: upstream ``config[toolName] ?? true`` treats null as
        # unset, so a ``None`` entry (e.g. from JSON/YAML config) must not
        # ungate a tool -- ``None`` is the "no gate" value on ChatTool.
        tools = create_chat_tools(
            chat=harness.chat,
            require_approval={"getUser": None, "postMessage": None},  # type: ignore[dict-item]
        )
        assert tools["getUser"].needs_approval is True
        assert tools["postMessage"].needs_approval is True


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

    async def test_postmessage_dispatches_via_the_adapters_postmessage(self, harness: _Harness):
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

    async def test_postchannelmessage_dispatches_via_the_adapters_postchannelmessage(self, harness: _Harness):
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

    async def test_addreaction_dispatches_via_the_adapters_addreaction(self, harness: _Harness):
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

    async def test_removereaction_dispatches_via_the_adapters_removereaction(self, harness: _Harness):
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

    async def test_deletemessage_dispatches_via_the_adapters_deletemessage(self, harness: _Harness):
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

    async def test_editmessage_dispatches_via_the_adapters_editmessage(self, harness: _Harness):
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

    async def test_starttyping_dispatches_via_the_adapters_starttyping(self, harness: _Harness):
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

    async def test_fetch_messages_serves_sdk_cached_history_for_persisting_adapters(self, harness: _Harness):
        # fetchMessages routes through chat.history.thread.list, so an adapter
        # whose history lives in the SDK-side cache (Telegram, WhatsApp) gets
        # the cached messages when the platform page is empty.
        harness.adapter.persist_thread_history = True
        await harness.chat.history.thread.append("slack:C123:1234.5678", create_test_message("c1", "cached"))
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["fetchMessages"].execute({"threadId": "slack:C123:1234.5678"})
        assert [m["text"] for m in result["messages"]] == ["cached"]
        assert result["nextCursor"] is None

    async def test_fetch_messages_raises_for_an_unregistered_adapter_prefix(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match='no adapter registered with name "slcak"'):
            await tools["fetchMessages"].execute({"threadId": "slcak:C123:1234.5678"})

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

    async def test_fetchchannelmessages_dispatches_via_the_adapter_and_projects_messages(self, harness: _Harness):
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

    async def test_fetchchannelmessages_throws_when_the_adapter_does_not_support_it(self, harness: _Harness):
        # Remove the adapter's fetch_channel_messages so the tool path's
        # "does not support" branch fires.
        harness.adapter.fetch_channel_messages = None  # type: ignore[method-assign,assignment]
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not support fetching channel messages"):
            await tools["fetchChannelMessages"].execute({"channelId": "slack:C123"})
        # No silent fallback to thread reads for a non-persisting adapter.
        assert harness.adapter._fetch_calls == []

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

    async def test_listthreads_projects_threadsummary_entries(self, harness: _Harness):
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

    async def test_list_threads_uses_keyword_options(self, harness: _Harness):
        """Pin that the tool passes ``options`` as a keyword to ``list_threads``.

        ``MockAdapter.list_threads`` (and any adapter using a ``**kwargs``
        signature) rejects a second positional arg with ``TypeError`` — the
        tool must use the keyword form. This exercises the **real**
        ``MockAdapter.list_threads`` (no ``AsyncMock`` override) so a
        regression to positional args trips immediately at runtime, not just
        in the mock-adapter ergonomics.
        """
        tools = create_chat_tools(chat=harness.chat)
        result = await tools["listThreads"].execute({"channelId": "slack:C123"})
        assert result == {"threads": [], "nextCursor": None}

    async def test_listthreads_throws_when_the_adapter_does_not_support_it(self, harness: _Harness):
        harness.adapter.list_threads = None  # type: ignore[method-assign,assignment]
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not implement listThreads"):
            await tools["listThreads"].execute({"channelId": "slack:C123"})

    async def test_list_threads_wraps_not_implemented(self, harness: _Harness):
        harness.adapter.list_threads = AsyncMock(  # type: ignore[method-assign]
            side_effect=ChatNotImplementedError("slack", "list_threads"),
        )
        tools = create_chat_tools(chat=harness.chat)
        with pytest.raises(ChatError, match="does not implement listThreads") as exc_info:
            await tools["listThreads"].execute({"channelId": "slack:C123"})
        assert isinstance(exc_info.value.__cause__, ChatNotImplementedError)

    async def test_getthreadparticipants_delegates_to_threadgetparticipants_and_projects_authors(
        self, harness: _Harness
    ):
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

    async def test_getuser_projects_userinfo_when_the_adapter_resolves_a_user(self, harness: _Harness):
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
            "getUser",
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


# ---------------------------------------------------------------------------
# Schema isolation (review): tools must not share mutable nested schema dicts
# ---------------------------------------------------------------------------


class TestSchemaIsolation:
    """Each tool must own a fully independent ``input_schema``.

    The factories build several tools from the same shared schema source
    (the postable-message body, the fetch ``direction`` enum). If two tools
    embedded the *same* nested dict object, a downstream consumer mutating
    one tool's schema in place would silently corrupt its siblings. These
    tests deep-mutate one tool's schema and assert the sibling is untouched,
    pinning the per-tool deep-copy guarantee.
    """

    async def test_postable_message_schema_not_shared_between_tools(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        post_msg = tools["postMessage"]
        post_channel = tools["postChannelMessage"]

        a = post_msg.input_schema["properties"]["message"]
        b = post_channel.input_schema["properties"]["message"]
        # Distinct top-level objects and distinct nested objects.
        assert a is not b
        assert a["oneOf"] is not b["oneOf"]
        assert a["oneOf"][1] is not b["oneOf"][1]

        # Deep-mutate one tool's nested schema; the sibling must be unaffected.
        a["oneOf"][1]["properties"]["markdown"]["description"] = "MUTATED"
        a["oneOf"].append({"type": "null"})
        assert "description" not in b["oneOf"][1]["properties"]["markdown"]
        assert len(b["oneOf"]) == 3

        # A freshly built toolset must also be pristine (no module-level bleed).
        fresh = create_chat_tools(chat=harness.chat)
        fresh_msg = fresh["postMessage"].input_schema["properties"]["message"]
        assert "description" not in fresh_msg["oneOf"][1]["properties"]["markdown"]
        assert len(fresh_msg["oneOf"]) == 3

    async def test_fetch_direction_schema_not_shared_between_tools(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        fetch_msgs = tools["fetchMessages"]
        fetch_channel = tools["fetchChannelMessages"]

        a = fetch_msgs.input_schema["properties"]["direction"]
        b = fetch_channel.input_schema["properties"]["direction"]
        assert a is not b
        # The enum list is a nested mutable object that must not be shared.
        assert a["enum"] is not b["enum"]

        # Deep-mutate one tool's direction enum; sibling must be unaffected.
        a["enum"].append("sideways")
        assert b["enum"] == ["forward", "backward"]

        fresh = create_chat_tools(chat=harness.chat)
        fresh_dir = fresh["fetchMessages"].input_schema["properties"]["direction"]
        assert fresh_dir["enum"] == ["forward", "backward"]


# ---------------------------------------------------------------------------
# Conversation scope (upstream "read scope" / "write scope", #751/#774/#875)
# ---------------------------------------------------------------------------

_CALLER_THREAD = "slack:C123:1234.5678"
_OTHER_THREAD = "slack:C999:1111.2222"
_OUT_OF_SCOPE = "tools are scoped to"
_FETCH = {"limit": 5, "direction": "backward"}


def _user() -> Author:
    return Author(user_id="U1", user_name="alice", full_name="Alice", is_bot=False, is_me=False)


def _recording_fetch(adapter: MockAdapter) -> AsyncMock:
    mock = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
    adapter.fetch_messages = mock  # type: ignore[method-assign]
    return mock


def _recording_channel_fetch(adapter: MockAdapter) -> AsyncMock:
    mock = AsyncMock(return_value=FetchResult(messages=[], next_cursor=None))
    adapter.fetch_channel_messages = mock  # type: ignore[method-assign]
    return mock


async def _outcome(tools: dict[str, ChatTool], thread_id: str) -> str:
    try:
        await tools["fetchMessages"].execute({"threadId": thread_id, **_FETCH})
    except ChatError:
        return "blocked"
    return "allowed"


async def _drain(tasks: list[Any]) -> None:
    """Await every fire-and-forget task captured through ``wait_until``."""
    await asyncio.gather(*tasks)


class TestReadScope:
    """Upstream ``describe("read scope")``."""

    async def test_blocks_reading_a_thread_outside_the_scoped_channel(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE) as exc_info:
            await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        # Upstream's exact text.
        assert str(exc_info.value) == (
            f'Tool call blocked: tools are scoped to "{_CALLER_THREAD}", but "{_OTHER_THREAD}" resolves outside it.'
        )
        fetch.assert_not_called()

    async def test_allows_a_sibling_thread_in_the_scoped_channel_by_default(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        await tools["fetchMessages"].execute({"threadId": "slack:C123:9999.0000", **_FETCH})
        fetch.assert_awaited_once()

    async def test_blocks_a_sibling_thread_when_scoped_to_a_single_thread_with_strictscope(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD, strict_scope=True)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": "slack:C123:9999.0000", **_FETCH})
        fetch.assert_not_called()

    async def test_allows_a_sibling_thread_under_strictscope_when_a_channel_is_scoped(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope="slack:C123", strict_scope=True)
        await tools["fetchMessages"].execute({"threadId": "slack:C123:9999.0000", **_FETCH})
        fetch.assert_awaited_once()

    async def test_allows_channellevel_reads_when_scoped_to_a_thread(self, harness: _Harness):
        channel_fetch = _recording_channel_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        await tools["fetchChannelMessages"].execute({"channelId": "slack:C123", **_FETCH})
        channel_fetch.assert_awaited_once()

    async def test_allows_sibling_threads_when_scoped_to_the_whole_channel(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope="slack:C123")
        await tools["fetchMessages"].execute({"threadId": "slack:C123:9999.0000", **_FETCH})
        fetch.assert_awaited_once()

    async def test_blocks_channeladdressed_reads_outside_the_scoped_channel(self, harness: _Harness):
        channel_fetch = _recording_channel_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchChannelMessages"].execute({"channelId": "slack:C999", **_FETCH})
        channel_fetch.assert_not_called()

    async def test_scopes_every_read_tool(self, harness: _Harness):
        harness.adapter.fetch_thread = AsyncMock()  # type: ignore[method-assign]
        harness.adapter.list_threads = AsyncMock()  # type: ignore[method-assign]
        harness.adapter.fetch_channel_info = AsyncMock()  # type: ignore[method-assign]
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchThread"].execute({"threadId": _OTHER_THREAD})
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["listThreads"].execute({"channelId": "slack:C999", "limit": 5})
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["getThreadParticipants"].execute({"threadId": _OTHER_THREAD})
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["getChannelInfo"].execute({"channelId": "slack:C999"})
        # The guard runs before any platform call.
        harness.adapter.fetch_thread.assert_not_called()
        harness.adapter.list_threads.assert_not_called()
        harness.adapter.fetch_channel_info.assert_not_called()
        fetch.assert_not_called()

    async def test_accepts_a_thread_as_the_scope(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=harness.chat.thread(_CALLER_THREAD))
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        fetch.assert_not_called()
        # The Thread's own id is in scope.
        await tools["fetchMessages"].execute({"threadId": _CALLER_THREAD, **_FETCH})
        fetch.assert_awaited_once()

    async def test_blocks_reads_across_adapters(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": "discord:C123:1.2", **_FETCH})

    async def test_inherits_the_conversation_being_handled_when_no_scope_is_passed(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat)
        with conversation(_CALLER_THREAD), pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        fetch.assert_not_called()

    async def test_still_reads_its_own_conversation_when_inheriting(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat)
        with conversation(_CALLER_THREAD):
            await tools["fetchMessages"].execute({"threadId": _CALLER_THREAD, **_FETCH})
        fetch.assert_awaited_once()

    async def test_lets_an_explicit_scope_override_the_handled_conversation(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_OTHER_THREAD)
        with conversation(_CALLER_THREAD):
            await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        fetch.assert_awaited_once()

    async def test_keeps_each_concurrent_conversations_scope_separate(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)

        async def read(source: str, target: str) -> str:
            with conversation(source):
                await asyncio.sleep(0)
                return await _outcome(tools, target)

        results = await asyncio.gather(
            read(_CALLER_THREAD, _CALLER_THREAD),
            read(_OTHER_THREAD, _CALLER_THREAD),
            read(_CALLER_THREAD, _OTHER_THREAD),
            read(_OTHER_THREAD, _OTHER_THREAD),
        )
        assert results == ["allowed", "blocked", "blocked", "allowed"]
        assert active_conversation() is None

    async def test_scopes_reads_during_action_dispatch(self, harness: _Harness):
        outcome = "not-run"

        @harness.chat.on_action("do-thing")
        async def _handler(event: Any) -> None:
            nonlocal outcome
            outcome = await _outcome(create_chat_tools(chat=harness.chat), _OTHER_THREAD)

        tasks: list[Any] = []
        harness.chat.process_action(
            ActionEvent(
                adapter=harness.adapter,
                thread=None,
                thread_id=_CALLER_THREAD,
                message_id="m1",
                user=_user(),
                action_id="do-thing",
                value=None,
            ),
            WebhookOptions(wait_until=tasks.append),
        )
        await _drain(tasks)

        assert outcome == "blocked"
        assert active_conversation() is None

    async def test_scopes_reads_during_slash_command_dispatch(self, harness: _Harness):
        outcome = "not-run"

        @harness.chat.on_slash_command("/go")
        async def _handler(event: Any) -> None:
            nonlocal outcome
            outcome = await _outcome(create_chat_tools(chat=harness.chat), _OTHER_THREAD)

        event = SlashCommandEvent(
            adapter=harness.adapter,
            channel=None,  # type: ignore[arg-type]
            user=_user(),
            command="/go",
            text="",
        )
        # Adapters attach the resolved channel id to the partial event.
        event.channel_id = "slack:C123"  # type: ignore[attr-defined]
        tasks: list[Any] = []
        harness.chat.process_slash_command(event, WebhookOptions(wait_until=tasks.append))
        await _drain(tasks)

        assert outcome == "blocked"

    async def test_stays_unscoped_outside_a_handler_when_scope_is_omitted_but_warns(self):
        adapter = create_mock_adapter("slack")
        logger = MockLogger()
        chat = Chat(user_name="testbot", adapters={"slack": adapter}, state=create_mock_state(), logger=logger)
        fetch = _recording_fetch(adapter)
        tools = create_chat_tools(chat=chat)

        await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        fetch.assert_awaited_once()
        assert len(logger.warn.calls) == 1
        assert logger.warn.calls[0][0].startswith(f'Agent tool ran unscoped: "{_OTHER_THREAD}" was accessed')
        # A second unscoped read on the same guard must not warn again.
        await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        assert len(logger.warn.calls) == 1

    async def test_scopes_to_the_conversation_not_the_channel_across_adapters(self):
        # Discord collapses a thread id to `discord:guild:channel`, so sibling
        # threads share a channel that the coarse gate alone would allow.
        discord = create_mock_adapter("discord")
        discord.channel_id_from_thread_id = lambda tid: ":".join(tid.split(":")[:3])  # type: ignore[method-assign]
        channel_fetch = _recording_channel_fetch(discord)
        chat = Chat(user_name="testbot", adapters={"discord": discord}, state=create_mock_state(), logger=mock_logger)

        tools = create_chat_tools(chat=chat, scope="discord:g:c:t1", strict_scope=True)
        # Sibling thread under the same parent channel is blocked.
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": "discord:g:c:t2", **_FETCH})
        # The parent channel is rejected too: on a per-thread-ACL platform it
        # is the widest read available.
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchChannelMessages"].execute({"channelId": "discord:g:c", **_FETCH})
        channel_fetch.assert_not_called()

    async def test_keeps_dmonly_adapter_conversations_out_of_each_others_scope(self):
        twilio = create_mock_adapter("twilio")
        twilio.channel_id_from_thread_id = lambda tid: tid  # type: ignore[method-assign]
        fetch = _recording_fetch(twilio)
        chat = Chat(user_name="testbot", adapters={"twilio": twilio}, state=create_mock_state(), logger=mock_logger)

        tools = create_chat_tools(chat=chat, scope="twilio:bot:user1")
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["fetchMessages"].execute({"threadId": "twilio:bot:user2", **_FETCH})
        fetch.assert_not_called()

    async def test_allows_a_channellevel_read_under_strictscope_when_a_channel_is_scoped(self, harness: _Harness):
        channel_fetch = _recording_channel_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope="slack:C123", strict_scope=True)
        await tools["fetchChannelMessages"].execute({"channelId": "slack:C123", **_FETCH})
        channel_fetch.assert_awaited_once()

    async def test_scopes_reads_during_memberjoined_dispatch(self, harness: _Harness):
        outcome = "not-run"

        @harness.chat.on_member_joined_channel
        async def _handler(event: Any) -> None:
            nonlocal outcome
            outcome = await _outcome(create_chat_tools(chat=harness.chat), _OTHER_THREAD)

        tasks: list[Any] = []
        harness.chat.process_member_joined_channel(
            MemberJoinedChannelEvent(adapter=harness.adapter, channel_id="slack:C123", user_id="U1"),
            WebhookOptions(wait_until=tasks.append),
        )
        await _drain(tasks)

        assert outcome == "blocked"


class TestWriteScope:
    """Upstream ``describe("write scope")``."""

    async def test_blocks_posting_to_a_thread_outside_the_scoped_channel(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["postMessage"].execute({"threadId": _OTHER_THREAD, "message": "hi"})
        assert harness.adapter._post_calls == []

    async def test_allows_posting_inside_the_scoped_conversation(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        await tools["postMessage"].execute({"threadId": _CALLER_THREAD, "message": "hi"})
        assert harness.adapter._post_calls == [(_CALLER_THREAD, "hi")]

    async def test_scopes_every_thread_or_channeltargeting_write_tool(self, harness: _Harness):
        await harness.state.subscribe(_OTHER_THREAD)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        calls: list[tuple[str, dict[str, Any]]] = [
            ("postChannelMessage", {"channelId": "slack:C999", "message": "hi"}),
            ("editMessage", {"threadId": _OTHER_THREAD, "messageId": "m1", "message": "hi"}),
            ("deleteMessage", {"threadId": _OTHER_THREAD, "messageId": "m1"}),
            ("addReaction", {"threadId": _OTHER_THREAD, "messageId": "m1", "emoji": "thumbs_up"}),
            ("removeReaction", {"threadId": _OTHER_THREAD, "messageId": "m1", "emoji": "thumbs_up"}),
            ("subscribeThread", {"threadId": _OTHER_THREAD}),
            ("unsubscribeThread", {"threadId": _OTHER_THREAD}),
            ("startTyping", {"threadId": _OTHER_THREAD, "status": "Injected status"}),
        ]
        for name, args in calls:
            with pytest.raises(ChatError, match=_OUT_OF_SCOPE):
                await tools[name].execute(args)
        assert harness.adapter._post_calls == []
        assert harness.adapter._edit_calls == []
        assert harness.adapter._delete_calls == []
        assert harness.adapter._add_reaction_calls == []
        assert harness.adapter._remove_reaction_calls == []
        assert harness.adapter._start_typing_calls == []
        # The blocked unsubscribe left the pre-seeded subscription in place.
        assert await harness.state.is_subscribed(_OTHER_THREAD) is True

    async def test_inherits_the_handled_conversation_for_writes_when_no_scope_is_passed(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat)
        with conversation(_CALLER_THREAD), pytest.raises(ChatError, match=_OUT_OF_SCOPE):
            await tools["postMessage"].execute({"threadId": _OTHER_THREAD, "message": "hi"})
        assert harness.adapter._post_calls == []

    # sendDirectMessage targets a user id, not a thread or channel, so the
    # conversation scope guard has nothing to check it against. Approval is
    # its only gate.
    async def test_does_not_scope_senddirectmessage(self, harness: _Harness):
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD)
        await tools["sendDirectMessage"].execute({"userId": "U42", "message": "hi"})
        # MockAdapter.open_dm("U42") -> "slack:DU42:", then post there.
        assert harness.adapter._post_calls == [("slack:DU42:", "hi")]


class TestScopeOptOutAndContext:
    """Python-specific: ``scope=False``, and the context var's lifecycle."""

    async def test_scope_false_opts_out_even_inside_a_handled_conversation(self):
        adapter = create_mock_adapter("slack")
        logger = MockLogger()
        chat = Chat(user_name="testbot", adapters={"slack": adapter}, state=create_mock_state(), logger=logger)
        fetch = _recording_fetch(adapter)
        tools = create_chat_tools(chat=chat, scope=False)
        with conversation(_CALLER_THREAD):
            await tools["fetchMessages"].execute({"threadId": _OTHER_THREAD, **_FETCH})
        fetch.assert_awaited_once()
        # Opting out is deliberate, so it does not warn.
        assert logger.warn.calls == []

    async def test_strict_thread_scope_allows_the_scoped_thread_itself(self, harness: _Harness):
        fetch = _recording_fetch(harness.adapter)
        tools = create_chat_tools(chat=harness.chat, scope=_CALLER_THREAD, strict_scope=True)
        await tools["fetchMessages"].execute({"threadId": _CALLER_THREAD, **_FETCH})
        fetch.assert_awaited_once()

    async def test_create_chat_tools_options_carries_scope_fields(self, harness: _Harness):
        opts = ChatToolsOptions(chat=harness.chat)
        assert opts.scope is None
        assert opts.strict_scope is False

    async def test_conversation_none_inherits_and_resets_after_a_raise(self):
        assert active_conversation() is None
        with conversation(_CALLER_THREAD):
            # A ``None`` id runs bare, keeping the outer conversation.
            with conversation(None):
                assert active_conversation() == _CALLER_THREAD
            with pytest.raises(RuntimeError), conversation(_OTHER_THREAD):
                assert active_conversation() == _OTHER_THREAD
                raise RuntimeError("boom")
            assert active_conversation() == _CALLER_THREAD
        assert active_conversation() is None

    async def test_active_conversation_is_none_after_a_handler_raises(self, harness: _Harness):
        seen: list[str | None] = []

        @harness.chat.on_mention
        async def _handler(thread: Any, message: Any, context: Any = None) -> None:
            seen.append(active_conversation())
            raise RuntimeError("handler failed")

        # handle_incoming_message runs in THIS task, so a leak would be visible here.
        with pytest.raises(RuntimeError, match="handler failed"):
            await harness.chat.handle_incoming_message(
                harness.adapter,
                _CALLER_THREAD,
                create_test_message("msg-raise", "Hey @slack-bot", thread_id=_CALLER_THREAD),
            )
        assert seen == [_CALLER_THREAD]
        assert active_conversation() is None

    async def test_every_dispatch_path_runs_handlers_in_its_conversation(self, harness: _Harness):
        chat = harness.chat
        seen: dict[str, str | None] = {}

        def record(key: str) -> Any:
            async def _h(*_args: Any, **_kwargs: Any) -> None:
                seen[key] = active_conversation()

            return _h

        chat.on_reaction(record("reaction"))
        chat.on_assistant_thread_started(record("assistant_started"))
        chat.on_assistant_context_changed(record("assistant_context"))
        chat.on_app_home_opened(record("app_home"))
        chat.on_modal_submit(record("modal_submit"))
        chat.on_modal_close(record("modal_close"))

        tasks: list[Any] = []
        opts = WebhookOptions(wait_until=tasks.append)
        chat.process_reaction(
            ReactionEvent(
                adapter=harness.adapter,
                thread=None,  # type: ignore[arg-type]
                thread_id="slack:C1:1.1",
                message_id="m1",
                user=_user(),
                emoji=get_emoji("thumbs_up"),
                raw_emoji="+1",
                added=True,
            ),
            opts,
        )
        chat.process_assistant_thread_started(
            AssistantThreadStartedEvent(
                adapter=harness.adapter, thread_id="slack:D2:2.2", thread_ts="2.2", channel_id="D2", user_id="U1"
            ),
            opts,
        )
        chat.process_assistant_context_changed(
            AssistantContextChangedEvent(
                adapter=harness.adapter, thread_id="slack:D3:3.3", thread_ts="3.3", channel_id="D3", user_id="U1"
            ),
            opts,
        )
        chat.process_app_home_opened(
            AppHomeOpenedEvent(adapter=harness.adapter, channel_id="slack:D4", user_id="U1"), opts
        )
        await _drain(tasks)

        # Modals resolve related_thread first, then related_channel, then run bare.
        with patch.object(
            chat,
            "_retrieve_modal_context",
            AsyncMock(return_value={"related_channel": chat.channel("slack:C5")}),
        ):
            await chat.process_modal_submit(
                ModalSubmitEvent(adapter=harness.adapter, user=_user(), view_id="v", callback_id="cb", values={}),
                "ctx",
            )
        with patch.object(
            chat,
            "_retrieve_modal_context",
            AsyncMock(return_value={"related_channel": chat.channel("slack:C6")}),
        ):
            tasks.clear()
            chat.process_modal_close(
                ModalCloseEvent(adapter=harness.adapter, user=_user(), view_id="v", callback_id="cb"),
                "ctx",
                opts,
            )
            await _drain(tasks)

        assert seen == {
            "reaction": "slack:C1:1.1",
            "assistant_started": "slack:D2:2.2",
            "assistant_context": "slack:D3:3.3",
            "app_home": "slack:D4",
            "modal_submit": "slack:C5",
            "modal_close": "slack:C6",
        }
        assert active_conversation() is None

    async def test_modal_handlers_prefer_related_thread_then_run_bare(self, harness: _Harness):
        # Upstream ``relatedThread?.id ?? relatedChannel?.id`` (chat.ts): the
        # thread wins when both are present; neither runs the handler bare.
        chat = harness.chat
        seen: dict[str, list[str | None]] = {"submit": [], "close": []}

        async def _submit(*_args: Any, **_kwargs: Any) -> None:
            seen["submit"].append(active_conversation())

        async def _close(*_args: Any, **_kwargs: Any) -> None:
            seen["close"].append(active_conversation())

        chat.on_modal_submit(_submit)
        chat.on_modal_close(_close)
        tasks: list[Any] = []
        opts = WebhookOptions(wait_until=tasks.append)
        both = {"related_thread": chat.thread("slack:C5:5.5"), "related_channel": chat.channel("slack:C6")}
        for context in (both, {}):
            with patch.object(chat, "_retrieve_modal_context", AsyncMock(return_value=context)):
                await chat.process_modal_submit(
                    ModalSubmitEvent(adapter=harness.adapter, user=_user(), view_id="v", callback_id="cb", values={}),
                    "ctx",
                )
                tasks.clear()
                chat.process_modal_close(
                    ModalCloseEvent(adapter=harness.adapter, user=_user(), view_id="v", callback_id="cb"),
                    "ctx",
                    opts,
                )
                await _drain(tasks)

        assert seen == {"submit": ["slack:C5:5.5", None], "close": ["slack:C5:5.5", None]}
        assert active_conversation() is None
