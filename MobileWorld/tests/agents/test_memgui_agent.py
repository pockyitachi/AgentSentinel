from __future__ import annotations

import json
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.memgui_agent import (
    MEMGUI_COORD_SCALE,
    MemGUIAgent,
    _validate_folding_directive,
    build_json_action,
    parse_memgui_response,
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

_NO_FOLDING = object()


def _agent(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_conf: dict[str, Any] | None = None,
) -> MemGUIAgent:
    monkeypatch.setattr(BaseAgent, "build_openai_client", lambda *_args, **_kwargs: None)
    agent = MemGUIAgent(
        model_name="MemGUI-8B-SFT",
        llm_base_url="http://127.0.0.1:18007/v1",
        runtime_conf=runtime_conf,
        tools=[],
    )
    agent.initialize("Complete the MemGUI fixture task")
    return agent


def _model_output(
    arguments: dict[str, Any],
    *,
    folding: dict[str, Any] | object = _NO_FOLDING,
    name: str = "mobile_use",
    thinking: str = "Inspect the current screen.",
    ui_observation: str = "The fixture screen is visible.",
    action_intent: str = "Perform the fixture action.",
) -> str:
    parts = [f"<thinking>{thinking}</thinking>"]
    if folding is not _NO_FOLDING:
        parts.append(f"<folding>{json.dumps(folding)}</folding>")
    parts.extend(
        [
            "<tool_call>" + json.dumps({"name": name, "arguments": arguments}) + "</tool_call>",
            f"<ui_observation>{ui_observation}</ui_observation>",
            f"<action_intent>{action_intent}</action_intent>",
        ]
    )
    return "\n".join(parts)


def _image_urls(messages: list[dict[str, Any]]) -> list[str]:
    return [
        part["image_url"]["url"]
        for message in messages
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]


class _Response:
    def __init__(self, content: str, response_id: str = "memgui-fixture-response") -> None:
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
        self.base_url = "http://127.0.0.1:18007/v1/"
        self.max_retries = 2
        self.timeout = 120.0
        self.chat = SimpleNamespace(completions=_Completions(responses))


def test_runtime_conf_is_copied_and_keeps_mobileworld_temperature_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied = {"temperature": 0.25, "top_p": 0.8, "max_tokens": 1024}
    original = dict(supplied)

    configured = _agent(monkeypatch, runtime_conf=supplied)
    defaulted = _agent(monkeypatch)

    assert supplied == original
    assert configured.runtime_conf == supplied
    assert configured.runtime_conf is not supplied
    assert defaulted.runtime_conf == {"temperature": 0.0}


@pytest.mark.parametrize(
    ("tool_payload", "error_match"),
    [
        ("[]", "JSON object"),
        (json.dumps({"arguments": {"action": "wait"}}), "non-empty string 'name'"),
        (json.dumps({"name": "mobile_use", "arguments": []}), "object-valued 'arguments'"),
        (json.dumps({"name": "mobile_use", "arguments": {}}), "non-empty string 'action'"),
    ],
)
def test_parser_rejects_malformed_tool_call_structure(
    tool_payload: str,
    error_match: str,
) -> None:
    output = (
        "<thinking>inspect</thinking>\n"
        f"<tool_call>{tool_payload}</tool_call>\n"
        "<ui_observation>screen</ui_observation>\n"
        "<action_intent>wait</action_intent>"
    )

    with pytest.raises(ValueError, match=error_match):
        parse_memgui_response(output, image_height=20, image_width=10, current_step=1)


@pytest.mark.parametrize(
    ("output", "error_match"),
    [
        (_model_output({"action": "wait"}, name="hallucinated_tool"), "tool call"),
        (_model_output({"action": "hallucinated_action"}), "MemGUI action"),
        (
            _model_output({"action": "system_button", "button": "Recent"}),
            "system_button",
        ),
    ],
)
def test_parser_rejects_values_outside_the_prompt_tool_contract(
    output: str,
    error_match: str,
) -> None:
    with pytest.raises(ValueError, match=error_match):
        parse_memgui_response(output, image_height=20, image_width=10, current_step=1)


@pytest.mark.parametrize(
    ("arguments", "error_match"),
    [
        ({"action": "memory_add", "content": "value"}, "memory_id"),
        ({"action": "memory_add", "memory_id": "id"}, "content"),
        ({"action": "memory_update", "memory_id": "id", "content": 7}, "content"),
        ({"action": "memory_delete"}, "memory_id"),
    ],
)
def test_parser_rejects_invalid_memory_operations(
    arguments: dict[str, Any],
    error_match: str,
) -> None:
    with pytest.raises(ValueError, match=error_match):
        parse_memgui_response(
            _model_output(arguments),
            image_height=20,
            image_width=10,
            current_step=1,
        )


@pytest.mark.parametrize(
    ("coordinate", "error_match"),
    [
        ([1], "two-element list"),
        ([1, 2, 3], "two-element list"),
        ("1,2", "two-element list"),
        ([True, 2], "finite numbers"),
        (["1", 2], "finite numbers"),
        ([float("inf"), 2], "finite numbers"),
    ],
)
def test_parser_rejects_invalid_coordinates(
    coordinate: Any,
    error_match: str,
) -> None:
    with pytest.raises(ValueError, match=error_match):
        parse_memgui_response(
            _model_output({"action": "click", "coordinate": coordinate}),
            image_height=20,
            image_width=10,
            current_step=1,
        )


def test_parser_requires_complete_core_tags_but_accepts_training_order() -> None:
    official_training_order = (
        "<thinking>inspect</thinking>\n"
        "<ui_observation>screen</ui_observation>\n"
        "<action_intent>wait</action_intent>\n"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
    )

    parsed = parse_memgui_response(
        official_training_order,
        image_height=20,
        image_width=10,
        current_step=1,
    )
    assert parsed["action_json"] == {"action": "wait"}

    with pytest.raises(ValueError, match="ui_observation"):
        parse_memgui_response(
            official_training_order.replace(
                "<ui_observation>screen</ui_observation>\n",
                "",
            ),
            image_height=20,
            image_width=10,
            current_step=1,
        )


def test_parser_requires_folding_from_step_two() -> None:
    with pytest.raises(ValueError, match="required from step 2"):
        parse_memgui_response(
            _model_output({"action": "wait"}),
            image_height=20,
            image_width=10,
            current_step=2,
        )


@pytest.mark.parametrize(
    ("folding_text", "error_match"),
    [
        ('{"range":[1,1],"summary":"unfinished"', "parse <folding> JSON"),
        ("[]", "JSON object"),
        ('{"range":[0,1],"summary":"bad"}', "Invalid folding range"),
        ('{"range":[2,1],"summary":"bad"}', "Invalid folding range"),
        ('{"range":[1,3],"summary":"future"}', "Invalid folding range"),
        ('{"range":[1,1],"summary":""}', "non-empty string"),
    ],
)
def test_parser_rejects_malformed_or_out_of_bounds_folding(
    folding_text: str,
    error_match: str,
) -> None:
    output = _model_output({"action": "wait"}).replace(
        "<tool_call>",
        f"<folding>{folding_text}</folding>\n<tool_call>",
    )

    with pytest.raises(ValueError, match=error_match):
        parse_memgui_response(output, image_height=20, image_width=10, current_step=2)


def test_folding_preserves_upstream_destructive_overlap_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    agent.current_step = 4
    agent.state_summaries = [(1, 3, "[Steps 1-3] Existing summary")]

    directive = {"range": [3, 4], "summary": "[Steps 3-4] Partial rewrite"}
    _validate_folding_directive(directive, current_step=agent.current_step)
    agent._apply_folding_directive(directive)

    # The official adapter removes every overlapping record, including the
    # uncovered part of a partially overlapped span.  Preserve this native
    # history representation so the experiment does not introduce a new policy.
    assert agent.state_summaries == [(3, 4, "[Steps 3-4] Partial rewrite")]


def test_coordinates_stay_1000_scale_and_clamp_to_image_bounds() -> None:
    assert MEMGUI_COORD_SCALE == 1000

    bottom_right = parse_memgui_response(
        _model_output({"action": "click", "coordinate": [1000, 1000]}),
        image_height=20,
        image_width=10,
        current_step=1,
    )
    outside = parse_memgui_response(
        _model_output(
            {
                "action": "swipe",
                "coordinate": [-10, 0],
                "coordinate2": [1100, 1000],
            }
        ),
        image_height=20,
        image_width=10,
        current_step=1,
    )

    assert build_json_action(bottom_right, image_height=20, image_width=10).model_dump(
        exclude_none=True
    ) == {"action_type": "click", "x": 9, "y": 19}
    assert build_json_action(outside, image_height=20, image_width=10).model_dump(
        exclude_none=True
    ) == {
        "action_type": "drag",
        "start_x": 0,
        "start_y": 0,
        "end_x": 9,
        "end_y": 19,
    }


def test_prompt_advertised_menu_and_defensive_unknown_tool_are_non_crashing_unknowns() -> None:
    menu = parse_memgui_response(
        _model_output({"action": "system_button", "button": "Menu"}),
        image_height=20,
        image_width=10,
        current_step=1,
    )
    unknown_tool = {
        "action_name": "hallucinated_tool",
        "action_json": {"action": "wait"},
    }

    menu_action = build_json_action(menu, image_height=20, image_width=10)
    tool_action = build_json_action(unknown_tool, image_height=20, image_width=10)
    assert menu_action.action_type == "unknown"
    assert menu_action.text == "Unsupported MemGUI system_button: menu"
    assert tool_action.action_type == "unknown"
    assert tool_action.text == "Unsupported MemGUI tool call: hallucinated_tool"


def test_structured_context_uses_current_image_and_three_text_state_sections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            _model_output(
                {"action": "click", "coordinate": [200, 300]},
                ui_observation="Settings icon is visible.",
                action_intent="Open Settings.",
            ),
            _model_output(
                {
                    "action": "memory_add",
                    "memory_id": "target",
                    "description": "Target value",
                    "content": "exact-value-123",
                },
                folding={"range": [1, 1], "summary": "[Step 1] Opened Settings."},
                ui_observation="Settings is open.",
                action_intent="Remember the target value.",
            ),
            _model_output(
                {"action": "wait"},
                folding={
                    "range": [1, 2],
                    "summary": "[Steps 1-2] Opened Settings and saved the target.",
                },
            ),
        ]
    )

    def fake_completion(self: MemGUIAgent, **kwargs: Any) -> str:
        calls.append(kwargs)
        return next(responses)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    first_prediction, first_action = agent.predict(
        {"screenshot": Image.new("RGB", (10, 20), (10, 20, 30))}
    )
    second_prediction, second_action = agent.predict(
        {"screenshot": Image.new("RGB", (10, 20), (20, 30, 40))}
    )
    _, third_action = agent.predict({"screenshot": Image.new("RGB", (10, 20), (30, 40, 50))})

    assert first_action.action_type == "click"
    assert second_action.action_type == "wait"
    assert third_action.action_type == "wait"
    assert agent.history_responses[:2] == [first_prediction, second_prediction]
    assert agent.memory_state == {
        "target": {"description": "Target value", "content": "exact-value-123"}
    }
    assert agent.state_summaries == [(1, 2, "[Steps 1-2] Opened Settings and saved the target.")]

    assert [call["temperature"] for call in calls] == [0.0, 0.0, 0.0]
    for call in calls:
        assert [message["role"] for message in call["messages"]] == ["system", "user"]
        assert len(_image_urls(call["messages"])) == 1

    second_prompt = calls[1]["messages"][1]["content"][0]["text"]
    assert "(no previous steps)" in second_prompt
    assert "Step 1:" in second_prompt
    assert "UI Observation: Settings icon is visible." in second_prompt
    assert "Action Intent: Open Settings." in second_prompt
    assert "Action Taken: click" in second_prompt
    assert "(empty)" in second_prompt
    assert first_prediction not in second_prompt

    third_prompt = calls[2]["messages"][1]["content"][0]["text"]
    assert "[Step 1] Opened Settings." in third_prompt
    assert "Step 2:" in third_prompt
    assert "Memory: Added memory [target]" in third_prompt
    assert "[target]" in third_prompt
    assert "exact-value-123" in third_prompt
    assert first_prediction not in third_prompt
    assert second_prediction not in third_prompt


def test_missing_folding_retries_without_fabricating_previous_step_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    valid_folding = {
        "range": [1, 1],
        "summary": "[Step 1] Waited for the Settings page to load.",
    }
    responses = iter(
        [
            _model_output(
                {"action": "wait"},
                action_intent="Wait for the Settings page to load.",
            ),
            _model_output(
                {"action": "wait"},
                action_intent="Wrongly attributed current intent one.",
            ),
            _model_output(
                {"action": "wait"},
                action_intent="Wrongly attributed current intent two.",
            ),
            _model_output(
                {"action": "wait"},
                folding=valid_folding,
                action_intent="Inspect the newly loaded page.",
            ),
        ]
    )
    message_ids: list[int] = []

    def fake_completion(self: MemGUIAgent, **kwargs: Any) -> str:
        message_ids.append(id(kwargs["messages"]))
        return next(responses)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    observation = {"screenshot": Image.new("RGB", (10, 20))}
    agent.predict(observation)
    agent.predict(observation)

    assert agent.state_summaries == [(1, 1, "[Step 1] Waited for the Settings page to load.")]
    assert len(message_ids) == 4
    assert len(set(message_ids[1:])) == 1
    assert len(agent.history_responses) == 2
    assert "Wrongly attributed" not in "\n".join(agent.history_responses)


def test_parse_and_action_validation_retries_are_atomic_and_reuse_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    responses = iter(
        [
            _model_output({"action": "wait"}, name="hallucinated_tool"),
            _model_output({"action": "system_button", "button": "Menu"}),
            _model_output({"action": "wait"}),
        ]
    )
    message_ids: list[int] = []

    def fake_completion(self: MemGUIAgent, **kwargs: Any) -> str:
        message_ids.append(id(kwargs["messages"]))
        return next(responses)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    prediction, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert prediction == _model_output({"action": "wait"})
    assert action.action_type == "wait"
    assert len(message_ids) == 3
    assert len(set(message_ids)) == 1
    assert agent.current_step == 1
    assert agent.history_responses == [prediction]
    assert agent.thoughts == ["Inspect the current screen."]
    assert agent.ui_observations == ["The fixture screen is visible."]
    assert agent.action_intents == ["Perform the fixture action."]
    assert agent.latest_interaction == {
        "step": 1,
        "ui_observation": "The fixture screen is visible.",
        "action_intent": "Perform the fixture action.",
        "action_summary": "wait",
    }


def test_invalid_memory_retries_before_folding_or_memory_state_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    observation = {"screenshot": Image.new("RGB", (10, 20))}
    folding = {"range": [1, 1], "summary": "[Step 1] Waited."}
    responses = iter(
        [
            _model_output({"action": "wait"}),
            _model_output(
                {"action": "memory_add", "memory_id": "target"},
                folding=folding,
            ),
            _model_output(
                {
                    "action": "memory_add",
                    "memory_id": "target",
                    "content": "accepted value",
                },
                folding=folding,
            ),
        ]
    )
    message_ids: list[int] = []

    def fake_completion(self: MemGUIAgent, **kwargs: Any) -> str:
        message_ids.append(id(kwargs["messages"]))
        return next(responses)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    agent.predict(observation)
    prediction, action = agent.predict(observation)

    assert prediction.endswith("</action_intent>")
    assert action.action_type == "wait"
    assert len(message_ids) == 3
    assert message_ids[1] == message_ids[2]
    assert agent.state_summaries == [(1, 1, "[Step 1] Waited.")]
    assert agent.folding_stats == {
        "step_level_distillations": 1,
        "span_level_abstractions": 0,
        "total_steps_folded": 1,
    }
    assert agent.memory_state == {"target": {"description": "", "content": "accepted value"}}


@pytest.mark.parametrize(
    ("invalid_arguments", "initial_memory"),
    [
        (
            {
                "action": "memory_add",
                "memory_id": "target",
                "content": "must not overwrite",
            },
            {"target": {"description": "existing", "content": "original value"}},
        ),
        (
            {
                "action": "memory_update",
                "memory_id": "missing",
                "content": "must not create",
            },
            {},
        ),
        (
            {"action": "memory_delete", "memory_id": "missing"},
            {},
        ),
    ],
    ids=["duplicate-add", "missing-update", "missing-delete"],
)
def test_memory_state_preconditions_retry_without_partial_commit(
    invalid_arguments: dict[str, Any],
    initial_memory: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    observation = {"screenshot": Image.new("RGB", (10, 20))}
    folding = {"range": [1, 1], "summary": "[Step 1] Waited."}
    invalid_response = _model_output(invalid_arguments, folding=folding)
    responses = iter(
        [
            _model_output({"action": "wait"}),
            invalid_response,
            _model_output({"action": "wait"}, folding=folding),
        ]
    )
    expected_memory = {key: dict(value) for key, value in initial_memory.items()}
    message_ids: list[int] = []

    def fake_completion(self: MemGUIAgent, **kwargs: Any) -> str:
        message_ids.append(id(kwargs["messages"]))
        if len(message_ids) == 3:
            assert self.state_summaries == []
            assert self.folding_stats == {
                "step_level_distillations": 0,
                "span_level_abstractions": 0,
                "total_steps_folded": 0,
            }
            assert self.memory_state == expected_memory
            assert len(self.history_responses) == 1
        return next(responses)

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    agent.predict(observation)
    agent.memory_state = {key: dict(value) for key, value in initial_memory.items()}
    prediction, action = agent.predict(observation)

    assert action.action_type == "wait"
    assert len(message_ids) == 3
    assert message_ids[1] == message_ids[2]
    assert agent.state_summaries == [(1, 1, "[Step 1] Waited.")]
    assert agent.folding_stats == {
        "step_level_distillations": 1,
        "span_level_abstractions": 0,
        "total_steps_folded": 1,
    }
    assert agent.memory_state == expected_memory
    assert agent.history_responses == [_model_output({"action": "wait"}), prediction]
    assert invalid_response not in agent.history_responses


def test_exhausted_validation_returns_legal_unknown_without_state_pollution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    calls = 0

    def fake_completion(self: MemGUIAgent, **_kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        return _model_output({"action": "click"})

    agent.openai_chat_completions_create = MethodType(fake_completion, agent)
    prediction, action = agent.predict({"screenshot": Image.new("RGB", (10, 20))})

    assert calls == 3
    assert prediction.startswith("memgui output invalid after multiple retries:")
    assert action.action_type == "unknown"
    assert "requires a two-element coordinate" in (action.text or "")
    assert agent.history_responses == []
    assert agent.state_summaries == []
    assert agent.latest_interaction is None
    assert agent.memory_state == {}


def test_reset_clears_all_structured_history_state(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _agent(monkeypatch)
    agent.current_step = 4
    agent.state_summaries = [(1, 3, "summary")]
    agent.latest_interaction = {"step": 4}
    agent.memory_state = {"id": {"description": "desc", "content": "value"}}
    agent.history_responses = ["response"]
    agent.thoughts = ["thought"]
    agent.ui_observations = ["screen"]
    agent.action_intents = ["intent"]
    agent.folding_stats = {
        "step_level_distillations": 2,
        "span_level_abstractions": 1,
        "total_steps_folded": 4,
    }

    agent.reset()

    assert agent.current_step == 0
    assert agent.state_summaries == []
    assert agent.latest_interaction is None
    assert agent.memory_state == {}
    assert agent.history_responses == []
    assert agent.thoughts == []
    assert agent.ui_observations == []
    assert agent.action_intents == []
    assert agent.folding_stats == {
        "step_level_distillations": 0,
        "span_level_abstractions": 0,
        "total_steps_folded": 0,
    }


def test_registry_resolves_memgui_agent() -> None:
    assert AGENT_CONFIGS["memgui"]["class"] is MemGUIAgent


def test_memgui_request_is_losslessly_captured_at_provider_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = _agent(monkeypatch)
    agent.current_step = 1
    agent.state_summaries = [(1, 1, "[Step 1] Opened Settings.")]
    agent.latest_interaction = {
        "step": 1,
        "ui_observation": "Settings is open.",
        "action_intent": "Inspect Wi-Fi.",
        "action_summary": "click",
    }
    agent.memory_state = {"target": {"description": "Target network", "content": "Cafe Wi-Fi"}}
    response_text = _model_output(
        {"action": "wait"},
        folding={"range": [1, 1], "summary": "[Step 1] Opened Settings."},
    )
    client = _Client([_Response(response_text)])
    agent.openai_client = client

    run = RunRecorder(
        tmp_path / "audit",
        producer=Producer.local(version="test", worker_id="memgui-test"),
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

        assert prediction == response_text
        assert action.action_type == "wait"
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
        assert request_payload["component"].endswith("memgui_agent")
        assert len(request_payload["request_images"]) == 1
        assert context.source_model_call_ids() == (request_payload["model_call_id"],)

        prompt = reconstructed["messages"][1]["content"][0]["text"]
        assert "### Folded Action History" in prompt
        assert "### Recent Step Record" in prompt
        assert "### Folded UI State" in prompt
        assert "Cafe Wi-Fi" in prompt
    finally:
        run.close()
