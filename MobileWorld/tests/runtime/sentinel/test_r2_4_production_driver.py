from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from mobile_world.runtime.sentinel.r2_4 import production_audit as production_audit_module
from mobile_world.runtime.sentinel.r2_4 import production_driver as production_driver_module
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    CaseExecutionLeaseBindingV1,
    LiveSmokeAdapterPortV1,
    PilotAdapterPortV1,
    ResourceLifecycleAdapterPortV1,
    StageAdapterContextV1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunStageV1,
    SmokeModeV1,
    SnapshotResourceV1,
    compute_snapshot_tree_digest,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
    PRODUCTION_DRIVER_REQUIRED_BINDINGS_V1,
    PRODUCTION_DRIVER_REQUIRED_HOOKS_V1,
    CpuProductionDriverFaultV1,
    CpuResourceLifecycleFaultV1,
    ProductionDispatchKindV1,
    ProductionDriverError,
    ProductionDriverHookV1,
    ProductionRuntimeConfigV1,
    build_cpu_test_production_driver_v1,
    build_cpu_test_resource_lifecycle_adapter_v1,
    build_production_driver_v1,
    pilot_stage_evidence_sha256,
    production_driver_available_v1,
    production_resource_stage_evidence_sha256,
    production_runtime_config_sha256,
    smoke_stage_evidence_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _semantic_decision(
    *,
    actor_call_index: int,
    rubric_calls: int,
    history_policy_calls: int,
) -> production_driver_module.ActorDecisionEvidenceV1:
    digest = _sha(f"decision-{actor_call_index}-{rubric_calls}-{history_policy_calls}")
    rubric_receipts = tuple(
        _sha(f"rubric-{actor_call_index}-{index}") for index in range(rubric_calls)
    )
    history_receipt = None if history_policy_calls == 0 else _sha(f"history-{actor_call_index}")
    return production_driver_module.ActorDecisionEvidenceV1(
        logical_call_id=f"semantic-call-{actor_call_index}",
        actor_call_index=actor_call_index,
        raw_request_sha256=digest,
        final_request_sha256=digest,
        provider_request_sha256=digest,
        provider_response_sha256="1" * 64,
        exact_diff_sha256="2" * 64,
        pre_provider_status=(
            production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.READY
            if history_policy_calls
            else production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
        ),
        pre_provider_outcome=(
            production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.READY
            if history_policy_calls
            else production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
        ),
        fallback_reason=(
            None
            if history_policy_calls
            else production_driver_module.SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
        ),
        fallback_check=(None if history_policy_calls else "r2_4_no_history_r21_v1_compatibility"),
        preflight_report_sha256="3" * 64,
        case_execution_lease_sha256="4" * 64,
        live_policy_factory_binding_sha256="5" * 64,
        live_policy_authority_sha256="6" * 64,
        rubric_attempt_receipt_sha256s=rubric_receipts,
        history_policy_attempt_receipt_sha256=history_receipt,
        actor_attempt_receipt_sha256="7" * 64,
        sentinel_receipt_sha256="8" * 64,
        provider_attempt_receipt_sha256="9" * 64,
        runtime_audit_detail_sha256="a" * 64,
        parser_result_sha256="b" * 64,
        parsed_action_sha256="c" * 64,
        executed_action_sha256=None,
        census=production_driver_module.DriverCallCensusV1(
            actor_calls=1,
            offline_rubric_evaluations=0,
            rubric_openai_calls=rubric_calls,
            history_policy_openai_calls=history_policy_calls,
            openai_calls=rubric_calls + history_policy_calls,
            actor_actions=0,
            cost_usd_micros=0,
            wall_time_ms=1,
        ),
    )


def _audit_commit_failure_recovery(
    *, action_executed: bool
) -> production_audit_module.ProductionRuntimeAuditCommitFailureReceiptV1:
    digest = "1" * 64
    request_locator: dict[str, Any] = {
        "run_id": "run-commit-fault",
        "task_run_id": "task-commit-fault",
        "event_type": "model_request",
        "event_id": "request-event-1",
        "event_sha256": "2" * 64,
        "snapshot_blob": {"sha256": "3" * 64},
    }
    terminal_locator: dict[str, Any] = {
        "run_id": "run-commit-fault",
        "task_run_id": "task-commit-fault",
        "event_type": "model_response",
        "event_id": "response-event-1",
        "event_sha256": "4" * 64,
        "snapshot_blob": {"sha256": "5" * 64},
    }
    attempt = production_audit_module.ProductionActorProviderAttemptV1(
        attempt_id="commit-fault-actor-attempt-1",
        attempt_index=1,
        sdk_arguments_sha256=digest,
        final_request_sha256=digest,
        collector_request_locator=cast(Any, request_locator),
        collector_terminal_locator=cast(Any, terminal_locator),
        status=production_audit_module.ProductionActorProviderAttemptStatusV1.SUCCEEDED,
        provider_response_sha256="6" * 64,
        response_id_sha256="7" * 64,
        model_id_sha256="8" * 64,
        finish_reason="stop",
        input_tokens=11,
        output_tokens=7,
        total_tokens=18,
        latency_ns=10,
        failure_code=None,
    )
    attempt_root = canonical_sha256(
        cast(
            Any,
            {
                "schema_version": "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1",
                "attempt_sha256s": [
                    canonical_sha256(
                        cast(
                            Any,
                            production_audit_module.production_actor_provider_attempt_projection(
                                attempt
                            ),
                        )
                    )
                ],
            },
        )
    )
    parsed_action: dict[str, Any] = {"action_type": "wait"}
    action_sha256 = canonical_sha256(cast(Any, parsed_action))
    terminal = production_audit_module.ProductionRuntimeAuditReceiptV1(
        detail_id="commit-fault-detail-1",
        logical_call_id="commit-fault-logical-call-1",
        raw_request_sha256=digest,
        final_request_sha256=digest,
        provider_request_sha256=digest,
        provider_response_sha256="6" * 64,
        exact_diff_sha256="9" * 64,
        pre_provider_sha256="a" * 64,
        pre_provider_status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.OFF,
        pre_provider_outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.OFF,
        fallback_reason=None,
        fallback_check=None,
        live_call_binding_sha256=None,
        live_attempt_receipt_root_sha256=None,
        actor_provider_attempt_root_sha256=attempt_root,
        sentinel_receipt_sha256="b" * 64,
        parser_input_sha256="c" * 64,
        parser_result_sha256="d" * 64,
        parsed_action_sha256=action_sha256,
        action_executed=action_executed,
        executed_action_sha256=action_sha256 if action_executed else None,
        provider_attempt_count=1,
        live_openai_calls=0,
        live_cost_usd_micros=0,
        live_cost_exact=True,
        total_ns=20,
        detail_sha256="e" * 64,
        _seal=production_audit_module._RECEIPT_SEAL,
    )
    return production_audit_module.ProductionRuntimeAuditCommitFailureReceiptV1(
        logical_call_id=terminal.logical_call_id,
        terminal_kind=production_audit_module.ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION,
        publication_status=(
            production_audit_module.ProductionRuntimeAuditPublicationStatusV1.COMMIT_OUTCOME_UNKNOWN
        ),
        failure_phase="AUDIT_TERMINAL_COMMIT",
        failure_code="AUDIT_TERMINAL_COMMIT_FAILED",
        attempted_terminal_receipt=terminal,
        attempted_terminal_receipt_sha256=(
            production_audit_module.production_runtime_audit_receipt_sha256(terminal)
        ),
        actor_provider_attempts=(attempt,),
        parsed_action=cast(Any, parsed_action),
        _seal=production_audit_module._COMMIT_FAILURE_RECEIPT_SEAL,
    )


def _journal_sha(domain: str, records: object) -> str:
    return canonical_sha256(
        cast(
            Any,
            {
                "domain": domain,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "value": records,
            },
        )
    )


def _assert_current_unit_hash(domain: str, current_unit: dict[str, object]) -> None:
    expected = current_unit["canonical_evidence_sha256"]
    preimage = dict(current_unit)
    del preimage["canonical_evidence_sha256"]
    assert expected == canonical_sha256(
        cast(
            Any,
            {
                "domain": domain,
                "schema_version": PRODUCTION_DRIVER_EVIDENCE_SCHEMA_VERSION,
                "value": preimage,
            },
        )
    )


def _pre_dispatch_cancelled_attempt(
    role: LiveAttemptRoleV1, *, logical_call_id: str, request_sha256: str
) -> LiveAttemptReceiptV1:
    return LiveAttemptReceiptV1(
        attempt_id=f"cancel-{role.value.lower()}",
        role=role,
        authority_sha256="1" * 64,
        manifest_sha256="a" * 64,
        preflight_sha256="2" * 64,
        case_execution_lease_sha256="3" * 64,
        stage_sha256="4" * 64,
        case_id="cancel-case",
        logical_call_id=logical_call_id,
        actor_request_sha256=request_sha256,
        request_sha256="5" * 64,
        transport_binding_sha256="6" * 64,
        pricing_binding_sha256="7" * 64,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH,
        dispatch_count=0,
        response_envelope_sha256=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=0,
        cancellation_requested=True,
        termination=LiveAttemptTerminationV1.COOPERATIVE,
        worker_pid=12345,
        worker_exit_code=0,
        worker_reaped=True,
        late_output_detected=False,
        duration_ns=1,
        failure_code=None,
    )


def _completed_semantic_attempt(
    role: LiveAttemptRoleV1,
    *,
    logical_call_id: str,
    request_sha256: str,
    attempt_index: int,
) -> LiveAttemptReceiptV1:
    return LiveAttemptReceiptV1(
        attempt_id=f"completed-{role.value.lower()}-{attempt_index}",
        role=role,
        authority_sha256="1" * 64,
        manifest_sha256="a" * 64,
        preflight_sha256="2" * 64,
        case_execution_lease_sha256="3" * 64,
        stage_sha256="4" * 64,
        case_id="generic-fallback-case",
        logical_call_id=logical_call_id,
        actor_request_sha256=request_sha256,
        request_sha256=hashlib.sha256(f"request-{attempt_index}".encode()).hexdigest(),
        transport_binding_sha256="6" * 64,
        pricing_binding_sha256="7" * 64,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.COMPLETED,
        dispatch_count=1,
        response_envelope_sha256=hashlib.sha256(f"response-{attempt_index}".encode()).hexdigest(),
        input_tokens=2,
        cached_input_tokens=0,
        output_tokens=1,
        total_tokens=3,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=1,
        cancellation_requested=False,
        termination=LiveAttemptTerminationV1.NONE,
        worker_pid=12_000 + attempt_index,
        worker_exit_code=0,
        worker_reaped=True,
        late_output_detected=False,
        duration_ns=attempt_index,
        failure_code=None,
    )


def _generic_fallback_terminal_receipt(
    *,
    logical_call_id: str,
    attempts: tuple[LiveAttemptReceiptV1, ...],
    fallback_reason: production_driver_module.SentinelFallbackReason,
    fallback_check: str,
    action_executed: bool,
) -> production_audit_module.ProductionRuntimeAuditReceiptV1:
    request_sha256 = attempts[0].actor_request_sha256
    action_sha256 = "8" * 64
    return production_audit_module.ProductionRuntimeAuditReceiptV1(
        detail_id=f"generic-fallback-{logical_call_id}",
        logical_call_id=logical_call_id,
        raw_request_sha256=request_sha256,
        final_request_sha256=request_sha256,
        provider_request_sha256=request_sha256,
        provider_response_sha256="9" * 64,
        exact_diff_sha256="a" * 64,
        pre_provider_sha256="b" * 64,
        pre_provider_status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
        pre_provider_outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
        fallback_reason=fallback_reason,
        fallback_check=fallback_check,
        live_call_binding_sha256=None,
        live_attempt_receipt_root_sha256="c" * 64,
        actor_provider_attempt_root_sha256="d" * 64,
        sentinel_receipt_sha256="e" * 64,
        parser_input_sha256="f" * 64,
        parser_result_sha256="0" * 64,
        parsed_action_sha256=action_sha256,
        action_executed=action_executed,
        executed_action_sha256=action_sha256 if action_executed else None,
        provider_attempt_count=1,
        live_openai_calls=sum(item.dispatch_count for item in attempts),
        live_cost_usd_micros=sum(cast(int, item.cost_usd_micros) for item in attempts),
        live_cost_exact=True,
        total_ns=1,
        detail_sha256="1" * 64,
        _seal=production_audit_module._RECEIPT_SEAL,
    )


class _Lease:
    def __init__(self, manifest_sha256: str) -> None:
        self._manifest_sha256 = manifest_sha256

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def environment_key(self) -> str:
        return "OPENAI_API_KEY"

    def issue_case_execution_lease(
        self,
        *,
        stage: RunStageV1,
        host: PilotHostV1,
        mode: SmokeModeV1,
        case_id: str,
        task_id: str,
        task_parameters_sha256: str | None,
        reset_seed: int | None,
        actor_call_index: int,
        request_sha256: str,
    ) -> CaseExecutionLeaseBindingV1:
        subject = (
            f"{stage.value}:{host.value}:{mode.value}:{case_id}:{task_id}:"
            f"{task_parameters_sha256}:{reset_seed}:{actor_call_index}:{request_sha256}"
        )
        return CaseExecutionLeaseBindingV1(
            manifest_sha256=self._manifest_sha256,
            preflight_report_sha256=_sha(f"preflight:{self._manifest_sha256}"),
            factory_binding_sha256=_sha(f"factory:{self._manifest_sha256}"),
            pricing_binding_sha256=_sha(f"pricing:{self._manifest_sha256}"),
            case_execution_lease_sha256=_sha(f"lease:{subject}"),
            execution_scope="CPU_TEST_LOCAL",
            openai_stage_set_sha256=_sha(f"openai-stage-set:{self._manifest_sha256}"),
            stage=stage,
            host=host,
            mode=mode,
            case_id=case_id,
            task_id=task_id,
            task_parameters_sha256=task_parameters_sha256,
            reset_seed=reset_seed,
            actor_call_index=actor_call_index,
            request_sha256=request_sha256,
            issued_at_utc="2026-09-03T00:00:00Z",
            expires_at_utc="2026-09-03T00:01:00Z",
        )

    def acquire_secret_lease(self, case_lease: CaseExecutionLeaseBindingV1) -> _SecretLease:
        assert case_lease.manifest_sha256 == self._manifest_sha256
        return _SecretLease(self._manifest_sha256)

    def close(self) -> None:
        return None


class _SecretLease:
    def __init__(self, manifest_sha256: str) -> None:
        self._manifest_sha256 = manifest_sha256

    @property
    def manifest_sha256(self) -> str:
        return self._manifest_sha256

    @property
    def environment_key(self) -> str:
        return "OPENAI_API_KEY"

    def close(self) -> None:
        return None


def _context(
    *,
    actor_calls: int = 1_000,
    openai_calls: int = 1_000,
    cost_usd_micros: int = 10_000_000,
    wall_time_ms: int = 20_000_000,
) -> StageAdapterContextV1:
    return StageAdapterContextV1(
        manifest_sha256="a" * 64,
        run_id="cpu-production-driver",
        source_commit="b" * 40,
        remaining_actor_calls=actor_calls,
        remaining_openai_calls=openai_calls,
        remaining_cost_usd_micros=cost_usd_micros,
        remaining_wall_time_ms=wall_time_ms,
        authority_deadline_monotonic_ns=time.monotonic_ns() + wall_time_ms * 1_000_000,
    )


def _policy_stage() -> OpenAIResponsesStageV1:
    return OpenAIResponsesStageV1(
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
    )


def _rubric_stage() -> OpenAIResponsesStageV1:
    return replace(
        _policy_stage(),
        role=OpenAIRoleV1.RUBRIC,
        max_output_tokens=8192,
    )


def _live_stages() -> tuple[OpenAIResponsesStageV1, OpenAIResponsesStageV1]:
    return (_rubric_stage(), _policy_stage())


def _resource(tmp_path: Path, host: PilotHostV1, port: int) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    snapshot_root = tmp_path / "snapshots"
    snapshot_path = snapshot_root / host.value
    snapshot_path.mkdir(parents=True, exist_ok=True)
    (snapshot_path / "weights.bin").write_bytes(f"snapshot:{host.value}".encode())
    resource = SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=str(snapshot_path),
        snapshot_storage_root=str(snapshot_root),
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=_sha(f"snapshot:{host.value}"),
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"cpu-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )
    digest = compute_snapshot_tree_digest(resource)
    return replace(
        resource,
        snapshot_tree_sha256=digest.sha256,
        snapshot_total_bytes=digest.total_bytes,
        snapshot_file_count=digest.file_count,
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
                    tmp_path / "fixtures" / f"{host.value.lower()}-{mode.value.lower()}.json"
                ),
                request_fixture_sha256=_sha(f"fixture:{host.value}:{mode.value}"),
                request_fixture_byte_count=100,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=30,
                max_cost_usd_micros=100,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
            for mode in SmokeModeV1
        ),
    )


def _pilot(tmp_path: Path, *, cohort_size: int = 20) -> FrozenPilotManifestV1:
    tasks = tuple(
        PilotTaskV1(
            task_id=f"task-{index:02d}",
            task_parameters_sha256=_sha(f"parameters:{index}"),
            reset_seed=20_000 + index,
        )
        for index in range(cohort_size)
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="cpu-driver-pilot",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path=str(tmp_path / "fixtures" / "pilot-tasks.json"),
        task_manifest_sha256=_sha("pilot-task-manifest"),
        task_manifest_byte_count=100,
        topology_comparison_artifact_path=str(tmp_path / "inputs" / "topology.json"),
        topology_comparison_artifact_sha256="0" * 64,
        topology_comparison_artifact_byte_count=1,
        cohort_selection_artifact_path=str(tmp_path / "inputs" / "cohort-selection.json"),
        cohort_selection_artifact_sha256="1" * 64,
        cohort_selection_artifact_byte_count=1,
        cohort_selection_sha256="1" * 64,
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
        max_steps_per_cell=3,
        per_cell_timeout_seconds=60,
        max_total_wall_time_seconds=10_000,
        max_total_actor_calls=cohort_size * 4,
        max_total_openai_calls=cohort_size * 6,
        max_total_cost_usd_micros=1_000_000,
    )


def _resources(tmp_path: Path) -> tuple[SnapshotResourceV1, SnapshotResourceV1]:
    return (
        _resource(tmp_path, PilotHostV1.QWEN3_VL, 18081),
        _resource(tmp_path, PilotHostV1.MAI_UI, 18082),
    )


def _runtime_config(tmp_path: Path) -> ProductionRuntimeConfigV1:
    return ProductionRuntimeConfigV1(
        backend_port=18080,
        backend_device="emulator-5554",
        qwen_gpu_index=0,
        mai_gpu_index=1,
        process_log_root=str(tmp_path / "process-logs"),
        authorized_pilot_input_root=str(tmp_path / "pilot-inputs"),
        repository_root=str(tmp_path / "repo"),
        mobileworld_source_root=str(tmp_path / "repo" / "MobileWorld" / "src"),
        vllm_python_executable="/opt/evofsm/skyrl-agent/.venv/bin/python",
        vllm_python_realpath="/shared/miniconda3/bin/python3.12",
        vllm_python_sha256="c" * 64,
        vllm_python_byte_count=12_345,
        vllm_version="0.11.0",
        backend_image_id_sha256="d" * 64,
        backend_environment_file=str(tmp_path / "mobileworld.env"),
        backend_environment_file_device=17,
        backend_environment_file_inode=23,
        backend_environment_file_mode=0o600,
        backend_environment_file_uid=1000,
        backend_environment_file_byte_count=123,
        backend_environment_file_mtime_ns=1_234_567_890,
        startup_timeout_seconds=60,
        shutdown_grace_seconds=5,
        health_poll_interval_ms=25,
    )


def test_resource_lifecycle_uses_only_fixed_argv_loopback_health_and_owned_cleanup(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)

    assert isinstance(adapter, ResourceLifecycleAdapterPortV1)
    assert production_runtime_config_sha256(config)
    result = adapter.prepare(_resources(tmp_path), _context())
    evidence = adapter.evidence
    assert evidence is not None
    assert result.evidence_sha256 == production_resource_stage_evidence_sha256(evidence)
    assert (result.actor_calls, result.openai_calls, result.actor_actions) == (0, 0, 0)

    commands = adapter.cpu_trace.commands
    assert len(commands) == 13
    (
        git_head,
        git_status,
        git_tree,
        vllm_attest,
        network,
        image,
        docker,
        qwen_gpu_identity,
        qwen_gpu_processes,
        qwen,
        mai_gpu_identity,
        mai_gpu_processes,
        mai,
    ) = commands
    assert git_head[-2:] == ("rev-parse", "HEAD")
    assert git_status[-3:] == ("status", "--porcelain=v1", "--untracked-files=all")
    assert git_tree[-2:] == ("rev-parse", f"{'b' * 40}:MobileWorld/src")
    assert vllm_attest == (
        config.vllm_python_executable,
        "-P",
        "-B",
        "-c",
        "import importlib.metadata as m; print(m.version('vllm'))",
    )
    assert network == (
        "/usr/bin/docker",
        "network",
        "inspect",
        "--format",
        "{{.Name}}",
        "mwnet",
    )
    assert image == (
        "/usr/bin/docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        "mobile_world:reset",
    )
    assert docker == (
        "/usr/bin/docker",
        "run",
        "--detach",
        "--rm",
        "--privileged",
        "--name",
        f"r24-{'a' * 20}",
        "--network",
        "mwnet",
        "--env-file",
        config.backend_environment_file,
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--volume",
        f"{config.mobileworld_source_root}:/app/service/src:ro",
        "--publish",
        "127.0.0.1:18080:6800",
        "mobile_world:reset",
    )
    assert qwen_gpu_identity == (
        "/usr/bin/nvidia-smi",
        "--id=0",
        "--query-gpu=index,uuid",
        "--format=csv,noheader,nounits",
    )
    assert qwen_gpu_processes == (
        "/usr/bin/nvidia-smi",
        "--id=0",
        "--query-compute-apps=gpu_uuid,pid",
        "--format=csv,noheader,nounits",
    )
    assert mai_gpu_identity == (
        "/usr/bin/nvidia-smi",
        "--id=1",
        "--query-gpu=index,uuid",
        "--format=csv,noheader,nounits",
    )
    assert mai_gpu_processes == (
        "/usr/bin/nvidia-smi",
        "--id=1",
        "--query-compute-apps=gpu_uuid,pid",
        "--format=csv,noheader,nounits",
    )
    for command, expected_port, expected_snapshot in (
        (qwen, "18081", str(tmp_path / "snapshots" / PilotHostV1.QWEN3_VL.value)),
        (mai, "18082", str(tmp_path / "snapshots" / PilotHostV1.MAI_UI.value)),
    ):
        assert command[:7] == (
            config.vllm_python_executable,
            "-P",
            "-B",
            "-m",
            "vllm.entrypoints.cli.main",
            "serve",
            expected_snapshot,
        )
        assert command[command.index("--host") + 1] == "127.0.0.1"
        assert command[command.index("--port") + 1] == expected_port
        assert "/bin/bash" not in command
        assert "-c" not in command

    assert adapter.cpu_trace.health_endpoints == (
        "http://127.0.0.1:18080/health",
        "http://127.0.0.1:18081/health",
        "http://127.0.0.1:18081/v1/models",
        "http://127.0.0.1:18082/health",
        "http://127.0.0.1:18082/v1/models",
    )
    adapter.cleanup(_context())
    assert adapter.cpu_trace.cleanup_targets == (
        "pid:10001",
        "pid:10000",
        f"container:{evidence.backend_container_id}",
    )


def test_expired_monotonic_authority_blocks_before_any_resource_operation(
    tmp_path: Path,
) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_runtime_config(tmp_path))
    context = replace(_context(), authority_deadline_monotonic_ns=1)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.prepare(_resources(tmp_path), context)
    assert raised.value.code == "OWNER_AUTHORITY_EXPIRED"
    assert adapter.cpu_trace.commands == ()
    assert adapter.cpu_trace.health_endpoints == ()


def test_cpu_gpu_lease_is_exclusive_and_released_without_files(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    first = build_cpu_test_resource_lifecycle_adapter_v1(config)
    second = build_cpu_test_resource_lifecycle_adapter_v1(config)
    first.prepare(_resources(tmp_path), _context())

    with pytest.raises(ProductionDriverError) as raised:
        second.prepare(_resources(tmp_path), _context())
    assert raised.value.code == "GPU_LEASE_CONFLICT"
    assert second.cpu_trace.commands == ()

    first.cleanup(_context())
    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), _context())
    replacement.cleanup(_context())


def test_gpu_idle_attestation_binds_index_uuid_and_rejects_foreign_compute_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(_runtime_config(tmp_path), seal=production_driver_module._MODULE_SEAL)
    uuid = "GPU-12345678-1234-1234-1234-123456789abc"
    responses = iter(
        (
            SimpleNamespace(returncode=0, stderr="", stdout=f"0, {uuid}\n"),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
        )
    )
    monkeypatch.setattr(
        system_type,
        "_attestation_run",
        staticmethod(lambda _argv, **_kwargs: next(responses)),
    )
    assert system.attest_gpu_idle(0)

    occupied_responses = iter(
        (
            SimpleNamespace(returncode=0, stderr="", stdout=f"0, {uuid}\n"),
            SimpleNamespace(returncode=0, stderr="", stdout=f"{uuid}, 4242\n"),
        )
    )
    monkeypatch.setattr(
        system_type,
        "_attestation_run",
        staticmethod(lambda _argv, **_kwargs: next(occupied_responses)),
    )
    with pytest.raises(ProductionDriverError) as raised:
        system.attest_gpu_idle(0)
    assert raised.value.code == "GPU_ALREADY_OCCUPIED"


def test_gpu_idle_attestation_rejects_index_uuid_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(_runtime_config(tmp_path), seal=production_driver_module._MODULE_SEAL)
    monkeypatch.setattr(
        system_type,
        "_attestation_run",
        staticmethod(
            lambda _argv, **_kwargs: SimpleNamespace(
                returncode=0,
                stderr="",
                stdout="1, GPU-12345678-1234-1234-1234-123456789abc\n",
            )
        ),
    )
    with pytest.raises(ProductionDriverError) as raised:
        system.attest_gpu_idle(0)
    assert raised.value.code == "GPU_IDENTITY_MISMATCH"


def test_backend_nonzero_launch_with_exact_absence_clears_pending_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(config, seal=production_driver_module._MODULE_SEAL)
    spec = production_driver_module._backend_command_spec(config, manifest_sha256="a" * 64)
    name = spec.argv[spec.argv.index("--name") + 1]
    absent = SimpleNamespace(
        returncode=1,
        stdout="[]\n",
        stderr=f"Error: No such object: {name}\n",
    )
    responses = iter(
        (
            absent,
            SimpleNamespace(returncode=125, stdout="", stderr="launch rejected\n"),
            absent,
        )
    )
    monkeypatch.setattr(
        production_driver_module, "_attest_backend_environment_file", lambda _config: "f" * 64
    )
    monkeypatch.setattr(production_driver_module, "_assert_loopback_port_free", lambda _port: None)
    monkeypatch.setattr(
        system_type,
        "_docker_run",
        staticmethod(lambda _argv, **_kwargs: next(responses)),
    )
    with pytest.raises(ProductionDriverError) as raised:
        system.start_backend(spec)
    assert raised.value.code == "BACKEND_START_FAILED"
    assert system.residual_capabilities()["pending_backend_names"] == []


def test_admitted_backend_already_gone_clears_owned_capability_without_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _runtime_config(tmp_path)
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(config, seal=production_driver_module._MODULE_SEAL)
    spec = production_driver_module._backend_command_spec(config, manifest_sha256="a" * 64)
    name = spec.argv[spec.argv.index("--name") + 1]
    container_id = "1" * 64
    owned = production_driver_module._OwnedBackendContainerV1(
        container_id=container_id,
        name=name,
        spec=spec,
    )
    system._backend_candidates[container_id] = owned

    def absent(target: str) -> object:
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=f"Error: No such object: {target}\n",
        )

    responses = iter((absent(container_id), absent(container_id), absent(name)))
    monkeypatch.setattr(
        system_type,
        "_docker_run",
        staticmethod(lambda _argv, **_kwargs: next(responses)),
    )
    system.stop_backend(owned)
    residual = system.residual_capabilities()
    assert residual["backend_candidates"] == []
    assert residual["pending_backend_ids"] == []
    assert residual["pending_backend_names"] == []


def test_per_host_disable_and_kill_do_not_disable_peer(tmp_path: Path) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_runtime_config(tmp_path))
    adapter.prepare(_resources(tmp_path), _context())
    deadline = time.monotonic_ns() + 1_000_000_000

    adapter.disable_host(PilotHostV1.QWEN3_VL)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.require_dispatch(
            PilotHostV1.QWEN3_VL,
            ProductionDispatchKindV1.ACTOR,
            authority_deadline_monotonic_ns=deadline,
        )
    assert raised.value.code == "HOST_DISABLED"
    adapter.require_dispatch(
        PilotHostV1.MAI_UI,
        ProductionDispatchKindV1.ACTOR,
        authority_deadline_monotonic_ns=deadline,
    )
    killed = adapter.kill_host(PilotHostV1.QWEN3_VL)
    assert len(killed) == 64
    adapter.require_dispatch(
        PilotHostV1.MAI_UI,
        ProductionDispatchKindV1.ACTION,
        authority_deadline_monotonic_ns=deadline,
    )
    assert adapter.cpu_trace.cleanup_targets == ("pid:10000",)
    adapter.cleanup(_context())
    evidence = adapter.evidence
    assert evidence is not None
    assert adapter.cpu_trace.cleanup_targets[-2:] == (
        "pid:10001",
        f"container:{evidence.backend_container_id}",
    )


@pytest.mark.parametrize(
    "fault,expected_code,cleanup_prefix",
    (
        (
            CpuResourceLifecycleFaultV1.BACKEND_START_TIMEOUT,
            "BACKEND_PARTIAL_START_RECOVERABLE",
            "container:",
        ),
        (
            CpuResourceLifecycleFaultV1.BACKEND_OWNERSHIP_MISMATCH,
            "BACKEND_OWNERSHIP_MISMATCH_RECOVERABLE",
            "container:",
        ),
        (
            CpuResourceLifecycleFaultV1.MODEL_PARTIAL_START,
            "MODEL_PARTIAL_START_RECOVERABLE",
            "partial-pid:",
        ),
    ),
)
def test_partial_start_failures_are_typed_reclaimed_and_release_gpu_leases(
    tmp_path: Path,
    fault: CpuResourceLifecycleFaultV1,
    expected_code: str,
    cleanup_prefix: str,
) -> None:
    config = _runtime_config(tmp_path)
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config, fault)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.prepare(_resources(tmp_path), _context())
    assert raised.value.code == expected_code
    failure_raw = adapter.failure_evidence_preimage(RunStageV1.RESOURCE_PREFLIGHT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["failure_code"] == expected_code
    assert failure["status"] == "FAILED"
    assert adapter.cpu_trace.pending_cleanup_count == 0
    assert any(item.startswith(cleanup_prefix) for item in adapter.cpu_trace.cleanup_targets)

    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), _context())
    replacement.cleanup(_context())


def test_partial_start_cleanup_failure_persists_residual_capability_for_retry(
    tmp_path: Path,
) -> None:
    config = _runtime_config(tmp_path)
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(
        config, CpuResourceLifecycleFaultV1.MODEL_PARTIAL_CLEANUP_ONCE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapter.prepare(_resources(tmp_path), context)
    assert raised.value.code == "RESOURCE_CLEANUP_FAILED"
    failure_raw = adapter.failure_evidence_preimage(RunStageV1.RESOURCE_PREFLIGHT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["status"] == "FAILED_CLEANUP_RETRY_REQUIRED"
    assert failure["cleanup_status"] == "RETRY_REQUIRED"
    assert failure["cleanup_failure_code"] == "RESOURCE_CLEANUP_FAILED"
    residual = failure["residual_capabilities"]
    assert residual["partial_model_processes"] == [{"capability": "partial-pid:10000"}]
    assert failure["residual_capabilities_sha256"] == _journal_sha(
        "production-resource-residual-capabilities", residual
    )
    assert adapter.cpu_trace.pending_cleanup_count == 1

    adapter.cleanup(context)
    assert adapter.cpu_trace.pending_cleanup_count == 0
    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), context)
    replacement.cleanup(context)


def test_failed_cleanup_retains_ownership_and_is_retryable(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path)
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(
        config, CpuResourceLifecycleFaultV1.MODEL_STOP_ONCE
    )
    adapter.prepare(_resources(tmp_path), _context())
    with pytest.raises(ProductionDriverError) as raised:
        adapter.cleanup(_context())
    assert raised.value.code == "RESOURCE_CLEANUP_FAILED"
    failure_raw = adapter.failure_evidence_preimage(RunStageV1.RESOURCE_PREFLIGHT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["status"] == "FAILED_CLEANUP_RETRY_REQUIRED"
    assert failure["cleanup_status"] == "RETRY_REQUIRED"
    assert failure["failure_code"] == "RESOURCE_CLEANUP_FAILED"
    assert failure["cleanup_failure_code"] == "RESOURCE_CLEANUP_FAILED"
    assert len(failure["completed_model_processes"]) == 1
    assert failure["gpu_lease_sha256s"]

    contender = build_cpu_test_resource_lifecycle_adapter_v1(config)
    with pytest.raises(ProductionDriverError) as conflict:
        contender.prepare(_resources(tmp_path), _context())
    assert conflict.value.code == "GPU_LEASE_CONFLICT"

    adapter.cleanup(_context())
    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), _context())
    replacement.cleanup(_context())


@pytest.mark.parametrize(
    "fault,expected_code",
    (
        (
            CpuResourceLifecycleFaultV1.DISPATCH_MODEL_IDENTITY,
            "MODEL_DISPATCH_IDENTITY_LOST",
        ),
        (
            CpuResourceLifecycleFaultV1.DISPATCH_BACKEND_IDENTITY,
            "BACKEND_DISPATCH_IDENTITY_LOST",
        ),
    ),
)
def test_every_dispatch_gate_rejects_live_identity_drift(
    tmp_path: Path,
    fault: CpuResourceLifecycleFaultV1,
    expected_code: str,
) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_runtime_config(tmp_path), fault)
    adapter.prepare(_resources(tmp_path), _context())
    with pytest.raises(ProductionDriverError) as raised:
        adapter.require_dispatch(
            PilotHostV1.QWEN3_VL,
            ProductionDispatchKindV1.SCORE,
            authority_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        )
    assert raised.value.code == expected_code
    adapter.cleanup(_context())


@pytest.mark.parametrize(
    "role,expected_rubric_receipts,expected_history_receipt",
    (
        (LiveAttemptRoleV1.RUBRIC, 1, False),
        (LiveAttemptRoleV1.HISTORY_POLICY, 0, True),
    ),
)
def test_pre_dispatch_terminal_receipt_is_not_counted_as_a_provider_dispatch(
    role: LiveAttemptRoleV1,
    expected_rubric_receipts: int,
    expected_history_receipt: bool,
) -> None:
    logical_call_id = "cancelled-logical-call"
    request_sha256 = "8" * 64
    attempt = _pre_dispatch_cancelled_attempt(
        role, logical_call_id=logical_call_id, request_sha256=request_sha256
    )

    class _Policy:
        execution_authority_sha256 = "9" * 64

        @staticmethod
        def attempt_receipts_for_call(_: str) -> tuple[LiveAttemptReceiptV1, ...]:
            return (attempt,)

        @staticmethod
        def call_binding(_: str) -> object:
            raise RuntimeError("no completed binding")

    port_type = production_driver_module._ProductionFixedExecutionPortV1
    port = object.__new__(port_type)
    object.__setattr__(
        port,
        "_factory",
        SimpleNamespace(preflight_report_sha256="a" * 64, factory_binding_sha256="b" * 64),
    )
    receipt = SimpleNamespace(
        live_cost_exact=True,
        logical_call_id=logical_call_id,
        live_openai_calls=0,
        live_cost_usd_micros=0,
        raw_request_sha256=request_sha256,
        final_request_sha256=request_sha256,
        provider_request_sha256=request_sha256,
        provider_response_sha256="c" * 64,
        exact_diff_sha256="d" * 64,
        pre_provider_status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.READY,
        pre_provider_outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.READY,
        fallback_reason=None,
        fallback_check=None,
        actor_provider_attempt_root_sha256="e" * 64,
        sentinel_receipt_sha256="f" * 64,
        detail_sha256="0" * 64,
        parser_result_sha256="1" * 64,
        parsed_action_sha256="2" * 64,
        executed_action_sha256=None,
        action_executed=False,
        total_ns=1,
    )
    decision = port._decision_from_receipt(
        SimpleNamespace(policy=_Policy()), receipt, actor_call_index=1
    )
    assert decision.census.openai_calls == 0
    assert decision.census.rubric_openai_calls == 0
    assert decision.census.history_policy_openai_calls == 0
    assert len(decision.rubric_attempt_receipt_sha256s) == expected_rubric_receipts
    assert (decision.history_policy_attempt_receipt_sha256 is not None) is expected_history_receipt
    assert decision.case_execution_lease_sha256 == attempt.case_execution_lease_sha256
    assert decision.live_policy_authority_sha256 == _Policy.execution_authority_sha256


def test_post_dispatch_unknown_cost_cannot_enter_successful_decision() -> None:
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    with pytest.raises(ProductionDriverError) as raised:
        port._decision_from_receipt(
            SimpleNamespace(policy=None),
            SimpleNamespace(live_cost_exact=False),
            actor_call_index=1,
        )
    assert raised.value.code == "LIVE_COST_ACCOUNTING_UNKNOWN"


@pytest.mark.parametrize(
    ("roles", "fallback_reason", "fallback_check", "action_executed"),
    (
        pytest.param(
            (LiveAttemptRoleV1.RUBRIC, LiveAttemptRoleV1.RUBRIC),
            production_driver_module.SentinelFallbackReason.SIDECAR_FAILURE,
            "sidecar_commit_failed",
            False,
            id="sidecar-commit-after-two-rubric-calls",
        ),
        pytest.param(
            (
                LiveAttemptRoleV1.RUBRIC,
                LiveAttemptRoleV1.RUBRIC,
                LiveAttemptRoleV1.HISTORY_POLICY,
            ),
            production_driver_module.SentinelFallbackReason.RENDERER_FAILURE,
            "renderer_failed",
            True,
            id="active-post-three-call-fallback",
        ),
    ),
)
def test_generic_semantic_fallback_is_journaled_then_typed_stage_fails(
    roles: tuple[LiveAttemptRoleV1, ...],
    fallback_reason: production_driver_module.SentinelFallbackReason,
    fallback_check: str,
    action_executed: bool,
) -> None:
    logical_call_id = f"generic-fallback-{len(roles)}"
    request_sha256 = "7" * 64
    attempts = tuple(
        _completed_semantic_attempt(
            role,
            logical_call_id=logical_call_id,
            request_sha256=request_sha256,
            attempt_index=index,
        )
        for index, role in enumerate(roles, start=1)
    )

    class _Policy:
        execution_authority_sha256 = "1" * 64

        @staticmethod
        def attempt_receipts_for_call(_: str) -> tuple[LiveAttemptReceiptV1, ...]:
            return attempts

        @staticmethod
        def call_binding(_: str) -> object:
            raise RuntimeError("generic fallback has no admitted successful binding")

    receipt = _generic_fallback_terminal_receipt(
        logical_call_id=logical_call_id,
        attempts=attempts,
        fallback_reason=fallback_reason,
        fallback_check=fallback_check,
        action_executed=action_executed,
    )
    state = production_driver_module._ProductionUnitStateV1(
        unit_id=f"fallback:{len(roles)}",
        host=PilotHostV1.QWEN3_VL,
        task_name="generic-fallback-task",
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        environment=None,
        observation=None,
        policy=cast(Any, _Policy()),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(
        port,
        "_factory",
        SimpleNamespace(preflight_report_sha256="2" * 64, factory_binding_sha256="3" * 64),
    )
    port._journal_completed_audit_terminal(state, receipt)

    with pytest.raises(ProductionDriverError) as raised:
        port._decision_from_receipt(state, receipt, actor_call_index=1)
    assert raised.value.code == "SENTINEL_PRE_PROVIDER_OUTCOME_REJECTED"
    assert len(state.decision_journal) == 1
    decision = state.decision_journal[0]
    assert (
        decision.pre_provider_outcome
        is production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL
    )
    assert decision.fallback_reason is fallback_reason
    assert decision.fallback_check == fallback_check
    assert decision.census.openai_calls == len(roles)
    assert decision.census.cost_usd_micros == len(roles)
    assert decision.census.actor_actions == int(action_executed)
    journal = json.loads(port._unit_journal_snapshot(state))
    assert len(journal["completed_decisions"]) == 1
    assert len(journal["terminal_audit_records"]) == 1
    projected = journal["completed_decisions"][0]
    assert projected["pre_provider_outcome"] == "GENERIC_FALLBACK_ORIGINAL"
    assert projected["fallback_reason"] == fallback_reason.value
    assert projected["fallback_check"] == fallback_check


def test_production_port_exports_append_only_terminal_and_decision_journals(
    tmp_path: Path,
) -> None:
    digest = "1" * 64

    def receipt(
        logical_call_id: str, suffix: str
    ) -> production_audit_module.ProductionRuntimeAuditReceiptV1:
        return production_audit_module.ProductionRuntimeAuditReceiptV1(
            detail_id=f"journal-detail-{suffix}",
            logical_call_id=logical_call_id,
            raw_request_sha256=digest,
            final_request_sha256=digest,
            provider_request_sha256=digest,
            provider_response_sha256="2" * 64,
            exact_diff_sha256="3" * 64,
            pre_provider_sha256="4" * 64,
            pre_provider_status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.OFF,
            pre_provider_outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.OFF,
            fallback_reason=None,
            fallback_check=None,
            live_call_binding_sha256=None,
            live_attempt_receipt_root_sha256=None,
            actor_provider_attempt_root_sha256="5" * 64,
            sentinel_receipt_sha256="6" * 64,
            parser_input_sha256="7" * 64,
            parser_result_sha256="8" * 64,
            parsed_action_sha256="9" * 64,
            action_executed=False,
            executed_action_sha256=None,
            provider_attempt_count=1,
            live_openai_calls=0,
            live_cost_usd_micros=0,
            live_cost_exact=True,
            total_ns=1,
            detail_sha256=("a" if suffix == "one" else "b") * 64,
            _seal=production_audit_module._RECEIPT_SEAL,
        )

    first = receipt("journal-logical-one", "one")
    second = receipt("journal-logical-two", "two")
    state = production_driver_module._ProductionUnitStateV1(
        unit_id="smoke:QWEN3_VL:OFF",
        host=PilotHostV1.QWEN3_VL,
        task_name="task-smoke-qwen3_vl",
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        environment=None,
        observation=None,
        policy=None,
        runtime_audit=cast(
            Any,
            SimpleNamespace(
                latest_completed_receipt=second,
                latest_failure_receipt=None,
                latest_commit_failure_receipt=None,
            ),
        ),
    )
    port_type = production_driver_module._ProductionFixedExecutionPortV1
    port = object.__new__(port_type)
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_units", {state.unit_id: state})
    object.__setattr__(port, "_unit_journals", {})
    object.__setattr__(
        port,
        "_factory",
        SimpleNamespace(preflight_report_sha256="c" * 64, factory_binding_sha256="d" * 64),
    )
    for index, terminal in enumerate((first, second), start=1):
        port._journal_completed_audit_terminal(state, terminal)
        port._decision_from_receipt(state, terminal, actor_call_index=index)
    port._journal_completed_audit_terminal(state, second)

    plan = _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL)
    invocation = production_driver_module._SmokeInvocationV1(
        manifest_sha256="a" * 64,
        run_id="run-1",
        source_commit="b" * 40,
        host=PilotHostV1.QWEN3_VL,
        sequence_index=0,
        case=plan.cases[0],
        actor_resource_sha256="e" * 64,
        history_policy_stage_sha256="f" * 64,
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
    )
    port._unit_journals[state.unit_id] = port._unit_journal_snapshot(state)
    port._units.clear()
    evidence = port.failure_evidence_for_unit(
        invocation,
        failure_phase="POST_DISPATCH_ADMISSION",
        failure_code="OFFICIAL_RESULT_REJECTED",
    )
    assert isinstance(evidence, dict)
    assert evidence["failure_phase"] == "POST_DISPATCH_ADMISSION"
    assert evidence["failure_code"] == "OFFICIAL_RESULT_REJECTED"
    assert len(cast(list[object], evidence["completed_decisions"])) == 2
    terminals = cast(list[dict[str, object]], evidence["terminal_audit_records"])
    assert len(terminals) == 2
    assert [item["kind"] for item in terminals] == ["COMPLETED", "COMPLETED"]
    for item in terminals:
        _assert_current_unit_hash("production-unit-terminal-audit", item)
    assert evidence["completed_decisions_sha256"] == _journal_sha(
        "production-unit-decision-journal",
        cast(list[object], evidence["completed_decisions"]),
    )
    assert evidence["terminal_audit_records_sha256"] == _journal_sha(
        "production-unit-terminal-audit-journal",
        cast(list[object], evidence["terminal_audit_records"]),
    )


@pytest.mark.parametrize(
    ("unit_id", "host", "action_executed"),
    (
        pytest.param(
            "smoke:QWEN3_VL:SHADOW",
            PilotHostV1.QWEN3_VL,
            False,
            id="smoke-actor-success",
        ),
        pytest.param(
            "pilot:007",
            PilotHostV1.MAI_UI,
            True,
            id="pilot-action-executed",
        ),
    ),
)
def test_terminal_commit_fault_is_bound_into_current_unit_journal(
    unit_id: str,
    host: PilotHostV1,
    action_executed: bool,
) -> None:
    recovery = _audit_commit_failure_recovery(action_executed=action_executed)
    audit = SimpleNamespace(
        latest_completed_receipt=None,
        latest_failure_receipt=None,
        latest_commit_failure_receipt=recovery,
    )

    def fail_terminal_commit(**_: object) -> object:
        raise production_audit_module.ProductionRuntimeAuditError(
            "AUDIT_TERMINAL_COMMIT_FAILED",
            "injected CPU-only audit commit fault",
        )

    state = production_driver_module._ProductionUnitStateV1(
        unit_id=unit_id,
        host=host,
        task_name="commit-fault-task",
        deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        environment=None,
        observation=None,
        agent=cast(
            Any,
            SimpleNamespace(
                finalize_prompt_sentinel_action_execution=fail_terminal_commit,
            ),
        ),
        policy=None,
        runtime_audit=cast(Any, audit),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    action = production_driver_module.JSONAction(action_type=production_driver_module.WAIT)

    with pytest.raises(production_audit_module.ProductionRuntimeAuditError) as raised:
        port._receipt_for_action(
            state,
            action,
            action_executed=action_executed,
            action_execution_ns=11 if action_executed else 0,
        )
    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"

    journal = json.loads(port._unit_journal_snapshot(state))
    terminals = cast(list[dict[str, Any]], journal["terminal_audit_records"])
    assert len(terminals) == 1
    assert terminals[0]["kind"] == "COMMIT_OUTCOME_UNKNOWN"
    _assert_current_unit_hash("production-unit-terminal-audit", terminals[0])
    receipt = cast(dict[str, Any], terminals[0]["receipt"])
    assert receipt["failure_code"] == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert receipt["recovery_required"] is True
    assert len(receipt["actor_provider_attempts"]) == 1
    attempted = cast(dict[str, Any], receipt["attempted_terminal_receipt"])
    assert attempted["provider_attempt_count"] == 1
    assert attempted["live_cost_usd_micros"] == 0
    assert attempted["action_executed"] is action_executed
    assert (attempted["executed_action_sha256"] is not None) is action_executed
    assert journal["terminal_audit_records_sha256"] == _journal_sha(
        "production-unit-terminal-audit-journal", terminals
    )


def test_production_factory_requires_exact_explicit_dependencies() -> None:
    assert production_driver_available_v1()
    assert PRODUCTION_DRIVER_REQUIRED_HOOKS_V1 == tuple(ProductionDriverHookV1)
    assert len(PRODUCTION_DRIVER_REQUIRED_BINDINGS_V1) == 5
    assert "callback" not in inspect.signature(build_cpu_test_production_driver_v1).parameters
    assert tuple(inspect.signature(build_production_driver_v1).parameters) == (
        "factory",
        "runtime_config",
        "confirmed_runtime_config_sha256",
        "pricing",
        "confirmed_pricing_sha256",
        "production_audit_sink",
        "resource_lifecycle",
    )
    with pytest.raises(TypeError):
        cast(Any, build_production_driver_v1)()


def test_fixed_smoke_adapter_proves_qwen_and_mai_exact_request_chains_without_actions(
    tmp_path: Path,
) -> None:
    adapters = build_cpu_test_production_driver_v1()
    context = _context()
    lease = _Lease(context.manifest_sha256)
    resources = _resources(tmp_path)

    assert isinstance(adapters.smoke, LiveSmokeAdapterPortV1)
    for host, resource in zip(PilotHostV1, resources, strict=True):
        result = adapters.smoke.run_host(
            host,
            _smoke_plan(tmp_path, host),
            resource,
            _live_stages(),
            context,
            lease,
        )
        expected_stage = (
            RunStageV1.QWEN_LIVE_SMOKE
            if host is PilotHostV1.QWEN3_VL
            else RunStageV1.MAI_LIVE_SMOKE
        )
        evidence = adapters.smoke.evidence_for_stage(expected_stage)
        assert evidence is not None
        assert result.evidence_sha256 == smoke_stage_evidence_sha256(evidence)
        assert (result.actor_calls, result.openai_calls, result.actor_actions) == (3, 6, 0)
        assert result.provider_final_request_proven
        assert tuple(item.mode for item in evidence.cases) == tuple(SmokeModeV1)
        for item in evidence.cases:
            call = item.decision
            assert call.provider_request_sha256 == call.final_request_sha256
            assert call.executed_action_sha256 is None
            assert call.census.actor_actions == 0
            assert call.actor_attempt_receipt_sha256
            assert call.preflight_report_sha256
            if item.mode in {SmokeModeV1.OFF, SmokeModeV1.SHADOW}:
                assert call.raw_request_sha256 == call.final_request_sha256
            if item.mode is SmokeModeV1.OFF:
                assert call.case_execution_lease_sha256 is None
                assert call.history_policy_attempt_receipt_sha256 is None
                assert call.live_policy_authority_sha256 is None
            else:
                assert call.case_execution_lease_sha256 is not None
                assert call.history_policy_attempt_receipt_sha256 is not None
                assert call.live_policy_authority_sha256 is not None
                assert len(call.rubric_attempt_receipt_sha256s) == 2

    trace = adapters.cpu_trace
    assert trace.smoke_dispatches == trace.cleanup_attempts == 6
    assert trace.pilot_dispatches == trace.pilot_resets == 0
    assert trace.events[:6] == (
        "RUN:smoke:QWEN3_VL:OFF",
        "CLEANUP:smoke:QWEN3_VL:OFF",
        "RUN:smoke:QWEN3_VL:SHADOW",
        "CLEANUP:smoke:QWEN3_VL:SHADOW",
        "RUN:smoke:QWEN3_VL:ACTIVE",
        "CLEANUP:smoke:QWEN3_VL:ACTIVE",
    )


def test_production_shaped_smoke_accepts_typed_first_call_no_history_census(
    tmp_path: Path,
) -> None:
    case = _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL).cases[1]
    assert case.mode is SmokeModeV1.SHADOW and case.max_openai_calls == 3
    decision = _semantic_decision(
        actor_call_index=1,
        rubric_calls=2,
        history_policy_calls=0,
    )

    production_driver_module._validate_smoke_decision(case, decision)

    assert decision.census.openai_calls == 2
    assert decision.history_policy_attempt_receipt_sha256 is None
    assert (
        decision.pre_provider_outcome
        is production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
    )
    assert production_driver_module._semantic_pre_provider_outcome_admitted(decision)


def test_production_shaped_joint_pilot_accepts_first_no_history_then_requires_history(
    tmp_path: Path,
) -> None:
    pilot = _pilot(tmp_path)
    cell = next(item for item in pilot.cells if item.arm is PilotArmV1.JOINT_SENTINEL)
    first = _semantic_decision(
        actor_call_index=1,
        rubric_calls=2,
        history_policy_calls=0,
    )

    census = production_driver_module._validate_pilot_decisions(pilot, cell, (first,))

    assert census.rubric_openai_calls == 2
    assert census.history_policy_openai_calls == 0
    assert census.openai_calls == 2
    assert production_driver_module._semantic_pre_provider_outcome_admitted(first)


def test_fixed_pilot_adapter_runs_matched_20_task_80_cell_reset_action_result_matrix(
    tmp_path: Path,
) -> None:
    adapters = build_cpu_test_production_driver_v1()
    pilot = _pilot(tmp_path)
    context = _context()
    result = adapters.pilot.run_pilot(
        pilot,
        _resources(tmp_path),
        _live_stages(),
        context,
        _Lease(context.manifest_sha256),
    )

    assert isinstance(adapters.pilot, PilotAdapterPortV1)
    evidence = adapters.pilot.evidence
    assert evidence is not None
    assert result.evidence_sha256 == pilot_stage_evidence_sha256(evidence)
    assert (result.actor_calls, result.openai_calls, result.actor_actions) == (80, 120, 80)
    assert len(evidence.cells) == len(result.completed_units) == 80
    assert len({item.reset_receipt_sha256 for item in evidence.cells}) == 80
    assert len({item.cleanup_receipt_sha256 for item in evidence.cells}) == 80
    for index, (cell, declared) in enumerate(zip(evidence.cells, pilot.cells, strict=True)):
        assert cell.sequence_index == index
        assert (
            cell.task_id,
            cell.task_parameters_sha256,
            cell.reset_seed,
            cell.host,
            cell.arm,
            cell.sentinel_mode,
        ) == (
            declared.task_id,
            declared.task_parameters_sha256,
            declared.reset_seed,
            declared.host,
            declared.arm,
            declared.sentinel_mode,
        )
        assert cell.official_result.task_id == declared.task_id
        assert cell.census.actor_actions == 1
        assert cell.decisions[0].executed_action_sha256 == (cell.decisions[0].parsed_action_sha256)
        expected_openai = 0 if declared.arm is PilotArmV1.BASELINE else 3
        assert cell.census.openai_calls == expected_openai

    for task in pilot.tasks:
        matched = tuple(item for item in evidence.cells if item.task_id == task.task_id)
        assert len(matched) == 4
        assert {item.task_parameters_sha256 for item in matched} == {task.task_parameters_sha256}
        assert {item.reset_seed for item in matched} == {task.reset_seed}
        assert len({item.effective_reset_state_sha256 for item in matched}) == 1
        assert {(item.host, item.arm) for item in matched} == {
            (host, arm) for host in pilot.hosts for arm in pilot.arms
        }

    trace = adapters.cpu_trace
    assert (trace.pilot_resets, trace.pilot_dispatches, trace.cleanup_attempts) == (80, 80, 80)
    assert trace.events[:6] == (
        "RESET:pilot:000",
        "RUN:pilot:000",
        "CLEANUP:pilot:000",
        "RESET:pilot:001",
        "RUN:pilot:001",
        "CLEANUP:pilot:001",
    )


@pytest.mark.parametrize("cohort_size", (21, 30))
def test_fixed_pilot_adapter_accepts_full_authorized_cohort_range(
    tmp_path: Path,
    cohort_size: int,
) -> None:
    adapters = build_cpu_test_production_driver_v1()
    pilot = _pilot(tmp_path, cohort_size=cohort_size)
    context = _context()
    result = adapters.pilot.run_pilot(
        pilot,
        _resources(tmp_path),
        _live_stages(),
        context,
        _Lease(context.manifest_sha256),
    )

    evidence = adapters.pilot.evidence
    assert evidence is not None
    assert len(evidence.cells) == len(result.completed_units) == cohort_size * 4
    assert tuple(item.sequence_index for item in evidence.cells) == tuple(range(cohort_size * 4))


def test_smoke_dispatch_failure_stops_before_active_and_still_cleans_failed_unit(
    tmp_path: Path,
) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.SMOKE_SHADOW_DISPATCH_FAILURE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.smoke.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            _resources(tmp_path)[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "SMOKE_CASE_EXECUTION_FAILED"
    assert adapters.cpu_trace.events == (
        "RUN:smoke:QWEN3_VL:OFF",
        "CLEANUP:smoke:QWEN3_VL:OFF",
        "RUN:smoke:QWEN3_VL:SHADOW",
        "CLEANUP:smoke:QWEN3_VL:SHADOW",
    )
    assert adapters.smoke.evidence_for_stage(RunStageV1.QWEN_LIVE_SMOKE) is None
    failure_raw = adapters.smoke.failure_evidence_preimage(RunStageV1.QWEN_LIVE_SMOKE)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["completed_case_ids"] == ["qwen3_vl-off"]
    assert len(failure["completed_records"]) == 1
    assert failure["completed_records"][0]["cleanup_receipt_sha256"]
    assert failure["completed_records"][0]["decision"]["actor_attempt_receipt_sha256"]
    assert failure["completed_records_sha256"] == _journal_sha(
        "smoke-completed-unit-journal", failure["completed_records"]
    )
    assert failure["failure_phase"] == "DISPATCH"
    assert failure["current_unit"]["cleanup"]["status"] == "SUCCEEDED"
    assert failure["current_unit"]["port_result"] is None
    _assert_current_unit_hash("smoke-current-unit-failure-journal", failure["current_unit"])


def test_pilot_failure_is_fail_fast_and_cleans_cell_seven(tmp_path: Path) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.PILOT_CELL_007_DISPATCH_FAILURE
    )
    pilot = _pilot(tmp_path)
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.pilot.run_pilot(
            pilot,
            _resources(tmp_path),
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "PILOT_CELL_EXECUTION_FAILED"
    trace = adapters.cpu_trace
    assert (trace.pilot_resets, trace.pilot_dispatches, trace.cleanup_attempts) == (8, 8, 8)
    assert trace.events[-3:] == (
        "RESET:pilot:007",
        "RUN:pilot:007",
        "CLEANUP:pilot:007",
    )
    assert all("pilot:008" not in event for event in trace.events)
    assert adapters.pilot.evidence is None
    failure_raw = adapters.pilot.failure_evidence_preimage(RunStageV1.R25_PILOT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["completed_cell_indices"] == list(range(7))
    assert len(failure["completed_records"]) == 7
    assert all(record["cleanup_receipt_sha256"] for record in failure["completed_records"])
    assert all(record["decisions"] for record in failure["completed_records"])
    assert failure["completed_records_sha256"] == _journal_sha(
        "pilot-completed-unit-journal", failure["completed_records"]
    )
    assert failure["failure_phase"] == "DISPATCH"
    assert failure["current_unit"]["reset_result"]["reset_receipt_sha256"]
    assert failure["current_unit"]["cleanup"]["status"] == "SUCCEEDED"
    assert failure["current_unit"]["port_result"] is None
    _assert_current_unit_hash("pilot-current-unit-failure-journal", failure["current_unit"])


def test_cleanup_failure_has_distinct_typed_failure(tmp_path: Path) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.SMOKE_SHADOW_CLEANUP_FAILURE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.smoke.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            _resources(tmp_path)[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "UNIT_CLEANUP_FAILED"
    assert adapters.cpu_trace.events[-1] == "CLEANUP:smoke:QWEN3_VL:SHADOW"
    failure_raw = adapters.smoke.failure_evidence_preimage(RunStageV1.QWEN_LIVE_SMOKE)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["failure_phase"] == "CLEANUP"
    assert failure["completed_case_ids"] == ["qwen3_vl-off"]
    assert failure["current_unit"]["sequence_index"] == 1
    assert failure["current_unit"]["port_result"]["decision"]["census"] == {
        "actor_actions": 0,
        "actor_calls": 1,
        "cost_usd_micros": 0,
        "history_policy_openai_calls": 1,
        "offline_rubric_evaluations": 0,
        "openai_calls": 3,
        "rubric_openai_calls": 2,
        "wall_time_ms": 1,
    }
    assert failure["current_unit"]["cleanup"] == {
        "attempted": True,
        "cleanup_receipt_sha256": None,
        "failure_code": "UNIT_CLEANUP_FAILED",
        "status": "FAILED",
    }
    _assert_current_unit_hash("smoke-current-unit-failure-journal", failure["current_unit"])


def test_pilot_cleanup_failure_preserves_current_dispatched_cell(tmp_path: Path) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.PILOT_CELL_007_CLEANUP_FAILURE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.pilot.run_pilot(
            _pilot(tmp_path),
            _resources(tmp_path),
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "UNIT_CLEANUP_FAILED"
    failure_raw = adapters.pilot.failure_evidence_preimage(RunStageV1.R25_PILOT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["failure_phase"] == "CLEANUP"
    assert failure["completed_cell_indices"] == list(range(7))
    assert failure["current_unit"]["sequence_index"] == 7
    assert failure["current_unit"]["reset_result"]["reset_receipt_sha256"]
    assert failure["current_unit"]["port_result"]["decisions"][0]["census"]["actor_actions"] == 1
    assert failure["current_unit"]["port_result"]["official_result"]["task_id"]
    assert failure["current_unit"]["cleanup"]["status"] == "FAILED"
    _assert_current_unit_hash("pilot-current-unit-failure-journal", failure["current_unit"])


def test_smoke_post_dispatch_admission_failure_preserves_current_unit(tmp_path: Path) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.SMOKE_SHADOW_POST_DISPATCH_ADMISSION_FAILURE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.smoke.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            _resources(tmp_path)[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "SMOKE_FIXTURE_MISMATCH"
    failure_raw = adapters.smoke.failure_evidence_preimage(RunStageV1.QWEN_LIVE_SMOKE)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["failure_phase"] == "POST_DISPATCH_ADMISSION"
    assert failure["failure_code"] == "SMOKE_FIXTURE_MISMATCH"
    assert failure["completed_case_ids"] == ["qwen3_vl-off"]
    assert failure["current_unit"]["port_result"]["decision"]["provider_attempt_receipt_sha256"]
    assert failure["current_unit"]["cleanup"]["status"] == "SUCCEEDED"
    _assert_current_unit_hash("smoke-current-unit-failure-journal", failure["current_unit"])


def test_pilot_post_dispatch_admission_failure_preserves_current_unit(tmp_path: Path) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.PILOT_CELL_007_POST_DISPATCH_ADMISSION_FAILURE
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.pilot.run_pilot(
            _pilot(tmp_path),
            _resources(tmp_path),
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "INVALID_OFFICIAL_RESULT"
    failure_raw = adapters.pilot.failure_evidence_preimage(RunStageV1.R25_PILOT)
    assert failure_raw is not None
    failure = json.loads(failure_raw)
    assert failure["failure_phase"] == "POST_DISPATCH_ADMISSION"
    assert failure["failure_code"] == "INVALID_OFFICIAL_RESULT"
    assert failure["completed_cell_indices"] == list(range(7))
    assert failure["current_unit"]["port_result"]["decisions"][0]["executed_action_sha256"]
    assert failure["current_unit"]["port_result"]["official_result"]["task_id"] == (
        "post-dispatch-admission-drift"
    )
    assert failure["current_unit"]["cleanup"]["status"] == "SUCCEEDED"
    _assert_current_unit_hash("pilot-current-unit-failure-journal", failure["current_unit"])


def test_hard_reservations_and_live_role_matrix_fail_before_port_dispatch(
    tmp_path: Path,
) -> None:
    adapters = build_cpu_test_production_driver_v1()
    context = _context(actor_calls=2)
    with pytest.raises(ProductionDriverError) as raised:
        adapters.smoke.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            _resources(tmp_path)[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "STAGE_RESERVATION_EXCEEDS_CONTEXT"
    assert adapters.cpu_trace.events == ()

    rubric = _rubric_stage()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.smoke.run_host(
            PilotHostV1.MAI_UI,
            _smoke_plan(tmp_path, PilotHostV1.MAI_UI),
            _resources(tmp_path)[1],
            (rubric, rubric, _policy_stage()),
            _context(),
            _Lease("a" * 64),
        )
    assert raised.value.code == "INVALID_LIVE_OPENAI_MATRIX"
    assert adapters.cpu_trace.events == ()

    pilot_adapters = build_cpu_test_production_driver_v1()
    pilot_context = _context(openai_calls=79)
    with pytest.raises(ProductionDriverError) as raised:
        pilot_adapters.pilot.run_pilot(
            _pilot(tmp_path),
            _resources(tmp_path),
            _live_stages(),
            pilot_context,
            _Lease(pilot_context.manifest_sha256),
        )
    assert raised.value.code == "STAGE_RESERVATION_EXCEEDS_CONTEXT"
    assert pilot_adapters.cpu_trace.events == ()


def test_pilot_aborts_when_matched_cells_have_different_effective_reset_state(
    tmp_path: Path,
) -> None:
    adapters = build_cpu_test_production_driver_v1(
        CpuProductionDriverFaultV1.PILOT_CELL_001_RESET_STATE_DRIFT
    )
    context = _context()
    with pytest.raises(ProductionDriverError) as raised:
        adapters.pilot.run_pilot(
            _pilot(tmp_path),
            _resources(tmp_path),
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )
    assert raised.value.code == "PILOT_EFFECTIVE_RESET_MISMATCH"
    assert adapters.cpu_trace.events[:6] == (
        "RESET:pilot:000",
        "RUN:pilot:000",
        "CLEANUP:pilot:000",
        "RESET:pilot:001",
        "RUN:pilot:001",
        "CLEANUP:pilot:001",
    )
