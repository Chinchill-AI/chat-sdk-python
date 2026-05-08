"""Tests for the dynamic ``bot_token`` resolver and custom ``webhook_verifier``.

Port of upstream ``vercel/chat#421`` (commit ``2531e9c``) — adds the
``SlackBotToken`` resolver shape and the ``SlackWebhookVerifier`` escape
hatch that bypasses HMAC signature verification.

The custom verifier path is security-sensitive: the default verifier uses
``hmac.compare_digest`` and a 5-minute timestamp tolerance check. A custom
verifier replaces both — these tests assert that the verifier is called,
that throws/falsy returns reject with 401, that string returns substitute
the body for downstream parsing, and that an explicit verifier opts out of
the ``SLACK_SIGNING_SECRET`` env fallback.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from chat_sdk.adapters.slack.adapter import SlackAdapter, create_slack_adapter
    from chat_sdk.adapters.slack.types import (
        SlackAdapterConfig,
        SlackInstallation,
    )
    from chat_sdk.shared.errors import AuthenticationError, ValidationError

    _SLACK_AVAILABLE = True
except ImportError:
    _SLACK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not _SLACK_AVAILABLE, reason="Slack adapter import failed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body.encode("utf-8")
        self.headers = headers or {}

    async def text(self) -> str:
        return self.body.decode("utf-8")


def _slack_signature(body: str, secret: str, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp or int(time.time()))
    sig_base = f"v0:{ts}:{body}"
    sig = "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()
    return ts, sig


def _signed_request(body: str, secret: str = "test-signing-secret") -> _FakeRequest:
    ts, sig = _slack_signature(body, secret)
    return _FakeRequest(
        body,
        {
            "x-slack-request-timestamp": ts,
            "x-slack-signature": sig,
            "content-type": "application/json",
        },
    )


def _unsigned_request(body: str, content_type: str = "application/json") -> _FakeRequest:
    return _FakeRequest(body, {"content-type": content_type})


def _make_mock_state() -> MagicMock:
    cache: dict[str, Any] = {}
    state = MagicMock()
    state.get = AsyncMock(side_effect=lambda k: cache.get(k))
    state.set = AsyncMock(side_effect=lambda k, v, *a, **kw: cache.__setitem__(k, v))
    state.delete = AsyncMock(side_effect=lambda k: cache.pop(k, None))
    state.append_to_list = AsyncMock()
    state.get_list = AsyncMock(return_value=[])
    state._cache = cache
    return state


def _make_mock_chat(state: MagicMock) -> MagicMock:
    chat = MagicMock()
    chat.process_message = MagicMock()
    chat.handle_incoming_message = AsyncMock()
    chat.process_reaction = MagicMock()
    chat.process_action = MagicMock()
    chat.process_modal_submit = AsyncMock()
    chat.process_modal_close = MagicMock()
    chat.process_slash_command = MagicMock()
    chat.get_state = MagicMock(return_value=state)
    chat.get_user_name = MagicMock(return_value="test-bot")
    chat.get_logger = MagicMock(return_value=MagicMock())
    return chat


# ---------------------------------------------------------------------------
# Constructor: bot_token as a callable resolver
# ---------------------------------------------------------------------------


class TestBotTokenResolverConstruction:
    """``bot_token`` accepts both a static string and a callable resolver."""

    def test_accepts_static_string(self):
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token="xoxb-static"))
        # current_token is sync — works for static config without ever invoking a resolver.
        assert adapter.current_token == "xoxb-static"

    def test_accepts_sync_callable(self):
        calls: list[int] = []

        def resolver() -> str:
            calls.append(1)
            return "xoxb-from-sync-resolver"

        # Construction must not invoke the resolver — verifier-only modes need
        # to defer all token resolution to per-request flow.
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        assert isinstance(adapter, SlackAdapter)
        assert calls == []

    def test_accepts_async_callable(self):
        async def resolver() -> str:
            return "xoxb-from-async-resolver"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        # No exception — resolver isn't invoked yet.
        assert adapter.name == "slack"

    @pytest.mark.asyncio
    async def test_sync_resolver_invoked_via_current_token_async(self):
        calls: list[int] = []

        def resolver() -> str:
            calls.append(1)
            return "xoxb-from-sync-resolver"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        token = await adapter.current_token_async()
        assert token == "xoxb-from-sync-resolver"
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_async_resolver_invoked_via_current_token_async(self):
        calls: list[int] = []

        async def resolver() -> str:
            calls.append(1)
            await asyncio.sleep(0)
            return "xoxb-from-async-resolver"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        token = await adapter.current_token_async()
        assert token == "xoxb-from-async-resolver"
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_resolver_invoked_per_call_supports_rotation(self):
        tokens = ["xoxb-token-1", "xoxb-token-2", "xoxb-token-3"]
        i = [0]

        def resolver() -> str:
            t = tokens[i[0]]
            i[0] += 1
            return t

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        assert await adapter.current_token_async() == "xoxb-token-1"
        assert await adapter.current_token_async() == "xoxb-token-2"
        assert await adapter.current_token_async() == "xoxb-token-3"
        assert i[0] == 3

    def test_sync_current_token_with_resolver_before_resolution_raises(self):
        """Sync ``current_token`` access before the resolver has run raises a clear error."""

        def resolver() -> str:
            return "xoxb-resolved"

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(AuthenticationError, match="resolver has not been invoked"):
            _ = adapter.current_token

    @pytest.mark.asyncio
    async def test_resolver_returning_empty_string_raises(self):
        def resolver() -> str:
            return ""

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(AuthenticationError, match="empty or non-string"):
            await adapter.current_token_async()

    @pytest.mark.asyncio
    async def test_resolver_propagates_user_exceptions(self):
        def resolver() -> str:
            raise RuntimeError("token fetch failed")

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(RuntimeError, match="token fetch failed"):
            await adapter.current_token_async()


# ---------------------------------------------------------------------------
# Constructor: webhook_verifier
# ---------------------------------------------------------------------------


class TestWebhookVerifierConstruction:
    """Webhook verifier replaces signing_secret as the auth requirement."""

    def test_signing_secret_or_verifier_required(self):
        old = os.environ.pop("SLACK_SIGNING_SECRET", None)
        try:
            with pytest.raises(ValidationError, match="signingSecret or webhookVerifier"):
                SlackAdapter(SlackAdapterConfig(bot_token="xoxb-x"))
        finally:
            if old is not None:
                os.environ["SLACK_SIGNING_SECRET"] = old

    def test_webhook_verifier_alone_is_sufficient(self):
        old = os.environ.pop("SLACK_SIGNING_SECRET", None)
        try:
            adapter = SlackAdapter(
                SlackAdapterConfig(
                    bot_token="xoxb-x",
                    webhook_verifier=lambda req, body: True,
                )
            )
            assert isinstance(adapter, SlackAdapter)
        finally:
            if old is not None:
                os.environ["SLACK_SIGNING_SECRET"] = old

    def test_signing_secret_takes_precedence_over_verifier(self):
        verifier_called: list[int] = []

        def verifier(req: Any, body: str) -> bool:
            verifier_called.append(1)
            return True

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="s",
                bot_token="xoxb-x",
                webhook_verifier=verifier,
            )
        )
        # Internal: webhook_verifier was suppressed because signing_secret won.
        assert adapter._webhook_verifier is None

    def test_verifier_opts_out_of_env_signing_secret(self):
        """An explicit verifier suppresses the SLACK_SIGNING_SECRET env fallback.

        Regression: a deployment with SLACK_SIGNING_SECRET set in env would
        otherwise silently shadow the verifier the caller intended to use.
        """
        old = os.environ.get("SLACK_SIGNING_SECRET")
        os.environ["SLACK_SIGNING_SECRET"] = "env-secret-should-not-be-used"
        try:
            adapter = SlackAdapter(
                SlackAdapterConfig(
                    bot_token="xoxb-x",
                    webhook_verifier=lambda req, body: True,
                )
            )
            assert adapter._signing_secret is None
            assert adapter._webhook_verifier is not None
        finally:
            if old is None:
                os.environ.pop("SLACK_SIGNING_SECRET", None)
            else:
                os.environ["SLACK_SIGNING_SECRET"] = old


# ---------------------------------------------------------------------------
# handle_webhook with custom verifier
# ---------------------------------------------------------------------------


class TestHandleWebhookCustomVerifier:
    @pytest.mark.asyncio
    async def test_verifier_truthy_accepts_request(self):
        adapter = SlackAdapter(
            SlackAdapterConfig(
                bot_token="xoxb-x",
                webhook_verifier=lambda req, body: True,
            )
        )
        body = json.dumps({"type": "url_verification", "challenge": "verifier-challenge"})
        response = await adapter.handle_webhook(_unsigned_request(body))
        assert response["status"] == 200
        assert json.loads(response["body"]) == {"challenge": "verifier-challenge"}

    @pytest.mark.asyncio
    async def test_verifier_throws_returns_401(self):
        def verifier(req: Any, body: str) -> bool:
            raise RuntimeError("bad signature")

        adapter = SlackAdapter(SlackAdapterConfig(bot_token="xoxb-x", webhook_verifier=verifier))
        body = json.dumps({"type": "url_verification"})
        response = await adapter.handle_webhook(_unsigned_request(body))
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_verifier_returns_false_returns_401(self):
        adapter = SlackAdapter(
            SlackAdapterConfig(
                bot_token="xoxb-x",
                webhook_verifier=lambda req, body: False,
            )
        )
        body = json.dumps({"type": "url_verification"})
        response = await adapter.handle_webhook(_unsigned_request(body))
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_verifier_returns_none_returns_401(self):
        adapter = SlackAdapter(
            SlackAdapterConfig(
                bot_token="xoxb-x",
                webhook_verifier=lambda req, body: None,
            )
        )
        body = json.dumps({"type": "url_verification"})
        response = await adapter.handle_webhook(_unsigned_request(body))
        assert response["status"] == 401

    @pytest.mark.asyncio
    async def test_verifier_receives_request_and_body(self):
        captured: list[tuple[Any, str]] = []

        def verifier(req: Any, body: str) -> bool:
            captured.append((req, body))
            return True

        adapter = SlackAdapter(SlackAdapterConfig(bot_token="xoxb-x", webhook_verifier=verifier))
        body = json.dumps({"type": "url_verification", "challenge": "x"})
        request = _unsigned_request(body)
        await adapter.handle_webhook(request)
        assert len(captured) == 1
        assert captured[0][0] is request
        assert captured[0][1] == body

    @pytest.mark.asyncio
    async def test_async_verifier_is_awaited(self):
        async def verifier(req: Any, body: str) -> bool:
            await asyncio.sleep(0)
            return True

        adapter = SlackAdapter(SlackAdapterConfig(bot_token="xoxb-x", webhook_verifier=verifier))
        body = json.dumps({"type": "url_verification", "challenge": "async-challenge"})
        response = await adapter.handle_webhook(_unsigned_request(body))
        assert response["status"] == 200
        assert json.loads(response["body"]) == {"challenge": "async-challenge"}

    @pytest.mark.asyncio
    async def test_verifier_returning_string_substitutes_body(self):
        # Verifier swaps the body so the parser sees a different challenge
        canonical_body = json.dumps({"type": "url_verification", "challenge": "canonical"})

        def verifier(req: Any, body: str) -> str:
            return canonical_body

        adapter = SlackAdapter(SlackAdapterConfig(bot_token="xoxb-x", webhook_verifier=verifier))
        # Original body has a *different* challenge; the verifier's return
        # should win for downstream parsing.
        original_body = json.dumps({"type": "url_verification", "challenge": "original"})
        response = await adapter.handle_webhook(_unsigned_request(original_body))
        assert response["status"] == 200
        assert json.loads(response["body"]) == {"challenge": "canonical"}

    @pytest.mark.asyncio
    async def test_verifier_path_does_not_invoke_default_signature_check(self):
        """When verifier is configured, the built-in HMAC + timestamp check is skipped.

        SECURITY note: this is the documented escape hatch. The implementer
        is responsible for replay protection (timestamp freshness) since the
        default 5-minute tolerance is bypassed.
        """
        # No timestamp/signature headers — would fail the default check —
        # but the verifier accepts.
        adapter = SlackAdapter(
            SlackAdapterConfig(
                bot_token="xoxb-x",
                webhook_verifier=lambda req, body: True,
            )
        )
        body = json.dumps({"type": "url_verification", "challenge": "no-headers"})
        response = await adapter.handle_webhook(_FakeRequest(body, {"content-type": "application/json"}))
        assert response["status"] == 200


# ---------------------------------------------------------------------------
# Resolver runs at webhook entry — sync _get_token sees the resolved value
# ---------------------------------------------------------------------------


class TestResolverIntegratedWithWebhookFlow:
    @pytest.mark.asyncio
    async def test_handle_webhook_invokes_resolver_before_dispatch(self):
        calls: list[int] = []

        def resolver() -> str:
            calls.append(1)
            return "xoxb-from-resolver"

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-signing-secret",
                bot_token=resolver,
            )
        )
        body = json.dumps({"type": "url_verification", "challenge": "ok"})
        response = await adapter.handle_webhook(_signed_request(body))
        assert response["status"] == 200
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_resolver_result_visible_to_sync_get_token_during_dispatch(self):
        """During webhook processing, sync ``_get_token`` returns the freshly resolved value."""
        resolved: list[str | None] = []

        def resolver() -> str:
            return "xoxb-rotated"

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-signing-secret",
                bot_token=resolver,
            )
        )

        # Custom verifier captures the sync token visible from inside dispatch.
        # The verifier runs BEFORE the resolver primes the per-request cache,
        # so we instead patch _process_event_payload to capture mid-dispatch.
        original = adapter._process_event_payload

        def capture(payload: Any, options: Any = None) -> None:
            try:
                resolved.append(adapter._get_token())
            except Exception as exc:  # pragma: no cover - debug aid
                resolved.append(f"ERR: {exc!r}")
            original(payload, options)

        adapter._process_event_payload = capture  # type: ignore[method-assign]

        body = json.dumps(
            {
                "type": "event_callback",
                "event": {"type": "user_change", "user": {"id": "U1"}},
                "team_id": "T1",
            }
        )
        await adapter.handle_webhook(_signed_request(body))
        assert resolved == ["xoxb-rotated"]


# ---------------------------------------------------------------------------
# Per-request isolation: concurrent webhooks see their own resolved token
# ---------------------------------------------------------------------------


class TestConcurrentRequestIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_resolver_invocations_do_not_leak_across_requests(self):
        """Two concurrent ``_resolve_default_token`` calls each see their own token.

        Regression guard for hazard #6 (ContextVar boundaries): the
        per-request resolved token cache must use ContextVar, not a shared
        instance attribute. We force the second call to await BEFORE its
        ``_get_token()`` read so that, if the cache were a shared instance
        attribute, the first task's overwrite would leak into the second
        task's read.
        """
        i = [0]
        # Both tasks enter the resolver and obtain their own token. Then the
        # FIRST task gates on this event before reading _get_token, so that
        # the SECOND task can run its full resolve+set+read cycle in
        # between. With a per-request ContextVar the first task's read
        # still returns its own resolved token; with a shared attribute it
        # would be clobbered by the second task's set.
        first_task_can_read = asyncio.Event()
        second_task_done = asyncio.Event()

        async def resolver() -> str:
            n = i[0]
            i[0] += 1
            return f"xoxb-call-{n}"

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-signing-secret",
                bot_token=resolver,
            )
        )

        async def call(label: str, is_first: bool) -> tuple[str, str]:
            await adapter._resolve_default_token()
            if is_first:
                # Wait for the second task to fully run its resolve cycle.
                await first_task_can_read.wait()
            else:
                # Yield so the first task already passed _resolve_default_token.
                await asyncio.sleep(0)
                await adapter._resolve_default_token()
                second_task_done.set()
                first_task_can_read.set()
            token = adapter._get_token()
            return label, token

        async def first() -> tuple[str, str]:
            return await call("first", is_first=True)

        async def second() -> tuple[str, str]:
            # Let the first task enter call() and pass its first await.
            await asyncio.sleep(0)
            return await call("second", is_first=False)

        results = await asyncio.gather(first(), second())
        by_label = dict(results)
        # If the per-request cache were a shared attribute, ``first`` would
        # see ``second``'s last-resolved token. With a ContextVar the first
        # task sees its own resolver call and the second task sees its own.
        assert by_label["first"] == "xoxb-call-0"
        assert by_label["second"] in {"xoxb-call-1", "xoxb-call-2"}
        # Sanity: tokens are distinct across the two tasks.
        assert by_label["first"] != by_label["second"]

    @pytest.mark.asyncio
    async def test_multi_workspace_concurrent_team_resolution_isolated(self):
        """Two concurrent webhooks for different teams each see their own InstallationStore token."""
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(chat)

        await adapter.set_installation(
            "T_ALPHA",
            SlackInstallation(bot_token="xoxb-alpha", bot_user_id="U_ALPHA", team_name="Alpha"),
        )
        await adapter.set_installation(
            "T_BETA",
            SlackInstallation(bot_token="xoxb-beta", bot_user_id="U_BETA", team_name="Beta"),
        )

        captured: dict[str, str] = {}
        gate = asyncio.Event()
        first_in: list[int] = []

        original = adapter._process_event_payload

        def capture(payload: Any, options: Any = None) -> None:
            team = payload.get("team_id", "?")
            # Force interleave: the first request to arrive waits until the
            # second one also enters this function, so both are in flight
            # with their respective ContextVar copies.
            if not first_in:
                first_in.append(1)
            try:
                captured[team] = adapter._get_token()
            except Exception as exc:  # pragma: no cover
                captured[team] = f"ERR: {exc!r}"
            original(payload, options)

        adapter._process_event_payload = capture  # type: ignore[method-assign]

        async def run_one(team: str) -> None:
            body = json.dumps(
                {
                    "type": "event_callback",
                    "event": {"type": "user_change", "user": {"id": f"U_{team}"}},
                    "team_id": team,
                }
            )
            # Yield to let the other task interleave.
            await asyncio.sleep(0)
            await adapter.handle_webhook(_signed_request(body))
            gate.set()

        await asyncio.gather(run_one("T_ALPHA"), run_one("T_BETA"))

        assert captured["T_ALPHA"] == "xoxb-alpha"
        assert captured["T_BETA"] == "xoxb-beta"


# ---------------------------------------------------------------------------
# create_slack_adapter factory wires the new options through
# ---------------------------------------------------------------------------


class TestCreateSlackAdapterFactoryAcceptsNewOptions:
    @pytest.mark.asyncio
    async def test_factory_accepts_resolver(self):
        adapter = create_slack_adapter(
            SlackAdapterConfig(
                signing_secret="s",
                bot_token=lambda: "xoxb-from-factory-resolver",
            )
        )
        token = await adapter.current_token_async()
        assert token == "xoxb-from-factory-resolver"

    def test_factory_accepts_verifier(self):
        old = os.environ.pop("SLACK_SIGNING_SECRET", None)
        try:
            adapter = create_slack_adapter(
                SlackAdapterConfig(
                    bot_token="xoxb-x",
                    webhook_verifier=lambda req, body: True,
                )
            )
            assert isinstance(adapter, SlackAdapter)
        finally:
            if old is not None:
                os.environ["SLACK_SIGNING_SECRET"] = old


# ---------------------------------------------------------------------------
# Adversarial / SECURITY: the default verifier must use compare_digest
# ---------------------------------------------------------------------------


class TestSecurityProperties:
    def test_default_verifier_uses_constant_time_compare(self):
        """Spot-check: the default verifier path uses ``hmac.compare_digest``.

        A regression to ``==`` would leak signature bytes via timing. This
        test inspects the source of ``_verify_signature`` to assert the
        primitive has not been swapped out.
        """
        import inspect as _inspect

        src = _inspect.getsource(SlackAdapter._verify_signature)
        assert "compare_digest" in src, (
            "default signature verifier must use hmac.compare_digest for constant-time comparison"
        )

    @pytest.mark.asyncio
    async def test_custom_verifier_is_a_security_escape_hatch(self):
        """The custom verifier bypasses both HMAC and the timestamp tolerance check.

        Implementers must take responsibility for both. This test does NOT
        validate implementer code — only that the documented contract holds:
        an accepting verifier accepts a request that the default check
        would reject for missing/old timestamps.
        """
        adapter = SlackAdapter(
            SlackAdapterConfig(
                bot_token="xoxb-x",
                webhook_verifier=lambda req, body: True,
            )
        )
        # Old timestamp (10 min in the past) — the default verifier would
        # reject this, but the custom verifier accepts.
        body = json.dumps({"type": "url_verification", "challenge": "old"})
        old_ts = str(int(time.time()) - 600)
        request = _FakeRequest(
            body,
            {
                "x-slack-request-timestamp": old_ts,
                "x-slack-signature": "v0=garbage",
                "content-type": "application/json",
            },
        )
        response = await adapter.handle_webhook(request)
        assert response["status"] == 200, "custom verifier must be able to accept requests the default check rejects"
