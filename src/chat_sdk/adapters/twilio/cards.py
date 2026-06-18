"""Card rendering for the Twilio adapter.

SMS is plain text, so cards collapse to the shared fallback text with the
``*bold*`` markers stripped (SMS clients render asterisks literally).
Mirrors upstream ``adapter-twilio/src/cards.ts``.
"""

from __future__ import annotations

from chat_sdk.cards import CardElement
from chat_sdk.shared.card_utils import card_to_fallback_text


def card_to_twilio_text(card: CardElement) -> str:
    """Render a card as plain SMS fallback text (no ``*`` markers)."""
    return card_to_fallback_text(card).replace("*", "")
