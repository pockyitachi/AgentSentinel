from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event

import pytest

from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.recorder import (
    RecorderClosedError,
    RecorderFinalizedError,
    RunRecorder,
    StreamCorruptionError,
)
from mobile_world.runtime.audit.schemas import SCHEMA_VERSION, Producer


def _producer() -> Producer:
    return Producer.local(version="test", worker_id="worker-1")


def _start_manifest(run_id: str) -> dict[str, object]:
    return {"run_id": run_id, "note": "immutable start"}


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_run_and_task_streams_have_independent_contiguous_sequences(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, producer=_producer(), sync=False)
    recorder.write_manifest_start(_start_manifest(recorder.run_id))

    run_started = recorder.append_run_event("run_started", {"kind": "start"})
    task = recorder.open_task()
    task_started = task.append_event("task_started", {"kind": "task"})
    step = task.append_event(
        "step_started",
        {"step_id": new_ulid()},
        task_started["event_id"],
    )
    run_ended = recorder.append_run_event("run_ended", {"kind": "end"}, run_started["event_id"])

    assert recorder.enabled is True
    assert recorder.collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert task.enabled is True
    assert task.collector_mode is recorder.collector_mode
    assert task.blob_store is recorder.blob_store
    assert [run_started["seq"], run_ended["seq"]] == [1, 2]
    assert [task_started["seq"], step["seq"]] == [1, 2]
    assert run_started["stream_id"] == recorder.run_id
    assert task_started["stream_id"] == task.task_run_id
    assert task.path == (
        tmp_path / "raw" / "runs" / recorder.run_id / "tasks" / task.task_run_id / "events.jsonl"
    )

    recorder.close()
    assert [event["seq"] for event in _read_jsonl(recorder.run_root / "run.events.jsonl")] == [
        1,
        2,
    ]
    assert [event["seq"] for event in _read_jsonl(task.path)] == [1, 2]


def test_concurrent_writers_share_file_lock_and_allocate_each_seq_once(tmp_path: Path) -> None:
    run_id = new_ulid()
    task_run_id = new_ulid()
    first = RunRecorder(tmp_path, producer=_producer(), run_id=run_id, sync=False)
    first.write_manifest_start(_start_manifest(run_id))
    second = RunRecorder(tmp_path, producer=_producer(), run_id=run_id, sync=False)
    first_task = first.open_task(task_run_id)
    second_task = second.open_task(task_run_id)

    def append(index: int) -> None:
        recorder = first_task if index % 2 == 0 else second_task
        recorder.append_event("collector_error", {"index": index})

    with ThreadPoolExecutor(max_workers=12) as executor:
        list(executor.map(append, range(120)))

    first.close()
    second.close()
    events = _read_jsonl(first_task.path)
    assert len(events) == 120
    assert [event["seq"] for event in events] == list(range(1, 121))
    assert len({event["event_id"] for event in events}) == 120


def test_manifests_are_exclusive_and_finalization_accepts_closed_tasks(tmp_path: Path) -> None:
    recorder = RunRecorder(
        tmp_path,
        producer=_producer(),
        collector_mode="fail_open_with_incomplete_marker",
    )
    start_path = recorder.write_manifest_start(_start_manifest(recorder.run_id))
    original_start = start_path.read_bytes()
    with pytest.raises(FileExistsError):
        recorder.write_manifest_start({"run_id": recorder.run_id, "changed": True})

    task = recorder.open_task()
    task.append_event("task_started", {})
    task.close()
    final_path = recorder.write_manifest_final(
        {"run_id": recorder.run_id, "capture_complete": False}
    )

    assert recorder.collector_mode is CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
    assert start_path.read_bytes() == original_start
    assert start_path != final_path
    assert json.loads(start_path.read_text())["raw_schema_version"] == SCHEMA_VERSION
    assert json.loads(final_path.read_text())["capture_complete"] is False
    with pytest.raises(RecorderFinalizedError):
        recorder.append_run_event("run_ended", {})
    with pytest.raises(RecorderFinalizedError):
        recorder.write_manifest_final({"run_id": recorder.run_id})
    recorder.close()


def test_cross_instance_finalization_waits_for_inflight_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = new_ulid()
    task_run_id = new_ulid()
    first = RunRecorder(tmp_path, producer=_producer(), run_id=run_id, sync=False)
    first.write_manifest_start(_start_manifest(run_id))
    second = RunRecorder(tmp_path, producer=_producer(), run_id=run_id, sync=False)
    task = first.open_task(task_run_id)
    append_entered = Event()
    release_append = Event()
    original_append = task._stream.append

    def blocked_append(*args, **kwargs):
        append_entered.set()
        assert release_append.wait(timeout=2)
        return original_append(*args, **kwargs)

    monkeypatch.setattr(task._stream, "append", blocked_append)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            append_future = executor.submit(task.append_event, "task_started", {})
            assert append_entered.wait(timeout=2)
            final_future = executor.submit(
                second.write_manifest_final,
                {"run_id": run_id, "capture_complete": False},
            )
            _, unfinished = wait([final_future], timeout=0.05)
            assert unfinished == {final_future}
            release_append.set()
            appended = append_future.result(timeout=2)
            final_path = final_future.result(timeout=2)

        assert _read_jsonl(task.path)[0]["event_id"] == appended["event_id"]
        assert final_path.is_file()
        with pytest.raises(RecorderFinalizedError):
            task.append_event("step_started", {})
    finally:
        release_append.set()
        first.close()
        second.close()


def test_task_close_cannot_race_an_inflight_append(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, producer=_producer(), sync=False)
    recorder.write_manifest_start(_start_manifest(recorder.run_id))
    task = recorder.open_task()

    with ThreadPoolExecutor(max_workers=2) as executor:
        append_future = executor.submit(task.append_event, "task_started", {"large": "x" * 100_000})
        close_future = executor.submit(task.close)
        event = append_future.result()
        close_future.result()

    assert event["seq"] == 1
    assert _read_jsonl(task.path)[0]["event_id"] == event["event_id"]
    with pytest.raises(RecorderClosedError):
        task.append_event("step_started", {})
    recorder.close()


def test_recorder_refuses_to_append_after_partial_jsonl_tail(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, producer=_producer(), sync=False)
    recorder.write_manifest_start(_start_manifest(recorder.run_id))
    recorder.append_run_event("run_started", {})
    recorder.close()

    with (recorder.run_root / "run.events.jsonl").open("ab") as stream:
        stream.write(b'{"partial":')
    reopened = RunRecorder(tmp_path, producer=_producer(), run_id=recorder.run_id, sync=False)
    with pytest.raises(StreamCorruptionError):
        reopened.append_run_event("run_ended", {})
    reopened.close()


def test_events_require_start_manifest(tmp_path: Path) -> None:
    recorder = RunRecorder(tmp_path, producer=_producer())
    with pytest.raises(RuntimeError, match="manifest.start.json"):
        recorder.append_run_event("run_started", {})
    recorder.close()


def test_task_recorder_tracks_incomplete_artifacts_and_collector_errors(
    tmp_path: Path,
) -> None:
    recorder = RunRecorder(tmp_path, producer=_producer(), sync=False)
    recorder.write_manifest_start(_start_manifest(recorder.run_id))
    task = recorder.open_task()

    task.mark_incomplete("request_image", "request_image")
    error = task.append_event(
        "collector_error",
        {
            "scope": "model_request",
            "missing_artifacts": ["model_request", "request_image"],
        },
    )

    assert task.capture_complete is False
    assert task.missing_artifacts == ("request_image", "model_request")
    assert task.collector_error_event_ids == (error["event_id"],)
    recorder.close()
