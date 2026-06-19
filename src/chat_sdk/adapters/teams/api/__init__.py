"""Microsoft Teams Bot Connector API primitives — a runtime-free subpath.

Port of ``packages/adapter-teams/src/api/`` (``client.ts``, ``messages.ts``,
``conversations.ts``, ``activities.ts``; NEW in chat@4.31.0, commit
``8c71411``), exposed upstream as ``@chat-adapter/teams/api``. Provides
fetch-based primitives for acquiring a Bot Framework access token
(``client_credentials`` grant), calling the Bot Connector REST API, posting /
updating / deleting messages, sending a typing indicator, creating a
conversation, and building a Teams message activity — independent of the full
Teams adapter, the ``microsoft_teams`` SDK, and the chat runtime.

Importing this module never imports ``microsoft_teams`` or an HTTP client; the
default ``fetch`` lazily imports ``httpx`` only when a request is actually
made, and any HTTP stack can be injected via the ``fetch`` parameter.

The injectable ``fetch`` is an async callable::

    async def fetch(url, *, method="GET", headers=None, body=None) -> response

where ``response`` exposes an ``int`` ``status`` (or ``status_code``), an
``ok`` flag (optional; derived from ``status`` when absent), and a ``text()``
method (sync or async) returning the raw body string. :class:`TeamsHttpResponse`
is the shape returned by the default fetch.

Python-specific hardening (divergence from upstream, see
``docs/UPSTREAM_SYNC.md`` Known Non-Parity): :func:`call_teams_connector_api`
gates ``serviceUrl`` through :func:`is_trusted_teams_service_url` **before**
attaching the ``Bearer`` token, refusing to forward the token to any host
outside the Microsoft Bot Framework allowlist (SSRF / token-leak guard).
Upstream attaches the token to whatever ``serviceUrl`` it is handed, with no
host check.
"""

from __future__ import annotations

import inspect
import json as _json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias
from urllib.parse import quote, urljoin, urlparse

__all__ = [
    "TEAMS_ADAPTIVE_CARD_CONTENT_TYPE",
    "TeamsActivity",
    "TeamsApiError",
    "TeamsApiResponse",
    "TeamsAttachment",
    "TeamsConversationMember",
    "TeamsCreatedConversation",
    "TeamsCredential",
    "TeamsCredentials",
    "TeamsFetch",
    "TeamsHttpResponse",
    "TeamsPostedMessage",
    "build_teams_message_activity",
    "build_teams_typing_activity",
    "call_teams_connector_api",
    "create_teams_conversation",
    "delete_teams_message",
    "is_trusted_teams_service_url",
    "post_teams_message",
    "read_response_body",
    "resolve_teams_access_token",
    "resolve_teams_credential",
    "send_teams_typing",
    "update_teams_message",
]

# A single credential value: a static string, or a zero-arg callable returning
# a ``str`` (sync) or an awaitable resolving to ``str``. Mirrors upstream
# ``type TeamsCredential = string | (() => Promise<string> | string)``. Declared
# locally (not imported from the adapter) so this subpath stays runtime-free.
TeamsCredential: TypeAlias = "str | Callable[[], str | Awaitable[str]]"

# Injectable HTTP transport — see the module docstring for the protocol.
TeamsFetch: TypeAlias = Callable[..., Awaitable[Any]]

TEAMS_ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"


# Sentinel distinguishing "argument omitted" from "explicitly passed None".
# Upstream JS distinguishes ``undefined`` (omit) from ``null`` (serialize) for
# ``body`` and ``channelData``; Python collapses both to ``None``, so a private
# sentinel preserves the distinction.
class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<UNSET>"


_UNSET: Any = _Unset()

_DEFAULT_BOT_SCOPE = "https://api.botframework.com/.default"
_DEFAULT_TENANT_ID = "botframework.com"
_LEADING_SLASH_PATTERN = re.compile(r"^/+")

# Allowed Microsoft Bot Framework service URL patterns (SSRF protection).
# Mirrors ``ALLOWED_SERVICE_URL_PATTERNS`` in the high-level Teams adapter
# (``teams/adapter.py``) — duplicated here (not imported) so this subpath never
# pulls in the adapter. Covers commercial, GCC, GCCH, DoD, and sovereign
# cloud endpoints.
_ALLOWED_SERVICE_URL_PATTERNS = [
    re.compile(r"^https://smba\.trafficmanager\.net/"),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.com/"),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.us/"),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.com/"),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.us/"),
    re.compile(r"^https://smba\.infra\.(gcc|gov)\.teams\.microsoft\.(com|us)/"),
]


class TeamsApiError(Exception):
    """Raised when a Teams API call fails (HTTP error or token failure)."""

    def __init__(
        self,
        message: str,
        *,
        body: Any = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.name = "TeamsApiError"
        self.body = body
        self.status = status


@dataclass
class TeamsCredentials:
    """Bot Framework credentials.

    Supply ``access_token`` for a pre-acquired bearer token (already scoped for
    the API it is used against), or ``app_id``/``app_password`` to have a token
    requested via the ``client_credentials`` grant. ``tenant_id`` defaults to
    ``botframework.com`` when omitted.
    """

    access_token: TeamsCredential | None = None
    app_id: TeamsCredential | None = None
    app_password: TeamsCredential | None = None
    tenant_id: TeamsCredential | None = None


@dataclass
class TeamsApiResponse:
    """A parsed Bot Connector API response."""

    body: Any
    ok: bool
    status: int


@dataclass
class TeamsHttpResponse:
    """Response shape returned by the default ``fetch`` implementation."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class TeamsAttachment:
    """A Teams activity attachment."""

    content_type: str
    content: Any = None
    content_url: str | None = None
    name: str | None = None


# A Teams Bot Framework activity payload (the wire shape posted to the
# Connector). Kept as a plain dict so callers can attach arbitrary
# ``[key: string]: unknown`` extension fields (matching upstream's index
# signature on ``TeamsActivity``).
TeamsActivity: TypeAlias = dict[str, Any]


@dataclass
class TeamsConversationMember:
    """A member referenced when creating a conversation."""

    id: str
    name: str | None = None


@dataclass
class TeamsCreatedConversation:
    """The result body of :func:`create_teams_conversation`."""

    id: str | None = None
    activity_id: str | None = None
    service_url: str | None = None


@dataclass
class TeamsPostedMessage:
    """Result of :func:`post_teams_message`."""

    id: str
    raw: Any


async def resolve_teams_credential(credential: TeamsCredential | None) -> str | None:
    """Resolve a static or callable (sync/async) credential to a string.

    Port of upstream ``resolveTeamsCredential`` — a callable is invoked each
    time, enabling rotation or lazy retrieval from a secret manager.
    """
    if callable(credential):
        resolved = credential()
        return await resolved if inspect.isawaitable(resolved) else resolved
    return credential


async def resolve_teams_access_token(
    credentials: TeamsCredentials,
    *,
    fetch: TeamsFetch | None = None,
    scope: str | None = None,
    token_url: str | None = None,
) -> str:
    """Resolve a Bot Framework access token.

    Returns ``credentials.access_token`` directly when supplied; otherwise
    requests one from Azure AD via the ``client_credentials`` grant using
    ``app_id``/``app_password`` (and ``tenant_id``, defaulting to
    ``botframework.com``). Raises :class:`TeamsApiError` when credentials are
    insufficient, the token request fails, or no ``access_token`` is returned.
    """
    direct_token = await resolve_teams_credential(credentials.access_token)
    # ``is not None`` would let an empty string through; upstream's truthiness
    # check (``if (directToken)``) treats "" as "fall back to client creds", so
    # we mirror the truthiness here intentionally.
    if direct_token:
        return direct_token

    app_id = await resolve_teams_credential(credentials.app_id)
    app_password = await resolve_teams_credential(credentials.app_password)
    resolved_tenant = await resolve_teams_credential(credentials.tenant_id)
    tenant_id = resolved_tenant if resolved_tenant is not None else _DEFAULT_TENANT_ID

    if not (app_id and app_password):
        raise TeamsApiError("Teams credentials require either accessToken or appId and appPassword")

    request = fetch if fetch is not None else _default_fetch
    resolved_token_url = (
        token_url
        if token_url is not None
        else f"https://login.microsoftonline.com/{quote(tenant_id, safe='')}/oauth2/v2.0/token"
    )
    body = _encode_form(
        {
            "client_id": app_id,
            "client_secret": app_password,
            "grant_type": "client_credentials",
            "scope": scope if scope is not None else _DEFAULT_BOT_SCOPE,
        }
    )

    response = await request(
        resolved_token_url,
        method="POST",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=body,
    )
    payload = await read_response_body(response)

    if not _response_ok(response):
        raise TeamsApiError(
            "Teams token request failed",
            body=payload,
            status=_response_status(response),
        )

    access_token = payload.get("access_token") if isinstance(payload, Mapping) else None
    if not isinstance(access_token, str) or len(access_token) == 0:
        raise TeamsApiError(
            "Teams token response did not include access_token",
            body=payload,
            status=_response_status(response),
        )

    return access_token


async def call_teams_connector_api(
    *,
    credentials: TeamsCredentials,
    path: str,
    service_url: str,
    body: Any = _UNSET,
    method: str | None = None,
    fetch: TeamsFetch | None = None,
) -> TeamsApiResponse:
    """Call the Bot Connector REST API with bearer auth.

    Resolves an access token (see :func:`resolve_teams_access_token`), joins
    ``path`` onto ``service_url``, and issues the request. Raises
    :class:`TeamsApiError` for non-2xx responses.

    Python-specific hardening: ``service_url`` is validated through
    :func:`is_trusted_teams_service_url` **before** the token is attached —
    refusing to forward the bearer token to a host outside the Microsoft Bot
    Framework allowlist (SSRF / token-leak guard). See ``docs/UPSTREAM_SYNC.md``
    Known Non-Parity. Upstream performs no host check.
    """
    if not is_trusted_teams_service_url(service_url):
        raise ValueError(f"Refusing to call Teams Connector API on untrusted serviceUrl: {service_url}")
    request = fetch if fetch is not None else _default_fetch
    token = await resolve_teams_access_token(credentials, fetch=fetch)
    url = urljoin(
        _ensure_trailing_slash(service_url),
        _LEADING_SLASH_PATTERN.sub("", path),
    )

    has_body = body is not _UNSET
    headers: dict[str, str] = {"authorization": f"Bearer {token}"}
    if has_body:
        headers["content-type"] = "application/json"

    response = await request(
        url,
        method=method if method is not None else "GET",
        headers=headers,
        body=None if not has_body else _json.dumps(body, separators=(",", ":")),
    )
    response_body = await read_response_body(response)

    if not _response_ok(response):
        raise TeamsApiError(
            "Teams Connector API request failed",
            body=response_body,
            status=_response_status(response),
        )

    return TeamsApiResponse(
        body=response_body,
        ok=_response_ok(response),
        status=_response_status(response),
    )


async def post_teams_message(
    *,
    credentials: TeamsCredentials,
    conversation_id: str,
    service_url: str,
    fetch: TeamsFetch | None = None,
    adaptive_card: Any = None,
    attachments: Sequence[TeamsAttachment] | None = None,
    channel_data: Any = _UNSET,
    markdown_text: str | None = None,
    reply_to_id: str | None = None,
    text: str | None = None,
) -> TeamsPostedMessage:
    """Post a message via the Bot Connector ``activities`` endpoint.

    When ``reply_to_id`` is supplied the activity is posted as a threaded reply.
    """
    activity = build_teams_message_activity(
        adaptive_card=adaptive_card,
        attachments=attachments,
        channel_data=channel_data,
        markdown_text=markdown_text,
        text=text,
    )
    encoded_conversation = quote(conversation_id, safe="")
    if reply_to_id:
        path = f"v3/conversations/{encoded_conversation}/activities/{quote(reply_to_id, safe='')}"
    else:
        path = f"v3/conversations/{encoded_conversation}/activities"
    response = await call_teams_connector_api(
        credentials=credentials,
        body=activity,
        method="POST",
        path=path,
        service_url=service_url,
        fetch=fetch,
    )

    posted_id = response.body.get("id") if isinstance(response.body, Mapping) else None
    return TeamsPostedMessage(
        id=posted_id if isinstance(posted_id, str) else "",
        raw=response.body,
    )


async def update_teams_message(
    *,
    credentials: TeamsCredentials,
    conversation_id: str,
    message_id: str,
    service_url: str,
    fetch: TeamsFetch | None = None,
    adaptive_card: Any = None,
    attachments: Sequence[TeamsAttachment] | None = None,
    channel_data: Any = _UNSET,
    markdown_text: str | None = None,
    text: str | None = None,
) -> TeamsApiResponse:
    """Update a previously posted message via ``PUT`` on its activity."""
    return await call_teams_connector_api(
        credentials=credentials,
        body=build_teams_message_activity(
            adaptive_card=adaptive_card,
            attachments=attachments,
            channel_data=channel_data,
            markdown_text=markdown_text,
            text=text,
        ),
        method="PUT",
        path=(f"v3/conversations/{quote(conversation_id, safe='')}/activities/{quote(message_id, safe='')}"),
        service_url=service_url,
        fetch=fetch,
    )


async def delete_teams_message(
    *,
    credentials: TeamsCredentials,
    conversation_id: str,
    message_id: str,
    service_url: str,
    fetch: TeamsFetch | None = None,
) -> None:
    """Delete a message via ``DELETE`` on its activity."""
    await call_teams_connector_api(
        credentials=credentials,
        method="DELETE",
        path=(f"v3/conversations/{quote(conversation_id, safe='')}/activities/{quote(message_id, safe='')}"),
        service_url=service_url,
        fetch=fetch,
    )


async def send_teams_typing(
    *,
    credentials: TeamsCredentials,
    conversation_id: str,
    service_url: str,
    fetch: TeamsFetch | None = None,
) -> TeamsApiResponse:
    """Send a typing indicator to a conversation."""
    return await call_teams_connector_api(
        credentials=credentials,
        body=build_teams_typing_activity(),
        method="POST",
        path=f"v3/conversations/{quote(conversation_id, safe='')}/activities",
        service_url=service_url,
        fetch=fetch,
    )


async def create_teams_conversation(
    *,
    credentials: TeamsCredentials,
    members: Sequence[TeamsConversationMember],
    service_url: str,
    fetch: TeamsFetch | None = None,
    bot: TeamsConversationMember | None = None,
    conversation_type: Literal["channel", "groupChat", "personal"] | None = None,
    is_group: bool | None = None,
    tenant_id: str | None = None,
) -> TeamsApiResponse:
    """Create a conversation via ``v3/conversations``."""
    body: dict[str, Any] = {}
    if bot is not None:
        body["bot"] = _member_to_wire(bot)
    if conversation_type is not None:
        body["conversationType"] = conversation_type
    body["isGroup"] = is_group if is_group is not None else False
    body["members"] = [_member_to_wire(member) for member in members]
    if tenant_id is not None:
        body["tenantId"] = tenant_id
    return await call_teams_connector_api(
        credentials=credentials,
        body=body,
        method="POST",
        path="v3/conversations",
        service_url=service_url,
        fetch=fetch,
    )


def build_teams_message_activity(
    *,
    adaptive_card: Any = None,
    attachments: Sequence[TeamsAttachment] | None = None,
    channel_data: Any = _UNSET,
    markdown_text: str | None = None,
    text: str | None = None,
) -> TeamsActivity:
    """Build a Teams ``message`` activity payload.

    ``markdown_text`` and ``text`` are mutually exclusive (raises
    :class:`TypeError`). An ``adaptive_card`` is prepended to the attachment
    list as an adaptive-card attachment. ``channel_data`` is included only when
    explicitly supplied (``None`` is a valid, serialized value).
    """
    if markdown_text and text:
        raise TypeError("markdownText cannot be combined with text")

    wire_attachments = [_attachment_to_wire(attachment) for attachment in (attachments or [])]
    if adaptive_card:
        wire_attachments.insert(
            0,
            {"content": adaptive_card, "contentType": TEAMS_ADAPTIVE_CARD_CONTENT_TYPE},
        )

    activity: TeamsActivity = {}
    if len(wire_attachments) > 0:
        activity["attachments"] = wire_attachments
    if channel_data is not _UNSET:
        activity["channelData"] = channel_data
    if markdown_text:
        activity["text"] = markdown_text
    if text:
        activity["text"] = text
    if markdown_text:
        activity["textFormat"] = "markdown"
    activity["type"] = "message"
    return activity


def build_teams_typing_activity() -> TeamsActivity:
    """Build a Teams ``typing`` activity payload."""
    return {"type": "typing"}


def is_trusted_teams_service_url(url: str) -> bool:
    """Gate Connector calls to known Microsoft Bot Framework service hosts.

    The bearer token must never be forwarded to an arbitrary ``serviceUrl`` — a
    crafted value could exfiltrate the bot's access token. This is a
    Python-first divergence: the upstream primitives perform no URL validation.
    See ``docs/UPSTREAM_SYNC.md`` Known Non-Parity. The allowlist mirrors the
    high-level adapter's ``ALLOWED_SERVICE_URL_PATTERNS``.
    """
    if not isinstance(url, str):
        return False
    # Reject obviously malformed URLs early; the regexes already pin scheme +
    # host shape, but parse-failures should fail closed rather than raise.
    try:
        urlparse(url)
    except (ValueError, TypeError):
        return False
    # The allowlist patterns require a ``/`` after the host (the host boundary).
    # ``call_teams_connector_api`` accepts (and normalizes) a slashless
    # ``serviceUrl``, so validate the same trailing-slash-normalized form — a
    # slashless trusted host must still pass, while a lookalike host followed by
    # extra labels (``…botframework.com.attacker.example``) still fails because
    # the ``.com/`` boundary never appears.
    candidate = _ensure_trailing_slash(url)
    return any(pattern.match(candidate) for pattern in _ALLOWED_SERVICE_URL_PATTERNS)


async def read_response_body(response: Any) -> Any:
    """Read a response body as JSON, falling back to raw text.

    Port of upstream ``readResponseBody``: an empty body returns ``None``
    (upstream ``undefined``); otherwise the text is JSON-parsed, falling back to
    the raw string when it is not valid JSON.
    """
    text = await _response_text(response)
    if text is None or len(text) == 0:
        return None
    try:
        return _json.loads(text)
    except (ValueError, TypeError):
        return text


def _member_to_wire(member: TeamsConversationMember) -> dict[str, Any]:
    wire: dict[str, Any] = {"id": member.id}
    if member.name is not None:
        wire["name"] = member.name
    return wire


def _attachment_to_wire(attachment: TeamsAttachment) -> dict[str, Any]:
    wire: dict[str, Any] = {"contentType": attachment.content_type}
    if attachment.content is not None:
        wire["content"] = attachment.content
    if attachment.content_url is not None:
        wire["contentUrl"] = attachment.content_url
    if attachment.name is not None:
        wire["name"] = attachment.name
    return wire


def _encode_form(values: Mapping[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(list(values.items()))


def _ensure_trailing_slash(value: str) -> str:
    return value if value.endswith("/") else f"{value}/"


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        raise TypeError("Teams fetch response must expose an int 'status' (or 'status_code')")
    return status


def _response_ok(response: Any) -> bool:
    ok = getattr(response, "ok", None)
    if isinstance(ok, bool):
        return ok
    return 200 <= _response_status(response) < 300


async def _response_text(response: Any) -> str | None:
    text_attr = getattr(response, "text", None)
    if callable(text_attr):
        value = text_attr()
        resolved = await value if inspect.isawaitable(value) else value
        # The ``text()`` protocol returns a string; coerce defensively so the
        # declared return type holds even for loosely-typed injected fetches.
        return None if resolved is None else str(resolved)
    if isinstance(text_attr, str):
        return text_attr
    # Some transports expose only ``json()``; round-trip it through a string so
    # ``read_response_body`` can parse uniformly.
    json_attr = getattr(response, "json", None)
    if callable(json_attr):
        value = json_attr()
        parsed = await value if inspect.isawaitable(value) else value
        return _json.dumps(parsed)
    return None


async def _default_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | str | None = None,
) -> TeamsHttpResponse:
    """Default HTTP transport. Lazily imports ``httpx`` (hazard #10)."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            url,
            headers=dict(headers) if headers is not None else None,
            content=body,
        )
        return TeamsHttpResponse(
            status=response.status_code,
            body=response.content,
            headers=dict(response.headers),
        )
