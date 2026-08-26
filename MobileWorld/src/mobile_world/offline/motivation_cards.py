"""Deterministic reconstruction and compact review cards for curated runs.

This module is an offline, derived-data consumer.  It never writes beneath a
curated manifest or any referenced raw run.  Candidate signals are retrieval
facts only; they are deliberately not history-validity, uptake, harm, or
causality labels.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from functools import lru_cache
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image

from mobile_world.agents.utils.prompts.gelab import (
    GELAB_INSTRUCTION_SUFFIX,
    GELAB_SYSTEM_PROMPT,
    GELAB_USER_PROMPT_TEMPLATE,
)
from mobile_world.agents.utils.prompts.gui_owl_1_5 import (
    GUI_OWL_1_5_USER_PROMPT_TEMPLATE,
    GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE,
)
from mobile_world.agents.utils.prompts.memgui import (
    MEMGUI_SYSTEM_PROMPT,
    MEMGUI_USER_TEMPLATE,
)
from mobile_world.agents.utils.prompts.ui_venus import UI_VENUS_15_PROMPT
from mobile_world.offline.curated_composite import validate_curated_composite
from mobile_world.offline.motivation_review import (
    REVIEW_SCHEMA_VERSION,
    canonical_sha256,
    validate_task_cards,
)
from mobile_world.runtime.audit.schemas import SchemaValidationError, validate_event_envelope

SCHEMA_VERSION = "mobileworld.audit.motivation-cards/v1"
BUILDER_VERSION = "mobileworld.audit.motivation-cards-builder/v7"

NEAR_REASONING_LOOKBACK = 5
NEAR_REASONING_JACCARD_THRESHOLD = 0.78
NEAR_REASONING_MIN_TOKENS = 10
REPEATED_ACTION_LOOKBACK = 8
ACTION_GRID_PX = 48
LONG_LAG_MINIMUM = 4
MAX_STRUCTURAL_CANDIDATES_PER_TASK = 4
MAX_GELAB_FORMAL_CANDIDATES_PER_TASK = 4
MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK = 4
MAX_MEMGUI_FORMAL_CANDIDATES_PER_TASK = 4
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1

_HIGH_PRECISION_SIGNALS = frozenset(
    {
        "FAILED_TRANSITION",
        "FAILED_TRANSITION_ACK",
        "PROGRESS_CLAIM",
        "PROVIDER_DECISION_DIFFERENCE",
        "SELF_CORRECTION",
    }
)
_STRUCTURAL_SIGNALS = frozenset(
    {"NEAR_DUPLICATE_REASONING", "REPEATED_ACTION", "STATIC_TRANSITION"}
)
_GELAB_CRITICAL_SIGNALS = frozenset(
    {
        "FAILED_TRANSITION",
        "FAILED_TRANSITION_ACK",
        "PROVIDER_DECISION_DIFFERENCE",
        "SELF_CORRECTION",
    }
)
_GUI_OWL_CRITICAL_SIGNALS = frozenset(
    {
        "ACTION_EXECUTION_MISMATCH",
        "FAILED_TRANSITION",
        "FAILED_TRANSITION_ACK",
        "PROVIDER_DECISION_DIFFERENCE",
        "SELF_CORRECTION",
    }
)
_MEMGUI_CRITICAL_SIGNALS = frozenset(
    {
        "FAILED_TRANSITION",
        "FAILED_TRANSITION_ACK",
        "PROVIDER_DECISION_DIFFERENCE",
        "SELF_CORRECTION",
    }
)
_MEMGUI_REPRESENTATION_SIGNALS = frozenset({"STRUCTURED_MEMORY_ENTRY", "STRUCTURED_SPAN_FOLD"})

_MESSAGE_INDEX_RE = re.compile(r"^messages\[(\d+)\]")
_QWEN_STEP_DELIMITER_RE = re.compile(r"; Step [1-9]\d*: ")
_GELAB_THINK_TAG_RE = re.compile(r"<\s*/?(?:THINK|think|TINK|tink)\s*>")
_THINK_RE = re.compile(r"<(?:think|thinking)>(.*?)</(?:think|thinking)>", re.IGNORECASE | re.DOTALL)
_TOOL_BLOCK_RE = re.compile(r"<tool_call>.*?</tool_call>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_TOKEN_RE = re.compile(r"[a-z0-9']+|[\u4e00-\u9fff]")
_SPACE_RE = re.compile(r"\s+")

_GUI_OWL_ACTION_HISTORY_SIGNAL = "ACTION_HISTORY_ENTRY"
_GUI_OWL_ACTION_MISMATCH_SIGNAL = "ACTION_EXECUTION_MISMATCH"
_GUI_OWL_IMPERATIVE_FILLER_RE = re.compile(
    r"^(?:(?:please|then|now|again)\s+|continue(?:\s+to)?\s+|"
    r"(?:请|然后|接着|继续|再次)\s*)+",
    re.IGNORECASE,
)
_GUI_OWL_ACTION_OPERATION_PATTERNS = (
    (
        "long_press",
        re.compile(
            r"^(?:long[\s-]*press|press\s+and\s+hold|tap\s+and\s+hold|长按)",
            re.IGNORECASE,
        ),
    ),
    (
        "keyboard_enter",
        re.compile(
            r"^(?:(?:press|hit)\s+(?:the\s+)?(?:enter|return)(?:\s+key)?\b|"
            r"(?:按|按下|点击)\s*(?:回车|enter)(?:键)?)",
            re.IGNORECASE,
        ),
    ),
    (
        "navigate_back",
        re.compile(
            r"^(?:(?:navigate|go)\s+back\b|return\s+back\b|"
            r"(?:press|hit)\s+(?:the\s+)?back(?:\s+(?:button|key))?\b|"
            r"(?:返回|回到)(?!主屏幕|桌面)|(?:按|按下)\s*返回(?:键)?)",
            re.IGNORECASE,
        ),
    ),
    (
        "navigate_home",
        re.compile(
            r"^(?:(?:navigate|go|return)\s+(?:to\s+)?(?:the\s+)?home\s+screen\b|"
            r"(?:press|hit)\s+(?:the\s+)?home(?:\s+(?:button|key))?\b|"
            r"(?:返回|回到)(?:主屏幕|桌面)|(?:按|按下)\s*home(?:键)?)",
            re.IGNORECASE,
        ),
    ),
    (
        "drag",
        re.compile(
            r"^(?:drag(?:ging)?|swipe|scroll(?:ing)?|slide|"
            r"滑动|滚动|向[上下左右](?:滑|滑动|滚动)|[上下左右]滑|拖动|拖拽)",
            re.IGNORECASE,
        ),
    ),
    (
        "ask_user",
        re.compile(
            r"^(?:ask\s+(?:the\s+)?user\b|request\s+(?:the\s+)?user\b|"
            r"prompt\s+(?:the\s+)?user\b|询问用户|请求用户|请用户|向用户(?:询问|确认|请求))",
            re.IGNORECASE,
        ),
    ),
    ("wait", re.compile(r"^(?:wait(?:\s+for)?\b|等待|稍等)", re.IGNORECASE)),
    (
        "input_text",
        re.compile(
            r"^(?:type|input|enter|paste|fill(?:\s+in)?)\b|^(?:输入|键入|填写|粘贴)",
            re.IGNORECASE,
        ),
    ),
    (
        "click",
        re.compile(
            r"^(?:click(?:ing)?|tap|press|select|check|uncheck|toggle)\b|"
            r"^(?:点击|点按|单击|按下|按|选择|勾选|取消勾选)",
            re.IGNORECASE,
        ),
    ),
    ("open_ui", re.compile(r"^(?:open|launch)\b|^(?:打开|启动)", re.IGNORECASE)),
    (
        "finished",
        re.compile(r"^(?:finish|terminate|complete)\b|^(?:结束任务|完成任务)", re.IGNORECASE),
    ),
    ("answer", re.compile(r"^answer\b|^回答", re.IGNORECASE)),
)
_GUI_OWL_COMPATIBLE_ACTION_TYPES = {
    "long_press": frozenset({"long_press"}),
    "keyboard_enter": frozenset({"keyboard_enter"}),
    "navigate_back": frozenset({"navigate_back"}),
    "navigate_home": frozenset({"navigate_home"}),
    "drag": frozenset({"drag"}),
    "ask_user": frozenset({"ask_user"}),
    "wait": frozenset({"wait"}),
    "input_text": frozenset({"input_text"}),
    "click": frozenset({"click"}),
    "open_ui": frozenset({"click", "open_app"}),
    "finished": frozenset({"finished"}),
    "answer": frozenset({"answer"}),
}
_GUI_OWL_TERMINAL_META_ACTION_TYPES = frozenset({"answer", "finished"})
_GUI_OWL_BACK_CONTROL_RE = re.compile(
    r"(?:\bback\b.{0,24}\b(?:arrow|button|icon|control)\b|"
    r"\b(?:arrow|button|icon|control)\b.{0,24}\bback\b|返回(?:箭头|按钮|键|图标))",
    re.IGNORECASE,
)
_GUI_OWL_HOME_CONTROL_RE = re.compile(
    r"(?:\bhome\b.{0,24}\b(?:button|icon|control)\b|"
    r"\b(?:button|icon|control)\b.{0,24}\bhome\b|主页(?:按钮|键|图标))",
    re.IGNORECASE,
)
_GUI_OWL_BACK_TO_HOME_RE = re.compile(
    r"(?:\bback\s+to\s+(?:the\s+)?home\s+screen\b|返回(?:到)?(?:主屏幕|桌面))",
    re.IGNORECASE,
)
_GUI_OWL_QUOTED_TEXT_ARGUMENT_PATTERNS = (
    re.compile(
        r"^(?:type|input|enter|paste)(?:\s+(?:the\s+)?"
        r"(?:text|message|value|title|name|url))?(?:\s+(?:as|value))?\s*"
        r'"([^\"]+)"',
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:type|input|enter|paste)(?:\s+(?:the\s+)?"
        r"(?:text|message|value|title|name|url))?(?:\s+(?:as|value))?\s*"
        r"“([^”]+)”",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:type|input|enter|paste)(?:\s+(?:the\s+)?"
        r"(?:text|message|value|title|name|url))?(?:\s+(?:as|value))?\s*"
        r"'((?:[^']|'(?=[A-Za-z]))+)'(?=\s|[.,;:!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:type|input|enter|paste)(?:\s+(?:the\s+)?"
        r"(?:text|message|value|title|name|url))?(?:\s+(?:as|value))?\s*"
        r"‘([^’]+)’",
        re.IGNORECASE,
    ),
    re.compile(r'^(?:输入|键入|填写|粘贴)(?:文本|内容|消息|名称|标题|网址)?\s*"([^\"]+)"'),
    re.compile(r"^(?:输入|键入|填写|粘贴)(?:文本|内容|消息|名称|标题|网址)?\s*“([^”]+)”"),
    re.compile(
        r"^(?:输入|键入|填写|粘贴)(?:文本|内容|消息|名称|标题|网址)?\s*"
        r"'((?:[^']|'(?=[A-Za-z]))+)'(?=\s|[.,;:!?]|$)"
    ),
    re.compile(r"^(?:输入|键入|填写|粘贴)(?:文本|内容|消息|名称|标题|网址)?\s*‘([^’]+)’"),
)
_GUI_OWL_PROSPECTIVE_ACTION_RE = re.compile(
    r"^(?:(?:i|we)\s+(?:will|plan\s+to|need\s+to|intend\s+to|"
    r"(?:am|are)\s+going\s+to)\b|(?:plan|need)\s+to\b|"
    r"(?:我|我们)(?:将|会|计划|需要|打算)|(?:下一步|计划|需要|准备))",
    re.IGNORECASE,
)

_QWEN_ADAPTER = "qwen3vl"
_GELAB_ADAPTER = "gelab_agent"
_GUI_OWL_ADAPTER = "gui_owl_1_5"
_MEMGUI_ADAPTER = "memgui"
_UI_VENUS_ADAPTER = "ui_venus_agent"
_RAW_REPLAY_ADAPTERS = frozenset({"mai_ui_agent"})
_QWEN_USER_PREFIX = "\nThe user query: "
_QWEN_PROGRESS_MARKER = (
    "Task progress (You have done the following operation on the current device): "
)
_GELAB_EMPTY_HISTORY = "暂无历史操作"
_GELAB_USER_RESPONSE_PREFIX = " 用户回复说："
_GELAB_USER_PROMPT_SUFFIX = "\n当前手机屏幕截图如下："
_UI_VENUS_SYSTEM_MESSAGE = "You are a helpful assistant."
_UI_VENUS_HISTORY_PLACEHOLDER = "{previous_actions}"
_UI_VENUS_TASK_PLACEHOLDER = "{user_task}"
_MEMGUI_MEMORY_ACTIONS = frozenset({"memory_add", "memory_update", "memory_delete"})
_MEMGUI_ACTION_TYPES = frozenset(
    {
        "click",
        "long_press",
        "swipe",
        "type",
        "answer",
        "system_button",
        "wait",
        "terminate",
        *_MEMGUI_MEMORY_ACTIONS,
    }
)
_MEMGUI_NON_MEMORY_ACTION_MAP = {
    "click": "click",
    "long_press": "long_press",
    "swipe": "drag",
    "type": "input_text",
    "answer": "answer",
    "wait": "wait",
    "terminate": "finished",
}
_MEMGUI_SYSTEM_BUTTON_ACTION_MAP = {
    "back": "navigate_back",
    "home": "navigate_home",
    "enter": "keyboard_enter",
}
_MEMGUI_TEMPLATE_FIELDS = (
    "instruction",
    "state_summaries",
    "latest_interaction",
    "memory_state",
    "folding_instruction",
)
_MEMGUI_HISTORY_SECTION_ORDER = {"H": 0, "L": 1, "M": 2}

_SELF_CORRECTION_PATTERNS = (
    (
        "explicit_wrong",
        re.compile(
            r"\b(?:i (?:was|am) wrong|my mistake|i (?:made|have made) (?:a |the )?mistake)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "misread_or_confused",
        re.compile(
            r"\b(?:misread|misinterpreted|misunderstood|mixed up|confused|misclicked|oops)\b",
            re.IGNORECASE,
        ),
    ),
    ("chinese_correction", re.compile(r"(?:我错了|弄错了|误读|误点|搞混了|看错了)")),
)

_FAILED_TRANSITION_PATTERNS = (
    (
        "did_not_work",
        re.compile(
            r"\b(?:did not|didn't|has not|hasn't) "
            r"(?:work|open|change|move|advance|respond|register|save|send|select|toggle)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "no_visible_change",
        re.compile(
            r"\b(?:nothing (?:happened|changed|moved|opened)|no (?:visible )?"
            r"(?:change|response|progress))\b",
            re.IGNORECASE,
        ),
    ),
    ("chinese_failed_transition", re.compile(r"(?:没有生效|没反应|没有变化|点击失败|操作失败)")),
)

_PROGRESS_CLAIM_PATTERNS = (
    (
        "success_adverb",
        re.compile(r"\b(?:successfully|that (?:worked|succeeded))\b", re.IGNORECASE),
    ),
    (
        "completed_action",
        re.compile(
            r"\b(?:i (?:have|just) |has been |is now |are now )"
            r"(?:opened|added|removed|saved|sent|selected|completed|finished|set|"
            r"enabled|disabled|created|bookmarked|followed|unfollowed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "task_completion",
        re.compile(
            r"\b(?:task (?:is )?(?:complete|completed|done|finished)|"
            r"that completes? (?:the |this )?task|everything (?:is|has been) "
            r"(?:set|done|complete|completed))\b",
            re.IGNORECASE,
        ),
    ),
    ("chinese_progress", re.compile(r"(?:已成功|已经完成|任务完成|已经保存|已经发送|已经创建)")),
)

_CONTENT_STOPWORDS = frozenset(
    """
    the a an to of and or is are was were be been being i we you it this that in
    on for with as at by from then so now need want can could should would will
    just my our your their its do did does have has had not no yes but first next
    here there right okay got get let me
    """.split()
)


class MotivationCardError(RuntimeError):
    """A curated input or requested derived output violates an invariant."""

    def __init__(self, code: str, message: str, **context: Any) -> None:
        super().__init__(message)
        self.code = code
        self.context = context


class _DuplicateJsonKey(ValueError):
    pass


def canonical_json_bytes(value: Any, *, newline: bool = True) -> bytes:
    """Encode deterministic UTF-8 JSON, rejecting non-finite numbers."""

    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return data + (b"\n" if newline else b"")


def generate_motivation_artifacts(
    *,
    manifest_path: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
    verify_blob_digests: bool = True,
) -> dict[str, Any]:
    """Validate a curated task set and deterministically reconstruct every task.

    The return value contains only derived in-memory documents.  This function
    performs no writes.  The curated validator is always run before any task is
    reconstructed.
    """

    manifest_file = Path(manifest_path).resolve(strict=True)
    base = Path(source_base).resolve(strict=True)
    validation = validate_curated_composite(
        manifest_path=manifest_file,
        source_base=base,
        verify_blob_digests=verify_blob_digests,
    )
    if validation.get("valid") is not True:
        raise MotivationCardError(
            "curated_manifest_invalid",
            "curated manifest validation failed",
            errors=validation.get("errors", []),
        )

    manifest_bytes = manifest_file.read_bytes()
    manifest = _loads_object(manifest_bytes, manifest_file)
    _require(
        manifest.get("schema_version") == "mobileworld.audit.curated-task-set/v1",
        "curated_schema_unsupported",
        "only mobileworld.audit.curated-task-set/v1 is supported",
    )
    _require(
        manifest.get("artifact_type") == "derived_task_selection"
        and manifest.get("is_raw_run") is False,
        "curated_artifact_invalid",
        "input must be a zero-copy derived task selection",
    )

    source_entries, source_roots = _resolve_sources(manifest, base)
    tasks = manifest.get("tasks")
    _require(isinstance(tasks, list), "curated_tasks_invalid", "tasks must be a list")
    ordered_tasks = sorted(tasks, key=lambda item: _positive_int(item, "canonical_suite_index"))
    expected_indices = list(range(1, len(ordered_tasks) + 1))
    _require(
        [_positive_int(task, "canonical_suite_index") for task in ordered_tasks]
        == expected_indices,
        "curated_task_indices_invalid",
        "canonical task indices must be contiguous from one",
    )

    task_cards: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    reconstructions: list[dict[str, Any]] = []
    dataset_sha256 = _sha256(manifest_bytes)
    selection_sha256 = _string(manifest, "selection_sha256")
    evaluation_run_id = _stable_id(
        "motivation-review",
        {
            "builder_version": BUILDER_VERSION,
            "dataset_sha256": dataset_sha256,
            "selection_sha256": selection_sha256,
        },
    )

    for task in ordered_tasks:
        source_id = _string(task, "source_id")
        _require(
            source_id in source_roots,
            "task_source_missing",
            "selected task refers to an unknown source",
            source_id=source_id,
        )
        stream_summary = _mapping(task, "task_stream")
        relative = _safe_relative_path(
            _string(stream_summary, "relative_path"), code="task_stream_path_invalid"
        )
        stream_path = source_roots[source_id].joinpath(*relative.parts)
        stream_bytes = stream_path.read_bytes()
        _require_file_summary(stream_summary, stream_bytes, stream_path)
        events = _jsonl_documents(stream_bytes, stream_path)
        result = reconstruct_task_events(
            task_entry=task,
            source_entry=source_entries[source_id],
            events=events,
            evaluation_run_id=evaluation_run_id,
            dataset_sha256=dataset_sha256,
            selection_sha256=selection_sha256,
            blob_reader=lambda ref, root=source_roots[source_id]: _read_verified_blob_ref(
                root, ref
            ),
        )
        task_cards.append(result["task_card"])
        outcomes.append(result["outcome_sidecar"])
        reconstructions.append(result["reconstruction"])

    validated_cards = validate_task_cards(
        {card["task"]["task_name"]: card for card in task_cards},
        expected_task_count=len(task_cards),
    )
    task_cards = sorted(validated_cards.values(), key=lambda card: card["task"]["catalog_index"])
    candidate_count = sum(len(card["candidates"]) for card in task_cards)
    candidate_reason_counts = Counter(
        reason
        for card in task_cards
        for candidate in card["candidates"]
        for reason in candidate["retrieval_reasons"]
    )
    long_lag_exposure_count = sum(
        exposure["lag"] >= LONG_LAG_MINIMUM and exposure["source_evidence_image_absent"]
        for reconstruction in reconstructions
        for step in reconstruction["steps"]
        for exposure in step["I_t"]["assistant_exposures"]
    )
    manifest_document = {
        "schema_version": SCHEMA_VERSION,
        "builder_version": BUILDER_VERSION,
        "artifact_type": "derived_outcome_blinded_review_bundle",
        "input": {
            "curated_dataset_id": manifest.get("dataset_id"),
            "curated_manifest_sha256": dataset_sha256,
            "curated_selection_sha256": manifest.get("selection_sha256"),
            "raw_schema_version": manifest.get("raw_schema_version"),
        },
        "configuration": {
            "action_grid_px": ACTION_GRID_PX,
            "long_lag_minimum": LONG_LAG_MINIMUM,
            "max_gelab_formal_candidates_per_task": MAX_GELAB_FORMAL_CANDIDATES_PER_TASK,
            "max_gui_owl_formal_candidates_per_task": MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK,
            "max_memgui_formal_candidates_per_task": MAX_MEMGUI_FORMAL_CANDIDATES_PER_TASK,
            "max_structural_candidates_per_task": MAX_STRUCTURAL_CANDIDATES_PER_TASK,
            "near_reasoning_jaccard_threshold": NEAR_REASONING_JACCARD_THRESHOLD,
            "near_reasoning_lookback": NEAR_REASONING_LOOKBACK,
            "near_reasoning_min_tokens": NEAR_REASONING_MIN_TOKENS,
            "repeated_action_lookback": REPEATED_ACTION_LOOKBACK,
            "candidate_semantics": "retrieval_only_not_an_evaluation_label",
            "long_lag_candidate_policy": (
                "preserve_all_exposures_in_reconstruction; add retrieval reason only when the "
                "same source-target pair has an independent content_or_behavior_signal"
            ),
            "structural_candidate_policy": (
                "retain every high-precision textual/transition signal; require at least two "
                "independent structural signals; then select at most four structurally strongest "
                "source-target pairs with deterministic temporal coverage per task"
            ),
            "gelab_rolling_summary_candidate_policy": {
                "applies_to": "gelab_agent rolling_summary formal review candidates only",
                "ordinary_eligibility_first": True,
                "priority_tiers": [
                    (
                        "critical: FAILED_TRANSITION, FAILED_TRANSITION_ACK, SELF_CORRECTION, "
                        "substantive PROVIDER_DECISION_DIFFERENCE"
                    ),
                    "PROGRESS_CLAIM plus at least one independent structural signal",
                    "at least two independent structural signals",
                    "PROGRESS_CLAIM only",
                ],
                "selection": (
                    "fill higher-priority tiers before lower tiers; within an overflowing tier, "
                    "choose deterministic best-ranked pairs across target-step temporal quartiles"
                ),
                "reconstruction_retention": (
                    "preserve every exact rolling-summary exposure in reconstruction_refs.jsonl"
                ),
                "interpretation": (
                    "bounded formal candidates are a conservative review subset/lower bound, "
                    "not the exposure denominator; dropped_candidate_count remains zero because "
                    "the bounded selection is the declared scanner policy"
                ),
            },
            "gui_owl_collapsed_history_policy": {
                "applies_to": "gui_owl_1_5 with formal history_n=1 only",
                "representation_type": "hybrid_folding",
                "mapping_status": "exact_gui_owl_collapsed_history_n1",
                "runtime_replay": (
                    "all prior accepted Action conclusions are rendered as cumulative "
                    "one-based StepN lines with the action result carried by observation N+1"
                ),
                "result_alignment": (
                    "Tool response comes from the next observation: tool result first, then "
                    "ask-user result, otherwise the literal None placeholder"
                ),
                "exposure_retention": (
                    "preserve every exact source-target appearance in reconstruction_refs.jsonl"
                ),
                "claim_boundary": (
                    "the actor-authored Action conclusion is the minimal review claim; the "
                    "aligned Tool response is retained as a separate exact target-request "
                    "evidence span and is never folded into the action claim"
                ),
                "claim_typing": (
                    "because the runtime places completed Action conclusions under Previous "
                    "actions, a pure imperative is ACTION_EXECUTION_CLAIM; only explicitly "
                    "prospective wording is ACTION_INTENT and an independently retrieved "
                    "completion assertion remains SUCCESS_CLAIM"
                ),
                "action_alignment_retrieval": (
                    "use an anchored bilingual imperative grammar plus the exact parsed action "
                    "and matching action_execution_started copy to retrieve high-confidence "
                    "operation or explicit text-argument mismatches; abstain on terminal answer "
                    "or finished meta-actions whose short Action prose is not an unambiguous "
                    "device-operation assertion; do not use task outcome, task score, post-state "
                    "success, screenshot change, or static pixels"
                ),
                "candidate_selection": (
                    "retain every high-confidence ACTION_EXECUTION_MISMATCH even when a task "
                    "has more than four; otherwise fill a total ordinary budget of four with "
                    "critical/progress/structural hits and temporally spread first/immediate "
                    "ACTION_HISTORY_ENTRY exposures, so every task with exposed prior actions "
                    "has an outcome-blind action-history sample"
                ),
                "interpretation": (
                    "ACTION_HISTORY_ENTRY is retrieval-only sampling and "
                    "ACTION_EXECUTION_MISMATCH is a mechanical alignment retrieval signal; "
                    "neither is a validity, misleading-history, uptake, or harm label. The card "
                    "set is a conservative review subset/lower bound, while the full exact "
                    "appearance ledger remains the exposure denominator"
                ),
                "current_image_proof": (
                    "require exactly one authoritative request image and RGB pixel-matrix "
                    "equality with the current observation"
                ),
            },
            "memgui_structured_folding_policy": {
                "applies_to": "memgui only",
                "representation_type": "structured_folding",
                "mapping_status": "exact_memgui_structured_hlm",
                "runtime_replay": (
                    "replay H folded summaries with destructive overlap replacement, "
                    "L latest accepted interaction, and insertion-ordered M memory "
                    "add/update/delete state"
                ),
                "claim_boundary": (
                    "review only actor-authored summary, UI-observation, action-intent, "
                    "description, and content text; retain runtime-derived L action text "
                    "separately in the exact exposed span"
                ),
                "claim_typing": (
                    "PROGRESS_CLAIM text is SUCCESS_CLAIM; otherwise H is SUMMARY_CLAIM, "
                    "M is OBSERVATION_CLAIM, and L is OBSERVATION_CLAIM when it has UI text "
                    "or ACTION_INTENT when it contains intent only"
                ),
                "exposure_retention": (
                    "preserve every exact H/L/M source-entry appearance in "
                    "reconstruction_refs.jsonl"
                ),
                "candidate_selection": (
                    "at source-target-entry granularity, make every M version eligible and sample "
                    "span-H entries as representation-specific retrieval facts alongside "
                    "shared eligibility; then retain at most four outcome-blind formal "
                    "candidates per task by M/critical/span/progress/structural priority, "
                    "H/L/M diversity, and deterministic target-step temporal coverage"
                ),
                "interpretation": (
                    "the bounded card set is a conservative review subset/lower bound, not "
                    "the complete exposure denominator"
                ),
                "current_image_proof": (
                    "require exactly one authoritative request image and RGB pixel-matrix "
                    "equality with the current observation"
                ),
                "evidence_image_presence": (
                    "compare source and post-state screenshots to request images by decoded "
                    "RGB pixel matrix with a task-local fingerprint cache"
                ),
            },
            "ui_venus_flat_previous_actions_policy": {
                "applies_to": "ui_venus_agent only",
                "representation_type": "flat_previous_actions",
                "mapping_status": "exact_ui_venus_flat_previous_actions",
                "runtime_replay": (
                    "cumulative zero-based Step N entries rendered exactly as "
                    "<think>...</think><action>...</action>"
                ),
                "included_history_fields": ["think", "action"],
                "excluded_history_fields": ["conclusion", "status"],
                "exposure_retention": (
                    "preserve every exact source-target exposure in reconstruction_refs.jsonl"
                ),
                "candidate_selection": (
                    "use the shared deterministic retrieval selector and shared structural "
                    "candidate bound; no UI-Venus-specific candidate bound"
                ),
                "current_image_proof": (
                    "require exactly one authoritative request image, exact request-view and "
                    "request-images provenance agreement, and RGB pixel-matrix equality with "
                    "the current observation"
                ),
            },
            "outcome_app_source": "task_definition_top_level_directory",
            "outcome_blinding": "environment outcome stored only in outcomes.sidecar.jsonl",
        },
        "counts": {
            "task_count": len(task_cards),
            "step_count": sum(card["coverage"]["decision_count"] for card in task_cards),
            "history_bearing_request_count": sum(
                card["coverage"]["history_bearing_decision_count"] for card in task_cards
            ),
            "unique_exposed_history_entry_count": sum(
                card["coverage"]["unique_history_claim_count"] for card in task_cards
            ),
            "unique_exposed_source_step_count": sum(
                len(
                    {
                        exposure["source_step_index"]
                        for step in reconstruction["steps"]
                        for exposure in step["I_t"]["assistant_exposures"]
                    }
                )
                for reconstruction in reconstructions
            ),
            "assistant_exposure_count": sum(
                card["coverage"]["actual_exposure_count"] for card in task_cards
            ),
            "candidate_count": candidate_count,
            "candidate_count_by_retrieval_reason": dict(sorted(candidate_reason_counts.items())),
            "gui_owl_action_history_candidate_count": candidate_reason_counts.get(
                _GUI_OWL_ACTION_HISTORY_SIGNAL, 0
            ),
            "gui_owl_action_execution_mismatch_candidate_count": candidate_reason_counts.get(
                _GUI_OWL_ACTION_MISMATCH_SIGNAL, 0
            ),
            "gui_owl_task_count_with_action_history_candidates": sum(
                any(
                    _GUI_OWL_ACTION_HISTORY_SIGNAL in candidate["retrieval_reasons"]
                    for candidate in card["candidates"]
                )
                for card in task_cards
            ),
            "gui_owl_task_count_with_action_execution_mismatch_candidates": sum(
                any(
                    _GUI_OWL_ACTION_MISMATCH_SIGNAL in candidate["retrieval_reasons"]
                    for candidate in card["candidates"]
                )
                for card in task_cards
            ),
            "long_lag_image_absent_exposure_count": long_lag_exposure_count,
        },
    }
    return {
        "manifest": manifest_document,
        "task_cards": task_cards,
        "outcome_sidecars": outcomes,
        "reconstructions": reconstructions,
    }


def write_motivation_artifacts(
    *, artifacts: Mapping[str, Any], output_dir: str | os.PathLike[str]
) -> dict[str, Any]:
    """Exclusively publish canonical derived files beneath a new directory."""

    output = Path(output_dir).resolve(strict=False)
    _require(
        output.parent.is_dir(),
        "output_parent_missing",
        "output directory parent must already exist",
        output_parent=str(output.parent),
    )
    if output.exists():
        raise MotivationCardError(
            "output_exists", "derived output directory already exists", output_dir=str(output)
        )

    documents = {
        "task_cards.jsonl": _jsonl_bytes(artifacts["task_cards"]),
        "outcomes.sidecar.jsonl": _jsonl_bytes(artifacts["outcome_sidecars"]),
        "reconstruction_refs.jsonl": _jsonl_bytes(artifacts["reconstructions"]),
    }
    file_summaries = {
        name: {
            "byte_count": len(data),
            "record_count": len(data.splitlines()),
            "sha256": _sha256(data),
        }
        for name, data in documents.items()
    }
    bundle_manifest = _json_clone(artifacts["manifest"])
    bundle_manifest["files"] = file_summaries
    documents["manifest.json"] = canonical_json_bytes(bundle_manifest)

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent))
    staging.chmod(0o700)
    try:
        for name, data in documents.items():
            _write_exclusive(staging / name, data)
        _fsync_directory(staging)
        _rename_directory_noreplace(staging, output)
        _fsync_directory(output.parent)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return {
        "valid": True,
        "output_dir": str(output),
        "task_count": bundle_manifest["counts"]["task_count"],
        "candidate_count": bundle_manifest["counts"]["candidate_count"],
        "manifest_sha256": _sha256(documents["manifest.json"]),
        "files": file_summaries,
    }


def _rename_directory_noreplace(source: Path, target: Path) -> None:
    """Atomically publish without replacing an existing target directory."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise MotivationCardError(
            "atomic_publish_unsupported",
            "libc renameat2(RENAME_NOREPLACE) is required for exclusive publication",
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise MotivationCardError(
            "output_exists",
            "exclusive publication refused an existing target",
            output_dir=str(target),
        )
    raise MotivationCardError(
        "atomic_publish_failed",
        "renameat2(RENAME_NOREPLACE) failed",
        source=str(source),
        target=str(target),
        errno=error_number,
        error=os.strerror(error_number),
    )


def generate_and_write_motivation_artifacts(
    *,
    manifest_path: str | os.PathLike[str],
    source_base: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    verify_blob_digests: bool = True,
) -> dict[str, Any]:
    """Validate, reconstruct, and publish without permitting writes to inputs."""

    manifest_file = Path(manifest_path).resolve(strict=True)
    base = Path(source_base).resolve(strict=True)
    manifest = _loads_object(manifest_file.read_bytes(), manifest_file)
    _, source_roots = _resolve_sources(manifest, base)
    output = Path(output_dir).resolve(strict=False)
    forbidden = [manifest_file.parent, *source_roots.values()]
    for root in forbidden:
        _require(
            not _is_within(output, root),
            "output_overlaps_input",
            "derived output must be outside the curated and raw input trees",
            output_dir=str(output),
            input_root=str(root),
        )
    artifacts = generate_motivation_artifacts(
        manifest_path=manifest_file,
        source_base=base,
        verify_blob_digests=verify_blob_digests,
    )
    return write_motivation_artifacts(artifacts=artifacts, output_dir=output)


def reconstruct_task_events(
    *,
    task_entry: Mapping[str, Any],
    source_entry: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    evaluation_run_id: str,
    dataset_sha256: str,
    selection_sha256: str,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None = None,
) -> dict[str, Any]:
    """Reconstruct one selected task using IDs, independent of input order."""

    ordered, event_by_id = _index_events(task_entry, source_entry, events)
    task_started = _only_event(ordered, "task_started")
    task_ended = _only_event(ordered, "task_ended")
    started_payload = _mapping(task_started, "payload")
    ended_payload = _mapping(task_ended, "payload")
    task_name = _string(started_payload, "task_name")
    _require(
        task_name == task_entry.get("task_name"),
        "task_name_mismatch",
        "task_started task_name differs from curated manifest",
        task_name=task_name,
    )
    task_index = _positive_int(task_entry, "canonical_suite_index")
    task_key = f"{task_index:03d}:{task_name}:{_string(task_entry, 'source_task_run_id')}"
    adapter = _selected_adapter(started_payload, source_entry, task_key=task_key)

    step_events = sorted(
        (event for event in ordered if event["event_type"] == "step_started"),
        key=lambda event: _positive_int(_mapping(event, "payload"), "step_index"),
    )
    _require(step_events, "task_has_no_steps", "selected task must contain decision steps")
    step_indices = [
        _positive_int(_mapping(event, "payload"), "step_index") for event in step_events
    ]
    _require(
        step_indices == list(range(1, len(step_events) + 1)),
        "step_indices_invalid",
        "step indices must be contiguous from one",
        task_key=task_key,
    )

    events_by_step: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in ordered:
        payload = _mapping(event, "payload")
        step_id = payload.get("step_id")
        if isinstance(step_id, str):
            events_by_step[step_id].append(event)

    cores: list[dict[str, Any]] = []
    for step_started in step_events:
        step_payload = _mapping(step_started, "payload")
        step_id = _string(step_payload, "step_id")
        step_index = _positive_int(step_payload, "step_index")
        step_related = events_by_step[step_id]
        decision = _only_from(step_related, "agent_decision", step_index=step_index)
        transition = _only_transition(step_related, step_index=step_index)
        selected_response = _selected_response(decision, event_by_id, step_related)
        selected_request = _selected_request(selected_response, event_by_id, step_related)
        execution = _execution_event(transition, event_by_id, step_related)
        provider_content = _provider_content(selected_response)
        decision_payload = _mapping(decision, "payload")
        prediction = decision_payload.get("prediction_raw")
        comparison = _provider_decision_comparison(provider_content, prediction)
        parsed_action = decision_payload.get("parsed_action")
        action = parsed_action.get("value") if isinstance(parsed_action, Mapping) else None
        transition_payload = _mapping(transition, "payload")
        cores.append(
            {
                "step_index": step_index,
                "step_id": step_id,
                "step_started": step_started,
                "decision": decision,
                "selected_response": selected_response,
                "selected_request": selected_request,
                "step_model_requests": sorted(
                    (event for event in step_related if event.get("event_type") == "model_request"),
                    key=lambda event: event["seq"],
                ),
                "step_model_responses": sorted(
                    (
                        event
                        for event in step_related
                        if event.get("event_type") == "model_response"
                    ),
                    key=lambda event: event["seq"],
                ),
                "execution": execution,
                "transition": transition,
                "pre_observation": step_payload.get("observation"),
                "post_observation": transition_payload.get("post_observation"),
                "prediction": prediction,
                "provider_content": provider_content,
                "provider_decision_comparison": comparison,
                "parsed_action": parsed_action,
                "action": action,
            }
        )

    observation_digest_steps: defaultdict[str, list[int]] = defaultdict(list)
    for core in cores:
        digest = _observation_screenshot_digest(core["pre_observation"])
        if digest is not None:
            observation_digest_steps[digest].append(core["step_index"])

    all_candidates: list[dict[str, Any]] = []
    gelab_claim_text_by_step: dict[int, str] = {}
    gui_owl_claim_text_by_step: dict[int, str] = {}
    memgui_claim_text_by_entry: dict[str, tuple[int, str]] = {}
    memgui_rgb_fingerprint_cache: dict[str, dict[str, Any]] = {}
    memgui_image_presence_cache: dict[tuple[int, int], tuple[bool, bool]] = {}
    for position, core in enumerate(cores):
        request_payload = _mapping(core["selected_request"], "payload")
        exposures = _map_history_exposures(
            cores,
            position,
            request_payload,
            task_key,
            adapter=adapter,
            task_instruction=_string(started_payload, "task_goal"),
            blob_reader=blob_reader,
        )
        request_images = _request_image_records(request_payload, observation_digest_steps)
        request_image_digests = {
            digest
            for record in request_images
            if (digest := _request_image_digest(record["request_image"])) is not None
        }
        for exposure in exposures:
            source_core = cores[exposure["source_step_index"] - 1]
            if exposure.get("representation_type") == "rolling_summary":
                summary_text = exposure["assistant_summary_text"]
                previous_text = gelab_claim_text_by_step.get(source_core["step_index"])
                _require(
                    previous_text is None or previous_text == summary_text,
                    "gelab_summary_source_inconsistent",
                    "one GELab source decision mapped to inconsistent summary text",
                    task_key=task_key,
                    source_step=source_core["step_index"],
                )
                gelab_claim_text_by_step[source_core["step_index"]] = summary_text
            elif exposure.get("mapping_status") == "exact_gui_owl_collapsed_history_n1":
                conclusion_text = exposure["assistant_conclusion_text"]
                previous_text = gui_owl_claim_text_by_step.get(source_core["step_index"])
                _require(
                    previous_text is None or previous_text == conclusion_text,
                    "gui_owl_conclusion_source_inconsistent",
                    "one GUI-Owl source decision mapped to inconsistent Action conclusions",
                    task_key=task_key,
                    source_step=source_core["step_index"],
                )
                gui_owl_claim_text_by_step[source_core["step_index"]] = conclusion_text
            elif exposure.get("mapping_status") == "exact_memgui_structured_hlm":
                history_entry_id = _string(exposure, "history_entry_id")
                actor_claim_text = exposure.get("actor_claim_text")
                _require(
                    isinstance(actor_claim_text, str),
                    "memgui_actor_claim_invalid",
                    "every reconstructed MemGUI history entry must retain its actor text boundary",
                    task_key=task_key,
                    source_step=source_core["step_index"],
                    target_step=core["step_index"],
                    history_entry_id=history_entry_id,
                )
                previous = memgui_claim_text_by_entry.get(history_entry_id)
                current = (source_core["step_index"], actor_claim_text)
                _require(
                    previous is None or previous == current,
                    "memgui_history_entry_inconsistent",
                    "one MemGUI source entry mapped to inconsistent actor-authored text",
                    task_key=task_key,
                    history_entry_id=history_entry_id,
                )
                memgui_claim_text_by_entry[history_entry_id] = current
            source_digest = _observation_screenshot_digest(source_core["pre_observation"])
            post_digest = _observation_screenshot_digest(source_core["post_observation"])
            if adapter == _MEMGUI_ADAPTER:
                presence_key = (source_core["step_index"], core["step_index"])
                if presence_key not in memgui_image_presence_cache:
                    memgui_image_presence_cache[presence_key] = _memgui_rgb_image_presence(
                        source_core,
                        request_images,
                        task_key=task_key,
                        target_step=core["step_index"],
                        blob_reader=blob_reader,
                        fingerprint_cache=memgui_rgb_fingerprint_cache,
                    )
                source_present, post_present = memgui_image_presence_cache[presence_key]
            else:
                source_present = (
                    source_digest is not None and source_digest in request_image_digests
                )
                post_present = post_digest is not None and post_digest in request_image_digests
            exposure.update(
                {
                    "source_observation_image_present": source_present,
                    "post_observation_image_present": post_present,
                    "source_evidence_image_absent": not source_present and not post_present,
                }
            )
            if exposure["lag"] >= LONG_LAG_MINIMUM and exposure["source_evidence_image_absent"]:
                all_candidates.append(
                    _candidate_without_id(
                        task_index=task_index,
                        task_name=task_name,
                        signal="LONG_LAG_IMAGE_ABSENT",
                        source_step=exposure["source_step_index"],
                        target_step=core["step_index"],
                        evidence_step=core["step_index"],
                        details={
                            "lag": exposure["lag"],
                            "message_index": exposure["message_index"],
                            **(
                                {"history_entry_id": exposure["history_entry_id"]}
                                if exposure.get("mapping_status") == "exact_memgui_structured_hlm"
                                else {}
                            ),
                            "source_observation_digest": source_digest,
                            "post_observation_digest": post_digest,
                            "request_image_digests": sorted(request_image_digests),
                        },
                    )
                )
        core["assistant_exposures"] = exposures
        core["request_images"] = request_images
        core["request_ask_user_messages"] = _request_ask_user_messages(
            cores, position, request_payload, started_payload.get("task_goal")
        )

    signal_candidates = _scan_candidates(
        task_index,
        task_name,
        cores,
        textual_claims_by_step=(
            gelab_claim_text_by_step
            if adapter == _GELAB_ADAPTER
            else gui_owl_claim_text_by_step
            if adapter == _GUI_OWL_ADAPTER
            else _memgui_textual_claims_by_step(memgui_claim_text_by_entry)
            if adapter == _MEMGUI_ADAPTER
            else None
        ),
    )
    all_candidates.extend(signal_candidates)

    exposure_count = sum(len(core["assistant_exposures"]) for core in cores)

    source_id = _string(task_entry, "source_id")
    source_run_id = _string(task_entry, "source_run_id")
    source_task_run_id = _string(task_entry, "source_task_run_id")
    task_stream = _mapping(task_entry, "task_stream")
    provenance = {
        "source_id": source_id,
        "source_run_id": source_run_id,
        "source_task_run_id": source_task_run_id,
        "source_relative_run_path": source_entry.get("relative_run_path"),
        "task_stream_relative_path": task_stream.get("relative_path"),
        "task_stream_sha256": task_stream.get("sha256"),
    }
    outcome_sidecar = _formal_outcome_record(
        task_index=task_index,
        task_name=task_name,
        ended_payload=ended_payload,
    )
    reconstruction = {
        "schema_version": SCHEMA_VERSION,
        "task_key": task_key,
        "canonical_suite_index": task_index,
        "task_name": task_name,
        "task_instruction": started_payload.get("task_goal"),
        "provenance": provenance,
        "task_started_event_id": task_started["event_id"],
        "task_ended_event_id": task_ended["event_id"],
        "steps": [_reconstruction_step(core) for core in cores],
    }
    formal_candidates = _formal_candidates(
        task_key,
        cores,
        all_candidates,
        adapter=adapter,
    )
    claim_ids_by_step: defaultdict[int, list[str]] = defaultdict(list)
    for candidate in formal_candidates:
        for source_step in candidate["claim"]["source_steps"]:
            claim_ids_by_step[source_step].append(candidate["candidate_id"])
    trajectory_outline = [
        {
            "step": core["step_index"],
            "prediction_excerpt": _nullable_excerpt(core.get("prediction")),
            "parsed_action": _nullable_compact_json(core.get("action")),
            "ui_delta": _ui_delta(core),
            "history_claim_ids": sorted(set(claim_ids_by_step[core["step_index"]])),
        }
        for core in cores
    ]
    task_card = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "record_type": "task_card",
        "evaluation_run_id": evaluation_run_id,
        "dataset_sha256": dataset_sha256,
        "selection_sha256": selection_sha256,
        "task": {
            "catalog_index": task_index,
            "task_name": task_name,
            "task_run_id": source_task_run_id,
            "raw_run_id": source_run_id,
            "source_id": source_id,
            "source_relative_run_path": _string(source_entry, "relative_run_path"),
            "task_stream_relative_path": _string(task_stream, "relative_path"),
        },
        "outcome_blinded": True,
        "instruction": _clean_nonempty_text(_string(started_payload, "task_goal")),
        "coverage": {
            "integrity_valid": True,
            "capture_complete": bool(
                task_entry.get("capture_complete") is True
                and ended_payload.get("capture_complete") is True
            ),
            "decision_count": len(cores),
            "reconstructed_decision_count": len(cores),
            "history_bearing_decision_count": sum(
                bool(core["assistant_exposures"]) for core in cores
            ),
            "unique_history_claim_count": len(
                {
                    exposure.get("history_entry_id", exposure["source_decision_event_id"])
                    for core in cores
                    for exposure in core["assistant_exposures"]
                }
            ),
            "actual_exposure_count": exposure_count,
            "scanner_candidate_count": len(formal_candidates),
            "dropped_candidate_count": 0,
            "full_reconstruction_sha256": canonical_sha256(reconstruction),
        },
        "trajectory_outline": trajectory_outline,
        "candidates": formal_candidates,
    }
    return {
        "task_card": task_card,
        "outcome_sidecar": outcome_sidecar,
        "reconstruction": reconstruction,
    }


def _formal_outcome_record(
    *, task_index: int, task_name: str, ended_payload: Mapping[str, Any]
) -> dict[str, Any]:
    evaluation = ended_payload.get("environment_evaluation")
    score: int | float | None = None
    if isinstance(evaluation, Mapping):
        raw_score = evaluation.get("score")
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            score = raw_score
    if score is None:
        outcome = "NO_RESULT"
    elif score > 0.99:
        outcome = "SUCCESS"
    else:
        outcome = "FAILURE"
    return {
        "app": _task_definition_app(task_name),
        "catalog_index": task_index,
        "outcome": outcome,
        "score": score,
        "task_name": task_name,
    }


@lru_cache(maxsize=1)
def _task_definition_app_map() -> dict[str, str]:
    """Map task classes to the stable top-level task-definition family."""

    definitions = Path(__file__).parents[1] / "tasks" / "definitions"
    result: dict[str, str] = {}
    if not definitions.is_dir():
        return result
    for source in sorted(definitions.rglob("*.py")):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        app = source.relative_to(definitions).parts[0]
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not any(
                (isinstance(base, ast.Name) and base.id == "BaseTask")
                or (isinstance(base, ast.Attribute) and base.attr == "BaseTask")
                for base in node.bases
            ):
                continue
            previous = result.setdefault(node.name, app)
            if previous != app:
                raise MotivationCardError(
                    "task_app_ambiguous",
                    "task class appears in multiple top-level definition families",
                    task_name=node.name,
                    apps=sorted({previous, app}),
                )
    return result


def _task_definition_app(task_name: str) -> str:
    return _task_definition_app_map().get(task_name, "unclassified")


def _index_events(
    task_entry: Mapping[str, Any],
    source_entry: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    _require(events, "task_stream_empty", "task event stream must not be empty")
    ordered = sorted(events, key=lambda event: _positive_int(event, "seq"))
    _require(
        [event["seq"] for event in ordered] == list(range(1, len(ordered) + 1)),
        "task_stream_seq_invalid",
        "task stream seq must be contiguous from one",
    )
    expected_run_id = _string(task_entry, "source_run_id")
    expected_task_run_id = _string(task_entry, "source_task_run_id")
    _require(
        source_entry.get("run_id") == expected_run_id,
        "source_run_id_mismatch",
        "source and task run IDs disagree",
    )
    event_by_id: dict[str, Mapping[str, Any]] = {}
    for event in ordered:
        try:
            validate_event_envelope(event)
        except (SchemaValidationError, TypeError, ValueError) as error:
            raise MotivationCardError(
                "event_envelope_invalid", "task event has an invalid v1 envelope", error=str(error)
            ) from error
        _require(
            event.get("run_id") == expected_run_id
            and event.get("task_run_id") == expected_task_run_id
            and event.get("stream_id") == expected_task_run_id,
            "event_identity_mismatch",
            "event identity differs from curated task identity",
            seq=event.get("seq"),
        )
        event_id = _string(event, "event_id")
        _require(
            event_id not in event_by_id,
            "event_id_duplicate",
            "task stream repeats an event ID",
            event_id=event_id,
        )
        event_by_id[event_id] = event
    for event in ordered:
        parent_id = event.get("caused_by_event_id")
        if parent_id is None:
            continue
        parent = event_by_id.get(parent_id)
        _require(
            parent is not None and parent["seq"] < event["seq"],
            "causal_reference_invalid",
            "causal reference must name an earlier event in the same task",
            event_id=event["event_id"],
            caused_by_event_id=parent_id,
        )
    return ordered, event_by_id


def _only_event(events: Sequence[Mapping[str, Any]], event_type: str) -> Mapping[str, Any]:
    return _only_from(events, event_type)


def _only_from(
    events: Sequence[Mapping[str, Any]], event_type: str, *, step_index: int | None = None
) -> Mapping[str, Any]:
    matches = [event for event in events if event.get("event_type") == event_type]
    _require(
        len(matches) == 1,
        "event_cardinality_invalid",
        "expected exactly one event of the requested type",
        event_type=event_type,
        step_index=step_index,
        actual=len(matches),
    )
    return matches[0]


def _only_transition(events: Sequence[Mapping[str, Any]], *, step_index: int) -> Mapping[str, Any]:
    terminal_types = {"transition_completed", "transition_failed", "transition_not_executed"}
    matches = [event for event in events if event.get("event_type") in terminal_types]
    _require(
        len(matches) == 1,
        "transition_cardinality_invalid",
        "each decision step must have exactly one transition terminal",
        step_index=step_index,
        actual=len(matches),
    )
    return matches[0]


def _selected_response(
    decision: Mapping[str, Any],
    event_by_id: Mapping[str, Mapping[str, Any]],
    step_events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    parent = event_by_id.get(decision.get("caused_by_event_id"))
    if parent is not None and parent.get("event_type") == "model_response":
        return parent
    source_calls = _mapping(decision, "payload").get("source_model_call_ids")
    call_ids = set(source_calls) if isinstance(source_calls, list) else set()
    matches = [
        event
        for event in step_events
        if event.get("event_type") == "model_response"
        and _mapping(event, "payload").get("model_call_id") in call_ids
    ]
    _require(
        len(matches) == 1,
        "selected_model_response_ambiguous",
        "cannot identify the model response used by the MAI decision",
        decision_event_id=decision.get("event_id"),
        actual=len(matches),
    )
    return matches[0]


def _selected_request(
    response: Mapping[str, Any],
    event_by_id: Mapping[str, Mapping[str, Any]],
    step_events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    parent = event_by_id.get(response.get("caused_by_event_id"))
    if parent is not None and parent.get("event_type") == "model_request":
        return parent
    request_id = _mapping(response, "payload").get("request_id")
    matches = [
        event
        for event in step_events
        if event.get("event_type") == "model_request"
        and _mapping(event, "payload").get("request_id") == request_id
    ]
    _require(
        len(matches) == 1,
        "selected_model_request_ambiguous",
        "cannot identify the exact request associated with the selected response",
        response_event_id=response.get("event_id"),
        actual=len(matches),
    )
    return matches[0]


def _execution_event(
    transition: Mapping[str, Any],
    event_by_id: Mapping[str, Mapping[str, Any]],
    step_events: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    if transition.get("event_type") == "transition_not_executed":
        return None
    execution_id = _mapping(transition, "payload").get("action_execution_event_id")
    execution = event_by_id.get(execution_id)
    if execution is not None and execution.get("event_type") == "action_execution_started":
        return execution
    matches = [
        event for event in step_events if event.get("event_type") == "action_execution_started"
    ]
    _require(
        len(matches) == 1,
        "execution_event_ambiguous",
        "executed transition must link one action_execution_started event",
        transition_event_id=transition.get("event_id"),
        actual=len(matches),
    )
    return matches[0]


def _provider_content(response: Mapping[str, Any]) -> Any:
    normalized = _mapping(response, "payload").get("normalized_response")
    if not isinstance(normalized, Mapping):
        return None
    choices = normalized.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        return None
    return choices[0].get("content")


def _provider_decision_comparison(provider: Any, decision: Any) -> dict[str, Any]:
    if provider is None and decision is None:
        status = "both_missing"
    elif provider == decision:
        status = "exact_match"
    elif (
        isinstance(provider, str)
        and isinstance(decision, str)
        and provider.strip() == decision.strip()
    ):
        status = "edge_whitespace_only"
    elif provider is None:
        status = "provider_missing"
    elif decision is None:
        status = "decision_missing"
    else:
        status = "different"
    return {
        "status": status,
        "provider_content": _value_summary(provider),
        "decision_prediction": _value_summary(decision),
    }


def _selected_adapter(
    task_started_payload: Mapping[str, Any],
    source_entry: Mapping[str, Any],
    *,
    task_key: str,
) -> str | None:
    """Resolve adapter provenance without guessing from source names or prompt text."""

    task_agent = task_started_payload.get("agent")
    task_adapter = task_agent.get("adapter") if isinstance(task_agent, Mapping) else None
    provenance = source_entry.get("provenance")
    source_adapter = provenance.get("agent_type") if isinstance(provenance, Mapping) else None
    for value, field in (
        (task_adapter, "task_started.payload.agent.adapter"),
        (source_adapter, "curated.sources[].provenance.agent_type"),
    ):
        _require(
            value is None or (isinstance(value, str) and bool(value)),
            "adapter_provenance_invalid",
            "adapter provenance must be a non-empty string when present",
            task_key=task_key,
            field=field,
        )
    _require(
        task_adapter is None or source_adapter is None or task_adapter == source_adapter,
        "adapter_provenance_mismatch",
        "task and curated-source adapter provenance disagree",
        task_key=task_key,
        task_adapter=task_adapter,
        source_adapter=source_adapter,
    )
    return task_adapter or source_adapter


def _map_history_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    adapter: str | None,
    task_instruction: str,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> list[dict[str, Any]]:
    if adapter == _QWEN_ADAPTER:
        return _map_qwen_flat_progress_exposures(
            cores,
            target_position,
            request_payload,
            task_key,
            task_instruction=task_instruction,
        )
    if adapter == _GELAB_ADAPTER:
        return _map_gelab_rolling_summary_exposures(
            cores,
            target_position,
            request_payload,
            task_key,
            task_instruction=task_instruction,
        )
    if adapter == _GUI_OWL_ADAPTER:
        return _map_gui_owl_collapsed_history_exposures(
            cores,
            target_position,
            request_payload,
            task_key,
            task_instruction=task_instruction,
            blob_reader=blob_reader,
        )
    if adapter == _MEMGUI_ADAPTER:
        return _map_memgui_structured_folding_exposures(
            cores,
            target_position,
            request_payload,
            task_key,
            task_instruction=task_instruction,
            blob_reader=blob_reader,
        )
    if adapter == _UI_VENUS_ADAPTER:
        return _map_ui_venus_flat_previous_actions_exposures(
            cores,
            target_position,
            request_payload,
            task_key,
            task_instruction=task_instruction,
            blob_reader=blob_reader,
        )
    _require(
        adapter is None or adapter in _RAW_REPLAY_ADAPTERS,
        "history_adapter_unsupported",
        "motivation-card history reconstruction does not support this adapter",
        task_key=task_key,
        adapter=adapter,
    )
    return _map_assistant_exposures(cores, target_position, request_payload, task_key)


def _map_memgui_structured_folding_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    task_instruction: str,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> list[dict[str, Any]]:
    """Replay MemGUI's exact structured H/L/M request state.

    H records are folding summaries authored by the accepted response that
    installed them.  L retains the most recent accepted UI observation and
    action intent while keeping the runtime-derived action summary separate.
    M records retain the insertion-ordered memory state and the provenance of
    their latest mutation.  Destructive overlap replacement and memory
    add/update/delete semantics are replayed before the actual request bytes
    are accepted as exact.
    """

    target_step = target_position + 1
    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list) and len(messages) == 2,
        "memgui_request_messages_invalid",
        "MemGUI request must contain exactly its system and current user messages",
        task_key=task_key,
        target_step=target_step,
    )
    system_message, user_message = messages
    _require(
        isinstance(system_message, Mapping)
        and system_message.get("role") == "system"
        and _qwen_text_image_content_shape(system_message, expected_types=("text",))
        and system_message["content"][0].get("text") == MEMGUI_SYSTEM_PROMPT,
        "memgui_system_message_invalid",
        "MemGUI system message must exactly match the frozen runtime prompt",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        isinstance(user_message, Mapping)
        and user_message.get("role") == "user"
        and _qwen_text_image_content_shape(user_message, expected_types=("text", "image_url")),
        "memgui_user_message_invalid",
        "MemGUI user message must contain structured text then the current image",
        task_key=task_key,
        target_step=target_step,
    )
    user_content = user_message["content"]
    message_text = user_content[0].get("text")
    _require(
        isinstance(message_text, str),
        "memgui_user_prompt_invalid",
        "MemGUI H/L/M prompt content must be text",
        task_key=task_key,
        target_step=target_step,
    )
    _validate_memgui_current_image(
        cores,
        target_position,
        request_payload,
        user_content[1],
        task_key,
        blob_reader=blob_reader,
    )

    state = _replay_memgui_state(
        cores,
        stop_position=target_position,
        task_key=task_key,
    )
    summary_render = _render_memgui_summaries(state["summaries"])
    latest_render = _render_memgui_latest(state["latest"])
    memory_render = _render_memgui_memory(state["memory"])
    values = {
        "instruction": task_instruction,
        "state_summaries": summary_render["text"],
        "latest_interaction": latest_render["text"],
        "memory_state": memory_render["text"],
        "folding_instruction": (
            "Skip <folding> for the first step"
            if target_step == 1
            else "Output <folding> to compress your previous step(s)"
        ),
    }
    expected_prompt, field_offsets = _render_memgui_user_prompt(values)
    _require(
        message_text == expected_prompt,
        "memgui_structured_prompt_mismatch",
        "MemGUI request does not exactly match replayed H/L/M runtime state",
        task_key=task_key,
        target_step=target_step,
        actual_sha256=_span_sha256(message_text),
        expected_sha256=_span_sha256(expected_prompt),
    )

    exposures: list[dict[str, Any]] = []
    for section, rendered in (
        ("H", summary_render),
        ("L", latest_render),
        ("M", memory_render),
    ):
        section_field = {
            "H": "state_summaries",
            "L": "latest_interaction",
            "M": "memory_state",
        }[section]
        section_start, section_end = field_offsets[section_field]
        _require(
            message_text[section_start:section_end] == rendered["text"],
            "memgui_section_span_mismatch",
            "MemGUI structured section does not occupy its exact template span",
            task_key=task_key,
            target_step=target_step,
            history_section=section,
        )
        for record in rendered["records"]:
            source_core = record["source_core"]
            local_start = record["span_start"]
            local_end = record["span_end"]
            span_start = section_start + local_start
            span_end = section_start + local_end
            exposed_text = record["exposed_text"]
            _require(
                message_text[span_start:span_end] == exposed_text,
                "memgui_history_entry_span_mismatch",
                "MemGUI H/L/M history entry cannot be recovered at its exact request span",
                task_key=task_key,
                target_step=target_step,
                history_section=section,
                history_entry_id=record["history_entry_id"],
            )
            exposure = {
                "mapping_status": "exact_memgui_structured_hlm",
                "representation_type": "structured_folding",
                "history_section": section,
                "history_entry_id": record["history_entry_id"],
                "message_index": 1,
                "content_block_index": 0,
                "source_step_index": source_core["step_index"],
                "source_step_id": source_core["step_id"],
                "source_decision_event_id": source_core["decision"]["event_id"],
                "source_prediction_sha256": _value_summary(source_core.get("prediction"))["sha256"],
                "actor_claim_text": record["actor_claim_text"],
                "actor_claim_sha256": _span_sha256(record["actor_claim_text"]),
                "exposed_text": exposed_text,
                "exposed_text_sha256": _span_sha256(exposed_text),
                "span_start": span_start,
                "span_end": span_end,
                "target_step_index": target_step,
                "lag": target_step - source_core["step_index"],
                **record["metadata"],
            }
            exposures.append(exposure)
    return sorted(
        exposures,
        key=lambda exposure: (
            _MEMGUI_HISTORY_SECTION_ORDER[exposure["history_section"]],
            exposure["span_start"],
            exposure["history_entry_id"],
        ),
    )


def _render_memgui_user_prompt(values: Mapping[str, str]) -> tuple[str, dict[str, tuple[int, int]]]:
    """Render the frozen template while retaining exact placeholder spans."""

    _require(
        set(values) == set(_MEMGUI_TEMPLATE_FIELDS),
        "memgui_template_values_invalid",
        "MemGUI template renderer requires the exact frozen placeholder set",
    )
    cursor = 0
    output: list[str] = []
    output_length = 0
    offsets: dict[str, tuple[int, int]] = {}
    for field in _MEMGUI_TEMPLATE_FIELDS:
        marker = "{" + field + "}"
        _require(
            MEMGUI_USER_TEMPLATE.count(marker) == 1,
            "memgui_prompt_template_unsupported",
            "MemGUI runtime prompt placeholders changed; update the exact mapper",
            field=field,
        )
        marker_index = MEMGUI_USER_TEMPLATE.find(marker, cursor)
        _require(
            marker_index >= cursor,
            "memgui_prompt_template_order_invalid",
            "MemGUI runtime prompt placeholder order changed",
            field=field,
        )
        literal = MEMGUI_USER_TEMPLATE[cursor:marker_index]
        output.append(literal)
        output_length += len(literal)
        value = values[field]
        start = output_length
        output.append(value)
        output_length += len(value)
        offsets[field] = (start, output_length)
        cursor = marker_index + len(marker)
    output.append(MEMGUI_USER_TEMPLATE[cursor:])
    return "".join(output), offsets


def _render_memgui_summaries(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not summaries:
        return {"text": "  (no previous steps)", "records": []}
    output: list[str] = []
    records: list[dict[str, Any]] = []
    cursor = 0
    for index, summary in enumerate(summaries):
        if index:
            output.append("\n")
            cursor += 1
        text = _string(summary, "summary")
        exposed = f"  {text}"
        start = cursor
        output.append(exposed)
        cursor += len(exposed)
        records.append(
            {
                "source_core": summary["source_core"],
                "history_entry_id": summary["history_entry_id"],
                "actor_claim_text": text,
                "exposed_text": exposed,
                "span_start": start,
                "span_end": cursor,
                "metadata": {
                    "fold_range": list(summary["fold_range"]),
                    "fold_summary_text": text,
                    "fold_summary_sha256": _span_sha256(text),
                },
            }
        )
    return {"text": "".join(output), "records": records}


def _render_memgui_latest(latest: Mapping[str, Any] | None) -> dict[str, Any]:
    if latest is None:
        return {"text": "  (no previous interaction)", "records": []}
    lines = [f"  Step {latest['step']}:"]
    actor_lines: list[str] = []
    if latest["ui_observation"]:
        line = f"    UI Observation: {latest['ui_observation']}"
        lines.append(line)
        actor_lines.append(f"UI Observation: {latest['ui_observation']}")
    if latest["action_intent"]:
        line = f"    Action Intent: {latest['action_intent']}"
        lines.append(line)
        actor_lines.append(f"Action Intent: {latest['action_intent']}")
    if latest["action_summary"]:
        lines.append(f"    Action Taken: {latest['action_summary']}")
    text = "\n".join(lines)
    actor_claim = "\n".join(actor_lines)
    return {
        "text": text,
        "records": [
            {
                "source_core": latest["source_core"],
                "history_entry_id": latest["history_entry_id"],
                "actor_claim_text": actor_claim,
                "exposed_text": text,
                "span_start": 0,
                "span_end": len(text),
                "metadata": {
                    "latest_ui_observation_text": latest["ui_observation"],
                    "latest_action_intent_text": latest["action_intent"],
                    "runtime_derived_action_text": latest["action_summary"],
                },
            }
        ],
    }


def _render_memgui_memory(memory: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    if not memory:
        return {"text": "  (empty)", "records": []}
    output: list[str] = []
    records: list[dict[str, Any]] = []
    cursor = 0
    for index, (memory_id, item) in enumerate(memory.items()):
        if index:
            output.append("\n")
            cursor += 1
        exposed = (
            f"  [{memory_id}]\n"
            f"    Description: {item['description']}\n"
            f"    Content: {item['content']}"
        )
        actor_claim = (
            f"Memory [{memory_id}]\nDescription: {item['description']}\nContent: {item['content']}"
        )
        start = cursor
        output.append(exposed)
        cursor += len(exposed)
        records.append(
            {
                "source_core": item["source_core"],
                "history_entry_id": item["history_entry_id"],
                "actor_claim_text": actor_claim,
                "exposed_text": exposed,
                "span_start": start,
                "span_end": cursor,
                "metadata": {
                    "memory_id": memory_id,
                    "memory_description_text": item["description"],
                    "memory_content_text": item["content"],
                    "memory_description_source_step": item["description_source_step"],
                    "memory_description_source_decision_event_id": item[
                        "description_source_decision_event_id"
                    ],
                    "memory_content_source_step": item["content_source_step"],
                    "memory_content_source_decision_event_id": item[
                        "content_source_decision_event_id"
                    ],
                    "memory_last_operation": item["last_operation"],
                },
            }
        )
    return {"text": "".join(output), "records": records}


def _replay_memgui_state(
    cores: Sequence[Mapping[str, Any]],
    *,
    stop_position: int,
    task_key: str,
) -> dict[str, Any]:
    """Replay state committed by accepted MemGUI decisions before one request."""

    _require(
        isinstance(stop_position, int)
        and not isinstance(stop_position, bool)
        and 0 <= stop_position <= len(cores),
        "memgui_replay_boundary_invalid",
        "MemGUI replay boundary must address a valid request position",
        task_key=task_key,
        stop_position=stop_position,
        step_count=len(cores),
    )
    summaries: list[dict[str, Any]] = []
    latest: dict[str, Any] | None = None
    memory: dict[str, dict[str, Any]] = {}
    for position, core in enumerate(cores[:stop_position]):
        source_step = position + 1
        _require(
            core.get("step_index") == source_step,
            "memgui_replay_step_order_invalid",
            "MemGUI replay requires contiguous one-based accepted decisions",
            task_key=task_key,
            expected_step=source_step,
            actual_step=core.get("step_index"),
        )
        _validate_memgui_decision_provenance(core, task_key=task_key)
        parsed = _parse_memgui_accepted_prediction(
            core.get("prediction"),
            current_step=source_step,
            task_key=task_key,
        )
        action_summary = _validate_memgui_parsed_action(
            parsed,
            core,
            task_key=task_key,
        )

        folding = parsed["folding_directive"]
        # The runtime parses and validates a legal step-one directive but does
        # not commit it.  From step two onward it commits exactly one directive.
        if source_step > 1:
            _require(
                folding is not None,
                "memgui_folding_required",
                "accepted MemGUI decisions require folding from step two onward",
                task_key=task_key,
                source_step=source_step,
            )
            fold_range = tuple(folding["range"])
            summary_text = folding["summary"]
            summaries = [
                record
                for record in summaries
                if record["fold_range"][1] < fold_range[0]
                or record["fold_range"][0] > fold_range[1]
            ]
            summaries.append(
                {
                    "fold_range": fold_range,
                    "summary": summary_text,
                    "source_core": core,
                    "history_entry_id": _stable_id(
                        "history-entry",
                        {
                            "source_decision_event_id": core["decision"]["event_id"],
                            "history_section": "H",
                            "fold_range": list(fold_range),
                            "actor_claim_sha256": _span_sha256(summary_text),
                        },
                    ),
                }
            )
            summaries.sort(key=lambda record: record["fold_range"][0])

        memory_args = parsed["memory_args"]
        if memory_args is not None:
            operation = memory_args["operation"]
            memory_id = memory_args["memory_id"]
            exists = memory_id in memory
            _require(
                not (operation == "add" and exists),
                "memgui_memory_add_conflict",
                "accepted memory_add cannot overwrite an existing memory ID",
                task_key=task_key,
                source_step=source_step,
                memory_id=memory_id,
            )
            _require(
                not (operation in {"update", "delete"} and not exists),
                "memgui_memory_target_missing",
                "accepted memory_update/delete requires an existing memory ID",
                task_key=task_key,
                source_step=source_step,
                memory_id=memory_id,
                operation=operation,
            )
            if operation == "add":
                description = memory_args["description"]
                content = memory_args["content"]
                memory[memory_id] = {
                    "description": description,
                    "content": content,
                    "description_source_step": source_step,
                    "description_source_decision_event_id": core["decision"]["event_id"],
                    "content_source_step": source_step,
                    "content_source_decision_event_id": core["decision"]["event_id"],
                    "source_core": core,
                    "last_operation": operation,
                    "history_entry_id": _memgui_memory_entry_id(
                        core,
                        memory_id=memory_id,
                        operation=operation,
                        description=description,
                        content=content,
                    ),
                }
                preview = content[:50] + "..." if len(content) > 50 else content
                action_summary = f"Memory: Added memory [{memory_id}]: {description} | {preview}"
            elif operation == "update":
                old = memory[memory_id]
                description = memory_args["description"] or old["description"]
                content = memory_args["content"]
                # Reassigning an existing dict key preserves runtime insertion
                # order while giving the version the current mutation source.
                memory[memory_id] = {
                    "description": description,
                    "content": content,
                    "description_source_step": (
                        source_step
                        if memory_args["description"]
                        else old["description_source_step"]
                    ),
                    "description_source_decision_event_id": (
                        core["decision"]["event_id"]
                        if memory_args["description"]
                        else old["description_source_decision_event_id"]
                    ),
                    "content_source_step": source_step,
                    "content_source_decision_event_id": core["decision"]["event_id"],
                    "source_core": core,
                    "last_operation": operation,
                    "history_entry_id": _memgui_memory_entry_id(
                        core,
                        memory_id=memory_id,
                        operation=operation,
                        description=description,
                        content=content,
                    ),
                }
                action_summary = f"Memory: Updated memory [{memory_id}]"
            else:
                memory.pop(memory_id)
                action_summary = f"Memory: Deleted memory [{memory_id}]"

        latest = {
            "step": source_step,
            "ui_observation": parsed["ui_observation"],
            "action_intent": parsed["action_intent"],
            "action_summary": action_summary,
            "source_core": core,
            "history_entry_id": _stable_id(
                "history-entry",
                {
                    "source_decision_event_id": core["decision"]["event_id"],
                    "history_section": "L",
                },
            ),
        }

    return {"summaries": summaries, "latest": latest, "memory": memory}


def _validate_memgui_decision_provenance(core: Mapping[str, Any], *, task_key: str) -> None:
    """Require the exact accepted request/response/decision chain used by MemGUI."""

    source_step = core["step_index"]
    request = core["selected_request"]
    response = core["selected_response"]
    decision = core["decision"]
    request_payload = _mapping(request, "payload")
    response_payload = _mapping(response, "payload")
    decision_payload = _mapping(decision, "payload")
    model_call_id = request_payload.get("model_call_id")
    source_model_call_ids = decision_payload.get("source_model_call_ids")
    _require(
        request.get("event_type") == "model_request"
        and response.get("event_type") == "model_response"
        and decision.get("event_type") == "agent_decision"
        and response.get("caused_by_event_id") == request.get("event_id")
        and decision.get("caused_by_event_id") == response.get("event_id")
        and isinstance(model_call_id, str)
        and bool(model_call_id)
        and response_payload.get("model_call_id") == model_call_id
        and response_payload.get("request_id") == request_payload.get("request_id")
        and isinstance(source_model_call_ids, list)
        and bool(source_model_call_ids)
        and all(isinstance(value, str) and bool(value) for value in source_model_call_ids)
        and len(set(source_model_call_ids)) == len(source_model_call_ids)
        and source_model_call_ids[-1] == model_call_id,
        "memgui_decision_provenance_mismatch",
        "MemGUI history source must have one exact accepted request-response-decision chain",
        task_key=task_key,
        source_step=source_step,
    )
    _require(
        all(
            payload.get("step_id") == core["step_id"]
            for payload in (request_payload, response_payload, decision_payload)
        ),
        "memgui_decision_step_provenance_mismatch",
        "MemGUI accepted request-response-decision events must belong to the same step",
        task_key=task_key,
        source_step=source_step,
    )
    _require(
        request_payload.get("component") == "mobile_world.agents.implementations.memgui_agent"
        and request_payload.get("call_role") == "actor",
        "memgui_request_provenance_invalid",
        "MemGUI exact replay requires the frozen actor adapter component",
        task_key=task_key,
        source_step=source_step,
        component=request_payload.get("component"),
        call_role=request_payload.get("call_role"),
    )
    _require(
        decision_payload.get("parse_outcome") == "returned"
        and decision_payload.get("parse_exception") is None,
        "memgui_decision_not_accepted",
        "only accepted MemGUI decisions may mutate reconstructed H/L/M state",
        task_key=task_key,
        source_step=source_step,
        parse_outcome=decision_payload.get("parse_outcome"),
    )
    step_requests = core.get("step_model_requests")
    step_responses = core.get("step_model_responses")
    _require(
        isinstance(step_requests, list)
        and isinstance(step_responses, list)
        and all(isinstance(event, Mapping) for event in (*step_requests, *step_responses)),
        "memgui_retry_provenance_missing",
        "MemGUI exact replay requires all step-local model attempts",
        task_key=task_key,
        source_step=source_step,
    )
    selected_request_view = request_payload.get("request_view")
    selected_request_images = request_payload.get("request_images")
    for source_model_call_id in source_model_call_ids:
        call_requests = [
            event
            for event in step_requests
            if _mapping(event, "payload").get("model_call_id") == source_model_call_id
        ]
        call_responses = [
            event
            for event in step_responses
            if _mapping(event, "payload").get("model_call_id") == source_model_call_id
        ]
        _require(
            bool(call_requests) and len(call_responses) == 1,
            "memgui_retry_call_provenance_ambiguous",
            "each MemGUI outer attempt must have requests and one returned response",
            task_key=task_key,
            source_step=source_step,
            model_call_id=source_model_call_id,
            request_count=len(call_requests),
            response_count=len(call_responses),
        )
        call_response = call_responses[0]
        _require(
            any(
                request_event.get("event_id") == call_response.get("caused_by_event_id")
                for request_event in call_requests
            ),
            "memgui_retry_response_causality_invalid",
            "each MemGUI outer response must link one request for the same logical call",
            task_key=task_key,
            source_step=source_step,
            model_call_id=source_model_call_id,
        )
        for request_event in call_requests:
            attempt_payload = _mapping(request_event, "payload")
            _require(
                attempt_payload.get("component")
                == "mobile_world.agents.implementations.memgui_agent"
                and attempt_payload.get("call_role") == "actor"
                and attempt_payload.get("request_view") == selected_request_view
                and attempt_payload.get("request_images") == selected_request_images,
                "memgui_retry_request_mismatch",
                "MemGUI outer retries must reuse the exact structured request and image",
                task_key=task_key,
                source_step=source_step,
                model_call_id=source_model_call_id,
                request_event_id=request_event.get("event_id"),
            )
    _require(
        core.get("provider_content") == core.get("prediction")
        and isinstance(core.get("prediction"), str),
        "memgui_provider_decision_mismatch",
        "MemGUI history source must equal the selected provider response byte for byte",
        task_key=task_key,
        source_step=source_step,
        comparison=core.get("provider_decision_comparison", {}).get("status"),
    )


def _parse_memgui_accepted_prediction(
    prediction: Any,
    *,
    current_step: int,
    task_key: str,
) -> dict[str, Any]:
    """Mirror the frozen runtime parser for fields that can enter H/L/M."""

    _require(
        isinstance(prediction, str),
        "memgui_prediction_invalid",
        "accepted MemGUI prediction must be text",
        task_key=task_key,
        source_step=current_step,
    )
    tag_matches = {
        tag: re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            prediction,
            re.DOTALL | re.IGNORECASE,
        )
        for tag in ("thinking", "tool_call", "ui_observation", "action_intent")
    }
    missing = [tag for tag, match in tag_matches.items() if match is None]
    _require(
        not missing,
        "memgui_prediction_tags_incomplete",
        "accepted MemGUI prediction must contain every complete required tag",
        task_key=task_key,
        source_step=current_step,
        missing_tags=missing,
    )

    folding_match = re.search(
        r"<folding>\s*(.*?)\s*</folding>",
        prediction,
        re.DOTALL | re.IGNORECASE,
    )
    folding = None
    if folding_match is not None:
        folding = _memgui_json_object(
            folding_match.group(1).strip(),
            code="memgui_folding_json_invalid",
            message="accepted MemGUI folding directive must be a JSON object",
            task_key=task_key,
            source_step=current_step,
        )
        fold_range = folding.get("range")
        summary = folding.get("summary")
        _require(
            isinstance(fold_range, list)
            and len(fold_range) == 2
            and all(isinstance(value, int) and not isinstance(value, bool) for value in fold_range)
            and isinstance(summary, str)
            and bool(summary.strip()),
            "memgui_folding_shape_invalid",
            "accepted MemGUI folding needs an integer pair and non-empty summary",
            task_key=task_key,
            source_step=current_step,
        )
        start_step, end_step = fold_range
        _require(
            1 <= start_step <= end_step <= current_step,
            "memgui_folding_range_invalid",
            "accepted MemGUI folding range falls outside runtime bounds",
            task_key=task_key,
            source_step=current_step,
            fold_range=fold_range,
        )
        folding = {**folding, "range": list(fold_range), "summary": summary.strip()}
    _require(
        current_step == 1 or folding is not None,
        "memgui_folding_required",
        "accepted MemGUI prediction requires folding from step two onward",
        task_key=task_key,
        source_step=current_step,
    )

    tool_match = tag_matches["tool_call"]
    assert tool_match is not None
    tool_call = _memgui_json_object(
        tool_match.group(1).strip(),
        code="memgui_tool_call_json_invalid",
        message="accepted MemGUI tool_call must be a JSON object",
        task_key=task_key,
        source_step=current_step,
    )
    action_name = tool_call.get("name")
    arguments = tool_call.get("arguments")
    _require(
        isinstance(action_name, str)
        and bool(action_name.strip())
        and isinstance(arguments, Mapping),
        "memgui_tool_call_shape_invalid",
        "accepted MemGUI tool_call needs a non-empty name and object arguments",
        task_key=task_key,
        source_step=current_step,
    )
    _require(
        action_name.strip() == "mobile_use",
        "memgui_tool_name_invalid",
        "accepted MemGUI tool_call must target mobile_use",
        task_key=task_key,
        source_step=current_step,
        action_name=action_name.strip(),
    )
    action_args = dict(arguments)
    action_type = action_args.get("action")
    _require(
        isinstance(action_type, str)
        and bool(action_type.strip())
        and action_type.strip() in _MEMGUI_ACTION_TYPES,
        "memgui_action_type_invalid",
        "accepted MemGUI tool action must use the frozen action vocabulary",
        task_key=task_key,
        source_step=current_step,
        action_type=action_type,
    )
    action_type = action_type.strip()
    action_args["action"] = action_type
    for coordinate_field in ("coordinate", "coordinate2"):
        if coordinate_field in action_args:
            _validate_memgui_coordinate(
                action_args[coordinate_field],
                field_name=coordinate_field,
                task_key=task_key,
                source_step=current_step,
            )

    memory_args = None
    if action_type in _MEMGUI_MEMORY_ACTIONS:
        memory_id = action_args.get("memory_id")
        description = action_args.get("description", "")
        content = action_args.get("content")
        _require(
            isinstance(memory_id, str)
            and bool(memory_id)
            and isinstance(description, str)
            and (action_type == "memory_delete" or (isinstance(content, str) and bool(content))),
            "memgui_memory_arguments_invalid",
            "accepted MemGUI memory action has invalid ID, description, or content",
            task_key=task_key,
            source_step=current_step,
            action_type=action_type,
        )
        memory_args = {
            "operation": action_type.removeprefix("memory_"),
            "memory_id": memory_id,
            "description": description,
            "content": content,
        }

    return {
        "folding_directive": folding,
        "action_type": action_type,
        "action_args": action_args,
        "memory_args": memory_args,
        "ui_observation": tag_matches["ui_observation"].group(1).strip(),
        "action_intent": tag_matches["action_intent"].group(1).strip(),
    }


def _memgui_json_object(
    text: str,
    *,
    code: str,
    message: str,
    task_key: str,
    source_step: int,
) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as error:
        raise MotivationCardError(
            code,
            message,
            task_key=task_key,
            source_step=source_step,
        ) from error
    _require(
        isinstance(value, dict),
        code,
        message,
        task_key=task_key,
        source_step=source_step,
    )
    return value


def _validate_memgui_coordinate(
    value: Any,
    *,
    field_name: str,
    task_key: str,
    source_step: int,
) -> None:
    _require(
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        ),
        "memgui_coordinate_invalid",
        "accepted MemGUI coordinate must contain two finite numbers",
        task_key=task_key,
        source_step=source_step,
        field_name=field_name,
    )


def _validate_memgui_parsed_action(
    parsed: Mapping[str, Any],
    core: Mapping[str, Any],
    *,
    task_key: str,
) -> str:
    """Validate the archived JSONAction type and return runtime L text."""

    source_step = core["step_index"]
    action_type = parsed["action_type"]
    action_args = parsed["action_args"]
    if action_type in _MEMGUI_MEMORY_ACTIONS:
        expected = "wait"
    elif action_type == "system_button":
        button = action_args.get("button")
        _require(
            isinstance(button, str) and button.lower() in {"back", "home", "menu", "enter"},
            "memgui_system_button_invalid",
            "accepted MemGUI system_button must use the frozen button vocabulary",
            task_key=task_key,
            source_step=source_step,
            button=button,
        )
        expected = _MEMGUI_SYSTEM_BUTTON_ACTION_MAP.get(button.lower())
        _require(
            expected is not None,
            "memgui_system_button_unexecutable",
            "MemGUI runtime cannot accept an unmapped system button",
            task_key=task_key,
            source_step=source_step,
            button=button,
        )
    else:
        expected = _MEMGUI_NON_MEMORY_ACTION_MAP[action_type]
    if action_type in {"click", "long_press"}:
        _require(
            "coordinate" in action_args,
            "memgui_action_coordinate_missing",
            "accepted MemGUI point action requires coordinate",
            task_key=task_key,
            source_step=source_step,
            action_type=action_type,
        )
    if action_type == "swipe":
        _require(
            "coordinate" in action_args and "coordinate2" in action_args,
            "memgui_swipe_coordinates_missing",
            "accepted MemGUI swipe requires both coordinate pairs",
            task_key=task_key,
            source_step=source_step,
        )
    action = core.get("action")
    _require(
        isinstance(action, Mapping) and action.get("action_type") == expected,
        "memgui_parsed_action_mismatch",
        "archived MemGUI JSONAction must match the accepted tool_call action",
        task_key=task_key,
        source_step=source_step,
        tool_action_type=action_type,
        expected_action_type=expected,
        actual_action_type=(action.get("action_type") if isinstance(action, Mapping) else None),
    )
    return expected


def _memgui_memory_entry_id(
    core: Mapping[str, Any],
    *,
    memory_id: str,
    operation: str,
    description: str,
    content: str,
) -> str:
    return _stable_id(
        "history-entry",
        {
            "source_decision_event_id": core["decision"]["event_id"],
            "history_section": "M",
            "memory_id": memory_id,
            "operation": operation,
            "description_sha256": _span_sha256(description),
            "content_sha256": _span_sha256(content),
        },
    )


def _memgui_textual_claims_by_step(
    claims_by_entry: Mapping[str, tuple[int, str]],
) -> dict[int, str]:
    """Group non-empty actor-authored entry text for the shared text scanner."""

    claims: defaultdict[int, list[str]] = defaultdict(list)
    for entry_id, (source_step, actor_text) in sorted(claims_by_entry.items()):
        if actor_text:
            claims[source_step].append(actor_text)
    return {source_step: "\n".join(texts) for source_step, texts in sorted(claims.items())}


def _validate_memgui_current_image(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    image_part: Any,
    task_key: str,
    *,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> None:
    """Prove the sole MemGUI request image is the current observation in RGB."""

    target_step = target_position + 1
    image_url = image_part.get("image_url") if isinstance(image_part, Mapping) else None
    externalized_url = image_url.get("url") if isinstance(image_url, Mapping) else None
    externalized_image = (
        externalized_url.get("$externalized_data_url")
        if isinstance(externalized_url, Mapping)
        else None
    )
    expected_externalized_keys = {
        "base64_alphabet",
        "content_blob",
        "content_path",
        "media_type",
        "original_text_blob",
    }
    _require(
        isinstance(externalized_image, Mapping)
        and set(externalized_image) == expected_externalized_keys,
        "memgui_request_image_view_invalid",
        "MemGUI request-view image must retain authoritative data-URL provenance",
        task_key=task_key,
        target_step=target_step,
    )
    request_images = request_payload.get("request_images")
    _require(
        isinstance(request_images, list) and len(request_images) == 1,
        "memgui_request_image_count_invalid",
        "formal MemGUI requests must send exactly one current screenshot",
        task_key=task_key,
        target_step=target_step,
        actual=(len(request_images) if isinstance(request_images, list) else None),
    )
    request_image = request_images[0]
    expected_request_image_keys = {
        "canonical_base64",
        "capture_status",
        "content_blob",
        "content_path",
        "height",
        "media_type",
        "original_text_blob",
        "width",
    }
    _require(
        isinstance(request_image, Mapping) and set(request_image) == expected_request_image_keys,
        "memgui_request_image_record_invalid",
        "MemGUI captured request-image record has an unsupported provenance shape",
        task_key=task_key,
        target_step=target_step,
    )
    expected_content_path = "messages[1].content[1].image_url.url"
    _require(
        request_image.get("content_path") == expected_content_path,
        "memgui_request_image_path_invalid",
        "MemGUI request image must be the current user-message image",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("capture_status") == "captured"
        and request_image.get("canonical_base64") is True
        and externalized_image.get("base64_alphabet") == "standard",
        "memgui_request_image_capture_invalid",
        "MemGUI request image must be captured canonical standard-base64 data",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        all(
            externalized_image.get(field) == request_image.get(field)
            for field in ("content_blob", "original_text_blob", "content_path", "media_type")
        )
        and externalized_image.get("content_path") == expected_content_path,
        "memgui_request_image_provenance_mismatch",
        "MemGUI request-view image and captured image record must agree exactly",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        blob_reader is not None,
        "memgui_blob_reader_required",
        "MemGUI current-image proof requires strict source blob access",
        task_key=task_key,
        target_step=target_step,
    )
    observation = cores[target_position].get("pre_observation")
    screenshot = observation.get("screenshot") if isinstance(observation, Mapping) else None
    observation_blob = screenshot.get("pixel_blob") if isinstance(screenshot, Mapping) else None
    request_blob = request_image.get("content_blob")
    _require(
        isinstance(observation_blob, Mapping) and isinstance(request_blob, Mapping),
        "memgui_current_image_blob_missing",
        "MemGUI current observation and request image must both have captured blobs",
        task_key=task_key,
        target_step=target_step,
    )
    observation_pixels = _decoded_rgb_fingerprint(
        blob_reader(observation_blob),
        code="memgui_observation_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    request_pixels = _decoded_rgb_fingerprint(
        blob_reader(request_blob),
        code="memgui_request_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("width") == request_pixels["width"]
        and request_image.get("height") == request_pixels["height"],
        "memgui_request_image_dimensions_mismatch",
        "MemGUI request-image dimensions must match its decoded pixel matrix",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_pixels == observation_pixels,
        "memgui_current_image_mismatch",
        "MemGUI request image must equal the current observation's RGB pixel matrix",
        task_key=task_key,
        target_step=target_step,
    )


def _memgui_rgb_image_presence(
    source_core: Mapping[str, Any],
    request_image_records: Sequence[Mapping[str, Any]],
    *,
    task_key: str,
    target_step: int,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
    fingerprint_cache: dict[str, dict[str, Any]],
) -> tuple[bool, bool]:
    """Compare evidence images by decoded RGB, independent of PNG encoding."""

    _require(
        blob_reader is not None and len(request_image_records) == 1,
        "memgui_image_presence_inputs_invalid",
        "MemGUI RGB evidence-presence checks require one request image and blob access",
        task_key=task_key,
        target_step=target_step,
    )
    request_image = request_image_records[0].get("request_image")
    request_blob = request_image.get("content_blob") if isinstance(request_image, Mapping) else None
    _require(
        isinstance(request_blob, Mapping),
        "memgui_image_presence_request_blob_missing",
        "MemGUI RGB evidence-presence check requires the captured request blob",
        task_key=task_key,
        target_step=target_step,
    )
    request_fingerprint = _memgui_cached_rgb_fingerprint(
        request_blob,
        task_key=task_key,
        target_step=target_step,
        blob_reader=blob_reader,
        fingerprint_cache=fingerprint_cache,
    )

    def observation_fingerprint(observation: Any) -> dict[str, Any] | None:
        screenshot = observation.get("screenshot") if isinstance(observation, Mapping) else None
        pixel_blob = screenshot.get("pixel_blob") if isinstance(screenshot, Mapping) else None
        if not isinstance(pixel_blob, Mapping):
            return None
        return _memgui_cached_rgb_fingerprint(
            pixel_blob,
            task_key=task_key,
            target_step=target_step,
            blob_reader=blob_reader,
            fingerprint_cache=fingerprint_cache,
        )

    source_fingerprint = observation_fingerprint(source_core.get("pre_observation"))
    post_fingerprint = observation_fingerprint(source_core.get("post_observation"))
    return (
        source_fingerprint == request_fingerprint,
        post_fingerprint == request_fingerprint,
    )


def _memgui_cached_rgb_fingerprint(
    blob: Mapping[str, Any],
    *,
    task_key: str,
    target_step: int,
    blob_reader: Callable[[Mapping[str, Any]], bytes],
    fingerprint_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    digest = _blob_digest(blob)
    _require(
        digest is not None,
        "memgui_image_presence_blob_digest_missing",
        "MemGUI evidence image blob must retain its digest",
        task_key=task_key,
        target_step=target_step,
    )
    if digest not in fingerprint_cache:
        fingerprint_cache[digest] = _decoded_rgb_fingerprint(
            blob_reader(blob),
            code="memgui_evidence_image_decode_failed",
            task_key=task_key,
            target_step=target_step,
        )
    return fingerprint_cache[digest]


def _map_gui_owl_collapsed_history_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    task_instruction: str,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> list[dict[str, Any]]:
    """Replay the formal GUI-Owl ``history_n=1`` collapsed text exactly.

    Every accepted earlier Action conclusion is retained.  Action ``i`` gets
    the tool/ask result delivered by observation ``i + 1``; this is the
    alignment used by the repaired runtime adapter.  The mapper accepts no raw
    assistant/user replay window and proves that the sole request image is the
    current observation by decoded RGB pixels.
    """

    target_step = target_position + 1
    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list) and len(messages) == 2,
        "gui_owl_request_messages_invalid",
        "formal GUI-Owl history_n=1 requests must contain only system and current user",
        task_key=task_key,
        target_step=target_step,
    )
    system_message, user_message = messages
    _require(
        isinstance(system_message, Mapping)
        and system_message.get("role") == "system"
        and isinstance(system_message.get("content"), str),
        "gui_owl_system_message_invalid",
        "GUI-Owl system message must be one text string",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        isinstance(user_message, Mapping)
        and user_message.get("role") == "user"
        and _qwen_text_image_content_shape(user_message, expected_types=("text", "image_url")),
        "gui_owl_user_message_invalid",
        "formal GUI-Owl user message must contain history text then the current image",
        task_key=task_key,
        target_step=target_step,
    )
    user_content = user_message["content"]
    message_text = user_content[0].get("text")
    _require(
        isinstance(message_text, str),
        "gui_owl_user_prompt_invalid",
        "GUI-Owl instruction/history content must be text",
        task_key=task_key,
        target_step=target_step,
    )
    _validate_gui_owl_current_image(
        cores,
        target_position,
        request_payload,
        user_content[1],
        task_key,
        blob_reader=blob_reader,
    )

    if target_position == 0:
        expected = GUI_OWL_1_5_USER_PROMPT_TEMPLATE.format(instruction=task_instruction)
        _require(
            message_text == expected,
            "gui_owl_first_prompt_mismatch",
            "GUI-Owl first request must use the no-history runtime template exactly",
            task_key=task_key,
            target_step=target_step,
            actual_sha256=_span_sha256(message_text),
            expected_sha256=_span_sha256(expected),
        )
        return []

    template_prefix = GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE.format(
        instruction=task_instruction,
        previous_steps="",
    )
    _require(
        message_text.startswith(template_prefix),
        "gui_owl_history_prompt_template_mismatch",
        "GUI-Owl history request does not start at the exact runtime template boundary",
        task_key=task_key,
        target_step=target_step,
        text_sha256=_span_sha256(message_text),
    )
    history_text = message_text[len(template_prefix) :]
    _require(
        bool(history_text),
        "gui_owl_collapsed_history_empty",
        "GUI-Owl request after step one must contain collapsed prior actions",
        task_key=task_key,
        target_step=target_step,
    )

    records: list[dict[str, Any]] = []
    for source_position, source_core in enumerate(cores[:target_position]):
        source_step = source_core["step_index"]
        _require(
            source_step == source_position + 1 and isinstance(source_core.get("action"), Mapping),
            "gui_owl_accepted_history_invalid",
            "every collapsed GUI-Owl entry must come from a prior accepted action turn",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
        )
        comparison_status = source_core["provider_decision_comparison"]["status"]
        _require(
            comparison_status in {"exact_match", "edge_whitespace_only"},
            "gui_owl_accepted_response_unresolved",
            "GUI-Owl collapsed history source must match its selected provider response",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
            comparison_status=comparison_status,
        )
        prediction_record = _gui_owl_prediction_record(
            source_core.get("prediction"),
            task_key=task_key,
            source_step=source_step,
        )
        conclusion = prediction_record["conclusion"]
        action, action_copy_status = _gui_owl_source_action_evidence(
            source_core,
            task_key=task_key,
            source_step=source_step,
        )
        action_alignment = _gui_owl_action_record_alignment(conclusion, action)
        rendered_conclusion = _gui_owl_add_period_robustly(conclusion)
        result_observation = cores[source_position + 1].get("pre_observation")
        records.append(
            {
                "source_core": source_core,
                "conclusion": conclusion,
                "rendered_conclusion": rendered_conclusion,
                "result_observation": result_observation,
                "provider_tool_name": prediction_record["tool_call"]["name"],
                "provider_tool_action": prediction_record["tool_call"]["arguments"].get("action"),
                "action_copy_status": action_copy_status,
                "action_alignment": action_alignment,
                "line_prefix": (f"Step{source_step}: {rendered_conclusion} Tool response: "),
            }
        )

    exposures: list[dict[str, Any]] = []
    cursor = 0
    absolute_history_start = len(template_prefix)
    for index, record in enumerate(records):
        source_core = record["source_core"]
        source_step = source_core["step_index"]
        line_prefix = record["line_prefix"]
        _require(
            history_text.startswith(line_prefix, cursor),
            "gui_owl_collapsed_line_prefix_mismatch",
            "GUI-Owl collapsed Step line does not match its accepted Action conclusion",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
        )
        result_start = cursor + len(line_prefix)
        if index + 1 < len(records):
            next_anchor = "\n" + records[index + 1]["line_prefix"]
            boundaries = _all_substring_positions(history_text, next_anchor, start=result_start)
            _require(
                len(boundaries) == 1,
                "gui_owl_collapsed_step_boundary_ambiguous",
                "GUI-Owl collapsed history must have one unambiguous next Step boundary",
                task_key=task_key,
                source_step=source_step,
                target_step=target_step,
                actual=len(boundaries),
            )
            result_end = boundaries[0]
            next_cursor = result_end + 1
        else:
            result_end = len(history_text)
            next_cursor = result_end
        result_text = history_text[result_start:result_end]
        result_record = _validate_gui_owl_aligned_result(
            record["result_observation"],
            result_text,
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
        )

        step_span_start = absolute_history_start + cursor
        step_span_end = absolute_history_start + result_end
        step_label = f"Step{source_step}: "
        span_start = step_span_start + len(step_label)
        span_end = step_span_end
        rendered_conclusion = record["rendered_conclusion"]
        exposed_text = f"{rendered_conclusion} Tool response: {result_text}"
        _require(
            message_text[step_span_start:step_span_end] == f"{step_label}{exposed_text}"
            and message_text[span_start:span_end] == exposed_text,
            "gui_owl_collapsed_span_mismatch",
            "GUI-Owl collapsed history cannot be recovered at its exact request span",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
        )
        assistant_span_end = span_start + len(rendered_conclusion)
        result_span_start = absolute_history_start + result_start
        result_span_end = absolute_history_start + result_end
        exposures.append(
            {
                "mapping_status": "exact_gui_owl_collapsed_history_n1",
                "representation_type": "hybrid_folding",
                "message_index": 1,
                "content_block_index": 0,
                "source_step_index": source_step,
                "source_step_id": source_core["step_id"],
                "source_decision_event_id": source_core["decision"]["event_id"],
                "source_prediction_sha256": _value_summary(source_core.get("prediction"))["sha256"],
                "assistant_conclusion_text": record["conclusion"],
                "assistant_conclusion_sha256": _span_sha256(record["conclusion"]),
                "rendered_conclusion_text": rendered_conclusion,
                "rendered_conclusion_sha256": _span_sha256(rendered_conclusion),
                "provider_tool_name": record["provider_tool_name"],
                "provider_tool_action": record["provider_tool_action"],
                "source_action_copy_status": record["action_copy_status"],
                "action_record_alignment": record["action_alignment"],
                "aligned_result_kind": result_record["kind"],
                "aligned_result_observation_step": source_step + 1,
                "aligned_result_value_sha256": result_record["value_sha256"],
                "aligned_result_text": result_text,
                "aligned_result_text_sha256": _span_sha256(result_text),
                "external_evidence_suffix": f" Tool response: {result_text}",
                "external_evidence_suffix_sha256": _span_sha256(f" Tool response: {result_text}"),
                "exposed_text": exposed_text,
                "exposed_text_sha256": _span_sha256(exposed_text),
                "span_start": span_start,
                "span_end": span_end,
                "assistant_span_start": span_start,
                "assistant_span_end": assistant_span_end,
                "external_span_start": assistant_span_end,
                "external_span_end": span_end,
                "result_span_start": result_span_start,
                "result_span_end": result_span_end,
                "step_span_start": step_span_start,
                "step_span_end": step_span_end,
                "step_span_sha256": _span_sha256(message_text[step_span_start:step_span_end]),
                "target_step_index": target_step,
                "lag": target_step - source_step,
            }
        )
        cursor = next_cursor
    _require(
        cursor == len(history_text),
        "gui_owl_collapsed_history_boundary_mismatch",
        "GUI-Owl collapsed history has unmatched trailing text",
        task_key=task_key,
        target_step=target_step,
    )
    return exposures


def _validate_gui_owl_current_image(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    image_part: Any,
    task_key: str,
    *,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> None:
    target_step = target_position + 1
    image_url = image_part.get("image_url") if isinstance(image_part, Mapping) else None
    externalized_url = image_url.get("url") if isinstance(image_url, Mapping) else None
    externalized_image = (
        externalized_url.get("$externalized_data_url")
        if isinstance(externalized_url, Mapping)
        else None
    )
    expected_externalized_keys = {
        "base64_alphabet",
        "content_blob",
        "content_path",
        "media_type",
        "original_text_blob",
    }
    _require(
        isinstance(externalized_image, Mapping)
        and set(externalized_image) == expected_externalized_keys,
        "gui_owl_request_image_view_invalid",
        "GUI-Owl request-view image must retain the authoritative data-URL provenance",
        task_key=task_key,
        target_step=target_step,
    )
    request_images = request_payload.get("request_images")
    _require(
        isinstance(request_images, list) and len(request_images) == 1,
        "gui_owl_request_image_count_invalid",
        "formal GUI-Owl history_n=1 must send exactly one current screenshot",
        task_key=task_key,
        target_step=target_step,
        actual=(len(request_images) if isinstance(request_images, list) else None),
    )
    request_image = request_images[0]
    expected_request_image_keys = {
        "canonical_base64",
        "capture_status",
        "content_blob",
        "content_path",
        "height",
        "media_type",
        "original_text_blob",
        "width",
    }
    _require(
        isinstance(request_image, Mapping) and set(request_image) == expected_request_image_keys,
        "gui_owl_request_image_record_invalid",
        "GUI-Owl captured request-image record has an unsupported provenance shape",
        task_key=task_key,
        target_step=target_step,
    )
    expected_content_path = "messages[1].content[1].image_url.url"
    _require(
        request_image.get("content_path") == expected_content_path,
        "gui_owl_request_image_path_invalid",
        "GUI-Owl request image must be the sole current user-message image",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("capture_status") == "captured"
        and request_image.get("canonical_base64") is True
        and externalized_image.get("base64_alphabet") == "standard",
        "gui_owl_request_image_capture_invalid",
        "GUI-Owl request image must be captured canonical standard-base64 data",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        all(
            externalized_image.get(field) == request_image.get(field)
            for field in ("content_blob", "original_text_blob", "content_path", "media_type")
        )
        and externalized_image.get("content_path") == expected_content_path,
        "gui_owl_request_image_provenance_mismatch",
        "GUI-Owl request-view image and captured image record must agree exactly",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        blob_reader is not None,
        "gui_owl_blob_reader_required",
        "GUI-Owl current-image proof requires strict source blob access",
        task_key=task_key,
        target_step=target_step,
    )
    observation = cores[target_position].get("pre_observation")
    screenshot = observation.get("screenshot") if isinstance(observation, Mapping) else None
    observation_blob = screenshot.get("pixel_blob") if isinstance(screenshot, Mapping) else None
    request_blob = request_image.get("content_blob")
    _require(
        isinstance(observation_blob, Mapping) and isinstance(request_blob, Mapping),
        "gui_owl_current_image_blob_missing",
        "GUI-Owl current observation and request image must both have captured blobs",
        task_key=task_key,
        target_step=target_step,
    )
    observation_pixels = _decoded_rgb_fingerprint(
        blob_reader(observation_blob),
        code="gui_owl_observation_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    request_pixels = _decoded_rgb_fingerprint(
        blob_reader(request_blob),
        code="gui_owl_request_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("width") == request_pixels["width"]
        and request_image.get("height") == request_pixels["height"],
        "gui_owl_request_image_dimensions_mismatch",
        "GUI-Owl request-image dimensions must match its decoded pixel matrix",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_pixels == observation_pixels,
        "gui_owl_current_image_mismatch",
        "GUI-Owl request image must equal the current observation's RGB pixel matrix",
        task_key=task_key,
        target_step=target_step,
    )


def _gui_owl_prediction_record(
    prediction: Any,
    *,
    task_key: str,
    source_step: int,
) -> dict[str, Any]:
    """Mirror the accepted tagged output and retain Action/tool boundaries."""

    _require(
        isinstance(prediction, str),
        "gui_owl_prediction_invalid",
        "GUI-Owl source prediction must be text",
        task_key=task_key,
        source_step=source_step,
    )
    text = prediction.strip()
    action_parts = text.split("Action:", 1)
    action_content = action_parts[1] if len(action_parts) > 1 else text
    tool_parts = action_content.split("<tool_call>", 1)
    _require(
        len(tool_parts) == 2 and "</tool_call>" in tool_parts[1],
        "gui_owl_prediction_tool_block_invalid",
        "accepted GUI-Owl source prediction must contain one closed tool_call block",
        task_key=task_key,
        source_step=source_step,
    )
    conclusion = tool_parts[0].strip()
    if conclusion.startswith('"') and conclusion.endswith('"'):
        conclusion = conclusion[1:-1]
    tool_raw = tool_parts[1].split("</tool_call>", 1)[0].strip()
    try:
        tool_call = json.loads(tool_raw)
    except (json.JSONDecodeError, TypeError) as error:
        raise MotivationCardError(
            "gui_owl_prediction_tool_json_invalid",
            "accepted GUI-Owl source tool_call is not valid JSON",
            task_key=task_key,
            source_step=source_step,
        ) from error
    _require(
        isinstance(tool_call, Mapping)
        and isinstance(tool_call.get("name"), str)
        and bool(tool_call["name"].strip())
        and isinstance(tool_call.get("arguments"), Mapping),
        "gui_owl_prediction_tool_shape_invalid",
        "accepted GUI-Owl source tool_call must have string name and object arguments",
        task_key=task_key,
        source_step=source_step,
    )
    return {
        "conclusion": conclusion,
        "tool_call": {
            "name": tool_call["name"].strip(),
            "arguments": _json_clone(tool_call["arguments"]),
        },
    }


def _gui_owl_prediction_conclusion(
    prediction: Any,
    *,
    task_key: str,
    source_step: int,
) -> str:
    """Return the minimal actor-authored Action text from one accepted turn."""

    return _gui_owl_prediction_record(
        prediction,
        task_key=task_key,
        source_step=source_step,
    )["conclusion"]


def _gui_owl_source_action_evidence(
    source_core: Mapping[str, Any],
    *,
    task_key: str,
    source_step: int,
) -> tuple[Mapping[str, Any], str]:
    """Resolve A_i and verify its execution-boundary copy when one exists."""

    action = source_core.get("action")
    _require(
        isinstance(action, Mapping),
        "gui_owl_source_action_invalid",
        "a collapsed GUI-Owl action record must have a parsed source action",
        task_key=task_key,
        source_step=source_step,
    )
    execution = source_core.get("execution")
    if execution is None:
        return action, "parsed_action_only_not_executed"
    execution_payload = _mapping(execution, "payload")
    execution_action = execution_payload.get("action")
    _require(
        isinstance(execution_action, Mapping) and execution_action == action,
        "gui_owl_parsed_execution_action_mismatch",
        "GUI-Owl parsed action and action_execution_started copy must agree exactly",
        task_key=task_key,
        source_step=source_step,
    )
    return action, "parsed_and_execution_started_match"


def _gui_owl_action_record_alignment(
    action_text: str,
    actual_action: Mapping[str, Any],
) -> dict[str, Any]:
    """Mechanically align a short imperative with A_i, without outcome evidence.

    The GUI-Owl prompt fixes the text's discourse role as an Action.  This
    parser therefore uses only an anchored imperative grammar (English and
    Chinese in the audited checkpoint), never arbitrary substring occurrence
    or screenshot change.  Unrecognized wording abstains instead of becoming
    a mismatch.
    """

    description = _gui_owl_described_action(action_text)
    actual_action_type = actual_action.get("action_type")
    actual_action_type = (
        actual_action_type.strip()
        if isinstance(actual_action_type, str) and actual_action_type.strip()
        else None
    )
    if (
        description["described_operation"] is None
        or actual_action_type is None
        or actual_action_type in _GUI_OWL_TERMINAL_META_ACTION_TYPES
    ):
        return {
            **description,
            "actual_action_type": actual_action_type,
            "operation_status": "unresolved",
            "text_argument_status": "not_checked",
            "mismatch_dimensions": [],
            "status": "unresolved",
            "abstention_reason": (
                "terminal_meta_action"
                if actual_action_type in _GUI_OWL_TERMINAL_META_ACTION_TYPES
                else "operation_not_resolved"
            ),
            "uses_outcome_evidence": False,
        }

    compatible_action_types = set(description["compatible_action_types"])
    operation_status = "match" if actual_action_type in compatible_action_types else "mismatch"
    text_argument_status = "not_applicable"
    mismatch_dimensions = [] if operation_status == "match" else ["operation"]
    explicit_text_argument = description["explicit_text_argument"]
    actual_text_argument = actual_action.get("text")
    if description["described_operation"] == "input_text" and explicit_text_argument is not None:
        if isinstance(actual_text_argument, str):
            expected = _SPACE_RE.sub(" ", explicit_text_argument).strip()
            actual = _SPACE_RE.sub(" ", actual_text_argument).strip()
            text_argument_status = "match" if expected == actual else "mismatch"
        else:
            text_argument_status = "mismatch"
        if text_argument_status == "mismatch":
            mismatch_dimensions.append("text_argument")
    return {
        **description,
        "actual_action_type": actual_action_type,
        "actual_text_argument_sha256": (
            _span_sha256(actual_text_argument) if isinstance(actual_text_argument, str) else None
        ),
        "operation_status": operation_status,
        "text_argument_status": text_argument_status,
        "mismatch_dimensions": sorted(set(mismatch_dimensions)),
        "status": "mismatch" if mismatch_dimensions else "match",
        "abstention_reason": None,
        "uses_outcome_evidence": False,
    }


def _gui_owl_described_action(action_text: str) -> dict[str, Any]:
    normalized = _SPACE_RE.sub(" ", action_text).strip()
    imperative = _GUI_OWL_IMPERATIVE_FILLER_RE.sub("", normalized).strip()
    operation: str | None = None
    basis: str | None = None
    for candidate, pattern in _GUI_OWL_ACTION_OPERATION_PATTERNS:
        if pattern.search(imperative) is not None:
            operation = candidate
            basis = f"anchored_imperative:{candidate}"
            break
    compatible = set(_GUI_OWL_COMPATIBLE_ACTION_TYPES.get(operation, ()))
    if operation == "navigate_back" and _GUI_OWL_BACK_TO_HOME_RE.search(imperative) is not None:
        compatible.add("navigate_home")
        basis = "anchored_imperative:navigate_back_to_home"
    if operation == "click":
        if _GUI_OWL_BACK_CONTROL_RE.search(imperative) is not None:
            compatible.add("navigate_back")
            basis = "anchored_imperative:click_back_control"
        if _GUI_OWL_HOME_CONTROL_RE.search(imperative) is not None:
            compatible.add("navigate_home")
            basis = "anchored_imperative:click_home_control"
    explicit_text_argument = (
        _gui_owl_explicit_text_argument(imperative) if operation == "input_text" else None
    )
    return {
        "described_operation": operation,
        "compatible_action_types": sorted(compatible),
        "description_basis": basis,
        "explicit_text_argument": explicit_text_argument,
        "explicit_text_argument_sha256": (
            _span_sha256(explicit_text_argument) if explicit_text_argument is not None else None
        ),
    }


def _gui_owl_explicit_text_argument(imperative: str) -> str | None:
    for pattern in _GUI_OWL_QUOTED_TEXT_ARGUMENT_PATTERNS:
        match = pattern.search(imperative)
        if match is not None:
            value = _SPACE_RE.sub(" ", match.group(1)).strip()
            return value or None
    return None


def _gui_owl_add_period_robustly(text: str) -> str:
    """Clone the runtime punctuation rule used by ``_format_previous_steps``."""

    if not text or not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return stripped
    end_punctuations = {
        "。",
        "！",
        "？",
        "…",
        "；",
        ".",
        "!",
        "?",
        ";",
        "~",
        "～",
        "》",
        "」",
        "』",
        "）",
        ")",
        "]",
        "}",
    }
    if stripped[-1] in end_punctuations:
        return stripped
    chinese_count = sum(1 for character in stripped if "\u4e00" <= character <= "\u9fff")
    english_count = sum(1 for character in stripped if character.isalpha() and ord(character) < 128)
    return stripped + ("。" if chinese_count > english_count else ".")


def _validate_gui_owl_aligned_result(
    observation: Any,
    rendered_result: str,
    *,
    task_key: str,
    source_step: int,
    target_step: int,
) -> dict[str, Any]:
    """Verify one collapsed result against observation ``source_step + 1``."""

    _require(
        isinstance(observation, Mapping),
        "gui_owl_result_observation_missing",
        "GUI-Owl collapsed result requires the following observation",
        task_key=task_key,
        source_step=source_step,
        target_step=target_step,
    )
    tool_result = observation.get("tool_call")
    ask_result = observation.get("ask_user_response")
    if tool_result is not None:
        kind = "tool_call"
        value = tool_result
        exact = str(tool_result)
        aligned = rendered_result == exact
        if not aligned and isinstance(tool_result, (Mapping, list, tuple)):
            try:
                aligned = ast.literal_eval(rendered_result) == tool_result
            except (SyntaxError, ValueError, TypeError):
                aligned = False
    elif ask_result is not None:
        kind = "ask_user_response"
        value = ask_result
        aligned = rendered_result == f"(Ask_user_response){ask_result}"
    else:
        kind = "none"
        value = None
        aligned = rendered_result == "None"
    _require(
        aligned,
        "gui_owl_result_alignment_mismatch",
        "GUI-Owl collapsed Tool response is not aligned with observation source_step + 1",
        task_key=task_key,
        source_step=source_step,
        result_observation_step=source_step + 1,
        target_step=target_step,
        result_kind=kind,
    )
    return {"kind": kind, "value_sha256": _stable_digest(value)}


def _all_substring_positions(text: str, needle: str, *, start: int = 0) -> list[int]:
    positions: list[int] = []
    cursor = start
    while True:
        position = text.find(needle, cursor)
        if position < 0:
            return positions
        positions.append(position)
        cursor = position + 1


def _map_ui_venus_flat_previous_actions_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    task_instruction: str,
    blob_reader: Callable[[Mapping[str, Any]], bytes] | None,
) -> list[dict[str, Any]]:
    """Replay UI-Venus's cumulative, zero-based ``Previous Actions`` exactly.

    Runtime history stores one ``StepData`` for every returned model string,
    including parse failures.  Only the runtime-extracted ``think`` and
    ``action`` fields are rendered; ``conclusion`` and ``status`` never enter a
    later request.  A missing or empty action tag uses the complete stripped
    provider-return value, matching ``VenusNaviAgent.predict``'s bare-action
    fallback.
    """

    target_step = target_position + 1
    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list) and len(messages) == 2,
        "ui_venus_request_messages_invalid",
        "UI-Venus request must contain exactly its system and current user messages",
        task_key=task_key,
        target_step=target_step,
    )
    system_message, user_message = messages
    _require(
        isinstance(system_message, Mapping)
        and system_message.get("role") == "system"
        and system_message.get("content") == _UI_VENUS_SYSTEM_MESSAGE,
        "ui_venus_system_message_invalid",
        "UI-Venus system message does not match the runtime constant",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        isinstance(user_message, Mapping)
        and user_message.get("role") == "user"
        and _qwen_text_image_content_shape(user_message, expected_types=("text", "image_url")),
        "ui_venus_user_message_invalid",
        "UI-Venus user message must contain one text block followed by the current image",
        task_key=task_key,
        target_step=target_step,
    )
    user_content = user_message["content"]
    message_text = user_content[0].get("text")
    image_url = user_content[1].get("image_url")
    _require(
        isinstance(message_text, str),
        "ui_venus_query_text_invalid",
        "UI-Venus query content must be text",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        isinstance(image_url, Mapping) and set(image_url) == {"url"},
        "ui_venus_request_image_invalid",
        "UI-Venus current-image block must contain only image_url.url",
        task_key=task_key,
        target_step=target_step,
    )
    externalized_url = image_url["url"]
    _require(
        isinstance(externalized_url, Mapping)
        and set(externalized_url) == {"$externalized_data_url"},
        "ui_venus_request_image_view_invalid",
        "UI-Venus request-view image must retain the authoritative externalized data URL",
        task_key=task_key,
        target_step=target_step,
    )
    externalized_image = externalized_url["$externalized_data_url"]
    expected_externalized_keys = {
        "base64_alphabet",
        "content_blob",
        "content_path",
        "media_type",
        "original_text_blob",
    }
    _require(
        isinstance(externalized_image, Mapping)
        and set(externalized_image) == expected_externalized_keys,
        "ui_venus_request_image_view_invalid",
        "UI-Venus externalized request-view image has an unsupported provenance shape",
        task_key=task_key,
        target_step=target_step,
    )

    request_images = request_payload.get("request_images")
    _require(
        isinstance(request_images, list) and len(request_images) == 1,
        "ui_venus_request_image_count_invalid",
        "UI-Venus request must contain exactly one captured current image",
        task_key=task_key,
        target_step=target_step,
        actual=(len(request_images) if isinstance(request_images, list) else None),
    )
    request_image = request_images[0]
    expected_request_image_keys = {
        "canonical_base64",
        "capture_status",
        "content_blob",
        "content_path",
        "height",
        "media_type",
        "original_text_blob",
        "width",
    }
    _require(
        isinstance(request_image, Mapping) and set(request_image) == expected_request_image_keys,
        "ui_venus_request_image_record_invalid",
        "UI-Venus captured request-image record has an unsupported provenance shape",
        task_key=task_key,
        target_step=target_step,
    )
    expected_content_path = "messages[1].content[1].image_url.url"
    _require(
        request_image.get("content_path") == expected_content_path,
        "ui_venus_request_image_path_invalid",
        "UI-Venus request image must be the current user-message image block",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("capture_status") == "captured"
        and request_image.get("canonical_base64") is True
        and externalized_image.get("base64_alphabet") == "standard",
        "ui_venus_request_image_capture_invalid",
        "UI-Venus request image must be a captured canonical standard-base64 data URL",
        task_key=task_key,
        target_step=target_step,
    )
    media_type = request_image.get("media_type")
    _require(
        isinstance(media_type, str) and media_type.startswith("image/"),
        "ui_venus_request_image_media_type_invalid",
        "UI-Venus captured request image must have an image media type",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        all(
            externalized_image.get(field) == request_image.get(field)
            for field in ("content_blob", "original_text_blob", "content_path", "media_type")
        )
        and externalized_image.get("content_path") == expected_content_path,
        "ui_venus_request_image_provenance_mismatch",
        "UI-Venus request-view image and captured request-image record must agree exactly",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        blob_reader is not None,
        "ui_venus_blob_reader_required",
        "UI-Venus current-image provenance requires strict blob-byte access",
        task_key=task_key,
        target_step=target_step,
    )
    observation = cores[target_position].get("pre_observation")
    screenshot = observation.get("screenshot") if isinstance(observation, Mapping) else None
    observation_blob = screenshot.get("pixel_blob") if isinstance(screenshot, Mapping) else None
    request_blob = request_image.get("content_blob")
    _require(
        isinstance(observation_blob, Mapping) and isinstance(request_blob, Mapping),
        "ui_venus_current_image_blob_missing",
        "UI-Venus current observation and request image must both have captured blobs",
        task_key=task_key,
        target_step=target_step,
    )
    observation_pixels = _decoded_rgb_fingerprint(
        blob_reader(observation_blob),
        code="ui_venus_observation_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    request_pixels = _decoded_rgb_fingerprint(
        blob_reader(request_blob),
        code="ui_venus_request_image_decode_failed",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        request_image.get("width") == request_pixels["width"]
        and request_image.get("height") == request_pixels["height"],
        "ui_venus_request_image_dimensions_mismatch",
        "UI-Venus captured request-image dimensions must match its decoded pixel matrix",
        task_key=task_key,
        target_step=target_step,
        recorded_width=request_image.get("width"),
        recorded_height=request_image.get("height"),
        decoded_width=request_pixels["width"],
        decoded_height=request_pixels["height"],
    )
    _require(
        request_pixels == observation_pixels,
        "ui_venus_current_image_mismatch",
        "UI-Venus request image must have the current observation's exact RGB pixel matrix",
        task_key=task_key,
        target_step=target_step,
        request_pixels=request_pixels,
        current_observation_pixels=observation_pixels,
    )

    _require(
        UI_VENUS_15_PROMPT.count(_UI_VENUS_HISTORY_PLACEHOLDER) == 1
        and UI_VENUS_15_PROMPT.count(_UI_VENUS_TASK_PLACEHOLDER) == 1,
        "ui_venus_prompt_template_unsupported",
        "UI-Venus runtime prompt placeholders changed; update the exact mapper",
    )
    raw_prefix, raw_suffix = UI_VENUS_15_PROMPT.split(_UI_VENUS_HISTORY_PLACEHOLDER, 1)
    prompt_prefix = raw_prefix.replace(_UI_VENUS_TASK_PLACEHOLDER, task_instruction)
    prompt_suffix = raw_suffix.replace(_UI_VENUS_TASK_PLACEHOLDER, task_instruction)

    history_entries: list[str] = []
    parsed_history: list[dict[str, Any]] = []
    for history_ordinal, source_core in enumerate(cores[:target_position]):
        prediction = source_core.get("prediction")
        _require(
            isinstance(prediction, str),
            "ui_venus_prediction_invalid",
            "UI-Venus source prediction must be text",
            task_key=task_key,
            source_step=source_core["step_index"],
        )
        think_text, action_text = _ui_venus_history_fields(prediction)
        history_payload = f"<think>{think_text}</think><action>{action_text}</action>"
        step_label = f"Step {history_ordinal}: "
        history_entry = step_label + history_payload
        history_entries.append(history_entry)
        parsed_history.append(
            {
                "source_core": source_core,
                "history_ordinal": history_ordinal,
                "think_text": think_text,
                "action_text": action_text,
                "history_payload": history_payload,
                "history_entry": history_entry,
                "step_label": step_label,
            }
        )

    rendered_history = "\n".join(history_entries)
    expected_query = prompt_prefix + rendered_history + prompt_suffix
    _require(
        message_text == expected_query,
        "ui_venus_previous_actions_content_mismatch",
        "UI-Venus Previous Actions do not exactly match all prior runtime history entries",
        task_key=task_key,
        target_step=target_step,
        expected_history_count=target_position,
        actual_sha256=_span_sha256(message_text),
        expected_sha256=_span_sha256(expected_query),
    )

    exposures: list[dict[str, Any]] = []
    cursor = len(prompt_prefix)
    for index, record in enumerate(parsed_history):
        source_core = record["source_core"]
        step_span_start = cursor
        step_span_end = step_span_start + len(record["history_entry"])
        span_start = step_span_start + len(record["step_label"])
        span_end = step_span_end
        _require(
            message_text[step_span_start:step_span_end] == record["history_entry"]
            and message_text[span_start:span_end] == record["history_payload"],
            "ui_venus_previous_actions_span_mismatch",
            "UI-Venus history entry cannot be recovered at its exact runtime span",
            task_key=task_key,
            source_step=source_core["step_index"],
            target_step=target_step,
        )
        exposures.append(
            {
                "mapping_status": "exact_ui_venus_flat_previous_actions",
                "representation_type": "flat_previous_actions",
                "message_index": 1,
                "content_block_index": 0,
                "source_step_index": source_core["step_index"],
                "source_step_id": source_core["step_id"],
                "source_decision_event_id": source_core["decision"]["event_id"],
                "source_prediction_sha256": _value_summary(source_core.get("prediction"))["sha256"],
                "history_ordinal": record["history_ordinal"],
                "assistant_think_text": record["think_text"],
                "assistant_think_sha256": _span_sha256(record["think_text"]),
                "assistant_action_text": record["action_text"],
                "assistant_action_sha256": _span_sha256(record["action_text"]),
                "assistant_history_text": record["history_payload"],
                "assistant_history_sha256": _span_sha256(record["history_payload"]),
                "exposed_text": record["history_payload"],
                "exposed_text_sha256": _span_sha256(record["history_payload"]),
                "span_start": span_start,
                "span_end": span_end,
                "step_span_start": step_span_start,
                "step_span_end": step_span_end,
                "step_span_sha256": _span_sha256(record["history_entry"]),
                "target_step_index": target_step,
                "lag": target_step - source_core["step_index"],
            }
        )
        cursor = step_span_end + (1 if index + 1 < len(parsed_history) else 0)
    _require(
        cursor == len(prompt_prefix) + len(rendered_history),
        "ui_venus_previous_actions_boundary_mismatch",
        "UI-Venus history span does not end at the runtime template boundary",
        task_key=task_key,
        target_step=target_step,
    )
    return exposures


def _ui_venus_history_fields(prediction: str) -> tuple[str, str]:
    """Mirror the two exact extraction expressions in ``VenusNaviAgent.predict``."""

    think_match = re.search(r"<think>(.*?)</think>", prediction, re.DOTALL)
    action_match = re.search(r"<action>(.*?)</action>", prediction, re.DOTALL)
    think_text = think_match.group(1).strip() if think_match else ""
    tagged_action = action_match.group(1).strip() if action_match else None
    action_text = tagged_action or prediction.strip()
    return think_text, action_text


def _decoded_rgb_fingerprint(
    data: bytes,
    *,
    code: str,
    task_key: str,
    target_step: int,
) -> dict[str, Any]:
    """Return encoding-independent image identity without retaining image bytes."""

    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            rgb = image.convert("RGB")
            width, height = rgb.size
            pixel_sha256 = _sha256(rgb.tobytes())
    except (OSError, ValueError) as error:
        raise MotivationCardError(
            code,
            "captured request or observation image blob is not decodable",
            task_key=task_key,
            target_step=target_step,
            error=type(error).__name__,
        ) from error
    return {
        "mode": "RGB",
        "width": width,
        "height": height,
        "pixel_sha256": pixel_sha256,
    }


def _map_gelab_rolling_summary_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    task_instruction: str,
) -> list[dict[str, Any]]:
    """Map GELab's one-step rolling summary to its exact request span.

    GELab retains only the immediately preceding parsed ``summary`` value.  A
    user response is appended by the adapter for the next request, but remains
    external evidence rather than model-authored summary text.
    """

    target_step = target_position + 1
    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list) and len(messages) == 1,
        "gelab_request_messages_invalid",
        "GELab request must contain exactly one current user message",
        task_key=task_key,
        target_step=target_step,
    )
    message = messages[0]
    _require(
        isinstance(message, Mapping) and message.get("role") == "user",
        "gelab_request_message_invalid",
        "GELab request message must have the user role",
        task_key=task_key,
        target_step=target_step,
    )
    content = message.get("content")
    _require(
        isinstance(content, list)
        and tuple(part.get("type") if isinstance(part, Mapping) else None for part in content)
        == ("text", "text", "image_url", "text"),
        "gelab_request_content_invalid",
        "GELab user message must contain text, text, image_url, text in that order",
        task_key=task_key,
        target_step=target_step,
    )
    system_part, user_prompt_part, image_part, instruction_part = content
    _require(
        system_part.get("text") == GELAB_SYSTEM_PROMPT,
        "gelab_system_prompt_mismatch",
        "GELab system prompt block does not match the runtime constant",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        instruction_part.get("text") == GELAB_INSTRUCTION_SUFFIX,
        "gelab_instruction_suffix_mismatch",
        "GELab instruction suffix block does not match the runtime constant",
        task_key=task_key,
        target_step=target_step,
    )
    _require(
        isinstance(image_part.get("image_url"), Mapping) and "url" in image_part["image_url"],
        "gelab_request_image_invalid",
        "GELab image block must retain its image_url.url value",
        task_key=task_key,
        target_step=target_step,
    )
    message_text = user_prompt_part.get("text")
    _require(
        isinstance(message_text, str),
        "gelab_user_prompt_invalid",
        "GELab task/history block must be text",
        task_key=task_key,
        target_step=target_step,
    )

    source_core: Mapping[str, Any] | None = None
    assistant_summary = ""
    latest_action: Mapping[str, Any] | None = None
    for prior_core in cores[:target_position]:
        parsed_action = _gelab_parse_prediction(prior_core.get("prediction"))
        if parsed_action is not None:
            source_core = prior_core
            latest_action = parsed_action
    if latest_action is not None:
        summary_value = latest_action.get("summary", "")
        _require(
            isinstance(summary_value, str),
            "gelab_summary_invalid",
            "GELab parsed summary must be text",
            task_key=task_key,
            source_step=source_core["step_index"] if source_core is not None else None,
        )
        assistant_summary = summary_value

    observation = cores[target_position].get("pre_observation")
    raw_user_response = (
        observation.get("ask_user_response") if isinstance(observation, Mapping) else None
    )
    _require(
        not assistant_summary or not raw_user_response or isinstance(raw_user_response, str),
        "gelab_user_response_invalid",
        "a truthy GELab ask-user response appended to summary must be text",
        task_key=task_key,
        target_step=target_step,
    )
    user_response = raw_user_response or ""
    external_suffix = (
        f"{_GELAB_USER_RESPONSE_PREFIX}{user_response}"
        if assistant_summary and user_response
        else ""
    )
    exposed_text = (
        assistant_summary + external_suffix if assistant_summary else _GELAB_EMPTY_HISTORY
    )

    expected_prompt = GELAB_USER_PROMPT_TEMPLATE.render(
        task=task_instruction,
        history_display=exposed_text,
    )
    _require(
        message_text == expected_prompt,
        "gelab_user_prompt_template_mismatch",
        "GELab task/history text does not match the exact runtime rendering",
        task_key=task_key,
        target_step=target_step,
        actual_sha256=_span_sha256(message_text),
        expected_sha256=_span_sha256(expected_prompt),
    )
    empty_prompt = GELAB_USER_PROMPT_TEMPLATE.render(
        task=task_instruction,
        history_display="",
    )
    _require(
        empty_prompt.endswith(_GELAB_USER_PROMPT_SUFFIX),
        "gelab_prompt_boundary_unsupported",
        "GELab runtime prompt boundary changed; update the exact mapper",
        task_key=task_key,
    )
    span_start = len(empty_prompt) - len(_GELAB_USER_PROMPT_SUFFIX)
    span_end = span_start + len(exposed_text)
    _require(
        message_text[span_start:span_end] == exposed_text
        and message_text[span_end:] == _GELAB_USER_PROMPT_SUFFIX,
        "gelab_history_span_mismatch",
        "GELab history span cannot be located at the exact runtime boundary",
        task_key=task_key,
        target_step=target_step,
    )

    if not assistant_summary:
        return []
    _require(
        source_core is not None,
        "gelab_summary_provenance_invalid",
        "GELab non-empty rolling summary has no successfully parsed source decision",
        task_key=task_key,
        target_step=target_step,
    )
    assistant_span_end = span_start + len(assistant_summary)
    return [
        {
            "mapping_status": "exact_gelab_rolling_summary",
            "representation_type": "rolling_summary",
            "message_index": 0,
            "content_block_index": 1,
            "source_step_index": source_core["step_index"],
            "source_step_id": source_core["step_id"],
            "source_decision_event_id": source_core["decision"]["event_id"],
            "source_prediction_sha256": _value_summary(source_core.get("prediction"))["sha256"],
            "source_summary_sha256": _span_sha256(assistant_summary),
            "assistant_summary_text": assistant_summary,
            "assistant_summary_sha256": _span_sha256(assistant_summary),
            "external_evidence_suffix": external_suffix or None,
            "external_evidence_suffix_sha256": (
                _span_sha256(external_suffix) if external_suffix else None
            ),
            "exposed_text": exposed_text,
            "exposed_text_sha256": _span_sha256(exposed_text),
            "span_start": span_start,
            "span_end": span_end,
            "assistant_span_start": span_start,
            "assistant_span_end": assistant_span_end,
            "external_span_start": assistant_span_end if external_suffix else None,
            "external_span_end": span_end if external_suffix else None,
            "target_step_index": target_step,
            "lag": target_step - source_core["step_index"],
        }
    ]


def _gelab_parse_prediction(prediction: Any) -> dict[str, Any] | None:
    """Mirror ``parse_gelab_response`` and its predict-level exception boundary.

    The runtime skips colonless fields, lets later duplicate keys overwrite
    earlier ones, tolerates missing THINK tags, and does not append an action
    when parsing raises.  ``None`` represents that last case so replay can keep
    the most recent earlier action exactly as ``self.actions`` does.
    """

    try:
        response = prediction.strip()
        response = _GELAB_THINK_TAG_RE.sub(
            lambda match: "<THINK>" if "/" not in match.group() else "</THINK>",
            response,
        )
        try:
            cot = response.split("<THINK>")[1].split("</THINK>")[0].strip()
            kv_part = response.split("</THINK>")[1].strip()
        except IndexError:
            kv_part = response
            cot = ""

        action: dict[str, Any] = {"cot": cot}
        for raw_field in kv_part.split("\t"):
            field = raw_field.strip()
            if ":" not in field:
                continue
            key, value = field.split(":", 1)
            key = key.strip()
            value = value.strip()
            if "point" in key:
                coordinates = value.replace(",", " ").split()
                if len(coordinates) >= 2:
                    action[key] = [int(coordinates[0]), int(coordinates[1])]
            else:
                action[key] = value
        return action
    except Exception:
        return None


def _map_qwen_flat_progress_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
    *,
    task_instruction: str,
) -> list[dict[str, Any]]:
    """Map Qwen's exact flat-progress spans back to parsed prior conclusions."""

    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list) and len(messages) == 2,
        "qwen_request_messages_invalid",
        "Qwen request must contain exactly its system and current user messages",
        task_key=task_key,
        target_step=target_position + 1,
    )
    system_message, user_message = messages
    _require(
        isinstance(system_message, Mapping)
        and system_message.get("role") == "system"
        and _qwen_text_image_content_shape(system_message, expected_types=("text",)),
        "qwen_system_message_invalid",
        "Qwen system message must contain exactly one text block",
        task_key=task_key,
        target_step=target_position + 1,
    )
    _require(
        isinstance(user_message, Mapping)
        and user_message.get("role") == "user"
        and _qwen_text_image_content_shape(user_message, expected_types=("text", "image_url")),
        "qwen_user_message_invalid",
        "Qwen user message must contain one text block followed by the current image",
        task_key=task_key,
        target_step=target_position + 1,
    )
    user_content = user_message["content"]
    message_text = user_content[0].get("text")
    _require(
        isinstance(message_text, str),
        "qwen_progress_text_invalid",
        "Qwen progress content must be text",
        task_key=task_key,
        target_step=target_position + 1,
    )
    header = f"{_QWEN_USER_PREFIX}{task_instruction}\n{_QWEN_PROGRESS_MARKER}"
    _require(
        message_text.startswith(header) and message_text.endswith("\n"),
        "qwen_progress_template_mismatch",
        "Qwen user text does not match the exact task/progress template",
        task_key=task_key,
        target_step=target_position + 1,
        text_sha256=_span_sha256(message_text),
    )
    progress = message_text[len(header) : -1]
    _require(
        _QWEN_PROGRESS_MARKER not in progress,
        "qwen_progress_marker_ambiguous",
        "a rendered Qwen conclusion collides with the progress marker",
        task_key=task_key,
        target_step=target_position + 1,
    )

    expected_parts: list[str] = []
    exposures: list[dict[str, Any]] = []
    progress_cursor = 0
    for source_position, source_core in enumerate(cores[:target_position]):
        source_step = source_core["step_index"]
        conclusion = _qwen_prediction_conclusion(
            source_core.get("prediction"),
            task_key=task_key,
            source_step=source_step,
        )
        rendered_record = _qwen_rendered_conclusion(
            conclusion,
            next_observation=cores[source_position + 1].get("pre_observation"),
            task_key=task_key,
            source_step=source_step,
        )
        rendered = rendered_record["exposed_text"]
        _require(
            _QWEN_STEP_DELIMITER_RE.search(rendered) is None,
            "qwen_progress_step_delimiter_ambiguous",
            "a rendered Qwen conclusion collides with a flat-progress step delimiter",
            task_key=task_key,
            source_step=source_step,
            target_step=target_position + 1,
        )
        label = f"Step {source_step}: "
        step_text = f"{label}{rendered}; "
        step_span_start = len(header) + progress_cursor
        span_start = step_span_start + len(label)
        span_end = span_start + len(rendered)
        step_span_end = step_span_start + len(step_text)
        expected_parts.append(step_text)
        progress_cursor += len(step_text)
        exposures.append(
            {
                "mapping_status": "exact_qwen_flat_progress",
                "representation_type": "flat_progress",
                "message_index": 1,
                "content_block_index": 0,
                "source_step_index": source_step,
                "source_step_id": source_core["step_id"],
                "source_decision_event_id": source_core["decision"]["event_id"],
                "source_prediction_sha256": _value_summary(source_core.get("prediction"))["sha256"],
                "source_conclusion_sha256": _span_sha256(conclusion),
                "assistant_conclusion_text": rendered_record["assistant_conclusion_text"],
                "assistant_conclusion_sha256": rendered_record["assistant_conclusion_sha256"],
                "external_evidence_suffix": rendered_record["external_evidence_suffix"],
                "external_evidence_suffix_sha256": rendered_record[
                    "external_evidence_suffix_sha256"
                ],
                "exposed_text": rendered,
                "exposed_text_sha256": _span_sha256(rendered),
                "span_start": span_start,
                "span_end": span_end,
                "assistant_span_start": span_start,
                "assistant_span_end": span_start
                + len(rendered_record["assistant_conclusion_text"]),
                "step_span_start": step_span_start,
                "step_span_end": step_span_end,
                "step_span_sha256": _span_sha256(step_text),
                "target_step_index": target_position + 1,
                "lag": target_position + 1 - source_step,
            }
        )

    expected_progress = "".join(expected_parts)
    _require(
        progress == expected_progress,
        "qwen_progress_content_mismatch",
        "Qwen flat progress does not exactly match all prior parsed conclusions",
        task_key=task_key,
        target_step=target_position + 1,
        expected_step_count=target_position,
        actual_sha256=_span_sha256(progress),
        expected_sha256=_span_sha256(expected_progress),
    )
    return exposures


def _qwen_text_image_content_shape(
    message: Mapping[str, Any], *, expected_types: tuple[str, ...]
) -> bool:
    content = message.get("content")
    return (
        isinstance(content, list)
        and tuple(part.get("type") if isinstance(part, Mapping) else None for part in content)
        == expected_types
    )


def _qwen_prediction_conclusion(prediction: Any, *, task_key: str, source_step: int) -> str:
    _require(
        isinstance(prediction, str) and bool(prediction),
        "qwen_prediction_invalid",
        "Qwen source prediction must be non-empty text",
        task_key=task_key,
        source_step=source_step,
    )
    marker_counts = {
        "Thought:": prediction.count("Thought:"),
        "Action:": prediction.count("Action:"),
        "<tool_call>": prediction.count("<tool_call>"),
        "</tool_call>": prediction.count("</tool_call>"),
    }
    _require(
        all(count == 1 for count in marker_counts.values()),
        "qwen_prediction_markers_invalid",
        "Qwen source prediction must contain one unambiguous tagged response",
        task_key=task_key,
        source_step=source_step,
        marker_counts=marker_counts,
    )
    thought_index = prediction.index("Thought:")
    action_index = prediction.index("Action:")
    tool_index = prediction.index("<tool_call>")
    tool_end_index = prediction.index("</tool_call>")
    _require(
        thought_index < action_index < tool_index < tool_end_index,
        "qwen_prediction_marker_order_invalid",
        "Qwen tagged response markers are out of order",
        task_key=task_key,
        source_step=source_step,
    )
    conclusion = prediction[action_index + len("Action:") : tool_index].strip()
    if conclusion.startswith('"') and conclusion.endswith('"'):
        conclusion = conclusion[1:-1]
    _require(
        bool(conclusion),
        "qwen_conclusion_empty",
        "Qwen parsed action conclusion must not be empty",
        task_key=task_key,
        source_step=source_step,
    )
    return conclusion


def _qwen_rendered_conclusion(
    conclusion: str,
    *,
    next_observation: Any,
    task_key: str,
    source_step: int,
) -> dict[str, Any]:
    external_suffix = ""
    if isinstance(next_observation, Mapping):
        tool_call = next_observation.get("tool_call")
        if tool_call is not None:
            try:
                tool_json = json.dumps(tool_call, ensure_ascii=False)
            except (TypeError, ValueError) as error:
                raise MotivationCardError(
                    "qwen_tool_result_unserializable",
                    "Qwen tool result cannot be reproduced with the adapter renderer",
                    task_key=task_key,
                    source_step=source_step,
                ) from error
            external_suffix += (
                "; Tool call result: <tool_response>" + tool_json + "</tool_response>"
            )
        ask_user_response = next_observation.get("ask_user_response")
        if ask_user_response is not None:
            external_suffix += f"; Ask user response: {ask_user_response}"
    assistant_conclusion = conclusion.replace("\n", "").replace('"', "")
    normalized_suffix = external_suffix.replace("\n", "").replace('"', "")
    rendered = assistant_conclusion + normalized_suffix
    _require(
        bool(assistant_conclusion),
        "qwen_rendered_assistant_conclusion_empty",
        "Qwen assistant conclusion is empty after flat-progress normalization",
        task_key=task_key,
        source_step=source_step,
    )
    _require(
        bool(rendered),
        "qwen_rendered_conclusion_empty",
        "Qwen rendered conclusion must not be empty",
        task_key=task_key,
        source_step=source_step,
    )
    return {
        "assistant_conclusion_text": assistant_conclusion,
        "assistant_conclusion_sha256": _span_sha256(assistant_conclusion),
        "external_evidence_suffix": normalized_suffix or None,
        "external_evidence_suffix_sha256": (
            _span_sha256(normalized_suffix) if normalized_suffix else None
        ),
        "exposed_text": rendered,
    }


def _map_assistant_exposures(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_key: str,
) -> list[dict[str, Any]]:
    request_view = _mapping(request_payload, "request_view")
    messages = request_view.get("messages")
    _require(
        isinstance(messages, list),
        "request_messages_invalid",
        "MAI request_view.messages must be a list",
        task_key=task_key,
        target_step=target_position + 1,
    )
    assistant_parts = [
        (message_index, content_block_index, content)
        for message_index, message in enumerate(messages)
        if isinstance(message, Mapping) and message.get("role") == "assistant"
        for content_block_index, content in _message_text_parts(message)
    ]
    prior_predictions = [core["prediction"] for core in cores[:target_position]]
    contents = [content for _, _, content in assistant_parts]
    source_positions = _unique_subsequence_positions(
        contents,
        prior_predictions,
        task_key=task_key,
        target_step=target_position + 1,
    )
    exposures: list[dict[str, Any]] = []
    for (message_index, content_block_index, content), matched_position in zip(
        assistant_parts, source_positions, strict=True
    ):
        source_core = cores[matched_position]
        exposures.append(
            {
                "mapping_status": "exact_content_monotonic",
                "message_index": message_index,
                "content_block_index": content_block_index,
                "source_step_index": source_core["step_index"],
                "source_step_id": source_core["step_id"],
                "source_decision_event_id": source_core["decision"]["event_id"],
                "source_prediction_sha256": _value_summary(content)["sha256"],
                "target_step_index": target_position + 1,
                "lag": target_position + 1 - source_core["step_index"],
            }
        )
    return exposures


def _unique_subsequence_positions(
    requested: Sequence[Any],
    prior: Sequence[Any],
    *,
    task_key: str,
    target_step: int,
) -> list[int]:
    earliest: list[int] = []
    cursor = 0
    for message_index, content in enumerate(requested):
        while cursor < len(prior) and prior[cursor] != content:
            cursor += 1
        _require(
            cursor < len(prior),
            "assistant_exposure_unresolved",
            "assistant history is not an exact ordered subsequence of earlier decisions",
            task_key=task_key,
            target_step=target_step,
            assistant_ordinal=message_index,
            content_sha256=_value_summary(content)["sha256"],
        )
        earliest.append(cursor)
        cursor += 1

    latest_reversed: list[int] = []
    cursor = len(prior) - 1
    for content in reversed(requested):
        while cursor >= 0 and prior[cursor] != content:
            cursor -= 1
        _require(
            cursor >= 0,
            "assistant_exposure_unresolved",
            "assistant history is not an exact ordered subsequence of earlier decisions",
            task_key=task_key,
            target_step=target_step,
        )
        latest_reversed.append(cursor)
        cursor -= 1
    latest = list(reversed(latest_reversed))
    _require(
        earliest == latest,
        "assistant_exposure_ambiguous",
        "assistant history has more than one exact source-step alignment",
        task_key=task_key,
        target_step=target_step,
        earliest=earliest,
        latest=latest,
    )
    return earliest


def _request_image_records(
    request_payload: Mapping[str, Any],
    observation_digest_steps: Mapping[str, list[int]],
) -> list[dict[str, Any]]:
    images = request_payload.get("request_images")
    if images is None:
        return []
    _require(isinstance(images, list), "request_images_invalid", "request_images must be a list")
    records: list[dict[str, Any]] = []
    for image in images:
        _require(
            isinstance(image, Mapping),
            "request_image_invalid",
            "every request image must be an object",
        )
        copied = _json_clone(image)
        digest = _request_image_digest(copied)
        path = copied.get("content_path")
        match = _MESSAGE_INDEX_RE.match(path) if isinstance(path, str) else None
        records.append(
            {
                "request_image": copied,
                "message_index": int(match.group(1)) if match is not None else None,
                "observation_step_indices_by_exact_digest": (
                    list(observation_digest_steps.get(digest, [])) if digest is not None else []
                ),
            }
        )
    return records


def _request_ask_user_messages(
    cores: Sequence[Mapping[str, Any]],
    target_position: int,
    request_payload: Mapping[str, Any],
    task_goal: Any,
) -> list[dict[str, Any]]:
    messages = _mapping(request_payload, "request_view").get("messages")
    if not isinstance(messages, list):
        return []
    candidates: list[tuple[int, Any]] = []
    for position in range(target_position + 1):
        observation = cores[position].get("pre_observation")
        if isinstance(observation, Mapping) and observation.get("ask_user_response") is not None:
            candidates.append((cores[position]["step_index"], observation["ask_user_response"]))
    used: set[int] = set()
    result: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        text_parts = _message_text_parts(message)
        for content_block_index, value in text_parts:
            if value == task_goal:
                continue
            for candidate_index, (observation_step, response) in enumerate(candidates):
                if candidate_index in used or value != response:
                    continue
                used.add(candidate_index)
                origin_execution_step = observation_step - 1 if observation_step > 1 else None
                result.append(
                    {
                        "message_index": message_index,
                        "content_block_index": content_block_index,
                        "response": _json_clone(response),
                        "observation_step_index": observation_step,
                        "origin_execution_step_index": origin_execution_step,
                    }
                )
                break
    return result


def _message_text_parts(message: Mapping[str, Any]) -> list[tuple[int | None, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [(None, content)]
    if not isinstance(content, list):
        return []
    return [
        (index, part.get("text"))
        for index, part in enumerate(content)
        if isinstance(part, Mapping) and part.get("type") == "text" and "text" in part
    ]


def _reconstruction_step(core: Mapping[str, Any]) -> dict[str, Any]:
    request_payload = _mapping(core["selected_request"], "payload")
    response_payload = _mapping(core["selected_response"], "payload")
    decision_payload = _mapping(core["decision"], "payload")
    transition_payload = _mapping(core["transition"], "payload")
    response_difference: dict[str, Any] = {
        "comparison": core["provider_decision_comparison"],
    }
    if core["provider_decision_comparison"]["status"] != "exact_match":
        response_difference["provider_content_exact"] = _json_clone(core["provider_content"])
        response_difference["decision_prediction_exact"] = _json_clone(core["prediction"])
    return {
        "step_index": core["step_index"],
        "step_id": core["step_id"],
        "S_t": {
            "event_id": core["step_started"]["event_id"],
            "observation": _json_clone(core["pre_observation"]),
        },
        "I_t": {
            "event_id": core["selected_request"]["event_id"],
            "request_id": request_payload.get("request_id"),
            "model_call_id": request_payload.get("model_call_id"),
            "sdk_arguments_snapshot_blob": request_payload.get("sdk_arguments_snapshot_blob"),
            "request_view_sha256": _value_summary(request_payload.get("request_view"))["sha256"],
            "request_images": core["request_images"],
            "request_ask_user_messages": core["request_ask_user_messages"],
            "assistant_exposures": core["assistant_exposures"],
        },
        "P_t": {
            "model_response_event_id": core["selected_response"]["event_id"],
            "raw_response": response_payload.get("raw_response"),
            "returned_value_snapshot_blob": response_payload.get("returned_value_snapshot_blob"),
            "decision_event_id": core["decision"]["event_id"],
            "prediction_raw": decision_payload.get("prediction_raw"),
            "prediction_snapshot_blob": decision_payload.get("prediction_snapshot_blob"),
            "parse_outcome": decision_payload.get("parse_outcome"),
            "parse_exception": decision_payload.get("parse_exception"),
            "provider_vs_decision": response_difference,
        },
        "A_t": {
            "parsed_action": decision_payload.get("parsed_action"),
            "action_execution_started_event_id": (
                core["execution"]["event_id"] if core["execution"] is not None else None
            ),
        },
        "R_t": {
            "transition_event_id": core["transition"]["event_id"],
            "transition_type": core["transition"]["event_type"],
            "execution_result": transition_payload.get("execution_result"),
            "available_execution_result": transition_payload.get("available_execution_result"),
            "exception": transition_payload.get("exception"),
            "reason": transition_payload.get("reason"),
            "duration_ns": transition_payload.get("duration_ns"),
        },
        "S_t_plus_1": {
            "transition_event_id": core["transition"]["event_id"],
            "observation": _json_clone(core["post_observation"]),
        },
    }


def _formal_candidates(
    task_key: str,
    cores: Sequence[Mapping[str, Any]],
    scanner_signals: Sequence[Mapping[str, Any]],
    *,
    adapter: str | None = None,
) -> list[dict[str, Any]]:
    """Anchor every emitted review candidate to an assistant span actually requested."""

    if adapter == _MEMGUI_ADAPTER:
        return _formal_memgui_candidates(task_key, cores, scanner_signals)

    exposures: dict[tuple[int, int], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for target_core in cores:
        for exposure in target_core["assistant_exposures"]:
            source_step = exposure["source_step_index"]
            exposures[(source_step, target_core["step_index"])] = (target_core, exposure)

    reasons_by_pair: defaultdict[tuple[int, int], set[str]] = defaultdict(set)
    long_lag_pairs = {
        (signal["source_step"], signal["target_step"])
        for signal in scanner_signals
        if signal["signal"] == "LONG_LAG_IMAGE_ABSENT"
    }
    for signal in scanner_signals:
        if signal["signal"] == "LONG_LAG_IMAGE_ABSENT":
            continue
        signal_name = _string(signal, "signal")
        source_step = signal["source_step"]
        target_step = signal["target_step"]
        pair = (source_step, target_step)
        attached: set[tuple[int, int]] = set()
        if adapter == _GELAB_ADAPTER and signal_name in {
            "FAILED_TRANSITION_ACK",
            "SELF_CORRECTION",
        }:
            target_exposures = sorted(key for key in exposures if key[1] == target_step)
            _require(
                len(target_exposures) <= 1,
                "gelab_target_exposure_ambiguous",
                "one GELab target request cannot expose multiple rolling-summary sources",
                task_key=task_key,
                target_step=target_step,
                actual=len(target_exposures),
            )
            attached.update(target_exposures)
        elif source_step < target_step and pair in exposures:
            attached.add(pair)
        elif source_step == target_step:
            later = sorted(key for key in exposures if key[0] == source_step)
            if later:
                attached.add(later[0])
        else:
            target_exposures = sorted(
                (key for key in exposures if key[1] == target_step), reverse=True
            )
            if target_exposures:
                attached.add(target_exposures[0])
        for attached_pair in attached:
            reasons_by_pair[attached_pair].add(signal_name)

    for pair in set(reasons_by_pair) & long_lag_pairs:
        reasons_by_pair[pair].add("LONG_LAG_IMAGE_ABSENT")

    gui_owl_action_pairs: list[tuple[int, int]] = []
    if adapter == _GUI_OWL_ADAPTER:
        gui_owl_action_pairs = _add_gui_owl_action_history_reasons(
            task_key,
            exposures,
            reasons_by_pair,
        )

    retained_pairs = _select_formal_candidate_pairs(reasons_by_pair, step_count=len(cores))
    if adapter == _GELAB_ADAPTER:
        retained_pairs = _select_gelab_formal_candidate_pairs(
            retained_pairs,
            reasons_by_pair,
            step_count=len(cores),
        )
    elif adapter == _GUI_OWL_ADAPTER:
        retained_pairs = _select_gui_owl_formal_candidate_pairs(
            sorted(set(retained_pairs) | set(gui_owl_action_pairs)),
            reasons_by_pair,
            step_count=len(cores),
        )

    formal: list[dict[str, Any]] = []
    for source_step, target_step in retained_pairs:
        source_core = cores[source_step - 1]
        target_core, exposure = exposures[(source_step, target_step)]
        reasons = sorted(reasons_by_pair[(source_step, target_step)])
        formal.append(
            _formal_candidate(
                task_key=task_key,
                source_core=source_core,
                target_core=target_core,
                exposure=exposure,
                retrieval_reasons=reasons,
            )
        )
    return sorted(formal, key=lambda candidate: candidate["candidate_id"])


def _add_gui_owl_action_history_reasons(
    task_key: str,
    exposures: Mapping[
        tuple[int, int],
        tuple[Mapping[str, Any], Mapping[str, Any]],
    ],
    reasons_by_pair: defaultdict[tuple[int, int], set[str]],
) -> list[tuple[int, int]]:
    """Make one outcome-blind immediate-exposure entry eligible per source action."""

    appearances_by_source: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair in exposures:
        appearances_by_source[pair[0]].append(pair)
    selected_pairs: list[tuple[int, int]] = []
    for source_step, appearances in sorted(appearances_by_source.items()):
        ordered = sorted(appearances, key=lambda pair: pair[1])
        first_pair = ordered[0]
        _require(
            first_pair[1] == source_step + 1,
            "gui_owl_first_action_exposure_not_immediate",
            "every accepted GUI-Owl action must first appear in the immediately later request",
            task_key=task_key,
            source_step=source_step,
            target_step=first_pair[1],
        )
        statuses = {
            exposure.get("action_record_alignment", {}).get("status")
            for pair in ordered
            for exposure in (exposures[pair][1],)
            if isinstance(exposure.get("action_record_alignment"), Mapping)
        }
        _require(
            len(statuses) == 1 and statuses <= {"match", "mismatch", "unresolved"},
            "gui_owl_action_alignment_inconsistent",
            "one GUI-Owl source action must have one stable mechanical alignment status",
            task_key=task_key,
            source_step=source_step,
            statuses=sorted(str(status) for status in statuses),
        )
        reasons_by_pair[first_pair].add(_GUI_OWL_ACTION_HISTORY_SIGNAL)
        if statuses == {"mismatch"}:
            reasons_by_pair[first_pair].add(_GUI_OWL_ACTION_MISMATCH_SIGNAL)
        selected_pairs.append(first_pair)
    return selected_pairs


def _formal_memgui_candidates(
    task_key: str,
    cores: Sequence[Mapping[str, Any]],
    scanner_signals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach shared signals to exact MemGUI entries without H/L/M collisions."""

    entries: dict[tuple[int, int, str], tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    by_pair: defaultdict[tuple[int, int], list[tuple[int, int, str]]] = defaultdict(list)
    by_source_entry: defaultdict[tuple[int, str], list[tuple[int, int, str]]] = defaultdict(list)
    by_target: defaultdict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for target_core in cores:
        target_step = target_core["step_index"]
        for exposure in target_core["assistant_exposures"]:
            _require(
                exposure.get("mapping_status") == "exact_memgui_structured_hlm"
                and exposure.get("representation_type") == "structured_folding",
                "memgui_formal_exposure_invalid",
                "MemGUI formal selection accepts only exact structured H/L/M exposures",
                task_key=task_key,
                target_step=target_step,
            )
            source_step = exposure["source_step_index"]
            entry_id = _string(exposure, "history_entry_id")
            key = (source_step, target_step, entry_id)
            _require(
                key not in entries,
                "memgui_exposure_entry_duplicate",
                "one MemGUI history entry may appear only once in one target request",
                task_key=task_key,
                source_step=source_step,
                target_step=target_step,
                history_entry_id=entry_id,
            )
            entries[key] = (target_core, exposure)
            by_pair[(source_step, target_step)].append(key)
            by_source_entry[(source_step, entry_id)].append(key)
            by_target[target_step].append(key)
    for values in (*by_pair.values(), *by_source_entry.values(), *by_target.values()):
        values.sort()

    reasons_by_entry: defaultdict[tuple[int, int, str], set[str]] = defaultdict(set)
    for appearances in by_source_entry.values():
        first_key = appearances[0]
        exposure = entries[first_key][1]
        if exposure["history_section"] == "M":
            reasons_by_entry[first_key].add("STRUCTURED_MEMORY_ENTRY")
        fold_range = exposure.get("fold_range")
        if (
            exposure["history_section"] == "H"
            and isinstance(fold_range, list)
            and len(fold_range) == 2
            and fold_range[0] < fold_range[1]
        ):
            reasons_by_entry[first_key].add("STRUCTURED_SPAN_FOLD")
    long_lag_entries: set[tuple[int, int, str]] = set()
    for signal in scanner_signals:
        signal_name = _string(signal, "signal")
        source_step = signal["source_step"]
        target_step = signal["target_step"]
        if signal_name == "LONG_LAG_IMAGE_ABSENT":
            details = signal.get("details")
            entry_id = details.get("history_entry_id") if isinstance(details, Mapping) else None
            if isinstance(entry_id, str):
                key = (source_step, target_step, entry_id)
                if key in entries:
                    long_lag_entries.add(key)
            else:
                long_lag_entries.update(by_pair.get((source_step, target_step), ()))
            continue

        attached: set[tuple[int, int, str]] = set()
        if signal_name == "PROGRESS_CLAIM":
            for (entry_source, entry_id), appearances in by_source_entry.items():
                if entry_source != source_step:
                    continue
                first_key = appearances[0]
                actor_text = entries[first_key][1].get("actor_claim_text")
                if isinstance(actor_text, str) and _memgui_has_progress_claim(actor_text):
                    attached.add(first_key)
        elif source_step < target_step and (source_step, target_step) in by_pair:
            attached.update(by_pair[(source_step, target_step)])
        elif source_step == target_step:
            attached.update(
                appearances[0]
                for (entry_source, _), appearances in by_source_entry.items()
                if entry_source == source_step
            )
        else:
            target_entries = by_target.get(target_step, [])
            if target_entries:
                latest_source = max(key[0] for key in target_entries)
                attached.update(key for key in target_entries if key[0] == latest_source)
        for key in attached:
            reasons_by_entry[key].add(signal_name)

    # Long lag is contextual support, never a standalone reason to create a
    # review card.  The full long-lag appearance ledger remains reconstructed.
    for key in set(reasons_by_entry) & long_lag_entries:
        reasons_by_entry[key].add("LONG_LAG_IMAGE_ABSENT")

    eligible = [
        key
        for key, reasons in reasons_by_entry.items()
        if isinstance(entries[key][1].get("actor_claim_text"), str)
        and bool(entries[key][1]["actor_claim_text"])
        and (
            bool(reasons & _HIGH_PRECISION_SIGNALS)
            or len(reasons & _STRUCTURAL_SIGNALS) >= 2
            or bool(reasons & _MEMGUI_REPRESENTATION_SIGNALS)
        )
    ]
    retained = _select_memgui_formal_candidate_entries(
        eligible,
        reasons_by_entry,
        entries,
        step_count=len(cores),
    )
    formal: list[dict[str, Any]] = []
    for source_step, _target_step, entry_id in retained:
        target_core, exposure = entries[(source_step, _target_step, entry_id)]
        source_steps = {source_step}
        if exposure.get("history_section") == "M":
            for field in (
                "memory_description_source_step",
                "memory_content_source_step",
            ):
                field_step = exposure.get(field)
                _require(
                    isinstance(field_step, int)
                    and not isinstance(field_step, bool)
                    and 1 <= field_step < target_core["step_index"],
                    "memgui_memory_field_source_invalid",
                    "MemGUI memory claim field lineage must precede the exposure",
                    task_key=task_key,
                    history_entry_id=entry_id,
                    field=field,
                    value=field_step,
                )
                source_steps.add(field_step)
        source_cores = [cores[step - 1] for step in sorted(source_steps)]
        formal.append(
            _formal_candidate(
                task_key=task_key,
                source_core=cores[source_step - 1],
                target_core=target_core,
                exposure=exposure,
                retrieval_reasons=sorted(reasons_by_entry[(source_step, _target_step, entry_id)]),
                claim_source_cores=source_cores,
            )
        )
    return sorted(formal, key=lambda candidate: candidate["candidate_id"])


def _memgui_has_progress_claim(text: str) -> bool:
    return any(pattern.search(text) is not None for _, pattern in _PROGRESS_CLAIM_PATTERNS)


def _select_memgui_formal_candidate_entries(
    eligible: Sequence[tuple[int, int, str]],
    reasons_by_entry: Mapping[tuple[int, int, str], set[str]],
    entries: Mapping[tuple[int, int, str], tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    step_count: int,
) -> list[tuple[int, int, str]]:
    """Bound MemGUI cards after shared eligibility while keeping the ledger whole."""

    tiers: defaultdict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for key in sorted(set(eligible)):
        tiers[_memgui_candidate_tier(reasons_by_entry[key])].append(key)
    selected: list[tuple[int, int, str]] = []
    for tier in sorted(tiers):
        remaining = MAX_MEMGUI_FORMAL_CANDIDATES_PER_TASK - len(selected)
        if remaining <= 0:
            break
        selected.extend(
            _select_memgui_tier_with_diversity(
                tiers[tier],
                reasons_by_entry,
                entries,
                step_count=step_count,
                limit=remaining,
            )
        )
    return sorted(selected)


def _memgui_candidate_tier(reasons: set[str]) -> int:
    structural_count = len(reasons & _STRUCTURAL_SIGNALS)
    if "STRUCTURED_MEMORY_ENTRY" in reasons:
        return 0
    if reasons & _MEMGUI_CRITICAL_SIGNALS:
        return 1
    if "STRUCTURED_SPAN_FOLD" in reasons:
        return 2
    if "PROGRESS_CLAIM" in reasons and structural_count >= 1:
        return 3
    if structural_count >= 2:
        return 4
    if "PROGRESS_CLAIM" in reasons:
        return 5
    return 6


def _select_memgui_tier_with_diversity(
    keys: Sequence[tuple[int, int, str]],
    reasons_by_entry: Mapping[tuple[int, int, str], set[str]],
    entries: Mapping[tuple[int, int, str], tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    step_count: int,
    limit: int,
) -> list[tuple[int, int, str]]:
    ordered = sorted(
        set(keys),
        key=lambda key: _memgui_entry_rank(key, reasons_by_entry, entries),
    )
    if len(ordered) <= limit:
        return ordered

    selected: list[tuple[int, int, str]] = []
    # First retain the best available M/H/L representative.  Within H, span
    # folds rank ahead of single-step summaries.
    for section in ("M", "H", "L"):
        members = [key for key in ordered if entries[key][1]["history_section"] == section]
        if members and len(selected) < limit:
            selected.append(members[0])
    selected_set = set(selected)
    # Then spread remaining slots across target-step temporal quartiles.
    for bucket in range(4):
        if len(selected) >= limit:
            break
        members = [
            key
            for key in ordered
            if key not in selected_set and min(3, ((key[1] - 1) * 4) // step_count) == bucket
        ]
        if members:
            selected.append(members[0])
            selected_set.add(members[0])
    if len(selected) < limit:
        selected.extend(key for key in ordered if key not in selected_set)
    return selected[:limit]


def _memgui_entry_rank(
    key: tuple[int, int, str],
    reasons_by_entry: Mapping[tuple[int, int, str], set[str]],
    entries: Mapping[tuple[int, int, str], tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> tuple[int, int, int, int, int, int, int, str]:
    reasons = reasons_by_entry[key]
    exposure = entries[key][1]
    section = exposure["history_section"]
    fold_range = exposure.get("fold_range")
    is_span_fold = (
        section == "H"
        and isinstance(fold_range, list)
        and len(fold_range) == 2
        and fold_range[0] < fold_range[1]
    )
    section_rank = {"M": 0, "H": 1, "L": 2}[section]
    return (
        section_rank,
        -int(is_span_fold),
        -len(reasons & _MEMGUI_CRITICAL_SIGNALS),
        -len(reasons & _STRUCTURAL_SIGNALS),
        -len(reasons),
        -(key[1] - key[0]),
        key[1],
        key[2],
    )


def _select_formal_candidate_pairs(
    reasons_by_pair: Mapping[tuple[int, int], set[str]], *, step_count: int
) -> list[tuple[int, int]]:
    """Keep semantic hits and a small, temporally spread structural sample.

    Repetition, static pixels, and near-duplicate reasoning are intentionally
    high-recall retrieval facts.  Any one of them alone is common in harmless
    GUI work, so a formal review candidate requires at least two independent
    structural signals.  All stronger textual/transition signals are retained.
    Structural pairs are capped per task to keep the 117-task review bounded;
    the complete trajectory and all assistant exposures remain in the
    reconstruction sidecar and can be rescanned deterministically.
    """

    high_precision: set[tuple[int, int]] = set()
    structural: list[tuple[int, int]] = []
    for pair, reasons in reasons_by_pair.items():
        if reasons & _HIGH_PRECISION_SIGNALS:
            high_precision.add(pair)
            continue
        if len(reasons & _STRUCTURAL_SIGNALS) >= 2:
            structural.append(pair)

    if len(structural) > MAX_STRUCTURAL_CANDIDATES_PER_TASK:
        bucket_count = MAX_STRUCTURAL_CANDIDATES_PER_TASK
        by_bucket: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
        for pair in structural:
            target_step = pair[1]
            bucket = min(bucket_count - 1, ((target_step - 1) * bucket_count) // step_count)
            by_bucket[bucket].append(pair)

        selected: list[tuple[int, int]] = []
        for bucket in range(bucket_count):
            members = by_bucket.get(bucket, [])
            if members:
                selected.append(
                    min(members, key=lambda pair: _structural_pair_rank(pair, reasons_by_pair))
                )
        if len(selected) < bucket_count:
            remaining = sorted(
                (pair for pair in structural if pair not in set(selected)),
                key=lambda pair: _structural_pair_rank(pair, reasons_by_pair),
            )
            selected.extend(remaining[: bucket_count - len(selected)])
        structural = selected

    return sorted(high_precision | set(structural))


def _select_gelab_formal_candidate_pairs(
    eligible_pairs: Sequence[tuple[int, int]],
    reasons_by_pair: Mapping[tuple[int, int], set[str]],
    *,
    step_count: int,
) -> list[tuple[int, int]]:
    """Bound GELab review pairs after applying the shared eligibility rule.

    Higher-priority tiers are exhausted before a lower tier can consume a
    slot.  When one tier overflows the remaining budget, selection is spread
    across target-step temporal quartiles within that tier.  The complete
    rolling-summary exposure ledger remains untouched in the reconstruction.
    """

    tiers: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair in sorted(set(eligible_pairs)):
        tiers[_gelab_candidate_tier(reasons_by_pair[pair])].append(pair)

    selected: list[tuple[int, int]] = []
    for tier in sorted(tiers):
        remaining = MAX_GELAB_FORMAL_CANDIDATES_PER_TASK - len(selected)
        if remaining <= 0:
            break
        selected.extend(
            _select_gelab_tier_with_temporal_coverage(
                tiers[tier],
                reasons_by_pair,
                step_count=step_count,
                limit=remaining,
            )
        )
    return sorted(selected)


def _select_gui_owl_formal_candidate_pairs(
    eligible_pairs: Sequence[tuple[int, int]],
    reasons_by_pair: Mapping[tuple[int, int], set[str]],
    *,
    step_count: int,
) -> list[tuple[int, int]]:
    """Keep every mechanical mismatch, then fill the ordinary four-card budget."""

    mismatch_pairs = sorted(
        {
            pair
            for pair in eligible_pairs
            if _GUI_OWL_ACTION_MISMATCH_SIGNAL in reasons_by_pair[pair]
        }
    )
    if len(mismatch_pairs) >= MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK:
        return mismatch_pairs

    tiers: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair in sorted(set(eligible_pairs)):
        if pair in mismatch_pairs:
            continue
        tiers[_gui_owl_candidate_tier(reasons_by_pair[pair])].append(pair)

    selected: list[tuple[int, int]] = list(mismatch_pairs)
    for tier in sorted(tiers):
        remaining = MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK - len(selected)
        if remaining <= 0:
            break
        selected.extend(
            _select_gui_owl_tier_with_temporal_coverage(
                tiers[tier],
                reasons_by_pair,
                step_count=step_count,
                limit=remaining,
            )
        )
    return sorted(selected)


def _gui_owl_candidate_tier(reasons: set[str]) -> int:
    structural_count = len(reasons & _STRUCTURAL_SIGNALS)
    if _GUI_OWL_ACTION_MISMATCH_SIGNAL in reasons:
        return 0
    if reasons & _GUI_OWL_CRITICAL_SIGNALS:
        return 1
    if "PROGRESS_CLAIM" in reasons and structural_count >= 1:
        return 2
    if structural_count >= 2:
        return 3
    if "PROGRESS_CLAIM" in reasons:
        return 4
    if _GUI_OWL_ACTION_HISTORY_SIGNAL in reasons:
        return 5
    return 6


def _select_gui_owl_tier_with_temporal_coverage(
    pairs: Sequence[tuple[int, int]],
    reasons_by_pair: Mapping[tuple[int, int], set[str]],
    *,
    step_count: int,
    limit: int,
) -> list[tuple[int, int]]:
    ordered = sorted(
        set(pairs),
        key=lambda pair: _gui_owl_pair_rank(pair, reasons_by_pair),
    )
    if len(ordered) <= limit:
        return ordered
    if limit == 1:
        return ordered[:1]

    by_bucket: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair in ordered:
        by_bucket[_gelab_temporal_bucket(pair, step_count=step_count)].append(pair)
    representatives = [
        min(members, key=lambda pair: _gui_owl_pair_rank(pair, reasons_by_pair))
        for _, members in sorted(by_bucket.items())
    ]
    if len(representatives) > limit:
        indices = _evenly_spaced_indices(len(representatives), limit)
        selected = [representatives[index] for index in indices]
    else:
        selected = list(representatives)
    selected_set = set(selected)
    selected.extend(pair for pair in ordered if pair not in selected_set)
    return selected[:limit]


def _gui_owl_pair_rank(
    pair: tuple[int, int], reasons_by_pair: Mapping[tuple[int, int], set[str]]
) -> tuple[int, int, int, int, int, int, int]:
    reasons = reasons_by_pair[pair]
    return (
        -len(reasons & _GUI_OWL_CRITICAL_SIGNALS),
        -len(reasons & _STRUCTURAL_SIGNALS),
        -len(reasons),
        -int("LONG_LAG_IMAGE_ABSENT" in reasons),
        -(pair[1] - pair[0]),
        pair[1],
        pair[0],
    )


def _gelab_candidate_tier(reasons: set[str]) -> int:
    structural_count = len(reasons & _STRUCTURAL_SIGNALS)
    if reasons & _GELAB_CRITICAL_SIGNALS:
        return 0
    if "PROGRESS_CLAIM" in reasons and structural_count >= 1:
        return 1
    if structural_count >= 2:
        return 2
    if "PROGRESS_CLAIM" in reasons:
        return 3
    return 4


def _select_gelab_tier_with_temporal_coverage(
    pairs: Sequence[tuple[int, int]],
    reasons_by_pair: Mapping[tuple[int, int], set[str]],
    *,
    step_count: int,
    limit: int,
) -> list[tuple[int, int]]:
    ordered = sorted(
        set(pairs),
        key=lambda pair: _gelab_pair_rank(pair, reasons_by_pair),
    )
    if len(ordered) <= limit:
        return ordered
    if limit == 1:
        return ordered[:1]

    by_bucket: defaultdict[int, list[tuple[int, int]]] = defaultdict(list)
    for pair in ordered:
        by_bucket[_gelab_temporal_bucket(pair, step_count=step_count)].append(pair)
    representatives = [
        min(members, key=lambda pair: _gelab_pair_rank(pair, reasons_by_pair))
        for _, members in sorted(by_bucket.items())
    ]
    if len(representatives) > limit:
        representative_indices = _evenly_spaced_indices(len(representatives), limit)
        selected = [representatives[index] for index in representative_indices]
    else:
        selected = list(representatives)

    selected_set = set(selected)
    selected.extend(pair for pair in ordered if pair not in selected_set)
    return selected[:limit]


def _gelab_temporal_bucket(pair: tuple[int, int], *, step_count: int) -> int:
    return min(3, ((pair[1] - 1) * 4) // step_count)


def _evenly_spaced_indices(item_count: int, selected_count: int) -> list[int]:
    _require(
        1 < selected_count <= item_count,
        "temporal_selection_cardinality_invalid",
        "temporal selection requires between two and all available items",
        item_count=item_count,
        selected_count=selected_count,
    )
    return [
        (index * (item_count - 1) + (selected_count - 1) // 2) // (selected_count - 1)
        for index in range(selected_count)
    ]


def _gelab_pair_rank(
    pair: tuple[int, int], reasons_by_pair: Mapping[tuple[int, int], set[str]]
) -> tuple[int, int, int, int, int, int, int]:
    reasons = reasons_by_pair[pair]
    return (
        -len(reasons & _GELAB_CRITICAL_SIGNALS),
        -len(reasons & _STRUCTURAL_SIGNALS),
        -len(reasons),
        -int("LONG_LAG_IMAGE_ABSENT" in reasons),
        -(pair[1] - pair[0]),
        pair[1],
        pair[0],
    )


def _structural_pair_rank(
    pair: tuple[int, int], reasons_by_pair: Mapping[tuple[int, int], set[str]]
) -> tuple[int, int, int, int, int]:
    reasons = reasons_by_pair[pair]
    return (
        -len(reasons & _STRUCTURAL_SIGNALS),
        -int("LONG_LAG_IMAGE_ABSENT" in reasons),
        -(pair[1] - pair[0]),
        pair[1],
        pair[0],
    )


def _formal_candidate(
    *,
    task_key: str,
    source_core: Mapping[str, Any],
    target_core: Mapping[str, Any],
    exposure: Mapping[str, Any],
    retrieval_reasons: Sequence[str],
    claim_source_cores: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    source_step = source_core["step_index"]
    target_step = target_core["step_index"]
    message_index = exposure["message_index"]
    content_block_index = exposure.get("content_block_index")
    representation_type = exposure.get("representation_type", "raw_replay")
    additional_target_request_spans: list[tuple[str, str]] = []
    if representation_type == "flat_progress":
        claim_value = exposure["assistant_conclusion_text"]
        exposure_span_value = exposure["exposed_text"]
        span_start = exposure["span_start"]
        span_end = exposure["span_end"]
        request_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{span_start}:{span_end}]"
        )
        target_request_excerpt = exposure["exposed_text"]
    elif representation_type == "flat_previous_actions":
        claim_value = exposure["assistant_history_text"]
        exposure_span_value = exposure["exposed_text"]
        span_start = exposure["span_start"]
        span_end = exposure["span_end"]
        request_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{span_start}:{span_end}]"
        )
        target_request_excerpt = exposure["exposed_text"]
    elif representation_type == "rolling_summary":
        claim_value = exposure["assistant_summary_text"]
        exposure_span_value = exposure["exposed_text"]
        span_start = exposure["span_start"]
        span_end = exposure["span_end"]
        request_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{span_start}:{span_end}]"
        )
        target_request_excerpt = exposure["exposed_text"]
    elif representation_type == "hybrid_folding":
        claim_value = exposure["assistant_conclusion_text"]
        exposure_span_value = exposure["rendered_conclusion_text"]
        span_start = exposure["assistant_span_start"]
        span_end = exposure["assistant_span_end"]
        request_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{span_start}:{span_end}]"
        )
        target_request_excerpt = exposure["rendered_conclusion_text"]
        result_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{exposure['result_span_start']}:"
            f"{exposure['result_span_end']}]"
        )
        additional_target_request_spans.append((result_path, exposure["aligned_result_text"]))
    elif representation_type == "structured_folding":
        _require(
            exposure.get("mapping_status") == "exact_memgui_structured_hlm",
            "memgui_formal_mapping_status_invalid",
            "structured_folding candidates require exact MemGUI H/L/M mapping",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
        )
        claim_value = exposure["actor_claim_text"]
        _require(
            isinstance(claim_value, str) and bool(claim_value),
            "memgui_formal_actor_claim_empty",
            "empty MemGUI actor text may remain in the ledger but cannot become a review claim",
            task_key=task_key,
            source_step=source_step,
            target_step=target_step,
            history_entry_id=exposure.get("history_entry_id"),
        )
        exposure_span_value = exposure["exposed_text"]
        span_start = exposure["span_start"]
        span_end = exposure["span_end"]
        request_path = (
            f"payload.request_view.messages[{message_index}].content"
            f"[{content_block_index}].text[{span_start}:{span_end}]"
        )
        target_request_excerpt = exposure["exposed_text"]
    else:
        claim_value = source_core.get("prediction")
        exposure_span_value = claim_value
        request_path = f"payload.request_view.messages[{message_index}].content"
        if content_block_index is not None:
            request_path += f"[{content_block_index}].text"
        target_request_excerpt = f"exact assistant span from source step {source_step}"
    candidate_identity = {
        "task_key": task_key,
        "source_step": source_step,
        "target_step": target_step,
        "message_index": message_index,
    }
    if representation_type == "structured_folding":
        candidate_identity.update(
            {
                "history_entry_id": exposure["history_entry_id"],
                "span_start": exposure["span_start"],
                "span_end": exposure["span_end"],
            }
        )
    candidate_id = _stable_id(
        "candidate",
        candidate_identity,
    )
    resolved_source_cores = list(claim_source_cores or (source_core,))
    source_cores_by_step = {core["step_index"]: core for core in resolved_source_cores}
    source_cores_by_step[source_step] = source_core
    claim_source_steps = sorted(source_cores_by_step)
    evidence_refs = _candidate_evidence_refs(
        task_key=task_key,
        candidate_id=candidate_id,
        source_core=source_core,
        target_core=target_core,
        request_path=request_path,
        target_request_excerpt=target_request_excerpt,
        additional_target_request_spans=additional_target_request_spans,
    )
    if representation_type == "hybrid_folding" and isinstance(
        source_core.get("execution"), Mapping
    ):
        execution = source_core["execution"]
        execution_payload = _mapping(execution, "payload")
        evidence_refs.append(
            _evidence_ref(
                task_key,
                candidate_id,
                "source_action",
                _string(execution, "event_id"),
                source_step,
                "payload.action",
                None,
                _compact_json_excerpt(execution_payload.get("action")),
            )
        )
    for additional_step, additional_core in source_cores_by_step.items():
        if additional_step != source_step:
            evidence_refs.extend(
                _additional_source_evidence_refs(
                    task_key=task_key,
                    candidate_id=candidate_id,
                    source_core=additional_core,
                )
            )
    evidence_refs.sort(key=lambda ref: ref["ref_id"])
    return {
        "candidate_id": candidate_id,
        "retrieval_reasons": sorted(set(retrieval_reasons)),
        "claim": {
            "text": (
                claim_value
                if representation_type == "structured_folding"
                else _claim_text(claim_value)
            ),
            "claim_type": (
                _memgui_claim_type(exposure, retrieval_reasons)
                if representation_type == "structured_folding"
                else _gui_owl_claim_type(claim_value, retrieval_reasons)
                if representation_type == "hybrid_folding"
                else _claim_type(claim_value, retrieval_reasons)
            ),
            "source_steps": claim_source_steps,
            "representation_type": representation_type,
            "provenance_confidence": "EXACT",
        },
        "exposure": {
            "target_step": target_step,
            "request_path": request_path,
            "was_actually_in_request": True,
            "span_sha256": _span_sha256(exposure_span_value),
        },
        "evidence_refs": evidence_refs,
    }


def _memgui_claim_type(exposure: Mapping[str, Any], retrieval_reasons: Sequence[str]) -> str:
    """Type structured actor text by H/L/M field without changing shared enums."""

    if "PROGRESS_CLAIM" in retrieval_reasons:
        return "SUCCESS_CLAIM"
    section = exposure.get("history_section")
    if section == "H":
        return "SUMMARY_CLAIM"
    if section == "M":
        return "OBSERVATION_CLAIM"
    _require(
        section == "L",
        "memgui_history_section_invalid",
        "structured MemGUI candidate must identify H, L, or M",
        history_section=section,
    )
    if exposure.get("latest_ui_observation_text"):
        return "OBSERVATION_CLAIM"
    return "ACTION_INTENT"


def _additional_source_evidence_refs(
    *,
    task_key: str,
    candidate_id: str,
    source_core: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Cite an inherited MemGUI memory field's earlier actor decision."""

    source_step = source_core["step_index"]
    source_decision_payload = _mapping(source_core["decision"], "payload")
    return [
        _evidence_ref(
            task_key,
            candidate_id,
            "source_pre",
            source_core["step_started"]["event_id"],
            source_step,
            "payload.observation.screenshot.pixel_blob",
            _observation_screenshot_digest(source_core.get("pre_observation")),
            _ui_excerpt(source_core.get("pre_observation")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_prediction",
            source_core["decision"]["event_id"],
            source_step,
            "payload.prediction_raw",
            _blob_digest(source_decision_payload.get("prediction_snapshot_blob")),
            _claim_text(source_core.get("prediction")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_action",
            source_core["decision"]["event_id"],
            source_step,
            "payload.parsed_action.value",
            None,
            _compact_json_excerpt(source_core.get("action")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_result",
            source_core["transition"]["event_id"],
            source_step,
            "payload.execution_result",
            _nested_blob_digest(
                _mapping(source_core["transition"], "payload").get("execution_result")
            ),
            _compact_json_excerpt(
                _mapping(source_core["transition"], "payload").get("execution_result")
            ),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_post",
            source_core["transition"]["event_id"],
            source_step,
            "payload.post_observation.screenshot.pixel_blob",
            _observation_screenshot_digest(source_core.get("post_observation")),
            _ui_excerpt(source_core.get("post_observation")),
        ),
    ]


def _candidate_evidence_refs(
    *,
    task_key: str,
    candidate_id: str,
    source_core: Mapping[str, Any],
    target_core: Mapping[str, Any],
    request_path: str,
    target_request_excerpt: str,
    additional_target_request_spans: Sequence[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    source_step = source_core["step_index"]
    target_step = target_core["step_index"]
    source_decision_payload = _mapping(source_core["decision"], "payload")
    target_decision_payload = _mapping(target_core["decision"], "payload")
    target_request_payload = _mapping(target_core["selected_request"], "payload")
    refs = [
        _evidence_ref(
            task_key,
            candidate_id,
            "source_pre",
            source_core["step_started"]["event_id"],
            source_step,
            "payload.observation.screenshot.pixel_blob",
            _observation_screenshot_digest(source_core.get("pre_observation")),
            _ui_excerpt(source_core.get("pre_observation")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_prediction",
            source_core["decision"]["event_id"],
            source_step,
            "payload.prediction_raw",
            _blob_digest(source_decision_payload.get("prediction_snapshot_blob")),
            _claim_text(source_core.get("prediction")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_action",
            source_core["decision"]["event_id"],
            source_step,
            "payload.parsed_action.value",
            None,
            _compact_json_excerpt(source_core.get("action")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_result",
            source_core["transition"]["event_id"],
            source_step,
            "payload.execution_result",
            _nested_blob_digest(
                _mapping(source_core["transition"], "payload").get("execution_result")
            ),
            _compact_json_excerpt(
                _mapping(source_core["transition"], "payload").get("execution_result")
            ),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "source_post",
            source_core["transition"]["event_id"],
            source_step,
            "payload.post_observation.screenshot.pixel_blob",
            _observation_screenshot_digest(source_core.get("post_observation")),
            _ui_excerpt(source_core.get("post_observation")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "target_pre",
            target_core["step_started"]["event_id"],
            target_step,
            "payload.observation.screenshot.pixel_blob",
            _observation_screenshot_digest(target_core.get("pre_observation")),
            _ui_excerpt(target_core.get("pre_observation")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "target_request",
            target_core["selected_request"]["event_id"],
            target_step,
            request_path,
            _blob_digest(target_request_payload.get("sdk_arguments_snapshot_blob")),
            target_request_excerpt,
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "target_prediction",
            target_core["decision"]["event_id"],
            target_step,
            "payload.prediction_raw",
            _blob_digest(target_decision_payload.get("prediction_snapshot_blob")),
            _claim_text(target_core.get("prediction")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "target_action",
            target_core["decision"]["event_id"],
            target_step,
            "payload.parsed_action.value",
            None,
            _compact_json_excerpt(target_core.get("action")),
        ),
        _evidence_ref(
            task_key,
            candidate_id,
            "target_post",
            target_core["transition"]["event_id"],
            target_step,
            "payload.post_observation.screenshot.pixel_blob",
            _observation_screenshot_digest(target_core.get("post_observation")),
            _ui_excerpt(target_core.get("post_observation")),
        ),
    ]
    refs.extend(
        _evidence_ref(
            task_key,
            candidate_id,
            "target_request",
            target_core["selected_request"]["event_id"],
            target_step,
            field_path,
            _blob_digest(target_request_payload.get("sdk_arguments_snapshot_blob")),
            excerpt,
        )
        for field_path, excerpt in additional_target_request_spans
    )
    return sorted(refs, key=lambda ref: ref["ref_id"])


def _evidence_ref(
    task_key: str,
    candidate_id: str,
    role: str,
    event_id: str,
    step: int,
    field_path: str,
    blob_sha256: str | None,
    excerpt: str,
) -> dict[str, Any]:
    ref_id = _stable_id(
        "evidence",
        {
            "task_key": task_key,
            "candidate_id": candidate_id,
            "role": role,
            "event_id": event_id,
            "field_path": field_path,
        },
    )
    return {
        "ref_id": ref_id,
        "role": role,
        "event_id": event_id,
        "step": step,
        "field_path": field_path,
        "blob_sha256": blob_sha256,
        "excerpt": _excerpt(excerpt, maximum=360),
    }


def _claim_type(prediction: Any, retrieval_reasons: Sequence[str]) -> str:
    reasons = set(retrieval_reasons)
    if "PROGRESS_CLAIM" in reasons:
        return "SUCCESS_CLAIM"
    text = _thinking_text(prediction).casefold()
    if any(word in text for word in ("i will", "next", "plan", "need to")):
        return "PLAN"
    if any(word in text for word in ("click", "tap", "type", "scroll", "select")):
        return "ACTION_INTENT"
    if "summary" in text:
        return "SUMMARY_CLAIM"
    return "OBSERVATION_CLAIM"


def _gui_owl_claim_type(action_text: Any, retrieval_reasons: Sequence[str]) -> str:
    """Type the collapsed Action record from its adapter role, not verb language."""

    if "PROGRESS_CLAIM" in retrieval_reasons:
        return "SUCCESS_CLAIM"
    text = action_text if isinstance(action_text, str) else ""
    if _GUI_OWL_PROSPECTIVE_ACTION_RE.search(_SPACE_RE.sub(" ", text).strip()) is not None:
        return "ACTION_INTENT"
    return "ACTION_EXECUTION_CLAIM"


def _claim_text(value: Any) -> str:
    text = value if isinstance(value, str) else _compact_json_excerpt(value)
    return _excerpt(text, maximum=800) or "<empty assistant message>"


def _span_sha256(value: Any) -> str:
    if isinstance(value, str):
        return _sha256(value.encode("utf-8"))
    return _stable_digest(value)


def _nullable_excerpt(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else _compact_json_excerpt(value)
    return _excerpt(text, maximum=320)


def _nullable_compact_json(value: Any) -> str | None:
    if value is None:
        return None
    return _compact_json_excerpt(value, maximum=320)


def _compact_json_excerpt(value: Any, *, maximum: int = 360) -> str:
    text = canonical_json_bytes(value, newline=False).decode("utf-8")
    return _excerpt(text, maximum=maximum)


def _excerpt(text: str, *, maximum: int) -> str:
    normalized = _SPACE_RE.sub(" ", text).strip()
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 1] + "…"


def _clean_nonempty_text(value: str) -> str:
    cleaned = value.strip()
    _require(cleaned != "", "text_empty", "text is empty after removing edge whitespace")
    return cleaned


def _ui_excerpt(observation: Any) -> str:
    if not isinstance(observation, Mapping):
        return "observation unavailable"
    digest = _observation_screenshot_digest(observation)
    ask = observation.get("ask_user_response")
    tool = observation.get("tool_call")
    return _compact_json_excerpt(
        {"screenshot_digest": digest, "ask_user_response": ask, "tool_call": tool}
    )


def _ui_delta(core: Mapping[str, Any]) -> str | None:
    before = _observation_screenshot_digest(core.get("pre_observation"))
    after = _observation_screenshot_digest(core.get("post_observation"))
    if before is None or after is None:
        return None
    return "screenshot_static" if before == after else "screenshot_changed"


def _blob_digest(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    digest = value.get("digest")
    return digest if isinstance(digest, str) else None


def _nested_blob_digest(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in (
        "agent_visible_tool_result_snapshot_blob",
        "response_body_blob",
        "raw_tool_result_blob",
        "request_body_snapshot_blob",
    ):
        digest = _blob_digest(value.get(key))
        if digest is not None:
            return digest
    return None


def _scan_candidates(
    task_index: int,
    task_name: str,
    cores: Sequence[Mapping[str, Any]],
    *,
    textual_claims_by_step: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Recall deterministic structural/textual candidates without judging them."""

    candidates: list[dict[str, Any]] = []
    reasoning_tokens: list[set[str]] = []
    action_signatures: list[Any] = []
    for position, core in enumerate(cores):
        step = core["step_index"]
        reasoning_text = _thinking_text(core.get("prediction"))
        source_claim_text = (
            textual_claims_by_step.get(step, "")
            if textual_claims_by_step is not None
            else reasoning_text
        )
        tokens = _content_tokens(reasoning_text)
        reasoning_tokens.append(tokens)

        for pattern_name, pattern in _SELF_CORRECTION_PATTERNS:
            match = pattern.search(reasoning_text)
            if match is not None:
                candidates.append(
                    _candidate_without_id(
                        task_index,
                        task_name,
                        "SELF_CORRECTION",
                        source_step=max(1, step - 1),
                        target_step=step,
                        evidence_step=step,
                        details={
                            "pattern": pattern_name,
                            "matched_text_sha256": _sha256(match.group(0).encode("utf-8")),
                        },
                    )
                )

        for pattern_name, pattern in _FAILED_TRANSITION_PATTERNS:
            match = pattern.search(reasoning_text)
            if match is not None:
                candidates.append(
                    _candidate_without_id(
                        task_index,
                        task_name,
                        "FAILED_TRANSITION_ACK",
                        source_step=max(1, step - 1),
                        target_step=step,
                        evidence_step=step,
                        details={
                            "pattern": pattern_name,
                            "matched_text_sha256": _sha256(match.group(0).encode("utf-8")),
                            "previous_transition_type": (
                                cores[position - 1]["transition"]["event_type"]
                                if position > 0
                                else None
                            ),
                        },
                    )
                )

        for pattern_name, pattern in _PROGRESS_CLAIM_PATTERNS:
            match = pattern.search(source_claim_text)
            if match is not None:
                candidates.append(
                    _candidate_without_id(
                        task_index,
                        task_name,
                        "PROGRESS_CLAIM",
                        source_step=step,
                        target_step=step,
                        evidence_step=step,
                        details={
                            "pattern": pattern_name,
                            "matched_text_sha256": _sha256(match.group(0).encode("utf-8")),
                        },
                    )
                )

        transition_type = core["transition"]["event_type"]
        if transition_type == "transition_failed":
            candidates.append(
                _candidate_without_id(
                    task_index,
                    task_name,
                    "FAILED_TRANSITION",
                    source_step=step,
                    target_step=step,
                    evidence_step=step,
                    details={"transition_type": transition_type},
                )
            )

        signature = _action_signature(core.get("action"))
        action_signatures.append(signature)
        if signature is not None:
            lower = max(0, position - REPEATED_ACTION_LOOKBACK)
            for prior_position in range(position - 1, lower - 1, -1):
                if action_signatures[prior_position] == signature:
                    candidates.append(
                        _candidate_without_id(
                            task_index,
                            task_name,
                            "REPEATED_ACTION",
                            source_step=cores[prior_position]["step_index"],
                            target_step=step,
                            evidence_step=step,
                            details={
                                "lag": position - prior_position,
                                "normalized_action_sha256": _stable_digest(signature),
                            },
                        )
                    )
                    break

        pre_digest = _observation_screenshot_digest(core.get("pre_observation"))
        post_digest = _observation_screenshot_digest(core.get("post_observation"))
        if pre_digest is not None and pre_digest == post_digest:
            candidates.append(
                _candidate_without_id(
                    task_index,
                    task_name,
                    "STATIC_TRANSITION",
                    source_step=step,
                    target_step=step,
                    evidence_step=step,
                    details={
                        "screenshot_digest": pre_digest,
                        "transition_type": transition_type,
                        "expected_static_action": _expected_static_action(core.get("action")),
                    },
                )
            )

        if len(tokens) >= NEAR_REASONING_MIN_TOKENS:
            lower = max(0, position - NEAR_REASONING_LOOKBACK)
            for prior_position in range(position - 1, lower - 1, -1):
                prior = reasoning_tokens[prior_position]
                if len(prior) < NEAR_REASONING_MIN_TOKENS:
                    continue
                similarity = _jaccard(tokens, prior)
                if similarity >= NEAR_REASONING_JACCARD_THRESHOLD:
                    candidates.append(
                        _candidate_without_id(
                            task_index,
                            task_name,
                            "NEAR_DUPLICATE_REASONING",
                            source_step=cores[prior_position]["step_index"],
                            target_step=step,
                            evidence_step=step,
                            details={
                                "jaccard": round(similarity, 6),
                                "source_token_count": len(prior),
                                "target_token_count": len(tokens),
                            },
                        )
                    )
                    break

        comparison = core["provider_decision_comparison"]
        if comparison["status"] not in {
            "exact_match",
            "both_missing",
            "edge_whitespace_only",
        }:
            candidates.append(
                _candidate_without_id(
                    task_index,
                    task_name,
                    "PROVIDER_DECISION_DIFFERENCE",
                    source_step=step,
                    target_step=step,
                    evidence_step=step,
                    details={"status": comparison["status"]},
                )
            )
    return candidates


def _candidate_without_id(
    task_index: int,
    task_name: str,
    signal: str,
    *,
    source_step: int,
    target_step: int,
    evidence_step: int,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_semantics": "retrieval_only_not_an_evaluation_label",
        "canonical_suite_index": task_index,
        "task_name": task_name,
        "signal": signal,
        "source_step": source_step,
        "target_step": target_step,
        "evidence_step": evidence_step,
        "details": _json_clone(details),
    }


def _thinking_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    matches = _THINK_RE.findall(value)
    selected = "\n".join(matches) if matches else _TOOL_BLOCK_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", _TAG_RE.sub(" ", selected)).strip()


def _content_tokens(text: str) -> set[str]:
    normalized = _NUMBER_RE.sub(" <number> ", text.casefold())
    return {
        token
        for token in _TOKEN_RE.findall(normalized)
        if token not in _CONTENT_STOPWORDS and len(token) > 1
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _action_signature(action: Any) -> Any:
    if action is None:
        return None
    if isinstance(action, Mapping):
        return {
            str(key): _action_signature_value(str(key), value)
            for key, value in sorted(action.items(), key=lambda item: str(item[0]))
        }
    if isinstance(action, (list, tuple)):
        return [_action_signature_value(str(index), value) for index, value in enumerate(action)]
    return _action_signature_value("value", action)


def _action_signature_value(key: str, value: Any) -> Any:
    coordinate_key = key.casefold() in {"x", "y", "start_x", "start_y", "end_x", "end_y"}
    if coordinate_key and isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value // ACTION_GRID_PX)
    if isinstance(value, Mapping):
        return {
            str(child_key): _action_signature_value(str(child_key), child_value)
            for child_key, child_value in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_action_signature_value(str(index), item) for index, item in enumerate(value)]
    if isinstance(value, str):
        return _SPACE_RE.sub(" ", value.casefold()).strip()
    return value


def _expected_static_action(action: Any) -> bool:
    text = json.dumps(action, ensure_ascii=False, sort_keys=True).casefold()
    return any(name in text for name in ("wait", "ask_user", "type", "input_text"))


def _observation_screenshot_digest(observation: Any) -> str | None:
    if not isinstance(observation, Mapping):
        return None
    screenshot = observation.get("screenshot")
    if not isinstance(screenshot, Mapping):
        return None
    pixel_blob = screenshot.get("pixel_blob")
    if isinstance(pixel_blob, Mapping) and isinstance(pixel_blob.get("digest"), str):
        return pixel_blob["digest"]
    if isinstance(screenshot.get("digest"), str):
        return screenshot["digest"]
    return None


def _request_image_digest(image: Any) -> str | None:
    if not isinstance(image, Mapping):
        return None
    content_blob = image.get("content_blob")
    if isinstance(content_blob, Mapping) and isinstance(content_blob.get("digest"), str):
        return content_blob["digest"]
    if isinstance(image.get("digest"), str):
        return image["digest"]
    return None


def _value_summary(value: Any) -> dict[str, Any]:
    encoded = canonical_json_bytes(value, newline=False)
    summary: dict[str, Any] = {
        "json_type": _json_type(value),
        "sha256": _sha256(encoded),
        "utf8_byte_count": len(encoded),
    }
    if isinstance(value, str):
        summary["character_count"] = len(value)
    elif isinstance(value, (list, Mapping)):
        summary["item_count"] = len(value)
    return summary


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return type(value).__name__


def _resolve_sources(
    manifest: Mapping[str, Any], base: Path
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Path]]:
    sources = manifest.get("sources")
    _require(isinstance(sources, list) and sources, "sources_invalid", "sources must be a list")
    entries: dict[str, Mapping[str, Any]] = {}
    roots: dict[str, Path] = {}
    for item in sources:
        _require(isinstance(item, Mapping), "source_invalid", "source entry must be an object")
        source_id = _string(item, "source_id")
        _require(source_id not in entries, "source_id_duplicate", "source ID is duplicated")
        relative = _safe_relative_path(
            _string(item, "relative_run_path"), code="source_run_path_invalid"
        )
        try:
            root = base.joinpath(*relative.parts).resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise MotivationCardError(
                "source_run_missing", "referenced source run is unavailable", source_id=source_id
            ) from error
        _require(
            root.is_dir() and _is_within(root, base),
            "source_run_invalid",
            "referenced source must be a directory beneath source_base",
            source_id=source_id,
        )
        entries[source_id] = item
        roots[source_id] = root
    return entries, roots


def _read_verified_blob_ref(run_root: Path, reference: Mapping[str, Any]) -> bytes:
    """Read one source-local BlobRef and verify its complete content identity."""

    _require(
        reference.get("algorithm") == "sha256",
        "ui_venus_blob_algorithm_invalid",
        "UI-Venus image BlobRef must use SHA-256",
    )
    digest = _string(reference, "digest")
    _require(
        re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
        "ui_venus_blob_digest_invalid",
        "UI-Venus image BlobRef digest must be canonical SHA-256",
    )
    expected_length = _positive_int(reference, "byte_length", allow_zero=True)
    relative = _safe_relative_path(
        _string(reference, "relative_path"),
        code="ui_venus_blob_path_invalid",
    )
    expected_relative = PurePosixPath("blobs", "sha256", digest[:2], digest)
    _require(
        relative == expected_relative,
        "ui_venus_blob_path_digest_mismatch",
        "UI-Venus image BlobRef path must match its digest",
        digest=digest,
    )
    try:
        path = run_root.joinpath(*relative.parts).resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise MotivationCardError(
            "ui_venus_blob_missing",
            "UI-Venus image BlobRef does not resolve beneath its source run",
            digest=digest,
        ) from error
    _require(
        path.is_file() and _is_within(path, run_root),
        "ui_venus_blob_path_escape",
        "UI-Venus image BlobRef must resolve to a source-run file",
        digest=digest,
    )
    data = path.read_bytes()
    _require(
        len(data) == expected_length and _sha256(data) == digest,
        "ui_venus_blob_content_mismatch",
        "UI-Venus image blob bytes do not match the referenced identity",
        digest=digest,
        expected_byte_length=expected_length,
        actual_byte_length=len(data),
    )
    return data


def _loads_object(data: bytes, path: Path) -> dict[str, Any]:
    value = _loads_strict(data, path)
    _require(isinstance(value, dict), "json_object_required", "JSON document must be an object")
    return value


def _jsonl_documents(data: bytes, path: Path) -> list[dict[str, Any]]:
    lines = data.splitlines()
    _require(lines, "jsonl_empty", "JSONL task stream must not be empty", path=str(path))
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        _require(line, "jsonl_blank_line", "JSONL task stream contains a blank line")
        value = _loads_strict(line, path, line_number=line_number)
        _require(
            isinstance(value, dict),
            "jsonl_object_required",
            "each JSONL record must be an object",
            line_number=line_number,
        )
        result.append(value)
    return result


def _loads_strict(data: bytes, path: Path, *, line_number: int | None = None) -> Any:
    try:
        return json.loads(
            data,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_raise_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError) as error:
        raise MotivationCardError(
            "json_invalid",
            "input contains invalid or non-strict JSON",
            path=str(path),
            line_number=line_number,
            error=str(error),
        ) from error


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raise_nonfinite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _safe_relative_path(value: str, *, code: str) -> PurePosixPath:
    path = PurePosixPath(value)
    _require(
        bool(value)
        and not path.is_absolute()
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in path.parts),
        code,
        "path must be a normalized relative POSIX path",
        path=value,
    )
    return path


def _require_file_summary(summary: Mapping[str, Any], data: bytes, path: Path) -> None:
    expected_size = _positive_int(summary, "byte_count", allow_zero=True)
    expected_digest = _string(summary, "sha256")
    _require(
        len(data) == expected_size,
        "task_stream_size_mismatch",
        "task stream byte count differs from curated manifest",
        path=str(path),
    )
    _require(
        _sha256(data) == expected_digest,
        "task_stream_digest_mismatch",
        "task stream digest differs from curated manifest",
        path=str(path),
    )


def _mapping(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    child = value.get(field)
    _require(isinstance(child, Mapping), "mapping_required", "field must be an object", field=field)
    return child


def _string(value: Mapping[str, Any], field: str) -> str:
    child = value.get(field)
    _require(
        isinstance(child, str) and bool(child),
        "string_required",
        "field must be a non-empty string",
        field=field,
    )
    return child


def _positive_int(value: Mapping[str, Any], field: str, *, allow_zero: bool = False) -> int:
    child = value.get(field)
    minimum = 0 if allow_zero else 1
    _require(
        isinstance(child, int) and not isinstance(child, bool) and child >= minimum,
        "integer_required",
        "field must be an integer in the accepted range",
        field=field,
        minimum=minimum,
    )
    return child


def _require(condition: bool, code: str, message: str, **context: Any) -> None:
    if not condition:
        raise MotivationCardError(code, message, **context)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_digest(value: Any) -> str:
    return _sha256(canonical_json_bytes(value, newline=False))


def _stable_id(prefix: str, value: Any) -> str:
    material = value if isinstance(value, str) else canonical_json_bytes(value, newline=False)
    data = material.encode("utf-8") if isinstance(material, str) else material
    return f"{prefix}-{_sha256(data)[:24]}"


def _json_clone(value: Any) -> Any:
    return json.loads(canonical_json_bytes(value, newline=False))


def _jsonl_bytes(documents: Sequence[Any]) -> bytes:
    return b"".join(canonical_json_bytes(document) for document in documents)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if path.exists():
            path.unlink()
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build deterministic outcome-blinded trajectory review cards."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-base", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--verify-blob-digests", action=argparse.BooleanOptionalAction, default=True
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = generate_and_write_motivation_artifacts(
            manifest_path=args.manifest,
            source_base=args.source_base,
            output_dir=args.output_dir,
            verify_blob_digests=args.verify_blob_digests,
        )
    except MotivationCardError as error:
        print(
            json.dumps(
                {"valid": False, "error": error.code, "message": str(error), **error.context},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "BUILDER_VERSION",
    "MotivationCardError",
    "SCHEMA_VERSION",
    "canonical_json_bytes",
    "generate_and_write_motivation_artifacts",
    "generate_motivation_artifacts",
    "main",
    "reconstruct_task_events",
    "write_motivation_artifacts",
]


if __name__ == "__main__":
    raise SystemExit(main())
