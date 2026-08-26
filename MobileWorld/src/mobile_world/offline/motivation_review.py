"""Deterministic schemas and metrics for offline motivation review.

This module belongs to the disposable, versioned derived-data layer.  It does
not read raw audit runs, call a model, or write files.  Callers reconstruct
evidence cards elsewhere, then use the functions here to validate review
records, select independent second reviews, and compute conservative
observational metrics.

The public records are intentionally strict JSON objects.  Unknown fields,
unknown enum values, broken evidence references, non-canonical hashes, and
partial catalog coverage are rejected rather than silently repaired.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Any

REVIEW_SCHEMA_VERSION = "mobileworld.audit.motivation-review/v1"
EXPECTED_TASK_COUNT = 117
DEFAULT_NEGATIVE_AUDIT_RATE = 0.15

RECORD_TYPES = frozenset(
    {
        "task_card",
        "task_review",
        "review_batch",
        "pass2_selection",
        "motivation_metrics",
    }
)
REVIEW_PHASES = frozenset({"PASS1", "PASS2", "NEGATIVE_AUDIT", "ADJUDICATION"})
SCREEN_CLASSES = frozenset({"POSITIVE", "UNCERTAIN", "NEGATIVE"})
COVERAGE_VERDICTS = frozenset({"SUFFICIENT", "INSUFFICIENT"})
CONFIDENCE_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW"})

CLAIM_TYPES = frozenset(
    {
        "OBSERVATION_CLAIM",
        "ACTION_INTENT",
        "ACTION_EXECUTION_CLAIM",
        "SUCCESS_CLAIM",
        "PLAN",
        "SUMMARY_CLAIM",
    }
)
REPRESENTATION_TYPES = frozenset(
    {
        "raw_replay",
        "flat_progress",
        "flat_previous_actions",
        "rolling_summary",
        "hybrid_folding",
        "structured_folding",
    }
)
PROVENANCE_CONFIDENCE = frozenset({"EXACT", "HIGH", "AMBIGUOUS", "MISSING"})
HISTORY_VALIDITY = frozenset(
    {
        "SUPPORTED",
        "REFUTED",
        "STALE",
        "OFFTRACK_TRUE",
        "UNVERIFIABLE",
        "NOT_A_FACTUAL_CLAIM",
    }
)
INVALID_SUBTYPES = frozenset(
    {
        "FALSE_CLAIM",
        "FALSE_SUCCESS",
        "GUI_MISINTERPRETATION",
        "STALE_STATE",
        "TRUE_BUT_OFFTRACK",
        "SUMMARY_CORRUPTION",
        "RESULT_MISALIGNMENT",
    }
)
UPTAKE_EVIDENCE = frozenset(
    {
        "NO_OBSERVED_UPTAKE",
        "BEHAVIOR_CONSISTENT",
        "EXPLICIT_USE",
        "EXPLICIT_REJECTION",
        "UNKNOWN",
    }
)
STATE_CONFOUNDS = frozenset(
    {
        "NONE",
        "CURRENT_GUI_REINFORCES_SAME_PREMISE",
        "CURRENT_GUI_CONTRADICTS_PREMISE",
        "UNKNOWN",
    }
)
DOWNSTREAM_EFFECTS = frozenset(
    {
        "NO_VISIBLE_HARM",
        "UNNECESSARY_ACTION",
        "WRONG_ACTION",
        "REPEATED_ACTION",
        "PREMATURE_TERMINATION",
        "OFFTRACK_CONTINUATION",
        "RECOVERED",
        "UNKNOWN_EFFECT",
    }
)
HARMFUL_EFFECTS = frozenset(
    {
        "UNNECESSARY_ACTION",
        "WRONG_ACTION",
        "REPEATED_ACTION",
        "PREMATURE_TERMINATION",
        "OFFTRACK_CONTINUATION",
    }
)
OUTCOMES = frozenset({"SUCCESS", "FAILURE", "NO_RESULT"})

EVIDENCE_ROLES = frozenset(
    {
        "source_pre",
        "source_prediction",
        "source_action",
        "source_result",
        "source_post",
        "target_pre",
        "target_request",
        "target_prediction",
        "target_action",
        "target_post",
    }
)

_PRIMARY_INVALID = frozenset({"REFUTED", "STALE"})
_POSITIVE_VALIDITY = frozenset({"REFUTED", "STALE", "OFFTRACK_TRUE"})
_STRICT_PROVENANCE = frozenset({"EXACT", "HIGH"})
_LOW_CONFOUND = frozenset({"NONE", "CURRENT_GUI_CONTRADICTS_PREMISE"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class ReviewValidationError(ValueError):
    """A derived review record violates the frozen v1 contract."""

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


class _DuplicateJsonKey(ValueError):
    pass


def canonical_json_bytes(value: Any, *, newline: bool = True) -> bytes:
    """Return canonical UTF-8 JSON bytes used for JSONL records and hashes.

    Canonical JSON here means sorted object keys, compact separators, UTF-8
    characters preserved, finite JSON numbers only, and (by default) exactly
    one trailing LF suitable for one JSONL record.
    """

    _validate_json_tree(value, path="$")
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # Defensive after tree validation.
        raise ReviewValidationError("json_not_canonicalizable", str(exc)) from exc
    return encoded + (b"\n" if newline else b"")


def canonical_sha256(value: Any) -> str:
    """Hash one canonical JSONL record, including its trailing LF."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_canonical_json_line(data: bytes | str) -> dict[str, Any]:
    """Parse exactly one canonical JSONL object and reject duplicate keys."""

    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not isinstance(raw, bytes):
        raise ReviewValidationError("json_line_type", "JSON line must be bytes or str")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise ReviewValidationError(
            "json_line_termination", "canonical JSONL record must end in exactly one LF"
        )
    body = raw[:-1]
    if b"\n" in body or b"\r" in body:
        raise ReviewValidationError(
            "json_line_physical_lines", "one JSONL record must occupy one physical line"
        )
    try:
        value = json.loads(body, object_pairs_hook=_strict_object_pairs)
    except (_DuplicateJsonKey, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReviewValidationError("json_line_invalid", str(exc)) from exc
    if not isinstance(value, dict):
        raise ReviewValidationError("json_line_shape", "JSONL record must be an object")
    if canonical_json_bytes(value) != raw:
        raise ReviewValidationError(
            "json_line_not_canonical", "record bytes do not match canonical serialization"
        )
    return value


def validate_task_cards(
    cards_by_task: Mapping[str, Mapping[str, Any]],
    *,
    expected_task_count: int = EXPECTED_TASK_COUNT,
) -> dict[str, dict[str, Any]]:
    """Validate exact task-card coverage and return a detached name-keyed copy."""

    expected_count = _positive_int(expected_task_count, "expected_task_count")
    if not isinstance(cards_by_task, Mapping):
        _fail("cards_shape", "cards_by_task must be a mapping", path="cards_by_task")
    if len(cards_by_task) != expected_count:
        _fail(
            "card_coverage_count",
            f"expected exactly {expected_count} task cards",
            path="cards_by_task",
            actual=len(cards_by_task),
        )

    detached: dict[str, dict[str, Any]] = {}
    indices: set[int] = set()
    task_run_ids: set[str] = set()
    common: tuple[str, str, str] | None = None
    for mapping_key, card_value in cards_by_task.items():
        task_name = _nonempty_string(mapping_key, "cards_by_task.<key>")
        card = _mapping(card_value, f"cards_by_task[{task_name!r}]")
        _validate_task_card(card, path=f"cards_by_task[{task_name!r}]")
        card_task = card["task"]
        if card_task["task_name"] != task_name:
            _fail(
                "card_key_mismatch",
                "mapping key must equal task.task_name",
                path=f"cards_by_task[{task_name!r}].task.task_name",
            )
        index = card_task["catalog_index"]
        if index in indices:
            _fail("catalog_index_duplicate", "catalog index is duplicated", catalog_index=index)
        indices.add(index)
        task_run_id = card_task["task_run_id"]
        if task_run_id in task_run_ids:
            _fail("task_run_id_duplicate", "task_run_id is duplicated", task_run_id=task_run_id)
        task_run_ids.add(task_run_id)
        identity = (
            card["evaluation_run_id"],
            card["dataset_sha256"],
            card["selection_sha256"],
        )
        if common is None:
            common = identity
        elif identity != common:
            _fail(
                "card_dataset_mismatch",
                "all cards must share evaluation and dataset identity",
                path=f"cards_by_task[{task_name!r}]",
            )
        detached[task_name] = _json_detach(card)

    expected_indices = set(range(1, expected_count + 1))
    if indices != expected_indices:
        _fail(
            "catalog_index_coverage",
            f"catalog indices must be exactly 1..{expected_count}",
            missing=sorted(expected_indices - indices),
            unexpected=sorted(indices - expected_indices),
        )
    return detached


def validate_review_batch(
    payload: Mapping[str, Any],
    cards_by_task: Mapping[str, Mapping[str, Any]],
    expected_phase: str,
) -> tuple[dict[str, Any], ...]:
    """Validate one review-batch payload against all 117 evidence cards.

    The payload is a strict ``review_batch`` object.  The returned tuple is a
    detached copy of its review records in input order; the function never
    mutates the caller's objects.
    """

    phase = _enum(expected_phase, REVIEW_PHASES, "expected_phase")
    cards = validate_task_cards(cards_by_task)
    batch = _mapping(payload, "payload")
    _exact_keys(
        batch,
        {
            "schema_version",
            "record_type",
            "evaluation_run_id",
            "dataset_sha256",
            "selection_sha256",
            "batch_id",
            "phase",
            "reviews",
        },
        path="payload",
    )
    _literal(batch["schema_version"], REVIEW_SCHEMA_VERSION, "payload.schema_version")
    _literal(batch["record_type"], "review_batch", "payload.record_type")
    _nonempty_string(batch["evaluation_run_id"], "payload.evaluation_run_id")
    _sha256(batch["dataset_sha256"], "payload.dataset_sha256")
    _sha256(batch["selection_sha256"], "payload.selection_sha256")
    _nonempty_string(batch["batch_id"], "payload.batch_id")
    _literal(batch["phase"], phase, "payload.phase")
    reviews = _list(batch["reviews"], "payload.reviews")
    if not reviews:
        _fail("review_batch_empty", "review batch must contain at least one review")

    first_card = next(iter(cards.values()))
    batch_identity = (
        batch["evaluation_run_id"],
        batch["dataset_sha256"],
        batch["selection_sha256"],
    )
    card_identity = (
        first_card["evaluation_run_id"],
        first_card["dataset_sha256"],
        first_card["selection_sha256"],
    )
    if batch_identity != card_identity:
        _fail("review_batch_dataset_mismatch", "batch identity does not match task cards")

    seen_tasks: set[str] = set()
    seen_review_ids: set[str] = set()
    detached: list[dict[str, Any]] = []
    for offset, review_value in enumerate(reviews):
        path = f"payload.reviews[{offset}]"
        review = _mapping(review_value, path)
        _validate_review_structure(review, path=path)
        _literal(review["phase"], phase, f"{path}.phase")
        if (
            review["evaluation_run_id"],
            review["dataset_sha256"],
            review["selection_sha256"],
        ) != batch_identity:
            _fail(
                "review_dataset_mismatch",
                "review identity does not match its batch",
                path=path,
            )
        task_name = review["task_name"]
        if task_name in seen_tasks:
            _fail("review_task_duplicate", "task appears twice in one batch", task_name=task_name)
        seen_tasks.add(task_name)
        review_id = review["review_id"]
        if review_id in seen_review_ids:
            _fail("review_id_duplicate", "review_id appears twice", review_id=review_id)
        seen_review_ids.add(review_id)
        card = cards.get(task_name)
        if card is None:
            _fail("review_task_unknown", "review references an unknown task", task_name=task_name)
        _validate_review_against_card(review, card, path=path)
        detached.append(_json_detach(review))
    return tuple(detached)


def validate_primary_coverage(
    primary_reviews: Sequence[Mapping[str, Any]],
    *,
    expected_task_count: int = EXPECTED_TASK_COUNT,
) -> dict[str, dict[str, Any]]:
    """Require one PASS1 review for every canonical catalog task."""

    expected_count = _positive_int(expected_task_count, "expected_task_count")
    reviews = _sequence(primary_reviews, "primary_reviews")
    if len(reviews) != expected_count:
        _fail(
            "primary_coverage_count",
            f"expected exactly {expected_count} PASS1 reviews",
            path="primary_reviews",
            actual=len(reviews),
        )
    by_task: dict[str, dict[str, Any]] = {}
    indices: set[int] = set()
    review_ids: set[str] = set()
    common: tuple[str, str, str] | None = None
    for offset, value in enumerate(reviews):
        path = f"primary_reviews[{offset}]"
        review = _mapping(value, path)
        _validate_review_structure(review, path=path)
        _literal(review["phase"], "PASS1", f"{path}.phase")
        task_name = review["task_name"]
        if task_name in by_task:
            _fail("primary_task_duplicate", "PASS1 task is duplicated", task_name=task_name)
        if review["review_id"] in review_ids:
            _fail("review_id_duplicate", "review_id is duplicated", review_id=review["review_id"])
        review_ids.add(review["review_id"])
        index = review["catalog_index"]
        if index in indices:
            _fail("catalog_index_duplicate", "catalog index is duplicated", catalog_index=index)
        indices.add(index)
        identity = (
            review["evaluation_run_id"],
            review["dataset_sha256"],
            review["selection_sha256"],
        )
        if common is None:
            common = identity
        elif identity != common:
            _fail("primary_dataset_mismatch", "PASS1 reviews span different datasets")
        by_task[task_name] = _json_detach(review)
    expected_indices = set(range(1, expected_count + 1))
    if indices != expected_indices:
        _fail(
            "primary_catalog_coverage",
            f"PASS1 catalog indices must be exactly 1..{expected_count}",
            missing=sorted(expected_indices - indices),
            unexpected=sorted(indices - expected_indices),
        )
    return by_task


def select_pass2(
    primary_reviews: Sequence[Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    dataset_sha: str,
    selection_sha: str,
    rate: float = DEFAULT_NEGATIVE_AUDIT_RATE,
) -> dict[str, Any]:
    """Select all positive/uncertain tasks and a seeded negative audit sample.

    Negatives are stratified by ``app x outcome``.  The total sample is exactly
    ``ceil(rate * N_negative)``; largest-remainder allocation and SHA-256 ranks
    make the result independent of mapping/list insertion order.
    """

    dataset_digest = _sha256(dataset_sha, "dataset_sha")
    selection_digest = _sha256(selection_sha, "selection_sha")
    rate_fraction = _rate_fraction(rate)
    primary = validate_primary_coverage(primary_reviews)
    outcome_map = _validate_outcomes(outcomes, primary)
    first_review = next(iter(primary.values()))
    if first_review["dataset_sha256"] != dataset_digest:
        _fail("dataset_sha_mismatch", "dataset_sha does not match PASS1 reviews")
    if first_review["selection_sha256"] != selection_digest:
        _fail("selection_sha_mismatch", "selection_sha does not match PASS1 reviews")

    seed_material = f"{dataset_digest}\0{selection_digest}\0negative-audit-v1".encode()
    seed_sha = hashlib.sha256(seed_material).hexdigest()
    positive = [review for review in primary.values() if review["task_screen_class"] == "POSITIVE"]
    uncertain = [
        review for review in primary.values() if review["task_screen_class"] == "UNCERTAIN"
    ]
    negatives = [review for review in primary.values() if review["task_screen_class"] == "NEGATIVE"]
    negative_target = _ceil_fraction(rate_fraction * len(negatives))

    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for review in negatives:
        outcome = outcome_map[review["task_name"]]
        strata[(outcome["app"], outcome["outcome"])].append(review)
    allocation = _largest_remainder_allocation(strata, negative_target, seed_sha)

    sampled_negative_names: set[str] = set()
    sampling_hashes: dict[str, str] = {}
    for stratum, members in strata.items():
        ranked = sorted(
            members,
            key=lambda review: (
                _task_sampling_hash(seed_sha, review["catalog_index"], review["task_name"]),
                review["catalog_index"],
            ),
        )
        for review in ranked[: allocation[stratum]]:
            task_name = review["task_name"]
            sampled_negative_names.add(task_name)
            sampling_hashes[task_name] = _task_sampling_hash(
                seed_sha, review["catalog_index"], task_name
            )

    task_records: list[dict[str, Any]] = []
    for review in sorted(primary.values(), key=lambda item: item["catalog_index"]):
        screen = review["task_screen_class"]
        if screen == "NEGATIVE" and review["task_name"] not in sampled_negative_names:
            continue
        if screen == "POSITIVE":
            reason = "PRIMARY_POSITIVE"
            required_phase = "PASS2"
            sampling_hash = None
        elif screen == "UNCERTAIN":
            reason = "PRIMARY_UNCERTAIN"
            required_phase = "PASS2"
            sampling_hash = None
        else:
            reason = "NEGATIVE_RANDOM_AUDIT"
            required_phase = "NEGATIVE_AUDIT"
            sampling_hash = sampling_hashes[review["task_name"]]
        task_records.append(
            {
                "catalog_index": review["catalog_index"],
                "task_name": review["task_name"],
                "primary_class": screen,
                "required_phase": required_phase,
                "selection_reason": reason,
                "sampling_hash": sampling_hash,
            }
        )

    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "pass2_selection",
        "evaluation_run_id": first_review["evaluation_run_id"],
        "dataset_sha256": dataset_digest,
        "selection_sha256": selection_digest,
        "seed_sha256": seed_sha,
        "negative_rate": {
            "numerator": rate_fraction.numerator,
            "denominator": rate_fraction.denominator,
        },
        "primary_counts": {
            "positive": len(positive),
            "uncertain": len(uncertain),
            "negative": len(negatives),
        },
        "negative_population_count": len(negatives),
        "negative_sample_count": len(sampled_negative_names),
        "selected_task_count": len(task_records),
        "tasks": task_records,
    }


def adjudication_needed(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    """Return whether two independent reviews disagree on a metric-critical field."""

    first_review = _mapping(first, "first")
    second_review = _mapping(second, "second")
    _validate_review_structure(first_review, path="first")
    _validate_review_structure(second_review, path="second")
    _literal(first_review["phase"], "PASS1", "first.phase")
    if second_review["phase"] not in {"PASS2", "NEGATIVE_AUDIT"}:
        _fail(
            "second_review_phase",
            "second review phase must be PASS2 or NEGATIVE_AUDIT",
            path="second.phase",
        )
    identity_fields = (
        "evaluation_run_id",
        "dataset_sha256",
        "selection_sha256",
        "task_name",
        "catalog_index",
        "card_sha256",
    )
    for field in identity_fields:
        if first_review[field] != second_review[field]:
            _fail(
                "independent_review_identity_mismatch",
                f"reviews disagree on identity field {field}",
                path=field,
            )
    if first_review["reviewer_id"] == second_review["reviewer_id"]:
        _fail(
            "reviewer_not_independent",
            "first and second review must use different reviewer_id values",
        )
    if first_review["coverage_verdict"] != second_review["coverage_verdict"]:
        return True
    if first_review["task_screen_class"] != second_review["task_screen_class"]:
        return True
    return _critical_chain_signature(first_review) != _critical_chain_signature(second_review)


def compute_metrics(
    final_reviews: Sequence[Mapping[str, Any]],
    cards_by_task: Mapping[str, Mapping[str, Any]],
    outcomes: Mapping[str, Mapping[str, Any]],
    *,
    expected_task_count: int = EXPECTED_TASK_COUNT,
) -> dict[str, Any]:
    """Compute conservative task/exposure counts and motivation strength.

    ``final_reviews`` contains exactly one frozen review per catalog task.  It
    may use PASS1/PASS2/NEGATIVE_AUDIT/ADJUDICATION phase values; workflow code
    is responsible for choosing the agreed or adjudicated record before this
    function is called.
    """

    expected_count = _positive_int(expected_task_count, "expected_task_count")
    cards = validate_task_cards(cards_by_task, expected_task_count=expected_count)
    reviews = _sequence(final_reviews, "final_reviews")
    if len(reviews) != expected_count:
        _fail(
            "final_coverage_count",
            f"expected exactly {expected_count} final reviews",
            actual=len(reviews),
        )

    final_by_task: dict[str, dict[str, Any]] = {}
    indices: set[int] = set()
    review_ids: set[str] = set()
    for offset, value in enumerate(reviews):
        path = f"final_reviews[{offset}]"
        review = _mapping(value, path)
        _validate_review_structure(review, path=path)
        task_name = review["task_name"]
        if task_name in final_by_task:
            _fail("final_task_duplicate", "final task review is duplicated", task_name=task_name)
        if review["review_id"] in review_ids:
            _fail("review_id_duplicate", "review_id is duplicated", review_id=review["review_id"])
        review_ids.add(review["review_id"])
        if review["catalog_index"] in indices:
            _fail(
                "catalog_index_duplicate",
                "final catalog index is duplicated",
                catalog_index=review["catalog_index"],
            )
        indices.add(review["catalog_index"])
        card = cards.get(task_name)
        if card is None:
            _fail("final_task_unknown", "final review references unknown task", task_name=task_name)
        _validate_review_against_card(review, card, path=path)
        final_by_task[task_name] = _json_detach(review)
    expected_indices = set(range(1, expected_count + 1))
    if indices != expected_indices or set(final_by_task) != set(cards):
        _fail("final_catalog_coverage", "final reviews do not cover the exact card catalog")

    outcome_map = _validate_outcomes(outcomes, final_by_task)
    screen_counts = Counter(review["task_screen_class"] for review in final_by_task.values())
    severity_counts: Counter[str] = Counter()
    exposure_counts: Counter[str] = Counter()
    task_sets: dict[str, set[str]] = defaultdict(set)
    incomplete_tasks: set[str] = set()

    for task_name, review in final_by_task.items():
        card = cards[task_name]
        card_candidates = {candidate["candidate_id"]: candidate for candidate in card["candidates"]}
        if review["coverage_verdict"] != "SUFFICIENT" or not _card_coverage_sufficient(card):
            incomplete_tasks.add(task_name)
        for chain in review["chains"]:
            candidate = card_candidates[chain["candidate_id"]]
            validity = chain["history_validity"]
            uptake = chain["uptake_evidence"]
            effects = set(chain["downstream_effects"])
            harmful = bool(effects & HARMFUL_EFFECTS)
            exposure_counts["reviewed_candidate"] += 1
            if validity in _PRIMARY_INVALID:
                exposure_counts["confirmed_primary_invalid"] += 1
                task_sets["confirmed_primary_invalid"].add(task_name)
                severity_counts[_derive_severity(chain)] += 1
                if uptake == "BEHAVIOR_CONSISTENT":
                    exposure_counts["possible_mislead"] += 1
                    task_sets["possible_mislead"].add(task_name)
                if uptake == "EXPLICIT_USE":
                    exposure_counts["broad_explicit_use"] += 1
                    task_sets["broad_explicit_use"].add(task_name)
                if uptake == "EXPLICIT_REJECTION":
                    exposure_counts["explicit_rejection"] += 1
                    task_sets["explicit_rejection"].add(task_name)
                strict = _is_strict_explicit(review, card, candidate, chain)
                if strict:
                    exposure_counts["strict_explicit_use"] += 1
                    task_sets["strict_explicit_use"].add(task_name)
                    if harmful:
                        exposure_counts["strict_harm"] += 1
                        task_sets["strict_harm"].add(task_name)
                        if "RECOVERED" in effects:
                            exposure_counts["strict_harm_recovered"] += 1
                            task_sets["strict_harm_recovered"].add(task_name)
            elif validity == "OFFTRACK_TRUE":
                exposure_counts["confirmed_offtrack"] += 1
                task_sets["confirmed_offtrack"].add(task_name)

    strict_explicit_count = len(task_sets["strict_explicit_use"])
    strict_harm_count = len(task_sets["strict_harm"])
    broad_explicit_count = len(task_sets["broad_explicit_use"])
    strength, strength_rule = _motivation_strength(
        incomplete=bool(incomplete_tasks),
        strict_explicit_task_count=strict_explicit_count,
        strict_harm_task_count=strict_harm_count,
        broad_explicit_task_count=broad_explicit_count,
        confirmed_signal_task_count=len(
            task_sets["confirmed_primary_invalid"] | task_sets["confirmed_offtrack"]
        ),
    )

    outcome_counts = Counter(record["outcome"] for record in outcome_map.values())
    outcome_strata: dict[str, dict[str, int]] = {}
    for outcome_name in sorted(OUTCOMES):
        names = {
            task_name
            for task_name, record in outcome_map.items()
            if record["outcome"] == outcome_name
        }
        outcome_strata[outcome_name] = {
            "task_count": len(names),
            "positive_task_count": len(
                names
                & {
                    task_name
                    for task_name, review in final_by_task.items()
                    if review["task_screen_class"] == "POSITIVE"
                }
            ),
            "strict_explicit_task_count": len(names & task_sets["strict_explicit_use"]),
            "strict_harm_task_count": len(names & task_sets["strict_harm"]),
        }

    strict_explicit_apps = {
        outcome_map[task_name]["app"] for task_name in task_sets["strict_explicit_use"]
    }
    strict_harm_apps = {outcome_map[task_name]["app"] for task_name in task_sets["strict_harm"]}
    task_counts = {
        "confirmed_primary_invalid": len(task_sets["confirmed_primary_invalid"]),
        "confirmed_offtrack": len(task_sets["confirmed_offtrack"]),
        "possible_mislead": len(task_sets["possible_mislead"]),
        "broad_explicit_use": broad_explicit_count,
        "explicit_rejection": len(task_sets["explicit_rejection"]),
        "strict_explicit_use": strict_explicit_count,
        "strict_harm": strict_harm_count,
        "strict_harm_recovered": len(task_sets["strict_harm_recovered"]),
        "successful_strict_harm": len(
            task_sets["strict_harm"]
            & {
                task_name
                for task_name, record in outcome_map.items()
                if record["outcome"] == "SUCCESS"
            }
        ),
    }
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "motivation_metrics",
        "evaluation_run_id": next(iter(final_by_task.values()))["evaluation_run_id"],
        "dataset_sha256": next(iter(final_by_task.values()))["dataset_sha256"],
        "selection_sha256": next(iter(final_by_task.values()))["selection_sha256"],
        "denominators": {
            "audited_task_count": expected_count,
            "reviewed_candidate_count": exposure_counts["reviewed_candidate"],
        },
        "screen_counts": {
            "positive": screen_counts["POSITIVE"],
            "uncertain": screen_counts["UNCERTAIN"],
            "negative": screen_counts["NEGATIVE"],
        },
        "outcome_counts": {
            "success": outcome_counts["SUCCESS"],
            "failure": outcome_counts["FAILURE"],
            "no_result": outcome_counts["NO_RESULT"],
        },
        "exposure_counts": dict(sorted(exposure_counts.items())),
        "severity_counts": {
            level: severity_counts[level]
            for level in ("WEAK_NOISE", "POSSIBLE_MISLEAD", "STRONG_MISLEAD", "EXPLICIT_HARM")
        },
        "task_counts": task_counts,
        "outcome_strata": outcome_strata,
        "strict_explicit_app_count": len(strict_explicit_apps),
        "strict_harm_app_count": len(strict_harm_apps),
        "incomplete_task_count": len(incomplete_tasks),
        "incomplete_tasks": sorted(incomplete_tasks),
        "motivation_strength": {
            "level": strength,
            "rule": strength_rule,
            "causal_claim_supported": False,
        },
    }


def _validate_task_card(card: dict[str, Any], *, path: str) -> None:
    _exact_keys(
        card,
        {
            "schema_version",
            "record_type",
            "evaluation_run_id",
            "dataset_sha256",
            "selection_sha256",
            "task",
            "outcome_blinded",
            "instruction",
            "coverage",
            "trajectory_outline",
            "candidates",
        },
        path=path,
    )
    _literal(card["schema_version"], REVIEW_SCHEMA_VERSION, f"{path}.schema_version")
    _literal(card["record_type"], "task_card", f"{path}.record_type")
    _nonempty_string(card["evaluation_run_id"], f"{path}.evaluation_run_id")
    _sha256(card["dataset_sha256"], f"{path}.dataset_sha256")
    _sha256(card["selection_sha256"], f"{path}.selection_sha256")
    task = _mapping(card["task"], f"{path}.task")
    _exact_keys(
        task,
        {
            "catalog_index",
            "task_name",
            "task_run_id",
            "raw_run_id",
            "source_id",
            "source_relative_run_path",
            "task_stream_relative_path",
        },
        path=f"{path}.task",
    )
    _positive_int(task["catalog_index"], f"{path}.task.catalog_index")
    _nonempty_string(task["task_name"], f"{path}.task.task_name")
    _nonempty_string(task["task_run_id"], f"{path}.task.task_run_id")
    _nonempty_string(task["raw_run_id"], f"{path}.task.raw_run_id")
    source_id = _nonempty_string(task["source_id"], f"{path}.task.source_id")
    if not _SOURCE_ID_RE.fullmatch(source_id):
        _fail(
            "source_id",
            "source_id must use lowercase letters, digits, dots, underscores, or hyphens",
            path=f"{path}.task.source_id",
        )
    _relative_posix_path(task["source_relative_run_path"], f"{path}.task.source_relative_run_path")
    stream_relative_path = _relative_posix_path(
        task["task_stream_relative_path"], f"{path}.task.task_stream_relative_path"
    )
    expected_stream = f"tasks/{task['task_run_id']}/events.jsonl"
    if stream_relative_path != expected_stream:
        _fail(
            "task_stream_relative_path",
            "task stream path must be tasks/<task_run_id>/events.jsonl",
            path=f"{path}.task.task_stream_relative_path",
            expected=expected_stream,
        )
    _literal(card["outcome_blinded"], True, f"{path}.outcome_blinded")
    _nonempty_string(card["instruction"], f"{path}.instruction")

    coverage = _mapping(card["coverage"], f"{path}.coverage")
    _exact_keys(
        coverage,
        {
            "integrity_valid",
            "capture_complete",
            "decision_count",
            "reconstructed_decision_count",
            "history_bearing_decision_count",
            "unique_history_claim_count",
            "actual_exposure_count",
            "scanner_candidate_count",
            "dropped_candidate_count",
            "full_reconstruction_sha256",
        },
        path=f"{path}.coverage",
    )
    _boolean(coverage["integrity_valid"], f"{path}.coverage.integrity_valid")
    _boolean(coverage["capture_complete"], f"{path}.coverage.capture_complete")
    count_fields = (
        "decision_count",
        "reconstructed_decision_count",
        "history_bearing_decision_count",
        "unique_history_claim_count",
        "actual_exposure_count",
        "scanner_candidate_count",
        "dropped_candidate_count",
    )
    for field in count_fields:
        _nonnegative_int(coverage[field], f"{path}.coverage.{field}")
    if coverage["reconstructed_decision_count"] > coverage["decision_count"]:
        _fail(
            "coverage_count_order",
            "reconstructed decisions cannot exceed decisions",
            path=f"{path}.coverage",
        )
    if coverage["history_bearing_decision_count"] > coverage["reconstructed_decision_count"]:
        _fail(
            "coverage_count_order",
            "history-bearing decisions cannot exceed reconstructed decisions",
            path=f"{path}.coverage",
        )
    if coverage["dropped_candidate_count"] != 0:
        _fail(
            "candidate_truncation",
            "candidate records may be partitioned but never silently dropped",
            path=f"{path}.coverage.dropped_candidate_count",
        )
    _sha256(coverage["full_reconstruction_sha256"], f"{path}.coverage.full_reconstruction_sha256")

    outline = _list(card["trajectory_outline"], f"{path}.trajectory_outline")
    if len(outline) != coverage["decision_count"]:
        _fail(
            "outline_count",
            "trajectory outline must contain one entry per decision",
            path=f"{path}.trajectory_outline",
        )
    prior_step = 0
    for offset, value in enumerate(outline):
        item_path = f"{path}.trajectory_outline[{offset}]"
        item = _mapping(value, item_path)
        _exact_keys(
            item,
            {"step", "prediction_excerpt", "parsed_action", "ui_delta", "history_claim_ids"},
            path=item_path,
        )
        step = _positive_int(item["step"], f"{item_path}.step")
        if step <= prior_step:
            _fail("outline_order", "outline steps must be strictly increasing", path=item_path)
        prior_step = step
        _nullable_string(item["prediction_excerpt"], f"{item_path}.prediction_excerpt")
        _nullable_string(item["parsed_action"], f"{item_path}.parsed_action")
        _nullable_string(item["ui_delta"], f"{item_path}.ui_delta")
        _sorted_unique_strings(item["history_claim_ids"], f"{item_path}.history_claim_ids")

    candidates = _list(card["candidates"], f"{path}.candidates")
    if len(candidates) != coverage["scanner_candidate_count"]:
        _fail(
            "candidate_count",
            "candidate array length must equal scanner_candidate_count",
            path=f"{path}.candidates",
        )
    prior_candidate_id = ""
    candidate_ids: set[str] = set()
    source_candidate_ids_by_step: dict[int, set[str]] = defaultdict(set)
    for offset, value in enumerate(candidates):
        candidate_path = f"{path}.candidates[{offset}]"
        candidate = _mapping(value, candidate_path)
        _validate_candidate(candidate, path=candidate_path)
        candidate_id = candidate["candidate_id"]
        if candidate_id in candidate_ids:
            _fail("candidate_id_duplicate", "candidate_id is duplicated", path=candidate_path)
        if prior_candidate_id and candidate_id <= prior_candidate_id:
            _fail(
                "candidate_order",
                "candidates must be sorted by candidate_id",
                path=candidate_path,
            )
        candidate_ids.add(candidate_id)
        prior_candidate_id = candidate_id
        for source_step in candidate["claim"]["source_steps"]:
            source_candidate_ids_by_step[source_step].add(candidate_id)
    for offset, item in enumerate(outline):
        expected_claim_ids = sorted(source_candidate_ids_by_step[item["step"]])
        if item["history_claim_ids"] != expected_claim_ids:
            _fail(
                "outline_claim_references",
                "history_claim_ids must list candidate_ids sourced at this step",
                path=f"{path}.trajectory_outline[{offset}].history_claim_ids",
                expected=expected_claim_ids,
            )


def _validate_candidate(candidate: dict[str, Any], *, path: str) -> None:
    _exact_keys(
        candidate,
        {"candidate_id", "retrieval_reasons", "claim", "exposure", "evidence_refs"},
        path=path,
    )
    _nonempty_string(candidate["candidate_id"], f"{path}.candidate_id")
    retrieval_reasons = _sorted_unique_strings(
        candidate["retrieval_reasons"], f"{path}.retrieval_reasons"
    )
    if not retrieval_reasons:
        _fail("retrieval_reason_empty", "candidate needs at least one retrieval reason", path=path)
    claim = _mapping(candidate["claim"], f"{path}.claim")
    _exact_keys(
        claim,
        {"text", "claim_type", "source_steps", "representation_type", "provenance_confidence"},
        path=f"{path}.claim",
    )
    _nonempty_string(claim["text"], f"{path}.claim.text")
    _enum(claim["claim_type"], CLAIM_TYPES, f"{path}.claim.claim_type")
    source_steps = _sorted_unique_positive_ints(claim["source_steps"], f"{path}.claim.source_steps")
    if not source_steps:
        _fail("source_steps_empty", "candidate claim needs at least one source step", path=path)
    _enum(
        claim["representation_type"],
        REPRESENTATION_TYPES,
        f"{path}.claim.representation_type",
    )
    _enum(
        claim["provenance_confidence"],
        PROVENANCE_CONFIDENCE,
        f"{path}.claim.provenance_confidence",
    )
    exposure = _mapping(candidate["exposure"], f"{path}.exposure")
    _exact_keys(
        exposure,
        {"target_step", "request_path", "was_actually_in_request", "span_sha256"},
        path=f"{path}.exposure",
    )
    target_step = _positive_int(exposure["target_step"], f"{path}.exposure.target_step")
    if any(source_step >= target_step for source_step in source_steps):
        _fail(
            "source_target_order",
            "every history source step must precede the target step",
            path=f"{path}.claim.source_steps",
        )
    _nonempty_string(exposure["request_path"], f"{path}.exposure.request_path")
    _literal(exposure["was_actually_in_request"], True, f"{path}.exposure.was_actually_in_request")
    _sha256(exposure["span_sha256"], f"{path}.exposure.span_sha256")

    refs = _list(candidate["evidence_refs"], f"{path}.evidence_refs")
    if not refs:
        _fail("evidence_refs_empty", "candidate must contain evidence references", path=path)
    prior_ref_id = ""
    roles: set[str] = set()
    seen_ref_ids: set[str] = set()
    for offset, value in enumerate(refs):
        ref_path = f"{path}.evidence_refs[{offset}]"
        ref = _mapping(value, ref_path)
        _exact_keys(
            ref,
            {"ref_id", "role", "event_id", "step", "field_path", "blob_sha256", "excerpt"},
            path=ref_path,
        )
        ref_id = _nonempty_string(ref["ref_id"], f"{ref_path}.ref_id")
        if ref_id in seen_ref_ids:
            _fail("evidence_ref_duplicate", "evidence ref_id is duplicated", path=ref_path)
        if prior_ref_id and ref_id <= prior_ref_id:
            _fail("evidence_ref_order", "evidence refs must be sorted by ref_id", path=ref_path)
        seen_ref_ids.add(ref_id)
        prior_ref_id = ref_id
        role = _enum(ref["role"], EVIDENCE_ROLES, f"{ref_path}.role")
        roles.add(role)
        _nonempty_string(ref["event_id"], f"{ref_path}.event_id")
        step = _positive_int(ref["step"], f"{ref_path}.step")
        if role.startswith("source_") and step not in source_steps:
            _fail(
                "source_evidence_step",
                "source evidence step must be listed in claim.source_steps",
                path=f"{ref_path}.step",
            )
        if role.startswith("target_") and step != target_step:
            _fail(
                "target_evidence_step",
                "target evidence step must equal exposure.target_step",
                path=f"{ref_path}.step",
            )
        _nonempty_string(ref["field_path"], f"{ref_path}.field_path")
        if ref["blob_sha256"] is not None:
            _sha256(ref["blob_sha256"], f"{ref_path}.blob_sha256")
        _string(ref["excerpt"], f"{ref_path}.excerpt")
    if "target_request" not in roles:
        _fail(
            "target_request_reference_missing",
            "candidate must cite the exact target request exposure",
            path=f"{path}.evidence_refs",
        )


def _validate_review_structure(review: dict[str, Any], *, path: str) -> None:
    _exact_keys(
        review,
        {
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
        },
        path=path,
    )
    _literal(review["schema_version"], REVIEW_SCHEMA_VERSION, f"{path}.schema_version")
    _literal(review["record_type"], "task_review", f"{path}.record_type")
    _nonempty_string(review["evaluation_run_id"], f"{path}.evaluation_run_id")
    _sha256(review["dataset_sha256"], f"{path}.dataset_sha256")
    _sha256(review["selection_sha256"], f"{path}.selection_sha256")
    _enum(review["phase"], REVIEW_PHASES, f"{path}.phase")
    _nonempty_string(review["review_id"], f"{path}.review_id")
    _nonempty_string(review["reviewer_id"], f"{path}.reviewer_id")
    _nonempty_string(review["task_name"], f"{path}.task_name")
    _positive_int(review["catalog_index"], f"{path}.catalog_index")
    _sha256(review["card_sha256"], f"{path}.card_sha256")
    coverage = _enum(review["coverage_verdict"], COVERAGE_VERDICTS, f"{path}.coverage_verdict")
    chains = _list(review["chains"], f"{path}.chains")
    prior_candidate_id = ""
    seen_candidate_ids: set[str] = set()
    for offset, value in enumerate(chains):
        chain_path = f"{path}.chains[{offset}]"
        chain = _mapping(value, chain_path)
        _validate_chain(chain, path=chain_path)
        candidate_id = chain["candidate_id"]
        if candidate_id in seen_candidate_ids:
            _fail("review_candidate_duplicate", "candidate is reviewed twice", path=chain_path)
        if prior_candidate_id and candidate_id <= prior_candidate_id:
            _fail(
                "review_chain_order",
                "review chains must be sorted by candidate_id",
                path=chain_path,
            )
        seen_candidate_ids.add(candidate_id)
        prior_candidate_id = candidate_id
    screen_class = _enum(review["task_screen_class"], SCREEN_CLASSES, f"{path}.task_screen_class")
    expected_screen = derive_task_screen_class(coverage, chains)
    if screen_class != expected_screen:
        _fail(
            "screen_class_not_derived",
            f"task_screen_class must be {expected_screen} for these labels",
            path=f"{path}.task_screen_class",
        )
    summary = _nonempty_string(review["summary"], f"{path}.summary")
    if len(summary) > 2000:
        _fail("summary_too_long", "review summary must not exceed 2000 characters", path=path)


def _validate_chain(chain: dict[str, Any], *, path: str) -> None:
    _exact_keys(
        chain,
        {
            "candidate_id",
            "history_validity",
            "invalid_subtypes",
            "uptake_evidence",
            "state_confound",
            "downstream_effects",
            "evidence_ref_ids",
            "confidence",
            "rationale",
        },
        path=path,
    )
    _nonempty_string(chain["candidate_id"], f"{path}.candidate_id")
    validity = _enum(chain["history_validity"], HISTORY_VALIDITY, f"{path}.history_validity")
    subtypes = set(
        _sorted_unique_enums(
            chain["invalid_subtypes"], INVALID_SUBTYPES, f"{path}.invalid_subtypes"
        )
    )
    if validity == "REFUTED" and not (
        subtypes
        & {
            "FALSE_CLAIM",
            "FALSE_SUCCESS",
            "GUI_MISINTERPRETATION",
            "SUMMARY_CORRUPTION",
            "RESULT_MISALIGNMENT",
        }
    ):
        _fail(
            "refuted_subtype_missing",
            "REFUTED requires a refutation subtype",
            path=f"{path}.invalid_subtypes",
        )
    if validity == "STALE" and "STALE_STATE" not in subtypes:
        _fail(
            "stale_subtype_missing",
            "STALE requires STALE_STATE",
            path=f"{path}.invalid_subtypes",
        )
    if validity == "OFFTRACK_TRUE" and "TRUE_BUT_OFFTRACK" not in subtypes:
        _fail(
            "offtrack_subtype_missing",
            "OFFTRACK_TRUE requires TRUE_BUT_OFFTRACK",
            path=f"{path}.invalid_subtypes",
        )
    if validity in {"SUPPORTED", "UNVERIFIABLE", "NOT_A_FACTUAL_CLAIM"} and subtypes:
        _fail(
            "invalid_subtype_incompatible",
            f"{validity} cannot carry invalid_subtypes",
            path=f"{path}.invalid_subtypes",
        )
    _enum(chain["uptake_evidence"], UPTAKE_EVIDENCE, f"{path}.uptake_evidence")
    _enum(chain["state_confound"], STATE_CONFOUNDS, f"{path}.state_confound")
    effects = set(
        _sorted_unique_enums(
            chain["downstream_effects"], DOWNSTREAM_EFFECTS, f"{path}.downstream_effects"
        )
    )
    if not effects:
        _fail("downstream_effect_empty", "at least one downstream effect is required", path=path)
    if "NO_VISIBLE_HARM" in effects and len(effects) != 1:
        _fail(
            "no_harm_not_exclusive",
            "NO_VISIBLE_HARM must be the only downstream effect",
            path=f"{path}.downstream_effects",
        )
    if "UNKNOWN_EFFECT" in effects and len(effects) != 1:
        _fail(
            "unknown_effect_not_exclusive",
            "UNKNOWN_EFFECT must be the only downstream effect",
            path=f"{path}.downstream_effects",
        )
    if "RECOVERED" in effects and not effects.intersection(HARMFUL_EFFECTS):
        _fail(
            "recovered_without_harm",
            "RECOVERED must accompany at least one concrete harmful effect",
            path=f"{path}.downstream_effects",
        )
    evidence_refs = _sorted_unique_strings(chain["evidence_ref_ids"], f"{path}.evidence_ref_ids")
    if not evidence_refs:
        _fail("review_evidence_empty", "chain must cite evidence refs", path=path)
    _enum(chain["confidence"], CONFIDENCE_LEVELS, f"{path}.confidence")
    rationale = _nonempty_string(chain["rationale"], f"{path}.rationale")
    if len(rationale) > 1000:
        _fail("rationale_too_long", "chain rationale must not exceed 1000 characters", path=path)


def _validate_review_against_card(
    review: dict[str, Any], card: dict[str, Any], *, path: str
) -> None:
    if review["catalog_index"] != card["task"]["catalog_index"]:
        _fail("review_catalog_mismatch", "review catalog_index does not match card", path=path)
    identity_fields = ("evaluation_run_id", "dataset_sha256", "selection_sha256")
    for field in identity_fields:
        if review[field] != card[field]:
            _fail("review_card_identity_mismatch", f"review {field} does not match card", path=path)
    expected_card_hash = canonical_sha256(card)
    if review["card_sha256"] != expected_card_hash:
        _fail(
            "review_card_hash_mismatch",
            "review card_sha256 does not match canonical evidence card",
            path=f"{path}.card_sha256",
        )
    candidates = {candidate["candidate_id"]: candidate for candidate in card["candidates"]}
    chains = {chain["candidate_id"]: chain for chain in review["chains"]}
    if set(candidates) != set(chains):
        _fail(
            "review_candidate_coverage",
            "review must label every card candidate exactly once",
            path=f"{path}.chains",
            missing=sorted(set(candidates) - set(chains)),
            unexpected=sorted(set(chains) - set(candidates)),
        )
    if review["coverage_verdict"] == "SUFFICIENT" and not _card_coverage_sufficient(card):
        _fail(
            "coverage_verdict_incompatible",
            "mechanically incomplete card cannot receive SUFFICIENT coverage",
            path=f"{path}.coverage_verdict",
        )
    for candidate_id, chain in chains.items():
        candidate = candidates[candidate_id]
        available_refs = {ref["ref_id"] for ref in candidate["evidence_refs"]}
        cited_refs = set(chain["evidence_ref_ids"])
        if not cited_refs <= available_refs:
            _fail(
                "review_evidence_reference",
                "review cites an evidence ref absent from its candidate",
                path=f"{path}.chains",
                candidate_id=candidate_id,
                missing=sorted(cited_refs - available_refs),
            )
        if chain["history_validity"] == "NOT_A_FACTUAL_CLAIM" and candidate["claim"][
            "claim_type"
        ] not in {"ACTION_INTENT", "PLAN"}:
            _fail(
                "not_factual_claim_type",
                "NOT_A_FACTUAL_CLAIM is only valid for ACTION_INTENT or PLAN",
                path=f"{path}.chains",
                candidate_id=candidate_id,
            )


def _card_coverage_sufficient(card: Mapping[str, Any]) -> bool:
    coverage = card["coverage"]
    return bool(
        coverage["integrity_valid"]
        and coverage["capture_complete"]
        and coverage["decision_count"] == coverage["reconstructed_decision_count"]
        and coverage["dropped_candidate_count"] == 0
    )


def derive_task_screen_class(coverage: str, chains: Sequence[Mapping[str, Any]]) -> str:
    """Derive the task-level screen from coverage and candidate-chain labels."""

    coverage_value = _enum(coverage, COVERAGE_VERDICTS, "coverage_verdict")
    chain_values = _sequence(chains, "chains")
    validities = [
        _enum(
            _mapping(chain, f"chains[{index}]").get("history_validity"),
            HISTORY_VALIDITY,
            f"chains[{index}].history_validity",
        )
        for index, chain in enumerate(chain_values)
    ]
    if any(validity in _POSITIVE_VALIDITY for validity in validities):
        return "POSITIVE"
    if coverage_value == "INSUFFICIENT" or "UNVERIFIABLE" in validities:
        return "UNCERTAIN"
    return "NEGATIVE"


def _derive_severity(chain: Mapping[str, Any]) -> str:
    effects = set(chain["downstream_effects"])
    if chain["uptake_evidence"] == "EXPLICIT_USE":
        return "EXPLICIT_HARM" if effects & HARMFUL_EFFECTS else "STRONG_MISLEAD"
    if chain["uptake_evidence"] == "BEHAVIOR_CONSISTENT":
        return "POSSIBLE_MISLEAD"
    return "WEAK_NOISE"


def _is_strict_explicit(
    review: Mapping[str, Any],
    card: Mapping[str, Any],
    candidate: Mapping[str, Any],
    chain: Mapping[str, Any],
) -> bool:
    return bool(
        review["coverage_verdict"] == "SUFFICIENT"
        and _card_coverage_sufficient(card)
        and candidate["exposure"]["was_actually_in_request"] is True
        and candidate["claim"]["provenance_confidence"] in _STRICT_PROVENANCE
        and chain["history_validity"] in _PRIMARY_INVALID
        and chain["uptake_evidence"] == "EXPLICIT_USE"
        and chain["state_confound"] in _LOW_CONFOUND
    )


def _motivation_strength(
    *,
    incomplete: bool,
    strict_explicit_task_count: int,
    strict_harm_task_count: int,
    broad_explicit_task_count: int,
    confirmed_signal_task_count: int,
) -> tuple[str, str]:
    if incomplete:
        return (
            "INCONCLUSIVE",
            "at least one task has incomplete mechanical or review coverage",
        )
    if strict_explicit_task_count >= 3 and strict_harm_task_count >= 2:
        return (
            "STRONG_OBSERVATIONAL",
            ">=3 independent strict-explicit tasks and >=2 strict-harm tasks",
        )
    if 1 <= strict_explicit_task_count <= 2 or broad_explicit_task_count >= 3:
        return (
            "MODERATE_OBSERVATIONAL",
            "1-2 strict-explicit tasks or >=3 broad explicit-use tasks",
        )
    if confirmed_signal_task_count >= 1:
        return (
            "WEAK_OBSERVATIONAL",
            "confirmed invalid/off-track exposure without moderate explicit-use support",
        )
    return (
        "NOT_SUPPORTED",
        "no confirmed invalid or off-track exposure in the complete reviewed catalog",
    )


def _validate_outcomes(
    outcomes: Mapping[str, Mapping[str, Any]],
    tasks: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(outcomes, Mapping):
        _fail("outcomes_shape", "outcomes must be a mapping keyed by task_name")
    if set(outcomes) != set(tasks):
        _fail(
            "outcome_coverage",
            "outcomes must cover exactly the reviewed task names",
            missing=sorted(set(tasks) - set(outcomes)),
            unexpected=sorted(set(outcomes) - set(tasks)),
        )
    detached: dict[str, dict[str, Any]] = {}
    seen_indices: set[int] = set()
    for task_name, value in outcomes.items():
        _nonempty_string(task_name, "outcomes.<key>")
        path = f"outcomes[{task_name!r}]"
        record = _mapping(value, path)
        _exact_keys(
            record,
            {"task_name", "catalog_index", "app", "outcome"},
            optional={"score"},
            path=path,
        )
        _literal(record["task_name"], task_name, f"{path}.task_name")
        index = _positive_int(record["catalog_index"], f"{path}.catalog_index")
        if index != tasks[task_name]["catalog_index"]:
            _fail(
                "outcome_catalog_mismatch", "outcome catalog_index does not match review", path=path
            )
        if index in seen_indices:
            _fail("outcome_catalog_duplicate", "outcome catalog_index is duplicated", path=path)
        seen_indices.add(index)
        _nonempty_string(record["app"], f"{path}.app")
        outcome = _enum(record["outcome"], OUTCOMES, f"{path}.outcome")
        if "score" in record:
            score = record["score"]
            if score is not None:
                score = _finite_number(score, f"{path}.score")
            if outcome == "NO_RESULT" and score is not None:
                _fail("outcome_score_mismatch", "NO_RESULT requires a null score", path=path)
            if outcome == "SUCCESS" and (score is None or score <= 0.99):
                _fail("outcome_score_mismatch", "SUCCESS requires score > 0.99", path=path)
            if outcome == "FAILURE" and (score is None or score > 0.99):
                _fail("outcome_score_mismatch", "FAILURE requires numeric score <= 0.99", path=path)
        detached[task_name] = _json_detach(record)
    return detached


def _largest_remainder_allocation(
    strata: Mapping[tuple[str, str], Sequence[Mapping[str, Any]]],
    target: int,
    seed_sha: str,
) -> dict[tuple[str, str], int]:
    population = sum(len(members) for members in strata.values())
    if target < 0 or target > population:
        _fail("sample_target", "negative sample target is outside the population")
    if population == 0:
        return {stratum: 0 for stratum in strata}
    allocation: dict[tuple[str, str], int] = {}
    remainders: list[tuple[int, str, tuple[str, str]]] = []
    for stratum, members in strata.items():
        scaled = target * len(members)
        allocation[stratum] = scaled // population
        remainder = scaled % population
        tie_hash = hashlib.sha256(f"{seed_sha}\0{stratum[0]}\0{stratum[1]}".encode()).hexdigest()
        remainders.append((remainder, tie_hash, stratum))
    remaining = target - sum(allocation.values())
    for _, _, stratum in sorted(remainders, key=lambda item: (-item[0], item[1]))[:remaining]:
        allocation[stratum] += 1
    return allocation


def _task_sampling_hash(seed_sha: str, catalog_index: int, task_name: str) -> str:
    return hashlib.sha256(f"{seed_sha}\0{catalog_index}\0{task_name}".encode()).hexdigest()


def _critical_chain_signature(review: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        (
            chain["candidate_id"],
            chain["history_validity"],
            tuple(chain["invalid_subtypes"]),
            chain["uptake_evidence"],
            chain["state_confound"],
            tuple(chain["downstream_effects"]),
        )
        for chain in review["chains"]
    )


def _rate_fraction(rate: float) -> Fraction:
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        _fail("negative_rate_type", "negative audit rate must be an int or float")
    if isinstance(rate, float) and not math.isfinite(rate):
        _fail("negative_rate_finite", "negative audit rate must be finite")
    fraction = Fraction(str(rate))
    if not Fraction(1, 10) <= fraction <= Fraction(1, 5):
        _fail("negative_rate_range", "negative audit rate must be between 0.10 and 0.20")
    return fraction


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _validate_json_tree(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail("json_nonfinite", "JSON number must be finite", path=path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _fail("json_key_type", "JSON object keys must be strings", path=path)
            _validate_json_tree(item, path=f"{path}.{key}")
        return
    _fail(
        "json_value_type",
        f"value of type {type(value).__name__} is not a JSON value",
        path=path,
    )


def _json_detach(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json_bytes(value, newline=False))


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("object_type", "value must be a JSON object", path=path)
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        _fail("array_type", "value must be a JSON array", path=path)
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("sequence_type", "value must be a sequence of review objects", path=path)
    return value


def _exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    *,
    optional: set[str] | None = None,
    path: str,
) -> None:
    optional_keys = optional or set()
    actual = set(value)
    missing = required - actual
    unknown = actual - required - optional_keys
    if missing or unknown:
        _fail(
            "object_keys",
            "object has missing or unknown keys",
            path=path,
            missing=sorted(missing),
            unknown=sorted(unknown),
        )


def _literal(value: Any, expected: Any, path: str) -> Any:
    if type(value) is not type(expected) or value != expected:
        _fail("literal_value", f"value must equal {expected!r}", path=path)
    return value


def _enum(value: Any, allowed: frozenset[str], path: str) -> str:
    item = _string(value, path)
    if item not in allowed:
        _fail("enum_value", f"unknown enum value {item!r}", path=path, allowed=sorted(allowed))
    return item


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        _fail("string_type", "value must be a string", path=path)
    return value


def _nonempty_string(value: Any, path: str) -> str:
    item = _string(value, path)
    if not item or item != item.strip():
        _fail("string_nonempty", "value must be nonempty with no edge whitespace", path=path)
    return item


def _nullable_string(value: Any, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        _fail("boolean_type", "value must be a boolean", path=path)
    return value


def _positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail("positive_integer", "value must be a positive integer", path=path)
    return value


def _nonnegative_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("nonnegative_integer", "value must be a nonnegative integer", path=path)
    return value


def _finite_number(value: Any, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("number_type", "value must be a JSON number", path=path)
    if isinstance(value, float) and not math.isfinite(value):
        _fail("number_finite", "number must be finite", path=path)
    return value


def _sha256(value: Any, path: str) -> str:
    digest = _string(value, path)
    if not _SHA256_RE.fullmatch(digest):
        _fail("sha256", "value must be 64 lowercase hexadecimal characters", path=path)
    return digest


def _relative_posix_path(value: Any, path: str) -> str:
    text = _nonempty_string(value, path)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or pure.as_posix() != text
        or any(part in {".", ".."} for part in pure.parts)
    ):
        _fail(
            "relative_posix_path",
            "path must be a normalized relative POSIX path without dot traversal",
            path=path,
        )
    return text


def _sorted_unique_strings(value: Any, path: str) -> list[str]:
    items = _list(value, path)
    result = [_nonempty_string(item, f"{path}[{index}]") for index, item in enumerate(items)]
    if result != sorted(set(result)):
        _fail("sorted_unique", "array must be lexicographically sorted and unique", path=path)
    return result


def _sorted_unique_enums(value: Any, allowed: frozenset[str], path: str) -> list[str]:
    items = _list(value, path)
    result = [_enum(item, allowed, f"{path}[{index}]") for index, item in enumerate(items)]
    if result != sorted(set(result)):
        _fail("sorted_unique", "enum array must be sorted and unique", path=path)
    return result


def _sorted_unique_positive_ints(value: Any, path: str) -> list[int]:
    items = _list(value, path)
    result = [_positive_int(item, f"{path}[{index}]") for index, item in enumerate(items)]
    if result != sorted(set(result)):
        _fail("sorted_unique", "integer array must be sorted and unique", path=path)
    return result


def _fail(code: str, message: str, *, path: str = "$", **context: Any) -> None:
    raise ReviewValidationError(code, message, path=path, context=context)


__all__ = [
    "DEFAULT_NEGATIVE_AUDIT_RATE",
    "EXPECTED_TASK_COUNT",
    "HARMFUL_EFFECTS",
    "REVIEW_SCHEMA_VERSION",
    "ReviewValidationError",
    "adjudication_needed",
    "canonical_json_bytes",
    "canonical_sha256",
    "compute_metrics",
    "derive_task_screen_class",
    "load_canonical_json_line",
    "select_pass2",
    "validate_primary_coverage",
    "validate_review_batch",
    "validate_task_cards",
]
