"""Synthetic Collector-only token accounting; no model or environment calls."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from mobile_world.offline.gui_ledger_usage import build_report, main, render_markdown


def usage(prompt: int = 100, output: int = 10, cached: int = 20) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "cached_tokens": cached,
        "provider_usage": raw_usage(prompt, output, cached),
    }


def raw_usage(prompt: int = 100, output: int = 10, cached: int = 20) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


class RunFixture:
    def __init__(self, directory: Path, run_id: str = "run-fixture") -> None:
        self.root = directory
        self.run_id = run_id
        self.root.mkdir(parents=True)
        self.task_streams: list[dict[str, Any]] = []
        self._write(
            "manifest.start.json",
            {
                "raw_schema_version": "mobileworld.audit.event/v1",
                "run_id": run_id,
                "model_name": "fixture-qwen",
                "agent_type": "qwen3vl",
                "suite_family": "mobile_world",
                "started_at_utc": "2026-10-08T00:00:00Z",
                "resolved_cli_config": {"gui_ledger_mode": "off", "max_round": 50},
            },
        )
        self.finalize()

    def _write(self, relative: str, value: Any) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def finalize(self, **changes: Any) -> None:
        value = {
            "run_id": self.run_id,
            "runtime_status": "completed",
            "capture_complete": True,
            "task_streams": self.task_streams,
            "missing_artifacts": [],
        }
        value.update(changes)
        self._write("manifest.final.json", value)

    def task(
        self,
        name: str = "TaskA",
        *,
        task_id: str = "task-1",
        attempt: int = 1,
        score: float | None = 1.0,
        calls: list[dict[str, Any]] | None = None,
        cumulative: int = 999_999,
        capture_complete: bool = True,
        terminal: bool = True,
        actions: int = 1,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        def append(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            seq = len(events) + 1
            event = {
                "schema_version": "mobileworld.audit.event/v1",
                "event_id": f"{task_id}-event-{seq}",
                "event_type": kind,
                "run_id": self.run_id,
                "task_run_id": task_id,
                "seq": seq,
                "wall_time": f"2026-10-08T00:00:{min(seq, 59):02d}Z",
                "payload": payload,
            }
            events.append(event)
            return event

        append(
            "task_started",
            {
                "task_name": name,
                "whole_task_attempt_index": attempt,
                "task_goal": "PRIVATE_TASK_INSTRUCTION",
                "agent": {"adapter": "qwen3vl", "model": "fixture-qwen"},
            },
        )
        append("step_started", {"step_id": f"{task_id}-step", "step_index": 0})
        for index, call in enumerate(calls if calls is not None else [{}]):
            request_id = call.get("request_id", f"request-{index}")
            correlation = {
                "request_id": request_id,
                "model_call_id": call.get("model_call_id", f"model-{index}"),
                "retry_group_id": call.get("retry_group_id", "retry-group"),
                "adapter_attempt_index": call.get("adapter_attempt_index", 1),
                "attempt_index": call.get("attempt_index", 1),
                "step_id": f"{task_id}-step",
            }
            request = {
                **correlation,
                "call_role": call.get("role", "actor"),
                "component": "mobile_world.agents.implementations.qwen3vl",
                "stream": bool(call.get("stream", False)),
                "request_view": {
                    "model": "fixture-qwen",
                    "messages": [{"role": "user", "content": "PRIVATE_PROMPT_NOT_FOR_REPORT"}],
                },
            }
            if call.get("omit_role"):
                request.pop("call_role")
            append("model_request", request)
            for chunk_index, chunk_usage in enumerate(call.get("chunks", [])):
                append(
                    "model_stream_chunk",
                    {
                        **correlation,
                        "chunk_index": chunk_index,
                        "chunk_view": {"usage": chunk_usage, "choices": []},
                    },
                )
            terminal_type = call.get("terminal", "model_response")
            if terminal_type is None:
                continue
            normalized = {"usage": call.get("usage", usage()), "choices": []}
            payload: dict[str, Any] = dict(correlation)
            if terminal_type == "model_attempt_failed":
                payload.update(
                    {
                        "normalized_partial_response": normalized,
                        "failure_phase": "stream_iteration",
                        "retry_planned": False,
                        "exception": {"message": "PRIVATE_ERROR_NOT_FOR_REPORT"},
                    }
                )
            else:
                payload.update(
                    {
                        "normalized_response": normalized,
                        "response_mode": "stream" if call.get("stream") else "non_stream",
                        "stream_state": "complete" if call.get("stream") else None,
                    }
                )
            if "raw_usage" in call:
                payload["raw_response_view"] = {"usage": call["raw_usage"]}
            append(terminal_type, payload)
            if call.get("duplicate_terminal"):
                append(terminal_type, payload)
            if call.get("conflicting_terminal"):
                append(
                    "model_response",
                    {**correlation, "normalized_response": {"usage": usage(333, 44, 55)}},
                )
            if call.get("parse_failed"):
                append(
                    "agent_decision",
                    {"step_id": f"{task_id}-step", "parse_status": "failed"},
                )
        for index in range(actions):
            append(
                "action_execution_started",
                {
                    "step_id": f"{task_id}-step",
                    "execution_id": f"execution-{index}",
                    "execution_kind": "gui",
                    "action": {"action_type": "click", "x": 1, "y": 1},
                },
            )
        if terminal:
            append(
                "task_ended",
                {
                    "runtime_status": "completed" if score is not None else "crashed",
                    "environment_evaluation": {"score": score},
                    "capture_complete": capture_complete,
                    "missing_artifacts": [] if capture_complete else ["model_response"],
                    "token_usage": {
                        "prompt_tokens": cumulative,
                        "completion_tokens": cumulative,
                        "total_tokens": 2 * cumulative,
                        "cached_tokens": cumulative,
                    },
                },
            )
        self.write_events(task_id, events)
        self.task_streams.append(
            {
                "task_run_id": task_id,
                "relative_path": f"tasks/{task_id}/events.jsonl",
                "runtime_status": "completed" if score is not None else "crashed",
                "capture_complete": capture_complete,
            }
        )
        self.finalize()
        return events

    def write_events(self, task_id: str, events: list[dict[str, Any]]) -> None:
        path = self.root / "tasks" / task_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def arm(run: RunFixture, expected_tasks: list[str] | None = None) -> dict[str, Any]:
    return build_report({"OFF": [run.root]}, expected_tasks=expected_tasks)["arms"]["OFF"]


def test_counts_every_physical_actor_request_including_transport_and_parse_retries(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        calls=[
            {"usage": usage(10, 2, 1), "parse_failed": True, "adapter_attempt_index": 1},
            {
                "usage": usage(20, 3, 2),
                "model_call_id": "model-1",
                "adapter_attempt_index": 2,
                "attempt_index": 1,
                "terminal": "model_attempt_failed",
            },
            {
                "usage": usage(30, 4, 3),
                "model_call_id": "model-1",
                "adapter_attempt_index": 2,
                "attempt_index": 2,
            },
        ],
    )
    result = arm(run)
    assert result["model_calls"] == 3
    assert result["input_tokens"] == 60
    assert result["output_tokens"] == 9
    assert result["cached_tokens"] == 6
    assert result["gui_action_attempts"] == 1


@pytest.mark.parametrize("stream", [False, True])
def test_failed_terminal_with_observed_usage_counts_once(tmp_path, stream):
    run = RunFixture(tmp_path / "run")
    call = {"terminal": "model_attempt_failed", "stream": stream, "usage": usage(21, 4, 2)}
    if not stream:
        call.update({"usage": None, "raw_usage": raw_usage(21, 4, 2)})
    run.task(calls=[call], score=0.0)
    result = arm(run)
    assert result["input_tokens"] == 21
    assert result["output_tokens"] == 4
    assert result["cached_tokens"] == 2
    assert result["model_calls"] == 1


def test_stream_terminal_usage_is_not_added_to_cumulative_chunk_usage(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        calls=[
            {
                "stream": True,
                "usage": usage(100, 15, 20),
                "chunks": [raw_usage(100, 7, 20), raw_usage(100, 15, 20)],
            }
        ]
    )
    result = arm(run)
    assert (result["input_tokens"], result["output_tokens"], result["cached_tokens"]) == (
        100,
        15,
        20,
    )
    assert result["model_calls"] == 1


def test_stream_chunks_without_terminal_usage_do_not_become_billed_totals(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"stream": True, "usage": None, "chunks": [raw_usage()]}])
    result = arm(run)
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert result["calls_missing_input"] == result["calls_missing_output"] == 1


def test_whole_task_retries_sum_calls_not_cumulative_task_totals_and_latest_score_wins(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        task_id="earlier",
        attempt=1,
        score=1.0,
        calls=[{"usage": usage(100, 10, 20)}],
        cumulative=100,
    )
    run.task(
        task_id="later", attempt=2, score=0.0, calls=[{"usage": usage(50, 5, 10)}], cumulative=150
    )
    result = arm(run)
    assert result["task_count"] == 1 and result["task_attempts"] == 2
    assert result["input_tokens"] == 150 and result["output_tokens"] == 15
    assert result["successes"] == 0 and result["success_rate"] == 0.0
    assert result["tasks"][0]["score"] == 0.0
    assert result["tasks"][0]["attempts"] == 2


def test_latest_attempt_selection_does_not_depend_on_directory_sort_order(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(task_id="zz-earlier", attempt=1, score=0.0)
    run.task(task_id="aa-later", attempt=2, score=1.0)
    assert arm(run)["successes"] == 1


def test_latest_unscored_attempt_is_not_replaced_by_earlier_success(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(task_id="earlier", attempt=1, score=1.0)
    run.task(task_id="later", attempt=2, score=None)
    result = arm(run)
    assert result["tasks"][0]["score"] is None
    assert result["scored_tasks"] == 0
    assert result["success_rate"] is None
    assert result["input_tokens_per_success"] is None


def test_request_identity_is_scoped_to_task_attempt(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        task_id="first", attempt=1, calls=[{"request_id": "same-id", "usage": usage(40, 4, 0)}]
    )
    run.task(
        task_id="second", attempt=2, calls=[{"request_id": "same-id", "usage": usage(60, 6, 0)}]
    )
    result = arm(run)
    assert result["model_calls"] == 2 and result["input_tokens"] == 100


@pytest.mark.parametrize("terminal", ["model_response", "model_attempt_failed", None])
def test_missing_usage_is_unknown_not_zero_and_known_subtotals_survive(tmp_path, terminal):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"usage": usage(10, 2, 1)}, {"terminal": terminal, "usage": None}])
    result = arm(run)
    assert result["model_calls"] == 2
    assert result["input_tokens"] is None and result["known_input_tokens"] == 10
    assert result["output_tokens"] is None and result["known_output_tokens"] == 2
    assert result["cached_tokens"] is None and result["known_cached_tokens"] == 1
    assert result["calls_missing_input"] == result["calls_missing_output"] == 1
    assert result["input_tokens_per_success"] is None
    assert result["complete"] is False


def test_input_and_output_missingness_are_independent(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        calls=[
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": None,
                    "cached_tokens": 0,
                    "provider_usage": {
                        "prompt_tokens": 100,
                        "prompt_tokens_details": {"cached_tokens": 0},
                    },
                }
            }
        ]
    )
    result = arm(run)
    assert result["input_tokens"] == 100
    assert result["output_tokens"] is None
    assert result["calls_missing_input"] == 0 and result["calls_missing_output"] == 1


def test_reported_cache_is_a_subset_not_additional_or_subtracted_input(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"usage": usage(100, 10, 80)}])
    result = arm(run)
    assert result["input_tokens"] == 100 and result["output_tokens"] == 10
    assert result["cached_tokens"] == 80
    assert result["input_tokens_per_success"] == 100


def test_normalizer_default_zero_is_not_evidence_provider_reported_cache(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(
        calls=[
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "cached_tokens": 0,
                    "provider_usage": {"prompt_tokens": 100, "completion_tokens": 10},
                }
            }
        ]
    )
    result = arm(run)
    assert result["input_tokens"] == 100 and result["output_tokens"] == 10
    assert result["cached_tokens"] is None and result["calls_missing_cache"] == 1


@pytest.mark.parametrize("bad", [-1, True, "100", 1.5])
def test_malformed_prompt_usage_does_not_silently_coerce_to_count(tmp_path, bad):
    run = RunFixture(tmp_path / "run")
    invalid = usage()
    invalid["prompt_tokens"] = bad
    invalid["provider_usage"]["prompt_tokens"] = bad
    run.task(calls=[{"usage": invalid}])
    result = arm(run)
    assert result["input_tokens"] is None
    assert result["calls_missing_input"] == 1
    assert not result["complete"]


def test_cache_greater_than_prompt_is_not_admitted_as_valid_subset(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"usage": usage(10, 2, 11)}])
    result = arm(run)
    assert result["cached_tokens"] is None and result["calls_missing_cache"] == 1


@pytest.mark.parametrize("role", ["sentinel", "simulated_user", "judge"])
def test_nonactor_calls_are_excluded(tmp_path, role):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"usage": usage(10, 2, 1)}, {"role": role, "usage": usage(900, 90, 9)}])
    result = arm(run)
    assert result["model_calls"] == 1
    assert result["input_tokens"] == 10


def test_missing_call_role_is_not_guessed_actor_from_component(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"omit_role": True}])
    result = arm(run)
    assert result["model_calls"] == 0
    assert result["issues"] and not result["complete"]


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_physical_terminal_never_doublecounts(tmp_path, conflicting):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"duplicate_terminal": not conflicting, "conflicting_terminal": conflicting}])
    result = arm(run)
    assert result["model_calls"] == 1
    assert result["input_tokens"] in (None, 100)
    if conflicting:
        assert result["input_tokens"] is None and not result["complete"]


def test_conflicting_normalized_and_raw_terminal_usage_is_not_silently_preferred(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"usage": usage(100, 10, 20), "raw_usage": raw_usage(120, 10, 20)}])
    result = arm(run)
    assert result["input_tokens"] is None and result["issues"]


@pytest.mark.parametrize(
    "broken", ["missing_final", "unfinished_run", "capture_incomplete", "missing_task_terminal"]
)
def test_incomplete_evidence_disables_reduction_comparisons(tmp_path, broken):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task()
    full.task(calls=[{"usage": usage(50, 5, 10)}], terminal=broken != "missing_task_terminal")
    if broken == "missing_final":
        (full.root / "manifest.final.json").unlink()
    elif broken == "unfinished_run":
        full.finalize(runtime_status="aborted")
    elif broken == "capture_incomplete":
        full.finalize(capture_complete=False)
    report = build_report({"OFF": [off.root], "FULL": [full.root]}, expected_tasks=["TaskA"])
    result = report["arms"]["FULL"]
    assert not result["complete"]
    assert result["input_reduction_vs_baseline"] is None
    assert result["comparison_issues"]


def test_task_capture_incomplete_is_not_overridden_by_complete_run_manifest(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(capture_complete=False)
    assert not arm(run)["complete"]


def test_same_complete_cohort_compares_all_tasks_including_failures(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task(name="TaskA", score=1.0)
    off.task(name="TaskB", task_id="task-2", score=0.0)
    full.task(name="TaskA", score=1.0, calls=[{"usage": usage(50, 5, 10)}])
    full.task(name="TaskB", task_id="task-2", score=0.0, calls=[{"usage": usage(50, 5, 10)}])
    report = build_report(
        {"OFF": [off.root], "FULL": [full.root]}, expected_tasks=["TaskA", "TaskB"]
    )
    result = report["arms"]["FULL"]
    assert result["complete"]
    assert result["input_reduction_vs_baseline"] == 0.5
    assert result["success_rate"] == 0.5
    assert result["input_tokens_per_success"] == 100


def test_missing_expected_task_does_not_silently_shrink_denominator(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task()
    result = arm(run, expected_tasks=["TaskA", "TaskB"])
    assert not result["complete"]
    assert result["success_rate"] is None
    assert result["input_reduction_vs_baseline"] is None


def test_different_observed_cohorts_do_not_support_reduction_claim(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task(name="TaskA")
    full.task(name="TaskB")
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["input_reduction_vs_baseline"] is None
    assert result["comparison_issues"]


def test_same_name_across_roots_is_ambiguous_not_a_guessed_latest_attempt(tmp_path):
    first = RunFixture(tmp_path / "first", "first-run")
    second = RunFixture(tmp_path / "second", "second-run")
    first.task(score=1.0)
    second.task(score=0.0)
    result = build_report({"OFF": [first.root, second.root]})["arms"]["OFF"]
    assert not result["complete"]
    assert any("ambiguous_task_attempts" in issue for issue in result["issues"])
    assert result["input_tokens"] == 200


@pytest.mark.parametrize("same_path", [True, False])
def test_repeated_root_or_run_id_is_rejected_to_prevent_doublecount(tmp_path, same_path):
    first = RunFixture(tmp_path / "first", "same-run")
    first.task()
    if same_path:
        second_root = first.root
    else:
        second = RunFixture(tmp_path / "second", "same-run")
        second.task()
        second_root = second.root
    with pytest.raises(ValueError):
        build_report({"OFF": [first.root, second_root]})


@pytest.mark.parametrize("score,success", [(1.0, 1), (0.999, 1), (0.99, 0), (0.5, 0), (0.0, 0)])
def test_official_success_threshold_is_strictly_above_point_99(tmp_path, score, success):
    run = RunFixture(tmp_path / "run")
    run.task(score=score)
    assert arm(run)["successes"] == success


def test_zero_success_has_no_fabricated_cost_per_success(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(score=0.0)
    assert arm(run)["input_tokens_per_success"] is None


def test_zero_baseline_tokens_has_no_percentage_division(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task(calls=[{"usage": usage(0, 0, 0)}])
    full.task(calls=[{"usage": usage(0, 0, 0)}])
    assert (
        build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"][
            "input_reduction_vs_baseline"
        ]
        is None
    )


def test_single_nonbaseline_arm_reports_usage_without_claiming_reduction(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task()
    report = build_report({"FULL": [run.root]})
    assert report["arms"]["FULL"]["input_tokens"] == 100
    assert report["arms"]["FULL"]["input_reduction_vs_baseline"] is None


def test_reports_do_not_copy_task_prompt_provider_response_or_exception_text(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task(calls=[{"terminal": "model_attempt_failed"}])
    report = build_report({"OFF": [run.root]})
    rendered = json.dumps(report) + render_markdown(report)
    for private in (
        "PRIVATE_TASK_INSTRUCTION",
        "PRIVATE_PROMPT_NOT_FOR_REPORT",
        "PRIVATE_ERROR_NOT_FOR_REPORT",
    ):
        assert private not in rendered


def test_build_and_markdown_are_read_only_on_raw_evidence(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task()
    before = {
        str(path.relative_to(run.root)): path.read_bytes()
        for path in run.root.rglob("*")
        if path.is_file()
    }
    render_markdown(build_report({"OFF": [run.root]}))
    after = {
        str(path.relative_to(run.root)): path.read_bytes()
        for path in run.root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_cli_stdout_markdown_never_writes_into_raw_run(tmp_path, capsys):
    run = RunFixture(tmp_path / "run")
    run.task()
    original_paths = {str(path.relative_to(run.root)) for path in run.root.rglob("*")}
    assert main(["--run", f"OFF={run.root}"]) == 0
    output = capsys.readouterr().out
    assert "OFF" in output and "100" in output
    assert {str(path.relative_to(run.root)) for path in run.root.rglob("*")} == original_paths


@pytest.mark.parametrize(
    "location", ["source_root", "source_child", "symlink_child", "existing_output"]
)
def test_cli_rejects_unsafe_or_existing_output_destinations_without_writes(tmp_path, location):
    run = RunFixture(tmp_path / "run")
    run.task()
    if location == "source_root":
        output = run.root
    elif location == "source_child":
        output = run.root / "derived"
    elif location == "symlink_child":
        alias = tmp_path / "alias"
        alias.symlink_to(run.root, target_is_directory=True)
        output = alias / "derived"
    else:
        output = tmp_path / "old_report"
        output.mkdir()
        (output / "report.json").write_text("original report", encoding="utf-8")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(SystemExit) as error:
        main(["--run", f"OFF={run.root}", "--output-dir", str(output)])
    assert error.value.code == 2
    after = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_cli_writes_new_external_numeric_reports_and_uses_explicit_cohort(tmp_path, capsys):
    run = RunFixture(tmp_path / "run")
    run.task()
    expected = tmp_path / "expected.txt"
    expected.write_text("TaskA\n", encoding="utf-8")
    destination = tmp_path / "new_report"
    assert (
        main(
            [
                "--run",
                f"OFF={run.root}",
                "--expected-tasks",
                str(expected),
                "--output-dir",
                str(destination),
            ]
        )
        == 0
    )
    assert {path.name for path in destination.iterdir()} == {
        "report.json",
        "report.md",
        "tasks.csv",
    }
    report = json.loads((destination / "report.json").read_text())
    assert report["cohort_source"] == "provided"
    assert report["arms"]["OFF"]["input_tokens"] == 100
    assert "TaskA" in (destination / "tasks.csv").read_text()
    assert "Report written" in capsys.readouterr().out


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_name", "other-model"),
        ("agent_type", "other-agent"),
        ("suite_family", "other-suite"),
        ("max_round", 100),
    ],
)
def test_mismatched_run_configuration_disables_reduction_but_preserves_accounting(
    tmp_path, field, value
):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task()
    full.task(calls=[{"usage": usage(50, 5, 10)}])
    manifest = json.loads((full.root / "manifest.start.json").read_text())
    if field == "max_round":
        manifest["resolved_cli_config"][field] = value
    else:
        manifest[field] = value
    full._write("manifest.start.json", manifest)
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["input_tokens"] == 50
    assert result["input_reduction_vs_baseline"] is None
    assert "mismatched_configurations" in result["comparison_issues"]


@pytest.mark.parametrize(
    "field,value", [("model", "another-model"), ("temperature", 0.7), ("max_tokens", 2000)]
)
def test_mismatched_actual_actor_request_configuration_disables_comparison(tmp_path, field, value):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task()
    events = full.task(calls=[{"usage": usage(50, 5, 10)}])
    request = next(event for event in events if event["event_type"] == "model_request")
    request["payload"]["request_view"][field] = value
    full.write_events("task-1", events)
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["input_reduction_vs_baseline"] is None
    assert "mismatched_request_configurations" in result["comparison_issues"]


def test_treatment_configuration_difference_is_the_intended_comparison_not_a_mismatch(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task()
    full.task(calls=[{"usage": usage(50, 5, 10)}])
    manifest = json.loads((full.root / "manifest.start.json").read_text())
    manifest["resolved_cli_config"].update(
        gui_ledger_mode="full", gui_ledger_ui_tree=True, gui_ledger_extra_model_calls=0
    )
    full._write("manifest.start.json", manifest)
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["input_reduction_vs_baseline"] == 0.5


def test_unreported_cache_does_not_block_complete_input_token_comparison(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    off.task()
    uncached_details_unknown = {
        "prompt_tokens": 50,
        "completion_tokens": 5,
        "cached_tokens": 0,
        "provider_usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }
    full.task(calls=[{"usage": uncached_details_unknown}])
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["cached_tokens"] is None
    assert result["complete"]
    assert result["input_reduction_vs_baseline"] == 0.5


def test_missing_required_comparison_configuration_does_not_fabricate_reduction(tmp_path):
    off = RunFixture(tmp_path / "off", "run-off")
    full = RunFixture(tmp_path / "full", "run-full")
    for run in (off, full):
        run.task()
        manifest = json.loads((run.root / "manifest.start.json").read_text())
        manifest["resolved_cli_config"].pop("max_round")
        run._write("manifest.start.json", manifest)
    result = build_report({"OFF": [off.root], "FULL": [full.root]})["arms"]["FULL"]
    assert result["input_tokens"] == 100
    assert result["input_reduction_vs_baseline"] is None
    assert "unverified_configuration" in result["comparison_issues"]


@pytest.mark.parametrize(
    "inventory", [None, {}, [], [{"task_run_id": "task-elsewhere", "capture_complete": True}]]
)
def test_missing_malformed_or_mismatched_final_inventory_is_incomplete(tmp_path, inventory):
    run = RunFixture(tmp_path / "run")
    run.task()
    final = json.loads((run.root / "manifest.final.json").read_text())
    if inventory is None:
        final.pop("task_streams")
    else:
        final["task_streams"] = inventory
    run._write("manifest.final.json", final)
    result = arm(run)
    assert not result["complete"]
    assert result["issues"]


@pytest.mark.parametrize("mode", ["duplicate_id", "missing_capture_flag", "incomplete_capture"])
def test_final_task_inventory_must_be_unique_and_explicitly_complete(tmp_path, mode):
    run = RunFixture(tmp_path / "run")
    run.task()
    final = json.loads((run.root / "manifest.final.json").read_text())
    summary = final["task_streams"][0]
    if mode == "duplicate_id":
        final["task_streams"].append(dict(summary))
    elif mode == "missing_capture_flag":
        summary.pop("capture_complete")
    else:
        summary["capture_complete"] = False
    run._write("manifest.final.json", final)
    assert not arm(run)["complete"]


@pytest.mark.parametrize("total", [0, 109, 111])
def test_reported_total_conflicting_with_input_plus_output_invalidates_usage(tmp_path, total):
    run = RunFixture(tmp_path / "run")
    observed = usage(100, 10, 20)
    observed["provider_usage"]["total_tokens"] = total
    run.task(calls=[{"usage": observed}])
    result = arm(run)
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert not result["complete"] and "conflicting_usage" in result["issues"]


@pytest.mark.parametrize("total", [-1, True, "110", 110.5])
def test_invalid_reported_total_is_flagged_without_coercion(tmp_path, total):
    run = RunFixture(tmp_path / "run")
    observed = usage()
    observed["provider_usage"]["total_tokens"] = total
    run.task(calls=[{"usage": observed}])
    result = arm(run)
    assert not result["complete"]
    assert "invalid_usage_value" in result["issues"]


def test_omitted_provider_total_does_not_erase_known_input_and_output(tmp_path):
    run = RunFixture(tmp_path / "run")
    observed = usage()
    observed["provider_usage"].pop("total_tokens")
    run.task(calls=[{"usage": observed}])
    result = arm(run)
    assert result["input_tokens"] == 100 and result["output_tokens"] == 10
    assert result["complete"]


def test_gui_action_count_excludes_answers_simulator_interactions_and_mcp(tmp_path):
    run = RunFixture(tmp_path / "run")
    events = run.task(actions=4)
    executions = [event for event in events if event["event_type"] == "action_execution_started"]
    for event, kind in zip(executions, ("gui", "answer", "ask_user", "mcp"), strict=True):
        event["payload"]["execution_kind"] = kind
    run.write_events("task-1", events)
    result = arm(run)
    assert result["action_attempts"] == 4
    assert result["gui_action_attempts"] == 1
    assert result["non_gui_action_attempts"] == 3


@pytest.mark.parametrize("kind", [None, "unrecognized"])
def test_missing_or_unknown_execution_kind_is_not_assumed_gui(tmp_path, kind):
    run = RunFixture(tmp_path / "run")
    events = run.task()
    execution = next(event for event in events if event["event_type"] == "action_execution_started")
    if kind is None:
        execution["payload"].pop("execution_kind")
    else:
        execution["payload"]["execution_kind"] = kind
    run.write_events("task-1", events)
    result = arm(run)
    assert result["gui_action_attempts"] == 0
    assert not result["complete"] and "unknown_action_kind" in result["issues"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_call_id", "another-call"),
        ("retry_group_id", "another-group"),
        ("adapter_attempt_index", 2),
        ("attempt_index", 2),
        ("step_id", "another-step"),
    ],
)
def test_request_and_terminal_correlation_conflict_cannot_admit_usage(tmp_path, field, value):
    run = RunFixture(tmp_path / "run")
    events = run.task()
    terminal = next(event for event in events if event["event_type"] == "model_response")
    terminal["payload"][field] = value
    run.write_events("task-1", events)
    result = arm(run)
    assert result["model_calls"] == 1
    assert result["input_tokens"] is result["output_tokens"] is None
    assert "conflicting_call_correlation" in result["issues"]
    assert not result["complete"]


@pytest.mark.parametrize("task_name", ["=1+1", "+1+1", "-1+1", "@SUM(1,2)", "\t =1+1"])
def test_csv_neutralizes_formula_cells_while_json_keeps_original_task_name(tmp_path, task_name):
    run = RunFixture(tmp_path / "run")
    run.task(name=task_name)
    destination = tmp_path / "report"
    assert main(["--run", f"OFF={run.root}", "--output-dir", str(destination)]) == 0
    with (destination / "tasks.csv").open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["task_name"] == "'" + task_name
    report = json.loads((destination / "report.json").read_text())
    assert report["arms"]["OFF"]["tasks"][0]["task_name"] == task_name
    assert report["expected_tasks"] == [task_name]


def test_markdown_arm_metadata_cannot_create_live_links_html_or_extra_table_cells(tmp_path):
    run = RunFixture(tmp_path / "run")
    run.task()
    label = "[click](https://example.invalid) <img src=x> | extra"
    report = build_report({label: [run.root]})
    rendered = render_markdown(report)
    assert "[click](https://example.invalid)" not in rendered
    assert "<img src=x>" not in rendered
    assert "\\[click\\]\\(https://example\\.invalid\\)" in rendered
    assert "\\<img src=x\\> \\| extra" in rendered
