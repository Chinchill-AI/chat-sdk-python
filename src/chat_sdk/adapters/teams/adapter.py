"""Teams adapter for chat SDK.

Uses the Microsoft Teams Bot Framework for message handling.
Supports messaging, adaptive cards, reactions, and typing indicators.

Python port of packages/adapter-teams/src/index.ts.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, NoReturn, cast
from urllib.parse import quote, urlparse

if TYPE_CHECKING:
    from microsoft_teams.api import ConversationReference
    from microsoft_teams.apps import StreamerProtocol

from chat_sdk.adapters.teams.api import _is_loopback_emulator_url, is_trusted_teams_service_url
from chat_sdk.adapters.teams.attachments import (
    BOT_TOKEN_REFUSAL,
    TeamsAttachmentFetchers,
    _connector_origin,
    create_anonymous_attachment_fetch_data,
    create_teams_attachment,
    fetch_with_bot_token,
    rehydrate_teams_attachment,
)
from chat_sdk.adapters.teams.bridge import BridgeHttpAdapter
from chat_sdk.adapters.teams.cards import AUTO_SUBMIT_ACTION_ID, card_to_adaptive_card
from chat_sdk.adapters.teams.format_converter import TeamsFormatConverter
from chat_sdk.adapters.teams.types import (
    TeamsAdapterConfig,
    TeamsChannelContext,
    TeamsDmContext,
    TeamsGraphContext,
    TeamsThreadId,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import (
    extract_card,
    extract_files,
    is_thinking_chunk,
    maybe_render_thinking,
)
from chat_sdk.shared.buffer_utils import buffer_to_data_uri, to_buffer
from chat_sdk.shared.download import validate_attachment_url
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
    FileUpload,
    FormattedContent,
    ListThreadsOptions,
    ListThreadsResult,
    LockScope,
    Message,
    MessageMetadata,
    PostableMarkdown,
    RawMessage,
    ReactionEvent,
    StreamOptions,
    ThreadInfo,
    ThreadSummary,
    UserInfo,
    WebhookOptions,
    _parse_iso,
)

MESSAGEID_CAPTURE_PATTERN = re.compile(r"messageid=(\d+)")
MESSAGEID_STRIP_PATTERN = re.compile(r";messageid=\d+")
# AAD object IDs are GUIDs (8-4-4-4-12 hex). Used to gate ``aadObjectId``
# values from incoming activities before formatting them into Microsoft
# Graph chat IDs (vercel/chat#403). See ``_cache_user_context`` and
# ``_chat_id_from_context``.
_AAD_OBJECT_ID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
CACHE_TTL_MS = 30 * 24 * 60 * 60 * 1000  # 30 days
USER_INFO_CACHE_TTL_MS = 60 * 60 * 1000  # 1 hour
USER_INFO_NEGATIVE_CACHE_TTL_MS = 5 * 60 * 1000  # 5 minutes
# Cached when a Graph lookup fails, so tenants without Graph consent don't pay
# a failing network call on every message.
USER_INFO_NEGATIVE_SENTINEL = "unresolvable"
# Bound on the sender lookup awaited before dispatch (Python-only; see
# docs/UPSTREAM_SYNC.md). On expiry the message is dispatched without email.
INCOMING_USER_TIMEOUT_S = 5.0

# Strong references for fire-and-forget tasks (user-info cache writes) so the
# GC does not collect them mid-flight.
_background_tasks: set[asyncio.Task[Any]] = set()


def _pin_task(task: asyncio.Task[Any]) -> None:
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _first_present(*values: Any) -> str | None:
    """First value that is a non-empty string (upstream ``a ?? b``).

    Python-only: ``""`` counts as missing, so an empty ``mail`` still falls
    back to the user principal name.
    """
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _user_info_to_json(info: UserInfo) -> str:
    """Serialize ``info`` in upstream's camelCase ``UserInfo`` wire shape."""
    data: dict[str, Any] = {
        "userId": info.user_id,
        "userName": info.user_name,
        "fullName": info.full_name,
        "isBot": info.is_bot,
    }
    if info.email is not None:
        data["email"] = info.email
    if info.avatar_url is not None:
        data["avatarUrl"] = info.avatar_url
    return json.dumps(data)


def _user_info_from_json(raw: str, user_id: str) -> UserInfo | None:
    """Parse a cached ``UserInfo`` (any SDK's write), re-keyed to ``user_id``."""
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    user_name = data.get("userName")
    full_name = data.get("fullName")
    if not isinstance(user_name, str) or not isinstance(full_name, str):
        return None
    email = data.get("email")
    avatar_url = data.get("avatarUrl")
    return UserInfo(
        user_id=user_id,
        user_name=user_name,
        full_name=full_name,
        is_bot=data.get("isBot") is True,
        email=email if isinstance(email, str) else None,
        avatar_url=avatar_url if isinstance(avatar_url, str) else None,
    )


# Allowed Microsoft Bot Framework service URL patterns (SSRF protection).
# Covers commercial, GCC, GCCH, DoD, China (21Vianet) and sovereign cloud
# endpoints. A superset of upstream's ``TRUSTED_CONNECTOR_HOSTS`` (chat@4.40.0):
# the ``*.botframework.*`` / ``*.teams.microsoft.*`` wildcards are kept so no
# existing deployment regresses. Divergence from upstream — see
# docs/UPSTREAM_SYNC.md. Plain-``http`` loopback (the local Bot Framework
# Emulator) is accepted separately by ``_validate_service_url``.
# Scheme and host compare case-insensitively (upstream lowercases
# ``url.hostname``); ``re.ASCII`` keeps ``[a-z]`` from folding non-ASCII
# letters such as the Kelvin sign (U+212A) or long s (U+017F).
_SERVICE_URL_FLAGS = re.IGNORECASE | re.ASCII
ALLOWED_SERVICE_URL_PATTERNS = [
    re.compile(r"^https://smba\.trafficmanager\.net/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://msteams\.botframework\.azure\.cn/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.com/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://[a-z0-9.-]+\.botframework\.us/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.com/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://[a-z0-9.-]+\.teams\.microsoft\.us/", _SERVICE_URL_FLAGS),
    re.compile(r"^https://smba\.infra\.(gcc|gov|dod)\.teams\.microsoft\.(com|us)/", _SERVICE_URL_FLAGS),
]

_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# ``microsoft_teams.apps.token_manager.DEFAULT_TENANT_FOR_GRAPH_TOKEN``.
_GRAPH_DEFAULT_TENANT = "common"
_APP_ID_UNRESOLVED_MESSAGE = "appId has not been resolved. Ensure chat.initialize() has completed."


def _to_app_options(config: TeamsAdapterConfig) -> dict[str, Any]:
    """Convert :class:`TeamsAdapterConfig` (public API) to Teams SDK ``AppOptions``.

    Python port of ``packages/adapter-teams/src/config.ts`` ``toAppOptions``
    (synced to ``@chat-adapter/teams@4.30.0``). Maps our public
    ``app_id``/``app_password``/``app_tenant_id``/``federated``/``app_type``/
    ``api_url`` fields onto the SDK's ``client_id``/``client_secret``/
    ``tenant_id``/``managed_identity_client_id``/``service_url`` keys, reading
    the same ``TEAMS_*`` env-var fallbacks the legacy adapter used.

    Returns a dict suitable for ``App(**options)`` minus ``http_server_adapter``
    (the adapter injects the bridge separately). Only keys with a value are
    included so the SDK applies its own defaults for the rest.

    Certificate auth is rejected here for exact parity with upstream — the
    adapter constructor also guards it, but mirroring the check keeps the
    config conversion self-contained.

    A custom ``token`` factory is forwarded as the SDK ``token`` and no
    ``client_secret`` is emitted (neither the configured ``app_password`` nor
    ``TEAMS_APP_PASSWORD``). Upstream additionally passes ``clientSecret: ""``
    to suppress the SDK's ``CLIENT_SECRET`` env fallback; the Python SDK
    ignores an empty string there (``client_secret or os.getenv(...)``), so the
    adapter enforces the factory's precedence in the ``App`` subclass built by
    :func:`_token_precedence_app_class` instead.

    ``config.app_id`` must already be resolved: a callable resolver is
    replaced by its result (``dataclasses.replace``) before this is called.
    """
    if config.certificate is not None:
        raise ValidationError(
            "teams",
            "Certificate-based authentication is not yet supported by the Teams SDK adapter. "
            "Use appPassword (client secret), federated (workload identity), or token (custom token factory) "
            "authentication instead.",
        )
    if callable(config.app_id):
        raise TypeError("_to_app_options requires a resolved app_id; resolve the app_id callable first")

    client_id = config.app_id if config.app_id is not None else os.environ.get("TEAMS_APP_ID")
    # Federated (workload identity) auth derives the secret from a managed
    # identity, and a custom token factory mints tokens itself, so no client
    # secret is supplied in either mode.
    if config.federated is not None or config.token is not None:
        client_secret = None
    elif config.app_password is not None:
        client_secret = config.app_password
    else:
        client_secret = os.environ.get("TEAMS_APP_PASSWORD")

    # SingleTenant apps need a tenant_id; MultiTenant apps omit it.
    if config.app_type == "MultiTenant":
        tenant_id = None
    elif config.app_tenant_id is not None:
        tenant_id = config.app_tenant_id
    else:
        tenant_id = os.environ.get("TEAMS_APP_TENANT_ID")

    federated = config.federated
    if federated is not None and federated.get("client_audience") and config.logger is not None:
        config.logger.warn("federated.clientAudience is not supported by the Teams SDK and will be ignored.")

    managed_identity_client_id = federated.get("client_id") if federated is not None else None

    # Override the Bot Framework service URL for sovereign clouds (GCC-High,
    # DoD, etc.). Upstream config.ts:38 — ``config.apiUrl ?? TEAMS_API_URL`` —
    # is fed to the SDK ``AppOptions.serviceUrl``.
    service_url = config.api_url if config.api_url is not None else os.environ.get("TEAMS_API_URL")

    options: dict[str, Any] = {}
    if client_id:
        options["client_id"] = client_id
    if client_secret:
        options["client_secret"] = client_secret
    if tenant_id:
        options["tenant_id"] = tenant_id
    if managed_identity_client_id:
        options["managed_identity_client_id"] = managed_identity_client_id
    if config.token is not None:
        options["token"] = config.token
    if service_url:
        options["service_url"] = service_url
    return options


_TOKEN_PRECEDENCE_APP_CLASS: type | None = None


def _token_precedence_app_class() -> type:
    """Return (building once) the SDK ``App`` subclass that lets ``token`` win.

    Divergence from upstream — see docs/UPSTREAM_SYNC.md. The Python Teams SDK's
    ``App._init_credentials`` (``microsoft-teams-apps`` 2.0.13 – 2.1.x) reads
    ``options.client_secret or os.getenv("CLIENT_SECRET")`` and checks
    ``client_id and client_secret`` *before* ``client_id and token``, so a
    stray ``CLIENT_SECRET`` in the environment would silently beat a configured
    token factory (upstream's ``clientSecret: ""`` trick relies on JS ``??``
    semantics and does not carry over). This subclass returns
    ``TokenCredentials`` whenever a ``token`` factory and a client id are
    present, and defers to the SDK otherwise. Built lazily because the SDK is
    the optional ``[teams]`` extra.
    """
    global _TOKEN_PRECEDENCE_APP_CLASS
    if _TOKEN_PRECEDENCE_APP_CLASS is None:
        from microsoft_teams.api import TokenCredentials
        from microsoft_teams.apps import App

        class _TokenPrecedenceApp(App):
            def _init_credentials(self) -> Any:
                token = self.options.token
                client_id = self.options.client_id or os.getenv("CLIENT_ID")
                if token is not None and client_id:
                    tenant_id = self.options.tenant_id or os.getenv("TENANT_ID")
                    return TokenCredentials(client_id=client_id, tenant_id=tenant_id, token=token)
                return super()._init_credentials()

        _TOKEN_PRECEDENCE_APP_CLASS = _TokenPrecedenceApp
    return _TOKEN_PRECEDENCE_APP_CLASS


def _sdk_skip_auth_option() -> str:
    """Name of the SDK ``AppOptions`` flag that disables inbound JWT validation.

    ``microsoft-teams-apps`` 2.0.14+ renamed ``skip_auth`` to
    ``dangerously_allow_unauthenticated_requests`` (the old name still works
    but emits a ``DeprecationWarning``); feature-detect so both ends of the
    supported ``>=2.0.13,<2.2`` range get the right key.
    """
    from microsoft_teams.apps.options import AppOptions

    keys = getattr(AppOptions, "__annotations__", {})
    if "dangerously_allow_unauthenticated_requests" in keys:
        return "dangerously_allow_unauthenticated_requests"
    return "skip_auth"


def _validate_service_url(url: str) -> None:
    """Validate that a service URL matches known Microsoft Bot Framework endpoints.

    Raises :class:`~chat_sdk.shared.errors.ValidationError` if the URL is not
    in the allow-list, preventing SSRF attacks via crafted ``serviceUrl`` values.
    Plain-``http`` loopback (``localhost`` / ``127.x.x.x`` / ``::1``) is accepted
    for the local Bot Framework Emulator, as upstream does.
    """
    for pattern in ALLOWED_SERVICE_URL_PATTERNS:
        if pattern.match(url):
            return
    if _is_loopback_emulator_url(url):
        return
    raise ValidationError(
        "teams",
        f"Service URL is not an allowed Bot Framework endpoint: {url}",
    )


def _error_field(error: Any, dict_key: str, attr_name: str) -> Any:
    """Read a field from a Teams error that may be a dict or an SDK exception.

    The hand-rolled Graph / outbound aiohttp path raises plain dicts
    (``{"statusCode": 429, ...}``); the Microsoft Teams SDK raises typed
    exceptions whose HTTP details live on attributes (``status_code``,
    ``retry_after``, ``inner_http_error``). This reads ``dict_key`` from a
    dict error or ``attr_name`` from an object error so :func:`_handle_teams_error`
    maps both shapes to our taxonomy with one code path.
    """
    if isinstance(error, dict):
        return error.get(dict_key)
    return getattr(error, attr_name, None)


def _handle_teams_error(error: Any, operation: str) -> NoReturn:
    """Convert Teams SDK / Bot Framework errors to adapter errors and raise.

    Python port of ``packages/adapter-teams/src/errors.ts`` ``handleTeamsError``
    (synced to ``@chat-adapter/teams@4.30.0``). Maps the error onto our taxonomy:

    - ``401`` → :class:`AuthenticationError`
    - ``403`` (or a "permission" message) → :class:`AdapterPermissionError`
    - ``404`` → :class:`NetworkError` ("not found")
    - ``429`` → :class:`AdapterRateLimitError` (with ``retry_after`` when present)
    - any other message → :class:`NetworkError`

    Handles both the plain-dict errors raised by the still-hand-rolled Graph /
    outbound path and the typed exceptions raised by the Microsoft Teams SDK
    (whose HTTP status lives on ``inner_http_error.status_code`` /
    ``status_code`` / ``status`` / ``code``).
    """
    if error is not None and isinstance(error, (dict, Exception)):
        # SDK ``HttpError`` shape exposes the upstream status on
        # ``inner_http_error.status_code``; dict errors use ``innerHttpError``.
        inner = _error_field(error, "innerHttpError", "inner_http_error")
        inner_status = _error_field(inner, "statusCode", "status_code") if inner is not None else None
        status_code = (
            inner_status
            if inner_status is not None
            else (
                _error_field(error, "statusCode", "status_code")
                or _error_field(error, "status", "status")
                or _error_field(error, "code", "code")
            )
        )

        if isinstance(status_code, str) and status_code.isdigit():
            status_code = int(status_code)

        message = error.get("message") if isinstance(error, dict) else str(error)

        if status_code == 401:
            raise AuthenticationError(
                "teams",
                f"Authentication failed for {operation}: {message or 'unauthorized'}",
            )
        if status_code == 403 or (isinstance(message, str) and "permission" in message.lower()):
            raise AdapterPermissionError("teams", operation)
        if status_code == 404:
            raise NetworkError(
                "teams",
                f"Resource not found during {operation}: conversation or message may no longer exist",
            )
        if status_code == 429:
            retry_after_raw = _error_field(error, "retryAfter", "retry_after")
            retry_after = retry_after_raw if isinstance(retry_after_raw, (int, float)) else None
            raise AdapterRateLimitError("teams", retry_after)
        if isinstance(message, str) and message and isinstance(error, dict):
            raise NetworkError(
                "teams",
                f"Teams API error during {operation}: {message}",
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
        # Snapshot the config (shallow copy, like upstream's ``{ ...config }``)
        # so later mutation of the caller's object — e.g. assigning a
        # ``webhook_verifier`` after construction — cannot change the
        # authentication captured here or at lazy ``initialize()`` time.
        config = dataclasses.replace(config)

        self._name = "teams"
        self._config = config
        self._logger: Logger = config.logger or ConsoleLogger("info", prefix="teams")
        self._user_name = config.user_name or "bot"
        self._chat: ChatInstance | None = None
        self._format_converter = TeamsFormatConverter()

        # A callable ``app_id`` is resolved (once) in ``initialize()``; until
        # then the id is empty, ``bot_user_id`` is ``None`` and the SDK ``App``
        # does not exist (accessing ``_app`` raises ``ValidationError``).
        self._app_id: str = "" if callable(config.app_id) else (config.app_id or os.environ.get("TEAMS_APP_ID", ""))
        self._app_password = config.app_password or os.environ.get("TEAMS_APP_PASSWORD", "")
        self._app_tenant_id = config.app_tenant_id or os.environ.get("TEAMS_APP_TENANT_ID", "")

        if config.certificate is not None:
            # Exact parity with upstream adapter-teams/src/config.ts:13-18.
            # ``appPassword`` is referenced in camelCase to match upstream text.
            raise ValidationError(
                "teams",
                "Certificate-based authentication is not yet supported by the Teams SDK adapter. "
                "Use appPassword (client secret), federated (workload identity), or token (custom token factory) "
                "authentication instead.",
            )

        if not self._app_id and not callable(config.app_id) and config.webhook_verifier is None:
            self._logger.warn(
                "Teams app_id is empty — webhook verification will reject all incoming requests. "
                "Set TEAMS_APP_ID or pass app_id in config."
            )
        # Bot Framework token cache (scope ``api.botframework.com``). Owned by
        # ``_get_access_token`` and consumed only by the still-hand-rolled
        # ``open_dm`` REST call (the SDK ``App`` does not expose a 1:1
        # conversation-create helper). The SDK ``App`` mints its own Bot
        # Framework token for the migrated outbound send/edit/delete/typing
        # paths and for native streaming, so those no longer touch this field.
        self._access_token: str | None = None
        self._token_expiry: float = 0
        # Microsoft Graph token cache (scope ``graph.microsoft.com``). Kept on
        # DEDICATED fields so it can never collide with the Bot Framework token
        # above — the two have different scopes, and sharing one cache caused
        # last-writer-wins corruption (issue #93). Owned by ``_get_graph_token``
        # and consumed only by the hand-rolled Graph reads.
        self._graph_token: str | None = None
        self._graph_token_expiry: float = 0
        self._token_lock = asyncio.Lock()

        # Microsoft Teams SDK ``App`` — owns inbound JWT validation and
        # activity routing (issue #93 PR 1). The ``BridgeHttpAdapter`` captures
        # the route handler the App registers during ``app.initialize()`` so
        # ``handle_webhook`` can dispatch serverless webhooks through it. The
        # SDK is an optional ([teams] extra) dependency, so it is imported
        # lazily here rather than at module scope.
        #
        # With a ``webhook_verifier`` the bridge verifies every request before
        # it reaches the SDK handler, and the App is built with the SDK's
        # skip-auth flag. With a callable ``app_id`` the App is built in
        # ``initialize()`` once the id is resolved (upstream ``createApp``).
        self._bridge = BridgeHttpAdapter(
            self._logger,
            webhook_verifier=config.webhook_verifier,
            reject_before_auth=self._rejects_before_auth,
        )
        self._app_instance: Any | None = None
        if not callable(config.app_id):
            self._app_instance = self._build_app(config)
        # One shared in-flight ``initialize()`` task (cleared on failure so a
        # later call retries) and a guard so SDK handlers register only once.
        self._initialization: asyncio.Task[None] | None = None
        self._handlers_registered = False

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        # In-flight native streaming sessions, keyed by thread_id. Each value
        # is the Teams SDK ``IStreamer`` (``StreamerProtocol``) captured from
        # the inbound DM activity context. Populated by
        # ``_handle_message_activity`` for DMs (which awaits the handler so the
        # streamer stays alive); consulted by ``stream()`` to decide between
        # native streaming via ``emit()`` and the accumulate-and-post fallback.
        self._active_streams: dict[str, StreamerProtocol] = {}

    def _build_app(self, config: TeamsAdapterConfig) -> Any:
        """Construct the Microsoft Teams SDK ``App`` for this adapter.

        Lazy-imports ``microsoft_teams.apps`` (the optional ``[teams]`` extra),
        maps :class:`TeamsAdapterConfig` onto SDK ``AppOptions`` via
        :func:`_to_app_options`, injects the :class:`BridgeHttpAdapter`, and
        stamps the ``User-Agent: Vercel.ChatSDK`` client header — matching
        upstream ``adapter-teams/src/index.ts`` App construction.

        The SDK's ``App`` enforces inbound JWT validation by default; when
        ``client_id`` is configured it builds a Bot Framework
        ``TokenValidator`` (RS256, audience = ``app_id`` + ``api://`` variants,
        Bot Framework issuer + JWKS). Only when a ``webhook_verifier`` is
        configured do we pass the SDK skip-auth flag — the bridge then verifies
        every request itself before the SDK handler runs (upstream
        ``skipAuth: Boolean(config.webhookVerifier)``).

        ``config.app_id`` must already be a string (or ``None``); the lazy path
        passes a copy with the resolved id.
        """
        try:
            from microsoft_teams.common import ClientOptions
        except ImportError as exc:  # pragma: no cover - exercised via packaging
            raise ImportError(
                "The Teams adapter requires the 'teams' extra. Install it with: pip install 'chat-sdk[teams]'"
            ) from exc

        options = _to_app_options(config)
        if config.webhook_verifier is not None:
            options[_sdk_skip_auth_option()] = True
        app_class = _token_precedence_app_class()
        return app_class(
            **options,
            client=ClientOptions(headers={"User-Agent": "Vercel.ChatSDK"}),
            http_server_adapter=self._bridge,
        )

    @property
    def _app(self) -> Any:
        """The Microsoft Teams SDK ``App``.

        Raises :class:`ValidationError` while a callable ``app_id`` is still
        unresolved (upstream's ``app`` getter), so an outbound call made before
        ``chat.initialize()`` fails with an actionable message.
        """
        if self._app_instance is None:
            raise ValidationError("teams", _APP_ID_UNRESOLVED_MESSAGE)
        return self._app_instance

    @property
    def name(self) -> str:
        return self._name

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def bot_user_id(self) -> str | None:
        # Derived from the (possibly lazily resolved) app id; ``None`` until a
        # callable ``app_id`` has been resolved. The bare-id format is tracked
        # separately (#217).
        return self._app_id or None

    @property
    def lock_scope(self) -> LockScope | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and the underlying Teams SDK ``App``.

        Mirrors upstream ``TeamsAdapter.initialize`` / ``initializeApp``: set
        ``chat``, then (once) resolve a callable ``app_id`` and build the App,
        register the event handlers, and ``await app.initialize()``.
        ``app.initialize()`` registers the messaging-endpoint route with the
        :class:`BridgeHttpAdapter` (so :meth:`handle_webhook` can dispatch) and
        configures inbound JWT validation. We then point the SDK's
        ``server.on_request`` at :meth:`_dispatch_activity` so JWT-validated
        activities route to our chat-processing handlers.

        Concurrent calls share one in-flight task. A failed attempt is not
        cached: the next call retries, without re-resolving an already
        resolved ``app_id`` or re-registering handlers. The shared task is
        shielded so cancelling one caller does not cancel it for the others.
        """
        self._chat = chat
        task = self._initialization
        if task is None:
            task = asyncio.get_running_loop().create_task(self._initialize_app())
            self._initialization = task
            task.add_done_callback(self._clear_failed_initialization)
        await asyncio.shield(task)

    def _clear_failed_initialization(self, task: asyncio.Task[None]) -> None:
        """Forget a failed/cancelled initialization so the next call retries."""
        if self._initialization is task and (task.cancelled() or task.exception() is not None):
            self._initialization = None

    async def _initialize_app(self) -> None:
        if self._app_instance is None:
            resolver = self._config.app_id
            if not callable(resolver):  # pragma: no cover - constructor builds the App eagerly
                raise ValidationError("teams", _APP_ID_UNRESOLVED_MESSAGE)
            resolved: Any = resolver()
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if not isinstance(resolved, str) or not resolved.strip():
                raise ValidationError("teams", "appId resolver must return a nonempty string.")
            self._app_instance = self._build_app(dataclasses.replace(self._config, app_id=resolved))
            self._app_id = resolved
        if not self._handlers_registered:
            self._register_event_handlers()
            self._handlers_registered = True
        app = self._app
        await app.initialize()
        # The SDK's ``HttpServer`` invokes ``on_request`` *after* JWT
        # validation. The default callback runs the SDK's strict typed
        # router + a live user-token fetch; we replace it with our own
        # dispatcher that routes the (already-authenticated) activity to
        # the registered handlers using the adapter's existing logic.
        app.server.on_request = self._dispatch_activity
        self._logger.info("Teams adapter initialized")

    def _register_event_handlers(self) -> None:
        """Register inbound handlers on the Teams SDK ``App``.

        Registers the handlers via the SDK decorators so the App is aware of
        them (parity with upstream ``registerEventHandlers``). The actual
        dispatch happens through :meth:`_dispatch_activity` (wired in
        :meth:`initialize`), which routes by activity type to the same handler
        coroutines — keeping the chat-processing logic identical to the
        pre-migration adapter while sourcing data from the SDK activity.
        """
        self._app.on_message(self._on_sdk_message)
        self._app.on_message_reaction(self._on_sdk_message_reaction)
        self._app.on_card_action(self._on_sdk_card_action)
        self._app.on_dialog_open(self._on_sdk_dialog_open)
        self._app.on_dialog_submit(self._on_sdk_dialog_submit)
        self._app.on_conversation_update(self._on_sdk_conversation_update)
        self._app.on_install_add(self._on_sdk_install)
        self._app.on_install_remove(self._on_sdk_install)

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up a Teams user via Microsoft Graph ``GET /users/{id}``.

        Teams Bot Framework user IDs (``29:...``) are not directly usable
        by Graph — Graph needs the tenant-scoped AAD object ID. We cache
        the ``aadObjectId`` from each inbound activity in
        :meth:`_cache_user_context`, so this call only succeeds for users
        that have interacted with the bot since the cache TTL.

        Returns ``None`` when the user has never interacted (no cached
        ``aadObjectId``), the chat instance isn't initialized, or the
        Graph API call fails. Requires the ``User.Read.All`` application
        permission on the bot's app registration.

        Results are cached under ``teams:userInfo:{aadObjectId}`` for an
        hour; a failed lookup caches ``"unresolvable"`` for five minutes so
        tenants without Graph consent don't retry on every message.
        ``email`` is ``mail``, else ``userPrincipalName``.

        Mirrors upstream ``TeamsAdapter.getUser`` (vercel/chat#404, #711,
        #708).
        """
        if not self._chat:
            return None
        try:
            aad_object_id = await self._chat.get_state().get(f"teams:aadObjectId:{user_id}")
        except Exception as error:
            self._logger.warn(
                "Failed to read cached aadObjectId from state",
                {"userId": user_id, "error": str(error)},
            )
            return None
        if not aad_object_id or not isinstance(aad_object_id, str):
            self._logger.debug("No cached aadObjectId for user", {"userId": user_id})
            return None
        return await self._get_user_by_aad_object_id(user_id, aad_object_id)

    async def _get_user_by_aad_object_id(self, user_id: str, aad_object_id: str) -> UserInfo | None:
        """Graph ``GET /users/{id}`` with the shared ``teams:userInfo`` cache."""
        # Defense in depth: aadObjectId came from a webhook so it's already
        # platform-trusted, but reject obvious junk before issuing a Graph
        # call (avoids URL injection if the cache is ever populated from
        # an attacker-controlled path). Reject the structural splitters
        # that change URL semantics outright (`/`, `?`, `#`), then
        # percent-encode the remainder via `quote(safe="")` (matches
        # Discord's pattern) so whitespace, `\\`, `;`, etc. cannot escape
        # the `/users/{id}` path segment.
        aad_str = aad_object_id
        if not aad_str or "/" in aad_str or "?" in aad_str or "#" in aad_str:
            return None
        cache_key = f"teams:userInfo:{aad_str}"
        cached = await self._read_cached_user_info(cache_key, user_id)
        if cached == USER_INFO_NEGATIVE_SENTINEL:
            return None
        if isinstance(cached, UserInfo):
            return cached
        try:
            token = await self._get_graph_token()
            session = await self._get_http_session()
            url = f"https://graph.microsoft.com/v1.0/users/{quote(aad_str, safe='')}"
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            ) as response:
                if not response.ok:
                    raise NetworkError("teams", f"Graph user lookup failed: {response.status}")
                graph_user = await response.json()
            if not isinstance(graph_user, dict):
                raise NetworkError("teams", "Graph user lookup returned no user")
        except Exception as error:
            self._logger.warn(
                "Failed to fetch user info from Graph API",
                {"userId": user_id, "error": str(error)},
            )
            self._cache_user_info_later(cache_key, USER_INFO_NEGATIVE_SENTINEL, USER_INFO_NEGATIVE_CACHE_TTL_MS)
            return None
        display_name = _first_present(graph_user.get("displayName"))
        user_principal = _first_present(graph_user.get("userPrincipalName"))
        user_info = UserInfo(
            user_id=user_id,
            user_name=_first_present(user_principal, display_name, user_id) or user_id,
            full_name=display_name if display_name is not None else aad_str,
            is_bot=False,
            email=_first_present(graph_user.get("mail"), user_principal),
            avatar_url=None,
        )
        self._cache_user_info_later(cache_key, _user_info_to_json(user_info), USER_INFO_CACHE_TTL_MS)
        return user_info

    async def _get_incoming_user(
        self,
        activity: dict[str, Any],
        user_id: str,
        aad_object_id: str,
    ) -> UserInfo | None:
        """Look up the sender through the Bot Framework conversation-members API.

        Port of upstream ``getIncomingUser`` (vercel/chat#860): no Graph
        permission needed. Reads the shared ``teams:userInfo`` cache (a
        negative sentinel left by a failed Graph lookup is ignored), else asks
        the activity's own connector for the member. A success is cached for
        an hour; a failure only logs, so the next message retries.
        """
        cache_key = f"teams:userInfo:{aad_object_id}"
        cached = await self._read_cached_user_info(cache_key, user_id)
        if isinstance(cached, UserInfo):
            return cached
        try:
            member = await self._get_conversation_member(activity, user_id)
            name = _first_present(getattr(member, "name", None))
            email = _first_present(getattr(member, "email", None))
            user_principal = _first_present(getattr(member, "user_principal_name", None))
            user_info = UserInfo(
                user_id=user_id,
                user_name=_first_present(user_principal, email, name, user_id) or user_id,
                full_name=name if name is not None else aad_object_id,
                is_bot=False,
                email=_first_present(email, user_principal),
                avatar_url=None,
            )
        except Exception as error:
            self._logger.warn(
                "Failed to fetch user info from Teams conversation members API",
                {"userId": user_id, "error": str(error)},
            )
            return None
        self._cache_user_info_later(cache_key, _user_info_to_json(user_info), USER_INFO_CACHE_TTL_MS)
        return user_info

    async def _get_conversation_member(self, activity: dict[str, Any], user_id: str) -> Any:
        """Fetch ``user_id``'s ``TeamsChannelAccount`` from the activity's connector.

        Upstream calls ``ctx.api.conversations.getMemberById`` on the client
        the SDK scoped to the activity's service URL. We scope with
        ``App.api.from_service_url`` (``microsoft-teams-apps`` 2.1+); 2.0.x
        has no scoping, so the shared ``App.api`` is used (a lookup for
        another region then fails and the message has no email).
        """
        service_url = activity.get("serviceUrl")
        conversation_id = (activity.get("conversation") or {}).get("id")
        if not isinstance(service_url, str) or not isinstance(conversation_id, str) or not conversation_id:
            raise ValidationError("teams", "Activity has no serviceUrl or conversation id")
        _validate_service_url(service_url)
        api = self._app.api
        from_service_url = getattr(api, "from_service_url", None)
        if callable(from_service_url):
            api = from_service_url(service_url.rstrip("/"))
        conversations = api.conversations
        get_member_by_id: Any = getattr(conversations, "get_member_by_id", None)
        if get_member_by_id is not None:
            return await get_member_by_id(conversation_id, user_id)
        # microsoft-teams-api < 2.0.16 only has the grouped accessor.
        return await conversations.members(conversation_id).get(user_id)

    async def _read_cached_user_info(self, cache_key: str, user_id: str) -> UserInfo | str | None:
        """Return the cached ``UserInfo``, the negative sentinel, or ``None``."""
        if not self._chat:
            return None
        try:
            cached = await self._chat.get_state().get(cache_key)
        except Exception:
            return None
        if not cached or not isinstance(cached, str):
            return None
        if cached == USER_INFO_NEGATIVE_SENTINEL:
            return USER_INFO_NEGATIVE_SENTINEL
        return _user_info_from_json(cached, user_id)

    def _cache_user_info_later(self, cache_key: str, value: str, ttl_ms: int) -> None:
        """Write a ``teams:userInfo`` entry without waiting (upstream ``.catch(() => {})``)."""
        if not self._chat:
            return
        state = self._chat.get_state()

        async def write() -> None:
            try:
                await state.set(cache_key, value, ttl_ms)
            except Exception as error:
                self._logger.debug("Failed to cache Teams user info", {"error": str(error)})

        _pin_task(asyncio.get_running_loop().create_task(write()))

    async def _hydrate_author_email(self, activity: dict[str, Any], message: Message) -> None:
        """Set ``message.author.email`` from the sender lookup (upstream ``handleMessageActivity``).

        Senders with ``from.aadObjectId`` use the conversation-members API;
        others fall back to :meth:`get_user`. Never raises: the lookup is
        bounded by ``INCOMING_USER_TIMEOUT_S`` (Python-only) and on any
        failure the message is dispatched without email.
        """
        aad_object_id = (activity.get("from") or {}).get("aadObjectId")
        user_id = message.author.user_id
        try:
            async with asyncio.timeout(INCOMING_USER_TIMEOUT_S):
                if isinstance(aad_object_id, str) and aad_object_id:
                    user = await self._get_incoming_user(activity, user_id, aad_object_id)
                else:
                    user = await self.get_user(user_id)
        except Exception as error:
            self._logger.warn(
                "Failed to look up the Teams message sender",
                {"userId": user_id, "error": str(error) or type(error).__name__},
            )
            return
        if user is not None and user.email:
            message.author.email = user.email

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle an incoming webhook from Teams Bot Framework.

        Delegates to the :class:`BridgeHttpAdapter`, which feeds the request
        through the Microsoft Teams SDK route handler: the SDK validates the
        inbound JWT (RS256 + Bot Framework audience/issuer/JWKS) and routes the
        activity to our handlers via :meth:`_dispatch_activity`. Returns the
        framework-agnostic ``{body, status, headers}`` dict our consumers
        expect.

        Mirrors upstream ``TeamsAdapter.handleWebhook`` (which is a one-line
        delegate to ``bridgeAdapter.dispatch``).
        """
        return await self._bridge.dispatch(request, options)

    async def _dispatch_activity(self, event: Any) -> Any:
        """Route a JWT-validated activity to the adapter's chat-processing logic.

        Installed as the Teams SDK ``HttpServer.on_request`` callback in
        :meth:`initialize`. By the time this runs the SDK has already validated
        the inbound JWT. ``event.body`` is the SDK's lenient ``CoreActivity``;
        we dump it back to the camelCase activity dict our handlers consume so
        the routing logic stays identical to the pre-migration adapter while
        the data now flows from the SDK-parsed activity.

        Returns an ``InvokeResponse``-shaped dict the SDK's HttpServer maps to
        the HTTP response. Card actions (``invoke``) return the Bot Framework
        invoke acknowledgement; everything else returns ``200`` with no body.
        """
        if not self._is_bot_framework_token(getattr(event, "token", None)):
            return {"status": 401, "body": {"error": "Unauthorized"}}

        activity = self._activity_to_dict(event)
        activity_type = activity.get("type", "")
        self._logger.debug("Teams activity received", {"type": activity_type})

        # Cache user context from activity metadata (serviceUrl / tenantId /
        # aadObjectId / channel + DM context) — unchanged from upstream.
        await self._cache_user_context(activity)

        options = self._bridge.get_webhook_options(activity.get("id"))

        if activity_type == "message":
            await self._handle_message_activity(activity, options)
        elif activity_type == "messageReaction":
            self._handle_reaction_activity(activity, options)
        elif activity_type == "invoke":
            # Adaptive card actions (Action.Execute → invoke). Upstream's
            # ``card.action`` handler acknowledges every ``adaptiveCard/action``
            # invoke and only routes the ones carrying an ``actionId``.
            action_data = (activity.get("value") or {}).get("action", {}).get("data", {})
            has_action_id = isinstance(action_data, dict) and bool(action_data.get("actionId"))
            is_card_action = activity.get("name") == "adaptiveCard/action"
            if has_action_id:
                await self._handle_adaptive_card_action(activity, action_data, options)
            elif is_card_action:
                self._logger.debug("Adaptive card action missing actionId", {"value": activity.get("value")})
            if has_action_id or is_card_action:
                return {
                    "status": 200,
                    "body": {
                        "statusCode": 200,
                        "type": "application/vnd.microsoft.activity.message",
                        "value": "",
                    },
                }

        return {"status": 200, "body": None}

    def _rejects_before_auth(self, headers: dict[str, str]) -> bool:
        """Return ``True`` to answer 401 before the SDK's JWT validator runs.

        ``microsoft-teams-apps`` 2.0.x validates inbound activities against the
        Bot Framework issuer only (``TokenValidator.for_service``). 2.1.x
        switched to ``InboundActivityTokenValidator``, which picks its branch
        from the token's *unverified* ``iss``. An Entra-looking issuer
        (``login.microsoftonline.com/...`` or ``sts.windows.net/...``) sends it
        down the Agent 365 "Agent ID" branch: it builds a per-``tid`` Entra
        validator and fetches that tenant's JWKS (a blocking HTTP call), and it
        accepts any tenant's Entra token whose audience is our app id, without
        the Bot Framework ``serviceurl`` claim check. This adapter does not
        support Agent ID activities, so lifting the ``<2.1`` cap (#250) must
        not widen who can deliver activities, nor let an unauthenticated
        request pick which JWKS the bot fetches.

        We therefore read the Bearer token's unverified ``iss`` here and reject
        anything but the cloud's Bot Framework issuer. A token that passes still
        goes through the SDK's full validation (signature, audience, expiry,
        ``serviceurl``), so this only narrows. On 2.0.x the SDK would reject
        the same tokens after fetching the fixed Bot Framework JWKS.

        Requests without a ``Bearer`` Authorization header, and every request
        in ``dangerously_allow_unauthenticated_requests`` mode (where the SDK
        ignores the header), are left to the SDK unchanged. That includes a
        configured ``webhook_verifier``: the App is then built with that flag
        and the bridge has already run the verifier, which replaces the SDK's
        JWT validation (and so this pre-check) entirely.
        """
        if self._auth_disabled():
            return False
        authorization = headers.get("authorization") or headers.get("Authorization") or ""
        if not authorization.startswith("Bearer "):
            return False
        import jwt

        try:
            claims = jwt.decode(authorization.removeprefix("Bearer "), options={"verify_signature": False})
        except jwt.InvalidTokenError:
            # Unreadable token: the SDK would reject it too. Its issuer is not
            # the Bot Framework's, so reject here without touching the SDK.
            claims = {}
        return not self._is_bot_framework_issuer(claims.get("iss"))

    def _auth_disabled(self) -> bool:
        """Whether the SDK App skips inbound JWT validation.

        ``dangerously_allow_unauthenticated_requests`` on 2.0.14+ (set from the
        option or the SDK's env var), ``skip_auth`` on 2.0.13.
        """
        options = self._app.options
        allow = getattr(options, "dangerously_allow_unauthenticated_requests", None)
        if allow is None:
            allow = getattr(options, "skip_auth", False)
        return bool(allow)

    def _is_bot_framework_token(self, token: Any) -> bool:
        """Return ``False`` for a validated inbound token not issued by the Bot Framework.

        Defence in depth behind :meth:`_rejects_before_auth`, which already
        turns such tokens away before validation: the same issuer rule, applied
        to the token the SDK validated, in case a request ever reaches
        :meth:`_dispatch_activity` without passing the bridge's pre-check.

        Only a ``JsonWebToken`` is checked: the SDK wraps every token it
        validated in one. In ``dangerously_allow_unauthenticated_requests``
        mode the SDK passes a placeholder token instead, and that mode is left
        as the SDK defines it.
        """
        from microsoft_teams.api import JsonWebToken

        if not isinstance(token, JsonWebToken):
            return True
        return self._is_bot_framework_issuer(token.issuer)

    def _is_bot_framework_issuer(self, issuer: Any) -> bool:
        """Whether ``issuer`` is this cloud's Bot Framework token issuer; logs a rejection."""
        expected = self._app.cloud.token_issuer
        if issuer == expected:
            return True
        self._logger.warn(
            "Teams activity rejected: inbound token was not issued by the Bot Framework",
            {"issuer": issuer, "expectedIssuer": expected},
        )
        return False

    @staticmethod
    def _activity_to_dict(event: Any) -> dict[str, Any]:
        """Extract the camelCase activity dict from a Teams SDK activity event.

        Handles both the SDK ``ActivityEvent`` (``event.body`` is a
        ``CoreActivity`` pydantic model) and an ``ActivityContext`` (``ctx``
        whose ``ctx.activity`` is a typed activity model). In both cases we
        ``model_dump(by_alias=True, exclude_none=True)`` to recover the exact
        Bot Framework wire shape (``from``/``serviceUrl``/``channelData``/…)
        that the adapter's dict-based handlers were written against.
        """
        source = getattr(event, "body", None)
        if source is None:
            source = getattr(event, "activity", event)
        model_dump = getattr(source, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(by_alias=True, exclude_none=True)
            if isinstance(dumped, dict):
                return dumped
        return source if isinstance(source, dict) else {}

    # ------------------------------------------------------------------
    # Teams SDK event handlers (registered in _register_event_handlers).
    # Each sources its activity from the SDK ``ActivityContext`` and routes to
    # the adapter's existing chat-processing logic. They are wired to the SDK
    # decorators for parity; dispatch is driven by _dispatch_activity.
    # ------------------------------------------------------------------
    async def _on_sdk_message(self, ctx: Any) -> None:
        activity = self._activity_to_dict(ctx)
        await self._cache_user_context(activity)
        await self._handle_message_activity(activity, self._bridge.get_webhook_options(activity.get("id")))

    async def _on_sdk_message_reaction(self, ctx: Any) -> None:
        activity = self._activity_to_dict(ctx)
        await self._cache_user_context(activity)
        self._handle_reaction_activity(activity, self._bridge.get_webhook_options(activity.get("id")))

    async def _on_sdk_card_action(self, ctx: Any) -> Any:
        activity = self._activity_to_dict(ctx)
        await self._cache_user_context(activity)
        action_data = (activity.get("value") or {}).get("action", {}).get("data", {})
        if isinstance(action_data, dict) and action_data.get("actionId"):
            await self._handle_adaptive_card_action(
                activity, action_data, self._bridge.get_webhook_options(activity.get("id"))
            )
        return {
            "status": 200,
            "body": {
                "statusCode": 200,
                "type": "application/vnd.microsoft.activity.message",
                "value": "",
            },
        }

    async def _on_sdk_dialog_open(self, ctx: Any) -> Any:
        # Modal/dialog support is out of PR-1 scope (tracked for a later wave);
        # cache context for parity and return no task module response.
        activity = self._activity_to_dict(ctx)
        await self._cache_user_context(activity)
        return None

    async def _on_sdk_dialog_submit(self, ctx: Any) -> Any:
        activity = self._activity_to_dict(ctx)
        await self._cache_user_context(activity)
        return None

    async def _on_sdk_conversation_update(self, ctx: Any) -> None:
        await self._cache_user_context(self._activity_to_dict(ctx))

    async def _on_sdk_install(self, ctx: Any) -> None:
        await self._cache_user_context(self._activity_to_dict(ctx))

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

        # Cache aadObjectId for Microsoft Graph API user lookups (chat.get_user).
        # Only Bot Framework user IDs ("29:...") are surfaced in incoming
        # activities; the Graph API needs the tenant-scoped AAD object ID
        # to call /users/{id}. Cache when present so get_user() can map.
        aad_object_id = from_user.get("aadObjectId")
        if aad_object_id and state:
            await state.set(f"teams:aadObjectId:{user_id}", aad_object_id, ttl)

        # Cache channel context
        team_aad_group_id = channel_data.get("team", {}).get("aadGroupId")
        conversation_id = conversation.get("id", "")
        base_channel_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)

        if team_aad_group_id and channel_data.get("channel", {}).get("id") and state:
            # Wire-shape parity with upstream TS (#403): the channel branch
            # omits the discriminator. ``_chat_id_from_context`` and
            # ``_get_graph_context`` treat ``type != "dm"`` as channel, so
            # the missing key is unambiguous.
            context: TeamsChannelContext = {
                "team_id": team_aad_group_id,
                "channel_id": channel_data["channel"]["id"],
            }
            await state.set(f"teams:channelContext:{base_channel_id}", json.dumps(context), ttl)

        # Cache DM context for Microsoft Graph chat ID resolution
        # (vercel/chat#403). Bot Framework hands out opaque DM conversation
        # IDs that Graph's ``/chats/{chat-id}/messages`` endpoint rejects;
        # the canonical Graph chat ID for a 1:1 DM is
        # ``19:{userAadId}_{botId}@unq.gbl.spaces``. ``aadObjectId`` is
        # only present for real Teams users (not bots), and DM conversation
        # IDs do not start with ``19:`` (channel/group chats do).
        #
        # Defense-in-depth: AAD object IDs are GUIDs (8-4-4-4-12 hex). Bot
        # Framework JWT verification authenticates the activity envelope
        # but does not constrain ``from.aadObjectId``; a malformed value
        # could otherwise inject ``/`` / ``?`` / ``#`` into the Graph URL
        # path and cause a misrouted request. Reject anything that doesn't
        # match the GUID shape before formatting it into the chat ID.
        aad_object_id = from_user.get("aadObjectId")
        if (
            isinstance(aad_object_id, str)
            and self._app_id
            and not base_channel_id.startswith("19:")
            and state
            and _AAD_OBJECT_ID_PATTERN.fullmatch(aad_object_id)
        ):
            dm_context: TeamsDmContext = {
                "graph_chat_id": f"19:{aad_object_id}_{self._app_id}@unq.gbl.spaces",
                "type": "dm",
            }
            await state.set(f"teams:channelContext:{base_channel_id}", json.dumps(dm_context), ttl)

    async def _handle_message_activity(
        self,
        activity: dict[str, Any],
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle message activities.

        For DMs we capture the Teams SDK ``IStreamer`` for this conversation
        and ``await`` the chat-handler task so :meth:`stream` can dispatch
        through ``IStreamer.emit()`` while the streamer is live. Group chats
        remain fire-and-forget — Teams doesn't support native streaming in
        channels/group threads, so :meth:`stream` falls through to the
        accumulate-and-post path.

        Mirrors upstream ``handleMessageActivity`` in
        ``packages/adapter-teams/src/index.ts``: capture ``ctx.stream`` for
        DMs, block until processing completes, then close the streamer.
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
        # Both the group and DM paths wait for the sender lookup before
        # dispatch, as upstream does.
        await self._hydrate_author_email(activity, message)

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
            # active streamer and accumulate-and-post.
            self._chat.process_message(self, thread_id, message, options)
            return

        # DM path: capture the Teams SDK ``IStreamer`` for this conversation,
        # register it, then block on the handler so ``stream()`` can dispatch
        # through ``IStreamer.emit()`` while the streamer stays alive. We chain
        # a ``waitUntil`` shim on top of the caller-supplied one (if any) so a
        # hosting webhook framework that respects ``waitUntil`` still gets the
        # underlying task — the local ``await`` is purely so we know when the
        # handler is done and the streamer can be closed.
        #
        # Mirrors upstream ``handleMessageActivity`` in
        # ``packages/adapter-teams/src/index.ts`` (``@chat-adapter/teams@4.30.0``):
        # ``this.activeStreams.set(threadId, ctx.stream)`` → build
        # ``processingDone`` + wrapped ``waitUntil`` → ``await processingDone``.
        # Upstream lets the Teams SDK ``App`` auto-close ``ctx.stream`` after the
        # handler returns; our bridge owns dispatch (we override
        # ``server.on_request``), so we play the lifecycle-owner role and call
        # ``stream.close()`` ourselves in the ``finally`` below — exactly where
        # the SDK's own ``app_process.process_activity`` calls it. ``stream()``
        # and ``_stream_via_emit`` never close the streamer.
        streamer = self._create_streamer(activity, thread_id)
        if streamer is None:
            # Could not build a streamer (e.g. the inbound activity lacks the
            # recipient/conversation fields the SDK ref needs). Fall back to
            # fire-and-forget; ``stream()`` will accumulate-and-post.
            self._chat.process_message(self, thread_id, message, options)
            return

        # Keyed by ``thread_id`` to match upstream ``activeStreams.set(threadId, …)``.
        # Safe because the default per-thread concurrency strategy in
        # ``Chat.handle_incoming_message`` serialises DM handlers for the same
        # thread (overlapping webhooks are deduped or dropped before they reach
        # a handler, so two ``stream()`` calls cannot share a streamer).
        self._active_streams[thread_id] = streamer
        loop = asyncio.get_running_loop()
        processing_done: asyncio.Future[None] = loop.create_future()

        def _resolve_processing(task: Awaitable[Any]) -> None:
            # Hook the handler task's completion so we can release
            # ``processing_done`` regardless of
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
        # The task the chained wait_until received during ``process_message``
        # (``None`` if it never fired). Used below as the gate fallback when
        # ``process_message`` returns no Task.
        handed_task: Awaitable[Any] | None = None

        def _chained_wait_until(task: Awaitable[Any]) -> None:
            nonlocal handed_task
            # Record the task for the gate BEFORE invoking the caller's
            # ``wait_until``: even if that callback raises, blocks, or never
            # fires, the gate below is still wired up, so the await on
            # ``processing_done`` cannot starve due to a misbehaving
            # caller-supplied hook.
            handed_task = task
            if upstream_wait_until is not None:
                # Catch synchronous failures in the caller's hook. If we
                # let it escape, ``Chat.process_message`` propagates the
                # exception, the outer ``try`` skips ``await processing_done``,
                # and the ``finally`` closes the streamer while the underlying
                # chat task is still scheduled — handlers that later call
                # ``thread.stream()`` would then miss native streaming and fall
                # back to a normal post. Logging keeps the failure visible
                # without breaking the streaming path.
                try:
                    upstream_wait_until(task)
                except Exception as exc:
                    self._logger.warn(
                        "Caller-supplied WebhookOptions.wait_until raised",
                        {"threadId": thread_id, "error": str(exc)},
                    )

        # Spread the caller's options (upstream ``{ ...baseOptions, waitUntil }``)
        # so ``propagate_handler_errors`` / ``deduplicate`` survive the shim.
        # ``wait_until`` receives the raw handler task when
        # ``propagate_handler_errors`` is set, otherwise Chat's error-swallowing
        # wrapper Task.
        chained_options = dataclasses.replace(options or WebhookOptions(), wait_until=_chained_wait_until)

        try:
            handler_task = self._chat.process_message(self, thread_id, message, chained_options)
            # Gate on the handler task ``process_message`` returns rather than
            # on the Task handed to ``wait_until``: by default that is Chat's
            # error-swallowing wrapper, which a host may cancel (e.g. a
            # background-task timeout) while the shielded handler keeps
            # streaming. Upstream's ``.catch`` promise cannot be cancelled, so
            # there it always settles with the handler. The handed task is the
            # fallback for a ``process_message`` that returns no Task.
            if isinstance(handler_task, asyncio.Task):
                _resolve_processing(handler_task)
            elif handed_task is not None:
                _resolve_processing(handed_task)
            # If ``process_message`` returned no task and never invoked
            # ``wait_until``, no chat task was scheduled (deduped, dropped by the concurrency strategy, or
            # the message wasn't admitted for handling). Resolve the gate
            # immediately so ``await processing_done`` doesn't hang
            # forever — there is no in-flight handler to wait on.
            elif not processing_done.done():
                processing_done.set_result(None)
            await processing_done
        finally:
            # Always close the streamer (the SDK sends the ``streamType: 'final'``
            # message here) and drop the registry entry so a subsequent message
            # can register fresh. We close even on cancellation, in two cases:
            #   • SDK-detected cancel (Teams 403 / ``StreamCancelledError``): the
            #     SDK's ``HttpStream.close`` no-ops because ``_canceled`` is set —
            #     matching the SDK App's ``process_activity``, which also closes in
            #     both its success and ``StreamCancelledError`` branches.
            #   • Raw ``asyncio.CancelledError`` (the webhook task is cancelled
            #     mid-stream): ``_canceled`` is NOT set, so ``close`` flushes a
            #     final activity for whatever was accumulated. This is intentional —
            #     it finalizes the partial response instead of leaving Teams in a
            #     dangling "streaming" state. The send below is wrapped in
            #     ``try/except Exception``, which does NOT catch ``CancelledError``
            #     (a ``BaseException``), so any network failure is swallowed while
            #     the original cancellation still propagates after the ``finally``.
            current = self._active_streams.get(thread_id)
            if current is streamer:
                self._active_streams.pop(thread_id, None)
            try:
                await streamer.close()
            except Exception as exc:  # pragma: no cover — diagnostic-only
                self._logger.warn(
                    "Teams stream finalization failed",
                    {"threadId": thread_id, "error": str(exc)},
                )

    def _create_streamer(self, activity: dict[str, Any], thread_id: str) -> StreamerProtocol | None:
        """Create a Teams SDK ``IStreamer`` for the inbound DM activity.

        Builds the :class:`ConversationReference` the streamer needs from the
        inbound activity (``recipient`` → bot account, ``conversation``,
        ``channelId``, ``serviceUrl``) and builds the stream the same way the
        installed SDK's own ``ActivityContext`` builds ``ctx.stream``. The
        returned ``HttpStream`` owns the Bot Framework streaming wire format
        (``streamType``/``streamSequence``/``streamId``), the per-flush
        throttle, and 429 retry/backoff. We never poke its internals.

        Supports both SDK lines by feature detection (not version sniffing):

        * ``microsoft-teams-apps`` 2.0.x exposes ``App.activity_sender``;
          ``ActivitySender.create_stream(ref)`` builds a fresh ``ApiClient``
          on ``ref.service_url``.
        * 2.1.x removed ``ActivitySender``. ``ActivityContext.stream`` now
          calls ``HttpStream(app.api, ref)`` directly, and ``HttpStream``
          scopes its own client with ``api.from_service_url(ref.service_url)``.

        Either way the stream gets its own client pinned to the inbound
        activity's service URL, so a later :meth:`_point_app_api_at` (which
        retargets the shared ``App.api`` in place) cannot move it.

        Returns ``None`` when the activity lacks the fields the SDK ref
        requires (``serviceUrl``), so the caller can fall back to
        fire-and-forget processing + accumulate-and-post.
        """
        from chat_sdk.adapters.teams.streamer import build_conversation_reference

        service_url = activity.get("serviceUrl") or ""
        if not service_url:
            return None
        try:
            _validate_service_url(service_url)
            ref = build_conversation_reference(activity, bot_app_id=self._app_id)
            activity_sender = getattr(self._app, "activity_sender", None)
            if activity_sender is not None:
                # microsoft-teams-apps 2.0.x
                return activity_sender.create_stream(ref)
            # microsoft-teams-apps 2.1+: no ActivitySender.
            return self._new_http_stream(ref)
        except Exception as exc:
            self._logger.warn(
                "Failed to create Teams streamer; falling back to buffered post",
                {"threadId": thread_id, "error": str(exc)},
            )
            return None

    def _new_http_stream(self, ref: ConversationReference) -> StreamerProtocol:
        """Build an ``HttpStream`` for ``ref`` on SDKs without ``ActivitySender`` (2.1+).

        Mirrors 2.1's ``ActivityContext.stream`` (``HttpStream(self.api, ref)``),
        except that we hand ``HttpStream`` a client already scoped with
        ``App.api.from_service_url(ref.service_url)`` rather than the shared
        ``App.api``. 2.1's ``HttpStream`` scopes the client again itself, so
        this costs one extra lightweight clone (the HTTP connection is shared).
        It keeps the stream off the shared client even if a future
        ``HttpStream`` stops scoping, because ``_point_app_api_at`` retargets
        ``App.api`` in place for outbound calls.

        An SDK whose ``ApiClient`` has no ``from_service_url`` raises
        ``AttributeError`` here, which :meth:`_create_streamer` turns into the
        buffered-post fallback.
        """
        from microsoft_teams.apps import HttpStream

        scoped_api = self._app.api.from_service_url(ref.service_url)
        return HttpStream(scoped_api, ref)

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
        ``{"actionId": "__auto_submit", "my_select": "option_1"}`` and is
        fanned out into one :meth:`process_action` per input key (upstream
        index.ts:404-412 + fanOutAutoSubmit index.ts:513-556).
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

        # Auto-submit fan-out: fire on_action for each input value.
        if action_value.get("actionId") == AUTO_SUBMIT_ACTION_ID:
            self._fan_out_auto_submit(action_value, activity, thread_id, options)
            return

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

        # Auto-submit fan-out: fire on_action for each input value.
        if action_data.get("actionId") == AUTO_SUBMIT_ACTION_ID:
            self._fan_out_auto_submit(action_data, activity, thread_id, options)
            return

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

    def _fan_out_auto_submit(
        self,
        payload: dict[str, Any],
        activity: dict[str, Any],
        thread_id: str,
        options: WebhookOptions | None = None,
    ) -> None:
        """Fan out an auto-submit payload into individual ``on_action`` calls.

        Called when the sentinel ``__auto_submit`` action ID is detected on a
        ChoiceSet (Select / RadioSelect) submission. Each input key/value pair
        is dispatched as a separate :meth:`process_action` so a handler
        registered as ``on_action(input_key)`` fires once per submitted input.

        Python port of upstream ``fanOutAutoSubmit`` (adapter-teams/src/index.ts:513-556).
        Transport keys (``actionId``, ``msteams``) injected by the SDK card
        renderer / Teams infra are filtered out; non-string values map to
        ``None`` (upstream ``typeof val === "string" ? val : undefined``).
        """
        if not self._chat:
            return

        entries = [(k, v) for k, v in payload.items() if k not in self._ACTION_TRANSPORT_KEYS]

        self._logger.debug(
            "Auto-submit fan-out",
            {"inputCount": len(entries), "keys": [k for k, _ in entries]},
        )

        from_user = activity.get("from", {})
        user = Author(
            user_id=from_user.get("id", "unknown"),
            user_name=from_user.get("name", "unknown"),
            full_name=from_user.get("name", "unknown"),
            is_bot=False,
            is_me=False,
        )
        message_id = activity.get("replyToId") or activity.get("id", "")

        for key, val in entries:
            self._chat.process_action(
                ActionEvent(
                    action_id=key,
                    value=val if isinstance(val, str) else None,
                    user=user,
                    message_id=message_id,
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
        service_url = activity.get("serviceUrl")
        attachments = [
            self._create_attachment(att, service_url if isinstance(service_url, str) else None)
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

    def _create_attachment(self, att: dict[str, Any], service_url: str | None = None) -> Attachment:
        """Create an Attachment from a Teams attachment dict (upstream ``createAttachment``).

        Inline attachments on the activity's connector origin are fetched with
        the bot token; everything else (including file cards, via their
        pre-signed ``content.downloadUrl``) is fetched anonymously. See
        :mod:`chat_sdk.adapters.teams.attachments`.
        """
        return create_teams_attachment(att, service_url, self._attachment_fetchers())

    def _attachment_fetchers(self) -> TeamsAttachmentFetchers:
        return TeamsAttachmentFetchers(
            create_anonymous_fetch_data=self._build_teams_fetch_data,
            fetch_authenticated=self._fetch_authenticated_attachment,
        )

    @staticmethod
    def _is_trusted_teams_download_url(url: str) -> bool:
        """Gate anonymous Teams file downloads to Microsoft-owned hosts.

        Checked on the initial URL in front of the shared guarded downloader
        (which re-validates scheme and internal addresses on every hop).

        This is a Python-first divergence: upstream fetches any public HTTPS
        URL. See ``docs/UPSTREAM_SYNC.md`` Known Non-Parity.
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
        """Build the anonymous ``fetch_data`` closure for ``url`` (upstream ``createFetchDataFn``).

        Subclasses may override it; rehydrated attachments use the override
        too. The download goes through the shared guarded downloader (HTTPS
        only, internal addresses refused, every redirect re-validated, 25 MB
        cap, 30 s deadline).
        """
        anonymous = create_anonymous_attachment_fetch_data(url)

        async def fetch_data() -> bytes:
            # Upstream's URL checks run first, so their refusals keep
            # upstream's messages.
            target = validate_attachment_url(url, "teams")
            # Divergence from upstream — see docs/UPSTREAM_SYNC.md: the
            # Microsoft host allowlist stays in front of the downloader.
            if not self._is_trusted_teams_download_url(target):
                raise ValidationError(
                    "teams",
                    f"Refusing to fetch Teams file from untrusted URL: {url}",
                )
            return await anonymous()

        return fetch_data

    async def _fetch_authenticated_attachment(self, url: str) -> bytes:
        """GET a protected inline attachment from the connector with the bot token.

        Port of upstream ``fetchAuthenticatedAttachment``: the token comes
        from the SDK's ``App.api.http`` and redirects are not followed. The
        caller has already checked that ``url`` is on the activity's connector
        origin.
        """
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: the token goes
        # only to an allow-listed Bot Framework connector, so a tampered
        # ``connectorOrigin`` in rehydrated metadata cannot send it to a
        # non-Bot-Framework host. Plain-``http`` loopback stays accepted for
        # the Emulator (upstream parity: ``getConnectorOrigin`` in
        # ``attachments.ts`` accepts it too). The allowlist is checked on the
        # normalized origin, so an explicit default port (``:443``) passes.
        origin = _connector_origin(url)
        if origin is None or not is_trusted_teams_service_url(origin):
            raise NetworkError("teams", BOT_TOKEN_REFUSAL)
        try:
            return await fetch_with_bot_token(self._app.api.http, url)
        except NetworkError:
            raise
        except Exception as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            suffix = f": {status}" if isinstance(status, int) else ""
            raise NetworkError("teams", f"Failed to fetch authenticated file{suffix}", error) from error

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Teams attachment.

        Rebuilds the retrieval recorded in ``fetch_metadata``: anonymous (by
        URL, falling back to ``attachment.url``) or bot-authenticated
        (``auth: "bot"`` plus ``connectorOrigin``; without the origin the
        fetch is refused). URLs are re-validated inside the closure, so a
        tampered entry fails at fetch time. Returns the attachment unchanged
        when no URL is available.
        """
        return rehydrate_teams_attachment(attachment, self._attachment_fetchers())

    def _is_message_from_self(self, activity: dict[str, Any]) -> bool:
        """Check if the activity is from the bot."""
        from_id = activity.get("from", {}).get("id")
        if not (from_id and self._app_id):
            return False
        if from_id == self._app_id:
            return True
        return bool(from_id.endswith(f":{self._app_id}"))

    async def _files_to_attachments(self, files: list[FileUpload]) -> list[dict[str, Any]]:
        """Convert ``FileUpload`` objects to Bot Framework data-URI attachments.

        Python port of ``filesToAttachments`` in
        ``packages/adapter-teams/src/index.ts`` (lines ~1006-1035). Each file's
        bytes are base64-encoded into a ``data:`` URI and attached as a
        Bot Framework activity attachment. Files whose data cannot be resolved
        to bytes are skipped (mirrors upstream's ``throwOnUnsupported: false``
        followed by ``if (!buffer) continue``).
        """
        attachments: list[dict[str, Any]] = []

        for file in files:
            buffer = await to_buffer(file.data, "teams", throw_on_unsupported=False)
            if buffer is None:
                self._logger.debug(
                    "Teams API: skipping file with unsupported data",
                    {"filename": file.filename},
                )
                continue

            mime_type = file.mime_type or "application/octet-stream"
            data_uri = buffer_to_data_uri(buffer, mime_type)

            attachments.append(
                {
                    "contentType": mime_type,
                    "contentUrl": data_uri,
                    "name": file.filename,
                }
            )

        return attachments

    def _point_app_api_at(self, service_url: str) -> None:
        """Aim the SDK ``App``'s Bot Framework client at ``service_url``.

        The migrated outbound paths call ``self._app.send(...)`` and
        ``self._app.api.conversations.activities(...)`` directly (parity with
        upstream ``this.app.send`` / ``this.app.api.conversations``). The SDK
        binds the App's :class:`ApiClient` to a single service URL at
        construction, and ``app.send`` reads ``self.api.service_url`` into the
        outgoing :class:`ConversationReference`. Our thread IDs encode a
        per-thread service URL, so before each call we retarget the App's API
        client — validating against the SSRF allow-list first, exactly as the
        retired hand-rolled senders did.

        The setter walks the real :class:`ApiClient`'s service-url chain
        (the client itself, its ``conversations`` sub-client, and that
        sub-client's ``activities_client``). It is defensive about test doubles
        that replace ``self._app.api`` with a mock lacking those attributes —
        an ``AttributeError`` there is harmless because the mock ignores the
        service URL anyway.

        Works on both ``microsoft-teams-apps`` 2.0.x and 2.1.x. On 2.1 the
        activity methods gained optional ``service_url=`` / ``agentic_identity=``
        keywords; we pass neither, so they fall back to the client's own (just
        retargeted) ``service_url``, and ``app.send`` sees
        ``ref.service_url == api.service_url`` and sends on ``App.api`` itself.
        """
        _validate_service_url(service_url)
        normalized = service_url.rstrip("/")
        api = self._app.api
        try:
            api.service_url = normalized
            conversations = api.conversations
            conversations.service_url = normalized
            conversations.activities_client.service_url = normalized
        except AttributeError:
            # ``self._app.api`` is a test double without the real client's
            # service-url chain; nothing to retarget.
            pass

    @staticmethod
    def _message_activity_input(payload: dict[str, Any]) -> Any:
        """Build the SDK ``MessageActivityInput`` from our camelCase activity dict.

        The dict we construct (``text`` / ``textFormat`` / ``attachments`` with
        ``contentType`` / ``contentUrl`` / ``content`` / ``name`` keys) is the
        Bot Framework wire shape, which matches the SDK input model's
        serialization aliases — so ``model_validate`` round-trips it directly.
        We keep building the dict (it is still returned as ``RawMessage.raw``,
        preserving the public contract) and convert at the SDK boundary.

        Upstream constructs a ``MessageActivity``; the Python SDK splits input
        (``MessageActivityInput``) from output (``MessageActivity``) models and
        ``app.send`` / ``activities.update`` accept only the input variant.
        """
        from microsoft_teams.api import MessageActivityInput

        return MessageActivityInput.model_validate(payload)

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Teams conversation."""
        decoded = self.decode_thread_id(thread_id)

        files = extract_files(message)
        file_attachments = await self._files_to_attachments(files) if files else []

        card = extract_card(message)
        if card:
            adaptive_card = card_to_adaptive_card(card)
            activity_payload = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    },
                    *file_attachments,
                ],
            }

            self._logger.debug(
                "Teams API: send (adaptive card)",
                {
                    "conversationId": decoded.conversation_id,
                    "fileCount": len(file_attachments),
                },
            )

            try:
                self._point_app_api_at(decoded.service_url)
                sent = await self._app.send(
                    decoded.conversation_id,
                    self._message_activity_input(activity_payload),
                )
                return RawMessage(
                    id=getattr(sent, "id", "") or "",
                    thread_id=thread_id,
                    raw=activity_payload,
                )
            except Exception as error:
                self._logger.error(
                    "Teams API: send failed",
                    {
                        "conversationId": decoded.conversation_id,
                        "error": str(error),
                    },
                )
                _handle_teams_error(error, "postMessage")
                raise  # unreachable: _handle_teams_error always raises

        # Regular text message
        text = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "teams",
        )

        activity_payload: dict[str, Any] = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }
        if file_attachments:
            activity_payload["attachments"] = file_attachments

        self._logger.debug(
            "Teams API: send (message)",
            {
                "conversationId": decoded.conversation_id,
                "textLength": len(text),
                "fileCount": len(file_attachments),
            },
        )

        try:
            self._point_app_api_at(decoded.service_url)
            sent = await self._app.send(
                decoded.conversation_id,
                self._message_activity_input(activity_payload),
            )
            self._logger.debug("Teams API: send response", {"messageId": getattr(sent, "id", None)})
            return RawMessage(
                id=getattr(sent, "id", "") or "",
                thread_id=thread_id,
                raw=activity_payload,
            )
        except Exception as error:
            self._logger.error(
                "Teams API: send failed",
                {
                    "conversationId": decoded.conversation_id,
                    "error": str(error),
                },
            )
            _handle_teams_error(error, "postMessage")
            # Should not reach here due to _handle_teams_error always raising
            raise  # pragma: no cover

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Teams message.

        Note: file delivery is intentionally NOT wired here. Upstream
        ``vercel/chat`` ports ``filesToAttachments`` into ``postMessage`` and
        ``postChannelMessage`` only — ``editMessage`` does not carry files — and
        chinchill delivers execution artifacts via a fresh ``post`` (never by
        editing files into an existing message). Keeping ``edit_message``
        file-free preserves upstream fidelity.
        """
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
                    },
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
            self._point_app_api_at(decoded.service_url)
            await self._app.api.conversations.activities(decoded.conversation_id).update(
                message_id,
                self._message_activity_input(activity_payload),
            )
        except Exception as error:
            self._logger.error(
                "Teams API: updateActivity failed",
                {
                    "conversationId": decoded.conversation_id,
                    "messageId": message_id,
                    "error": str(error),
                },
            )
            _handle_teams_error(error, "editMessage")
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
            self._point_app_api_at(decoded.service_url)
            await self._app.api.conversations.activities(decoded.conversation_id).delete(message_id)
        except Exception as error:
            self._logger.error(
                "Teams API: deleteActivity failed",
                {
                    "conversationId": decoded.conversation_id,
                    "messageId": message_id,
                    "error": str(error),
                },
            )
            _handle_teams_error(error, "deleteMessage")
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
        from microsoft_teams.api import TypingActivityInput

        decoded = self.decode_thread_id(thread_id)

        self._logger.debug(
            "Teams API: send (typing)",
            {
                "conversationId": decoded.conversation_id,
            },
        )

        try:
            self._point_app_api_at(decoded.service_url)
            await self._app.send(decoded.conversation_id, TypingActivityInput())
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

        DMs stream natively via the Teams SDK ``IStreamer.emit()`` when an
        active streamer exists (captured by :meth:`_handle_message_activity`
        from the inbound activity). Group chats / channels / proactive messages
        accumulate the stream and post a single message — Teams supports native
        streaming only in 1:1 chats.

        Mirrors upstream ``stream`` in
        ``packages/adapter-teams/src/index.ts`` (``@chat-adapter/teams@4.30.0``):
        delegate to ``streamViaEmit`` when ``activeStream && !activeStream.canceled``,
        else accumulate and ``postMessage``.
        """
        active_stream = self._active_streams.get(thread_id)
        if active_stream is not None and not active_stream.canceled:
            return await self._stream_via_emit(thread_id, text_stream, active_stream)

        # No native streamer (group chats, proactive messages, or DMs whose
        # streamer was already canceled). Accumulate and post once — matching
        # upstream's post-#416 behavior of avoiding the post+edit flicker
        # where Teams doesn't support native streaming.
        accumulated = ""
        async for chunk in text_stream:
            text = ""
            if isinstance(chunk, str):
                text = chunk
            elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                text = chunk.get("text", "")
            elif is_thinking_chunk(chunk):
                # Python-only divergence: streaming-only reasoning, not message
                # content. Default-skip keeps the buffered post byte-identical;
                # an opt-in ``render_thinking`` hook may display it.
                await maybe_render_thinking(getattr(self, "render_thinking", None), chunk)
                continue
            if not text:
                continue
            accumulated += text

        if not accumulated:
            return RawMessage(id="", thread_id=thread_id, raw={"text": ""})

        # SDK-backed buffered send (PR 2): same path as a normal text post.
        return await self.post_message(thread_id, PostableMarkdown(markdown=accumulated))

    async def _stream_via_emit(
        self,
        thread_id: str,
        text_stream: Any,
        stream: StreamerProtocol,
    ) -> RawMessage:
        """Native streaming via the Teams SDK ``IStreamer.emit()``.

        Each non-empty chunk is handed to ``stream.emit(text)``; the SDK
        ``HttpStream`` owns the Bot Framework streaming wire format
        (``streamType``/``streamSequence``/``streamId``), the per-flush
        throttle (~500ms between flushes), and 429 retry/backoff. We never
        touch its internals and we never call ``stream.close()`` — the SDK
        sends the ``streamType: 'final'`` message after the handler returns
        (see :meth:`_handle_message_activity`'s ``finally`` block, which plays
        the lifecycle-owner role the SDK App otherwise would).

        Cancellation is detected two ways, matching upstream ``streamViaEmit``:

        - ``stream.canceled`` is checked before each ``emit`` (the user pressed
          Stop, or the Teams 2-minute streaming timeout elapsed).
        - ``StreamCancelledError`` raised by ``emit``/iteration is caught and
          swallowed (other exceptions re-raise).

        We capture the first chunk's server-assigned id via ``on_chunk`` and
        await it ONLY when text was emitted and the stream was not canceled —
        awaiting unconditionally would hang forever if no chunk was ever
        delivered (empty stream, or canceled before the first flush).

        Mirrors upstream ``streamViaEmit`` in
        ``packages/adapter-teams/src/index.ts`` (``@chat-adapter/teams@4.30.0``).
        """
        from microsoft_teams.apps import StreamCancelledError

        accumulated = ""
        message_id = ""

        # Capture the first chunk's id. The SDK emits a ``chunk`` event for
        # every stream activity it ships (``HttpStream._send_activity``); we
        # only care about the first one, which carries the message id Teams
        # assigns to the streamed message. A Future resolved by the first
        # callback mirrors upstream's ``stream.events.once("chunk", …)``.
        loop = asyncio.get_running_loop()
        id_captured: asyncio.Future[str] = loop.create_future()

        async def _on_chunk(activity: Any) -> None:
            if not id_captured.done():
                id_captured.set_result(getattr(activity, "id", "") or "")

        stream.on_chunk(_on_chunk)

        try:
            async for chunk in text_stream:
                if stream.canceled:
                    self._logger.debug("Teams stream canceled by user", {"threadId": thread_id})
                    break

                text = ""
                if isinstance(chunk, str):
                    text = chunk
                elif isinstance(chunk, dict) and chunk.get("type") == "markdown_text":
                    text = chunk.get("text", "")
                elif is_thinking_chunk(chunk):
                    # Python-only divergence: streaming-only reasoning, not
                    # message content. Default-skip keeps emitted text
                    # byte-identical; an opt-in ``render_thinking`` hook may
                    # display it.
                    await maybe_render_thinking(getattr(self, "render_thinking", None), chunk)
                    continue
                elif getattr(chunk, "type", None) == "markdown_text":
                    # Dataclass ``MarkdownTextChunk`` form — mirror
                    # ``Thread.stream``'s ``_wrapped_stream`` extraction so the
                    # adapter emits exactly the text Thread accumulates. Other
                    # ``StreamChunk`` variants (task/plan updates) carry no
                    # ``text`` and are skipped.
                    text = getattr(chunk, "text", "") or ""
                if not text:
                    continue

                stream.emit(text)
                accumulated += text
        except StreamCancelledError:
            self._logger.debug("Teams stream canceled during iteration", {"threadId": thread_id})

        # Only await the chunk id if we emitted text and the stream wasn't
        # canceled before any chunk was delivered (which would hang forever).
        if accumulated and not stream.canceled:
            try:
                message_id = await id_captured
            except StreamCancelledError:
                self._logger.debug(
                    "Teams stream canceled before first chunk delivered",
                    {"threadId": thread_id},
                )

        return RawMessage(id=message_id, thread_id=thread_id, raw={"text": accumulated})

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

        # vercel/chat#403: always look up the Graph context, not just for
        # channel threads — DMs need it to map the opaque Bot Framework
        # conversation ID to the canonical Graph chat ID
        # (``19:{aadId}_{botId}@unq.gbl.spaces``) that ``/chats/{id}``
        # accepts.
        graph_context = await self._get_graph_context(base_conversation_id)
        context_type: str | None = graph_context.get("type") if graph_context else None

        try:
            self._logger.debug(
                "Teams Graph API: fetching messages",
                {
                    "conversationId": base_conversation_id,
                    "threadMessageId": thread_message_id,
                    "contextType": context_type or "none",
                    "limit": limit,
                    "cursor": cursor,
                    "direction": direction,
                },
            )

            if graph_context and context_type != "dm" and thread_message_id:
                # Narrowed: channel context for a channel thread.
                channel_context = cast(TeamsChannelContext, graph_context)
                return await self._fetch_channel_thread_messages(
                    channel_context,
                    thread_message_id,
                    thread_id,
                    options,
                )

            chat_id = self._chat_id_from_context(graph_context, base_conversation_id)
            graph_messages, has_more = await self._paginate_graph_chat_messages(chat_id, limit, direction, cursor)

            if thread_message_id and not graph_context:
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
            graph_context = await self._get_graph_context(base_conversation_id)
            context_type = graph_context.get("type") if graph_context else None

            self._logger.debug(
                "Teams Graph API: fetchChannelMessages",
                {
                    "conversationId": base_conversation_id,
                    "contextType": context_type or "none",
                    "limit": limit,
                    "direction": direction,
                },
            )

            graph_messages: list[dict[str, Any]]
            has_more = False

            if graph_context and context_type != "dm":
                channel_context = cast(TeamsChannelContext, graph_context)
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
            else:
                # vercel/chat#403: DM contexts substitute the canonical Graph
                # chat ID for the opaque Bot Framework conversation ID; no
                # context (group chat) falls through to the raw ID.
                chat_id = self._chat_id_from_context(graph_context, base_conversation_id)
                graph_messages, has_more = await self._paginate_graph_chat_messages(
                    chat_id, limit, direction, options.cursor
                )

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

    async def list_threads(
        self,
        channel_id: str,
        options: ListThreadsOptions | dict[str, Any] | None = None,
    ) -> ListThreadsResult:
        """List threads in a Teams channel (or chat) via Microsoft Graph API.

        Python port of upstream ``GraphReader.listThreads``
        (adapter-teams/src/graph-api.ts:367-516, surfaced by index.ts:1357).
        Each top-level Graph message becomes a :class:`ThreadSummary` whose
        ``id`` is the per-message thread ID (``{baseConversationId};messageid=
        {msg.id}``) and whose ``root_message`` is the mapped message. The
        channel path additionally carries ``last_reply_at`` (the message's
        ``lastModifiedDateTime``); the chat path does not.
        """
        if options is None:
            options = ListThreadsOptions()
        elif isinstance(options, dict):
            options = ListThreadsOptions(**options)

        decoded = self.decode_thread_id(channel_id)
        conversation_id = decoded.conversation_id
        service_url = decoded.service_url
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", conversation_id)
        limit = options.limit if options.limit is not None else 50

        try:
            graph_context = await self._get_graph_context(base_conversation_id)
            context_type = graph_context.get("type") if graph_context else None

            self._logger.debug(
                "Teams Graph API: listThreads",
                {
                    "conversationId": base_conversation_id,
                    "contextType": context_type or "none",
                    "limit": limit,
                },
            )

            threads: list[ThreadSummary] = []

            if graph_context and context_type != "dm":
                channel_context = cast(TeamsChannelContext, graph_context)
                graph_messages = await self._graph_list_channel_messages(
                    channel_context["team_id"],
                    channel_context["channel_id"],
                    limit=limit,
                )
                for msg in graph_messages:
                    if not msg.get("id"):
                        continue
                    thread_id = self.encode_thread_id(
                        TeamsThreadId(
                            conversation_id=f"{base_conversation_id};messageid={msg['id']}",
                            service_url=service_url,
                        )
                    )
                    last_modified = msg.get("lastModifiedDateTime")
                    threads.append(
                        ThreadSummary(
                            id=thread_id,
                            root_message=self._map_graph_message(msg, thread_id),
                            last_reply_at=(_parse_iso(last_modified) if last_modified else None),
                        )
                    )
            else:
                chat_id = self._chat_id_from_context(graph_context, base_conversation_id)
                graph_messages = await self._graph_list_chat_messages(
                    chat_id,
                    {"$top": limit, "$orderby": "createdDateTime desc"},
                )
                for msg in graph_messages:
                    if not msg.get("id"):
                        continue
                    thread_id = self.encode_thread_id(
                        TeamsThreadId(
                            conversation_id=f"{base_conversation_id};messageid={msg['id']}",
                            service_url=service_url,
                        )
                    )
                    threads.append(
                        ThreadSummary(
                            id=thread_id,
                            root_message=self._map_graph_message(msg, thread_id),
                        )
                    )

            self._logger.debug("Teams Graph API: listThreads result", {"threadCount": len(threads)})
            return ListThreadsResult(threads=threads)

        except Exception as error:
            self._logger.error("Teams Graph API: listThreads error", {"error": str(error)})
            raise

    async def post_channel_message(
        self,
        channel_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Teams channel's base conversation (top-level).

        Python port of upstream ``postChannelMessage``
        (adapter-teams/src/index.ts:1368-1430). The ``;messageid=`` suffix is
        stripped so the activity lands on the base channel conversation rather
        than threaded under a specific root message. Supports card, file, and
        plain-text postable shapes, mirroring :meth:`post_message`.
        """
        decoded = self.decode_thread_id(channel_id)
        base_conversation_id = MESSAGEID_STRIP_PATTERN.sub("", decoded.conversation_id)

        files = extract_files(message)
        file_attachments = await self._files_to_attachments(files) if files else []

        card = extract_card(message)
        if card:
            adaptive_card = card_to_adaptive_card(card)
            activity_payload: dict[str, Any] = {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": adaptive_card,
                    },
                    *file_attachments,
                ],
            }

            try:
                self._point_app_api_at(decoded.service_url)
                sent = await self._app.send(
                    base_conversation_id,
                    self._message_activity_input(activity_payload),
                )
                return RawMessage(
                    id=getattr(sent, "id", "") or "",
                    thread_id=channel_id,
                    raw=activity_payload,
                )
            except Exception as error:
                self._logger.error(
                    "Teams API: postChannelMessage failed",
                    {"conversationId": base_conversation_id, "error": str(error)},
                )
                _handle_teams_error(error, "postChannelMessage")
                raise  # unreachable: _handle_teams_error always raises

        text = convert_emoji_placeholders(
            self._format_converter.render_postable(message),
            "teams",
        )
        activity_payload = {
            "type": "message",
            "text": text,
            "textFormat": "markdown",
        }
        if file_attachments:
            activity_payload["attachments"] = file_attachments

        try:
            self._point_app_api_at(decoded.service_url)
            sent = await self._app.send(
                base_conversation_id,
                self._message_activity_input(activity_payload),
            )
            self._logger.debug("Teams API: postChannelMessage response", {"messageId": getattr(sent, "id", None)})
            return RawMessage(
                id=getattr(sent, "id", "") or "",
                thread_id=channel_id,
                raw=activity_payload,
            )
        except Exception as error:
            self._logger.error(
                "Teams API: postChannelMessage failed",
                {"conversationId": base_conversation_id, "error": str(error)},
            )
            _handle_teams_error(error, "postChannelMessage")
            raise  # unreachable: _handle_teams_error always raises

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

        # vercel/chat#403: only call into the Graph teams/channels
        # endpoint for true channel contexts. A cached DM context (now
        # possible when ``aadObjectId`` was present on the activity)
        # must not be treated as a channel.
        graph_context = await self._get_graph_context(base_conversation_id) if not is_dm else None
        channel_context: TeamsChannelContext | None = None
        if graph_context and graph_context.get("type") != "dm":
            channel_context = cast(TeamsChannelContext, graph_context)

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

        # Join on a single ``/``: an Emulator serviceUrl (``http://localhost:N``)
        # has no trailing slash, so plain concatenation would yield
        # ``http://localhost:Nv3/conversations``.
        url = f"{service_url.rstrip('/')}/v3/conversations"

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

    async def _get_graph_context(self, base_conversation_id: str) -> TeamsGraphContext | None:
        """Look up cached Microsoft Graph context for a conversation.

        Returns either a :class:`TeamsChannelContext` (channel/team
        thread) or a :class:`TeamsDmContext` (1:1 DM with a resolved
        Graph chat ID). For group chats, no entry is cached — the raw
        conversation ID works as-is with Graph's ``/chats`` endpoints.

        Backwards compat: cached entries written before vercel/chat#403
        omit the ``type`` discriminator and are treated as
        ``"channel"`` by :meth:`_chat_id_from_context` and the call
        sites that branch on context type.
        """
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

    @staticmethod
    def _chat_id_from_context(
        context: TeamsGraphContext | None,
        base_conversation_id: str,
    ) -> str:
        """Resolve the Microsoft Graph chat ID for a non-channel conversation.

        Uses the DM context's ``graph_chat_id`` when present, otherwise
        falls back to the raw Bot Framework conversation ID (which works
        as-is for group chats and the legacy pre-#403 cache shape).
        """
        if context is not None and context.get("type") == "dm":
            return cast("TeamsDmContext", context)["graph_chat_id"]
        return base_conversation_id

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

    async def _paginate_graph_chat_messages(
        self,
        chat_id: str,
        limit: int,
        direction: str,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Issue a single Graph ``/chats/{chat_id}/messages`` page and report has_more.

        Backward direction reverses the result so callers always see chronological
        order; cursor filter clause is ``gt`` for forward, ``lt`` for backward.
        """
        order_by = "createdDateTime asc" if direction == "forward" else "createdDateTime desc"
        filter_op = "gt" if direction == "forward" else "lt"
        params: dict[str, Any] = {"$top": limit, "$orderby": order_by}
        if cursor:
            params["$filter"] = f"createdDateTime {filter_op} {cursor}"
        graph_messages = await self._graph_list_chat_messages(chat_id, params)
        if direction != "forward":
            graph_messages.reverse()
        return graph_messages, len(graph_messages) >= limit

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
        """Get a Microsoft Graph API access token (OAuth2 client credentials).

        Caches on the DEDICATED ``_graph_token`` / ``_graph_token_expiry``
        fields — never the Bot Framework ``_access_token`` field. The two
        tokens carry different scopes (``graph.microsoft.com`` vs
        ``api.botframework.com``); sharing one cache slot let whichever was
        fetched last clobber the other, so a Graph read could end up sending a
        Bot Framework token (and vice versa). See issue #93.

        With a custom ``token`` factory configured, the factory is called
        instead (scope ``https://graph.microsoft.com/.default``) and
        ``app_password`` is never read (upstream routes Graph through the SDK
        ``App``, which uses the same factory). The scope stays the public
        Graph scope even under a sovereign ``CLOUD``: every hand-rolled Graph
        read targets ``https://graph.microsoft.com`` (sovereign Graph routing
        for these reads is a pre-existing gap), and the token audience must
        match the host it is sent to.
        """
        import time as _time

        if self._app_instance is None:
            # A callable ``app_id`` is still unresolved: there is no client id
            # to mint a token for yet.
            raise ValidationError("teams", _APP_ID_UNRESOLVED_MESSAGE)
        if self._config.token is not None:
            return await self._token_from_factory(_GRAPH_SCOPE)

        # Reuse cached token if valid
        if self._graph_token and _time.time() < self._graph_token_expiry:
            return self._graph_token

        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            if self._graph_token and _time.time() < self._graph_token_expiry:
                return self._graph_token

            tenant_id = self._app_tenant_id or "botframework.com"
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

            session = await self._get_http_session()
            async with session.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._app_id,
                    "client_secret": self._app_password,
                    "scope": _GRAPH_SCOPE,
                },
            ) as response:
                if not response.ok:
                    error_text = await response.text()
                    raise AuthenticationError(
                        "teams",
                        f"Failed to get Graph API token: {response.status} {error_text}",
                    )
                data = await response.json()
                self._graph_token = data["access_token"]
                self._graph_token_expiry = _time.time() + data.get("expires_in", 3600) - 300
                return self._graph_token  # type: ignore[return-value]

    # =========================================================================
    # Teams Bot Framework HTTP API helpers
    # =========================================================================

    async def _get_access_token(self) -> str:
        """Get a Bot Framework access token (OAuth2 client credentials).

        Scope ``api.botframework.com``, cached on ``_access_token`` /
        ``_token_expiry``. The migrated outbound paths
        (post/edit/delete/typing) now mint their Bot Framework token through
        the SDK ``App``, so this hand-rolled token is consumed only by the
        still-hand-rolled :meth:`open_dm` REST call. It must never share a
        cache slot with the Graph token (see :meth:`_get_graph_token`).

        With a custom ``token`` factory configured, the factory is called
        instead and ``app_password`` is never read. The scope is the SDK
        ``App``'s cloud Bot Framework scope (``https://api.botframework.com/.default``
        on the public cloud, ``https://api.botframework.us/.default`` under
        ``CLOUD=USGov`` …), the same one the SDK asks the factory for, since
        :meth:`open_dm` posts to that cloud's Connector.
        """
        import time

        if self._app_instance is None:
            # A callable ``app_id`` is still unresolved: there is no client id
            # to mint a token for yet.
            raise ValidationError("teams", _APP_ID_UNRESOLVED_MESSAGE)
        if self._config.token is not None:
            cloud_scope = getattr(getattr(self._app, "cloud", None), "bot_scope", None)
            scope = cloud_scope if isinstance(cloud_scope, str) and cloud_scope else _BOT_FRAMEWORK_SCOPE
            return await self._token_from_factory(scope)

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
                        "scope": _BOT_FRAMEWORK_SCOPE,
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

    def _factory_tenant_id(self, scope: str) -> str:
        """Tenant passed to the ``token`` factory, resolved exactly as the SDK does.

        Mirrors ``TokenManager._resolve_tenant_id`` in ``microsoft-teams-apps``:
        the tenant the SDK ``App`` was given (``credentials.tenant_id`` — unset
        for ``app_type="MultiTenant"``, which omits it), else the cloud's login
        tenant (``botframework.com`` on the public cloud) for the Bot Framework
        scope and ``common`` for the Graph scope. Using the same rule keeps a
        factory that routes or allow-lists by tenant from seeing one scope under
        two tenants depending on whether the SDK or a hand-rolled path asked.
        """
        app = self._app
        credentials_tenant = getattr(getattr(app, "credentials", None), "tenant_id", None)
        if isinstance(credentials_tenant, str) and credentials_tenant:
            return credentials_tenant
        if scope == _GRAPH_SCOPE:
            return _GRAPH_DEFAULT_TENANT
        login_tenant = getattr(getattr(app, "cloud", None), "login_tenant", None)
        return login_tenant if isinstance(login_tenant, str) and login_tenant else "botframework.com"

    async def _token_from_factory(self, scope: str) -> str:
        """Mint a token through the configured ``token`` factory.

        Called as ``token(scope, tenant_id)`` — the same contract the Teams SDK
        uses for ``AppOptions.token`` — with the tenant the SDK itself would
        pass for that scope (see :meth:`_factory_tenant_id`). The result is not
        cached: the factory owns token lifetime, as it does for the SDK's own
        calls. A non-string or empty result, or a raising factory, becomes an
        :class:`AuthenticationError`.
        """
        factory = self._config.token
        if factory is None:  # pragma: no cover - callers check first
            raise AuthenticationError("teams", "No custom token factory is configured")
        tenant_id = self._factory_tenant_id(scope)
        try:
            result: Any = factory(scope, tenant_id)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            raise AuthenticationError("teams", f"Custom token factory failed for {scope}: {exc}") from exc
        if not isinstance(result, str) or not result:
            raise AuthenticationError("teams", f"Custom token factory returned no token for {scope}")
        return result


def create_teams_adapter(config: TeamsAdapterConfig | None = None) -> TeamsAdapter:
    """Factory function to create a Teams adapter."""
    return TeamsAdapter(config)
