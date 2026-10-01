"""Normalize async iterable streams for use with ``thread.post()``.

Python port of from-full-stream.ts.

Handles these stream types automatically:

- **Text streams** (``AsyncIterable[str]``) -- passed through as-is.
- **AI SDK full streams** (``AsyncIterable[object]``) -- extracts
  ``text-delta`` events and injects ``"\\n\\n"`` separators after each
  ``finish-step`` so that multi-step agent output reads naturally.
- **AG-UI streams** (e.g. TanStack AI ``chat()``, or the Python
  ``ag-ui-protocol`` event models) -- extracts ``TEXT_MESSAGE_CONTENT``
  deltas and injects ``"\\n\\n"`` separators after each
  ``TEXT_MESSAGE_END``, since every model turn in a tool loop is its own
  text message. Tool-call, reasoning, state and lifecycle events are skipped.
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

from chat_sdk._compat import aclose_quietly
from chat_sdk.types import StreamInput, ThinkingChunk

_STREAM_CHUNK_TYPES = frozenset({"markdown_text", "task_update", "plan_update"})

# Text-carrying events: AI SDK ``text-delta`` and AG-UI ``TEXT_MESSAGE_CONTENT``
# (vercel/chat#934). Matching on the type (not on the presence of ``delta``)
# keeps AG-UI ``TOOL_CALL_ARGS`` / ``STATE_DELTA`` deltas out of the text.
_TEXT_DELTA_TYPES = frozenset({"text-delta", "TEXT_MESSAGE_CONTENT"})
# Step boundaries that arm the ``"\n\n"`` separator before the next text.
_STEP_END_TYPES = frozenset({"finish-step", "TEXT_MESSAGE_END"})

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

    # Upstream's ``for await`` calls the source's ``return()`` when the
    # consumer stops early; Python's ``async for`` does not, so close the
    # source when this generator exits before exhausting it (e.g. a turn
    # aborted between chunks). Nothing is closed after normal exhaustion.
    iterator = aiter(stream)
    exhausted = False
    try:
        async for event in iterator:
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

            # Python AG-UI producers (``ag-ui-protocol``, pydantic-ai) type events
            # with a ``str`` Enum; compare on its value.
            event_type = getattr(event_type, "value", event_type)
            if not event_type or not isinstance(event_type, str):
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

            # AI SDK v6 uses "text", v5 uses "textDelta"; AG-UI uses "delta".
            # Priority: text > delta > textDelta > text_delta (matches TS)
            text_content = _pick(event, _TEXT_KEYS)

            if event_type in _TEXT_DELTA_TYPES and isinstance(text_content, str):
                if needs_separator and has_emitted_text:
                    yield "\n\n"
                needs_separator = False
                has_emitted_text = True
                yield text_content
            elif event_type in _STEP_END_TYPES:
                needs_separator = True
        exhausted = True
    finally:
        if not exhausted:
            await aclose_quietly(iterator)
