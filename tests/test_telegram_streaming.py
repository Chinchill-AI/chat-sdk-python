"""Tests for Telegram native rich streaming + send/edit rewrite.

Faithful translation of the ``index.test.ts`` rich-message additions from
upstream vercel/chat#479 (commit 4662309, "feat(telegram): native rich
streaming + send/edit"). The outbound path is a **rich → MarkdownV2 →
plain** fallback ladder:

* ``stream()`` updates a draft bubble through ``sendRichMessageDraft`` and
  persists the final message via ``sendRichMessage``; on a rich-endpoint
  failure it demotes to the #340 ``sendMessageDraft`` / ``sendMessage``
  MarkdownV2 path (now the second tier), then to plain text.
* ``post_message`` / ``edit_message`` route ``{markdown}`` / ``{ast}``
  payloads through ``sendRichMessage`` / rich ``editMessageText``, with the
  same fallback ladder.

There is **no empty opening draft** anymore (it was removed in the rewrite),
so the streaming call counts here are one lower than the #340 suite.

A 404 ``method not found`` / ``ResourceNotFound`` on a rich call latches
``_rich_messages_available`` to ``False`` permanently (future messages skip
rich). A transient ``can't parse`` / validation failure falls back for the
current message only and leaves the flag set.

Upstream mocks ``fetch`` at the HTTP layer; these tests follow the
established Python convention of mocking ``telegram_fetch`` with an ordered
script — each entry is either a value to return or an exception to raise.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TelegramAdapter,
)
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig
from chat_sdk.shared.errors import (
    AdapterRateLimitError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.shared.mock_adapter import MockLogger

# =============================================================================
# Helpers
# =============================================================================

DM_THREAD_ID = "telegram:123"
DM_CHAT_ID = "123"


def _make_adapter(**overrides: Any) -> TelegramAdapter:
    """Create a TelegramAdapter with minimal valid config and a MockLogger."""
    config = TelegramAdapterConfig(
        bot_token=overrides.pop("bot_token", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"),
        logger=overrides.pop("logger", MockLogger()),
        user_name=overrides.pop("user_name", "mybot"),
        **overrides,
    )
    return TelegramAdapter(config)


def _sample_message(
    text: str | None = "hello world",
    message_id: int = 11,
    chat_id: int = 123,
    chat_type: str = "private",
    rich_message: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a minimal Telegram message dict (mirrors upstream sampleMessage)."""
    msg: dict[str, Any] = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 999, "is_bot": True, "first_name": "Bot", "username": "mybot"},
        "date": 1700000000,
    }
    if text is not None:
        msg["text"] = text
    if rich_message is not None:
        msg["rich_message"] = rich_message
    return msg


def _rich_message(text: str) -> dict[str, Any]:
    """A rich_message payload whose single paragraph carries *text*."""
    return {"blocks": [{"type": "paragraph", "text": text}]}


def _parse_entities_error() -> ValidationError:
    """The error ``throw_telegram_api_error`` raises for an entity-parse 400."""
    return ValidationError(
        "telegram",
        "Bad Request: can't parse entities: Can't find end of the entity",
    )


def _method_not_found_error() -> ResourceNotFoundError:
    """The error a 404 ``method not found`` maps to (rich endpoint missing).

    ``throw_telegram_api_error`` raises ``ResourceNotFoundError(adapter,
    method)`` for an ``error_code`` of 404, mirroring upstream's
    ``ResourceNotFoundError`` for the absent ``sendRichMessage*`` endpoints.
    """
    return ResourceNotFoundError("telegram", "sendRichMessage")


def _rich_unsupported_error() -> ValidationError:
    """A ``rich message ... unsupported`` 400 (latches rich off)."""
    return ValidationError("telegram", "Bad Request: rich message is unsupported")


def _rich_cant_parse_error() -> ValidationError:
    """A transient ``can't parse rich message`` 400 (does NOT latch rich off)."""
    return ValidationError("telegram", "Bad Request: can't parse rich message")


def _script_fetch(adapter: TelegramAdapter, script: list[Any]) -> list[tuple[str, Any]]:
    """Replace ``telegram_fetch`` with an ordered script of results.

    Each entry is returned in turn; ``Exception`` entries are raised
    instead. Returns the recorded ``(method, payload)`` call list.
    """
    calls: list[tuple[str, Any]] = []
    queue = list(script)

    async def fetch(method: str, payload: Any = None, **_kwargs: Any) -> Any:
        calls.append((method, payload))
        if not queue:
            raise AssertionError(f"Unexpected Telegram API call: {method}")
        action = queue.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    adapter.telegram_fetch = fetch  # type: ignore[method-assign]
    return calls


def _method_fetch(adapter: TelegramAdapter, handlers: dict[str, Any]) -> list[tuple[str, Any]]:
    """Replace ``telegram_fetch`` with a per-method handler map.

    Each handler is either a static value (returned), an ``Exception``
    (raised), or a zero-arg callable producing the next value. Mirrors
    upstream's ``mockFetch.mockImplementation`` switch on the Bot API method.
    """
    calls: list[tuple[str, Any]] = []

    async def fetch(method: str, payload: Any = None, **_kwargs: Any) -> Any:
        calls.append((method, payload))
        if method not in handlers:
            raise AssertionError(f"Unexpected Telegram method in test: {method}")
        handler = handlers[method]
        if callable(handler):
            handler = handler()
        if isinstance(handler, Exception):
            raise handler
        return handler

    adapter.telegram_fetch = fetch  # type: ignore[method-assign]
    return calls


async def _text_stream(chunks: list[str]):
    for chunk in chunks:
        yield chunk


def _stream_options(update_interval_ms: int):
    from chat_sdk.types import StreamOptions

    options = StreamOptions()
    options.update_interval_ms = update_interval_ms
    return options


# =============================================================================
# Tests -- non-streaming rich send (post_message)
# =============================================================================


class TestTelegramRichSend:
    """it() blocks for postMessage / editMessage rich routing."""

    # it("uses rich messages for markdown messages")
    @pytest.mark.asyncio
    async def test_uses_rich_messages_for_markdown_messages(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [_sample_message()])

        await adapter.post_message(DM_THREAD_ID, {"markdown": "**bold** and _italic_"})

        assert calls[0][0] == "sendRichMessage"
        assert calls[0][1]["chat_id"] == DM_CHAT_ID
        assert calls[0][1]["rich_message"]["markdown"] == "**bold** and _italic_"

    # it("uses rich messages for AST messages")
    @pytest.mark.asyncio
    async def test_uses_rich_messages_for_ast_messages(self):
        from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter

        adapter = _make_adapter()
        calls = _script_fetch(adapter, [_sample_message()])

        ast = TelegramFormatConverter().to_ast("**hello** world!")
        await adapter.post_message(DM_THREAD_ID, {"ast": ast})

        assert calls[0][0] == "sendRichMessage"
        # stringifyMarkdown renders strong as ``**`` and appends a trailing
        # newline — the rich markdown is sent verbatim.
        assert calls[0][1]["rich_message"]["markdown"] == "**hello** world!\n"

    # it("omits parse_mode for plain string messages") -- plain strings skip
    # rich entirely (the resolveRichMessage `typeof message === "string"` gate)
    # and ship through sendMessage with no parse_mode.
    @pytest.mark.asyncio
    async def test_plain_string_messages_skip_rich_and_omit_parse_mode(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [_sample_message()])

        await adapter.post_message(DM_THREAD_ID, "plain text message")

        assert calls[0][0] == "sendMessage"
        assert calls[0][1].get("parse_mode") is None

    # it("omits parse_mode for raw messages") -- a ``raw`` payload opts out of
    # rich via the `"raw" in message` PRESENCE gate, even non-empty.
    @pytest.mark.asyncio
    async def test_raw_messages_skip_rich_and_ship_verbatim(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [_sample_message()])

        await adapter.post_message(DM_THREAD_ID, {"raw": "raw.unparsed!(text)"})

        assert calls[0][0] == "sendMessage"
        assert calls[0][1].get("parse_mode") is None
        assert calls[0][1]["text"] == "raw.unparsed!(text)"


# =============================================================================
# Tests -- rich draft streaming (vercel/chat#479)
# =============================================================================


class TestTelegramRichDraftStreaming:
    """it() blocks for TelegramAdapter.stream rich draft updates."""

    # it("streams draft updates for private chats and sends a final message")
    #
    # HAZARD 1 (dropped opening draft): the rewrite removed the empty
    # ``send_draft("", False)`` that opened the bubble in #340. With two
    # chunks at interval 0 the call sequence is now [draft, draft, final] =
    # THREE calls (was four). This test pins the NEW count.
    @pytest.mark.asyncio
    async def test_streams_rich_draft_updates_and_sends_final_rich_message(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                True,  # first sendRichMessageDraft
                True,  # second sendRichMessageDraft
                _sample_message(text=None, rich_message=_rich_message("hello world")),
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["hello", " world"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"
        assert result.thread_id == DM_THREAD_ID

        methods = [method for method, _payload in calls]
        # No opening empty draft: two rich drafts then the final rich send.
        assert methods == ["sendRichMessageDraft", "sendRichMessageDraft", "sendRichMessage"]

        first_draft = calls[0][1]
        second_draft = calls[1][1]
        final_send = calls[2][1]

        assert first_draft["chat_id"] == DM_CHAT_ID
        assert first_draft["rich_message"]["markdown"] == "hello"
        assert second_draft["draft_id"] == first_draft["draft_id"]
        assert second_draft["rich_message"]["markdown"] == "hello world"
        assert final_send["chat_id"] == DM_CHAT_ID
        assert final_send["rich_message"]["markdown"] == "hello world"

    # it("flushes trailing table-like lines before completing a rich stream")
    #
    # HAZARD 2 (flush AFTER finish): the StreamingMarkdownRenderer holds back a
    # complete-but-still-streaming table block — ``renderer.render()`` returns
    # ``""`` for it mid-stream and only ``renderer.finish()`` releases the full
    # ``| a | b | ... | 1 | 2 |`` markdown. The final flush MUST run after
    # ``finish()`` so the last draft carries the released table.
    #
    # With the flush BEFORE finish (the mutation), the post-finish flush is
    # gone and the only draft would carry the held-back ``""`` render — so the
    # draft-body assertion below FAILS on that ordering. The whole table is
    # supplied in one chunk with interval MAX so no mid-stream flush fires; the
    # post-finish flush is the sole draft.
    @pytest.mark.asyncio
    async def test_flushes_trailing_table_like_block_after_renderer_finish(self):
        table = "| a | b |\n| - | - |\n| 1 | 2 |"
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                True,  # sendRichMessageDraft (the post-finish flush)
                _sample_message(text=None, rich_message=_rich_message("table")),
            ],
        )

        await adapter.stream(DM_THREAD_ID, _text_stream([table]), _stream_options(2**53 - 1))

        assert [method for method, _payload in calls] == ["sendRichMessageDraft", "sendRichMessage"]
        draft_body = calls[0][1]
        final_body = calls[1][1]
        # The held-back table only appears in the draft because the flush ran
        # AFTER finish; a pre-finish flush would have sent an empty render.
        assert draft_body["rich_message"]["markdown"] == table
        assert final_body["rich_message"]["markdown"] == table

    # it("keeps rich markdown for a draft and final message") -- the rich
    # markdown is sent verbatim (no MarkdownV2 re-render). A long body whose
    # ``**ok**`` survives intact proves the rich path bypasses the escaping
    # MarkdownV2 renderer entirely.
    @pytest.mark.asyncio
    async def test_keeps_rich_markdown_for_draft_and_final_without_markdownv2_rerender(self):
        long_markdown = "a" * 3494 + "**ok**"

        adapter = _make_adapter()
        calls = _method_fetch(
            adapter,
            {
                "sendRichMessageDraft": True,
                "sendRichMessage": lambda: _sample_message(text=None, message_id=41, rich_message=_rich_message("ok")),
            },
        )

        result = await adapter.stream(
            DM_THREAD_ID,
            _text_stream([long_markdown]),
            _stream_options(2**53 - 1),
        )

        assert result is not None
        assert [
            (method, len(payload["rich_message"]["markdown"]), payload["rich_message"]["markdown"][-10:])
            for method, payload in calls
        ] == [
            ("sendRichMessageDraft", len(long_markdown), long_markdown[-10:]),
            ("sendRichMessage", len(long_markdown), long_markdown[-10:]),
        ]
        # The literal ``**ok**`` markers survive — never escaped to ``\*\*``.
        assert calls[0][1]["rich_message"]["markdown"] == long_markdown
        assert calls[1][1]["rich_message"]["markdown"] == long_markdown

    # it("returns null for non-DM streaming so Chat SDK can use fallback streaming")
    @pytest.mark.asyncio
    async def test_returns_null_for_nondm_streaming_so_chat_sdk_can_use_fallback_streaming(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [])

        result = await adapter.stream(
            "telegram:-100123",
            _text_stream(["hello"]),
            _stream_options(0),
        )

        assert result is None
        # Delegation happens before any chunk is consumed or API call made.
        assert calls == []

    # it("renders MarkdownV2 when rich draft streaming is unavailable")
    #
    # HAZARD 4a (404/ResourceNotFound -> permanent flag flip): the first rich
    # draft 404s with ``method not found``; the stream demotes to the
    # MarkdownV2 ``sendMessageDraft`` path AND latches
    # ``_rich_messages_available`` to ``False`` permanently. The final send
    # goes through ``sendMessage`` (MarkdownV2), not ``sendRichMessage``.
    @pytest.mark.asyncio
    async def test_renders_markdownv2_when_rich_draft_streaming_is_unavailable(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [
                ValidationError("telegram", "Not Found: method not found"),  # rich draft missing
                True,  # MarkdownV2 sendMessageDraft accepted
                _sample_message(text="hello world"),  # final sendMessage
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**hello**"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"
        assert any(
            call[0] == "Telegram rich draft failed; retrying with a regular draft"
            and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )

        methods = [method for method, _payload in calls]
        assert methods == ["sendRichMessageDraft", "sendMessageDraft", "sendMessage"]

        fallback_draft = calls[1][1]
        assert fallback_draft["chat_id"] == DM_CHAT_ID
        assert fallback_draft["parse_mode"] == "MarkdownV2"
        assert fallback_draft["text"] == "*hello*"
        # The endpoint-missing failure latched rich off for the instance.
        assert adapter._rich_messages_available is False

    # it("continues to the final message when markdown draft retry also fails")
    #
    # Full ladder exercised: rich draft 404s (demote to MarkdownV2 + latch
    # off), MarkdownV2 draft hits a can't-parse 400 (demote to plain), the
    # plain retry 429s (draft updates disabled), yet the final plain
    # ``sendMessage`` still persists the message.
    @pytest.mark.asyncio
    async def test_continues_to_the_final_message_when_markdown_draft_retry_also_fails(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [
                ValidationError("telegram", "Not Found: method not found"),  # rich draft missing
                _parse_entities_error(),  # MarkdownV2 draft rejected
                AdapterRateLimitError("telegram", 1),  # plain retry 429s
                _sample_message(text="**broken"),  # final plain send
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**broken"]), _stream_options(0))

        assert result is not None
        assert any(
            call[0] == "Telegram draft streaming update failed" and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )

        final_send = calls[3][1]
        assert calls[3][0] == "sendMessage"
        assert final_send.get("parse_mode") is None
        assert final_send["text"] == "**broken"

    # Python-only: a transient rich-draft ``can't parse`` failure demotes the
    # CURRENT stream to MarkdownV2 but must NOT latch rich off — the flag stays
    # True so the next stream tries rich again. Pins HAZARD 4 from the draft
    # side (can_fallback allows fallback; remember_failure does not flip).
    @pytest.mark.asyncio
    async def test_transient_rich_draft_failure_does_not_latch_rich_off(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                _rich_cant_parse_error(),  # transient rich draft failure
                True,  # MarkdownV2 sendMessageDraft accepted
                _sample_message(text="hello"),  # final sendMessage
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**hello**"]), _stream_options(0))

        assert result is not None
        # Demoted this stream to the MarkdownV2 path...
        assert [m for m, _ in calls] == ["sendRichMessageDraft", "sendMessageDraft", "sendMessage"]
        # ...but rich remains available for the next message.
        assert adapter._rich_messages_available is True

    # it("falls back to plain-text draft and final send when Telegram can't
    #     parse streamed markdown") -- rich disabled up front (flag already
    #     False), MarkdownV2 draft rejected, plain retry accepted, plain final.
    @pytest.mark.asyncio
    async def test_falls_back_to_plaintext_draft_and_final_send_when_telegram_cant_parse_streamed_markdown(self):
        adapter = _make_adapter()
        adapter._rich_messages_available = False  # rich endpoint known-missing
        calls = _script_fetch(
            adapter,
            [
                _parse_entities_error(),  # MarkdownV2 draft rejected
                True,  # plain retry accepted
                _sample_message(text="**broken"),  # final plain send
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**broken"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"

        retry_draft = calls[1][1]
        final_send = calls[2][1]
        assert calls[0][0] == "sendMessageDraft"
        assert calls[1][0] == "sendMessageDraft"
        assert "parse_mode" not in retry_draft
        assert retry_draft["text"] == "**broken"
        assert calls[2][0] == "sendMessage"
        assert final_send.get("parse_mode") is None
        assert final_send["text"] == "**broken"

    # Python-only: direct adapter.stream call with options omitted entirely
    # still defaults update_interval_ms instead of crashing. With rich enabled
    # the drafts go through sendRichMessageDraft and the final via
    # sendRichMessage.
    @pytest.mark.asyncio
    async def test_stream_defaults_update_interval_when_options_omitted(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [True, True, _sample_message(text=None, rich_message=_rich_message("hello world"))],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["hello", " world"]))

        assert result is not None
        assert result.id == "123:11"
        # Final rich send last; only rich drafts before it (count depends on
        # wall-clock timing against the 250ms default, so only shape asserted).
        assert calls[-1][0] == "sendRichMessage"
        assert all(method == "sendRichMessageDraft" for method, _payload in calls[:-1])

    # Python-only: the empty-stream contract — whitespace-only streams raise
    # BEFORE any final send. With no opening draft, the only call is the
    # mid-stream rich draft for the whitespace chunk (the rich render of
    # ``"  "`` is non-empty), after which ``accumulated.strip()`` is empty and
    # the stream raises rather than persisting a blank message.
    @pytest.mark.asyncio
    async def test_stream_rejects_whitespace_only_streams(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [True])

        with pytest.raises(ValidationError, match="requires text content"):
            await adapter.stream(DM_THREAD_ID, _text_stream(["  "]), _stream_options(0))

        # Only the mid-stream rich draft went out; no final send was attempted.
        assert [method for method, _payload in calls] == ["sendRichMessageDraft"]


# =============================================================================
# Tests -- rich send/edit fallback ladder (post_message / edit_message)
# =============================================================================


class TestTelegramRichSendFallback:
    """it() blocks for the rich -> MarkdownV2 -> plain ladder on post/edit."""

    # it("falls back from rich messages to plain text when Telegram can't
    #     parse markdown")
    #
    # HAZARD 3 + 4b: a transient ``can't parse rich message`` lets the message
    # fall back (rich -> MarkdownV2 -> plain) but does NOT latch rich off.
    @pytest.mark.asyncio
    async def test_falls_back_from_rich_to_plain_text_on_cant_parse(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [
                _rich_cant_parse_error(),  # rich send rejected (transient)
                _parse_entities_error(),  # MarkdownV2 send rejected
                _sample_message(text="**broken"),  # plain send accepted
            ],
        )

        result = await adapter.post_message(DM_THREAD_ID, {"markdown": "**broken"})

        assert result.id == "123:11"

        assert calls[0][0] == "sendRichMessage"
        assert calls[0][1]["rich_message"]["markdown"] == "**broken"
        assert calls[1][0] == "sendMessage"
        assert calls[1][1]["parse_mode"] == "MarkdownV2"
        assert calls[2][0] == "sendMessage"
        assert calls[2][1].get("parse_mode") is None
        assert calls[2][1]["text"] == "**broken"
        assert any(
            call[0] == "Telegram rich message failed; retrying with a regular message"
            and call[1]["method"] == "sendRichMessage"
            and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )
        # Transient parse failure: rich stays enabled for the next message.
        assert adapter._rich_messages_available is True

    # it("caches unavailable rich message endpoints")
    #
    # HAZARD 4a (permanent flag flip): a 404 ``method not found`` on the first
    # rich send latches ``_rich_messages_available`` off, so the SECOND post
    # skips ``sendRichMessage`` and goes straight to ``sendMessage``.
    @pytest.mark.asyncio
    async def test_caches_unavailable_rich_message_endpoints(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                _method_not_found_error(),  # first rich send 404s
                _sample_message(),  # first message falls back to sendMessage
                _sample_message(message_id=12),  # second message: sendMessage directly
            ],
        )

        await adapter.post_message(DM_THREAD_ID, {"markdown": "first"})
        assert adapter._rich_messages_available is False

        await adapter.post_message(DM_THREAD_ID, {"markdown": "second"})

        assert calls[0][0] == "sendRichMessage"
        assert calls[1][0] == "sendMessage"
        # The second message never attempts rich — the flag stayed flipped.
        assert calls[2][0] == "sendMessage"

    # it("does not swallow non-parse validation errors during markdown send")
    @pytest.mark.asyncio
    async def test_does_not_swallow_nonparse_validation_errors_during_rich_send(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [ValidationError("telegram", "Bad Request: chat not found")],
        )

        with pytest.raises(ValidationError, match="chat not found"):
            await adapter.post_message(DM_THREAD_ID, {"markdown": "**broken**"})

        # A non-rich-related validation error is NOT a fallback trigger: the
        # rich send raises and nothing else is attempted.
        assert [method for method, _payload in calls] == ["sendRichMessage"]
        assert adapter._rich_messages_available is True

    # it("does not treat unrelated unsupported errors as rich message failures")
    # -- ``message thread is unsupported`` lacks the ``rich message`` token, so
    # canFallbackFromRichMessage rejects it and the error propagates.
    @pytest.mark.asyncio
    async def test_does_not_treat_unrelated_unsupported_errors_as_rich_failures(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [ValidationError("telegram", "Bad Request: message thread is unsupported")],
        )

        with pytest.raises(ValidationError, match="message thread is unsupported"):
            await adapter.post_message(DM_THREAD_ID, {"markdown": "**hello**"})

        assert [method for method, _payload in calls] == ["sendRichMessage"]
        assert adapter._rich_messages_available is True

    # it("falls back from rich edits to plain text when Telegram can't parse
    #     markdown") -- a ``rich message is unsupported`` edit failure latches
    #     rich off AND falls back through the MarkdownV2 -> plain edit ladder.
    @pytest.mark.asyncio
    async def test_falls_back_from_rich_edits_to_plain_text(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                _sample_message(),  # post (rich send succeeds)
                _rich_unsupported_error(),  # rich edit unsupported -> latch off
                _parse_entities_error(),  # MarkdownV2 edit rejected
                _sample_message(text="**broken"),  # plain edit accepted
            ],
        )

        posted = await adapter.post_message(DM_THREAD_ID, "hello")
        result = await adapter.edit_message(DM_THREAD_ID, posted.id, {"markdown": "**broken"})

        assert result.id == "123:11"

        # post(hello) is a plain string -> sendMessage (no rich).
        assert calls[0][0] == "sendMessage"
        assert calls[1][0] == "editMessageText"
        assert calls[1][1]["rich_message"]["markdown"] == "**broken"
        assert calls[2][0] == "editMessageText"
        assert calls[2][1]["parse_mode"] == "MarkdownV2"
        assert calls[3][0] == "editMessageText"
        assert calls[3][1].get("parse_mode") is None
        assert calls[3][1]["text"] == "**broken"
        # ``rich message ... unsupported`` latched rich off.
        assert adapter._rich_messages_available is False


# =============================================================================
# Tests -- can_fallback_from_rich_message vs remember_rich_message_failure
# =============================================================================


class TestRichFallbackPredicates:
    """Direct unit tests for the two DISTINCT boolean shapes (HAZARD 4).

    ``can_fallback_from_rich_message`` decides whether a rich failure may
    retry as a regular send. ``remember_rich_message_failure`` decides the
    NARROWER set that also latches rich off permanently. A transient
    ``can't parse`` qualifies for the former but NOT the latter.
    """

    def test_cant_parse_allows_fallback_but_does_not_latch(self):
        adapter = _make_adapter()
        err = _rich_cant_parse_error()

        assert adapter.can_fallback_from_rich_message(err, "sendRichMessage") is True

        adapter.remember_rich_message_failure(err, "sendRichMessage")
        # Transient parse failure must leave rich enabled.
        assert adapter._rich_messages_available is True

    def test_method_not_found_validation_allows_fallback_and_latches(self):
        adapter = _make_adapter()
        err = ValidationError("telegram", "Not Found: method not found")

        assert adapter.can_fallback_from_rich_message(err, "sendRichMessageDraft") is True

        adapter.remember_rich_message_failure(err, "sendRichMessageDraft")
        assert adapter._rich_messages_available is False

    def test_unsupported_rich_validation_allows_fallback_and_latches(self):
        adapter = _make_adapter()
        err = _rich_unsupported_error()

        assert adapter.can_fallback_from_rich_message(err, "sendRichMessage") is True

        adapter.remember_rich_message_failure(err, "sendRichMessage")
        assert adapter._rich_messages_available is False

    def test_resource_not_found_only_falls_back_for_send_rich_methods(self):
        # The ResourceNotFound branch is gated on ``method.startswith
        # ("sendRichMessage")``. A 404 attributed to a non-rich method is NOT a
        # rich fallback trigger.
        adapter = _make_adapter()
        err = ResourceNotFoundError("telegram", "editMessageText")

        assert adapter.can_fallback_from_rich_message(err, "sendRichMessage") is True
        assert adapter.can_fallback_from_rich_message(err, "editMessageText") is False

    def test_resource_not_found_latches_only_for_send_rich_methods(self):
        adapter = _make_adapter()

        adapter.remember_rich_message_failure(ResourceNotFoundError("telegram", "editMessageText"), "editMessageText")
        assert adapter._rich_messages_available is True

        adapter.remember_rich_message_failure(ResourceNotFoundError("telegram", "sendRichMessage"), "sendRichMessage")
        assert adapter._rich_messages_available is False

    def test_unrelated_validation_error_neither_falls_back_nor_latches(self):
        adapter = _make_adapter()
        err = ValidationError("telegram", "Bad Request: message thread is unsupported")

        # ``unsupported`` without the ``rich message`` token does not qualify.
        assert adapter.can_fallback_from_rich_message(err, "sendRichMessage") is False

        adapter.remember_rich_message_failure(err, "sendRichMessage")
        assert adapter._rich_messages_available is True


# =============================================================================
# Tests -- resolve_rich_message gate (HAZARD 3: 'raw' PRESENCE disjunction)
# =============================================================================


class TestResolveRichMessageGate:
    """Direct unit tests for resolve_rich_message's opt-out disjunction."""

    def test_markdown_payload_resolves_to_rich(self):
        adapter = _make_adapter()
        rich = adapter.resolve_rich_message({"markdown": "**hi**"}, None, 0, 0)
        assert rich is not None
        assert rich.markdown == "**hi**"

    def test_raw_presence_opts_out_even_when_empty(self):
        # ``"raw" in message`` is a PRESENCE check, not truthiness, and it
        # short-circuits BEFORE the ``markdown`` branch. A payload carrying an
        # EMPTY ``raw`` AND a ``markdown`` body still opts out of rich — the
        # raw key alone disables it. A ``message.get("raw")`` truthiness
        # mutation would treat the empty ``raw`` as absent, fall through to the
        # ``markdown`` branch, and (wrongly) resolve a rich payload — so the
        # ``is None`` assertion below FAILS on that mutation. (Without the
        # co-present ``markdown`` key the gate is untestable: a bare
        # ``{"raw": ""}`` resolves to ``None`` either way for lack of a body.)
        adapter = _make_adapter()
        assert adapter.resolve_rich_message({"raw": "", "markdown": "**hi**"}, None, 0, 0) is None
        assert adapter.resolve_rich_message({"raw": "non-empty", "markdown": "**hi**"}, None, 0, 0) is None

    def test_plain_string_opts_out(self):
        adapter = _make_adapter()
        assert adapter.resolve_rich_message("plain", None, 0, 0) is None

    def test_card_opts_out(self):
        adapter = _make_adapter()
        assert adapter.resolve_rich_message({"markdown": "**hi**"}, object(), 0, 0) is None

    def test_file_or_attachment_opts_out(self):
        adapter = _make_adapter()
        assert adapter.resolve_rich_message({"markdown": "**hi**"}, None, 1, 0) is None
        assert adapter.resolve_rich_message({"markdown": "**hi**"}, None, 0, 1) is None

    def test_unavailable_flag_opts_out(self):
        adapter = _make_adapter()
        adapter._rich_messages_available = False
        assert adapter.resolve_rich_message({"markdown": "**hi**"}, None, 0, 0) is None


# =============================================================================
# Tests -- retry without parse_mode (regular sendMessage path)
# =============================================================================


class TestTelegramMarkdownParseFallback:
    """it() blocks for withTelegramMarkdownFallback on the regular send path.

    These exercise the SECOND tier (MarkdownV2 -> plain) directly by routing
    through messages that skip rich (``raw`` payloads / rich-disabled
    adapters), keeping the markdown-retry coverage from #340 intact.
    """

    # it("retries markdown messages without parse_mode when Telegram can't
    #     parse entities") -- driven through a rich-disabled adapter so the
    #     ``{markdown}`` post lands directly on the MarkdownV2 sendMessage.
    @pytest.mark.asyncio
    async def test_retries_markdown_messages_without_parsemode_when_telegram_cant_parse_entities(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        adapter._rich_messages_available = False
        calls = _script_fetch(
            adapter,
            [_parse_entities_error(), _sample_message(text="**broken")],
        )

        result = await adapter.post_message(DM_THREAD_ID, {"markdown": "**broken"})

        assert result.id == "123:11"

        first_send = calls[0][1]
        second_send = calls[1][1]
        assert calls[0][0] == "sendMessage"
        assert first_send["parse_mode"] == "MarkdownV2"
        assert second_send.get("parse_mode") is None
        assert second_send["text"] == "**broken"
        assert any(
            call[0] == "Telegram markdown parse failed; retrying without parse mode"
            and call[1]["method"] == "sendMessage"
            and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )

    # it("retries markdown messages with original text when plain-text
    #     fallback would be empty")
    @pytest.mark.asyncio
    async def test_retries_markdown_messages_with_original_text_when_plaintext_fallback_would_be_empty(self):
        adapter = _make_adapter()
        adapter._rich_messages_available = False
        calls = _script_fetch(
            adapter,
            [_parse_entities_error(), _sample_message(text="**")],
        )

        result = await adapter.post_message(DM_THREAD_ID, {"markdown": "**"})

        assert result.id == "123:11"

        second_send = calls[1][1]
        assert second_send.get("parse_mode") is None
        assert second_send["text"] == "**"

    # it("does not swallow non-parse validation errors during markdown send")
    @pytest.mark.asyncio
    async def test_does_not_swallow_nonparse_validation_errors_during_markdown_send(self):
        adapter = _make_adapter()
        adapter._rich_messages_available = False
        calls = _script_fetch(
            adapter,
            [ValidationError("telegram", "Bad Request: chat not found")],
        )

        with pytest.raises(ValidationError, match="chat not found"):
            await adapter.post_message(DM_THREAD_ID, {"markdown": "**broken**"})

        # No retry attempt for non-parse errors.
        assert [method for method, _payload in calls] == ["sendMessage"]

    # it("retries markdown edits without parse_mode when Telegram can't
    #     parse entities")
    @pytest.mark.asyncio
    async def test_retries_markdown_edits_without_parsemode_when_telegram_cant_parse_entities(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        adapter._rich_messages_available = False
        calls = _script_fetch(
            adapter,
            [
                _sample_message(text="hello"),  # post
                _parse_entities_error(),  # first edit
                _sample_message(text="**broken"),  # retry edit
            ],
        )

        posted = await adapter.post_message(DM_THREAD_ID, "hello")
        result = await adapter.edit_message(DM_THREAD_ID, posted.id, {"markdown": "**broken"})

        assert result.id == "123:11"

        first_edit = calls[1][1]
        second_edit = calls[2][1]
        assert calls[1][0] == "editMessageText"
        assert first_edit["parse_mode"] == "MarkdownV2"
        assert calls[2][0] == "editMessageText"
        assert second_edit.get("parse_mode") is None
        assert second_edit["text"] == "**broken"
        assert any(
            call[0] == "Telegram markdown parse failed; retrying without parse mode"
            and call[1]["method"] == "editMessageText"
            and call[1]["message_id"] == posted.id
            and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )

    # it("retries markdown edits with original text when plain-text fallback
    #     would be empty")
    @pytest.mark.asyncio
    async def test_retries_markdown_edits_with_original_text_when_plaintext_fallback_would_be_empty(self):
        adapter = _make_adapter()
        adapter._rich_messages_available = False
        calls = _script_fetch(
            adapter,
            [
                _sample_message(text="hello"),  # post
                _parse_entities_error(),  # first edit
                _sample_message(text="**"),  # retry edit
            ],
        )

        posted = await adapter.post_message(DM_THREAD_ID, "hello")
        result = await adapter.edit_message(DM_THREAD_ID, posted.id, {"markdown": "**"})

        assert result.id == "123:11"

        second_edit = calls[2][1]
        assert second_edit.get("parse_mode") is None
        assert second_edit["text"] == "**"

    # Python-only: pins the empty-fallback branch of resolve_telegram_fallback_text.
    def test_resolve_telegram_fallback_text_reuses_original_when_fallback_is_blank(self):
        adapter = _make_adapter()

        assert adapter.resolve_telegram_fallback_text("**original**", "plain") == "plain"
        assert adapter.resolve_telegram_fallback_text("**original**", "") == "**original**"
        assert adapter.resolve_telegram_fallback_text("**original**", "  \n") == "**original**"
