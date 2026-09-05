from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator

from mobile_world.offline.causal_replay.contracts import JsonValue, SpanRole
from mobile_world.runtime.sentinel.contracts import SentinelMode
from mobile_world.runtime.sentinel.r2_2.contracts import RuntimeOperationKind
from mobile_world.runtime.sentinel.r2_3.contracts import (
    PathRelevanceOutputV1,
    TopologyComparisonV1,
    TopologyDeclarationV1,
    TopologyKind,
    TopologyRunStatus,
    TopologyRunV1,
    topology_comparison_sha256,
)
from mobile_world.runtime.sentinel.r2_4.audit_detail import (
    CanonicalAuditArtifactV1,
    CpuFakeAuditResourceFlagsV1,
    ExternalRuntimeAuditDetailSinkV1,
    MemoryRuntimeAuditDetailSinkV1,
    ParserResultStatusV1,
    RuntimeAuditArtifactKindV1,
    RuntimeAuditDetailBuilderV1,
    RuntimeAuditDetailV1,
    RuntimeAuditOutcomeV1,
    RuntimeAuditStageLatenciesV1,
    runtime_audit_detail_projection,
    runtime_audit_detail_sha256,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import RuntimeHistoryCodecResolverV1
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalAdmittedPlanV1,
    RuntimeVerticalDecisionV1,
    RuntimeVerticalOperationV1,
    RuntimeVerticalPolicyOutputV1,
    RuntimeVerticalStatus,
    canonical_json_bytes,
    canonical_sha256,
    issue_cpu_fake_active_authority,
)
from mobile_world.runtime.sentinel.r2_4.renderer import render_vertical_admitted_plan
from mobile_world.runtime.sentinel.r2_4.topology import (
    CpuFakeTopologyComparisonRunnerV1,
    CpuFakeTopologyExecutionControlV1,
    CpuFakeTopologyStimulusV1,
    PilotTopologySelectionStatusV1,
    R24TopologyOutcomeV1,
    TopologyBackendStageV1,
    TopologyFailureObservationV1,
    build_cpu_fake_topology_stimulus,
    parse_r24_topology_comparison,
    r23_topology_run_sha256,
    r24_topology_comparison_projection,
    r24_topology_comparison_sha256,
    topology_input_binding_sha256,
)
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    parse_r24_cpu_topology_artifact,
    r24_cpu_topology_artifact_projection,
    r24_cpu_topology_artifact_sha256,
)
from mobile_world.runtime.sentinel.r2_4.topology_cpu import (
    produce_cpu_fake_topology_artifact_bytes,
    produce_cpu_fake_topology_comparison,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _detail(
    *,
    configured_mode: SentinelMode = SentinelMode.ACTIVE,
    effective_mode: SentinelMode = SentinelMode.ACTIVE,
    final_is_candidate: bool = True,
    provider_content: str = "<thinking>private plan</thinking>Action: wait()",
    builder_capture: list[RuntimeAuditDetailBuilderV1] | None = None,
) -> RuntimeAuditDetailV1:
    fixture = (
        Path(__file__).resolve().parents[3]
        / "tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))["application_request"]
    resolver = RuntimeHistoryCodecResolverV1()
    overlay = next(item for item in resolver.overlay_declarations if "qwen" in item.host_id)
    extraction = resolver.by_id(overlay.base_codec_id).extract_runtime(raw)
    assert extraction.history_ir is not None
    history = extraction.history_ir
    record, span = next(
        (record, span)
        for record in history.records
        for span in record.editable_spans
        if span.span_role is SpanRole.EDITABLE_CLAIM
    )
    source_policy_output_sha = _sha("source-policy-output")
    source_policy_receipt_sha = _sha("source-policy-receipt")
    source_transport_sha = _sha("source-transport")
    decision = RuntimeVerticalDecisionV1(
        decision_id="decision-1",
        target_id="target-1",
        operation=RuntimeOperationKind.DROP,
        source_decision_sha256=_sha("source-decision"),
    )
    operation = RuntimeVerticalOperationV1(
        operation_id="operation-1",
        decision_id=decision.decision_id,
        target_id=decision.target_id,
        target_record_id=record.record_id,
        target_span_sha256=span.span_sha256,
        kind=RuntimeOperationKind.DROP,
        source_operation_sha256=_sha("source-operation"),
    )
    plan = RuntimeVerticalAdmittedPlanV1(
        plan_id="plan-1",
        logical_call_id="logical-1",
        host_id=history.host_id,
        history_family=history.history_family.value,
        history_codec_id=history.codec_id,
        history_codec_contract_version=history.codec_contract_version,
        source_request_sha256=canonical_sha256(raw),
        source_policy_output_sha256=source_policy_output_sha,
        source_policy_receipt_sha256=source_policy_receipt_sha,
        source_transport_descriptor_sha256=source_transport_sha,
        source_r22_admitted_plan_sha256=_sha("r22-plan"),
        operations=(operation,),
    )
    policy = RuntimeVerticalPolicyOutputV1(
        policy_id="r24-cpu-fake-policy",
        status=RuntimeVerticalStatus.EVALUATED,
        decisions=(decision,),
        admitted_plan=plan,
        source_policy_output_sha256=source_policy_output_sha,
        source_policy_receipt_sha256=source_policy_receipt_sha,
        source_transport_descriptor_sha256=source_transport_sha,
        validation_checks=("CPU_FAKE_POLICY_BOUND",),
    )
    render = render_vertical_admitted_plan(raw, history, plan)
    rubric = PathRelevanceOutputV1(
        linkage_id="linkage-1",
        logical_call_id="logical-1",
        rubric_state_sha256=_sha("rubric-state"),
        records=(),
        topology=TopologyDeclarationV1(
            kind=TopologyKind.ISOLATED_HISTORY_FREE,
            independent_grounding_claim_eligible=True,
        ),
    )
    final = render.candidate_request if final_is_candidate else raw
    raw_response: JsonValue = {
        "id": "fake-response-1",
        "choices": [{"message": {"content": provider_content}, "finish_reason": "stop"}],
    }
    builder = RuntimeAuditDetailBuilderV1.begin_pre_provider(
        detail_id="detail-1",
        raw_request=raw,
        extraction=extraction,
        policy_output=policy,
        rubric_output=rubric,
        render_result=render,
        configured_mode=configured_mode,
        effective_mode=effective_mode,
        final_request=final,
        topology_comparison_sha256=_sha("topology"),
        pre_provider_latencies=RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=11,
            history_extract_ns=13,
            rubric_ns=17,
            policy_ns=19,
            render_ns=23,
            validator_ns=29,
            provider_ns=0,
            parser_ns=0,
            total_ns=131,
        ),
    )
    if builder_capture is not None:
        builder_capture.append(builder)
    return builder.finalize_actor_output(
        attempt_id="attempt-1",
        raw_provider_response=raw_response,
        raw_parser_input=provider_content,
        parsed_action={"action_type": "wait"},
        parser_id="unchanged-qwen-parser",
        parser_status=ParserResultStatusV1.PARSED,
        parser_attempt_count=1,
        provider_ns=31,
        parser_ns=37,
        total_ns=211,
        response_id="fake-response-1",
        model_id="fake-qwen",
        finish_reason="stop",
    )


def test_audit_artifact_is_detached_canonical_and_hash_bound() -> None:
    source = {"nested": [{"value": 1}], "unicode": "完成"}
    artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.RAW_REQUEST,
        source,
    )
    source["nested"][0]["value"] = 9  # type: ignore[index]
    projected = artifact.value
    assert projected == {"nested": [{"value": 1}], "unicode": "完成"}
    projected["nested"][0]["value"] = 7  # type: ignore[index]
    assert artifact.value["nested"][0]["value"] == 1  # type: ignore[index]
    assert artifact.sha256 == canonical_sha256(artifact.value)

    with pytest.raises(R24ContractError, match="ARTIFACT_HASH_MISMATCH"):
        replace(artifact, sha256=_sha("wrong"))


@pytest.mark.parametrize(
    "unsafe",
    [
        {"headers": {"Authorization": "Bearer abcdefghijklmnop"}},
        {"openai_api_key": "not-a-real-key"},
        {"reasoning": "private chain"},
        {"text": "OPENAI_API_KEY=not-a-real-secret"},
    ],
)
def test_audit_artifact_rejects_credentials_environment_and_reasoning(
    unsafe: dict[str, object],
) -> None:
    with pytest.raises(R24ContractError, match="FORBIDDEN_AUDIT_MATERIAL"):
        CanonicalAuditArtifactV1.capture(RuntimeAuditArtifactKindV1.RAW_REQUEST, unsafe)


def test_audit_detail_binds_every_stage_and_preserves_shadow_original() -> None:
    detail = _detail(
        configured_mode=SentinelMode.SHADOW,
        effective_mode=SentinelMode.SHADOW,
        final_is_candidate=False,
    )
    projection = runtime_audit_detail_projection(detail)

    assert detail.would_edit is True
    assert detail.edit_applied is False
    assert detail.raw_request.sha256 == detail.final_request.sha256
    assert detail.candidate_request.sha256 != detail.final_request.sha256
    assert set(projection["artifacts"]) == {  # type: ignore[arg-type]
        "raw_request",
        "history_ir",
        "policy_output",
        "rubric_output",
        "render_result",
        "candidate_request",
        "exact_diff",
        "validator_result",
        "final_request",
        "provider_response",
        "parser_result",
        "actor_action",
    }
    assert projection["resources"]["action_executed"] is False  # type: ignore[index]
    assert projection["resources"]["detail_written_to_collector"] is False  # type: ignore[index]
    assert runtime_audit_detail_sha256(detail) == canonical_sha256(projection)


def test_audit_detail_requires_module_builder_and_builder_finalizes_once() -> None:
    builders: list[RuntimeAuditDetailBuilderV1] = []
    detail = _detail(builder_capture=builders)
    assert len(builders) == 1
    with pytest.raises(R24ContractError, match="AUDIT_BUILDER_ALREADY_FINALIZED"):
        builders[0].finalize_actor_output(
            attempt_id="attempt-2",
            raw_provider_response={"content": "Action: wait()"},
            raw_parser_input="Action: wait()",
            parsed_action={"action_type": "wait"},
            parser_id="unchanged-qwen-parser",
            parser_status=ParserResultStatusV1.PARSED,
            parser_attempt_count=1,
            provider_ns=1,
            parser_ns=1,
            total_ns=200,
        )
    with pytest.raises(R24ContractError, match="MODULE_OWNED_AUDIT_BUILDER_REQUIRED"):
        replace(detail)


def test_audit_detail_fail_closed_mode_and_resource_invariants() -> None:
    with pytest.raises(R24ContractError, match="ORIGINAL_PARITY_VIOLATION"):
        _detail(
            configured_mode=SentinelMode.SHADOW,
            effective_mode=SentinelMode.SHADOW,
            final_is_candidate=True,
        )
    with pytest.raises(R24ContractError, match="ACTIVE_CANDIDATE_MISMATCH"):
        _detail(final_is_candidate=False)

    active = _detail()
    with pytest.raises(R24ContractError, match="MODULE_OWNED_AUDIT_BUILDER_REQUIRED"):
        replace(active, actor_action=active.parser_result)
    with pytest.raises(R24ContractError, match="CPU_FAKE_RESOURCE_BOUNDARY"):
        CpuFakeAuditResourceFlagsV1(action_executed=True)
    original_as_final = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.FINAL_REQUEST,
        active.raw_request.value,
    )
    with pytest.raises(R24ContractError, match="MODULE_OWNED_AUDIT_BUILDER_REQUIRED"):
        replace(
            active,
            outcome=RuntimeAuditOutcomeV1.FALLBACK_ORIGINAL,
            final_request=original_as_final,
            edit_applied=False,
            reason_code="POLICY_TIMEOUT",
        )


@pytest.mark.parametrize(
    "provider_content",
    (
        "<thinking>private qwen reasoning</thinking>Action: wait()",
        "Thought: private MAI reasoning\nAction: wait()",
    ),
)
def test_normal_provider_reasoning_is_hash_bound_but_never_persisted(
    provider_content: str,
) -> None:
    detail = _detail(provider_content=provider_content)
    projection = runtime_audit_detail_projection(detail)
    encoded = canonical_json_bytes(projection)
    provider = projection["artifacts"]["provider_response"]["value"]  # type: ignore[index]
    parser = projection["artifacts"]["parser_result"]["value"]  # type: ignore[index]

    assert provider["response_content_persisted"] is False  # type: ignore[index]
    assert provider["reasoning_persisted"] is False  # type: ignore[index]
    assert parser["parser_input_persisted"] is False  # type: ignore[index]
    assert provider_content.encode() not in encoded
    assert b"private qwen reasoning" not in encoded
    assert b"private MAI reasoning" not in encoded


def test_memory_detail_sink_snapshots_on_emit_and_read() -> None:
    sink = MemoryRuntimeAuditDetailSinkV1()
    detail = _detail()
    expected_hash = runtime_audit_detail_sha256(detail)
    sink.emit(detail)

    returned = sink.details[0]
    object.__setattr__(returned.raw_request, "sha256", _sha("mutated"))
    assert runtime_audit_detail_sha256(sink.details[0]) == expected_hash
    with pytest.raises(FileExistsError):
        sink.emit(detail)


def test_external_detail_sink_is_owner_only_atomic_and_repo_external(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    inside_parent = repository / "artifacts"
    inside_parent.mkdir()
    with pytest.raises(ValueError, match="outside the Git repository"):
        ExternalRuntimeAuditDetailSinkV1(
            inside_parent / "details",
            repository_root=repository,
        )
    actual_repository = Path(__file__).resolve().parents[4]
    with pytest.raises(ValueError, match="outside the Git repository"):
        ExternalRuntimeAuditDetailSinkV1(
            actual_repository / "forbidden-runtime-audit-details",
            repository_root=repository,
        )

    external_parent = tmp_path / "external"
    external_parent.mkdir()
    sink = ExternalRuntimeAuditDetailSinkV1(
        external_parent / "details",
        repository_root=repository,
    )
    detail = _detail()
    sink.emit(detail)
    destination = sink.root / "logical-1.runtime-audit-detail.v1.json"

    assert stat.S_IMODE(sink.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(destination.read_bytes()) == runtime_audit_detail_projection(detail)
    assert list(sink.root.glob("*.tmp")) == []
    with pytest.raises(FileExistsError):
        sink.emit(detail)


def _topology(kind: TopologyKind) -> TopologyDeclarationV1:
    return TopologyDeclarationV1(
        kind=kind,
        independent_grounding_claim_eligible=kind is TopologyKind.ISOLATED_HISTORY_FREE,
    )


def _admitted_run(kind: TopologyKind, output_label: str, latency_ns: int) -> TopologyRunV1:
    joint = kind is TopologyKind.JOINT_NON_INDEPENDENT
    return TopologyRunV1(
        topology=_topology(kind),
        status=TopologyRunStatus.ADMITTED,
        rubric_input_sha256=_sha("unbound-input"),
        rubric_output_sha256=_sha(output_label),
        rubric_receipt_sha256=_sha(f"{kind.value}-receipt"),
        history_policy_input_sha256=_sha("unbound-input") if joint else None,
        history_policy_output_sha256=_sha("joint-policy-output") if joint else None,
        failure_code=None,
        total_latency_ns=latency_ns,
    )


def _failed_run(kind: TopologyKind, code: str, latency_ns: int = 41) -> TopologyRunV1:
    return TopologyRunV1(
        topology=_topology(kind),
        status=TopologyRunStatus.FALLBACK,
        rubric_input_sha256=_sha("unbound-input"),
        rubric_output_sha256=None,
        rubric_receipt_sha256=None,
        history_policy_input_sha256=None,
        history_policy_output_sha256=None,
        failure_code=code,
        total_latency_ns=latency_ns,
    )


def _stimulus() -> CpuFakeTopologyStimulusV1:
    return build_cpu_fake_topology_stimulus(
        pair_id="pair-1",
        logical_call_id="logical-1",
        task_instruction="Wait on the current screen.",
        causal_cutoff={"event_seq": 7, "step_id": "step-1"},
        current_observation={"event_id": "obs-1", "sha256": _sha("screen")},
        isolated_rubric_script={"result": "rubric"},
        isolated_policy_script={"result": "policy"},
        joint_script={"result": "joint"},
        authority=issue_cpu_fake_active_authority(),
    )


class _ControlledExecutor:
    def __init__(
        self,
        run: TopologyRunV1,
        *,
        mismatch_input: bool = False,
        mismatch_script: bool = False,
    ) -> None:
        self._run = run
        self._mismatch_input = mismatch_input
        self._mismatch_script = mismatch_script
        self.invocations = 0

    def execute(
        self,
        *,
        stimulus: CpuFakeTopologyStimulusV1,
        control: CpuFakeTopologyExecutionControlV1,
    ) -> TopologyRunV1:
        self.invocations += 1
        kind = self._run.topology.kind
        wrong_script = _sha("wrong-script")
        if kind is TopologyKind.ISOLATED_HISTORY_FREE:
            control.run_backend(
                TopologyBackendStageV1.ISOLATED_RUBRIC,
                script_sha256=(
                    wrong_script
                    if self._mismatch_script
                    else stimulus.isolated_rubric_script_sha256
                ),
                call=lambda _invocation: None,
            )
            if self._run.status is TopologyRunStatus.ADMITTED:
                control.run_backend(
                    TopologyBackendStageV1.ISOLATED_HISTORY_POLICY,
                    script_sha256=stimulus.isolated_policy_script_sha256,
                    call=lambda _invocation: None,
                )
        else:
            control.run_backend(
                TopologyBackendStageV1.JOINT_RUBRIC_POLICY,
                script_sha256=stimulus.joint_script_sha256,
                call=lambda _invocation: None,
            )
        if self._run.status is TopologyRunStatus.NOT_RUN:
            return self._run
        expected = control.expected_input_sha256
        bound = _sha("mismatch") if self._mismatch_input else expected
        return replace(
            self._run,
            rubric_input_sha256=bound,
            history_policy_input_sha256=(
                bound
                if kind is TopologyKind.JOINT_NON_INDEPENDENT
                and self._run.status is TopologyRunStatus.ADMITTED
                else None
            ),
        )


def _compare(isolated: TopologyRunV1, joint: TopologyRunV1):
    isolated_executor = _ControlledExecutor(isolated)
    joint_executor = _ControlledExecutor(joint)
    comparison = CpuFakeTopologyComparisonRunnerV1(issue_cpu_fake_active_authority()).execute(
        comparison_id="comparison-1",
        stimulus=_stimulus(),
        isolated_executor=isolated_executor,
        joint_executor=joint_executor,
    )
    assert isolated_executor.invocations == 1
    assert joint_executor.invocations == 1
    return comparison


@pytest.mark.parametrize(
    ("joint_output", "outcome", "agreement"),
    [
        ("same", R24TopologyOutcomeV1.BOTH_ADMITTED_AGREE, True),
        ("different", R24TopologyOutcomeV1.BOTH_ADMITTED_DIVERGE, False),
    ],
)
def test_topology_runner_records_agreement_divergence_calls_and_latency(
    joint_output: str,
    outcome: R24TopologyOutcomeV1,
    agreement: bool,
) -> None:
    comparison = _compare(
        _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 101),
        _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, joint_output, 43),
    )
    projection = r24_topology_comparison_projection(comparison)

    assert comparison.outcome is outcome
    assert comparison.output_agreement is agreement
    assert comparison.total_call_count == 3
    assert comparison.total_latency_ns == 144
    assert comparison.failure_observation is TopologyFailureObservationV1.NO_FAILURE
    assert projection["total_call_count"] == 3
    assert r24_topology_comparison_sha256(comparison) == canonical_sha256(projection)


@pytest.mark.parametrize(
    ("isolated", "joint", "outcome", "failure_observation"),
    [
        (
            _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "a", 10),
            _failed_run(TopologyKind.JOINT_NON_INDEPENDENT, "BACKEND_ERROR"),
            R24TopologyOutcomeV1.ISOLATED_ONLY_ADMITTED,
            TopologyFailureObservationV1.JOINT_ONLY_FAILURE,
        ),
        (
            _failed_run(TopologyKind.ISOLATED_HISTORY_FREE, "BACKEND_ERROR"),
            _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "a", 10),
            R24TopologyOutcomeV1.JOINT_ONLY_ADMITTED,
            TopologyFailureObservationV1.ISOLATED_ONLY_FAILURE,
        ),
        (
            _failed_run(TopologyKind.ISOLATED_HISTORY_FREE, "BACKEND_ERROR"),
            _failed_run(TopologyKind.JOINT_NON_INDEPENDENT, "INVALID_RESPONSE"),
            R24TopologyOutcomeV1.BOTH_FAILED,
            TopologyFailureObservationV1.BOTH_FAILED_SAME_TRIAL,
        ),
        (
            _failed_run(TopologyKind.ISOLATED_HISTORY_FREE, "RUBRIC_TIMEOUT"),
            _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "a", 10),
            R24TopologyOutcomeV1.ISOLATED_TIMEOUT,
            TopologyFailureObservationV1.ISOLATED_ONLY_FAILURE,
        ),
        (
            _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "a", 10),
            _failed_run(TopologyKind.JOINT_NON_INDEPENDENT, "POLICY_TIMEOUT"),
            R24TopologyOutcomeV1.JOINT_TIMEOUT,
            TopologyFailureObservationV1.JOINT_ONLY_FAILURE,
        ),
        (
            _failed_run(TopologyKind.ISOLATED_HISTORY_FREE, "RUBRIC_TIMEOUT"),
            _failed_run(TopologyKind.JOINT_NON_INDEPENDENT, "POLICY_TIMEOUT"),
            R24TopologyOutcomeV1.BOTH_TIMEOUT,
            TopologyFailureObservationV1.BOTH_FAILED_SAME_TRIAL,
        ),
    ],
)
def test_topology_runner_records_one_both_failure_and_timeout(
    isolated: TopologyRunV1,
    joint: TopologyRunV1,
    outcome: R24TopologyOutcomeV1,
    failure_observation: TopologyFailureObservationV1,
) -> None:
    comparison = _compare(isolated, joint)
    assert comparison.outcome is outcome
    assert comparison.failure_observation is failure_observation


def test_topology_source_hashes_bind_exact_r23_runs() -> None:
    isolated = _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 101)
    joint = _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 43)
    comparison = _compare(isolated, joint)
    stimulus = _stimulus()
    isolated = replace(
        isolated,
        rubric_input_sha256=topology_input_binding_sha256(
            stimulus, TopologyKind.ISOLATED_HISTORY_FREE
        ),
    )
    joint_input = topology_input_binding_sha256(stimulus, TopologyKind.JOINT_NON_INDEPENDENT)
    joint = replace(
        joint,
        rubric_input_sha256=joint_input,
        history_policy_input_sha256=joint_input,
    )
    source = TopologyComparisonV1(
        comparison_id="comparison-1",
        logical_call_id="logical-1",
        isolated=isolated,
        joint=joint,
    )
    assert comparison.source_r23_comparison_sha256 == topology_comparison_sha256(source)
    assert comparison.source_isolated_run_sha256 == r23_topology_run_sha256(isolated)
    assert comparison.source_joint_run_sha256 == r23_topology_run_sha256(joint)


def test_isolated_is_always_primary_and_cpu_freezes_selection_not_run_authority() -> None:
    comparison = _compare(
        _failed_run(TopologyKind.ISOLATED_HISTORY_FREE, "BACKEND_ERROR"),
        _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "joint", 10),
    )
    assert comparison.primary_topology is TopologyKind.ISOLATED_HISTORY_FREE
    assert comparison.joint_classification is TopologyKind.JOINT_NON_INDEPENDENT
    assert comparison.joint_may_replace_isolated is False
    assert comparison.proposed_pilot_topology is TopologyKind.ISOLATED_HISTORY_FREE
    assert comparison.pilot_selection_status is (PilotTopologySelectionStatusV1.FROZEN_FOR_R25)
    assert comparison.owner_authority_present is False
    assert comparison.deployment_topology_frozen is True
    assert comparison.action_executed is False
    assert "JOINT_GROUNDING_NON_INDEPENDENT" in comparison.claim_limitations

    with pytest.raises(R24ContractError, match="MODULE_OWNED_TOPOLOGY_RUNNER_REQUIRED"):
        replace(comparison, joint_may_replace_isolated=True)
    with pytest.raises(R24ContractError, match="MODULE_OWNED_TOPOLOGY_RUNNER_REQUIRED"):
        replace(comparison, deployment_topology_frozen=False)
    with pytest.raises(R24ContractError, match="MODULE_OWNED_TOPOLOGY_RUNNER_REQUIRED"):
        replace(comparison, primary_topology="ISOLATED_HISTORY_FREE")  # type: ignore[arg-type]


def test_topology_runner_rejects_mislabeled_joint_not_run_and_tampered_authority() -> None:
    isolated = _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 10)
    runner = CpuFakeTopologyComparisonRunnerV1(issue_cpu_fake_active_authority())
    with pytest.raises(R24ContractError):
        runner.execute(
            comparison_id="comparison-1",
            stimulus=_stimulus(),
            isolated_executor=_ControlledExecutor(isolated),
            joint_executor=_ControlledExecutor(isolated),
        )

    not_run = TopologyRunV1(
        topology=_topology(TopologyKind.JOINT_NON_INDEPENDENT),
        status=TopologyRunStatus.NOT_RUN,
        rubric_input_sha256=None,
        rubric_output_sha256=None,
        rubric_receipt_sha256=None,
        history_policy_input_sha256=None,
        history_policy_output_sha256=None,
        failure_code=None,
        total_latency_ns=0,
    )
    with pytest.raises(R24ContractError, match="TOPOLOGY_CALL_CENSUS_MISMATCH"):
        runner.execute(
            comparison_id="comparison-1",
            stimulus=_stimulus(),
            isolated_executor=_ControlledExecutor(isolated),
            joint_executor=_ControlledExecutor(not_run),
        )

    with pytest.raises(R24ContractError, match="TOPOLOGY_STIMULUS_BINDING_MISMATCH"):
        runner.execute(
            comparison_id="comparison-1",
            stimulus=_stimulus(),
            isolated_executor=_ControlledExecutor(isolated, mismatch_input=True),
            joint_executor=_ControlledExecutor(
                _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 10)
            ),
        )

    with pytest.raises(R24ContractError, match="TOPOLOGY_SCRIPT_BINDING_MISMATCH"):
        runner.execute(
            comparison_id="comparison-1",
            stimulus=_stimulus(),
            isolated_executor=_ControlledExecutor(isolated, mismatch_script=True),
            joint_executor=_ControlledExecutor(
                _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 10)
            ),
        )

    with pytest.raises(R24ContractError, match="MODULE_OWNED_STIMULUS_BUILDER_REQUIRED"):
        replace(_stimulus(), pair_id="pair-2")

    authority = issue_cpu_fake_active_authority()
    object.__setattr__(authority, "gpu_allowed", True)
    with pytest.raises(R24ContractError, match="CPU_FAKE_AUTHORITY_REQUIRED"):
        CpuFakeTopologyComparisonRunnerV1(authority)


def test_audit_and_topology_projections_are_canonical_json() -> None:
    detail = _detail()
    topology = _compare(
        _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 10),
        _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 7),
    )
    assert json.loads(canonical_json_bytes(runtime_audit_detail_projection(detail))) == (
        runtime_audit_detail_projection(detail)
    )
    assert json.loads(canonical_json_bytes(r24_topology_comparison_projection(topology))) == (
        r24_topology_comparison_projection(topology)
    )


def test_topology_artifact_strict_parser_recomputes_source_and_derived_fields() -> None:
    comparison = _compare(
        _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 10),
        _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 7),
    )
    projection = r24_topology_comparison_projection(comparison)

    parsed = parse_r24_topology_comparison(projection)

    assert r24_topology_comparison_sha256(parsed) == r24_topology_comparison_sha256(comparison)
    tampered_total = dict(projection)
    tampered_total["total_latency_ns"] = 18
    with pytest.raises(R24ContractError, match="TOPOLOGY_DERIVATION_MISMATCH"):
        parse_r24_topology_comparison(tampered_total)
    tampered_run = json.loads(json.dumps(projection))
    tampered_run["source_joint_run"]["rubric_output_sha256"] = _sha("tampered")
    with pytest.raises(R24ContractError, match="SOURCE_TOPOLOGY_BINDING_MISMATCH"):
        parse_r24_topology_comparison(tampered_run)


def test_topology_artifact_projection_matches_checked_in_schema() -> None:
    comparison = _compare(
        _admitted_run(TopologyKind.ISOLATED_HISTORY_FREE, "same", 10),
        _admitted_run(TopologyKind.JOINT_NON_INDEPENDENT, "same", 7),
    )
    schema_path = (
        Path(__file__).resolve().parents[4]
        / "mobileworld_audit_handoff/schemas/r2_4/topology_comparison.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    projection = r24_topology_comparison_projection(comparison)
    validator.validate(projection)
    invalid = dict(projection)
    invalid["deployment_topology_frozen"] = False
    assert list(validator.iter_errors(invalid))


def test_real_r23_and_gpt56_cpu_components_produce_the_topology_artifact() -> None:
    repository_root = Path(__file__).resolve().parents[4]

    result = produce_cpu_fake_topology_comparison(repository_root=repository_root)

    assert result.comparison.outcome is R24TopologyOutcomeV1.BOTH_ADMITTED_AGREE
    assert result.comparison.output_agreement is True
    assert result.comparison.isolated_observed_stages == (
        TopologyBackendStageV1.ISOLATED_RUBRIC,
        TopologyBackendStageV1.ISOLATED_HISTORY_POLICY,
    )
    assert result.comparison.joint_observed_stages == (TopologyBackendStageV1.JOINT_RUBRIC_POLICY,)
    assert result.isolated_components.comparison_provider_dispatches == 2
    assert result.joint_components.comparison_provider_dispatches == 1
    for census in (result.isolated_components, result.joint_components):
        assert census.setup_rubric_provider_calls == 1
        assert census.rubric_session_receipts == 3
        assert census.policy_admission_adapter_calls == 1
        assert census.policy_receipts == 1
        assert census.policy_evaluations == 1
        assert census.rubric_output_admitted is True
        assert census.history_policy_output_admitted is True
    assert result.joint_failure_probe.provider_dispatches == 1
    assert result.joint_failure_probe.rubric_output_admitted is False
    assert result.joint_failure_probe.history_policy_output_admitted is False
    assert result.joint_failure_probe.failure_coupled is True
    assert result.comparison.cpu_only is True
    assert result.comparison.offline is True
    assert result.comparison.external_network_attempted is False
    assert result.comparison.gpu_used is False
    assert result.comparison.mobileworld_backend_used is False
    assert result.comparison.action_executed is False
    projection = r24_cpu_topology_artifact_projection(result)
    assert parse_r24_cpu_topology_artifact(projection) == result
    raw = produce_cpu_fake_topology_artifact_bytes(repository_root=repository_root)
    parsed = parse_r24_cpu_topology_artifact(json.loads(raw))
    assert parsed.isolated_components.comparison_provider_dispatches == 2
    assert parsed.joint_components.comparison_provider_dispatches == 1
    assert parsed.joint_failure_probe.provider_dispatches == 1
    assert parsed.joint_failure_probe.failure_coupled is True
    assert parsed.joint_failure_probe.rubric_output_admitted is False
    assert parsed.joint_failure_probe.history_policy_output_admitted is False
    assert r24_cpu_topology_artifact_sha256(parsed) == hashlib.sha256(raw).hexdigest()

    old_comparison_only_bytes = canonical_json_bytes(
        cast(JsonValue, r24_topology_comparison_projection(result.comparison))
    )
    with pytest.raises(R24ContractError, match="UNTRUSTED_RUNTIME_TYPE"):
        parse_r24_cpu_topology_artifact(json.loads(old_comparison_only_bytes))
