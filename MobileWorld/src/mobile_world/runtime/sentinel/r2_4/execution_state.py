"""Project a Collector execution ledger into a bounded actor-visible state view.

The projection is representation-agnostic.  It carries only closed execution
facts, redacts free-text action values, and emits a view only after an exact
action has actually been dispatched at least twice.
"""

from __future__ import annotations

from mobile_world.runtime.sentinel.prompt_view import (
    ActionDirectionV1,
    ActionKindV1,
    ActionProjectionV1,
    EvidenceGapV1,
    ExecutionAttemptSummaryV1,
    ExecutionKindV1,
    ExecutionStateViewV1,
    RepeatClassificationV1,
    RepeatClusterV1,
    RubricStateChangeV1,
    SystemButtonV1,
    TerminalStatusV1,
    VisibleDeltaV1,
)
from mobile_world.runtime.sentinel.r2_4.execution_ledger import (
    ExecutionAttemptV1,
    ExecutorTerminalStatusV1,
    LedgerActionKindV1,
    MobileExecutionLedgerV1,
    RepeatFactKindV1,
    ScreenPixelRelationV1,
)

_MAX_VISIBLE_ATTEMPTS = 12
_MAX_VISIBLE_CLUSTERS = 4

_ACTION_KIND = {value: ActionKindV1(value.value) for value in LedgerActionKindV1}


def build_execution_state_view(
    *,
    ledger: MobileExecutionLedgerV1,
    logical_call_id: str,
    source_request_sha256: str,
    rubric_state_changed: bool | None,
) -> ExecutionStateViewV1 | None:
    """Build a fixed-schema view, or ``None`` when no exact repeat exists."""

    if type(ledger) is not MobileExecutionLedgerV1:
        raise TypeError("ledger must use the exact MobileExecutionLedgerV1 contract")
    if type(logical_call_id) is not str or not logical_call_id:
        raise TypeError("logical_call_id must be non-empty exact text")
    if type(source_request_sha256) is not str:
        raise TypeError("source_request_sha256 must be exact text")
    if rubric_state_changed is not None and type(rubric_state_changed) is not bool:
        raise TypeError("rubric_state_changed must be bool or None")
    if not ledger.repeat_facts:
        return None

    attempts_by_id = {item.attempt_id: item for item in ledger.attempts}
    ordered_facts = sorted(
        ledger.repeat_facts,
        key=lambda fact: max(
            _attempt_seq(attempts_by_id[item]) for item in fact.member_attempt_ids
        ),
        reverse=True,
    )[:_MAX_VISIBLE_CLUSTERS]
    selected_ids: set[str] = set()
    for fact in ordered_facts:
        member_attempts = sorted(
            (attempts_by_id[item] for item in fact.member_attempt_ids),
            key=_attempt_seq,
        )
        for attempt in member_attempts[-2:]:
            if len(selected_ids) < _MAX_VISIBLE_ATTEMPTS:
                selected_ids.add(attempt.attempt_id)
    selected = tuple(item for item in ledger.attempts if item.attempt_id in selected_ids)
    summaries = tuple(_attempt_summary(item) for item in selected)
    clusters: list[RepeatClusterV1] = []
    for fact in ordered_facts:
        member_ids = tuple(item for item in fact.member_attempt_ids if item in selected_ids)
        if len(member_ids) < 2:
            continue
        clusters.append(
            RepeatClusterV1(
                action_sha256=fact.action_sha256,
                member_attempt_ids=member_ids,
                lower_bound=fact.lower_bound,
                classification=(
                    RepeatClassificationV1.EXACT_ACTION_AND_SCREEN_REPEAT
                    if fact.fact_kind is RepeatFactKindV1.EXACT_ACTION_AND_SCREEN_REPEAT
                    else RepeatClassificationV1.EXACT_ACTION_REPEAT
                ),
            )
        )
    clusters.sort(
        key=lambda item: (
            item.action_sha256,
            item.classification.value,
            item.member_attempt_ids,
        )
    )
    if not clusters:
        return None

    gaps = {
        EvidenceGapV1.TASK_COMPLETION_UNVERIFIED,
        EvidenceGapV1.SEMANTIC_OUTCOME_UNVERIFIED,
        EvidenceGapV1.REPEATED_ATTEMPT_OUTCOME_UNKNOWN,
    }
    if any(item.visible_delta is VisibleDeltaV1.UNKNOWN for item in summaries):
        gaps.add(EvidenceGapV1.VISIBLE_DELTA_UNKNOWN)
    if rubric_state_changed is None:
        gaps.add(EvidenceGapV1.RUBRIC_UNAVAILABLE)
        rubric_change = RubricStateChangeV1.UNAVAILABLE
    else:
        rubric_change = (
            RubricStateChangeV1.CHANGED if rubric_state_changed else RubricStateChangeV1.UNCHANGED
        )

    return ExecutionStateViewV1(
        logical_call_id=logical_call_id,
        source_request_sha256=source_request_sha256,
        cutoff_event_id=ledger.cutoff_event_id,
        cutoff_event_seq=ledger.cutoff_event_seq,
        ledger_sha256=ledger.sha256,
        source_event_count=ledger.source_event_count,
        source_event_ids_sha256=ledger.source_event_ids_sha256,
        recent_attempts=summaries,
        repeat_clusters=tuple(clusters),
        rubric_state_change=rubric_change,
        evidence_gaps=tuple(sorted(gaps, key=lambda item: item.value)),
        omitted_attempt_count=len(ledger.attempts) - len(selected),
    )


def _attempt_summary(value: ExecutionAttemptV1) -> ExecutionAttemptSummaryV1:
    return ExecutionAttemptSummaryV1(
        attempt_id=value.attempt_id,
        source_event_seq=_attempt_seq(value),
        execution_kind=ExecutionKindV1(value.execution_kind.value),
        action_sha256=value.action_sha256,
        action_projection=ActionProjectionV1(
            action_kind=_ACTION_KIND[value.action_projection.action_kind],
            coordinate=value.action_projection.coordinate,
            coordinate2=value.action_projection.coordinate2,
            direction=(
                None
                if value.action_projection.direction is None
                else ActionDirectionV1(value.action_projection.direction.value)
            ),
            button=(
                None
                if value.action_projection.button is None
                else SystemButtonV1(value.action_projection.button.value)
            ),
            duration_ms=value.action_projection.duration_ms,
            sensitive_value_present=value.action_projection.sensitive_value_present,
        ),
        terminal_status={
            ExecutorTerminalStatusV1.EXECUTOR_RETURNED: TerminalStatusV1.EXECUTOR_RETURNED,
            ExecutorTerminalStatusV1.EXECUTOR_RAISED: TerminalStatusV1.EXECUTOR_RAISED,
            ExecutorTerminalStatusV1.NOT_DISPATCHED: TerminalStatusV1.NOT_DISPATCHED,
            ExecutorTerminalStatusV1.UNKNOWN: TerminalStatusV1.UNKNOWN,
        }[value.terminal_status],
        visible_delta={
            ScreenPixelRelationV1.SCREEN_PIXELS_EXACTLY_SAME: (
                VisibleDeltaV1.SCREEN_PIXELS_EXACTLY_SAME
            ),
            ScreenPixelRelationV1.SCREEN_PIXELS_DIFFERENT: (VisibleDeltaV1.SCREEN_PIXELS_DIFFERENT),
            ScreenPixelRelationV1.UNKNOWN: VisibleDeltaV1.UNKNOWN,
        }[value.screen_pixel_relation],
    )


def _attempt_seq(value: ExecutionAttemptV1) -> int:
    sequence = value.action_event_seq or value.terminal_event_seq
    if sequence is None:  # pragma: no cover - guarded by ExecutionAttemptV1.
        raise ValueError("execution attempt lacks a source sequence")
    return sequence


__all__ = ["build_execution_state_view"]
