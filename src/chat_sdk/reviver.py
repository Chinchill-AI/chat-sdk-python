"""Standalone JSON reviver for Chat SDK objects.

Restores serialized Thread, Channel, and Message instances during
``json.loads(... object_hook=...)`` or :func:`rehydrate` without requiring a
:class:`~chat_sdk.chat.Chat` instance. This is useful in environments such as
Vercel Workflow functions where importing the full Chat instance (with its
adapter dependencies) is not possible.

Thread and Channel instances created this way use lazy adapter resolution —
the adapter is looked up from the Chat singleton when first accessed, so
``chat.register_singleton()`` (or ``chat.activate()``) must be called before
using methods like :meth:`Thread.post` (typically inside a step function).

Usage
-----
Python's ``json.loads`` has no direct equivalent of JS's ``JSON.parse``
reviver, but the same effect is achieved with ``object_hook``::

    import json
    from chat_sdk import reviver

    data = json.loads(payload, object_hook=reviver)
    await data["thread"].post("Hello from workflow!")

The function itself accepts a single dict and returns either the revived
object or the dict unchanged, matching the ``object_hook`` contract.
"""

from __future__ import annotations

from typing import Any

from chat_sdk.channel import ChannelImpl
from chat_sdk.thread import ThreadImpl
from chat_sdk.types import Message


def reviver(value: Any) -> Any:
    """Revive a Chat SDK object from its serialized dict representation.

    Compatible with :func:`json.loads` ``object_hook``. Returns ``value``
    unchanged for dicts without a recognized ``_type`` discriminator.
    """
    if isinstance(value, dict) and "_type" in value:
        t = value["_type"]
        if t == "chat:Thread":
            return ThreadImpl.from_json(value)
        if t == "chat:Channel":
            return ChannelImpl.from_json(value)
        if t == "chat:Message":
            return Message.from_json(value)
    return value
