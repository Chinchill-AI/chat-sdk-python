"""Telegram adapter types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from chat_sdk.logger import Logger

# =============================================================================
# Adapter Configuration
# =============================================================================

TelegramAdapterMode = Literal["auto", "webhook", "polling"]


@dataclass
class TelegramLongPollingConfig:
    """Telegram long-polling configuration.

    See https://core.telegram.org/bots/api#getupdates
    """

    allowed_updates: list[str] | None = None
    """Allowed update types passed to getUpdates."""

    delete_webhook: bool | None = None
    """Delete webhook before polling starts.
    Telegram requires this when switching from webhook mode to getUpdates.
    Defaults to True.
    """

    drop_pending_updates: bool | None = None
    """Passed to deleteWebhook as drop_pending_updates when deleting webhook."""

    limit: int | None = None
    """Maximum number of updates per getUpdates call. Telegram range: 1-100. Default: 100."""

    retry_delay_ms: int | None = None
    """Delay before retrying polling after errors. Default: 1000."""

    timeout: int | None = None
    """Long-poll timeout in seconds for getUpdates. Default: 30."""


@dataclass
class TelegramAdapterConfig:
    """Telegram adapter configuration."""

    api_base_url: str | None = None
    """Optional custom API base URL (defaults to https://api.telegram.org).
    Defaults to TELEGRAM_API_BASE_URL env var.
    """

    bot_token: str | None = None
    """Telegram bot token from BotFather. Defaults to TELEGRAM_BOT_TOKEN env var."""

    logger: Logger | None = None
    """Logger instance for error reporting."""

    long_polling: TelegramLongPollingConfig | None = None
    """Optional long-polling configuration for getUpdates flow."""

    mode: TelegramAdapterMode | None = None
    """Adapter runtime mode:
    - auto: choose webhook vs polling based on webhook registration/runtime (default)
    - webhook: webhook-only mode
    - polling: polling-only mode
    """

    secret_token: str | None = None
    """Optional webhook secret token checked against x-telegram-bot-api-secret-token.
    Defaults to TELEGRAM_WEBHOOK_SECRET_TOKEN env var.
    """

    user_name: str | None = None
    """Override bot username (optional). Defaults to TELEGRAM_BOT_USERNAME env var."""


# =============================================================================
# Thread ID
# =============================================================================


@dataclass
class TelegramThreadId:
    """Telegram thread ID components."""

    chat_id: str
    """Telegram chat ID."""

    message_thread_id: int | None = None
    """Optional forum topic ID for supergroup topics."""


# =============================================================================
# Telegram API types
# =============================================================================


class TelegramUser(TypedDict, total=False):
    """Telegram user object.

    See https://core.telegram.org/bots/api#user
    """

    first_name: str  # required
    id: int  # required
    is_bot: bool  # required
    language_code: str
    last_name: str
    username: str


class TelegramChat(TypedDict, total=False):
    """Telegram chat object.

    See https://core.telegram.org/bots/api#chat
    """

    first_name: str
    id: int  # required
    last_name: str
    title: str
    type: str  # "private" | "group" | "supergroup" | "channel"  # required
    username: str


class TelegramMessageEntity(TypedDict, total=False):
    """Telegram message entity (mentions, links, commands, etc).

    See https://core.telegram.org/bots/api#messageentity
    """

    language: str
    length: int  # required
    offset: int  # required
    type: str  # required
    url: str
    user: TelegramUser


class TelegramFile(TypedDict, total=False):
    """Telegram file metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str


class TelegramPhotoSize(TypedDict, total=False):
    """Telegram photo size object."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    height: int  # required
    width: int  # required


class TelegramAudioFile(TypedDict, total=False):
    """Telegram audio file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int
    performer: str
    title: str
    mime_type: str
    file_name: str


class TelegramDocumentFile(TypedDict, total=False):
    """Telegram document file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    file_name: str
    mime_type: str


class TelegramStickerFile(TypedDict, total=False):
    """Telegram sticker file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    emoji: str


class TelegramVideoFile(TypedDict, total=False):
    """Telegram video file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    width: int
    height: int
    mime_type: str
    file_name: str


class TelegramVoiceFile(TypedDict, total=False):
    """Telegram voice file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int
    mime_type: str


class TelegramMessage(TypedDict, total=False):
    """Telegram message.

    See https://core.telegram.org/bots/api#message
    """

    audio: TelegramAudioFile
    caption: str
    caption_entities: list[TelegramMessageEntity]
    chat: TelegramChat  # required
    date: int  # required
    document: TelegramDocumentFile
    edit_date: int
    entities: list[TelegramMessageEntity]
    from_user: TelegramUser  # Note: 'from' is a Python keyword; mapped to 'from_user' in parsing
    message_id: int  # required
    message_thread_id: int
    photo: list[TelegramPhotoSize]
    sender_chat: TelegramChat
    sticker: TelegramStickerFile
    text: str
    video: TelegramVideoFile
    voice: TelegramVoiceFile


class TelegramInlineKeyboardButton(TypedDict, total=False):
    """Telegram inline keyboard button.

    See https://core.telegram.org/bots/api#inlinekeyboardbutton
    """

    callback_data: str
    text: str  # required
    url: str


class TelegramInlineKeyboardMarkup(TypedDict):
    """Telegram inline keyboard markup.

    See https://core.telegram.org/bots/api#inlinekeyboardmarkup
    """

    inline_keyboard: list[list[TelegramInlineKeyboardButton]]


class TelegramCallbackQuery(TypedDict, total=False):
    """Telegram callback query (inline keyboard button click).

    See https://core.telegram.org/bots/api#callbackquery
    """

    chat_instance: str  # required
    data: str
    from_user: TelegramUser  # Note: 'from' mapped to 'from_user'  # required
    id: str  # required
    inline_message_id: str
    message: TelegramMessage


class TelegramEmojiReaction(TypedDict):
    """Telegram emoji reaction type."""

    type: str  # "emoji"
    emoji: str


class TelegramCustomEmojiReaction(TypedDict):
    """Telegram custom emoji reaction type."""

    type: str  # "custom_emoji"
    custom_emoji_id: str


# Union type for reactions
TelegramReactionType = TelegramEmojiReaction | TelegramCustomEmojiReaction


class TelegramMessageReactionUpdated(TypedDict, total=False):
    """Telegram message reaction update.

    See https://core.telegram.org/bots/api#messagereactionupdated
    """

    actor_chat: TelegramChat
    chat: TelegramChat  # required
    date: int  # required
    message_id: int  # required
    message_thread_id: int
    new_reaction: list[TelegramReactionType]  # required
    old_reaction: list[TelegramReactionType]  # required
    user: TelegramUser


class TelegramUpdate(TypedDict, total=False):
    """Telegram webhook update payload.

    See https://core.telegram.org/bots/api#update
    """

    callback_query: TelegramCallbackQuery
    channel_post: TelegramMessage
    edited_channel_post: TelegramMessage
    edited_message: TelegramMessage
    message: TelegramMessage
    message_reaction: TelegramMessageReactionUpdated
    update_id: int  # required


class TelegramApiResponse(TypedDict, total=False):
    """Telegram API response envelope."""

    description: str
    error_code: int
    ok: bool  # required
    parameters: dict[str, Any]
    result: Any


class TelegramWebhookInfo(TypedDict, total=False):
    """Telegram webhook info response.

    See https://core.telegram.org/bots/api#getwebhookinfo
    """

    allowed_updates: list[str]
    has_custom_certificate: bool  # required
    ip_address: str
    last_error_date: int
    last_error_message: str
    max_connections: int
    pending_update_count: int  # required
    url: str  # required


# Alias for raw message type
TelegramRawMessage = TelegramMessage
