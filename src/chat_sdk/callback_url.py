"""Callback URL handling for buttons and modals.

Python port of callback-url.ts (vercel/chat#454, vercel/chat#875).

When a button (or modal) carries a ``callback_url``, the SDK stores the URL
in the state adapter under a short random token at post time and rewrites
the button's ``value`` to an encoded token (``__cb:<token>``). When the
button is clicked, :meth:`Chat.process_action` decodes the token, restores
the original value for handlers, and POSTs the action payload to the stored
URL. Modal callback URLs are stored in the modal context and POSTed on
submit.

Since chat@4.40.0 (vercel/chat#875) a button token is bound to the button
that minted it (``actionId``) and to the conversation it was posted in
(``scope``: a thread or a channel). A click resolves the token only when
both match, and a resolved token is deleted, so each token is consumed at
most once.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Literal

from chat_sdk.cards import ActionsElement, ButtonElement, CardChild, CardElement
from chat_sdk.errors import ChatError
from chat_sdk.types import StateAdapter

CALLBACK_TOKEN_PREFIX = "__cb:"
CALLBACK_CACHE_KEY_PREFIX = "chat:callback:"
CALLBACK_TTL_MS = 7 * 24 * 60 * 60 * 1000  # 7 days
CALLBACK_LOCK_TTL_MS = 10_000


# ---------------------------------------------------------------------------
# Result types (TS uses inline object literals; port rule #9: typed objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedCallbackValue:
    """Result of :func:`decode_callback_value`."""

    callback_token: str | None


@dataclass(frozen=True)
class CallbackScope:
    """The conversation a callback token is bound to.

    Stored as ``{"id", "type"}``. A ``"thread"`` scope matches the clicked
    message's thread id; a ``"channel"`` scope matches the channel id the
    adapter derives from it (``channel_id_from_thread_id``).
    """

    id: str
    type: Literal["channel", "thread"]


@dataclass(frozen=True)
class CallbackContext:
    """Where a button click happened; matched against a stored callback."""

    action_id: str
    channel_id: str | None = None
    thread_id: str | None = None


@dataclass(frozen=True)
class ResolvedCallback:
    """A stored callback, resolved and consumed from the state adapter."""

    url: str
    original_value: str | None = None
    # Keyword-only so positional ``ResolvedCallback(url, original_value)``
    # keeps binding the same fields as before these were added.
    action_id: str = field(kw_only=True)
    scope: CallbackScope = field(kw_only=True)


@dataclass(frozen=True)
class CallbackPostResult:
    """Result of :func:`post_to_callback_url`."""

    error: Exception | None = None
    status: int | None = None


# ---------------------------------------------------------------------------
# Token encoding
# ---------------------------------------------------------------------------


def encode_callback_value(token: str) -> str:
    """Encode a callback token into a button value."""
    return f"{CALLBACK_TOKEN_PREFIX}{token}"


def decode_callback_value(value: str | None) -> DecodedCallbackValue:
    """Extract the callback token from an encoded button value, if any."""
    if not value or not value.startswith(CALLBACK_TOKEN_PREFIX):
        return DecodedCallbackValue(callback_token=None)
    return DecodedCallbackValue(callback_token=value[len(CALLBACK_TOKEN_PREFIX) :])


def _generate_token() -> str:
    # Upstream: crypto.randomUUID().replace(/-/g, "").slice(0, 16).
    # Port rule #12: the token gates where action payloads are POSTed, so
    # use `secrets` (16 hex chars = 64 bits, same shape as upstream).
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Card processing (runs at post time)
# ---------------------------------------------------------------------------


async def _process_actions_element(
    actions: ActionsElement,
    state_adapter: StateAdapter,
    scope: CallbackScope,
) -> ActionsElement:
    children: list[Any] = []
    for el in actions.get("children", []):
        if not isinstance(el, dict) or el.get("type") != "button" or not el.get("callback_url"):
            children.append(el)
            continue

        token = _generate_token()
        # Stored shape matches the TS SDK (`{actionId, url, originalValue?,
        # scope: {id, type}}`) so state written by either SDK resolves in
        # both. `originalValue` is omitted (not None) when the button has no
        # value — hazard #7.
        stored: dict[str, Any] = {"actionId": el["id"], "url": el["callback_url"]}
        original_value = el.get("value")
        if original_value is not None:
            stored["originalValue"] = original_value
        stored["scope"] = {"id": scope.id, "type": scope.type}
        await state_adapter.set(f"{CALLBACK_CACHE_KEY_PREFIX}{token}", stored, CALLBACK_TTL_MS)

        # Keep every other button field so new ones (like tooltip) are not
        # silently dropped; only the callback URL is replaced by the token.
        processed: ButtonElement = {k: v for k, v in el.items() if k != "callback_url"}  # type: ignore[assignment]
        processed["value"] = encode_callback_value(token)
        children.append(processed)
    return {"type": "actions", "children": children}


def _has_callback_buttons(children: list[CardChild]) -> bool:
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "actions":
            for el in child.get("children", []):
                if isinstance(el, dict) and el.get("type") == "button" and el.get("callback_url"):
                    return True
        if child.get("type") == "section" and "children" in child and _has_callback_buttons(child["children"]):
            return True
    return False


async def _process_children(
    children: list[CardChild],
    state_adapter: StateAdapter,
    scope: CallbackScope,
) -> list[CardChild]:
    result: list[CardChild] = []
    for child in children:
        if isinstance(child, dict) and child.get("type") == "actions":
            result.append(await _process_actions_element(child, state_adapter, scope))  # type: ignore[arg-type]
        elif isinstance(child, dict) and child.get("type") == "section" and "children" in child:
            result.append({**child, "children": await _process_children(child["children"], state_adapter, scope)})  # type: ignore[misc]
        else:
            result.append(child)
    return result


async def process_card_callback_urls(
    card: CardElement,
    state_adapter: StateAdapter,
    scope: CallbackScope,
) -> CardElement:
    """Replace ``callback_url`` buttons with encoded token values.

    Each minted token is bound to its button's ``id`` and to ``scope``.
    Returns the *same* card object when no button carries a callback URL;
    otherwise returns a new card (the original is never mutated).
    """
    if not _has_callback_buttons(card.get("children", [])):
        return card

    return {**card, "children": await _process_children(card.get("children", []), state_adapter, scope)}


# ---------------------------------------------------------------------------
# Resolution + POST (runs at click/submit time)
# ---------------------------------------------------------------------------


def _parse_stored_callback(stored: Any) -> ResolvedCallback | None:
    """Validate a stored record strictly; ``None`` for any other shape.

    Mirrors upstream's ``typeof`` checks (never truthiness): a legacy
    bare-string record, or one without ``actionId`` / ``scope``, is
    rejected. An empty-string ``actionId`` is still a string and passes.
    """
    if not isinstance(stored, dict):
        return None
    action_id = stored.get("actionId")
    url = stored.get("url")
    if not isinstance(action_id, str) or not isinstance(url, str):
        return None
    original_value = stored.get("originalValue")
    if "originalValue" in stored and not isinstance(original_value, str):
        return None
    scope = stored.get("scope")
    if not isinstance(scope, dict):
        return None
    scope_id = scope.get("id")
    if not isinstance(scope_id, str):
        return None
    raw_type = scope.get("type")
    scope_type: Literal["channel", "thread"]
    if raw_type == "channel":
        scope_type = "channel"
    elif raw_type == "thread":
        scope_type = "thread"
    else:
        return None
    return ResolvedCallback(
        url=url,
        original_value=original_value,
        action_id=action_id,
        scope=CallbackScope(id=scope_id, type=scope_type),
    )


async def resolve_callback_url(
    token: str,
    state_adapter: StateAdapter,
    context: CallbackContext | None = None,
) -> ResolvedCallback | None:
    """Resolve and consume a stored button callback.

    Returns ``None`` when the token is unknown or malformed, when a
    concurrent click holds the token's lock, or when the record does not
    match ``context`` (the clicked ``action_id`` and, depending on the
    stored scope, ``channel_id`` or ``thread_id``). A ``None`` context
    never matches. A matching record is deleted before returning, so each
    token resolves at most once; a mismatch leaves the record in place.
    """
    key = f"{CALLBACK_CACHE_KEY_PREFIX}{token}"
    # Lock keys live in their own namespace in every state backend, so
    # locking the value key does not touch the stored record.
    lock = await state_adapter.acquire_lock(key, CALLBACK_LOCK_TTL_MS)
    if lock is None:
        return None

    try:
        resolved = _parse_stored_callback(await state_adapter.get(key))
        if resolved is None or context is None:
            return None

        scope_id = context.channel_id if resolved.scope.type == "channel" else context.thread_id
        if resolved.action_id != context.action_id or resolved.scope.id != scope_id:
            return None

        await state_adapter.delete(key)
        return resolved
    finally:
        await state_adapter.release_lock(lock)


async def _fetch(url: str, *, method: str, headers: dict[str, str], body: str) -> tuple[int, str]:
    """POST ``body`` to ``url`` and return ``(status, text)``.

    Thin seam over aiohttp so tests can stub the network the way upstream
    stubs global ``fetch``. aiohttp is an optional dependency, so it is
    imported lazily (hazard #10); only http(s) URLs are supported, matching
    the WHATWG ``fetch`` upstream relies on.
    """
    import aiohttp

    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, data=body.encode("utf-8"), headers=headers) as response,
    ):
        try:
            text = await response.text()
        except Exception:
            # Mirrors upstream's `response.text().catch(() => "")`.
            text = ""
        return response.status, text


async def post_to_callback_url(
    callback_url: str,
    payload: dict[str, Any],
) -> CallbackPostResult:
    """POST a JSON payload to a callback URL.

    Never raises: network and HTTP errors are returned in
    :class:`CallbackPostResult` for the caller to log.
    """
    try:
        status, text = await _fetch(
            callback_url,
            method="POST",
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload),
        )
        if not 200 <= status < 300:
            return CallbackPostResult(
                error=ChatError(f"Callback URL returned {status}: {text}"),
                status=status,
            )
        return CallbackPostResult(status=status)
    except Exception as error:
        return CallbackPostResult(error=error)
