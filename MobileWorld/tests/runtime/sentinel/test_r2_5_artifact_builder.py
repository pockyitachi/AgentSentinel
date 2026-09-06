from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _r2_4_topology_fixture import write_cpu_topology_artifact
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    RefResolver,
    ValidationError,
)

import mobile_world.runtime.sentinel.r2_5.artifact_builder as artifact_builder_module
from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.authority_promotion import (
    AuthorityPromotionError,
    load_canonical_draft_authority_v1,
    promote_draft_authority_v1,
    write_fresh_owner_authority_v1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import (
    OpenAIRoleV1,
    RunAuthorizationStatusV1,
    SmokeModeV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    ARTIFACT_BUNDLE_FILENAME,
    COHORT_SELECTION_ALGORITHM,
    COHORT_SELECTION_FILENAME,
    FROZEN_PILOT_MANIFEST_FILENAME,
    GUI_ONLY_TASK_SOURCE_FILENAME,
    GUI_ONLY_TASK_SOURCE_TASK_COUNT,
    GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE,
    PILOT_TASK_SOURCE_FILENAME,
    RUN_AUTHORITY_MANIFEST_FILENAME,
    TOPOLOGY_COMPARISON_FILENAME,
    AuthorityArtifactInputsV1,
    R25ArtifactBuildError,
    RegistryTaskMetadataV1,
    RegistryTaskTimeDependencyV1,
    SnapshotDeclarationV1,
    artifact_bundle_output,
    artifact_bundle_projection,
    build_authority_artifact_bundle,
    cohort_selection_projection,
    cohort_selection_sha256,
    current_registry_metadata,
    freeze_gui_only_task_source_from_historical_manifest_v1,
    frozen_gui_only_task_source_receipt_v1,
    load_gui_only_task_source_freeze_receipt_v1,
    parse_cohort_selection,
    select_gui_only_cohort,
    validate_written_artifact_bundle_v1,
    write_artifact_bundle,
    write_fresh_gui_only_task_source_freeze_receipt_v1,
    write_fresh_gui_only_task_source_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION,
    frozen_pilot_manifest_projection,
    parse_frozen_pilot_manifest,
    resolve_pilot_task_inputs_v1,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATHS = (
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_4/topology_comparison.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_4/cpu_topology_artifact.v1.schema.json",
    REPOSITORY_ROOT
    / "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v1.schema.json",
    REPOSITORY_ROOT
    / "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v2.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/frozen_pilot_manifest.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/frozen_pilot_manifest.v2.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/cohort_selection.v1.schema.json",
    REPOSITORY_ROOT
    / "mobileworld_audit_handoff/schemas/r2_5/executable_task_source.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/artifact_bundle.v1.schema.json",
)


def _schemas() -> tuple[dict[str, object], ...]:
    return tuple(json.loads(path.read_text(encoding="utf-8")) for path in SCHEMA_PATHS)


def _validator(schema: dict[str, object]) -> Draft202012Validator:
    schemas = _schemas()
    store = {str(item["$id"]): item for item in schemas}
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store=store),
    )


def _record(
    task_id: str,
    *,
    tags: tuple[str, ...] = (),
    apps: tuple[str, ...] = ("Settings",),
    time_dependency: RegistryTaskTimeDependencyV1 = (
        RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
    ),
) -> RegistryTaskMetadataV1:
    return RegistryTaskMetadataV1(
        task_id=task_id,
        task_tags=tags,
        app_names=apps,
        task_time_dependency=time_dependency,
        definition_source_sha256=hashlib.sha256(f"source:{task_id}".encode()).hexdigest(),
    )


def _source_and_registry(tmp_path: Path) -> tuple[Path, tuple[RegistryTaskMetadataV1, ...]]:
    task_ids = [f"GuiTask{index:02d}" for index in range(23)]
    source_rows: list[dict[str, JsonValue]] = [
        {"task_name": task_id, "trial": 1} for task_id in task_ids
    ]
    source_rows.extend(
        (
            {"task_name": "MissingTask", "trial": 1},
            {"task_name": "NeedsAskUserTask", "trial": 1},
            {"task_name": "TaggedInteractionTask", "trial": 1},
            {"task_name": "TaggedMcpTask", "trial": 1},
            {"task_name": "McpAppTask", "trial": 1},
        )
    )
    source = tmp_path / "gui-only.jsonl"
    source.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in source_rows),
        encoding="utf-8",
    )
    records = [_record(task_id) for task_id in task_ids]
    records.extend(
        (
            _record("NeedsAskUserTask"),
            _record("TaggedInteractionTask", tags=("agent-user-interaction",)),
            _record("TaggedMcpTask", tags=("agent-mcp",)),
            _record("McpAppTask", apps=("MCP-Github",)),
        )
    )
    records.sort(key=lambda item: item.task_id.encode("utf-8"))
    return source, tuple(records)


def _snapshot(tmp_path: Path, name: str, port: int) -> SnapshotDeclarationV1:
    return SnapshotDeclarationV1(
        snapshot_path=str(tmp_path / "models" / name / "snapshot"),
        snapshot_storage_root=str(tmp_path / "models" / name),
        snapshot_tree_sha256=hashlib.sha256(name.encode()).hexdigest(),
        snapshot_total_bytes=123,
        snapshot_file_count=2,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"fixture-{name}",
    )


def _inputs(tmp_path: Path) -> tuple[AuthorityArtifactInputsV1, tuple[RegistryTaskMetadataV1, ...]]:
    historical_manifest, historical_raw = _historical_gui117_manifest(tmp_path)
    source_freeze = freeze_gui_only_task_source_from_historical_manifest_v1(
        historical_manifest.resolve(),
        expected_manifest_sha256=hashlib.sha256(historical_raw).hexdigest(),
        expected_manifest_byte_count=len(historical_raw),
    )
    source = tmp_path / "gui-only-authority-source.jsonl"
    source_receipt = tmp_path / "gui-only-authority-source.freeze.json"
    write_fresh_gui_only_task_source_v1(
        source_freeze, source.resolve(), repository_root=REPOSITORY_ROOT
    )
    write_fresh_gui_only_task_source_freeze_receipt_v1(
        source_freeze, source_receipt.resolve(), repository_root=REPOSITORY_ROOT
    )
    records = tuple(
        _record(f"HistoricalGuiTask{index:03d}")
        for index in range(1, GUI_ONLY_TASK_SOURCE_TASK_COUNT + 1)
    )
    repository = REPOSITORY_ROOT
    external = tmp_path / "external"
    external.mkdir()
    qwen_fixture = tmp_path / "qwen-request.json"
    mai_fixture = tmp_path / "mai-request.json"
    qwen_fixture.write_bytes(b'{"host":"qwen"}')
    mai_fixture.write_bytes(b'{"host":"mai"}')
    topology_artifact = tmp_path / "topology-comparison.json"
    write_cpu_topology_artifact(topology_artifact)
    return (
        AuthorityArtifactInputsV1(
            source_task_jsonl=source,
            source_freeze_receipt=source_receipt,
            repository_root=repository,
            bundle_directory=external / "authority-bundle",
            runtime_output_root=external / "runtime-output",
            secret_file=external / "not-created-or-read.key",
            topology_comparison_artifact=topology_artifact,
            qwen_snapshot=_snapshot(tmp_path, "qwen", 18_081),
            mai_snapshot=_snapshot(tmp_path, "mai", 18_082),
            qwen_smoke_fixture=qwen_fixture,
            mai_smoke_fixture=mai_fixture,
            qwen_smoke_task_id="HistoricalGuiTask001",
            mai_smoke_task_id="HistoricalGuiTask002",
            source_commit="a" * 40,
            cohort_id="r25-deterministic-20",
            run_id="r24-r25-draft",
            frozen_at_utc="2026-09-03T03:00:00Z",
            authorization_id="pending-owner-authorization",
            authorized_by="owner-pending",
            issued_at_utc="2026-09-03T03:00:00Z",
            expires_at_utc="2026-09-10T03:00:00Z",
            resource_topology="SINGLE_GPU_SEQUENTIAL_SHARED",
            runtime_config_sha256=hashlib.sha256(b"runtime-config").hexdigest(),
            pricing_sha256=hashlib.sha256(b"pricing").hexdigest(),
            max_resource_cleanup_wall_time_seconds=278,
            resource_cleanup_upper_bound_sha256=hashlib.sha256(b"cleanup-upper-bound").hexdigest(),
            max_model_switch_wall_time_seconds=300,
            max_post_run_integrity_wall_time_seconds=3_600,
        ),
        records,
    )


def _historical_gui117_manifest(
    tmp_path: Path,
    task_names: tuple[str, ...] | None = None,
) -> tuple[Path, bytes]:
    if task_names is None:
        task_names = tuple(
            f"HistoricalGuiTask{index:03d}"
            for index in range(1, GUI_ONLY_TASK_SOURCE_TASK_COUNT + 1)
        )
    assert len(task_names) == GUI_ONLY_TASK_SOURCE_TASK_COUNT
    tasks: list[dict[str, JsonValue]] = []
    for index, task_name in enumerate(task_names, start=1):
        tasks.append(
            {
                "canonical_suite_index": index,
                "capture_complete": True,
                "collector_error_event_ids": [],
                "environment_evaluation": {"score": 0.0},
                "missing_artifacts": [],
                "runtime_status": "completed",
                "source_id": "historical-source",
                "source_run_id": "historical-run",
                "source_task_index": index,
                "source_task_run_id": f"historical-task-{index}",
                "task_ended_event_id": f"ended-{index}",
                "task_goal_utf8_byte_count": 1,
                "task_goal_utf8_sha256": hashlib.sha256(f"goal-{index}".encode()).hexdigest(),
                "task_name": task_name,
                "task_started_event_id": f"started-{index}",
                "task_stream": {"byte_count": 1},
                "whole_task_attempt_index": 1,
            }
        )
    task_name_index: list[dict[str, JsonValue]] = [
        {"task_index": task["canonical_suite_index"], "task_name": task["task_name"]}
        for task in tasks
    ]
    manifest: dict[str, JsonValue] = {
        "artifact_type": "derived_task_selection",
        "canonical_catalog": {
            "task_catalog_sha256": "1" * 64,
            "task_count": GUI_ONLY_TASK_SOURCE_TASK_COUNT,
            "task_name_index_sha256": hashlib.sha256(
                canonical_json_bytes(task_name_index)
            ).hexdigest(),
        },
        "counts": {
            "blob_reference_occurrences": 0,
            "selected_task_stream_byte_count": 0,
            "task_count": GUI_ONLY_TASK_SOURCE_TASK_COUNT,
            "task_count_by_source": {"historical-source": GUI_ONLY_TASK_SOURCE_TASK_COUNT},
            "unique_blob_byte_count_summed_by_source": 0,
            "unique_blob_count_summed_by_source": 0,
        },
        "dataset_id": "historical-gui117",
        "is_raw_run": False,
        "raw_schema_version": "mobileworld.audit.event/v1",
        "schema_version": "mobileworld.audit.curated-task-set/v1",
        "selection_policy": {
            "candidate_resolution": "exactly_one_eligible_stream_per_canonical_task",
            "canonical_catalog_source_id": "historical-source",
            "collector_error_event_ids_must_be_empty": True,
            "missing_artifacts_must_be_empty": True,
            "raw_events_or_blobs_copied": False,
            "source_run_global_capture_complete_required": False,
            "task_capture_complete": True,
            "task_goal_sha256_must_match_catalog": True,
            "task_outcome_score_filter": None,
            "task_runtime_status": "completed",
            "unit": "task_run",
        },
        "selection_sha256": hashlib.sha256(canonical_json_bytes(tasks)).hexdigest(),
        "source_locator": {"kind": "historical"},
        "sources": [],
        "tasks": tasks,
    }
    raw = canonical_json_bytes(manifest) + b"\n"
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "historical-gui117-manifest.json"
    path.write_bytes(raw)
    return path, raw


def test_gui117_source_freeze_binds_exact_manifest_and_declares_new_trial(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    external = tmp_path / "external"
    repository.mkdir()
    external.mkdir()
    manifest, raw = _historical_gui117_manifest(tmp_path)

    source = freeze_gui_only_task_source_from_historical_manifest_v1(
        manifest.resolve(),
        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        expected_manifest_byte_count=len(raw),
    )
    output = write_fresh_gui_only_task_source_v1(
        source,
        (external / "gui117-r25-trial-one.jsonl").resolve(),
        repository_root=repository.resolve(),
    )

    rows = [json.loads(line) for line in output.read_bytes().splitlines()]
    assert len(rows) == GUI_ONLY_TASK_SOURCE_TASK_COUNT
    assert all(set(row) == {"task_name", "trial"} and row["trial"] == 1 for row in rows)
    assert source.trial_provenance == GUI_ONLY_TASK_SOURCE_TRIAL_PROVENANCE
    assert source.historical_manifest_sha256 == hashlib.sha256(raw).hexdigest()
    assert source.task_source_sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    receipt = frozen_gui_only_task_source_receipt_v1(source)
    assert (
        receipt["source_freeze_sha256"]
        == hashlib.sha256(canonical_json_bytes(receipt["source_freeze"])).hexdigest()
    )

    with pytest.raises(R25ArtifactBuildError, match="HISTORICAL_MANIFEST_DRIFT"):
        freeze_gui_only_task_source_from_historical_manifest_v1(
            manifest.resolve(),
            expected_manifest_sha256="0" * 64,
            expected_manifest_byte_count=len(raw),
        )
    with pytest.raises(R25ArtifactBuildError, match="OUTPUT_NOT_FRESH"):
        write_fresh_gui_only_task_source_v1(
            source,
            output,
            repository_root=repository.resolve(),
        )


def test_gui117_source_freeze_rejects_approximate_manifest_shape(tmp_path: Path) -> None:
    manifest, raw = _historical_gui117_manifest(tmp_path)
    value = json.loads(raw)
    value["approximate_extra"] = True
    forged = canonical_json_bytes(value) + b"\n"
    manifest.write_bytes(forged)

    with pytest.raises(R25ArtifactBuildError, match="INVALID_JSON_OBJECT"):
        freeze_gui_only_task_source_from_historical_manifest_v1(
            manifest.resolve(),
            expected_manifest_sha256=hashlib.sha256(forged).hexdigest(),
            expected_manifest_byte_count=len(forged),
        )


def test_frozen_gui117_source_rejects_forged_rows_and_metadata(tmp_path: Path) -> None:
    manifest, raw = _historical_gui117_manifest(tmp_path)
    source = freeze_gui_only_task_source_from_historical_manifest_v1(
        manifest.resolve(),
        expected_manifest_sha256=hashlib.sha256(raw).hexdigest(),
        expected_manifest_byte_count=len(raw),
    )
    lines = source.task_source_bytes.splitlines()

    trial_two = canonical_json_bytes({"task_name": json.loads(lines[0])["task_name"], "trial": 2})
    forged_trial_bytes = b"\n".join((trial_two, *lines[1:])) + b"\n"
    with pytest.raises(R25ArtifactBuildError, match="INVALID_GUI_ONLY_TASK_SOURCE"):
        replace(
            source,
            task_source_bytes=forged_trial_bytes,
            task_source_sha256=hashlib.sha256(forged_trial_bytes).hexdigest(),
        )

    with pytest.raises(R25ArtifactBuildError, match="INVALID_GUI_ONLY_TASK_SOURCE"):
        replace(source, task_count=GUI_ONLY_TASK_SOURCE_TASK_COUNT - 1)
    with pytest.raises(R25ArtifactBuildError, match="INVALID_GUI_ONLY_TASK_SOURCE"):
        replace(source, trial=True)

    truncated_bytes = b"\n".join(lines[:-1]) + b"\n"
    with pytest.raises(R25ArtifactBuildError, match="INVALID_GUI_ONLY_TASK_SOURCE"):
        replace(
            source,
            task_source_bytes=truncated_bytes,
            task_source_sha256=hashlib.sha256(truncated_bytes).hexdigest(),
        )

    noncanonical_line = json.dumps(json.loads(lines[0]), sort_keys=True).encode("utf-8")
    assert noncanonical_line != lines[0]
    noncanonical_bytes = b"\n".join((noncanonical_line, *lines[1:])) + b"\n"
    with pytest.raises(R25ArtifactBuildError, match="INVALID_GUI_ONLY_TASK_SOURCE"):
        replace(
            source,
            task_source_bytes=noncanonical_bytes,
            task_source_sha256=hashlib.sha256(noncanonical_bytes).hexdigest(),
        )

    duplicate_bytes = b"\n".join((lines[0], lines[0], *lines[2:])) + b"\n"
    with pytest.raises(R25ArtifactBuildError, match="DUPLICATE_SOURCE_TASK"):
        replace(
            source,
            task_source_bytes=duplicate_bytes,
            task_source_sha256=hashlib.sha256(duplicate_bytes).hexdigest(),
        )


def test_selection_is_deterministic_and_excludes_non_gui_dependencies(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)

    first = select_gui_only_cohort(source, records)
    second = select_gui_only_cohort(source, tuple(reversed(records)))

    assert first == second
    assert len(first.members) == 20
    assert first.eligible_task_count == 23
    assert first.excluded_missing_registry == 1
    assert first.excluded_user_interaction == 2
    assert first.excluded_mcp == 2
    assert first.excluded_dynamic_time == 0
    assert len(first.source_task_audit) == 28
    assert tuple(record.source_row_index for record in first.source_task_audit) == tuple(
        range(1, 29)
    )
    assert sum(record.disposition.value == "ELIGIBLE" for record in first.source_task_audit) == 23
    assert len({member.task_id for member in first.members}) == 20
    assert all(1 <= member.reset_seed <= 2_147_483_647 for member in first.members)
    assert all(member.task_id.startswith("GuiTask") for member in first.members)


def test_selection_excludes_dynamic_or_unknown_wall_clock_tasks(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    dynamic = replace(
        records[0],
        task_time_dependency=(RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK),
    )
    audited = (dynamic, *records[1:])

    selection = select_gui_only_cohort(source, audited)

    assert selection.excluded_dynamic_time == 1
    assert selection.eligible_task_count == 22
    assert dynamic.task_id not in {member.task_id for member in selection.members}


def test_current_registry_time_audit_is_conservative_and_source_bound() -> None:
    records = {record.task_id: record for record in current_registry_metadata()}

    assert records["MattermostTechnicalDebtTriageTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
    )
    assert records["CheckGithubInfoTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
    )
    assert records["TakeSelfieTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
    )
    assert records["ScheduleLunchViaSmsTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
    )
    assert all(len(record.definition_source_sha256) == 64 for record in records.values())


def test_task_definition_snapshot_rejects_leaf_and_ancestor_aliases(tmp_path: Path) -> None:
    leaf_root = tmp_path / "leaf-root"
    leaf_root.mkdir()
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (leaf_root / "task.py").symlink_to(target)
    with pytest.raises(R25ArtifactBuildError, match="TASK_DEFINITION_ALIAS_FORBIDDEN"):
        artifact_builder_module._snapshot_task_definition_tree_v1(leaf_root.resolve())

    actual_root = tmp_path / "actual-root"
    actual_root.mkdir()
    (actual_root / "task.py").write_text("value = 1\n", encoding="utf-8")
    alias_root = tmp_path / "alias-root"
    alias_root.symlink_to(actual_root, target_is_directory=True)
    with pytest.raises(R25ArtifactBuildError, match="INVALID_PATH"):
        artifact_builder_module._snapshot_task_definition_tree_v1(alias_root.absolute())


def test_task_definition_snapshot_rejects_hardlink_and_import_window_swap(
    tmp_path: Path,
) -> None:
    hardlink_root = tmp_path / "hardlink-root"
    hardlink_root.mkdir()
    first = hardlink_root / "first.py"
    first.write_text("value = 1\n", encoding="utf-8")
    (hardlink_root / "second.py").hardlink_to(first)
    with pytest.raises(R25ArtifactBuildError, match="TASK_DEFINITION_ALIAS_FORBIDDEN"):
        artifact_builder_module._snapshot_task_definition_tree_v1(hardlink_root.resolve())

    swap_root = tmp_path / "swap-root"
    swap_root.mkdir()
    source = swap_root / "task.py"
    source.write_text("value = 'trusted'\n", encoding="utf-8")
    held = artifact_builder_module._snapshot_task_definition_tree_v1(swap_root.resolve())
    try:
        original = swap_root / "task.original.py"
        source.rename(original)
        source.write_text("value = 'replacement'\n", encoding="utf-8")
        with pytest.raises(R25ArtifactBuildError, match="TASK_DEFINITION_TREE_DRIFT"):
            held.revalidate()
    finally:
        held.close()


def test_task_definition_snapshot_bytes_must_equal_head_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = {"task.py": b"value = 'worktree'\n"}
    expected_raw = b"value = 'head'\n"
    expected_preimage = b"blob " + str(len(expected_raw)).encode("ascii") + b"\0" + expected_raw
    monkeypatch.setattr(
        artifact_builder_module,
        "_git_head_task_definition_blobs_v1",
        lambda repository_root, definitions_root: {
            "task.py": hashlib.sha1(expected_preimage, usedforsecurity=False).hexdigest()
        },
    )
    with pytest.raises(R25ArtifactBuildError, match="TASK_DEFINITION_GIT_MISMATCH"):
        artifact_builder_module._validate_task_definition_sources_against_git_v1(
            sources,
            repository_root=tmp_path,
            definitions_root=tmp_path,
        )


def test_bundle_has_executable_inline_source_and_80_matched_cells(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)

    bundle = build_authority_artifact_bundle(inputs, records)

    assert bundle.source_freeze.task_count == GUI_ONLY_TASK_SOURCE_TASK_COUNT
    assert bundle.source_freeze.trial == 1

    assert bundle.task_source["schema_version"] == EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION
    raw_tasks = bundle.task_source["tasks"]
    assert isinstance(raw_tasks, list)
    assert len(raw_tasks) == 20
    assert len(bundle.pilot_manifest.cells) == 80
    for item in raw_tasks:
        assert isinstance(item, dict)
        source = item["parameter_source"]
        assert isinstance(source, dict)
        assert source["kind"] == "INLINE_CANONICAL_JSON"
        payload = source["payload"]
        assert isinstance(payload, dict)
        assert source["sha256"] == hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    authority = bundle.authority_manifest
    assert authority.authorization.status is RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED
    assert tuple(stage.role for stage in authority.openai_stages) == (
        OpenAIRoleV1.RUBRIC,
        OpenAIRoleV1.HISTORY_POLICY,
    )
    assert tuple(case.mode for plan in authority.smoke_plans for case in plan.cases) == (
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
    )
    assert parse_authority_manifest(authority_manifest_projection(authority)) == authority
    assert (
        authority.topology_comparison_artifact_sha256
        == bundle.pilot_manifest.topology_comparison_artifact_sha256
    )
    assert bundle.pilot_manifest.dynamic_wall_clock_tasks_excluded is True
    assert not inputs.secret_file.exists()


def test_50_step_authority_uses_extended_history_policy_output_bound(tmp_path: Path) -> None:
    default_inputs, default_records = _inputs(tmp_path / "default")
    default_bundle = build_authority_artifact_bundle(default_inputs, default_records)
    default_history_stage = default_bundle.authority_manifest.openai_stages[1]
    assert default_history_stage.role is OpenAIRoleV1.HISTORY_POLICY
    assert default_history_stage.max_output_tokens == 4096

    long_inputs, long_records = _inputs(tmp_path / "long")
    long_bundle = build_authority_artifact_bundle(
        replace(long_inputs, max_steps_per_cell=50), long_records
    )
    long_history_stage = long_bundle.authority_manifest.openai_stages[1]
    assert long_history_stage.role is OpenAIRoleV1.HISTORY_POLICY
    assert long_history_stage.max_output_tokens == 8192
    assert (
        parse_authority_manifest(authority_manifest_projection(long_bundle.authority_manifest))
        == long_bundle.authority_manifest
    )


def test_authority_build_requires_reopenable_historical_source_freeze(
    tmp_path: Path,
) -> None:
    inputs, records = _inputs(tmp_path)
    with pytest.raises(R25ArtifactBuildError, match="SOURCE_FREEZE_RECEIPT_REQUIRED"):
        build_authority_artifact_bundle(replace(inputs, source_freeze_receipt=None), records)

    assert inputs.source_freeze_receipt is not None
    source = load_gui_only_task_source_freeze_receipt_v1(
        inputs.source_freeze_receipt,
        task_source_path=inputs.source_task_jsonl,
    )
    lines = source.task_source_bytes.splitlines()
    forged_first = canonical_json_bytes({"task_name": "ForgedGuiTask", "trial": 1})
    forged_bytes = b"\n".join((forged_first, *lines[1:])) + b"\n"
    forged = replace(
        source,
        task_source_bytes=forged_bytes,
        task_source_sha256=hashlib.sha256(forged_bytes).hexdigest(),
    )
    forged_source = tmp_path / "forged-gui117.jsonl"
    forged_receipt = tmp_path / "forged-gui117.freeze.json"
    write_fresh_gui_only_task_source_v1(
        forged, forged_source.resolve(), repository_root=REPOSITORY_ROOT
    )
    write_fresh_gui_only_task_source_freeze_receipt_v1(
        forged, forged_receipt.resolve(), repository_root=REPOSITORY_ROOT
    )
    with pytest.raises(R25ArtifactBuildError, match="SOURCE_FREEZE_PROVENANCE_MISMATCH"):
        build_authority_artifact_bundle(
            replace(
                inputs,
                source_task_jsonl=forged_source,
                source_freeze_receipt=forged_receipt,
            ),
            records,
        )


def test_persisted_authority_artifacts_match_schemas_and_module_round_trips(
    tmp_path: Path,
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    schemas = _schemas()
    by_id = {str(schema["$id"]): schema for schema in schemas}
    for schema in schemas:
        Draft202012Validator.check_schema(schema)

    frozen = frozen_pilot_manifest_projection(bundle.pilot_manifest)
    authority = authority_manifest_projection(bundle.authority_manifest)
    executable = bundle.task_source
    selection = cohort_selection_projection(bundle.selection)
    bundle_output = artifact_bundle_output(bundle)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/frozen_pilot_manifest.v2.schema.json"]
    ).validate(frozen)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_4/run_authority_manifest.v2.schema.json"]
    ).validate(authority)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/cohort_selection.v1.schema.json"]
    ).validate(selection)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/executable_task_source.v1.schema.json"]
    ).validate(executable)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/artifact_bundle.v1.schema.json"]
    ).validate(bundle_output)

    assert parse_frozen_pilot_manifest(json.loads(canonical_json_bytes(frozen))) == (
        bundle.pilot_manifest
    )
    assert parse_authority_manifest(json.loads(canonical_json_bytes(authority))) == (
        bundle.authority_manifest
    )
    assert parse_cohort_selection(json.loads(canonical_json_bytes(selection))) == bundle.selection
    assert cohort_selection_sha256(bundle.selection) == (
        bundle.pilot_manifest.cohort_selection_artifact_sha256
    )
    projected_bundle = artifact_bundle_projection(bundle)
    assert (
        bundle_output["artifact_bundle_sha256"]
        == hashlib.sha256(canonical_json_bytes(projected_bundle)).hexdigest()
    )

    tampered = json.loads(canonical_json_bytes(bundle_output))
    tampered["artifact_bundle"]["cohort_selection"]["source_task_audit"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        _validator(
            by_id["https://agentsentinel.local/schemas/r2_5/artifact_bundle.v1.schema.json"]
        ).validate(tampered)


def test_written_source_resolves_to_exact_task_reset_inputs(tmp_path: Path) -> None:
    inputs, _ = _inputs(tmp_path)
    records = current_registry_metadata()
    source_records = records[:GUI_ONLY_TASK_SOURCE_TASK_COUNT]
    historical_manifest, historical_raw = _historical_gui117_manifest(
        tmp_path / "current-source-history",
        tuple(record.task_id for record in source_records),
    )
    source_freeze = freeze_gui_only_task_source_from_historical_manifest_v1(
        historical_manifest.resolve(),
        expected_manifest_sha256=hashlib.sha256(historical_raw).hexdigest(),
        expected_manifest_byte_count=len(historical_raw),
    )
    source = tmp_path / "current-registry-tasks.jsonl"
    source_receipt = tmp_path / "current-registry-tasks.freeze.json"
    write_fresh_gui_only_task_source_v1(
        source_freeze, source.resolve(), repository_root=REPOSITORY_ROOT
    )
    write_fresh_gui_only_task_source_freeze_receipt_v1(
        source_freeze, source_receipt.resolve(), repository_root=REPOSITORY_ROOT
    )
    selected = select_gui_only_cohort(source, records)
    inputs = replace(
        inputs,
        source_task_jsonl=source,
        source_freeze_receipt=source_receipt,
        qwen_smoke_task_id=selected.members[0].task_id,
        mai_smoke_task_id=selected.members[1].task_id,
    )
    bundle = build_authority_artifact_bundle(inputs, records)

    written = write_artifact_bundle(
        bundle,
        repository_root=inputs.repository_root,
        allow_unverified_cpu_fixture=True,
    )
    validation = validate_written_artifact_bundle_v1(
        inputs.bundle_directory,
        repository_root=inputs.repository_root,
        allow_unverified_cpu_fixture=True,
    )

    assert {path.name for path in written} >= {
        PILOT_TASK_SOURCE_FILENAME,
        RUN_AUTHORITY_MANIFEST_FILENAME,
        ARTIFACT_BUNDLE_FILENAME,
        TOPOLOGY_COMPARISON_FILENAME,
        COHORT_SELECTION_FILENAME,
        GUI_ONLY_TASK_SOURCE_FILENAME,
    }
    resolved = resolve_pilot_task_inputs_v1(
        bundle.pilot_manifest,
        authorized_input_root=inputs.bundle_directory,
        repository_root=inputs.repository_root,
    )
    assert len(resolved.tasks) == 20
    assert tuple(task.task_id for task in resolved.tasks) == tuple(
        member.task_id for member in bundle.selection.members
    )
    assert all(task.trial == 1 for task in resolved.tasks)
    assert validation.artifact_count == 7
    assert validation.source_task_count == GUI_ONLY_TASK_SOURCE_TASK_COUNT
    assert validation.cohort_size == 20
    assert inputs.cohort_size == 20
    assert (
        canonical_json_bytes(artifact_bundle_output(bundle))
        == (inputs.bundle_directory / ARTIFACT_BUNDLE_FILENAME).read_bytes()
    )
    topology_bytes = (inputs.bundle_directory / TOPOLOGY_COMPARISON_FILENAME).read_bytes()
    assert hashlib.sha256(topology_bytes).hexdigest() == (
        bundle.pilot_manifest.topology_comparison_artifact_sha256
    )
    selection_bytes = (inputs.bundle_directory / COHORT_SELECTION_FILENAME).read_bytes()
    assert hashlib.sha256(selection_bytes).hexdigest() == (
        bundle.pilot_manifest.cohort_selection_artifact_sha256
    )
    assert (inputs.bundle_directory / GUI_ONLY_TASK_SOURCE_FILENAME).read_bytes() == (
        source.read_bytes()
    )


def test_owner_promotion_changes_only_status_and_writes_fresh_external_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )
    draft_path = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    draft = load_canonical_draft_authority_v1(
        draft_path,
        repository_root=inputs.repository_root,
    )
    draft_sha256 = authority_manifest_sha256(draft)
    promoted = promote_draft_authority_v1(
        draft,
        confirmed_draft_sha256=draft_sha256,
    )

    before = json.loads(canonical_json_bytes(authority_manifest_projection(draft)))
    after = json.loads(canonical_json_bytes(authority_manifest_projection(promoted)))
    assert before["authorization"]["status"] == "DRAFT_NOT_AUTHORIZED"
    before["authorization"]["status"] = "OWNER_AUTHORIZED"
    assert after == before
    assert promoted.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED

    output = tmp_path / "owner-authorized-manifest.json"
    digest = write_fresh_owner_authority_v1(
        promoted,
        output,
        repository_root=inputs.repository_root,
    )
    assert digest == authority_manifest_sha256(promoted)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() == canonical_json_bytes(authority_manifest_projection(promoted))
    with pytest.raises(AuthorityPromotionError, match="OUTPUT_NOT_FRESH"):
        write_fresh_owner_authority_v1(
            promoted,
            output,
            repository_root=inputs.repository_root,
        )
    with pytest.raises(AuthorityPromotionError, match="DRAFT_CONFIRMATION_MISMATCH"):
        promote_draft_authority_v1(draft, confirmed_draft_sha256="0" * 64)


def _load_promotion_cli() -> ModuleType:
    script = REPOSITORY_ROOT / "MobileWorld/scripts/promote_r2_4_r2_5_authority.py"
    spec = importlib.util.spec_from_file_location("r24_r25_authority_promotion_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owner_promotion_cli_requires_explicit_assertion_and_exact_draft_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )
    draft = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    output = tmp_path / "promoted.json"
    cli = _load_promotion_cli()
    base = [
        "--draft-manifest",
        str(draft),
        "--confirm-draft-sha256",
        authority_manifest_sha256(bundle.authority_manifest),
        "--output",
        str(output),
        "--repository-root",
        str(inputs.repository_root),
    ]

    assert cli.main(base) == 2
    capsys.readouterr()
    assert not output.exists()
    wrong_hash = list(base)
    wrong_hash[wrong_hash.index("--confirm-draft-sha256") + 1] = "0" * 64
    assert cli.main([*wrong_hash, "--owner-approved"]) == 2
    capsys.readouterr()
    assert not output.exists()
    assert cli.main([*base, "--owner-approved"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["projection_change"] == "AUTHORIZATION_STATUS_ONLY"
    assert emitted["draft_manifest_sha256"] == authority_manifest_sha256(bundle.authority_manifest)
    assert emitted["authorized_manifest_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    parsed = parse_authority_manifest(json.loads(output.read_bytes()))
    assert parsed.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED


def test_owner_promotion_rejects_noncanonical_or_already_promoted_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )
    draft_path = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    draft = load_canonical_draft_authority_v1(
        draft_path,
        repository_root=inputs.repository_root,
    )
    promoted = promote_draft_authority_v1(
        draft,
        confirmed_draft_sha256=authority_manifest_sha256(draft),
    )
    with pytest.raises(AuthorityPromotionError, match="DRAFT_REQUIRED"):
        promote_draft_authority_v1(
            promoted,
            confirmed_draft_sha256=authority_manifest_sha256(promoted),
        )

    symlink = tmp_path / "draft-link.json"
    symlink.symlink_to(draft_path)
    with pytest.raises(AuthorityPromotionError, match="INVALID_DRAFT_FILE"):
        load_canonical_draft_authority_v1(
            symlink,
            repository_root=inputs.repository_root,
        )

    draft_path.write_text(
        json.dumps(authority_manifest_projection(draft), indent=2),
        encoding="utf-8",
    )
    draft_path.chmod(0o600)
    with pytest.raises(AuthorityPromotionError, match="NONCANONICAL_DRAFT"):
        load_canonical_draft_authority_v1(
            draft_path,
            repository_root=inputs.repository_root,
        )


def test_selection_parser_rejects_forged_static_member_derivations(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    selection = select_gui_only_cohort(source, records)
    projection = cohort_selection_projection(selection)

    forged_seed = json.loads(canonical_json_bytes(projection))
    forged_seed["members"][0]["reset_seed"] += 1
    with pytest.raises(R25ArtifactBuildError, match="INVALID_SELECTION"):
        parse_cohort_selection(forged_seed)

    forged_static = json.loads(canonical_json_bytes(projection))
    excluded = next(
        record
        for record in forged_static["source_task_audit"]
        if record["disposition"] != "ELIGIBLE"
    )
    excluded["disposition"] = "ELIGIBLE"
    excluded["selection_sha256"] = "0" * 64
    with pytest.raises(R25ArtifactBuildError):
        parse_cohort_selection(forged_static)


def test_builder_rejects_noncanonical_or_forged_topology_preimage(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)
    parsed = json.loads(inputs.topology_comparison_artifact.read_bytes())
    inputs.topology_comparison_artifact.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    with pytest.raises(R25ArtifactBuildError, match="NONCANONICAL_TOPOLOGY_COMPARISON"):
        build_authority_artifact_bundle(inputs, records)

    parsed["comparison"]["total_call_count"] += 1
    inputs.topology_comparison_artifact.write_bytes(canonical_json_bytes(parsed))
    with pytest.raises(R25ArtifactBuildError, match="INVALID_TOPOLOGY_COMPARISON"):
        build_authority_artifact_bundle(inputs, records)


def test_writer_rejects_existing_or_repo_internal_directory(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    with pytest.raises(R25ArtifactBuildError, match="SOURCE_STATE_CONFIRMATION_REQUIRED"):
        write_artifact_bundle(bundle, repository_root=inputs.repository_root)
    inputs.bundle_directory.mkdir()

    with pytest.raises(R25ArtifactBuildError, match="OUTPUT_DIRECTORY_NOT_FRESH"):
        write_artifact_bundle(
            bundle,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )

    internal_inputs = replace(
        inputs,
        bundle_directory=inputs.repository_root / "authority-bundle",
    )
    with pytest.raises(R25ArtifactBuildError, match="REPOSITORY_PATH_FORBIDDEN"):
        build_authority_artifact_bundle(internal_inputs, records)


def test_writer_retains_complete_publication_when_readback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)

    def reject_readback(*_args: object, **_kwargs: object) -> object:
        raise R25ArtifactBuildError("ARTIFACT_READBACK_FAILED", "injected readback failure")

    monkeypatch.setattr(
        artifact_builder_module,
        "validate_written_artifact_bundle_v1",
        reject_readback,
    )
    with pytest.raises(
        R25ArtifactBuildError,
        match="(?:ARTIFACT_READBACK_FAILED|ARTIFACT_BINDING_MISMATCH)",
    ):
        write_artifact_bundle(
            bundle,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )

    assert inputs.bundle_directory.is_dir()
    assert {path.name for path in inputs.bundle_directory.iterdir()} == {
        GUI_ONLY_TASK_SOURCE_FILENAME,
        COHORT_SELECTION_FILENAME,
        PILOT_TASK_SOURCE_FILENAME,
        FROZEN_PILOT_MANIFEST_FILENAME,
        RUN_AUTHORITY_MANIFEST_FILENAME,
        ARTIFACT_BUNDLE_FILENAME,
        TOPOLOGY_COMPARISON_FILENAME,
    }


def test_readback_rejects_extra_file_and_tampered_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )

    wrong_repository = tmp_path / "wrong-repository"
    wrong_repository.mkdir()
    with pytest.raises(R25ArtifactBuildError, match="(?:UNREADABLE_FILE|INVALID_PATH)"):
        validate_written_artifact_bundle_v1(
            inputs.bundle_directory,
            repository_root=wrong_repository.resolve(),
            allow_unverified_cpu_fixture=True,
        )

    extra = inputs.bundle_directory / "unexpected"
    extra.write_bytes(b"unexpected")
    extra.chmod(0o600)
    with pytest.raises(
        R25ArtifactBuildError,
        match="(?:ARTIFACT_READBACK_FAILED|ARTIFACT_BINDING_MISMATCH)",
    ):
        validate_written_artifact_bundle_v1(
            inputs.bundle_directory,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )
    extra.unlink()

    bundle_path = inputs.bundle_directory / ARTIFACT_BUNDLE_FILENAME
    value = json.loads(bundle_path.read_bytes())
    value["artifact_bundle_sha256"] = "0" * 64
    bundle_path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(R25ArtifactBuildError, match="ARTIFACT_BUNDLE_MISMATCH"):
        validate_written_artifact_bundle_v1(
            inputs.bundle_directory,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )


def test_writer_uses_held_directory_fds_not_path_reopen_for_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    fsync_calls: list[Path] = []
    real_fsync_directory = artifact_builder_module._fsync_directory

    def recording_fsync(path: Path) -> None:
        fsync_calls.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(artifact_builder_module, "_fsync_directory", recording_fsync)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )

    assert fsync_calls == []


def test_owner_only_readback_rejects_symlink_hardlink_and_open_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = tmp_path / "original"
    original.write_bytes(b"original")
    original.chmod(0o600)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(original)
    with pytest.raises(R25ArtifactBuildError, match="ARTIFACT_READBACK_FAILED"):
        artifact_builder_module._read_owner_only_artifact(symlink, name="symlink")

    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(original)
    with pytest.raises(R25ArtifactBuildError, match="ARTIFACT_READBACK_FAILED"):
        artifact_builder_module._read_owner_only_artifact(original, name="hardlink source")
    hardlink.unlink()

    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"replacement")
    replacement.chmod(0o600)
    real_open = artifact_builder_module.os.open
    swapped = False

    def swapping_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == original.name and dir_fd is not None and not swapped:
            swapped = True
            artifact_builder_module.os.replace(replacement, original)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifact_builder_module.os, "open", swapping_open)
    with pytest.raises(R25ArtifactBuildError, match="changed while being opened"):
        artifact_builder_module._read_owner_only_artifact(original, name="raced artifact")


def test_component_bound_source_read_rejects_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "bound-parent"
    parent.mkdir()
    source = parent / "source.json"
    source.write_bytes(b"bound-source")
    displaced = tmp_path / "displaced-parent"
    real_read = artifact_builder_module.os.read
    swapped = False

    def swapping_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        value = real_read(descriptor, size)
        if not swapped:
            swapped = True
            parent.rename(displaced)
            parent.mkdir()
        return value

    monkeypatch.setattr(artifact_builder_module.os, "read", swapping_read)
    with pytest.raises(R25ArtifactBuildError, match="PATH_IDENTITY_DRIFT"):
        artifact_builder_module._read_regular_file(
            source.resolve(), maximum=1024, name="parent-swap source"
        )


def test_bundle_writer_rejects_parent_swap_and_preserves_published_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    external = inputs.bundle_directory.parent
    displaced = tmp_path / "displaced-external"
    real_write = artifact_builder_module.os.write
    swapped = False

    def swapping_write(descriptor: int, value: object) -> int:
        nonlocal swapped
        count = real_write(descriptor, value)
        if not swapped:
            swapped = True
            external.rename(displaced)
            external.mkdir(mode=0o700)
        return count

    monkeypatch.setattr(artifact_builder_module.os, "write", swapping_write)
    with pytest.raises(R25ArtifactBuildError, match="PATH_IDENTITY_DRIFT"):
        write_artifact_bundle(
            bundle,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )
    assert (displaced / inputs.bundle_directory.name).is_dir()


def test_writer_rejects_valid_different_bundle_substitution_before_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    replacement_inputs = replace(
        inputs,
        bundle_directory=inputs.bundle_directory.parent / "replacement-bundle",
        runtime_output_root=inputs.bundle_directory.parent / "replacement-runtime",
        run_id="r24-r25-valid-different",
    )
    replacement_bundle = build_authority_artifact_bundle(replacement_inputs, records)
    write_artifact_bundle(
        replacement_bundle,
        repository_root=inputs.repository_root,
        allow_unverified_cpu_fixture=True,
    )
    original_bundle = build_authority_artifact_bundle(inputs, records)
    real_validate = artifact_builder_module.validate_written_artifact_bundle_v1
    displaced = inputs.bundle_directory.parent / "original-displaced"
    swapped = False

    def substitute_before_readback(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        if not swapped:
            swapped = True
            inputs.bundle_directory.rename(displaced)
            replacement_inputs.bundle_directory.rename(inputs.bundle_directory)
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(
        artifact_builder_module,
        "validate_written_artifact_bundle_v1",
        substitute_before_readback,
    )
    with pytest.raises(R25ArtifactBuildError, match="PATH_IDENTITY_DRIFT"):
        write_artifact_bundle(
            original_bundle,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )
    assert displaced.is_dir()


def test_readback_rejects_current_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, records = _inputs(tmp_path)
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: records)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(
        bundle, repository_root=inputs.repository_root, allow_unverified_cpu_fixture=True
    )

    drifted = (
        replace(records[0], definition_source_sha256="f" * 64),
        *records[1:],
    )
    monkeypatch.setattr(artifact_builder_module, "current_registry_metadata", lambda: drifted)
    with pytest.raises(R25ArtifactBuildError, match="ARTIFACT_BINDING_MISMATCH"):
        validate_written_artifact_bundle_v1(
            inputs.bundle_directory,
            repository_root=inputs.repository_root,
            allow_unverified_cpu_fixture=True,
        )


def test_malformed_or_duplicate_source_rows_fail_closed(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    source.write_text('{"task_name":"GuiTask00","task_name":"GuiTask01","trial":1}\n')
    with pytest.raises(R25ArtifactBuildError, match="DUPLICATE_JSON_KEY"):
        select_gui_only_cohort(source, records)

    source.write_text(
        '{"task_name":"GuiTask00","trial":1}\n{"task_name":"GuiTask00","trial":2}\n',
        encoding="utf-8",
    )
    with pytest.raises(R25ArtifactBuildError, match="DUPLICATE_SOURCE_TASK"):
        select_gui_only_cohort(source, records)


def test_selection_algorithm_identifier_is_frozen() -> None:
    assert COHORT_SELECTION_ALGORITHM == "SHA256_R25_PILOT_V1"
