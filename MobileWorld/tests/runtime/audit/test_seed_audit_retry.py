from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.seed_agent import SeedAgent
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
    get_audit_context,
)
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.null_recorder import NULL_TASK_RECORDER
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.utils.models import ANSWER


class _Chunk:
    def __init__(
        self,
        *,
        reasoning: str | None = None,
        content: str | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.id = "seed-stream"
        self.usage = None
        self.choices = [
            SimpleNamespace(
                index=0,
                finish_reason=finish_reason,
                delta=SimpleNamespace(
                    reasoning_content=reasoning,
                    content=content,
                    tool_calls=None,
                ),
            )
        ]

    def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
        assert mode == "json"
        assert exclude_none is False
        choice = self.choices[0]
        return {
            "id": self.id,
            "choices": [
                {
                    "index": choice.index,
                    "finish_reason": choice.finish_reason,
                    "delta": {
                        "reasoning_content": choice.delta.reasoning_content,
                        "content": choice.delta.content,
                        "tool_calls": choice.delta.tool_calls,
                    },
                }
            ],
            "usage": None,
        }


class _Stream(Iterator[_Chunk]):
    def __init__(self, chunks: list[_Chunk], *, error: Exception | None = None) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._raised = False

    def __iter__(self) -> _Stream:
        return self

    def __next__(self) -> _Chunk:
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error is not None and not self._raised:
                self._raised = True
                raise self._error
            raise


class _Completions:
    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Client:
    def __init__(self, outcomes: list[Any]) -> None:
        self.api_key = "seed-test-secret"
        self.base_url = "https://seed.example/v1"
        self.chat = SimpleNamespace(completions=_Completions(outcomes))


def _seed_with(outcomes: list[Any] | None = None) -> SeedAgent:
    agent = SeedAgent.__new__(SeedAgent)
    BaseAgent.__init__(agent)
    if outcomes is not None:
        agent.openai_client = _Client(outcomes)
        agent.model_name = "seed-model"
        agent.reasoning_effort = "high"
        agent.max_tokens = 100
        agent.temperature = 0.1
        agent.top_p = 0.9
    return agent


def _open_audit_task(tmp_path: Any) -> tuple[RunRecorder, TaskRecorder, AuditContext]:
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="test", worker_id="seed-pytest"),
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


def _events(task: TaskRecorder, event_type: str) -> list[dict[str, Any]]:
    return [
        event
        for event in (json.loads(line) for line in task.path.read_text().splitlines())
        if event["event_type"] == event_type
    ]


def test_seed_retry_default_off_allocates_no_id_and_binds_no_child_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _seed_with()
    messages = [{"role": "user", "content": "same object"}]
    original_context = AuditContext(run_id="disabled", recorder=NULL_TASK_RECORDER)
    observed_contexts = []
    observed_messages = []
    calls = 0

    def fake_inference(actual_messages: list[dict]) -> str:
        nonlocal calls
        calls += 1
        observed_contexts.append(get_audit_context())
        observed_messages.append(actual_messages)
        if calls == 1:
            raise RuntimeError("first attempt")
        return "second attempt"

    monkeypatch.setattr(agent, "_inference_with_thinking", fake_inference)
    monkeypatch.setattr(
        "mobile_world.runtime.audit.ids.new_ulid",
        lambda: pytest.fail("disabled Seed retry allocated an audit ID"),
    )

    with bind_audit_context(original_context):
        result = agent._inference_with_retries(messages)

    assert result == "second attempt"
    assert calls == 2
    assert observed_contexts == [original_context, original_context]
    assert all(actual is messages for actual in observed_messages)


def test_seed_retry_default_off_preserves_final_exception_and_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _seed_with()
    errors = iter([RuntimeError("one"), RuntimeError("two"), RuntimeError("three")])
    calls = 0

    def failing_inference(_: list[dict]) -> str:
        nonlocal calls
        calls += 1
        raise next(errors)

    monkeypatch.setattr(agent, "_inference_with_thinking", failing_inference)
    with pytest.raises(
        ValueError,
        match="^Failed to get response from LLM after retries: three$",
    ):
        agent._inference_with_retries([])
    assert calls == 3


def test_seed_predict_appends_only_the_final_retry_result_to_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _seed_with()
    agent.history_images = []
    agent.history_responses = []
    calls = 0
    messages = [{"role": "user", "content": "rendered once"}]

    def fake_build_messages(*_: Any) -> list[dict]:
        return messages

    def fake_inference(actual_messages: list[dict]) -> str:
        nonlocal calls
        calls += 1
        assert actual_messages is messages
        if calls == 1:
            raise RuntimeError("retry me")
        return "plain final answer"

    monkeypatch.setattr(agent, "_build_messages", fake_build_messages)
    monkeypatch.setattr(agent, "_inference_with_thinking", fake_inference)
    prediction, action = agent.predict(
        {
            "screenshot": Image.new("RGB", (2, 2)),
            "tool_call": None,
            "ask_user_response": None,
        }
    )

    assert calls == 2
    assert prediction == "plain final answer"
    assert action.action_type == ANSWER
    assert agent.history_responses == ["plain final answer"]


def test_seed_partial_stream_retry_shares_group_but_uses_distinct_logical_calls(
    tmp_path: Any,
) -> None:
    first_error = RuntimeError("partial stream failure")
    first_stream = _Stream([_Chunk(reasoning="discarded")], error=first_error)
    second_stream = _Stream(
        [
            _Chunk(reasoning="valid reasoning"),
            _Chunk(content="valid action", finish_reason="stop"),
        ]
    )
    agent = _seed_with([first_stream, second_stream])
    messages = [{"role": "user", "content": "act"}]
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent._inference_with_retries(messages)

        assert result == "<think>valid reasoning</think>valid action"
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        responses = _events(task, "model_response")
        chunks = _events(task, "model_stream_chunk")
        assert len(requests) == 2
        assert len(failures) == 1
        assert len(responses) == 1
        assert [event["payload"]["adapter_attempt_index"] for event in requests] == [1, 2]
        assert [event["payload"]["attempt_index"] for event in requests] == [1, 1]
        assert len({event["payload"]["retry_group_id"] for event in requests}) == 1
        model_call_ids = tuple(event["payload"]["model_call_id"] for event in requests)
        assert len(set(model_call_ids)) == 2
        assert context.source_model_call_ids() == model_call_ids
        assert failures[0]["payload"]["failure_phase"] == "stream_iteration"
        assert failures[0]["payload"]["retry_planned"] is True
        assert len(failures[0]["payload"]["partial_chunk_event_ids"]) == 1
        assert requests[1]["caused_by_event_id"] == failures[0]["event_id"]
        assert responses[0]["payload"]["stream_state"] == "complete"
        assert responses[0]["payload"]["model_call_id"] == model_call_ids[1]
        assert len(chunks) == 3
        assert all(
            call["messages"] is messages for call in agent.openai_client.chat.completions.calls
        )
    finally:
        run.close()


def test_seed_stream_create_failure_is_correlated_to_later_outer_attempt(
    tmp_path: Any,
) -> None:
    create_error = ConnectionError("create failed")
    success_stream = _Stream([_Chunk(content="answer", finish_reason="stop")])
    agent = _seed_with([create_error, success_stream])
    run, task, context = _open_audit_task(tmp_path)
    try:
        with bind_audit_context(context):
            result = agent._inference_with_retries([])

        assert result == "<think></think>answer"
        requests = _events(task, "model_request")
        failures = _events(task, "model_attempt_failed")
        responses = _events(task, "model_response")
        assert len(requests) == 2
        assert len(failures) == 1
        assert len(responses) == 1
        assert failures[0]["payload"]["failure_phase"] == "provider_call"
        assert failures[0]["payload"]["retry_planned"] is True
        assert failures[0]["payload"]["adapter_attempt_index"] == 1
        assert responses[0]["payload"]["adapter_attempt_index"] == 2
        assert requests[1]["caused_by_event_id"] == failures[0]["event_id"]
        assert requests[0]["payload"]["retry_group_id"] == requests[1]["payload"]["retry_group_id"]
        assert requests[0]["payload"]["model_call_id"] != requests[1]["payload"]["model_call_id"]
    finally:
        run.close()
