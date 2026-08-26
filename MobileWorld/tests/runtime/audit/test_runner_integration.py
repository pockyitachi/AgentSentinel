from __future__ import annotations

import io
import json
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

import mobile_world.core.runner as runner_module
import mobile_world.runtime.audit.execution_io as execution_io_module
from mobile_world.core.runner import _execute_single_task, _process_task_on_env
from mobile_world.runtime.audit.config import AuditConfig
from mobile_world.runtime.audit.execution_io import (
    record_gui_request,
    record_gui_response,
    record_screenshot_source,
)
from mobile_world.runtime.audit.integrity import check_run_integrity
from mobile_world.runtime.audit.lifecycle import bootstrap_audit_run
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import (
    RunnerTaskCapture,
    RunnerTaskMetadata,
)
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.utils.models import ANSWER, ENV_FAIL, FINISHED, UNKNOWN, JSONAction


class _HTTPResponse:
    status_code = 200
    content = b'{"status":"ok"}'
    headers = {"content-type": "application/json"}


class _TerminalAction:
    def __init__(self, action_type: str) -> None:
        self.action_type = action_type

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return {"action_type": self.action_type}


def _image_and_png(color: tuple[int, int, int]) -> tuple[Image.Image, bytes]:
    image = Image.new("RGB", (3, 2), color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return image, output.getvalue()


class _FakeEnv:
    base_url = "http://fixture.invalid"

    def __init__(
        self,
        calls: list[Any],
        *,
        goal_exception: BaseException | None = None,
        execute_exception: BaseException | None = None,
        evaluation_exception: BaseException | None = None,
        teardown_exception: BaseException | None = None,
    ) -> None:
        self.calls = calls
        self.goal_exception = goal_exception
        self.execute_exception = execute_exception
        self.evaluation_exception = evaluation_exception
        self.teardown_exception = teardown_exception
        self.pre_image, self.pre_png = _image_and_png((10, 20, 30))
        self.post_image, self.post_png = _image_and_png((40, 50, 60))

    def get_task_goal(self, *, task_type: str) -> str:
        self.calls.append(("goal", task_type))
        if self.goal_exception is not None:
            raise self.goal_exception
        return "exact fixture goal"

    def initialize_task(self, *, task_name: str) -> SimpleNamespace:
        self.calls.append(("initialize", task_name))
        record_screenshot_source(self.pre_image, self.pre_png)
        return SimpleNamespace(
            screenshot=self.pre_image,
            tool_call=None,
            ask_user_response=None,
        )

    def execute_action(self, action: Any) -> SimpleNamespace:
        self.calls.append(("execute", action))
        record_gui_request(
            {"device": "fixture", "action": action.model_dump()},
            request_endpoint=f"{self.base_url}/step",
        )
        if self.execute_exception is not None:
            raise self.execute_exception
        record_gui_response(_HTTPResponse())
        record_screenshot_source(self.post_image, self.post_png)
        return SimpleNamespace(
            screenshot=self.post_image,
            tool_call=None,
            ask_user_response=None,
        )

    def get_task_score(self, *, task_type: str) -> tuple[float, str]:
        self.calls.append(("score", task_type))
        if self.evaluation_exception is not None:
            raise self.evaluation_exception
        return 0.75, "exact evaluator reason"

    def tear_down_task(self, *, task_type: str) -> dict[str, Any]:
        self.calls.append(("teardown", task_type))
        if self.teardown_exception is not None:
            raise self.teardown_exception
        return {"status": "success", "message": "exact teardown result"}


class _FakeAgent:
    def __init__(self, calls: list[Any], outcomes: list[Any]) -> None:
        self.calls = calls
        self.outcomes = list(outcomes)

    def initialize(self, goal: str) -> None:
        self.calls.append(("agent.initialize", goal))

    def predict(self, observation: dict[str, Any]) -> tuple[Any, Any]:
        self.calls.append(("predict", observation))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def get_total_token_usage(self) -> dict[str, int]:
        self.calls.append("token_usage")
        return {"input_tokens": 3, "output_tokens": 2}

    def done(self) -> None:
        self.calls.append("agent.done")


class _FakeTrajLogger:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls

    def log_traj(self, *args: Any) -> None:
        self.calls.append(("log_traj", args))

    def log_score(self, *, score: float, reason: str) -> None:
        self.calls.append(("log_score", score, reason))

    def log_tools(self, tools: Any) -> None:
        self.calls.append(("log_tools", tools))


def _capture(
    root: Path,
    *,
    capture_type: type[RunnerTaskCapture] = RunnerTaskCapture,
) -> tuple[RunRecorder, TaskRecorder, RunnerTaskCapture, RunnerTaskMetadata]:
    recorder = RunRecorder(
        root,
        producer=Producer.local(version="test", worker_id="runner-integration"),
        sync=False,
    )
    recorder.write_manifest_start({"run_id": recorder.run_id})
    task = recorder.open_task()
    capture = capture_type(task)
    metadata = RunnerTaskMetadata(
        run_id=recorder.run_id,
        task_run_id=task.task_run_id,
        task_index=7,
        suite_family="mobile_world",
        agent={"adapter": "fixture", "model": "none", "configuration": {}},
        environment={"backend_id": "fixture", "device_id": "fixture-device"},
        whole_task_attempt_index=2,
    )
    return recorder, task, capture, metadata


def _audit_lifecycle(tmp_path: Path) -> Any:
    return bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit"),
        repository_root=tmp_path / "repository",
        repository="fixture/AgentSentinel",
        repository_url="https://github.com/fixture/AgentSentinel.git",
        repository_commit="a" * 40,
        repository_dirty=False,
        mobile_world_upstream_url="https://github.com/Tongyi-MAI/MobileWorld.git",
        mobile_world_upstream_commit="b" * 40,
        agent_type="fixture",
        model_name="none",
        sync=False,
    )


def _only_task_events(lifecycle: Any) -> list[dict[str, Any]]:
    paths = list(lifecycle.recorder.run_root.glob("tasks/*/events.jsonl"))
    assert len(paths) == 1
    return [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]


def _events(task: TaskRecorder) -> list[dict[str, Any]]:
    return [json.loads(line) for line in task.path.read_text(encoding="utf-8").splitlines()]


def _event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event for event in events if event["event_type"] == event_type]
    assert len(matches) == 1
    return matches[0]


def _run(
    tmp_path: Path,
    outcomes: list[Any],
    *,
    max_step: int = 1,
    env_kwargs: dict[str, Any] | None = None,
    capture_type: type[RunnerTaskCapture] = RunnerTaskCapture,
) -> tuple[
    tuple[int, float],
    list[Any],
    _FakeEnv,
    TaskRecorder,
    RunnerTaskCapture,
]:
    calls: list[Any] = []
    env = _FakeEnv(calls, **(env_kwargs or {}))
    agent = _FakeAgent(calls, outcomes)
    traj = _FakeTrajLogger(calls)
    _, task, capture, metadata = _capture(tmp_path, capture_type=capture_type)
    result = _execute_single_task(
        env,
        agent,
        "FixtureTask",
        max_step,
        traj,
        audit_capture=capture,
        audit_metadata=metadata,
    )
    return result, calls, env, task, capture


def test_enabled_normal_max_step_captures_last_post_state_and_task_result(
    tmp_path: Path,
) -> None:
    action = JSONAction(action_type="click", x=11, y=12)
    result, calls, env, task, capture = _run(
        tmp_path,
        [("exact prediction", action)],
    )

    assert result == (1, 0.75)
    assert ("execute", action) in calls
    assert calls[-1] == "agent.done"
    events = _events(task)
    assert [event["event_type"] for event in events] == [
        "task_started",
        "step_started",
        "agent_decision",
        "action_execution_started",
        "transition_completed",
        "task_ended",
    ]
    transition = _event(events, "transition_completed")["payload"]
    assert transition["post_observation"]["screenshot"]["source_blob"]["digest"]
    assert transition["duration_ns"] >= 0
    assert transition["execution_result"]["http_status"] == 200
    task_ended = _event(events, "task_ended")["payload"]
    assert task_ended["termination"] == {
        "source": "max_step",
        "step_index": 1,
        "exception": None,
    }
    assert task_ended["environment_evaluation"] == {
        "score": 0.75,
        "reason": "exact evaluator reason",
        "exception": None,
    }
    assert task_ended["teardown"]["returned"] is True
    assert task_ended["capture_complete"] is True
    assert capture.capture_complete is True
    assert env.post_image.tobytes() == bytes((40, 50, 60)) * 6


def test_complete_fake_run_finalizes_and_passes_integrity(tmp_path: Path) -> None:
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit"),
        repository_root=tmp_path / "repository",
        repository="fixture/AgentSentinel",
        repository_url="https://github.com/fixture/AgentSentinel.git",
        repository_commit="a" * 40,
        repository_dirty=False,
        mobile_world_upstream_url="https://github.com/Tongyi-MAI/MobileWorld.git",
        mobile_world_upstream_commit="b" * 40,
        agent_type="fixture",
        model_name="none",
        sync=False,
    )
    calls: list[Any] = []
    env = _FakeEnv(calls)
    agent = _FakeAgent(
        calls,
        [("exact prediction", JSONAction(action_type="click", x=11, y=12))],
    )
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent=agent,
        environment=env,
        whole_task_attempt_index=1,
    )
    assert binding is not None

    result = _execute_single_task(
        env,
        agent,
        "FixtureTask",
        1,
        _FakeTrajLogger(calls),
        audit_capture=binding.capture,
        audit_metadata=binding.metadata,
    )
    lifecycle.finish_task_attempt(
        binding=binding,
        result=result,
        exception=None,
        retry_planned=False,
        runtime_status="completed",
    )
    final_manifest = lifecycle.finalize()

    assert final_manifest is not None
    report = check_run_integrity(lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_answer_executes_and_captures_post_state_before_termination(tmp_path: Path) -> None:
    action = JSONAction(action_type=ANSWER, text="exact answer")
    result, calls, _, task, _ = _run(tmp_path, [("answer prediction", action)], max_step=9)

    assert result == (1, 0.75)
    assert ("execute", action) in calls
    assert (
        _event(_events(task), "task_ended")["payload"]["termination"]["source"] == "answer_action"
    )
    assert _event(_events(task), "transition_completed")["payload"]["post_observation"] is not None


@pytest.mark.parametrize("action_type", [FINISHED, UNKNOWN, ENV_FAIL])
def test_terminal_actions_are_not_executed(tmp_path: Path, action_type: str) -> None:
    action = _TerminalAction(action_type)
    result, calls, _, task, _ = _run(tmp_path, [("terminal prediction", action)])

    assert result == (1, 0.75)
    assert not any(call[0] == "execute" for call in calls if isinstance(call, tuple))
    transition = _event(_events(task), "transition_not_executed")["payload"]
    assert transition["reason"] == "terminal_action"
    assert transition["post_observation"] is None


def test_prediction_none_records_nonexecution_and_preserves_original_flow(
    tmp_path: Path,
) -> None:
    action = JSONAction(action_type="click", x=1, y=2)
    result, calls, _, task, _ = _run(tmp_path, [(None, action)])

    assert result == (1, 0.75)
    assert not any(call[0] == "execute" for call in calls if isinstance(call, tuple))
    events = _events(task)
    assert _event(events, "agent_decision")["payload"]["parse_outcome"] == (
        "returned_prediction_none"
    )
    assert _event(events, "transition_not_executed")["payload"]["reason"] == ("prediction_none")
    assert _event(events, "task_ended")["payload"]["runtime_status"] == "aborted"


def test_prediction_none_runtime_status_matches_final_manifest(tmp_path: Path) -> None:
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit"),
        repository_root=tmp_path / "repository",
        repository="fixture/AgentSentinel",
        repository_url="https://github.com/fixture/AgentSentinel.git",
        repository_commit="a" * 40,
        repository_dirty=False,
        mobile_world_upstream_url="https://github.com/Tongyi-MAI/MobileWorld.git",
        mobile_world_upstream_commit="b" * 40,
        agent_type="fixture",
        model_name="none",
        sync=False,
    )
    calls: list[Any] = []
    env = _FakeEnv(calls)
    action = JSONAction(action_type="click", x=1, y=2)
    agent = _FakeAgent(calls, [(None, action)])
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent=agent,
        environment=env,
        whole_task_attempt_index=1,
    )
    assert binding is not None
    statuses: list[str] = []

    result = _execute_single_task(
        env,
        agent,
        "FixtureTask",
        1,
        _FakeTrajLogger(calls),
        audit_capture=binding.capture,
        audit_metadata=binding.metadata,
        audit_runtime_status_callback=statuses.append,
    )
    assert statuses == ["aborted"]
    lifecycle.finish_task_attempt(
        binding=binding,
        result=result,
        exception=None,
        retry_planned=False,
        runtime_status=statuses[0],
    )
    final_path = lifecycle.finalize()
    assert final_path is not None

    final = json.loads(final_path.read_text(encoding="utf-8"))
    task_ended = _event(_events(binding.task_recorder), "task_ended")["payload"]
    assert task_ended["runtime_status"] == "aborted"
    assert final["task_streams"][0]["runtime_status"] == "aborted"


@pytest.mark.parametrize("failure_point", ["action_dump", "token_usage", "trajectory"])
def test_local_runner_failure_after_decision_closes_nonexecution(
    tmp_path: Path,
    failure_point: str,
) -> None:
    error = RuntimeError(f"exact {failure_point} failure")
    calls: list[Any] = []

    if failure_point == "action_dump":

        class FailingSecondDumpAction:
            action_type = "click"

            def __init__(self) -> None:
                self.dump_count = 0

            def model_dump(self, **_: Any) -> dict[str, Any]:
                self.dump_count += 1
                if self.dump_count > 1:
                    raise error
                return {"action_type": self.action_type}

        action: Any = FailingSecondDumpAction()
    else:
        action = JSONAction(action_type="click", x=7, y=8)

    env = _FakeEnv(calls)
    agent = _FakeAgent(calls, [("prediction", action)])
    traj = _FakeTrajLogger(calls)
    if failure_point == "token_usage":

        def fail_token_usage() -> dict[str, int]:
            raise error

        agent.get_total_token_usage = fail_token_usage  # type: ignore[method-assign]
    elif failure_point == "trajectory":

        def fail_trajectory(*_: Any) -> None:
            raise error

        traj.log_traj = fail_trajectory  # type: ignore[method-assign]

    _, task, capture, metadata = _capture(tmp_path)
    with pytest.raises(RuntimeError) as raised:
        _execute_single_task(
            env,
            agent,
            "FixtureTask",
            1,
            traj,
            audit_capture=capture,
            audit_metadata=metadata,
        )

    assert raised.value is error
    events = _events(task)
    assert [event["event_type"] for event in events] == [
        "task_started",
        "step_started",
        "agent_decision",
        "transition_not_executed",
        "task_ended",
    ]
    assert _event(events, "transition_not_executed")["payload"]["reason"] == ("runner_exception")
    assert _event(events, "task_ended")["payload"]["runtime_status"] == "crashed"
    assert not any(call[0] == "execute" for call in calls if isinstance(call, tuple))


def test_prediction_exception_is_recorded_and_same_exception_is_reraised(
    tmp_path: Path,
) -> None:
    error = RuntimeError("exact prediction failure")
    calls: list[Any] = []
    env = _FakeEnv(calls)
    agent = _FakeAgent(calls, [error])
    _, task, capture, metadata = _capture(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        _execute_single_task(
            env,
            agent,
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
        )

    assert raised.value is error
    assert not any(call[0] == "score" for call in calls if isinstance(call, tuple))
    events = _events(task)
    assert _event(events, "agent_decision")["payload"]["parse_outcome"] == "raised"
    assert _event(events, "transition_not_executed")["payload"]["reason"] == (
        "prediction_exception"
    )
    ended = _event(events, "task_ended")["payload"]
    assert ended["runtime_status"] == "crashed"
    assert ended["termination"]["source"] == "prediction_exception"


def test_goal_retrieval_failure_still_starts_and_ends_attempt_before_reraise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = RuntimeError("exact goal retrieval failure")
    calls: list[Any] = []
    env = _FakeEnv(calls, goal_exception=error)
    agent = _FakeAgent(calls, [])
    _, task, capture, metadata = _capture(tmp_path)

    def trace_must_not_start(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("execution trace started before task_started")

    monkeypatch.setattr(
        execution_io_module.ExecutionEvidenceTrace,
        "from_context",
        trace_must_not_start,
    )

    with pytest.raises(RuntimeError) as raised:
        _execute_single_task(
            env,
            agent,
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
        )

    assert raised.value is error
    events = _events(task)
    assert [event["event_type"] for event in events] == ["task_started", "task_ended"]
    assert events[0]["seq"] == 1
    assert events[0]["payload"]["task_goal"] is None
    assert events[0]["payload"]["task_goal_status"] == "retrieval_failed"
    assert events[1]["payload"]["runtime_status"] == "crashed"
    assert events[1]["payload"]["termination"]["source"] == "task_goal_retrieval"


def test_environment_exception_closes_failed_transition_and_reraises_same_object(
    tmp_path: Path,
) -> None:
    error = RuntimeError("exact environment failure")
    calls: list[Any] = []
    env = _FakeEnv(calls, execute_exception=error)
    action = JSONAction(action_type="click", x=5, y=6)
    agent = _FakeAgent(calls, [("prediction", action)])
    _, task, capture, metadata = _capture(tmp_path)

    with pytest.raises(RuntimeError) as raised:
        _execute_single_task(
            env,
            agent,
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
        )

    assert raised.value is error
    transition = _event(_events(task), "transition_failed")["payload"]
    assert transition["exception"]["message"] == "exact environment failure"
    assert transition["duration_ns"] >= 0
    assert transition["post_observation"] is None


def test_evaluation_and_teardown_exceptions_remain_distinct(tmp_path: Path) -> None:
    terminal = _TerminalAction(FINISHED)
    evaluation_error = RuntimeError("exact evaluation failure")
    with pytest.raises(RuntimeError) as evaluation_raised:
        _run(
            tmp_path / "evaluation",
            [("done", terminal)],
            env_kwargs={"evaluation_exception": evaluation_error},
        )
    assert evaluation_raised.value is evaluation_error

    teardown_error = RuntimeError("exact teardown failure")
    calls: list[Any] = []
    env = _FakeEnv(calls, teardown_exception=teardown_error)
    agent = _FakeAgent(calls, [("done", terminal)])
    _, task, capture, metadata = _capture(tmp_path / "teardown")
    with pytest.raises(RuntimeError) as teardown_raised:
        _execute_single_task(
            env,
            agent,
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
        )
    assert teardown_raised.value is teardown_error
    ended = _event(_events(task), "task_ended")["payload"]
    assert ended["environment_evaluation"]["score"] == 0.75
    assert ended["teardown"]["returned"] is False
    assert ended["teardown"]["exception"]["message"] == "exact teardown failure"


def test_trace_setup_failure_occurs_after_task_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_trace_setup(self: Any, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs
        raise RuntimeError("fixture trace setup failure")

    monkeypatch.setattr(
        execution_io_module.ExecutionEvidenceTrace,
        "__init__",
        fail_trace_setup,
    )
    result, _, _, task, capture = _run(
        tmp_path,
        [("done", _TerminalAction(FINISHED))],
    )

    assert result == (1, 0.75)
    events = _events(task)
    assert events[0]["event_type"] == "task_started"
    assert events[0]["seq"] == 1
    assert events[1]["event_type"] == "collector_error"
    assert capture.capture_complete is False


def test_fail_open_missing_step_reference_does_not_change_business_result(
    tmp_path: Path,
) -> None:
    class MissingStepCapture(RunnerTaskCapture):
        def start_step(self, **_: Any) -> None:
            self.mark_incomplete("step_started.observation")
            return None

    action = JSONAction(action_type="click", x=9, y=8)
    result, calls, _, task, capture = _run(
        tmp_path,
        [("prediction", action)],
        capture_type=MissingStepCapture,
    )

    assert result == (1, 0.75)
    assert ("execute", action) in calls
    assert capture.capture_complete is False
    assert _event(_events(task), "task_ended")["payload"]["capture_complete"] is False


def test_disabled_capture_uses_the_unmodified_control_flow(tmp_path: Path) -> None:
    class DisabledCapture:
        enabled = False

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"disabled capture inspected {name}")

    class PoisonMetadata:
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError(f"disabled metadata inspected {name}")

    def run_once(*, disabled: bool) -> tuple[tuple[int, float], list[Any]]:
        calls: list[Any] = []
        action = JSONAction(action_type="click", x=2, y=3)
        kwargs = (
            {"audit_capture": DisabledCapture(), "audit_metadata": PoisonMetadata()}
            if disabled
            else {}
        )
        result = _execute_single_task(
            _FakeEnv(calls),
            _FakeAgent(calls, [("prediction", action)]),
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            **kwargs,
        )
        normalized_calls = [
            (call[0], call[1].model_dump())
            if isinstance(call, tuple) and len(call) == 2 and call[0] == "execute"
            else call
            for call in calls
        ]
        return result, normalized_calls

    assert run_once(disabled=False) == run_once(disabled=True)
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "failure_point",
    [
        "start_task",
        "start_step",
        "record_decision",
        "execution_started",
        "transition_completed",
        "end_task",
    ],
)
def test_capture_hook_fault_matches_audit_off_business_path(
    tmp_path: Path,
    failure_point: str,
) -> None:
    class RaisingHookCapture(RunnerTaskCapture):
        def __getattribute__(self, name: str) -> Any:
            if name == failure_point:

                def fail(*_: Any, **__: Any) -> None:
                    raise OSError(f"collector hook unavailable: {failure_point}")

                return fail
            return super().__getattribute__(name)

    def run_once(*, capture_fault: bool) -> tuple[tuple[int, float], list[Any]]:
        calls: list[Any] = []
        action = JSONAction(action_type="click", x=2, y=3)
        kwargs: dict[str, Any] = {}
        if capture_fault:
            _, _, capture, metadata = _capture(
                tmp_path / failure_point,
                capture_type=RaisingHookCapture,
            )
            kwargs = {"audit_capture": capture, "audit_metadata": metadata}
        result = _execute_single_task(
            _FakeEnv(calls),
            _FakeAgent(calls, [("prediction", action)]),
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            **kwargs,
        )
        business_call_order = [call if isinstance(call, str) else call[0] for call in calls]
        return result, business_call_order

    assert run_once(capture_fault=False) == run_once(capture_fault=True)


def test_runtime_status_callback_fault_does_not_change_business_path(tmp_path: Path) -> None:
    def run_once(*, callback_fault: bool) -> tuple[tuple[int, float], list[Any]]:
        calls: list[Any] = []
        _, _, capture, metadata = _capture(tmp_path / str(callback_fault))

        def callback(_: str) -> None:
            if callback_fault:
                raise OSError("collector status callback unavailable")

        result = _execute_single_task(
            _FakeEnv(calls),
            _FakeAgent(
                calls,
                [("prediction", JSONAction(action_type="click", x=2, y=3))],
            ),
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
            audit_runtime_status_callback=callback,
        )
        return result, [call if isinstance(call, str) else call[0] for call in calls]

    assert run_once(callback_fault=False) == run_once(callback_fault=True)


@pytest.mark.parametrize(
    ("scenario", "failure_point"),
    [
        ("prediction", "transition_not_executed"),
        ("execution", "transition_failed"),
    ],
)
def test_terminal_capture_fault_never_masks_business_exception(
    tmp_path: Path,
    scenario: str,
    failure_point: str,
) -> None:
    original = RuntimeError(f"exact {scenario} business failure")

    class RaisingTerminalCapture(RunnerTaskCapture):
        def __getattribute__(self, name: str) -> Any:
            if name == failure_point:

                def fail(*_: Any, **__: Any) -> None:
                    raise OSError(f"collector terminal unavailable: {failure_point}")

                return fail
            return super().__getattribute__(name)

    calls: list[Any] = []
    env = _FakeEnv(
        calls,
        execute_exception=(original if scenario == "execution" else None),
    )
    outcomes = (
        [original]
        if scenario == "prediction"
        else [("prediction", JSONAction(action_type="click", x=2, y=3))]
    )
    _, _, capture, metadata = _capture(
        tmp_path,
        capture_type=RaisingTerminalCapture,
    )

    with pytest.raises(RuntimeError) as raised:
        _execute_single_task(
            env,
            _FakeAgent(calls, outcomes),
            "FixtureTask",
            1,
            _FakeTrajLogger(calls),
            audit_capture=capture,
            audit_metadata=metadata,
        )

    assert raised.value is original


def test_process_lifecycle_indices_cover_device_retry_and_outer_reschedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    first_error = RuntimeError("Device is not healthy: fixture")
    second_error = RuntimeError("ordinary first-round failure")

    class FakeProcessEnv:
        base_url = "http://fixture.invalid"

    env = FakeProcessEnv()
    agent = object()

    class FakeProcessTraj:
        def __init__(self, *_: Any) -> None:
            calls.append("traj.init")

        def reset_traj(self) -> None:
            calls.append("traj.reset")

    class FakeLifecycle:
        enabled = True

        def __init__(self) -> None:
            self.started: list[dict[str, Any]] = []
            self.finished: list[dict[str, Any]] = []
            self.attempt_count = 0

        def start_task_attempt(self, **kwargs: Any) -> Any:
            self.started.append(kwargs)
            self.attempt_count = max(
                self.attempt_count + 1,
                kwargs["whole_task_attempt_index"],
            )
            calls.append(("start", self.attempt_count))
            return SimpleNamespace(
                capture=SimpleNamespace(enabled=True),
                metadata=SimpleNamespace(
                    task_run_id=f"attempt-{self.attempt_count}",
                    whole_task_attempt_index=self.attempt_count,
                ),
            )

        def finish_task_attempt(self, **kwargs: Any) -> None:
            self.finished.append(kwargs)
            calls.append(("finish", kwargs["retry_planned"]))

    lifecycle = FakeLifecycle()
    outcomes: list[Any] = [first_error, second_error, (3, 0.5)]

    def fake_execute(*args: Any, **kwargs: Any) -> tuple[int, float]:
        calls.append(("execute_task", kwargs["audit_metadata"].task_run_id))
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        kwargs["audit_runtime_status_callback"]("completed")
        return outcome

    monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
    monkeypatch.setattr(runner_module, "create_agent", lambda *args, **kwargs: agent)
    monkeypatch.setattr(runner_module, "_execute_single_task", fake_execute)
    monkeypatch.setattr(
        runner_module.time, "sleep", lambda seconds: calls.append(("sleep", seconds))
    )
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(
        runner_module.logger, "remove", lambda handler: calls.append(("remove", handler))
    )

    env_queue: Queue[tuple[Any, str]] = Queue()
    env_queue.put((env, "fixture-container"))
    first_round_result = _process_task_on_env(
        task_name="FixtureTask",
        env_queue=env_queue,
        agent_type="fixture",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        api_key=None,
        log_file_root=str(tmp_path),
        max_step=4,
        retry_on_device_unhealthy=1,
        audit_lifecycle=lifecycle,
        audit_task_index=9,
        audit_suite_family="fixture-suite",
    )
    second_round_result = _process_task_on_env(
        task_name="FixtureTask",
        env_queue=env_queue,
        agent_type="fixture",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        api_key=None,
        log_file_root=str(tmp_path),
        max_step=4,
        retry_on_device_unhealthy=1,
        audit_lifecycle=lifecycle,
        audit_task_index=9,
        audit_suite_family="fixture-suite",
    )

    assert first_round_result is None
    assert second_round_result == {"task_name": "FixtureTask", "score": 0.5}
    assert [item["whole_task_attempt_index"] for item in lifecycle.started] == [
        1,
        2,
        1,
    ]
    assert [item["binding"].metadata.whole_task_attempt_index for item in lifecycle.finished] == [
        1,
        2,
        3,
    ]
    assert all(item["task_index"] == 9 for item in lifecycle.started)
    assert all(item["suite_family"] == "fixture-suite" for item in lifecycle.started)
    assert lifecycle.finished[0]["exception"] is first_error
    assert lifecycle.finished[0]["retry_planned"] is True
    assert lifecycle.finished[0]["result"] is None
    assert lifecycle.finished[0]["runtime_status"] == "crashed"
    assert lifecycle.finished[1]["exception"] is second_error
    assert lifecycle.finished[1]["retry_planned"] is False
    assert lifecycle.finished[1]["runtime_status"] == "crashed"
    assert lifecycle.finished[2]["exception"] is None
    assert lifecycle.finished[2]["result"] == (3, 0.5)
    assert lifecycle.finished[2]["runtime_status"] == "completed"
    assert calls.index(("finish", True)) < calls.index(("sleep", 20))
    assert env_queue.get_nowait() == (env, "fixture-container")


@pytest.mark.parametrize("failure_point", ["start_task_attempt", "finish_task_attempt"])
def test_lifecycle_hook_fault_does_not_change_result_or_trigger_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    calls: list[Any] = []

    class FakeProcessEnv:
        base_url = "http://fixture.invalid"

    env = FakeProcessEnv()

    class FakeProcessTraj:
        def __init__(self, *_: Any) -> None:
            calls.append("traj.init")

        def reset_traj(self) -> None:
            calls.append("traj.reset")

    class FakeLifecycle:
        enabled = True

        def start_task_attempt(self, **_: Any) -> Any:
            calls.append("audit.start")
            if failure_point == "start_task_attempt":
                raise OSError("collector start unavailable")
            return SimpleNamespace(
                capture=SimpleNamespace(enabled=False),
                metadata=None,
            )

        def finish_task_attempt(self, **_: Any) -> None:
            calls.append("audit.finish")
            if failure_point == "finish_task_attempt":
                raise OSError("collector finish unavailable")

    lifecycle = FakeLifecycle()

    def execute_once(*_: Any, **__: Any) -> tuple[int, float]:
        calls.append("execute")
        return 2, 0.5

    monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
    monkeypatch.setattr(runner_module, "create_agent", lambda *args, **kwargs: object())
    monkeypatch.setattr(runner_module, "_execute_single_task", execute_once)
    monkeypatch.setattr(
        runner_module.time, "sleep", lambda seconds: calls.append(("sleep", seconds))
    )
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(runner_module.logger, "remove", lambda handler: None)
    env_queue: Queue[tuple[Any, str]] = Queue()
    env_queue.put((env, "fixture-container"))

    result = _process_task_on_env(
        task_name="FixtureTask",
        env_queue=env_queue,
        agent_type="fixture",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        api_key=None,
        log_file_root=str(tmp_path),
        max_step=1,
        retry_on_device_unhealthy=3,
        audit_lifecycle=lifecycle,
    )

    assert result == {"task_name": "FixtureTask", "score": 0.5}
    assert calls.count("execute") == 1
    assert not any(isinstance(call, tuple) and call[0] == "sleep" for call in calls)
    assert env_queue.get_nowait() == (env, "fixture-container")


def test_enabled_mcp_reset_failure_gets_closed_task_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    error = RuntimeError("exact reset failure")

    class FakeMCPEnv:
        base_url = "http://fixture.invalid"

        def reset_tools(self, *, task_type: str) -> None:
            calls.append(("reset_tools", task_type))
            raise error

    class FakeProcessTraj:
        def __init__(self, *_: Any) -> None:
            calls.append("traj.init")

    def create_must_not_run(*_: Any, **__: Any) -> Any:
        raise AssertionError("agent construction ran after reset failure")

    monkeypatch.setattr(runner_module, "AndroidMCPEnvClient", FakeMCPEnv)
    monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
    monkeypatch.setattr(runner_module, "create_agent", create_must_not_run)
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(runner_module.logger, "remove", lambda handler: None)
    lifecycle = _audit_lifecycle(tmp_path)
    env = FakeMCPEnv()
    env_queue: Queue[tuple[Any, str]] = Queue()
    env_queue.put((env, "fixture-container"))

    result = _process_task_on_env(
        task_name="FixtureTask",
        env_queue=env_queue,
        agent_type="fixture-agent",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        api_key=None,
        log_file_root=str(tmp_path / "traj"),
        max_step=1,
        enable_mcp=True,
        audit_lifecycle=lifecycle,
    )

    assert result is None
    assert env_queue.get_nowait() == (env, "fixture-container")
    events = _only_task_events(lifecycle)
    assert [event["event_type"] for event in events] == ["task_started", "task_ended"]
    assert events[0]["payload"]["task_goal"] is None
    assert events[0]["payload"]["task_goal_status"] == "retrieval_failed"
    assert events[0]["payload"]["agent"]["adapter"] == "fixture-agent"
    assert events[1]["payload"]["runtime_status"] == "crashed"
    assert events[1]["payload"]["termination"]["source"] == "mcp_reset_tools"
    assert events[1]["payload"]["termination"]["exception"]["message"] == str(error)
    assert lifecycle.finalize() is not None
    report = check_run_integrity(lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_enabled_agent_construction_failure_gets_closed_task_stream_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    error = RuntimeError("exact agent construction failure")

    class FakeProcessEnv:
        base_url = "http://fixture.invalid"

    class FakeProcessTraj:
        def __init__(self, *_: Any) -> None:
            calls.append("traj.init")

    def fail_create(*_: Any, **__: Any) -> Any:
        calls.append("create_agent")
        raise error

    monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
    monkeypatch.setattr(runner_module, "create_agent", fail_create)
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(runner_module.logger, "remove", lambda handler: None)
    lifecycle = _audit_lifecycle(tmp_path)
    env = FakeProcessEnv()
    env_queue: Queue[tuple[Any, str]] = Queue()
    env_queue.put((env, "fixture-container"))

    with pytest.raises(RuntimeError) as raised:
        _process_task_on_env(
            task_name="FixtureTask",
            env_queue=env_queue,
            agent_type="fixture-agent",
            model_name="fixture-model",
            llm_base_url="http://model.invalid",
            api_key=None,
            log_file_root=str(tmp_path / "traj"),
            max_step=1,
            audit_lifecycle=lifecycle,
        )

    assert raised.value is error
    assert env_queue.get_nowait() == (env, "fixture-container")
    events = _only_task_events(lifecycle)
    assert [event["event_type"] for event in events] == ["task_started", "task_ended"]
    assert events[0]["payload"]["task_goal"] is None
    assert events[1]["payload"]["termination"]["source"] == "agent_construction"
    assert events[1]["payload"]["termination"]["exception"]["message"] == str(error)
    assert lifecycle.finalize() is not None
    report = check_run_integrity(lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_enabled_tool_logging_failure_gets_closed_task_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    error = RuntimeError("exact tool logging failure")

    class FakeMCPEnv:
        base_url = "http://fixture.invalid"
        tools = [{"name": "fixture_tool"}]

        def reset_tools(self, *, task_type: str) -> None:
            calls.append(("reset_tools", task_type))

        def get_task_goal(self, *, task_type: str) -> str:
            raise AssertionError(f"goal retrieval ran after tool log failure: {task_type}")

    class FakeProcessTraj:
        def __init__(self, *_: Any) -> None:
            calls.append("traj.init")

        def log_tools(self, tools: Any) -> None:
            calls.append(("log_tools", tools))
            raise error

    monkeypatch.setattr(runner_module, "AndroidMCPEnvClient", FakeMCPEnv)
    monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
    monkeypatch.setattr(
        runner_module,
        "create_agent",
        lambda *args, **kwargs: _FakeAgent(calls, []),
    )
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(runner_module.logger, "remove", lambda handler: None)
    lifecycle = _audit_lifecycle(tmp_path)
    env = FakeMCPEnv()
    env_queue: Queue[tuple[Any, str]] = Queue()
    env_queue.put((env, "fixture-container"))

    result = _process_task_on_env(
        task_name="FixtureTask",
        env_queue=env_queue,
        agent_type="fixture-agent",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        api_key=None,
        log_file_root=str(tmp_path / "traj"),
        max_step=1,
        enable_mcp=True,
        audit_lifecycle=lifecycle,
    )

    assert result is None
    assert env_queue.get_nowait() == (env, "fixture-container")
    events = _only_task_events(lifecycle)
    assert [event["event_type"] for event in events] == ["task_started", "task_ended"]
    assert events[0]["payload"]["task_goal"] is None
    assert events[1]["payload"]["termination"]["source"] == "trajectory_tool_logging"
    assert events[1]["payload"]["termination"]["exception"]["message"] == str(error)
    assert lifecycle.finalize() is not None
    report = check_run_integrity(lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


@pytest.mark.parametrize("failure_stage", ["reset", "create", "log_tools"])
def test_feature_off_setup_failure_order_and_behavior_are_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    monkeypatch.setattr(runner_module.logger, "add", lambda *args, **kwargs: 101)
    monkeypatch.setattr(runner_module.logger, "remove", lambda handler: None)

    def run_once(audit_lifecycle: Any) -> tuple[str, Any, list[str]]:
        calls: list[str] = []
        error = RuntimeError(f"exact disabled {failure_stage} failure")

        class FakeMCPEnv:
            base_url = "http://fixture.invalid"
            tools = []

            def reset_tools(self, *, task_type: str) -> None:
                del task_type
                calls.append("reset")
                if failure_stage == "reset":
                    raise error

        class FakeProcessTraj:
            def __init__(self, *_: Any) -> None:
                calls.append("traj.init")

            def log_tools(self, tools: Any) -> None:
                del tools
                calls.append("log_tools")
                if failure_stage == "log_tools":
                    raise error

        def create(*_: Any, **__: Any) -> object:
            calls.append("create")
            if failure_stage == "create":
                raise error
            return object()

        monkeypatch.setattr(runner_module, "AndroidMCPEnvClient", FakeMCPEnv)
        monkeypatch.setattr(runner_module, "TrajLogger", FakeProcessTraj)
        monkeypatch.setattr(runner_module, "create_agent", create)
        env = FakeMCPEnv()
        env_queue: Queue[tuple[Any, str]] = Queue()
        env_queue.put((env, "fixture-container"))
        try:
            result = _process_task_on_env(
                task_name="FixtureTask",
                env_queue=env_queue,
                agent_type="fixture-agent",
                model_name="fixture-model",
                llm_base_url="http://model.invalid",
                api_key=None,
                log_file_root=str(tmp_path / failure_stage),
                max_step=1,
                enable_mcp=True,
                audit_lifecycle=audit_lifecycle,
            )
        except Exception as raised:
            assert raised is error
            outcome: tuple[str, Any, list[str]] = ("raised_same", type(raised), calls)
        else:
            outcome = ("returned", result, calls)
        assert env_queue.get_nowait() == (env, "fixture-container")
        return outcome

    assert run_once(None) == run_once(SimpleNamespace(enabled=False))
    assert not list(tmp_path.rglob("events.jsonl"))


def test_run_uses_stable_original_task_indices_after_shuffle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeLifecycle:
        enabled = True

    seen: list[tuple[str, int]] = []
    scan_results = iter(
        [
            ([], []),
            (["TaskB", "TaskA"], [0.2, 0.1]),
        ]
    )

    monkeypatch.setattr(runner_module, "_init_env", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        runner_module, "scan_finished_tasks", lambda *args, **kwargs: next(scan_results)
    )
    monkeypatch.setattr(runner_module.random, "shuffle", lambda values: values.reverse())
    monkeypatch.setattr(
        runner_module,
        "_process_task_on_env",
        lambda **kwargs: (
            seen.append((kwargs["task_name"], kwargs["audit_task_index"]))
            or {"task_name": kwargs["task_name"], "score": 1.0}
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "delayed",
        lambda function: lambda *args, **kwargs: lambda: function(*args, **kwargs),
    )

    class ImmediateParallel:
        def __init__(self, **_: Any) -> None:
            pass

        def __call__(self, jobs: Any) -> list[Any]:
            return [job() for job in jobs]

    monkeypatch.setattr(runner_module, "Parallel", ImmediateParallel)

    result, missing = runner_module.run_agent_with_evaluation(
        agent_type="fixture",
        model_name="fixture-model",
        llm_base_url="http://model.invalid",
        log_file_root=str(tmp_path),
        tasks=["TaskB", "TaskA"],
        aw_urls=["http://env.invalid"],
        shuffle_tasks=True,
        auto_retry=0,
        audit_lifecycle=FakeLifecycle(),
    )

    assert seen == [("TaskA", 2), ("TaskB", 1)]
    assert result == [
        {"task_name": "TaskB", "score": 0.2},
        {"task_name": "TaskA", "score": 0.1},
    ]
    assert missing == []
