"""Tests for ``WhatsAppApiError``.

Port of packages/adapter-whatsapp/src/errors.test.ts (chat@4.41.1), plus
Python-specific cases for ``bool``/``float`` codes and JSON constants.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from chat_sdk.adapters.whatsapp import WhatsAppApiError
from chat_sdk.shared.errors import AdapterError


class TestWhatsAppApiError:
    def test_preserves_the_error_contract_and_raw_meta_response(self):
        raw = {
            "error": {
                "message": "Invalid token",
                "type": "OAuthException",
                "code": 190,
                "error_subcode": 463,
                "error_data": {"details": "The access token has expired"},
                "fbtrace_id": "trace123",
                "extra": {"retained": True},
            },
        }
        error = WhatsAppApiError("WhatsApp API error", 401, json.dumps(raw))

        assert isinstance(error, Exception)
        assert isinstance(error, AdapterError)
        assert type(error).__name__ == "WhatsAppApiError"
        assert error.adapter == "whatsapp"
        assert error.code == "AUTH_FAILED"
        assert error.error_code == 190
        assert error.status == 401
        assert error.provider_message == "Invalid token"
        assert error.type == "OAuthException"
        assert error.subcode == 463
        assert error.details == "The access token has expired"
        assert error.trace_id == "trace123"
        assert error.raw == raw
        assert str(error) == "WhatsApp API error: 401 Invalid token"

    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (400, 130_429, "RATE_LIMITED"),
            (400, 80_007, "RATE_LIMITED"),
            (429, 100, "RATE_LIMITED"),
            (400, 190, "AUTH_FAILED"),
            (401, 100, "AUTH_FAILED"),
            (400, 10, "PERMISSION_DENIED"),
            (400, 200, "PERMISSION_DENIED"),
            (400, 299, "PERMISSION_DENIED"),
            (400, 300, None),
            (403, 100, "PERMISSION_DENIED"),
            (404, 100, "NOT_FOUND"),
            (400, 131_047, None),
            (500, None, None),
        ],
    )
    def test_maps_status_and_meta_code_to_expected(self, status: int, code: int | None, expected: str | None):
        error = WhatsAppApiError("WhatsApp API error", status, json.dumps({"error": {"code": code}}))

        assert error.code == expected
        assert error.error_code == code

    def test_accepts_numeric_strings_from_proxies_in_front_of_the_cloud_api(self):
        error = WhatsAppApiError(
            "WhatsApp API error",
            400,
            json.dumps({"error": {"code": "130429", "error_subcode": "2494055"}}),
        )

        assert error.code == "RATE_LIMITED"
        assert error.error_code == 130_429
        assert error.subcode == 2_494_055

    @pytest.mark.parametrize("body", ["", "<html>Bad gateway</html>", '{"error":'])
    def test_retains_non_json_response_without_masking_the_http_error(self, body: str):
        error = WhatsAppApiError("WhatsApp API error", 502, body)

        assert error.raw == body
        assert error.status == 502
        assert error.code is None
        assert error.error_code is None
        assert str(error) == f"WhatsApp API error: 502 {body}"

    def test_bounds_a_long_non_json_body_in_the_message_and_keeps_it_whole_in_raw(self):
        body = "x" * 2000
        error = WhatsAppApiError("WhatsApp API error", 502, body)

        assert error.raw == body
        assert str(error) == f"WhatsApp API error: 502 {'x' * 500}…"

    def test_falls_back_to_the_body_when_meta_omits_a_message(self):
        body = json.dumps({"error": {"code": 100}})
        error = WhatsAppApiError("WhatsApp API error", 400, body)

        assert error.provider_message is None
        assert str(error) == f"WhatsApp API error: 400 {body}"

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            [],
            "unexpected",
            {"error": None},
            {"error": []},
            {"error": "unexpected"},
            {
                "error": {
                    "code": {"value": 190},
                    "error_subcode": "4.5",
                    "error_data": {"details": []},
                    "fbtrace_id": 123,
                    "message": ["nope"],
                    "type": 7,
                },
            },
        ],
    )
    def test_ignores_invalid_field_shapes(self, raw: Any):
        error = WhatsAppApiError("WhatsApp API error", 500, json.dumps(raw))

        assert error.raw == raw
        assert error.code is None
        assert error.error_code is None
        assert error.provider_message is None
        assert error.type is None
        assert error.details is None
        assert error.subcode is None
        assert error.trace_id is None

    def test_preserves_error_code_zero_without_requiring_optional_fields(self):
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"code": 0}}))

        assert error.error_code == 0
        assert error.code == "AUTH_FAILED"
        assert error.details is None
        assert error.subcode is None
        assert error.trace_id is None


class TestWhatsAppApiErrorPythonSpecific:
    """Python type-system hazards the JS port does not have."""

    @pytest.mark.parametrize("code", [True, False])
    def test_bool_code_is_not_an_integer(self, code: bool):
        # ``isinstance(True, int)`` holds in Python; JSON ``true`` is not a
        # number in JS, so ``True`` must not map to code 1 (or ``False`` to
        # the AUTH_FAILED code 0).
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"code": code}}))

        assert error.error_code is None
        assert error.code is None

    def test_integral_float_code_matches_js_number_semantics(self):
        # ``JSON.parse("4.0")`` is the JS number ``4`` (``Number.isInteger``
        # holds), so upstream reads it as code 4.
        error = WhatsAppApiError("WhatsApp API error", 400, '{"error": {"code": 4.0, "error_subcode": 1e3}}')

        assert error.error_code == 4
        assert isinstance(error.error_code, int)
        assert error.code == "RATE_LIMITED"
        assert error.subcode == 1000

    def test_fractional_float_code_is_rejected(self):
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"code": 4.5}}))

        assert error.error_code is None
        assert error.code is None

    @pytest.mark.parametrize("code", ["١٩٠", "190\n", " 190"])
    def test_numeric_string_must_be_plain_ascii_digits(self, code: str):
        # JS ``/^-?\d+$/`` has no Unicode digits and ``$`` does not match
        # before a trailing newline.
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"code": code}}))

        assert error.error_code is None

    def test_negative_numeric_string_is_parsed(self):
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"code": "-1"}}))

        assert error.error_code == -1

    @pytest.mark.parametrize("body", ["NaN", '{"error": {"code": Infinity}}'])
    def test_json_constants_rejected_like_js_json_parse(self, body: str):
        error = WhatsAppApiError("WhatsApp API error", 500, body)

        assert error.raw == body
        assert error.error_code is None

    def test_provider_message_is_not_truncated(self):
        long_message = "m" * 800
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"message": long_message}}))

        assert str(error) == f"WhatsApp API error: 400 {long_message}"

    def test_empty_provider_message_is_used_not_the_body(self):
        # ``error.message ?? body`` only falls back on null/undefined.
        error = WhatsAppApiError("WhatsApp API error", 400, json.dumps({"error": {"message": ""}}))

        assert error.provider_message == ""
        assert str(error) == "WhatsApp API error: 400 "

    @pytest.mark.parametrize(("length", "suffix"), [(500, ""), (501, "…")])
    def test_non_json_body_is_truncated_past_500_characters(self, length: int, suffix: str):
        error = WhatsAppApiError("WhatsApp API error", 502, "x" * length)

        assert str(error) == f"WhatsApp API error: 502 {'x' * 500}{suffix}"

    @pytest.mark.parametrize("field", ["code", "error_subcode"])
    def test_numeric_string_past_the_int_digit_limit_stays_typed(self, field: str):
        # CPython refuses ``int()`` past 4300 digits; upstream's ``Number()``
        # gives ``Infinity`` without throwing.
        body = json.dumps({"error": {"message": "m", field: "1" * 5000}})

        error = WhatsAppApiError("WhatsApp API error", 400, body)

        assert error.status == 400
        assert error.provider_message == "m"
        assert error.error_code is None
        assert error.subcode is None
