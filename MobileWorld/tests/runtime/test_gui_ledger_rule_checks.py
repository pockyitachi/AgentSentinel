"""Independent synthetic regressions for rule-based advisory Action Check."""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

import pytest
from PIL import Image

from mobile_world.runtime.gui_ledger import GuiLedger

NEXT = {"action_type": "click", "x": 10, "y": 20}
OTHER = {"action_type": "click", "x": 60, "y": 20}
RULE_NAMES = (
    "current_sample",
    "node_presence",
    "field_changes",
    "action_target",
    "input_value",
    "scroll_region",
)


def node(children="", **changes):
    attributes = {
        "package": "example.app",
        "resource-id": "example.app:id/next",
        "class": "android.widget.Button",
        "bounds": "[0,0][40,40]",
        "text": "Next",
        "content-desc": "",
        "password": "false",
        "enabled": "true",
        "focused": "false",
        "selected": "false",
        "checked": "false",
        "checkable": "false",
        "focusable": "true",
        "clickable": "true",
        "long-clickable": "false",
        "scrollable": "false",
    }
    attributes.update(changes)
    fields = " ".join(
        f"{name}={quoteattr(value)}" for name, value in attributes.items() if value is not None
    )
    return f"<node {fields}>{children}</node>"


def page(label, *, private=False):
    heading = node(
        **{
            "resource-id": "example.app:id/title",
            "class": "android.widget.TextView",
            "bounds": "[100,0][200,40]",
            "text": label,
            "clickable": "false",
        }
    )
    other = node(
        **{
            "resource-id": "example.app:id/other",
            "bounds": "[50,0][90,40]",
            "text": "Other",
        }
    )
    body = node(
        heading + node() + other,
        **{
            "resource-id": "example.app:id/page",
            "class": "android.widget.LinearLayout",
            "bounds": "[0,0][400,400]",
            "text": "",
            "content-desc": label,
            "clickable": "false",
            "password": "true" if private else "false",
        },
    )
    return {
        "status": "ok",
        "source": "uiautomator",
        "xml": '<hierarchy rotation="0">' + body + "</hierarchy>",
    }


def execute(ledger, step, action, before, after, *, image=None):
    ledger.observe(step, image, ui_tree=before)
    decision = ledger.govern(step, action)
    ledger.record_transition(
        step,
        action,
        outcome="returned",
        screenshot=image,
        decision=decision,
        ui_tree=after,
    )
    return decision


def rule_totals(summary):
    return {
        rule: sum(
            summary[f"ui_rule_{rule}_{status}_count"]
            for status in ("recorded", "not_applicable", "unavailable")
        )
        for rule in RULE_NAMES
    }


def prepare_cycle(*, broken_post=None):
    ledger = GuiLedger("Inspect these pages", ui_tree_enabled=True)
    for step, action in enumerate((NEXT, OTHER, NEXT, OTHER)):
        before = page("Page A" if step % 2 == 0 else "Page B")
        after = page("Page B" if step % 2 == 0 else "Page A")
        if step == 1 and broken_post == "missing":
            after = None
        elif step == 1 and broken_post == "private":
            after = page("Page A", private=True)
        assert execute(ledger, step, action, before, after).kind == "ALLOW"
    ledger.observe(4, None, ui_tree=page("Page A"))
    return ledger


def test_surviving_next_button_does_not_imply_repetition_across_changing_pages():
    ledger = GuiLedger("Complete the onboarding pages", ui_tree_enabled=True)
    for step in range(2):
        assert (
            execute(ledger, step, NEXT, page(f"Page {step + 1}"), page(f"Page {step + 2}")).kind
            == "ALLOW"
        )
    ledger.observe(2, None, ui_tree=page("Page 3"))
    assert ledger.summary()["last_ui_changed_fact_count"] == 2
    assert ledger.govern(2, NEXT).kind == "ALLOW"


@pytest.mark.parametrize("action_type", ["navigate_back", "navigate_home"])
def test_fresh_ui_change_vetoes_stale_identical_pixels_without_an_action_target(action_type):
    action = {"action_type": action_type}
    ledger = GuiLedger("Return to another page", ui_tree_enabled=True)
    with Image.new("RGB", (40, 40), "red") as image:
        for step in range(2):
            assert (
                execute(ledger, step, action, page("Old page"), page("Old page"), image=image).kind
                == "ALLOW"
            )
        # The same screenshot bytes do not override newly sampled UI evidence.
        ledger.observe(2, image, ui_tree=page("A new page arrived"))
        assert ledger.summary()["last_ui_rule_action_target_status"] == "NOT_APPLICABLE"
        assert ledger.govern(2, action).kind == "ALLOW"


def test_changing_ui_edges_can_form_a_repeated_advisory_cycle_without_screenshots():
    ledger = prepare_cycle()
    assert ledger.summary()["last_ui_changed_fact_count"] == 2
    assert ledger.summary()["last_image_comparison"] == "SAMPLE_UNAVAILABLE"
    decision = ledger.govern(4, NEXT)
    assert decision.kind == "NUDGE"
    assert decision.reason == "REPEATED_UI_OBSERVATION_CYCLE"
    assert "still executed" in decision.notice
    assert "does not prove task failure" in decision.notice


@pytest.mark.parametrize("broken_post", ["missing", "private"])
def test_missing_or_private_evidence_cannot_close_a_ui_cycle(broken_post):
    ledger = prepare_cycle(broken_post=broken_post)
    assert ledger.summary()["recorded_executions"] == 4
    assert ledger.govern(4, NEXT).kind == "ALLOW"


def test_rule_counters_and_nudge_budget_advance_only_when_a_transition_commits():
    ledger = prepare_cycle()
    baseline = ledger.summary()
    assert rule_totals(baseline) == dict.fromkeys(RULE_NAMES, 4)
    assert baseline["nudge_count"] == 0
    report = ledger.render_inform()
    decision = ledger.govern(4, NEXT)
    assert decision.kind == "NUDGE"
    for _ in range(3):
        # Actor retries must not accept a replacement same-step sample or spend
        # another budget merely by rendering/checking the candidate again.
        ledger.observe(4, None, ui_tree=page("Retry replacement", private=True))
        assert ledger.render_inform() == report
        assert ledger.govern(4, NEXT) == decision
        assert ledger.summary() == baseline

    ledger.record_transition(
        4,
        NEXT,
        outcome="returned",
        screenshot=None,
        decision=decision,
        ui_tree=page("Page B"),
    )
    committed = ledger.summary()
    assert rule_totals(committed) == dict.fromkeys(RULE_NAMES, 5)
    assert committed["recorded_executions"] == 5
    assert committed["nudge_count"] == 1
    ledger.record_transition(
        4,
        NEXT,
        outcome="returned",
        screenshot=None,
        decision=decision,
        ui_tree=None,
    )
    assert ledger.summary() == committed
    ledger.observe(5, None, ui_tree=page("Fresh page"))
    assert rule_totals(ledger.summary()) == dict.fromkeys(RULE_NAMES, 5)
    assert ledger.summary()["nudge_count"] == 1
