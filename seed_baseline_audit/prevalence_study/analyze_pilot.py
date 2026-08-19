#!/usr/bin/env python3
"""Merge the completed 50-pair pilot review and compute bounded summaries.

The pilot is intentionally narrow: it adjudicates only P_(t-1) relative to P_t.
It does not turn negative immediate-pair labels into negative all-history-turn
labels.  Overall claims are limited to deterministic, manually confirmed lower
bounds assembled from the existing case ledger plus new confirmed pilot rows.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path("/Users/apigo/Desktop/agent monitor/seed_baseline_audit")
STUDY = ROOT / "prevalence_study"
POPULATION_TURNS = 3281
POPULATION_EXPOSURES = 65808
POPULATION_TASKS = 116

LABEL_FIELDS = [
    "source_content_status",
    "downstream_use",
    "primary_label",
    "error_type",
    "evidence_steps",
    "confidence",
    "review_notes",
]


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    sample = load_csv(STUDY / "pilot_sample.csv")
    label_payload = json.loads(
        (STUDY / "pilot_manual_labels.json").read_text(encoding="utf-8")
    )
    labels = label_payload["labels"]
    sample_ids = {row["pair_id"] for row in sample}
    if sample_ids != set(labels):
        raise RuntimeError(
            "Pilot/label ID mismatch: "
            f"missing={sorted(sample_ids - set(labels))}, "
            f"extra={sorted(set(labels) - sample_ids)}"
        )

    reviewed: list[dict[str, Any]] = []
    for row in sample:
        values = labels[row["pair_id"]]
        if len(values) != len(LABEL_FIELDS):
            raise RuntimeError(f"Bad label width for {row['pair_id']}: {len(values)}")
        reviewed.append(
            {
                **row,
                **dict(zip(LABEL_FIELDS, values)),
                "reviewer": label_payload["reviewer"],
            }
        )
    write_csv(STUDY / "pilot_annotations_reviewed.csv", reviewed)

    category = {
        "CONFIRMED_PROPAGATION": "propagation",
        "INVALID_REJECTED_OR_MUTATED": "invalid_rejected_or_self_corrected",
        "ACTION_FAILURE_CONTROL": "action_failure_control",
        "NO_INVALID_SOURCE": "no_invalid_source",
        "UNVERIFIABLE": "unverifiable",
    }
    label_counts = Counter(category[row["primary_label"]] for row in reviewed)
    domain_counts = Counter(row["scanner_domain"] for row in reviewed)
    cross_tab = Counter(
        (row["scanner_domain"], category[row["primary_label"]]) for row in reviewed
    )

    confirmed_invalid = [
        row
        for row in reviewed
        if row["primary_label"]
        in {"CONFIRMED_PROPAGATION", "INVALID_REJECTED_OR_MUTATED"}
    ]
    propagation = [
        row for row in reviewed if row["primary_label"] == "CONFIRMED_PROPAGATION"
    ]

    weight_sum = sum(float(row["design_weight"]) for row in reviewed)
    propagation_weight = sum(float(row["design_weight"]) for row in propagation)
    invalid_weight = sum(float(row["design_weight"]) for row in confirmed_invalid)
    weight_square_sum = sum(float(row["design_weight"]) ** 2 for row in reviewed)
    kish_effective_n = weight_sum**2 / weight_square_sum

    # Deterministic lower bounds: every listed event was directly confirmed.
    # Unreviewed population units may add positives but cannot remove these.
    manual = json.loads(
        (ROOT / "output" / "manual_review.json").read_text(encoding="utf-8")
    )
    exposure_pairs: set[tuple[str, int, int]] = set()
    positive_turns: set[tuple[str, int]] = set()
    immediate_pairs: set[tuple[str, int, int]] = set()
    positive_tasks: set[str] = set()
    for case in manual["cases"]:
        if case["label"] != "CONFIRMED_MISLEADING":
            continue
        task = case["task"]
        source = int(case["source_step"])
        positive_tasks.add(task)
        for target in case["target_steps"]:
            target = int(target)
            exposure_pairs.add((task, source, target))
            positive_turns.add((task, target))
            if target == source + 1:
                immediate_pairs.add((task, source, target))
    for row in propagation:
        task = row["task"]
        source = int(row["source_step"])
        target = int(row["target_step"])
        positive_tasks.add(task)
        exposure_pairs.add((task, source, target))
        positive_turns.add((task, target))
        if target == source + 1:
            immediate_pairs.add((task, source, target))

    confirmed_immediate_domain = Counter()
    frame = {
        (row["task"], int(row["source_step"])): row["scanner_domain"]
        for row in load_csv(STUDY / "immediate_pair_frame.csv")
    }
    for task, source, _target in immediate_pairs:
        confirmed_immediate_domain[frame[(task, source)]] += 1

    results = {
        "schema_version": "1.0",
        "scope": {
            "run": "seed_baseline only",
            "pilot_unit": "immediate pair P_(t-1) -> decision_t",
            "reviewed_pairs": len(reviewed),
            "selected_tasks": len({row["task"] for row in reviewed}),
            "single_reviewer": True,
            "publication_ready": False,
            "stopping_rule": "All 50 probability-sampled immediate pairs were reviewed once; uncertain evidence was labeled UNVERIFIABLE rather than forced negative.",
            "critical_limitation": "A negative immediate-pair label does not rule out reliance on an older P_i. The recommended primary unit is target decision turn t with all P_i, i<t, visible to the annotator.",
        },
        "pilot_counts": {
            "scanner_domains": dict(domain_counts),
            "categories": dict(label_counts),
            "confirmed_invalid_source_total": len(confirmed_invalid),
            "confirmed_invalid_breakdown": {
                "harmfully_propagated": len(propagation),
                "rejected_or_self_corrected_next": len(confirmed_invalid)
                - len(propagation),
            },
            "scanner_by_category": [
                {"scanner_domain": key[0], "category": key[1], "n": count}
                for key, count in sorted(cross_tab.items())
            ],
        },
        "weighted_pilot_descriptives": {
            "warning": "Feasibility-only narrow immediate-pair descriptives; not an estimate of all-history decision-turn propagation and no formal CI is reported.",
            "sum_design_weights": weight_sum,
            "kish_effective_n": kish_effective_n,
            "horvitz_thompson_immediate_propagation_fraction": propagation_weight
            / POPULATION_TURNS,
            "hajek_immediate_propagation_fraction": propagation_weight / weight_sum,
            "horvitz_thompson_invalid_source_fraction": invalid_weight
            / POPULATION_TURNS,
            "hajek_invalid_source_fraction": invalid_weight / weight_sum,
            "why_no_ci": "The pilot samples mostly one pair per task x scanner domain, so within-domain second-stage variance is not estimable; n_eff is also very small because weights vary sharply.",
        },
        "legacy_ledger_plus_pilot_lower_bounds": {
            "interpretation": "Deterministic observed lower bounds computed only from output/manual_review.json's legacy six confirmed cases plus this 50-pair pilot. They are not confidence intervals and are not the final total for any broader/newer case ledger; synchronize and deduplicate that ledger before publication.",
            "decision_turns_relying_on_an_invalid_history_claim": {
                "confirmed": len(positive_turns),
                "population": POPULATION_TURNS,
                "lower_bound_fraction": len(positive_turns) / POPULATION_TURNS,
            },
            "immediate_source_target_pairs": {
                "confirmed": len(immediate_pairs),
                "population": POPULATION_TURNS,
                "lower_bound_fraction": len(immediate_pairs) / POPULATION_TURNS,
            },
            "all_history_source_target_exposures": {
                "confirmed": len(exposure_pairs),
                "population": POPULATION_EXPOSURES,
                "lower_bound_fraction": len(exposure_pairs) / POPULATION_EXPOSURES,
            },
            "tasks_with_at_least_one_confirmed_event": {
                "confirmed": len(positive_tasks),
                "population": POPULATION_TASKS,
                "lower_bound_fraction": len(positive_tasks) / POPULATION_TASKS,
            },
            "confirmed_immediate_pairs_by_scanner_domain": dict(
                confirmed_immediate_domain
            ),
        },
    }
    (STUDY / "pilot_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
