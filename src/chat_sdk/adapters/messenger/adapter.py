"""Messenger (Meta) adapter for chat SDK.

Supports messaging via the Meta Messenger Platform (Graph API).
All conversations are 1:1 DMs between the Page and a user (PSID).

Python port of ``packages/adapter-messenger/src/index.ts`` (PR 2 of 2 of
the Messenger port; PR 1 added types, format converter, and cards).

See: https://developers.facebook.com/docs/messenger-platform
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os
import time
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from chat_sdk.adapters.messenger.cards import (
    MessengerCardResultTemplate,
    MessengerCardResultText,
    card_to_messenger,
    decode_messenger_callback_data,
)
from chat_sdk.adapters.messenger.format_converter import MessengerFormatConverter
from chat_sdk.adapters.messenger.types import (
    ENV_APP_SECRET,
    ENV_PAGE_ACCESS_TOKEN,
    ENV_VERIFY_TOKEN,
    MessengerAdapterConfig,
    MessengerMessagingEvent,
    MessengerRawMessage,
    MessengerSendApiResponse,
    MessengerTemplatePayload,
    MessengerThreadId,
    MessengerUserProfile,
    MessengerWebhookPayload,
)
from chat_sdk.emoji import convert_emoji_placeholders, default_emoji_resolver
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import (
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
    ResourceNotFoundError,
    ValidationError,
)
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    Attachment,
    Author,
    ChannelInfo,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    WebhookOptions,
)

# Meta Graph API base URL
GRAPH_API_BASE = "https://graph.facebook.com"
# Default Graph API version (matches upstream)
DEFAULT_API_VERSION = "v21.0"
# Maximum text length the Send API will accept in a single message
MESSENGER_MESSAGE_LIMIT = 2000
# Suffix that encodes a sequence number on Send-API mids (``mid.abc:2`` etc.)
# Used to disambiguate identical-timestamp messages in the local cache.
_MESSAGE_SEQUENCE_SUFFIX = ":"


class MessengerAdapter:
    """Messenger (Meta) adapter for chat SDK.

    Implements the chat-sdk ``Adapter`` interface for the Messenger Platform.
    """

    def __init__(self, config: MessengerAdapterConfig) -> None:
        # Upstream's constructor takes the resolved credentials directly. Our
        # ``MessengerAdapterConfig`` exposes them via ``resolved_*`` helpers so
        # the constructor and the factory both go through the same fallback
        # chain. Constructing the adapter without resolved credentials is a
        # programmer error — the factory enforces presence, and direct callers
        # opt in to env-var fallbacks by leaving fields as ``None``.
        app_secret = config.resolved_app_secret()
        page_access_token = config.resolved_page_access_token()
        verify_token = config.resolved_verify_token()
        if not app_secret:
            raise ValidationError(
                "messenger",
                f"appSecret is required. Set {ENV_APP_SECRET} or provide it in config.",
            )
        if not page_access_token:
            raise ValidationError(
                "messenger",
                f"pageAccessToken is required. Set {ENV_PAGE_ACCESS_TOKEN} or provide it in config.",
            )
        if not verify_token:
            raise ValidationError(
                "messenger",
                f"verifyToken is required. Set {ENV_VERIFY_TOKEN} or provide it in config.",
            )

        self._name = "messenger"
        self._lock_scope: LockScope = "channel"
        self._persist_message_history = True

        self._app_secret = app_secret
        self._page_access_token = page_access_token
        self._verify_token = verify_token
        self._api_version = config.api_version or DEFAULT_API_VERSION
        self._graph_api_url = f"{GRAPH_API_BASE}/{self._api_version}"
        self._logger: Logger = config.logger
        self._format_converter = MessengerFormatConverter()

        # If a user_name is provided we treat it as explicit and never
        # overwrite it from ``/me`` / ``chat.get_user_name()`` — matches the
        # upstream ``hasExplicitUserName`` gate. ``is not None`` (not truthy)
        # so an explicit ``user_name=""`` is respected rather than silently
        # replaced by the ``"bot"`` fallback.
        self._has_explicit_user_name = config.user_name is not None
        self._user_name = config.user_name if config.user_name is not None else "bot"

        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None

        # Local caches. Messenger has no message-history API, so the adapter
        # holds every parsed message in memory to back ``fetch_messages``.
        self._message_cache: dict[str, list[Message]] = {}
        self._user_profile_cache: dict[str, MessengerUserProfile] = {}

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
    def persist_message_history(self) -> bool:
        return self._persist_message_history

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch Page identity (best-effort)."""
        self._chat = chat

        if not self._has_explicit_user_name:
            self._user_name = chat.get_user_name()

        try:
            me = await self._graph_api_fetch("me", method="GET")
            self._bot_user_id = cast(str, me.get("id"))
            name = me.get("name")
            if not self._has_explicit_user_name and name:
                self._user_name = name
            self._logger.info(
                "Messenger adapter initialized",
                {"botUserId": self._bot_user_id, "userName": self._user_name},
            )
        except Exception as error:
            # Match upstream: identity fetch failure is non-fatal; the
            # adapter still functions for incoming webhook events.
            self._logger.warn(
                "Failed to fetch Messenger page identity",
                {"error": str(error)},
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

    # =========================================================================
    # Webhook
    # =========================================================================

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle an incoming webhook request.

        - GET: webhook subscription verification challenge.
        - POST: event notifications (messages, postbacks, reactions, ...).
        """
        method = getattr(request, "method", "POST")
        if method == "GET":
            return self._handle_verification(request)

        body = await self._get_request_body(request)

        signature = self._get_header(request, "x-hub-signature-256")
        if not self._verify_signature(body, signature):
            self._logger.warn("Messenger webhook rejected due to invalid signature")
            return self._make_response("Invalid signature", 403)

        try:
            payload: MessengerWebhookPayload = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return self._make_response("Invalid JSON", 400)

        if payload.get("object") != "page":
            return self._make_response("Not a page subscription", 404)

        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring Messenger webhook")
            return self._make_response("EVENT_RECEIVED", 200)

        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                message = event.get("message")
                if message and not message.get("is_echo"):
                    self._handle_incoming_message(event, options)
                elif message and message.get("is_echo"):
                    self._handle_echo(event)

                if event.get("postback"):
                    self._handle_postback(event, options)

                if event.get("reaction"):
                    self._handle_reaction(event, options)

                delivery = event.get("delivery")
                if delivery:
                    self._logger.debug(
                        "Message delivery confirmation",
                        {
                            "watermark": delivery.get("watermark"),
                            "mids": delivery.get("mids"),
                        },
                    )

                read = event.get("read")
                if read:
                    self._logger.debug(
                        "Message read confirmation",
                        {"watermark": read.get("watermark")},
                    )

        return self._make_response("EVENT_RECEIVED", 200)

    def _handle_verification(self, request: Any) -> Any:
        """Handle the GET subscription challenge."""
        url = getattr(request, "url", "")
        parsed = urlparse(str(url))
        params = parse_qs(parsed.query)

        mode = (params.get("hub.mode") or [None])[0]
        token = (params.get("hub.verify_token") or [None])[0]
        challenge = (params.get("hub.challenge") or [""])[0]

        if mode == "subscribe" and token == self._verify_token:
            self._logger.info("Messenger webhook verified")
            return self._make_response(challenge, 200)

        self._logger.warn("Messenger webhook verification failed")
        return self._make_response("Forbidden", 403)

    def _verify_signature(self, body: bytes, signature: str | None) -> bool:
        """Verify the ``X-Hub-Signature-256`` header (HMAC-SHA256, App Secret).

        Format: ``sha256=<hex>``. Returns ``False`` on any malformed input.
        Q3 (see #110): we keep upstream's hard-wired ``app_secret``-based
        HMAC scheme. A swappable verifier (the pattern used by the Slack
        adapter) would diverge from upstream's contract and isn't justified
        for a single-secret Meta integration; flagged as a possible future
        divergence but not introduced here.
        """
        if not signature:
            return False

        # Header is ``algo=hash``. Reject anything that isn't sha256 hex.
        parts = signature.split("=", 1)
        if len(parts) != 2:
            return False
        algo, hash_hex = parts
        if algo != "sha256" or not hash_hex:
            return False

        try:
            computed_hex = hmac.new(
                self._app_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            # Compare hex strings. ``hexdigest()`` is lowercase; Node's
            # ``Buffer.from(hex)`` is case-insensitive, so upstream accepts an
            # uppercase-hex signature. Normalize the header hash to lowercase
            # before the constant-time compare to match that behavior.
            return hmac.compare_digest(hash_hex.lower(), computed_hex)
        except Exception:
            self._logger.warn("Failed to verify Messenger webhook signature")
            return False

    # =========================================================================
    # Event handlers
    # =========================================================================

    def _handle_incoming_message(
        self,
        event: MessengerMessagingEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        if not self._chat:
            return

        sender = event.get("sender") or {}
        thread_id = self.encode_thread_id(MessengerThreadId(recipient_id=sender.get("id", "")))

        parsed = self._parse_messenger_message(event, thread_id)
        self._cache_message(parsed)
        self._chat.process_message(self, thread_id, parsed, options)

    def _handle_echo(self, event: MessengerMessagingEvent) -> None:
        """Cache echoed (bot-sent) messages but don't dispatch them."""
        if not event.get("message"):
            return

        recipient = event.get("recipient") or {}
        thread_id = self.encode_thread_id(MessengerThreadId(recipient_id=recipient.get("id", "")))

        parsed = self._parse_messenger_message(event, thread_id)
        self._cache_message(parsed)

    def _handle_postback(
        self,
        event: MessengerMessagingEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        postback = event.get("postback")
        if not (self._chat and postback):
            return

        sender = event.get("sender") or {}
        sender_id = sender.get("id", "")
        thread_id = self.encode_thread_id(MessengerThreadId(recipient_id=sender_id))

        decoded = decode_messenger_callback_data(postback.get("payload"))
        action_id = decoded.get("action_id") or ""
        value = decoded.get("value")

        message_id = postback.get("mid") or f"postback:{event.get('timestamp', 0)}"

        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,  # filled in by Chat
                thread_id=thread_id,
                message_id=message_id,
                user=Author(
                    user_id=sender_id,
                    user_name=sender_id,
                    full_name=sender_id,
                    is_bot=False,
                    is_me=False,
                ),
                action_id=action_id,
                value=value,
                raw=event,
            ),
            options,
        )

    def _handle_reaction(
        self,
        event: MessengerMessagingEvent,
        options: WebhookOptions | None = None,
    ) -> None:
        reaction = event.get("reaction")
        if not (self._chat and reaction):
            return

        sender = event.get("sender") or {}
        sender_id = sender.get("id", "")
        thread_id = self.encode_thread_id(MessengerThreadId(recipient_id=sender_id))

        added = reaction.get("action") == "react"
        raw_emoji = reaction.get("emoji", "")
        # Resolve to an EmojiValue using the gchat-style raw unicode resolver,
        # mirroring upstream's ``defaultEmojiResolver.fromGChat(...)`` call.
        emoji_value = default_emoji_resolver.from_gchat(raw_emoji)

        self._chat.process_reaction(
            ReactionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=reaction.get("mid", ""),
                user=Author(
                    user_id=sender_id,
                    user_name=sender_id,
                    full_name=sender_id,
                    is_bot=False,
                    is_me=False,
                ),
                emoji=emoji_value,
                raw_emoji=raw_emoji,
                added=added,
                raw=event,
            ),
            options,
        )

    # =========================================================================
    # Sending messages
    # =========================================================================

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Send a message to a Messenger user."""
        card = extract_card(message)
        if card:
            result = card_to_messenger(card)
            if result.get("type") == "template":
                template_payload = cast(MessengerCardResultTemplate, result)["payload"]
                # Convert emoji placeholders inside the JSON-encoded payload
                # to mirror upstream's ``convertEmojiPlaceholders`` pass.
                converted = cast(
                    MessengerTemplatePayload,
                    json.loads(
                        convert_emoji_placeholders(
                            json.dumps(template_payload),
                            "messenger",
                        )
                    ),
                )
                return await self._send_template_message(thread_id, converted)
            # Text fallback
            return await self._send_text_message(
                thread_id,
                convert_emoji_placeholders(
                    cast(MessengerCardResultText, result)["text"],
                    "messenger",
                ),
            )

        # Regular text/markdown/AST message
        text = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "messenger",
        )
        return await self._send_text_message(thread_id, text)

    async def _send_text_message(self, thread_id: str, text: str) -> RawMessage:
        """Send a plain text message via the Send API."""
        recipient_id = self._resolve_thread_id(thread_id).recipient_id
        truncated = self._truncate_message(text)

        if not truncated.strip():
            raise ValidationError("messenger", "Message text cannot be empty")

        result: MessengerSendApiResponse = await self._graph_api_fetch(
            "me/messages",
            method="POST",
            body={
                "recipient": {"id": recipient_id},
                "message": {"text": truncated},
                "messaging_type": "RESPONSE",
            },
        )

        raw_event: MessengerMessagingEvent = {
            "sender": {"id": self._bot_user_id or ""},
            "recipient": {"id": recipient_id},
            "timestamp": int(time.time() * 1000),
            "message": {
                "mid": result["message_id"],
                "text": truncated,
                "is_echo": True,
            },
        }

        parsed = self._parse_messenger_message(raw_event, thread_id)
        self._cache_message(parsed)

        return RawMessage(
            id=result["message_id"],
            thread_id=thread_id,
            raw=raw_event,
        )

    async def _send_template_message(
        self,
        thread_id: str,
        payload: MessengerTemplatePayload,
    ) -> RawMessage:
        """Send a Generic / Button template message via the Send API."""
        recipient_id = self._resolve_thread_id(thread_id).recipient_id

        result: MessengerSendApiResponse = await self._graph_api_fetch(
            "me/messages",
            method="POST",
            body={
                "recipient": {"id": recipient_id},
                "message": {
                    "attachment": {
                        "type": "template",
                        "payload": payload,
                    },
                },
                "messaging_type": "RESPONSE",
            },
        )

        raw_event: MessengerMessagingEvent = {
            "sender": {"id": self._bot_user_id or ""},
            "recipient": {"id": recipient_id},
            "timestamp": int(time.time() * 1000),
            "message": {
                "mid": result["message_id"],
                "is_echo": True,
            },
        }

        parsed = self._parse_messenger_message(raw_event, thread_id)
        self._cache_message(parsed)

        return RawMessage(
            id=result["message_id"],
            thread_id=thread_id,
            raw=raw_event,
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Messenger Send API does not support editing — raises."""
        raise ValidationError("messenger", "Messenger does not support editing messages")

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Buffer all stream chunks and send as a single message.

        Messenger doesn't support message edits, so we can't incrementally
        update the same message. Mirrors upstream's behavior exactly.
        """
        accumulated = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                accumulated += chunk
            elif getattr(chunk, "type", None) == "markdown_text":
                accumulated += getattr(chunk, "text", "")
        return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Messenger Send API does not support deleting — raises."""
        raise ValidationError("messenger", "Messenger does not support deleting messages")

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Messenger Send API does not expose reaction send — raises."""
        raise ValidationError("messenger", "Messenger does not support reactions via API")

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Messenger Send API does not expose reaction send — raises."""
        raise ValidationError("messenger", "Messenger does not support reactions via API")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Send a ``typing_on`` sender_action via the Send API."""
        recipient_id = self._resolve_thread_id(thread_id).recipient_id
        await self._graph_api_fetch(
            "me/messages",
            method="POST",
            body={
                "recipient": {"id": recipient_id},
                "sender_action": "typing_on",
            },
        )

    # =========================================================================
    # Fetching
    # =========================================================================

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from the local cache (Messenger has no history API)."""
        opts = options or FetchOptions()
        messages = list(self._message_cache.get(thread_id, []))
        messages.sort(key=self._sort_key)
        return self._paginate_messages(messages, opts)

    async def fetch_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> Message | None:
        """Fetch a single cached message by ID across all thread caches."""
        for messages in self._message_cache.values():
            for msg in messages:
                if msg.id == message_id:
                    return msg
        return None

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info, hydrated with the user profile when available."""
        recipient_id = self._resolve_thread_id(thread_id).recipient_id
        profile = await self._fetch_user_profile(recipient_id)
        display_name = self._profile_display_name(profile)

        # On Messenger every conversation is a 1:1 DM, so channel == thread.
        return ThreadInfo(
            id=thread_id,
            channel_id=thread_id,
            channel_name=display_name,
            is_dm=True,
            metadata={"profile": dict(profile)},
        )

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel info (channel == thread on Messenger)."""
        recipient_id = self._resolve_thread_id(channel_id).recipient_id
        profile = await self._fetch_user_profile(recipient_id)
        display_name = self._profile_display_name(profile)

        return ChannelInfo(
            id=channel_id,
            name=display_name,
            is_dm=True,
            metadata={"profile": dict(profile)},
        )

    # =========================================================================
    # Thread ID encoding
    # =========================================================================

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """On Messenger every conversation is a 1:1 DM, channel == thread."""
        return thread_id

    def is_dm(self, thread_id: str) -> bool:
        """All Messenger conversations are DMs."""
        return True

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a user. Returns the encoded thread ID."""
        return self.encode_thread_id(MessengerThreadId(recipient_id=user_id))

    def encode_thread_id(self, platform_data: MessengerThreadId) -> str:
        """Encode a Messenger thread ID. Format: ``messenger:{recipientId}``."""
        return f"messenger:{platform_data.recipient_id}"

    def decode_thread_id(self, thread_id: str) -> MessengerThreadId:
        """Decode a Messenger thread ID. Format: ``messenger:{recipientId}``."""
        parts = thread_id.split(":")
        if parts[0] != "messenger" or len(parts) != 2:
            raise ValidationError("messenger", f"Invalid Messenger thread ID: {thread_id}")
        recipient_id = parts[1]
        if not recipient_id:
            raise ValidationError("messenger", f"Invalid Messenger thread ID: {thread_id}")
        return MessengerThreadId(recipient_id=recipient_id)

    def _resolve_thread_id(self, value: str) -> MessengerThreadId:
        """Resolve a value to a ``MessengerThreadId``.

        Accepts both encoded thread IDs (``messenger:PSID``) and bare PSIDs,
        mirroring upstream's ``resolveThreadId`` helper.
        """
        if value.startswith("messenger:"):
            return self.decode_thread_id(value)
        return MessengerThreadId(recipient_id=value)

    # =========================================================================
    # Parsing
    # =========================================================================

    def parse_message(self, raw: MessengerRawMessage) -> Message:
        """Parse a raw messaging event into a normalized ``Message``."""
        sender = raw.get("sender") or {}
        thread_id = self.encode_thread_id(MessengerThreadId(recipient_id=sender.get("id", "")))
        message = self._parse_messenger_message(raw, thread_id)
        self._cache_message(message)
        return message

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted AST content back to Messenger text."""
        return self._format_converter.from_ast(content)

    def _parse_messenger_message(
        self,
        event: MessengerMessagingEvent,
        thread_id: str,
    ) -> Message:
        message = event.get("message") or {}
        postback = event.get("postback") or {}
        sender = event.get("sender") or {}
        text = message.get("text") or postback.get("title") or ""
        is_echo = bool(message.get("is_echo"))
        is_me = is_echo or (sender.get("id") == self._bot_user_id and self._bot_user_id is not None)

        mid = message.get("mid")
        timestamp = event.get("timestamp", 0)
        msg_id = mid if mid else f"event:{timestamp}"

        return Message(
            id=msg_id,
            thread_id=thread_id,
            text=text,
            formatted=self._format_converter.to_ast(text),
            raw=event,
            author=Author(
                user_id=sender.get("id", ""),
                user_name=sender.get("id", ""),
                full_name=sender.get("id", ""),
                is_bot=is_me,
                is_me=is_me,
            ),
            metadata=MessageMetadata(
                date_sent=datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc),
                edited=False,
            ),
            attachments=self._extract_attachments(event),
            is_mention=True,
        )

    def _extract_attachments(self, event: MessengerMessagingEvent) -> list[Attachment]:
        message = event.get("message") or {}
        attachments = message.get("attachments") or []
        result: list[Attachment] = []
        for attachment in attachments:
            payload = attachment.get("payload") or {}
            url = payload.get("url")
            if not url:
                continue
            result.append(
                Attachment(
                    type=self._map_attachment_type(attachment.get("type", "")),
                    url=url,
                    fetch_data=self._make_attachment_downloader(url),
                    # Persist the download URL so the closure can be rebuilt
                    # by ``rehydrate_attachment`` after the message survives
                    # a queue/debounce/burst JSON roundtrip (which drops the
                    # ``fetch_data`` closure).  Messenger payload URLs are
                    # signature-gated by Meta and require no auth header, so
                    # the URL alone is sufficient to rebuild the closure.
                    fetch_metadata={"url": url},
                )
            )
        return result

    def _make_attachment_downloader(self, url: str) -> Any:
        """Build a closure that downloads ``url`` lazily.

        Kept as a named helper so the ``url`` capture isn't subject to
        late-binding when multiple attachments are extracted from a single
        event.
        """

        async def _download() -> bytes:
            return await self._download_attachment(url)

        return _download

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Messenger attachment.

        Called by :class:`~chat_sdk.chat.Chat` during message rehydration in
        the queue/debounce/burst concurrency paths, where the original
        ``fetch_data`` closure was dropped during JSON serialization.  Reads
        the download URL from ``attachment.fetch_metadata`` (populated by
        :meth:`_extract_attachments`) and rebuilds the lazy downloader that
        reuses the shared aiohttp session.  Returns the attachment unchanged
        when no URL is present — matching the upstream documented "leave
        unchanged when no hook" degraded-mode behavior.

        Mirrors :meth:`WhatsAppAdapter.rehydrate_attachment` exactly (both
        platforms are Meta-family and share the same queue-mode failure
        shape).  Like the original ``_download_attachment``, the rebuilt
        closure attaches no auth headers — Messenger payload URLs are
        signature-gated by Meta and do not require Bearer tokens.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        url = meta.get("url")
        if not url:
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
            fetch_data=self._make_attachment_downloader(url),
            fetch_metadata=attachment.fetch_metadata,
        )

    @staticmethod
    def _map_attachment_type(fb_type: str) -> str:
        """Map a Messenger attachment type to the SDK ``Attachment.type`` enum."""
        if fb_type == "image":
            return "image"
        if fb_type == "video":
            return "video"
        if fb_type == "audio":
            return "audio"
        return "file"

    async def _download_attachment(self, url: str) -> bytes:
        """Download an attachment payload URL. Wraps errors in ``NetworkError``."""
        try:
            session = await self._get_http_session()
            async with session.get(url) as response:
                if response.status != 200:
                    raise NetworkError(
                        "messenger",
                        f"Failed to download Messenger attachment: {response.status}",
                    )
                return await response.read()
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError(
                "messenger",
                "Failed to download Messenger attachment",
                original_error=error if isinstance(error, Exception) else None,
            ) from error

    # =========================================================================
    # User profile (with cache)
    # =========================================================================

    async def _fetch_user_profile(self, user_id: str) -> MessengerUserProfile:
        cached = self._user_profile_cache.get(user_id)
        # ``is not None`` (not truthy): an empty-dict ``{}`` cache entry is
        # falsy and would otherwise trigger a re-fetch on every call.
        if cached is not None:
            return cached

        try:
            profile = await self._graph_api_fetch(
                user_id,
                method="GET",
                query_params={"fields": "first_name,last_name,profile_pic"},
            )
            if not isinstance(profile, dict):
                return {"id": user_id}
            self._user_profile_cache[user_id] = profile
            return cast(MessengerUserProfile, profile)
        except Exception:
            # On any error, fall back to a minimal profile carrying just the
            # user ID. Matches upstream's silent fallback.
            return {"id": user_id}

    @staticmethod
    def _profile_display_name(profile: MessengerUserProfile) -> str:
        parts = [p for p in (profile.get("first_name"), profile.get("last_name")) if p]
        if parts:
            return " ".join(parts)
        return profile.get("id", "")

    # =========================================================================
    # Cache / pagination
    # =========================================================================

    def _cache_message(self, message: Message) -> None:
        existing = self._message_cache.get(message.thread_id, [])
        for i, item in enumerate(existing):
            if item.id == message.id:
                existing[i] = message
                break
        else:
            existing.append(message)
        existing.sort(key=self._sort_key)
        self._message_cache[message.thread_id] = existing

    @staticmethod
    def _sort_key(message: Message) -> tuple[float, int]:
        """Sort by ``(date_sent, sequence_suffix)``.

        Messages with the same timestamp but a ``:N`` sequence suffix on the
        ID are ordered by N. Mirrors upstream's ``compareMessages``.
        """
        date_value = message.metadata.date_sent.timestamp() if message.metadata else 0.0
        seq = 0
        if _MESSAGE_SEQUENCE_SUFFIX in message.id:
            tail = message.id.rsplit(_MESSAGE_SEQUENCE_SUFFIX, 1)[1]
            if tail.isdigit():
                seq = int(tail)
        return (date_value, seq)

    @staticmethod
    def _paginate_messages(messages: list[Message], options: FetchOptions) -> FetchResult:
        limit = max(1, min(options.limit if options.limit is not None else 50, 100))
        direction = options.direction or "backward"

        if not messages:
            return FetchResult(messages=[])

        index_by_id = {m.id: i for i, m in enumerate(messages)}

        if direction == "backward":
            end = index_by_id[options.cursor] if options.cursor and options.cursor in index_by_id else len(messages)
            start = max(0, end - limit)
            page = messages[start:end]
            return FetchResult(
                messages=page,
                next_cursor=(page[0].id if start > 0 and page else None),
            )

        # forward
        start = index_by_id[options.cursor] + 1 if options.cursor and options.cursor in index_by_id else 0
        end = min(len(messages), start + limit)
        page = messages[start:end]
        return FetchResult(
            messages=page,
            next_cursor=(page[-1].id if end < len(messages) and page else None),
        )

    # =========================================================================
    # Send-API helpers
    # =========================================================================

    @staticmethod
    def _truncate_message(text: str) -> str:
        if len(text) <= MESSENGER_MESSAGE_LIMIT:
            return text
        return f"{text[: MESSENGER_MESSAGE_LIMIT - 3]}..."

    async def _graph_api_fetch(
        self,
        endpoint: str,
        *,
        method: str,
        body: dict[str, Any] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> Any:
        """Call the Meta Graph API with the page access token.

        Wraps the call in standardized adapter errors. Mirrors the upstream
        ``graphApiFetch`` helper's surface (URL building, query params, JSON
        parse-then-status check, error-code mapping).
        """
        session = await self._get_http_session()
        url = f"{self._graph_api_url}/{endpoint}"
        params: dict[str, str] = {"access_token": self._page_access_token}
        if query_params:
            params.update(query_params)

        try:
            if method == "GET":
                response_ctx = session.get(url, params=params)
            else:
                response_ctx = session.post(
                    url,
                    params=params,
                    headers={"Content-Type": "application/json"},
                    json=body,
                )

            async with response_ctx as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except Exception as error:
                    raise NetworkError(
                        "messenger",
                        f"Failed to parse Messenger API response for {endpoint}",
                    ) from error

                if status < 200 or status >= 300:
                    self._throw_graph_api_error(endpoint, status, data or {})

                return data
        except NetworkError:
            raise
        except AdapterRateLimitError:
            raise
        except AuthenticationError:
            raise
        except ResourceNotFoundError:
            raise
        except ValidationError:
            raise
        except Exception as error:
            raise NetworkError(
                "messenger",
                f"Network error calling Messenger Graph API {endpoint}",
                original_error=error if isinstance(error, Exception) else None,
            ) from error

    @staticmethod
    def _throw_graph_api_error(
        endpoint: str,
        status: int,
        data: dict[str, Any],
    ) -> None:
        """Translate a non-2xx Graph API response to a typed adapter error."""
        error = data.get("error") if isinstance(data, dict) else None
        if not isinstance(error, dict):
            error = {}
        message = error.get("message") or f"Messenger API {endpoint} failed"
        code = error.get("code") if error.get("code") is not None else status

        # Rate limiting: HTTP 429 or known Meta rate-limit codes.
        if status == 429 or code in (4, 32, 613):
            raise AdapterRateLimitError("messenger")
        # Auth: HTTP 401 or Meta auth code 190.
        if status == 401 or code == 190:
            raise AuthenticationError("messenger", message)
        # Permission: HTTP 403 or Meta permission codes 10 / 200.
        if status == 403 or code in (10, 200):
            raise ValidationError("messenger", message)
        # Not found: HTTP 404.
        if status == 404:
            raise ResourceNotFoundError("messenger", endpoint)
        raise NetworkError(
            "messenger",
            f"{message} (status {status}, code {code})",
        )

    # =========================================================================
    # Request helpers (framework-agnostic)
    # =========================================================================

    @staticmethod
    async def _get_request_body(request: Any) -> bytes:
        """Extract the raw request body as bytes (sync or async).

        Returned as raw bytes so signature verification operates on the exact
        wire bytes Meta signed. Any encode/decode round-trip risks replacement
        characters or altered code points and breaks HMAC parity.
        """
        body = getattr(request, "body", None)
        if body is not None:
            if callable(body):
                result = body()
                body = await result if inspect.isawaitable(result) else result
            if isinstance(body, (bytes, bytearray)):
                return bytes(body)
            if isinstance(body, str):
                return body.encode("utf-8")
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            if isinstance(text_attr, (bytes, bytearray)):
                return bytes(text_attr)
            if isinstance(text_attr, str):
                return text_attr.encode("utf-8")
        return b""

    @staticmethod
    def _get_header(request: Any, name: str) -> str | None:
        """Get a header value from a request object (case-insensitive)."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                for k, v in headers.items():
                    if k.lower() == name.lower():
                        return v
                return None
            return headers.get(name)
        return None

    @staticmethod
    def _make_response(body: str, status: int) -> dict[str, Any]:
        """Create a framework-agnostic response dict."""
        return {"body": body, "status": status}


# =============================================================================
# Factory
# =============================================================================


def create_messenger_adapter(
    *,
    app_secret: str | None = None,
    page_access_token: str | None = None,
    verify_token: str | None = None,
    api_version: str | None = None,
    logger: Logger | None = None,
    user_name: str | None = None,
) -> MessengerAdapter:
    """Factory for ``MessengerAdapter`` with env-var fallbacks.

    Q1 (see #110): init-failure behavior.  We match the upstream contract by
    raising ``ValidationError`` at construction time for each missing required
    credential.  This improves on the sibling ``WhatsAppAdapter``, whose
    constructor does no validation (direct misconstruction raises ``TypeError``,
    not ``ValidationError``) -- Messenger raises a descriptive ``ValidationError``
    from both the factory and the constructor.  It surfaces config errors loudly
    during startup rather than at first webhook call.
    """
    _logger = logger or ConsoleLogger("info").child("messenger")

    _app_secret = app_secret or os.environ.get(ENV_APP_SECRET)
    if not _app_secret:
        raise ValidationError(
            "messenger",
            f"appSecret is required. Set {ENV_APP_SECRET} or provide it in config.",
        )

    _page_access_token = page_access_token or os.environ.get(ENV_PAGE_ACCESS_TOKEN)
    if not _page_access_token:
        raise ValidationError(
            "messenger",
            f"pageAccessToken is required. Set {ENV_PAGE_ACCESS_TOKEN} or provide it in config.",
        )

    _verify_token = verify_token or os.environ.get(ENV_VERIFY_TOKEN)
    if not _verify_token:
        raise ValidationError(
            "messenger",
            f"verifyToken is required. Set {ENV_VERIFY_TOKEN} or provide it in config.",
        )

    return MessengerAdapter(
        MessengerAdapterConfig(
            app_secret=_app_secret,
            page_access_token=_page_access_token,
            verify_token=_verify_token,
            api_version=api_version,
            logger=_logger,
            user_name=user_name,
        )
    )
