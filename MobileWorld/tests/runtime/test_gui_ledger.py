"""CPU-only behavior tests for the deterministic GUI Ledger."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from mobile_world.runtime.gui_ledger import GovernDecision, GuiLedger

SWIPE = {"action_type": "swipe", "start_x": 10, "start_y": 30, "end_x": 10, "end_y": 5}
CLICK = {"action_type": "click", "x": 10, "y": 20}


def picture(color="red", *, mode="RGB", size=(8, 8)):
    return Image.new(mode, size, color)


def execute(ledger, step, action=SWIPE, *, before=None, after=None, outcome="returned"):
    ledger.observe(step, before)
    decision = ledger.govern(step, action)
    ledger.record_transition(step, action, outcome=outcome, screenshot=after, decision=decision)
    return decision


def test_stationary_repetition_nudges_proposed_third_action_without_spending_budget_early():
    ledger = GuiLedger("Find Alice")
    screen = picture()
    assert execute(ledger, 0, before=screen, after=screen).kind == "ALLOW"
    assert execute(ledger, 1, before=screen, after=screen).kind == "ALLOW"
    ledger.observe(2, screen)
    before = ledger.summary()
    decision = ledger.govern(2, SWIPE)
    assert decision.kind == "NUDGE"
    assert decision.reason == "REPEATED_ACTION_SAME_SAMPLED_IMAGE"
    assert "2 consecutive prior executions" in decision.notice
    assert ledger.govern(2, SWIPE) == decision
    assert ledger.summary() == before
    ledger.record_transition(2, SWIPE, outcome="returned", screenshot=screen, decision=decision)
    assert ledger.summary()["nudge_count"] == 1
    assert ledger.summary()["recorded_executions"] == 3


def test_same_action_moving_through_screens_is_not_a_loop():
    ledger = GuiLedger("Browse contacts")
    colors = ["red", "blue", "green", "white", "black"]
    for step in range(4):
        decision = execute(
            ledger, step, before=picture(colors[step]), after=picture(colors[step + 1])
        )
        assert decision.kind == "ALLOW"
    assert ledger.summary()["last_image_comparison"] == "CHANGED"
    assert "CHANGED does not establish task progress" in ledger.render_inform()


@pytest.mark.parametrize("missing", ["before", "after"])
def test_unknown_sample_breaks_repeat_evidence(missing):
    ledger = GuiLedger("Find Alice")
    screen = picture()
    execute(ledger, 0, before=screen, after=screen)
    samples = {"before": screen, "after": screen, missing: None}
    execute(ledger, 1, **samples)
    ledger.observe(2, screen)
    assert ledger.govern(2, SWIPE).kind == "ALLOW"
    assert ledger.summary()["last_image_comparison"] == "UNKNOWN"


def test_unknown_current_sample_always_allows():
    ledger = GuiLedger("Find Alice")
    for step in range(3):
        execute(ledger, step, before=picture(), after=picture())
    ledger.observe(3, None)
    assert ledger.govern(3, SWIPE).kind == "ALLOW"
    assert "sampled observation UNKNOWN" in ledger.render_inform()


def test_identical_rgba_pixels_ignore_source_encoding_mode_but_keep_dimensions():
    ledger = GuiLedger("Task")
    execute(ledger, 0, before=picture(mode="RGB"), after=picture(mode="RGBA"))
    assert ledger.summary()["last_image_comparison"] == "SAME"
    execute(ledger, 1, before=picture(size=(4, 16)), after=picture(size=(8, 8)))
    assert ledger.summary()["last_image_comparison"] == "CHANGED"


def test_broken_screenshot_is_unknown_and_not_fatal(monkeypatch):
    screenshot = picture()

    def broken(*args, **kwargs):
        raise OSError("unreadable screenshot")

    monkeypatch.setattr(screenshot, "convert", broken)
    ledger = GuiLedger("Task")
    ledger.observe(0, screenshot)
    assert ledger.govern(0, CLICK).kind == "ALLOW"
    assert ledger.summary()["retained_observations"] == 0


def test_oversized_screenshot_is_not_decoded(monkeypatch):
    screenshot = picture()
    monkeypatch.setattr(screenshot, "_size", (8193, 1))

    def unexpected(*args, **kwargs):
        pytest.fail("oversized screenshot must not be decoded")

    monkeypatch.setattr(screenshot, "convert", unexpected)
    ledger = GuiLedger("Task")
    ledger.observe(0, screenshot)
    assert ledger.summary()["retained_observations"] == 0


def test_confirmed_two_action_cycle_nudges_next_matching_candidate():
    ledger = GuiLedger("Task")
    red, blue = picture("red"), picture("blue")
    back = {"action_type": "navigate_back"}
    for step in range(4):
        action, before, after = (CLICK, red, blue) if step % 2 == 0 else (back, blue, red)
        assert execute(ledger, step, action, before=before, after=after).kind == "ALLOW"
    ledger.observe(4, red)
    assert ledger.govern(4, CLICK).reason == "REPEATED_TWO_ACTION_SEQUENCE"
    assert ledger.govern(4, {"action_type": "click", "x": 11, "y": 20}).kind == "ALLOW"


def test_repeated_actions_without_matching_connected_screens_do_not_form_cycle():
    ledger = GuiLedger("Task")
    back = {"action_type": "navigate_back"}
    for step in range(4):
        execute(
            ledger,
            step,
            CLICK if step % 2 == 0 else back,
            before=picture("red"),
            after=picture("blue"),
        )
    ledger.observe(4, picture("red"))
    assert ledger.govern(4, CLICK).kind == "ALLOW"


def test_cooldown_and_per_attempt_cap_count_executions_not_govern_calls():
    ledger = GuiLedger("Task", cooldown_steps=3, max_nudges=2)
    screen = picture()
    nudged = []
    for step in range(15):
        ledger.observe(step, screen)
        first = ledger.govern(step, SWIPE)
        assert all(ledger.govern(step, SWIPE) == first for _ in range(5))
        ledger.record_transition(step, SWIPE, outcome="returned", screenshot=screen, decision=first)
        if first.kind == "NUDGE":
            nudged.append(step)
    assert nudged == [2, 6]
    assert ledger.summary()["nudge_count"] == 2


def test_raised_execution_is_recorded_but_breaks_repeat_and_can_spend_delivered_nudge():
    ledger = GuiLedger("Task")
    for step in range(2):
        execute(ledger, step, before=picture(), after=picture())
    decision = execute(ledger, 2, before=picture(), after=None, outcome="raised")
    assert decision.kind == "NUDGE"
    assert ledger.summary()["nudge_count"] == 1
    assert ledger.summary()["last_outcome"] == "raised"
    ledger.observe(3, picture())
    assert ledger.govern(3, SWIPE).kind == "ALLOW"
    assert "executor=raised" in ledger.render_inform()


@pytest.mark.parametrize(
    "action",
    [
        {"action_type": "wait"},
        {"action_type": "finished", "text": "success"},
        {"action_type": "answer", "text": "done"},
        {"action_type": "keyboard_enter"},
        {"action_type": "mcp", "action_json": {"tool": "send"}},
        {"action_type": "delete"},
        {"action_type": "click", "x": 10},
        {"action_type": "click", "x": True, "y": 20},
        {"action_type": "click", "x": float("nan"), "y": 20},
        {"action_type": "click", "x": 10, "y": 20, "unexpected": "value"},
        {"action_type": "input_text", "text": "x" * 16385},
    ],
)
def test_unknown_malformed_or_exempt_actions_do_not_trigger_nudges(action):
    ledger = GuiLedger("Task", cooldown_steps=0)
    for step in range(5):
        assert execute(ledger, step, action, before=picture(), after=picture()).kind == "ALLOW"


def test_action_arguments_are_exact_private_and_detached_from_mutable_caller():
    ledger = GuiLedger("Enter the provided value")
    secret = "PrivateEmail@example.com"
    action = {"action_type": "input_text", "text": secret}
    for step in range(2):
        execute(ledger, step, action, before=picture(), after=picture())
    action["text"] = secret.lower()
    ledger.observe(2, picture())
    assert ledger.govern(2, action).kind == "ALLOW"
    action["text"] = secret
    decision = ledger.govern(2, action)
    assert decision.kind == "NUDGE"
    assert secret not in ledger.render_inform()
    assert secret not in decision.notice
    assert secret not in json.dumps(ledger.summary())
    assert secret not in repr(ledger.__dict__)


def test_recording_is_idempotent_even_after_ring_eviction_and_stale_samples_are_ignored():
    ledger = GuiLedger("Task", max_records=4)
    for step in range(10):
        execute(ledger, step, before=picture(), after=picture())
    before = ledger.summary()
    ledger.observe(0, picture("green"))
    ledger.record_transition(
        0, SWIPE, outcome="returned", screenshot=None, decision=GovernDecision("NUDGE")
    )
    assert ledger.summary() == before
    assert ledger.govern(0, SWIPE).kind == "ALLOW"


def test_observation_retries_and_rendering_do_not_advance_state_or_repeat_hints():
    ledger = GuiLedger("Task")
    ledger.observe(0, picture())
    before = ledger.summary()
    text = ledger.render_inform()
    for _ in range(10):
        ledger.observe(0, picture("blue"))
        assert ledger.render_inform() == text
        assert ledger.summary() == before
    assert text.count("[GUI Ledger: current execution state]") == 1


def test_record_and_observation_memory_and_render_size_are_bounded():
    ledger = GuiLedger("T" * 10000, max_records=4)
    for step in range(100):
        screen = picture((step, 0, 0))
        execute(ledger, step, before=screen, after=screen)
    summary = ledger.summary()
    assert summary["recorded_executions"] == 100
    assert summary["retained_records"] == summary["retained_observations"] == 4
    assert "Anchor truncated" in ledger.render_inform()
    assert len(ledger.render_inform()) < 7000
    assert "Attempt step 0:" not in ledger.render_inform()


def test_step_gap_does_not_bridge_unobserved_execution():
    ledger = GuiLedger("Task")
    for step in [0, 1]:
        execute(ledger, step, before=picture(), after=picture())
    ledger.observe(4, picture())
    assert ledger.govern(4, SWIPE).kind == "ALLOW"


def test_new_attempt_has_no_observations_actions_or_nudge_budget_from_previous_attempt():
    first = GuiLedger("Task")
    for step in range(3):
        execute(first, step, before=picture(), after=picture())
    second = GuiLedger("Task")
    second.observe(0, picture())
    assert second.govern(0, SWIPE).kind == "ALLOW"
    assert second.summary()["nudge_count"] == 0
    assert "No recorded execution in this attempt" in second.render_inform()


@pytest.mark.parametrize(
    "kwargs",
    [{"repeat_threshold": 1}, {"max_records": 3}, {"cooldown_steps": -1}, {"max_nudges": True}],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        GuiLedger("Task", **kwargs)


@pytest.mark.parametrize(
    "qwen_arguments,expected_kind",
    [
        ({"action": "click", "coordinate": [500, 500]}, "NUDGE"),
        ({"action": "long_press", "coordinate": [500, 500]}, "NUDGE"),
        ({"action": "swipe", "coordinate": [500, 800], "coordinate2": [500, 200]}, "NUDGE"),
        ({"action": "type", "text": "Private value"}, "NUDGE"),
        ({"action": "system_button", "button": "Back"}, "NUDGE"),
        ({"action": "system_button", "button": "Home"}, "NUDGE"),
        ({"action": "system_button", "button": "Enter"}, "ALLOW"),
        ({"action": "wait"}, "ALLOW"),
    ],
)
def test_actual_qwen_parser_and_json_action_dump_feed_the_comparator(qwen_arguments, expected_kind):
    from mobile_world.agents.implementations.qwen3vl import (
        parse_action_to_structure_output,
        parsing_response_to_andoid_world_env_action,
    )
    from mobile_world.runtime.utils.models import JSONAction

    response = (
        'Thought: Inspect the screen.\nAction: "Attempt the operation"\n<tool_call>'
        + json.dumps({"name": "mobile_use", "arguments": qwen_arguments})
        + "</tool_call>"
    )
    structured = parse_action_to_structure_output(response)
    parsed = parsing_response_to_andoid_world_env_action(structured, 8, 8)
    action = JSONAction(**parsed).model_dump(exclude_none=True)
    ledger = GuiLedger("Task")
    for step in range(2):
        assert execute(ledger, step, action, before=picture(), after=picture()).kind == "ALLOW"
    ledger.observe(2, picture())
    assert ledger.govern(2, action).kind == expected_kind


@pytest.mark.parametrize("outcome", ["returned", "raised"])
@pytest.mark.parametrize(
    "action,label",
    [
        (CLICK, "click"),
        ({"action_type": "long_press", "x": 10, "y": 20}, "long_press"),
        ({"action_type": "input_text", "text": "private-ledger-value"}, "type"),
        ({**SWIPE, "action_type": "drag"}, "swipe"),
        (SWIPE, "swipe"),
        ({"action_type": "navigate_home"}, 'system_button (button="Home")'),
        ({"action_type": "navigate_back"}, 'system_button (button="Back")'),
        ({"action_type": "keyboard_enter"}, 'system_button (button="Enter")'),
        ({"action_type": "finished", "text": "success"}, "terminate"),
        ({"action_type": "answer", "text": "private-ledger-value"}, "answer"),
        ({"action_type": "ask_user", "text": "private-ledger-value"}, "ask_user"),
        ({"action_type": "wait"}, "wait"),
    ],
)
def test_inform_uses_qwen_tool_labels_without_exposing_private_arguments(action, label, outcome):
    ledger = GuiLedger("Task")
    execute(ledger, 1, action, before=picture(), after=picture(), outcome=outcome)

    inform = ledger.render_inform()
    assert (
        f"- Attempt step 1: {label}; executor={outcome}; sampled image=SAME (O1 -> O1)." in inform
    )
    assert "private arguments withheld" in inform
    assert "private-ledger-value" not in inform
    assert "success" not in inform.split("Recent executed commands", 1)[1].split("SAME means", 1)[0]
    # Projection is display-only; execution records retain their original vocabulary.
    assert ledger._records[-1].action_kind == action["action_type"]


@pytest.mark.parametrize(
    "kind",
    ["open_app", "scroll", "double_tap", "status", "unknown", "mcp", "invented\ncommand"],
)
def test_unrepresented_actions_get_neutral_labels_without_fabricating_tool_actions(kind):
    ledger = GuiLedger("Task")
    execute(ledger, 1, {"action_type": kind, "text": "private-ledger-value"}, outcome="returned")
    command = next(
        line for line in ledger.render_inform().splitlines() if "Attempt step 1:" in line
    )
    assert command == (
        "- Attempt step 1: GUI operation (label omitted); "
        "executor=returned; sampled image=UNKNOWN (UNKNOWN -> UNKNOWN)."
    )


def test_render_projection_does_not_rewrite_task_anchor_or_change_govern_identity():
    task = "Inspect text mentioning navigate_home, input_text and drag."
    ledger = GuiLedger(task)
    drag = {**SWIPE, "action_type": "drag"}
    for step in (1, 2):
        execute(ledger, step, drag, before=picture(), after=picture())
    ledger.observe(3, picture())
    before_summary = ledger.summary()
    before_records = tuple(ledger._records)
    before_decision = ledger.govern(3, drag)
    assert before_decision.kind == "NUDGE"

    inform = ledger.render_inform()
    assert f"Task anchor:\n{task}\n" in inform
    assert "Attempt step 1: swipe;" in inform
    assert "Attempt step 2: swipe;" in inform
    assert ledger.summary() == before_summary
    assert tuple(ledger._records) == before_records
    assert ledger.govern(3, drag) == before_decision
    # Same display label does not equate distinct executor actions in Govern.
    assert ledger.govern(3, SWIPE).kind == "ALLOW"
