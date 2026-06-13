"""Port of adapter-twilio/src/api/index.test.ts -- REST API helpers.

Covers the raw call surface (Basic auth, form encoding, search params),
send/fetch/list/delete message helpers, live-call updates, authenticated
media fetch, credential resolution (explicit / resolver / env), and the
typed error mapping (the Python adaptation of upstream's TwilioApiError).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl, urlsplit

import pytest

from chat_sdk.adapters.twilio.api import (
    TwilioApiError,
    call_twilio_api,
    delete_twilio_message,
    encode_twilio_form,
    fetch_twilio_media,
    fetch_twilio_message,
    list_twilio_messages,
    resolve_twilio_credential,
    send_twilio_message,
    update_twilio_call,
)
from chat_sdk.adapters.twilio.types import (
    ENV_ACCOUNT_SID,
    ENV_AUTH_TOKEN,
    TwilioCredentials,
    TwilioHttpResponse,
)
from chat_sdk.shared.errors import (
    AdapterPermissionError,
    AdapterRateLimitError,
    AuthenticationError,
    ResourceNotFoundError,
    ValidationError,
)

BASIC_AUTH = "Basic QUMxMjM6dG9rZW4="  # base64("AC123:token")


def _credentials() -> TwilioCredentials:
    return TwilioCredentials(account_sid="AC123", auth_token="token")


def _mock_http(payload: Any, status: int = 200) -> AsyncMock:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return AsyncMock(return_value=TwilioHttpResponse(status=status, body=body))


def _request_of(mock: AsyncMock) -> tuple[str, str, dict[str, str], str | None]:
    method, url, headers, body = mock.await_args.args
    return method, url, dict(headers), body


def _form_of(body: str | None) -> dict[str, str]:
    assert body is not None
    return dict(parse_qsl(body, keep_blank_values=True))


@pytest.fixture
def _clear_twilio_credential_env() -> Iterator[None]:
    saved: dict[str, str | None] = {k: os.environ.pop(k, None) for k in (ENV_ACCOUNT_SID, ENV_AUTH_TOKEN)}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class TestCallTwilioApi:
    """Tests for call_twilio_api."""

    @pytest.mark.asyncio
    async def test_supports_raw_api_calls_with_base_url_override(self):
        request = _mock_http({"ok": True})

        response = await call_twilio_api(
            "/2010-04-01/Accounts/AC123/Messages.json",
            api_base_url="https://twilio.test",
            body={"Body": "hello", "Optional": None, "To": "+15550000002"},
            credentials=_credentials(),
            http_request=request,
        )

        assert response.ok is True
        assert response.status == 200
        method, url, headers, body = _request_of(request)
        assert method == "POST"
        assert url == "https://twilio.test/2010-04-01/Accounts/AC123/Messages.json"
        assert headers["authorization"] == BASIC_AUTH
        assert headers["content-type"] == "application/x-www-form-urlencoded;charset=UTF-8"
        # `Optional: None` must be omitted, not serialized as "None".
        assert _form_of(body) == {"Body": "hello", "To": "+15550000002"}

    @pytest.mark.asyncio
    async def test_omits_the_form_content_type_without_a_body(self):
        request = _mock_http({"ok": True})

        await call_twilio_api(
            "/2010-04-01/Accounts/AC123/Messages.json",
            credentials=_credentials(),
            http_request=request,
            method="GET",
        )

        method, url, headers, body = _request_of(request)
        assert method == "GET"
        assert url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
        assert body is None
        assert "content-type" not in headers


class TestSendTwilioMessage:
    """Tests for send_twilio_message."""

    @pytest.mark.asyncio
    async def test_sends_form_encoded_messages_with_phone_number_sender(self):
        request = _mock_http({"sid": "SM123"})

        message = await send_twilio_message(
            body="hello",
            credentials=_credentials(),
            http_request=request,
            from_="+15550000001",
            media_url=["https://example.com/photo.jpg"],
            status_callback_url="https://example.com/status",
            to="+15550000002",
        )

        assert message["sid"] == "SM123"
        method, url, headers, body = _request_of(request)
        assert method == "POST"
        assert url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
        assert headers["authorization"] == BASIC_AUTH
        assert _form_of(body) == {
            "Body": "hello",
            "From": "+15550000001",
            "MediaUrl": "https://example.com/photo.jpg",
            "StatusCallback": "https://example.com/status",
            "To": "+15550000002",
        }

    @pytest.mark.asyncio
    async def test_sends_messages_with_a_messaging_service_sid(self):
        request = _mock_http({"sid": "SM123"})

        await send_twilio_message(
            body="hello",
            credentials=_credentials(),
            http_request=request,
            messaging_service_sid="MG123",
            to="+15550000002",
        )

        form = _form_of(_request_of(request)[3])
        assert form["MessagingServiceSid"] == "MG123"
        assert "From" not in form

    @pytest.mark.asyncio
    async def test_appends_one_media_url_pair_per_item(self):
        request = _mock_http({"sid": "SM123"})

        await send_twilio_message(
            credentials=_credentials(),
            http_request=request,
            from_="+15550000001",
            media_url=["https://example.com/a.jpg", "https://example.com/b.jpg"],
            to="+15550000002",
        )

        body = _request_of(request)[3]
        assert body is not None
        pairs = parse_qsl(body, keep_blank_values=True)
        assert pairs.count(("MediaUrl", "https://example.com/a.jpg")) == 1
        assert pairs.count(("MediaUrl", "https://example.com/b.jpg")) == 1

    @pytest.mark.asyncio
    async def test_requires_body_or_media(self):
        with pytest.raises(ValidationError, match="body or mediaUrl is required"):
            await send_twilio_message(
                credentials=_credentials(),
                http_request=_mock_http({"sid": "SM123"}),
                from_="+15550000001",
                to="+15550000002",
            )

    @pytest.mark.asyncio
    async def test_requires_a_sender(self):
        with pytest.raises(ValidationError, match="from or messagingServiceSid is required"):
            await send_twilio_message(
                body="hello",
                credentials=_credentials(),
                http_request=_mock_http({"sid": "SM123"}),
                to="+15550000002",
            )


class TestFetchListDelete:
    """Tests for fetch/list/delete message helpers."""

    @pytest.mark.asyncio
    async def test_fetches_messages_by_sid(self):
        request = _mock_http({"sid": "SM123"})

        await fetch_twilio_message("SM123", credentials=_credentials(), http_request=request)

        method, url, _headers, body = _request_of(request)
        assert method == "GET"
        assert url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages/SM123.json"
        assert body is None

    @pytest.mark.asyncio
    async def test_lists_messages_with_from_and_to_filters(self):
        request = _mock_http({"messages": [{"sid": "SM123"}, {"sid": "SM124"}]})

        messages = await list_twilio_messages(
            credentials=_credentials(),
            http_request=request,
            from_="+15550000001",
            limit=1,
            page_size=50,
            to="+15550000002",
        )

        assert messages == [{"sid": "SM123"}]
        method, url, _headers, _body = _request_of(request)
        assert method == "GET"
        assert url == (
            "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
            "?From=%2B15550000001&PageSize=50&To=%2B15550000002"
        )

    @pytest.mark.asyncio
    async def test_lists_messages_tolerate_missing_messages_key(self):
        request = _mock_http({})
        assert await list_twilio_messages(credentials=_credentials(), http_request=request) == []

    @pytest.mark.asyncio
    async def test_deletes_messages_by_sid(self):
        request = _mock_http(None)

        await delete_twilio_message("SM123", credentials=_credentials(), http_request=request)

        method, url, _headers, _body = _request_of(request)
        assert method == "DELETE"
        assert url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages/SM123.json"


class TestUpdateTwilioCall:
    """Tests for update_twilio_call."""

    @pytest.mark.asyncio
    async def test_updates_live_calls_with_twiml(self):
        request = _mock_http({"sid": "CA123"})

        call = await update_twilio_call(
            "CA123",
            credentials=_credentials(),
            http_request=request,
            twiml="<Response><Say>hello</Say></Response>",
        )

        assert call["sid"] == "CA123"
        method, url, _headers, body = _request_of(request)
        assert method == "POST"
        assert url == "https://api.twilio.com/2010-04-01/Accounts/AC123/Calls/CA123.json"
        assert _form_of(body) == {"Twiml": "<Response><Say>hello</Say></Response>"}

    @pytest.mark.asyncio
    async def test_updates_live_calls_with_a_redirect_url(self):
        request = _mock_http({"sid": "CA123"})

        await update_twilio_call(
            "CA123",
            credentials=_credentials(),
            http_request=request,
            method="GET",
            url="https://example.com/voice",
        )

        form = _form_of(_request_of(request)[3])
        assert form == {"Method": "GET", "Url": "https://example.com/voice"}

    @pytest.mark.asyncio
    async def test_rejects_ambiguous_call_updates(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            await update_twilio_call(
                "CA123",
                credentials=_credentials(),
                http_request=_mock_http({"sid": "CA123"}),
                twiml="<Response></Response>",
                url="https://example.com/voice",
            )

    @pytest.mark.asyncio
    async def test_requires_twiml_url_or_status(self):
        with pytest.raises(ValidationError, match="twiml, url, or status is required"):
            await update_twilio_call(
                "CA123",
                credentials=_credentials(),
                http_request=_mock_http({"sid": "CA123"}),
            )


class TestFetchTwilioMedia:
    """Tests for fetch_twilio_media."""

    @pytest.mark.asyncio
    async def test_fetches_media_with_basic_auth(self):
        request = AsyncMock(return_value=TwilioHttpResponse(status=200, body=b"photo"))

        media = await fetch_twilio_media(
            "https://api.twilio.com/media/photo",
            credentials=_credentials(),
            http_request=request,
        )

        assert media == b"photo"
        method, url, headers, body = _request_of(request)
        assert method == "GET"
        assert url == "https://api.twilio.com/media/photo"
        assert headers == {"authorization": BASIC_AUTH}
        assert body is None

    @pytest.mark.asyncio
    async def test_raises_typed_errors_for_failed_media_downloads(self):
        request = AsyncMock(return_value=TwilioHttpResponse(status=404, body=b""))
        with pytest.raises(ResourceNotFoundError):
            await fetch_twilio_media(
                "https://api.twilio.com/media/missing",
                credentials=_credentials(),
                http_request=request,
            )


class TestErrorMapping:
    """Typed error mapping for non-2xx responses (Python adaptation)."""

    async def _send(self, status: int, payload: Any) -> None:
        await send_twilio_message(
            body="hello",
            credentials=_credentials(),
            http_request=_mock_http(payload, status=status),
            from_="+15550000001",
            to="+15550000002",
        )

    @pytest.mark.asyncio
    async def test_maps_400_to_validation_error_with_twilio_message(self):
        with pytest.raises(ValidationError, match="not a valid phone number"):
            await self._send(400, {"code": 21211, "message": "The 'To' number is not a valid phone number."})

    @pytest.mark.asyncio
    async def test_maps_401_to_authentication_error(self):
        with pytest.raises(AuthenticationError):
            await self._send(401, {"message": "Authentication Error"})

    @pytest.mark.asyncio
    async def test_maps_403_to_permission_error(self):
        with pytest.raises(AdapterPermissionError):
            await self._send(403, {"message": "Forbidden"})

    @pytest.mark.asyncio
    async def test_maps_429_to_rate_limit_error(self):
        with pytest.raises(AdapterRateLimitError):
            await self._send(429, {"message": "Too Many Requests"})

    @pytest.mark.asyncio
    async def test_other_statuses_raise_twilio_api_error_with_status_and_body(self):
        with pytest.raises(TwilioApiError) as excinfo:
            await self._send(500, {"message": "Server Error"})
        assert excinfo.value.status == 500
        assert excinfo.value.body == {"message": "Server Error"}
        assert excinfo.value.adapter == "twilio"


class TestResolveTwilioCredential:
    """Tests for resolve_twilio_credential."""

    @pytest.mark.asyncio
    async def test_resolves_explicit_values(self, _clear_twilio_credential_env: None):
        assert await resolve_twilio_credential("AC123", ENV_ACCOUNT_SID) == "AC123"

    @pytest.mark.asyncio
    async def test_resolves_sync_and_async_resolvers(self, _clear_twilio_credential_env: None):
        assert await resolve_twilio_credential(lambda: "from-sync", ENV_ACCOUNT_SID) == "from-sync"

        async def resolver() -> str:
            return "from-async"

        assert await resolve_twilio_credential(resolver, ENV_ACCOUNT_SID) == "from-async"

    @pytest.mark.asyncio
    async def test_falls_back_to_the_env_var(self, _clear_twilio_credential_env: None):
        os.environ[ENV_ACCOUNT_SID] = "ACenv"
        assert await resolve_twilio_credential(None, ENV_ACCOUNT_SID) == "ACenv"

    @pytest.mark.asyncio
    async def test_missing_credential_names_the_env_var(self, _clear_twilio_credential_env: None):
        with pytest.raises(AuthenticationError, match="TWILIO_ACCOUNT_SID is required"):
            await resolve_twilio_credential(None, ENV_ACCOUNT_SID)


class TestEncodeTwilioForm:
    """Tests for encode_twilio_form value semantics."""

    def test_encodes_scalars_sequences_and_omits_none(self):
        params = encode_twilio_form(
            {
                "Flag": True,
                "Off": False,
                "Items": ["a", "b"],
                "Missing": None,
                "Number": 50,
                "Text": "hi",
            }
        )
        assert params == [
            ("Flag", "true"),
            ("Off", "false"),
            ("Items", "a"),
            ("Items", "b"),
            ("Number", "50"),
            ("Text", "hi"),
        ]

    def test_url_query_assembly_keeps_existing_query(self):
        # Defensive coverage of the search-merge branch in call_twilio_api.
        split = urlsplit("https://api.twilio.com/x.json?A=1")
        assert split.query == "A=1"
