"""Guarded downloads for untrusted attachment URLs.

Port of upstream ``@chat-adapter/shared`` ``download.ts`` (vercel/chat
``bb926884`` #850, ``153bd964`` #856, ``b6fa24c6`` #865 and the shared slice of
``6adca361`` #916). :func:`download_attachment` fetches a URL that arrived in
a webhook payload with SSRF and resource-exhaustion protection:

- HTTPS only; internal and reserved addresses are refused both as URL
  literals and after DNS resolution (the default transport connects only to
  the addresses its resolver vetted);
- every redirect hop is re-validated (scheme, internal literal, optional host
  allowlist), and request headers are resolved per hop;
- the decoded body is capped at ``limit`` bytes;
- one deadline covers every hop and the body read, enforced by the
  downloader itself so it holds for a transport that ignores cancellation.

Adapters adopt it in their own issues; this module only provides the helper.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import re
import socket
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from typing import Any, NamedTuple, Protocol, cast
from urllib.parse import SplitResult, quote, unquote, urljoin, urlsplit

from chat_sdk.shared.errors import NetworkError

DEFAULT_LIMIT = 25 * 1024 * 1024
DEFAULT_REDIRECTS = 5
DEFAULT_TIMEOUT_MS = 30_000
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Copied exactly from upstream ``download.ts`` (``IPV4`` / ``IPV6``).
BLOCKED_IPV4_RANGES: tuple[tuple[str, int], ...] = (
    ("0.0.0.0", 8),
    ("10.0.0.0", 8),
    ("100.64.0.0", 10),
    ("127.0.0.0", 8),
    ("169.254.0.0", 16),
    ("172.16.0.0", 12),
    ("192.0.0.0", 24),
    ("192.0.2.0", 24),
    ("192.88.99.0", 24),
    ("192.168.0.0", 16),
    ("198.18.0.0", 15),
    ("198.51.100.0", 24),
    ("203.0.113.0", 24),
    ("224.0.0.0", 4),
    ("240.0.0.0", 4),
)
BLOCKED_IPV6_RANGES: tuple[tuple[str, int], ...] = (
    ("::", 3),
    ("2001::", 23),
    ("2001:db8::", 32),
    ("2002::", 16),
    ("3fff::", 20),
    ("4000::", 2),
    ("8000::", 1),
)

_BLOCKED4 = tuple(ipaddress.IPv4Network(f"{a}/{p}") for a, p in BLOCKED_IPV4_RANGES)
_BLOCKED6 = tuple(ipaddress.IPv6Network(f"{a}/{p}") for a, p in BLOCKED_IPV6_RANGES)

_USER_AGENT = "Vercel.ChatSDK"
# Headers a static ``headers`` mapping never carries to another origin.
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
# Upper bound on decoded bytes produced per decompression step, so one small
# compressed chunk cannot expand far past ``limit`` before the cap applies.
_DECODE_STEP = 64 * 1024
_READ_CHUNK = 64 * 1024

_INTERNAL = "Refusing to fetch an internal attachment URL"
_UNTRUSTED = "Refusing to fetch an untrusted attachment URL"
_TIMED_OUT = "Timed out fetching the attachment"
_OVER_LIMIT = "Attachment exceeds the download limit"

# Tasks abandoned at the deadline (a transport or body read that ignored
# cancellation). Held here so they are not garbage-collected mid-flight.
_abandoned: set[asyncio.Future[Any]] = set()


class ResolvedAddress(NamedTuple):
    """One DNS answer: ``family`` is 4 or 6 (anything else is refused)."""

    address: str
    family: int


DnsQuery = Callable[[str, int], Awaitable[Sequence[ResolvedAddress]]]
"""``(hostname, socket family) -> answers``; injectable for tests."""

GuardedResolver = Callable[..., Awaitable[list[ResolvedAddress]]]
"""``(hostname, family=AF_UNSPEC) -> vetted answers`` from :func:`create_resolver`."""


class AttachmentResponse(Protocol):
    """One HTTP response as the downloader sees it.

    ``headers`` lookups should be case-insensitive (aiohttp's are; plain
    lowercase-keyed dicts also work). ``iter_chunks`` yields the body bytes
    exactly as received, still content-encoded: the downloader decodes them
    itself so the size cap applies to decoded bytes. ``close`` releases the
    connection; it may be sync or async and must tolerate repeated calls.
    """

    @property
    def status(self) -> int: ...

    @property
    def reason(self) -> str | None: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_chunks(self) -> AsyncIterator[bytes]: ...

    def close(self) -> Awaitable[None] | None: ...


class AttachmentTransport(Protocol):
    """Issues one request (no redirect following) and returns its response.

    Receives the hop's validated URL and resolved request headers. Supply
    your own to route downloads through a proxy or custom egress; a custom
    transport skips the built-in DNS-pinned resolver, but scheme, internal
    literal and allowlist checks still run on every hop. The overall deadline
    is enforced by cancelling the call, and holds even if the transport
    ignores cancellation (upstream passes an ``AbortSignal`` instead).
    """

    def __call__(self, url: str, headers: dict[str, str]) -> Awaitable[AttachmentResponse]: ...


HeadersOption = Mapping[str, str] | Callable[[str], Mapping[str, str] | None]
OnResponse = Callable[[AttachmentResponse], Awaitable[None] | None]


# ---------------------------------------------------------------------------
# Address and URL checks
# ---------------------------------------------------------------------------


def is_blocked_address(ip: str) -> bool:
    """Return True for internal/reserved addresses, and for anything unparseable."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _BLOCKED4)
    return any(address in network for network in _BLOCKED6)


def _refusal(adapter: str) -> NetworkError:
    return NetworkError(adapter, _INTERNAL)


def _untrusted(adapter: str) -> NetworkError:
    return NetworkError(adapter, _UNTRUSTED)


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_OCT_DIGITS = frozenset("01234567")
_DEC_DIGITS = frozenset("0123456789")
_HOSTNAME = re.compile(r"[a-z0-9._-]+")
# ``quote`` always keeps ASCII letters, digits and ``_.-~``; these add the
# characters a valid URL may already carry (``%`` keeps existing escapes).
_PATH_SAFE = "!$%&'()*+,/:;=?@[]^|"
_USERINFO_SAFE = "!$%&'()*+,;=:"


def _parse_ipv4_number(part: str) -> int | None:
    """WHATWG IPv4 number parser (decimal, ``0x`` hex, leading-zero octal)."""
    if not part:
        return None
    digits, radix, allowed = part, 10, _DEC_DIGITS
    if len(part) >= 2 and part[:2] in ("0x", "0X"):
        digits, radix, allowed = part[2:], 16, _HEX_DIGITS
    elif len(part) >= 2 and part[0] == "0":
        digits, radix, allowed = part[1:], 8, _OCT_DIGITS
    if not digits:
        return 0
    if not all(ch in allowed for ch in digits):
        return None
    return int(digits, radix)


def _ends_in_number(host: str) -> bool:
    """WHATWG "ends in a number" check: such hosts must parse as IPv4."""
    parts = host.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last and all(ch in _DEC_DIGITS for ch in last):
        return True
    return _parse_ipv4_number(last) is not None


def _parse_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """WHATWG IPv4 parser; accepts ``2130706433``, ``0x7f.1``, ``127.1`` etc."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        return None
    numbers: list[int] = []
    for part in parts:
        number = _parse_ipv4_number(part)
        if number is None:
            return None
        numbers.append(number)
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return ipaddress.IPv4Address(value)


def _domain_to_ascii(host: str) -> str | None:
    """WHATWG "domain to ASCII": UTS46 non-transitional processing.

    Uses the ``idna`` package (a dependency of aiohttp/yarl, httpx and
    requests), which is also what yarl uses, so the validated host and the
    one the HTTP client contacts agree. Python's built-in ``idna`` codec is
    IDNA 2003 (``faß.de`` -> ``fass.de``) and would change the destination,
    so without the package a non-ASCII host is refused.
    """
    try:
        import idna
    except ImportError:
        return None
    try:
        return idna.encode(host, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError, ValueError):
        return None


def _canonical_host(parts: SplitResult) -> str | ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Canonicalize the URL host the way a WHATWG ``URL`` would.

    ``urllib.parse`` leaves percent-encoding, full-width characters and legacy
    numeric IPv4 notations in the host untouched, while the HTTP client (and
    WHATWG) resolves them, so the checks run on the canonical form. Returns
    an IP address object for literals, the lowercased ASCII hostname
    otherwise, or ``None`` when the host is not valid.
    """
    if not parts.hostname:
        return None
    # The raw host, before ``urlsplit``'s ``str.lower()``: UTS46 does its
    # own case mapping, which differs for a few code points (e.g. U+1E9E).
    host = parts.netloc.rpartition("@")[2]
    host = host[1 : host.index("]")] if host.startswith("[") else host.partition(":")[0]
    if "%" in host:
        try:
            host = unquote(host, errors="strict")
        except UnicodeDecodeError:
            return None
    if not host.isascii():
        host = _domain_to_ascii(host)
        if host is None:
            return None
    host = host.lower()
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host)
        except ValueError:
            return None
    if _ends_in_number(host):
        return _parse_ipv4(host)
    # Anything beyond DNS-label characters (e.g. a decoded "/" or "@") would
    # change which host the rebuilt URL names; refuse it.
    return host if _HOSTNAME.fullmatch(host) else None


def validate_attachment_url(url: str, adapter: str, hosts: Sequence[str] | None = None) -> str:
    """Validate one attachment URL (or redirect target) and return it normalized.

    Raises ``NetworkError(adapter, "Refusing to fetch an internal attachment
    URL")`` for an internal or reserved IP literal, and ``NetworkError(adapter,
    "Refusing to fetch an untrusted attachment URL")`` for a non-HTTPS URL, a
    host outside ``hosts`` (exact or subdomain match, case-insensitive), or a
    URL whose host cannot be parsed. The returned URL carries the canonical
    host, so the transport connects to exactly the host that was checked.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
        host = _canonical_host(parts)
    except ValueError:
        # WHATWG ``new URL`` throws on these; fail closed with a NetworkError.
        raise _untrusted(adapter) from None
    # A backslash, space or control character in the authority is where
    # WHATWG and ``urlsplit`` disagree about the host; fail closed.
    if host is None or any(ch == "\\" or ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in parts.netloc):
        raise _untrusted(adapter)
    if not isinstance(host, str) and is_blocked_address(str(host)):
        raise _refusal(adapter)
    hostname = host if isinstance(host, str) else str(host)
    if parts.scheme.lower() != "https" or (
        hosts is not None
        and not any(hostname == allowed.lower() or hostname.endswith(f".{allowed.lower()}") for allowed in hosts)
    ):
        raise _untrusted(adapter)
    netloc_host = f"[{hostname}]" if isinstance(host, ipaddress.IPv6Address) else hostname
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = f"{quote(userinfo, safe=_USERINFO_SAFE)}@{netloc_host}" if "@" in parts.netloc else netloc_host
    if port is not None:
        netloc = f"{netloc}:{port}"
    # Percent-encode what WHATWG would (spaces, quotes, non-ASCII, ...) while
    # keeping existing escapes byte-for-byte: the transport sends the result
    # as-is, so signatures over the escaped request target stay valid.
    path = quote(parts.path.replace("\\", "/"), safe=_PATH_SAFE)
    query = quote(parts.query, safe=_PATH_SAFE)
    return SplitResult("https", netloc, path, query, "").geturl()


# ---------------------------------------------------------------------------
# DNS
# ---------------------------------------------------------------------------


async def _system_query(hostname: str, family: int) -> list[ResolvedAddress]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, None, family=family, type=socket.SOCK_STREAM)
    answers: list[ResolvedAddress] = []
    for info_family, _type, _proto, _canon, sockaddr in infos:
        version = 4 if info_family == socket.AF_INET else 6 if info_family == socket.AF_INET6 else 0
        answer = ResolvedAddress(str(sockaddr[0]), version)
        if answer not in answers:
            answers.append(answer)
    return answers


def create_resolver(adapter: str, query: DnsQuery | None = None) -> GuardedResolver:
    """Return a resolver that refuses a host when **any** answer is internal.

    An empty answer raises ``NetworkError(adapter, "Could not resolve the
    attachment host")``; query errors propagate unchanged. ``query`` defaults
    to the system resolver (``getaddrinfo``).
    """
    lookup: DnsQuery = query if query is not None else _system_query

    async def resolve(hostname: str, family: int = socket.AF_UNSPEC) -> list[ResolvedAddress]:
        answers = list(await lookup(hostname, family))
        if not answers:
            raise NetworkError(adapter, "Could not resolve the attachment host")
        if any(answer.family not in (4, 6) or is_blocked_address(answer.address) for answer in answers):
            raise _refusal(adapter)
        return answers

    return resolve


# ---------------------------------------------------------------------------
# Default transport (aiohttp, lazily imported)
# ---------------------------------------------------------------------------


class _AiohttpResponse:
    """Adapts an aiohttp response (and the one-shot session that owns it)."""

    def __init__(self, session: Any, response: Any) -> None:
        self._session = session
        self._response = response
        self.status: int = response.status
        self.reason: str | None = response.reason
        self.headers: Mapping[str, str] = response.headers

    def iter_chunks(self) -> AsyncIterator[bytes]:
        return self._response.content.iter_chunked(_READ_CHUNK)

    async def close(self) -> None:
        self._response.close()
        await self._session.close()


def _pinned_resolver_class() -> type:
    from aiohttp.abc import AbstractResolver

    class PinnedResolver(AbstractResolver):
        """aiohttp resolver that hands the socket only vetted addresses."""

        def __init__(self, guarded: GuardedResolver) -> None:
            self._guarded = guarded

        async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC) -> list[Any]:
            answers = await self._guarded(host, family)
            return [
                {
                    "hostname": host,
                    "host": answer.address,
                    "port": port,
                    "family": socket.AF_INET if answer.family == 4 else socket.AF_INET6,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                }
                for answer in answers
            ]

        async def close(self) -> None:
            return None

    return PinnedResolver


def create_transport(adapter: str) -> AttachmentTransport:
    """The built-in transport: aiohttp with a DNS-pinned resolver.

    Each request gets its own connector (``force_close``, no DNS cache) so no
    pooled connection bypasses the resolver; redirects are not followed,
    bodies are not auto-decompressed, and proxy environment variables are
    ignored. SNI and certificate checks still use the URL hostname.
    """
    try:
        import aiohttp
        import yarl
    except ImportError as error:
        raise ImportError(
            "aiohttp is required for the default attachment transport. "
            "Install an adapter extra that includes it, or pass transport=."
        ) from error

    resolver_class = _pinned_resolver_class()
    guarded = create_resolver(adapter)

    async def send(url: str, headers: dict[str, str]) -> AttachmentResponse:
        # ``encoded=True``: send the validated URL byte-for-byte (no
        # requoting, so signed query strings survive). The HTTP client parses
        # it again; refuse if its idea of the host differs from the validated
        # one, or is an internal literal (literals never reach the resolver).
        target = yarl.URL(url, encoded=True)
        raw_host = (target.raw_host or "").lower()
        if target.scheme != "https" or not raw_host or raw_host != urlsplit(url).hostname:
            raise _untrusted(adapter)
        with contextlib.suppress(ValueError):
            if is_blocked_address(str(ipaddress.ip_address(raw_host))):
                raise _refusal(adapter)
        connector = aiohttp.TCPConnector(resolver=resolver_class(guarded), force_close=True, use_dns_cache=False)
        session = aiohttp.ClientSession(connector=connector, auto_decompress=False, trust_env=False)
        try:
            response = await session.get(target, headers=headers, allow_redirects=False)
        except BaseException:
            await session.close()
            raise
        return _AiohttpResponse(session, response)

    return send


# ---------------------------------------------------------------------------
# Body decoding
# ---------------------------------------------------------------------------


class _Decoder(Protocol):
    def feed(self, data: bytes) -> Iterator[bytes]: ...

    def finish(self) -> None: ...


class _ZlibDecoder:
    def __init__(self, wbits: int) -> None:
        self._wbits = wbits
        self._stream = zlib.decompressobj(wbits)
        self._trailing = False

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self._trailing:
            return
        pending = data
        while True:
            out = self._stream.decompress(pending, _DECODE_STEP)
            if out:
                yield out
            if self._stream.eof:
                rest = self._stream.unused_data
                if not rest:
                    return
                if self._wbits != _GZIP_WBITS or not rest.startswith(b"\x1f"):
                    # Data after the end of the stream is ignored, as Node's
                    # zlib does unless it starts another gzip member.
                    self._trailing = True
                    return
                # Concatenated gzip members decode as one body (as Node does).
                self._stream = zlib.decompressobj(self._wbits)
                pending = rest
                continue
            pending = self._stream.unconsumed_tail
            if not pending and len(out) < _DECODE_STEP:
                return

    def finish(self) -> None:
        if not self._stream.eof:
            raise zlib.error("unexpected end of file")


class _BrotliDecoder:
    def __init__(self, module: Any) -> None:
        self._stream = module.Decompressor()
        self._error = module.error

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self._stream.is_finished():
            return  # data after the end of the stream is ignored
        out = self._stream.process(data, output_buffer_limit=_DECODE_STEP)
        # Output can stay buffered even when ``can_accept_more_data()`` is
        # true, so drain with empty input until a step produces nothing.
        while out:
            yield out
            if self._stream.is_finished():
                return
            out = self._stream.process(b"", output_buffer_limit=_DECODE_STEP)

    def finish(self) -> None:
        if not self._stream.is_finished():
            raise self._error("unexpected end of file")


_GZIP_WBITS = 16 + zlib.MAX_WBITS


def _brotli_module() -> Any | None:
    """The ``brotli`` module when it can bound its output (Brotli >= 1.2)."""
    try:
        import brotli  # type: ignore[import-not-found]
    except ImportError:
        return None
    decompressor = getattr(brotli, "Decompressor", None)
    if decompressor is None or not hasattr(decompressor, "can_accept_more_data"):
        return None
    return brotli


def _decoder_for(encoding: str) -> _Decoder | None:
    if encoding in ("gzip", "x-gzip"):
        return _ZlibDecoder(_GZIP_WBITS)
    if encoding == "deflate":
        return _ZlibDecoder(zlib.MAX_WBITS)
    if encoding == "br":
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: br is decoded
        # (and advertised) only when a Brotli with bounded output is installed.
        module = _brotli_module()
        return _BrotliDecoder(module) if module is not None else None
    return None


def _accept_encoding() -> str:
    return "gzip, deflate, br" if _brotli_module() is not None else "gzip, deflate"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return None


def _declared_length(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value or not all(ch in _DEC_DIGITS for ch in value):
        return None
    return int(value)


async def _close(response: AttachmentResponse) -> None:
    result = response.close()
    if inspect.isawaitable(result):
        await result


async def read_attachment_body(response: AttachmentResponse, adapter: str, limit: int = DEFAULT_LIMIT) -> bytes:
    """Read and decode ``response``'s body, capped at ``limit`` decoded bytes.

    Closes the response when it refuses the body. :func:`download_attachment`
    applies its deadline around this call.
    """
    try:
        return await _read_body(response, adapter, limit)
    except BaseException:
        # A failing close must not mask why the body was refused.
        with contextlib.suppress(Exception):
            await _close(response)
        raise


async def _read_body(response: AttachmentResponse, adapter: str, limit: int) -> bytes:
    encoding = (_header(response.headers, "content-encoding") or "").strip().lower() or "identity"
    decoder = None if encoding == "identity" else _decoder_for(encoding)
    if encoding != "identity" and decoder is None:
        raise NetworkError(adapter, f"Unsupported attachment encoding: {encoding}")
    declared = None if decoder is not None else _declared_length(_header(response.headers, "content-length"))
    if declared is not None and declared > limit:
        raise NetworkError(adapter, _OVER_LIMIT)

    # One growing buffer: memory tracks the decoded size, not the number of
    # pieces (many tiny gzip members would otherwise cost an object each).
    body = bytearray()

    def take(piece: bytes) -> None:
        size = len(body) + len(piece)
        if declared is not None and size > declared:
            raise NetworkError(adapter, "Attachment body exceeds its declared length")
        if size > limit:
            raise NetworkError(adapter, _OVER_LIMIT)
        body.extend(piece)

    async for chunk in response.iter_chunks():
        if decoder is None:
            take(chunk)
            continue
        for piece in decoder.feed(bytes(chunk)):
            take(piece)
    if decoder is not None:
        decoder.finish()
    return bytes(body)


# ---------------------------------------------------------------------------
# Deadline
# ---------------------------------------------------------------------------


def _abandon(task: asyncio.Future[Any], late: Callable[[Any], None] | None = None) -> None:
    """Cancel ``task`` without waiting for it; clean up whatever it yields later."""
    task.cancel()
    if task.done():
        _settle(task, late)
        return
    _abandoned.add(task)

    def settled(done: asyncio.Future[Any]) -> None:
        _abandoned.discard(done)
        _settle(done, late)

    task.add_done_callback(settled)


def _settle(task: asyncio.Future[Any], late: Callable[[Any], None] | None) -> None:
    if task.cancelled():
        return
    if task.exception() is None and late is not None:
        late(task.result())


def _close_late(response: Any) -> None:
    result = _close_quietly(response)
    if result is not None:
        _abandoned.add(result)
        result.add_done_callback(_abandoned.discard)


def _detach(task: asyncio.Future[Any]) -> None:
    """Let ``task`` finish in the background (kept referenced, errors retrieved)."""

    def settled(done: asyncio.Future[Any]) -> None:
        _abandoned.discard(done)
        if not done.cancelled():
            done.exception()

    _abandoned.add(task)
    task.add_done_callback(settled)


async def _close_within(response: AttachmentResponse, deadline: float) -> None:
    """Close ``response`` without letting a slow async ``close()`` outlive the deadline.

    The synchronous part of ``close()`` always runs; an awaitable it returns
    gets whatever time is left, then finishes in the background. Close
    errors are ignored so they cannot mask the download's own outcome.
    """
    try:
        result = response.close()
    except Exception:
        return
    if not inspect.isawaitable(result):
        return
    task = asyncio.ensure_future(result)
    remaining = deadline - asyncio.get_running_loop().time()
    current = asyncio.current_task()
    if current is not None and current.cancelling():
        remaining = 0  # the caller cancelled us: never hold cancellation up for cleanup
    try:
        if remaining > 0:
            await asyncio.wait({task}, timeout=remaining)
    finally:
        if task.done():
            if not task.cancelled():
                task.exception()
        else:
            _detach(task)


def _close_quietly(response: Any) -> asyncio.Task[None] | None:
    async def run() -> None:
        with contextlib.suppress(Exception):
            await _close(response)

    try:
        return asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        return None


async def _within_deadline(
    pending: Awaitable[Any],
    deadline: float,
    adapter: str,
    late: Callable[[Any], None] | None = None,
) -> Any:
    """Await ``pending`` until ``deadline`` (loop time), then give up on it.

    Unlike ``asyncio.timeout`` this does not wait for ``pending`` to honour
    cancellation, so an awaitable that ignores it cannot stall the download.
    ``late`` receives a result that arrives after the deadline. Once the
    deadline has passed nothing new is started, and a result that completes
    at or after it is treated as late (upstream destroys such a response).
    """
    task = asyncio.ensure_future(pending)
    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        # Cancelled before its first step, so a coroutine never runs.
        _abandon(task, late)
        raise NetworkError(adapter, _TIMED_OUT)
    try:
        done, _ = await asyncio.wait({task}, timeout=remaining)
    except BaseException:
        _abandon(task, late)
        raise
    if task not in done or loop.time() >= deadline:
        _abandon(task, late)
        raise NetworkError(adapter, _TIMED_OUT)
    return task.result()


def _check_deadline(deadline: float, adapter: str) -> None:
    if asyncio.get_running_loop().time() >= deadline:
        raise NetworkError(adapter, _TIMED_OUT)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or ""), parts.port if parts.port is not None else 443


def _hop_headers(headers: HeadersOption | None, url: str, first_origin: tuple[str, str, int | None]) -> dict[str, str]:
    merged: dict[str, str] = {"accept-encoding": _accept_encoding(), "user-agent": _USER_AGENT}
    if callable(headers):
        extra = headers(url)
        strip = False
    else:
        extra = headers
        # Divergence from upstream — see docs/UPSTREAM_SYNC.md: a static
        # mapping's credentials never follow a redirect to another origin.
        strip = _origin(url) != first_origin
    if not extra:
        return merged
    for key, value in extra.items():
        lowered = key.lower()
        if strip and lowered in _CREDENTIAL_HEADERS:
            continue
        for existing in [k for k in merged if k.lower() == lowered]:
            del merged[existing]
        merged[key] = value
    return merged


async def download_attachment(
    url: str,
    *,
    adapter: str,
    headers: HeadersOption | None = None,
    hosts: Sequence[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    on_response: OnResponse | None = None,
    redirects: int = DEFAULT_REDIRECTS,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    transport: AttachmentTransport | None = None,
) -> bytes:
    """Download an untrusted attachment URL with SSRF protection.

    HTTPS only, internal addresses refused (as literals and after DNS
    resolution), redirects followed manually (at most ``redirects``) and
    re-validated against ``hosts``, the decoded body capped at ``limit``
    bytes, and the whole operation bounded by ``timeout_ms``.

    ``headers`` is merged over the defaults (``accept-encoding`` and
    ``user-agent: Vercel.ChatSDK``) on every hop; pass a function of the hop
    URL to decide per hop. A static mapping's ``authorization``, ``cookie``
    and ``proxy-authorization`` are dropped on hops to another origin.
    ``on_response`` sees the final 2xx response before its body is read; if
    it raises, the response is closed and the error propagates.

    Failures raise :class:`NetworkError`, except errors from the transport,
    ``on_response`` or body decoding, which propagate unchanged (as
    upstream); callers usually wrap those.
    """
    send = transport if transport is not None else create_transport(adapter)
    current = validate_attachment_url(url, adapter, hosts)
    first_origin = _origin(current)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    try:
        for hop in range(redirects + 1):
            # No new hop once the deadline has passed (e.g. a slow redirect
            # close used it up), even for a transport that never suspends.
            _check_deadline(deadline, adapter)
            hop_headers = _hop_headers(headers, current, first_origin)
            response: AttachmentResponse = await _within_deadline(
                send(current, hop_headers), deadline, adapter, late=_close_late
            )
            try:
                status = response.status
                if status in REDIRECT_STATUSES:
                    location = _header(response.headers, "location")
                    if not location:
                        raise NetworkError(adapter, "Attachment redirect has no location")
                    if hop == redirects:
                        raise NetworkError(adapter, "Too many attachment redirects")
                    try:
                        target = urljoin(current, location)
                    except ValueError:
                        raise _untrusted(adapter) from None
                    current = validate_attachment_url(target, adapter, hosts)
                    continue
                if status < 200 or status >= 300:
                    reason = response.reason if response.reason is not None else ""
                    raise NetworkError(adapter, f"Failed to fetch file: {status} {reason}".strip())
                if on_response is not None:
                    result = on_response(response)
                    if inspect.isawaitable(result):
                        await _within_deadline(result, deadline, adapter)
                return cast(
                    bytes,
                    await _within_deadline(read_attachment_body(response, adapter, limit), deadline, adapter),
                )
            finally:
                await _close_within(response, deadline)
        raise NetworkError(adapter, "Too many attachment redirects")
    except NetworkError:
        raise
    except Exception as error:
        if loop.time() >= deadline:
            raise NetworkError(adapter, _TIMED_OUT, original_error=error) from error
        raise


__all__ = [
    "BLOCKED_IPV4_RANGES",
    "BLOCKED_IPV6_RANGES",
    "DEFAULT_LIMIT",
    "DEFAULT_REDIRECTS",
    "DEFAULT_TIMEOUT_MS",
    "REDIRECT_STATUSES",
    "AttachmentResponse",
    "AttachmentTransport",
    "ResolvedAddress",
    "create_resolver",
    "create_transport",
    "download_attachment",
    "is_blocked_address",
    "read_attachment_body",
    "validate_attachment_url",
]
