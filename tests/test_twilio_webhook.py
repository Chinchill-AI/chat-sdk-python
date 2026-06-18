"""Port of adapter-twilio/src/webhook/index.test.ts -- verification + parsing.

Covers the X-Twilio-Signature scheme (documented Twilio example vector,
sorted/deduplicated base string, GET vs POST signing), the read pipeline
(verified POST/GET webhooks, custom verifier precedence), and webhook body
classification (text / MMS media / status / unsupported).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import pytest

from chat_sdk.adapters.twilio.types import (
    ENV_AUTH_TOKEN,
    TwilioStatusPayload,
    TwilioTextPayload,
    TwilioUnsupportedPayload,
    TwilioWebhookVerificationError,
)
from chat_sdk.adapters.twilio.webhook import (
    parse_twilio_webhook_body,
    read_twilio_webhook,
    sign_twilio_request,
    twilio_signature_base,
    verify_twilio_request,
)
from chat_sdk.shared.errors import AuthenticationError

WEBHOOK_URL = "https://example.com/twilio"


@dataclass
class _FakeRequest:
    """Minimal request-like object (``url``/``method``/``headers``/``text``)."""

    url: str = WEBHOOK_URL
    method: str = "POST"
    _body: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    async def text(self) -> str:
        return self._body


def _signed_post(fields: dict[str, str], *, auth_token: str = "token", url: str = WEBHOOK_URL) -> _FakeRequest:
    body = urlencode(fields)
    signature = sign_twilio_request(auth_token=auth_token, params=fields, url=url)
    return _FakeRequest(
        url=url,
        method="POST",
        _body=body,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": signature,
        },
    )


# ---------------------------------------------------------------------------
# Signature computation
# ---------------------------------------------------------------------------


class TestTwilioSignature:
    """Tests for sign_twilio_request / twilio_signature_base."""

    def test_matches_twilios_documented_form_signature_example(self):
        signature = sign_twilio_request(
            auth_token="12345",
            params={
                "CallSid": "CA1234567890ABCDE",
                "Caller": "+12349013030",
                "Digits": "1234",
                "From": "+12349013030",
                "To": "+18005551212",
            },
            url="https://mycompany.com/myapp",
        )
        assert signature == "3KI2uRuYyAdhZIJXcpU0izDUzWI="

    def test_builds_a_form_post_signature_base_with_sorted_parameters(self):
        base = twilio_signature_base(
            WEBHOOK_URL,
            [("To", "+15550000002"), ("From", "+15550000001"), ("Body", "hello")],
        )
        assert base == "https://example.com/twilioBodyhelloFrom+15550000001To+15550000002"

    def test_sorts_duplicate_form_parameters_like_twilio_node(self):
        base = twilio_signature_base(
            WEBHOOK_URL,
            [
                ("MediaUrl", "https://example.com/b.jpg"),
                ("MediaUrl", "https://example.com/a.jpg"),
            ],
        )
        assert base == ("https://example.com/twilioMediaUrlhttps://example.com/a.jpgMediaUrlhttps://example.com/b.jpg")

    def test_deduplicates_identical_duplicate_values(self):
        # Upstream groups values into a Set, so an exact duplicate pair
        # contributes once. Pinned: changing this breaks signature parity.
        base = twilio_signature_base(WEBHOOK_URL, [("A", "1"), ("A", "1")])
        assert base == "https://example.com/twilioA1"

    def test_signs_url_only_when_params_are_none(self):
        assert twilio_signature_base(f"{WEBHOOK_URL}?A=1") == f"{WEBHOOK_URL}?A=1"


# ---------------------------------------------------------------------------
# Verification + read pipeline
# ---------------------------------------------------------------------------


class TestReadTwilioWebhook:
    """Tests for read_twilio_webhook / verify_twilio_request."""

    @pytest.mark.asyncio
    async def test_reads_verified_post_form_webhooks(self):
        request = _signed_post(
            {
                "Body": "hello",
                "From": "+15550000001",
                "MessageSid": "SM123",
                "NumMedia": "0",
                "To": "+15550000002",
            }
        )

        payload = await read_twilio_webhook(request, auth_token="token")

        assert isinstance(payload, TwilioTextPayload)
        assert payload.body == "hello"
        assert payload.from_ == "+15550000001"
        assert payload.message_sid == "SM123"
        assert payload.to == "+15550000002"

    @pytest.mark.asyncio
    async def test_reads_verified_get_webhooks(self):
        url = f"{WEBHOOK_URL}?Body=hello&From=%2B15550000001&To=%2B15550000002"
        signature = sign_twilio_request(auth_token="token", params=None, url=url)
        request = _FakeRequest(url=url, method="GET", headers={"x-twilio-signature": signature})

        payload = await read_twilio_webhook(request, auth_token="token")

        assert isinstance(payload, TwilioTextPayload)
        assert payload.body == "hello"
        assert payload.from_ == "+15550000001"
        assert payload.to == "+15550000002"

    @pytest.mark.asyncio
    async def test_rejects_invalid_signatures(self):
        request = _FakeRequest(
            _body="Body=hello",
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-twilio-signature": "invalid",
            },
        )
        with pytest.raises(TwilioWebhookVerificationError, match="signature is invalid"):
            await read_twilio_webhook(request, auth_token="token")

    @pytest.mark.asyncio
    async def test_rejects_requests_without_a_signature_header(self):
        request = _FakeRequest(_body="Body=hello")
        with pytest.raises(TwilioWebhookVerificationError, match="signature header is required"):
            await read_twilio_webhook(request, auth_token="token")

    @pytest.mark.asyncio
    async def test_signature_header_lookup_is_case_insensitive(self):
        request = _signed_post({"Body": "hello", "From": "+1", "To": "+2"})
        request.headers["X-Twilio-Signature"] = request.headers.pop("x-twilio-signature")

        payload = await read_twilio_webhook(request, auth_token="token")

        assert isinstance(payload, TwilioTextPayload)

    @pytest.mark.asyncio
    async def test_verifies_against_an_explicit_webhook_url(self):
        # Twilio signs the public URL; behind a proxy the request URL
        # differs, so an explicit webhook_url must drive verification.
        public_url = "https://public.example/twilio"
        fields = {"Body": "hello", "From": "+1", "To": "+2"}
        request = _signed_post(fields, url=public_url)
        request.url = "http://internal:8080/twilio"

        payload = await read_twilio_webhook(request, auth_token="token", webhook_url=public_url)
        assert isinstance(payload, TwilioTextPayload)

        with pytest.raises(TwilioWebhookVerificationError):
            await read_twilio_webhook(request, auth_token="token")

    @pytest.mark.asyncio
    async def test_resolves_a_callable_webhook_url(self):
        public_url = "https://public.example/twilio"
        request = _signed_post({"Body": "hi", "From": "+1", "To": "+2"}, url=public_url)
        request.url = "http://internal:8080/twilio"

        async def resolver(req: Any) -> str:
            assert req is request
            return public_url

        payload = await read_twilio_webhook(request, auth_token="token", webhook_url=resolver)
        assert isinstance(payload, TwilioTextPayload)

    @pytest.mark.asyncio
    async def test_falls_back_to_the_env_auth_token(self):
        saved = os.environ.get(ENV_AUTH_TOKEN)
        os.environ[ENV_AUTH_TOKEN] = "env-token"
        try:
            request = _signed_post({"Body": "hello", "From": "+1", "To": "+2"}, auth_token="env-token")
            payload = await read_twilio_webhook(request)
            assert isinstance(payload, TwilioTextPayload)
        finally:
            if saved is None:
                os.environ.pop(ENV_AUTH_TOKEN, None)
            else:
                os.environ[ENV_AUTH_TOKEN] = saved

    @pytest.mark.asyncio
    async def test_missing_auth_token_raises_authentication_error(self):
        saved = os.environ.pop(ENV_AUTH_TOKEN, None)
        try:
            request = _FakeRequest(_body="Body=x", headers={"x-twilio-signature": "sig"})
            with pytest.raises(AuthenticationError, match="TWILIO_AUTH_TOKEN is required"):
                await read_twilio_webhook(request)
        finally:
            if saved is not None:
                os.environ[ENV_AUTH_TOKEN] = saved


class TestWebhookVerifier:
    """Custom webhook_verifier contract (SECURITY surface)."""

    @pytest.mark.asyncio
    async def test_verifier_replaces_signature_verification(self):
        # No signature header, no auth token: a passing verifier is enough.
        request = _FakeRequest(_body="Body=hello&From=%2B1&To=%2B2")
        seen: list[tuple[Any, str]] = []

        def verifier(req: Any, body: str) -> bool:
            seen.append((req, body))
            return True

        payload = await read_twilio_webhook(request, webhook_verifier=verifier)

        assert isinstance(payload, TwilioTextPayload)
        assert seen == [(request, "Body=hello&From=%2B1&To=%2B2")]

    @pytest.mark.asyncio
    async def test_falsy_verifier_result_rejects_the_request(self):
        request = _FakeRequest(_body="Body=hello")

        async def verifier(req: Any, body: str) -> bool:
            return False

        with pytest.raises(TwilioWebhookVerificationError, match="verifier rejected"):
            await read_twilio_webhook(request, webhook_verifier=verifier)

    @pytest.mark.asyncio
    async def test_string_verifier_result_substitutes_the_body(self):
        request = _FakeRequest(_body="Body=original&From=%2B1&To=%2B2")

        def verifier(req: Any, body: str) -> str:
            return "Body=replaced&From=%2B9&To=%2B8"

        verified = await verify_twilio_request(request, webhook_verifier=verifier)

        assert verified.body == "Body=replaced&From=%2B9&To=%2B8"
        payload = parse_twilio_webhook_body(verified.params)
        assert isinstance(payload, TwilioTextPayload)
        assert payload.body == "replaced"
        assert payload.from_ == "+9"


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


class TestParseTwilioWebhookBody:
    """Tests for parse_twilio_webhook_body."""

    def test_parses_mms_media_parameters(self):
        payload = parse_twilio_webhook_body(
            {
                "Body": "photo",
                "From": "+15550000001",
                "MediaContentType0": "image/jpeg",
                "MediaUrl0": "https://api.twilio.com/media/one",
                "MessageSid": "SM123",
                "NumMedia": "1",
                "To": "+15550000002",
            }
        )

        assert isinstance(payload, TwilioTextPayload)
        assert len(payload.media) == 1
        assert payload.media[0].content_type == "image/jpeg"
        assert payload.media[0].url == "https://api.twilio.com/media/one"

    def test_parses_status_callbacks_separately(self):
        payload = parse_twilio_webhook_body(
            {
                "From": "+15550000002",
                "MessageSid": "SM123",
                "MessageStatus": "delivered",
                "To": "+15550000001",
            }
        )

        assert isinstance(payload, TwilioStatusPayload)
        assert payload.message_status == "delivered"
        assert payload.message_sid == "SM123"

    def test_classifies_unrecognized_webhooks_as_unsupported(self):
        payload = parse_twilio_webhook_body({"CallSid": "CA123", "CallStatus": "ringing"})
        assert isinstance(payload, TwilioUnsupportedPayload)

    def test_parses_media_only_messages_with_no_body(self):
        # Body absent but NumMedia > 0 still classifies as text with body "".
        payload = parse_twilio_webhook_body(
            {
                "From": "+15550000001",
                "MediaUrl0": "https://api.twilio.com/media/one",
                "NumMedia": "1",
                "To": "+15550000002",
            }
        )
        assert isinstance(payload, TwilioTextPayload)
        assert payload.body == ""
        assert [media.url for media in payload.media] == ["https://api.twilio.com/media/one"]
        assert payload.media[0].content_type is None

    def test_skips_media_indexes_without_urls(self):
        payload = parse_twilio_webhook_body(
            {
                "Body": "x",
                "From": "+1",
                "MediaUrl1": "https://api.twilio.com/media/two",
                "NumMedia": "2",
                "To": "+2",
            }
        )
        assert isinstance(payload, TwilioTextPayload)
        assert [media.url for media in payload.media] == ["https://api.twilio.com/media/two"]

    def test_falls_back_to_sms_prefixed_fields(self):
        # SmsMessageSid / SmsStatus are the legacy aliases Twilio still sends.
        status = parse_twilio_webhook_body({"SmsStatus": "sent", "MessageSid": "SM1"})
        assert isinstance(status, TwilioStatusPayload)
        assert status.message_status == "sent"

        text = parse_twilio_webhook_body({"Body": "hi", "From": "+1", "SmsMessageSid": "SM2", "To": "+2"})
        assert isinstance(text, TwilioTextPayload)
        assert text.message_sid == "SM2"
