from __future__ import annotations

import builtins
import io
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.core import runner as runner_module
from mobile_world.runtime.audit.config import AuditConfig
from mobile_world.runtime.audit.execution_io import (
    record_gui_request,
    record_gui_response,
    record_screenshot_source,
)
from mobile_world.runtime.audit.integrity import check_run_integrity
from mobile_world.runtime.audit.lifecycle import bootstrap_audit_run
from mobile_world.runtime.utils.models import FINISHED, UNKNOWN, JSONAction


class _Response:
    status_code = 200
    content = b'{"status":"ok"}'
    headers = {"content-type": "application/json"}


class _Env:
    base_url = "http://fixture.invalid"

    def __init__(self) -> None:
        self.image = Image.new("RGB", (4, 4), (10, 20, 30))
        output = io.BytesIO()
        self.image.save(output, format="PNG")
        self.png = output.getvalue()
        self.executed: list[JSONAction] = []
        self.observations: list[Any] = []

    def get_task_goal(self, *, task_type: str) -> str:
        return "Open the fixture item."

    def _observation(self) -> SimpleNamespace:
        record_screenshot_source(self.image, self.png)
        observation = SimpleNamespace(screenshot=self.image, tool_call=None, ask_user_response=None)
        self.observations.append(observation)
        return observation

    def initialize_task(self, *, task_name: str) -> SimpleNamespace:
        return self._observation()

    def execute_action(self, action: JSONAction) -> SimpleNamespace:
        self.executed.append(action)
        record_gui_request(
            {"device": "fixture", "action": action.model_dump()},
            request_endpoint=f"{self.base_url}/step",
        )
        record_gui_response(_Response())
        return self._observation()

    def get_task_score(self, *, task_type: str) -> tuple[float, str]:
        return 0.0, "fixture score"

    def tear_down_task(self, *, task_type: str) -> dict[str, str]:
        return {"status": "success"}


class _Agent:
    model_name = "fixture-model"

    def __init__(self, actions: list[JSONAction]) -> None:
        self.actions = list(actions)
        self.received: list[dict[str, Any]] = []

    def initialize(self, goal: str) -> None:
        self.goal = goal

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        self.received.append(dict(observation))
        return "fixture prediction", self.actions.pop(0)

    def get_total_token_usage(self) -> dict[str, int]:
        return {"input_tokens": 0, "output_tokens": 0}

    def done(self) -> None:
        pass


class _Traj:
    def log_traj(self, *_args: Any) -> None:
        pass

    def log_score(self, **_kwargs: Any) -> None:
        pass


def _clicks(count: int) -> list[JSONAction]:
    return [JSONAction(action_type="click", x=1, y=2) for _ in range(count)]


def _lifecycle(root: Path) -> Any:
    return bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=root / "audit"),
        agent_type="qwen3vl",
        model_name="fixture-model",
        sync=False,
    )


def _run(
    root: Path,
    *,
    mode: str,
    actions: list[JSONAction] | None = None,
    lifecycle: Any = None,
    audited: bool = True,
    incomplete: bool = False,
    whole_task_attempt_index: int = 1,
    retry_planned: bool = False,
    agent: Any = None,
    ui_tree: bool = False,
    env: Any = None,
) -> SimpleNamespace:
    actions = _clicks(4) if actions is None else actions
    env = _Env() if env is None else env
    agent = _Agent(actions) if agent is None else agent
    owns_lifecycle = audited and lifecycle is None
    if owns_lifecycle:
        lifecycle = _lifecycle(root)
    binding = None
    if audited:
        binding = lifecycle.start_task_attempt(
            task_name="FixtureTask",
            task_index=1,
            suite_family="mobile_world",
            agent=agent,
            environment=env,
            whole_task_attempt_index=whole_task_attempt_index,
        )
        assert binding is not None
        if incomplete:
            binding.capture.mark_incomplete("fixture_missing_observation")
    result = runner_module._execute_single_task(
        env,
        agent,
        "FixtureTask",
        len(actions),
        _Traj(),
        audit_capture=binding.capture if binding is not None else None,
        audit_metadata=binding.metadata if binding is not None else None,
        gui_ledger_mode=mode,
        gui_ledger_ui_tree=ui_tree,
    )
    if binding is not None:
        lifecycle.finish_task_attempt(
            binding=binding,
            result=result,
            exception=None,
            retry_planned=retry_planned,
            runtime_status="completed",
        )
    if owns_lifecycle:
        lifecycle.finalize()
    return SimpleNamespace(
        result=result, env=env, agent=agent, lifecycle=lifecycle, binding=binding, actions=actions
    )


def _events(run: SimpleNamespace) -> list[dict[str, Any]]:
    return [json.loads(line) for line in run.binding.task_recorder.path.read_text().splitlines()]


def _derived(run: SimpleNamespace) -> list[dict[str, Any]]:
    directory = (
        run.lifecycle.recorder.run_root / "gui_ledger" / run.binding.task_recorder.path.parent.name
    )
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def test_full_nudges_third_attempt_but_executes_every_original_action(tmp_path: Path) -> None:
    run = _run(tmp_path, mode="full")

    assert run.result == (4, 0.0)
    assert len(run.env.executed) == 4
    assert all(actual is proposed for actual, proposed in zip(run.env.executed, run.actions))
    assert all(
        action.model_dump(exclude_none=True) == {"action_type": "click", "x": 1, "y": 2}
        for action in run.env.executed
    )
    assert len(run.agent.received) == 4
    assert all("ledger_inform" in observation for observation in run.agent.received)
    assert all("ledger_nudge" not in observation for observation in run.agent.received[:3])
    assert (
        "this action matches 2 consecutive prior executions"
        in run.agent.received[3]["ledger_nudge"]
    )
    assert "Attempt step 3: click" in run.agent.received[3]["ledger_inform"]
    assert [record["decision"] for record in _derived(run)] == ["ALLOW", "ALLOW", "NUDGE", "ALLOW"]
    assert _derived(run)[2]["nudge_count"] == 1

    # Environment evidence remains separate from temporary Inform and derived Nudge.
    assert all(
        set(vars(observation)) == {"screenshot", "tool_call", "ask_user_response"}
        for observation in run.env.observations
    )
    raw = json.dumps(_events(run))
    assert "ledger_inform" not in raw
    assert "ledger_nudge" not in raw
    assert "GUI Ledger" not in raw
    assert run.binding.capture.capture_complete is True
    report = check_run_integrity(run.lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_real_qwen_provider_boundary_uses_only_four_actor_calls_and_complete_audit(
    tmp_path: Path,
) -> None:
    from openai.types.chat import ChatCompletion

    from mobile_world.agents.base import BaseAgent
    from mobile_world.agents.implementations.qwen3vl import Qwen3VLAgentMCP

    provider_requests: list[dict[str, Any]] = []
    response_text = (
        'Thought: Check the screen.\nAction: "Tap the button"\n'
        '<tool_call>{"name":"mobile_use","arguments":'
        '{"action":"click","coordinate":[250,500]}}</tool_call>'
    )

    def completion(**kwargs: Any) -> ChatCompletion:
        provider_requests.append(deepcopy(kwargs))
        return ChatCompletion(
            id=f"fixture-{len(provider_requests)}",
            object="chat.completion",
            created=0,
            model="fixture-model",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": response_text},
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    agent = Qwen3VLAgentMCP.__new__(Qwen3VLAgentMCP)
    BaseAgent.__init__(agent)
    agent.model_name = "fixture-model"
    agent.llm_base_url = "http://fixture.invalid/v1"
    agent.runtime_conf = {"temperature": 0.0}
    agent.tools = []
    agent.reset()
    agent.openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
        base_url="http://fixture.invalid/v1",
        api_key="empty",
        max_retries=0,
    )
    run = _run(tmp_path, mode="full", agent=agent)

    assert len(provider_requests) == len(run.env.executed) == 4
    assert all(request["model"] == "fixture-model" for request in provider_requests)
    for step, request in enumerate(provider_requests, start=1):
        content = request["messages"][-1]["content"]
        assert [block["type"] for block in content] == ["text", "image_url", "text"]
        assert content[-1]["text"].startswith("[GUI Ledger: current execution state]")
        assert f"at attempt step {step}." in content[-1]["text"]
        assert "[GUI Ledger: current execution state]" not in content[0]["text"]
    assert "Execution observation: GUI Ledger:" not in repr(provider_requests[:3])
    assert (
        "Execution observation: GUI Ledger:"
        in provider_requests[3]["messages"][-1]["content"][0]["text"]
    )
    assert [record["decision"] for record in _derived(run)] == ["ALLOW", "ALLOW", "NUDGE", "ALLOW"]
    assert run.binding.capture.capture_complete is True
    events = _events(run)
    assert sum(event["event_type"] == "model_request" for event in events) == 4
    assert sum(event["event_type"] == "model_response" for event in events) == 4
    assert "GUI Ledger" not in json.dumps(
        [event for event in events if event["event_type"] == "step_started"]
    )
    report = check_run_integrity(run.lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_inform_mode_observes_repeats_without_govern_notices(tmp_path: Path) -> None:
    run = _run(tmp_path, mode="inform")

    assert len(run.env.executed) == 4
    assert all("ledger_inform" in observation for observation in run.agent.received)
    assert all("ledger_nudge" not in observation for observation in run.agent.received)
    assert all(
        record["decision"] == "ALLOW" and record["nudge_count"] == 0 for record in _derived(run)
    )


@pytest.mark.parametrize("mode", ["off", "inform", "full"])
def test_ledger_does_not_repair_or_suppress_original_unrecognized_action(
    tmp_path: Path, mode: str
) -> None:
    # The original Qwen converter returns {} for an unrecognized mobile_use
    # action. Its JSONAction has no action_type; Ledger must not invent a wait,
    # reject it, or change what the existing executor receives.
    actions = [JSONAction() for _ in range(4)]
    run = _run(tmp_path, mode=mode, actions=actions)

    assert run.result == (4, 0.0)
    assert len(run.env.executed) == len(actions)
    assert all(actual is proposed for actual, proposed in zip(run.env.executed, actions))
    assert all(action.action_type is None for action in run.env.executed)
    assert all("ledger_nudge" not in observation for observation in run.agent.received)
    if mode != "off":
        records = _derived(run)
        assert len(records) == len(actions)
        assert all(record["decision"] == "ALLOW" for record in records)


def test_off_preserves_observations_without_importing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__
    ledger_imports: list[str] = []

    def checked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "mobile_world.runtime.gui_ledger":
            ledger_imports.append(name)
            raise AssertionError("default-off imported GUI Ledger")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    run = _run(tmp_path, mode="off")

    assert len(run.env.executed) == 4
    assert ledger_imports == []
    assert all(
        set(observation) == {"screenshot", "tool_call", "ask_user_response"}
        for observation in run.agent.received
    )
    assert not (run.lifecycle.recorder.run_root / "gui_ledger").exists()


@pytest.mark.parametrize("audited,incomplete", [(False, False), (True, True)])
def test_no_usable_audit_disables_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audited: bool, incomplete: bool
) -> None:
    from mobile_world.runtime import gui_ledger

    attempts: list[bool] = []

    def reject_ledger(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append(True)
        raise AssertionError("unusable audit must not initialize Ledger")

    monkeypatch.setattr(gui_ledger, "GuiLedger", reject_ledger)
    run = _run(tmp_path, mode="full", audited=audited, incomplete=incomplete)

    assert len(run.env.executed) == 4
    assert attempts == []
    assert all(
        "ledger_inform" not in observation and "ledger_nudge" not in observation
        for observation in run.agent.received
    )
    if run.lifecycle is not None:
        assert not (run.lifecycle.recorder.run_root / "gui_ledger").exists()


@pytest.mark.parametrize(
    "failure_point", ["__init__", "observe", "render_inform", "govern", "record_transition"]
)
def test_ledger_failure_preserves_execution_and_disables_later_hints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    from mobile_world.runtime.gui_ledger import GuiLedger

    failures: list[str] = []

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        failures.append(failure_point)
        raise RuntimeError("fixture ledger failure")

    monkeypatch.setattr(GuiLedger, failure_point, fail)
    run = _run(tmp_path, mode="full")

    assert run.result == (4, 0.0)
    assert len(run.env.executed) == 4
    assert failures == [failure_point]
    assert all(
        "ledger_inform" not in observation and "ledger_nudge" not in observation
        for observation in run.agent.received[1:]
    )
    assert run.binding.capture.capture_complete is True


def test_derived_log_failure_does_not_stop_execution_or_future_nudges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = runner_module.os.open

    def fail_ledger_output(path: Any, *args: Any, **kwargs: Any) -> Any:
        if "gui_ledger" in Path(path).parts:
            raise OSError("fixture derived log failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(runner_module.os, "open", fail_ledger_output)
    run = _run(tmp_path, mode="full")

    assert len(run.env.executed) == 4
    assert "ledger_nudge" in run.agent.received[3]
    assert run.binding.capture.capture_complete is True
    assert _derived(run) == []


@pytest.mark.parametrize("fault", ["record_transition", "derived_log"])
def test_execution_exception_survives_ledger_failure_without_delivering_a_nudge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from mobile_world.runtime.gui_ledger import GuiLedger

    original_error = RuntimeError("original third-action execution error")
    lifecycle = _lifecycle(tmp_path)
    env = _Env()
    actions = _clicks(4)
    agent = _Agent(actions)
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent=agent,
        environment=env,
        whole_task_attempt_index=1,
    )
    assert binding is not None
    original_execute = env.execute_action

    def fail_third(action: JSONAction) -> SimpleNamespace:
        if len(env.executed) == 2:
            env.executed.append(action)
            record_gui_request(
                {"device": "fixture", "action": action.model_dump()},
                request_endpoint=f"{env.base_url}/step",
            )
            raise original_error
        return original_execute(action)

    monkeypatch.setattr(env, "execute_action", fail_third)
    fault_calls: list[str] = []
    if fault == "record_transition":
        original_record = GuiLedger.record_transition

        def fail_record(self: Any, *args: Any, **kwargs: Any) -> Any:
            if kwargs["outcome"] == "raised":
                fault_calls.append(fault)
                raise OSError("derived failure must not replace original exception")
            return original_record(self, *args, **kwargs)

        monkeypatch.setattr(GuiLedger, "record_transition", fail_record)
    else:
        original_write = runner_module._write_gui_ledger_step

        def fail_write(capture: Any, ledger: Any, mode: str, decision: Any) -> None:
            if ledger.summary()["last_outcome"] == "raised":
                fault_calls.append(fault)
                raise OSError("derived failure must not replace original exception")
            original_write(capture, ledger, mode, decision)

        monkeypatch.setattr(runner_module, "_write_gui_ledger_step", fail_write)

    with pytest.raises(RuntimeError) as caught:
        runner_module._execute_single_task(
            env,
            agent,
            "FixtureTask",
            4,
            _Traj(),
            audit_capture=binding.capture,
            audit_metadata=binding.metadata,
            gui_ledger_mode="full",
        )

    assert caught.value is original_error
    assert fault_calls == [fault]
    assert len(env.executed) == len(agent.received) == 3
    assert all(actual is proposed for actual, proposed in zip(env.executed, actions[:3]))
    assert all("ledger_nudge" not in observation for observation in agent.received)
    lifecycle.finish_task_attempt(
        binding=binding,
        result=None,
        exception=original_error,
        retry_planned=False,
        runtime_status="crashed",
    )
    lifecycle.finalize(runtime_status="crashed")
    events = [json.loads(line) for line in binding.task_recorder.path.read_text().splitlines()]
    failed = [event for event in events if event["event_type"] == "transition_failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["exception"]["message"] == str(original_error)
    assert all(
        event["event_type"] != "transition_completed"
        for event in events
        if event["payload"].get("step_id") == failed[0]["payload"]["step_id"]
    )


@pytest.mark.parametrize("break_after", [1, 3])
@pytest.mark.parametrize("ui_tree", [False, True])
def test_mid_task_incomplete_collector_disables_future_hints_without_changing_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, break_after: int, ui_tree: bool
) -> None:
    from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture

    original_complete = RunnerTaskCapture.transition_completed
    completed: list[int] = []

    def complete_then_break(self: Any, *args: Any, **kwargs: Any) -> Any:
        event = original_complete(self, *args, **kwargs)
        completed.append(1)
        if len(completed) == break_after:
            self.mark_incomplete("fixture_evidence_chain_broken")
        return event

    monkeypatch.setattr(RunnerTaskCapture, "transition_completed", complete_then_break)
    env = _UiEnv()
    run = _run(tmp_path, mode="full", env=env, ui_tree=ui_tree)

    assert len(run.env.executed) == len(run.agent.received) == 4
    assert all(actual is proposed for actual, proposed in zip(run.env.executed, run.actions))
    assert "ledger_inform" in run.agent.received[0]
    assert all(
        "ledger_inform" not in observation and "ledger_nudge" not in observation
        for observation in run.agent.received[break_after:]
    )
    assert all("ledger_nudge" not in observation for observation in run.agent.received)
    assert run.binding.capture.capture_complete is False
    assert env.tree_calls == (1 + break_after if ui_tree else 0)


@pytest.mark.parametrize("action_type", [FINISHED, UNKNOWN])
def test_terminal_action_is_not_executed_or_committed_to_ledger(
    tmp_path: Path, action_type: str
) -> None:
    run = _run(tmp_path, mode="full", actions=[JSONAction(action_type=action_type)])

    assert run.result == (1, 0.0)
    assert run.env.executed == []
    assert "ledger_inform" in run.agent.received[0]
    assert _derived(run) == []
    transitions = [
        event for event in _events(run) if event["event_type"] == "transition_not_executed"
    ]
    assert len(transitions) == 1
    assert transitions[0]["payload"]["reason"] == "terminal_action"


def test_whole_task_retry_receives_a_fresh_ledger(tmp_path: Path) -> None:
    lifecycle = _lifecycle(tmp_path)
    first = _run(
        tmp_path,
        mode="full",
        actions=_clicks(3),
        lifecycle=lifecycle,
        whole_task_attempt_index=1,
        retry_planned=True,
    )
    second = _run(
        tmp_path,
        mode="full",
        actions=_clicks(2),
        lifecycle=lifecycle,
        whole_task_attempt_index=2,
    )
    lifecycle.finalize()

    assert first.binding.task_recorder.task_run_id != second.binding.task_recorder.task_run_id
    assert _derived(first)[-1]["decision"] == "NUDGE"
    assert "No recorded execution in this attempt" in second.agent.received[0]["ledger_inform"]
    assert all("ledger_nudge" not in observation for observation in second.agent.received)
    assert [record["nudge_count"] for record in _derived(second)] == [0, 0]
    report = check_run_integrity(lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


class _UiEnv(_Env):
    def __init__(self, *, unavailable: bool = False) -> None:
        super().__init__()
        self.tree_calls = 0
        self.unavailable = unavailable

    def get_ui_tree(self) -> dict[str, Any]:
        self.tree_calls += 1
        if self.unavailable:
            raise TimeoutError("fixture private exception, must not enter hints")
        return {
            "status": "ok",
            "source": "uiautomator",
            "reason": None,
            "xml": (
                '<hierarchy rotation="0"><node package="fixture.app" '
                'resource-id="fixture.app:id/button" class="android.widget.Button" '
                'bounds="[0,0][4,4]" text="PUBLIC_UI_LABEL" content-desc="" '
                'clickable="true" long-clickable="false" focusable="true" '
                'focused="false" enabled="true" checked="false" checkable="false" '
                'selected="false" scrollable="false" password="false" /></hierarchy>'
            ),
        }


@pytest.mark.parametrize("mode", ["off", "inform", "full"])
def test_coordinate_varying_clicks_stay_original_and_notice_is_received_next_step(
    tmp_path: Path, mode: str
) -> None:
    env = _UiEnv()
    actions = [JSONAction(action_type="click", x=x, y=2) for x in range(4)]
    originals = [action.model_dump() for action in actions]
    run = _run(tmp_path, mode=mode, ui_tree=mode != "off", env=env, actions=actions)

    assert len(env.executed) == len(run.agent.received) == 4
    assert all(actual is original for actual, original in zip(env.executed, actions))
    assert [action.model_dump() for action in env.executed] == originals
    assert env.tree_calls == (0 if mode == "off" else 5)
    if mode == "full":
        assert all("ledger_nudge" not in observation for observation in run.agent.received[:3])
        assert "differing coordinates" in run.agent.received[3]["ledger_nudge"]
        assert [record["decision"] for record in _derived(run)] == [
            "ALLOW",
            "ALLOW",
            "NUDGE",
            "ALLOW",
        ]
        assert _derived(run)[2]["reason"] == "REPEATED_CLICK_SAME_UI_CONTROL"
        assert _derived(run)[-1]["nudge_count"] == 1
    else:
        assert all("ledger_nudge" not in observation for observation in run.agent.received)
    if mode == "off":
        assert all("ledger_inform" not in observation for observation in run.agent.received)
    report = check_run_integrity(run.lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_ui_tree_captured_once_per_sample_and_only_reporter_reaches_actor(tmp_path: Path) -> None:
    env = _UiEnv()
    run = _run(tmp_path, mode="full", ui_tree=True, env=env)
    assert env.tree_calls == 5  # initial + four executed actions; next pre reuses post sample
    assert len(env.executed) == len(run.agent.received) == 4
    assert all(actual is original for actual, original in zip(env.executed, run.actions))
    for observation in run.agent.received:
        assert "accessibility_tree" not in observation
        assert '"label":"PUBLIC_UI_LABEL"' in observation["ledger_inform"]
        assert "<hierarchy" not in observation["ledger_inform"]
        assert "fixture.app:id/button" not in observation["ledger_inform"]
    assert all("PUBLIC_UI_LABEL" not in repr(record) for record in _derived(run))
    for record in _derived(run):
        assert "last_ui_target_comparison" not in record
        assert record["ui_rule_format"] == "independent_observations_v1"
        assert record["last_ui_rule_current_sample_status"] == "RECORDED"
        assert record["last_ui_rule_action_target_status"] == "RECORDED"
        assert record["last_ui_rule_scroll_region_status"] == "NOT_APPLICABLE"
        assert all(value is None or type(value) in (str, int) for value in record.values())
    # A new scoped-rule notice still reaches only the NEXT actor decision;
    # every proposed action executes once unchanged and no new model call occurs.
    assert all("ledger_nudge" not in o for o in run.agent.received[:3])
    assert "same uniquely selected control scope" in run.agent.received[3]["ledger_nudge"]
    assert [record["decision"] for record in _derived(run)] == ["ALLOW", "ALLOW", "NUDGE", "ALLOW"]
    assert _derived(run)[2]["reason"] == "REPEATED_ACTION_SAME_UI_OBSERVATION"
    assert _derived(run)[-1]["ui_rule_current_sample_recorded_count"] == 4
    assert all(not hasattr(obs, "accessibility_tree") for obs in env.observations)
    events = _events(run)
    steps = [e["payload"]["observation"] for e in events if e["event_type"] == "step_started"]
    posts = [
        e["payload"]["post_observation"]
        for e in events
        if e["event_type"] == "transition_completed"
    ]
    assert len(steps) == len(posts) == 4
    assert all(o["accessibility_tree"] is not None for o in steps + posts)
    assert all(
        steps[i + 1]["accessibility_tree"] == posts[i]["accessibility_tree"] for i in range(3)
    )
    report = check_run_integrity(run.lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


@pytest.mark.parametrize("mode", ["off", "inform", "full"])
def test_no_ui_tree_io_without_explicit_opt_in(tmp_path: Path, mode: str) -> None:
    env = _UiEnv()
    _run(tmp_path, mode=mode, env=env)
    assert env.tree_calls == 0


@pytest.mark.parametrize("audited,incomplete", [(False, False), (True, True)])
def test_disabled_ledger_never_collects_ui_tree(
    tmp_path: Path, audited: bool, incomplete: bool
) -> None:
    env = _UiEnv()
    run = _run(tmp_path, mode="full", ui_tree=True, env=env, audited=audited, incomplete=incomplete)
    assert env.tree_calls == 0
    assert all("ledger_inform" not in o for o in run.agent.received)


def test_ui_capture_failure_keeps_actions_and_raw_unavailability(tmp_path: Path) -> None:
    env = _UiEnv(unavailable=True)
    run = _run(tmp_path, mode="inform", ui_tree=True, env=env)
    assert len(env.executed) == 4
    assert env.tree_calls == 5
    assert all("ledger_inform" in o for o in run.agent.received)
    assert all("private exception" not in o["ledger_inform"] for o in run.agent.received)
    assert all("last_ui_target_comparison" not in record for record in _derived(run))
    assert all(
        record["last_ui_rule_current_sample_status"] == "UNAVAILABLE" for record in _derived(run)
    )
    assert _derived(run)[-1]["ui_rule_current_sample_unavailable_count"] == 4
    report = check_run_integrity(run.lifecycle.recorder.run_root)
    assert report["valid"] is True, report["errors"]


def test_terminal_action_does_not_acquire_post_tree(tmp_path: Path) -> None:
    env = _UiEnv()
    run = _run(
        tmp_path,
        mode="full",
        ui_tree=True,
        env=env,
        actions=[JSONAction(action_type=FINISHED)],
    )
    assert env.tree_calls == 1
    assert env.executed == []
    assert len(run.agent.received) == 1
