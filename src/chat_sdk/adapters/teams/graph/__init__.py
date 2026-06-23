"""Microsoft Graph primitives for Teams — a runtime-free subpath.

Port of ``packages/adapter-teams/src/graph/`` (``client.ts``, ``channels.ts``,
``messages.ts``, ``types.ts``; NEW in chat@4.31.0, commit ``8c71411``), exposed
upstream as ``@chat-adapter/teams/graph``. Provides fetch-based primitives for
calling the Microsoft Graph REST API with a Graph-scoped Bot Framework token:
acquiring the token at the ``https://graph.microsoft.com/.default`` scope,
calling an arbitrary Graph endpoint, following ``@odata.nextLink`` pagination,
reading a channel's metadata, listing chat / channel messages and replies,
reading a single channel message, and extracting plain text from a Graph
message's HTML body — independent of the full Teams adapter, the
``microsoft_teams`` SDK, and the chat runtime.

The token-acquisition, response-body reading, and error type are reused from the
sibling ``chat_sdk.adapters.teams.api`` subpath (the cross-subpath import mirrors
upstream's ``import { ... } from "../api/client"``). Importing this module never
imports ``microsoft_teams`` or an HTTP client; the default ``fetch`` (inherited
from the api subpath) lazily imports ``httpx`` only when a request is actually
made, and any HTTP stack can be injected via the ``fetch`` parameter.

The injectable ``fetch`` is an async callable::

    async def fetch(url, *, method="GET", headers=None, body=None) -> response

where ``response`` exposes an ``int`` ``status`` (or ``status_code``), an
``ok`` flag (optional; derived from ``status`` when absent), and a ``text()``
method (sync or async) returning the raw body string.

Python-specific hardening (divergence from upstream, see ``docs/UPSTREAM_SYNC.md``
Known Non-Parity): :func:`call_teams_graph_api` gates an absolute ``path_or_url``
(and, transitively, each ``@odata.nextLink`` followed by :func:`paginate_teams_graph`)
through :func:`is_trusted_graph_url` **before** attaching the ``Bearer`` token,
refusing to forward the Graph token to any host outside ``graph.microsoft.com``
(SSRF / token-leak guard). Upstream attaches the token to whatever URL it is
handed — including an attacker-controlled ``@odata.nextLink`` — with no host
check.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from chat_sdk.adapters.teams.api import (
    TeamsApiError,
    TeamsCredentials,
    TeamsFetch,
    read_response_body,
    resolve_teams_access_token,
)
from chat_sdk.adapters.teams.api import (
    _default_fetch as _default_fetch,  # noqa: PLC2701 — reuse the api subpath's lazy-httpx transport
)
from chat_sdk.adapters.teams.api import (
    _response_ok as _response_ok,  # noqa: PLC2701 — reuse the api subpath's response protocol helpers
)
from chat_sdk.adapters.teams.api import (
    _response_status as _response_status,  # noqa: PLC2701
)

__all__ = [
    "GetTeamsChannelMessageOptions",
    "GetTeamsChannelOptions",
    "ListTeamsChannelMessagesOptions",
    "ListTeamsChatMessagesOptions",
    "ListTeamsMessageRepliesOptions",
    "TeamsChannelInfo",
    "TeamsGraphListOptions",
    "TeamsGraphListResult",
    "TeamsGraphMessage",
    "TeamsGraphOptions",
    "TeamsGraphUser",
    "call_teams_graph_api",
    "extract_text_from_graph_message",
    "get_teams_channel",
    "get_teams_channel_message",
    "is_trusted_graph_url",
    "list_teams_channel_messages",
    "list_teams_chat_messages",
    "list_teams_message_replies",
    "paginate_teams_graph",
    "resolve_graph_access_token",
    "to_graph_message",
]

_DEFAULT_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_DEFAULT_GRAPH_URL = "https://graph.microsoft.com/v1.0/"
_LEADING_SLASH_PATTERN = re.compile(r"^/+")

# SSRF allowlist (Python-first divergence from upstream). The Microsoft Graph
# API lives on a single host; the Graph-scoped bearer token must never be
# attached to any other host, including an attacker-supplied ``@odata.nextLink``.
# This is the API-host counterpart to the broader file-download allowlist in the
# high-level adapter (``teams/adapter.py`` ``_is_trusted_teams_download_url``,
# which permits the ``.graph.microsoft.com`` suffix and the exact
# ``graph.microsoft.com`` host); a Graph *API* call only ever targets the bare
# ``graph.microsoft.com`` host, so we pin to it exactly.
_TRUSTED_GRAPH_HOSTS = frozenset({"graph.microsoft.com"})


@dataclass
class TeamsGraphOptions:
    """Options common to every Graph call.

    ``credentials`` is reused from the api subpath; ``fetch`` injects an HTTP
    transport (defaulting to lazy ``httpx``); ``graph_url`` overrides the base
    Graph URL for relative paths (defaults to
    ``https://graph.microsoft.com/v1.0/``).
    """

    credentials: TeamsCredentials
    fetch: TeamsFetch | None = None
    graph_url: str | None = None


@dataclass
class TeamsGraphListOptions(TeamsGraphOptions):
    """:class:`TeamsGraphOptions` plus an optional ``$top`` page-size ``limit``."""

    limit: int | None = None


@dataclass
class GetTeamsChannelOptions(TeamsGraphOptions):
    """Options for :func:`get_teams_channel`."""

    # Defaulted so the dataclass can extend the optional-field base; both are
    # required in practice (``call_teams_graph_api`` encodes them into the path).
    channel_id: str = ""
    team_id: str = ""


@dataclass
class ListTeamsChatMessagesOptions(TeamsGraphListOptions):
    """Options for :func:`list_teams_chat_messages`."""

    chat_id: str = ""


@dataclass
class ListTeamsChannelMessagesOptions(TeamsGraphListOptions):
    """Options for :func:`list_teams_channel_messages`."""

    channel_id: str = ""
    team_id: str = ""


@dataclass
class ListTeamsMessageRepliesOptions(TeamsGraphListOptions):
    """Options for :func:`list_teams_message_replies`."""

    channel_id: str = ""
    message_id: str = ""
    team_id: str = ""


@dataclass
class GetTeamsChannelMessageOptions(TeamsGraphOptions):
    """Options for :func:`get_teams_channel_message`."""

    channel_id: str = ""
    message_id: str = ""
    team_id: str = ""


@dataclass
class TeamsGraphUser:
    """The ``from.user`` block of a Graph chat message."""

    display_name: str | None = None
    id: str | None = None
    user_identity_type: str | None = None


@dataclass
class TeamsGraphMessage:
    """A normalized Graph message (text extracted from the HTML body)."""

    id: str
    text: str
    raw: dict[str, Any]
    created_at: str | None = None
    from_: TeamsGraphUser | None = None
    reply_to_id: str | None = None


@dataclass
class TeamsGraphListResult:
    """A page of :class:`TeamsGraphMessage` items plus an optional cursor.

    ``cursor`` carries the raw ``@odata.nextLink`` (used as-is — never
    re-encoded — for :func:`paginate_teams_graph`).
    """

    items: list[TeamsGraphMessage]
    raw: dict[str, Any]
    cursor: str | None = None


@dataclass
class TeamsChannelInfo:
    """Channel metadata returned by :func:`get_teams_channel`."""

    id: str
    raw: dict[str, Any]
    display_name: str | None = None


def is_trusted_graph_url(url: str) -> bool:
    """Return ``True`` when ``url`` targets the Microsoft Graph API host.

    Python-first SSRF / token-leak guard (no upstream counterpart): only an
    ``https://`` URL whose host is exactly ``graph.microsoft.com`` is trusted to
    receive the Graph-scoped bearer token. Used to gate both an absolute
    ``path_or_url`` and a followed ``@odata.nextLink`` **before** the token is
    attached. Parse failures fail closed.
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host in _TRUSTED_GRAPH_HOSTS


async def resolve_graph_access_token(options: TeamsGraphOptions) -> str:
    """Resolve a Bot Framework token scoped for Microsoft Graph.

    Port of upstream ``resolveGraphAccessToken`` — delegates to the api subpath's
    :func:`resolve_teams_access_token` with the fixed Graph scope
    ``https://graph.microsoft.com/.default``.
    """
    return await resolve_teams_access_token(
        options.credentials,
        fetch=options.fetch,
        scope=_DEFAULT_GRAPH_SCOPE,
    )


async def call_teams_graph_api(path_or_url: str, options: TeamsGraphOptions) -> Any:
    """Call a Microsoft Graph endpoint with a Graph-scoped bearer token.

    Port of upstream ``callTeamsGraphApi``. An absolute ``path_or_url`` (one that
    :func:`urllib.parse.urlparse` reads as having a scheme or a host) is used
    as-is; otherwise it is joined onto the base Graph URL (``options.graph_url``
    or ``https://graph.microsoft.com/v1.0/``) after stripping leading slashes.
    Raises :class:`TeamsApiError` for non-2xx responses.

    Python-specific hardening: an absolute ``path_or_url`` is validated through
    :func:`is_trusted_graph_url` **before** the token is resolved or attached,
    raising :class:`ValueError` for any host other than ``graph.microsoft.com``.
    This is what gates a hostile ``@odata.nextLink`` followed via
    :func:`paginate_teams_graph`. Relative paths are always joined onto the
    trusted base URL, so they need no host check. Upstream performs no such
    check.
    """
    # Route on the PARSED form, not a case-sensitive ``startswith("http")`` prefix.
    # ``HTTPS://evil`` (mixed-case scheme) and ``//evil`` (scheme-relative) both slip
    # past a lowercase-prefix test, yet ``urljoin`` still resolves them to an absolute
    # attacker URL — so the routing diverged from ``is_trusted_graph_url`` and the Graph
    # token could attach to an untrusted host. Anything ``urlparse`` reads as having a
    # scheme or a netloc is treated as absolute and forced through the host allowlist;
    # a malformed URL (``urlparse`` raises, e.g. a bad IPv6 literal) fails closed the
    # same way. Only a truly relative path is joined onto the trusted base.
    try:
        parsed = urlparse(path_or_url)
        is_absolute = bool(parsed.scheme or parsed.netloc)
    except (ValueError, TypeError):
        is_absolute = True  # unparseable -> fail closed; the allowlist below rejects it
    if is_absolute:
        if not is_trusted_graph_url(path_or_url):
            raise ValueError(f"Refusing to call Microsoft Graph API on untrusted host: {path_or_url}")
        url = path_or_url
    else:
        url = urljoin(
            options.graph_url if options.graph_url is not None else _DEFAULT_GRAPH_URL,
            _LEADING_SLASH_PATTERN.sub("", path_or_url),
        )

    request = options.fetch if options.fetch is not None else _default_fetch
    token = await resolve_graph_access_token(options)

    response = await request(
        url,
        method="GET",
        headers={"authorization": f"Bearer {token}"},
    )
    body = await read_response_body(response)

    if not _response_ok(response):
        raise TeamsApiError(
            "Microsoft Graph request failed",
            body=body,
            status=_response_status(response),
        )

    return body


async def paginate_teams_graph(next_link: str, options: TeamsGraphOptions) -> Any:
    """Follow an ``@odata.nextLink`` page URL.

    Port of upstream ``paginateTeamsGraph``: the ``next_link`` is passed to
    :func:`call_teams_graph_api` **as-is** (never re-encoded) — and is therefore
    subject to the same host allowlist before the token attaches.
    """
    return await call_teams_graph_api(next_link, options)


async def get_teams_channel(options: GetTeamsChannelOptions) -> TeamsChannelInfo:
    """Read a channel's metadata via ``GET teams/{team}/channels/{channel}``.

    Port of upstream ``getTeamsChannel``. ``id`` falls back to the requested
    ``channel_id`` and ``display_name`` is omitted when Graph returns neither.
    """
    channel = await call_teams_graph_api(
        f"teams/{quote(options.team_id, safe='')}/channels/{quote(options.channel_id, safe='')}",
        options,
    )
    channel = channel if isinstance(channel, Mapping) else {}
    display_name = channel.get("displayName")
    channel_id = channel.get("id")
    return TeamsChannelInfo(
        id=channel_id if channel_id is not None else options.channel_id,
        raw=dict(channel),
        display_name=display_name if display_name else None,
    )


async def list_teams_chat_messages(
    options: ListTeamsChatMessagesOptions,
) -> TeamsGraphListResult:
    """List messages in a 1:1 / group chat via ``GET chats/{chat}/messages``."""
    result = await call_teams_graph_api(
        _with_top(f"chats/{quote(options.chat_id, safe='')}/messages", options.limit),
        options,
    )
    return _to_list_result(result)


async def list_teams_channel_messages(
    options: ListTeamsChannelMessagesOptions,
) -> TeamsGraphListResult:
    """List channel messages via ``GET teams/{team}/channels/{channel}/messages``."""
    result = await call_teams_graph_api(
        _with_top(
            f"teams/{quote(options.team_id, safe='')}/channels/{quote(options.channel_id, safe='')}/messages",
            options.limit,
        ),
        options,
    )
    return _to_list_result(result)


async def list_teams_message_replies(
    options: ListTeamsMessageRepliesOptions,
) -> TeamsGraphListResult:
    """List replies to a channel message via the ``/replies`` endpoint."""
    result = await call_teams_graph_api(
        _with_top(
            f"teams/{quote(options.team_id, safe='')}/channels/{quote(options.channel_id, safe='')}"
            f"/messages/{quote(options.message_id, safe='')}/replies",
            options.limit,
        ),
        options,
    )
    return _to_list_result(result)


async def get_teams_channel_message(
    options: GetTeamsChannelMessageOptions,
) -> TeamsGraphMessage:
    """Read a single channel message via its ``messages/{message}`` endpoint."""
    result = await call_teams_graph_api(
        f"teams/{quote(options.team_id, safe='')}/channels/{quote(options.channel_id, safe='')}"
        f"/messages/{quote(options.message_id, safe='')}",
        options,
    )
    return to_graph_message(result if isinstance(result, Mapping) else {})


def to_graph_message(message: Mapping[str, Any]) -> TeamsGraphMessage:
    """Normalize a raw Graph chat message into a :class:`TeamsGraphMessage`.

    Port of upstream ``toGraphMessage``: ``created_at``, ``from_`` and
    ``reply_to_id`` are populated only when present; ``id`` defaults to ``""``;
    ``text`` is extracted from the HTML body. ``raw`` is a shallow copy of the
    input.
    """
    raw = dict(message)
    message_id = message.get("id")
    from_block = message.get("from")
    user = from_block.get("user") if isinstance(from_block, Mapping) else None
    created_at = message.get("createdDateTime")
    reply_to_id = message.get("replyToId")
    return TeamsGraphMessage(
        id=message_id if message_id is not None else "",
        text=extract_text_from_graph_message(message),
        raw=raw,
        created_at=created_at if created_at else None,
        from_=_to_graph_user(user) if user else None,
        reply_to_id=reply_to_id if reply_to_id else None,
    )


def extract_text_from_graph_message(message: Mapping[str, Any]) -> str:
    """Convert a Graph message's HTML body to plain text.

    Port of upstream ``extractTextFromGraphMessage``: a single ordered regex
    pass — ``<at>`` mentions become ``@name``, ``<br>`` becomes a newline, a
    ``</p><p>`` boundary becomes a blank line, remaining tags are stripped, then
    the named entities are decoded. ``&amp;`` is decoded **last** so an encoded
    ``&lt;`` in the source never becomes ``<`` then gets mistaken for a tag, and
    a literal ``&amp;lt;`` decodes to ``&lt;`` rather than ``<``.
    """
    body = message.get("body")
    content = body.get("content") if isinstance(body, Mapping) else None
    if not content:
        return ""
    content = re.sub(r"<at\b[^>]*>(.*?)</at>", r"@\1", content, flags=re.IGNORECASE | re.DOTALL)
    content = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    content = re.sub(r"</p>\s*<p[^>]*>", "\n\n", content, flags=re.IGNORECASE)
    content = re.sub(r"<[^>]+>", "", content)
    content = content.replace("&nbsp;", " ")
    content = content.replace("&lt;", "<")
    content = content.replace("&gt;", ">")
    content = content.replace("&amp;", "&")
    return content.strip()


def _to_graph_user(user: Mapping[str, Any]) -> TeamsGraphUser:
    return TeamsGraphUser(
        display_name=user.get("displayName"),
        id=user.get("id"),
        user_identity_type=user.get("userIdentityType"),
    )


def _to_list_result(result: Any) -> TeamsGraphListResult:
    result = result if isinstance(result, Mapping) else {}
    next_link = result.get("@odata.nextLink")
    value = result.get("value")
    items_source = value if isinstance(value, list) else []
    return TeamsGraphListResult(
        items=[to_graph_message(item if isinstance(item, Mapping) else {}) for item in items_source],
        raw=dict(result),
        cursor=next_link if next_link else None,
    )


def _with_top(path: str, limit: int | None) -> str:
    """Append a ``$top`` query parameter when ``limit`` is truthy.

    Mirrors upstream ``withTop``: ``limit`` of ``0`` / ``None`` is a no-op
    (the truthiness here is intentional parity). The ``$top`` token is appended
    raw — the base path is already percent-encoded.
    """
    if not limit:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}$top={limit}"
