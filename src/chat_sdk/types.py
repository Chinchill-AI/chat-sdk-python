"""Core types for chat-sdk.

Python port of Vercel Chat SDK types.ts.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Literal,
    Protocol,
    TypedDict,
    runtime_checkable,
)

from chat_sdk.cards import CardElement
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import Logger, LogLevel
from chat_sdk.modals import OptionsLoadGroup, SelectOptionElement

# A handler may return either a flat list of options or a list of labeled
# groups (Slack's ``option_groups`` shape). Mirrors upstream TS
# ``OptionsLoadResult = SelectOptionElement[] | OptionsLoadGroup[]``.
OptionsLoadResult = list[SelectOptionElement] | list[OptionsLoadGroup]


def _parse_iso(s: str) -> datetime:
    """Parse ISO 8601 datetime string, supporting Python 3.10+.

    Handles two Python 3.10 limitations:
    - ``Z`` suffix not accepted (replaced with ``+00:00``)
    - Fractional seconds limited to 6 digits (truncated from 7+)
    """
    import re

    s = s.replace("Z", "+00:00")
    # Python 3.10 only supports up to 6 fractional digits (microseconds).
    # Truncate any extra digits (e.g., Teams sends 7-digit nanosecond timestamps).
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    return datetime.fromisoformat(s)


# =============================================================================
# Channel Visibility
# =============================================================================

ChannelVisibility = Literal["private", "workspace", "external", "unknown"]

# =============================================================================
# FormattedContent (mdast Root equivalent)
# =============================================================================

# In TS this is mdast Root; in Python we use a dict representation
FormattedContent = dict[str, Any]

# =============================================================================
# Configuration
# =============================================================================

LockScope = Literal["thread", "channel"]
ConcurrencyStrategy = Literal["drop", "queue", "debounce", "burst", "concurrent"]
OnLockConflict = (
    Literal["drop", "force"]
    | Callable[..., Awaitable[Literal["force", "drop"] | bool] | Literal["force", "drop"] | bool]
)
FetchDirection = Literal["forward", "backward"]

# Well-known emoji names
WellKnownEmoji = Literal[
    "thumbs_up",
    "thumbs_down",
    "clap",
    "wave",
    "pray",
    "muscle",
    "ok_hand",
    "point_up",
    "point_down",
    "point_left",
    "point_right",
    "raised_hands",
    "shrug",
    "facepalm",
    "heart",
    "smile",
    "laugh",
    "thinking",
    "sad",
    "cry",
    "angry",
    "love_eyes",
    "cool",
    "wink",
    "surprised",
    "worried",
    "confused",
    "neutral",
    "sleeping",
    "sick",
    "mind_blown",
    "relieved",
    "grimace",
    "rolling_eyes",
    "hug",
    "zany",
    "check",
    "x",
    "question",
    "exclamation",
    "warning",
    "stop",
    "info",
    "100",
    "no_entry",
    "green_circle",
    "yellow_circle",
    "red_circle",
    "blue_circle",
    "white_circle",
    "black_circle",
    "rocket",
    "party",
    "confetti",
    "balloon",
    "gift",
    "trophy",
    "medal",
    "star",
    "sparkles",
    "fire",
    "lightning",
    "bulb",
    "lightbulb",
    "gear",
    "wrench",
    "hammer",
    "link",
    "lock",
    "unlock",
    "key",
    "pin",
    "bell",
    "megaphone",
    "loudspeaker",
    "clipboard",
    "memo",
    "book",
    "calendar",
    "clock",
    "hourglass",
    "mag",
    "chart",
    "bar_chart",
    "folder",
    "file",
    "package",
    "email",
    "inbox",
    "outbox",
    "coffee",
    "pizza",
    "beer",
    "arrow_up",
    "arrow_down",
    "arrow_left",
    "arrow_right",
    "arrow_up_right",
    "arrow_down_right",
    "arrow_right_hook",
    "arrows_counterclockwise",
    "sun",
    "cloud",
    "rain",
    "snow",
    "rainbow",
]


@dataclass(frozen=True)
class EmojiValue:
    """Immutable emoji value with identity comparison."""

    name: str

    def __str__(self) -> str:
        return f"{{{{emoji:{self.name}}}}}"

    def to_json(self) -> str:
        return f"{{{{emoji:{self.name}}}}}"


@dataclass
class EmojiFormats:
    """Platform-specific emoji formats."""

    slack: str | list[str] = ""
    gchat: str | list[str] = ""


# =============================================================================
# Concurrency
# =============================================================================


@dataclass
class LockScopeContext:
    """Context provided to the lockScope resolver function."""

    adapter: Adapter
    channel_id: str
    is_dm: bool
    thread_id: str


@dataclass
class ConcurrencyConfig:
    """Fine-grained concurrency configuration."""

    strategy: ConcurrencyStrategy
    # Debounce window in milliseconds (debounce/burst strategies). Default: 1500.
    debounce_ms: int = 1500
    max_concurrent: int | None = None  # None = Infinity
    # Max queued messages per thread (queue/burst strategies). Default: 10.
    max_queue_size: int = 10
    on_queue_full: Literal["drop-oldest", "drop-newest"] = "drop-oldest"
    queue_entry_ttl_ms: int = 90000


# =============================================================================
# Message Types
# =============================================================================


@dataclass
class Author:
    """Message author information."""

    full_name: str
    is_bot: bool | Literal["unknown"]
    is_me: bool
    user_id: str
    user_name: str


@dataclass
class UserInfo:
    """User information returned by :meth:`Adapter.get_user`.

    Mirrors upstream ``UserInfo`` (``packages/chat/src/types.ts``).  Fields
    that aren't universally available across platforms (``email``,
    ``avatar_url``) are optional.
    """

    full_name: str
    is_bot: bool
    user_id: str
    user_name: str
    avatar_url: str | None = None
    email: str | None = None


@dataclass
class MessageMetadata:
    """Message metadata."""

    date_sent: datetime
    edited: bool = False
    edited_at: datetime | None = None


@dataclass
class Attachment:
    """File attachment.

    ``fetch_metadata`` is a serializable dict of adapter-specific identifiers
    (e.g. Slack URL + team ID, Telegram file_id, WhatsApp media_id) that
    survives JSON roundtrips and lets
    :meth:`Adapter.rehydrate_attachment` rebuild the ``fetch_data`` download
    closure after the queue/debounce path drops callables during
    serialization.
    """

    type: Literal["image", "file", "video", "audio"]
    url: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    data: bytes | None = None
    fetch_data: Callable[[], Awaitable[bytes]] | None = None
    fetch_metadata: dict[str, str] | None = None


@dataclass
class LinkPreview:
    """Link preview data."""

    url: str
    title: str | None = None
    description: str | None = None
    image_url: str | None = None
    site_name: str | None = None
    fetch_message: Callable[[], Awaitable[Message]] | None = None


@dataclass
class FileUpload:
    """File upload data."""

    data: bytes
    filename: str
    mime_type: str | None = None


class SerializedMessageAuthor(TypedDict):
    """Serialized author data."""

    user_id: str
    user_name: str
    full_name: str
    is_bot: bool | Literal["unknown"]
    is_me: bool


class SerializedMessageMetadata(TypedDict, total=False):
    """Serialized message metadata."""

    date_sent: str  # ISO string
    edited: bool
    edited_at: str  # ISO string


class SerializedAttachment(TypedDict, total=False):
    """Serialized attachment (non-serializable fields omitted).

    ``fetch_metadata`` is preserved so ``Adapter.rehydrate_attachment`` can
    reconstruct the download closure after a JSON roundtrip through the
    state adapter (e.g. queue/debounce concurrency).
    """

    type: Literal["image", "file", "video", "audio"]
    url: str
    name: str
    mime_type: str
    size: int
    width: int
    height: int
    fetch_metadata: dict[str, str]


class SerializedLinkPreview(TypedDict, total=False):
    """Serialized link preview."""

    url: str
    title: str
    description: str
    image_url: str
    site_name: str


class _SerializedMessageRequired(TypedDict):
    """Required fields of a serialized message."""

    _type: str  # "chat:Message"
    id: str
    thread_id: str
    text: str
    author: SerializedMessageAuthor
    metadata: SerializedMessageMetadata
    attachments: list[SerializedAttachment]


class SerializedMessage(_SerializedMessageRequired, total=False):
    """Serialized message data for passing to external systems.

    Dates are converted to ISO strings, and non-serializable fields
    (``fetch_data``, ``fetch_message``) are omitted.
    """

    formatted: FormattedContent
    raw: Any
    is_mention: bool
    links: list[SerializedLinkPreview]


@dataclass
class MessageSubjectParty:
    """A person referenced by a :class:`MessageSubject` (assignee/author).

    Mirrors the inline ``{ id: string; name: string }`` shape used by
    upstream's ``MessageSubject.assignee`` / ``MessageSubject.author``.
    """

    id: str
    name: str


@dataclass
class MessageSubject:
    """The external subject a message refers to (e.g. a Linear issue or GitHub PR).

    Python port of the TS ``MessageSubject`` interface
    (``packages/chat/src/types.ts``). Resolved lazily via
    :attr:`Message.subject`, which delegates to the owning adapter's
    optional :meth:`Adapter.fetch_subject` hook.

    Field names are snake_case per the Python port convention; ``raw`` is
    the platform-specific escape hatch.
    """

    # ``id`` and ``type`` are the only required fields upstream; everything
    # else is optional. ``raw`` is required upstream but defaults to ``None``
    # here so partially-populated subjects (e.g. in tests) construct cleanly.
    id: str
    type: str
    raw: Any = None
    assignee: MessageSubjectParty | None = None
    author: MessageSubjectParty | None = None
    description: str | None = None
    labels: list[str] | None = None
    status: str | None = None
    title: str | None = None
    url: str | None = None


# --------------------------------------------------------------------------
# Message -> Adapter registry (powers ``Message.subject``)
# --------------------------------------------------------------------------
#
# Upstream (``packages/chat/src/message.ts``) uses
# ``const adapterMap = new WeakMap<Message, Adapter>()`` so a dispatched
# message can lazily ask its owning adapter to resolve its subject, without
# the message holding a hard reference to the adapter and without leaking
# messages after they fall out of scope.
#
# Python port hazard — hashability/weakref:
#   ``Message`` is a plain ``@dataclass`` (``eq=True``), which makes instances
#   *unhashable*. A ``weakref.WeakKeyDictionary[Message, Adapter]`` therefore
#   raises ``TypeError: unhashable type: 'Message'``. We deliberately do NOT
#   change ``Message`` to ``eq=False``/``frozen=True`` (that would alter its
#   public equality contract). Instead we key a plain ``dict`` by
#   ``id(message)`` (object identity, matching ``WeakMap`` semantics) and
#   register a ``weakref.finalize`` callback per message that pops the entry
#   when the message is garbage-collected. ``weakref.ref(message)`` works on a
#   plain dataclass even though ``hash()`` does not, so this is safe. The
#   finalizer also closes the ``id()`` reuse hole: the entry is removed before
#   CPython can recycle the id for a new object.
_message_adapter_map: dict[int, Adapter] = {}


def set_message_adapter(message: Message, adapter: Adapter) -> None:
    """Register the adapter that owns ``message`` (powers ``message.subject``).

    Called by :class:`~chat_sdk.chat.Chat` at the dispatch bind site so every
    message handed to a handler can resolve its subject via the adapter's
    optional :meth:`Adapter.fetch_subject` hook.

    Mirrors upstream ``setMessageAdapter`` (``packages/chat/src/message.ts``).
    The mapping is keyed by object identity and weakly scoped: when ``message``
    is garbage-collected, its entry is removed automatically.
    """
    key = id(message)
    already_registered = key in _message_adapter_map
    _message_adapter_map[key] = adapter

    # Register the GC finalizer only on first registration for a given message
    # identity; re-registering the same live message just overwrites the adapter
    # value above. This prevents an accumulation of redundant finalizers when a
    # message is registered more than once (re-dispatch, rehydrate, multiple
    # handler passes). The ``id()``-reuse hole stays closed: if a prior message
    # with the same id was GC'd, its finalizer already popped the entry, so
    # ``key not in _message_adapter_map`` is true again and a fresh finalizer is
    # registered for the new object.
    if not already_registered:
        # Drop the entry when the message is GC'd. A zero-arg closure (rather
        # than ``weakref.finalize(message, dict.pop, key, None)``) captures
        # ``key`` and keeps the finalizer callable's type unambiguous for the
        # type-checker. ``pop(key, None)`` is a no-op if the entry was already
        # removed.
        def _cleanup() -> None:
            _message_adapter_map.pop(key, None)

        weakref.finalize(message, _cleanup)


def _get_message_adapter(message: Message) -> Adapter | None:
    """Return the adapter registered for ``message``, or ``None``."""
    return _message_adapter_map.get(id(message))


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    """Remove keys whose value is ``None`` from a dict.

    Used by :meth:`Message.to_json` so serialized attachment and link
    sub-dicts only contain keys that have values, matching the TS
    ``SerializedAttachment`` / ``SerializedLinkPreview`` shapes.
    """
    return {k: v for k, v in d.items() if v is not None}


@dataclass
class Message:
    """Normalized chat message."""

    id: str
    thread_id: str
    text: str
    formatted: FormattedContent
    author: Author
    metadata: MessageMetadata
    attachments: list[Attachment] = field(default_factory=list)
    is_mention: bool | None = None
    links: list[LinkPreview] | None = None
    raw: Any = None
    # Cross-platform user key for this message's author.
    #
    # Set by the Chat SDK before passing the message to handlers, when
    # ``ChatConfig.identity`` is configured.  ``None`` if no resolver is
    # configured or when the resolver returned ``None``.
    #
    # Used by the Transcripts API to look up / append per-user transcripts.
    # Not part of the serialized ``SerializedMessage`` shape.
    user_key: str | None = None

    # Cached awaitable for ``subject``. Mirrors upstream's ``_subjectPromise``:
    # the first ``await message.subject`` stores the in-flight future here so a
    # second access reuses it instead of re-calling ``fetch_subject``.
    # ``init=False``/``compare=False``/``repr=False`` keep it out of the
    # dataclass ``__init__``, equality, and ``repr`` — it is purely internal
    # resolution state, not message data.
    _subject_future: Any = field(default=None, init=False, compare=False, repr=False)

    async def _resolve_subject(self) -> MessageSubject | None:
        """Resolve the subject via the owning adapter's ``fetch_subject`` hook.

        Returns ``None`` when no adapter is registered, the adapter has no
        ``fetch_subject`` hook, the hook returns ``None``, or the hook raises
        (failures are swallowed, mirroring upstream's ``.catch(() => null)``).
        """
        adapter = _get_message_adapter(self)
        if adapter is None:
            return None
        fetch_subject = getattr(adapter, "fetch_subject", None)
        if fetch_subject is None:
            return None
        try:
            return await fetch_subject(self.raw)
        except Exception:
            return None

    async def _subject(self) -> MessageSubject | None:
        """Coroutine backing the :attr:`subject` accessor (caches the result).

        The first await schedules ``_resolve_subject`` once via
        ``ensure_future`` and stores the shared future on the instance; every
        later/concurrent await reuses it, so ``fetch_subject`` runs at most
        once. Mirrors upstream's cached ``_subjectPromise``.

        The cached future is awaited through :func:`asyncio.shield` so that a
        caller cancellation (e.g. ``asyncio.wait_for(msg.subject, timeout=...)``
        firing) propagates ``CancelledError`` to the caller but does *not*
        cancel the shared inner task. Without shielding, the first cancelled
        awaiter would poison the cache and every subsequent ``await
        msg.subject`` would raise ``CancelledError``.
        """
        if self._subject_future is None:
            self._subject_future = asyncio.ensure_future(self._resolve_subject())
        return await asyncio.shield(self._subject_future)

    @property
    def subject(self) -> Awaitable[MessageSubject | None]:
        """The external subject this message refers to (issue, PR, etc.), or ``None``.

        Lazily resolved via the owning adapter's optional
        :meth:`Adapter.fetch_subject` hook. The adapter is registered at
        dispatch time by :func:`set_message_adapter`.

        Mirrors upstream ``Message.subject`` (``packages/chat/src/message.ts``):
        it is an awaitable, the result is cached after the first access, and a
        second ``await message.subject`` does NOT re-call ``fetch_subject``.
        Concurrent awaits share a single in-flight resolution.

        Usage::

            subject = await message.subject
            if subject is not None:
                ...
        """
        return self._subject()

    def to_json(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict.

        Returns a ``SerializedMessage``-shaped dictionary.  Non-serializable
        fields (attachment ``data``/``fetch_data``, link ``fetch_message``)
        are omitted.  ``None`` values are stripped from attachment and link
        sub-dicts so the output matches the TS ``SerializedMessage`` shape
        (where those keys are optional, not ``null``).
        """
        metadata: dict[str, Any] = {
            "dateSent": self.metadata.date_sent.isoformat(),
            "edited": self.metadata.edited,
        }
        if self.metadata.edited_at is not None:
            metadata["editedAt"] = self.metadata.edited_at.isoformat()

        result: dict[str, Any] = {
            "_type": "chat:Message",
            "id": self.id,
            "threadId": self.thread_id,
            "text": self.text,
            "formatted": self.formatted,
            "raw": self.raw,
            "author": {
                "userId": self.author.user_id,
                "userName": self.author.user_name,
                "fullName": self.author.full_name,
                "isBot": self.author.is_bot,
                "isMe": self.author.is_me,
            },
            "metadata": metadata,
            "attachments": [
                _strip_none(
                    {
                        "type": att.type,
                        "url": att.url,
                        "name": att.name,
                        "mimeType": att.mime_type,
                        "size": att.size,
                        "width": att.width,
                        "height": att.height,
                        # ``fetchMetadata`` carries adapter-specific identifiers
                        # (URL, team id, file id, etc.) used by
                        # ``Adapter.rehydrate_attachment`` to rebuild the
                        # download closure after a JSON roundtrip.
                        "fetchMetadata": att.fetch_metadata,
                    }
                )
                for att in self.attachments
            ],
        }
        if self.is_mention is not None:
            result["isMention"] = self.is_mention
        if self.links:
            result["links"] = [
                _strip_none(
                    {
                        "url": link.url,
                        "title": link.title,
                        "description": link.description,
                        "imageUrl": link.image_url,
                        "siteName": link.site_name,
                    }
                )
                for link in self.links
            ]
        return result

    @classmethod
    def from_json(cls, data: dict[str, Any] | Message) -> Message:
        """Reconstruct a Message from serialized JSON data.

        Converts ISO date strings back to ``datetime`` objects.
        Accepts both camelCase (canonical output of ``to_json()``) and
        snake_case keys for backward compatibility.  For explicit
        dual-format handling see :meth:`from_json_compat`.

        Idempotent: if ``data`` is already a :class:`Message`, it is
        returned unchanged. This makes it safe to call via
        ``json.loads(..., object_hook=reviver)``, where nested values are
        revived bottom-up and the outer dict may already contain a revived
        instance.
        """
        if isinstance(data, Message):
            return data
        meta = data.get("metadata", {})

        date_sent_raw = meta.get("dateSent") or meta.get("date_sent")
        date_sent = (
            _parse_iso(date_sent_raw)
            if isinstance(date_sent_raw, str)
            else (date_sent_raw if isinstance(date_sent_raw, datetime) else datetime.now())
        )
        edited_at_raw = meta.get("editedAt") or meta.get("edited_at")
        edited_at: datetime | None = (
            _parse_iso(edited_at_raw)
            if isinstance(edited_at_raw, str)
            else (edited_at_raw if isinstance(edited_at_raw, datetime) else None)
        )

        author_data = data.get("author", {})
        attachments_data = data.get("attachments", [])
        links_data = data.get("links")

        return cls(
            id=data.get("id", ""),
            thread_id=data.get("threadId") or data.get("thread_id", ""),
            text=data.get("text", ""),
            formatted=data.get("formatted", {"type": "root", "children": []}),
            raw=data.get("raw"),
            author=Author(
                user_id=author_data.get("userId") or author_data.get("user_id", ""),
                user_name=author_data.get("userName") or author_data.get("user_name", ""),
                full_name=author_data.get("fullName") or author_data.get("full_name", ""),
                is_bot=author_data.get("isBot") if "isBot" in author_data else author_data.get("is_bot", False),
                is_me=author_data.get("isMe") if "isMe" in author_data else author_data.get("is_me", False),
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=meta.get("edited", False),
                edited_at=edited_at,
            ),
            attachments=[
                Attachment(
                    type=att.get("type", "file"),
                    url=att.get("url"),
                    name=att.get("name"),
                    mime_type=(att.get("mimeType") if att.get("mimeType") is not None else att.get("mime_type")),
                    size=att.get("size"),
                    width=att.get("width"),
                    height=att.get("height"),
                    # ``data`` is not part of the ``SerializedAttachment`` wire
                    # shape (``to_json`` drops bytes — JSON can't carry them).
                    # We still accept it here so callers handing a raw dict
                    # that happens to carry pre-fetched bytes (e.g. in-memory
                    # state backends) don't silently lose the payload.
                    data=att.get("data"),
                    fetch_metadata=(
                        att.get("fetchMetadata") if att.get("fetchMetadata") is not None else att.get("fetch_metadata")
                    ),
                )
                for att in attachments_data
            ],
            is_mention=data.get("isMention") if "isMention" in data else data.get("is_mention"),
            links=[
                LinkPreview(
                    url=lp.get("url", ""),
                    title=lp.get("title"),
                    description=lp.get("description"),
                    image_url=lp.get("imageUrl") or lp.get("image_url"),
                    site_name=lp.get("siteName") or lp.get("site_name"),
                )
                for lp in links_data
            ]
            if links_data
            else None,
        )

    @classmethod
    def from_json_compat(cls, data: dict[str, Any]) -> Message:
        """Reconstruct a Message from serialized JSON data with TS interop.

        Like :meth:`from_json` but also accepts camelCase keys
        (``threadId``, ``dateSent``, etc.) for TypeScript SDK interop.
        """
        meta = data.get("metadata", {})

        date_sent_raw = meta.get("date_sent") or meta.get("dateSent")
        date_sent = (
            _parse_iso(date_sent_raw)
            if isinstance(date_sent_raw, str)
            else (date_sent_raw if isinstance(date_sent_raw, datetime) else datetime.now())
        )
        edited_at_raw = meta.get("edited_at") or meta.get("editedAt")
        edited_at: datetime | None = (
            _parse_iso(edited_at_raw)
            if isinstance(edited_at_raw, str)
            else (edited_at_raw if isinstance(edited_at_raw, datetime) else None)
        )

        author_data = data.get("author", {})
        attachments_data = data.get("attachments", [])
        links_data = data.get("links")

        return cls(
            id=data.get("id", ""),
            thread_id=data.get("thread_id") or data.get("threadId", ""),
            text=data.get("text", ""),
            formatted=data.get("formatted", {"type": "root", "children": []}),
            raw=data.get("raw"),
            author=Author(
                user_id=author_data.get("user_id") or author_data.get("userId", ""),
                user_name=author_data.get("user_name") or author_data.get("userName", ""),
                full_name=author_data.get("full_name") or author_data.get("fullName", ""),
                is_bot=author_data.get("is_bot") if "is_bot" in author_data else author_data.get("isBot", False),
                is_me=author_data.get("is_me") if "is_me" in author_data else author_data.get("isMe", False),
            ),
            metadata=MessageMetadata(
                date_sent=date_sent,
                edited=meta.get("edited", False),
                edited_at=edited_at,
            ),
            attachments=[
                Attachment(
                    type=att.get("type", "file"),
                    url=att.get("url"),
                    name=att.get("name"),
                    mime_type=(att.get("mime_type") if att.get("mime_type") is not None else att.get("mimeType")),
                    size=att.get("size"),
                    width=att.get("width"),
                    height=att.get("height"),
                    # ``data`` is not part of the ``SerializedAttachment`` wire
                    # shape (bytes aren't JSON-safe), but accepting it here
                    # keeps in-memory callers that pass raw dicts with
                    # pre-fetched bytes from silently losing the payload.
                    data=att.get("data"),
                    fetch_metadata=(
                        att.get("fetch_metadata") if att.get("fetch_metadata") is not None else att.get("fetchMetadata")
                    ),
                )
                for att in attachments_data
            ],
            is_mention=data.get("is_mention") if "is_mention" in data else data.get("isMention"),
            links=[
                LinkPreview(
                    url=lp.get("url", ""),
                    title=lp.get("title"),
                    description=lp.get("description"),
                    image_url=lp.get("image_url") or lp.get("imageUrl"),
                    site_name=lp.get("site_name") or lp.get("siteName"),
                )
                for lp in links_data
            ]
            if links_data
            else None,
        )


@dataclass
class MessageData:
    """Raw message data for constructing a Message."""

    id: str
    thread_id: str
    text: str
    formatted: FormattedContent
    author: Author
    metadata: MessageMetadata
    attachments: list[Attachment] = field(default_factory=list)
    is_mention: bool | None = None
    links: list[LinkPreview] | None = None
    raw: Any = None


@dataclass
class RawMessage:
    """Raw platform message wrapper."""

    id: str
    thread_id: str
    raw: Any
    # Optional adapter-authoritative text snapshot. When set, callers
    # like ``Thread.stream`` MUST prefer this over their own local
    # accumulator when constructing the recorded ``SentMessage`` body /
    # message-history entry. Used by adapters whose internal state
    # (cancellation, throttling, partial commits) makes the local
    # accumulator diverge from what the platform actually accepted —
    # the Teams native streaming path sets this when a session is
    # canceled mid-flight so ``Thread.stream`` records only the text
    # Teams shipped, not the buffered suffix the user canceled out of.
    # ``None`` means "use the caller's existing logic" — backward
    # compatible for adapters that don't need this override.
    #
    # Divergence from upstream — see docs/UPSTREAM_SYNC.md. Upstream's
    # ``RawMessage`` interface (packages/chat/src/types.ts) has only
    # ``id``, ``raw``, ``threadId``; the override is Python-only because
    # we hand-roll Teams native streaming (upstream uses
    # ``@microsoft/teams.apps``'s ``IStreamer.emit`` which owns the
    # cancellation-text reconciliation internally). Will simplify or
    # disappear once we migrate to ``microsoft-teams-apps`` (Python).
    text: str | None = None


@dataclass
class SentMessage:
    """A sent message with edit/delete capabilities."""

    id: str
    thread_id: str
    text: str
    formatted: FormattedContent
    author: Author
    metadata: MessageMetadata
    attachments: list[Attachment] = field(default_factory=list)
    is_mention: bool | None = None
    links: list[LinkPreview] | None = None
    raw: Any = None

    # These are set by the SDK after construction
    _add_reaction: Callable[..., Awaitable[None]] | None = field(default=None, repr=False)
    _remove_reaction: Callable[..., Awaitable[None]] | None = field(default=None, repr=False)
    _edit: Callable[..., Awaitable[SentMessage]] | None = field(default=None, repr=False)
    _delete: Callable[..., Awaitable[None]] | None = field(default=None, repr=False)

    async def add_reaction(self, emoji: EmojiValue | str) -> None:
        if self._add_reaction:
            await self._add_reaction(emoji)

    async def remove_reaction(self, emoji: EmojiValue | str) -> None:
        if self._remove_reaction:
            await self._remove_reaction(emoji)

    async def edit(self, new_content: Any) -> SentMessage:
        if self._edit:
            return await self._edit(new_content)
        raise RuntimeError("edit not available")

    async def delete(self) -> None:
        if self._delete:
            await self._delete()


@dataclass
class EphemeralMessage:
    """An ephemeral (user-only visible) message."""

    id: str
    thread_id: str
    raw: Any
    used_fallback: bool


@dataclass
class ScheduledMessage:
    """A scheduled future message."""

    scheduled_message_id: str
    channel_id: str
    post_at: datetime
    raw: Any
    _cancel: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def cancel(self) -> None:
        if self._cancel:
            await self._cancel()


# =============================================================================
# Postable Messages
# =============================================================================


@dataclass
class PostableRaw:
    """Raw platform text message."""

    raw: str
    attachments: list[Attachment] | None = None
    files: list[FileUpload] | None = None


@dataclass
class PostableMarkdown:
    """Markdown message."""

    markdown: str
    attachments: list[Attachment] | None = None
    files: list[FileUpload] | None = None


@dataclass
class PostableAst:
    """AST-based message."""

    ast: FormattedContent
    attachments: list[Attachment] | None = None
    files: list[FileUpload] | None = None


@dataclass
class PostableCard:
    """Card message."""

    card: CardElement
    fallback_text: str | None = None
    files: list[FileUpload] | None = None


# Union of adapter-postable message types
AdapterPostableMessage = str | PostableRaw | PostableMarkdown | PostableAst | PostableCard | CardElement

# Union of all postable message types (includes streaming and PostableObjects)
# PostableObject instances are detected at runtime via ``is_postable_object()``.
PostableMessage = AdapterPostableMessage | AsyncIterable[Any]

# =============================================================================
# Streaming Types
# =============================================================================


@dataclass
class MarkdownTextChunk:
    """Streamed markdown text content."""

    type: Literal["markdown_text"] = "markdown_text"
    text: str = ""


@dataclass
class TaskUpdateChunk:
    """Tool/step progress card."""

    type: Literal["task_update"] = "task_update"
    id: str = ""
    title: str = ""
    status: Literal["pending", "in_progress", "complete", "error"] = "pending"
    output: str | None = None


@dataclass
class PlanUpdateChunk:
    """Plan title update."""

    type: Literal["plan_update"] = "plan_update"
    title: str = ""


StreamChunk = MarkdownTextChunk | TaskUpdateChunk | PlanUpdateChunk


@dataclass
class StreamOptions:
    """Options for streaming messages."""

    recipient_team_id: str | None = None
    recipient_user_id: str | None = None
    stop_blocks: list[Any] | None = None
    task_display_mode: Literal["timeline", "plan"] | None = None
    update_interval_ms: int | None = None


# =============================================================================
# Fetch Types
# =============================================================================


@dataclass
class FetchOptions:
    """Options for fetching messages."""

    limit: int | None = None
    cursor: str | None = None
    direction: FetchDirection | None = None


@dataclass
class FetchResult:
    """Result of a message fetch."""

    messages: list[Message] = field(default_factory=list)
    next_cursor: str | None = None


# =============================================================================
# Thread & Channel Types
# =============================================================================


@dataclass
class ChannelInfo:
    """Channel information."""

    id: str
    name: str | None = None
    is_dm: bool | None = None
    member_count: int | None = None
    channel_visibility: ChannelVisibility | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThreadInfo:
    """Thread information."""

    id: str
    channel_id: str
    channel_name: str | None = None
    is_dm: bool | None = None
    channel_visibility: ChannelVisibility | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThreadSummary:
    """Lightweight thread summary."""

    id: str
    root_message: Message
    reply_count: int | None = None
    last_reply_at: datetime | None = None


@dataclass
class ListThreadsOptions:
    """Options for listing threads."""

    limit: int | None = None
    cursor: str | None = None


@dataclass
class ListThreadsResult:
    """Result of listing threads."""

    threads: list[ThreadSummary] = field(default_factory=list)
    next_cursor: str | None = None


THREAD_STATE_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days


# =============================================================================
# Webhook Options
# =============================================================================


@dataclass
class WebhookOptions:
    """Options for webhook handling."""

    wait_until: Callable[[Awaitable[Any]], None] | None = None


# =============================================================================
# Queue Entry
# =============================================================================


@dataclass
class QueueEntry:
    """An entry in the per-thread message queue."""

    enqueued_at: int
    expires_at: int
    message: Message


@dataclass
class MessageContext:
    """Context for messages that were queued."""

    skipped: list[Message] = field(default_factory=list)
    total_since_last_handler: int = 0


# =============================================================================
# Lock
# =============================================================================


@dataclass
class Lock:
    """Thread lock."""

    thread_id: str
    token: str
    expires_at: int


# =============================================================================
# State Adapter Interface
# =============================================================================


@runtime_checkable
class StateAdapter(Protocol):
    """State adapter for subscriptions, locking, and caching."""

    async def acquire_lock(self, thread_id: str, ttl_ms: int) -> Lock | None: ...
    async def release_lock(self, lock: Lock) -> None: ...
    async def extend_lock(self, lock: Lock, ttl_ms: int) -> bool: ...
    async def force_release_lock(self, thread_id: str) -> None: ...
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl_ms: int | None = None) -> None: ...
    async def set_if_not_exists(self, key: str, value: Any, ttl_ms: int | None = None) -> bool: ...
    async def delete(self, key: str) -> None: ...
    async def append_to_list(
        self, key: str, value: Any, *, max_length: int | None = None, ttl_ms: int | None = None
    ) -> None: ...
    async def get_list(self, key: str) -> list[Any]: ...
    async def enqueue(self, thread_id: str, entry: QueueEntry, max_size: int) -> int: ...
    async def dequeue(self, thread_id: str) -> QueueEntry | None: ...
    async def queue_depth(self, thread_id: str) -> int: ...
    async def subscribe(self, thread_id: str) -> None: ...
    async def unsubscribe(self, thread_id: str) -> None: ...
    async def is_subscribed(self, thread_id: str) -> bool: ...
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...


# =============================================================================
# Event Types
# =============================================================================


@dataclass
class PostEphemeralOptions:
    """Options for posting ephemeral messages."""

    fallback_to_dm: bool = True


@dataclass
class ReactionEvent:
    """Reaction event data.

    Matches upstream TS `ReactionEvent` shape: `thread` is required here
    because handlers receive the fully-populated event after `Chat`
    re-wraps any partial event from an adapter. Adapters dispatch via
    `chat.process_reaction(...)` with a partial event (`thread=None` at
    construction); `Chat` resolves the real thread before invoking
    handlers, so this field is never `None` at handler time.
    """

    adapter: Adapter
    thread: Thread
    thread_id: str
    message_id: str
    user: Author
    emoji: EmojiValue
    raw_emoji: str
    added: bool
    message: Message | None = None
    raw: Any = None


@dataclass
class ActionEvent:
    """Button click/action event data."""

    adapter: Adapter
    thread: Thread | None
    thread_id: str
    message_id: str
    user: Author
    action_id: str
    value: str | None = None
    trigger_id: str | None = None
    raw: Any = None
    _open_modal: Callable[..., Awaitable[dict[str, str] | None]] | None = field(default=None, repr=False)

    async def open_modal(self, modal: Any) -> dict[str, str] | None:
        if self._open_modal:
            return await self._open_modal(modal)
        return None


@dataclass
class OptionsLoadEvent:
    """Event emitted when an adapter needs dynamic options for an external select.

    Port of upstream TS ``OptionsLoadEvent``. Slack dispatches a
    ``block_suggestion`` payload to populate an external-select menu; the
    handler returns the matching options for the current query text.
    """

    action_id: str
    adapter: Adapter
    query: str
    user: Author
    raw: Any = None


@dataclass
class ModalSubmitEvent:
    """Modal form submission event."""

    adapter: Adapter
    user: Author
    view_id: str
    callback_id: str
    values: dict[str, str]
    private_metadata: str | None = None
    related_thread: Any = None
    related_message: Any = None
    related_channel: Any = None
    raw: Any = None


@dataclass
class ModalCloseEvent:
    """Modal close event."""

    adapter: Adapter
    user: Author
    view_id: str
    callback_id: str
    private_metadata: str | None = None
    related_thread: Any = None
    related_message: Any = None
    related_channel: Any = None
    raw: Any = None


@dataclass
class ModalResponse:
    """Response to a modal submit event.

    The ``action`` field selects which Slack ``response_action`` is sent:

    * ``"close"``     — close the current view (no body)
    * ``"clear"``     — close the entire view stack
    * ``"update"``    — replace the current view with ``modal``
    * ``"push"``      — push ``modal`` onto the view stack
    * ``"errors"``    — show field-level errors (``errors`` dict)
    """

    action: Literal["close", "clear", "update", "push", "errors"]
    modal: Any = None
    errors: dict[str, str] | None = None


@dataclass
class SlashCommandEvent:
    """Slash command event data.

    Matches upstream TS `SlashCommandEvent`: `channel` is required here
    because handlers receive the fully-populated event after `Chat`
    re-wraps the partial event from an adapter. Adapters pass
    `channel=None` at construction; `Chat` constructs a real `Channel`
    before invoking handlers, so this is never `None` at handler time.
    """

    adapter: Adapter
    channel: Channel
    user: Author
    command: str
    text: str
    trigger_id: str | None = None
    raw: Any = None
    _open_modal: Callable[..., Awaitable[dict[str, str] | None]] | None = field(default=None, repr=False)

    async def open_modal(self, modal: Any) -> dict[str, str] | None:
        if self._open_modal:
            return await self._open_modal(modal)
        return None


@dataclass
class AssistantThreadStartedEvent:
    """Slack Assistant thread started event."""

    adapter: Adapter
    thread_id: str
    thread_ts: str
    channel_id: str
    user_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AssistantContextChangedEvent:
    """Slack Assistant context changed event."""

    adapter: Adapter
    thread_id: str
    thread_ts: str
    channel_id: str
    user_id: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppHomeOpenedEvent:
    """App Home opened event."""

    adapter: Adapter
    channel_id: str
    user_id: str


@dataclass
class MemberJoinedChannelEvent:
    """Member joined channel event."""

    adapter: Adapter
    channel_id: str
    user_id: str
    inviter_id: str | None = None


# =============================================================================
# Adapter Interface
# =============================================================================


@runtime_checkable
class Adapter(Protocol):
    """Adapter interface for platform implementations.

    Defines the **required** methods every adapter must implement.
    For optional methods (``stream``, ``open_dm``, ``post_ephemeral``, etc.)
    see :class:`BaseAdapter` which provides default implementations that
    raise :class:`~chat_sdk.errors.ChatNotImplementedError`.
    """

    @property
    def name(self) -> str: ...
    @property
    def user_name(self) -> str: ...
    @property
    def bot_user_id(self) -> str | None: ...
    @property
    def lock_scope(self) -> LockScope | None: ...

    # NOTE: the optional history-persistence flags (``persist_thread_history``
    # and its deprecated alias ``persist_message_history``) are declared on
    # ``BaseAdapter``, NOT here. Adding optional hooks to this structural
    # Protocol makes them *required* members and breaks adapters that don't
    # define them (several adapters satisfy the Protocol without extending
    # BaseAdapter). The SDK reads both flags via ``getattr(..., None)``.

    def encode_thread_id(self, platform_data: Any) -> str: ...
    def decode_thread_id(self, thread_id: str) -> Any: ...
    def channel_id_from_thread_id(self, thread_id: str) -> str: ...

    async def post_message(self, thread_id: str, message: AdapterPostableMessage) -> RawMessage: ...
    async def edit_message(self, thread_id: str, message_id: str, message: AdapterPostableMessage) -> RawMessage: ...
    async def delete_message(self, thread_id: str, message_id: str) -> None: ...
    async def fetch_messages(self, thread_id: str, options: FetchOptions | None = None) -> FetchResult: ...
    def parse_message(self, raw: Any) -> Message: ...
    async def fetch_thread(self, thread_id: str) -> ThreadInfo: ...

    async def add_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None: ...
    async def remove_reaction(self, thread_id: str, message_id: str, emoji: EmojiValue | str) -> None: ...
    async def start_typing(self, thread_id: str, status: str | None = None) -> None: ...

    def render_formatted(self, content: FormattedContent) -> str: ...

    async def handle_webhook(self, request: Any, options: WebhookOptions | None = None) -> Any: ...
    async def initialize(self, chat: ChatInstance) -> None: ...

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up user information by user ID.

        Optional — not all platforms support this.  Returns ``None`` when the
        user is not found or the lookup fails.
        """
        return None

    # NOTE: ``fetch_subject`` is intentionally NOT declared here. Upstream's
    # ``Adapter.fetchSubject`` is an *optional* member (``fetchSubject?(...)``),
    # and in this Python port the established convention for optional adapter
    # hooks (``stream``, ``open_dm``, ``rehydrate_attachment``,
    # ``get_channel_visibility``, ...) is to declare them on :class:`BaseAdapter`
    # only — NOT on this structural ``Protocol`` — so that adapters which don't
    # implement them still satisfy ``Adapter`` for type-checking. Declaring it
    # on the Protocol would make it a *required* attribute and break every
    # adapter that doesn't define it. :attr:`Message.subject` reads the hook via
    # ``getattr(adapter, "fetch_subject", None)``, so presence is fully optional.


class BaseAdapter:
    """Base adapter with default implementations for optional methods.

    Concrete adapters should inherit from this class and override methods
    they support.  Required methods (from :class:`Adapter`) must still be
    implemented by the subclass.  Optional methods raise
    :class:`~chat_sdk.errors.ChatNotImplementedError` by default.
    """

    # -- Required properties (must be overridden) ----------------------------

    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def user_name(self) -> str:
        raise NotImplementedError

    @property
    def bot_user_id(self) -> str | None:
        return None

    @property
    def lock_scope(self) -> LockScope | None:
        return None

    # Deprecated: renamed to ``persist_thread_history``.  Kept for
    # backwards compatibility; either flag being truthy enables persistence.
    @property
    def persist_message_history(self) -> bool | None:
        return None

    @property
    def persist_thread_history(self) -> bool | None:
        return None

    # -- Optional methods with default (not-implemented) --------------------

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage | None:
        """Stream a message using platform-native streaming APIs.

        The adapter consumes the async iterable and handles the entire
        streaming lifecycle.  Available on platforms with native streaming
        or preview APIs.  Adapters may return ``None`` before consuming any
        chunks to delegate back to the SDK's built-in post+edit fallback
        for the current thread (vercel/chat#340).
        """
        raise ChatNotImplementedError(self.name, "stream")

    async def open_dm(self, user_id: str) -> str:
        """Open a direct message conversation with a user.

        Returns the thread ID for the DM conversation.
        """
        raise ChatNotImplementedError(self.name, "openDM")

    async def post_ephemeral(
        self,
        thread_id: str,
        user_id: str,
        message: AdapterPostableMessage,
    ) -> EphemeralMessage:
        """Post an ephemeral message visible only to a specific user."""
        raise ChatNotImplementedError(self.name, "postEphemeral")

    async def schedule_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
        options: dict[str, Any],
    ) -> ScheduledMessage:
        """Schedule a message for future delivery.

        ``options`` must contain ``post_at`` (a :class:`~datetime.datetime`).
        """
        raise ChatNotImplementedError(self.name, "scheduleMessage")

    async def list_threads(
        self,
        channel_id: str,
        options: ListThreadsOptions | dict[str, Any] | None = None,
    ) -> ListThreadsResult:
        """List threads in a channel."""
        raise ChatNotImplementedError(self.name, "listThreads")

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel info/metadata."""
        raise ChatNotImplementedError(self.name, "fetchChannelInfo")

    async def fetch_channel_messages(
        self,
        channel_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch channel-level messages (top-level, not thread replies)."""
        raise ChatNotImplementedError(self.name, "fetchChannelMessages")

    async def fetch_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> Message | None:
        """Fetch a single message by ID.

        Returns the message, or ``None`` if not found / not supported.
        """
        raise ChatNotImplementedError(self.name, "fetchMessage")

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a direct message conversation."""
        raise ChatNotImplementedError(self.name, "isDM")

    def get_channel_visibility(self, thread_id: str) -> ChannelVisibility:
        """Get the visibility scope of the channel containing the thread."""
        raise ChatNotImplementedError(self.name, "getChannelVisibility")

    async def open_modal(
        self,
        trigger_id: str,
        modal: Any,
        context_id: str | None = None,
    ) -> dict[str, str]:
        """Open a modal/dialog form.

        Returns a dict containing at least ``view_id``.
        """
        raise ChatNotImplementedError(self.name, "openModal")

    async def post_channel_message(
        self,
        channel_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to channel top-level (not in a thread)."""
        raise ChatNotImplementedError(self.name, "postChannelMessage")

    async def on_thread_subscribe(self, thread_id: str) -> None:
        """Hook called when a thread is subscribed to.

        Adapters can use this to set up platform-specific subscriptions
        (e.g., Google Chat Workspace Events).
        """
        raise ChatNotImplementedError(self.name, "onThreadSubscribe")

    async def disconnect(self) -> None:
        """Cleanup hook called when the Chat instance is shut down."""
        raise ChatNotImplementedError(self.name, "disconnect")

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up user information by user ID.

        Optional — not all platforms support this.  Concrete adapters that
        can resolve users via a platform API should override this.  The
        default raises :class:`~chat_sdk.errors.ChatNotImplementedError`,
        which :meth:`~chat_sdk.chat.Chat.get_user` translates into a
        ``"does not support get_user"`` :class:`~chat_sdk.errors.ChatError`.
        """
        raise ChatNotImplementedError(self.name, "getUser")

    async def fetch_subject(self, raw: Any) -> MessageSubject | None:
        """Resolve the external subject a message refers to (issue, PR, etc.).

        Optional — the default returns ``None`` (no subject).  Adapters that
        can resolve a backing entity (a Linear issue, a GitHub PR, etc.) from a
        message's raw payload should override this.  Unlike most optional
        :class:`BaseAdapter` hooks it does *not* raise
        :class:`~chat_sdk.errors.ChatNotImplementedError`, because
        :attr:`Message.subject` is best-effort: "this adapter has no subject
        concept" is a normal, non-error outcome that maps to ``None``.

        Mirrors upstream's optional ``Adapter.fetchSubject``
        (``packages/chat/src/types.ts``).
        """
        return None

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on an attachment after deserialization.

        Called by :class:`~chat_sdk.chat.Chat` during message rehydration in
        the queue/debounce concurrency paths.  The default implementation is a
        no-op (returns the attachment unchanged) — adapters that support file
        downloads should override it to rebuild the platform-specific
        download closure from ``attachment.fetch_metadata``.

        .. important::
           This hook must be **synchronous**.  Async rehydration is not
           supported — the call site assigns the return value directly into
           ``Message.attachments``, so returning a coroutine would land a
           coroutine in the list and downstream ``att.fetch_data`` access
           would raise.  If platform-specific rehydration needs I/O, push
           the async work into the returned ``fetch_data`` closure (which
           *is* awaited when consumers call it) instead of doing it here.
        """
        return attachment


# =============================================================================
# Chat Configuration
# =============================================================================


@dataclass
class ChatConfig:
    """Chat configuration."""

    adapters: dict[str, Adapter]
    state: StateAdapter
    user_name: str
    # How to handle concurrent messages to the same thread/channel.
    # Pass a strategy name ("drop", "queue", "debounce", "burst", "concurrent")
    # or a full ConcurrencyConfig for fine-grained control.
    concurrency: ConcurrencyStrategy | ConcurrencyConfig | None = None
    # Milliseconds to remember a message ID for deduplication (default 5 min).
    dedupe_ttl_ms: int = 300000
    fallback_streaming_placeholder_text: str | None = "..."
    # Resolves a stable cross-platform user key from inbound messages.
    #
    # Required when ``transcripts`` is configured.  Called once per inbound
    # message during dispatch; the result is attached to the Message
    # instance as ``message.user_key`` for handlers to use.
    identity: IdentityResolver | None = None
    # Whether locks are scoped per-thread or per-channel.
    # Can also be a callable that inspects context and returns the scope.
    lock_scope: LockScope | Callable[..., LockScope | Awaitable[LockScope]] | None = None
    logger: Logger | LogLevel | None = None
    # Deprecated: renamed to ``thread_history``.  Both fields are read for
    # backwards compatibility; ``thread_history`` takes precedence when both
    # are set.
    message_history: dict[str, Any] | None = None
    # What to do when a lock is already held: "drop" the new message,
    # "force" acquire, or a callable that decides at runtime.
    on_lock_conflict: OnLockConflict | None = None
    streaming_update_interval_ms: int = 500
    # Configuration dict for persistent per-thread message history backfill
    # (e.g. {"max_messages": 50, "ttl_ms": 86_400_000}).
    #
    # Only used by adapters that set ``persist_thread_history`` (e.g.
    # Telegram, WhatsApp).  Distinct from ``transcripts`` (the cross-platform
    # per-user Transcripts API).
    thread_history: dict[str, Any] | None = None
    # Cross-platform per-user message persistence.
    #
    # When set, ``chat.transcripts`` is available for append/list/count/delete
    # keyed by a resolved cross-platform user key.
    #
    # Requires ``identity`` to also be set; the constructor raises otherwise.
    transcripts: TranscriptsConfig | None = None


# =============================================================================
# Chat Instance Interface
# =============================================================================


@runtime_checkable
class ChatInstance(Protocol):
    """Internal interface for Chat instance passed to adapters."""

    def get_logger(self, prefix: str | None = None) -> Logger: ...
    def get_state(self) -> StateAdapter: ...
    def get_user_name(self) -> str: ...
    def process_message(
        self,
        adapter: Adapter,
        thread_id: str,
        message: Message | Callable[[], Awaitable[Message]],
        options: WebhookOptions | None = None,
    ) -> asyncio.Task[None] | None: ...
    async def handle_incoming_message(self, adapter: Adapter, thread_id: str, message: Message) -> None: ...
    def process_action(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_reaction(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_slash_command(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_modal_submit(
        self, event: Any, context_id: str | None = None, options: WebhookOptions | None = None
    ) -> Awaitable[ModalResponse | None]: ...
    def process_options_load(
        self, event: OptionsLoadEvent, options: WebhookOptions | None = None
    ) -> Awaitable[OptionsLoadResult | None]: ...
    def process_modal_close(
        self, event: Any, context_id: str | None = None, options: WebhookOptions | None = None
    ) -> None: ...
    def process_assistant_thread_started(
        self, event: AssistantThreadStartedEvent, options: WebhookOptions | None = None
    ) -> None: ...
    def process_assistant_context_changed(
        self, event: AssistantContextChangedEvent, options: WebhookOptions | None = None
    ) -> None: ...
    def process_app_home_opened(self, event: AppHomeOpenedEvent, options: WebhookOptions | None = None) -> None: ...
    def process_member_joined_channel(
        self, event: MemberJoinedChannelEvent, options: WebhookOptions | None = None
    ) -> None: ...

    # Cross-platform per-user transcript store.  Raises on access when
    # ``transcripts`` is not configured on the Chat instance — callers should
    # check ``ChatConfig.transcripts`` if they need a no-raise guard.
    @property
    def transcripts(self) -> TranscriptsApi: ...


# =============================================================================
# Postable / Channel / Thread interfaces (simplified for adapter use)
# =============================================================================


class Postable(Protocol):
    """Base for entities that can receive messages.

    Both :class:`Thread` and :class:`Channel` extend this interface.
    """

    @property
    def id(self) -> str: ...
    @property
    def adapter(self) -> Adapter: ...
    @property
    def is_dm(self) -> bool: ...
    @property
    def channel_visibility(self) -> ChannelVisibility: ...

    def mention_user(self, user_id: str) -> str:
        """Get a platform-specific mention string for a user."""
        ...

    async def post(self, message: PostableMessage) -> SentMessage:
        """Post a message."""
        ...

    async def post_ephemeral(
        self,
        user: str | Author,
        message: AdapterPostableMessage,
        options: PostEphemeralOptions,
    ) -> EphemeralMessage | None:
        """Post an ephemeral message visible only to a specific user."""
        ...

    async def schedule(
        self,
        message: AdapterPostableMessage,
        *,
        post_at: datetime,
    ) -> ScheduledMessage:
        """Schedule a message for future delivery."""
        ...

    async def set_state(
        self,
        new_state: dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Set the state. Merges with existing state by default.

        Parameter is named `new_state` to match upstream TS
        `setState(newState)` and preserve call-site kwarg compatibility.
        """
        ...

    async def start_typing(self, status: str | None = None) -> None:
        """Show typing indicator."""
        ...

    async def get_state(self) -> Any | None:
        """Get the current state. Returns ``None`` if no state has been set."""
        ...


class Channel(Postable, Protocol):
    """Channel interface.

    Represents a channel/conversation container that holds threads.
    Extends :class:`Postable` for message posting capabilities.
    """

    @property
    def name(self) -> str | None: ...

    async def fetch_metadata(self) -> ChannelInfo:
        """Fetch channel metadata from the platform."""
        ...

    def messages(self) -> AsyncIterable[Message]:
        """Iterate messages newest first (backward from most recent).

        Auto-paginates lazily -- only fetches pages as consumed.

        Note: This is a method, not a property.  Call with ``()``:
        ``async for msg in channel.messages(): ...``
        """
        ...

    def threads(self) -> AsyncIterable[ThreadSummary]:
        """Iterate threads in this channel, most recently active first.

        Returns :class:`ThreadSummary` (lightweight) for efficiency.
        """
        ...

    def to_json(self) -> dict[str, Any]:
        """Serialize the channel to a plain JSON object."""
        ...


class Thread(Postable, Protocol):
    """Thread interface.

    Extends :class:`Postable` with thread-specific capabilities like
    message iteration, subscription, and channel access.
    """

    @property
    def channel_id(self) -> str: ...

    @property
    def channel(self) -> Channel: ...

    @property
    def recent_messages(self) -> list[Message]: ...

    def messages(self) -> AsyncIterable[Message]:
        """Iterate messages newest first (backward from most recent).

        Auto-paginates lazily -- only fetches pages as consumed.

        Note: This is a method, not a property.  Call with ``()``:
        ``async for msg in thread.messages(): ...``
        """
        ...

    def all_messages(self) -> AsyncIterable[Message]:
        """Iterate messages oldest first (forward from beginning).

        Auto-paginates lazily.

        Note: This is a method, not a property.  Call with ``()``:
        ``async for msg in thread.all_messages(): ...``
        """
        ...

    async def is_subscribed(self) -> bool:
        """Check if this thread is currently subscribed."""
        ...

    async def subscribe(self) -> None:
        """Subscribe to future messages in this thread."""
        ...

    async def unsubscribe(self) -> None:
        """Unsubscribe from this thread."""
        ...

    async def refresh(self) -> None:
        """Refresh ``recent_messages`` from the API."""
        ...

    async def get_participants(self) -> list[Author]:
        """Return unique non-bot, non-self authors who've posted in the thread."""
        ...

    def create_sent_message_from_message(self, message: Message) -> SentMessage:
        """Wrap a Message as a SentMessage with edit/delete capabilities."""
        ...

    def to_json(self) -> dict[str, Any]:
        """Serialize the thread to a plain JSON object."""
        ...


# =============================================================================
# Transcripts API (cross-platform per-user message persistence)
# =============================================================================


@dataclass
class IdentityContext:
    """Context passed to an :data:`IdentityResolver`."""

    # Adapter name (e.g. "slack", "discord").
    adapter: str
    author: Author
    message: Message


# Resolves a stable, cross-platform user key from an inbound message context.
#
# Return ``None`` to skip persistence for this event (unknown user, system
# message, or the bot itself).  The SDK fails loudly rather than silently
# falling back to a platform-specific ID.  May be sync or async.
IdentityResolver = Callable[[IdentityContext], "str | None | Awaitable[str | None]"]

# Role tag on a stored message.
#
# - ``user``: produced by the resolved end-user
# - ``assistant``: produced by this bot
# - ``system``: SDK-injected marker (handoff, summary). Adapters never produce it.
TranscriptRole = Literal["user", "assistant", "system"]

# Duration shorthand: e.g. ``"7d"``, ``"30m"``, ``"2h"``, ``"45s"``.
# (TS models this as a template-literal type; Python uses a plain ``str``.)
DurationString = str


@dataclass
class TranscriptEntry:
    """A stored transcript entry.

    Serialized at the storage boundary via :meth:`to_json` using the same
    camelCase shape the upstream TS SDK writes, so stores are interoperable.
    """

    # UUID assigned by the SDK at append time. Opaque — not lexicographically
    # sortable. Entries are returned by ``list()`` in append order (the
    # underlying list semantics of ``state.append_to_list``); use
    # ``timestamp`` to reason about ordering across stores.
    id: str
    # Cross-platform user key from the IdentityResolver.
    user_key: str
    role: TranscriptRole
    # Plain-text body — canonical field for prompt building.
    text: str
    # Originating adapter name.
    platform: str
    # Originating thread ID.
    thread_id: str
    # ms-since-epoch, set at append time on the SDK side.
    timestamp: int
    # mdast AST. Only present when ``transcripts.store_formatted`` is true.
    formatted: FormattedContent | None = None
    # Platform-native message ID, when known.
    platform_message_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Serialize to the camelCase storage shape (optional keys omitted)."""
        result: dict[str, Any] = {
            "id": self.id,
            "userKey": self.user_key,
            "role": self.role,
            "text": self.text,
            "platform": self.platform,
            "threadId": self.thread_id,
            "timestamp": self.timestamp,
        }
        if self.formatted is not None:
            result["formatted"] = self.formatted
        if self.platform_message_id is not None:
            result["platformMessageId"] = self.platform_message_id
        return result

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TranscriptEntry:
        """Reconstruct an entry from its camelCase storage shape."""
        return cls(
            id=data.get("id", ""),
            user_key=data.get("userKey", ""),
            role=data.get("role", "user"),
            text=data.get("text", ""),
            platform=data.get("platform", ""),
            thread_id=data.get("threadId", ""),
            timestamp=data.get("timestamp", 0),
            formatted=data.get("formatted"),
            platform_message_id=data.get("platformMessageId"),
        )


@dataclass
class TranscriptsConfig:
    """Configuration for the cross-platform per-user Transcripts API."""

    # Hard cap; older messages evicted on append. Default 200.
    max_per_user: int | None = None
    # Default retention applied as the list TTL (ms, or a DurationString such
    # as "7d"). Refreshed on every append (matches ``append_to_list``
    # semantics). Omit for no expiry.
    retention: int | DurationString | None = None
    # Persist ``formatted`` (mdast). Default False to keep storage small.
    store_formatted: bool = False


@dataclass
class AppendInput:
    """Input shape for appending a non-Message (e.g. an assistant reply you
    just posted via ``thread.post()``)."""

    role: TranscriptRole
    text: str
    formatted: FormattedContent | None = None
    platform_message_id: str | None = None


@dataclass
class AppendOptions:
    """Options for :meth:`TranscriptsApi.append`."""

    # Required when appending an ``AppendInput`` (assistant/system role) — the
    # SDK has no Message instance from which to read the resolved key.
    #
    # Ignored when appending a Message; the Message's own ``user_key`` is used.
    user_key: str | None = None


@dataclass
class ListQuery:
    """Query for :meth:`TranscriptsApi.list`."""

    user_key: str
    # Newest N kept (still returned in chronological order). Default 50.
    limit: int | None = None
    # Filter to a subset of adapter names.
    platforms: list[str] | None = None
    # Filter to specific roles. Default: all.
    roles: list[TranscriptRole] | None = None
    # Filter to a single thread.
    thread_id: str | None = None


@dataclass
class DeleteTarget:
    """Target for :meth:`TranscriptsApi.delete`.  Wipes every stored message
    under the given user key."""

    user_key: str


@dataclass
class CountQuery:
    """Query shape for :meth:`TranscriptsApi.count`."""

    user_key: str


@dataclass
class DeleteResult:
    """Result of :meth:`TranscriptsApi.delete`.

    Python-side named type for the upstream inline ``{ deleted: number }``
    return shape.
    """

    deleted: int


class TranscriptsApi(Protocol):
    """Cross-platform per-user message store.

    Distinct from the existing per-thread ``thread_history`` config (which
    exists to backfill thread context for adapters that lack server-side
    history APIs).  The Transcripts API is keyed by a resolved cross-platform
    user key and is intended for transcript-style use cases (LLM context
    building, audit).
    """

    async def append(
        self,
        thread: Postable,
        message: Message | AppendInput,
        options: AppendOptions | None = None,
    ) -> TranscriptEntry | None:
        """Persist a Message (or AppendInput) under the user key.

        - For Message: ``user_key`` is read from the Message instance (set by
          the SDK during inbound dispatch via the configured IdentityResolver).
          No-op if the Message has no ``user_key`` (resolver returned None).
        - For AppendInput: ``options.user_key`` is required.
        """
        ...

    async def count(self, query: CountQuery) -> int:
        """Total stored count for a user key."""
        ...

    async def delete(self, target: DeleteTarget) -> DeleteResult:
        """GDPR / DSR delete — wipes every stored message under the user key."""
        ...

    async def list(self, query: ListQuery) -> list[TranscriptEntry]:
        """Return the most recent entries in chronological order (oldest
        first), capped at ``query.limit`` (default 50).

        Pagination is intentionally not supported — the store keeps at most
        ``transcripts.max_per_user`` entries per user.  To widen the window,
        raise ``max_per_user``; to fetch a different slice, narrow with
        ``thread_id`` / ``platforms`` / ``roles``.
        """
        ...
