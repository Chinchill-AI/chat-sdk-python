"""Telegram adapter types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, TypedDict

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


class TelegramAnimation(TypedDict, total=False):
    """Telegram animation (GIF or H.264/MPEG-4 AVC video without sound).

    Extends TelegramFile. See https://core.telegram.org/bots/api#animation
    """

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int  # required
    file_name: str
    height: int  # required
    mime_type: str
    thumbnail: TelegramPhotoSize
    width: int  # required


class TelegramAudio(TypedDict, total=False):
    """Telegram audio file to be treated as music.

    Extends TelegramFile. See https://core.telegram.org/bots/api#audio
    """

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int  # required
    file_name: str
    mime_type: str
    performer: str
    thumbnail: TelegramPhotoSize
    title: str


class TelegramLocation(TypedDict, total=False):
    """Telegram location on a map.

    See https://core.telegram.org/bots/api#location
    """

    heading: int
    horizontal_accuracy: float
    latitude: float  # required
    live_period: int
    longitude: float  # required
    proximity_alert_radius: int


class TelegramVideoQuality(TypedDict, total=False):
    """Available quality of a Telegram video.

    Extends TelegramFile. See https://core.telegram.org/bots/api#video
    """

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    codec: str  # required
    height: int  # required
    width: int  # required


class TelegramVideo(TypedDict, total=False):
    """Telegram video file.

    Extends TelegramFile. See https://core.telegram.org/bots/api#video
    """

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    cover: list[TelegramPhotoSize]
    duration: int  # required
    file_name: str
    height: int  # required
    mime_type: str
    qualities: list[TelegramVideoQuality]
    start_timestamp: int
    thumbnail: TelegramPhotoSize
    width: int  # required


class TelegramVoice(TypedDict, total=False):
    """Telegram voice note.

    Extends TelegramFile. See https://core.telegram.org/bots/api#voice
    """

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int  # required
    mime_type: str


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


class TelegramVideoNoteFile(TypedDict, total=False):
    """Telegram video note (round video message) with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    length: int
    duration: int


class TelegramVoiceFile(TypedDict, total=False):
    """Telegram voice file with extra metadata."""

    file_id: str  # required
    file_path: str
    file_size: int
    file_unique_id: str
    duration: int
    mime_type: str


# =============================================================================
# Rich message wire types (Bot API 10.1)
#
# Rich messages are recursive: a TelegramRichText may nest further
# TelegramRichText, and a TelegramRichBlock may nest further blocks. The
# recursion is expressed with quoted forward references in TypeAlias string
# RHS values (e.g. "... | list[TelegramRichText] | ...") so the aliases never
# evaluate ``|`` against a not-yet-defined name at runtime while pyrefly still
# resolves them statically.
#
# Wire keys are snake_case: the Telegram Bot API is natively snake_case, so
# there is no camelCase serialization boundary here (unlike most adapters).
# =============================================================================


class TelegramRichTextStyled(TypedDict):
    """Styled rich-text span (bold, italic, code, etc)."""

    type: Literal[
        "bold",
        "italic",
        "underline",
        "strikethrough",
        "spoiler",
        "subscript",
        "superscript",
        "marked",
        "code",
    ]
    text: TelegramRichText


class TelegramRichTextDateTime(TypedDict):
    """Rich-text date/time span."""

    type: Literal["date_time"]
    text: TelegramRichText
    unix_time: int
    date_time_format: str


class TelegramRichTextTextMention(TypedDict):
    """Rich-text mention of a user without a username."""

    type: Literal["text_mention"]
    text: TelegramRichText
    user: TelegramUser


class TelegramRichTextCustomEmoji(TypedDict):
    """Rich-text custom emoji span."""

    type: Literal["custom_emoji"]
    alternative_text: str
    custom_emoji_id: str


class TelegramRichTextMath(TypedDict):
    """Rich-text inline mathematical expression."""

    type: Literal["mathematical_expression"]
    expression: str


class TelegramRichTextUrl(TypedDict):
    """Rich-text URL span."""

    type: Literal["url"]
    text: TelegramRichText
    url: str


class TelegramRichTextEmailAddress(TypedDict):
    """Rich-text email address span."""

    type: Literal["email_address"]
    email_address: str
    text: TelegramRichText


class TelegramRichTextPhoneNumber(TypedDict):
    """Rich-text phone number span."""

    type: Literal["phone_number"]
    phone_number: str
    text: TelegramRichText


class TelegramRichTextBankCardNumber(TypedDict):
    """Rich-text bank card number span."""

    type: Literal["bank_card_number"]
    bank_card_number: str
    text: TelegramRichText


class TelegramRichTextMention(TypedDict):
    """Rich-text @username mention span."""

    type: Literal["mention"]
    text: TelegramRichText
    username: str


class TelegramRichTextHashtag(TypedDict):
    """Rich-text hashtag span."""

    type: Literal["hashtag"]
    hashtag: str
    text: TelegramRichText


class TelegramRichTextCashtag(TypedDict):
    """Rich-text cashtag span."""

    type: Literal["cashtag"]
    cashtag: str
    text: TelegramRichText


class TelegramRichTextBotCommand(TypedDict):
    """Rich-text bot command span."""

    type: Literal["bot_command"]
    bot_command: str
    text: TelegramRichText


class TelegramRichTextAnchor(TypedDict):
    """Rich-text anchor target span."""

    type: Literal["anchor"]
    name: str


class TelegramRichTextAnchorLink(TypedDict):
    """Rich-text anchor link span."""

    type: Literal["anchor_link"]
    anchor_name: str
    text: TelegramRichText


class TelegramRichTextReference(TypedDict):
    """Rich-text reference target span."""

    type: Literal["reference"]
    name: str
    text: TelegramRichText


class TelegramRichTextReferenceLink(TypedDict):
    """Rich-text reference link span."""

    type: Literal["reference_link"]
    reference_name: str
    text: TelegramRichText


# Recursive rich-text union. The RHS is a quoted forward reference so the
# self-reference (``list[TelegramRichText]`` and the ``text`` fields above)
# resolves statically without a runtime NameError.
# See https://core.telegram.org/bots/api#richtext
TelegramRichText: TypeAlias = "str | list[TelegramRichText] | TelegramRichTextStyled | TelegramRichTextDateTime | TelegramRichTextTextMention | TelegramRichTextCustomEmoji | TelegramRichTextMath | TelegramRichTextUrl | TelegramRichTextEmailAddress | TelegramRichTextPhoneNumber | TelegramRichTextBankCardNumber | TelegramRichTextMention | TelegramRichTextHashtag | TelegramRichTextCashtag | TelegramRichTextBotCommand | TelegramRichTextAnchor | TelegramRichTextAnchorLink | TelegramRichTextReference | TelegramRichTextReferenceLink"  # noqa: E501


class TelegramRichCaption(TypedDict, total=False):
    """Caption attached to a rich message block.

    See https://core.telegram.org/bots/api#richblockcaption
    """

    credit: TelegramRichText
    text: TelegramRichText  # required


class TelegramRichCell(TypedDict, total=False):
    """Cell in a rich message table.

    See https://core.telegram.org/bots/api#richblocktablecell
    """

    align: Literal["left", "center", "right"]  # required
    colspan: int
    is_header: Literal[True]
    rowspan: int
    text: TelegramRichText
    valign: Literal["top", "middle", "bottom"]  # required


class TelegramRichItem(TypedDict, total=False):
    """Item in a rich message list.

    See https://core.telegram.org/bots/api#richblocklistitem
    """

    blocks: list[TelegramRichBlock]  # required
    has_checkbox: Literal[True]
    is_checked: Literal[True]
    label: str  # required
    type: Literal["a", "A", "i", "I", "1"]
    value: int


class TelegramRichBlockText(TypedDict):
    """Rich block: paragraph, footer or thinking text."""

    type: Literal["paragraph", "footer", "thinking"]
    text: TelegramRichText


class TelegramRichBlockHeading(TypedDict):
    """Rich block: heading."""

    type: Literal["heading"]
    size: int
    text: TelegramRichText


class TelegramRichBlockPre(TypedDict, total=False):
    """Rich block: preformatted / code block."""

    type: Literal["pre"]  # required
    language: str
    text: TelegramRichText  # required


class TelegramRichBlockDivider(TypedDict):
    """Rich block: horizontal divider."""

    type: Literal["divider"]


class TelegramRichBlockMath(TypedDict):
    """Rich block: block-level mathematical expression."""

    type: Literal["mathematical_expression"]
    expression: str


class TelegramRichBlockAnchor(TypedDict):
    """Rich block: anchor target."""

    type: Literal["anchor"]
    name: str


class TelegramRichBlockList(TypedDict):
    """Rich block: list of items."""

    type: Literal["list"]
    items: list[TelegramRichItem]


class TelegramRichBlockBlockquote(TypedDict, total=False):
    """Rich block: blockquote."""

    type: Literal["blockquote"]  # required
    blocks: list[TelegramRichBlock]  # required
    credit: TelegramRichText


class TelegramRichBlockPullquote(TypedDict, total=False):
    """Rich block: pullquote."""

    type: Literal["pullquote"]  # required
    credit: TelegramRichText
    text: TelegramRichText  # required


class TelegramRichBlockCollage(TypedDict, total=False):
    """Rich block: collage or slideshow of nested blocks."""

    type: Literal["collage", "slideshow"]  # required
    blocks: list[TelegramRichBlock]  # required
    caption: TelegramRichCaption


class TelegramRichBlockTable(TypedDict, total=False):
    """Rich block: table."""

    type: Literal["table"]  # required
    caption: TelegramRichText
    cells: list[list[TelegramRichCell]]  # required
    is_bordered: Literal[True]
    is_striped: Literal[True]


class TelegramRichBlockDetails(TypedDict, total=False):
    """Rich block: collapsible details/summary."""

    type: Literal["details"]  # required
    blocks: list[TelegramRichBlock]  # required
    is_open: Literal[True]
    summary: TelegramRichText  # required


class TelegramRichBlockMap(TypedDict, total=False):
    """Rich block: map."""

    type: Literal["map"]  # required
    caption: TelegramRichCaption
    height: int  # required
    location: TelegramLocation  # required
    width: int  # required
    zoom: int  # required


class TelegramRichBlockAnimation(TypedDict, total=False):
    """Rich block: animation."""

    type: Literal["animation"]  # required
    animation: TelegramAnimation  # required
    caption: TelegramRichCaption
    has_spoiler: Literal[True]


class TelegramRichBlockAudio(TypedDict, total=False):
    """Rich block: audio."""

    type: Literal["audio"]  # required
    audio: TelegramAudio  # required
    caption: TelegramRichCaption


class TelegramRichBlockPhoto(TypedDict, total=False):
    """Rich block: photo."""

    type: Literal["photo"]  # required
    caption: TelegramRichCaption
    has_spoiler: Literal[True]
    photo: list[TelegramPhotoSize]  # required


class TelegramRichBlockVideo(TypedDict, total=False):
    """Rich block: video."""

    type: Literal["video"]  # required
    caption: TelegramRichCaption
    has_spoiler: Literal[True]
    video: TelegramVideo  # required


class TelegramRichBlockVoiceNote(TypedDict, total=False):
    """Rich block: voice note."""

    type: Literal["voice_note"]  # required
    caption: TelegramRichCaption
    voice_note: TelegramVoice  # required


# Recursive rich-block union. As with TelegramRichText, the RHS is a quoted
# forward reference so nested ``list[TelegramRichBlock]`` self-references
# resolve statically without a runtime NameError.
# See https://core.telegram.org/bots/api#richblock
TelegramRichBlock: TypeAlias = "TelegramRichBlockText | TelegramRichBlockHeading | TelegramRichBlockPre | TelegramRichBlockDivider | TelegramRichBlockMath | TelegramRichBlockAnchor | TelegramRichBlockList | TelegramRichBlockBlockquote | TelegramRichBlockPullquote | TelegramRichBlockCollage | TelegramRichBlockTable | TelegramRichBlockDetails | TelegramRichBlockMap | TelegramRichBlockAnimation | TelegramRichBlockAudio | TelegramRichBlockPhoto | TelegramRichBlockVideo | TelegramRichBlockVoiceNote"  # noqa: E501


class TelegramRichMessage(TypedDict, total=False):
    """Rich formatted message received from Telegram.

    See https://core.telegram.org/bots/api#richmessage
    """

    blocks: list[TelegramRichBlock]  # required
    is_rtl: bool


class TelegramMessage(TypedDict, total=False):
    """Telegram message.

    See https://core.telegram.org/bots/api#message

    Note: The Telegram API uses ``"from"`` as the key for the sender, but
    ``from`` is a Python reserved word and cannot be used as a TypedDict
    field name. This TypedDict declares it as ``from_user`` instead.
    The adapter handles this mismatch by reading both keys from the raw
    JSON: ``raw.get("from_user") or raw.get("from")``.
    """

    audio: TelegramAudioFile
    caption: str
    caption_entities: list[TelegramMessageEntity]
    chat: TelegramChat  # required
    date: int  # required
    document: TelegramDocumentFile
    edit_date: int
    entities: list[TelegramMessageEntity]
    # Telegram API sends "from" but that is a Python reserved word.
    # The adapter reads both keys: raw.get("from_user") or raw.get("from").
    from_user: TelegramUser
    message_id: int  # required
    message_thread_id: int
    photo: list[TelegramPhotoSize]
    rich_message: TelegramRichMessage
    sender_chat: TelegramChat
    sticker: TelegramStickerFile
    text: str
    video: TelegramVideo
    video_note: TelegramVideoNoteFile
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
    # Telegram API sends "from" but that is a Python reserved word (see TelegramMessage).
    from_user: TelegramUser  # required
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
