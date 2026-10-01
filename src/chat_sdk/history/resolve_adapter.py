"""Adapter resolution helpers for the History API.

Python port of ``history/resolve-adapter.ts``.
"""

from __future__ import annotations

from typing import Any

from chat_sdk.errors import ChatError
from chat_sdk.types import Adapter, AdapterResolver, BaseAdapter


def require_adapter(get_adapter: AdapterResolver, id: str, scope: str) -> Adapter:
    """Resolve the adapter named by a thread or channel ID prefix
    (``{adapter}:...``).

    Raises :class:`~chat_sdk.errors.ChatError` when the prefix is missing or
    no adapter is registered under that name. A typo'd or unregistered
    adapter must fail loudly here — falling through to an empty result would
    let callers (including AI tools) mistake a misconfiguration for an empty
    conversation.
    """
    adapter_name = id.split(":")[0]
    if not adapter_name:
        raise ChatError(f'{scope}: cannot resolve adapter from ID "{id}" — expected format "{{adapter}}:..."')
    adapter = get_adapter(adapter_name)
    if adapter is None:
        raise ChatError(f'{scope}: no adapter registered with name "{adapter_name}"')
    return adapter


def persists_history(adapter: Adapter) -> bool:
    """Whether an adapter's message history lives in the SDK-side thread
    history cache rather than on the platform (e.g. Telegram, WhatsApp).

    Mirrors the gating ``Chat`` uses when wiring the cache into threads.
    """
    return bool(getattr(adapter, "persist_thread_history", None) or getattr(adapter, "persist_message_history", None))


def optional_method(adapter: Adapter, name: str) -> Any:
    """Return the adapter's optional method ``name``, or ``None`` when absent.

    Upstream tests ``if (adapter.fetchChannelMessages)``. In Python an adapter
    that subclasses :class:`~chat_sdk.types.BaseAdapter` always has the
    attribute: the base stub raises ``ChatNotImplementedError``. An
    unoverridden stub therefore counts as absent, so a persisting adapter
    still reaches the channel-cache fallback exactly as upstream.
    """
    method = getattr(adapter, name, None)
    if method is None:
        return None
    stub = getattr(BaseAdapter, name, None)
    if stub is not None and getattr(method, "__func__", None) is stub:
        return None
    return method
