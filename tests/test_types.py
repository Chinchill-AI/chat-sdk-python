"""Tests for chat_sdk.types core data types."""

from __future__ import annotations

from datetime import datetime, timezone

from chat_sdk.types import (
    Attachment,
    Author,
    EmojiFormats,
    EmojiValue,
    Message,
    MessageMetadata,
    RawMessage,
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
