"""Port of adapter-twilio/src/index.test.ts -- the TwilioAdapter itself.

Covers thread-ID encode/decode/channel for phone and channel-prefixed
addresses, ``open_dm``, webhook routing into ``chat.process_message``
(including MMS media attachments), private-media rehydration with adapter
credentials, the outbound send paths (SMS, MMS by URL, media-only, the
messaging-service sender, thread-ID stability), inbound REST parsing, the
``NotImplementedError`` surfaces, and the Python-only SSRF guard on media
downloads (a documented divergence from upstream).

Upstream injects a ``fetch`` stand-in; the Python adapter injects an
``http_request`` transport returning :class:`TwilioHttpResponse`, so the
mocks here speak that shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qsl, urlencode

import pytest

from chat_sdk.adapters.twilio import (
    TwilioAdapter,
    TwilioMessageResource,
    create_twilio_adapter,
)
from chat_sdk.adapters.twilio.types import TwilioHttpResponse, TwilioThreadId
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import Attachment, Author, FetchOptions, Message, PostableMarkdown

BASIC_AUTH = "Basic QUMxMjM6dG9rZW4="  # base64("AC123:token")


@dataclass
class _FakeRequest:
    """Minimal request-like object (``url``/``method``/``headers``/``text``)."""

    _body: str = ""
    url: str = "https://example.com/twilio"
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)

    async def text(self) -> str:
        return self._body


def _form_request(fields: dict[str, str]) -> _FakeRequest:
    return _FakeRequest(
        _body=urlencode(fields),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def _mock_http(payload: Any) -> AsyncMock:
    """An ``http_request`` transport returning a 200 JSON (or raw) body."""
    body = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
    return AsyncMock(return_value=TwilioHttpResponse(status=200, body=body))


def _sent_form(mock: AsyncMock) -> dict[str, str]:
    """The form body of the (single) request the transport received."""
    body = mock.await_args.args[3]
    assert body is not None
    return dict(parse_qsl(body, keep_blank_values=True))


def _sent_form_pairs(mock: AsyncMock) -> list[tuple[str, str]]:
    body = mock.await_args.args[3]
    assert body is not None
    return parse_qsl(body, keep_blank_values=True)


def _mock_chat() -> MagicMock:
    """A ChatInstance double; ``process_message`` is sync (returns None)."""
    chat = MagicMock()
    chat.process_message = MagicMock(return_value=None)
    return chat


class TestThreadIds:
    """Thread-ID encode/decode/channel through the adapter facade."""

    def test_encodes_and_decodes_phone_and_channel_address_thread_ids(self):
        adapter = create_twilio_adapter()
        thread = TwilioThreadId(recipient="whatsapp:+15550000002", sender="whatsapp:+15550000001")

        thread_id = adapter.encode_thread_id(thread)

        assert thread_id == "twilio:whatsapp%3A%2B15550000001:whatsapp%3A%2B15550000002"
        assert adapter.decode_thread_id(thread_id) == thread
        assert adapter.channel_id_from_thread_id(thread_id) == "twilio:whatsapp%3A%2B15550000001"

    def test_is_dm_is_true_for_twilio_thread_ids(self):
        adapter = create_twilio_adapter()
        assert adapter.is_dm("twilio:%2B1:%2B2") is True
        assert adapter.is_dm("slack:C1:1.2") is False

    @pytest.mark.asyncio
    async def test_opens_dms_with_the_configured_phone_number(self):
        adapter = create_twilio_adapter(phone_number="+15550000001")
        assert await adapter.open_dm("+15550000002") == "twilio:%2B15550000001:%2B15550000002"

    @pytest.mark.asyncio
    async def test_open_dm_without_a_sender_raises(self):
        adapter = create_twilio_adapter()
        with pytest.raises(ValidationError, match="phoneNumber or messagingServiceSid is required"):
            await adapter.open_dm("+15550000002")


class TestWebhookRouting:
    """handle_webhook -> chat.process_message."""

    @pytest.mark.asyncio
    async def test_routes_incoming_message_webhooks_to_chat_processing(self):
        chat = _mock_chat()
        adapter = create_twilio_adapter(
            http_request=_mock_http("media"),
            webhook_verifier=lambda request, body: True,
        )
        await adapter.initialize(chat)

        response = await adapter.handle_webhook(
            _form_request(
                {
                    "Body": "hello",
                    "From": "+15550000002",
                    "MediaContentType0": "image/jpeg",
                    "MediaUrl0": "https://api.twilio.com/media/photo",
                    "MessageSid": "SM123",
                    "NumMedia": "1",
                    "To": "+15550000001",
                }
            )
        )

        assert response["status"] == 200
        assert response["body"] == "<Response></Response>"
        chat.process_message.assert_called_once()
        _adapter_arg, thread_id, message, _options = chat.process_message.call_args.args
        assert thread_id == "twilio:%2B15550000001:%2B15550000002"
        assert isinstance(message, Message)
        assert message.text == "hello"
        assert message.attachments[0].mime_type == "image/jpeg"
        assert message.attachments[0].type == "image"
        assert message.attachments[0].url == "https://api.twilio.com/media/photo"

    @pytest.mark.asyncio
    async def test_acknowledges_non_text_webhooks_without_processing(self):
        chat = _mock_chat()
        adapter = create_twilio_adapter(webhook_verifier=lambda request, body: True)
        await adapter.initialize(chat)

        # A status callback is not a text payload: ack, but do not route.
        response = await adapter.handle_webhook(_form_request({"MessageStatus": "delivered", "MessageSid": "SM1"}))

        assert response["body"] == "<Response></Response>"
        chat.process_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_signature_returns_401(self):
        adapter = create_twilio_adapter(auth_token="token")
        request = _FakeRequest(_body="Body=hello", headers={"x-twilio-signature": "invalid"})
        await adapter.initialize(_mock_chat())

        response = await adapter.handle_webhook(request)

        assert response["status"] == 401


class TestRehydrateAttachment:
    """rehydrate_attachment rebuilds the authenticated downloader."""

    @pytest.mark.asyncio
    async def test_rehydrates_private_media_fetchers_with_adapter_credentials(self):
        http = _mock_http("photo")
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        attachment = adapter.rehydrate_attachment(
            Attachment(type="image", fetch_metadata={"twilioMediaUrl": "https://api.twilio.com/media/photo"})
        )

        assert attachment.fetch_data is not None
        data = await attachment.fetch_data()
        assert data == b"photo"
        method, url, headers, _body = http.await_args.args
        assert method == "GET"
        assert url == "https://api.twilio.com/media/photo"
        assert headers["authorization"] == BASIC_AUTH

    def test_rehydrate_without_a_url_returns_the_attachment_unchanged(self):
        adapter = create_twilio_adapter()
        original = Attachment(type="file")
        assert adapter.rehydrate_attachment(original) is original

    @pytest.mark.asyncio
    async def test_media_downloader_refuses_untrusted_hosts(self):
        # Divergence from upstream: the SSRF guard fails closed rather than
        # forwarding Basic auth to an arbitrary host rehydrated from state.
        http = _mock_http("photo")
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        attachment = adapter.rehydrate_attachment(
            Attachment(type="image", fetch_metadata={"twilioMediaUrl": "https://evil.example/steal"})
        )

        assert attachment.fetch_data is not None
        with pytest.raises(ValidationError, match="untrusted URL"):
            await attachment.fetch_data()
        http.assert_not_awaited()


class TestPostMessage:
    """Outbound send paths through the Messages API."""

    @pytest.mark.asyncio
    async def test_posts_sms_messages_through_the_messages_api(self):
        http = _mock_http(
            {
                "body": "hello",
                "direction": "outbound-api",
                "from": "+15550000001",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(
            account_sid="AC123",
            auth_token="token",
            http_request=http,
            phone_number="+15550000001",
        )

        result = await adapter.post_message("twilio:%2B15550000001:%2B15550000002", "hello")

        assert result.id == "SM123"
        assert result.thread_id == "twilio:%2B15550000001:%2B15550000002"
        form = _sent_form(http)
        assert form["Body"] == "hello"
        assert form["From"] == "+15550000001"
        assert form["To"] == "+15550000002"

    @pytest.mark.asyncio
    async def test_keeps_messaging_service_threads_stable_after_sending(self):
        http = _mock_http(
            {
                "body": "hello",
                "direction": "outbound-api",
                "from": "+15550000001",
                "messaging_service_sid": "MG123",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        result = await adapter.post_message("twilio:MG123:%2B15550000002", "hello")

        assert result.thread_id == "twilio:MG123:%2B15550000002"

    @pytest.mark.asyncio
    async def test_posts_mms_messages_from_attachment_urls(self):
        http = _mock_http(
            {
                "body": "photo",
                "direction": "outbound-api",
                "from": "+15550000001",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        await adapter.post_message(
            "twilio:%2B15550000001:%2B15550000002",
            PostableMarkdown(
                markdown="photo",
                attachments=[Attachment(type="image", url="https://example.com/photo.jpg")],
            ),
        )

        assert _sent_form(http)["MediaUrl"] == "https://example.com/photo.jpg"

    @pytest.mark.asyncio
    async def test_posts_media_only_mms_without_a_blank_body(self):
        http = _mock_http(
            {
                "direction": "outbound-api",
                "from": "+15550000001",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        await adapter.post_message(
            "twilio:%2B15550000001:%2B15550000002",
            PostableMarkdown(
                markdown="",
                attachments=[Attachment(type="image", url="https://example.com/photo.jpg")],
            ),
        )

        names = [name for name, _value in _sent_form_pairs(http)]
        assert "Body" not in names
        assert _sent_form(http)["MediaUrl"] == "https://example.com/photo.jpg"

    @pytest.mark.asyncio
    async def test_rejects_media_attachments_without_public_urls(self):
        adapter = create_twilio_adapter(
            account_sid="AC123",
            auth_token="token",
            http_request=_mock_http({"sid": "SM123"}),
        )

        with pytest.raises(ValidationError, match="public URL"):
            await adapter.post_message(
                "twilio:%2B15550000001:%2B15550000002",
                PostableMarkdown(markdown="photo", attachments=[Attachment(type="image")]),
            )

    @pytest.mark.asyncio
    async def test_uses_messaging_service_senders(self):
        http = _mock_http(
            {
                "body": "hello",
                "direction": "outbound-api",
                "from": "MG123",
                "messaging_service_sid": "MG123",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(
            account_sid="AC123",
            auth_token="token",
            http_request=http,
            messaging_service_sid="MG123",
        )

        await adapter.post_message("twilio:MG123:%2B15550000002", "hello")

        form = _sent_form(http)
        assert form["MessagingServiceSid"] == "MG123"
        assert "From" not in form

    @pytest.mark.asyncio
    async def test_rejects_empty_messages(self):
        adapter = create_twilio_adapter(
            account_sid="AC123",
            auth_token="token",
            http_request=_mock_http({"sid": "SM123"}),
        )
        with pytest.raises(ValidationError, match="Message text cannot be empty"):
            await adapter.post_message("twilio:%2B15550000001:%2B15550000002", "")


class TestParseMessage:
    """parse_message over Messages API resources and webhook payloads."""

    def test_parses_inbound_rest_messages_with_the_sender_as_author(self):
        adapter = create_twilio_adapter()
        raw: TwilioMessageResource = {
            "body": "hello",
            "date_created": "Tue, 01 Apr 2025 12:00:00 +0000",
            "direction": "inbound",
            "from": "+15550000002",
            "sid": "SM123",
            "to": "+15550000001",
        }

        message = adapter.parse_message(raw)

        assert message.author.user_id == "+15550000002"
        assert message.author.is_me is False
        assert message.thread_id == "twilio:%2B15550000001:%2B15550000002"

    def test_parses_outbound_rest_messages_as_the_bot(self):
        adapter = create_twilio_adapter()
        raw: TwilioMessageResource = {
            "body": "hi back",
            "direction": "outbound-api",
            "from": "+15550000001",
            "sid": "SM124",
            "to": "+15550000002",
        }

        message = adapter.parse_message(raw)

        assert message.author.is_me is True
        assert message.author.user_id == "+15550000001"
        assert message.thread_id == "twilio:%2B15550000001:%2B15550000002"

    def test_resource_missing_routing_raises(self):
        adapter = create_twilio_adapter()
        with pytest.raises(ValidationError, match="missing routing"):
            adapter.parse_message({"sid": "SM1", "direction": "inbound"})


class TestNotImplemented:
    """Surfaces Twilio genuinely cannot support."""

    @pytest.mark.asyncio
    async def test_edit_message_raises(self):
        adapter = create_twilio_adapter()
        with pytest.raises(ChatNotImplementedError, match="editMessage"):
            await adapter.edit_message("twilio:%2B1:%2B2", "SM1", "x")

    @pytest.mark.asyncio
    async def test_add_reaction_raises(self):
        adapter = create_twilio_adapter()
        with pytest.raises(ChatNotImplementedError, match="addReaction"):
            await adapter.add_reaction("twilio:%2B1:%2B2", "SM1", "👍")

    @pytest.mark.asyncio
    async def test_remove_reaction_raises(self):
        adapter = create_twilio_adapter()
        with pytest.raises(ChatNotImplementedError, match="removeReaction"):
            await adapter.remove_reaction("twilio:%2B1:%2B2", "SM1", "👍")


class TestAdapterProperties:
    """Static adapter metadata."""

    def test_exposes_twilio_adapter_metadata(self):
        adapter = create_twilio_adapter()
        assert adapter.name == "twilio"
        assert adapter.lock_scope == "channel"
        assert adapter.persist_thread_history is True
        assert adapter.user_name == "bot"
        assert adapter.bot_user_id is None

    def test_user_name_is_configurable(self):
        assert create_twilio_adapter(user_name="concierge").user_name == "concierge"

    def test_constructs_directly_without_config(self):
        adapter = TwilioAdapter()
        assert adapter.name == "twilio"


class TestStreaming:
    """stream() buffers chunks and posts a single message."""

    @pytest.mark.asyncio
    async def test_stream_accumulates_chunks_into_one_send(self):
        http = _mock_http(
            {
                "body": "hello world",
                "direction": "outbound-api",
                "from": "+15550000001",
                "sid": "SM123",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        async def chunks() -> Any:
            yield "hello "
            yield "world"

        result = await adapter.stream("twilio:%2B15550000001:%2B15550000002", chunks())

        assert result.id == "SM123"
        http.assert_awaited_once()
        assert _sent_form(http)["Body"] == "hello world"

    @pytest.mark.asyncio
    async def test_stream_ignores_thinking_chunk(self):
        """A ``ThinkingChunk`` is streaming-only reasoning, not message content.

        The text-accumulating adapter must skip it without crashing and the
        posted body must equal the text chunks only.
        """
        from chat_sdk.types import ThinkingChunk

        http = _mock_http(
            {
                "body": "hello world",
                "direction": "outbound-api",
                "from": "+15550000001",
                "sid": "SM124",
                "to": "+15550000002",
            }
        )
        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=http)

        async def chunks() -> Any:
            yield ThinkingChunk(content="reasoning")
            yield "hello "
            yield ThinkingChunk(content="more reasoning")
            yield "world"

        result = await adapter.stream("twilio:%2B15550000001:%2B15550000002", chunks())

        assert result.id == "SM124"
        assert _sent_form(http)["Body"] == "hello world"


class TestFetchMessages:
    """fetch_message / fetch_messages over the Messages API."""

    @pytest.mark.asyncio
    async def test_fetch_message_returns_none_on_api_failure(self):
        # fetch_twilio_message raises ResourceNotFoundError on 404; the
        # adapter swallows it and yields None (upstream's try/catch).
        async def failing(method: str, url: str, headers: Any, body: Any) -> TwilioHttpResponse:
            return TwilioHttpResponse(status=404, body=b"")

        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=failing)
        assert await adapter.fetch_message("twilio:%2B15550000001:%2B15550000002", "SM404") is None

    @pytest.mark.asyncio
    async def test_fetch_messages_merges_both_directions_sorted_by_date(self):
        outbound = {
            "messages": [
                {
                    "sid": "SMout",
                    "direction": "outbound-api",
                    "from": "+15550000001",
                    "to": "+15550000002",
                    "body": "from bot",
                    "date_sent": "Tue, 01 Apr 2025 12:00:02 +0000",
                }
            ]
        }
        inbound = {
            "messages": [
                {
                    "sid": "SMin",
                    "direction": "inbound",
                    "from": "+15550000002",
                    "to": "+15550000001",
                    "body": "from user",
                    "date_sent": "Tue, 01 Apr 2025 12:00:01 +0000",
                }
            ]
        }

        async def transport(method: str, url: str, headers: Any, body: Any) -> TwilioHttpResponse:
            # Both list calls hit Messages.json (GET); the bot-as-sender call
            # carries From=<sender> in the query, the inbound call From=<recipient>.
            payload = outbound if "From=%2B15550000001" in url else inbound
            return TwilioHttpResponse(status=200, body=json.dumps(payload).encode("utf-8"))

        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=transport)

        result = await adapter.fetch_messages("twilio:%2B15550000001:%2B15550000002")

        # Sorted ascending by date_sent: inbound (12:00:01) before outbound (12:00:02).
        assert [message.id for message in result.messages] == ["SMin", "SMout"]
        assert result.messages[0].author.is_me is False
        assert result.messages[1].author.is_me is True

    @pytest.mark.asyncio
    async def test_fetch_messages_respects_the_limit_after_merge(self):
        def page(prefix: str, count: int, base_second: int) -> dict[str, Any]:
            return {
                "messages": [
                    {
                        "sid": f"{prefix}{index}",
                        "direction": "inbound" if prefix == "in" else "outbound-api",
                        "from": "+15550000002" if prefix == "in" else "+15550000001",
                        "to": "+15550000001" if prefix == "in" else "+15550000002",
                        "body": "x",
                        "date_sent": f"Tue, 01 Apr 2025 12:00:{base_second + index:02d} +0000",
                    }
                    for index in range(count)
                ]
            }

        async def transport(method: str, url: str, headers: Any, body: Any) -> TwilioHttpResponse:
            payload = page("out", 2, 30) if "From=%2B15550000001" in url else page("in", 2, 10)
            return TwilioHttpResponse(status=200, body=json.dumps(payload).encode("utf-8"))

        adapter = create_twilio_adapter(account_sid="AC123", auth_token="token", http_request=transport)

        result = await adapter.fetch_messages("twilio:%2B15550000001:%2B15550000002", FetchOptions(limit=3))

        # 4 messages merged, newest 3 kept (slice(-limit)); the oldest inbound drops.
        assert len(result.messages) == 3
        assert [message.id for message in result.messages] == ["in1", "out0", "out1"]


class TestFetchThreadAndUser:
    """fetch_thread / get_user echo the address pair."""

    @pytest.mark.asyncio
    async def test_fetch_thread_is_a_dm_keyed_on_the_address_pair(self):
        adapter = create_twilio_adapter()
        info = await adapter.fetch_thread("twilio:%2B15550000001:%2B15550000002")
        assert info.is_dm is True
        assert info.channel_id == "twilio:%2B15550000001"
        assert info.channel_name == "+15550000001"
        assert info.metadata == {"recipient": "+15550000002", "sender": "+15550000001"}

    @pytest.mark.asyncio
    async def test_get_user_echoes_the_phone_number(self):
        adapter = create_twilio_adapter()
        user = await adapter.get_user("+15550000002")
        assert user is not None
        assert user.user_id == "+15550000002"
        assert user.full_name == "+15550000002"
        assert user.is_bot is False


def test_author_helper_marks_bot_authorship():
    # The bot side is both ``is_me`` and ``is_bot`` (no Twilio bot identity).
    author = TwilioAdapter._author("+15550000001", True)
    assert isinstance(author, Author)
    assert author.is_me is True
    assert author.is_bot is True
    assert author.user_id == "+15550000001"
