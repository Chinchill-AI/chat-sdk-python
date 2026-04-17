"""Discord adapter for chat SDK.

Uses Discord's HTTP Interactions API (not Gateway WebSocket) for
serverless compatibility. Webhook signature verification uses Ed25519.

Python port of packages/adapter-discord/src/index.ts.
"""

from __future__ import annotations

import hmac
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Literal, cast
from urllib.parse import quote

from chat_sdk.adapters.discord.cards import (
    card_to_discord_payload,
    card_to_fallback_text,
)
from chat_sdk.adapters.discord.format_converter import DiscordFormatConverter
from chat_sdk.adapters.discord.types import (
    DiscordActionRow,
    DiscordAdapterConfig,
    DiscordCommandOption,
    DiscordForwardedEvent,
    DiscordGatewayMessageData,
    DiscordGatewayReactionData,
    DiscordInteraction,
    DiscordInteractionData,
    DiscordInteractionResponse,
    DiscordRequestContext,
    DiscordSlashCommandContext,
    DiscordThreadId,
    InteractionResponseType,
)
from chat_sdk.emoji import convert_emoji_placeholders, get_emoji, resolve_emoji_from_gchat
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card, extract_files
from chat_sdk.shared.errors import NetworkError, ValidationError
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
    FileUpload,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    RawMessage,
    ReactionEvent,
    SlashCommandEvent,
    StreamOptions,
    ThreadInfo,
    WebhookOptions,
    _parse_iso,
)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_CONTENT_LENGTH = 2000
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HEX_PATTERN = re.compile(r"^[0-9a-f]+$")

# Discord interaction types (from discord-api-types/v10)
INTERACTION_TYPE_PING = 1
INTERACTION_TYPE_APPLICATION_COMMAND = 2
INTERACTION_TYPE_MESSAGE_COMPONENT = 3

# Discord interaction response type for PONG
INTERACTION_RESPONSE_PONG = 1

# Discord channel types for threads
CHANNEL_TYPE_PUBLIC_THREAD = 11
CHANNEL_TYPE_PRIVATE_THREAD = 12
CHANNEL_TYPE_DM = 1
CHANNEL_TYPE_GROUP_DM = 3

# Thread parent cache TTL
THREAD_PARENT_CACHE_TTL = 5 * 60  # 5 minutes in seconds


class DiscordAdapter:
    """Discord adapter for chat SDK.

    Implements the Adapter interface for Discord HTTP Interactions API.
    """

    def __init__(self, config: DiscordAdapterConfig | None = None) -> None:
        if config is None:
            config = DiscordAdapterConfig()

        bot_token = config.bot_token or os.environ.get("DISCORD_BOT_TOKEN")
        if not bot_token:
            raise ValidationError(
                "discord",
                "bot_token is required. Set DISCORD_BOT_TOKEN or provide it in config.",
            )

        public_key = config.public_key or os.environ.get("DISCORD_PUBLIC_KEY")
        if not public_key:
            raise ValidationError(
                "discord",
                "public_key is required. Set DISCORD_PUBLIC_KEY or provide it in config.",
            )

        application_id = config.application_id or os.environ.get("DISCORD_APPLICATION_ID")
        if not application_id:
            raise ValidationError(
                "discord",
                "application_id is required. Set DISCORD_APPLICATION_ID or provide it in config.",
            )

        self._name = "discord"
        self._bot_token = bot_token
        self._public_key = public_key.strip().lower()
        self._application_id = application_id
        self._mention_role_ids: list[str] = config.mention_role_ids or (
            [rid.strip() for rid in os.environ.get("DISCORD_MENTION_ROLE_IDS", "").split(",") if rid.strip()]
        )
        self._bot_user_id: str | None = application_id  # Discord app ID is the bot's user ID
        self._logger: Logger = config.logger or ConsoleLogger("info", prefix="discord")
        self._user_name = config.user_name or "bot"
        self._chat: ChatInstance | None = None
        self._format_converter = DiscordFormatConverter()
        self._request_context: ContextVar[DiscordRequestContext | None] = ContextVar(
            f"discord_request_context_{id(self)}", default=None
        )
        self._thread_parent_cache: dict[str, dict[str, Any]] = {}

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        # Validate public key format
        if not HEX_64_PATTERN.match(self._public_key):
            self._logger.error(
                "Invalid Discord public key format",
                {
                    "length": len(self._public_key),
                    "isHex": bool(HEX_PATTERN.match(self._public_key)),
                },
            )

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    @property
    def lock_scope(self) -> LockScope | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter."""
        self._chat = chat
        self._logger.info("Discord adapter initialized")

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle incoming Discord webhook (HTTP Interactions or forwarded Gateway events)."""
        body = await self._get_request_body(request)
        body_bytes = body.encode("utf-8") if isinstance(body, str) else body

        # Check if this is a forwarded Gateway event (uses bot token for auth)
        gateway_token = self._get_header(request, "x-discord-gateway-token")
        if gateway_token:
            if not hmac.compare_digest(gateway_token, self._bot_token):
                self._logger.warn("Invalid gateway token")
                return self._make_response("Invalid gateway token", 401)
            self._logger.info("Discord forwarded Gateway event received")
            try:
                event: DiscordForwardedEvent = json.loads(body if isinstance(body, str) else body.decode("utf-8"))
                return await self._handle_forwarded_gateway_event(event, options)
            except (json.JSONDecodeError, ValueError):
                return self._make_response("Invalid JSON", 400)

        body_text = body if isinstance(body, str) else body.decode("utf-8")

        self._logger.info(
            "Discord webhook received",
            {
                "bodyLength": len(body_text),
                "hasSignature": bool(self._get_header(request, "x-signature-ed25519")),
                "hasTimestamp": bool(self._get_header(request, "x-signature-timestamp")),
            },
        )

        # Verify Ed25519 signature
        signature = self._get_header(request, "x-signature-ed25519")
        timestamp = self._get_header(request, "x-signature-timestamp")

        if not await self._verify_signature(
            body_bytes if isinstance(body_bytes, bytes) else body_bytes.encode("utf-8"), signature, timestamp
        ):
            self._logger.warn("Discord signature verification failed, returning 401")
            return self._make_response("Invalid signature", 401)

        self._logger.info("Discord signature verification passed")

        try:
            interaction: DiscordInteraction = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            return self._make_response("Invalid JSON", 400)

        interaction_type = interaction.get("type", 0)

        self._logger.info(
            "Discord interaction parsed",
            {
                "type": interaction_type,
                "id": interaction.get("id"),
            },
        )

        # Handle PING (Discord verification)
        if interaction_type == INTERACTION_TYPE_PING:
            response_body = json.dumps({"type": INTERACTION_RESPONSE_PONG})
            self._logger.info("Discord PING received, responding with PONG")
            return self._make_json_response(response_body, 200)

        # Handle MESSAGE_COMPONENT (button clicks)
        if interaction_type == INTERACTION_TYPE_MESSAGE_COMPONENT:
            self._handle_component_interaction(interaction, options)
            return self._respond_to_interaction(
                {
                    "type": InteractionResponseType.DEFERRED_UPDATE_MESSAGE,
                }
            )

        # Handle APPLICATION_COMMAND (slash commands)
        if interaction_type == INTERACTION_TYPE_APPLICATION_COMMAND:
            self._handle_application_command_interaction(interaction, options)
            return self._respond_to_interaction(
                {
                    "type": InteractionResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
                }
            )

        return self._make_response("Unknown interaction type", 400)

    async def _verify_signature(
        self,
        body_bytes: bytes,
        signature: str | None,
        timestamp: str | None,
    ) -> bool:
        """Verify Discord's Ed25519 signature.

        Uses PyNaCl for Ed25519 verification (lazy import).
        """
        if not (signature and timestamp):
            self._logger.warn("Discord signature verification failed: missing headers")
            return False

        try:
            import nacl.signing  # lazy import

            verify_key = nacl.signing.VerifyKey(bytes.fromhex(self._public_key))
            message = timestamp.encode("utf-8") + body_bytes
            verify_key.verify(message, bytes.fromhex(signature))
            return True
        except ImportError:
            self._logger.error(
                "PyNaCl is required for Discord signature verification. Install with: pip install PyNaCl"
            )
            return False
        except Exception as exc:
            self._logger.warn("Discord signature verification failed", {"error": str(exc)})
            return False

    def _respond_to_interaction(self, response: DiscordInteractionResponse) -> Any:
        """Create a JSON response for Discord interactions."""
        return self._make_json_response(json.dumps(response), 200)

    def _handle_component_interaction(
        self,
        interaction: DiscordInteraction,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle MESSAGE_COMPONENT interactions (button clicks)."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring interaction")
            return

        data = interaction.get("data", {})
        custom_id = data.get("custom_id")
        if not custom_id:
            self._logger.warn("No custom_id in component interaction")
            return

        user = (interaction.get("member") or {}).get("user") or interaction.get("user")
        if not user:
            self._logger.warn("No user in component interaction")
            return

        interaction_channel_id = interaction.get("channel_id")
        guild_id = interaction.get("guild_id") or "@me"
        message = interaction.get("message", {})
        message_id = message.get("id") if message else None

        if not (interaction_channel_id and message_id):
            self._logger.warn("Missing channel_id or message_id in interaction")
            return

        # Detect if the interaction is inside a thread channel
        channel = interaction.get("channel", {})
        channel_type = channel.get("type", 0)
        is_thread = channel_type in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD)
        parent_channel_id = (
            channel.get("parent_id", interaction_channel_id)
            if is_thread and channel.get("parent_id")
            else interaction_channel_id
        )

        thread_id = self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=parent_channel_id,
                thread_id=interaction_channel_id if is_thread else None,
            )
        )

        self._logger.debug(
            "Processing Discord button action",
            {
                "actionId": custom_id,
                "messageId": message_id,
                "threadId": thread_id,
            },
        )

        self._chat.process_action(
            ActionEvent(
                action_id=custom_id,
                value=custom_id,
                user=Author(
                    user_id=user.get("id", ""),
                    user_name=user.get("username", ""),
                    full_name=user.get("global_name") or user.get("username", ""),
                    is_bot=user.get("bot", False),
                    is_me=False,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,
                adapter=self,
                raw=interaction,
            ),
            options,
        )

    def _handle_application_command_interaction(
        self,
        interaction: DiscordInteraction,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle APPLICATION_COMMAND interactions (slash commands)."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring interaction")
            return

        data: DiscordInteractionData = interaction.get("data", cast(DiscordInteractionData, {}))
        command_name = data.get("name")
        if not command_name:
            self._logger.warn("No command name in application command interaction")
            return

        user = (interaction.get("member") or {}).get("user") or interaction.get("user")
        if not user:
            self._logger.warn("No user in application command interaction")
            return

        interaction_channel_id = interaction.get("channel_id")
        if not interaction_channel_id:
            self._logger.warn("Missing channel_id in application command interaction")
            return

        guild_id = interaction.get("guild_id") or "@me"
        channel = interaction.get("channel", {})
        channel_type = channel.get("type", 0)
        is_thread = channel_type in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD)
        parent_channel_id = (
            channel.get("parent_id", interaction_channel_id)
            if is_thread and channel.get("parent_id")
            else interaction_channel_id
        )

        channel_id = self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=parent_channel_id,
                thread_id=interaction_channel_id if is_thread else None,
            )
        )

        command, text = self._parse_slash_command(command_name, data.get("options"))

        self._logger.debug(
            "Processing Discord slash command",
            {
                "command": command,
                "text": text,
                "userId": user.get("id"),
                "channelId": channel_id,
            },
        )

        # Store interaction context for deferred response
        self._request_context.set(
            DiscordRequestContext(
                slash_command=DiscordSlashCommandContext(
                    channel_id=channel_id,
                    interaction_token=interaction.get("token", ""),
                    initial_response_sent=False,
                ),
            )
        )

        event = SlashCommandEvent(
            command=command,
            text=text,
            user=Author(
                user_id=user.get("id", ""),
                user_name=user.get("username", ""),
                full_name=user.get("global_name") or user.get("username", ""),
                is_bot=user.get("bot", False),
                is_me=user.get("id") == self._application_id,
            ),
            adapter=self,
            channel=cast(Any, None),  # chat.py's _handle_slash_command_event creates the ChannelImpl
            raw=interaction,
        )
        event.channel_id = channel_id  # type: ignore[attr-defined]
        self._chat.process_slash_command(event, options)

    def _parse_slash_command(
        self,
        name: str,
        options: list[DiscordCommandOption] | None = None,
    ) -> tuple[str, str]:
        """Parse a Discord slash command into command path and flat text.

        Returns (command, text) tuple.
        """
        command_parts: list[str] = [name if name.startswith("/") else f"/{name}"]
        value_parts: list[str] = []

        def collect(items: list[DiscordCommandOption]) -> None:
            for option in items:
                if option.get("value") is not None:
                    value_parts.append(str(option["value"]))
                    continue
                sub_options = option.get("options", [])
                if sub_options:
                    command_parts.append(option.get("name", ""))
                    collect(sub_options)

        if options:
            collect(options)

        return " ".join(command_parts), " ".join(value_parts).strip()

    async def _handle_forwarded_gateway_event(
        self,
        event: DiscordForwardedEvent,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle a forwarded Gateway event received via webhook."""
        event_type = event.get("type", "")
        self._logger.info(
            "Processing forwarded Gateway event",
            {
                "type": event_type,
                "timestamp": event.get("timestamp"),
            },
        )

        if event_type == "GATEWAY_MESSAGE_CREATE":
            await self._handle_forwarded_message(event.get("data", {}), options)
        elif event_type == "GATEWAY_MESSAGE_REACTION_ADD":
            await self._handle_forwarded_reaction(event.get("data", {}), True, options)
        elif event_type == "GATEWAY_MESSAGE_REACTION_REMOVE":
            await self._handle_forwarded_reaction(event.get("data", {}), False, options)
        else:
            self._logger.debug("Forwarded Gateway event (no handler)", {"type": event_type})

        return self._make_json_response(json.dumps({"ok": True}), 200)

    async def _handle_forwarded_message(
        self,
        data: DiscordGatewayMessageData,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a forwarded MESSAGE_CREATE event."""
        if not self._chat:
            return

        guild_id = data.get("guild_id") or "@me"
        channel_id = data.get("channel_id", "")

        discord_thread_id: str | None = None
        parent_channel_id = channel_id

        thread = data.get("thread")
        if thread:
            discord_thread_id = thread.get("id")
            parent_channel_id = thread.get("parent_id", channel_id)
        elif data.get("channel_type") in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD):
            try:
                response = await self._discord_fetch(f"/channels/{channel_id}", "GET")
                channel_info = response
                if channel_info.get("parent_id"):
                    discord_thread_id = channel_id
                    parent_channel_id = channel_info["parent_id"]
            except Exception as error:
                self._logger.error(
                    "Failed to fetch thread parent",
                    {
                        "error": str(error),
                        "channelId": channel_id,
                    },
                )

        # Check if bot is mentioned
        mentions = data.get("mentions", [])
        is_user_mentioned = data.get("is_mention", False) or any(m.get("id") == self._application_id for m in mentions)
        mention_roles = data.get("mention_roles", [])
        is_role_mentioned = bool(self._mention_role_ids) and any(
            role_id in self._mention_role_ids for role_id in mention_roles
        )
        is_mentioned = is_user_mentioned or is_role_mentioned

        # If mentioned and not in a thread, create one
        if not discord_thread_id and is_mentioned:
            try:
                new_thread = await self._create_discord_thread(channel_id, data.get("id", ""))
                discord_thread_id = new_thread["id"]
            except Exception as error:
                self._logger.error(
                    "Failed to create Discord thread for mention",
                    {
                        "error": str(error),
                        "messageId": data.get("id"),
                    },
                )

        thread_id = self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=parent_channel_id,
                thread_id=discord_thread_id,
            )
        )

        author_data = data.get("author", {})
        content = data.get("content", "")
        attachments_data = data.get("attachments", [])

        chat_message = Message(
            id=data.get("id", ""),
            thread_id=thread_id,
            text=content,
            formatted=self._format_converter.to_ast(content),
            author=Author(
                user_id=author_data.get("id", ""),
                user_name=author_data.get("username", ""),
                full_name=author_data.get("global_name") or author_data.get("username", ""),
                is_bot=author_data.get("bot", False),
                is_me=author_data.get("id") == self._application_id,
            ),
            metadata=MessageMetadata(
                date_sent=_parse_iso(data.get("timestamp", ""))
                if data.get("timestamp")
                else datetime.now(timezone.utc),
                edited=False,
            ),
            attachments=[
                Attachment(
                    type=self._get_attachment_type(a.get("content_type")),
                    url=a.get("url"),
                    name=a.get("filename"),
                    mime_type=a.get("content_type"),
                    size=a.get("size"),
                )
                for a in attachments_data
            ],
            raw=data,
            is_mention=is_mentioned,
        )

        try:
            await cast(Any, self._chat).handle_incoming_message(self, thread_id, chat_message)
        except Exception as error:
            self._logger.error(
                "Error handling forwarded message",
                {
                    "error": str(error),
                    "messageId": data.get("id"),
                },
            )

    async def _handle_forwarded_reaction(
        self,
        data: DiscordGatewayReactionData,
        added: bool,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a forwarded REACTION_ADD or REACTION_REMOVE event."""
        if not self._chat:
            return

        guild_id = data.get("guild_id") or "@me"
        channel_id = data.get("channel_id", "")

        discord_thread_id: str | None = None
        parent_channel_id = channel_id

        channel_type = data.get("channel_type", 0)
        if channel_type in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD):
            cached = self._thread_parent_cache.get(channel_id)
            import time

            if cached and cached.get("expires_at", 0) > time.time():
                discord_thread_id = channel_id
                parent_channel_id = cached["parent_id"]
            else:
                try:
                    channel_info = await self._discord_fetch(f"/channels/{channel_id}", "GET")
                    if channel_info.get("parent_id"):
                        discord_thread_id = channel_id
                        parent_channel_id = channel_info["parent_id"]
                        self._thread_parent_cache[channel_id] = {
                            "parent_id": channel_info["parent_id"],
                            "expires_at": time.time() + THREAD_PARENT_CACHE_TTL,
                        }
                        # Prevent unbounded cache growth
                        if len(self._thread_parent_cache) > 1000:
                            now = time.time()
                            expired = [k for k, v in self._thread_parent_cache.items() if v.get("expires_at", 0) <= now]
                            for k in expired:
                                del self._thread_parent_cache[k]
                            # Hard limit: evict oldest if still over threshold
                            if len(self._thread_parent_cache) > 1000:
                                keys = list(self._thread_parent_cache.keys())
                                for k in keys[: len(keys) - 1000]:
                                    del self._thread_parent_cache[k]
                except Exception as error:
                    self._logger.error(
                        "Failed to fetch thread parent for reaction",
                        {
                            "error": str(error),
                            "channelId": channel_id,
                        },
                    )

        thread_id = self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=parent_channel_id,
                thread_id=discord_thread_id,
            )
        )

        emoji_data = data.get("emoji", {})
        emoji_name = emoji_data.get("name") or "unknown"

        # Get user info from either data.user (DMs) or data.member.user (guilds)
        user_info = data.get("user") or (data.get("member") or {}).get("user")
        if not user_info:
            self._logger.warn("Reaction event missing user info")
            return

        emoji_id = emoji_data.get("id")
        raw_emoji = f"<:{emoji_name}:{emoji_id}>" if emoji_id else emoji_name

        # Normalize emoji through the emoji resolver
        if emoji_name and not emoji_id:
            # Standard unicode emoji -- resolve through gchat (unicode) resolver
            normalized = resolve_emoji_from_gchat(emoji_name)
        else:
            # Custom emoji -- use custom:{id} key or raw name
            normalized = get_emoji(f"custom:{emoji_id}" if emoji_id else emoji_name)

        self._chat.process_reaction(
            ReactionEvent(
                adapter=self,
                thread=cast(Any, None),
                thread_id=thread_id,
                message_id=data.get("message_id", ""),
                emoji=normalized,
                raw_emoji=raw_emoji,
                added=added,
                user=Author(
                    user_id=user_info.get("id", ""),
                    user_name=user_info.get("username", ""),
                    full_name=user_info.get("username", ""),
                    is_bot=user_info.get("bot", False),
                    is_me=user_info.get("id") == self._application_id,
                ),
                raw=data,
            )
        )

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Discord channel or thread."""
        decoded = self.decode_thread_id(thread_id)
        channel_id = decoded.thread_id if decoded.thread_id else decoded.channel_id

        # Build message payload
        payload: dict[str, Any] = {}
        embeds: list[dict[str, Any]] = []
        components: list[DiscordActionRow] = []

        card = extract_card(message)
        if card:
            card_payload = card_to_discord_payload(card)
            embeds.extend(card_payload["embeds"])
            components.extend(card_payload["components"])
            payload["content"] = self._truncate_content(card_to_fallback_text(card))
        else:
            payload["content"] = self._truncate_content(
                convert_emoji_placeholders(
                    self._format_converter.render_postable(message),
                    "discord",
                )
            )

        if embeds:
            payload["embeds"] = embeds
        if components:
            payload["components"] = components

        # --- Handle file attachments via multipart/form-data ---
        files = extract_files(message)

        # --- Resolve deferred slash-command interaction if pending ---
        req_ctx = self._request_context.get()
        slash_ctx = req_ctx.slash_command if req_ctx else None
        if slash_ctx and not slash_ctx.initial_response_sent:
            slash_ctx.initial_response_sent = True
            self._logger.debug(
                "Discord API: PATCH deferred interaction response",
                {
                    "channelId": channel_id,
                    "contentLength": len(payload.get("content", "")),
                    "embedCount": len(embeds),
                    "componentCount": len(components),
                    "fileCount": len(files),
                },
            )

            result = await self._discord_fetch(
                f"/webhooks/{self._application_id}/{slash_ctx.interaction_token}/messages/@original",
                "PATCH",
                payload,
                files=files or None,
            )

            self._logger.debug(
                "Discord API: PATCH deferred interaction response completed",
                {"messageId": result.get("id") if result else None},
            )

            return RawMessage(
                id=(result or {}).get("id", ""),
                thread_id=thread_id,
                raw=result or {},
            )

        self._logger.debug(
            "Discord API: POST message",
            {
                "channelId": channel_id,
                "contentLength": len(payload.get("content", "")),
                "embedCount": len(embeds),
                "componentCount": len(components),
                "fileCount": len(files),
            },
        )

        result = await self._discord_fetch(
            f"/channels/{channel_id}/messages",
            "POST",
            payload,
            files=files or None,
        )

        self._logger.debug(
            "Discord API: POST message response",
            {
                "messageId": result.get("id"),
            },
        )

        return RawMessage(
            id=result.get("id", ""),
            thread_id=thread_id,
            raw=result,
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Discord message."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id

        payload: dict[str, Any] = {}
        embeds: list[dict[str, Any]] = []
        components: list[DiscordActionRow] = []

        card = extract_card(message)
        if card:
            card_payload = card_to_discord_payload(card)
            embeds.extend(card_payload["embeds"])
            components.extend(card_payload["components"])
            payload["content"] = self._truncate_content(card_to_fallback_text(card))
        else:
            payload["content"] = self._truncate_content(
                convert_emoji_placeholders(
                    self._format_converter.render_postable(message),
                    "discord",
                )
            )

        if embeds:
            payload["embeds"] = embeds
        if components:
            payload["components"] = components

        self._logger.debug(
            "Discord API: PATCH message",
            {
                "channelId": target_channel_id,
                "messageId": message_id,
                "contentLength": len(payload.get("content", "")),
            },
        )

        result = await self._discord_fetch(
            f"/channels/{target_channel_id}/messages/{message_id}",
            "PATCH",
            payload,
        )

        self._logger.debug(
            "Discord API: PATCH message response",
            {
                "messageId": result.get("id"),
            },
        )

        return RawMessage(
            id=result.get("id", ""),
            thread_id=thread_id,
            raw=result,
        )

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a Discord message."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id

        self._logger.debug(
            "Discord API: DELETE message",
            {
                "channelId": target_channel_id,
                "messageId": message_id,
            },
        )

        await self._discord_fetch(
            f"/channels/{target_channel_id}/messages/{message_id}",
            "DELETE",
        )

        self._logger.debug("Discord API: DELETE message response", {"ok": True})

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a Discord message."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id
        emoji_encoded = self._encode_emoji(emoji)

        self._logger.debug(
            "Discord API: PUT reaction",
            {
                "channelId": target_channel_id,
                "messageId": message_id,
                "emoji": emoji_encoded,
            },
        )

        await self._discord_fetch(
            f"/channels/{target_channel_id}/messages/{message_id}/reactions/{emoji_encoded}/@me",
            "PUT",
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a Discord message."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id
        emoji_encoded = self._encode_emoji(emoji)

        self._logger.debug(
            "Discord API: DELETE reaction",
            {
                "channelId": target_channel_id,
                "messageId": message_id,
                "emoji": emoji_encoded,
            },
        )

        await self._discord_fetch(
            f"/channels/{target_channel_id}/messages/{message_id}/reactions/{emoji_encoded}/@me",
            "DELETE",
        )

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:  # noqa: ARG002
        """Start typing indicator in a Discord channel or thread."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id

        self._logger.debug(
            "Discord API: POST typing",
            {
                "channelId": target_channel_id,
            },
        )

        await self._discord_fetch(f"/channels/{target_channel_id}/typing", "POST")

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from a Discord channel or thread."""
        if options is None:
            options = FetchOptions()

        decoded = self.decode_thread_id(thread_id)
        target_channel_id = decoded.thread_id or decoded.channel_id

        limit = options.limit if options.limit is not None else 50
        direction = options.direction or "backward"

        params: list[str] = [f"limit={limit}"]
        if options.cursor:
            if direction == "backward":
                params.append(f"before={options.cursor}")
            else:
                params.append(f"after={options.cursor}")

        self._logger.debug(
            "Discord API: GET messages",
            {
                "channelId": target_channel_id,
                "limit": limit,
                "direction": direction,
                "cursor": options.cursor,
            },
        )

        raw_messages = await self._discord_fetch(
            f"/channels/{target_channel_id}/messages?{'&'.join(params)}",
            "GET",
        )

        self._logger.debug(
            "Discord API: GET messages response",
            {
                "messageCount": len(raw_messages) if isinstance(raw_messages, list) else 0,
            },
        )

        if not isinstance(raw_messages, list):
            raw_messages = []

        # Discord returns messages in reverse chronological order
        sorted_messages = list(reversed(raw_messages))

        messages = [self._parse_discord_message(msg, thread_id) for msg in sorted_messages]

        # Determine next cursor
        next_cursor: str | None = None
        if len(raw_messages) == limit:
            if direction == "backward":
                oldest = raw_messages[-1] if raw_messages else None
                next_cursor = oldest.get("id") if oldest else None
            else:
                newest = raw_messages[0] if raw_messages else None
                next_cursor = newest.get("id") if newest else None

        return FetchResult(messages=messages, next_cursor=next_cursor)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread/channel information."""
        decoded = self.decode_thread_id(thread_id)

        self._logger.debug("Discord API: GET channel", {"channelId": decoded.channel_id})

        channel = await self._discord_fetch(f"/channels/{decoded.channel_id}", "GET")

        channel_type = channel.get("type", 0)

        return ThreadInfo(
            id=thread_id,
            channel_id=decoded.channel_id,
            channel_name=channel.get("name"),
            is_dm=channel_type in (CHANNEL_TYPE_DM, CHANNEL_TYPE_GROUP_DM),
            metadata={
                "guild_id": decoded.guild_id,
                "channel_type": channel_type,
                "raw": channel,
            },
        )

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a user."""
        self._logger.debug("Discord API: POST DM channel", {"userId": user_id})

        dm_channel = await self._discord_fetch(
            "/users/@me/channels",
            "POST",
            {
                "recipient_id": user_id,
            },
        )

        self._logger.debug(
            "Discord API: POST DM channel response",
            {
                "channelId": dm_channel.get("id"),
            },
        )

        return self.encode_thread_id(
            DiscordThreadId(
                guild_id="@me",
                channel_id=dm_channel.get("id", ""),
            )
        )

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a DM."""
        decoded = self.decode_thread_id(thread_id)
        return decoded.guild_id == "@me"

    def encode_thread_id(self, platform_data: DiscordThreadId) -> str:
        """Encode platform data into a thread ID string.

        Format: discord:{guild_id}:{channel_id}[:{thread_id}]
        """
        thread_part = f":{platform_data.thread_id}" if platform_data.thread_id else ""
        return f"discord:{platform_data.guild_id}:{platform_data.channel_id}{thread_part}"

    def decode_thread_id(self, thread_id: str) -> DiscordThreadId:
        """Decode thread ID string back to platform data."""
        parts = thread_id.split(":")
        if len(parts) < 3 or parts[0] != "discord":
            raise ValidationError("discord", f"Invalid Discord thread ID: {thread_id}")

        return DiscordThreadId(
            guild_id=parts[1],
            channel_id=parts[2],
            thread_id=parts[3] if len(parts) > 3 else None,
        )

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Extract the channel ID from a thread ID.

        Discord thread IDs are encoded as ``discord:{guildId}:{channelId}``
        or ``discord:{guildId}:{channelId}:{threadId}``.  The channel ID is
        always ``discord:{guildId}:{channelId}``.
        """
        decoded = self.decode_thread_id(thread_id)
        return self.encode_thread_id(
            DiscordThreadId(
                guild_id=decoded.guild_id,
                channel_id=decoded.channel_id,
            )
        )

    def parse_message(self, raw: Any) -> Message:
        """Parse a Discord message into normalized format."""
        guild_id = raw.get("guild_id") or "@me"
        thread_id = self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=raw.get("channel_id", ""),
            )
        )
        return self._parse_discord_message(raw, thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to Discord markdown."""
        return self._format_converter.from_ast(content)

    async def stream(
        self,
        thread_id: str,
        text_stream: Any,
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream responses by accumulating chunks and posting/editing a single message.

        Discord does not support native streaming, so this accumulates the
        text and periodically edits the message in-place.
        """
        accumulated = ""
        message_id: str | None = None

        async for chunk in text_stream:
            text = ""
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                text = chunk.get("text", "")
            if not text:
                continue

            accumulated += text

            postable = cast(AdapterPostableMessage, {"raw": accumulated})

            if message_id:
                await self.edit_message(thread_id, message_id, postable)
            else:
                result = await self.post_message(thread_id, postable)
                message_id = result.id

        return RawMessage(
            id=message_id or "",
            thread_id=thread_id,
            raw={"text": accumulated},
        )

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel information from Discord."""
        decoded = self.decode_thread_id(channel_id)

        channel = await self._discord_fetch(f"/channels/{decoded.channel_id}", "GET")

        channel_type = channel.get("type", 0)
        is_dm = channel_type in (CHANNEL_TYPE_DM, CHANNEL_TYPE_GROUP_DM)

        return ChannelInfo(
            id=channel_id,
            name=channel.get("name"),
            is_dm=is_dm,
            member_count=channel.get("member_count"),
            metadata={
                "guild_id": decoded.guild_id,
                "channel_type": channel_type,
                "raw": channel,
            },
        )

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
        self._logger.debug("Discord adapter disconnecting")

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _parse_discord_message(self, raw: dict[str, Any], thread_id: str) -> Message:
        """Parse a Discord API message into normalized format."""
        # Use original message instead of empty thread starter message if available
        msg = raw
        if raw.get("type") == 21 and raw.get("referenced_message"):  # ThreadStarterMessage
            msg = raw["referenced_message"]

        author = msg.get("author", {})
        is_bot = author.get("bot", False)
        is_me = author.get("id") == self._bot_user_id

        attachments_data = msg.get("attachments", [])

        return Message(
            id=msg.get("id", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(msg.get("content", "")),
            formatted=self._format_converter.to_ast(msg.get("content", "")),
            raw=raw,
            author=Author(
                user_id=author.get("id", ""),
                user_name=author.get("username", ""),
                full_name=author.get("global_name") or author.get("username", ""),
                is_bot=is_bot,
                is_me=is_me,
            ),
            metadata=MessageMetadata(
                date_sent=_parse_iso(msg["timestamp"]) if msg.get("timestamp") else datetime.now(timezone.utc),
                edited=msg.get("edited_timestamp") is not None,
                edited_at=_parse_iso(msg["edited_timestamp"]) if msg.get("edited_timestamp") else None,
            ),
            attachments=[
                Attachment(
                    type=self._get_attachment_type(att.get("content_type")),
                    url=att.get("url"),
                    name=att.get("filename"),
                    mime_type=att.get("content_type"),
                    size=att.get("size"),
                )
                for att in attachments_data
            ],
        )

    def _get_attachment_type(self, mime_type: str | None) -> Literal["image", "file", "video", "audio"]:
        """Determine attachment type from MIME type."""
        if not mime_type:
            return "file"
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
        return "file"

    def _truncate_content(self, content: str) -> str:
        """Truncate content to Discord's maximum length."""
        if len(content) <= DISCORD_MAX_CONTENT_LENGTH:
            return content
        return f"{content[: DISCORD_MAX_CONTENT_LENGTH - 3]}..."

    def _encode_emoji(self, emoji: EmojiValue | str) -> str:
        """Encode an emoji for use in Discord API URLs."""
        emoji_str = emoji if isinstance(emoji, str) else emoji.name
        return quote(emoji_str)

    async def _create_discord_thread(
        self,
        channel_id: str,
        message_id: str,
    ) -> dict[str, str]:
        """Create a Discord thread from a message."""
        thread_name = f"Thread {datetime.now(timezone.utc).isoformat()}"

        self._logger.debug(
            "Discord API: POST thread",
            {
                "channelId": channel_id,
                "messageId": message_id,
                "threadName": thread_name,
            },
        )

        try:
            result = await self._discord_fetch(
                f"/channels/{channel_id}/messages/{message_id}/threads",
                "POST",
                {
                    "name": thread_name,
                    "auto_archive_duration": 1440,  # 24 hours
                },
            )

            self._logger.debug(
                "Discord API: POST thread response",
                {
                    "threadId": result.get("id"),
                },
            )

            return {"id": result.get("id", ""), "name": result.get("name", thread_name)}
        except NetworkError as error:
            # Discord error 160004: "A thread has already been created for this message"
            if "160004" in str(error):
                self._logger.debug(
                    "Thread already exists for message, reusing existing thread",
                    {"channelId": channel_id, "messageId": message_id},
                )
                return {"id": message_id, "name": thread_name}
            raise

    async def _discord_fetch(
        self,
        path: str,
        method: str,
        body: Any = None,
        files: list[FileUpload] | None = None,
    ) -> Any:
        """Make a request to the Discord API using aiohttp (lazy import).

        When *files* is provided the request uses ``multipart/form-data``
        with a ``payload_json`` field for the JSON body and one field per
        file attachment, matching the Discord API multipart upload spec.
        """
        import aiohttp  # lazy import (needed for FormData)

        url = f"{DISCORD_API_BASE}{path}"
        headers: dict[str, str] = {
            "Authorization": f"Bot {self._bot_token}",
        }

        # Build request kwargs depending on whether we have file uploads
        request_kwargs: dict[str, Any] = {}
        if files:
            # Multipart form-data with payload_json + file parts
            form = aiohttp.FormData()
            form.add_field("payload_json", json.dumps(body or {}), content_type="application/json")
            for idx, file in enumerate(files):
                form.add_field(
                    f"files[{idx}]",
                    file.data,
                    filename=file.filename,
                    content_type=file.mime_type or "application/octet-stream",
                )
            request_kwargs["data"] = form
            # Do NOT set Content-Type header -- aiohttp sets the multipart boundary
        else:
            if body is not None:
                headers["Content-Type"] = "application/json"
                request_kwargs["json"] = body

        session = await self._get_http_session()
        async with session.request(
            method,
            url,
            headers=headers,
            **request_kwargs,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                self._logger.error(
                    "Discord API error",
                    {
                        "path": path,
                        "method": method,
                        "status": response.status,
                        "error": error_text,
                    },
                )
                raise NetworkError(
                    "discord",
                    f"Discord API error: {response.status} {error_text}",
                )

            if response.status == 204:
                return None

            return await response.json()

    # =========================================================================
    # Request/Response helpers (framework-agnostic)
    # =========================================================================

    async def _get_request_body(self, request: Any) -> str:
        """Extract the request body as a string."""
        if hasattr(request, "body"):
            body = request.body
            if callable(body):
                body = body()
            if hasattr(body, "read"):
                raw = await body.read() if hasattr(body.read, "__await__") else body.read()
                return raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return body.decode("utf-8") if isinstance(body, bytes) else str(body)
        if hasattr(request, "text"):
            text_attr = request.text
            if callable(text_attr):
                return await cast(Any, text_attr)()
            return text_attr
        if hasattr(request, "data"):
            data = request.data
            return data.decode("utf-8") if isinstance(data, bytes) else str(data)
        return ""

    def _get_header(self, request: Any, name: str) -> str | None:
        """Extract a header value from the request."""
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict):
                return headers.get(name) or headers.get(name.title())
            if hasattr(headers, "get"):
                return headers.get(name)
        return None

    def _make_response(self, body: str, status: int) -> Any:
        """Create a simple text response."""
        return {"body": body, "status": status, "headers": {"Content-Type": "text/plain"}}

    def _make_json_response(self, body: str, status: int) -> Any:
        """Create a JSON response."""
        return {"body": body, "status": status, "headers": {"Content-Type": "application/json"}}


def create_discord_adapter(config: DiscordAdapterConfig | None = None) -> DiscordAdapter:
    """Factory function to create a Discord adapter."""
    return DiscordAdapter(config)
