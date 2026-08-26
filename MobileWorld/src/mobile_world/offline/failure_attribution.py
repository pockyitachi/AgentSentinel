"""Two-phase, outcome-aware attribution cards for frozen strict-MHR reviews.

This module is an offline derived-data layer.  Phase A selects every task with
at least one frozen strict-MHR chain and exposes a lossless projection of the
full trajectory, while reserving target-to-terminal evidence for the chain's
main recovery judgment.  It deliberately does not read outcomes or evaluator
payloads.  Phase B can be built only after complete primary/secondary/material-
adjudication Phase-A reviews are frozen; it then joins outcomes and digest-bound
``task_ended`` evaluator evidence for the full pool, including success controls.

The original motivation card, review chain, and their canonical hashes are
copied into each card.  Neither phase can relabel MHR or MHR-OH.  Natural-run
evidence supports observational contribution levels only, never causal proof.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mobile_world.offline.motivation_review import (
    HARMFUL_EFFECTS,
    REVIEW_SCHEMA_VERSION,
    canonical_json_bytes,
    canonical_sha256,
    compute_metrics,
    load_canonical_json_line,
    validate_task_cards,
)

ATTRIBUTION_SCHEMA_VERSION = "mobileworld.audit.failure-attribution/v1"

PHASE_A_RECORD_TYPE = "failure_attribution_phase_a_card"
PHASE_A_REVIEW_RECORD_TYPE = "failure_attribution_phase_a_review"
PHASE_B_RECORD_TYPE = "failure_attribution_phase_b_card"
PHASE_B_REVIEW_RECORD_TYPE = "failure_attribution_phase_b_review"

RECOVERY_STATUSES = frozenset({"RECOVERED", "NOT_RECOVERED", "NO_HARM_TO_RECOVER", "UNKNOWN"})
CONTINUITY_STATUSES = frozenset({"CONTINUOUS", "INTERRUPTED", "NOT_APPLICABLE", "UNKNOWN"})
FINAL_OBSERVABLE_PREDICATES = frozenset(
    {"SATISFIED", "UNSATISFIED", "PARTIAL", "NOT_OBSERVABLE", "UNKNOWN"}
)
TARGET_CONTRIBUTIONS = frozenset(
    {
        "CREATES_DEFECT",
        "PRESERVES_DEFECT_BY_FALSE_COMPLETION",
        "AMPLIFIES",
        "ABANDONS_SUBGOAL",
        "UNBROKEN_LOOP",
        "MERELY_CONCURRENT",
        "UNKNOWN",
    }
)
FULL_TRAJECTORY_EVIDENCE = "FULL_RECONSTRUCTION_PROJECTION"
SUCCESS_CONTROL = "NOT_APPLICABLE_SUCCESS_CONTROL"
VERIFIER_ALIGNMENTS = frozenset({"DIRECT", "INDIRECT", "NONE", "UNKNOWN", SUCCESS_CONTROL})
ALTERNATIVE_SUFFICIENT_FAILURES = frozenset({"PRESENT", "ABSENT", "UNKNOWN", SUCCESS_CONTROL})
FAILURE_LINK_LEVELS = frozenset(
    {
        "CO_OCCURRENCE_ONLY",
        "PLAUSIBLE_OBSERVED_CONTRIBUTION",
        "STRONG_OBSERVED_CONTRIBUTION",
        "INDETERMINATE",
        SUCCESS_CONTROL,
    }
)
CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW"})

STRICT_MHR_DEFINITION = {
    "definition_source": REVIEW_SCHEMA_VERSION,
    "coverage_verdict": "SUFFICIENT",
    "mechanical_coverage_required": True,
    "actual_request_exposure_required": True,
    "claim_provenance_confidence": ["EXACT", "HIGH"],
    "history_validity": ["REFUTED", "STALE"],
    "uptake_evidence": "EXPLICIT_USE",
    "state_confound": ["CURRENT_GUI_CONTRADICTS_PREMISE", "NONE"],
}

ATTRIBUTION_RUBRIC = {
    "causal_claim_supported": False,
    "interpretation": (
        "Natural-run cards can establish co-occurrence or observed contribution, "
        "not counterfactual causation."
    ),
    "phase_a": {
        "selection": "all frozen strict-MHR tasks, including success controls",
        "outcome_blinded": True,
        "fields": [
            "recovery_status",
            "continuity_status",
            "final_observable_predicate",
            "affected_predicate",
            "target_contribution",
            "competing_trace_defects",
        ],
    },
    "phase_b": {
        "selection": "all Phase-A tasks after Phase-A reviews are frozen",
        "fields": [
            "recovery_status",
            "evaluator_predicate",
            "verifier_alignment",
            "alternative_sufficient_failure",
            "evaluator_revealed_alternatives",
            "failure_link_level",
        ],
        "highest_supported_level": "STRONG_OBSERVED_CONTRIBUTION",
        "alternative_absent_gate": (
            "ABSENT is valid only with FULL_RECONSTRUCTION_PROJECTION prefix evidence"
        ),
        "strong_competing_defect_gate": (
            "STRONG_OBSERVED_CONTRIBUTION is invalid when Phase A froze any competing trace defect"
        ),
    },
}

_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PREDICATE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ALTERNATIVE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EVALUATOR_EVIDENCE_FIELDS = frozenset(
    {
        "task_ended.environment_evaluation.reason",
        "task_ended.environment_evaluation.exception",
        "task_ended.environment_evaluation.score",
        "task_ended.termination",
    }
)
_SCORE_EVALUATOR_EVIDENCE_FIELD = "task_ended.environment_evaluation.score"
_STRICT_PROVENANCE = frozenset({"EXACT", "HIGH"})
_PRIMARY_INVALID = frozenset({"REFUTED", "STALE"})
_LOW_CONFOUND = frozenset({"NONE", "CURRENT_GUI_CONTRADICTS_PREMISE"})
_PHASE_B_PRECEDENCE = {
    SUCCESS_CONTROL: -1,
    "CO_OCCURRENCE_ONLY": 0,
    "INDETERMINATE": 1,
    "PLAUSIBLE_OBSERVED_CONTRIBUTION": 2,
    "STRONG_OBSERVED_CONTRIBUTION": 3,
}


class FailureAttributionError(ValueError):
    """An attribution input or derived record violates the v1 contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "$",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{path}: {message}")
        self.code = code
        self.path = path
        self.context = dict(context or {})


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """Paths for one frozen model audit bundle."""

    model_id: str
    root: Path
    cards_path: Path
    reviews_path: Path
    reconstruction_path: Path
    outcomes_path: Path

    @classmethod
    def from_root(cls, model_id: str, root: Path) -> SourceBundle:
        _validate_model_id(model_id)
        resolved = root.resolve(strict=True)
        review_paths = sorted(resolved.glob("review*/final/reviews.jsonl"))
        if len(review_paths) != 1:
            _fail(
                "final_review_path",
                "bundle root must contain exactly one review*/final/reviews.jsonl",
                path=str(resolved),
                matches=[str(path) for path in review_paths],
            )
        return cls(
            model_id=model_id,
            root=resolved,
            cards_path=resolved / "cards" / "task_cards.jsonl",
            reviews_path=review_paths[0],
            reconstruction_path=resolved / "cards" / "reconstruction_refs.jsonl",
            outcomes_path=resolved / "cards" / "outcomes.sidecar.jsonl",
        )


@dataclass(frozen=True, slots=True)
class PhaseABundle:
    manifest: dict[str, Any]
    cards: tuple[dict[str, Any], ...]
    sources: tuple[_LoadedSource, ...]


@dataclass(frozen=True, slots=True)
class PhaseBBundle:
    manifest: dict[str, Any]
    cards: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PhaseAResolution:
    """Frozen primary/secondary/adjudication resolution for Phase A."""

    manifest: dict[str, Any]
    primary_reviews: tuple[dict[str, Any], ...]
    secondary_reviews: tuple[dict[str, Any], ...]
    adjudication_reviews: tuple[dict[str, Any], ...]
    final_reviews: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PhaseBResolution:
    """Frozen primary/secondary/adjudication resolution for Phase B."""

    manifest: dict[str, Any]
    primary_reviews: tuple[dict[str, Any], ...]
    secondary_reviews: tuple[dict[str, Any], ...]
    adjudication_reviews: tuple[dict[str, Any], ...]
    final_reviews: tuple[dict[str, Any], ...]
    metrics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LoadedSource:
    spec: SourceBundle
    cards: dict[str, dict[str, Any]]
    reviews: dict[str, dict[str, Any]]
    reconstructions: dict[str, dict[str, Any]]
    cards_sha256: str
    reviews_sha256: str
    reconstruction_sha256: str


def build_phase_a_bundle(source_bundles: Sequence[SourceBundle]) -> PhaseABundle:
    """Build outcome-blind cards for every frozen strict-MHR task."""

    specs = _validate_source_specs(source_bundles)
    loaded = tuple(_load_phase_a_source(spec) for spec in specs)
    source_manifest = [_phase_a_source_manifest(source) for source in loaded]
    run_seed = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "phase": "A",
        "sources": source_manifest,
        "strict_mhr_definition": STRICT_MHR_DEFINITION,
    }
    run_id = f"failure-attribution-a-{canonical_sha256(run_seed)[:24]}"

    cards: list[dict[str, Any]] = []
    per_model: dict[str, dict[str, int]] = {}
    strict_chain_count = 0
    strict_mhr_oh_chain_count = 0
    for source in loaded:
        model_task_count = 0
        model_chain_count = 0
        model_oh_chain_count = 0
        for task_name, card in sorted(
            source.cards.items(), key=lambda item: item[1]["task"]["catalog_index"]
        ):
            review = source.reviews[task_name]
            strict_pairs = _strict_chain_pairs(card, review)
            if not strict_pairs:
                continue
            reconstruction = source.reconstructions[task_name]
            phase_a_card = _build_phase_a_card(
                run_id=run_id,
                model_id=source.spec.model_id,
                card=card,
                review=review,
                reconstruction=reconstruction,
                strict_pairs=strict_pairs,
            )
            cards.append(phase_a_card)
            model_task_count += 1
            model_chain_count += len(strict_pairs)
            model_oh_chain_count += sum(
                bool(set(chain["downstream_effects"]) & HARMFUL_EFFECTS)
                for _, chain in strict_pairs
            )
        per_model[source.spec.model_id] = {
            "strict_mhr_task_count": model_task_count,
            "strict_mhr_chain_count": model_chain_count,
            "strict_mhr_oh_chain_count": model_oh_chain_count,
        }
        strict_chain_count += model_chain_count
        strict_mhr_oh_chain_count += model_oh_chain_count

    cards.sort(key=_card_sort_key)
    card_set_sha256 = _record_set_sha256(cards)
    manifest = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_a_manifest",
        "attribution_run_id": run_id,
        "phase": "A",
        "outcome_blinded": True,
        "causal_claim_supported": False,
        "strict_mhr_definition": copy.deepcopy(STRICT_MHR_DEFINITION),
        "rubric": copy.deepcopy(ATTRIBUTION_RUBRIC["phase_a"]),
        "sources": source_manifest,
        "counts": {
            "source_count": len(loaded),
            "strict_mhr_task_count": len(cards),
            "strict_mhr_chain_count": strict_chain_count,
            "strict_mhr_oh_chain_count": strict_mhr_oh_chain_count,
            "strict_mhr_without_oh_chain_count": strict_chain_count - strict_mhr_oh_chain_count,
            "by_model": per_model,
        },
        "phase_a_card_set_sha256": card_set_sha256,
    }
    return PhaseABundle(manifest=manifest, cards=tuple(cards), sources=loaded)


def preflight_phase_b(
    phase_a_bundle: PhaseABundle,
    phase_a_resolution: PhaseAResolution,
    *,
    source_base: Path,
) -> dict[str, Any]:
    """Validate the Phase-B join after complete Phase-A reviews are frozen."""

    _validate_phase_a_resolution_binding(phase_a_resolution, phase_a_bundle)
    validated_reviews = validate_phase_a_reviews(
        phase_a_resolution.final_reviews, phase_a_bundle.cards
    )
    base = source_base.resolve(strict=True)
    card_keys = {card["task_key"] for card in phase_a_bundle.cards}
    failure_task_count = 0
    failure_chain_count = 0
    success_control_task_count = 0
    success_control_chain_count = 0
    raw_task_ended_count = 0
    all_failure_task_count = 0
    per_model: dict[str, dict[str, int]] = {}

    for source in phase_a_bundle.sources:
        outcomes, outcomes_sha256 = _load_outcomes(source)
        all_failure_task_count += sum(
            outcome["outcome"] == "FAILURE" for outcome in outcomes.values()
        )
        model_counts = {
            "all_failure_task_count": sum(
                outcome["outcome"] == "FAILURE" for outcome in outcomes.values()
            ),
            "failure_strict_mhr_task_count": 0,
            "failure_strict_mhr_chain_count": 0,
            "success_control_task_count": 0,
            "success_control_chain_count": 0,
            "validated_task_ended_count": 0,
        }
        for card in phase_a_bundle.cards:
            if card["model_id"] != source.spec.model_id:
                continue
            task_name = card["task"]["task_name"]
            outcome = outcomes[task_name]
            chain_count = len(card["frozen_strict_mhr_chains"])
            if outcome["outcome"] in {"FAILURE", "SUCCESS"}:
                _load_task_ended(
                    card=card,
                    reconstruction=source.reconstructions[task_name],
                    outcome=outcome,
                    source_base=base,
                )
                raw_task_ended_count += 1
                model_counts["validated_task_ended_count"] += 1
            if outcome["outcome"] == "FAILURE":
                failure_task_count += 1
                failure_chain_count += chain_count
                model_counts["failure_strict_mhr_task_count"] += 1
                model_counts["failure_strict_mhr_chain_count"] += chain_count
            elif outcome["outcome"] == "SUCCESS":
                success_control_task_count += 1
                success_control_chain_count += chain_count
                model_counts["success_control_task_count"] += 1
                model_counts["success_control_chain_count"] += chain_count
        model_counts["outcomes_sha256"] = outcomes_sha256  # type: ignore[assignment]
        per_model[source.spec.model_id] = model_counts

    if card_keys != {card["task_key"] for card in phase_a_bundle.cards}:
        _fail("phase_a_card_key", "Phase-A task keys changed during preflight")
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_b_preflight",
        "phase_a_run_id": phase_a_bundle.manifest["attribution_run_id"],
        "phase_a_resolution_id": phase_a_resolution.manifest["resolution_id"],
        "phase_a_card_set_sha256": phase_a_bundle.manifest["phase_a_card_set_sha256"],
        "phase_a_reviews_frozen": True,
        "phase_a_reviews_sha256": _record_set_sha256(
            tuple(validated_reviews[card["task_key"]] for card in phase_a_bundle.cards)
        ),
        "phase_b_cards_buildable": True,
        "causal_claim_supported": False,
        "counts": {
            "all_failure_task_count": all_failure_task_count,
            "failure_strict_mhr_task_count": failure_task_count,
            "failure_strict_mhr_chain_count": failure_chain_count,
            "success_control_task_count": success_control_task_count,
            "success_control_chain_count": success_control_chain_count,
            "validated_task_ended_count": raw_task_ended_count,
            "by_model": per_model,
        },
    }


def build_phase_b_bundle(
    phase_a_bundle: PhaseABundle,
    phase_a_resolution: PhaseAResolution,
    *,
    source_base: Path,
) -> PhaseBBundle:
    """Join outcomes/evaluator evidence after Phase-A reviews are frozen."""

    _validate_phase_a_resolution_binding(phase_a_resolution, phase_a_bundle)
    validated_reviews = validate_phase_a_reviews(
        phase_a_resolution.final_reviews, phase_a_bundle.cards
    )
    ordered_reviews = tuple(validated_reviews[card["task_key"]] for card in phase_a_bundle.cards)
    phase_a_reviews_sha256 = _record_set_sha256(ordered_reviews)
    base = source_base.resolve(strict=True)
    source_by_model = {source.spec.model_id: source for source in phase_a_bundle.sources}
    outcomes_by_model: dict[str, dict[str, dict[str, Any]]] = {}
    outcome_hashes: dict[str, str] = {}
    all_failure_task_count = 0
    for source in phase_a_bundle.sources:
        outcomes, digest = _load_outcomes(source)
        outcomes_by_model[source.spec.model_id] = outcomes
        outcome_hashes[source.spec.model_id] = digest
        all_failure_task_count += sum(
            record["outcome"] == "FAILURE" for record in outcomes.values()
        )

    run_seed = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "phase": "B",
        "phase_a_card_set_sha256": phase_a_bundle.manifest["phase_a_card_set_sha256"],
        "phase_a_reviews_sha256": phase_a_reviews_sha256,
        "phase_a_resolution_manifest_sha256": canonical_sha256(phase_a_resolution.manifest),
        "outcomes_sha256_by_model": outcome_hashes,
    }
    run_id = f"failure-attribution-b-{canonical_sha256(run_seed)[:24]}"
    cards: list[dict[str, Any]] = []
    chain_count = 0
    for phase_a_card in phase_a_bundle.cards:
        model_id = phase_a_card["model_id"]
        task_name = phase_a_card["task"]["task_name"]
        outcome = outcomes_by_model[model_id][task_name]
        source = source_by_model[model_id]
        task_ended = _load_task_ended(
            card=phase_a_card,
            reconstruction=source.reconstructions[task_name],
            outcome=outcome,
            source_base=base,
        )
        phase_a_review = validated_reviews[phase_a_card["task_key"]]
        cards.append(
            _build_phase_b_card(
                run_id=run_id,
                phase_a_card=phase_a_card,
                phase_a_review=phase_a_review,
                outcome=outcome,
                task_ended=task_ended,
            )
        )
        chain_count += len(phase_a_card["frozen_strict_mhr_chains"])

    cards.sort(key=_card_sort_key)
    per_model: dict[str, dict[str, int]] = {}
    for model_id in sorted(source_by_model):
        model_cards = [card for card in cards if card["model_id"] == model_id]
        failure_cards = [card for card in model_cards if card["outcome"]["outcome"] == "FAILURE"]
        success_cards = [card for card in model_cards if card["outcome"]["outcome"] == "SUCCESS"]
        per_model[model_id] = {
            "phase_b_task_count": len(model_cards),
            "phase_b_chain_count": sum(
                len(card["frozen_strict_mhr_chains"]) for card in model_cards
            ),
            "failure_strict_mhr_task_count": len(failure_cards),
            "failure_strict_mhr_chain_count": sum(
                len(card["frozen_strict_mhr_chains"]) for card in failure_cards
            ),
            "success_control_task_count": len(success_cards),
            "success_control_chain_count": sum(
                len(card["frozen_strict_mhr_chains"]) for card in success_cards
            ),
        }
    manifest = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_b_manifest",
        "attribution_run_id": run_id,
        "phase": "B",
        "phase_a_run_id": phase_a_bundle.manifest["attribution_run_id"],
        "phase_a_resolution_id": phase_a_resolution.manifest["resolution_id"],
        "phase_a_resolution_manifest_sha256": canonical_sha256(phase_a_resolution.manifest),
        "phase_a_card_set_sha256": phase_a_bundle.manifest["phase_a_card_set_sha256"],
        "phase_a_reviews_sha256": phase_a_reviews_sha256,
        "phase_b_card_set_sha256": _record_set_sha256(cards),
        "causal_claim_supported": False,
        "rubric": copy.deepcopy(ATTRIBUTION_RUBRIC["phase_b"]),
        "outcomes_sha256_by_model": dict(sorted(outcome_hashes.items())),
        "counts": {
            "all_failure_task_count": all_failure_task_count,
            "phase_b_task_count": len(cards),
            "phase_b_chain_count": chain_count,
            "failure_strict_mhr_task_count": sum(
                card["outcome"]["outcome"] == "FAILURE" for card in cards
            ),
            "failure_strict_mhr_chain_count": sum(
                len(card["frozen_strict_mhr_chains"])
                for card in cards
                if card["outcome"]["outcome"] == "FAILURE"
            ),
            "success_control_task_count": sum(
                card["outcome"]["outcome"] == "SUCCESS" for card in cards
            ),
            "success_control_chain_count": sum(
                len(card["frozen_strict_mhr_chains"])
                for card in cards
                if card["outcome"]["outcome"] == "SUCCESS"
            ),
            "by_model": per_model,
        },
    }
    return PhaseBBundle(manifest=manifest, cards=tuple(cards))


def validate_phase_a_reviews(
    reviews: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate complete, outcome-blind Phase-A review coverage."""

    card_map = _card_map(cards, expected_record_type=PHASE_A_RECORD_TYPE)
    values = _sequence(reviews, "reviews")
    if len(values) != len(card_map):
        _fail(
            "phase_a_review_coverage",
            "Phase-A reviews must cover every Phase-A card exactly once",
            actual=len(values),
            expected=len(card_map),
        )
    result: dict[str, dict[str, Any]] = {}
    seen_review_ids: set[str] = set()
    for index, raw in enumerate(values):
        path = f"reviews[{index}]"
        review = _mapping(raw, path)
        _exact_keys(
            review,
            {
                "schema_version",
                "record_type",
                "attribution_run_id",
                "review_id",
                "reviewer_id",
                "task_key",
                "model_id",
                "task_name",
                "catalog_index",
                "phase_a_card_sha256",
                "outcome_blinded",
                "chains",
                "summary",
            },
            path=path,
        )
        _literal(review["schema_version"], ATTRIBUTION_SCHEMA_VERSION, f"{path}.schema_version")
        _literal(review["record_type"], PHASE_A_REVIEW_RECORD_TYPE, f"{path}.record_type")
        task_key = _nonempty_string(review["task_key"], f"{path}.task_key")
        card = card_map.get(task_key)
        if card is None:
            _fail("phase_a_review_task", "review references an unknown task_key", path=path)
        if task_key in result:
            _fail("phase_a_review_duplicate", "task_key is reviewed twice", path=path)
        _literal(
            review["attribution_run_id"],
            card["attribution_run_id"],
            f"{path}.attribution_run_id",
        )
        _identity_matches_card(review, card, path=path)
        _literal(
            review["phase_a_card_sha256"], canonical_sha256(card), f"{path}.phase_a_card_sha256"
        )
        _literal(review["outcome_blinded"], True, f"{path}.outcome_blinded")
        review_id = _nonempty_string(review["review_id"], f"{path}.review_id")
        if review_id in seen_review_ids:
            _fail("phase_a_review_id_duplicate", "review_id is duplicated", path=path)
        seen_review_ids.add(review_id)
        _nonempty_string(review["reviewer_id"], f"{path}.reviewer_id")
        _validate_phase_a_review_chains(review["chains"], card, path=f"{path}.chains")
        _bounded_string(review["summary"], f"{path}.summary", maximum=2000)
        result[task_key] = _detach(review)
    if set(result) != set(card_map):
        _fail("phase_a_review_coverage", "Phase-A review task coverage is incomplete")
    return result


def validate_phase_b_reviews(
    reviews: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Validate complete Phase-B outcome-aware review coverage and rubric rules."""

    card_map = _card_map(cards, expected_record_type=PHASE_B_RECORD_TYPE)
    values = _sequence(reviews, "reviews")
    if len(values) != len(card_map):
        _fail(
            "phase_b_review_coverage",
            "Phase-B reviews must cover every failure card and success control exactly once",
            actual=len(values),
            expected=len(card_map),
        )
    result: dict[str, dict[str, Any]] = {}
    seen_review_ids: set[str] = set()
    for index, raw in enumerate(values):
        path = f"reviews[{index}]"
        review = _mapping(raw, path)
        _exact_keys(
            review,
            {
                "schema_version",
                "record_type",
                "attribution_run_id",
                "review_id",
                "reviewer_id",
                "task_key",
                "model_id",
                "task_name",
                "catalog_index",
                "phase_b_card_sha256",
                "chains",
                "task_failure_link_level",
                "summary",
            },
            path=path,
        )
        _literal(review["schema_version"], ATTRIBUTION_SCHEMA_VERSION, f"{path}.schema_version")
        _literal(review["record_type"], PHASE_B_REVIEW_RECORD_TYPE, f"{path}.record_type")
        task_key = _nonempty_string(review["task_key"], f"{path}.task_key")
        card = card_map.get(task_key)
        if card is None:
            _fail("phase_b_review_task", "review references an unknown task_key", path=path)
        if task_key in result:
            _fail("phase_b_review_duplicate", "task_key is reviewed twice", path=path)
        _literal(
            review["attribution_run_id"],
            card["attribution_run_id"],
            f"{path}.attribution_run_id",
        )
        _identity_matches_card(review, card, path=path)
        _literal(
            review["phase_b_card_sha256"], canonical_sha256(card), f"{path}.phase_b_card_sha256"
        )
        review_id = _nonempty_string(review["review_id"], f"{path}.review_id")
        if review_id in seen_review_ids:
            _fail("phase_b_review_id_duplicate", "review_id is duplicated", path=path)
        seen_review_ids.add(review_id)
        _nonempty_string(review["reviewer_id"], f"{path}.reviewer_id")
        chain_levels = _validate_phase_b_review_chains(
            review["chains"], card, path=f"{path}.chains"
        )
        derived_level = max(chain_levels, key=_PHASE_B_PRECEDENCE.__getitem__)
        _literal(
            review["task_failure_link_level"],
            derived_level,
            f"{path}.task_failure_link_level",
        )
        _bounded_string(review["summary"], f"{path}.summary", maximum=2000)
        result[task_key] = _detach(review)
    if set(result) != set(card_map):
        _fail("phase_b_review_coverage", "Phase-B review task coverage is incomplete")
    return result


def phase_a_material_disagreement(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    card: Mapping[str, Any],
) -> bool:
    """Return whether two independent Phase-A reviews differ materially.

    Material fields are the per-chain recovery, continuity, atomic predicate,
    target-contribution mechanism, final observable predicate, and competing-
    defect identity/step/evidence tuple.  Confidence, prose rationale, and main
    supporting evidence selections are non-material when those labels agree.
    """

    first_validated = next(iter(validate_phase_a_reviews([first], [card]).values()))
    second_validated = next(iter(validate_phase_a_reviews([second], [card]).values()))
    _require_independent(first_validated, second_validated, phase="A")
    return _phase_a_material_signature(first_validated) != _phase_a_material_signature(
        second_validated
    )


def phase_b_material_disagreement(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    card: Mapping[str, Any],
) -> bool:
    """Return whether two independent Phase-B reviews differ materially.

    Material fields are per-chain recovery, frozen atomic-predicate identity
    and contribution mechanism, evaluator-predicate mapping, verifier
    alignment, alternative sufficient failure (including evaluator-revealed
    alternatives), failure-link level, referenced alternative defect IDs, and
    the derived task-level failure-link level.  Confidence, prose rationale,
    and supporting trace-step selections are non-material when those labels
    agree.
    """

    first_validated = next(iter(validate_phase_b_reviews([first], [card]).values()))
    second_validated = next(iter(validate_phase_b_reviews([second], [card]).values()))
    _require_independent(first_validated, second_validated, phase="B")
    return _phase_b_material_signature(first_validated) != _phase_b_material_signature(
        second_validated
    )


def resolve_phase_a_reviews(
    phase_a_bundle: PhaseABundle,
    primary_reviews: Sequence[Mapping[str, Any]],
    secondary_reviews: Sequence[Mapping[str, Any]],
    adjudication_reviews: Sequence[Mapping[str, Any]],
) -> PhaseAResolution:
    """Resolve two complete Phase-A passes plus exactly material adjudications."""

    primary = validate_phase_a_reviews(primary_reviews, phase_a_bundle.cards)
    secondary = validate_phase_a_reviews(secondary_reviews, phase_a_bundle.cards)
    ordered_cards = tuple(phase_a_bundle.cards)
    material_keys: set[str] = set()
    for card in ordered_cards:
        task_key = card["task_key"]
        _require_independent(primary[task_key], secondary[task_key], phase="A")
        if _phase_a_material_signature(primary[task_key]) != _phase_a_material_signature(
            secondary[task_key]
        ):
            material_keys.add(task_key)
    adjudications = _validate_subset_reviews(
        adjudication_reviews,
        cards=ordered_cards,
        required_keys=material_keys,
        phase="A",
    )
    _require_globally_unique_review_ids(primary, secondary, adjudications, phase="A")

    final_reviews: list[dict[str, Any]] = []
    resolution_records = []
    for card in ordered_cards:
        task_key = card["task_key"]
        first = primary[task_key]
        second = secondary[task_key]
        material = task_key in material_keys
        if material:
            selected = adjudications[task_key]
            _require_adjudicator_independent(selected, first, second, phase="A")
            selected_role = "ADJUDICATION"
        else:
            selected = first
            selected_role = "PRIMARY_AGREEMENT"
        final_reviews.append(selected)
        resolution_records.append(
            {
                "task_key": task_key,
                "primary_review_sha256": canonical_sha256(first),
                "secondary_review_sha256": canonical_sha256(second),
                "material_disagreement": material,
                "adjudication_review_sha256": (
                    canonical_sha256(adjudications[task_key]) if material else None
                ),
                "selected_role": selected_role,
                "final_review_sha256": canonical_sha256(selected),
            }
        )
    final_map = validate_phase_a_reviews(final_reviews, ordered_cards)
    ordered_primary = tuple(primary[card["task_key"]] for card in ordered_cards)
    ordered_secondary = tuple(secondary[card["task_key"]] for card in ordered_cards)
    ordered_adjudications = tuple(
        adjudications[card["task_key"]]
        for card in ordered_cards
        if card["task_key"] in adjudications
    )
    ordered_final = tuple(final_map[card["task_key"]] for card in ordered_cards)
    manifest_seed = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "phase": "A",
        "phase_a_manifest_sha256": canonical_sha256(phase_a_bundle.manifest),
        "phase_a_card_set_sha256": phase_a_bundle.manifest["phase_a_card_set_sha256"],
        "primary_reviews_sha256": _record_set_sha256(ordered_primary),
        "secondary_reviews_sha256": _record_set_sha256(ordered_secondary),
        "adjudication_reviews_sha256": _record_set_sha256(ordered_adjudications),
        "final_reviews_sha256": _record_set_sha256(ordered_final),
        "task_resolutions": resolution_records,
    }
    resolution_id = f"failure-attribution-a-resolution-{canonical_sha256(manifest_seed)[:24]}"
    manifest = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_a_resolution",
        "resolution_id": resolution_id,
        "attribution_run_id": phase_a_bundle.manifest["attribution_run_id"],
        "phase": "A",
        "phase_a_manifest_sha256": manifest_seed["phase_a_manifest_sha256"],
        "phase_a_card_set_sha256": manifest_seed["phase_a_card_set_sha256"],
        "review_set_hashes": {
            "primary_reviews_sha256": manifest_seed["primary_reviews_sha256"],
            "secondary_reviews_sha256": manifest_seed["secondary_reviews_sha256"],
            "adjudication_reviews_sha256": manifest_seed["adjudication_reviews_sha256"],
            "final_reviews_sha256": manifest_seed["final_reviews_sha256"],
        },
        "counts": {
            "task_count": len(ordered_cards),
            "strict_mhr_chain_count": sum(
                len(card["frozen_strict_mhr_chains"]) for card in ordered_cards
            ),
            "primary_review_count": len(ordered_primary),
            "secondary_review_count": len(ordered_secondary),
            "material_disagreement_task_count": len(material_keys),
            "adjudication_review_count": len(ordered_adjudications),
            "unresolved_task_count": 0,
        },
        "task_resolutions": resolution_records,
        "outcomes_opened": False,
        "causal_claim_supported": False,
    }
    return PhaseAResolution(
        manifest=manifest,
        primary_reviews=ordered_primary,
        secondary_reviews=ordered_secondary,
        adjudication_reviews=ordered_adjudications,
        final_reviews=ordered_final,
    )


def resolve_phase_b_reviews(
    phase_b_bundle: PhaseBBundle,
    primary_reviews: Sequence[Mapping[str, Any]],
    secondary_reviews: Sequence[Mapping[str, Any]],
    adjudication_reviews: Sequence[Mapping[str, Any]],
    *,
    all_failure_task_count: int,
) -> PhaseBResolution:
    """Resolve two complete Phase-B passes plus exactly material adjudications."""

    _literal(
        all_failure_task_count,
        phase_b_bundle.manifest["counts"]["all_failure_task_count"],
        "all_failure_task_count",
    )
    primary = validate_phase_b_reviews(primary_reviews, phase_b_bundle.cards)
    secondary = validate_phase_b_reviews(secondary_reviews, phase_b_bundle.cards)
    ordered_cards = tuple(phase_b_bundle.cards)
    material_keys: set[str] = set()
    for card in ordered_cards:
        task_key = card["task_key"]
        _require_independent(primary[task_key], secondary[task_key], phase="B")
        if _phase_b_material_signature(primary[task_key]) != _phase_b_material_signature(
            secondary[task_key]
        ):
            material_keys.add(task_key)
    adjudications = _validate_subset_reviews(
        adjudication_reviews,
        cards=ordered_cards,
        required_keys=material_keys,
        phase="B",
    )
    _require_globally_unique_review_ids(primary, secondary, adjudications, phase="B")

    final_reviews: list[dict[str, Any]] = []
    resolution_records = []
    for card in ordered_cards:
        task_key = card["task_key"]
        first = primary[task_key]
        second = secondary[task_key]
        material = task_key in material_keys
        if material:
            selected = adjudications[task_key]
            _require_adjudicator_independent(selected, first, second, phase="B")
            selected_role = "ADJUDICATION"
        else:
            selected = first
            selected_role = "PRIMARY_AGREEMENT"
        final_reviews.append(selected)
        resolution_records.append(
            {
                "task_key": task_key,
                "primary_review_sha256": canonical_sha256(first),
                "secondary_review_sha256": canonical_sha256(second),
                "material_disagreement": material,
                "adjudication_review_sha256": (
                    canonical_sha256(adjudications[task_key]) if material else None
                ),
                "selected_role": selected_role,
                "final_review_sha256": canonical_sha256(selected),
            }
        )
    final_map = validate_phase_b_reviews(final_reviews, ordered_cards)
    ordered_primary = tuple(primary[card["task_key"]] for card in ordered_cards)
    ordered_secondary = tuple(secondary[card["task_key"]] for card in ordered_cards)
    ordered_adjudications = tuple(
        adjudications[card["task_key"]]
        for card in ordered_cards
        if card["task_key"] in adjudications
    )
    ordered_final = tuple(final_map[card["task_key"]] for card in ordered_cards)
    metrics = compute_phase_b_metrics(
        ordered_final,
        ordered_cards,
        all_failure_task_count=all_failure_task_count,
    )
    manifest_seed = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "phase": "B",
        "phase_b_manifest_sha256": canonical_sha256(phase_b_bundle.manifest),
        "phase_b_card_set_sha256": phase_b_bundle.manifest["phase_b_card_set_sha256"],
        "primary_reviews_sha256": _record_set_sha256(ordered_primary),
        "secondary_reviews_sha256": _record_set_sha256(ordered_secondary),
        "adjudication_reviews_sha256": _record_set_sha256(ordered_adjudications),
        "final_reviews_sha256": _record_set_sha256(ordered_final),
        "metrics_sha256": canonical_sha256(metrics),
        "task_resolutions": resolution_records,
    }
    resolution_id = f"failure-attribution-b-resolution-{canonical_sha256(manifest_seed)[:24]}"
    manifest = {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_phase_b_resolution",
        "resolution_id": resolution_id,
        "attribution_run_id": phase_b_bundle.manifest["attribution_run_id"],
        "phase": "B",
        "phase_b_manifest_sha256": manifest_seed["phase_b_manifest_sha256"],
        "phase_b_card_set_sha256": manifest_seed["phase_b_card_set_sha256"],
        "review_set_hashes": {
            "primary_reviews_sha256": manifest_seed["primary_reviews_sha256"],
            "secondary_reviews_sha256": manifest_seed["secondary_reviews_sha256"],
            "adjudication_reviews_sha256": manifest_seed["adjudication_reviews_sha256"],
            "final_reviews_sha256": manifest_seed["final_reviews_sha256"],
            "metrics_sha256": manifest_seed["metrics_sha256"],
        },
        "counts": {
            "task_count": len(ordered_cards),
            "chain_count": sum(len(card["frozen_strict_mhr_chains"]) for card in ordered_cards),
            "all_failure_task_count": phase_b_bundle.manifest["counts"]["all_failure_task_count"],
            "failure_strict_mhr_task_count": phase_b_bundle.manifest["counts"][
                "failure_strict_mhr_task_count"
            ],
            "failure_strict_mhr_chain_count": phase_b_bundle.manifest["counts"][
                "failure_strict_mhr_chain_count"
            ],
            "success_control_task_count": phase_b_bundle.manifest["counts"][
                "success_control_task_count"
            ],
            "success_control_chain_count": phase_b_bundle.manifest["counts"][
                "success_control_chain_count"
            ],
            "primary_review_count": len(ordered_primary),
            "secondary_review_count": len(ordered_secondary),
            "material_disagreement_task_count": len(material_keys),
            "adjudication_review_count": len(ordered_adjudications),
            "unresolved_task_count": 0,
        },
        "task_resolutions": resolution_records,
        "outcomes_opened": True,
        "causal_claim_supported": False,
    }
    return PhaseBResolution(
        manifest=manifest,
        primary_reviews=ordered_primary,
        secondary_reviews=ordered_secondary,
        adjudication_reviews=ordered_adjudications,
        final_reviews=ordered_final,
        metrics=metrics,
    )


def compute_phase_b_metrics(
    reviews: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
    *,
    all_failure_task_count: int,
) -> dict[str, Any]:
    """Compute reproducible observational counts; causal counts remain null."""

    if isinstance(all_failure_task_count, bool) or not isinstance(all_failure_task_count, int):
        _fail("failure_denominator", "all_failure_task_count must be an integer")
    validated = validate_phase_b_reviews(reviews, cards)
    cards_by_key = {card["task_key"]: card for card in cards}
    failure_reviews = {
        task_key: review
        for task_key, review in validated.items()
        if cards_by_key[task_key]["outcome"]["outcome"] == "FAILURE"
    }
    if all_failure_task_count < len(failure_reviews):
        _fail(
            "failure_denominator",
            "all_failure_task_count cannot be smaller than the failure strict-MHR pool",
        )
    counts = Counter(review["task_failure_link_level"] for review in failure_reviews.values())
    strong = counts["STRONG_OBSERVED_CONTRIBUTION"]
    plausible_or_strong = strong + counts["PLAUSIBLE_OBSERVED_CONTRIBUTION"]
    pool_count = len(failure_reviews)
    linked_chain_classes = {
        "strict_mhr_any_chain": {"strong": 0, "plausible_or_strong": 0},
        "strict_mhr_oh_chain": {"strong": 0, "plausible_or_strong": 0},
        "strict_mhr_non_oh_chain": {"strong": 0, "plausible_or_strong": 0},
    }
    for task_key, review in failure_reviews.items():
        frozen_by_candidate = {
            chain["candidate_id"]: chain
            for chain in cards_by_key[task_key]["frozen_strict_mhr_chains"]
        }
        chain_rows = [
            (
                chain["failure_link_level"],
                frozen_by_candidate[chain["candidate_id"]]["strict_mhr_oh"],
            )
            for chain in review["chains"]
        ]
        for class_name, class_filter in (
            ("strict_mhr_any_chain", lambda _is_oh: True),
            ("strict_mhr_oh_chain", bool),
            ("strict_mhr_non_oh_chain", lambda is_oh: not is_oh),
        ):
            class_levels = [level for level, is_oh in chain_rows if class_filter(is_oh)]
            if "STRONG_OBSERVED_CONTRIBUTION" in class_levels:
                linked_chain_classes[class_name]["strong"] += 1
            if any(
                level
                in {
                    "PLAUSIBLE_OBSERVED_CONTRIBUTION",
                    "STRONG_OBSERVED_CONTRIBUTION",
                }
                for level in class_levels
            ):
                linked_chain_classes[class_name]["plausible_or_strong"] += 1

    class_metrics = {
        class_name: {
            "strong_observed_contribution": _count_and_rates(
                class_counts["strong"],
                pool_count=pool_count,
                all_failure_count=all_failure_task_count,
            ),
            "plausible_or_strong_observed_contribution": _count_and_rates(
                class_counts["plausible_or_strong"],
                pool_count=pool_count,
                all_failure_count=all_failure_task_count,
            ),
        }
        for class_name, class_counts in linked_chain_classes.items()
    }
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": "failure_attribution_metrics",
        "causal_claim_supported": False,
        "causal_failure_task_count": None,
        "causal_failure_percentage_of_all_failures": None,
        "denominators": {
            "all_failure_task_count": all_failure_task_count,
            "failure_strict_mhr_task_count": pool_count,
            "success_control_task_count": len(validated) - pool_count,
        },
        "task_counts_by_failure_link_level": {
            level: counts[level] for level in sorted(FAILURE_LINK_LEVELS)
        },
        "strong_observed_contribution": _count_and_rates(
            strong, pool_count=pool_count, all_failure_count=all_failure_task_count
        ),
        "plausible_or_strong_observed_contribution": _count_and_rates(
            plausible_or_strong,
            pool_count=pool_count,
            all_failure_count=all_failure_task_count,
        ),
        "observed_contribution_by_linked_chain_class": {
            "task_classes_may_overlap": True,
            "interpretation": (
                "Each task is counted once per class when at least one chain of that frozen "
                "class reaches the stated level; OH and non-OH task classes may overlap."
            ),
            **class_metrics,
        },
    }


def phase_a_review_schema() -> dict[str, Any]:
    """Return the strict JSON Schema used for Phase-A review responses."""

    return _review_json_schema(phase="A")


def phase_b_review_schema() -> dict[str, Any]:
    """Return the strict JSON Schema used for Phase-B review responses."""

    return _review_json_schema(phase="B")


def write_phase_a_bundle(
    bundle: PhaseABundle,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a new Phase-A directory, or return its dry-run summary."""

    files = {
        "manifest.json": canonical_json_bytes(bundle.manifest),
        "cards.jsonl": b"".join(canonical_json_bytes(card) for card in bundle.cards),
        "review_schema.json": canonical_json_bytes(phase_a_review_schema()),
    }
    return _write_once(files, output_dir, dry_run=dry_run)


def write_phase_b_bundle(
    bundle: PhaseBBundle,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a new Phase-B directory, or return its dry-run summary."""

    files = {
        "manifest.json": canonical_json_bytes(bundle.manifest),
        "cards.jsonl": b"".join(canonical_json_bytes(card) for card in bundle.cards),
        "review_schema.json": canonical_json_bytes(phase_b_review_schema()),
    }
    return _write_once(files, output_dir, dry_run=dry_run)


def write_phase_a_resolution(
    resolution: PhaseAResolution,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Freeze all Phase-A review passes, resolutions, and selected finals."""

    files = _resolution_files(resolution, include_metrics=False)
    return _write_once(files, output_dir, dry_run=dry_run)


def write_phase_b_resolution(
    resolution: PhaseBResolution,
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Freeze all Phase-B review passes, resolutions, finals, and metrics."""

    files = _resolution_files(resolution, include_metrics=True)
    return _write_once(files, output_dir, dry_run=dry_run)


def load_phase_a_resolution(
    freeze_dir: Path,
    phase_a_bundle: PhaseABundle,
) -> PhaseAResolution:
    """Verify and load a complete Phase-A freeze before any outcome join."""

    stored_manifest = _read_canonical_json_object(freeze_dir / "manifest.json")
    primary, _ = _read_canonical_jsonl(freeze_dir / "primary_reviews.jsonl")
    secondary, _ = _read_canonical_jsonl(freeze_dir / "secondary_reviews.jsonl")
    adjudications, _ = _read_canonical_jsonl(
        freeze_dir / "adjudication_reviews.jsonl", allow_empty=True
    )
    final, _ = _read_canonical_jsonl(freeze_dir / "final_reviews.jsonl")
    rebuilt = resolve_phase_a_reviews(phase_a_bundle, primary, secondary, adjudications)
    if rebuilt.manifest != stored_manifest:
        _fail(
            "phase_a_freeze_manifest",
            "stored Phase-A freeze manifest differs from deterministic resolution",
            path=str(freeze_dir / "manifest.json"),
        )
    if tuple(final) != rebuilt.final_reviews:
        _fail(
            "phase_a_freeze_final",
            "stored Phase-A final reviews differ from deterministic resolution",
            path=str(freeze_dir / "final_reviews.jsonl"),
        )
    return rebuilt


def load_phase_b_resolution(
    freeze_dir: Path,
    phase_b_bundle: PhaseBBundle,
) -> PhaseBResolution:
    """Verify and load a complete Phase-B freeze and its metrics."""

    stored_manifest = _read_canonical_json_object(freeze_dir / "manifest.json")
    primary, _ = _read_canonical_jsonl(freeze_dir / "primary_reviews.jsonl")
    secondary, _ = _read_canonical_jsonl(freeze_dir / "secondary_reviews.jsonl")
    adjudications, _ = _read_canonical_jsonl(
        freeze_dir / "adjudication_reviews.jsonl", allow_empty=True
    )
    final, _ = _read_canonical_jsonl(freeze_dir / "final_reviews.jsonl")
    stored_metrics = _read_canonical_json_object(freeze_dir / "metrics.json")
    all_failure_task_count = phase_b_bundle.manifest["counts"]["all_failure_task_count"]
    rebuilt = resolve_phase_b_reviews(
        phase_b_bundle,
        primary,
        secondary,
        adjudications,
        all_failure_task_count=all_failure_task_count,
    )
    if rebuilt.manifest != stored_manifest:
        _fail(
            "phase_b_freeze_manifest",
            "stored Phase-B freeze manifest differs from deterministic resolution",
            path=str(freeze_dir / "manifest.json"),
        )
    if tuple(final) != rebuilt.final_reviews:
        _fail(
            "phase_b_freeze_final",
            "stored Phase-B final reviews differ from deterministic resolution",
            path=str(freeze_dir / "final_reviews.jsonl"),
        )
    if stored_metrics != rebuilt.metrics:
        _fail(
            "phase_b_freeze_metrics",
            "stored Phase-B metrics differ from deterministic recomputation",
            path=str(freeze_dir / "metrics.json"),
        )
    return rebuilt


def load_phase_a_reviews(path: Path) -> tuple[dict[str, Any], ...]:
    """Load canonical Phase-A review JSONL without validating card linkage."""

    records, _ = _read_canonical_jsonl(path)
    return tuple(records)


def _load_phase_a_source(spec: SourceBundle) -> _LoadedSource:
    card_records, cards_sha256 = _read_canonical_jsonl(spec.cards_path)
    cards_by_task: dict[str, dict[str, Any]] = {}
    for card in card_records:
        task = card.get("task")
        task_name = task.get("task_name") if isinstance(task, Mapping) else None
        if not isinstance(task_name, str) or not task_name:
            _fail("task_card_name", "task card lacks task.task_name", path=str(spec.cards_path))
        if task_name in cards_by_task:
            _fail("task_card_duplicate", "task card is duplicated", path=task_name)
        cards_by_task[task_name] = card
    try:
        cards = validate_task_cards(cards_by_task, expected_task_count=len(cards_by_task))
    except ValueError as exc:
        raise FailureAttributionError(
            "task_card_validation", str(exc), path=str(spec.cards_path)
        ) from exc

    review_records, reviews_sha256 = _read_canonical_jsonl(spec.reviews_path)
    dummy_outcomes = {
        task_name: {
            "task_name": task_name,
            "catalog_index": card["task"]["catalog_index"],
            "app": "validation-only",
            "outcome": "NO_RESULT",
        }
        for task_name, card in cards.items()
    }
    try:
        compute_metrics(
            review_records,
            cards,
            dummy_outcomes,
            expected_task_count=len(cards),
        )
    except ValueError as exc:
        raise FailureAttributionError(
            "final_review_validation", str(exc), path=str(spec.reviews_path)
        ) from exc
    reviews = {review["task_name"]: review for review in review_records}

    reconstruction_records, reconstruction_sha256 = _read_canonical_jsonl(spec.reconstruction_path)
    reconstructions: dict[str, dict[str, Any]] = {}
    for reconstruction in reconstruction_records:
        task_name = reconstruction.get("task_name")
        if not isinstance(task_name, str) or task_name not in cards:
            _fail(
                "reconstruction_task",
                "reconstruction references an unknown task",
                path=str(spec.reconstruction_path),
            )
        if task_name in reconstructions:
            _fail("reconstruction_duplicate", "task reconstruction is duplicated", path=task_name)
        _validate_reconstruction_binding(cards[task_name], reconstruction)
        reconstructions[task_name] = reconstruction
    if set(reconstructions) != set(cards):
        _fail(
            "reconstruction_coverage",
            "reconstruction refs must cover the exact task-card catalog",
            missing=sorted(set(cards) - set(reconstructions)),
        )
    return _LoadedSource(
        spec=spec,
        cards=cards,
        reviews=reviews,
        reconstructions=reconstructions,
        cards_sha256=cards_sha256,
        reviews_sha256=reviews_sha256,
        reconstruction_sha256=reconstruction_sha256,
    )


def _load_outcomes(
    source: _LoadedSource,
) -> tuple[dict[str, dict[str, Any]], str]:
    records, digest = _read_canonical_jsonl(source.spec.outcomes_path)
    outcomes: dict[str, dict[str, Any]] = {}
    for record in records:
        task_name = record.get("task_name")
        if not isinstance(task_name, str) or task_name in outcomes:
            _fail("outcome_task", "outcome task_name is missing or duplicated")
        outcomes[task_name] = record
    try:
        compute_metrics(
            list(source.reviews.values()),
            source.cards,
            outcomes,
            expected_task_count=len(source.cards),
        )
    except ValueError as exc:
        raise FailureAttributionError(
            "outcome_validation", str(exc), path=str(source.spec.outcomes_path)
        ) from exc
    return outcomes, digest


def _strict_chain_pairs(
    card: Mapping[str, Any], review: Mapping[str, Any]
) -> tuple[tuple[dict[str, Any], dict[str, Any]], ...]:
    candidates = {candidate["candidate_id"]: candidate for candidate in card["candidates"]}
    result = []
    for chain in review["chains"]:
        candidate = candidates[chain["candidate_id"]]
        coverage = card["coverage"]
        strict = bool(
            review["coverage_verdict"] == "SUFFICIENT"
            and coverage["integrity_valid"]
            and coverage["capture_complete"]
            and coverage["decision_count"] == coverage["reconstructed_decision_count"]
            and coverage["dropped_candidate_count"] == 0
            and candidate["exposure"]["was_actually_in_request"] is True
            and candidate["claim"]["provenance_confidence"] in _STRICT_PROVENANCE
            and chain["history_validity"] in _PRIMARY_INVALID
            and chain["uptake_evidence"] == "EXPLICIT_USE"
            and chain["state_confound"] in _LOW_CONFOUND
        )
        if strict:
            result.append((_detach(candidate), _detach(chain)))
    return tuple(result)


def _build_phase_a_card(
    *,
    run_id: str,
    model_id: str,
    card: Mapping[str, Any],
    review: Mapping[str, Any],
    reconstruction: Mapping[str, Any],
    strict_pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    strict_chains = []
    target_steps: dict[str, int] = {}
    for candidate, chain in strict_pairs:
        candidate_id = candidate["candidate_id"]
        target_step = candidate["exposure"]["target_step"]
        target_steps[candidate_id] = target_step
        strict_chains.append(
            {
                "candidate_id": candidate_id,
                "candidate_sha256": canonical_sha256(candidate),
                "frozen_review_chain_sha256": canonical_sha256(chain),
                "strict_mhr": True,
                "strict_mhr_oh": bool(set(chain["downstream_effects"]) & HARMFUL_EFFECTS),
                "reuse_target_step": target_step,
                "source_steps": list(candidate["claim"]["source_steps"]),
                "candidate": candidate,
                "frozen_review_chain": chain,
            }
        )
    strict_chains.sort(key=lambda item: item["candidate_id"])
    min_target = min(target_steps.values())
    prefix_steps = [
        _project_trace_step(step)
        for step in reconstruction["steps"]
        if step["step_index"] < min_target
    ]
    trace_steps = [
        _project_trace_step(step)
        for step in reconstruction["steps"]
        if step["step_index"] >= min_target
    ]
    if not trace_steps:
        _fail("terminal_trace_empty", "strict MHR target has no target-to-terminal trace")
    task = _detach(card["task"])
    task_key = _task_key(model_id, task["task_name"])
    annotation_template = [
        {
            "candidate_id": chain["candidate_id"],
            "recovery_status": None,
            "continuity_status": None,
            "final_observable_predicate": None,
            "affected_predicate": {"predicate_id": None, "description": None},
            "target_contribution": None,
            "competing_trace_defects": [],
            "evidence_step_ids": [],
            "confidence": None,
            "rationale": None,
        }
        for chain in strict_chains
    ]
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": PHASE_A_RECORD_TYPE,
        "attribution_run_id": run_id,
        "phase": "A",
        "outcome_blinded": True,
        "causal_claim_supported": False,
        "task_key": task_key,
        "model_id": model_id,
        "task": task,
        "instruction": card["instruction"],
        "source_binding": {
            "motivation_schema_version": REVIEW_SCHEMA_VERSION,
            "evaluation_run_id": card["evaluation_run_id"],
            "dataset_sha256": card["dataset_sha256"],
            "selection_sha256": card["selection_sha256"],
            "source_task_card_sha256": canonical_sha256(card),
            "source_final_review_sha256": canonical_sha256(review),
            "source_reconstruction_sha256": canonical_sha256(reconstruction),
            "source_trajectory_outline_sha256": canonical_sha256(card["trajectory_outline"]),
            "task_stream_sha256": reconstruction["provenance"]["task_stream_sha256"],
            "task_ended_event_id": reconstruction["task_ended_event_id"],
        },
        "frozen_strict_mhr_chains": strict_chains,
        "trajectory_evidence_completeness": FULL_TRAJECTORY_EVIDENCE,
        "full_trajectory_outline": _detach_value(card["trajectory_outline"]),
        "prefix_trace": {
            "start_step": prefix_steps[0]["step_index"] if prefix_steps else None,
            "end_step": prefix_steps[-1]["step_index"] if prefix_steps else None,
            "steps": prefix_steps,
        },
        "terminal_trace": {
            "start_step": min_target,
            "end_step": trace_steps[-1]["step_index"],
            "steps": trace_steps,
        },
        "annotation_template": annotation_template,
    }


def _build_phase_b_card(
    *,
    run_id: str,
    phase_a_card: Mapping[str, Any],
    phase_a_review: Mapping[str, Any],
    outcome: Mapping[str, Any],
    task_ended: Mapping[str, Any],
) -> dict[str, Any]:
    phase_a_annotations = {chain["candidate_id"]: chain for chain in phase_a_review["chains"]}
    annotation_template = []
    is_success_control = outcome["outcome"] == "SUCCESS"
    for frozen in phase_a_card["frozen_strict_mhr_chains"]:
        candidate_id = frozen["candidate_id"]
        phase_a_chain = phase_a_annotations[candidate_id]
        annotation_template.append(
            {
                "candidate_id": candidate_id,
                "recovery_status": phase_a_chain["recovery_status"],
                "affected_predicate_id": phase_a_chain["affected_predicate"]["predicate_id"],
                "target_contribution": phase_a_chain["target_contribution"],
                "evaluator_predicate": {
                    "affected_predicate_id": phase_a_chain["affected_predicate"]["predicate_id"],
                    "evaluator_predicate_description": None,
                    "evaluator_evidence": {"field_path": None, "excerpt": None},
                },
                "verifier_alignment": SUCCESS_CONTROL if is_success_control else None,
                "alternative_sufficient_failure": (SUCCESS_CONTROL if is_success_control else None),
                "failure_link_level": SUCCESS_CONTROL if is_success_control else None,
                "alternative_defect_ids": [],
                "evaluator_revealed_alternatives": [],
                "evidence_step_ids": [],
                "confidence": None,
                "rationale": None,
            }
        )
    return {
        "schema_version": ATTRIBUTION_SCHEMA_VERSION,
        "record_type": PHASE_B_RECORD_TYPE,
        "attribution_run_id": run_id,
        "phase": "B",
        "causal_claim_supported": False,
        "task_key": phase_a_card["task_key"],
        "model_id": phase_a_card["model_id"],
        "task": _detach(phase_a_card["task"]),
        "instruction": phase_a_card["instruction"],
        "phase_a_binding": {
            "phase_a_run_id": phase_a_card["attribution_run_id"],
            "phase_a_card_sha256": canonical_sha256(phase_a_card),
            "phase_a_review_sha256": canonical_sha256(phase_a_review),
        },
        "source_binding": _detach(phase_a_card["source_binding"]),
        "frozen_strict_mhr_chains": _detach({"chains": phase_a_card["frozen_strict_mhr_chains"]})[
            "chains"
        ],
        "frozen_phase_a_review": _detach(phase_a_review),
        "trajectory_evidence_completeness": phase_a_card["trajectory_evidence_completeness"],
        "full_trajectory_outline": _detach_value(phase_a_card["full_trajectory_outline"]),
        "prefix_trace": _detach(phase_a_card["prefix_trace"]),
        "terminal_trace": _detach(phase_a_card["terminal_trace"]),
        "outcome": _detach(outcome),
        "task_ended": _detach(task_ended),
        "annotation_template": annotation_template,
    }


def _project_trace_step(step: Mapping[str, Any]) -> dict[str, Any]:
    input_record = _mapping(step["I_t"], "step.I_t")
    prediction = _mapping(step["P_t"], "step.P_t")
    action = _mapping(step["A_t"], "step.A_t")
    transition = _mapping(step["R_t"], "step.R_t")
    state_before = _mapping(step["S_t"], "step.S_t")
    state_after = _mapping(step["S_t_plus_1"], "step.S_t_plus_1")
    assistant_exposures = []
    for exposure in input_record.get("assistant_exposures", []):
        if not isinstance(exposure, Mapping):
            continue
        assistant_exposures.append(
            {
                key: _detach_value(exposure[key])
                for key in (
                    "source_step_id",
                    "source_step_index",
                    "representation_type",
                    "mapping_status",
                    "exposed_text",
                    "exposed_text_sha256",
                    "assistant_conclusion_text",
                    "assistant_conclusion_sha256",
                )
                if key in exposure
            }
        )
    return {
        "step_index": step["step_index"],
        "step_id": step["step_id"],
        "source_step_sha256": canonical_sha256(step),
        "state_before": {
            "event_id": state_before.get("event_id"),
            "observation": _detach_value(state_before.get("observation")),
        },
        "request": {
            "event_id": input_record.get("event_id"),
            "request_view_sha256": input_record.get("request_view_sha256"),
            "assistant_exposures": assistant_exposures,
            "ask_user_messages": _detach_value(input_record.get("request_ask_user_messages", [])),
        },
        "prediction": {
            "decision_event_id": prediction.get("decision_event_id"),
            "parse_outcome": prediction.get("parse_outcome"),
            "parse_exception": _detach_value(prediction.get("parse_exception")),
            "prediction_raw": prediction.get("prediction_raw"),
        },
        "action": {
            "action_execution_started_event_id": action.get("action_execution_started_event_id"),
            "parsed_action": _detach_value(action.get("parsed_action")),
        },
        "transition": {
            key: _detach_value(transition.get(key))
            for key in (
                "transition_event_id",
                "transition_type",
                "reason",
                "exception",
                "available_execution_result",
                "execution_result",
            )
        },
        "state_after": {
            "transition_event_id": state_after.get("transition_event_id"),
            "observation": _detach_value(state_after.get("observation")),
        },
    }


def _load_task_ended(
    *,
    card: Mapping[str, Any],
    reconstruction: Mapping[str, Any],
    outcome: Mapping[str, Any],
    source_base: Path,
) -> dict[str, Any]:
    provenance = _mapping(reconstruction["provenance"], "reconstruction.provenance")
    relative_run = _safe_relative_path(
        provenance["source_relative_run_path"],
        "reconstruction.provenance.source_relative_run_path",
    )
    relative_stream = _safe_relative_path(
        provenance["task_stream_relative_path"],
        "reconstruction.provenance.task_stream_relative_path",
    )
    stream_path = (source_base / relative_run / relative_stream).resolve(strict=True)
    if not stream_path.is_relative_to(source_base):
        _fail("task_stream_escape", "task stream escapes source_base", path=str(stream_path))
    expected_stream_sha = provenance["task_stream_sha256"]
    _require_sha256(expected_stream_sha, "reconstruction.provenance.task_stream_sha256")
    target_event_id = reconstruction["task_ended_event_id"]
    digest = hashlib.sha256()
    found: list[tuple[dict[str, Any], str]] = []
    with stream_path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            digest.update(line)
            try:
                event = load_canonical_json_line(line)
            except ValueError as exc:
                raise FailureAttributionError(
                    "raw_event_jsonl",
                    f"raw event is not canonical JSONL: {exc}",
                    path=f"{stream_path}:{line_number}",
                ) from exc
            if event.get("event_id") == target_event_id:
                found.append((event, hashlib.sha256(line).hexdigest()))
    actual_stream_sha = digest.hexdigest()
    if actual_stream_sha != expected_stream_sha:
        _fail(
            "task_stream_digest",
            "raw task stream digest does not match frozen reconstruction provenance",
            path=str(stream_path),
            expected=expected_stream_sha,
            actual=actual_stream_sha,
        )
    if len(found) != 1:
        _fail(
            "task_ended_event",
            "task_ended_event_id must resolve exactly once",
            path=str(stream_path),
            matches=len(found),
        )
    event, event_sha256 = found[0]
    _literal(event.get("event_type"), "task_ended", "task_ended.event_type")
    _literal(event.get("task_run_id"), card["task"]["task_run_id"], "task_ended.task_run_id")
    payload = _mapping(event.get("payload"), "task_ended.payload")
    evaluator = _mapping(
        payload.get("environment_evaluation"),
        "task_ended.payload.environment_evaluation",
    )
    termination = _mapping(payload.get("termination"), "task_ended.payload.termination")
    if outcome["outcome"] not in {"FAILURE", "SUCCESS"}:
        _fail(
            "phase_b_outcome",
            "Phase B requires an evaluator-backed FAILURE or SUCCESS outcome",
        )
    outcome_score = outcome.get("score")
    if evaluator.get("score") != outcome_score:
        _fail(
            "evaluator_score_mismatch",
            "outcome sidecar score differs from task_ended evaluator score",
            expected=outcome_score,
            actual=evaluator.get("score"),
        )
    return {
        "event_id": event["event_id"],
        "event_sha256": event_sha256,
        "task_stream_relative_path": relative_stream.as_posix(),
        "task_stream_sha256": actual_stream_sha,
        "environment_evaluation": _detach(evaluator),
        "termination": _detach(termination),
        "runtime_status": payload.get("runtime_status"),
        "capture_complete": payload.get("capture_complete"),
        "collector_error_event_ids": _detach_value(payload.get("collector_error_event_ids", [])),
        "missing_artifacts": _detach_value(payload.get("missing_artifacts", [])),
    }


def _validate_reconstruction_binding(
    card: Mapping[str, Any], reconstruction: Mapping[str, Any]
) -> None:
    task = card["task"]
    _literal(reconstruction.get("task_name"), task["task_name"], "reconstruction.task_name")
    _literal(
        reconstruction.get("canonical_suite_index"),
        task["catalog_index"],
        "reconstruction.canonical_suite_index",
    )
    reconstruction_instruction = reconstruction.get("task_instruction")
    if (
        not isinstance(reconstruction_instruction, str)
        or reconstruction_instruction.strip() != card["instruction"]
    ):
        _fail(
            "reconstruction_instruction",
            "reconstruction instruction must match the card after edge-whitespace normalization",
            path="reconstruction.task_instruction",
        )
    provenance = _mapping(reconstruction.get("provenance"), "reconstruction.provenance")
    expected = {
        "source_id": task["source_id"],
        "source_relative_run_path": task["source_relative_run_path"],
        "source_run_id": task["raw_run_id"],
        "source_task_run_id": task["task_run_id"],
        "task_stream_relative_path": task["task_stream_relative_path"],
    }
    for key, value in expected.items():
        _literal(provenance.get(key), value, f"reconstruction.provenance.{key}")
    expected_reconstruction_sha = card["coverage"]["full_reconstruction_sha256"]
    _literal(
        canonical_sha256(reconstruction),
        expected_reconstruction_sha,
        "card.coverage.full_reconstruction_sha256",
    )
    steps = _sequence(reconstruction.get("steps"), "reconstruction.steps")
    expected_indices = list(range(1, card["coverage"]["decision_count"] + 1))
    actual_indices = [step.get("step_index") for step in steps if isinstance(step, Mapping)]
    if actual_indices != expected_indices:
        _fail(
            "reconstruction_steps",
            "reconstruction step indices must be contiguous and cover every decision",
            expected=expected_indices,
            actual=actual_indices,
        )
    outline_indices = [
        outline.get("step")
        for outline in card["trajectory_outline"]
        if isinstance(outline, Mapping)
    ]
    if outline_indices != expected_indices:
        _fail(
            "trajectory_outline_steps",
            "task-card trajectory outline must cover every reconstruction step",
            expected=expected_indices,
            actual=outline_indices,
        )
    _nonempty_string(reconstruction.get("task_ended_event_id"), "task_ended_event_id")


def _validate_phase_a_review_chains(raw_chains: Any, card: Mapping[str, Any], *, path: str) -> None:
    chains = _sequence(raw_chains, path)
    expected = {chain["candidate_id"]: chain for chain in card["frozen_strict_mhr_chains"]}
    if len(chains) != len(expected):
        _fail(
            "phase_a_chain_coverage", "Phase-A chains must cover every strict-MHR chain", path=path
        )
    prior = ""
    seen: set[str] = set()
    terminal_steps = {
        step["step_id"]: step["step_index"] for step in card["terminal_trace"]["steps"]
    }
    all_trace_steps = _all_trace_steps(card)
    predicate_descriptions: dict[str, str] = {}
    for index, raw in enumerate(chains):
        chain_path = f"{path}[{index}]"
        chain = _mapping(raw, chain_path)
        _exact_keys(
            chain,
            {
                "candidate_id",
                "recovery_status",
                "continuity_status",
                "final_observable_predicate",
                "affected_predicate",
                "target_contribution",
                "competing_trace_defects",
                "evidence_step_ids",
                "confidence",
                "rationale",
            },
            path=chain_path,
        )
        candidate_id = _nonempty_string(chain["candidate_id"], f"{chain_path}.candidate_id")
        if (
            candidate_id not in expected
            or candidate_id in seen
            or (prior and candidate_id <= prior)
        ):
            _fail(
                "phase_a_chain_identity",
                "Phase-A chains must be unique and sorted frozen candidate IDs",
                path=chain_path,
            )
        seen.add(candidate_id)
        prior = candidate_id
        _enum(chain["recovery_status"], RECOVERY_STATUSES, f"{chain_path}.recovery_status")
        _enum(chain["continuity_status"], CONTINUITY_STATUSES, f"{chain_path}.continuity_status")
        _enum(
            chain["final_observable_predicate"],
            FINAL_OBSERVABLE_PREDICATES,
            f"{chain_path}.final_observable_predicate",
        )
        predicate_id, predicate_description = _validate_affected_predicate(
            chain["affected_predicate"], path=f"{chain_path}.affected_predicate"
        )
        prior_description = predicate_descriptions.setdefault(predicate_id, predicate_description)
        if prior_description != predicate_description:
            _fail(
                "predicate_description_mismatch",
                "one predicate_id must have one stable description within a task",
                path=f"{chain_path}.affected_predicate",
            )
        _enum(
            chain["target_contribution"],
            TARGET_CONTRIBUTIONS,
            f"{chain_path}.target_contribution",
        )
        _enum(chain["confidence"], CONFIDENCE_LEVELS, f"{chain_path}.confidence")
        _bounded_string(chain["rationale"], f"{chain_path}.rationale", maximum=2000)
        target_step = expected[candidate_id]["reuse_target_step"]
        _validate_step_ids(
            chain["evidence_step_ids"],
            terminal_steps,
            minimum_step=target_step,
            path=f"{chain_path}.evidence_step_ids",
            require_nonempty=True,
        )
        _validate_competing_defects(
            chain["competing_trace_defects"],
            all_trace_steps,
            minimum_step=1,
            path=f"{chain_path}.competing_trace_defects",
        )
    if seen != set(expected):
        _fail("phase_a_chain_coverage", "Phase-A chain coverage is incomplete", path=path)


def _validate_phase_b_review_chains(
    raw_chains: Any, card: Mapping[str, Any], *, path: str
) -> list[str]:
    chains = _sequence(raw_chains, path)
    phase_a_chains = {
        chain["candidate_id"]: chain for chain in card["frozen_phase_a_review"]["chains"]
    }
    if len(chains) != len(phase_a_chains):
        _fail(
            "phase_b_chain_coverage",
            "Phase-B chains must cover every frozen Phase-A chain",
            path=path,
        )
    trace_steps = _all_trace_steps(card)
    prior = ""
    seen: set[str] = set()
    levels: list[str] = []
    for index, raw in enumerate(chains):
        chain_path = f"{path}[{index}]"
        chain = _mapping(raw, chain_path)
        _exact_keys(
            chain,
            {
                "candidate_id",
                "recovery_status",
                "affected_predicate_id",
                "target_contribution",
                "evaluator_predicate",
                "verifier_alignment",
                "alternative_sufficient_failure",
                "failure_link_level",
                "alternative_defect_ids",
                "evaluator_revealed_alternatives",
                "evidence_step_ids",
                "confidence",
                "rationale",
            },
            path=chain_path,
        )
        candidate_id = _nonempty_string(chain["candidate_id"], f"{chain_path}.candidate_id")
        if (
            candidate_id not in phase_a_chains
            or candidate_id in seen
            or (prior and candidate_id <= prior)
        ):
            _fail(
                "phase_b_chain_identity",
                "Phase-B chains must be unique and sorted frozen candidate IDs",
                path=chain_path,
            )
        seen.add(candidate_id)
        prior = candidate_id
        phase_a_chain = phase_a_chains[candidate_id]
        recovery = _enum(
            chain["recovery_status"], RECOVERY_STATUSES, f"{chain_path}.recovery_status"
        )
        _literal(
            recovery,
            phase_a_chain["recovery_status"],
            f"{chain_path}.recovery_status",
        )
        affected_predicate_id = _nonempty_string(
            chain["affected_predicate_id"],
            f"{chain_path}.affected_predicate_id",
        )
        _literal(
            affected_predicate_id,
            phase_a_chain["affected_predicate"]["predicate_id"],
            f"{chain_path}.affected_predicate_id",
        )
        target_contribution = _enum(
            chain["target_contribution"],
            TARGET_CONTRIBUTIONS,
            f"{chain_path}.target_contribution",
        )
        _literal(
            target_contribution,
            phase_a_chain["target_contribution"],
            f"{chain_path}.target_contribution",
        )
        evaluator_evidence = _validate_evaluator_predicate(
            chain["evaluator_predicate"],
            expected_predicate_id=affected_predicate_id,
            card=card,
            path=f"{chain_path}.evaluator_predicate",
        )
        alignment = _enum(
            chain["verifier_alignment"],
            VERIFIER_ALIGNMENTS,
            f"{chain_path}.verifier_alignment",
        )
        alternative = _enum(
            chain["alternative_sufficient_failure"],
            ALTERNATIVE_SUFFICIENT_FAILURES,
            f"{chain_path}.alternative_sufficient_failure",
        )
        level = _enum(
            chain["failure_link_level"],
            FAILURE_LINK_LEVELS,
            f"{chain_path}.failure_link_level",
        )
        levels.append(level)
        is_success_control = card["outcome"]["outcome"] == "SUCCESS"
        failure_only_values = {alignment, alternative, level}
        if is_success_control and failure_only_values != {SUCCESS_CONTROL}:
            _fail(
                "success_control_labels",
                "success controls require NOT_APPLICABLE_SUCCESS_CONTROL for all failure-only fields",
                path=chain_path,
            )
        if not is_success_control and SUCCESS_CONTROL in failure_only_values:
            _fail(
                "failure_labels_not_applicable",
                "failure tasks cannot use the success-control label",
                path=chain_path,
            )
        defect_ids = _sorted_unique_strings(
            chain["alternative_defect_ids"], f"{chain_path}.alternative_defect_ids"
        )
        revealed_alternatives = _validate_evaluator_revealed_alternatives(
            chain["evaluator_revealed_alternatives"],
            card=card,
            path=f"{chain_path}.evaluator_revealed_alternatives",
        )
        available_defects = {
            defect["defect_id"] for defect in phase_a_chain["competing_trace_defects"]
        }
        if not set(defect_ids) <= available_defects:
            _fail(
                "phase_b_defect_reference",
                "alternative_defect_ids must reference frozen Phase-A defects",
                path=f"{chain_path}.alternative_defect_ids",
            )
        revealed_ids = {alternative["alternative_id"] for alternative in revealed_alternatives}
        if available_defects & revealed_ids:
            _fail(
                "phase_b_alternative_id_collision",
                "evaluator-revealed alternative IDs must not collide with frozen "
                "Phase-A defect IDs",
                path=f"{chain_path}.evaluator_revealed_alternatives",
            )
        if alternative == "PRESENT" and not (defect_ids or revealed_alternatives):
            _fail(
                "phase_b_alternative_evidence",
                "PRESENT alternative failure requires a Phase-A defect ID or an "
                "evaluator-revealed alternative",
                path=chain_path,
            )
        if alternative != "PRESENT" and (defect_ids or revealed_alternatives):
            _fail(
                "phase_b_alternative_evidence",
                "only PRESENT alternative failure may cite alternative evidence",
                path=chain_path,
            )
        target_step = next(
            frozen["reuse_target_step"]
            for frozen in card["frozen_strict_mhr_chains"]
            if frozen["candidate_id"] == candidate_id
        )
        evidence_step_ids = _validate_step_ids(
            chain["evidence_step_ids"],
            trace_steps,
            minimum_step=1,
            path=f"{chain_path}.evidence_step_ids",
            require_nonempty=True,
        )
        _enum(chain["confidence"], CONFIDENCE_LEVELS, f"{chain_path}.confidence")
        _bounded_string(chain["rationale"], f"{chain_path}.rationale", maximum=2000)
        if not is_success_control:
            _validate_failure_link_cross_fields(
                level=level,
                recovery=recovery,
                continuity=phase_a_chain["continuity_status"],
                final_predicate=phase_a_chain["final_observable_predicate"],
                target_contribution=target_contribution,
                alignment=alignment,
                evaluator_evidence_field=evaluator_evidence["field_path"],
                alternative=alternative,
                trajectory_evidence_completeness=card["trajectory_evidence_completeness"],
                has_phase_a_competing_defect=bool(phase_a_chain["competing_trace_defects"]),
                has_suffix_evidence=any(
                    trace_steps[step_id] >= target_step for step_id in evidence_step_ids
                ),
                path=chain_path,
            )
    if seen != set(phase_a_chains):
        _fail("phase_b_chain_coverage", "Phase-B chain coverage is incomplete", path=path)
    return levels


def _validate_failure_link_cross_fields(
    *,
    level: str,
    recovery: str,
    continuity: str,
    final_predicate: str,
    target_contribution: str,
    alignment: str,
    evaluator_evidence_field: str,
    alternative: str,
    trajectory_evidence_completeness: str,
    has_phase_a_competing_defect: bool,
    has_suffix_evidence: bool,
    path: str,
) -> None:
    if alignment == "DIRECT" and evaluator_evidence_field == _SCORE_EVALUATOR_EVIDENCE_FIELD:
        _fail(
            "direct_alignment_score_only",
            "DIRECT verifier alignment requires evaluator evidence beyond the score alone",
            path=path,
        )
    if alternative == "ABSENT" and trajectory_evidence_completeness != FULL_TRAJECTORY_EVIDENCE:
        _fail(
            "alternative_absent_without_full_trajectory",
            "ABSENT requires a full reconstruction projection of the task prefix",
            path=path,
        )
    if level == "STRONG_OBSERVED_CONTRIBUTION":
        required = {
            "recovery_status": (recovery, "NOT_RECOVERED"),
            "continuity_status": (continuity, "CONTINUOUS"),
            "verifier_alignment": (alignment, "DIRECT"),
            "alternative_sufficient_failure": (alternative, "ABSENT"),
        }
        mismatches = {
            key: {"actual": actual, "required": expected}
            for key, (actual, expected) in required.items()
            if actual != expected
        }
        if final_predicate not in {"UNSATISFIED", "PARTIAL"}:
            mismatches["final_observable_predicate"] = {
                "actual": final_predicate,
                "required": "UNSATISFIED or PARTIAL",
            }
        if target_contribution in {"MERELY_CONCURRENT", "UNKNOWN"}:
            mismatches["target_contribution"] = {
                "actual": target_contribution,
                "required": "a non-concurrent, known contribution mechanism",
            }
        if has_phase_a_competing_defect:
            mismatches["competing_trace_defects"] = {
                "actual": "one or more frozen Phase-A defects",
                "required": "none for the strong level",
            }
        if not has_suffix_evidence:
            mismatches["evidence_step_ids"] = {
                "actual": "prefix-only",
                "required": "at least one reuse-target-or-later evidence step",
            }
        if mismatches:
            _fail(
                "strong_observed_requirements",
                "strong observed contribution requires an unbroken, unrecovered, "
                "directly verifier-aligned pathway with no sufficient alternative",
                path=path,
                mismatches=mismatches,
            )
    if level == "PLAUSIBLE_OBSERVED_CONTRIBUTION":
        if alignment not in {"DIRECT", "INDIRECT"}:
            _fail(
                "plausible_observed_alignment",
                "plausible observed contribution requires DIRECT or INDIRECT alignment",
                path=path,
            )
        if recovery in {"RECOVERED", "NO_HARM_TO_RECOVER"}:
            _fail(
                "plausible_observed_recovery",
                "a recovered/no-harm chain cannot be a plausible observed contributor",
                path=path,
            )
        if target_contribution in {"MERELY_CONCURRENT", "UNKNOWN"}:
            _fail(
                "plausible_observed_mechanism",
                "plausible observed contribution requires a non-concurrent known mechanism",
                path=path,
            )
    if target_contribution == "MERELY_CONCURRENT" and level != "CO_OCCURRENCE_ONLY":
        _fail(
            "concurrent_failure_link",
            "MERELY_CONCURRENT requires CO_OCCURRENCE_ONLY",
            path=path,
        )
    if level == "INDETERMINATE" and "UNKNOWN" not in {
        recovery,
        continuity,
        final_predicate,
        target_contribution,
        alignment,
        alternative,
    }:
        _fail(
            "indeterminate_without_unknown",
            "INDETERMINATE requires at least one UNKNOWN prerequisite",
            path=path,
        )


def _phase_a_material_signature(review: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        (
            chain["candidate_id"],
            chain["recovery_status"],
            chain["continuity_status"],
            chain["final_observable_predicate"],
            (
                chain["affected_predicate"]["predicate_id"],
                chain["affected_predicate"]["description"],
            ),
            chain["target_contribution"],
            tuple(
                (
                    defect["defect_id"],
                    defect["first_step"],
                    defect["description"],
                    tuple(defect["evidence_step_ids"]),
                )
                for defect in chain["competing_trace_defects"]
            ),
        )
        for chain in review["chains"]
    )


def _phase_b_material_signature(review: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(
            (
                chain["candidate_id"],
                chain["recovery_status"],
                chain["affected_predicate_id"],
                chain["target_contribution"],
                (
                    chain["evaluator_predicate"]["affected_predicate_id"],
                    chain["evaluator_predicate"]["evaluator_predicate_description"],
                    chain["evaluator_predicate"]["evaluator_evidence"]["field_path"],
                    chain["evaluator_predicate"]["evaluator_evidence"]["excerpt"],
                ),
                chain["verifier_alignment"],
                chain["alternative_sufficient_failure"],
                chain["failure_link_level"],
                tuple(chain["alternative_defect_ids"]),
                tuple(
                    (
                        alternative["alternative_id"],
                        alternative["description"],
                        alternative["evaluator_evidence"]["field_path"],
                        alternative["evaluator_evidence"]["excerpt"],
                    )
                    for alternative in chain["evaluator_revealed_alternatives"]
                ),
            )
            for chain in review["chains"]
        ),
        review["task_failure_link_level"],
    )


def _require_independent(
    first: Mapping[str, Any], second: Mapping[str, Any], *, phase: str
) -> None:
    identity_fields = (
        "attribution_run_id",
        "task_key",
        "model_id",
        "task_name",
        "catalog_index",
        "phase_a_card_sha256" if phase == "A" else "phase_b_card_sha256",
    )
    for field in identity_fields:
        if first[field] != second[field]:
            _fail(
                "independent_review_identity",
                f"independent Phase-{phase} reviews disagree on {field}",
                path=field,
            )
    if first["reviewer_id"] == second["reviewer_id"]:
        _fail(
            "reviewer_not_independent",
            f"Phase-{phase} primary and secondary reviewer_id values must differ",
            path=first["task_key"],
        )
    if first["review_id"] == second["review_id"]:
        _fail(
            "review_id_not_independent",
            f"Phase-{phase} primary and secondary review_id values must differ",
            path=first["task_key"],
        )


def _require_adjudicator_independent(
    adjudication: Mapping[str, Any],
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
    *,
    phase: str,
) -> None:
    _require_independent(primary, secondary, phase=phase)
    for other_role, other in (("primary", primary), ("secondary", secondary)):
        if adjudication["reviewer_id"] == other["reviewer_id"]:
            _fail(
                "adjudicator_not_independent",
                f"Phase-{phase} adjudicator must differ from {other_role} reviewer",
                path=adjudication["task_key"],
            )
        if adjudication["review_id"] == other["review_id"]:
            _fail(
                "adjudication_review_id",
                f"Phase-{phase} adjudication review_id must differ from {other_role}",
                path=adjudication["task_key"],
            )


def _require_globally_unique_review_ids(
    primary: Mapping[str, Mapping[str, Any]],
    secondary: Mapping[str, Mapping[str, Any]],
    adjudications: Mapping[str, Mapping[str, Any]],
    *,
    phase: str,
) -> None:
    review_ids = [
        review["review_id"]
        for review_set in (primary, secondary, adjudications)
        for review in review_set.values()
    ]
    if len(review_ids) != len(set(review_ids)):
        _fail(
            "review_id_global_duplicate",
            f"Phase-{phase} review_id values must be globally unique across all passes",
        )


def _validate_subset_reviews(
    reviews: Sequence[Mapping[str, Any]],
    *,
    cards: Sequence[Mapping[str, Any]],
    required_keys: set[str],
    phase: str,
) -> dict[str, dict[str, Any]]:
    values = _sequence(reviews, "adjudication_reviews")
    raw_by_key: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(values):
        review = _mapping(raw, f"adjudication_reviews[{index}]")
        task_key = review.get("task_key")
        if not isinstance(task_key, str) or not task_key or task_key in raw_by_key:
            _fail(
                "adjudication_task_key",
                "adjudication task_key is missing or duplicated",
                path=f"adjudication_reviews[{index}]",
            )
        raw_by_key[task_key] = review
    if set(raw_by_key) != required_keys:
        _fail(
            "adjudication_coverage",
            "adjudications must cover exactly the material disagreements",
            missing=sorted(required_keys - set(raw_by_key)),
            unexpected=sorted(set(raw_by_key) - required_keys),
        )
    card_by_key = {card["task_key"]: card for card in cards}
    result: dict[str, dict[str, Any]] = {}
    for task_key in sorted(required_keys):
        card = card_by_key[task_key]
        if phase == "A":
            validated = validate_phase_a_reviews([raw_by_key[task_key]], [card])
        else:
            validated = validate_phase_b_reviews([raw_by_key[task_key]], [card])
        result[task_key] = validated[task_key]
    review_ids = [review["review_id"] for review in result.values()]
    if len(review_ids) != len(set(review_ids)):
        _fail("adjudication_review_id_duplicate", "adjudication review_id is duplicated")
    return result


def _validate_phase_a_resolution_binding(
    resolution: PhaseAResolution, phase_a_bundle: PhaseABundle
) -> None:
    if not isinstance(resolution, PhaseAResolution):
        _fail(
            "phase_a_resolution_type",
            "Phase B requires a validated PhaseAResolution, not an arbitrary review list",
        )
    rebuilt = resolve_phase_a_reviews(
        phase_a_bundle,
        resolution.primary_reviews,
        resolution.secondary_reviews,
        resolution.adjudication_reviews,
    )
    if rebuilt.manifest != resolution.manifest or rebuilt.final_reviews != resolution.final_reviews:
        _fail(
            "phase_a_resolution_binding",
            "Phase-A resolution does not match its cards and frozen review sets",
        )
    if resolution.manifest["counts"]["unresolved_task_count"] != 0:
        _fail("phase_a_unresolved", "Phase-A resolution contains unresolved tasks")
    if resolution.manifest["outcomes_opened"] is not False:
        _fail("phase_a_outcome_blind", "Phase-A resolution must declare outcomes_opened=false")


def _all_trace_steps(card: Mapping[str, Any]) -> dict[str, int]:
    _literal(
        card.get("trajectory_evidence_completeness"),
        FULL_TRAJECTORY_EVIDENCE,
        "card.trajectory_evidence_completeness",
    )
    projected_steps = [
        *card["prefix_trace"]["steps"],
        *card["terminal_trace"]["steps"],
    ]
    indices = [step["step_index"] for step in projected_steps]
    if indices != list(range(1, len(projected_steps) + 1)):
        _fail(
            "full_trace_coverage",
            "prefix and terminal projections must cover every task step contiguously",
            actual=indices,
        )
    result = {step["step_id"]: step["step_index"] for step in projected_steps}
    if len(result) != len(projected_steps):
        _fail("full_trace_step_id", "full trajectory step_id values must be unique")
    outline = _sequence(card["full_trajectory_outline"], "card.full_trajectory_outline")
    outline_indices = [item.get("step") for item in outline if isinstance(item, Mapping)]
    if outline_indices != indices:
        _fail(
            "full_outline_coverage",
            "full trajectory outline must align with every projected step",
            actual=outline_indices,
            expected=indices,
        )
    expected_outline_sha = card["source_binding"]["source_trajectory_outline_sha256"]
    _literal(
        canonical_sha256(outline),
        expected_outline_sha,
        "card.source_binding.source_trajectory_outline_sha256",
    )
    return result


def _validate_affected_predicate(value: Any, *, path: str) -> tuple[str, str]:
    predicate = _mapping(value, path)
    _exact_keys(predicate, {"predicate_id", "description"}, path=path)
    predicate_id = _nonempty_string(predicate["predicate_id"], f"{path}.predicate_id")
    if not _PREDICATE_ID_RE.fullmatch(predicate_id):
        _fail(
            "predicate_id",
            "predicate_id must be 1-64 lowercase letters, digits, dots, underscores, or hyphens",
            path=f"{path}.predicate_id",
        )
    description = _bounded_string(predicate["description"], f"{path}.description", maximum=500)
    return predicate_id, description


def _validate_evaluator_predicate(
    value: Any,
    *,
    expected_predicate_id: str,
    card: Mapping[str, Any],
    path: str,
) -> Mapping[str, Any]:
    predicate = _mapping(value, path)
    _exact_keys(
        predicate,
        {
            "affected_predicate_id",
            "evaluator_predicate_description",
            "evaluator_evidence",
        },
        path=path,
    )
    _literal(
        predicate["affected_predicate_id"],
        expected_predicate_id,
        f"{path}.affected_predicate_id",
    )
    _bounded_string(
        predicate["evaluator_predicate_description"],
        f"{path}.evaluator_predicate_description",
        maximum=1000,
    )
    return _validate_evaluator_evidence(
        predicate["evaluator_evidence"],
        card=card,
        path=f"{path}.evaluator_evidence",
    )


def _validate_evaluator_revealed_alternatives(
    value: Any,
    *,
    card: Mapping[str, Any],
    path: str,
) -> list[Mapping[str, Any]]:
    alternatives = _sequence(value, path)
    result: list[Mapping[str, Any]] = []
    prior_id = ""
    for index, raw in enumerate(alternatives):
        alternative_path = f"{path}[{index}]"
        alternative = _mapping(raw, alternative_path)
        _exact_keys(
            alternative,
            {"alternative_id", "description", "evaluator_evidence"},
            path=alternative_path,
        )
        alternative_id = _nonempty_string(
            alternative["alternative_id"], f"{alternative_path}.alternative_id"
        )
        if not _ALTERNATIVE_ID_RE.fullmatch(alternative_id):
            _fail(
                "alternative_id",
                "alternative_id must be 1-64 lowercase letters, digits, dots, underscores, or hyphens",
                path=f"{alternative_path}.alternative_id",
            )
        if prior_id and alternative_id <= prior_id:
            _fail(
                "evaluator_alternative_order",
                "evaluator-revealed alternatives must be sorted by alternative_id",
                path=alternative_path,
            )
        prior_id = alternative_id
        _bounded_string(
            alternative["description"],
            f"{alternative_path}.description",
            maximum=1000,
        )
        _validate_evaluator_evidence(
            alternative["evaluator_evidence"],
            card=card,
            path=f"{alternative_path}.evaluator_evidence",
        )
        result.append(alternative)
    return result


def _validate_evaluator_evidence(
    value: Any,
    *,
    card: Mapping[str, Any],
    path: str,
) -> Mapping[str, Any]:
    evidence = _mapping(value, path)
    _exact_keys(evidence, {"field_path", "excerpt"}, path=path)
    field_path = _enum(
        evidence["field_path"],
        EVALUATOR_EVIDENCE_FIELDS,
        f"{path}.field_path",
    )
    excerpt = _bounded_string(evidence["excerpt"], f"{path}.excerpt", maximum=1000)
    actual_value = _card_evaluator_field_value(card, field_path=field_path, path=path)
    normalized_actual = _normalize_evaluator_evidence(actual_value)
    if not normalized_actual:
        _fail(
            "evaluator_evidence_empty_source",
            "evaluator evidence cannot cite an absent or empty task_ended field",
            path=f"{path}.field_path",
        )
    normalized_excerpt = _normalize_evaluator_evidence(excerpt)
    if not normalized_excerpt or normalized_excerpt not in normalized_actual:
        _fail(
            "evaluator_evidence_excerpt",
            "evaluator evidence excerpt must be a real nonempty substring of the "
            "normalized task_ended field value",
            path=f"{path}.excerpt",
            field_path=field_path,
        )
    return evidence


def _card_evaluator_field_value(card: Mapping[str, Any], *, field_path: str, path: str) -> Any:
    task_ended = _mapping(card.get("task_ended"), f"{path}.card.task_ended")
    evaluator = _mapping(
        task_ended.get("environment_evaluation"),
        f"{path}.card.task_ended.environment_evaluation",
    )
    if field_path == "task_ended.environment_evaluation.reason":
        return evaluator.get("reason")
    if field_path == "task_ended.environment_evaluation.exception":
        return evaluator.get("exception")
    if field_path == _SCORE_EVALUATOR_EVIDENCE_FIELD:
        return evaluator.get("score")
    if field_path == "task_ended.termination":
        return task_ended.get("termination")
    _fail("evaluator_evidence_field", "unsupported evaluator evidence field", path=path)


def _normalize_evaluator_evidence(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Mapping) and not value:
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) and not value:
        return ""
    return canonical_json_bytes(value, newline=False).decode("utf-8")


def _validate_competing_defects(
    raw_defects: Any,
    trace_steps: Mapping[str, int],
    *,
    minimum_step: int,
    path: str,
) -> None:
    defects = _sequence(raw_defects, path)
    prior = ""
    for index, raw in enumerate(defects):
        defect_path = f"{path}[{index}]"
        defect = _mapping(raw, defect_path)
        _exact_keys(
            defect,
            {"defect_id", "first_step", "description", "evidence_step_ids"},
            path=defect_path,
        )
        defect_id = _nonempty_string(defect["defect_id"], f"{defect_path}.defect_id")
        if prior and defect_id <= prior:
            _fail("defect_order", "competing defects must be sorted by defect_id", path=defect_path)
        prior = defect_id
        first_step = _positive_int(defect["first_step"], f"{defect_path}.first_step")
        if first_step < minimum_step:
            _fail(
                "defect_before_evidence_range",
                "competing defect precedes the allowed trajectory evidence range",
                path=defect_path,
            )
        _bounded_string(defect["description"], f"{defect_path}.description", maximum=1000)
        evidence_ids = _validate_step_ids(
            defect["evidence_step_ids"],
            trace_steps,
            minimum_step=minimum_step,
            path=f"{defect_path}.evidence_step_ids",
            require_nonempty=True,
        )
        if first_step != min(trace_steps[step_id] for step_id in evidence_ids):
            _fail(
                "defect_first_step",
                "first_step must equal the earliest cited evidence step",
                path=defect_path,
            )


def _validate_step_ids(
    raw_ids: Any,
    trace_steps: Mapping[str, int],
    *,
    minimum_step: int,
    path: str,
    require_nonempty: bool,
) -> list[str]:
    values = _sorted_unique_strings(raw_ids, path)
    if require_nonempty and not values:
        _fail("step_evidence_empty", "at least one evidence step is required", path=path)
    for step_id in values:
        step_index = trace_steps.get(step_id)
        if step_index is None:
            _fail(
                "step_evidence_unknown",
                "evidence step_id is absent from the full projected trajectory",
                path=path,
            )
        if step_index < minimum_step:
            _fail(
                "step_evidence_before_minimum",
                "evidence precedes the allowed minimum step",
                path=path,
            )
    return values


def _phase_a_source_manifest(source: _LoadedSource) -> dict[str, Any]:
    return {
        "model_id": source.spec.model_id,
        "bundle_root": str(source.spec.root),
        "task_cards_path": str(source.spec.cards_path),
        "task_cards_sha256": source.cards_sha256,
        "final_reviews_path": str(source.spec.reviews_path),
        "final_reviews_sha256": source.reviews_sha256,
        "reconstruction_path": str(source.spec.reconstruction_path),
        "reconstruction_sha256": source.reconstruction_sha256,
        "catalog_task_count": len(source.cards),
    }


def _read_canonical_jsonl(
    path: Path, *, allow_empty: bool = False
) -> tuple[list[dict[str, Any]], str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise FailureAttributionError("input_path", str(exc), path=str(path)) from exc
    records: list[dict[str, Any]] = []
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            digest.update(line)
            try:
                records.append(load_canonical_json_line(line))
            except ValueError as exc:
                raise FailureAttributionError(
                    "canonical_jsonl",
                    str(exc),
                    path=f"{resolved}:{line_number}",
                ) from exc
    if not records and not allow_empty:
        _fail("jsonl_empty", "canonical JSONL input must not be empty", path=str(resolved))
    return records, digest.hexdigest()


def _read_canonical_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = path.resolve(strict=True).read_bytes()
    except OSError as exc:
        raise FailureAttributionError("input_path", str(exc), path=str(path)) from exc
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FailureAttributionError("canonical_json", str(exc), path=str(path)) from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        _fail("canonical_json", "file is not one canonical JSON object", path=str(path))
    return value


def _resolution_files(
    resolution: PhaseAResolution | PhaseBResolution, *, include_metrics: bool
) -> dict[str, bytes]:
    files = {
        "manifest.json": canonical_json_bytes(resolution.manifest),
        "primary_reviews.jsonl": b"".join(
            canonical_json_bytes(review) for review in resolution.primary_reviews
        ),
        "secondary_reviews.jsonl": b"".join(
            canonical_json_bytes(review) for review in resolution.secondary_reviews
        ),
        "adjudication_reviews.jsonl": b"".join(
            canonical_json_bytes(review) for review in resolution.adjudication_reviews
        ),
        "final_reviews.jsonl": b"".join(
            canonical_json_bytes(review) for review in resolution.final_reviews
        ),
    }
    if include_metrics:
        if not isinstance(resolution, PhaseBResolution):
            _fail("resolution_metrics", "only Phase-B resolution has metrics")
        files["metrics.json"] = canonical_json_bytes(resolution.metrics)
    return files


def _write_once(files: Mapping[str, bytes], output_dir: Path, *, dry_run: bool) -> dict[str, Any]:
    summary = {
        "dry_run": dry_run,
        "output_dir": str(output_dir),
        "files": {
            name: {"byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(files.items())
        },
    }
    if dry_run:
        return summary
    if output_dir.exists():
        _fail("output_exists", "output directory already exists", path=str(output_dir))
    output_dir.mkdir(parents=True, exist_ok=False)
    try:
        for name, data in files.items():
            with (output_dir / name).open("xb") as handle:
                handle.write(data)
                handle.flush()
    except Exception:
        # Leave a visible partial directory rather than deleting evidence of a
        # failed write or risking a broad destructive cleanup.
        raise
    return summary


def _review_json_schema(*, phase: str) -> dict[str, Any]:
    identity_properties = {
        "schema_version": {"type": "string", "const": ATTRIBUTION_SCHEMA_VERSION},
        "record_type": {
            "type": "string",
            "const": PHASE_A_REVIEW_RECORD_TYPE if phase == "A" else PHASE_B_REVIEW_RECORD_TYPE,
        },
        "attribution_run_id": {"type": "string", "minLength": 1},
        "review_id": {"type": "string", "minLength": 1},
        "reviewer_id": {"type": "string", "minLength": 1},
        "task_key": {"type": "string", "minLength": 1},
        "model_id": {"type": "string", "pattern": _MODEL_ID_RE.pattern},
        "task_name": {"type": "string", "minLength": 1},
        "catalog_index": {"type": "integer", "minimum": 1},
        "summary": {"type": "string", "minLength": 1, "maxLength": 2000},
    }
    if phase == "A":
        identity_properties.update(
            {
                "phase_a_card_sha256": {"type": "string", "pattern": _SHA256_RE.pattern},
                "outcome_blinded": {"type": "boolean", "const": True},
                "chains": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "candidate_id",
                            "recovery_status",
                            "continuity_status",
                            "final_observable_predicate",
                            "affected_predicate",
                            "target_contribution",
                            "competing_trace_defects",
                            "evidence_step_ids",
                            "confidence",
                            "rationale",
                        ],
                        "properties": {
                            "candidate_id": {"type": "string", "minLength": 1},
                            "recovery_status": {
                                "type": "string",
                                "enum": sorted(RECOVERY_STATUSES),
                            },
                            "continuity_status": {
                                "type": "string",
                                "enum": sorted(CONTINUITY_STATUSES),
                            },
                            "final_observable_predicate": {
                                "type": "string",
                                "enum": sorted(FINAL_OBSERVABLE_PREDICATES),
                            },
                            "affected_predicate": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["predicate_id", "description"],
                                "properties": {
                                    "predicate_id": {
                                        "type": "string",
                                        "pattern": _PREDICATE_ID_RE.pattern,
                                    },
                                    "description": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 500,
                                    },
                                },
                            },
                            "target_contribution": {
                                "type": "string",
                                "enum": sorted(TARGET_CONTRIBUTIONS),
                            },
                            "competing_trace_defects": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "defect_id",
                                        "first_step",
                                        "description",
                                        "evidence_step_ids",
                                    ],
                                    "properties": {
                                        "defect_id": {"type": "string", "minLength": 1},
                                        "first_step": {"type": "integer", "minimum": 1},
                                        "description": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 1000,
                                        },
                                        "evidence_step_ids": {
                                            "type": "array",
                                            "minItems": 1,
                                            "items": {"type": "string", "minLength": 1},
                                        },
                                    },
                                },
                            },
                            "evidence_step_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "confidence": {
                                "type": "string",
                                "enum": sorted(CONFIDENCE_LEVELS),
                            },
                            "rationale": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                        },
                    },
                },
            }
        )
    else:
        identity_properties.update(
            {
                "phase_b_card_sha256": {"type": "string", "pattern": _SHA256_RE.pattern},
                "task_failure_link_level": {
                    "type": "string",
                    "enum": sorted(FAILURE_LINK_LEVELS),
                },
                "chains": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "candidate_id",
                            "recovery_status",
                            "affected_predicate_id",
                            "target_contribution",
                            "evaluator_predicate",
                            "verifier_alignment",
                            "alternative_sufficient_failure",
                            "failure_link_level",
                            "alternative_defect_ids",
                            "evaluator_revealed_alternatives",
                            "evidence_step_ids",
                            "confidence",
                            "rationale",
                        ],
                        "properties": {
                            "candidate_id": {"type": "string", "minLength": 1},
                            "recovery_status": {
                                "type": "string",
                                "enum": sorted(RECOVERY_STATUSES),
                            },
                            "affected_predicate_id": {
                                "type": "string",
                                "pattern": _PREDICATE_ID_RE.pattern,
                            },
                            "target_contribution": {
                                "type": "string",
                                "enum": sorted(TARGET_CONTRIBUTIONS),
                            },
                            "evaluator_predicate": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "affected_predicate_id",
                                    "evaluator_predicate_description",
                                    "evaluator_evidence",
                                ],
                                "properties": {
                                    "affected_predicate_id": {
                                        "type": "string",
                                        "pattern": _PREDICATE_ID_RE.pattern,
                                    },
                                    "evaluator_predicate_description": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": 1000,
                                    },
                                    "evaluator_evidence": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": ["field_path", "excerpt"],
                                        "properties": {
                                            "field_path": {
                                                "type": "string",
                                                "enum": sorted(EVALUATOR_EVIDENCE_FIELDS),
                                            },
                                            "excerpt": {
                                                "type": "string",
                                                "minLength": 1,
                                                "maxLength": 1000,
                                            },
                                        },
                                    },
                                },
                            },
                            "verifier_alignment": {
                                "type": "string",
                                "enum": sorted(VERIFIER_ALIGNMENTS),
                            },
                            "alternative_sufficient_failure": {
                                "type": "string",
                                "enum": sorted(ALTERNATIVE_SUFFICIENT_FAILURES),
                            },
                            "failure_link_level": {
                                "type": "string",
                                "enum": sorted(FAILURE_LINK_LEVELS),
                            },
                            "alternative_defect_ids": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                            "evaluator_revealed_alternatives": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "alternative_id",
                                        "description",
                                        "evaluator_evidence",
                                    ],
                                    "properties": {
                                        "alternative_id": {
                                            "type": "string",
                                            "pattern": _ALTERNATIVE_ID_RE.pattern,
                                        },
                                        "description": {
                                            "type": "string",
                                            "minLength": 1,
                                            "maxLength": 1000,
                                        },
                                        "evaluator_evidence": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["field_path", "excerpt"],
                                            "properties": {
                                                "field_path": {
                                                    "type": "string",
                                                    "enum": sorted(EVALUATOR_EVIDENCE_FIELDS),
                                                },
                                                "excerpt": {
                                                    "type": "string",
                                                    "minLength": 1,
                                                    "maxLength": 1000,
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                            "evidence_step_ids": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "confidence": {
                                "type": "string",
                                "enum": sorted(CONFIDENCE_LEVELS),
                            },
                            "rationale": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2000,
                            },
                        },
                    },
                },
            }
        )
    required = list(identity_properties)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"MobileWorld failure attribution Phase {phase} review",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": identity_properties,
    }


def _validate_source_specs(
    source_bundles: Sequence[SourceBundle],
) -> tuple[SourceBundle, ...]:
    specs = _sequence(source_bundles, "source_bundles")
    if not specs:
        _fail("source_bundles_empty", "at least one source bundle is required")
    seen: set[str] = set()
    result = []
    for index, spec in enumerate(specs):
        if not isinstance(spec, SourceBundle):
            _fail("source_bundle_type", "source bundle must be SourceBundle", path=f"[{index}]")
        _validate_model_id(spec.model_id)
        if spec.model_id in seen:
            _fail("model_id_duplicate", "model_id is duplicated", path=spec.model_id)
        seen.add(spec.model_id)
        result.append(spec)
    return tuple(sorted(result, key=lambda item: item.model_id))


def _validate_model_id(model_id: str) -> None:
    if not isinstance(model_id, str) or not _MODEL_ID_RE.fullmatch(model_id):
        _fail(
            "model_id",
            "model_id must contain lowercase letters, digits, dots, underscores, or hyphens",
        )


def _task_key(model_id: str, task_name: str) -> str:
    return f"{model_id}/{task_name}"


def _card_sort_key(card: Mapping[str, Any]) -> tuple[str, int, str]:
    return (
        card["model_id"],
        card["task"]["catalog_index"],
        card["task"]["task_name"],
    )


def _card_map(
    cards: Sequence[Mapping[str, Any]], *, expected_record_type: str
) -> dict[str, Mapping[str, Any]]:
    values = _sequence(cards, "cards")
    result: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(values):
        card = _mapping(raw, f"cards[{index}]")
        _literal(
            card.get("schema_version"), ATTRIBUTION_SCHEMA_VERSION, f"cards[{index}].schema_version"
        )
        _literal(card.get("record_type"), expected_record_type, f"cards[{index}].record_type")
        task_key = _nonempty_string(card.get("task_key"), f"cards[{index}].task_key")
        if task_key in result:
            _fail("attribution_card_duplicate", "task_key is duplicated")
        result[task_key] = card
    return result


def _identity_matches_card(
    review: Mapping[str, Any], card: Mapping[str, Any], *, path: str
) -> None:
    expected = {
        "task_key": card["task_key"],
        "model_id": card["model_id"],
        "task_name": card["task"]["task_name"],
        "catalog_index": card["task"]["catalog_index"],
    }
    for key, value in expected.items():
        _literal(review[key], value, f"{path}.{key}")


def _safe_relative_path(value: Any, path: str) -> PurePosixPath:
    text = _nonempty_string(value, path)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or pure.as_posix() != text
        or any(part in {".", ".."} for part in pure.parts)
    ):
        _fail("relative_path", "path must be normalized relative POSIX", path=path)
    return pure


def _record_set_sha256(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(canonical_json_bytes(record))
    return digest.hexdigest()


def _count_and_rates(count: int, *, pool_count: int, all_failure_count: int) -> dict[str, Any]:
    return {
        "task_count": count,
        "percentage_of_failure_strict_mhr_pool": (
            100.0 * count / pool_count if pool_count else None
        ),
        "percentage_of_all_failures": (
            100.0 * count / all_failure_count if all_failure_count else None
        ),
    }


def _detach(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json_bytes(value, newline=False))


def _detach_value(value: Any) -> Any:
    return json.loads(canonical_json_bytes({"value": value}, newline=False))["value"]


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("object_type", "value must be an object", path=path)
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("sequence_type", "value must be a sequence", path=path)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    actual = set(value)
    if actual != expected:
        _fail(
            "object_keys",
            "object has missing or unknown keys",
            path=path,
            missing=sorted(expected - actual),
            unknown=sorted(actual - expected),
        )


def _literal(value: Any, expected: Any, path: str) -> Any:
    if type(value) is not type(expected) or value != expected:
        _fail("literal", f"value must equal {expected!r}", path=path)
    return value


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail("nonempty_string", "value must be a nonempty edge-trimmed string", path=path)
    return value


def _bounded_string(value: Any, path: str, *, maximum: int) -> str:
    text = _nonempty_string(value, path)
    if len(text) > maximum:
        _fail("string_length", f"string exceeds {maximum} characters", path=path)
    return text


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("positive_integer", "value must be a positive integer", path=path)
    return value


def _enum(value: Any, allowed: frozenset[str], path: str) -> str:
    text = _nonempty_string(value, path)
    if text not in allowed:
        _fail("enum", f"unknown enum value {text!r}", path=path, allowed=sorted(allowed))
    return text


def _sorted_unique_strings(value: Any, path: str) -> list[str]:
    items = _sequence(value, path)
    result = [_nonempty_string(item, f"{path}[{index}]") for index, item in enumerate(items)]
    if result != sorted(set(result)):
        _fail("sorted_unique", "strings must be sorted and unique", path=path)
    return result


def _require_sha256(value: Any, path: str) -> str:
    text = _nonempty_string(value, path)
    if not _SHA256_RE.fullmatch(text):
        _fail("sha256", "value must be a lowercase SHA-256 digest", path=path)
    return text


def _fail(code: str, message: str, *, path: str = "$", **context: Any) -> None:
    raise FailureAttributionError(code, message, path=path, context=context)


__all__ = [
    "ALTERNATIVE_SUFFICIENT_FAILURES",
    "ATTRIBUTION_RUBRIC",
    "ATTRIBUTION_SCHEMA_VERSION",
    "CONTINUITY_STATUSES",
    "EVALUATOR_EVIDENCE_FIELDS",
    "FAILURE_LINK_LEVELS",
    "FINAL_OBSERVABLE_PREDICATES",
    "FULL_TRAJECTORY_EVIDENCE",
    "FailureAttributionError",
    "PhaseABundle",
    "PhaseAResolution",
    "PhaseBBundle",
    "PhaseBResolution",
    "RECOVERY_STATUSES",
    "STRICT_MHR_DEFINITION",
    "SUCCESS_CONTROL",
    "SourceBundle",
    "TARGET_CONTRIBUTIONS",
    "VERIFIER_ALIGNMENTS",
    "build_phase_a_bundle",
    "build_phase_b_bundle",
    "compute_phase_b_metrics",
    "load_phase_a_reviews",
    "load_phase_a_resolution",
    "load_phase_b_resolution",
    "phase_a_material_disagreement",
    "phase_a_review_schema",
    "phase_b_material_disagreement",
    "phase_b_review_schema",
    "preflight_phase_b",
    "resolve_phase_a_reviews",
    "resolve_phase_b_reviews",
    "validate_phase_a_reviews",
    "validate_phase_b_reviews",
    "write_phase_a_bundle",
    "write_phase_a_resolution",
    "write_phase_b_bundle",
    "write_phase_b_resolution",
]
