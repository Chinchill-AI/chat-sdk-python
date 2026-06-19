"""Tests for the runtime-free Teams Bot Connector API primitives subpath.

Port of ``packages/adapter-teams/src/api/index.test.ts`` and
``api/boundary.test.ts`` (NEW in chat@4.31.0, commit ``8c71411``), exposed
upstream as ``@chat-adapter/teams/api``. These primitives never touch the
network in tests: a fake ``fetch`` (an :class:`~unittest.mock.AsyncMock`) is
injected and its recorded calls are asserted, mirroring upstream's ``vi.fn()``
request mocks.

Upstream's fixtures use a ``smba.example`` ``serviceUrl``; that host is **not**
in this port's Bot Framework allowlist (the SSRF / token-leak divergence — see
``docs/UPSTREAM_SYNC.md``). The faithful ports therefore use a trusted host
(``smba.trafficmanager.net``) while preserving the exact path / encoding
assertions, and a dedicated divergence test asserts an untrusted host is
rejected before any token is fetched.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest

from chat_sdk.adapters.teams.api import (
    TeamsApiError,
    TeamsCredentials,
    build_teams_message_activity,
    call_teams_connector_api,
    create_teams_conversation,
    delete_teams_message,
    is_trusted_teams_service_url,
    post_teams_message,
    resolve_teams_access_token,
    send_teams_typing,
    update_teams_message,
)

# A trusted Bot Framework host substituted for upstream's ``smba.example``
# fixture (which this port rejects). Matches the allowlist's
# ``smba.trafficmanager.net`` pattern.
TRUSTED_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
TRUSTED_SERVICE_URL_BARE = "https://smba.trafficmanager.net/"


class _Response:
    """Minimal stand-in for the injected fetch's response object.

    Exposes ``status``, an ``ok`` flag, and a sync ``text()`` returning the
    raw body string — the shape :func:`read_response_body` and the connector
    client read (mirroring the DOM ``Response`` upstream constructs).
    """

    def __init__(self, body: str, status: int = 200) -> None:
        self._body = body
        self.status = status

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self._body


def _json_response(value: Any, status: int = 200) -> _Response:
    return _Response(json.dumps(value), status)


def _empty_response(status: int = 204) -> _Response:
    return _Response("", status)


def _url(call_args: Any) -> str:
    return call_args.args[0]


CREDENTIALS = TeamsCredentials(app_id="app-id", app_password="secret", tenant_id="tenant-id")


class TestTeamsApiPrimitives:
    async def test_resolves_access_tokens_with_client_credentials(self) -> None:
        request = AsyncMock(return_value=_json_response({"access_token": "t"}))

        token = await resolve_teams_access_token(CREDENTIALS, fetch=request)

        assert token == "t"
        call = request.await_args
        assert _url(call) == "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
        body = parse_qs(str(call.kwargs["body"]))
        assert body["client_id"] == ["app-id"]
        assert body["scope"] == ["https://api.botframework.com/.default"]

    def test_builds_message_activities_with_adaptive_cards(self) -> None:
        activity = build_teams_message_activity(
            adaptive_card={"type": "AdaptiveCard"},
            markdown_text="**hello**",
        )

        assert activity["attachments"] == [
            {
                "content": {"type": "AdaptiveCard"},
                "contentType": "application/vnd.microsoft.card.adaptive",
            }
        ]
        assert activity["text"] == "**hello**"
        assert activity["textFormat"] == "markdown"
        assert activity["type"] == "message"

    async def test_posts_teams_messages_through_connector_rest(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response({"access_token": "token"}),
                _json_response({"id": "activity-id"}),
            ]
        )

        posted = await post_teams_message(
            conversation_id="19:abc@thread.tacv2",
            credentials=CREDENTIALS,
            fetch=request,
            markdown_text="hello",
            service_url=TRUSTED_SERVICE_URL,
        )

        assert posted.id == "activity-id"
        assert _url(request.await_args_list[1]) == (
            "https://smba.trafficmanager.net/teams/v3/conversations/19%3Aabc%40thread.tacv2/activities"
        )
        headers = request.await_args_list[1].kwargs["headers"]
        assert headers["authorization"] == "Bearer token"
        assert headers["content-type"] == "application/json"

    async def test_posts_threaded_replies_when_reply_to_id_is_provided(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response({"access_token": "token"}),
                _json_response({"id": "reply-id"}),
            ]
        )

        await post_teams_message(
            conversation_id="conversation",
            credentials=CREDENTIALS,
            fetch=request,
            reply_to_id="root",
            service_url=TRUSTED_SERVICE_URL,
            text="reply",
        )

        assert _url(request.await_args_list[1]) == (
            "https://smba.trafficmanager.net/teams/v3/conversations/conversation/activities/root"
        )

    async def test_updates_deletes_types_and_creates_conversations(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response({"access_token": "token"}),
                _json_response({"ok": True}),
                _json_response({"access_token": "token"}),
                _empty_response(status=204),
                _json_response({"access_token": "token"}),
                _json_response({"ok": True}),
                _json_response({"access_token": "token"}),
                _json_response({"id": "conversation-id"}),
            ]
        )

        await update_teams_message(
            conversation_id="conversation",
            credentials=CREDENTIALS,
            fetch=request,
            message_id="activity",
            service_url=TRUSTED_SERVICE_URL_BARE,
            text="updated",
        )
        await delete_teams_message(
            conversation_id="conversation",
            credentials=CREDENTIALS,
            fetch=request,
            message_id="activity",
            service_url=TRUSTED_SERVICE_URL_BARE,
        )
        await send_teams_typing(
            conversation_id="conversation",
            credentials=CREDENTIALS,
            fetch=request,
            service_url=TRUSTED_SERVICE_URL_BARE,
        )
        await create_teams_conversation(
            credentials=CREDENTIALS,
            fetch=request,
            members=[_member("user")],
            service_url=TRUSTED_SERVICE_URL_BARE,
            tenant_id="tenant",
        )

        calls = request.await_args_list
        assert calls[1].kwargs["method"] == "PUT"
        assert calls[3].kwargs["method"] == "DELETE"
        assert calls[5].kwargs["method"] == "POST"
        assert calls[7].kwargs["method"] == "POST"

    async def test_throws_teams_api_error_for_connector_errors(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response({"access_token": "token"}),
                _json_response({"error": "rate limit"}, status=429),
            ]
        )

        with pytest.raises(TeamsApiError) as exc_info:
            await post_teams_message(
                conversation_id="conversation",
                credentials=CREDENTIALS,
                fetch=request,
                service_url=TRUSTED_SERVICE_URL_BARE,
                text="hello",
            )

        assert exc_info.value.body == {"error": "rate limit"}
        assert exc_info.value.status == 429

    async def test_uses_a_direct_access_token_and_normalizes_slashless_service_url(self) -> None:
        request = AsyncMock(return_value=_json_response({}))

        posted = await post_teams_message(
            conversation_id="c",
            credentials=TeamsCredentials(access_token=lambda: "direct"),
            fetch=request,
            service_url="https://smba.trafficmanager.net",
            text="hi",
        )

        assert posted.id == ""
        request.assert_awaited_once()
        assert _url(request.await_args) == ("https://smba.trafficmanager.net/v3/conversations/c/activities")
        assert request.await_args.kwargs["headers"]["authorization"] == "Bearer direct"

    async def test_falls_back_to_the_default_tenant_when_none_is_provided(self) -> None:
        request = AsyncMock(return_value=_json_response({"access_token": "t"}))

        await resolve_teams_access_token(
            TeamsCredentials(app_id="id", app_password="secret"),
            fetch=request,
        )

        assert _url(request.await_args) == ("https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token")

    async def test_requires_either_access_token_or_app_id_and_app_password(self) -> None:
        request = AsyncMock()

        with pytest.raises(TeamsApiError, match="accessToken or appId and appPassword"):
            await resolve_teams_access_token(
                TeamsCredentials(app_id="only-id"),
                fetch=request,
            )

        request.assert_not_awaited()

    async def test_throws_when_the_token_request_fails(self) -> None:
        request = AsyncMock(return_value=_json_response({"error": "bad"}, status=400))

        with pytest.raises(TeamsApiError) as exc_info:
            await resolve_teams_access_token(CREDENTIALS, fetch=request)

        assert exc_info.value.status == 400

    async def test_throws_when_the_token_response_omits_access_token(self) -> None:
        request = AsyncMock(return_value=_json_response({"token_type": "Bearer"}))

        with pytest.raises(TeamsApiError, match="did not include access_token"):
            await resolve_teams_access_token(CREDENTIALS, fetch=request)

    def test_rejects_combining_markdown_text_with_text(self) -> None:
        with pytest.raises(TypeError):
            build_teams_message_activity(markdown_text="a", text="b")

    async def test_includes_optional_fields_when_creating_a_conversation(self) -> None:
        request = AsyncMock(
            side_effect=[
                _json_response({"access_token": "token"}),
                _json_response({"id": "conversation"}),
            ]
        )

        await create_teams_conversation(
            bot=_member("bot"),
            conversation_type="personal",
            credentials=CREDENTIALS,
            fetch=request,
            is_group=True,
            members=[_member("user")],
            service_url=TRUSTED_SERVICE_URL_BARE,
        )

        body = json.loads(request.await_args_list[1].kwargs["body"])
        assert body["bot"] == {"id": "bot"}
        assert body["conversationType"] == "personal"
        assert body["isGroup"] is True
        assert body["members"] == [{"id": "user"}]


class TestTeamsApiSsrfDivergence:
    """Python-first SSRF / token-leak gate (see docs/UPSTREAM_SYNC.md)."""

    async def test_rejects_a_non_teams_service_url_host_before_fetching_a_token(self) -> None:
        request = AsyncMock(return_value=_json_response({"access_token": "token"}))

        # Upstream's own fixture host — untrusted under this port's allowlist.
        with pytest.raises(ValueError, match="untrusted serviceUrl"):
            await call_teams_connector_api(
                credentials=CREDENTIALS,
                path="v3/conversations/c/activities",
                service_url="https://smba.example/",
                fetch=request,
            )

        # The gate runs BEFORE any token request, so no bearer token leaks.
        request.assert_not_awaited()

    async def test_post_teams_message_rejects_untrusted_host(self) -> None:
        request = AsyncMock(return_value=_json_response({"access_token": "token"}))

        with pytest.raises(ValueError, match="untrusted serviceUrl"):
            await post_teams_message(
                conversation_id="c",
                credentials=CREDENTIALS,
                fetch=request,
                service_url="https://attacker.example/",
                text="hi",
            )

        request.assert_not_awaited()

    def test_allowlist_accepts_known_bot_framework_hosts(self) -> None:
        assert is_trusted_teams_service_url("https://smba.trafficmanager.net/")
        assert is_trusted_teams_service_url("https://smba.uk.botframework.com/")
        assert is_trusted_teams_service_url("https://smba.gov.botframework.us/")
        assert is_trusted_teams_service_url("https://smba.infra.gcc.teams.microsoft.com/")

    def test_allowlist_rejects_untrusted_and_malformed_hosts(self) -> None:
        # Plain attacker host.
        assert not is_trusted_teams_service_url("https://attacker.example/")
        # http (not https) to a real-looking host.
        assert not is_trusted_teams_service_url("http://smba.trafficmanager.net/")
        # Lookalike suffix that the anchored regex must reject.
        assert not is_trusted_teams_service_url("https://botframework.com.attacker.example/")
        # Credentials/host-confusion attempt.
        assert not is_trusted_teams_service_url("https://smba.trafficmanager.net@attacker.example/")
        # Non-string input fails closed.
        assert not is_trusted_teams_service_url(None)  # type: ignore[arg-type]


class TestApiImportBoundary:
    """Port of upstream ``api/boundary.test.ts``.

    Upstream's boundary test is a **static source-scan**: it reads every
    non-test ``.ts`` in the ``api/`` directory and asserts the source never
    imports the full adapter, the shared runtime, or ``@microsoft/teams.apps``.
    We port that source-scan over the ``api/`` package's ``.py`` files — this
    is the right granularity while the package's ``teams/__init__.py`` is still
    eager (the PEP 562 lazy-subpath registration that would let a *runtime*
    ``sys.modules`` check pass is deferred to the packaging PR T7).
    """

    def test_api_source_does_not_import_the_adapter_sdk_or_runtime(self) -> None:
        import chat_sdk.adapters.teams.api as api_pkg

        package_dir = Path(api_pkg.__file__).parent
        sources = [
            path.read_text(encoding="utf-8")
            for path in sorted(package_dir.glob("*.py"))
            if not path.name.startswith("test_")
        ]
        assert sources, "expected at least one api source file"
        joined = "\n".join(sources)

        # No Teams SDK import in any form.
        assert "import microsoft_teams" not in joined
        assert "from microsoft_teams" not in joined
        # No high-level adapter / shared-runtime imports.
        assert "from chat_sdk.adapters.teams.adapter" not in joined
        assert "import chat_sdk.adapters.teams.adapter" not in joined
        assert "from chat_sdk.adapters.teams.bridge" not in joined
        assert "from chat_sdk.adapters.teams.cards" not in joined
        assert "from chat_sdk.adapters.teams.types" not in joined
        # No eager HTTP-client import (httpx is lazily imported inside the
        # default fetch only, so it must never appear at module top level).
        for source in sources:
            assert "\nimport httpx" not in source, "httpx must be lazily imported"
            assert "\nimport aiohttp" not in source

    def test_importing_api_does_not_eagerly_import_an_http_client(self) -> None:
        """Importing the api subpath in a fresh interpreter must not load an
        HTTP client — the default fetch imports ``httpx`` lazily.

        (We do not assert the high-level adapter is absent here: the eager
        ``teams/__init__.py`` pulls it in transitively until packaging PR T7
        makes the subpath lazy. The source-scan above already proves the api
        sources themselves never import the adapter.)
        """
        code = (
            "import sys\n"
            "import chat_sdk.adapters.teams.api\n"
            "forbidden = ['microsoft_teams', 'httpx', 'aiohttp']\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'api subpath eagerly imported: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def _member(member_id: str) -> Any:
    from chat_sdk.adapters.teams.api import TeamsConversationMember

    return TeamsConversationMember(id=member_id)
