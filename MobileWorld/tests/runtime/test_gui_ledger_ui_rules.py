"""Independent CPU tests for rule observations rather than whole-step verdicts."""

from __future__ import annotations

from xml.sax.saxutils import quoteattr

import pytest

from mobile_world.runtime.gui_ledger_ui import (
    MAX_DISPLAY_CHARS,
    action_observation_key,
    analyze_ui_transition,
    parse_ui_tree,
)

CLICK = {"action_type": "click", "x": 10, "y": 20}
DRAG = {"action_type": "drag", "start_x": 10, "start_y": 150, "end_x": 10, "end_y": 20}


def node(children="", **changes):
    attributes = {
        "package": "example.app",
        "resource-id": "example.app:id/button",
        "class": "android.widget.Button",
        "bounds": "[0,0][40,40]",
        "text": "Login",
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
        f"{key}={quoteattr(value)}" for key, value in attributes.items() if value is not None
    )
    return f"<node {fields}>{children}</node>"


def snapshot(*nodes, rotation="0"):
    return parse_ui_tree(
        {
            "status": "ok",
            "source": "uiautomator",
            "xml": f'<hierarchy rotation="{rotation}">' + "".join(nodes) + "</hierarchy>",
        }
    )


def container(*children, **changes):
    values = {
        "resource-id": "example.app:id/list",
        "class": "android.widget.ScrollView",
        "bounds": "[0,0][200,200]",
        "scrollable": "true",
        "clickable": "false",
        "text": "",
    }
    values.update(changes)
    return node("".join(children), **values)


def item(text, bounds="[0,30][100,60]", **changes):
    values = {
        "resource-id": "",
        "class": "android.widget.TextView",
        "text": text,
        "bounds": bounds,
        "clickable": "false",
    }
    values.update(changes)
    return node(**values)


def rule(transition, name):
    return next(result for result in transition.rules if result.rule == name)


def values(transition, name):
    return dict(rule(transition, name).values)


def test_navigation_target_disappears_but_presence_and_current_facts_are_recorded():
    before = snapshot(node())
    after = snapshot(item("Home", **{"resource-id": "example.app:id/home"}))
    result = analyze_ui_transition(before, after, CLICK)
    assert not hasattr(result, "comparison")
    assert values(result, "action_target") == {
        "candidate_count": 1,
        "matched": False,
        "observed_after": False,
    }
    assert values(result, "node_presence") == {"matched": 0, "appeared": 1, "not_observed": 1}
    assert values(result, "current_sample")["facts"] == 1
    assert result.has_observed_change
    assert result.action_before_key and result.action_after_key is None


def test_moved_control_matches_anchor_but_never_retargets_old_click_coordinate():
    before = snapshot(node())
    after = snapshot(node(bounds="[0,80][40,120]"), node(**{"resource-id": "new", "text": "Other"}))
    result = analyze_ui_transition(before, after, CLICK)
    assert values(result, "action_target")["matched"] is True
    assert values(result, "action_target")["bounds_changed"] is True
    assert result.action_after_key != action_observation_key(after, CLICK)
    assert result.action_before_key != result.action_after_key


def test_partial_boolean_fields_do_not_suppress_checked_or_focus_changes():
    before = snapshot(node(enabled=None, checkable="true"))
    after = snapshot(node(enabled=None, checkable="true", checked="true", focused="true"))
    result = analyze_ui_transition(before, after, CLICK)
    changes = values(result, "field_changes")
    assert changes["checked"] == changes["focused"] == 1
    assert "enabled" not in changes
    assert result.changed_fact_count == 1
    assert result.has_observed_change
    assert action_observation_key(before, CLICK)


def test_text_and_accessibility_description_are_independent_observed_fields():
    before = snapshot(node(text="old", **{"content-desc": "constant label"}))
    after = snapshot(node(text="new", **{"content-desc": "constant label"}))
    result = analyze_ui_transition(before, after, CLICK)
    assert values(result, "field_changes")["text"] == 1
    assert result.changed_fact_count == 1 and result.has_observed_change
    assert not result.fact_changes  # The preferred display label itself is unchanged.


def test_same_input_request_already_present_is_not_new_input_success():
    field = node(**{"class": "android.widget.EditText", "focused": "true", "text": "hello"})
    result = analyze_ui_transition(
        snapshot(field), snapshot(field), {"action_type": "input_text", "text": "hello"}
    )
    assert values(result, "input_value") == {
        "before_equals_request": True,
        "after_equals_request": True,
        "after_basis": "MATCHED_PRE_ACTION_FIELD",
        "text_changed": False,
    }
    assert result.action_before_key == result.action_after_key
    assert not result.has_observed_change


def test_changed_input_field_matches_after_keyboard_changes_geometry():
    before = snapshot(
        node(**{"class": "android.widget.EditText", "focused": "true", "text": "old"})
    )
    after = snapshot(
        node(
            **{
                "class": "android.widget.EditText",
                "focused": "false",
                "text": "new",
                "bounds": "[0,80][100,120]",
            }
        )
    )
    result = analyze_ui_transition(before, after, {"action_type": "input_text", "text": "new"})
    assert values(result, "input_value")["after_equals_request"]
    assert values(result, "input_value")["after_basis"] == "MATCHED_PRE_ACTION_FIELD"
    assert values(result, "input_value")["text_changed"]


def test_post_only_focused_input_value_has_explicit_nonrecipient_basis():
    before = snapshot(
        node(**{"class": "android.widget.EditText", "focused": "true", "text": "old"})
    )
    after = snapshot(
        node(
            **{
                "class": "android.widget.EditText",
                "focused": "true",
                "text": "new",
                "resource-id": "another",
            }
        )
    )
    result = analyze_ui_transition(before, after, {"action_type": "input_text", "text": "new"})
    assert values(result, "input_value")["after_basis"] == "CURRENT_FOCUSED_FIELD"
    assert "text_changed" not in values(result, "input_value")
    assert not values(result, "action_target")["matched"]


def test_ambiguous_candidates_do_not_block_independent_field_rules():
    before = snapshot(node(), node(**{"resource-id": "overlapping"}))
    after = snapshot(node(selected="true"), node(**{"resource-id": "overlapping"}))
    result = analyze_ui_transition(before, after, CLICK)
    assert rule(result, "action_target").reason == "AMBIGUOUS_ACTION_CANDIDATE"
    assert values(result, "field_changes")["selected"] == 1
    assert result.action_before_key is result.action_after_key is None


def test_no_id_has_current_facts_but_no_manufactured_persistent_identity():
    state = snapshot(node(**{"resource-id": ""}))
    result = analyze_ui_transition(state, state, CLICK)
    assert values(result, "current_sample")["facts"] == 1
    assert rule(result, "action_target").reason == "NO_UNIQUE_SCOPE_ANCHOR"
    assert result.action_before_key is None


def test_static_noneditable_focus_does_not_make_unique_focused_input_ambiguous():
    state = snapshot(
        node(**{"class": "android.widget.EditText", "focused": "true"}),
        item("Keyboard", focused="true"),
    )
    assert action_observation_key(state, {"action_type": "input_text", "text": "hi"})


def test_two_focused_editable_fields_cannot_establish_target_equality():
    state = snapshot(
        node(**{"class": "android.widget.EditText", "focused": "true"}),
        node(**{"class": "android.widget.EditText", "focused": "true", "resource-id": "other"}),
    )
    action = {"action_type": "input_text", "text": "hi"}
    assert action_observation_key(state, action) is None
    assert rule(analyze_ui_transition(state, state, action), "input_value").status == "UNAVAILABLE"


def test_scroll_rule_compares_scoped_content_and_movement_not_sibling_list():
    before = snapshot(
        container(item("Alpha"), item("Beta", "[0,70][100,100]")),
        container(
            item("Unrelated", "[250,30][350,60]"),
            **{"resource-id": "second", "bounds": "[220,0][400,200]"},
        ),
    )
    after = snapshot(
        container(item("Beta", "[0,30][100,60]"), item("Gamma", "[0,70][100,100]")),
        container(
            item("Unrelated", "[250,30][350,60]"),
            **{"resource-id": "second", "bounds": "[220,0][400,200]"},
        ),
    )
    result = analyze_ui_transition(before, after, DRAG)
    assert values(result, "scroll_region") == {
        "candidate_count": 1,
        "matched_entries": 1,
        "moved_entries": 1,
        "content_added": 1,
        "content_removed": 1,
        "sample_equal": False,
    }
    assert result.has_observed_change
    assert result.action_before_key[0] == "scroll"


def test_scroll_repeated_content_is_not_unique_item_correspondence():
    before = snapshot(container(item("Duplicate"), item("Duplicate", "[0,70][100,100]")))
    after = snapshot(
        container(item("Duplicate", "[0,10][100,40]"), item("Duplicate", "[0,50][100,80]"))
    )
    result = analyze_ui_transition(before, after, DRAG)
    assert values(result, "scroll_region")["matched_entries"] == 0
    assert values(result, "scroll_region")["moved_entries"] == 0
    assert values(result, "scroll_region")["sample_equal"] is False


@pytest.mark.parametrize(
    "change", [{"password": "true"}, {"text": None}, {"text": "x" * (MAX_DISPLAY_CHARS + 1)}]
)
def test_scroll_incomplete_projection_does_not_manufacture_content_absence(change):
    before = snapshot(container(item("Alpha")))
    attributes = {"text": "Alpha", **change}
    after = snapshot(container(item(**attributes)))
    result = analyze_ui_transition(before, after, DRAG)
    observed = values(result, "scroll_region")
    assert "content_added" not in observed and "content_removed" not in observed
    assert "sample_equal" not in observed
    assert observed["sample_comparison_gap"] == "INCOMPLETE_PUBLIC_SCOPE"
    assert result.action_after_key is None


@pytest.mark.parametrize("action", [DRAG, {"action_type": "swipe", "start_x": 1, "start_y": 2}])
def test_generic_drag_and_bad_coordinates_do_not_imply_scroll(action):
    state = snapshot(node())
    result = analyze_ui_transition(state, state, action)
    assert rule(result, "scroll_region").reason == "NO_SCROLL_CONTAINER"
    assert result.action_before_key is None


def test_nested_scrollable_containers_are_ambiguous():
    state = snapshot(container(container(item("Entry"), **{"resource-id": "inner"})))
    result = analyze_ui_transition(state, state, DRAG)
    assert rule(result, "scroll_region").reason == "AMBIGUOUS_SCROLL_CONTAINER"
    assert result.action_before_key is None


def test_truncated_changed_suffix_is_never_treated_as_comparable_public_text():
    prefix = "x" * MAX_DISPLAY_CHARS
    before = snapshot(node(text=prefix + "a"))
    after = snapshot(node(text=prefix + "b"))
    result = analyze_ui_transition(before, after, CLICK)
    assert "text" not in values(result, "field_changes")
    assert result.action_before_key is result.action_after_key is None
    assert before.sample_key is after.sample_key is None


def test_private_ancestor_hides_descendant_hashes_and_breaks_scope_equality():
    state = snapshot(container(item("SECRET"), password="true"))
    assert not state.facts
    assert state.nodes[1].text_key is None
    assert state.sample_key is None
    assert action_observation_key(state, DRAG) is None


def test_all_changed_nodes_counted_even_after_six_detail_cap():
    before = snapshot(
        *(
            node(**{"resource-id": str(i), "bounds": f"[{i * 50},0][{i * 50 + 40},40]"})
            for i in range(10)
        )
    )
    after = snapshot(
        *(
            node(
                selected="true",
                **{"resource-id": str(i), "bounds": f"[{i * 50},0][{i * 50 + 40},40]"},
            )
            for i in range(10)
        )
    )
    result = analyze_ui_transition(before, after, CLICK)
    assert len(result.fact_changes) == 6
    assert result.changed_fact_count == 10
    assert values(result, "field_changes")["changed_nodes"] == 10


def test_missing_before_does_not_suppress_current_sample_or_post_input_literal():
    after = snapshot(node(**{"class": "android.widget.EditText", "focused": "true", "text": "hi"}))
    result = analyze_ui_transition(
        parse_ui_tree(None), after, {"action_type": "input_text", "text": "hi"}
    )
    assert rule(result, "field_changes").reason == "BEFORE_SAMPLE_UNAVAILABLE"
    assert rule(result, "current_sample").status == "RECORDED"
    assert values(result, "input_value")["after_equals_request"]


def test_current_sample_gap_retains_closed_parser_reason():
    result = analyze_ui_transition(snapshot(node()), parse_ui_tree(None), CLICK)
    assert values(result, "current_sample")["sample_reason"] == "NOT_AVAILABLE"


def test_sample_signature_includes_rotation_and_missing_field_shape():
    first = snapshot(node(enabled=None))
    second = snapshot(node(enabled="true"))
    rotated = snapshot(node(enabled=None), rotation="1")
    assert first.sample_key and second.sample_key and rotated.sample_key
    assert len({first.sample_key, second.sample_key, rotated.sample_key}) == 3


def test_action_key_includes_containing_context_not_just_surviving_button():
    before = snapshot(container(node(), item("Page one"), scrollable="false"))
    after = snapshot(container(node(), item("Page two"), scrollable="false"))
    result = analyze_ui_transition(before, after, CLICK)
    assert values(result, "action_target")["fields_changed"] == 0
    assert result.action_before_key and result.action_after_key
    assert result.action_before_key != result.action_after_key


def test_top_level_unrelated_clock_does_not_change_root_control_scope_key():
    before = snapshot(node(), item("12:00", bounds="[200,0][300,30]"))
    after = snapshot(node(), item("12:01", bounds="[200,0][300,30]"))
    result = analyze_ui_transition(before, after, CLICK)
    assert result.action_before_key and result.action_before_key == result.action_after_key
    assert before.sample_key != after.sample_key


def test_same_parent_private_or_missing_sibling_does_not_establish_repeat_equality():
    state = snapshot(container(node(), item("Private", password="true"), scrollable="false"))
    result = analyze_ui_transition(state, state, CLICK)
    assert values(result, "action_target")["matched"]
    assert result.action_before_key is result.action_after_key is None
