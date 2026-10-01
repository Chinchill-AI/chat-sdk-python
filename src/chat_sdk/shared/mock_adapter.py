"""Mock adapter and state for testing the Chat orchestrator.

Python port of mock-adapter.ts.
Provides ``MockAdapter``, ``MockStateAdapter``, ``create_mock_adapter``,
``create_mock_state``, ``create_test_message``, ``create_mock_chat_instance``
and ``mock_logger``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from chat_sdk.types import (
    AdapterPostableMessage,
    Author,
    ChannelInfo,
    ChannelVisibility,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    ListThreadsResult,
    Lock,
    Message,
    MessageMetadata,
    QueueEntry,
    RawMessage,
    ThreadInfo,
    WebhookOptions,
)

# ---------------------------------------------------------------------------
# Mock Logger
# ---------------------------------------------------------------------------


class _CallRecorder:
    """Records calls made to the mock."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


@dataclass
class MockLogger:
    """Logger that captures all log calls."""

    debug: _CallRecorder = field(default_factory=_CallRecorder)
    info: _CallRecorder = field(default_factory=_CallRecorder)
    warn: _CallRecorder = field(default_factory=_CallRecorder)
    error: _CallRecorder = field(default_factory=_CallRecorder)

    def child(self, prefix: str) -> MockLogger:
        return self


mock_logger = MockLogger()


# ---------------------------------------------------------------------------
# Mock Adapter
# ---------------------------------------------------------------------------


@dataclass
class _RawMsg:
    id: str
    thread_id: str | None = None
    raw: Any = field(default_factory=dict)


class MockAdapter:
    """Mock adapter for testing. Records all operations."""

    def __init__(self, name: str = "slack") -> None:
        self.name = name
        self.user_name = f"{name}-bot"
        self.bot_user_id: str | None = None
        self.lock_scope = None
        # Deprecated alias read alongside persist_thread_history; tests set
        # either flag per-instance (mirrors the upstream mock, where neither
        # is defined until a test assigns it).
        self.persist_message_history = None
        self.persist_thread_history = None

        # Call recorders
        self._post_calls: list[tuple[str, AdapterPostableMessage]] = []
        self._edit_calls: list[tuple[str, str, AdapterPostableMessage]] = []
        self._delete_calls: list[tuple[str, str]] = []
        self._add_reaction_calls: list[tuple[str, str, Any]] = []
        self._remove_reaction_calls: list[tuple[str, str, Any]] = []
        self._start_typing_calls: list[tuple[str, str | None]] = []
        self._fetch_calls: list[tuple[str, FetchOptions | None]] = []
        self._initialize_calls: list[Any] = []

    async def initialize(self, chat: Any) -> None:
        self._initialize_calls.append(chat)

    async def disconnect(self) -> None:
        pass

    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> Any:
        return "ok"

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage:
        self._post_calls.append((thread_id, message))
        return RawMessage(id="msg-1", thread_id=thread_id, raw={})

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        self._edit_calls.append((thread_id, message_id, message))
        return RawMessage(id=message_id, thread_id=thread_id, raw={})

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        self._delete_calls.append((thread_id, message_id))

    async def add_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        self._add_reaction_calls.append((thread_id, message_id, emoji))

    async def remove_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None:
        self._remove_reaction_calls.append((thread_id, message_id, emoji))

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        self._start_typing_calls.append((thread_id, status))

    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult:
        self._fetch_calls.append((thread_id, options))
        return FetchResult(messages=[], next_cursor=None)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        return ThreadInfo(id=thread_id, channel_id="c1", metadata={})

    async def fetch_message(self, thread_id: str, message_id: str) -> Message | None:
        return None

    def encode_thread_id(self, platform_data: Any) -> str:
        channel = platform_data.get("channel", "")
        thread = platform_data.get("thread", "")
        return f"{self.name}:{channel}:{thread}"

    def decode_thread_id(self, thread_id: str) -> dict[str, str]:
        parts = thread_id.split(":")
        return {"channel": parts[1] if len(parts) > 1 else "", "thread": parts[2] if len(parts) > 2 else ""}

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        return ":".join(thread_id.split(":")[:2])

    def parse_message(self, raw: Any) -> Message:
        from chat_sdk.errors import ChatNotImplementedError

        raise ChatNotImplementedError("mock", "parse_message")

    def render_formatted(self, content: FormattedContent) -> str:
        return "formatted"

    async def open_dm(self, user_id: str) -> str:
        return f"{self.name}:D{user_id}:"

    def is_dm(self, thread_id: str) -> bool:
        return ":D" in thread_id

    def get_channel_visibility(self, thread_id: str) -> ChannelVisibility:
        return "unknown"

    async def open_modal(
        self,
        trigger_id: str = "",
        modal: Any = None,
        context_id: str | None = None,
    ) -> dict[str, str]:
        return {"view_id": "V123"}

    async def fetch_channel_messages(self, channel_id: str, options: FetchOptions | None = None) -> FetchResult:
        return FetchResult(messages=[], next_cursor=None)

    async def list_threads(self, channel_id: str, **kwargs: Any) -> ListThreadsResult:
        return ListThreadsResult(threads=[], next_cursor=None)

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        return ChannelInfo(
            id=channel_id,
            name=f"#{channel_id}",
            is_dm=False,
            metadata={},
        )

    async def post_channel_message(self, channel_id: str, message: AdapterPostableMessage) -> RawMessage:
        # Channel-scoped posts don't have a thread — use the channel id as
        # the identifier so downstream code that expects a non-null
        # `thread_id` can still route by string key.
        return RawMessage(id="msg-1", thread_id=channel_id, raw={})

    async def get_user(self, user_id: str) -> Any:
        """Default mock implementation raises ``ChatNotImplementedError``.

        Tests that exercise :meth:`Chat.get_user` should override this on
        a per-instance basis (e.g. ``adapter.get_user = AsyncMock(...)``)
        to mirror the upstream pattern of attaching ``vi.fn()`` per test.
        Mirrors the upstream Vitest mock, where ``getUser`` is undefined
        until the test explicitly sets it.
        """
        from chat_sdk.errors import ChatNotImplementedError

        raise ChatNotImplementedError(self.name, "getUser")


# ---------------------------------------------------------------------------
# Mock State Adapter
# ---------------------------------------------------------------------------


class MockStateAdapter:
    """Mock state adapter with working in-memory storage.

    Has working subscriptions, locks, cache, and queues.
    Includes a ``cache`` attribute for direct access to stored values.
    """

    def __init__(self) -> None:
        self.cache: dict[str, Any] = {}
        self._subscriptions: set[str] = set()
        self._locks: dict[str, Lock] = {}
        self._queues: dict[str, list[QueueEntry]] = {}

        # Call recorders for verifying arguments in tests
        self._acquire_lock_calls: list[tuple[str, int]] = []
        self._force_release_lock_calls: list[str] = []
        self._enqueue_calls: list[tuple[str, QueueEntry, int]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def subscribe(self, thread_id: str) -> None:
        self._subscriptions.add(thread_id)

    async def unsubscribe(self, thread_id: str) -> None:
        self._subscriptions.discard(thread_id)

    async def is_subscribed(self, thread_id: str) -> bool:
        return thread_id in self._subscriptions

    async def acquire_lock(self, thread_id: str, ttl_ms: int) -> Lock | None:
        self._acquire_lock_calls.append((thread_id, ttl_ms))
        if thread_id in self._locks:
            return None
        lock = Lock(
            thread_id=thread_id,
            token="test-token",
            expires_at=int(time.time() * 1000) + ttl_ms,
        )
        self._locks[thread_id] = lock
        return lock

    async def force_release_lock(self, thread_id: str) -> None:
        self._force_release_lock_calls.append(thread_id)
        self._locks.pop(thread_id, None)

    async def release_lock(self, lock: Lock) -> None:
        self._locks.pop(lock.thread_id, None)

    async def extend_lock(self, lock: Lock, ttl_ms: int) -> bool:
        return True

    async def get(self, key: str) -> Any | None:
        return self.cache.get(key)

    async def set(self, key: str, value: Any, ttl_ms: int | None = None) -> None:
        self.cache[key] = value

    async def set_if_not_exists(self, key: str, value: Any, ttl_ms: int | None = None) -> bool:
        if key in self.cache:
            return False
        self.cache[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.cache.pop(key, None)

    async def append_to_list(
        self,
        key: str,
        value: Any,
        *,
        max_length: int | None = None,
        ttl_ms: int | None = None,
    ) -> None:
        lst: list[Any] = self.cache.get(key, [])
        lst.append(value)
        if max_length and len(lst) > max_length:
            lst = lst[len(lst) - max_length :]
        self.cache[key] = lst

    async def get_list(self, key: str) -> list[Any]:
        return self.cache.get(key, [])

    async def enqueue(self, thread_id: str, entry: QueueEntry, max_size: int) -> int:
        self._enqueue_calls.append((thread_id, entry, max_size))
        queue = self._queues.setdefault(thread_id, [])
        queue.append(entry)
        if len(queue) > max_size:
            del queue[: len(queue) - max_size]
        return len(queue)

    async def dequeue(self, thread_id: str) -> QueueEntry | None:
        queue = self._queues.get(thread_id)
        if not queue:
            return None
        entry = queue.pop(0)
        if not queue:
            del self._queues[thread_id]
        return entry

    async def queue_depth(self, thread_id: str) -> int:
        queue = self._queues.get(thread_id)
        return len(queue) if queue else 0


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def create_mock_adapter(name: str = "slack") -> MockAdapter:
    """Create a mock adapter for testing."""
    return MockAdapter(name)


def create_mock_state() -> MockStateAdapter:
    """Create a mock state adapter for testing."""
    return MockStateAdapter()


def create_test_message(
    id: str,
    text: str,
    **overrides: Any,
) -> Message:
    """Create a test message for testing.

    Parameters
    ----------
    id:
        Message ID.
    text:
        Message text content.
    **overrides:
        Optional overrides for message fields.
    """
    defaults: dict[str, Any] = {
        "id": id,
        "thread_id": "slack:C123:1234.5678",
        "text": text,
        "formatted": {"type": "root", "children": []},
        "raw": {},
        "author": Author(
            user_id="U123",
            user_name="testuser",
            full_name="Test User",
            is_bot=False,
            is_me=False,
        ),
        "metadata": MessageMetadata(
            date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
            edited=False,
        ),
        "attachments": [],
        "links": [],
    }
    defaults.update(overrides)
    return Message(**defaults)


# ---------------------------------------------------------------------------
# Mock ChatInstance
# ---------------------------------------------------------------------------

# Fire-and-forget processors: plain ``MagicMock`` (the real ones return a task
# or ``None``; adapters never await them).
_MOCK_CHAT_SYNC_PROCESSORS = (
    "process_message",
    "process_message_updated",
    "process_message_deleted",
    "process_reaction",
    "process_action",
    "process_modal_close",
    "process_slash_command",
    "process_installed",
    "process_uninstalled",
    "process_member_joined_channel",
    "process_app_home_opened",
    "process_assistant_thread_started",
    "process_assistant_context_changed",
    "process_app_context_changed",
)
# Processors adapters await (they return a value to the platform).
_MOCK_CHAT_ASYNC_PROCESSORS = (
    "handle_incoming_message",
    "process_options_load",
    "process_modal_submit",
)


def create_mock_chat_instance(
    *,
    state: Any = None,
    logger: Any = None,
    user_name: str = "test-bot",
    overrides: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Create a mock ``ChatInstance`` for adapter tests.

    Port of upstream ``createMockChatInstance`` (``packages/tests``). Every
    ``process_*`` hook is a recording mock — ``MagicMock`` for the
    fire-and-forget ones, ``AsyncMock`` (resolving to ``None``) for
    ``handle_incoming_message``, ``process_options_load`` and
    ``process_modal_submit`` — so tests can assert an adapter dispatched
    through the right hook. Includes the optional ``process_installed`` /
    ``process_uninstalled``. ``get_state()`` / ``get_user_name()`` /
    ``get_logger()`` return the supplied (or default) state, user name and
    logger; ``history`` is a recording ``AsyncMock`` (``transcripts`` is
    ``history.user``); ``overrides`` replaces attributes last. Unknown attributes raise
    ``AttributeError`` (it is not a bare ``MagicMock``), so
    ``getattr(chat, "process_x", None)`` probes behave as on a real ``Chat``.
    """
    from unittest.mock import AsyncMock, MagicMock

    resolved_state = state if state is not None else create_mock_state()
    resolved_logger = logger if logger is not None else mock_logger

    attrs: dict[str, Any] = {name: MagicMock(name=name) for name in _MOCK_CHAT_SYNC_PROCESSORS}
    attrs.update({name: AsyncMock(name=name, return_value=None) for name in _MOCK_CHAT_ASYNC_PROCESSORS})
    # Upstream's factory omits ``history`` and casts ``as unknown as
    # ChatInstance``; Python's ``ChatInstance`` is ``@runtime_checkable``, so
    # without it ``isinstance(mock, ChatInstance)`` is False. ``transcripts``
    # is ``history.user``, as on a real ``Chat``.
    history = AsyncMock(name="history")
    attrs["history"] = history
    attrs["transcripts"] = history.user
    attrs["get_state"] = lambda: resolved_state
    attrs["get_user_name"] = lambda: user_name
    attrs["get_logger"] = lambda prefix=None: resolved_logger
    if overrides:
        attrs.update(overrides)
    return SimpleNamespace(**attrs)
