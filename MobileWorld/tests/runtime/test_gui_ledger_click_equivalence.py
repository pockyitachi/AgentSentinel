"""Independent CPU tests for guarded coordinate-independent click repetition."""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

import pytest
from PIL import Image

from mobile_world.runtime.gui_ledger import GuiLedger
from mobile_world.runtime.gui_ledger_ui import (
    has_discrete_click_candidate,
    parse_ui_tree,
)

OLD_REASON = "REPEATED_ACTION_SAME_UI_OBSERVATION"
NEW_REASON = "REPEATED_CLICK_SAME_UI_CONTROL"


def click(x=10, y=20, **changes):
    return {"action_type": "click", "x": x, "y": y, **changes}


def node(children="", **changes):
    attributes = {
        "package": "example.app",
        "resource-id": "example.app:id/button",
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


def tree(*nodes, rotation="0"):
    return {
        "status": "ok",
        "source": "uiautomator",
        "xml": f'<hierarchy rotation="{rotation}">' + "".join(nodes) + "</hierarchy>",
    }


def page(*, heading="Page one", control=None, sibling=None, private=False, rotation="0"):
    title = node(
        **{
            "resource-id": "example.app:id/title",
            "class": "android.widget.TextView",
            "bounds": "[100,0][200,40]",
            "text": heading,
            "clickable": "false",
        }
    )
    parent = node(
        title + (node() if control is None else control) + (sibling or ""),
        **{
            "resource-id": "example.app:id/page",
            "class": "android.widget.LinearLayout",
            "bounds": "[0,0][400,400]",
            "text": "",
            "content-desc": "Form",
            "clickable": "false",
            "password": "true" if private else "false",
        },
    )
    return tree(parent, rotation=rotation)


def execute(ledger, step, action, before, after, *, outcome="returned", image=None):
    ledger.observe(step, image, ui_tree=before)
    decision = ledger.govern(step, action)
    ledger.record_transition(
        step,
        action,
        outcome=outcome,
        screenshot=image,
        decision=decision,
        ui_tree=after,
    )
    return decision


def after_two_clicks(sample, *, first=None, second=None, current=None):
    ledger = GuiLedger("Use this form", ui_tree_enabled=True)
    execute(ledger, 0, click() if first is None else first, sample, sample)
    execute(ledger, 1, click(11) if second is None else second, sample, sample)
    ledger.observe(2, None, ui_tree=sample if current is None else current)
    return ledger


@pytest.mark.parametrize(
    "widget",
    [
        "Button",
        "ImageButton",
        "CompoundButton",
        "CheckBox",
        "RadioButton",
        "ToggleButton",
        "Switch",
    ],
)
def test_enabled_native_leaf_controls_support_coordinate_independent_repetition(widget):
    sample = page(control=node(**{"class": f"android.widget.{widget}"}))
    assert has_discrete_click_candidate(parse_ui_tree(sample), click())
    decision = after_two_clicks(sample).govern(2, click(12))
    assert decision.kind == "NUDGE"
    assert decision.reason == NEW_REASON
    assert "still executed" in decision.notice


@pytest.mark.parametrize(
    "widget",
    [
        "example.Button",
        "android.widget.SeekBar",
        "android.widget.ImageView",
        "android.webkit.WebView",
        "android.view.View",
        "android.widget.EditText",
        "androidx.appcompat.widget.AppCompatButton",
    ],
)
def test_nonallowlisted_or_short_name_impostor_classes_do_not_gain_equivalence(widget):
    sample = page(control=node(**{"class": widget}))
    assert not has_discrete_click_candidate(parse_ui_tree(sample), click())
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": "false"},
        {"enabled": None},
        {"scrollable": "true"},
        {"scrollable": None},
        {"clickable": "false"},
        {"clickable": None},
        {"password": "true"},
        {"password": None},
        {"resource-id": ""},
        {"package": ""},
        {"class": ""},
        {"bounds": "bad"},
    ],
)
def test_missing_identity_or_unproven_click_capabilities_do_not_gain_equivalence(changes):
    sample = page(control=node(**changes))
    assert not has_discrete_click_candidate(parse_ui_tree(sample), click())
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize(
    "child_changes",
    [
        {"class": "android.widget.TextView", "clickable": "false"},
        {"class": "android.widget.EditText", "clickable": "false"},
        {"scrollable": "true", "clickable": "false"},
        {"clickable": "true"},
        {"clickable": None},
    ],
)
def test_any_exposed_child_rejects_leaf_equivalence_even_when_static(child_changes):
    child = node(**{"resource-id": "example.app:id/child", **child_changes})
    sample = page(control=node(child))
    assert not has_discrete_click_candidate(parse_ui_tree(sample), click())
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize(
    "action",
    [
        {"action_type": "click", "index": 1},
        click(True),
        click(10.0),
        click(40),
        click(-1),
        click(action_type="double_tap"),
        click(action_type="long_press"),
        {"action_type": "drag", "start_x": 10, "start_y": 20, "end_x": 11, "end_y": 20},
        {"action_type": "input_text", "text": "Next"},
        click(extra_argument="unrecognized"),
    ],
)
def test_only_valid_simple_clicks_receive_coordinate_independent_candidate(action):
    assert not has_discrete_click_candidate(parse_ui_tree(page()), action)


def test_distinct_siblings_are_not_merged_just_because_parent_scope_matches():
    sibling = node(**{"resource-id": "example.app:id/other", "bounds": "[50,0][90,40]"})
    sample = page(sibling=sibling)
    ledger = after_two_clicks(sample, first=click(), second=click(60))
    assert ledger.govern(2, click(11)).kind == "ALLOW"
    ledger = after_two_clicks(sample)
    assert ledger.govern(2, click(60)).kind == "ALLOW"


@pytest.mark.parametrize("overlap", [True, False])
def test_ambiguous_hit_or_duplicate_selector_is_not_a_unique_control(overlap):
    sibling = node(
        **{
            "resource-id": "example.app:id/other" if overlap else "example.app:id/button",
            "bounds": "[0,0][40,40]" if overlap else "[50,0][90,40]",
        }
    )
    sample = page(sibling=sibling)
    assert not has_discrete_click_candidate(parse_ui_tree(sample), click())
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize(
    "sibling_changes",
    [{"password": "true"}, {"text": None}, {"content-desc": None}, {"text": "x" * 100}],
)
def test_private_missing_or_truncated_parent_context_cannot_establish_repeat(sibling_changes):
    sibling = node(
        **{
            "resource-id": "example.app:id/context",
            "bounds": "[50,0][90,40]",
            "clickable": "false",
            **sibling_changes,
        }
    )
    sample = page(sibling=sibling)
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


def test_private_ancestor_cannot_establish_repeat():
    sample = page(private=True)
    assert not has_discrete_click_candidate(parse_ui_tree(sample), click())
    assert after_two_clicks(sample).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize(
    "current",
    [
        page(heading="Another page"),
        page(control=node(selected="true")),
        page(control=node(bounds="[0,50][40,90]")),
        page(rotation="1"),
    ],
)
def test_changed_target_or_containing_context_breaks_equivalent_click_chain(current):
    assert after_two_clicks(page(), current=current).govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize("outcome", ["returned", "raised"])
def test_missing_or_failed_post_sample_breaks_equivalent_click_chain(outcome):
    sample = page()
    ledger = GuiLedger("Task", ui_tree_enabled=True)
    execute(ledger, 0, click(), sample, sample)
    execute(
        ledger, 1, click(11), sample, None if outcome == "returned" else sample, outcome=outcome
    )
    ledger.observe(2, None, ui_tree=sample)
    assert ledger.govern(2, click(12)).kind == "ALLOW"


def test_nonconsecutive_executions_do_not_form_a_repeat_chain():
    sample = page()
    ledger = GuiLedger("Task", ui_tree_enabled=True)
    execute(ledger, 0, click(), sample, sample)
    execute(ledger, 2, click(11), sample, sample)
    ledger.observe(3, None, ui_tree=sample)
    assert ledger.govern(3, click(12)).kind == "ALLOW"


def test_malformed_prior_click_cannot_be_rescued_by_coordinate_equivalence():
    ledger = after_two_clicks(page(), first=click(extra_argument="unrecognized"))
    assert ledger.govern(2, click(12)).kind == "ALLOW"


@pytest.mark.parametrize("first_x,second_x,current_x", [(10, 10, 11), (10, 11, 11), (10, 11, 10)])
def test_mixed_exact_and_equivalent_clicks_use_the_new_precise_reason(first_x, second_x, current_x):
    ledger = after_two_clicks(page(), first=click(first_x), second=click(second_x))
    assert ledger.govern(2, click(current_x)).reason == NEW_REASON


@pytest.mark.parametrize("widget", ["android.widget.Button", "example.CustomButton"])
def test_all_exact_repetitions_keep_the_original_rule_including_nonallowlisted_controls(widget):
    sample = page(control=node(**{"class": widget}))
    ledger = after_two_clicks(sample, first=click(), second=click())
    assert ledger.govern(2, click()).reason == OLD_REASON


def test_ui_disabled_preserves_exact_pixel_rule_and_does_not_call_new_helper(monkeypatch):
    from mobile_world.runtime import gui_ledger as module

    def unexpected(*_args):
        raise AssertionError("UI-disabled path must not inspect discrete controls")

    monkeypatch.setattr(module, "has_discrete_click_candidate", unexpected)
    sample = page()
    with Image.new("RGB", (40, 40), "red") as image:
        exact, varied = GuiLedger("Task"), GuiLedger("Task")
        for step in range(2):
            execute(exact, step, click(), sample, sample, image=image)
            execute(varied, step, click(10 + step), sample, sample, image=image)
        exact.observe(2, image, ui_tree=sample)
        varied.observe(2, image, ui_tree=sample)
        assert exact.govern(2, click()).reason == "REPEATED_ACTION_SAME_SAMPLED_IMAGE"
        assert varied.govern(2, click(12)).kind == "ALLOW"
        assert "ui_tree_status" not in exact.summary()


def test_equivalent_click_retries_are_read_only_and_count_only_one_committed_nudge():
    sample = page()
    ledger = after_two_clicks(sample)
    action = click(12)
    baseline, report = ledger.summary(), ledger.render_inform()
    decision = ledger.govern(2, action)
    assert decision.reason == NEW_REASON
    for _ in range(3):
        ledger.observe(2, None, ui_tree=page(private=True))
        assert ledger.govern(2, action) == decision
        assert ledger.summary() == baseline
        assert ledger.render_inform() == report
        assert action == click(12)
    ledger.record_transition(
        2, action, outcome="returned", screenshot=None, decision=decision, ui_tree=sample
    )
    committed = ledger.summary()
    assert committed["nudge_count"] == 1
    ledger.record_transition(
        2, action, outcome="returned", screenshot=None, decision=decision, ui_tree=None
    )
    assert ledger.summary() == committed


def test_equivalent_clicks_obey_existing_nudge_budget():
    sample = page()
    ledger = GuiLedger("Task", ui_tree_enabled=True, max_nudges=1)
    results = [execute(ledger, step, click(10 + step), sample, sample).kind for step in range(8)]
    assert results == ["ALLOW", "ALLOW", "NUDGE", "ALLOW", "ALLOW", "ALLOW", "ALLOW", "ALLOW"]
    assert ledger.summary()["nudge_count"] == 1
