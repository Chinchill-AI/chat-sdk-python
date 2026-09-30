"""Faithful translation of callback-url.test.ts (21 tests at chat@4.41.1).

Each ``it("...")`` block from the TypeScript test suite is translated
to a corresponding ``async def test_...`` method, preserving the same
inputs, assertions, and test structure.

TS stubs the global ``fetch``; Python patches the ``_fetch`` seam in
``chat_sdk.callback_url`` (the lazy aiohttp wrapper).

Python-specific coverage for the token lock / consume path lives in
``TestResolveCallbackUrlLocking`` at the bottom of the resolve section.

TS file: packages/chat/src/callback-url.test.ts
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from unittest.mock import AsyncMock, call, patch

import pytest

from chat_sdk.callback_url import (
    CALLBACK_LOCK_TTL_MS,
    CALLBACK_TTL_MS,
    CallbackContext,
    CallbackScope,
    ResolvedCallback,
    decode_callback_value,
    encode_callback_value,
    post_to_callback_url,
    process_card_callback_urls,
    resolve_callback_url,
)
from chat_sdk.cards import Actions, Button, Card, CardText, Section
from chat_sdk.state.memory import MemoryStateAdapter
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


CHANNEL_SCOPE = CallbackScope(id="slack:C1", type="channel")


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

        result = await process_card_callback_urls(card, state, CHANNEL_SCOPE)
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

        result = await process_card_callback_urls(card, state, CallbackScope(id="slack:C1:1.1", type="thread"))

        actions = next(c for c in result["children"] if c["type"] == "actions")
        button = actions["children"][0]
        assert button["type"] == "button"
        assert CALLBACK_TOKEN_PATTERN.match(button["value"])
        assert "callback_url" not in button

        decoded = decode_callback_value(button["value"])
        assert decoded.callback_token is not None

        resolved = await resolve_callback_url(
            decoded.callback_token,
            state,
            CallbackContext(action_id="approve", thread_id="slack:C1:1.1"),
        )
        assert resolved is not None
        assert resolved.url == "https://example.com/webhook/123"
        assert resolved.scope == CallbackScope(id="slack:C1:1.1", type="thread")

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

        result = await process_card_callback_urls(card, state, CHANNEL_SCOPE)
        button = next(c for c in result["children"] if c["type"] == "actions")["children"][0]

        assert CALLBACK_TOKEN_PATTERN.match(button["value"])

        decoded = decode_callback_value(button["value"])
        resolved = await resolve_callback_url(
            decoded.callback_token or "",
            state,
            CallbackContext(action_id="btn", channel_id="slack:C1"),
        )
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

        result = await process_card_callback_urls(card, state, CHANNEL_SCOPE)
        actions = next(c for c in result["children"] if c["type"] == "actions")
        normal_btn = actions["children"][0]
        callback_btn = actions["children"][1]

        assert normal_btn["value"] == "keep"
        assert CALLBACK_PREFIX_PATTERN.match(callback_btn["value"])

    # it("keeps every other button field when replacing the callback URL")
    async def test_keeps_every_other_button_field_when_replacing_the_callback_url(self):
        state = self._state()
        button_in = Button(
            id="approve",
            label="Approve",
            style="primary",
            disabled=True,
            action_type="modal",
            callback_url="https://example.com/hook",
        )
        # `Button(tooltip=...)` arrives with #202; a raw key proves unknown
        # fields survive the token swap.
        button_in["tooltip"] = "Approve the request"  # type: ignore[typeddict-unknown-key]
        card = Card(title="Test", children=[Actions([button_in])])

        result = await process_card_callback_urls(card, state, CHANNEL_SCOPE)
        actions = next(c for c in result["children"] if c["type"] == "actions")
        button = actions["children"][0]

        assert {k: v for k, v in button.items() if k != "value"} == {
            "type": "button",
            "id": "approve",
            "label": "Approve",
            "style": "primary",
            "disabled": True,
            "action_type": "modal",
            "tooltip": "Approve the request",
        }
        assert "callback_url" not in button
        assert CALLBACK_PREFIX_PATTERN.match(button["value"])

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

        result = await process_card_callback_urls(card, state, CHANNEL_SCOPE)
        section = next(c for c in result["children"] if c["type"] == "section")
        actions = next(c for c in section["children"] if c["type"] == "actions")
        button = actions["children"][0]
        assert button["type"] == "button"

        assert CALLBACK_TOKEN_PATTERN.match(button["value"])
        assert "callback_url" not in button

        decoded = decode_callback_value(button["value"])
        resolved = await resolve_callback_url(
            decoded.callback_token or "",
            state,
            CallbackContext(action_id="nested-btn", channel_id="slack:C1"),
        )
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
        await process_card_callback_urls(card, state, CHANNEL_SCOPE)
        assert card == original


class TestProcessCardCallbackUrlsStoredRecord:
    """Python-specific: the exact record written to state (cross-SDK shape)."""

    async def test_stores_camelcase_record_with_scope_and_seven_day_ttl(self):
        state = create_mock_state()
        state.set = AsyncMock(wraps=state.set)  # type: ignore[method-assign]
        card = Card(
            children=[
                Actions(
                    [
                        Button(id="with-value", label="A", value="v1", callback_url="https://example.com/a"),
                        Button(id="no-value", label="B", callback_url="https://example.com/b"),
                    ]
                )
            ]
        )

        result = await process_card_callback_urls(card, state, CallbackScope(id="slack:C9:9.9", type="thread"))

        tokens = [decode_callback_value(b["value"]).callback_token for b in result["children"][0]["children"]]
        assert state.set.await_args_list == [
            call(
                f"chat:callback:{tokens[0]}",
                {
                    "actionId": "with-value",
                    "url": "https://example.com/a",
                    "originalValue": "v1",
                    "scope": {"id": "slack:C9:9.9", "type": "thread"},
                },
                CALLBACK_TTL_MS,
            ),
            # `originalValue` is omitted, never written as None.
            call(
                f"chat:callback:{tokens[1]}",
                {
                    "actionId": "no-value",
                    "url": "https://example.com/b",
                    "scope": {"id": "slack:C9:9.9", "type": "thread"},
                },
                CALLBACK_TTL_MS,
            ),
        ]
        assert CALLBACK_TTL_MS == 7 * 24 * 60 * 60 * 1000


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
            {
                "actionId": "approve",
                "url": "https://example.com/hook",
                "originalValue": "item-42",
                "scope": {"id": "slack:C1:1.1", "type": "thread"},
            },
        )
        result = await resolve_callback_url(
            "test-token",
            state,
            CallbackContext(action_id="approve", thread_id="slack:C1:1.1"),
        )
        assert result is not None
        assert result.url == "https://example.com/hook"
        assert result.original_value == "item-42"
        assert result.scope == CallbackScope(id="slack:C1:1.1", type="thread")
        assert await state.get("chat:callback:test-token") is None

    # it("rejects legacy unbound callback records")
    async def test_rejects_legacy_unbound_callback_records(self):
        state = create_mock_state()
        await state.set("chat:callback:legacy-token", "https://example.com/hook")
        result = await resolve_callback_url("legacy-token", state, CallbackContext(action_id="approve"))
        assert result is None

    # it("handles legacy string format") — chat@4.31.0 title, behavior now
    # reversed. Kept so strict fidelity at the 4.31.0 pin stays green until
    # the pin moves to 4.41.1 (#203), where upstream replaced it with the
    # test above. Asserts what the rejection above does not: even a context
    # that would match any record resolves nothing, and the legacy record is
    # left for its TTL rather than deleted.
    async def test_handles_legacy_string_format(self):
        state = create_mock_state()
        await state.set("chat:callback:legacy-token", "https://example.com/hook")
        state.delete = AsyncMock(wraps=state.delete)  # type: ignore[method-assign]

        result = await resolve_callback_url(
            "legacy-token",
            state,
            CallbackContext(action_id="approve", channel_id="slack:C1", thread_id="slack:C1:1.1"),
        )

        assert result is None
        state.delete.assert_not_awaited()
        assert await state.get("chat:callback:legacy-token") == "https://example.com/hook"
        assert state._locks == {}

    # it("allows a callback token to be consumed only once")
    async def test_allows_a_callback_token_to_be_consumed_only_once(self):
        state = create_mock_state()
        await state.set(
            "chat:callback:single-use",
            {
                "actionId": "approve",
                "scope": {"id": "slack:C1", "type": "channel"},
                "url": "https://example.com/hook",
            },
        )
        context = CallbackContext(action_id="approve", channel_id="slack:C1")

        first = await resolve_callback_url("single-use", state, context)
        assert first is not None
        assert first.url == "https://example.com/hook"
        assert await resolve_callback_url("single-use", state, context) is None

    # it("rejects callback tokens outside their action and thread")
    async def test_rejects_callback_tokens_outside_their_action_and_thread(self):
        state = create_mock_state()
        await state.set(
            "chat:callback:bound-token",
            {
                "actionId": "approve",
                "scope": {"id": "slack:C1:1.1", "type": "thread"},
                "url": "https://example.com/hook",
            },
        )

        assert (
            await resolve_callback_url(
                "bound-token",
                state,
                CallbackContext(action_id="deny", thread_id="slack:C1:1.1"),
            )
            is None
        )
        assert (
            await resolve_callback_url(
                "bound-token",
                state,
                CallbackContext(action_id="approve", thread_id="slack:C1:2.2"),
            )
            is None
        )
        assert await state.get("chat:callback:bound-token") is not None

    # it("rejects callback tokens outside their channel")
    async def test_rejects_callback_tokens_outside_their_channel(self):
        state = create_mock_state()
        await state.set(
            "chat:callback:channel-token",
            {
                "actionId": "approve",
                "scope": {"id": "slack:C1", "type": "channel"},
                "url": "https://example.com/hook",
            },
        )

        assert (
            await resolve_callback_url(
                "channel-token",
                state,
                CallbackContext(action_id="approve", channel_id="slack:C2", thread_id="slack:C2:2.2"),
            )
            is None
        )
        resolved = await resolve_callback_url(
            "channel-token",
            state,
            CallbackContext(action_id="approve", channel_id="slack:C1", thread_id="slack:C1:2.2"),
        )
        assert resolved is not None
        assert resolved.url == "https://example.com/hook"


# Python-specific: strict record validation (isinstance, never truthiness).
_VALID_RECORD = {
    "actionId": "approve",
    "url": "https://example.com/hook",
    "scope": {"id": "slack:C1", "type": "channel"},
}


class TestResolveCallbackUrlValidation:
    """Python-specific: record shapes upstream's ``typeof`` checks reject."""

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param({**_VALID_RECORD, "actionId": None}, id="actionId-none"),
            pytest.param({k: v for k, v in _VALID_RECORD.items() if k != "actionId"}, id="actionId-missing"),
            pytest.param({**_VALID_RECORD, "url": 42}, id="url-not-str"),
            pytest.param({**_VALID_RECORD, "originalValue": None}, id="originalValue-none"),
            pytest.param({**_VALID_RECORD, "originalValue": 7}, id="originalValue-not-str"),
            pytest.param({k: v for k, v in _VALID_RECORD.items() if k != "scope"}, id="scope-missing"),
            pytest.param({**_VALID_RECORD, "scope": "slack:C1"}, id="scope-not-dict"),
            pytest.param({**_VALID_RECORD, "scope": {"id": 1, "type": "channel"}}, id="scope-id-not-str"),
            pytest.param({**_VALID_RECORD, "scope": {"id": "slack:C1", "type": "dm"}}, id="scope-type-unknown"),
            pytest.param(["https://example.com/hook"], id="list"),
        ],
    )
    async def test_rejects_malformed_records_without_deleting(self, record):
        state = create_mock_state()
        state.cache["chat:callback:tok"] = record

        result = await resolve_callback_url("tok", state, CallbackContext(action_id="approve", channel_id="slack:C1"))

        assert result is None
        assert state.cache["chat:callback:tok"] == record

    async def test_accepts_empty_string_action_id_like_upstream(self):
        state = create_mock_state()
        state.cache["chat:callback:tok"] = {**_VALID_RECORD, "actionId": ""}

        result = await resolve_callback_url("tok", state, CallbackContext(action_id="", channel_id="slack:C1"))

        assert result is not None
        assert result.action_id == ""
        assert result.original_value is None

    async def test_ignores_snake_case_original_value_key(self):
        # The pre-4.41 Python-only `original_value` fallback read is gone:
        # only the cross-SDK camelCase `originalValue` key is honored.
        state = create_mock_state()
        state.cache["chat:callback:tok"] = {**_VALID_RECORD, "original_value": "legacy"}

        result = await resolve_callback_url("tok", state, CallbackContext(action_id="approve", channel_id="slack:C1"))

        assert result is not None
        assert result.original_value is None

    async def test_none_context_never_matches(self):
        state = create_mock_state()
        state.cache["chat:callback:tok"] = dict(_VALID_RECORD)

        assert await resolve_callback_url("tok", state) is None
        assert "chat:callback:tok" in state.cache


class TestResolveCallbackUrlLocking:
    """Python-specific: the consume path is serialized by a per-token lock."""

    async def test_concurrent_resolves_yield_exactly_one_result(self):
        state = MemoryStateAdapter()
        await state.connect()
        await state.set("chat:callback:race", dict(_VALID_RECORD))
        context = CallbackContext(action_id="approve", channel_id="slack:C1")
        real_get = state.get

        async def yielding_get(key: str):
            # Yield inside the critical section so the second click runs
            # while the first still holds the token's lock.
            await asyncio.sleep(0)
            return await real_get(key)

        state.get = yielding_get  # type: ignore[method-assign]

        results = await asyncio.gather(
            resolve_callback_url("race", state, context),
            resolve_callback_url("race", state, context),
        )

        assert sum(r is not None for r in results) == 1
        assert await state.get("chat:callback:race") is None

    async def test_lost_lease_fails_closed_instead_of_double_consuming(self):
        """Divergence from upstream (docs/UPSTREAM_SYNC.md): upstream returns
        the record even when its 10 s lease lapsed mid-consume, so a second
        click that took the expired lock also resolves and both POST.
        """
        state = MemoryStateAdapter()
        await state.connect()
        await state.set("chat:callback:stall", dict(_VALID_RECORD))
        context = CallbackContext(action_id="approve", channel_id="slack:C1")
        real_delete = state.delete
        stalled = False
        second: list[ResolvedCallback | None] = []

        async def stalled_delete(key: str) -> None:
            nonlocal stalled
            if not stalled:
                stalled = True
                # The lease lapses while this delete is in flight, and a
                # second click takes the lock and consumes the same record.
                await state.force_release_lock(key)
                second.append(await resolve_callback_url("stall", state, context))
            await real_delete(key)

        state.delete = stalled_delete  # type: ignore[method-assign]

        first = await resolve_callback_url("stall", state, context)

        assert first is None
        assert second[0] is not None
        assert second[0].url == "https://example.com/hook"
        assert await state.get("chat:callback:stall") is None

    async def test_returns_none_without_reading_when_lock_is_held(self):
        state = create_mock_state()
        state.cache["chat:callback:held"] = dict(_VALID_RECORD)
        held = await state.acquire_lock("chat:callback:held", CALLBACK_LOCK_TTL_MS)
        assert held is not None
        state.get = AsyncMock(wraps=state.get)  # type: ignore[method-assign]

        result = await resolve_callback_url("held", state, CallbackContext(action_id="approve", channel_id="slack:C1"))

        assert result is None
        state.get.assert_not_awaited()
        assert state.cache["chat:callback:held"] == _VALID_RECORD

    async def test_locks_the_record_key_with_a_ten_second_ttl(self):
        state = create_mock_state()
        state.cache["chat:callback:tok"] = dict(_VALID_RECORD)

        await resolve_callback_url("tok", state, CallbackContext(action_id="approve", channel_id="slack:C1"))

        assert state._acquire_lock_calls == [("chat:callback:tok", 10_000)]
        # Released afterwards, so a later click is not locked out.
        assert state._locks == {}

    async def test_releases_lock_when_get_raises(self):
        state = create_mock_state()
        state.get = AsyncMock(side_effect=RuntimeError("state down"))  # type: ignore[method-assign]
        state.release_lock = AsyncMock(wraps=state.release_lock)  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="state down"):
            await resolve_callback_url("tok", state, CallbackContext(action_id="approve", channel_id="slack:C1"))

        state.release_lock.assert_awaited_once()
        assert state.release_lock.await_args.args[0].thread_id == "chat:callback:tok"
        assert state._locks == {}


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
