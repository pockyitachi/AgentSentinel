from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import mobile_world.runtime.audit.runner_capture as runner_capture_module
from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.context import ModelCallTrace
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer
from mobile_world.runtime.utils.models import JSONAction

_MATTERMOST_ASK_USER_CASES = (
    (
        "<thinking>\n"
        "Thought: I need to log in to access the channels. I should ask the user for "
        "their credentials to proceed.\n"
        "</thinking>\n"
        "<tool_call>\n"
        '{"name":"mobile_use","arguments":{"action":"ask_user","text":"Please '
        "provide your Mattermost login details: email or username and password. If you "
        "prefer to register a new account, let me know and I’ll use the default "
        'credentials (e.g., username: user, password: user)."}}\n'
        "</tool_call>",
        "Please provide your Mattermost login details: email or username and password. "
        "If you prefer to register a new account, let me know and I’ll use the default "
        "credentials (e.g., username: user, password: user).",
        "Sorry, I can't provide that information.",
    ),
    (
        "<thinking>\n"
        "Thought: I can’t proceed without credentials. I should ask if they want me to "
        "register a new account with default details.\n"
        "</thinking>\n"
        "<tool_call>\n"
        '{"name":"mobile_use","arguments":{"action":"ask_user","text":"No '
        "problem. Would you like me to register a new account with default credentials "
        "(username: user, password: user) on the server http://10.0.2.2:8065? If yes, "
        "please confirm and I’ll create the account and then set up the ‘reading’ "
        'channel, invite everyone, and post a welcome message."}}\n'
        "</tool_call>",
        "No problem. Would you like me to register a new account with default credentials "
        "(username: user, password: user) on the server http://10.0.2.2:8065? If yes, "
        "please confirm and I’ll create the account and then set up the ‘reading’ channel, "
        "invite everyone, and post a welcome message.",
        "Yes, please register a new account with the default credentials and proceed with "
        "setting up the ‘reading’ channel, inviting everyone, and posting a welcome message.",
    ),
)


def _make_capture(
    root: Path,
    *,
    mode: CollectorMode = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER,
    configured_secrets: tuple[str, ...] = (),
) -> tuple[RunRecorder, TaskRecorder, RunnerTaskCapture]:
    recorder = RunRecorder(
        root,
        producer=Producer.local(version="test", worker_id="runner-capture-test"),
        collector_mode=mode,
        sync=False,
    )
    recorder.write_manifest_start({"run_id": recorder.run_id})
    task = recorder.open_task()
    return (
        recorder,
        task,
        RunnerTaskCapture(
            task,
            configured_secrets=configured_secrets,
        ),
    )


def _start_task(capture: RunnerTaskCapture) -> dict[str, Any]:
    event = capture.start_task(
        task_name="FixtureTask",
        task_goal="preserve this exact goal",
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": "fixture", "model": "none", "configuration": {}},
        environment={"backend_id": "fixture-backend", "device_id": "fixture-device"},
        whole_task_attempt_index=1,
    )
    assert event is not None
    return event


def _observation(
    color: tuple[int, int, int],
    *,
    accessibility_tree: Any = None,
    tool_call: Any = None,
    ask_user_response: Any = None,
) -> dict[str, Any]:
    return {
        "screenshot": Image.new("RGB", (3, 2), color),
        "accessibility_tree": accessibility_tree,
        "tool_call": tool_call,
        "ask_user_response": ask_user_response,
    }


def _transport_result(recorder: RunRecorder) -> dict[str, Any]:
    request = recorder.blob_store.put_bytes(b'{"action":"click"}', "application/json")
    response = recorder.blob_store.put_bytes(b'{"status":"ok"}', "application/json")
    return {
        "kind": "gui_transport",
        "request_endpoint": "http://fixture.invalid/step",
        "request_body_snapshot_blob": request,
        "http_status": 200,
        "response_body_blob": response,
        "response_headers": {"content-type": "application/json"},
        "raw_tool_result_blob": None,
        "agent_visible_tool_result": None,
        "ask_user_response": None,
        "exception": None,
    }


def _events(task: TaskRecorder) -> list[dict[str, Any]]:
    return [json.loads(line) for line in task.path.read_text(encoding="utf-8").splitlines()]


def _by_type(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    matches = [event for event in events if event["event_type"] == event_type]
    assert len(matches) == 1
    return matches[0]


def test_disabled_capture_returns_before_ids_serialization_or_recorder_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DisabledRecorder:
        enabled = False

        @property
        def collector_mode(self) -> None:
            raise AssertionError("collector mode was accessed")

        @property
        def blob_store(self) -> None:
            raise AssertionError("blob store was accessed")

        def append_event(self, *_: Any, **__: Any) -> None:
            raise AssertionError("recorder was called")

    class Poison:
        def __getattribute__(self, name: str) -> Any:
            raise AssertionError(f"disabled capture inspected {name}")

    def fail_id_allocation() -> str:
        raise AssertionError("disabled capture allocated an ID")

    monkeypatch.setattr(runner_capture_module, "new_ulid", fail_id_allocation)
    poison = Poison()
    capture = RunnerTaskCapture(DisabledRecorder())

    capture.mark_incomplete("should-not-be-recorded")
    assert (
        capture.start_task(
            task_name="unused",
            task_goal=None,
            task_goal_status="retrieval_failed",
            task_index=1,
            suite_family="unused",
            agent=poison,
            environment=poison,
            whole_task_attempt_index=1,
        )
        is None
    )
    assert capture.start_step(step_index=1, observation=poison) is None
    assert capture.record_decision(prediction=poison, action=poison) is None
    assert capture.execution_started(decision=poison) is None
    assert capture.transition_completed(post_observation=poison, execution=poison) is None
    assert (
        capture.transition_failed(
            exception=RuntimeError("business failure"),
            execution=poison,
            post_observation=poison,
        )
        is None
    )
    assert capture.transition_not_executed(reason="unused", decision=poison) is None
    assert (
        capture.end_task(
            runtime_status="completed",
            termination_source="unused",
            final_step_index=0,
            teardown_result=poison,
            token_usage=poison,
        )
        is None
    )
    assert capture.capture_complete is True
    assert capture.missing_artifacts == []
    assert capture.collector_error_event_ids == []


def test_complete_transition_is_lossless_and_explicitly_causal(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    task_started = _start_task(capture)
    pre_observation = _observation(
        (10, 20, 30),
        accessibility_tree={"root": {"text": "before"}},
        tool_call={"previous": None},
    )
    pre_pixels = pre_observation["screenshot"].tobytes()
    step = capture.start_step(
        step_index=1,
        observation=pre_observation,
        source_screenshot_bytes=b"exact-pre-source",
    )
    assert step is not None and step.step_started_event_id is not None

    model_call_id = new_ulid()
    model_terminal = task.append_event(
        "model_response",
        {"step_id": step.step_id, "model_call_id": model_call_id},
        caused_by_event_id=step.step_started_event_id,
    )
    trace = ModelCallTrace()
    trace.record_terminal(model_call_id, model_terminal["event_id"])

    action = JSONAction(action_type="click", x=11, y=12)
    action_before = copy.deepcopy(action.model_dump(mode="json", exclude_none=False))
    decision = capture.record_decision(
        prediction="exact prediction",
        action=action,
        model_call_trace=trace,
    )
    assert decision is not None and decision.event_id is not None
    assert decision.action is action
    assert decision.action_snapshot == action_before
    mutable_decision_snapshot = decision.action_snapshot
    mutable_decision_snapshot["x"] = 999
    assert decision.action_snapshot == action_before

    execution = capture.execution_started()
    assert execution is not None and execution.event_id is not None
    assert execution.action is action
    mutable_execution_snapshot = execution.action_snapshot
    mutable_execution_snapshot["y"] = 999
    post_observation = _observation(
        (40, 50, 60),
        accessibility_tree={"root": {"text": "after"}},
        tool_call={"result": ["visible", None]},
    )
    post_pixels = post_observation["screenshot"].tobytes()
    transition = capture.transition_completed(
        post_observation=post_observation,
        execution_result=_transport_result(recorder),
        duration_ns=123_456,
        source_screenshot_bytes=b"exact-post-source",
    )
    assert transition is not None
    task_ended = capture.end_task(
        runtime_status="completed",
        termination_source="max_step",
        final_step_index=1,
        score=0.25,
        reason="exact evaluator reason",
        teardown_attempted=True,
        teardown_result={"status": "ok", "message": "exact teardown"},
        token_usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )
    assert task_ended is not None

    events = _events(task)
    assert [event["event_type"] for event in events] == [
        "task_started",
        "step_started",
        "model_response",
        "agent_decision",
        "action_execution_started",
        "transition_completed",
        "task_ended",
    ]
    step_event = _by_type(events, "step_started")
    decision_event = _by_type(events, "agent_decision")
    execution_event = _by_type(events, "action_execution_started")
    transition_event = _by_type(events, "transition_completed")
    end_event = _by_type(events, "task_ended")

    assert step_event["caused_by_event_id"] == task_started["event_id"]
    assert decision_event["caused_by_event_id"] == model_terminal["event_id"]
    assert execution_event["caused_by_event_id"] == decision_event["event_id"]
    assert transition_event["caused_by_event_id"] == execution_event["event_id"]
    assert end_event["caused_by_event_id"] == transition_event["event_id"]
    assert decision_event["payload"]["source_model_call_ids"] == [model_call_id]
    assert decision_event["payload"]["parsed_action"]["value"] == action_before
    assert execution_event["payload"]["action"] == action_before
    assert transition_event["payload"]["action"] == action_before
    assert transition_event["payload"]["pre_observation_event_id"] == step_event["event_id"]
    assert transition_event["payload"]["action_execution_event_id"] == execution_event["event_id"]

    pre_image = step_event["payload"]["observation"]["screenshot"]
    post_image = transition_event["payload"]["post_observation"]["screenshot"]
    assert recorder.blob_store.read_bytes(pre_image["source_blob"]) == b"exact-pre-source"
    assert recorder.blob_store.read_bytes(post_image["source_blob"]) == b"exact-post-source"
    with Image.open(io.BytesIO(recorder.blob_store.read_bytes(pre_image["pixel_blob"]))) as image:
        image.load()
        assert image.tobytes() == pre_pixels
    with Image.open(io.BytesIO(recorder.blob_store.read_bytes(post_image["pixel_blob"]))) as image:
        image.load()
        assert image.tobytes() == post_pixels

    assert action.model_dump(mode="json", exclude_none=False) == action_before
    assert pre_observation["screenshot"].tobytes() == pre_pixels
    assert post_observation["screenshot"].tobytes() == post_pixels
    end_payload = end_event["payload"]
    assert end_payload["environment_evaluation"] == {
        "score": 0.25,
        "reason": "exact evaluator reason",
        "exception": None,
    }
    assert ArtifactSerializer(recorder.blob_store).rehydrate(
        end_payload["teardown"]["result_snapshot_blob"]
    ) == {"status": "ok", "message": "exact teardown"}
    assert end_payload["capture_complete"] is True
    assert end_payload["missing_artifacts"] == []
    assert end_payload["collector_error_event_ids"] == []
    assert task.capture_complete is True
    recorder.close()


@pytest.mark.parametrize(
    ("prediction", "ask_user_text", "user_response"),
    _MATTERMOST_ASK_USER_CASES,
)
def test_model_visible_credential_shaped_text_is_lossless_and_zero_intervention(
    tmp_path: Path,
    prediction: str,
    ask_user_text: str,
    user_response: str,
) -> None:
    mattermost_url = "http://10.0.2.2:8065"
    task_goal = (
        f"Preserve password=user, api_key=fixture, Bearer demo, {mattermost_url}; "
        f"then ask: {ask_user_text}"
    )
    reason = f"Observed password=user; api_key=fixture; Bearer demo; {mattermost_url}"
    teardown_result = {
        "password": "user",
        "api_key": "fixture",
        "authorization": "Bearer demo",
    }
    recorder, task, capture = _make_capture(tmp_path)
    task_started = capture.start_task(
        task_name="MattermostCreateChannelTask",
        task_goal=task_goal,
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": "fixture", "model": "none", "configuration": {}},
        environment={"backend_id": "fixture", "device_id": "fixture"},
        whole_task_attempt_index=1,
    )
    assert task_started is not None

    pre_observation = _observation(
        (11, 22, 33),
        accessibility_tree={"password": "user", "server_url": mattermost_url},
        tool_call={"api_key": "fixture", "prompt": ask_user_text},
        ask_user_response="Bearer demo",
    )
    pre_semantics_before = copy.deepcopy(
        {key: value for key, value in pre_observation.items() if key != "screenshot"}
    )
    pre_pixels_before = pre_observation["screenshot"].tobytes()
    capture.start_step(step_index=1, observation=pre_observation)

    semantic_prediction = (
        f"{prediction}\npassword=user; api_key=fixture; Bearer demo; {mattermost_url}"
    )
    action = JSONAction(
        action_type="ask_user",
        text=ask_user_text,
        action_json={
            "password": "user",
            "api_key": "fixture",
            "authorization": "Bearer demo",
            "server_url": mattermost_url,
        },
    )
    action_before = action.model_dump(mode="json", exclude_none=False)
    decision = capture.record_decision(prediction=semantic_prediction, action=action)
    assert decision is not None
    assert decision.action is action
    execution = capture.execution_started()
    assert execution is not None

    post_observation = _observation(
        (33, 22, 11),
        accessibility_tree={"authorization": "Bearer demo"},
        tool_call={"password": "user", "server_url": mattermost_url},
        ask_user_response=user_response,
    )
    post_semantics_before = copy.deepcopy(
        {key: value for key, value in post_observation.items() if key != "screenshot"}
    )
    post_pixels_before = post_observation["screenshot"].tobytes()
    execution_result = _transport_result(recorder)
    execution_result.update(
        {
            "request_endpoint": "http://fixture.invalid/step",
            "agent_visible_tool_result": post_observation["tool_call"],
            "ask_user_response": user_response,
        }
    )
    execution_result_before = copy.deepcopy(execution_result)
    capture.transition_completed(
        post_observation=post_observation,
        execution_result=execution_result,
        duration_ns=1,
    )
    teardown_before = copy.deepcopy(teardown_result)
    capture.end_task(
        runtime_status="completed",
        termination_source="max_step",
        final_step_index=1,
        score=0.0,
        reason=reason,
        teardown_attempted=True,
        teardown_result=teardown_result,
    )

    events = _events(task)
    start_payload = _by_type(events, "task_started")["payload"]
    step_payload = _by_type(events, "step_started")["payload"]
    decision_payload = _by_type(events, "agent_decision")["payload"]
    execution_payload = _by_type(events, "action_execution_started")["payload"]
    transition_payload = _by_type(events, "transition_completed")["payload"]
    end_payload = _by_type(events, "task_ended")["payload"]
    assert start_payload["task_goal"] == task_goal
    assert (
        step_payload["observation"]["accessibility_tree"]
        == pre_semantics_before["accessibility_tree"]
    )
    assert step_payload["observation"]["tool_call"] == pre_semantics_before["tool_call"]
    assert step_payload["observation"]["ask_user_response"] == "Bearer demo"
    assert decision_payload["prediction_raw"] == semantic_prediction
    assert decision_payload["parsed_action"]["value"] == action_before
    assert execution_payload["action"] == action_before
    assert transition_payload["action"] == action_before
    assert (
        transition_payload["execution_result"]["agent_visible_tool_result"]
        == (post_semantics_before["tool_call"])
    )
    assert transition_payload["execution_result"]["ask_user_response"] == user_response
    assert (
        transition_payload["post_observation"]["accessibility_tree"]
        == (post_semantics_before["accessibility_tree"])
    )
    assert transition_payload["post_observation"]["tool_call"] == post_semantics_before["tool_call"]
    assert end_payload["environment_evaluation"]["reason"] == reason
    assert (
        ArtifactSerializer(recorder.blob_store).rehydrate(
            end_payload["teardown"]["result_snapshot_blob"]
        )
        == teardown_before
    )
    assert end_payload["capture_complete"] is True
    assert end_payload["missing_artifacts"] == []
    assert end_payload["collector_error_event_ids"] == []
    assert "[REDACTED" not in json.dumps(events, ensure_ascii=False)

    assert pre_observation["screenshot"].tobytes() == pre_pixels_before
    assert post_observation["screenshot"].tobytes() == post_pixels_before
    assert {
        key: value for key, value in pre_observation.items() if key != "screenshot"
    } == pre_semantics_before
    assert {
        key: value for key, value in post_observation.items() if key != "screenshot"
    } == post_semantics_before
    assert action.model_dump(mode="json", exclude_none=False) == action_before
    assert execution_result == execution_result_before
    assert teardown_result == teardown_before
    recorder.close()


def test_large_prediction_and_runtime_value_use_ref_only_typed_placeholders(
    tmp_path: Path,
) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    large_runtime = "tree:" + ("x" * 70_000)
    large_prediction = "prediction:" + ("y" * 70_000)
    step = capture.start_step(
        step_index=1,
        observation=_observation(
            (1, 2, 3),
            accessibility_tree={"xml": large_runtime},
        ),
    )
    assert step is not None
    decision = capture.record_decision(
        prediction=large_prediction,
        action=JSONAction(action_type="finished"),
    )
    assert decision is not None
    capture.transition_not_executed(reason="terminal_action")
    capture.end_task(
        runtime_status="completed",
        termination_source="agent_terminal_action",
        final_step_index=1,
    )

    events = _events(task)
    step_payload = _by_type(events, "step_started")["payload"]
    decision_payload = _by_type(events, "agent_decision")["payload"]
    runtime_placeholder = step_payload["observation"]["accessibility_tree"]
    prediction_placeholder = decision_payload["prediction_raw"]
    runtime_metadata = runtime_placeholder["$artifact_snapshot"]
    prediction_metadata = prediction_placeholder["$artifact_snapshot"]

    assert "view" not in runtime_metadata
    assert "view" not in prediction_metadata
    assert len(json.dumps(_by_type(events, "step_started"))) < 10_000
    assert len(json.dumps(_by_type(events, "agent_decision"))) < 10_000
    serializer = ArtifactSerializer(recorder.blob_store)
    assert serializer.rehydrate(runtime_metadata["snapshot_blob"]) == {"xml": large_runtime}
    assert serializer.rehydrate(prediction_metadata["snapshot_blob"]) == large_prediction
    assert prediction_metadata["snapshot_blob"] == decision_payload["prediction_snapshot_blob"]
    assert _by_type(events, "task_ended")["payload"]["capture_complete"] is True
    recorder.close()


@pytest.mark.parametrize(
    ("action", "execution_kind", "result_kind", "tool_call", "user_response"),
    [
        (JSONAction(action_type="answer", text="final"), "answer", "gui_transport", None, None),
        (
            JSONAction(action_type="ask_user", text="question"),
            "ask_user",
            "gui_transport",
            None,
            "exact user response",
        ),
        (
            JSONAction(
                action_type="mcp",
                action_name="fixture_tool",
                action_json={"query": "exact"},
            ),
            "mcp",
            "mcp_tool",
            {"text": "exact visible tool result"},
            None,
        ),
    ],
)
def test_executed_answer_ask_user_and_mcp_paths_keep_returned_observation(
    tmp_path: Path,
    action: JSONAction,
    execution_kind: str,
    result_kind: str,
    tool_call: Any,
    user_response: str | None,
) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((2, 3, 4)))
    capture.record_decision(prediction="exact", action=action)
    execution = capture.execution_started()
    assert execution is not None and execution.execution_kind == execution_kind

    execution_result = _transport_result(recorder)
    execution_result["kind"] = result_kind
    execution_result["agent_visible_tool_result"] = tool_call
    execution_result["ask_user_response"] = user_response
    if execution_kind == "mcp":
        execution_result["raw_tool_result_blob"] = recorder.blob_store.put_bytes(
            b"exact raw tool result",
            "application/octet-stream",
        )
    transition = capture.transition_completed(
        post_observation=_observation(
            (4, 3, 2),
            tool_call=tool_call,
            ask_user_response=user_response,
        ),
        execution_result=execution_result,
        duration_ns=1,
    )
    assert transition is not None
    capture.end_task(
        runtime_status="completed",
        termination_source="executed_fixture_action",
        final_step_index=1,
    )

    events = _events(task)
    execution_payload = _by_type(events, "action_execution_started")["payload"]
    transition_payload = _by_type(events, "transition_completed")["payload"]
    assert execution_payload["execution_kind"] == execution_kind
    assert transition_payload["execution_result"]["kind"] == result_kind
    assert transition_payload["post_observation"]["tool_call"] == tool_call
    assert transition_payload["post_observation"]["ask_user_response"] == user_response
    assert _by_type(events, "task_ended")["payload"]["capture_complete"] is True
    recorder.close()


@pytest.mark.parametrize(
    ("action_type", "prediction", "parse_exception", "reason", "expected_outcome"),
    [
        ("finished", "done", None, "terminal_action", "returned"),
        ("unknown", "unknown", None, "terminal_action", "returned"),
        ("error_env", "environment failed", None, "terminal_action", "returned"),
        (None, None, None, "prediction_none", "returned_prediction_none"),
        (
            None,
            None,
            RuntimeError("predict failed exactly"),
            "prediction_exception",
            "raised",
        ),
    ],
)
def test_nonexecuted_decision_paths_have_no_fabricated_post_state(
    tmp_path: Path,
    action_type: str | None,
    prediction: str | None,
    parse_exception: BaseException | None,
    reason: str,
    expected_outcome: str,
) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((3, 4, 5)))
    if action_type == "error_env":
        # ENV_FAIL is handled by the runner but is currently absent from
        # JSONAction's validator allow-list, so exercise the factual mapping
        # form without changing that unrelated runtime model here.
        action: Any = {"action_type": action_type}
    else:
        action = JSONAction(action_type=action_type) if action_type is not None else None
    decision = capture.record_decision(
        prediction=prediction,
        action=action,
        parse_exception=parse_exception,
    )
    assert decision is not None
    terminal = capture.transition_not_executed(reason=reason)
    assert terminal is not None
    capture.end_task(
        runtime_status="crashed" if parse_exception else "completed",
        termination_source=reason,
        final_step_index=1,
        termination_exception=parse_exception,
    )

    events = _events(task)
    assert "action_execution_started" not in [event["event_type"] for event in events]
    decision_payload = _by_type(events, "agent_decision")["payload"]
    terminal_payload = _by_type(events, "transition_not_executed")["payload"]
    assert decision_payload["parse_outcome"] == expected_outcome
    assert terminal_payload["post_observation"] is None
    assert terminal_payload["reason"] == reason
    expected_action = (
        action.model_dump(mode="json", exclude_none=False)
        if isinstance(action, JSONAction)
        else action
    )
    assert terminal_payload["action"] == expected_action
    if parse_exception is not None:
        assert decision_payload["parse_exception"] == {
            "class": "builtins.RuntimeError",
            "message": "predict failed exactly",
            "details_blob": None,
        }
    recorder.close()


def test_transition_failure_preserves_exception_and_available_facts(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((5, 6, 7)))
    action = JSONAction(action_type="click", x=1, y=2)
    capture.record_decision(prediction="click", action=action)
    execution = capture.execution_started()
    assert execution is not None
    response_blob = recorder.blob_store.put_bytes(b"backend unavailable", "text/plain")
    transition = capture.transition_failed(
        exception=RuntimeError("execute failed exactly"),
        available_execution_result={
            "kind": "gui_transport",
            "http_status": 503,
            "response_body_blob": response_blob,
        },
        duration_ns=999,
    )
    assert transition is not None
    capture.end_task(
        runtime_status="crashed",
        termination_source="uncaught_exception",
        final_step_index=1,
        termination_exception=RuntimeError("execute failed exactly"),
    )

    events = _events(task)
    payload = _by_type(events, "transition_failed")["payload"]
    assert payload["post_observation"] is None
    assert payload["duration_ns"] == 999
    assert payload["available_execution_result"]["response_body_blob"] == response_blob
    assert payload["exception"] == {
        "class": "builtins.RuntimeError",
        "message": "execute failed exactly",
        "details_blob": None,
    }
    assert payload["action"] == action.model_dump(mode="json", exclude_none=False)
    assert _by_type(events, "task_ended")["payload"]["capture_complete"] is True
    recorder.close()


def test_missing_transport_is_explicitly_incomplete_in_fail_open_mode(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((7, 8, 9)))
    capture.record_decision(
        prediction="click",
        action=JSONAction(action_type="click", x=1, y=2),
    )
    capture.execution_started()
    transition = capture.transition_completed(
        post_observation=_observation((9, 8, 7)),
        duration_ns=12,
    )
    assert transition is not None
    capture.end_task(
        runtime_status="completed",
        termination_source="max_step",
        final_step_index=1,
    )

    missing = "transition_completed.execution_result.transport_evidence"
    events = _events(task)
    transition_payload = _by_type(events, "transition_completed")["payload"]
    end_payload = _by_type(events, "task_ended")["payload"]
    assert transition_payload["execution_result"]["kind"] == "gui_transport"
    assert transition_payload["execution_result"]["http_status"] is None
    assert transition_payload["duration_ns"] == 12
    assert capture.capture_complete is False
    assert task.capture_complete is False
    assert missing in end_payload["missing_artifacts"]
    assert end_payload["capture_complete"] is False
    recorder.close()


def test_incomplete_transition_never_raises_at_runtime(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((8, 8, 8)))
    capture.record_decision(
        prediction="click",
        action=JSONAction(action_type="click", x=1, y=2),
    )
    capture.execution_started()

    event = capture.transition_completed(post_observation=_observation((9, 9, 9)))

    assert event is not None
    assert capture.capture_complete is False
    assert task.capture_complete is False
    assert capture.collector_error_event_ids == []
    assert "collector_error" not in [event["event_type"] for event in _events(task)]
    recorder.close()


def test_fail_open_serialization_error_emits_error_and_task_marker(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    task_started = _start_task(capture)

    step = capture.start_step(
        step_index=1,
        observation={"screenshot": object()},
    )
    assert step is not None and step.step_started_event_id is None
    assert capture.capture_complete is False
    assert capture.collector_error_event_ids
    capture.end_task(
        runtime_status="crashed",
        termination_source="collector_fixture_continued",
        final_step_index=1,
    )

    events = _events(task)
    assert [event["event_type"] for event in events] == [
        "task_started",
        "collector_error",
        "task_ended",
    ]
    error_event = _by_type(events, "collector_error")
    assert error_event["caused_by_event_id"] == task_started["event_id"]
    assert error_event["payload"]["step_id"] == step.step_id
    assert error_event["payload"]["scope"] == "step_started"
    assert error_event["payload"]["agent_execution_continued"] is True
    end_payload = _by_type(events, "task_ended")["payload"]
    assert end_payload["capture_complete"] is False
    assert "step_started.observation" in end_payload["missing_artifacts"]
    assert error_event["event_id"] in end_payload["collector_error_event_ids"]
    recorder.close()


def test_action_serialization_failure_keeps_live_action_without_false_event(
    tmp_path: Path,
) -> None:
    class UnsupportedAction:
        action_type = "click"

        def __init__(self) -> None:
            self.payload = ["must", "stay", "live"]

    recorder, task, capture = _make_capture(tmp_path)
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((1, 1, 1)))
    action = UnsupportedAction()
    payload_before = list(action.payload)

    decision = capture.record_decision(prediction="raw", action=action)
    assert decision is not None
    assert decision.event_id is None
    assert decision.action is action
    assert decision.action_snapshot is None
    execution = capture.execution_started()
    assert execution is not None
    assert execution.event_id is None
    assert execution.action is action
    assert execution.execution_kind == "gui"
    assert action.payload == payload_before
    capture.end_task(
        runtime_status="completed",
        termination_source="fixture_continued",
        final_step_index=1,
    )

    event_types = [event["event_type"] for event in _events(task)]
    assert "agent_decision" not in event_types
    assert "action_execution_started" not in event_types
    assert event_types.count("collector_error") == 2
    assert capture.capture_complete is False
    recorder.close()


def test_task_end_merges_collector_state_from_other_capture_surfaces(tmp_path: Path) -> None:
    recorder, task, capture = _make_capture(tmp_path)
    task_started = _start_task(capture)
    external_error = task.append_event(
        "collector_error",
        {
            "scope": "model_request",
            "related_event_id": task_started["event_id"],
            "step_id": None,
            "exception": {
                "class": "builtins.OSError",
                "message": "fixture storage failure",
            },
            "missing_artifacts": ["sdk_arguments_snapshot_blob"],
            "agent_execution_continued": True,
        },
        caused_by_event_id=task_started["event_id"],
    )
    assert capture.capture_complete is True

    capture.end_task(
        runtime_status="completed",
        termination_source="fixture",
        final_step_index=0,
    )

    end_payload = _by_type(_events(task), "task_ended")["payload"]
    assert end_payload["capture_complete"] is False
    assert "sdk_arguments_snapshot_blob" in end_payload["missing_artifacts"]
    assert external_error["event_id"] in end_payload["collector_error_event_ids"]
    recorder.close()


def test_metadata_headers_signed_urls_and_exceptions_never_persist_secrets(
    tmp_path: Path,
) -> None:
    secret = "sk-fixture-secret-never-store"
    recorder, task, capture = _make_capture(
        tmp_path,
        configured_secrets=(secret,),
    )
    task_started = capture.start_task(
        task_name="FixtureTask",
        task_goal=f"goal containing {secret}",
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={
            "adapter": "fixture",
            "model": f"prefix-{secret}-suffix",
            "api_key": secret,
        },
        environment={
            "backend_id": "fixture",
            "dashboard_url": ("https://example.invalid/path?page=2&X-Amz-Signature=signed-value"),
            "authorization": f"Bearer {secret}",
        },
        whole_task_attempt_index=1,
    )
    assert task_started is not None
    capture.start_step(step_index=1, observation=_observation((1, 2, 3)))
    capture.record_decision(
        prediction="click",
        action=JSONAction(action_type="click", x=1, y=2),
    )
    capture.execution_started()
    execution_result = _transport_result(recorder)
    execution_result["response_headers"] = {
        "Authorization": f"Bearer {secret}",
        "Set-Cookie": f"session={secret}",
        "x-trace": f"prefix-{secret}-suffix",
    }
    capture.transition_completed(
        post_observation=_observation((3, 2, 1)),
        execution_result=execution_result,
        duration_ns=4,
    )
    capture.end_task(
        runtime_status="completed",
        termination_source="max_step",
        final_step_index=1,
        termination_exception=RuntimeError(
            f"Authorization: Bearer {secret} at "
            "https://example.invalid/log?token=query-secret&keep=yes"
        ),
        reason=f"api_key={secret}; preserved tail",
    )

    events = _events(task)
    serialized_events = json.dumps(events, ensure_ascii=False)
    assert secret not in serialized_events
    start_payload = _by_type(events, "task_started")["payload"]
    assert start_payload["task_goal"] is None
    assert "api_key" not in start_payload["agent"]
    assert start_payload["agent"]["model"] == ("prefix-[REDACTED_CONFIGURED_SECRET]-suffix")
    assert "authorization" not in start_payload["environment"]
    assert "page=2" in start_payload["environment"]["dashboard_url"]
    assert "signed-value" not in start_payload["environment"]["dashboard_url"]
    transition_payload = _by_type(events, "transition_completed")["payload"]
    headers = transition_payload["execution_result"]["response_headers"]
    assert set(headers) == {"x-trace"}
    assert headers["x-trace"] == "prefix-[REDACTED_CONFIGURED_SECRET]-suffix"
    end_payload = _by_type(events, "task_ended")["payload"]
    assert "query-secret" not in end_payload["termination"]["exception"]["message"]
    assert "keep=yes" in end_payload["termination"]["exception"]["message"]
    assert end_payload["environment_evaluation"]["reason"] is None
    assert end_payload["capture_complete"] is False
    assert "task_started.task_goal.configured_secret_excluded" in end_payload["missing_artifacts"]
    assert (
        "task_ended.environment_evaluation.reason.configured_secret_excluded"
        in end_payload["missing_artifacts"]
    )
    for blob in (recorder.run_root / "blobs" / "sha256").glob("*/*"):
        assert secret.encode() not in blob.read_bytes()
    recorder.close()


def test_local_api_key_placeholder_is_not_a_runtime_semantic_secret() -> None:
    assert runner_capture_module._normalize_secrets(("empty", "EMPTY", b"EmPtY")) == ()


def test_runtime_observation_prediction_and_action_exclude_configured_secrets(
    tmp_path: Path,
) -> None:
    secret = "runtime-secret-never-store"
    recorder, task, capture = _make_capture(
        tmp_path,
        configured_secrets=(secret,),
    )
    _start_task(capture)
    capture.start_step(
        step_index=1,
        observation=_observation(
            (1, 2, 3),
            accessibility_tree={"text": f"tree-{secret}"},
            tool_call={"text": f"tool-{secret}"},
            ask_user_response=f"user-{secret}",
        ),
    )
    capture.record_decision(
        prediction=f"prediction-{secret}",
        action={
            "action_type": "mcp",
            "action_name": "fixture",
            "action_json": {"value": f"action-{secret}"},
        },
    )
    capture.transition_not_executed(reason="fixture")
    capture.end_task(
        runtime_status="completed",
        termination_source="fixture",
        final_step_index=1,
        reason=f"reason-{secret}",
        teardown_attempted=True,
        teardown_result={"message": f"teardown-{secret}"},
    )

    events = _events(task)
    serialized = json.dumps(events, ensure_ascii=False)
    assert secret not in serialized
    step = _by_type(events, "step_started")["payload"]["observation"]
    assert step["accessibility_tree"]["text"] == "tree-[REDACTED_CONFIGURED_SECRET]"
    assert step["tool_call"]["text"] == "tool-[REDACTED_CONFIGURED_SECRET]"
    assert step["ask_user_response"] == "user-[REDACTED_CONFIGURED_SECRET]"
    decision = _by_type(events, "agent_decision")["payload"]
    assert decision["prediction_raw"] is None
    assert decision["parsed_action"]["value"]["action_json"]["value"] == (
        "action-[REDACTED_CONFIGURED_SECRET]"
    )
    ended = _by_type(events, "task_ended")["payload"]
    assert ended["environment_evaluation"]["reason"] is None
    assert ArtifactSerializer(recorder.blob_store).rehydrate(
        ended["teardown"]["result_snapshot_blob"]
    ) == {"message": "teardown-[REDACTED_CONFIGURED_SECRET]"}
    assert ended["capture_complete"] is False
    assert any("configured_secret" in item for item in ended["missing_artifacts"])
    for blob in (recorder.run_root / "blobs" / "sha256").glob("*/*"):
        assert secret.encode() not in blob.read_bytes()
    recorder.close()


def test_secret_in_to_dict_runtime_object_fails_closed_without_persisting_value(
    tmp_path: Path,
) -> None:
    secret = "opaque-object-secret"

    class ToolResult:
        def to_dict(self) -> dict[str, str]:
            return {"value": secret}

    recorder, task, capture = _make_capture(
        tmp_path,
        configured_secrets=(secret,),
    )
    _start_task(capture)
    step = capture.start_step(
        step_index=1,
        observation=_observation((1, 2, 3), tool_call=ToolResult()),
    )
    assert step is not None and step.step_started_event_id is None
    capture.end_task(
        runtime_status="crashed",
        termination_source="collector_fixture",
        final_step_index=1,
    )

    persisted = b"".join(
        path.read_bytes() for path in recorder.run_root.rglob("*") if path.is_file()
    )
    assert secret.encode() not in persisted
    assert _by_type(_events(task), "task_ended")["payload"]["capture_complete"] is False
    recorder.close()


def test_invalid_execution_evidence_is_null_marked_in_every_configured_mode(
    tmp_path: Path,
) -> None:
    invalid_reference = {
        "algorithm": "sha256",
        "digest": "0" * 64,
        "byte_length": 1,
        "media_type": "application/json",
        "relative_path": f"blobs/sha256/00/{'0' * 64}",
    }
    provided = {
        "kind": "wrong-kind",
        "request_body_snapshot_blob": invalid_reference,
        "http_status": "200",
        "response_body_blob": invalid_reference,
        "response_headers": [],
        "raw_tool_result_blob": None,
        "agent_visible_tool_result": None,
        "ask_user_response": None,
        "exception": None,
    }

    recorder, task, capture = _make_capture(tmp_path / "fail-open")
    _start_task(capture)
    capture.start_step(step_index=1, observation=_observation((1, 1, 2)))
    capture.record_decision(
        prediction="click",
        action=JSONAction(action_type="click", x=1, y=2),
    )
    capture.execution_started()
    event = capture.transition_completed(
        post_observation=_observation((2, 1, 1)),
        execution_result=provided,
        duration_ns=5,
    )
    assert event is not None
    payload = event["payload"]["execution_result"]
    assert payload["kind"] == "gui_transport"
    assert payload["request_body_snapshot_blob"] is None
    assert payload["response_body_blob"] is None
    assert payload["http_status"] is None
    assert payload["response_headers"] == {}
    assert capture.capture_complete is False
    assert "transition_completed.execution_result.kind" in capture.missing_artifacts
    recorder.close()


def test_task_end_serialization_failure_writes_minimal_terminal(tmp_path: Path) -> None:
    class UnsupportedTeardownResult:
        pass

    recorder, task, capture = _make_capture(tmp_path)
    task_started = _start_task(capture)
    event = capture.end_task(
        runtime_status="completed",
        termination_source="fixture",
        final_step_index=0,
        score=1.0,
        reason="would have been retained",
        teardown_attempted=True,
        teardown_result=UnsupportedTeardownResult(),
        token_usage={"total_tokens": 1},
    )
    assert event is not None

    events = _events(task)
    assert [item["event_type"] for item in events] == [
        "task_started",
        "collector_error",
        "task_ended",
    ]
    error = _by_type(events, "collector_error")
    terminal = _by_type(events, "task_ended")
    assert error["caused_by_event_id"] == task_started["event_id"]
    assert terminal["payload"]["capture_complete"] is False
    assert "task_ended.full_payload" in terminal["payload"]["missing_artifacts"]
    assert error["event_id"] in terminal["payload"]["collector_error_event_ids"]
    assert terminal["payload"]["environment_evaluation"]["score"] is None
    assert terminal["payload"]["teardown"]["result_snapshot_blob"] is None
    recorder.close()


def test_fail_open_mark_incomplete_ignores_recorder_state_failure(tmp_path: Path) -> None:
    recorder, task, _ = _make_capture(tmp_path)

    class RaisingMarkerRecorder:
        enabled = True

        @property
        def collector_mode(self) -> CollectorMode:
            return task.collector_mode

        @property
        def blob_store(self) -> Any:
            return task.blob_store

        @property
        def capture_complete(self) -> bool:
            return task.capture_complete

        @property
        def missing_artifacts(self) -> tuple[str, ...]:
            return task.missing_artifacts

        @property
        def collector_error_event_ids(self) -> tuple[str, ...]:
            return task.collector_error_event_ids

        def mark_incomplete(self, *_: str) -> None:
            raise OSError("state channel unavailable")

        def append_event(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return task.append_event(*args, **kwargs)

    capture = RunnerTaskCapture(RaisingMarkerRecorder())
    _start_task(capture)
    capture.mark_incomplete("fixture.missing")
    capture.end_task(
        runtime_status="completed",
        termination_source="fixture",
        final_step_index=0,
    )

    assert capture.capture_complete is False
    assert "fixture.missing" in capture.missing_artifacts
    assert _by_type(_events(task), "task_ended")["payload"]["capture_complete"] is False
    recorder.close()


def test_every_runtime_write_failure_is_no_throw(
    tmp_path: Path,
) -> None:
    recorder, task, _ = _make_capture(tmp_path)

    class RaisingWriterRecorder:
        enabled = True

        @property
        def collector_mode(self) -> CollectorMode:
            return task.collector_mode

        @property
        def blob_store(self) -> Any:
            return task.blob_store

        @property
        def capture_complete(self) -> bool:
            return task.capture_complete

        @property
        def missing_artifacts(self) -> tuple[str, ...]:
            return task.missing_artifacts

        @property
        def collector_error_event_ids(self) -> tuple[str, ...]:
            return task.collector_error_event_ids

        def mark_incomplete(self, *artifacts: str) -> None:
            task.mark_incomplete(*artifacts)

        def append_event(self, *_: Any, **__: Any) -> None:
            raise OSError("collector writer unavailable")

    capture = RunnerTaskCapture(RaisingWriterRecorder())
    assert (
        capture.start_task(
            task_name="FixtureTask",
            task_goal="goal",
            task_goal_status="resolved",
            task_index=1,
            suite_family="mobile_world",
            agent={"adapter": "fixture"},
            environment={"backend_id": "fixture"},
            whole_task_attempt_index=1,
        )
        is None
    )
    step = capture.start_step(step_index=1, observation=_observation((1, 2, 3)))
    assert step is not None
    decision = capture.record_decision(
        prediction="click",
        action=JSONAction(action_type="click", x=1, y=2),
    )
    assert decision is not None
    execution = capture.execution_started(decision=decision)
    assert execution is not None
    assert (
        capture.transition_completed(
            post_observation=_observation((3, 2, 1)),
            execution=execution,
            duration_ns=1,
        )
        is None
    )
    assert (
        capture.transition_failed(
            exception=RuntimeError("business failure"),
            execution=execution,
            duration_ns=1,
        )
        is None
    )
    assert capture.transition_not_executed(reason="fixture", decision=decision) is None
    assert (
        capture.end_task(
            runtime_status="completed",
            termination_source="fixture",
            final_step_index=1,
        )
        is None
    )
    assert capture.capture_complete is False
    assert task.capture_complete is False
    recorder.close()
