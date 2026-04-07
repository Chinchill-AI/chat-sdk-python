"""Emoji management for chat-sdk."""

from __future__ import annotations

import re

from chat_sdk.types import EmojiFormats, EmojiValue

# Internal emoji registry for singleton instances
_emoji_registry: dict[str, EmojiValue] = {}


def get_emoji(name: str) -> EmojiValue:
    """Get or create an immutable singleton EmojiValue.

    Always returns the same object for the same name,
    enabling ``is`` comparison for emoji identity.
    """
    if name not in _emoji_registry:
        _emoji_registry[name] = EmojiValue(name=name)
    return _emoji_registry[name]


# Default emoji map for well-known emoji
DEFAULT_EMOJI_MAP: dict[str, EmojiFormats] = {
    # Reactions & Gestures
    "thumbs_up": EmojiFormats(slack=["+1", "thumbsup"], gchat="👍"),
    "thumbs_down": EmojiFormats(slack=["-1", "thumbsdown"], gchat="👎"),
    "clap": EmojiFormats(slack="clap", gchat="👏"),
    "wave": EmojiFormats(slack="wave", gchat="👋"),
    "pray": EmojiFormats(slack="pray", gchat="🙏"),
    "muscle": EmojiFormats(slack="muscle", gchat="💪"),
    "ok_hand": EmojiFormats(slack="ok_hand", gchat="👌"),
    "point_up": EmojiFormats(slack="point_up", gchat="👆"),
    "point_down": EmojiFormats(slack="point_down", gchat="👇"),
    "point_left": EmojiFormats(slack="point_left", gchat="👈"),
    "point_right": EmojiFormats(slack="point_right", gchat="👉"),
    "raised_hands": EmojiFormats(slack="raised_hands", gchat="🙌"),
    "shrug": EmojiFormats(slack="shrug", gchat="🤷"),
    "facepalm": EmojiFormats(slack="facepalm", gchat="🤦"),
    # Emotions & Faces
    "heart": EmojiFormats(slack="heart", gchat=["❤️", "❤"]),
    "smile": EmojiFormats(slack=["smile", "slightly_smiling_face"], gchat="😊"),
    "laugh": EmojiFormats(slack=["laughing", "satisfied", "joy"], gchat=["😂", "😆"]),
    "thinking": EmojiFormats(slack="thinking_face", gchat="🤔"),
    "sad": EmojiFormats(slack=["cry", "sad", "white_frowning_face"], gchat="😢"),
    "cry": EmojiFormats(slack="sob", gchat="😭"),
    "angry": EmojiFormats(slack="angry", gchat="😠"),
    "love_eyes": EmojiFormats(slack="heart_eyes", gchat="😍"),
    "cool": EmojiFormats(slack="sunglasses", gchat="😎"),
    "wink": EmojiFormats(slack="wink", gchat="😉"),
    "surprised": EmojiFormats(slack="open_mouth", gchat="😮"),
    "worried": EmojiFormats(slack="worried", gchat="😟"),
    "confused": EmojiFormats(slack="confused", gchat="😕"),
    "neutral": EmojiFormats(slack="neutral_face", gchat="😐"),
    "sleeping": EmojiFormats(slack="sleeping", gchat="😴"),
    "sick": EmojiFormats(slack="nauseated_face", gchat="🤢"),
    "mind_blown": EmojiFormats(slack="exploding_head", gchat="🤯"),
    "relieved": EmojiFormats(slack="relieved", gchat="😌"),
    "grimace": EmojiFormats(slack="grimacing", gchat="😬"),
    "rolling_eyes": EmojiFormats(slack="rolling_eyes", gchat="🙄"),
    "hug": EmojiFormats(slack="hugging_face", gchat="🤗"),
    "zany": EmojiFormats(slack="zany_face", gchat="🤪"),
    # Status & Symbols
    "check": EmojiFormats(slack=["white_check_mark", "heavy_check_mark"], gchat=["✅", "✔️"]),
    "x": EmojiFormats(slack=["x", "heavy_multiplication_x"], gchat=["❌", "✖️"]),
    "question": EmojiFormats(slack="question", gchat=["❓", "?"]),
    "exclamation": EmojiFormats(slack="exclamation", gchat="❗"),
    "warning": EmojiFormats(slack="warning", gchat="⚠️"),
    "stop": EmojiFormats(slack="octagonal_sign", gchat="🛑"),
    "info": EmojiFormats(slack="information_source", gchat="ℹ️"),
    "100": EmojiFormats(slack="100", gchat="💯"),
    "no_entry": EmojiFormats(slack="no_entry_sign", gchat="🚫"),
    "eyes": EmojiFormats(slack="eyes", gchat="👀"),
    "boom": EmojiFormats(slack="boom", gchat="💥"),
    # Status Indicators
    "green_circle": EmojiFormats(slack="large_green_circle", gchat="🟢"),
    "yellow_circle": EmojiFormats(slack="large_yellow_circle", gchat="🟡"),
    "red_circle": EmojiFormats(slack="red_circle", gchat="🔴"),
    "blue_circle": EmojiFormats(slack="large_blue_circle", gchat="🔵"),
    "white_circle": EmojiFormats(slack="white_circle", gchat="⚪"),
    "black_circle": EmojiFormats(slack="black_circle", gchat="⚫"),
    # Objects & Tools
    "rocket": EmojiFormats(slack="rocket", gchat="🚀"),
    "party": EmojiFormats(slack=["tada", "partying_face"], gchat=["🎉", "🥳"]),
    "confetti": EmojiFormats(slack="confetti_ball", gchat="🎊"),
    "balloon": EmojiFormats(slack="balloon", gchat="🎈"),
    "gift": EmojiFormats(slack="gift", gchat="🎁"),
    "trophy": EmojiFormats(slack="trophy", gchat="🏆"),
    "medal": EmojiFormats(slack="first_place_medal", gchat="🥇"),
    "star": EmojiFormats(slack="star", gchat="⭐"),
    "sparkles": EmojiFormats(slack="sparkles", gchat="✨"),
    "fire": EmojiFormats(slack="fire", gchat="🔥"),
    "lightning": EmojiFormats(slack="zap", gchat="⚡"),
    "bulb": EmojiFormats(slack="bulb", gchat="💡"),
    "lightbulb": EmojiFormats(slack="bulb", gchat="💡"),
    "gear": EmojiFormats(slack="gear", gchat="⚙️"),
    "wrench": EmojiFormats(slack="wrench", gchat="🔧"),
    "hammer": EmojiFormats(slack="hammer", gchat="🔨"),
    "bug": EmojiFormats(slack="bug", gchat="🐛"),
    "link": EmojiFormats(slack="link", gchat="🔗"),
    "lock": EmojiFormats(slack="lock", gchat="🔒"),
    "unlock": EmojiFormats(slack="unlock", gchat="🔓"),
    "key": EmojiFormats(slack="key", gchat="🔑"),
    "pin": EmojiFormats(slack="pushpin", gchat="📌"),
    "bell": EmojiFormats(slack="bell", gchat="🔔"),
    "megaphone": EmojiFormats(slack="mega", gchat="📢"),
    "loudspeaker": EmojiFormats(slack="loudspeaker", gchat="📢"),
    "speech_bubble": EmojiFormats(slack="speech_balloon", gchat="💬"),
    "clipboard": EmojiFormats(slack="clipboard", gchat="📋"),
    "memo": EmojiFormats(slack="memo", gchat="📝"),
    "book": EmojiFormats(slack="book", gchat="📖"),
    "calendar": EmojiFormats(slack="calendar", gchat="📅"),
    "clock": EmojiFormats(slack="clock1", gchat="🕐"),
    "hourglass": EmojiFormats(slack="hourglass", gchat="⏳"),
    "mag": EmojiFormats(slack="mag", gchat="🔍"),
    "chart": EmojiFormats(slack="chart_with_upwards_trend", gchat="📈"),
    "chart_up": EmojiFormats(slack="chart_with_upwards_trend", gchat="📈"),
    "chart_down": EmojiFormats(slack="chart_with_downwards_trend", gchat="📉"),
    "bar_chart": EmojiFormats(slack="bar_chart", gchat="📊"),
    "folder": EmojiFormats(slack="file_folder", gchat="📁"),
    "file": EmojiFormats(slack="page_facing_up", gchat="📄"),
    "package": EmojiFormats(slack="package", gchat="📦"),
    "email": EmojiFormats(slack="email", gchat="📧"),
    "inbox": EmojiFormats(slack="inbox_tray", gchat="📥"),
    "outbox": EmojiFormats(slack="outbox_tray", gchat="📤"),
    # Food & Drink
    "coffee": EmojiFormats(slack="coffee", gchat="☕"),
    "pizza": EmojiFormats(slack="pizza", gchat="🍕"),
    "beer": EmojiFormats(slack="beer", gchat="🍺"),
    # Arrows & Directions
    "arrow_up": EmojiFormats(slack="arrow_up", gchat="⬆️"),
    "arrow_down": EmojiFormats(slack="arrow_down", gchat="⬇️"),
    "arrow_left": EmojiFormats(slack="arrow_left", gchat="⬅️"),
    "arrow_right": EmojiFormats(slack="arrow_right", gchat="➡️"),
    "arrow_up_right": EmojiFormats(slack="arrow_upper_right", gchat="↗️"),
    "arrow_down_right": EmojiFormats(slack="arrow_lower_right", gchat="↘️"),
    "arrow_right_hook": EmojiFormats(slack="arrow_right_hook", gchat="↪️"),
    "arrows_counterclockwise": EmojiFormats(slack="arrows_counterclockwise", gchat="🔄"),
    "refresh": EmojiFormats(slack="arrows_counterclockwise", gchat="🔄"),
    # Nature & Weather
    "sun": EmojiFormats(slack="sunny", gchat="☀️"),
    "cloud": EmojiFormats(slack="cloud", gchat="☁️"),
    "rain": EmojiFormats(slack="rain_cloud", gchat="🌧️"),
    "snow": EmojiFormats(slack="snowflake", gchat="❄️"),
    "rainbow": EmojiFormats(slack="rainbow", gchat="🌈"),
}

# =============================================================================
# EmojiResolver class
# =============================================================================

# Teams reaction type → normalized emoji name
_TEAMS_MAP: dict[str, str] = {
    "like": "thumbs_up",
    "heart": "heart",
    "laugh": "laugh",
    "surprised": "surprised",
    "sad": "sad",
    "angry": "angry",
}


class EmojiResolver:
    """Emoji resolver that handles conversion between platform formats and normalized names."""

    def __init__(self, custom_map: dict[str, EmojiFormats] | None = None) -> None:
        self._emoji_map: dict[str, EmojiFormats] = {**DEFAULT_EMOJI_MAP}
        if custom_map:
            self._emoji_map.update(custom_map)
        self._slack_to_normalized: dict[str, str] = {}
        self._gchat_to_normalized: dict[str, str] = {}
        self._build_reverse_maps()

    def _build_reverse_maps(self) -> None:
        self._slack_to_normalized.clear()
        self._gchat_to_normalized.clear()
        for normalized, formats in self._emoji_map.items():
            # Slack reverse map
            slack_vals = formats.slack if isinstance(formats.slack, list) else [formats.slack]
            for s in slack_vals:
                if s:
                    self._slack_to_normalized[s.lower()] = normalized

            # GChat reverse map
            gchat_vals = formats.gchat if isinstance(formats.gchat, list) else [formats.gchat]
            for g in gchat_vals:
                if g:
                    self._gchat_to_normalized[g] = normalized

    def from_slack(self, slack_emoji: str) -> EmojiValue:
        """Convert a Slack emoji name to normalized EmojiValue.

        Returns an EmojiValue for the raw emoji if no mapping exists.
        """
        cleaned = slack_emoji.lower()
        # Strip at most one colon from each end (avoid stripping interior colons)
        if cleaned.startswith(":"):
            cleaned = cleaned[1:]
        if cleaned.endswith(":"):
            cleaned = cleaned[:-1]
        normalized = self._slack_to_normalized.get(cleaned, slack_emoji)
        return get_emoji(normalized)

    def from_gchat(self, gchat_emoji: str) -> EmojiValue:
        """Convert a Google Chat unicode emoji to normalized EmojiValue.

        Returns an EmojiValue for the raw emoji if no mapping exists.
        """
        normalized = self._gchat_to_normalized.get(gchat_emoji, gchat_emoji)
        return get_emoji(normalized)

    def from_teams(self, teams_reaction: str) -> EmojiValue:
        """Convert a Teams reaction type to normalized EmojiValue.

        Teams uses specific names: like, heart, laugh, surprised, sad, angry.
        Returns an EmojiValue for the raw reaction if no mapping exists.
        """
        normalized = _TEAMS_MAP.get(teams_reaction, teams_reaction)
        return get_emoji(normalized)

    def to_slack(self, emoji: EmojiValue | str) -> str:
        """Convert a normalized emoji (or EmojiValue) to Slack format.

        Returns the first Slack format if multiple exist.
        """
        name = emoji.name if isinstance(emoji, EmojiValue) else emoji
        formats = self._emoji_map.get(name)
        if formats is None:
            return name
        slack_val = formats.slack
        return slack_val[0] if isinstance(slack_val, list) else slack_val

    def to_gchat(self, emoji: EmojiValue | str) -> str:
        """Convert a normalized emoji (or EmojiValue) to Google Chat format.

        Returns the first GChat format if multiple exist.
        """
        name = emoji.name if isinstance(emoji, EmojiValue) else emoji
        formats = self._emoji_map.get(name)
        if formats is None:
            return name
        gchat_val = formats.gchat
        return gchat_val[0] if isinstance(gchat_val, list) else gchat_val

    def to_discord(self, emoji: EmojiValue | str) -> str:
        """Convert a normalized emoji (or EmojiValue) to Discord format (unicode).

        Discord uses unicode emoji, same as Google Chat.
        """
        return self.to_gchat(emoji)

    def matches(self, raw_emoji: str, normalized: EmojiValue | str) -> bool:
        """Check if an emoji (in any format) matches a normalized emoji name or EmojiValue."""
        name = normalized.name if isinstance(normalized, EmojiValue) else normalized
        formats = self._emoji_map.get(name)
        if formats is None:
            return raw_emoji == name

        slack_vals = formats.slack if isinstance(formats.slack, list) else [formats.slack]
        gchat_vals = formats.gchat if isinstance(formats.gchat, list) else [formats.gchat]

        cleaned_raw = raw_emoji.strip(":").lower()

        return any(s.lower() == cleaned_raw for s in slack_vals) or raw_emoji in gchat_vals

    def extend(self, custom_map: dict[str, EmojiFormats]) -> None:
        """Add or override emoji mappings."""
        self._emoji_map.update(custom_map)
        self._build_reverse_maps()


# =============================================================================
# Default singleton
# =============================================================================

default_emoji_resolver = EmojiResolver()


# =============================================================================
# Module-level convenience functions (wrappers around the default resolver)
# =============================================================================


def resolve_emoji_from_slack(slack_name: str) -> EmojiValue:
    """Resolve a Slack emoji name to an EmojiValue."""
    return default_emoji_resolver.from_slack(slack_name)


def resolve_emoji_from_gchat(gchat_emoji: str) -> EmojiValue:
    """Resolve a Google Chat emoji to an EmojiValue."""
    return default_emoji_resolver.from_gchat(gchat_emoji)


def emoji_to_slack(emoji: EmojiValue | str) -> str:
    """Convert an EmojiValue or string to Slack format."""
    return default_emoji_resolver.to_slack(emoji)


def emoji_to_gchat(emoji: EmojiValue | str) -> str:
    """Convert an EmojiValue or string to Google Chat format."""
    return default_emoji_resolver.to_gchat(emoji)


def emoji_to_unicode(emoji: EmojiValue | str) -> str:
    """Convert an EmojiValue or string to unicode emoji."""
    return default_emoji_resolver.to_gchat(emoji)


_EMOJI_PLACEHOLDER_RE = re.compile(r"\{\{emoji:([a-z0-9_]+)\}\}", re.IGNORECASE)


def convert_emoji_placeholders(
    text: str,
    platform: str,
    resolver: EmojiResolver | None = None,
) -> str:
    """Convert emoji placeholders like ``{{emoji:wave}}`` to platform format.

    Parameters
    ----------
    text:
        Text containing ``{{emoji:name}}`` placeholders.
    platform:
        Target platform (``"slack"``, ``"gchat"``, ``"teams"``, ``"discord"``,
        ``"github"``, ``"linear"``, ``"whatsapp"``).
    resolver:
        Optional custom :class:`EmojiResolver`. Defaults to the module-level
        ``default_emoji_resolver``.
    """
    r = resolver or default_emoji_resolver

    def replacer(match: re.Match[str]) -> str:
        emoji_name = match.group(1)
        if platform == "slack":
            return f":{r.to_slack(emoji_name)}:"
        # All other platforms use unicode
        return r.to_gchat(emoji_name)

    return _EMOJI_PLACEHOLDER_RE.sub(replacer, text)


class _EmojiProxy:
    """Attribute-style emoji access: ``emoji.thumbs_up``, ``emoji.fire``, etc."""

    def __getattr__(self, name: str) -> EmojiValue:
        return get_emoji(name)


emoji = _EmojiProxy()
