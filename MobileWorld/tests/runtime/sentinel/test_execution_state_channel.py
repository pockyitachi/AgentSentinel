from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.contracts import (
    SentinelCallRole,
    SentinelFallbackReason,
    SentinelMode,
    SentinelReceipt,
    SentinelResult,
    SentinelValidationStatus,
)
from mobile_world.runtime.sentinel.execution_state_channel import (
    ExecutionStateChannelError,
    ExecutionStateChannelStatusV1,
    ExecutionStateFallbackReasonV1,
    ExternalExecutionStateReceiptSinkV1,
    MemoryExecutionStateReceiptSinkV1,
    build_execution_state_channel_receipt,
    build_execution_state_composite_result,
    history_result_receipt_sha256,
    snapshot_execution_state_composite_result,
)
from mobile_world.runtime.sentinel.prompt_view import (
    ActionKindV1,
    ActionProjectionV1,
    EvidenceGapV1,
    ExecutionAttemptSummaryV1,
    ExecutionKindV1,
    ExecutionStateViewV1,
    RepeatClassificationV1,
    RepeatClusterV1,
    RubricStateChangeV1,
    TerminalStatusV1,
    VisibleDeltaV1,
    bind_original_validated_history_candidate,
    render_execution_state_view,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalReceiptBridgeV1,
    RuntimeVerticalSentinelResultV1,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)
LOGICAL_CALL_ID = "execution-state-channel-call-1"


def _request() -> dict[str, JsonValue]:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))["application_request"]
    assert type(value) is dict
    return cast(dict[str, JsonValue], value)


def _history_fallback(request: JsonValue) -> SentinelResult:
    request_hash = canonical_sha256(request)
    receipt = SentinelReceipt(
        logical_call_id=LOGICAL_CALL_ID,
        host_id="mobileworld.qwen3vl.actor",
        call_role=SentinelCallRole.ACTOR,
        configured_mode=SentinelMode.ACTIVE,
        effective_mode=SentinelMode.OFF,
        bypass_reason=None,
        global_kill_switch_active=False,
        history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
        history_codec_contract_version="v1",
        policy_id="test.history-policy",
        policy_output_sha256="0" * 64,
        raw_request_sha256=request_hash,
        candidate_request_sha256=request_hash,
        final_request_sha256=request_hash,
        exact_diff_sha256="1" * 64,
        decision_kinds=(),
        policy_evaluated=False,
        would_edit=False,
        edit_applied=False,
        fallback_reason=SentinelFallbackReason.POLICY_EXCEPTION,
        validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
        validation_checks=("POLICY_EXCEPTION",),
        latency_ns=1,
    )
    request_bytes = canonical_json_bytes(request)
    return SentinelResult(
        receipt=receipt,
        _raw_request_json=request_bytes,
        _candidate_request_json=request_bytes,
        _final_request_json=request_bytes,
    )


def _state(request: JsonValue) -> ExecutionStateViewV1:
    action = ActionProjectionV1(action_kind=ActionKindV1.CLICK, coordinate=(25, 50))
    attempts = tuple(
        ExecutionAttemptSummaryV1(
            attempt_id=f"execution-{index}",
            source_event_seq=index * 2,
            execution_kind=ExecutionKindV1.MOBILE_ACTION,
            action_sha256="a" * 64,
            action_projection=action,
            terminal_status=TerminalStatusV1.EXECUTOR_RETURNED,
            visible_delta=VisibleDeltaV1.SCREEN_PIXELS_EXACTLY_SAME,
        )
        for index in (1, 2)
    )
    return ExecutionStateViewV1(
        logical_call_id=LOGICAL_CALL_ID,
        source_request_sha256=canonical_sha256(request),
        cutoff_event_id="event-step-started-5",
        cutoff_event_seq=5,
        ledger_sha256="b" * 64,
        source_event_count=5,
        source_event_ids_sha256="c" * 64,
        recent_attempts=attempts,
        repeat_clusters=(
            RepeatClusterV1(
                action_sha256="a" * 64,
                member_attempt_ids=("execution-1", "execution-2"),
                lower_bound=2,
                classification=RepeatClassificationV1.EXACT_ACTION_AND_SCREEN_REPEAT,
            ),
        ),
        rubric_state_change=RubricStateChangeV1.UNAVAILABLE,
        evidence_gaps=(
            EvidenceGapV1.RUBRIC_UNAVAILABLE,
            EvidenceGapV1.SEMANTIC_OUTCOME_UNVERIFIED,
            EvidenceGapV1.TASK_COMPLETION_UNVERIFIED,
        ),
        omitted_attempt_count=0,
    )


def _rendered_case() -> tuple[SentinelResult, ExecutionStateViewV1, object]:
    request = _request()
    history = _history_fallback(request)
    ir = (
        build_runtime_history_codec_resolver()
        .by_id("mobileworld.g1.history-codec.qwen-flat-progress")
        .extract(request)
    )
    proof = bind_original_validated_history_candidate(request, ir, LOGICAL_CALL_ID)
    state = _state(request)
    render = render_execution_state_view(request, ir, proof, state)
    return history, state, render


def test_history_failure_and_execution_state_success_remain_truthfully_independent() -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    render = cast(PromptViewRenderResultV1, untyped_render)
    original_history_receipt = history.receipt
    result = build_execution_state_composite_result(
        history_result=history,
        status=ExecutionStateChannelStatusV1.APPLIED,
        execution_state_view=state,
        render_result=render,
    )

    assert result.history_receipt == original_history_receipt
    assert result.history_receipt.validation_status is SentinelValidationStatus.FALLBACK_ORIGINAL
    assert result.history_receipt.fallback_reason is SentinelFallbackReason.POLICY_EXCEPTION
    assert result.execution_state_receipt.history_policy_admission_claimed is False
    assert result.execution_state_receipt.status is ExecutionStateChannelStatusV1.APPLIED
    assert result.execution_state_receipt.history_receipt_sha256 == (
        history_result_receipt_sha256(history)
    )
    assert result.raw_request == history.raw_request
    assert result.final_request == render.candidate_request
    assert result.use_transformed_request is True
    assert snapshot_execution_state_composite_result(result) == result


def test_composite_accepts_detached_runtime_vertical_history_result() -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    vertical = RuntimeVerticalSentinelResultV1(
        base_result=history,
        bridge=RuntimeVerticalReceiptBridgeV1.no_history(LOGICAL_CALL_ID),
        overlay_declaration_sha256=None,
    )
    result = build_execution_state_composite_result(
        history_result=vertical,
        status=ExecutionStateChannelStatusV1.APPLIED,
        execution_state_view=state,
        render_result=cast(PromptViewRenderResultV1, untyped_render),
    )

    assert type(result.history_result) is RuntimeVerticalSentinelResultV1
    assert result.history_receipt == history.receipt
    assert result.final_request == cast(PromptViewRenderResultV1, untyped_render).candidate_request


def test_composite_accessors_return_detached_snapshots() -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    result = build_execution_state_composite_result(
        history_result=history,
        status=ExecutionStateChannelStatusV1.APPLIED,
        execution_state_view=state,
        render_result=cast(PromptViewRenderResultV1, untyped_render),
    )
    raw_copy = cast(dict[str, JsonValue], result.raw_request)
    final_copy = cast(dict[str, JsonValue], result.final_request)
    raw_copy["model"] = "mutated-raw-copy"
    final_copy["model"] = "mutated-final-copy"

    assert cast(dict[str, JsonValue], result.raw_request)["model"] != "mutated-raw-copy"
    assert cast(dict[str, JsonValue], result.final_request)["model"] != "mutated-final-copy"


@pytest.mark.parametrize(
    ("status", "reason"),
    (
        (
            ExecutionStateChannelStatusV1.FAILED,
            ExecutionStateFallbackReasonV1.RENDER_FAILED,
        ),
        (
            ExecutionStateChannelStatusV1.UNAVAILABLE,
            ExecutionStateFallbackReasonV1.STATE_UNAVAILABLE,
        ),
    ),
)
def test_state_fallback_statuses_preserve_the_history_final_exactly(
    status: ExecutionStateChannelStatusV1,
    reason: ExecutionStateFallbackReasonV1,
) -> None:
    history = _history_fallback(_request())
    result = build_execution_state_composite_result(
        history_result=history,
        status=status,
        fallback_reason=reason,
    )

    assert result.candidate_request == history.final_request
    assert result.final_request == history.final_request
    assert result.execution_state_receipt.augmentation_applied is False
    assert result.use_transformed_request is False


def test_shadow_binds_would_augment_but_sends_the_history_final() -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    render = cast(PromptViewRenderResultV1, untyped_render)
    result = build_execution_state_composite_result(
        history_result=history,
        status=ExecutionStateChannelStatusV1.SHADOW,
        execution_state_view=state,
        render_result=render,
    )

    assert result.candidate_request == render.candidate_request
    assert result.final_request == history.final_request
    assert result.execution_state_receipt.would_augment is True
    assert result.execution_state_receipt.augmentation_applied is False


def test_channel_rejects_state_from_another_raw_request() -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    with pytest.raises(ExecutionStateChannelError, match="view binds another actor call"):
        build_execution_state_channel_receipt(
            history_result=history,
            status=ExecutionStateChannelStatusV1.APPLIED,
            execution_state_view=replace(state, source_request_sha256="d" * 64),
            render_result=cast(PromptViewRenderResultV1, untyped_render),
        )


def test_memory_and_external_sinks_are_distinct_single_use_hash_only_channels(
    tmp_path: Path,
) -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    receipt = build_execution_state_channel_receipt(
        history_result=history,
        status=ExecutionStateChannelStatusV1.APPLIED,
        execution_state_view=state,
        render_result=cast(PromptViewRenderResultV1, untyped_render),
    )
    memory = MemoryExecutionStateReceiptSinkV1()
    transaction = memory.begin(LOGICAL_CALL_ID)
    transaction.commit(receipt)
    assert memory.receipts == (receipt,)
    with pytest.raises(FileExistsError):
        memory.begin(LOGICAL_CALL_ID)

    root = tmp_path / "execution-state-receipts"
    external = ExternalExecutionStateReceiptSinkV1(root)
    try:
        external_transaction = external.begin(LOGICAL_CALL_ID)
    except OSError as exc:
        pytest.skip(f"filesystem lacks anonymous atomic receipt support: {exc}")
    external_transaction.commit(receipt)
    destination = root / f"{LOGICAL_CALL_ID}.execution-state-receipt.v1.json"
    payload = destination.read_text(encoding="utf-8")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(payload) == receipt.to_dict()
    assert cast(str, _request()["model"]) not in payload
    with pytest.raises(FileExistsError):
        external.begin(LOGICAL_CALL_ID)


def test_external_sink_directory_fsync_failure_rejects_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history, state, untyped_render = _rendered_case()
    from mobile_world.runtime.sentinel.prompt_view import PromptViewRenderResultV1

    receipt = build_execution_state_channel_receipt(
        history_result=history,
        status=ExecutionStateChannelStatusV1.APPLIED,
        execution_state_view=state,
        render_result=cast(PromptViewRenderResultV1, untyped_render),
    )
    root = tmp_path / "execution-state-receipts"
    external = ExternalExecutionStateReceiptSinkV1(root)
    try:
        transaction = external.begin(LOGICAL_CALL_ID)
    except OSError as exc:
        pytest.skip(f"filesystem lacks anonymous atomic receipt support: {exc}")
    directory_fd = transaction._directory_fd
    original_fsync = os.fsync

    def fail_directory_fsync(fd: int) -> None:
        if fd == directory_fd:
            raise OSError("injected directory fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        transaction.commit(receipt)
    assert not (root / f"{LOGICAL_CALL_ID}.execution-state-receipt.v1.json").exists()
