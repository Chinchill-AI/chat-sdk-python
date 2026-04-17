"""Tests for Message.to_json/from_json round-trip serialization.

Covers: type tags, date handling, author fields, attachments (without
non-serializable fields), is_mention, links, and round-trip integrity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from chat_sdk import reviver
from chat_sdk.channel import ChannelImpl
from chat_sdk.chat import Chat
from chat_sdk.testing import (
    create_mock_adapter,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig, clear_chat_singleton
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

    def test_should_serialize_message_with_correct_type_tag(self):
        message = create_test_message("msg-1", "Hello world")
        data = message.to_json()

        assert data["_type"] == "chat:Message"
        assert data["id"] == "msg-1"
        assert data["text"] == "Hello world"

    def test_should_convert_date_to_iso_string(self):
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

        assert data["metadata"]["dateSent"] == "2024-01-15T10:30:00+00:00"
        assert data["metadata"]["editedAt"] == "2024-01-15T11:00:00+00:00"

    def test_should_handle_undefined_editedat(self):
        message = create_test_message(
            "msg-1",
            "Test",
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
                edited=False,
            ),
        )
        data = message.to_json()
        # editedAt is omitted from the dict when None (not set to null)
        assert "editedAt" not in data["metadata"]

    def test_should_serialize_author_correctly(self):
        message = create_test_message("msg-1", "Test")
        data = message.to_json()

        assert data["author"] == {
            "userId": "U123",
            "userName": "testuser",
            "fullName": "Test User",
            "isBot": False,
            "isMe": False,
        }

    def test_should_serialize_attachments_without_datafetchdata(self):
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
        assert att["mimeType"] == "image/png"
        assert att["size"] == 1024
        assert att["width"] == 800
        assert att["height"] == 600
        # data and fetch_data should NOT be present
        assert "data" not in att or att.get("data") is None  # None is ok since it's not callable
        assert "fetch_data" not in att

    def test_should_serialize_ismention_flag(self):
        message = create_test_message("msg-1", "Test", is_mention=True)
        data = message.to_json()
        assert data["isMention"] is True

    def test_should_serialize_links_without_fetchmessage(self):
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
            "siteName": "Vercel",
        }
        # fetch_message should NOT be in serialized output
        assert "fetch_message" not in data["links"][0]

    def test_should_omit_links_when_empty(self):
        message = create_test_message("msg-1", "No links", links=[])
        data = message.to_json()
        # links key should not be present when links is empty list
        assert "links" not in data

    def test_should_produce_jsonserializable_output(self):
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

    def test_should_restore_message_from_json(self):
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

    def test_should_convert_iso_strings_back_to_date_objects(self):
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

    def test_fromjson_should_handle_undefined_editedat(self):
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

    def test_should_roundtrip_and_restore_links_correctly(self):
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

    def test_should_roundtrip_correctly(self):
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

    def test_should_roundtrip_links_correctly(self):
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

    def test_should_roundtrip_correctly_complete(self):
        """Ensure the data survives JSON.stringify/parse equivalent."""
        original = create_test_message("msg-1", "Serializable test")
        data = original.to_json()
        stringified = json.dumps(data)
        parsed = json.loads(stringified)
        restored = Message.from_json(parsed)

        assert restored.id == original.id
        assert restored.text == original.text
        assert isinstance(restored.metadata.date_sent, datetime)

    def test_should_roundtrip_message_correctly_preserving_author(self):
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

    def test_should_serialize_and_roundtrip_with_raw_data_via_workflowserialize(self):
        original = create_test_message("msg-1", "Test", raw={"team_id": "T123", "nested": {"key": "value"}})
        data = original.to_json()
        restored = Message.from_json(data)

        assert restored.raw == {"team_id": "T123", "nested": {"key": "value"}}


# ============================================================================
# ThreadImpl serialization (complements test_thread.py)
# ============================================================================


class TestThreadSerialization:
    """Thread-level serialization tests co-located with Message serialization."""

    def test_should_serialize_thread_with_correct_type_tag(self, mock_adapter, mock_state):
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
        assert data["channelId"] == "C123"
        assert data["isDM"] is False
        assert data["adapterName"] == "slack"

    def test_should_roundtrip_thread_correctly_preserving_fields(self, mock_adapter, mock_state):
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

    def test_thread_should_produce_jsonserializable_output(self, mock_adapter, mock_state):
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


# ============================================================================
# ThreadImpl.toJSON() - additional tests
# ============================================================================


class TestThreadToJsonFaithful:
    """Additional ThreadImpl.toJSON() tests from TS."""

    def test_should_serialize_dm_thread_correctly(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:DU123:",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="DU123",
                is_dm=True,
            )
        )
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["isDM"] is True

    def test_should_serialize_external_channel_thread_correctly(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                channel_visibility="external",
            )
        )
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["channelVisibility"] == "external"

    def test_should_serialize_private_channel_thread_correctly(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                channel_visibility="private",
            )
        )
        data = thread.to_json()

        assert data["_type"] == "chat:Thread"
        assert data["channelVisibility"] == "private"

    def test_should_serialize_workspace_channel_thread_correctly(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                channel_visibility="workspace",
            )
        )
        data = thread.to_json()

        assert data["channelVisibility"] == "workspace"


# ============================================================================
# ThreadImpl.fromJSON()
# ============================================================================


class TestThreadFromJsonFaithful:
    """Tests for ThreadImpl.fromJSON()."""

    def test_should_reconstruct_thread_from_json(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert thread.id == "slack:C123:1234.5678"
        assert thread.channel_id == "C123"
        assert thread.is_dm is False
        assert thread.adapter.name == "slack"

    def test_should_rebind_adapter_when_data_is_already_a_threadimpl(self, mock_state):
        """Idempotent path: when ``data`` is already a ThreadImpl (e.g. revived
        via ``object_hook``), passing an explicit ``adapter=`` must still rebind
        it — an early-return shortcut would leave ``_adapter`` stale. Regression
        for a CodeRabbit finding on commit 8dd34d1."""
        from chat_sdk.testing import create_mock_adapter

        first = create_mock_adapter("slack")
        second = create_mock_adapter("teams")
        original = ThreadImpl.from_json(
            {
                "_type": "chat:Thread",
                "id": "slack:C123:1234.5678",
                "channel_id": "C123",
                "is_dm": False,
                "adapter_name": "slack",
            },
            adapter=first,
        )
        rebound = ThreadImpl.from_json(original, adapter=second)

        # Rebind applied even though data was already a ThreadImpl:
        assert rebound.adapter.name == "teams"
        assert rebound.to_json()["adapterName"] == "teams"

    def test_should_sync_adapter_name_when_explicit_adapter_is_bound(self, mock_state):
        """from_json(data, adapter=X) must update _adapter_name to X.name so
        to_json() doesn't serialize a stale name that refers to a different
        adapter than what's actually bound. Regression for a P2 raised in
        review."""
        from chat_sdk.testing import create_mock_adapter

        renamed_adapter = create_mock_adapter("teams")
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",  # different from the bound adapter
        }
        thread = ThreadImpl.from_json(data, adapter=renamed_adapter)

        # Runtime uses the bound adapter...
        assert thread.adapter.name == "teams"
        # ...and re-serialization reflects that, not the stale "slack" name.
        assert thread.to_json()["adapterName"] == "teams"

    def test_should_reconstruct_dm_thread(self, mock_adapter, mock_state):
        data = {
            "_type": "chat:Thread",
            "id": "slack:DU456:",
            "channel_id": "DU456",
            "is_dm": True,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert thread.is_dm is True

    def test_should_throw_error_for_unknown_adapter_on_access(self):
        data = {
            "_type": "chat:Thread",
            "id": "discord:channel:thread",
            "channel_id": "channel",
            "is_dm": False,
            "adapter_name": "discord",
        }
        thread = ThreadImpl.from_json(data)
        # Error is thrown on adapter access, not during from_json
        with pytest.raises(RuntimeError):
            _ = thread.adapter

    def test_should_roundtrip_channelvisibility_correctly(self, mock_adapter, mock_state):
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                channel_visibility="external",
            )
        )
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert restored.channel_visibility == "external"

    def test_should_default_channelvisibility_to_unknown_when_missing_from_json(self, mock_adapter):
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert thread.channel_visibility == "unknown"

    def test_should_serialize_currentmessage(self, mock_adapter, mock_state):
        current_message = create_test_message(
            "msg-1",
            "Hello",
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
        )
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                current_message=current_message,
            )
        )
        data = original.to_json()

        assert data["currentMessage"] is not None
        assert data["currentMessage"]["_type"] == "chat:Message"
        assert data["currentMessage"]["author"]["userId"] == "U456"
        assert data["currentMessage"]["raw"] == {"team_id": "T123"}

    def test_should_roundtrip_with_currentmessage_for_streaming(self, mock_adapter, mock_state):
        current_message = create_test_message(
            "msg-1",
            "Hello",
            raw={"team_id": "T123"},
            author=Author(
                user_id="U456",
                user_name="user",
                full_name="Test User",
                is_bot=False,
                is_me=False,
            ),
        )
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                current_message=current_message,
            )
        )
        data = original.to_json()
        restored = ThreadImpl.from_json(data, adapter=mock_adapter)

        assert data["currentMessage"]["author"]["userId"] == "U456"
        assert data["currentMessage"]["raw"] == {"team_id": "T123"}
        assert restored.id == original.id
        assert restored.channel_id == original.channel_id


# ============================================================================
# chat.reviver()
# ============================================================================


class TestChatReviver:
    """Tests for chat.reviver() JSON deserialization."""

    def test_should_revive_chatthread_objects(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        try:
            reviver = chat.reviver()
            thread_data = {
                "_type": "chat:Thread",
                "id": "slack:C123:1234.5678",
                "channel_id": "C123",
                "is_dm": False,
                "adapter_name": "slack",
            }
            result = reviver("thread", thread_data)
            assert isinstance(result, ThreadImpl)
            assert result.id == "slack:C123:1234.5678"
        finally:
            clear_chat_singleton()

    def test_should_revive_chatmessage_objects(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        try:
            reviver = chat.reviver()
            message_data = {
                "_type": "chat:Message",
                "id": "msg-1",
                "thread_id": "slack:C123:1234.5678",
                "text": "Hello",
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
            result = reviver("message", message_data)
            assert result.id == "msg-1"
            assert isinstance(result.metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()

    def test_should_revive_both_thread_and_message_in_same_payload(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        try:
            reviver = chat.reviver()
            thread_data = {
                "_type": "chat:Thread",
                "id": "slack:C123:1234.5678",
                "channel_id": "C123",
                "is_dm": False,
                "adapter_name": "slack",
            }
            message_data = {
                "_type": "chat:Message",
                "id": "msg-1",
                "thread_id": "slack:C123:1234.5678",
                "text": "Hello",
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
            thread = reviver("thread", thread_data)
            message = reviver("message", message_data)
            assert isinstance(thread, ThreadImpl)
            assert isinstance(message.metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()

    def test_should_leave_nonchat_objects_unchanged(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        try:
            reviver = chat.reviver()
            data = {"_type": "other:Type", "value": "unchanged"}
            result = reviver("nested", data)
            assert result["_type"] == "other:Type"
            assert result["value"] == "unchanged"
        finally:
            clear_chat_singleton()

    def test_should_work_with_nested_structures(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        try:
            reviver = chat.reviver()
            message_data = {
                "_type": "chat:Message",
                "id": "msg-1",
                "thread_id": "slack:C123:1234.5678",
                "text": "Hello",
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
            result = reviver("message", message_data)
            assert isinstance(result.metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()


# ============================================================================
# Standalone reviver (no Chat instance required at import time)
# ============================================================================


class TestStandaloneReviver:
    """Tests for the module-level :func:`chat_sdk.reviver` function.

    Mirrors the TS ``standalone reviver()`` describe block. Python's
    ``json.loads`` uses ``object_hook`` rather than a key/value reviver, so
    usage differs slightly: the function is passed as ``object_hook`` and
    receives each decoded dict.
    """

    def test_should_revive_chatthread_objects(self, mock_adapter, mock_state):
        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            payload = json.dumps(
                {
                    "thread": {
                        "_type": "chat:Thread",
                        "id": "slack:C123:1234.5678",
                        "channelId": "C123",
                        "isDM": False,
                        "adapterName": "slack",
                    }
                }
            )
            parsed = json.loads(payload, object_hook=reviver)
            assert isinstance(parsed["thread"], ThreadImpl)
            assert parsed["thread"].id == "slack:C123:1234.5678"
        finally:
            clear_chat_singleton()

    def test_should_revive_chatmessage_objects(self, mock_adapter, mock_state):
        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            payload = json.dumps(
                {
                    "message": {
                        "_type": "chat:Message",
                        "id": "msg-1",
                        "threadId": "slack:C123:1234.5678",
                        "text": "Hello",
                        "formatted": {"type": "root", "children": []},
                        "raw": {},
                        "author": {
                            "userId": "U123",
                            "userName": "testuser",
                            "fullName": "Test User",
                            "isBot": False,
                            "isMe": False,
                        },
                        "metadata": {
                            "dateSent": "2024-01-15T10:30:00.000Z",
                            "edited": False,
                        },
                        "attachments": [],
                    }
                }
            )
            parsed = json.loads(payload, object_hook=reviver)
            assert parsed["message"].id == "msg-1"
            assert isinstance(parsed["message"].metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()

    def test_should_revive_both_thread_and_message_in_same_payload(self, mock_adapter, mock_state):
        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            payload = json.dumps(
                {
                    "thread": {
                        "_type": "chat:Thread",
                        "id": "slack:C123:1234.5678",
                        "channelId": "C123",
                        "isDM": False,
                        "adapterName": "slack",
                    },
                    "message": {
                        "_type": "chat:Message",
                        "id": "msg-1",
                        "threadId": "slack:C123:1234.5678",
                        "text": "Hello",
                        "formatted": {"type": "root", "children": []},
                        "raw": {},
                        "author": {
                            "userId": "U123",
                            "userName": "testuser",
                            "fullName": "Test User",
                            "isBot": False,
                            "isMe": False,
                        },
                        "metadata": {
                            "dateSent": "2024-01-15T10:30:00.000Z",
                            "edited": False,
                        },
                        "attachments": [],
                    },
                }
            )
            parsed = json.loads(payload, object_hook=reviver)
            assert isinstance(parsed["thread"], ThreadImpl)
            assert isinstance(parsed["message"].metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()

    def test_should_leave_nonchat_objects_unchanged(self, mock_adapter, mock_state):
        payload = json.dumps(
            {
                "name": "test",
                "count": 42,
                "nested": {"_type": "other:Type", "value": "unchanged"},
            }
        )
        parsed = json.loads(payload, object_hook=reviver)
        assert parsed["name"] == "test"
        assert parsed["count"] == 42
        assert parsed["nested"]["_type"] == "other:Type"

    def test_should_be_usable_directly_as_json_parse_second_argument(self, mock_adapter, mock_state):
        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            message_json = {
                "_type": "chat:Message",
                "id": "msg-direct",
                "threadId": "slack:C123:1234.5678",
                "text": "Direct usage",
                "formatted": {"type": "root", "children": []},
                "raw": {},
                "author": {
                    "userId": "U123",
                    "userName": "testuser",
                    "fullName": "Test User",
                    "isBot": False,
                    "isMe": False,
                },
                "metadata": {
                    "dateSent": "2024-01-15T10:30:00.000Z",
                    "edited": False,
                },
                "attachments": [],
            }
            parsed = json.loads(json.dumps(message_json), object_hook=reviver)
            assert parsed.id == "msg-direct"
            assert parsed.text == "Direct usage"
            assert isinstance(parsed.metadata.date_sent, datetime)
        finally:
            clear_chat_singleton()

    def test_should_allow_reserialization_of_a_revived_thread_without_singleton(self):
        clear_chat_singleton()
        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channelId": "C123",
            "isDM": False,
            "adapterName": "slack",
        }
        thread = ThreadImpl.from_json(data)
        reserialized = thread.to_json()
        assert reserialized["_type"] == "chat:Thread"
        assert reserialized["adapterName"] == "slack"
        assert reserialized["id"] == "slack:C123:1234.5678"

    def test_should_allow_reserialization_of_a_revived_channel_without_singleton(self):
        clear_chat_singleton()
        data = {
            "_type": "chat:Channel",
            "id": "C123",
            "isDM": False,
            "adapterName": "slack",
        }
        # Route through the public `chat_sdk.reviver` entry point rather than
        # `ChannelImpl.from_json` directly so a regression in the reviver's
        # "chat:Channel" dispatch would fail here too.
        channel = json.loads(json.dumps(data), object_hook=reviver)
        assert isinstance(channel, ChannelImpl)
        reserialized = channel.to_json()
        assert reserialized["_type"] == "chat:Channel"
        assert reserialized["adapterName"] == "slack"
        assert reserialized["id"] == "C123"

    def test_should_revive_thread_with_nested_current_message_via_object_hook(self, mock_adapter, mock_state):
        """``object_hook`` revives children first, so ``currentMessage`` reaches
        ``ThreadImpl.from_json`` as a :class:`Message` instance, not a dict.
        ``from_json`` must accept that without raising ``AttributeError``."""
        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            payload = json.dumps(
                {
                    "_type": "chat:Thread",
                    "id": "slack:C123:1234.5678",
                    "channelId": "C123",
                    "isDM": False,
                    "adapterName": "slack",
                    "currentMessage": {
                        "_type": "chat:Message",
                        "id": "msg-current",
                        "threadId": "slack:C123:1234.5678",
                        "text": "hi",
                        "formatted": {"type": "root", "children": []},
                        "raw": {},
                        "author": {
                            "userId": "U123",
                            "userName": "testuser",
                            "fullName": "Test User",
                            "isBot": False,
                            "isMe": False,
                        },
                        "metadata": {
                            "dateSent": "2024-01-15T10:30:00.000Z",
                            "edited": False,
                        },
                        "attachments": [],
                    },
                }
            )
            thread = json.loads(payload, object_hook=reviver)
            assert isinstance(thread, ThreadImpl)
            assert thread._current_message is not None
            assert isinstance(thread._current_message, Message)
            assert thread._current_message.id == "msg-current"
        finally:
            clear_chat_singleton()


# ============================================================================
# @workflow/serde integration — ThreadImpl
# ============================================================================


class TestThreadWorkflowSerde:
    """Tests for ThreadImpl WORKFLOW_SERIALIZE/DESERIALIZE (to_json/from_json)."""

    def test_should_have_workflowserialize_static_method(self):
        # Python equivalent: to_json is an instance method
        assert hasattr(ThreadImpl, "to_json")
        assert callable(ThreadImpl.to_json)

    def test_should_have_workflowdeserialize_static_method(self):
        # Python equivalent: from_json is a class method
        assert hasattr(ThreadImpl, "from_json")
        assert callable(ThreadImpl.from_json)

    def test_should_serialize_via_workflowserialize(self, mock_adapter, mock_state):
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=mock_adapter,
                state_adapter=mock_state,
                channel_id="C123",
                is_dm=False,
            )
        )
        serialized = thread.to_json()

        assert serialized["_type"] == "chat:Thread"
        assert serialized["id"] == "slack:C123:1234.5678"
        assert serialized["channelId"] == "C123"
        assert serialized["channelVisibility"] == "unknown"
        assert serialized["isDM"] is False
        assert serialized["adapterName"] == "slack"

    def test_should_deserialize_via_workflowdeserialize_with_lazy_resolution(self, mock_adapter, mock_state):
        from chat_sdk.chat import Chat
        from chat_sdk.thread import clear_chat_singleton

        chat = Chat(
            user_name="test-bot",
            adapters={"slack": mock_adapter},
            state=mock_state,
            logger="silent",
        )
        chat.register_singleton()
        try:
            data = {
                "_type": "chat:Thread",
                "id": "slack:C123:1234.5678",
                "channel_id": "C123",
                "is_dm": False,
                "adapter_name": "slack",
            }
            result = ThreadImpl.from_json(data)

            assert isinstance(result, ThreadImpl)
            assert result.id == "slack:C123:1234.5678"
            assert result.channel_id == "C123"
            assert result.is_dm is False
            assert result.adapter.name == "slack"
        finally:
            clear_chat_singleton()


# ============================================================================
# @workflow/serde integration — Message
# ============================================================================


class TestMessageWorkflowSerde:
    """Tests for Message WORKFLOW_SERIALIZE/DESERIALIZE (to_json/from_json)."""

    def test_message_should_have_workflowserialize_static_method(self):
        # Python equivalent: to_json is an instance method
        assert hasattr(Message, "to_json")
        assert callable(Message.to_json)

    def test_message_should_have_workflowdeserialize_static_method(self):
        # Python equivalent: from_json is a class method
        assert hasattr(Message, "from_json")
        assert callable(Message.from_json)

    def test_message_should_serialize_via_workflowserialize(self):
        message = create_test_message("msg-1", "Hello world")
        serialized = message.to_json()

        assert serialized["_type"] == "chat:Message"
        assert serialized["id"] == "msg-1"
        assert serialized["text"] == "Hello world"
        assert isinstance(serialized["metadata"]["dateSent"], str)

    def test_should_deserialize_via_workflowdeserialize(self):
        data = {
            "_type": "chat:Message",
            "id": "msg-1",
            "thread_id": "slack:C123:1234.5678",
            "text": "Hello",
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

        assert message.id == "msg-1"
        assert message.text == "Hello"
        assert isinstance(message.metadata.date_sent, datetime)

    def test_should_roundtrip_via_workflowserialize_and_workflowdeserialize(self):
        original = create_test_message("msg-1", "Test message", is_mention=True)

        serialized = original.to_json()
        restored = Message.from_json(serialized)

        assert restored.id == original.id
        assert restored.text == original.text
        assert restored.is_mention == original.is_mention
        assert restored.metadata.date_sent == original.metadata.date_sent
