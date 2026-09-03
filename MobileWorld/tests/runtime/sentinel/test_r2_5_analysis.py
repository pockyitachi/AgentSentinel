from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel import SentinelFallbackReason
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    LIVE_EXECUTOR_BINDING_SCHEMA_VERSION,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION,
    ProductionRuntimeAuditPreProviderOutcomeV1,
    ProductionRuntimeAuditPreProviderStatusV1,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    OFFICIAL_RESULT_EVALUATOR_ID_V1,
    ActorDecisionEvidenceV1,
    DriverCallCensusV1,
    DriverStageCensusV1,
    OfficialTaskResultEvidenceV1,
    PilotCellEvidenceV1,
    PilotStageEvidenceV1,
    pilot_stage_evidence_projection,
)
from mobile_world.runtime.sentinel.r2_5.analysis import (
    PILOT_ANALYSIS_SCHEMA_VERSION,
    PilotCellAnalysisV1,
    PilotClassificationV1,
    PilotGroupAnalysisV1,
    PilotMeasurementStatusV1,
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
        max_total_openai_calls=240,
        max_total_cost_usd_micros=1_000_000,
    )


@dataclass(frozen=True)
class _BuiltDecision:
    evidence: ActorDecisionEvidenceV1
    detail_hash: str
    detail: JsonValue


def _detail(
    *,
    logical_call_id: str,
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
) -> JsonValue:
    parsed_hash = canonical_sha256(cast(JsonValue, parsed_action))
    restricted: JsonValue = {}
    if status == "READY":
        restricted = {
            "vertical_output": {"decisions": [{"operation": operation}]},
            "path_relevance_output": {
                "records": [{"disposition": "ARCHIVE_SHADOW" if archive else "RETAIN"}]
            },
            "live_attempt_receipts": [
                {
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                    "total_tokens": 13,
                }
                for _ in range(3)
            ],
        }
    elif status == "FALLBACK_ORIGINAL":
        restricted = {"live_attempt_receipts": []}
    pre: JsonValue = {
        "status": status,
        "outcome": ("GENERIC_FALLBACK_ORIGINAL" if status == "FALLBACK_ORIGINAL" else status),
        "fallback_reason": fallback_reason,
        "fallback_check": ("analysis_fixture_fallback" if status == "FALLBACK_ORIGINAL" else None),
        "raw_request_sha256": raw_sha256,
        "final_request_sha256": final_sha256,
        "exact_diff_sha256": exact_diff_sha256,
        "restricted_stage_projection": restricted,
    }
    attempts: list[JsonValue] = []
    if provider_retry_failed:
        attempts.append({"status": "FAILED"})
    attempts.append(
        {"status": "SUCCEEDED", "input_tokens": 20, "output_tokens": 4, "total_tokens": 24}
    )
    return {
        "schema_version": PRODUCTION_RUNTIME_AUDIT_DETAIL_SCHEMA_VERSION,
        "detail_id": f"detail-{logical_call_id}",
        "logical_call_id": logical_call_id,
        "pre_provider": pre,
        "pre_provider_sha256": canonical_sha256(pre),
        "sentinel_receipt_sha256": _sha(f"sentinel:{logical_call_id}"),
        "actor_provider_attempts": attempts,
        "actor_provider_attempt_root_sha256": _sha(f"attempts:{logical_call_id}"),
        "terminal": {
            "successful_provider_response_sha256": provider_response_sha256,
            "parsed_action": parsed_action,
            "parsed_action_sha256": parsed_hash,
            "action_executed": executed,
            "executed_action_sha256": parsed_hash if executed else None,
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
    elif fallback_reason is not None:
        status = "FALLBACK_ORIGINAL"
        edit = False
        final = raw
    else:
        status = "READY"
    detail = _detail(
        logical_call_id=logical_call_id,
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
    )
    detail_hash = canonical_sha256(detail)
    semantic = arm is PilotArmV1.JOINT_SENTINEL and fallback_reason is None
    census = DriverCallCensusV1(
        actor_calls=1,
        offline_rubric_evaluations=0,
        rubric_openai_calls=2 if semantic else 0,
        history_policy_openai_calls=1 if semantic else 0,
        openai_calls=3 if semantic else 0,
        actor_actions=1 if executed else 0,
        cost_usd_micros=30 if semantic else 0,
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
        pre_provider_status=(
            ProductionRuntimeAuditPreProviderStatusV1.OFF
            if arm is PilotArmV1.BASELINE
            else (
                ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
                if fallback_reason is not None
                else ProductionRuntimeAuditPreProviderStatusV1.READY
            )
        ),
        pre_provider_outcome=(
            ProductionRuntimeAuditPreProviderOutcomeV1.OFF
            if arm is PilotArmV1.BASELINE
            else (
                ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL
                if fallback_reason is not None
                else ProductionRuntimeAuditPreProviderOutcomeV1.READY
            )
        ),
        fallback_reason=(
            None if fallback_reason is None else SentinelFallbackReason(fallback_reason)
        ),
        fallback_check=(None if fallback_reason is None else "analysis_fixture_fallback"),
        preflight_report_sha256=_sha(f"preflight:{logical_call_id}"),
        case_execution_lease_sha256=(_sha(f"lease:{logical_call_id}") if semantic else None),
        live_policy_factory_binding_sha256=_sha("factory"),
        live_policy_authority_sha256=(_sha(f"authority:{logical_call_id}") if semantic else None),
        rubric_attempt_receipt_sha256s=(
            (_sha(f"rubric-1:{logical_call_id}"), _sha(f"rubric-2:{logical_call_id}"))
            if semantic
            else ()
        ),
        history_policy_attempt_receipt_sha256=(
            _sha(f"history:{logical_call_id}") if semantic else None
        ),
        actor_attempt_receipt_sha256=_sha(f"actor-attempt:{logical_call_id}"),
        sentinel_receipt_sha256=_sha(f"sentinel:{logical_call_id}"),
        provider_attempt_receipt_sha256=_sha(f"provider-attempt:{logical_call_id}"),
        runtime_audit_detail_sha256=detail_hash,
        parser_result_sha256=_sha(f"parser:{logical_call_id}"),
        parsed_action_sha256=parsed_action_hash,
        executed_action_sha256=parsed_action_hash if executed else None,
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
        if planned.arm is PilotArmV1.JOINT_SENTINEL:
            selector = index % 8
            if selector == 1:
                first_operation = "DROP"
                first_edit = True
            elif selector == 3:
                first_operation = "KEEP_UNCERTAIN"
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
                action_type="finished",
            )
        built = (first, second)
        for item in built:
            details[item.detail_hash] = item.detail
        call_censuses = tuple(item.evidence.census for item in built)
        cell_census = _sum_stage_census(call_censuses, wall_time_ms=25)
        official_binding = f"official:{index}"
        official = OfficialTaskResultEvidenceV1(
            task_id=planned.task_id,
            evaluator_id=OFFICIAL_RESULT_EVALUATOR_ID_V1,
            score_ppm=1_000_000 if successful else 0,
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
    assert analysis.overall.actor_provider_tokens.input_tokens == 3_000
    assert analysis.overall.sentinel_openai_tokens.measured_call_denominator == 80
    assert analysis.overall.sentinel_openai_tokens.not_applicable_call_count == 80
    assert analysis.overall.sentinel_openai_tokens.input_tokens == 2_100
    assert analysis.matched_overall.joint_sentinel_openai_tokens.measured_call_denominator == 80

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
    assert edit.measured_denominator == 2
    assert edit.missing_count == 0


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


def test_present_detail_must_hash_and_cross_bind_exactly() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    detail_hash = evidence.cells[0].decisions[0].runtime_audit_detail_sha256
    detail = cast(dict[str, JsonValue], details[detail_hash])
    detail["logical_call_id"] = "another-call"

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "AUDIT_DETAIL_HASH_MISMATCH"


def test_unreferenced_detail_is_rejected_not_silently_accepted() -> None:
    manifest = _manifest()
    evidence, details = _evidence(manifest)
    details[_sha("unreferenced")] = {"not": "a referenced detail"}

    with pytest.raises(R25AnalysisContractError) as error:
        analyze_pilot_stage_v1(manifest, evidence, audit_detail_projections=details)

    assert error.value.code == "UNREFERENCED_AUDIT_DETAIL"


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


def test_complete_external_artifacts_recompute_and_publish_owner_only_analysis(
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
    digest = write_pilot_analysis_artifact_v1(
        analysis,
        output,
        repository_root=Path(__file__).resolve().parents[3],
    )

    assert digest == pilot_analysis_sha256(analysis)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    published = json.loads(output.read_text(encoding="utf-8"))
    assert canonical_json_bytes(cast(JsonValue, published)) == output.read_bytes()
    assert canonical_sha256(cast(JsonValue, published)) == digest
    assert len(analysis.cells) == 80
    assert len(analysis.matched_pairs) == 40


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
