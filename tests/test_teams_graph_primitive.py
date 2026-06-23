"""Tests for the runtime-free Microsoft Graph primitives subpath for Teams.

Port of ``packages/adapter-teams/src/graph/index.test.ts`` and
``graph/boundary.test.ts`` (NEW in chat@4.31.0, commit ``8c71411``), exposed
upstream as ``@chat-adapter/teams/graph``. These primitives never touch the
network in tests: a fake ``fetch`` (an :class:`~unittest.mock.AsyncMock`) is
injected and its recorded calls are asserted, mirroring upstream's ``vi.fn()``
request mocks. The first injected call always resolves the Graph-scoped token
(``access_token``); subsequent calls return the Graph response bodies.

Python-specific divergence (no upstream counterpart, see
``docs/UPSTREAM_SYNC.md`` Known Non-Parity): the Graph-scoped bearer token is
attached only to the ``graph.microsoft.com`` host. A dedicated test asserts a
hostile ``@odata.nextLink`` is rejected (``ValueError``) *before* the token is
fetched — the SSRF / token-leak guard. Upstream follows whatever ``nextLink``
the server returns with no host check.
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

from chat_sdk.adapters.teams.api import TeamsApiError, TeamsCredentials
from chat_sdk.adapters.teams.graph import (
    TeamsChannelInfo,
    TeamsGraphMessage,
    extract_text_from_graph_message,
    get_teams_channel,
    get_teams_channel_message,
    is_trusted_graph_url,
    list_teams_channel_messages,
    list_teams_chat_messages,
    list_teams_message_replies,
    paginate_teams_graph,
    to_graph_message,
)

CREDENTIALS = TeamsCredentials(
    app_id="app-id",
    app_password="secret",
    tenant_id="tenant-id",
)

# A Graph-scoped token-response body and a sentinel attacker host used by the
# SSRF divergence test.
_TOKEN_BODY = {"access_token": "graph-token"}
_ATTACKER_NEXT_LINK = "https://evil.example.com/v1.0/next"


class _Response:
    """Minimal stand-in for the injected fetch's response object.

    Exposes ``status``, an ``ok`` flag, and a sync ``text()`` returning the raw
    body string — the shape :func:`read_response_body` and the Graph client
    read (mirroring the DOM ``Response`` upstream constructs).
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


def _fetch(*responses: _Response) -> AsyncMock:
    """Build an injectable async fetch returning ``responses`` in order."""
    return AsyncMock(side_effect=list(responses))


def _url(call_args: Any) -> str:
    """Stringify the URL positional arg of a recorded fetch call."""
    return str(call_args.args[0])


def _headers(call_args: Any) -> dict[str, str]:
    return call_args.kwargs["headers"]


class TestTeamsGraphPrimitives:
    """Port of upstream ``graph/index.test.ts`` (7 ``it`` blocks)."""

    @pytest.mark.asyncio
    async def test_lists_chat_messages_with_graph_token_scope(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response(
                {
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
                    "value": [
                        {
                            "body": {
                                "content": "<p>Hello <b>world</b></p>",
                                "contentType": "html",
                            },
                            "createdDateTime": "2026-01-01T00:00:00Z",
                            "from": {"user": {"displayName": "Ada", "id": "user"}},
                            "id": "message-id",
                        }
                    ],
                }
            ),
        )

        result = await list_teams_chat_messages(
            _opts("chat", chat_id="19:chat", limit=5, fetch=request),
        )

        # The token request (call 0) carries the Graph ``.default`` scope.
        token_body = parse_qs(request.await_args_list[0].kwargs["body"])
        assert token_body["scope"] == ["https://graph.microsoft.com/.default"]
        # The data request (call 1) is the encoded chat-messages URL with $top.
        assert _url(request.await_args_list[1]) == ("https://graph.microsoft.com/v1.0/chats/19%3Achat/messages?$top=5")
        # The resolved Graph token is attached as the Bearer credential.
        assert _headers(request.await_args_list[1])["authorization"] == "Bearer graph-token"
        assert request.await_args_list[1].kwargs["method"] == "GET"
        assert result.cursor == "https://graph.microsoft.com/v1.0/next"
        assert len(result.items) == 1
        assert result.items[0].id == "message-id"
        assert result.items[0].text == "Hello world"

    @pytest.mark.asyncio
    async def test_lists_channel_messages_and_replies(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({"value": []}),
            _json_response(_TOKEN_BODY),
            _json_response({"value": []}),
        )

        await list_teams_channel_messages(
            _opts("channel_messages", channel_id="channel", team_id="team", fetch=request),
        )
        await list_teams_message_replies(
            _opts(
                "replies",
                channel_id="channel",
                message_id="root",
                team_id="team",
                fetch=request,
            ),
        )

        assert _url(request.await_args_list[1]) == (
            "https://graph.microsoft.com/v1.0/teams/team/channels/channel/messages"
        )
        assert _url(request.await_args_list[3]) == (
            "https://graph.microsoft.com/v1.0/teams/team/channels/channel/messages/root/replies"
        )

    @pytest.mark.asyncio
    async def test_gets_a_channel_message_and_channel_info(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({"body": {"content": "hello"}, "id": "m"}),
            _json_response(_TOKEN_BODY),
            _json_response({"displayName": "General", "id": "c"}),
        )

        message = await get_teams_channel_message(
            _opts(
                "message",
                channel_id="c",
                message_id="m",
                team_id="t",
                fetch=request,
            ),
        )
        assert message.id == "m"
        assert message.text == "hello"

        channel = await get_teams_channel(
            _opts("get_channel", channel_id="c", team_id="t", fetch=request),
        )
        assert channel.display_name == "General"
        assert channel.id == "c"

    @pytest.mark.asyncio
    async def test_paginates_next_links(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({"value": []}),
        )

        await paginate_teams_graph(
            "https://graph.microsoft.com/v1.0/next",
            _opts("paginate", fetch=request),
        )

        # The nextLink is used verbatim (not re-encoded) for the data call.
        assert _url(request.await_args_list[1]) == "https://graph.microsoft.com/v1.0/next"

    @pytest.mark.asyncio
    async def test_throws_teams_api_error_when_graph_responds_with_an_error(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({"error": "forbidden"}, status=403),
        )

        with pytest.raises(TeamsApiError) as excinfo:
            await list_teams_chat_messages(_opts("chat", chat_id="c", fetch=request))
        assert excinfo.value.status == 403

    @pytest.mark.asyncio
    async def test_returns_sparse_messages_with_empty_text_and_minimal_fields(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({"value": [{"id": "m"}]}),
        )

        result = await list_teams_chat_messages(_opts("chat", chat_id="c", fetch=request))

        # Exact equality: no created_at / from_ / reply_to_id populated.
        assert result.items[0] == TeamsGraphMessage(id="m", raw={"id": "m"}, text="")
        assert result.items[0].created_at is None
        assert result.items[0].from_ is None
        assert result.items[0].reply_to_id is None
        assert result.cursor is None

    @pytest.mark.asyncio
    async def test_falls_back_to_channel_id_and_omits_display_name(self) -> None:
        request = _fetch(
            _json_response(_TOKEN_BODY),
            _json_response({}),
        )

        channel = await get_teams_channel(
            _opts("get_channel", channel_id="c-id", team_id="t", fetch=request),
        )

        assert channel == TeamsChannelInfo(id="c-id", raw={})
        assert channel.display_name is None


class TestTeamsGraphTextExtraction:
    """Direct coverage of the ordered ``extractTextFromGraphMessage`` regex pass
    and ``toGraphMessage`` shaping (the helpers exercised indirectly above)."""

    def test_empty_body_yields_empty_text(self) -> None:
        assert extract_text_from_graph_message({}) == ""
        assert extract_text_from_graph_message({"body": {"content": ""}}) == ""

    def test_converts_mentions_breaks_paragraphs_and_decodes_entities(self) -> None:
        content = "<at>Ada</at> said:<br/>line<br>two</p><p>next &nbsp;&lt;tag&gt;&amp;done"
        assert extract_text_from_graph_message({"body": {"content": content}}) == (
            "@Ada said:\nline\ntwo\n\nnext  <tag>&done"
        )

    def test_decodes_amp_last_so_encoded_entities_survive(self) -> None:
        # ``&amp;lt;`` must decode to ``&lt;`` (literal), NOT to ``<``: ``&amp;``
        # is the LAST replacement, so the ``&lt;`` pass never sees this ``<``.
        assert extract_text_from_graph_message({"body": {"content": "a &amp;lt; b"}}) == "a &lt; b"

    def test_to_graph_message_populates_optional_fields_when_present(self) -> None:
        message = to_graph_message(
            {
                "id": "m1",
                "createdDateTime": "2026-01-01T00:00:00Z",
                "from": {"user": {"displayName": "Ada", "id": "u", "userIdentityType": "aadUser"}},
                "replyToId": "root",
                "body": {"content": "hi"},
            }
        )
        assert message.created_at == "2026-01-01T00:00:00Z"
        assert message.reply_to_id == "root"
        assert message.from_ is not None
        assert message.from_.display_name == "Ada"
        assert message.from_.user_identity_type == "aadUser"


class TestTeamsGraphSsrfDivergence:
    """Python-first SSRF / token-leak guard (no upstream counterpart)."""

    @pytest.mark.asyncio
    async def test_rejects_an_attacker_next_link_host_before_attaching_the_token(self) -> None:
        request = _fetch(_json_response(_TOKEN_BODY))

        with pytest.raises(ValueError, match="untrusted host"):
            await paginate_teams_graph(_ATTACKER_NEXT_LINK, _opts("paginate", fetch=request))

        # Critically: the gate fires BEFORE any token is fetched — the injected
        # fetch is never awaited, so the Graph token never leaves the process.
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_call_with_an_absolute_attacker_url_is_rejected(self) -> None:
        request = _fetch(_json_response(_TOKEN_BODY))

        with pytest.raises(ValueError, match="untrusted host"):
            # A list-helper limit path is relative; but a caller can pass an
            # absolute hostile URL through ``paginate_teams_graph``. Assert the
            # http(s)-prefixed branch is what's gated.
            await paginate_teams_graph("http://graph.microsoft.com/v1.0/next", _opts("p", fetch=request))
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_mixed_case_scheme_and_scheme_relative_attacker_urls(self) -> None:
        # Regression: a case-sensitive ``startswith("http")`` routing test let a
        # mixed-case scheme (``HTTPS://``) or a scheme-relative (``//host``) URL
        # skip the absolute branch; ``urljoin`` still resolved it to the attacker
        # host and attached the Graph token. Routing now keys off the parsed
        # scheme/netloc, so every absolute form is forced through the allowlist.
        for hostile in (
            "HTTPS://evil.example/x",
            "HtTpS://evil.example/x",
            "//evil.example/x",
        ):
            request = _fetch(_json_response(_TOKEN_BODY))
            with pytest.raises(ValueError, match="untrusted host"):
                await paginate_teams_graph(hostile, _opts("p", fetch=request))
            request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_url_fails_closed_without_fetching_a_token(self) -> None:
        # urlparse raises on some inputs (e.g. a bad IPv6 literal). The router must
        # fail closed — treat it as absolute and reject via the allowlist — never join
        # it or fetch a token for it.
        request = _fetch(_json_response(_TOKEN_BODY))
        with pytest.raises(ValueError, match="untrusted host"):
            await paginate_teams_graph("https://[oops", _opts("p", fetch=request))
        request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_colon_in_relative_graph_path_still_joins_onto_the_trusted_base(self) -> None:
        # A relative Graph segment whose colon falls after a slash (e.g. a OneDrive
        # ``…/root:/path:/content`` address) parses with no scheme/netloc, so the
        # parse-based routing still joins it onto the trusted base — it is not
        # over-rejected as an absolute URL.
        request = _fetch(_json_response(_TOKEN_BODY), _json_response({"id": "x"}))
        result = await paginate_teams_graph(
            "me/drive/root:/Reports/jan.csv:/content", _opts("p", fetch=request)
        )
        assert _url(request.await_args_list[1]) == (
            "https://graph.microsoft.com/v1.0/me/drive/root:/Reports/jan.csv:/content"
        )
        assert _headers(request.await_args_list[1])["authorization"] == "Bearer graph-token"
        assert result == {"id": "x"}

    def test_is_trusted_graph_url_allowlist(self) -> None:
        assert is_trusted_graph_url("https://graph.microsoft.com/v1.0/next") is True
        # Wrong scheme, lookalike suffix, foreign host, and parse junk all fail.
        assert is_trusted_graph_url("http://graph.microsoft.com/v1.0/next") is False
        assert is_trusted_graph_url("https://graph.microsoft.com.attacker.example/x") is False
        assert is_trusted_graph_url("https://evil.example.com/x") is False
        assert is_trusted_graph_url("://nonsense") is False


class TestGraphImportBoundary:
    """Port of upstream ``graph/boundary.test.ts``.

    Upstream's boundary test is a **static source-scan**: it reads every
    non-test ``.ts`` in the ``graph/`` directory and asserts the source never
    imports the full adapter (``"chat"``), the shared runtime, or
    ``@microsoft/teams.apps``. We port that source-scan over the ``graph/``
    package's ``.py`` files. The cross-subpath import from
    ``chat_sdk.adapters.teams.api`` is *expected* and allowed (it mirrors
    upstream's ``import ... from "../api/client"``); only the high-level
    adapter / SDK imports are forbidden.
    """

    def test_graph_source_does_not_import_the_adapter_sdk_or_runtime(self) -> None:
        import chat_sdk.adapters.teams.graph as graph_pkg

        package_dir = Path(graph_pkg.__file__).parent
        sources = [
            path.read_text(encoding="utf-8")
            for path in sorted(package_dir.glob("*.py"))
            if not path.name.startswith("test_")
        ]
        assert sources, "expected at least one graph source file"
        joined = "\n".join(sources)

        # No Teams SDK import in any form.
        assert "import microsoft_teams" not in joined
        assert "from microsoft_teams" not in joined
        # No high-level adapter / shared-runtime imports (the api subpath is OK).
        assert "from chat_sdk.adapters.teams.adapter" not in joined
        assert "import chat_sdk.adapters.teams.adapter" not in joined
        assert "from chat_sdk.adapters.teams.bridge" not in joined
        assert "from chat_sdk.adapters.teams.cards" not in joined
        # No eager HTTP-client import (httpx is lazily imported inside the
        # default fetch only — inherited from the api subpath).
        for source in sources:
            assert "\nimport httpx" not in source, "httpx must be lazily imported"
            assert "\nimport aiohttp" not in source

    def test_importing_graph_does_not_eagerly_import_an_http_client(self) -> None:
        """Importing the graph subpath in a fresh interpreter must not load an
        HTTP client — the default fetch (from the api subpath) imports ``httpx``
        lazily.
        """
        code = (
            "import sys\n"
            "import chat_sdk.adapters.teams.graph\n"
            "forbidden = ['microsoft_teams', 'httpx', 'aiohttp']\n"
            "loaded = [name for name in forbidden if name in sys.modules]\n"
            "assert not loaded, f'graph subpath eagerly imported: {loaded}'\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def _opts(kind: str, *, fetch: AsyncMock, **fields: Any) -> Any:
    """Construct the right options dataclass for ``kind`` with ``CREDENTIALS``.

    Keeps each test call terse while threading the shared credentials and the
    injected fetch through the typed options objects. ``kind`` names the call
    site exactly (no overloading), so each maps to one options class.
    """
    from chat_sdk.adapters.teams.graph import (
        GetTeamsChannelMessageOptions,
        GetTeamsChannelOptions,
        ListTeamsChannelMessagesOptions,
        ListTeamsChatMessagesOptions,
        ListTeamsMessageRepliesOptions,
        TeamsGraphOptions,
    )

    table = {
        "chat": ListTeamsChatMessagesOptions,
        "channel_messages": ListTeamsChannelMessagesOptions,
        "replies": ListTeamsMessageRepliesOptions,
        "message": GetTeamsChannelMessageOptions,
        "get_channel": GetTeamsChannelOptions,
        "paginate": TeamsGraphOptions,
        "p": TeamsGraphOptions,
    }
    factory = table[kind]
    return factory(credentials=CREDENTIALS, fetch=fetch, **fields)
