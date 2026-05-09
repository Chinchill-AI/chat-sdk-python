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
        # Tightened: error message must mention ``current_token_async`` so
        # callers know the right async accessor to use, not just that "the
        # resolver hasn't run". Substring check on "current_token_async"
        # is escaped via ``re.escape`` so the underscore isn't treated as a
        # regex token (it isn't, but be explicit).
        with pytest.raises(AuthenticationError, match=r"current_token_async"):
            _ = adapter.current_token

    @pytest.mark.asyncio
    async def test_resolver_returning_empty_string_raises(self):
        def resolver() -> str:
            return ""

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(AuthenticationError, match="empty or non-string"):
            await adapter.current_token_async()

    @pytest.mark.asyncio
    async def test_resolver_returning_none_raises(self):
        """Non-string ``None`` return must be rejected, not silently used."""

        def resolver() -> Any:
            return None

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(AuthenticationError, match="empty or non-string"):
            await adapter.current_token_async()

    @pytest.mark.asyncio
    async def test_resolver_returning_int_raises(self):
        """Non-string ``int`` (e.g. accidental ``return 0``) must be rejected."""

        def resolver() -> Any:
            return 12345

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        with pytest.raises(AuthenticationError, match="empty or non-string"):
            await adapter.current_token_async()

    @pytest.mark.asyncio
    async def test_resolver_returning_dict_raises(self):
        """Non-string ``dict`` (e.g. returning the secret-manager response object) must be rejected."""

        def resolver() -> Any:
            return {"token": "xoxb-buried-in-dict"}

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

    @pytest.mark.asyncio
    async def test_async_resolver_exception_is_logged_and_propagated(self):
        """Async resolver exceptions raise during ``await``, not at call time.

        What to fix if this fails: in ``_resolve_default_token``
        (``adapters/slack/adapter.py``), make sure the ``await result`` is
        inside the ``try`` block alongside ``provider()`` — otherwise async
        resolver failures bypass the logger and the rotation-safety
        invariants documented in the PR.
        """
        log_calls: list[tuple[str, dict[str, object]]] = []

        async def resolver() -> str:
            raise RuntimeError("async fetch failed")

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        adapter._logger.error = lambda msg, ctx=None: log_calls.append((msg, ctx or {}))  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="async fetch failed"):
            await adapter.current_token_async()

        assert any(msg == "Bot token resolver raised" for msg, _ in log_calls), (
            "_resolve_default_token must log async resolver failures via "
            "self._logger.error('Bot token resolver raised'); "
            "ensure 'await result' is inside the try block"
        )

    @pytest.mark.asyncio
    async def test_url_verification_bypasses_broken_resolver(self):
        """A broken bot-token resolver must not break Slack's URL verification.

        URL verification is a one-time setup ping at app-install / event-
        subscription time and only needs the ``challenge`` echo back. No API
        call (and thus no token) is required.

        What to fix if this fails: in
        ``src/chat_sdk/adapters/slack/adapter.py`` ``handle_webhook``, the
        ``url_verification`` short-circuit must run BEFORE
        ``_resolve_default_token()``. Otherwise a flaky/down secret-manager
        keeps Slack from re-subscribing the webhook, which blocks app
        installation. Mirrors upstream where ``getToken`` is only called at
        per-API-call sites, never at webhook entry.
        """

        def resolver() -> str:
            raise RuntimeError("secret manager is down")

        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="s", bot_token=resolver))
        body = json.dumps({"type": "url_verification", "challenge": "abc-123"})
        response = await adapter.handle_webhook(_signed_request(body, "s"))

        assert response["status"] == 200, (
            "URL verification must succeed even when the bot-token resolver is broken; "
            "the resolver call must be deferred until after the url_verification short-circuit"
        )
        assert json.loads(response["body"]) == {"challenge": "abc-123"}


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
        """For real event payloads, the resolver is invoked at handle_webhook
        entry so downstream sync ``_get_token`` callers see the resolved
        value.

        Note: url_verification is now special-cased and short-circuits
        BEFORE the resolver runs (see test_url_verification_bypasses_broken_resolver
        above) — so this test uses a regular ``event_callback`` payload to
        exercise the resolver-before-dispatch invariant.
        """
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
        # Use an event_callback (a real Slack event) — this is the path that
        # actually needs a token in dispatch. URL verification doesn't.
        body = json.dumps({
            "type": "event_callback",
            "team_id": "T123",
            "event": {"type": "app_mention", "user": "U1", "channel": "C1", "ts": "1.0", "text": "hi"},
        })
        response = await adapter.handle_webhook(_signed_request(body))
        # event_callback returns 200 even if no handlers fire.
        assert response["status"] == 200
        assert calls == [1], "resolver must be invoked at handle_webhook entry for real events"

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
        primitive has not been swapped out. The regex requires an actual
        ``hmac.compare_digest(`` call — a passing mention in a comment or
        docstring (e.g. ``# use hmac.compare_digest, never ==``) does not
        satisfy the assertion.
        """
        import inspect as _inspect
        import re as _re

        src = _inspect.getsource(SlackAdapter._verify_signature)
        assert _re.search(r"\bhmac\.compare_digest\s*\(", src), (
            "default signature verifier must call hmac.compare_digest(...) for constant-time comparison"
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


# ---------------------------------------------------------------------------
# Rotation safety: schedule_message().cancel() and Attachment.fetch_data
#
# These two paths build a closure that runs *after* the originating webhook
# context has unwound. The contract is:
#
# - Single-workspace mode: re-resolve the token at call time so a dynamic
#   ``bot_token`` resolver picks up rotated tokens (Slack rotation TTL is 12h
#   and a scheduled message / queued attachment can outlive its origin token).
# - Multi-workspace mode: snapshot the per-team token from the request
#   context at construction time — the per-team InstallationStore lookup
#   already happened at webhook entry, and ``cancel`` / ``fetch_data`` may
#   run outside any ContextVar frame.
# ---------------------------------------------------------------------------


def _install_mock_slack_client(adapter: SlackAdapter) -> dict[str, Any]:
    """Patch ``adapter._get_client`` to return a recording mock.

    Returns a dict containing ``calls`` (list of (method, kwargs, token)
    tuples) and ``tokens`` (set of tokens the adapter requested clients for).
    """
    record: dict[str, Any] = {"calls": [], "tokens": []}

    class _Client:
        def __init__(self, token: str) -> None:
            self._token = token

        async def chat_scheduleMessage(self, **kwargs: Any) -> dict[str, Any]:
            record["calls"].append(("chat_scheduleMessage", kwargs, self._token))
            return {"scheduled_message_id": "Q-test", "ok": True}

        async def chat_deleteScheduledMessage(self, **kwargs: Any) -> dict[str, Any]:
            record["calls"].append(("chat_deleteScheduledMessage", kwargs, self._token))
            return {"ok": True}

    def _get_client(token: str | None = None) -> Any:
        resolved = token if token is not None else adapter._get_token()
        record["tokens"].append(resolved)
        return _Client(resolved)

    adapter._get_client = _get_client  # type: ignore[method-assign]
    return record


class TestScheduleMessageCancelRotationSafety:
    @pytest.mark.asyncio
    async def test_schedule_message_cancel_re_resolves_token_in_single_workspace_mode(self):
        """In single-workspace mode, ``cancel()`` invokes the resolver again.

        The contract: a dynamic ``bot_token`` resolver must pick up rotated
        tokens between ``schedule_message()`` and ``cancel()``. We assert
        the resolver is called twice and that ``cancel()`` reaches Slack
        with the *new* token.
        """
        from datetime import datetime, timedelta, timezone

        tokens = ["xoxb-old", "xoxb-new"]
        i = [0]

        def resolver() -> str:
            t = tokens[i[0]]
            i[0] += 1
            return t

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-signing-secret",
                bot_token=resolver,
            )
        )
        record = _install_mock_slack_client(adapter)

        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        scheduled = await adapter.schedule_message("slack:C123:1234567890.000000", "hello", future)

        # First call to resolver happened during schedule_message itself.
        schedule_call = next(c for c in record["calls"] if c[0] == "chat_scheduleMessage")
        assert schedule_call[2] == "xoxb-old"

        await scheduled.cancel()

        # Cancel must have re-invoked the resolver and used the rotated token.
        assert i[0] == 2, f"resolver should be invoked twice (schedule + cancel), got {i[0]}"
        cancel_call = next(c for c in record["calls"] if c[0] == "chat_deleteScheduledMessage")
        assert cancel_call[2] == "xoxb-new", (
            "cancel() in single-workspace mode must re-resolve the token so rotated "
            "credentials are picked up; got the original 'xoxb-old' instead"
        )

    @pytest.mark.asyncio
    async def test_schedule_message_cancel_uses_snapshot_in_multi_workspace_mode(self):
        """In multi-workspace mode, ``cancel()`` uses the snapshotted per-team token.

        Rationale: ``cancel()`` may run outside any ContextVar frame (e.g.
        from a cron job) — there's no per-team request context to consult.
        We assert that ``cancel()`` does NOT call the InstallationStore a
        second time, and uses the snapshot captured at ``schedule_message``
        time.
        """
        from datetime import datetime, timedelta, timezone

        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(chat)

        await adapter.set_installation(
            "T_MWS",
            SlackInstallation(bot_token="xoxb-team-mws", bot_user_id="U_MWS", team_name="MWS"),
        )

        record = _install_mock_slack_client(adapter)

        # Spy on get_installation to assert ``cancel()`` does not re-consult it.
        get_install_calls: list[str] = []
        original_get = adapter.get_installation

        async def get_install_spy(team_id: str) -> Any:
            get_install_calls.append(team_id)
            return await original_get(team_id)

        adapter.get_installation = get_install_spy  # type: ignore[method-assign]

        # Run schedule_message with the per-team token in context.
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        async def do_schedule() -> Any:
            return await adapter.schedule_message("slack:C123:1234567890.000000", "hi", future)

        scheduled = await adapter.with_bot_token_async("xoxb-team-mws", do_schedule)

        schedule_call = next(c for c in record["calls"] if c[0] == "chat_scheduleMessage")
        assert schedule_call[2] == "xoxb-team-mws"

        # Now run cancel OUTSIDE the request context — snapshot must be used.
        get_install_calls.clear()
        await scheduled.cancel()

        cancel_call = next(c for c in record["calls"] if c[0] == "chat_deleteScheduledMessage")
        assert cancel_call[2] == "xoxb-team-mws", (
            "cancel() in multi-workspace mode must use the snapshotted ctx_token, "
            "not re-resolve via the (absent) request context"
        )
        assert get_install_calls == [], (
            f"cancel() must NOT re-consult InstallationStore in multi-workspace mode; got {get_install_calls!r}"
        )


class TestAttachmentFetchDataRotationSafety:
    def _make_file(self) -> dict[str, Any]:
        return {
            "url_private": "https://files.slack.com/img.png",
            "mimetype": "image/png",
            "name": "img.png",
            "size": 100,
        }

    @pytest.mark.asyncio
    async def test_attachment_fetch_data_re_resolves_token_in_single_workspace_mode(self):
        """``fetch_data`` re-invokes the resolver in single-workspace mode.

        Same rotation contract as ``schedule_message().cancel()`` — a
        queued message can outlive the bot token that minted it.
        """
        tokens = ["xoxb-att-old", "xoxb-att-new"]
        i = [0]

        def resolver() -> str:
            t = tokens[i[0]]
            i[0] += 1
            return t

        adapter = SlackAdapter(
            SlackAdapterConfig(
                signing_secret="test-signing-secret",
                bot_token=resolver,
            )
        )

        # Stub _fetch_slack_file so we can assert which token it received.
        fetched: list[tuple[str, str]] = []

        async def fake_fetch(url: str, token: str) -> bytes:
            fetched.append((url, token))
            return b"bytes"

        adapter._fetch_slack_file = fake_fetch  # type: ignore[method-assign]

        # Build the attachment OUTSIDE any request context (single-workspace
        # mode: no ctx token to snapshot — the closure must defer to the
        # resolver at call time).
        attachment = adapter._create_attachment(self._make_file())
        assert attachment.fetch_data is not None
        assert i[0] == 0, "resolver must NOT be invoked at attachment-creation time"

        result = await attachment.fetch_data()
        assert result == b"bytes"
        assert i[0] == 1, "resolver must be invoked once at fetch_data() time"
        assert fetched == [("https://files.slack.com/img.png", "xoxb-att-old")]

        # Second fetch picks up the rotated token.
        result2 = await attachment.fetch_data()
        assert result2 == b"bytes"
        assert i[0] == 2, "resolver must be invoked again on a second fetch (rotation)"
        assert fetched[-1] == ("https://files.slack.com/img.png", "xoxb-att-new")

    @pytest.mark.asyncio
    async def test_attachment_fetch_data_uses_snapshot_in_multi_workspace_mode(self):
        """``fetch_data`` uses the snapshotted per-team token in multi-workspace mode.

        The closure must NOT consult the InstallationStore at fetch time —
        the per-team token was already captured into the closure when the
        webhook was being processed.
        """
        state = _make_mock_state()
        chat = _make_mock_chat(state)
        adapter = SlackAdapter(SlackAdapterConfig(signing_secret="test-signing-secret"))
        await adapter.initialize(chat)

        await adapter.set_installation(
            "T_ATT",
            SlackInstallation(bot_token="xoxb-att-team", bot_user_id="U_ATT", team_name="ATT"),
        )

        fetched: list[tuple[str, str]] = []

        async def fake_fetch(url: str, token: str) -> bytes:
            fetched.append((url, token))
            return b"bytes"

        adapter._fetch_slack_file = fake_fetch  # type: ignore[method-assign]

        # Spy on get_installation — fetch_data must not consult it.
        get_install_calls: list[str] = []
        original_get = adapter.get_installation

        async def get_install_spy(team_id: str) -> Any:
            get_install_calls.append(team_id)
            return await original_get(team_id)

        adapter.get_installation = get_install_spy  # type: ignore[method-assign]

        # Build the attachment INSIDE the per-team request context so the
        # snapshot captures the team token.
        async def build() -> Any:
            return adapter._create_attachment(self._make_file(), team_id="T_ATT")

        attachment = await adapter.with_bot_token_async("xoxb-att-team", build)
        assert attachment.fetch_data is not None

        # Now invoke fetch_data OUTSIDE any request context.
        get_install_calls.clear()
        result = await attachment.fetch_data()
        assert result == b"bytes"
        assert fetched == [("https://files.slack.com/img.png", "xoxb-att-team")], (
            "fetch_data in multi-workspace mode must use the snapshotted ctx_token captured at attachment-creation time"
        )
        assert get_install_calls == [], (
            "fetch_data must NOT re-consult InstallationStore at fetch time in "
            f"multi-workspace mode; got {get_install_calls!r}"
        )
