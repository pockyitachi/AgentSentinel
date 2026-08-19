#!/usr/bin/env python3
"""Conservative candidate scan for Seed-2.0-Pro MobileWorld baselines.

This script does *not* label a pre-step as misleading.  It creates an
auditable candidate ledger from agent-native baseline trajectories using:

* explicit self-correction and failed-transition language;
* repeated/near-duplicate reasoning;
* repeated actions, optionally paired with a visually static transition;
* unsuccessful terminal completion claims.

Every candidate still requires human review against the adjacent screenshots
and subsequent decisions.  No verifier/reflection output is read.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image


DEFAULT_INPUT = Path("/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline")
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"

SELF_CORRECTION_PATTERNS = (
    r"\bi (?:was|am|have been) wrong\b",
    r"\bi (?:made|keep making|kept making|have made) (?:a |the )?mistake\b",
    r"\bi (?:messed|screwed) up\b",
    r"\bi (?:misread|misinterpreted|misunderstood|confused|mixed up)\b",
    r"\bi (?:clicked|opened|selected|targeted|tapped) (?:the )?wrong\b",
    r"\bthat(?:'s| is) (?:the |my )?(?:mistake|error|problem)\b",
    r"\b(?:i thought|i assumed).{0,180}\bbut\b",
    r"\boh my god.{0,180}\b(?:mistake|wrong|error|confused|mixed up)\b",
)

FAILED_TRANSITION_PATTERNS = (
    r"\b(?:previous|last) (?:tap|click|action|attempt|swipe|scroll).{0,160}\b(?:didn'?t|did not|failed|no change)\b",
    r"\b(?:didn'?t|did not) (?:work|open|change|move|advance|respond|register|save|send|select|toggle)\b",
    r"\bnothing (?:happened|changed|moved|opened)\b",
    r"\bno (?:visible )?(?:change|response|progress)\b",
    r"\bstill (?:on|at|in|shows?|showing|open|here)\b",
)

LOOP_PATTERNS = (
    r"\b(?:in a |this )?loop\b",
    r"\bkeep (?:clicking|opening|returning|trying|going|repeating)\b",
    r"\bkept (?:clicking|opening|returning|trying|going|repeating)\b",
    r"\brepeat(?:ing|ed)? (?:the )?(?:same|this)\b",
    r"\bagain and again\b",
    r"\bthis whole time\b",
)

PROGRESS_PATTERNS = (
    r"\bsuccessfully\b",
    r"\bthat (?:worked|succeeded)\b",
    r"\b(?:is|are) now (?:selected|open|opened|set|added|removed|saved|sent|enabled|disabled)\b",
    r"\bi (?:have|just) (?:opened|added|removed|saved|sent|selected|completed|finished|unfollowed|followed|pinned|bookmarked)\b",
    r"\bhas been (?:added|removed|saved|sent|selected|completed|set|enabled|disabled)\b",
)

COMPLETION_PATTERNS = (
    r"\btask (?:is )?(?:complete|completed|done|finished)\b",
    r"\bthat completes? (?:the |this )?task\b",
    r"\beverything (?:is|has been) (?:set|done|complete|completed)\b",
    r"\ball (?:requirements|steps|items|tasks).{0,80}\b(?:done|complete|completed|satisfied)\b",
)

TAG_RE = re.compile(r"<[^>]+>")
TOOL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.IGNORECASE | re.DOTALL)
THINK_RE = re.compile(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", re.IGNORECASE | re.DOTALL)
POINT_RE = re.compile(r"<point>.*?</point>", re.IGNORECASE | re.DOTALL)
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
SPACE_RE = re.compile(r"\s+")


@dataclass
class ImageMetrics:
    mad: float | None = None
    changed_fraction: float | None = None
    dhash_distance: int | None = None

    @property
    def is_static(self) -> bool:
        return (
            self.mad is not None
            and self.changed_fraction is not None
            and self.dhash_distance is not None
            and self.mad <= 0.0035
            and self.changed_fraction <= 0.012
            and self.dhash_distance <= 2
        )


def _matches(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def thinking_text(prediction: str) -> str:
    match = THINK_RE.search(prediction or "")
    if match:
        return SPACE_RE.sub(" ", match.group(1)).strip()
    without_tool = TOOL_BLOCK_RE.sub(" ", prediction or "")
    return SPACE_RE.sub(" ", TAG_RE.sub(" ", without_tool)).strip()


def normalize_text(text: str) -> str:
    text = POINT_RE.sub(" point ", text.lower())
    text = NUMBER_RE.sub(" number ", text)
    text = re.sub(r"[^a-z0-9_\u4e00-\u9fff]+", " ", text)
    return SPACE_RE.sub(" ", text).strip()


def token_jaccard(left: str, right: str) -> float:
    a = set(normalize_text(left).split())
    b = set(normalize_text(right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def action_key(action: dict[str, Any], grid: int = 48) -> str:
    kind = str(action.get("action_type", "unknown"))
    if kind in {"click", "long_press", "double_tap", "scroll"}:
        x = action.get("x")
        y = action.get("y")
        qx = round(float(x) / grid) * grid if x is not None else None
        qy = round(float(y) / grid) * grid if y is not None else None
        direction = action.get("direction")
        return f"{kind}:{qx}:{qy}:{direction or ''}"
    if kind == "drag":
        values = []
        for key in ("start_x", "start_y", "end_x", "end_y"):
            value = action.get(key)
            values.append(round(float(value) / grid) * grid if value is not None else None)
        return f"drag:{':'.join(map(str, values))}"
    if kind in {"input_text", "type"}:
        # The text is intentionally not copied into the key/output.
        return f"{kind}:<TEXT>"
    return f"{kind}:{action.get('direction') or action.get('button') or action.get('app_name') or ''}"


def _dhash_bits(image: Image.Image) -> np.ndarray:
    small = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = np.asarray(small, dtype=np.int16)
    return (pixels[:, 1:] > pixels[:, :-1]).reshape(-1)


def image_metrics(before_path: Path, after_path: Path) -> ImageMetrics:
    if not before_path.exists() or not after_path.exists():
        return ImageMetrics()
    with Image.open(before_path) as before_image, Image.open(after_path) as after_image:
        before = before_image.convert("L").resize((128, 256), Image.Resampling.BILINEAR)
        after = after_image.convert("L").resize((128, 256), Image.Resampling.BILINEAR)
        left = np.asarray(before, dtype=np.int16)
        right = np.asarray(after, dtype=np.int16)
        delta = np.abs(left - right)
        return ImageMetrics(
            mad=float(delta.mean() / 255.0),
            changed_fraction=float((delta > 12).mean()),
            dhash_distance=int(np.count_nonzero(_dhash_bits(before_image) != _dhash_bits(after_image))),
        )


def parse_result(path: Path) -> tuple[float | None, str]:
    if not path.exists():
        return None, "missing"
    text = path.read_text(errors="replace")
    match = re.search(r"score:\s*(-?\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if not match:
        return None, "unparseable"
    score = float(match.group(1))
    if math.isclose(score, 1.0):
        return score, "success"
    if math.isclose(score, 0.0):
        return score, "failure"
    return score, "partial"


def task_dirs(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and "_backup_" not in path.name and (path / "traj.json").exists()
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return float(np.quantile(np.asarray(values, dtype=float), q))


def scan(root: Path, output: Path) -> dict[str, Any]:
    all_steps: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    task_rows: list[dict[str, Any]] = []

    for task_dir in task_dirs(root):
        payload = json.loads((task_dir / "traj.json").read_text(errors="replace"))
        run = payload.get("0", {})
        trajectory = run.get("traj") or []
        score, result_status = parse_result(task_dir / "result.txt")
        n_steps = len(trajectory)
        task_goal = trajectory[0].get("task_goal", "") if trajectory else ""
        action_history: list[str] = []
        text_history: list[str] = []
        task_signal_counts: Counter[str] = Counter()

        step_records: list[dict[str, Any]] = []
        for index, item in enumerate(trajectory):
            step = int(item.get("step", index + 1))
            prediction = str(item.get("prediction") or "")
            thought = thinking_text(prediction)
            lowered = thought.lower()
            action = item.get("action") or {}
            key = action_key(action)
            recent_actions = action_history[max(0, len(action_history) - 8) :]
            same_action_recent_count = recent_actions.count(key)

            recent_texts = text_history[max(0, len(text_history) - 5) :]
            previous_similarity = token_jaccard(thought, text_history[-1]) if text_history else 0.0
            recent_similarities = [token_jaccard(thought, previous) for previous in recent_texts]
            max_recent_similarity = max(recent_similarities, default=0.0)

            before_path = task_dir / "screenshots" / f"{task_dir.name}-0-{step}.png"
            after_path = task_dir / "screenshots" / f"{task_dir.name}-0-{step + 1}.png"
            metrics = image_metrics(before_path, after_path)

            explicit_self_correction = _matches(SELF_CORRECTION_PATTERNS, lowered)
            failed_transition_ack = _matches(FAILED_TRANSITION_PATTERNS, lowered)
            loop_language = _matches(LOOP_PATTERNS, lowered)
            progress_claim = _matches(PROGRESS_PATTERNS, lowered)
            completion_claim = _matches(COMPLETION_PATTERNS, lowered)
            repeated_action = same_action_recent_count >= 2
            near_duplicate_reasoning = max_recent_similarity >= 0.78 and len(normalize_text(thought).split()) >= 12
            static_repeated_action = repeated_action and metrics.is_static
            terminal_failure_claim = (
                result_status == "failure"
                and index == n_steps - 1
                and (completion_claim or action.get("action_type") in {"answer", "finished", "terminate"})
            )

            candidate_types: list[str] = []
            if explicit_self_correction:
                candidate_types.append("SELF_CORRECTION_EVIDENCE")
            if failed_transition_ack:
                candidate_types.append("FAILED_TRANSITION_ACK")
            if loop_language:
                candidate_types.append("LOOP_LANGUAGE")
            if repeated_action:
                candidate_types.append("REPEATED_ACTION")
            if near_duplicate_reasoning:
                candidate_types.append("NEAR_DUPLICATE_REASONING")
            if static_repeated_action:
                candidate_types.append("STATIC_REPEATED_ACTION")
            if terminal_failure_claim:
                candidate_types.append("UNSUCCESSFUL_TERMINAL_COMPLETION")

            for signal in candidate_types:
                task_signal_counts[signal] += 1

            record = {
                "task": task_dir.name,
                "step": step,
                "n_steps": n_steps,
                "score": "" if score is None else score,
                "result_status": result_status,
                "task_goal": task_goal,
                "action_type": action.get("action_type", ""),
                "action_key": key,
                "action_json": json.dumps(action, ensure_ascii=False, sort_keys=True),
                "thought_excerpt": thought[:700],
                "explicit_self_correction": int(explicit_self_correction),
                "failed_transition_ack": int(failed_transition_ack),
                "loop_language": int(loop_language),
                "progress_claim": int(progress_claim),
                "completion_claim": int(completion_claim),
                "same_action_recent_count": same_action_recent_count,
                "previous_text_jaccard": round(previous_similarity, 6),
                "max_recent_text_jaccard": round(max_recent_similarity, 6),
                "visual_mad_to_next": "" if metrics.mad is None else round(metrics.mad, 8),
                "visual_change_fraction_to_next": ""
                if metrics.changed_fraction is None
                else round(metrics.changed_fraction, 8),
                "dhash_distance_to_next": "" if metrics.dhash_distance is None else metrics.dhash_distance,
                "visually_static_to_next": int(metrics.is_static),
                "candidate_types": ";".join(candidate_types),
                "screenshot_before": str(before_path) if before_path.exists() else "",
                "screenshot_after": str(after_path) if after_path.exists() else "",
                "traj_path": str(task_dir / "traj.json"),
                "result_path": str(task_dir / "result.txt") if (task_dir / "result.txt").exists() else "",
            }
            step_records.append(record)
            all_steps.append(record)

            if candidate_types:
                candidate_rows.append(
                    {
                        **record,
                        "candidate_target_step": max(1, step - 1)
                        if (explicit_self_correction or failed_transition_ack)
                        else step,
                        "evidence_step": step,
                        "review_status": "UNREVIEWED",
                        "review_label": "",
                        "review_notes": "",
                    }
                )

            action_history.append(key)
            text_history.append(thought)

        # A self-correction at t is evidence about an earlier record.  Add a
        # conservative linked candidate for t-1 without calling it confirmed.
        for record in step_records:
            if record["explicit_self_correction"] or record["failed_transition_ack"]:
                target_step = int(record["step"]) - 1
                if target_step < 1:
                    continue
                target = next((row for row in step_records if int(row["step"]) == target_step), None)
                if target is None:
                    continue
                linked_type = (
                    "LATER_SELF_CORRECTION"
                    if record["explicit_self_correction"]
                    else "LATER_FAILURE_ACK"
                )
                candidate_rows.append(
                    {
                        **target,
                        "candidate_types": linked_type,
                        "candidate_target_step": target_step,
                        "evidence_step": record["step"],
                        "review_status": "UNREVIEWED",
                        "review_label": "",
                        "review_notes": f"Evidence excerpt: {record['thought_excerpt'][:350]}",
                    }
                )
                task_signal_counts[linked_type] += 1

        task_rows.append(
            {
                "task": task_dir.name,
                "n_steps": n_steps,
                "score": "" if score is None else score,
                "result_status": result_status,
                "task_goal": task_goal,
                "candidate_rows": sum(1 for row in candidate_rows if row["task"] == task_dir.name),
                **{signal: task_signal_counts.get(signal, 0) for signal in sorted(task_signal_counts)},
                "traj_path": str(task_dir / "traj.json"),
                "result_path": str(task_dir / "result.txt") if (task_dir / "result.txt").exists() else "",
            }
        )

    step_fields = [
        "task",
        "step",
        "n_steps",
        "score",
        "result_status",
        "task_goal",
        "action_type",
        "action_key",
        "action_json",
        "thought_excerpt",
        "explicit_self_correction",
        "failed_transition_ack",
        "loop_language",
        "progress_claim",
        "completion_claim",
        "same_action_recent_count",
        "previous_text_jaccard",
        "max_recent_text_jaccard",
        "visual_mad_to_next",
        "visual_change_fraction_to_next",
        "dhash_distance_to_next",
        "visually_static_to_next",
        "candidate_types",
        "screenshot_before",
        "screenshot_after",
        "traj_path",
        "result_path",
    ]
    candidate_fields = step_fields + [
        "candidate_target_step",
        "evidence_step",
        "review_status",
        "review_label",
        "review_notes",
    ]
    task_signal_fields = sorted(
        {key for row in task_rows for key in row if key not in {"task", "n_steps", "score", "result_status", "task_goal", "candidate_rows", "traj_path", "result_path"}}
    )
    task_fields = [
        "task",
        "n_steps",
        "score",
        "result_status",
        "task_goal",
        "candidate_rows",
        *task_signal_fields,
        "traj_path",
        "result_path",
    ]

    write_csv(output / "steps.csv", all_steps, step_fields)
    write_csv(output / "candidates.csv", candidate_rows, candidate_fields)
    write_csv(output / "tasks.csv", task_rows, task_fields)

    candidate_type_counts = Counter()
    candidate_task_sets: dict[str, set[str]] = defaultdict(set)
    for row in candidate_rows:
        for candidate_type in str(row["candidate_types"]).split(";"):
            if not candidate_type:
                continue
            candidate_type_counts[candidate_type] += 1
            candidate_task_sets[candidate_type].add(str(row["task"]))

    completed_steps = [float(row["n_steps"]) for row in task_rows if row["n_steps"]]
    step_counts = [int(row["n_steps"]) for row in task_rows]
    eligible_immediate_presteps = sum(max(0, n - 1) for n in step_counts)
    all_history_exposure_pairs = sum(n * (n - 1) // 2 for n in step_counts)
    # Seed keeps all earlier assistant text but only the latest three image
    # observations.  At target step t, a source P_i with lag >= 4 has neither
    # S_i nor S_{i+1} left among those three images.
    evidence_evicted_exposure_pairs = sum(
        max(0, (n - 4) * (n - 3) // 2) for n in step_counts
    )
    decisions_with_evicted_history = sum(max(0, n - 4) for n in step_counts)
    summary = {
        "scope": {
            "input": str(root),
            "formal_task_directories": len(task_rows),
            "nonempty_trajectories": sum(n > 0 for n in step_counts),
            "excluded_backup_rule": "directory name contains _backup_",
            "other_run_groups_read": False,
            "verifier_or_reflection_data_read": False,
            "model_identity": "doubao-seed-2-0-pro-260215",
            "model_identity_source": (
                "QR-MW/report.md run configuration; traj.json does not itself store model_name"
            ),
        },
        "outcomes": dict(Counter(row["result_status"] for row in task_rows)),
        "total_steps": len(all_steps),
        "history_exposure_denominators": {
            "eligible_source_presteps_with_later_decision": eligible_immediate_presteps,
            "immediate_prestep_decision_pairs": eligible_immediate_presteps,
            "all_history_exposure_pairs": all_history_exposure_pairs,
            "lag_ge_4_evidence_evicted_exposure_pairs": evidence_evicted_exposure_pairs,
            "decision_turns_with_at_least_one_evidence_evicted_pre_step": (
                decisions_with_evicted_history
            ),
            "seed_history_policy": (
                "all prior assistant responses retained; latest 3 image observations retained"
            ),
        },
        "step_distribution": {
            "min": min(completed_steps) if completed_steps else None,
            "median": statistics.median(completed_steps) if completed_steps else None,
            "p75": percentile(completed_steps, 0.75),
            "p90": percentile(completed_steps, 0.90),
            "max": max(completed_steps) if completed_steps else None,
            "tasks_at_50_steps": sum(int(value == 50) for value in completed_steps),
        },
        "candidate_rows": len(candidate_rows),
        "tasks_with_any_candidate": len({row["task"] for row in candidate_rows}),
        "candidate_type_counts": dict(sorted(candidate_type_counts.items())),
        "candidate_task_counts": {
            key: len(value) for key, value in sorted(candidate_task_sets.items())
        },
        "thresholds": {
            "repeated_action": "same quantized action seen >=2 times in prior 8 steps",
            "near_duplicate_reasoning": "max token Jaccard >=0.78 over prior 5 steps",
            "visually_static": "MAD<=0.0035, changed_fraction<=0.012, dHash distance<=2",
        },
        "interpretation_warning": (
            "Candidate counts are retrieval statistics, not prevalence estimates. "
            "Human screenshot-grounded review is required before assigning supported, "
            "refuted, invalidated, off-track, or misleading labels."
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    summary = scan(args.input.resolve(), args.output.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
