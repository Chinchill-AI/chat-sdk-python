"""WhatsApp adapter for chat SDK.

Supports messaging via the WhatsApp Business Cloud API (Meta Graph API).
All conversations are 1:1 DMs between the business phone number and users.

Python port of packages/adapter-whatsapp/src/index.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import os
import re
import time
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlparse, urlsplit

from chat_sdk.adapters.whatsapp.cards import (
    WhatsAppCardResultInteractive,
    WhatsAppCardResultText,
    card_link_button_lines,
    card_to_whatsapp,
    decode_whatsapp_callback_data,
)
from chat_sdk.adapters.whatsapp.errors import WhatsAppApiError, parse_json_text
from chat_sdk.adapters.whatsapp.format_converter import WhatsAppFormatConverter
from chat_sdk.adapters.whatsapp.types import (
    WhatsAppAdapterConfig,
    WhatsAppContact,
    WhatsAppInboundMessage,
    WhatsAppInteractiveMessage,
    WhatsAppMediaUploadResponse,
    WhatsAppRawMessage,
    WhatsAppTemplateComponent,
    WhatsAppTemplateMessage,
    WhatsAppThreadId,
    WhatsAppWebhookPayload,
    WhatsAppWebhookValue,
)
from chat_sdk.emoji import convert_emoji_placeholders, emoji_to_unicode, get_emoji
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared._js_compat import JS_WHITESPACE
from chat_sdk.shared.adapter_utils import extract_card, extract_files, extract_postable_attachments
from chat_sdk.shared.buffer_utils import to_buffer
from chat_sdk.shared.card_utils import card_to_fallback_text
from chat_sdk.shared.download import AttachmentTransport, download_attachment
from chat_sdk.shared.errors import AdapterError, NetworkError, ValidationError
from chat_sdk.shared.log_utils import utf8_byte_length
from chat_sdk.thread_history import ThreadHistoryCache
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    Attachment,
    Author,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FileUpload,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    StreamInput,
    StreamOptions,
    ThreadInfo,
    TypingOptions,
    UserInfo,
    WebhookOptions,
)

# Default Graph API version
DEFAULT_API_VERSION = "v25.0"

# Maximum message length for WhatsApp Cloud API
WHATSAPP_MESSAGE_LIMIT = 4096

# Maximum caption length for WhatsApp media messages
WHATSAPP_CAPTION_LIMIT = 1024

# Meta hosts that serve WhatsApp media (exact host or any subdomain). The
# access token goes only to these and to the configured Graph API origin.
_WHATSAPP_MEDIA_HOSTS = ("fbcdn.net", "fbsbx.com")
_UNTRUSTED_MEDIA_URL = "Refusing to send the access token to an untrusted media URL"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _url_origin(url: str) -> tuple[str, str, int | None]:
    """``(scheme, hostname, port)`` with the scheme's default port filled in.

    Raises ``ValueError`` for an unparseable URL or port.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    return scheme, parts.hostname or "", port if port is not None else _DEFAULT_PORTS.get(scheme)


def _is_whatsapp_media_url(url: str, graph_api_url: str) -> bool:
    """Port of upstream ``isWhatsAppMediaUrl``: may ``url`` receive the access token?

    True for the exact configured Graph API origin (scheme, host and port),
    or for an https URL with no explicit non-default port whose host is one
    of :data:`_WHATSAPP_MEDIA_HOSTS` or a subdomain of one.

    :func:`download_attachment` re-runs this on every hop's normalized URL
    (WHATWG host parsing) before attaching the token, so that per-hop check
    is the authoritative one; this also runs on the raw Graph-supplied URL
    to refuse early, as upstream does.
    """
    if not isinstance(url, str):
        return False
    try:
        scheme, hostname, port = _url_origin(url)
        if not hostname:
            return False
        if (scheme, hostname, port) == _url_origin(graph_api_url):
            return True
    except ValueError:
        return False
    return (
        scheme == "https"
        and port == 443
        and any(hostname == host or hostname.endswith(f".{host}") for host in _WHATSAPP_MEDIA_HOSTS)
    )


# WhatsApp media message types supported for outbound sends
WhatsAppMediaType = Literal["image", "document", "video", "audio"]

# Per-type upload size limits (bytes) from WhatsApp Cloud API
WHATSAPP_MEDIA_SIZE_LIMITS: dict[WhatsAppMediaType, int] = {
    "image": 5 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "document": 100 * 1024 * 1024,
}

# Filename-extension fallback when a file carries no MIME type
EXTENSION_MIME_TYPES: dict[str, str] = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".webp": "image/webp",
}

# A multipart part's filename is serialized like WHATWG ``FormData`` (what
# upstream's ``uploadMedia`` relies on): only LF, CR and ``"`` are
# percent-escaped, and spaces / non-ASCII stay raw UTF-8.
# See https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#multipart-form-data
_MULTIPART_FILENAME_ESCAPES = str.maketrans({"\n": "%0A", "\r": "%0D", '"': "%22"})
# aiohttp refuses (bare ``ValueError``) any other C0 control or DEL in a part
# header, where Node would send it raw; percent-escape those too so the upload
# still goes out.
_FORBIDDEN_HEADER_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _multipart_filename(filename: str) -> str:
    """Escape a filename for a multipart ``Content-Disposition`` header."""
    escaped = filename.translate(_MULTIPART_FILENAME_ESCAPES)
    return _FORBIDDEN_HEADER_CHARS.sub(lambda match: f"%{ord(match.group()):02X}", escaped)


def _blob_content_type(mime_type: str) -> str:
    """The part ``Content-Type`` Node sends for ``new Blob([...], {type})``.

    A Blob type containing any character outside U+0020-U+007E is dropped,
    and FormData then labels the part ``application/octet-stream``; a valid
    type is ASCII-lowercased.
    """
    if mime_type and all(" " <= char <= "~" for char in mime_type):
        return mime_type.lower()
    return "application/octet-stream"


# Business-scoped user ID shape (e.g. ``US.13491208655302741918`` or the
# parent form ``US.ENT.11815799212886844830``). Used with ``fullmatch`` so a
# trailing newline does not match, like JS ``/^...$/`` without the ``m`` flag.
_BSUID_PATTERN = re.compile(r"[A-Z]{2}\.(?:ENT\.)?[A-Za-z0-9]{1,128}")


@dataclass(frozen=True)
class _WhatsAppIdentity:
    """Resolved sender identity.

    ``user_id`` is the canonical identifier the thread is keyed by (a phone
    number or a BSUID); the other fields are the identifiers known for the
    user, any of which may be ``None``.
    """

    user_id: str
    bsuid: str | None = None
    parent: str | None = None
    phone: str | None = None


# Stored route: ``{"bsuid"?, "parent"?, "phone"?}`` with absent keys omitted
# (never written as ``None``) so the JSON matches the TS SDK byte for byte.
_WhatsAppRoute = dict[str, str]
# Outbound addressing: ``{"to"?, "recipient"?}``, spread into send payloads.
_WhatsAppRecipient = dict[str, str]


def _route(bsuid: str | None, parent: str | None, phone: str | None) -> _WhatsAppRoute:
    """Build a route dict, omitting absent (``None``) identifiers."""
    route: _WhatsAppRoute = {}
    if bsuid is not None:
        route["bsuid"] = bsuid
    if parent is not None:
        route["parent"] = parent
    if phone is not None:
        route["phone"] = phone
    return route


def _reply_context(reply_id: str | None) -> dict[str, Any]:
    """``{"context": {"message_id": reply_id}}``, or ``{}`` for no reply.

    Truthiness, as upstream's ``...(replyId ? {context} : {})``: an empty
    ID sends no context.
    """
    return {"context": {"message_id": reply_id}} if reply_id else {}


def _first_not_none(*values: Any) -> Any:
    """Return the first value that is not ``None`` (a JS ``??`` chain)."""
    for value in values:
        if value is not None:
            return value
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it is a dict, else an empty dict (JS ``?.`` on a non-object)."""
    return value if isinstance(value, dict) else {}


def _metadata_phone_number_id(value: Any) -> str:
    """Business ``phone_number_id`` from a webhook change value, or ``""``.

    Tolerates a missing, null or non-dict ``metadata`` so a malformed change
    can never raise out of ``handle_webhook``.
    """
    phone_number_id = _as_dict(_as_dict(value).get("metadata")).get("phone_number_id")
    return phone_number_id if isinstance(phone_number_id, str) else ""


def _convert_template_component_emoji(component: WhatsAppTemplateComponent) -> WhatsAppTemplateComponent:
    """Convert emoji placeholders in a template component's text parameters.

    Only ``type == "text"`` parameters are converted; payloads, URLs and media
    references must stay literal. Returns new dicts and leaves the caller's
    component untouched.
    """
    # pyrefly does not narrow the TypedDict union on the ``type`` tag, so the
    # parameters are handled as plain dicts.
    source: list[dict[str, Any]] = cast(list[dict[str, Any]], component["parameters"])
    parameters = [
        {**parameter, "text": convert_emoji_placeholders(parameter["text"], "whatsapp")}
        if parameter.get("type") == "text"
        else parameter
        for parameter in source
    ]
    return cast(WhatsAppTemplateComponent, {**component, "parameters": parameters})


def get_whatsapp_media_type(mime_type: str) -> WhatsAppMediaType:
    """Map a MIME type to a WhatsApp outbound media message type.

    JPEG and PNG are images; other ``image/*`` types (GIF, WebP, ...) go out
    as documents because the Cloud API rejects them as images. MP4 and 3GPP
    are videos, any ``audio/*`` is audio, and everything else is a document.
    """
    normalized = mime_type.lower().split(";")[0].strip(JS_WHITESPACE)

    if normalized in ("image/jpeg", "image/png"):
        return "image"

    if normalized.startswith("image/"):
        return "document"

    if normalized in ("video/mp4", "video/3gpp"):
        return "video"

    if normalized.startswith("audio/"):
        return "audio"

    return "document"


def validate_file_size(media_type: WhatsAppMediaType, size: int | float) -> None:
    """Validate a binary size against WhatsApp's per-type upload limits.

    Raises:
        ValidationError: When ``size`` exceeds the limit for ``media_type``.
    """
    limit = WHATSAPP_MEDIA_SIZE_LIMITS[media_type]

    if size > limit:
        raise ValidationError(
            "whatsapp",
            f"File size {size} bytes exceeds WhatsApp {media_type} limit of {limit} bytes",
        )


def _infer_mime_type(filename: str, mime_type: str | None = None) -> str:
    """Return ``mime_type`` if set, else a guess from the filename extension."""
    if mime_type:
        return mime_type

    extension = filename[filename.rfind(".") :].lower() if "." in filename else ""

    return EXTENSION_MIME_TYPES.get(extension, "application/octet-stream")


def _attachment_to_whatsapp_type(attachment: Attachment) -> WhatsAppMediaType:
    """WhatsApp media type for an attachment: its MIME type, else its kind."""
    if attachment.mime_type:
        return get_whatsapp_media_type(attachment.mime_type)

    if attachment.type in ("image", "video", "audio"):
        return attachment.type
    return "document"


def _js_length(text: str) -> int:
    """``text.length`` in JavaScript: the UTF-16 code unit count."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


@dataclass(frozen=True)
class _ResolvedWhatsAppMedia:
    """A file or attachment resolved to a sendable WhatsApp media payload."""

    caption_eligible: bool
    mime_type: str
    payload: dict[str, str]  # {"id": media_id} or {"link": https_url}
    type: WhatsAppMediaType
    filename: str | None = None


def split_message(text: str) -> list[str]:
    """Split text into chunks that fit within WhatsApp's message limit.

    Breaks on paragraph boundaries (\\n\\n) when possible, then line
    boundaries (\\n), and finally at the character limit as a last resort.
    """
    if len(text) <= WHATSAPP_MESSAGE_LIMIT:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > WHATSAPP_MESSAGE_LIMIT:
        slice_ = remaining[:WHATSAPP_MESSAGE_LIMIT]

        # Try to break at a paragraph boundary
        break_index = slice_.rfind("\n\n")
        if break_index == -1 or break_index < WHATSAPP_MESSAGE_LIMIT // 2:
            # Try a line boundary
            break_index = slice_.rfind("\n")
        if break_index == -1 or break_index < WHATSAPP_MESSAGE_LIMIT // 2:
            # Hard break at the limit
            break_index = WHATSAPP_MESSAGE_LIMIT

        chunks.append(remaining[:break_index].rstrip())
        remaining = remaining[break_index:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks


class WhatsAppAdapter:
    """WhatsApp adapter for chat SDK.

    Implements the Adapter interface for WhatsApp Business Cloud API.
    """

    def __init__(self, config: WhatsAppAdapterConfig) -> None:
        self._name = "whatsapp"
        self._lock_scope: LockScope = "channel"
        self._persist_thread_history = True
        self._user_name = config.user_name
        self._access_token = config.access_token
        self._app_secret = config.app_secret
        self._phone_number_id = config.phone_number_id
        self._verify_token = config.verify_token
        self._logger: Logger = config.logger
        api_version = config.api_version or DEFAULT_API_VERSION
        self._graph_api_url = f"https://graph.facebook.com/{api_version}"
        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None
        self._format_converter = WhatsAppFormatConverter()

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def lock_scope(self) -> LockScope:
        return self._lock_scope

    @property
    def persist_thread_history(self) -> bool:
        return self._persist_thread_history

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch business profile info."""
        self._chat = chat
        self._bot_user_id = self._phone_number_id
        self._logger.info("WhatsApp adapter initialized", {"phoneNumberId": self._phone_number_id})

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Not implemented — see docs/UPSTREAM_SYNC.md non-parity table.

        WhatsApp Cloud API has no user lookup endpoint; the only stable
        identifiers are the phone number and the business-scoped user ID
        (BSUID), and there's no equivalent of
        ``users.info`` exposed to business apps. Raising
        :class:`~chat_sdk.errors.ChatNotImplementedError` lets
        :meth:`Chat.get_user` translate this into a ``"does not support
        get_user"`` error rather than returning ``None`` (which would
        falsely imply "user not found").
        """
        from chat_sdk.errors import ChatNotImplementedError

        raise ChatNotImplementedError("whatsapp", "getUser")

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily if needed."""
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def disconnect(self) -> None:
        """Cleanup hook. Close the shared HTTP session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle incoming webhook from WhatsApp.

        Handles both the GET verification challenge and POST event notifications.
        """
        # Handle webhook verification challenge (GET request)
        method = getattr(request, "method", "POST")
        if method == "GET":
            return self._handle_verification_challenge(request)

        body = await self._get_request_body(request)
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: upstream still
        # logs a raw-body preview here (before signature verification) and a
        # body-prefix preview on invalid JSON; we log request-shape metadata only.

        # Verify request signature (X-Hub-Signature-256 header)
        signature = self._get_header(request, "x-hub-signature-256")
        if not self._verify_signature(body, signature):
            return self._make_response("Invalid signature", 401)

        # Parse the JSON payload
        try:
            payload: WhatsAppWebhookPayload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._logger.error(
                "WhatsApp webhook invalid JSON",
                {
                    "bodyBytes": utf8_byte_length(body),
                    "contentType": self._get_header(request, "content-type"),
                },
            )
            return self._make_response("Invalid JSON", 400)

        # Process entries
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") == "user_id_update":
                    await self._handle_user_id_update(change.get("value", {}))
                    continue

                if change.get("field") != "messages":
                    continue

                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                phone_number_id = _metadata_phone_number_id(value)
                contacts = value.get("contacts") or []

                # Process incoming messages. `value["messages"]` is typed as
                # `list[dict[str, Any]]` on the webhook TypedDict; cast each
                # entry to the more-specific inbound shape for handler dispatch.
                if value.get("messages"):
                    for message in value["messages"]:
                        try:
                            # Upstream reads `value.metadata.phone_number_id`
                            # inside this try, so a malformed change fails
                            # per message (logged) and the webhook still
                            # returns 200. Raising here keeps that contract
                            # and never writes identity keys under an empty
                            # business number.
                            if not phone_number_id:
                                raise ValueError("WhatsApp change is missing metadata.phone_number_id")
                            inbound = cast("WhatsAppInboundMessage", message)
                            contact = self._match_contact(inbound, contacts)
                            identity = await self._resolve(inbound, contact, phone_number_id)
                            if identity is None:
                                self._logger.warn(
                                    "WhatsApp message has no user identifier",
                                    {"messageId": inbound.get("id")},
                                )
                                continue
                            # System messages (number / BSUID changes) only
                            # re-link identity state; they are not dispatched.
                            if inbound.get("type") == "system":
                                continue
                            self._handle_inbound_message(
                                inbound,
                                contact,
                                phone_number_id,
                                options,
                                identity,
                            )
                        except Exception as error:
                            self._logger.error(
                                "Failed to handle inbound message",
                                {
                                    "messageId": message.get("id"),
                                    "error": str(error),
                                },
                            )

        return self._make_response("ok", 200)

    def _handle_verification_challenge(self, request: Any) -> Any:
        """Handle the webhook verification challenge from Meta."""
        url = getattr(request, "url", "")
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        mode = (params.get("hub.mode") or [None])[0]
        token = (params.get("hub.verify_token") or [None])[0]
        challenge = (params.get("hub.challenge") or [""])[0]

        if mode == "subscribe" and token == self._verify_token:
            self._logger.info("WhatsApp webhook verification succeeded")
            return self._make_response(challenge, 200)

        self._logger.warn(
            "WhatsApp webhook verification failed",
            {
                "mode": mode,
                "tokenMatch": token == self._verify_token,
            },
        )
        return self._make_response("Forbidden", 403)

    def _verify_signature(self, body: str, signature: str | None) -> bool:
        """Verify webhook signature using HMAC-SHA256 with the App Secret."""
        if not signature:
            return False

        expected = (
            "sha256="
            + hmac.new(
                self._app_secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        try:
            return hmac.compare_digest(signature, expected)
        except Exception:
            return False

    def _handle_inbound_message(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
        options: WebhookOptions | None = None,
        identity: _WhatsAppIdentity | None = None,
    ) -> None:
        """Handle an inbound message from a user."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring message")
            return

        user = identity if identity is not None else self._fields(inbound, contact)
        if user is None:
            self._logger.warn(
                "WhatsApp message has no user identifier",
                {"messageId": inbound.get("id")},
            )
            return

        # Handle reactions separately
        if inbound.get("type") == "reaction" and inbound.get("reaction"):
            self._handle_reaction(inbound, contact, phone_number_id, options, user)
            return

        # Handle interactive message replies (button clicks)
        if inbound.get("type") == "interactive" and inbound.get("interactive"):
            self._handle_interactive_reply(inbound, contact, phone_number_id, options, user)
            return

        # Handle legacy button responses (from template quick replies)
        if inbound.get("type") == "button" and inbound.get("button"):
            self._handle_button_response(inbound, contact, phone_number_id, options, user)
            return

        # Extract text content based on message type
        text = self._extract_text_content(inbound)
        if text is None:
            self._logger.debug(
                "Unsupported message type, ignoring",
                {
                    "type": inbound.get("type"),
                    "messageId": inbound.get("id"),
                },
            )
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=user.user_id,
            )
        )

        message = self._build_message(inbound, contact, thread_id, text, phone_number_id, user)
        self._chat.process_message(self, thread_id, message, options)

    def _handle_reaction(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
        options: WebhookOptions | None = None,
        identity: _WhatsAppIdentity | None = None,
    ) -> None:
        """Handle reaction events."""
        if not (self._chat and inbound.get("reaction")):
            return

        user = identity if identity is not None else self._fields(inbound, contact)
        if user is None:
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=user.user_id,
            )
        )

        raw_emoji = inbound["reaction"].get("emoji", "")
        added = raw_emoji != ""
        emoji_value = get_emoji(raw_emoji) if added else get_emoji("")

        self._chat.process_reaction(
            ReactionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=inbound["reaction"]["message_id"],
                user=self._author(user, contact),
                emoji=emoji_value,
                raw_emoji=raw_emoji,
                added=added,
                raw=inbound,
            ),
            options,
        )

    def _handle_interactive_reply(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
        options: WebhookOptions | None = None,
        identity: _WhatsAppIdentity | None = None,
    ) -> None:
        """Handle interactive message replies (button/list selection)."""
        if not (self._chat and inbound.get("interactive")):
            return

        user = identity if identity is not None else self._fields(inbound, contact)
        if user is None:
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=user.user_id,
            )
        )

        interactive = inbound["interactive"]
        raw_id: str
        fallback_value: str

        if interactive.get("type") == "button_reply" and interactive.get("button_reply"):
            raw_id = interactive["button_reply"]["id"]
            fallback_value = interactive["button_reply"]["title"]
        elif interactive.get("type") == "list_reply" and interactive.get("list_reply"):
            raw_id = interactive["list_reply"]["id"]
            fallback_value = interactive["list_reply"]["title"]
        else:
            return

        decoded = decode_whatsapp_callback_data(raw_id)
        action_id = decoded["action_id"] or ""
        value = decoded.get("value") if decoded.get("value") is not None else fallback_value

        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=inbound["id"],
                user=self._author(user, contact),
                action_id=action_id,
                value=value,
                raw=inbound,
            ),
            options,
        )

    def _handle_button_response(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
        options: WebhookOptions | None = None,
        identity: _WhatsAppIdentity | None = None,
    ) -> None:
        """Handle legacy button responses (from template quick replies)."""
        if not (self._chat and inbound.get("button")):
            return

        user = identity if identity is not None else self._fields(inbound, contact)
        if user is None:
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=user.user_id,
            )
        )

        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=inbound["id"],
                user=self._author(user, contact),
                action_id=inbound["button"]["payload"],
                value=inbound["button"]["text"],
                raw=inbound,
            ),
            options,
        )

    # =========================================================================
    # Identity: phone numbers, business-scoped user IDs (BSUIDs), usernames
    # =========================================================================

    @staticmethod
    def _match_contact(
        inbound: WhatsAppInboundMessage,
        contacts: list[WhatsAppContact],
    ) -> WhatsAppContact | None:
        """Find the contact for a message by BSUID, parent BSUID or phone.

        Divergence from upstream — see docs/UPSTREAM_SYNC.md: upstream also
        matches only ``user_id`` / ``wa_id`` and otherwise falls back to
        ``contacts[0]``. In a batched webhook that first contact can belong to
        another sender, and ``_fields`` would then borrow its phone/BSUID and
        alias this sender to the wrong user. We also match ``parent_user_id``,
        and fall back only when the payload has exactly one contact and the
        message carries no sender identifier of its own. A message whose
        identifiers match no contact cannot be tied to one. System messages
        carry their identifiers in ``system``, so they never take the fallback.
        """
        from_user_id = inbound.get("from_user_id")
        from_parent_user_id = inbound.get("from_parent_user_id")
        from_phone = inbound.get("from")
        for item in contacts:
            if not isinstance(item, dict):
                continue
            if (
                (from_user_id and item.get("user_id") == from_user_id)
                or (from_parent_user_id and item.get("parent_user_id") == from_parent_user_id)
                or (from_phone and item.get("wa_id") == from_phone)
            ):
                return item
        if from_user_id or from_parent_user_id or from_phone or inbound.get("type") == "system":
            return None
        return contacts[0] if len(contacts) == 1 else None

    def _author(self, identity: _WhatsAppIdentity, contact: WhatsAppContact | None = None) -> Author:
        """Build the message author for an identity.

        ``or`` rather than ``is not None`` (upstream ``||``): an empty-string
        profile name must still fall back to the user ID.
        """
        profile: dict[str, Any] = cast("dict[str, Any]", (contact or {}).get("profile") or {})
        return Author(
            user_id=identity.user_id,
            user_name=profile.get("username") or profile.get("name") or identity.user_id,
            full_name=profile.get("name") or profile.get("username") or identity.user_id,
            is_bot=False,
            is_me=identity.user_id == self._bot_user_id,
        )

    @staticmethod
    def _fields(
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None = None,
    ) -> _WhatsAppIdentity | None:
        """Extract the sender's identifiers from a message and its contact.

        Precedence (upstream ``??`` chains): phone, then BSUID, then parent
        BSUID. The system block wins because it carries the post-change values.
        """
        system: dict[str, Any] = cast("dict[str, Any]", inbound.get("system") or {})
        contact_dict: dict[str, Any] = cast("dict[str, Any]", contact or {})
        phone = _first_not_none(system.get("wa_id"), inbound.get("from"), contact_dict.get("wa_id"))
        bsuid = _first_not_none(system.get("user_id"), inbound.get("from_user_id"), contact_dict.get("user_id"))
        parent = _first_not_none(
            system.get("parent_user_id"),
            inbound.get("from_parent_user_id"),
            contact_dict.get("parent_user_id"),
        )
        user_id = _first_not_none(phone, bsuid, parent)
        if not user_id:
            return None
        return _WhatsAppIdentity(user_id=user_id, bsuid=bsuid, parent=parent, phone=phone)

    async def _resolve(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
    ) -> _WhatsAppIdentity | None:
        """Resolve the canonical identity for a message and persist its aliases.

        State errors are logged and fall back to the un-linked identity, so a
        state outage never drops an inbound message.
        """
        identity = self._fields(inbound, contact)
        if identity is None:
            return None

        bsuid, parent, phone = identity.bsuid, identity.parent, identity.phone
        changed = inbound.get("type") == "system"
        # A system message's `from` carries the pre-change identifier, so
        # prefer it as the canonical fallback: a thread that predates any
        # alias state keeps its original key that way.
        source = inbound.get("from") if changed else None
        fallback = source if source is not None else identity.user_id

        if not self._chat:
            return _WhatsAppIdentity(user_id=fallback, bsuid=bsuid, parent=parent, phone=phone)

        identifiers = [value for value in (source, bsuid, parent, phone) if value]

        def merge(route: _WhatsAppRoute) -> _WhatsAppRoute:
            return _route(
                bsuid if bsuid is not None else route.get("bsuid"),
                parent if parent is not None else route.get("parent"),
                # A system message re-keys the phone: absent means the user no
                # longer exposes one, so any stored number is stale.
                phone if changed else (phone if phone is not None else route.get("phone")),
            )

        try:
            return await self._link(self._chat.get_state(), phone_number_id, identifiers, fallback, merge)
        except Exception as error:
            self._logger.warn(
                "Failed to persist WhatsApp user identity",
                {"error": str(error), "messageId": inbound.get("id")},
            )
            return _WhatsAppIdentity(user_id=fallback, bsuid=bsuid, parent=parent, phone=phone)

    async def _handle_user_id_update(self, value: WhatsAppWebhookValue) -> None:
        """Handle a ``user_id_update`` change.

        Meta sends it when a phone number change rotates a user's
        business-scoped user ID. The payload carries the previous and current
        values, so both get aliased to the same canonical user and the route
        picks up the new identifiers.

        See: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids
        """
        if not self._chat or not isinstance(value, dict):
            return

        phone_number_id = _metadata_phone_number_id(value)
        if not phone_number_id:
            # Upstream throws out of handleWebhook here (500, Meta retries the
            # batch); we skip the change instead of writing identity keys
            # under an empty business number.
            self._logger.warn("WhatsApp user_id_update is missing metadata.phone_number_id")
            return
        for update in value.get("user_id_update") or []:
            if not isinstance(update, dict):
                continue
            # Upstream `update.user_id?.previous` yields undefined for a
            # non-object field; treat any non-dict the same way.
            user_id = _as_dict(update.get("user_id"))
            parent_user_id = _as_dict(update.get("parent_user_id"))
            wa_id = update.get("wa_id")
            identifiers = [
                identifier
                for identifier in (
                    user_id.get("previous"),
                    parent_user_id.get("previous"),
                    user_id.get("current"),
                    parent_user_id.get("current"),
                    wa_id,
                )
                if identifier
            ]
            # Prefer the previous BSUID as the canonical fallback so a thread
            # keyed by it survives the rotation even without alias state.
            if not identifiers:
                continue
            fallback = identifiers[0]
            current_bsuid = user_id.get("current")
            current_parent = parent_user_id.get("current")

            def merge(
                route: _WhatsAppRoute,
                current_bsuid: str | None = current_bsuid,
                current_parent: str | None = current_parent,
                wa_id: str | None = wa_id,
            ) -> _WhatsAppRoute:
                return _route(
                    current_bsuid if current_bsuid is not None else route.get("bsuid"),
                    current_parent if current_parent is not None else route.get("parent"),
                    # The rotation implies a phone number change, so any stored
                    # phone is stale; keep only the update's wa_id, when present.
                    wa_id,
                )

            try:
                await self._link(self._chat.get_state(), phone_number_id, identifiers, fallback, merge)
            except Exception as error:
                self._logger.warn("Failed to apply WhatsApp user ID update", {"error": str(error)})

    async def _link(
        self,
        state: Any,
        phone_number_id: str,
        identifiers: list[str],
        fallback: str,
        merge: Callable[[_WhatsAppRoute], _WhatsAppRoute],
    ) -> _WhatsAppIdentity:
        """Resolve the canonical user ID for equivalent identifiers and persist changes.

        Aliases are looked up concurrently but honored in list order (the first
        truthy one wins), so callers list the identifiers most likely to match
        an existing thread first. Only aliases that differ, and a route whose
        bsuid/parent/phone changed, are written.
        """
        aliases = await asyncio.gather(
            *(state.get(self._identity_key("alias", phone_number_id, identifier)) for identifier in identifiers)
        )
        user_id = next((alias for alias in aliases if isinstance(alias, str) and alias), fallback)

        path = self._identity_key("route", phone_number_id, user_id)
        stored = await state.get(path)
        route: _WhatsAppRoute = stored if isinstance(stored, dict) else {}
        updated = merge(route)

        writes = [
            state.set(self._identity_key("alias", phone_number_id, identifier), user_id)
            for identifier, alias in zip(identifiers, aliases, strict=True)
            if alias != user_id
        ]
        if any(route.get(key) != updated.get(key) for key in ("bsuid", "parent", "phone")):
            writes.append(state.set(path, updated))
        await asyncio.gather(*writes)

        return _WhatsAppIdentity(
            user_id=user_id,
            bsuid=updated.get("bsuid"),
            parent=updated.get("parent"),
            phone=updated.get("phone"),
        )

    async def _recipient(self, thread_id: str, user_id: str) -> _WhatsAppRecipient:
        """Resolve outbound addressing (``to`` and/or ``recipient``) for a thread.

        A stored route is honored only when it can address someone; otherwise
        a BSUID-shaped ``user_id`` goes out as ``recipient`` and anything else
        as ``to``. State errors are logged and fall through to that pattern.
        """
        if self._chat:
            try:
                decoded = self.decode_thread_id(thread_id)
                stored = await self._chat.get_state().get(self._identity_key("route", decoded.phone_number_id, user_id))
                route: _WhatsAppRoute = stored if isinstance(stored, dict) else {}
                bsuid = route.get("bsuid")
                recipient = bsuid if bsuid is not None else route.get("parent")
                phone = route.get("phone")
                # Only honor a stored route that can actually address someone;
                # an empty route falls through to the user_id below.
                if phone or recipient:
                    result: _WhatsAppRecipient = {}
                    if phone:
                        result["to"] = phone
                    if recipient:
                        result["recipient"] = recipient
                    return result
            except Exception as error:
                self._logger.warn(
                    "Failed to resolve WhatsApp recipient",
                    {"error": str(error), "threadId": thread_id},
                )

        return {"recipient": user_id} if _BSUID_PATTERN.fullmatch(user_id) else {"to": user_id}

    @staticmethod
    def _identity_key(kind: Literal["alias", "route"], phone_number_id: str, value: str) -> str:
        """State key for identity aliases/routes (byte-identical to the TS SDK)."""
        return f"whatsapp:identity:{kind}:{phone_number_id}:{value}"

    def _extract_text_content(self, message: WhatsAppInboundMessage) -> str | None:
        """Extract text content from an inbound message. Returns None for unsupported types."""
        msg_type = message.get("type")

        if msg_type == "text":
            return (message.get("text") or {}).get("body")
        if msg_type == "image":
            return (message.get("image") or {}).get("caption") or "[Image]"
        if msg_type == "document":
            doc = message.get("document") or {}
            return doc.get("caption") or f"[Document: {doc.get('filename', 'file')}]"
        if msg_type == "audio":
            return "[Audio message]"
        if msg_type == "voice":
            return "[Voice message]"
        if msg_type == "video":
            return "[Video]"
        if msg_type == "sticker":
            return "[Sticker]"
        if msg_type == "location":
            loc = message.get("location")
            if loc:
                parts = [f"[Location: {loc['latitude']}, {loc['longitude']}"]
                if loc.get("name"):
                    parts[0] = f"[Location: {loc['name']}"
                if loc.get("address"):
                    parts.append(loc["address"])
                return f"{' - '.join(parts)}]"
            return "[Location]"

        return None

    def _build_message(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        thread_id: str,
        text: str,
        phone_number_id: str | None = None,
        identity: _WhatsAppIdentity | None = None,
    ) -> Message:
        """Build a Message from a WhatsApp inbound message."""
        user = identity if identity is not None else self._fields(inbound, contact)
        if user is None:
            raise ValidationError("whatsapp", "Message has no user identifier")

        author = self._author(user, contact)

        formatted = self._format_converter.to_ast(text)

        raw: WhatsAppRawMessage = {
            "message": inbound,
            "contact": contact,
            "phone_number_id": phone_number_id or self._phone_number_id,
            "user_id": user.user_id,
        }

        attachments = self._build_attachments(inbound)

        return Message(
            id=inbound["id"],
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            raw=raw,
            author=author,
            metadata=MessageMetadata(
                date_sent=datetime.fromtimestamp(
                    int(inbound.get("timestamp", "0")),
                    tz=timezone.utc,
                ),
                edited=False,
            ),
            attachments=attachments,
        )

    def _build_attachments(self, inbound: WhatsAppInboundMessage) -> list[Attachment]:
        """Build attachments from an inbound message."""
        attachments: list[Attachment] = []

        if inbound.get("image"):
            attachments.append(
                self._build_media_attachment(
                    inbound["image"]["id"],
                    "image",
                    inbound["image"].get("mime_type", ""),
                )
            )

        if inbound.get("document"):
            attachments.append(
                self._build_media_attachment(
                    inbound["document"]["id"],
                    "file",
                    inbound["document"].get("mime_type", ""),
                    inbound["document"].get("filename"),
                )
            )

        if inbound.get("audio"):
            attachments.append(
                self._build_media_attachment(
                    inbound["audio"]["id"],
                    "audio",
                    inbound["audio"].get("mime_type", ""),
                )
            )

        if inbound.get("video"):
            attachments.append(
                self._build_media_attachment(
                    inbound["video"]["id"],
                    "video",
                    inbound["video"].get("mime_type", ""),
                )
            )

        if inbound.get("voice"):
            attachments.append(
                self._build_media_attachment(
                    inbound["voice"]["id"],
                    "audio",
                    inbound["voice"].get("mime_type", ""),
                    "voice",
                )
            )

        if inbound.get("sticker"):
            attachments.append(
                self._build_media_attachment(
                    inbound["sticker"]["id"],
                    "image",
                    inbound["sticker"].get("mime_type", ""),
                    "sticker",
                )
            )

        if inbound.get("location"):
            loc = inbound["location"]
            lat = float(loc.get("latitude", 0))
            lng = float(loc.get("longitude", 0))
            if math.isfinite(lat) and math.isfinite(lng):
                map_url = f"https://www.google.com/maps?q={lat},{lng}"
                attachments.append(
                    Attachment(
                        type="file",
                        name=loc.get("name") or "Location",
                        url=map_url,
                        mime_type="application/geo+json",
                    )
                )

        return attachments

    def _build_media_attachment(
        self,
        media_id: str,
        type_: str,
        mime_type: str,
        name: str | None = None,
    ) -> Attachment:
        """Build a single media attachment with a lazy fetch_data function."""
        return Attachment(
            type=type_,  # type: ignore[arg-type]
            mime_type=mime_type,
            name=name,
            fetch_data=lambda mid=media_id: self.download_media(mid),
            fetch_metadata={"mediaId": media_id},
        )

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized WhatsApp attachment.

        Pulls ``mediaId`` from ``fetch_metadata`` and rebuilds the lazy
        ``download_media`` closure.  Returns the attachment unchanged when
        no media ID is present.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        media_id = meta.get("mediaId")
        if not media_id:
            return attachment
        return Attachment(
            type=attachment.type,
            url=attachment.url,
            name=attachment.name,
            mime_type=attachment.mime_type,
            size=attachment.size,
            width=attachment.width,
            height=attachment.height,
            data=attachment.data,
            fetch_data=lambda mid=media_id: self.download_media(mid),
            fetch_metadata=attachment.fetch_metadata,
        )

    async def download_media(self, media_id: str, transport: AttachmentTransport | None = None) -> bytes:
        """Download media from WhatsApp.

        Two-step process:
        1. GET the media metadata to obtain the download URL
        2. GET the binary data through the shared guarded downloader
           (:func:`~chat_sdk.shared.download.download_attachment`): https
           only, internal addresses refused, at most 5 redirects, a 25 MB cap
           and a 30 s deadline. The access token is attached per hop, only
           when that hop's URL passes :func:`_is_whatsapp_media_url`, so a
           redirect cannot carry it off-policy.

        ``transport`` overrides the downloader's HTTP transport (upstream's
        optional ``transport`` argument), e.g. for tests or custom egress.

        See: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media#download-media
        """
        # Step 1: Get the media URL
        media_info = await self._graph_fetch_json(
            "GET",
            f"{self._graph_api_url}/{media_id}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            label="Failed to get media URL",
            context={"mediaId": media_id},
        )

        download_url = media_info.get("url") if isinstance(media_info, dict) else None
        if not isinstance(download_url, str) or not _is_whatsapp_media_url(download_url, self._graph_api_url):
            raise NetworkError("whatsapp", _UNTRUSTED_MEDIA_URL)

        graph_api_url = self._graph_api_url
        graph_host = urlsplit(graph_api_url).hostname
        hosts = (*_WHATSAPP_MEDIA_HOSTS, graph_host) if graph_host else _WHATSAPP_MEDIA_HOSTS

        def headers_for(hop_url: str) -> dict[str, str]:
            if not _is_whatsapp_media_url(hop_url, graph_api_url):
                raise NetworkError("whatsapp", _UNTRUSTED_MEDIA_URL)
            return {"authorization": f"Bearer {self._access_token}"}

        # Step 2: Download the actual file. Every hop is checked against the
        # exact-origin and Meta media host policy before the access token is
        # attached, so a redirect cannot carry it to an off-policy host.
        try:
            return await download_attachment(
                download_url,
                adapter="whatsapp",
                headers=headers_for,
                hosts=hosts,
                transport=transport,
            )
        except Exception as error:
            self._logger.error("Failed to download media", {"mediaId": media_id})
            if isinstance(error, NetworkError):
                raise
            raise NetworkError("whatsapp", f"Failed to download media {media_id}", error) from error

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Send a message to a WhatsApp user.

        Files and attachments go out as media messages (see
        :meth:`_post_message_with_media`).
        """
        return await self._send(thread_id, message)

    async def reply(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Send ``message`` as a contextual reply to ``message_id``.

        The first outgoing message (first text chunk, first media item, or
        the interactive card) carries ``context.message_id``; any further
        messages of the same post do not.

        See: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-messages#contextual-replies
        """
        return await self._send(thread_id, message, message_id)

    async def _send(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
        reply_id: str | None = None,
    ) -> RawMessage:
        """Shared body of :meth:`post_message` and :meth:`reply`."""
        decoded = self.decode_thread_id(thread_id)
        user_wa_id = decoded.user_wa_id
        # Resolve the route once per logical post; the send helpers reuse it
        # across chunked and multi-part sends.
        recipient = await self._recipient(thread_id, user_wa_id)

        media_items: list[FileUpload | Attachment] = [
            *extract_files(message),
            *extract_postable_attachments(message),
        ]
        if len(media_items) > 0:
            return await self._post_message_with_media(
                thread_id,
                user_wa_id,
                message,
                media_items,
                reply_id=reply_id,
                recipient=recipient,
            )

        # Check if this is a card with interactive buttons
        card = extract_card(message)
        if card:
            # `card_to_whatsapp` returns a `WhatsAppCardResultInteractive`
            # or `WhatsAppCardResultText` union; the `type` check narrows
            # at runtime but pyrefly doesn't propagate TypedDict `type`-tag
            # discrimination, so cast the field access on each branch.
            result = card_to_whatsapp(card)
            if result.get("type") == "interactive":
                interactive_raw = cast(WhatsAppCardResultInteractive, result)["interactive"]
                interactive = json.loads(convert_emoji_placeholders(json.dumps(interactive_raw), "whatsapp"))
                return await self._send_interactive_message(
                    thread_id, user_wa_id, interactive, recipient, reply_id=reply_id
                )
            return await self._send_text_message(
                thread_id,
                user_wa_id,
                convert_emoji_placeholders(cast(WhatsAppCardResultText, result)["text"], "whatsapp"),
                recipient,
                reply_id=reply_id,
            )

        # Regular text message
        body = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "whatsapp",
        )
        return await self._send_text_message(thread_id, user_wa_id, body, recipient, reply_id=reply_id)

    async def _post_message_with_media(
        self,
        thread_id: str,
        user_wa_id: str,
        message: AdapterPostableMessage,
        media_items: list[FileUpload | Attachment],
        *,
        reply_id: str | None = None,
        recipient: _WhatsAppRecipient | None = None,
    ) -> RawMessage:
        """Send one or more media messages, optionally followed by a card.

        The message text captions the first media when it fits (at most
        1024 characters, and the media is not audio); otherwise it goes out
        first as its own text message. Media are sent in order, then the
        card: an interactive card is sent after the media and never captions
        them, while a text-fallback card (plus its link-button lines) is the
        caption.
        """
        # Only the first message sent carries the reply context.
        remaining_id = reply_id
        card = extract_card(message)
        # cta_url promotion is disabled alongside media: the text fallback
        # captions the media in a single send, whereas an interactive
        # message would strip the caption and cost a second API send.
        card_result = card_to_whatsapp(card, allow_cta_url=False) if card else None

        text = ""

        if card:
            if card_result is not None and card_result.get("type") != "interactive":
                # The shared fallback excludes action elements, so append link
                # button URLs to keep them reachable from the caption.
                # Upstream parity (adapter-whatsapp/src/index.ts:1318-1327 at
                # chat@4.41.1): the caption is the shared ``cardToFallbackText``,
                # which omits ``image_url`` / image children, and the full
                # ``card_to_whatsapp_text`` is only sent when that caption is empty.
                fallback = "\n".join(
                    part for part in [card_to_fallback_text(card), *card_link_button_lines(card)] if part
                )
                text = convert_emoji_placeholders(fallback, "whatsapp")
        else:
            text = convert_emoji_placeholders(self._render_postable_text(message), "whatsapp")

        # Resolve (and upload) concurrently like upstream's ``Promise.all``;
        # the sends below stay sequential so messages arrive in order.
        resolved = await asyncio.gather(*(self._resolve_media(item) for item in media_items))

        first_media = resolved[0]
        # ``_js_length`` counts UTF-16 code units, like upstream's ``text.length``.
        use_separate_text = len(text) > 0 and (
            _js_length(text) > WHATSAPP_CAPTION_LIMIT or first_media.type == "audio" or not first_media.caption_eligible
        )

        if use_separate_text:
            await self._send_text_message(thread_id, user_wa_id, text, recipient, reply_id=remaining_id)
            remaining_id = None

        result: RawMessage | None = None

        for index, media in enumerate(resolved):
            caption = (
                text if index == 0 and not use_separate_text and len(text) > 0 and media.caption_eligible else None
            )

            result = await self._send_media_message(
                thread_id,
                user_wa_id,
                media.type,
                media.payload,
                caption=caption,
                filename=media.filename,
                reply_id=remaining_id,
                recipient=recipient,
            )
            remaining_id = None

        if card_result is not None:
            if card_result.get("type") == "interactive":
                interactive_raw = cast(WhatsAppCardResultInteractive, card_result)["interactive"]
                interactive = json.loads(convert_emoji_placeholders(json.dumps(interactive_raw), "whatsapp"))
                result = await self._send_interactive_message(
                    thread_id, user_wa_id, interactive, recipient, reply_id=remaining_id
                )
                remaining_id = None
            elif len(text) == 0:
                result = await self._send_text_message(
                    thread_id,
                    user_wa_id,
                    convert_emoji_placeholders(cast(WhatsAppCardResultText, card_result)["text"], "whatsapp"),
                    recipient,
                    reply_id=remaining_id,
                )
                remaining_id = None

        if result is None:
            raise RuntimeError("WhatsApp media message did not return a result")

        return result

    def _render_postable_text(self, message: AdapterPostableMessage) -> str:
        """Render optional text from a postable message (empty for files-only payloads)."""
        if isinstance(message, str):
            return message

        if isinstance(message, dict):
            if "markdown" in message or "raw" in message or "ast" in message:
                return self._format_converter.render_postable(message)
            return ""

        if hasattr(message, "markdown") or hasattr(message, "raw") or hasattr(message, "ast"):
            return self._format_converter.render_postable(message)

        return ""

    async def _upload_media(self, data: bytes, filename: str, mime_type: str) -> str:
        """Upload binary media to the Cloud API and return its media ID.

        See: https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media#upload-media
        """
        import aiohttp

        # ``quote_fields=False`` keeps aiohttp from percent-encoding spaces and
        # non-ASCII in the filename; ``_multipart_filename`` applies the WHATWG
        # escapes instead. (aiohttp still writes a backslash as ``\\``, the
        # quoted-string form, where Node sends it raw.)
        form = aiohttp.FormData(quote_fields=False)
        form.add_field("messaging_product", "whatsapp")
        form.add_field(
            "file",
            data,
            filename=_multipart_filename(filename),
            content_type=_blob_content_type(mime_type),
        )

        response = cast(
            WhatsAppMediaUploadResponse,
            await self._graph_api_upload(f"/{self._phone_number_id}/media", form),
        )

        media_id = response.get("id") if isinstance(response, dict) else None
        if not media_id:
            raise RuntimeError("WhatsApp API did not return a media ID for upload")

        return media_id

    async def _send_media_message(
        self,
        thread_id: str,
        to: str,
        media_type: WhatsAppMediaType,
        payload: dict[str, str],
        *,
        caption: str | None = None,
        filename: str | None = None,
        reply_id: str | None = None,
        recipient: _WhatsAppRecipient | None = None,
    ) -> RawMessage:
        """Send a media message (image, document, video, or audio)."""
        media_object: dict[str, str] = {}

        if payload.get("id"):
            media_object["id"] = payload["id"]

        if payload.get("link"):
            media_object["link"] = payload["link"]

        if caption and media_type != "audio":
            media_object["caption"] = caption

        if filename and media_type == "document":
            media_object["filename"] = filename

        addressing = recipient if recipient is not None else await self._recipient(thread_id, to)
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **addressing,
                **_reply_context(reply_id),
                "type": media_type,
                media_type: media_object,
            },
        )

        messages = response.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise RuntimeError(f"WhatsApp API did not return a message ID for {media_type} message")

        message_id = messages[0]["id"]
        return RawMessage(
            id=message_id,
            thread_id=thread_id,
            raw={
                "message": {
                    "id": message_id,
                    "from": self._phone_number_id,
                    "timestamp": str(int(time.time())),
                    "type": media_type,
                },
                "phone_number_id": self._phone_number_id,
            },
        )

    async def _resolve_media(self, item: FileUpload | Attachment) -> _ResolvedWhatsAppMedia:
        """Normalize a FileUpload or Attachment into a WhatsApp media payload.

        Binary sources (``FileUpload.data``, ``Attachment.data``, then
        ``Attachment.fetch_data``) are size-checked and uploaded; otherwise an
        HTTPS ``Attachment.url`` is passed to WhatsApp as a ``link``.
        """
        if isinstance(item, FileUpload):
            mime_type = _infer_mime_type(item.filename, item.mime_type)
            media_type = get_whatsapp_media_type(mime_type)
            buffer = await to_buffer(item.data, "whatsapp")

            # ``is None``, not truthiness: an empty upload is still sent, as
            # upstream's (always truthy) Buffer is.
            if buffer is None:
                raise ValidationError("whatsapp", "File upload data is empty")

            validate_file_size(media_type, len(buffer))

            media_id = await self._upload_media(buffer, item.filename, mime_type)

            return _ResolvedWhatsAppMedia(
                caption_eligible=media_type != "audio",
                filename=item.filename,
                mime_type=mime_type,
                payload={"id": media_id},
                type=media_type,
            )

        media_type = _attachment_to_whatsapp_type(item)
        filename = item.name if item.name is not None else "attachment"
        mime_type = _infer_mime_type(filename, item.mime_type)

        data: bytes | None = item.data
        if data is None and item.fetch_data is not None:
            data = await item.fetch_data()

        if data is not None:
            buffer = await to_buffer(data, "whatsapp")

            if buffer is None:
                raise ValidationError("whatsapp", "Attachment data is empty")

            validate_file_size(media_type, len(buffer))

            media_id = await self._upload_media(buffer, filename, mime_type)

            return _ResolvedWhatsAppMedia(
                caption_eligible=media_type != "audio",
                filename=filename,
                mime_type=mime_type,
                payload={"id": media_id},
                type=media_type,
            )

        if not item.url:
            raise ValidationError(
                "whatsapp",
                "Attachment requires data, fetchData, or a public HTTPS url",
            )

        if not item.url.startswith("https://"):
            raise ValidationError(
                "whatsapp",
                "Attachment URL must use HTTPS for WhatsApp link passthrough",
            )

        # A size of 0 is valid, so test the type rather than truthiness.
        if isinstance(item.size, (int, float)) and not isinstance(item.size, bool):
            validate_file_size(media_type, item.size)

        return _ResolvedWhatsAppMedia(
            caption_eligible=media_type != "audio",
            filename=filename,
            mime_type=mime_type,
            payload={"link": item.url},
            type=media_type,
        )

    async def _send_single_text_message(
        self,
        thread_id: str,
        to: str,
        text: str,
        recipient: _WhatsAppRecipient | None = None,
        *,
        reply_id: str | None = None,
    ) -> RawMessage:
        """Send a single text message via the Cloud API."""
        addressing = recipient if recipient is not None else await self._recipient(thread_id, to)
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **addressing,
                **_reply_context(reply_id),
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )

        messages = response.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise RuntimeError("WhatsApp API did not return a message ID for text message")

        message_id = messages[0]["id"]
        return RawMessage(
            id=message_id,
            thread_id=thread_id,
            raw={
                "message": {
                    "id": message_id,
                    "from": self._phone_number_id,
                    "timestamp": str(int(time.time())),
                    "type": "text",
                    "text": {"body": text},
                },
                "phone_number_id": self._phone_number_id,
            },
        )

    async def _send_text_message(
        self,
        thread_id: str,
        to: str,
        text: str,
        recipient: _WhatsAppRecipient | None = None,
        *,
        reply_id: str | None = None,
    ) -> RawMessage:
        """Send a text message, splitting into multiple if it exceeds the limit.

        Only the first chunk carries ``reply_id``'s reply context.
        """
        chunks = split_message(text)
        # Resolve the route once so chunked sends share a single lookup.
        resolved = recipient if recipient is not None else await self._recipient(thread_id, to)
        result: RawMessage | None = None

        for index, chunk in enumerate(chunks):
            result = await self._send_single_text_message(
                thread_id, to, chunk, resolved, reply_id=reply_id if index == 0 else None
            )

        assert result is not None
        return result

    async def _send_interactive_message(
        self,
        thread_id: str,
        to: str,
        interactive: WhatsAppInteractiveMessage,
        recipient: _WhatsAppRecipient | None = None,
        *,
        reply_id: str | None = None,
    ) -> RawMessage:
        """Send an interactive message (buttons or list) via the Cloud API."""
        addressing = recipient if recipient is not None else await self._recipient(thread_id, to)
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **addressing,
                **_reply_context(reply_id),
                "type": "interactive",
                "interactive": interactive,
            },
        )

        messages = response.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise RuntimeError("WhatsApp API did not return a message ID for interactive message")

        message_id = messages[0]["id"]
        return RawMessage(
            id=message_id,
            thread_id=thread_id,
            raw={
                "message": {
                    "id": message_id,
                    "from": self._phone_number_id,
                    "timestamp": str(int(time.time())),
                    "type": "interactive",
                },
                "phone_number_id": self._phone_number_id,
            },
        )

    async def send_template(self, thread_id: str, template: WhatsAppTemplateMessage) -> RawMessage:
        """Send a pre-approved template message via the Cloud API.

        Templates are the only message type WhatsApp accepts outside the
        24-hour customer service window, making them the way to start
        business-initiated conversations. The adapter does not auto-substitute
        templates for outbound text posts -- callers must opt in explicitly
        when they detect the window is closed.

        Example::

            await adapter.send_template(thread_id, {
                "name": "appointment_reminder",
                "language": "en",
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": "Tomorrow at 2pm"}]},
                ],
            })

        See: https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates
        """
        user_wa_id = self.decode_thread_id(thread_id).user_wa_id

        # Convert emoji placeholders in text parameters only; payloads, URLs,
        # and media references must stay literal.
        source_components = template.get("components")
        components = (
            [_convert_template_component_emoji(component) for component in source_components]
            if source_components
            else None
        )

        template_payload: dict[str, Any] = {
            "name": template["name"],
            "language": {"code": template["language"]},
        }
        if components is not None:
            template_payload["components"] = components

        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **(await self._recipient(thread_id, user_wa_id)),
                "type": "template",
                "template": template_payload,
            },
        )

        messages = response.get("messages") or []
        if not messages or not messages[0].get("id"):
            raise RuntimeError("WhatsApp API did not return a message ID for template message")

        message_id = messages[0]["id"]
        return RawMessage(
            id=message_id,
            thread_id=thread_id,
            raw={
                "message": {
                    "id": message_id,
                    "from": self._phone_number_id,
                    "timestamp": str(int(time.time())),
                    "type": "template",
                },
                "phone_number_id": self._phone_number_id,
            },
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit a message. Not supported by WhatsApp Cloud API."""
        raise RuntimeError(
            "WhatsApp does not support editing messages. Use post_message to send a new message instead."
        )

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[StreamInput],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream a message by buffering all chunks and sending as a single message."""
        accumulated = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                accumulated += chunk
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":
                accumulated += chunk.text
        return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message. Not supported by WhatsApp Cloud API."""
        raise RuntimeError("WhatsApp does not support deleting messages.")

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a message."""
        decoded = self.decode_thread_id(thread_id)
        emoji_str = self._resolve_emoji(emoji)

        await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **(await self._recipient(thread_id, decoded.user_wa_id)),
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": emoji_str},
            },
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a message by sending empty emoji."""
        decoded = self.decode_thread_id(thread_id)

        await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                **(await self._recipient(thread_id, decoded.user_wa_id)),
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": ""},
            },
        )

    async def start_typing(
        self, thread_id: str, status: str | None = None, *, options: TypingOptions | None = None
    ) -> None:
        """Start typing indicator.

        WhatsApp typing indicators require the most recent inbound message ID.
        They also implicitly mark the referenced message as read.

        See: https://developers.facebook.com/documentation/business-messaging/whatsapp/typing-indicators
        """
        message_id = await self._resolve_typing_target_message_id(thread_id)
        self._logger.debug(
            "WhatsApp typing indicator requested",
            {"messageId": message_id, "threadId": thread_id},
        )

        if not message_id:
            self._logger.warn(
                "WhatsApp typing indicator skipped - no inbound message context",
                {"threadId": thread_id},
            )
            return

        if status:
            self._logger.warn(
                "WhatsApp typing indicator ignores custom status text",
                {"status": status, "threadId": thread_id, "messageId": message_id},
            )

        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            },
        )

        if not response.get("success"):
            self._logger.error(
                "WhatsApp typing indicator failed: API returned success=false",
                {"messageId": message_id, "threadId": thread_id},
            )
            raise AdapterError("WhatsApp typing indicator failed", "whatsapp")

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages. Not supported by WhatsApp Cloud API."""
        self._logger.debug("fetchMessages not supported on WhatsApp - message history is not available via Cloud API")
        return FetchResult(messages=[])

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info."""
        decoded = self.decode_thread_id(thread_id)

        return ThreadInfo(
            id=thread_id,
            channel_id=f"whatsapp:{decoded.phone_number_id}",
            channel_name=f"WhatsApp: {decoded.user_wa_id}",
            is_dm=True,
            metadata={"phone_number_id": decoded.phone_number_id, "user_wa_id": decoded.user_wa_id},
        )

    def encode_thread_id(self, platform_data: WhatsAppThreadId) -> str:
        """Encode a WhatsApp thread ID. Format: whatsapp:{phoneNumberId}:{userWaId}"""
        return f"whatsapp:{platform_data.phone_number_id}:{platform_data.user_wa_id}"

    def decode_thread_id(self, thread_id: str) -> WhatsAppThreadId:
        """Decode a WhatsApp thread ID. Format: whatsapp:{phoneNumberId}:{userWaId}"""
        if not thread_id.startswith("whatsapp:"):
            raise ValidationError("whatsapp", f"Invalid WhatsApp thread ID: {thread_id}")

        without_prefix = thread_id[9:]
        if not without_prefix:
            raise ValidationError("whatsapp", f"Invalid WhatsApp thread ID format: {thread_id}")

        parts = without_prefix.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValidationError("whatsapp", f"Invalid WhatsApp thread ID format: {thread_id}")

        return WhatsAppThreadId(phone_number_id=parts[0], user_wa_id=parts[1])

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID. On WhatsApp every conversation is a 1:1 DM."""
        return thread_id

    def is_dm(self, thread_id: str) -> bool:
        """All WhatsApp conversations are DMs."""
        return True

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a user. Returns the thread ID for the conversation.

        For WhatsApp, this simply constructs the thread ID since all
        conversations are inherently DMs. Note: you can only message users
        who have messaged you first (within the 24-hour window) or via
        approved template messages (see :meth:`send_template`).
        """
        return self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=self._phone_number_id,
                user_wa_id=user_id,
            )
        )

    def parse_message(self, raw: WhatsAppRawMessage) -> Message:
        """Parse platform message format to normalized format."""
        text = self._extract_text_content(raw["message"]) or ""
        formatted = self._format_converter.to_ast(text)
        attachments = self._build_attachments(raw["message"])
        contact = raw.get("contact")
        # A stored canonical user_id wins; otherwise derive the identity with
        # the same precedence the webhook path uses.
        identity = self._fields(raw["message"], contact)
        stored_user_id = raw.get("user_id")
        user_id = stored_user_id if stored_user_id is not None else (identity.user_id if identity else None)
        if not user_id:
            raise ValidationError("whatsapp", "WhatsApp message has no user identifier")
        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=raw["phone_number_id"],
                user_wa_id=user_id,
            )
        )

        author_identity = (
            _WhatsAppIdentity(user_id=user_id, bsuid=identity.bsuid, parent=identity.parent, phone=identity.phone)
            if identity
            else _WhatsAppIdentity(user_id=user_id)
        )

        return Message(
            id=raw["message"]["id"],
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            author=self._author(author_identity, contact),
            metadata=MessageMetadata(
                date_sent=datetime.fromtimestamp(
                    int(raw["message"].get("timestamp", "0")),
                    tz=timezone.utc,
                ),
                edited=False,
            ),
            attachments=attachments,
            raw=raw,
        )

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to WhatsApp markdown."""
        return self._format_converter.from_ast(content)

    async def mark_as_read(
        self,
        thread_id_or_message_id: str | None = None,
        message_id: str | None = None,
        message: Message | None = None,
    ) -> None:
        """Mark an inbound message as read.

        Called by ``Thread.mark_as_read()`` as ``(thread_id, message_id,
        message)``; the one-argument form ``mark_as_read(message_id)`` still
        works, as upstream's ``messageId ?? threadIdOrMessageId``. The first
        parameter is optional so the pre-#239 keyword call
        ``mark_as_read(message_id=...)`` keeps working too (Python-only:
        upstream has no keyword arguments).

        Raises :class:`AdapterError` unless the Graph API answers
        ``success: true``.

        See: https://developers.facebook.com/docs/whatsapp/cloud-api/messages/mark-messages-as-read
        """
        target = message_id if message_id is not None else thread_id_or_message_id
        if target is None:
            raise TypeError("mark_as_read() requires a message id")
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": target,
            },
        )

        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: upstream's
        # ``!response.success`` is truthiness; only a JSON ``true`` counts here.
        if not (isinstance(response, dict) and response.get("success") is True):
            raise AdapterError("WhatsApp mark as read failed", "whatsapp")

    # =========================================================================
    # Private helpers
    # =========================================================================

    async def _resolve_typing_target_message_id(self, thread_id: str) -> str | None:
        """Resolve the latest inbound message ID for a thread."""
        if not self._chat:
            return None

        state = self._chat.get_state()
        history = await ThreadHistoryCache(state).get_messages(thread_id)

        for message in reversed(history):
            if not message.author.is_me:
                return message.id

        return None

    async def _graph_api_request(self, path: str, body: Any) -> Any:
        """Make a request to the Meta Graph API."""
        return await self._graph_fetch_json(
            "POST",
            f"{self._graph_api_url}{path}",
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
            json_body=body,
            label="WhatsApp API error",
            context={"path": path},
        )

    async def _graph_api_upload(self, path: str, form: Any) -> Any:
        """Make a multipart upload request to the Meta Graph API.

        ``form`` is an ``aiohttp.FormData``; aiohttp sets the multipart
        ``Content-Type`` (with its boundary), so none is passed here.
        """
        return await self._graph_fetch_json(
            "POST",
            f"{self._graph_api_url}{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            data=form,
            label="WhatsApp API upload error",
            context={"path": path},
        )

    async def _graph_fetch_json(
        self,
        method: str,
        url: str,
        *,
        label: str,
        context: dict[str, Any],
        headers: dict[str, str],
        json_body: Any = None,
        data: Any = None,
    ) -> Any:
        """Fetch a Graph API endpoint and parse its JSON body.

        Transport failures and unparseable bodies become ``NetworkError``; a
        non-2xx response becomes a ``WhatsAppApiError`` carrying Meta's error
        envelope. ``label`` prefixes the error message and log line, and
        ``context`` is attached to the log line.
        """
        import aiohttp

        session = await self._get_http_session()
        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if data is not None:
            kwargs["data"] = data
        try:
            async with session.request(method, url, **kwargs) as response:
                status: int = response.status
                # Decode like WHATWG ``Response.text()`` ("UTF-8 decode": one
                # leading BOM stripped, invalid bytes replaced); aiohttp's
                # ``text()`` would guess the charset instead.
                body_text = (await response.read()).decode("utf-8-sig", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            # A failure while reading the body is wrapped too (upstream only
            # wraps ``fetch()``), so no raw aiohttp error escapes.
            self._logger.error(label, {**context, "error": str(error)})
            raise NetworkError("whatsapp", f"{label}: request failed", error) from error

        if not 200 <= status < 300:
            self._logger.error(label, {"status": status, "body": body_text, **context})
            raise WhatsAppApiError(label, status, body_text)

        try:
            # Parse the text rather than ``response.json()``: aiohttp rejects
            # non-``application/json`` content types (Graph can answer with
            # ``text/javascript``), while WHATWG ``Response.json()`` does not.
            return parse_json_text(body_text)
        except ValueError as error:
            self._logger.error(label, {"status": status, **context, "error": str(error)})
            raise NetworkError("whatsapp", f"{label}: response was not valid JSON", error) from error

    def _resolve_emoji(self, emoji: EmojiValue | str) -> str:
        """Resolve an emoji value to a unicode string."""
        return emoji_to_unicode(emoji)

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract body text from a request object."""
        # `hasattr` narrows `Any` → `object` (not awaitable); using
        # `getattr(..., None)` preserves `Any` for framework duck-typing.
        # Handle both callable and non-callable `request.text`. Gating
        # entry on callability would drop populated string attributes.
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
        body = getattr(request, "body", None)
        if body is not None:
            # Some frameworks expose `body` as an async method; call and
            # await if needed before treating as bytes/str.
            if callable(body):
                body = body()
            if inspect.isawaitable(body):
                body = await body
            if isinstance(body, (bytes, bytearray)):
                return body.decode("utf-8")
            return str(body)
        return ""

    @staticmethod
    def _get_header(request: Any, name: str) -> str | None:
        """Get a header value from a request object."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                # Case-insensitive lookup
                for k, v in headers.items():
                    if k.lower() == name.lower():
                        return v
                return None
            return headers.get(name)
        return None

    @staticmethod
    def _make_response(body: str, status: int) -> dict[str, Any]:
        """Create a response dict."""
        return {"body": body, "status": status}


def create_whatsapp_adapter(
    *,
    access_token: str | None = None,
    api_version: str | None = None,
    app_secret: str | None = None,
    logger: Logger | None = None,
    phone_number_id: str | None = None,
    user_name: str | None = None,
    verify_token: str | None = None,
) -> WhatsAppAdapter:
    """Factory function to create a WhatsApp adapter."""
    _logger = logger or ConsoleLogger("info").child("whatsapp")

    _access_token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN")
    if not _access_token:
        raise ValidationError(
            "whatsapp",
            "accessToken is required. Set WHATSAPP_ACCESS_TOKEN or provide it in config.",
        )

    _app_secret = app_secret or os.environ.get("WHATSAPP_APP_SECRET")
    if not _app_secret:
        raise ValidationError(
            "whatsapp",
            "appSecret is required. Set WHATSAPP_APP_SECRET or provide it in config.",
        )

    _phone_number_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not _phone_number_id:
        raise ValidationError(
            "whatsapp",
            "phoneNumberId is required. Set WHATSAPP_PHONE_NUMBER_ID or provide it in config.",
        )

    _verify_token = verify_token or os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if not _verify_token:
        raise ValidationError(
            "whatsapp",
            "verifyToken is required. Set WHATSAPP_VERIFY_TOKEN or provide it in config.",
        )

    _user_name = user_name or os.environ.get("WHATSAPP_BOT_USERNAME") or "whatsapp-bot"

    return WhatsAppAdapter(
        WhatsAppAdapterConfig(
            access_token=_access_token,
            api_version=api_version,
            app_secret=_app_secret,
            phone_number_id=_phone_number_id,
            verify_token=_verify_token,
            user_name=_user_name,
            logger=_logger,
        )
    )
