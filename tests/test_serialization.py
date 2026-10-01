"""Tests for Message.to_json/from_json round-trip serialization.

Covers: type tags, date handling, author fields, attachments (without
non-serializable fields), is_mention, links, and round-trip integrity.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk import reviver
from chat_sdk.channel import ChannelImpl
from chat_sdk.chat import Chat
from chat_sdk.plan import StreamingPlan, StreamingPlanOptions
from chat_sdk.testing import (
    create_mock_adapter,
    create_mock_state,
    create_test_message,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig, clear_chat_singleton
from chat_sdk.types import (
    UNSET,
    Attachment,
    Author,
    ChatConfig,
    LinkPreview,
    Message,
    MessageMetadata,
    MessageSubject,
    ModalCloseEvent,
    ModalSubmitEvent,
    PostableMarkdown,
    RawMessage,
    WebhookOptions,
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

    # TS: "should round-trip replied-to message context"
    def test_should_roundtrip_repliedto_message_context(self):
        reply_to = create_test_message(
            "msg-original",
            "Original message",
            raw={"platformId": "original-1"},
            author=Author(
                user_id="U456",
                user_name="original-author",
                full_name="Original Author",
                is_bot=False,
                is_me=False,
            ),
            metadata=MessageMetadata(
                date_sent=datetime(2024, 1, 14, 10, 30, 0, tzinfo=timezone.utc),
                edited=False,
            ),
            attachments=[
                Attachment(type="file", name="original.pdf", fetch_metadata={"fileId": "file-1"}),
            ],
        )
        original = create_test_message("msg-reply", "Reply", reply_to=reply_to)

        restored = Message.from_json(original.to_json())

        assert isinstance(restored.reply_to, Message)
        assert restored.reply_to.id == "msg-original"
        assert restored.reply_to.text == "Original message"
        assert restored.reply_to.author.user_name == "original-author"
        assert restored.reply_to.raw == {"platformId": "original-1"}
        assert restored.reply_to.metadata.date_sent == datetime(2024, 1, 14, 10, 30, 0, tzinfo=timezone.utc)
        assert restored.reply_to.attachments == [
            Attachment(type="file", name="original.pdf", fetch_metadata={"fileId": "file-1"}),
        ]

    # Python-specific: ``to_json`` emits ``replyTo`` only when set, and
    # ``json.loads(object_hook=...)`` revives bottom-up, so the outer dict
    # reaches ``Message.from_json`` / the Chat reviver with ``replyTo``
    # already a ``Message`` -- both must pass it through.
    # The authors also check that both revivers read ``email`` / ``isSystem``,
    # with a ``False`` ``isSystem`` kept as ``False`` (not ``None``).
    def test_reply_to_survives_bottom_up_object_hook_revival(self, mock_adapter, mock_state):
        reply_author = Author(
            user_id="USLACK", user_name="Slack", full_name="Slack", is_bot=False, is_me=False, is_system=True
        )
        reply_to = create_test_message("msg-original", "Original", raw={"r": 1}, author=reply_author)
        outer_author = Author(
            user_id="U1",
            user_name="a",
            full_name="A",
            is_bot=False,
            is_me=False,
            email="a@b.c",
            is_system=False,
        )
        original = create_test_message("msg-reply", "Reply", reply_to=reply_to, author=outer_author)
        assert "replyTo" not in reply_to.to_json()
        payload = json.dumps({"message": original.to_json()})

        standalone = json.loads(payload, object_hook=reviver)["message"]

        chat = Chat(user_name="test-bot", adapters={"slack": mock_adapter}, state=mock_state, logger="silent")
        chat_reviver = chat.reviver()
        bound = json.loads(payload, object_hook=lambda d: chat_reviver("", d))["message"]

        for revived in (standalone, bound):
            assert isinstance(revived, Message)
            assert isinstance(revived.reply_to, Message)
            assert revived.reply_to.id == "msg-original"
            assert revived.reply_to.raw == {"r": 1}
            assert revived.reply_to.reply_to is None
            assert revived.author.email == "a@b.c"
            assert revived.author.is_system is False
            assert revived.reply_to.author.is_system is True
            assert revived.reply_to.author.email is None

    # Python-specific: ``from_json_compat`` prefers snake_case keys and
    # recurses into ``reply_to`` (also accepting an already-revived Message).
    def test_from_json_compat_reads_snake_case_reply_to_and_author_fields(self):
        already_revived = create_test_message("msg-0", "Revived")
        data = {
            "id": "msg-1",
            "thread_id": "slack:C1:1.1",
            "text": "Reply",
            "author": {
                "user_id": "USLACK",
                "user_name": "Slack",
                "full_name": "Slack",
                "is_bot": False,
                "is_me": False,
                "is_system": True,
                "email": "slack@example.com",
            },
            "metadata": {"date_sent": "2024-01-15T10:30:00+00:00", "edited": False},
            "reply_to": {
                "id": "msg-orig",
                "thread_id": "slack:C1:1.1",
                "text": "Original",
                "author": {"user_id": "U1", "user_name": "u", "full_name": "U", "is_bot": False, "is_me": False},
                "metadata": {"date_sent": "2024-01-14T10:30:00+00:00", "edited": False},
                "reply_to": already_revived,
            },
        }

        restored = Message.from_json_compat(data)

        assert restored.author.is_system is True
        assert restored.author.email == "slack@example.com"
        assert restored.reply_to is not None
        assert restored.reply_to.id == "msg-orig"
        assert restored.reply_to.author.is_system is None
        assert restored.reply_to.reply_to is already_revived
        assert Message.from_json_compat(already_revived) is already_revived

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

    def test_should_invalidate_state_and_channel_caches_on_idempotent_rebind(self, mock_state):
        """When `from_json(existing_instance, adapter=X)` rebinds an already-
        revived ThreadImpl, caches derived from the previous binding must be
        invalidated — otherwise `_state_adapter_instance` would continue
        routing to the OLD chat's state backend and `_channel_cache` would
        still reference the OLD adapter. Regression for a Codex P1."""
        from chat_sdk.testing import create_mock_adapter, create_mock_state

        first_adapter = create_mock_adapter("slack")
        second_adapter = create_mock_adapter("teams")
        first_state = create_mock_state()

        # Prime the thread with the first adapter + state, and force both
        # caches (state and channel) to populate.
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=first_adapter,
                state_adapter=first_state,
                channel_id="C123",
            )
        )
        _ = original.channel  # populate _channel_cache
        assert original._channel_cache is not None
        assert original._state_adapter_instance is first_state

        # Rebind to a different adapter. Both caches must drop so the next
        # access resolves against the new binding.
        rebound = ThreadImpl.from_json(original, adapter=second_adapter)
        assert rebound._channel_cache is None
        assert rebound._state_adapter_instance is None
        # And the adapter is actually rebound.
        assert rebound.adapter.name == "teams"

    def test_should_reset_is_subscribed_context_on_rebind(self, mock_state):
        """A thread constructed inside a subscribed-context handler carries
        `_is_subscribed_context=True`. If that thread is rebound to a new
        adapter/chat, the new context has no active subscription — the flag
        should clear so `is_subscribed()` doesn't short-circuit to True
        against the new state backend. Regression for a self-review-
        subagent finding."""
        from chat_sdk.testing import create_mock_adapter, create_mock_state

        first_adapter = create_mock_adapter("slack")
        second_adapter = create_mock_adapter("teams")
        first_state = create_mock_state()

        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=first_adapter,
                state_adapter=first_state,
                channel_id="C123",
                is_subscribed_context=True,
            )
        )
        assert original._is_subscribed_context is True

        rebound = ThreadImpl.from_json(original, adapter=second_adapter)
        assert rebound._is_subscribed_context is False

    def test_should_leave_thread_unchanged_when_chat_rebind_lookup_fails(self, mock_state):
        """Transactional rebind: `from_json(existing_thread, chat=Y)` must
        either fully apply the rebind or leave the thread untouched. If
        `chat.get_adapter(name)` returns None, the RuntimeError must fire
        BEFORE any cache invalidation — callers that catch the exception
        should be able to keep using the thread with its original
        bindings intact. Regression for a Codex P2."""
        from chat_sdk.chat import Chat, ChatConfig
        from chat_sdk.testing import create_mock_adapter, create_mock_state, create_test_message

        slack_a = create_mock_adapter("slack")
        state_a = create_mock_state()
        # Chat B has a different adapter name, so looking up "slack" will fail.
        chat_b = Chat(
            ChatConfig(
                user_name="bot-b",
                adapters={"teams": create_mock_adapter("teams")},
                state=create_mock_state(),
            )
        )

        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=slack_a,
                state_adapter=state_a,
                channel_id="C123",
                initial_message=create_test_message("m1", "hello"),
                is_subscribed_context=True,
            )
        )
        # Capture every state-bearing attribute before the rebind.
        snapshot = {
            "_adapter": original._adapter,
            "_adapter_name": original._adapter_name,
            "_state_adapter_instance": original._state_adapter_instance,
            "_channel_cache": original._channel_cache,
            "_thread_history": original._thread_history,
            "_recent_messages": list(original._recent_messages),
            "_is_subscribed_context": original._is_subscribed_context,
        }

        with pytest.raises(RuntimeError, match='Adapter "slack" not found'):
            ThreadImpl.from_json(original, chat=chat_b)

        # Every cached attribute must be exactly as it was before. A
        # non-transactional implementation would have nulled some of these
        # before the raise, making recovery unsafe.
        for attr, before in snapshot.items():
            after = getattr(original, attr)
            assert after == before, f"{attr}: expected {before!r}, got {after!r}"
        # Extra identity check for objects where equality might be loose.
        assert original._adapter is snapshot["_adapter"]
        assert original._state_adapter_instance is snapshot["_state_adapter_instance"]

    def test_should_invalidate_recent_messages_and_thread_history_on_rebind(self, mock_state):
        """Two additional caches that carry previous-binding references must
        also drop on idempotent rebind: `_recent_messages` (populated from
        adapter fetches) and `_thread_history` (tied to chat's cache).
        Regression for a self-review-subagent finding."""
        from chat_sdk.testing import create_mock_adapter, create_mock_state, create_test_message

        first_adapter = create_mock_adapter("slack")
        second_adapter = create_mock_adapter("teams")
        first_state = create_mock_state()

        # Prime caches: initial_message populates _recent_messages, and
        # thread_history is a sentinel cache object.
        sentinel_history = object()
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=first_adapter,
                state_adapter=first_state,
                channel_id="C123",
                initial_message=create_test_message("m1", "hello"),
                thread_history=sentinel_history,
            )
        )
        assert len(original._recent_messages) == 1
        assert original._thread_history is sentinel_history

        rebound = ThreadImpl.from_json(original, adapter=second_adapter)
        assert rebound._recent_messages == [], "recent_messages should drop on rebind"
        assert rebound._thread_history is None, "thread_history should drop on rebind"

    def test_should_set_state_from_chat_when_both_adapter_and_chat_passed(self, mock_state):
        """adapter= and chat= are applied together. Passing both binds the
        explicit adapter AND takes state from the explicit chat. The previous
        `elif chat` branch silently ignored chat when adapter was also
        passed, leading to split routing. The adapter must be one the chat
        registered (vercel/chat#967); here it is registered under a key that
        differs from its name."""
        from chat_sdk.chat import Chat, ChatConfig
        from chat_sdk.testing import create_mock_adapter, create_mock_state

        teams_adapter = create_mock_adapter("teams")
        chat_state = create_mock_state()
        chat_instance = Chat(
            ChatConfig(
                user_name="bot",
                adapters={"slack": create_mock_adapter("slack"), "msteams": teams_adapter},
                state=chat_state,
            )
        )

        data = {
            "_type": "chat:Thread",
            "id": "slack:C123:1234.5678",
            "channel_id": "C123",
            "is_dm": False,
            "adapter_name": "slack",
        }
        thread = ThreadImpl.from_json(data, adapter=teams_adapter, chat=chat_instance)

        # Explicit adapter wins for adapter binding.
        assert thread.adapter is teams_adapter
        # Explicit chat's state is applied (previously the elif branch
        # skipped this, leaving state as None and silently routing to the
        # singleton on next access).
        assert thread._state_adapter_instance is chat_state

    async def test_should_take_streaming_settings_from_the_new_chat_on_idempotent_rebind(self):
        """A Chat-created thread carries that Chat's streaming values; after
        `from_json(thread, chat=other)` the new owner's settings apply."""
        first = Chat(
            ChatConfig(
                user_name="first",
                adapters={"slack": create_mock_adapter("slack")},
                state=create_mock_state(),
                streaming_update_interval_ms=1000,
                fallback_streaming_placeholder_text=None,
            )
        )
        second_adapter = create_mock_adapter("slack")
        stream = AsyncMock(return_value=RawMessage(id="m", thread_id="slack:C123:1234.5678", raw={}))
        second_adapter.stream = stream  # type: ignore[attr-defined]
        second = Chat(
            ChatConfig(
                user_name="second",
                adapters={"slack": second_adapter},
                state=create_mock_state(),
                streaming_update_interval_ms=250,
                fallback_streaming_placeholder_text="Second",
            )
        )

        thread = ThreadImpl.from_json(first.thread("slack:C123:1234.5678"), chat=second)

        async def _chunks():
            yield "Hi"

        await thread.post(_chunks())
        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (250, "Second")

    def test_should_raise_before_rebinding_when_chat_does_not_own_the_adapter(self, mock_state):
        """Python raises in from_json itself (upstream raises on first state
        use), so an existing instance is left untouched."""
        from chat_sdk.chat import Chat, ChatConfig
        from chat_sdk.testing import create_mock_adapter, create_mock_state

        stray = create_mock_adapter("slack")
        chat_instance = Chat(
            ChatConfig(user_name="bot", adapters={"slack": create_mock_adapter("slack")}, state=create_mock_state())
        )
        existing = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678", adapter=stray, state_adapter=mock_state, channel_id="slack:C123"
            )
        )

        with pytest.raises(RuntimeError, match='Adapter "slack" does not belong to this Chat instance'):
            ThreadImpl.from_json(existing, adapter=create_mock_adapter("slack"), chat=chat_instance)
        assert existing.adapter is stray
        assert existing._state_adapter_instance is mock_state

    def test_should_rebind_adapter_on_idempotent_chat_rebind_for_direct_constructed_thread(self, mock_state):
        """When `from_json(existing_instance, chat=Y)` rebinds a thread that
        was originally constructed directly by a Chat (so `_adapter_name`
        is None), the new chat's matching adapter must replace the old one.
        Otherwise state calls go to chat Y while `post`/`edit` still use
        chat X's adapter — split-routing. Regression for a Codex P1."""
        from chat_sdk.chat import Chat, ChatConfig
        from chat_sdk.testing import create_mock_adapter, create_mock_state

        slack_a = create_mock_adapter("slack")
        slack_b = create_mock_adapter("slack")
        state_a = create_mock_state()
        state_b = create_mock_state()
        chat_b = Chat(ChatConfig(user_name="bot-b", adapters={"slack": slack_b}, state=state_b))

        # Thread constructed via chat_a's path: _adapter is slack_a,
        # _adapter_name is None (not set by direct construction).
        original = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=slack_a,
                state_adapter=state_a,
                channel_id="C123",
            )
        )
        assert original._adapter is slack_a
        assert original._adapter_name is None

        # Rebind to chat_b. Both adapter and state must switch to chat_b's.
        rebound = ThreadImpl.from_json(original, chat=chat_b)
        assert rebound.adapter is slack_b, "adapter not rebound to the new chat"
        assert rebound._state_adapter_instance is state_b, "state not rebound to the new chat"

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


# ============================================================================
# Restored runtime ownership and streaming settings (vercel/chat#967)
# ============================================================================
#
# Python has no ``WORKFLOW_DESERIALIZE``. Upstream's is literally
# ``fromJSON(data)`` (thread.ts / channel.ts at chat@4.41.1), so its
# ``workflow`` restore method would run exactly the ``json`` case here and
# is omitted rather than duplicated (CLAUDE.md principle 3). ``chat.reviver()`` does not register the Chat
# as the singleton in Python, so tests that relied on that side effect
# register it explicitly.

_THREAD_ID = "slack:C123:1234.5678"
_RESTORE_METHODS = ["json", "reviver", "standalone", "adapter"]


@pytest.fixture
def _no_singleton():
    clear_chat_singleton()
    yield
    clear_chat_singleton()


def _bot(adapters: dict[str, Any], state: Any = None, **config: Any) -> Chat:
    return Chat(
        ChatConfig(
            user_name=config.pop("user_name", "bot"),
            adapters=adapters,
            state=state if state is not None else create_mock_state(),
            logger="silent",
            **config,
        )
    )


def _decode(payload: str, chat: Chat) -> Any:
    hook = chat.reviver()
    return json.loads(payload, object_hook=lambda value: hook("", value))


def _dumps(value: Any) -> str:
    return json.dumps(value, default=lambda obj: obj.to_json())


def _restore(chat: Chat, method: str, adapter: Any) -> ThreadImpl:
    data = chat.thread(_THREAD_ID).to_json()
    if method == "json":
        return ThreadImpl.from_json(json.loads(json.dumps(data)))
    if method == "reviver":
        return _decode(json.dumps(data), chat)
    if method == "standalone":
        return json.loads(json.dumps(data), object_hook=reviver)
    return ThreadImpl.from_json(data, adapter)


def _native_stream(adapter: Any, result: Any = "sent") -> AsyncMock:
    stream = AsyncMock(
        return_value=None if result is None else RawMessage(id="msg-1", thread_id=_THREAD_ID, raw={}),
    )
    adapter.stream = stream
    return stream


async def _hello():
    yield "Hello"


async def _reply():
    yield "Reply"


@pytest.mark.usefixtures("_no_singleton")
class TestRevivedStreamingConfiguration:
    """describe("revived streaming configuration")"""

    @pytest.mark.parametrize("placeholder", [None, "Loading...", "", UNSET])
    @pytest.mark.parametrize("method", _RESTORE_METHODS)
    async def test_uses_the_current_chat_placeholder_after_lazy_restoration(self, method, placeholder):
        adapter = create_mock_adapter("slack")
        state = create_mock_state()
        original = _bot(
            {"slack": adapter},
            state,
            fallback_streaming_placeholder_text=placeholder if method == "reviver" else "Old configuration",
        )
        thread = _restore(original, method, adapter)
        clear_chat_singleton()
        assert "fallbackStreamingPlaceholderText" not in thread.to_json()
        _bot({"slack": adapter}, state, fallback_streaming_placeholder_text=placeholder).register_singleton()

        await thread.post(_hello())

        expected = (
            PostableMarkdown(markdown="Hello")
            if placeholder is None
            else ("..." if placeholder is UNSET else placeholder)
        )
        assert adapter._post_calls[0] == (_THREAD_ID, expected)

    @pytest.mark.parametrize("interval", [None, 250])
    @pytest.mark.parametrize("method", _RESTORE_METHODS)
    async def test_restores_the_fallback_edit_interval_with_override(self, method, interval, monkeypatch):
        adapter = create_mock_adapter("slack")
        chat = _bot({"slack": adapter}, streaming_update_interval_ms=1000)
        thread = _restore(chat, method, adapter)
        chat.register_singleton()
        timeouts: list[float] = []

        class _RecordingAsyncio:
            """``chat_sdk.thread``'s view of asyncio, recording edit-loop waits."""

            def __getattr__(self, name: str) -> Any:
                return getattr(asyncio, name)

            @staticmethod
            async def wait_for(awaitable: Any, timeout: float | None = None) -> Any:
                if timeout is not None:
                    timeouts.append(timeout)
                return await asyncio.wait_for(awaitable, timeout=timeout)

        # The fallback edit loop waits ``interval / 1000`` seconds between
        # edits; record that wait instead of advancing a fake clock.
        monkeypatch.setattr("chat_sdk.thread.asyncio", _RecordingAsyncio())
        finish = asyncio.Event()

        async def _delayed():
            yield "Hello"
            await finish.wait()

        posting = asyncio.ensure_future(
            thread.post(StreamingPlan(_delayed(), StreamingPlanOptions(update_interval_ms=interval)))
        )
        try:
            for _ in range(100):
                if timeouts:
                    break
                await asyncio.sleep(0)
            assert adapter._edit_calls == []
        finally:
            finish.set()
            await posting

        assert timeouts
        assert set(timeouts) == {(interval if interval is not None else 1000) / 1000}
        assert adapter._edit_calls[-1] == (_THREAD_ID, "msg-1", PostableMarkdown(markdown="Hello"))

    @pytest.mark.parametrize("interval", [None, 250])
    @pytest.mark.parametrize("method", _RESTORE_METHODS)
    async def test_passes_restored_defaults_and_the_interval_override_to_native_streaming(self, method, interval):
        adapter = create_mock_adapter("slack")
        chat = _bot(
            {"slack": adapter},
            streaming_update_interval_ms=1000,
            fallback_streaming_placeholder_text=None,
        )
        stream = _native_stream(adapter)
        thread = _restore(chat, method, adapter)
        chat.register_singleton()

        await thread.post(
            _hello() if interval is None else StreamingPlan(_hello(), StreamingPlanOptions(update_interval_ms=interval))
        )

        options = stream.await_args.args[2]
        assert options.update_interval_ms == (interval if interval is not None else 1000)
        assert options.fallback_streaming_placeholder_text is None

    @pytest.mark.parametrize("placeholder", [None, "Thread placeholder", ""])
    async def test_preserves_explicit_lazy_thread_overrides_with_placeholder(self, placeholder):
        adapter = create_mock_adapter("slack")
        _bot(
            {"slack": adapter},
            streaming_update_interval_ms=1000,
            fallback_streaming_placeholder_text="Bot placeholder",
        ).register_singleton()
        stream = _native_stream(adapter)
        thread = ThreadImpl(
            _ThreadImplConfig(
                id=_THREAD_ID,
                channel_id="slack:C123",
                adapter_name="slack",
                streaming_update_interval_ms=250,
                fallback_streaming_placeholder_text=placeholder,
            )
        )

        await thread.post(_hello())

        options = stream.await_args.args[2]
        assert options.update_interval_ms == 250
        assert options.fallback_streaming_placeholder_text == placeholder
        assert options.fallback_streaming_placeholder_text is not UNSET

    async def test_does_not_inherit_another_chat_configuration_for_directly_constructed_threads(self):
        adapter = create_mock_adapter("slack")
        _bot(
            {"slack": create_mock_adapter("slack")},
            user_name="other",
            streaming_update_interval_ms=1000,
            fallback_streaming_placeholder_text=None,
        ).register_singleton()
        stream = _native_stream(adapter)
        thread = ThreadImpl(
            _ThreadImplConfig(
                id=_THREAD_ID, channel_id="slack:C123", adapter=adapter, state_adapter=create_mock_state()
            )
        )

        await thread.post(_hello())

        options = stream.await_args.args[2]
        assert options.update_interval_ms == 500
        assert options.fallback_streaming_placeholder_text is UNSET

    async def test_can_stream_with_an_explicit_restored_adapter_without_a_singleton(self):
        adapter = create_mock_adapter("slack")
        thread = ThreadImpl.from_json(
            {
                "_type": "chat:Thread",
                "id": _THREAD_ID,
                "channelId": "slack:C123",
                "adapterName": "slack",
                "isDM": False,
            },
            adapter,
        )

        await thread.post(_hello())

        assert adapter._post_calls[0] == (_THREAD_ID, "...")

    @pytest.mark.parametrize("method", ["submit", "close"])
    async def test_keeps_restored_modal_context_bound_to_its_chat(self, method):
        adapter = create_mock_adapter("slack")
        state = create_mock_state()
        owner = _bot(
            {"slack": adapter},
            state,
            user_name="owner",
            fallback_streaming_placeholder_text=None,
            streaming_update_interval_ms=1200,
        )
        other = _bot(
            {"slack": create_mock_adapter("slack")}, user_name="other", fallback_streaming_placeholder_text="Other"
        )
        await state.set(
            "modal-context:slack:context",
            {"thread": owner.thread(_THREAD_ID).to_json(), "channel": owner.channel("slack:C123").to_json()},
        )
        restored: list[Any] = []

        async def _handler(event: Any) -> None:
            restored.append(event)

        owner.on_modal_submit("modal", _handler)
        owner.on_modal_close("modal", _handler)
        other.register_singleton()
        user = create_test_message("message", "Hello").author
        if method == "submit":
            await owner.process_modal_submit(
                ModalSubmitEvent(adapter=adapter, user=user, view_id="view", callback_id="modal", values={}, raw={}),
                "context",
            )
        else:
            tasks: list[Any] = []
            owner.process_modal_close(
                ModalCloseEvent(adapter=adapter, user=user, view_id="view", callback_id="modal", raw={}),
                "context",
                WebhookOptions(wait_until=tasks.append),
            )
            await asyncio.gather(*tasks)

        assert len(restored) == 1
        thread = restored[0].related_thread
        channel = restored[0].related_channel
        assert thread is not None
        assert channel is not None
        await thread.post(_hello())
        assert adapter._post_calls[0] == (_THREAD_ID, PostableMarkdown(markdown="Hello"))
        await thread.set_state({"owner": "owner"})
        await channel.set_state({"owner": "owner"})
        assert await owner.thread(_THREAD_ID).get_state() == {"owner": "owner"}
        assert await owner.channel("slack:C123").get_state() == {"owner": "owner"}
        assert await other.thread(_THREAD_ID).get_state() is None
        assert await other.channel("slack:C123").get_state() is None
        stream = _native_stream(adapter, None)
        await thread.post(_hello())
        options = stream.await_args.args[2]
        assert options.update_interval_ms == 1200
        assert options.fallback_streaming_placeholder_text is None

    @pytest.mark.parametrize("method", ["submit", "close"])
    async def test_modal_context_for_an_adapter_under_a_custom_key_stays_with_its_chat(self, method):
        # Python-specific: the event's adapter is registered under a key
        # other than `adapter.name`. The restore must still bind to this
        # Chat, never to the active singleton that registered another
        # adapter under the plain name (cross-bot routing, vercel/chat#967).
        adapter = create_mock_adapter("slack")
        state = create_mock_state()
        owner = _bot(
            {"slack-main": adapter},
            state,
            user_name="owner",
            fallback_streaming_placeholder_text=None,
            streaming_update_interval_ms=1200,
        )
        other_adapter = create_mock_adapter("slack")
        other = _bot({"slack": other_adapter}, user_name="other")
        await state.set(
            "modal-context:slack:context",
            {
                "thread": {
                    "_type": "chat:Thread",
                    "id": _THREAD_ID,
                    "channelId": "slack:C123",
                    "adapterName": "slack",
                    "isDM": False,
                },
                "channel": {"_type": "chat:Channel", "id": "slack:C123", "adapterName": "slack", "isDM": False},
            },
        )
        restored: list[Any] = []

        async def _handler(event: Any) -> None:
            restored.append(event)

        owner.on_modal_submit("modal", _handler)
        owner.on_modal_close("modal", _handler)
        other.register_singleton()
        user = create_test_message("message", "Hello").author
        if method == "submit":
            await owner.process_modal_submit(
                ModalSubmitEvent(adapter=adapter, user=user, view_id="view", callback_id="modal", values={}, raw={}),
                "context",
            )
        else:
            tasks: list[Any] = []
            owner.process_modal_close(
                ModalCloseEvent(adapter=adapter, user=user, view_id="view", callback_id="modal", raw={}),
                "context",
                WebhookOptions(wait_until=tasks.append),
            )
            await asyncio.gather(*tasks)

        assert len(restored) == 1
        thread = restored[0].related_thread
        channel = restored[0].related_channel
        assert thread.adapter is adapter
        assert channel.adapter is adapter
        await thread.set_state({"owner": "owner"})
        await channel.set_state({"owner": "owner"})
        assert await state.get(f"thread-state:{_THREAD_ID}") == {"owner": "owner"}
        assert await state.get("channel-state:slack:C123") == {"owner": "owner"}
        assert await other.thread(_THREAD_ID).get_state() is None
        assert await other.channel("slack:C123").get_state() is None
        await thread.post(_hello())
        assert adapter._post_calls[0] == (_THREAD_ID, PostableMarkdown(markdown="Hello"))
        assert other_adapter._post_calls == []
        stream = _native_stream(adapter, None)
        await thread.post(_hello())
        assert stream.await_args.args[2].update_interval_ms == 1200


def _restore_pair(method: str, data: dict[str, Any], adapter: Any) -> tuple[ThreadImpl, ChannelImpl]:
    if method == "standalone":
        restored = json.loads(json.dumps(data), object_hook=reviver)
        return restored["thread"], restored["channel"]
    explicit = adapter if method == "adapter" else None
    return (
        ThreadImpl.from_json(json.loads(json.dumps(data["thread"])), explicit),
        ChannelImpl.from_json(json.loads(json.dumps(data["channel"])), explicit),
    )


@pytest.mark.usefixtures("_no_singleton")
class TestRestoredRuntimeOwnership:
    """describe("restored runtime ownership")"""

    @pytest.mark.parametrize("singleton", ["replaced", "cleared"])
    @pytest.mark.parametrize("access", ["adapter", "state", "stream"])
    @pytest.mark.parametrize("method", ["json", "standalone", "adapter"])
    async def test_retains_the_runtime_after_resolving_first_with_the_singleton(self, method, access, singleton):
        adapter = create_mock_adapter("slack")
        first = _bot(
            {"slack": adapter},
            user_name="first",
            fallback_streaming_placeholder_text=None,
            streaming_update_interval_ms=1200,
        )
        other = create_mock_adapter("slack")
        second = _bot(
            {"slack": other},
            user_name="second",
            fallback_streaming_placeholder_text="Other",
            streaming_update_interval_ms=250,
        )
        data = {
            "thread": first.thread(_THREAD_ID).to_json(),
            "channel": first.channel("slack:C123").to_json(),
        }
        thread, channel = _restore_pair(method, data, adapter)
        first.register_singleton()
        stream = _native_stream(adapter, None)
        if access == "adapter":
            assert thread.adapter is adapter
            assert channel.adapter is adapter
        elif access == "state":
            await thread.get_state()
            await channel.get_state()
        else:
            await thread.post(_reply())
            await channel.post("Channel")
        second.register_singleton()
        if singleton == "cleared":
            clear_chat_singleton()

        await thread.set_state({"owner": "first"})
        await channel.set_state({"owner": "first"})
        await thread.subscribe()
        assert thread.adapter is adapter
        assert channel.adapter is adapter
        assert await first.thread(_THREAD_ID).get_state() == {"owner": "first"}
        assert await first.channel("slack:C123").get_state() == {"owner": "first"}
        assert await second.thread(_THREAD_ID).get_state() is None
        assert await second.channel("slack:C123").get_state() is None
        assert await first.get_state().is_subscribed(_THREAD_ID) is True
        assert await second.get_state().is_subscribed(_THREAD_ID) is False
        await thread.post(_reply())
        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (1200, None)
        clear_chat_singleton()
        await thread.post(_reply())
        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (1200, None)
        assert await thread.channel.get_state() == {"owner": "first"}
        assert other._post_calls == []
        assert {"thread": thread.to_json(), "channel": channel.to_json()} == data

    async def test_does_not_borrow_an_unrelated_runtime_for_an_explicit_adapter(self):
        adapter = create_mock_adapter("slack")
        owner = _bot(
            {"slack": adapter},
            user_name="owner",
            fallback_streaming_placeholder_text=None,
            streaming_update_interval_ms=1200,
        )
        other = _bot(
            {"slack": create_mock_adapter("slack")},
            user_name="other",
            fallback_streaming_placeholder_text="Other",
            streaming_update_interval_ms=250,
        )
        thread = ThreadImpl.from_json(owner.thread(_THREAD_ID).to_json(), adapter)
        channel = ChannelImpl.from_json(owner.channel("slack:C123").to_json(), adapter)
        other.register_singleton()
        stream = _native_stream(adapter, None)

        await thread.post(_reply())

        assert adapter._post_calls[0] == (_THREAD_ID, "...")
        options = stream.await_args.args[2]
        assert options.update_interval_ms == 500
        assert options.fallback_streaming_placeholder_text is UNSET
        # Unowned explicit adapters keep falling back to the singleton's state
        await thread.set_state({"owner": "other"})
        await channel.set_state({"owner": "other"})
        assert await other.thread(_THREAD_ID).get_state() == {"owner": "other"}
        assert await other.channel("slack:C123").get_state() == {"owner": "other"}
        assert await thread.channel.get_state() == {"owner": "other"}
        owner.register_singleton()
        await thread.set_state({"owner": "owner"})
        await channel.set_state({"owner": "owner"})
        other.register_singleton()
        await thread.post(_reply())
        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (1200, None)
        assert await owner.thread(_THREAD_ID).get_state() == {"owner": "owner"}
        assert await owner.channel("slack:C123").get_state() == {"owner": "owner"}


@pytest.mark.usefixtures("_no_singleton")
class TestRestoredRuntimeOwnershipFixes:
    """describe("restored runtime ownership fixes")"""

    async def test_throws_for_an_explicit_chat_that_does_not_own_the_explicit_adapter(self):
        adapter = create_mock_adapter("slack")
        other = _bot({"slack": create_mock_adapter("slack")}, user_name="other")

        # Python raises in from_json; upstream raises on first state use.
        with pytest.raises(RuntimeError, match="does not belong to this Chat instance"):
            ThreadImpl.from_json(other.thread(_THREAD_ID).to_json(), adapter, other)
        with pytest.raises(RuntimeError, match="does not belong to this Chat instance"):
            ChannelImpl.from_json(other.channel("slack:C123").to_json(), adapter, other)
        assert await other.thread(_THREAD_ID).get_state() is None

    async def test_recognizes_adapters_registered_under_a_key_that_differs_from_their_name(self):
        adapter = create_mock_adapter("slack")
        stream = _native_stream(adapter, None)
        state = create_mock_state()
        bot = _bot(
            {"mySlack": adapter},
            state,
            streaming_update_interval_ms=1200,
            fallback_streaming_placeholder_text=None,
        ).register_singleton()
        thread = ThreadImpl.from_json(
            {
                "_type": "chat:Thread",
                "id": _THREAD_ID,
                "channelId": "slack:C123",
                "adapterName": "slack",
                "isDM": False,
            },
            adapter,
        )

        await thread.post(_reply())
        await thread.set_state({"owner": "bot"})

        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (1200, None)
        assert await bot.get_state().get(f"thread-state:{_THREAD_ID}") == {"owner": "bot"}

    async def test_honors_a_streamingplan_updateintervalms_of_0(self):
        adapter = create_mock_adapter("slack")
        stream = _native_stream(adapter, None)
        bot = _bot({"slack": adapter}, streaming_update_interval_ms=1200)
        thread = _decode(json.dumps(bot.thread(_THREAD_ID).to_json()), bot)

        await thread.post(StreamingPlan(_reply(), StreamingPlanOptions(update_interval_ms=0)))

        assert stream.await_args.args[2].update_interval_ms == 0

    async def test_binds_messages_revived_by_botreviver_to_that_bots_adapter(self):
        adapter = create_mock_adapter("slack")
        subject = MessageSubject(id="issue-1", type="issue", title="Bug", raw={})
        adapter.fetch_subject = AsyncMock(return_value=subject)  # type: ignore[attr-defined]
        bot = _bot({"slack": adapter})
        restored = _decode(_dumps({"message": create_test_message("message", "Hello")}), bot)

        assert await restored["message"].subject == subject
        adapter.fetch_subject.assert_awaited_once()

    async def test_from_json_with_chat_binds_the_restored_current_message_to_its_adapter(self):
        # Python-specific: a plain dict (no reviver) so only
        # `ThreadImpl.from_json(..., chat=bot)` can bind `current_message`
        # (upstream thread.ts fromJSON: setMessageAdapter on _currentMessage).
        adapter = create_mock_adapter("slack")
        subject = MessageSubject(id="issue-1", type="issue", title="Bug", raw={})
        adapter.fetch_subject = AsyncMock(return_value=subject)  # type: ignore[attr-defined]
        bot = _bot({"slack": adapter})
        data = {
            "_type": "chat:Thread",
            "id": _THREAD_ID,
            "channelId": "slack:C123",
            "adapterName": "slack",
            "isDM": False,
            "currentMessage": create_test_message("message", "Hello").to_json(),
        }

        thread = ThreadImpl.from_json(json.loads(json.dumps(data)), chat=bot)

        assert thread._current_message is not None
        assert await thread._current_message.subject == subject
        adapter.fetch_subject.assert_awaited_once()


@pytest.mark.usefixtures("_no_singleton")
class TestChatReviverOwnership:
    """describe("chat.reviver()") — ownership cases from vercel/chat#967."""

    @pytest.mark.parametrize("mode", ["replaced", "cached", "cleared"])
    async def test_keeps_threads_and_channels_bound_to_their_chat_when_the_singleton_is(self, mode):
        first_adapter = create_mock_adapter("slack")
        second_adapter = create_mock_adapter("slack")
        first_adapter.post_channel_message = AsyncMock(  # type: ignore[method-assign]
            return_value=RawMessage(id="msg-1", thread_id="slack:C123", raw={})
        )
        second_adapter.post_channel_message = AsyncMock(  # type: ignore[method-assign]
            return_value=RawMessage(id="msg-1", thread_id="slack:C123", raw={})
        )
        first_state = create_mock_state()
        second_state = create_mock_state()
        first = _bot(
            {"slack": first_adapter},
            first_state,
            user_name="first",
            fallback_streaming_placeholder_text=None,
            streaming_update_interval_ms=1000,
        )
        second = _bot(
            {"slack": second_adapter},
            second_state,
            user_name="second",
            fallback_streaming_placeholder_text="Second",
            streaming_update_interval_ms=250,
        )
        payload = _dumps({"thread": first.thread(_THREAD_ID), "channel": first.channel("slack:C123")})
        # Upstream's reviver() registers its Chat as the singleton; Python's
        # does not, so register where upstream would have.
        first.register_singleton()
        restored = _decode(payload, first)
        if mode == "cached":
            assert restored["thread"].adapter is first_adapter
            assert restored["channel"].adapter is first_adapter
        second.register_singleton()
        other = _decode(payload, second)
        delayed = _decode(payload, first)
        if mode == "cleared":
            clear_chat_singleton()

        await restored["thread"].set_state({"owner": "first"})
        await restored["channel"].set_state({"owner": "first"})
        await restored["thread"].subscribe()
        await other["thread"].set_state({"owner": "second"})
        await other["channel"].set_state({"owner": "second"})
        assert await restored["thread"].get_state() == {"owner": "first"}
        assert await delayed["thread"].get_state() == {"owner": "first"}
        assert await restored["thread"].channel.get_state() == {"owner": "first"}
        assert await other["thread"].get_state() == {"owner": "second"}
        assert await other["channel"].get_state() == {"owner": "second"}
        assert await first_state.is_subscribed(_THREAD_ID) is True
        assert await second_state.is_subscribed(_THREAD_ID) is False

        await restored["thread"].post(_reply())
        await other["thread"].post(_reply())
        assert first_adapter._post_calls[0] == (_THREAD_ID, PostableMarkdown(markdown="Reply"))
        assert second_adapter._post_calls[0] == (_THREAD_ID, "Second")
        await restored["channel"].post("First channel")
        first_adapter.post_channel_message.assert_awaited_once_with("slack:C123", "First channel")
        second_adapter.post_channel_message.assert_not_awaited()
        stream = _native_stream(first_adapter)
        await delayed["thread"].post(_reply())
        options = stream.await_args.args[2]
        assert (options.update_interval_ms, options.fallback_streaming_placeholder_text) == (1000, None)
        assert _dumps(restored) == payload

    async def test_does_not_fall_back_to_another_chats_adapter(self):
        chat = _bot({"slack": create_mock_adapter("slack")})
        owner = _bot({}, user_name="owner")
        chat.register_singleton()
        thread_payload = _dumps({"thread": chat.thread(_THREAD_ID)})
        channel_payload = _dumps({"channel": chat.channel("slack:C123")})

        # Python resolves an explicit Chat's adapter in from_json, so the
        # missing adapter raises while decoding rather than on first access.
        with pytest.raises(RuntimeError, match='Adapter "slack" not found'):
            _decode(thread_payload, owner)
        with pytest.raises(RuntimeError, match='Adapter "slack" not found'):
            _decode(channel_payload, owner)

    async def test_retains_ownership_while_streams_from_different_bots_interleave(self):
        chat = _bot({"slack": create_mock_adapter("slack")})
        adapter = create_mock_adapter("slack")
        first = _bot({"slack": adapter}, fallback_streaming_placeholder_text=None)
        payload = _dumps(first.thread(_THREAD_ID))
        thread = _decode(payload, first)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def _delayed():
            started.set()
            await gate.wait()
            yield "First"

        pending = asyncio.ensure_future(thread.post(_delayed()))
        try:
            await started.wait()
            second = _decode(payload, chat)

            async def _immediate():
                yield "Second"

            await second.post(_immediate())
            clear_chat_singleton()
        finally:
            gate.set()
            sent = await pending

        assert (_THREAD_ID, PostableMarkdown(markdown="First")) in adapter._post_calls
        await sent.edit("Edited first")
        await sent.delete()
        assert (_THREAD_ID, sent.id, "Edited first") in adapter._edit_calls
        assert adapter._delete_calls == [(_THREAD_ID, sent.id)]
        assert chat.get_adapter("slack")._delete_calls == []

    async def test_rebinds_serialized_objects_without_retaining_the_previous_runtime(self):
        chat = _bot({"slack": create_mock_adapter("slack")})
        payload = _dumps(
            {
                "nested": [chat.thread(_THREAD_ID), chat.channel("slack:C123")],
                "message": create_test_message("message", "Hello"),
                "values": [None, False, 0, "", {"_type": "unknown"}],
            }
        )
        restored = _decode(payload, chat)
        assert isinstance(restored["message"].metadata.date_sent, datetime)
        assert restored["values"] == [None, False, 0, "", {"_type": "unknown"}]
        adapter = create_mock_adapter("slack")
        receiving = _bot({"slack": adapter}, user_name="receiving")
        rebound = _decode(_dumps(restored), receiving)
        clear_chat_singleton()
        assert rebound["nested"][0].adapter is adapter
        assert rebound["nested"][1].adapter is adapter
        assert restored["nested"][0].adapter is chat.get_adapter("slack")
        assert _dumps(rebound) == payload

        thread = ThreadImpl.from_json(json.loads(_dumps(restored["nested"][0])))
        channel = ChannelImpl.from_json(json.loads(_dumps(restored["nested"][1])))
        with pytest.raises(RuntimeError, match="No Chat instance available"):
            _ = thread.adapter
        with pytest.raises(RuntimeError, match="No Chat instance available"):
            _ = channel.adapter
        receiving.register_singleton()
        assert thread.adapter is adapter
        assert channel.adapter is adapter
