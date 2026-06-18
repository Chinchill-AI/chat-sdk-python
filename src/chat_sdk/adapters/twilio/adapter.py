"""Twilio adapter for chat SDK.

Supports SMS and MMS via Twilio Programmable Messaging: inbound message
webhooks (with X-Twilio-Signature verification) and outbound sends through
the Messages REST API. Conversations are 1:1 DMs keyed by the
(sender, recipient) address pair.

Python port of ``packages/adapter-twilio/src/index.ts`` (PR 2 of 2 of the
Twilio port; PR 1 added types, the format converter, and cards). Like
upstream — which deliberately avoids the ``twilio`` npm package — the
send paths are hand-rolled over the small REST surface in
:mod:`chat_sdk.adapters.twilio.api` (lazy aiohttp), keeping the adapter
free of the official SDK dependency.

See: https://www.twilio.com/docs/messaging
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict
from urllib.parse import urlparse

from chat_sdk.adapters.twilio.api import (
    delete_twilio_message,
    fetch_twilio_media,
    fetch_twilio_message,
    list_twilio_messages,
    send_twilio_message,
)
from chat_sdk.adapters.twilio.cards import card_to_twilio_text
from chat_sdk.adapters.twilio.format_converter import (
    TWILIO_MESSAGE_LIMIT,
    TwilioFormatConverter,
    truncate_twilio_text,
    twilio_text_or_placeholder,
)
from chat_sdk.adapters.twilio.thread import (
    decode_twilio_thread_id,
    encode_twilio_thread_id,
    twilio_channel_id,
)
from chat_sdk.adapters.twilio.types import (
    TwilioAdapterConfig,
    TwilioCredential,
    TwilioCredentials,
    TwilioHttpRequest,
    TwilioHttpResponse,
    TwilioMediaPayload,
    TwilioMessageResource,
    TwilioRawMessage,
    TwilioStatusPayload,
    TwilioTextPayload,
    TwilioThreadId,
    TwilioUnsupportedPayload,
    TwilioWebhookParseError,
    TwilioWebhookUrl,
    TwilioWebhookVerificationError,
    TwilioWebhookVerifier,
)
from chat_sdk.adapters.twilio.utils import attachment_type, sender_fields, twiml_response
from chat_sdk.adapters.twilio.webhook import read_twilio_webhook
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import (
    extract_card,
    extract_files,
    extract_postable_attachments,
)
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import (
    AdapterPostableMessage,
    Attachment,
    Author,
    ChatInstance,
    FetchOptions,
    FetchResult,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    PostableMarkdown,
    RawMessage,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    UserInfo,
    WebhookOptions,
)

# SSRF / credential-exfiltration guard for authenticated media downloads.
# Twilio media lives on api.twilio.com (redirecting to the Twilio CDN);
# Basic auth must never be forwarded to an arbitrary host rehydrated from
# persisted state. Divergence from upstream — see docs/UPSTREAM_SYNC.md
# (`rehydrate_attachment` URL allowlist rows; upstream fetches blindly).
_TRUSTED_MEDIA_HOSTS = frozenset({"twilio.com", "api.twilio.com"})
_TRUSTED_MEDIA_HOST_SUFFIXES = (".twilio.com", ".twiliocdn.com")


class _TwilioApiKwargs(TypedDict):
    """Common kwargs the adapter passes to the low-level API helpers."""

    api_url: str | None
    credentials: TwilioCredentials
    http_request: TwilioHttpRequest


class TwilioAdapter:
    """Twilio adapter for chat SDK.

    Implements the chat-sdk ``Adapter`` interface for Twilio Programmable
    Messaging (SMS/MMS). Voice webhooks are intentionally NOT routed
    through this adapter — see :mod:`chat_sdk.adapters.twilio.voice`.
    """

    def __init__(self, config: TwilioAdapterConfig | None = None) -> None:
        # Upstream's config is all-optional by design: ``account_sid`` /
        # ``auth_token`` resolve lazily (env fallback) at first API call,
        # so — unlike the Messenger adapter (#110 Q1) — neither the
        # constructor nor the factory can require credentials without
        # breaking upstream's `createTwilioAdapter()` contract (webhook
        # verification may be fully delegated to ``webhook_verifier`` and
        # the voice/webhook helpers are usable standalone). Misconfig
        # surfaces as ``AuthenticationError`` naming the missing env var.
        resolved = config if config is not None else TwilioAdapterConfig()

        self._name = "twilio"
        self._lock_scope: LockScope = "channel"
        self._persist_thread_history = True

        self._account_sid: TwilioCredential | None = resolved.account_sid
        self._api_url = resolved.api_url
        self._auth_token: TwilioCredential | None = resolved.auth_token
        self._config_http_request: TwilioHttpRequest | None = resolved.http_request
        self._logger: Logger = resolved.logger if resolved.logger is not None else ConsoleLogger("info").child("twilio")
        self._messaging_service_sid = resolved.resolved_messaging_service_sid()
        self._phone_number = resolved.resolved_phone_number()
        self._status_callback_url = resolved.status_callback_url
        self._user_name = resolved.user_name if resolved.user_name is not None else "bot"
        self._webhook_url: TwilioWebhookUrl | None = resolved.webhook_url
        self._webhook_verifier: TwilioWebhookVerifier | None = resolved.webhook_verifier
        self._format_converter = TwilioFormatConverter()

        self._chat: ChatInstance | None = None

        # Shared aiohttp session for connection pooling; created lazily.
        self._http_session: Any | None = None

    # =========================================================================
    # Adapter interface properties
    # =========================================================================

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
        # Twilio has no bot identity; authorship is derived from the
        # message ``direction``, mirroring upstream (no ``botUserId``).
        return None

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter (no identity fetch — Twilio has none)."""
        self._chat = chat
        self._logger.info(
            "Twilio adapter initialized",
            {
                "messagingServiceSid": self._messaging_service_sid,
                "phoneNumber": self._phone_number,
            },
        )

    async def disconnect(self) -> None:
        """Cleanup hook. Close the shared HTTP session if it was created."""
        if self._http_session is not None and not getattr(self._http_session, "closed", True):
            await self._http_session.close()
            self._http_session = None

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily on first use."""
        import aiohttp

        if self._http_session is None or getattr(self._http_session, "closed", True):
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _session_http_request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str | None,
    ) -> TwilioHttpResponse:
        """Shared-session transport handed to the low-level API helpers."""
        session = await self._get_http_session()
        async with session.request(method, url, headers=dict(headers), data=body) as response:
            return TwilioHttpResponse(status=response.status, body=await response.read())

    def _http_request(self) -> TwilioHttpRequest:
        """The configured transport, else the shared-session default."""
        if self._config_http_request is not None:
            return self._config_http_request
        return self._session_http_request

    # =========================================================================
    # Webhook
    # =========================================================================

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle an incoming Twilio messaging webhook.

        Signature failures return 401 (upstream's choice, matching this
        repo's convention); parse failures return 400; everything else is
        acknowledged with an empty TwiML response so Twilio doesn't send
        an error SMS back to the user.
        """
        try:
            payload = await read_twilio_webhook(
                request,
                auth_token=self._auth_token,
                webhook_url=self._webhook_url,
                webhook_verifier=self._webhook_verifier,
            )
        except TwilioWebhookVerificationError:
            return self._make_response("Invalid signature", 401)
        except TwilioWebhookParseError:
            return self._make_response("Invalid webhook", 400)

        if not isinstance(payload, TwilioTextPayload) or not self._chat:
            return twiml_response()

        thread_id = self.encode_thread_id(TwilioThreadId(recipient=payload.from_, sender=payload.to))
        message = self._parse_twilio_text_payload(payload, thread_id)
        self._chat.process_message(self, thread_id, message, options)
        return twiml_response()

    # =========================================================================
    # Sending messages
    # =========================================================================

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Send an SMS/MMS through the Messages API."""
        thread = self.decode_thread_id(thread_id)
        body = self._render_postable_text(message)
        media_url = self._media_urls(message)
        if not body and len(media_url) == 0:
            raise ValidationError("twilio", "Message text cannot be empty")

        raw = await send_twilio_message(
            to=thread.recipient,
            body=(twilio_text_or_placeholder(body) if (body or len(media_url) == 0) else None),
            media_url=media_url,
            status_callback_url=self._status_callback_url,
            **sender_fields(thread.sender),
            **self._api_options(),
        )

        return RawMessage(
            id=raw["sid"],
            thread_id=self._thread_id_for_resource(raw, thread),
            raw=raw,
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Twilio does not support editing sent messages — raises."""
        raise ChatNotImplementedError("twilio", "editMessage")

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a sent message resource by SID."""
        await delete_twilio_message(message_id, **self._api_options())

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: Any,
    ) -> None:
        """Twilio does not support message reactions — raises."""
        raise ChatNotImplementedError("twilio", "addReaction")

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: Any,
    ) -> None:
        """Twilio does not support message reactions — raises."""
        raise ChatNotImplementedError("twilio", "removeReaction")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """No-op: SMS has no typing indicator (mirrors upstream)."""

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Buffer all stream chunks and send as a single message.

        Twilio can't edit sent messages, so incremental post+edit
        streaming is impossible; accumulate-and-post mirrors the
        Messenger / WhatsApp adapters.
        """
        accumulated = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                accumulated += chunk
            elif getattr(chunk, "type", None) == "markdown_text":
                accumulated += getattr(chunk, "text", "")
        return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))

    # =========================================================================
    # Parsing
    # =========================================================================

    def parse_message(self, raw: TwilioRawMessage) -> Message:
        """Parse a raw webhook payload or Messages API resource."""
        if isinstance(raw, (TwilioTextPayload, TwilioStatusPayload, TwilioUnsupportedPayload)):
            if not isinstance(raw, TwilioTextPayload):
                raise ValidationError("twilio", "Cannot parse unsupported webhook")
            return self._parse_twilio_text_payload(
                raw,
                self.encode_thread_id(TwilioThreadId(recipient=raw.from_, sender=raw.to)),
            )
        return self._parse_twilio_resource(raw, None)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted AST content back to SMS text."""
        return self._format_converter.from_ast(content)

    # =========================================================================
    # Fetching
    # =========================================================================

    async def fetch_message(self, thread_id: str, message_id: str) -> Message | None:
        """Fetch a single message by SID; ``None`` on any API failure."""
        thread = self.decode_thread_id(thread_id)
        try:
            raw = await fetch_twilio_message(message_id, **self._api_options())
            return self._parse_twilio_resource(raw, thread)
        except Exception:
            return None

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch both directions of the conversation, merged by date."""
        opts = options if options is not None else FetchOptions()
        thread = self.decode_thread_id(thread_id)
        limit = opts.limit if opts.limit is not None else 50
        outbound, inbound = await asyncio.gather(
            list_twilio_messages(
                from_=thread.sender,
                limit=limit,
                to=thread.recipient,
                **self._api_options(),
            ),
            list_twilio_messages(
                from_=thread.recipient,
                limit=limit,
                to=thread.sender,
                **self._api_options(),
            ),
        )
        messages = [self._parse_twilio_resource(raw, thread) for raw in [*outbound, *inbound]]
        messages.sort(key=self._date_sort_key)
        return FetchResult(messages=messages[-limit:])

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Thread info derived from the address pair (always a DM)."""
        thread = self.decode_thread_id(thread_id)
        return ThreadInfo(
            id=thread_id,
            channel_id=self.channel_id_from_thread_id(thread_id),
            channel_name=thread.sender,
            is_dm=True,
            metadata={"recipient": thread.recipient, "sender": thread.sender},
        )

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Phone numbers are the only identity; echo the address back."""
        return UserInfo(
            full_name=user_id,
            is_bot=False,
            user_id=user_id,
            user_name=user_id,
        )

    # =========================================================================
    # Thread ID encoding
    # =========================================================================

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a phone number using the configured sender."""
        return self.encode_thread_id(TwilioThreadId(recipient=user_id, sender=self._default_sender()))

    def is_dm(self, thread_id: str) -> bool:
        """Every Twilio conversation is a DM."""
        return thread_id.startswith("twilio:")

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Channel = the bot-side sender address."""
        return twilio_channel_id(thread_id)

    def encode_thread_id(self, platform_data: TwilioThreadId) -> str:
        """Encode. Format: ``twilio:{sender}:{recipient}`` (URI-escaped)."""
        return encode_twilio_thread_id(platform_data)

    def decode_thread_id(self, thread_id: str) -> TwilioThreadId:
        """Decode a ``twilio:{sender}:{recipient}`` thread ID."""
        return decode_twilio_thread_id(thread_id)

    # =========================================================================
    # Attachments
    # =========================================================================

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Twilio attachment.

        Reads the media URL from ``fetch_metadata`` (key ``twilioMediaUrl``
        — camelCase for cross-SDK state parity with upstream) and rebuilds
        the authenticated downloader. Returns the attachment unchanged when
        no URL is present.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        meta_url = meta.get("twilioMediaUrl")
        url = meta_url if meta_url is not None else attachment.url
        if not url:
            return attachment
        return self._twilio_attachment(TwilioMediaPayload(url=url, content_type=attachment.mime_type))

    def _twilio_attachment(self, media: TwilioMediaPayload) -> Attachment:
        """Build an Attachment with an authenticated lazy downloader."""
        return Attachment(
            type=attachment_type(media.content_type),
            url=media.url,
            mime_type=media.content_type,
            fetch_data=self._make_media_downloader(media.url),
            fetch_metadata={"twilioMediaUrl": media.url},
        )

    def _make_media_downloader(self, url: str) -> Callable[[], Awaitable[bytes]]:
        """Closure downloading ``url`` with adapter credentials.

        Named helper so the ``url`` capture isn't subject to late binding
        when a message carries multiple media items. The URL is validated
        inside the closure (not at build time) so a trusted-at-parse-time
        URL still fails closed if the allowlist tightens later.
        """

        async def _download() -> bytes:
            if not _is_trusted_twilio_media_url(url):
                # Divergence from upstream — see docs/UPSTREAM_SYNC.md
                # (SSRF guard: never forward Basic auth off-platform).
                raise ValidationError(
                    "twilio",
                    f"Refusing to fetch Twilio media from untrusted URL: {url}",
                )
            return await fetch_twilio_media(
                url,
                credentials=self._credentials(),
                http_request=self._http_request(),
            )

        return _download

    # =========================================================================
    # Message parsing internals
    # =========================================================================

    def _parse_twilio_text_payload(self, raw: TwilioTextPayload, thread_id: str) -> Message:
        message_id = raw.message_sid if raw.message_sid is not None else f"twilio:{int(time.time() * 1000)}"
        return Message(
            id=message_id,
            thread_id=thread_id,
            text=raw.body,
            formatted=self._format_converter.to_ast(raw.body),
            author=self._author(raw.from_, False),
            metadata=MessageMetadata(
                date_sent=datetime.now(tz=timezone.utc),
                edited=False,
            ),
            attachments=[self._twilio_attachment(media) for media in raw.media],
            raw=raw,
        )

    def _parse_twilio_resource(
        self,
        raw: TwilioMessageResource,
        fallback_thread: TwilioThreadId | None,
    ) -> Message:
        direction = raw.get("direction")
        is_me = direction.startswith("outbound") if direction is not None else False

        from_value = raw.get("from")
        if from_value is None:
            from_value = raw.get("messaging_service_sid")
        if from_value is None and fallback_thread is not None:
            from_value = fallback_thread.sender if is_me else fallback_thread.recipient

        to_value = raw.get("to")
        if to_value is None and fallback_thread is not None:
            to_value = fallback_thread.recipient if is_me else fallback_thread.sender

        if not (from_value and to_value):
            raise ValidationError("twilio", "Twilio message is missing routing")

        text = raw.get("body")
        text = text if text is not None else ""

        if is_me:
            thread = TwilioThreadId(
                recipient=fallback_thread.recipient if fallback_thread is not None else to_value,
                sender=fallback_thread.sender if fallback_thread is not None else from_value,
            )
        else:
            thread = TwilioThreadId(recipient=from_value, sender=to_value)

        date_value = raw.get("date_sent")
        if date_value is None:
            date_value = raw.get("date_created")

        return Message(
            id=raw["sid"],
            thread_id=self.encode_thread_id(thread),
            text=text,
            formatted=self._format_converter.to_ast(text),
            author=self._author(thread.sender if is_me else from_value, is_me),
            metadata=MessageMetadata(
                date_sent=_date_from_twilio(date_value),
                edited=False,
            ),
            attachments=[],
            raw=raw,
        )

    def _thread_id_for_resource(self, raw: TwilioMessageResource, fallback: TwilioThreadId) -> str:
        """The stable thread ID for a sent resource (fallback-pinned)."""
        return self._parse_twilio_resource(raw, fallback).thread_id

    @staticmethod
    def _author(user_id: str, is_me: bool) -> Author:
        return Author(
            full_name=user_id,
            is_bot=is_me,
            is_me=is_me,
            user_id=user_id,
            user_name=user_id,
        )

    @staticmethod
    def _date_sort_key(message: Message) -> float:
        return message.metadata.date_sent.timestamp() if message.metadata else 0.0

    # =========================================================================
    # Send helpers
    # =========================================================================

    def _render_postable_text(self, message: AdapterPostableMessage) -> str:
        card = extract_card(message)
        text = card_to_twilio_text(card) if card else self._format_converter.render_postable(message)
        return truncate_twilio_text(text, limit=TWILIO_MESSAGE_LIMIT).text

    @staticmethod
    def _media_urls(message: AdapterPostableMessage) -> list[str]:
        """Public media URLs from postable attachments.

        Binary ``files`` uploads are rejected: Twilio's Messages API only
        accepts media by URL it can fetch itself.
        """
        files = extract_files(message)
        if len(files) > 0:
            raise ValidationError(
                "twilio",
                "Twilio adapter supports media attachments by public URL only",
            )
        media_url: list[str] = []
        for attachment in extract_postable_attachments(message):
            # Duck-typed like upstream: postable attachments may arrive as
            # ``Attachment`` dataclasses or raw dicts.
            url = attachment.get("url") if isinstance(attachment, dict) else getattr(attachment, "url", None)
            if not isinstance(url, str) or len(url) == 0:
                raise ValidationError(
                    "twilio",
                    "Twilio adapter supports media attachments by public URL only",
                )
            media_url.append(url)
        return media_url

    def _credentials(self) -> TwilioCredentials:
        return TwilioCredentials(account_sid=self._account_sid, auth_token=self._auth_token)

    def _api_options(self) -> _TwilioApiKwargs:
        """Common kwargs for the low-level API helpers."""
        return {
            "api_url": self._api_url,
            "credentials": self._credentials(),
            "http_request": self._http_request(),
        }

    def _default_sender(self) -> str:
        sender = self._phone_number if self._phone_number is not None else self._messaging_service_sid
        if not sender:
            raise ValidationError("twilio", "phoneNumber or messagingServiceSid is required")
        return sender

    @staticmethod
    def _make_response(body: str, status: int) -> dict[str, Any]:
        """Create a framework-agnostic response dict."""
        return {"body": body, "status": status}


# =============================================================================
# Module helpers
# =============================================================================


def _date_from_twilio(value: str | None) -> datetime:
    """Parse a Twilio date (RFC 2822, occasionally ISO 8601) to aware UTC.

    Falls back to "now" for missing/unparseable values, mirroring
    upstream's ``dateFromTwilio`` NaN guard.
    """
    if value:
        try:
            parsed = parsedate_to_datetime(value)
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


def _is_trusted_twilio_media_url(url: str) -> bool:
    """True when ``url`` is an https URL on a Twilio-owned host."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host in _TRUSTED_MEDIA_HOSTS:
        return True
    return host.endswith(_TRUSTED_MEDIA_HOST_SUFFIXES)


# =============================================================================
# Factory
# =============================================================================


def create_twilio_adapter(
    *,
    account_sid: TwilioCredential | None = None,
    api_url: str | None = None,
    auth_token: TwilioCredential | None = None,
    http_request: TwilioHttpRequest | None = None,
    logger: Logger | None = None,
    messaging_service_sid: str | None = None,
    phone_number: str | None = None,
    status_callback_url: str | None = None,
    user_name: str | None = None,
    webhook_url: TwilioWebhookUrl | None = None,
    webhook_verifier: TwilioWebhookVerifier | None = None,
) -> TwilioAdapter:
    """Factory for ``TwilioAdapter`` with TWILIO_* env fallbacks.

    Mirrors upstream ``createTwilioAdapter``: every argument is optional
    (see the constructor note on why required-credential validation —
    the Messenger #110 Q1 answer — does not transfer to Twilio).
    """
    return TwilioAdapter(
        TwilioAdapterConfig(
            account_sid=account_sid,
            api_url=api_url,
            auth_token=auth_token,
            http_request=http_request,
            logger=logger,
            messaging_service_sid=messaging_service_sid,
            phone_number=phone_number,
            status_callback_url=status_callback_url,
            user_name=user_name,
            webhook_url=webhook_url,
            webhook_verifier=webhook_verifier,
        )
    )
