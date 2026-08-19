#!/usr/bin/env python3
"""Build a reproducible probability sample from *seed_baseline* only.

The primary population is the 3,281 immediate pairs P_i -> decision_(i+1).
Tasks are first-stage clusters, stratified by result status and trajectory length.
Within each sampled task, scanner-hit and scanner-non-hit pairs are sampled as
separate second-stage domains.  Every row therefore has a known inclusion
probability and Horvitz--Thompson design weight.

This program creates an annotation sample; scanner signals never become labels.
It only reads the named baseline run and the scanner ledger already derived from
that same run.  It never writes into QR-MW.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BASELINE_ROOT = Path("/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline")
AUDIT_ROOT = Path("/Users/apigo/Desktop/agent monitor/seed_baseline_audit")
DEFAULT_OUTPUT = AUDIT_ROOT / "prevalence_study"
DEFAULT_SEED = 20260815
TASKS_PER_STRATUM = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, default=BASELINE_ROOT)
    parser.add_argument(
        "--candidates",
        type=Path,
        default=AUDIT_ROOT / "output" / "candidates.csv",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--tasks-per-stratum", type=int, default=TASKS_PER_STRATUM)
    return parser.parse_args()


def validate_baseline_root(path: Path) -> Path:
    root = path.expanduser().resolve(strict=True)
    if root.name != "seed_baseline" or not root.is_dir():
        raise ValueError(f"Refusing non-seed_baseline root: {root}")
    return root


def length_stratum(n_steps: int) -> str:
    if n_steps <= 15:
        return "short_02_15"
    if n_steps <= 34:
        return "medium_16_34"
    return "long_35_50"


def parse_score(task_dir: Path) -> tuple[str, float | None]:
    result = task_dir / "result.txt"
    if not result.is_file():
        return "missing", None
    text = result.read_text(encoding="utf-8", errors="replace")
    import re

    match = re.search(r"score:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))", text)
    if not match:
        return "missing", None
    score = float(match.group(1))
    if score == 1.0:
        return "success", score
    if score == 0.0:
        return "failure", score
    return "partial", score


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_scanner_hits(path: Path) -> dict[tuple[str, int], set[str]]:
    hits: dict[tuple[str, int], set[str]] = defaultdict(set)
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            target = int(row["candidate_target_step"])
            for signal in row["candidate_types"].split(";"):
                if signal:
                    hits[(row["task"], target)].add(signal)
    return hits


def load_frame(root: Path, hits: dict[tuple[str, int], set[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pairs: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for task_dir in sorted(root.iterdir(), key=lambda p: p.name):
        if not task_dir.is_dir() or task_dir.is_symlink() or "_backup_" in task_dir.name:
            continue
        traj_path = task_dir / "traj.json"
        if not traj_path.is_file():
            continue
        payload = json.loads(traj_path.read_text(encoding="utf-8", errors="replace"))
        trajectory = payload.get("0", {}).get("traj") or []
        if not trajectory:
            continue
        n_steps = len(trajectory)
        status, score = parse_score(task_dir)
        l_stratum = length_stratum(n_steps)
        task_goal = str(trajectory[0].get("task_goal") or "")
        task_row = {
            "task": task_dir.name,
            "n_steps": n_steps,
            "eligible_immediate_pairs": max(0, n_steps - 1),
            "all_history_exposures": n_steps * (n_steps - 1) // 2,
            "result_status": status,
            "score": "" if score is None else score,
            "length_stratum": l_stratum,
            "task_stratum": f"{status}__{l_stratum}",
            "task_goal": task_goal,
            "traj_path": str(traj_path),
            "traj_sha256": sha256_file(traj_path),
        }
        tasks.append(task_row)
        for index in range(n_steps - 1):
            source = trajectory[index]
            target = trajectory[index + 1]
            source_step = int(source.get("step", index + 1))
            target_step = int(target.get("step", index + 2))
            signal_set = hits.get((task_dir.name, source_step), set())
            source_shot = task_dir / "screenshots" / f"{task_dir.name}-0-{source_step}.png"
            target_shot = task_dir / "screenshots" / f"{task_dir.name}-0-{target_step}.png"
            pair = {
                **task_row,
                "pair_id": f"{task_dir.name}::s{source_step}->s{target_step}",
                "target_turn_id": f"{task_dir.name}::decision_s{target_step}",
                "source_step": source_step,
                "target_step": target_step,
                "lag": 1,
                "prior_text_records_at_target": index + 1,
                "all_prior_source_target_exposures_at_target": index + 1,
                "scanner_domain": "hit" if signal_set else "non_hit",
                "scanner_signals": ";".join(sorted(signal_set)),
                "source_prediction": str(source.get("prediction") or ""),
                "source_action_json": json.dumps(source.get("action") or {}, ensure_ascii=False, sort_keys=True),
                "target_prediction": str(target.get("prediction") or ""),
                "target_action_json": json.dumps(target.get("action") or {}, ensure_ascii=False, sort_keys=True),
                "source_screenshot": str(source_shot) if source_shot.is_file() else "",
                "target_screenshot": str(target_shot) if target_shot.is_file() else "",
            }
            pairs.append(pair)
    return pairs, tasks


def sample_tasks(tasks: list[dict[str, Any]], per_stratum: int, rng: random.Random) -> tuple[set[str], dict[str, tuple[int, int]]]:
    strata: dict[str, list[str]] = defaultdict(list)
    for row in tasks:
        strata[row["task_stratum"]].append(row["task"])
    selected: set[str] = set()
    counts: dict[str, tuple[int, int]] = {}
    for stratum, names in sorted(strata.items()):
        names = sorted(names)
        n_selected = min(per_stratum, len(names))
        chosen = rng.sample(names, n_selected)
        selected.update(chosen)
        counts[stratum] = (len(names), n_selected)
    return selected, counts


def sample_pairs(
    frame: list[dict[str, Any]],
    selected_tasks: set[str],
    task_counts: dict[str, tuple[int, int]],
    rng: random.Random,
    seed: int,
) -> list[dict[str, Any]]:
    by_task_domain: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    domains_by_task: dict[str, set[str]] = defaultdict(set)
    for row in frame:
        by_task_domain[(row["task"], row["scanner_domain"])].append(row)
        domains_by_task[row["task"]].add(row["scanner_domain"])

    sampled: list[dict[str, Any]] = []
    for task in sorted(selected_tasks):
        domains = domains_by_task[task]
        for domain in sorted(domains):
            population = sorted(
                by_task_domain[(task, domain)], key=lambda row: int(row["source_step"])
            )
            # One per domain guarantees both hit and non-hit when both exist.
            # If a task has only one domain, draw two to retain comparable effort.
            m = min(len(population), 1 if len(domains) == 2 else 2)
            chosen = rng.sample(population, m)
            for row in chosen:
                N_tasks, n_tasks = task_counts[row["task_stratum"]]
                pi_task = n_tasks / N_tasks
                pi_pair_given_task = m / len(population)
                inclusion_probability = pi_task * pi_pair_given_task
                sampled.append(
                    {
                        **row,
                        "task_population_in_stratum": N_tasks,
                        "tasks_sampled_in_stratum": n_tasks,
                        "pair_domain_population_in_task": len(population),
                        "pairs_sampled_in_task_domain": m,
                        "task_inclusion_probability": round(pi_task, 12),
                        "conditional_pair_inclusion_probability": round(pi_pair_given_task, 12),
                        "overall_inclusion_probability": round(inclusion_probability, 12),
                        "design_weight": round(1.0 / inclusion_probability, 12),
                        "sample_seed": seed,
                    }
                )
    return sorted(sampled, key=lambda row: (row["task_stratum"], row["task"], int(row["source_step"])))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_sampled_turn_context(path: Path, sample: list[dict[str, Any]]) -> None:
    """Materialize all prior text for each sampled target turn.

    The 50-row pilot labels only the immediate source P_(t-1).  This JSONL is
    deliberately richer: it is the input bundle needed for the next annotation
    pass whose unit is decision turn t and whose history is every P_i, i<t.
    """
    with path.open("w", encoding="utf-8") as handle:
        for row in sample:
            payload = json.loads(
                Path(row["traj_path"]).read_text(encoding="utf-8", errors="replace")
            )
            trajectory = payload.get("0", {}).get("traj") or []
            target_step = int(row["target_step"])
            prior = []
            target_record: dict[str, Any] | None = None
            for index, item in enumerate(trajectory):
                step = int(item.get("step", index + 1))
                screenshot = (
                    Path(row["traj_path"]).parent
                    / "screenshots"
                    / f"{row['task']}-0-{step}.png"
                )
                record = {
                    "step": step,
                    "prediction": str(item.get("prediction") or ""),
                    "action": item.get("action") or {},
                    "observation_path": str(screenshot) if screenshot.is_file() else "",
                }
                if step < target_step:
                    record["lag_to_target"] = target_step - step
                    record["source_observation_in_actor_prompt"] = step >= target_step - 2
                    record["source_and_post_action_observations_both_evicted"] = (
                        target_step - step >= 4
                    )
                    prior.append(record)
                elif step == target_step:
                    target_record = record
                    break
            bundle = {
                "target_turn_id": row["target_turn_id"],
                "narrow_pilot_pair_id": row["pair_id"],
                "task": row["task"],
                "task_goal": row["task_goal"],
                "result_status": row["result_status"],
                "length_stratum": row["length_stratum"],
                "immediate_source_scanner_domain": row["scanner_domain"],
                "design_weight": float(row["design_weight"]),
                "history_policy": "all prior assistant text; latest 3 observation images",
                "prior_history": prior,
                "target_decision": target_record,
                "annotation_fields_next_pass": {
                    "any_invalid_active_claim": "",
                    "relied_on_any_invalid_claim": "",
                    "source_steps_relied_on": [],
                    "harmful_or_off_rubric": "",
                    "confidence": "",
                    "notes": "",
                },
            }
            handle.write(json.dumps(bundle, ensure_ascii=False) + "\n")


def main() -> None:
    args = parse_args()
    root = validate_baseline_root(args.baseline_root)
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    hits = read_scanner_hits(args.candidates)
    frame, tasks = load_frame(root, hits)
    rng = random.Random(args.seed)
    selected_tasks, task_counts = sample_tasks(tasks, args.tasks_per_stratum, rng)
    sample = sample_pairs(frame, selected_tasks, task_counts, rng, args.seed)

    if len(tasks) != 116 or len(frame) != 3281:
        raise RuntimeError(f"Corpus drift: expected 116 tasks/3281 pairs, got {len(tasks)}/{len(frame)}")
    exposures = sum(int(row["all_history_exposures"]) for row in tasks)
    if exposures != 65808:
        raise RuntimeError(f"Corpus drift: expected 65808 all-history exposures, got {exposures}")

    write_csv(output / "task_frame.csv", tasks)
    write_csv(output / "immediate_pair_frame.csv", frame)
    write_csv(output / "pilot_sample.csv", sample)

    annotation_fields = [
        "pair_id",
        "task",
        "source_step",
        "target_step",
        "result_status",
        "length_stratum",
        "scanner_domain",
        "scanner_signals",
        "design_weight",
        "task_goal",
        "source_screenshot",
        "target_screenshot",
        "source_prediction",
        "target_prediction",
        "source_content_status",
        "downstream_use",
        "primary_label",
        "error_type",
        "evidence_steps",
        "confidence",
        "reviewer",
        "review_notes",
    ]
    blank = [
        {
            **row,
            "source_content_status": "",
            "downstream_use": "",
            "primary_label": "",
            "error_type": "",
            "evidence_steps": "",
            "confidence": "",
            "reviewer": "",
            "review_notes": "",
        }
        for row in sample
    ]
    write_csv(output / "pilot_annotations_blank.csv", blank, annotation_fields)
    write_sampled_turn_context(output / "pilot_turn_context_all_history.jsonl", sample)

    by_task_stratum = Counter(row["task_stratum"] for row in tasks)
    by_pair_stratum = Counter(
        (row["result_status"], row["length_stratum"], row["scanner_domain"])
        for row in frame
    )
    sample_domains = Counter(row["scanner_domain"] for row in sample)
    design = {
        "schema_version": "1.0",
        "source_scope": {
            "run": str(root),
            "other_runs_read": False,
            "nonempty_tasks": len(tasks),
            "immediate_pairs": len(frame),
            "all_history_exposures": exposures,
            "scanner_hit_definition": "At least one candidates.csv row derived from the same seed_baseline has candidate_target_step equal to source step i.",
        },
        "recommended_primary_estimand": "Among the 3,281 decision turns t with non-empty text history, the proportion in which P_t explicitly relies on any invalid active-history claim P_i, i<t, in a harmful or off-rubric way.",
        "pilot_estimand": "Narrow feasibility outcome for the immediate P_(t-1) -> decision_t pair only. It cannot estimate all-history propagation because it misses long-range reuse.",
        "secondary_estimands": [
            "Prevalence of INVALID source pre-steps among eligible source records.",
            "Task-level incidence of at least one confirmed propagation event.",
            "Scanner sensitivity and specificity after probability-sample adjudication.",
        ],
        "sampling": {
            "seed": args.seed,
            "stage_1": "SRSWOR of tasks within result_status x trajectory-length strata.",
            "tasks_per_stratum_target": args.tasks_per_stratum,
            "stage_2": "Within each selected task, SRSWOR of one scanner hit and one non-hit when both exist; otherwise two from the sole domain (or all if fewer).",
            "weight": "1 / (task sampling fraction * within-task-domain pair sampling fraction)",
            "selected_tasks": len(selected_tasks),
            "sampled_pairs": len(sample),
            "sample_domain_counts": dict(sample_domains),
        },
        "population_task_strata": dict(sorted(by_task_stratum.items())),
        "population_pair_strata": [
            {
                "result_status": key[0],
                "length_stratum": key[1],
                "scanner_domain": key[2],
                "pairs": count,
            }
            for key, count in sorted(by_pair_stratum.items())
        ],
        "analysis_warning": "Scanner hit is an allocation variable, never a ground-truth label. The 50-pair pilot is immediate-history feasibility work only. A target-turn label must inspect all prior P_i; do not report the narrow pilot as overall pre-step propagation prevalence.",
    }
    (output / "design.json").write_text(
        json.dumps(design, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(design["source_scope"] | design["sampling"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
