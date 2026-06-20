"""Integration tests for the Python-only ``ThinkingChunk`` divergence.

Covers the cross-cutting guarantees of the opt-in thinking-stream feature:

- ``Thread._handle_stream`` gracefully *skips* a ``ThinkingChunk`` so the
  posted message is byte-identical with or without thinking in the stream.
- The thread-level ``emit_thinking`` flag (default-off) threads through to
  ``_from_full_stream`` so a raw AI-SDK ``reasoning`` part becomes a
  ``ThinkingChunk`` only when enabled.
- The persisted ``Message`` round-trips byte-identically — no thinking ever
  reaches history/state.
- Every adapter's stream handler ignores a ``ThinkingChunk`` without crashing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from chat_sdk.testing import create_mock_adapter, create_mock_state
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig, _to_message
from chat_sdk.types import (
    Author,
    MarkdownTextChunk,
    Message,
    MessageMetadata,
    PostableMarkdown,
    RawMessage,
    ThinkingChunk,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(*, emit_thinking: bool = False, adapter: Any = None) -> ThreadImpl:
    return ThreadImpl(
        _ThreadImplConfig(
            id="slack:C123:1234.5678",
            adapter=adapter or create_mock_adapter(),
            state_adapter=create_mock_state(),
            channel_id="C123",
            emit_thinking=emit_thinking,
            # Edit on every chunk so the fallback path's accumulated text is
            # fully exercised.
            streaming_update_interval_ms=0,
        )
    )


async def _stream(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# _handle_stream graceful skip (fallback post+edit path)
# ---------------------------------------------------------------------------


class TestHandleStreamGracefulSkip:
    @pytest.mark.asyncio
    async def test_posted_message_identical_with_and_without_thinking(self):
        # Same text chunks, one stream interleaved with ThinkingChunks.
        thread_plain = _make_thread()
        plain = await thread_plain.post(_stream(["Hello ", "world"]))

        thread_thinking = _make_thread()
        with_thinking = await thread_thinking.post(
            _stream(
                [
                    ThinkingChunk(content="reasoning A"),
                    "Hello ",
                    ThinkingChunk(content="reasoning B"),
                    "world",
                ]
            )
        )

        assert plain.text == with_thinking.text == "Hello world"

    @pytest.mark.asyncio
    async def test_thinking_chunk_does_not_appear_in_posted_text(self):
        thread = _make_thread()
        sent = await thread.post(_stream([ThinkingChunk(content="SECRET-THOUGHT"), "visible"]))
        assert "SECRET-THOUGHT" not in sent.text
        assert sent.text == "visible"

    @pytest.mark.asyncio
    async def test_thinking_only_stream_posts_no_thinking_text(self):
        thread = _make_thread()
        sent = await thread.post(_stream([ThinkingChunk(content="just thinking")]))
        assert "just thinking" not in sent.text


# ---------------------------------------------------------------------------
# emit_thinking flag threading through _handle_stream -> adapter.stream
# ---------------------------------------------------------------------------


class TestEmitThinkingThreading:
    @pytest.mark.asyncio
    async def test_default_off_drops_raw_reasoning_before_adapter(self):
        # A native-stream adapter records every chunk it receives. With the
        # default-off flag, raw reasoning parts must NOT reach it.
        received: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            async for chunk in text_stream:
                received.append(chunk)
            return RawMessage(id="m1", thread_id=thread_id, raw={})

        adapter = create_mock_adapter()
        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter=adapter)  # emit_thinking defaults to False
        await thread.post(
            _stream(
                [
                    {"type": "reasoning", "text": "hidden"},
                    {"type": "text-delta", "text": "answer"},
                ]
            )
        )

        assert all(not isinstance(c, ThinkingChunk) for c in received)
        assert "answer" in received

    @pytest.mark.asyncio
    async def test_opt_in_surfaces_thinking_chunk_to_adapter(self):
        received: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            async for chunk in text_stream:
                received.append(chunk)
            return RawMessage(id="m1", thread_id=thread_id, raw={})

        adapter = create_mock_adapter()
        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter=adapter, emit_thinking=True)
        sent = await thread.post(
            _stream(
                [
                    {"type": "reasoning", "text": "thinking"},
                    {"type": "text-delta", "text": "answer"},
                ]
            )
        )

        thinking = [c for c in received if isinstance(c, ThinkingChunk)]
        assert len(thinking) == 1
        assert thinking[0].content == "thinking"
        # Even with thinking surfaced, the posted message text excludes it.
        assert sent.text == "answer"

    @pytest.mark.asyncio
    async def test_opt_in_passes_prebuilt_thinking_chunk_through(self):
        received: list[Any] = []

        async def mock_stream(thread_id: str, text_stream: Any, options: Any = None) -> RawMessage:
            async for chunk in text_stream:
                received.append(chunk)
            return RawMessage(id="m1", thread_id=thread_id, raw={})

        adapter = create_mock_adapter()
        adapter.stream = mock_stream  # type: ignore[attr-defined]

        thread = _make_thread(adapter=adapter, emit_thinking=True)
        await thread.post(_stream([ThinkingChunk(content="pre"), "text"]))

        assert any(isinstance(c, ThinkingChunk) and c.content == "pre" for c in received)


# ---------------------------------------------------------------------------
# No state pollution: persisted Message round-trips byte-identically
# ---------------------------------------------------------------------------


def _make_message(text: str) -> Message:
    return Message(
        id="m1",
        thread_id="slack:C123:1234.5678",
        text=text,
        formatted={"type": "root", "children": []},
        author=Author(user_id="U1", user_name="a", full_name="A", is_bot=True, is_me=True),
        metadata=MessageMetadata(date_sent=datetime(2024, 1, 1, tzinfo=timezone.utc), edited=False),
    )


class TestNoStatePollution:
    def test_message_has_no_thinking_field(self):
        msg = _make_message("hi")
        assert not hasattr(msg, "thinking")
        assert "thinking" not in msg.to_json()

    def test_message_roundtrip_byte_identical(self):
        msg = _make_message("response body")
        serialized = msg.to_json()
        restored = Message.from_json(serialized)
        # Re-serialization must be byte-identical: thinking never enters state.
        assert restored.to_json() == serialized
        assert "thinking" not in restored.to_json()

    @pytest.mark.asyncio
    async def test_streamed_message_persisted_without_thinking(self):
        state = create_mock_state()
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="slack:C123:1234.5678",
                adapter=create_mock_adapter(),
                state_adapter=state,
                channel_id="C123",
                streaming_update_interval_ms=0,
            )
        )
        sent = await thread.post(_stream([ThinkingChunk(content="SECRET"), "posted text"]))
        persisted = _to_message(sent)
        assert "SECRET" not in persisted.to_json()
        assert persisted.text == "posted text"


# ---------------------------------------------------------------------------
# from_full_stream pre-built ThinkingChunk passthrough symmetry
# ---------------------------------------------------------------------------


class TestStreamChunkUnion:
    def test_markdown_and_thinking_share_no_discriminant(self):
        assert MarkdownTextChunk().type == "markdown_text"
        assert ThinkingChunk().type == "thinking"

    @pytest.mark.asyncio
    async def test_postable_markdown_unaffected_by_thinking(self):
        # Sanity: posting a normal PostableMarkdown is unchanged.
        thread = _make_thread()
        sent = await thread.post(PostableMarkdown(markdown="plain message"))
        assert sent.text == "plain message"


# ---------------------------------------------------------------------------
# Every adapter's text-accumulate stream handler ignores a ThinkingChunk
# without crashing. (Slack/Teams native-stream paths + Twilio/Messenger are
# covered in their own adapter test modules.)
# ---------------------------------------------------------------------------


def _build_text_accumulate_adapters() -> list[tuple[str, Any, str]]:
    """Construct the text-accumulate adapters with their encoded thread IDs.

    Returns ``(name, adapter, thread_id)`` triples. Each adapter's ``stream``
    accumulates only ``str`` / ``markdown_text`` text and posts via
    ``post_message`` / ``edit_message``, which we stub per-adapter so no
    network call is made.
    """
    from chat_sdk.logger import ConsoleLogger

    triples: list[tuple[str, Any, str]] = []

    # Discord
    from chat_sdk.adapters.discord.adapter import DiscordAdapter
    from chat_sdk.adapters.discord.types import DiscordAdapterConfig, DiscordThreadId

    discord = DiscordAdapter(DiscordAdapterConfig(bot_token="t", public_key="a" * 64, application_id="app"))
    triples.append(("discord", discord, discord.encode_thread_id(DiscordThreadId(guild_id="g", channel_id="c"))))

    # WhatsApp
    from chat_sdk.adapters.whatsapp.adapter import WhatsAppAdapter
    from chat_sdk.adapters.whatsapp.types import WhatsAppAdapterConfig, WhatsAppThreadId

    whatsapp = WhatsAppAdapter(
        WhatsAppAdapterConfig(
            access_token="t",
            app_secret="s",
            phone_number_id="111",
            verify_token="v",
            user_name="bot",
            logger=ConsoleLogger("error"),
        )
    )
    triples.append(
        ("whatsapp", whatsapp, whatsapp.encode_thread_id(WhatsAppThreadId(phone_number_id="111", user_wa_id="222")))
    )

    # GitHub
    from chat_sdk.adapters.github.adapter import GitHubAdapter
    from chat_sdk.adapters.github.types import GitHubThreadId

    github = GitHubAdapter({"webhook_secret": "s", "token": "ghp_t", "logger": ConsoleLogger("error")})
    triples.append(("github", github, github.encode_thread_id(GitHubThreadId(owner="o", repo="r", pr_number=1))))

    # Linear
    from chat_sdk.adapters.linear.adapter import LinearAdapter
    from chat_sdk.adapters.linear.types import LinearAdapterAPIKeyConfig, LinearThreadId

    linear = LinearAdapter(
        LinearAdapterAPIKeyConfig(api_key="k", webhook_secret="s", user_name="bot", logger=ConsoleLogger("error"))
    )
    triples.append(("linear", linear, linear.encode_thread_id(LinearThreadId(issue_id="abc123-def456-789"))))

    # Stub the network-touching post/edit on each adapter.
    for _name, adapter, _tid in triples:
        captured: dict[str, str] = {"text": ""}

        async def _post(thread_id: str, message: Any, _cap: dict[str, str] = captured) -> RawMessage:
            _cap["text"] = getattr(message, "markdown", None) or getattr(message, "raw", "") or ""
            return RawMessage(id="posted", thread_id=thread_id, raw={})

        async def _edit(thread_id: str, message_id: str, message: Any, _cap: dict[str, str] = captured) -> RawMessage:
            _cap["text"] = getattr(message, "markdown", None) or getattr(message, "raw", "") or ""
            return RawMessage(id=message_id, thread_id=thread_id, raw={})

        adapter.post_message = _post  # type: ignore[attr-defined]
        adapter.edit_message = _edit  # type: ignore[attr-defined]
        adapter._captured = captured  # type: ignore[attr-defined]

    return triples


@pytest.mark.parametrize("name,adapter,thread_id", _build_text_accumulate_adapters())
@pytest.mark.asyncio
async def test_adapter_stream_ignores_thinking_chunk(name: str, adapter: Any, thread_id: str) -> None:
    """Each text-accumulate adapter's ``stream`` skips a ``ThinkingChunk``
    without crashing, and the posted text contains only the text chunks."""

    async def gen() -> AsyncIterator[Any]:
        yield ThinkingChunk(content="reasoning")
        yield "Hello "
        yield ThinkingChunk(content="more reasoning")
        yield "world"

    # Must not raise.
    await adapter.stream(thread_id, gen())
    posted = adapter._captured["text"]
    assert "reasoning" not in posted, f"{name}: thinking leaked into posted text"
    assert posted == "Hello world", f"{name}: unexpected posted text {posted!r}"
