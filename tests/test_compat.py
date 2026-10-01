"""Tests for the Python-only adapter-signature probe (``chat_sdk._compat``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chat_sdk._compat import accepts_kwarg
from chat_sdk.cards import Actions, Button, Card
from chat_sdk.errors import ChatNotImplementedError
from chat_sdk.testing import create_mock_state
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import BaseAdapter


async def _three_args(thread_id: str, user_id: str, message: Any) -> None: ...


async def _positional_options(thread_id: str, user_id: str, message: Any, options: Any = None) -> None: ...


async def _keyword_only_options(thread_id: str, user_id: str, message: Any, *, options: Any = None) -> None: ...


async def _var_kwargs(thread_id: str, user_id: str, message: Any, **kwargs: Any) -> None: ...


async def _positional_only_options(thread_id: str, user_id: str, message: Any, options: Any = None, /) -> None: ...


class _Adapter:
    async def legacy(self, thread_id: str, user_id: str, message: Any) -> None: ...

    async def modern(self, thread_id: str, user_id: str, message: Any, *, options: Any = None) -> None: ...


class _Unhashable:
    __hash__ = None  # type: ignore[assignment]

    async def __call__(self, thread_id: str, user_id: str, message: Any, *, options: Any = None) -> None: ...


@pytest.mark.parametrize(
    ("func", "expected"),
    [
        (_three_args, False),
        (_positional_options, True),
        (_keyword_only_options, True),
        (_var_kwargs, True),
        (_positional_only_options, False),
        (_Adapter().legacy, False),
        (_Adapter().modern, True),
        (AsyncMock(), True),
        (_Unhashable(), True),
    ],
    ids=[
        "three-args",
        "positional-or-keyword",
        "keyword-only",
        "var-kwargs",
        "positional-only",
        "bound-legacy",
        "bound-modern",
        "async-mock",
        "unhashable-callable",
    ],
)
def test_accepts_kwarg(func: Any, expected: bool) -> None:
    assert accepts_kwarg(func, "options") is expected


class _MinimalBaseAdapter(BaseAdapter):
    @property
    def name(self) -> str:
        return "custom"

    @property
    def user_name(self) -> str:
        return "custom-bot"


def _base_adapter_thread(state: Any = None) -> ThreadImpl:
    return ThreadImpl(
        _ThreadImplConfig(
            id="custom:C1:T1",
            adapter=_MinimalBaseAdapter(),  # type: ignore[arg-type]
            state_adapter=state if state is not None else create_mock_state(),
        )
    )


# A BaseAdapter subclass that does not define ``reply`` / ``mark_as_read`` is
# unsupported, and the error comes first, as upstream's ``if
# (!this.adapter.reply)`` check: no stream is consumed and no callback token is
# minted before ``ChatNotImplementedError``.


@pytest.mark.asyncio
async def test_base_adapter_without_reply_raises_before_consuming_the_stream() -> None:
    consumed: list[str] = []

    async def stream() -> AsyncIterator[str]:
        for chunk in ("a", "b"):
            consumed.append(chunk)
            yield chunk

    with pytest.raises(ChatNotImplementedError) as exc_info:
        await _base_adapter_thread().reply("m1", stream())
    assert (exc_info.value.adapter, exc_info.value.method) == ("custom", "replies")
    assert consumed == []


@pytest.mark.asyncio
async def test_base_adapter_without_reply_raises_before_minting_callback_tokens() -> None:
    state = create_mock_state()
    card = Card(children=[Actions([Button(id="b", label="Go", callback_url="https://example.com/cb")])])

    with pytest.raises(ChatNotImplementedError):
        await _base_adapter_thread(state).reply("m1", card)
    assert [key for key in state.cache if key.startswith("chat:callback:")] == []


@pytest.mark.asyncio
async def test_base_adapter_without_mark_as_read_raises_not_implemented_outside_a_handler() -> None:
    # No current message: upstream reports the missing hook, not MESSAGE_REQUIRED.
    with pytest.raises(ChatNotImplementedError) as exc_info:
        await _base_adapter_thread().mark_as_read()
    assert (exc_info.value.adapter, exc_info.value.method) == ("custom", "read-receipts")
