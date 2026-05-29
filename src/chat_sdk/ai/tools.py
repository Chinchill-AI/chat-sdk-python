"""Tool factory for exposing Chat SDK operations to AI agents.

Python port of ``packages/chat/src/ai/tools.ts`` (and the supporting
``tools/{channels,messages,reactions,threads,users}.ts`` / ``types.ts``
files) introduced by `vercel/chat#492`_.

The TS implementation builds on the Vercel AI SDK's :func:`tool` helper and
``zod`` schemas. Python has no direct equivalent: there's no canonical AI
agent runtime in the standard library, and adding ``pydantic`` (or any other
runtime) would couple ``chat_sdk`` to a third-party schema validator. To
keep the surface framework-agnostic — and faithful to the upstream contract
that a tool is "a description + an input schema + an ``execute`` callable" —
each factory returns a plain :class:`ChatTool` dataclass holding:

* ``description`` — the natural-language description shown to the model.
* ``input_schema`` — a JSON-Schema-shaped :class:`dict` describing the tool
  inputs. Consumers that bind these tools into the Vercel AI SDK (via the
  ``@ai-sdk/python``-style bridge) or any other agent runtime can feed this
  dict directly to their schema layer.
* ``needs_approval`` — mirrors upstream's ``needsApproval`` flag for
  human-in-the-loop write tools. Falsy for read-only tools, ``True`` by
  default for writes.
* ``execute`` — an ``async`` callable taking a ``dict`` of validated
  arguments and returning the tool result.

The factory entry point :func:`create_chat_tools` mirrors upstream's
``createChatTools`` exactly: presets, ``require_approval`` config (bool or
per-tool mapping), per-tool ``overrides``, and the same set of protected
core fields that overrides cannot replace.

.. _vercel/chat#492: https://github.com/vercel/chat/pull/492
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from chat_sdk.chat import Chat
from chat_sdk.errors import ChatError, ChatNotImplementedError
from chat_sdk.types import Author, FetchOptions, ListThreadsOptions, Message

# ---------------------------------------------------------------------------
# Tool dataclass
# ---------------------------------------------------------------------------


@dataclass
class ChatTool:
    """A Chat SDK tool exposed to an AI agent.

    Mirrors the runtime shape produced by upstream's ``tool({...})`` helper:
    a ``description`` + an ``input_schema`` (JSON-Schema-shaped) + an
    ``execute`` coroutine. ``needs_approval`` is ``True`` for write tools so
    callers can gate execution behind human approval (matching upstream's
    ``needsApproval`` flag).

    Additional upstream fields exposed via the ``overrides`` config —
    ``title``, ``input_examples``, ``metadata``, ``provider_options``,
    ``strict``, ``to_model_output``, ``on_input_available``,
    ``on_input_delta``, ``on_input_start`` — are stored in :attr:`extras`
    as a free-form dict so callers can forward them to whatever agent
    runtime they bind these tools into.
    """

    description: str
    input_schema: dict[str, Any]
    execute: Callable[[dict[str, Any]], Awaitable[Any]]
    needs_approval: bool | None = None
    extras: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Aliases mirroring upstream type names
# ---------------------------------------------------------------------------

#: Alias for the ``Chat`` instance threaded through every tool factory.
#: Upstream types this as ``Chat<any, any>``; in Python ``Chat`` is already
#: untyped at the adapter level so a plain alias is enough.
ChatBinding = Chat


@dataclass
class ToolOptions:
    """Common options for write tools that may require approval before executing.

    Mirrors upstream's ``ToolOptions`` interface.
    """

    needs_approval: bool = True


#: Partial overrides for a single tool. Mirrors upstream's ``ToolOverrides``,
#: but ``input_schema``/``execute``/``output_schema`` etc. are filtered out
#: at apply-time (see :data:`_PROTECTED_TOOL_FIELDS`) so semantics stay
#: stable.
ToolOverrides = dict[str, Any]


# ---------------------------------------------------------------------------
# Tool names
# ---------------------------------------------------------------------------

#: Every tool name produced by :func:`create_chat_tools`. Kept as a
#: ``Literal`` alias instead of an enum so the mapping types below can be
#: expressed naturally.
ChatToolName = Literal[
    "fetchMessages",
    "fetchChannelMessages",
    "fetchThread",
    "listThreads",
    "getThreadParticipants",
    "getChannelInfo",
    "getUser",
    "startTyping",
    "postMessage",
    "postChannelMessage",
    "sendDirectMessage",
    "editMessage",
    "deleteMessage",
    "addReaction",
    "removeReaction",
    "subscribeThread",
    "unsubscribeThread",
]

#: Names of every tool that mutates platform state. These default to
#: ``needs_approval=True`` and can be toggled via ``require_approval`` on
#: :func:`create_chat_tools`.
ChatWriteToolName = Literal[
    "postMessage",
    "postChannelMessage",
    "sendDirectMessage",
    "editMessage",
    "deleteMessage",
    "addReaction",
    "removeReaction",
    "subscribeThread",
    "unsubscribeThread",
]

#: Whether write operations require user approval.
#:
#: - ``True``  — every write tool needs approval (default)
#: - ``False`` — no write tool needs approval
#: - ``dict``  — per-tool override; unspecified write tools default to
#:   ``True``
ApprovalConfig = bool | dict[str, bool]

#: Predefined tool presets for common chat-agent use cases.
#:
#: - ``"reader"``    — read-only: fetch threads, messages, channel info,
#:   users
#: - ``"messenger"`` — basic posting: post in thread/channel, DM, react,
#:   typing
#: - ``"moderator"`` — full management: read + write + edit/delete +
#:   subscriptions
ChatToolPreset = Literal["reader", "messenger", "moderator"]


_PRESET_TOOLS: dict[str, list[str]] = {
    "reader": [
        "fetchMessages",
        "fetchChannelMessages",
        "fetchThread",
        "listThreads",
        "getThreadParticipants",
        "getChannelInfo",
        "getUser",
    ],
    "messenger": [
        "fetchMessages",
        "fetchThread",
        "getChannelInfo",
        "getUser",
        "postMessage",
        "postChannelMessage",
        "sendDirectMessage",
        "addReaction",
        "removeReaction",
        "startTyping",
    ],
    "moderator": [
        "fetchMessages",
        "fetchChannelMessages",
        "fetchThread",
        "listThreads",
        "getThreadParticipants",
        "getChannelInfo",
        "getUser",
        "postMessage",
        "postChannelMessage",
        "sendDirectMessage",
        "editMessage",
        "deleteMessage",
        "addReaction",
        "removeReaction",
        "subscribeThread",
        "unsubscribeThread",
        "startTyping",
    ],
}


# Fields that overrides cannot replace. Mirrors upstream's
# ``PROTECTED_TOOL_FIELDS``. ``args``/``id``/``output_schema``/``type``/
# ``supports_deferred_results`` come from upstream's AI SDK shape; they're
# included verbatim so consumers porting upstream ``overrides`` dicts get
# the same protection.
_PROTECTED_TOOL_FIELDS: frozenset[str] = frozenset(
    {
        "args",
        "execute",
        "id",
        "input_schema",
        "inputSchema",
        "output_schema",
        "outputSchema",
        "supports_deferred_results",
        "supportsDeferredResults",
        "type",
    }
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _project_message(message: Message) -> dict[str, Any]:
    """Flatten a :class:`~chat_sdk.types.Message` for model consumption.

    Mirrors upstream's ``projectMessage`` helper. Field names use camelCase
    so that JSON dumps of the returned dicts match the upstream wire shape;
    Python callers who want snake_case can rename downstream.
    """
    return {
        "id": message.id,
        "threadId": message.thread_id,
        "text": message.text,
        "author": {
            "userId": message.author.user_id,
            "userName": message.author.user_name,
            "fullName": message.author.full_name,
            "isBot": message.author.is_bot,
            "isMe": message.author.is_me,
        },
        "dateSent": (message.metadata.date_sent.isoformat() if message.metadata.date_sent else None),
        "edited": message.metadata.edited,
        "isMention": getattr(message, "is_mention", False),
        "attachments": [
            {
                "type": att.type,
                "name": att.name,
                "mimeType": att.mime_type,
                "url": att.url,
            }
            for att in (message.attachments or [])
        ],
    }


def _project_author(author: Author) -> dict[str, Any]:
    return {
        "userId": author.user_id,
        "userName": author.user_name,
        "fullName": author.full_name,
        "isBot": author.is_bot,
    }


# ``message`` arg shape — one of: plain string, ``{"markdown": "..."}``,
# ``{"raw": "..."}``. Mirrors upstream's ``POSTABLE_INPUT`` union.
_POSTABLE_INPUT_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {"type": "string", "description": "Plain text body"},
        {
            "type": "object",
            "properties": {"markdown": {"type": "string"}},
            "required": ["markdown"],
            "additionalProperties": False,
            "description": "Markdown body, converted to the platform's native format",
        },
        {
            "type": "object",
            "properties": {"raw": {"type": "string"}},
            "required": ["raw"],
            "additionalProperties": False,
            "description": "Raw body, passed through to the platform untouched",
        },
    ],
    "description": "Message body",
}


def _to_postable(message: Any) -> Any:
    """Translate a tool-input ``message`` value to the SDK's postable form.

    Accepts a plain string, ``{"markdown": "..."}``, or ``{"raw": "..."}``.
    The string and ``{"raw": ...}`` cases pass through unchanged so the
    adapter can decide how to handle them; ``{"markdown": ...}`` is wrapped
    in a :class:`~chat_sdk.types.PostableMarkdown` so the SDK's markdown
    rendering path runs.
    """
    # Lazy import to keep tools.py importable without triggering the full
    # types module on cold start of consumers that only want the factory
    # for static introspection.
    from chat_sdk.types import PostableMarkdown, PostableRaw

    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        if "markdown" in message:
            return PostableMarkdown(markdown=message["markdown"])
        if "raw" in message:
            return PostableRaw(raw=message["raw"])
    # Fall through — pass anything else through unchanged so adapters that
    # accept their own postable shapes (e.g. a card) still work.
    return message


# ---------------------------------------------------------------------------
# channels.ts
# ---------------------------------------------------------------------------


def get_channel_info(chat: ChatBinding) -> ChatTool:
    """Fetch metadata for a channel (name, member count, DM status, etc.)."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        channel = chat.channel(args["channelId"])
        info = await channel.fetch_metadata()
        return {
            "id": info.id,
            "name": info.name,
            "isDM": info.is_dm if info.is_dm is not None else False,
            "memberCount": info.member_count,
            "channelVisibility": info.channel_visibility,
        }

    return ChatTool(
        description=(
            "Fetch metadata for a channel: name, member count, DM status, "
            "visibility, etc. Use to identify a channel before posting."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channelId": {
                    "type": "string",
                    "description": "Full channel id including adapter prefix",
                },
            },
            "required": ["channelId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


# ---------------------------------------------------------------------------
# messages.ts
# ---------------------------------------------------------------------------


def post_message(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Post a message inside an existing thread."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        sent = await thread.post(_to_postable(args["message"]))
        return {"messageId": sent.id, "threadId": sent.thread_id}

    return ChatTool(
        description=(
            "Post a message inside an existing thread. Use this to reply within a "
            "conversation the bot already has context for. The threadId is the "
            "full id (e.g. 'slack:C123:1234567890.123456')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {
                    "type": "string",
                    "description": "Full thread id including adapter prefix",
                },
                "message": _POSTABLE_INPUT_SCHEMA,
            },
            "required": ["threadId", "message"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def post_channel_message(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Post a top-level channel message (not threaded under another message)."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        channel = chat.channel(args["channelId"])
        sent = await channel.post(_to_postable(args["message"]))
        return {"messageId": sent.id, "threadId": sent.thread_id}

    return ChatTool(
        description=(
            "Post a top-level message to a channel (not threaded under an existing "
            "message). The channelId is the full id (e.g. 'slack:C123ABC')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channelId": {
                    "type": "string",
                    "description": "Full channel id including adapter prefix",
                },
                "message": _POSTABLE_INPUT_SCHEMA,
            },
            "required": ["channelId", "message"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def send_direct_message(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Open (or reuse) a 1:1 DM with a user and post in it."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        dm = await chat.open_dm(args["userId"])
        sent = await dm.post(_to_postable(args["message"]))
        return {"messageId": sent.id, "threadId": sent.thread_id}

    return ChatTool(
        description=(
            "Open (or reuse) a 1:1 direct-message conversation with a user and post "
            "a message in it. The userId format is platform-specific (e.g. 'U123456' "
            "for Slack, 'users/123' for Google Chat)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "userId": {
                    "type": "string",
                    "description": "Platform-specific user id; the adapter is auto-detected",
                },
                "message": _POSTABLE_INPUT_SCHEMA,
            },
            "required": ["userId", "message"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def edit_message(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Edit a previously posted message in a thread."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        result = await thread.adapter.edit_message(
            args["threadId"],
            args["messageId"],
            _to_postable(args["message"]),
        )
        return {"messageId": result.id, "threadId": result.thread_id}

    return ChatTool(
        description=(
            "Edit a previously posted message in a thread. Replaces the existing "
            "message body. Only messages the bot itself authored can be edited on "
            "most platforms."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "messageId": {
                    "type": "string",
                    "description": "Platform-specific message id of the message to edit",
                },
                "message": _POSTABLE_INPUT_SCHEMA,
            },
            "required": ["threadId", "messageId", "message"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def delete_message(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Delete a message from a thread."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.adapter.delete_message(args["threadId"], args["messageId"])
        return {
            "deleted": True,
            "messageId": args["messageId"],
            "threadId": args["threadId"],
        }

    return ChatTool(
        description=(
            "Delete a message from a thread. Only messages the bot itself authored can be deleted on most platforms."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "messageId": {
                    "type": "string",
                    "description": "Platform-specific message id of the message to delete",
                },
            },
            "required": ["threadId", "messageId"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


# ---------------------------------------------------------------------------
# reactions.ts
# ---------------------------------------------------------------------------


def add_reaction(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Add an emoji reaction to a specific message."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.adapter.add_reaction(args["threadId"], args["messageId"], args["emoji"])
        return {
            "added": True,
            "emoji": args["emoji"],
            "messageId": args["messageId"],
            "threadId": args["threadId"],
        }

    return ChatTool(
        description=(
            "Add an emoji reaction to a specific message. Use a well-known emoji "
            "name (e.g. 'thumbs_up', 'heart', 'check') or a platform-native shorthand."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "messageId": {
                    "type": "string",
                    "description": "Platform-specific message id to react to",
                },
                "emoji": {
                    "type": "string",
                    "description": ("Emoji name or platform shortcode (e.g. 'thumbs_up', 'white_check_mark')"),
                },
            },
            "required": ["threadId", "messageId", "emoji"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def remove_reaction(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Remove an emoji reaction the bot previously added."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.adapter.remove_reaction(args["threadId"], args["messageId"], args["emoji"])
        return {
            "removed": True,
            "emoji": args["emoji"],
            "messageId": args["messageId"],
            "threadId": args["threadId"],
        }

    return ChatTool(
        description="Remove an emoji reaction the bot previously added to a message.",
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "messageId": {
                    "type": "string",
                    "description": "Platform-specific message id to remove the reaction from",
                },
                "emoji": {
                    "type": "string",
                    "description": "Emoji name or platform shortcode previously added by the bot",
                },
            },
            "required": ["threadId", "messageId", "emoji"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


# ---------------------------------------------------------------------------
# threads.ts
# ---------------------------------------------------------------------------


_FETCH_DIRECTION_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["forward", "backward"],
    "default": "backward",
}


def fetch_messages(chat: ChatBinding) -> ChatTool:
    """Fetch recent messages from a thread, oldest-first within the page."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        limit = args.get("limit", 20)
        cursor = args.get("cursor")
        direction = args.get("direction", "backward")
        result = await thread.adapter.fetch_messages(
            args["threadId"],
            FetchOptions(limit=limit, cursor=cursor, direction=direction),
        )
        return {
            "messages": [_project_message(m) for m in result.messages],
            "nextCursor": result.next_cursor,
        }

    return ChatTool(
        description=(
            "Fetch recent messages from a thread, ordered chronologically (oldest "
            "first within the page). Use to read the conversation before responding."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Maximum number of messages to fetch",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous fetchMessages call",
                },
                "direction": {
                    **_FETCH_DIRECTION_SCHEMA,
                    "description": (
                        "'backward' (default) returns the most recent messages; 'forward' iterates from the oldest"
                    ),
                },
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


def fetch_channel_messages(chat: ChatBinding) -> ChatTool:
    """Fetch top-level messages in a channel (not thread replies)."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        channel_id: str = args["channelId"]
        adapter_name = channel_id.split(":")[0] if ":" in channel_id else ""
        adapter = chat.get_adapter(adapter_name) if adapter_name else None
        fetch_method = getattr(adapter, "fetch_channel_messages", None) if adapter is not None else None
        if fetch_method is None:
            raise ChatError(f'Adapter "{adapter_name}" does not support fetching channel messages')

        limit = args.get("limit", 20)
        cursor = args.get("cursor")
        direction = args.get("direction", "backward")
        try:
            result = await fetch_method(
                channel_id,
                FetchOptions(limit=limit, cursor=cursor, direction=direction),
            )
        except ChatNotImplementedError as exc:
            raise ChatError(f'Adapter "{adapter_name}" does not support fetching channel messages') from exc
        return {
            "messages": [_project_message(m) for m in result.messages],
            "nextCursor": result.next_cursor,
        }

    return ChatTool(
        description=(
            "Fetch top-level messages in a channel (not thread replies). Returns "
            "messages in chronological order within the page."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channelId": {"type": "string", "description": "Full channel id"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "cursor": {"type": "string"},
                "direction": _FETCH_DIRECTION_SCHEMA,
            },
            "required": ["channelId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


def fetch_thread(chat: ChatBinding) -> ChatTool:
    """Fetch metadata about a thread."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        info = await thread.adapter.fetch_thread(args["threadId"])
        return {
            "id": info.id,
            "channelId": info.channel_id,
            "channelName": info.channel_name,
            "channelVisibility": info.channel_visibility,
            "isDM": info.is_dm if info.is_dm is not None else False,
        }

    return ChatTool(
        description=("Fetch metadata about a thread (channel id, channel name, visibility, DM status, etc)."),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


def list_threads(chat: ChatBinding) -> ChatTool:
    """List recent threads in a channel."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        channel_id: str = args["channelId"]
        adapter_name = channel_id.split(":")[0] if ":" in channel_id else ""
        adapter = chat.get_adapter(adapter_name) if adapter_name else None
        list_method = getattr(adapter, "list_threads", None) if adapter is not None else None
        if list_method is None:
            raise ChatError(f'Adapter "{adapter_name}" does not support listing threads')

        limit = args.get("limit", 20)
        cursor = args.get("cursor")
        try:
            result = await list_method(channel_id, ListThreadsOptions(limit=limit, cursor=cursor))
        except ChatNotImplementedError as exc:
            raise ChatError(f'Adapter "{adapter_name}" does not support listing threads') from exc
        return {
            "threads": [
                {
                    "id": t.id,
                    "replyCount": t.reply_count,
                    "lastReplyAt": t.last_reply_at.isoformat() if t.last_reply_at else None,
                    "rootMessage": _project_message(t.root_message),
                }
                for t in result.threads
            ],
            "nextCursor": result.next_cursor,
        }

    return ChatTool(
        description=(
            "List recent threads in a channel. Returns lightweight summaries with the root message of each thread."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "channelId": {"type": "string", "description": "Full channel id"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "cursor": {"type": "string"},
            },
            "required": ["channelId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


def get_thread_participants(chat: ChatBinding) -> ChatTool:
    """Return the unique non-bot participants in a thread."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        participants = await thread.get_participants()
        return {"participants": [_project_author(p) for p in participants]}

    return ChatTool(
        description=(
            "Return the unique non-bot participants in a thread. Useful for "
            "deciding whether to subscribe (1:1) or stay quiet (group)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


def subscribe_thread(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Subscribe to all future messages in a thread."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.subscribe()
        return {"subscribed": True, "threadId": args["threadId"]}

    return ChatTool(
        description=(
            "Subscribe to all future messages in a thread. After subscribing, the "
            "bot will receive every message in this thread (not just @mentions)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {
                    "type": "string",
                    "description": "Full thread id to subscribe to",
                },
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def unsubscribe_thread(chat: ChatBinding, options: ToolOptions | None = None) -> ChatTool:
    """Unsubscribe from a thread."""
    opts = options or ToolOptions()

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.unsubscribe()
        return {"subscribed": False, "threadId": args["threadId"]}

    return ChatTool(
        description=("Unsubscribe from a thread. The bot will stop receiving non-mention messages in this thread."),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {
                    "type": "string",
                    "description": "Full thread id to unsubscribe from",
                },
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
        needs_approval=opts.needs_approval,
    )


def start_typing(chat: ChatBinding) -> ChatTool:
    """Show a typing indicator in a thread."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any]:
        thread = chat.thread(args["threadId"])
        await thread.start_typing(args.get("status"))
        return {"typing": True, "threadId": args["threadId"]}

    return ChatTool(
        description=(
            "Show a typing indicator in a thread. Use this when starting a "
            "long-running operation so users know the bot is working."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "threadId": {"type": "string", "description": "Full thread id"},
                "status": {
                    "type": "string",
                    "description": ("Optional human-readable status (some platforms display this, others ignore it)"),
                },
            },
            "required": ["threadId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


# ---------------------------------------------------------------------------
# users.ts
# ---------------------------------------------------------------------------


def get_user(chat: ChatBinding) -> ChatTool:
    """Look up profile information about a user by platform-specific id."""

    async def _execute(args: dict[str, Any]) -> dict[str, Any] | None:
        user = await chat.get_user(args["userId"])
        if not user:
            return None
        return {
            "userId": user.user_id,
            "userName": user.user_name,
            "fullName": user.full_name,
            "email": user.email,
            "isBot": user.is_bot,
            "avatarUrl": user.avatar_url,
        }

    return ChatTool(
        description=(
            "Look up profile information about a user by their platform-specific id "
            "(e.g. 'U123456' for Slack, '29:...' for Teams, 'users/123' for Google "
            "Chat). Returns null if the user is unknown."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "userId": {
                    "type": "string",
                    "description": "Platform-specific user id; the adapter is auto-detected",
                },
            },
            "required": ["userId"],
            "additionalProperties": False,
        },
        execute=_execute,
    )


# ---------------------------------------------------------------------------
# Orchestrator: createChatTools
# ---------------------------------------------------------------------------


def _resolve_approval(tool_name: str, config: ApprovalConfig) -> bool:
    if isinstance(config, bool):
        return config
    return config.get(tool_name, True)


def _resolve_preset_tools(preset: ChatToolPreset | list[ChatToolPreset]) -> set[str]:
    presets: list[str] = [preset] if isinstance(preset, str) else list(preset)
    tools: set[str] = set()
    for p in presets:
        if p not in _PRESET_TOOLS:
            raise ChatError(f'Unknown preset: "{p}"')
        for t in _PRESET_TOOLS[p]:
            tools.add(t)
    return tools


def _apply_overrides(tool: ChatTool, overrides: ToolOverrides | None) -> ChatTool:
    """Apply tool overrides while blocking attempts to replace core fields.

    Core fields — ``execute``, ``input_schema``, etc. — are filtered out so
    that overrides can never break tool semantics. ``description`` and
    ``needs_approval`` are first-class fields on :class:`ChatTool` and are
    applied directly; everything else is stashed in :attr:`ChatTool.extras`
    for downstream agent runtimes to pick up.
    """
    if not overrides:
        return tool

    safe = {k: v for k, v in overrides.items() if k not in _PROTECTED_TOOL_FIELDS}

    description = safe.pop("description", tool.description)
    needs_approval = safe.pop("needs_approval", safe.pop("needsApproval", tool.needs_approval))

    extras = {**tool.extras, **safe}
    return replace(
        tool,
        description=description,
        needs_approval=needs_approval,
        extras=extras,
    )


@dataclass
class ChatToolsOptions:
    """Options for :func:`create_chat_tools`. Mirrors upstream's ``ChatToolsOptions``."""

    chat: ChatBinding
    overrides: dict[str, ToolOverrides] | None = None
    preset: ChatToolPreset | list[ChatToolPreset] | None = None
    require_approval: ApprovalConfig = True


def create_chat_tools(
    chat: ChatBinding | None = None,
    *,
    preset: ChatToolPreset | list[ChatToolPreset] | None = None,
    require_approval: ApprovalConfig = True,
    overrides: dict[str, ToolOverrides] | None = None,
) -> dict[str, ChatTool]:
    """Create a set of Chat SDK tools for an AI agent.

    Mirrors upstream's ``createChatTools`` from ``chat/ai``: returns a dict
    keyed by ``ChatToolName`` (camelCase, matching upstream so consumers
    that bind to AI runtimes get the same tool ids).

    Each entry is built lazily so a preset filter skips both the
    ``approval`` lookup and the underlying tool construction for tools the
    agent will never see — same optimization as upstream.

    Parameters mirror upstream 1:1:

    chat:
        The :class:`~chat_sdk.chat.Chat` instance the tools dispatch
        operations against. **Required.**
    preset:
        Optional preset or list of presets to scope the returned toolset.
        Omit (or pass ``None``) to get every tool.
    require_approval:
        ``True`` (default) to require human approval for every write tool;
        ``False`` to disable approval globally; or a per-tool ``dict``
        where unspecified write tools default to ``True``.
    overrides:
        Per-tool overrides. Mirrors upstream's behaviour: core fields
        cannot be overridden (see :data:`_PROTECTED_TOOL_FIELDS`).
    """
    if chat is None:
        raise ChatError(
            "createChatTools requires a `chat` instance. Pass your `Chat({ ... })` instance as the `chat` option."
        )

    allowed: set[str] | None = _resolve_preset_tools(preset) if preset is not None else None

    def _approval(name: str) -> ToolOptions:
        return ToolOptions(needs_approval=_resolve_approval(name, require_approval))

    factories: dict[str, Callable[[], ChatTool]] = {
        "fetchMessages": lambda: fetch_messages(chat),
        "fetchChannelMessages": lambda: fetch_channel_messages(chat),
        "fetchThread": lambda: fetch_thread(chat),
        "listThreads": lambda: list_threads(chat),
        "getThreadParticipants": lambda: get_thread_participants(chat),
        "getChannelInfo": lambda: get_channel_info(chat),
        "getUser": lambda: get_user(chat),
        "startTyping": lambda: start_typing(chat),
        "postMessage": lambda: post_message(chat, _approval("postMessage")),
        "postChannelMessage": lambda: post_channel_message(chat, _approval("postChannelMessage")),
        "sendDirectMessage": lambda: send_direct_message(chat, _approval("sendDirectMessage")),
        "editMessage": lambda: edit_message(chat, _approval("editMessage")),
        "deleteMessage": lambda: delete_message(chat, _approval("deleteMessage")),
        "addReaction": lambda: add_reaction(chat, _approval("addReaction")),
        "removeReaction": lambda: remove_reaction(chat, _approval("removeReaction")),
        "subscribeThread": lambda: subscribe_thread(chat, _approval("subscribeThread")),
        "unsubscribeThread": lambda: unsubscribe_thread(chat, _approval("unsubscribeThread")),
    }

    result: dict[str, ChatTool] = {}
    overrides_map = overrides or {}
    for name, build in factories.items():
        if allowed is not None and name not in allowed:
            continue
        built = build()
        result[name] = _apply_overrides(built, overrides_map.get(name))
    return result


#: Alias matching upstream's ``ChatTools`` — the shape returned by
#: :func:`create_chat_tools`. Kept as an alias rather than a distinct type
#: so consumers can treat the result as a plain dict.
ChatTools = dict[str, ChatTool]


__all__ = [
    "ApprovalConfig",
    "ChatBinding",
    "ChatTool",
    "ChatToolName",
    "ChatToolPreset",
    "ChatTools",
    "ChatToolsOptions",
    "ChatWriteToolName",
    "ToolOptions",
    "ToolOverrides",
    "add_reaction",
    "create_chat_tools",
    "delete_message",
    "edit_message",
    "fetch_channel_messages",
    "fetch_messages",
    "fetch_thread",
    "get_channel_info",
    "get_thread_participants",
    "get_user",
    "list_threads",
    "post_channel_message",
    "post_message",
    "remove_reaction",
    "send_direct_message",
    "start_typing",
    "subscribe_thread",
    "unsubscribe_thread",
]
