"""Teams adapter for chat SDK.

Uses the Microsoft Teams Bot Framework for message handling.
Supports messaging, adaptive cards, reactions, and typing indicators.

Python port of packages/adapter-teams/src/index.ts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import UTC, datetime
from typing import Any, NoReturn

from chat_sdk.adapters.teams.cards import card_to_adaptive_card
from chat_sdk.adapters.teams.format_converter import TeamsFormatConverter
from chat_sdk.adapters.teams.types import (
    TeamsAdapterConfig,
    TeamsChannelContext,
    TeamsThreadId,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    NetworkError,
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
    Message,
    MessageMetadata,
    RawMessage,
    ReactionEvent,
    StreamOptions,
    ThreadInfo,
    WebhookOptions,
)

MESSAGEID_CAPTURE_PATTERN = re.compile(r"messageid=(\d+)")
MESSAGEID_STRIP_PATTERN = re.compile(r";messageid=\d+")
CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days

# Allowed Microsoft Bot Framework service URL patterns (SSRF protection).
# Covers commercial, GCC, GCCH, DoD, and sovereign cloud endpoints.
ALLOWED_SERVICE_URL_PATTERNS = [
    re.compile(r"^https://smba\.trafficmanager\.net/"),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.com/"),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.us/"),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.com/"),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.us/"),
    re.compile(r"^https://smba\.infra\.(gcc|gov)\.teams\.microsoft\.(com|us)/"),
]

# Bot Framework OpenID configuration URL for JWT verification
BOT_FRAMEWORK_OPENID_CONFIG_URL = "https://login.botframework.com/v1/.well-known/openid-configuration"


def _validate_service_url(url: str) -> None:
    """Validate that a service URL matches known Microsoft Bot Framework endpoints.

    Raises :class:`~chat_sdk.shared.errors.ValidationError` if the URL is not
    in the allow-list, preventing SSRF attacks via crafted ``serviceUrl`` values.
    """
    for pattern in ALLOWED_SERVICE_URL_PATTERNS:
        if pattern.match(url):
            return
    raise ValidationError(
        "teams",
        f"Service URL is not an allowed Bot Framework endpoint: {url}",
    )


def _handle_teams_error(error: Any, operation: str) -> NoReturn:
    """Convert Teams SDK errors to adapter errors and raise.

    Raises an appropriate AdapterError subclass based on the error shape.
    """
    if error and isinstance(error, dict):
        inner_error = error.get("innerHttpError", {})
        status_code = (
            inner_error.get("statusCode") or error.get("statusCode") or error.get("status") or error.get("code")
        )

        if isinstance(status_code, str) and status_code.isdigit():
            status_code = int(status_code)

        if status_code == 401:
            raise AuthenticationError(
                "teams",
                f"Authentication failed for {operation}: {error.get('message', 'unauthorized')}",
            )
        if status_code == 403 or (
            isinstance(error.get("message"), str) and "permission" in error.get("message", "").lower()
        ):
            raise AdapterPermissionError("teams", operation)
        if status_code == 404:
            raise NetworkError(
                "teams",
                f"Resource not found during {operation}: conversation or message may no longer exist",
            )
        if status_code == 429:
            retry_after = error.get("retryAfter") if isinstance(error.get("retryAfter"), (int, float)) else None
            raise AdapterRateLimitError("teams", retry_after)
        if isinstance(error.get("message"), str):
            raise NetworkError(
                "teams",
                f"Teams API error during {operation}: {error['message']}",
            )

    if isinstance(error, Exception):
        raise NetworkError(
            "teams",
            f"Teams API error during {operation}: {error}",
            error,
        )

    raise NetworkError(
        "teams",
        f"Teams API error during {operation}: {error}",
    )


class TeamsAdapter:
    """Teams adapter for chat SDK.

    Implements the Adapter interface for Microsoft Teams Bot Framework.
    """

    def __init__(self, config: TeamsAdapterConfig | None = None) -> None:
        if config is None:
            config = TeamsAdapterConfig()

        self._name = "teams"
        self._config = config
        self._logger: Logger = config.logger or ConsoleLogger("info", prefix="teams")
        self._user_name = config.user_name or "bot"
        self._chat: ChatInstance | None = None
        self._format_converter = TeamsFormatConverter()

        self._app_id = config.app_id or os.environ.get("TEAMS_APP_ID", "")
        self._app_password = config.app_password or os.environ.get("TEAMS_APP_PASSWORD", "")
        self._app_tenant_id = config.app_tenant_id or os.environ.get("TEAMS_APP_TENANT_ID", "")

        if config.certificate:
            raise ValidationError(
                "teams",
                "Certificate-based authentication is not yet supported. "
                "Use app_password (client secret) or federated (workload identity) authentication instead.",
            )

        if not self._app_id:
            self._logger.warn(
                "Teams app_id is empty — webhook verification will reject all incoming requests. "
                "Set TEAMS_APP_ID or pass app_id in config."
            )

        self._bot_user_id: str | None = self._app_id or None
        self._access_token: str | None = None
        self._token_expiry: float = 0
        self._token_lock = asyncio.Lock()
        self._jwks_client: Any | None = None  # Cached PyJWKClient for JWT verification

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

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
    def lock_scope(self) -> str | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter."""
        self._chat = chat
        self._logger.info("Teams adapter initialized")

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle incoming webhook from Teams Bot Framework.

        Processes message, reaction, and card action activities.
        """
        body = await self._get_request_body(request)
        self._logger.debug("Teams webhook raw body", {"body": body[:500] if body else ""})

        # ---- JWT verification (Bot Framework tokens) ----
        if not self._app_id:
            self._logger.warn("Rejecting Teams webhook: app_id is not configured, cannot verify JWT")
            return self._make_response("Unauthorized – Teams app_id not configured", 401)

        auth_result = await self._verify_bot_framework_token(request)
        if auth_result is not None:
            return auth_result

        try:
            activity: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._logger.error("Failed to parse request body")
            return self._make_response("Invalid JSON", 400)

        activity_type = activity.get("type", "")
        self._logger.debug("Teams activity received", {"type": activity_type})

        # Cache user context from activity metadata
        await self._cache_user_context(activity)

        if activity_type == "message":
            await self._handle_message_activity(activity, options)
        elif activity_type == "messageReaction":
            self._handle_reaction_activity(activity, options)
        elif activity_type == "invoke":
            # Adaptive card actions
            action_data = (activity.get("value") or {}).get("action", {}).get("data", {})
            if action_data.get("actionId"):
                await self._handle_adaptive_card_action(activity, action_data, options)
                return self._make_json_response(
                    json.dumps(
                        {
                            "statusCode": 200,
                            "type": "application/vnd.microsoft.activity.message",
                            "value": "",
                        }
                    ),
                    200,
                )

        return self._make_json_response("{}", 200)

    async def _cache_user_context(self, activity: dict[str, Any]) -> None:
        """Cache serviceUrl, tenantId, and channel context from activity metadata."""
        if not self._chat:
            return

        from_user = activity.get("from", {})
        user_id = from_user.get("id")
        if not user_id:
            return

        ttl = CACHE_TTL_MS
        state = self._chat.get_state()

        # Cache serviceUrl (validate against SSRF allow-list first)
        service_url = activity.get("serviceUrl")
        if service_url and state:
            try:
                _validate_service_url(service_url)
            except ValidationError:
                self._logger.warn(
                    "Refusing to cache disallowed serviceUrl",
                    {"serviceUrl": service_url},
                )
                service_url = None
            if service_url:
                await state.set(f"teams:serviceUrl:{user_id}", service_url, ttl)

        # Cache tenantId
        channel_data = activity.get("channelData", {})
        conversation = activity.get("conversation", {})
        tenant_id = conversation.get("tenantId") or channel_data.get("tenant", {}).get("id")
        if tenant_id and state:
            await state.set(f"teams:tenantId:{user_id}", tenant_id, ttl)

        # Cache channel context
        team_aad_group_id = channel_data.get("team", {}).get("aadGroupId")
        conversation_id = conversation.get("id", "")
        base_channel_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)

        if team_aad_group_id and channel_data.get("channel", {}).get("id") and state:
            context: TeamsChannelContext = {
                "team_id": team_aad_group_id,
                "channel_id": channel_data["channel"]["id"],
            }
            await state.set(f"teams:channelContext:{base_channel_id}", json.dumps(context), ttl)

    async def _handle_message_activity(
        self,
        activity: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle message activities."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring event")
            return

        # Check for button click (Action.Submit) in value
        action_value = activity.get("value", {})
        if isinstance(action_value, dict) and action_value.get("actionId"):
            self._handle_message_action(activity, action_value, options)
            return

        conversation_id = activity.get("conversation", {}).get("id", "")
        service_url = activity.get("serviceUrl", "")

        thread_id = self.encode_thread_id(
            TeamsThreadId(
                conversation_id=conversation_id,
                service_url=service_url,
                reply_to_id=activity.get("replyToId"),
            )
        )

        message = self._parse_teams_message(activity, thread_id)

        # Detect @mention
        entities = activity.get("entities", [])
        is_mention = any(
            e.get("type") == "mention"
            and e.get("mentioned", {}).get("id")
            and (e["mentioned"]["id"] == self._app_id or e["mentioned"]["id"].endswith(f":{self._app_id}"))
            for e in entities
        )
        if is_mention:
            message.is_mention = True

        self._chat.process_message(self, thread_id, message, options)

    def _handle_message_action(
        self,
        activity: dict[str, Any],
        action_value: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle Action.Submit button clicks sent as message activities."""
        if not self._chat:
            return

        conversation_id = activity.get("conversation", {}).get("id", "")
        service_url = activity.get("serviceUrl", "")

        thread_id = self.encode_thread_id(
            TeamsThreadId(
                conversation_id=conversation_id,
                service_url=service_url,
            )
        )

        from_user = activity.get("from", {})
        self._chat.process_action(
            ActionEvent(
                action_id=action_value.get("actionId", ""),
                value=action_value.get("value"),
                user=Author(
                    user_id=from_user.get("id", "unknown"),
                    user_name=from_user.get("name", "unknown"),
                    full_name=from_user.get("name", "unknown"),
                    is_bot=False,
                    is_me=False,
                ),
                message_id=activity.get("replyToId") or activity.get("id", ""),
                thread_id=thread_id,
                thread=None,
                adapter=self,
                raw=activity,
            ),
            options,
        )

    async def _handle_adaptive_card_action(
        self,
        activity: dict[str, Any],
        action_data: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle adaptive card button clicks (invoke-based)."""
        if not self._chat:
            return

        conversation_id = activity.get("conversation", {}).get("id", "")
        service_url = activity.get("serviceUrl", "")

        thread_id = self.encode_thread_id(
            TeamsThreadId(
                conversation_id=conversation_id,
                service_url=service_url,
            )
        )

        from_user = activity.get("from", {})
        self._chat.process_action(
            ActionEvent(
                action_id=action_data.get("actionId", ""),
                value=action_data.get("value"),
                user=Author(
                    user_id=from_user.get("id", "unknown"),
                    user_name=from_user.get("name", "unknown"),
                    full_name=from_user.get("name", "unknown"),
                    is_bot=False,
                    is_me=False,
                ),
                message_id=activity.get("replyToId") or activity.get("id", ""),
                thread_id=thread_id,
                thread=None,
                adapter=self,
                raw=activity,
            ),
            options,
        )

    def _handle_reaction_activity(
        self,
        activity: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle Teams reaction events."""
        if not self._chat:
            return

        conversation_id = activity.get("conversation", {}).get("id", "")
        message_id_match = MESSAGEID_CAPTURE_PATTERN.search(conversation_id)
        message_id = (message_id_match.group(1) if message_id_match else None) or activity.get("replyToId", "")

        service_url = activity.get("serviceUrl", "")
        thread_id = self.encode_thread_id(
            TeamsThreadId(
                conversation_id=conversation_id,
                service_url=service_url,
            )
        )

        from_user = activity.get("from", {})
        user = Author(
            user_id=from_user.get("id", "unknown"),
            user_name=from_user.get("name", "unknown"),
            full_name=from_user.get("name", "unknown"),
            is_bot=False,
            is_me=self._is_message_from_self(activity),
        )

        for reaction in activity.get("reactionsAdded", []):
            raw_emoji = reaction.get("type", "")
            self._chat.process_reaction(
                ReactionEvent(
                    emoji=EmojiValue(name=raw_emoji),
                    raw_emoji=raw_emoji,
                    added=True,
                    user=user,
                    message_id=message_id,
                    thread_id=thread_id,
                    thread=None,
                    adapter=self,
                    raw=activity,
                ),
                options,
            )

        for reaction in activity.get("reactionsRemoved", []):
            raw_emoji = reaction.get("type", "")
            self._chat.process_reaction(
                ReactionEvent(
                    emoji=EmojiValue(name=raw_emoji),
                    raw_emoji=raw_emoji,
                    added=False,
                    user=user,
                    message_id=message_id,
                    thread_id=thread_id,
                    thread=None,
                    adapter=self,
                    raw=activity,
                ),
                options,
            )

    def _parse_teams_message(
        self,
        activity: dict[str, Any],
        thread_id: str,
    ) -> Message:
        """Parse a Teams activity into a Message."""
        text = activity.get("text", "").strip()
        is_me = self._is_message_from_self(activity)
        from_user = activity.get("from", {})

        # Filter out adaptive card and empty HTML attachments
        attachments = [
            self._create_attachment(att)
            for att in activity.get("attachments", [])
            if att.get("contentType") != "application/vnd.microsoft.card.adaptive"
            and not (att.get("contentType") == "text/html" and not att.get("contentUrl"))
        ]

        return Message(
            id=activity.get("id", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(text),
            formatted=self._format_converter.to_ast(text),
            raw=activity,
            author=Author(
                user_id=from_user.get("id", "unknown"),
                user_name=from_user.get("name", "unknown"),
                full_name=from_user.get("name", "unknown"),
                is_bot=False,
                is_me=is_me,
            ),
            metadata=MessageMetadata(
                date_sent=datetime.fromisoformat(activity["timestamp"])
                if activity.get("timestamp")
                else datetime.now(UTC),
                edited=False,
            ),
            attachments=attachments,
        )

    def _create_attachment(self, att: dict[str, Any]) -> Attachment:
        """Create an Attachment from a Teams attachment dict."""
        content_type = att.get("contentType", "")
        att_type: str = "file"
        if content_type.startswith("image/"):
            att_type = "image"
        elif content_type.startswith("video/"):
            att_type = "video"
        elif content_type.startswith("audio/"):
            att_type = "audio"

        return Attachment(
            type=att_type,
            url=att.get("contentUrl"),
            name=att.get("name"),
            mime_type=content_type or None,
        )

    def _is_message_from_self(self, activity: dict[str, Any]) -> bool:
        """Check if the activity is from the bot."""
        from_id = activity.get("from", {}).get("id")
        if not (from_id and self._app_id):
            return False
        if from_id == self._app_id:
            return True
        return bool(from_id.endswith(f":{self._app_id}"))

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Teams conversation."""
        decoded = self.decode_thread_id(thread_id)

        card = extract_card(message)
        if card:
            adaptive_card = card_to_adaptive_card(card)
            activity_payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    }
                ],
            }

            self._logger.debug(
                "Teams API: send (adaptive card)",
                {
                    "conversationId": decoded.conversation_id,
                },
            )

            try:
                result = await self._teams_send(decoded, activity_payload)
                return RawMessage(id=result.get("id", ""), thread_id=thread_id, raw=activity_payload)
            except Exception as error:
                self._logger.error(
                    "Teams API: send failed",
                    {
                        "conversationId": decoded.conversation_id,
                        "error": str(error),
                    },
                )
                error_dict: dict[str, Any] = {"message": str(error)}
                if hasattr(error, "status"):
                    error_dict["statusCode"] = error.status
                _handle_teams_error(error_dict, "postMessage")
                raise  # unreachable: _handle_teams_error always raises

        # Regular text message
        text = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "teams",
        )

        activity_payload = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }

        self._logger.debug(
            "Teams API: send (message)",
            {
                "conversationId": decoded.conversation_id,
                "textLength": len(text),
            },
        )

        try:
            result = await self._teams_send(decoded, activity_payload)
            return RawMessage(id=result.get("id", ""), thread_id=thread_id, raw=activity_payload)
        except Exception as error:
            self._logger.error(
                "Teams API: send failed",
                {
                    "conversationId": decoded.conversation_id,
                    "error": str(error),
                },
            )
            error_dict = {"message": str(error)}
            if hasattr(error, "status"):
                error_dict["statusCode"] = error.status
            _handle_teams_error(error_dict, "postMessage")
            # Should not reach here due to _handle_teams_error always raising
            raise  # pragma: no cover

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Teams message."""
        decoded = self.decode_thread_id(thread_id)

        card = extract_card(message)
        if card:
            adaptive_card = card_to_adaptive_card(card)
            activity_payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    }
                ],
            }
        else:
            text = convert_emoji_placeholders(
                self._format_converter.render_postable(message),
                "teams",
            )
            activity_payload = {
                "type": "message",
                "text": text,
                "textFormat": "markdown",
            }

        self._logger.debug(
            "Teams API: updateActivity",
            {
                "conversationId": decoded.conversation_id,
                "messageId": message_id,
            },
        )

        try:
            await self._teams_update(decoded, message_id, activity_payload)
        except Exception as error:
            self._logger.error(
                "Teams API: updateActivity failed",
                {
                    "conversationId": decoded.conversation_id,
                    "messageId": message_id,
                    "error": str(error),
                },
            )
            error_dict = {"message": str(error)}
            if hasattr(error, "status"):
                error_dict["statusCode"] = error.status
            _handle_teams_error(error_dict, "editMessage")
            raise  # unreachable: _handle_teams_error always raises

        return RawMessage(id=message_id, thread_id=thread_id, raw=activity_payload)

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a Teams message."""
        decoded = self.decode_thread_id(thread_id)

        self._logger.debug(
            "Teams API: deleteActivity",
            {
                "conversationId": decoded.conversation_id,
                "messageId": message_id,
            },
        )

        try:
            await self._teams_delete(decoded, message_id)
        except Exception as error:
            self._logger.error(
                "Teams API: deleteActivity failed",
                {
                    "conversationId": decoded.conversation_id,
                    "messageId": message_id,
                    "error": str(error),
                },
            )
            error_dict = {"message": str(error)}
            if hasattr(error, "status"):
                error_dict["statusCode"] = error.status
            _handle_teams_error(error_dict, "deleteMessage")
            raise  # unreachable: _handle_teams_error always raises

    async def add_reaction(
        self,
        _thread_id: str,
        _message_id: str,
        _emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction (not supported by Teams Bot Framework API)."""
        self._logger.warn("addReaction is not supported by the Teams Bot Framework API")

    async def remove_reaction(
        self,
        _thread_id: str,
        _message_id: str,
        _emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction (not supported by Teams Bot Framework API)."""
        self._logger.warn("removeReaction is not supported by the Teams Bot Framework API")

    async def start_typing(self, thread_id: str, _status: str | None = None) -> None:
        """Send typing indicator to a Teams conversation."""
        decoded = self.decode_thread_id(thread_id)

        self._logger.debug(
            "Teams API: send (typing)",
            {
                "conversationId": decoded.conversation_id,
            },
        )

        try:
            await self._teams_send(decoded, {"type": "typing"})
        except Exception as error:
            self._logger.error(
                "Teams API: send (typing) failed",
                {
                    "conversationId": decoded.conversation_id,
                    "error": str(error),
                },
            )

    async def stream(
        self,
        thread_id: str,
        text_stream: Any,
        _options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream responses via post+edit."""
        decoded = self.decode_thread_id(thread_id)
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

            activity_payload = {
                "type": "message",
                "text": accumulated,
                "textFormat": "markdown",
            }

            if message_id:
                await self._teams_update(decoded, message_id, activity_payload)
            else:
                result = await self._teams_send(decoded, activity_payload)
                message_id = result.get("id", "")

        return RawMessage(
            id=message_id or "",
            thread_id=thread_id,
            raw={"text": accumulated},
        )

    def encode_thread_id(self, platform_data: TeamsThreadId) -> str:
        """Encode platform data into a thread ID string.

        Format: teams:{base64url(conversation_id)}:{base64url(service_url)}
        """
        encoded_conversation_id = (
            base64.urlsafe_b64encode(platform_data.conversation_id.encode("utf-8")).decode("ascii").rstrip("=")
        )
        encoded_service_url = (
            base64.urlsafe_b64encode(platform_data.service_url.encode("utf-8")).decode("ascii").rstrip("=")
        )
        return f"teams:{encoded_conversation_id}:{encoded_service_url}"

    def decode_thread_id(self, thread_id: str) -> TeamsThreadId:
        """Decode thread ID string back to platform data."""
        parts = thread_id.split(":")
        if len(parts) != 3 or parts[0] != "teams":
            raise ValidationError("teams", f"Invalid Teams thread ID: {thread_id}")

        # Add padding for base64url decoding
        def _b64_decode(s: str) -> str:
            padding = 4 - len(s) % 4
            if padding != 4:
                s += "=" * padding
            return base64.urlsafe_b64decode(s.encode("ascii")).decode("utf-8")

        conversation_id = _b64_decode(parts[1])
        service_url = _b64_decode(parts[2])
        return TeamsThreadId(conversation_id=conversation_id, service_url=service_url)

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a DM (not a channel/team conversation)."""
        decoded = self.decode_thread_id(thread_id)
        return not decoded.conversation_id.startswith("19:")

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID by stripping message ID from thread ID."""
        decoded = self.decode_thread_id(thread_id)
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", decoded.conversation_id)
        return self.encode_thread_id(
            TeamsThreadId(
                conversation_id=base_conversation_id,
                service_url=decoded.service_url,
            )
        )

    def parse_message(self, raw: Any) -> Message:
        """Parse a Teams activity into normalized format."""
        thread_id = self.encode_thread_id(
            TeamsThreadId(
                conversation_id=raw.get("conversation", {}).get("id", ""),
                service_url=raw.get("serviceUrl", ""),
            )
        )
        return self._parse_teams_message(raw, thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to Teams markdown."""
        return self._format_converter.from_ast(content)

    # =========================================================================
    # Graph API — message history
    # =========================================================================

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from a Teams conversation via Microsoft Graph API.

        For channel threads (conversationId contains ;messageid=), fetches the
        thread parent + replies. For DM / group chats, lists chat messages.
        """
        if options is None:
            options = FetchOptions()

        decoded = self.decode_thread_id(thread_id)
        conversation_id = decoded.conversation_id
        limit = options.limit if options.limit is not None else 50
        cursor = options.cursor
        direction = options.direction or "backward"

        message_id_match = MESSAGEID_CAPTURE_PATTERN.search(conversation_id)
        thread_message_id = message_id_match.group(1) if message_id_match else None
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)

        channel_context = await self._get_channel_context(base_conversation_id) if thread_message_id else None

        try:
            self._logger.debug(
                "Teams Graph API: fetching messages",
                {
                    "conversationId": base_conversation_id,
                    "threadMessageId": thread_message_id,
                    "hasChannelContext": channel_context is not None,
                    "limit": limit,
                    "cursor": cursor,
                    "direction": direction,
                },
            )

            if channel_context and thread_message_id:
                return await self._fetch_channel_thread_messages(
                    channel_context,
                    thread_message_id,
                    thread_id,
                    options,
                )

            graph_messages: list[dict[str, Any]]
            has_more = False

            if direction == "forward":
                params: dict[str, Any] = {
                    "$top": limit,
                    "$orderby": "createdDateTime asc",
                }
                if cursor:
                    params["$filter"] = f"createdDateTime gt {cursor}"
                graph_messages = await self._graph_list_chat_messages(base_conversation_id, params)
                has_more = len(graph_messages) >= limit
            else:
                params = {
                    "$top": limit,
                    "$orderby": "createdDateTime desc",
                }
                if cursor:
                    params["$filter"] = f"createdDateTime lt {cursor}"
                graph_messages = await self._graph_list_chat_messages(base_conversation_id, params)
                graph_messages.reverse()
                has_more = len(graph_messages) >= limit

            if thread_message_id and not channel_context:
                graph_messages = [msg for msg in graph_messages if msg.get("id") and msg["id"] >= thread_message_id]
                self._logger.debug(
                    "Filtered group chat messages to thread",
                    {"threadMessageId": thread_message_id, "filteredCount": len(graph_messages)},
                )

            self._logger.debug(
                "Teams Graph API: fetched messages",
                {"count": len(graph_messages), "direction": direction, "hasMoreMessages": has_more},
            )

            messages = [self._map_graph_message(msg, thread_id) for msg in graph_messages if msg.get("id")]

            next_cursor: str | None = None
            if has_more and graph_messages:
                if direction == "forward":
                    last_msg = graph_messages[-1]
                    next_cursor = last_msg.get("createdDateTime")
                else:
                    oldest_msg = graph_messages[0]
                    next_cursor = oldest_msg.get("createdDateTime")

            return FetchResult(messages=messages, next_cursor=next_cursor)

        except Exception as error:
            self._logger.error("Teams Graph API: fetchMessages error", {"error": str(error)})

            if isinstance(error, Exception) and "403" in str(error):
                raise AdapterPermissionError(
                    "teams",
                    "fetchMessages",
                    "ChatMessage.Read.Chat, Chat.Read.All, or Chat.Read.WhereInstalled",
                ) from error
            raise

    async def fetch_channel_messages(
        self,
        channel_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch top-level messages from a Teams channel via Microsoft Graph API."""
        if options is None:
            options = FetchOptions()

        decoded = self.decode_thread_id(channel_id)
        conversation_id = decoded.conversation_id
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)
        limit = options.limit if options.limit is not None else 50
        direction = options.direction or "backward"

        try:
            channel_context = await self._get_channel_context(base_conversation_id)

            self._logger.debug(
                "Teams Graph API: fetchChannelMessages",
                {
                    "conversationId": base_conversation_id,
                    "hasChannelContext": channel_context is not None,
                    "limit": limit,
                    "direction": direction,
                },
            )

            graph_messages: list[dict[str, Any]]
            has_more = False

            if channel_context:
                if direction == "forward":
                    graph_messages = await self._graph_list_channel_messages(
                        channel_context["team_id"],
                        channel_context["channel_id"],
                    )
                    graph_messages.reverse()

                    start_index = 0
                    if options.cursor:
                        cursor_val = options.cursor
                        for i, msg in enumerate(graph_messages):
                            if msg.get("createdDateTime") and msg["createdDateTime"] > cursor_val:
                                start_index = i
                                break
                        else:
                            start_index = len(graph_messages)
                    has_more = start_index + limit < len(graph_messages)
                    graph_messages = graph_messages[start_index : start_index + limit]
                else:
                    graph_messages = await self._graph_list_channel_messages(
                        channel_context["team_id"],
                        channel_context["channel_id"],
                        limit=limit,
                    )
                    graph_messages.reverse()
                    has_more = len(graph_messages) >= limit
            elif direction == "forward":
                params = {"$top": limit, "$orderby": "createdDateTime asc"}
                if options.cursor:
                    params["$filter"] = f"createdDateTime gt {options.cursor}"
                graph_messages = await self._graph_list_chat_messages(base_conversation_id, params)
                has_more = len(graph_messages) >= limit
            else:
                params = {"$top": limit, "$orderby": "createdDateTime desc"}
                if options.cursor:
                    params["$filter"] = f"createdDateTime lt {options.cursor}"
                graph_messages = await self._graph_list_chat_messages(base_conversation_id, params)
                graph_messages.reverse()
                has_more = len(graph_messages) >= limit

            messages = [self._map_graph_message(msg, channel_id) for msg in graph_messages if msg.get("id")]

            next_cursor: str | None = None
            if has_more and graph_messages:
                if direction == "forward":
                    next_cursor = graph_messages[-1].get("createdDateTime")
                else:
                    next_cursor = graph_messages[0].get("createdDateTime")

            return FetchResult(messages=messages, next_cursor=next_cursor)

        except Exception as error:
            self._logger.error("Teams Graph API: fetchChannelMessages error", {"error": str(error)})
            raise

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch basic thread info for a Teams conversation."""
        decoded = self.decode_thread_id(thread_id)
        return ThreadInfo(
            id=thread_id,
            channel_id=decoded.conversation_id,
            metadata={},
        )

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel information via Microsoft Graph API.

        For channel conversations, fetches channel metadata from the Graph API.
        For DM / group chat conversations, returns basic info from the thread ID.
        """
        decoded = self.decode_thread_id(channel_id)
        conversation_id = decoded.conversation_id
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)
        is_dm = not conversation_id.startswith("19:")

        channel_context = await self._get_channel_context(base_conversation_id) if not is_dm else None

        if channel_context:
            try:
                token = await self._get_graph_token()
                url = (
                    f"https://graph.microsoft.com/v1.0/teams/{channel_context['team_id']}"
                    f"/channels/{channel_context['channel_id']}"
                )

                session = await self._get_http_session()
                async with session.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                ) as response:
                    if response.ok:
                        data = await response.json()
                        return ChannelInfo(
                            id=channel_id,
                            name=data.get("displayName"),
                            is_dm=False,
                            member_count=data.get("memberCount"),
                            metadata={
                                "team_id": channel_context["team_id"],
                                "channel_id": channel_context["channel_id"],
                                "raw": data,
                            },
                        )
            except Exception as error:
                self._logger.error("Teams Graph API: fetchChannelInfo error", {"error": str(error)})

        return ChannelInfo(
            id=channel_id,
            name=None,
            is_dm=is_dm,
            metadata={
                "conversation_id": base_conversation_id,
            },
        )

    async def open_dm(self, user_id: str) -> str:
        """Open a DM conversation with a user via the Bot Framework.

        Creates a new conversation with the specified user and returns the
        encoded thread ID for the conversation.
        """
        if not self._chat:
            raise ChatNotImplementedError("teams", "openDM requires initialized chat instance")

        state = self._chat.get_state()
        service_url: str | None = None
        tenant_id: str | None = None

        if state:
            service_url = await state.get(f"teams:serviceUrl:{user_id}")
            tenant_id = await state.get(f"teams:tenantId:{user_id}")

        if not service_url:
            service_url = "https://smba.trafficmanager.net/teams/"

        _validate_service_url(service_url)

        token = await self._get_access_token()

        payload: dict[str, Any] = {
            "bot": {"id": self._app_id},
            "members": [{"id": user_id}],
            "isGroup": False,
            "channelData": {},
        }
        if tenant_id:
            payload["channelData"]["tenant"] = {"id": tenant_id}

        url = f"{service_url}v3/conversations"

        session = await self._get_http_session()
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "teams",
                    f"Failed to open DM: {response.status} {error_text}",
                )
            data = await response.json()

        conversation_id = data.get("id", "")
        return self.encode_thread_id(
            TeamsThreadId(
                conversation_id=conversation_id,
                service_url=service_url,
            )
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
        self._logger.debug("Teams adapter disconnecting")

    # =========================================================================
    # Graph API — internal helpers
    # =========================================================================

    async def _get_channel_context(self, base_conversation_id: str) -> TeamsChannelContext | None:
        """Look up cached channel context (team_id, channel_id) for a conversation."""
        if not self._chat:
            return None
        state = self._chat.get_state()
        if not state:
            return None
        raw = await state.get(f"teams:channelContext:{base_conversation_id}")
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass
        return None

    async def _graph_list_chat_messages(
        self,
        chat_id: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """List messages in a chat via Microsoft Graph API."""
        token = await self._get_graph_token()
        url = f"https://graph.microsoft.com/v1.0/chats/{chat_id}/messages"

        session = await self._get_http_session()
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError("teams", f"Graph API error: {response.status} {error_text}")
            data = await response.json()
            return data.get("value", [])

    async def _graph_list_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List messages in a team channel via Microsoft Graph API."""
        token = await self._get_graph_token()
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages"

        session = await self._get_http_session()
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            params={"$top": limit},
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError("teams", f"Graph API error: {response.status} {error_text}")
            data = await response.json()
            return data.get("value", [])

    async def _graph_list_channel_replies(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
    ) -> list[dict[str, Any]]:
        """List replies to a channel message via Microsoft Graph API."""
        token = await self._get_graph_token()
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"

        all_replies: list[dict[str, Any]] = []
        session = await self._get_http_session()
        next_url: str | None = url
        while next_url:
            async with session.get(
                next_url,
                headers={"Authorization": f"Bearer {token}"},
                params={"$top": 50} if next_url == url else None,
            ) as response:
                if not response.ok:
                    error_text = await response.text()
                    raise NetworkError("teams", f"Graph API error: {response.status} {error_text}")
                data = await response.json()
                all_replies.extend(data.get("value", []))
                next_url = data.get("@odata.nextLink")

        return all_replies

    async def _graph_get_channel_message(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Fetch a single channel message via Microsoft Graph API."""
        token = await self._get_graph_token()
        url = f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages/{message_id}"

        session = await self._get_http_session()
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if not response.ok:
                return None
            return await response.json()

    async def _fetch_channel_thread_messages(
        self,
        context: TeamsChannelContext,
        thread_message_id: str,
        thread_id: str,
        options: FetchOptions,
    ) -> FetchResult:
        """Fetch messages from a channel thread (parent + replies)."""
        limit = options.limit if options.limit is not None else 50
        cursor = options.cursor
        direction = options.direction or "backward"

        self._logger.debug(
            "Teams Graph API: fetching channel thread messages",
            {
                "teamId": context["team_id"],
                "channelId": context["channel_id"],
                "threadMessageId": thread_message_id,
                "limit": limit,
                "cursor": cursor,
                "direction": direction,
            },
        )

        parent_message = await self._graph_get_channel_message(
            context["team_id"],
            context["channel_id"],
            thread_message_id,
        )

        all_replies = await self._graph_list_channel_replies(
            context["team_id"],
            context["channel_id"],
            thread_message_id,
        )
        all_replies.reverse()

        all_messages = ([parent_message] if parent_message else []) + all_replies

        graph_messages: list[dict[str, Any]]
        has_more = False

        if direction == "forward":
            start_index = 0
            if cursor:
                for i, msg in enumerate(all_messages):
                    if msg.get("createdDateTime") and msg["createdDateTime"] > cursor:
                        start_index = i
                        break
                else:
                    start_index = len(all_messages)
            has_more = start_index + limit < len(all_messages)
            graph_messages = all_messages[start_index : start_index + limit]
        else:
            if cursor:
                cursor_index = -1
                for i, msg in enumerate(all_messages):
                    if msg.get("createdDateTime") and msg["createdDateTime"] >= cursor:
                        cursor_index = i
                        break
                if cursor_index > 0:
                    slice_start = max(0, cursor_index - limit)
                    graph_messages = all_messages[slice_start:cursor_index]
                    has_more = slice_start > 0
                else:
                    graph_messages = all_messages[-limit:]
                    has_more = len(all_messages) > limit
            else:
                graph_messages = all_messages[-limit:]
                has_more = len(all_messages) > limit

        self._logger.debug(
            "Teams Graph API: fetched channel thread messages",
            {"count": len(graph_messages), "direction": direction, "hasMoreMessages": has_more},
        )

        messages = [self._map_graph_message(msg, thread_id) for msg in graph_messages if msg.get("id")]

        next_cursor: str | None = None
        if has_more and graph_messages:
            if direction == "forward":
                next_cursor = graph_messages[-1].get("createdDateTime")
            else:
                next_cursor = graph_messages[0].get("createdDateTime")

        return FetchResult(messages=messages, next_cursor=next_cursor)

    def _map_graph_message(self, msg: dict[str, Any], thread_id: str) -> Message:
        """Map a Microsoft Graph API chat message to a normalized Message."""
        from_data = msg.get("from") or {}
        user_data = from_data.get("user") or {}
        app_data = from_data.get("application") or {}

        user_id = user_data.get("id") or app_data.get("id") or "unknown"
        user_name = user_data.get("displayName") or app_data.get("displayName") or "unknown"
        is_bot = bool(app_data)
        is_me = user_id == self._app_id or (self._app_id and user_id.endswith(f":{self._app_id}"))

        text = self._extract_text_from_graph_message(msg)

        attachments = self._extract_attachments_from_graph_message(msg)

        return Message(
            id=msg.get("id", ""),
            thread_id=thread_id,
            text=text,
            formatted=self._format_converter.to_ast(text),
            raw=msg,
            author=Author(
                user_id=user_id,
                user_name=user_name,
                full_name=user_name,
                is_bot=is_bot,
                is_me=bool(is_me),
            ),
            metadata=MessageMetadata(
                date_sent=(
                    datetime.fromisoformat(msg["createdDateTime"]) if msg.get("createdDateTime") else datetime.now(UTC)
                ),
                edited=bool(msg.get("lastModifiedDateTime")),
            ),
            attachments=attachments,
        )

    def _extract_text_from_graph_message(self, msg: dict[str, Any]) -> str:
        """Extract plain text from a Graph API message."""
        body = msg.get("body") or {}
        content = body.get("content") or ""

        if body.get("contentType") == "text":
            return content

        # Strip HTML tags
        text = ""
        in_tag = False
        for ch in content:
            if ch == "<":
                in_tag = True
            elif ch == ">":
                in_tag = False
            elif not in_tag:
                text += ch
        text = text.strip()

        if not text and msg.get("attachments"):
            for att in msg["attachments"]:
                if att.get("contentType") == "application/vnd.microsoft.card.adaptive":
                    try:
                        card_data = json.loads(att.get("content", "{}"))
                        title = self._extract_card_title(card_data)
                        return title if title else "[Card]"
                    except (json.JSONDecodeError, ValueError):
                        return "[Card]"

        return text

    def _extract_card_title(self, card: Any) -> str | None:
        """Extract the title from an Adaptive Card JSON."""
        if not isinstance(card, dict):
            return None

        body = card.get("body")
        if not isinstance(body, list):
            return None

        # First pass: look for prominent text blocks
        for element in body:
            if isinstance(element, dict) and element.get("type") == "TextBlock":  # noqa: SIM102
                if element.get("weight") == "bolder" or element.get("size") in ("large", "extraLarge"):
                    text = element.get("text")
                    if isinstance(text, str):
                        return text

        # Second pass: first text block
        for element in body:
            if isinstance(element, dict) and element.get("type") == "TextBlock":
                text = element.get("text")
                if isinstance(text, str):
                    return text

        return None

    def _extract_attachments_from_graph_message(self, msg: dict[str, Any]) -> list[Attachment]:
        """Extract attachments from a Graph API message."""
        raw_attachments = msg.get("attachments") or []
        attachments: list[Attachment] = []
        for att in raw_attachments:
            content_type = att.get("contentType") or ""
            att_type = "image" if "image" in content_type else "file"
            attachments.append(
                Attachment(
                    type=att_type,
                    name=att.get("name"),
                    url=att.get("contentUrl"),
                    mime_type=content_type or None,
                )
            )
        return attachments

    async def _get_graph_token(self) -> str:
        """Get a Microsoft Graph API access token (OAuth2 client credentials)."""
        import time as _time

        # Reuse cached token if valid
        if self._access_token and _time.time() < self._token_expiry:
            return self._access_token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            if self._access_token and _time.time() < self._token_expiry:
                return self._access_token

            tenant_id = self._app_tenant_id or "botframework.com"
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

            session = await self._get_http_session()
            async with session.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._app_id,
                    "client_secret": self._app_password,
                    "scope": "https://graph.microsoft.com/.default",
                },
            ) as response:
                if not response.ok:
                    error_text = await response.text()
                    raise AuthenticationError(
                        "teams",
                        f"Failed to get Graph API token: {response.status} {error_text}",
                    )
                data = await response.json()
                self._access_token = data["access_token"]
                self._token_expiry = _time.time() + data.get("expires_in", 3600) - 300
                return self._access_token  # type: ignore[return-value]

    # =========================================================================
    # Teams Bot Framework HTTP API helpers
    # =========================================================================

    async def _get_access_token(self) -> str:
        """Get a Bot Framework access token (OAuth2 client credentials)."""
        import time

        if self._access_token and time.time() < self._token_expiry:
            return self._access_token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            if self._access_token and time.time() < self._token_expiry:
                return self._access_token

            import aiohttp  # lazy import (needed for ClientError)

            tenant = self._app_tenant_id or "botframework.com"
            token_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

            try:
                session = await self._get_http_session()
                async with session.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._app_id,
                        "client_secret": self._app_password,
                        "scope": "https://api.botframework.com/.default",
                    },
                ) as response:
                    if not response.ok:
                        error_text = await response.text()
                        raise AuthenticationError(
                            "teams",
                            f"Failed to get access token: {response.status} {error_text}",
                        )
                    data = await response.json()
                    self._access_token = data["access_token"]
                    self._token_expiry = time.time() + data.get("expires_in", 3600) - 300
                    return self._access_token  # type: ignore[return-value]
            except AuthenticationError:
                raise
            except aiohttp.ClientError as exc:
                raise NetworkError(
                    "teams",
                    f"Network error obtaining Bot Framework access token: {exc}",
                    exc,
                ) from exc

    async def _teams_send(
        self,
        decoded: TeamsThreadId,
        activity: dict[str, Any],
    ) -> dict[str, Any]:
        """Send an activity to a Teams conversation via Bot Framework REST API."""
        _validate_service_url(decoded.service_url)
        token = await self._get_access_token()
        url = f"{decoded.service_url}v3/conversations/{decoded.conversation_id}/activities"

        session = await self._get_http_session()
        async with session.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=activity,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "teams",
                    f"Teams API error: {response.status} {error_text}",
                )
            return await response.json()

    async def _teams_update(
        self,
        decoded: TeamsThreadId,
        message_id: str,
        activity: dict[str, Any],
    ) -> None:
        """Update an activity in a Teams conversation via Bot Framework REST API."""
        _validate_service_url(decoded.service_url)
        token = await self._get_access_token()
        url = f"{decoded.service_url}v3/conversations/{decoded.conversation_id}/activities/{message_id}"

        session = await self._get_http_session()
        async with session.put(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=activity,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "teams",
                    f"Teams API error: {response.status} {error_text}",
                )

    async def _teams_delete(
        self,
        decoded: TeamsThreadId,
        message_id: str,
    ) -> None:
        """Delete an activity from a Teams conversation via Bot Framework REST API."""
        _validate_service_url(decoded.service_url)
        token = await self._get_access_token()
        url = f"{decoded.service_url}v3/conversations/{decoded.conversation_id}/activities/{message_id}"

        session = await self._get_http_session()
        async with session.delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "teams",
                    f"Teams API error: {response.status} {error_text}",
                )

    # =========================================================================
    # JWT verification (Bot Framework)
    # =========================================================================

    async def _verify_bot_framework_token(self, request: Any) -> Any | None:
        """Verify the JWT Bearer token from the Bot Framework.

        Returns a 401 response dict if authentication fails, or ``None`` if
        the token is valid.
        """
        auth_header: str | None = self._get_header(request, "authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            self._logger.warn("Missing or invalid Authorization header on Teams webhook")
            return self._make_response("Unauthorized", 401)

        token = auth_header[7:]
        try:
            import jwt as pyjwt
            from jwt import PyJWKClient

            # Lazily create and cache the JWKS client
            if self._jwks_client is None:
                session = await self._get_http_session()
                async with session.get(BOT_FRAMEWORK_OPENID_CONFIG_URL) as resp:
                    if resp.status != 200:
                        self._logger.error("Failed to fetch Bot Framework OpenID config", {"status": resp.status})
                        return self._make_response("Unauthorized", 401)
                    openid_config = await resp.json()
                jwks_uri = openid_config.get("jwks_uri")
                if not jwks_uri:
                    self._logger.error("No jwks_uri in Bot Framework OpenID config")
                    return self._make_response("Unauthorized", 401)
                self._jwks_client = PyJWKClient(jwks_uri)

            signing_key = await asyncio.to_thread(self._jwks_client.get_signing_key_from_jwt, token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._app_id,
                issuer="https://api.botframework.com",
            )
            self._logger.debug(
                "Teams JWT verified",
                {
                    "iss": payload.get("iss"),
                    "aud": payload.get("aud"),
                },
            )
            return None  # success
        except Exception as exc:
            self._logger.warn(f"Teams JWT verification failed: {exc}")
            return self._make_response("Unauthorized", 401)

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
            if callable(request.text):
                return await request.text()
            return request.text
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


def create_teams_adapter(config: TeamsAdapterConfig | None = None) -> TeamsAdapter:
    """Factory function to create a Teams adapter."""
    return TeamsAdapter(config)
