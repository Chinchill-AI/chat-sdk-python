"""Conversation scope guard for Chat SDK agent tools.

Python port of upstream ``packages/chat/src/ai/scope.ts`` (vercel/chat#751,
#774, #875). Tools built by :func:`~chat_sdk.ai.tools.create_chat_tools`
call the guard on the thread or channel id the model supplied *before*
touching the platform, so a prompt-injected agent cannot read or post
outside the conversation it is serving.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol

from chat_sdk.context import active_conversation
from chat_sdk.errors import ChatError

if TYPE_CHECKING:
    from chat_sdk.chat import Chat


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


#: The conversation a toolset is confined to: the ``Thread`` or ``Channel``
#: the agent is running in (anything with an ``id``), or a raw thread/channel
#: id. Mirrors upstream ``ReadScope``.
ReadScope = str | _HasId

#: Raises :class:`~chat_sdk.errors.ChatError` when ``id`` resolves outside the
#: scoped conversation. Mirrors upstream ``ScopeGuard``.
ScopeGuard = Callable[[str], None]


def _channel_of(chat: Chat, id: str) -> str:
    prefix = id.split(":")[0]
    adapter: Any = chat.get_adapter(prefix) if prefix else None
    channel_id_from_thread_id = getattr(adapter, "channel_id_from_thread_id", None) if adapter is not None else None
    if channel_id_from_thread_id is not None:
        channel = channel_id_from_thread_id(id)
        if channel is not None:
            return channel
    return ":".join(id.split(":")[:2])


def create_scope_guard(
    chat: Chat,
    scope: ReadScope | Literal[False] | None,
    strict: bool = False,
) -> ScopeGuard | None:
    """Build the guard tools call before touching the platform.

    Read tools and thread/channel-targeting write tools both run it on their
    target id.

    The scope resolves per call: an explicit one wins, and a toolset built
    without one inherits the conversation currently being handled. Returns
    ``None`` only when the caller opted out with ``scope=False``.

    By default a call is allowed when it resolves to the same channel as the
    scoped conversation, so a thread scope still permits sibling threads in
    its channel. Pass ``strict`` to confine a thread scope to that thread
    alone, rejecting sibling threads and the parent channel; a channel scope
    is unaffected and still allows any thread within it.

    When no scope resolves (no explicit scope and no conversation being
    handled, e.g. a cron job or queued work), tools run workspace-wide but
    warn once per guard.
    """
    if scope is False:
        return None

    explicit: str | None = None
    if scope is not None:
        explicit = scope if isinstance(scope, str) else scope.id

    # Warned-once flag lives in the closure (per guard), as upstream does.
    warned_unscoped = False

    def guard(id: str) -> None:
        nonlocal warned_unscoped
        # Upstream: ``explicit ?? activeConversation()``.
        active = explicit if explicit is not None else active_conversation()
        if not active:
            if not warned_unscoped:
                warned_unscoped = True
                chat.get_logger().warn(
                    f'Agent tool ran unscoped: "{id}" was accessed workspace-wide because no conversation '
                    "is being handled and no `scope` was set. Pass `scope` to createChatTools to confine tools."
                )
            return

        active_channel = _channel_of(chat, active)
        target_channel = _channel_of(chat, id)

        # Same channel is always required. Under strict a thread scope
        # additionally requires the exact scoped conversation, so both sibling
        # threads and the parent channel are rejected: on per-thread-ACL
        # platforms the channel is the widest surface available (a GitHub
        # channel is the whole repo). A channel scope is unaffected.
        same_channel = target_channel == active_channel
        scope_is_channel = active == active_channel
        in_scope = same_channel and (not strict or scope_is_channel or id == active)

        if not in_scope:
            raise ChatError(f'Tool call blocked: tools are scoped to "{active}", but "{id}" resolves outside it.')

    return guard


__all__ = ["ReadScope", "ScopeGuard", "create_scope_guard"]
