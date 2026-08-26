from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.integrity import IntegrityChecker, check_run_integrity
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer


def _manifest(run_id: str) -> dict[str, Any]:
    return {
        "raw_schema_version": "mobileworld.audit.event/v1",
        "run_id": run_id,
        "repository": "Tongyi-MAI/MobileWorld",
        "git_commit": "a" * 40,
        "git_dirty": False,
        "python_version": "3.12.0",
        "mobile_world_version": "0.1.0",
        "agent_type": "test_agent",
        "model_name": "test-model",
        "suite_family": "mobile_world",
        "resolved_cli_config": {},
        "resolved_agent_runtime_config": {},
        "environment_image": "test-image",
        "started_at_utc": "2026-08-19T00:00:00Z",
        "collection_policy": {
            "label_free": True,
            "prompt_intervention": False,
            "collector_mode": "fail_open_with_incomplete_marker",
            "stream_chunks": True,
        },
    }


def _build_run(
    tmp_path: Path,
    *,
    include_model: bool = False,
    request_view: dict[str, Any] | None = None,
    configured_blob: bytes | None = None,
    request_image_data_url: str | None = None,
    chunk_index: int = 0,
    duplicate_model_terminal: bool = False,
) -> dict[str, Any]:
    recorder = RunRecorder(
        tmp_path,
        producer=Producer.local(version="test", worker_id="worker-1"),
        sync=False,
    )
    recorder.write_manifest_start(_manifest(recorder.run_id))
    run_started = recorder.append_run_event("run_started", {})
    task = recorder.open_task()
    task_started = task.append_event(
        "task_started",
        {
            "task_name": "FixtureTask",
            "task_goal": "perform the exact fixture task",
            "task_goal_status": "resolved",
            "task_index": 1,
            "suite_family": "mobile_world",
            "agent": {
                "adapter": "test_agent",
                "model": "test-model",
                "configuration": {},
            },
            "environment": {
                "backend_id": "backend-fixture",
                "device_id": "device-fixture",
            },
            "whole_task_attempt_index": 1,
        },
    )
    step_id = new_ulid()
    observation: dict[str, Any] = {}
    configured_ref = None
    if configured_blob is not None:
        configured_ref = recorder.blob_store.put_bytes(configured_blob, "application/octet-stream")
        observation["opaque_blob"] = configured_ref
    step = task.append_event(
        "step_started",
        {"step_id": step_id, "step_index": 1, "observation": observation},
        task_started["event_id"],
    )

    model_call_id = new_ulid()
    last_event = step
    request_images: list[dict[str, Any]] = []
    if include_model:
        request_id = new_ulid()
        retry_group_id = new_ulid()
        logical_request_view = request_view or {"model": "test-model", "messages": []}
        if request_image_data_url is not None:
            logical_request_view = {
                "model": "test-model",
                "messages": [{"content": [{"image_url": {"url": request_image_data_url}}]}],
            }
        snapshot = ArtifactSerializer(recorder.blob_store).snapshot_sdk_arguments(
            logical_request_view
        )
        request_images = list(snapshot.request_images)
        request_payload = {
            "step_id": step_id,
            "model_call_id": model_call_id,
            "retry_group_id": retry_group_id,
            "adapter_attempt_index": 1,
            "request_id": request_id,
            "attempt_index": 1,
            "sdk": {"package": "openai", "version": "test", "method": "create"},
            "endpoint": {"origin": "https://example.invalid", "path": "/v1"},
            "stream": True,
            "sdk_arguments_snapshot_blob": snapshot.snapshot_blob,
            "request_view": snapshot.request_view,
            "request_images": request_images,
        }
        request = task.append_event(
            "model_request", request_payload, caused_by_event_id=step["event_id"]
        )
        raw_chunk = recorder.blob_store.put_bytes(b"{}", "application/json")
        chunk = task.append_event(
            "model_stream_chunk",
            {
                "step_id": step_id,
                "model_call_id": model_call_id,
                "retry_group_id": retry_group_id,
                "adapter_attempt_index": 1,
                "request_id": request_id,
                "attempt_index": 1,
                "chunk_index": chunk_index,
                "raw_chunk_snapshot_blob": raw_chunk,
                "chunk_view": {},
            },
            caused_by_event_id=request["event_id"],
        )
        response_payload = {
            "step_id": step_id,
            "model_call_id": model_call_id,
            "retry_group_id": retry_group_id,
            "adapter_attempt_index": 1,
            "request_id": request_id,
            "attempt_index": 1,
            "response_mode": "stream",
            "raw_response": {
                "kind": "stream_chunks",
                "snapshot_blob": None,
                "chunk_event_ids": [chunk["event_id"]],
                "chunk_count": 1,
            },
            "raw_response_view": None,
            "normalized_response": {},
            "returned_value_snapshot_blob": None,
            "stream_state": "complete",
        }
        response = task.append_event(
            "model_response", response_payload, caused_by_event_id=chunk["event_id"]
        )
        last_event = response
        if duplicate_model_terminal:
            last_event = task.append_event(
                "model_attempt_failed",
                {
                    "step_id": step_id,
                    "model_call_id": model_call_id,
                    "retry_group_id": retry_group_id,
                    "adapter_attempt_index": 1,
                    "request_id": request_id,
                    "attempt_index": 1,
                    "partial_chunk_event_ids": [chunk["event_id"]],
                },
                caused_by_event_id=response["event_id"],
            )

    decision_id = new_ulid()
    decision = task.append_event(
        "agent_decision",
        {
            "step_id": step_id,
            "decision_id": decision_id,
            "source_model_call_ids": [model_call_id] if include_model else [],
        },
        caused_by_event_id=last_event["event_id"],
    )
    execution_id = new_ulid()
    execution = task.append_event(
        "action_execution_started",
        {"step_id": step_id, "decision_id": decision_id, "execution_id": execution_id},
        caused_by_event_id=decision["event_id"],
    )
    transition = task.append_event(
        "transition_completed",
        {
            "step_id": step_id,
            "decision_id": decision_id,
            "execution_id": execution_id,
            "pre_observation_event_id": step["event_id"],
            "action_execution_event_id": execution["event_id"],
            "post_observation": {},
        },
        caused_by_event_id=execution["event_id"],
    )
    task.append_event(
        "task_ended",
        {
            "runtime_status": "completed",
            "termination": {
                "source": "agent_terminal_action",
                "step_index": 1,
                "exception": None,
            },
            "environment_evaluation": {
                "score": 1.0,
                "reason": "exact fixture reason",
                "exception": None,
            },
            "teardown": {
                "returned": True,
                "result_snapshot_blob": None,
                "exception": None,
            },
            "token_usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cached_tokens": 0,
                "total_tokens": 12,
            },
            "capture_complete": True,
            "missing_artifacts": [],
            "collector_error_event_ids": [],
        },
        caused_by_event_id=transition["event_id"],
    )
    task.close()
    recorder.append_run_event(
        "run_ended",
        {
            "runtime_status": "completed",
            "task_run_ids": [task.task_run_id],
            "task_counts": {"started": 1, "completed": 1, "crashed": 0},
            "capture_complete": True,
            "collector_error_event_ids": [],
            "manifest_final_path": "manifest.final.json",
        },
        caused_by_event_id=run_started["event_id"],
    )
    blob_count, blob_byte_count = _blob_summary(recorder.run_root / "blobs" / "sha256")
    recorder.write_manifest_final(
        {
            "ended_at_utc": "2026-08-19T00:01:00Z",
            "runtime_status": "completed",
            "manifest_start": _file_summary(recorder.manifest_start_path),
            "run_stream": _file_summary(recorder.run_root / "run.events.jsonl"),
            "task_streams": [
                {
                    "task_run_id": task.task_run_id,
                    "relative_path": f"tasks/{task.task_run_id}/events.jsonl",
                    **_file_summary(task.path),
                    "runtime_status": "completed",
                    "retry_planned": False,
                    "capture_complete": True,
                    "missing_artifacts": [],
                    "collector_error_event_ids": [],
                }
            ],
            "blob_count": blob_count,
            "blob_byte_count": blob_byte_count,
            "capture_complete": True,
            "missing_artifacts": [],
            "collector_error_event_ids": [],
        }
    )
    recorder.close()
    return {
        "run_root": recorder.run_root,
        "task_events": task.path,
        "configured_ref": configured_ref,
        "request_images": request_images,
    }


def _error_codes(report: dict[str, Any]) -> set[str]:
    return {error["code"] for error in report["errors"]}


def _file_summary(path: Path) -> dict[str, int | str]:
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "byte_count": len(data)}


def _blob_summary(root: Path) -> tuple[int, int]:
    paths = [path for path in root.rglob("*") if path.is_file()] if root.is_dir() else []
    return len(paths), sum(path.stat().st_size for path in paths)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )


def _read_final(run_root: Path) -> dict[str, Any]:
    return json.loads((run_root / "manifest.final.json").read_text(encoding="utf-8"))


def _write_final(run_root: Path, final: dict[str, Any]) -> None:
    (run_root / "manifest.final.json").write_text(
        json.dumps(final, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _refresh_stream_summary(run_root: Path, stream_path: Path) -> None:
    final = _read_final(run_root)
    if stream_path == run_root / "run.events.jsonl":
        final["run_stream"] = _file_summary(stream_path)
    else:
        relative_path = stream_path.relative_to(run_root).as_posix()
        summary = next(
            item for item in final["task_streams"] if item["relative_path"] == relative_path
        )
        summary.update(_file_summary(stream_path))
    _write_final(run_root, final)


def test_valid_run_produces_machine_readable_report(tmp_path: Path) -> None:
    built = _build_run(tmp_path, include_model=True)
    checker = IntegrityChecker(built["run_root"])
    report = checker.check()

    assert report["valid"] is True
    assert report["errors"] == []
    assert report["counts"]["event_count"] == 11
    assert report["counts"]["model_request_count"] == 1
    report_path = checker.write_report(report)
    assert json.loads(report_path.read_text(encoding="utf-8"))["valid"] is True
    with pytest.raises(FileExistsError):
        checker.write_report(report)


def test_checker_reports_noncontiguous_seq_and_partial_tail(tmp_path: Path) -> None:
    built = _build_run(tmp_path)
    path = built["task_events"]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[1]["seq"] = 99
    path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    report = check_run_integrity(built["run_root"])
    assert report["valid"] is False
    assert {"incomplete_jsonl_tail", "non_contiguous_seq"} <= _error_codes(report)
    assert "task_capture_complete_inconsistent" in _error_codes(report)


def test_checker_enforces_model_terminal_pair_and_chunk_index(tmp_path: Path) -> None:
    built = _build_run(
        tmp_path,
        include_model=True,
        chunk_index=1,
        duplicate_model_terminal=True,
    )
    report = check_run_integrity(built["run_root"])

    assert report["valid"] is False
    assert {"model_terminal_count", "non_contiguous_chunk_index"} <= _error_codes(report)


def test_opaque_tool_api_key_property_is_allowed_but_exact_secret_is_not(tmp_path: Path) -> None:
    harmless = _build_run(
        tmp_path / "harmless",
        include_model=True,
        request_view={
            "model": "test-model",
            "messages": [],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "configure",
                        "parameters": {
                            "type": "object",
                            "properties": {"api_key": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    )
    harmless_report = check_run_integrity(harmless["run_root"])
    assert harmless_report["valid"] is True
    assert "credential_key_present" not in _error_codes(harmless_report)
    placeholder_report = check_run_integrity(harmless["run_root"], configured_secrets=["EMPTY"])
    assert placeholder_report["valid"] is True
    assert "configured_secret_present" not in _error_codes(placeholder_report)

    secret = "sk-live-DO-NOT-LOG"
    leaked = _build_run(
        tmp_path / "leaked",
        include_model=True,
        request_view={"model": "test-model", "messages": [{"content": f"Bearer {secret}"}]},
    )
    leaked_report = check_run_integrity(leaked["run_root"], configured_secrets=[secret])
    assert "configured_secret_present" in _error_codes(leaked_report)
    assert secret not in json.dumps(leaked_report)


def test_checker_verifies_blob_bytes_and_scans_opaque_blobs(tmp_path: Path) -> None:
    secret = b"opaque-configured-secret"
    leaked = _build_run(tmp_path / "secret", configured_blob=secret)
    leaked_report = check_run_integrity(leaked["run_root"], configured_secrets=[secret])
    assert "configured_secret_present" in _error_codes(leaked_report)

    corrupted = _build_run(tmp_path / "corrupt", configured_blob=b"original")
    reference = corrupted["configured_ref"]
    blob_path = corrupted["run_root"] / reference["relative_path"]
    blob_path.write_bytes(b"tampered")
    corrupted_report = check_run_integrity(corrupted["run_root"])
    assert "blob_integrity" in _error_codes(corrupted_report)


def test_artifact_rehydration_detects_missing_nested_request_image_blob(tmp_path: Path) -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXQ"
        "AAAAASUVORK5CYII="
    )
    built = _build_run(
        tmp_path,
        include_model=True,
        request_image_data_url=png_data_url,
    )
    content_ref = built["request_images"][0]["content_blob"]
    (built["run_root"] / content_ref["relative_path"]).unlink()

    report = check_run_integrity(built["run_root"])
    assert "artifact_graph_integrity" in _error_codes(report)
    assert "blob_integrity" in _error_codes(report)


def test_collector_metadata_credential_key_is_rejected(tmp_path: Path) -> None:
    built = _build_run(tmp_path, include_model=True)
    path = built["task_events"]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    request = next(event for event in events if event["event_type"] == "model_request")
    request["payload"]["endpoint"]["authorization"] = "removed"
    path.write_text(
        "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
        encoding="utf-8",
    )

    report = check_run_integrity(built["run_root"])
    assert "credential_key_present" in _error_codes(report)


@pytest.mark.parametrize("target", ["manifest_start", "run_stream", "task_stream"])
def test_final_manifest_file_summaries_detect_legal_json_tampering(
    tmp_path: Path,
    target: str,
) -> None:
    built = _build_run(tmp_path)
    run_root = built["run_root"]

    if target == "manifest_start":
        path = run_root / "manifest.start.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["model_name"] = "semantically-valid-tamper"
        path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    elif target == "run_stream":
        path = run_root / "run.events.jsonl"
        events = _read_jsonl(path)
        events[0]["payload"]["semantically_valid_extra"] = True
        _write_jsonl(path, events)
    else:
        path = built["task_events"]
        events = _read_jsonl(path)
        events[0]["payload"]["task_goal"] = "a different but still valid exact goal"
        _write_jsonl(path, events)

    report = check_run_integrity(run_root)
    assert "manifest_file_summary_mismatch" in _error_codes(report)


def test_request_view_crosscheck_handles_externalized_data_url_and_detects_drift(
    tmp_path: Path,
) -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXQ"
        "AAAAASUVORK5CYII="
    )
    built = _build_run(tmp_path, include_model=True, request_image_data_url=png_data_url)
    assert check_run_integrity(built["run_root"])["valid"] is True

    events = _read_jsonl(built["task_events"])
    request = next(event for event in events if event["event_type"] == "model_request")
    request["payload"]["request_view"]["model"] = "drifted-inspectable-view"
    _write_jsonl(built["task_events"], events)
    _refresh_stream_summary(built["run_root"], built["task_events"])

    report = check_run_integrity(built["run_root"])
    assert "request_view_artifact_mismatch" in _error_codes(report)
    assert "manifest_file_summary_mismatch" not in _error_codes(report)


def test_request_view_crosscheck_preserves_json_numeric_types(tmp_path: Path) -> None:
    built = _build_run(
        tmp_path,
        include_model=True,
        request_view={"model": "test-model", "messages": [], "temperature": 1},
    )
    events = _read_jsonl(built["task_events"])
    request = next(event for event in events if event["event_type"] == "model_request")
    request["payload"]["request_view"]["temperature"] = 1.0
    _write_jsonl(built["task_events"], events)
    _refresh_stream_summary(built["run_root"], built["task_events"])

    report = check_run_integrity(built["run_root"])
    assert "request_view_artifact_mismatch" in _error_codes(report)


def test_skeletal_task_evidence_and_invalid_token_usage_are_rejected(tmp_path: Path) -> None:
    built = _build_run(tmp_path)
    events = _read_jsonl(built["task_events"])
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_ended = next(event for event in events if event["event_type"] == "task_ended")
    task_started["payload"].pop("task_goal")
    task_ended["payload"]["environment_evaluation"].pop("reason")
    task_ended["payload"]["teardown"].pop("exception")
    task_ended["payload"]["token_usage"] = {"total_tokens": -1}
    _write_jsonl(built["task_events"], events)
    _refresh_stream_summary(built["run_root"], built["task_events"])

    report = check_run_integrity(built["run_root"])
    assert {
        "missing_required_payload_fields",
        "invalid_task_ended_payload",
    } <= _error_codes(report)


def test_model_request_step_and_decision_model_sources_are_cross_checked(
    tmp_path: Path,
) -> None:
    wrong_step = _build_run(tmp_path / "wrong-step", include_model=True)
    events = _read_jsonl(wrong_step["task_events"])
    request = next(event for event in events if event["event_type"] == "model_request")
    request["payload"]["step_id"] = new_ulid()
    _write_jsonl(wrong_step["task_events"], events)
    _refresh_stream_summary(wrong_step["run_root"], wrong_step["task_events"])
    wrong_step_codes = _error_codes(check_run_integrity(wrong_step["run_root"]))
    assert "model_request_step_reference" in wrong_step_codes
    assert "decision_model_sources_mismatch" in wrong_step_codes

    missing_source = _build_run(tmp_path / "missing-source", include_model=True)
    events = _read_jsonl(missing_source["task_events"])
    decision = next(event for event in events if event["event_type"] == "agent_decision")
    decision["payload"]["source_model_call_ids"] = []
    _write_jsonl(missing_source["task_events"], events)
    _refresh_stream_summary(missing_source["run_root"], missing_source["task_events"])
    missing_source_codes = _error_codes(check_run_integrity(missing_source["run_root"]))
    assert "decision_model_sources_mismatch" in missing_source_codes


def test_final_task_list_counts_and_capture_status_are_cross_checked(tmp_path: Path) -> None:
    built = _build_run(tmp_path)
    final = _read_final(built["run_root"])
    final["task_streams"] = []
    final["capture_complete"] = False
    _write_final(built["run_root"], final)

    report = check_run_integrity(built["run_root"])
    assert {
        "manifest_task_stream_set_mismatch",
        "run_task_list_mismatch",
        "run_task_counts_mismatch",
        "run_final_manifest_mismatch",
        "manifest_capture_status_mismatch",
    } <= _error_codes(report)


def test_final_manifest_must_reference_run_level_collector_errors(tmp_path: Path) -> None:
    built = _build_run(tmp_path)
    run_path = built["run_root"] / "run.events.jsonl"
    run_started, run_ended = _read_jsonl(run_path)
    collector_error = {
        **run_started,
        "event_id": new_ulid(),
        "event_type": "collector_error",
        "seq": 2,
        "caused_by_event_id": run_started["event_id"],
        "payload": {
            "scope": "run_finalization",
            "related_event_id": run_started["event_id"],
            "step_id": None,
            "exception": {"class": "RuntimeError", "message": "safe fixture failure"},
            "missing_artifacts": ["fixture_artifact"],
            "agent_execution_continued": True,
        },
    }
    run_ended["seq"] = 3
    _write_jsonl(run_path, [run_started, collector_error, run_ended])
    _refresh_stream_summary(built["run_root"], run_path)

    report = check_run_integrity(built["run_root"])
    assert "manifest_collector_error_references" in _error_codes(report)
