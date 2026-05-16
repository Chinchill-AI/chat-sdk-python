"""Telegram adapter for chat SDK.

Supports messaging via the Telegram Bot API using either webhook or
long-polling mode.  All conversations are keyed by Telegram chat ID
(with optional forum-topic thread IDs).

Python port of packages/adapter-telegram/src/index.ts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

from chat_sdk.adapters.telegram.cards import (
    card_to_telegram_inline_keyboard,
    decode_telegram_callback_data,
    empty_telegram_inline_keyboard,
)
from chat_sdk.adapters.telegram.format_converter import TelegramFormatConverter
from chat_sdk.adapters.telegram.types import (
    TelegramAdapterConfig,
    TelegramApiResponse,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramFile,
    TelegramInlineKeyboardMarkup,
    TelegramLongPollingConfig,
    TelegramMessage,
    TelegramMessageEntity,
    TelegramMessageReactionUpdated,
    TelegramRawMessage,
    TelegramReactionType,
    TelegramThreadId,
    TelegramUpdate,
    TelegramUser,
    TelegramWebhookInfo,
)
from chat_sdk.emoji import convert_emoji_placeholders, emoji_to_unicode, get_emoji
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.logger import ConsoleLogger, Logger
from chat_sdk.shared.adapter_utils import extract_card, extract_files
from chat_sdk.shared.card_utils import card_to_fallback_text
from chat_sdk.shared.errors import (
    AdapterPermissionError,
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
    RawMessage,
    ReactionEvent,
    ThreadInfo,
    WebhookOptions,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_SECRET_TOKEN_HEADER = "x-telegram-bot-api-secret-token"  # pragma: allowlist secret
MESSAGE_ID_PATTERN = re.compile(r"^([^:]+):(\d+)$")
TELEGRAM_MARKDOWN_PARSE_MODE = "MarkdownV2"
MESSAGE_SEQUENCE_PATTERN = re.compile(r":(\d+)$")
LEADING_AT_PATTERN = re.compile(r"^@+")
EMOJI_PLACEHOLDER_PATTERN = re.compile(r"^\{\{emoji:([a-z0-9_]+)\}\}$", re.IGNORECASE)
EMOJI_NAME_PATTERN = re.compile(r"^[a-z0-9_+-]+$", re.IGNORECASE)

TELEGRAM_DEFAULT_POLLING_TIMEOUT_SECONDS = 30
TELEGRAM_DEFAULT_POLLING_LIMIT = 100
TELEGRAM_DEFAULT_POLLING_RETRY_DELAY_MS = 1000
TELEGRAM_MAX_POLLING_LIMIT = 100
TELEGRAM_MIN_POLLING_LIMIT = 1
TELEGRAM_MIN_POLLING_TIMEOUT_SECONDS = 0
TELEGRAM_MAX_POLLING_TIMEOUT_SECONDS = 300

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class TelegramMessageAuthor:
    """Author information extracted from a Telegram user / chat."""

    full_name: str
    is_bot: bool | str  # bool or "unknown"
    is_me: bool
    user_id: str
    user_name: str


@dataclass
class ResolvedTelegramLongPollingConfig:
    """Fully resolved long-polling configuration."""

    allowed_updates: list[str] | None
    delete_webhook: bool
    drop_pending_updates: bool
    limit: int
    retry_delay_ms: int
    timeout: int


TelegramRuntimeMode = str  # "webhook" | "polling"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _utf16_len(text: str) -> int:
    """Return the length of *text* measured in UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def _truncate_to_utf16(text: str, limit: int, ellipsis: str = "...") -> str:
    """Truncate *text* so its UTF-16 length does not exceed *limit*.

    When truncation is needed the last characters are replaced with *ellipsis*.
    """
    if _utf16_len(text) <= limit:
        return text
    ellipsis_units = _utf16_len(ellipsis)
    budget = limit - ellipsis_units
    count = 0
    cut = 0
    for i, ch in enumerate(text):
        units = 2 if ord(ch) > 0xFFFF else 1
        if count + units > budget:
            cut = i
            break
        count += units
    else:
        cut = len(text)
    return text[:cut] + ellipsis


# ---------------------------------------------------------------------------
# MarkdownV2-safe truncation
# ---------------------------------------------------------------------------
#
# Port of packages/adapter-telegram/src/markdown.ts (chat@4.27.0).
#
# Naive ``slice + "..."`` produces invalid MarkdownV2: ``.`` is a reserved
# character (must be escaped as ``\.``); a slice can leave an orphan
# trailing ``\`` that escapes the ellipsis or nothing; and a slice can cut
# through a paired entity (``*bold*``, `` `code` ``, ``[label](url)``)
# leaving it unclosed. Telegram rejects all three with
# ``Bad Request: can't parse entities``.
#
# These helpers walk back past unbalanced delimiters and orphan backslashes
# before appending an escaped ellipsis. They also run on
# under-the-limit MarkdownV2 inputs (per upstream f46a6fb / chat#446) so
# streamed chunks that arrive with a transiently unpaired opener are
# trimmed back to a parseable boundary.

# Entity delimiters whose opener/closer pairing must be preserved when
# truncating a rendered MarkdownV2 string.
_MARKDOWN_V2_ENTITY_MARKERS: tuple[str, ...] = ("*", "_", "~", "`")

_MARKDOWN_V2_ELLIPSIS = "\\.\\.\\."
_PLAIN_ELLIPSIS = "..."


def find_unescaped_positions(text: str, marker: str) -> list[int]:
    """Return indices of every occurrence of *marker* in *text* not preceded
    by an odd number of backslashes (i.e. not escaped)."""
    positions: list[int] = []
    for i, ch in enumerate(text):
        if ch != marker:
            continue
        backslashes = 0
        j = i - 1
        while j >= 0 and text[j] == "\\":
            backslashes += 1
            j -= 1
        if backslashes % 2 == 0:
            positions.append(i)
    return positions


def _find_unescaped_positions_outside_code(text: str, marker: str) -> list[int]:
    """Like :func:`find_unescaped_positions` but skips occurrences inside
    fenced code blocks (```````) or inline code spans
    (`````). Inside those regions Telegram treats ``*``, ``_``, ``~``,
    ``[``, ``]`` as literal text.

    Port of upstream ``findUnescapedPositionsOutsideCode`` (chat#446).
    """
    positions: list[int] = []
    in_fence = False
    in_inline = False
    backslashes = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        if ch == "\\":
            backslashes += 1
            i += 1
            continue

        escaped = backslashes % 2 == 1
        backslashes = 0

        if ch == "`" and not escaped:
            is_triple = text[i + 1 : i + 2] == "`" and text[i + 2 : i + 3] == "`"
            if is_triple and not in_inline:
                in_fence = not in_fence
                i += 3
                continue
            if not in_fence:
                in_inline = not in_inline
            i += 1
            continue

        if ch == marker and not escaped and not in_fence and not in_inline:
            positions.append(i)
        i += 1

    return positions


def ends_with_orphan_backslash(text: str) -> bool:
    """Return True if *text* ends with an odd number of trailing ``\\``."""
    trailing = 0
    i = len(text) - 1
    while i >= 0 and text[i] == "\\":
        trailing += 1
        i -= 1
    return trailing % 2 == 1


def _trim_to_markdown_v2_safe_boundary(text: str) -> str:
    """Drop trailing characters that would produce invalid MarkdownV2.

    Drops:
      - orphan trailing ``\\`` (would escape the appended ellipsis or nothing)
      - unclosed entity delimiter (``*``, ``_``, ``~``, `` ` ``) whose closer
        was cut off
      - unmatched ``[`` from a link whose closer was cut off

    Best-effort: may drop more than strictly necessary in edge cases, but
    guarantees the output is parseable MarkdownV2 (when the input was).
    """
    current = text
    max_iterations = len(current) + 1

    for _ in range(max_iterations):
        if ends_with_orphan_backslash(current):
            current = current[:-1]
            continue

        min_unsafe_position = len(current)

        for marker in _MARKDOWN_V2_ENTITY_MARKERS:
            if marker == "`":
                positions = find_unescaped_positions(current, marker)
            else:
                positions = _find_unescaped_positions_outside_code(current, marker)
            if len(positions) % 2 == 1:
                last_unpaired = positions[-1] if positions else len(current)
                if last_unpaired < min_unsafe_position:
                    min_unsafe_position = last_unpaired

        open_brackets = _find_unescaped_positions_outside_code(current, "[")
        close_brackets = _find_unescaped_positions_outside_code(current, "]")
        if len(open_brackets) > len(close_brackets):
            last_open = open_brackets[-1] if open_brackets else len(current)
            if last_open < min_unsafe_position:
                min_unsafe_position = last_open

        if min_unsafe_position >= len(current):
            return current

        current = current[:min_unsafe_position]

    return current


def truncate_for_telegram(text: str, limit: int, parse_mode: str | None) -> str:
    """Truncate *text* to *limit* characters, appending an ellipsis.

    For MarkdownV2 (``parse_mode == "MarkdownV2"``), uses an escaped
    ellipsis (``\\.\\.\\.``) and trims back past any unbalanced entity
    delimiter or orphan backslash before appending. Plain text gets a
    literal ``...``.

    Even when *text* is under the limit, MarkdownV2 inputs go through
    :func:`_trim_to_markdown_v2_safe_boundary` so that streamed chunks
    with transiently unpaired entity markers don't trigger Telegram's
    ``can't parse entities`` 400 (port of chat#446 / upstream f46a6fb).

    Limit is interpreted in Python ``len()`` units (== UTF-16 code units
    for the BMP, == 1 for astral characters). Telegram's 4096 / 1024
    caps are documented in UTF-16 code units; for parity with upstream
    we follow upstream's `string.length` semantics here. Pre-existing
    UTF-16-aware truncation is reserved for non-MarkdownV2 paths.
    """
    is_markdown_v2 = parse_mode == "MarkdownV2"

    if len(text) <= limit:
        return _trim_to_markdown_v2_safe_boundary(text) if is_markdown_v2 else text

    ellipsis = _MARKDOWN_V2_ELLIPSIS if is_markdown_v2 else _PLAIN_ELLIPSIS
    sliced = text[: limit - len(ellipsis)]

    if is_markdown_v2:
        sliced = _trim_to_markdown_v2_safe_boundary(sliced)

    return f"{sliced}{ellipsis}"


def _trim_trailing_slashes(url: str) -> str:
    """Remove trailing ``/`` characters from *url*."""
    end = len(url)
    while end > 0 and url[end - 1] == "/":
        end -= 1
    return url[:end]


def _escape_markdown_in_entity(text: str) -> str:
    """Escape markdown-special characters inside entity text."""
    return re.sub(r"([\[\]()\\])", r"\\\1", text)


def apply_telegram_entities(
    text: str,
    entities: list[TelegramMessageEntity],
) -> str:
    """Convert Telegram message entities to markdown.

    Telegram delivers formatting as separate entity objects alongside plain
    text.  This function reconstructs markdown so that links, bold, italic,
    code, etc. are preserved when the text is later parsed as markdown.

    Entities use UTF-16 offsets which match JavaScript string indexing.
    Python strings are UTF-32, so we encode to UTF-16-LE for correct slicing.
    """
    if not entities:
        return text

    # Encode to UTF-16 LE for correct offset handling (2 bytes per code unit)
    utf16_bytes = text.encode("utf-16-le")

    # Sort entities by offset descending so replacements don't shift later offsets.
    # For entities at the same offset, apply the shorter (inner) one first.
    sorted_entities = sorted(
        entities,
        key=lambda e: (-e.get("offset", 0), e.get("length", 0)),
    )

    for entity in sorted_entities:
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        start_byte = offset * 2
        end_byte = (offset + length) * 2
        entity_text = utf16_bytes[start_byte:end_byte].decode("utf-16-le")

        replacement: str | None = None
        entity_type = entity.get("type", "")

        if entity_type == "text_link":
            url = entity.get("url")
            if url:
                replacement = f"[{_escape_markdown_in_entity(entity_text)}]({url})"
        elif entity_type == "bold":
            replacement = f"**{entity_text}**"
        elif entity_type == "italic":
            replacement = f"*{entity_text}*"
        elif entity_type == "code":
            replacement = f"`{entity_text}`"
        elif entity_type == "pre":
            lang = entity.get("language") or ""
            replacement = f"```{lang}\n{entity_text}\n```"
        elif entity_type == "strikethrough":
            replacement = f"~~{entity_text}~~"

        if replacement is not None:
            replacement_bytes = replacement.encode("utf-16-le")
            utf16_bytes = utf16_bytes[:start_byte] + replacement_bytes + utf16_bytes[end_byte:]

    return utf16_bytes.decode("utf-16-le")


# =========================================================================
# TelegramAdapter
# =========================================================================


class TelegramAdapter:
    """Telegram adapter for chat SDK.

    Implements the Adapter interface for the Telegram Bot API.
    """

    def __init__(self, config: TelegramAdapterConfig | None = None) -> None:
        if config is None:
            config = TelegramAdapterConfig()

        bot_token = config.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
        if not bot_token:
            raise ValidationError(
                "telegram",
                "botToken is required. Set TELEGRAM_BOT_TOKEN or provide it in config.",
            )

        self._name: str = "telegram"
        self._lock_scope: LockScope = "channel"
        self._persist_message_history: bool = True

        self._bot_token: str = bot_token
        self._api_base_url: str = _trim_trailing_slashes(
            config.api_base_url or os.environ.get("TELEGRAM_API_BASE_URL") or TELEGRAM_API_BASE
        )
        self._secret_token: str | None = config.secret_token or os.environ.get("TELEGRAM_WEBHOOK_SECRET_TOKEN")
        self._warned_no_verification: bool = False
        self._logger: Logger = config.logger or ConsoleLogger("info").child("telegram")
        self._format_converter: TelegramFormatConverter = TelegramFormatConverter()
        self._message_cache: dict[str, list[Message]] = {}

        self._chat: ChatInstance | None = None
        self._bot_user_id: str | None = None

        explicit_user_name = config.user_name or os.environ.get("TELEGRAM_BOT_USERNAME")
        self._user_name: str = self.normalize_user_name(explicit_user_name or "bot")
        self._has_explicit_user_name: bool = bool(explicit_user_name)

        self._mode: str = config.mode or "auto"
        self._long_polling: TelegramLongPollingConfig | None = config.long_polling

        self._runtime_mode: TelegramRuntimeMode = "webhook"
        self._polling_task: asyncio.Task[None] | None = None
        self._polling_active: bool = False

        # Shared aiohttp session for connection pooling
        self._http_session: Any | None = None

        if self._mode not in ("auto", "webhook", "polling"):
            raise ValidationError(
                "telegram",
                f'Invalid mode: {self._mode}. Expected "auto", "webhook", or "polling".',
            )

    # -- Properties ----------------------------------------------------------

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
    def bot_user_id(self) -> str | None:
        return self._bot_user_id

    @property
    def user_name(self) -> str:
        return self._user_name

    @property
    def is_polling(self) -> bool:
        return self._polling_active

    @property
    def runtime_mode(self) -> TelegramRuntimeMode:
        return self._runtime_mode

    # -- Lifecycle -----------------------------------------------------------

    async def initialize(self, chat: ChatInstance) -> None:
        """Initialize the adapter and fetch bot identity via ``getMe``."""
        self._chat = chat

        if not self._has_explicit_user_name:
            chat_user_name = getattr(chat, "get_user_name", None)
            if callable(chat_user_name):
                resolved = chat_user_name()
                if isinstance(resolved, str) and resolved.strip():
                    self._user_name = self.normalize_user_name(resolved)

        try:
            me: TelegramUser = await self.telegram_fetch("getMe")
            self._bot_user_id = str(me.get("id", ""))
            if not self._has_explicit_user_name and me.get("username"):
                self._user_name = self.normalize_user_name(me["username"])

            self._logger.info(
                "Telegram adapter initialized",
                {"botUserId": self._bot_user_id, "userName": self._user_name},
            )
        except Exception as error:
            self._logger.warn(
                "Failed to fetch Telegram bot identity",
                {"error": str(error)},
            )

        runtime_mode = await self.resolve_runtime_mode()
        self._runtime_mode = runtime_mode

        if runtime_mode == "polling":
            polling_config = self._long_polling
            if self._mode == "auto":
                if polling_config:
                    merged = TelegramLongPollingConfig(
                        allowed_updates=polling_config.allowed_updates,
                        delete_webhook=False,
                        drop_pending_updates=polling_config.drop_pending_updates,
                        limit=polling_config.limit,
                        retry_delay_ms=polling_config.retry_delay_ms,
                        timeout=polling_config.timeout,
                    )
                    await self.start_polling(merged)
                else:
                    await self.start_polling(TelegramLongPollingConfig(delete_webhook=False))
            else:
                await self.start_polling(polling_config)

    async def handle_webhook(
        self,
        request: Any,
        options: WebhookOptions | None = None,
    ) -> Any:
        """Handle an incoming Telegram webhook request.

        Validates the secret token header, parses the JSON update, and
        dispatches it to ``processUpdate``.
        """
        if self._secret_token:
            header_token = self._get_header(request, TELEGRAM_SECRET_TOKEN_HEADER)
            valid = False
            try:
                if header_token:
                    valid = hmac.compare_digest(header_token, self._secret_token)
            except Exception:
                pass
            if not valid:
                self._logger.warn(
                    "Telegram webhook rejected due to invalid secret token",
                )
                return self._make_response("Invalid secret token", 401)
        elif not self._warned_no_verification:
            self._warned_no_verification = True
            self._logger.warn(
                "Telegram webhook verification is disabled. "
                "Set TELEGRAM_WEBHOOK_SECRET_TOKEN or secretToken to verify incoming requests.",
            )

        try:
            body = await self._get_request_body(request)
            update: TelegramUpdate = json.loads(body)
        except Exception:
            return self._make_response("Invalid JSON", 400)

        if not self._chat:
            self._logger.warn(
                "Chat instance not initialized, ignoring Telegram webhook",
            )
            return self._make_response("OK", 200)

        try:
            self.process_update(update, options)
        except Exception as error:
            self._logger.warn(
                "Failed to process Telegram webhook update",
                {"error": str(error), "updateId": update.get("update_id")},
            )

        return self._make_response("OK", 200)

    async def _get_http_session(self) -> Any:
        """Return the shared aiohttp session, creating it lazily if needed."""
        import aiohttp

        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def disconnect(self) -> None:
        """Disconnect the adapter, stop polling, and close the shared HTTP session."""
        await self.stop_polling()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    # -- Polling -------------------------------------------------------------

    async def start_polling(self, config: TelegramLongPollingConfig | None = None) -> None:
        """Start long-polling for updates."""
        if not self._chat:
            raise ValidationError(
                "telegram",
                "Cannot start polling before initialize()",
            )

        if self._polling_active:
            self._logger.debug("Telegram polling already active")
            return

        resolved_config = self.resolve_polling_config(config)
        previous_runtime_mode = self._runtime_mode
        self._polling_active = True

        try:
            if resolved_config.delete_webhook:
                await self.reset_webhook(resolved_config.drop_pending_updates)
            self._runtime_mode = "polling"
        except Exception:
            self._polling_active = False
            self._runtime_mode = previous_runtime_mode
            raise

        self._logger.info(
            "Telegram polling started",
            {
                "limit": resolved_config.limit,
                "timeout": resolved_config.timeout,
                "allowedUpdates": resolved_config.allowed_updates,
            },
        )

        async def _run_polling() -> None:
            try:
                await self.polling_loop(resolved_config)
            finally:
                self._polling_active = False
                self._polling_task = None

        try:
            self._polling_task = asyncio.get_running_loop().create_task(_run_polling())
        except RuntimeError:
            self._polling_task = None
            self._polling_active = False
            self._logger.error("No running event loop to start polling task")

    async def stop_polling(self) -> None:
        """Stop long-polling.

        Cancels the polling task so that a blocked long-poll HTTP request
        does not cause a ~30 s hang on shutdown.
        """
        if not self._polling_active:
            return
        self._polling_active = False
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._polling_task
        self._polling_task = None
        self._logger.info("Telegram polling stopped")

    async def reset_webhook(self, drop_pending_updates: bool = False) -> None:
        """Delete the current Telegram webhook."""
        await self.telegram_fetch(
            "deleteWebhook",
            {"drop_pending_updates": drop_pending_updates},
        )
        self._logger.info(
            "Telegram webhook reset",
            {"dropPendingUpdates": drop_pending_updates},
        )

    async def polling_loop(self, config: ResolvedTelegramLongPollingConfig) -> None:
        """Core polling loop that calls ``getUpdates`` in a loop."""
        offset: int | None = None
        consecutive_failures = 0
        max_backoff_ms = 30_000

        while self._polling_active:
            try:
                params: dict[str, Any] = {"limit": config.limit, "timeout": config.timeout}
                if offset is not None:
                    params["offset"] = offset
                if config.allowed_updates is not None:
                    params["allowed_updates"] = config.allowed_updates
                updates: list[TelegramUpdate] = await self.telegram_fetch(
                    "getUpdates",
                    params,
                )

                consecutive_failures = 0

                for update in updates:
                    offset = update.get("update_id", 0) + 1
                    try:
                        self.process_update(update)
                    except Exception as error:
                        self._logger.warn(
                            "Failed to process Telegram polled update",
                            {
                                "error": str(error),
                                "updateId": update.get("update_id"),
                            },
                        )
            except asyncio.CancelledError:
                return
            except Exception as error:
                consecutive_failures += 1
                backoff_ms = min(
                    config.retry_delay_ms * 2 ** (consecutive_failures - 1),
                    max_backoff_ms,
                )

                self._logger.warn(
                    "Telegram polling request failed",
                    {
                        "error": str(error),
                        "retryDelayMs": backoff_ms,
                        "consecutiveFailures": consecutive_failures,
                    },
                )

                if not self._polling_active:
                    return

                await asyncio.sleep(backoff_ms / 1000.0)

    # -- Runtime mode resolution ---------------------------------------------

    async def resolve_runtime_mode(self) -> TelegramRuntimeMode:
        """Determine whether to use webhook or polling mode."""
        if self._mode == "webhook":
            return "webhook"

        if self._mode == "polling":
            return "polling"

        webhook_info = await self._fetch_webhook_info()
        if not webhook_info:
            self._logger.warn(
                "Telegram auto mode could not verify webhook status; keeping webhook mode",
            )
            return "webhook"

        url = webhook_info.get("url", "")
        if isinstance(url, str) and url.strip():
            self._logger.debug(
                "Telegram auto mode selected webhook mode",
                {"webhookUrl": url},
            )
            return "webhook"

        if self.is_likely_serverless_runtime():
            self._logger.warn(
                "Telegram auto mode detected serverless runtime without webhook URL; keeping webhook mode",
            )
            return "webhook"

        self._logger.info("Telegram auto mode selected polling mode")
        return "polling"

    def is_likely_serverless_runtime(self) -> bool:
        """Heuristic check for serverless execution environments."""
        return bool(
            os.environ.get("VERCEL")
            or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
            or (os.environ.get("AWS_EXECUTION_ENV") or "").find("AWS_Lambda") >= 0
            or os.environ.get("FUNCTIONS_WORKER_RUNTIME")
            or os.environ.get("NETLIFY")
            or os.environ.get("K_SERVICE")
        )

    async def _fetch_webhook_info(self) -> TelegramWebhookInfo | None:
        """Fetch the current webhook info from Telegram."""
        try:
            return await self.telegram_fetch("getWebhookInfo")
        except Exception as error:
            self._logger.warn(
                "Failed to fetch Telegram webhook info",
                {"error": str(error)},
            )
            return None

    # -- Update dispatching --------------------------------------------------

    def process_update(
        self,
        update: TelegramUpdate,
        options: WebhookOptions | None = None,
    ) -> None:
        """Dispatch a Telegram update to the appropriate handler."""
        message_update = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
        )

        if message_update:
            self.handle_incoming_message_update(message_update, options)

        if update.get("callback_query"):
            self.handle_callback_query(update["callback_query"], options)

        if update.get("message_reaction"):
            self.handle_message_reaction_update(update["message_reaction"], options)

    def handle_incoming_message_update(
        self,
        telegram_message: TelegramMessage,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a new or edited message update."""
        if not self._chat:
            return

        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(telegram_message["chat"]["id"]),
                message_thread_id=telegram_message.get("message_thread_id"),
            )
        )

        parsed_message = self.parse_telegram_message(telegram_message, thread_id)
        self.cache_message(parsed_message)

        self._chat.process_message(self, thread_id, parsed_message, options)

    def handle_callback_query(
        self,
        callback_query: TelegramCallbackQuery,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a callback query (inline keyboard button press)."""
        if not (self._chat and callback_query.get("message")):
            return

        message = callback_query["message"]
        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(message["chat"]["id"]),
                message_thread_id=message.get("message_thread_id"),
            )
        )

        message_id = self.encode_message_id(
            str(message["chat"]["id"]),
            message["message_id"],
        )

        decoded = decode_telegram_callback_data(callback_query.get("data"))
        action_id = decoded["action_id"] or ""
        value = decoded["value"]

        # The TS source uses callback_query.from – in our types this is from_user
        from_user = cast(
            "TelegramUser | None",
            callback_query.get("from_user") or callback_query.get("from"),  # type: ignore[call-overload]
        )
        user = (
            self.to_author(from_user)
            if from_user
            else Author(
                full_name="unknown",
                is_bot="unknown",
                is_me=False,
                user_id="unknown",
                user_name="unknown",
            )
        )

        self._chat.process_action(
            ActionEvent(
                adapter=self,
                thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                thread_id=thread_id,
                message_id=message_id,
                user=user,
                action_id=action_id,
                value=value,
                raw=callback_query,
            ),
            options,
        )

        # Fire-and-forget: acknowledge the callback query
        async def _ack() -> None:
            try:
                await self.telegram_fetch(
                    "answerCallbackQuery",
                    {"callback_query_id": callback_query["id"]},
                )
            except Exception as error:
                self._logger.warn(
                    "Failed to acknowledge Telegram callback query",
                    {"callbackQueryId": callback_query["id"], "error": str(error)},
                )

        wait_until = getattr(options, "wait_until", None) if options else None
        try:
            task = asyncio.get_running_loop().create_task(_ack())
        except RuntimeError:
            task = None
        if task and callable(wait_until):
            wait_until(task)

    def handle_message_reaction_update(
        self,
        reaction_update: TelegramMessageReactionUpdated,
        options: WebhookOptions | None = None,
    ) -> None:
        """Handle a message reaction update."""
        if not self._chat:
            return

        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(reaction_update["chat"]["id"]),
                message_thread_id=reaction_update.get("message_thread_id"),
            )
        )

        message_id = self.encode_message_id(
            str(reaction_update["chat"]["id"]),
            reaction_update["message_id"],
        )

        old_reactions = {self.reaction_key(r) for r in reaction_update.get("old_reaction", [])}
        new_reactions = {self.reaction_key(r) for r in reaction_update.get("new_reaction", [])}

        user_field = reaction_update.get("user")
        actor = self.to_author(user_field) if user_field else self.to_reaction_actor_author(reaction_update["chat"])

        for reaction in reaction_update.get("new_reaction", []):
            key = self.reaction_key(reaction)
            if key not in old_reactions:
                self._chat.process_reaction(
                    ReactionEvent(
                        adapter=self,
                        thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                        thread_id=thread_id,
                        message_id=message_id,
                        user=actor,
                        emoji=self.reaction_to_emoji_value(reaction),
                        raw_emoji=key,
                        added=True,
                        raw=reaction_update,
                    ),
                    options,
                )

        for reaction in reaction_update.get("old_reaction", []):
            key = self.reaction_key(reaction)
            if key not in new_reactions:
                self._chat.process_reaction(
                    ReactionEvent(
                        adapter=self,
                        thread=None,  # pyrefly: ignore[bad-argument-type]  # filled in by Chat
                        thread_id=thread_id,
                        message_id=message_id,
                        user=actor,
                        emoji=self.reaction_to_emoji_value(reaction),
                        raw_emoji=key,
                        added=False,
                        raw=reaction_update,
                    ),
                    options,
                )

    # -- Posting / editing / deleting ----------------------------------------

    async def post_message(
        self,
        thread_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Telegram thread."""
        parsed_thread = self._resolve_thread_id(thread_id)

        card = extract_card(message)
        reply_markup = card_to_telegram_inline_keyboard(card) if card else None
        parse_mode = self.resolve_parse_mode(message, card)
        text = self.truncate_message(
            convert_emoji_placeholders(
                # Route the card's standard-markdown fallback through the
                # MarkdownV2 renderer so titles render as real bold instead
                # of literal ``**title**``.
                self._format_converter.from_markdown(card_to_fallback_text(card))
                if card
                else self._format_converter.render_postable(message),
                "gchat",
            ),
            parse_mode,
        )

        files = extract_files(message)
        if len(files) > 1:
            raise ValidationError(
                "telegram",
                "Telegram adapter supports a single file upload per message",
            )

        raw_message: TelegramMessage

        if len(files) == 1:
            file = files[0]
            if not file:
                raise ValidationError("telegram", "File upload payload is empty")
            raw_message = await self.send_document(parsed_thread, file, text, reply_markup, parse_mode)
        else:
            if not text.strip():
                raise ValidationError("telegram", "Message text cannot be empty")

            raw_message = await self.telegram_fetch(
                "sendMessage",
                {
                    "chat_id": parsed_thread.chat_id,
                    "message_thread_id": parsed_thread.message_thread_id,
                    "text": text,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                },
            )

        resulting_thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(raw_message["chat"]["id"]),
                message_thread_id=(
                    raw_message.get("message_thread_id")
                    if raw_message.get("message_thread_id") is not None
                    else parsed_thread.message_thread_id
                ),
            )
        )

        parsed_message = self.parse_telegram_message(raw_message, resulting_thread_id)
        self.cache_message(parsed_message)

        return RawMessage(
            id=parsed_message.id,
            thread_id=parsed_message.thread_id,
            raw=raw_message,
        )

    async def post_channel_message(
        self,
        channel_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Post a message to a Telegram channel."""
        return await self.post_message(channel_id, message)

    async def edit_message(
        self,
        thread_id: str,
        message_id: str,
        message: AdapterPostableMessage,
    ) -> RawMessage:
        """Edit an existing Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)
        chat_id = decoded["chat_id"]
        telegram_message_id = decoded["message_id"]
        composite_id = decoded["composite_id"]

        card = extract_card(message)
        reply_markup = card_to_telegram_inline_keyboard(card) if card else None
        parse_mode = self.resolve_parse_mode(message, card)
        text = self.truncate_message(
            convert_emoji_placeholders(
                self._format_converter.from_markdown(card_to_fallback_text(card))
                if card
                else self._format_converter.render_postable(message),
                "gchat",
            ),
            parse_mode,
        )

        if not text.strip():
            raise ValidationError("telegram", "Message text cannot be empty")

        result = await self.telegram_fetch(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": telegram_message_id,
                "text": text,
                "reply_markup": reply_markup or empty_telegram_inline_keyboard(),
                "parse_mode": parse_mode,
            },
        )

        # Telegram returns ``true`` when editing inline messages
        if result is True:
            existing = self.find_cached_message(composite_id)
            if not existing:
                raise ChatNotImplementedError(
                    "Telegram returned a non-message edit result and no cached message was found",
                    "editMessage",
                )

            updated = Message(
                id=existing.id,
                thread_id=existing.thread_id,
                text=text,
                formatted=self._format_converter.to_ast(text),
                raw=existing.raw,
                author=existing.author,
                metadata=MessageMetadata(
                    date_sent=existing.metadata.date_sent,
                    edited=True,
                    edited_at=datetime.now(timezone.utc),
                ),
                attachments=existing.attachments,
                is_mention=existing.is_mention,
            )

            self.cache_message(updated)

            return RawMessage(
                id=updated.id,
                thread_id=updated.thread_id,
                raw=updated.raw,
            )

        resulting_thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(result["chat"]["id"]),
                message_thread_id=(
                    result.get("message_thread_id")
                    if result.get("message_thread_id") is not None
                    else parsed_thread.message_thread_id
                ),
            )
        )

        parsed_message = self.parse_telegram_message(result, resulting_thread_id)
        self.cache_message(parsed_message)

        return RawMessage(
            id=parsed_message.id,
            thread_id=parsed_message.thread_id,
            raw=result,
        )

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        """Delete a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "deleteMessage",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
            },
        )

        self.delete_cached_message(decoded["composite_id"])

    # -- Reactions -----------------------------------------------------------

    async def add_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Add a reaction to a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "setMessageReaction",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
                "reaction": [self.to_telegram_reaction(emoji)],
            },
        )

    async def remove_reaction(
        self,
        thread_id: str,
        message_id: str,
        emoji: EmojiValue | str,
    ) -> None:
        """Remove a reaction from a Telegram message."""
        parsed_thread = self._resolve_thread_id(thread_id)
        decoded = self.decode_composite_message_id(message_id, parsed_thread.chat_id)

        await self.telegram_fetch(
            "setMessageReaction",
            {
                "chat_id": decoded["chat_id"],
                "message_id": decoded["message_id"],
                "reaction": [],
            },
        )

    # -- Typing --------------------------------------------------------------

    async def start_typing(self, thread_id: str, status: str | None = None) -> None:
        """Send a ``typing`` chat action."""
        parsed_thread = self._resolve_thread_id(thread_id)
        await self.telegram_fetch(
            "sendChatAction",
            {
                "chat_id": parsed_thread.chat_id,
                "message_thread_id": parsed_thread.message_thread_id,
                "action": "typing",
            },
        )

    # -- Fetching messages ---------------------------------------------------

    async def fetch_messages(
        self,
        thread_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch cached messages for a thread."""
        if options is None:
            options = FetchOptions()

        messages = sorted(
            list(self._message_cache.get(thread_id, [])),
            key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)),
        )

        return self.paginate_messages(messages, options)

    async def fetch_channel_messages(
        self,
        channel_id: str,
        options: FetchOptions | None = None,
    ) -> FetchResult:
        """Fetch cached messages across all threads in a channel."""
        if options is None:
            options = FetchOptions()

        by_id: dict[str, Message] = {}

        for tid, messages in self._message_cache.items():
            try:
                decoded = self.decode_thread_id(tid)
            except Exception:
                continue

            if decoded.chat_id != channel_id:
                continue

            for msg in messages:
                by_id[msg.id] = msg

        all_messages = sorted(
            list(by_id.values()),
            key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)),
        )

        return self.paginate_messages(all_messages, options)

    async def fetch_message(
        self,
        _thread_id: str,
        message_id: str,
    ) -> Message | None:
        """Fetch a single cached message by ID."""
        return self.find_cached_message(message_id)

    async def fetch_thread(self, thread_id: str) -> ThreadInfo:
        """Fetch thread information from Telegram."""
        parsed_thread = self._resolve_thread_id(thread_id)
        chat: TelegramChat = await self.telegram_fetch(
            "getChat",
            {"chat_id": parsed_thread.chat_id},
        )

        return ThreadInfo(
            id=self.encode_thread_id(parsed_thread),
            channel_id=str(chat["id"]),
            channel_name=self.chat_display_name(chat) or str(chat["id"]),
            is_dm=chat.get("type") == "private",
            metadata={
                "chat": chat,
                "messageThreadId": parsed_thread.message_thread_id,
            },
        )

    async def fetch_channel_info(self, channel_id: str) -> ChannelInfo:
        """Fetch channel information from Telegram."""
        chat: TelegramChat = await self.telegram_fetch(
            "getChat",
            {"chat_id": channel_id},
        )

        member_count: int | None = None
        try:
            member_count = await self.telegram_fetch(
                "getChatMemberCount",
                {"chat_id": channel_id},
            )
        except Exception:
            member_count = None

        return ChannelInfo(
            id=str(chat["id"]),
            name=self.chat_display_name(chat) or str(chat["id"]),
            is_dm=chat.get("type") == "private",
            member_count=member_count,
            metadata={"chat": chat},
        )

    # -- Thread / channel ID helpers -----------------------------------------

    def encode_thread_id(self, platform_data: TelegramThreadId) -> str:
        """Encode a :class:`TelegramThreadId` into a string thread ID."""
        if isinstance(platform_data.message_thread_id, int):
            return f"telegram:{platform_data.chat_id}:{platform_data.message_thread_id}"
        return f"telegram:{platform_data.chat_id}"

    def decode_thread_id(self, thread_id: str) -> TelegramThreadId:
        """Decode a string thread ID into a :class:`TelegramThreadId`."""
        parts = thread_id.split(":")
        if parts[0] != "telegram" or len(parts) < 2 or len(parts) > 3:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread ID: {thread_id}",
            )

        chat_id = parts[1] if len(parts) > 1 else ""
        if not chat_id:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread ID: {thread_id}",
            )

        if len(parts) < 3 or not parts[2]:
            return TelegramThreadId(chat_id=chat_id)

        try:
            message_thread_id = int(parts[2])
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread topic ID in thread ID: {thread_id}",
            ) from exc

        if not math.isfinite(message_thread_id):
            raise ValidationError(
                "telegram",
                f"Invalid Telegram thread topic ID in thread ID: {thread_id}",
            )

        return TelegramThreadId(chat_id=chat_id, message_thread_id=message_thread_id)

    def channel_id_from_thread_id(self, thread_id: str) -> str:
        """Extract the channel ID from a thread ID."""
        resolved = self._resolve_thread_id(thread_id)
        return f"telegram:{resolved.chat_id}"

    async def open_dm(self, user_id: str) -> str:
        """Open a DM with a user by their Telegram user ID."""
        return self.encode_thread_id(TelegramThreadId(chat_id=user_id))

    def is_dm(self, thread_id: str) -> bool:
        """Check if a thread ID is a DM (positive chat ID)."""
        resolved = self._resolve_thread_id(thread_id)
        return not resolved.chat_id.startswith("-")

    # -- Message parsing -----------------------------------------------------

    def parse_message(self, raw: TelegramRawMessage) -> Message:
        """Parse a raw Telegram message into a :class:`Message`."""
        thread_id = self.encode_thread_id(
            TelegramThreadId(
                chat_id=str(raw["chat"]["id"]),
                message_thread_id=raw.get("message_thread_id"),
            )
        )
        message = self.parse_telegram_message(raw, thread_id)
        self.cache_message(message)
        return message

    def render_formatted(self, content: FormattedContent) -> str:
        """Render formatted content to plain markdown text."""
        return self._format_converter.from_ast(content)

    def parse_telegram_message(
        self,
        raw: TelegramMessage,
        thread_id: str,
    ) -> Message:
        """Parse a Telegram message into a normalised :class:`Message`."""
        plain_text = raw.get("text") or raw.get("caption") or ""
        entities = raw.get("entities") or raw.get("caption_entities") or []
        text = apply_telegram_entities(plain_text, entities)

        # Determine author -- Telegram uses 'from' key which is a reserved word
        from_user = cast(
            "TelegramUser | None",
            raw.get("from_user") or raw.get("from"),  # type: ignore[call-overload]
        )
        sender_chat = raw.get("sender_chat")

        if from_user:
            author = self.to_author(from_user)
        elif sender_chat:
            author = self.to_reaction_actor_author(sender_chat)
        else:
            fallback_name = self.chat_display_name(raw["chat"]) or str(raw["chat"]["id"])
            author = Author(
                user_id=str(raw["chat"]["id"]),
                user_name=fallback_name,
                full_name=fallback_name,
                is_bot="unknown",
                is_me=False,
            )

        edit_date = raw.get("edit_date")

        return Message(
            id=self.encode_message_id(str(raw["chat"]["id"]), raw["message_id"]),
            thread_id=thread_id,
            text=text,
            formatted=self._format_converter.to_ast(text),
            raw=raw,
            author=author,
            metadata=MessageMetadata(
                date_sent=datetime.fromtimestamp(raw["date"], tz=timezone.utc),
                edited=edit_date is not None,
                edited_at=(datetime.fromtimestamp(edit_date, tz=timezone.utc) if edit_date is not None else None),
            ),
            attachments=self.extract_attachments(raw),
            is_mention=self.is_bot_mentioned(raw, plain_text),
        )

    # -- Attachments ---------------------------------------------------------

    def extract_attachments(self, raw: TelegramMessage) -> list[Attachment]:
        """Extract file attachments from a Telegram message."""
        attachments: list[Attachment] = []

        photos = raw.get("photo")
        if photos:
            photo = photos[-1]  # Largest resolution
            attachments.append(
                self.create_attachment(
                    "image",
                    photo["file_id"],
                    size=photo.get("file_size"),
                    width=photo.get("width"),
                    height=photo.get("height"),
                )
            )

        video = raw.get("video")
        if video:
            attachments.append(
                self.create_attachment(
                    "video",
                    video["file_id"],
                    size=video.get("file_size"),
                    width=video.get("width"),
                    height=video.get("height"),
                    name=video.get("file_name"),
                    mime_type=video.get("mime_type"),
                )
            )

        audio = raw.get("audio")
        if audio:
            attachments.append(
                self.create_attachment(
                    "audio",
                    audio["file_id"],
                    size=audio.get("file_size"),
                    name=audio.get("file_name"),
                    mime_type=audio.get("mime_type"),
                )
            )

        voice = raw.get("voice")
        if voice:
            attachments.append(
                self.create_attachment(
                    "audio",
                    voice["file_id"],
                    size=voice.get("file_size"),
                    mime_type=voice.get("mime_type"),
                )
            )

        document = raw.get("document")
        if document:
            attachments.append(
                self.create_attachment(
                    "file",
                    document["file_id"],
                    size=document.get("file_size"),
                    name=document.get("file_name"),
                    mime_type=document.get("mime_type"),
                )
            )

        return attachments

    def create_attachment(
        self,
        type_: str,
        file_id: str,
        *,
        size: int | None = None,
        width: int | None = None,
        height: int | None = None,
        name: str | None = None,
        mime_type: str | None = None,
    ) -> Attachment:
        """Create an :class:`Attachment` with a lazy ``fetch_data`` callback."""
        return Attachment(
            type=type_,  # type: ignore[arg-type]
            size=size,
            width=width,
            height=height,
            name=name,
            mime_type=mime_type,
            fetch_data=lambda _fid=file_id: self.download_file(_fid),
            fetch_metadata={"fileId": file_id},
        )

    def rehydrate_attachment(self, attachment: Attachment) -> Attachment:
        """Reconstruct ``fetch_data`` on a deserialized Telegram attachment.

        Pulls ``fileId`` from ``fetch_metadata`` and rebuilds the lazy
        ``download_file`` closure.  Returns the attachment unchanged when
        no file ID is present (e.g. a pre-serialized attachment that did
        not originate from this adapter).
        """
        meta = attachment.fetch_metadata if attachment.fetch_metadata is not None else {}
        file_id = meta.get("fileId")
        if not file_id:
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
            fetch_data=lambda _fid=file_id: self.download_file(_fid),
            fetch_metadata=attachment.fetch_metadata,
        )

    async def download_file(self, file_id: str) -> bytes:
        """Download a file from Telegram by its ``file_id``."""
        import aiohttp

        file_info: TelegramFile = await self.telegram_fetch(
            "getFile",
            {"file_id": file_id},
        )

        file_path = file_info.get("file_path")
        if not file_path:
            raise ResourceNotFoundError("telegram", "file", file_id)

        file_url = f"{self._api_base_url}/file/bot{self._bot_token}/{file_path}"

        try:
            session = await self._get_http_session()
            async with session.get(file_url) as response:
                if not response.ok:
                    raise NetworkError(
                        "telegram",
                        f"Failed to download Telegram file {file_id}: {response.status}",
                    )
                return await response.read()
        except aiohttp.ClientError as error:
            raise NetworkError(
                "telegram",
                f"Failed to download Telegram file {file_id}",
                error,
            ) from error

    async def send_document(
        self,
        thread: TelegramThreadId,
        file: Any,
        text: str,
        reply_markup: TelegramInlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> TelegramMessage:
        """Send a document (file upload) to Telegram."""
        import aiohttp

        data = getattr(file, "data", b"")
        if isinstance(data, memoryview) or not isinstance(data, bytes):
            data = bytes(data)

        form_data = aiohttp.FormData()
        form_data.add_field("chat_id", thread.chat_id)

        if isinstance(thread.message_thread_id, int):
            form_data.add_field("message_thread_id", str(thread.message_thread_id))

        if text.strip():
            form_data.add_field("caption", self.truncate_caption(text, parse_mode))
            if parse_mode:
                form_data.add_field("parse_mode", parse_mode)

        filename = getattr(file, "filename", "file")
        content_type = getattr(file, "mime_type", None) or "application/octet-stream"
        form_data.add_field(
            "document",
            data,
            filename=filename,
            content_type=content_type,
        )

        if reply_markup:
            form_data.add_field("reply_markup", json.dumps(reply_markup))

        return await self.telegram_fetch("sendDocument", form_data)

    # -- Message caching -----------------------------------------------------

    def cache_message(self, message: Message) -> None:
        """Store or update a message in the in-memory cache."""
        existing = self._message_cache.get(message.thread_id, [])
        index = next(
            (i for i, m in enumerate(existing) if m.id == message.id),
            -1,
        )

        if index >= 0:
            existing[index] = message
        else:
            existing.append(message)

        existing.sort(key=lambda m: (m.metadata.date_sent, self.message_sequence(m.id)))
        self._message_cache[message.thread_id] = existing

    def find_cached_message(self, message_id: str) -> Message | None:
        """Find a cached message by ID across all threads."""
        for messages in self._message_cache.values():
            for msg in messages:
                if msg.id == message_id:
                    return msg
        return None

    def delete_cached_message(self, message_id: str) -> None:
        """Remove a message from the cache."""
        for tid in list(self._message_cache.keys()):
            messages = self._message_cache[tid]
            filtered = [m for m in messages if m.id != message_id]
            if not filtered:
                del self._message_cache[tid]
            elif len(filtered) != len(messages):
                self._message_cache[tid] = filtered

    def compare_messages(self, a: Message, b: Message) -> int:
        """Compare two messages for sorting (older first)."""
        time_diff = a.metadata.date_sent.timestamp() - b.metadata.date_sent.timestamp()
        if time_diff != 0:
            return -1 if time_diff < 0 else 1

        return self.message_sequence(a.id) - self.message_sequence(b.id)

    def message_sequence(self, message_id: str) -> int:
        """Extract the numeric sequence from a composite message ID."""
        match = MESSAGE_SEQUENCE_PATTERN.search(message_id)
        return int(match.group(1)) if match else 0

    # -- Pagination ----------------------------------------------------------

    def paginate_messages(
        self,
        messages: list[Message],
        options: FetchOptions,
    ) -> FetchResult:
        """Paginate a list of messages according to fetch options."""
        limit = max(1, min(getattr(options, "limit", 50) if getattr(options, "limit", 50) is not None else 50, 100))
        direction = getattr(options, "direction", "backward") or "backward"

        if not messages:
            return FetchResult(messages=[])

        message_index_by_id: dict[str, int] = {m.id: i for i, m in enumerate(messages)}

        cursor = getattr(options, "cursor", None)

        if direction == "backward":
            end = message_index_by_id[cursor] if cursor and cursor in message_index_by_id else len(messages)
            start = max(0, end - limit)
            page = messages[start:end]

            return FetchResult(
                messages=page,
                next_cursor=page[0].id if (start > 0 and page) else None,
            )

        # forward
        start = message_index_by_id[cursor] + 1 if cursor and cursor in message_index_by_id else 0
        end = min(len(messages), start + limit)
        page = messages[start:end]

        return FetchResult(
            messages=page,
            next_cursor=page[-1].id if (end < len(messages) and page) else None,
        )

    # -- Message ID encoding / decoding --------------------------------------

    def encode_message_id(self, chat_id: str, message_id: int) -> str:
        """Encode a chat ID and message ID into a composite string."""
        return f"{chat_id}:{message_id}"

    def decode_composite_message_id(
        self,
        message_id: str,
        expected_chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Decode a composite message ID string.

        Returns a dict with ``chat_id``, ``message_id`` (int), and ``composite_id``.
        """
        composite_match = MESSAGE_ID_PATTERN.match(message_id)

        if composite_match:
            chat_id = composite_match.group(1)
            raw_message_id = composite_match.group(2)
            parsed_message_id = int(raw_message_id)

            if expected_chat_id and chat_id != expected_chat_id:
                raise ValidationError(
                    "telegram",
                    f"Message ID chat mismatch: expected {expected_chat_id}, got {chat_id}",
                )

            return {
                "chat_id": chat_id,
                "message_id": parsed_message_id,
                "composite_id": f"{chat_id}:{parsed_message_id}",
            }

        if not expected_chat_id:
            raise ValidationError(
                "telegram",
                f"Telegram message ID must be in <chatId>:<messageId> format, got: {message_id}",
            )

        try:
            parsed_message_id = int(message_id)
        except (ValueError, TypeError) as exc:
            raise ValidationError(
                "telegram",
                f"Invalid Telegram message ID: {message_id}",
            ) from exc

        if not math.isfinite(parsed_message_id):
            raise ValidationError(
                "telegram",
                f"Invalid Telegram message ID: {message_id}",
            )

        return {
            "chat_id": expected_chat_id,
            "message_id": parsed_message_id,
            "composite_id": f"{expected_chat_id}:{parsed_message_id}",
        }

    # -- Author helpers ------------------------------------------------------

    def to_author(self, user: TelegramUser) -> Author:
        """Convert a Telegram user to an :class:`Author`."""
        full_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()

        return Author(
            user_id=str(user.get("id", "")),
            user_name=user.get("username") or user.get("first_name") or str(user.get("id", "")),
            full_name=full_name or user.get("username") or str(user.get("id", "")),
            is_bot=user.get("is_bot", False),
            is_me=str(user.get("id", "")) == self._bot_user_id,
        )

    def to_reaction_actor_author(self, chat: TelegramChat) -> Author:
        """Convert a Telegram chat to an :class:`Author` for reaction events."""
        name = self.chat_display_name(chat) or str(chat.get("id", ""))
        return Author(
            user_id=f"chat:{chat.get('id', '')}",
            user_name=name,
            full_name=name,
            is_bot="unknown",
            is_me=False,
        )

    def chat_display_name(self, chat: TelegramChat) -> str | None:
        """Get the display name for a Telegram chat."""
        title = chat.get("title")
        if title:
            return title

        private_name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])).strip()
        if private_name:
            return private_name

        return chat.get("username")

    # -- Mention detection ---------------------------------------------------

    def is_bot_mentioned(self, message: TelegramMessage, text: str) -> bool:
        """Check if the bot is mentioned in a message."""
        if not text:
            return False

        username = self._user_name
        entities = message.get("entities") or message.get("caption_entities") or []

        for entity in entities:
            entity_type = entity.get("type", "")

            if entity_type == "mention":
                mention_text = self.entity_text(text, entity)
                if mention_text.lower() == f"@{username.lower()}":
                    return True

            if entity_type == "text_mention":
                entity_user = entity.get("user")
                if entity_user and self._bot_user_id and str(entity_user.get("id", "")) == self._bot_user_id:
                    return True

            if entity_type == "bot_command":
                command_text = self.entity_text(text, entity)
                if command_text.lower().endswith(f"@{username.lower()}"):
                    return True

        mention_regex = re.compile(rf"@{self.escape_regex(username)}\b", re.IGNORECASE)
        return bool(mention_regex.search(text))

    def entity_text(self, text: str, entity: TelegramMessageEntity) -> str:
        """Extract entity text from a message using UTF-16 offsets."""
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        # Use UTF-16 encoding for correct offset handling
        utf16 = text.encode("utf-16-le")
        return utf16[offset * 2 : (offset + length) * 2].decode("utf-16-le")

    @staticmethod
    def escape_regex(input_str: str) -> str:
        """Escape regex special characters."""
        return re.escape(input_str)

    def normalize_user_name(self, value: Any) -> str:
        """Normalize a username by stripping leading ``@`` characters."""
        if not isinstance(value, str):
            return "bot"
        result = LEADING_AT_PATTERN.sub("", value).strip()
        return result or "bot"

    # -- Parse mode ----------------------------------------------------------

    def resolve_parse_mode(
        self,
        message: AdapterPostableMessage,
        card: Any,
    ) -> str | None:
        """Determine the Telegram ``parse_mode`` for an outgoing message.

        Cards and any message routed through the format converter are
        rendered as MarkdownV2, so Telegram must parse them with
        ``MarkdownV2``. Plain strings and ``{"raw": ...}`` payloads ship
        verbatim with no parse mode (Bot API field omitted).
        """
        if card:
            return TELEGRAM_MARKDOWN_PARSE_MODE
        # Plain strings ship as-is.
        if isinstance(message, str):
            return None
        # ``{"raw": ...}`` and dataclasses with ``.raw`` ship as-is.
        if isinstance(message, dict) and "raw" in message:
            return None
        if hasattr(message, "raw") and not isinstance(message, str):
            return None
        # Every other shape ({markdown}, {ast}, JSX, etc.) flows through
        # format_converter.render_postable, which emits MarkdownV2.
        return TELEGRAM_MARKDOWN_PARSE_MODE

    # -- Truncation ----------------------------------------------------------

    def truncate_message(self, text: str, parse_mode: str | None = None) -> str:
        """Truncate message text to the Telegram message limit.

        For ``parse_mode == "MarkdownV2"`` uses :func:`truncate_for_telegram`,
        which escapes the ellipsis and walks back past any unbalanced
        entity delimiter or orphan backslash so the result is parseable.
        For plain text, falls back to UTF-16 truncation with a literal
        ``"..."`` ellipsis.
        """
        if parse_mode == "MarkdownV2":
            return truncate_for_telegram(text, TELEGRAM_MESSAGE_LIMIT, parse_mode)
        return _truncate_to_utf16(text, TELEGRAM_MESSAGE_LIMIT)

    def truncate_caption(self, text: str, parse_mode: str | None = None) -> str:
        """Truncate caption text to the Telegram caption limit.

        See :meth:`truncate_message` for parse-mode handling.
        """
        if parse_mode == "MarkdownV2":
            return truncate_for_telegram(text, TELEGRAM_CAPTION_LIMIT, parse_mode)
        return _truncate_to_utf16(text, TELEGRAM_CAPTION_LIMIT)

    # -- Emoji / reactions ---------------------------------------------------

    def to_telegram_reaction(self, emoji: EmojiValue | str) -> TelegramReactionType:
        """Convert an emoji value to a Telegram reaction type."""
        if not isinstance(emoji, str):
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(emoji),
            }

        if emoji.startswith("custom:"):
            return {
                "type": "custom_emoji",
                "custom_emoji_id": emoji[len("custom:") :],
            }

        placeholder_match = EMOJI_PLACEHOLDER_PATTERN.match(emoji)
        if placeholder_match:
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(placeholder_match.group(1)),
            }

        if EMOJI_NAME_PATTERN.match(emoji):
            return {
                "type": "emoji",
                "emoji": emoji_to_unicode(emoji.lower()),
            }

        return {
            "type": "emoji",
            "emoji": emoji,
        }

    def reaction_key(self, reaction: TelegramReactionType) -> str:
        """Compute a unique key for a Telegram reaction."""
        # `TelegramReactionType` is a TypedDict union; `.get()` returns the
        # union of all value-types, so narrow the strings we know are strings.
        if reaction.get("type") == "emoji":
            return cast("str", reaction.get("emoji", ""))
        return f"custom:{cast('str', reaction.get('custom_emoji_id', ''))}"

    def reaction_to_emoji_value(self, reaction: TelegramReactionType) -> EmojiValue:
        """Convert a Telegram reaction to an :class:`EmojiValue`."""
        if reaction.get("type") == "emoji":
            return get_emoji(cast("str", reaction.get("emoji", "")))
        return get_emoji(f"custom:{cast('str', reaction.get('custom_emoji_id', ''))}")

    # -- Telegram API --------------------------------------------------------

    async def telegram_fetch(
        self,
        method: str,
        payload: Any = None,
        *,
        signal: asyncio.Event | None = None,  # noqa: ARG002 - reserved for future cancellation support
    ) -> Any:
        """Call a Telegram Bot API method.

        Accepts either a dict payload (sent as JSON) or an
        ``aiohttp.FormData`` payload (sent as multipart/form-data).

        *signal* is currently unused but kept for API compatibility.
        """
        import aiohttp

        url = f"{self._api_base_url}/bot{self._bot_token}/{method}"
        is_form = False

        with contextlib.suppress(Exception):
            is_form = isinstance(payload, aiohttp.FormData)

        try:
            session = await self._get_http_session()
            if is_form:
                async with session.post(url, data=payload) as response:
                    data = await self._parse_telegram_response(method, response)
            else:
                async with session.post(
                    url,
                    json=payload or {},
                    headers={"Content-Type": "application/json"},
                ) as response:
                    data = await self._parse_telegram_response(method, response)
        except (aiohttp.ClientError, OSError) as error:
            raise NetworkError(
                "telegram",
                f"Network error calling Telegram {method}",
                error,
            ) from error

        return data

    async def _parse_telegram_response(
        self,
        method: str,
        response: Any,
    ) -> Any:
        """Parse a Telegram API response, raising appropriate errors."""
        try:
            data: TelegramApiResponse = await response.json(content_type=None)
        except Exception as exc:
            raise NetworkError(
                "telegram",
                f"Failed to parse Telegram API response for {method}",
            ) from exc

        if not (response.ok and data.get("ok")):
            self.throw_telegram_api_error(method, response.status, data)

        result = data.get("result")
        if result is None and "result" not in data:
            raise NetworkError(
                "telegram",
                f"Telegram API {method} returned no result",
            )

        return result

    def throw_telegram_api_error(
        self,
        method: str,
        status: int,
        data: TelegramApiResponse,
    ) -> None:
        """Raise the appropriate error for a Telegram API failure."""
        error_code = data.get("error_code") or status
        description = data.get("description") or f"Telegram API {method} failed"

        if error_code == 429:
            params = data.get("parameters") or {}
            raise AdapterRateLimitError("telegram", params.get("retry_after"))

        if error_code == 401:
            raise AuthenticationError("telegram", description)

        if error_code == 403:
            raise AdapterPermissionError("telegram", method)

        if error_code == 404:
            raise ResourceNotFoundError("telegram", method)

        if 400 <= error_code < 500:
            raise ValidationError("telegram", description)

        raise NetworkError(
            "telegram",
            f"{description} (status {status}, error {error_code})",
        )

    # -- Polling config resolution -------------------------------------------

    def resolve_polling_config(
        self,
        override: TelegramLongPollingConfig | None = None,
    ) -> ResolvedTelegramLongPollingConfig:
        """Merge and validate polling configuration."""
        base = self._long_polling or TelegramLongPollingConfig()
        merged_allowed_updates = (
            override.allowed_updates if override and override.allowed_updates else None
        ) or base.allowed_updates
        merged_delete_webhook = (
            override.delete_webhook if (override and override.delete_webhook is not None) else base.delete_webhook
        )
        merged_drop_pending = (
            override.drop_pending_updates
            if (override and override.drop_pending_updates is not None)
            else base.drop_pending_updates
        )
        merged_limit = override.limit if (override and override.limit is not None) else base.limit
        merged_retry_delay = (
            override.retry_delay_ms if (override and override.retry_delay_ms is not None) else base.retry_delay_ms
        )
        merged_timeout = override.timeout if (override and override.timeout is not None) else base.timeout

        return ResolvedTelegramLongPollingConfig(
            allowed_updates=(list(merged_allowed_updates) if merged_allowed_updates else None),
            delete_webhook=merged_delete_webhook if merged_delete_webhook is not None else True,
            drop_pending_updates=bool(merged_drop_pending) if merged_drop_pending is not None else False,
            limit=self.clamp_integer(
                merged_limit,
                TELEGRAM_DEFAULT_POLLING_LIMIT,
                TELEGRAM_MIN_POLLING_LIMIT,
                TELEGRAM_MAX_POLLING_LIMIT,
            ),
            retry_delay_ms=self.clamp_integer(
                merged_retry_delay,
                TELEGRAM_DEFAULT_POLLING_RETRY_DELAY_MS,
                0,
                2**53 - 1,
            ),
            timeout=self.clamp_integer(
                merged_timeout,
                TELEGRAM_DEFAULT_POLLING_TIMEOUT_SECONDS,
                TELEGRAM_MIN_POLLING_TIMEOUT_SECONDS,
                TELEGRAM_MAX_POLLING_TIMEOUT_SECONDS,
            ),
        )

    @staticmethod
    def clamp_integer(
        value: int | float | None,
        fallback: int,
        min_val: int,
        max_val: int,
    ) -> int:
        """Clamp a numeric value to an integer within ``[min_val, max_val]``."""
        if value is None or not isinstance(value, (int, float)) or not math.isfinite(value):
            return fallback
        parsed = int(math.trunc(value))
        return max(min_val, min(max_val, parsed))

    # -- Private helpers -----------------------------------------------------

    def _resolve_thread_id(self, value: str) -> TelegramThreadId:
        """Resolve a string to a :class:`TelegramThreadId`."""
        if value.startswith("telegram:"):
            return self.decode_thread_id(value)
        return TelegramThreadId(chat_id=value)

    @staticmethod
    async def _get_request_body(request: Any) -> str:
        """Extract body text from a framework-agnostic request object."""
        # `hasattr` narrows `Any` → `object` (not awaitable); `getattr(..., None)`
        # preserves `Any` for the duck-typed framework paths.
        # Handle both callable and non-callable `request.text` forms.
        # Gating entry on callability would drop populated string attrs.
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            return text_attr.decode("utf-8") if isinstance(text_attr, (bytes, bytearray)) else str(text_attr)
        body = getattr(request, "body", None)
        if body is not None:
            if callable(body):
                result = body()
                body = await result if inspect.isawaitable(result) else result
            if isinstance(body, (bytes, bytearray)):
                return body.decode("utf-8")
            return str(body)
        return ""

    @staticmethod
    def _get_header(request: Any, name: str) -> str | None:
        """Get a header value from a framework-agnostic request object."""
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


# =========================================================================
# Factory function
# =========================================================================


def create_telegram_adapter(
    config: TelegramAdapterConfig | None = None,
) -> TelegramAdapter:
    """Create a new :class:`TelegramAdapter` instance."""
    return TelegramAdapter(config or TelegramAdapterConfig())
