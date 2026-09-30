"""Log-hygiene helpers shared by the webhook adapters.

Webhook handlers log request-shape metadata (byte length, event type,
content type, whether a signature is present) instead of request bodies,
so unauthenticated input and message content never reach log sinks
(upstream ``fc7df9c4`` / ``f485255b``).
"""

from __future__ import annotations


def utf8_byte_length(body: str | bytes | bytearray | None) -> int:
    """Return the UTF-8 byte length of a webhook body, for logging.

    Python counterpart of Node's ``Buffer.byteLength(body)`` — the byte count,
    not ``len(str)`` (which counts code points). ``None`` and empty bodies are
    ``0``. Lone surrogates are counted as three bytes each (as Node does when it
    substitutes U+FFFD) instead of raising, so computing a log field can never
    turn a malformed request into a 500.
    """
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    return len(body.encode("utf-8", "surrogatepass"))
