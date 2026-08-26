from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import pytest

from mobile_world.offline.failure_attribution import (
    ATTRIBUTION_SCHEMA_VERSION,
    FailureAttributionError,
    SourceBundle,
    build_phase_a_bundle,
    build_phase_b_bundle,
    compute_phase_b_metrics,
    load_phase_a_resolution,
    load_phase_b_resolution,
    phase_a_material_disagreement,
    phase_a_review_schema,
    phase_b_material_disagreement,
    phase_b_review_schema,
    preflight_phase_b,
    resolve_phase_a_reviews,
    resolve_phase_b_reviews,
    validate_phase_a_reviews,
    validate_phase_b_reviews,
    write_phase_a_bundle,
    write_phase_a_resolution,
    write_phase_b_resolution,
)
from mobile_world.offline.motivation_review import (
    REVIEW_SCHEMA_VERSION,
    canonical_json_bytes,
    canonical_sha256,
)

DATASET_SHA = "a" * 64
SELECTION_SHA = "b" * 64
SPAN_SHA = "c" * 64


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(canonical_json_bytes(record) for record in records))


def _step(index: int, *, terminal: bool) -> dict[str, Any]:
    step_id = f"step-{index}"
    action_type = "finished" if terminal else "click"
    return {
        "step_id": step_id,
        "step_index": index,
        "S_t": {
            "event_id": f"observation-{index}",
            "observation": {
                "accessibility_tree": None,
                "ask_user_response": None,
                "screenshot": {
                    "height": 10,
                    "mode": "RGB",
                    "pixel_blob": {
                        "algorithm": "sha256",
                        "byte_length": 3,
                        "digest": f"{index}" * 64,
                        "media_type": "image/png",
                        "relative_path": f"blobs/sha256/{index}{index}/{index * 64}",
                    },
                    "representation": "fixture",
                    "source_blob": None,
                    "width": 10,
                },
                "tool_call": None,
            },
        },
        "I_t": {
            "event_id": f"request-{index}",
            "request_view_sha256": f"{index + 2}" * 64,
            "assistant_exposures": (
                []
                if index == 1
                else [
                    {
                        "source_step_id": "step-1",
                        "source_step_index": 1,
                        "representation_type": "raw_replay",
                        "mapping_status": "exact",
                        "exposed_text": "The action succeeded",
                        "exposed_text_sha256": "d" * 64,
                    }
                ]
            ),
            "request_ask_user_messages": [],
        },
        "P_t": {
            "decision_event_id": f"decision-{index}",
            "parse_outcome": "returned",
            "parse_exception": None,
            "prediction_raw": "finish because history says done" if terminal else "tap",
        },
        "A_t": {
            "action_execution_started_event_id": None if terminal else f"action-{index}",
            "parsed_action": {
                "class": "fixture.Action",
                "serializer": "fixture",
                "serializer_version": "1",
                "value": {"action_type": action_type, "text": "success" if terminal else None},
            },
        },
        "R_t": {
            "transition_event_id": f"transition-{index}",
            "transition_type": "transition_not_executed" if terminal else "action_executed",
            "reason": "terminal_action" if terminal else None,
            "exception": None,
            "available_execution_result": None,
            "execution_result": None,
        },
        "S_t_plus_1": {
            "transition_event_id": f"transition-{index}",
            "observation": None if terminal else {"screenshot": None},
        },
    }


def _card(index: int, reconstruction: dict[str, Any]) -> dict[str, Any]:
    task_name = f"Task{index:03d}"
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "task_card",
        "evaluation_run_id": "evaluation-1",
        "dataset_sha256": DATASET_SHA,
        "selection_sha256": SELECTION_SHA,
        "task": {
            "catalog_index": index,
            "task_name": task_name,
            "task_run_id": f"task-run-{index:03d}",
            "raw_run_id": "raw-run-1",
            "source_id": "fixture-source",
            "source_relative_run_path": "fixture-source/audit/raw/runs/raw-run-1",
            "task_stream_relative_path": f"tasks/task-run-{index:03d}/events.jsonl",
        },
        "outcome_blinded": True,
        "instruction": f"Complete fixture task {index}",
        "coverage": {
            "integrity_valid": True,
            "capture_complete": True,
            "decision_count": 2,
            "reconstructed_decision_count": 2,
            "history_bearing_decision_count": 1,
            "unique_history_claim_count": 1,
            "actual_exposure_count": 1,
            "scanner_candidate_count": 1,
            "dropped_candidate_count": 0,
            "full_reconstruction_sha256": canonical_sha256(reconstruction),
        },
        "trajectory_outline": [
            {
                "step": 1,
                "prediction_excerpt": "source",
                "parsed_action": "click",
                "ui_delta": "changed",
                "history_claim_ids": ["candidate-1"],
            },
            {
                "step": 2,
                "prediction_excerpt": "target",
                "parsed_action": "finished",
                "ui_delta": None,
                "history_claim_ids": [],
            },
        ],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "retrieval_reasons": ["fixture"],
                "claim": {
                    "text": "The action succeeded",
                    "claim_type": "SUCCESS_CLAIM",
                    "source_steps": [1],
                    "representation_type": "raw_replay",
                    "provenance_confidence": "EXACT",
                },
                "exposure": {
                    "target_step": 2,
                    "request_path": "messages[3].content[0]",
                    "was_actually_in_request": True,
                    "span_sha256": SPAN_SHA,
                },
                "evidence_refs": [
                    {
                        "ref_id": "ref-source",
                        "role": "source_prediction",
                        "event_id": f"event-source-{index}",
                        "step": 1,
                        "field_path": "payload.prediction_raw",
                        "blob_sha256": None,
                        "excerpt": "The action succeeded",
                    },
                    {
                        "ref_id": "ref-target",
                        "role": "target_request",
                        "event_id": f"event-target-{index}",
                        "step": 2,
                        "field_path": "payload.request_view.messages[3].content[0]",
                        "blob_sha256": None,
                        "excerpt": "The action succeeded",
                    },
                ],
            }
        ],
    }


def _review(card: dict[str, Any], *, harmful: bool) -> dict[str, Any]:
    effects = ["WRONG_ACTION"] if harmful else ["NO_VISIBLE_HARM"]
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "task_review",
        "evaluation_run_id": card["evaluation_run_id"],
        "dataset_sha256": card["dataset_sha256"],
        "selection_sha256": card["selection_sha256"],
        "phase": "ADJUDICATION",
        "review_id": f"final-{card['task']['catalog_index']}",
        "reviewer_id": "fixture-reviewer",
        "task_name": card["task"]["task_name"],
        "catalog_index": card["task"]["catalog_index"],
        "card_sha256": canonical_sha256(card),
        "coverage_verdict": "SUFFICIENT",
        "chains": [
            {
                "candidate_id": "candidate-1",
                "history_validity": "REFUTED",
                "invalid_subtypes": ["FALSE_CLAIM"],
                "uptake_evidence": "EXPLICIT_USE",
                "state_confound": "NONE",
                "downstream_effects": effects,
                "evidence_ref_ids": ["ref-source", "ref-target"],
                "confidence": "HIGH",
                "rationale": "Fixture evidence establishes explicit reuse.",
            }
        ],
        "task_screen_class": "POSITIVE",
        "summary": "Fixture positive review.",
    }


def _task_ended(index: int, *, success: bool) -> dict[str, Any]:
    score = 1.0 if success else 0.0
    return {
        "caused_by_event_id": f"transition-{index}",
        "event_id": f"task-ended-{index}",
        "event_type": "task_ended",
        "monotonic_ns": index,
        "payload": {
            "capture_complete": True,
            "collector_error_event_ids": [],
            "environment_evaluation": {
                "exception": None,
                "reason": "success" if success else "required predicate was not satisfied",
                "score": score,
            },
            "missing_artifacts": [],
            "runtime_status": "completed",
            "teardown": {"exception": None, "returned": True, "result_snapshot_blob": None},
            "termination": {"exception": None, "source": "finished_action", "step_index": 2},
            "token_usage": None,
        },
        "producer": {"component": "fixture", "process_id": 1, "version": "1", "worker_id": "w"},
        "run_id": "raw-run-1",
        "schema_version": "mobileworld.audit.event/v1",
        "seq": 1,
        "stream_id": f"task-run-{index:03d}",
        "task_run_id": f"task-run-{index:03d}",
        "wall_time": "2026-01-01T00:00:00Z",
    }


def _fixture_bundle(tmp_path: Path) -> tuple[SourceBundle, Path]:
    source_base = tmp_path / "source-base"
    bundle_root = source_base / "motivation-bundle"
    cards: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    reconstructions: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for index, success in ((1, False), (2, True)):
        task_stream = (
            source_base
            / "fixture-source"
            / "audit"
            / "raw"
            / "runs"
            / "raw-run-1"
            / "tasks"
            / f"task-run-{index:03d}"
            / "events.jsonl"
        )
        event = _task_ended(index, success=success)
        _write_jsonl(task_stream, [event])
        stream_sha = hashlib.sha256(task_stream.read_bytes()).hexdigest()
        reconstruction = {
            "canonical_suite_index": index,
            "provenance": {
                "source_id": "fixture-source",
                "source_relative_run_path": "fixture-source/audit/raw/runs/raw-run-1",
                "source_run_id": "raw-run-1",
                "source_task_run_id": f"task-run-{index:03d}",
                "task_stream_relative_path": f"tasks/task-run-{index:03d}/events.jsonl",
                "task_stream_sha256": stream_sha,
            },
            "schema_version": "mobileworld.audit.motivation-cards/v1",
            "steps": [_step(1, terminal=False), _step(2, terminal=True)],
            "task_ended_event_id": f"task-ended-{index}",
            "task_instruction": f"Complete fixture task {index}",
            "task_key": f"fixture::{index}",
            "task_name": f"Task{index:03d}",
            "task_started_event_id": f"task-started-{index}",
        }
        card = _card(index, reconstruction)
        cards.append(card)
        reviews.append(_review(card, harmful=index == 1))
        reconstructions.append(reconstruction)
        outcomes.append(
            {
                "app": "fixture-app",
                "catalog_index": index,
                "outcome": "SUCCESS" if success else "FAILURE",
                "score": 1.0 if success else 0.0,
                "task_name": f"Task{index:03d}",
            }
        )
    _write_jsonl(bundle_root / "cards" / "task_cards.jsonl", cards)
    _write_jsonl(bundle_root / "cards" / "reconstruction_refs.jsonl", reconstructions)
    _write_jsonl(bundle_root / "cards" / "outcomes.sidecar.jsonl", outcomes)
    _write_jsonl(bundle_root / "review_v1" / "final" / "reviews.jsonl", reviews)
    return SourceBundle.from_root("fixture-model", bundle_root), source_base


def _phase_a_reviews(
    cards: tuple[dict[str, Any], ...],
    *,
    reviewer_id: str = "fixture-phase-a-reviewer",
    review_prefix: str = "phase-a",
) -> list[dict[str, Any]]:
    result = []
    for card in cards:
        failure = card["task"]["catalog_index"] == 1
        chain = card["frozen_strict_mhr_chains"][0]
        step_id = card["terminal_trace"]["steps"][0]["step_id"]
        result.append(
            {
                "schema_version": ATTRIBUTION_SCHEMA_VERSION,
                "record_type": "failure_attribution_phase_a_review",
                "attribution_run_id": card["attribution_run_id"],
                "review_id": f"{review_prefix}-{card['task_key']}",
                "reviewer_id": reviewer_id,
                "task_key": card["task_key"],
                "model_id": card["model_id"],
                "task_name": card["task"]["task_name"],
                "catalog_index": card["task"]["catalog_index"],
                "phase_a_card_sha256": canonical_sha256(card),
                "outcome_blinded": True,
                "chains": [
                    {
                        "candidate_id": chain["candidate_id"],
                        "recovery_status": "NOT_RECOVERED" if failure else "RECOVERED",
                        "continuity_status": "CONTINUOUS" if failure else "INTERRUPTED",
                        "final_observable_predicate": "UNSATISFIED" if failure else "SATISFIED",
                        "affected_predicate": {
                            "predicate_id": "required-task-state",
                            "description": "The task's required final state is satisfied.",
                        },
                        "target_contribution": "PRESERVES_DEFECT_BY_FALSE_COMPLETION",
                        "competing_trace_defects": [],
                        "evidence_step_ids": [step_id],
                        "confidence": "HIGH",
                        "rationale": "The target-to-terminal trace supports this frozen judgment.",
                    }
                ],
                "summary": "Outcome-blind suffix review.",
            }
        )
    return result


def _phase_b_review(
    card: dict[str, Any],
    *,
    reviewer_id: str = "fixture-phase-b-reviewer",
    review_prefix: str = "phase-b",
) -> dict[str, Any]:
    candidate_id = card["frozen_strict_mhr_chains"][0]["candidate_id"]
    step_id = card["terminal_trace"]["steps"][0]["step_id"]
    success = card["outcome"]["outcome"] == "SUCCESS"
    link_level = "NOT_APPLICABLE_SUCCESS_CONTROL" if success else "STRONG_OBSERVED_CONTRIBUTION"
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_b_review",
        "attribution_run_id": card["attribution_run_id"],
        "review_id": f"{review_prefix}-{card['task_key']}",
        "reviewer_id": reviewer_id,
        "task_key": card["task_key"],
        "model_id": card["model_id"],
        "task_name": card["task"]["task_name"],
        "catalog_index": card["task"]["catalog_index"],
        "phase_b_card_sha256": canonical_sha256(card),
        "chains": [
            {
                "candidate_id": candidate_id,
                "recovery_status": card["annotation_template"][0]["recovery_status"],
                "affected_predicate_id": card["annotation_template"][0]["affected_predicate_id"],
                "target_contribution": card["annotation_template"][0]["target_contribution"],
                "evaluator_predicate": {
                    "affected_predicate_id": card["annotation_template"][0][
                        "affected_predicate_id"
                    ],
                    "evaluator_predicate_description": (
                        "The environment evaluator checks the required task state."
                    ),
                    "evaluator_evidence": {
                        "field_path": "task_ended.environment_evaluation.reason",
                        "excerpt": card["task_ended"]["environment_evaluation"]["reason"],
                    },
                },
                "verifier_alignment": ("NOT_APPLICABLE_SUCCESS_CONTROL" if success else "DIRECT"),
                "alternative_sufficient_failure": (
                    "NOT_APPLICABLE_SUCCESS_CONTROL" if success else "ABSENT"
                ),
                "failure_link_level": link_level,
                "alternative_defect_ids": [],
                "evaluator_revealed_alternatives": [],
                "evidence_step_ids": [step_id],
                "confidence": "HIGH",
                "rationale": "The unrecovered path directly matches the evaluator reason.",
            }
        ],
        "task_failure_link_level": link_level,
        "summary": (
            "Successful control; failure attribution is not applicable."
            if success
            else "Strong observed contribution, not causal proof."
        ),
    }


def _contains_key(value: Any, forbidden: set[str]) -> bool:
    if isinstance(value, dict):
        return bool(set(value) & forbidden) or any(
            _contains_key(item, forbidden) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_key(item, forbidden) for item in value)
    return False


def _json_schema_scalar_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    raise AssertionError(f"unsupported scalar in const/enum regression: {value!r}")


def _assert_provider_typed_const_and_enum(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        if "const" in value:
            assert value.get("type") == _json_schema_scalar_type(value["const"]), path
        if "enum" in value:
            enum_values = value["enum"]
            assert isinstance(enum_values, list) and enum_values, path
            assert value.get("type") is not None, path
            assert {_json_schema_scalar_type(enum_value) for enum_value in enum_values} == {
                value["type"]
            }, path
        for key, child in value.items():
            _assert_provider_typed_const_and_enum(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_provider_typed_const_and_enum(child, path=f"{path}[{index}]")


def test_phase_a_is_outcome_blind_and_includes_success_control(tmp_path: Path) -> None:
    source, _ = _fixture_bundle(tmp_path)
    source.outcomes_path.unlink()
    bundle = build_phase_a_bundle([source])

    assert bundle.manifest["counts"] == {
        **bundle.manifest["counts"],
        "strict_mhr_task_count": 2,
        "strict_mhr_chain_count": 2,
        "strict_mhr_oh_chain_count": 1,
        "strict_mhr_without_oh_chain_count": 1,
    }
    assert len(bundle.cards) == 2
    assert all(card["outcome_blinded"] is True for card in bundle.cards)
    assert not _contains_key(
        {"cards": bundle.cards, "manifest": bundle.manifest},
        {"outcome", "score", "environment_evaluation"},
    )
    assert bundle.cards[1]["frozen_strict_mhr_chains"][0]["strict_mhr_oh"] is False
    assert bundle.cards[0]["annotation_template"][0]["recovery_status"] is None
    first_card = bundle.cards[0]
    assert first_card["trajectory_evidence_completeness"] == ("FULL_RECONSTRUCTION_PROJECTION")
    assert [step["step"] for step in first_card["full_trajectory_outline"]] == [1, 2]
    assert [step["step_index"] for step in first_card["prefix_trace"]["steps"]] == [1]
    assert [step["step_index"] for step in first_card["terminal_trace"]["steps"]] == [2]
    assert first_card["source_binding"]["source_trajectory_outline_sha256"] == canonical_sha256(
        first_card["full_trajectory_outline"]
    )

    dry_output = tmp_path / "must-not-exist"
    summary = write_phase_a_bundle(bundle, dry_output, dry_run=True)
    assert summary["dry_run"] is True
    assert not dry_output.exists()


def test_phase_b_preflight_joins_only_after_phase_a_reviews_freeze(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])

    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])
    preflight = preflight_phase_b(phase_a, resolution, source_base=source_base)

    assert preflight["phase_a_reviews_frozen"] is True
    assert preflight["phase_b_cards_buildable"] is True
    assert preflight["counts"] == {
        **preflight["counts"],
        "all_failure_task_count": 1,
        "failure_strict_mhr_task_count": 1,
        "failure_strict_mhr_chain_count": 1,
        "success_control_task_count": 1,
        "success_control_chain_count": 1,
        "validated_task_ended_count": 2,
    }


def test_phase_b_binds_task_ended_and_freezes_phase_a_recovery(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    assert len(validate_phase_a_reviews(primary, phase_a.cards)) == 2
    resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])

    phase_b = build_phase_b_bundle(phase_a, resolution, source_base=source_base)

    assert phase_b.manifest["counts"]["all_failure_task_count"] == 1
    assert phase_b.manifest["counts"]["failure_strict_mhr_task_count"] == 1
    assert phase_b.manifest["counts"]["phase_b_task_count"] == 2
    assert phase_b.manifest["counts"]["success_control_task_count"] == 1
    assert len(phase_b.cards) == 2
    card = next(card for card in phase_b.cards if card["outcome"]["outcome"] == "FAILURE")
    assert card["outcome"]["outcome"] == "FAILURE"
    assert card["task_ended"]["environment_evaluation"]["reason"] == (
        "required predicate was not satisfied"
    )
    assert card["annotation_template"][0]["recovery_status"] == "NOT_RECOVERED"
    assert card["causal_claim_supported"] is False

    reviews = [_phase_b_review(item) for item in phase_b.cards]
    assert len(validate_phase_b_reviews(reviews, phase_b.cards)) == 2
    metrics = compute_phase_b_metrics(reviews, phase_b.cards, all_failure_task_count=1)
    assert metrics["strong_observed_contribution"]["percentage_of_all_failures"] == 100.0
    by_chain_class = metrics["observed_contribution_by_linked_chain_class"]
    assert (
        by_chain_class["strict_mhr_any_chain"]["strong_observed_contribution"][
            "percentage_of_all_failures"
        ]
        == 100.0
    )
    assert by_chain_class["strict_mhr_oh_chain"]["strong_observed_contribution"]["task_count"] == 1
    assert (
        by_chain_class["strict_mhr_non_oh_chain"]["strong_observed_contribution"]["task_count"] == 0
    )
    assert by_chain_class["task_classes_may_overlap"] is True
    assert metrics["causal_failure_task_count"] is None

    mutated_reviews = copy.deepcopy(reviews)
    mutated = next(
        review for review in mutated_reviews if review["task_name"] == card["task"]["task_name"]
    )
    mutated["chains"][0]["recovery_status"] = "RECOVERED"
    with pytest.raises(FailureAttributionError, match="NOT_RECOVERED"):
        validate_phase_b_reviews(mutated_reviews, phase_b.cards)

    fabricated_predicate_evidence = copy.deepcopy(reviews)
    fabricated = next(
        review
        for review in fabricated_predicate_evidence
        if review["task_name"] == card["task"]["task_name"]
    )
    fabricated["chains"][0]["evaluator_predicate"]["evaluator_evidence"]["excerpt"] = (
        "THIS TEXT IS NOT IN THE RAW EVENT"
    )
    with pytest.raises(FailureAttributionError, match="real nonempty substring"):
        validate_phase_b_reviews(fabricated_predicate_evidence, phase_b.cards)

    score_only_direct = copy.deepcopy(reviews)
    score_only = next(
        review for review in score_only_direct if review["task_name"] == card["task"]["task_name"]
    )
    score_only["chains"][0]["evaluator_predicate"]["evaluator_evidence"] = {
        "field_path": "task_ended.environment_evaluation.score",
        "excerpt": "0.0",
    }
    with pytest.raises(FailureAttributionError, match="beyond the score alone"):
        validate_phase_b_reviews(score_only_direct, phase_b.cards)


def test_phase_b_refuses_incomplete_phase_a_or_raw_digest_drift(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    with pytest.raises(FailureAttributionError, match="cover every Phase-A card"):
        resolve_phase_a_reviews(phase_a, primary, secondary[:1], [])
    resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])

    stream = (
        source_base
        / "fixture-source"
        / "audit"
        / "raw"
        / "runs"
        / "raw-run-1"
        / "tasks"
        / "task-run-001"
        / "events.jsonl"
    )
    stream.write_bytes(stream.read_bytes() + b"\n")
    with pytest.raises(FailureAttributionError, match="canonical JSONL"):
        preflight_phase_b(phase_a, resolution, source_base=source_base)


def test_phase_a_material_adjudication_and_freeze_round_trip(tmp_path: Path) -> None:
    source, _ = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    failure_card = phase_a.cards[0]
    assert not phase_a_material_disagreement(primary[0], secondary[0], failure_card)

    predicate_disagreement = copy.deepcopy(secondary[0])
    predicate_disagreement["chains"][0]["affected_predicate"]["description"] = (
        "A materially different atomic task predicate."
    )
    assert phase_a_material_disagreement(primary[0], predicate_disagreement, failure_card)

    mechanism_disagreement = copy.deepcopy(secondary[0])
    mechanism_disagreement["chains"][0]["target_contribution"] = "AMPLIFIES"
    assert phase_a_material_disagreement(primary[0], mechanism_disagreement, failure_card)

    secondary[0]["chains"][0]["final_observable_predicate"] = "PARTIAL"
    assert phase_a_material_disagreement(primary[0], secondary[0], failure_card)
    with pytest.raises(FailureAttributionError, match="material disagreements"):
        resolve_phase_a_reviews(phase_a, primary, secondary, [])

    adjudication = copy.deepcopy(primary[0])
    adjudication["review_id"] = "adjudication-fixture-model/Task001"
    adjudication["reviewer_id"] = "phase-a-adjudicator"
    resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [adjudication])
    assert resolution.manifest["counts"]["material_disagreement_task_count"] == 1
    assert resolution.manifest["counts"]["adjudication_review_count"] == 1
    assert resolution.manifest["counts"]["unresolved_task_count"] == 0
    assert resolution.manifest["outcomes_opened"] is False
    assert resolution.final_reviews[0]["reviewer_id"] == "phase-a-adjudicator"

    freeze_dir = tmp_path / "phase-a-freeze"
    write_phase_a_resolution(resolution, freeze_dir)
    loaded = load_phase_a_resolution(freeze_dir, phase_a)
    assert loaded.manifest == resolution.manifest
    assert loaded.final_reviews == resolution.final_reviews


def test_phase_b_material_adjudication_and_freeze_round_trip(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    phase_a_primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    phase_a_secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    phase_a_resolution = resolve_phase_a_reviews(phase_a, phase_a_primary, phase_a_secondary, [])
    phase_b = build_phase_b_bundle(phase_a, phase_a_resolution, source_base=source_base)
    primary = [
        _phase_b_review(card, reviewer_id="phase-b-primary", review_prefix="primary")
        for card in phase_b.cards
    ]
    secondary = [
        _phase_b_review(card, reviewer_id="phase-b-secondary", review_prefix="secondary")
        for card in phase_b.cards
    ]
    failure_index = next(
        index for index, card in enumerate(phase_b.cards) if card["outcome"]["outcome"] == "FAILURE"
    )
    failure_card = phase_b.cards[failure_index]
    assert not phase_b_material_disagreement(
        primary[failure_index], secondary[failure_index], failure_card
    )

    evidence_disagreement = copy.deepcopy(secondary[failure_index])
    evidence_disagreement["chains"][0]["evaluator_predicate"]["evaluator_evidence"]["excerpt"] = (
        "predicate"
    )
    assert phase_b_material_disagreement(
        primary[failure_index], evidence_disagreement, failure_card
    )

    secondary[failure_index]["chains"][0]["verifier_alignment"] = "INDIRECT"
    secondary[failure_index]["chains"][0]["failure_link_level"] = "PLAUSIBLE_OBSERVED_CONTRIBUTION"
    secondary[failure_index]["task_failure_link_level"] = "PLAUSIBLE_OBSERVED_CONTRIBUTION"
    assert phase_b_material_disagreement(
        primary[failure_index], secondary[failure_index], failure_card
    )
    adjudication = copy.deepcopy(primary[failure_index])
    adjudication["review_id"] = "adjudication-fixture-model/Task001"
    adjudication["reviewer_id"] = "phase-b-adjudicator"
    resolution = resolve_phase_b_reviews(
        phase_b,
        primary,
        secondary,
        [adjudication],
        all_failure_task_count=1,
    )
    assert resolution.manifest["counts"]["material_disagreement_task_count"] == 1
    assert resolution.manifest["counts"]["unresolved_task_count"] == 0
    assert resolution.manifest["outcomes_opened"] is True
    assert resolution.metrics["causal_failure_task_count"] is None

    freeze_dir = tmp_path / "phase-b-freeze"
    write_phase_b_resolution(resolution, freeze_dir)
    loaded = load_phase_b_resolution(freeze_dir, phase_b)
    assert loaded.manifest == resolution.manifest
    assert loaded.metrics == resolution.metrics


def test_review_schemas_exclude_causal_proof_level() -> None:
    phase_a_schema = phase_a_review_schema()
    phase_b_schema = phase_b_review_schema()
    encoded = canonical_json_bytes(
        {"phase_a": phase_a_schema, "phase_b": phase_b_schema}, newline=False
    )

    assert b"CAUSAL" not in encoded
    assert b"STRONG_OBSERVED_CONTRIBUTION" in encoded
    assert phase_a_schema["additionalProperties"] is False
    assert phase_b_schema["additionalProperties"] is False
    phase_b_chain_schema = phase_b_schema["properties"]["chains"]["items"]
    evaluator_predicate_schema = phase_b_chain_schema["properties"]["evaluator_predicate"]
    assert "evaluator_evidence" in evaluator_predicate_schema["required"]
    assert (
        evaluator_predicate_schema["properties"]["evaluator_evidence"]["additionalProperties"]
        is False
    )


def test_review_schemas_type_every_const_and_enum_for_provider() -> None:
    _assert_provider_typed_const_and_enum(phase_a_review_schema())
    _assert_provider_typed_const_and_enum(phase_b_review_schema())


def test_prefix_competing_defect_is_frozen_and_blocks_strong_level(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    for reviews in (primary, secondary):
        failure_review = next(review for review in reviews if review["catalog_index"] == 1)
        failure_review["chains"][0]["competing_trace_defects"] = [
            {
                "defect_id": "prefix-independent-defect",
                "first_step": 1,
                "description": (
                    "An independent task-defeating defect is visible before the reuse target."
                ),
                "evidence_step_ids": ["step-1"],
            }
        ]

    assert len(validate_phase_a_reviews(primary, phase_a.cards)) == 2
    failure_card_a = next(card for card in phase_a.cards if card["task"]["catalog_index"] == 1)
    primary_failure = next(review for review in primary if review["catalog_index"] == 1)
    changed_description = copy.deepcopy(
        next(review for review in secondary if review["catalog_index"] == 1)
    )
    changed_description["chains"][0]["competing_trace_defects"][0]["description"] = (
        "A materially different account of the prefix defect."
    )
    assert phase_a_material_disagreement(primary_failure, changed_description, failure_card_a)
    phase_a_resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])
    phase_b = build_phase_b_bundle(phase_a, phase_a_resolution, source_base=source_base)
    reviews = [_phase_b_review(card) for card in phase_b.cards]

    with pytest.raises(FailureAttributionError, match="strong observed contribution"):
        validate_phase_b_reviews(reviews, phase_b.cards)

    failure_review = next(review for review in reviews if review["catalog_index"] == 1)
    failure_review["chains"][0].update(
        {
            "alternative_sufficient_failure": "PRESENT",
            "alternative_defect_ids": ["prefix-independent-defect"],
            "failure_link_level": "PLAUSIBLE_OBSERVED_CONTRIBUTION",
        }
    )
    failure_review["task_failure_link_level"] = "PLAUSIBLE_OBSERVED_CONTRIBUTION"
    assert len(validate_phase_b_reviews(reviews, phase_b.cards)) == 2

    colliding_alternative = copy.deepcopy(failure_review)
    colliding_alternative["chains"][0]["evaluator_revealed_alternatives"] = [
        {
            "alternative_id": "prefix-independent-defect",
            "description": "A colliding evaluator alternative ID.",
            "evaluator_evidence": {
                "field_path": "task_ended.environment_evaluation.reason",
                "excerpt": "required predicate was not satisfied",
            },
        }
    ]
    failure_card_b = next(card for card in phase_b.cards if card["task"]["catalog_index"] == 1)
    with pytest.raises(FailureAttributionError, match="must not collide"):
        validate_phase_b_reviews([colliding_alternative], [failure_card_b])


def test_predicate_identity_and_concurrent_mechanism_gate_failure_link(tmp_path: Path) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    for reviews in (primary, secondary):
        failure_review = next(review for review in reviews if review["catalog_index"] == 1)
        failure_review["chains"][0]["target_contribution"] = "MERELY_CONCURRENT"
    phase_a_resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])
    phase_b = build_phase_b_bundle(phase_a, phase_a_resolution, source_base=source_base)
    reviews = [_phase_b_review(card) for card in phase_b.cards]
    failure_review = next(review for review in reviews if review["catalog_index"] == 1)
    failure_card = next(card for card in phase_b.cards if card["task"]["catalog_index"] == 1)
    failure_review["chains"][0]["failure_link_level"] = "CO_OCCURRENCE_ONLY"
    failure_review["task_failure_link_level"] = "CO_OCCURRENCE_ONLY"
    assert len(validate_phase_b_reviews(reviews, phase_b.cards)) == 2

    mismatched_predicate = copy.deepcopy(failure_review)
    mismatched_predicate["chains"][0]["evaluator_predicate"]["affected_predicate_id"] = (
        "different-predicate"
    )
    with pytest.raises(FailureAttributionError, match="required-task-state"):
        validate_phase_b_reviews([mismatched_predicate], [failure_card])

    overstated = copy.deepcopy(failure_review)
    overstated["chains"][0]["failure_link_level"] = "STRONG_OBSERVED_CONTRIBUTION"
    overstated["task_failure_link_level"] = "STRONG_OBSERVED_CONTRIBUTION"
    with pytest.raises(FailureAttributionError) as exc_info:
        validate_phase_b_reviews([overstated], [failure_card])
    assert exc_info.value.code == "strong_observed_requirements"
    assert "target_contribution" in exc_info.value.context["mismatches"]


def test_evaluator_revealed_alternative_is_structured_and_blocks_strong(
    tmp_path: Path,
) -> None:
    source, source_base = _fixture_bundle(tmp_path)
    phase_a = build_phase_a_bundle([source])
    primary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-primary", review_prefix="primary"
    )
    secondary = _phase_a_reviews(
        phase_a.cards, reviewer_id="phase-a-secondary", review_prefix="secondary"
    )
    phase_a_resolution = resolve_phase_a_reviews(phase_a, primary, secondary, [])
    phase_b = build_phase_b_bundle(phase_a, phase_a_resolution, source_base=source_base)
    failure_card = next(card for card in phase_b.cards if card["outcome"]["outcome"] == "FAILURE")
    review = _phase_b_review(failure_card)
    review["chains"][0].update(
        {
            "alternative_sufficient_failure": "PRESENT",
            "failure_link_level": "PLAUSIBLE_OBSERVED_CONTRIBUTION",
            "evaluator_revealed_alternatives": [
                {
                    "alternative_id": "backend-independent-failure",
                    "description": (
                        "The evaluator reveals an independent backend predicate failure."
                    ),
                    "evaluator_evidence": {
                        "field_path": "task_ended.environment_evaluation.reason",
                        "excerpt": "required predicate was not satisfied",
                    },
                }
            ],
        }
    )
    review["task_failure_link_level"] = "PLAUSIBLE_OBSERVED_CONTRIBUTION"
    assert len(validate_phase_b_reviews([review], [failure_card])) == 1

    fabricated_alternative = copy.deepcopy(review)
    fabricated_alternative["chains"][0]["evaluator_revealed_alternatives"][0]["evaluator_evidence"][
        "excerpt"
    ] = "THIS TEXT IS NOT IN THE RAW EVENT"
    with pytest.raises(FailureAttributionError, match="real nonempty substring"):
        validate_phase_b_reviews([fabricated_alternative], [failure_card])

    alternative_description_disagreement = copy.deepcopy(review)
    alternative_description_disagreement["review_id"] = "different-evaluator-alternative"
    alternative_description_disagreement["reviewer_id"] = "different-reviewer"
    alternative_description_disagreement["chains"][0]["evaluator_revealed_alternatives"][0][
        "description"
    ] = "A materially different evaluator-revealed alternative."
    assert phase_b_material_disagreement(review, alternative_description_disagreement, failure_card)

    missing_evidence = copy.deepcopy(review)
    missing_evidence["chains"][0]["evaluator_revealed_alternatives"] = []
    with pytest.raises(FailureAttributionError, match="requires a Phase-A defect ID"):
        validate_phase_b_reviews([missing_evidence], [failure_card])

    overstated = copy.deepcopy(review)
    overstated["chains"][0]["failure_link_level"] = "STRONG_OBSERVED_CONTRIBUTION"
    overstated["task_failure_link_level"] = "STRONG_OBSERVED_CONTRIBUTION"
    with pytest.raises(FailureAttributionError, match="no sufficient alternative"):
        validate_phase_b_reviews([overstated], [failure_card])
