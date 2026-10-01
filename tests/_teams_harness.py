"""Shared harness for Teams tests that drive the real webhook bridge.

Python counterpart of upstream ``packages/adapter-teams/src/test-utils.ts``:
activities go through ``TeamsAdapter.handle_webhook`` → ``BridgeHttpAdapter``
→ the Microsoft Teams SDK ``HttpServer`` → ``TeamsAdapter._dispatch_activity``,
never straight into private handlers.
"""

from __future__ import annotations

import inspect
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat_sdk.adapters.teams.adapter import TeamsAdapter
from chat_sdk.adapters.teams.types import TeamsAdapterConfig
from chat_sdk.types import WebhookOptions

# Upstream uses an all-digit GUID, which makes its casing tests vacuous; hex
# letters make `.upper()` differ.
APP_ID = "11111111-2222-3333-4444-5555aaaabbbb"
BOT_ID = f"28:{APP_ID}"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"


def allow_unauthenticated_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the SDK's skip-auth flag so unsigned test requests reach the handlers.

    The fixture from #180: ``microsoft-teams-apps`` 2.0.14+ renamed
    ``skip_auth`` to ``dangerously_allow_unauthenticated_requests``, so set
    whichever flag the installed SDK has. In this mode the SDK hands the
    adapter a placeholder token whose ``service_url`` is the body's
    ``serviceUrl`` (or ``""``).
    """
    from microsoft_teams.apps.http.http_server import HttpServer

    real_initialize = HttpServer.initialize
    skip_flag = (
        "dangerously_allow_unauthenticated_requests"
        if "dangerously_allow_unauthenticated_requests" in inspect.signature(real_initialize).parameters
        else "skip_auth"
    )

    def _initialize_skip_auth(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.pop("skip_auth", None)
        kwargs.pop("dangerously_allow_unauthenticated_requests", None)
        return real_initialize(self, *args, **{**kwargs, skip_flag: True})

    monkeypatch.setattr(HttpServer, "initialize", _initialize_skip_auth)


def accept_test_signing_key(monkeypatch: pytest.MonkeyPatch, signing_key: Any) -> None:
    """Resolve every JWKS lookup to ``signing_key`` so the SDK's real JWT checks run."""
    import jwt

    def get_signing_key_from_jwt(self: Any, token: str) -> Any:
        return SimpleNamespace(key=signing_key.public_key())

    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", get_signing_key_from_jwt)
    monkeypatch.delenv("CLOUD", raising=False)
    monkeypatch.delenv("DANGEROUSLY_ALLOW_UNAUTHENTICATED_REQUESTS", raising=False)


def bot_framework_token(signing_key: Any, service_url: str = SERVICE_URL, app_id: str = APP_ID) -> str:
    """A Bot Framework-issued inbound JWT for ``app_id`` carrying ``serviceurl``."""
    import jwt

    now = int(time.time())
    claims = {
        "iss": BOT_FRAMEWORK_ISSUER,
        "aud": app_id,
        "serviceurl": service_url,
        "iat": now,
        "nbf": now,
        "exp": now + 600,
    }
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test-kid"})


def make_logger() -> MagicMock:
    return MagicMock(debug=MagicMock(), info=MagicMock(), warn=MagicMock(), error=MagicMock())


def make_adapter(logger: Any = None, **overrides: Any) -> TeamsAdapter:
    config = TeamsAdapterConfig(
        app_id=overrides.pop("app_id", APP_ID),
        app_password=overrides.pop("app_password", "secret"),
        logger=logger if logger is not None else make_logger(),
        **overrides,
    )
    return TeamsAdapter(config)


def _drop_none(value: Any) -> Any:
    """Drop ``None`` values the way ``JSON.stringify`` drops ``undefined``."""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


class _Request:
    def __init__(self, body: str, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def text(self) -> str:
        return self._body


async def receive(
    adapter: TeamsAdapter,
    body: dict[str, Any],
    options: WebhookOptions | None = None,
    token: str | None = None,
    *,
    keep_nulls: bool = False,
) -> dict[str, Any]:
    """POST ``body`` through ``adapter.handle_webhook`` (the real bridge).

    ``None`` values are dropped (``undefined``) unless ``keep_nulls`` sends
    them as explicit JSON ``null``.
    """
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    payload = body if keep_nulls else _drop_none(body)
    return await adapter.handle_webhook(_Request(json.dumps(payload), headers), options)


def spy_activity_sender(adapter: TeamsAdapter) -> AsyncMock:
    """Observe the SDK send with its resolved ``ConversationReference``.

    ``TeamsAdapter._send_to`` hands ``(activity, ref)`` to
    ``App.activity_sender.send`` whenever the App has one, on every SDK line
    (upstream ``spyActivitySender``).
    """
    send = AsyncMock(return_value=SimpleNamespace(id="sent", type="message"))
    adapter._app.activity_sender = SimpleNamespace(send=send)
    return send


def collecting_options() -> tuple[WebhookOptions, list[Any]]:
    tasks: list[Any] = []
    return WebhookOptions(wait_until=tasks.append), tasks
