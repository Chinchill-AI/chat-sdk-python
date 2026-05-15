"""Teams adapter for chat SDK.

Uses the Microsoft Teams Bot Framework for message handling.
Supports messaging, adaptive cards, reactions, and typing indicators.

Python port of packages/adapter-teams/src/index.ts.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn
from urllib.parse import urlparse

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
    LockScope,
    Message,
    MessageMetadata,
    RawMessage,
    ReactionEvent,
    StreamOptions,
    ThreadInfo,
    WebhookOptions,
    _parse_iso,
)


class _TeamsStreamSession:
    """Bookkeeping for a single in-flight native streaming activity.

    Mirrors the upstream Teams SDK ``IStreamer`` surface (``emit``, ``canceled``)
    that ``streamViaEmit`` uses in ``packages/adapter-teams/src/index.ts``. The
    Python adapter constructs one of these per DM message-handler invocation,
    registers it in ``TeamsAdapter._active_streams`` so ``stream()`` can find
    it, and closes it after the handler completes.

    Carries the running ``stream_id`` (allocated by Teams on the first
    ``streaming`` activity) and an incrementing ``stream_sequence`` so the
    Bot Framework streaming protocol's wire shape stays valid.
    """

    __slots__ = ("stream_id", "sequence", "canceled", "first_chunk_id", "_text")

    def __init__(self) -> None:
        self.stream_id: str | None = None
        # Per Bot Framework streaming protocol: streamSequence starts at 1
        # for the first informative/streaming activity and increments by 1.
        self.sequence: int = 0
        self.canceled: bool = False
        # Captured from the first activity returned by the Bot Framework REST
        # API; this becomes the ``streamId`` for subsequent chunks and the
        # final message.
        self.first_chunk_id: str = ""
        self._text: str = ""

    def cancel(self) -> None:
        """Mark the session canceled. ``stream()`` checks this each chunk."""
        self.canceled = True

    @property
    def text(self) -> str:
        """Read-only view of the cumulative streamed text.

        External callers (tests, other adapter helpers) should read this
        instead of poking at the private ``_text`` attribute. Writes go
        through ``_stream_via_emit`` which owns the buffer.
        """
        return self._text


# Bot Framework streaming protocol values for ``channelData.streamType``.
_STREAM_TYPE_STREAMING = "streaming"
_STREAM_TYPE_FINAL = "final"

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

        if config.certificate is not None:
            # Exact parity with upstream adapter-teams/src/config.ts:13-18.
            # ``appPassword`` is referenced in camelCase to match upstream text.
            raise ValidationError(
                "teams",
                "Certificate-based authentication is not yet supported by the Teams SDK adapter. "
                "Use appPassword (client secret) or federated (workload identity) authentication instead.",
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

        # In-flight native streaming sessions, keyed by thread_id. Populated
        # by ``_handle_message_activity`` for DMs (which awaits the handler
        # so the session stays alive); consulted by ``stream()`` to decide
        # between native streaming via emit and the accumulate-and-post
        # fallback path.
        self._active_streams: dict[str, _TeamsStreamSession] = {}
        # Throttle for native DM streaming — Bot Framework streaming is
        # ~1 request/second; Microsoft recommends 1.5-2s buffering. See the
        # field docstring on TeamsAdapterConfig for full context.
        self._native_stream_min_emit_interval_ms: int = config.native_stream_min_emit_interval_ms
        # Monotonic-clock callable returning milliseconds since some epoch.
        # Injectable so tests can drive throttle behavior without real sleeps.
        # Default reads the running event loop's clock — matches what
        # ``asyncio.sleep`` would observe. The lazy lambda is intentional:
        # there is no running loop at ``__init__`` time.
        self._stream_clock_ms: Callable[[], float] = lambda: asyncio.get_running_loop().time() * 1000.0
        # Awaitable sleep keyed by milliseconds. Pairs with
        # ``_stream_clock_ms`` so the end-of-stream flush can honor the
        # throttle window without forcing real ``asyncio.sleep`` calls in
        # tests. Default = real ``asyncio.sleep``; tests substitute an
        # AsyncMock so they don't actually wait the configured interval.
        self._stream_sleep_ms: Callable[[float], Awaitable[None]] = lambda ms: asyncio.sleep(ms / 1000.0)

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
        """Handle message activities.

        For DMs we register a :class:`_TeamsStreamSession` and ``await`` the
        chat-handler task so :meth:`stream` can dispatch through the native
        Bot Framework streaming protocol while the session is live. Group
        chats remain fire-and-forget — Teams doesn't support native streaming
        in channels/group threads, so :meth:`stream` falls through to the
        accumulate-and-post path.

        Mirrors upstream ``handleMessageActivity`` in
        ``packages/adapter-teams/src/index.ts``: capture ``ctx.stream`` for
        DMs, block until processing completes, then drop the session.
        """
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

        if not self.is_dm(thread_id):
            # Group chat / channel — fire-and-forget. ``stream()`` will see no
            # active session and accumulate-and-post.
            self._chat.process_message(self, thread_id, message, options)
            return

        # DM path: register a streaming session, then block on the handler so
        # ``stream()`` can dispatch through the native streaming protocol
        # while the session stays alive. We chain a ``waitUntil`` shim on
        # top of the caller-supplied one (if any) so a hosting webhook
        # framework that respects ``waitUntil`` still gets the underlying
        # task — the local ``await`` is purely so we know when to reap the
        # session.
        session = _TeamsStreamSession()
        # Keyed by ``thread_id`` to match upstream ``activeStreams.set(threadId, …)``
        # in ``packages/chat-teams/src/index.ts``. Safe because the default
        # per-thread concurrency strategy in ``Chat.handle_incoming_message``
        # serialises DM handlers for the same thread (overlapping webhooks are
        # deduped or dropped before they reach a handler, so two ``stream()``
        # calls cannot share a session). A per-handler ``ContextVar`` would
        # decouple this from the concurrency strategy but would be a Python-only
        # divergence — tracked as a follow-up rather than landed inside the
        # parity sync.
        self._active_streams[thread_id] = session
        loop = asyncio.get_running_loop()
        processing_done: asyncio.Future[None] = loop.create_future()

        def _resolve_processing(task: Awaitable[Any]) -> None:
            # ``WebhookOptions.wait_until`` receives the chat task; we hook
            # done so we can release ``processing_done`` regardless of
            # success/failure (mirrors the upstream ``task.then(resolve,
            # resolve)`` pattern).
            if isinstance(task, asyncio.Task):

                def _on_done(_t: asyncio.Task[Any]) -> None:
                    if not processing_done.done():
                        processing_done.set_result(None)

                task.add_done_callback(_on_done)
            elif not processing_done.done():
                # Non-Task awaitables are uncommon on this path, but if we
                # ever get one we still need to unblock — resolve eagerly
                # so we don't deadlock the webhook handler.
                processing_done.set_result(None)

        upstream_wait_until = options.wait_until if options is not None else None
        # Track whether the chained wait_until fired synchronously during
        # ``process_message``. Used below to detect deduped/dropped
        # messages where no chat task was scheduled and we'd otherwise
        # hang on ``await processing_done``.
        wait_until_invoked = False

        def _chained_wait_until(task: Awaitable[Any]) -> None:
            nonlocal wait_until_invoked
            wait_until_invoked = True
            # Resolve our own gate FIRST, before invoking the upstream
            # ``wait_until`` callback. This way, even if the upstream
            # callback raises, blocks, or never fires, ``processing_done``
            # is still wired up — making the deadlock-immunity argument
            # trivially obvious: the await on ``processing_done`` below
            # cannot starve due to a misbehaving caller-supplied hook.
            _resolve_processing(task)
            if upstream_wait_until is not None:
                # Catch synchronous failures in the caller's hook. If we
                # let it escape, ``Chat.process_message`` propagates the
                # exception, the outer ``try`` skips ``await processing_done``,
                # and the ``finally`` tears down the session while the
                # underlying chat task is still scheduled — handlers that
                # later call ``thread.stream()`` would then miss native
                # streaming and fall back to a normal post. Logging keeps
                # the failure visible without breaking the streaming path.
                try:
                    upstream_wait_until(task)
                except Exception as exc:
                    self._logger.warn(
                        "Caller-supplied WebhookOptions.wait_until raised",
                        {"threadId": thread_id, "error": str(exc)},
                    )

        chained_options = WebhookOptions(wait_until=_chained_wait_until)

        try:
            self._chat.process_message(self, thread_id, message, chained_options)
            # If ``process_message`` returned without invoking
            # ``wait_until`` synchronously, no chat task was scheduled
            # (deduped, dropped by the concurrency strategy, or the
            # message wasn't admitted for handling). Resolve the gate
            # immediately so ``await processing_done`` doesn't hang
            # forever — there is no in-flight handler to wait on.
            # Note: we check ``wait_until_invoked`` rather than
            # ``processing_done.done()`` because the latter is set via
            # an ``add_done_callback`` on task COMPLETION; the task is
            # scheduled but has not run yet at this point.
            if not wait_until_invoked and not processing_done.done():
                processing_done.set_result(None)
            try:
                await processing_done
            except asyncio.CancelledError:
                # Caller cancelled the webhook handler — propagate cancel
                # into the streaming session so any in-flight ``stream()``
                # exits cleanly without sending more chunks.
                session.cancel()
                raise
        finally:
            # Always close the session — sending a final activity if any
            # chunks were emitted — and drop the registry entry so a
            # subsequent message can register fresh.
            current = self._active_streams.get(thread_id)
            if current is session:
                self._active_streams.pop(thread_id, None)
            try:
                await self._close_stream_session(thread_id, session)
            except Exception as exc:  # pragma: no cover — diagnostic-only
                self._logger.warn(
                    "Teams stream finalization failed",
                    {"threadId": thread_id, "error": str(exc)},
                )

    # Keys injected by the SDK's card renderer or Teams transport — not user input.
    _ACTION_TRANSPORT_KEYS = frozenset({"actionId", "msteams"})

    @staticmethod
    def _extract_action_values(action_data: dict[str, Any]) -> tuple[str, Any]:
        """Extract action ID and submitted values from a Teams action payload.

        Strips transport keys (``actionId``, ``msteams``) that are injected by
        the SDK's card renderer or Teams infrastructure and are not user input.

        For plain buttons: ``{"actionId": "btn", "value": "x"}`` → ``("btn", "x")``
        For ChoiceSet: ``{"actionId": "__auto_submit", "sel": "opt"}`` → ``("__auto_submit", {"sel": "opt"})``
        """
        action_id = action_data.get("actionId", "")
        submitted_values: Any = {k: v for k, v in action_data.items() if k not in TeamsAdapter._ACTION_TRANSPORT_KEYS}
        # Unwrap single "value" key for plain button backward compat
        if list(submitted_values.keys()) == ["value"]:
            submitted_values = submitted_values["value"]
        return action_id, submitted_values

    def _handle_message_action(
        self,
        activity: dict[str, Any],
        action_value: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle Action.Submit button clicks sent as message activities.

        For plain buttons, ``action_value`` looks like ``{"actionId": "btn_id", "value": "clicked"}``.
        For ChoiceSet (Select/RadioSelect) submissions, it looks like
        ``{"actionId": "__auto_submit", "my_select": "option_1"}``.
        In both cases, we pass the full dict (minus ``actionId``) as ``value``
        so handlers receive all submitted input values.
        """
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

        action_id, submitted_values = self._extract_action_values(action_value)

        from_user = activity.get("from", {})
        self._chat.process_action(
            ActionEvent(
                action_id=action_id,
                value=submitted_values,
                user=Author(
                    user_id=from_user.get("id", "unknown"),
                    user_name=from_user.get("name", "unknown"),
                    full_name=from_user.get("name", "unknown"),
                    is_bot=False,
                    is_me=False,
                ),
                message_id=activity.get("replyToId") or activity.get("id", ""),
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
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

        action_id, submitted_values = self._extract_action_values(action_data)

        from_user = activity.get("from", {})
        self._chat.process_action(
            ActionEvent(
                action_id=action_id,
                value=submitted_values,
                user=Author(
                    user_id=from_user.get("id", "unknown"),
                    user_name=from_user.get("name", "unknown"),
                    full_name=from_user.get("name", "unknown"),
                    is_bot=False,
                    is_me=False,
                ),
                message_id=activity.get("replyToId") or activity.get("id", ""),
                thread_id=thread_id,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
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
                    thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
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
                    thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
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
                date_sent=_parse_iso(activity["timestamp"])
                if activity.get("timestamp")
                else datetime.now(timezone.utc),
                edited=False,
            ),
            attachments=attachments,
        )

    def _create_attachment(self, att: dict[str, Any]) -> Attachment:
        """Create an Attachment from a Teams attachment dict."""
        content_type = att.get("contentType", "")
        att_type: Literal["audio", "file", "image", "video"] = "file"
        if content_type.startswith("image/"):
            att_type = "image"
        elif content_type.startswith("video/"):
            att_type = "video"
        elif content_type.startswith("audio/"):
            att_type = "audio"

        url = att.get("contentUrl")
        return Attachment(
            type=att_type,
            url=url,
            name=att.get("name"),
            mime_type=content_type or None,
            fetch_metadata={"url": url} if url else None,
            fetch_data=self._build_teams_fetch_data(url) if url else None,
        )

    @staticmethod
    def _is_trusted_teams_download_url(url: str) -> bool:
        """Gate Teams file downloads to Microsoft-owned hosts.

        After ``rehydrate_attachment`` reconstructs the fetch closure
        from serialized ``fetch_metadata``, the URL may have been
        tampered with.  We refuse to issue a direct GET unless the host
        is a known Microsoft/Graph download host.

        This is a Python-first divergence: upstream Teams adapter does
        not validate the URL.  See ``docs/UPSTREAM_SYNC.md`` Known
        Non-Parity.
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
        # Microsoft Graph / Bot Framework / SharePoint / Teams file hosts
        allowed_suffixes = (
            ".botframework.com",
            ".graph.microsoft.com",
            ".sharepoint.com",
            ".officeapps.live.com",
            ".office.com",
            ".office365.com",
            ".onedrive.com",
            ".microsoft.com",
        )
        if host.endswith(allowed_suffixes):
            return True
        # Exact-match traffic-manager / Graph / Teams service hosts
        return host in {
            "smba.trafficmanager.net",
            "graph.microsoft.com",
            "attachments.office.net",
        }

    def _build_teams_fetch_data(self, url: str) -> Callable[[], Awaitable[bytes]]:
        """Build a lazy ``fetch_data`` closure for a Teams file URL.

        Uses the adapter's shared ``aiohttp.ClientSession`` (via
        :meth:`_get_http_session`) so downloads reuse the connection
        pool instead of constructing a throwaway client per request.
        """

        async def fetch_data() -> bytes:
            if not self._is_trusted_teams_download_url(url):
                raise ValidationError(
                    "teams",
                    f"Refusing to fetch Teams file from untrusted URL: {url}",
                )
            session = await self._get_http_session()
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise NetworkError(
                        "teams",
                        f"Failed to fetch file: {resp.status}",
                    )
                return await resp.read()

        return fetch_data

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Teams attachment.

        Teams uses public file URLs (signed by the Graph API), so all we
        need to rebuild the download closure is the URL — either from
        ``fetch_metadata["url"]`` or the attachment's top-level ``url``.
        Returns the attachment unchanged when no URL is available.
        The URL host is validated inside the closure, so tampered URLs
        raise at fetch time.
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        meta_url = meta.get("url")
        url = meta_url if meta_url is not None else attachment.url
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
            fetch_data=self._build_teams_fetch_data(url),
            fetch_metadata=attachment.fetch_metadata,
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
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction (not supported by Teams Bot Framework API)."""
        self._logger.warn("addReaction is not supported by the Teams Bot Framework API")

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction (not supported by Teams Bot Framework API)."""
        self._logger.warn("removeReaction is not supported by the Teams Bot Framework API")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
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
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream responses to a Teams conversation.

        DMs use the Bot Framework streaming protocol via :meth:`_stream_via_emit`
        when an active streaming session exists (set up by
        :meth:`_handle_message_activity`). Group chats / channels accumulate
        the stream and post a single message — matching upstream's
        post-#416 behavior of avoiding the post+edit flicker where Teams
        doesn't support native streaming. See
        ``packages/adapter-teams/src/index.ts`` ``stream`` and
        ``streamViaEmit`` at upstream commit ``ed46bae``.
        """
        session = self._active_streams.get(thread_id)
        if session is not None and not session.canceled:
            return await self._stream_via_emit(thread_id, text_stream, session, options)

        # No native streamer (group chats, proactive messages, or DMs whose
        # session was already canceled). Accumulate and post once.
        accumulated = ""
        async for chunk in text_stream:
            text = ""
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                text = chunk.get("text", "")
            if not text:
                continue
            accumulated += text

        if not accumulated:
            return RawMessage(id="", thread_id=thread_id, raw={"text": ""})

        decoded = self.decode_thread_id(thread_id)
        activity_payload = {
            "type": "message",
            "text": accumulated,
            "textFormat": "markdown",
        }
        result = await self._teams_send(decoded, activity_payload)
        return RawMessage(
            id=result.get("id", ""),
            thread_id=thread_id,
            raw={"text": accumulated},
        )

    async def _stream_via_emit(
        self,
        thread_id: str,
        text_stream: Any,
        session: _TeamsStreamSession,
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Native Bot Framework streaming: typing chunks + final message.

        Wire format (per Bot Framework streaming protocol):

        - Each non-empty chunk is a ``typing`` activity with
          ``channelData = {streamType: "streaming", streamSequence: N,
          streamId?: <id>}`` and a parallel ``streaminfo`` entity. Per
          the Bot Framework streaming contract, ``streamId`` MUST appear
          on the ``streaminfo`` entity (not just ``channelData``) for
          subsequent and final activities; the first chunk omits it
          everywhere because the server hasn't assigned an id yet.
        - On stream completion, a final ``message`` activity is sent by
          :meth:`_close_stream_session` (it carries ``streamType: "final"``).

        Throttling: Teams' streaming endpoint enforces ~1 request/second
        and Microsoft recommends 1.5-2s buffering. We accumulate every
        non-empty chunk locally but only ship a ``typing`` activity once
        the emit interval has elapsed since the previous send; in-window
        chunks are coalesced into the next eligible emit. The interval
        defaults to ``TeamsAdapterConfig.native_stream_min_emit_interval_ms``
        (1500ms) and is overridden per-call by
        ``StreamOptions.update_interval_ms`` when provided.

        End-of-stream flush: when the iterator ends, any text that was
        buffered (coalesced into the throttle window) but never emitted
        as a ``typing`` activity is shipped now via one final forced
        ``typing`` emit before this method returns. Without that flush,
        buffered text would only ship in :meth:`_close_stream_session`'s
        final ``message`` activity — and if THAT send fails (429 / network
        blip), ``Thread.stream`` would have already built a ``SentMessage``
        from this method's return value containing text Teams never
        accepted (the chat handler returns and ``SentMessage`` is created
        before the close runs from the handler's finally block). With the
        flush, ``accumulated`` is confirmed-accepted by Teams before we
        return, so the close-path final ``message`` activity becomes a
        UI-clearing marker whose failure is a stale-streaming-UI cost
        rather than a recording inconsistency.

        We never emit a chunk after :attr:`_TeamsStreamSession.canceled` is
        set, and we surface stream-iterator and send exceptions to the
        caller (after canceling the session) so the close path won't post
        a final message that doesn't reflect what the user saw.
        """
        decoded = self.decode_thread_id(thread_id)
        accumulated = ""
        # The cumulative-text snapshot last confirmed-accepted by Teams
        # via a successful ``_teams_send``. After the loop we compare
        # against ``accumulated`` to decide whether the throttle window
        # buffered text that needs an end-of-stream flush.
        last_committed_text = ""

        emit_interval_ms: int = (
            options.update_interval_ms
            if options is not None and options.update_interval_ms is not None
            else self._native_stream_min_emit_interval_ms
        )
        # Tracks when the most recent successful emit landed, in the same
        # ms-since-arbitrary-epoch frame as ``self._stream_clock_ms()``.
        # ``-inf`` so the first chunk always passes the interval gate
        # regardless of what value the clock returns on its first call.
        last_emit_at_ms: float = float("-inf")

        try:
            async for chunk in text_stream:
                if session.canceled:
                    self._logger.debug("Teams stream canceled by user", {"threadId": thread_id})
                    break

                text = ""
                if isinstance(chunk, str):
                    text = chunk
                elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                    text = chunk.get("text", "")
                if not text:
                    continue

                # Always accumulate locally so the close-path final
                # ``message`` activity carries the full user-visible
                # text. The decision below only governs whether THIS
                # chunk triggers an intermediate ``typing`` send.
                accumulated += text

                now_ms = self._stream_clock_ms()
                if now_ms - last_emit_at_ms < emit_interval_ms:
                    # Inside the throttle window — coalesce. Buffered text
                    # ships in the next eligible emit, or in the
                    # end-of-stream flush below if the iterator ends
                    # before another window opens.
                    continue

                result = await self._emit_streaming_activity(
                    decoded=decoded,
                    thread_id=thread_id,
                    session=session,
                    text=accumulated,
                )
                last_committed_text = accumulated
                last_emit_at_ms = now_ms

                if session.stream_id is None:
                    chunk_id = result.get("id") or ""
                    session.first_chunk_id = chunk_id
                    if chunk_id:
                        session.stream_id = chunk_id
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit):
            # Control-flow exceptions: cancel the session so close() doesn't
            # post a final message, then re-raise so the caller's cancellation
            # propagates correctly. Hazard #5 — orphaned tasks here would
            # leave a half-finished streaming activity visible to the user.
            session.cancel()
            raise
        except Exception:
            # Iterator raised mid-stream. Cancel so the close path doesn't
            # ship a final message. The exception surfaces to the caller
            # (the chat handler), which will propagate to the message
            # processing task — same shape as the fallback path's
            # stream-exception capture, sized for native streaming.
            session.cancel()
            raise

        # End-of-stream flush — see method docstring for the data-corruption
        # rationale. Only runs when the iterator ended normally AND the
        # throttle window buffered text since the last successful emit.
        # ``session.canceled`` is checked because mid-stream cancellation
        # via ``session.cancel()`` (e.g. user-initiated abort) breaks the
        # loop above without an exception, and we shouldn't flush text the
        # user explicitly canceled out of.
        if not session.canceled and accumulated != last_committed_text:
            # Honor the throttle even for the end-of-stream flush — a fast
            # LLM stream that finishes inside the throttle window after the
            # last successful emit would 429 the Bot Framework streaming
            # endpoint otherwise (1 req/sec quota), cancelling the stream
            # mid-flight. Wait for the remaining window before shipping.
            elapsed_ms = self._stream_clock_ms() - last_emit_at_ms
            if elapsed_ms < emit_interval_ms:
                await self._stream_sleep_ms(emit_interval_ms - elapsed_ms)
            # Re-check cancellation after the wait — the chat handler can
            # call ``session.cancel()`` from another task while we sleep.
            # If we're canceled, skip the emit entirely; the bottom return
            # block will surface only ``last_committed_text`` so the
            # ``SentMessage`` matches what Teams actually shipped to the
            # user (not the buffered suffix the user explicitly canceled
            # out of). Same shape as in-loop cancellation.
            if not session.canceled:
                result = await self._emit_streaming_activity(
                    decoded=decoded,
                    thread_id=thread_id,
                    session=session,
                    text=accumulated,
                )
                last_committed_text = accumulated
                if session.stream_id is None:
                    chunk_id = result.get("id") or ""
                    session.first_chunk_id = chunk_id
                    if chunk_id:
                        session.stream_id = chunk_id

        # Pick the cumulative text that Teams actually accepted: when
        # canceled (in-loop break or during-wait cancellation), some
        # text may have been buffered locally but never shipped — return
        # only ``last_committed_text`` so ``Thread.stream``'s outer
        # accumulator records what the user actually saw. When the stream
        # ran to completion, ``last_committed_text`` and ``accumulated``
        # are equal (the flush above committed the final batch), so this
        # collapses to ``accumulated`` in the happy path.
        final_text = last_committed_text if session.canceled else accumulated
        # Persist accumulated text on the session so close() can emit the
        # final ``message`` activity with the same content the user saw.
        # Direct ``_text`` write is the canonical mutator (the public
        # ``text`` property is read-only by design); both classes live in
        # the same module so this isn't a cross-module SLF001.
        session._text = final_text  # noqa: SLF001
        return RawMessage(
            id=session.first_chunk_id,
            thread_id=thread_id,
            raw={"text": final_text},
        )

    async def _emit_streaming_activity(
        self,
        *,
        decoded: TeamsThreadId,
        thread_id: str,
        session: _TeamsStreamSession,
        text: str,
    ) -> dict[str, Any]:
        """Send one ``typing`` activity carrying the cumulative ``text`` snapshot.

        Increments ``session.sequence`` on success. Raises (after canceling
        the session and logging) if ``_teams_send`` fails — ``Thread.stream``
        accumulates each chunk locally BEFORE yielding to the adapter, so
        swallowing the failure here would let the SDK record a SentMessage
        / append a message-history entry containing text Teams never
        accepted. Returns the REST response dict on success so the caller
        can capture the server-assigned ``streamId`` for the first chunk.

        Hazard #7 — only include ``streamId`` once the server has assigned
        one. Sending ``"streamId": None`` (or ``""``) on the first chunk
        would cause Teams to reject the activity. The Bot Framework REST
        contract requires ``streamId`` on BOTH ``channelData`` and the
        ``streaminfo`` entity for subsequent activities; setting it only
        on ``channelData`` may cause Teams to detach the chunk from the
        initial stream.
        """
        next_sequence = session.sequence + 1
        channel_data: dict[str, Any] = {
            "streamType": _STREAM_TYPE_STREAMING,
            "streamSequence": next_sequence,
        }
        stream_info_entity: dict[str, Any] = {
            "type": "streaminfo",
            "streamType": _STREAM_TYPE_STREAMING,
            "streamSequence": next_sequence,
        }
        if session.stream_id is not None:
            channel_data["streamId"] = session.stream_id
            stream_info_entity["streamId"] = session.stream_id

        activity_payload: dict[str, Any] = {
            "type": "typing",
            "text": text,
            "channelData": channel_data,
            "entities": [stream_info_entity],
        }

        try:
            result = await self._teams_send(decoded, activity_payload)
        except Exception as exc:
            self._logger.warn(
                "Teams stream emit failed; canceling stream",
                {"threadId": thread_id, "error": str(exc)},
            )
            session.cancel()
            raise

        session.sequence = next_sequence
        return result

    async def _close_stream_session(
        self,
        thread_id: str,
        session: _TeamsStreamSession,
    ) -> None:
        """Send the final ``message`` activity to close out a stream.

        No-op if the session was canceled, or if no chunks were ever
        emitted (empty ``text``). Otherwise we send the final activity —
        even if the server never returned an ``id`` for the first chunk
        (i.e. ``stream_id`` is ``None``), in which case we omit
        ``streamId`` from ``channelData``. Mirrors upstream's looser
        check: as long as the user saw streamed text, ship the final
        ``message`` so the Teams streaming UI clears, instead of leaving
        it spinning until Teams times the session out client-side.
        """
        if session.canceled:
            return
        # ``text`` is the cumulative buffer; empty means nothing was ever
        # emitted (empty stream, or stream canceled before first send).
        if not session.text:
            return

        decoded = self.decode_thread_id(thread_id)
        channel_data: dict[str, Any] = {
            "streamType": _STREAM_TYPE_FINAL,
        }
        stream_info_entity: dict[str, Any] = {
            "type": "streaminfo",
            "streamType": _STREAM_TYPE_FINAL,
        }
        # Hazard #7 — only include ``streamId`` when we actually have one.
        # The Bot Framework REST response can return ``id=""`` even on a
        # 200, in which case ``stream_id`` stays ``None`` (see emit guard
        # in ``_stream_via_emit``); ship the final without a ``streamId``
        # rather than skipping the send. When present, ``streamId`` must
        # appear on BOTH ``channelData`` and the ``streaminfo`` entity
        # per the Bot Framework streaming contract for the final activity.
        if session.stream_id is not None:
            channel_data["streamId"] = session.stream_id
            stream_info_entity["streamId"] = session.stream_id

        final_activity: dict[str, Any] = {
            "type": "message",
            "text": session.text,
            "textFormat": "markdown",
            "channelData": channel_data,
            "entities": [stream_info_entity],
        }
        try:
            await self._teams_send(decoded, final_activity)
        except Exception as exc:
            # Logged at warn — by the time we get here, ``_stream_via_emit``
            # has already done an end-of-stream flush so every byte of
            # ``session.text`` was confirmed-accepted by Teams via a prior
            # ``typing`` activity. The user has seen the full text. The
            # final ``message`` activity exists to switch the streaming UI
            # from typing indicator to message bubble; if that send fails
            # the streaming UI may stay until Teams times the session out
            # client-side, but the recorded ``SentMessage`` and
            # ``_message_history`` entry still match what the user saw.
            self._logger.warn(
                "Teams stream final activity failed",
                {"threadId": thread_id, "error": str(exc)},
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
                    _parse_iso(msg["createdDateTime"]) if msg.get("createdDateTime") else datetime.now(timezone.utc)
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


def create_teams_adapter(config: TeamsAdapterConfig | None = None) -> TeamsAdapter:
    """Factory function to create a Teams adapter."""
    return TeamsAdapter(config)
