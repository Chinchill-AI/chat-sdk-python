"""Low-level Twilio voice helpers.

Python port of upstream ``adapter-twilio/src/voice/index.ts``. Voice calls
are intentionally NOT routed through the SMS/MMS adapter: apps that own a
Twilio voice route use these helpers (with
:mod:`chat_sdk.adapters.twilio.webhook` for signature verification) to
parse call / transcription webhooks and build TwiML responses.

TwiML responses are framework-agnostic dicts (``body`` / ``status`` /
``headers``), matching the response shape the adapters return from
``handle_webhook``.

Custom voice routes should verify the Twilio signature and apply their own
caller allow-list before returning TwiML.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from chat_sdk.adapters.twilio.types import TwilioFormParams, TwilioParamsInput
from chat_sdk.adapters.twilio.utils import as_param_pairs, coalesce, first_param

# =============================================================================
# Payloads
# =============================================================================


@dataclass
class TwilioVoiceCallPayload:
    """An inbound voice-call webhook (``From``/``Caller`` + ``CallSid``)."""

    from_: str
    raw: TwilioFormParams
    account_sid: str | None = None
    call_sid: str | None = None
    to: str | None = None


@dataclass
class TwilioVoiceTranscriptionPayload:
    """A final transcription result.

    Unifies the three Twilio shapes: ``<Gather input="speech">`` action
    callbacks (``SpeechResult``), recording transcription callbacks
    (``TranscriptionText``), and real-time Transcription events
    (``TranscriptionData`` JSON).
    """

    text: str
    raw: TwilioFormParams
    account_sid: str | None = None
    call_sid: str | None = None
    confidence: float | None = None
    final: bool | None = None
    from_: str | None = None
    sequence_id: str | None = None
    timestamp: str | None = None
    to: str | None = None
    track: str | None = None
    transcription_event: str | None = None
    transcription_sid: str | None = None


@dataclass
class TwilioGatherSpeechResponseOptions:
    """Options for :func:`gather_speech_twilio_response`."""

    action_url: str
    prompt: str
    action_on_empty_result: bool | None = None
    hints: Sequence[str] | str | None = None
    language: str | None = None
    method: Literal["GET", "POST"] | None = None
    profanity_filter: bool | None = None
    speech_model: str | None = None
    speech_timeout: str | None = None
    timeout_seconds: int | None = None
    voice: str | None = None


# =============================================================================
# Parsing
# =============================================================================


def parse_twilio_voice_call(params: TwilioParamsInput) -> TwilioVoiceCallPayload | None:
    """Parse an inbound voice-call webhook; ``None`` when not a call."""
    pairs = as_param_pairs(params)
    from_ = coalesce(first_param(pairs, "From"), first_param(pairs, "Caller"))
    if from_ is None:
        return None
    return TwilioVoiceCallPayload(
        account_sid=first_param(pairs, "AccountSid"),
        call_sid=first_param(pairs, "CallSid"),
        from_=from_,
        raw=pairs,
        to=coalesce(first_param(pairs, "To"), first_param(pairs, "Called")),
    )


def parse_twilio_voice_transcription(
    params: TwilioParamsInput,
) -> TwilioVoiceTranscriptionPayload | None:
    """Parse a transcription webhook; ``None`` for partial/empty results."""
    pairs = as_param_pairs(params)
    data = _parse_transcription_data(first_param(pairs, "TranscriptionData"))
    final = _parse_boolean(first_param(pairs, "Final"))
    if final is False:
        return None
    text_value = coalesce(
        coalesce(first_param(pairs, "SpeechResult"), first_param(pairs, "TranscriptionText")),
        data.get("transcript") if data is not None else None,
    )
    text = text_value if text_value is not None else ""
    if len(text.strip()) == 0:
        return None
    confidence_raw = coalesce(
        first_param(pairs, "Confidence"),
        data.get("confidence") if data is not None else None,
    )
    return TwilioVoiceTranscriptionPayload(
        account_sid=first_param(pairs, "AccountSid"),
        call_sid=first_param(pairs, "CallSid"),
        confidence=_parse_number(confidence_raw),
        final=final,
        from_=coalesce(first_param(pairs, "From"), first_param(pairs, "Caller")),
        raw=pairs,
        sequence_id=first_param(pairs, "SequenceId"),
        text=text,
        timestamp=first_param(pairs, "Timestamp"),
        to=coalesce(first_param(pairs, "To"), first_param(pairs, "Called")),
        track=first_param(pairs, "Track"),
        transcription_event=first_param(pairs, "TranscriptionEvent"),
        transcription_sid=first_param(pairs, "TranscriptionSid"),
    )


# =============================================================================
# TwiML responses
# =============================================================================


def empty_twilio_response() -> dict[str, Any]:
    """An empty ``<Response></Response>`` TwiML response."""
    return twilio_response("<Response></Response>")


def say_twilio_response(message: str) -> dict[str, Any]:
    """A single ``<Say>`` TwiML response with the message XML-escaped."""
    return twilio_response(f"<Response><Say>{escape_xml(message)}</Say></Response>")


def gather_speech_twilio_response(
    options: TwilioGatherSpeechResponseOptions,
) -> dict[str, Any]:
    """A ``<Gather input="speech">`` TwiML response wrapping a ``<Say>``.

    Attribute order and defaults mirror upstream exactly (``method``
    defaults to POST, ``actionOnEmptyResult`` to true; optional
    attributes are omitted when unset).
    """
    if isinstance(options.hints, str):
        hints: str | None = options.hints
    elif options.hints is not None:
        hints = ",".join(options.hints)
    else:
        hints = None
    method = options.method if options.method is not None else "POST"
    action_on_empty = "false" if options.action_on_empty_result is False else "true"
    language = options.language
    speech_model = options.speech_model
    speech_timeout = options.speech_timeout
    voice = options.voice
    attributes = [
        'input="speech"',
        f'action="{escape_xml(options.action_url)}"',
        f'method="{method}"',
        f'actionOnEmptyResult="{action_on_empty}"',
        f'language="{escape_xml(language)}"' if language else None,
        f'speechModel="{escape_xml(speech_model)}"' if speech_model else None,
        None if options.timeout_seconds is None else f'timeout="{options.timeout_seconds}"',
        f'speechTimeout="{escape_xml(speech_timeout)}"' if speech_timeout else None,
        f'hints="{escape_xml(hints)}"' if hints else None,
        None
        if options.profanity_filter is None
        else f'profanityFilter="{"true" if options.profanity_filter else "false"}"',
    ]
    gather_attributes = " ".join(attribute for attribute in attributes if attribute is not None)
    say_attribute_parts = [
        f'voice="{escape_xml(voice)}"' if voice else None,
        f'language="{escape_xml(language)}"' if language else None,
    ]
    say_attributes = " ".join(attribute for attribute in say_attribute_parts if attribute is not None)
    say_open = f"<Say {say_attributes}>" if say_attributes else "<Say>"
    return twilio_response(
        f"<Response><Gather {gather_attributes}>{say_open}{escape_xml(options.prompt)}</Say></Gather></Response>"
    )


def twilio_response(twiml: str) -> dict[str, Any]:
    """Wrap a TwiML string in a framework-agnostic 200 response dict."""
    return {
        "body": twiml,
        "status": 200,
        "headers": {"content-type": "text/xml;charset=UTF-8"},
    }


def escape_xml(value: str) -> str:
    """Escape XML special characters for TwiML content and attributes."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


# =============================================================================
# Internal parse helpers
# =============================================================================


def _parse_transcription_data(data: str | None) -> dict[str, str | None] | None:
    """Decode the ``TranscriptionData`` JSON blob (real-time events)."""
    if data is None:
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        # JSON scalars/arrays carry neither transcript nor confidence;
        # upstream's property reads would yield undefined for both.
        return {"confidence": None, "transcript": None}
    confidence = parsed.get("confidence")
    transcript = parsed.get("transcript")
    if isinstance(confidence, bool):
        confidence_str: str | None = None  # JS typeof true is "boolean", not number
    elif isinstance(confidence, (int, float)):
        confidence_str = _js_number_string(confidence)
    elif isinstance(confidence, str):
        confidence_str = confidence
    else:
        confidence_str = None
    return {
        "confidence": confidence_str,
        "transcript": transcript if isinstance(transcript, str) else None,
    }


def _js_number_string(value: int | float) -> str:
    """``String(number)`` semantics: integral floats drop the ``.0``."""
    if isinstance(value, float) and value.is_integer() and math.isfinite(value):
        return str(int(value))
    return str(value)


def _parse_boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
