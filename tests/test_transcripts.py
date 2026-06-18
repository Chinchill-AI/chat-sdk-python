"""Faithful translation of transcripts.test.ts.

Tests for TranscriptsApiImpl: append, list, count, delete, eviction, and
formatted round-trip behavior.

TS file: packages/chat/src/transcripts.test.ts
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from chat_sdk.shared.markdown_parser import parse_markdown
from chat_sdk.testing import MockStateAdapter, create_mock_state, create_test_message
from chat_sdk.transcripts import TranscriptsApiImpl
from chat_sdk.types import (
    AppendInput,
    AppendOptions,
    CountQuery,
    DeleteTarget,
    ListQuery,
    TranscriptsConfig,
)

UUID_RE = re.compile(r"^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$")
USER_KEY_REQUIRED_RE = r"options\.user_key is required"
INVALID_DURATION_RE = r"Invalid duration"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _ThreadStub:
    """Minimal Postable stand-in (mirrors the TS test's cast stub)."""

    adapter: Any
    id: str


def create_test_thread(adapter_name: str = "slack", thread_id: str = "slack:C123:1234.5678") -> _ThreadStub:
    return _ThreadStub(adapter=SimpleNamespace(name=adapter_name), id=thread_id)


def _record_append_to_list_calls(state: MockStateAdapter) -> list[tuple[str, Any, int | None, int | None]]:
    """Wrap ``state.append_to_list`` to record ``(key, value, max_length, ttl_ms)``.

    Python stand-in for the TS mock state's ``vi.fn()``-wrapped ``appendToList``.
    """
    calls: list[tuple[str, Any, int | None, int | None]] = []
    real_append = state.append_to_list

    async def _recording_append(
        key: str, value: Any, *, max_length: int | None = None, ttl_ms: int | None = None
    ) -> None:
        calls.append((key, value, max_length, ttl_ms))
        await real_append(key, value, max_length=max_length, ttl_ms=ttl_ms)

    state.append_to_list = _recording_append
    return calls


@pytest.fixture
def state() -> MockStateAdapter:
    return create_mock_state()


@pytest.fixture
def api(state: MockStateAdapter) -> TranscriptsApiImpl:
    return TranscriptsApiImpl(state, TranscriptsConfig())


async def _seed(api: TranscriptsApiImpl, user_key: str, count: int = 5) -> None:
    thread = create_test_thread()
    for i in range(count):
        msg = create_test_message(f"m{i}", f"msg {i}")
        msg.user_key = user_key
        await api.append(thread, msg)


# ---------------------------------------------------------------------------
# append
# ---------------------------------------------------------------------------


class TestAppend:
    # TS: "persists a Message under the resolved userKey"
    async def test_persists_a_message_under_the_resolved_userkey(self, state, api):
        append_calls = _record_append_to_list_calls(state)
        thread = create_test_thread()
        msg = create_test_message("m1", "Hello")
        msg.user_key = "user@example.com"

        stored = await api.append(thread, msg)

        assert stored is not None
        assert stored.user_key == "user@example.com"
        assert stored.text == "Hello"
        assert stored.role == "user"
        assert stored.platform == "slack"
        assert stored.thread_id == "slack:C123:1234.5678"
        assert stored.platform_message_id == "m1"
        assert UUID_RE.match(stored.id)
        assert stored.timestamp > 0

        assert len(append_calls) == 1
        key, value, max_length, ttl_ms = append_calls[0]
        assert key == "transcripts:user:user@example.com"
        assert value["userKey"] == "user@example.com"
        assert max_length == 200
        assert ttl_ms is None

    # TS: "no-ops when Message has no userKey"
    async def test_noops_when_message_has_no_userkey(self, state, api):
        append_calls = _record_append_to_list_calls(state)
        thread = create_test_thread()
        msg = create_test_message("m1", "Hello")
        # user_key deliberately not set

        stored = await api.append(thread, msg)

        assert stored is None
        assert append_calls == []

    # TS: "requires options.userKey when appending an AppendInput"
    async def test_requires_optionsuserkey_when_appending_an_appendinput(self, api):
        thread = create_test_thread()

        with pytest.raises(ValueError, match=USER_KEY_REQUIRED_RE):
            await api.append(thread, AppendInput(role="assistant", text="hi"))

    # TS: "appends an assistant message with explicit userKey"
    async def test_appends_an_assistant_message_with_explicit_userkey(self, api):
        thread = create_test_thread()

        stored = await api.append(
            thread,
            AppendInput(role="assistant", text="Hello, Mike"),
            AppendOptions(user_key="mike@acme.com"),
        )

        assert stored is not None
        assert stored.role == "assistant"
        assert stored.user_key == "mike@acme.com"
        assert stored.text == "Hello, Mike"
        assert stored.platform_message_id is None

    # TS: "omits formatted by default"
    async def test_omits_formatted_by_default(self, api):
        thread = create_test_thread()
        msg = create_test_message("m1", "Hello")
        msg.user_key = "u1"

        stored = await api.append(thread, msg)

        assert stored is not None
        assert stored.formatted is None

    # TS: "includes formatted when storeFormatted is true"
    async def test_includes_formatted_when_storeformatted_is_true(self, state):
        api = TranscriptsApiImpl(state, TranscriptsConfig(store_formatted=True))
        thread = create_test_thread()
        msg = create_test_message("m1", "**bold**")
        msg.user_key = "u1"

        stored = await api.append(thread, msg)

        assert stored is not None
        assert stored.formatted is not None
        assert stored.formatted["type"] == "root"

        round_trip = await api.list(ListQuery(user_key="u1"))
        assert round_trip[0].formatted == stored.formatted

    # TS: "passes retention duration string through as ttlMs"
    async def test_passes_retention_duration_string_through_as_ttlms(self, state):
        api = TranscriptsApiImpl(state, TranscriptsConfig(retention="7d"))
        append_calls = _record_append_to_list_calls(state)
        thread = create_test_thread()
        msg = create_test_message("m1", "Hello")
        msg.user_key = "u1"

        await api.append(thread, msg)

        assert len(append_calls) == 1
        key, _value, max_length, ttl_ms = append_calls[0]
        assert key == "transcripts:user:u1"
        assert max_length == 200
        assert ttl_ms == 7 * 24 * 60 * 60 * 1000

    # TS: "passes numeric retention through unchanged"
    async def test_passes_numeric_retention_through_unchanged(self, state):
        api = TranscriptsApiImpl(state, TranscriptsConfig(retention=60_000, max_per_user=50))
        append_calls = _record_append_to_list_calls(state)
        thread = create_test_thread()
        msg = create_test_message("m1", "Hello")
        msg.user_key = "u1"

        await api.append(thread, msg)

        assert len(append_calls) == 1
        key, _value, max_length, ttl_ms = append_calls[0]
        assert key == "transcripts:user:u1"
        assert max_length == 50
        assert ttl_ms == 60_000

    # TS: "rejects malformed duration strings"
    def test_rejects_malformed_duration_strings(self, state):
        with pytest.raises(ValueError, match=INVALID_DURATION_RE):
            TranscriptsApiImpl(state, TranscriptsConfig(retention="7days"))


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    # TS: "returns all messages in chronological order by default"
    async def test_returns_all_messages_in_chronological_order_by_default(self, api):
        await _seed(api, "u1")

        listed = await api.list(ListQuery(user_key="u1"))

        assert len(listed) == 5
        assert [m.text for m in listed] == ["msg 0", "msg 1", "msg 2", "msg 3", "msg 4"]

    # TS: "returns empty array when no messages exist"
    async def test_returns_empty_array_when_no_messages_exist(self, api):
        listed = await api.list(ListQuery(user_key="nobody"))
        assert listed == []

    # TS: "returns the newest N when limit is set, still chronological"
    async def test_returns_the_newest_n_when_limit_is_set_still_chronological(self, api):
        await _seed(api, "u1")

        listed = await api.list(ListQuery(user_key="u1", limit=2))

        assert len(listed) == 2
        assert [m.text for m in listed] == ["msg 3", "msg 4"]

    # TS: "filters by platform"
    async def test_filters_by_platform(self, api):
        slack_thread = create_test_thread("slack")
        discord_thread = create_test_thread("discord", "discord:C:T")
        slack_msg = create_test_message("s1", "from slack")
        slack_msg.user_key = "u1"
        discord_msg = create_test_message("d1", "from discord")
        discord_msg.user_key = "u1"

        await api.append(slack_thread, slack_msg)
        await api.append(discord_thread, discord_msg)

        slack_only = await api.list(ListQuery(user_key="u1", platforms=["slack"]))
        assert len(slack_only) == 1
        assert slack_only[0].platform == "slack"

    # TS: "filters by threadId"
    async def test_filters_by_threadid(self, api):
        thread_a = create_test_thread("slack", "slack:C:A")
        thread_b = create_test_thread("slack", "slack:C:B")
        m1 = create_test_message("m1", "thread A")
        m1.user_key = "u1"
        m2 = create_test_message("m2", "thread B")
        m2.user_key = "u1"

        await api.append(thread_a, m1)
        await api.append(thread_b, m2)

        listed = await api.list(ListQuery(user_key="u1", thread_id="slack:C:A"))
        assert len(listed) == 1
        assert listed[0].text == "thread A"

    # TS: "filters by role"
    async def test_filters_by_role(self, api):
        thread = create_test_thread()
        user_msg = create_test_message("m1", "user msg")
        user_msg.user_key = "u1"
        await api.append(thread, user_msg)
        await api.append(
            thread,
            AppendInput(role="assistant", text="bot msg"),
            AppendOptions(user_key="u1"),
        )

        user_only = await api.list(ListQuery(user_key="u1", roles=["user"]))
        assert len(user_only) == 1
        assert user_only[0].role == "user"

        assistant_only = await api.list(ListQuery(user_key="u1", roles=["assistant"]))
        assert len(assistant_only) == 1
        assert assistant_only[0].role == "assistant"


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------


class TestCount:
    # TS: "returns the total stored count for a userKey"
    async def test_returns_the_total_stored_count_for_a_userkey(self, api):
        await _seed(api, "u1", count=3)

        total = await api.count(CountQuery(user_key="u1"))
        assert total == 3

    # TS: "returns 0 for unknown userKey"
    async def test_returns_0_for_unknown_userkey(self, api):
        assert await api.count(CountQuery(user_key="nobody")) == 0


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    # TS: "clears all stored entries for a userKey and reports the count"
    async def test_clears_all_stored_entries_for_a_userkey_and_reports_the_count(self, api):
        await _seed(api, "u1", count=4)

        result = await api.delete(DeleteTarget(user_key="u1"))

        assert result.deleted == 4
        assert await api.count(CountQuery(user_key="u1")) == 0
        assert await api.list(ListQuery(user_key="u1")) == []

    # TS: "returns deleted: 0 for unknown userKey"
    async def test_returns_deleted_0_for_unknown_userkey(self, api):
        result = await api.delete(DeleteTarget(user_key="nobody"))
        assert result.deleted == 0

    # TS: "hides the tombstone from list/count after deletion"
    async def test_hides_the_tombstone_from_listcount_after_deletion(self, api):
        thread = create_test_thread()
        msg = create_test_message("m1", "before")
        msg.user_key = "u1"
        await api.append(thread, msg)
        await api.delete(DeleteTarget(user_key="u1"))

        # list and count both ignore the tombstone marker
        assert await api.list(ListQuery(user_key="u1")) == []
        assert await api.count(CountQuery(user_key="u1")) == 0

    # TS: "appends after delete behave as if the list were freshly empty"
    async def test_appends_after_delete_behave_as_if_the_list_were_freshly_empty(self, api):
        thread = create_test_thread()
        before = create_test_message("m1", "before")
        before.user_key = "u1"
        await api.append(thread, before)
        await api.delete(DeleteTarget(user_key="u1"))

        after = create_test_message("m2", "after")
        after.user_key = "u1"
        await api.append(thread, after)

        listed = await api.list(ListQuery(user_key="u1"))
        assert len(listed) == 1
        assert listed[0].text == "after"
        assert await api.count(CountQuery(user_key="u1")) == 1

    # TS: "does not double-count if delete is called twice"
    async def test_does_not_doublecount_if_delete_is_called_twice(self, api):
        thread = create_test_thread()
        msg = create_test_message("m1", "hello")
        msg.user_key = "u1"
        await api.append(thread, msg)

        first = await api.delete(DeleteTarget(user_key="u1"))
        second = await api.delete(DeleteTarget(user_key="u1"))

        assert first.deleted == 1
        assert second.deleted == 0

    # TS: "preserves invariants when append/delete/append are interleaved without awaits"
    async def test_preserves_invariants_when_appenddeleteappend_are_interleaved_without_awaits(self, api):
        thread = create_test_thread()
        before = create_test_message("m0", "before")
        before.user_key = "u1"
        await api.append(thread, before)

        post1 = create_test_message("m1", "post1")
        post1.user_key = "u1"
        post2 = create_test_message("m2", "post2")
        post2.user_key = "u1"

        await asyncio.gather(
            api.append(thread, post1),
            api.delete(DeleteTarget(user_key="u1")),
            api.append(thread, post2),
        )

        listed = await api.list(ListQuery(user_key="u1"))
        count = await api.count(CountQuery(user_key="u1"))

        # count() and list() must agree — neither should leak the tombstone.
        assert count == len(listed)
        # Whatever survives is a real entry under the right userKey, and
        # never the pre-delete entry (which delete() must have evicted).
        for entry in listed:
            assert entry.user_key == "u1"
            assert entry.text != "before"
        # Final size is bounded by the two post-delete appends.
        assert count <= 2


# ---------------------------------------------------------------------------
# maxPerUser eviction
# ---------------------------------------------------------------------------


class TestMaxPerUserEviction:
    # TS: "trims to maxPerUser via appendToList semantics"
    async def test_trims_to_maxperuser_via_appendtolist_semantics(self, state):
        api = TranscriptsApiImpl(state, TranscriptsConfig(max_per_user=3))
        await _seed(api, "u1")

        listed = await api.list(ListQuery(user_key="u1"))
        assert len(listed) == 3
        assert [m.text for m in listed] == ["msg 2", "msg 3", "msg 4"]


# ---------------------------------------------------------------------------
# formatted round-trip
# ---------------------------------------------------------------------------


class TestFormattedRoundTrip:
    # TS: "preserves mdast Root through state serialization"
    async def test_preserves_mdast_root_through_state_serialization(self, state):
        api = TranscriptsApiImpl(state, TranscriptsConfig(store_formatted=True))
        thread = create_test_thread()
        original = parse_markdown("# Hello\n\n*world*")
        msg = create_test_message("m1", "Hello world")
        msg.user_key = "u1"
        msg.formatted = original

        await api.append(thread, msg)
        listed = await api.list(ListQuery(user_key="u1"))

        assert listed[0].formatted == original
