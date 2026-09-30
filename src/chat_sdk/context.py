"""Active-conversation tracking for handler dispatch.

Python port of upstream ``packages/chat/src/context.ts`` (vercel/chat#751,
#774). Upstream uses ``AsyncLocalStorage``; the Python equivalent is a
:class:`contextvars.ContextVar`. Internal: like upstream, this module is not
re-exported from the package root.

Task-boundary notes (see ``docs/UPSTREAM_SYNC.md``):

* ``asyncio.create_task`` copies the current context when the task is
  created, so :class:`~chat_sdk.chat.Chat` enters :func:`conversation`
  *inside* the scheduled coroutine that runs the handlers. Tasks a handler
  spawns inherit the conversation, the same as upstream's async-context
  propagation.
* The var is reset in ``finally`` in the same task that set it, so a handler
  that raises never leaks its conversation to later work on that task.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_conversation: ContextVar[str | None] = ContextVar("chat_sdk_active_conversation", default=None)


@contextmanager
def conversation(thread_id: str | None) -> Iterator[None]:
    """Mark ``thread_id`` as the conversation being handled for the block.

    Mirrors upstream ``runInConversation``. A ``None`` id runs the block bare
    (no set, no reset), so it inherits whatever conversation is already
    active; outside a handler that is none. This lets dispatch paths whose
    conversation id is optional (a modal with no related thread or channel)
    enter it unconditionally.
    """
    if thread_id is None:
        yield
        return
    token = _conversation.set(thread_id)
    try:
        yield
    finally:
        _conversation.reset(token)


def active_conversation() -> str | None:
    """The conversation being handled in the current context, if any.

    Returns ``None`` outside a handler, such as in a cron job or a script.
    Mirrors upstream ``activeConversation``.
    """
    return _conversation.get()


__all__ = ["active_conversation", "conversation"]
