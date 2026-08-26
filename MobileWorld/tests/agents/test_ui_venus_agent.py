from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.ui_venus_agent import (
    StepData,
    VenusNaviAgent,
    convert_venus_action_to_json_action,
    parse_answer,
)
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
)
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer


def _step(*, think: str, action: str, status: str = "success") -> StepData:
    return StepData(
        raw_screenshot=Image.new("RGB", (4, 6)),
        query="query",
        generated_text=f"<think>{think}</think><action>{action}</action>",
        think=think,
        action=action,
        conclusion="not exposed in later UI-Venus prompts",
        status=status,
    )


def _agent(monkeypatch: Any, *, history_length: int = 0) -> VenusNaviAgent:
    monkeypatch.setattr(BaseAgent, "build_openai_client", lambda *_args, **_kwargs: None)
    agent = VenusNaviAgent(
        llm_base_url="http://127.0.0.1:18007/v1",
        model_name="UI-Venus-1.5-8B",
        history_length=history_length,
    )
    agent.initialize("Complete the fixture task")
    return agent


class _Response:
    def __init__(self, content: str) -> None:
        self.id = "ui-venus-fixture-response"
        self.usage = None
        self.choices = [
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=None,
                    tool_calls=[],
                ),
            )
        ]

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
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return self.response


class _Client:
    def __init__(self, response: _Response) -> None:
        self.api_key = "empty"
        self.base_url = "http://127.0.0.1:18007/v1/"
        self.max_retries = 2
        self.timeout = 120.0
        self.chat = SimpleNamespace(completions=_Completions(response))


def test_default_zero_history_length_explicitly_preserves_all_previous_actions(
    monkeypatch: Any,
) -> None:
    agent = _agent(monkeypatch)
    agent.history = [
        _step(think="first thought", action="Click(box=(100,200))"),
        _step(think="parser failed", action="", status="failed"),
    ]

    query = agent._build_query("Complete the fixture task")

    assert "Step 0: <think>first thought</think><action>Click(box=(100,200))</action>" in query
    assert "Step 1: <think>parser failed</think><action></action>" in query
    assert "not exposed in later UI-Venus prompts" not in query


def test_finite_history_length_keeps_existing_flat_history_policy(monkeypatch: Any) -> None:
    agent = _agent(monkeypatch, history_length=1)
    agent.history = [
        _step(think="older", action="Wait()"),
        _step(think="newer", action="PressBack()"),
    ]

    query = agent._build_query("Complete the fixture task")

    assert "older" not in query
    assert "Step 0: <think>newer</think><action>PressBack()</action>" in query


def test_parser_accepts_official_single_and_double_quoted_content() -> None:
    assert parse_answer("Type(content='hello, world')") == (
        "Type",
        {"content": "hello, world"},
    )
    assert parse_answer('Type(content="O\'Reilly, Inc.")') == (
        "Type",
        {"content": "O'Reilly, Inc."},
    )


def test_official_press_recent_output_is_an_explicit_non_crashing_unknown() -> None:
    action_name, params = parse_answer("PressRecent()")

    assert convert_venus_action_to_json_action(action_name, params, 400, 200) == {
        "action_type": "unknown",
        "text": "Unsupported UI-Venus action: PressRecent",
    }


def test_predict_uses_current_image_flat_history_and_official_bare_action_fallback(
    monkeypatch: Any,
) -> None:
    agent = _agent(monkeypatch)
    agent.history = [_step(think="inspect", action="Click(box=(100,200))")]
    observed_call: dict[str, Any] = {}

    def fake_completion(self: VenusNaviAgent, **kwargs: Any) -> str:
        observed_call.update(kwargs)
        return "PressBack()"

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    prediction, action = agent.predict(
        {
            "screenshot": Image.new("RGB", (200, 400), (10, 20, 30)),
            "tool_call": None,
            "ask_user_response": None,
        }
    )

    assert prediction == "PressBack()"
    assert action.action_type == "navigate_back"
    assert observed_call["model"] == "UI-Venus-1.5-8B"
    assert observed_call["max_tokens"] == 4096
    assert observed_call["temperature"] == 0.0
    assert observed_call["top_p"] == 1.0

    messages = observed_call["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    content = messages[1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert (
        "Step 0: <think>inspect</think><action>Click(box=(100,200))</action>" in content[0]["text"]
    )
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert (
        sum(
            part["type"] == "image_url"
            for message in messages
            for part in (message["content"] if isinstance(message["content"], list) else [])
        )
        == 1
    )
    assert agent.history[-1].action == "PressBack()"
    assert agent.history[-1].status == "success"


def test_ui_venus_request_is_losslessly_captured_at_the_provider_boundary(
    tmp_path: Path, monkeypatch: Any
) -> None:
    agent = _agent(monkeypatch)
    agent.history = [_step(think="inspect", action="Click(box=(100,200))")]
    client = _Client(
        _Response(
            "<think>go back</think><action>PressBack()</action><conclusion>went back</conclusion>"
        )
    )
    agent.openai_client = client

    run = RunRecorder(
        tmp_path / "audit",
        producer=Producer.local(version="test", worker_id="ui-venus-test"),
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

    try:
        with bind_audit_context(context):
            prediction, action = agent.predict(
                {"screenshot": Image.new("RGB", (200, 400), (10, 20, 30))}
            )

        assert prediction.startswith("<think>go back</think>")
        assert action.action_type == "navigate_back"
        provider_payload = client.chat.completions.calls[0]
        events = [json.loads(line) for line in task.path.read_text().splitlines()]
        requests = [event for event in events if event["event_type"] == "model_request"]
        responses = [event for event in events if event["event_type"] == "model_response"]
        assert len(requests) == len(responses) == 1

        request_payload = requests[0]["payload"]
        reconstructed = ArtifactSerializer(run.blob_store).rehydrate(
            request_payload["sdk_arguments_snapshot_blob"]
        )
        assert reconstructed == provider_payload
        assert request_payload["call_role"] == "actor"
        assert request_payload["component"].endswith("ui_venus_agent")
        assert len(request_payload["request_images"]) == 1
        assert context.source_model_call_ids() == (request_payload["model_call_id"],)
    finally:
        run.close()
