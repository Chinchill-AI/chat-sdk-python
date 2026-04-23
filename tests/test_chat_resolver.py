"""Tests for the 3-level Chat resolver (ContextVar → global → error).

Verifies:
- chat.activate() scopes resolution to the current context
- Explicit chat= parameter beats context and global
- Multiple concurrent chats don't bleed across tasks
- reviver() uses explicit chat= instead of global singleton
- clear_chat_singleton() resets both levels
"""

from __future__ import annotations

import asyncio

import pytest

from chat_sdk import Chat, MemoryStateAdapter
from chat_sdk.channel import ChannelImpl
from chat_sdk.testing import create_mock_adapter
from chat_sdk.thread import (
    ThreadImpl,
    clear_chat_singleton,
    get_chat_singleton,
    has_chat_singleton,
)


def _make_chat(name: str = "slack") -> Chat:
    adapter = create_mock_adapter(name)
    state = MemoryStateAdapter()
    return Chat(adapters={name: adapter}, state=state, user_name=f"{name}-bot")


def _thread_json(adapter_name: str = "slack") -> dict:
    return {
        "_type": "chat:Thread",
        "id": "t1",
        "channelId": f"{adapter_name}:C1",
        "channelVisibility": "public",
        "isDM": False,
        "adapterName": adapter_name,
    }


def _channel_json(adapter_name: str = "slack") -> dict:
    return {
        "_type": "chat:Channel",
        "id": f"{adapter_name}:C1",
        "channelVisibility": "public",
        "isDM": False,
        "adapterName": adapter_name,
    }


class TestGlobalSingleton:
    """Existing register_singleton pattern still works."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_register_and_resolve(self):
        chat = _make_chat()
        chat.register_singleton()
        assert get_chat_singleton() is chat

    def test_has_singleton_false_before_register(self):
        assert not has_chat_singleton()

    def test_has_singleton_true_after_register(self):
        chat = _make_chat()
        chat.register_singleton()
        assert has_chat_singleton()

    def test_clear_resets(self):
        chat = _make_chat()
        chat.register_singleton()
        clear_chat_singleton()
        assert not has_chat_singleton()

    def test_error_when_no_singleton(self):
        with pytest.raises(RuntimeError, match="No Chat instance available"):
            get_chat_singleton()

    def test_from_json_errors_when_no_singleton(self):
        """from_json without chat= or singleton raises RuntimeError on adapter access."""
        data = _thread_json("slack")
        thread = ThreadImpl.from_json(data)
        # Thread is created but adapter resolution fails lazily
        with pytest.raises(RuntimeError, match="No Chat instance available"):
            _ = thread.adapter

    def test_from_json_with_mismatched_adapter_name_raises(self):
        """from_json with chat= but wrong adapter name fails fast."""
        chat = _make_chat("slack")
        data = _thread_json("nonexistent")
        with pytest.raises(RuntimeError, match='Adapter "nonexistent" not found'):
            ThreadImpl.from_json(data, chat=chat)


class TestActivateExceptionSafety:
    """activate() context manager resets ContextVar even after exceptions."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_contextvar_reset_after_exception(self):
        chat = _make_chat()
        with chat.activate():
            assert get_chat_singleton() is chat
            with pytest.raises(ValueError, match="test error"):
                raise ValueError("test error")
        # ContextVar should be reset after activate() exits
        assert not has_chat_singleton()


class TestContextVarActivation:
    """chat.activate() scopes resolution to the current context."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_activate_scopes_to_context(self):
        chat = _make_chat()
        with chat.activate():
            assert get_chat_singleton() is chat
        # Outside context, no singleton
        assert not has_chat_singleton()

    def test_activate_overrides_global(self):
        global_chat = _make_chat("global")
        local_chat = _make_chat("local")
        global_chat.register_singleton()

        assert get_chat_singleton() is global_chat

        with local_chat.activate():
            assert get_chat_singleton() is local_chat

        # After exit, global is restored
        assert get_chat_singleton() is global_chat

    def test_nested_activate(self):
        chat_a = _make_chat("a")
        chat_b = _make_chat("b")

        with chat_a.activate():
            assert get_chat_singleton() is chat_a
            with chat_b.activate():
                assert get_chat_singleton() is chat_b
            # Back to chat_a after inner exit
            assert get_chat_singleton() is chat_a

    def test_from_json_resolves_via_activate(self):
        chat = _make_chat("slack")
        data = _thread_json("slack")

        with chat.activate():
            thread = ThreadImpl.from_json(data)
            assert thread.adapter is not None
            assert thread.adapter.name == "slack"

    def test_from_json_eagerly_binds_so_survives_context_exit(self):
        """Thread deserialized inside activate() keeps its binding after exit."""
        chat = _make_chat("slack")

        with chat.activate():
            thread = ThreadImpl.from_json(_thread_json("slack"))
            channel = ChannelImpl.from_json(_channel_json("slack"))

        # After context exit, thread/channel should still work
        # because adapter was eagerly bound during from_json
        assert thread.adapter.name == "slack"
        assert channel.adapter.name == "slack"

    def test_from_json_eagerly_binds_prevents_wrong_chat(self):
        """Thread deserialized under chat_a doesn't resolve to chat_b later."""
        chat_a = _make_chat("a_adapter")
        chat_b = _make_chat("b_adapter")

        with chat_a.activate():
            thread = ThreadImpl.from_json(_thread_json("a_adapter"))

        # Now activate a different chat — thread should NOT re-resolve
        with chat_b.activate():
            assert thread.adapter.name == "a_adapter"  # still bound to chat_a


class TestExplicitChatParameter:
    """Explicit chat= parameter on from_json beats all fallbacks."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_explicit_chat_beats_global(self):
        global_chat = _make_chat("global")
        explicit_chat = _make_chat("explicit")
        global_chat.register_singleton()

        data = _thread_json("explicit")
        thread = ThreadImpl.from_json(data, chat=explicit_chat)
        assert thread.adapter.name == "explicit"

    def test_explicit_chat_beats_contextvar(self):
        context_chat = _make_chat("context")
        explicit_chat = _make_chat("explicit")

        data = _thread_json("explicit")
        with context_chat.activate():
            thread = ThreadImpl.from_json(data, chat=explicit_chat)
            assert thread.adapter.name == "explicit"

    def test_explicit_chat_works_without_any_singleton(self):
        chat = _make_chat("solo")
        data = _thread_json("solo")
        thread = ThreadImpl.from_json(data, chat=chat)
        assert thread.adapter.name == "solo"


class TestConcurrentIsolation:
    """Multiple concurrent chats don't bleed across tasks."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    async def test_concurrent_tasks_isolated(self):
        chat_a = _make_chat("adapter_a")
        chat_b = _make_chat("adapter_b")
        results: dict[str, str | None] = {}

        async def task(chat: Chat, adapter_name: str) -> None:
            with chat.activate():
                await asyncio.sleep(0.01)
                resolved = get_chat_singleton()
                # Verify via public API: check which adapter is registered
                a = resolved.get_adapter(adapter_name)
                results[adapter_name] = a.name if a else None

        await asyncio.gather(
            task(chat_a, "adapter_a"),
            task(chat_b, "adapter_b"),
        )

        assert results["adapter_a"] == "adapter_a"
        assert results["adapter_b"] == "adapter_b"

    async def test_concurrent_from_json_isolated(self):
        chat_a = _make_chat("aa")
        chat_b = _make_chat("bb")
        results: dict[str, str] = {}

        async def task(chat: Chat, adapter_name: str) -> None:
            data = _thread_json(adapter_name)
            with chat.activate():
                await asyncio.sleep(0.01)
                thread = ThreadImpl.from_json(data)
                results[adapter_name] = thread.adapter.name

        await asyncio.gather(
            task(chat_a, "aa"),
            task(chat_b, "bb"),
        )

        assert results["aa"] == "aa"
        assert results["bb"] == "bb"


class TestReviver:
    """reviver() uses explicit chat= instead of global singleton."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_reviver_does_not_register_singleton(self):
        chat = _make_chat()
        chat.reviver()  # invoke but don't need the return value
        # reviver() should NOT register a global singleton
        assert not has_chat_singleton()

    def test_reviver_deserializes_thread(self):
        chat = _make_chat("slack")
        reviver = chat.reviver()
        data = _thread_json("slack")
        thread = reviver("key", data)
        assert isinstance(thread, ThreadImpl)
        assert thread.adapter.name == "slack"

    def test_reviver_bound_to_specific_chat(self):
        chat_a = _make_chat("a")
        chat_b = _make_chat("b")

        reviver_a = chat_a.reviver()
        reviver_b = chat_b.reviver()

        thread_a = reviver_a("k", _thread_json("a"))
        thread_b = reviver_b("k", _thread_json("b"))

        assert thread_a.adapter.name == "a"
        assert thread_b.adapter.name == "b"

    def test_reviver_passes_through_non_typed_values(self):
        chat = _make_chat()
        reviver = chat.reviver()
        assert reviver("key", "plain string") == "plain string"
        assert reviver("key", 42) == 42
        assert reviver("key", {"no_type": True}) == {"no_type": True}

    def test_reviver_deserializes_channel(self):
        chat = _make_chat("slack")
        reviver = chat.reviver()
        data = _channel_json("slack")
        channel = reviver("key", data)
        assert isinstance(channel, ChannelImpl)
        assert channel.adapter.name == "slack"

    def test_reviver_channel_bound_to_specific_chat(self):
        chat_a = _make_chat("a")
        chat_b = _make_chat("b")

        reviver_a = chat_a.reviver()
        reviver_b = chat_b.reviver()

        ch_a = reviver_a("k", _channel_json("a"))
        ch_b = reviver_b("k", _channel_json("b"))

        assert ch_a.adapter.name == "a"
        assert ch_b.adapter.name == "b"


class TestChannelImplResolver:
    """Symmetric ChannelImpl coverage for the 3-level resolver."""

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_explicit_chat_parameter(self):
        chat = _make_chat("slack")
        data = _channel_json("slack")
        channel = ChannelImpl.from_json(data, chat=chat)
        assert channel.adapter.name == "slack"

    def test_explicit_chat_beats_global(self):
        global_chat = _make_chat("global")
        explicit_chat = _make_chat("explicit")
        global_chat.register_singleton()

        channel = ChannelImpl.from_json(_channel_json("explicit"), chat=explicit_chat)
        assert channel.adapter.name == "explicit"

    def test_activate_resolves_channel(self):
        chat = _make_chat("slack")
        with chat.activate():
            channel = ChannelImpl.from_json(_channel_json("slack"))
            assert channel.adapter.name == "slack"

    def test_explicit_chat_without_singleton(self):
        chat = _make_chat("solo")
        channel = ChannelImpl.from_json(_channel_json("solo"), chat=chat)
        assert channel.adapter.name == "solo"

    def test_explicit_chat_beats_contextvar(self):
        context_chat = _make_chat("context")
        explicit_chat = _make_chat("explicit")

        with context_chat.activate():
            channel = ChannelImpl.from_json(_channel_json("explicit"), chat=explicit_chat)
            assert channel.adapter.name == "explicit"

    def test_mismatched_adapter_raises(self):
        chat = _make_chat("slack")
        with pytest.raises(RuntimeError, match='Adapter "nonexistent" not found'):
            ChannelImpl.from_json(_channel_json("nonexistent"), chat=chat)

    def test_from_json_errors_when_no_singleton(self):
        channel = ChannelImpl.from_json(_channel_json("slack"))
        with pytest.raises(RuntimeError, match="No Chat instance available"):
            _ = channel.adapter

    def test_eager_bind_survives_context_exit(self):
        chat = _make_chat("slack")
        with chat.activate():
            channel = ChannelImpl.from_json(_channel_json("slack"))
        assert channel.adapter.name == "slack"

    def test_eager_bind_prevents_wrong_chat(self):
        chat_a = _make_chat("a_adapter")
        chat_b = _make_chat("b_adapter")

        with chat_a.activate():
            channel = ChannelImpl.from_json(_channel_json("a_adapter"))

        with chat_b.activate():
            assert channel.adapter.name == "a_adapter"

    async def test_concurrent_channel_isolation(self):
        chat_a = _make_chat("ca")
        chat_b = _make_chat("cb")
        results: dict[str, str] = {}

        async def task(chat: Chat, name: str) -> None:
            with chat.activate():
                await asyncio.sleep(0.01)
                channel = ChannelImpl.from_json(_channel_json(name))
                results[name] = channel.adapter.name

        await asyncio.gather(task(chat_a, "ca"), task(chat_b, "cb"))
        assert results["ca"] == "ca"
        assert results["cb"] == "cb"


class TestChatThreadFactory:
    """``Chat.thread(thread_id)`` — public worker-reconstruction path (issue #46).

    Mirrors ``chat.thread(threadId)`` from the upstream TS SDK. Lets worker
    processes rebuild a Thread bound to this Chat's state and the adapter
    inferred from the thread ID prefix — no need to reach into
    ``ThreadImpl`` / ``_ThreadImplConfig`` directly.
    """

    def setup_method(self):
        clear_chat_singleton()

    def teardown_method(self):
        clear_chat_singleton()

    def test_infers_adapter_from_thread_id_prefix(self):
        chat = _make_chat("slack")
        thread = chat.thread("slack:C123:1234567890.123456")
        assert thread.adapter.name == "slack"
        assert thread.id == "slack:C123:1234567890.123456"

    def test_propagates_explicit_current_message(self):
        """Slack native streaming reads ``current_message`` to populate
        recipient IDs; the public factory must let workers supply it.
        """
        from datetime import datetime, timezone

        from chat_sdk import Author, Message, MessageMetadata

        chat = _make_chat("slack")
        thread_id = "slack:C123:1234567890.123456"
        msg = Message(
            id="M1",
            thread_id=thread_id,
            text="hi",
            formatted={"type": "root", "children": []},
            raw=None,
            author=Author(user_id="U1", user_name="alice", full_name="Alice", is_bot=False, is_me=False),
            # Fixed timestamp — `datetime.now()` makes tests non-deterministic.
            metadata=MessageMetadata(date_sent=datetime(2024, 1, 1, tzinfo=timezone.utc), edited=False),
        )
        thread = chat.thread(thread_id, current_message=msg)
        assert thread._current_message is msg

    def test_omitting_current_message_stubs_a_placeholder(self):
        """Workers that only post (no streaming) can omit ``current_message``."""
        chat = _make_chat("slack")
        thread = chat.thread("slack:C123:1234567890.123456")
        assert thread._current_message is not None
        assert thread._current_message.id == ""

    def test_reuses_parent_chat_state_and_history(self):
        """The factory must bind the new Thread to the parent Chat's state
        adapter and message history. This is the core contract of
        `chat.thread()` — worker processes reconstruct a Thread that
        shares state with the original Chat instance, not a fresh
        detached thread. Without this, state writes/reads from the worker
        wouldn't be visible to the Chat in-process.

        For adapters that don't persist message history (persist_message_history
        falsy), `_create_thread` intentionally skips the history bind —
        there's no cache to share. This test uses a persisting adapter to
        exercise the positive case.
        """
        chat = _make_chat("slack")
        # Opt the mock adapter into message history so the factory binds it
        adapter = chat._adapters["slack"]
        adapter.persist_message_history = True

        thread = chat.thread("slack:C123:1234567890.123456")
        # State adapter must be the exact same instance, not a copy
        assert thread._state_adapter is chat._state_adapter
        # Message history must be the exact same instance too
        assert thread._message_history is chat._message_history

    def test_omits_history_when_adapter_does_not_persist(self):
        """Adapters with `persist_message_history=False`/None opt out of
        the shared history cache. `Chat.thread()` respects that: the
        Thread gets `None` for history rather than a shared cache the
        adapter won't populate.
        """
        chat = _make_chat("slack")
        adapter = chat._adapters["slack"]
        assert not adapter.persist_message_history  # default on mock is None

        thread = chat.thread("slack:C123:1234567890.123456")
        assert thread._state_adapter is chat._state_adapter
        assert thread._message_history is None

    def test_invalid_thread_id_raises(self):
        from chat_sdk.errors import ChatError

        chat = _make_chat("slack")
        with pytest.raises(ChatError, match="Invalid thread ID"):
            chat.thread("no-colon-here")

    def test_empty_remainder_raises(self):
        """`slack:` or `slack::` would create a thread with empty channel
        ID that blows up on the first adapter call — surface the error
        at construction time instead.
        """
        from chat_sdk.errors import ChatError

        chat = _make_chat("slack")
        with pytest.raises(ChatError, match="Invalid thread ID"):
            chat.thread("slack:")
        with pytest.raises(ChatError, match="Invalid thread ID"):
            chat.thread("slack::")

    def test_unregistered_adapter_raises(self):
        from chat_sdk.errors import ChatError

        chat = _make_chat("slack")
        with pytest.raises(ChatError, match='Adapter "teams" not found'):
            chat.thread("teams:foo:bar")
