"""Read-only actor usage accounting from Collector events; no runtime dependencies.

Count application-visible SDK requests, not logical decisions or SDK-internal
network retries. Never sum task_ended.token_usage: it can be cumulative across
whole-task attempts. Only numeric usage and allowlisted metadata leave this reader.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_METRICS = ("input", "output", "cached")
_MISSING_NAMES = ("input", "output", "cache")
_CLI_SETTINGS = (
    "max_round",
    "auto_retry",
    "pass_k",
    "enable_user_interaction",
    "enable_mcp",
    "executor_model_name",
    "executor_agent_class",
    "scale_factor",
    "step_wait_time",
    "timeout",
)
_REQUEST_SETTINGS = ("model", "temperature", "top_p", "max_tokens", "max_completion_tokens", "seed")
_TREATMENTS = ("gui_ledger_mode", "gui_ledger_ui_tree", "gui_ledger_extra_model_calls")


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nonempty_name(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _scalar_metadata(source: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {
        key: source[key]
        for key in keys
        if key in source and (source[key] is None or type(source[key]) in (str, int, float, bool))
    }


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise ValueError("Cannot read a required Collector JSON object") from None


def _token(value: Any, issues: set[str]) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        issues.add("invalid_usage_value")
        return None
    return value


def _usage(value: Any, *, normalized: bool, issues: set[str]) -> tuple[int | None, ...] | None:
    data = _mapping(value)
    if not data:
        return None
    provider = _mapping(data.get("provider_usage")) if normalized else data
    prompt = _token(data.get("prompt_tokens"), issues)
    output = _token(data.get("completion_tokens"), issues)
    if normalized and provider:
        for name, current in (("prompt_tokens", prompt), ("completion_tokens", output)):
            raw = _token(provider.get(name), issues)
            if raw is not None and current is not None and raw != current:
                issues.add("conflicting_usage")
            if name == "prompt_tokens" and current is None:
                prompt = raw
            elif name == "completion_tokens" and current is None:
                output = raw
    # normalized.cached_tokens defaults to zero even if the provider omitted it.
    cache = _token(_mapping(provider.get("prompt_tokens_details")).get("cached_tokens"), issues)
    if cache is not None and prompt is not None and cache > prompt:
        issues.add("invalid_cached_subset")
        cache = None
    total = _token(provider.get("total_tokens"), issues)
    if total is not None and prompt is not None and output is not None and total != prompt + output:
        issues.add("conflicting_usage")
    return prompt, output, cache


def _terminal_usage(payload: dict[str, Any]) -> tuple[tuple[int | None, ...], set[str]]:
    issues: set[str] = set()
    candidates = []
    for key in ("normalized_response", "normalized_partial_response", "raw_response_view"):
        candidate = _usage(
            _mapping(payload.get(key)).get("usage"),
            normalized=key != "raw_response_view",
            issues=issues,
        )
        if candidate is not None:
            candidates.append(candidate)
    result: list[int | None] = []
    for i in range(3):
        values = {candidate[i] for candidate in candidates if candidate[i] is not None}
        if len(values) > 1:
            issues.add("conflicting_usage")
        result.append(next(iter(values)) if len(values) == 1 else None)
    if "conflicting_usage" in issues:
        return (None, None, None), issues
    return tuple(result), issues


@dataclass
class _Call:
    requests: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    correlations: set[str] = field(default_factory=set)
    terminals: set[tuple[str, tuple[int | None, ...]]] = field(default_factory=set)
    issues: set[str] = field(default_factory=set)


def _empty_counts() -> dict[str, Any]:
    result = {
        "model_calls": 0,
        "gui_action_attempts": 0,
        "action_attempts": 0,
        "non_gui_action_attempts": 0,
        "excluded_nonactor_calls": 0,
        "unknown_role_calls": 0,
        "failed_model_calls": 0,
    }
    for metric, missing in zip(_METRICS, _MISSING_NAMES, strict=True):
        result[f"known_{metric}_tokens"] = 0
        result[f"calls_missing_{missing}"] = 0
    return result


def _finish_counts(result: dict[str, Any]) -> None:
    for metric, missing in zip(_METRICS, _MISSING_NAMES, strict=True):
        result[f"{metric}_tokens"] = (
            None if result[f"calls_missing_{missing}"] else result[f"known_{metric}_tokens"]
        )


def _read_attempt(path: Path, run_id: str) -> dict[str, Any]:
    issues: set[str] = set()
    calls: dict[str, _Call] = defaultdict(_Call)
    starts: set[str] = set()
    ends: set[str] = set()
    actions: dict[str, str] = {}
    request_configs: set[str] = set()
    try:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                        raise ValueError
                except ValueError:
                    issues.add("invalid_event_json")
                    continue
                if event.get("run_id") != run_id or event.get("task_run_id") != path.parent.name:
                    issues.add("event_identity_mismatch")
                kind, payload = event.get("event_type"), event["payload"]
                if kind == "task_started":
                    starts.add(
                        _json_key(
                            _scalar_metadata(
                                payload,
                                (
                                    "task_name",
                                    "whole_task_attempt_index",
                                    "suite_family",
                                ),
                            )
                        )
                    )
                elif kind == "task_ended":
                    ends.add(
                        _json_key(
                            {
                                "score": _mapping(payload.get("environment_evaluation")).get(
                                    "score"
                                ),
                                "capture_complete": payload.get("capture_complete"),
                            }
                        )
                    )
                elif kind == "action_execution_started":
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or not event_id:
                        issues.add("missing_action_event_id")
                        event_id = f"line:{line_no}"
                    execution_kind = payload.get("execution_kind")
                    if execution_kind not in ("gui", "mcp", "ask_user", "answer"):
                        issues.add("unknown_action_kind")
                        execution_kind = "unknown"
                    if event_id in actions and actions[event_id] != execution_kind:
                        issues.add("conflicting_action_kind")
                    actions[event_id] = execution_kind
                elif kind in ("model_request", "model_response", "model_attempt_failed"):
                    request_id = payload.get("request_id")
                    if not isinstance(request_id, str) or not request_id:
                        issues.add("missing_request_id")
                        continue
                    call = calls[request_id]
                    call.correlations.add(
                        _json_key(
                            _scalar_metadata(
                                payload,
                                (
                                    "model_call_id",
                                    "retry_group_id",
                                    "adapter_attempt_index",
                                    "attempt_index",
                                    "step_id",
                                ),
                            )
                        )
                    )
                    if kind == "model_request":
                        role = payload.get("call_role")
                        call.roles.add(role if isinstance(role, str) and role else "unknown")
                        config = _json_key(
                            _scalar_metadata(
                                _mapping(payload.get("request_view")),
                                _REQUEST_SETTINGS,
                            )
                        )
                        call.requests.add(config)
                        if role == "actor":
                            request_configs.add(config)
                    else:
                        usage, errors = _terminal_usage(payload)
                        call.issues.update(errors)
                        call.terminals.add((kind, usage))
                elif kind == "collector_error":
                    issues.add("collector_error")
                # Stream chunks are cumulative; only terminal usage is counted.
    except OSError:
        issues.add("unreadable_task_stream")

    start = json.loads(next(iter(starts))) if len(starts) == 1 else {}
    end = json.loads(next(iter(ends))) if len(ends) == 1 else {}
    if len(starts) != 1:
        issues.add("missing_or_conflicting_task_start")
    if len(ends) != 1 or end.get("capture_complete") is not True:
        issues.add("incomplete_task_capture")
    name, index = start.get("task_name"), start.get("whole_task_attempt_index")
    if not isinstance(name, str) or not name:
        name = f"unidentified:{run_id}:{path.parent.name}"
        issues.add("missing_task_name")
    if type(index) is not int or index < 1:
        index = None
        issues.add("invalid_task_attempt_index")
    score = end.get("score")
    if (
        not isinstance(score, (int, float))
        or isinstance(score, bool)
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        score = None

    result = _empty_counts()
    result.update(
        task_name=name,
        run_id=run_id,
        task_run_id=path.parent.name,
        attempt_index=index,
        score=score,
        request_configurations=sorted(request_configs),
    )
    result["action_attempts"] = len(actions)
    result["gui_action_attempts"] = sum(kind == "gui" for kind in actions.values())
    result["non_gui_action_attempts"] = len(actions) - result["gui_action_attempts"]
    for call in calls.values():
        if not call.requests:
            issues.add("orphan_model_terminal")
            result["unknown_role_calls"] += 1
            continue
        if len(call.roles) != 1 or "unknown" in call.roles:
            issues.add("unknown_call_role")
            result["unknown_role_calls"] += 1
            continue
        if call.roles != {"actor"}:
            result["excluded_nonactor_calls"] += 1
            continue
        result["model_calls"] += 1
        issues.update(call.issues)
        usage = (None, None, None)
        if len(call.correlations) != 1:
            issues.add("conflicting_call_correlation")
        elif len(call.requests) != 1:
            issues.add("conflicting_model_request")
        elif len(call.terminals) > 1:
            issues.add("conflicting_model_terminal")
        elif len(call.terminals) == 1:
            terminal, usage = next(iter(call.terminals))
            result["failed_model_calls"] += terminal == "model_attempt_failed"
        else:
            issues.add("missing_model_terminal")
        for metric, missing, value in zip(_METRICS, _MISSING_NAMES, usage, strict=True):
            if value is None:
                result[f"calls_missing_{missing}"] += 1
            else:
                result[f"known_{metric}_tokens"] += value
    _finish_counts(result)
    result["issues"] = sorted(issues)
    return result


def _aggregate(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    result = _empty_counts()
    for row in rows:
        for key in result:
            result[key] += row[key]
    _finish_counts(result)
    return result


def _summarize_arm(roots: Sequence[Path], seen_runs: set[str]) -> dict[str, Any]:
    issues: set[str] = set()
    attempts: list[dict[str, Any]] = []
    configurations: set[str] = set()
    treatments: set[str] = set()
    run_ids: list[str] = []
    for root in roots:
        manifest = _read_object(root / "manifest.start.json")
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in seen_runs:
            raise ValueError("Each input must have a unique, nonempty run_id")
        seen_runs.add(run_id)
        run_ids.append(run_id)
        config = _scalar_metadata(manifest, ("agent_type", "model_name", "suite_family"))
        cli = _mapping(manifest.get("resolved_cli_config"))
        config.update(_scalar_metadata(cli, _CLI_SETTINGS))
        config["sampling"] = _scalar_metadata(
            _mapping(manifest.get("resolved_agent_runtime_config")),
            _REQUEST_SETTINGS[1:],
        )
        configurations.add(_json_key(config))
        treatments.add(_json_key(_scalar_metadata(cli, _TREATMENTS)))
        paths = sorted((root / "tasks").glob("*/events.jsonl"))
        if not paths:
            issues.add("no_task_streams")
        final_path = root / "manifest.final.json"
        if not final_path.is_file():
            issues.add("missing_final_manifest")
        else:
            final = _read_object(final_path)
            if final.get("run_id") != run_id:
                issues.add("final_manifest_identity_mismatch")
            if final.get("capture_complete") is not True:
                issues.add("incomplete_run_capture")
            if final.get("runtime_status") != "completed":
                issues.add("unfinished_run")
            summaries = final.get("task_streams")
            if isinstance(summaries, list):
                declared = [
                    item.get("task_run_id")
                    for item in summaries
                    if isinstance(item, dict) and _nonempty_name(item.get("task_run_id"))
                ]
                if (
                    len(declared) != len(summaries)
                    or len(set(declared)) != len(declared)
                    or set(declared) != {path.parent.name for path in paths}
                ):
                    issues.add("task_stream_inventory_mismatch")
                if any(_mapping(item).get("capture_complete") is not True for item in summaries):
                    issues.add("incomplete_task_stream_inventory")
            else:
                issues.add("missing_task_stream_inventory")
        attempts.extend(_read_attempt(path, run_id) for path in paths)

    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        by_task[attempt["task_name"]].append(attempt)
        issues.update(attempt["issues"])
    tasks = []
    for name, task_attempts in sorted(by_task.items()):
        indices = [attempt["attempt_index"] for attempt in task_attempts]
        ambiguous = (
            None in indices
            or len(set(indices)) != len(indices)
            or len({attempt["run_id"] for attempt in task_attempts}) > 1
        )
        if ambiguous:
            issues.add("ambiguous_task_attempts")
        latest = None if ambiguous else max(task_attempts, key=lambda row: row["attempt_index"])
        row = _aggregate(task_attempts)
        row.update(
            task_name=name, attempts=len(task_attempts), score=latest["score"] if latest else None
        )
        tasks.append(row)
    result = _aggregate(attempts)
    result.update(
        run_ids=run_ids,
        task_count=len(tasks),
        task_attempts=len(attempts),
        scored_tasks=sum(row["score"] is not None for row in tasks),
        successes=sum(row["score"] is not None and row["score"] > 0.99 for row in tasks),
        tasks=tasks,
        configurations=[json.loads(value) for value in sorted(configurations)],
        treatments=[json.loads(value) for value in sorted(treatments)],
        request_configurations=[
            json.loads(value)
            for value in sorted(
                {value for attempt in attempts for value in attempt["request_configurations"]}
            )
        ],
    )
    if result["calls_missing_input"]:
        issues.add("missing_input_usage")
    if result["calls_missing_output"]:
        issues.add("missing_output_usage")
    if result["scored_tasks"] != result["task_count"]:
        issues.add("unscored_tasks")
    result["issues"] = sorted(issues)
    return result


def build_report(
    runs: Mapping[str, Sequence[Path]],
    *,
    baseline: str = "OFF",
    expected_tasks: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarize exact Collector run roots; repeated labels may contain disjoint shards.

    All attempts contribute usage. Only a unique latest whole-task attempt
    contributes its score. Ambiguous cross-run repetitions are not best-of-k.
    """
    if not runs or any(not _nonempty_name(label) or not roots for label, roots in runs.items()):
        raise ValueError("Provide at least one labeled run root")
    if expected_tasks is not None and (
        not expected_tasks
        or any(not _nonempty_name(name) for name in expected_tasks)
        or len(set(expected_tasks)) != len(expected_tasks)
    ):
        raise ValueError("Expected task names must be nonempty and unique")
    seen_runs: set[str] = set()
    arms = {
        label: _summarize_arm([Path(root) for root in roots], seen_runs)
        for label, roots in runs.items()
    }
    cohort = (
        set(expected_tasks)
        if expected_tasks is not None
        else {row["task_name"] for arm in arms.values() for row in arm["tasks"]}
    )
    for arm in arms.values():
        actual = {row["task_name"] for row in arm["tasks"]}
        arm["missing_tasks"], arm["extra_tasks"] = sorted(cohort - actual), sorted(actual - cohort)
        if actual != cohort:
            arm["issues"] = sorted(set(arm["issues"]) | {"cohort_mismatch"})
        scores_complete = bool(cohort) and actual == cohort and arm["scored_tasks"] == len(cohort)
        arm["success_rate"] = arm["successes"] / len(cohort) if scores_complete else None
        arm["complete"] = not arm["issues"]
        arm["input_tokens_per_success"] = (
            arm["input_tokens"] / arm["successes"] if arm["complete"] and arm["successes"] else None
        )
    reference = arms.get(baseline)
    for label, arm in arms.items():
        reasons: set[str] = set()
        if reference is None:
            reasons.add("baseline_not_provided")
        else:
            if not reference["complete"] or not arm["complete"]:
                reasons.add("incomplete_accounting_or_scores")
            if not reference["input_tokens"]:
                reasons.add("baseline_input_unavailable_or_zero")
            for field_name in ("configurations", "request_configurations"):
                if arm[field_name] != reference[field_name]:
                    reasons.add(f"mismatched_{field_name}")
            for candidate in (reference, arm):
                configs = candidate["configurations"]
                if len(configs) != 1 or any(
                    configs[0].get(key) is None
                    for key in ("agent_type", "model_name", "suite_family", "max_round")
                ):
                    reasons.add("unverified_configuration")
                if len(candidate["request_configurations"]) != 1 or not all(
                    config.get("model") for config in candidate["request_configurations"]
                ):
                    reasons.add("unverified_request_configuration")
        arm["comparison_issues"] = sorted(reasons)
        arm["input_reduction_vs_baseline"] = (
            1 - arm["input_tokens"] / reference["input_tokens"]
            if reference is not None and not reasons
            else None
        )
    return {
        "schema_version": "mobileworld.gui_ledger_usage/v1",
        "scope": "actor_application_visible_sdk_calls",
        "baseline": baseline,
        "expected_tasks": sorted(cohort),
        "cohort_source": "provided" if expected_tasks is not None else "observed_union",
        "arms": arms,
    }


def _cell(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return "".join("\\" + char if char in "\\`*{}[]()#+-.!|<>_" else char for char in text)


def _csv_cell(value: Any) -> Any:
    # csv quoting alone does not prevent spreadsheet formula execution.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_markdown(report: dict[str, Any]) -> str:
    """Render totals alongside SR and missing-usage coverage, never a success-only mean."""
    lines = [
        "# GUI Ledger actor token report",
        "",
        f"Cohort: {len(report['expected_tasks'])}; source: {report['cohort_source']}. "
        "Success = latest unambiguous attempt score > 0.99.",
        "",
        "| Arm | Successes | SR | Input tokens | Output tokens | Calls | GUI actions | Input reduction |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, arm in report["arms"].items():
        tokens = [
            f"{arm[key]:,}" if arm[key] is not None else f"NA (known {arm['known_' + key]:,})"
            for key in ("input_tokens", "output_tokens")
        ]
        sr = f"{arm['success_rate']:.2%}" if arm["success_rate"] is not None else "NA"
        reduction = arm["input_reduction_vs_baseline"]
        delta = f"{reduction:.2%}" if reduction is not None else "NA"
        lines.append(
            f"| {_cell(label)} | {arm['successes']} | {sr} | {tokens[0]} | {tokens[1]} | "
            f"{arm['model_calls']:,} | {arm['gui_action_attempts']:,} | {delta} |"
        )
    for label, arm in report["arms"].items():
        lines.extend(
            [
                "",
                f"## {_cell(label)}",
                "",
                f"Tasks/attempts: {arm['task_count']}/{arm['task_attempts']}; "
                f"scored tasks: {arm['scored_tasks']}; complete: {arm['complete']}.",
                "",
                "Missing usage calls (input/output/cache): "
                f"{arm['calls_missing_input']}/{arm['calls_missing_output']}/"
                f"{arm['calls_missing_cache']}. "
                f"Known cached input: {arm['known_cached_tokens']:,} (already within input).",
                "",
                f"Input tokens per success (all attempts): {arm['input_tokens_per_success']}.",
                "",
                f"Accounting issues: {', '.join(arm['issues']) or 'none'}.",
                "",
                f"Comparison issues: {', '.join(arm['comparison_issues']) or 'none'}.",
                "",
                f"Treatments: {_cell(_json_key(arm['treatments']))}.",
            ]
        )
    lines.extend(
        [
            "",
            "## Scope and limitations",
            "",
            "All observed actor attempts, including failures and retries, are counted. "
            "No prompts, screenshots or blobs are exported. Task-summary cumulative counters "
            "and intermediate stream chunks are not summed. Missing usage is not zero.",
            "",
            "Cached tokens are a subset of input, not an extra chargeable token total. "
            "Injected hints are already included in provider-reported input. Image accounting "
            "follows the serving model's usage convention; no text/image split is inferred.",
            "",
            "Simulator calls and SDK-internal retries are not fully observable here. "
            "This is not whole-system usage, an API bill, or a causal efficiency claim. "
            "Capture completeness uses Collector flags and stream inventory, not a new "
            "cryptographic integrity audit. Omitted/unrecorded configuration cannot be verified.",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="LABEL=ROOT")
    parser.add_argument("--baseline", default="OFF")
    parser.add_argument("--expected-tasks", type=Path, help="One nonempty task name per line")
    parser.add_argument(
        "--output-dir", type=Path, help="New directory outside all source run roots"
    )
    args = parser.parse_args(argv)
    runs: dict[str, list[Path]] = defaultdict(list)
    try:
        for specification in args.run:
            label, separator, root = specification.partition("=")
            if not separator or not label or not root:
                raise ValueError("--run requires LABEL=ROOT")
            runs[label].append(Path(root).resolve())
        expected = None
        if args.expected_tasks is not None:
            expected = [
                line.strip()
                for line in args.expected_tasks.read_text().splitlines()
                if line.strip()
            ]
        destination = args.output_dir.resolve() if args.output_dir is not None else None
        if destination is not None:
            if destination.exists():
                raise ValueError("Output directory already exists; choose a new report directory")
            if any(destination.is_relative_to(root) for roots in runs.values() for root in roots):
                raise ValueError("Output must not be inside a source Collector run")
        report = build_report(runs, baseline=args.baseline, expected_tasks=expected)
        markdown = render_markdown(report)
        if destination is None:
            print(markdown, end="")
        else:
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            (destination / "report.md").write_text(markdown, encoding="utf-8")
            rows = [
                {"arm": label, **row}
                for label, arm in report["arms"].items()
                for row in arm["tasks"]
            ]
            with (destination / "tasks.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=list(rows[0]) if rows else ["arm", "task_name"]
                )
                writer.writeheader()
                writer.writerows(
                    {key: _csv_cell(value) for key, value in row.items()} for row in rows
                )
            print(f"Report written to {destination}")
    except (OSError, ValueError) as error:
        # Do not echo exception details from raw audit contents or JSON decoding.
        message = (
            str(error) if type(error) is ValueError else "Unable to read input or write report"
        )
        parser.exit(2, f"error: {message}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
