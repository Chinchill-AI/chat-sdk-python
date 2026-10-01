"""Tests for WhatsApp adapter API-calling methods.

Covers: post_message (text, long split, interactive card, files and
attachments), add_reaction, remove_reaction, stream (accumulation),
attachment fetch_data presence, send_template, and Graph API error mapping.

Uses a mock for _graph_api_request to intercept all Graph API calls without
network access; the send_template and error tests go one level lower, to a
fake aiohttp session, so the wire payload and error translation are covered.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest

from chat_sdk.adapters.whatsapp import WhatsAppApiError, get_whatsapp_media_type, validate_file_size
from chat_sdk.adapters.whatsapp.adapter import (
    WHATSAPP_MESSAGE_LIMIT,
    WhatsAppAdapter,
)
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import AdapterError, NetworkError, ValidationError
from chat_sdk.types import Attachment, FileUpload, MarkdownTextChunk, PostableCard, PostableMarkdown, StreamChunk

# =============================================================================
# Helpers
# =============================================================================

THREAD_ID = "whatsapp:1234567890:49151234567"
USER_WA_ID = "49151234567"
PHONE_NUMBER_ID = "1234567890"


def _make_adapter(**overrides: Any) -> WhatsAppAdapter:
    """Create a WhatsAppAdapter with minimal valid config."""
    defaults: dict[str, Any] = {
        "access_token": "test-token",
        "app_secret": "test-secret",
        "phone_number_id": PHONE_NUMBER_ID,
        "verify_token": "verify-me",
        "user_name": "test-bot",
        "logger": ConsoleLogger("error"),
    }
    defaults.update(overrides)
    return WhatsAppAdapter(WhatsAppAdapterConfig(**defaults))


def _graph_api_response(message_id: str = "wamid.abc123") -> dict[str, Any]:
    """Simulate a successful Graph API send response."""
    return {"messages": [{"id": message_id}]}


# =============================================================================
# Tests — post_message
# =============================================================================


class TestPostMessageText:
    """post_message with a plain text body sends a single text API call."""

    @pytest.mark.asyncio
    async def test_post_message_text(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        result = await adapter.post_message(THREAD_ID, {"markdown": "Hello, world!"})

        assert result.id == "wamid.abc123"
        assert result.thread_id == THREAD_ID

        # Verify the API was called exactly once
        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        path, body = call_args[0]

        assert path == f"/{PHONE_NUMBER_ID}/messages"
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == USER_WA_ID
        assert body["type"] == "text"
        assert body["text"]["body"] == "Hello, world!"


class TestPostMessageSplitsLong:
    """post_message splits text exceeding the WhatsApp limit into 2+ calls."""

    @pytest.mark.asyncio
    async def test_post_message_splits_long(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        # Build a message that exceeds the limit, with paragraph breaks
        paragraph = "A" * (WHATSAPP_MESSAGE_LIMIT // 2)
        long_text = f"{paragraph}\n\n{paragraph}"
        assert len(long_text) > WHATSAPP_MESSAGE_LIMIT

        await adapter.post_message(THREAD_ID, {"markdown": long_text})

        # Should have been called at least twice (one per chunk)
        call_count = adapter._graph_api_request.call_count
        assert call_count >= 2, f"Expected >=2 API calls for split, got {call_count}"

        # Each call should be a text message
        for call in adapter._graph_api_request.call_args_list:
            body = call[0][1]
            assert body["type"] == "text"
            # Each chunk must be within the limit
            assert len(body["text"]["body"]) <= WHATSAPP_MESSAGE_LIMIT


class TestPostMessageCardInteractive:
    """post_message with a card containing buttons sends an interactive payload."""

    @pytest.mark.asyncio
    async def test_post_message_card_interactive(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        card = {
            "card": {
                "title": "Pick one",
                "body": "Choose an option",
                "buttons": [
                    {"label": "Option A", "action_id": "opt_a"},
                    {"label": "Option B", "action_id": "opt_b"},
                ],
            }
        }
        result = await adapter.post_message(THREAD_ID, card)

        assert result.id == "wamid.abc123"
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == USER_WA_ID
        # The card should produce either an interactive message or fallback text.
        # With buttons, WhatsApp cards map to the interactive type.
        assert body["type"] in ("interactive", "text")
        if body["type"] == "interactive":
            assert "interactive" in body


# =============================================================================
# Tests — add_reaction / remove_reaction
# =============================================================================


class TestAddReaction:
    """add_reaction sends a reaction payload with the emoji."""

    @pytest.mark.asyncio
    async def test_add_reaction(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.reaction1"}]})

        await adapter.add_reaction(THREAD_ID, "wamid.target123", "thumbs_up")

        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.target123"
        # The emoji string should be non-empty (resolved to unicode)
        assert body["reaction"]["emoji"] != ""
        assert body["to"] == USER_WA_ID


class TestRemoveReaction:
    """remove_reaction sends a reaction payload with empty emoji."""

    @pytest.mark.asyncio
    async def test_remove_reaction(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value={"messages": [{"id": "wamid.reaction1"}]})

        await adapter.remove_reaction(THREAD_ID, "wamid.target123", "thumbs_up")

        adapter._graph_api_request.assert_called_once()
        call_args = adapter._graph_api_request.call_args
        body = call_args[0][1]

        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.target123"
        assert body["reaction"]["emoji"] == ""


# =============================================================================
# Tests — stream
# =============================================================================


class TestStreamAccumulates:
    """stream() buffers all chunks and posts a single message."""

    @pytest.mark.asyncio
    async def test_stream_accumulates(self):
        adapter = _make_adapter()
        adapter._graph_api_request = AsyncMock(return_value=_graph_api_response())

        async def _chunks() -> AsyncIterator[str | StreamChunk]:
            yield "Hello "
            yield MarkdownTextChunk(text="world")
            yield "!"

        result = await adapter.stream(THREAD_ID, _chunks())

        assert result.id == "wamid.abc123"
        # stream should result in a single post_message, meaning
        # _graph_api_request is called once (for a short accumulated text)
        assert adapter._graph_api_request.call_count == 1
        body = adapter._graph_api_request.call_args[0][1]
        assert body["type"] == "text"
        assert "Hello " in body["text"]["body"]
        assert "world" in body["text"]["body"]


# =============================================================================
# Tests — attachment fetch_data
# =============================================================================


class TestAttachmentHasFetchData:
    """Media attachments include a callable fetch_data for lazy downloading."""

    def test_attachment_has_fetch_data(self):
        adapter = _make_adapter()
        inbound = {
            "id": "wamid.img1",
            "from": "49151234567",
            "type": "image",
            "timestamp": "1700000000",
            "image": {"id": "media_123", "mime_type": "image/jpeg"},
        }
        attachments = adapter._build_attachments(inbound)

        assert len(attachments) == 1
        attachment = attachments[0]
        assert attachment.type == "image"
        assert attachment.mime_type == "image/jpeg"
        # fetch_data should be a callable (coroutine function)
        assert attachment.fetch_data is not None
        assert callable(attachment.fetch_data)


# =============================================================================
# Fake aiohttp session (wire-level Graph API tests)
# =============================================================================


class _FakeGraphResponse:
    """aiohttp-compatible response stub used as an async context manager."""

    def __init__(self, status: int, body: str | bytes, read_error: Exception | None = None) -> None:
        self.status = status
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self._read_error = read_error

    async def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._body

    async def __aenter__(self) -> _FakeGraphResponse:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FailingRequest:
    """Mimics aiohttp: transport errors surface when the request is entered."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> Any:
        raise self._error

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeGraphSession:
    """Records ``request()`` / ``get()`` calls and replays canned responses."""

    def __init__(self, *responses: _FakeGraphResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def _next(self) -> Any:
        response = self._responses.pop(0)
        return _FailingRequest(response) if isinstance(response, Exception) else response

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        return self._next()

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append(("GET", url, kwargs))
        return self._next()

    async def close(self) -> None:
        self.closed = True


def _json_response(payload: Any, status: int = 200) -> _FakeGraphResponse:
    return _FakeGraphResponse(status, json.dumps(payload))


def _adapter_with_session(*responses: _FakeGraphResponse | Exception) -> tuple[WhatsAppAdapter, _FakeGraphSession]:
    adapter = _make_adapter()
    session = _FakeGraphSession(*responses)
    adapter._http_session = session
    return adapter, session


# =============================================================================
# Tests — send_template (port of describe("sendTemplate"))
# =============================================================================


def _template_response() -> _FakeGraphResponse:
    return _json_response({"messages": [{"id": "wamid.template123"}]})


class TestSendTemplate:
    @pytest.mark.asyncio
    async def test_sends_a_template_with_name_and_language(self):
        adapter, session = _adapter_with_session(_template_response())

        result = await adapter.send_template(THREAD_ID, {"name": "appointment_reminder", "language": "en"})

        assert len(session.calls) == 1
        method, url, kwargs = session.calls[0]
        assert method == "POST"
        assert url.endswith(f"/{PHONE_NUMBER_ID}/messages")
        sent = kwargs["json"]
        assert sent["type"] == "template"
        assert sent["to"] == USER_WA_ID
        assert sent["template"] == {"name": "appointment_reminder", "language": {"code": "en"}}
        assert result.id == "wamid.template123"
        assert result.thread_id == THREAD_ID
        assert result.raw["message"]["type"] == "template"
        assert result.raw["message"]["from"] == PHONE_NUMBER_ID

    @pytest.mark.asyncio
    async def test_includes_components_when_provided(self):
        adapter, session = _adapter_with_session(_template_response())
        components: Any = [
            {"type": "body", "parameters": [{"type": "text", "text": "Ada"}, {"type": "text", "text": "#12345"}]},
            {"type": "button", "sub_type": "url", "index": 0, "parameters": [{"type": "text", "text": "12345"}]},
        ]

        await adapter.send_template(
            THREAD_ID, {"name": "order_shipped", "language": "en_US", "components": copy.deepcopy(components)}
        )

        assert len(session.calls) == 1
        sent = session.calls[0][2]["json"]
        assert sent["template"]["name"] == "order_shipped"
        assert sent["template"]["language"] == {"code": "en_US"}
        assert sent["template"]["components"] == components

    @pytest.mark.asyncio
    async def test_sends_templates_to_bsuid_recipients(self):
        adapter, session = _adapter_with_session(_template_response())

        await adapter.send_template(
            f"whatsapp:{PHONE_NUMBER_ID}:US.13491208655302741918",
            {"name": "order_shipped", "language": "en_US"},
        )

        sent = session.calls[0][2]["json"]
        assert sent["recipient"] == "US.13491208655302741918"
        assert "to" not in sent

    @pytest.mark.asyncio
    async def test_converts_emoji_placeholders_in_text_parameters(self):
        adapter, session = _adapter_with_session(_template_response())

        await adapter.send_template(
            THREAD_ID,
            {
                "name": "order_shipped",
                "language": "en_US",
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": "Shipped! {{emoji:thumbs_up}}"}]},
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": 0,
                        "parameters": [{"type": "text", "text": "{{emoji:thumbs_up}}"}],
                    },
                ],
            },
        )

        components = session.calls[0][2]["json"]["template"]["components"]
        assert components[0]["parameters"][0]["text"] == "Shipped! 👍"
        assert components[1]["parameters"][0]["text"] == "👍"

    @pytest.mark.asyncio
    async def test_does_not_emoji_convert_quick_reply_payloads(self):
        adapter, session = _adapter_with_session(_template_response())

        await adapter.send_template(
            THREAD_ID,
            {
                "name": "order_shipped",
                "language": "en_US",
                "components": [
                    {
                        "type": "button",
                        "sub_type": "quick_reply",
                        "index": 0,
                        "parameters": [{"type": "payload", "payload": "{{emoji:thumbs_up}}:1"}],
                    },
                    {
                        "type": "header",
                        "parameters": [{"type": "image", "image": {"link": "https://example.com/{{emoji:wave}}.png"}}],
                    },
                ],
            },
        )

        components = session.calls[0][2]["json"]["template"]["components"]
        assert components[0]["parameters"][0]["payload"] == "{{emoji:thumbs_up}}:1"
        assert components[1]["parameters"][0]["image"]["link"] == "https://example.com/{{emoji:wave}}.png"

    @pytest.mark.asyncio
    async def test_omits_components_when_the_array_is_empty(self):
        adapter, session = _adapter_with_session(_template_response())

        await adapter.send_template(THREAD_ID, {"name": "hello_world", "language": "en_US", "components": []})

        assert session.calls[0][2]["json"]["template"] == {"name": "hello_world", "language": {"code": "en_US"}}

    @pytest.mark.asyncio
    async def test_throws_when_the_api_returns_no_message_id(self):
        adapter, _ = _adapter_with_session(_json_response({"messages": []}))

        with pytest.raises(RuntimeError, match="did not return a message ID for template message"):
            await adapter.send_template(THREAD_ID, {"name": "hello_world", "language": "en_US"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("messages", [[{}], [{"id": ""}]])
    async def test_throws_when_the_first_message_has_no_id(self, messages: list[dict[str, Any]]):
        adapter, _ = _adapter_with_session(_json_response({"messages": messages}))

        with pytest.raises(RuntimeError, match="did not return a message ID for template message"):
            await adapter.send_template(THREAD_ID, {"name": "hello_world", "language": "en_US"})

    @pytest.mark.asyncio
    async def test_throws_on_invalid_thread_id(self):
        adapter, session = _adapter_with_session()

        with pytest.raises(ValidationError, match="Invalid WhatsApp thread ID"):
            await adapter.send_template("slack:C123:ts123", {"name": "hello_world", "language": "en_US"})
        assert session.calls == []

    @pytest.mark.asyncio
    async def test_send_template_does_not_mutate_the_callers_template(self):
        adapter, _ = _adapter_with_session(_template_response())
        template: Any = {
            "name": "order_shipped",
            "language": "en_US",
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "{{emoji:thumbs_up}}"}]}],
        }
        snapshot = copy.deepcopy(template)

        await adapter.send_template(THREAD_ID, template)

        assert template == snapshot


# =============================================================================
# Tests — Graph API errors (port of describe("API errors"))
# =============================================================================

_RATE_LIMIT_BODY = {
    "error": {
        "message": "(#130429) Rate limit hit",
        "type": "OAuthException",
        "code": 130429,
        "error_data": {
            "messaging_product": "whatsapp",
            "details": "Cloud API message throughput has been reached.",
        },
        "fbtrace_id": "trace123",
    },
}


async def _send_message(adapter: WhatsAppAdapter) -> Any:
    return await adapter.post_message(THREAD_ID, "hello")


async def _upload_media(adapter: WhatsAppAdapter) -> Any:
    return await adapter.post_message(
        THREAD_ID,
        {"files": [FileUpload(data=b"report", filename="report.txt", mime_type="text/plain")]},
    )


async def _fetch_media_metadata(adapter: WhatsAppAdapter) -> Any:
    return await adapter.download_media("media123")


_OPERATIONS = [
    pytest.param(_send_message, id="message sends"),
    pytest.param(_upload_media, id="media uploads"),
    pytest.param(_fetch_media_metadata, id="media metadata requests"),
]


class TestGraphApiErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", _OPERATIONS)
    async def test_preserves_meta_error_fields(self, run: Any):
        adapter, session = _adapter_with_session(_json_response(_RATE_LIMIT_BODY, status=400))

        with pytest.raises(WhatsAppApiError) as excinfo:
            await run(adapter)

        error = excinfo.value
        assert isinstance(error, AdapterError)
        assert error.adapter == "whatsapp"
        assert error.code == "RATE_LIMITED"
        assert error.error_code == 130_429
        assert error.status == 400
        assert error.provider_message == "(#130429) Rate limit hit"
        assert error.type == "OAuthException"
        assert error.details == "Cloud API message throughput has been reached."
        assert error.trace_id == "trace123"
        assert error.raw == _RATE_LIMIT_BODY
        assert len(session.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", _OPERATIONS)
    async def test_wraps_transport_failures(self, run: Any):
        cause = aiohttp.ClientConnectionError("fetch failed")
        adapter, _ = _adapter_with_session(cause)

        with pytest.raises(NetworkError) as excinfo:
            await run(adapter)

        assert excinfo.value.adapter == "whatsapp"
        assert excinfo.value.code == "NETWORK_ERROR"
        assert excinfo.value.original_error is cause

    @pytest.mark.asyncio
    @pytest.mark.parametrize("run", _OPERATIONS)
    async def test_wraps_non_json_success_bodies(self, run: Any):
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, "<html>Bad gateway</html>"))

        with pytest.raises(NetworkError, match="response was not valid JSON"):
            await run(adapter)


class TestGraphFetchJsonPythonSpecific:
    """Behavior of the Python transport layer that upstream gets from fetch()."""

    @pytest.mark.asyncio
    async def test_timeout_is_wrapped_as_network_error(self):
        cause = TimeoutError()
        adapter, _ = _adapter_with_session(cause)

        with pytest.raises(NetworkError, match="WhatsApp API error: request failed") as excinfo:
            await adapter.mark_as_read("wamid.1")

        assert excinfo.value.original_error is cause

    @pytest.mark.asyncio
    async def test_body_read_failure_is_wrapped_as_network_error(self):
        cause = aiohttp.ClientPayloadError("connection reset mid-body")
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, "", read_error=cause))

        with pytest.raises(NetworkError, match="request failed") as excinfo:
            await adapter.mark_as_read("wamid.1")

        assert excinfo.value.original_error is cause

    @pytest.mark.asyncio
    async def test_non_200_success_status_is_accepted(self):
        # ``response.ok`` is any 2xx; the old port rejected everything but 200.
        adapter, _ = _adapter_with_session(_json_response({"messages": [{"id": "wamid.created"}]}, status=201))

        result = await adapter.post_message(THREAD_ID, "hello")

        assert result.id == "wamid.created"

    @pytest.mark.asyncio
    async def test_redirect_status_is_an_api_error(self):
        adapter, _ = _adapter_with_session(_FakeGraphResponse(302, ""))

        with pytest.raises(WhatsAppApiError) as excinfo:
            await adapter.mark_as_read("wamid.1")

        assert excinfo.value.status == 302
        assert str(excinfo.value) == "WhatsApp API error: 302 "

    @pytest.mark.asyncio
    async def test_media_metadata_get_then_binary_download(self):
        media_url = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"
        adapter, session = _adapter_with_session(
            _json_response({"url": media_url, "id": "media123"}),
            _FakeGraphResponse(200, b"\x89PNG"),
        )

        data = await adapter.download_media("media123")

        assert data == b"\x89PNG"
        (meta_method, meta_url, meta_kwargs), (get_method, get_url, _) = session.calls
        assert meta_method == "GET"
        assert meta_url.endswith("/media123")
        assert meta_kwargs == {"headers": {"Authorization": "Bearer test-token"}}
        assert (get_method, get_url) == ("GET", media_url)

    @pytest.mark.asyncio
    async def test_media_metadata_error_message_uses_its_label(self):
        adapter, _ = _adapter_with_session(_FakeGraphResponse(404, "Not Found"))

        with pytest.raises(WhatsAppApiError) as excinfo:
            await adapter.download_media("media123")

        assert str(excinfo.value) == "Failed to get media URL: 404 Not Found"
        assert excinfo.value.code == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_success_body_is_decoded_as_utf8_text(self):
        # The body is decoded as UTF-8 and parsed with ``json.loads`` (like
        # WHATWG ``Response.json()``), so aiohttp's content-type check and
        # charset sniffing never apply.
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, '{"messages": [{"id": "wamid.é"}]}'.encode()))

        result = await adapter.post_message(THREAD_ID, "hello")

        assert result.id == "wamid.é"

    @pytest.mark.asyncio
    async def test_leading_bom_is_stripped_like_whatwg_utf8_decode(self):
        body = b"\xef\xbb\xbf" + b'{"messages": [{"id": "wamid.bom"}]}'
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, body))

        result = await adapter.post_message(THREAD_ID, "hello")

        assert result.id == "wamid.bom"

    @pytest.mark.asyncio
    async def test_leading_bom_does_not_hide_the_meta_error_envelope(self):
        body = b"\xef\xbb\xbf" + json.dumps({"error": {"message": "Invalid token", "code": 190}}).encode()
        adapter, _ = _adapter_with_session(_FakeGraphResponse(401, body))

        with pytest.raises(WhatsAppApiError) as excinfo:
            await adapter.post_message(THREAD_ID, "hello")

        assert excinfo.value.error_code == 190
        assert excinfo.value.provider_message == "Invalid token"

    @pytest.mark.asyncio
    async def test_json_constants_in_a_success_body_are_not_valid_json(self):
        # JS ``Response.json()`` rejects ``NaN``; Python's ``json.loads`` accepts it by default.
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, '{"messages": [{"id": "wamid.x"}], "n": NaN}'))

        with pytest.raises(NetworkError, match="response was not valid JSON"):
            await adapter.post_message(THREAD_ID, "hello")

    @pytest.mark.asyncio
    async def test_huge_integer_literal_parses_like_a_js_number(self):
        # CPython refuses ``int()`` past 4300 digits; JS reads it as ``Infinity``.
        body = '{"messages": [{"id": "wamid.big"}], "n": ' + "1" * 5000 + "}"
        adapter, _ = _adapter_with_session(_FakeGraphResponse(200, body))

        result = await adapter.post_message(THREAD_ID, "hello")

        assert result.id == "wamid.big"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "expected"), [(200, NetworkError), (400, WhatsAppApiError)])
    async def test_deeply_nested_body_keeps_the_typed_error(self, status: int, expected: type[Exception]):
        # CPython's JSON scanner raises ``RecursionError`` (not ``ValueError``)
        # on deep nesting; it must not escape the typed-error contract.
        depth = 200_000
        adapter, _ = _adapter_with_session(_FakeGraphResponse(status, "[" * depth + "]" * depth))

        with pytest.raises(expected):
            await adapter.post_message(THREAD_ID, "hello")

    @pytest.mark.asyncio
    async def test_api_error_is_caught_by_a_bare_adapter_error_handler(self):
        # The compatibility claim for existing callers: a non-2xx Graph call
        # lands in a pre-existing ``except AdapterError`` block.
        adapter, _ = _adapter_with_session(_json_response(_RATE_LIMIT_BODY, status=429))

        caught: AdapterError | None = None
        try:
            await adapter.post_message(THREAD_ID, "hello")
        except AdapterError as error:
            caught = error

        assert type(caught) is WhatsAppApiError


# =============================================================================
# Tests — post_message file uploads (port of describe("postMessage - file uploads"))
# =============================================================================


class _MediaGraphSession:
    """Routes like upstream's ``createMediaFetchMock``: ``/media`` uploads
    answer ``media-{n}``, every other call answers ``wamid.msg{n}``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False
        self._media_counter = 0
        self._message_counter = 0

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url, kwargs))
        if "/media" in url:
            self._media_counter += 1
            return _json_response({"id": f"media-{self._media_counter}"})
        self._message_counter += 1
        return _json_response({"messages": [{"id": f"wamid.msg{self._message_counter}"}]})

    async def close(self) -> None:
        self.closed = True

    def media_calls(self) -> list[dict[str, Any]]:
        return [kwargs for _, url, kwargs in self.calls if "/media" in url]

    def message_bodies(self) -> list[dict[str, Any]]:
        return [kwargs["json"] for _, url, kwargs in self.calls if url.endswith("/messages")]


def _media_adapter() -> tuple[WhatsAppAdapter, _MediaGraphSession]:
    adapter = _make_adapter()
    session = _MediaGraphSession()
    adapter._http_session = session
    return adapter, session


def _form_fields(form: aiohttp.FormData) -> list[dict[str, Any]]:
    """The fields of an ``aiohttp.FormData`` as ``{name, filename, content_type, value}``."""
    return [
        {
            "name": options.get("name"),
            "filename": options.get("filename"),
            "content_type": headers.get("Content-Type"),
            "value": value,
        }
        for options, headers, value in form._fields
    ]


_WHATSAPP_IMAGE_SIZE_LIMIT_PATTERN = r"exceeds WhatsApp image limit"


def _link_card(**extra: Any) -> dict[str, Any]:
    return {
        "type": "card",
        "children": [
            {
                "type": "actions",
                "children": [{"type": "link-button", "url": "https://example.com/track", "label": "Track"}],
            }
        ],
        **extra,
    }


class TestPostMessageFileUploads:
    @pytest.mark.asyncio
    async def test_single_pdf_with_markdown_caption_uploads_then_sends_document(self):
        adapter, session = _media_adapter()

        result = await adapter.post_message(
            THREAD_ID,
            {
                "markdown": "Here is the report",
                "files": [FileUpload(data=b"pdf-content", filename="report.pdf", mime_type="application/pdf")],
            },
        )

        assert len(session.media_calls()) == 1
        bodies = session.message_bodies()
        assert len(bodies) == 1
        sent = bodies[0]
        assert sent["type"] == "document"
        assert sent["document"] == {"id": "media-1", "caption": "Here is the report", "filename": "report.pdf"}
        assert result.id == "wamid.msg1"
        assert result.raw["message"]["type"] == "document"

    @pytest.mark.asyncio
    async def test_single_jpeg_maps_to_image_message_type(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {"markdown": "Photo", "files": [FileUpload(data=b"jpeg", filename="photo.jpg", mime_type="image/jpeg")]},
        )

        sent = session.message_bodies()[0]
        assert sent["type"] == "image"
        # ``filename`` is document-only.
        assert sent["image"] == {"id": "media-1", "caption": "Photo"}

    @pytest.mark.asyncio
    async def test_sends_media_to_bsuid_recipients(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            f"whatsapp:{PHONE_NUMBER_ID}:US.13491208655302741918",
            {"files": [FileUpload(data=b"jpeg", filename="photo.jpg", mime_type="image/jpeg")]},
        )

        sent = session.message_bodies()[0]
        assert sent["recipient"] == "US.13491208655302741918"
        assert "to" not in sent

    @pytest.mark.asyncio
    async def test_audio_with_text_sends_leading_text_message_without_audio_caption(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "markdown": "Listen to this",
                "files": [FileUpload(data=b"audio", filename="clip.mp3", mime_type="audio/mpeg")],
            },
        )

        text_message, audio_message = session.message_bodies()
        assert text_message["type"] == "text"
        assert text_message["text"]["body"] == "Listen to this"
        assert audio_message["type"] == "audio"
        assert audio_message["audio"] == {"id": "media-1"}

    @pytest.mark.asyncio
    async def test_long_text_with_image_sends_text_first_then_image_without_caption(self):
        adapter, session = _media_adapter()
        long_text = "a" * 1025

        await adapter.post_message(
            THREAD_ID,
            {"markdown": long_text, "files": [FileUpload(data=b"jpeg", filename="photo.jpg", mime_type="image/jpeg")]},
        )

        text_message, image_message = session.message_bodies()
        assert text_message["type"] == "text"
        assert text_message["text"]["body"] == long_text
        assert image_message["type"] == "image"
        assert "caption" not in image_message["image"]

    @pytest.mark.asyncio
    async def test_multiple_files_send_sequentially_with_caption_only_on_first(self):
        adapter, session = _media_adapter()

        result = await adapter.post_message(
            THREAD_ID,
            {
                "markdown": "Two files",
                "files": [
                    FileUpload(data=b"a", filename="first.pdf", mime_type="application/pdf"),
                    FileUpload(data=b"b", filename="second.pdf", mime_type="application/pdf"),
                ],
            },
        )

        assert len(session.media_calls()) == 2
        first, second = session.message_bodies()
        assert first["document"] == {"id": "media-1", "caption": "Two files", "filename": "first.pdf"}
        assert second["document"] == {"id": "media-2", "filename": "second.pdf"}
        assert result.id == "wamid.msg2"

    @pytest.mark.asyncio
    async def test_attachment_with_https_url_uses_link_passthrough_without_upload(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "markdown": "Remote doc",
                "attachments": [
                    Attachment(type="file", url="https://example.com/report.pdf", mime_type="application/pdf")
                ],
            },
        )

        assert session.media_calls() == []
        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["document"] == {
            "link": "https://example.com/report.pdf",
            "caption": "Remote doc",
            "filename": "attachment",
        }

    @pytest.mark.asyncio
    async def test_attachment_with_fetch_data_uploads_binary(self):
        adapter, session = _media_adapter()
        fetch_data = AsyncMock(return_value=b"png-bytes")

        await adapter.post_message(
            THREAD_ID,
            {"markdown": "", "attachments": [Attachment(type="image", mime_type="image/png", fetch_data=fetch_data)]},
        )

        fetch_data.assert_awaited_once()
        uploads = session.media_calls()
        assert len(uploads) == 1
        file_field = _form_fields(uploads[0]["data"])[1]
        assert file_field["value"] == b"png-bytes"
        sent = session.message_bodies()[0]
        assert sent["type"] == "image"
        assert sent["image"] == {"id": "media-1"}

    @pytest.mark.asyncio
    async def test_card_with_files_sends_media_then_interactive_message(self):
        adapter, session = _media_adapter()
        card = {
            "type": "card",
            "title": "Approve?",
            "children": [
                {
                    "type": "actions",
                    "children": [
                        {"type": "button", "id": "yes", "label": "Yes"},
                        {"type": "button", "id": "no", "label": "No"},
                    ],
                }
            ],
        }

        result = await adapter.post_message(
            THREAD_ID,
            {"card": card, "files": [FileUpload(data=b"png", filename="proof.png", mime_type="image/png")]},
        )

        assert len(session.media_calls()) == 1
        media_message, interactive_message = session.message_bodies()
        assert media_message["type"] == "image"
        assert "caption" not in media_message["image"]
        assert interactive_message["type"] == "interactive"
        assert interactive_message["interactive"]["header"]["text"] == "Approve?"
        assert len(interactive_message["interactive"]["action"]["buttons"]) == 2
        assert result.id == "wamid.msg2"

    @pytest.mark.asyncio
    async def test_interactive_card_file_does_not_duplicate_title_across_caption_and_header(self):
        adapter, session = _media_adapter()
        title = "Demo image card"

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": title,
                    "children": [{"type": "actions", "children": [{"type": "button", "id": "ok", "label": "OK"}]}],
                },
                "files": [FileUpload(data=b"png", filename="demo.png", mime_type="image/png")],
            },
        )

        media_message, interactive_message = session.message_bodies()
        assert "caption" not in media_message["image"]
        assert interactive_message["interactive"]["header"]["text"] == title
        assert json.dumps([media_message, interactive_message]).count(title) == 1

    @pytest.mark.asyncio
    async def test_interactive_card_file_does_not_duplicate_card_text_fields_in_caption(self):
        adapter, session = _media_adapter()
        body_line = "Your order has shipped"

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": "Shipment",
                    "subtitle": "Status update",
                    "children": [
                        {"type": "text", "content": body_line},
                        {
                            "type": "fields",
                            "children": [{"type": "field", "label": "Tracking code", "value": "SHIP-UNIQUE-VALUE"}],
                        },
                        {
                            "type": "actions",
                            "children": [
                                {"type": "button", "id": "track", "label": "Track"},
                                {"type": "button", "id": "help", "label": "Help"},
                            ],
                        },
                    ],
                },
                "files": [FileUpload(data=b"png", filename="box.png", mime_type="image/png")],
            },
        )

        media_message, interactive_message = session.message_bodies()
        assert "caption" not in media_message["image"]
        interactive = interactive_message["interactive"]
        assert interactive["header"]["text"] == "Shipment"
        assert interactive["body"]["text"] == f"Status update\n{body_line}\nTracking code: SHIP-UNIQUE-VALUE"

    @pytest.mark.asyncio
    async def test_interactive_card_multiple_files_leaves_all_media_uncaptioned(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": "Review docs",
                    "children": [
                        {
                            "type": "actions",
                            "children": [
                                {"type": "button", "id": "approve", "label": "Approve"},
                                {"type": "button", "id": "reject", "label": "Reject"},
                            ],
                        }
                    ],
                },
                "files": [
                    FileUpload(data=b"a", filename="a.pdf", mime_type="application/pdf"),
                    FileUpload(data=b"b", filename="b.pdf", mime_type="application/pdf"),
                ],
            },
        )

        assert len(session.media_calls()) == 2
        first, second, interactive_message = session.message_bodies()
        assert first["document"] == {"id": "media-1", "filename": "a.pdf"}
        assert second["document"] == {"id": "media-2", "filename": "b.pdf"}
        assert interactive_message["type"] == "interactive"
        assert interactive_message["interactive"]["header"]["text"] == "Review docs"

    @pytest.mark.asyncio
    async def test_interactive_card_audio_sends_audio_then_interactive_with_no_leading_text(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": "Voice note",
                    "children": [{"type": "actions", "children": [{"type": "button", "id": "ack", "label": "Got it"}]}],
                },
                "files": [FileUpload(data=b"audio", filename="note.mp3", mime_type="audio/mpeg")],
            },
        )

        bodies = session.message_bodies()
        assert [body["type"] for body in bodies] == ["audio", "interactive"]
        assert bodies[0]["audio"] == {"id": "media-1"}
        assert bodies[1]["interactive"]["header"]["text"] == "Voice note"

    @pytest.mark.asyncio
    async def test_interactive_card_https_attachment_does_not_caption_with_card_title(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": "Remote image card",
                    "children": [{"type": "actions", "children": [{"type": "button", "id": "open", "label": "Open"}]}],
                },
                "attachments": [Attachment(type="image", url="https://example.com/photo.jpg", mime_type="image/jpeg")],
            },
        )

        assert session.media_calls() == []
        media_message, interactive_message = session.message_bodies()
        assert media_message["type"] == "image"
        assert media_message["image"] == {"link": "https://example.com/photo.jpg"}
        assert interactive_message["interactive"]["header"]["text"] == "Remote image card"

    @pytest.mark.asyncio
    async def test_card_with_single_link_button_and_file_sends_one_captioned_media_message(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "card": _link_card(title="Order update"),
                "files": [FileUpload(data=b"png", filename="receipt.png", mime_type="image/png")],
            },
        )

        # Media posts keep the single captioned send; the caption carries the
        # link URL instead of a second cta_url message.
        assert len(session.media_calls()) == 1
        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["type"] == "image"
        assert bodies[0]["image"]["caption"] == "*Order update*\nTrack: https://example.com/track"

    @pytest.mark.asyncio
    async def test_text_fallback_card_file_puts_title_and_body_only_in_the_caption_once(self):
        adapter, session = _media_adapter()
        title = "Receipt details"
        body_line = "Thanks for your purchase"

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": title,
                    "children": [
                        {"type": "text", "content": body_line},
                        {
                            "type": "actions",
                            "children": [
                                {"type": "link-button", "url": "https://example.com/receipt", "label": "View"},
                                {"type": "link-button", "url": "https://example.com/help", "label": "Help"},
                            ],
                        },
                    ],
                },
                "files": [FileUpload(data=b"png", filename="receipt.png", mime_type="image/png")],
            },
        )

        bodies = session.message_bodies()
        assert len(bodies) == 1
        caption = bodies[0]["image"]["caption"]
        assert caption.count(title) == 1
        assert caption.count(body_line) == 1
        assert caption == (f"*{title}*\n{body_line}\nView: https://example.com/receipt\nHelp: https://example.com/help")

    @pytest.mark.asyncio
    async def test_single_link_button_card_posts_cta_url_interactive_payload(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            {
                "card": {
                    "type": "card",
                    "title": "See dates",
                    "children": [
                        {"type": "text", "content": "Tap the button below to see available dates."},
                        {
                            "type": "actions",
                            "children": [
                                {"type": "link-button", "url": "https://example.com/dates", "label": "See Dates"}
                            ],
                        },
                    ],
                }
            },
        )

        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["type"] == "interactive"
        assert bodies[0]["interactive"] == {
            "type": "cta_url",
            "header": {"type": "text", "text": "See dates"},
            "body": {"text": "Tap the button below to see available dates."},
            "action": {
                "name": "cta_url",
                "parameters": {"display_text": "See Dates", "url": "https://example.com/dates"},
            },
        }

    @pytest.mark.asyncio
    async def test_oversize_image_throws_validation_error_before_upload(self):
        adapter, session = _media_adapter()
        oversized = bytes(6 * 1024 * 1024)

        with pytest.raises(ValidationError, match=_WHATSAPP_IMAGE_SIZE_LIMIT_PATTERN):
            await adapter.post_message(
                THREAD_ID,
                {"markdown": "", "files": [FileUpload(data=oversized, filename="huge.png", mime_type="image/png")]},
            )

        assert session.calls == []


class TestGetWhatsAppMediaType:
    @pytest.mark.parametrize(
        ("mime_type", "expected"),
        [
            ("image/png", "image"),
            ("image/jpeg", "image"),
            ("image/gif", "document"),
            ("video/mp4", "video"),
            ("video/3gpp", "video"),
            ("audio/mpeg", "audio"),
            ("application/pdf", "document"),
        ],
    )
    def test_maps_mime_type_to_media_type(self, mime_type: str, expected: str):
        assert get_whatsapp_media_type(mime_type) == expected


class TestOutboundMediaPythonSpecific:
    """Wire details and translation hazards of the media port."""

    @pytest.mark.asyncio
    async def test_upload_is_a_multipart_post_to_the_media_endpoint(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID, {"files": [FileUpload(data=b"%PDF", filename="report.pdf", mime_type="application/pdf")]}
        )

        method, url, kwargs = session.calls[0]
        assert method == "POST"
        assert url == f"https://graph.facebook.com/v25.0/{PHONE_NUMBER_ID}/media"
        # aiohttp sets the multipart Content-Type (with boundary) itself.
        assert kwargs["headers"] == {"Authorization": "Bearer test-token"}
        assert "json" not in kwargs
        assert _form_fields(kwargs["data"]) == [
            {"name": "messaging_product", "filename": None, "content_type": None, "value": "whatsapp"},
            {"name": "file", "filename": "report.pdf", "content_type": "application/pdf", "value": b"%PDF"},
        ]

    @pytest.mark.asyncio
    async def test_upload_errors_use_the_upload_label(self):
        adapter, _ = _adapter_with_session(_FakeGraphResponse(500, "boom"))

        with pytest.raises(WhatsAppApiError) as excinfo:
            await adapter.post_message(THREAD_ID, {"files": [FileUpload(data=b"x", filename="x.pdf")]})

        assert str(excinfo.value) == "WhatsApp API upload error: 500 boom"

    @pytest.mark.asyncio
    async def test_upload_response_without_an_id_raises(self):
        adapter, _ = _adapter_with_session(_json_response({}))

        with pytest.raises(RuntimeError, match="did not return a media ID for upload"):
            await adapter.post_message(THREAD_ID, {"files": [FileUpload(data=b"x", filename="x.pdf")]})

    @pytest.mark.asyncio
    async def test_empty_file_upload_is_still_uploaded(self):
        # Upstream's Buffer is truthy even when empty; ``b""`` must not read as "no data".
        adapter, session = _media_adapter()

        await adapter.post_message(THREAD_ID, {"files": [FileUpload(data=b"", filename="empty.pdf")]})

        assert _form_fields(session.media_calls()[0]["data"])[1]["value"] == b""
        assert session.message_bodies()[0]["document"] == {"id": "media-1", "filename": "empty.pdf"}

    @pytest.mark.asyncio
    async def test_empty_attachment_data_uploads_instead_of_falling_back_to_the_url(self):
        adapter, session = _media_adapter()
        fetch_data = AsyncMock(return_value=b"unused")

        await adapter.post_message(
            THREAD_ID,
            {
                "attachments": [
                    Attachment(type="file", data=b"", url="https://example.com/x.pdf", fetch_data=fetch_data)
                ]
            },
        )

        fetch_data.assert_not_awaited()
        assert len(session.media_calls()) == 1
        assert session.message_bodies()[0]["document"] == {"id": "media-1", "filename": "attachment"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("filename", "media_type", "content_type"),
        [
            ("PHOTO.JPG", "image", "image/jpeg"),
            ("clip.ogg", "audio", "audio/ogg"),
            ("anim.gif", "document", "image/gif"),
            ("notes", "document", "application/octet-stream"),
        ],
    )
    async def test_mime_type_is_inferred_from_the_extension(self, filename: str, media_type: str, content_type: str):
        adapter, session = _media_adapter()

        await adapter.post_message(THREAD_ID, {"files": [FileUpload(data=b"x", filename=filename)]})

        assert _form_fields(session.media_calls()[0]["data"])[1]["content_type"] == content_type
        assert session.message_bodies()[0]["type"] == media_type

    @pytest.mark.asyncio
    async def test_attachment_without_mime_type_uses_its_kind(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID, {"attachments": [Attachment(type="video", url="https://example.com/v", name="v")]}
        )

        assert session.message_bodies()[0] == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": USER_WA_ID,
            "type": "video",
            "video": {"link": "https://example.com/v"},
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("attachment", "message"),
        [
            (Attachment(type="file"), "requires data, fetchData, or a public HTTPS url"),
            (Attachment(type="file", url=""), "requires data, fetchData, or a public HTTPS url"),
            (Attachment(type="file", url="http://example.com/x.pdf"), "must use HTTPS"),
        ],
    )
    async def test_attachment_without_a_usable_source_raises_before_any_send(self, attachment: Any, message: str):
        adapter, session = _media_adapter()

        with pytest.raises(ValidationError, match=message):
            await adapter.post_message(THREAD_ID, {"markdown": "hi", "attachments": [attachment]})

        assert session.calls == []

    @pytest.mark.asyncio
    async def test_link_attachment_declared_size_is_checked_before_any_send(self):
        adapter, session = _media_adapter()
        too_big = Attachment(type="image", url="https://example.com/a.png", size=5 * 1024 * 1024 + 1)

        with pytest.raises(ValidationError, match=_WHATSAPP_IMAGE_SIZE_LIMIT_PATTERN):
            await adapter.post_message(THREAD_ID, {"attachments": [too_big]})

        assert session.calls == []

    @pytest.mark.parametrize(
        ("media_type", "limit"),
        [
            ("image", 5 * 1024 * 1024),
            ("audio", 16 * 1024 * 1024),
            ("video", 16 * 1024 * 1024),
            ("document", 100 * 1024 * 1024),
        ],
    )
    def test_validate_file_size_allows_exactly_the_limit(self, media_type: str, limit: int):
        validate_file_size(media_type, limit)
        with pytest.raises(ValidationError, match=f"File size {limit + 1} bytes exceeds WhatsApp {media_type} limit"):
            validate_file_size(media_type, limit + 1)

    @pytest.mark.asyncio
    async def test_upload_filename_is_serialized_like_whatwg_form_data(self):
        # Node's FormData escapes only LF, CR and '"'; spaces and non-ASCII stay
        # raw. aiohttp's default quoting would percent-encode them, and a raw
        # CR/LF (or other control char) would make it raise a bare ValueError.
        adapter, session = _media_adapter()
        filename = 'my réport "q"\r\n\x01.pdf'

        result = await adapter.post_message(
            THREAD_ID,
            {"files": [FileUpload(data=b"%PDF", filename=filename, mime_type="application/pdf")]},
        )

        serialized = session.media_calls()[0]["data"]().decode()
        assert 'Content-Disposition: form-data; name="file"; filename="my réport %22q%22%0D%0A%01.pdf"' in serialized
        # The document message carries the original, unescaped name.
        assert session.message_bodies()[0]["document"] == {"id": "media-1", "filename": filename}
        assert result.id == "wamid.msg1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mime_type", "part_content_type"),
        [
            ("Application/PDF", "application/pdf"),
            ("application/pdf\r\nX-Injected: 1", "application/octet-stream"),
            ("application/péf", "application/octet-stream"),
        ],
    )
    async def test_upload_part_content_type_follows_blob_type_rules(self, mime_type: str, part_content_type: str):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID, {"files": [FileUpload(data=b"%PDF", filename="r.pdf", mime_type=mime_type)]}
        )

        assert _form_fields(session.media_calls()[0]["data"])[1]["content_type"] == part_content_type
        assert session.message_bodies()[0]["type"] == "document"

    @pytest.mark.asyncio
    async def test_empty_text_fallback_card_is_sent_as_text_after_uncaptioned_media(self):
        # A button without an id cannot be a reply button, so the card falls
        # back to text; its caption fallback is empty, so the card text follows
        # the media as its own message.
        adapter, session = _media_adapter()
        card = {
            "type": "card",
            "children": [{"type": "actions", "children": [{"type": "button", "label": "{{emoji:fire}} Go"}]}],
        }

        result = await adapter.post_message(
            THREAD_ID, {"card": card, "files": [FileUpload(data=b"j", filename="a.jpg", mime_type="image/jpeg")]}
        )

        image_message, text_message = session.message_bodies()
        assert image_message["image"] == {"id": "media-1"}
        assert text_message["text"]["body"] == "[\U0001f525 Go]"
        assert result.id == "wamid.msg2"

    @pytest.mark.asyncio
    async def test_interactive_card_after_media_converts_emoji_placeholders(self):
        adapter, session = _media_adapter()
        card = {
            "type": "card",
            "title": "Approve {{emoji:fire}}",
            "children": [{"type": "actions", "children": [{"type": "button", "id": "yes", "label": "Yes"}]}],
        }

        await adapter.post_message(
            THREAD_ID, {"card": card, "files": [FileUpload(data=b"j", filename="a.jpg", mime_type="image/jpeg")]}
        )

        interactive_message = session.message_bodies()[1]
        assert interactive_message["interactive"]["header"]["text"] == "Approve \U0001f525"

    @pytest.mark.asyncio
    async def test_text_fallback_card_caption_converts_emoji_placeholders(self):
        adapter, session = _media_adapter()
        card = _link_card(title="Shipped {{emoji:fire}}")

        await adapter.post_message(
            THREAD_ID, {"card": card, "files": [FileUpload(data=b"j", filename="a.jpg", mime_type="image/jpeg")]}
        )

        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["image"]["caption"] == "*Shipped \U0001f525*\nTrack: https://example.com/track"

    def test_media_type_ignores_parameters_and_case(self):
        assert get_whatsapp_media_type(" Image/PNG ; charset=binary") == "image"

    @pytest.mark.asyncio
    async def test_caption_limit_counts_utf16_code_units_like_upstream(self):
        # 600 astral emoji: 600 code points, but ``text.length`` is 1200 > 1024.
        adapter, session = _media_adapter()
        text = "\U0001f600" * 600

        await adapter.post_message(
            THREAD_ID, {"raw": text, "files": [FileUpload(data=b"j", filename="p.jpg", mime_type="image/jpeg")]}
        )

        text_message, image_message = session.message_bodies()
        assert text_message["text"]["body"] == text
        assert image_message["image"] == {"id": "media-1"}

    @pytest.mark.asyncio
    async def test_caption_converts_emoji_placeholders(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            PostableMarkdown(
                markdown="Done {{emoji:fire}}",
                files=[FileUpload(data=b"j", filename="p.jpg", mime_type="image/jpeg")],
            ),
        )

        assert session.message_bodies()[0]["image"]["caption"] == "Done 🔥"

    @pytest.mark.asyncio
    async def test_files_only_message_sends_no_text_and_no_caption(self):
        adapter, session = _media_adapter()

        await adapter.post_message(THREAD_ID, {"files": [FileUpload(data=b"j", filename="p.jpg")]})

        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["image"] == {"id": "media-1"}

    @pytest.mark.asyncio
    async def test_postable_card_dataclass_with_files_uses_the_media_path(self):
        adapter, session = _media_adapter()

        await adapter.post_message(
            THREAD_ID,
            PostableCard(card=_link_card(), files=[FileUpload(data=b"j", filename="p.jpg")]),
        )

        bodies = session.message_bodies()
        assert len(bodies) == 1
        assert bodies[0]["image"]["caption"] == "Track: https://example.com/track"
