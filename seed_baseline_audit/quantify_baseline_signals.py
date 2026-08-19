#!/usr/bin/env python3
"""Quantify conservative repetition/static/revision signals in seed_baseline.

This script is intentionally a candidate-retrieval tool, not a misleading-history
classifier.  It reads only direct, non-backup task directories under a directory
named ``seed_baseline``.  It never reads verifier, retry, pilot, smoke, or sibling
run directories.

The implementation uses only Python's standard library so it can be rerun without
installing project dependencies.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence


DEFAULT_BASELINE_ROOT = Path(
    "/Users/apigo/Desktop/Projects/QR-MW/traj_logs/seed_baseline"
)
DEFAULT_OUTPUT = Path(
    "/Users/apigo/Desktop/agent monitor/seed_baseline_audit/output/"
    "conservative_metrics.json"
)

SCRIPT_VERSION = "1.0.0"

# Thresholds are deliberately exposed in both code and JSON output.
NEAR_ACTION_DISTANCE_PX = 80.0
ACTION_SIGNATURE_BIN_PX = 80.0
ACTION_CYCLE_LENGTHS = (2, 3, 4)
REASONING_LOOKBACK_STEPS = 8
REASONING_MIN_CONTENT_TOKENS = 10
REASONING_COSINE_THRESHOLD = 0.82
REASONING_BIGRAM_JACCARD_THRESHOLD = 0.28
TOP_N = 15

# scroll/wait repeats are common task progress operations and are excluded from
# the conservative repeated-action metrics.  ask_user/wait are excluded from the
# actionable static-screen metric because an unchanged GUI is often expected.
ROUTINE_REPEAT_ACTION_TYPES = frozenset({"scroll", "wait"})
EXPECTED_STATIC_ACTION_TYPES = frozenset({"ask_user", "wait"})

CONTENT_STOPWORDS = frozenset(
    """
    the a an to of and or is are was were be been being i we you it this that in
    on for with as at by from then so now need want can could should would will
    just my our your their its do did does have has had not no yes but first next
    here there right okay got get let me
    """.split()
)

# High-precision English error acknowledgements.  Generic "wait" and "oh right"
# are intentionally absent; they are measured separately as a noisy diagnostic.
EXPLICIT_ERROR_REVISION_RE = re.compile(
    r"\b(?:"
    r"my mistake|"
    r"(?:i(?:'ve| have)?|we(?:'ve| have)?) "
    r"(?:made|make|keep making|am making|was making|have been making|had been making) "
    r"(?:the same |this |a )?mistake|"
    r"i (?:was|am) wrong|"
    r"i messed up|messed up|oops|accidentally|by mistake|"
    r"misclicked|misread|misinterpreted|misunderstood|mixed up|confused|"
    r"stuck in (?:a )?loop|going in circles|"
    r"wrong (?:place|page|screen|menu|app|account|post|item|button|channel|"
    r"tab|direction|way|thing|option|icon|spot|view|user|profile|file|field|"
    r"location|chat|contact|number)|"
    r"that's not right|that was wrong"
    r")\b",
    re.IGNORECASE,
)

# This broader cue is retained only as a noisy diagnostic.  It must not be used
# as the conservative revision signal or as a misleading label.
BROAD_REVISION_RE = re.compile(
    r"\b(?:wait\s*[,—-]\s*no|no\s*[,—-]\s*wait|actually|"
    r"i (?:just |now )?realize(?:d)?|oh right)\b",
    re.IGNORECASE,
)

THINK_RE = re.compile(r"<think>(.*?)</think>", re.IGNORECASE | re.DOTALL)
SCORE_RE = re.compile(r"score:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-root",
        type=Path,
        default=DEFAULT_BASELINE_ROOT,
        help="Path whose final component must be exactly 'seed_baseline'.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--assert-current-corpus",
        action="store_true",
        help="Assert the known corpus-integrity counts observed in this snapshot.",
    )
    return parser.parse_args()


def validate_baseline_root(root: Path) -> Path:
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Baseline root is not a directory: {root}")
    if root.name != "seed_baseline":
        raise ValueError(
            "Refusing to scan a non-baseline run: the root directory name must "
            f"be exactly 'seed_baseline', got {root.name!r}"
        )
    return root


def discover_task_dirs(root: Path) -> list[Path]:
    task_dirs: list[Path] = []
    for candidate in sorted(root.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        if "_backup_" in candidate.name:
            continue
        # Resolve and verify containment so a task path cannot escape the one run.
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root:
            raise ValueError(f"Task directory escapes seed_baseline: {candidate}")
        task_dirs.append(resolved)
    return task_dirs


def extract_reasoning(prediction: Any) -> str:
    if not isinstance(prediction, str):
        return ""
    match = THINK_RE.search(prediction)
    return match.group(1).strip() if match else ""


def content_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z0-9']+", text.lower())
        if len(token) > 1 and token not in CONTENT_STOPWORDS
    ]


def cosine_and_bigram_jaccard(left: str, right: str) -> tuple[float, float]:
    left_tokens = content_tokens(left)
    right_tokens = content_tokens(right)
    left_counts = collections.Counter(left_tokens)
    right_counts = collections.Counter(right_tokens)
    dot = sum(count * right_counts[token] for token, count in left_counts.items())
    denominator = math.sqrt(
        sum(count * count for count in left_counts.values())
        * sum(count * count for count in right_counts.values())
    )
    cosine = dot / denominator if denominator else 0.0

    left_bigrams = set(zip(left_tokens, left_tokens[1:]))
    right_bigrams = set(zip(right_tokens, right_tokens[1:]))
    union = left_bigrams | right_bigrams
    jaccard = len(left_bigrams & right_bigrams) / len(union) if union else 0.0
    return cosine, jaccard


def normalized_text(value: Any) -> str:
    return " ".join(str(value if value is not None else "").lower().split())


def euclidean(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.hypot(x1 - x2, y1 - y2)


def actions_near(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not left or not right or left.get("action_type") != right.get("action_type"):
        return False
    action_type = left.get("action_type")
    if action_type in {"click", "long_press", "double_tap"}:
        return (
            euclidean(left["x"], left["y"], right["x"], right["y"])
            <= NEAR_ACTION_DISTANCE_PX
        )
    if action_type == "drag":
        start_distance = euclidean(
            left["start_x"], left["start_y"], right["start_x"], right["start_y"]
        )
        end_distance = euclidean(
            left["end_x"], left["end_y"], right["end_x"], right["end_y"]
        )
        return max(start_distance, end_distance) <= NEAR_ACTION_DISTANCE_PX
    if action_type in {"input_text", "answer", "ask_user", "unknown"}:
        return normalized_text(left.get("text")) == normalized_text(right.get("text"))
    # navigate_home/navigate_back and other parameterless actions are equal-target
    # when their action_type is equal. scroll/wait are removed by the caller.
    return True


def quantized(value: Any) -> int:
    return round(float(value) / ACTION_SIGNATURE_BIN_PX)


def action_signature(action: dict[str, Any]) -> tuple[Any, ...]:
    action_type = action.get("action_type", "none") if action else "none"
    if action_type in {"click", "long_press", "double_tap"}:
        return (action_type, quantized(action["x"]), quantized(action["y"]))
    if action_type == "scroll":
        return (
            action_type,
            action.get("direction"),
            quantized(action["x"]),
            quantized(action["y"]),
        )
    if action_type == "drag":
        return (
            action_type,
            quantized(action["start_x"]),
            quantized(action["start_y"]),
            quantized(action["end_x"]),
            quantized(action["end_y"]),
        )
    if action_type in {"input_text", "answer", "ask_user", "unknown"}:
        return (action_type, normalized_text(action.get("text")))
    return (action_type,)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_result(result_path: Path) -> tuple[float | None, str | None]:
    if not result_path.exists():
        return None, None
    text = result_path.read_text(encoding="utf-8", errors="replace")
    match = SCORE_RE.search(text)
    return (float(match.group(1)) if match else None), text


def event_step_list(events: Sequence[dict[str, Any]], key: str = "step") -> list[int]:
    return [int(event[key]) for event in events if key in event]


def analyze_task(task_dir: Path) -> dict[str, Any]:
    task_name = task_dir.name
    traj_path = task_dir / "traj.json"
    if not traj_path.is_file():
        return {
            "task": task_name,
            "trajectory_status": "missing_traj_json",
            "steps": 0,
            "score": None,
            "signals": {},
        }

    payload = json.loads(traj_path.read_text(encoding="utf-8"))
    if not payload:
        return {
            "task": task_name,
            "trajectory_status": "empty",
            "steps": 0,
            "score": None,
            "signals": {},
        }
    if "0" not in payload or not isinstance(payload["0"].get("traj"), list):
        raise ValueError(f"Unexpected trajectory schema: {traj_path}")

    trajectory: list[dict[str, Any]] = payload["0"]["traj"]
    actions = [step.get("action") or {} for step in trajectory]
    reasoning = [extract_reasoning(step.get("prediction")) for step in trajectory]
    step_numbers = [int(step.get("step", index + 1)) for index, step in enumerate(trajectory)]

    score, _ = parse_result(task_dir / "result.txt")

    exact_action_events: list[dict[str, Any]] = []
    near_action_events: list[dict[str, Any]] = []
    for index in range(1, len(actions)):
        action_type = actions[index].get("action_type")
        if action_type in ROUTINE_REPEAT_ACTION_TYPES:
            continue
        if actions[index] == actions[index - 1]:
            exact_action_events.append(
                {"step": step_numbers[index], "action_type": action_type}
            )
        if actions_near(actions[index], actions[index - 1]):
            near_action_events.append(
                {"step": step_numbers[index], "action_type": action_type}
            )

    signatures = [action_signature(action) for action in actions]
    short_cycle_events: list[dict[str, Any]] = []
    strong_three_repeat_events: list[dict[str, Any]] = []
    for cycle_length in ACTION_CYCLE_LENGTHS:
        for start in range(0, len(signatures) - 2 * cycle_length + 1):
            block = signatures[start : start + cycle_length]
            second = signatures[start + cycle_length : start + 2 * cycle_length]
            if block != second:
                continue
            if len(set(block)) < 2:
                continue
            if not any(
                signature[0] not in ROUTINE_REPEAT_ACTION_TYPES for signature in block
            ):
                continue
            event = {
                "step": step_numbers[start],
                "cycle_length": cycle_length,
                "action_types": [str(signature[0]) for signature in block],
            }
            short_cycle_events.append(event)
            third_end = start + 3 * cycle_length
            if third_end <= len(signatures) and block == signatures[
                start + 2 * cycle_length : third_end
            ]:
                strong_three_repeat_events.append(dict(event))

    near_reasoning_events: list[dict[str, Any]] = []
    for current in range(len(reasoning)):
        current_tokens = content_tokens(reasoning[current])
        if len(current_tokens) < REASONING_MIN_CONTENT_TOKENS:
            continue
        for previous in range(max(0, current - REASONING_LOOKBACK_STEPS), current):
            if len(content_tokens(reasoning[previous])) < REASONING_MIN_CONTENT_TOKENS:
                continue
            cosine, bigram_jaccard = cosine_and_bigram_jaccard(
                reasoning[current], reasoning[previous]
            )
            if (
                cosine >= REASONING_COSINE_THRESHOLD
                and bigram_jaccard >= REASONING_BIGRAM_JACCARD_THRESHOLD
            ):
                near_reasoning_events.append(
                    {
                        "previous_step": step_numbers[previous],
                        "step": step_numbers[current],
                        "cosine": round(cosine, 6),
                        "bigram_jaccard": round(bigram_jaccard, 6),
                    }
                )

    explicit_revision_events = [
        {"step": step_numbers[index]}
        for index, text in enumerate(reasoning)
        if EXPLICIT_ERROR_REVISION_RE.search(text)
    ]
    broad_revision_events = [
        {"step": step_numbers[index]}
        for index, text in enumerate(reasoning)
        if BROAD_REVISION_RE.search(text)
    ]

    screenshot_hashes: list[str | None] = []
    missing_screenshots: list[str] = []
    screenshot_dir = task_dir / "screenshots"
    for step_number in step_numbers:
        screenshot = screenshot_dir / f"{task_name}-0-{step_number}.png"
        if screenshot.is_file():
            screenshot_hashes.append(sha256_file(screenshot))
        else:
            screenshot_hashes.append(None)
            missing_screenshots.append(str(screenshot))

    exact_static_events: list[dict[str, Any]] = []
    exact_static_nonexpected_events: list[dict[str, Any]] = []
    skipped_nonconsecutive_pairs: list[list[int]] = []
    for index in range(len(trajectory) - 1):
        if step_numbers[index + 1] != step_numbers[index] + 1:
            skipped_nonconsecutive_pairs.append(
                [step_numbers[index], step_numbers[index + 1]]
            )
            continue
        if (
            screenshot_hashes[index] is not None
            and screenshot_hashes[index] == screenshot_hashes[index + 1]
        ):
            action_type = actions[index].get("action_type")
            event = {"step": step_numbers[index], "action_type": action_type}
            exact_static_events.append(event)
            if action_type not in EXPECTED_STATIC_ACTION_TYPES:
                exact_static_nonexpected_events.append(dict(event))

    signals = {
        "exact_consecutive_nonroutine_action": exact_action_events,
        "near_consecutive_nonroutine_target": near_action_events,
        "short_action_cycle": short_cycle_events,
        "strong_three_repeat_action_cycle": strong_three_repeat_events,
        "near_duplicate_reasoning": near_reasoning_events,
        "explicit_error_revision": explicit_revision_events,
        "broad_revision_cue_noisy": broad_revision_events,
        "exact_static_screenshot_all": exact_static_events,
        "exact_static_screenshot_nonexpected": exact_static_nonexpected_events,
    }

    return {
        "task": task_name,
        "trajectory_status": "nonempty",
        "steps": len(trajectory),
        "score": score,
        "result_status": "scored" if score is not None else "missing_or_unparsed",
        "missing_screenshots": missing_screenshots,
        "skipped_nonconsecutive_screenshot_pairs": skipped_nonconsecutive_pairs,
        "signals": signals,
    }


PRIMARY_SIGNALS = (
    "exact_consecutive_nonroutine_action",
    "near_consecutive_nonroutine_target",
    "short_action_cycle",
    "strong_three_repeat_action_cycle",
    "near_duplicate_reasoning",
    "explicit_error_revision",
    "exact_static_screenshot_all",
    "exact_static_screenshot_nonexpected",
)


def signal_count(task: dict[str, Any], signal: str) -> int:
    return len(task.get("signals", {}).get(signal, []))


def summarize_group(tasks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    step_values = [int(task["steps"]) for task in tasks]
    total_steps = sum(step_values)
    signal_summary: dict[str, Any] = {}
    for signal in PRIMARY_SIGNALS:
        event_count = sum(signal_count(task, signal) for task in tasks)
        tasks_positive = sum(signal_count(task, signal) > 0 for task in tasks)
        signal_summary[signal] = {
            "tasks_positive": tasks_positive,
            "task_fraction": round(tasks_positive / len(tasks), 6) if tasks else None,
            "events": event_count,
            "events_per_100_steps": (
                round(100.0 * event_count / total_steps, 6) if total_steps else None
            ),
        }
    broad_events = sum(signal_count(task, "broad_revision_cue_noisy") for task in tasks)
    broad_positive = sum(
        signal_count(task, "broad_revision_cue_noisy") > 0 for task in tasks
    )
    return {
        "tasks": len(tasks),
        "steps": total_steps,
        "mean_steps": round(statistics.mean(step_values), 6) if step_values else None,
        "median_steps": statistics.median(step_values) if step_values else None,
        "signals": signal_summary,
        "noisy_diagnostic_broad_revision_cue": {
            "tasks_positive": broad_positive,
            "events": broad_events,
        },
    }


def lower_index_quantile(values: Sequence[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def step_bin(step_count: int) -> str:
    if step_count == 50:
        return "50"
    if step_count >= 31:
        return "31-49"
    if step_count >= 21:
        return "21-30"
    if step_count >= 11:
        return "11-20"
    return "<=10"


def top_tasks_for_signal(
    tasks: Sequence[dict[str, Any]], signal: str, limit: int = TOP_N
) -> list[dict[str, Any]]:
    positive = [task for task in tasks if signal_count(task, signal) > 0]
    positive.sort(key=lambda task: (-signal_count(task, signal), task["task"]))
    output: list[dict[str, Any]] = []
    for task in positive[:limit]:
        events = task["signals"][signal]
        item: dict[str, Any] = {
            "task": task["task"],
            "steps": task["steps"],
            "score": task["score"],
            "events": len(events),
            "event_steps": event_step_list(events),
        }
        if signal == "near_duplicate_reasoning":
            item["step_pairs"] = [
                [event["previous_step"], event["step"]] for event in events
            ]
        if signal in {"short_action_cycle", "strong_three_repeat_action_cycle"}:
            item["cycle_examples"] = events
        output.append(item)
    return output


def fifty_step_retrieval_ranking(
    tasks: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank 50-step trajectories by multiple conservative retrieval dimensions.

    The ranking is explicitly not a loop or misleading classification.  A task
    can hit 50 steps while still making progress.
    """

    ranked: list[dict[str, Any]] = []
    for task in tasks:
        if task["steps"] != 50:
            continue
        counts = {
            signal: signal_count(task, signal)
            for signal in (
                "strong_three_repeat_action_cycle",
                "short_action_cycle",
                "near_consecutive_nonroutine_target",
                "near_duplicate_reasoning",
                "explicit_error_revision",
                "exact_static_screenshot_nonexpected",
            )
        }
        dimensions: list[str] = []
        if counts["strong_three_repeat_action_cycle"] > 0:
            dimensions.append("three_repeat_action_cycle")
        if counts["short_action_cycle"] > 0:
            dimensions.append("short_action_cycle")
        if counts["near_consecutive_nonroutine_target"] >= 4:
            dimensions.append("near_target_repetition_ge_4")
        if counts["near_duplicate_reasoning"] >= 2:
            dimensions.append("near_reasoning_pairs_ge_2")
        if counts["explicit_error_revision"] >= 10:
            dimensions.append("explicit_error_steps_ge_10")
        if counts["exact_static_screenshot_nonexpected"] >= 2:
            dimensions.append("nonexpected_exact_static_ge_2")
        ranked.append(
            {
                "task": task["task"],
                "score": task["score"],
                "evidence_dimension_count": len(dimensions),
                "evidence_dimensions": dimensions,
                "signal_counts": counts,
            }
        )

    ranked.sort(
        key=lambda item: (
            -int(item["signal_counts"]["strong_three_repeat_action_cycle"] > 0),
            -item["evidence_dimension_count"],
            -item["signal_counts"]["short_action_cycle"],
            -item["signal_counts"]["explicit_error_revision"],
            -item["signal_counts"]["near_duplicate_reasoning"],
            item["task"],
        )
    )
    return ranked


def compact_task_metrics(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": task["task"],
        "trajectory_status": task["trajectory_status"],
        "steps": task["steps"],
        "score": task.get("score"),
        "signal_counts": {
            signal: signal_count(task, signal)
            for signal in (*PRIMARY_SIGNALS, "broad_revision_cue_noisy")
        },
    }


def build_report(root: Path, tasks: Sequence[dict[str, Any]]) -> dict[str, Any]:
    nonempty = [task for task in tasks if task["trajectory_status"] == "nonempty"]
    empty = [task for task in tasks if task["trajectory_status"] != "nonempty"]
    scored = [task for task in nonempty if task.get("score") is not None]
    success = [task for task in scored if task["score"] == 1.0]
    failure = [task for task in scored if task["score"] == 0.0]
    other_scores = [task for task in scored if task["score"] not in {0.0, 1.0}]
    unscored = [task for task in nonempty if task.get("score") is None]
    step_values = [task["steps"] for task in nonempty]
    bins = collections.Counter(step_bin(step_count) for step_count in step_values)

    missing_screenshot_count = sum(
        len(task.get("missing_screenshots", [])) for task in nonempty
    )
    skipped_pair_count = sum(
        len(task.get("skipped_nonconsecutive_screenshot_pairs", []))
        for task in nonempty
    )

    report = {
        "schema_version": "1.0",
        "script_version": SCRIPT_VERSION,
        "source_scope": {
            "baseline_root": str(root),
            "scope_rule": (
                "Only direct child task directories of seed_baseline are read; "
                "directories containing '_backup_' and symlinks are excluded."
            ),
            "other_runs_read": False,
            "files_read_per_task": ["traj.json", "result.txt", "screenshots/*.png"],
            "thread_logs_read": False,
        },
        "interpretation_boundary": {
            "output_type": "conservative_candidate_retrieval_metrics",
            "is_misleading_label": False,
            "warning": (
                "Repetition, an unchanged screenshot, explicit self-correction, "
                "failure, and reaching 50 steps are not individually or jointly "
                "proof that a prior step misled the agent. Misleading requires "
                "manual evidence alignment and downstream-use verification."
            ),
        },
        "thresholds": {
            "near_action_distance_px": NEAR_ACTION_DISTANCE_PX,
            "action_signature_bin_px": ACTION_SIGNATURE_BIN_PX,
            "action_cycle_lengths": list(ACTION_CYCLE_LENGTHS),
            "action_cycle_min_distinct_signatures": 2,
            "routine_repeat_action_types_excluded": sorted(
                ROUTINE_REPEAT_ACTION_TYPES
            ),
            "expected_static_action_types_excluded_from_actionable_static": sorted(
                EXPECTED_STATIC_ACTION_TYPES
            ),
            "reasoning_lookback_steps": REASONING_LOOKBACK_STEPS,
            "reasoning_min_content_tokens": REASONING_MIN_CONTENT_TOKENS,
            "reasoning_unigram_cosine_min": REASONING_COSINE_THRESHOLD,
            "reasoning_bigram_jaccard_min": REASONING_BIGRAM_JACCARD_THRESHOLD,
            "static_definition": "byte-identical consecutive PNGs by SHA-256",
            "explicit_revision_definition": EXPLICIT_ERROR_REVISION_RE.pattern,
            "broad_revision_diagnostic_definition": BROAD_REVISION_RE.pattern,
        },
        "denominators": {
            "nominal_nonbackup_task_directories": len(tasks),
            "nonempty_trajectories": len(nonempty),
            "empty_or_missing_trajectories": len(empty),
            "scored_trajectories": len(scored),
            "unscored_nonempty_trajectories": len(unscored),
            "successes": len(success),
            "failures": len(failure),
            "other_numeric_scores": len(other_scores),
            "total_steps_nonempty": sum(step_values),
            "total_steps_scored": sum(task["steps"] for task in scored),
            "screenshots_expected": sum(step_values),
            "screenshots_missing": missing_screenshot_count,
            "nonconsecutive_screenshot_pairs_skipped": skipped_pair_count,
            "empty_or_missing_tasks": [task["task"] for task in empty],
            "unscored_nonempty_tasks": [task["task"] for task in unscored],
        },
        "step_distribution_nonempty": {
            "min": min(step_values) if step_values else None,
            "median": statistics.median(step_values) if step_values else None,
            "mean": round(statistics.mean(step_values), 6) if step_values else None,
            "p75_lower_index": lower_index_quantile(step_values, 0.75),
            "p90_lower_index": lower_index_quantile(step_values, 0.90),
            "max": max(step_values) if step_values else None,
            "bins": {
                label: bins.get(label, 0)
                for label in ("<=10", "11-20", "21-30", "31-49", "50")
            },
        },
        "stratified_scored_results": {
            "success": summarize_group(success),
            "failure": summarize_group(failure),
            "all_scored": summarize_group(scored),
        },
        "all_nonempty_summary": summarize_group(nonempty),
        "top_tasks_by_signal": {
            signal: top_tasks_for_signal(nonempty, signal) for signal in PRIMARY_SIGNALS
        },
        "fifty_step_retrieval_candidates": {
            "count": sum(task["steps"] == 50 for task in nonempty),
            "ranking_is_loop_or_misleading_label": False,
            "ranking_basis": (
                "Lexicographic retrieval ordering: presence of a three-repeat "
                "action cycle, number of conservative evidence dimensions, short "
                "cycle count, explicit-error count, reasoning-pair count, task name."
            ),
            "tasks": fifty_step_retrieval_ranking(nonempty),
        },
        "task_metrics": [compact_task_metrics(task) for task in tasks],
    }
    return report


def validate_report(report: dict[str, Any], assert_current_corpus: bool) -> None:
    denominator = report["denominators"]
    if denominator["nominal_nonbackup_task_directories"] != (
        denominator["nonempty_trajectories"]
        + denominator["empty_or_missing_trajectories"]
    ):
        raise AssertionError("Task denominator decomposition failed")
    if denominator["nonempty_trajectories"] != (
        denominator["scored_trajectories"]
        + denominator["unscored_nonempty_trajectories"]
    ):
        raise AssertionError("Nonempty/scored denominator decomposition failed")
    if denominator["scored_trajectories"] != (
        denominator["successes"]
        + denominator["failures"]
        + denominator["other_numeric_scores"]
    ):
        raise AssertionError("Score denominator decomposition failed")
    if denominator["screenshots_missing"] != 0:
        raise AssertionError(
            f"Expected complete screenshots, found {denominator['screenshots_missing']} missing"
        )
    if report["source_scope"]["other_runs_read"] is not False:
        raise AssertionError("Source-scope guard failed")

    if assert_current_corpus:
        expected = {
            "nominal_nonbackup_task_directories": 117,
            "nonempty_trajectories": 116,
            "empty_or_missing_trajectories": 1,
            "scored_trajectories": 115,
            "unscored_nonempty_trajectories": 1,
            "successes": 46,
            "failures": 69,
            "other_numeric_scores": 0,
            "total_steps_nonempty": 3397,
            "total_steps_scored": 3391,
        }
        mismatches = {
            key: {"expected": value, "actual": denominator.get(key)}
            for key, value in expected.items()
            if denominator.get(key) != value
        }
        if mismatches:
            raise AssertionError(f"Current corpus assertions failed: {mismatches}")
        if report["step_distribution_nonempty"]["bins"]["50"] != 35:
            raise AssertionError("Expected 35 exactly-50-step trajectories")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    root = validate_baseline_root(args.baseline_root)
    task_dirs = discover_task_dirs(root)
    tasks = [analyze_task(task_dir) for task_dir in task_dirs]
    report = build_report(root, tasks)
    validate_report(report, args.assert_current_corpus)
    write_json_atomic(args.output, report)

    denominator = report["denominators"]
    print(
        "Wrote conservative baseline metrics to "
        f"{args.output.expanduser().resolve()}\n"
        f"tasks={denominator['nominal_nonbackup_task_directories']} "
        f"nonempty={denominator['nonempty_trajectories']} "
        f"scored={denominator['scored_trajectories']} "
        f"success={denominator['successes']} failure={denominator['failures']} "
        f"steps={denominator['total_steps_nonempty']}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
