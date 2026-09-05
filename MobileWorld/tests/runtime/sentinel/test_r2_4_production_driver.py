from __future__ import annotations

import base64
import hashlib
import inspect
import io
import json
import os
import random
import signal
import stat
import threading
import time
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from mobile_world.runtime.client import (
    CleanupTaskTeardownResultV1,
    CleanupTaskTeardownStatusV1,
)
from mobile_world.runtime.sentinel.r2_4 import live_executor as live_executor_module
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
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LiveRubricOperationV1,
    build_live_rubric_provider_request_v1,
)
from mobile_world.runtime.sentinel.r2_4.run_fatal import (
    build_production_run_fatal_latch_v1,
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
    restricted: dict[str, Any] = {
        "kind": "OFF_NO_SEMANTIC_WORK",
        "raw_request_sha256": digest,
    }
    pre_provider = production_audit_module.ProductionRuntimeAuditPreProviderV1(
        logical_call_id="commit-fault-logical-call-1",
        host_id="mobileworld.qwen3vl.actor",
        status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.OFF,
        outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.OFF,
        configured_mode=production_audit_module.SentinelMode.OFF,
        effective_mode=production_audit_module.SentinelMode.OFF,
        fallback_reason=None,
        fallback_check=None,
        raw_request_sha256=digest,
        extraction_sha256=None,
        history_ir_sha256=None,
        codec_overlay_sha256=None,
        vertical_output_sha256=None,
        coordinated_record_sha256=None,
        rubric_result_sha256=None,
        path_relevance_output_sha256=None,
        render_result_sha256=None,
        candidate_request_sha256=digest,
        exact_diff_sha256="9" * 64,
        validator_result_sha256="0" * 64,
        final_request_sha256=digest,
        live_call_binding_sha256=None,
        live_attempt_receipt_sha256s=(),
        live_attempt_receipt_root_sha256=None,
        case_execution_lease_sha256=None,
        preflight_report_sha256=None,
        factory_binding_sha256=None,
        execution_authority_sha256=None,
        source_transport_binding_sha256=None,
        pricing_binding_sha256=None,
        live_openai_calls=0,
        live_cost_usd_micros=0,
        live_cost_exact=True,
        restricted_stage_projection=cast(Any, restricted),
        restricted_stage_projection_sha256=canonical_sha256(cast(Any, restricted)),
        evidence_snapshot_ns=0,
        history_extract_ns=0,
        rubric_ns=0,
        policy_ns=0,
        render_ns=0,
        validator_ns=0,
        pre_provider_total_ns=0,
        _seal=production_audit_module._PRE_PROVIDER_SEAL,
    )
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
        pre_provider_sha256=(
            production_audit_module.production_runtime_audit_pre_provider_sha256(pre_provider)
        ),
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
        pre_provider=pre_provider,
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
        requested_model="gpt-5.6-sol",
        returned_model="gpt-5.6-sol",
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
        sequence_execution_scope="R24_R25_FULL",
        sequence_scope_authority_sha256="a" * 64,
        run_id="cpu-production-driver",
        source_commit="b" * 40,
        remaining_actor_calls=actor_calls,
        remaining_openai_calls=openai_calls,
        remaining_cost_usd_micros=cost_usd_micros,
        remaining_wall_time_ms=wall_time_ms,
        authority_deadline_monotonic_ns=time.monotonic_ns() + wall_time_ms * 1_000_000,
    )


def _shared_context(**kwargs: int) -> StageAdapterContextV1:
    return replace(
        _context(**kwargs),
        sequence_execution_scope="R24_LIVE_SMOKE_ONLY",
        sequence_scope_authority_sha256="f" * 64,
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
        shutdown_grace_seconds=8,
        health_poll_interval_ms=25,
    )


def _shared_runtime_config(tmp_path: Path) -> ProductionRuntimeConfigV1:
    return replace(
        _runtime_config(tmp_path),
        qwen_gpu_index=5,
        mai_gpu_index=5,
        resource_topology=(
            production_driver_module.ProductionResourceTopologyV1.SINGLE_GPU_SEQUENTIAL_SHARED
        ),
        vllm_gpu_memory_utilization="0.24",
        minimum_free_gpu_memory_mib=51_200,
    )


def test_runtime_config_rejects_grace_without_post_termination_teardown_budget(
    tmp_path: Path,
) -> None:
    accepted = _runtime_config(tmp_path)
    termination_bound_ns = (
        production_driver_module._PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
    )
    assert accepted.shutdown_grace_seconds * 1_000_000_000 - termination_bound_ns == (1_000_000_000)
    with pytest.raises(ProductionDriverError) as raised:
        replace(accepted, shutdown_grace_seconds=1)
    assert raised.value.code == "INSUFFICIENT_SHUTDOWN_GRACE"


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


def test_shared_single_gpu_prepare_handoff_dispatch_and_cleanup_are_bound(
    tmp_path: Path,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)

    prepared = adapter.prepare(_resources(tmp_path), context)
    evidence = adapter.evidence
    assert evidence is not None
    assert evidence.active_hosts == (PilotHostV1.QWEN3_VL,)
    assert len(evidence.gpu_lease_sha256s) == 1
    assert evidence.vllm_gpu_memory_utilization == "0.24"
    assert evidence.minimum_free_gpu_memory_mib == 51_200
    assert evidence.sequence_execution_scope == "R24_LIVE_SMOKE_ONLY"
    assert len(evidence.shared_gpu_attestations) == 2
    assert all(item.reserved_memory_mib == 614 for item in evidence.shared_gpu_attestations)
    assert prepared.evidence_sha256 == hashlib.sha256(prepared.evidence_preimage).hexdigest()
    model_commands = [
        item for item in adapter.cpu_trace.commands if "vllm.entrypoints.cli.main" in item
    ]
    assert len(model_commands) == 1
    assert model_commands[0][model_commands[0].index("--gpu-memory-utilization") + 1] == "0.24"

    deadline = time.monotonic_ns() + 1_000_000_000
    adapter.require_dispatch(
        PilotHostV1.QWEN3_VL,
        ProductionDispatchKindV1.ACTOR,
        authority_deadline_monotonic_ns=deadline,
    )
    with pytest.raises(ProductionDriverError) as raised:
        adapter.require_dispatch(
            PilotHostV1.MAI_UI,
            ProductionDispatchKindV1.ACTOR,
            authority_deadline_monotonic_ns=deadline,
        )
    assert raised.value.code == "RESOURCE_DISPATCH_UNAVAILABLE"

    handoff = adapter.handoff_to_mai(context)
    assert handoff.stage is RunStageV1.MAI_LIVE_SMOKE
    assert handoff.completed_units == ("resource-handoff:QWEN3_VL:MAI_UI",)
    assert handoff.evidence_sha256 == hashlib.sha256(handoff.evidence_preimage).hexdigest()
    handoff_value = json.loads(handoff.evidence_preimage)["value"]
    assert handoff_value["source_host"] == PilotHostV1.QWEN3_VL.value
    assert handoff_value["target_host"] == PilotHostV1.MAI_UI.value
    assert handoff_value["post_stop_shared_gpu_attestation"]["processes"][0]["user"]
    assert handoff_value["target_ready_shared_gpu_attestation"]["processes"][-1]["user"] == (
        "cpu-owner"
    )
    model_commands = [
        item for item in adapter.cpu_trace.commands if "vllm.entrypoints.cli.main" in item
    ]
    assert len(model_commands) == 2
    adapter.require_dispatch(
        PilotHostV1.MAI_UI,
        ProductionDispatchKindV1.ACTOR,
        authority_deadline_monotonic_ns=deadline,
    )

    adapter.cleanup(context)
    cleanup_preimage = adapter.cleanup_success_evidence_preimage()
    assert cleanup_preimage is not None
    cleanup_value = json.loads(cleanup_preimage)["value"]
    assert cleanup_value["gpu_lease_released"] is True
    assert cleanup_value["residual_capabilities"] == {
        "admitted_model_processes": [],
        "backend_candidates": [],
        "partial_model_processes": [],
        "pending_backend_ids": [],
        "pending_backend_names": [],
    }
    assert [item["host"] for item in cleanup_value["stopped_models"]] == [
        PilotHostV1.QWEN3_VL.value,
        PilotHostV1.MAI_UI.value,
    ]
    assert adapter.cpu_trace.cleanup_targets[:2] == ("pid:10000", "pid:10001")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("mai_gpu_index", 6),
        ("vllm_gpu_memory_utilization", "0.25"),
        ("minimum_free_gpu_memory_mib", 51_199),
    ),
)
def test_shared_single_gpu_configuration_is_exact(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ProductionDriverError) as raised:
        replace(_shared_runtime_config(tmp_path), **{field: value})
    assert raised.value.code == "INVALID_RESOURCE_CONFIG"


@pytest.mark.parametrize(
    ("shutdown_grace_seconds", "health_poll_interval_ms", "expected_seconds"),
    ((10, 250, 278), (60, 5_000, 540)),
)
def test_shared_cleanup_upper_bound_covers_every_bounded_cleanup_path(
    tmp_path: Path,
    shutdown_grace_seconds: int,
    health_poll_interval_ms: int,
    expected_seconds: int,
) -> None:
    config = replace(
        _shared_runtime_config(tmp_path),
        shutdown_grace_seconds=shutdown_grace_seconds,
        health_poll_interval_ms=health_poll_interval_ms,
    )
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)

    assert adapter.cleanup_upper_bound_seconds == expected_seconds
    assert (
        adapter.cleanup_upper_bound_sha256
        == hashlib.sha256(adapter.cleanup_upper_bound_preimage).hexdigest()
    )
    envelope = json.loads(adapter.cleanup_upper_bound_preimage)
    assert envelope["schema_version"] == (
        production_driver_module.PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1
    )
    assert envelope["domain"] == "production-resource-cleanup-bound"
    value = envelope["value"]
    poll_ceiling_seconds = (health_poll_interval_ms + 999) // 1_000
    assert value["admitted_model_cleanup_upper_bound_seconds"] == (
        5 * shutdown_grace_seconds + 3 * poll_ceiling_seconds
    )
    assert value["partial_model_cleanup_upper_bound_seconds"] == (
        3 * shutdown_grace_seconds + 2 * poll_ceiling_seconds
    )
    assert value["pending_backend_cleanup_upper_bound_seconds"] == 105
    assert value["final_shared_gpu_attestation_upper_bound_seconds"] == 120
    assert value["cleanup_upper_bound_seconds"] == expected_seconds
    assert value["runtime_config_sha256"] == production_runtime_config_sha256(config)
    assert production_driver_module.canonical_json_bytes(cast(Any, envelope)) == (
        adapter.cleanup_upper_bound_preimage
    )
    assert adapter.cpu_trace.commands == ()


def test_shared_cleanup_bound_hash_rejects_runtime_config_drift(
    tmp_path: Path,
) -> None:
    first_config = replace(
        _shared_runtime_config(tmp_path / "first"),
        shutdown_grace_seconds=10,
        health_poll_interval_ms=250,
    )
    drifted_config = replace(first_config, backend_port=18_090)
    first = build_cpu_test_resource_lifecycle_adapter_v1(first_config)
    drifted = build_cpu_test_resource_lifecycle_adapter_v1(drifted_config)

    assert first.cleanup_upper_bound_seconds == drifted.cleanup_upper_bound_seconds == 278
    assert first.runtime_config_sha256 != drifted.runtime_config_sha256
    assert first.cleanup_upper_bound_preimage != drifted.cleanup_upper_bound_preimage
    assert first.cleanup_upper_bound_sha256 != drifted.cleanup_upper_bound_sha256
    assert (
        json.loads(first.cleanup_upper_bound_preimage)["value"]["runtime_config_sha256"]
        == first.runtime_config_sha256
    )
    assert (
        json.loads(drifted.cleanup_upper_bound_preimage)["value"]["runtime_config_sha256"]
        == drifted.runtime_config_sha256
    )


def test_shared_cleanup_bound_exposes_insufficient_manifest_reserve_before_io(
    tmp_path: Path,
) -> None:
    config = replace(
        _shared_runtime_config(tmp_path),
        shutdown_grace_seconds=10,
        health_poll_interval_ms=250,
    )
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    manifest_cleanup_reserve_seconds = 277

    assert manifest_cleanup_reserve_seconds < adapter.cleanup_upper_bound_seconds
    assert adapter.cpu_trace == production_driver_module.CpuResourceLifecycleTraceV1(
        commands=(),
        health_endpoints=(),
        cleanup_targets=(),
        dispatch_attestations=(),
        pending_cleanup_count=0,
    )


def test_cleanup_upper_bound_is_not_claimed_for_legacy_concurrent_topology(
    tmp_path: Path,
) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_runtime_config(tmp_path))

    with pytest.raises(ProductionDriverError) as raised:
        _ = adapter.cleanup_upper_bound_seconds

    assert raised.value.code == "CLEANUP_BOUND_UNAVAILABLE"


def test_shared_gpu_attestation_binds_reserved_memory_and_uses_final_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(
        _shared_runtime_config(tmp_path), seal=production_driver_module._MODULE_SEAL
    )
    uuid = "GPU-12345678-1234-1234-1234-123456789abc"
    responses = iter(
        (
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=f"5, {uuid}, 143771, 94750, 48407, 614, 20, 33\n",
            ),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
            SimpleNamespace(returncode=0, stderr="", stdout=""),
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=f"5, {uuid}, 143771, 94730, 48427, 616, 21, 34\n",
            ),
        )
    )
    monkeypatch.setattr(
        system_type,
        "_attestation_run",
        staticmethod(lambda _argv, **_kwargs: next(responses)),
    )
    attestation = system.attest_gpu_shared_capacity(5, minimum_free_memory_mib=51_200)
    assert (attestation.free_memory_mib, attestation.used_memory_mib) == (94_730, 48_427)
    assert attestation.reserved_memory_mib == 616
    assert (
        production_driver_module.production_shared_gpu_attestation_projection(attestation)[
            "reserved_memory_mib"
        ]
        == 616
    )
    assert replace(attestation, used_memory_mib=48_423)
    with pytest.raises(ProductionDriverError) as raised:
        replace(attestation, reserved_memory_mib=620)
    assert raised.value.code == "INVALID_GPU_ATTESTATION"


def test_shared_gpu_attestation_rejects_missing_owned_compute_row(tmp_path: Path) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_shared_runtime_config(tmp_path))
    adapter.prepare(_resources(tmp_path), _shared_context())
    evidence = adapter.evidence
    assert evidence is not None
    baseline, ready = evidence.shared_gpu_attestations
    owned = evidence.model_processes[0]
    missing = replace(ready, processes=baseline.processes)
    with pytest.raises(ProductionDriverError) as raised:
        production_driver_module._validate_shared_gpu_tenants(
            missing, baseline=baseline, owned_identity=owned
        )
    assert raised.value.code == "GPU_OWNED_PROCESS_ABSENT"
    adapter.cleanup(_shared_context())


def test_shared_initial_capacity_failure_reclaims_single_lease(tmp_path: Path) -> None:
    config = _shared_runtime_config(tmp_path)
    failed = build_cpu_test_resource_lifecycle_adapter_v1(
        config, CpuResourceLifecycleFaultV1.SHARED_GPU_CAPACITY_BELOW_MINIMUM
    )
    with pytest.raises(ProductionDriverError) as raised:
        failed.prepare(_resources(tmp_path), _shared_context())
    assert raised.value.code == "GPU_SHARED_CAPACITY_INSUFFICIENT"
    failure = failed.failure_evidence_preimage(RunStageV1.RESOURCE_PREFLIGHT)
    assert failure is not None
    value = json.loads(failure)
    assert value["failure_code"] == "GPU_SHARED_CAPACITY_INSUFFICIENT"
    assert value["cleanup_status"] == "RECLAIMED"
    reclaimed = value["reclaimed_cleanup_outcome"]
    assert len(reclaimed["value"]["gpu_lease_sha256s"]) == 1
    assert reclaimed["value"]["backend_container_id"]
    assert (
        value["reclaimed_cleanup_outcome_sha256"]
        == hashlib.sha256(
            production_driver_module.canonical_json_bytes(cast(Any, reclaimed))
        ).hexdigest()
    )
    failed.cleanup(_shared_context())
    cleanup = failed.cleanup_success_evidence_preimage()
    assert cleanup is not None
    cleanup_value = json.loads(cleanup)["value"]
    assert cleanup_value["cleanup_outcome"] == "SHARED_MODELS_RECLAIMED"
    assert cleanup_value["gpu_lease_sha256s"] == reclaimed["value"]["gpu_lease_sha256s"]
    assert cleanup_value["backend_container_id"] == reclaimed["value"]["backend_container_id"]
    assert cleanup_value["reclaimed_cleanup_outcome"] == reclaimed

    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), _shared_context())
    replacement.cleanup(_shared_context())


@pytest.mark.parametrize(
    "failure_point",
    ("attest_runtime", "start_backend", "attest_gpu_shared_capacity"),
)
def test_shared_prepare_failure_before_baseline_has_idempotent_cleanup_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    system_type = type(adapter._system)
    original = getattr(system_type, failure_point)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise ProductionDriverError(
            "CPU_PRE_BASELINE_FAILURE", f"CPU {failure_point} failed before baseline"
        )

    monkeypatch.setattr(system_type, failure_point, fail)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.prepare(_resources(tmp_path), context)
    assert raised.value.code == "CPU_PRE_BASELINE_FAILURE"
    adapter.cleanup(context)
    cleanup = adapter.cleanup_success_evidence_preimage()
    assert cleanup is not None
    cleanup_value = json.loads(cleanup)["value"]
    assert cleanup_value["cleanup_outcome"] == "PRE_BASELINE_NO_MODEL_RECLAIMED"
    assert cleanup_value["gpu_lease_released"] is True
    assert cleanup_value["prepare_failure_evidence_sha256"]

    monkeypatch.setattr(system_type, failure_point, original)
    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), context)
    replacement.cleanup(context)


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        (
            CpuResourceLifecycleFaultV1.HANDOFF_GPU_CAPACITY_BELOW_MINIMUM,
            "GPU_SHARED_CAPACITY_INSUFFICIENT",
        ),
        (
            CpuResourceLifecycleFaultV1.HANDOFF_MODEL_REAP_UNCONFIRMED,
            "MODEL_REAP_UNCONFIRMED",
        ),
        (
            CpuResourceLifecycleFaultV1.MAI_PARTIAL_START,
            "MODEL_PARTIAL_START_RECOVERABLE",
        ),
    ),
)
def test_shared_handoff_failure_retains_lease_until_cleanup(
    tmp_path: Path,
    fault: CpuResourceLifecycleFaultV1,
    expected_code: str,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config, fault)
    adapter.prepare(_resources(tmp_path), context)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.handoff_to_mai(context)
    assert raised.value.code == expected_code
    failure = adapter.handoff_failure_evidence_preimage()
    assert failure is not None
    failure_value = json.loads(failure)["value"]
    assert failure_value["failure_code"] == expected_code
    assert failure_value["status"] == "FAILED_CLEANUP_REQUIRED"

    competing = build_cpu_test_resource_lifecycle_adapter_v1(config)
    with pytest.raises(ProductionDriverError) as lease_raised:
        competing.prepare(_resources(tmp_path), context)
    assert lease_raised.value.code == "GPU_LEASE_CONFLICT"
    adapter.cleanup(context)
    assert adapter.cleanup_success_evidence_preimage() is not None
    assert all(not target.startswith("pid:42") for target in adapter.cpu_trace.cleanup_targets)


def test_shared_handoff_snapshot_crossing_deadline_never_starts_mai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    adapter.prepare(_resources(tmp_path), context)
    now_ns = [1]
    handoff_context = replace(context, authority_deadline_monotonic_ns=100)
    original_attest = production_driver_module._attest_snapshot_resource

    def attest_then_expire(resource: SnapshotResourceV1) -> str:
        result = original_attest(resource)
        now_ns[0] = 100
        return result

    monkeypatch.setattr(production_driver_module.time, "monotonic_ns", lambda: now_ns[0])
    monkeypatch.setattr(production_driver_module, "_attest_snapshot_resource", attest_then_expire)

    with pytest.raises(ProductionDriverError) as raised:
        adapter.handoff_to_mai(handoff_context)

    assert raised.value.code == "OWNER_AUTHORITY_EXPIRED"
    model_commands = [
        item for item in adapter.cpu_trace.commands if "vllm.entrypoints.cli.main" in item
    ]
    assert len(model_commands) == 1
    assert not any("18082" in item for command in model_commands for item in command)
    failure = adapter.handoff_failure_evidence_preimage()
    assert failure is not None
    assert json.loads(failure)["value"]["failure_code"] == "OWNER_AUTHORITY_EXPIRED"

    now_ns[0] = 1
    adapter.cleanup(replace(context, authority_deadline_monotonic_ns=1_000))
    assert adapter.cleanup_success_evidence_preimage() is not None


def test_shared_handoff_start_crossing_deadline_tracks_and_cleans_mai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    adapter.prepare(_resources(tmp_path), context)
    now_ns = [1]
    handoff_context = replace(context, authority_deadline_monotonic_ns=100)
    system_type = production_driver_module._CpuRecordingResourceSystemV1
    original_start = system_type.start_model

    def start_then_expire(
        system: object,
        spec: production_driver_module.ProductionCommandSpecV1,
        *,
        log_label: str,
    ) -> object:
        result = original_start(cast(Any, system), spec, log_label=log_label)
        if spec.kind == "VLLM_MAI":
            now_ns[0] = 100
        return result

    monkeypatch.setattr(production_driver_module.time, "monotonic_ns", lambda: now_ns[0])
    monkeypatch.setattr(system_type, "start_model", start_then_expire)

    with pytest.raises(ProductionDriverError) as raised:
        adapter.handoff_to_mai(handoff_context)

    assert raised.value.code == "OWNER_AUTHORITY_EXPIRED"
    model_commands = [
        item for item in adapter.cpu_trace.commands if "vllm.entrypoints.cli.main" in item
    ]
    assert len(model_commands) == 2
    assert not any(
        endpoint.startswith("http://127.0.0.1:18082")
        for endpoint in adapter.cpu_trace.health_endpoints
    )
    failure = adapter.handoff_failure_evidence_preimage()
    assert failure is not None
    assert json.loads(failure)["value"]["failure_code"] == "OWNER_AUTHORITY_EXPIRED"
    assert set(adapter._models) == {PilotHostV1.MAI_UI}

    now_ns[0] = 1
    adapter.cleanup(replace(context, authority_deadline_monotonic_ns=1_000))
    assert "pid:10001" in adapter.cpu_trace.cleanup_targets
    assert adapter.cleanup_success_evidence_preimage() is not None


@pytest.mark.parametrize(
    "expiry_boundary",
    ("post-stop-capacity", "await-ready", "snapshot-recheck"),
)
def test_shared_handoff_deadline_gates_each_following_external_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expiry_boundary: str,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    adapter.prepare(_resources(tmp_path), context)
    now_ns = [1]
    snapshot_calls = [0]
    handoff_context = replace(context, authority_deadline_monotonic_ns=100)
    system_type = production_driver_module._CpuRecordingResourceSystemV1
    original_capacity = system_type.attest_gpu_shared_capacity
    original_await = system_type.await_model
    original_snapshot = production_driver_module._attest_snapshot_resource

    def capacity_then_maybe_expire(
        system: object,
        gpu_index: int,
        *,
        minimum_free_memory_mib: int,
    ) -> production_driver_module.ProductionSharedGpuAttestationV1:
        result = original_capacity(
            cast(Any, system),
            gpu_index,
            minimum_free_memory_mib=minimum_free_memory_mib,
        )
        if (
            expiry_boundary == "post-stop-capacity"
            and system is adapter._system
            and cast(Any, system)._shared_attestation_count == 3
        ):
            now_ns[0] = 100
        return result

    def await_then_maybe_expire(
        system: object,
        owned: object,
        *,
        deadline_ns: int,
    ) -> str:
        result = original_await(cast(Any, system), cast(Any, owned), deadline_ns=deadline_ns)
        if expiry_boundary == "await-ready" and cast(Any, owned).spec.kind == "VLLM_MAI":
            now_ns[0] = 100
        return result

    def snapshot_then_maybe_expire(resource: SnapshotResourceV1) -> str:
        result = original_snapshot(resource)
        snapshot_calls[0] += 1
        if expiry_boundary == "snapshot-recheck" and snapshot_calls[0] == 2:
            now_ns[0] = 100
        return result

    monkeypatch.setattr(production_driver_module.time, "monotonic_ns", lambda: now_ns[0])
    monkeypatch.setattr(system_type, "attest_gpu_shared_capacity", capacity_then_maybe_expire)
    monkeypatch.setattr(system_type, "await_model", await_then_maybe_expire)
    monkeypatch.setattr(
        production_driver_module,
        "_attest_snapshot_resource",
        snapshot_then_maybe_expire,
    )

    with pytest.raises(ProductionDriverError) as raised:
        adapter.handoff_to_mai(handoff_context)

    assert raised.value.code == "OWNER_AUTHORITY_EXPIRED"
    assert cast(Any, adapter._system)._shared_attestation_count == 3
    expected_snapshots = {
        "post-stop-capacity": 0,
        "await-ready": 1,
        "snapshot-recheck": 2,
    }
    assert snapshot_calls[0] == expected_snapshots[expiry_boundary]
    model_commands = [
        item for item in adapter.cpu_trace.commands if "vllm.entrypoints.cli.main" in item
    ]
    assert len(model_commands) == (1 if expiry_boundary == "post-stop-capacity" else 2)

    now_ns[0] = 1
    adapter.cleanup(replace(context, authority_deadline_monotonic_ns=1_000))
    assert adapter.cleanup_success_evidence_preimage() is not None


def test_shared_dispatch_tenant_drift_retains_complete_offender_and_recovers(
    tmp_path: Path,
) -> None:
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(
        _shared_runtime_config(tmp_path),
        CpuResourceLifecycleFaultV1.SHARED_GPU_TENANT_DRIFT,
    )
    context = _shared_context()
    adapter.prepare(_resources(tmp_path), context)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.require_dispatch(
            PilotHostV1.QWEN3_VL,
            ProductionDispatchKindV1.ACTOR,
            authority_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        )
    assert raised.value.code == "GPU_SHARED_TENANT_DRIFT"
    failure = adapter.last_dispatch_failure_evidence_preimage()
    assert failure is not None
    value = json.loads(failure)["value"]
    assert value["sequence_execution_scope"] == "R24_LIVE_SMOKE_ONLY"
    offender = value["shared_gpu_attestation"]["processes"][-1]
    assert offender == {
        "pid": 42_999,
        "process_group_id": 42_999,
        "session_id": 42_999,
        "starttime_ticks": 4_299_900,
        "uid": os.geteuid(),
        "used_gpu_memory_mib": 1_024,
        "user": "cpu-owner",
    }
    adapter.cleanup(context)


def test_successful_shared_dispatch_attestation_enters_full_unit_journal(
    tmp_path: Path,
) -> None:
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(_shared_runtime_config(tmp_path))
    adapter.prepare(_resources(tmp_path), context)
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_resource_lifecycle", adapter)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_unit_journals", {})
    object.__setattr__(port, "_unpublished_unit_evidence", {})
    deadline = time.monotonic_ns() + 2_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id="smoke:QWEN3_VL:OFF",
        host=PilotHostV1.QWEN3_VL,
        task_name="shared-dispatch-proof",
        deadline_monotonic_ns=deadline,
        cleanup_deadline_monotonic_ns=deadline + 1_000_000_000,
        authority_deadline_monotonic_ns=deadline + 2_000_000_000,
        attempt_termination_upper_bound_ns=0,
        environment=None,
        observation=None,
    )
    port._require_resource_dispatch(
        PilotHostV1.QWEN3_VL,
        ProductionDispatchKindV1.ACTOR,
        deadline_ns=deadline,
        state=state,
    )
    assert len(state.resource_dispatch_journal) == 1
    dispatch = state.resource_dispatch_journal[0]
    assert dispatch["value"]["status"] == "PASSED"
    assert dispatch["value"]["shared_gpu_attestation"]["processes"][-1]["user"] == ("cpu-owner")
    case = _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL).cases[0]
    invocation = production_driver_module._SmokeInvocationV1(
        manifest_sha256=context.manifest_sha256,
        run_id=context.run_id,
        source_commit=context.source_commit,
        host=PilotHostV1.QWEN3_VL,
        sequence_index=0,
        case=case,
        actor_resource_sha256="1" * 64,
        history_policy_stage_sha256="2" * 64,
        deadline_monotonic_ns=state.deadline_monotonic_ns,
        cleanup_deadline_monotonic_ns=state.cleanup_deadline_monotonic_ns,
        authority_deadline_monotonic_ns=state.authority_deadline_monotonic_ns,
        attempt_termination_upper_bound_ns=0,
    )
    object.__setattr__(port, "_units", {state.unit_id: state})
    cleanup = port.cleanup_unit(invocation)
    assert cleanup.unit_journal_preimage is not None
    assert cleanup.unit_journal_sha256 == hashlib.sha256(cleanup.unit_journal_preimage).hexdigest()
    full = json.loads(cleanup.unit_journal_preimage)
    assert full["resource_dispatch_records"] == state.resource_dispatch_journal
    assert full["resource_dispatch_records_sha256"]

    decision = _semantic_decision(actor_call_index=1, rubric_calls=2, history_policy_calls=1)
    census = production_driver_module._sum_census((decision.census,))
    records = []
    for index, mode in enumerate(SmokeModeV1):
        journal = dict(full)
        journal["unit_id"] = f"smoke:{PilotHostV1.QWEN3_VL.value}:{mode.value}"
        journal_raw = production_driver_module.canonical_json_bytes(cast(Any, journal))
        records.append(
            production_driver_module.SmokeCaseEvidenceV1(
                manifest_sha256=context.manifest_sha256,
                run_id=context.run_id,
                stage=RunStageV1.QWEN_LIVE_SMOKE,
                host=PilotHostV1.QWEN3_VL,
                sequence_index=index,
                case_id=f"qwen-{mode.value.lower()}",
                task_id="shared-dispatch-proof",
                mode=mode,
                actor_resource_sha256="1" * 64,
                history_policy_stage_sha256="2" * 64,
                request_fixture_sha256="3" * 64,
                request_fixture_byte_count=100,
                decision=decision,
                cleanup_receipt_sha256=cleanup.cleanup_receipt_sha256,
                unit_journal_preimage=journal_raw,
                unit_journal_sha256=hashlib.sha256(journal_raw).hexdigest(),
                unit_journal_validated_reference=None,
                census=census,
            )
        )
    stage_evidence = production_driver_module.SmokeStageEvidenceV1(
        manifest_sha256=context.manifest_sha256,
        run_id=context.run_id,
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=PilotHostV1.QWEN3_VL,
        actor_resource_sha256="1" * 64,
        history_policy_stage_sha256="2" * 64,
        cases=tuple(records),
        census=production_driver_module._sum_census(tuple(item.census for item in records)),
        schema_version=(
            production_driver_module.PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2
        ),
    )
    durable = production_driver_module.smoke_stage_evidence_projection(stage_evidence)
    assert durable["schema_version"] == (
        production_driver_module.PRODUCTION_SHARED_SMOKE_EVIDENCE_SCHEMA_VERSION_V2
    )
    assert durable["cases"][0]["unit_journal"] == full
    assert durable["cases"][0]["unit_journal_sha256"] == cleanup.unit_journal_sha256
    adapter.cleanup(context)


def test_shared_predispatch_failure_cleanup_preserves_dispatch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _shared_context()
    config = _shared_runtime_config(tmp_path)
    resources = _resources(tmp_path)
    resource_lifecycle = build_cpu_test_resource_lifecycle_adapter_v1(config)
    resource_lifecycle.prepare(resources, context)
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_config", config)
    object.__setattr__(port, "_resource_lifecycle", resource_lifecycle)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_units", {})
    object.__setattr__(port, "_unit_journals", {})
    object.__setattr__(port, "_unpublished_unit_evidence", {})

    def reject_fixture_before_dispatch(
        exact_port: production_driver_module._ProductionFixedExecutionPortV1,
        invocation: production_driver_module._SmokeInvocationV1,
        _: object,
    ) -> None:
        state = production_driver_module._ProductionUnitStateV1(
            unit_id=exact_port._unit_id(invocation),
            host=invocation.host,
            task_name=invocation.case.task_id,
            deadline_monotonic_ns=invocation.deadline_monotonic_ns,
            cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
            authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
            attempt_termination_upper_bound_ns=invocation.attempt_termination_upper_bound_ns,
            environment=None,
            observation=None,
        )
        exact_port._units[state.unit_id] = state
        raise ProductionDriverError(
            "SMOKE_FIXTURE_MISMATCH",
            "fixture actor binding differs",
        )

    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "run_smoke_case",
        reject_fixture_before_dispatch,
    )
    adapter = production_driver_module.FixedLiveSmokeAdapterV1(
        port,
        seal=production_driver_module._MODULE_SEAL,
    )

    with pytest.raises(ProductionDriverError) as raised:
        adapter.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            resources[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )

    assert raised.value.code == "SMOKE_CASE_EXECUTION_FAILED"
    failure_raw = adapter.failure_evidence_preimage(RunStageV1.QWEN_LIVE_SMOKE)
    assert failure_raw is not None
    failure = cast(dict[str, Any], json.loads(failure_raw))
    assert failure["failure_phase"] == "DISPATCH"
    assert failure["failure_code"] == "SMOKE_CASE_EXECUTION_FAILED"
    assert failure["dispatch_failure_code"] == "SMOKE_FIXTURE_MISMATCH"
    assert failure["current_unit"]["cleanup"]["status"] == "SUCCEEDED"
    unit_failure = cast(dict[str, Any], failure["unit_failure_evidence"])
    assert unit_failure["resource_dispatch_records"] == []
    assert unit_failure["resource_dispatch_records_sha256"] == _journal_sha(
        "production-unit-resource-dispatch-journal",
        [],
    )
    assert port._units == {}
    assert tuple(port._unit_journals) == ("smoke:QWEN3_VL:OFF",)
    resource_lifecycle.cleanup(context)


def test_production_smoke_port_keeps_fixture_and_actor_request_hashes_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_source = (
        Path(__file__).resolve().parents[2]
        / "offline"
        / "fixtures"
        / "g1_5_history_codecs"
        / "qwen_flat_progress.captured.v1.json"
    )
    fixture_value = cast(dict[str, Any], json.loads(fixture_source.read_bytes()))
    fixture_raw = production_driver_module.canonical_json_bytes(cast(Any, fixture_value))
    actor_request_sha256 = cast(str, fixture_value["fixture_request_sha256"])
    fixture_artifact_sha256 = hashlib.sha256(fixture_raw).hexdigest()
    assert actor_request_sha256 != fixture_artifact_sha256

    config = _shared_runtime_config(tmp_path)
    fixture_root = Path(config.authorized_pilot_input_root)
    fixture_root.mkdir(parents=True)
    Path(config.repository_root).mkdir(parents=True)
    fixture_path = fixture_root / "qwen-off.captured.v1.json"
    fixture_path.write_bytes(fixture_raw)
    case = replace(
        _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL).cases[0],
        request_fixture_path=str(fixture_path),
        request_fixture_sha256=fixture_artifact_sha256,
        request_fixture_byte_count=len(fixture_raw),
    )
    now = time.monotonic_ns()
    invocation = production_driver_module._SmokeInvocationV1(
        manifest_sha256="a" * 64,
        run_id="fixture-artifact-hash-run",
        source_commit="b" * 40,
        host=PilotHostV1.QWEN3_VL,
        sequence_index=0,
        case=case,
        actor_resource_sha256="c" * 64,
        history_policy_stage_sha256="d" * 64,
        deadline_monotonic_ns=now + 10_000_000_000,
        cleanup_deadline_monotonic_ns=now + 18_000_000_000,
        authority_deadline_monotonic_ns=now + 20_000_000_000,
        attempt_termination_upper_bound_ns=(
            production_driver_module._PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
        ),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_config", config)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_units", {})

    def return_actor_request_evidence(
        _: production_driver_module._ProductionFixedExecutionPortV1,
        state: production_driver_module._ProductionUnitStateV1,
        fixture: production_driver_module._LoadedSmokeFixtureV1,
    ) -> tuple[str, object, production_driver_module.ActorDecisionEvidenceV1]:
        assert fixture.request_sha256 == actor_request_sha256
        base = _semantic_decision(actor_call_index=1, rubric_calls=2, history_policy_calls=1)
        decision = replace(
            base,
            logical_call_id="fixture-artifact-hash-call",
            raw_request_sha256=fixture.request_sha256,
            final_request_sha256=fixture.request_sha256,
            provider_request_sha256=fixture.request_sha256,
            pre_provider_status=(
                production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.OFF
            ),
            pre_provider_outcome=(
                production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.OFF
            ),
            fallback_reason=None,
            fallback_check=None,
            case_execution_lease_sha256=None,
            live_policy_authority_sha256=None,
            rubric_attempt_receipt_sha256s=(),
            history_policy_attempt_receipt_sha256=None,
            census=production_driver_module.DriverCallCensusV1(
                actor_calls=1,
                offline_rubric_evaluations=0,
                rubric_openai_calls=0,
                history_policy_openai_calls=0,
                openai_calls=0,
                actor_actions=0,
                cost_usd_micros=0,
                wall_time_ms=1,
            ),
        )
        state.decision_journal.append(decision)
        return "actor output", object(), decision

    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "_require_broker",
        lambda *_: None,
    )
    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "_begin_task_runtime",
        lambda *_, **__: None,
    )
    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "_dispatch_smoke_fixture",
        return_actor_request_evidence,
    )

    result = port.run_smoke_case(invocation, cast(Any, object()))

    assert result.request_fixture_sha256 == fixture_artifact_sha256
    assert result.request_fixture_sha256 == invocation.case.request_fixture_sha256
    assert result.request_fixture_byte_count == len(fixture_raw)
    assert result.decision.raw_request_sha256 == actor_request_sha256


def test_shared_cleanup_records_persistent_foreign_tenant_without_signaling_it(
    tmp_path: Path,
) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    adapter = build_cpu_test_resource_lifecycle_adapter_v1(
        config,
        CpuResourceLifecycleFaultV1.SHARED_GPU_TENANT_DRIFT_PERSISTS,
    )
    adapter.prepare(_resources(tmp_path), context)
    with pytest.raises(ProductionDriverError) as raised:
        adapter.require_dispatch(
            PilotHostV1.QWEN3_VL,
            ProductionDispatchKindV1.ACTOR,
            authority_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        )
    assert raised.value.code == "GPU_SHARED_TENANT_DRIFT"
    adapter.cleanup(context)
    cleanup = adapter.cleanup_success_evidence_preimage()
    assert cleanup is not None
    final_processes = json.loads(cleanup)["value"]["final_shared_gpu_attestation"]["processes"]
    assert final_processes[-1]["pid"] == 42_999
    assert final_processes[-1]["user"] == "cpu-owner"
    assert all(not item.startswith("pid:42") for item in adapter.cpu_trace.cleanup_targets)

    replacement = build_cpu_test_resource_lifecycle_adapter_v1(config)
    replacement.prepare(_resources(tmp_path), context)
    replacement.cleanup(context)


def test_shared_scope_tamper_blocks_prepare_handoff_and_cleanup(tmp_path: Path) -> None:
    config = _shared_runtime_config(tmp_path)
    context = _shared_context()
    unauthorized = replace(context, sequence_execution_scope="R24_R25_FULL")
    unprepared = build_cpu_test_resource_lifecycle_adapter_v1(config)
    with pytest.raises(ProductionDriverError) as raised:
        unprepared.prepare(_resources(tmp_path), unauthorized)
    assert raised.value.code == "RESOURCE_SCOPE_UNAUTHORIZED"

    adapter = build_cpu_test_resource_lifecycle_adapter_v1(config)
    adapter.prepare(_resources(tmp_path), context)
    tampered = replace(context, sequence_scope_authority_sha256="e" * 64)
    with pytest.raises(ProductionDriverError) as handoff_raised:
        adapter.handoff_to_mai(tampered)
    assert handoff_raised.value.code == "RESOURCE_BINDING_MISMATCH"
    with pytest.raises(ProductionDriverError) as cleanup_raised:
        adapter.cleanup(tampered)
    assert cleanup_raised.value.code == "RESOURCE_BINDING_MISMATCH"
    adapter.cleanup(context)


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


def test_owned_session_drain_signals_leaderless_worker_by_stable_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leader = production_driver_module.OwnedProcessIdentityV1(
        pid=20_000,
        process_group_id=20_000,
        session_id=20_000,
        starttime_ticks=2_000_000,
        uid=os.geteuid(),
    )
    worker = production_driver_module._OwnedSessionMemberV1(
        pid=20_001,
        process_group_id=20_001,
        session_id=20_000,
        starttime_ticks=2_000_100,
        uid=os.geteuid(),
    )
    scans = iter(((worker,), (worker,), ()))
    signals: list[tuple[int, int]] = []
    clocks = iter((0, 0, 2_000_000_000, 2_000_000_000, 2_000_000_000))
    monkeypatch.setattr(
        production_driver_module, "_owned_session_members", lambda _identity: next(scans)
    )
    monkeypatch.setattr(
        production_driver_module,
        "_signal_owned_session_member",
        lambda member, signum: signals.append((member.pid, signum)),
    )
    monkeypatch.setattr(production_driver_module.time, "monotonic_ns", lambda: next(clocks))
    monkeypatch.setattr(production_driver_module.time, "sleep", lambda _seconds: None)

    remaining = production_driver_module._drain_owned_session(
        leader, shutdown_grace_seconds=1, poll_interval_ms=1
    )
    assert remaining == ()
    assert signals == [(worker.pid, signal.SIGTERM), (worker.pid, signal.SIGKILL)]


def test_partial_model_cleanup_drains_leaderless_session_and_checks_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _shared_runtime_config(tmp_path)
    system_type = production_driver_module._PosixProductionResourceSystemV1
    system = system_type(config, seal=production_driver_module._MODULE_SEAL)
    spec = production_driver_module._vllm_command_spec(
        _resources(tmp_path)[0], gpu_index=5, config=config
    )

    class _ExitedProcess:
        pid = 20_100

        @staticmethod
        def poll() -> int:
            return 0

    stdout = io.BytesIO()
    stderr = io.BytesIO()
    partial = production_driver_module._PartialModelProcessV1(
        cast(Any, _ExitedProcess()), spec=spec, stdout_handle=stdout, stderr_handle=stderr
    )
    system._partial_models[20_100] = partial
    drained: list[production_driver_module.OwnedProcessIdentityV1] = []
    monkeypatch.setattr(
        production_driver_module,
        "_drain_owned_session",
        lambda identity, **_kwargs: drained.append(identity) or (),
    )
    checked_ports: list[int] = []
    monkeypatch.setattr(
        production_driver_module,
        "_assert_loopback_port_free",
        lambda port: checked_ports.append(port),
    )

    system._stop_partial_model(20_100)
    assert [(item.pid, item.session_id, item.uid) for item in drained] == [
        (20_100, 20_100, os.geteuid())
    ]
    assert checked_ports == [18081]
    assert system.residual_capabilities()["partial_model_processes"] == []
    assert stdout.closed and stderr.closed


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


def test_unit_deadlines_reserve_hash_bound_cleanup_grace_before_dispatch() -> None:
    started_ns = time.monotonic_ns()
    owner_deadline_ns = started_ns + 30_000_000_000

    execution_deadline_ns, cleanup_deadline_ns = production_driver_module._freeze_unit_deadlines(
        unit_started_ns=started_ns,
        unit_timeout_seconds=20,
        authority_deadline_monotonic_ns=owner_deadline_ns,
        shutdown_grace_seconds=8,
        attempt_termination_upper_bound_ns=(
            production_driver_module._PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
        ),
    )

    assert cleanup_deadline_ns <= owner_deadline_ns
    assert cleanup_deadline_ns - execution_deadline_ns == 8_000_000_000

    with pytest.raises(ProductionDriverError) as raised:
        production_driver_module._freeze_unit_deadlines(
            unit_started_ns=started_ns,
            unit_timeout_seconds=20,
            authority_deadline_monotonic_ns=started_ns + 8_000_000_000,
            shutdown_grace_seconds=8,
            attempt_termination_upper_bound_ns=(
                production_driver_module._PRODUCTION_ATTEMPT_TERMINATION_UPPER_BOUND_NS_V1
            ),
        )
    assert raised.value.code == "INSUFFICIENT_CLEANUP_WINDOW"


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
    deadline_ns = time.monotonic_ns() + 1_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id=f"fallback:{len(roles)}",
        host=PilotHostV1.QWEN3_VL,
        task_name="generic-fallback-task",
        deadline_monotonic_ns=deadline_ns,
        cleanup_deadline_monotonic_ns=deadline_ns,
        authority_deadline_monotonic_ns=deadline_ns,
        attempt_termination_upper_bound_ns=0,
        environment=None,
        observation=None,
        policy=cast(Any, _Policy()),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
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
    deadline_ns = time.monotonic_ns() + 1_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id="smoke:QWEN3_VL:OFF",
        host=PilotHostV1.QWEN3_VL,
        task_name="task-smoke-qwen3_vl",
        deadline_monotonic_ns=deadline_ns,
        cleanup_deadline_monotonic_ns=deadline_ns,
        authority_deadline_monotonic_ns=deadline_ns,
        attempt_termination_upper_bound_ns=0,
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
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
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
        cleanup_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        authority_deadline_monotonic_ns=time.monotonic_ns() + 1_000_000_000,
        attempt_termination_upper_bound_ns=0,
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

    deadline_ns = time.monotonic_ns() + 1_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id=unit_id,
        host=host,
        task_name="commit-fault-task",
        deadline_monotonic_ns=deadline_ns,
        cleanup_deadline_monotonic_ns=deadline_ns,
        authority_deadline_monotonic_ns=deadline_ns,
        attempt_termination_upper_bound_ns=0,
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
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
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
    pre_provider = cast(dict[str, Any], receipt["pre_provider"])
    assert canonical_sha256(cast(Any, pre_provider)) == attempted["pre_provider_sha256"]
    assert attempted["provider_attempt_count"] == 1
    assert attempted["live_cost_usd_micros"] == 0
    assert attempted["action_executed"] is action_executed
    assert (attempted["executed_action_sha256"] is not None) is action_executed
    assert journal["terminal_audit_records_sha256"] == _journal_sha(
        "production-unit-terminal-audit-journal", terminals
    )


def test_pre_provider_admission_failure_is_bound_without_a_transaction() -> None:
    pre_provider = _audit_commit_failure_recovery(action_executed=False).pre_provider
    recovery = production_audit_module.ProductionRuntimeAuditAdmissionFailureReceiptV1(
        logical_call_id=pre_provider.logical_call_id,
        publication_status=(
            production_audit_module.ProductionRuntimeAuditPublicationStatusV1.ADMISSION_OUTCOME_UNKNOWN
        ),
        failure_phase="AUDIT_PRE_PROVIDER_ADMISSION",
        failure_code="AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
        admission_stage=(
            production_audit_module.ProductionRuntimeAuditAdmissionStageV1.ADMISSION_FILE_FSYNC
        ),
        sink_exception_type="OSError",
        pre_provider=pre_provider,
        pre_provider_sha256=(
            production_audit_module.production_runtime_audit_pre_provider_sha256(pre_provider)
        ),
        sentinel_receipt_sha256="f" * 64,
        _seal=production_audit_module._ADMISSION_FAILURE_RECEIPT_SEAL,
    )
    audit = SimpleNamespace(
        latest_admission_failure_receipt=recovery,
        latest_completed_receipt=None,
        latest_failure_receipt=None,
        latest_commit_failure_receipt=None,
    )
    deadline_ns = time.monotonic_ns() + 1_000_000_000
    state = production_driver_module._ProductionUnitStateV1(
        unit_id="smoke:QWEN3_VL:OFF",
        host=PilotHostV1.QWEN3_VL,
        task_name="admission-fault-task",
        deadline_monotonic_ns=deadline_ns,
        cleanup_deadline_monotonic_ns=deadline_ns,
        authority_deadline_monotonic_ns=deadline_ns,
        attempt_termination_upper_bound_ns=0,
        environment=None,
        observation=None,
        runtime_audit=cast(Any, audit),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())

    journal = json.loads(port._unit_journal_snapshot(state))

    terminals = cast(list[dict[str, Any]], journal["terminal_audit_records"])
    assert len(terminals) == 1
    assert terminals[0]["kind"] == "ADMISSION_OUTCOME_UNKNOWN"
    receipt = cast(dict[str, Any], terminals[0]["receipt"])
    assert receipt["pre_provider"] == (
        production_audit_module.production_runtime_audit_pre_provider_projection(pre_provider)
    )
    assert receipt["pre_provider_sha256"] == (
        production_audit_module.production_runtime_audit_pre_provider_sha256(pre_provider)
    )
    assert receipt["recovery_required"] is True
    assert receipt["admission_stage"] == "ADMISSION_FILE_FSYNC"
    assert journal["terminal_audit_records_sha256"] == _journal_sha(
        "production-unit-terminal-audit-journal", terminals
    )


@pytest.fixture(scope="module")
def large_legal_rubric_provider_request() -> dict[str, Any]:
    """Build a real, schema-bounded Responses request above the old 4 MiB cap."""

    pixels = random.Random(2404).randbytes(1280 * 1280 * 3)
    image = Image.frombytes("RGB", (1280, 1280), pixels)
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
    request = build_live_rubric_provider_request_v1(
        operation=LiveRubricOperationV1.TRACK,
        provider_input={"packet": {"schema_version": "cpu-large-proof/v1"}},
        current_image_data_url=data_url,
    )
    assert 4 * 1024 * 1024 < request.byte_count < 8 * 1024 * 1024
    return cast(dict[str, Any], json.loads(request.canonical_bytes))


def _large_failed_terminal(request: dict[str, Any]) -> dict[str, Any]:
    request_raw = production_driver_module.canonical_json_bytes(cast(Any, request))
    proof: dict[str, Any] = {
        "provider_request": request,
        "provider_request_byte_count": len(request_raw),
        "provider_request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "schema_version": "mobileworld.runtime.sentinel-r2.4-live-rubric-request-proof/v1",
    }
    terminal: dict[str, Any] = {
        "attempt_journal_failure_code": None,
        "kind": "FAILED",
        "live_attempt_receipt_sha256s": [],
        "live_attempt_receipts": [],
        "receipt": {
            "pre_provider": {"restricted_stage_projection": {"rubric_request_proofs": [proof]}}
        },
        "receipt_sha256": "1" * 64,
    }
    terminal["canonical_evidence_sha256"] = _journal_sha("production-unit-terminal-audit", terminal)
    return terminal


def _large_unit_journal_port(
    tmp_path: Path,
    request: dict[str, Any],
) -> tuple[
    production_driver_module._ProductionFixedExecutionPortV1,
    production_driver_module._ProductionUnitStateV1,
    production_driver_module._SmokeInvocationV1,
    production_audit_module.ExternalProductionRuntimeAuditSinkV1,
]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    audit_sink = production_audit_module.ExternalProductionRuntimeAuditSinkV1(
        tmp_path / "owner-only-audit"
    )
    terminal = _large_failed_terminal(request)
    now = time.monotonic_ns()
    plan = _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL)
    invocation = production_driver_module._SmokeInvocationV1(
        manifest_sha256="a" * 64,
        run_id="large-proof-run",
        source_commit="b" * 40,
        host=PilotHostV1.QWEN3_VL,
        sequence_index=0,
        case=plan.cases[0],
        actor_resource_sha256="c" * 64,
        history_policy_stage_sha256="d" * 64,
        deadline_monotonic_ns=now + 20_000_000_000,
        cleanup_deadline_monotonic_ns=now + 30_000_000_000,
        authority_deadline_monotonic_ns=now + 30_000_000_000,
        attempt_termination_upper_bound_ns=0,
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    state = production_driver_module._ProductionUnitStateV1(
        unit_id=port._unit_id(invocation),
        host=invocation.host,
        task_name=invocation.case.task_id,
        deadline_monotonic_ns=invocation.deadline_monotonic_ns,
        cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
        authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
        attempt_termination_upper_bound_ns=invocation.attempt_termination_upper_bound_ns,
        environment=None,
        observation=None,
        terminal_audit_journal=[cast(Any, terminal)],
    )
    object.__setattr__(port, "_audit_sink", audit_sink)
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_units", {state.unit_id: state})
    object.__setattr__(port, "_unit_journals", {})
    object.__setattr__(port, "_unpublished_unit_evidence", {})
    return port, state, invocation, audit_sink


def _large_validated_smoke_case(
    tmp_path: Path,
    request: dict[str, Any],
) -> tuple[
    production_driver_module.SmokeCaseEvidenceV1,
    bytes,
    production_driver_module._ValidatedUnitJournalReferenceV1,
    Path,
]:
    port, state, invocation, audit_sink = _large_unit_journal_port(tmp_path, request)
    state.resource_dispatch_journal.append(
        {
            "schema_version": "cpu-shared-dispatch-attestation/v1",
            "value": {
                "gpu_index": 5,
                "status": "PASSED",
                "user": "cpu-owner",
            },
        }
    )
    journal_raw = port._unit_journal_snapshot(state)
    validated = port._validated_smoke_unit_journal_reference(
        journal_raw, expected_unit_id=state.unit_id
    )
    assert validated is not None
    reference = cast(dict[str, Any], json.loads(journal_raw))
    blob = cast(dict[str, Any], reference["blob"])
    blob_path = audit_sink.root / cast(str, blob["safe_locator"])
    decision = _semantic_decision(actor_call_index=1, rubric_calls=2, history_policy_calls=1)
    evidence = production_driver_module.SmokeCaseEvidenceV1(
        manifest_sha256=invocation.manifest_sha256,
        run_id=invocation.run_id,
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=invocation.host,
        sequence_index=invocation.sequence_index,
        case_id=invocation.case.case_id,
        task_id=invocation.case.task_id,
        mode=invocation.case.mode,
        actor_resource_sha256=invocation.actor_resource_sha256,
        history_policy_stage_sha256=invocation.history_policy_stage_sha256,
        request_fixture_sha256=invocation.case.request_fixture_sha256,
        request_fixture_byte_count=invocation.case.request_fixture_byte_count,
        decision=decision,
        cleanup_receipt_sha256="e" * 64,
        unit_journal_preimage=journal_raw,
        unit_journal_sha256=hashlib.sha256(journal_raw).hexdigest(),
        unit_journal_validated_reference=validated,
        census=production_driver_module._sum_census((decision.census,)),
    )
    return evidence, journal_raw, validated, blob_path


def _configure_large_cleanup_state(
    *,
    port: production_driver_module._ProductionFixedExecutionPortV1,
    state: production_driver_module._ProductionUnitStateV1,
    tmp_path: Path,
    events: list[str],
) -> None:
    class _InitializedEnvironment:
        is_initialized = True

        @staticmethod
        def request_deadline_scope(_: int) -> object:
            return nullcontext()

        @staticmethod
        def tear_down_task_if_initialized(
            _: str, *, dispatch_started: object
        ) -> CleanupTaskTeardownResultV1:
            assert callable(dispatch_started)
            cast(Any, dispatch_started)()
            events.append("teardown")
            return CleanupTaskTeardownResultV1(
                status=CleanupTaskTeardownStatusV1.SUCCEEDED,
                message="closed",
                request_dispatched=True,
            )

        @staticmethod
        def close() -> None:
            events.append("environment-close")

    manifest_path = tmp_path / "collector-manifest.json"
    manifest_path.write_bytes(b"{}")

    def finalize_collector(**_: object) -> Path:
        events.append("collector-finalize")
        return manifest_path

    state.environment = cast(Any, _InitializedEnvironment())
    state.agent = cast(
        Any,
        SimpleNamespace(
            done=lambda: events.append("agent-done"),
            openai_client=SimpleNamespace(close=lambda: events.append("client-close")),
            get_total_token_usage=lambda: {},
        ),
    )
    state.task_binding = cast(
        Any,
        SimpleNamespace(
            capture=SimpleNamespace(
                capture_complete=True,
                end_task=lambda **_: events.append("collector-end-task"),
            ),
            metadata=SimpleNamespace(task_run_id="large-proof-task-run"),
        ),
    )
    state.lifecycle = cast(
        Any,
        SimpleNamespace(
            finish_task_attempt=lambda **_: events.append("collector-finish-attempt"),
            finalize=finalize_collector,
        ),
    )
    object.__setattr__(
        port,
        "_resource_lifecycle",
        SimpleNamespace(
            require_dispatch=lambda *_, **__: events.append("cleanup-dispatch-authorized")
        ),
    )


def _assert_full_large_failure_evidence(
    evidence: dict[str, Any],
    *,
    request: dict[str, Any],
    publication_failure_code: str,
    terminal_kind: str = "FAILED",
) -> None:
    terminals = cast(list[dict[str, Any]], evidence["terminal_audit_records"])
    assert [terminal["kind"] for terminal in terminals] == [terminal_kind]
    assert "actor_completed_receipt" not in evidence
    proof = terminals[0]["receipt"]["pre_provider"]["restricted_stage_projection"][
        "rubric_request_proofs"
    ][0]
    assert proof["provider_request"] == request
    request_raw = production_driver_module.canonical_json_bytes(cast(Any, request))
    assert proof["provider_request_byte_count"] == len(request_raw)
    assert proof["provider_request_sha256"] == hashlib.sha256(request_raw).hexdigest()

    publication = cast(dict[str, Any], evidence["evidence_publication_failure"])
    assert publication["publication_failure_code"] == publication_failure_code
    assert publication["status"] in {
        "PUBLICATION_FAILED_IN_MEMORY_RECOVERED",
        "PUBLICATION_FAILURE_DURABLY_RECOVERED",
    }
    assert evidence["evidence_publication_failure_code"] == publication_failure_code
    full_projection = {
        name: evidence[name]
        for name in (
            "cleanup_recovery_outcome",
            "cleanup_recovery_outcome_sha256",
            "completed_decisions",
            "completed_decisions_sha256",
            "resource_dispatch_records",
            "resource_dispatch_records_sha256",
            "terminal_audit_records",
            "terminal_audit_records_sha256",
            "run_fatal_state",
            "run_fatal_state_sha256",
            "unit_deadline",
            "unit_id",
        )
    }
    full_raw = production_driver_module.canonical_json_bytes(cast(Any, full_projection))
    assert len(full_raw) == publication["full_unit_evidence_byte_count"]
    assert hashlib.sha256(full_raw).hexdigest() == publication["full_unit_evidence_sha256"]
    binding = dict(publication)
    claimed_binding_sha256 = binding.pop("binding_sha256")
    assert claimed_binding_sha256 == _journal_sha(
        "production-unit-evidence-publication-failure", binding
    )


def test_large_legal_request_proof_is_atomically_blobbed_and_cleanup_proceeds(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
) -> None:
    port, state, invocation, audit_sink = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )

    journal_reference_raw = port._unit_journal_snapshot(state)
    reference = cast(dict[str, Any], json.loads(journal_reference_raw))
    blob = cast(dict[str, Any], reference["blob"])
    blob_path = audit_sink.root / cast(str, blob["safe_locator"])

    assert len(journal_reference_raw) < 4 * 1024 * 1024
    assert reference["storage"] == "OWNER_ONLY_CONTENT_ADDRESSED_BLOB"
    assert blob["byte_count"] > 4 * 1024 * 1024
    assert blob_path.is_file() and not blob_path.is_symlink()
    assert blob_path.stat().st_mode & 0o777 == 0o600
    assert hashlib.sha256(blob_path.read_bytes()).hexdigest() == blob["sha256"]

    cleanup = port.cleanup_unit(invocation)

    assert len(cleanup.cleanup_receipt_sha256) == 64
    assert state.unit_id not in port._units
    assert port._unit_journals[state.unit_id] == journal_reference_raw
    recovered = cast(dict[str, Any], port.failure_evidence_for_unit(invocation))
    recovered_proof = recovered["terminal_audit_records"][0]["receipt"]["pre_provider"][
        "restricted_stage_projection"
    ]["rubric_request_proofs"][0]
    assert recovered_proof["provider_request"] == large_legal_rubric_provider_request
    assert recovered_proof["provider_request_sha256"] == canonical_sha256(
        cast(Any, large_legal_rubric_provider_request)
    )


def test_large_smoke_case_cas_reference_is_read_back_and_durably_bound(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
) -> None:
    evidence, journal_raw, validated, blob_path = _large_validated_smoke_case(
        tmp_path, large_legal_rubric_provider_request
    )

    durable = production_driver_module._smoke_case_evidence_projection(evidence)
    binding = cast(dict[str, Any], durable["unit_journal_validated_reference"])
    reference = cast(dict[str, Any], json.loads(journal_raw))

    assert blob_path.is_file()
    assert binding["validation_status"] == "EXACT_OWNER_ONLY_READBACK"
    assert binding["expected_unit_id"] == "smoke:QWEN3_VL:OFF"
    assert binding["reference_preimage_sha256"] == hashlib.sha256(journal_raw).hexdigest()
    assert binding["reference_sha256"] == reference["reference_sha256"]
    assert binding["blob"]["byte_count"] > 4 * 1024 * 1024
    assert binding["blob"]["sha256"] == hashlib.sha256(blob_path.read_bytes()).hexdigest()
    assert binding["resource_dispatch_record_count"] == 1
    assert binding == production_driver_module._validated_unit_journal_reference_projection(
        validated
    )


def test_empty_dispatch_blob_is_valid_recovery_but_not_success_evidence(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
) -> None:
    port, state, invocation, _ = _large_unit_journal_port(
        tmp_path,
        large_legal_rubric_provider_request,
    )
    journal_raw = port._unit_journal_snapshot(state)
    validated = port._validated_smoke_unit_journal_reference(
        journal_raw,
        expected_unit_id=state.unit_id,
    )
    assert validated is not None
    assert validated.resource_dispatch_record_count == 0

    decision = _semantic_decision(actor_call_index=1, rubric_calls=2, history_policy_calls=1)
    with pytest.raises(ProductionDriverError) as raised:
        production_driver_module.SmokeCaseEvidenceV1(
            manifest_sha256=invocation.manifest_sha256,
            run_id=invocation.run_id,
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=invocation.host,
            sequence_index=invocation.sequence_index,
            case_id=invocation.case.case_id,
            task_id=invocation.case.task_id,
            mode=invocation.case.mode,
            actor_resource_sha256=invocation.actor_resource_sha256,
            history_policy_stage_sha256=invocation.history_policy_stage_sha256,
            request_fixture_sha256=invocation.case.request_fixture_sha256,
            request_fixture_byte_count=invocation.case.request_fixture_byte_count,
            decision=decision,
            cleanup_receipt_sha256="e" * 64,
            unit_journal_preimage=journal_raw,
            unit_journal_sha256=hashlib.sha256(journal_raw).hexdigest(),
            unit_journal_validated_reference=validated,
            census=production_driver_module._sum_census((decision.census,)),
        )

    assert raised.value.code == "INVALID_UNIT_JOURNAL"
    assert str(raised.value) == (
        "INVALID_UNIT_JOURNAL: smoke unit journal lacks its dispatch census"
    )


@pytest.mark.parametrize("mutation", ("forged-minimal", "wrong-unit", "unsafe-locator"))
def test_large_smoke_case_rejects_unvalidated_or_malformed_cas_reference(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    mutation: str,
) -> None:
    evidence, journal_raw, validated, _ = _large_validated_smoke_case(
        tmp_path, large_legal_rubric_provider_request
    )
    reference = cast(dict[str, Any], json.loads(journal_raw))
    validation: object = validated
    if mutation == "forged-minimal":
        reference = {"storage": "OWNER_ONLY_CONTENT_ADDRESSED_BLOB"}
        validation = None
    elif mutation == "wrong-unit":
        reference["unit_id"] = "smoke:MAI_UI:OFF"
    else:
        cast(dict[str, Any], reference["blob"])["safe_locator"] = "../escaped.json"
    if mutation != "forged-minimal":
        without_hash = dict(reference)
        del without_hash["reference_sha256"]
        reference["reference_sha256"] = canonical_sha256(cast(Any, without_hash))
    mutated_raw = production_driver_module.canonical_json_bytes(cast(Any, reference))

    with pytest.raises(ProductionDriverError):
        replace(
            evidence,
            unit_journal_preimage=mutated_raw,
            unit_journal_sha256=hashlib.sha256(mutated_raw).hexdigest(),
            unit_journal_validated_reference=cast(Any, validation),
        )


@pytest.mark.parametrize("admission_phase", ("construction", "projection"))
@pytest.mark.parametrize("mutation", ("missing", "content"))
def test_large_smoke_case_cas_readback_fails_closed_after_blob_change(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    admission_phase: str,
    mutation: str,
) -> None:
    evidence, _, _, blob_path = _large_validated_smoke_case(
        tmp_path, large_legal_rubric_provider_request
    )
    if mutation == "missing":
        blob_path.unlink()
        expected_code = "UNIT_EVIDENCE_BLOB_MISSING"
    else:
        with blob_path.open("r+b") as handle:
            handle.write(b"X")
            handle.flush()
            os.fsync(handle.fileno())
        expected_code = "UNIT_EVIDENCE_BLOB_INVALID"

    with pytest.raises(ProductionDriverError) as raised:
        if admission_phase == "construction":
            replace(evidence)
        else:
            production_driver_module._smoke_case_evidence_projection(evidence)

    assert raised.value.code == expected_code


@pytest.mark.parametrize("mutation", ("content", "mode"))
def test_large_unit_journal_readback_rejects_blob_tamper(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    mutation: str,
) -> None:
    port, state, invocation, audit_sink = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )
    journal_reference_raw = port._unit_journal_snapshot(state)
    reference = cast(dict[str, Any], json.loads(journal_reference_raw))
    blob = cast(dict[str, Any], reference["blob"])
    blob_path = audit_sink.root / cast(str, blob["safe_locator"])
    port._unit_journals[state.unit_id] = journal_reference_raw
    port._units.clear()
    if mutation == "content":
        with blob_path.open("r+b") as handle:
            handle.write(b"X")
            handle.flush()
            os.fsync(handle.fileno())
    else:
        blob_path.chmod(0o640)

    with pytest.raises(ProductionDriverError) as raised:
        port.failure_evidence_for_unit(invocation)

    assert raised.value.code == "UNIT_EVIDENCE_BLOB_INVALID"


@pytest.mark.parametrize("mutation", ("bool-byte-count", "path-traversal"))
def test_large_unit_journal_rejects_rehashed_unsafe_reference(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    mutation: str,
) -> None:
    port, state, invocation, _ = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )
    reference = cast(dict[str, Any], json.loads(port._unit_journal_snapshot(state)))
    blob = cast(dict[str, Any], reference["blob"])
    if mutation == "bool-byte-count":
        blob["byte_count"] = True
    else:
        blob["safe_locator"] = "../escaped-unit-evidence.json"
    reference_without_hash = dict(reference)
    del reference_without_hash["reference_sha256"]
    reference["reference_sha256"] = canonical_sha256(cast(Any, reference_without_hash))
    port._unit_journals[state.unit_id] = production_driver_module.canonical_json_bytes(
        cast(Any, reference)
    )
    port._units.clear()

    with pytest.raises(ProductionDriverError) as raised:
        port.failure_evidence_for_unit(invocation)

    assert raised.value.code == "UNIT_EVIDENCE_BLOB_REFERENCE_INVALID"


def test_large_unit_journal_rejects_content_address_collision_without_overwrite(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
) -> None:
    port, state, _, first_sink = _large_unit_journal_port(
        tmp_path / "first", large_legal_rubric_provider_request
    )
    first_reference = cast(dict[str, Any], json.loads(port._unit_journal_snapshot(state)))
    first_blob = cast(dict[str, Any], first_reference["blob"])
    assert (first_sink.root / cast(str, first_blob["safe_locator"])).is_file()
    (tmp_path / "second").mkdir()
    second_sink = production_audit_module.ExternalProductionRuntimeAuditSinkV1(
        tmp_path / "second" / "owner-only-audit"
    )
    collision_path = second_sink.root / cast(str, first_blob["safe_locator"])
    with collision_path.open("wb") as handle:
        handle.write(b"X")
        handle.truncate(cast(int, first_blob["byte_count"]))
        handle.flush()
        os.fsync(handle.fileno())
    collision_path.chmod(0o600)
    object.__setattr__(port, "_audit_sink", second_sink)

    with pytest.raises(ProductionDriverError) as raised:
        port._unit_journal_snapshot(state)

    assert raised.value.code == "UNIT_EVIDENCE_BLOB_COLLISION"
    assert collision_path.read_bytes()[:1] == b"X"


def _large_off_pre_provider(
    request: dict[str, Any],
) -> production_audit_module.ProductionRuntimeAuditPreProviderV1:
    base = _audit_commit_failure_recovery(action_executed=False).pre_provider
    terminal = _large_failed_terminal(request)
    proof = terminal["receipt"]["pre_provider"]["restricted_stage_projection"][
        "rubric_request_proofs"
    ][0]
    request_sha256 = canonical_sha256(cast(Any, request))
    restricted: dict[str, Any] = {
        "kind": "OFF_NO_SEMANTIC_WORK",
        "raw_request": request,
        "rubric_request_proofs": [proof],
    }
    return replace(
        base,
        logical_call_id="large-admission-fault-logical-call-1",
        raw_request_sha256=request_sha256,
        candidate_request_sha256=request_sha256,
        final_request_sha256=request_sha256,
        restricted_stage_projection=restricted,
        restricted_stage_projection_sha256=canonical_sha256(cast(Any, restricted)),
        _seal=production_audit_module._PRE_PROVIDER_SEAL,
    )


@pytest.mark.parametrize(
    ("failure_stage", "expected_admission_stage", "expected_blob_code"),
    (
        (
            "root_open",
            production_audit_module.ProductionRuntimeAuditAdmissionStageV1.ROOT_OPEN,
            "UNIT_EVIDENCE_BLOB_ROOT_INVALID",
        ),
        (
            "write",
            production_audit_module.ProductionRuntimeAuditAdmissionStageV1.ADMISSION_WRITE,
            "UNIT_EVIDENCE_BLOB_PERSIST_FAILED",
        ),
        (
            "file_fsync",
            production_audit_module.ProductionRuntimeAuditAdmissionStageV1.ADMISSION_FILE_FSYNC,
            "UNIT_EVIDENCE_BLOB_PERSIST_FAILED",
        ),
    ),
)
def test_actual_audit_admission_and_large_recovery_share_persistent_fault(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_admission_stage: production_audit_module.ProductionRuntimeAuditAdmissionStageV1,
    expected_blob_code: str,
) -> None:
    port, state, invocation, audit_sink = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )
    state.terminal_audit_journal.clear()
    state.terminal_audit_sha256s.clear()
    audit = production_audit_module.ProductionRuntimeAuditV1(
        policy=None,
        sink=audit_sink,
    )
    pre_provider = _large_off_pre_provider(large_legal_rubric_provider_request)
    original_open_root = audit_sink._open_root
    original_write = os.write
    original_fsync = os.fsync

    def fail_root_open(*_: object, **__: object) -> int:
        raise OSError("injected persistent audit/recovery root-open failure")

    def fail_write(*_: object, **__: object) -> int:
        raise OSError("injected persistent audit/recovery write failure")

    def fail_fsync(*_: object, **__: object) -> None:
        raise OSError("injected persistent audit/recovery fsync failure")

    if failure_stage == "root_open":
        assert callable(original_open_root)
        monkeypatch.setattr(
            production_audit_module.ExternalProductionRuntimeAuditSinkV1,
            "_open_root",
            fail_root_open,
        )
    elif failure_stage == "write":
        assert callable(original_write)
        monkeypatch.setattr(production_audit_module.os, "write", fail_write)
    else:
        assert callable(original_fsync)
        monkeypatch.setattr(production_audit_module.os, "fsync", fail_fsync)

    with pytest.raises(production_audit_module.ProductionRuntimeAuditError) as admission_error:
        audit._admit_pre_provider(
            pre_provider,
            sentinel_receipt_sha256="f" * 64,
        )

    assert admission_error.value.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    admission_failure = audit.latest_admission_failure_receipt
    assert admission_failure is not None
    assert admission_failure.admission_stage is expected_admission_stage
    assert audit.pending_count == 0
    assert audit.latest_completed_receipt is None
    assert audit.latest_failure_receipt is None
    assert audit.latest_commit_failure_receipt is None
    state.runtime_audit = audit
    events: list[str] = []
    _configure_large_cleanup_state(
        port=port,
        state=state,
        tmp_path=tmp_path,
        events=events,
    )

    initial = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="DISPATCH",
            failure_code="AUDIT_PRE_PROVIDER_ADMISSION_FAILED",
        ),
    )
    _assert_full_large_failure_evidence(
        initial,
        request=large_legal_rubric_provider_request,
        publication_failure_code=expected_blob_code,
        terminal_kind="ADMISSION_OUTCOME_UNKNOWN",
    )
    assert initial["actor_admission_failure_receipt"]["pre_provider"] == (
        production_audit_module.production_runtime_audit_pre_provider_projection(pre_provider)
    )

    with pytest.raises(ProductionDriverError) as cleanup_error:
        port.cleanup_unit(invocation)

    assert cleanup_error.value.code == expected_blob_code
    assert events == [
        "cleanup-dispatch-authorized",
        "teardown",
        "environment-close",
        "agent-done",
        "client-close",
        "collector-end-task",
        "collector-finish-attempt",
        "collector-finalize",
    ]
    final = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code=expected_blob_code,
        ),
    )
    _assert_full_large_failure_evidence(
        final,
        request=large_legal_rubric_provider_request,
        publication_failure_code=expected_blob_code,
        terminal_kind="ADMISSION_OUTCOME_UNKNOWN",
    )
    assert final["cleanup_recovery_outcome"]["outcome"] == "SUCCEEDED"
    assert tuple(audit_sink.root.iterdir()) == ()
    port._release_unpublished_unit_evidence(invocation)
    assert port._unpublished_unit_evidence == {}


@pytest.mark.parametrize("failure_stage", ("root_open", "write", "file_fsync", "link"))
def test_large_unit_journal_publish_failure_is_typed_and_cleanup_still_runs(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    port, state, invocation, audit_sink = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )
    events: list[str] = []
    _configure_large_cleanup_state(
        port=port,
        state=state,
        tmp_path=tmp_path,
        events=events,
    )

    def fail_io(*_: object, **__: object) -> None:
        raise OSError("injected atomic publication failure")

    if failure_stage == "root_open":
        monkeypatch.setattr(
            production_audit_module.ExternalProductionRuntimeAuditSinkV1,
            "_open_root",
            fail_io,
        )
    elif failure_stage == "write":
        monkeypatch.setattr(
            production_driver_module._ProductionFixedExecutionPortV1,
            "_write_unit_evidence_blob",
            staticmethod(fail_io),
        )
    elif failure_stage == "file_fsync":
        monkeypatch.setattr(production_driver_module.os, "fsync", fail_io)
    else:
        monkeypatch.setattr(production_driver_module.os, "link", fail_io)

    recovery_failure = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="DISPATCH",
            failure_code="RUN_FATAL_TERMINATION_UNCONFIRMED",
        ),
    )
    expected_code = (
        "UNIT_EVIDENCE_BLOB_ROOT_INVALID"
        if failure_stage == "root_open"
        else "UNIT_EVIDENCE_BLOB_PERSIST_FAILED"
    )
    _assert_full_large_failure_evidence(
        recovery_failure,
        request=large_legal_rubric_provider_request,
        publication_failure_code=expected_code,
    )

    with pytest.raises(ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == expected_code
    assert tuple(audit_sink.root.iterdir()) == ()
    assert events == [
        "cleanup-dispatch-authorized",
        "teardown",
        "environment-close",
        "agent-done",
        "client-close",
        "collector-end-task",
        "collector-finish-attempt",
        "collector-finalize",
    ]
    assert state.unit_id in port._units
    final_failure = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code=expected_code,
        ),
    )
    _assert_full_large_failure_evidence(
        final_failure,
        request=large_legal_rubric_provider_request,
        publication_failure_code=expected_code,
    )
    assert final_failure["cleanup_recovery_outcome"]["outcome"] == "SUCCEEDED"
    assert state.unit_id in port._unpublished_unit_evidence
    port._release_unpublished_unit_evidence(invocation)
    assert port._unpublished_unit_evidence == {}


def test_large_unit_journal_fifo_collision_is_nonblocking_and_cleanup_still_runs(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, state, invocation, audit_sink = _large_unit_journal_port(
        tmp_path, large_legal_rubric_provider_request
    )
    events: list[str] = []
    _configure_large_cleanup_state(
        port=port,
        state=state,
        tmp_path=tmp_path,
        events=events,
    )
    full_raw = port._unit_journal_full_snapshot(state)
    reference = port._unit_evidence_blob_reference(unit_id=state.unit_id, raw=full_raw)
    blob = cast(dict[str, Any], reference["blob"])
    locator = cast(str, blob["safe_locator"])
    fifo_path = audit_sink.root / locator
    os.mkfifo(fifo_path, 0o600)
    original_open = os.open
    observed_fifo_open_flags: list[int] = []

    def guarded_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == locator:
            observed_fifo_open_flags.append(flags)
            assert flags & os.O_NONBLOCK
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(production_driver_module.os, "open", guarded_open)

    initial = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="DISPATCH",
            failure_code="RUN_FATAL_TERMINATION_UNCONFIRMED",
        ),
    )
    _assert_full_large_failure_evidence(
        initial,
        request=large_legal_rubric_provider_request,
        publication_failure_code="UNIT_EVIDENCE_BLOB_COLLISION",
    )

    with pytest.raises(ProductionDriverError) as raised:
        port.cleanup_unit(invocation)

    assert raised.value.code == "UNIT_EVIDENCE_BLOB_COLLISION"
    assert observed_fifo_open_flags
    assert stat.S_ISFIFO(fifo_path.lstat().st_mode)
    assert events == [
        "cleanup-dispatch-authorized",
        "teardown",
        "environment-close",
        "agent-done",
        "client-close",
        "collector-end-task",
        "collector-finish-attempt",
        "collector-finalize",
    ]
    final = cast(
        dict[str, Any],
        port._recoverable_failure_evidence_for_unit(
            invocation,
            failure_phase="CLEANUP",
            failure_code="UNIT_EVIDENCE_BLOB_COLLISION",
        ),
    )
    _assert_full_large_failure_evidence(
        final,
        request=large_legal_rubric_provider_request,
        publication_failure_code="UNIT_EVIDENCE_BLOB_COLLISION",
    )
    assert final["cleanup_recovery_outcome"]["outcome"] == "SUCCEEDED"
    port._release_unpublished_unit_evidence(invocation)
    assert port._unpublished_unit_evidence == {}


@pytest.mark.parametrize(
    "fault_mode",
    (
        "one_time_link",
        "one_time_readback",
        "persistent_root_open",
        "persistent_write",
        "persistent_file_fsync",
        "persistent_link",
        "persistent_readback",
    ),
)
def test_smoke_outer_failure_transaction_keeps_large_proof_and_releases_memory(
    tmp_path: Path,
    large_legal_rubric_provider_request: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    fault_mode: str,
) -> None:
    audit_sink = production_audit_module.ExternalProductionRuntimeAuditSinkV1(
        tmp_path / "owner-only-audit"
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    object.__setattr__(port, "_audit_sink", audit_sink)
    object.__setattr__(port, "_config", SimpleNamespace(shutdown_grace_seconds=8))
    object.__setattr__(port, "_run_fatal_latch", build_production_run_fatal_latch_v1())
    object.__setattr__(port, "_lock", threading.RLock())
    object.__setattr__(port, "_units", {})
    object.__setattr__(port, "_unit_journals", {})
    object.__setattr__(port, "_unpublished_unit_evidence", {})
    events: list[str] = []
    created_states: list[production_driver_module._ProductionUnitStateV1] = []

    def fail_dispatch(
        exact_port: production_driver_module._ProductionFixedExecutionPortV1,
        invocation: production_driver_module._SmokeInvocationV1,
        _: object,
    ) -> None:
        assert exact_port is port
        state = production_driver_module._ProductionUnitStateV1(
            unit_id=port._unit_id(invocation),
            host=invocation.host,
            task_name=invocation.case.task_id,
            deadline_monotonic_ns=invocation.deadline_monotonic_ns,
            cleanup_deadline_monotonic_ns=invocation.cleanup_deadline_monotonic_ns,
            authority_deadline_monotonic_ns=invocation.authority_deadline_monotonic_ns,
            attempt_termination_upper_bound_ns=invocation.attempt_termination_upper_bound_ns,
            environment=None,
            observation=None,
            terminal_audit_journal=[
                cast(Any, _large_failed_terminal(large_legal_rubric_provider_request))
            ],
        )
        _configure_large_cleanup_state(
            port=port,
            state=state,
            tmp_path=tmp_path,
            events=events,
        )
        port._units[state.unit_id] = state
        created_states.append(state)
        raise ProductionDriverError(
            "RUN_FATAL_TERMINATION_UNCONFIRMED",
            "injected dispatch failure with a legal large request proof",
        )

    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "run_smoke_case",
        fail_dispatch,
    )
    link_calls = 0
    successful_readbacks = 0
    expected_publication_code: str
    if fault_mode == "persistent_root_open":

        def fail_root_open(*_: object, **__: object) -> None:
            raise OSError("injected persistent root-open failure")

        monkeypatch.setattr(
            production_audit_module.ExternalProductionRuntimeAuditSinkV1,
            "_open_root",
            fail_root_open,
        )
        expected_publication_code = "UNIT_EVIDENCE_BLOB_ROOT_INVALID"
    elif fault_mode == "persistent_write":

        def fail_blob_write(*_: object, **__: object) -> None:
            raise OSError("injected persistent blob write failure")

        monkeypatch.setattr(
            production_driver_module._ProductionFixedExecutionPortV1,
            "_write_unit_evidence_blob",
            staticmethod(fail_blob_write),
        )
        expected_publication_code = "UNIT_EVIDENCE_BLOB_PERSIST_FAILED"
    elif fault_mode == "persistent_file_fsync":
        original_fsync = os.fsync

        def fail_audit_root_fsync(descriptor: int) -> None:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if target == audit_sink.root or target.is_relative_to(audit_sink.root):
                raise OSError("injected persistent blob fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(production_driver_module.os, "fsync", fail_audit_root_fsync)
        expected_publication_code = "UNIT_EVIDENCE_BLOB_PERSIST_FAILED"
    elif fault_mode == "persistent_link":

        def fail_blob_link(*_: object, **__: object) -> None:
            raise OSError("injected persistent blob link failure")

        monkeypatch.setattr(production_driver_module.os, "link", fail_blob_link)
        expected_publication_code = "UNIT_EVIDENCE_BLOB_PERSIST_FAILED"
    elif fault_mode in {"one_time_readback", "persistent_readback"}:
        original_readback = (
            production_driver_module._ProductionFixedExecutionPortV1._read_unit_evidence_blob
        )

        def fail_post_link_readback(
            exact_port: production_driver_module._ProductionFixedExecutionPortV1,
            **kwargs: Any,
        ) -> bytes | None:
            nonlocal successful_readbacks
            result = original_readback(exact_port, **kwargs)
            if result is None:
                return None
            successful_readbacks += 1
            if fault_mode == "persistent_readback" or successful_readbacks == 1:
                raise ProductionDriverError(
                    "UNIT_EVIDENCE_BLOB_READ_FAILED",
                    "injected post-link exact-readback failure",
                )
            return result

        monkeypatch.setattr(
            production_driver_module._ProductionFixedExecutionPortV1,
            "_read_unit_evidence_blob",
            fail_post_link_readback,
        )
        expected_publication_code = "UNIT_EVIDENCE_BLOB_READ_FAILED"
    else:
        original_link = os.link

        def fail_link_once(*args: object, **kwargs: object) -> None:
            nonlocal link_calls
            link_calls += 1
            if link_calls == 1:
                raise OSError("injected one-time link failure")
            cast(Any, original_link)(*args, **kwargs)

        monkeypatch.setattr(production_driver_module.os, "link", fail_link_once)
        expected_publication_code = "UNIT_EVIDENCE_BLOB_PERSIST_FAILED"

    adapter = production_driver_module.FixedLiveSmokeAdapterV1(
        port, seal=production_driver_module._MODULE_SEAL
    )
    context = _context()
    with pytest.raises(ProductionDriverError):
        adapter.run_host(
            PilotHostV1.QWEN3_VL,
            _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
            _resources(tmp_path)[0],
            _live_stages(),
            context,
            _Lease(context.manifest_sha256),
        )

    outer_raw = adapter.failure_evidence_preimage(RunStageV1.QWEN_LIVE_SMOKE)
    assert outer_raw is not None
    outer = cast(dict[str, Any], json.loads(outer_raw))
    assert outer["status"] == "FAILED"
    unit_failure = cast(dict[str, Any], outer["unit_failure_evidence"])
    _assert_full_large_failure_evidence(
        unit_failure,
        request=large_legal_rubric_provider_request,
        publication_failure_code=expected_publication_code,
    )
    assert adapter.evidence_for_stage(RunStageV1.QWEN_LIVE_SMOKE) is None
    assert events == [
        "cleanup-dispatch-authorized",
        "teardown",
        "environment-close",
        "agent-done",
        "client-close",
        "collector-end-task",
        "collector-finish-attempt",
        "collector-finalize",
    ]
    assert created_states
    assert port._unpublished_unit_evidence == {}
    if fault_mode not in {"one_time_link", "one_time_readback"}:
        assert outer["failure_phase"] == "CLEANUP"
        assert port._unit_journals == {}
        assert created_states[0].unit_id in port._units
        assert unit_failure["cleanup_recovery_outcome"]["outcome"] == "SUCCEEDED"
    else:
        assert outer["failure_phase"] == "DISPATCH"
        if fault_mode == "one_time_link":
            assert link_calls >= 2
        else:
            assert successful_readbacks >= 3
        assert created_states[0].unit_id not in port._units
        assert len(port._unit_journals) == 1
        archived = next(iter(port._unit_journals.values()))
        assert len(archived) < 4 * 1024 * 1024
    if fault_mode in {"one_time_readback", "persistent_readback"}:
        blob_paths = tuple(audit_sink.root.glob("*.production-unit-evidence-blob.v1.json"))
        assert blob_paths
        assert all(path.is_file() and path.stat().st_mode & 0o777 == 0o600 for path in blob_paths)
        assert tuple(audit_sink.root.glob("*.tmp")) == ()

    output_root = tmp_path / "independent-executor-failure"
    output = live_executor_module.AtomicExternalOutputTransactionV1(
        output_root=output_root,
        repository_root=Path(__file__).resolve().parents[4],
        run_id="large-proof-run",
        source_commit="b" * 40,
        manifest_sha256="a" * 64,
    )
    output.fail(
        failed_stage=RunStageV1.QWEN_LIVE_SMOKE,
        failure_code="STAGE_ADAPTER_FAILED",
        stage_failure_evidence_preimage=outer_raw,
    )
    durable_failure_raw = (output_root / "failure.json").read_bytes()
    durable_failure = cast(dict[str, Any], json.loads(durable_failure_raw))
    assert durable_failure["status"] == "FAILED"
    assert durable_failure["stage_failure_evidence"] == outer
    assert durable_failure["stage_failure_evidence_sha256"] == hashlib.sha256(outer_raw).hexdigest()
    assert (output_root / "failure.json").stat().st_mode & 0o777 == 0o600


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


@pytest.mark.parametrize("drift", ("scope", "factory_config", "lifecycle_config"))
def test_shared_production_driver_cross_binds_scope_factory_and_lifecycle_config(
    tmp_path: Path,
    drift: str,
) -> None:
    runtime_config = _shared_runtime_config(tmp_path)
    confirmed = production_runtime_config_sha256(runtime_config)
    lifecycle_config = (
        replace(runtime_config, backend_port=18090)
        if drift == "lifecycle_config"
        else runtime_config
    )
    lifecycle = build_cpu_test_resource_lifecycle_adapter_v1(lifecycle_config)
    factory = object.__new__(production_driver_module.ProductionPostPreflightFactoryV1)
    object.__setattr__(
        factory,
        "_sequence_execution_scope",
        SimpleNamespace(value="R24_R25_FULL" if drift == "scope" else "R24_LIVE_SMOKE_ONLY"),
    )
    object.__setattr__(
        factory,
        "_runtime_config_sha256",
        "d" * 64 if drift == "factory_config" else confirmed,
    )

    with pytest.raises(ProductionDriverError) as raised:
        build_production_driver_v1(
            factory=factory,
            runtime_config=runtime_config,
            confirmed_runtime_config_sha256=confirmed,
            pricing=cast(Any, None),
            confirmed_pricing_sha256="e" * 64,
            production_audit_sink=cast(Any, None),
            resource_lifecycle=lifecycle,
        )
    assert raised.value.code == "SHARED_RESOURCE_AUTHORITY_MISMATCH"


def test_smoke_only_factory_cannot_select_legacy_independent_gpu_runtime(
    tmp_path: Path,
) -> None:
    runtime_config = _runtime_config(tmp_path)
    confirmed = production_runtime_config_sha256(runtime_config)
    lifecycle = build_cpu_test_resource_lifecycle_adapter_v1(runtime_config)
    factory = object.__new__(production_driver_module.ProductionPostPreflightFactoryV1)
    object.__setattr__(
        factory,
        "_sequence_execution_scope",
        SimpleNamespace(value="R24_LIVE_SMOKE_ONLY"),
    )
    object.__setattr__(factory, "_runtime_config_sha256", confirmed)

    with pytest.raises(ProductionDriverError) as raised:
        build_production_driver_v1(
            factory=factory,
            runtime_config=runtime_config,
            confirmed_runtime_config_sha256=confirmed,
            pricing=cast(Any, None),
            confirmed_pricing_sha256="e" * 64,
            production_audit_sink=cast(Any, None),
            resource_lifecycle=lifecycle,
        )
    assert raised.value.code == "SHARED_RESOURCE_AUTHORITY_MISMATCH"


def test_smoke_only_scope_rejects_direct_pilot_adapter_and_port_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = object.__new__(production_driver_module.ProductionPostPreflightFactoryV1)
    object.__setattr__(
        factory,
        "_sequence_execution_scope",
        SimpleNamespace(value="R24_LIVE_SMOKE_ONLY"),
    )
    port = object.__new__(production_driver_module._ProductionFixedExecutionPortV1)
    port._factory = factory
    adapter = production_driver_module.FixedPilotAdapterV1(
        port,
        seal=production_driver_module._MODULE_SEAL,
    )
    calls = {"pilot_snapshot": 0, "input_resolve": 0, "reset": 0, "dispatch": 0}

    def record_pilot_snapshot(value: object) -> Any:
        calls["pilot_snapshot"] += 1
        return value

    def record_input_resolve(*args: object, **kwargs: object) -> Any:
        calls["input_resolve"] += 1
        return None

    def record_reset(*args: object, **kwargs: object) -> None:
        calls["reset"] += 1

    def record_dispatch(*args: object, **kwargs: object) -> None:
        calls["dispatch"] += 1

    monkeypatch.setattr(production_driver_module, "_snapshot_pilot", record_pilot_snapshot)
    monkeypatch.setattr(
        production_driver_module,
        "resolve_pilot_task_inputs_v1",
        record_input_resolve,
    )
    monkeypatch.setattr(production_driver_module, "AndroidEnvClient", record_reset)
    monkeypatch.setattr(
        production_driver_module._ProductionFixedExecutionPortV1,
        "_require_resource_dispatch",
        record_dispatch,
    )

    attempts = (
        lambda: adapter.run_pilot(
            cast(Any, object()),
            cast(Any, object()),
            cast(Any, object()),
            _shared_context(),
            cast(Any, object()),
        ),
        lambda: port.prepare_pilot(cast(Any, object())),
        lambda: port.reset_pilot_cell(cast(Any, object())),
        lambda: port.run_pilot_cell(cast(Any, object()), cast(Any, object())),
    )
    for attempt in attempts:
        with pytest.raises(ProductionDriverError) as raised:
            attempt()
        assert raised.value.code == "PILOT_SCOPE_UNAUTHORIZED"

    assert calls == {"pilot_snapshot": 0, "input_resolve": 0, "reset": 0, "dispatch": 0}


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
