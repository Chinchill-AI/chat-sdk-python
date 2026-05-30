"""Tests for chat_sdk.types core data types."""

from __future__ import annotations

import asyncio
import gc
import weakref
from datetime import datetime, timezone

import chat_sdk.types as types_module
from chat_sdk.types import (
    Attachment,
    Author,
    EmojiFormats,
    EmojiValue,
    Message,
    MessageMetadata,
    MessageSubject,
    MessageSubjectParty,
    RawMessage,
    set_message_adapter,
)


class TestEmojiValue:
    """Tests for EmojiValue dataclass."""

    def test_creation(self):
        emoji = EmojiValue(name="thumbs_up")
        assert emoji.name == "thumbs_up"

    def test_str_produces_placeholder(self):
        emoji = EmojiValue(name="wave")
        assert str(emoji) == "{{emoji:wave}}"

    def test_to_json_produces_placeholder(self):
        emoji = EmojiValue(name="heart")
        assert emoji.to_json() == "{{emoji:heart}}"

    def test_frozen(self):
        emoji = EmojiValue(name="fire")
        try:
            emoji.name = "ice"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass

    def test_equality(self):
        a = EmojiValue(name="rocket")
        b = EmojiValue(name="rocket")
        assert a == b

    def test_inequality(self):
        a = EmojiValue(name="rocket")
        b = EmojiValue(name="star")
        assert a != b


class TestEmojiFormats:
    """Tests for EmojiFormats dataclass."""

    def test_defaults(self):
        fmt = EmojiFormats()
        assert fmt.slack == ""
        assert fmt.gchat == ""

    def test_string_values(self):
        fmt = EmojiFormats(slack="thumbsup", gchat="thumbs_up_emoji")
        assert fmt.slack == "thumbsup"
        assert fmt.gchat == "thumbs_up_emoji"

    def test_list_values(self):
        fmt = EmojiFormats(slack=["+1", "thumbsup"], gchat="emoji_code")
        assert isinstance(fmt.slack, list)
        assert len(fmt.slack) == 2


class TestAuthor:
    """Tests for Author dataclass."""

    def test_creation(self):
        author = Author(
            user_id="U123",
            user_name="alice",
            full_name="Alice Smith",
            is_bot=False,
            is_me=False,
        )
        assert author.user_id == "U123"
        assert author.user_name == "alice"
        assert author.full_name == "Alice Smith"
        assert author.is_bot is False
        assert author.is_me is False

    def test_bot_author(self):
        author = Author(
            user_id="B001",
            user_name="bot",
            full_name="Test Bot",
            is_bot=True,
            is_me=True,
        )
        assert author.is_bot is True
        assert author.is_me is True

    def test_unknown_bot_status(self):
        author = Author(
            user_id="U999",
            user_name="unknown",
            full_name="Unknown",
            is_bot="unknown",
            is_me=False,
        )
        assert author.is_bot == "unknown"


class TestMessageMetadata:
    """Tests for MessageMetadata dataclass."""

    def test_creation_defaults(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        meta = MessageMetadata(date_sent=dt)
        assert meta.date_sent == dt
        assert meta.edited is False
        assert meta.edited_at is None

    def test_edited_message(self):
        sent = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        edited = datetime(2024, 1, 15, 12, 5, 0, tzinfo=timezone.utc)
        meta = MessageMetadata(date_sent=sent, edited=True, edited_at=edited)
        assert meta.edited is True
        assert meta.edited_at == edited


class TestAttachment:
    """Tests for Attachment dataclass."""

    def test_image_attachment(self):
        att = Attachment(
            type="image",
            url="https://example.com/photo.jpg",
            mime_type="image/jpeg",
        )
        assert att.type == "image"
        assert att.url == "https://example.com/photo.jpg"
        assert att.mime_type == "image/jpeg"

    def test_defaults(self):
        att = Attachment(type="file")
        assert att.url is None
        assert att.name is None
        assert att.mime_type is None
        assert att.size is None
        assert att.data is None
        assert att.fetch_data is None

    def test_data_bytes_preserved_through_coerce_attachments(self):
        """``_coerce_attachments`` must not drop ``data: bytes`` from raw dicts.

        In-memory state adapters can hand us attachment dicts that still
        carry pre-fetched bytes (bypassing ``to_json`` — bytes aren't
        JSON-safe, so the serialization path explicitly omits them).
        Round-trip the dict through ``_coerce_attachments`` and assert the
        bytes survive; regression guard for the silent-data-loss bug on
        the queue/debounce rehydrate paths.
        """
        from chat_sdk.chat import _coerce_attachments

        raw = [
            {
                "type": "file",
                "url": "https://example.com/f.pdf",
                "name": "f.pdf",
                "mimeType": "application/pdf",
                "data": b"PDF-bytes",
                "fetchMetadata": {"url": "https://example.com/f.pdf"},
            }
        ]
        out = _coerce_attachments(raw)
        assert len(out) == 1
        assert isinstance(out[0], Attachment)
        assert out[0].data == b"PDF-bytes"
        assert out[0].mime_type == "application/pdf"
        assert out[0].fetch_metadata == {"url": "https://example.com/f.pdf"}

    def test_data_bytes_preserved_through_from_json(self):
        """``Message.from_json`` must preserve raw ``data`` bytes when present.

        ``to_json`` drops ``data`` (JSON can't carry bytes), so it will
        not appear on the wire — but callers that hand us a raw dict with
        both the envelope and a ``data`` field should not silently lose
        it.  Exercises the camelCase-first (``from_json``) path.
        """
        dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        raw = {
            "_type": "chat:Message",
            "id": "m1",
            "threadId": "t1",
            "text": "hi",
            "formatted": {"type": "root", "children": []},
            "author": {
                "userId": "U1",
                "userName": "a",
                "fullName": "A",
                "isBot": False,
                "isMe": False,
            },
            "metadata": {"dateSent": dt.isoformat(), "edited": False},
            "attachments": [
                {
                    "type": "file",
                    "url": "https://example.com/f.pdf",
                    "mimeType": "application/pdf",
                    "data": b"PDF-bytes",
                }
            ],
        }
        msg = Message.from_json(raw)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].data == b"PDF-bytes"
        assert msg.attachments[0].mime_type == "application/pdf"

    def test_data_bytes_preserved_through_from_json_compat(self):
        """``Message.from_json_compat`` (snake_case-first) must preserve ``data``."""
        dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        raw = {
            "id": "m1",
            "thread_id": "t1",
            "text": "hi",
            "formatted": {"type": "root", "children": []},
            "author": {
                "user_id": "U1",
                "user_name": "a",
                "full_name": "A",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {"date_sent": dt.isoformat(), "edited": False},
            "attachments": [
                {
                    "type": "file",
                    "url": "https://example.com/f.pdf",
                    "mime_type": "application/pdf",
                    "data": b"PDF-bytes",
                }
            ],
        }
        msg = Message.from_json_compat(raw)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].data == b"PDF-bytes"
        assert msg.attachments[0].mime_type == "application/pdf"

    def test_to_json_still_drops_bytes(self):
        """``to_json`` must not emit ``data`` (bytes aren't JSON-safe).

        Regression guard: propagating ``data`` through the in-memory
        rehydrate paths must not accidentally start serializing bytes
        onto the wire.
        """
        dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        msg = Message(
            id="m1",
            thread_id="t1",
            text="hi",
            formatted={"type": "root", "children": []},
            author=Author(
                user_id="U1",
                user_name="a",
                full_name="A",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=dt),
            attachments=[
                Attachment(
                    type="file",
                    url="https://example.com/f.pdf",
                    mime_type="application/pdf",
                    data=b"PDF-bytes",
                )
            ],
        )
        serialized = msg.to_json()
        assert "data" not in serialized["attachments"][0]


class TestMessage:
    """Tests for Message dataclass."""

    def test_creation(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        msg = Message(
            id="msg-001",
            thread_id="thread-001",
            text="Hello, world!",
            formatted={"type": "root", "children": []},
            author=Author(
                user_id="U1",
                user_name="alice",
                full_name="Alice",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=dt),
        )
        assert msg.id == "msg-001"
        assert msg.thread_id == "thread-001"
        assert msg.text == "Hello, world!"
        assert msg.attachments == []
        assert msg.is_mention is None
        assert msg.links is None
        assert msg.raw is None

    def test_to_json(self):
        dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        msg = Message(
            id="m1",
            thread_id="t1",
            text="test",
            formatted={},
            author=Author(
                user_id="U1",
                user_name="bob",
                full_name="Bob Jones",
                is_bot=False,
                is_me=True,
            ),
            metadata=MessageMetadata(date_sent=dt),
        )
        data = msg.to_json()
        assert data["_type"] == "chat:Message"
        assert data["id"] == "m1"
        assert data["threadId"] == "t1"
        assert data["text"] == "test"
        assert data["author"]["userName"] == "bob"
        assert data["author"]["fullName"] == "Bob Jones"
        assert data["author"]["isMe"] is True
        assert "2024-06-15" in data["metadata"]["dateSent"]
        assert data["metadata"]["edited"] is False
        assert "editedAt" not in data["metadata"]  # omitted when None

    def test_to_json_with_edited(self):
        sent = datetime(2024, 1, 1, tzinfo=timezone.utc)
        edited = datetime(2024, 1, 2, tzinfo=timezone.utc)
        msg = Message(
            id="m2",
            thread_id="t2",
            text="edited",
            formatted={},
            author=Author(
                user_id="U2",
                user_name="a",
                full_name="A",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(date_sent=sent, edited=True, edited_at=edited),
        )
        data = msg.to_json()
        assert data["metadata"]["edited"] is True
        assert data["metadata"]["editedAt"] is not None


class TestRawMessage:
    """Tests for RawMessage dataclass."""

    def test_creation(self):
        raw = RawMessage(
            id="raw-001",
            thread_id="thread-001",
            raw={"platform": "test", "data": 42},
        )
        assert raw.id == "raw-001"
        assert raw.thread_id == "thread-001"
        assert raw.raw["platform"] == "test"


def _make_message(**overrides) -> Message:
    """Build a Message for subject tests (mirrors upstream ``makeMessage``)."""
    defaults = {
        "id": "msg-1",
        "thread_id": "slack:C123:1234.5678",
        "text": "Hello world",
        "formatted": {"type": "root", "children": []},
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
        "raw": {"platform": "test"},
    }
    defaults.update(overrides)
    return Message(**defaults)


class _AdapterWithSubject:
    """Minimal adapter stub exposing ``fetch_subject`` that records call count."""

    def __init__(self, result: MessageSubject | None) -> None:
        self._result = result
        self.calls = 0

    async def fetch_subject(self, raw):  # noqa: ANN001, ANN201
        self.calls += 1
        return self._result


class TestMessageSubjectDataclass:
    """Tests for the MessageSubject / MessageSubjectParty dataclasses."""

    def test_minimal_required_fields(self):
        subject = MessageSubject(id="ENG-1", type="issue")
        assert subject.id == "ENG-1"
        assert subject.type == "issue"
        assert subject.raw is None
        assert subject.assignee is None
        assert subject.labels is None

    def test_all_fields(self):
        subject = MessageSubject(
            id="ENG-123",
            type="issue",
            raw={"k": "v"},
            assignee=MessageSubjectParty(id="u1", name="Alice"),
            author=MessageSubjectParty(id="u2", name="Bob"),
            description="A bug",
            labels=["bug", "p0"],
            status="In Progress",
            title="Fix bug",
            url="https://linear.app/team/ENG-123",
        )
        assert subject.assignee == MessageSubjectParty(id="u1", name="Alice")
        assert subject.author.name == "Bob"
        assert subject.labels == ["bug", "p0"]
        assert subject.status == "In Progress"
        assert subject.title == "Fix bug"
        assert subject.url == "https://linear.app/team/ENG-123"
        assert subject.description == "A bug"


class TestMessageSubject:
    """Tests for the Message.subject accessor (mirrors upstream message.test.ts)."""

    async def test_returns_none_when_no_adapter_is_set(self):
        msg = _make_message()
        assert await msg.subject is None

    async def test_returns_none_when_adapter_has_no_fetch_subject(self):
        msg = _make_message()
        set_message_adapter(msg, object())
        assert await msg.subject is None

    async def test_returns_subject_from_adapter(self):
        msg = _make_message()
        expected = MessageSubject(
            type="issue",
            id="ENG-123",
            title="Fix bug",
            status="In Progress",
            url="https://linear.app/team/ENG-123",
            raw={},
        )
        set_message_adapter(msg, _AdapterWithSubject(expected))
        result = await msg.subject
        assert result == expected

    async def test_should_cache_the_result(self):
        msg = _make_message()
        adapter = _AdapterWithSubject(MessageSubject(type="issue", id="1", raw={}))
        set_message_adapter(msg, adapter)
        await msg.subject
        await msg.subject
        assert adapter.calls == 1

    async def test_should_cache_null_result(self):
        msg = _make_message()
        adapter = _AdapterWithSubject(None)
        set_message_adapter(msg, adapter)
        await msg.subject
        await msg.subject
        assert adapter.calls == 1

    async def test_should_handle_concurrent_access(self):
        msg = _make_message()
        adapter = _AdapterWithSubject(MessageSubject(type="issue", id="1", raw={}))
        set_message_adapter(msg, adapter)
        a, b = await asyncio.gather(msg.subject, msg.subject)
        assert a == b
        assert adapter.calls == 1

    async def test_swallows_fetch_subject_errors(self):
        """A raising hook resolves to None (mirrors upstream .catch(() => null))."""
        msg = _make_message()

        class Boom:
            async def fetch_subject(self, raw):  # noqa: ANN001, ANN201
                raise RuntimeError("boom")

        set_message_adapter(msg, Boom())
        assert await msg.subject is None

    async def test_passes_raw_payload_to_fetch_subject(self):
        msg = _make_message(raw={"native": "payload"})
        seen = {}

        class Capturing:
            async def fetch_subject(self, raw):  # noqa: ANN001, ANN201
                seen["raw"] = raw
                return None

        set_message_adapter(msg, Capturing())
        await msg.subject
        assert seen["raw"] == {"native": "payload"}


class TestSetMessageAdapterWeakref:
    """Tests for the identity-keyed, weakly-scoped adapter registry."""

    def test_registration_does_not_crash_on_unhashable_message(self):
        # Message is a plain dataclass (eq=True) -> unhashable. The registry
        # must not rely on hashing the Message itself.
        msg = _make_message()
        with __import__("pytest").raises(TypeError):
            hash(msg)
        set_message_adapter(msg, object())  # must not raise

    def test_entry_removed_when_message_is_garbage_collected(self):
        msg = _make_message()
        set_message_adapter(msg, object())
        key = id(msg)
        assert key in types_module._message_adapter_map
        del msg
        gc.collect()
        assert key not in types_module._message_adapter_map

    def test_distinct_messages_get_distinct_adapters(self):
        m1 = _make_message()
        m2 = _make_message()
        a1, a2 = object(), object()
        set_message_adapter(m1, a1)
        set_message_adapter(m2, a2)
        assert types_module._get_message_adapter(m1) is a1
        assert types_module._get_message_adapter(m2) is a2

    @staticmethod
    def _live_finalizer_count(message: Message) -> int:
        """Count live ``weakref.finalize`` callbacks attached to ``message``.

        ``weakref.finalize`` keeps a class-level registry whose keys are the
        ``finalize`` instances; ``peek()`` returns ``(obj, func, args, kwargs)``
        while the finalizer is still alive. We count entries whose tracked
        object is ``message`` to assert exactly one cleanup is registered.
        """
        count = 0
        for finalizer in list(weakref.finalize._registry):
            peeked = finalizer.peek()
            if peeked is not None and peeked[0] is message:
                count += 1
        return count

    def test_re_registration_does_not_add_duplicate_finalizer(self):
        # Registering the same live message more than once (re-dispatch,
        # rehydrate, multiple handler passes) must not accumulate finalizers.
        msg = _make_message()
        set_message_adapter(msg, object())
        assert self._live_finalizer_count(msg) == 1
        set_message_adapter(msg, object())
        set_message_adapter(msg, object())
        assert self._live_finalizer_count(msg) == 1

    def test_re_registration_updates_adapter_value(self):
        # The adapter VALUE is still overwritten on re-registration even though
        # no second finalizer is added.
        msg = _make_message()
        adapter_a, adapter_b = object(), object()
        set_message_adapter(msg, adapter_a)
        assert types_module._get_message_adapter(msg) is adapter_a
        set_message_adapter(msg, adapter_b)
        assert types_module._get_message_adapter(msg) is adapter_b

    def test_re_registered_message_cleans_up_exactly_once(self):
        # After re-registration, GC must remove the single entry without a
        # double-pop and without leaving a stale finalizer behind.
        msg = _make_message()
        set_message_adapter(msg, object())
        set_message_adapter(msg, object())
        key = id(msg)
        assert key in types_module._message_adapter_map
        del msg
        gc.collect()
        assert key not in types_module._message_adapter_map
