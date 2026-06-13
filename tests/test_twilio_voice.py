"""Port of adapter-twilio/src/voice/index.test.ts -- voice helpers.

Covers inbound call parsing, the three transcription webhook shapes
(Gather speech results, real-time TranscriptionData events, recording
transcription callbacks), TwiML response builders (exact wire strings),
and XML escaping.
"""

from __future__ import annotations

from chat_sdk.adapters.twilio.voice import (
    TwilioGatherSpeechResponseOptions,
    empty_twilio_response,
    escape_xml,
    gather_speech_twilio_response,
    parse_twilio_voice_call,
    parse_twilio_voice_transcription,
    say_twilio_response,
)


class TestParseTwilioVoiceCall:
    """Tests for parse_twilio_voice_call."""

    def test_parses_inbound_voice_call_webhooks(self):
        call = parse_twilio_voice_call(
            {
                "AccountSid": "AC123",
                "CallSid": "CA123",
                "Called": "+15550000001",
                "Caller": "+15550000002",
            }
        )

        assert call is not None
        assert call.account_sid == "AC123"
        assert call.call_sid == "CA123"
        assert call.from_ == "+15550000002"
        assert call.to == "+15550000001"

    def test_prefers_from_and_to_over_caller_and_called(self):
        call = parse_twilio_voice_call({"Called": "+2", "Caller": "+1", "From": "+15550000009", "To": "+15550000008"})
        assert call is not None
        assert call.from_ == "+15550000009"
        assert call.to == "+15550000008"

    def test_returns_none_without_a_caller(self):
        assert parse_twilio_voice_call({"CallSid": "CA123"}) is None


class TestParseTwilioVoiceTranscription:
    """Tests for parse_twilio_voice_transcription."""

    def test_parses_gather_speech_results(self):
        transcription = parse_twilio_voice_transcription(
            {
                "CallSid": "CA123",
                "Confidence": "0.9",
                "From": "+15550000002",
                "SpeechResult": "hello there",
                "To": "+15550000001",
            }
        )

        assert transcription is not None
        assert transcription.call_sid == "CA123"
        assert transcription.confidence == 0.9
        assert transcription.from_ == "+15550000002"
        assert transcription.text == "hello there"
        assert transcription.to == "+15550000001"

    def test_parses_final_real_time_transcription_content(self):
        transcription = parse_twilio_voice_transcription(
            {
                "AccountSid": "AC123",
                "CallSid": "CA123",
                "Final": "true",
                "SequenceId": "2",
                "Timestamp": "2024-06-25T18:45:21.454203Z",
                "Track": "outbound_track",
                "TranscriptionData": '{"transcript":"hello from the call","confidence":0.9956335}',
                "TranscriptionEvent": "transcription-content",
                "TranscriptionSid": "GT123",
            }
        )

        assert transcription is not None
        assert transcription.confidence == 0.9956335
        assert transcription.final is True
        assert transcription.sequence_id == "2"
        assert transcription.text == "hello from the call"
        assert transcription.track == "outbound_track"
        assert transcription.transcription_event == "transcription-content"
        assert transcription.transcription_sid == "GT123"

    def test_ignores_partial_real_time_transcription_content(self):
        transcription = parse_twilio_voice_transcription(
            {
                "CallSid": "CA123",
                "Final": "false",
                "TranscriptionData": '{"transcript":"partial words"}',
            }
        )
        assert transcription is None

    def test_parses_recording_transcription_callbacks(self):
        transcription = parse_twilio_voice_transcription(
            {
                "CallSid": "CA123",
                "From": "+15550000002",
                "To": "+15550000001",
                "TranscriptionSid": "TR123",
                "TranscriptionText": "recording text",
            }
        )

        assert transcription is not None
        assert transcription.call_sid == "CA123"
        assert transcription.text == "recording text"
        assert transcription.transcription_sid == "TR123"

    def test_returns_none_for_whitespace_only_text(self):
        assert parse_twilio_voice_transcription({"CallSid": "CA1", "SpeechResult": "   "}) is None

    def test_ignores_non_numeric_confidence(self):
        transcription = parse_twilio_voice_transcription({"Confidence": "high", "SpeechResult": "hello"})
        assert transcription is not None
        assert transcription.confidence is None


class TestTwiMLResponses:
    """Tests for the TwiML response builders."""

    def test_renders_gather_speech_twiml(self):
        response = gather_speech_twilio_response(
            TwilioGatherSpeechResponseOptions(
                action_url="https://example.com/voice/result",
                hints=["billing", "support"],
                language="en-US",
                profanity_filter=False,
                prompt='say "hello" & continue',
                speech_model="phone_call",
                speech_timeout="auto",
                timeout_seconds=4,
                voice="Polly.Joanna-Neural",
            )
        )

        assert response["status"] == 200
        assert response["headers"]["content-type"] == "text/xml;charset=UTF-8"
        assert response["body"] == (
            '<Response><Gather input="speech" action="https://example.com/voice/result"'
            ' method="POST" actionOnEmptyResult="true" language="en-US"'
            ' speechModel="phone_call" timeout="4" speechTimeout="auto"'
            ' hints="billing,support" profanityFilter="false">'
            '<Say voice="Polly.Joanna-Neural" language="en-US">'
            "say &quot;hello&quot; &amp; continue</Say></Gather></Response>"
        )

    def test_renders_minimal_gather_twiml_with_defaults(self):
        response = gather_speech_twilio_response(
            TwilioGatherSpeechResponseOptions(
                action_url="https://example.com/voice",
                prompt="hi",
            )
        )
        assert response["body"] == (
            '<Response><Gather input="speech" action="https://example.com/voice"'
            ' method="POST" actionOnEmptyResult="true"><Say>hi</Say></Gather></Response>'
        )

    def test_action_on_empty_result_false_is_explicit(self):
        response = gather_speech_twilio_response(
            TwilioGatherSpeechResponseOptions(
                action_url="https://example.com/voice",
                prompt="hi",
                action_on_empty_result=False,
            )
        )
        assert 'actionOnEmptyResult="false"' in response["body"]

    def test_renders_simple_twiml_responses(self):
        empty = empty_twilio_response()
        assert empty["body"] == "<Response></Response>"
        assert empty["status"] == 200
        assert empty["headers"]["content-type"] == "text/xml;charset=UTF-8"

        say = say_twilio_response("hello <there>")
        assert say["body"] == "<Response><Say>hello &lt;there&gt;</Say></Response>"

    def test_escapes_xml_attributes_and_content(self):
        assert escape_xml("\"fish\" & 'chips' <ok>") == ("&quot;fish&quot; &amp; &apos;chips&apos; &lt;ok&gt;")
