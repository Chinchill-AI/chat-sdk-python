"""Core types for chat-sdk.

Python port of Vercel Chat SDK types.ts.
"""

from __future__ import annotations

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


def _parse_iso(s: str) -> datetime:
    """Parse ISO 8601 datetime string, supporting Python 3.10+.

    Python 3.10's ``fromisoformat`` doesn't accept the ``Z`` suffix.
    """
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


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
ConcurrencyStrategy = Literal["drop", "queue", "debounce", "concurrent"]
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
    debounce_ms: int = 1500
    max_concurrent: int | None = None  # None = Infinity
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
class MessageMetadata:
    """Message metadata."""

    date_sent: datetime
    edited: bool = False
    edited_at: datetime | None = None


@dataclass
class Attachment:
    """File attachment."""

    type: Literal["image", "file", "video", "audio"]
    url: str | None = None
    name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    data: bytes | None = None
    fetch_data: Callable[[], Awaitable[bytes]] | None = None


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
    """Serialized attachment (non-serializable fields omitted)."""

    type: Literal["image", "file", "video", "audio"]
    url: str
    name: str
    mime_type: str
    size: int
    width: int
    height: int


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
    def from_json(cls, data: dict[str, Any]) -> Message:
        """Reconstruct a Message from serialized JSON data.

        Converts ISO date strings back to ``datetime`` objects.
        Accepts both camelCase (canonical output of ``to_json()``) and
        snake_case keys for backward compatibility.  For explicit
        dual-format handling see :meth:`from_json_compat`.
        """
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
                    mime_type=att.get("mimeType") or att.get("mime_type"),
                    size=att.get("size"),
                    width=att.get("width"),
                    height=att.get("height"),
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
                    mime_type=att.get("mime_type") or att.get("mimeType"),
                    size=att.get("size"),
                    width=att.get("width"),
                    height=att.get("height"),
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

# Union of all postable message types (includes streaming)
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
    """Reaction event data."""

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
    """Response to a modal submit event."""

    action: Literal["close", "update", "push", "errors"]
    modal: Any = None
    errors: dict[str, str] | None = None


@dataclass
class SlashCommandEvent:
    """Slash command event data."""

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
    @property
    def persist_message_history(self) -> bool | None: ...

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

    @property
    def persist_message_history(self) -> bool | None:
        return None

    # -- Optional methods with default (not-implemented) --------------------

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream a message using platform-native streaming APIs.

        The adapter consumes the async iterable and handles the entire
        streaming lifecycle.  Only available on platforms with native
        streaming support (e.g., Slack).
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
    # Pass a strategy name ("drop", "queue", "debounce", "concurrent")
    # or a full ConcurrencyConfig for fine-grained control.
    concurrency: ConcurrencyStrategy | ConcurrencyConfig | None = None
    # Milliseconds to remember a message ID for deduplication (default 5 min).
    dedupe_ttl_ms: int = 300000
    fallback_streaming_placeholder_text: str | None = "..."
    # Whether locks are scoped per-thread or per-channel.
    # Can also be a callable that inspects context and returns the scope.
    lock_scope: LockScope | Callable[..., LockScope | Awaitable[LockScope]] | None = None
    logger: Logger | LogLevel | None = None
    # Configuration dict forwarded to MessageHistoryCache
    # (e.g. {"max_messages": 50, "persist": True}).
    message_history: dict[str, Any] | None = None
    # What to do when a lock is already held: "drop" the new message,
    # "force" acquire, or a callable that decides at runtime.
    on_lock_conflict: OnLockConflict | None = None
    streaming_update_interval_ms: int = 500


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
    ) -> None: ...
    def process_action(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_reaction(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_slash_command(self, event: Any, options: WebhookOptions | None = None) -> None: ...
    def process_modal_submit(
        self, event: Any, context_id: str | None = None, options: WebhookOptions | None = None
    ) -> Awaitable[ModalResponse | None]: ...
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
        state: dict[str, Any],
        *,
        replace: bool = False,
    ) -> None:
        """Set the state. Merges with existing state by default."""
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

    def create_sent_message_from_message(self, message: Message) -> SentMessage:
        """Wrap a Message as a SentMessage with edit/delete capabilities."""
        ...

    def to_json(self) -> dict[str, Any]:
        """Serialize the thread to a plain JSON object."""
        ...
