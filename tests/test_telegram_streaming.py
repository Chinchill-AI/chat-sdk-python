"""Tests for Telegram native DM draft streaming and markdown-parse fallback.

Faithful translation of the ``index.test.ts`` additions from upstream
vercel/chat#340 (commit 5461ea9, "feat(telegram): add native DM draft
streaming with segmented stream results"): ``stream()`` via
``sendMessageDraft`` for private chats, ``None``-delegation for non-DM
threads, and the retry-without-``parse_mode`` path shared by
``post_message`` / ``edit_message`` when Telegram rejects MarkdownV2
entity parsing.

Upstream mocks ``fetch`` at the HTTP layer; these tests follow the
established Python convention (see ``test_telegram_api.py``) of mocking
``telegram_fetch`` with an ordered script — each entry is either a value
to return or an exception to raise, mirroring upstream's
``mockResolvedValueOnce`` / ``telegramError`` chains after Bot API error
mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.adapters.telegram.adapter import (
    TelegramAdapter,
)
from chat_sdk.adapters.telegram.types import TelegramAdapterConfig
from chat_sdk.shared.errors import AdapterRateLimitError, ValidationError
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
    text: str = "hello world",
    message_id: int = 11,
    chat_id: int = 123,
    chat_type: str = "private",
) -> dict[str, Any]:
    """Return a minimal Telegram message dict (mirrors upstream sampleMessage)."""
    return {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 999, "is_bot": True, "first_name": "Bot", "username": "mybot"},
        "date": 1700000000,
        "text": text,
    }


def _parse_entities_error() -> ValidationError:
    """The error ``throw_telegram_api_error`` raises for an entity-parse 400."""
    return ValidationError(
        "telegram",
        "Bad Request: can't parse entities: Can't find end of the entity",
    )


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


async def _text_stream(chunks: list[str]):
    for chunk in chunks:
        yield chunk


def _stream_options(update_interval_ms: int):
    from chat_sdk.types import StreamOptions

    options = StreamOptions()
    options.update_interval_ms = update_interval_ms
    return options


# =============================================================================
# Tests -- stream() draft streaming (vercel/chat#340)
# =============================================================================


class TestTelegramDraftStreaming:
    """it() blocks for TelegramAdapter.stream draft updates."""

    # it("streams draft updates for private chats and sends a final message")
    @pytest.mark.asyncio
    async def test_streams_draft_updates_for_private_chats_and_sends_a_final_message(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [True, True, True, _sample_message()])

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["hello", " world"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"
        assert result.thread_id == DM_THREAD_ID

        methods = [method for method, _payload in calls]
        assert methods == ["sendMessageDraft", "sendMessageDraft", "sendMessageDraft", "sendMessage"]

        initial_draft = calls[0][1]
        first_draft = calls[1][1]
        second_draft = calls[2][1]
        final_send = calls[3][1]

        assert initial_draft["chat_id"] == DM_CHAT_ID
        assert initial_draft["text"] == ""
        assert "parse_mode" not in initial_draft
        assert first_draft["chat_id"] == DM_CHAT_ID
        assert first_draft["draft_id"] == initial_draft["draft_id"]
        assert first_draft["text"] == "hello"
        assert first_draft["parse_mode"] == "MarkdownV2"
        assert second_draft["draft_id"] == first_draft["draft_id"]
        assert second_draft["text"] == "hello world"
        assert final_send["chat_id"] == DM_CHAT_ID
        assert final_send["text"] == "hello world"
        assert final_send["parse_mode"] == "MarkdownV2"

    # it("keeps markdown parse mode for an exact-limit draft and final message")
    @pytest.mark.asyncio
    async def test_keeps_markdown_parse_mode_for_an_exactlimit_draft_and_final_message(self):
        long_markdown = "a" * 3494 + "**ok**"
        rendered_markdown = "a" * 3494 + "*ok*"

        adapter = _make_adapter()
        calls = _script_fetch(adapter, [True, True, _sample_message(text="a" * 3494 + "ok", message_id=41)])

        result = await adapter.stream(
            DM_THREAD_ID,
            _text_stream([long_markdown]),
            # JS Number.MAX_SAFE_INTEGER: no mid-stream flush; only the
            # end-of-stream flush ships the rendered draft.
            _stream_options(2**53 - 1),
        )

        assert result is not None
        assert [
            (method, len(payload["text"]), payload["text"][-10:], payload.get("parse_mode"))
            for method, payload in calls
        ] == [
            ("sendMessageDraft", 0, "", None),
            (
                "sendMessageDraft",
                len(rendered_markdown),
                rendered_markdown[-10:],
                "MarkdownV2",
            ),
            (
                "sendMessage",
                len(rendered_markdown),
                rendered_markdown[-10:],
                "MarkdownV2",
            ),
        ]

        draft_payload = calls[1][1]
        final_payload = calls[2][1]
        # Exact bodies: no ellipsis was appended and the MarkdownV2-safe
        # boundary trim left the balanced ``*ok*`` pair intact.
        assert draft_payload["text"] == rendered_markdown
        assert final_payload["text"] == rendered_markdown

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

    # it("falls back to a final message when draft streaming updates fail")
    @pytest.mark.asyncio
    async def test_falls_back_to_a_final_message_when_draft_streaming_updates_fail(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [
                ValidationError("telegram", "Bad Request: chat not found"),
                _sample_message(),
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["hello", " world"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"
        assert any(
            call[0] == "Telegram draft streaming update failed" and call[1]["thread_id"] == DM_THREAD_ID
            for call in logger.warn.calls
        )

        # The failed (non-parse-error) initial draft disables draft
        # updates; the stream still persists the final message.
        assert [method for method, _payload in calls] == ["sendMessageDraft", "sendMessage"]

    # it("continues to the final message when markdown draft retry also fails")
    @pytest.mark.asyncio
    async def test_continues_to_the_final_message_when_markdown_draft_retry_also_fails(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [
                True,  # initial empty draft
                _parse_entities_error(),  # markdown draft rejected
                AdapterRateLimitError("telegram", 1),  # plain retry 429s
                _sample_message(text="**broken"),
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

    # it("falls back to plain-text draft and final send when Telegram can't
    #     parse streamed markdown")
    @pytest.mark.asyncio
    async def test_falls_back_to_plaintext_draft_and_final_send_when_telegram_cant_parse_streamed_markdown(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                True,  # initial empty draft
                _parse_entities_error(),  # markdown draft rejected
                True,  # plain retry accepted
                _sample_message(text="**broken"),
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**broken"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"

        retry_draft = calls[2][1]
        final_send = calls[3][1]
        assert calls[2][0] == "sendMessageDraft"
        assert "parse_mode" not in retry_draft
        assert retry_draft["text"] == "**broken"
        assert calls[3][0] == "sendMessage"
        assert final_send.get("parse_mode") is None
        assert final_send["text"] == "**broken"

    # it("reuses original text when streamed plain-text fallback would be empty")
    @pytest.mark.asyncio
    async def test_reuses_original_text_when_streamed_plaintext_fallback_would_be_empty(self):
        adapter = _make_adapter()
        calls = _script_fetch(
            adapter,
            [
                True,  # initial empty draft
                _parse_entities_error(),  # markdown draft rejected
                True,  # plain retry accepted
                _sample_message(text="**"),
            ],
        )

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["**"]), _stream_options(0))

        assert result is not None
        assert result.id == "123:11"

        # Upstream's remark renders ``**`` to an empty plain string, so the
        # fallback resolver reuses the original text. Our parser keeps the
        # unpaired markers literal (``**`` plain-renders as ``**``); both
        # paths converge on shipping the original text verbatim. The
        # empty-fallback branch itself is pinned by
        # test_resolve_telegram_fallback_text_reuses_original_when_fallback_is_blank.
        retry_draft = calls[2][1]
        final_send = calls[3][1]
        assert "parse_mode" not in retry_draft
        assert retry_draft["text"] == "**"
        assert final_send.get("parse_mode") is None
        assert final_send["text"] == "**"

    # Python-only: direct adapter.stream call with options omitted entirely.
    # Upstream can't hit this via Thread.post (thread.ts seeds
    # updateIntervalMs before calling the adapter); our thread.py
    # intentionally does NOT seed it (see docs/UPSTREAM_SYNC.md divergence),
    # so options=None / update_interval_ms=None must resolve to
    # TELEGRAM_DEFAULT_STREAM_UPDATE_INTERVAL_MS instead of crashing.
    @pytest.mark.asyncio
    async def test_stream_defaults_update_interval_when_options_omitted(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [True, True, _sample_message()])

        result = await adapter.stream(DM_THREAD_ID, _text_stream(["hello", " world"]))

        assert result is not None
        assert result.id == "123:11"
        # Initial empty draft first, persisted message last, and nothing
        # but draft updates in between (count depends on wall-clock timing
        # against the 250ms default, so only the shape is asserted).
        assert calls[0][0] == "sendMessageDraft"
        assert calls[0][1]["text"] == ""
        assert calls[-1][0] == "sendMessage"
        assert all(method == "sendMessageDraft" for method, _payload in calls[:-1])

    # Python-only: the empty-stream contract from upstream's stream() body
    # (`if (!accumulated.trim()) throw`) — whitespace-only streams must
    # raise instead of persisting a blank message.
    @pytest.mark.asyncio
    async def test_stream_rejects_whitespace_only_streams(self):
        adapter = _make_adapter()
        calls = _script_fetch(adapter, [True])

        with pytest.raises(ValidationError, match="requires text content"):
            await adapter.stream(DM_THREAD_ID, _text_stream(["  "]), _stream_options(0))

        # Only the initial empty draft went out; whitespace renders to ""
        # which matches the last draft text, so flushes were skipped.
        assert [method for method, _payload in calls] == ["sendMessageDraft"]


# =============================================================================
# Tests -- retry without parse_mode (post_message / edit_message)
# =============================================================================


class TestTelegramMarkdownParseFallback:
    """it() blocks for withTelegramMarkdownFallback on post/edit."""

    # it("retries markdown messages without parse_mode when Telegram can't
    #     parse entities")
    @pytest.mark.asyncio
    async def test_retries_markdown_messages_without_parsemode_when_telegram_cant_parse_entities(self):
        logger = MockLogger()
        adapter = _make_adapter(logger=logger)
        calls = _script_fetch(
            adapter,
            [_parse_entities_error(), _sample_message(text="**broken")],
        )

        result = await adapter.post_message(DM_THREAD_ID, {"markdown": "**broken"})

        assert result.id == "123:11"

        first_send = calls[0][1]
        second_send = calls[1][1]
        # First attempt ships the MarkdownV2 rendering (escaped markers).
        assert first_send["parse_mode"] == "MarkdownV2"
        # Retry drops parse_mode and ships the plain-text rendering.
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

    # Python-only: pins the branch upstream exercises through remark's
    # empty rendering of ``**`` — our parser keeps those markers literal,
    # so the faithful tests above never produce a blank fallback. Without
    # this, the ``fallback_text.strip()`` guard could regress unnoticed.
    def test_resolve_telegram_fallback_text_reuses_original_when_fallback_is_blank(self):
        adapter = _make_adapter()

        assert adapter.resolve_telegram_fallback_text("**original**", "plain") == "plain"
        assert adapter.resolve_telegram_fallback_text("**original**", "") == "**original**"
        assert adapter.resolve_telegram_fallback_text("**original**", "  \n") == "**original**"
