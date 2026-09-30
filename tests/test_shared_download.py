"""Tests for the shared guarded attachment downloader.

Ports upstream ``packages/adapter-shared/src/download.test.ts``
(``describe("guarded attachment downloads")``, chat@4.41.1). The TS
``transport`` returning a Node ``IncomingMessage`` maps to ``FakeTransport``
returning ``FakeResponse`` objects; the TS ``AbortSignal`` has no Python
counterpart (the deadline cancels the transport call instead). The DNS
``query`` is injected, so nothing here touches the network; the one test
that uses the default resolver resolves ``localhost``.

Python-specific additions at the bottom cover WHATWG-style host
canonicalization, the static-mapping credential stripping and Brotli
divergences, the aiohttp-backed default transport, outer cancellation and
the lazy aiohttp import.
"""

from __future__ import annotations

import asyncio
import gzip
import importlib
import socket
import sys
import time
import tracemalloc
import types
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from chat_sdk.shared import download as download_module
from chat_sdk.shared.download import (
    AttachmentResponse,
    ResolvedAddress,
    create_resolver,
    create_transport,
    download_attachment,
    is_blocked_address,
    read_attachment_body,
    validate_attachment_url,
)
from chat_sdk.shared.errors import NetworkError

INTERNAL = "Refusing to fetch an internal attachment URL"
UNTRUSTED = "Refusing to fetch an untrusted attachment URL"
TIMED_OUT = "Timed out fetching the attachment"


class FakeResponse:
    """In-memory ``AttachmentResponse``; records every ``close()``."""

    def __init__(
        self,
        body: bytes | str = b"",
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        reason: str | None = "OK",
        *,
        chunks: list[bytes] | None = None,
        hang: bool = False,
    ) -> None:
        payload = body.encode() if isinstance(body, str) else body
        self.status = status
        self.reason = reason
        self.headers: Mapping[str, str] = dict(headers or {})
        self._chunks = chunks if chunks is not None else [payload]
        self._hang = hang
        self.close_calls = 0
        self.closed = asyncio.Event()
        self.reading = asyncio.Event()
        self.read_cancelled = False

    async def _iterate(self) -> AsyncIterator[bytes]:
        self.reading.set()
        if self._hang:
            # A body that never ends: awaits a future nothing resolves.
            try:
                await asyncio.get_running_loop().create_future()
            except asyncio.CancelledError:
                self.read_cancelled = True
                raise
        for chunk in self._chunks:
            yield chunk

    def iter_chunks(self) -> AsyncIterator[bytes]:
        return self._iterate()

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class FakeTransport:
    """Returns the queued responses in order (the last one repeats)."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, headers: dict[str, str]) -> AttachmentResponse:
        self.calls.append((url, dict(headers)))
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]


def redirect(location: str | None, status: int = 302) -> FakeResponse:
    return FakeResponse("", status, {"location": location} if location is not None else {})


# ---------------------------------------------------------------------------
# Ported from download.test.ts
# ---------------------------------------------------------------------------


class TestGuardedAttachmentDownloads:
    @pytest.mark.parametrize(
        "host",
        ["files.example.com", "contoso.sharepoint.com", "CONTOSO.SHAREPOINT.COM", "cdn.example.net:8443"],
    )
    def test_accepts_public_https_host(self, host: str) -> None:
        assert validate_attachment_url(f"https://{host}/file", "test") == f"https://{host.lower()}/file"

    @pytest.mark.parametrize(
        "url",
        ["http://files.example.com/file", "ftp://files.example.com/file", "file:///etc/passwd"],
    )
    def test_rejects_nonhttps_url(self, url: str) -> None:
        with pytest.raises(NetworkError, match=UNTRUSTED):
            validate_attachment_url(url, "test")

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1/file",
            "https://2130706433/file",
            "https://[::1]/file",
            "https://169.254.169.254/file",
            "https://10.0.0.1/file",
        ],
    )
    def test_rejects_internal_file_url_with_a_network_error(self, url: str) -> None:
        with pytest.raises(NetworkError, match=INTERNAL) as info:
            validate_attachment_url(url, "test")
        assert info.value.adapter == "test"

    async def test_returns_the_validated_dns_results_to_the_socket(self) -> None:
        addresses = [
            ResolvedAddress("93.184.216.34", 4),
            ResolvedAddress("2606:2800:220:1:248:1893:25c8:1946", 6),
        ]
        calls: list[tuple[str, int]] = []

        async def query(hostname: str, family: int) -> list[ResolvedAddress]:
            calls.append((hostname, family))
            return addresses

        guarded = create_resolver("test", query)

        assert await guarded("files.example.com") == addresses
        assert calls == [("files.example.com", socket.AF_UNSPEC)]

    async def test_rejects_mixed_public_and_internal_dns_results(self) -> None:
        async def query(hostname: str, family: int) -> list[ResolvedAddress]:
            return [ResolvedAddress("93.184.216.34", 4), ResolvedAddress("10.0.0.1", 4)]

        with pytest.raises(NetworkError, match=INTERNAL):
            await create_resolver("test", query)("files.example.com")

    async def test_reports_an_empty_dns_result_as_a_resolution_failure_not_a_refusal(self) -> None:
        async def query(hostname: str, family: int) -> list[ResolvedAddress]:
            return []

        with pytest.raises(NetworkError, match="Could not resolve the attachment host"):
            await create_resolver("test", query)("gone.example.com")

    async def test_rejects_hostnames_that_resolve_to_internal_addresses(self) -> None:
        # Default (system) resolver: localhost resolves without the network.
        with pytest.raises(NetworkError, match=INTERNAL):
            await create_resolver("test")("localhost")

    @pytest.mark.parametrize(
        "url",
        ["https://fbsbx.com/file", "https://cdn.fbsbx.com/file", "https://SContent.XX.FBCDN.NET/file"],
    )
    def test_accepts_allowlisted_host_url(self, url: str) -> None:
        assert validate_attachment_url(url, "test", ["fbsbx.com", "FBCDN.net"]) == url.lower()

    @pytest.mark.parametrize(
        "url",
        ["https://example.com/file", "https://fbsbx.com.attacker.example/file", "https://cdn.fbsbx.com./file"],
    )
    def test_rejects_offallowlist_url(self, url: str) -> None:
        with pytest.raises(NetworkError, match=UNTRUSTED):
            validate_attachment_url(url, "test", ["fbsbx.com", "fbcdn.net"])

    async def test_applies_the_host_allowlist_to_redirect_targets(self) -> None:
        transport = FakeTransport(redirect("https://example.com/file"))

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await download_attachment(
                "https://cdn.fbsbx.com/file", adapter="test", hosts=["fbsbx.com"], transport=transport
            )
        assert len(transport.calls) == 1

    async def test_follows_redirects_between_allowlisted_hosts(self) -> None:
        transport = FakeTransport(redirect("https://scontent.xx.fbcdn.net/file"), FakeResponse("media"))

        result = await download_attachment(
            "https://lookaside.fbsbx.com/file", adapter="test", hosts=["fbsbx.com", "fbcdn.net"], transport=transport
        )

        assert result == b"media"

    async def test_resolves_headers_per_hop_and_drops_credentials_on_redirects(self) -> None:
        transport = FakeTransport(redirect("https://cdn.example.net/file"), FakeResponse("file contents"))

        def headers(url: str) -> dict[str, str] | None:
            return {"authorization": "Bearer secret"} if url.startswith("https://files.example.com/") else None

        result = await download_attachment(
            "https://files.example.com/file", adapter="test", headers=headers, transport=transport
        )

        assert result == b"file contents"
        first_headers, second_headers = (call[1] for call in transport.calls)
        assert first_headers["authorization"] == "Bearer secret"
        assert first_headers["user-agent"] == "Vercel.ChatSDK"
        assert "authorization" not in second_headers
        assert second_headers["user-agent"] == "Vercel.ChatSDK"

    async def test_rejects_responses_that_fail_the_onresponse_check(self) -> None:
        response = FakeResponse("<html>sign in</html>", 200, {"content-type": "text/html"})

        def on_response(message: AttachmentResponse) -> None:
            if "text/html" in (message.headers.get("content-type") or ""):
                raise NetworkError("test", "Unexpected HTML response")

        with pytest.raises(NetworkError, match="Unexpected HTML response"):
            await download_attachment(
                "https://files.example.com/file",
                adapter="test",
                on_response=on_response,
                transport=FakeTransport(response),
            )
        assert response.close_calls >= 1

    async def test_rejects_redirects_to_internal_addresses(self) -> None:
        transport = FakeTransport(redirect("https://169.254.169.254/latest/meta-data"))

        with pytest.raises(NetworkError, match=INTERNAL):
            await download_attachment("https://contoso.sharepoint.com/file", adapter="test", transport=transport)
        assert len(transport.calls) == 1

    async def test_follows_redirects_to_other_public_https_hosts(self) -> None:
        transport = FakeTransport(redirect("https://cdn.example.net/file"), FakeResponse("file contents"))

        result = await download_attachment("https://contoso.sharepoint.com/file", adapter="test", transport=transport)

        assert result == b"file contents"
        last_url, last_headers = transport.calls[-1]
        assert last_url == "https://cdn.example.net/file"
        assert last_headers["user-agent"] == "Vercel.ChatSDK"

    async def test_rejects_redirect_chains_past_the_redirect_limit(self) -> None:
        transport = FakeTransport(redirect("https://cdn.example.net/file"))

        with pytest.raises(NetworkError, match="Too many attachment redirects"):
            await download_attachment(
                "https://files.example.com/file", adapter="test", redirects=1, transport=transport
            )
        assert len(transport.calls) == 2

    async def test_rejects_redirects_without_a_location_header(self) -> None:
        with pytest.raises(NetworkError, match="Attachment redirect has no location"):
            await download_attachment(
                "https://files.example.com/file", adapter="test", transport=FakeTransport(redirect(None))
            )

    async def test_rejects_error_statuses(self) -> None:
        transport = FakeTransport(FakeResponse("", 404, {}, "Not Found"))

        with pytest.raises(NetworkError) as info:
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)
        assert str(info.value) == "Failed to fetch file: 404 Not Found"

    async def test_times_out_slow_downloads_with_a_distinct_error(self) -> None:
        async def transport(url: str, headers: dict[str, str]) -> AttachmentResponse:
            # Honors cancellation: waits on a future nothing resolves.
            return await asyncio.get_running_loop().create_future()

        with pytest.raises(NetworkError) as info:
            await download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=20, transport=transport
            )
        assert str(info.value) == TIMED_OUT

    async def test_times_out_a_transport_that_never_responds_and_ignores_the_signal(self) -> None:
        gate: asyncio.Future[FakeResponse] = asyncio.get_running_loop().create_future()

        async def transport(url: str, headers: dict[str, str]) -> AttachmentResponse:
            while True:
                try:
                    return await asyncio.shield(gate)
                except asyncio.CancelledError:
                    continue  # ignores cancellation, like a transport ignoring the signal

        with pytest.raises(NetworkError, match=TIMED_OUT):
            await download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=20, transport=transport
            )

        # A response that arrives after the deadline is closed, not leaked.
        late = FakeResponse("late")
        gate.set_result(late)
        await asyncio.wait_for(late.closed.wait(), timeout=1)
        assert late.close_calls == 1

    async def test_times_out_a_body_read_that_the_transport_does_not_tie_to_the_signal(self) -> None:
        hanging = FakeResponse(hang=True)

        with pytest.raises(NetworkError, match=TIMED_OUT):
            await download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=20, transport=FakeTransport(hanging)
            )
        assert hanging.close_calls >= 1

    async def test_decodes_gzip_response_bodies(self) -> None:
        transport = FakeTransport(FakeResponse(gzip.compress(b"file contents"), 200, {"content-encoding": "gzip"}))

        assert await download_attachment("https://files.example.com/file", adapter="test", transport=transport) == (
            b"file contents"
        )

    async def test_applies_the_download_limit_to_decompressed_bytes(self) -> None:
        transport = FakeTransport(FakeResponse(gzip.compress(bytes(64 * 1024)), 200, {"content-encoding": "gzip"}))

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await download_attachment("https://files.example.com/file", adapter="test", limit=1024, transport=transport)

    async def test_rejects_unsupported_content_encodings(self) -> None:
        transport = FakeTransport(FakeResponse("payload", 200, {"content-encoding": "zstd"}))

        with pytest.raises(NetworkError, match="Unsupported attachment encoding: zstd"):
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)

    async def test_stops_reading_attachments_at_the_download_limit(self) -> None:
        message = FakeResponse(chunks=[b"abc", b"def"])

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await read_attachment_body(message, "test", 5)
        assert message.close_calls == 1

    async def test_rejects_declared_sizes_over_the_limit_before_reading(self) -> None:
        read: list[bytes] = []

        class Declared(FakeResponse):
            async def _iterate(self) -> AsyncIterator[bytes]:
                read.append(b"abcdef")
                yield b"abcdef"

        message = Declared(headers={"content-length": "10"})

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await read_attachment_body(message, "test", 5)
        assert read == []

    async def test_reads_declaredsize_bodies_into_a_single_buffer(self) -> None:
        message = FakeResponse(headers={"content-length": "6"}, chunks=[b"abc", b"def"])

        assert await read_attachment_body(message, "test") == b"abcdef"

    async def test_rejects_bodies_that_exceed_their_declared_length(self) -> None:
        message = FakeResponse(headers={"content-length": "4"}, chunks=[b"abcdef"])

        with pytest.raises(NetworkError, match="Attachment body exceeds its declared length"):
            await read_attachment_body(message, "test")


# ---------------------------------------------------------------------------
# Python-specific
# ---------------------------------------------------------------------------


class TestHostCanonicalization:
    """``urllib.parse`` does not canonicalize hosts like WHATWG ``URL``."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://0x7f.1/file",
            "https://0177.0.0.1/file",
            "https://127.1/file",
            "https://127.0.0.1./file",
            "https://%31%32%37.0.0.1/file",
            "https://１２７.０.０.１/file",
            "https://127。0。0。1/file",
            "https://[::ffff:127.0.0.1]/file",
            "https://[fe80::1]/file",
            "http://127.0.0.1/file",
        ],
    )
    def test_numeric_and_encoded_internal_hosts_are_refused(self, url: str) -> None:
        with pytest.raises(NetworkError, match=INTERNAL):
            validate_attachment_url(url, "test")

    @pytest.mark.parametrize(
        "url",
        [
            "https://1.2.3.4.5/file",
            "https://256.1.1.1/file",
            "https://09.1.1.1/file",
            "https://[::1/file",
            "https:///file",
            "https://a%2fb.example.com/file",
            "https://evil.example\\@files.example.com/file",
        ],
    )
    def test_unparseable_or_ambiguous_hosts_are_untrusted(self, url: str) -> None:
        with pytest.raises(NetworkError, match=UNTRUSTED):
            validate_attachment_url(url, "test")

    def test_public_numeric_host_is_rewritten_to_its_canonical_address(self) -> None:
        assert validate_attachment_url("https://1572395042/f?x=1", "test") == "https://93.184.216.34/f?x=1"

    def test_path_and_query_are_percent_encoded_keeping_existing_escapes(self) -> None:
        url = 'https://files.example.com/a b/%2Fé?download=a%2Fb&x="y"#frag'

        assert validate_attachment_url(url, "test") == (
            "https://files.example.com/a%20b/%2F%C3%A9?download=a%2Fb&x=%22y%22"
        )

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://bücher.example/x", "https://xn--bcher-kva.example/x"),
            # Non-transitional UTS46 (WHATWG, yarl): not IDNA 2003's fass.de.
            ("https://faß.de/file", "https://xn--fa-hia.de/file"),
            # Case mapping is UTS46's, applied to the raw host (matches Node).
            ("https://FAẞ.de/file", "https://xn--fa-hia.de/file"),
            ("https://BÜCHER.Example/x", "https://xn--bcher-kva.example/x"),
        ],
    )
    def test_unicode_host_is_converted_to_ascii(self, url: str, expected: str) -> None:
        assert validate_attachment_url(url, "test") == expected

    def test_unicode_host_is_untrusted_without_the_idna_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "idna", None)

        with pytest.raises(NetworkError, match=UNTRUSTED):
            validate_attachment_url("https://faß.de/file", "test")
        assert validate_attachment_url("https://files.example.com/x", "test") == "https://files.example.com/x"

    @pytest.mark.parametrize(
        ("address", "blocked"),
        [
            ("93.184.216.34", False),
            ("100.64.0.1", True),
            ("198.19.255.255", True),
            ("2606:4700::1", False),
            ("2001:db8::1", True),
            ("3fff::1", True),
            ("not an ip", True),
        ],
    )
    def test_is_blocked_address_uses_upstream_ranges(self, address: str, blocked: bool) -> None:
        assert is_blocked_address(address) is blocked

    async def test_invalid_redirect_location_is_untrusted(self) -> None:
        transport = FakeTransport(redirect("https://[::1/file"))

        with pytest.raises(NetworkError, match=UNTRUSTED):
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)
        assert len(transport.calls) == 1


class TestStaticHeaderCredentials:
    """Divergence: a static mapping's credentials stay on the first origin."""

    async def test_static_mapping_credentials_are_dropped_on_cross_origin_hop(self) -> None:
        transport = FakeTransport(redirect("https://cdn.example.net/file"), FakeResponse("ok"))

        await download_attachment(
            "https://files.example.com/file",
            adapter="test",
            headers={"Authorization": "Bearer secret", "Cookie": "a=b", "x-trace": "1"},
            transport=transport,
        )

        first, second = (call[1] for call in transport.calls)
        assert first["Authorization"] == "Bearer secret"
        assert first["Cookie"] == "a=b"
        assert {k.lower() for k in second} == {"accept-encoding", "user-agent", "x-trace"}

    async def test_static_mapping_credentials_follow_same_origin_hop(self) -> None:
        transport = FakeTransport(redirect("/other"), FakeResponse("ok"))

        await download_attachment(
            "https://files.example.com/file",
            adapter="test",
            headers={"authorization": "Bearer secret"},
            transport=transport,
        )

        assert transport.calls[1][0] == "https://files.example.com/other"
        assert transport.calls[1][1]["authorization"] == "Bearer secret"

    async def test_caller_headers_replace_defaults_case_insensitively(self) -> None:
        transport = FakeTransport(FakeResponse("ok"))

        await download_attachment(
            "https://files.example.com/file", adapter="test", headers={"User-Agent": "custom"}, transport=transport
        )

        sent = transport.calls[0][1]
        assert sent["User-Agent"] == "custom"
        assert "user-agent" not in sent


class TestBrotli:
    """Divergence: ``br`` is advertised and decoded only with Brotli >= 1.2."""

    async def test_br_is_unsupported_without_brotli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brotli", None)
        transport = FakeTransport(FakeResponse("payload", 200, {"content-encoding": "br"}))

        with pytest.raises(NetworkError, match="Unsupported attachment encoding: br"):
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)
        assert transport.calls[0][1]["accept-encoding"] == "gzip, deflate"

    async def test_br_is_unsupported_with_a_brotli_that_cannot_bound_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Decompressor:  # Brotli < 1.2: no output_buffer_limit support
            def process(self, data: bytes) -> bytes:
                return data

        old = types.ModuleType("brotli")
        old.Decompressor = Decompressor  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "brotli", old)
        transport = FakeTransport(FakeResponse("payload", 200, {"content-encoding": "br"}))

        with pytest.raises(NetworkError, match="Unsupported attachment encoding: br"):
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)
        assert transport.calls[0][1]["accept-encoding"] == "gzip, deflate"

    @pytest.mark.parametrize("chunk_size", [5, 4096, 1 << 20])
    async def test_br_bodies_are_decoded_with_real_brotli(self, chunk_size: int) -> None:
        brotli = pytest.importorskip("brotli")
        payload = bytes(range(256)) * 400 + b"x" * 100_000  # buffered output past one step
        body = brotli.compress(payload)
        chunks = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
        transport = FakeTransport(FakeResponse(headers={"content-encoding": "br"}, chunks=chunks))

        result = await download_attachment("https://files.example.com/file", adapter="test", transport=transport)

        assert result == payload
        assert transport.calls[0][1]["accept-encoding"] == "gzip, deflate, br"

    async def test_br_download_limit_applies_to_decompressed_bytes(self) -> None:
        brotli = pytest.importorskip("brotli")
        body = brotli.compress(bytes(10 * 1024 * 1024))  # tiny on the wire
        transport = FakeTransport(FakeResponse(body, 200, {"content-encoding": "br"}))

        with pytest.raises(NetworkError, match="Attachment exceeds the download limit"):
            await download_attachment("https://files.example.com/file", adapter="test", limit=1024, transport=transport)

    async def test_truncated_br_body_is_an_error(self) -> None:
        brotli = pytest.importorskip("brotli")
        body = brotli.compress(bytes(range(256)) * 400)
        message = FakeResponse(headers={"content-encoding": "br"}, chunks=[body[: len(body) // 2]])

        with pytest.raises(brotli.error, match="unexpected end of file"):
            await read_attachment_body(message, "test")


class TestBodyDecoding:
    async def test_concatenated_gzip_members_decode_as_one_body(self) -> None:
        body = gzip.compress(b"first ") + gzip.compress(b"second")
        message = FakeResponse(headers={"content-encoding": "gzip"}, chunks=[body[:7], body[7:]])

        assert await read_attachment_body(message, "test") == b"first second"

    async def test_trailing_bytes_after_a_gzip_member_are_ignored(self) -> None:
        body = gzip.compress(b"payload") + b"\x00\x00garbage"
        message = FakeResponse(headers={"content-encoding": "gzip"}, chunks=[body, b"more"])

        assert await read_attachment_body(message, "test") == b"payload"

    async def test_truncated_gzip_body_is_an_error(self) -> None:
        body = gzip.compress(b"file contents" * 100)
        message = FakeResponse(headers={"content-encoding": "gzip"}, chunks=[body[: len(body) // 2]])

        with pytest.raises(Exception, match="unexpected end of file"):
            await read_attachment_body(message, "test")

    async def test_memory_tracks_decoded_size_not_gzip_member_count(self) -> None:
        member = gzip.compress(b"x")
        body = member * 50_000  # ~1 MB on the wire, 50 KB decoded
        chunks = [body[i : i + 65536] for i in range(0, len(body), 65536)]
        message = FakeResponse(headers={"content-encoding": "gzip"}, chunks=chunks)

        tracemalloc.start()
        try:
            result = await read_attachment_body(message, "test")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert result == b"x" * 50_000
        # One object per member (the naive approach) peaks well above 1 MB.
        assert peak < 1024 * 1024

    async def test_deflate_bodies_are_decoded(self) -> None:
        import zlib

        message = FakeResponse(headers={"content-encoding": "deflate"}, chunks=[zlib.compress(b"deflated")])

        assert await read_attachment_body(message, "test") == b"deflated"


class TestDeadlineAndCancellation:
    async def test_outer_cancellation_propagates_and_closes_the_response(self) -> None:
        hanging = FakeResponse(hang=True)
        task = asyncio.create_task(
            download_attachment("https://files.example.com/file", adapter="test", transport=FakeTransport(hanging))
        )
        await asyncio.wait_for(hanging.reading.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert hanging.close_calls >= 1
        # The body read runs in its own task; it is cancelled, not orphaned.
        assert hanging.read_cancelled is True

    async def test_outer_cancellation_is_not_held_up_by_a_hanging_async_close(self) -> None:
        class SlowClose(FakeResponse):
            def close(self) -> asyncio.Future[None]:
                self.close_calls += 1
                return asyncio.get_running_loop().create_future()  # never completes

        hanging = SlowClose(hang=True)
        task = asyncio.create_task(
            download_attachment("https://files.example.com/file", adapter="test", transport=FakeTransport(hanging))
        )
        await asyncio.wait_for(hanging.reading.wait(), timeout=1)

        task.cancel()
        # Default 30 s deadline: cleanup must not wait it out after a cancel.
        done, _ = await asyncio.wait({task}, timeout=1)
        assert task in done
        assert task.cancelled()
        assert hanging.close_calls >= 1

    async def test_outer_cancellation_cancels_a_pending_transport_call(self) -> None:
        started = asyncio.Event()
        cancelled: list[bool] = []

        async def transport(url: str, headers: dict[str, str]) -> AttachmentResponse:
            started.set()
            try:
                return await asyncio.get_running_loop().create_future()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        task = asyncio.create_task(
            download_attachment("https://files.example.com/file", adapter="test", transport=transport)
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled == [True]

    async def test_a_hanging_async_close_does_not_outlive_the_deadline(self) -> None:
        class SlowClose(FakeResponse):
            def close(self) -> asyncio.Future[None]:
                self.close_calls += 1
                return asyncio.get_running_loop().create_future()  # never completes

        hanging = SlowClose(hang=True)
        with pytest.raises(NetworkError, match=TIMED_OUT):
            await asyncio.wait_for(
                download_attachment(
                    "https://files.example.com/file", adapter="test", timeout_ms=20, transport=FakeTransport(hanging)
                ),
                timeout=2,
            )
        assert hanging.close_calls >= 1

        done = SlowClose("ok")
        result = await asyncio.wait_for(
            download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=50, transport=FakeTransport(done)
            ),
            timeout=2,
        )
        assert result == b"ok"
        assert done.close_calls == 1

    async def test_no_hop_starts_after_a_slow_redirect_close_uses_up_the_deadline(self) -> None:
        class SlowClose(FakeResponse):
            def close(self) -> asyncio.Future[None]:
                self.close_calls += 1
                return asyncio.get_running_loop().create_future()  # never completes

        responses = [SlowClose("", 302, {"location": "https://cdn.example.net/file"}), FakeResponse("too late")]
        calls: list[str] = []

        def transport(url: str, headers: dict[str, str]) -> asyncio.Future[AttachmentResponse]:
            # Synchronous transport: does its work when called, never suspends.
            calls.append(url)
            done: asyncio.Future[AttachmentResponse] = asyncio.get_running_loop().create_future()
            done.set_result(responses.pop(0))
            return done

        with pytest.raises(NetworkError, match=TIMED_OUT):
            await download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=20, transport=transport
            )
        assert calls == ["https://files.example.com/file"]

    async def test_a_body_read_completing_after_the_deadline_is_refused(self) -> None:
        class BlockingBody(FakeResponse):
            async def _iterate(self) -> AsyncIterator[bytes]:
                time.sleep(0.05)  # blocking work past the 20 ms deadline, no suspension
                yield b"too late"

        late = BlockingBody()

        with pytest.raises(NetworkError, match=TIMED_OUT):
            await download_attachment(
                "https://files.example.com/file", adapter="test", timeout_ms=20, transport=FakeTransport(late)
            )
        assert late.close_calls >= 1

    async def test_transport_errors_propagate_unchanged(self) -> None:
        async def transport(url: str, headers: dict[str, str]) -> AttachmentResponse:
            raise ConnectionResetError("reset")

        with pytest.raises(ConnectionResetError, match="reset"):
            await download_attachment("https://files.example.com/file", adapter="test", transport=transport)


class TestDefaultTransport:
    async def test_default_transport_refuses_a_hostname_resolving_internally(self) -> None:
        # End-to-end through aiohttp: the pinned resolver refuses localhost
        # before any socket is opened.
        with pytest.raises(NetworkError, match=INTERNAL):
            await download_attachment("https://localhost/file", adapter="test", timeout_ms=5_000)

    async def test_default_transport_resolves_idn_hosts_by_their_ascii_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        async def query(hostname: str, family: int) -> list[ResolvedAddress]:
            seen.append(hostname)
            return [ResolvedAddress("10.0.0.1", 4)]

        monkeypatch.setattr(download_module, "_system_query", query)

        # Reaching the (patched) resolver proves the host check accepted the
        # punycode host; the internal answer then stops the download.
        with pytest.raises(NetworkError, match=INTERNAL):
            await download_attachment("https://bücher.example/x", adapter="test", timeout_ms=5_000)
        assert seen == ["xn--bcher-kva.example"]

    async def test_default_transport_sends_the_validated_url_unmodified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import aiohttp

        seen: list[tuple[Any, dict[str, Any]]] = []

        async def fake_get(self: Any, url: Any, **kwargs: Any) -> Any:
            seen.append((url, kwargs))
            raise ConnectionResetError("stop")

        monkeypatch.setattr(aiohttp.ClientSession, "get", fake_get)

        with pytest.raises(ConnectionResetError):
            await download_attachment("https://files.example.com/a%2Fb?sig=a%2Fb%3D&x=1", adapter="test")

        url, kwargs = seen[0]
        # Escapes are sent byte-for-byte, so signed URLs stay valid.
        assert str(url) == "https://files.example.com/a%2Fb?sig=a%2Fb%3D&x=1"
        assert url.raw_query_string == "sig=a%2Fb%3D&x=1"
        assert kwargs["allow_redirects"] is False

    async def test_pinned_resolver_hands_aiohttp_only_vetted_addresses(self) -> None:
        async def query(hostname: str, family: int) -> list[ResolvedAddress]:
            return [ResolvedAddress("93.184.216.34", 4)]

        resolver = download_module._pinned_resolver_class()(create_resolver("test", query))

        assert await resolver.resolve("files.example.com", 443) == [
            {
                "hostname": "files.example.com",
                "host": "93.184.216.34",
                "port": 443,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
            }
        ]

    async def test_default_transport_rechecks_the_client_parsed_host(self) -> None:
        send = create_transport("test")

        with pytest.raises(NetworkError, match=INTERNAL):
            await send("https://[::1]/file", {})


def test_module_imports_without_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "aiohttp", None)
    monkeypatch.delitem(sys.modules, "chat_sdk.shared.download")
    fresh: Any = importlib.import_module("chat_sdk.shared.download")

    assert asyncio.run(
        fresh.download_attachment(
            "https://files.example.com/f", adapter="test", transport=FakeTransport(FakeResponse("ok"))
        )
    ) == (b"ok")
    with pytest.raises(ImportError, match="aiohttp is required"):
        fresh.create_transport("test")
