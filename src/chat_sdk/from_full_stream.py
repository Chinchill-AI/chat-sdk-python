"""Normalize async iterable streams for use with ``thread.post()``.

Python port of from-full-stream.ts.

Handles three stream types automatically:

- **Text streams** (``AsyncIterable[str]``) -- passed through as-is.
- **Full streams** (``AsyncIterable[object]``) -- extracts ``text-delta``
  events and injects ``"\\n\\n"`` separators between steps so that
  multi-step agent output reads naturally.
- **StreamChunk objects** (``task_update``, ``plan_update``,
  ``markdown_text``) -- passed through as-is for adapters with native
  structured chunk support.

Python-only divergence (default-off): when ``emit_thinking=True``, AI-SDK
``reasoning`` / ``reasoning-delta`` parts (and pydantic-ai
``part_kind == "thinking"`` parts) are surfaced as
:class:`~chat_sdk.types.ThinkingChunk` objects. With the default
``emit_thinking=False`` the output is byte-for-byte identical to upstream
chat@4.31 (reasoning is dropped, no ``ThinkingChunk`` is emitted). See
``docs/UPSTREAM_SYNC.md`` (Known Non-Parity).
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator

from chat_sdk.types import StreamInput, ThinkingChunk

_STREAM_CHUNK_TYPES = frozenset({"markdown_text", "task_update", "plan_update"})

# AI-SDK v5/v6 reasoning part types, plus pydantic-ai's ``thinking`` part kind.
# These are only consulted when ``emit_thinking=True``; otherwise they fall
# through and are dropped exactly as upstream does.
_REASONING_TYPES = frozenset({"reasoning", "reasoning-delta", "thinking"})

_TEXT_KEYS = ("text", "delta", "textDelta", "text_delta")
# Reasoning payloads carry the text under the same keys, with ``content`` added
# for pydantic-ai's ``ThinkingPart`` shape (``part_kind == "thinking"``).
_REASONING_KEYS = ("content", "text", "delta", "textDelta", "text_delta")


def _pick(event: object, keys: tuple[str, ...]) -> object | None:
    """Return the first non-``None`` value among ``keys`` on a dict/object."""
    if isinstance(event, dict):
        return next((v for k in keys if (v := event.get(k)) is not None), None)
    return next((v for k in keys if (v := getattr(event, k, None)) is not None), None)


async def from_full_stream(
    stream: AsyncIterable[object],
    *,
    emit_thinking: bool = False,
) -> AsyncIterator[StreamInput]:
    """Normalize an async iterable stream for use with ``thread.post()``.

    Yields plain ``str`` chunks, canonical ``StreamChunk`` objects, or — only
    when ``emit_thinking=True`` — the opt-in, Python-only ``ThinkingChunk``.

    Args:
        stream: The source async iterable (text stream, full stream, or
            pre-built ``StreamChunk`` objects).
        emit_thinking: **Opt-in, default off.** When ``False`` (the default),
            behavior is byte-for-byte upstream: AI-SDK ``reasoning`` /
            ``reasoning-delta`` parts are dropped and **no**
            :class:`~chat_sdk.types.ThinkingChunk` is emitted. When ``True``,
            such parts (and pydantic-ai ``thinking`` parts) are surfaced as
            ``ThinkingChunk`` objects so a consumer/adapter can render agent
            reasoning. Thinking is never accumulated into the posted message
            text, so the posted message is unchanged either way.
    """
    needs_separator = False
    has_emitted_text = False

    async for event in stream:
        # Plain string chunk (e.g. from AI SDK textStream)
        if isinstance(event, str):
            yield event
            continue

        if event is None:
            continue

        # Support both dict and object-style events
        if isinstance(event, dict):
            event_type = event.get("type", "")
        elif hasattr(event, "type"):
            event_type = getattr(event, "type", "")
        else:
            continue

        if not event_type:
            continue

        # Pass through canonical StreamChunk objects. (Pre-built ThinkingChunk
        # has ``type == "thinking"`` and is handled by the reasoning branch
        # below, gated on ``emit_thinking``.)
        if event_type in _STREAM_CHUNK_TYPES:
            yield event  # type: ignore[misc]
            continue

        # Opt-in reasoning surfacing. Default-off => this whole branch is
        # skipped and reasoning parts fall through (dropped) exactly as
        # upstream chat@4.31 does.
        if event_type in _REASONING_TYPES:
            if emit_thinking:
                content = _pick(event, _REASONING_KEYS)
                if isinstance(content, str) and content:
                    yield ThinkingChunk(content=content)
            continue

        # AI SDK v6 uses "text", v5 uses "textDelta"; also accept "delta"
        # Priority: text > delta > textDelta > text_delta (matches TS)
        text_content = _pick(event, _TEXT_KEYS)

        if event_type == "text-delta" and isinstance(text_content, str):
            if needs_separator and has_emitted_text:
                yield "\n\n"
            needs_separator = False
            has_emitted_text = True
            yield text_content
        elif event_type == "finish-step":
            needs_separator = True
