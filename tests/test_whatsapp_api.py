"""Tests for WhatsApp adapter API-calling methods.

Covers: post_message (text, long split, interactive card), add_reaction,
remove_reaction, stream (accumulation), attachment fetch_data presence,
send_template, and Graph API error mapping.

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

from chat_sdk.adapters.whatsapp import WhatsAppApiError
from chat_sdk.adapters.whatsapp.adapter import (
    WHATSAPP_MESSAGE_LIMIT,
    WhatsAppAdapter,
)
from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig
from chat_sdk.logger import ConsoleLogger
from chat_sdk.shared.errors import AdapterError, NetworkError, ValidationError
from chat_sdk.types import MarkdownTextChunk, StreamChunk

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


async def _fetch_media_metadata(adapter: WhatsAppAdapter) -> Any:
    return await adapter.download_media("media123")


# The upstream "media uploads" row lands with outbound media uploads (#238).
_OPERATIONS = [
    pytest.param(_send_message, id="message sends"),
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
