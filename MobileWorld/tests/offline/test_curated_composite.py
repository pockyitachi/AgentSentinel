from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import mobile_world.offline.curated_composite as curated_module
from mobile_world.offline.curated_composite import (
    COMPOSITE_SCHEMA_VERSION,
    CompositeBuildError,
    TaskSourcePin,
    build_curated_composite,
    validate_curated_composite,
)


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ulid(seed: int) -> str:
    assert 0 <= seed <= 9999
    return f"01ARZ3NDEKTSV4RRFFQ69G{seed:04d}"


def _task_run_id(run_id: str, serial: int) -> str:
    return _ulid(int(run_id[-4:]) * 100 + serial)


def _event_id(run_id: str, serial: int, ordinal: int) -> str:
    return _ulid(int(run_id[-4:]) * 1000 + serial * 10 + ordinal)


def _summary(data: bytes) -> dict[str, Any]:
    return {"sha256": _sha(data), "byte_count": len(data)}


def _blob_ref(root: Path, data: bytes, media_type: str) -> dict[str, Any]:
    digest = _sha(data)
    relative = Path("blobs") / "sha256" / digest[:2] / digest
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(data),
        "media_type": media_type,
        "relative_path": relative.as_posix(),
    }


def _task(
    name: str,
    index: int,
    *,
    runtime: str = "completed",
    capture: bool = True,
    missing: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "index": index,
        "runtime": runtime,
        "capture": capture,
        "missing": missing or [],
    }


def _event(
    *,
    run_id: str,
    task_run_id: str,
    event_id: str,
    event_type: str,
    seq: int,
    payload: dict[str, Any],
    caused_by_event_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": "mobileworld.audit.event/v1",
        "event_id": event_id,
        "event_type": event_type,
        "run_id": run_id,
        "task_run_id": task_run_id,
        "stream_id": task_run_id,
        "seq": seq,
        "wall_time": f"2026-08-21T00:00:0{seq}Z",
        "monotonic_ns": seq,
        "caused_by_event_id": caused_by_event_id,
        "producer": {
            "component": "mobile_world.audit",
            "version": "test",
            "process_id": 1,
            "worker_id": "pytest",
        },
        "payload": payload,
    }


def _write_run(
    base: Path,
    relative: str,
    tasks: list[dict[str, Any]],
    *,
    global_runtime: str = "completed",
    global_capture: bool = True,
) -> Path:
    root = base / relative
    root.mkdir(parents=True)
    run_id = root.name
    start = {
        "raw_schema_version": "mobileworld.audit.event/v1",
        "run_id": run_id,
        "git_commit": "a" * 40,
        "git_dirty": True,
        "git_dirty_status": "reported",
        "agent_type": "mai_ui_agent",
        "model_name": "MAI-UI-8B",
        "environment_image": "mobile_world:test",
        "started_at_utc": "2026-08-21T00:00:00Z",
    }
    start_bytes = _canonical(start)
    (root / "manifest.start.json").write_bytes(start_bytes)
    run_bytes = b'{"event_type":"run_started"}\n'
    (root / "run.events.jsonl").write_bytes(run_bytes)

    summaries = []
    for serial, task in enumerate(tasks, start=1):
        task_run_id = _task_run_id(run_id, serial)
        leaf_ref = _blob_ref(root, f"leaf:{run_id}:{task_run_id}".encode(), "text/plain")
        graph_ref = _blob_ref(root, _canonical({"leaf": leaf_ref}), "application/json")
        missing = task["missing"]
        start_event = _event(
            run_id=run_id,
            task_run_id=task_run_id,
            event_id=_event_id(run_id, serial, 1),
            event_type="task_started",
            seq=1,
            caused_by_event_id=None,
            payload={
                "task_name": task["name"],
                "task_goal": f"goal for {task['name']}",
                "task_index": task["index"],
                "whole_task_attempt_index": 1,
            },
        )
        evidence_event = _event(
            run_id=run_id,
            task_run_id=task_run_id,
            event_id=_event_id(run_id, serial, 2),
            event_type="step_started",
            seq=2,
            caused_by_event_id=start_event["event_id"],
            payload={"artifact": graph_ref},
        )
        end_event = _event(
            run_id=run_id,
            task_run_id=task_run_id,
            event_id=_event_id(run_id, serial, 3),
            event_type="task_ended",
            seq=3,
            caused_by_event_id=evidence_event["event_id"],
            payload={
                "runtime_status": task["runtime"],
                "capture_complete": task["capture"],
                "missing_artifacts": missing,
                "collector_error_event_ids": [],
                "environment_evaluation": {
                    "score": 1.0 if task["runtime"] == "completed" else None,
                    "reason": "fixture",
                },
            },
        )
        stream_bytes = b"".join(
            _canonical(event) for event in (start_event, evidence_event, end_event)
        )
        relative_stream = Path("tasks") / task_run_id / "events.jsonl"
        stream_path = root / relative_stream
        stream_path.parent.mkdir(parents=True)
        stream_path.write_bytes(stream_bytes)
        summaries.append(
            {
                "task_run_id": task_run_id,
                "relative_path": relative_stream.as_posix(),
                **_summary(stream_bytes),
                "runtime_status": task["runtime"],
                "capture_complete": task["capture"],
                "missing_artifacts": missing,
                "collector_error_event_ids": [],
            }
        )

    final = {
        "raw_schema_version": "mobileworld.audit.event/v1",
        "run_id": run_id,
        "runtime_status": global_runtime,
        "capture_complete": global_capture,
        "missing_artifacts": [],
        "collector_error_event_ids": [],
        "manifest_start": _summary(start_bytes),
        "run_stream": _summary(run_bytes),
        "task_streams": summaries,
    }
    (root / "manifest.final.json").write_bytes(_canonical(final))
    return root


def _three_sources(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    base = tmp_path / "source-base"
    base.mkdir()
    old_relative = f"old/audit/raw/runs/{_ulid(1)}"
    rerun_relative = f"rerun/audit/raw/runs/{_ulid(2)}"
    final_relative = f"final/audit/raw/runs/{_ulid(3)}"
    _write_run(
        base,
        old_relative,
        [
            _task("TaskA", 1),
            _task("TaskB", 2, runtime="crashed"),
            _task("TaskC", 3, runtime="crashed"),
        ],
        global_runtime="crashed",
    )
    _write_run(
        base,
        rerun_relative,
        [_task("TaskB", 1), _task("TaskC", 2, capture=False, missing=["fixture-gap"])],
        global_capture=False,
    )
    _write_run(base, final_relative, [_task("TaskC", 1)])
    return base, {"old": old_relative, "rerun": rerun_relative, "final": final_relative}


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_task_events(run_root: Path, task_run_id: str) -> list[dict[str, Any]]:
    stream = run_root / "tasks" / task_run_id / "events.jsonl"
    return [json.loads(line) for line in stream.read_bytes().splitlines()]


def _rewrite_task_events(run_root: Path, task_run_id: str, events: list[dict[str, Any]]) -> None:
    stream_bytes = b"".join(_canonical(event) for event in events)
    stream = run_root / "tasks" / task_run_id / "events.jsonl"
    stream.write_bytes(stream_bytes)
    final_path = run_root / "manifest.final.json"
    final = json.loads(final_path.read_bytes())
    summary = next(item for item in final["task_streams"] if item["task_run_id"] == task_run_id)
    summary.update(_summary(stream_bytes))
    final_path.write_bytes(_canonical(final))


def test_builds_zero_copy_manifest_with_canonical_indices(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    before = _tree_hash(base)
    output = base / "curated"

    manifest_path, report_path, manifest, report = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=output,
        dataset_id="fixture-curated",
    )

    assert manifest_path.is_file()
    assert report_path.is_file()
    assert sorted(path.name for path in output.iterdir()) == [
        "manifest.json",
        "validation_report.json",
    ]
    assert manifest["schema_version"] == COMPOSITE_SCHEMA_VERSION
    assert manifest["artifact_type"] == "derived_task_selection"
    assert manifest["is_raw_run"] is False
    assert "run_id" not in manifest
    assert "created_at_utc" not in manifest
    assert [entry["canonical_suite_index"] for entry in manifest["tasks"]] == [1, 2, 3]
    assert [entry["source_task_index"] for entry in manifest["tasks"]] == [1, 1, 1]
    assert [entry["source_id"] for entry in manifest["tasks"]] == ["old", "rerun", "final"]
    assert manifest["counts"]["task_count_by_source"] == {"old": 1, "rerun": 1, "final": 1}
    assert manifest["selection_policy"]["candidate_resolution"] == (
        "exactly_one_eligible_stream_per_canonical_task"
    )
    assert "task_source_pins" not in manifest["selection_policy"]
    assert "task_source_pin_count" not in manifest["counts"]
    assert report["valid"] is True
    assert report["checks"]["selected_transitive_blob_sha256_valid"] is True
    assert not (output / "blobs").exists()

    # Account for the new derived directory; every pre-existing source byte is unchanged.
    output.rename(tmp_path / "detached-output")
    assert _tree_hash(base) == before


def test_manifest_is_deterministic_for_same_inputs_and_dataset_id(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "nested" / "at-another-depth" / "second"
    second.parent.mkdir(parents=True)
    _, _, first_manifest, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=first,
        dataset_id="same-id",
    )
    _, _, second_manifest, _ = build_curated_composite(
        source_base=base,
        sources=dict(reversed(list(sources.items()))),
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=second,
        dataset_id="same-id",
    )
    assert first_manifest == second_manifest
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()
    assert "source_base_relative_to_manifest_dir" not in first_manifest["source_locator"]


def test_ambiguity_fails_before_output_is_created(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    _write_run(base, duplicate_relative, [_task("TaskA", 1)])
    sources["duplicate"] = duplicate_relative
    output = base / "must-not-exist"

    with pytest.raises(CompositeBuildError, match="exactly one") as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
        )

    assert raised.value.code == "eligible_task_cardinality"
    assert not output.exists()


def test_exact_task_source_pin_resolves_only_the_named_ambiguity(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    duplicate_root = _write_run(base, duplicate_relative, [_task("TaskA", 1)])
    sources["duplicate"] = duplicate_relative
    duplicate_task_run_id = _task_run_id(duplicate_root.name, 1)
    before = _tree_hash(base)
    output = base / "pinned-curated"

    manifest_path, _, manifest, report = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=output,
        task_source_pins=(
            TaskSourcePin(
                canonical_suite_index=1,
                task_name="TaskA",
                source_id="duplicate",
                source_task_run_id=duplicate_task_run_id,
            ),
        ),
    )

    assert [task["canonical_suite_index"] for task in manifest["tasks"]] == [1, 2, 3]
    assert [task["source_id"] for task in manifest["tasks"]] == [
        "duplicate",
        "rerun",
        "final",
    ]
    assert manifest["counts"]["task_count"] == 3
    assert manifest["counts"]["task_source_pin_count"] == 1
    assert manifest["selection_policy"]["candidate_resolution"] == (
        "exact_task_source_pin_else_exactly_one_eligible_stream_per_canonical_task"
    )
    assert manifest["selection_policy"]["task_source_pins"] == [
        {
            "canonical_suite_index": 1,
            "task_name": "TaskA",
            "source_id": "duplicate",
            "source_task_run_id": duplicate_task_run_id,
        }
    ]
    assert report["valid"] is True
    assert report["checks"]["explicit_task_source_pins_matched_exactly"] is True
    assert (
        validate_curated_composite(
            manifest_path=manifest_path,
            source_base=base,
        )["valid"]
        is True
    )

    output.rename(tmp_path / "detached-pinned-output")
    assert _tree_hash(base) == before


def test_task_source_pin_never_falls_back_to_an_unpinned_candidate(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    _write_run(base, duplicate_relative, [_task("TaskA", 1)])
    sources["duplicate"] = duplicate_relative
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
            task_source_pins=(
                TaskSourcePin(
                    canonical_suite_index=1,
                    task_name="TaskA",
                    source_id="duplicate",
                    source_task_run_id=_ulid(9999),
                ),
            ),
        )

    assert raised.value.code == "task_source_pin_match_cardinality"
    assert not output.exists()


def test_task_source_pin_never_falls_back_from_an_ineligible_stream(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
            task_source_pins=(
                TaskSourcePin(
                    canonical_suite_index=2,
                    task_name="TaskB",
                    source_id="old",
                    source_task_run_id=_task_run_id(old_root.name, 2),
                ),
            ),
        )

    assert raised.value.code == "task_source_pin_match_cardinality"
    assert raised.value.context["eligible_candidates"] == [
        {
            "source_id": "rerun",
            "source_task_run_id": _task_run_id((base / sources["rerun"]).name, 1),
        }
    ]
    assert not output.exists()


def test_task_source_pin_does_not_relax_an_unpinned_task_ambiguity(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    duplicate_root = _write_run(
        base,
        duplicate_relative,
        [_task("TaskA", 1), _task("TaskB", 2)],
    )
    sources["duplicate"] = duplicate_relative
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
            task_source_pins=(
                TaskSourcePin(
                    canonical_suite_index=1,
                    task_name="TaskA",
                    source_id="duplicate",
                    source_task_run_id=_task_run_id(duplicate_root.name, 1),
                ),
            ),
        )

    assert raised.value.code == "eligible_task_cardinality"
    assert raised.value.context["task_name"] == "TaskB"
    assert not output.exists()


@pytest.mark.parametrize(
    ("canonical_suite_index", "task_name"),
    [(2, "TaskA"), (1, "TaskB")],
)
def test_task_source_pin_binds_exact_canonical_name_and_index(
    tmp_path: Path, canonical_suite_index: int, task_name: str
) -> None:
    base, sources = _three_sources(tmp_path)
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
            task_source_pins=(
                TaskSourcePin(
                    canonical_suite_index=canonical_suite_index,
                    task_name=task_name,
                    source_id="old",
                    source_task_run_id=_task_run_id((base / sources["old"]).name, 1),
                ),
            ),
        )

    assert raised.value.code == "task_source_pin_catalog_mismatch"
    assert not output.exists()


def test_duplicate_task_source_pins_fail_before_scanning_sources(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
            task_source_pins=(
                TaskSourcePin(1, "TaskA", "old", _task_run_id(old_root.name, 1)),
                TaskSourcePin(1, "TaskB", "rerun", _task_run_id(old_root.name, 2)),
            ),
        )

    assert raised.value.code == "task_source_pin_duplicate_canonical_index"
    assert not output.exists()


def test_validation_replays_manifest_task_source_pins_fail_closed(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    duplicate_root = _write_run(base, duplicate_relative, [_task("TaskA", 1)])
    sources["duplicate"] = duplicate_relative
    manifest_path, _, manifest, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=tmp_path / "curated",
        task_source_pins=(
            TaskSourcePin(
                1,
                "TaskA",
                "duplicate",
                _task_run_id(duplicate_root.name, 1),
            ),
        ),
    )
    manifest["selection_policy"]["task_source_pins"][0]["source_task_run_id"] = _ulid(9999)
    manifest_path.write_bytes(_canonical(manifest))

    report = validate_curated_composite(manifest_path=manifest_path, source_base=base)

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "task_source_pin_match_cardinality"


def test_cli_accepts_exact_task_source_pin_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base, sources = _three_sources(tmp_path)
    duplicate_relative = f"duplicate/audit/raw/runs/{_ulid(4)}"
    duplicate_root = _write_run(base, duplicate_relative, [_task("TaskA", 1)])
    sources["duplicate"] = duplicate_relative
    output = tmp_path / "cli-curated"

    arguments = ["build", "--source-base", str(base)]
    for source_id, relative in sources.items():
        arguments.extend(["--source", f"{source_id}={relative}"])
    arguments.extend(
        [
            "--catalog-source",
            "old",
            "--expected-task-count",
            "3",
            "--task-source-pin",
            f"1:TaskA=duplicate:{_task_run_id(duplicate_root.name, 1)}",
            "--output-dir",
            str(output),
        ]
    )

    assert curated_module.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["tasks"][0]["source_id"] == "duplicate"


def test_validation_detects_transitive_blob_tampering(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    output = base / "curated"
    manifest_path, _, manifest, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=output,
    )
    source = next(item for item in manifest["sources"] if item["source_id"] == "old")
    old_root = base / source["relative_run_path"]
    task_run_id = _task_run_id(old_root.name, 1)
    leaf = next(
        path
        for path in (old_root / "blobs" / "sha256").rglob("*")
        if path.is_file() and path.read_bytes() == f"leaf:{old_root.name}:{task_run_id}".encode()
    )
    original = leaf.read_bytes()
    leaf.write_bytes(bytes([original[0] ^ 1]) + original[1:])

    report = validate_curated_composite(
        manifest_path=manifest_path,
        source_base=base,
        verify_blob_digests=True,
    )

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "blob_digest_mismatch"


def test_rejects_output_under_a_source_raw_root(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    output = base / "old" / "audit" / "raw" / "derived-is-forbidden"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
        )

    assert raised.value.code == "output_inside_raw_root"
    assert not output.exists()


def test_validation_rejects_noncanonical_manifest_bytes(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    manifest_path, _, manifest, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=tmp_path / "curated",
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    report = validate_curated_composite(manifest_path=manifest_path, source_base=base)

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "manifest_not_canonical"


def test_validation_rejects_invalid_dataset_id_in_canonical_manifest(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    manifest_path, _, manifest, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=tmp_path / "curated",
    )
    manifest["dataset_id"] = "../../invalid"
    manifest_path.write_bytes(_canonical(manifest))

    report = validate_curated_composite(manifest_path=manifest_path, source_base=base)

    assert report["valid"] is False
    assert report["errors"][0]["code"] == "dataset_id_invalid"


def test_rejects_undeclared_physical_task_stream(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    extra = old_root / "tasks" / "undeclared-task" / "events.jsonl"
    extra.parent.mkdir()
    extra.write_bytes(b"{}\n")
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
        )

    assert raised.value.code == "physical_task_layout_mismatch"
    assert not output.exists()


@pytest.mark.parametrize("symlink_kind", ["source", "tasks-root", "task-child", "blob-prefix"])
def test_rejects_symlink_components_beneath_source(tmp_path: Path, symlink_kind: str) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    outside = tmp_path / f"outside-{symlink_kind}"
    if symlink_kind == "source":
        alias = base / "source-alias"
        alias.symlink_to(old_root, target_is_directory=True)
        sources["old"] = alias.relative_to(base).as_posix()
    elif symlink_kind == "tasks-root":
        (old_root / "tasks").rename(outside)
        (old_root / "tasks").symlink_to(outside, target_is_directory=True)
    elif symlink_kind == "task-child":
        task_child = old_root / "tasks" / _task_run_id(old_root.name, 1)
        task_child.rename(outside)
        task_child.symlink_to(outside, target_is_directory=True)
    else:
        events = _read_task_events(old_root, _task_run_id(old_root.name, 1))
        blob_relative = Path(events[1]["payload"]["artifact"]["relative_path"])
        digest_prefix = old_root / blob_relative.parent
        digest_prefix.rename(outside)
        digest_prefix.symlink_to(outside, target_is_directory=True)
    output = tmp_path / "must-not-exist"

    with pytest.raises(CompositeBuildError):
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
        )

    assert not output.exists()


@pytest.mark.parametrize("injected_type", ["collector_error", "task_started", "task_ended"])
def test_selected_stream_rejects_inconsistent_lifecycle_events(
    tmp_path: Path, injected_type: str
) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    task_run_id = _task_run_id(old_root.name, 1)
    events = _read_task_events(old_root, task_run_id)
    injected = _event(
        run_id=old_root.name,
        task_run_id=task_run_id,
        event_id=_ulid(
            {"collector_error": 9001, "task_started": 9002, "task_ended": 9003}[injected_type]
        ),
        event_type=injected_type,
        seq=3,
        caused_by_event_id=events[1]["event_id"],
        payload=(
            {
                "scope": "fixture",
                "related_event_id": None,
                "step_id": None,
                "exception": {"class": "FixtureError", "message": "fixture"},
                "missing_artifacts": [],
                "agent_execution_continued": True,
            }
            if injected_type == "collector_error"
            else dict(events[0 if injected_type == "task_started" else -1]["payload"])
        ),
    )
    events[-1]["seq"] = 4
    events[-1]["caused_by_event_id"] = injected["event_id"]
    events.insert(-1, injected)
    _rewrite_task_events(old_root, task_run_id, events)

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=tmp_path / "must-not-exist",
        )

    expected = (
        "selected_stream_collector_errors_mismatch"
        if injected_type == "collector_error"
        else "selected_stream_lifecycle_invalid"
    )
    assert raised.value.code == expected


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("event_type", "invented_event"), ("wall_time", "not-rfc3339")],
)
def test_selected_stream_uses_authoritative_envelope_validation(
    tmp_path: Path, field: str, invalid_value: str
) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    task_run_id = _task_run_id(old_root.name, 1)
    events = _read_task_events(old_root, task_run_id)
    events[1][field] = invalid_value
    _rewrite_task_events(old_root, task_run_id, events)

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=tmp_path / "must-not-exist",
        )

    assert raised.value.code == "selected_stream_event_schema_invalid"


def test_later_json_media_type_still_expands_transitive_closure(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    old_root = base / sources["old"]
    task_run_id = _task_run_id(old_root.name, 1)
    events = _read_task_events(old_root, task_run_id)
    graph_ref = events[1]["payload"]["artifact"]
    graph = json.loads((old_root / graph_ref["relative_path"]).read_bytes())
    leaf_path = old_root / graph["leaf"]["relative_path"]
    leaf_bytes = leaf_path.read_bytes()
    leaf_path.write_bytes(bytes([leaf_bytes[0] ^ 1]) + leaf_bytes[1:])
    non_json_ref = {**graph_ref, "media_type": "text/plain"}
    json_ref = {**graph_ref, "media_type": "text/json"}
    events[1]["payload"] = {"artifacts": [non_json_ref, json_ref]}
    _rewrite_task_events(old_root, task_run_id, events)

    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=tmp_path / "must-not-exist",
        )

    assert raised.value.code == "blob_digest_mismatch"


def test_publish_race_preserves_exclusive_empty_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, sources = _three_sources(tmp_path)
    output = tmp_path / "racing-output"
    original_publish = curated_module._rename_directory_noreplace
    raced_inode: list[int] = []

    def publish_after_race(staging: Path, target: Path) -> None:
        target.mkdir()
        raced_inode.append(target.stat().st_ino)
        original_publish(staging, target)

    monkeypatch.setattr(curated_module, "_rename_directory_noreplace", publish_after_race)
    with pytest.raises(CompositeBuildError) as raised:
        build_curated_composite(
            source_base=base,
            sources=sources,
            catalog_source_id="old",
            expected_task_count=3,
            output_dir=output,
        )

    assert raised.value.code == "output_exists"
    assert output.is_dir()
    assert output.stat().st_ino == raced_inode[0]
    assert list(output.iterdir()) == []
    assert list(tmp_path.glob(".racing-output.staging-*")) == []


def test_cli_report_rejects_source_raw_location(tmp_path: Path) -> None:
    base, sources = _three_sources(tmp_path)
    manifest_path, _, _, _ = build_curated_composite(
        source_base=base,
        sources=sources,
        catalog_source_id="old",
        expected_task_count=3,
        output_dir=tmp_path / "curated",
    )
    report_path = base / "old" / "audit" / "raw" / "forbidden-report.json"
    before = _tree_hash(base)

    exit_code = curated_module.main(
        [
            "validate",
            "--manifest",
            str(manifest_path),
            "--source-base",
            str(base),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 2
    assert not report_path.exists()
    assert _tree_hash(base) == before
