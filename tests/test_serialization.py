"""Tests for Message.to_json/from_json round-trip serialization.

Covers: type tags, date handling, author fields, attachments (without
non-serializable fields), is_mention, links, and round-trip integrity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from chat_sdk.testing import (
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import (
    Attachment,
    Author,
    LinkPreview,
    Message,
    MessageMetadata,
)


# ============================================================================
# Message.to_json()
# ============================================================================


class TestMessageToJson:
    """Tests for Message.to_json()."""

    def test_correct_type_tag(self):
        message = create_test_message("msg-1", "Hello world")
        data = message.to_json()

        assert data["_type"] == "chat:Message"
        assert data["id"] == "msg-1"
        assert data["text"] == "Hello world"

    def test_converts_date_to_iso_string(self):
        message = create_test_message(
            "msg-1",
            "Test",
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
                edited=True,
                edited_at=datetime(2024, 1, 15, 11, 0, 0, tzinfo=timezone.utc),
            ),
        )
        data = message.to_json()

        assert data["metadata"]["date_sent"] == "2024-01-15T10:30:00+00:00"
        assert data["metadata"]["edited_at"] == "2024-01-15T11:00:00+00:00"

    def test_handles_none_edited_at(self):
        message = create_test_message(
            "msg-1",
            "Test",
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
                edited=False,
            ),
        )
        data = message.to_json()
        # edited_at is omitted from the dict when None (not set to null)
        assert "edited_at" not in data["metadata"]

    def test_serializes_author(self):
        message = create_test_message("msg-1", "Test")
        data = message.to_json()

        assert data["author"] == {
            "user_id": "U123",
            "user_name": "testuser",
            "full_name": "Test User",
            "is_bot": False,
            "is_me": False,
        }

    def test_serializes_attachments_without_data_or_fetch_data(self):
        async def fetch() -> bytes:
            return b"test"

        message = create_test_message(
            "msg-1",
            "Test",
            attachments=[
                Attachment(
                    type="image",
                    url="https://example.com/image.png",
                    name="image.png",
                    mime_type="image/png",
                    size=1024,
                    width=800,
                    height=600,
                    data=b"test",
                    fetch_data=fetch,
                ),
            ],
        )
        data = message.to_json()

        assert len(data["attachments"]) == 1
        att = data["attachments"][0]
        assert att["type"] == "image"
        assert att["url"] == "https://example.com/image.png"
        assert att["name"] == "image.png"
        assert att["mime_type"] == "image/png"
        assert att["size"] == 1024
        assert att["width"] == 800
        assert att["height"] == 600
        # data and fetch_data should NOT be present
        assert "data" not in att or att.get("data") is None  # None is ok since it's not callable
        assert "fetch_data" not in att

    def test_serializes_is_mention(self):
        message = create_test_message("msg-1", "Test", is_mention=True)
        data = message.to_json()
        assert data["is_mention"] is True

    def test_serializes_links_without_fetch_message(self):
        async def fetch_linked() -> Message:
            return create_test_message("linked", "linked")

        message = create_test_message(
            "msg-1",
            "Check this out",
            links=[
                LinkPreview(
                    url="https://example.com",
                    title="Example",
                    fetch_message=fetch_linked,
                ),
                LinkPreview(url="https://vercel.com", site_name="Vercel"),
            ],
        )
        data = message.to_json()

        assert len(data["links"]) == 2
        # _strip_none removes None values from link dicts
        assert data["links"][0] == {
            "url": "https://example.com",
            "title": "Example",
        }
        assert data["links"][1] == {
            "url": "https://vercel.com",
            "site_name": "Vercel",
        }
        # fetch_message should NOT be in serialized output
        assert "fetch_message" not in data["links"][0]

    def test_omits_links_when_empty(self):
        message = create_test_message("msg-1", "No links", links=[])
        data = message.to_json()
        # links key should not be present when links is empty list
        assert "links" not in data

    def test_json_serializable(self):
        message = create_test_message("msg-1", "Hello **world**")
        data = message.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)

        assert parsed["_type"] == "chat:Message"
        assert parsed["text"] == "Hello **world**"


# ============================================================================
# Message.from_json()
# ============================================================================


class TestMessageFromJson:
    """Tests for Message.from_json()."""

    def test_restore_from_json(self):
        data = {
            "_type": "chat:Message",
            "id": "msg-1",
            "thread_id": "slack:C123:1234.5678",
            "text": "Hello world",
            "formatted": {"type": "root", "children": []},
            "raw": {"some": "data"},
            "author": {
                "user_id": "U123",
                "user_name": "testuser",
                "full_name": "Test User",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {
                "date_sent": "2024-01-15T10:30:00+00:00",
                "edited": False,
            },
            "attachments": [],
        }
        message = Message.from_json(data)

        assert message.id == "msg-1"
        assert message.text == "Hello world"
        assert message.author.user_name == "testuser"

    def test_converts_iso_strings_to_datetime(self):
        data = {
            "_type": "chat:Message",
            "id": "msg-1",
            "thread_id": "slack:C123:1234.5678",
            "text": "Test",
            "formatted": {"type": "root", "children": []},
            "raw": {},
            "author": {
                "user_id": "U123",
                "user_name": "testuser",
                "full_name": "Test User",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {
                "date_sent": "2024-01-15T10:30:00+00:00",
                "edited": True,
                "edited_at": "2024-01-15T11:00:00+00:00",
            },
            "attachments": [],
        }
        message = Message.from_json(data)

        assert isinstance(message.metadata.date_sent, datetime)
        assert message.metadata.date_sent.isoformat() == "2024-01-15T10:30:00+00:00"
        assert isinstance(message.metadata.edited_at, datetime)
        assert message.metadata.edited_at.isoformat() == "2024-01-15T11:00:00+00:00"

    def test_handles_none_edited_at(self):
        data = {
            "_type": "chat:Message",
            "id": "msg-1",
            "thread_id": "slack:C123:1234.5678",
            "text": "Test",
            "formatted": {"type": "root", "children": []},
            "raw": {},
            "author": {
                "user_id": "U123",
                "user_name": "testuser",
                "full_name": "Test User",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {
                "date_sent": "2024-01-15T10:30:00+00:00",
                "edited": False,
            },
            "attachments": [],
        }
        message = Message.from_json(data)
        assert message.metadata.edited_at is None

    def test_restores_attachments(self):
        data = {
            "id": "msg-1",
            "thread_id": "t1",
            "text": "Test",
            "formatted": {"type": "root", "children": []},
            "raw": {},
            "author": {
                "user_id": "U1",
                "user_name": "u",
                "full_name": "U",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {"date_sent": "2024-01-15T10:30:00+00:00", "edited": False},
            "attachments": [
                {
                    "type": "file",
                    "url": "https://example.com/file.pdf",
                    "name": "file.pdf",
                    "mime_type": "application/pdf",
                    "size": 2048,
                },
            ],
        }
        message = Message.from_json(data)

        assert len(message.attachments) == 1
        assert message.attachments[0].type == "file"
        assert message.attachments[0].url == "https://example.com/file.pdf"
        assert message.attachments[0].name == "file.pdf"
        assert message.attachments[0].mime_type == "application/pdf"
        assert message.attachments[0].size == 2048

    def test_restores_links(self):
        data = {
            "id": "msg-1",
            "thread_id": "t1",
            "text": "Links test",
            "formatted": {"type": "root", "children": []},
            "raw": {},
            "author": {
                "user_id": "U1",
                "user_name": "u",
                "full_name": "U",
                "is_bot": False,
                "is_me": False,
            },
            "metadata": {"date_sent": "2024-01-15T10:30:00+00:00", "edited": False},
            "attachments": [],
            "links": [
                {"url": "https://example.com", "title": "Example"},
                {"url": "https://vercel.com", "site_name": "Vercel"},
            ],
        }
        message = Message.from_json(data)

        assert message.links is not None
        assert len(message.links) == 2
        assert message.links[0].url == "https://example.com"
        assert message.links[0].title == "Example"
        assert message.links[1].url == "https://vercel.com"
        assert message.links[1].site_name == "Vercel"
        # fetch_message is not preserved across serialization
        assert message.links[0].fetch_message is None


# ============================================================================
# Round-trip
# ============================================================================


class TestRoundTrip:
    """Tests for to_json/from_json round-trip integrity."""

    def test_basic_round_trip(self):
        original = create_test_message(
            "msg-1",
            "Hello **world**",
            is_mention=True,
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
                edited=True,
                edited_at=datetime(2024, 1, 15, 11, 0, 0, tzinfo=timezone.utc),
            ),
            attachments=[
                Attachment(
                    type="file",
                    url="https://example.com/file.pdf",
                    name="file.pdf",
                ),
            ],
        )

        data = original.to_json()
        restored = Message.from_json(data)

        assert restored.id == original.id
        assert restored.text == original.text
        assert restored.is_mention == original.is_mention
        assert restored.metadata.date_sent == original.metadata.date_sent
        assert restored.metadata.edited_at == original.metadata.edited_at
        assert len(restored.attachments) == 1
        assert restored.attachments[0].type == "file"
        assert restored.attachments[0].url == "https://example.com/file.pdf"
        assert restored.attachments[0].name == "file.pdf"

    def test_round_trip_links(self):
        original = create_test_message(
            "msg-1",
            "Links test",
            links=[
                LinkPreview(url="https://example.com", title="Example"),
                LinkPreview(url="https://vercel.com", site_name="Vercel"),
            ],
        )

        data = original.to_json()
        restored = Message.from_json(data)

        assert restored.links is not None
        assert len(restored.links) == 2
        assert restored.links[0].url == "https://example.com"
        assert restored.links[0].title == "Example"
        assert restored.links[1].url == "https://vercel.com"
        assert restored.links[1].site_name == "Vercel"
        assert restored.links[0].fetch_message is None

    def test_round_trip_through_json_string(self):
        """Ensure the data survives JSON.stringify/parse equivalent."""
        original = create_test_message("msg-1", "Serializable test")
        data = original.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        restored = Message.from_json(parsed)

        assert restored.id == original.id
        assert restored.text == original.text
        assert isinstance(restored.metadata.date_sent, datetime)

    def test_round_trip_preserves_author(self):
        original = create_test_message(
            "msg-1",
            "Test",
            author=Author(
                user_id="U999",
                user_name="custom_user",
                full_name="Custom User",
                is_bot=True,
                is_me=True,
            ),
        )
        data = original.to_json()
        restored = Message.from_json(data)

        assert restored.author.user_id == "U999"
        assert restored.author.user_name == "custom_user"
        assert restored.author.full_name == "Custom User"
        assert restored.author.is_bot is True
        assert restored.author.is_me is True

    def test_round_trip_with_raw_data(self):
        original = create_test_message("msg-1", "Test", raw={"team_id": "T123", "nested": {"key": "value"}})
        data = original.to_json()
        restored = Message.from_json(data)

        assert restored.raw == {"team_id": "T123", "nested": {"key": "value"}}


# ============================================================================
# ThreadImpl serialization (complements test_thread.py)
# ============================================================================


class TestThreadSerialization:
    """Thread-level serialization tests co-located with Message serialization."""

    def test_thread_type_tag(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                is_dm=False,
            )
        )
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["id"] == "slack:C123:1234.5678"
        assert data["channel_id"] == "C123"
        assert data["is_dm"] is False
        assert data["adapter_name"] == "slack"

    def test_thread_round_trip(self, mock_adapter, mock_state):
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                is_dm=True,
                channel_visibility="external",
            )
        )
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.id == original.id
        assert restored.channel_id == original.channel_id
        assert restored.is_dm == original.is_dm
        assert restored.channel_visibility == "external"
        assert restored.adapter.name == original.adapter.name

    def test_thread_json_serializable(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="teams:channel123:thread456",
                adapter=create_mock_adapter("teams"),
                state_adapter=mock_state,
            )
        )
        data = thread.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        assert parsed == data
