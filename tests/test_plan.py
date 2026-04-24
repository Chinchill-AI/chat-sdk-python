"""Tests for Plan and PostableObject support.

Covers:
- Plan creation and initial state
- PostableObject protocol detection (is_postable_object)
- Posting a Plan to a thread (fallback path)
- Posting a Plan to a channel (fallback path)
- Plan mutations: add_task, update_task, complete, reset
- Fallback text rendering
- Mutations before posting (no-ops)
- Native adapter path (post_object/edit_object)
"""

from __future__ import annotations

from typing import Any

import pytest

from chat_sdk.channel import ChannelImpl, _ChannelImplConfigWithAdapter
from chat_sdk.plan import (
    AddTaskOptions,
    CompletePlanOptions,
    Plan,
    PostableObjectContext,
    StartPlanOptions,
    UpdateTaskInput,
    is_postable_object,
    post_postable_object,
)
from chat_sdk.testing import (
    MockAdapter,
    MockStateAdapter,
    create_mock_adapter,
    create_mock_state,
)
from chat_sdk.thread import ThreadImpl, _ThreadImplConfig
from chat_sdk.types import RawMessage

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_thread(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    thread_id: str = "slack:C123:1234.5678",
) -> ThreadImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ThreadImpl(
        _ThreadImplConfig(
            id=thread_id,
            adapter=adapter,
            state_adapter=state,
        )
    )


def _make_channel(
    adapter: MockAdapter | None = None,
    state: MockStateAdapter | None = None,
    *,
    channel_id: str = "C123",
) -> ChannelImpl:
    adapter = adapter or create_mock_adapter()
    state = state or create_mock_state()
    return ChannelImpl(
        _ChannelImplConfigWithAdapter(
            id=channel_id,
            adapter=adapter,
            state_adapter=state,
        )
    )


# ---------------------------------------------------------------------------
# is_postable_object
# ---------------------------------------------------------------------------


class TestIsPostableObject:
    def test_plan_is_postable_object(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        assert is_postable_object(plan) is True

    def test_string_is_not_postable_object(self) -> None:
        assert is_postable_object("hello") is False

    def test_none_is_not_postable_object(self) -> None:
        assert is_postable_object(None) is False

    def test_dict_is_not_postable_object(self) -> None:
        assert is_postable_object({"kind": "plan"}) is False

    def test_plain_object_missing_methods(self) -> None:
        class Incomplete:
            kind = "plan"

        assert is_postable_object(Incomplete()) is False


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


class TestPlanConstruction:
    def test_initial_state(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Starting..."))
        assert plan.title == "Starting..."
        assert plan.kind == "plan"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].status == "in_progress"
        assert plan.tasks[0].title == "Starting..."

    def test_default_title(self) -> None:
        plan = Plan(StartPlanOptions(initial_message=""))
        assert plan.title == "Plan"

    def test_initial_message_list(self) -> None:
        plan = Plan(StartPlanOptions(initial_message=["Step", "1"]))
        assert plan.title == "Step 1"

    def test_initial_message_markdown_dict(self) -> None:
        plan = Plan(StartPlanOptions(initial_message={"markdown": "**Bold**"}))
        assert plan.title == "**Bold**"

    def test_current_task_is_first_task(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Task 1"))
        assert plan.current_task is not None
        assert plan.current_task.title == "Task 1"
        assert plan.current_task.status == "in_progress"

    def test_id_and_thread_id_empty_before_posting(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        assert plan.id == ""
        assert plan.thread_id == ""


# ---------------------------------------------------------------------------
# Fallback text
# ---------------------------------------------------------------------------


class TestFallbackText:
    def test_basic_fallback(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="My Plan"))
        text = plan.get_fallback_text()
        assert "\U0001f4cb My Plan" in text
        assert "\U0001f504 My Plan" in text  # in_progress icon

    @pytest.mark.asyncio
    async def test_fallback_after_completion(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="My Plan"))
        # Manually bind to allow mutations
        plan.on_posted(
            PostableObjectContext(
                adapter=create_mock_adapter(),
                message_id="msg-1",
                thread_id="t1",
            )
        )
        await plan.complete(CompletePlanOptions(complete_message="Done!"))
        text = plan.get_fallback_text()
        assert "\u2705" in text  # complete icon


# ---------------------------------------------------------------------------
# Mutations before posting (should no-op / return None)
# ---------------------------------------------------------------------------


class TestMutationsBeforePosting:
    @pytest.mark.asyncio
    async def test_add_task_before_post_returns_none(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        result = await plan.add_task(AddTaskOptions(title="Nope"))
        assert result is None
        assert len(plan.tasks) == 1  # unchanged

    @pytest.mark.asyncio
    async def test_update_task_before_post_returns_none(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        result = await plan.update_task("output")
        assert result is None

    @pytest.mark.asyncio
    async def test_complete_before_post_is_noop(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        await plan.complete(CompletePlanOptions(complete_message="Done"))
        # Title should be unchanged since not bound
        assert plan.title == "Test"

    @pytest.mark.asyncio
    async def test_reset_before_post_returns_none(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        result = await plan.reset(StartPlanOptions(initial_message="New"))
        assert result is None


# ---------------------------------------------------------------------------
# Posting via thread (fallback path)
# ---------------------------------------------------------------------------


class TestPostPlanToThread:
    @pytest.mark.asyncio
    async def test_post_plan_returns_plan(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Working..."))

        result = await thread.post(plan)

        assert result is plan
        assert plan.id == "msg-1"
        assert plan.thread_id == thread.id

    @pytest.mark.asyncio
    async def test_post_plan_calls_adapter_post_message(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Working..."))

        await thread.post(plan)

        assert len(adapter._post_calls) == 1
        thread_id, message = adapter._post_calls[0]
        assert thread_id == thread.id
        assert isinstance(message, str)
        assert "Working..." in message

    @pytest.mark.asyncio
    async def test_mutations_after_posting(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Step 1"))

        await thread.post(plan)

        # Add a task
        task = await plan.add_task(AddTaskOptions(title="Step 2"))
        assert task is not None
        assert task.title == "Step 2"
        assert task.status == "in_progress"
        assert len(plan.tasks) == 2
        assert plan.tasks[0].status == "complete"  # auto-completed

        # Edit was called (fallback path)
        assert len(adapter._edit_calls) >= 1

    @pytest.mark.asyncio
    async def test_update_task_sets_output(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Working"))
        await thread.post(plan)

        result = await plan.update_task("Got results")
        assert result is not None
        assert result.status == "in_progress"

        # Check the internal model for output
        assert plan._model.tasks[0].output == "Got results"

    @pytest.mark.asyncio
    async def test_update_task_with_structured_input(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Working"))
        await thread.post(plan)

        result = await plan.update_task(UpdateTaskInput(output="data", status="error"))
        assert result is not None
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_complete_marks_all_done(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Step 1"))
        await thread.post(plan)

        await plan.add_task(AddTaskOptions(title="Step 2"))
        await plan.complete(CompletePlanOptions(complete_message="All done!"))

        assert plan.title == "All done!"
        for t in plan.tasks:
            assert t.status == "complete"

    @pytest.mark.asyncio
    async def test_reset_replaces_all_tasks(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Step 1"))
        await thread.post(plan)
        await plan.add_task(AddTaskOptions(title="Step 2"))

        result = await plan.reset(StartPlanOptions(initial_message="Fresh start"))
        assert result is not None
        assert result.title == "Fresh start"
        assert len(plan.tasks) == 1
        assert plan.tasks[0].status == "in_progress"

    @pytest.mark.asyncio
    async def test_current_task_tracks_in_progress(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Task 1"))
        await thread.post(plan)

        assert plan.current_task is not None
        assert plan.current_task.title == "Task 1"

        await plan.add_task(AddTaskOptions(title="Task 2"))
        assert plan.current_task is not None
        assert plan.current_task.title == "Task 2"


# ---------------------------------------------------------------------------
# Posting via channel (fallback path)
# ---------------------------------------------------------------------------


class TestPostPlanToChannel:
    @pytest.mark.asyncio
    async def test_post_plan_to_channel_returns_plan(self) -> None:
        adapter = create_mock_adapter()
        channel = _make_channel(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Channel plan"))

        result = await channel.post(plan)

        assert result is plan
        assert plan.id != ""


# ---------------------------------------------------------------------------
# Native adapter path (post_object / edit_object)
# ---------------------------------------------------------------------------


class _NativeAdapter(MockAdapter):
    """MockAdapter with post_object/edit_object support."""

    def __init__(self) -> None:
        super().__init__("native")
        self._post_object_calls: list[tuple[str, str, Any]] = []
        self._edit_object_calls: list[tuple[str, str, str, Any]] = []

    async def post_object(self, thread_id: str, kind: str, data: Any) -> RawMessage:
        self._post_object_calls.append((thread_id, kind, data))
        return RawMessage(id="obj-1", thread_id=thread_id, raw={})

    async def edit_object(self, thread_id: str, message_id: str, kind: str, data: Any) -> RawMessage:
        self._edit_object_calls.append((thread_id, message_id, kind, data))
        return RawMessage(id=message_id, thread_id=thread_id, raw={})


class TestNativeAdapterPath:
    @pytest.mark.asyncio
    async def test_uses_post_object_when_supported(self) -> None:
        adapter = _NativeAdapter()
        state = create_mock_state()
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="t1",
                adapter=adapter,
                state_adapter=state,
            )
        )
        plan = Plan(StartPlanOptions(initial_message="Native plan"))

        result = await thread.post(plan)

        assert result is plan
        assert len(adapter._post_object_calls) == 1
        assert adapter._post_object_calls[0][1] == "plan"
        # Fallback post_message should NOT have been called
        assert len(adapter._post_calls) == 0

    @pytest.mark.asyncio
    async def test_uses_edit_object_for_mutations(self) -> None:
        adapter = _NativeAdapter()
        state = create_mock_state()
        thread = ThreadImpl(
            _ThreadImplConfig(
                id="t1",
                adapter=adapter,
                state_adapter=state,
            )
        )
        plan = Plan(StartPlanOptions(initial_message="Native plan"))
        await thread.post(plan)

        await plan.add_task(AddTaskOptions(title="Step 2"))

        assert len(adapter._edit_object_calls) >= 1
        call = adapter._edit_object_calls[0]
        assert call[1] == "obj-1"  # message_id
        assert call[2] == "plan"  # kind

    @pytest.mark.asyncio
    async def test_is_supported_returns_true_for_native(self) -> None:
        adapter = _NativeAdapter()
        plan = Plan(StartPlanOptions(initial_message="Test"))
        assert plan.is_supported(adapter) is True

    @pytest.mark.asyncio
    async def test_is_supported_returns_false_for_mock(self) -> None:
        adapter = create_mock_adapter()
        plan = Plan(StartPlanOptions(initial_message="Test"))
        assert plan.is_supported(adapter) is False


# ---------------------------------------------------------------------------
# post_postable_object helper
# ---------------------------------------------------------------------------


class TestPostPostableObject:
    @pytest.mark.asyncio
    async def test_fallback_path(self) -> None:
        adapter = create_mock_adapter()
        plan = Plan(StartPlanOptions(initial_message="Test"))

        async def post_fn(thread_id: str, message: str) -> RawMessage:
            return RawMessage(id="m1", thread_id=thread_id, raw={})

        await post_postable_object(plan, adapter, "t1", post_fn)

        assert plan.id == "m1"
        assert plan.thread_id == "t1"

    @pytest.mark.asyncio
    async def test_native_path(self) -> None:
        adapter = _NativeAdapter()
        plan = Plan(StartPlanOptions(initial_message="Test"))

        async def post_fn(thread_id: str, message: str) -> RawMessage:
            raise AssertionError("should not be called")

        await post_postable_object(plan, adapter, "t1", post_fn)

        assert plan.id == "obj-1"
        assert len(adapter._post_object_calls) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_multiple_add_tasks_mark_previous_complete(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Start"))
        await thread.post(plan)

        await plan.add_task(AddTaskOptions(title="Task 2"))
        await plan.add_task(AddTaskOptions(title="Task 3"))

        assert len(plan.tasks) == 3
        assert plan.tasks[0].status == "complete"
        assert plan.tasks[1].status == "complete"
        assert plan.tasks[2].status == "in_progress"

    @pytest.mark.asyncio
    async def test_update_task_with_none(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Start"))
        await thread.post(plan)

        result = await plan.update_task(None)
        assert result is not None
        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_plan_get_post_data_returns_model(self) -> None:
        plan = Plan(StartPlanOptions(initial_message="Test"))
        data = plan.get_post_data()
        assert data.title == "Test"
        assert len(data.tasks) == 1

    @pytest.mark.asyncio
    async def test_add_task_with_children(self) -> None:
        adapter = create_mock_adapter()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Start"))
        await thread.post(plan)

        task = await plan.add_task(AddTaskOptions(title="Sub", children=["a", "b"]))
        assert task is not None
        # Check internal model has details
        assert plan._model.tasks[-1].details == ["a", "b"]


# ---------------------------------------------------------------------------
# Error handling in _enqueue_edit
# ---------------------------------------------------------------------------


class _FailingEditAdapter(MockAdapter):
    """MockAdapter whose edit_message raises on demand."""

    def __init__(self) -> None:
        super().__init__("failing")
        self.fail_edit = False

    async def edit_message(self, thread_id: str, message_id: str, message: Any) -> RawMessage:
        if self.fail_edit:
            raise RuntimeError("simulated edit failure")
        return await super().edit_message(thread_id, message_id, message)


class _SpyLogger:
    """Minimal logger that records warn calls."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, Any]] = []

    def child(self, prefix: str) -> _SpyLogger:
        return self

    def debug(self, message: str, *args: Any) -> None:
        pass

    def info(self, message: str, *args: Any) -> None:
        pass

    def warn(self, message: str, *args: Any) -> None:
        self.warnings.append((message, args))

    def error(self, message: str, *args: Any) -> None:
        pass


class TestEditErrorPath:
    @pytest.mark.asyncio
    async def test_edit_failure_propagates_and_plan_continues(self) -> None:
        """When ``adapter.edit_message`` raises, the caller sees the
        exception (mirroring upstream TS ``enqueueEdit``), the failure
        is logged by the internal chain tail, and the next mutation
        still fires successfully without the previous rejection
        poisoning the queue.
        """
        adapter = _FailingEditAdapter()
        logger = _SpyLogger()
        thread = _make_thread(adapter=adapter)
        plan = Plan(StartPlanOptions(initial_message="Step 1"))
        await thread.post(plan)

        # Inject the spy logger into the bound state
        assert plan._bound is not None
        plan._bound.logger = logger

        # First mutation: edit will fail — caller observes the error.
        adapter.fail_edit = True
        with pytest.raises(RuntimeError, match="simulated edit failure"):
            await plan.add_task(AddTaskOptions(title="Step 2"))

        # The internal chain absorbs the error and logs it so the queue
        # is not poisoned for the next edit.
        assert any("Failed to edit plan" in w[0] for w in logger.warnings)

        # Second mutation: edit succeeds -- plan is still usable.
        adapter.fail_edit = False
        logger.warnings.clear()
        task = await plan.add_task(AddTaskOptions(title="Step 3"))
        assert task is not None
        assert task.title == "Step 3"
        assert len(plan.tasks) == 3
