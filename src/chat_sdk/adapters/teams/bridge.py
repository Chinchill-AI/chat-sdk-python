"""Bridge between framework-agnostic webhooks and the Microsoft Teams SDK.

Python port of ``packages/adapter-teams/src/bridge-adapter.ts`` (synced to
upstream ``adapter-teams@chat@4.30.0``).

The :class:`BridgeHttpAdapter` implements the Teams SDK's
:class:`~microsoft_teams.apps.http.adapter.HttpServerAdapter` protocol. Rather
than owning an HTTP server (the SDK's default ``FastAPIAdapter`` does), it
captures the single route handler the ``App`` registers during
``app.initialize()`` and exposes :meth:`dispatch` for the adapter's
``handle_webhook`` to call. This keeps the adapter serverless / framework
agnostic: the consumer's web framework calls ``handle_webhook`` →
``bridge.dispatch`` → the SDK route handler (which performs JWT validation and
activity routing) → back out as a plain ``{body, status, headers}`` dict.

It also owns the per-activity :class:`~chat_sdk.types.WebhookOptions` map so each
inbound event handler can recover the ``WebhookOptions`` (e.g. ``wait_until``)
that belong to *its* activity without sharing mutable state across concurrent
webhooks. Options are keyed by the activity ``id`` for the duration of a single
dispatch and removed in the ``finally`` block.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal, cast

from chat_sdk.logger import Logger
from chat_sdk.types import WebhookOptions

if TYPE_CHECKING:
    # Reuse the SDK's own ``HttpRouteHandler`` protocol so ``register_route``
    # matches the ``HttpServerAdapter`` interface signature exactly.
    from microsoft_teams.apps.http.adapter import HttpRouteHandler


class BridgeHttpAdapter:
    """Virtual ``HttpServerAdapter`` that captures the SDK route handler.

    Implements the Teams SDK ``HttpServerAdapter`` protocol
    (``register_route`` / ``serve_static`` / ``start`` / ``stop``). Only
    ``register_route`` does real work — it stashes the handler the ``App``
    registers for the messaging endpoint so :meth:`dispatch` can invoke it.

    Mirrors upstream ``BridgeHttpAdapter`` in
    ``packages/adapter-teams/src/bridge-adapter.ts``.
    """

    def __init__(self, logger: Logger) -> None:
        self._handler: HttpRouteHandler | None = None
        self._webhook_options: dict[str, WebhookOptions] = {}
        self._logger = logger

    # ------------------------------------------------------------------
    # HttpServerAdapter protocol
    # ------------------------------------------------------------------
    def register_route(self, method: Literal["POST"], path: str, handler: HttpRouteHandler) -> None:
        """Capture the route handler registered by ``app.initialize()``.

        The SDK registers exactly one ``POST`` route (the messaging
        endpoint). We ignore the method/path and keep the handler — the
        consumer's web framework decides which URL maps to
        ``handle_webhook``. Parameter names mirror the SDK's
        ``HttpServerAdapter`` protocol for keyword compatibility.
        """
        del method, path
        self._handler = handler

    def serve_static(self, path: str, directory: str) -> None:
        """No-op — the bridge never serves static assets (tabs/pages)."""
        del path, directory

    async def start(self, port: int) -> None:
        """The bridge does not own a server lifecycle; starting is a no-op.

        Unlike the SDK's default adapter (which raises here), the Teams
        adapter intentionally never calls ``app.start()`` — it only uses
        ``app.initialize()`` for proactive messaging + route capture, and
        drives inbound traffic through :meth:`dispatch`. Returning quietly
        keeps that contract explicit.
        """

    async def stop(self) -> None:
        """No-op — nothing to tear down (we never started a server)."""

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    async def dispatch(self, request: Any, options: WebhookOptions | None = None) -> dict[str, Any]:
        """Dispatch a framework-agnostic webhook request through the SDK.

        Extracts the raw body + headers from ``request`` (duck-typed across
        web frameworks), parses the JSON activity, records ``options`` keyed
        by the activity ``id``, then invokes the captured SDK route handler.
        The SDK handler performs JWT validation and activity routing; its
        ``{status, body}`` result is translated back into the
        ``{body, status, headers}`` dict our consumers expect.
        """
        body = await self._read_body(request)
        self._logger.debug("Teams webhook raw body", {"body": body[:500] if body else ""})

        try:
            parsed_body: Any = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError) as exc:
            self._logger.error("Failed to parse request body", {"error": str(exc)})
            return _make_response("Invalid JSON", 400, content_type="text/plain")

        if not isinstance(parsed_body, dict):
            self._logger.error("Teams webhook body is not a JSON object")
            return _make_response("Invalid JSON", 400, content_type="text/plain")

        if self._handler is None:
            self._logger.error("No SDK route handler registered (app not initialized?)")
            return _make_response(
                json.dumps({"error": "No handler registered"}),
                500,
                content_type="application/json",
            )

        headers = self._read_headers(request)

        activity_id = parsed_body.get("id")
        if activity_id and options is not None:
            self._webhook_options[activity_id] = options

        try:
            server_response = await self._handler({"body": parsed_body, "headers": headers})
            status = server_response.get("status", 200)
            response_body = server_response.get("body")
            if response_body is not None:
                return _make_response(
                    json.dumps(response_body),
                    status,
                    content_type="application/json",
                )
            return _make_response("", status, content_type=None)
        except Exception as error:  # pragma: no cover - defensive parity with upstream
            self._logger.error("Bridge adapter dispatch error", {"error": str(error)})
            return _make_response(
                json.dumps({"error": "Internal error"}),
                500,
                content_type="application/json",
            )
        finally:
            if activity_id:
                self._webhook_options.pop(activity_id, None)

    def get_webhook_options(self, activity_id: str | None) -> WebhookOptions | None:
        """Recover the ``WebhookOptions`` recorded for ``activity_id``.

        Called by each inbound event handler so the chat-processing call
        uses the options (e.g. ``wait_until``) that belong to its own
        activity. Returns ``None`` when there is no id or no recorded
        options (e.g. a fire-and-forget webhook with no options passed).
        """
        if not activity_id:
            return None
        return self._webhook_options.get(activity_id)

    # ------------------------------------------------------------------
    # Request introspection (framework-agnostic)
    # ------------------------------------------------------------------
    @staticmethod
    async def _read_body(request: Any) -> str:
        """Extract the request body as a string across web frameworks.

        Handles ``request.text`` (callable or attribute, sync or async),
        ``request.body`` (callable/awaitable/stream), and ``request.data``.
        Mirrors the duck-typing the adapter relied on before the migration so
        existing consumers and test doubles keep working.
        """
        text_attr = getattr(request, "text", None)
        if text_attr is not None:
            if callable(text_attr):
                result = text_attr()
                text_attr = await result if inspect.isawaitable(result) else result
            if isinstance(text_attr, (bytes, bytearray)):
                return text_attr.decode("utf-8")
            return str(text_attr)

        body = getattr(request, "body", None)
        if body is not None:
            if callable(body):
                body = body()
            if inspect.isawaitable(body):
                body = await body
            if hasattr(body, "read"):
                raw_result = body.read()
                raw = await raw_result if inspect.isawaitable(raw_result) else raw_result
                return raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            return body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)

        data = getattr(request, "data", None)
        if data is not None:
            return data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else str(data)
        return ""

    @staticmethod
    def _read_headers(request: Any) -> dict[str, str]:
        """Extract request headers as a plain ``dict[str, str]``.

        The SDK's JWT validator reads the ``Authorization`` header from this
        dict, so we normalise whatever header container the framework exposes
        (``dict``, ``Mapping``, or an iterable of pairs) into a plain dict.
        """
        headers = getattr(request, "headers", None)
        if headers is None:
            return {}
        if isinstance(headers, dict):
            return {str(k): str(v) for k, v in headers.items()}
        # Starlette/aiohttp-style multidicts and ``email.message`` headers all
        # support ``.items()``; fall back to that before giving up.
        items = getattr(headers, "items", None)
        if callable(items):
            try:
                pairs = cast("Iterable[tuple[Any, Any]]", items())
            except Exception:
                return {}
            return {str(key): str(value) for key, value in pairs}
        return {}


def _make_response(body: str, status: int, *, content_type: str | None) -> dict[str, Any]:
    """Build the framework-agnostic response dict our consumers expect."""
    response_headers: dict[str, str] = {}
    if content_type is not None:
        response_headers["Content-Type"] = content_type
    return {"body": body, "status": status, "headers": response_headers}
