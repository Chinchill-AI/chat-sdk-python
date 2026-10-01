"""Compatibility helpers for adapters written against older hook signatures.

Python-only (no upstream counterpart). Upstream adds optional trailing
parameters to adapter hooks (e.g. ``startTyping(threadId, status, options)``);
in JS, extra arguments are ignored, but in Python they raise ``TypeError`` on
an adapter that predates them. Callers pass the new keyword only when
:func:`accepts_kwarg` says the hook takes it.
"""

from __future__ import annotations

import contextlib
import inspect
import weakref
from collections.abc import Callable
from typing import Any

_cache: weakref.WeakKeyDictionary[Any, dict[str, bool]] = weakref.WeakKeyDictionary()


def accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    """Return whether ``fn`` can be called with the keyword argument ``name``.

    True when ``fn`` declares ``name`` as a keyword-capable parameter or takes
    ``**kwargs``. False when it does not, or when its signature cannot be
    inspected (calling without the keyword is the safe choice). Results are
    cached per underlying function (bound methods share their function's
    entry).
    """
    target = getattr(fn, "__func__", fn)
    try:
        per_fn = _cache.get(target)
    except TypeError:  # not hashable / not weak-referenceable
        per_fn = None
    if per_fn is not None and name in per_fn:
        return per_fn[name]

    result = _probe(fn, name)
    with contextlib.suppress(TypeError):  # not hashable / not weak-referenceable
        _cache.setdefault(target, {})[name] = result
    return result


def _probe(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    for param in params:
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False
