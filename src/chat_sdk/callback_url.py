"""Callback URL handling for buttons and modals.

Python port of callback-url.ts (vercel/chat#454).

When a button (or modal) carries a ``callback_url``, the SDK stores the URL
in the state adapter under a short random token at post time and rewrites
the button's ``value`` to an encoded token (``__cb:<token>``). When the
button is clicked, :meth:`Chat.process_action` decodes the token, restores
the original value for handlers, and POSTs the action payload to the stored
URL. Modal callback URLs are stored in the modal context and POSTed on
submit.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from chat_sdk.cards import ActionsElement, ButtonElement, CardChild, CardElement
from chat_sdk.errors import ChatError
from chat_sdk.types import StateAdapter

CALLBACK_TOKEN_PREFIX = "__cb:"
CALLBACK_CACHE_KEY_PREFIX = "chat:callback:"
CALLBACK_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days


# ---------------------------------------------------------------------------
# Result types (TS uses inline object literals; port rule #9: typed objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedCallbackValue:
    """Result of :func:`decode_callback_value`."""

    callback_token: str | None


@dataclass(frozen=True)
class ResolvedCallback:
    """A stored callback resolved from the state adapter."""

    url: str
    original_value: str | None = None


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
) -> ActionsElement:
    children: list[Any] = []
    for el in actions.get("children", []):
        if not isinstance(el, dict) or el.get("type") != "button" or not el.get("callback_url"):
            children.append(el)
            continue

        token = _generate_token()
        # Stored shape matches the TS SDK (`{url, originalValue}`) so state
        # written by either SDK resolves in both. `originalValue` is omitted
        # (not None) when the button has no value — hazard #7.
        stored: dict[str, Any] = {"url": el["callback_url"]}
        original_value = el.get("value")
        if original_value is not None:
            stored["originalValue"] = original_value
        await state_adapter.set(f"{CALLBACK_CACHE_KEY_PREFIX}{token}", stored, CALLBACK_TTL_MS)

        # Rebuild the button without `callback_url` (mirrors upstream's
        # explicit-key copy; keys absent on the source stay absent).
        processed: ButtonElement = {"type": "button", "id": el["id"], "label": el["label"]}
        if "style" in el:
            processed["style"] = el["style"]
        if "disabled" in el:
            processed["disabled"] = el["disabled"]
        processed["value"] = encode_callback_value(token)
        if "action_type" in el:
            processed["action_type"] = el["action_type"]
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
) -> list[CardChild]:
    result: list[CardChild] = []
    for child in children:
        if isinstance(child, dict) and child.get("type") == "actions":
            result.append(await _process_actions_element(child, state_adapter))  # type: ignore[arg-type]
        elif isinstance(child, dict) and child.get("type") == "section" and "children" in child:
            result.append({**child, "children": await _process_children(child["children"], state_adapter)})  # type: ignore[misc]
        else:
            result.append(child)
    return result


async def process_card_callback_urls(
    card: CardElement,
    state_adapter: StateAdapter,
) -> CardElement:
    """Replace ``callback_url`` buttons with encoded token values.

    Returns the *same* card object when no button carries a callback URL;
    otherwise returns a new card (the original is never mutated).
    """
    if not _has_callback_buttons(card.get("children", [])):
        return card

    return {**card, "children": await _process_children(card.get("children", []), state_adapter)}


# ---------------------------------------------------------------------------
# Resolution + POST (runs at click/submit time)
# ---------------------------------------------------------------------------


async def resolve_callback_url(
    token: str,
    state_adapter: StateAdapter,
) -> ResolvedCallback | None:
    """Look up a stored callback by token. Returns ``None`` when unknown."""
    stored = await state_adapter.get(f"{CALLBACK_CACHE_KEY_PREFIX}{token}")
    if not stored:
        return None
    if isinstance(stored, str):
        # Legacy format: the URL was stored as a bare string.
        return ResolvedCallback(url=stored)
    original_value = stored["originalValue"] if "originalValue" in stored else stored.get("original_value")
    return ResolvedCallback(url=stored.get("url"), original_value=original_value)


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
