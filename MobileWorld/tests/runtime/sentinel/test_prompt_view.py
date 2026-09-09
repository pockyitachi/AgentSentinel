from __future__ import annotations

import inspect
import json
from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path
from typing import cast

import pytest

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    SpanRole,
    canonical_sha256,
    get_at_path,
)
from mobile_world.runtime.sentinel.prompt_view import (
    ActionKindV1,
    ActionProjectionV1,
    EvidenceGapV1,
    ExecutionAttemptSummaryV1,
    ExecutionKindV1,
    ExecutionStateViewV1,
    PromptViewError,
    RepeatClassificationV1,
    RepeatClusterV1,
    RubricStateChangeV1,
    TerminalStatusV1,
    VisibleDeltaV1,
    bind_r2_4_validated_history_candidate,
    build_prompt_view_adapter_registry,
    format_execution_state_view,
    render_execution_state_view,
    restore_prompt_view_history_candidate,
    validate_prompt_view_render_result,
)
from mobile_world.runtime.sentinel.r2_2.contracts import RuntimeOperationKind
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalAdmittedPlanV1,
    RuntimeVerticalOperationV1,
)
from mobile_world.runtime.sentinel.r2_4.renderer import render_vertical_admitted_plan

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "offline/fixtures/g1_5_history_codecs"
CASES = (
    "qwen_flat_progress.captured.v1.json",
    "mai_raw_replay.captured.v1.json",
)


def _request(fixture_name: str) -> dict[str, JsonValue]:
    value = json.loads((FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"))[
        "application_request"
    ]
    assert type(value) is dict
    return cast(dict[str, JsonValue], value)


def _history_case(
    fixture_name: str,
    *,
    history_ir: HistoryIR | None = None,
) -> tuple[
    dict[str, JsonValue],
    HistoryIR,
    RuntimeVerticalAdmittedPlanV1,
    object,
]:
    request = _request(fixture_name)
    codec_id = cast(str, json.loads((FIXTURE_ROOT / fixture_name).read_text())["codec_id"])
    ir = history_ir or build_runtime_history_codec_resolver().by_id(codec_id).extract(request)
    selected = next(
        (record, span)
        for record in ir.records
        for span in record.editable_spans
        if span.span_role is SpanRole.EDITABLE_CLAIM
    )
    record, span = selected
    operation = RuntimeVerticalOperationV1(
        operation_id="prompt-view-operation-1",
        decision_id="prompt-view-decision-1",
        target_id="prompt-view-target-1",
        target_record_id=record.record_id,
        target_span_sha256=span.span_sha256,
        kind=RuntimeOperationKind.DROP,
        source_operation_sha256="a" * 64,
    )
    plan = RuntimeVerticalAdmittedPlanV1(
        plan_id="prompt-view-plan-1",
        logical_call_id="prompt-view-logical-call-1",
        host_id=ir.host_id,
        history_family=ir.history_family.value,
        history_codec_id=ir.codec_id,
        history_codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        source_policy_output_sha256="b" * 64,
        source_policy_receipt_sha256="c" * 64,
        source_transport_descriptor_sha256="d" * 64,
        source_r22_admitted_plan_sha256="e" * 64,
        operations=(operation,),
    )
    render_result = render_vertical_admitted_plan(request, ir, plan)
    return request, ir, plan, render_result


def _state(request: JsonValue) -> ExecutionStateViewV1:
    action = ActionProjectionV1(action_kind=ActionKindV1.CLICK, coordinate=(100, 200))
    attempts = (
        ExecutionAttemptSummaryV1(
            attempt_id="execution-1",
            source_event_seq=3,
            execution_kind=ExecutionKindV1.MOBILE_ACTION,
            action_sha256="f" * 64,
            action_projection=action,
            terminal_status=TerminalStatusV1.EXECUTOR_RETURNED,
            visible_delta=VisibleDeltaV1.SCREEN_PIXELS_EXACTLY_SAME,
        ),
        ExecutionAttemptSummaryV1(
            attempt_id="execution-2",
            source_event_seq=7,
            execution_kind=ExecutionKindV1.MOBILE_ACTION,
            action_sha256="f" * 64,
            action_projection=action,
            terminal_status=TerminalStatusV1.EXECUTOR_RETURNED,
            visible_delta=VisibleDeltaV1.SCREEN_PIXELS_EXACTLY_SAME,
        ),
    )
    gaps = tuple(
        sorted(
            (
                EvidenceGapV1.TASK_COMPLETION_UNVERIFIED,
                EvidenceGapV1.SEMANTIC_OUTCOME_UNVERIFIED,
                EvidenceGapV1.REPEATED_ATTEMPT_OUTCOME_UNKNOWN,
            ),
            key=lambda item: item.value,
        )
    )
    return ExecutionStateViewV1(
        logical_call_id="prompt-view-logical-call-1",
        source_request_sha256=canonical_sha256(request),
        cutoff_event_id="event-step-started-10",
        cutoff_event_seq=10,
        ledger_sha256="1" * 64,
        source_event_count=10,
        source_event_ids_sha256="2" * 64,
        recent_attempts=attempts,
        repeat_clusters=(
            RepeatClusterV1(
                action_sha256="f" * 64,
                member_attempt_ids=("execution-1", "execution-2"),
                lower_bound=2,
                classification=RepeatClassificationV1.EXACT_ACTION_AND_SCREEN_REPEAT,
            ),
        ),
        rubric_state_change=RubricStateChangeV1.UNKNOWN,
        evidence_gaps=cast(tuple[EvidenceGapV1, ...], gaps),
        omitted_attempt_count=0,
    )


@pytest.mark.parametrize("fixture_name", CASES)
def test_qwen_and_mai_insert_the_same_typed_state_before_current_image(
    fixture_name: str,
) -> None:
    request, ir, plan, untyped_render_result = _history_case(fixture_name)
    from mobile_world.runtime.sentinel.r2_4.renderer import RuntimeVerticalRenderResultV1

    history_render_result = cast(RuntimeVerticalRenderResultV1, untyped_render_result)
    before = deepcopy(request)
    proof = bind_r2_4_validated_history_candidate(request, ir, plan, history_render_result)
    state = _state(request)
    result = render_execution_state_view(request, ir, proof, state)

    assert request == before
    assert restore_prompt_view_history_candidate(result) == history_render_result.candidate_request
    assert validate_prompt_view_render_result(request, ir, proof, state, result)
    anchor = ir.records[0].correction_anchors[0]
    history_container = get_at_path(history_render_result.candidate_request, anchor.container_path)
    candidate_container = get_at_path(result.candidate_request, anchor.container_path)
    assert type(history_container) is list and type(candidate_container) is list
    assert candidate_container[: anchor.insert_index] == history_container[: anchor.insert_index]
    assert (
        candidate_container[anchor.insert_index + 1 :] == history_container[anchor.insert_index :]
    )
    inserted = candidate_container[anchor.insert_index]
    assert type(inserted) is dict
    assert inserted == {"type": "text", "text": format_execution_state_view(state)}
    assert candidate_container[anchor.insert_index + 1] == get_at_path(
        request, anchor.reference_path
    )
    text = cast(str, inserted["text"])
    assert "do not establish task success" in text
    assert "same exact executor-dispatched action occurred at least 2 times" in text
    assert "do not establish task success, failure, or semantic outcome" in text
    assert (
        "No actor-native action label, task-outcome claim, or next-action recommendation is "
        "inferred."
    ) in text
    for executor_private_detail in (
        ActionKindV1.CLICK.value,
        "coordinate",
        "direction",
        "100",
        "200",
        "f" * 64,
        "rubric",
    ):
        assert executor_private_detail.lower() not in text.lower()
    action_only_state = replace(
        state,
        repeat_clusters=(
            replace(
                state.repeat_clusters[0],
                classification=RepeatClassificationV1.EXACT_ACTION_REPEAT,
            ),
        ),
    )
    action_only_text = format_execution_state_view(action_only_state)
    assert "no all-members starting-screen pixel equality is asserted" in action_only_text
    assert "were not all exactly equal" not in action_only_text

    detached = cast(dict[str, JsonValue], result.candidate_request)
    detached["model"] = "caller-mutation-does-not-stick"
    assert result.candidate_request != detached


def test_ambiguous_anchor_and_reference_hash_drift_fail_closed() -> None:
    request = _request(CASES[0])
    codec_id = cast(str, json.loads((FIXTURE_ROOT / CASES[0]).read_text())["codec_id"])
    ir = build_runtime_history_codec_resolver().by_id(codec_id).extract(request)
    anchor = ir.records[0].correction_anchors[0]
    ambiguous = replace(
        ir,
        records=(
            replace(
                ir.records[0],
                correction_anchors=(replace(anchor, visible_suffix="\n"),),
            ),
            *ir.records[1:],
        ),
    )
    request, ambiguous, plan, untyped_render_result = _history_case(CASES[0], history_ir=ambiguous)
    from mobile_world.runtime.sentinel.r2_4.renderer import RuntimeVerticalRenderResultV1

    history_render_result = cast(RuntimeVerticalRenderResultV1, untyped_render_result)
    proof = bind_r2_4_validated_history_candidate(request, ambiguous, plan, history_render_result)
    with pytest.raises(PromptViewError, match="AMBIGUOUS_PROMPT_VIEW_ANCHOR"):
        render_execution_state_view(request, ambiguous, proof, _state(request))

    drifted = replace(
        ir,
        records=tuple(
            replace(
                record,
                correction_anchors=(
                    replace(record.correction_anchors[0], reference_sha256="0" * 64),
                ),
            )
            for record in ir.records
        ),
    )
    _, _, clean_plan, clean_untyped_result = _history_case(CASES[0])
    with pytest.raises(PromptViewError, match="CORRECTION_REFERENCE_DRIFT"):
        bind_r2_4_validated_history_candidate(
            request,
            drifted,
            clean_plan,
            cast(RuntimeVerticalRenderResultV1, clean_untyped_result),
        )


@pytest.mark.parametrize("fixture_name", CASES)
def test_registry_keys_only_on_host_and_history_representation(fixture_name: str) -> None:
    request = _request(fixture_name)
    codec_id = cast(str, json.loads((FIXTURE_ROOT / fixture_name).read_text())["codec_id"])
    codec = build_runtime_history_codec_resolver().by_id(codec_id)
    first = build_prompt_view_adapter_registry().for_history_ir(codec.extract(request))
    request["model"] = "an-entirely-different-target-model"
    second = build_prompt_view_adapter_registry().for_history_ir(codec.extract(request))

    assert first == second
    assert set(first.key.to_dict()) == {
        "host_id",
        "history_codec_id",
        "history_codec_contract_version",
    }
    assert "model" not in inspect.signature(build_prompt_view_adapter_registry().by_key).parameters
    assert "text" not in {field.name for field in fields(ActionProjectionV1)}
