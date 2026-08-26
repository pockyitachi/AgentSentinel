from __future__ import annotations

import copy
import hashlib
import json
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import mobile_world.offline.motivation_cards as cards_module
from mobile_world.agents.implementations import gui_owl_1_5 as runtime_gui_owl
from mobile_world.agents.implementations import memgui_agent as runtime_memgui
from mobile_world.agents.implementations import ui_venus_agent as runtime_ui_venus
from mobile_world.agents.implementations.gelab_agent import parse_gelab_response
from mobile_world.agents.utils.helpers import add_period_robustly
from mobile_world.agents.utils.prompts import (
    GUI_OWL_1_5_USER_PROMPT_TEMPLATE,
    GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE,
)
from mobile_world.agents.utils.prompts.memgui import MEMGUI_SYSTEM_PROMPT, MEMGUI_USER_TEMPLATE
from mobile_world.agents.utils.prompts.ui_venus import UI_VENUS_15_PROMPT
from mobile_world.offline.motivation_cards import (
    MotivationCardError,
    canonical_json_bytes,
    generate_and_write_motivation_artifacts,
    reconstruct_task_events,
)
from mobile_world.offline.motivation_review import validate_task_cards


def _ulid(seed: int) -> str:
    return f"01ARZ3NDEKTSV4RRFFQ69G{seed:04d}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blob(digest: str, media_type: str = "image/png") -> dict[str, Any]:
    return {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": 1,
        "media_type": media_type,
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }


def _blob_from_bytes(data: bytes, media_type: str = "image/png") -> dict[str, Any]:
    digest = _sha(data)
    return {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(data),
        "media_type": media_type,
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }


def _png_bytes(color: tuple[int, int, int], *, compress_level: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (4, 6), color).save(
        buffer,
        format="PNG",
        compress_level=compress_level,
    )
    return buffer.getvalue()


def _observation(digest: str, *, ask_user_response: Any = None) -> dict[str, Any]:
    return {
        "screenshot": {
            "pixel_blob": _blob(digest),
            "source_blob": None,
            "width": 1080,
            "height": 2400,
            "mode": "RGB",
            "representation": "canonical_png_from_runtime_pixels",
        },
        "accessibility_tree": None,
        "tool_call": None,
        "ask_user_response": ask_user_response,
    }


def _event(
    *,
    seq: int,
    event_type: str,
    run_id: str,
    task_run_id: str,
    payload: dict[str, Any],
    caused_by: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": "mobileworld.audit.event/v1",
        "event_id": _ulid(100 + seq),
        "event_type": event_type,
        "run_id": run_id,
        "task_run_id": task_run_id,
        "stream_id": task_run_id,
        "seq": seq,
        "wall_time": "2026-08-21T00:00:00Z",
        "monotonic_ns": seq,
        "caused_by_event_id": caused_by,
        "producer": {
            "component": "mobile_world.audit",
            "version": "fixture",
            "process_id": 1,
            "worker_id": "pytest",
        },
        "payload": payload,
    }


def _fixture_events() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    run_id = _ulid(1)
    task_run_id = _ulid(2)
    predictions = [
        (
            "<think>I have successfully opened the settings panel and confirmed the profile "
            "page is visible with account controls ready for editing now.</think>"
            '<tool_call>{"name":"click"}</tool_call>'
        ),
        (
            "<think>Oops, that did not work and nothing changed; I misclicked the old button. "
            "I need to open the settings panel, inspect the profile page, and use the account "
            "controls carefully before editing.</think>"
            '<tool_call>{"name":"click"}</tool_call>'
        ),
        (
            "<think>That did not work and nothing changed; I misclicked the old button. I need "
            "to open the settings panel, inspect the profile page, and use the account controls "
            "carefully before editing.</think>"
            '<tool_call>{"name":"ask_user"}</tool_call>'
        ),
        (
            "<think>The user answered blue, so I will type blue in the requested field and "
            "continue checking the profile page.</think>"
            '<tool_call>{"name":"type"}</tool_call>'
        ),
        (
            "<think>I have successfully opened the settings panel and confirmed the profile "
            "page is visible with account controls ready for editing now.</think>"
            '<tool_call>{"name":"click"}</tool_call>'
        ),
    ]
    actions: list[Any] = [
        {"action_type": "click", "x": 101, "y": 202},
        {"action_type": "click", "x": 103, "y": 204},
        {"action_type": "ask_user", "message": "Which color?"},
        {"action_type": "type", "text": "blue"},
        None,
    ]
    digests = [f"{value:064x}" for value in (11, 11, 33, 44, 55)]
    pre = [
        _observation(digests[0]),
        _observation(digests[1]),
        _observation(digests[2]),
        _observation(digests[3], ask_user_response="blue"),
        _observation(digests[4]),
    ]
    post = [
        copy.deepcopy(pre[1]),
        copy.deepcopy(pre[2]),
        copy.deepcopy(pre[3]),
        copy.deepcopy(pre[4]),
        copy.deepcopy(pre[4]),
    ]

    events: list[dict[str, Any]] = []
    seq = 1
    task_start = _event(
        seq=seq,
        event_type="task_started",
        run_id=run_id,
        task_run_id=task_run_id,
        caused_by=None,
        payload={
            "task_name": "FixtureTask",
            "task_goal": "Set the profile color requested by the user.",
            "task_index": 1,
            "whole_task_attempt_index": 1,
        },
    )
    events.append(task_start)
    prior_event_id = task_start["event_id"]
    for position in range(5):
        step = position + 1
        step_id = _ulid(20 + step)
        seq += 1
        step_event = _event(
            seq=seq,
            event_type="step_started",
            run_id=run_id,
            task_run_id=task_run_id,
            caused_by=prior_event_id,
            payload={"step_id": step_id, "step_index": step, "observation": pre[position]},
        )
        events.append(step_event)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "fixture"},
            {"role": "user", "content": "Set the profile color requested by the user."},
        ]
        for prediction in predictions[:position]:
            messages.append({"role": "assistant", "content": prediction})
        if step == 4:
            messages.append({"role": "user", "content": "blue"})
        request_images = []
        if step == 5:
            request_images = [
                {
                    "content_path": f"messages[{index}].content[0].image_url.url",
                    "content_blob": _blob(digest),
                    "width": 1080,
                    "height": 2400,
                }
                for index, digest in enumerate(digests[2:5], start=1)
            ]
        request_id = _ulid(40 + step)
        model_call_id = _ulid(50 + step)
        seq += 1
        request = _event(
            seq=seq,
            event_type="model_request",
            run_id=run_id,
            task_run_id=task_run_id,
            caused_by=step_event["event_id"],
            payload={
                "step_id": step_id,
                "step_index": step,
                "request_id": request_id,
                "model_call_id": model_call_id,
                "request_view": {"model": "fixture", "messages": messages},
                "request_images": request_images,
                "sdk_arguments_snapshot_blob": _blob(f"{70 + step:064x}", "application/json"),
            },
        )
        events.append(request)

        provider_content = predictions[position]
        if step == 5:
            provider_content = "Provider returned a long malformed response before parsing failed."
        seq += 1
        response = _event(
            seq=seq,
            event_type="model_response",
            run_id=run_id,
            task_run_id=task_run_id,
            caused_by=request["event_id"],
            payload={
                "step_id": step_id,
                "step_index": step,
                "request_id": request_id,
                "model_call_id": model_call_id,
                "normalized_response": {"choices": [{"content": provider_content}]},
                "raw_response": {"kind": "fixture"},
                "returned_value_snapshot_blob": None,
            },
        )
        events.append(response)

        seq += 1
        decision = _event(
            seq=seq,
            event_type="agent_decision",
            run_id=run_id,
            task_run_id=task_run_id,
            caused_by=response["event_id"],
            payload={
                "step_id": step_id,
                "step_index": step,
                "source_model_call_ids": [model_call_id],
                "prediction_raw": predictions[position],
                "prediction_snapshot_blob": None,
                "parsed_action": {"value": actions[position]} if actions[position] else None,
                "parse_outcome": "ok" if actions[position] else "error",
                "parse_exception": None if actions[position] else "fixture parse error",
            },
        )
        events.append(decision)

        if step < 5:
            seq += 1
            execution = _event(
                seq=seq,
                event_type="action_execution_started",
                run_id=run_id,
                task_run_id=task_run_id,
                caused_by=decision["event_id"],
                payload={"step_id": step_id, "step_index": step, "action": actions[position]},
            )
            events.append(execution)
            seq += 1
            transition_type = "transition_failed" if step == 2 else "transition_completed"
            transition = _event(
                seq=seq,
                event_type=transition_type,
                run_id=run_id,
                task_run_id=task_run_id,
                caused_by=execution["event_id"],
                payload={
                    "step_id": step_id,
                    "step_index": step,
                    "action_execution_event_id": execution["event_id"],
                    "post_observation": post[position],
                    "execution_result": {
                        "kind": "ask_user" if step == 3 else "gui_transport",
                        "ask_user_response": "blue" if step == 3 else None,
                        "http_status": 200,
                    },
                    "available_execution_result": None,
                    "exception": "fixture failure" if step == 2 else None,
                    "reason": None,
                    "duration_ns": 10,
                },
            )
        else:
            seq += 1
            transition = _event(
                seq=seq,
                event_type="transition_not_executed",
                run_id=run_id,
                task_run_id=task_run_id,
                caused_by=decision["event_id"],
                payload={
                    "step_id": step_id,
                    "step_index": step,
                    "post_observation": post[position],
                    "execution_result": None,
                    "available_execution_result": None,
                    "exception": None,
                    "reason": "parse_failed",
                    "duration_ns": 0,
                },
            )
        events.append(transition)
        prior_event_id = transition["event_id"]

    seq += 1
    task_end = _event(
        seq=seq,
        event_type="task_ended",
        run_id=run_id,
        task_run_id=task_run_id,
        caused_by=prior_event_id,
        payload={
            "runtime_status": "completed",
            "termination": {"source": "max_step", "final_step_index": 5},
            "environment_evaluation": {"score": 0.0, "reason": "private outcome reason"},
            "teardown": {"attempted": True},
            "token_usage": {"total_tokens": 123},
            "capture_complete": True,
            "missing_artifacts": [],
            "collector_error_event_ids": [],
        },
    )
    events.append(task_end)

    task_entry = {
        "canonical_suite_index": 1,
        "task_name": "FixtureTask",
        "source_id": "fixture",
        "source_run_id": run_id,
        "source_task_run_id": task_run_id,
        "capture_complete": True,
        "task_stream": {
            "relative_path": f"tasks/{task_run_id}/events.jsonl",
            "sha256": "0" * 64,
            "byte_count": 0,
        },
    }
    source_entry = {
        "source_id": "fixture",
        "run_id": run_id,
        "relative_run_path": "raw/run",
    }
    return events, task_entry, source_entry


def _qwen_fixture_events() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    events, task_entry, source_entry = _fixture_events()
    predictions = [
        (
            "Thought: I should inspect the profile.\n"
            'Action: "I successfully opened the \\"Profile\\" panel.\nIt is ready."\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click"}}</tool_call>'
        ),
        (
            "Thought: I should inspect the unchanged control.\n"
            'Action: "I inspected the unchanged account control."\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click"}}</tool_call>'
        ),
        (
            "Thought: I should ask for the requested color.\n"
            'Action: "I successfully asked the user for the requested color."\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"ask_user"}}</tool_call>'
        ),
        (
            "Thought: I should use the answer.\n"
            'Action: "I inspected the unchanged account control."\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"type"}}</tool_call>'
        ),
        (
            "Thought: I should finish checking.\n"
            'Action: "I finished checking the profile."\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"terminate"}}</tool_call>'
        ),
    ]
    task_goal = "Set the profile color requested by the user."
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    step_events[2]["payload"]["observation"]["tool_call"] = {"status": "ready"}
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"] = {"adapter": "qwen3vl", "model": "fixture-qwen"}
    source_entry["provenance"] = {"agent_type": "qwen3vl", "model_name": "fixture-qwen"}

    conclusions = [
        'I successfully opened the \\"Profile\\" panel.\nIt is ready.',
        "I inspected the unchanged account control.",
        "I successfully asked the user for the requested color.",
        "I inspected the unchanged account control.",
        "I finished checking the profile.",
    ]
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    for position, request in enumerate(request_events):
        progress = ""
        for source_position, conclusion in enumerate(conclusions[:position]):
            rendered = conclusion
            next_observation = step_events[source_position + 1]["payload"]["observation"]
            if next_observation.get("tool_call") is not None:
                rendered += (
                    "; Tool call result: <tool_response>"
                    + json.dumps(next_observation["tool_call"], ensure_ascii=False)
                    + "</tool_response>"
                )
            if next_observation.get("ask_user_response") is not None:
                rendered += f"; Ask user response: {next_observation['ask_user_response']}"
            rendered = rendered.replace("\n", "").replace('"', "")
            progress += f"Step {source_position + 1}: {rendered}; "
        text = (
            f"\nThe user query: {task_goal}\n"
            "Task progress (You have done the following operation on the current device): "
            f"{progress}\n"
        )
        pre_digest = step_events[position]["payload"]["observation"]["screenshot"]["pixel_blob"][
            "digest"
        ]
        request["payload"]["request_view"] = {
            "model": "fixture-qwen",
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "system"}]},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": "externalized"}},
                    ],
                },
            ],
        }
        request["payload"]["request_images"] = [
            {
                "content_path": "messages[1].content[1].image_url.url",
                "content_blob": _blob(pre_digest),
                "width": 1080,
                "height": 2400,
            }
        ]
        response_events[position]["payload"]["normalized_response"]["choices"][0]["content"] = (
            predictions[position]
        )
        decision_events[position]["payload"]["prediction_raw"] = predictions[position]
    return events, task_entry, source_entry


def _ui_venus_fixture_events() -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, bytes],
]:
    events, task_entry, source_entry = _fixture_events()
    duplicate_prediction = (
        "<think>I have successfully opened the profile panel.</think>"
        "<action>Click(box=(100,200))</action>"
        "<conclusion>This conclusion must never enter later history.</conclusion>"
    )
    predictions = [
        duplicate_prediction,
        "PressBack()",
        (
            "<think>Parser failed, but this step remains in history.</think>"
            "<action>NotAnAction()</action>"
            "<conclusion>Failed status and conclusion are not history.</conclusion>"
        ),
        duplicate_prediction,
        (
            "<think>I should finish checking the profile.</think>"
            "<action>Finished(content='done')</action>"
            "<conclusion>The task is complete.</conclusion>"
        ),
    ]
    task_goal = "Set the profile color requested by the user."
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"] = {
        "adapter": "ui_venus_agent",
        "model": "fixture-ui-venus",
    }
    source_entry["provenance"] = {
        "agent_type": "ui_venus_agent",
        "model_name": "fixture-ui-venus",
    }
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    blob_bytes: dict[str, bytes] = {}
    request_image_refs: list[dict[str, Any]] = []
    for position, step_event in enumerate(step_events):
        color = (20 + position, 40 + position, 60 + position)
        observation_bytes = _png_bytes(color, compress_level=9)
        request_bytes = _png_bytes(color, compress_level=0)
        assert observation_bytes != request_bytes
        observation_ref = _blob_from_bytes(observation_bytes)
        request_ref = _blob_from_bytes(request_bytes)
        original_text = b"data:image/png;base64,fixture"
        original_text_ref = _blob_from_bytes(
            original_text,
            media_type="text/plain;charset=utf-8",
        )
        blob_bytes[observation_ref["digest"]] = observation_bytes
        blob_bytes[request_ref["digest"]] = request_bytes
        blob_bytes[original_text_ref["digest"]] = original_text
        screenshot = step_event["payload"]["observation"]["screenshot"]
        screenshot.update(
            {
                "pixel_blob": observation_ref,
                "width": 4,
                "height": 6,
                "mode": "RGB",
            }
        )
        request_image_refs.append(
            {
                "content_blob": request_ref,
                "original_text_blob": original_text_ref,
            }
        )
    for position, request in enumerate(request_events):
        history_entries = []
        for history_ordinal, prediction in enumerate(predictions[:position]):
            think = runtime_ui_venus._extract_tag_content("think", prediction) or ""
            action = (
                runtime_ui_venus._extract_tag_content("action", prediction) or prediction.strip()
            )
            history_entries.append(
                f"Step {history_ordinal}: <think>{think}</think><action>{action}</action>"
            )
        query = UI_VENUS_15_PROMPT.format(
            user_task=task_goal,
            previous_actions="\n".join(history_entries),
        )
        request["payload"]["request_view"] = {
            "model": "fixture-ui-venus",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": query},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": {
                                    "$externalized_data_url": {
                                        "base64_alphabet": "standard",
                                        "content_blob": request_image_refs[position][
                                            "content_blob"
                                        ],
                                        "content_path": ("messages[1].content[1].image_url.url"),
                                        "media_type": "image/png",
                                        "original_text_blob": request_image_refs[position][
                                            "original_text_blob"
                                        ],
                                    }
                                }
                            },
                        },
                    ],
                },
            ],
        }
        request["payload"]["request_images"] = [
            {
                "content_path": "messages[1].content[1].image_url.url",
                "original_text_blob": request_image_refs[position]["original_text_blob"],
                "content_blob": request_image_refs[position]["content_blob"],
                "media_type": "image/png",
                "width": 4,
                "height": 6,
                "capture_status": "captured",
                "canonical_base64": True,
            }
        ]
        response_events[position]["payload"]["normalized_response"]["choices"][0]["content"] = (
            predictions[position]
        )
        decision_events[position]["payload"]["prediction_raw"] = predictions[position]
    decision_events[2]["payload"]["parsed_action"] = {
        "value": {"action_type": "unknown", "text": "Unknown action: NotAnAction"}
    }
    decision_events[2]["payload"]["parse_outcome"] = "fallback_returned"
    return events, task_entry, source_entry, blob_bytes


def _gui_owl_fixture_events() -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, bytes],
]:
    events, task_entry, source_entry = _fixture_events()
    predictions = [
        (
            'Action: "I have successfully opened the profile settings panel"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click",'
            '"coordinate":[100,200]}}</tool_call>'
        ),
        (
            'Action: "That did not work and nothing changed; click the account control again"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click",'
            '"coordinate":[100,200]}}</tool_call>'
        ),
        (
            'Action: "Ask the user which profile color is required"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"interact",'
            '"text":"Which color?"}}</tool_call>'
        ),
        (
            'Action: "Use the blue answer in the profile color field"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"type",'
            '"text":"blue"}}</tool_call>'
        ),
        (
            'Action: "The profile color task is complete"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"terminate",'
            '"status":"success"}}</tool_call>'
        ),
    ]
    task_goal = "Set the profile color requested by the user."
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"] = {
        "adapter": "gui_owl_1_5",
        "model": "fixture-gui-owl",
    }
    source_entry["provenance"] = {
        "agent_type": "gui_owl_1_5",
        "model_name": "fixture-gui-owl",
    }
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    transitions = sorted(
        (
            event
            for event in events
            if event["event_type"]
            in {"transition_completed", "transition_failed", "transition_not_executed"}
        ),
        key=lambda event: event["payload"]["step_index"],
    )
    tool_result = {"status": "ready", "count": 1}
    step_events[2]["payload"]["observation"]["tool_call"] = copy.deepcopy(tool_result)
    transitions[1]["payload"]["post_observation"]["tool_call"] = copy.deepcopy(tool_result)
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )

    conclusions = [
        runtime_gui_owl.parse_action_to_structure_output(prediction)["conclusion"]
        for prediction in predictions
    ]
    formatter = object.__new__(runtime_gui_owl.GUIOWL15AgentMCP)
    formatter.conclusions = conclusions

    blob_bytes: dict[str, bytes] = {}
    request_image_refs: list[dict[str, Any]] = []
    for position, step_event in enumerate(step_events):
        color = (80 + position, 100 + position, 120 + position)
        observation_bytes = _png_bytes(color, compress_level=9)
        request_bytes = _png_bytes(color, compress_level=0)
        assert observation_bytes != request_bytes
        observation_ref = _blob_from_bytes(observation_bytes)
        request_ref = _blob_from_bytes(request_bytes)
        original_text = f"data:image/png;base64,gui-owl-{position}".encode()
        original_text_ref = _blob_from_bytes(
            original_text,
            media_type="text/plain;charset=utf-8",
        )
        blob_bytes[observation_ref["digest"]] = observation_bytes
        blob_bytes[request_ref["digest"]] = request_bytes
        blob_bytes[original_text_ref["digest"]] = original_text
        screenshot = step_event["payload"]["observation"]["screenshot"]
        screenshot.update(
            {
                "pixel_blob": observation_ref,
                "width": 4,
                "height": 6,
                "mode": "RGB",
            }
        )
        request_image_refs.append(
            {
                "content_blob": request_ref,
                "original_text_blob": original_text_ref,
            }
        )

    for position, request in enumerate(request_events):
        if position == 0:
            query = GUI_OWL_1_5_USER_PROMPT_TEMPLATE.format(instruction=task_goal)
        else:
            history_user_content = [
                (
                    "fixture-image",
                    step_event["payload"]["observation"].get("tool_call"),
                    step_event["payload"]["observation"].get("ask_user_response"),
                )
                for step_event in step_events[: position + 1]
            ]
            previous_steps = formatter._format_previous_steps(  # noqa: SLF001
                0,
                position,
                history_user_content,
            )
            query = GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE.format(
                instruction=task_goal,
                previous_steps=previous_steps,
            )
        request_image_ref = request_image_refs[position]
        request["payload"]["request_view"] = {
            "model": "fixture-gui-owl",
            "messages": [
                {"role": "system", "content": "fixture GUI-Owl system prompt"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": query},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": {
                                    "$externalized_data_url": {
                                        "base64_alphabet": "standard",
                                        "content_blob": request_image_ref["content_blob"],
                                        "content_path": ("messages[1].content[1].image_url.url"),
                                        "media_type": "image/png",
                                        "original_text_blob": request_image_ref[
                                            "original_text_blob"
                                        ],
                                    }
                                }
                            },
                        },
                    ],
                },
            ],
        }
        request["payload"]["request_images"] = [
            {
                "content_path": "messages[1].content[1].image_url.url",
                "original_text_blob": request_image_ref["original_text_blob"],
                "content_blob": request_image_ref["content_blob"],
                "media_type": "image/png",
                "width": 4,
                "height": 6,
                "capture_status": "captured",
                "canonical_base64": True,
            }
        ]
        response_events[position]["payload"]["normalized_response"]["choices"][0]["content"] = (
            predictions[position]
        )
        decision_events[position]["payload"]["prediction_raw"] = predictions[position]

    decision_events[-1]["payload"]["parsed_action"] = {
        "value": {"action_type": "finished", "text": "success"}
    }
    decision_events[-1]["payload"]["parse_outcome"] = "returned"
    decision_events[-1]["payload"]["parse_exception"] = None
    transitions[-1]["payload"]["reason"] = "terminal_action"
    return events, task_entry, source_entry, blob_bytes


def _replace_gui_owl_source_turn(
    events: list[dict[str, Any]],
    *,
    source_step: int,
    prediction: str,
    action: dict[str, Any],
) -> None:
    """Replace one accepted turn and deterministically replay collapsed prompts."""

    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    prediction_index = source_step - 1
    response_events[prediction_index]["payload"]["normalized_response"]["choices"][0]["content"] = (
        prediction
    )
    decision_events[prediction_index]["payload"]["prediction_raw"] = prediction
    decision_events[prediction_index]["payload"]["parsed_action"] = {"value": action}
    for event in events:
        if (
            event["event_type"] == "action_execution_started"
            and event["payload"]["step_index"] == source_step
        ):
            event["payload"]["action"] = copy.deepcopy(action)
        if (
            event["event_type"] in {"transition_completed", "transition_failed"}
            and event["payload"]["step_index"] == source_step
            and "action" in event["payload"]
        ):
            event["payload"]["action"] = copy.deepcopy(action)

    task_goal = next(event for event in events if event["event_type"] == "task_started")["payload"][
        "task_goal"
    ]
    predictions = [event["payload"]["prediction_raw"] for event in decision_events]
    formatter = object.__new__(runtime_gui_owl.GUIOWL15AgentMCP)
    formatter.conclusions = [
        runtime_gui_owl.parse_action_to_structure_output(value)["conclusion"]
        for value in predictions
    ]
    history_user_content = [
        (
            "fixture-image",
            step_event["payload"]["observation"].get("tool_call"),
            step_event["payload"]["observation"].get("ask_user_response"),
        )
        for step_event in step_events
    ]
    for position, request in enumerate(request_events):
        if position == 0:
            query = GUI_OWL_1_5_USER_PROMPT_TEMPLATE.format(instruction=task_goal)
        else:
            previous_steps = formatter._format_previous_steps(  # noqa: SLF001
                0,
                position,
                history_user_content,
            )
            query = GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE.format(
                instruction=task_goal,
                previous_steps=previous_steps,
            )
        request["payload"]["request_view"]["messages"][1]["content"][0]["text"] = query


def _ui_venus_blob_reader(blob_bytes: dict[str, bytes]) -> Any:
    return lambda reference: blob_bytes[reference["digest"]]


def _memgui_fixture_events() -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    dict[str, bytes],
]:
    events, task_entry, source_entry = _fixture_events()
    duplicate_summary = "[Step 1] I have successfully stored alpha.\n  [literal marker]"
    predictions = [
        (
            '<thinking>Store the first value.</thinking><folding>{"range":[1,1],'
            f'"summary":{json.dumps("ignored first-step fold")}'
            "}</folding>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"alpha","description":"Original description",'
            '"content":"first value"}}</tool_call>'
            "<ui_observation>Profile panel is visible.</ui_observation>"
            "<action_intent>Store alpha for later use.</action_intent>"
        ),
        (
            '<thinking>Update the value.</thinking><folding>{"range":[1,1],'
            f'"summary":{json.dumps(duplicate_summary)}'
            "}</folding>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_update",'
            '"memory_id":"alpha","description":"",'
            '"content":"I have successfully updated the second value."}}</tool_call>'
            "<ui_observation>I have successfully updated alpha.</ui_observation>"
            "<action_intent>Continue after the successful update.</action_intent>"
        ),
        (
            '<thinking>Add beta.</thinking><folding>{"range":[3,3],'
            f'"summary":{json.dumps(duplicate_summary)}'
            "}</folding>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"beta","description":"Beta description",'
            '"content":"beta value"}}</tool_call>'
            "<ui_observation>Alpha remains visible.</ui_observation>"
            "<action_intent>Store beta after alpha.</action_intent>"
        ),
        (
            '<thinking>Delete alpha.</thinking><folding>{"range":[2,3],'
            '"summary":"[Steps 2-3] I have successfully updated alpha and stored beta."'
            "}</folding>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_delete",'
            '"memory_id":"alpha"}}</tool_call>'
            "<ui_observation></ui_observation><action_intent></action_intent>"
        ),
        (
            '<thinking>Finish.</thinking><folding>{"range":[1,2],'
            '"summary":"[Steps 1-2] Finished the earlier memory work."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"terminate",'
            '"status":"success"}}</tool_call>'
            "<ui_observation>Done screen.</ui_observation>"
            "<action_intent>Terminate successfully.</action_intent>"
        ),
    ]
    task_goal = "Set the profile color requested by the user."
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"] = {
        "adapter": "memgui",
        "model": "fixture-memgui",
    }
    source_entry["provenance"] = {
        "agent_type": "memgui",
        "model_name": "fixture-memgui",
    }
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    execution_events = {
        event["payload"]["step_index"]: event
        for event in events
        if event["event_type"] == "action_execution_started"
    }
    transitions = sorted(
        (
            event
            for event in events
            if event["event_type"]
            in {"transition_completed", "transition_failed", "transition_not_executed"}
        ),
        key=lambda event: event["payload"]["step_index"],
    )

    blob_bytes: dict[str, bytes] = {}
    image_refs: list[dict[str, Any]] = []
    for position, step_event in enumerate(step_events):
        color = (130 + position, 150 + position, 170 + position)
        observation_bytes = _png_bytes(color, compress_level=9)
        request_bytes = _png_bytes(color, compress_level=0)
        assert observation_bytes != request_bytes
        observation_ref = _blob_from_bytes(observation_bytes)
        request_ref = _blob_from_bytes(request_bytes)
        original_text = f"data:image/png;base64,memgui-{position}".encode()
        original_text_ref = _blob_from_bytes(
            original_text,
            media_type="text/plain;charset=utf-8",
        )
        blob_bytes[observation_ref["digest"]] = observation_bytes
        blob_bytes[request_ref["digest"]] = request_bytes
        blob_bytes[original_text_ref["digest"]] = original_text
        step_event["payload"]["observation"]["screenshot"].update(
            {
                "pixel_blob": observation_ref,
                "width": 4,
                "height": 6,
                "mode": "RGB",
            }
        )
        image_refs.append(
            {
                "content_blob": request_ref,
                "original_text_blob": original_text_ref,
            }
        )
    for position, transition in enumerate(transitions):
        next_position = min(position + 1, len(step_events) - 1)
        transition["payload"]["post_observation"] = copy.deepcopy(
            step_events[next_position]["payload"]["observation"]
        )

    formatter = object.__new__(runtime_memgui.MemGUIAgent)
    formatter.current_step = 0
    formatter.state_summaries = []
    formatter.latest_interaction = None
    formatter.memory_state = {}
    formatter.folding_stats = {
        "step_level_distillations": 0,
        "span_level_abstractions": 0,
        "total_steps_folded": 0,
    }
    for position, prediction in enumerate(predictions):
        step = position + 1
        formatter.current_step = step
        folding_instruction = (
            "Skip <folding> for the first step"
            if step == 1
            else "Output <folding> to compress your previous step(s)"
        )
        query = MEMGUI_USER_TEMPLATE.format(
            instruction=task_goal,
            state_summaries=formatter._format_state_summaries(),  # noqa: SLF001
            latest_interaction=formatter._format_latest_interaction(),  # noqa: SLF001
            memory_state=formatter._format_memory_state(),  # noqa: SLF001
            folding_instruction=folding_instruction,
        )
        image_ref = image_refs[position]
        request = request_events[position]
        request["payload"].update(
            {
                "component": "mobile_world.agents.implementations.memgui_agent",
                "call_role": "actor",
                "request_view": {
                    "model": "fixture-memgui",
                    "messages": [
                        {
                            "role": "system",
                            "content": [{"type": "text", "text": MEMGUI_SYSTEM_PROMPT}],
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": query},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": {
                                            "$externalized_data_url": {
                                                "base64_alphabet": "standard",
                                                "content_blob": image_ref["content_blob"],
                                                "content_path": (
                                                    "messages[1].content[1].image_url.url"
                                                ),
                                                "media_type": "image/png",
                                                "original_text_blob": image_ref[
                                                    "original_text_blob"
                                                ],
                                            }
                                        }
                                    },
                                },
                            ],
                        },
                    ],
                },
                "request_images": [
                    {
                        "content_path": "messages[1].content[1].image_url.url",
                        "original_text_blob": image_ref["original_text_blob"],
                        "content_blob": image_ref["content_blob"],
                        "media_type": "image/png",
                        "width": 4,
                        "height": 6,
                        "capture_status": "captured",
                        "canonical_base64": True,
                    }
                ],
            }
        )
        response_events[position]["payload"]["normalized_response"]["choices"][0]["content"] = (
            prediction
        )
        decision = decision_events[position]
        parsed = runtime_memgui.parse_memgui_response(
            prediction,
            image_height=6,
            image_width=4,
            current_step=step,
        )
        json_action = runtime_memgui.build_json_action(parsed, image_height=6, image_width=4)
        action_value = json_action.model_dump(mode="json")
        decision["payload"].update(
            {
                "prediction_raw": prediction,
                "parsed_action": {"value": action_value},
                "parse_outcome": "returned",
                "parse_exception": None,
            }
        )
        if step in execution_events:
            execution_events[step]["payload"]["action"] = copy.deepcopy(action_value)

        if step > 1:
            (
                formatter.state_summaries,
                formatter.folding_stats,
                _,
            ) = formatter._prepare_folding_update(parsed["folding_directive"])  # noqa: SLF001
        next_memory = dict(formatter.memory_state)
        if parsed["memory_args"] is not None:
            memory_result = formatter._execute_memory_operation(  # noqa: SLF001
                parsed["memory_args"],
                memory_state=next_memory,
            )
            action_summary = f"Memory: {memory_result}"
        else:
            action_summary = json_action.action_type
        formatter.memory_state = next_memory
        formatter.latest_interaction = {
            "step": step,
            "ui_observation": parsed["ui_observation"],
            "action_intent": parsed["action_intent"],
            "action_summary": action_summary,
        }

    transitions[-1]["payload"]["reason"] = "terminal_action"
    return events, task_entry, source_entry, blob_bytes


def _insert_memgui_rejected_retries(events: list[dict[str, Any]], *, step: int) -> None:
    """Insert two unselected outer-parse responses before the accepted call."""

    step_event = next(
        event
        for event in events
        if event["event_type"] == "step_started" and event["payload"]["step_index"] == step
    )
    selected_request = next(
        event
        for event in events
        if event["event_type"] == "model_request" and event["payload"]["step_index"] == step
    )
    selected_decision = next(
        event
        for event in events
        if event["event_type"] == "agent_decision" and event["payload"]["step_index"] == step
    )
    selected_model_call_id = selected_request["payload"]["model_call_id"]
    insert_at = events.index(selected_request)
    rejected_ids: list[str] = []
    additions: list[dict[str, Any]] = []
    for retry_index in range(2):
        request = copy.deepcopy(selected_request)
        request["event_id"] = _ulid(800 + retry_index * 2)
        request["caused_by_event_id"] = step_event["event_id"]
        request["payload"]["request_id"] = _ulid(820 + retry_index)
        request["payload"]["model_call_id"] = _ulid(830 + retry_index)
        request["payload"]["adapter_attempt_index"] = retry_index + 1
        response = copy.deepcopy(
            next(
                event
                for event in events
                if event["event_type"] == "model_response"
                and event["payload"]["step_index"] == step
            )
        )
        response["event_id"] = _ulid(801 + retry_index * 2)
        response["caused_by_event_id"] = request["event_id"]
        response["payload"]["request_id"] = request["payload"]["request_id"]
        response["payload"]["model_call_id"] = request["payload"]["model_call_id"]
        response["payload"]["adapter_attempt_index"] = retry_index + 1
        response["payload"]["normalized_response"]["choices"][0]["content"] = (
            '<thinking>Rejected.</thinking><folding>{"range":[1,1],'
            '"summary":"REJECTED HISTORY"}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"rejected","content":"REJECTED MEMORY"}}</tool_call>'
            "<ui_observation>Rejected.</ui_observation>"
            "<action_intent>Rejected.</action_intent>"
        )
        rejected_ids.append(request["payload"]["model_call_id"])
        additions.extend((request, response))
    events[insert_at:insert_at] = additions
    selected_decision["payload"]["source_model_call_ids"] = [
        *rejected_ids,
        selected_model_call_id,
    ]
    for seq, event in enumerate(events, start=1):
        event["seq"] = seq
        event["monotonic_ns"] = seq


def _memgui_cores_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    event_by_id = {event["event_id"]: event for event in events}
    cores = []
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    for step_event in step_events:
        step_id = step_event["payload"]["step_id"]
        step = step_event["payload"]["step_index"]
        decision = next(
            event
            for event in events
            if event["event_type"] == "agent_decision"
            and event["payload"].get("step_id") == step_id
        )
        response = event_by_id[decision["caused_by_event_id"]]
        request = event_by_id[response["caused_by_event_id"]]
        transition = next(
            event
            for event in events
            if event["event_type"]
            in {"transition_completed", "transition_failed", "transition_not_executed"}
            and event["payload"].get("step_id") == step_id
        )
        prediction = decision["payload"]["prediction_raw"]
        provider_content = response["payload"]["normalized_response"]["choices"][0]["content"]
        cores.append(
            {
                "step_index": step,
                "step_id": step_id,
                "step_started": step_event,
                "selected_request": request,
                "selected_response": response,
                "step_model_requests": [
                    event
                    for event in events
                    if event["event_type"] == "model_request"
                    and event["payload"].get("step_id") == step_id
                ],
                "step_model_responses": [
                    event
                    for event in events
                    if event["event_type"] == "model_response"
                    and event["payload"].get("step_id") == step_id
                ],
                "decision": decision,
                "transition": transition,
                "pre_observation": step_event["payload"].get("observation"),
                "post_observation": transition["payload"].get("post_observation"),
                "prediction": prediction,
                "provider_content": provider_content,
                "provider_decision_comparison": cards_module._provider_decision_comparison(
                    provider_content,
                    prediction,
                ),
                "action": decision["payload"]["parsed_action"]["value"],
            }
        )
    return cores


def _synthetic_memgui_core(
    step: int,
    prediction: str,
    *,
    action_type: str,
) -> dict[str, Any]:
    step_id = _ulid(900 + step)
    request_id = _ulid(920 + step)
    model_call_id = _ulid(940 + step)
    request = {
        "event_id": _ulid(960 + step),
        "event_type": "model_request",
        "payload": {
            "step_id": step_id,
            "request_id": request_id,
            "model_call_id": model_call_id,
            "component": "mobile_world.agents.implementations.memgui_agent",
            "call_role": "actor",
        },
    }
    response = {
        "event_id": _ulid(980 + step),
        "event_type": "model_response",
        "caused_by_event_id": request["event_id"],
        "payload": {
            "step_id": step_id,
            "request_id": request_id,
            "model_call_id": model_call_id,
        },
    }
    decision = {
        "event_id": _ulid(1000 + step),
        "event_type": "agent_decision",
        "caused_by_event_id": response["event_id"],
        "payload": {
            "step_id": step_id,
            "source_model_call_ids": [model_call_id],
            "prediction_raw": prediction,
            "parsed_action": {"value": {"action_type": action_type}},
            "parse_outcome": "returned",
            "parse_exception": None,
        },
    }
    return {
        "step_index": step,
        "step_id": step_id,
        "step_started": {"event_id": _ulid(1020 + step)},
        "selected_request": request,
        "selected_response": response,
        "step_model_requests": [request],
        "step_model_responses": [response],
        "decision": decision,
        "transition": {"event_id": _ulid(1040 + step), "payload": {}},
        "prediction": prediction,
        "provider_content": prediction,
        "provider_decision_comparison": {"status": "exact_match"},
        "action": {"action_type": action_type},
    }


def _gelab_fixture_events() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    events, task_entry, source_entry = _fixture_events()
    predictions = [
        (
            "<THINK> I have successfully opened the profile settings panel. </THINK>\n"
            "explain:Open profile settings.\taction:CLICK\tpoint:100,200\t"
            "summary:I opened Settings: the Profile panel is visible."
        ),
        (
            "<THINK> I should wait for the unchanged page. </THINK>\n"
            "explain:Wait for the page.\taction:WAIT\tvalue:1"
        ),
        (
            "<THINK> I should ask the user for the requested color. </THINK>\n"
            "explain:Ask which color is required.\taction:INFO\tvalue:Which color?\t"
            "summary:I have successfully asked the user which color is required."
        ),
        (
            "<THINK> I have successfully entered the requested blue color. </THINK>\n"
            "explain:Enter the answer.\taction:TYPE\tvalue:blue\tpoint:100,200\t"
            "summary:I entered blue in the profile color field."
        ),
        (
            "<THINK> I should finish checking the profile. </THINK>\n"
            "explain:Finish the task.\taction:COMPLETE\treturn:Done\t"
            "summary:The profile color task is complete."
        ),
    ]
    summaries = [
        "I opened Settings: the Profile panel is visible.",
        "",
        "I have successfully asked the user which color is required.",
        "I entered blue in the profile color field.",
        "The profile color task is complete.",
    ]
    task_goal = "Set the profile color requested by the user."
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"] = {"adapter": "gelab_agent", "model": "fixture-gelab"}
    source_entry["provenance"] = {
        "agent_type": "gelab_agent",
        "model_name": "fixture-gelab",
    }
    step_events = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    request_events = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    response_events = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    decision_events = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    for position, request in enumerate(request_events):
        summary = summaries[position - 1] if position else ""
        ask_user_response = step_events[position]["payload"]["observation"].get("ask_user_response")
        history_display = cards_module._GELAB_EMPTY_HISTORY
        if summary:
            history_display = summary
            if ask_user_response:
                history_display += cards_module._GELAB_USER_RESPONSE_PREFIX + ask_user_response
        user_prompt = cards_module.GELAB_USER_PROMPT_TEMPLATE.render(
            task=task_goal,
            history_display=history_display,
        )
        pre_digest = step_events[position]["payload"]["observation"]["screenshot"]["pixel_blob"][
            "digest"
        ]
        request["payload"]["request_view"] = {
            "model": "fixture-gelab",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": cards_module.GELAB_SYSTEM_PROMPT,
                        },
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": "externalized"},
                        },
                        {
                            "type": "text",
                            "text": cards_module.GELAB_INSTRUCTION_SUFFIX,
                        },
                    ],
                }
            ],
        }
        request["payload"]["request_images"] = [
            {
                "content_path": "messages[0].content[2].image_url.url",
                "content_blob": _blob(pre_digest),
                "width": 1080,
                "height": 2400,
            }
        ]
        response_events[position]["payload"]["normalized_response"]["choices"][0]["content"] = (
            predictions[position] + "\n"
        )
        decision_events[position]["payload"]["prediction_raw"] = predictions[position]
    return events, task_entry, source_entry


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, list[dict[str, Any]]]:
    events, task_entry, source_entry = _fixture_events()
    source_base = tmp_path / "source-base"
    run_root = source_base / source_entry["relative_run_path"]
    stream = run_root / task_entry["task_stream"]["relative_path"]
    stream.parent.mkdir(parents=True)
    stream_bytes = b"".join(canonical_json_bytes(event) for event in events)
    stream.write_bytes(stream_bytes)
    task_entry["task_stream"].update(
        {"sha256": _sha(stream_bytes), "byte_count": len(stream_bytes)}
    )
    manifest = {
        "schema_version": "mobileworld.audit.curated-task-set/v1",
        "artifact_type": "derived_task_selection",
        "dataset_id": "fixture-curated",
        "is_raw_run": False,
        "raw_schema_version": "mobileworld.audit.event/v1",
        "selection_sha256": "a" * 64,
        "sources": [source_entry],
        "tasks": [task_entry],
    }
    curated = tmp_path / "curated"
    curated.mkdir()
    manifest_path = curated / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return manifest_path, source_base, events


def _identity() -> dict[str, str]:
    return {
        "evaluation_run_id": "fixture-evaluation",
        "dataset_sha256": "b" * 64,
        "selection_sha256": "a" * 64,
    }


def _assert_no_key(value: Any, forbidden: set[str]) -> None:
    if isinstance(value, dict):
        assert not (set(value) & forbidden)
        for child in value.values():
            _assert_no_key(child, forbidden)
    elif isinstance(value, list):
        for child in value:
            _assert_no_key(child, forbidden)


def test_builds_formal_blinded_cards_without_mutating_raw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, source_base, _ = _write_fixture(tmp_path)
    validator_calls: list[dict[str, Any]] = []

    def valid_manifest(**kwargs: Any) -> dict[str, Any]:
        validator_calls.append(kwargs)
        return {"valid": True}

    monkeypatch.setattr(cards_module, "validate_curated_composite", valid_manifest)
    before = {
        path.relative_to(source_base).as_posix(): path.read_bytes()
        for path in source_base.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "motivation-cards"

    result = generate_and_write_motivation_artifacts(
        manifest_path=manifest_path,
        source_base=source_base,
        output_dir=output,
    )

    after = {
        path.relative_to(source_base).as_posix(): path.read_bytes()
        for path in source_base.rglob("*")
        if path.is_file()
    }
    assert before == after
    assert validator_calls and validator_calls[0]["verify_blob_digests"] is True
    assert result["task_count"] == 1
    assert sorted(path.name for path in output.iterdir()) == [
        "manifest.json",
        "outcomes.sidecar.jsonl",
        "reconstruction_refs.jsonl",
        "task_cards.jsonl",
    ]
    for path in output.iterdir():
        if path.suffix == ".jsonl":
            for line in path.read_bytes().splitlines(keepends=True):
                assert line == canonical_json_bytes(json.loads(line))

    card = json.loads((output / "task_cards.jsonl").read_bytes())
    validate_task_cards({"FixtureTask": card}, expected_task_count=1)
    _assert_no_key(card, {"score", "environment_evaluation", "runtime_status", "teardown"})
    assert card["outcome_blinded"] is True
    assert card["coverage"] == {
        **card["coverage"],
        "decision_count": 5,
        "reconstructed_decision_count": 5,
        "actual_exposure_count": 10,
        "dropped_candidate_count": 0,
    }
    reasons = {
        reason for candidate in card["candidates"] for reason in candidate["retrieval_reasons"]
    }
    assert {
        "SELF_CORRECTION",
        "FAILED_TRANSITION_ACK",
        "PROGRESS_CLAIM",
        "REPEATED_ACTION",
        "STATIC_TRANSITION",
        "NEAR_DUPLICATE_REASONING",
    } <= reasons
    assert all(candidate["exposure"]["was_actually_in_request"] for candidate in card["candidates"])

    bundle_manifest = json.loads((output / "manifest.json").read_bytes())
    configuration = bundle_manifest["configuration"]
    assert configuration["max_gelab_formal_candidates_per_task"] == 4
    gelab_policy = configuration["gelab_rolling_summary_candidate_policy"]
    assert gelab_policy["ordinary_eligibility_first"] is True
    assert len(gelab_policy["priority_tiers"]) == 4
    assert "every exact rolling-summary exposure" in gelab_policy["reconstruction_retention"]
    assert "lower bound" in gelab_policy["interpretation"]
    assert configuration["max_gui_owl_formal_candidates_per_task"] == 4
    gui_owl_policy = configuration["gui_owl_collapsed_history_policy"]
    assert gui_owl_policy["representation_type"] == "hybrid_folding"
    assert gui_owl_policy["mapping_status"] == "exact_gui_owl_collapsed_history_n1"
    assert "observation N+1" in gui_owl_policy["runtime_replay"]
    assert "every exact source-target appearance" in gui_owl_policy["exposure_retention"]
    assert "minimal review claim" in gui_owl_policy["claim_boundary"]
    assert "ACTION_EXECUTION_CLAIM" in gui_owl_policy["claim_typing"]
    assert "do not use task outcome" in gui_owl_policy["action_alignment_retrieval"]
    assert (
        "retain every high-confidence ACTION_EXECUTION_MISMATCH"
        in (gui_owl_policy["candidate_selection"])
    )
    assert "every task with exposed prior actions" in gui_owl_policy["candidate_selection"]
    assert "lower bound" in gui_owl_policy["interpretation"]
    assert "neither is a validity" in gui_owl_policy["interpretation"]
    assert "RGB pixel-matrix equality" in gui_owl_policy["current_image_proof"]
    assert configuration["max_memgui_formal_candidates_per_task"] == 4
    memgui_policy = configuration["memgui_structured_folding_policy"]
    assert memgui_policy["representation_type"] == "structured_folding"
    assert memgui_policy["mapping_status"] == "exact_memgui_structured_hlm"
    assert "destructive overlap replacement" in memgui_policy["runtime_replay"]
    assert "make every M version eligible" in memgui_policy["candidate_selection"]
    assert "lower bound" in memgui_policy["interpretation"]
    assert "decoded RGB pixel matrix" in memgui_policy["evidence_image_presence"]
    ui_venus_policy = configuration["ui_venus_flat_previous_actions_policy"]
    assert ui_venus_policy["representation_type"] == "flat_previous_actions"
    assert ui_venus_policy["mapping_status"] == "exact_ui_venus_flat_previous_actions"
    assert ui_venus_policy["included_history_fields"] == ["think", "action"]
    assert ui_venus_policy["excluded_history_fields"] == ["conclusion", "status"]
    assert "every exact source-target exposure" in ui_venus_policy["exposure_retention"]
    assert "no UI-Venus-specific candidate bound" in ui_venus_policy["candidate_selection"]
    assert "RGB pixel-matrix equality" in ui_venus_policy["current_image_proof"]
    assert bundle_manifest["counts"]["history_bearing_request_count"] == 4
    assert bundle_manifest["counts"]["unique_exposed_history_entry_count"] == 4
    assert bundle_manifest["counts"]["unique_exposed_source_step_count"] == 4
    assert bundle_manifest["counts"]["gui_owl_action_history_candidate_count"] == 0
    assert bundle_manifest["counts"]["gui_owl_action_execution_mismatch_candidate_count"] == 0

    outcome = json.loads((output / "outcomes.sidecar.jsonl").read_bytes())
    assert outcome == {
        "app": "unclassified",
        "catalog_index": 1,
        "outcome": "FAILURE",
        "score": 0.0,
        "task_name": "FixtureTask",
    }
    reconstruction = json.loads((output / "reconstruction_refs.jsonl").read_bytes())
    assert [
        exposure["source_step_index"]
        for exposure in reconstruction["steps"][4]["I_t"]["assistant_exposures"]
    ] == [1, 2, 3, 4]
    assert reconstruction["steps"][3]["I_t"]["request_ask_user_messages"] == [
        {
            "content_block_index": None,
            "message_index": 5,
            "observation_step_index": 4,
            "origin_execution_step_index": 3,
            "response": "blue",
        }
    ]
    provider = reconstruction["steps"][4]["P_t"]["provider_vs_decision"]
    assert provider["comparison"]["status"] == "different"
    assert provider["provider_content_exact"].startswith("Provider returned")
    assert provider["decision_prediction_exact"].startswith(
        "<think>I have successfully opened the settings panel"
    )
    long_lag = [
        exposure
        for exposure in reconstruction["steps"][4]["I_t"]["assistant_exposures"]
        if exposure["lag"] >= cards_module.LONG_LAG_MINIMUM
        and exposure["source_evidence_image_absent"]
    ]
    assert [exposure["source_step_index"] for exposure in long_lag] == [1]


def test_qwen_flat_progress_maps_every_ordinal_span_and_external_suffix() -> None:
    events, task_entry, source_entry = _qwen_fixture_events()

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    reconstruction = result["reconstruction"]
    validate_task_cards(
        {result["task_card"]["task"]["task_name"]: result["task_card"]},
        expected_task_count=1,
    )
    exposures_by_step = [step["I_t"]["assistant_exposures"] for step in reconstruction["steps"]]
    assert [len(exposures) for exposures in exposures_by_step] == [0, 1, 2, 3, 4]
    assert [exposure["source_step_index"] for exposure in exposures_by_step[-1]] == [1, 2, 3, 4]
    assert all(
        exposure["representation_type"] == "flat_progress"
        and exposure["mapping_status"] == "exact_qwen_flat_progress"
        for exposures in exposures_by_step
        for exposure in exposures
    )

    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    for request, exposures in zip(requests, exposures_by_step, strict=True):
        text = request["payload"]["request_view"]["messages"][1]["content"][0]["text"]
        for exposure in exposures:
            assert text[exposure["span_start"] : exposure["span_end"]] == exposure["exposed_text"]
            assert (
                text[exposure["assistant_span_start"] : exposure["assistant_span_end"]]
                == exposure["assistant_conclusion_text"]
            )
            assert _sha(exposure["exposed_text"].encode()) == exposure["exposed_text_sha256"]
            step_text = text[exposure["step_span_start"] : exposure["step_span_end"]]
            assert _sha(step_text.encode()) == exposure["step_span_sha256"]

    quoted = exposures_by_step[1][0]
    assert quoted["assistant_conclusion_text"] == (
        "I successfully opened the \\Profile\\ panel.It is ready."
    )
    tool_augmented = exposures_by_step[2][1]
    assert tool_augmented["external_evidence_suffix"] == (
        "; Tool call result: <tool_response>{status: ready}</tool_response>"
    )
    ask_augmented = exposures_by_step[3][2]
    assert ask_augmented["external_evidence_suffix"] == "; Ask user response: blue"

    candidates = result["task_card"]["candidates"]
    assert len(candidates) <= cards_module.MAX_GELAB_FORMAL_CANDIDATES_PER_TASK
    assert candidates
    assert all(
        candidate["claim"]["representation_type"] == "flat_progress" for candidate in candidates
    )
    ask_candidate = next(
        candidate
        for candidate in candidates
        if candidate["claim"]["source_steps"] == [3] and candidate["exposure"]["target_step"] == 4
    )
    assert ask_candidate["claim"]["text"] == (
        "I successfully asked the user for the requested color."
    )
    assert "Ask user response" not in ask_candidate["claim"]["text"]
    assert ask_candidate["exposure"]["span_sha256"] == ask_augmented["exposed_text_sha256"]
    target_request_ref = next(
        ref for ref in ask_candidate["evidence_refs"] if ref["role"] == "target_request"
    )
    assert "Ask user response: blue" in target_request_ref["excerpt"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("renumber", "qwen_progress_content_mismatch"),
        ("altered_byte", "qwen_progress_content_mismatch"),
        ("malformed_prediction", "qwen_prediction_markers_invalid"),
    ],
)
def test_qwen_flat_progress_fails_closed_on_unreconstructable_history(
    mutation: str, expected_code: str
) -> None:
    events, task_entry, source_entry = _qwen_fixture_events()
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    if mutation == "malformed_prediction":
        decisions = sorted(
            (event for event in events if event["event_type"] == "agent_decision"),
            key=lambda event: event["payload"]["step_index"],
        )
        decisions[0]["payload"]["prediction_raw"] = decisions[0]["payload"][
            "prediction_raw"
        ].replace("Action:", "")
    else:
        text_part = requests[-1]["payload"]["request_view"]["messages"][1]["content"][0]
        if mutation == "renumber":
            text_part["text"] = text_part["text"].replace("Step 2:", "Step 9:", 1)
        else:
            text_part["text"] = text_part["text"].replace("unchanged", "changed", 1)

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "prediction",
    [
        (
            'Action: "Open the Profile panel"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"click"}}</tool_call>'
        ),
        (
            "检查个人资料面板\n"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
        ),
        (
            'Action: "The panel is already open!"\n'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
        ),
    ],
)
def test_gui_owl_conclusion_and_punctuation_clone_runtime(prediction: str) -> None:
    runtime_conclusion = runtime_gui_owl.parse_action_to_structure_output(prediction)["conclusion"]

    offline_conclusion = cards_module._gui_owl_prediction_conclusion(
        prediction,
        task_key="fixture",
        source_step=1,
    )

    assert offline_conclusion == runtime_conclusion
    assert cards_module._gui_owl_add_period_robustly(offline_conclusion) == (
        add_period_robustly(runtime_conclusion)
    )


def test_gui_owl_history_n1_maps_all_collapsed_lines_and_aligned_results() -> None:
    events, task_entry, source_entry, blob_bytes = _gui_owl_fixture_events()

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )

    reconstruction = result["reconstruction"]
    exposures_by_step = [step["I_t"]["assistant_exposures"] for step in reconstruction["steps"]]
    assert [len(exposures) for exposures in exposures_by_step] == [0, 1, 2, 3, 4]
    assert result["task_card"]["coverage"]["actual_exposure_count"] == 10
    assert result["task_card"]["coverage"]["unique_history_claim_count"] == 4
    assert all(
        exposure["representation_type"] == "hybrid_folding"
        and exposure["mapping_status"] == "exact_gui_owl_collapsed_history_n1"
        for exposures in exposures_by_step
        for exposure in exposures
    )

    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    for request, exposures in zip(requests, exposures_by_step, strict=True):
        messages = request["payload"]["request_view"]["messages"]
        assert [message["role"] for message in messages] == ["system", "user"]
        assert len(request["payload"]["request_images"]) == 1
        text = messages[1]["content"][0]["text"]
        for exposure in exposures:
            assert text[exposure["span_start"] : exposure["span_end"]] == exposure["exposed_text"]
            assert (
                text[exposure["assistant_span_start"] : exposure["assistant_span_end"]]
                == exposure["rendered_conclusion_text"]
            )
            assert _sha(exposure["exposed_text"].encode()) == exposure["exposed_text_sha256"]

    tool_augmented = exposures_by_step[-1][1]
    assert tool_augmented["source_step_index"] == 2
    assert tool_augmented["aligned_result_kind"] == "tool_call"
    assert tool_augmented["aligned_result_observation_step"] == 3
    assert tool_augmented["external_evidence_suffix"] == (
        " Tool response: {'status': 'ready', 'count': 1}"
    )
    ask_augmented = exposures_by_step[-1][2]
    assert ask_augmented["source_step_index"] == 3
    assert ask_augmented["aligned_result_kind"] == "ask_user_response"
    assert ask_augmented["aligned_result_observation_step"] == 4
    assert ask_augmented["external_evidence_suffix"] == (" Tool response: (Ask_user_response)blue")

    candidates = result["task_card"]["candidates"]
    assert candidates
    assert len(candidates) <= cards_module.MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK
    assert all(
        candidate["claim"]["representation_type"] == "hybrid_folding" for candidate in candidates
    )
    assert all("Tool response:" not in candidate["claim"]["text"] for candidate in candidates)
    target_refs = [
        ref
        for candidate in candidates
        for ref in candidate["evidence_refs"]
        if ref["role"] == "target_request"
    ]
    for ref in target_refs:
        encoded_span = ref["field_path"].rsplit(".text[", maxsplit=1)[1].removesuffix("]")
        span_start, span_end = (int(value) for value in encoded_span.split(":"))
        request_text = requests[ref["step"] - 1]["payload"]["request_view"]["messages"][1][
            "content"
        ][0]["text"]
        assert request_text[span_start:span_end] == ref["excerpt"]
    assert all(
        cards_module._GUI_OWL_ACTION_HISTORY_SIGNAL in candidate["retrieval_reasons"]
        for candidate in candidates
    )
    assert all(
        candidate["claim"]["claim_type"] in {"ACTION_EXECUTION_CLAIM", "SUCCESS_CLAIM"}
        for candidate in candidates
    )

    ask_exposure = exposures_by_step[-1][2]
    assert ask_exposure["source_action_copy_status"] == "parsed_and_execution_started_match"
    assert ask_exposure["action_record_alignment"]["actual_action_type"] == "ask_user"
    assert ask_exposure["action_record_alignment"]["described_operation"] == "ask_user"
    assert ask_exposure["action_record_alignment"]["operation_status"] == "match"
    assert ask_exposure["action_record_alignment"]["status"] == "match"
    assert ask_exposure["action_record_alignment"]["uses_outcome_evidence"] is False
    ask_candidate = next(
        candidate for candidate in candidates if candidate["claim"]["source_steps"] == [3]
    )
    assert "Tool response:" not in ask_candidate["claim"]["text"]
    assert "Tool response:" not in ask_candidate["exposure"]["request_path"]
    ask_request_refs = [
        ref for ref in ask_candidate["evidence_refs"] if ref["role"] == "target_request"
    ]
    assert len(ask_request_refs) == 2
    assert {ref["excerpt"] for ref in ask_request_refs} == {
        "Ask the user which profile color is required.",
        "(Ask_user_response)blue",
    }
    assert len({ref["field_path"] for ref in ask_request_refs}) == 2
    ask_action_refs = [
        ref for ref in ask_candidate["evidence_refs"] if ref["role"] == "source_action"
    ]
    assert len(ask_action_refs) == 2
    assert {ref["field_path"] for ref in ask_action_refs} == {
        "payload.parsed_action.value",
        "payload.action",
    }
    assert len({ref["event_id"] for ref in ask_action_refs}) == 2
    assert len({ref["excerpt"] for ref in ask_action_refs}) == 1
    assert '"action_type":"ask_user"' in ask_action_refs[0]["excerpt"]


@pytest.mark.parametrize(
    ("action_text", "reasons", "expected"),
    [
        ("向下滚动以查找更多文件", [], "ACTION_EXECUTION_CLAIM"),
        ("继续向下滚动以查找更多文件", [], "ACTION_EXECUTION_CLAIM"),
        ("Drag the brightness slider to the right", [], "ACTION_EXECUTION_CLAIM"),
        ("Ask the user for the required color", [], "ACTION_EXECUTION_CLAIM"),
        ("Uncheck the enabled option", [], "ACTION_EXECUTION_CLAIM"),
        ("Swipe upward to reveal more files", [], "ACTION_EXECUTION_CLAIM"),
        ("Press the Save button", [], "ACTION_EXECUTION_CLAIM"),
        ("I will click the Save button next", [], "ACTION_INTENT"),
        ("下一步需要点击保存按钮", [], "ACTION_INTENT"),
        ("The task is complete", ["PROGRESS_CLAIM"], "SUCCESS_CLAIM"),
    ],
)
def test_gui_owl_claim_type_uses_collapsed_action_role(
    action_text: str,
    reasons: list[str],
    expected: str,
) -> None:
    assert cards_module._gui_owl_claim_type(action_text, reasons) == expected


@pytest.mark.parametrize(
    ("action_text", "actual_action_type", "expected_status", "described_operation"),
    [
        ("向下滚动以查找更多文件", "drag", "match", "drag"),
        ("Drag down to reveal more files", "drag", "match", "drag"),
        ("Ask the user for the required login code", "ask_user", "match", "ask_user"),
        ("Uncheck the active option", "click", "match", "click"),
        ("Swipe upward to reveal more files", "drag", "match", "drag"),
        ("Press the Save button", "click", "match", "click"),
        ("Click the back arrow to return", "navigate_back", "match", "click"),
        ("Navigate back to the home screen", "navigate_back", "match", "navigate_back"),
        ("Navigate back to the home screen", "navigate_home", "match", "navigate_back"),
        ("Navigate back to the Messages app", "navigate_home", "mismatch", "navigate_back"),
        ("Drag down to reveal more files", "click", "mismatch", "drag"),
        ("Click the Save button", "wait", "mismatch", "click"),
        ("Enter the list name in the text field", "click", "mismatch", "input_text"),
        ("Enter the username or email", "ask_user", "mismatch", "input_text"),
        ("Enter the site URL in the address bar", "keyboard_enter", "mismatch", "input_text"),
        ("Scroll down to verify the result", "answer", "unresolved", "drag"),
        ("Use the blue answer in the field", "input_text", "unresolved", None),
    ],
)
def test_gui_owl_action_record_alignment_is_anchored_and_outcome_blind(
    action_text: str,
    actual_action_type: str,
    expected_status: str,
    described_operation: str | None,
) -> None:
    alignment = cards_module._gui_owl_action_record_alignment(
        action_text,
        {"action_type": actual_action_type},
    )

    assert alignment["status"] == expected_status
    assert alignment["described_operation"] == described_operation
    assert alignment["uses_outcome_evidence"] is False


def test_gui_owl_action_record_alignment_detects_explicit_text_mismatch() -> None:
    alignment = cards_module._gui_owl_action_record_alignment(
        'Type "Family" in the list name field',
        {"action_type": "input_text", "text": "Friends"},
    )

    assert alignment["operation_status"] == "match"
    assert alignment["text_argument_status"] == "mismatch"
    assert alignment["mismatch_dimensions"] == ["text_argument"]
    assert alignment["status"] == "mismatch"


@pytest.mark.parametrize(
    "action_text",
    [
        'Type "I\'ll be there at 12:30"',
        'Type the message "Do you know what time we\'re meeting?"',
        "Type 'I'll be there at 12:30'",
        "Type 'support-tickets' in the URL bar to check if it's a server",
        "输入消息“我会在12:30到”",
    ],
)
def test_gui_owl_action_record_alignment_preserves_paired_quoted_text(
    action_text: str,
) -> None:
    expected = {
        'Type "I\'ll be there at 12:30"': "I'll be there at 12:30",
        'Type the message "Do you know what time we\'re meeting?"': (
            "Do you know what time we're meeting?"
        ),
        "Type 'I'll be there at 12:30'": "I'll be there at 12:30",
        "Type 'support-tickets' in the URL bar to check if it's a server": "support-tickets",
        "输入消息“我会在12:30到”": "我会在12:30到",
    }[action_text]
    alignment = cards_module._gui_owl_action_record_alignment(
        action_text,
        {"action_type": "input_text", "text": expected},
    )

    assert alignment["explicit_text_argument"] == expected
    assert alignment["text_argument_status"] == "match"
    assert alignment["status"] == "match"


def test_gui_owl_mechanical_mismatch_is_retained_as_an_immediate_candidate() -> None:
    events, task_entry, source_entry, blob_bytes = _gui_owl_fixture_events()
    prediction = (
        'Action: "Drag down to reveal more files"\n'
        '<tool_call>{"name":"mobile_use","arguments":{"action":"click",'
        '"coordinate":[100,200]}}</tool_call>'
    )
    _replace_gui_owl_source_turn(
        events,
        source_step=2,
        prediction=prediction,
        action={"action_type": "click", "x": 103, "y": 204},
    )

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )

    mismatch = next(
        candidate
        for candidate in result["task_card"]["candidates"]
        if cards_module._GUI_OWL_ACTION_MISMATCH_SIGNAL in candidate["retrieval_reasons"]
    )
    assert mismatch["claim"]["source_steps"] == [2]
    assert mismatch["exposure"]["target_step"] == 3
    exposure = result["reconstruction"]["steps"][2]["I_t"]["assistant_exposures"][1]
    assert exposure["action_record_alignment"]["mismatch_dimensions"] == ["operation"]
    assert exposure["action_record_alignment"]["actual_action_type"] == "click"


def test_gui_owl_cards_do_not_read_task_outcome() -> None:
    events, task_entry, source_entry, blob_bytes = _gui_owl_fixture_events()
    failure = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )
    success_events = copy.deepcopy(events)
    task_ended = next(event for event in success_events if event["event_type"] == "task_ended")
    task_ended["payload"]["environment_evaluation"] = {
        "score": 1.0,
        "reason": "private changed outcome",
    }
    success = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=success_events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )

    assert success["task_card"] == failure["task_card"]
    assert success["reconstruction"] == failure["reconstruction"]
    assert success["outcome_sidecar"] != failure["outcome_sidecar"]


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("template", "gui_owl_history_prompt_template_mismatch"),
        ("result", "gui_owl_result_alignment_mismatch"),
        ("raw_pair", "gui_owl_request_messages_invalid"),
        ("extra_image", "gui_owl_request_image_count_invalid"),
        ("historical_image", "gui_owl_current_image_mismatch"),
    ],
)
def test_gui_owl_history_n1_fails_closed_on_nonformal_or_misaligned_request(
    mutation: str,
    expected_code: str,
) -> None:
    events, task_entry, source_entry, blob_bytes = _gui_owl_fixture_events()
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    final_request = requests[-1]["payload"]
    if mutation == "template":
        text_part = final_request["request_view"]["messages"][1]["content"][0]
        text_part["text"] = text_part["text"].replace("Previous actions:", "History:", 1)
    elif mutation == "result":
        text_part = final_request["request_view"]["messages"][1]["content"][0]
        text_part["text"] = text_part["text"].replace(
            "Tool response: {'status': 'ready', 'count': 1}",
            "Tool response: {'status': 'wrong', 'count': 1}",
            1,
        )
    elif mutation == "raw_pair":
        final_request["request_view"]["messages"].insert(
            1,
            {"role": "assistant", "content": "raw history is not formal history_n=1"},
        )
    elif mutation == "extra_image":
        final_request["request_images"].append(copy.deepcopy(final_request["request_images"][0]))
    else:
        first_request = requests[0]["payload"]
        historical = copy.deepcopy(first_request["request_images"][0])
        final_request["request_images"][0] = historical
        externalized = final_request["request_view"]["messages"][1]["content"][1]["image_url"][
            "url"
        ]["$externalized_data_url"]
        externalized["content_blob"] = historical["content_blob"]
        externalized["original_text_blob"] = historical["original_text_blob"]

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == expected_code


def test_gui_owl_adapter_provenance_must_not_conflict() -> None:
    events, task_entry, source_entry, blob_bytes = _gui_owl_fixture_events()
    source_entry["provenance"]["agent_type"] = "qwen3vl"

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == "adapter_provenance_mismatch"


def test_memgui_parser_and_hlm_replay_match_runtime_semantics() -> None:
    events, _, _, _ = _memgui_fixture_events()
    cores = _memgui_cores_from_events(events)
    for core in cores:
        step = core["step_index"]
        runtime = runtime_memgui.parse_memgui_response(
            core["prediction"],
            image_height=6,
            image_width=4,
            current_step=step,
        )
        offline = cards_module._parse_memgui_accepted_prediction(
            core["prediction"],
            current_step=step,
            task_key="fixture",
        )
        assert offline["folding_directive"] == runtime["folding_directive"]
        assert offline["memory_args"] == runtime["memory_args"]
        assert offline["ui_observation"] == runtime["ui_observation"]
        assert offline["action_intent"] == runtime["action_intent"]

    state1 = cards_module._replay_memgui_state(
        cores,
        stop_position=1,
        task_key="fixture",
    )
    assert state1["summaries"] == []  # legal step-one folding is parsed but ignored
    assert list(state1["memory"]) == ["alpha"]

    state2 = cards_module._replay_memgui_state(
        cores,
        stop_position=2,
        task_key="fixture",
    )
    alpha = state2["memory"]["alpha"]
    assert alpha["description"] == "Original description"
    assert alpha["description_source_step"] == 1
    assert alpha["content_source_step"] == 2
    assert state2["summaries"][0]["source_core"]["step_index"] == 2
    assert state2["summaries"][0]["fold_range"] == (1, 1)

    state3 = cards_module._replay_memgui_state(
        cores,
        stop_position=3,
        task_key="fixture",
    )
    assert [record["fold_range"] for record in state3["summaries"]] == [(1, 1), (3, 3)]
    assert state3["summaries"][0]["summary"] == state3["summaries"][1]["summary"]
    assert list(state3["memory"]) == ["alpha", "beta"]

    state4 = cards_module._replay_memgui_state(
        cores,
        stop_position=4,
        task_key="fixture",
    )
    assert [record["fold_range"] for record in state4["summaries"]] == [(1, 1), (2, 3)]
    assert [record["source_core"]["step_index"] for record in state4["summaries"]] == [
        2,
        4,
    ]
    assert list(state4["memory"]) == ["beta"]
    assert state4["latest"]["ui_observation"] == ""
    assert state4["latest"]["action_intent"] == ""

    state5 = cards_module._replay_memgui_state(
        cores,
        stop_position=5,
        task_key="fixture",
    )
    # [1,2] destroys both [1,1] and the partially overlapping [2,3].
    assert [record["fold_range"] for record in state5["summaries"]] == [(1, 2)]
    assert state5["summaries"][0]["source_core"]["step_index"] == 5

    readd = _synthetic_memgui_core(
        6,
        (
            '<thinking>Re-add alpha.</thinking><folding>{"range":[5,5],'
            '"summary":"[Step 5] Finished."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"alpha","description":"New alpha",'
            '"content":"re-added"}}</tool_call>'
            "<ui_observation>Done.</ui_observation><action_intent>Store.</action_intent>"
        ),
        action_type="wait",
    )
    state6 = cards_module._replay_memgui_state(
        [*cores, readd],
        stop_position=6,
        task_key="fixture",
    )
    assert list(state6["memory"]) == ["beta", "alpha"]


def test_memgui_l_uses_normalized_action_type_without_tool_arguments() -> None:
    swipe = _synthetic_memgui_core(
        1,
        (
            "<thinking>Swipe.</thinking>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"swipe",'
            '"coordinate":[1,2],"coordinate2":[3,4]}}</tool_call>'
            "<ui_observation></ui_observation><action_intent></action_intent>"
        ),
        action_type="drag",
    )
    state1 = cards_module._replay_memgui_state(
        [swipe],
        stop_position=1,
        task_key="fixture",
    )
    rendered1 = cards_module._render_memgui_latest(state1["latest"])
    assert rendered1["text"] == "  Step 1:\n    Action Taken: drag"
    assert rendered1["records"][0]["actor_claim_text"] == ""

    typed = _synthetic_memgui_core(
        2,
        (
            '<thinking>Type.</thinking><folding>{"range":[1,1],'
            '"summary":"[Step 1] Swiped."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"type",'
            '"text":"secret text","coordinate":[5,6]}}</tool_call>'
            "<ui_observation>Field visible.</ui_observation>"
            "<action_intent>Type the value.</action_intent>"
        ),
        action_type="input_text",
    )
    state2 = cards_module._replay_memgui_state(
        [swipe, typed],
        stop_position=2,
        task_key="fixture",
    )
    rendered2 = cards_module._render_memgui_latest(state2["latest"])
    assert "Action Taken: input_text" in rendered2["text"]
    assert "secret text" not in rendered2["text"]
    assert "[5,6]" not in rendered2["text"]


def test_memgui_parser_uses_first_complete_case_insensitive_tags_in_any_order() -> None:
    prediction = (
        "<ACTION_INTENT>first intent</ACTION_INTENT>"
        "<UI_OBSERVATION>first observation</UI_OBSERVATION>"
        '<TOOL_CALL>{"name":"mobile_use","arguments":{"action":"click",'
        '"coordinate":[100,200]}}</TOOL_CALL>'
        "<THINKING>reason</THINKING>"
        "<ui_observation>second observation</ui_observation>"
        "<action_intent>second intent</action_intent>"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
        '"memory_id":"wrong","content":"wrong"}}</tool_call>'
    )
    runtime = runtime_memgui.parse_memgui_response(
        prediction,
        image_height=6,
        image_width=4,
        current_step=1,
    )
    offline = cards_module._parse_memgui_accepted_prediction(
        prediction,
        current_step=1,
        task_key="fixture",
    )
    assert runtime["ui_observation"] == offline["ui_observation"] == "first observation"
    assert runtime["action_intent"] == offline["action_intent"] == "first intent"
    assert runtime["action_json"]["action"] == offline["action_type"] == "click"


def test_memgui_memory_keeps_full_unicode_content_but_l_uses_exact_preview() -> None:
    content = "界" * 450 + "\n" + "value" * 100
    tool_call = json.dumps(
        {
            "name": "mobile_use",
            "arguments": {
                "action": "memory_add",
                "memory_id": "long",
                "description": "Unicode",
                "content": content,
            },
        }
    )
    core = _synthetic_memgui_core(
        1,
        (
            "<thinking>Store long content.</thinking>"
            f"<tool_call>{tool_call}</tool_call>"
            "<ui_observation>Long value visible.</ui_observation>"
            "<action_intent>Store it.</action_intent>"
        ),
        action_type="wait",
    )
    state = cards_module._replay_memgui_state(
        [core],
        stop_position=1,
        task_key="fixture",
    )
    latest = cards_module._render_memgui_latest(state["latest"])
    memory = cards_module._render_memgui_memory(state["memory"])
    assert f"Memory: Added memory [long]: Unicode | {content[:50]}..." in latest["text"]
    assert content in memory["text"]
    assert memory["records"][0]["actor_claim_text"].endswith(content)
    target = _synthetic_memgui_core(
        2,
        (
            '<thinking>Wait.</thinking><folding>{"range":[1,1],'
            '"summary":"[Step 1] Stored long content."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
            "<ui_observation>Waiting.</ui_observation><action_intent>Wait.</action_intent>"
        ),
        action_type="wait",
    )
    record = memory["records"][0]
    exposure = {
        "mapping_status": "exact_memgui_structured_hlm",
        "representation_type": "structured_folding",
        "history_section": "M",
        "history_entry_id": record["history_entry_id"],
        "message_index": 1,
        "content_block_index": 0,
        "actor_claim_text": record["actor_claim_text"],
        "exposed_text": record["exposed_text"],
        "span_start": 0,
        "span_end": len(record["exposed_text"]),
    }
    candidate = cards_module._formal_candidate(
        task_key="fixture",
        source_core=core,
        target_core=target,
        exposure=exposure,
        retrieval_reasons=["STRUCTURED_MEMORY_ENTRY"],
    )
    assert len(candidate["claim"]["text"]) > 800
    assert candidate["claim"]["text"] == record["actor_claim_text"]


def test_memgui_nonblank_update_replaces_description_without_reordering_memory() -> None:
    predictions = [
        (
            "<thinking>Add alpha.</thinking>"
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"alpha","description":"Old","content":"one"}}</tool_call>'
            "<ui_observation>A.</ui_observation><action_intent>Add.</action_intent>"
        ),
        (
            '<thinking>Add beta.</thinking><folding>{"range":[1,1],'
            '"summary":"[Step 1] Added alpha."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_add",'
            '"memory_id":"beta","description":"Beta","content":"two"}}</tool_call>'
            "<ui_observation>B.</ui_observation><action_intent>Add.</action_intent>"
        ),
        (
            '<thinking>Update alpha.</thinking><folding>{"range":[2,2],'
            '"summary":"[Step 2] Added beta."}</folding>'
            '<tool_call>{"name":"mobile_use","arguments":{"action":"memory_update",'
            '"memory_id":"alpha","description":"New","content":"three"}}</tool_call>'
            "<ui_observation>C.</ui_observation><action_intent>Update.</action_intent>"
        ),
    ]
    cores = [
        _synthetic_memgui_core(step, prediction, action_type="wait")
        for step, prediction in enumerate(predictions, start=1)
    ]
    state = cards_module._replay_memgui_state(
        cores,
        stop_position=3,
        task_key="fixture",
    )
    assert list(state["memory"]) == ["alpha", "beta"]
    assert state["memory"]["alpha"]["description"] == "New"
    assert state["memory"]["alpha"]["description_source_step"] == 3
    assert state["memory"]["alpha"]["content_source_step"] == 3


def test_memgui_reconstructs_exact_entry_ledger_and_actor_claim_boundaries() -> None:
    events, task_entry, source_entry, blob_bytes = _memgui_fixture_events()
    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=list(reversed(events)),
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )
    reconstruction = result["reconstruction"]
    exposures_by_step = [step["I_t"]["assistant_exposures"] for step in reconstruction["steps"]]
    assert [len(exposures) for exposures in exposures_by_step] == [0, 2, 3, 5, 4]
    all_exposures = [exposure for exposures in exposures_by_step for exposure in exposures]
    assert all(
        exposure["mapping_status"] == "exact_memgui_structured_hlm"
        and exposure["representation_type"] == "structured_folding"
        for exposure in all_exposures
    )
    # The fixture deliberately uses different PNG compression for request and
    # observation blobs; pixel-equal post-state evidence must still be present.
    assert all(
        exposure["post_observation_image_present"] is True
        and exposure["source_evidence_image_absent"] is False
        for exposure in exposures_by_step[1]
    )

    same_pair = [
        exposure for exposure in exposures_by_step[2] if exposure["source_step_index"] == 2
    ]
    assert [exposure["history_section"] for exposure in same_pair] == ["H", "L", "M"]
    assert len({exposure["history_entry_id"] for exposure in same_pair}) == 3
    raw_cores = _memgui_cores_from_events(events)
    same_pair_candidates = [
        cards_module._formal_candidate(
            task_key=reconstruction["task_key"],
            source_core=raw_cores[1],
            target_core=raw_cores[2],
            exposure=exposure,
            retrieval_reasons=["PROGRESS_CLAIM"],
            claim_source_cores=(
                [raw_cores[0], raw_cores[1]]
                if exposure["history_section"] == "M"
                else [raw_cores[1]]
            ),
        )
        for exposure in same_pair
    ]
    assert len({candidate["candidate_id"] for candidate in same_pair_candidates}) == 3
    assert len({candidate["exposure"]["request_path"] for candidate in same_pair_candidates}) == 3
    assert all(
        candidate["claim"]["text"] == exposure["actor_claim_text"]
        for candidate, exposure in zip(same_pair_candidates, same_pair, strict=True)
    )
    latest = next(exposure for exposure in same_pair if exposure["history_section"] == "L")
    assert "Action Taken: Memory: Updated memory [alpha]" in latest["exposed_text"]
    assert "Action Taken" not in latest["actor_claim_text"]

    updated_memory = next(exposure for exposure in same_pair if exposure["history_section"] == "M")
    assert updated_memory["memory_description_source_step"] == 1
    assert updated_memory["memory_content_source_step"] == 2
    assert updated_memory["source_step_index"] == 2

    duplicate_h = [
        exposure for exposure in exposures_by_step[3] if exposure["history_section"] == "H"
    ]
    assert len(duplicate_h) == 2
    assert duplicate_h[0]["actor_claim_text"] == duplicate_h[1]["actor_claim_text"]
    request_text = reconstruction["steps"][3]["I_t"]
    request_event = next(event for event in events if event["event_id"] == request_text["event_id"])
    prompt = request_event["payload"]["request_view"]["messages"][1]["content"][0]["text"]
    for exposure in duplicate_h:
        assert prompt[exposure["span_start"] : exposure["span_end"]] == exposure["exposed_text"]

    empty_l = next(
        exposure for exposure in exposures_by_step[4] if exposure["history_section"] == "L"
    )
    assert empty_l["actor_claim_text"] == ""
    assert empty_l["exposed_text"].endswith("Action Taken: Memory: Deleted memory [alpha]")

    span_h = next(
        exposure
        for exposure in exposures_by_step[4]
        if exposure["history_section"] == "H" and exposure["fold_range"] == [2, 3]
    )
    span_candidate = cards_module._formal_candidate(
        task_key=reconstruction["task_key"],
        source_core=raw_cores[3],
        target_core=raw_cores[4],
        exposure=span_h,
        retrieval_reasons=["STRUCTURED_SPAN_FOLD"],
    )
    assert span_candidate["claim"]["claim_type"] == "SUMMARY_CLAIM"
    assert cards_module._memgui_claim_type(updated_memory, ["STRUCTURED_MEMORY_ENTRY"]) == (
        "OBSERVATION_CLAIM"
    )
    assert (
        cards_module._memgui_claim_type(
            {
                "history_section": "L",
                "latest_ui_observation_text": "",
                "latest_action_intent_text": "Do it",
            },
            [],
        )
        == "ACTION_INTENT"
    )

    card = result["task_card"]
    assert card["coverage"]["actual_exposure_count"] == 14
    assert card["coverage"]["unique_history_claim_count"] == 10
    assert len(card["candidates"]) <= cards_module.MAX_MEMGUI_FORMAL_CANDIDATES_PER_TASK
    assert all(
        candidate["claim"]["representation_type"] == "structured_folding"
        and candidate["claim"]["text"]
        for candidate in card["candidates"]
    )
    memory_candidates = [
        candidate
        for candidate in card["candidates"]
        if "STRUCTURED_MEMORY_ENTRY" in candidate["retrieval_reasons"]
    ]
    assert len(memory_candidates) == 3
    updated_candidate = next(
        candidate
        for candidate in memory_candidates
        if "I have successfully updated the second value" in candidate["claim"]["text"]
    )
    assert updated_candidate["claim"]["source_steps"] == [1, 2]
    assert {
        ref["step"]
        for ref in updated_candidate["evidence_refs"]
        if ref["role"] == "source_prediction"
    } == {1, 2}


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("first_history", "memgui_structured_prompt_mismatch"),
        ("folding_instruction", "memgui_structured_prompt_mismatch"),
        ("prompt_byte", "memgui_structured_prompt_mismatch"),
        ("extra_image_record", "memgui_request_image_count_invalid"),
        ("historical_image", "memgui_current_image_mismatch"),
        ("request_view_provenance", "memgui_request_image_provenance_mismatch"),
        ("wrong_image_path", "memgui_request_image_path_invalid"),
        ("wrong_system", "memgui_system_message_invalid"),
        ("wrong_role", "memgui_user_message_invalid"),
        ("swapped_messages", "memgui_system_message_invalid"),
        ("extra_content", "memgui_user_message_invalid"),
        ("no_blob_reader", "memgui_blob_reader_required"),
        ("wrong_component", "memgui_request_provenance_invalid"),
        ("provider_difference", "memgui_provider_decision_mismatch"),
    ],
)
def test_memgui_fails_closed_on_prompt_provenance_or_image_mismatch(
    mutation: str,
    expected_code: str,
) -> None:
    events, task_entry, source_entry, blob_bytes = _memgui_fixture_events()
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    if mutation == "first_history":
        text = requests[0]["payload"]["request_view"]["messages"][1]["content"][0]
        text["text"] = text["text"].replace(
            "  (no previous steps)",
            "  [Step 99] inherited from another task",
        )
    elif mutation == "folding_instruction":
        text = requests[1]["payload"]["request_view"]["messages"][1]["content"][0]
        text["text"] = text["text"].replace(
            "Output <folding> to compress your previous step(s)",
            "Skip <folding> for the first step",
        )
    elif mutation == "prompt_byte":
        text = requests[2]["payload"]["request_view"]["messages"][1]["content"][0]
        text["text"] = text["text"].replace("Original description", "Altered description")
    elif mutation == "extra_image_record":
        requests[2]["payload"]["request_images"].append(
            copy.deepcopy(requests[2]["payload"]["request_images"][0])
        )
    elif mutation == "historical_image":
        historical_record = copy.deepcopy(requests[0]["payload"]["request_images"][0])
        requests[2]["payload"]["request_images"] = [historical_record]
        historical_view = copy.deepcopy(
            requests[0]["payload"]["request_view"]["messages"][1]["content"][1]
        )
        requests[2]["payload"]["request_view"]["messages"][1]["content"][1] = historical_view
    elif mutation == "request_view_provenance":
        externalized = requests[2]["payload"]["request_view"]["messages"][1]["content"][1][
            "image_url"
        ]["url"]["$externalized_data_url"]
        externalized["media_type"] = "image/jpeg"
    elif mutation == "wrong_image_path":
        requests[2]["payload"]["request_images"][0]["content_path"] = (
            "messages[0].content[1].image_url.url"
        )
    elif mutation == "wrong_system":
        requests[0]["payload"]["request_view"]["messages"][0]["content"][0]["text"] += " "
    elif mutation == "wrong_role":
        requests[0]["payload"]["request_view"]["messages"][1]["role"] = "assistant"
    elif mutation == "swapped_messages":
        messages = requests[0]["payload"]["request_view"]["messages"]
        messages[0], messages[1] = messages[1], messages[0]
    elif mutation == "extra_content":
        requests[0]["payload"]["request_view"]["messages"][1]["content"].append(
            {"type": "text", "text": "unexpected"}
        )
    elif mutation == "no_blob_reader":
        pass
    elif mutation == "wrong_component":
        requests[0]["payload"]["component"] = "another.adapter"
    else:
        response = next(
            event
            for event in events
            if event["event_type"] == "model_response" and event["payload"]["step_index"] == 1
        )
        response["payload"]["normalized_response"]["choices"][0]["content"] += " "

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=(
                None if mutation == "no_blob_reader" else _ui_venus_blob_reader(blob_bytes)
            ),
            **_identity(),
        )
    assert raised.value.code == expected_code


def test_memgui_rejected_outer_retries_do_not_enter_hlm_state() -> None:
    events, task_entry, source_entry, blob_bytes = _memgui_fixture_events()
    _insert_memgui_rejected_retries(events, step=2)
    decision = next(
        event
        for event in events
        if event["event_type"] == "agent_decision" and event["payload"]["step_index"] == 2
    )
    selected_response = next(
        event for event in events if event["event_id"] == decision["caused_by_event_id"]
    )
    assert len(decision["payload"]["source_model_call_ids"]) == 3
    assert (
        decision["payload"]["source_model_call_ids"][-1]
        == selected_response["payload"]["model_call_id"]
    )
    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )
    serialized = json.dumps(result["reconstruction"], sort_keys=True)
    assert "REJECTED HISTORY" not in serialized
    assert "REJECTED MEMORY" not in serialized
    assert "rejected" not in {
        exposure.get("memory_id")
        for step in result["reconstruction"]["steps"]
        for exposure in step["I_t"]["assistant_exposures"]
    }


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("request_bytes", "memgui_retry_request_mismatch"),
        ("accepted_not_last", "memgui_decision_provenance_mismatch"),
        ("response_parent", "memgui_retry_response_causality_invalid"),
    ],
)
def test_memgui_retry_provenance_fails_closed(
    mutation: str,
    expected_code: str,
) -> None:
    events, task_entry, source_entry, blob_bytes = _memgui_fixture_events()
    _insert_memgui_rejected_retries(events, step=2)
    decision = next(
        event
        for event in events
        if event["event_type"] == "agent_decision" and event["payload"]["step_index"] == 2
    )
    source_ids = decision["payload"]["source_model_call_ids"]
    rejected_request = next(
        event
        for event in events
        if event["event_type"] == "model_request"
        and event["payload"]["model_call_id"] == source_ids[0]
    )
    if mutation == "request_bytes":
        rejected_request["payload"]["request_view"]["messages"][1]["content"][0]["text"] += (
            " altered"
        )
    elif mutation == "accepted_not_last":
        decision["payload"]["source_model_call_ids"] = [source_ids[1], source_ids[2], source_ids[0]]
    else:
        rejected_response = next(
            event
            for event in events
            if event["event_type"] == "model_response"
            and event["payload"]["model_call_id"] == source_ids[1]
        )
        rejected_response["caused_by_event_id"] = next(
            event
            for event in events
            if event["event_type"] == "model_request"
            and event["payload"]["model_call_id"] == source_ids[0]
        )["event_id"]
    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == expected_code


def test_memgui_entry_aware_attachment_keeps_h_l_m_and_exact_long_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_exposures = [
        {
            "mapping_status": "exact_memgui_structured_hlm",
            "representation_type": "structured_folding",
            "history_section": section,
            "history_entry_id": f"entry-{section}",
            "source_step_index": 1,
            "actor_claim_text": "I have successfully stored the value.",
            **({"fold_range": [1, 2]} if section == "H" else {}),
            **(
                {
                    "memory_description_source_step": 1,
                    "memory_content_source_step": 1,
                }
                if section == "M"
                else {}
            ),
        }
        for section in ("H", "L", "M")
    ]
    cores = [
        {"step_index": 1, "assistant_exposures": []},
        {"step_index": 2, "assistant_exposures": entry_exposures},
    ]
    captured: list[dict[str, Any]] = []

    def fake_formal_candidate(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"candidate_id": kwargs["exposure"]["history_entry_id"]}

    monkeypatch.setattr(cards_module, "_formal_candidate", fake_formal_candidate)
    result = cards_module._formal_memgui_candidates(
        "fixture",
        cores,
        [
            {
                "signal": "PROGRESS_CLAIM",
                "source_step": 1,
                "target_step": 1,
                "details": {},
            },
            {
                "signal": "LONG_LAG_IMAGE_ABSENT",
                "source_step": 1,
                "target_step": 2,
                "details": {"history_entry_id": "entry-H"},
            },
        ],
    )
    assert {candidate["candidate_id"] for candidate in result} == {
        "entry-H",
        "entry-L",
        "entry-M",
    }
    reasons = {
        call["exposure"]["history_entry_id"]: set(call["retrieval_reasons"]) for call in captured
    }
    assert "LONG_LAG_IMAGE_ABSENT" in reasons["entry-H"]
    assert "LONG_LAG_IMAGE_ABSENT" not in reasons["entry-L"]
    assert "LONG_LAG_IMAGE_ABSENT" not in reasons["entry-M"]


def test_memgui_selector_is_deterministic_bounded_and_prioritizes_m_and_span_h() -> None:
    keys = [(index, index + 1, f"entry-{index}") for index in range(1, 9)]
    entries: dict[tuple[int, int, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    reasons: dict[tuple[int, int, str], set[str]] = {}
    for index, key in enumerate(keys):
        if index < 2:
            section = "M"
            reason = "STRUCTURED_MEMORY_ENTRY"
            exposure: dict[str, Any] = {"history_section": section}
        elif index < 6:
            section = "H"
            reason = "STRUCTURED_SPAN_FOLD"
            exposure = {"history_section": section, "fold_range": [1, 2]}
        else:
            section = "L"
            reason = "PROGRESS_CLAIM"
            exposure = {"history_section": section}
        entries[key] = ({"step_index": key[1]}, exposure)
        reasons[key] = {reason}
    selected = cards_module._select_memgui_formal_candidate_entries(
        keys,
        reasons,
        entries,
        step_count=10,
    )
    reversed_keys = list(reversed(keys))
    selected_reversed = cards_module._select_memgui_formal_candidate_entries(
        reversed_keys,
        {key: reasons[key] for key in reversed_keys},
        {key: entries[key] for key in reversed_keys},
        step_count=10,
    )
    assert selected == selected_reversed
    assert len(selected) == cards_module.MAX_MEMGUI_FORMAL_CANDIDATES_PER_TASK == 4
    assert set(keys[:2]) <= set(selected)
    assert all(entries[key][1]["history_section"] == "H" for key in selected[2:])


def test_memgui_adapter_provenance_must_not_conflict() -> None:
    events, task_entry, source_entry, blob_bytes = _memgui_fixture_events()
    source_entry["provenance"]["agent_type"] = "ui_venus_agent"
    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == "adapter_provenance_mismatch"


@pytest.mark.parametrize(
    "prediction",
    [
        "<think> inspect </think><action> Click(box=(1,2)) </action><conclusion>x</conclusion>",
        "PressBack()",
        "<think>parser failed</think><action>NotAnAction()</action>",
        "<think>empty action falls back</think><action>   </action>",
        "<THINK>uppercase tags are not runtime tags</THINK><ACTION>Wait()</ACTION>",
    ],
)
def test_ui_venus_history_field_clone_matches_runtime_extraction(prediction: str) -> None:
    expected_think = runtime_ui_venus._extract_tag_content("think", prediction) or ""
    expected_action = (
        runtime_ui_venus._extract_tag_content("action", prediction) or prediction.strip()
    )

    assert cards_module._ui_venus_history_fields(prediction) == (
        expected_think,
        expected_action,
    )


def test_ui_venus_flat_previous_actions_maps_every_span_and_runtime_history_case() -> None:
    events, task_entry, source_entry, blob_bytes = _ui_venus_fixture_events()

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )

    reconstruction = result["reconstruction"]
    exposures_by_step = [step["I_t"]["assistant_exposures"] for step in reconstruction["steps"]]
    assert [len(exposures) for exposures in exposures_by_step] == [0, 1, 2, 3, 4]
    final_exposures = exposures_by_step[-1]
    assert [exposure["source_step_index"] for exposure in final_exposures] == [1, 2, 3, 4]
    assert [exposure["history_ordinal"] for exposure in final_exposures] == [0, 1, 2, 3]
    assert [exposure["lag"] for exposure in final_exposures] == [4, 3, 2, 1]
    assert all(
        exposure["representation_type"] == "flat_previous_actions"
        and exposure["mapping_status"] == "exact_ui_venus_flat_previous_actions"
        for exposures in exposures_by_step
        for exposure in exposures
    )

    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    steps = sorted(
        (event for event in events if event["event_type"] == "step_started"),
        key=lambda event: event["payload"]["step_index"],
    )
    for request, step, exposures in zip(requests, steps, exposures_by_step, strict=True):
        text = request["payload"]["request_view"]["messages"][1]["content"][0]["text"]
        images = request["payload"]["request_images"]
        assert len(images) == 1
        assert (
            images[0]["content_blob"]["digest"]
            != step["payload"]["observation"]["screenshot"]["pixel_blob"]["digest"]
        )
        for exposure in exposures:
            assert (
                text[exposure["span_start"] : exposure["span_end"]]
                == exposure["assistant_history_text"]
            )
            step_text = text[exposure["step_span_start"] : exposure["step_span_end"]]
            assert _sha(step_text.encode()) == exposure["step_span_sha256"]
            assert "conclusion" not in exposure
            assert "status" not in exposure
            assert "<conclusion>" not in exposure["assistant_history_text"]

    tagged = exposures_by_step[1][0]
    assert tagged["assistant_think_text"] == "I have successfully opened the profile panel."
    assert tagged["assistant_action_text"] == "Click(box=(100,200))"
    bare = exposures_by_step[2][1]
    assert bare["assistant_think_text"] == ""
    assert bare["assistant_action_text"] == "PressBack()"
    parse_failed = exposures_by_step[3][2]
    assert parse_failed["assistant_think_text"] == (
        "Parser failed, but this step remains in history."
    )
    assert parse_failed["assistant_action_text"] == "NotAnAction()"
    assert (
        exposures_by_step[-1][0]["assistant_history_sha256"]
        == exposures_by_step[-1][3]["assistant_history_sha256"]
    )
    assert (
        exposures_by_step[-1][0]["source_decision_event_id"]
        != exposures_by_step[-1][3]["source_decision_event_id"]
    )

    candidates = result["task_card"]["candidates"]
    assert candidates
    assert all(
        candidate["claim"]["representation_type"] == "flat_previous_actions"
        for candidate in candidates
    )
    assert all("<conclusion>" not in candidate["claim"]["text"] for candidate in candidates)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("renumber", "ui_venus_previous_actions_content_mismatch"),
        ("altered_byte", "ui_venus_previous_actions_content_mismatch"),
        ("extra_image", "ui_venus_request_image_count_invalid"),
        ("historical_image", "ui_venus_current_image_mismatch"),
        ("request_view_provenance", "ui_venus_request_image_provenance_mismatch"),
        ("wrong_image_path", "ui_venus_request_image_path_invalid"),
        ("wrong_system", "ui_venus_system_message_invalid"),
    ],
)
def test_ui_venus_fails_closed_on_template_or_current_image_mismatch(
    mutation: str, expected_code: str
) -> None:
    events, task_entry, source_entry, blob_bytes = _ui_venus_fixture_events()
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    final_request = requests[-1]["payload"]
    if mutation in {"renumber", "altered_byte"}:
        text_part = final_request["request_view"]["messages"][1]["content"][0]
        if mutation == "renumber":
            text_part["text"] = text_part["text"].replace("Step 1:", "Step 9:", 1)
        else:
            text_part["text"] = text_part["text"].replace("profile panel", "account panel", 1)
    elif mutation == "extra_image":
        final_request["request_images"].append(copy.deepcopy(final_request["request_images"][0]))
    elif mutation == "historical_image":
        first_digest = requests[0]["payload"]["request_images"][0]["content_blob"]
        final_request["request_images"][0]["content_blob"] = copy.deepcopy(first_digest)
        view_image = final_request["request_view"]["messages"][1]["content"][1]["image_url"]["url"][
            "$externalized_data_url"
        ]
        view_image["content_blob"] = copy.deepcopy(first_digest)
    elif mutation == "request_view_provenance":
        first_digest = requests[0]["payload"]["request_images"][0]["content_blob"]
        view_image = final_request["request_view"]["messages"][1]["content"][1]["image_url"]["url"][
            "$externalized_data_url"
        ]
        view_image["content_blob"] = copy.deepcopy(first_digest)
    elif mutation == "wrong_image_path":
        final_request["request_images"][0]["content_path"] = "messages[0].content[0].image_url.url"
    else:
        final_request["request_view"]["messages"][0]["content"] = "different system"

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == expected_code


def test_ui_venus_provider_difference_is_recorded_without_changing_history_source() -> None:
    events, task_entry, source_entry, blob_bytes = _ui_venus_fixture_events()
    responses = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    responses[0]["payload"]["normalized_response"]["choices"][0]["content"] = (
        "provider-only text that the runtime decision did not retain"
    )

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        blob_reader=_ui_venus_blob_reader(blob_bytes),
        **_identity(),
    )

    comparison = result["reconstruction"]["steps"][0]["P_t"]["provider_vs_decision"]
    assert comparison["comparison"]["status"] == "different"
    exposure = result["reconstruction"]["steps"][1]["I_t"]["assistant_exposures"][0]
    assert exposure["assistant_action_text"] == "Click(box=(100,200))"
    assert any(
        candidate["claim"]["source_steps"] == [1]
        and candidate["exposure"]["target_step"] == 2
        and "PROVIDER_DECISION_DIFFERENCE" in candidate["retrieval_reasons"]
        for candidate in result["task_card"]["candidates"]
    )


def test_ui_venus_adapter_provenance_must_not_conflict() -> None:
    events, task_entry, source_entry, blob_bytes = _ui_venus_fixture_events()
    source_entry["provenance"]["agent_type"] = "qwen3vl"

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            blob_reader=_ui_venus_blob_reader(blob_bytes),
            **_identity(),
        )
    assert raised.value.code == "adapter_provenance_mismatch"


def test_gelab_rolling_summary_maps_exact_span_and_external_suffix() -> None:
    events, task_entry, source_entry = _gelab_fixture_events()

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    reconstruction = result["reconstruction"]
    exposures_by_step = [step["I_t"]["assistant_exposures"] for step in reconstruction["steps"]]
    assert [len(exposures) for exposures in exposures_by_step] == [0, 1, 0, 1, 1]
    assert [
        exposures[0]["source_step_index"] if exposures else None for exposures in exposures_by_step
    ] == [None, 1, None, 3, 4]
    assert all(
        exposure["representation_type"] == "rolling_summary"
        and exposure["mapping_status"] == "exact_gelab_rolling_summary"
        and exposure["lag"] == 1
        for exposures in exposures_by_step
        for exposure in exposures
    )

    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    for request, exposures in zip(requests, exposures_by_step, strict=True):
        text = request["payload"]["request_view"]["messages"][0]["content"][1]["text"]
        for exposure in exposures:
            assert text[exposure["span_start"] : exposure["span_end"]] == exposure["exposed_text"]
            assert (
                text[exposure["assistant_span_start"] : exposure["assistant_span_end"]]
                == exposure["assistant_summary_text"]
            )
            assert _sha(exposure["exposed_text"].encode()) == exposure["exposed_text_sha256"]

    assert exposures_by_step[1][0]["assistant_summary_text"] == (
        "I opened Settings: the Profile panel is visible."
    )
    ask_augmented = exposures_by_step[3][0]
    assert ask_augmented["assistant_summary_text"] == (
        "I have successfully asked the user which color is required."
    )
    assert ask_augmented["external_evidence_suffix"] == " 用户回复说：blue"
    assert ask_augmented["exposed_text"] == (
        "I have successfully asked the user which color is required. 用户回复说：blue"
    )

    candidates = result["task_card"]["candidates"]
    ask_candidate = next(
        candidate
        for candidate in candidates
        if candidate["claim"]["source_steps"] == [3] and candidate["exposure"]["target_step"] == 4
    )
    assert ask_candidate["claim"]["representation_type"] == "rolling_summary"
    assert ask_candidate["claim"]["text"] == (
        "I have successfully asked the user which color is required."
    )
    assert "PROGRESS_CLAIM" in ask_candidate["retrieval_reasons"]
    assert "用户回复说" not in ask_candidate["claim"]["text"]
    assert ask_candidate["exposure"]["span_sha256"] == ask_augmented["exposed_text_sha256"]
    target_request_ref = next(
        ref for ref in ask_candidate["evidence_refs"] if ref["role"] == "target_request"
    )
    assert "用户回复说：blue" in target_request_ref["excerpt"]


def test_gelab_first_step_and_missing_or_empty_summary_validate_sentinel() -> None:
    for explicit_empty_summary in (False, True):
        events, task_entry, source_entry = _gelab_fixture_events()
        if explicit_empty_summary:
            decisions = sorted(
                (event for event in events if event["event_type"] == "agent_decision"),
                key=lambda event: event["payload"]["step_index"],
            )
            responses = sorted(
                (event for event in events if event["event_type"] == "model_response"),
                key=lambda event: event["payload"]["step_index"],
            )
            decisions[1]["payload"]["prediction_raw"] += "\tsummary:"
            content = responses[1]["payload"]["normalized_response"]["choices"][0]["content"]
            responses[1]["payload"]["normalized_response"]["choices"][0]["content"] = (
                content.rstrip() + "\tsummary:\n"
            )

        result = reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
        reconstruction = result["reconstruction"]
        assert reconstruction["steps"][0]["I_t"]["assistant_exposures"] == []
        assert reconstruction["steps"][2]["I_t"]["assistant_exposures"] == []
        for step_index in (0, 2):
            request = reconstruction["steps"][step_index]["I_t"]
            event = next(event for event in events if event.get("event_id") == request["event_id"])
            text = event["payload"]["request_view"]["messages"][0]["content"][1]["text"]
            assert "已知已经执行过的历史动作如下：暂无历史操作" in text


def test_gelab_drops_ask_user_response_when_source_summary_is_empty() -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    decisions = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    responses = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    old_summary = "I have successfully asked the user which color is required."
    decisions[2]["payload"]["prediction_raw"] = decisions[2]["payload"]["prediction_raw"].replace(
        f"summary:{old_summary}", "summary:"
    )
    responses[2]["payload"]["normalized_response"]["choices"][0]["content"] = responses[2][
        "payload"
    ]["normalized_response"]["choices"][0]["content"].replace(f"summary:{old_summary}", "summary:")
    requests[3]["payload"]["request_view"]["messages"][0]["content"][1]["text"] = (
        cards_module.GELAB_USER_PROMPT_TEMPLATE.render(
            task="Set the profile color requested by the user.",
            history_display=cards_module._GELAB_EMPTY_HISTORY,
        )
    )

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    exposure = result["reconstruction"]["steps"][3]["I_t"]["assistant_exposures"]
    assert exposure == []
    prompt = requests[3]["payload"]["request_view"]["messages"][0]["content"][1]["text"]
    assert "暂无历史操作" in prompt
    assert "blue" not in prompt


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("request_shape", "gelab_request_content_invalid"),
        ("altered_history", "gelab_user_prompt_template_mismatch"),
    ],
)
def test_gelab_fails_closed_on_malformed_request_or_history_provenance(
    mutation: str, expected_code: str
) -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    if mutation == "request_shape":
        requests[0]["payload"]["request_view"]["messages"][0]["content"].pop()
    else:
        text_part = requests[1]["payload"]["request_view"]["messages"][0]["content"][1]
        text_part["text"] = text_part["text"].replace("Profile", "Account", 1)

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
    assert raised.value.code == expected_code


def test_gelab_parser_clone_matches_runtime_tolerant_semantics() -> None:
    predictions = [
        "explain:No think block\taction:WAIT\tsummary:missing think accepted",
        (
            "<THINK>reason</THINK>\nexplain:ok\tignored field\taction:WAIT\t"
            "summary:colonless field skipped"
        ),
        ("<THINK>reason</THINK>\nexplain:ok\taction:WAIT\tsummary:first\tsummary:last"),
        "<THINK>reason</THINK>\nexplain:ok\taction:WAIT\tsummary:",
        "<THINK>reason</THINK>\nexplain:ok\taction:CLICK\tpoint:100",
        "<THINK>reason</THINK>\nexplain:bad\taction:CLICK\tpoint:not,integers",
    ]
    for prediction in predictions:
        try:
            expected = parse_gelab_response(prediction)
        except Exception:
            expected = None
        assert cards_module._gelab_parse_prediction(prediction) == expected


@pytest.mark.parametrize("mutation", ["missing_think", "colonless_field", "duplicate_last_wins"])
def test_gelab_reconstruction_accepts_runtime_tolerated_parser_output(mutation: str) -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    decisions = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    responses = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    prediction = decisions[0]["payload"]["prediction_raw"]
    expected_summary = "I opened Settings: the Profile panel is visible."
    if mutation == "missing_think":
        prediction = prediction.split("</THINK>", 1)[1].strip()
    elif mutation == "colonless_field":
        prediction = prediction.replace("\tsummary:", "\tignored field\tsummary:", 1)
    else:
        expected_summary = "The duplicate summary wins."
        prediction += f"\tsummary:{expected_summary}"
        requests[1]["payload"]["request_view"]["messages"][0]["content"][1]["text"] = (
            cards_module.GELAB_USER_PROMPT_TEMPLATE.render(
                task="Set the profile color requested by the user.",
                history_display=expected_summary,
            )
        )
    decisions[0]["payload"]["prediction_raw"] = prediction
    responses[0]["payload"]["normalized_response"]["choices"][0]["content"] = prediction + "\n"

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    exposure = result["reconstruction"]["steps"][1]["I_t"]["assistant_exposures"]
    assert len(exposure) == 1
    assert exposure[0]["assistant_summary_text"] == expected_summary


def test_gelab_parser_failure_reuses_latest_successful_summary_with_longer_lag() -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    decisions = sorted(
        (event for event in events if event["event_type"] == "agent_decision"),
        key=lambda event: event["payload"]["step_index"],
    )
    responses = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    requests = sorted(
        (event for event in events if event["event_type"] == "model_request"),
        key=lambda event: event["payload"]["step_index"],
    )
    failed_prediction = decisions[1]["payload"]["prediction_raw"] + "\tpoint:not,integers"
    decisions[1]["payload"]["prediction_raw"] = failed_prediction
    responses[1]["payload"]["normalized_response"]["choices"][0]["content"] = (
        failed_prediction + "\n"
    )
    failed_ack_prediction = decisions[2]["payload"]["prediction_raw"].replace(
        "I should ask the user for the requested color.",
        "That did not work and nothing changed; I should ask the user for the requested color.",
        1,
    )
    decisions[2]["payload"]["prediction_raw"] = failed_ack_prediction
    responses[2]["payload"]["normalized_response"]["choices"][0]["content"] = (
        failed_ack_prediction + "\n"
    )
    reused_summary = "I opened Settings: the Profile panel is visible."
    requests[2]["payload"]["request_view"]["messages"][0]["content"][1]["text"] = (
        cards_module.GELAB_USER_PROMPT_TEMPLATE.render(
            task="Set the profile color requested by the user.",
            history_display=reused_summary,
        )
    )

    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    exposure = result["reconstruction"]["steps"][2]["I_t"]["assistant_exposures"]
    assert len(exposure) == 1
    assert exposure[0]["source_step_index"] == 1
    assert exposure[0]["target_step_index"] == 3
    assert exposure[0]["lag"] == 2
    assert exposure[0]["assistant_summary_text"] == reused_summary
    failed_ack_candidate = next(
        candidate
        for candidate in result["task_card"]["candidates"]
        if "FAILED_TRANSITION_ACK" in candidate["retrieval_reasons"]
    )
    assert failed_ack_candidate["claim"]["source_steps"] == [1]
    assert failed_ack_candidate["exposure"]["target_step"] == 3


def test_gelab_adapter_provenance_must_not_conflict() -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    source_entry["provenance"]["agent_type"] = "qwen3vl"

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
    assert raised.value.code == "adapter_provenance_mismatch"


def test_provider_edge_whitespace_difference_is_preserved_but_not_a_candidate() -> None:
    events, task_entry, source_entry = _gelab_fixture_events()
    result = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )

    comparisons = [
        step["P_t"]["provider_vs_decision"] for step in result["reconstruction"]["steps"]
    ]
    assert all(item["comparison"]["status"] == "edge_whitespace_only" for item in comparisons)
    assert comparisons[0]["provider_content_exact"].endswith("\n")
    assert not comparisons[0]["decision_prediction_exact"].endswith("\n")
    assert all(
        "PROVIDER_DECISION_DIFFERENCE" not in candidate["retrieval_reasons"]
        for candidate in result["task_card"]["candidates"]
    )

    responses = sorted(
        (event for event in events if event["event_type"] == "model_response"),
        key=lambda event: event["payload"]["step_index"],
    )
    responses[0]["payload"]["normalized_response"]["choices"][0]["content"] += "different"
    substantive = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )
    assert (
        substantive["reconstruction"]["steps"][0]["P_t"]["provider_vs_decision"]["comparison"][
            "status"
        ]
        == "different"
    )
    assert any(
        "PROVIDER_DECISION_DIFFERENCE" in candidate["retrieval_reasons"]
        for candidate in substantive["task_card"]["candidates"]
    )


def test_qwen_adapter_provenance_must_not_conflict() -> None:
    events, task_entry, source_entry = _qwen_fixture_events()
    source_entry["provenance"]["agent_type"] = "mai_ui_agent"

    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
    assert raised.value.code == "adapter_provenance_mismatch"

    events, task_entry, source_entry = _qwen_fixture_events()
    task_started = next(event for event in events if event["event_type"] == "task_started")
    task_started["payload"]["agent"]["adapter"] = "ui_venus_agent"
    source_entry["provenance"]["agent_type"] = "ui_venus_agent"
    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=events,
            **_identity(),
        )
    assert raised.value.code == "ui_venus_system_message_invalid"


def test_structural_candidates_require_compound_signal_and_are_capped() -> None:
    reasons = {
        (step, step + 1): {"REPEATED_ACTION", "NEAR_DUPLICATE_REASONING"} for step in range(1, 13)
    }
    reasons[(2, 3)] = {"REPEATED_ACTION"}
    reasons[(5, 6)].add("STATIC_TRANSITION")
    reasons[(6, 7)].add("LONG_LAG_IMAGE_ABSENT")
    reasons[(12, 13)] = {"PROGRESS_CLAIM"}

    selected = cards_module._select_formal_candidate_pairs(reasons, step_count=13)

    assert (2, 3) not in selected
    assert (12, 13) in selected
    structural = [pair for pair in selected if pair != (12, 13)]
    assert len(structural) == cards_module.MAX_STRUCTURAL_CANDIDATES_PER_TASK
    assert (5, 6) in structural
    assert {((pair[1] - 1) * 4) // 13 for pair in structural} == {0, 1, 2, 3}


def test_gelab_candidate_cap_is_deterministic_and_temporally_spread() -> None:
    reasons = {(step, step + 1): {"PROGRESS_CLAIM"} for step in range(1, 16)}
    ordinary = cards_module._select_formal_candidate_pairs(reasons, step_count=16)
    assert len(ordinary) == 15  # MAI/Qwen high-precision behavior remains unchanged.

    selected = cards_module._select_gelab_formal_candidate_pairs(
        ordinary,
        reasons,
        step_count=16,
    )
    reversed_reasons = dict(reversed(list(reasons.items())))
    selected_reversed = cards_module._select_gelab_formal_candidate_pairs(
        cards_module._select_formal_candidate_pairs(reversed_reasons, step_count=16),
        reversed_reasons,
        step_count=16,
    )

    assert selected_reversed == selected
    assert len(selected) == cards_module.MAX_GELAB_FORMAL_CANDIDATES_PER_TASK
    assert {cards_module._gelab_temporal_bucket(pair, step_count=16) for pair in selected} == {
        0,
        1,
        2,
        3,
    }


def test_gelab_candidate_tiers_preserve_critical_priority() -> None:
    reasons = {
        (1, 2): {"FAILED_TRANSITION_ACK"},
        (13, 14): {"SELF_CORRECTION", "PROGRESS_CLAIM"},
        (5, 6): {"PROGRESS_CLAIM", "REPEATED_ACTION"},
        (9, 10): {"REPEATED_ACTION", "STATIC_TRANSITION"},
        (3, 4): {"PROGRESS_CLAIM"},
        (7, 8): {"PROGRESS_CLAIM"},
    }
    ordinary = cards_module._select_formal_candidate_pairs(reasons, step_count=16)
    selected = cards_module._select_gelab_formal_candidate_pairs(
        ordinary,
        reasons,
        step_count=16,
    )

    assert set(selected) == {(1, 2), (13, 14), (5, 6), (9, 10)}
    assert (3, 4) not in selected and (7, 8) not in selected


def test_gui_owl_candidate_cap_is_deterministic_and_preserves_critical_priority() -> None:
    reasons = {(step, step + 1): {"PROGRESS_CLAIM"} for step in range(1, 16)}
    reasons[(2, 3)].add("FAILED_TRANSITION_ACK")
    reasons[(13, 14)].add("SELF_CORRECTION")
    ordinary = cards_module._select_formal_candidate_pairs(reasons, step_count=16)
    assert len(ordinary) == 15

    selected = cards_module._select_gui_owl_formal_candidate_pairs(
        ordinary,
        reasons,
        step_count=16,
    )
    reversed_reasons = dict(reversed(list(reasons.items())))
    selected_reversed = cards_module._select_gui_owl_formal_candidate_pairs(
        cards_module._select_formal_candidate_pairs(reversed_reasons, step_count=16),
        reversed_reasons,
        step_count=16,
    )

    assert selected_reversed == selected
    assert len(selected) == cards_module.MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK
    assert {(2, 3), (13, 14)} <= set(selected)
    assert len(set(selected) - {(2, 3), (13, 14)}) == 2


def test_gui_owl_action_history_sampling_is_temporal_and_fills_total_budget() -> None:
    reasons = {
        (source_step, source_step + 1): {cards_module._GUI_OWL_ACTION_HISTORY_SIGNAL}
        for source_step in range(1, 16)
    }

    selected = cards_module._select_gui_owl_formal_candidate_pairs(
        list(reasons),
        reasons,
        step_count=16,
    )
    reversed_reasons = dict(reversed(list(reasons.items())))
    selected_reversed = cards_module._select_gui_owl_formal_candidate_pairs(
        list(reversed_reasons),
        reversed_reasons,
        step_count=16,
    )

    assert selected_reversed == selected
    assert len(selected) == cards_module.MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK
    assert {cards_module._gelab_temporal_bucket(pair, step_count=16) for pair in selected} == {
        0,
        1,
        2,
        3,
    }


def test_gui_owl_action_mismatches_are_uncapped_and_displace_baseline() -> None:
    reasons = {
        (source_step, source_step + 1): {cards_module._GUI_OWL_ACTION_HISTORY_SIGNAL}
        for source_step in range(1, 11)
    }
    mismatch_pairs = {(step, step + 1) for step in range(1, 7)}
    for pair in mismatch_pairs:
        reasons[pair].add(cards_module._GUI_OWL_ACTION_MISMATCH_SIGNAL)

    selected = cards_module._select_gui_owl_formal_candidate_pairs(
        list(reasons),
        reasons,
        step_count=11,
    )

    assert set(selected) == mismatch_pairs
    assert len(selected) > cards_module.MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK


def test_gui_owl_action_mismatches_fill_before_baseline_to_four() -> None:
    reasons = {
        (source_step, source_step + 1): {cards_module._GUI_OWL_ACTION_HISTORY_SIGNAL}
        for source_step in range(1, 11)
    }
    mismatch_pairs = {(2, 3), (8, 9)}
    for pair in mismatch_pairs:
        reasons[pair].add(cards_module._GUI_OWL_ACTION_MISMATCH_SIGNAL)

    selected = cards_module._select_gui_owl_formal_candidate_pairs(
        list(reasons),
        reasons,
        step_count=11,
    )

    assert mismatch_pairs <= set(selected)
    assert len(selected) == cards_module.MAX_GUI_OWL_FORMAL_CANDIDATES_PER_TASK


def test_reconstruction_is_id_driven_and_rejects_unresolved_history() -> None:
    events, task_entry, source_entry = _fixture_events()
    ordered = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=events,
        **_identity(),
    )
    shuffled = reconstruct_task_events(
        task_entry=task_entry,
        source_entry=source_entry,
        events=list(reversed(events)),
        **_identity(),
    )
    assert shuffled == ordered

    broken = copy.deepcopy(events)
    requests = [event for event in broken if event["event_type"] == "model_request"]
    requests[-1]["payload"]["request_view"]["messages"][2]["content"] = "unmapped"
    with pytest.raises(MotivationCardError) as raised:
        reconstruct_task_events(
            task_entry=task_entry,
            source_entry=source_entry,
            events=broken,
            **_identity(),
        )
    assert raised.value.code == "assistant_exposure_unresolved"


def test_output_cannot_overlap_curated_or_raw_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, source_base, _ = _write_fixture(tmp_path)
    monkeypatch.setattr(cards_module, "validate_curated_composite", lambda **_: {"valid": True})
    forbidden = source_base / "raw" / "run" / "derived"
    with pytest.raises(MotivationCardError) as raised:
        generate_and_write_motivation_artifacts(
            manifest_path=manifest_path,
            source_base=source_base,
            output_dir=forbidden,
        )
    assert raised.value.code == "output_overlaps_input"
    assert not forbidden.exists()


def test_publication_does_not_replace_a_racing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path, source_base, _ = _write_fixture(tmp_path)
    monkeypatch.setattr(cards_module, "validate_curated_composite", lambda **_: {"valid": True})
    artifacts = cards_module.generate_motivation_artifacts(
        manifest_path=manifest_path,
        source_base=source_base,
        verify_blob_digests=False,
    )
    output = tmp_path / "racing-output"
    original = cards_module._rename_directory_noreplace

    def race(source: Path, target: Path) -> None:
        target.mkdir()
        (target / "owner-marker").write_text("preserve", encoding="utf-8")
        original(source, target)

    monkeypatch.setattr(cards_module, "_rename_directory_noreplace", race)
    with pytest.raises(MotivationCardError) as raised:
        cards_module.write_motivation_artifacts(artifacts=artifacts, output_dir=output)

    assert raised.value.code == "output_exists"
    assert (output / "owner-marker").read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".racing-output.staging-*"))
