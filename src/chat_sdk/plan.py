"""Plan implementation for chat-sdk.

Python port of Vercel Chat SDK plan.ts and postable-object.ts.
Provides the Plan class (a PostableObject that manages a task list),
and the ``post_postable_object`` helper used by Thread/Channel to post
any PostableObject.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from chat_sdk.logger import Logger
from chat_sdk.types import Adapter

# =============================================================================
# Plan Types
# =============================================================================

PlanTaskStatus = Literal["pending", "in_progress", "complete", "error"]


@dataclass
class PlanTask:
    """Public view of a plan task (id, title, status)."""

    id: str
    title: str
    status: PlanTaskStatus


@dataclass
class PlanModelTask:
    """Internal model of a plan task with optional details/output."""

    id: str
    title: str
    status: PlanTaskStatus
    details: PlanContent | None = None
    output: PlanContent | None = None


PlanContent = str | list[str] | dict[str, Any]
"""Content that can be plain text, a list of strings, or ``{"markdown": ...}``/``{"ast": ...}``."""


@dataclass
class PlanModel:
    """Internal plan model with title and tasks."""

    title: str
    tasks: list[PlanModelTask] = field(default_factory=list)


@dataclass
class StartPlanOptions:
    """Options for starting a new plan."""

    initial_message: PlanContent


@dataclass
class AddTaskOptions:
    """Options for adding a task to a plan."""

    title: PlanContent
    children: PlanContent | None = None


@dataclass
class UpdateTaskInput:
    """Structured update input targeting a task by ``id`` (or the last
    in-progress task when ``id`` is omitted) with optional output and
    status override.

    Mirrors upstream ``UpdateTaskInput`` shape (`plan.ts`):
    ``{ id?: string; output?: PlanContent; status?: PlanTaskStatus }``.
    When ``id`` is set but no matching task exists, ``update_task``
    returns ``None`` (matching upstream).
    """

    id: str | None = None
    output: PlanContent | None = None
    status: PlanTaskStatus | None = None


@dataclass
class CompletePlanOptions:
    """Options for completing a plan."""

    complete_message: PlanContent


# =============================================================================
# PostableObject context
# =============================================================================


@dataclass
class PostableObjectContext:
    """Context provided to a PostableObject after it has been posted."""

    adapter: Adapter
    message_id: str
    thread_id: str
    logger: Logger | None = None


# =============================================================================
# Helpers
# =============================================================================


def _content_to_plain_text(content: PlanContent | None) -> str:
    """Convert PlanContent to plain text for titles/labels."""
    if content is None:
        return ""
    if isinstance(content, list):
        return " ".join(content).strip()
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and "markdown" in content:
        md = content["markdown"]
        return str(md) if md is not None else ""
    if isinstance(content, dict):
        # For ast dicts, return empty -- full rendering not needed for titles
        pass
    return ""


def is_postable_object(value: Any) -> bool:
    """Check if a value is a PostableObject (has the required protocol methods)."""
    return (
        value is not None
        and hasattr(value, "kind")
        and hasattr(value, "get_fallback_text")
        and hasattr(value, "get_post_data")
        and hasattr(value, "is_supported")
        and hasattr(value, "on_posted")
    )


# =============================================================================
# post_postable_object — shared helper for Thread & Channel
# =============================================================================


async def post_postable_object(
    obj: Any,
    adapter: Adapter,
    thread_id: str,
    post_fn: Any,
    logger: Logger | None = None,
) -> Any:
    """Post a PostableObject using the adapter's native support or fallback text.

    Returns the ``RawMessage`` from the adapter so callers can use the
    real message ID for history caching.

    Parameters
    ----------
    obj:
        The PostableObject to post.
    adapter:
        The adapter to use.
    thread_id:
        Thread or channel ID to post to.
    post_fn:
        Async callable ``(thread_id, message) -> RawMessage`` used for posting.
    logger:
        Optional logger for error reporting.
    """

    def _make_context(raw: Any) -> PostableObjectContext:
        return PostableObjectContext(
            adapter=adapter,
            logger=logger,
            message_id=raw.id,
            thread_id=getattr(raw, "thread_id", None) or thread_id,
        )

    if obj.is_supported(adapter) and hasattr(adapter, "post_object") and adapter.post_object:
        raw = await adapter.post_object(thread_id, obj.kind, obj.get_post_data())
        obj.on_posted(_make_context(raw))
    else:
        raw = await post_fn(thread_id, obj.get_fallback_text())
        obj.on_posted(_make_context(raw))
    return raw


# =============================================================================
# Bound state for a posted Plan
# =============================================================================


@dataclass
class _BoundState:
    adapter: Adapter
    fallback: bool
    message_id: str
    thread_id: str
    logger: Logger | None = None
    # Tail of the synchronously-built edit chain. Each ``_enqueue_edit``
    # reads this, chains a new task after it, and assigns the new tail
    # — all before yielding — so concurrent callers see FIFO ordering.
    update_chain: asyncio.Task[None] | None = None


# =============================================================================
# Plan
# =============================================================================


class Plan:
    """A Plan represents a task list that can be posted to a thread.

    Create a plan with ``Plan(StartPlanOptions(initial_message="..."))``
    and post it with ``await thread.post(plan)``.

    After posting, use methods like ``add_task()``, ``update_task()``,
    and ``complete()`` to update it.

    Example::

        plan = Plan(StartPlanOptions(initial_message="Starting task..."))
        await thread.post(plan)
        await plan.add_task(AddTaskOptions(title="Fetch data"))
        await plan.update_task("Got 42 results")
        await plan.complete(CompletePlanOptions(complete_message="Done!"))
    """

    kind: str = "plan"

    def __init__(self, options: StartPlanOptions) -> None:
        title = _content_to_plain_text(options.initial_message) or "Plan"
        first_task = PlanModelTask(
            id=str(uuid.uuid4()),
            title=title,
            status="in_progress",
        )
        self._model = PlanModel(title=title, tasks=[first_task])
        self._bound: _BoundState | None = None

    # -- PostableObject protocol ------------------------------------------------

    def is_supported(self, adapter: Adapter) -> bool:
        """Check if the adapter supports native plan rendering."""
        return (
            hasattr(adapter, "post_object")
            and adapter.post_object is not None  # type: ignore[union-attr]
            and hasattr(adapter, "edit_object")
            and adapter.edit_object is not None  # type: ignore[union-attr]
        )

    def get_post_data(self) -> PlanModel:
        """Get the plan model data for the adapter."""
        return self._model

    def get_fallback_text(self) -> str:
        """Get a plain-text fallback representation of the plan."""
        lines: list[str] = []
        lines.append(f"\U0001f4cb {self._model.title or 'Plan'}")
        status_icons: dict[str, str] = {
            "complete": "\u2705",
            "in_progress": "\U0001f504",
            "error": "\u274c",
        }
        for task in self._model.tasks:
            icon = status_icons.get(task.status, "\u2b1c")
            lines.append(f"{icon} {task.title}")
        return "\n".join(lines)

    def on_posted(self, context: PostableObjectContext) -> None:
        """Bind this plan to a posted message so subsequent mutations update it."""
        self._bound = _BoundState(
            adapter=context.adapter,
            fallback=not self.is_supported(context.adapter),
            logger=context.logger,
            message_id=context.message_id,
            thread_id=context.thread_id,
        )

    # -- Read-only properties ---------------------------------------------------

    @property
    def id(self) -> str:
        return self._bound.message_id if self._bound else ""

    @property
    def thread_id(self) -> str:
        return self._bound.thread_id if self._bound else ""

    @property
    def title(self) -> str:
        return self._model.title

    @property
    def tasks(self) -> list[PlanTask]:
        return [PlanTask(id=t.id, title=t.title, status=t.status) for t in self._model.tasks]

    @property
    def current_task(self) -> PlanTask | None:
        """Get the current (last in-progress) task, or the last task."""
        current: PlanModelTask | None = None
        for t in reversed(self._model.tasks):
            if t.status == "in_progress":
                current = t
                break
        if current is None and self._model.tasks:
            current = self._model.tasks[-1]
        if current is None:
            return None
        return PlanTask(id=current.id, title=current.title, status=current.status)

    # -- Mutations --------------------------------------------------------------

    async def add_task(self, options: AddTaskOptions) -> PlanTask | None:
        """Add a new task to the plan.

        Marks all in-progress tasks as complete and adds a new in-progress task.
        """
        if not self._can_mutate():
            return None
        title = _content_to_plain_text(options.title) or "Task"
        for task in self._model.tasks:
            if task.status == "in_progress":
                task.status = "complete"
        next_task = PlanModelTask(
            id=str(uuid.uuid4()),
            title=title,
            status="in_progress",
            details=options.children,
        )
        self._model.tasks.append(next_task)
        self._model.title = title

        await self._enqueue_edit()
        return PlanTask(id=next_task.id, title=next_task.title, status=next_task.status)

    async def update_task(self, update: PlanContent | UpdateTaskInput | None = None) -> PlanTask | None:
        """Update a task on this plan.

        ``update`` can be:
        - ``PlanContent`` (str, list, dict) -- sets the output on the last
          in-progress task (falling back to the last task).
        - ``UpdateTaskInput`` -- sets output and/or status. When
          ``update.id`` is set, targets that specific task and returns
          ``None`` if no task matches. When ``id`` is omitted, behaves
          like the PlanContent path (last in-progress task).
        - ``None`` -- just triggers a re-render of the current state.
        """
        if not self._can_mutate():
            return None
        current: PlanModelTask | None = None
        if isinstance(update, UpdateTaskInput) and update.id is not None:
            for t in self._model.tasks:
                if t.id == update.id:
                    current = t
                    break
            # Upstream returns null for a non-existent id rather than
            # silently falling back to "last in-progress".
            if current is None:
                return None
        else:
            for t in reversed(self._model.tasks):
                if t.status == "in_progress":
                    current = t
                    break
            if current is None and self._model.tasks:
                current = self._model.tasks[-1]
            if current is None:
                return None

        if update is not None:
            if isinstance(update, UpdateTaskInput):
                if update.output is not None:
                    current.output = update.output
                if update.status is not None:
                    current.status = update.status
            else:
                # PlanContent
                current.output = update

        await self._enqueue_edit()
        return PlanTask(id=current.id, title=current.title, status=current.status)

    async def reset(self, options: StartPlanOptions) -> PlanTask | None:
        """Reset the plan to a single new task."""
        if not self._can_mutate():
            return None
        title = _content_to_plain_text(options.initial_message) or "Plan"
        first_task = PlanModelTask(
            id=str(uuid.uuid4()),
            title=title,
            status="in_progress",
        )
        self._model = PlanModel(title=title, tasks=[first_task])
        await self._enqueue_edit()
        return PlanTask(id=first_task.id, title=first_task.title, status=first_task.status)

    async def complete(self, options: CompletePlanOptions) -> None:
        """Mark the plan as complete.

        Sets all in-progress tasks to complete and updates the title.
        """
        if not self._can_mutate():
            return
        for task in self._model.tasks:
            if task.status == "in_progress":
                task.status = "complete"
        self._model.title = _content_to_plain_text(options.complete_message) or self._model.title
        await self._enqueue_edit()

    # -- Internal ---------------------------------------------------------------

    def _can_mutate(self) -> bool:
        return self._bound is not None

    async def _enqueue_edit(self) -> None:
        """Edit the posted message with the current plan state.

        Chains edits sequentially to avoid race conditions. Mirrors the
        upstream TS pattern (`plan.ts`):

        ```ts
        const chained = bound.updateChain.then(doEdit, doEdit);
        bound.updateChain = chained.then(() => undefined, (err) => log);
        return chained;
        ```

        Crucially, the new chain tail (``update_chain``) is registered
        **synchronously** — before any ``await`` — so that concurrent
        callers racing through ``asyncio.gather`` observe a strict FIFO
        ordering. Errors from the adapter edit propagate to the caller
        via the returned awaitable (``chained``); the internal chain
        absorbs them so the next enqueued edit still runs.
        """
        if self._bound is None:
            return

        bound = self._bound
        prev = bound.update_chain  # synchronous read — must not await first

        async def _do_edit() -> None:
            if bound.fallback:
                await bound.adapter.edit_message(
                    bound.thread_id,
                    bound.message_id,
                    self.get_fallback_text(),
                )
            else:
                edit_object = getattr(bound.adapter, "edit_object", None)
                if edit_object is None:
                    return
                await edit_object(
                    bound.thread_id,
                    bound.message_id,
                    self.kind,
                    self._model,
                )

        async def _run_after_prev() -> None:
            if prev is not None:
                # Upstream ``.then(doEdit, doEdit)`` runs doEdit whether
                # the previous edit resolved or rejected; mirror that by
                # absorbing any exception from the previous step here.
                # (Note: the internal chain tail absorbs errors anyway,
                # so in practice ``await prev`` only raises if someone
                # swapped the chain out with a rejecting future — the
                # suppression keeps the parity guarantee defensive.)
                with contextlib.suppress(BaseException):
                    await prev
            await _do_edit()

        loop = asyncio.get_running_loop()
        chained = loop.create_task(_run_after_prev())

        async def _absorb_for_chain() -> None:
            # The internal chain tail must not propagate errors — otherwise
            # the next enqueued edit would await a rejected future and be
            # treated as a previous-failure chain that still runs doEdit,
            # but we'd also lose the ability to recover cleanly. Upstream
            # uses ``chained.then(() => undefined, (err) => logger.warn)``.
            try:
                await chained
            except BaseException as exc:  # noqa: BLE001 — log and swallow for queue
                if bound.logger is not None:
                    bound.logger.warn("Failed to edit plan", exc)

        bound.update_chain = loop.create_task(_absorb_for_chain())
        # ``chained`` preserves upstream semantics: exceptions from the
        # adapter edit propagate to the caller.
        await chained
