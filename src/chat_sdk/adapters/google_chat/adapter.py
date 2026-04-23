"""Google Chat adapter for chat SDK.

Supports messaging via the Google Chat API with service account, ADC, or custom auth.
Handles direct webhooks, Pub/Sub push messages from Workspace Events, card button clicks,
reactions, DMs, and message threading.

Python port of packages/adapter-gchat/src/index.ts.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import time
from collections.abc import AsyncIterable, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, NoReturn

from chat_sdk.adapters.google_chat.cards import card_to_google_card
from chat_sdk.adapters.google_chat.format_converter import GoogleChatFormatConverter
from chat_sdk.adapters.google_chat.thread_utils import (
    GoogleChatThreadId,
    decode_thread_id,
    encode_thread_id,
    is_dm_thread,
)
from chat_sdk.adapters.google_chat.types import (
    GoogleChatAdapterConfig,
    ServiceAccountCredentials,
)
from chat_sdk.adapters.google_chat.user_info import UserInfoCache
from chat_sdk.adapters.google_chat.workspace_events import (
    CreateSpaceSubscriptionOptions,
    WorkspaceEventNotification,
    WorkspaceEventsAuthADC,
    WorkspaceEventsAuthCredentials,
    WorkspaceEventsAuthOptions,
    create_space_subscription,
    decode_pub_sub_message,
    list_space_subscriptions,
)
from chat_sdk.emoji import (
    convert_emoji_placeholders,
    emoji_to_gchat,
    resolve_emoji_from_gchat,
)
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card, extract_files
from chat_sdk.shared.errors import (
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
    EphemeralMessage,
    FetchOptions,
    FetchResult,
    FormattedContent,
    ListThreadsOptions,
    ListThreadsResult,
    LockScope,
    Message,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    StateAdapter,
    StreamChunk,
    StreamOptions,
    ThreadInfo,
    ThreadSummary,
    WebhookOptions,
    _parse_iso,
)

# Strong-reference set for fire-and-forget tasks to prevent GC collection.
_background_tasks: set[asyncio.Task[Any]] = set()


def _pin_task(task: asyncio.Task[Any]) -> None:
    """Pin a fire-and-forget task so the GC doesn't collect it."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# How long before expiry to refresh subscriptions (1 hour)
SUBSCRIPTION_REFRESH_BUFFER_MS = 60 * 60 * 1000
# TTL for subscription cache entries (25 hours - longer than max subscription lifetime)
SUBSCRIPTION_CACHE_TTL_MS = 25 * 60 * 60 * 1000
# Key prefix for space subscription cache
SPACE_SUB_KEY_PREFIX = "gchat:space-sub:"
# Regex for extracting message name from reaction resource name
REACTION_MESSAGE_NAME_PATTERN = re.compile(r"(spaces/[^/]+/messages/[^/]+)")

# Google Chat API base URL
GCHAT_API_BASE = "https://chat.googleapis.com/v1"
# Google Chat API scopes
GCHAT_SCOPES = [
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.messages.reactions.create",
    "https://www.googleapis.com/auth/chat.messages.reactions",
    "https://www.googleapis.com/auth/chat.spaces.create",
]
GCHAT_IMPERSONATION_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.messages.readonly",
]


class GoogleChatAdapter:
    """Google Chat adapter for chat SDK.

    Implements the Adapter interface for Google Chat API.
    Supports service account credentials, Application Default Credentials (ADC),
    and custom auth providers.
    """

    def __init__(self, config: GoogleChatAdapterConfig | None = None) -> None:
        if config is None:
            config = GoogleChatAdapterConfig()

        self._name = "gchat"
        self._lock_scope: LockScope | None = None
        self._persist_message_history: bool | None = None
        self._logger: Logger = config.logger or ConsoleLogger("info").child("gchat")
        self._user_name = config.user_name or "bot"
        self._bot_user_id: str | None = None
        self._chat: ChatInstance | None = None
        self._state: StateAdapter | None = None
        self._format_converter = GoogleChatFormatConverter()

        # User info cache (updated in initialize with state adapter)
        self._user_info_cache = UserInfoCache(None, self._logger)

        # Pub/Sub and Workspace Events config
        self._pubsub_topic = config.pubsub_topic or os.environ.get("GOOGLE_CHAT_PUBSUB_TOPIC")
        self._impersonate_user = config.impersonate_user or os.environ.get("GOOGLE_CHAT_IMPERSONATE_USER")
        self._endpoint_url = config.endpoint_url
        self._google_chat_project_number = config.google_chat_project_number or os.environ.get(
            "GOOGLE_CHAT_PROJECT_NUMBER"
        )
        self._pubsub_audience = config.pubsub_audience or os.environ.get("GOOGLE_CHAT_PUBSUB_AUDIENCE")

        # In-progress subscription creations to prevent duplicate requests
        self._pending_subscriptions: dict[str, Any] = {}

        # Verification warning tracking
        self._warned_no_webhook_verification = False
        self._warned_no_pubsub_verification = False

        # Cached JWKS client for JWT verification (lazy init on first use)
        self._jwks_client: Any | None = None

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        # Auth setup
        self._credentials: ServiceAccountCredentials | None = None
        self._use_adc = False
        self._custom_auth: Any = None
        self._access_token: str | None = None
        self._access_token_expires: float = 0
        self._impersonated_access_token: str | None = None
        self._impersonated_access_token_expires: float = 0
        self._token_lock = asyncio.Lock()

        if config.credentials:
            self._credentials = config.credentials
        elif config.use_application_default_credentials:
            self._use_adc = True
        elif os.environ.get("GOOGLE_CHAT_CREDENTIALS"):
            creds_json = json.loads(os.environ["GOOGLE_CHAT_CREDENTIALS"])
            self._credentials = ServiceAccountCredentials(
                client_email=creds_json["client_email"],
                private_key=creds_json["private_key"],
                project_id=creds_json.get("project_id"),
            )
        elif os.environ.get("GOOGLE_CHAT_USE_ADC") == "true":
            self._use_adc = True
        else:
            raise ValidationError(
                "gchat",
                "Authentication is required. Set GOOGLE_CHAT_CREDENTIALS or "
                "GOOGLE_CHAT_USE_ADC=true, or provide credentials/auth in config.",
            )

    # =========================================================================
    # Properties (Adapter interface)
    # =========================================================================

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
        return self._lock_scope

    @property
    def persist_message_history(self) -> bool | None:
        return self._persist_message_history

    # =========================================================================
    # Auth token management
    # =========================================================================

    async def _get_access_token(self) -> str:
        """Get an access token for Google Chat API calls.

        Caches tokens and refreshes when expired.
        """
        now = time.time()
        if self._access_token and now < self._access_token_expires - 60:
            return self._access_token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            now = time.time()
            if self._access_token and now < self._access_token_expires - 60:
                return self._access_token

            if self._credentials:
                token = await self._get_service_account_token(self._credentials, GCHAT_SCOPES)
            elif self._use_adc:
                token = await self._get_adc_token(GCHAT_SCOPES)
            else:
                raise AuthenticationError("gchat", "No auth configured")

            self._access_token = token
            self._access_token_expires = now + 3500  # ~58 minutes
            return token

    async def _get_impersonated_access_token(self) -> str:
        """Get an access token with user impersonation for DM/listing operations."""
        now = time.time()
        if self._impersonated_access_token and now < self._impersonated_access_token_expires - 60:
            return self._impersonated_access_token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            now = time.time()
            if self._impersonated_access_token and now < self._impersonated_access_token_expires - 60:
                return self._impersonated_access_token

            if self._credentials and self._impersonate_user:
                token = await self._get_service_account_token(
                    self._credentials,
                    GCHAT_IMPERSONATION_SCOPES,
                    subject=self._impersonate_user,
                )
            elif self._use_adc:
                token = await self._get_adc_token(GCHAT_IMPERSONATION_SCOPES)
            else:
                raise AuthenticationError("gchat", "No impersonation auth configured")

            self._impersonated_access_token = token
            self._impersonated_access_token_expires = now + 3500
            return token

    async def _get_service_account_token(
        self,
        credentials: ServiceAccountCredentials,
        scopes: list[str],
        subject: str | None = None,
    ) -> str:
        """Get an access token using service account credentials (JWT flow)."""
        import aiohttp
        import jwt as pyjwt

        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": credentials.client_email,
            "scope": " ".join(scopes),
            "aud": "https://oauth2.googleapis.com/token",
            "iat": now,
            "exp": now + 3600,
        }
        if subject:
            claims["sub"] = subject

        token = pyjwt.encode(
            claims,
            credentials.private_key,
            algorithm="RS256",
        )

        try:
            session = await self._get_http_session()
            async with session.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": token,
                },
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return data["access_token"]
        except aiohttp.ClientError as exc:
            raise AuthenticationError(
                "gchat",
                f"Failed to obtain service account token: {exc}",
            ) from exc

    async def _get_adc_token(self, scopes: list[str]) -> str:
        """Get an access token using Application Default Credentials."""
        try:
            import google.auth
            import google.auth.transport.requests

            creds, _ = google.auth.default(scopes=scopes)
            request = google.auth.transport.requests.Request()
            await asyncio.to_thread(creds.refresh, request)
            return creds.token
        except ImportError:
            pass

        # Fallback: metadata server (GCE, Cloud Run, etc.)
        import aiohttp

        url = (
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/token"
            f"?scopes={','.join(scopes)}"
        )
        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                headers={"Metadata-Flavor": "Google"},
            ) as response:
                response.raise_for_status()
                data = await response.json()
                return data["access_token"]
        except aiohttp.ClientError as exc:
            raise AuthenticationError(
                "gchat",
                f"Failed to obtain ADC token from metadata server: {exc}",
            ) from exc

    # =========================================================================
    # Shared HTTP session
    # =========================================================================

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily if needed."""
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    # =========================================================================
    # Google Chat API request helpers
    # =========================================================================

    async def _gchat_api_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        use_impersonation: bool = False,
    ) -> dict[str, Any]:
        """Make an authenticated request to the Google Chat API.

        Args:
            method: HTTP method (GET, POST, PUT, PATCH, DELETE).
            path: API path (e.g., "spaces/AAAA/messages").
            body: JSON request body.
            params: Query parameters.
            use_impersonation: Use impersonated credentials.

        Returns:
            JSON response as dict.
        """
        if use_impersonation and self._impersonate_user:
            token = await self._get_impersonated_access_token()
        else:
            token = await self._get_access_token()

        url = f"{GCHAT_API_BASE}/{path}"

        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        session = await self._get_http_session()
        async with session.request(
            method,
            url,
            json=body,
            params=params,
            headers=headers,
        ) as response:
            if response.status == 204:
                return {}
            result = await response.json()
            if response.status >= 400:
                error_msg = result.get("error", {}).get("message", str(result))
                error_code = result.get("error", {}).get("code", response.status)
                raise _GoogleApiError(
                    message=error_msg,
                    code=error_code,
                    errors=result.get("error", {}).get("errors"),
                )
            return result

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter with the Chat instance."""
        self._chat = chat
        self._state = chat.get_state()
        # Update user info cache to use the state adapter for persistence
        self._user_info_cache = UserInfoCache(self._state, self._logger)

        # Restore persisted bot user ID from state (for serverless environments)
        if not self._bot_user_id:
            saved_bot_user_id = await self._state.get("gchat:botUserId")
            if saved_bot_user_id:
                self._bot_user_id = saved_bot_user_id
                self._logger.debug(
                    "Restored bot user ID from state",
                    {"botUserId": self._bot_user_id},
                )

    async def disconnect(self) -> None:
        """Disconnect the adapter and close the shared HTTP session."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # =========================================================================
    # Thread subscription (Workspace Events)
    # =========================================================================

    async def on_thread_subscribe(self, thread_id: str) -> None:
        """Called when a thread is subscribed to.

        Ensures the space has a Workspace Events subscription so we receive
        all messages.
        """
        self._logger.info(
            "onThreadSubscribe called",
            {"threadId": thread_id, "hasPubsubTopic": bool(self._pubsub_topic)},
        )

        if not self._pubsub_topic:
            self._logger.warn(
                "No pubsubTopic configured, skipping space subscription. Set GOOGLE_CHAT_PUBSUB_TOPIC env var."
            )
            return

        decoded = self.decode_thread_id(thread_id)
        await self._ensure_space_subscription(decoded.space_name)

    async def _ensure_space_subscription(self, space_name: str) -> None:
        """Ensure a Workspace Events subscription exists for a space.

        Creates one if it doesn't exist or is about to expire.
        """
        self._logger.info(
            "ensureSpaceSubscription called",
            {
                "spaceName": space_name,
                "hasPubsubTopic": bool(self._pubsub_topic),
                "hasState": bool(self._state),
                "hasCredentials": bool(self._credentials),
                "hasADC": self._use_adc,
            },
        )

        if not (self._pubsub_topic and self._state):
            self._logger.warn(
                "ensureSpaceSubscription skipped - missing config",
                {
                    "hasPubsubTopic": bool(self._pubsub_topic),
                    "hasState": bool(self._state),
                },
            )
            return

        cache_key = f"{SPACE_SUB_KEY_PREFIX}{space_name}"

        # Check if we already have a valid subscription
        cached = await self._state.get(cache_key)
        if cached:
            expire_time = (
                cached.get("expire_time", 0) if isinstance(cached, dict) else getattr(cached, "expire_time", 0)
            )
            time_until_expiry = expire_time - int(time.time() * 1000)
            if time_until_expiry > SUBSCRIPTION_REFRESH_BUFFER_MS:
                self._logger.debug(
                    "Space subscription still valid",
                    {
                        "spaceName": space_name,
                        "expiresIn": round(time_until_expiry / 1000 / 60),
                    },
                )
                return
            self._logger.debug(
                "Space subscription expiring soon, will refresh",
                {
                    "spaceName": space_name,
                    "expiresIn": round(time_until_expiry / 1000 / 60),
                },
            )

        # Check if we're already creating a subscription for this space
        if space_name in self._pending_subscriptions:
            self._logger.debug(
                "Subscription creation already in progress, waiting",
                {"spaceName": space_name},
            )
            pending = self._pending_subscriptions[space_name]
            await pending["event"].wait()
            if pending.get("error"):
                raise pending["error"]
            return

        # Create the subscription
        pending_entry: dict[str, Any] = {"event": asyncio.Event(), "error": None}
        self._pending_subscriptions[space_name] = pending_entry
        try:
            await self._create_space_subscription_with_cache(space_name, cache_key)
        except Exception as e:
            pending_entry["error"] = e
            raise
        finally:
            pending_entry["event"].set()
            self._pending_subscriptions.pop(space_name, None)

    async def _create_space_subscription_with_cache(
        self,
        space_name: str,
        cache_key: str,
    ) -> None:
        """Create a Workspace Events subscription and cache the result."""
        auth_options = self._get_auth_options()
        self._logger.info(
            "createSpaceSubscriptionWithCache",
            {
                "spaceName": space_name,
                "hasAuthOptions": bool(auth_options),
                "hasCredentials": bool(self._credentials),
                "hasADC": self._use_adc,
            },
        )

        if not auth_options:
            self._logger.error(
                "Cannot create subscription: no auth configured. "
                "Use GOOGLE_CHAT_CREDENTIALS, GOOGLE_CHAT_USE_ADC=true, or custom auth."
            )
            return

        pubsub_topic = self._pubsub_topic
        if not pubsub_topic:
            return

        try:
            # First check if a subscription already exists via the API
            existing = await self._find_existing_subscription(space_name, auth_options)
            if existing:
                self._logger.debug(
                    "Found existing subscription",
                    {
                        "spaceName": space_name,
                        "subscriptionName": existing["subscription_name"],
                    },
                )
                if self._state:
                    await self._state.set(cache_key, existing, SUBSCRIPTION_CACHE_TTL_MS)
                return

            self._logger.info(
                "Creating Workspace Events subscription",
                {"spaceName": space_name},
            )

            result = await create_space_subscription(
                CreateSpaceSubscriptionOptions(
                    space_name=space_name,
                    pubsub_topic=pubsub_topic,
                ),
                auth_options,
                http_session=await self._get_http_session(),
            )

            subscription_info = {
                "subscription_name": result.name,
                "expire_time": int(_parse_iso(result.expire_time).timestamp() * 1000) if result.expire_time else 0,
            }

            if self._state:
                await self._state.set(cache_key, subscription_info, SUBSCRIPTION_CACHE_TTL_MS)

            self._logger.info(
                "Workspace Events subscription created",
                {
                    "spaceName": space_name,
                    "subscriptionName": result.name,
                    "expireTime": result.expire_time,
                },
            )
        except Exception as error:
            self._logger.error(
                "Failed to create Workspace Events subscription",
                {"spaceName": space_name, "error": error},
            )
            # Don't raise - subscription failure shouldn't break the main flow

    async def _find_existing_subscription(
        self,
        space_name: str,
        auth_options: WorkspaceEventsAuthOptions,
    ) -> dict[str, Any] | None:
        """Check if a subscription already exists for this space."""
        try:
            subscriptions = await list_space_subscriptions(
                space_name, auth_options, http_session=await self._get_http_session()
            )
            for sub in subscriptions:
                expire_time_str = sub.get("expire_time", "")
                if expire_time_str:
                    expire_time = int(_parse_iso(expire_time_str).timestamp() * 1000)
                    if expire_time > int(time.time() * 1000) + SUBSCRIPTION_REFRESH_BUFFER_MS:
                        return {
                            "subscription_name": sub.get("name", ""),
                            "expire_time": expire_time,
                        }
        except Exception as error:
            self._logger.error("Error checking existing subscriptions", {"error": error})
        return None

    def _get_auth_options(self) -> WorkspaceEventsAuthOptions | None:
        """Get auth options for Workspace Events API calls."""
        if self._credentials:
            return WorkspaceEventsAuthCredentials(
                credentials=self._credentials,
                impersonate_user=self._impersonate_user,
            )
        if self._use_adc:
            return WorkspaceEventsAuthADC(
                use_application_default_credentials=True,
                impersonate_user=self._impersonate_user,
            )
        if self._custom_auth:
            return {"auth": self._custom_auth}
        return None

    # =========================================================================
    # JWT verification
    # =========================================================================

    async def _verify_bearer_token(
        self,
        request: Any,
        expected_audience: str,
    ) -> bool:
        """Verify a Google-signed JWT Bearer token from the Authorization header.

        Used for both direct Google Chat webhooks and Pub/Sub push messages.
        """
        # Extract authorization header
        auth_header: str | None = None
        if hasattr(request, "headers"):
            headers = request.headers
            if isinstance(headers, dict) or hasattr(headers, "get"):
                auth_header = headers.get("authorization") or headers.get("Authorization")

        if not auth_header or not auth_header.startswith("Bearer "):
            self._logger.warn("Missing or invalid Authorization header")
            return False

        token = auth_header[7:]
        try:
            import jwt as pyjwt
            from jwt import PyJWKClient

            # Lazily create and cache the JWKS client (avoid per-request instantiation)
            if self._jwks_client is None:
                self._jwks_client = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs")
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            payload = pyjwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=expected_audience,
            )
            self._logger.debug(
                "JWT verified",
                {
                    "iss": payload.get("iss"),
                    "aud": payload.get("aud"),
                    "email": payload.get("email"),
                },
            )
            return True
        except Exception as error:
            self._logger.warn("JWT verification failed", {"error": error})
            return False

    # =========================================================================
    # Webhook handling
    # =========================================================================

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        """Handle incoming webhook from Google Chat.

        Handles direct Google Chat webhooks, Pub/Sub push messages from
        Workspace Events subscriptions, and card button clicks.

        Args:
            request: The incoming HTTP request (dict or request-like object).
            options: Webhook options including wait_until callback.

        Returns:
            Response dict with body and status keys.
        """
        # Auto-detect endpoint URL from incoming request for button click routing
        if not self._endpoint_url:
            try:
                if hasattr(request, "url"):
                    url_str = str(request.url)
                    self._endpoint_url = url_str
                    self._logger.debug(
                        "Auto-detected endpoint URL",
                        {"endpointUrl": self._endpoint_url},
                    )
            except Exception:
                pass

        # Parse request body. `hasattr` narrows `Any` → `object` (not
        # awaitable); `getattr(..., None)` preserves `Any` for the
        # framework duck-typed path.
        body: str
        text_attr = getattr(request, "text", None)
        if text_attr is not None and callable(text_attr):
            result = text_attr()
            body = str(await result if inspect.isawaitable(result) else result)
        else:
            raw_body = getattr(request, "body", None)
            if raw_body is not None:
                body = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else str(raw_body)
            elif isinstance(request, dict):
                body = json.dumps(request)
            else:
                body = str(request)

        self._logger.debug("GChat webhook raw body", {"body": body})

        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {"body": "Invalid JSON", "status": 400}

        # Check if this is a Pub/Sub push message (from Workspace Events subscription)
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("message"), dict)
            and parsed["message"].get("data")
            and parsed.get("subscription")
        ):
            # Verify Pub/Sub JWT if audience is configured
            if self._pubsub_audience:
                valid = await self._verify_bearer_token(request, self._pubsub_audience)
                if not valid:
                    return {"body": "Unauthorized", "status": 401}
            elif not self._warned_no_pubsub_verification:
                self._warned_no_pubsub_verification = True
                self._logger.warn(
                    "Pub/Sub webhook verification is disabled. "
                    "Set GOOGLE_CHAT_PUBSUB_AUDIENCE or pubsubAudience to verify incoming requests."
                )
            return self._handle_pub_sub_message(parsed, options)

        # Verify direct Google Chat webhook JWT if project number is configured
        if self._google_chat_project_number:
            valid = await self._verify_bearer_token(request, self._google_chat_project_number)
            if not valid:
                return {"body": "Unauthorized", "status": 401}
        elif not self._warned_no_webhook_verification:
            self._warned_no_webhook_verification = True
            self._logger.warn(
                "Google Chat webhook verification is disabled. "
                "Set GOOGLE_CHAT_PROJECT_NUMBER or googleChatProjectNumber "
                "to verify incoming requests."
            )

        # Treat as a direct Google Chat webhook event
        event: dict[str, Any] = parsed

        # Handle ADDED_TO_SPACE - automatically create subscription
        added_payload = (event.get("chat") or {}).get("addedToSpacePayload")
        if added_payload:
            space = added_payload.get("space", {})
            self._logger.debug(
                "Bot added to space",
                {"space": space.get("name"), "spaceType": space.get("type")},
            )
            self._handle_added_to_space(space, options)

        # Handle REMOVED_FROM_SPACE (for logging)
        removed_payload = (event.get("chat") or {}).get("removedFromSpacePayload")
        if removed_payload:
            space = removed_payload.get("space", {})
            self._logger.debug("Bot removed from space", {"space": space.get("name")})

        # Handle card button clicks
        button_clicked_payload = (event.get("chat") or {}).get("buttonClickedPayload")
        invoked_function = (event.get("commonEventObject") or {}).get("invokedFunction")
        if button_clicked_payload or invoked_function:
            self._handle_card_click(event, options)
            return {"body": json.dumps({}), "status": 200}

        # Check for message payload in the Add-ons format
        message_payload = (event.get("chat") or {}).get("messagePayload")
        if message_payload:
            self._logger.debug(
                "message event",
                {
                    "space": message_payload.get("space", {}).get("name"),
                    "sender": (message_payload.get("message") or {}).get("sender", {}).get("displayName"),
                    "text": (message_payload.get("message") or {}).get("text", "")[:50],
                },
            )
            self._handle_message_event(event, options)
        elif not (added_payload or removed_payload):
            self._logger.debug(
                "Non-message event received",
                {
                    "hasChat": bool(event.get("chat")),
                    "hasCommonEventObject": bool(event.get("commonEventObject")),
                },
            )

        # Google Chat expects an empty response or a message response
        return {"body": json.dumps({}), "status": 200}

    # =========================================================================
    # Pub/Sub message handling
    # =========================================================================

    def _handle_pub_sub_message(
        self,
        push_message: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> dict[str, Any]:
        """Handle Pub/Sub push messages from Workspace Events subscriptions.

        These contain all messages in a space, not just @mentions.
        """
        # Early filter: Check event type BEFORE base64 decoding to save CPU
        event_type = (push_message.get("message") or {}).get("attributes", {}).get("ce-type")
        allowed_event_types = [
            "google.workspace.chat.message.v1.created",
            "google.workspace.chat.reaction.v1.created",
            "google.workspace.chat.reaction.v1.deleted",
        ]
        if event_type and event_type not in allowed_event_types:
            self._logger.debug("Skipping unsupported Pub/Sub event", {"eventType": event_type})
            return {"body": json.dumps({"success": True}), "status": 200}

        try:
            notification = decode_pub_sub_message(push_message)
            self._logger.debug(
                "Pub/Sub notification decoded",
                {
                    "eventType": notification.event_type,
                    "messageId": (notification.message or {}).get("name") if notification.message else None,
                    "reactionName": (notification.reaction or {}).get("name") if notification.reaction else None,
                },
            )

            # Handle message.created events
            if notification.message:
                self._handle_pub_sub_message_event(notification, options)

            # Handle reaction events
            if notification.reaction:
                self._handle_pub_sub_reaction_event(notification, options)

            # Acknowledge the message
            return {"body": json.dumps({"success": True}), "status": 200}
        except Exception as error:
            self._logger.error("Error processing Pub/Sub message", {"error": error})
            # Return 200 to avoid retries for malformed messages
            return {
                "body": json.dumps({"error": "Processing failed"}),
                "status": 200,
            }

    def _handle_pub_sub_message_event(
        self,
        notification: WorkspaceEventNotification,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle message events received via Pub/Sub (Workspace Events)."""
        if not (self._chat and notification.message):
            return

        message = notification.message
        # Extract space name from targetResource: "//chat.googleapis.com/spaces/AAAA"
        space_name = (notification.target_resource or "").replace("//chat.googleapis.com/", "")
        thread_name = (message.get("thread") or {}).get("name") or message.get("name")
        thread_id = self.encode_thread_id(
            GoogleChatThreadId(
                space_name=space_name or (message.get("space") or {}).get("name", ""),
                thread_name=thread_name,
            )
        )

        # Refresh subscription if needed (runs in background)
        resolved_space_name = space_name or (message.get("space") or {}).get("name")
        if resolved_space_name and options and options.wait_until:

            async def _refresh() -> None:
                try:
                    await self._ensure_space_subscription(resolved_space_name)
                except Exception as err:
                    self._logger.error(
                        "Subscription refresh failed",
                        {"spaceName": resolved_space_name, "error": err},
                    )

            options.wait_until(_refresh())

        # Let Chat class handle async processing and waitUntil
        # Use factory function since parsePubSubMessage is async
        self._chat.process_message(
            self,
            thread_id,
            lambda: self._parse_pub_sub_message(notification, thread_id),
            options,
        )

    def _handle_pub_sub_reaction_event(
        self,
        notification: WorkspaceEventNotification,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle reaction events received via Pub/Sub (Workspace Events).

        Fetches the message to get thread context for proper reply threading.
        """
        if not (self._chat and notification.reaction):
            return

        reaction = notification.reaction
        raw_emoji = (reaction.get("emoji") or {}).get("unicode", "")
        normalized_emoji = resolve_emoji_from_gchat(raw_emoji)

        # Extract message name from reaction name
        # Format: spaces/{space}/messages/{message}/reactions/{reaction}
        reaction_name = reaction.get("name", "")
        message_name_match = REACTION_MESSAGE_NAME_PATTERN.search(reaction_name)
        message_name = message_name_match.group(1) if message_name_match else ""

        # Extract space name from targetResource
        space_name = (notification.target_resource or "").replace("//chat.googleapis.com/", "")

        # Check if reaction is from this bot
        is_me = self._bot_user_id is not None and (reaction.get("user") or {}).get("name") == self._bot_user_id

        # Determine if this is an add or remove
        added = "created" in notification.event_type

        chat = self._chat

        async def build_reaction_event() -> ReactionEvent:
            thread_id: str
            if message_name:
                try:
                    message_response = await self._gchat_api_request("GET", message_name)
                    thread_name = (message_response.get("thread") or {}).get("name")
                    thread_id = self.encode_thread_id(
                        GoogleChatThreadId(
                            space_name=space_name or "",
                            thread_name=thread_name,
                        )
                    )
                    self._logger.debug(
                        "Fetched thread context for reaction",
                        {
                            "messageName": message_name,
                            "threadName": thread_name,
                            "threadId": thread_id,
                        },
                    )
                except Exception as error:
                    self._logger.warn(
                        "Failed to fetch message for thread context",
                        {"messageName": message_name, "error": error},
                    )
                    thread_id = self.encode_thread_id(GoogleChatThreadId(space_name=space_name or ""))
            else:
                thread_id = self.encode_thread_id(GoogleChatThreadId(space_name=space_name or ""))

            reaction_user = reaction.get("user") or {}
            return ReactionEvent(
                adapter=self,
                thread=None,
                thread_id=thread_id,
                message_id=message_name,
                user=Author(
                    user_id=reaction_user.get("name", "unknown"),
                    user_name=reaction_user.get("displayName", "unknown"),
                    full_name=reaction_user.get("displayName", "unknown"),
                    is_bot=reaction_user.get("type") == "BOT",
                    is_me=is_me,
                ),
                emoji=normalized_emoji,
                raw_emoji=raw_emoji,
                added=added,
                raw=notification,
            )

        import asyncio

        async def process_task() -> None:
            reaction_event = await build_reaction_event()
            chat.process_reaction(reaction_event, options)

        if options and options.wait_until:
            options.wait_until(process_task())
        else:
            # Fire and forget
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                _pin_task(loop.create_task(process_task()))
            except RuntimeError:
                pass

    async def _parse_pub_sub_message(
        self,
        notification: WorkspaceEventNotification,
        thread_id: str,
    ) -> Message:
        """Parse a Pub/Sub message into the standard Message format.

        Resolves user display names from cache since Pub/Sub messages
        don't include them.
        """
        message = notification.message
        if not message:
            raise ValidationError("gchat", "PubSub notification missing message")

        text = self._normalize_bot_mentions(message)
        is_bot = (message.get("sender") or {}).get("type") == "BOT"
        is_me = self._is_message_from_self(message)

        # Pub/Sub messages don't include displayName - resolve from cache
        user_id = (message.get("sender") or {}).get("name", "unknown")
        display_name = await self._user_info_cache.resolve_display_name(
            user_id,
            (message.get("sender") or {}).get("displayName"),
            self._bot_user_id,
            self._user_name,
        )

        parsed_message = Message(
            id=message.get("name", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(text),
            formatted=self._format_converter.to_ast(text),
            raw=notification,
            author=Author(
                user_id=user_id,
                user_name=display_name,
                full_name=display_name,
                is_bot=is_bot,
                is_me=is_me,
            ),
            metadata=_parse_message_metadata(message),
            attachments=[self._create_attachment(att) for att in (message.get("attachment") or [])],
        )

        self._logger.debug(
            "Pub/Sub parsed message",
            {
                "threadId": thread_id,
                "messageId": parsed_message.id,
                "text": parsed_message.text,
                "author": parsed_message.author.full_name,
                "isBot": parsed_message.author.is_bot,
                "isMe": parsed_message.author.is_me,
            },
        )

        return parsed_message

    # =========================================================================
    # Added to space / Card click / Message event handlers
    # =========================================================================

    def _handle_added_to_space(
        self,
        space: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle bot being added to a space - create Workspace Events subscription."""
        import asyncio

        async def _subscribe() -> None:
            await self._ensure_space_subscription(space.get("name", ""))

        if options and options.wait_until:
            options.wait_until(_subscribe())
        else:
            try:
                loop = asyncio.get_running_loop()
                _pin_task(loop.create_task(_subscribe()))
            except RuntimeError:
                pass

    def _handle_card_click(
        self,
        event: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle card button clicks.

        For HTTP endpoint apps, the actionId is passed via parameters
        (since function is the URL). For other deployments, actionId may
        be in invokedFunction.
        """
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring card click")
            return

        button_payload = (event.get("chat") or {}).get("buttonClickedPayload")
        common_event = event.get("commonEventObject") or {}

        # Get action ID
        action_id = (common_event.get("parameters") or {}).get("actionId") or common_event.get("invokedFunction")
        if not action_id:
            self._logger.debug(
                "Card click missing actionId",
                {
                    "parameters": common_event.get("parameters"),
                    "invokedFunction": common_event.get("invokedFunction"),
                },
            )
            return

        # Get value from parameters
        value = (common_event.get("parameters") or {}).get("value")

        # Get space and message info
        space = (button_payload or {}).get("space")
        message = (button_payload or {}).get("message")
        user = (button_payload or {}).get("user") or (event.get("chat") or {}).get("user")

        if not space:
            self._logger.warn("Card click missing space info")
            return

        thread_name = (message or {}).get("thread", {}).get("name") or (message or {}).get("name")
        thread_id = self.encode_thread_id(
            GoogleChatThreadId(
                space_name=space.get("name", ""),
                thread_name=thread_name,
            )
        )

        action_event = ActionEvent(
            adapter=self,
            thread=None,
            thread_id=thread_id,
            message_id=(message or {}).get("name", ""),
            user=Author(
                user_id=(user or {}).get("name", "unknown"),
                user_name=(user or {}).get("displayName", "unknown"),
                full_name=(user or {}).get("displayName", "unknown"),
                is_bot=(user or {}).get("type") == "BOT",
                is_me=False,
            ),
            action_id=action_id,
            value=value,
            raw=event,
        )

        self._logger.debug(
            "Processing GChat card click",
            {
                "actionId": action_id,
                "value": value,
                "messageId": action_event.message_id,
                "threadId": thread_id,
            },
        )

        self._chat.process_action(action_event, options)

    def _handle_message_event(
        self,
        event: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle direct webhook message events (Add-ons format)."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring event")
            return

        message_payload = (event.get("chat") or {}).get("messagePayload")
        if not message_payload:
            self._logger.debug("Ignoring event without messagePayload")
            return

        message = message_payload.get("message", {})
        space = message_payload.get("space", {})

        # For DMs, use space-only thread ID so all messages in the DM
        # match the DM subscription created by openDM()
        is_dm = space.get("type") == "DM" or space.get("spaceType") == "DIRECT_MESSAGE"
        thread_name = None if is_dm else ((message.get("thread") or {}).get("name") or message.get("name"))
        thread_id = self.encode_thread_id(
            GoogleChatThreadId(
                space_name=space.get("name", ""),
                thread_name=thread_name,
                is_dm=is_dm,
            )
        )

        # Let Chat class handle async processing and waitUntil
        self._chat.process_message(
            self,
            thread_id,
            self._parse_google_chat_message(event, thread_id),
            options,
        )

    def _parse_google_chat_message(
        self,
        event: dict[str, Any],
        thread_id: str,
    ) -> Message:
        """Parse a direct webhook event into the standard Message format."""
        message_payload = (event.get("chat") or {}).get("messagePayload")
        message = (message_payload or {}).get("message")
        if not message:
            raise ValidationError("gchat", "Event has no message payload")

        # Normalize bot mentions
        text = self._normalize_bot_mentions(message)

        is_bot = (message.get("sender") or {}).get("type") == "BOT"
        is_me = self._is_message_from_self(message)

        # Cache user info for future Pub/Sub messages (which don't include displayName)
        user_id = (message.get("sender") or {}).get("name", "unknown")
        display_name = (message.get("sender") or {}).get("displayName", "unknown")
        if user_id != "unknown" and display_name != "unknown":
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                _pin_task(
                    loop.create_task(
                        self._user_info_cache.set(
                            user_id,
                            display_name,
                            (message.get("sender") or {}).get("email"),
                        )
                    )
                )
            except RuntimeError:
                pass

        return Message(
            id=message.get("name", ""),
            thread_id=thread_id,
            text=self._format_converter.extract_plain_text(text),
            formatted=self._format_converter.to_ast(text),
            raw=event,
            author=Author(
                user_id=user_id,
                user_name=display_name,
                full_name=display_name,
                is_bot=is_bot,
                is_me=is_me,
            ),
            metadata=_parse_message_metadata(message),
            attachments=[self._create_attachment(att) for att in (message.get("attachment") or [])],
        )

    # =========================================================================
    # Posting messages
    # =========================================================================

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Google Chat space/thread."""
        decoded = self.decode_thread_id(thread_id)
        space_name = decoded.space_name
        thread_name = decoded.thread_name

        try:
            # Check for files - currently not implemented for GChat
            files = extract_files(message)
            if files:
                self._logger.warn(
                    "File uploads are not yet supported for Google Chat. Files will be ignored.",
                    {"fileCount": len(files)},
                )

            # Check if message contains a card
            card = extract_card(message)

            if card:
                card_id = f"card-{int(time.time() * 1000)}-{_random_id()}"
                google_card = card_to_google_card(
                    card,
                    {"card_id": card_id, "endpoint_url": self._endpoint_url},
                )

                self._logger.debug(
                    "GChat API: spaces.messages.create (card)",
                    {
                        "spaceName": space_name,
                        "threadName": thread_name,
                        "googleCard": json.dumps(google_card),
                    },
                )

                request_body: dict[str, Any] = {
                    "cardsV2": [google_card],
                }
                if thread_name:
                    request_body["thread"] = {"name": thread_name}

                params: dict[str, str] = {}
                if thread_name:
                    params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

                response = await self._gchat_api_request(
                    "POST",
                    f"{space_name}/messages",
                    body=request_body,
                    params=params if params else None,
                )

                self._logger.debug(
                    "GChat API: spaces.messages.create response",
                    {"messageName": response.get("name")},
                )

                return RawMessage(
                    id=response.get("name", ""),
                    thread_id=thread_id,
                    raw=response,
                )

            # Regular text message
            text = convert_emoji_placeholders(
                self._format_converter.render_postable(message),
                "gchat",
            )

            self._logger.debug(
                "GChat API: spaces.messages.create",
                {
                    "spaceName": space_name,
                    "threadName": thread_name,
                    "textLength": len(text),
                },
            )

            request_body = {"text": text}
            if thread_name:
                request_body["thread"] = {"name": thread_name}

            params = {}
            if thread_name:
                params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

            response = await self._gchat_api_request(
                "POST",
                f"{space_name}/messages",
                body=request_body,
                params=params if params else None,
            )

            self._logger.debug(
                "GChat API: spaces.messages.create response",
                {"messageName": response.get("name")},
            )

            return RawMessage(
                id=response.get("name", ""),
                thread_id=thread_id,
                raw=response,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "postMessage")

    async def post_ephemeral(
        self,
        thread_id: str,
        user_id: str,
        message: AdapterPostableMessage,
    ) -> EphemeralMessage:
        """Post an ephemeral (user-only visible) message."""
        decoded = self.decode_thread_id(thread_id)
        space_name = decoded.space_name
        thread_name = decoded.thread_name

        try:
            card = extract_card(message)

            request_body: dict[str, Any] = {
                "privateMessageViewer": {"name": user_id},
            }
            if thread_name:
                request_body["thread"] = {"name": thread_name}

            if card:
                card_id = f"card-{int(time.time() * 1000)}-{_random_id()}"
                google_card = card_to_google_card(
                    card,
                    {"card_id": card_id, "endpoint_url": self._endpoint_url},
                )
                request_body["cardsV2"] = [google_card]

                self._logger.debug(
                    "GChat API: spaces.messages.create (ephemeral card)",
                    {
                        "spaceName": space_name,
                        "threadName": thread_name,
                        "userId": user_id,
                    },
                )
            else:
                request_body["text"] = convert_emoji_placeholders(
                    self._format_converter.render_postable(message),
                    "gchat",
                )

                self._logger.debug(
                    "GChat API: spaces.messages.create (ephemeral)",
                    {
                        "spaceName": space_name,
                        "threadName": thread_name,
                        "userId": user_id,
                        "textLength": len(request_body["text"]),
                    },
                )

            params: dict[str, str] = {}
            if thread_name:
                params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"

            response = await self._gchat_api_request(
                "POST",
                f"{space_name}/messages",
                body=request_body,
                params=params if params else None,
            )

            self._logger.debug(
                "GChat API: spaces.messages.create ephemeral response",
                {"messageName": response.get("name")},
            )

            return EphemeralMessage(
                id=response.get("name", ""),
                thread_id=thread_id,
                used_fallback=False,
                raw=response,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "postEphemeral")

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing message."""
        try:
            card = extract_card(message)

            if card:
                card_id = f"card-{int(time.time() * 1000)}-{_random_id()}"
                google_card = card_to_google_card(
                    card,
                    {"card_id": card_id, "endpoint_url": self._endpoint_url},
                )

                self._logger.debug(
                    "GChat API: spaces.messages.update (card)",
                    {"messageId": message_id, "cardId": card_id},
                )

                response = await self._gchat_api_request(
                    "PATCH",
                    message_id,
                    body={"cardsV2": [google_card]},
                    params={"updateMask": "cardsV2"},
                )

                self._logger.debug(
                    "GChat API: spaces.messages.update response",
                    {"messageName": response.get("name")},
                )

                return RawMessage(
                    id=response.get("name", ""),
                    thread_id=thread_id,
                    raw=response,
                )

            # Regular text message
            text = convert_emoji_placeholders(
                self._format_converter.render_postable(message),
                "gchat",
            )

            self._logger.debug(
                "GChat API: spaces.messages.update",
                {"messageId": message_id, "textLength": len(text)},
            )

            response = await self._gchat_api_request(
                "PATCH",
                message_id,
                body={"text": text},
                params={"updateMask": "text"},
            )

            self._logger.debug(
                "GChat API: spaces.messages.update response",
                {"messageName": response.get("name")},
            )

            return RawMessage(
                id=response.get("name", ""),
                thread_id=thread_id,
                raw=response,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "editMessage")

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message."""
        try:
            self._logger.debug("GChat API: spaces.messages.delete", {"messageId": message_id})

            await self._gchat_api_request("DELETE", message_id)

            self._logger.debug("GChat API: spaces.messages.delete response", {"ok": True})
        except Exception as error:
            self._handle_google_chat_error(error, "deleteMessage")

    async def stream(
        self,
        thread_id: str,
        text_stream: AsyncIterable[str | StreamChunk],
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream by accumulating all chunks and posting as a single message."""
        accumulated = ""
        async for chunk in text_stream:
            if isinstance(chunk, str):
                accumulated += chunk
            elif hasattr(chunk, "type") and chunk.type == "markdown_text":
                accumulated += chunk.text
        return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))

    # =========================================================================
    # Reactions
    # =========================================================================

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a message."""
        gchat_emoji = emoji_to_gchat(emoji)

        try:
            self._logger.debug(
                "GChat API: spaces.messages.reactions.create",
                {"messageId": message_id, "emoji": gchat_emoji},
            )

            await self._gchat_api_request(
                "POST",
                f"{message_id}/reactions",
                body={"emoji": {"unicode": gchat_emoji}},
            )

            self._logger.debug(
                "GChat API: spaces.messages.reactions.create response",
                {"ok": True},
            )
        except Exception as error:
            self._handle_google_chat_error(error, "addReaction")

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a message."""
        gchat_emoji = emoji_to_gchat(emoji)

        try:
            # Google Chat requires the reaction resource name to delete it.
            # We need to list reactions and find the one with matching emoji.
            self._logger.debug(
                "GChat API: spaces.messages.reactions.list",
                {"messageId": message_id},
            )

            response = await self._gchat_api_request("GET", f"{message_id}/reactions")

            reactions = response.get("reactions", [])
            self._logger.debug(
                "GChat API: spaces.messages.reactions.list response",
                {"reactionCount": len(reactions)},
            )

            matching_reaction = None
            for r in reactions:
                if (r.get("emoji") or {}).get("unicode") == gchat_emoji:
                    matching_reaction = r
                    break

            if not matching_reaction or not matching_reaction.get("name"):
                self._logger.debug(
                    "Reaction not found to remove",
                    {"messageId": message_id, "emoji": gchat_emoji},
                )
                return

            self._logger.debug(
                "GChat API: spaces.messages.reactions.delete",
                {"reactionName": matching_reaction["name"]},
            )

            await self._gchat_api_request("DELETE", matching_reaction["name"])

            self._logger.debug(
                "GChat API: spaces.messages.reactions.delete response",
                {"ok": True},
            )
        except Exception as error:
            self._handle_google_chat_error(error, "removeReaction")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Start typing indicator (Google Chat doesn't support this for bots)."""

    # =========================================================================
    # DMs
    # =========================================================================

    async def open_dm(self, user_id: str) -> str:
        """Open a direct message conversation with a user.

        Returns a thread ID that can be used to post messages.
        For Google Chat, this first tries to find an existing DM space with the user.
        If no DM exists, it creates one using spaces.setup.

        Args:
            user_id: The user's resource name (e.g., "users/123456").
        """
        try:
            # First, try to find an existing DM space with this user
            self._logger.debug("GChat API: spaces.findDirectMessage", {"userId": user_id})

            find_response = await self._gchat_api_request(
                "GET",
                "spaces:findDirectMessage",
                params={"name": user_id},
            )

            if find_response.get("name"):
                self._logger.debug(
                    "GChat API: Found existing DM space",
                    {"spaceName": find_response["name"]},
                )
                return self.encode_thread_id(
                    GoogleChatThreadId(
                        space_name=find_response["name"],
                        is_dm=True,
                    )
                )
        except Exception as error:
            # 404 means no DM exists yet - we'll try to create one
            g_error = error
            error_code = getattr(g_error, "code", None)
            if error_code != 404:
                self._logger.debug("GChat API: findDirectMessage failed", {"error": error})

        # No existing DM found - try to create one
        use_impersonation = bool(self._impersonate_user)

        if not self._impersonate_user:
            self._logger.warn(
                "openDM: No existing DM found and no impersonation configured. "
                "Creating new DMs requires domain-wide delegation. "
                "Set 'impersonateUser' in adapter config."
            )

        try:
            self._logger.debug(
                "GChat API: spaces.setup (DM)",
                {
                    "userId": user_id,
                    "hasImpersonation": use_impersonation,
                    "impersonateUser": self._impersonate_user,
                },
            )

            response = await self._gchat_api_request(
                "POST",
                "spaces:setup",
                body={
                    "space": {
                        "spaceType": "DIRECT_MESSAGE",
                    },
                    "memberships": [
                        {
                            "member": {
                                "name": user_id,
                                "type": "HUMAN",
                            },
                        },
                    ],
                },
                use_impersonation=use_impersonation,
            )

            space_name = response.get("name")
            if not space_name:
                raise NetworkError(
                    "gchat",
                    "Failed to create DM - no space name returned",
                )

            self._logger.debug("GChat API: spaces.setup response", {"spaceName": space_name})

            return self.encode_thread_id(GoogleChatThreadId(space_name=space_name, is_dm=True))
        except Exception as error:
            self._handle_google_chat_error(error, "openDM")

    # =========================================================================
    # Fetching messages
    # =========================================================================

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from a thread."""
        if options is None:
            options = FetchOptions()

        decoded = self.decode_thread_id(thread_id)
        space_name = decoded.space_name
        thread_name = decoded.thread_name
        direction = options.direction or "backward"
        limit = options.limit if options.limit is not None else 100
        use_impersonation = bool(self._impersonate_user)

        try:
            # Build filter to scope to specific thread if threadName is available
            filter_str = f'thread.name = "{thread_name}"' if thread_name else None

            if direction == "forward":
                return await self._fetch_messages_forward(
                    space_name,
                    thread_id,
                    filter_str,
                    limit,
                    options.cursor,
                    use_impersonation,
                )

            return await self._fetch_messages_backward(
                space_name,
                thread_id,
                filter_str,
                limit,
                options.cursor,
                use_impersonation,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "fetchMessages")

    async def _fetch_messages_backward(
        self,
        space_name: str,
        thread_id: str,
        filter_str: str | None,
        limit: int,
        cursor: str | None,
        use_impersonation: bool = False,
    ) -> FetchResult:
        """Fetch messages in backward direction (most recent first)."""
        self._logger.debug(
            "GChat API: spaces.messages.list (backward)",
            {
                "spaceName": space_name,
                "filter": filter_str,
                "pageSize": limit,
                "cursor": cursor,
            },
        )

        params: dict[str, str] = {
            "pageSize": str(limit),
            "orderBy": "createTime desc",
        }
        if cursor:
            params["pageToken"] = cursor
        if filter_str:
            params["filter"] = filter_str

        response = await self._gchat_api_request(
            "GET",
            f"{space_name}/messages",
            params=params,
            use_impersonation=use_impersonation,
        )

        # API returns newest first (DESC), reverse to get chronological order
        raw_messages = list(reversed(response.get("messages") or []))

        self._logger.debug(
            "GChat API: spaces.messages.list response (backward)",
            {
                "messageCount": len(raw_messages),
                "hasNextPageToken": bool(response.get("nextPageToken")),
            },
        )

        messages = []
        for msg in raw_messages:
            parsed = await self._parse_gchat_list_message(msg, space_name, thread_id)
            messages.append(parsed)

        return FetchResult(
            messages=messages,
            next_cursor=response.get("nextPageToken"),
        )

    async def _fetch_messages_forward(
        self,
        space_name: str,
        thread_id: str,
        filter_str: str | None,
        limit: int,
        cursor: str | None,
        use_impersonation: bool = False,
    ) -> FetchResult:
        """Fetch messages in forward direction (oldest first)."""
        self._logger.debug(
            "GChat API: spaces.messages.list (forward)",
            {
                "spaceName": space_name,
                "filter": filter_str,
                "limit": limit,
                "cursor": cursor,
            },
        )

        # Fetch all messages (GChat defaults to createTime ASC = oldest first)
        all_raw_messages: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params: dict[str, str] = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token
            if filter_str:
                params["filter"] = filter_str

            response = await self._gchat_api_request(
                "GET",
                f"{space_name}/messages",
                params=params,
                use_impersonation=use_impersonation,
            )

            page_messages = response.get("messages") or []
            all_raw_messages.extend(page_messages)
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        self._logger.debug(
            "GChat API: fetched all messages for forward pagination",
            {"totalCount": len(all_raw_messages)},
        )

        # Find starting position based on cursor
        start_index = 0
        if cursor:
            for i, msg in enumerate(all_raw_messages):
                if msg.get("name") == cursor:
                    start_index = i + 1
                    break

        # Get the requested slice
        selected_messages = all_raw_messages[start_index : start_index + limit]

        messages = []
        for msg in selected_messages:
            parsed = await self._parse_gchat_list_message(msg, space_name, thread_id)
            messages.append(parsed)

        # Determine nextCursor
        next_cursor: str | None = None
        if start_index + limit < len(all_raw_messages) and selected_messages:
            last_msg = selected_messages[-1]
            if last_msg.get("name"):
                next_cursor = last_msg["name"]

        return FetchResult(messages=messages, next_cursor=next_cursor)

    async def _parse_gchat_list_message(
        self,
        msg: dict[str, Any],
        space_name: str,
        _thread_id: str,
    ) -> Message:
        """Parse a message from the list API into the standard Message format."""
        msg_thread_id = self.encode_thread_id(
            GoogleChatThreadId(
                space_name=space_name,
                thread_name=(msg.get("thread") or {}).get("name"),
            )
        )
        msg_is_bot = (msg.get("sender") or {}).get("type") == "BOT"

        # Resolve display name - the list API may not include it
        user_id = (msg.get("sender") or {}).get("name", "unknown")
        display_name = await self._user_info_cache.resolve_display_name(
            user_id,
            (msg.get("sender") or {}).get("displayName"),
            self._bot_user_id,
            self._user_name,
        )

        is_me = self._is_message_from_self(msg)

        return Message(
            id=msg.get("name", ""),
            thread_id=msg_thread_id,
            text=self._format_converter.extract_plain_text(msg.get("text", "")),
            formatted=self._format_converter.to_ast(msg.get("text", "")),
            raw=msg,
            author=Author(
                user_id=user_id,
                user_name=display_name,
                full_name=display_name,
                is_bot=msg_is_bot,
                is_me=is_me,
            ),
            metadata=_parse_message_metadata(msg),
            attachments=[],
        )

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread information."""
        decoded = self.decode_thread_id(thread_id)
        space_name = decoded.space_name

        try:
            self._logger.debug("GChat API: spaces.get", {"spaceName": space_name})

            response = await self._gchat_api_request("GET", space_name)

            self._logger.debug(
                "GChat API: spaces.get response",
                {"displayName": response.get("displayName")},
            )

            return ThreadInfo(
                id=thread_id,
                channel_id=space_name,
                channel_name=response.get("displayName"),
                metadata={"space": response},
            )
        except Exception as error:
            self._handle_google_chat_error(error, "fetchThread")

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a Google Chat thread ID.

        gchat:{spaceName}:{encodedThreadName} -> gchat:{spaceName}
        """
        decoded = self.decode_thread_id(thread_id)
        return f"gchat:{decoded.space_name}"

    # =========================================================================
    # Channel-level operations
    # =========================================================================

    async def fetch_channel_messages(
        self,
        channel_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch channel-level messages (top-level posts only, not thread replies).

        Google Chat doesn't support server-side filtering for thread roots,
        so we fetch messages and filter client-side.
        """
        if options is None:
            options = FetchOptions()

        # Channel ID format: "gchat:spaces/ABC123"
        parts = channel_id.split(":", 1)
        space_name = parts[1] if len(parts) > 1 else ""
        if not space_name:
            raise ValidationError(
                "gchat",
                f"Invalid Google Chat channel ID: {channel_id}",
            )

        direction = options.direction or "backward"
        limit = options.limit if options.limit is not None else 100
        use_impersonation = bool(self._impersonate_user)

        try:
            if direction == "backward":
                return await self._fetch_channel_messages_backward(
                    space_name,
                    channel_id,
                    limit,
                    options.cursor,
                    use_impersonation,
                )

            return await self._fetch_channel_messages_forward(
                space_name,
                channel_id,
                limit,
                options.cursor,
                use_impersonation,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "fetchChannelMessages")

    def _is_thread_root(self, msg: dict[str, Any]) -> bool:
        """Check if a GChat message is a thread root (not a reply).

        Thread root messages have a name like "spaces/X/messages/THREAD_ID.THREAD_ID"
        where both parts of the dotted message ID match.
        """
        thread_info = msg.get("thread")
        msg_name = msg.get("name")
        if not (thread_info and thread_info.get("name") and msg_name):
            return True

        # Extract the thread ID from thread.name: "spaces/X/threads/THREAD_ID"
        thread_parts = thread_info["name"].split("/")
        thread_id_val = thread_parts[-1] if thread_parts else ""

        # Extract message ID parts from name: "spaces/X/messages/THREAD_ID.MSG_ID"
        msg_parts = msg_name.split("/")
        msg_id_full = msg_parts[-1] if msg_parts else ""
        dot_index = msg_id_full.find(".")
        if dot_index == -1:
            return True  # No dot = top-level

        msg_thread_part = msg_id_full[:dot_index]
        msg_id_part = msg_id_full[dot_index + 1 :]

        # Thread root: both parts match the thread ID
        return msg_thread_part == msg_id_part and msg_thread_part == thread_id_val

    async def _fetch_channel_messages_backward(
        self,
        space_name: str,
        channel_id: str,
        limit: int,
        cursor: str | None,
        use_impersonation: bool = False,
    ) -> FetchResult:
        """Fetch channel messages backward, filtered to thread roots only."""
        self._logger.debug(
            "GChat API: spaces.messages.list (channel, backward)",
            {"spaceName": space_name, "limit": limit, "cursor": cursor},
        )

        top_level: list[dict[str, Any]] = []
        page_token = cursor
        api_next_page_token: str | None = None

        while len(top_level) < limit:
            params: dict[str, str] = {
                "pageSize": str(min(limit * 3, 1000)),
                "orderBy": "createTime desc",
            }
            if page_token:
                params["pageToken"] = page_token

            response = await self._gchat_api_request(
                "GET",
                f"{space_name}/messages",
                params=params,
                use_impersonation=use_impersonation,
            )

            page_messages = response.get("messages") or []
            if not page_messages:
                break

            for msg in page_messages:
                if self._is_thread_root(msg):
                    top_level.append(msg)

            api_next_page_token = response.get("nextPageToken")
            if not api_next_page_token:
                break
            page_token = api_next_page_token

        # Take only the requested limit and reverse to chronological order
        selected = list(reversed(top_level[:limit]))

        messages = []
        for msg in selected:
            parsed = await self._parse_gchat_list_message(msg, space_name, channel_id)
            messages.append(parsed)

        return FetchResult(
            messages=messages,
            next_cursor=api_next_page_token if len(top_level) >= limit else None,
        )

    async def _fetch_channel_messages_forward(
        self,
        space_name: str,
        channel_id: str,
        limit: int,
        cursor: str | None,
        use_impersonation: bool = False,
    ) -> FetchResult:
        """Fetch channel messages forward, filtered to thread roots only."""
        self._logger.debug(
            "GChat API: spaces.messages.list (channel, forward)",
            {"spaceName": space_name, "limit": limit, "cursor": cursor},
        )

        # Fetch all messages and filter to thread roots
        all_raw_messages: list[dict[str, Any]] = []
        page_token: str | None = None

        while True:
            params: dict[str, str] = {"pageSize": "1000"}
            if page_token:
                params["pageToken"] = page_token

            response = await self._gchat_api_request(
                "GET",
                f"{space_name}/messages",
                params=params,
                use_impersonation=use_impersonation,
            )

            all_raw_messages.extend(response.get("messages") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        # Filter to thread roots only
        top_level = [msg for msg in all_raw_messages if self._is_thread_root(msg)]

        start_index = 0
        if cursor:
            for i, msg in enumerate(top_level):
                if msg.get("name") == cursor:
                    start_index = i + 1
                    break

        selected_messages = top_level[start_index : start_index + limit]

        messages = []
        for msg in selected_messages:
            parsed = await self._parse_gchat_list_message(msg, space_name, channel_id)
            messages.append(parsed)

        next_cursor: str | None = None
        if start_index + limit < len(top_level) and selected_messages:
            last_msg = selected_messages[-1]
            if last_msg.get("name"):
                next_cursor = last_msg["name"]

        return FetchResult(messages=messages, next_cursor=next_cursor)

    async def list_threads(
        self,
        channel_id: str,
        options: ListThreadsOptions | None = None,
    ) -> ListThreadsResult:
        """List threads in a Google Chat space.

        Fetches messages and deduplicates by thread name.
        """
        if options is None:
            options = ListThreadsOptions()

        parts = channel_id.split(":", 1)
        space_name = parts[1] if len(parts) > 1 else ""
        if not space_name:
            raise ValidationError(
                "gchat",
                f"Invalid Google Chat channel ID: {channel_id}",
            )

        limit = options.limit if options.limit is not None else 50
        use_impersonation = bool(self._impersonate_user)

        try:
            self._logger.debug(
                "GChat API: spaces.messages.list (listThreads)",
                {
                    "spaceName": space_name,
                    "limit": limit,
                    "cursor": options.cursor,
                },
            )

            params: dict[str, str] = {
                "pageSize": str(min(limit * 3, 1000)),
                "orderBy": "createTime desc",
            }
            if options.cursor:
                params["pageToken"] = options.cursor

            response = await self._gchat_api_request(
                "GET",
                f"{space_name}/messages",
                params=params,
                use_impersonation=use_impersonation,
            )

            raw_messages = response.get("messages") or []

            # Group by thread name, keeping the first (most recent) message per thread
            thread_map: dict[str, dict[str, Any]] = {}
            for msg in raw_messages:
                thread_name = (msg.get("thread") or {}).get("name")
                if not thread_name:
                    continue
                existing = thread_map.get(thread_name)
                if existing:
                    existing["count"] += 1
                else:
                    thread_map[thread_name] = {"root_msg": msg, "count": 1}

            # Convert to ThreadSummary (limited)
            threads: list[ThreadSummary] = []
            count = 0
            for thread_name, entry in thread_map.items():
                if count >= limit:
                    break

                root_msg = entry["root_msg"]
                reply_count = entry["count"]

                thread_id = self.encode_thread_id(
                    GoogleChatThreadId(
                        space_name=space_name,
                        thread_name=thread_name,
                    )
                )

                msg = await self._parse_gchat_list_message(root_msg, space_name, thread_id)

                create_time = root_msg.get("createTime")
                last_reply_at: datetime | None = None
                if create_time:
                    with contextlib.suppress(ValueError, AttributeError):
                        last_reply_at = _parse_iso(create_time)

                threads.append(
                    ThreadSummary(
                        id=thread_id,
                        root_message=msg,
                        reply_count=reply_count,
                        last_reply_at=last_reply_at,
                    )
                )
                count += 1  # noqa: SIM113

            self._logger.debug(
                "GChat API: listThreads result",
                {"threadCount": len(threads)},
            )

            return ListThreadsResult(
                threads=threads,
                next_cursor=response.get("nextPageToken"),
            )
        except Exception as error:
            self._handle_google_chat_error(error, "listThreads")

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch Google Chat space info/metadata."""
        parts = channel_id.split(":", 1)
        space_name = parts[1] if len(parts) > 1 else ""
        if not space_name:
            raise ValidationError(
                "gchat",
                f"Invalid Google Chat channel ID: {channel_id}",
            )

        try:
            self._logger.debug("GChat API: spaces.get (channelInfo)", {"spaceName": space_name})

            space = await self._gchat_api_request("GET", space_name)

            # Try to get member count
            member_count: int | None = None
            try:
                members_response = await self._gchat_api_request(
                    "GET",
                    f"{space_name}/members",
                    params={"pageSize": "1"},
                )
                memberships = members_response.get("memberships")
                if memberships:
                    member_count = len(memberships)
            except Exception:
                # Member list may not be accessible
                pass

            return ChannelInfo(
                id=channel_id,
                name=space.get("displayName"),
                is_dm=(space.get("spaceType") == "DIRECT_MESSAGE" or space.get("singleUserBotDm") is True),
                member_count=member_count,
                metadata={
                    "spaceType": space.get("spaceType"),
                    "spaceThreadingState": space.get("spaceThreadingState"),
                    "raw": space,
                },
            )
        except Exception as error:
            self._handle_google_chat_error(error, "fetchChannelInfo")

    async def post_channel_message(
        self,
        channel_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a space top-level (starts a new conversation, not in a thread)."""
        parts = channel_id.split(":", 1)
        space_name = parts[1] if len(parts) > 1 else ""
        if not space_name:
            raise ValidationError(
                "gchat",
                f"Invalid Google Chat channel ID: {channel_id}",
            )

        try:
            card = extract_card(message)

            if card:
                card_id = f"card-{int(time.time() * 1000)}-{_random_id()}"
                google_card = card_to_google_card(
                    card,
                    {"card_id": card_id, "endpoint_url": self._endpoint_url},
                )

                self._logger.debug(
                    "GChat API: spaces.messages.create (channel, card)",
                    {"spaceName": space_name},
                )

                response = await self._gchat_api_request(
                    "POST",
                    f"{space_name}/messages",
                    body={"cardsV2": [google_card]},
                )

                return RawMessage(
                    id=response.get("name", ""),
                    thread_id=channel_id,
                    raw=response,
                )

            # Regular text message
            text = convert_emoji_placeholders(
                self._format_converter.render_postable(message),
                "gchat",
            )

            self._logger.debug(
                "GChat API: spaces.messages.create (channel)",
                {"spaceName": space_name, "textLength": len(text)},
            )

            response = await self._gchat_api_request(
                "POST",
                f"{space_name}/messages",
                body={"text": text},
            )

            return RawMessage(
                id=response.get("name", ""),
                thread_id=channel_id,
                raw=response,
            )
        except Exception as error:
            self._handle_google_chat_error(error, "postChannelMessage")

    # =========================================================================
    # Thread ID encoding/decoding
    # =========================================================================

    def encode_thread_id(self, platform_data: GoogleChatThreadId) -> str:
        """Encode platform-specific data into a thread ID string."""
        return encode_thread_id(platform_data)

    def decode_thread_id(self, thread_id: str) -> GoogleChatThreadId:
        """Decode thread ID string back to platform-specific data."""
        return decode_thread_id(thread_id)

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread is a direct message conversation."""
        return is_dm_thread(thread_id)

    # =========================================================================
    # Message parsing / rendering
    # =========================================================================

    def parse_message(self, raw: Any) -> Message:
        """Parse a raw event into a standard Message."""
        event = raw if isinstance(raw, dict) else {}
        message_payload = (event.get("chat") or {}).get("messagePayload")
        if not message_payload:
            raise ValidationError("gchat", "Cannot parse non-message event")
        message = message_payload.get("message", {})
        thread_name = (message.get("thread") or {}).get("name") or message.get("name")
        space = message_payload.get("space", {})
        thread_id = self.encode_thread_id(
            GoogleChatThreadId(
                space_name=space.get("name", ""),
                thread_name=thread_name,
            )
        )
        return self._parse_google_chat_message(event, thread_id)

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content (mdast AST) to Google Chat format."""
        return self._format_converter.from_ast(content)

    # =========================================================================
    # Private helpers: bot mentions, self detection, attachments
    # =========================================================================

    def _normalize_bot_mentions(self, message: dict[str, Any]) -> str:
        """Normalize bot mentions in message text.

        Google Chat uses the bot's display name (e.g., "@Chat SDK Demo") but the
        Chat SDK expects "@{userName}" format. This replaces bot mentions with
        the adapter's userName so mention detection works properly.
        Also learns the bot's user ID from annotations for isMe detection.
        """
        text = message.get("text", "")

        annotations = message.get("annotations") or []
        # Process in reverse order (highest startIndex first) to avoid index drift
        # when replacing mentions with different-length strings.
        annotations = sorted(annotations, key=lambda a: a.get("startIndex", 0), reverse=True)
        for annotation in annotations:
            if (
                annotation.get("type") == "USER_MENTION"
                and (annotation.get("userMention") or {}).get("user", {}).get("type") == "BOT"
            ):
                bot_user = annotation["userMention"]["user"]
                bot_display_name = bot_user.get("displayName")

                # Learn our bot's user ID from mentions and persist to state
                if bot_user.get("name") and not self._bot_user_id:
                    self._bot_user_id = bot_user["name"]
                    self._logger.info(
                        "Learned bot user ID from mention",
                        {"botUserId": self._bot_user_id},
                    )
                    # Persist to state for serverless environments
                    if self._state:
                        import asyncio

                        try:
                            loop = asyncio.get_running_loop()
                            _pin_task(
                                loop.create_task(
                                    self._state.set(
                                        "gchat:botUserId", self._bot_user_id, ttl_ms=30 * 24 * 60 * 60 * 1000
                                    )
                                )
                            )
                        except RuntimeError:
                            pass

                # Replace the bot mention with @{userName}
                start_index = annotation.get("startIndex")
                length = annotation.get("length")
                if start_index is not None and length is not None:
                    start_index = int(start_index)
                    length = int(length)
                    mention_text = text[start_index : start_index + length]
                    text = text[:start_index] + f"@{self._user_name}" + text[start_index + length :]
                    self._logger.debug(
                        "Normalized bot mention",
                        {
                            "original": mention_text,
                            "replacement": f"@{self._user_name}",
                        },
                    )
                elif bot_display_name:
                    mention_text = f"@{bot_display_name}"
                    text = text.replace(mention_text, f"@{self._user_name}")

        return text

    def _is_message_from_self(self, message: dict[str, Any]) -> bool:
        """Check if a message is from this bot.

        Bot user ID is learned dynamically from message annotations when the bot
        is @mentioned. Until we learn the ID, we cannot reliably determine isMe.
        """
        sender_id = (message.get("sender") or {}).get("name")

        # Use exact match when we know our bot ID
        if self._bot_user_id and sender_id:
            return sender_id == self._bot_user_id

        # If we don't know our bot ID yet, we can't reliably determine isMe
        if not self._bot_user_id and (message.get("sender") or {}).get("type") == "BOT":
            self._logger.debug(
                "Cannot determine isMe - bot user ID not yet learned. "
                "Bot ID is learned from @mentions. Assuming message is not from self.",
                {"senderId": sender_id},
            )

        return False

    def _create_attachment(self, att: dict[str, Any]) -> Attachment:
        """Create an Attachment object from a Google Chat attachment."""
        url = att.get("downloadUri")
        resource_name = (att.get("attachmentDataRef") or {}).get("resourceName")

        # Determine type based on contentType
        content_type = att.get("contentType", "")
        att_type: str = "file"
        if content_type.startswith("image/"):
            att_type = "image"
        elif content_type.startswith("video/"):
            att_type = "video"
        elif content_type.startswith("audio/"):
            att_type = "audio"

        # Build fetchData closure
        fetch_data: Callable[[], Awaitable[bytes]] | None = None
        if resource_name or url:
            adapter = self

            async def _fetch_data() -> bytes:
                # Prefer media.download API
                if resource_name:
                    token = await adapter._get_access_token()
                    download_url = f"https://chat.googleapis.com/v1/media/{resource_name}?alt=media"
                    session = await adapter._get_http_session()
                    async with session.get(
                        download_url,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as response:
                        if response.status >= 400:
                            raise NetworkError(
                                "gchat",
                                f"Failed to download media: {response.status}",
                            )
                        return await response.read()

                # Fallback to direct URL fetch (downloadUri)
                if url:
                    token = await adapter._get_access_token()
                    session = await adapter._get_http_session()
                    async with session.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as response:
                        if response.status >= 400:
                            raise NetworkError(
                                "gchat",
                                f"Failed to fetch file: {response.status}",
                            )
                        return await response.read()

                raise AuthenticationError("gchat", "Cannot fetch file: no URL or resource name")

            fetch_data = _fetch_data

        return Attachment(
            type=att_type,  # type: ignore[arg-type]
            url=url,
            name=att.get("contentName"),
            mime_type=att.get("contentType"),
            fetch_data=fetch_data,
        )

    # =========================================================================
    # Error handling
    # =========================================================================

    def _handle_google_chat_error(self, error: Any, context: str | None = None) -> NoReturn:
        """Handle Google Chat API errors with proper error classification.

        Always re-raises — the `NoReturn` annotation lets type checkers skip
        the "missing return" warning for callers that rely on this to
        propagate out of a `try/except` block.
        """
        error_code = getattr(error, "code", None)
        error_message = getattr(error, "message", str(error))
        error_errors = getattr(error, "errors", None)

        self._logger.error(
            f"GChat API error{f' ({context})' if context else ''}",
            {
                "code": error_code,
                "message": error_message,
                "errors": error_errors,
                "error": error,
            },
        )

        if error_code == 429:
            raise AdapterRateLimitError("gchat")

        raise error


# =============================================================================
# Factory function
# =============================================================================


def create_google_chat_adapter(
    config: GoogleChatAdapterConfig | None = None,
) -> GoogleChatAdapter:
    """Create a new Google Chat adapter instance.

    Args:
        config: Adapter configuration. If None, auto-detects from env vars.

    Returns:
        Configured GoogleChatAdapter instance.
    """
    return GoogleChatAdapter(config)


# =============================================================================
# Internal helpers
# =============================================================================


class _GoogleApiError(Exception):
    """Internal exception for Google API errors."""

    def __init__(
        self,
        message: str,
        code: int | None = None,
        errors: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.errors = errors


def _random_id() -> str:
    """Generate a short random ID for cosmetic use (card widget IDs).

    NOT suitable for security-sensitive purposes (lock tokens, signatures, etc.)
    — use ``secrets.token_hex()`` for those. This uses ``random.choices`` because
    these IDs are only used as opaque Google Chat card widget suffixes and do not
    need to be unpredictable.
    """
    import random
    import string

    return "".join(random.choices(string.ascii_lowercase + string.digits, k=7))


def _parse_message_metadata(message: dict[str, Any]) -> Any:
    """Parse message metadata from a Google Chat message dict."""
    from chat_sdk.types import MessageMetadata

    create_time = message.get("createTime", "")
    date_sent: datetime
    if create_time:
        try:
            date_sent = _parse_iso(create_time)
        except (ValueError, AttributeError):
            date_sent = datetime.now(tz=timezone.utc)
    else:
        date_sent = datetime.now(tz=timezone.utc)

    return MessageMetadata(
        date_sent=date_sent,
        edited=False,
    )
