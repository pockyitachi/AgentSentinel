from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import jsonschema
import pytest

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.integrity import CHECKER_VERSION, check_run_integrity
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    live_attempt_receipt_projection,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import LIVE_EXECUTOR_BINDING_SCHEMA_VERSION
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    OwnerAuthorizationV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceSafetyV1,
    SmokeModeV1,
    SnapshotResourceV1,
    authority_manifest_projection,
    authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    OwnedProcessIdentityV1,
    ProductionModelHandoffEvidenceV1,
    ProductionModelStopEvidenceV1,
    ProductionPilotModelSwitchEvidenceV1,
    ProductionResourceStageEvidenceV1,
    ProductionResourceTopologyV1,
    ProductionSharedGpuAttestationV1,
    SharedGpuProcessEvidenceV1,
    production_model_stop_evidence_projection,
    production_model_stop_evidence_sha256,
    production_pilot_model_switch_evidence_projection,
    production_pilot_model_switch_evidence_sha256,
    production_resource_stage_evidence_projection,
    production_shared_gpu_attestation_projection,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import openai_stage_sha256
from mobile_world.runtime.sentinel.r2_5 import integrity_gate
from mobile_world.runtime.sentinel.r2_5.integrity_gate import (
    POST_RUN_INTEGRITY_SCHEMA_VERSION,
    PostRunIntegrityAuthorityV1,
    R25PostRunIntegrityError,
    reopen_validate_post_run_integrity_artifact_v1,
    run_post_run_integrity_gate_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    OFFICIAL_SUCCESS_METRIC_ID_V1,
    OFFICIAL_SUCCESS_OPERATOR_V1,
    OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1,
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
    frozen_pilot_manifest_sha256,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _live_attempt(
    manifest: R24R25RunAuthorityManifestV1,
    roots: dict[str, str],
    *,
    logical_call_id: str,
    role: LiveAttemptRoleV1,
    index: int,
    actor_request_sha256: str,
    live_authority_sha256: str,
    lease_sha256: str,
    case_id: str,
) -> JsonValue:
    stage = next(item for item in manifest.openai_stages if item.role.value == role.value)
    receipt = LiveAttemptReceiptV1(
        attempt_id=f"attempt-{logical_call_id}-{role.value.lower()}-{index}",
        role=role,
        authority_sha256=live_authority_sha256,
        manifest_sha256=roots["manifest_sha256"],
        preflight_sha256=roots["preflight_report_sha256"],
        case_execution_lease_sha256=lease_sha256,
        stage_sha256=openai_stage_sha256(stage),
        case_id=case_id,
        logical_call_id=logical_call_id,
        actor_request_sha256=actor_request_sha256,
        request_sha256=_sha(f"attempt-request:{logical_call_id}:{role.value}:{index}"),
        transport_binding_sha256=_sha(f"attempt-transport:{logical_call_id}:{role.value}:{index}"),
        pricing_binding_sha256=roots["pricing_sha256"],
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.COMPLETED,
        dispatch_count=1,
        response_envelope_sha256=_sha(f"attempt-response:{logical_call_id}:{role.value}:{index}"),
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        total_tokens=13,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=10,
        cancellation_requested=False,
        termination=LiveAttemptTerminationV1.NONE,
        worker_pid=10_000 + index,
        worker_exit_code=0,
        worker_reaped=True,
        late_output_detected=False,
        duration_ns=1_000_000,
        failure_code=None,
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
    )
    return cast(JsonValue, live_attempt_receipt_projection(receipt))


def _production_envelope(domain: str, value: JsonValue) -> dict[str, JsonValue]:
    return {
        "domain": domain,
        "schema_version": "mobileworld.runtime.sentinel-r2.4-r2.5-production-driver-evidence/v1",
        "value": value,
    }


def _deadline() -> int:
    return time.monotonic_ns() + 600 * 1_000_000_000


def _sequence_start() -> int:
    return time.monotonic_ns() - 1_000_000


def _manifest(run_id: str) -> dict[str, Any]:
    return {
        "raw_schema_version": "mobileworld.audit.event/v1",
        "run_id": run_id,
        "repository": "Tongyi-MAI/MobileWorld",
        "git_commit": "a" * 40,
        "git_dirty": False,
        "python_version": "3.12.0",
        "mobile_world_version": "0.1.0",
        "agent_type": "r25_integrity_fixture",
        "model_name": "fixture-model",
        "suite_family": "mobile_world",
        "resolved_cli_config": {},
        "resolved_agent_runtime_config": {},
        "environment_image": "fixture-image",
        "started_at_utc": "2026-09-05T00:00:00Z",
        "collection_policy": {
            "label_free": True,
            "prompt_intervention": False,
            "collector_mode": "fail_open_with_incomplete_marker",
            "stream_chunks": True,
        },
    }


def _file_summary(path: Path) -> dict[str, int | str]:
    raw = path.read_bytes()
    return {"byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _blob_summary(root: Path) -> tuple[int, int]:
    paths = [path for path in root.rglob("*") if path.is_file()] if root.is_dir() else []
    return len(paths), sum(path.stat().st_size for path in paths)


def _build_raw_cell(
    raw_parent: Path, *, sequence_index: int, task_id: str, smoke: bool = False
) -> dict[str, JsonValue]:
    recorder = RunRecorder(
        raw_parent,
        producer=Producer.local(version="test", worker_id="r25-integrity"),
        sync=False,
    )
    recorder.write_manifest_start(_manifest(recorder.run_id))
    run_started = recorder.append_run_event("run_started", {})
    task = recorder.open_task()
    task_goal = f"Perform exact fixture task {task_id}."
    task_started = task.append_event(
        "task_started",
        {
            "task_name": task_id,
            "task_goal": task_goal,
            "task_goal_status": "resolved",
            "task_index": sequence_index + 1,
            "suite_family": "mobile_world",
            "agent": {"adapter": "fixture", "model": "fixture-model", "configuration": {}},
            "environment": {"backend_id": "fixture", "device_id": "fixture"},
            "whole_task_attempt_index": 1,
        },
    )
    screenshot_raw = b"\x89PNG\r\n\x1a\n" + (sequence_index // 4).to_bytes(4, "big")
    screenshot_ref = recorder.blob_store.put_bytes(screenshot_raw, "image/png")
    step_id = new_ulid()
    step = task.append_event(
        "step_started",
        {
            "step_id": step_id,
            "step_index": 1,
            "observation": {
                "screenshot": {
                    "height": 1,
                    "mode": "RGB",
                    "pixel_blob": screenshot_ref,
                    "representation": "canonical_png_from_runtime_pixels",
                    "source_blob": screenshot_ref,
                    "width": 1,
                }
            },
        },
        task_started["event_id"],
    )
    decision_id = new_ulid()
    decision = task.append_event(
        "agent_decision",
        {"decision_id": decision_id, "source_model_call_ids": [], "step_id": step_id},
        caused_by_event_id=step["event_id"],
    )
    if smoke:
        transition = task.append_event(
            "transition_not_executed",
            {
                "action": {},
                "decision_id": decision_id,
                "post_observation": None,
                "pre_observation_event_id": step["event_id"],
                "reason": "R2.4 live smoke forbids GUI actions",
                "step_id": step_id,
            },
            caused_by_event_id=decision["event_id"],
        )
    else:
        execution_id = new_ulid()
        execution = task.append_event(
            "action_execution_started",
            {"decision_id": decision_id, "execution_id": execution_id, "step_id": step_id},
            caused_by_event_id=decision["event_id"],
        )
        transition = task.append_event(
            "transition_completed",
            {
                "action_execution_event_id": execution["event_id"],
                "decision_id": decision_id,
                "execution_id": execution_id,
                "post_observation": {},
                "pre_observation_event_id": step["event_id"],
                "step_id": step_id,
            },
            caused_by_event_id=execution["event_id"],
        )
    reason = None if smoke else f"official reason {sequence_index}"
    task.append_event(
        "task_ended",
        {
            "capture_complete": True,
            "collector_error_event_ids": [],
            "environment_evaluation": {
                "exception": None,
                "reason": reason,
                "score": None if smoke else 1.0,
            },
            "missing_artifacts": [],
            "runtime_status": "aborted" if smoke else "completed",
            "teardown": {"exception": None, "result_snapshot_blob": None, "returned": True},
            "termination": {
                "exception": None,
                "source": "r2_4_parser_smoke_no_action" if smoke else "agent_terminal_action",
                "step_index": 1,
            },
            "token_usage": {
                "cached_tokens": 0,
                "completion_tokens": 1,
                "prompt_tokens": 1,
                "total_tokens": 2,
            },
        },
        caused_by_event_id=transition["event_id"],
    )
    task.close()
    recorder.append_run_event(
        "run_ended",
        {
            "capture_complete": True,
            "collector_error_event_ids": [],
            "manifest_final_path": "manifest.final.json",
            "runtime_status": "completed",
            "task_counts": {
                "completed": 0 if smoke else 1,
                "crashed": 1 if smoke else 0,
                "started": 1,
            },
            "task_run_ids": [task.task_run_id],
        },
        caused_by_event_id=run_started["event_id"],
    )
    blob_count, blob_bytes = _blob_summary(recorder.run_root / "blobs" / "sha256")
    recorder.write_manifest_final(
        {
            "blob_byte_count": blob_bytes,
            "blob_count": blob_count,
            "capture_complete": True,
            "collector_error_event_ids": [],
            "ended_at_utc": "2026-09-05T00:01:00Z",
            "manifest_start": _file_summary(recorder.manifest_start_path),
            "missing_artifacts": [],
            "run_stream": _file_summary(recorder.run_root / "run.events.jsonl"),
            "runtime_status": "completed",
            "task_streams": [
                {
                    **_file_summary(task.path),
                    "capture_complete": True,
                    "collector_error_event_ids": [],
                    "missing_artifacts": [],
                    "relative_path": f"tasks/{task.task_run_id}/events.jsonl",
                    "retry_planned": False,
                    "runtime_status": "aborted" if smoke else "completed",
                    "task_run_id": task.task_run_id,
                }
            ],
        }
    )
    recorder.close()
    final_path = recorder.run_root / "manifest.final.json"
    final_raw = final_path.read_bytes()
    return {
        "collector_manifest_final_byte_count": len(final_raw),
        "collector_manifest_final_path": str(final_path),
        "collector_manifest_final_sha256": hashlib.sha256(final_raw).hexdigest(),
        "collector_run_id": recorder.run_id,
        "collector_run_root": str(recorder.run_root),
        "collector_task_run_id": task.task_run_id,
        "reason": reason,
        "screenshot_sha256": hashlib.sha256(screenshot_raw).hexdigest(),
        "task_goal": task_goal,
    }


def _write_canonical(path: Path, value: JsonValue) -> bytes:
    raw = canonical_json_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def _roots() -> dict[str, str]:
    return {
        "factory_binding_sha256": _sha("factory"),
        "manifest_sha256": _sha("manifest"),
        "preflight_report_sha256": _sha("preflight"),
        "pricing_sha256": _sha("pricing"),
        "runtime_config_sha256": _sha("runtime"),
        "sentinel_config_sha256": _sha("sentinel"),
    }


def _utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _pilot_manifest(tmp_path: Path, *, task_count: int) -> FrozenPilotManifestV1:
    tasks = tuple(
        PilotTaskV1(
            task_id=f"Task{index:02d}",
            task_parameters_sha256=_sha(f"params-{index}"),
            reset_seed=1000 + index,
        )
        for index in range(task_count)
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="r25-integrity-fixture",
        frozen_at_utc="2026-09-05T00:00:00Z",
        task_manifest_path=str(tmp_path / "inputs" / "tasks.json"),
        task_manifest_sha256=_sha("task-manifest"),
        task_manifest_byte_count=100,
        topology_comparison_artifact_path=str(tmp_path / "inputs" / "topology.json"),
        topology_comparison_artifact_sha256=_sha("topology"),
        topology_comparison_artifact_byte_count=1,
        cohort_selection_artifact_path=str(tmp_path / "inputs" / "cohort.json"),
        cohort_selection_artifact_sha256=_sha("cohort-selection"),
        cohort_selection_artifact_byte_count=1,
        cohort_selection_sha256=_sha("cohort-selection"),
        task_time_authority=PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY,
        dynamic_wall_clock_tasks_excluded=True,
        tasks=tasks,
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
        max_steps_per_cell=1,
        per_cell_timeout_seconds=60,
        max_total_wall_time_seconds=6000,
        max_total_actor_calls=task_count * 4,
        max_total_openai_calls=task_count * 4,
        max_total_cost_usd_micros=1_000_000,
    )


def _actor_resource(tmp_path: Path, host: PilotHostV1, *, port: int) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    return SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=str(tmp_path / "models" / host.value / "snapshot"),
        snapshot_storage_root=str(tmp_path / "models" / host.value),
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=_sha(f"snapshot-{host.value}"),
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"fixture-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )


def _smoke_plan(tmp_path: Path, host: PilotHostV1) -> HostLiveSmokePlanV1:
    return HostLiveSmokePlanV1(
        host=host,
        cases=tuple(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=str(
                    tmp_path / "inputs" / f"{host.value.lower()}-{mode.value}.json"
                ),
                request_fixture_sha256=_sha(f"fixture-{host.value}-{mode.value}"),
                request_fixture_byte_count=10,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=60,
                max_cost_usd_micros=100,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
            for mode in SmokeModeV1
        ),
    )


def _run_manifest(
    tmp_path: Path,
    output: Path,
    roots: dict[str, str],
    *,
    task_count: int,
) -> R24R25RunAuthorityManifestV1:
    secret = tmp_path / "credentials" / "openai.key"
    secret.parent.mkdir()
    secret.write_text("unused-fixture-secret", encoding="utf-8")
    secret.chmod(0o600)
    pilot = _pilot_manifest(tmp_path, task_count=task_count)
    smokes = tuple(_smoke_plan(tmp_path, host) for host in PilotHostV1)
    cleanup_preimage = canonical_json_bytes(
        cast(
            JsonValue,
            {
                "domain": "cpu-test-full-resource-cleanup-bound",
                "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
                "value": {
                    "cleanup_upper_bound_seconds": 8,
                    "resource_topology": "SINGLE_GPU_SEQUENTIAL_SHARED",
                    "runtime_config_sha256": roots["runtime_config_sha256"],
                },
            },
        )
    )
    now = datetime.now(UTC).replace(microsecond=0)
    max_integrity = 600
    max_switches = task_count * 2 + 1
    return R24R25RunAuthorityManifestV1(
        schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
        run_id="r25-integrity-fixture",
        source_commit="b" * 40,
        authorization=OwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
            authorization_id="r25-integrity-owner",
            authorized_by="owner",
            issued_at_utc=_utc(now - timedelta(hours=1)),
            expires_at_utc=_utc(now + timedelta(days=1)),
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            pilot_gui_actions_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=SequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
                RunStageV1.R25_PILOT,
            ),
            stop_on_failure=True,
            pilot_only_after_both_smokes_pass=True,
            default_dry_run=True,
            arbitrary_commands_forbidden=True,
            secrets_in_logs_forbidden=True,
            repo_external_output_required=True,
        ),
        secret=SecretFileReferenceV1(
            path=str(secret),
            environment_key="OPENAI_API_KEY",
            required_mode=0o600,
            content_may_be_read_by_preflight=False,
            persist_value_or_hash=False,
        ),
        openai_stages=(
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.RUBRIC,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=8192,
                timeout_ms=30_000,
                max_attempts=1,
                store=False,
            ),
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.HISTORY_POLICY,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=4096,
                timeout_ms=30_000,
                max_attempts=1,
                store=False,
            ),
        ),
        actor_resources=(
            _actor_resource(tmp_path, PilotHostV1.QWEN3_VL, port=18081),
            _actor_resource(tmp_path, PilotHostV1.MAI_UI, port=18082),
        ),
        smoke_plans=cast(tuple[HostLiveSmokePlanV1, HostLiveSmokePlanV1], smokes),
        pilot=pilot,
        topology_comparison_artifact_sha256=pilot.topology_comparison_artifact_sha256,
        output_root=str(output),
        max_resource_preflight_wall_time_seconds=100,
        max_sequence_wall_time_seconds=(100 + 360 + 6000 + max_switches + 8 + max_integrity),
        max_sequence_openai_calls=12 + task_count * 4,
        max_sequence_actor_calls=6 + task_count * 4,
        max_sequence_cost_usd_micros=1_000_600,
        resource_topology="SINGLE_GPU_SEQUENTIAL_SHARED",
        runtime_config_sha256=roots["runtime_config_sha256"],
        pricing_sha256=roots["pricing_sha256"],
        sentinel_config_sha256=roots["sentinel_config_sha256"],
        max_resource_cleanup_wall_time_seconds=8,
        resource_cleanup_upper_bound_sha256=hashlib.sha256(cleanup_preimage).hexdigest(),
        max_model_switches=max_switches,
        max_model_switch_wall_time_seconds=1,
        max_total_model_switch_wall_time_seconds=max_switches,
        max_post_run_integrity_wall_time_seconds=max_integrity,
    )


def _gpu_attestation(
    *, process: OwnedProcessIdentityV1 | None = None, minimum_free: int
) -> ProductionSharedGpuAttestationV1:
    processes: tuple[SharedGpuProcessEvidenceV1, ...] = ()
    if process is not None:
        processes = (
            SharedGpuProcessEvidenceV1(
                pid=process.pid,
                process_group_id=process.process_group_id,
                session_id=process.session_id,
                starttime_ticks=process.starttime_ticks,
                uid=process.uid,
                user="fixture-owner",
                used_gpu_memory_mib=1000,
            ),
        )
    return ProductionSharedGpuAttestationV1(
        gpu_index=5,
        gpu_uuid="GPU-12345678-1234-1234-1234-123456789abc",
        total_memory_mib=80_000,
        free_memory_mib=60_000 if process is not None else 70_000,
        used_memory_mib=1000 if process is not None else 0,
        reserved_memory_mib=19_000 if process is not None else 10_000,
        gpu_utilization_percent=1 if process is not None else 0,
        memory_utilization_percent=1 if process is not None else 0,
        minimum_free_memory_mib=minimum_free,
        processes=processes,
    )


def _pilot_switches(
    manifest: R24R25RunAuthorityManifestV1,
    roots: dict[str, str],
) -> tuple[
    ProductionSharedGpuAttestationV1,
    str,
    list[dict[str, JsonValue]],
    list[str],
    list[ProductionModelStopEvidenceV1],
]:
    manifest_sha256 = roots["manifest_sha256"]
    host_blocks = [
        cell.host.value for index, cell in enumerate(manifest.pilot.cells) if index % 2 == 0
    ]
    switch_authority = integrity_gate._production_hash(
        "production-pilot-switch-authority",
        cast(
            JsonValue,
            {
                "factory_binding_sha256": roots["factory_binding_sha256"],
                "host_blocks": host_blocks,
                "manifest_sha256": manifest_sha256,
                "max_model_switches": manifest.max_model_switches,
                "max_model_switch_wall_time_seconds": (manifest.max_model_switch_wall_time_seconds),
                "max_total_model_switch_wall_time_seconds": (
                    manifest.max_total_model_switch_wall_time_seconds
                ),
                "resource_cleanup_upper_bound_seconds": (
                    manifest.max_resource_cleanup_wall_time_seconds
                ),
                "resource_cleanup_upper_bound_sha256": (
                    manifest.resource_cleanup_upper_bound_sha256
                ),
                "runtime_config_sha256": roots["runtime_config_sha256"],
                "sequence_scope_authority_sha256": manifest_sha256,
            },
        ),
    )
    lease_sha256 = _sha("shared-gpu-lease")
    baseline = _gpu_attestation(minimum_free=51_200)
    switches: list[dict[str, JsonValue]] = []
    switch_roots: list[str] = []
    source_stops: list[ProductionModelStopEvidenceV1] = []
    for index, target_name in enumerate(host_blocks):
        target_host = PilotHostV1(target_name)
        source_host = (
            PilotHostV1.MAI_UI if target_host is PilotHostV1.QWEN3_VL else PilotHostV1.QWEN3_VL
        )
        source_pid = 10_000 + 2 * index
        target_pid = source_pid + 1
        source_process = OwnedProcessIdentityV1(
            pid=source_pid,
            process_group_id=source_pid,
            session_id=source_pid,
            starttime_ticks=source_pid * 100,
            uid=os.geteuid(),
        )
        target_process = OwnedProcessIdentityV1(
            pid=target_pid,
            process_group_id=target_pid,
            session_id=target_pid,
            starttime_ticks=target_pid * 100,
            uid=os.geteuid(),
        )
        source_stop = ProductionModelStopEvidenceV1(
            host=source_host,
            process=source_process,
            endpoint=(
                "http://127.0.0.1:18081"
                if source_host is PilotHostV1.QWEN3_VL
                else "http://127.0.0.1:18082"
            ),
            leader_reaped=True,
            session_members_remaining=0,
            port_available=True,
        )
        handoff = ProductionModelHandoffEvidenceV1(
            manifest_sha256=manifest_sha256,
            runtime_config_sha256=roots["runtime_config_sha256"],
            sequence_execution_scope="R24_R25_FULL",
            sequence_scope_authority_sha256=manifest_sha256,
            resource_topology=ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED,
            gpu_lease_sha256=lease_sha256,
            source_host=source_host,
            target_host=target_host,
            source_stop=source_stop,
            baseline_shared_gpu_attestation=baseline,
            post_stop_shared_gpu_attestation=baseline,
            target_command_sha256=_sha(f"target-command-{index}"),
            target_process=target_process,
            target_health_sha256=_sha(f"target-health-{index}"),
            target_snapshot_attestation_sha256=_sha(f"target-snapshot-{index}"),
            target_ready_shared_gpu_attestation=_gpu_attestation(
                process=target_process, minimum_free=0
            ),
        )
        typed_switch = ProductionPilotModelSwitchEvidenceV1(
            switch_authority_sha256=switch_authority,
            switch_index=index,
            pilot_host_block_count=len(host_blocks),
            max_model_switches=cast(int, manifest.max_model_switches),
            max_model_switch_wall_time_seconds=cast(
                int, manifest.max_model_switch_wall_time_seconds
            ),
            max_total_model_switch_wall_time_seconds=cast(
                int, manifest.max_total_model_switch_wall_time_seconds
            ),
            transition=handoff,
        )
        envelope: dict[str, JsonValue] = {
            "domain": "production-pilot-model-switch-evidence",
            "schema_version": ("mobileworld.runtime.sentinel-r2.5-pilot-model-switch-evidence/v1"),
            "value": cast(
                JsonValue, production_pilot_model_switch_evidence_projection(typed_switch)
            ),
        }
        switches.append(envelope)
        switch_roots.append(production_pilot_model_switch_evidence_sha256(typed_switch))
        source_stops.append(source_stop)
    return baseline, lease_sha256, switches, switch_roots, source_stops


def _smoke_stage_evidence(
    *,
    manifest: R24R25RunAuthorityManifestV1,
    roots: dict[str, str],
    host: PilotHostV1,
    actor_resource_sha256: str,
    history_policy_stage_sha256: str,
    raw_parent: Path,
) -> dict[str, JsonValue]:
    stage = "QWEN_LIVE_SMOKE" if host is PilotHostV1.QWEN3_VL else "MAI_LIVE_SMOKE"
    plan = next(item for item in manifest.smoke_plans if item.host is host)
    cases: list[JsonValue] = []
    for index, expected_case in enumerate(plan.cases):
        smoke_global_index = (0 if host is PilotHostV1.QWEN3_VL else 3) + index
        raw_binding = _build_raw_cell(
            raw_parent,
            sequence_index=smoke_global_index,
            task_id=expected_case.task_id,
            smoke=True,
        )
        semantic = expected_case.mode is not SmokeModeV1.OFF
        logical_call_id = f"smoke-{host.value}-{expected_case.mode.value}"
        request_sha256 = _sha(f"smoke-request-{host.value}-{index}")
        live_authority_sha256 = _sha(f"smoke-live-authority-{host.value}-{index}")
        lease_sha256 = _sha(f"smoke-lease-{host.value}-{index}")
        attempts: list[JsonValue] = (
            [
                _live_attempt(
                    manifest,
                    roots,
                    logical_call_id=logical_call_id,
                    role=LiveAttemptRoleV1.RUBRIC,
                    index=smoke_global_index * 3 + offset,
                    actor_request_sha256=request_sha256,
                    live_authority_sha256=live_authority_sha256,
                    lease_sha256=lease_sha256,
                    case_id=expected_case.case_id,
                )
                for offset in range(2)
            ]
            + [
                _live_attempt(
                    manifest,
                    roots,
                    logical_call_id=logical_call_id,
                    role=LiveAttemptRoleV1.HISTORY_POLICY,
                    index=smoke_global_index * 3 + 2,
                    actor_request_sha256=request_sha256,
                    live_authority_sha256=live_authority_sha256,
                    lease_sha256=lease_sha256,
                    case_id=expected_case.case_id,
                )
            ]
            if semantic
            else []
        )
        attempt_roots = [integrity_gate._sha_json(item) for item in attempts]
        census: dict[str, JsonValue] = {
            "actor_actions": 0,
            "actor_calls": 1,
            "cost_usd_micros": 30 if semantic else 0,
            "history_policy_openai_calls": 1 if semantic else 0,
            "offline_rubric_evaluations": 0,
            "openai_calls": 3 if semantic else 0,
            "rubric_openai_calls": 2 if semantic else 0,
            "wall_time_ms": 1,
        }
        decision: dict[str, JsonValue] = {
            "actor_call_index": 1,
            "actor_attempt_receipt_sha256": _sha(f"smoke-actor-{host.value}-{index}"),
            "case_execution_lease_sha256": lease_sha256 if semantic else None,
            "census": census,
            "exact_diff_sha256": _sha(f"smoke-diff-{host.value}-{index}"),
            "executed_action_sha256": None,
            "fallback_check": None,
            "fallback_reason": None,
            "final_request_sha256": request_sha256,
            "history_policy_attempt_receipt_sha256": (attempt_roots[-1] if semantic else None),
            "live_policy_authority_sha256": live_authority_sha256 if semantic else None,
            "live_policy_factory_binding_sha256": roots["factory_binding_sha256"],
            "logical_call_id": logical_call_id,
            "parsed_action_sha256": _sha(f"smoke-parsed-{host.value}-{index}"),
            "parser_result_sha256": _sha(f"smoke-parser-{host.value}-{index}"),
            "pre_provider_outcome": "READY" if semantic else "OFF",
            "pre_provider_status": "READY" if semantic else "OFF",
            "preflight_report_sha256": roots["preflight_report_sha256"],
            "provider_attempt_receipt_sha256": _sha(f"smoke-provider-attempt-{host.value}-{index}"),
            "provider_request_sha256": request_sha256,
            "provider_response_sha256": _sha(f"smoke-provider-response-{host.value}-{index}"),
            "raw_request_sha256": request_sha256,
            "rubric_attempt_receipt_sha256s": cast(
                JsonValue, attempt_roots[:2] if semantic else []
            ),
            "runtime_audit_detail_sha256": _sha(f"smoke-detail-{host.value}-{index}"),
            "sentinel_receipt_sha256": _sha(f"smoke-sentinel-{host.value}-{index}"),
        }
        receipt: dict[str, JsonValue] = {
            "action_executed": False,
            "detail_sha256": decision["runtime_audit_detail_sha256"],
            "live_openai_calls": census["openai_calls"],
            "logical_call_id": logical_call_id,
            "parser_result_sha256": decision["parser_result_sha256"],
            "sentinel_receipt_sha256": decision["sentinel_receipt_sha256"],
        }
        terminal: dict[str, JsonValue] = {
            "attempt_journal_failure_code": None,
            "kind": "COMPLETED",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_roots),
            "live_attempt_receipts": attempts,
            "receipt": receipt,
            "receipt_sha256": integrity_gate._sha_json(cast(JsonValue, receipt)),
        }
        terminal["canonical_evidence_sha256"] = integrity_gate._production_hash(
            "production-unit-terminal-audit", cast(JsonValue, terminal)
        )
        execution_deadline = 10_000_000 + index * 10_000
        deadline_preimage: dict[str, JsonValue] = {
            "attempt_termination_upper_bound_ns": 100,
            "authority_deadline_monotonic_ns": execution_deadline + 2_000,
            "cleanup_deadline_monotonic_ns": execution_deadline + 1_000,
            "cleanup_grace_ns": 1_000,
            "cleanup_within_owner_authority": True,
            "execution_deadline_monotonic_ns": execution_deadline,
            "teardown_budget_ns": 900,
            "teardown_budget_positive": True,
        }
        deadline = {
            **deadline_preimage,
            "deadline_binding_sha256": integrity_gate._production_hash(
                "production-unit-deadline-binding", cast(JsonValue, deadline_preimage)
            ),
        }
        cleanup_outcome: dict[str, JsonValue] = {
            "initialization_permitted": False,
            "message_sha256": _sha("fixture cleanup succeeded"),
            "outcome": "SUCCEEDED",
            "request_dispatched": True,
            "teardown_attempted": True,
        }
        dispatches: list[JsonValue] = [
            {"kind": "fixture", "host": host.value, "mode": expected_case.mode.value}
        ]
        journal: dict[str, JsonValue] = {
            "cleanup_recovery_outcome": cleanup_outcome,
            "cleanup_recovery_outcome_sha256": integrity_gate._production_hash(
                "production-unit-cleanup-recovery-outcome",
                cast(JsonValue, cleanup_outcome),
            ),
            "collector_run_binding": {
                "collector_manifest_capture_complete": True,
                "collector_manifest_final_byte_count": raw_binding[
                    "collector_manifest_final_byte_count"
                ],
                "collector_manifest_final_path": raw_binding["collector_manifest_final_path"],
                "collector_manifest_final_sha256": raw_binding["collector_manifest_final_sha256"],
                "collector_manifest_runtime_status": "completed",
                "collector_run_id": raw_binding["collector_run_id"],
                "collector_run_root": raw_binding["collector_run_root"],
                "collector_task_run_id": raw_binding["collector_task_run_id"],
            },
            "completed_decisions": [decision],
            "completed_decisions_sha256": integrity_gate._production_hash(
                "production-unit-decision-journal", [decision]
            ),
            "resource_dispatch_records": dispatches,
            "resource_dispatch_records_sha256": integrity_gate._production_hash(
                "production-unit-resource-dispatch-journal", cast(JsonValue, dispatches)
            ),
            "run_fatal_state": None,
            "run_fatal_state_sha256": None,
            "official_result_evidence": None,
            "official_result_evidence_sha256": None,
            "reset_evidence": None,
            "reset_evidence_sha256": None,
            "terminal_audit_records": [terminal],
            "terminal_audit_records_sha256": integrity_gate._production_hash(
                "production-unit-terminal-audit-journal", [terminal]
            ),
            "unit_deadline": cast(JsonValue, deadline),
            "unit_id": f"smoke:{host.value}:{expected_case.mode.value}",
        }
        journal["collector_run_binding_sha256"] = integrity_gate._production_hash(
            "production-collector-run-binding",
            cast(JsonValue, journal["collector_run_binding"]),
        )
        journal_raw = canonical_json_bytes(cast(JsonValue, journal))
        cases.append(
            cast(
                JsonValue,
                {
                    "actor_resource_sha256": actor_resource_sha256,
                    "case_id": expected_case.case_id,
                    "census": census,
                    "cleanup_receipt_sha256": _sha(f"smoke-cleanup-{host.value}-{index}"),
                    "decision": decision,
                    "history_policy_stage_sha256": history_policy_stage_sha256,
                    "host": host.value,
                    "manifest_sha256": roots["manifest_sha256"],
                    "mode": expected_case.mode.value,
                    "request_fixture_byte_count": expected_case.request_fixture_byte_count,
                    "request_fixture_sha256": expected_case.request_fixture_sha256,
                    "run_id": manifest.run_id,
                    "sequence_index": index,
                    "stage": stage,
                    "task_id": expected_case.task_id,
                    "unit_journal": journal,
                    "unit_journal_byte_count": len(journal_raw),
                    "unit_journal_sha256": hashlib.sha256(journal_raw).hexdigest(),
                },
            )
        )
    census_fields = cast(dict[str, JsonValue], cast(dict[str, Any], cases[0])["census"])
    stage_census = {
        field: sum(cast(int, cast(dict[str, Any], item)["census"][field]) for item in cases)
        for field in census_fields
    }
    return {
        "actor_resource_sha256": actor_resource_sha256,
        "cases": cases,
        "census": cast(JsonValue, stage_census),
        "history_policy_stage_sha256": history_policy_stage_sha256,
        "host": host.value,
        "manifest_sha256": roots["manifest_sha256"],
        "run_id": manifest.run_id,
        "schema_version": ("mobileworld.runtime.sentinel-r2.5-production-smoke-evidence/v3"),
        "stage": stage,
    }


def _build_sequence(
    tmp_path: Path, *, cell_count: int = 80
) -> tuple[Path, Path, Path, PostRunIntegrityAuthorityV1]:
    repository = tmp_path / "repo"
    repository.mkdir(mode=0o700)
    raw_parent = tmp_path / "raw"
    raw_parent.mkdir(mode=0o700)
    output = tmp_path / "sequence"
    output.mkdir(mode=0o700)
    roots = _roots()
    run_id = "r25-integrity-fixture"
    run_manifest = _run_manifest(tmp_path, output, roots, task_count=cell_count // 4)
    roots["manifest_sha256"] = authority_manifest_sha256(run_manifest)
    resolved_inputs_sha256 = _sha("resolved-pilot-inputs")
    manifest_projection = authority_manifest_projection(run_manifest)
    projected_resources = cast(list[dict[str, JsonValue]], manifest_projection["actor_resources"])
    actor_resource_sha256s = {
        cast(str, resource["host"]): integrity_gate._production_hash(
            "actor-resource", cast(JsonValue, resource)
        )
        for resource in projected_resources
    }
    actor_resources_sha256 = integrity_gate._production_hash(
        "actor-resource-matrix",
        cast(
            JsonValue,
            [
                {
                    "host": resource["host"],
                    "resource_sha256": actor_resource_sha256s[cast(str, resource["host"])],
                }
                for resource in projected_resources
            ],
        ),
    )
    policy_stage = next(
        cast(dict[str, JsonValue], stage)
        for stage in cast(list[JsonValue], manifest_projection["openai_stages"])
        if cast(dict[str, JsonValue], stage)["role"] == "HISTORY_POLICY"
    )
    history_policy_stage_sha256 = integrity_gate._production_hash(
        "history-policy-stage", cast(JsonValue, policy_stage)
    )
    baseline_attestation, lease_sha256, switches, switch_roots, source_stops = _pilot_switches(
        run_manifest, roots
    )
    cells: list[JsonValue] = []
    for index in range(cell_count):
        task_id = f"Task{index // 4:02d}"
        raw_binding = _build_raw_cell(raw_parent, sequence_index=index, task_id=task_id)
        unit_id = f"pilot:{index:03d}"
        task_parameters_sha256 = _sha(f"params-{index // 4}")
        reset_seed = 1000 + index // 4
        effective_value: JsonValue = {
            "observation_screenshot_sha256": raw_binding["screenshot_sha256"],
            "reset_seed": reset_seed,
            "task_goal_sha256": hashlib.sha256(
                cast(str, raw_binding["task_goal"]).encode()
            ).hexdigest(),
            "task_id": task_id,
            "task_name": task_id,
            "task_parameters_sha256": task_parameters_sha256,
            "trial": 1,
        }
        effective_sha256 = integrity_gate._production_hash(
            "production-pilot-effective-reset-state",
            cast(
                JsonValue,
                integrity_gate._pilot_effective_reset_match_projection(
                    cast(dict[str, JsonValue], effective_value)
                ),
            ),
        )
        reset_value: dict[str, JsonValue] = {
            "backend_endpoint": "http://127.0.0.1:6800",
            "case_id": f"pilot-cell-{index:03d}",
            "effective_reset_state": effective_value,
            "effective_reset_state_sha256": effective_sha256,
            "manifest_sha256": roots["manifest_sha256"],
            "observation_screenshot_sha256": raw_binding["screenshot_sha256"],
            "resolved_inputs_sha256": resolved_inputs_sha256,
            "reset_seed": reset_seed,
            "resource_switch_evidence_sha256": (
                switch_roots[index // 2] if index % 2 == 0 else None
            ),
            "task_id": task_id,
            "task_name": task_id,
            "task_parameters_sha256": task_parameters_sha256,
            "trial": 1,
        }
        reset_evidence = _production_envelope(
            "production-pilot-reset", cast(JsonValue, reset_value)
        )
        reset_receipt_sha256 = integrity_gate._sha_json(cast(JsonValue, reset_evidence))
        reason_sha256 = hashlib.sha256(cast(str, raw_binding["reason"]).encode()).hexdigest()
        official_value: dict[str, JsonValue] = {
            "evaluator_id": "mobileworld.task.official-success/v1",
            "official_success_metric_id": OFFICIAL_SUCCESS_METRIC_ID_V1,
            "official_success_operator": OFFICIAL_SUCCESS_OPERATOR_V1,
            "official_success_threshold_float_hex": (OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1),
            "reason": raw_binding["reason"],
            "reason_sha256": reason_sha256,
            "score_float_hex": (1.0).hex(),
            "score_ppm": 1_000_000,
            "task_id": task_id,
        }
        official_evidence = _production_envelope(
            "production-official-result", cast(JsonValue, official_value)
        )
        official_evidence_sha256 = integrity_gate._sha_json(cast(JsonValue, official_evidence))
        collector_binding: dict[str, JsonValue] = {
            "collector_manifest_capture_complete": True,
            "collector_manifest_final_byte_count": raw_binding[
                "collector_manifest_final_byte_count"
            ],
            "collector_manifest_final_path": raw_binding["collector_manifest_final_path"],
            "collector_manifest_final_sha256": raw_binding["collector_manifest_final_sha256"],
            "collector_manifest_runtime_status": "completed",
            "collector_run_id": raw_binding["collector_run_id"],
            "collector_run_root": raw_binding["collector_run_root"],
            "collector_task_run_id": raw_binding["collector_task_run_id"],
        }
        joint = index % 2 == 1
        decision_census: dict[str, JsonValue] = {
            "actor_actions": 1,
            "actor_calls": 1,
            "cost_usd_micros": 20 if joint else 0,
            "history_policy_openai_calls": 0,
            "offline_rubric_evaluations": 0,
            "openai_calls": 2 if joint else 0,
            "rubric_openai_calls": 2 if joint else 0,
            "wall_time_ms": 1,
        }
        logical_call_id = f"logical-{index}"
        raw_request_sha256 = _sha(f"raw-request-{index}")
        live_authority_sha256 = _sha(f"live-authority-{index}")
        case_lease_sha256 = _sha(f"case-lease-{index}")
        attempts: list[JsonValue] = (
            [
                _live_attempt(
                    run_manifest,
                    roots,
                    logical_call_id=logical_call_id,
                    role=LiveAttemptRoleV1.RUBRIC,
                    index=index * 2 + offset,
                    actor_request_sha256=raw_request_sha256,
                    live_authority_sha256=live_authority_sha256,
                    lease_sha256=case_lease_sha256,
                    case_id=f"pilot-cell-{index:03d}",
                )
                for offset in range(2)
            ]
            if joint
            else []
        )
        attempt_hashes = [integrity_gate._sha_json(item) for item in attempts]
        final_request_sha256 = raw_request_sha256
        parsed_action_sha256 = _sha(f"parsed-action-{index}")
        decision: dict[str, JsonValue] = {
            "actor_call_index": 1,
            "actor_attempt_receipt_sha256": _sha(f"actor-attempt-{index}"),
            "case_execution_lease_sha256": case_lease_sha256 if joint else None,
            "census": decision_census,
            "exact_diff_sha256": _sha(f"exact-diff-{index}"),
            "executed_action_sha256": parsed_action_sha256,
            "fallback_check": ("r2_4_no_history_r21_v1_compatibility" if joint else None),
            "fallback_reason": "HISTORY_EXTRACTION_FAILURE" if joint else None,
            "final_request_sha256": final_request_sha256,
            "history_policy_attempt_receipt_sha256": None,
            "live_policy_authority_sha256": live_authority_sha256 if joint else None,
            "live_policy_factory_binding_sha256": roots["factory_binding_sha256"],
            "logical_call_id": logical_call_id,
            "parsed_action_sha256": parsed_action_sha256,
            "parser_result_sha256": _sha(f"parser-{index}"),
            "pre_provider_outcome": ("NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL" if joint else "OFF"),
            "pre_provider_status": "FALLBACK_ORIGINAL" if joint else "OFF",
            "preflight_report_sha256": roots["preflight_report_sha256"],
            "provider_attempt_receipt_sha256": _sha(f"provider-attempt-{index}"),
            "provider_request_sha256": final_request_sha256,
            "provider_response_sha256": _sha(f"provider-response-{index}"),
            "raw_request_sha256": raw_request_sha256,
            "rubric_attempt_receipt_sha256s": cast(JsonValue, attempt_hashes),
            "runtime_audit_detail_sha256": _sha(f"detail-{index}"),
            "sentinel_receipt_sha256": _sha(f"sentinel-{index}"),
        }
        receipt: dict[str, JsonValue] = {
            "action_executed": True,
            "detail_sha256": decision["runtime_audit_detail_sha256"],
            "live_openai_calls": decision_census["openai_calls"],
            "logical_call_id": logical_call_id,
            "parser_result_sha256": decision["parser_result_sha256"],
            "sentinel_receipt_sha256": decision["sentinel_receipt_sha256"],
        }
        terminal_record: dict[str, JsonValue] = {
            "attempt_journal_failure_code": None,
            "kind": "COMPLETED",
            "live_attempt_receipt_sha256s": cast(JsonValue, attempt_hashes),
            "live_attempt_receipts": cast(JsonValue, attempts),
            "receipt": receipt,
            "receipt_sha256": integrity_gate._sha_json(cast(JsonValue, receipt)),
        }
        terminal_record["canonical_evidence_sha256"] = integrity_gate._production_hash(
            "production-unit-terminal-audit", cast(JsonValue, terminal_record)
        )
        execution_deadline = 1_000_000 + index * 10_000
        deadline_preimage: dict[str, JsonValue] = {
            "attempt_termination_upper_bound_ns": 100,
            "authority_deadline_monotonic_ns": execution_deadline + 2_000,
            "cleanup_deadline_monotonic_ns": execution_deadline + 1_000,
            "cleanup_grace_ns": 1_000,
            "cleanup_within_owner_authority": True,
            "execution_deadline_monotonic_ns": execution_deadline,
            "teardown_budget_ns": 900,
            "teardown_budget_positive": True,
        }
        deadline_binding = {
            **deadline_preimage,
            "deadline_binding_sha256": integrity_gate._production_hash(
                "production-unit-deadline-binding", cast(JsonValue, deadline_preimage)
            ),
        }
        dispatches: list[JsonValue] = [{"kind": "fixture", "sequence_index": index}]
        cleanup_recovery_outcome: dict[str, JsonValue] = {
            "initialization_permitted": False,
            "message_sha256": _sha("fixture cleanup succeeded"),
            "outcome": "SUCCEEDED",
            "request_dispatched": True,
            "teardown_attempted": True,
        }
        unit_journal: dict[str, JsonValue] = {
            "completed_decisions": [decision],
            "completed_decisions_sha256": integrity_gate._production_hash(
                "production-unit-decision-journal", [decision]
            ),
            "terminal_audit_records": [terminal_record],
            "terminal_audit_records_sha256": integrity_gate._production_hash(
                "production-unit-terminal-audit-journal", [terminal_record]
            ),
            "resource_dispatch_records": dispatches,
            "resource_dispatch_records_sha256": integrity_gate._production_hash(
                "production-unit-resource-dispatch-journal", cast(JsonValue, dispatches)
            ),
            "cleanup_recovery_outcome": cleanup_recovery_outcome,
            "cleanup_recovery_outcome_sha256": integrity_gate._production_hash(
                "production-unit-cleanup-recovery-outcome",
                cast(JsonValue, cleanup_recovery_outcome),
            ),
            "run_fatal_state": None,
            "run_fatal_state_sha256": None,
            "reset_evidence": reset_value,
            "reset_evidence_sha256": reset_receipt_sha256,
            "official_result_evidence": official_value,
            "official_result_evidence_sha256": official_evidence_sha256,
            "collector_run_binding": collector_binding,
            "collector_run_binding_sha256": integrity_gate._production_hash(
                "production-collector-run-binding", cast(JsonValue, collector_binding)
            ),
            "unit_deadline": cast(JsonValue, deadline_binding),
            "unit_id": unit_id,
        }
        unit_journal_sha256 = integrity_gate._sha_json(cast(JsonValue, unit_journal))
        locator: dict[str, JsonValue] = {
            **collector_binding,
            "manifest_sha256": roots["manifest_sha256"],
            "run_id": run_id,
            "sequence_index": index,
            "task_id": task_id,
            "unit_id": unit_id,
            "unit_journal_sha256": unit_journal_sha256,
        }
        official: dict[str, JsonValue] = {
            "evaluator_id": "mobileworld.task.official-success/v1",
            "official_success_metric_id": OFFICIAL_SUCCESS_METRIC_ID_V1,
            "official_success_operator": OFFICIAL_SUCCESS_OPERATOR_V1,
            "official_success_threshold_float_hex": (OFFICIAL_SUCCESS_THRESHOLD_FLOAT_HEX_V1),
            "reason_sha256": reason_sha256,
            "result_payload_sha256": official_evidence_sha256,
            "score_float_hex": (1.0).hex(),
            "score_ppm": 1_000_000,
            "successful": True,
            "task_id": task_id,
        }
        teardown_result: dict[str, JsonValue] = {
            "message": "fixture cleanup succeeded",
            "message_sha256": hashlib.sha256(b"fixture cleanup succeeded").hexdigest(),
            "request_dispatched": True,
            "status": "SUCCEEDED",
            "task_name": task_id,
        }
        cleanup_value: dict[str, JsonValue] = {
            "cleanup_dispatch_authorized": True,
            "collector_manifest_sha256": raw_binding["collector_manifest_final_sha256"],
            "collector_run_locator": locator,
            "collector_run_locator_sha256": integrity_gate._sha_json(cast(JsonValue, locator)),
            "deadline_binding": cast(JsonValue, deadline_binding),
            "manifest_sha256": roots["manifest_sha256"],
            "task_run_id": raw_binding["collector_task_run_id"],
            "teardown_attempted": True,
            "teardown_result": teardown_result,
            "teardown_result_sha256": integrity_gate._production_hash(
                "production-task-teardown-result", cast(JsonValue, teardown_result)
            ),
            "unit_id": unit_id,
            "unit_journal_sha256": unit_journal_sha256,
        }
        cleanup_evidence = _production_envelope(
            "production-unit-cleanup", cast(JsonValue, cleanup_value)
        )
        cleanup_evidence_sha256 = integrity_gate._sha_json(cast(JsonValue, cleanup_evidence))
        cell: dict[str, JsonValue] = {
            "actor_resource_sha256": actor_resource_sha256s[
                "QWEN3_VL" if index % 4 < 2 else "MAI_UI"
            ],
            "arm": "BASELINE" if index % 2 == 0 else "JOINT_SENTINEL",
            "collector_run_locator": locator,
            "collector_run_locator_sha256": integrity_gate._sha_json(cast(JsonValue, locator)),
            "cleanup_evidence": cleanup_evidence,
            "cleanup_evidence_sha256": cleanup_evidence_sha256,
            "cleanup_receipt_sha256": cleanup_evidence_sha256,
            "census": decision_census,
            "decisions": [decision],
            "effective_reset_state_sha256": effective_sha256,
            "host": "QWEN3_VL" if index % 4 < 2 else "MAI_UI",
            "history_policy_stage_sha256": history_policy_stage_sha256,
            "manifest_sha256": roots["manifest_sha256"],
            "official_result": official,
            "official_result_evidence": official_evidence,
            "official_result_evidence_sha256": official_evidence_sha256,
            "reset_evidence": reset_evidence,
            "reset_evidence_sha256": integrity_gate._sha_json(cast(JsonValue, reset_evidence)),
            "reset_receipt_sha256": reset_receipt_sha256,
            "reset_seed": reset_seed,
            "run_id": run_id,
            "sentinel_mode": "OFF" if index % 2 == 0 else "ACTIVE",
            "sequence_index": index,
            "task_id": task_id,
            "task_parameters_sha256": task_parameters_sha256,
            "unit_journal": unit_journal,
            "unit_journal_byte_count": len(canonical_json_bytes(cast(JsonValue, unit_journal))),
            "unit_journal_sha256": unit_journal_sha256,
        }
        cells.append(cast(JsonValue, cell))

    resource_pid = 9000
    resource_process = OwnedProcessIdentityV1(
        pid=resource_pid,
        process_group_id=resource_pid,
        session_id=resource_pid,
        starttime_ticks=resource_pid * 100,
        uid=os.geteuid(),
    )
    resource_stage = ProductionResourceStageEvidenceV1(
        manifest_sha256=roots["manifest_sha256"],
        runtime_config_sha256=roots["runtime_config_sha256"],
        runtime_attestation_sha256=_sha("runtime-attestation"),
        backend_command_sha256=_sha("backend-command"),
        backend_container_id=_sha("backend-container"),
        backend_health_sha256=_sha("backend-health"),
        model_command_sha256s=(_sha("qwen-command"), _sha("mai-command")),
        model_processes=(resource_process,),
        model_health_sha256s=(_sha("qwen-health"),),
        gpu_lease_sha256s=(lease_sha256,),
        gpu_idle_attestation_sha256s=(),
        shared_gpu_attestations=(
            baseline_attestation,
            _gpu_attestation(process=resource_process, minimum_free=0),
        ),
        active_hosts=(PilotHostV1.QWEN3_VL,),
        resource_topology=ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED,
        vllm_gpu_memory_utilization="0.24",
        minimum_free_gpu_memory_mib=51_200,
        sequence_execution_scope="R24_R25_FULL",
        sequence_scope_authority_sha256=roots["manifest_sha256"],
        pilot_switch_authority_sha256=cast(
            str, cast(dict[str, Any], switches[0]["value"])["switch_authority_sha256"]
        ),
    )
    resource_stage_evidence: dict[str, JsonValue] = {
        "domain": "production-resource-stage-evidence",
        "schema_version": ("mobileworld.runtime.sentinel-r2.4-shared-resource-evidence/v2"),
        "value": cast(JsonValue, production_resource_stage_evidence_projection(resource_stage)),
    }
    cleanup_bound: dict[str, JsonValue] = {
        "domain": "cpu-test-full-resource-cleanup-bound",
        "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
        "value": {
            "cleanup_upper_bound_seconds": run_manifest.max_resource_cleanup_wall_time_seconds,
            "resource_topology": run_manifest.resource_topology,
            "runtime_config_sha256": roots["runtime_config_sha256"],
        },
    }
    binding: dict[str, JsonValue] = {
        **roots,
        "authorized_stages": [stage for stage, _ in integrity_gate._STAGE_FILES],
        "execution_scope": "R24_R25_FULL",
        "max_model_switch_wall_time_seconds": (run_manifest.max_model_switch_wall_time_seconds),
        "max_model_switches": run_manifest.max_model_switches,
        "max_total_model_switch_wall_time_seconds": (
            run_manifest.max_total_model_switch_wall_time_seconds
        ),
        "pilot_switch_authority_sha256": resource_stage.pilot_switch_authority_sha256,
        "production_evidence_required": True,
        "resource_cleanup_upper_bound": cleanup_bound,
        "resource_cleanup_upper_bound_seconds": (
            run_manifest.max_resource_cleanup_wall_time_seconds
        ),
        "resource_cleanup_upper_bound_sha256": (run_manifest.resource_cleanup_upper_bound_sha256),
        "resource_topology": run_manifest.resource_topology,
        "run_id": run_id,
        "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
        "source_commit": "b" * 40,
    }
    stage_documents: list[tuple[str, dict[str, JsonValue], bytes]] = []
    receipts: list[JsonValue] = []
    for stage, filename in integrity_gate._STAGE_FILES:
        receipt_census: dict[str, JsonValue]
        if stage == "R25_PILOT":
            census_fields = (
                "actor_actions",
                "actor_calls",
                "cost_usd_micros",
                "history_policy_openai_calls",
                "offline_rubric_evaluations",
                "openai_calls",
                "rubric_openai_calls",
                "wall_time_ms",
            )
            pilot_census: dict[str, JsonValue] = {
                field: sum(cast(int, cast(dict[str, Any], cell)["census"][field]) for cell in cells)
                for field in census_fields
            }
            evidence = {
                "actor_resources_sha256": actor_resources_sha256,
                "cells": cast(JsonValue, cells),
                "census": pilot_census,
                "history_policy_stage_sha256": history_policy_stage_sha256,
                "manifest_sha256": roots["manifest_sha256"],
                "pilot_manifest_sha256": frozen_pilot_manifest_sha256(run_manifest.pilot),
                "run_id": run_id,
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.5-production-pilot-evidence/v2"
                ),
            }
            receipt_census = pilot_census
        elif stage == "RESOURCE_PREFLIGHT":
            evidence = resource_stage_evidence
            receipt_census = {
                "actor_actions": 0,
                "actor_calls": 0,
                "cost_usd_micros": 0,
                "openai_calls": 0,
                "wall_time_ms": 1,
            }
        else:
            host = PilotHostV1.QWEN3_VL if stage == "QWEN_LIVE_SMOKE" else PilotHostV1.MAI_UI
            evidence = _smoke_stage_evidence(
                manifest=run_manifest,
                roots=roots,
                host=host,
                actor_resource_sha256=actor_resource_sha256s[host.value],
                history_policy_stage_sha256=history_policy_stage_sha256,
                raw_parent=raw_parent,
            )
            receipt_census = cast(dict[str, JsonValue], evidence["census"])
            if stage == "MAI_LIVE_SMOKE":
                handoff_value = cast(
                    dict[str, JsonValue],
                    cast(dict[str, JsonValue], switches[1]["value"])["transition"],
                )
                handoff_evidence: dict[str, JsonValue] = {
                    "domain": "production-model-handoff-evidence",
                    "schema_version": (
                        "mobileworld.runtime.sentinel-r2.4-model-handoff-evidence/v1"
                    ),
                    "value": handoff_value,
                }
                evidence = {
                    "domain": "r24-r25-full-mai-stage-evidence",
                    "handoff_evidence": handoff_evidence,
                    "handoff_evidence_sha256": integrity_gate._sha_json(
                        cast(JsonValue, handoff_evidence)
                    ),
                    "manifest_sha256": roots["manifest_sha256"],
                    "smoke_evidence": evidence,
                    "smoke_evidence_sha256": integrity_gate._sha_json(cast(JsonValue, evidence)),
                }
        expected_host = "QWEN3_VL" if stage == "QWEN_LIVE_SMOKE" else "MAI_UI"
        receipt: dict[str, JsonValue] = {
            "actor_actions": receipt_census["actor_actions"],
            "actor_calls": receipt_census["actor_calls"],
            "completed_units": (
                ["resources"]
                if stage == "RESOURCE_PREFLIGHT"
                else (
                    [
                        f"{expected_host}:{case.mode.value}"
                        for case in next(
                            plan
                            for plan in run_manifest.smoke_plans
                            if plan.host.value == expected_host
                        ).cases
                    ]
                    if stage in {"QWEN_LIVE_SMOKE", "MAI_LIVE_SMOKE"}
                    else [
                        f"pilot-cell-{index:03d}"
                        for index, _ in enumerate(run_manifest.pilot.cells)
                    ]
                )
            ),
            "cost_usd_micros": receipt_census["cost_usd_micros"],
            "evidence_sha256": integrity_gate._sha_json(cast(JsonValue, evidence)),
            "manifest_sha256": roots["manifest_sha256"],
            "openai_calls": receipt_census["openai_calls"],
            "passed": True,
            "provider_final_request_proven": stage != "RESOURCE_PREFLIGHT",
            "stage": stage,
            "wall_time_ms": receipt_census["wall_time_ms"],
        }
        document: dict[str, JsonValue] = {
            **binding,
            "evidence": evidence,
            "receipt": receipt,
        }
        raw = _write_canonical(output / filename, cast(JsonValue, document))
        stage_documents.append((stage, document, raw))
        receipts.append(cast(JsonValue, receipt))
    stopped_models = [
        cast(JsonValue, production_model_stop_evidence_projection(item)) for item in source_stops
    ]
    stopped_model_sha256s = [production_model_stop_evidence_sha256(item) for item in source_stops]
    residual: dict[str, JsonValue] = {
        "admitted_model_processes": [],
        "backend_candidates": [],
        "partial_model_processes": [],
        "pending_backend_ids": [],
        "pending_backend_names": [],
    }
    final_attestation = _gpu_attestation(minimum_free=0)
    reclaimed_value: dict[str, JsonValue] = {
        "backend_container_id": _sha("backend-container"),
        "final_shared_gpu_attestation": cast(
            JsonValue, production_shared_gpu_attestation_projection(final_attestation)
        ),
        "gpu_lease_sha256s": [lease_sha256],
        "manifest_sha256": roots["manifest_sha256"],
        "residual_capabilities": residual,
        "resource_topology": "SINGLE_GPU_SEQUENTIAL_SHARED",
        "runtime_config_sha256": roots["runtime_config_sha256"],
        "sequence_execution_scope": "R24_R25_FULL",
        "sequence_scope_authority_sha256": roots["manifest_sha256"],
        "status": "RECLAIMED",
        "stopped_models": stopped_models,
    }
    reclaimed: dict[str, JsonValue] = {
        "domain": "production-resource-reclaimed-cleanup-outcome",
        "schema_version": ("mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"),
        "value": reclaimed_value,
    }
    cleanup_value: dict[str, JsonValue] = {
        "backend_container_id": _sha("backend-container"),
        "baseline_shared_gpu_attestation": cast(
            JsonValue, production_shared_gpu_attestation_projection(baseline_attestation)
        ),
        "cleanup_outcome": "SHARED_MODELS_RECLAIMED",
        "final_shared_gpu_attestation": cast(
            JsonValue, production_shared_gpu_attestation_projection(final_attestation)
        ),
        "final_shared_gpu_attestation_sha256": (
            integrity_gate.production_shared_gpu_attestation_sha256(final_attestation)
        ),
        "gpu_lease_released": True,
        "gpu_lease_sha256s": [lease_sha256],
        "manifest_sha256": roots["manifest_sha256"],
        "minimum_free_gpu_memory_mib": 51_200,
        "pilot_model_switch_evidence": cast(JsonValue, switches),
        "pilot_model_switch_evidence_sha256s": cast(JsonValue, switch_roots),
        "reclaimed_cleanup_outcome": reclaimed,
        "reclaimed_cleanup_outcome_sha256": integrity_gate._sha_json(cast(JsonValue, reclaimed)),
        "residual_capabilities": residual,
        "residual_capabilities_sha256": integrity_gate._production_hash(
            "production-resource-residual-capabilities", cast(JsonValue, residual)
        ),
        "resource_topology": "SINGLE_GPU_SEQUENTIAL_SHARED",
        "runtime_config_sha256": roots["runtime_config_sha256"],
        "sequence_execution_scope": "R24_R25_FULL",
        "sequence_scope_authority_sha256": roots["manifest_sha256"],
        "shared_gpu_tenant_continuity_status": "UNCHANGED_OR_EXITED",
        "status": "CLEANED",
        "stopped_model_sha256s": cast(JsonValue, stopped_model_sha256s),
        "stopped_models": cast(JsonValue, stopped_models),
        "unconsumed_pilot_model_switch_evidence_sha256s": [],
        "vllm_gpu_memory_utilization": "0.24",
    }
    cleanup_evidence: dict[str, JsonValue] = {
        "domain": "production-resource-cleanup-evidence",
        "schema_version": ("mobileworld.runtime.sentinel-r2.4-resource-cleanup-evidence/v1"),
        "value": cleanup_value,
    }
    cleanup_document: dict[str, JsonValue] = {
        **binding,
        "resource_cleanup_evidence": cleanup_evidence,
        "resource_cleanup_evidence_sha256": integrity_gate._sha_json(
            cast(JsonValue, cleanup_evidence)
        ),
        "resource_cleanup_status": "SUCCEEDED",
    }
    cleanup_raw = _write_canonical(
        output / "04-resource-cleanup.json", cast(JsonValue, cleanup_document)
    )
    _write_canonical(output / "manifest-binding.json", cast(JsonValue, binding))
    receipt_objects = [cast(dict[str, JsonValue], item) for item in receipts]
    stage_wall_time_ms = sum(cast(int, item["wall_time_ms"]) for item in receipt_objects)
    census: dict[str, JsonValue] = {
        "actor_actions": sum(cast(int, item["actor_actions"]) for item in receipt_objects),
        "actor_calls": sum(cast(int, item["actor_calls"]) for item in receipt_objects),
        "cleanup_attempted": True,
        "cleanup_evidence_sha256": cleanup_document["resource_cleanup_evidence_sha256"],
        "cleanup_succeeded": True,
        "cleanup_wall_time_ms": 1,
        "completed_stages": [stage for stage, _ in integrity_gate._STAGE_FILES],
        "cost_usd_micros": sum(cast(int, item["cost_usd_micros"]) for item in receipt_objects),
        "openai_calls": sum(cast(int, item["openai_calls"]) for item in receipt_objects),
        "output_committed": True,
        "secret_leases_acquired": 3,
        "secret_leases_closed": 3,
        "stage_wall_time_ms": stage_wall_time_ms,
        "state": "COMPLETE",
        "wall_time_ms": stage_wall_time_ms + 1,
    }
    result: dict[str, JsonValue] = {
        "failed_stage": None,
        "failure_code": None,
        "manifest_sha256": roots["manifest_sha256"],
        "receipts": receipts,
        "run_id": run_id,
        "schema_version": "mobileworld.runtime.sentinel-r2.4-r2.5-sequence-result/v1",
        "status": "COMPLETE",
    }
    initial_handoff_evidence = cast(
        dict[str, JsonValue],
        cast(dict[str, JsonValue], stage_documents[2][1]["evidence"])["handoff_evidence"],
    )
    terminal: dict[str, JsonValue] = {
        **binding,
        "acceptance_status": "EXECUTION_COMPLETE_INTEGRITY_PENDING",
        "cleanup_file_sha256": hashlib.sha256(cleanup_raw).hexdigest(),
        "executor_census": census,
        "executor_census_sha256": integrity_gate._sha_json(cast(JsonValue, census)),
        "handoff_model_switch_count": 1,
        "handoff_model_switch_evidence_sha256": integrity_gate._sha_json(
            cast(JsonValue, initial_handoff_evidence)
        ),
        "pilot_model_switch_count": cast(int, run_manifest.max_model_switches) - 1,
        "result": result,
        "result_sha256": integrity_gate._sha_json(cast(JsonValue, result)),
        "stage_file_sha256s": {
            stage: hashlib.sha256(raw).hexdigest() for stage, _, raw in stage_documents
        },
        "status": "COMPLETE",
        "terminal_output_published": True,
        "total_model_switch_count": run_manifest.max_model_switches,
    }
    _write_canonical(output / "terminal.json", cast(JsonValue, terminal))
    authority = PostRunIntegrityAuthorityV1(
        run_id=run_id,
        authority_manifest_sha256=roots["manifest_sha256"],
        preflight_report_sha256=roots["preflight_report_sha256"],
        runtime_config_sha256=roots["runtime_config_sha256"],
        pricing_sha256=roots["pricing_sha256"],
        sentinel_config_sha256=roots["sentinel_config_sha256"],
        factory_binding_sha256=roots["factory_binding_sha256"],
        run_manifest=run_manifest,
        source_commit=run_manifest.source_commit,
        pilot_manifest=run_manifest.pilot,
        pilot_manifest_sha256=frozen_pilot_manifest_sha256(run_manifest.pilot),
        resolved_pilot_inputs_sha256=resolved_inputs_sha256,
        backend_endpoint="http://127.0.0.1:6800",
        expected_cell_count=cell_count,
        max_sequence_wall_time_seconds=run_manifest.max_sequence_wall_time_seconds,
        max_wall_time_seconds=600,
    )
    return (
        repository,
        output,
        integrity_gate.derived_post_run_integrity_root_v1(output),
        authority,
    )


def test_gate_runs_official_checker_for_all_cells_and_strictly_reopens(
    tmp_path: Path,
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    artifact, artifact_sha256, artifact_path = run_post_run_integrity_gate_v1(
        sequence_output_root=output,
        repository_root=repository,
        authority=authority,
        sequence_started_monotonic_ns=_sequence_start(),
        sequence_deadline_monotonic_ns=_deadline(),
    )

    assert artifact["status"] == "VALID"
    assert artifact["checker_version"] == CHECKER_VERSION
    assert artifact["collector_run_count"] == 86
    assert artifact["pilot_collector_run_count"] == 80
    assert artifact["smoke_collector_run_count"] == 6
    assert len(cast(list[JsonValue], artifact["pilot_collector_runs"])) == 80
    assert len(cast(list[JsonValue], artifact["smoke_collector_runs"])) == 6
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    reopened, reopened_sha256 = reopen_validate_post_run_integrity_artifact_v1(
        artifact_path,
        repository_root=repository,
        authority=authority,
        rerun_official_checker=True,
    )
    assert reopened == artifact
    assert reopened_sha256 == artifact_sha256

    schema_path = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff/schemas/r2_5/post_run_integrity.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(artifact)


def test_gate_authority_rejects_draft_manifest(tmp_path: Path) -> None:
    _, _, _, authority = _build_sequence(tmp_path)
    draft_manifest = replace(
        authority.run_manifest,
        authorization=replace(
            authority.run_manifest.authorization,
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
        ),
    )

    with pytest.raises(R25PostRunIntegrityError) as raised:
        replace(
            authority,
            run_manifest=draft_manifest,
            authority_manifest_sha256=authority_manifest_sha256(draft_manifest),
        )
    assert raised.value.code == "INVALID_GATE_AUTHORITY"


def test_standalone_gate_cli_uses_owner_authorized_v2_loader(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, output, _, authority = _build_sequence(tmp_path)
    draft_manifest = replace(
        authority.run_manifest,
        authorization=replace(
            authority.run_manifest.authorization,
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
        ),
    )
    manifest_path = tmp_path / "draft-authority.json"
    _write_canonical(
        manifest_path,
        cast(JsonValue, authority_manifest_projection(draft_manifest)),
    )
    script = Path(__file__).resolve().parents[3] / "scripts/check_r2_5_post_run_integrity.py"
    spec = importlib.util.spec_from_file_location("r25_integrity_gate_cli_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    digest = authority_manifest_sha256(draft_manifest)

    result = module.main(
        [
            "--authority-manifest",
            str(manifest_path),
            "--sequence-output-root",
            str(output),
            "--confirm-sequence-deadline-monotonic-ns",
            str(time.monotonic_ns() + 1_000_000_000),
            "--confirm-sequence-started-monotonic-ns",
            str(time.monotonic_ns() - 1_000_000),
            "--confirm-manifest-sha256",
            digest,
            "--confirm-preflight-report-sha256",
            authority.preflight_report_sha256,
            "--confirm-runtime-config-sha256",
            authority.runtime_config_sha256,
            "--confirm-pricing-sha256",
            authority.pricing_sha256,
            "--confirm-sentinel-config-sha256",
            authority.sentinel_config_sha256,
            "--confirm-factory-binding-sha256",
            authority.factory_binding_sha256,
            "--runtime-config",
            str(tmp_path / "unread-runtime.json"),
        ]
    )
    assert result == 2
    assert "OWNER_AUTHORITY_FILE_INVALID" in capsys.readouterr().err


def test_invalid_checker_report_is_preserved_but_acceptance_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    monkeypatch.setattr(
        integrity_gate,
        "_descriptor_bound_official_integrity_check",
        lambda *_args, **_kwargs: {
            "checked_at": "2026-09-05T00:02:00Z",
            "checker_version": CHECKER_VERSION,
            "counts": {},
            "errors": [{"code": "injected_invalid"}],
            "valid": False,
            "warnings": [],
        },
    )

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "COLLECTOR_INTEGRITY_REJECTED"
    assert (reports / "smoke-00-qwen3_vl-off.integrity.v1.json").is_file()
    assert not (reports / "post-run-integrity.v1.json").exists()


def test_nonempty_checker_warnings_fail_the_explicit_empty_warning_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    monkeypatch.setattr(
        integrity_gate,
        "_descriptor_bound_official_integrity_check",
        lambda *_args, **_kwargs: {
            "checked_at": "2026-09-05T00:02:00Z",
            "checker_version": CHECKER_VERSION,
            "counts": {},
            "errors": [],
            "valid": True,
            "warnings": [{"code": "injected_warning"}],
        },
    )

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "COLLECTOR_INTEGRITY_REJECTED"
    report = json.loads(
        (reports / "smoke-00-qwen3_vl-off.integrity.v1.json").read_text(encoding="utf-8")
    )
    assert report["warnings"] == [{"code": "injected_warning"}]
    assert not (reports / "post-run-integrity.v1.json").exists()


def test_gate_rejects_before_reports_when_terminal_or_cleanup_is_not_complete(
    tmp_path: Path,
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    terminal_path = output / "terminal.json"
    terminal = json.loads(terminal_path.read_bytes())
    terminal["status"] = "FAILED"
    _write_canonical(terminal_path, cast(JsonValue, terminal))

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "SEQUENCE_NOT_COMPLETE"
    assert not reports.exists()


def test_gate_rejects_self_consistent_authorized_stage_binding_tamper(
    tmp_path: Path,
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    tampered_stages = ["RESOURCE_PREFLIGHT", "QWEN_LIVE_SMOKE", "R25_PILOT"]
    binding_path = output / "manifest-binding.json"
    binding = json.loads(binding_path.read_bytes())
    binding["authorized_stages"] = tampered_stages
    _write_canonical(binding_path, cast(JsonValue, binding))

    stage_hashes: dict[str, str] = {}
    for stage, filename in integrity_gate._STAGE_FILES:
        path = output / filename
        document = json.loads(path.read_bytes())
        document["authorized_stages"] = tampered_stages
        raw = _write_canonical(path, cast(JsonValue, document))
        stage_hashes[stage] = hashlib.sha256(raw).hexdigest()
    cleanup_path = output / "04-resource-cleanup.json"
    cleanup = json.loads(cleanup_path.read_bytes())
    cleanup["authorized_stages"] = tampered_stages
    cleanup_raw = _write_canonical(cleanup_path, cast(JsonValue, cleanup))
    terminal_path = output / "terminal.json"
    terminal = json.loads(terminal_path.read_bytes())
    terminal["authorized_stages"] = tampered_stages
    terminal["stage_file_sha256s"] = stage_hashes
    terminal["cleanup_file_sha256"] = hashlib.sha256(cleanup_raw).hexdigest()
    _write_canonical(terminal_path, cast(JsonValue, terminal))

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "SEQUENCE_BINDING_MISMATCH"
    assert not reports.exists()


def test_gate_rejects_rechained_negative_stage_receipt_census(
    tmp_path: Path,
) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    qwen_path = output / "01-qwen-live-smoke.json"
    qwen = json.loads(qwen_path.read_bytes())
    prior = qwen["receipt"]["wall_time_ms"]
    qwen["receipt"]["wall_time_ms"] = -1
    qwen_raw = _write_canonical(qwen_path, cast(JsonValue, qwen))

    terminal_path = output / "terminal.json"
    terminal = json.loads(terminal_path.read_bytes())
    terminal["result"]["receipts"][1] = qwen["receipt"]
    terminal["result_sha256"] = integrity_gate._sha_json(cast(JsonValue, terminal["result"]))
    terminal["stage_file_sha256s"]["QWEN_LIVE_SMOKE"] = hashlib.sha256(qwen_raw).hexdigest()
    terminal["executor_census"]["stage_wall_time_ms"] = (
        terminal["executor_census"]["stage_wall_time_ms"] - prior - 1
    )
    terminal["executor_census_sha256"] = integrity_gate._sha_json(
        cast(JsonValue, terminal["executor_census"])
    )
    _write_canonical(terminal_path, cast(JsonValue, terminal))

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "STAGE_BINDING_MISMATCH"
    assert not reports.exists()


def test_gate_rejects_rechained_negative_terminal_census(tmp_path: Path) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    terminal_path = output / "terminal.json"
    terminal = json.loads(terminal_path.read_bytes())
    terminal["executor_census"]["actor_calls"] = -1
    terminal["executor_census_sha256"] = integrity_gate._sha_json(
        cast(JsonValue, terminal["executor_census"])
    )
    _write_canonical(terminal_path, cast(JsonValue, terminal))

    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=_sequence_start(),
            sequence_deadline_monotonic_ns=_deadline(),
        )
    assert raised.value.code == "INVALID_SEQUENCE_TERMINAL"
    assert not reports.exists()


def test_locator_requires_unique_raw_runs(tmp_path: Path) -> None:
    repository, output, _, authority = _build_sequence(tmp_path)
    del repository
    pilot = json.loads((output / "03-r25-pilot.json").read_bytes())["evidence"]
    first = pilot["cells"][0]["collector_run_locator"]
    pilot["cells"][1]["collector_run_locator"] = first
    pilot["cells"][1]["collector_run_locator_sha256"] = integrity_gate._sha_json(first)

    with pytest.raises(R25PostRunIntegrityError):
        integrity_gate.collector_run_locators_from_pilot_evidence_v1(
            pilot, authority=authority, repository_root=tmp_path / "repo"
        )


def test_strict_reopen_rejects_report_replacement(tmp_path: Path) -> None:
    repository, output, reports, authority = _build_sequence(tmp_path)
    _, _, artifact_path = run_post_run_integrity_gate_v1(
        sequence_output_root=output,
        repository_root=repository,
        authority=authority,
        sequence_started_monotonic_ns=_sequence_start(),
        sequence_deadline_monotonic_ns=_deadline(),
    )
    report = reports / "pilot-cell-000.integrity.v1.json"
    original = report.read_bytes()
    replacement = reports / "replacement"
    replacement.write_bytes(original)
    replacement.chmod(0o600)
    os.replace(replacement, report)

    with pytest.raises(R25PostRunIntegrityError):
        reopen_validate_post_run_integrity_artifact_v1(
            artifact_path,
            repository_root=repository,
            authority=authority,
            rerun_official_checker=False,
        )


def test_real_raw_fixture_passes_official_checker(tmp_path: Path) -> None:
    raw_parent = tmp_path / "raw"
    raw_parent.mkdir(mode=0o700)
    binding = _build_raw_cell(raw_parent, sequence_index=0, task_id="FixtureTask")
    report = check_run_integrity(cast(str, binding["collector_run_root"]))
    assert report["valid"] is True, report["errors"]
    assert report["warnings"] == []
    assert report["checker_version"] == CHECKER_VERSION


def test_scoreless_smoke_raw_fixture_is_aborted_task_in_complete_run(tmp_path: Path) -> None:
    raw_parent = tmp_path / "raw"
    raw_parent.mkdir(mode=0o700)
    binding = _build_raw_cell(raw_parent, sequence_index=0, task_id="SmokeTask", smoke=True)
    run_root = Path(cast(str, binding["collector_run_root"]))
    report = check_run_integrity(run_root)
    final = json.loads((run_root / "manifest.final.json").read_bytes())
    events = [
        json.loads(line)
        for line in (
            run_root / "tasks" / cast(str, binding["collector_task_run_id"]) / "events.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    ended = next(item["payload"] for item in events if item["event_type"] == "task_ended")
    assert report["valid"] is True, report["errors"]
    assert report["warnings"] == []
    assert final["runtime_status"] == "completed"
    assert final["task_streams"][0]["runtime_status"] == "aborted"
    assert ended["runtime_status"] == "aborted"
    assert ended["environment_evaluation"]["score"] is None
    assert not any(item["event_type"] == "action_execution_started" for item in events)


def test_smoke_v3_requires_durable_collector_binding(tmp_path: Path) -> None:
    _, output, _, authority = _build_sequence(tmp_path)
    qwen = json.loads((output / "01-qwen-live-smoke.json").read_bytes())["evidence"]
    journal = qwen["cases"][0]["unit_journal"]
    journal.pop("collector_run_binding")
    journal.pop("collector_run_binding_sha256")
    qwen["cases"][0]["unit_journal_sha256"] = integrity_gate._sha_json(journal)
    qwen["cases"][0]["unit_journal_byte_count"] = len(
        canonical_json_bytes(cast(JsonValue, journal))
    )

    with pytest.raises(R25PostRunIntegrityError) as raised:
        integrity_gate.validate_smoke_stage_durable_evidence_projection_v2(
            qwen, authority=authority, expected_stage="QWEN_LIVE_SMOKE"
        )
    assert raised.value.code == "INVALID_SMOKE_STAGE"


def test_official_checker_is_terminated_at_absolute_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, output, _, authority = _build_sequence(tmp_path)

    def slow_checker(*_args: object, **_kwargs: object) -> dict[str, JsonValue]:
        time.sleep(5)
        raise AssertionError("checker should have been terminated")

    monkeypatch.setattr(integrity_gate, "_descriptor_bound_official_integrity_check", slow_checker)
    existing_children = {process.pid for process in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(R25PostRunIntegrityError) as raised:
        run_post_run_integrity_gate_v1(
            sequence_output_root=output,
            repository_root=repository,
            authority=authority,
            sequence_started_monotonic_ns=time.monotonic_ns() - 1_000_000,
            sequence_deadline_monotonic_ns=time.monotonic_ns() + 2_000_000_000,
        )
    assert raised.value.code == "POST_RUN_INTEGRITY_WALL_TIME_EXCEEDED"
    assert time.monotonic() - started < 2.5
    assert {process.pid for process in multiprocessing.active_children()} <= existing_children


def test_gate_invokes_one_official_check_per_smoke_and_pilot_raw_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, output, _, authority = _build_sequence(tmp_path)
    calls: list[str] = []

    def accepted_once(
        _directory_fd: int,
        *,
        collector_run_id: str,
        deadline_ns: int,
        public_path: Path | None,
    ) -> dict[str, JsonValue]:
        del deadline_ns
        assert public_path is None
        calls.append(collector_run_id)
        return {
            "checked_at": "2026-09-05T00:02:00Z",
            "checker_version": CHECKER_VERSION,
            "counts": {},
            "errors": [],
            "valid": True,
            "warnings": [],
        }

    monkeypatch.setattr(integrity_gate, "_bounded_official_integrity_check", accepted_once)
    artifact, _, _ = run_post_run_integrity_gate_v1(
        sequence_output_root=output,
        repository_root=repository,
        authority=authority,
        sequence_started_monotonic_ns=_sequence_start(),
        sequence_deadline_monotonic_ns=_deadline(),
    )
    assert len(calls) == 86
    assert len(set(calls)) == 86
    assert artifact["smoke_collector_run_count"] == 6
    assert artifact["pilot_collector_run_count"] == 80


def test_schema_is_draft_2020_12_and_closed() -> None:
    schema_path = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff/schemas/r2_5/post_run_integrity.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == (POST_RUN_INTEGRITY_SCHEMA_VERSION)
