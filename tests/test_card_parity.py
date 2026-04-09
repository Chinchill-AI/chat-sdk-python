"""Card rendering parity test -- build a comprehensive CardElement and render
it through ALL 8 adapter card converters.

Asserts each output is non-empty and contains the expected title/text.
This is a smoke test, not a golden-file comparison.

References issue #18 (cross-SDK parity).
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.cards import (
    Actions,
    Button,
    Card,
    CardElement,
    Divider,
    Field,
    Fields,
    Image,
    LinkButton,
    Section,
    Table,
    Text,
    card_to_fallback_text,
)

# ---------------------------------------------------------------------------
# Adapter card converter imports (guarded)
# ---------------------------------------------------------------------------

try:
    from chat_sdk.adapters.slack.cards import card_to_block_kit

    _SLACK_CARDS = True
except ImportError:
    _SLACK_CARDS = False

try:
    from chat_sdk.adapters.teams.cards import card_to_adaptive_card

    _TEAMS_CARDS = True
except ImportError:
    _TEAMS_CARDS = False

try:
    from chat_sdk.adapters.google_chat.cards import card_to_google_card

    _GCHAT_CARDS = True
except ImportError:
    _GCHAT_CARDS = False

try:
    from chat_sdk.adapters.discord.cards import card_to_discord_payload

    _DISCORD_CARDS = True
except ImportError:
    _DISCORD_CARDS = False

try:
    from chat_sdk.adapters.telegram.cards import card_to_telegram_inline_keyboard

    _TELEGRAM_CARDS = True
except ImportError:
    _TELEGRAM_CARDS = False

try:
    from chat_sdk.adapters.github.cards import card_to_github_markdown

    _GITHUB_CARDS = True
except ImportError:
    _GITHUB_CARDS = False

try:
    from chat_sdk.adapters.linear.cards import card_to_linear_markdown

    _LINEAR_CARDS = True
except ImportError:
    _LINEAR_CARDS = False

try:
    from chat_sdk.adapters.whatsapp.cards import card_to_whatsapp

    _WHATSAPP_CARDS = True
except ImportError:
    _WHATSAPP_CARDS = False


# ---------------------------------------------------------------------------
# Shared comprehensive card fixture
# ---------------------------------------------------------------------------


def _build_comprehensive_card() -> CardElement:
    """Build a card that exercises every element type."""
    return Card(
        title="System Status Report",
        subtitle="Daily infrastructure health check",
        children=[
            Text("All systems operational", style="bold"),
            Text("Last updated: 2026-04-03T12:00:00Z", style="muted"),
            Divider(),
            Fields(
                [
                    Field(label="Uptime", value="99.97%"),
                    Field(label="Region", value="us-east-1"),
                    Field(label="Incidents", value="0"),
                ]
            ),
            Divider(),
            Image(url="https://example.com/status.png", alt="Status chart"),
            Table(
                headers=["Service", "Status", "Latency"],
                rows=[
                    ["API Gateway", "Healthy", "12ms"],
                    ["Database", "Healthy", "3ms"],
                    ["Cache", "Degraded", "45ms"],
                ],
            ),
            Section(
                [
                    Text("Actions available:"),
                ]
            ),
            Actions(
                [
                    Button(id="refresh", label="Refresh Status", style="primary"),
                    Button(id="acknowledge", label="Acknowledge", style="default"),
                    Button(id="escalate", label="Escalate", style="danger"),
                    LinkButton(url="https://status.example.com", label="View Dashboard"),
                ]
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCardParitySmokeTest:
    """Render the same comprehensive CardElement through all 8 converters."""

    @pytest.fixture
    def card(self) -> CardElement:
        return _build_comprehensive_card()

    def test_shared_fallback_text(self, card: CardElement):
        """Shared card_to_fallback_text should produce non-empty output."""
        result = card_to_fallback_text(card)
        assert result, "Fallback text should be non-empty"
        assert "System Status Report" in result
        assert "All systems operational" in result

    @pytest.mark.skipif(not _SLACK_CARDS, reason="Slack cards not available")
    def test_slack_block_kit(self, card: CardElement):
        """Slack Block Kit renderer produces valid blocks."""
        blocks = card_to_block_kit(card)
        assert isinstance(blocks, list)
        assert len(blocks) > 0, "Should produce at least one block"

        # Check that the title appears in a header block
        block_types = [b.get("type") for b in blocks]
        assert "header" in block_types, "Should include a header block for the title"

        # Find the header and check content
        for block in blocks:
            if block.get("type") == "header":
                header_text = block.get("text", {}).get("text", "")
                assert "System Status Report" in header_text
                break

    @pytest.mark.skipif(not _TEAMS_CARDS, reason="Teams cards not available")
    def test_teams_adaptive_card(self, card: CardElement):
        """Teams Adaptive Card renderer produces valid JSON structure."""
        adaptive_card = card_to_adaptive_card(card)
        assert isinstance(adaptive_card, dict)
        assert adaptive_card.get("type") == "AdaptiveCard"
        assert "body" in adaptive_card
        assert len(adaptive_card["body"]) > 0

        # Verify title appears in the card body
        body_json = str(adaptive_card)
        assert "System Status Report" in body_json

    @pytest.mark.skipif(not _GCHAT_CARDS, reason="Google Chat cards not available")
    def test_gchat_google_card(self, card: CardElement):
        """Google Chat Card v2 renderer produces valid structure."""
        google_card = card_to_google_card(card)
        assert isinstance(google_card, dict)

        # Google cards have a sections array
        card_json = str(google_card)
        assert "System Status Report" in card_json
        assert len(card_json) > 50, "Google card should have substantial content"

    @pytest.mark.skipif(not _DISCORD_CARDS, reason="Discord cards not available")
    def test_discord_payload(self, card: CardElement):
        """Discord payload renderer produces embeds and components."""
        payload = card_to_discord_payload(card)
        assert isinstance(payload, dict)

        # Should have embeds
        embeds = payload.get("embeds", [])
        assert len(embeds) > 0, "Should produce at least one embed"

        # Title should appear in first embed
        first_embed = embeds[0]
        assert "System Status Report" in first_embed.get("title", "")

        # Should have button components
        components = payload.get("components", [])
        assert len(components) > 0, "Should produce action row components"

    @pytest.mark.skipif(not _TELEGRAM_CARDS, reason="Telegram cards not available")
    def test_telegram_inline_keyboard(self, card: CardElement):
        """Telegram inline keyboard renderer produces button markup."""
        keyboard = card_to_telegram_inline_keyboard(card)
        # Should produce a keyboard since the card has action buttons
        assert keyboard is not None, "Card with buttons should produce inline keyboard"
        assert "inline_keyboard" in keyboard
        buttons = keyboard["inline_keyboard"]
        assert len(buttons) > 0, "Should have at least one row of buttons"

    @pytest.mark.skipif(not _GITHUB_CARDS, reason="GitHub cards not available")
    def test_github_markdown(self, card: CardElement):
        """GitHub markdown renderer produces non-empty markdown."""
        markdown = card_to_github_markdown(card)
        assert isinstance(markdown, str)
        assert len(markdown) > 0
        assert "System Status Report" in markdown
        assert "All systems operational" in markdown

    @pytest.mark.skipif(not _LINEAR_CARDS, reason="Linear cards not available")
    def test_linear_markdown(self, card: CardElement):
        """Linear markdown renderer produces non-empty markdown."""
        markdown = card_to_linear_markdown(card)
        assert isinstance(markdown, str)
        assert len(markdown) > 0
        assert "System Status Report" in markdown

    @pytest.mark.skipif(not _WHATSAPP_CARDS, reason="WhatsApp cards not available")
    def test_whatsapp_card(self, card: CardElement):
        """WhatsApp card renderer produces non-empty output."""
        result = card_to_whatsapp(card)
        assert result is not None
        # WhatsApp returns either interactive payload or text fallback
        if isinstance(result, dict):
            assert len(str(result)) > 20
        elif isinstance(result, str):
            assert "System Status Report" in result

    def test_all_converters_agree_on_title(self, card: CardElement):
        """All available converters should include the card title in their output."""
        converters: list[tuple[str, bool, Any]] = [
            ("fallback", True, lambda c: card_to_fallback_text(c)),
        ]
        if _SLACK_CARDS:
            converters.append(("slack", True, lambda c: str(card_to_block_kit(c))))
        if _TEAMS_CARDS:
            converters.append(("teams", True, lambda c: str(card_to_adaptive_card(c))))
        if _GCHAT_CARDS:
            converters.append(("gchat", True, lambda c: str(card_to_google_card(c))))
        if _DISCORD_CARDS:
            converters.append(("discord", True, lambda c: str(card_to_discord_payload(c))))
        if _GITHUB_CARDS:
            converters.append(("github", True, lambda c: card_to_github_markdown(c)))
        if _LINEAR_CARDS:
            converters.append(("linear", True, lambda c: card_to_linear_markdown(c)))

        for name, _available, converter in converters:
            output = converter(card)
            assert "System Status Report" in output, f"{name} converter should include card title"
