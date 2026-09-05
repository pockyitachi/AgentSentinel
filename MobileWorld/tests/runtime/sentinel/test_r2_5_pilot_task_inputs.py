from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from _r2_4_topology_fixture import write_cpu_topology_artifact

import mobile_world.runtime.sentinel.r2_5.artifact_builder as artifact_builder
from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    cohort_selection_projection,
    current_registry_metadata,
    select_gui_only_cohort,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    ExternalPilotTaskParametersV1,
    FrozenPilotManifestV1,
    InlinePilotTaskParametersV1,
    MobileWorldTaskParametersV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskParameterSourceKindV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
    R25PilotContractError,
    ResolvedPilotTaskInputsV1,
    executable_pilot_task_source_projection,
    pilot_task_source_projection,
    resolve_pilot_task_inputs_v1,
    resolved_pilot_task_inputs_projection,
    resolved_pilot_task_inputs_sha256,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest(
    tmp_path: Path,
) -> tuple[FrozenPilotManifestV1, Path, Path, dict[str, JsonValue]]:
    input_root = tmp_path / "authorized-inputs"
    blob_root = input_root / "task-parameter-blobs"
    blob_root.mkdir(parents=True)
    registry = current_registry_metadata()
    cohort_source_path = input_root / "gui-only-task-source.jsonl"
    cohort_source_path.write_text(
        "".join(
            json.dumps({"task_name": record.task_id, "trial": 1}, sort_keys=True) + "\n"
            for record in registry
        ),
        encoding="utf-8",
    )
    selection = select_gui_only_cohort(cohort_source_path, registry)
    tasks: list[PilotTaskV1] = []
    bindings: list[InlinePilotTaskParametersV1 | ExternalPilotTaskParametersV1] = []
    external_blob = blob_root / "task-00.json"

    for index, member in enumerate(selection.members):
        task_id = member.task_id
        parameters = MobileWorldTaskParametersV1(task_name=task_id, trial=member.trial)
        parameter_raw = canonical_json_bytes(
            {"task_name": parameters.task_name, "trial": parameters.trial}
        )
        parameter_sha256 = _sha(parameter_raw)
        tasks.append(
            PilotTaskV1(
                task_id=task_id,
                task_parameters_sha256=parameter_sha256,
                reset_seed=member.reset_seed,
            )
        )
        if index == 0:
            external_blob.write_bytes(parameter_raw)
            bindings.append(
                ExternalPilotTaskParametersV1(
                    task_id=task_id,
                    path=str(external_blob),
                    sha256=parameter_sha256,
                    byte_count=len(parameter_raw),
                )
            )
        else:
            bindings.append(InlinePilotTaskParametersV1(task_id=task_id, parameters=parameters))

    source = executable_pilot_task_source_projection(
        "r25-executable-fixture", tuple(tasks), tuple(bindings)
    )
    source_raw = canonical_json_bytes(source)
    source_path = input_root / "pilot-task-source.json"
    source_path.write_bytes(source_raw)
    topology_path = input_root / "topology.json"
    topology_sha256, topology_byte_count = write_cpu_topology_artifact(topology_path)
    selection_path = input_root / "cohort-selection.json"
    selection_raw = canonical_json_bytes(cohort_selection_projection(selection))
    selection_path.write_bytes(selection_raw)
    selection_sha256 = _sha(selection_raw)
    manifest = FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="r25-executable-fixture",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path=str(source_path),
        task_manifest_sha256=_sha(source_raw),
        task_manifest_byte_count=len(source_raw),
        topology_comparison_artifact_path=str(topology_path),
        topology_comparison_artifact_sha256=topology_sha256,
        topology_comparison_artifact_byte_count=topology_byte_count,
        cohort_selection_artifact_path=str(selection_path),
        cohort_selection_artifact_sha256=selection_sha256,
        cohort_selection_artifact_byte_count=len(selection_raw),
        cohort_selection_sha256=selection_sha256,
        task_time_authority=PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY,
        dynamic_wall_clock_tasks_excluded=True,
        tasks=tuple(tasks),
        hosts=(PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI),
        arms=(PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL),
        topology=PilotTopologyV1.ISOLATED_HISTORY_FREE,
        seed_policy=PilotSeedPolicyV1.FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS,
        baseline_mode="OFF",
        joint_mode="ACTIVE",
        environment_reset_between_cells=True,
        matched_task_ids=True,
        matched_task_parameters=True,
        official_success_metric_required=True,
        max_steps_per_cell=3,
        per_cell_timeout_seconds=60,
        max_total_wall_time_seconds=10_000,
        max_total_actor_calls=160,
        max_total_openai_calls=80,
        max_total_cost_usd_micros=1_000_000,
    )
    return manifest, input_root, external_blob, source


def _bind_source_bytes(
    manifest: FrozenPilotManifestV1, path: Path, raw: bytes
) -> FrozenPilotManifestV1:
    path.write_bytes(raw)
    return replace(
        manifest,
        task_manifest_path=str(path),
        task_manifest_sha256=_sha(raw),
        task_manifest_byte_count=len(raw),
    )


def _resolve(manifest: FrozenPilotManifestV1, input_root: Path) -> ResolvedPilotTaskInputsV1:
    repository_root = input_root.parent / "repository"
    repository_root.mkdir(exist_ok=True)
    return resolve_pilot_task_inputs_v1(
        manifest,
        authorized_input_root=input_root,
        repository_root=repository_root,
    )


def test_resolver_rebuilds_exact_reset_and_task_init_inputs(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)

    resolved = _resolve(manifest, input_root)

    assert len(resolved.tasks) == 20
    first, second = resolved.tasks[:2]
    assert (
        first.parameter_source_kind is PilotTaskParameterSourceKindV1.EXTERNAL_CANONICAL_JSON_BLOB
    )
    assert second.parameter_source_kind is PilotTaskParameterSourceKindV1.INLINE_CANONICAL_JSON
    first_frozen = manifest.tasks[0]
    assert first.environment_reset_input == {
        "reset_seed": first_frozen.reset_seed,
        "seed_policy": "FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS",
        "task_time_authority": "STATIC_WALL_CLOCK_INDEPENDENT_ONLY",
        "cohort_selection_sha256": manifest.cohort_selection_sha256,
        "task_id": first_frozen.task_id,
    }
    assert first.task_initialization_input == {"task_name": first_frozen.task_id, "trial": 1}
    assert resolved_pilot_task_inputs_sha256(resolved) == _sha(
        canonical_json_bytes(resolved_pilot_task_inputs_projection(resolved))
    )


def test_hash_only_v1_source_is_never_executable(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    legacy = pilot_task_source_projection(manifest.cohort_id, manifest.tasks)
    legacy_path = input_root / "legacy.json"
    manifest = _bind_source_bytes(manifest, legacy_path, canonical_json_bytes(legacy))

    with pytest.raises(R25PilotContractError, match="UNEXECUTABLE_TASK_SOURCE"):
        _resolve(manifest, input_root)


def test_topology_artifact_preimage_is_recomputed_not_just_hash_matched(
    tmp_path: Path,
) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    topology_path = Path(manifest.topology_comparison_artifact_path)
    value = json.loads(topology_path.read_bytes())
    value["joint_component_census"]["comparison_provider_dispatches"] += 1
    raw = canonical_json_bytes(value)
    topology_path.write_bytes(raw)
    rebound = replace(
        manifest,
        topology_comparison_artifact_sha256=_sha(raw),
        topology_comparison_artifact_byte_count=len(raw),
    )

    with pytest.raises(R25PilotContractError, match="INVALID_TOPOLOGY_COMPARISON"):
        _resolve(rebound, input_root)


def test_cohort_artifact_is_recomputed_against_current_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    registry = list(current_registry_metadata())
    registry[0] = replace(registry[0], definition_source_sha256="0" * 64)
    monkeypatch.setattr(artifact_builder, "current_registry_metadata", lambda: tuple(registry))

    with pytest.raises(R25PilotContractError, match="COHORT_SELECTION_BINDING_MISMATCH"):
        _resolve(manifest, input_root)


def test_cohort_artifact_preimage_is_not_replaceable_by_a_rebound_hash(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    path = Path(manifest.cohort_selection_artifact_path)
    value = json.loads(path.read_bytes())
    value["members"][0]["reset_seed"] += 1
    raw = canonical_json_bytes(value)
    path.write_bytes(raw)
    rebound = replace(
        manifest,
        cohort_selection_artifact_sha256=_sha(raw),
        cohort_selection_artifact_byte_count=len(raw),
        cohort_selection_sha256=_sha(raw),
    )

    with pytest.raises(R25PilotContractError, match="COHORT_SELECTION_RECOMPUTE_FAILED"):
        _resolve(rebound, input_root)


def test_dynamic_task_time_authority_cannot_enter_frozen_pilot(tmp_path: Path) -> None:
    manifest, _, _, _ = _manifest(tmp_path)
    with pytest.raises(R25PilotContractError, match="DYNAMIC_TASK_TIME_FORBIDDEN"):
        replace(manifest, task_time_authority="DYNAMIC_WALL_CLOCK")  # type: ignore[arg-type]


def test_source_must_be_canonical_even_when_declared_hash_matches(tmp_path: Path) -> None:
    manifest, input_root, _, source = _manifest(tmp_path)
    noncanonical = json.dumps(source, ensure_ascii=False, indent=2).encode()
    manifest = _bind_source_bytes(manifest, Path(manifest.task_manifest_path), noncanonical)

    with pytest.raises(R25PilotContractError, match="NONCANONICAL_JSON"):
        _resolve(manifest, input_root)


def test_parameter_blob_content_drift_fails_closed(tmp_path: Path) -> None:
    manifest, input_root, external_blob, _ = _manifest(tmp_path)
    raw = external_blob.read_bytes()
    external_blob.write_bytes(raw[:-1] + (b"}" if raw[-1:] != b"}" else b"]"))

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_DRIFT"):
        _resolve(manifest, input_root)


def test_missing_parameter_blob_fails_closed(tmp_path: Path) -> None:
    manifest, input_root, external_blob, _ = _manifest(tmp_path)
    external_blob.unlink()

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_MISSING"):
        _resolve(manifest, input_root)


def test_parameter_blob_symlink_fails_closed(tmp_path: Path) -> None:
    manifest, input_root, external_blob, _ = _manifest(tmp_path)
    raw = external_blob.read_bytes()
    target = external_blob.with_name("real-task-00.json")
    target.write_bytes(raw)
    external_blob.unlink()
    external_blob.symlink_to(target)

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_SYMLINK"):
        _resolve(manifest, input_root)


def test_task_source_outside_authorized_root_fails_closed(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    source = Path(manifest.task_manifest_path)
    outside = tmp_path / "outside.json"
    outside.write_bytes(source.read_bytes())
    manifest = replace(manifest, task_manifest_path=str(outside))

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_OUTSIDE_ROOT"):
        _resolve(manifest, input_root)


def test_task_source_symlink_fails_closed(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)
    source = Path(manifest.task_manifest_path)
    target = source.with_name("real-source.json")
    target.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(target)

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_SYMLINK"):
        _resolve(manifest, input_root)


def test_authorized_input_root_must_be_repo_external(tmp_path: Path) -> None:
    manifest, input_root, _, _ = _manifest(tmp_path)

    with pytest.raises(R25PilotContractError, match="TASK_SOURCE_INSIDE_REPOSITORY"):
        resolve_pilot_task_inputs_v1(
            manifest,
            authorized_input_root=input_root,
            repository_root=tmp_path,
        )


def test_openai_budget_covers_isolated_rubric_and_history_policy(tmp_path: Path) -> None:
    manifest, _, _, _ = _manifest(tmp_path)

    # 160 total actor calls minus the 40 mandatory Baseline cells leaves at
    # most 120 Joint-Sentinel decisions.  Those allow one rubric tracking and
    # one history-policy call each, plus 40 task-start rubric generations.
    assert replace(manifest, max_total_openai_calls=280).max_total_openai_calls == 280
    with pytest.raises(R25PilotContractError, match="INVALID_BOUND"):
        replace(manifest, max_total_openai_calls=281)
