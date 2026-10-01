"""Inbound Teams attachment retrieval.

Python port of ``packages/adapter-teams/src/attachments.ts`` (vercel/chat
``3c37cfbc`` #749, chat@4.36.0, and ``bb926884`` #850, chat@4.39.0).

An attachment is fetched one of two ways:

- **bot**: an inline attachment (a pasted image) whose URL is on the
  activity's own connector origin. These need the bot token, so
  ``fetch_metadata`` records ``{"url", "auth": "bot", "connectorOrigin"}``
  and the fetch re-checks the URL against that origin before the token is
  sent.
- **anonymous**: everything else, including file cards
  (``file.download.info``), whose pre-signed ``content.downloadUrl`` needs
  no credentials. These go through the shared guarded downloader.

``fetch_metadata`` keys stay camelCase for cross-SDK state compatibility.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from chat_sdk.adapters.teams.api import _LOOPBACK_HOST_PATTERN, _URL_AMBIGUOUS_CHARS
from chat_sdk.shared.download import DEFAULT_LIMIT, DEFAULT_TIMEOUT_MS, download_attachment, read_attachment_body
from chat_sdk.shared.errors import NetworkError
from chat_sdk.types import Attachment

FILE_DOWNLOAD_INFO_CONTENT_TYPE = "application/vnd.microsoft.teams.file.download.info"
BOT_TOKEN_REFUSAL = "Refusing to send a bot token to an untrusted attachment URL"
FILE_MIME_TYPES: dict[str, str] = {
    "apng": "image/apng",
    "avif": "image/avif",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
    "txt": "text/plain",
    "webp": "image/webp",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

FetchData = Callable[[], Awaitable[bytes]]


@dataclass(frozen=True)
class TeamsAttachmentFetchers:
    """How the adapter fetches each kind of attachment (upstream ``TeamsAttachmentFetchers``)."""

    create_anonymous_fetch_data: Callable[[str], FetchData]
    fetch_authenticated: Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class _Retrieval:
    mode: Literal["anonymous", "bot", "rejected"]
    url: str
    connector_origin: str | None = None


def _infer_file_mime_type(name: Any, file_type: str | None) -> str:
    extension = file_type or (name.split(".")[-1] if isinstance(name, str) and name else "")
    extension = extension.lower()
    if extension.startswith("."):
        extension = extension[1:]
    return FILE_MIME_TYPES.get(extension, "application/octet-stream")


def _connector_origin(url: Any) -> str | None:
    """Return the origin of ``url`` when it can carry the bot token.

    ``https``, or plain ``http`` on loopback (the Bot Framework Emulator
    serves the connector there). Scheme and host compare lowercased and a
    non-default port is kept, as WHATWG ``URL.origin`` does. URLs where
    parsers disagree about the host (userinfo, whitespace, control
    characters, backslashes) have no origin, so they never get the token.
    """
    if not isinstance(url, str) or not url or _URL_AMBIGUOUS_CHARS.search(url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    # ``hostname`` is lowercased and has IPv6 brackets stripped.
    host = parts.hostname
    if not host or "@" in parts.netloc:
        return None
    scheme = parts.scheme.lower()
    if scheme == "https":
        default_port = 443
    elif scheme == "http" and _LOOPBACK_HOST_PATTERN.fullmatch(host):
        default_port = 80
    else:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def _is_trusted_bot_attachment_url(url: str, connector_origin: Any) -> bool:
    origin = _connector_origin(url)
    return origin is not None and origin == connector_origin


def _create_fetch_data_fn(retrieval: _Retrieval, fetchers: TeamsAttachmentFetchers) -> FetchData:
    if retrieval.mode == "anonymous":
        return fetchers.create_anonymous_fetch_data(retrieval.url)

    async def fetch_data() -> bytes:
        if retrieval.mode == "rejected" or not _is_trusted_bot_attachment_url(
            retrieval.url, retrieval.connector_origin
        ):
            raise NetworkError("teams", BOT_TOKEN_REFUSAL)
        return await fetchers.fetch_authenticated(retrieval.url)

    return fetch_data


def create_anonymous_attachment_fetch_data(url: str) -> FetchData:
    """Fetch ``url`` without credentials through the shared guarded downloader."""

    async def fetch_data() -> bytes:
        try:
            return await download_attachment(url, adapter="teams")
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError("teams", "Failed to fetch attachment", error) from error

    return fetch_data


class _StreamedResponse:
    """An ``httpx`` streamed response seen as an ``AttachmentResponse``."""

    def __init__(self, response: Any) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return int(self._response.status_code)

    @property
    def reason(self) -> str | None:
        return self._response.reason_phrase

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    def iter_chunks(self) -> Any:
        # Still content-encoded: ``read_attachment_body`` decodes it so the
        # cap applies to decoded bytes.
        return self._response.aiter_raw()

    async def close(self) -> None:
        await self._response.aclose()


async def fetch_with_bot_token(
    client: Any,
    url: str,
    *,
    limit: int = DEFAULT_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> bytes:
    """GET ``url`` with the bot token, following no redirects.

    ``client`` is the Teams SDK HTTP client (``App.api.http``): it supplies
    the bot token and default headers, and its ``httpx`` client sends the
    request. Upstream (``maxRedirects: 0``) also refuses redirects; the
    ``limit`` byte cap (checked on ``Content-Length`` and on the streamed
    body) and the ``timeout_ms`` deadline are Python-only hardening.
    """
    # Divergence from upstream — see docs/UPSTREAM_SYNC.md: byte cap + deadline.
    try:
        async with asyncio.timeout(timeout_ms / 1000):
            # ``Client._prepare_headers`` (same signature in
            # ``microsoft-teams-common`` 2.0.13 - 2.1.x) resolves the bot token
            # and default headers exactly as the SDK's own ``get`` does; the
            # SDK ``get`` itself cannot stream, so the body could not be capped.
            # ``identity``: the body is decoded (and capped) by
            # ``read_attachment_body``, which does not support every encoding
            # ``httpx`` would advertise.
            headers = await client._prepare_headers({"accept-encoding": "identity"}, None)
            async with client.http.stream("GET", url, headers=headers, follow_redirects=False) as response:
                status = response.status_code
                if status < 200 or status >= 300:
                    raise NetworkError("teams", f"Failed to fetch authenticated file: {status}")
                return await read_attachment_body(_StreamedResponse(response), "teams", limit)
    except TimeoutError as error:
        raise NetworkError("teams", "Timed out fetching the attachment", error) from error


def _classify_attachment_type(mime_type: str | None) -> Literal["audio", "file", "image", "video"]:
    if mime_type is not None and mime_type.startswith("image/"):
        return "image"
    if mime_type is not None and mime_type.startswith("video/"):
        return "video"
    if mime_type is not None and mime_type.startswith("audio/"):
        return "audio"
    return "file"


def create_teams_attachment(
    att: Mapping[str, Any],
    service_url: str | None,
    fetchers: TeamsAttachmentFetchers,
) -> Attachment:
    """Build an :class:`Attachment` from a Teams activity attachment."""
    content_type = att.get("contentType")
    content_type = content_type if isinstance(content_type, str) else None
    is_file_download = content_type == FILE_DOWNLOAD_INFO_CONTENT_TYPE
    content = att.get("content")
    file_content = content if is_file_download and isinstance(content, Mapping) else None
    file_type = file_content.get("fileType") if file_content is not None else None
    file_type = file_type if isinstance(file_type, str) else None

    url: str | None
    if is_file_download:
        download_url = file_content.get("downloadUrl") if file_content is not None else None
        url = download_url if isinstance(download_url, str) else None
    else:
        content_url = att.get("contentUrl")
        url = content_url if isinstance(content_url, str) else None
    mime_type = _infer_file_mime_type(att.get("name"), file_type) if is_file_download else content_type
    connector_origin = _connector_origin(service_url)
    use_bot_auth = (
        not is_file_download
        and url is not None
        and connector_origin is not None
        and _is_trusted_bot_attachment_url(url, connector_origin)
    )

    retrieval: _Retrieval | None = None
    if url:
        retrieval = _Retrieval("bot", url, connector_origin) if use_bot_auth else _Retrieval("anonymous", url)
    fetch_metadata: dict[str, str] | None = None
    if retrieval is not None:
        fetch_metadata = (
            {"url": retrieval.url, "auth": "bot", "connectorOrigin": retrieval.connector_origin}
            if retrieval.mode == "bot" and retrieval.connector_origin is not None
            else {"url": retrieval.url}
        )

    name = att.get("name")
    return Attachment(
        type=_classify_attachment_type(mime_type),
        url=url,
        name=name if isinstance(name, str) else None,
        mime_type=mime_type,
        fetch_metadata=fetch_metadata,
        fetch_data=_create_fetch_data_fn(retrieval, fetchers) if retrieval is not None else None,
    )


def rehydrate_teams_attachment(attachment: Attachment, fetchers: TeamsAttachmentFetchers) -> Attachment:
    """Rebuild ``fetch_data`` on a deserialized Teams attachment.

    A ``"bot"`` entry without ``connectorOrigin`` is rejected at fetch time,
    and the URL is re-checked against the recorded origin before the token
    is sent.
    """
    meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
    meta_url = meta.get("url")
    url = meta_url if meta_url is not None else attachment.url
    if not url:
        return attachment

    connector_origin = meta.get("connectorOrigin")
    retrieval = _Retrieval("anonymous", url)
    if meta.get("auth") == "bot":
        retrieval = _Retrieval("bot", url, connector_origin) if connector_origin else _Retrieval("rejected", url)

    return dataclasses.replace(attachment, fetch_data=_create_fetch_data_fn(retrieval, fetchers))


__all__ = [
    "BOT_TOKEN_REFUSAL",
    "FILE_DOWNLOAD_INFO_CONTENT_TYPE",
    "FILE_MIME_TYPES",
    "TeamsAttachmentFetchers",
    "create_anonymous_attachment_fetch_data",
    "create_teams_attachment",
    "fetch_with_bot_token",
    "rehydrate_teams_attachment",
]
