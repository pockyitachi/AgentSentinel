from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from mobile_world.offline.motivation_review import (
    EXPECTED_TASK_COUNT,
    REVIEW_SCHEMA_VERSION,
    ReviewValidationError,
    adjudication_needed,
    canonical_json_bytes,
    canonical_sha256,
    compute_metrics,
    derive_task_screen_class,
    load_canonical_json_line,
    select_pass2,
    validate_primary_coverage,
    validate_review_batch,
    validate_task_cards,
)

DATASET_SHA = "a" * 64
SELECTION_SHA = "b" * 64
RECONSTRUCTION_SHA = "c" * 64
SPAN_SHA = "d" * 64


def _card(
    index: int,
    *,
    capture_complete: bool = True,
    provenance: str = "EXACT",
) -> dict[str, Any]:
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
            "raw_run_id": f"raw-run-{(index - 1) // 40}",
            "source_id": f"source-{(index - 1) // 40}",
            "source_relative_run_path": (
                f"source-{(index - 1) // 40}/audit/raw/runs/raw-run-{(index - 1) // 40}"
            ),
            "task_stream_relative_path": f"tasks/task-run-{index:03d}/events.jsonl",
        },
        "outcome_blinded": True,
        "instruction": f"Complete fixture task {index}",
        "coverage": {
            "integrity_valid": True,
            "capture_complete": capture_complete,
            "decision_count": 2,
            "reconstructed_decision_count": 2,
            "history_bearing_decision_count": 1,
            "unique_history_claim_count": 1,
            "actual_exposure_count": 1,
            "scanner_candidate_count": 1,
            "dropped_candidate_count": 0,
            "full_reconstruction_sha256": RECONSTRUCTION_SHA,
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
                    "provenance_confidence": provenance,
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


def _cards(overrides: dict[int, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    result = {}
    for index in range(1, EXPECTED_TASK_COUNT + 1):
        kwargs = (overrides or {}).get(index, {})
        card = _card(index, **kwargs)
        result[card["task"]["task_name"]] = card
    return result


def _chain(
    screen: str,
    *,
    explicit: bool = False,
    harmful: bool = False,
    recovered: bool = False,
    confound: str = "NONE",
) -> dict[str, Any]:
    if screen == "POSITIVE":
        validity = "REFUTED"
        invalid_subtypes = ["FALSE_CLAIM"]
    elif screen == "UNCERTAIN":
        validity = "UNVERIFIABLE"
        invalid_subtypes = []
    else:
        validity = "SUPPORTED"
        invalid_subtypes = []
    if harmful:
        effects = ["WRONG_ACTION"]
        if recovered:
            effects = ["RECOVERED", "WRONG_ACTION"]
    else:
        effects = ["NO_VISIBLE_HARM"]
    return {
        "candidate_id": "candidate-1",
        "history_validity": validity,
        "invalid_subtypes": invalid_subtypes,
        "uptake_evidence": "EXPLICIT_USE" if explicit else "NO_OBSERVED_UPTAKE",
        "state_confound": confound,
        "downstream_effects": effects,
        "evidence_ref_ids": ["ref-source", "ref-target"],
        "confidence": "HIGH",
        "rationale": "Fixture evidence supports this label.",
    }


def _review(
    card: dict[str, Any],
    screen: str = "NEGATIVE",
    *,
    phase: str = "PASS1",
    reviewer: str = "reviewer-1",
    explicit: bool = False,
    harmful: bool = False,
    recovered: bool = False,
    confound: str = "NONE",
    coverage: str = "SUFFICIENT",
) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "task_review",
        "evaluation_run_id": card["evaluation_run_id"],
        "dataset_sha256": card["dataset_sha256"],
        "selection_sha256": card["selection_sha256"],
        "phase": phase,
        "review_id": f"{phase.lower()}-{reviewer}-{card['task']['catalog_index']}",
        "reviewer_id": reviewer,
        "task_name": card["task"]["task_name"],
        "catalog_index": card["task"]["catalog_index"],
        "card_sha256": canonical_sha256(card),
        "coverage_verdict": coverage,
        "chains": [
            _chain(
                screen,
                explicit=explicit,
                harmful=harmful,
                recovered=recovered,
                confound=confound,
            )
        ],
        "task_screen_class": screen,
        "summary": f"Fixture {screen.lower()} review.",
    }


def _hybrid_action_card(
    cards: dict[str, dict[str, Any]],
    index: int,
    *,
    text: str,
    parsed_action: str,
    source_result: str,
    instruction: str | None = None,
    ui_delta: str = "unchanged",
    claim_type: str = "ACTION_EXECUTION_CLAIM",
) -> dict[str, Any]:
    card = cards[f"Task{index:03d}"]
    if instruction is not None:
        card["instruction"] = instruction
    card["trajectory_outline"][0]["prediction_excerpt"] = text
    card["trajectory_outline"][0]["parsed_action"] = parsed_action
    card["trajectory_outline"][0]["ui_delta"] = ui_delta
    candidate = card["candidates"][0]
    candidate["claim"].update(
        {
            "text": text,
            "claim_type": claim_type,
            "representation_type": "hybrid_folding",
        }
    )
    candidate["evidence_refs"] = [
        {
            "ref_id": "ref-action",
            "role": "source_action",
            "event_id": f"event-action-{index}",
            "step": 1,
            "field_path": "payload.action",
            "blob_sha256": None,
            "excerpt": parsed_action,
        },
        {
            "ref_id": "ref-result",
            "role": "source_result",
            "event_id": f"event-result-{index}",
            "step": 1,
            "field_path": "payload.result",
            "blob_sha256": None,
            "excerpt": source_result,
        },
        {
            "ref_id": "ref-source",
            "role": "source_prediction",
            "event_id": f"event-source-{index}",
            "step": 1,
            "field_path": "payload.prediction_raw",
            "blob_sha256": None,
            "excerpt": text,
        },
        {
            "ref_id": "ref-target",
            "role": "target_request",
            "event_id": f"event-target-{index}",
            "step": 2,
            "field_path": "payload.request_view.messages[1].content[0]",
            "blob_sha256": None,
            "excerpt": text,
        },
    ]
    return card


def _hybrid_action_review(
    card: dict[str, Any],
    *,
    validity: str,
    invalid_subtypes: list[str],
    uptake: str = "NO_OBSERVED_UPTAKE",
    effects: list[str] | None = None,
) -> dict[str, Any]:
    review = _review(card)
    chain = review["chains"][0]
    chain.update(
        {
            "history_validity": validity,
            "invalid_subtypes": invalid_subtypes,
            "uptake_evidence": uptake,
            "downstream_effects": effects or ["NO_VISIBLE_HARM"],
            "evidence_ref_ids": sorted(
                ref["ref_id"] for ref in card["candidates"][0]["evidence_refs"]
            ),
        }
    )
    review["task_screen_class"] = derive_task_screen_class(
        review["coverage_verdict"], review["chains"]
    )
    return review


def _primary_reviews(
    cards: dict[str, dict[str, Any]],
    *,
    positives: set[int] | None = None,
    uncertain: set[int] | None = None,
) -> list[dict[str, Any]]:
    positive_indices = positives or set()
    uncertain_indices = uncertain or set()
    result = []
    for index in range(1, EXPECTED_TASK_COUNT + 1):
        card = cards[f"Task{index:03d}"]
        screen = (
            "POSITIVE"
            if index in positive_indices
            else "UNCERTAIN"
            if index in uncertain_indices
            else "NEGATIVE"
        )
        result.append(_review(card, screen))
    return result


def _outcomes(cards: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for task_name, card in cards.items():
        index = card["task"]["catalog_index"]
        success = index % 2 == 1
        result[task_name] = {
            "task_name": task_name,
            "catalog_index": index,
            "app": f"app-{index % 3}",
            "outcome": "SUCCESS" if success else "FAILURE",
            "score": 1.0 if success else 0.0,
        }
    return result


def _batch(reviews: list[dict[str, Any]], phase: str = "PASS1") -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "review_batch",
        "evaluation_run_id": "evaluation-1",
        "dataset_sha256": DATASET_SHA,
        "selection_sha256": SELECTION_SHA,
        "batch_id": "batch-001",
        "phase": phase,
        "reviews": reviews,
    }


def test_canonical_json_round_trip_is_strict() -> None:
    value = {"z": "中文", "a": [1, True, None]}
    encoded = canonical_json_bytes(value)
    assert encoded == b'{"a":[1,true,null],"z":"\xe4\xb8\xad\xe6\x96\x87"}\n'
    assert load_canonical_json_line(encoded) == value

    with pytest.raises(ReviewValidationError, match="canonical"):
        load_canonical_json_line(b'{"z":1, "a":2}\n')
    with pytest.raises(ReviewValidationError, match="duplicate"):
        load_canonical_json_line(b'{"a":1,"a":2}\n')
    with pytest.raises(ReviewValidationError, match="finite"):
        canonical_json_bytes({"bad": math.inf})


def test_validate_cards_and_review_batch() -> None:
    cards = _cards()
    detached = validate_task_cards(cards)
    reviews = [_review(cards["Task001"]), _review(cards["Task002"])]
    validated = validate_review_batch(_batch(reviews), cards, "PASS1")

    assert len(detached) == EXPECTED_TASK_COUNT
    assert tuple(review["task_name"] for review in validated) == ("Task001", "Task002")
    assert validated[0] is not reviews[0]


def test_ui_venus_flat_previous_actions_uses_the_shared_review_contract() -> None:
    cards = _cards()
    cards["Task001"]["candidates"][0]["claim"]["representation_type"] = "flat_previous_actions"

    validated = validate_task_cards(cards)

    assert (
        validated["Task001"]["candidates"][0]["claim"]["representation_type"]
        == "flat_previous_actions"
    )


def test_gui_owl_hybrid_folding_uses_the_shared_review_contract() -> None:
    cards = _cards()
    cards["Task001"]["candidates"][0]["claim"]["representation_type"] = "hybrid_folding"

    validated = validate_task_cards(cards)

    assert validated["Task001"]["candidates"][0]["claim"]["representation_type"] == "hybrid_folding"


@pytest.mark.parametrize(
    ("index", "text", "parsed_action", "source_result"),
    [
        (111, "Ask the user", "ask_user(text='Which account?')", "ask_user_response='Work'"),
        (112, "向上滚动", "scroll(direction='up')", "None"),
        (
            113,
            "Drag from (100, 900) to (100, 300)",
            "drag(start=(100,900), end=(100,300))",
            "None",
        ),
    ],
)
def test_hybrid_action_execution_records_accept_supported_short_imperatives(
    index: int,
    text: str,
    parsed_action: str,
    source_result: str,
) -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        index,
        text=text,
        parsed_action=parsed_action,
        source_result=source_result,
    )
    review = _hybrid_action_review(card, validity="SUPPORTED", invalid_subtypes=[])

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["history_validity"] == "SUPPORTED"
    assert card["trajectory_outline"][0]["ui_delta"] == "unchanged"


@pytest.mark.parametrize(
    ("text", "parsed_action", "source_result"),
    [
        ("Drag from (100, 900) to (100, 300)", "click(x=100,y=900)", "None"),
        (
            "Drag from (100, 900) to (100, 300)",
            "drag(start=(100,900), end=(500,300))",
            "None",
        ),
        ("向上滚动", "scroll(direction='down')", "None"),
        (
            "Ask the user. Tool response: Work",
            "ask_user(text='Which account?')",
            "ask_user_response='Personal'",
        ),
    ],
)
def test_hybrid_action_or_result_mismatch_accepts_result_misalignment_refutation(
    text: str,
    parsed_action: str,
    source_result: str,
) -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text=text,
        parsed_action=parsed_action,
        source_result=source_result,
    )
    review = _hybrid_action_review(
        card,
        validity="REFUTED",
        invalid_subtypes=["RESULT_MISALIGNMENT"],
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["invalid_subtypes"] == ["RESULT_MISALIGNMENT"]


def test_accurately_recorded_but_offtrack_hybrid_action_is_not_refuted() -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="Open the unrelated weather app",
        parsed_action="open_app(app_name='Weather')",
        source_result="None",
        instruction="Send the drafted email",
    )
    review = _hybrid_action_review(
        card,
        validity="OFFTRACK_TRUE",
        invalid_subtypes=["TRUE_BUT_OFFTRACK"],
        uptake="EXPLICIT_USE",
        effects=["OFFTRACK_CONTINUATION"],
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    chain = validated[0]["chains"][0]
    assert chain["history_validity"] == "OFFTRACK_TRUE"
    assert chain["uptake_evidence"] == "EXPLICIT_USE"
    assert chain["downstream_effects"] == ["OFFTRACK_CONTINUATION"]


def test_hybrid_prospective_intent_remains_not_a_factual_claim() -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="I will drag upward next",
        parsed_action="wait()",
        source_result="None",
        claim_type="ACTION_INTENT",
    )
    review = _hybrid_action_review(
        card,
        validity="NOT_A_FACTUAL_CLAIM",
        invalid_subtypes=[],
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["history_validity"] == "NOT_A_FACTUAL_CLAIM"


@pytest.mark.parametrize(
    ("validity", "invalid_subtypes"),
    [("SUPPORTED", []), ("REFUTED", ["FALSE_SUCCESS"])],
)
def test_hybrid_explicit_completion_claims_remain_factual(
    validity: str,
    invalid_subtypes: list[str],
) -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="The requested menu opened successfully",
        parsed_action="click(x=100,y=200)",
        source_result="None",
        claim_type="SUCCESS_CLAIM",
    )
    review = _hybrid_action_review(
        card,
        validity=validity,
        invalid_subtypes=invalid_subtypes,
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["history_validity"] == validity


def test_hybrid_action_with_false_embedded_effect_is_not_forced_to_result_misalignment() -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="Press Save; the draft is now saved",
        parsed_action="click(x=100,y=900)",
        source_result="None",
    )
    review = _hybrid_action_review(
        card,
        validity="REFUTED",
        invalid_subtypes=["FALSE_SUCCESS"],
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["invalid_subtypes"] == ["FALSE_SUCCESS"]


def test_hybrid_action_execution_claim_uses_the_generic_validity_contract() -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="Open the unrelated weather app",
        parsed_action="open_app(app_name='Weather')",
        source_result="None",
        instruction="Send the drafted email",
    )
    review = _hybrid_action_review(
        card,
        validity="OFFTRACK_TRUE",
        invalid_subtypes=["TRUE_BUT_OFFTRACK"],
    )

    validated = validate_review_batch(_batch([review]), cards, "PASS1")

    assert validated[0]["chains"][0]["history_validity"] == "OFFTRACK_TRUE"


def test_review_batch_rejects_unknown_keys_hashes_and_evidence_refs() -> None:
    cards = _cards()
    review = _review(cards["Task001"])
    bad_unknown = copy.deepcopy(review)
    bad_unknown["unexpected"] = True
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_review_batch(_batch([bad_unknown]), cards, "PASS1")
    assert exc_info.value.code == "object_keys"

    bad_hash = copy.deepcopy(review)
    bad_hash["card_sha256"] = "f" * 64
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_review_batch(_batch([bad_hash]), cards, "PASS1")
    assert exc_info.value.code == "review_card_hash_mismatch"

    bad_ref = copy.deepcopy(review)
    bad_ref["chains"][0]["evidence_ref_ids"] = ["missing-ref"]
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_review_batch(_batch([bad_ref]), cards, "PASS1")
    assert exc_info.value.code == "review_evidence_reference"


def test_exact_117_card_and_primary_coverage_is_required() -> None:
    cards = _cards()
    missing_cards = dict(cards)
    missing_cards.pop("Task117")
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_task_cards(missing_cards)
    assert exc_info.value.code == "card_coverage_count"

    primary = _primary_reviews(cards)
    assert len(validate_primary_coverage(primary)) == EXPECTED_TASK_COUNT
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_primary_coverage(primary[:-1])
    assert exc_info.value.code == "primary_coverage_count"

    duplicate_index = copy.deepcopy(primary)
    duplicate_index[-1]["catalog_index"] = 116
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_primary_coverage(duplicate_index)
    assert exc_info.value.code == "catalog_index_duplicate"


def test_card_source_paths_are_relative_and_task_bound() -> None:
    cards = _cards()
    absolute = copy.deepcopy(cards)
    absolute["Task001"]["task"]["source_relative_run_path"] = "/raw/runs/run-1"
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_task_cards(absolute)
    assert exc_info.value.code == "relative_posix_path"

    wrong_stream = copy.deepcopy(cards)
    wrong_stream["Task001"]["task"]["task_stream_relative_path"] = (
        "tasks/different-run/events.jsonl"
    )
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_task_cards(wrong_stream)
    assert exc_info.value.code == "task_stream_relative_path"


def test_screen_class_is_machine_derived() -> None:
    cards = _cards()
    contradictory = _review(cards["Task001"], "NEGATIVE")
    contradictory["chains"][0]["history_validity"] = "REFUTED"
    contradictory["chains"][0]["invalid_subtypes"] = ["FALSE_CLAIM"]
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_review_batch(_batch([contradictory]), cards, "PASS1")
    assert exc_info.value.code == "screen_class_not_derived"


def test_select_pass2_is_deterministic_and_samples_exactly_15_percent() -> None:
    cards = _cards()
    primary = _primary_reviews(cards, positives={1, 2, 3, 4, 5}, uncertain={6, 7})
    outcomes = _outcomes(cards)

    first = select_pass2(primary, outcomes, DATASET_SHA, SELECTION_SHA)
    second = select_pass2(list(reversed(primary)), outcomes, DATASET_SHA, SELECTION_SHA)

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert first["primary_counts"] == {"positive": 5, "uncertain": 2, "negative": 110}
    assert first["negative_sample_count"] == 17
    assert first["selected_task_count"] == 24
    assert first["negative_rate"] == {"numerator": 3, "denominator": 20}
    selected = {record["catalog_index"] for record in first["tasks"]}
    assert set(range(1, 8)) <= selected
    assert (
        sum(record["selection_reason"] == "NEGATIVE_RANDOM_AUDIT" for record in first["tasks"])
        == 17
    )


@pytest.mark.parametrize("rate", [0.09, 0.21, math.nan])
def test_select_pass2_rejects_out_of_policy_rates(rate: float) -> None:
    cards = _cards()
    primary = _primary_reviews(cards)
    with pytest.raises(ReviewValidationError):
        select_pass2(primary, _outcomes(cards), DATASET_SHA, SELECTION_SHA, rate=rate)


def test_adjudication_ignores_prose_but_detects_metric_disagreement() -> None:
    card = _card(1)
    first = _review(card, "POSITIVE", explicit=True)
    second = _review(
        card,
        "POSITIVE",
        phase="PASS2",
        reviewer="reviewer-2",
        explicit=True,
    )
    second["summary"] = "Independent prose can differ."
    second["chains"][0]["rationale"] = "Different prose, same frozen labels."
    assert adjudication_needed(first, second) is False

    second["chains"][0]["downstream_effects"] = ["WRONG_ACTION"]
    assert adjudication_needed(first, second) is True


def test_adjudication_requires_independent_reviewer() -> None:
    card = _card(1)
    first = _review(card)
    second = _review(card, phase="PASS2", reviewer="reviewer-1")
    with pytest.raises(ReviewValidationError) as exc_info:
        adjudication_needed(first, second)
    assert exc_info.value.code == "reviewer_not_independent"


def test_metrics_reach_strong_threshold_and_keep_successful_local_harm() -> None:
    cards = _cards()
    outcomes = _outcomes(cards)
    reviews = _primary_reviews(cards)
    for index in (1, 2, 3):
        reviews[index - 1] = _review(
            cards[f"Task{index:03d}"],
            "POSITIVE",
            explicit=True,
            harmful=index in {1, 2},
            recovered=index == 1,
        )

    metrics = compute_metrics(reviews, cards, outcomes)

    assert metrics["task_counts"]["strict_explicit_use"] == 3
    assert metrics["task_counts"]["strict_harm"] == 2
    assert metrics["task_counts"]["strict_harm_recovered"] == 1
    assert metrics["task_counts"]["successful_strict_harm"] == 1
    assert metrics["severity_counts"]["EXPLICIT_HARM"] == 2
    assert metrics["motivation_strength"] == {
        "level": "STRONG_OBSERVATIONAL",
        "rule": ">=3 independent strict-explicit tasks and >=2 strict-harm tasks",
        "causal_claim_supported": False,
    }


def test_metrics_distinguish_moderate_weak_and_not_supported() -> None:
    cards = _cards()
    outcomes = _outcomes(cards)

    moderate = _primary_reviews(cards)
    moderate[0] = _review(cards["Task001"], "POSITIVE", explicit=True)
    assert compute_metrics(moderate, cards, outcomes)["motivation_strength"]["level"] == (
        "MODERATE_OBSERVATIONAL"
    )

    weak = _primary_reviews(cards)
    weak[0] = _review(cards["Task001"], "POSITIVE")
    assert compute_metrics(weak, cards, outcomes)["motivation_strength"]["level"] == (
        "WEAK_OBSERVATIONAL"
    )

    unsupported = _primary_reviews(cards)
    assert compute_metrics(unsupported, cards, outcomes)["motivation_strength"]["level"] == (
        "NOT_SUPPORTED"
    )


def test_offtrack_action_keeps_uptake_effects_independent_and_strict_metrics_unchanged() -> None:
    cards = _cards()
    card = _hybrid_action_card(
        cards,
        1,
        text="Open the unrelated weather app",
        parsed_action="open_app(app_name='Weather')",
        source_result="None",
        instruction="Send the drafted email",
    )
    reviews = _primary_reviews(cards)
    reviews[0] = _hybrid_action_review(
        card,
        validity="OFFTRACK_TRUE",
        invalid_subtypes=["TRUE_BUT_OFFTRACK"],
        uptake="EXPLICIT_USE",
        effects=["OFFTRACK_CONTINUATION"],
    )

    metrics = compute_metrics(reviews, cards, _outcomes(cards))

    assert metrics["task_counts"]["confirmed_offtrack"] == 1
    assert metrics["task_counts"]["confirmed_primary_invalid"] == 0
    assert metrics["task_counts"]["strict_explicit_use"] == 0
    assert metrics["task_counts"]["strict_harm"] == 0
    assert metrics["motivation_strength"]["causal_claim_supported"] is False


def test_state_confound_and_ambiguous_provenance_are_not_strict() -> None:
    cards = _cards({1: {"provenance": "AMBIGUOUS"}})
    outcomes = _outcomes(cards)
    reviews = _primary_reviews(cards)
    reviews[0] = _review(cards["Task001"], "POSITIVE", explicit=True, harmful=True)
    reviews[1] = _review(
        cards["Task002"],
        "POSITIVE",
        explicit=True,
        harmful=True,
        confound="CURRENT_GUI_REINFORCES_SAME_PREMISE",
    )

    metrics = compute_metrics(reviews, cards, outcomes)

    assert metrics["task_counts"]["broad_explicit_use"] == 2
    assert metrics["task_counts"]["strict_explicit_use"] == 0
    assert metrics["task_counts"]["strict_harm"] == 0


def test_incomplete_coverage_forces_inconclusive_strength() -> None:
    cards = _cards({1: {"capture_complete": False}})
    outcomes = _outcomes(cards)
    reviews = _primary_reviews(cards)
    reviews[0] = _review(
        cards["Task001"],
        "POSITIVE",
        explicit=True,
        harmful=True,
        coverage="INSUFFICIENT",
    )

    metrics = compute_metrics(reviews, cards, outcomes)

    assert metrics["incomplete_task_count"] == 1
    assert metrics["incomplete_tasks"] == ["Task001"]
    assert metrics["motivation_strength"]["level"] == "INCONCLUSIVE"


def test_recovered_requires_a_concrete_harm_type() -> None:
    cards = _cards()
    review = _review(cards["Task001"], "POSITIVE")
    review["chains"][0]["downstream_effects"] = ["RECOVERED"]
    with pytest.raises(ReviewValidationError) as exc_info:
        validate_review_batch(_batch([review]), cards, "PASS1")
    assert exc_info.value.code == "recovered_without_harm"
