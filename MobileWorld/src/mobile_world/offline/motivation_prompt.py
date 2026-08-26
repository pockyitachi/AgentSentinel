"""Versioned prompts and JSON Schema for formal blind motivation review."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from mobile_world.offline.motivation_review import (
    CONFIDENCE_LEVELS,
    COVERAGE_VERDICTS,
    DOWNSTREAM_EFFECTS,
    HISTORY_VALIDITY,
    INVALID_SUBTYPES,
    REVIEW_SCHEMA_VERSION,
    STATE_CONFOUNDS,
    UPTAKE_EVIDENCE,
)

PREVIOUS_PROMPT_VERSION = "mobileworld.audit.motivation-codex-prompt/v2"
PROMPT_VERSION = "mobileworld.audit.motivation-codex-prompt/v3"


def _chain_schema() -> dict[str, Any]:
    # Keep this in the Structured Outputs JSON-Schema subset. The stricter
    # cross-field, ordering, length, and reference rules are enforced by
    # validate_review_batch after Codex returns.
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "candidate_id",
            "history_validity",
            "invalid_subtypes",
            "uptake_evidence",
            "state_confound",
            "downstream_effects",
            "evidence_ref_ids",
            "confidence",
            "rationale",
        ],
        "properties": {
            "candidate_id": {"type": "string"},
            "history_validity": {"type": "string", "enum": sorted(HISTORY_VALIDITY)},
            "invalid_subtypes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(INVALID_SUBTYPES)},
            },
            "uptake_evidence": {"type": "string", "enum": sorted(UPTAKE_EVIDENCE)},
            "state_confound": {"type": "string", "enum": sorted(STATE_CONFOUNDS)},
            "downstream_effects": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(DOWNSTREAM_EFFECTS)},
            },
            "evidence_ref_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "confidence": {"type": "string", "enum": sorted(CONFIDENCE_LEVELS)},
            "rationale": {"type": "string"},
        },
    }


def response_schema(
    *,
    phase: str,
    batch_id: str,
    expected_count: int,
    identity: Mapping[str, str],
    reviewer_id: str,
) -> dict[str, Any]:
    """Return the strict formal ``review_batch`` response schema."""

    review_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "record_type",
            "evaluation_run_id",
            "dataset_sha256",
            "selection_sha256",
            "phase",
            "review_id",
            "reviewer_id",
            "task_name",
            "catalog_index",
            "card_sha256",
            "coverage_verdict",
            "chains",
            "task_screen_class",
            "summary",
        ],
        "properties": {
            "schema_version": {"type": "string", "enum": [REVIEW_SCHEMA_VERSION]},
            "record_type": {"type": "string", "enum": ["task_review"]},
            "evaluation_run_id": {
                "type": "string",
                "enum": [identity["evaluation_run_id"]],
            },
            "dataset_sha256": {
                "type": "string",
                "enum": [identity["dataset_sha256"]],
            },
            "selection_sha256": {
                "type": "string",
                "enum": [identity["selection_sha256"]],
            },
            "phase": {"type": "string", "enum": [phase]},
            "review_id": {"type": "string"},
            "reviewer_id": {"type": "string", "enum": [reviewer_id]},
            "task_name": {"type": "string"},
            "catalog_index": {"type": "integer"},
            "card_sha256": {"type": "string"},
            "coverage_verdict": {"type": "string", "enum": sorted(COVERAGE_VERDICTS)},
            "chains": {"type": "array", "items": _chain_schema()},
            "task_screen_class": {
                "type": "string",
                "enum": ["POSITIVE", "UNCERTAIN", "NEGATIVE"],
            },
            "summary": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "record_type",
            "evaluation_run_id",
            "dataset_sha256",
            "selection_sha256",
            "batch_id",
            "phase",
            "reviews",
        ],
        "properties": {
            "schema_version": {"type": "string", "enum": [REVIEW_SCHEMA_VERSION]},
            "record_type": {"type": "string", "enum": ["review_batch"]},
            "evaluation_run_id": {
                "type": "string",
                "enum": [identity["evaluation_run_id"]],
            },
            "dataset_sha256": {
                "type": "string",
                "enum": [identity["dataset_sha256"]],
            },
            "selection_sha256": {
                "type": "string",
                "enum": [identity["selection_sha256"]],
            },
            "batch_id": {"type": "string", "enum": [batch_id]},
            "phase": {"type": "string", "enum": [phase]},
            "reviews": {
                "type": "array",
                "description": f"Exactly {expected_count} task reviews in supplied order.",
                "items": review_schema,
            },
        },
    }


_RUBRIC_V2 = """
Apply the formal task-card contract exactly:
- Label every candidate exactly once; chains must be sorted by candidate_id and cite only that
  candidate's evidence_ref IDs. evidence_ref_ids must be nonempty, sorted, and duplicate-free.
- REFUTED requires at least one of FALSE_CLAIM, FALSE_SUCCESS, GUI_MISINTERPRETATION,
  SUMMARY_CORRUPTION, or RESULT_MISALIGNMENT. STALE requires STALE_STATE. OFFTRACK_TRUE requires
  TRUE_BUT_OFFTRACK. SUPPORTED, UNVERIFIABLE, and NOT_A_FACTUAL_CLAIM require an empty
  invalid_subtypes list. NOT_A_FACTUAL_CLAIM is permitted only for ACTION_INTENT or PLAN.
  Plans/intents and action failure alone are not false completion claims.
- Ambiguous or missing provenance does not mechanically determine the task class. Use
  UNVERIFIABLE when the supplied evidence cannot establish validity, lower confidence as needed,
  and remember that ambiguous/missing provenance is excluded from strict metrics.
- EXPLICIT_USE requires the target prediction to cite, repeat, or rely on the old claim. Do not
  infer uptake from final failure, trajectory length, HTTP 200, repeated pixels, or action alone.
- Label observable local harm even if the trajectory later RECOVERED or may ultimately succeed.
- downstream_effects must never be empty. Use exactly [NO_VISIBLE_HARM] when the evidence shows
  no local harm, or exactly [UNKNOWN_EFFECT] when the effect cannot be established. Each of those
  two labels is exclusive. RECOVERED is never sufficient by itself: it must accompany at least
  one concrete harmful effect from UNNECESSARY_ACTION, WRONG_ACTION, REPEATED_ACTION,
  PREMATURE_TERMINATION, or OFFTRACK_CONTINUATION.
- Preserve state confounds. Natural trajectories support exposure/propagation/association, never
  causation. Abstain when evidence is insufficient.
- coverage_verdict may be SUFFICIENT only when coverage.integrity_valid=true,
  coverage.capture_complete=true, coverage.decision_count equals
  coverage.reconstructed_decision_count, and coverage.dropped_candidate_count=0. Otherwise it
  must be INSUFFICIENT.
  task_screen_class is mechanically derived from the completed chains: POSITIVE if any chain is
  REFUTED, STALE, or OFFTRACK_TRUE; otherwise UNCERTAIN if coverage is INSUFFICIENT or any chain
  is UNVERIFIABLE; otherwise NEGATIVE. An empty chain list with sufficient coverage is NEGATIVE.
- Sort every list-valued label and evidence-reference field, remove duplicates, and keep each
  rationale and task summary to one or two evidence-specific sentences. Each rationale must be
  nonempty and at most 1000 characters; each summary must be nonempty and at most 2000 characters.
- Never seek, guess, or use the official task outcome or score.
""".strip()


_RUBRIC = """
Apply the formal task-card contract exactly:
- Label every candidate exactly once; chains must be sorted by candidate_id and cite only that
  candidate's evidence_ref IDs. evidence_ref_ids must be nonempty, sorted, and duplicate-free.
- REFUTED requires at least one of FALSE_CLAIM, FALSE_SUCCESS, GUI_MISINTERPRETATION,
  SUMMARY_CORRUPTION, or RESULT_MISALIGNMENT. STALE requires STALE_STATE. OFFTRACK_TRUE requires
  TRUE_BUT_OFFTRACK. SUPPORTED, UNVERIFIABLE, and NOT_A_FACTUAL_CLAIM require an empty
  invalid_subtypes list. NOT_A_FACTUAL_CLAIM is permitted only for ACTION_INTENT or PLAN.
  Plans/intents and action failure alone are not false completion claims.
- For hybrid_folding, interpret ACTION_EXECUTION_CLAIM by its retrospective history role, not by
  surface grammar. A short imperative such as "Ask the user", Chinese "向上滚动/向上滑动", or
  "Drag from (x1,y1) to (x2,y2)" records the source action A_i even though it is phrased as a
  command. Compare it with the cited source_action and source_result/next observation:
  * If it faithfully records the parsed/executed action and is reasonable for the instruction,
    label SUPPORTED. Matching drag endpoints or direction remains SUPPORTED even when source_pre
    and source_post pixels are identical; a static UI alone never refutes action execution.
  * If it faithfully records the parsed/executed action but the action itself is clearly
    unreasonable or off-task for the instruction, label OFFTRACK_TRUE with TRUE_BUT_OFFTRACK,
    not REFUTED. For Task111-style "Ask the user", an actual ask_user action and aligned user
    response support the action record; judge task relevance separately.
  * If its action type, arguments, direction, endpoints, or aligned tool/ask result contradict
    actual A_i/R_i, label REFUTED with RESULT_MISALIGNMENT. For example, a recorded Drag paired
    with an actual click, or a claimed ask/tool result different from the aligned result, is a
    result-alignment refutation.
  * Explicit completion, success, or UI-effect assertions are still factual claims: evaluate them
    as SUPPORTED or REFUTED from direct result/post-state evidence. If the action record aligns but
    an embedded completion/effect assertion is false, use FALSE_CLAIM or FALSE_SUCCESS as
    applicable, not RESULT_MISALIGNMENT. Do not treat "I will/next/plan" prospective intent as
    executed history; ACTION_INTENT or PLAN receives NOT_A_FACTUAL_CLAIM.
- Label history validity, observed uptake, state confound, and downstream effects independently.
  An accurate off-task action does not itself prove target-step uptake or harm. Do not infer any
  axis from final task failure, trajectory length, static pixels, HTTP 200, or another axis.
- Ambiguous or missing provenance does not mechanically determine the task class. Use
  UNVERIFIABLE when the supplied evidence cannot establish validity, lower confidence as needed,
  and remember that ambiguous/missing provenance is excluded from strict metrics.
- EXPLICIT_USE requires the target prediction to cite, repeat, or rely on the old claim. Do not
  infer uptake from final failure, trajectory length, HTTP 200, repeated pixels, or action alone.
- Label observable local harm even if the trajectory later RECOVERED or may ultimately succeed.
- downstream_effects must never be empty. Use exactly [NO_VISIBLE_HARM] when the evidence shows
  no local harm, or exactly [UNKNOWN_EFFECT] when the effect cannot be established. Each of those
  two labels is exclusive. RECOVERED is never sufficient by itself: it must accompany at least
  one concrete harmful effect from UNNECESSARY_ACTION, WRONG_ACTION, REPEATED_ACTION,
  PREMATURE_TERMINATION, or OFFTRACK_CONTINUATION.
- Preserve state confounds. Natural trajectories support exposure/propagation/association, never
  causation. Abstain when evidence is insufficient.
- coverage_verdict may be SUFFICIENT only when coverage.integrity_valid=true,
  coverage.capture_complete=true, coverage.decision_count equals
  coverage.reconstructed_decision_count, and coverage.dropped_candidate_count=0. Otherwise it
  must be INSUFFICIENT.
  task_screen_class is mechanically derived from the completed chains: POSITIVE if any chain is
  REFUTED, STALE, or OFFTRACK_TRUE; otherwise UNCERTAIN if coverage is INSUFFICIENT or any chain
  is UNVERIFIABLE; otherwise NEGATIVE. An empty chain list with sufficient coverage is NEGATIVE.
- Sort every list-valued label and evidence-reference field, remove duplicates, and keep each
  rationale and task summary to one or two evidence-specific sentences. Each rationale must be
  nonempty and at most 1000 characters; each summary must be nonempty and at most 2000 characters.
- Never seek, guess, or use the official task outcome or score.
""".strip()


def _validation_feedback_block(validation_feedback: str | None) -> str:
    if validation_feedback is None:
        return ""
    compact = " ".join(validation_feedback.split())[:1000]
    return f"""

The previous response for this same blind batch was rejected by the deterministic contract
validator. Produce a complete fresh response for every supplied task; do not merely describe the
fix and do not change any evidence judgment unless required by the cited contract violation.
VALIDATION_FEEDBACK={compact}
""".rstrip()


def _build_review_prompt(
    *,
    phase: str,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
    rubric: str,
    prompt_version: str,
) -> str:
    payload = json.dumps(list(cases), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"""You are an independent offline reviewer of MobileWorld task-local history. Review
only the formal outcome-blind task cards and attached candidate images mapped by attachment_index
below. You have no need for files, tools, repositories, raw streams, outcome sidecars, or network
sources. Return only the formal review_batch JSON required by the output schema.

{rubric}

Phase={phase}; batch_id={batch_id}; reviewer_id={reviewer_id}; prompt_version={prompt_version}.
Copy every supplied expected identity field exactly. Review cards in the supplied order. The second
review is independent and contains no first-review labels.
{_validation_feedback_block(validation_feedback)}

FORMAL_BLIND_REVIEW_CASES_JSON={payload}
"""


def build_review_prompt(
    *,
    phase: str,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
) -> str:
    """Build the current prompt for a fresh independent review."""

    return _build_review_prompt(
        phase=phase,
        batch_id=batch_id,
        reviewer_id=reviewer_id,
        cases=cases,
        validation_feedback=validation_feedback,
        rubric=_RUBRIC,
        prompt_version=PROMPT_VERSION,
    )


def build_previous_review_prompt_v2(
    *,
    phase: str,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
) -> str:
    """Rebuild the frozen v2 prompt solely to verify/resume v2 artifacts."""

    return _build_review_prompt(
        phase=phase,
        batch_id=batch_id,
        reviewer_id=reviewer_id,
        cases=cases,
        validation_feedback=validation_feedback,
        rubric=_RUBRIC_V2,
        prompt_version=PREVIOUS_PROMPT_VERSION,
    )


def _build_adjudication_prompt(
    *,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
    rubric: str,
    prompt_version: str,
) -> str:
    payload = json.dumps(list(cases), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"""You are the independent adjudicator for formal MobileWorld motivation reviews.
Use only each outcome-blind task card, its attached candidate images, and the two
formal review records supplied below. Do not inspect outcome sidecars, scores, unrelated raw data,
the repository, or the network. Return only a formal ADJUDICATION review_batch JSON.

{rubric}

Resolve every metric-critical disagreement and label every candidate exactly once.
Phase=ADJUDICATION; batch_id={batch_id}; reviewer_id={reviewer_id};
prompt_version={prompt_version}. Copy expected identity fields exactly.
{_validation_feedback_block(validation_feedback)}

FORMAL_BLIND_ADJUDICATION_CASES_JSON={payload}
"""


def build_adjudication_prompt(
    *,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
) -> str:
    """Build the current prompt for fresh disagreement adjudication."""

    return _build_adjudication_prompt(
        batch_id=batch_id,
        reviewer_id=reviewer_id,
        cases=cases,
        validation_feedback=validation_feedback,
        rubric=_RUBRIC,
        prompt_version=PROMPT_VERSION,
    )


def build_previous_adjudication_prompt_v2(
    *,
    batch_id: str,
    reviewer_id: str,
    cases: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
) -> str:
    """Rebuild the frozen v2 adjudication prompt to verify/resume v2 artifacts."""

    return _build_adjudication_prompt(
        batch_id=batch_id,
        reviewer_id=reviewer_id,
        cases=cases,
        validation_feedback=validation_feedback,
        rubric=_RUBRIC_V2,
        prompt_version=PREVIOUS_PROMPT_VERSION,
    )
