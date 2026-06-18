"""Slack Web API primitives — a lightweight, runtime-free subpath.

Port of ``packages/adapter-slack/src/api/`` (``client.ts`` from
vercel/chat#548; ``extra.ts`` thread-reply and view helpers from
vercel/chat#559), exposed upstream as ``@chat-adapter/slack/api``.
Provides fetch-based primitives for calling Slack Web API methods,
posting and updating messages, sending response-URL payloads, uploading
files through Slack's external upload flow, fetching private Slack file
URLs with bearer auth, fetching thread replies, and opening modal views —
independent of the full Slack adapter, ``slack_sdk``, Socket Mode, and
the chat runtime. ``SlackBotToken`` is declared locally (rather than
imported from the adapter's ``types`` module) so importing this subpath
never pulls in the adapter, mirroring upstream's independent declaration
in ``api/client.ts``.

Importing this module never imports ``slack_sdk`` or an HTTP client; the
default ``fetch`` lazily imports ``httpx`` only when a request is actually
made, and any HTTP stack can be injected via the ``fetch`` parameter.

The injectable ``fetch`` is an async callable::

    async def fetch(url: str, *, method: str = "GET",
                    headers: Mapping[str, str] | None = None,
                    body: bytes | str | None = None) -> response

where ``response`` exposes an ``int`` ``status`` (or ``status_code``) and a
``json()`` method (sync or async). :class:`SlackHttpResponse` is the shape
returned by the default fetch.

Python-specific hardening (divergences from upstream, see
``docs/UPSTREAM_SYNC.md``): ``send_slack_response_url`` requires an
``https://*.slack.com`` URL and ``fetch_slack_file`` requires a trusted
Slack file host before forwarding the bearer token (SSRF / token-leak
guards mirroring the high-level adapter).
"""

from __future__ import annotations

import inspect
import json as _json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias
from urllib.parse import urlencode, urljoin, urlparse

__all__ = [
    "SlackApiError",
    "SlackApiResponse",
    "SlackBotToken",
    "SlackEncodedBody",
    "SlackFetch",
    "SlackFileUpload",
    "SlackHttpResponse",
    "SlackOpenViewResult",
    "SlackPostedMessage",
    "SlackThreadRepliesResult",
    "SlackUploadResult",
    "assert_slack_ok",
    "call_slack_api",
    "delete_slack_message",
    "encode_slack_api_body",
    "fetch_slack_file",
    "fetch_slack_thread_replies",
    "is_trusted_slack_file_url",
    "open_slack_view",
    "post_slack_ephemeral",
    "post_slack_message",
    "resolve_slack_bot_token",
    "send_slack_response_url",
    "update_slack_message",
    "upload_slack_files",
]

# Bot token configuration: a static string, or a zero-arg callable returning a
# ``str`` (sync) or an awaitable resolving to ``str``. The callable is invoked
# each time a token is needed, enabling rotation or lazy retrieval from a secret
# manager. Declared locally (rather than imported from the adapter's
# ``types`` module) so this subpath stays runtime-free — mirroring upstream's
# independent ``SlackBotToken`` declaration in ``api/client.ts``. Matches
# ``type SlackBotToken = string | (() => string | Promise<string>)``.
SlackBotToken: TypeAlias = "str | Callable[[], str | Awaitable[str]]"

# A parsed Slack Web API response body (``{"ok": bool, ...}``).
SlackApiResponse: TypeAlias = dict[str, Any]

# Injectable HTTP transport — see the module docstring for the protocol.
SlackFetch: TypeAlias = Callable[..., Awaitable[Any]]

_DEFAULT_API_URL = "https://slack.com/api/"


class SlackApiError(Exception):
    """Raised when a Slack Web API call fails (HTTP or ``ok: false``)."""

    def __init__(
        self,
        message: str,
        *,
        method: str,
        response: SlackApiResponse | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.response = response
        self.status = status


@dataclass
class SlackHttpResponse:
    """Response shape returned by the default ``fetch`` implementation."""

    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8")


@dataclass
class SlackEncodedBody:
    """An encoded Slack API request body and its content type."""

    body: str
    content_type: str


@dataclass
class SlackPostedMessage:
    """Result of posting, updating, or ephemeral-posting a message."""

    id: str
    raw: SlackApiResponse
    channel: str | None = None


@dataclass
class SlackFileUpload:
    """One file for :func:`upload_slack_files`."""

    data: bytes | bytearray | memoryview
    filename: str
    alt_text: str | None = None
    snippet_type: str | None = None
    title: str | None = None


@dataclass
class SlackUploadResult:
    """Result of :func:`upload_slack_files`."""

    file_ids: list[str]
    raw: SlackApiResponse


@dataclass
class SlackThreadRepliesResult:
    """Result of :func:`fetch_slack_thread_replies`."""

    messages: list[Any]
    raw: SlackApiResponse
    next_cursor: str | None = None


@dataclass
class SlackOpenViewResult:
    """Result of :func:`open_slack_view`."""

    raw: SlackApiResponse
    view: Any = None


async def resolve_slack_bot_token(token: SlackBotToken) -> str:
    """Resolve a static or callable (sync/async) bot token to a string."""
    if callable(token):
        resolved = token()
        return await resolved if inspect.isawaitable(resolved) else resolved
    return token


async def call_slack_api(
    method: str,
    body: Mapping[str, Any],
    *,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    content_type: Literal["form", "json"] = "form",
) -> SlackApiResponse:
    """POST a Slack Web API method with bearer auth and return the payload.

    Raises :class:`SlackApiError` for non-2xx HTTP responses. ``ok: false``
    payloads are returned as-is — pair with :func:`assert_slack_ok`.
    """
    resolved_token = await resolve_slack_bot_token(token)
    encoded = encode_slack_api_body(body, content_type)
    request = fetch if fetch is not None else _default_fetch
    url = urljoin(api_url if api_url is not None else _DEFAULT_API_URL, method)
    response = await request(
        url,
        method="POST",
        headers={
            "authorization": f"Bearer {resolved_token}",
            "content-type": encoded.content_type,
        },
        body=encoded.body,
    )
    payload = await _response_json(response)
    status = _response_status(response)
    if not 200 <= status < 300:
        raise SlackApiError(
            f"Slack {method} returned HTTP {status}",
            method=method,
            response=payload,
            status=status,
        )
    return payload


async def post_slack_message(
    *,
    channel: str,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    blocks: list[Any] | None = None,
    markdown_text: str | None = None,
    metadata: Any = None,
    reply_broadcast: bool | None = None,
    text: str | None = None,
    thread_ts: str | None = None,
    unfurl_links: bool | None = None,
    unfurl_media: bool | None = None,
) -> SlackPostedMessage:
    """Post a message via ``chat.postMessage``."""
    raw = await call_slack_api(
        "chat.postMessage",
        _slack_message_body(
            blocks=blocks,
            channel=channel,
            markdown_text=markdown_text,
            metadata=metadata,
            reply_broadcast=reply_broadcast,
            text=text,
            thread_ts=thread_ts,
            unfurl_links=unfurl_links,
            unfurl_media=unfurl_media,
        ),
        token=token,
        api_url=api_url,
        fetch=fetch,
    )
    assert_slack_ok("chat.postMessage", raw)
    return SlackPostedMessage(
        channel=_optional_string(raw.get("channel")),
        id=_string_value(raw.get("ts")),
        raw=raw,
    )


async def post_slack_ephemeral(
    *,
    channel: str,
    user: str,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    blocks: list[Any] | None = None,
    markdown_text: str | None = None,
    metadata: Any = None,
    reply_broadcast: bool | None = None,
    text: str | None = None,
    thread_ts: str | None = None,
    unfurl_links: bool | None = None,
    unfurl_media: bool | None = None,
) -> SlackPostedMessage:
    """Post an ephemeral message via ``chat.postEphemeral``."""
    body = _slack_message_body(
        blocks=blocks,
        channel=channel,
        markdown_text=markdown_text,
        metadata=metadata,
        reply_broadcast=reply_broadcast,
        text=text,
        thread_ts=thread_ts,
        unfurl_links=unfurl_links,
        unfurl_media=unfurl_media,
    )
    body["user"] = user
    raw = await call_slack_api("chat.postEphemeral", body, token=token, api_url=api_url, fetch=fetch)
    assert_slack_ok("chat.postEphemeral", raw)
    return SlackPostedMessage(
        channel=_optional_string(raw.get("channel")),
        id=_string_value(raw.get("message_ts")),
        raw=raw,
    )


async def update_slack_message(
    *,
    channel: str,
    ts: str,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    blocks: list[Any] | None = None,
    markdown_text: str | None = None,
    metadata: Any = None,
    reply_broadcast: bool | None = None,
    text: str | None = None,
    thread_ts: str | None = None,
    unfurl_links: bool | None = None,
    unfurl_media: bool | None = None,
) -> SlackPostedMessage:
    """Update a message via ``chat.update``."""
    body = _slack_message_body(
        blocks=blocks,
        channel=channel,
        markdown_text=markdown_text,
        metadata=metadata,
        reply_broadcast=reply_broadcast,
        text=text,
        thread_ts=thread_ts,
        unfurl_links=unfurl_links,
        unfurl_media=unfurl_media,
    )
    body["ts"] = ts
    raw = await call_slack_api("chat.update", body, token=token, api_url=api_url, fetch=fetch)
    assert_slack_ok("chat.update", raw)
    return SlackPostedMessage(
        channel=_optional_string(raw.get("channel")),
        id=_string_value(raw.get("ts")),
        raw=raw,
    )


async def delete_slack_message(
    *,
    channel: str,
    ts: str,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
) -> SlackApiResponse:
    """Delete a message via ``chat.delete``."""
    raw = await call_slack_api(
        "chat.delete",
        {"channel": channel, "ts": ts},
        token=token,
        api_url=api_url,
        fetch=fetch,
    )
    assert_slack_ok("chat.delete", raw)
    return raw


async def send_slack_response_url(
    url: str,
    *,
    fetch: SlackFetch | None = None,
    blocks: list[Any] | None = None,
    delete_original: bool | None = None,
    replace_original: bool | None = None,
    response_type: Literal["ephemeral", "in_channel"] | None = None,
    text: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """POST a JSON payload to a Slack interaction ``response_url``.

    Python-specific hardening: the URL must be ``https://*.slack.com``
    (response URLs always are) — refuses to POST elsewhere (SSRF guard,
    mirrors the high-level adapter).
    """
    _assert_slack_response_url(url)
    payload: dict[str, Any] = {}
    if blocks is not None:
        payload["blocks"] = blocks
    if delete_original is not None:
        payload["delete_original"] = delete_original
    if replace_original is not None:
        payload["replace_original"] = replace_original
    if response_type is not None:
        payload["response_type"] = response_type
    if text is not None:
        payload["text"] = text
    if thread_ts is not None:
        payload["thread_ts"] = thread_ts
    request = fetch if fetch is not None else _default_fetch
    response = await request(
        url,
        method="POST",
        headers={"content-type": "application/json"},
        body=_json.dumps(payload, separators=(",", ":")),
    )
    status = _response_status(response)
    if not 200 <= status < 300:
        raise SlackApiError(
            f"Slack response_url returned HTTP {status}",
            method="response_url",
            status=status,
        )


async def upload_slack_files(
    files: list[SlackFileUpload],
    *,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    channel_id: str | None = None,
    initial_comment: str | None = None,
    thread_ts: str | None = None,
) -> SlackUploadResult:
    """Upload files through Slack's external upload flow.

    ``files.getUploadURLExternal`` → raw POST per file →
    ``files.completeUploadExternal``.
    """
    if len(files) == 0:
        return SlackUploadResult(file_ids=[], raw={"ok": True})
    resolved_token = await resolve_slack_bot_token(token)
    request = fetch if fetch is not None else _default_fetch
    file_ids: list[str] = []
    for file in files:
        data = bytes(file.data)
        upload = await call_slack_api(
            "files.getUploadURLExternal",
            {
                "alt_txt": file.alt_text,
                "filename": file.filename,
                "length": len(data),
                "snippet_type": file.snippet_type,
            },
            token=token,
            api_url=api_url,
            fetch=fetch,
        )
        assert_slack_ok("files.getUploadURLExternal", upload)
        upload_url = _string_value(upload.get("upload_url"))
        file_id = _string_value(upload.get("file_id"))
        if not (upload_url and file_id):
            raise SlackApiError(
                "Slack files.getUploadURLExternal returned no upload URL",
                method="files.getUploadURLExternal",
                response=upload,
            )
        response = await request(
            upload_url,
            method="POST",
            headers={
                "authorization": f"Bearer {resolved_token}",
                "content-type": "application/octet-stream",
            },
            body=data,
        )
        status = _response_status(response)
        if not 200 <= status < 300:
            raise SlackApiError(
                f"Slack file upload returned HTTP {status}",
                method="files.upload",
                status=status,
            )
        file_ids.append(file_id)
    raw = await call_slack_api(
        "files.completeUploadExternal",
        {
            "channel_id": channel_id,
            "files": [
                {"id": file_id, "title": file.title if file.title is not None else file.filename}
                for file, file_id in zip(files, file_ids, strict=True)
            ],
            "initial_comment": initial_comment,
            "thread_ts": thread_ts,
        },
        token=token,
        api_url=api_url,
        fetch=fetch,
    )
    assert_slack_ok("files.completeUploadExternal", raw)
    return SlackUploadResult(file_ids=file_ids, raw=raw)


async def fetch_slack_file(
    *,
    url: str,
    token: SlackBotToken,
    fetch: SlackFetch | None = None,
) -> Any:
    """GET a private Slack file URL with bearer auth; returns the response.

    Python-specific hardening: the URL must pass
    :func:`is_trusted_slack_file_url` — refuses to forward the bot token to
    untrusted hosts (token-leak guard, mirrors the high-level adapter).
    """
    if not is_trusted_slack_file_url(url):
        raise ValueError(f"Refusing to fetch Slack file from untrusted URL: {url}")
    resolved_token = await resolve_slack_bot_token(token)
    request = fetch if fetch is not None else _default_fetch
    response = await request(
        url,
        method="GET",
        headers={"authorization": f"Bearer {resolved_token}"},
    )
    status = _response_status(response)
    if not 200 <= status < 300:
        raise SlackApiError(
            f"Slack file fetch returned HTTP {status}",
            method="files.fetch",
            status=status,
        )
    return response


async def fetch_slack_thread_replies(
    *,
    channel: str,
    ts: str,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    cursor: str | None = None,
    include_all_metadata: bool | None = None,
    inclusive: bool | None = None,
    latest: str | None = None,
    limit: int | None = None,
    oldest: str | None = None,
) -> SlackThreadRepliesResult:
    """Fetch a thread's replies via ``conversations.replies``.

    Returns the message list plus the ``next_cursor`` from
    ``response_metadata`` (``None`` when absent or empty) for pagination.
    """
    raw = await call_slack_api(
        "conversations.replies",
        {
            "channel": channel,
            "cursor": cursor,
            "include_all_metadata": include_all_metadata,
            "inclusive": inclusive,
            "latest": latest,
            "limit": limit,
            "oldest": oldest,
            "ts": ts,
        },
        token=token,
        api_url=api_url,
        fetch=fetch,
    )
    assert_slack_ok("conversations.replies", raw)
    messages = raw.get("messages")
    return SlackThreadRepliesResult(
        messages=messages if isinstance(messages, list) else [],
        next_cursor=_next_cursor(raw),
        raw=raw,
    )


async def open_slack_view(
    *,
    view: Any,
    token: SlackBotToken,
    api_url: str | None = None,
    fetch: SlackFetch | None = None,
    interactivity_pointer: str | None = None,
    trigger_id: str | None = None,
) -> SlackOpenViewResult:
    """Open a modal/view via ``views.open``.

    Requires a ``trigger_id`` or ``interactivity_pointer``.
    """
    if not (trigger_id or interactivity_pointer):
        raise TypeError("trigger_id or interactivity_pointer is required")
    raw = await call_slack_api(
        "views.open",
        {
            "interactivity_pointer": interactivity_pointer,
            "trigger_id": trigger_id,
            "view": view,
        },
        token=token,
        api_url=api_url,
        fetch=fetch,
    )
    assert_slack_ok("views.open", raw)
    return SlackOpenViewResult(raw=raw, view=raw.get("view"))


def encode_slack_api_body(
    body: Mapping[str, Any],
    content_type: Literal["form", "json"] = "form",
) -> SlackEncodedBody:
    """Encode an API body as form-urlencoded (default) or JSON.

    ``None`` values are omitted (TS ``undefined``/``null``). Non-scalar
    form values are JSON-encoded the way Slack expects (e.g. ``blocks``).
    """
    if content_type == "json":
        return SlackEncodedBody(
            body=_json.dumps({key: value for key, value in body.items() if value is not None}, separators=(",", ":")),
            content_type="application/json",
        )
    pairs: list[tuple[str, str]] = []
    for key, value in body.items():
        if value is None:
            continue
        pairs.append((key, _encode_slack_api_value(value)))
    return SlackEncodedBody(
        body=urlencode(pairs),
        content_type="application/x-www-form-urlencoded",
    )


def assert_slack_ok(method: str, response: SlackApiResponse) -> None:
    """Raise :class:`SlackApiError` unless ``response["ok"] is True``."""
    if response.get("ok") is not True:
        error = response.get("error")
        if error is None:
            error = "unknown_error"
        raise SlackApiError(
            f"Slack {method} failed: {error}",
            method=method,
            response=response,
        )


def is_trusted_slack_file_url(url: str) -> bool:
    """Gate Slack file downloads to known Slack-owned hosts.

    Bearer tokens must never be forwarded to an arbitrary URL — a crafted
    value could exfiltrate the workspace bot token. This is a Python-first
    divergence: the upstream primitives do not validate the URL. See
    ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # Exact-match hosts
    if host in {"files.slack.com", "slack.com"}:
        return True
    # Suffix match for Slack-owned subdomains
    return host.endswith(".slack.com") or host.endswith(".slack-edge.com")


def _slack_message_body(
    *,
    blocks: list[Any] | None,
    channel: str,
    markdown_text: str | None,
    metadata: Any,
    reply_broadcast: bool | None,
    text: str | None,
    thread_ts: str | None,
    unfurl_links: bool | None,
    unfurl_media: bool | None,
) -> dict[str, Any]:
    if markdown_text is not None and (text is not None or blocks is not None):
        raise TypeError("markdown_text cannot be used with text or blocks")
    return {
        "blocks": blocks,
        "channel": channel,
        "markdown_text": markdown_text,
        "metadata": metadata,
        "reply_broadcast": reply_broadcast,
        "text": text,
        "thread_ts": thread_ts,
        "unfurl_links": unfurl_links,
        "unfurl_media": unfurl_media,
    }


def _assert_slack_response_url(url: str) -> None:
    parsed = urlparse(url)
    if not (parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".slack.com")):
        raise ValueError(f"Invalid response_url: must be https://*.slack.com, got {url}")


def _encode_slack_api_value(value: Any) -> str:
    if isinstance(value, bool):
        # JS String(true) -> "true"; Slack's form API expects lowercase.
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return _json.dumps(value, separators=(",", ":"))


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "status_code", None)
    if not isinstance(status, int):
        raise TypeError("Slack fetch response must expose an int 'status' (or 'status_code')")
    return status


async def _response_json(response: Any) -> Any:
    value = response.json()
    return await value if inspect.isawaitable(value) else value


async def _default_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    body: bytes | str | None = None,
) -> SlackHttpResponse:
    """Default HTTP transport. Lazily imports ``httpx`` (hazard #10)."""
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.request(
            method,
            url,
            headers=dict(headers) if headers is not None else None,
            content=body,
        )
        return SlackHttpResponse(
            status=response.status_code,
            body=response.content,
            headers=dict(response.headers),
        )


def _string_value(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _next_cursor(response: SlackApiResponse) -> str | None:
    metadata = response.get("response_metadata")
    cursor = metadata.get("next_cursor") if isinstance(metadata, dict) else None
    return cursor if isinstance(cursor, str) and len(cursor) > 0 else None
