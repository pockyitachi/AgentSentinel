from typing import Any

from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.null_recorder import (
    NULL_RECORDER,
    NULL_TASK_RECORDER,
    NullRecorder,
    NullTaskRecorder,
)


class ExplodesIfInspected:
    def __iter__(self) -> Any:
        raise AssertionError("disabled recorder inspected captured data")

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"disabled recorder accessed captured data: {name}")


def test_null_run_recorder_is_a_true_no_op() -> None:
    recorder = NullRecorder()
    opaque = ExplodesIfInspected()

    assert recorder.enabled is False
    assert recorder.blob_store is None
    assert recorder.collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert recorder.write_manifest_start(opaque) is None
    assert recorder.append_run_event("run_started", opaque, EVENT_ID) is None
    assert recorder.write_manifest_final(opaque) is None
    assert recorder.close() is None


def test_null_task_recorder_is_reused_and_does_not_inspect_payload() -> None:
    opaque = ExplodesIfInspected()
    task = NULL_RECORDER.open_task("not-inspected")

    assert task is NULL_TASK_RECORDER
    assert isinstance(task, NullTaskRecorder)
    assert task.enabled is False
    assert task.blob_store is None
    assert task.collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert task.append_event("step_started", opaque, EVENT_ID) is None
    assert task.flush() is None
    assert task.close() is None
    assert NULL_RECORDER.open_task() is NULL_TASK_RECORDER


def test_null_recorders_are_safe_context_managers() -> None:
    with NULL_RECORDER as recorder:
        assert recorder is NULL_RECORDER
        with recorder.open_task("task") as task:
            assert task is NULL_TASK_RECORDER


EVENT_ID = "0198a000-0000-7000-8000-000000000101"
