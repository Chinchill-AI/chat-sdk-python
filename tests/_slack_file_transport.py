"""In-memory file transport for Slack guarded-download tests (#213).

Python stand-in for upstream ``packages/adapter-slack/src/test-fixtures.ts``
(``incomingMessage``) and the ``TransportSlackAdapter.fileTransport`` spy in
``index.test.ts``: the adapter's downloads go through the shared guarded
downloader, and this transport captures each hop's URL and resolved headers
so a test can assert which token (if any) a hop would send.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

from chat_sdk.shared.download import AttachmentResponse


class FakeFileResponse:
    """One response (upstream ``incomingMessage``); ``hang`` models a stalled body."""

    def __init__(
        self,
        body: bytes = b"file",
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        reason: str = "OK",
        hang: bool = False,
    ) -> None:
        self.status = status
        self.reason = reason
        self.headers: Mapping[str, str] = headers if headers is not None else {}
        self._body = body
        self._hang = hang
        self.closed = False

    async def _iterate(self) -> AsyncIterator[bytes]:
        if self._hang:
            await asyncio.get_running_loop().create_future()
        yield self._body

    def iter_chunks(self) -> AsyncIterator[bytes]:
        return self._iterate()

    def close(self) -> None:
        self.closed = True


class FakeFileTransport:
    """Returns queued responses in order (the last one repeats); records calls."""

    def __init__(self, *responses: FakeFileResponse) -> None:
        self._responses = list(responses) if responses else [FakeFileResponse(b"file-bytes")]
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> AttachmentResponse:
        self.calls.append((url, dict(headers)))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    @property
    def authorizations(self) -> list[str | None]:
        """The ``authorization`` header each hop carried (``None`` when absent)."""
        return [headers.get("authorization") for _, headers in self.calls]
