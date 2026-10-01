"""Python-only compatibility helpers for calling adapter hooks.

Upstream (TypeScript) can add a trailing optional parameter to an adapter
hook and every existing implementation keeps working, because JavaScript
silently drops extra arguments. Python raises ``TypeError`` instead, so when
the SDK starts passing a new keyword to a hook (``post_ephemeral(options=)``)
it first checks that the implementation accepts it. In-repo adapters always
do; the probe keeps third-party adapters written against the older signature
working. See docs/UPSTREAM_SYNC.md (Known Non-Parity).
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any


@functools.lru_cache(maxsize=512)
def _accepts_kwarg_cached(func: Callable[..., Any], name: str) -> bool:
    return _accepts_kwarg_uncached(func, name)


def _accepts_kwarg_uncached(func: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        # Not introspectable (some builtins / C callables): pass the keyword,
        # as upstream always does.
        return True
    for param in params:
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    """Return whether ``func`` can be called with keyword argument ``name``.

    ``**kwargs`` counts as accepting. Bound methods are probed through their
    underlying function so the result is cached per class, not per instance.
    """
    target = getattr(func, "__func__", func)
    try:
        return _accepts_kwarg_cached(target, name)
    except TypeError:
        # Unhashable callable: probe without the cache.
        return _accepts_kwarg_uncached(target, name)


async def aclose_quietly(iterator: object) -> None:
    """Best-effort ``aclose()``, like upstream's ``iterator.return().catch(() => {})``.

    Suppresses errors raised while closing, including the
    ``BaseExceptionGroup([GeneratorExit()])`` an async generator raises when
    it holds a ``TaskGroup`` across ``yield``. Cancellation and other
    control-flow exceptions still propagate. No-op without ``aclose``.
    """
    aclose = getattr(iterator, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: S110 — closing is best-effort
        pass
    except BaseExceptionGroup as group:
        _, rest = group.split((GeneratorExit, Exception))
        if rest is not None:
            raise
