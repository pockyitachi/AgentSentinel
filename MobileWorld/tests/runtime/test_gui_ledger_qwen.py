"""CPU-only checks at the real Qwen prompt/parser boundary."""

import json
from copy import deepcopy
from typing import Any

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations.qwen3vl import (
    Qwen3VLAgentMCP,
    parse_action_to_structure_output,
)
from mobile_world.runtime.gui_ledger import GuiLedger

RESPONSE = (
    'Thought: Check the screen.\nAction: "Tap the button"\n'
    '<tool_call>{"name":"mobile_use","arguments":'
    '{"action":"click","coordinate":[500,500]}}</tool_call>'
)


def _response(arguments: dict[str, Any]) -> str:
    tool_call = json.dumps({"name": "mobile_use", "arguments": arguments})
    return f'Thought: Check the screen.\nAction: "Use the requested control"\n<tool_call>{tool_call}</tool_call>'


def _agent(monkeypatch: pytest.MonkeyPatch, responses: list[str] | None = None):
    agent = Qwen3VLAgentMCP.__new__(Qwen3VLAgentMCP)
    BaseAgent.__init__(agent)
    agent.model_name = "fake-qwen"
    agent.runtime_conf = {"temperature": 0.0}
    agent.instruction = "Open the requested page"
    agent.tools = []
    agent.reset()
    requests: list[dict[str, Any]] = []
    messages_ids: list[int] = []
    pending = list(responses or [RESPONSE] * 8)

    def completion(**kwargs):
        messages_ids.append(id(kwargs["messages"]))
        requests.append(deepcopy(kwargs))
        return pending.pop(0)

    monkeypatch.setattr(agent, "openai_chat_completions_create", completion)
    return agent, requests, messages_ids


def test_inform_changes_only_one_trailing_text_block_and_not_history(monkeypatch):
    image = Image.new("RGB", (12, 8))
    plain, plain_requests, _ = _agent(monkeypatch)
    enabled, enabled_requests, _ = _agent(monkeypatch)
    ledger = GuiLedger("Open the requested page")
    ledger.observe(0, image)
    inform = ledger.render_inform()
    observation = {"screenshot": image, "ledger_inform": inform}
    _, plain_action = plain.predict({"screenshot": image})
    _, enabled_action = enabled.predict(observation)

    plain_request = plain_requests[0]
    request = enabled_requests[0]
    assert request["messages"][-1]["content"].pop() == {"type": "text", "text": inform}
    assert request == plain_request
    assert enabled_action == plain_action
    assert observation == {"screenshot": image, "ledger_inform": inform}
    assert enabled.conclusions == plain.conclusions == ["Tap the button"]
    assert enabled.history_responses == plain.history_responses == [RESPONSE]
    enabled.predict({"screenshot": image})
    assert "GUI Ledger" not in repr(enabled_requests[1])


def test_parse_retries_reuse_inform_and_deliver_prior_nudge_once(monkeypatch):
    image = Image.new("RGB", (12, 8))
    agent, requests, ids = _agent(monkeypatch, [RESPONSE, "bad", "bad", RESPONSE, RESPONSE])
    agent.predict({"screenshot": image})
    notice = "GUI Ledger: repeated sample. This action was still executed."
    agent.predict({"screenshot": image, "ledger_inform": "one-time inform", "ledger_nudge": notice})
    assert len(requests) == 4  # Original decision plus normal malformed-output retries.
    assert len(set(ids[1:])) == 1
    assert requests[1] == requests[2] == requests[3]
    assert repr(requests[1]).count(notice) == 1
    assert agent.conclusions[0].count(notice) == 1
    assert len(agent.conclusions) == 2
    assert "one-time inform" not in repr(agent.conclusions)
    agent.predict({"screenshot": image})
    assert notice in repr(requests[-1])
    assert "one-time inform" not in repr(requests[-1])
    agent.reset()
    assert agent.conclusions == []


@pytest.mark.parametrize("field,limit", [("ledger_inform", 16384), ("ledger_nudge", 2048)])
@pytest.mark.parametrize("invalid", [None, 5, {}, "oversized"])
def test_bad_optional_hint_cannot_break_qwen(monkeypatch, field, limit, invalid):
    agent, requests, _ = _agent(monkeypatch)
    image = Image.new("RGB", (12, 8))
    agent.predict({"screenshot": image})
    value = "x" * (limit + 1) if invalid == "oversized" else invalid
    agent.predict({"screenshot": image, field: value})
    assert len(requests) == 2
    assert len(requests[-1]["messages"][-1]["content"]) == 2
    assert agent.conclusions == ["Tap the button", "Tap the button"]


def test_no_legacy_hook_or_model_client_is_required(monkeypatch):
    agent, requests, _ = _agent(monkeypatch)
    assert not hasattr(agent, "_prompt_sentinel")
    assert not hasattr(agent, "_sentinel_logical_call_scope")
    agent.predict({"screenshot": Image.new("RGB", (12, 8)), "ledger_inform": "local facts"})
    assert len(requests) == 1
    assert requests[0]["model"] == "fake-qwen"


@pytest.mark.parametrize(
    "arguments,executor_kind,label",
    [
        (
            {"action": "system_button", "button": "Home"},
            "navigate_home",
            'system_button (button="Home")',
        ),
        (
            {"action": "system_button", "button": "Back"},
            "navigate_back",
            'system_button (button="Back")',
        ),
        (
            {"action": "system_button", "button": "Enter"},
            "keyboard_enter",
            'system_button (button="Enter")',
        ),
        ({"action": "type", "text": "private-input-837291"}, "input_text", "type"),
        (
            {"action": "swipe", "coordinate": [137, 291], "coordinate2": [682, 947]},
            "drag",
            "swipe",
        ),
        ({"action": "click", "coordinate": [137, 291]}, "click", "click"),
        ({"action": "long_press", "coordinate": [137, 291]}, "long_press", "long_press"),
        ({"action": "answer", "text": "private-answer-837291"}, "answer", "answer"),
        ({"action": "wait", "time": 8}, "wait", "wait"),
        ({"action": "ask_user", "text": "private-question-837291"}, "ask_user", "ask_user"),
        ({"action": "terminate", "status": "success"}, "finished", "terminate"),
        (
            {"action": "open", "text": "private-app-837291"},
            "open_app",
            "GUI operation (label omitted)",
        ),
    ],
)
def test_real_parser_execution_is_projected_to_safe_qwen_inform_only(
    monkeypatch, arguments, executor_kind, label
):
    """The execution vocabulary stays internal across the full Qwen seam."""
    image = Image.new("RGB", (1080, 2400))
    original_pixels = image.tobytes()
    first_response = _response(arguments)
    plain, plain_requests, _ = _agent(monkeypatch, [first_response, RESPONSE])
    enabled, enabled_requests, _ = _agent(monkeypatch, [first_response, RESPONSE])
    _, plain_action = plain.predict({"screenshot": image})
    _, executed_action = enabled.predict({"screenshot": image})
    assert executed_action == plain_action
    assert executed_action.action_type == executor_kind
    original_action = executed_action.model_dump()

    ledger = GuiLedger(enabled.instruction)
    ledger.observe(1, image)
    decision = ledger.govern(1, executed_action.model_dump())
    ledger.record_transition(
        1,
        executed_action.model_dump(),
        outcome="returned",
        screenshot=image,
        decision=decision,
    )
    ledger.observe(2, image)
    inform = ledger.render_inform()
    command_line = next(line for line in inform.splitlines() if line.startswith("- Attempt step"))
    assert command_line == (
        f"- Attempt step 1: {label}; executor=returned; sampled image=SAME (O1 -> O1)."
    )
    if executor_kind != label:
        assert executor_kind not in command_line
    assert "private-" not in inform
    assert "837291" not in inform
    assert not any(
        key in command_line for key in ("coordinate", "start_x", "start_y", "end_x", "end_y")
    )
    assert executed_action.model_dump() == original_action

    _, plain_next = plain.predict({"screenshot": image})
    observation = {"screenshot": image, "ledger_inform": inform}
    _, enabled_next = enabled.predict(observation)
    assert len(plain_requests) == len(enabled_requests) == 2
    assert enabled_requests[0] == plain_requests[0]
    final_request = deepcopy(enabled_requests[1])
    assert final_request["messages"][-1]["content"].pop() == {"type": "text", "text": inform}
    # Includes exact system/tool schema, task, native history, image, and sampling fields.
    assert final_request == plain_requests[1]
    assert enabled_next == plain_next
    assert enabled.actions == plain.actions
    assert enabled.conclusions == plain.conclusions
    assert enabled.history_responses == plain.history_responses
    assert "GUI Ledger" not in repr(enabled.conclusions)
    assert observation == {"screenshot": image, "ledger_inform": inform}
    assert image.tobytes() == original_pixels


@pytest.mark.parametrize(
    "unsupported",
    [
        "navigate_home",
        "navigate_back",
        "keyboard_enter",
        "input_text",
        "drag",
        "open_app",
        "finished",
        "invented_action",
    ],
)
@pytest.mark.parametrize("with_inform", [False, True], ids=["off", "inform"])
def test_ledger_label_fix_does_not_change_original_unknown_action_handling(
    monkeypatch, unsupported, with_inform
):
    unknown = _response({"action": unsupported})
    assert parse_action_to_structure_output(unknown)["action_json"] == {"action": unsupported}

    home = _response({"action": "system_button", "button": "Home"})
    agent, requests, _ = _agent(monkeypatch, [home, unknown, RESPONSE])
    image = Image.new("RGB", (12, 8))
    _, executed_action = agent.predict({"screenshot": image})
    ledger = GuiLedger(agent.instruction)
    ledger.observe(1, image)
    ledger.record_transition(
        1,
        executed_action.model_dump(),
        outcome="returned",
        screenshot=image,
        decision=ledger.govern(1, executed_action.model_dump()),
    )
    ledger.observe(2, image)
    inform = ledger.render_inform()
    before = ledger.summary()
    observation = {"screenshot": image}
    if with_inform:
        observation["ledger_inform"] = inform
    _, action = agent.predict(observation)
    assert 'system_button (button="Home")' in inform
    assert "navigate_home" not in inform
    assert len(requests) == 2  # Original host does not parse-retry unknown action names.
    assert len(requests[1]["messages"][-1]["content"]) == (3 if with_inform else 2)
    assert ledger.summary() == before
    # Preserve this pre-existing quirk; neither reject nor fabricate a wait.
    assert action.action_type is None
    assert agent.actions == [{"action_type": "navigate_home"}, {}]
    assert agent.history_responses == [home, unknown]
    assert agent.conclusions == ["Use the requested control", "Use the requested control"]
