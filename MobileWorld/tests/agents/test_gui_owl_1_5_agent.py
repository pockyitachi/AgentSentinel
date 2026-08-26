from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.gui_owl_1_5 import (
    SCALE_FACTOR,
    GUIOWL15AgentMCP,
    parse_action_to_structure_output,
    parsing_response_to_andoid_world_env_action,
)
from mobile_world.agents.registry import AGENT_CONFIGS
from mobile_world.runtime.audit.context import (
    AuditContext,
    ModelCallTrace,
    bind_audit_context,
)
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.audit.serializer import ArtifactSerializer


def _agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_conf: dict[str, Any] | None = None,
) -> GUIOWL15AgentMCP:
    monkeypatch.setattr(BaseAgent, "build_openai_client", lambda *_args, **_kwargs: None)
    agent = GUIOWL15AgentMCP(
        model_name="GUI-Owl-1.5-8B-Instruct",
        llm_base_url="http://127.0.0.1:18008/v1",
        runtime_conf=runtime_conf,
        tools=[],
    )
    agent.initialize("Complete the GUI-Owl fixture task")
    return agent


def _tool_output(arguments: dict[str, Any], *, name: str = "mobile_use") -> str:
    return (
        'Action: "perform the fixture action"\n'
        "<tool_call>\n"
        f"{json.dumps({'name': name, 'arguments': arguments})}\n"
        "</tool_call>"
    )


def _seed_completed_turns(
    agent: GUIOWL15AgentMCP,
    observations: list[tuple[str, Any, Any]],
) -> None:
    for index, observation in enumerate(observations):
        agent.history_images.append(observation[0])
        agent.history_user_content.append(observation)
        agent.history_responses.append(f"raw-response-{index}")
        agent.thoughts.append(f"thought-{index}")
        agent.conclusions.append(f"action-{index}")
        agent.actions.append({"action_type": "wait"})


def _image_urls(messages: list[dict[str, Any]]) -> list[str]:
    return [
        part["image_url"]["url"]
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]


class _Response:
    def __init__(self, content: str, response_id: str) -> None:
        self.id = response_id
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
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return next(self.responses)


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.api_key = "empty"
        self.base_url = "http://127.0.0.1:18008/v1/"
        self.max_retries = 2
        self.timeout = 120.0
        self.chat = SimpleNamespace(completions=_Completions(responses))


def test_runtime_conf_is_copied_and_default_history_remains_current_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = {
        "history_n": 3,
        "temperature": 0.25,
        "top_p": 0.75,
        "max_tokens": 512,
        "seed": 7,
    }
    original = dict(supplied)

    configured = _agent(monkeypatch, runtime_conf=supplied)
    defaulted = _agent(monkeypatch)

    assert supplied == original
    assert configured.history_n == 3
    assert configured.temperature == 0.25
    assert configured.top_p == 0.75
    assert configured.max_tokens == 512
    assert configured.runtime_conf == {"seed": 7}
    assert defaulted.history_n == 1
    assert defaulted.runtime_conf == {}


@pytest.mark.parametrize("history_n", [0, -1, True, 1.5])
def test_runtime_conf_rejects_invalid_history_window(
    monkeypatch: pytest.MonkeyPatch,
    history_n: Any,
) -> None:
    with pytest.raises(ValueError, match="history_n"):
        _agent(monkeypatch, runtime_conf={"history_n": history_n})


@pytest.mark.parametrize(
    ("current_observation", "expected_result"),
    [
        (("current-image", "tool-result-for-action-0", None), "tool-result-for-action-0"),
        (
            ("current-image", None, "user-result-for-action-0"),
            "(Ask_user_response)user-result-for-action-0",
        ),
    ],
)
def test_history_n_one_collapses_action_with_its_following_result_and_current_image_only(
    monkeypatch: pytest.MonkeyPatch,
    current_observation: tuple[str, Any, Any],
    expected_result: str,
) -> None:
    agent = _agent(monkeypatch)
    _seed_completed_turns(agent, [("old-image", None, None)])

    messages = agent._build_messages(current_observation)

    assert [message["role"] for message in messages] == ["system", "user"]
    collapsed_text = messages[1]["content"][0]["text"]
    assert f"Step1: action-0. Tool response: {expected_result}" in collapsed_text
    assert "Tool response: None" not in collapsed_text
    assert _image_urls(messages) == ["data:image/png;base64,current-image"]


def test_history_n_three_has_one_collapsed_turn_two_raw_pairs_and_aligned_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch, runtime_conf={"history_n": 3})
    _seed_completed_turns(
        agent,
        [
            ("image-0", None, None),
            ("image-1", "result-for-action-0", None),
            ("image-2", None, "result-for-action-1"),
        ],
    )

    messages = agent._build_messages(("current-image", "result-for-action-2", None))

    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    collapsed_text = messages[1]["content"][0]["text"]
    assert "Step1: action-0. Tool response: result-for-action-0" in collapsed_text
    assert "action-1" not in collapsed_text
    assert messages[2]["content"][0]["text"] == "raw-response-1"
    assert "(Ask_user_response)result-for-action-1" in str(messages[3]["content"])
    assert messages[4]["content"][0]["text"] == "raw-response-2"
    assert "result-for-action-2" in str(messages[5]["content"])
    assert _image_urls(messages) == [
        "data:image/png;base64,image-1",
        "data:image/png;base64,image-2",
        "data:image/png;base64,current-image",
    ]


@pytest.mark.parametrize(
    ("payload", "error_match"),
    [
        ('{"name":"mobile_use","arguments":{}}', "closing tag"),
        ("[]</tool_call>", "JSON object"),
        ('{"arguments":{}}</tool_call>', "non-empty string 'name'"),
        ('{"name":"mobile_use","arguments":[]}</tool_call>', "object-valued 'arguments'"),
    ],
)
def test_parser_rejects_malformed_tool_call_structure(
    payload: str,
    error_match: str,
) -> None:
    output = f"Action: bad\n<tool_call>{payload}"

    with pytest.raises(ValueError, match=error_match):
        parse_action_to_structure_output(output)


def test_coordinate_scale_stays_999_and_clamps_every_point_to_image_bounds() -> None:
    assert SCALE_FACTOR == 999

    bottom_right = parse_action_to_structure_output(
        _tool_output({"action": "click", "coordinate": [999, 999]})
    )
    beyond_bottom_right = parse_action_to_structure_output(
        _tool_output({"action": "click", "coordinate": [1000, 1000]})
    )
    before_top_left = parse_action_to_structure_output(
        _tool_output({"action": "click", "coordinate": [-10, -10]})
    )
    midpoint_box = parse_action_to_structure_output(
        _tool_output({"action": "click", "coordinate": [0, 0, 999, 999]})
    )
    swipe = parse_action_to_structure_output(
        _tool_output(
            {
                "action": "swipe",
                "coordinate": [-10, 0],
                "coordinate2": [1000, 999],
            }
        )
    )

    assert parsing_response_to_andoid_world_env_action(bottom_right, 20, 10) == {
        "action_type": "click",
        "x": 9,
        "y": 19,
    }
    assert parsing_response_to_andoid_world_env_action(beyond_bottom_right, 20, 10) == {
        "action_type": "click",
        "x": 9,
        "y": 19,
    }
    assert parsing_response_to_andoid_world_env_action(before_top_left, 20, 10) == {
        "action_type": "click",
        "x": 0,
        "y": 0,
    }
    assert parsing_response_to_andoid_world_env_action(midpoint_box, 20, 10) == {
        "action_type": "click",
        "x": 5,
        "y": 10,
    }
    assert parsing_response_to_andoid_world_env_action(swipe, 20, 10) == {
        "action_type": "drag",
        "start_x": 0,
        "start_y": 0,
        "end_x": 9,
        "end_y": 19,
    }


@pytest.mark.parametrize(
    ("arguments", "expected_text"),
    [
        ({"action": "key", "text": "clear"}, "mobile_use action: 'key'"),
        (
            {"action": "system_button", "button": "Menu"},
            "system_button: Menu",
        ),
        ({"action": "not_supported"}, "mobile_use action: 'not_supported'"),
    ],
)
def test_unrepresentable_official_actions_are_factual_non_crashing_unknowns(
    arguments: dict[str, Any],
    expected_text: str,
) -> None:
    parsed = parse_action_to_structure_output(_tool_output(arguments))

    converted = parsing_response_to_andoid_world_env_action(parsed, 20, 10)

    assert converted["action_type"] == "unknown"
    assert expected_text in converted["text"]


def test_conversion_failure_retries_same_request_and_commits_only_accepted_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    outputs = iter(
        [
            _tool_output({"action": "click"}),
            _tool_output({"action": "click", "coordinate": []}),
            _tool_output({"action": "wait"}),
        ]
    )
    calls: list[dict[str, Any]] = []
    state_during_calls: list[tuple[int, ...]] = []

    def fake_completion(self: GUIOWL15AgentMCP, **kwargs: Any) -> str:
        calls.append(kwargs)
        state_during_calls.append(
            (
                len(self.actions),
                len(self.thoughts),
                len(self.conclusions),
                len(self.history_images),
                len(self.history_responses),
                len(self.history_user_content),
            )
        )
        return next(outputs)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    prediction, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert prediction == _tool_output({"action": "wait"})
    assert action.action_type == "wait"
    assert state_during_calls == [(0, 0, 0, 0, 0, 0)] * 3
    assert len({id(call["messages"]) for call in calls}) == 1
    assert [call["retry_times"] for call in calls] == [3, 3, 3]
    assert agent.history_responses == [prediction]
    assert len(agent.actions) == len(agent.history_images) == len(agent.history_user_content) == 1


def test_exhausted_invalid_outputs_return_unknown_without_committing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_completion(self: GUIOWL15AgentMCP, **kwargs: Any) -> str:
        calls.append(kwargs)
        return "not a tool call"

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    prediction, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert len(calls) == 5
    assert len({id(call["messages"]) for call in calls}) == 1
    assert prediction.startswith("llm output invalid after multiple retries: ValueError")
    assert action.action_type == "unknown"
    assert "No <tool_call> block" in (action.text or "")
    assert (
        agent.actions
        == agent.thoughts
        == agent.conclusions
        == agent.history_images
        == agent.history_responses
        == agent.history_user_content
        == []
    )


def test_unsupported_action_commits_one_turn_then_reset_clears_all_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    calls: list[dict[str, Any]] = []

    def fake_completion(self: GUIOWL15AgentMCP, **kwargs: Any) -> str:
        calls.append(kwargs)
        return _tool_output({"action": "key", "text": "clear"})

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    _, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert len(calls) == 1
    assert action.action_type == "unknown"
    assert "'key'" in (action.text or "")
    assert len(agent.actions) == len(agent.history_responses) == 1

    agent.reset()

    assert (
        agent.actions
        == agent.thoughts
        == agent.conclusions
        == agent.history_images
        == agent.history_responses
        == agent.history_user_content
        == []
    )


@pytest.mark.parametrize(
    ("registered", "expected_action_type"),
    [(False, "unknown"), (True, "mcp")],
)
def test_non_mobile_tool_call_executes_only_when_the_environment_registered_it(
    monkeypatch: pytest.MonkeyPatch,
    registered: bool,
    expected_action_type: str,
) -> None:
    agent = _agent(monkeypatch)
    if registered:
        agent.tools = [{"name": "fixture_tool", "description": "fixture"}]

    def fake_completion(self: GUIOWL15AgentMCP, **kwargs: Any) -> str:
        return _tool_output({"value": 1}, name="fixture_tool")

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    _, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert action.action_type == expected_action_type
    if registered:
        assert action.action_name == "fixture_tool"
        assert action.action_json == {"value": 1}
    else:
        assert "Unregistered GUI-Owl tool call: fixture_tool" in (action.text or "")


def test_registry_resolves_gui_owl_adapter() -> None:
    assert AGENT_CONFIGS["gui_owl_1_5"]["class"] is GUIOWL15AgentMCP


def test_parse_retry_requests_are_losslessly_rehydratable_with_shared_correlation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    client = _Client(
        [
            _Response("malformed response", "gui-owl-malformed"),
            _Response(_tool_output({"action": "wait"}), "gui-owl-valid"),
        ]
    )
    agent.openai_client = client

    run = RunRecorder(
        tmp_path / "audit",
        producer=Producer.local(version="test", worker_id="gui-owl-test"),
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
                {"screenshot": Image.new("RGB", (10, 20), (10, 20, 30))}
            )

        assert prediction == _tool_output({"action": "wait"})
        assert action.action_type == "wait"
        events = [json.loads(line) for line in task.path.read_text().splitlines()]
        requests = [event for event in events if event["event_type"] == "model_request"]
        responses = [event for event in events if event["event_type"] == "model_response"]
        assert len(requests) == len(responses) == len(client.chat.completions.calls) == 2

        serializer = ArtifactSerializer(run.blob_store)
        for event, provider_payload in zip(
            requests,
            client.chat.completions.calls,
            strict=True,
        ):
            request_payload = event["payload"]
            reconstructed = serializer.rehydrate(request_payload["sdk_arguments_snapshot_blob"])
            assert reconstructed == provider_payload
            assert request_payload["call_role"] == "actor"
            assert request_payload["component"].endswith("gui_owl_1_5")
            assert len(request_payload["request_images"]) == 1

        request_payloads = [event["payload"] for event in requests]
        assert [payload["adapter_attempt_index"] for payload in request_payloads] == [1, 2]
        assert len({payload["retry_group_id"] for payload in request_payloads}) == 1
        assert len({payload["model_call_id"] for payload in request_payloads}) == 2
        assert len({id(call["messages"]) for call in client.chat.completions.calls}) == 1
        assert context.source_model_call_ids() == tuple(
            payload["model_call_id"] for payload in request_payloads
        )
        assert agent.history_responses == [prediction]
    finally:
        run.close()
