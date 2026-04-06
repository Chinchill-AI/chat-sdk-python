"""WhatsApp adapter for chat SDK.

Supports messaging via the WhatsApp Business Cloud API (Meta Graph API).
All conversations are 1:1 DMs between the business phone number and users.

Python port of packages/adapter-whatsapp/src/index.ts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from collections.abc import AsyncIterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from chat_sdk.adapters.whatsapp.cards import card_to_whatsapp, decode_whatsapp_callback_data
from chat_sdk.adapters.whatsapp.format_converter import WhatsAppFormatConverter
from chat_sdk.adapters.whatsapp.types import (
    WhatsAppAdapterConfig,
    WhatsAppContact,
    WhatsAppInboundMessage,
    WhatsAppInteractiveMessage,
    WhatsAppRawMessage,
    WhatsAppThreadId,
    WhatsAppWebhookPayload,
)
from chat_sdk.emoji import convert_emoji_placeholders, emoji_to_unicode, get_emoji
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import ValidationError
from chat_sdk.types import (
    ActionEvent,
    AdapterPostableMessage,
    Attachment,
    Author,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    Message,
    MessageMetadata,
    RawMessage,
    ReactionEvent,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    WebhookOptions,
)

# Default Graph API version
DEFAULT_API_VERSION = "v21.0"

# Maximum message length for WhatsApp Cloud API
WHATSAPP_MESSAGE_LIMIT = 4096


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
        self._lock_scope = "channel"
        self._persist_message_history = True
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

    @property
    def name(self) -> str:
        return self._name

    @property
    def lock_scope(self) -> str:
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

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch business profile info."""
        self._chat = chat
        self._bot_user_id = self._phone_number_id
        self._logger.info("WhatsApp adapter initialized", {"phoneNumberId": self._phone_number_id})

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
        self._logger.debug("WhatsApp webhook raw body", {"body": body[:500]})

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
                    "contentType": self._get_header(request, "content-type"),
                    "bodyPreview": body[:200],
                },
            )
            return self._make_response("Invalid JSON", 400)

        # Process entries
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue

                value = change.get("value", {})

                # Process incoming messages
                if value.get("messages"):
                    for message in value["messages"]:
                        try:
                            self._handle_inbound_message(
                                message,
                                (value.get("contacts") or [None])[0],
                                value.get("metadata", {}).get("phone_number_id", ""),
                                options,
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
    ) -> None:
        """Handle an inbound message from a user."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring message")
            return

        # Handle reactions separately
        if inbound.get("type") == "reaction" and inbound.get("reaction"):
            self._handle_reaction(inbound, contact, phone_number_id, options)
            return

        # Handle interactive message replies (button clicks)
        if inbound.get("type") == "interactive" and inbound.get("interactive"):
            self._handle_interactive_reply(inbound, contact, phone_number_id, options)
            return

        # Handle legacy button responses (from template quick replies)
        if inbound.get("type") == "button" and inbound.get("button"):
            self._handle_button_response(inbound, contact, phone_number_id, options)
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
                user_wa_id=inbound["from"],
            )
        )

        message = self._build_message(inbound, contact, thread_id, text, phone_number_id)
        self._chat.process_message(self, thread_id, message, options)

    def _handle_reaction(
        self,
        inbound: WhatsAppInboundMessage,
        contact: WhatsAppContact | None,
        phone_number_id: str,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle reaction events."""
        if not (self._chat and inbound.get("reaction")):
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=inbound["from"],
            )
        )

        raw_emoji = inbound["reaction"].get("emoji", "")
        added = raw_emoji != ""
        emoji_value = get_emoji(raw_emoji) if added else get_emoji("")

        contact_name = (contact or {}).get("profile", {}).get("name", "") or inbound["from"]
        user = Author(
            user_id=inbound["from"],
            user_name=contact_name,
            full_name=contact_name,
            is_bot=False,
            is_me=False,
        )

        self._chat.process_reaction(
            ReactionEvent(
                adapter=self,
                thread=None,
                thread_id=thread_id,
                message_id=inbound["reaction"]["message_id"],
                user=user,
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
    ) -> None:
        """Handle interactive message replies (button/list selection)."""
        if not (self._chat and inbound.get("interactive")):
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=inbound["from"],
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
        action_id = decoded["action_id"]
        value = decoded.get("value") if decoded.get("value") is not None else fallback_value

        contact_name = (contact or {}).get("profile", {}).get("name", "") or inbound["from"]
        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,
                thread_id=thread_id,
                message_id=inbound["id"],
                user=Author(
                    user_id=inbound["from"],
                    user_name=contact_name,
                    full_name=contact_name,
                    is_bot=False,
                    is_me=False,
                ),
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
    ) -> None:
        """Handle legacy button responses (from template quick replies)."""
        if not (self._chat and inbound.get("button")):
            return

        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=phone_number_id,
                user_wa_id=inbound["from"],
            )
        )

        contact_name = (contact or {}).get("profile", {}).get("name", "") or inbound["from"]
        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,
                thread_id=thread_id,
                message_id=inbound["id"],
                user=Author(
                    user_id=inbound["from"],
                    user_name=contact_name,
                    full_name=contact_name,
                    is_bot=False,
                    is_me=False,
                ),
                action_id=inbound["button"]["payload"],
                value=inbound["button"]["text"],
                raw=inbound,
            ),
            options,
        )

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
    ) -> Message:
        """Build a Message from a WhatsApp inbound message."""
        contact_name = (contact or {}).get("profile", {}).get("name", "") or inbound["from"]
        author = Author(
            user_id=inbound["from"],
            user_name=contact_name,
            full_name=contact_name,
            is_bot=False,
            is_me=False,
        )

        formatted = self._format_converter.to_ast(text)

        raw: WhatsAppRawMessage = {
            "message": inbound,
            "contact": contact,
            "phone_number_id": phone_number_id or self._phone_number_id,
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
        )

    async def download_media(self, media_id: str) -> bytes:
        """Download media from WhatsApp.

        Two-step process:
        1. GET the media metadata to obtain the download URL
        2. GET the actual binary data from the download URL
        """
        import aiohttp

        async with aiohttp.ClientSession() as session:
            # Step 1: Get the media URL
            async with session.get(
                f"{self._graph_api_url}/{media_id}",
                headers={"Authorization": f"Bearer {self._access_token}"},
            ) as meta_response:
                if meta_response.status != 200:
                    error_body = await meta_response.text()
                    self._logger.error(
                        "Failed to get media URL",
                        {
                            "status": meta_response.status,
                            "body": error_body,
                            "mediaId": media_id,
                        },
                    )
                    raise RuntimeError(f"Failed to get media URL: {meta_response.status} {error_body}")

                media_info = await meta_response.json()

            # Step 2: Download the actual file
            async with session.get(
                media_info["url"],
                headers={"Authorization": f"Bearer {self._access_token}"},
            ) as data_response:
                if data_response.status != 200:
                    self._logger.error(
                        "Failed to download media",
                        {
                            "status": data_response.status,
                            "mediaId": media_id,
                        },
                    )
                    raise RuntimeError(f"Failed to download media: {data_response.status}")

                return await data_response.read()

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Send a message to a WhatsApp user."""
        decoded = self.decode_thread_id(thread_id)
        user_wa_id = decoded.user_wa_id

        # Check if this is a card with interactive buttons
        card = extract_card(message)
        if card:
            result = card_to_whatsapp(card)
            if result.get("type") == "interactive":
                interactive = json.loads(convert_emoji_placeholders(json.dumps(result["interactive"]), "whatsapp"))
                return await self._send_interactive_message(thread_id, user_wa_id, interactive)
            return await self._send_text_message(
                thread_id,
                user_wa_id,
                convert_emoji_placeholders(result["text"], "whatsapp"),
            )

        # Regular text message
        body = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "whatsapp",
        )
        return await self._send_text_message(thread_id, user_wa_id, body)

    async def _send_single_text_message(
        self,
        thread_id: str,
        to: str,
        text: str,
    ) -> RawMessage:
        """Send a single text message via the Cloud API."""
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
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
    ) -> RawMessage:
        """Send a text message, splitting into multiple if it exceeds the limit."""
        chunks = split_message(text)
        result: RawMessage | None = None

        for chunk in chunks:
            result = await self._send_single_text_message(thread_id, to, chunk)

        assert result is not None
        return result

    async def _send_interactive_message(
        self,
        thread_id: str,
        to: str,
        interactive: WhatsAppInteractiveMessage,
    ) -> RawMessage:
        """Send an interactive message (buttons or list) via the Cloud API."""
        response = await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
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

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit a message. Not supported by WhatsApp Cloud API."""
        raise RuntimeError("WhatsApp does not support editing messages. Use post_message to send a new message instead.")

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream a message by buffering all chunks and sending as a single message."""
        accumulated = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                accumulated += chunk
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":
                accumulated += chunk.text
        return await self.post_message(thread_id, {"markdown": accumulated})

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
                "to": decoded.user_wa_id,
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
                "to": decoded.user_wa_id,
                "type": "reaction",
                "reaction": {"message_id": message_id, "emoji": ""},
            },
        )

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Start typing indicator. Not supported by WhatsApp Cloud API."""
        pass

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
        """Open a DM with a user. Returns the thread ID."""
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
        thread_id = self.encode_thread_id(
            WhatsAppThreadId(
                phone_number_id=raw["phone_number_id"],
                user_wa_id=raw["message"]["from"],
            )
        )

        contact = raw.get("contact")
        contact_name = ""
        contact_name = contact.get("profile", {}).get("name", "") or raw["message"]["from"] if contact else raw["message"]["from"]

        return Message(
            id=raw["message"]["id"],
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            author=Author(
                user_id=raw["message"]["from"],
                user_name=contact_name,
                full_name=contact_name,
                is_bot=False,
                is_me=raw["message"]["from"] == self._bot_user_id,
            ),
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

    async def mark_as_read(self, message_id: str) -> None:
        """Mark an inbound message as read."""
        await self._graph_api_request(
            f"/{self._phone_number_id}/messages",
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            },
        )

    # =========================================================================
    # Private helpers
    # =========================================================================

    async def _graph_api_request(self, path: str, body: Any) -> Any:
        """Make a request to the Meta Graph API."""
        import aiohttp

        async with (
            aiohttp.ClientSession() as session,
            session.post(
                f"{self._graph_api_url}{path}",
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as response,
        ):
            if response.status != 200:
                error_body = await response.text()
                self._logger.error(
                    "WhatsApp API error",
                    {
                        "status": response.status,
                        "body": error_body,
                        "path": path,
                    },
                )
                raise RuntimeError(f"WhatsApp API error: {response.status} {error_body}")

            return await response.json()

    def _resolve_emoji(self, emoji: EmojiValue | str) -> str:
        """Resolve an emoji value to a unicode string."""
        return emoji_to_unicode(emoji)

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract body text from a request object."""
        if hasattr(request, "text") and callable(request.text):
            return await request.text()
        if hasattr(request, "body"):
            body = request.body
            if isinstance(body, bytes):
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
