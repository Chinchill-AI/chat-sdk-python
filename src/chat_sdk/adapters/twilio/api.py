"""Low-level Twilio REST API helpers (Messages, Calls, Media).

Python port of upstream ``adapter-twilio/src/api/index.ts``. Upstream
intentionally avoids the ``twilio`` npm runtime dependency so these
helpers stay runtime-light; the Python port mirrors that choice and
hand-rolls the small REST surface over an injectable HTTP transport
(:data:`~chat_sdk.adapters.twilio.types.TwilioHttpRequest`), defaulting
to a lazily imported aiohttp session per call. The adapter supplies its
own shared-session transport for connection reuse.

Error mapping (Python adaptation): upstream throws a bare
``TwilioApiError`` for every non-2xx response; here common statuses map
to the typed :mod:`chat_sdk.shared.errors` hierarchy (400 validation,
401 auth, 403 permission, 404 not-found, 429 rate-limit) and anything
else raises :class:`TwilioApiError` (a :class:`NetworkError` subclass
carrying upstream's ``status`` / ``body`` fields).
"""

from __future__ import annotations

import base64
import inspect
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlencode, urljoin

from chat_sdk.adapters.twilio.types import (
    ENV_ACCOUNT_SID,
    ENV_AUTH_TOKEN,
    TwilioCallResource,
    TwilioCredential,
    TwilioCredentials,
    TwilioFormFields,
    TwilioFormParams,
    TwilioHttpRequest,
    TwilioHttpResponse,
    TwilioMessageResource,
)
from chat_sdk.adapters.twilio.utils import encode_uri_component
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.shared.errors import (
    NetworkError as _NetworkError,
)

DEFAULT_API_URL = "https://api.twilio.com"


class TwilioApiError(_NetworkError):
    """A Twilio REST API call failed.

    Carries upstream's ``status`` / ``body`` fields. ``status`` is ``0``
    for transport-level failures that never produced an HTTP response.
    """

    def __init__(self, message: str, *, body: Any = None, status: int = 0) -> None:
        super().__init__("twilio", message)
        self.body = body
        self.status = status


@dataclass
class TwilioApiResponse:
    """Decoded response from :func:`call_twilio_api`."""

    body: Any
    ok: bool
    status: int


# =============================================================================
# Credentials
# =============================================================================


async def resolve_twilio_credential(value: TwilioCredential | None, env_name: str) -> str:
    """Resolve a credential: explicit value/resolver, else the env var.

    Raises :class:`~chat_sdk.shared.errors.AuthenticationError` naming the
    env var when nothing is configured (the typed mapping of upstream's
    ``TwilioApiError`` with status 0).
    """
    source: TwilioCredential | None = value if value is not None else os.environ.get(env_name)
    if not source:
        raise AuthenticationError("twilio", f"{env_name} is required")
    if callable(source):
        resolved = source()
        return await resolved if inspect.isawaitable(resolved) else resolved
    return source


def _basic_authorization(account_sid: str, auth_token: str) -> str:
    credentials = f"{account_sid}:{auth_token}".encode()
    return f"Basic {base64.b64encode(credentials).decode('ascii')}"


# =============================================================================
# Form encoding
# =============================================================================


def encode_twilio_form(fields: TwilioFormFields) -> TwilioFormParams:
    """Encode a field mapping as ordered form pairs.

    ``None`` values are omitted (hazard #7: omit absent optional keys);
    sequences append one pair per item; booleans serialize as JS would
    (``true`` / ``false``). Mirrors upstream ``encodeTwilioForm``.
    """
    params: TwilioFormParams = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, bool):
            params.append((key, "true" if value else "false"))
            continue
        if isinstance(value, str):
            params.append((key, value))
            continue
        if isinstance(value, Sequence):
            params.extend((key, str(item)) for item in value)
            continue
        params.append((key, str(value)))
    return params


def _parse_twilio_response_body(body: bytes) -> Any:
    """Decode a response body: empty -> None, JSON when valid, else text."""
    text = body.decode("utf-8", errors="replace")
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _twilio_error_message(body: Any, status: int) -> str:
    """Best error message: Twilio's body ``message`` field when present."""
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return f"Twilio API returned HTTP {status}"


def _raise_for_status(status: int, body: Any, *, action: str) -> None:
    """Translate a non-2xx Twilio response to a typed adapter error."""
    message = _twilio_error_message(body, status)
    if status == 429:
        raise AdapterRateLimitError("twilio")
    if status == 401:
        raise AuthenticationError("twilio", message)
    if status == 403:
        raise AdapterPermissionError("twilio", action)
    if status == 404:
        raise ResourceNotFoundError("twilio", "Twilio resource", action)
    if status == 400:
        raise ValidationError("twilio", message)
    raise TwilioApiError(f"Twilio API returned HTTP {status}", body=body, status=status)


# =============================================================================
# Transport
# =============================================================================


async def _default_http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: str | None,
) -> TwilioHttpResponse:
    """Default transport: one-shot aiohttp session (lazy optional import).

    Standalone helper calls pay a session per request; the adapter passes
    a shared-session transport instead (hazard #11).
    """
    import aiohttp

    async with (
        aiohttp.ClientSession() as session,
        session.request(method, url, headers=headers, data=body) as response,
    ):
        return TwilioHttpResponse(status=response.status, body=await response.read())


# =============================================================================
# Raw API call
# =============================================================================


async def call_twilio_api(
    path: str,
    *,
    api_base_url: str | None = None,
    api_url: str | None = None,
    body: TwilioFormFields | TwilioFormParams | None = None,
    credentials: TwilioCredentials | None = None,
    http_request: TwilioHttpRequest | None = None,
    method: Literal["DELETE", "GET", "POST"] | None = None,
    search: TwilioFormFields | TwilioFormParams | None = None,
) -> TwilioApiResponse:
    """Call the Twilio REST API with Basic auth and form encoding.

    Mirrors upstream ``callTwilioApi`` (the object/string dual signature
    collapses to ``path`` + keyword options in Python).
    """
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    auth_token = await resolve_twilio_credential(creds.auth_token, ENV_AUTH_TOKEN)

    base = api_url if api_url is not None else (api_base_url if api_base_url is not None else DEFAULT_API_URL)
    url = urljoin(base, path)
    search_params = _form_params(search)
    if search_params:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode(search_params)}"

    body_params = _form_params(body)
    encoded_body = urlencode(body_params) if body_params is not None else None
    headers = {"authorization": _basic_authorization(account_sid, auth_token)}
    if encoded_body is not None:
        headers["content-type"] = "application/x-www-form-urlencoded;charset=UTF-8"

    request = http_request if http_request is not None else _default_http_request
    resolved_method = method if method is not None else "POST"
    response = await request(resolved_method, url, headers, encoded_body)
    response_body = _parse_twilio_response_body(response.body)
    if not response.ok:
        _raise_for_status(response.status, response_body, action=f"{resolved_method} {path}")
    return TwilioApiResponse(body=response_body, ok=response.ok, status=response.status)


def _form_params(
    fields: TwilioFormFields | TwilioFormParams | None,
) -> TwilioFormParams | None:
    """Normalize form input: ``None`` passthrough, pairs kept, mappings encoded."""
    if fields is None:
        return None
    if isinstance(fields, list):
        return fields
    return encode_twilio_form(fields)


# =============================================================================
# Messages
# =============================================================================


async def send_twilio_message(
    *,
    to: str,
    api_base_url: str | None = None,
    api_url: str | None = None,
    body: str | None = None,
    credentials: TwilioCredentials | None = None,
    from_: str | None = None,
    http_request: TwilioHttpRequest | None = None,
    media_url: Sequence[str] | str | None = None,
    messaging_service_sid: str | None = None,
    status_callback_url: str | None = None,
) -> TwilioMessageResource:
    """Send an SMS/MMS via the Messages API."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    media_urls = _array_value(media_url)
    if not body and len(media_urls) == 0:
        raise ValidationError("twilio", "body or mediaUrl is required")
    if not (from_ or messaging_service_sid):
        raise ValidationError("twilio", "from or messagingServiceSid is required")
    form = encode_twilio_form(
        {
            "Body": body,
            "From": from_,
            "MediaUrl": media_urls,
            "MessagingServiceSid": messaging_service_sid,
            "StatusCallback": status_callback_url,
            "To": to,
        }
    )
    response = await call_twilio_api(
        f"/2010-04-01/Accounts/{_encode_path_segment(account_sid)}/Messages.json",
        api_base_url=api_base_url,
        api_url=api_url,
        body=form,
        credentials=creds,
        http_request=http_request,
    )
    return cast("TwilioMessageResource", response.body)


async def fetch_twilio_message(
    message_sid: str,
    *,
    api_base_url: str | None = None,
    api_url: str | None = None,
    credentials: TwilioCredentials | None = None,
    http_request: TwilioHttpRequest | None = None,
) -> TwilioMessageResource:
    """Fetch a single message resource by SID."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    response = await call_twilio_api(
        f"/2010-04-01/Accounts/{_encode_path_segment(account_sid)}/Messages/{_encode_path_segment(message_sid)}.json",
        api_base_url=api_base_url,
        api_url=api_url,
        credentials=creds,
        http_request=http_request,
        method="GET",
    )
    return cast("TwilioMessageResource", response.body)


async def delete_twilio_message(
    message_sid: str,
    *,
    api_base_url: str | None = None,
    api_url: str | None = None,
    credentials: TwilioCredentials | None = None,
    http_request: TwilioHttpRequest | None = None,
) -> None:
    """Delete a message resource by SID."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    await call_twilio_api(
        f"/2010-04-01/Accounts/{_encode_path_segment(account_sid)}/Messages/{_encode_path_segment(message_sid)}.json",
        api_base_url=api_base_url,
        api_url=api_url,
        credentials=creds,
        http_request=http_request,
        method="DELETE",
    )


async def list_twilio_messages(
    *,
    api_base_url: str | None = None,
    api_url: str | None = None,
    credentials: TwilioCredentials | None = None,
    from_: str | None = None,
    http_request: TwilioHttpRequest | None = None,
    limit: int | None = None,
    page_size: int | None = None,
    to: str | None = None,
) -> list[TwilioMessageResource]:
    """List message resources filtered by From/To, newest first."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    search = encode_twilio_form(
        {
            "From": from_,
            "PageSize": page_size,
            "To": to,
        }
    )
    response = await call_twilio_api(
        f"/2010-04-01/Accounts/{_encode_path_segment(account_sid)}/Messages.json",
        api_base_url=api_base_url,
        api_url=api_url,
        credentials=creds,
        http_request=http_request,
        method="GET",
        search=search,
    )
    body = response.body if isinstance(response.body, dict) else {}
    messages = body.get("messages")
    resources = cast("list[TwilioMessageResource]", messages if messages is not None else [])
    return resources[:limit]


# =============================================================================
# Calls
# =============================================================================


async def update_twilio_call(
    call_sid: str,
    *,
    api_base_url: str | None = None,
    api_url: str | None = None,
    credentials: TwilioCredentials | None = None,
    http_request: TwilioHttpRequest | None = None,
    method: Literal["GET", "POST"] | None = None,
    status: Literal["canceled", "completed"] | None = None,
    twiml: str | None = None,
    url: str | None = None,
) -> TwilioCallResource:
    """Update a live call with TwiML, a redirect URL, or a status."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    if not (twiml or url or status):
        raise ValidationError("twilio", "twiml, url, or status is required")
    if twiml and url:
        raise ValidationError("twilio", "twiml and url are mutually exclusive")
    form = encode_twilio_form(
        {
            "Method": method,
            "Status": status,
            "Twiml": twiml,
            "Url": url,
        }
    )
    response = await call_twilio_api(
        f"/2010-04-01/Accounts/{_encode_path_segment(account_sid)}/Calls/{_encode_path_segment(call_sid)}.json",
        api_base_url=api_base_url,
        api_url=api_url,
        body=form,
        credentials=creds,
        http_request=http_request,
    )
    return cast("TwilioCallResource", response.body)


# =============================================================================
# Media
# =============================================================================


async def fetch_twilio_media(
    url: str,
    *,
    credentials: TwilioCredentials | None = None,
    http_request: TwilioHttpRequest | None = None,
) -> bytes:
    """Download a (private) media URL with Basic auth, returning raw bytes."""
    creds = credentials if credentials is not None else TwilioCredentials()
    account_sid = await resolve_twilio_credential(creds.account_sid, ENV_ACCOUNT_SID)
    auth_token = await resolve_twilio_credential(creds.auth_token, ENV_AUTH_TOKEN)
    request = http_request if http_request is not None else _default_http_request
    headers = {"authorization": _basic_authorization(account_sid, auth_token)}
    response = await request("GET", url, headers, None)
    if not response.ok:
        body = _parse_twilio_response_body(response.body)
        _raise_for_status(response.status, body, action=f"GET {url}")
    return response.body


def _encode_path_segment(value: str) -> str:
    """``encodeURIComponent`` for path segments (SIDs are alphanumeric)."""
    return encode_uri_component(value)


def _array_value(value: Sequence[str] | str | None) -> list[str]:
    """Normalize an optional string-or-sequence into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)
