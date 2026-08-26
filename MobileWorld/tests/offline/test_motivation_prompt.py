from __future__ import annotations

import hashlib

from mobile_world.offline.motivation_prompt import (
    PREVIOUS_PROMPT_VERSION,
    PROMPT_VERSION,
    build_adjudication_prompt,
    build_previous_review_prompt_v2,
    build_review_prompt,
)


def _review_prompt() -> str:
    return build_review_prompt(
        phase="PASS1",
        batch_id="pass1-0001-c111-c111",
        reviewer_id="reviewer-primary",
        cases=[],
    )


def test_prompt_v3_defines_gui_owl_action_history_semantics() -> None:
    prompt = _review_prompt()

    assert PROMPT_VERSION == "mobileworld.audit.motivation-codex-prompt/v3"
    assert f"prompt_version={PROMPT_VERSION}" in prompt
    assert "hybrid_folding" in prompt
    assert "ACTION_EXECUTION_CLAIM" in prompt
    assert '"Ask the user"' in prompt
    assert "向上滚动/向上滑动" in prompt
    assert '"Drag from (x1,y1) to (x2,y2)"' in prompt
    assert "a static UI alone never refutes action execution" in prompt
    assert "OFFTRACK_TRUE with TRUE_BUT_OFFTRACK" in prompt
    assert "REFUTED with RESULT_MISALIGNMENT" in prompt
    assert "use FALSE_CLAIM or FALSE_SUCCESS" in prompt
    assert "not RESULT_MISALIGNMENT" in prompt
    assert "ACTION_INTENT or PLAN receives NOT_A_FACTUAL_CLAIM" in prompt


def test_prompt_v3_keeps_validity_uptake_effects_and_outcomes_independent() -> None:
    prompt = _review_prompt()

    assert (
        "Label history validity, observed uptake, state confound, and downstream effects "
        "independently"
    ) in prompt
    assert "An accurate off-task action does not itself prove target-step uptake or harm" in prompt
    assert "Natural trajectories support exposure/propagation/association, never" in prompt
    assert "Never seek, guess, or use the official task outcome or score" in prompt


def test_adjudication_uses_the_same_action_semantics() -> None:
    prompt = build_adjudication_prompt(
        batch_id="adjudication-0001-c111-c111",
        reviewer_id="reviewer-adjudicator",
        cases=[],
    )

    assert f"prompt_version={PROMPT_VERSION}" in prompt
    assert 'Task111-style "Ask the user"' in prompt
    assert "REFUTED with RESULT_MISALIGNMENT" in prompt


def test_previous_v2_prompt_remains_reconstructable_without_v3_rules() -> None:
    prompt = build_previous_review_prompt_v2(
        phase="PASS1",
        batch_id="pass1-0001-c111-c111",
        reviewer_id="reviewer-primary",
        cases=[],
    )

    assert PREVIOUS_PROMPT_VERSION == "mobileworld.audit.motivation-codex-prompt/v2"
    assert f"prompt_version={PREVIOUS_PROMPT_VERSION}" in prompt
    assert "Task111-style" not in prompt
    assert "a static UI alone never refutes action execution" not in prompt
    assert hashlib.sha256(prompt.encode()).hexdigest() == (
        "265cc0f0bf7c8b3df4d600cf5117e502df2f9b56629b51c0d7ca3f5e64c1002b"
    )
