from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel import SentinelFallbackReason
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    canonical_json_bytes,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    live_attempt_receipt_projection,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
)
from mobile_world.runtime.sentinel.r2_4.live_run import R24R25RunAuthorityManifestV1
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION_V2,
    PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION,
    ProductionActorProviderAttemptStatusV1,
    ProductionActorProviderAttemptV1,
    ProductionRuntimeAuditPreProviderOutcomeV1,
    ProductionRuntimeAuditPreProviderStatusV1,
    production_actor_provider_attempt_projection,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    OFFICIAL_RESULT_EVALUATOR_ID_V1,
    PRODUCTION_PILOT_EVIDENCE_SCHEMA_VERSION_V2,
    ActorDecisionEvidenceV1,
    DriverCallCensusV1,
    DriverStageCensusV1,
    OfficialTaskResultEvidenceV1,
    PilotCellEvidenceV1,
    PilotStageEvidenceV1,
    pilot_stage_evidence_projection,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import LiveRubricError
from mobile_world.runtime.sentinel.r2_5 import analysis as analysis_module
from mobile_world.runtime.sentinel.r2_5 import analysis_artifact as analysis_artifact_module
from mobile_world.runtime.sentinel.r2_5.analysis import (
    PILOT_ANALYSIS_SCHEMA_VERSION,
    PilotAnalysisEvidenceCompletenessV1,
    PilotAnalysisProductionBindingsV1,
    PilotCellAnalysisV1,
    PilotClassificationV1,
    PilotGroupAnalysisV1,
    PilotMeasurementStatusV1,
    PilotOperationalCountV1,
    PilotOperationalMetricV1,
    PilotRateMetricV1,
    PilotRateSummaryV1,
    PilotTerminationReasonV1,
    R25AnalysisContractError,
    analyze_pilot_stage_v1,
    pilot_analysis_projection,
    pilot_analysis_sha256,
)
from mobile_world.runtime.sentinel.r2_5.analysis_artifact import (
    R25AnalysisArtifactError,
    analyze_pilot_artifacts_v1,
    write_pilot_analysis_artifact_v1,
)
from mobile_world.runtime.sentinel.r2_5.integrity_gate import (
    PostRunIntegrityAuthorityV1,
    ValidatedPostRunIntegrityArtifactV1,
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
    frozen_pilot_manifest_sha256,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _production_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> PilotAnalysisProductionBindingsV1:
    run_manifest = object.__new__(R24R25RunAuthorityManifestV1)
    object.__setattr__(run_manifest, "run_id", "analysis-run")
    capability = object.__new__(ValidatedPostRunIntegrityArtifactV1)
    for name, value in {
        "authority_manifest_sha256": _sha("run-authority"),
        "resolved_pilot_inputs_sha256": _sha("resolved-pilot-inputs"),
        "preflight_report_sha256": _sha("preflight"),
        "factory_binding_sha256": _sha("factory"),
        "backend_endpoint": "http://127.0.0.1:6960",
        "artifact_sha256": _sha("integrity"),
        "ordered_collector_integrity_root_sha256": _sha("ordered-collector-root"),
    }.items():
        object.__setattr__(capability, name, value)
    monkeypatch.setattr(
        analysis_module,
        "validated_post_run_integrity_projection_v1",
        lambda _capability: {},
    )
    return PilotAnalysisProductionBindingsV1(
        authority_manifest_sha256=_sha("run-authority"),
        run_id="analysis-run",
        run_manifest=run_manifest,
        resolved_pilot_inputs_sha256=_sha("resolved-pilot-inputs"),
        backend_endpoint="http://127.0.0.1:6960",
        preflight_report_sha256=_sha("preflight"),
        factory_binding_sha256=_sha("factory"),
        post_run_integrity_artifact_sha256=_sha("integrity"),
        ordered_collector_integrity_root_sha256=_sha("ordered-collector-root"),
        _validated_integrity=capability,
    )


def test_production_analysis_bindings_are_module_sealed() -> None:
    run_manifest = object.__new__(R24R25RunAuthorityManifestV1)
    object.__setattr__(run_manifest, "run_id", "analysis-run")
    with pytest.raises(PermissionError):
        PilotAnalysisProductionBindingsV1(
            authority_manifest_sha256=_sha("run-authority"),
            run_id="analysis-run",
            run_manifest=run_manifest,
            resolved_pilot_inputs_sha256=_sha("resolved-pilot-inputs"),
            backend_endpoint="http://127.0.0.1:6960",
            preflight_report_sha256=_sha("preflight"),
            factory_binding_sha256=_sha("factory"),
            post_run_integrity_artifact_sha256=_sha("integrity"),
            ordered_collector_integrity_root_sha256=_sha("ordered-collector-root"),
        )


@pytest.fixture(autouse=True)
def _accept_synthetic_provider_request_proofs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep metric fixtures synthetic while exercising the production proof calls.

    The two validators themselves have exhaustive R2.4 reconstruction tests.
    These R2.5 fixtures bind a compact synthetic proof envelope and assert that
    the public analysis path invokes the validators with the receipt-derived
    roots; focused negative tests below replace either validator with a typed
    failure to prove fail-closed composition.
    """

    monkeypatch.setattr(
        analysis_module,
        "validate_live_rubric_request_proof_projection_v1",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        analysis_module,
        "validate_live_history_policy_request_proof_projection_v1",
        lambda *args, **kwargs: None,
    )


def _manifest() -> FrozenPilotManifestV1:
    tasks = tuple(
        PilotTaskV1(
            task_id=f"Task{index:02d}",
            task_parameters_sha256=_sha(f"parameters:{index}"),
            reset_seed=1_000 + index,
        )
        for index in range(20)
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="analysis-fixture-20",
        frozen_at_utc="2026-09-03T12:00:00Z",
        task_manifest_path="/external/pilot/task-source.json",
        task_manifest_sha256=_sha("task-source"),
        task_manifest_byte_count=100,
        topology_comparison_artifact_path="/external/pilot/topology.json",
        topology_comparison_artifact_sha256=_sha("topology"),
        topology_comparison_artifact_byte_count=100,
        cohort_selection_artifact_path="/external/pilot/cohort-selection.json",
        cohort_selection_artifact_sha256=_sha("cohort"),
        cohort_selection_artifact_byte_count=100,
        cohort_selection_sha256=_sha("cohort"),
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
        max_steps_per_cell=2,
        per_cell_timeout_seconds=60,
        max_total_wall_time_seconds=10_000,
        max_total_actor_calls=160,
        max_total_openai_calls=160,
        max_total_cost_usd_micros=1_000_000,
    )


@dataclass(frozen=True)
class _BuiltDecision:
    evidence: ActorDecisionEvidenceV1
    detail_hash: str
    detail: JsonValue


def _live_attempt_projection(
    *, logical_call_id: str, role: LiveAttemptRoleV1, index: int, actor_request_sha256: str
) -> JsonValue:
    stage_sha256 = _sha(
        "history-openai-stage" if role is LiveAttemptRoleV1.HISTORY_POLICY else "rubric-stage"
    )
    receipt = LiveAttemptReceiptV1(
        attempt_id=f"live-{logical_call_id}-{role.value.lower()}-{index}",
        role=role,
        authority_sha256=_sha(f"live-authority:{logical_call_id}"),
        manifest_sha256=_sha("run-authority"),
        preflight_sha256=_sha("preflight"),
        case_execution_lease_sha256=_sha(f"lease:{logical_call_id}"),
        stage_sha256=stage_sha256,
        case_id=f"case-{logical_call_id}",
        logical_call_id=logical_call_id,
        actor_request_sha256=actor_request_sha256,
        request_sha256=_sha(f"live-request:{logical_call_id}:{index}"),
        transport_binding_sha256=_sha(f"transport:{logical_call_id}:{role.value}:{index}"),
        pricing_binding_sha256=_sha("pricing"),
        execution_kind=LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS,
        status=LiveAttemptStatusV1.COMPLETED,
        dispatch_count=1,
        response_envelope_sha256=_sha(f"live-response:{logical_call_id}:{index}"),
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
    )
    return cast(JsonValue, live_attempt_receipt_projection(receipt))


def _collector_locator(
    *, logical_call_id: str, attempt_index: int, event_type: str, snapshot: bool
) -> JsonValue:
    return {
        "run_id": f"collector-{logical_call_id}",
        "task_run_id": f"task-{logical_call_id}",
        "event_type": event_type,
        "event_id": f"event-{logical_call_id}-{attempt_index}-{event_type}",
        "event_sha256": _sha(f"event:{logical_call_id}:{attempt_index}:{event_type}"),
        "snapshot_blob": (
            {"sha256": _sha(f"blob:{logical_call_id}:{attempt_index}:{event_type}")}
            if snapshot
            else None
        ),
    }


def _actor_attempt_projection(
    *,
    logical_call_id: str,
    attempt_index: int,
    final_request_sha256: str,
    provider_response_sha256: str,
    succeeded: bool,
) -> JsonValue:
    attempt = ProductionActorProviderAttemptV1(
        attempt_id=f"actor-{logical_call_id}-{attempt_index}",
        attempt_index=attempt_index,
        sdk_arguments_sha256=final_request_sha256,
        final_request_sha256=final_request_sha256,
        collector_request_locator=_collector_locator(
            logical_call_id=logical_call_id,
            attempt_index=attempt_index,
            event_type="model_request",
            snapshot=True,
        ),
        collector_terminal_locator=_collector_locator(
            logical_call_id=logical_call_id,
            attempt_index=attempt_index,
            event_type="model_response" if succeeded else "model_attempt_failed",
            snapshot=succeeded,
        ),
        status=(
            ProductionActorProviderAttemptStatusV1.SUCCEEDED
            if succeeded
            else ProductionActorProviderAttemptStatusV1.FAILED
        ),
        provider_response_sha256=provider_response_sha256 if succeeded else None,
        response_id_sha256=_sha(f"response-id:{logical_call_id}:{attempt_index}")
        if succeeded
        else None,
        model_id_sha256=_sha(f"model:{logical_call_id}:{attempt_index}") if succeeded else None,
        finish_reason="stop" if succeeded else None,
        input_tokens=20 if succeeded else None,
        cached_input_tokens=5 if succeeded else None,
        output_tokens=4 if succeeded else None,
        total_tokens=24 if succeeded else None,
        latency_ns=1_000_000,
        failure_code=None if succeeded else "PROVIDER_EXCEPTION",
        schema_version=PRODUCTION_ACTOR_PROVIDER_ATTEMPT_SCHEMA_VERSION_V2,
    )
    return cast(JsonValue, production_actor_provider_attempt_projection(attempt))


def _detail(
    *,
    logical_call_id: str,
    actor_call_index: int,
    raw_sha256: str,
    final_sha256: str,
    exact_diff_sha256: str,
    provider_response_sha256: str,
    parsed_action: dict[str, JsonValue],
    executed: bool,
    status: str,
    fallback_reason: str | None,
    operation: str,
    archive: bool,
    provider_retry_failed: bool,
    parser_retry_failed: bool,
    action_execution_failed: bool,
) -> JsonValue:
    parsed_hash = canonical_sha256(cast(JsonValue, parsed_action))
    no_history = (
        status == "FALLBACK_ORIGINAL"
        and fallback_reason == SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE.value
    )
    semantic = status == "READY" or no_history
    live_attempt_preimages = (
        [
            _live_attempt_projection(
                logical_call_id=logical_call_id,
                role=role,
                index=index,
                actor_request_sha256=raw_sha256,
            )
            for index, role in enumerate(
                (
                    (LiveAttemptRoleV1.RUBRIC, LiveAttemptRoleV1.RUBRIC)
                    if actor_call_index == 1
                    else (LiveAttemptRoleV1.RUBRIC, LiveAttemptRoleV1.HISTORY_POLICY)
                ),
                1,
            )
        ]
        if semantic
        else []
    )
    raw_request: JsonValue = {"logical_call_id": logical_call_id, "request": "actor"}
    vertical_output: JsonValue = {"decisions": [{"operation": operation}]}
    path_relevance_output: JsonValue = {
        "records": [{"disposition": "ARCHIVE_SHADOW" if archive else "RETAIN"}]
    }
    restricted: JsonValue
    live_receipts = [canonical_sha256(projection) for projection in live_attempt_preimages]
    live_root = (
        None
        if not live_receipts
        else canonical_sha256(
            cast(
                JsonValue,
                {
                    "receipt_sha256s": live_receipts,
                    "schema_version": (
                        "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt-root/v1"
                    ),
                },
            )
        )
    )
    rubric_attempts = [
        cast(dict[str, JsonValue], item)
        for item in live_attempt_preimages
        if cast(dict[str, JsonValue], item)["role"] == LiveAttemptRoleV1.RUBRIC.value
    ]
    rubric_operations = (
        (("GENERATE", "TRACK") if actor_call_index == 1 else ("TRACK",)) if semantic else ()
    )
    tracking_packet_sha256 = _sha(f"tracking-packet:{logical_call_id}") if semantic else None
    rubric_proofs: list[JsonValue] = []
    for attempt_order, (attempt, rubric_operation) in enumerate(
        zip(rubric_attempts, rubric_operations, strict=True), 1
    ):
        rubric_proofs.append(
            {
                "attempt_authority_sha256": attempt["authority_sha256"],
                "attempt_constraint_binding": {
                    "synthetic_constraint": f"{logical_call_id}:{attempt_order}"
                },
                "attempt_id": attempt["attempt_id"],
                "attempt_order": attempt_order,
                "attempt_receipt_sha256": canonical_sha256(cast(JsonValue, attempt)),
                "attempt_role": LiveAttemptRoleV1.RUBRIC.value,
                "operation": rubric_operation,
                "provider_request_sha256": attempt["request_sha256"],
                "tracking_packet_sha256": (
                    tracking_packet_sha256 if rubric_operation == "TRACK" else None
                ),
            }
        )
    history_attempts = [
        cast(dict[str, JsonValue], item)
        for item in live_attempt_preimages
        if cast(dict[str, JsonValue], item)["role"] == LiveAttemptRoleV1.HISTORY_POLICY.value
    ]
    coordinator_evidence_packet_sha256 = (
        _sha(f"history-packet:{logical_call_id}") if history_attempts else None
    )
    history_proof: JsonValue = None
    if history_attempts:
        history_attempt = history_attempts[0]
        history_proof = {
            "attempt_authority_sha256": history_attempt["authority_sha256"],
            "attempt_id": history_attempt["attempt_id"],
            "attempt_receipt_sha256": canonical_sha256(cast(JsonValue, history_attempt)),
            "attempt_role": LiveAttemptRoleV1.HISTORY_POLICY.value,
            "constraint_binding": {"synthetic_constraint": logical_call_id},
            "coordinator_evidence_packet_sha256": coordinator_evidence_packet_sha256,
            "provider_request_sha256": history_attempt["request_sha256"],
        }
    coordinated_record: JsonValue = {
        "gpt56_evidence_packet_sha256": coordinator_evidence_packet_sha256,
        "logical_call_id": logical_call_id,
        "tracking_packet_sha256": tracking_packet_sha256,
    }
    if status == "READY":
        restricted = {
            "raw_request": raw_request,
            "extraction": {"host_id": "synthetic-host"},
            "history_ir": {"events": [], "host_id": "synthetic-host"},
            "vertical_output": vertical_output,
            "coordinated_record": coordinated_record,
            "rubric_generation_result": {"status": "ADMITTED"},
            "rubric_result": {"status": "ADMITTED"},
            "path_relevance_output": path_relevance_output,
            "render_result": {"candidate_request": raw_request, "exact_diff": []},
            "final_request": raw_request,
            "validator_result": {"status": "PASSED"},
            "live_call_binding": {"logical_call_id": logical_call_id},
            "live_attempt_receipts": live_attempt_preimages,
            "r2_4_rubric_call_receipts": [],
            "r2_4_rubric_request_proofs": rubric_proofs,
            "r2_4_history_policy_request_proof": history_proof,
            "r2_4_rubric_backend_extension": {"execution_scope": "OWNER_AUTHORIZED_LIVE"},
            "semantic_stage_projections_persisted": True,
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
    elif no_history:
        restricted = {
            "kind": "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL",
            "raw_request": raw_request,
            "final_request": raw_request,
            "sentinel_receipt": {"logical_call_id": logical_call_id},
            "coordinated_record": coordinated_record,
            "rubric_generation_result": {"status": "ADMITTED"},
            "rubric_result": {"status": "ADMITTED"},
            "path_relevance_output": path_relevance_output,
            "validator_result": {"status": "FALLBACK_ORIGINAL"},
            "live_failure_code": None,
            "live_call_binding": {"logical_call_id": logical_call_id},
            "live_attempt_receipts": live_attempt_preimages,
            "r2_4_rubric_call_receipts": [],
            "r2_4_rubric_request_proofs": rubric_proofs,
            "r2_4_history_policy_request_proof": None,
            "r2_4_rubric_backend_extension": {"execution_scope": "OWNER_AUTHORIZED_LIVE"},
            "semantic_stage_projections_persisted": True,
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
    elif status == "FALLBACK_ORIGINAL":
        restricted = {
            "kind": "FALLBACK_ORIGINAL",
            "raw_request": raw_request,
            "final_request": raw_request,
            "sentinel_receipt": {"logical_call_id": logical_call_id},
            "validator_result": {"status": "FALLBACK_ORIGINAL"},
            "live_failure_code": None,
            "live_call_binding": None,
            "live_attempt_receipts": [],
            "r2_4_rubric_call_receipts": [],
            "r2_4_rubric_request_proofs": [],
            "r2_4_history_policy_request_proof": None,
            "r2_4_rubric_backend_extension": None,
            "raw_request_persisted_in_owner_only_detail": True,
            "provider_response_via_collector_locator": True,
            "provider_reasoning_persisted": False,
        }
    else:
        validator_sha256 = _sha(f"validator:{logical_call_id}")
        restricted = {
            "kind": "OFF_NO_SEMANTIC_WORK",
            "raw_request": raw_request,
            "final_request": raw_request,
            "validator_result_sha256": validator_sha256,
            "semantic_text_persisted": False,
            "reasoning_persisted": False,
        }
    extraction = cast(dict[str, JsonValue], restricted).get("extraction")
    history_ir = cast(dict[str, JsonValue], restricted).get("history_ir")
    rubric_result = cast(dict[str, JsonValue], restricted).get("rubric_result")
    render_result = cast(dict[str, JsonValue], restricted).get("render_result")
    validator_result = cast(dict[str, JsonValue], restricted).get("validator_result")
    live_call_binding = cast(dict[str, JsonValue], restricted).get("live_call_binding")
    fallback_check = (
        "r2_4_no_history_r21_v1_compatibility"
        if no_history
        else "analysis_fixture_fallback"
        if status == "FALLBACK_ORIGINAL"
        else None
    )
    pre: JsonValue = {
        "schema_version": ("mobileworld.runtime.sentinel-r2.4-production-audit-pre-provider/v1"),
        "logical_call_id": logical_call_id,
        "host_id": "synthetic-host",
        "status": status,
        "outcome": (
            "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL"
            if no_history
            else "GENERIC_FALLBACK_ORIGINAL"
            if status == "FALLBACK_ORIGINAL"
            else status
        ),
        "configured_mode": "OFF" if status == "OFF" else "ACTIVE",
        "effective_mode": "ACTIVE" if status == "READY" else "OFF",
        "fallback_reason": fallback_reason,
        "fallback_check": fallback_check,
        "raw_request_sha256": raw_sha256,
        "extraction_sha256": canonical_sha256(extraction) if status == "READY" else None,
        "history_ir_sha256": canonical_sha256(history_ir) if status == "READY" else None,
        "codec_overlay_sha256": _sha(f"overlay:{logical_call_id}") if status == "READY" else None,
        "vertical_output_sha256": canonical_sha256(vertical_output) if status == "READY" else None,
        "rubric_result_sha256": canonical_sha256(rubric_result) if semantic else None,
        "path_relevance_output_sha256": (
            canonical_sha256(path_relevance_output) if semantic else None
        ),
        "render_result_sha256": canonical_sha256(render_result) if status == "READY" else None,
        "candidate_request_sha256": final_sha256,
        "final_request_sha256": final_sha256,
        "exact_diff_sha256": exact_diff_sha256,
        "validator_result_sha256": (
            canonical_sha256(validator_result)
            if validator_result is not None
            else cast(str, cast(dict[str, JsonValue], restricted)["validator_result_sha256"])
        ),
        "restricted_stage_projection": restricted,
        "restricted_stage_projection_sha256": canonical_sha256(restricted),
        "live_attempt_receipt_sha256s": live_receipts,
        "live_attempt_receipt_root_sha256": live_root,
        "case_execution_lease_sha256": (_sha(f"lease:{logical_call_id}") if semantic else None),
        "preflight_report_sha256": (_sha("preflight") if semantic else None),
        "factory_binding_sha256": _sha("factory") if semantic else None,
        "execution_authority_sha256": (_sha("run-authority") if semantic else None),
        "coordinated_record_sha256": (canonical_sha256(coordinated_record) if semantic else None),
        "live_call_binding_sha256": (canonical_sha256(live_call_binding) if semantic else None),
        "source_transport_binding_sha256": (
            cast(
                str,
                cast(dict[str, JsonValue], live_attempt_preimages[-1])["transport_binding_sha256"],
            )
            if semantic
            and any(
                cast(dict[str, JsonValue], item)["role"] == LiveAttemptRoleV1.HISTORY_POLICY.value
                for item in live_attempt_preimages
            )
            else None
        ),
        "pricing_binding_sha256": _sha("pricing") if semantic else None,
        "live_openai_calls": len(live_attempt_preimages),
        "live_cost_usd_micros": 10 * len(live_attempt_preimages),
        "live_cost_exact": True,
        "latencies_ns": {
            "evidence_snapshot": 0,
            "history_extract": 0,
            "rubric": 0,
            "policy": 0,
            "render": 0,
            "validator": 0,
            "pre_provider_total": 1,
        },
        "content_persistence": {
            "raw_request": True,
            "history_ir": status == "READY",
            "policy_output": status == "READY",
            "rubric_output": semantic,
            "rendered_request": status == "READY",
            "exact_diff": status == "READY",
            "validator_result": True,
            "provider_request": "COLLECTOR_EVENT_AND_BLOB_LOCATOR",
            "provider_response": "COLLECTOR_EVENT_AND_BLOB_LOCATOR",
            "credentials": False,
            "environment": False,
            "provider_reasoning": False,
        },
    }
    attempts: list[JsonValue] = []
    if provider_retry_failed:
        attempts.append(
            _actor_attempt_projection(
                logical_call_id=logical_call_id,
                attempt_index=len(attempts) + 1,
                final_request_sha256=final_sha256,
                provider_response_sha256=provider_response_sha256,
                succeeded=False,
            )
        )
    if parser_retry_failed:
        attempts.append(
            _actor_attempt_projection(
                logical_call_id=logical_call_id,
                attempt_index=len(attempts) + 1,
                final_request_sha256=final_sha256,
                provider_response_sha256=provider_response_sha256,
                succeeded=True,
            )
        )
    attempts.append(
        _actor_attempt_projection(
            logical_call_id=logical_call_id,
            attempt_index=len(attempts) + 1,
            final_request_sha256=final_sha256,
            provider_response_sha256=provider_response_sha256,
            succeeded=True,
        )
    )
    attempt_root = canonical_sha256(
        cast(
            JsonValue,
            {
                "attempt_sha256s": [canonical_sha256(item) for item in attempts],
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1"
                ),
            },
        )
    )
    return {
        "schema_version": PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION,
        "detail_id": f"detail-{logical_call_id}",
        "logical_call_id": logical_call_id,
        "pre_provider": pre,
        "pre_provider_sha256": canonical_sha256(pre),
        "sentinel_receipt_sha256": _sha(f"sentinel:{logical_call_id}"),
        "actor_provider_attempts": attempts,
        "actor_provider_attempt_root_sha256": attempt_root,
        "terminal": {
            "successful_provider_response_sha256": provider_response_sha256,
            "normalized_actor_output_sha256": _sha(f"normalized:{logical_call_id}"),
            "provider_response_persisted": False,
            "parser_input_sha256": _sha(f"parser-input:{logical_call_id}"),
            "parser_input_persisted": False,
            "parser_id": "synthetic-parser/v1",
            "parser_status": "PARSED",
            "parser_attempt_count": 2 if parser_retry_failed else 1,
            "parsed_action": parsed_action,
            "parsed_action_sha256": parsed_hash,
            "action_executed": executed and not action_execution_failed,
            "executed_action_sha256": (
                parsed_hash if executed and not action_execution_failed else None
            ),
            "latencies_ns": {
                "provider_total": 1,
                "parser": 1,
                "action_execution": 1,
                "total": 4,
            },
            "credentials_persisted": False,
            "environment_persisted": False,
            "reasoning_persisted": False,
        },
    }


def _decision(
    *,
    cell_index: int,
    call_index: int,
    arm: PilotArmV1,
    action_type: str,
    operation: str = "KEEP",
    edit: bool = False,
    fallback_reason: str | None = None,
    archive: bool = False,
    provider_retry_failed: bool = False,
    parser_retry_failed: bool = False,
    action_execution_failed: bool = False,
    action_index: int | None = None,
) -> _BuiltDecision:
    logical_call_id = f"analysis-{cell_index:03d}-{call_index}"
    raw = _sha(f"raw:{logical_call_id}")
    final = _sha(f"final:{logical_call_id}") if edit else raw
    exact_diff = _sha(f"diff:{logical_call_id}")
    provider_response = _sha(f"response:{logical_call_id}")
    action: dict[str, JsonValue] = {
        "action_type": action_type,
        "index": cell_index if action_index is None else action_index,
    }
    parsed_action_hash = canonical_sha256(cast(JsonValue, action))
    executed = action_type not in {"finished", "error_env", "unknown"}
    if arm is PilotArmV1.BASELINE:
        status = "OFF"
        fallback_reason = None
        no_history = False
    elif call_index == 1 and fallback_reason is None:
        status = "FALLBACK_ORIGINAL"
        fallback_reason = SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE.value
        no_history = True
        edit = False
        final = raw
    elif fallback_reason is not None:
        status = "FALLBACK_ORIGINAL"
        no_history = False
        edit = False
        final = raw
    else:
        status = "READY"
        no_history = False
    detail = _detail(
        logical_call_id=logical_call_id,
        actor_call_index=call_index,
        raw_sha256=raw,
        final_sha256=final,
        exact_diff_sha256=exact_diff,
        provider_response_sha256=provider_response,
        parsed_action=action,
        executed=executed,
        status=status,
        fallback_reason=fallback_reason,
        operation=operation,
        archive=archive,
        provider_retry_failed=provider_retry_failed,
        parser_retry_failed=parser_retry_failed,
        action_execution_failed=action_execution_failed,
    )
    detail_hash = canonical_sha256(detail)
    semantic = arm is PilotArmV1.JOINT_SENTINEL and (fallback_reason is None or no_history)
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    live_receipt_hashes = tuple(
        cast(str, item) for item in cast(list[JsonValue], pre["live_attempt_receipt_sha256s"])
    )
    rubric_call_count = 2 if semantic and call_index == 1 else int(semantic)
    history_call_count = int(semantic and call_index > 1)
    census = DriverCallCensusV1(
        actor_calls=1,
        offline_rubric_evaluations=0,
        rubric_openai_calls=rubric_call_count,
        history_policy_openai_calls=history_call_count,
        openai_calls=rubric_call_count + history_call_count,
        actor_actions=1 if executed and not action_execution_failed else 0,
        cost_usd_micros=10 * (rubric_call_count + history_call_count),
        wall_time_ms=10,
    )
    evidence = ActorDecisionEvidenceV1(
        logical_call_id=logical_call_id,
        actor_call_index=call_index,
        raw_request_sha256=raw,
        final_request_sha256=final,
        provider_request_sha256=final,
        provider_response_sha256=provider_response,
        exact_diff_sha256=exact_diff,
        pre_provider_status=(ProductionRuntimeAuditPreProviderStatusV1(cast(str, pre["status"]))),
        pre_provider_outcome=(
            ProductionRuntimeAuditPreProviderOutcomeV1(cast(str, pre["outcome"]))
        ),
        fallback_reason=(
            None if fallback_reason is None else SentinelFallbackReason(fallback_reason)
        ),
        fallback_check=cast(str | None, pre["fallback_check"]),
        preflight_report_sha256=_sha("preflight"),
        case_execution_lease_sha256=(_sha(f"lease:{logical_call_id}") if semantic else None),
        live_policy_factory_binding_sha256=_sha("factory"),
        live_policy_authority_sha256=(_sha("run-authority") if semantic else None),
        rubric_attempt_receipt_sha256s=(
            live_receipt_hashes[:rubric_call_count] if semantic else ()
        ),
        history_policy_attempt_receipt_sha256=(
            live_receipt_hashes[-1] if history_call_count else None
        ),
        actor_attempt_receipt_sha256=cast(str, detail["actor_provider_attempt_root_sha256"]),
        sentinel_receipt_sha256=_sha(f"sentinel:{logical_call_id}"),
        provider_attempt_receipt_sha256=cast(str, detail["actor_provider_attempt_root_sha256"]),
        runtime_audit_detail_sha256=detail_hash,
        parser_result_sha256=_sha(f"parser:{logical_call_id}"),
        parsed_action_sha256=parsed_action_hash,
        executed_action_sha256=(
            parsed_action_hash if executed and not action_execution_failed else None
        ),
        census=census,
    )
    return _BuiltDecision(evidence=evidence, detail_hash=detail_hash, detail=detail)


def _sum_stage_census(
    values: tuple[DriverCallCensusV1, ...], *, wall_time_ms: int
) -> DriverStageCensusV1:
    return DriverStageCensusV1(
        actor_calls=sum(item.actor_calls for item in values),
        offline_rubric_evaluations=sum(item.offline_rubric_evaluations for item in values),
        rubric_openai_calls=sum(item.rubric_openai_calls for item in values),
        history_policy_openai_calls=sum(item.history_policy_openai_calls for item in values),
        openai_calls=sum(item.openai_calls for item in values),
        actor_actions=sum(item.actor_actions for item in values),
        cost_usd_micros=sum(item.cost_usd_micros for item in values),
        wall_time_ms=wall_time_ms,
    )


def _evidence(
    manifest: FrozenPilotManifestV1,
) -> tuple[PilotStageEvidenceV1, dict[str, JsonValue]]:
    manifest_sha = _sha("run-authority")
    run_id = "analysis-run"
    policy_sha = _sha("policy-stage")
    details: dict[str, JsonValue] = {}
    cells: list[PilotCellEvidenceV1] = []
    for index, planned in enumerate(manifest.cells):
        first_operation = "KEEP"
        first_edit = False
        first_fallback: str | None = None
        first_archive = False
        first_retry = False
        first_parser_retry = False
        first_action_failure = False
        if planned.arm is PilotArmV1.JOINT_SENTINEL:
            selector = index % 8
            if selector == 1:
                first_operation = "DROP"
                first_edit = True
            elif selector == 3:
                first_operation = "KEEP_UNCERTAIN"
                first_parser_retry = True
                first_action_failure = True
            elif selector == 5:
                first_fallback = "UNSUPPORTED_HISTORY_FAMILY"
            elif selector == 7:
                first_retry = True
                first_archive = True
        first = _decision(
            cell_index=index,
            call_index=1,
            arm=planned.arm,
            action_type="click",
            operation=first_operation,
            edit=first_edit,
            fallback_reason=first_fallback,
            archive=first_archive,
            provider_retry_failed=first_retry,
            parser_retry_failed=first_parser_retry,
            action_execution_failed=False,
        )
        successful = index % 2 == 0
        if index == 0:
            # Exact consecutive duplicate executed-action hashes are a lower-
            # bound repeat that needs no semantic annotation.
            second = _decision(
                cell_index=index,
                call_index=2,
                arm=planned.arm,
                action_type="click",
                action_index=index,
            )
        else:
            second = _decision(
                cell_index=index,
                call_index=2,
                arm=planned.arm,
                action_type="click" if first_action_failure else "finished",
                edit=planned.arm is PilotArmV1.JOINT_SENTINEL and index == 1,
                operation="DROP" if index == 1 else "KEEP",
                action_execution_failed=first_action_failure,
            )
        built = (first, second)
        for item in built:
            details[item.detail_hash] = item.detail
        call_censuses = tuple(item.evidence.census for item in built)
        cell_census = _sum_stage_census(call_censuses, wall_time_ms=25)
        official_binding = f"official:{index}"
        score_ppm = 250_000 if index == 1 else 1_000_000 if successful else 0
        official = OfficialTaskResultEvidenceV1(
            task_id=planned.task_id,
            evaluator_id=OFFICIAL_RESULT_EVALUATOR_ID_V1,
            official_success_metric_id="mobileworld.core.eval-score-gt/v1",
            official_success_operator="GT",
            official_success_threshold_float_hex="0x1.fae147ae147aep-1",
            score_float_hex=(score_ppm / 1_000_000).hex(),
            score_ppm=score_ppm,
            successful=successful,
            result_payload_sha256=_sha(f"payload:{official_binding}"),
            reason_sha256=_sha(f"reason:{official_binding}"),
        )
        cells.append(
            PilotCellEvidenceV1(
                manifest_sha256=manifest_sha,
                run_id=run_id,
                sequence_index=index,
                task_id=planned.task_id,
                task_parameters_sha256=planned.task_parameters_sha256,
                reset_seed=planned.reset_seed,
                host=planned.host,
                arm=planned.arm,
                sentinel_mode=planned.sentinel_mode,
                actor_resource_sha256=_sha(f"actor:{planned.host.value}"),
                history_policy_stage_sha256=policy_sha,
                reset_receipt_sha256=_sha(f"reset:{index}"),
                effective_reset_state_sha256=_sha(f"state:{planned.task_id}"),
                decisions=tuple(item.evidence for item in built),
                official_result=official,
                cleanup_receipt_sha256=_sha(f"cleanup:{index}"),
                census=cell_census,
            )
        )
    stage_census = DriverStageCensusV1(
        actor_calls=sum(item.census.actor_calls for item in cells),
        offline_rubric_evaluations=sum(item.census.offline_rubric_evaluations for item in cells),
        rubric_openai_calls=sum(item.census.rubric_openai_calls for item in cells),
        history_policy_openai_calls=sum(item.census.history_policy_openai_calls for item in cells),
        openai_calls=sum(item.census.openai_calls for item in cells),
        actor_actions=sum(item.census.actor_actions for item in cells),
        cost_usd_micros=sum(item.census.cost_usd_micros for item in cells),
        wall_time_ms=sum(item.census.wall_time_ms for item in cells),
    )
    return (
        PilotStageEvidenceV1(
            manifest_sha256=manifest_sha,
            run_id=run_id,
            pilot_manifest_sha256=frozen_pilot_manifest_sha256(manifest),
            actor_resources_sha256=_sha("actor-matrix"),
            history_policy_stage_sha256=policy_sha,
            cells=tuple(cells),
            census=stage_census,
        ),
        details,
    )


def _metric(
    cell_or_group: PilotCellAnalysisV1 | PilotGroupAnalysisV1,
    metric: PilotRateMetricV1,
) -> PilotRateSummaryV1:
    return next(item for item in cell_or_group.call_rates if item.metric is metric)


def _operational(
    cell_or_group: PilotCellAnalysisV1 | PilotGroupAnalysisV1,
    metric: PilotOperationalMetricV1,
) -> PilotOperationalCountV1:
    return next(item for item in cell_or_group.operational_counts if item.metric is metric)


def test_analysis_has_exact_cell_group_and_missingness_denominators() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)

    analysis = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert len(analysis.cells) == 80
    assert len(analysis.host_arm_groups) == 4
    assert len(analysis.task_groups) == 20
    assert len(analysis.matched_pairs) == 40
    assert len(analysis.matched_host_comparisons) == 2
    assert analysis.overall.official_success.measured_denominator == 80
    assert analysis.overall.official_success.positive_count == 40
    assert analysis.overall.steps.total_steps == 160
    assert sum(item.cell_count for item in analysis.host_arm_groups) == 80
    assert all(item.cell_count == 4 for item in analysis.task_groups)
    assert analysis.matched_overall.pair_count == 40
    assert analysis.matched_overall.baseline_success_count == 40
    assert analysis.matched_overall.joint_success_count == 0
    assert analysis.matched_overall.joint_regressed_count == 40
    assert analysis.matched_overall.baseline_total_steps == 80
    assert analysis.matched_overall.joint_total_steps == 80
    assert analysis.matched_overall.joint_minus_baseline_total_steps == 0
    assert analysis.matched_pairs[0].baseline_score_ppm == 1_000_000
    assert analysis.matched_pairs[0].joint_score_ppm == 250_000
    assert analysis.matched_pairs[0].joint_minus_baseline_score_ppm == -750_000
    assert analysis.matched_overall.baseline_scores.score_count == 40
    assert analysis.matched_overall.baseline_scores.score_sum_ppm == 40_000_000
    assert analysis.matched_overall.baseline_scores.score_mean_ppm == 1_000_000
    assert analysis.matched_overall.joint_scores.score_sum_ppm == 250_000
    assert analysis.matched_overall.joint_scores.score_mean_ppm == 6_250
    assert analysis.matched_overall.joint_scores.minimum_score_ppm == 0
    assert analysis.matched_overall.joint_scores.maximum_score_ppm == 250_000
    assert analysis.matched_overall.joint_minus_baseline_scores.score_delta_sum_ppm == -39_750_000
    assert analysis.matched_overall.joint_minus_baseline_scores.score_delta_mean_ppm == -993_750
    assert analysis.overall.official_scores.score_count == 80
    assert analysis.overall.official_scores.score_sum_ppm == 40_250_000
    assert analysis.overall.official_scores.score_mean_ppm == 503_125

    overall_edit = _metric(analysis.overall, PilotRateMetricV1.EDIT)
    assert overall_edit.population_count == 160
    assert overall_edit.measured_denominator == 160
    assert overall_edit.missing_count == 0
    assert overall_edit.not_applicable_count == 0

    clean_false_edit = _metric(analysis.overall, PilotRateMetricV1.CLEAN_HISTORY_FALSE_EDIT)
    assert clean_false_edit.population_count == 160
    assert clean_false_edit.measured_denominator == 0
    assert clean_false_edit.missing_count == 80
    assert clean_false_edit.not_applicable_count == 80
    assert clean_false_edit.positive_count is None
    assert clean_false_edit.rate_ppm is None
    assert clean_false_edit.measurement_status is PilotMeasurementStatusV1.NOT_MEASURABLE

    assert analysis.overall.actor_provider_tokens.population_calls == 160
    assert analysis.overall.actor_provider_tokens.measured_call_denominator == 150
    assert analysis.overall.actor_provider_tokens.missing_call_count == 10
    assert analysis.overall.actor_provider_tokens.input_tokens == 3_200
    assert analysis.overall.actor_provider_tokens.cached_input_tokens == 800
    assert analysis.overall.sentinel_openai_tokens.measured_call_denominator == 80
    assert analysis.overall.sentinel_openai_tokens.not_applicable_call_count == 80
    assert analysis.overall.sentinel_openai_tokens.input_tokens == 1_400
    assert analysis.matched_overall.joint_sentinel_openai_tokens.measured_call_denominator == 80

    assert (
        _operational(
            analysis.overall, PilotOperationalMetricV1.PHYSICAL_ACTOR_ATTEMPT
        ).observed_count
        == 180
    )
    assert _operational(analysis.overall, PilotOperationalMetricV1.ACTOR_RETRY).observed_count == 20
    assert (
        _operational(analysis.overall, PilotOperationalMetricV1.PROVIDER_FAILURE).observed_count
        == 10
    )
    assert (
        _operational(analysis.overall, PilotOperationalMetricV1.PARSER_ATTEMPT).observed_count
        == 170
    )
    assert (
        _operational(analysis.overall, PilotOperationalMetricV1.PARSER_FAILURE).observed_count == 10
    )
    assert (
        _operational(analysis.overall, PilotOperationalMetricV1.ACTION_FAILURE).observed_count == 10
    )

    assert analysis.cells[0].repeated_action.classification is PilotClassificationV1.OBSERVED
    assert analysis.cells[0].termination_reason is PilotTerminationReasonV1.MAX_STEPS_EXHAUSTED
    assert analysis.cells[0].wrong_edit.classification is PilotClassificationV1.NOT_APPLICABLE
    assert analysis.cells[1].premature_stop.classification is PilotClassificationV1.OBSERVED
    assert analysis.cells[1].wrong_action.classification is PilotClassificationV1.NOT_MEASURABLE
    assert analysis.cells[1].wrong_edit.classification is PilotClassificationV1.NOT_MEASURABLE


def test_typed_and_canonical_projection_have_identical_hashable_analysis() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)

    typed = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)
    projected = analyze_pilot_stage_v1(
        manifest,
        cast(JsonValue, pilot_stage_evidence_projection(evidence)),
        audit_detail_projections=details,
    )

    assert pilot_analysis_projection(typed) == pilot_analysis_projection(projected)
    assert pilot_analysis_sha256(typed) == pilot_analysis_sha256(projected)
    assert len(pilot_analysis_sha256(typed)) == 64


def test_matched_pair_sequence_is_recomputed_from_frozen_cell_order() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    analysis = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    with pytest.raises(PermissionError):
        replace(analysis, matched_pairs=tuple(reversed(analysis.matched_pairs)))

    object.__setattr__(analysis, "matched_pairs", tuple(reversed(analysis.matched_pairs)))
    with pytest.raises(R25AnalysisContractError) as error:
        pilot_analysis_projection(analysis)

    assert error.value.code == "INVALID_MATCHED_PAIR"


def test_pilot_analysis_projection_matches_strict_schema_and_json_round_trip() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    analysis = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)
    projection = pilot_analysis_projection(analysis)
    schema_path = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff/schemas/r2_5/pilot_analysis.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    canonical_round_trip = json.loads(canonical_json_bytes(cast(JsonValue, projection)))
    validator.validate(canonical_round_trip)
    assert canonical_round_trip == projection
    assert canonical_round_trip["schema_version"] == PILOT_ANALYSIS_SCHEMA_VERSION
    assert hashlib.sha256(
        canonical_json_bytes(cast(JsonValue, canonical_round_trip))
    ).hexdigest() == (pilot_analysis_sha256(analysis))

    invalid = dict(canonical_round_trip)
    invalid["unexpected"] = True
    assert list(validator.iter_errors(invalid))

    invalid_nested = json.loads(canonical_json_bytes(cast(JsonValue, projection)))
    invalid_nested["overall"]["operational_counts"][0]["unexpected"] = True
    assert list(validator.iter_errors(invalid_nested))

    invalid_legacy_binding = json.loads(canonical_json_bytes(cast(JsonValue, projection)))
    invalid_legacy_binding["post_run_integrity_artifact_sha256"] = _sha("forged")
    assert list(validator.iter_errors(invalid_legacy_binding))

    invalid_production_binding = json.loads(canonical_json_bytes(cast(JsonValue, projection)))
    invalid_production_binding["evidence_completeness"] = "PRODUCTION_V2_INTEGRITY_BOUND"
    assert list(validator.iter_errors(invalid_production_binding))


def test_sealed_production_v2_analysis_binds_post_run_integrity_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    projection["schema_version"] = PRODUCTION_PILOT_EVIDENCE_SCHEMA_VERSION_V2
    bindings = _production_bindings(monkeypatch)
    monkeypatch.setattr(
        analysis_module,
        "validate_pilot_stage_durable_evidence_projection_v2",
        lambda value, **_kwargs: cast(dict[str, JsonValue], value),
    )
    validated_restricted_details: list[str] = []
    monkeypatch.setattr(
        analysis_module,
        "validate_production_runtime_audit_restricted_stage_projection_v1",
        lambda detail: validated_restricted_details.append(detail.logical_call_id),
    )

    analysis = analyze_pilot_stage_v1(
        manifest,
        cast(JsonValue, projection),
        audit_detail_projections=details,
        production_bindings=bindings,
    )

    assert (
        analysis.evidence_completeness
        is PilotAnalysisEvidenceCompletenessV1.PRODUCTION_V2_INTEGRITY_BOUND
    )
    assert (
        analysis.post_run_integrity_artifact_sha256 == bindings.post_run_integrity_artifact_sha256
    )
    assert (
        analysis.ordered_collector_integrity_root_sha256
        == bindings.ordered_collector_integrity_root_sha256
    )
    assert len(validated_restricted_details) == sum(len(cell.decisions) for cell in evidence.cells)


def test_missing_detail_is_missing_not_a_negative_or_dropped_call() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    missing_hash = evidence.cells[1].decisions[-1].runtime_audit_detail_sha256
    details.pop(missing_hash)

    analysis = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)
    cell = analysis.cells[1]

    assert cell.audit_detail_missing_count == 1
    assert cell.termination_reason is PilotTerminationReasonV1.UNKNOWN_MISSING_AUDIT_DETAIL
    fallback = _metric(cell, PilotRateMetricV1.FALLBACK)
    assert fallback.population_count == 2
    assert fallback.measured_denominator == 1
    assert fallback.missing_count == 1
    assert fallback.measurement_status is PilotMeasurementStatusV1.PARTIAL
    edit = _metric(cell, PilotRateMetricV1.EDIT)
    assert edit.measured_denominator == 1
    assert edit.missing_count == 1
    assert edit.measurement_status is PilotMeasurementStatusV1.PARTIAL
    assert cell.wrong_edit.classification is PilotClassificationV1.UNKNOWN
    physical = _operational(cell, PilotOperationalMetricV1.PHYSICAL_ACTOR_ATTEMPT)
    assert physical.population_logical_calls == 2
    assert physical.measured_logical_call_denominator == 1
    assert physical.missing_logical_call_count == 1
    assert physical.observed_count == 1
    assert physical.measurement_status is PilotMeasurementStatusV1.PARTIAL
    assert "AUDIT_DETAIL_UNAVAILABLE" in physical.reason_codes


def test_missing_edited_detail_cannot_be_counted_as_edit_or_nonedit() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    edited = evidence.cells[1].decisions[1]
    assert edited.raw_request_sha256 != edited.final_request_sha256
    details.pop(edited.runtime_audit_detail_sha256)

    analysis = analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)
    cell = analysis.cells[1]
    edit = _metric(cell, PilotRateMetricV1.EDIT)

    assert edit.population_count == 2
    assert edit.measured_denominator == 1
    assert edit.missing_count == 1
    assert edit.positive_count == 0
    assert edit.measurement_status is PilotMeasurementStatusV1.PARTIAL
    assert cell.wrong_edit.classification is PilotClassificationV1.UNKNOWN


def test_absent_details_make_semantic_numerators_and_tokens_unknown() -> None:
    manifest = _manifest()
    evidence, _ = _evidence(manifest)

    analysis = analyze_pilot_stage_v1(manifest, evidence)
    joint = next(
        item
        for item in analysis.host_arm_groups
        if item.host is PilotHostV1.QWEN3_VL and item.arm is PilotArmV1.JOINT_SENTINEL
    )
    fallback = _metric(joint, PilotRateMetricV1.FALLBACK)

    assert fallback.population_count == 40
    assert fallback.measured_denominator == 0
    assert fallback.missing_count == 40
    assert fallback.positive_count is None
    assert fallback.rate_ppm is None
    assert fallback.measurement_status is PilotMeasurementStatusV1.NOT_MEASURABLE
    assert joint.actor_provider_tokens.measured_call_denominator == 0
    assert joint.actor_provider_tokens.missing_call_count == 40
    assert joint.actor_provider_tokens.input_tokens is None
    assert joint.sentinel_openai_tokens.measured_call_denominator == 0
    assert joint.sentinel_openai_tokens.input_tokens is None
    provider_failures = _operational(joint, PilotOperationalMetricV1.PROVIDER_FAILURE)
    assert provider_failures.population_logical_calls == 40
    assert provider_failures.measured_logical_call_denominator == 0
    assert provider_failures.missing_logical_call_count == 40
    assert provider_failures.observed_count is None
    assert provider_failures.measurement_status is PilotMeasurementStatusV1.NOT_MEASURABLE


def test_partial_cell_projection_fails_instead_of_shrinking_denominator() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    assert isinstance(projection["cells"], list)
    projection["cells"] = projection["cells"][:-1]

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest, cast(JsonValue, projection), audit_detail_projections=details
        )

    assert error.value.code == "INCOMPLETE_CELL_MATRIX"


def test_official_success_cannot_replace_a_nonbinary_official_score() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    official = cast(dict[str, JsonValue], first_cell["official_result"])
    official["score_ppm"] = 750_000
    official["successful"] = True

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_OFFICIAL_RESULT"


def test_present_detail_must_hash_and_cross_bind_exactly() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    detail_hash = evidence.cells[0].decisions[0].runtime_audit_detail_sha256
    detail = cast(dict[str, JsonValue], details[detail_hash])
    detail["logical_call_id"] = "another-call"

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "AUDIT_DETAIL_HASH_MISMATCH"


@pytest.mark.parametrize(
    "field",
    (
        "actor_attempt_receipt_sha256",
        "provider_attempt_receipt_sha256",
        "sentinel_receipt_sha256",
    ),
)
def test_decision_receipts_must_cross_bind_the_rehashed_audit_detail(field: str) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    first_decision[field] = _sha(f"drift:{field}")

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "TRACE_BINDING_MISMATCH"


def test_final_attempt_response_must_bind_terminal_and_decision() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    attempts = cast(list[JsonValue], detail["actor_provider_attempts"])
    cast(dict[str, JsonValue], attempts[-1])["provider_response_sha256"] = _sha(
        "drift:provider-response"
    )
    attempt_root = canonical_sha256(
        cast(
            JsonValue,
            {
                "attempt_sha256s": [canonical_sha256(item) for item in attempts],
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.4-production-actor-attempt-root/v1"
                ),
            },
        )
    )
    detail["actor_provider_attempt_root_sha256"] = attempt_root
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    first_decision["actor_attempt_receipt_sha256"] = attempt_root
    first_decision["provider_attempt_receipt_sha256"] = attempt_root
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


def test_restricted_stage_projection_hash_is_recomputed() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    restricted["drift"] = True
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


@pytest.mark.parametrize(
    ("location", "field", "replacement"),
    (
        ("detail", "unexpected", True),
        ("detail", "detail_id", None),
        ("pre", "unexpected", True),
        ("pre", "content_persistence", None),
        ("pre_latency", "policy", None),
        ("terminal", "unexpected", True),
        ("terminal", "normalized_actor_output_sha256", None),
        ("terminal_latency", "parser", None),
    ),
)
def test_complete_audit_detail_projection_is_required_before_metrics(
    location: str,
    field: str,
    replacement: JsonValue | None,
) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, decision["runtime_audit_detail_sha256"])
    detail = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(details.pop(old_hash))))
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    terminal = cast(dict[str, JsonValue], detail["terminal"])
    targets = {
        "detail": detail,
        "pre": pre,
        "pre_latency": cast(dict[str, JsonValue], pre["latencies_ns"]),
        "terminal": terminal,
        "terminal_latency": cast(dict[str, JsonValue], terminal["latencies_ns"]),
    }
    target = targets[location]
    if replacement is None:
        target.pop(field)
    else:
        target[field] = replacement
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_hash = canonical_sha256(cast(JsonValue, detail))
    decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


def _mutate_ready_detail(
    evidence_projection: dict[str, JsonValue],
    details: dict[str, JsonValue],
    mutation: Callable[[dict[str, JsonValue]], object],
) -> None:
    cells = cast(list[JsonValue], evidence_projection["cells"])
    joint_cell = cast(dict[str, JsonValue], cells[1])
    decisions = cast(list[JsonValue], joint_cell["decisions"])
    decision = cast(dict[str, JsonValue], decisions[1])
    old_hash = cast(str, decision["runtime_audit_detail_sha256"])
    detail = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(details.pop(old_hash))))
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    mutation(restricted)
    pre["restricted_stage_projection_sha256"] = canonical_sha256(cast(JsonValue, restricted))
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_hash = canonical_sha256(cast(JsonValue, detail))
    decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)


def _mutate_no_history_detail(
    evidence_projection: dict[str, JsonValue],
    details: dict[str, JsonValue],
    mutation: Callable[[dict[str, JsonValue]], object],
) -> None:
    cells = cast(list[JsonValue], evidence_projection["cells"])
    joint_cell = cast(dict[str, JsonValue], cells[1])
    decisions = cast(list[JsonValue], joint_cell["decisions"])
    decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, decision["runtime_audit_detail_sha256"])
    detail = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(details.pop(old_hash))))
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    mutation(restricted)
    pre["restricted_stage_projection_sha256"] = canonical_sha256(cast(JsonValue, restricted))
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_hash = canonical_sha256(cast(JsonValue, detail))
    decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)


def test_ready_detail_requires_exact_request_proof_census() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    _mutate_ready_detail(
        projection,
        details,
        lambda restricted: restricted.pop("r2_4_rubric_request_proofs"),
    )

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "UNTRUSTED_TYPE"


def test_no_history_fallback_requires_every_persisted_rubric_request_proof() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)

    def _drop_last_proof(restricted: dict[str, JsonValue]) -> None:
        proofs = cast(list[JsonValue], restricted["r2_4_rubric_request_proofs"])
        proofs.pop()

    _mutate_no_history_detail(projection, details, _drop_last_proof)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "REQUEST_PROOF_CENSUS_MISMATCH"


def test_every_persisted_rubric_provider_request_proof_is_independently_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    monkeypatch.setattr(
        analysis_module,
        "validate_live_rubric_request_proof_projection_v1",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            LiveRubricError("INVALID_REQUEST_PROOF", "synthetic rejected proof")
        ),
    )

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "INVALID_REQUEST_PROOF"


def test_ready_history_provider_request_proof_is_independently_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    monkeypatch.setattr(
        analysis_module,
        "validate_live_history_policy_request_proof_projection_v1",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            R24ContractError("INVALID_HISTORY_REQUEST_PROOF", "synthetic rejected proof")
        ),
    )

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "INVALID_REQUEST_PROOF"


def test_live_attempt_token_preimage_must_bind_the_authority_receipt_hash() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    joint_cell = cast(dict[str, JsonValue], cells[1])
    decisions = cast(list[JsonValue], joint_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    receipts = cast(list[JsonValue], restricted["live_attempt_receipts"])
    first_receipt = cast(dict[str, JsonValue], receipts[0])
    first_receipt["input_tokens"] = cast(int, first_receipt["input_tokens"]) + 1
    first_receipt["total_tokens"] = cast(int, first_receipt["total_tokens"]) + 1
    pre["restricted_stage_projection_sha256"] = canonical_sha256(cast(JsonValue, restricted))
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "TRACE_BINDING_MISMATCH"


@pytest.mark.parametrize(
    ("receipt_index", "field", "replacement"),
    (
        (0, "manifest_sha256", _sha("drifted-run-authority")),
        (0, "preflight_sha256", _sha("drifted-preflight")),
        (0, "case_execution_lease_sha256", _sha("drifted-lease")),
        (0, "logical_call_id", "drifted-logical-call"),
        (0, "actor_request_sha256", _sha("drifted-actor-request")),
        (0, "pricing_binding_sha256", _sha("drifted-pricing")),
        (0, "role", "HISTORY_POLICY"),
        (1, "transport_binding_sha256", _sha("drifted-source-transport")),
        (0, "cost_usd_micros", 11),
    ),
)
def test_live_attempt_self_consistent_hash_cannot_drift_owner_bindings(
    receipt_index: int,
    field: str,
    replacement: JsonValue,
) -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    joint_cell = cast(dict[str, JsonValue], cells[1])
    decisions = cast(list[JsonValue], joint_cell["decisions"])
    decision = cast(dict[str, JsonValue], decisions[1])
    old_detail_hash = cast(str, decision["runtime_audit_detail_sha256"])
    detail = cast(
        dict[str, JsonValue], json.loads(canonical_json_bytes(details.pop(old_detail_hash)))
    )
    pre = cast(dict[str, JsonValue], detail["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    receipts = cast(list[JsonValue], restricted["live_attempt_receipts"])
    receipt = cast(dict[str, JsonValue], receipts[receipt_index])
    receipt[field] = replacement
    receipt_hashes = [canonical_sha256(item) for item in receipts]
    pre["live_attempt_receipt_sha256s"] = cast(JsonValue, receipt_hashes)
    pre["live_attempt_receipt_root_sha256"] = canonical_sha256(
        cast(
            JsonValue,
            {
                "receipt_sha256s": receipt_hashes,
                "schema_version": (
                    "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt-root/v1"
                ),
            },
        )
    )
    decision["rubric_attempt_receipt_sha256s"] = cast(JsonValue, receipt_hashes[:-1])
    decision["history_policy_attempt_receipt_sha256"] = receipt_hashes[-1]
    pre["restricted_stage_projection_sha256"] = canonical_sha256(cast(JsonValue, restricted))
    detail["pre_provider_sha256"] = canonical_sha256(cast(JsonValue, pre))
    new_detail_hash = canonical_sha256(cast(JsonValue, detail))
    decision["runtime_audit_detail_sha256"] = new_detail_hash
    details[new_detail_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "TRACE_BINDING_MISMATCH"


def test_actor_attempt_projection_cannot_omit_required_locator_fields() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    del detail["actor_provider_attempts"][0]["collector_request_locator"]
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


def test_hash_bound_operational_evidence_rejects_unknown_parser_status() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    detail["terminal"]["parser_status"] = "UNVERIFIED"
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


def test_physical_attempt_census_recomputes_the_bound_attempt_root() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    cells = cast(list[JsonValue], projection["cells"])
    first_cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], first_cell["decisions"])
    first_decision = cast(dict[str, JsonValue], decisions[0])
    old_hash = cast(str, first_decision["runtime_audit_detail_sha256"])
    detail = json.loads(canonical_json_bytes(details.pop(old_hash)))
    detail["actor_provider_attempts"][0]["unbound_field"] = True
    new_hash = canonical_sha256(cast(JsonValue, detail))
    first_decision["runtime_audit_detail_sha256"] = new_hash
    details[new_hash] = cast(JsonValue, detail)

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(
            manifest,
            cast(JsonValue, projection),
            audit_detail_projections=details,
        )

    assert error.value.code == "INVALID_AUDIT_DETAIL"


def test_unreferenced_detail_is_rejected_not_silently_accepted() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    details[_sha("unreferenced")] = {"not": "a referenced detail"}

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "UNREFERENCED_AUDIT_DETAIL"


def test_nonadjacent_equal_executed_actions_are_not_a_consecutive_repeat() -> None:
    source_manifest = _manifest()
    manifest = replace(source_manifest, max_steps_per_cell=3, max_total_actor_calls=161)
    evidence, details = _evidence(source_manifest)
    projection = pilot_stage_evidence_projection(evidence)
    projection["pilot_manifest_sha256"] = frozen_pilot_manifest_sha256(manifest)
    cells = cast(list[JsonValue], projection["cells"])
    cell = cast(dict[str, JsonValue], cells[0])
    decisions = cast(list[JsonValue], cell["decisions"])
    first = cast(dict[str, JsonValue], decisions[0])
    middle = cast(dict[str, JsonValue], decisions[1])
    details.pop(cast(str, middle["runtime_audit_detail_sha256"]))
    middle["executed_action_sha256"] = None
    middle_census = cast(dict[str, JsonValue], middle["census"])
    middle_census["actor_actions"] = 0
    last = cast(dict[str, JsonValue], json.loads(canonical_json_bytes(first)))
    last["actor_call_index"] = 3
    last["logical_call_id"] = "analysis-gap-repeat-third"
    last["runtime_audit_detail_sha256"] = _sha("missing-gap-repeat-third-detail")
    decisions.append(cast(JsonValue, last))
    cell_census = cast(dict[str, JsonValue], cell["census"])
    cell_census["actor_calls"] = cast(int, cell_census["actor_calls"]) + 1
    cell_census["wall_time_ms"] = 30
    stage_census = cast(dict[str, JsonValue], projection["census"])
    stage_census["actor_calls"] = cast(int, stage_census["actor_calls"]) + 1
    stage_census["wall_time_ms"] = cast(int, stage_census["wall_time_ms"]) + 5

    analysis = analyze_pilot_stage_v1(
        manifest,
        cast(JsonValue, projection),
        audit_detail_projections=details,
    )

    assert analysis.cells[0].repeated_action.classification is PilotClassificationV1.NOT_OBSERVED


def _write_owner_json(path: Path, value: JsonValue) -> None:
    path.write_bytes(canonical_json_bytes(value))
    path.chmod(0o600)


def _write_analysis_input_artifacts(
    root: Path,
) -> tuple[FrozenPilotManifestV1, Path, Path, Path, dict[str, JsonValue]]:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    projection = pilot_stage_evidence_projection(evidence)
    census = evidence.census
    stage = root / "03-r25-pilot.json"
    _write_owner_json(
        stage,
        cast(
            JsonValue,
            {
                "evidence": projection,
                "receipt": {
                    "actor_actions": census.actor_actions,
                    "actor_calls": census.actor_calls,
                    "completed_units": [
                        f"pilot-cell-{index:03d}" for index in range(len(manifest.cells))
                    ],
                    "cost_usd_micros": census.cost_usd_micros,
                    "evidence_sha256": canonical_sha256(cast(JsonValue, projection)),
                    "manifest_sha256": _sha("run-authority"),
                    "openai_calls": census.openai_calls,
                    "passed": True,
                    "provider_final_request_proven": True,
                    "stage": "R25_PILOT",
                    "wall_time_ms": census.wall_time_ms,
                },
                "schema_version": LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
            },
        ),
    )
    audit_root = root / "audit"
    audit_root.mkdir(mode=0o700)
    for detail in details.values():
        assert type(detail) is dict
        logical_call_id = detail["logical_call_id"]
        assert type(logical_call_id) is str
        _write_owner_json(
            audit_root / f"{logical_call_id}.production-runtime-audit.v1.json", detail
        )
    output_directory = root / "analysis"
    output_directory.mkdir(mode=0o700)
    return manifest, stage, audit_root, output_directory / "pilot-analysis.json", details


def _post_run_integrity_authority(
    manifest: FrozenPilotManifestV1,
    audit_root: Path,
) -> PostRunIntegrityAuthorityV1:
    # The integrity module exhaustively tests its sealed authority constructor.
    # This analysis-focused fixture mocks the successful reopen boundary and
    # therefore needs only the exact attributes consumed by that composition.
    authority = object.__new__(PostRunIntegrityAuthorityV1)
    run_manifest = object.__new__(R24R25RunAuthorityManifestV1)
    object.__setattr__(run_manifest, "run_id", "analysis-run")
    for name, value in {
        "run_id": "analysis-run",
        "authority_manifest_sha256": _sha("run-authority"),
        "preflight_report_sha256": _sha("preflight"),
        "factory_binding_sha256": _sha("factory"),
        "run_manifest": run_manifest,
        "pilot_manifest": manifest,
        "pilot_manifest_sha256": frozen_pilot_manifest_sha256(manifest),
        "resolved_pilot_inputs_sha256": _sha("resolved-pilot-inputs"),
        "backend_endpoint": "http://127.0.0.1:6960",
        "production_audit_root": str(audit_root),
    }.items():
        object.__setattr__(authority, name, value)
    return authority


def _upgrade_synthetic_stage_to_v2(stage: Path) -> dict[str, JsonValue]:
    document = cast(dict[str, JsonValue], json.loads(stage.read_bytes()))
    evidence = cast(dict[str, JsonValue], document["evidence"])
    receipt = cast(dict[str, JsonValue], document["receipt"])
    evidence["schema_version"] = PRODUCTION_PILOT_EVIDENCE_SCHEMA_VERSION_V2
    receipt["evidence_sha256"] = canonical_sha256(cast(JsonValue, evidence))
    _write_owner_json(stage, cast(JsonValue, document))
    raw = stage.read_bytes()
    return {
        "ordered_collector_integrity_root_sha256": _sha("ordered-collector-root"),
        "stage_files": [
            {
                "byte_count": len(raw),
                "path_identity": {"canonical_path": str(stage)},
                "sha256": hashlib.sha256(raw).hexdigest(),
                "stage": "R25_PILOT",
            }
        ],
    }


def _validated_integrity_capability(
    authority: PostRunIntegrityAuthorityV1,
    artifact_path: Path,
    artifact_sha256: str,
) -> ValidatedPostRunIntegrityArtifactV1:
    capability = object.__new__(ValidatedPostRunIntegrityArtifactV1)
    for name, value in {
        "artifact_path": str(artifact_path),
        "artifact_sha256": artifact_sha256,
        "ordered_collector_integrity_root_sha256": _sha("ordered-collector-root"),
        "authority_manifest_sha256": authority.authority_manifest_sha256,
        "pilot_manifest_sha256": frozen_pilot_manifest_sha256(authority.pilot_manifest),
        "resolved_pilot_inputs_sha256": authority.resolved_pilot_inputs_sha256,
        "preflight_report_sha256": authority.preflight_report_sha256,
        "factory_binding_sha256": authority.factory_binding_sha256,
        "backend_endpoint": authority.backend_endpoint,
        "collector_run_count": len(authority.pilot_manifest.cells) + 6,
        "pilot_collector_run_count": len(authority.pilot_manifest.cells),
        "smoke_collector_run_count": 6,
        "ordered_pilot_collector_integrity_root_sha256": _sha("ordered-pilot-collector-root"),
        "ordered_smoke_collector_integrity_root_sha256": _sha("ordered-smoke-collector-root"),
    }.items():
        object.__setattr__(capability, name, value)
    return capability


def test_complete_legacy_external_artifacts_cannot_publish_owner_only_analysis(
    tmp_path: Path,
) -> None:
    manifest, stage, audit_root, output, _ = _write_analysis_input_artifacts(tmp_path)

    analysis = analyze_pilot_artifacts_v1(
        manifest,
        run_manifest_sha256=_sha("run-authority"),
        run_id="analysis-run",
        pilot_stage_artifact=stage,
        production_audit_root=audit_root,
    )
    assert len(analysis.cells) == 80
    assert len(analysis.matched_pairs) == 40
    with pytest.raises(R25AnalysisArtifactError) as error:
        write_pilot_analysis_artifact_v1(
            analysis,
            output,
            repository_root=Path(__file__).resolve().parents[3],
        )

    assert error.value.code == "LEGACY_ANALYSIS_PUBLICATION_FORBIDDEN"
    assert not output.exists()


def test_production_external_analysis_requires_reopened_post_run_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, stage, audit_root, output, _ = _write_analysis_input_artifacts(tmp_path)
    accepted = _upgrade_synthetic_stage_to_v2(stage)
    authority = _post_run_integrity_authority(manifest, audit_root)
    confirmed_sha256 = _sha("post-run-integrity")
    integrity_path = tmp_path / "post-run-integrity.v1.json"
    capability = _validated_integrity_capability(authority, integrity_path, confirmed_sha256)
    monkeypatch.setattr(
        analysis_artifact_module,
        "reopen_validate_post_run_integrity_capability_v1",
        lambda *_args, **_kwargs: capability,
    )
    monkeypatch.setattr(
        analysis_artifact_module,
        "validated_post_run_integrity_projection_v1",
        lambda _capability: accepted,
    )
    monkeypatch.setattr(
        analysis_module,
        "validated_post_run_integrity_projection_v1",
        lambda _capability: accepted,
    )
    monkeypatch.setattr(
        analysis_module,
        "validate_pilot_stage_durable_evidence_projection_v2",
        lambda value, **_kwargs: cast(dict[str, JsonValue], value),
    )
    validated_restricted_details: list[str] = []
    monkeypatch.setattr(
        analysis_module,
        "validate_production_runtime_audit_restricted_stage_projection_v1",
        lambda detail: validated_restricted_details.append(detail.logical_call_id),
    )

    analysis = analyze_pilot_artifacts_v1(
        manifest,
        run_manifest_sha256=_sha("run-authority"),
        run_id="analysis-run",
        pilot_stage_artifact=stage,
        production_audit_root=audit_root,
        post_run_integrity_artifact=integrity_path,
        confirmed_post_run_integrity_sha256=confirmed_sha256,
        post_run_integrity_authority=authority,
    )
    digest = write_pilot_analysis_artifact_v1(
        analysis,
        output,
        repository_root=Path(__file__).resolve().parents[3],
    )

    assert (
        analysis.evidence_completeness
        is PilotAnalysisEvidenceCompletenessV1.PRODUCTION_V2_INTEGRITY_BOUND
    )
    assert analysis.post_run_integrity_artifact_sha256 == confirmed_sha256
    assert len(validated_restricted_details) == len(manifest.cells) * 2
    assert digest == pilot_analysis_sha256(analysis)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert (
        canonical_json_bytes(cast(JsonValue, json.loads(output.read_bytes())))
        == output.read_bytes()
    )


def test_production_external_analysis_rejects_unconfirmed_integrity_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, stage, audit_root, _, _ = _write_analysis_input_artifacts(tmp_path)
    accepted = _upgrade_synthetic_stage_to_v2(stage)
    authority = _post_run_integrity_authority(manifest, audit_root)
    integrity_path = tmp_path / "post-run-integrity.v1.json"
    capability = _validated_integrity_capability(
        authority, integrity_path, _sha("actual-integrity")
    )
    monkeypatch.setattr(
        analysis_artifact_module,
        "reopen_validate_post_run_integrity_capability_v1",
        lambda *_args, **_kwargs: capability,
    )
    monkeypatch.setattr(
        analysis_artifact_module,
        "validated_post_run_integrity_projection_v1",
        lambda _capability: accepted,
    )

    with pytest.raises(R25AnalysisArtifactError) as error:
        analyze_pilot_artifacts_v1(
            manifest,
            run_manifest_sha256=_sha("run-authority"),
            run_id="analysis-run",
            pilot_stage_artifact=stage,
            production_audit_root=audit_root,
            post_run_integrity_artifact=integrity_path,
            confirmed_post_run_integrity_sha256=_sha("wrong-integrity"),
            post_run_integrity_authority=authority,
        )

    assert error.value.code == "POST_RUN_INTEGRITY_CONFIRMATION_MISMATCH"


def test_external_analysis_rejects_missing_audit_detail(tmp_path: Path) -> None:
    manifest, stage, audit_root, _, details = _write_analysis_input_artifacts(tmp_path)
    first = next(iter(details.values()))
    assert type(first) is dict and type(first["logical_call_id"]) is str
    path = audit_root / f"{first['logical_call_id']}.production-runtime-audit.v1.json"
    os.unlink(path)

    with pytest.raises(R25AnalysisArtifactError) as error:
        analyze_pilot_artifacts_v1(
            manifest,
            run_manifest_sha256=_sha("run-authority"),
            run_id="analysis-run",
            pilot_stage_artifact=stage,
            production_audit_root=audit_root,
        )

    assert error.value.code == "AUDIT_DETAIL_UNAVAILABLE"


def test_external_analysis_rejects_intermediate_directory_symlink(tmp_path: Path) -> None:
    manifest, stage, audit_root, _, _ = _write_analysis_input_artifacts(tmp_path)
    alias = tmp_path / "audit-alias"
    alias.symlink_to(audit_root, target_is_directory=True)

    with pytest.raises(R25AnalysisArtifactError) as error:
        analyze_pilot_artifacts_v1(
            manifest,
            run_manifest_sha256=_sha("run-authority"),
            run_id="analysis-run",
            pilot_stage_artifact=stage,
            production_audit_root=alias,
        )

    assert error.value.code == "INVALID_PRODUCTION_AUDIT_ROOT"


def test_external_analysis_detects_audit_root_swap_and_keeps_original_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, stage, audit_root, _, _ = _write_analysis_input_artifacts(tmp_path)
    moved = tmp_path / "audit-moved"
    original_reader = analysis_artifact_module._read_owner_file_at
    swapped = False

    def _swapping_reader(*args: object, **kwargs: object) -> JsonValue:
        nonlocal swapped
        filename = cast(str, args[1])
        if not swapped and filename.endswith(".production-runtime-audit.v1.json"):
            os.rename(audit_root, moved)
            audit_root.mkdir(mode=0o700)
            swapped = True
        return original_reader(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(analysis_artifact_module, "_read_owner_file_at", _swapping_reader)

    with pytest.raises(R25AnalysisArtifactError) as error:
        analyze_pilot_artifacts_v1(
            manifest,
            run_manifest_sha256=_sha("run-authority"),
            run_id="analysis-run",
            pilot_stage_artifact=stage,
            production_audit_root=audit_root,
        )

    assert error.value.code == "INVALID_PRODUCTION_AUDIT_ROOT"
    assert any(moved.iterdir())
    assert not any(audit_root.iterdir())


def test_analysis_publication_detects_parent_swap_and_retains_created_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, stage, audit_root, output, _ = _write_analysis_input_artifacts(tmp_path)
    analysis = analyze_pilot_artifacts_v1(
        manifest,
        run_manifest_sha256=_sha("run-authority"),
        run_id="analysis-run",
        pilot_stage_artifact=stage,
        production_audit_root=audit_root,
    )
    # This test isolates the descriptor-bound publication path. Production-v2
    # construction/revalidation is exercised separately; the trusted object is
    # marked complete here only after the legacy publication rejection above.
    object.__setattr__(
        analysis,
        "evidence_completeness",
        PilotAnalysisEvidenceCompletenessV1.PRODUCTION_V2_INTEGRITY_BOUND,
    )
    object.__setattr__(analysis, "post_run_integrity_artifact_sha256", _sha("integrity"))
    object.__setattr__(
        analysis,
        "ordered_collector_integrity_root_sha256",
        _sha("ordered-collector-root"),
    )
    parent = output.parent
    moved = tmp_path / "analysis-moved"
    original_write = os.write
    swapped = False

    def _swapping_write(descriptor: int, data: object) -> int:
        nonlocal swapped
        if not swapped:
            os.rename(parent, moved)
            parent.mkdir(mode=0o700)
            swapped = True
        return original_write(descriptor, data)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "write", _swapping_write)

    with pytest.raises(R25AnalysisArtifactError) as error:
        write_pilot_analysis_artifact_v1(
            analysis,
            output,
            repository_root=Path(__file__).resolve().parents[3],
        )

    assert error.value.code == "INVALID_ANALYSIS_OUTPUT"
    assert not output.exists()
    retained = moved / output.name
    assert retained.is_file()
    assert stat.S_IMODE(retained.stat().st_mode) == 0o600


def test_analysis_cli_requires_and_forwards_post_run_integrity_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = Path(__file__).resolve().parents[3] / "scripts/analyze_r2_5_pilot.py"
    spec = importlib.util.spec_from_file_location("r25_analysis_cli_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)

    manifest_hash = _sha("run-authority")
    analysis_hash = _sha("analysis")
    pilot = _manifest()
    manifest = SimpleNamespace(
        run_id="analysis-run",
        source_commit="a" * 40,
        pilot=pilot,
        runtime_config_sha256=_sha("runtime"),
        pricing_sha256=_sha("pricing"),
        sentinel_config_sha256=_sha("sentinel"),
        max_sequence_wall_time_seconds=1_000,
        max_post_run_integrity_wall_time_seconds=100,
    )
    integrity_authority = object()
    analysis = SimpleNamespace(cells=tuple(range(80)), matched_pairs=tuple(range(40)))
    observed: dict[str, object] = {}

    def _authority_loader(path: Path, *, confirmed_manifest_sha256: str) -> object:
        observed["authority_path"] = path
        observed["authority_confirmation"] = confirmed_manifest_sha256
        return manifest

    def _integrity_authority(**kwargs: object) -> object:
        observed["integrity_authority"] = kwargs
        return integrity_authority

    def _analyze(*args: object, **kwargs: object) -> object:
        observed["analyze_args"] = args
        observed["analyze_kwargs"] = kwargs
        return analysis

    monkeypatch.setattr(module, "load_owner_authorized_authority_manifest_v2", _authority_loader)
    monkeypatch.setattr(module, "authority_manifest_sha256", lambda _manifest: manifest_hash)
    monkeypatch.setattr(module, "PostRunIntegrityAuthorityV1", _integrity_authority)
    monkeypatch.setattr(module, "analyze_pilot_artifacts_v1", _analyze)
    monkeypatch.setattr(
        module,
        "write_pilot_analysis_artifact_v1",
        lambda *_args, **_kwargs: analysis_hash,
    )
    monkeypatch.setattr(module, "pilot_analysis_sha256", lambda _analysis: analysis_hash)

    authority_path = tmp_path / "owner.json"
    stage_path = tmp_path / "03-r25-pilot.json"
    audit_root = tmp_path / "audit"
    integrity_path = tmp_path / "post-run-integrity.v1.json"
    output_path = tmp_path / "pilot-analysis.json"
    result = module.main(
        [
            "--authority-manifest",
            str(authority_path),
            "--pilot-stage-artifact",
            str(stage_path),
            "--production-audit-root",
            str(audit_root),
            "--post-run-integrity-artifact",
            str(integrity_path),
            "--output",
            str(output_path),
            "--confirm-manifest-sha256",
            manifest_hash,
            "--confirm-post-run-integrity-sha256",
            _sha("integrity"),
            "--confirm-preflight-report-sha256",
            _sha("preflight"),
            "--confirm-factory-binding-sha256",
            _sha("factory"),
            "--confirm-resolved-pilot-inputs-sha256",
            _sha("resolved"),
            "--backend-endpoint",
            "http://127.0.0.1:6960",
        ]
    )

    assert result == 0
    assert observed["authority_confirmation"] == manifest_hash
    integrity_kwargs = cast(dict[str, object], observed["integrity_authority"])
    assert integrity_kwargs["run_manifest"] is manifest
    analyze_kwargs = cast(dict[str, object], observed["analyze_kwargs"])
    assert analyze_kwargs["post_run_integrity_artifact"] == integrity_path
    assert analyze_kwargs["post_run_integrity_authority"] is integrity_authority
    assert json.loads(capsys.readouterr().out)["ok"] is True
