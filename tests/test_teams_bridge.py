"""Tests for ``BridgeHttpAdapter`` — the serverless dispatch bridge between
framework-agnostic webhooks and the Microsoft Teams SDK.

Python port coverage for ``packages/adapter-teams/src/bridge-adapter.ts``
(issue #93 PR 1). The bridge:

- implements the SDK ``HttpServerAdapter`` protocol (``register_route`` capture),
- parses the request body + headers across web frameworks,
- records per-activity ``WebhookOptions`` for the duration of a dispatch,
- invokes the captured SDK route handler and translates its ``{status, body}``
  result back into the ``{body, status, headers}`` dict consumers expect.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from microsoft_teams.apps.http.adapter import HttpServerAdapter

from chat_sdk.adapters.teams.bridge import BridgeHttpAdapter
from chat_sdk.logger import ConsoleLogger
from chat_sdk.testing import MockLogger
from chat_sdk.types import WebhookOptions
from tests._log_capture import MULTIBYTE_TEXT, assert_body_not_logged


def _make_bridge() -> BridgeHttpAdapter:
    return BridgeHttpAdapter(ConsoleLogger("error", prefix="teams"))


class _FakeRequest:
    """Minimal request double exposing ``text()`` + ``headers``."""

    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body


# ---------------------------------------------------------------------------
# Protocol conformance + route capture
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_satisfies_sdk_http_server_adapter_protocol(self):
        bridge = _make_bridge()
        assert isinstance(bridge, HttpServerAdapter)

    def test_register_route_captures_handler(self):
        bridge = _make_bridge()

        async def handler(_request):
            return {"status": 200, "body": None}

        bridge.register_route("POST", "/api/messages", handler)
        assert bridge._handler is handler

    def test_serve_static_is_noop(self):
        bridge = _make_bridge()
        # Should not raise.
        bridge.serve_static("/tabs/x", "/tmp/x")

    async def test_start_and_stop_are_noops(self):
        bridge = _make_bridge()
        await bridge.start(3978)
        await bridge.stop()


# ---------------------------------------------------------------------------
# dispatch — happy paths
# ---------------------------------------------------------------------------


class TestDispatch:
    async def test_dispatch_calls_handler_with_parsed_body_and_headers(self):
        bridge = _make_bridge()
        captured: dict[str, Any] = {}

        async def handler(request):
            captured["request"] = request
            return {"status": 200, "body": None}

        bridge.register_route("POST", "/api/messages", handler)
        req = _FakeRequest('{"type": "message", "id": "m1"}', {"Authorization": "Bearer x"})

        result = await bridge.dispatch(req)

        assert captured["request"]["body"] == {"type": "message", "id": "m1"}
        assert captured["request"]["headers"]["Authorization"] == "Bearer x"
        assert result["status"] == 200
        # No body → empty string + no Content-Type header.
        assert result["body"] == ""
        assert result["headers"] == {}

    async def test_dispatch_serializes_handler_body_as_json(self):
        bridge = _make_bridge()

        async def handler(_request):
            return {"status": 200, "body": {"statusCode": 200, "value": ""}}

        bridge.register_route("POST", "/api/messages", handler)
        result = await bridge.dispatch(_FakeRequest('{"id": "i1", "type": "invoke"}'))

        assert result["status"] == 200
        assert result["body"] == '{"statusCode": 200, "value": ""}'
        assert result["headers"]["Content-Type"] == "application/json"

    async def test_dispatch_passes_through_handler_status(self):
        bridge = _make_bridge()

        async def handler(_request):
            return {"status": 401, "body": {"error": "Unauthorized"}}

        bridge.register_route("POST", "/api/messages", handler)
        result = await bridge.dispatch(_FakeRequest('{"id": "m1"}'))
        assert result["status"] == 401
        assert result["body"] == '{"error": "Unauthorized"}'

    async def test_dispatch_invalid_json_returns_400_without_calling_handler(self):
        bridge = _make_bridge()
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)

        result = await bridge.dispatch(_FakeRequest("not json{{{"))

        assert result["status"] == 400
        assert result["body"] == "Invalid JSON"
        assert result["headers"]["Content-Type"] == "text/plain"
        handler.assert_not_called()

    async def test_dispatch_non_object_json_returns_400(self):
        bridge = _make_bridge()
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)

        result = await bridge.dispatch(_FakeRequest("[1, 2, 3]"))

        assert result["status"] == 400
        handler.assert_not_called()

    async def test_dispatch_without_handler_returns_500(self):
        bridge = _make_bridge()
        result = await bridge.dispatch(_FakeRequest('{"id": "m1"}'))
        assert result["status"] == 500
        assert "No handler registered" in result["body"]

    async def test_empty_body_is_invalid_json(self):
        # Upstream runs ``JSON.parse(body)`` unconditionally, so an empty body
        # is a 400 and never reaches the SDK handler as an empty activity.
        bridge = _make_bridge()
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)

        result = await bridge.dispatch(_FakeRequest(""))
        assert result["status"] == 400
        assert result["body"] == "Invalid JSON"
        handler.assert_not_called()

    async def test_dispatch_handler_exception_returns_500(self):
        bridge = _make_bridge()

        async def handler(_request):
            raise RuntimeError("boom")

        bridge.register_route("POST", "/api/messages", handler)
        result = await bridge.dispatch(_FakeRequest('{"id": "m1"}'))
        assert result["status"] == 500
        assert "Internal error" in result["body"]


# ---------------------------------------------------------------------------
# webhook_verifier (upstream TeamsWebhookVerifier, chat@4.41.0)
# ---------------------------------------------------------------------------


class TestWebhookVerifier:
    @pytest.mark.parametrize("result", [False, None, 0, "", {}])
    async def test_falsy_result_answers_401_without_reaching_the_handler(self, result: Any):
        bridge = BridgeHttpAdapter(ConsoleLogger("error", prefix="teams"), webhook_verifier=lambda _r, _b: result)
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)

        response = await bridge.dispatch(_FakeRequest('{"id": "m1"}'))

        assert response == {"body": "Unauthorized", "status": 401, "headers": {"Content-Type": "text/plain"}}
        handler.assert_not_awaited()

    async def test_truthy_async_result_reaches_the_handler_with_the_parsed_body(self):
        async def verifier(_request: Any, body: str) -> dict[str, bool]:
            return {"verified": body == '{"id": "m1"}'}

        bridge = BridgeHttpAdapter(ConsoleLogger("error", prefix="teams"), webhook_verifier=verifier)
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)

        response = await bridge.dispatch(_FakeRequest('{"id": "m1"}'))

        assert response["status"] == 200
        handler.assert_awaited_once()
        assert handler.await_args.args[0]["body"] == {"id": "m1"}


# ---------------------------------------------------------------------------
# WebhookOptions per-activity map
# ---------------------------------------------------------------------------


class TestWebhookOptions:
    async def test_options_available_during_dispatch_and_cleared_after(self):
        bridge = _make_bridge()
        seen: dict[str, Any] = {}
        options = WebhookOptions(wait_until=lambda _t: None)

        async def handler(request):
            activity_id = request["body"]["id"]
            seen["during"] = bridge.get_webhook_options(activity_id)
            return {"status": 200, "body": None}

        bridge.register_route("POST", "/api/messages", handler)
        await bridge.dispatch(_FakeRequest('{"id": "act-1"}'), options)

        # Visible to the handler while the activity is in flight...
        assert seen["during"] is options
        # ...and removed afterward so it cannot leak to a later activity.
        assert bridge.get_webhook_options("act-1") is None

    async def test_options_cleared_even_when_handler_raises(self):
        bridge = _make_bridge()

        async def handler(_request):
            raise RuntimeError("boom")

        bridge.register_route("POST", "/api/messages", handler)
        await bridge.dispatch(_FakeRequest('{"id": "act-2"}'), WebhookOptions())
        assert bridge.get_webhook_options("act-2") is None

    async def test_no_options_recorded_when_none_passed(self):
        bridge = _make_bridge()
        seen: dict[str, Any] = {}

        async def handler(request):
            seen["during"] = bridge.get_webhook_options(request["body"]["id"])
            return {"status": 200, "body": None}

        bridge.register_route("POST", "/api/messages", handler)
        await bridge.dispatch(_FakeRequest('{"id": "act-3"}'))
        assert seen["during"] is None

    def test_get_webhook_options_none_id_returns_none(self):
        bridge = _make_bridge()
        assert bridge.get_webhook_options(None) is None


# ---------------------------------------------------------------------------
# Body + header extraction across frameworks
# ---------------------------------------------------------------------------


class TestBodyExtraction:
    async def test_async_text_method(self):
        bridge = _make_bridge()
        assert await bridge._read_body(_FakeRequest('{"a": 1}')) == '{"a": 1}'

    async def test_static_text_attribute(self):
        bridge = _make_bridge()

        class Req:
            text = "static-text"

        assert await bridge._read_body(Req()) == "static-text"

    async def test_bytes_body_attribute(self):
        bridge = _make_bridge()

        class Req:
            body = b"raw-bytes"

        assert await bridge._read_body(Req()) == "raw-bytes"

    async def test_async_body_callable(self):
        bridge = _make_bridge()

        class Req:
            body = AsyncMock(return_value=b"async-body")

        assert await bridge._read_body(Req()) == "async-body"

    async def test_body_with_read_method(self):
        bridge = _make_bridge()

        class Stream:
            def read(self):
                return b"stream-bytes"

        class Req:
            body = Stream()

        assert await bridge._read_body(Req()) == "stream-bytes"

    async def test_data_attribute_fallback(self):
        bridge = _make_bridge()

        class Req:
            data = b"data-bytes"

        assert await bridge._read_body(Req()) == "data-bytes"

    async def test_empty_request_returns_empty_string(self):
        bridge = _make_bridge()

        class Req:
            pass

        assert await bridge._read_body(Req()) == ""


class TestHeaderExtraction:
    def test_dict_headers(self):
        bridge = _make_bridge()

        class Req:
            headers = {"Authorization": "Bearer t", "X-Other": "v"}

        out = bridge._read_headers(Req())
        assert out == {"Authorization": "Bearer t", "X-Other": "v"}

    def test_mapping_headers_via_items(self):
        bridge = _make_bridge()

        class Headers:
            def items(self):
                return [("authorization", "Bearer abc")]

        class Req:
            headers = Headers()

        out = bridge._read_headers(Req())
        assert out == {"authorization": "Bearer abc"}

    def test_missing_headers_returns_empty(self):
        bridge = _make_bridge()

        class Req:
            pass

        assert bridge._read_headers(Req()) == {}


# ---------------------------------------------------------------------------
# Logger interaction (request-shape metadata only — upstream f485255b)
# ---------------------------------------------------------------------------


class TestLogging:
    async def test_logs_body_length_not_body_at_debug(self):
        logger = MockLogger()
        bridge = BridgeHttpAdapter(logger)
        handler = AsyncMock(return_value={"status": 200, "body": None})
        bridge.register_route("POST", "/api/messages", handler)
        body = json.dumps(
            {
                "id": "m1",
                "type": "message",
                "text": f"Authorization: Bearer secret-token secret-access-token {MULTIBYTE_TEXT}",
                "from": {"name": "customer-team-slug"},
            },
            ensure_ascii=False,
        )

        await bridge.dispatch(_FakeRequest(body))

        # ``bodyLength`` is the UTF-8 byte length (``Buffer.byteLength``), not
        # the code-point count.
        assert len(body.encode("utf-8")) != len(body)
        assert ("Teams webhook received", {"bodyLength": len(body.encode("utf-8"))}) in logger.debug.calls
        assert_body_not_logged(logger, body)

    async def test_empty_body_logs_zero_length(self):
        logger = MockLogger()
        bridge = BridgeHttpAdapter(logger)
        bridge.register_route("POST", "/api/messages", AsyncMock(return_value={"status": 200, "body": None}))

        await bridge.dispatch(_FakeRequest(""))

        assert ("Teams webhook received", {"bodyLength": 0}) in logger.debug.calls
