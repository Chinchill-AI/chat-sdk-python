"""Faithful translation of callback-url.test.ts (17 tests).

Each ``it("...")`` block from the TypeScript test suite is translated
to a corresponding ``async def test_...`` method, preserving the same
inputs, assertions, and test structure.

TS stubs the global ``fetch``; Python patches the ``_fetch`` seam in
``chat_sdk.callback_url`` (the lazy aiohttp wrapper).

TS file: packages/chat/src/callback-url.test.ts
"""

from __future__ import annotations

import copy
import json
import re
from unittest.mock import AsyncMock, call, patch

from chat_sdk.callback_url import (
    decode_callback_value,
    encode_callback_value,
    post_to_callback_url,
    process_card_callback_urls,
    resolve_callback_url,
)
from chat_sdk.cards import Actions, Button, Card, CardText, Section
from chat_sdk.testing import MockStateAdapter, create_mock_state

CALLBACK_TOKEN_PATTERN = re.compile(r"^__cb:[a-f0-9]{16}$")
CALLBACK_PREFIX_PATTERN = re.compile(r"^__cb:")


# ===========================================================================
# encodeCallbackValue / decodeCallbackValue
# ===========================================================================


class TestEncodeDecodeCallbackValue:
    """describe("encodeCallbackValue / decodeCallbackValue")"""

    # it("encodes token")
    def test_encodes_token(self):
        encoded = encode_callback_value("abc123")
        assert encoded == "__cb:abc123"

    # it("decodes token from encoded value")
    def test_decodes_token_from_encoded_value(self):
        decoded = decode_callback_value("__cb:abc123")
        assert decoded.callback_token == "abc123"

    # it("returns no token for regular values")
    def test_returns_no_token_for_regular_values(self):
        decoded = decode_callback_value("regular-value")
        assert decoded.callback_token is None

    # it("returns no token for undefined value")
    def test_returns_no_token_for_undefined_value(self):
        decoded = decode_callback_value(None)
        assert decoded.callback_token is None

    # it("round-trips encode/decode")
    def test_roundtrips_encodedecode(self):
        encoded = encode_callback_value("tok123")
        decoded = decode_callback_value(encoded)
        assert decoded.callback_token == "tok123"


# ===========================================================================
# processCardCallbackUrls
# ===========================================================================


class TestProcessCardCallbackUrls:
    """describe("processCardCallbackUrls")"""

    def _state(self) -> MockStateAdapter:
        return create_mock_state()

    # it("returns card unchanged when no buttons have callbackUrl")
    async def test_returns_card_unchanged_when_no_buttons_have_callbackurl(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                CardText("Hello"),
                Actions([Button(id="btn", label="Click")]),
            ],
        )

        result = await process_card_callback_urls(card, state)
        assert result is card

    # it("encodes callbackUrl into button value and stores in state")
    async def test_encodes_callbackurl_into_button_value_and_stores_in_state(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                Actions(
                    [
                        Button(
                            id="approve",
                            label="Approve",
                            callback_url="https://example.com/webhook/123",
                        )
                    ]
                ),
            ],
        )

        result = await process_card_callback_urls(card, state)

        actions = next(c for c in result["children"] if c["type"] == "actions")
        button = actions["children"][0]
        assert button["type"] == "button"
        assert CALLBACK_TOKEN_PATTERN.match(button["value"])
        assert "callback_url" not in button

        decoded = decode_callback_value(button["value"])
        assert decoded.callback_token is not None

        resolved = await resolve_callback_url(decoded.callback_token, state)
        assert resolved is not None
        assert resolved.url == "https://example.com/webhook/123"

    # it("stores original value in state alongside callback URL")
    async def test_stores_original_value_in_state_alongside_callback_url(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                Actions(
                    [
                        Button(
                            id="btn",
                            label="Go",
                            value="item-99",
                            callback_url="https://hook.example.com",
                        )
                    ]
                ),
            ],
        )

        result = await process_card_callback_urls(card, state)
        button = next(c for c in result["children"] if c["type"] == "actions")["children"][0]

        assert CALLBACK_TOKEN_PATTERN.match(button["value"])

        decoded = decode_callback_value(button["value"])
        resolved = await resolve_callback_url(decoded.callback_token or "", state)
        assert resolved is not None
        assert resolved.url == "https://hook.example.com"
        assert resolved.original_value == "item-99"

    # it("only processes buttons with callbackUrl, leaves others untouched")
    async def test_only_processes_buttons_with_callbackurl_leaves_others_untouched(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                Actions(
                    [
                        Button(id="normal", label="Normal", value="keep"),
                        Button(
                            id="callback",
                            label="Callback",
                            callback_url="https://example.com",
                        ),
                    ]
                ),
            ],
        )

        result = await process_card_callback_urls(card, state)
        actions = next(c for c in result["children"] if c["type"] == "actions")
        normal_btn = actions["children"][0]
        callback_btn = actions["children"][1]

        assert normal_btn["value"] == "keep"
        assert CALLBACK_PREFIX_PATTERN.match(callback_btn["value"])

    # it("processes buttons nested inside sections")
    async def test_processes_buttons_nested_inside_sections(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                Section(
                    [
                        CardText("Nested"),
                        Actions(
                            [
                                Button(
                                    id="nested-btn",
                                    label="Go",
                                    callback_url="https://example.com/nested",
                                )
                            ]
                        ),
                    ]
                ),
            ],
        )

        result = await process_card_callback_urls(card, state)
        section = next(c for c in result["children"] if c["type"] == "section")
        actions = next(c for c in section["children"] if c["type"] == "actions")
        button = actions["children"][0]
        assert button["type"] == "button"

        assert CALLBACK_TOKEN_PATTERN.match(button["value"])
        assert "callback_url" not in button

        decoded = decode_callback_value(button["value"])
        resolved = await resolve_callback_url(decoded.callback_token or "", state)
        assert resolved is not None
        assert resolved.url == "https://example.com/nested"

    # it("does not mutate the original card")
    async def test_does_not_mutate_the_original_card(self):
        state = self._state()
        card = Card(
            title="Test",
            children=[
                Actions(
                    [
                        Button(
                            id="btn",
                            label="Go",
                            callback_url="https://example.com",
                        )
                    ]
                ),
            ],
        )

        original = copy.deepcopy(card)
        await process_card_callback_urls(card, state)
        assert card == original


# ===========================================================================
# resolveCallbackUrl
# ===========================================================================


class TestResolveCallbackUrl:
    """describe("resolveCallbackUrl")"""

    # it("returns null for unknown token")
    async def test_returns_null_for_unknown_token(self):
        state = create_mock_state()
        result = await resolve_callback_url("nonexistent", state)
        assert result is None

    # it("resolves stored callback with URL and original value")
    async def test_resolves_stored_callback_with_url_and_original_value(self):
        state = create_mock_state()
        await state.set(
            "chat:callback:test-token",
            {"url": "https://example.com/hook", "originalValue": "item-42"},
        )
        result = await resolve_callback_url("test-token", state)
        assert result is not None
        assert result.url == "https://example.com/hook"
        assert result.original_value == "item-42"

    # it("handles legacy string format")
    async def test_handles_legacy_string_format(self):
        state = create_mock_state()
        await state.set("chat:callback:legacy-token", "https://example.com/hook")
        result = await resolve_callback_url("legacy-token", state)
        assert result is not None
        assert result.url == "https://example.com/hook"
        assert result.original_value is None


# ===========================================================================
# postToCallbackUrl
# ===========================================================================


class TestPostToCallbackUrl:
    """describe("postToCallbackUrl")"""

    # it("POSTs JSON payload to the URL")
    async def test_posts_json_payload_to_the_url(self):
        with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(200, "ok"))) as fetch_mock:
            result = await post_to_callback_url(
                "https://example.com/hook",
                {"type": "action", "actionId": "approve"},
            )

        assert result.error is None
        assert result.status == 200
        assert fetch_mock.await_args == call(
            "https://example.com/hook",
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps({"type": "action", "actionId": "approve"}),
        )

    # it("returns error for non-2xx responses")
    async def test_returns_error_for_non2xx_responses(self):
        with patch("chat_sdk.callback_url._fetch", new=AsyncMock(return_value=(404, "Not Found"))):
            result = await post_to_callback_url("https://example.com/hook", {})

        assert isinstance(result.error, Exception)
        assert "Callback URL returned 404: Not Found" in str(result.error)
        assert result.status == 404

    # it("catches fetch errors and returns them")
    async def test_catches_fetch_errors_and_returns_them(self):
        with patch(
            "chat_sdk.callback_url._fetch",
            new=AsyncMock(side_effect=Exception("Network error")),
        ):
            result = await post_to_callback_url("https://example.com/hook", {})

        assert isinstance(result.error, Exception)
        assert str(result.error) == "Network error"
        assert result.status is None
