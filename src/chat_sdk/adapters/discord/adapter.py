"""Discord adapter for chat SDK.

Uses Discord's HTTP Interactions API (not Gateway WebSocket) for
serverless compatibility. Webhook signature verification uses Ed25519.

Python port of packages/adapter-discord/src/index.ts.
"""

from __future__ import annotations

import hmac
import inspect
import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal, TypeVar, cast
from urllib.parse import quote

from chat_sdk.adapters.discord.cards import (
    card_to_discord_payload,
    decode_discord_custom_id,
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
    DiscordInteractionFlagsContext,
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
    PostableRaw,
    RawMessage,
    ReactionEvent,
    SlashCommandEvent,
    StreamOptions,
    ThreadInfo,
    UserInfo,
    WebhookOptions,
    _parse_iso,
)

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_MAX_CONTENT_LENGTH = 2000
DISCORD_UNKNOWN_MESSAGE = 10_008
DISCORD_THREAD_ALREADY_CREATED = 160_004
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
THREAD_PARENT_CACHE_MAX = 1000

_T = TypeVar("_T")


class DiscordApiError(Exception):
    """A non-2xx Discord REST response (upstream ``DiscordApiError``).

    Carried as :attr:`NetworkError.original_error` by ``_discord_fetch`` so
    callers can branch on Discord's JSON error ``code`` (e.g. ``10008``
    Unknown Message, ``160004`` thread already created) instead of
    string-matching the body.
    """

    def __init__(self, status: int, body: str) -> None:
        super().__init__(body)
        self.status = status
        self.body = body
        self.code: int | None = _parse_discord_error_code(body)


def _parse_discord_error_code(body: str) -> int | None:
    """Discord's numeric ``code`` from a JSON error body, else ``None``."""
    try:
        data = json.loads(body)
    # ``RecursionError``: a deeply nested body must not escape the error
    # wrapper (upstream's bare ``catch`` around ``JSON.parse`` swallows it).
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    # ``bool`` is an ``int`` subclass; a JSON ``true`` is not an error code.
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return None


def _flatten(
    text: str | None,
    files: Iterable[dict[str, Any]] | None,
    snapshots: Iterable[dict[str, Any]] | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Merge a message's own content/attachments with its forwarded snapshots.

    Port of upstream ``flatten`` (#825): text is ``[content, *snapshot
    contents]`` minus empties, joined by a blank line; attachments are the
    message's own followed by each snapshot's.
    """
    items = [item for item in (snapshots or []) if isinstance(item, dict)]
    attachments: list[dict[str, Any]] = list(files or [])
    for item in items:
        attachments.extend(item.get("attachments") or [])
    parts = [text, *(item.get("content") for item in items)]
    return "\n\n".join(part for part in parts if part), attachments


def _snapshot_messages(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """``raw.message_snapshots?.map(({ message }) => message) ?? []``."""
    snapshots = raw.get("message_snapshots") or []
    return [snap["message"] for snap in snapshots if isinstance(snap, dict) and isinstance(snap.get("message"), dict)]


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

        # Custom Discord API base URL (proxy / mock / self-host). Port of
        # upstream's coalescing chain ``config.apiUrl ?? process.env.DISCORD_API_URL
        # ?? DISCORD_API_BASE`` (index.ts:142). Unlike Slack/GitHub/Linear --
        # which feed clients via a truthy spread -- ``_discord_fetch`` joins
        # ``f"{base}{path}"`` directly, so an empty ``apiUrl`` would yield a
        # broken relative URL. We use a truthy fallback so an empty string (or
        # env) resolves to the ``DISCORD_API_BASE`` default, matching the
        # empty-string-is-default contract shared by the other adapters.
        self._api_base_url: str = config.api_url or os.environ.get("DISCORD_API_URL") or DISCORD_API_BASE
        self._name = "discord"
        self._bot_token = bot_token
        self._public_key = public_key.strip().lower()
        self._application_id = application_id
        self._mention_role_ids: list[str] = config.mention_role_ids or (
            [rid.strip() for rid in os.environ.get("DISCORD_MENTION_ROLE_IDS", "").split(",") if rid.strip()]
        )
        # Upstream ``config.respondToChannelIds ?? env ?? []``: an explicit
        # ``[]`` wins over the env var (unlike the ``mention_role_ids`` parse
        # above). Blank env entries are dropped; upstream keeps them, but a
        # blank id never names a real channel.
        if config.respond_to_channel_ids is not None:
            self._respond_to_channel_ids: list[str] = list(config.respond_to_channel_ids)
        else:
            env_channel_ids = os.environ.get("DISCORD_RESPOND_TO_CHANNEL_IDS")
            self._respond_to_channel_ids = (
                [cid.strip() for cid in env_channel_ids.split(",") if cid.strip()] if env_channel_ids else []
            )
        self._respond_to_global_mentions: bool = (
            config.respond_to_global_mentions if config.respond_to_global_mentions is not None else False
        )
        self._interaction_flags = config.interaction_flags
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

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up a Discord user via ``GET /users/{user_id}``.

        Returns ``None`` on any failure (network error, 4xx/5xx, missing
        bot scope). Discord user IDs are 17-19 digit snowflakes — we
        validate the shape here both as a lightweight typo guard and to
        prevent path-segment injection (``/`` would escape the URL).

        Mirrors upstream ``DiscordAdapter.getUser`` (vercel/chat#391).
        """
        # Hazard #12: never let user input reach a URL path unvalidated.
        # Snowflakes are pure digits — anything else is rejected before
        # the network call so a crafted "../foo" can't pivot the request.
        if not user_id or not user_id.isdigit():
            return None
        try:
            user = await self._discord_fetch(f"/users/{quote(user_id, safe='')}", "GET")
        except Exception:
            return None
        if not isinstance(user, dict):
            return None
        avatar = user.get("avatar")
        avatar_url = f"https://cdn.discordapp.com/avatars/{user.get('id')}/{avatar}.png" if avatar else None
        username = user.get("username") or user_id
        return UserInfo(
            user_id=str(user.get("id") or user_id),
            user_name=username,
            full_name=user.get("global_name") or username,
            is_bot=bool(user.get("bot", False)),
            avatar_url=avatar_url,
            email=None,
        )

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
            context = self._build_application_command_context(interaction)
            flags = self._get_interaction_flags(context)
            self._handle_application_command_interaction(context, flags, options)
            deferred: DiscordInteractionResponse = {
                "type": InteractionResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
            }
            # ``is not None``: ``0`` is a real flag value and is still sent.
            if flags is not None:
                deferred["data"] = {"flags": flags}
            return self._respond_to_interaction(deferred)

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
        message = interaction.get("message", {})
        message_id = message.get("id") if message else None

        if not (interaction_channel_id and message_id):
            self._logger.warn("Missing channel_id or message_id in interaction")
            return

        thread_id = self._encode_interaction_thread_id(interaction, interaction_channel_id)

        self._logger.debug(
            "Processing Discord button action",
            {
                "actionId": custom_id,
                "messageId": message_id,
                "threadId": thread_id,
            },
        )

        decoded = decode_discord_custom_id(custom_id)
        # Select menus report their choice in ``data.values`` (upstream
        # ``values[0] ?? decoded.value ?? actionId``). An empty-string choice
        # is a real value, so only ``None`` falls through.
        values = cast("dict[str, Any]", data).get("values")
        selected = values[0] if isinstance(values, list) and values else None
        if selected is not None:
            value = selected
        elif decoded.value is not None:
            value = decoded.value
        else:
            value = decoded.action_id
        self._chat.process_action(
            ActionEvent(
                action_id=decoded.action_id,
                value=value,
                user=Author(
                    user_id=user.get("id", ""),
                    user_name=user.get("username", ""),
                    full_name=user.get("global_name") or user.get("username", ""),
                    is_bot=user.get("bot", False),
                    is_me=False,
                ),
                message_id=message_id,
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                adapter=self,
                raw=interaction,
            ),
            options,
        )

    def _build_application_command_context(
        self,
        interaction: DiscordInteraction,
    ) -> DiscordInteractionFlagsContext | None:
        """Parse an APPLICATION_COMMAND interaction once (upstream
        ``getApplicationCommandContext``); ``None`` when it is unusable."""
        # `interaction["data"]` is a union of several TypedDicts (one per
        # interaction type). Cast to a plain dict so we can access shared
        # fields like `name` and `options` without pyrefly rejecting keys
        # that only appear on one variant.
        data = cast("dict[str, Any]", interaction.get("data", {}))
        command_name = data.get("name")
        if not command_name:
            self._logger.warn("No command name in application command interaction")
            return None

        user = (interaction.get("member") or {}).get("user") or interaction.get("user")
        if not user:
            self._logger.warn("No user in application command interaction")
            return None

        interaction_channel_id = interaction.get("channel_id")
        if not interaction_channel_id:
            self._logger.warn("Missing channel_id in application command interaction")
            return None

        channel_id = self._encode_interaction_thread_id(interaction, interaction_channel_id)

        command, text = self._parse_slash_command(command_name, data.get("options"))

        return DiscordInteractionFlagsContext(
            channel_id=channel_id,
            command=command,
            interaction=interaction,
            text=text,
            user=user,
        )

    def _get_interaction_flags(self, context: DiscordInteractionFlagsContext | None) -> int | None:
        """Flags for the deferred slash-command response (upstream
        ``getInteractionFlags``)."""
        if not (context and self._interaction_flags):
            return None
        try:
            result: Any = self._interaction_flags(context)
        except Exception as error:
            # Divergence from upstream — see docs/UPSTREAM_SYNC.md: upstream
            # lets the callback throw, which fails the interaction ACK. The
            # command is still acknowledged here, without flags.
            self._logger.error(
                "Discord interaction_flags callback failed; deferring without flags",
                {"error": str(error), "command": context.command},
            )
            return None
        if result is None or (isinstance(result, int) and not isinstance(result, bool)):
            return result
        # Same divergence: an ``async def`` callback (or a non-int result such
        # as ``True``) cannot be serialized as flags. Close a coroutine so it
        # is not left un-awaited, then defer without flags.
        if inspect.iscoroutine(result):
            result.close()
        self._logger.error(
            "Discord interaction_flags callback failed; deferring without flags",
            {"error": f"expected int or None, got {type(result).__name__}", "command": context.command},
        )
        return None

    def _handle_application_command_interaction(
        self,
        context: DiscordInteractionFlagsContext | None,
        initial_response_flags: int | None = None,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle APPLICATION_COMMAND interactions (slash commands)."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring interaction")
            return

        if context is None:
            return

        channel_id = context.channel_id
        command = context.command
        interaction = context.interaction
        text = context.text
        user = context.user

        self._logger.debug(
            "Processing Discord slash command",
            {
                "command": command,
                # Divergence from upstream — see docs/UPSTREAM_SYNC.md: the
                # command text's length, not its content.
                "textLength": len(text),
                "userId": user.get("id"),
                "channelId": channel_id,
            },
        )

        # Keep interaction metadata in the request context so a handler's
        # ``post`` resolves the deferred response. Scoped like upstream's
        # ``requestContext.run``: the handler task copies the context when
        # ``process_slash_command`` creates it, and the reset below keeps the
        # slash context from leaking into later events on the caller's task
        # (which would turn their posts into interaction follow-ups).
        context_token = self._request_context.set(
            DiscordRequestContext(
                slash_command=DiscordSlashCommandContext(
                    channel_id=channel_id,
                    initial_response_flags=initial_response_flags,
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
            channel=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
            raw=interaction,
        )
        event.channel_id = channel_id  # type: ignore[attr-defined]
        try:
            self._chat.process_slash_command(event, options)
        finally:
            self._request_context.reset(context_token)

    def _encode_interaction_thread_id(
        self,
        interaction: DiscordInteraction,
        interaction_channel_id: str,
    ) -> str:
        """Chat SDK thread id for the channel an interaction arrived in.

        Port of upstream ``encodeInteractionThreadId``: a thread channel with
        a known ``parent_id`` encodes as ``guild:parent:thread`` and its
        parent is remembered, so later outbound calls skip the channel
        lookup. A thread without a parent (or any other channel) encodes as
        the channel alone.
        """
        guild_id = interaction.get("guild_id") or "@me"
        channel = interaction.get("channel") or {}
        is_thread = channel.get("type") in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD)
        thread_parent_id = channel.get("parent_id") if is_thread else None
        if not thread_parent_id:
            return self.encode_thread_id(DiscordThreadId(guild_id=guild_id, channel_id=interaction_channel_id))

        self._remember_thread_parent(interaction_channel_id, thread_parent_id)
        return self.encode_thread_id(
            DiscordThreadId(
                guild_id=guild_id,
                channel_id=thread_parent_id,
                thread_id=interaction_channel_id,
            )
        )

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

        def stringify(value: Any) -> str:
            # Match TS `String(value)` for boolean options: JSON booleans
            # arrive as Python bools, and `str(True)` would emit "True"
            # where upstream emits "true" (e.g. a `verbose: true` option).
            if value is True:
                return "true"
            if value is False:
                return "false"
            return str(value)

        def collect(items: list[DiscordCommandOption]) -> None:
            for option in items:
                if option.get("value") is not None:
                    value_parts.append(stringify(option["value"]))
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
        elif event_type == "GATEWAY_INTERACTION_CREATE":
            await self._handle_forwarded_interaction(event.get("data", {}), options)
        else:
            self._logger.debug("Forwarded Gateway event (no handler)", {"type": event_type})

        return self._make_json_response(json.dumps({"ok": True}), 200)

    async def _handle_forwarded_interaction(
        self,
        interaction: DiscordInteraction,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a forwarded INTERACTION_CREATE event (gateway-only mode).

        Discord sends interactions through either the Gateway or an
        Interactions Endpoint URL, not both (vercel/chat#490). Deployments
        that leave the endpoint URL unset receive interactions over the
        Gateway; the forwarder relays the raw INTERACTION_CREATE dispatch
        payload here, which is already in wire format, so the existing HTTP
        interaction handlers consume it unchanged.

        Unlike HTTP interactions -- where the deferral rides the HTTP
        response body -- a gateway interaction is acknowledged with an
        explicit REST call to the interaction callback endpoint. That is
        the same wire call upstream's gateway-only handler makes via
        discord.js ``deferReply()`` (slash commands) and ``deferUpdate()``
        (components). Slash commands then route through the existing slash
        command handler path (the deferred response is later resolved by
        ``post_message`` PATCHing the ``@original`` interaction webhook
        message), and component interactions route through the existing
        action handler path.
        """
        interaction_id = interaction.get("id")
        interaction_token = interaction.get("token")
        interaction_type = interaction.get("type", 0)

        self._logger.info(
            "Discord Gateway interaction received",
            {"id": interaction_id, "type": interaction_type},
        )

        if interaction_type not in (
            INTERACTION_TYPE_APPLICATION_COMMAND,
            INTERACTION_TYPE_MESSAGE_COMPONENT,
        ):
            self._logger.debug(
                "Forwarded Gateway interaction (no handler)",
                {"type": interaction_type},
            )
            return

        if not (interaction_id and interaction_token):
            # A gateway INTERACTION_CREATE always carries id + token; a
            # malformed forward must not produce a garbage callback URL.
            self._logger.warn(
                "Forwarded Gateway interaction missing id or token",
                {"id": interaction_id, "type": interaction_type},
            )
            return

        try:
            if interaction_type == INTERACTION_TYPE_APPLICATION_COMMAND:
                # deferReply: ACK now, respond via the interaction webhook later.
                context = self._build_application_command_context(interaction)
                flags = self._get_interaction_flags(context)
                await self._defer_gateway_interaction(
                    interaction_id,
                    interaction_token,
                    InteractionResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE,
                    flags,
                )
                self._handle_application_command_interaction(context, flags, options)
                return

            # deferUpdate: ACK the component, update the message later.
            await self._defer_gateway_interaction(
                interaction_id,
                interaction_token,
                InteractionResponseType.DEFERRED_UPDATE_MESSAGE,
            )
            self._handle_component_interaction(interaction, options)
        except Exception as error:
            self._logger.error(
                "Error handling Gateway interaction",
                {"error": str(error), "interactionId": interaction_id},
            )

    async def _defer_gateway_interaction(
        self,
        interaction_id: str,
        interaction_token: str,
        response_type: int,
        flags: int | None = None,
    ) -> None:
        """ACK a gateway-received interaction via the callback endpoint.

        ``POST /interactions/{id}/{token}/callback`` is the REST equivalent
        of returning the deferral as the HTTP response body on the
        Interactions Endpoint path (discord.js ``deferReply({flags})`` sends
        the same ``data.flags``). Path segments are URL-quoted so a crafted
        id/token in a forwarded payload cannot pivot the request (hazard #12,
        same guard as :meth:`get_user`).
        """
        body: dict[str, Any] = {"type": response_type}
        if flags is not None:
            body["data"] = {"flags": flags}
        await self._discord_fetch(
            f"/interactions/{quote(interaction_id, safe='')}/{quote(interaction_token, safe='')}/callback",
            "POST",
            body,
        )

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
        # Upstream's forwarder always sends ``thread.parent_id``. Without it the
        # parent is unknown, so fall back to the lookup below (or channel-only)
        # instead of guessing a parent the outbound validation would reject.
        if thread and thread.get("id") and thread.get("parent_id"):
            discord_thread_id = thread["id"]
            parent_channel_id = thread["parent_id"]
            self._remember_thread_parent(discord_thread_id, parent_channel_id)
        elif data.get("channel_type") in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD):
            try:
                response = await self._discord_fetch(f"/channels/{channel_id}", "GET")
                channel_info = response
                if channel_info.get("parent_id"):
                    discord_thread_id = channel_id
                    parent_channel_id = channel_info["parent_id"]
                    # Upstream only logs here; caching the parent Discord just
                    # returned saves the reply's own ``GET /channels/{thread}``.
                    self._remember_thread_parent(channel_id, parent_channel_id)
            except Exception as error:
                self._logger.error(
                    "Failed to fetch thread parent",
                    {
                        "error": str(error),
                        "channelId": channel_id,
                    },
                )

        # Check if bot is mentioned (by user ID or configured role IDs). A
        # forwarder-supplied ``is_mention`` field is ignored, as upstream does
        # since chat@4.41 (``61b98fca``): the mention is derived from the
        # dispatch payload itself.
        mentions = data.get("mentions") or []
        is_user_mentioned = any(m.get("id") == self._application_id for m in mentions)
        mention_roles = data.get("mention_roles") or []
        is_role_mentioned = bool(self._mention_role_ids) and any(
            role_id in self._mention_role_ids for role_id in mention_roles
        )
        # @everyone/@here only count when opted in (strict ``=== true``).
        is_everyone_mentioned = self._respond_to_global_mentions and data.get("mention_everyone") is True
        author_data = data.get("author") or {}
        # Allowlisted channels: any non-bot message whose *parent* channel is
        # listed counts, so messages in their threads qualify too.
        is_channel_allowlisted = not author_data.get("bot", False) and parent_channel_id in self._respond_to_channel_ids
        is_mentioned = is_user_mentioned or is_role_mentioned or is_everyone_mentioned or is_channel_allowlisted

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

        content, attachments_data = _flatten(
            data.get("content", ""),
            cast("list[dict[str, Any]]", data.get("attachments") or []),
            _snapshot_messages(cast("dict[str, Any]", data)),
        )

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
            attachments=[self._build_attachment(a) for a in attachments_data],
            raw=data,
            # ``None`` (not ``False``) when unmentioned: the forwarded payload
            # only proves a mention, so Chat still falls back to text detection
            # for a literal ``@botname`` (upstream ``isMentioned || undefined``,
            # vercel/chat#946).
            is_mention=True if is_mentioned else None,
        )

        try:
            await self._chat.handle_incoming_message(self, thread_id, chat_message)
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

        # Use thread info if the forwarder resolved it, otherwise fall back to
        # the cache and finally to a channel lookup.
        thread = data.get("thread")
        thread_info_id = thread.get("id") if thread else None
        thread_info_parent = thread.get("parent_id") if thread else None
        if thread_info_id and thread_info_parent:
            discord_thread_id = thread_info_id
            parent_channel_id = thread_info_parent
            self._remember_thread_parent(discord_thread_id, parent_channel_id)
        elif data.get("channel_type", 0) in (CHANNEL_TYPE_PUBLIC_THREAD, CHANNEL_TYPE_PRIVATE_THREAD):
            cached_parent = self._cached_thread_parent(channel_id)
            if cached_parent is not None:
                discord_thread_id = channel_id
                parent_channel_id = cached_parent
            else:
                try:
                    channel_info = await self._discord_fetch(f"/channels/{channel_id}", "GET")
                    if channel_info.get("parent_id"):
                        discord_thread_id = channel_id
                        parent_channel_id = channel_info["parent_id"]
                        self._remember_thread_parent(channel_id, parent_channel_id)
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
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
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
        # Validated before the slash-command branch too (upstream resolves
        # first as well), so a forged thread segment is refused even when a
        # slash-command context is active.
        channel_id = await self._resolve_thread_channel_id(decoded.channel_id, decoded.thread_id)

        # Build message payload
        payload: dict[str, Any] = {}
        embeds: list[dict[str, Any]] = []
        components: list[DiscordActionRow] = []

        card = extract_card(message)
        if card:
            card_payload = card_to_discord_payload(card)
            embeds.extend(card_payload["embeds"])
            components.extend(card_payload["components"])
            # Don't include text — Discord renders both `content` and the card
            # embed if `content` is set, so cards would post duplicate text.
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
        # Upstream ``tryPostSlashResponse``: only a post to the interaction's
        # own conversation answers it; posts elsewhere go to their channel.
        if slash_ctx and slash_ctx.channel_id == thread_id:
            return await self._post_slash_command_response(slash_ctx, thread_id, payload, files)

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

    async def _post_slash_command_response(
        self,
        slash_ctx: DiscordSlashCommandContext,
        thread_id: str,
        payload: dict[str, Any],
        files: list[FileUpload],
    ) -> RawMessage:
        """Answer a slash command through its interaction webhook.

        Port of upstream ``postSlashCommandResponse``: the first response
        edits the deferred ``@original`` message, later ones are follow-ups
        (``POST /webhooks/{app}/{token}?wait=true``). The deferral's flags
        (e.g. ephemeral) are OR'd into every response, so follow-ups stay
        ephemeral too.

        Upstream parity, deliberately: the returned message id is a plain
        message id. ``edit_message`` / ``delete_message`` (and the
        post-then-edit streaming fallback) still target
        ``/channels/{id}/messages/{id}``, which cannot reach an ephemeral
        message; upstream ``editMessage`` / ``deleteMessage`` do the same
        (chat@4.41.1 adapter-discord index.ts:1732-1789).
        """
        is_initial_response = not slash_ctx.initial_response_sent
        # Set before awaiting so a concurrent post becomes a follow-up rather
        # than a second ``@original`` edit. Upstream parity (index.ts:1483-1486
        # sets the flag the same way and does not wait for the PATCH before
        # a concurrent follow-up is sent).
        slash_ctx.initial_response_sent = True

        token = quote(slash_ctx.interaction_token, safe="")
        if is_initial_response:
            path = f"/webhooks/{self._application_id}/{token}/messages/@original"
            method = "PATCH"
        else:
            path = f"/webhooks/{self._application_id}/{token}?wait=true"
            method = "POST"

        response_payload = payload
        if slash_ctx.initial_response_flags is not None:
            payload_flags = payload.get("flags")
            response_payload = {
                **payload,
                "flags": slash_ctx.initial_response_flags | (payload_flags if payload_flags is not None else 0),
            }

        self._logger.debug(
            "Discord interaction webhook: responding to slash command",
            {
                "threadId": thread_id,
                "isInitialResponse": is_initial_response,
                "hasFiles": len(files) > 0,
            },
        )

        result = await self._discord_fetch(path, method, response_payload, files=files or None)

        return RawMessage(
            id=(result or {}).get("id", ""),
            thread_id=thread_id,
            raw=result or {},
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Discord message."""
        payload: dict[str, Any] = {}
        embeds: list[dict[str, Any]] = []
        components: list[DiscordActionRow] = []

        card = extract_card(message)
        if card:
            card_payload = card_to_discord_payload(card)
            embeds.extend(card_payload["embeds"])
            components.extend(card_payload["components"])
            # Clear content explicitly so leftover text from a previous edit
            # doesn't render alongside the card. Discord PATCH preserves
            # omitted fields, so we must send "" rather than skip the key.
            payload["content"] = ""
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

        async def patch(channel_id: str) -> Any:
            self._logger.debug(
                "Discord API: PATCH message",
                {
                    "channelId": channel_id,
                    "messageId": message_id,
                    "contentLength": len(payload.get("content", "")),
                },
            )
            return await self._discord_fetch(
                f"/channels/{channel_id}/messages/{message_id}",
                "PATCH",
                payload,
            )

        result = await self._with_message_channel(thread_id, message_id, patch)

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
        """Delete a Discord message.

        Deleting a text-channel thread's starter message (``message_id`` equal
        to the thread segment) deletes it from the parent channel, which
        Discord treats as deleting the thread.
        """

        async def delete(channel_id: str) -> Any:
            self._logger.debug(
                "Discord API: DELETE message",
                {
                    "channelId": channel_id,
                    "messageId": message_id,
                },
            )
            return await self._discord_fetch(
                f"/channels/{channel_id}/messages/{message_id}",
                "DELETE",
            )

        await self._with_message_channel(thread_id, message_id, delete)

        self._logger.debug("Discord API: DELETE message response", {"ok": True})

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a Discord message."""
        await self._with_reaction(thread_id, message_id, emoji, "PUT")

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a Discord message."""
        await self._with_reaction(thread_id, message_id, emoji, "DELETE")

    async def _with_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
        method: Literal["PUT", "DELETE"],
    ) -> None:
        """PUT or DELETE the bot's reaction (upstream ``withReaction``)."""
        emoji_encoded = self._encode_emoji(emoji)

        async def react(channel_id: str) -> Any:
            self._logger.debug(
                f"Discord API: {method} reaction",
                {
                    "channelId": channel_id,
                    "messageId": message_id,
                    "emoji": emoji_encoded,
                },
            )
            return await self._discord_fetch(
                f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_encoded}/@me",
                method,
            )

        await self._with_message_channel(thread_id, message_id, react)

    async def _resolve_thread_channel_id(
        self,
        parent_channel_id: str,
        discord_thread_id: str | None,
    ) -> str:
        """Channel to target for a decoded thread id, validating its parent.

        Port of upstream ``resolveThreadChannelId`` (#875). A thread id's
        thread segment is only trusted once Discord (or a fresh cache entry
        learned from Discord) confirms its parent is the thread id's channel
        segment, so ``discord:g:A:<thread in B>`` cannot reach channel B
        through a guard scoped to A. Raises :class:`ValidationError` on a
        mismatch; costs one ``GET /channels/{thread}`` per uncached thread.
        """
        if not discord_thread_id:
            return parent_channel_id

        cached_parent = self._cached_thread_parent(discord_thread_id)
        if cached_parent is not None:
            if cached_parent == parent_channel_id:
                return discord_thread_id
            raise ValidationError(
                "discord",
                f"Discord thread {discord_thread_id} does not belong to channel {parent_channel_id}",
            )

        channel = await self._discord_fetch(f"/channels/{quote(discord_thread_id, safe='')}", "GET")
        actual_parent = channel.get("parent_id") if isinstance(channel, dict) else None
        if actual_parent != parent_channel_id:
            raise ValidationError(
                "discord",
                f"Discord thread {discord_thread_id} does not belong to channel {parent_channel_id}",
            )

        self._remember_thread_parent(discord_thread_id, parent_channel_id)
        return discord_thread_id

    def _cached_thread_parent(self, thread_id: str) -> str | None:
        """Parent channel id from a fresh cache entry, else ``None``."""
        cached = self._thread_parent_cache.get(thread_id)
        if cached and cached.get("expires_at", 0) > time.time():
            return cached["parent_id"]
        return None

    def _remember_thread_parent(self, thread_id: str, parent_id: str) -> None:
        """Cache a thread's parent channel for ``THREAD_PARENT_CACHE_TTL``."""
        cache = self._thread_parent_cache
        # Re-insert so the dict's insertion order tracks recency for eviction.
        cache.pop(thread_id, None)
        cache[thread_id] = {
            "parent_id": parent_id,
            "expires_at": time.time() + THREAD_PARENT_CACHE_TTL,
        }
        # Prevent unbounded cache growth (upstream's Map is unbounded).
        if len(cache) > THREAD_PARENT_CACHE_MAX:
            now = time.time()
            for key in [k for k, v in cache.items() if v.get("expires_at", 0) <= now]:
                del cache[key]
            # Hard limit: evict the oldest entries if still over threshold.
            overflow = len(cache) - THREAD_PARENT_CACHE_MAX
            for key in list(cache)[: max(overflow, 0)]:
                del cache[key]

    async def _with_message_channel(
        self,
        thread_id: str,
        message_id: str,
        operation: Callable[[str], Awaitable[_T]],
    ) -> _T:
        """Run a message-scoped *operation* against the right channel.

        Port of upstream ``withMessageChannel`` (#815). A thread whose id
        equals the message id is a starter message: forum/media posts keep it
        inside the thread, text-channel threads keep it in the parent
        channel. Try the thread first and fall back to the parent only on
        Discord ``10008`` (Unknown Message); any other error propagates.
        """
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = await self._resolve_thread_channel_id(decoded.channel_id, decoded.thread_id)

        if not (decoded.thread_id and decoded.thread_id == message_id):
            return await operation(target_channel_id)

        try:
            return await operation(decoded.thread_id)
        except NetworkError as error:
            original = error.original_error
            if not (isinstance(original, DiscordApiError) and original.code == DISCORD_UNKNOWN_MESSAGE):
                raise
        return await operation(decoded.channel_id)

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Start typing indicator in a Discord channel or thread."""
        decoded = self.decode_thread_id(thread_id)
        target_channel_id = await self._resolve_thread_channel_id(decoded.channel_id, decoded.thread_id)

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
        target_channel_id = await self._resolve_thread_channel_id(decoded.channel_id, decoded.thread_id)

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

    async def set_thread_title(self, thread_id: str, title: str) -> None:
        """Rename a Discord thread channel (upstream ``setThreadTitle``).

        A thread id without a thread segment names a plain channel and is
        left alone. Otherwise the thread's parent is validated first, then
        ``PATCH /channels/{thread}`` sets its ``name``.
        """
        decoded = self.decode_thread_id(thread_id)
        if not decoded.thread_id:
            return

        target_channel_id = await self._resolve_thread_channel_id(decoded.channel_id, decoded.thread_id)
        await self._discord_fetch(
            f"/channels/{quote(target_channel_id, safe='')}",
            "PATCH",
            {"name": title},
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

            postable: AdapterPostableMessage = PostableRaw(raw=accumulated)

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

        content, attachments_data = _flatten(
            msg.get("content", ""),
            msg.get("attachments") or [],
            _snapshot_messages(msg),
        )

        return Message(
            id=msg.get("id", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(content),
            formatted=self._format_converter.to_ast(content),
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
            attachments=[self._build_attachment(att) for att in attachments_data],
        )

    def _build_attachment(self, att: dict[str, Any]) -> Attachment:
        """Normalize a Discord API attachment, with a guarded ``fetch_data``.

        ``fetch_metadata["url"]`` keeps the CDN URL verbatim (including the
        signed ``ex``/``is``/``hm`` query) so :meth:`rehydrate_attachment`
        can rebuild the download after a JSON round-trip.
        """
        url = att.get("url")
        return self.rehydrate_attachment(
            Attachment(
                type=self._get_attachment_type(att.get("content_type")),
                url=url,
                name=att.get("filename"),
                mime_type=att.get("content_type"),
                size=att.get("size"),
                width=att.get("width"),
                height=att.get("height"),
                fetch_metadata={"url": url} if url else None,
            )
        )

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Rebuild ``fetch_data`` for a (possibly deserialized) attachment.

        Port of upstream ``rehydrateAttachment`` (#679/#800): downloads
        ``fetch_metadata["url"]`` (falling back to ``attachment.url``) through
        the shared guarded downloader. Returns the attachment unchanged when
        it has no URL.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        meta_url = meta.get("url")
        url = meta_url if meta_url is not None else attachment.url
        if not url:
            return attachment

        async def fetch_data() -> bytes:
            return await self._download_attachment(url)

        return replace(attachment, fetch_data=fetch_data)

    async def _download_attachment(self, url: str) -> bytes:
        """Download an attachment URL via the shared guarded downloader.

        HTTPS only, internal addresses refused (including after redirects),
        25 MB cap and 30 s timeout; no Discord credentials are sent.
        """
        from chat_sdk.shared.download import download_attachment  # lazy: pulls in aiohttp

        try:
            return await download_attachment(url, adapter="discord")
        except NetworkError:
            raise
        except Exception as error:
            raise NetworkError("discord", "Failed to download Discord attachment", error) from error

    def _get_attachment_type(self, mime_type: str | None) -> Literal["audio", "file", "image", "video"]:
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
            # Discord error 160004: "A thread has already been created for this
            # message". Match the parsed JSON ``code`` only: the number can
            # appear elsewhere in a body (e.g. a 429's ``retry_after``).
            original = error.original_error
            if isinstance(original, DiscordApiError) and original.code == DISCORD_THREAD_ALREADY_CREATED:
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

        url = f"{self._api_base_url}{path}"
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
                    DiscordApiError(response.status, error_text),
                )

            if response.status == 204:
                return None

            return await response.json()

    # =========================================================================
    # Request/Response helpers (framework-agnostic)
    # =========================================================================

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract the request body as a string."""
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
            if callable(body):
                body = body()
            # Some frameworks expose `body` as an async method; if calling it
            # produced a coroutine, await it before treating as bytes/str.
            if inspect.isawaitable(body):
                body = await body
            if hasattr(body, "read"):
                raw_result = body.read()
                raw = await raw_result if inspect.isawaitable(raw_result) else raw_result
                return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
        data = getattr(request, "data", None)
        if data is not None:
            return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
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
