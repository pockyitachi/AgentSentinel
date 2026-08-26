from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.grounding import uiins as uiins_module
from mobile_world.agents.grounding.uiins import UIINSGroundingAgent
from mobile_world.runtime.audit import model_io
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
)
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.null_recorder import NULL_TASK_RECORDER
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer


class _Response:
    def __init__(self, content: str, *, response_id: str = "grounder-response") -> None:
        self.id = response_id
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        assert mode == "json"
        assert exclude_none is False
        return {
            "id": self.id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "content": self.choices[0].message.content,
                        "reasoning_content": None,
                        "tool_calls": [],
                    },
                }
            ],
            "usage": None,
        }


class _Completions:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(kwargs)
        return outcome


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.api_key = "empty"
        self.base_url = "https://grounder.example/v1"
        self.chat = SimpleNamespace(completions=_Completions(outcomes))


def _agent(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    *,
    instruction: str = "click the target",
) -> tuple[UIINSGroundingAgent, _Client, dict[str, Any]]:
    client = _Client(outcomes)
    constructor_kwargs: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> _Client:
        constructor_kwargs.update(kwargs)
        return client

    monkeypatch.setattr(uiins_module, "OpenAI", fake_openai)
    agent = UIINSGroundingAgent(
        llm_base_url="https://grounder.example/v1",
        model_name="executor-model",
        runtime_conf={
            "temperature": 0.25,
            "max_tokens": 321,
            "min_pixels": 1,
            "max_pixels": 100_000,
        },
    )
    agent.initialize(instruction)
    return agent, client, constructor_kwargs


def _open_audit_task(
    tmp_path: Path,
) -> tuple[RunRecorder, TaskRecorder, AuditContext]:
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="test", worker_id="pytest-worker"),
        sync=False,
    )
    run.write_manifest_start({})
    task = run.open_task()
    step_id = new_ulid()
    parent = task.append_event(
        "step_started",
        {"step_id": step_id, "observation": None, "agent_observation_keys": []},
    )
    context = AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=step_id,
        model_call_trace=ModelCallTrace(),
        parent_event_id=parent["event_id"],
    )
    return run, task, context


def _events(task: TaskRecorder, event_type: str | None = None) -> list[dict[str, Any]]:
    events = [json.loads(line) for line in task.path.read_text().splitlines()]
    if event_type is None:
        return events
    return [event for event in events if event["event_type"] == event_type]


def _screenshot() -> Image.Image:
    return Image.new("RGB", (10, 8), color=(180, 20, 40))


def test_feature_off_preserves_call_result_and_creates_no_audit_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, client, constructor_kwargs = _agent(monkeypatch, [_Response(" [7,9] ")])

    import mobile_world.runtime.audit.model_io as model_io

    monkeypatch.setattr(
        model_io,
        "begin_model_call",
        lambda **_: pytest.fail("disabled UIINS path entered audit capture"),
    )
    context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)
    with bind_audit_context(context):
        prediction, action = agent.predict({"screenshot": _screenshot()})

    assert prediction == "[7,9]"
    assert action.x == 7
    assert action.y == 9
    assert len(client.chat.completions.calls) == 1
    assert client.chat.completions.calls[0]["model"] == "executor-model"
    assert constructor_kwargs["max_retries"] == 3
    assert list(tmp_path.iterdir()) == []


def test_each_retry_retains_original_fresh_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def mutate_then_fail(kwargs: dict[str, Any]) -> Any:
        kwargs["extra_body"]["repetition_penalty"] = 999
        raise RuntimeError("provider mutated its private kwargs")

    agent, client, _ = _agent(monkeypatch, [mutate_then_fail, _Response("[3,4]")])
    sleeps: list[float] = []
    monkeypatch.setattr(uiins_module.time, "sleep", sleeps.append)

    prediction, action = agent.predict({"screenshot": _screenshot()})

    assert prediction == "[3,4]"
    assert (action.x, action.y) == (3, 4)
    assert sleeps == [2]
    assert client.chat.completions.calls[0]["extra_body"]["repetition_penalty"] == 999
    assert client.chat.completions.calls[1]["extra_body"]["repetition_penalty"] == 1.0


def test_success_captures_exact_resized_grounder_request_and_raw_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, client, _ = _agent(monkeypatch, [_Response("[2,3]")])
    monkeypatch.setattr(uiins_module, "smart_resize", lambda *_args, **_kwargs: (4, 6))
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[2,3]"
        assert (action.x, action.y) == (2, 3)
        requests = _events(task, "model_request")
        responses = _events(task, "model_response")
        assert len(requests) == len(responses) == 1
        request = requests[0]
        response = responses[0]
        payload = request["payload"]
        assert payload["call_role"] == "grounder"
        assert payload["component"] == "mobile_world.agents.grounding.uiins"
        assert payload["request_view"]["model"] == "executor-model"
        assert payload["request_images"][0]["width"] == 6
        assert payload["request_images"][0]["height"] == 4
        assert request["caused_by_event_id"] == context.parent_event_id
        assert response["caused_by_event_id"] == request["event_id"]

        serializer = ArtifactSerializer(run.blob_store)
        reconstructed = serializer.rehydrate(payload["sdk_arguments_snapshot_blob"])
        assert reconstructed == client.chat.completions.calls[0]
        assert reconstructed["messages"][1]["content"][1]["text"] == "click the target"
        image_ref = payload["request_images"][0]["content_blob"]
        with Image.open(BytesIO(run.blob_store.read_bytes(image_ref))) as captured:
            assert captured.size == (6, 4)
        raw_response = serializer.rehydrate(response["payload"]["raw_response"]["snapshot_blob"])
        assert raw_response["choices"][0]["message"]["content"] == "[2,3]"
        assert context.source_model_call_ids() == (payload["model_call_id"],)
        assert context.latest_model_terminal_event_id() == response["event_id"]
    finally:
        run.close()


def test_three_visible_attempts_have_unique_requests_and_one_causal_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, client, _ = _agent(
        monkeypatch,
        [TimeoutError("first timeout"), RuntimeError("second failure"), _Response("[4,5]")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(uiins_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[4,5]"
        assert (action.x, action.y) == (4, 5)
        assert sleeps == [2, 2]
        assert len(client.chat.completions.calls) == 3
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        responses = _events(task, "model_response")
        assert [event["payload"]["attempt_index"] for event in requests] == [1, 2, 3]
        assert len({event["payload"]["request_id"] for event in requests}) == 3
        assert len({event["payload"]["model_call_id"] for event in requests}) == 1
        assert len({event["payload"]["retry_group_id"] for event in requests}) == 1
        assert [event["payload"]["retry_planned"] for event in failures] == [True, True]
        assert [event["payload"]["exception"]["message"] for event in failures] == [
            "first timeout",
            "second failure",
        ]
        assert len(responses) == 1
        assert requests[0]["caused_by_event_id"] == context.parent_event_id
        assert failures[0]["caused_by_event_id"] == requests[0]["event_id"]
        assert requests[1]["caused_by_event_id"] == failures[0]["event_id"]
        assert failures[1]["caused_by_event_id"] == requests[1]["event_id"]
        assert requests[2]["caused_by_event_id"] == failures[1]["event_id"]
        assert responses[0]["caused_by_event_id"] == requests[2]["event_id"]
    finally:
        run.close()


def test_all_provider_failures_preserve_existing_return_and_sleep_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = _agent(
        monkeypatch,
        [RuntimeError("one"), RuntimeError("two"), RuntimeError("three")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(uiins_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.predict({"screenshot": _screenshot()})

        assert result == ("All retries failed for UIINS Grounding Agent", None)
        assert sleeps == [2, 2]
        assert len(_events(task, "model_request")) == 3
        failures = _events(task, "model_attempt_failed")
        assert len(failures) == 3
        assert [event["payload"]["retry_planned"] for event in failures] == [
            True,
            True,
            False,
        ]
        assert _events(task, "model_response") == []
    finally:
        run.close()


def test_post_provider_parse_retries_remain_successful_model_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, _, _ = _agent(
        monkeypatch,
        [_Response("[1,2]"), _Response("[1,2]"), _Response("[1,2]")],
        instruction="swipe the target",
    )
    sleeps: list[float] = []
    monkeypatch.setattr(uiins_module.time, "sleep", sleeps.append)
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent.predict({"screenshot": _screenshot()})

        assert result == ("All retries failed for UIINS Grounding Agent", None)
        assert sleeps == [2, 2]
        assert len(_events(task, "model_request")) == 3
        assert len(_events(task, "model_response")) == 3
        assert _events(task, "model_attempt_failed") == []
    finally:
        run.close()


def test_fail_open_capture_error_does_not_change_grounder_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, client, _ = _agent(monkeypatch, [_Response("[8,6]")])
    run, task, context = _open_audit_task(tmp_path)

    def fail_snapshot(*_: Any, **__: Any) -> Any:
        raise OSError("audit storage unavailable")

    monkeypatch.setattr(ArtifactSerializer, "snapshot_sdk_arguments", fail_snapshot)
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[8,6]"
        assert (action.x, action.y) == (8, 6)
        assert len(client.chat.completions.calls) == 1
        assert len(_events(task, "collector_error")) == 1
        assert _events(task, "model_request") == []
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["begin_call", "begin_attempt", "response_record"])
def test_grounder_collector_facade_faults_do_not_change_result_or_sdk_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    agent, client, _ = _agent(monkeypatch, [_Response("[6,7]")])
    run, _, context = _open_audit_task(tmp_path)

    def collector_fault(*_: Any, **__: Any) -> Any:
        raise OSError(f"collector {fault_point} failed")

    if fault_point == "begin_call":
        monkeypatch.setattr(model_io, "begin_model_call", collector_fault)
    elif fault_point == "begin_attempt":
        monkeypatch.setattr(model_io.ModelCallAudit, "begin_attempt", collector_fault)
    else:
        monkeypatch.setattr(
            model_io.ModelAttemptAudit,
            "record_nonstream_response",
            collector_fault,
        )
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[6,7]"
        assert (action.x, action.y) == (6, 7)
        assert len(client.chat.completions.calls) == 1
        provider_call = client.chat.completions.calls[0]
        assert provider_call["model"] == "executor-model"
        assert provider_call["extra_body"] == {"repetition_penalty": 1.0}
    finally:
        run.close()


def test_grounder_failure_recorder_fault_preserves_retry_sleep_and_fresh_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent, client, _ = _agent(
        monkeypatch,
        [TimeoutError("provider timeout"), _Response("[4,8]")],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(uiins_module.time, "sleep", sleeps.append)

    def collector_fault(*_: Any, **__: Any) -> Any:
        raise OSError("failure recorder unavailable")

    monkeypatch.setattr(model_io.ModelAttemptAudit, "record_failure", collector_fault)
    run, _, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[4,8]"
        assert (action.x, action.y) == (4, 8)
        assert sleeps == [2]
        assert len(client.chat.completions.calls) == 2
        assert (
            client.chat.completions.calls[0]["extra_body"]
            is not client.chat.completions.calls[1]["extra_body"]
        )
        assert [
            call["extra_body"]["repetition_penalty"] for call in client.chat.completions.calls
        ] == [1.0, 1.0]
    finally:
        run.close()


@pytest.mark.parametrize("fault_point", ["response_snapshot", "response_terminal"])
def test_grounder_response_capture_faults_do_not_prevent_action_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    response = _Response("[9,2]")
    agent, client, _ = _agent(monkeypatch, [response])
    run, task, context = _open_audit_task(tmp_path)

    if fault_point == "response_snapshot":
        original_snapshot = ArtifactSerializer.snapshot

        def faulting_snapshot(
            serializer: ArtifactSerializer,
            value: Any,
            **kwargs: Any,
        ) -> Any:
            if value is response:
                raise OSError("grounder response snapshot unavailable")
            return original_snapshot(serializer, value, **kwargs)

        monkeypatch.setattr(ArtifactSerializer, "snapshot", faulting_snapshot)
    else:
        original_append = task.append_event

        def faulting_append(
            event_type: str,
            payload: Any,
            caused_by_event_id: str | None = None,
        ) -> Any:
            if event_type == "model_response":
                raise OSError("grounder response terminal unavailable")
            return original_append(event_type, payload, caused_by_event_id)

        monkeypatch.setattr(task, "append_event", faulting_append)
    try:
        with bind_audit_context(context):
            prediction, action = agent.predict({"screenshot": _screenshot()})

        assert prediction == "[9,2]"
        assert (action.x, action.y) == (9, 2)
        assert len(client.chat.completions.calls) == 1
        assert task.capture_complete is False
        assert _events(task, "collector_error")
    finally:
        run.close()
