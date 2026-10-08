"""CPU-only regressions for the pre-Sentinel audited Qwen host at 9f10c01.

Known host quirks are intentionally preserved: Ledger is not a host repair.
"""

import hashlib
import json
from copy import deepcopy
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.qwen3vl import (
    Qwen3VLAgentMCP,
    parse_action_to_structure_output,
    parsing_response_to_andoid_world_env_action,
)
from mobile_world.agents.utils.helpers import pil_to_base64
from mobile_world.agents.utils.prompts.qwen3vl import (
    MOBILE_QWEN3VL_ORIGINAL_PROMPT,
    MOBILE_QWEN3VL_PROMPT_WITH_ASK_USER,
    MOBILE_QWEN3VL_USER_TEMPLATE,
)


def _response(arguments, *, name="mobile_use"):
    tool_call = json.dumps({"name": name, "arguments": arguments})
    return (
        f'Thought: Check the screen.\nAction: "Use the control"\n<tool_call>{tool_call}</tool_call>'
    )


def _convert(arguments):
    return parsing_response_to_andoid_world_env_action(
        parse_action_to_structure_output(_response(arguments)), 999, 999
    )


@pytest.mark.parametrize(
    "template,expected_hash",
    [
        (
            MOBILE_QWEN3VL_PROMPT_WITH_ASK_USER,
            "60a17f20e0e4de835199ca743c19d58c41aca4c861b635fa60a1b007b3225d95",
        ),
        (
            MOBILE_QWEN3VL_ORIGINAL_PROMPT,
            "f39ac8e884a982de0bc14a8509a5f00b546be046dff9ecabab7cc97f428c202c",
        ),
    ],
)
def test_prompt_bytes_match_original_host_including_known_schema_mismatches(
    template, expected_hash
):
    # Literal hashes from 9f10c01, independent of the runtime under test.
    rendered = template.render(tools="")
    assert hashlib.sha256(rendered.encode()).hexdigest() == expected_hash
    tools_text = rendered.split("<tools>\n", 1)[1].split("</tools>", 1)[0].strip()
    properties = json.loads(tools_text)["function"]["parameters"]["properties"]
    assert properties["button"]["enum"] == ["Back", "Home", "Menu", "Enter"]
    assert properties["time"]["type"] == "number"
    assert "specified seconds" in properties["action"]["description"]
    assert hashlib.sha256(MOBILE_QWEN3VL_USER_TEMPLATE.encode()).hexdigest() == (
        "b185fefac751685121e8754dcb77dbdf572e8fe0e2ea61639e9b9d5656e7fc81"
    )


@pytest.mark.parametrize(
    "action,kind,field",
    [
        ("type", "input_text", "text"),
        ("ask_user", "ask_user", "text"),
        ("answer", "answer", "text"),
        ("open", "open_app", "app_name"),
    ],
)
@pytest.mark.parametrize("text", ["example", " \t\n", "", None, 17, True, [], {}])
def test_original_text_default_and_passthrough(action, kind, field, text):
    assert _convert({"action": action}) == {"action_type": kind, field: ""}
    assert _convert({"action": action, "text": text}) == {"action_type": kind, field: text}


@pytest.mark.parametrize("status", ["success", "failure", "done", "", None, 1, True, [], {}])
def test_original_terminate_default_and_unvalidated_status(status):
    assert _convert({"action": "terminate"}) == {"action_type": "finished", "text": ""}
    assert _convert({"action": "terminate", "status": status}) == {
        "action_type": "finished",
        "text": status,
    }


@pytest.mark.parametrize("action", ["click", "long_press"])
@pytest.mark.parametrize("coordinate", [[200, 400], [0, 200, 400, 600]])
def test_point_and_box_coordinate_conversion_is_unchanged(action, coordinate):
    assert _convert({"action": action, "coordinate": coordinate}) == {
        "action_type": action,
        "x": 200,
        "y": 400,
    }


@pytest.mark.parametrize("coordinate", [[200, 400], [0, 200, 400, 600]])
@pytest.mark.parametrize("coordinate2", [[799, 599], [599, 399, 999, 799]])
def test_swipe_endpoint_conversion_is_unchanged(coordinate, coordinate2):
    assert _convert({"action": "swipe", "coordinate": coordinate, "coordinate2": coordinate2}) == {
        "action_type": "drag",
        "start_x": 200,
        "start_y": 400,
        "end_x": 799,
        "end_y": 599,
    }


@pytest.mark.parametrize("time", [None, 1, 8, 0.5, -2, "old", {}])
def test_advertised_time_is_still_ignored_by_original_converter(time):
    assert _convert({"action": "wait", "time": time}) == {"action_type": "wait"}
    assert _convert({"action": "long_press", "coordinate": [200, 400], "time": time}) == {
        "action_type": "long_press",
        "x": 200,
        "y": 400,
    }


def _agent(monkeypatch, responses):
    # No provider/client construction: every outcome is local.
    agent = Qwen3VLAgentMCP.__new__(Qwen3VLAgentMCP)
    BaseAgent.__init__(agent)
    agent.model_name = "offline-fake-qwen"
    agent.runtime_conf = {"temperature": 0.0}
    agent.instruction = "Open the requested page"
    agent.tools = []
    agent.reset()
    requests: list[dict[str, Any]] = []
    ids = []
    pending = iter(responses)

    def completion(**kwargs):
        ids.append(id(kwargs["messages"]))
        requests.append(deepcopy(kwargs))
        result = next(pending)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(agent, "openai_chat_completions_create", completion)
    return agent, requests, ids


def _observation(with_inform):
    observation = {"screenshot": Image.new("RGB", (12, 8))}
    if with_inform:
        observation["ledger_inform"] = "this-decision-only facts"
    return observation


def test_off_request_and_native_history_match_original_host(monkeypatch):
    response = _response({"action": "wait"})
    agent, requests, _ = _agent(monkeypatch, [response, response])
    image = Image.new("RGB", (12, 8))
    agent.predict({"screenshot": image})
    agent.predict({"screenshot": image, "tool_call": {"ok": True}, "ask_user_response": "yes"})
    conclusion = (
        'Use the control; Tool call result: <tool_response>{"ok": true}</tool_response>'
        "; Ask user response: yes"
    )
    assert agent.conclusions == [conclusion, "Use the control"]
    assert agent.actions == [{"action_type": "wait"}] * 2
    assert agent.thoughts == ["Check the screen."] * 2
    assert agent.history_responses == [response] * 2
    assert len(agent.history_images) == 2
    assert len(requests) == 2
    progress = conclusion.replace('"', "")
    for index, steps in enumerate(["", f"Step 1: {progress}; "]):
        request = requests[index]
        assert set(request) == {"model", "messages", "retry_times", "temperature"}
        assert (request["model"], request["retry_times"], request["temperature"]) == (
            "offline-fake-qwen",
            3,
            0.0,
        )
        system, user = request["messages"]
        assert system["role"] == "system"
        assert len(system["content"]) == 1
        assert hashlib.sha256(system["content"][0]["text"].encode()).hexdigest() == (
            "60a17f20e0e4de835199ca743c19d58c41aca4c861b635fa60a1b007b3225d95"
        )
        assert user == {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "\nThe user query: Open the requested page\n"
                        "Task progress (You have done the following operation on the current device): "
                        f"{steps}\n"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{pil_to_base64(image)}"},
                },
            ],
        }


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize(
    "invalid", ["bad", _response({"action": "click", "coordinate": [1, 2, 3]})]
)
def test_original_parse_retries_reuse_request_without_history_commit(
    monkeypatch, with_inform, recover, invalid
):
    valid = _response({"action": "wait"})
    agent, requests, ids = _agent(monkeypatch, [invalid] * 3 + [valid if recover else invalid])
    if recover:
        result, action = agent.predict(_observation(with_inform))
        assert result == valid
        assert action.action_type == "wait"
        assert agent.actions == [{"action_type": "wait"}]
        assert agent.history_responses == [valid]
        assert agent.conclusions == ["Use the control"]
    else:
        with pytest.raises(Exception, match="Failed to parse response after maximum retries"):
            agent.predict(_observation(with_inform))
        assert agent.actions == agent.thoughts == agent.conclusions == agent.history_responses == []
    assert len(requests) == 4
    assert len(set(ids)) == 1
    assert all(request == requests[0] for request in requests)
    assert len(requests[0]["messages"][-1]["content"]) == (3 if with_inform else 2)
    assert len(agent.history_images) == 1


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
@pytest.mark.parametrize(
    "arguments,match",
    [
        ({"action": "system_button", "button": "Menu"}, "Unsupported button: Menu"),
        ({"action": "click"}, "Invalid action_type: click"),
        ({"action": "swipe"}, "Invalid scroll box format"),
    ],
)
def test_conversion_errors_keep_original_post_history_commit_without_retry(
    monkeypatch, with_inform, arguments, match
):
    response = _response(arguments)
    agent, requests, _ = _agent(monkeypatch, [response])
    with pytest.raises(ValueError, match=match):
        agent.predict(_observation(with_inform))
    assert len(requests) == 1
    assert agent.history_responses == [response]
    assert agent.thoughts == ["Check the screen."]
    assert agent.conclusions == ["Use the control"]
    assert agent.actions == []


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
@pytest.mark.parametrize(
    "arguments,kind,text",
    [
        ({"action": "type"}, "input_text", ""),
        ({"action": "type", "text": 123}, "input_text", "123"),
        ({"action": "ask_user", "text": None}, "ask_user", None),
        ({"action": "answer"}, "answer", ""),
        ({"action": "terminate"}, "finished", ""),
        ({"action": "terminate", "status": "done"}, "finished", "done"),
    ],
)
def test_predict_keeps_original_defaulting_and_json_action_coercion(
    monkeypatch, with_inform, arguments, kind, text
):
    response = _response(arguments)
    agent, requests, _ = _agent(monkeypatch, [response])
    _, action = agent.predict(_observation(with_inform))
    assert (action.action_type, action.text) == (kind, text)
    assert agent.actions == [_convert(arguments)]
    assert agent.history_responses == [response]
    assert len(requests) == 1


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
def test_json_action_validation_remains_after_native_action_append(monkeypatch, with_inform):
    response = _response({"action": "open", "text": {"invalid": "app-name"}})
    agent, requests, _ = _agent(monkeypatch, [response])
    with pytest.raises(ValueError, match="app_name"):
        agent.predict(_observation(with_inform))
    assert len(requests) == 1
    assert agent.history_responses == [response]
    assert agent.conclusions == ["Use the control"]
    assert agent.actions == [{"action_type": "open_app", "app_name": {"invalid": "app-name"}}]


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
@pytest.mark.parametrize("result", [None, RuntimeError("offline provider error")])
def test_provider_failures_exit_without_adapter_parse_retry(monkeypatch, with_inform, result):
    agent, requests, _ = _agent(monkeypatch, [result])
    match = "Error when fetching response" if result is None else "offline provider error"
    with pytest.raises(Exception, match=match):
        agent.predict(_observation(with_inform))
    assert len(requests) == 1
    assert agent.actions == agent.thoughts == agent.conclusions == agent.history_responses == []
    assert len(agent.history_images) == 1


@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
def test_non_mobile_tools_keep_original_mcp_route(monkeypatch, with_inform):
    arguments = {"query": "preserved query"}
    response = _response(arguments, name="custom_tool")
    agent, requests, _ = _agent(monkeypatch, [response])
    _, action = agent.predict(_observation(with_inform))
    assert action.action_type == "mcp"
    assert action.action_name == "custom_tool"
    assert action.action_json == arguments
    assert agent.actions == [{"action_name": "custom_tool", "action_args": arguments}]
    assert len(requests) == 1
