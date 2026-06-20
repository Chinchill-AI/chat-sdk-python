"""Linear adapter for chat SDK.

Supports comment threads on Linear issues.
Authentication via personal API key, OAuth access token, or client credentials.

Python port of packages/adapter-linear/src/index.ts.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, cast

from chat_sdk.adapters.linear.cards import card_to_linear_markdown
from chat_sdk.adapters.linear.format_converter import LinearFormatConverter
from chat_sdk.adapters.linear.types import (
    AgentActivityWebhookPayload,
    AgentSessionEventWebhookPayload,
    AgentSessionUserChild,
    AgentSessionWebhookPayload,
    CommentWebhookPayload,
    LinearActorData,
    LinearAdapterBaseConfig,
    LinearAdapterConfig,
    LinearAdapterMode,
    LinearAgentSessionCommentRawMessage,
    LinearAgentSessionThreadId,
    LinearCommentData,
    LinearCommentRawMessage,
    LinearInstallation,
    LinearRawMessage,
    LinearThreadId,
    LinearWebhookActor,
    ReactionWebhookPayload,
)

# AES-256-GCM token-at-rest encryption helpers. These live under the Slack
# adapter package but are platform-agnostic (a faithful port of upstream's
# shared ``adapter-shared/crypto.ts``); reuse them rather than duplicating the
# crypto primitives. ``cryptography`` is imported lazily inside encrypt/decrypt
# so adapters that don't configure a key don't require the dependency.
from chat_sdk.adapters.slack.crypto import (
    EncryptedTokenData,
    decode_key,
    decrypt_token,
    encrypt_token,
    is_encrypted_token_data,
)
from chat_sdk.emoji import convert_emoji_placeholders
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card
from chat_sdk.shared.errors import (
    AdapterError,
    AuthenticationError,
    NetworkError,
    ValidationError,
)
from chat_sdk.types import (
    AdapterPostableMessage,
    Author,
    ChatInstance,
    EmojiValue,
    FetchOptions,
    FetchResult,
    FormattedContent,
    LockScope,
    Message,
    MessageMetadata,
    PostableRaw,
    RawMessage,
    StreamOptions,
    ThreadInfo,
    UserInfo,
    WebhookOptions,
    _parse_iso,
)

# Anchored thread-id patterns (most-specific first). Faithful ports of the
# upstream regexes in ``adapter-linear/src/index.ts`` (4.31/#151). All three
# carry explicit ``^...$`` anchors so ``.match()`` is a *full* match — an
# un-anchored pattern would mis-parse a thread id (e.g. silently truncate a
# trailing ``:s:{session}`` segment). The decode order COMMENT_SESSION →
# ISSUE_SESSION → COMMENT → bare-issue is load-bearing: each later form is a
# prefix-shaped subset of an earlier one, so a wrong order mis-routes ids.
COMMENT_SESSION_THREAD_PATTERN = re.compile(r"^([^:]+):c:([^:]+):s:([^:]+)$")
COMMENT_THREAD_PATTERN = re.compile(r"^([^:]+):c:([^:]+)$")
ISSUE_SESSION_THREAD_PATTERN = re.compile(r"^([^:]+):s:([^:]+)$")

# Linear profile URL → display name. Faithful port of upstream
# ``PROFILE_URL_REGEX`` (utils.ts:34). Anchored at the start (``^``) and stops
# the captured slug at the first ``/``, ``?``, or ``#`` so query strings /
# fragments / trailing path segments are excluded.
PROFILE_URL_REGEX = re.compile(r"^https://linear\.app/\S+/profiles/([^/?#]+)")

# Linear GraphQL API endpoint
LINEAR_API_URL = "https://api.linear.app/graphql"

# Linear OAuth token endpoint
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"

# JS ``String.prototype.trim()`` whitespace set. Upstream's
# ``flushMarkdown`` computes ``markdown.slice(...).trim()``; Python's bare
# ``str.strip()`` strips a broader Unicode set (e.g. the C0 separators
# ``\x1c``-``\x1f`` and NEL ``\x85``, which JS keeps) and does NOT strip the
# BOM (which JS removes). Passing this explicit string to ``strip`` matches JS
# character-for-character. Canonical definition lives alongside the Telegram
# adapter (``adapters/telegram/rich.py``); duplicated here (not imported) to
# avoid a cross-adapter import for a single shared literal.
_JS_WHITESPACE = (
    "\t\n\v\f\r "  # \t \n \v \f \r and SPACE
    " "  # NO-BREAK SPACE
    " "  # OGHAM SPACE MARK
    "           "  # EN QUAD..HAIR SPACE
    " "  # LINE SEPARATOR
    " "  # PARAGRAPH SEPARATOR
    " "  # NARROW NO-BREAK SPACE
    " "  # MEDIUM MATHEMATICAL SPACE
    "　"  # IDEOGRAPHIC SPACE
    "﻿"  # ZERO WIDTH NO-BREAK SPACE (BOM)
)

# Agent-activity create mutation. The Python adapter has no ``@linear/sdk``;
# ``createAgentActivity`` is ported as this raw GraphQL mutation. Schema-hardened
# against Linear's published GraphQL schema (https://linear.app/developers/
# agent-interaction , https://linear.app/developers/graphql): the mutation is
# ``agentActivityCreate(input: AgentActivityCreateInput!)`` where ``content`` is
# a ``JSONObject!`` scalar (so ``type``/``body``/``action``/... are passed inline
# as a JSON object with LOWERCASE ``type`` enum values — "response"/"thought"/
# "error"/"action"), ``agentSessionId: String!`` and ``ephemeral: Boolean``. The
# return selection requests only schema-valid fields that
# ``_parse_message_from_agent_activity`` reads off the resolved ``sourceComment``.
# NOTE: ``agentSessionId`` is NOT a scalar field on Linear's ``AgentActivity``
# type — the schema exposes only the relation ``agentSession: AgentSession!``.
# GraphQL strict-validates selections, so requesting a non-existent
# ``agentSessionId`` field server-rejects the whole mutation; select the
# ``agentSession { id }`` relation instead.
_AGENT_ACTIVITY_CREATE_MUTATION = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
    agentActivityCreate(input: $input) {
        success
        agentActivity {
            id
            agentSession {
                id
            }
            sourceComment {
                id
                body
                parentId
                createdAt
                updatedAt
                url
                user {
                    id
                    displayName
                    name
                    email
                    avatarUrl
                }
                botActor {
                    id
                    name
                    userDisplayName
                    avatarUrl
                }
            }
        }
    }
}
"""

# Agent-session plan-update mutation. Ports ``updateAgentSession``. Schema-
# hardened: ``agentSessionUpdate(id: String!, input: AgentSessionUpdateInput!)``
# where ``input.plan`` is an array of ``{content, status}`` items (status
# ``"completed"`` from upstream).
_AGENT_SESSION_UPDATE_MUTATION = """
mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
    agentSessionUpdate(id: $id, input: $input) {
        success
    }
}
"""

# Agent-session FETCH query. The Python adapter has no ``@linear/sdk``;
# upstream's ``linear.agentSession(id)`` (which lazy-resolves ``issueId`` and the
# root ``comment`` relation off the SDK model) is ported as this raw GraphQL
# query. Schema-hardened against Linear's published GraphQL schema
# (``linear/packages/sdk/src/schema.graphql`` @ master): the root query is
# ``agentSession(id: String!): AgentSession!``; ``AgentSession`` exposes ``id:
# ID!`` and the nullable ``comment: Comment`` relation.
#
# CRITICAL — ``AgentSession`` has NO scalar ``issueId`` field in the published
# schema (it exposes only the ``issue: Issue`` relation). Upstream's
# ``agentSession.issueId`` works because the SDK model derives ``issueId`` from
# the serialized object; in raw GraphQL, requesting a non-existent ``issueId``
# field server-rejects the WHOLE query (the L4 blocking-bug class). So we select
# the schema-valid ``issue { id }`` relation and derive the issue id from it —
# equivalent to upstream's ``agentSession.issueId``, and the same fallback shape
# upstream itself uses elsewhere (``agentSession.issueId ?? agentSession.issue?.id``
# at index.ts:959). The ``comment { ... }`` selection requests exactly the
# sub-fields the author/metadata resolution (``_raw_message_from_source_comment``,
# a faithful ``parseMessageFromComment``) reads off the root comment.
_AGENT_SESSION_FETCH_QUERY = """
query AgentSession($id: String!) {
    agentSession(id: $id) {
        id
        issue {
            id
        }
        comment {
            id
            body
            parentId
            createdAt
            updatedAt
            url
            user {
                id
                displayName
                name
                email
                avatarUrl
            }
            botActor {
                id
                name
                userDisplayName
                avatarUrl
            }
        }
    }
}
"""

# Agent-session children FETCH query. Ports upstream's
# ``linear.comments({ filter: { parent: { id: { eq: rootComment.id } } }, first|last })``
# (index.ts:1793). Schema-hardened: the root ``comments(filter: CommentFilter,
# first: Int, last: Int): CommentConnection!`` query exists, and
# the ``CommentFilter.parent: NullableCommentFilter`` → ``.id: IDComparator`` →
# ``.eq: ID`` chain is all schema-valid. Pagination is direction-driven —
# ``forward`` → ``first``, otherwise ``last`` (upstream's ternary). Upstream
# passes ONLY ``first``/``last`` here — it never reads ``options.cursor`` — and
# the sibling ``_fetch_issue_comments``/``_fetch_comment_thread`` paths likewise
# forward no cursor, so no ``after`` is sent. The same ``Comment`` sub-fields as
# the root-comment selection plus ``pageInfo { hasNextPage endCursor }`` (both
# published ``PageInfo`` fields).
_AGENT_SESSION_CHILDREN_QUERY = """
query AgentSessionComments(
    $filter: CommentFilter
    $first: Int
    $last: Int
) {
    comments(filter: $filter, first: $first, last: $last) {
        nodes {
            id
            body
            parentId
            createdAt
            updatedAt
            url
            user {
                id
                displayName
                name
                email
                avatarUrl
            }
            botActor {
                id
                name
                userDisplayName
                avatarUrl
            }
        }
        pageInfo {
            hasNextPage
            endCursor
        }
    }
}
"""

# Emoji mapping for Linear reactions (unicode)
EMOJI_MAPPING: dict[str, str] = {
    "thumbs_up": "\U0001f44d",
    "thumbs_down": "\U0001f44e",
    "heart": "\u2764\ufe0f",
    "fire": "\U0001f525",
    "rocket": "\U0001f680",
    "eyes": "\U0001f440",
    "check": "\u2705",
    "warning": "\u26a0\ufe0f",
    "sparkles": "\u2728",
    "wave": "\U0001f44b",
    "raised_hands": "\U0001f64c",
    "laugh": "\U0001f604",
    "hooray": "\U0001f389",
    "confused": "\U0001f615",
}


def get_user_name_from_profile_url(url: str) -> str:
    """Extract a user display name from a Linear profile URL.

    Faithful port of upstream ``getUserNameFromProfileUrl`` (utils.ts:40). A bit
    of a hack to avoid fetching the user just to get the display name: the slug
    after ``/profiles/`` in a Linear profile URL is the user's name. Returns
    ``""`` (NOT ``None``) when the URL does not match — upstream returns the
    empty string so the author's ``userName`` falls back to "" rather than
    propagating an undefined.
    """
    match = PROFILE_URL_REGEX.match(url)
    if not match:
        return ""
    return match.group(1)


def assert_agent_session_thread(
    thread: LinearThreadId,
) -> LinearAgentSessionThreadId:
    """Narrow a decoded thread to the agent-session case before session-only work.

    Faithful port of upstream ``assertAgentSessionThread`` (``utils.ts``). The TS
    signature is ``asserts thread is LinearAgentSessionThreadId`` (a void
    type-guard). Python has no in-place assertion narrowing for dataclasses, so
    this returns the same thread re-typed as ``LinearAgentSessionThreadId`` —
    callers can either ignore the return (assertion side-effect) or bind it to
    get the narrowed type. Raises ``ValidationError`` when the thread carries no
    ``agent_session_id``, matching the upstream message byte-for-byte.
    """
    if not thread.agent_session_id:
        raise ValidationError("linear", "Expected a Linear agent session thread")
    return cast("LinearAgentSessionThreadId", thread)


class LinearAdapter:
    """Linear adapter for chat SDK.

    Implements the Adapter interface for Linear issue comments.
    """

    def __init__(self, config: LinearAdapterConfig | None = None) -> None:
        if config is None:
            config = LinearAdapterBaseConfig()

        webhook_secret = getattr(config, "webhook_secret", None) or os.environ.get("LINEAR_WEBHOOK_SECRET")
        if not webhook_secret:
            raise ValidationError(
                "linear",
                "webhook_secret is required. Set LINEAR_WEBHOOK_SECRET or provide it in config.",
            )

        self._name = "linear"
        # Custom Linear GraphQL endpoint (proxy / mock / self-host). Faithful
        # port of upstream ``config.apiUrl ?? process.env.LINEAR_API_URL``
        # (index.ts:239), consumed via the truthy spread
        # ``...(this.apiUrl ? { apiUrl } : {})`` at every ``LinearClient``
        # construction — so an empty string falls back to the default endpoint.
        # We have no LinearClient -- ``_graphql_query`` POSTs raw GraphQL -- so
        # the override substitutes for the module-level ``LINEAR_API_URL``
        # default. The truthy check means an empty ``apiUrl`` (or env) uses the
        # default rather than POSTing to an empty/relative URL.
        config_api_url = getattr(config, "api_url", None)
        self._api_url: str = config_api_url or os.environ.get("LINEAR_API_URL") or LINEAR_API_URL
        self._webhook_secret = webhook_secret
        self._logger: Logger = getattr(config, "logger", None) or ConsoleLogger("info", prefix="linear")
        self._user_name = getattr(config, "user_name", None) or os.environ.get("LINEAR_BOT_USERNAME", "linear-bot")
        # Inbound webhook handling model. Faithful port of upstream
        # ``this.mode = config.mode ?? "comments"`` (index.ts:236). "comments"
        # is the data-change webhook model (existing behavior); "agent-sessions"
        # is the app-actor model. The agent-session routing/emit/fetch logic
        # lands in later waves (L3/L4/L5); L1 only plumbs the field.
        config_mode: LinearAdapterMode | None = getattr(config, "mode", None)
        self._mode: LinearAdapterMode = config_mode if config_mode is not None else "comments"
        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None
        # Default organization ID, resolved at ``initialize`` from the viewer's
        # ``organization.id``. Faithful port of upstream
        # ``defaultOrganizationId`` (index.ts:206/347): single-tenant fallback
        # used by the agent-activity emit path when there is no per-request
        # installation context (no webhook payload to read ``organizationId``
        # off). ``None`` until the bot identity resolves.
        self._default_organization_id: str | None = None
        self._format_converter = LinearFormatConverter()

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        # Authentication state
        self._access_token: str | None = None
        self._access_token_expiry: float | None = None
        self._client_credentials: dict[str, str] | None = None
        self._token_lock = asyncio.Lock()

        # Determine auth method
        api_key = getattr(config, "api_key", None)
        access_token = getattr(config, "access_token", None)
        client_id = getattr(config, "client_id", None)
        client_secret = getattr(config, "client_secret", None)

        if api_key:
            self._access_token = api_key
        elif access_token:
            self._access_token = access_token
        elif client_id and client_secret:
            self._client_credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
            }
        else:
            # Auto-detect from env vars
            env_api_key = os.environ.get("LINEAR_API_KEY")
            if env_api_key:
                self._access_token = env_api_key
            else:
                env_access_token = os.environ.get("LINEAR_ACCESS_TOKEN")
                if env_access_token:
                    self._access_token = env_access_token
                else:
                    env_client_id = os.environ.get("LINEAR_CLIENT_ID")
                    env_client_secret = os.environ.get("LINEAR_CLIENT_SECRET")
                    if env_client_id and env_client_secret:
                        self._client_credentials = {
                            "client_id": env_client_id,
                            "client_secret": env_client_secret,
                        }
                    else:
                        raise ValidationError(
                            "linear",
                            "Authentication is required. Set LINEAR_API_KEY, LINEAR_ACCESS_TOKEN, "
                            "or LINEAR_CLIENT_ID/LINEAR_CLIENT_SECRET, or provide auth in config.",
                        )

        # State-key prefix for per-organization installations. Use ``is not
        # None`` (not truthiness) so an explicit ``installation_key_prefix=""``
        # is honored verbatim and NOT silently overridden by the default
        # (CLAUDE.md truthiness-trap hazard).
        prefix = getattr(config, "installation_key_prefix", None)
        self._installation_key_prefix = prefix if prefix is not None else "linear:installation"

        # Optional AES-256-GCM key for encrypting OAuth tokens at rest. Use
        # ``is not None`` (not truthiness) so an explicit ``encryption_key=""``
        # is treated as "user explicitly opted out" and is NOT silently
        # shadowed by ``LINEAR_ENCRYPTION_KEY`` from the env (CLAUDE.md
        # truthiness-trap hazard). An empty user value still short-circuits
        # ``decode_key`` via the ``if encryption_key_raw`` guard, so no broken
        # key is ever built.
        config_encryption_key = getattr(config, "encryption_key", None)
        if config_encryption_key is not None:
            encryption_key_raw: str | None = config_encryption_key
        else:
            encryption_key_raw = os.environ.get("LINEAR_ENCRYPTION_KEY")
        self._encryption_key: bytes | None = None
        if encryption_key_raw:
            self._encryption_key = decode_key(encryption_key_raw)

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
    def mode(self) -> LinearAdapterMode:
        """Inbound webhook handling model ("comments" or "agent-sessions").

        Faithful port of upstream ``protected readonly mode`` (index.ts:201).
        """
        return self._mode

    @property
    def lock_scope(self) -> LockScope | None:
        return None

    @property
    def persist_message_history(self) -> bool | None:
        return None

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch the bot's user ID."""
        self._chat = chat

        # For client credentials mode, fetch an access token first
        if self._client_credentials:
            await self._refresh_client_credentials_token()

        # Fetch the bot's user ID for self-message detection. Also capture the
        # viewer's ``organization.id`` (upstream resolves the same field from
        # client identity at init, index.ts:347/727) so the agent-activity emit
        # path has a default organization ID in single-tenant mode.
        try:
            viewer = await self._graphql_query("query { viewer { id displayName organization { id } } }")
            viewer_data = viewer.get("data", {}).get("viewer", {})
            self._bot_user_id = viewer_data.get("id")
            organization = viewer_data.get("organization") or {}
            self._default_organization_id = organization.get("id")
            self._logger.info(
                "Linear auth completed",
                {
                    "botUserId": self._bot_user_id,
                    "displayName": viewer_data.get("displayName"),
                },
            )
        except Exception as error:
            self._logger.warn("Could not fetch Linear bot user ID", {"error": str(error)})

    async def _refresh_client_credentials_token(self) -> None:
        """Fetch a new access token using client credentials grant."""
        if not self._client_credentials:
            return

        import aiohttp  # lazy import (needed for ClientError)

        try:
            session = await self._get_http_session()
            async with session.post(
                LINEAR_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_credentials["client_id"],
                    "client_secret": self._client_credentials["client_secret"],
                    "scope": "read,write,comments:create,issues:create",
                },
            ) as response:
                if not response.ok:
                    error_body = await response.text()
                    raise AuthenticationError(
                        "linear",
                        f"Failed to fetch Linear client credentials token: {response.status} {error_body}",
                    )

                data = await response.json()
                self._access_token = data["access_token"]
                # Track expiry with 1 hour buffer
                self._access_token_expiry = time.time() + data.get("expires_in", 86400) - 3600

                self._logger.info(
                    "Linear client credentials token obtained",
                    {
                        "expiresIn": f"{round(data.get('expires_in', 0) / 86400)} days",
                    },
                )
        except AuthenticationError:
            raise
        except aiohttp.ClientError as exc:
            raise NetworkError(
                "linear",
                f"Network error obtaining Linear client credentials token: {exc}",
                exc,
            ) from exc

    async def _ensure_valid_token(self) -> None:
        """Ensure the client credentials token is still valid. Refresh if expired."""
        if not (self._client_credentials and self._access_token_expiry and time.time() > self._access_token_expiry):
            return
        async with self._token_lock:
            # Double-check after acquiring lock to avoid redundant refreshes
            if self._access_token_expiry and time.time() > self._access_token_expiry:
                self._logger.info("Linear access token expired, refreshing...")
                await self._refresh_client_credentials_token()

    # ------------------------------------------------------------------
    # Multi-tenant installation persistence (with optional encryption)
    # ------------------------------------------------------------------

    def _installation_key(self, organization_id: str) -> str:
        return f"{self._installation_key_prefix}:{organization_id}"

    async def set_installation(self, organization_id: str, installation: LinearInstallation) -> None:
        """Persist a Linear installation for an organization.

        Used in multi-tenant mode after a successful OAuth exchange. When an
        ``encryption_key`` is configured, ``access_token`` / ``refresh_token``
        are AES-256-GCM-encrypted before being written to the state store.
        """
        if not self._chat:
            raise ValidationError(
                "linear",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        key = self._installation_key(organization_id)
        await state.set(key, self._encrypt_installation(installation))
        self._logger.info("Linear installation saved", {"organizationId": organization_id})

    async def get_installation(self, organization_id: str) -> LinearInstallation | None:
        """Retrieve a Linear installation for an organization.

        Decrypts ``access_token`` / ``refresh_token`` when they were stored as
        encrypted envelopes. Tolerates plaintext records written before
        encryption was enabled, so a key can be rotated in with zero downtime.
        """
        if not self._chat:
            raise ValidationError(
                "linear",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        stored = await state.get(self._installation_key(organization_id))
        if not stored or not isinstance(stored, dict):
            return None

        return self._decrypt_installation(stored)

    async def delete_installation(self, organization_id: str) -> None:
        """Remove a Linear installation. Used for uninstall handling."""
        if not self._chat:
            raise ValidationError(
                "linear",
                "Adapter not initialized. Ensure chat.initialize() has been called first.",
            )

        state = self._chat.get_state()
        await state.delete(self._installation_key(organization_id))
        self._logger.info("Linear installation deleted", {"organizationId": organization_id})

    def _encrypt_installation(self, installation: LinearInstallation) -> dict[str, Any]:
        """Serialize an installation for storage, encrypting tokens if a key is set.

        Without a key, tokens are stored as plaintext (legacy behavior).
        Field names use camelCase to match the persisted-JSON boundary
        convention (CLAUDE.md) and upstream's ``StoredLinearInstallation``.
        """
        data: dict[str, Any] = {
            "botUserId": installation.bot_user_id,
            "expiresAt": installation.expires_at,
            "organizationId": installation.organization_id,
        }

        if self._encryption_key:
            access = encrypt_token(installation.access_token, self._encryption_key)
            data["accessToken"] = {"iv": access.iv, "data": access.data, "tag": access.tag}
            if installation.refresh_token is not None:
                refresh = encrypt_token(installation.refresh_token, self._encryption_key)
                data["refreshToken"] = {"iv": refresh.iv, "data": refresh.data, "tag": refresh.tag}
        else:
            data["accessToken"] = installation.access_token
            if installation.refresh_token is not None:
                data["refreshToken"] = installation.refresh_token

        return data

    def _decrypt_installation(self, stored: dict[str, Any]) -> LinearInstallation:
        """Reconstruct an installation from storage, decrypting tokens as needed."""
        # Tolerate both camelCase (this port / upstream) and snake_case keys.
        raw_access = stored.get("accessToken")
        if raw_access is None:
            raw_access = stored.get("access_token")
        raw_refresh = stored.get("refreshToken")
        if raw_refresh is None:
            raw_refresh = stored.get("refresh_token")

        access_token = self._maybe_decrypt(raw_access)
        refresh_token = None if raw_refresh is None else self._maybe_decrypt(raw_refresh)

        return LinearInstallation(
            access_token=access_token if access_token is not None else "",
            organization_id=stored.get("organizationId") or stored.get("organization_id") or "",
            bot_user_id=stored.get("botUserId") or stored.get("bot_user_id"),
            expires_at=stored.get("expiresAt") if stored.get("expiresAt") is not None else stored.get("expires_at"),
            refresh_token=refresh_token,
        )

    def _maybe_decrypt(self, value: Any) -> str | None:
        """Decrypt an encrypted-token envelope, or pass through a plaintext value.

        Plaintext tolerance: a value that is not an encrypted envelope (e.g. a
        record written before encryption was enabled) is returned unchanged.

        Raises:
            ValidationError: if an encrypted envelope is read but no key is
                configured -- a clear error instead of returning ciphertext.
        """
        if value is None:
            return None
        if is_encrypted_token_data(value):
            if not self._encryption_key:
                raise ValidationError(
                    "linear",
                    "Stored Linear installation token is encrypted but no encryption_key is "
                    "configured. Set LINEAR_ENCRYPTION_KEY (or config.encryption_key) to the key "
                    "used when the installation was saved.",
                )
            return decrypt_token(
                EncryptedTokenData(iv=value["iv"], data=value["data"], tag=value["tag"]),
                self._encryption_key,
            )
        # Plaintext value (legacy / no encryption configured at write time).
        return value if isinstance(value, str) else str(value)

    async def get_user(self, user_id: str) -> UserInfo | None:
        """Look up a Linear user by UUID via the GraphQL ``user`` query.

        Returns ``None`` on any failure (auth missing, user not found,
        network error). Mirrors upstream ``LinearAdapter.getUser``
        (vercel/chat#391), which uses the official Linear SDK; we issue
        the equivalent GraphQL query directly so we don't take a runtime
        dependency on the JS SDK.
        """
        try:
            await self._ensure_valid_token()
            data = await self._graphql_query(
                "query GetUser($id: String!) {  user(id: $id) {    id displayName name email avatarUrl  }}",
                {"id": user_id},
            )
        except Exception:
            return None
        user = (data.get("data") or {}).get("user") if isinstance(data, dict) else None
        if not user or not isinstance(user, dict):
            return None
        # Match upstream literally (vercel/chat#391):
        #   userName: user.displayName, fullName: user.name
        # Fall back to `user_id` when either field is missing — matches
        # the convention used by every other adapter's `get_user`
        # (slack/discord/github/teams/telegram) and satisfies the
        # non-Optional `str` typing of ``UserInfo.user_name`` /
        # ``UserInfo.full_name`` that JS's ``undefined`` would otherwise
        # violate.
        return UserInfo(
            user_id=user.get("id") or user_id,
            user_name=user.get("displayName") or user_id,
            full_name=user.get("name") or user_id,
            is_bot=False,
            avatar_url=user.get("avatarUrl"),
            email=user.get("email"),
        )

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle incoming webhook from Linear.

        See: https://linear.app/developers/webhooks
        """
        body = await self._get_request_body(request)
        self._logger.debug("Linear webhook raw body", {"body": body[:500] if body else ""})

        # Verify request signature (Linear-Signature header)
        signature = self._get_header(request, "linear-signature")
        if not self._verify_signature(body, signature):
            return self._make_response("Invalid signature", 401)

        try:
            payload: dict[str, Any] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self._logger.error(
                "Linear webhook invalid JSON",
                {
                    "contentType": self._get_header(request, "content-type"),
                    "bodyPreview": body[:200] if body else "",
                },
            )
            return self._make_response("Invalid JSON", 400)

        # Validate webhook timestamp (within 5 minutes)
        webhook_timestamp = payload.get("webhookTimestamp")
        if webhook_timestamp:
            time_diff = abs(int(time.time() * 1000) - webhook_timestamp)
            if time_diff > 5 * 60 * 1000:
                self._logger.warn(
                    "Linear webhook timestamp too old",
                    {
                        "webhookTimestamp": webhook_timestamp,
                        "timeDiff": time_diff,
                    },
                )
                return self._make_response("Webhook expired", 401)

        # Handle events based on type. The payload shape is determined by
        # `type` at runtime — cast to the matching TypedDict so each handler
        # sees the right variant.
        #
        # Mode-gating (faithful port of index.ts:1144-1165):
        #   - "Comment" events are only handled in mode="comments"
        #     (``this.mode !== "comments" || action !== "create"`` → return).
        #   - "AgentSessionEvent" events are only handled in
        #     mode="agent-sessions"; in any other mode we warn and ignore.
        # The gates are mutually exclusive, so a comment event in
        # agent-sessions mode (and vice-versa) is dropped — even when the body
        # @-mentions the bot's userName.
        payload_type = payload.get("type")
        if payload_type == "Comment":
            # Combined guard mirrors upstream's single `if` (mode + action). An
            # empty-string / wrong action and a non-"comments" mode both fall
            # through to the no-op without dispatching.
            if self._mode == "comments" and payload.get("action") == "create":
                self._handle_comment_created(cast("CommentWebhookPayload", payload), options)
        elif payload_type == "AgentSessionEvent":
            if self._mode != "agent-sessions":
                self._logger.warn(
                    "Received AgentSessionEvent webhook but adapter is not in agent-sessions mode, ignoring"
                )
            else:
                self._handle_agent_session_event(cast("AgentSessionEventWebhookPayload", payload), options)
        elif payload_type == "Reaction":
            self._handle_reaction(cast("ReactionWebhookPayload", payload))

        return self._make_response("ok", 200)

    def _verify_signature(self, body: str, signature: str | None) -> bool:
        """Verify Linear webhook signature using HMAC-SHA256."""
        if not signature:
            return False

        computed = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        try:
            return hmac.compare_digest(
                bytes.fromhex(computed),
                bytes.fromhex(signature),
            )
        except (ValueError, TypeError):
            return False

    def _handle_comment_created(
        self,
        payload: CommentWebhookPayload,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a new comment created on an issue."""
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring comment")
            return

        # TypedDict `.get()` unions every field-type from the union of shapes
        # (comment-created payloads vs older camel/snake fallbacks), producing
        # `object | str`. Cast to `str` where we've runtime-narrowed via the
        # truthy check — the dispatch block already filtered to `Comment`
        # events, so these keys are known to be strings.
        data = cast("LinearCommentData", payload.get("data", {}))
        actor = cast("LinearWebhookActor", payload.get("actor", {}))

        # Skip non-issue comments
        issue_id = cast("str | None", data.get("issueId") or data.get("issue_id"))
        if not issue_id:
            self._logger.debug("Ignoring non-issue comment", {"commentId": data.get("id")})
            return

        # Determine thread
        parent_id = data.get("parentId") or data.get("parent_id")
        root_comment_id = cast("str | None", parent_id or data.get("id"))
        thread_id = self.encode_thread_id(
            LinearThreadId(
                issue_id=issue_id,
                comment_id=root_comment_id,
            )
        )

        message = self._build_message(data, actor, thread_id)

        # Skip bot's own messages
        user_id = data.get("userId") or data.get("user_id")
        if user_id == self._bot_user_id:
            self._logger.debug("Ignoring message from self", {"messageId": data.get("id")})
            return

        self._chat.process_message(self, thread_id, message, options)

    def _parse_agent_session_message(
        self,
        raw: LinearAgentSessionCommentRawMessage,
    ) -> Message:
        """Build a ``Message`` from an agent-session raw message.

        Faithful port of upstream ``parseMessage`` (index.ts:2026) for the
        ``agent_session_comment`` branch. The existing :meth:`parse_message`
        predates the upstream rewrite and does not reproduce the threadId
        encode / ``is_mention`` / structured-author behavior, so the
        agent-session path renders the ``Message`` here directly:

        - ``is_mention=True`` — agent-session comments directly target the bot,
          so upstream always treats them as mentions.
        - ``thread_id`` is re-encoded from the raw comment so the session
          segment (``:s:{agentSessionId}``) is present on the routed thread.
        - ``author`` is read from the structured ``comment.user`` written by
          :meth:`_parse_message_from_agent_session_event` (display name, full
          name, ``is_bot`` from ``type == "bot"``, ``is_me`` from bot-user-id).
        """
        comment = raw["comment"]
        text = cast("str", comment.get("body", ""))
        user: LinearActorData = cast("LinearActorData", comment.get("user", {}))

        thread_id = self.encode_thread_id(
            LinearThreadId(
                issue_id=cast("str", comment.get("issueId", "")),
                comment_id=cast("str | None", comment.get("id")),
                agent_session_id=raw["agentSessionId"],
            )
        )

        # createdAt / updatedAt are ISO strings (the "created" branch reads
        # `payload.createdAt` as a raw string — no Date cast). `edited` mirrors
        # upstream's `createdAt !== updatedAt`.
        created_at = cast("str", comment.get("createdAt", ""))
        updated_at = cast("str", comment.get("updatedAt", ""))

        author = Author(
            user_id=cast("str", user.get("id", "")),
            user_name=cast("str", user.get("displayName", "")),
            full_name=cast("str", user.get("fullName", "")),
            is_bot=user.get("type") == "bot",
            is_me=user.get("id") == self._bot_user_id,
        )

        return Message(
            id=cast("str", comment.get("id", "")),
            thread_id=thread_id,
            is_mention=True,
            text=text,
            formatted=self._format_converter.to_ast(text),
            author=author,
            metadata=MessageMetadata(
                date_sent=_parse_iso(created_at) if created_at else datetime.now(timezone.utc),
                edited=created_at != updated_at,
                edited_at=_parse_iso(updated_at) if (created_at != updated_at and updated_at) else None,
            ),
            attachments=[],
            raw=cast("LinearRawMessage", raw),
        )

    def _parse_message_from_agent_session_event(
        self,
        payload: AgentSessionEventWebhookPayload,
    ) -> Message | None:
        """Parse an agent-session webhook event into a chat message, if applicable.

        Faithful port of upstream ``parseMessageFromAgentSessionEvent``
        (index.ts:955). Returns ``None`` (and logs a warning) when the event
        cannot be parsed. Handles two actions:

        - ``"prompted"`` — a user posting a follow-up in an existing session.
        - ``"created"`` — a user @-mentioning the bot, creating a new session.

        Any other action logs an "Unsupported agent session event action"
        warning and returns ``None``.
        """
        agent_session = cast("AgentSessionWebhookPayload", payload.get("agentSession", {}))

        # `issueId ?? issue?.id` — nullish (NOT truthy) fallback. Only fall back
        # to the nested issue.id when issueId is *absent*; an empty string would
        # be a real (if unusual) value, but we mirror upstream's `!issueId`
        # falsy guard below, which still bails on empty.
        issue_id = agent_session.get("issueId")
        if issue_id is None:
            issue = agent_session.get("issue")
            issue_id = issue.get("id") if issue is not None else None
        if not issue_id:
            return None

        action = payload.get("action")

        #
        # Follow-up message posted in an existing agent-session thread.
        #
        if action == "prompted":
            agent_activity = cast("AgentActivityWebhookPayload | None", payload.get("agentActivity"))
            if not agent_activity:
                self._logger.warn(
                    "Missing agent activity for prompted action",
                    {"agentSessionId": agent_session.get("id")},
                )
                return None

            source_comment_id = agent_activity.get("sourceCommentId")
            if not source_comment_id:
                self._logger.warn(
                    "Missing source comment ID for agent activity",
                    {
                        "agentSessionId": agent_session.get("id"),
                        "agentActivityId": agent_activity.get("id"),
                    },
                )
                return None

            content = agent_activity.get("content", {})
            activity_user = cast("AgentSessionUserChild", agent_activity.get("user", {}))
            # `agentActivity.user.avatarUrl ?? undefined` — nullish.
            avatar_url = activity_user.get("avatarUrl")
            # `parentId: payload.agentSession.comment?.id` — optional chain.
            # Short-circuit only on a missing/None comment (NOT a falsy empty
            # dict), mirroring `?.`; then read id (absent → None).
            prompted_session_comment = agent_session.get("comment")
            parent_id = prompted_session_comment.get("id") if prompted_session_comment is not None else None
            comment_data: LinearCommentData = {
                "id": cast("str", source_comment_id),
                "body": cast("str", content.get("body", "")),
                "issueId": cast("str", issue_id),
                "user": {
                    "type": "user",
                    "id": cast("str", activity_user.get("id", "")),
                    "displayName": get_user_name_from_profile_url(cast("str", activity_user.get("url", ""))),
                    "fullName": cast("str", activity_user.get("name", "")),
                    "email": cast("str", activity_user.get("email")),
                    **({"avatarUrl": cast("str", avatar_url)} if avatar_url is not None else {}),
                },
                "parentId": cast("str", parent_id),
                "createdAt": cast("str", agent_activity.get("createdAt", "")),
                "updatedAt": cast("str", agent_activity.get("createdAt", "")),
            }
            # `payload.agentSession.url ?? undefined` — nullish.
            session_url = agent_session.get("url")
            if session_url is not None:
                comment_data["url"] = cast("str", session_url)

            # `payload.promptContext ?? undefined` — nullish.
            prompt_context = payload.get("promptContext")
            raw: LinearAgentSessionCommentRawMessage = {
                "kind": "agent_session_comment",
                "organizationId": cast("str", payload.get("organizationId", "")),
                "comment": comment_data,
                "agentSessionId": cast("str", agent_session.get("id", "")),
            }
            if prompt_context is not None:
                raw["agentSessionPromptContext"] = cast("str", prompt_context)
            return self._parse_agent_session_message(raw)

        #
        # New session: a user mentions the bot in an issue, opening a session
        # and posting the first message.
        #
        if action == "created":
            # App-ownership guard. `agentSession.appUserId !== this.botUserId`.
            # We deliberately compare on the raw (possibly-None) values so a
            # mismatch with a foreign bot's appUserId is rejected. A None
            # botUserId only "matches" when appUserId is *also* None — that
            # cannot happen for a real created event (appUserId is always set),
            # so we never falsely accept a foreign session.
            app_user_id = agent_session.get("appUserId")
            if app_user_id != self._bot_user_id:
                self._logger.warn(
                    "Ignoring agent session event from another bot",
                    {
                        "agentSessionId": agent_session.get("id"),
                        "appUserId": app_user_id,
                    },
                )
                return None

            session_comment = agent_session.get("comment")
            if not session_comment:
                self._logger.warn(
                    "Missing comment for agent session",
                    {"agentSessionId": agent_session.get("id")},
                )
                return None

            creator = agent_session.get("creator")
            user: LinearActorData
            if creator:
                # `agentSession.creator.avatarUrl ?? undefined` — nullish.
                creator_avatar = creator.get("avatarUrl")
                user = {
                    "type": "user",
                    "id": cast("str", creator.get("id", "")),
                    "displayName": get_user_name_from_profile_url(cast("str", creator.get("url", ""))),
                    "fullName": cast("str", creator.get("name", "")),
                    "email": cast("str", creator.get("email")),
                    **({"avatarUrl": cast("str", creator_avatar)} if creator_avatar is not None else {}),
                }
            else:
                # No creator → fall back to the bot author (upstream uses
                # `this.botUserId` / `this.userName`). ``Author.user_id`` is a
                # non-Optional ``str``, so coerce a None bot-user-id (not yet
                # resolved by ``initialize``) to "" via ``is not None`` rather
                # than truthiness (CLAUDE.md hazard).
                user = {
                    "type": "bot",
                    "id": self._bot_user_id if self._bot_user_id is not None else "",
                    "displayName": self._user_name,
                    "fullName": self._user_name,
                }

            comment_data = {
                "id": cast("str", session_comment.get("id", "")),
                "body": cast("str", session_comment.get("body", "")),
                "issueId": cast("str", issue_id),
                "user": user,
                # The `created` branch reads `payload.createdAt` as a raw STRING
                # (no Date cast — upstream's `@ts-expect-error` notes the SDK
                # types are wrong about Date coercion for webhook payloads).
                "createdAt": cast("str", payload.get("createdAt", "")),
                "updatedAt": cast("str", payload.get("createdAt", "")),
            }
            # `payload.agentSession.url ?? undefined` — nullish.
            session_url = agent_session.get("url")
            if session_url is not None:
                comment_data["url"] = cast("str", session_url)

            # `payload.promptContext ?? undefined` — nullish.
            prompt_context = payload.get("promptContext")
            raw = {
                "kind": "agent_session_comment",
                "organizationId": cast("str", payload.get("organizationId", "")),
                "comment": comment_data,
                "agentSessionId": cast("str", agent_session.get("id", "")),
            }
            if prompt_context is not None:
                raw["agentSessionPromptContext"] = cast("str", prompt_context)
            return self._parse_agent_session_message(raw)

        self._logger.warn(
            "Unsupported agent session event action",
            {
                "action": action,
                "agentSessionId": agent_session.get("id"),
                "issueId": issue_id,
            },
        )
        return None

    def _handle_agent_session_event(
        self,
        payload: AgentSessionEventWebhookPayload,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle an agent-session webhook event.

        Faithful port of upstream ``handleAgentSessionEvent`` (index.ts:1269).
        Builds a message via :meth:`_parse_message_from_agent_session_event`
        and routes it to ``chat.process_message``. There is NO automatic
        acknowledgement — the bot does not auto-respond on receipt (no
        agentActivityCreate / typing / stream side-effect here; those land in
        L4). When the event cannot be parsed, logs a warning and returns.
        """
        if not self._chat:
            self._logger.warn("Chat instance not initialized, ignoring agent session event")
            return

        message = self._parse_message_from_agent_session_event(payload)
        if not message:
            self._logger.warn(
                "Unable to build message for Linear agent session event",
                {"agentSessionId": payload.get("agentSession", {}).get("id")},
            )
            return

        self._chat.process_message(self, message.thread_id, message, options)

    def _handle_reaction(self, payload: ReactionWebhookPayload) -> None:
        """Handle reaction events (logging only)."""
        if not self._chat:
            return

        data = payload.get("data", {})
        actor = payload.get("actor", {})

        self._logger.debug(
            "Received reaction webhook",
            {
                "reactionId": data.get("id"),
                "emoji": data.get("emoji"),
                "commentId": data.get("commentId") or data.get("comment_id"),
                "action": payload.get("action"),
                "actorName": actor.get("name"),
            },
        )

    def _build_message(
        self,
        comment: LinearCommentData,
        actor: LinearWebhookActor,
        thread_id: str,
    ) -> Message:
        """Build a Message from a Linear comment and actor."""
        # `comment.get("body")` unions every value type across the TypedDict
        # variants, giving `object | str`. Cast to `str` where the runtime
        # shape guarantees a string (Linear webhook `Comment` payloads
        # always have `body`, `userId`, `createdAt`, `updatedAt` as strings).
        text = cast("str", comment.get("body", ""))
        user_id = cast("str", comment.get("userId") or comment.get("user_id", ""))

        author = Author(
            user_id=user_id,
            user_name=actor.get("name", "unknown"),
            full_name=actor.get("name", "unknown"),
            is_bot=actor.get("type", "user") != "user",
            is_me=user_id == self._bot_user_id,
        )

        formatted = self._format_converter.to_ast(text)

        created_at = cast("str", comment.get("createdAt") or comment.get("created_at", ""))
        updated_at = cast("str", comment.get("updatedAt") or comment.get("updated_at", ""))

        return Message(
            id=comment.get("id", ""),
            thread_id=thread_id,
            text=text,
            formatted=formatted,
            raw=LinearCommentRawMessage(kind="comment", comment=comment),
            author=author,
            metadata=MessageMetadata(
                date_sent=_parse_iso(created_at) if created_at else datetime.now(timezone.utc),
                edited=created_at != updated_at,
                edited_at=_parse_iso(updated_at) if (created_at != updated_at and updated_at) else None,
            ),
            attachments=[],
        )

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a thread (create a comment on an issue)."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        # Render message to markdown
        card = extract_card(message)
        body = card_to_linear_markdown(card) if card else self._format_converter.render_postable(message)

        # Convert emoji placeholders to unicode
        body = convert_emoji_placeholders(body, "linear")

        # Agent-session branch (index.ts:1323). When the decoded thread carries a
        # session, post the reply as a "response" agent activity instead of a
        # comment, then resolve it back into a raw message from the activity's
        # ``sourceComment``. The comment path below is byte-identical to before.
        if decoded.agent_session_id:
            session = assert_agent_session_thread(decoded)
            activity_result = await self._create_agent_activity(
                session.agent_session_id,
                {"type": "response", "body": body},
            )
            return await self._parse_message_from_agent_activity(session, activity_result)

        # Create comment via GraphQL API
        result = await self._graphql_query(
            """
            mutation CommentCreate($input: CommentCreateInput!) {
                commentCreate(input: $input) {
                    success
                    comment {
                        id
                        body
                        url
                        createdAt
                        updatedAt
                    }
                }
            }
            """,
            {
                "input": {
                    "issueId": decoded.issue_id,
                    "body": body,
                    **({"parentId": decoded.comment_id} if decoded.comment_id else {}),
                }
            },
        )

        comment_data = result.get("data", {}).get("commentCreate", {}).get("comment")
        if not comment_data:
            raise AdapterError("Failed to create comment on Linear issue", "linear")

        return RawMessage(
            id=comment_data.get("id", ""),
            thread_id=thread_id,
            raw=LinearCommentRawMessage(
                kind="comment",
                comment={
                    "id": comment_data.get("id", ""),
                    "body": comment_data.get("body", ""),
                    "issueId": decoded.issue_id,
                    "userId": self._bot_user_id or "",
                    "createdAt": comment_data.get("createdAt", ""),
                    "updatedAt": comment_data.get("updatedAt", ""),
                    "url": comment_data.get("url"),
                },
            ),
        )

    async def _create_agent_activity(
        self,
        agent_session_id: str,
        content: dict[str, Any],
        *,
        ephemeral: bool | None = None,
    ) -> dict[str, Any]:
        """Create a Linear agent activity via raw GraphQL.

        Faithful port of ``@linear/sdk``'s ``createAgentActivity``
        (index.ts:1336/1522/1580/1608/1616). The TS SDK call shape is
        ``createAgentActivity({agentSessionId, content, ephemeral?})`` — here the
        same payload is posted as ``agentActivityCreate(input: ...)`` against the
        published schema, where ``content`` is a ``JSONObject!`` scalar carrying
        the lowercase ``type`` plus its body/action fields inline. ``ephemeral``
        is only included when explicitly set so the serialized input matches
        upstream's conditional spread (it is absent — not ``False`` — on the
        ``response``/``thought``/``error`` calls that don't pass it).

        Returns the raw ``agentActivityCreate`` payload node (``{success,
        agentActivity}``) — the same object upstream's ``AgentActivityPayload``
        models — for :meth:`_parse_message_from_agent_activity` to resolve.
        """
        activity_input: dict[str, Any] = {
            "agentSessionId": agent_session_id,
            "content": content,
        }
        if ephemeral is not None:
            activity_input["ephemeral"] = ephemeral

        result = await self._graphql_query(
            _AGENT_ACTIVITY_CREATE_MUTATION,
            {"input": activity_input},
        )
        return cast("dict[str, Any]", result.get("data", {}).get("agentActivityCreate", {}))

    async def _parse_message_from_agent_activity(
        self,
        thread: LinearAgentSessionThreadId,
        result: dict[str, Any],
    ) -> RawMessage:
        """Build a raw message from an ``agentActivityCreate`` mutation result.

        Faithful port of upstream ``parseMessageFromAgentActivity``
        (index.ts:1082). Resolves the created ``agentActivity`` and its
        ``sourceComment`` from the mutation payload, raising ``AdapterError`` with
        the exact upstream strings when the activity failed to create or its
        source comment could not be resolved. The resulting message is built off
        the source comment, mirroring upstream's delegation to
        ``parseMessageFromComment(sourceComment, issueId, agentSessionId)``.
        """
        activity = result.get("agentActivity")
        if not (result.get("success") and activity):
            raise AdapterError(
                f"Failed to create Linear agent activity for session {thread.agent_session_id}",
                "linear",
            )

        source_comment = activity.get("sourceComment")
        if not source_comment:
            raise AdapterError(
                f"Failed to resolve source comment for Linear agent activity {activity.get('id')}",
                "linear",
            )

        # The mutation selects the ``agentSession { id }`` relation (NOT a
        # non-existent scalar ``agentSessionId`` field — see
        # ``_AGENT_ACTIVITY_CREATE_MUTATION``); read the session id None-safely
        # off the nested relation node.
        activity_session_id = (activity.get("agentSession") or {}).get("id")
        if not activity_session_id:
            raise AdapterError(
                f"Missing agentSessionId for Linear agent activity {activity.get('id')}",
                "linear",
            )

        return self._raw_message_from_source_comment(
            source_comment,
            thread.issue_id,
            cast("str", activity_session_id),
        )

    def _raw_message_from_source_comment(
        self,
        comment: dict[str, Any],
        issue_id: str,
        agent_session_id: str,
    ) -> RawMessage:
        """Build the agent-session raw message from a resolved source comment.

        Faithful port of upstream ``parseMessageFromComment`` (index.ts:884) for
        the agent-activity emit path. Author resolution mirrors upstream: when
        the comment carries a ``user`` it is a user author; otherwise the comment
        was created by the app, so the ``botActor`` supplies a bot author (and,
        as upstream notes, an app comment without a botActor cannot determine an
        author). ``avatarUrl ?? undefined`` / ``botActor.id ?? this.botUserId``
        and the display/full-name fallbacks are nullish (``is not None`` / ``or``
        per upstream's ``??`` vs ``||``) faithful.
        """
        comment_user = comment.get("user")
        if comment_user:
            avatar_url = comment_user.get("avatarUrl")
            user: LinearActorData = {
                "type": "user",
                "id": cast("str", comment_user.get("id", "")),
                "displayName": cast("str", comment_user.get("displayName", "")),
                "fullName": cast("str", comment_user.get("name", "")),
                "email": cast("str", comment_user.get("email")),
                **({"avatarUrl": cast("str", avatar_url)} if avatar_url is not None else {}),
            }
        else:
            bot_actor = comment.get("botActor")
            if not bot_actor:
                raise AdapterError(
                    f"Comment {comment.get('id')} has no userId and no botActor, cannot determine author.",
                    "linear",
                )
            # ``botActor.id ?? this.botUserId`` — nullish. Coerce a None
            # bot-user-id (not yet resolved) to "" via ``is not None``.
            actor_id = bot_actor.get("id")
            resolved_id = actor_id if actor_id is not None else (self._bot_user_id or "")
            # ``userDisplayName ?? name ?? "unknown"`` / ``name ?? userDisplayName
            # ?? "unknown"`` — chained nullish.
            display_name = bot_actor.get("userDisplayName")
            actor_name = bot_actor.get("name")
            bot_avatar = bot_actor.get("avatarUrl")
            user = {
                "type": "bot",
                "id": cast("str", resolved_id),
                "displayName": cast(
                    "str",
                    display_name if display_name is not None else (actor_name if actor_name is not None else "unknown"),
                ),
                "fullName": cast(
                    "str",
                    actor_name if actor_name is not None else (display_name if display_name is not None else "unknown"),
                ),
                **({"avatarUrl": cast("str", bot_avatar)} if bot_avatar is not None else {}),
            }

        # ``parentId ?? undefined`` / ``url ?? undefined`` — nullish. createdAt /
        # updatedAt are ISO strings off the resolved comment node.
        parent_id = comment.get("parentId")
        comment_url = comment.get("url")
        comment_data: LinearCommentData = {
            "id": cast("str", comment.get("id", "")),
            "body": cast("str", comment.get("body", "")),
            "issueId": issue_id,
            "user": user,
            "createdAt": cast("str", comment.get("createdAt", "")),
            "updatedAt": cast("str", comment.get("updatedAt", "")),
        }
        if parent_id is not None:
            comment_data["parentId"] = cast("str", parent_id)
        if comment_url is not None:
            comment_data["url"] = cast("str", comment_url)

        raw: LinearAgentSessionCommentRawMessage = {
            "kind": "agent_session_comment",
            "organizationId": self._default_organization_id or "",
            "comment": comment_data,
            "agentSessionId": agent_session_id,
        }

        # The Python ``RawMessage`` is the minimal ``{id, thread_id, raw}`` wrapper
        # (no author field — unlike upstream's richer ``Message extends RawMessage``
        # return). The fully-parsed author/metadata live under ``raw`` and are
        # re-derived on read via :meth:`_parse_agent_session_message`. Encode the
        # routed thread id (carrying the ``:s:{session}`` segment) so callers can
        # round-trip it, matching the comment branch's ``thread_id`` semantics.
        # Upstream ``parseMessage`` (index.ts:2033-2038) and the adapter's own
        # read-path :meth:`_parse_agent_session_message` encode the source
        # comment's OWN id (``commentId: raw.comment.id``) — NOT its parentId.
        thread_id = self.encode_thread_id(
            LinearThreadId(
                issue_id=issue_id,
                comment_id=cast("str | None", comment_data.get("id")),
                agent_session_id=agent_session_id,
            )
        )
        return RawMessage(
            id=cast("str", comment_data["id"]),
            thread_id=thread_id,
            raw=cast("Any", raw),
        )

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing message (update a comment).

        Agent-session activities are append-only: upstream guards the session
        case first (index.ts:1408) and raises before any mutation. The comment
        path below is byte-identical to before.
        """
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        if decoded.agent_session_id:
            raise AdapterError(
                "Linear agent session activities are append-only and cannot be edited",
                "linear",
            )

        card = extract_card(message)
        body = card_to_linear_markdown(card) if card else self._format_converter.render_postable(message)

        body = convert_emoji_placeholders(body, "linear")

        result = await self._graphql_query(
            """
            mutation CommentUpdate($id: String!, $input: CommentUpdateInput!) {
                commentUpdate(id: $id, input: $input) {
                    success
                    comment {
                        id
                        body
                        url
                        createdAt
                        updatedAt
                    }
                }
            }
            """,
            {"id": message_id, "input": {"body": body}},
        )

        comment_data = result.get("data", {}).get("commentUpdate", {}).get("comment")
        if not comment_data:
            raise AdapterError("Failed to update comment on Linear", "linear")

        return RawMessage(
            id=comment_data.get("id", ""),
            thread_id=thread_id,
            raw=LinearCommentRawMessage(
                kind="comment",
                comment={
                    "id": comment_data.get("id", ""),
                    "body": comment_data.get("body", ""),
                    "issueId": decoded.issue_id,
                    "userId": self._bot_user_id or "",
                    "createdAt": comment_data.get("createdAt", ""),
                    "updatedAt": comment_data.get("updatedAt", ""),
                    "url": comment_data.get("url"),
                },
            ),
        )

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a message (delete a comment).

        Agent-session activities are append-only: upstream decodes the thread and
        guards the session case first (index.ts:1464), raising before any
        network call. The comment path below is byte-identical to before.
        """
        decoded = self.decode_thread_id(thread_id)
        if decoded.agent_session_id:
            raise AdapterError(
                "Linear agent session activities are append-only and cannot be deleted",
                "linear",
            )

        await self._ensure_valid_token()

        await self._graphql_query(
            """
            mutation CommentDelete($id: String!) {
                commentDelete(id: $id) {
                    success
                }
            }
            """,
            {"id": message_id},
        )

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a comment."""
        await self._ensure_valid_token()
        emoji_str = self._resolve_emoji(emoji)

        await self._graphql_query(
            """
            mutation ReactionCreate($input: ReactionCreateInput!) {
                reactionCreate(input: $input) {
                    success
                }
            }
            """,
            {"input": {"commentId": message_id, "emoji": emoji_str}},
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a comment (limited support)."""
        self._logger.warn("removeReaction is not fully supported on Linear - reaction ID lookup would be required")

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Start typing indicator.

        Faithful port of upstream ``startTyping`` (index.ts:1517). For
        agent-session threads this emits an ephemeral "thought" activity (the
        Linear-native typing equivalent); for standard comment threads it
        remains a warn-and-noop. ``status ?? "Thinking..."`` is nullish — an
        explicit empty-string status stays empty (NOT replaced by the default),
        so the ``is not None`` guard (not truthiness) is load-bearing.
        """
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        if decoded.agent_session_id:
            session = assert_agent_session_thread(decoded)
            await self._create_agent_activity(
                session.agent_session_id,
                {
                    "type": "thought",
                    "body": status if status is not None else "Thinking...",
                },
                ephemeral=True,
            )
            return

        self._logger.warn("startTyping is only supported in agent session threads. Ignoring for comment thread.")

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch messages from a thread."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        if options is None:
            options = FetchOptions()

        limit = options.limit if options.limit is not None else 50

        if decoded.agent_session_id:
            session = assert_agent_session_thread(decoded)
            return await self._fetch_agent_session_messages(session, options)

        if decoded.comment_id:
            return await self._fetch_comment_thread(thread_id, decoded.issue_id, decoded.comment_id, limit)

        return await self._fetch_issue_comments(thread_id, decoded.issue_id, limit)

    async def _fetch_agent_session_messages(
        self,
        thread: LinearAgentSessionThreadId,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch the visible comment thread associated with an agent session.

        Faithful port of upstream ``fetchAgentSessionMessages`` (index.ts:1771).
        The Python adapter has no ``@linear/sdk``; upstream's
        ``linear.agentSession(id)`` + ``linear.comments({filter})`` calls are
        ported as the schema-hardened raw GraphQL queries
        ``_AGENT_SESSION_FETCH_QUERY`` / ``_AGENT_SESSION_CHILDREN_QUERY``.

        - ``issue_id = agentSession.issue.id ?? thread.issue_id`` — nullish
          (``is not None``). The published schema has no scalar ``issueId`` on
          ``AgentSession`` (only the ``issue`` relation), so we read the issue id
          off ``issue { id }`` — equivalent to upstream's ``agentSession.issueId``.
          Raise ``AdapterError`` when neither yields an id.
        - ``root_comment = agentSession.comment`` — raise ``AdapterError`` when
          the session has no root comment.
        - children pagination is direction-driven: ``forward`` → ``first``,
          otherwise ``last`` (default limit 50), passing the
          ``{parent: {id: {eq: root_comment.id}}}`` filter.
        - each of ``[root_comment, *children.nodes]`` is parsed via the upstream
          ``parseMessageFromComment(comment, issue_id, agent_session.id)``
          semantics — reusing L4's ``_raw_message_from_source_comment`` (author
          user-vs-botActor resolution) + ``_parse_agent_session_message`` (the
          ``parseMessage`` agent-session branch). Each resulting message's
          ``thread_id`` therefore encodes the comment's OWN id plus the session
          segment: ``linear:{issue_id}:c:{comment.id}:s:{agent_session_id}`` —
          NOT a single fixed thread_id shared across messages — and is a mention.
        - ``next_cursor = endCursor if hasNextPage else None`` (upstream's
          ``hasNextPage ? (endCursor ?? undefined) : undefined``; ``is not None``).
        """
        limit = options.limit if (options is not None and options.limit is not None) else 50

        session_result = await self._graphql_query(
            _AGENT_SESSION_FETCH_QUERY,
            {"id": thread.agent_session_id},
        )
        agent_session = (session_result.get("data") or {}).get("agentSession")
        if not agent_session:
            # Port-only guard: upstream has no null-session branch — the
            # ``@linear/sdk`` ``linear.agentSession(id)`` throws its own
            # not-found. The raw-GraphQL port returns ``null`` instead, so this
            # guard describes the REAL failure (session not found), distinct from
            # the downstream missing-``issueId`` raise after the session resolves.
            raise AdapterError(
                f"Linear agent session {thread.agent_session_id} not found",
                "linear",
            )

        # ``agentSession.issueId ?? thread.issueId`` — but the published schema
        # exposes the issue id only via the ``issue`` relation (no scalar
        # ``issueId`` field), so read it off ``issue { id }``. Nullish, NOT
        # truthiness: an empty-string issue id would still short-circuit, but the
        # ``is not None`` guard matches upstream's ``??``.
        session_issue = agent_session.get("issue") or {}
        session_issue_id = session_issue.get("id")
        issue_id = session_issue_id if session_issue_id is not None else thread.issue_id
        if not issue_id:
            raise AdapterError(
                f"Linear agent session {thread.agent_session_id} is missing issueId",
                "linear",
            )

        root_comment = agent_session.get("comment")
        if not root_comment:
            raise AdapterError(
                f"Linear agent session {thread.agent_session_id} is missing a root comment",
                "linear",
            )

        agent_session_id = cast("str", agent_session.get("id", ""))

        # ``options?.direction === "forward" ? { first } : { last }`` — forward
        # paginates with ``first``, every other direction (incl. the default
        # ``backward``/unset) with ``last``. Send the unused bound as ``None`` so
        # GraphQL ignores it.
        forward = options is not None and options.direction == "forward"
        children_result = await self._graphql_query(
            _AGENT_SESSION_CHILDREN_QUERY,
            {
                "filter": {"parent": {"id": {"eq": root_comment.get("id")}}},
                "first": limit if forward else None,
                "last": None if forward else limit,
            },
        )

        children = ((children_result.get("data") or {}).get("comments")) or {}
        child_nodes = children.get("nodes") or []
        page_info = children.get("pageInfo") or {}

        # ``commentsToMessages([rootComment, ...children], issueId, agentSession.id)``
        # — each comment parsed via the ``parseMessageFromComment`` author logic
        # (reused from L4) and the ``parseMessage`` agent-session branch, so each
        # message encodes its OWN comment id in the thread id.
        messages: list[Message] = []
        for node in [root_comment, *child_nodes]:
            raw_message = self._raw_message_from_source_comment(node, issue_id, agent_session_id)
            messages.append(self._parse_agent_session_message(cast("Any", raw_message.raw)))

        end_cursor = page_info.get("endCursor")
        return FetchResult(
            messages=messages,
            next_cursor=end_cursor if page_info.get("hasNextPage") and end_cursor is not None else None,
        )

    async def _fetch_issue_comments(
        self,
        thread_id: str,
        issue_id: str,
        limit: int,
    ) -> FetchResult:
        """Fetch top-level comments on an issue."""
        result = await self._graphql_query(
            """
            query IssueComments($issueId: String!, $first: Int) {
                issue(id: $issueId) {
                    comments(first: $first) {
                        nodes {
                            id
                            body
                            createdAt
                            updatedAt
                            url
                            user {
                                id
                                displayName
                                name
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
            """,
            {"issueId": issue_id, "first": limit},
        )

        comments = result.get("data", {}).get("issue", {}).get("comments", {})
        nodes = comments.get("nodes", [])
        page_info = comments.get("pageInfo", {})

        messages = [self._comment_node_to_message(node, thread_id, issue_id) for node in nodes]

        return FetchResult(
            messages=messages,
            next_cursor=page_info.get("endCursor") if page_info.get("hasNextPage") else None,
        )

    async def _fetch_comment_thread(
        self,
        thread_id: str,
        issue_id: str,
        comment_id: str,
        limit: int,
    ) -> FetchResult:
        """Fetch a comment thread (root comment + its children/replies)."""
        result = await self._graphql_query(
            """
            query CommentThread($commentId: String!, $first: Int) {
                comment(id: $commentId) {
                    id
                    body
                    createdAt
                    updatedAt
                    url
                    user {
                        id
                        displayName
                        name
                    }
                    children(first: $first) {
                        nodes {
                            id
                            body
                            createdAt
                            updatedAt
                            url
                            user {
                                id
                                displayName
                                name
                            }
                        }
                        pageInfo {
                            hasNextPage
                            endCursor
                        }
                    }
                }
            }
            """,
            {"commentId": comment_id, "first": limit},
        )

        comment = result.get("data", {}).get("comment")
        if not comment:
            return FetchResult(messages=[])

        # Root comment as first message
        messages = [self._comment_node_to_message(comment, thread_id, issue_id)]

        # Child comments
        children = comment.get("children", {})
        for node in children.get("nodes", []):
            messages.append(self._comment_node_to_message(node, thread_id, issue_id))

        page_info = children.get("pageInfo", {})

        return FetchResult(
            messages=messages,
            next_cursor=page_info.get("endCursor") if page_info.get("hasNextPage") else None,
        )

    def _comment_node_to_message(
        self,
        node: dict[str, Any],
        thread_id: str,
        issue_id: str,
    ) -> Message:
        """Convert a GraphQL comment node to a Message."""
        user = node.get("user") or {}
        user_id = user.get("id", "unknown")

        return Message(
            id=node.get("id", ""),
            thread_id=thread_id,
            text=node.get("body", ""),
            formatted=self._format_converter.to_ast(node.get("body", "")),
            raw=LinearCommentRawMessage(
                kind="comment",
                comment={
                    "id": node.get("id", ""),
                    "body": node.get("body", ""),
                    "issueId": issue_id,
                    "userId": user_id,
                    "createdAt": node.get("createdAt", ""),
                    "updatedAt": node.get("updatedAt", ""),
                    "url": node.get("url", ""),
                },
            ),
            author=Author(
                user_id=user_id,
                user_name=user.get("displayName", "unknown"),
                full_name=user.get("name") or user.get("displayName", "unknown"),
                is_bot=False,
                is_me=user_id == self._bot_user_id,
            ),
            metadata=MessageMetadata(
                date_sent=_parse_iso(node["createdAt"]) if node.get("createdAt") else datetime.now(timezone.utc),
                edited=node.get("createdAt") != node.get("updatedAt"),
                edited_at=(
                    _parse_iso(node["updatedAt"])
                    if node.get("createdAt") != node.get("updatedAt") and node.get("updatedAt")
                    else None
                ),
            ),
            attachments=[],
        )

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread info for a Linear issue."""
        await self._ensure_valid_token()
        decoded = self.decode_thread_id(thread_id)

        result = await self._graphql_query(
            """
            query Issue($issueId: String!) {
                issue(id: $issueId) {
                    identifier
                    title
                    url
                }
            }
            """,
            {"issueId": decoded.issue_id},
        )

        issue = result.get("data", {}).get("issue", {})

        return ThreadInfo(
            id=thread_id,
            channel_id=decoded.issue_id,
            channel_name=f"{issue.get('identifier', '')}: {issue.get('title', '')}",
            is_dm=False,
            metadata={
                "issueId": decoded.issue_id,
                "issue_id": decoded.issue_id,  # snake_case alias for compatibility
                # ``agentSessionId`` mirrors upstream's fetchThread metadata
                # (index.ts:1928) — the decoded session id (``None`` for non-
                # session threads), so session-aware callers can round-trip it.
                "agentSessionId": decoded.agent_session_id,
                "identifier": issue.get("identifier"),
                "title": issue.get("title"),
                "url": issue.get("url"),
            },
        )

    def encode_thread_id(self, platform_data: LinearThreadId) -> str:
        """Encode a Linear thread ID.

        Formats:
        - Issue-level: linear:{issue_id}
        - Comment thread: linear:{issue_id}:c:{comment_id}
        - Agent-session issue: linear:{issue_id}:s:{agent_session_id}
        - Agent-session comment:
          linear:{issue_id}:c:{comment_id}:s:{agent_session_id}

        CRITICAL — cross-SDK state compat: the issue-level and comment-thread
        outputs are persisted (Redis/Postgres) and shared with the TS SDK, so
        they MUST stay byte-identical to the prior forms. The ``:s:`` session
        forms are new (no existing persisted data).
        """
        if platform_data.agent_session_id:
            if platform_data.comment_id:
                return (
                    f"linear:{platform_data.issue_id}:c:{platform_data.comment_id}:s:{platform_data.agent_session_id}"
                )
            return f"linear:{platform_data.issue_id}:s:{platform_data.agent_session_id}"

        if platform_data.comment_id:
            return f"linear:{platform_data.issue_id}:c:{platform_data.comment_id}"
        return f"linear:{platform_data.issue_id}"

    def decode_thread_id(self, thread_id: str) -> LinearThreadId:
        """Decode a Linear thread ID.

        Patterns are tried most-specific first — COMMENT_SESSION → ISSUE_SESSION
        → COMMENT → bare-issue — exactly as upstream. The order is load-bearing:
        a comment-session id ``linear:i:c:cm:s:sess`` also satisfies the bare
        anchored shape only via its first segment, and an issue-session id
        ``linear:i:s:sess`` must not be mistaken for a comment id, so the most
        specific anchored pattern must win.
        """
        if not thread_id.startswith("linear:"):
            raise ValidationError("linear", f"Invalid Linear thread ID: {thread_id}")

        without_prefix = thread_id[7:]
        if not without_prefix:
            raise ValidationError("linear", f"Invalid Linear thread ID format: {thread_id}")

        # Agent-session comment format: {issueId}:c:{commentId}:s:{agentSessionId}
        comment_session_match = COMMENT_SESSION_THREAD_PATTERN.match(without_prefix)
        if comment_session_match:
            return LinearThreadId(
                issue_id=comment_session_match.group(1),
                comment_id=comment_session_match.group(2),
                agent_session_id=comment_session_match.group(3),
            )

        # Agent-session issue format: {issueId}:s:{agentSessionId}
        issue_session_match = ISSUE_SESSION_THREAD_PATTERN.match(without_prefix)
        if issue_session_match:
            return LinearThreadId(
                issue_id=issue_session_match.group(1),
                agent_session_id=issue_session_match.group(2),
            )

        # Comment thread format: {issueId}:c:{commentId}
        match = COMMENT_THREAD_PATTERN.match(without_prefix)
        if match:
            return LinearThreadId(
                issue_id=match.group(1),
                comment_id=match.group(2),
            )

        # Issue-level format: {issueId}
        return LinearThreadId(issue_id=without_prefix)

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Derive channel ID from a Linear thread ID."""
        decoded = self.decode_thread_id(thread_id)
        return f"linear:{decoded.issue_id}"

    def parse_message(self, raw: LinearRawMessage) -> Message:
        """Parse platform message format to normalized format.

        TypedDict `.get()` unions every value-type across camel/snake-case
        aliases, producing `object | str`. Cast the string fields we know
        are strings at runtime so downstream constructors (`Author`,
        `_parse_iso`) receive `str` instead of `object`.
        """
        comment = raw.get("comment", {})
        text = cast("str", comment.get("body", ""))
        user_id = cast("str", comment.get("userId") or comment.get("user_id", ""))

        created_at = cast("str", comment.get("createdAt") or comment.get("created_at", ""))
        updated_at = cast("str", comment.get("updatedAt") or comment.get("updated_at", ""))

        return Message(
            id=comment.get("id", ""),
            thread_id="",
            text=text,
            formatted=self._format_converter.to_ast(text),
            author=Author(
                user_id=user_id,
                user_name="unknown",
                full_name="unknown",
                is_bot=False,
                is_me=user_id == self._bot_user_id,
            ),
            metadata=MessageMetadata(
                date_sent=(_parse_iso(created_at) if created_at else datetime.now(timezone.utc)),
                edited=created_at != updated_at,
                edited_at=(_parse_iso(updated_at) if created_at != updated_at and updated_at else None),
            ),
            attachments=[],
            raw=raw,
        )

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to Linear markdown."""
        return self._format_converter.from_ast(content)

    async def stream(
        self,
        thread_id: str,
        text_stream: Any,
        options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream responses to a thread.

        Faithful port of upstream ``stream`` (index.ts:1542): dispatch to the
        agent-session streamer when the decoded thread carries a session, else
        fall through to the existing comment-path behavior (byte-identical to
        before — Linear comments do not support native streaming, so the comment
        path accumulates the full text and posts a single comment at the end).
        """
        decoded = self.decode_thread_id(thread_id)
        if decoded.agent_session_id:
            await self._ensure_valid_token()
            session = assert_agent_session_thread(decoded)
            return await self._stream_in_agent_session(session, text_stream, options)

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

        # Post the accumulated text as a single comment
        if accumulated:
            postable: AdapterPostableMessage = PostableRaw(raw=accumulated)
            result = await self.post_message(thread_id, postable)
            message_id = result.id

        return RawMessage(
            id=message_id or "",
            thread_id=thread_id,
            raw={"text": accumulated},
        )

    async def _stream_in_agent_session(
        self,
        decoded: LinearAgentSessionThreadId,
        text_stream: Any,
        _options: StreamOptions | None = None,
    ) -> RawMessage:
        """Stream text/chunks into a Linear agent session.

        Faithful port of upstream ``streamInAgentSession`` (index.ts:1560).
        Unlike the comment path (a single post/edit), an agent session is
        append-only: committable markdown is flushed as successive "response"
        activities (with a delta computed against the last appended text),
        ``task_update`` chunks become "action"/"error" activities, and
        ``plan_update`` chunks replace the session plan. The final
        ``renderer.finish()`` is force-flushed as the closing response, then
        resolved back into a raw message via
        :meth:`_parse_message_from_agent_activity`.
        """
        from chat_sdk.shared.streaming_markdown import StreamingMarkdownRenderer

        renderer = StreamingMarkdownRenderer()
        agent_session_id = decoded.agent_session_id

        # Mutable closure state. ``last_appended`` tracks the full markdown text
        # already emitted so each flush sends only the new tail (the delta).
        last_appended = ""

        async def flush_markdown(
            activity_type: str,
            markdown: str | None = None,
            force: bool = False,
        ) -> dict[str, Any] | None:
            """Flush the current markdown buffer into a new "response"/"thought" activity.

            ``delta = markdown[len(last_appended):].trim()`` — the JS ``.trim()``
            whitespace set (``_JS_WHITESPACE``), NOT Python's broader
            ``str.strip()``. ``if delta or force`` mirrors upstream: an
            empty-string delta is falsy in both languages, so a no-delta flush
            is a no-op unless forced (the final flush). Returns the created
            activity payload, or ``None`` when nothing was sent.
            """
            nonlocal last_appended
            committable = renderer.get_committable_text() if markdown is None else markdown
            delta = committable[len(last_appended) :].strip(_JS_WHITESPACE)
            if delta or force:
                last_appended = committable
                return await self._create_agent_activity(
                    agent_session_id,
                    {"type": activity_type, "body": delta},
                )
            return None

        def _read(chunk: Any, name: str) -> Any:
            """Read a chunk field from either a dict or a dataclass chunk."""
            if isinstance(chunk, dict):
                return chunk.get(name)
            return getattr(chunk, name, None)

        async for chunk in text_stream:
            if isinstance(chunk, str):
                renderer.push(chunk)
                continue

            chunk_type = _read(chunk, "type")

            if chunk_type == "markdown_text":
                renderer.push(_read(chunk, "text") or "")
                continue

            if chunk_type == "task_update":
                # Flush any buffered markdown as a "thought" before sending the
                # action so the action card is distinct from the response body.
                await flush_markdown("thought")

                title = _read(chunk, "title")
                output = _read(chunk, "output")
                status = _read(chunk, "status")

                if status == "error":
                    # ``[title, output].filter(Boolean).join("\n")`` — drops None
                    # AND empty string (truthiness), faithful.
                    body = "\n".join(x for x in [title, output] if x)
                    await self._create_agent_activity(
                        agent_session_id,
                        {"type": "error", "body": body},
                    )
                else:
                    # ``ephemeral: status !== "complete"`` — in-progress/pending
                    # actions are ephemeral; only a completed action persists.
                    # Upstream passes ``result: chunk.output`` (string|undefined);
                    # ``JSON.stringify`` OMITS a key whose value is undefined, so
                    # OMIT ``result`` entirely when ``output`` is None to match
                    # the key-absent wire shape (rather than serializing
                    # ``"result": null``).
                    action_content: dict[str, Any] = {
                        "type": "action",
                        "action": title,
                        "parameter": "",
                    }
                    if output is not None:
                        action_content["result"] = output
                    await self._create_agent_activity(
                        agent_session_id,
                        action_content,
                        ephemeral=status != "complete",
                    )
                continue

            if chunk_type == "plan_update":
                # Replace the session plan in its entirety (agents cannot patch a
                # single item). https://linear.app/developers/agent-interaction
                await self._graphql_query(
                    _AGENT_SESSION_UPDATE_MUTATION,
                    {
                        "id": agent_session_id,
                        "input": {
                            "plan": [
                                {
                                    "content": _read(chunk, "title"),
                                    "status": "completed",
                                }
                            ]
                        },
                    },
                )

        final_activity = await flush_markdown("response", renderer.finish(), True)
        if not final_activity:
            # Upstream throws a bare ``Error`` here (NOT an ``AdapterError``);
            # ported as ``RuntimeError`` per the repo's bare-Error convention.
            raise RuntimeError("Failed to flush final markdown delta for agent session stream")

        return await self._parse_message_from_agent_activity(decoded, final_activity)

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
        self._logger.debug("Linear adapter disconnecting")

    def _resolve_emoji(self, emoji: EmojiValue | str) -> str:
        """Resolve an emoji value to a unicode string."""
        emoji_name = emoji if isinstance(emoji, str) else emoji.name
        return EMOJI_MAPPING.get(emoji_name, emoji_name)

    # =========================================================================
    # GraphQL API helper
    # =========================================================================

    async def _graphql_query(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL query against the Linear API."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._access_token or "",
        }

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        session = await self._get_http_session()
        async with session.post(
            self._api_url,
            headers=headers,
            json=payload,
        ) as response:
            if not response.ok:
                error_text = await response.text()
                raise NetworkError(
                    "linear",
                    f"Linear API error: {response.status} {error_text}",
                )
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


def create_linear_adapter(config: LinearAdapterConfig | None = None) -> LinearAdapter:
    """Factory function to create a Linear adapter."""
    return LinearAdapter(config)
