"""MemGUI Agent with ConAct design for explicit UI memory and proactive context folding.

This agent implements the ConAct approach where the model emits both UI
actions and context actions (history folding or UI memory operations). Key features:

1. Folded Action History: Compressed records of past actions at different
   granularities (step-level vs span-level).

2. Folding Directive: Agent outputs a folding instruction at each step to
   control how its history is compressed.

3. Two Folding Modes:
   - Step-level Distillation: Distill a single step into a compact record
   - Span-level Abstraction: Abstract a multi-step span into one reusable summary

4. Folded UI State (Memory): Agent can explicitly store, update, and delete
   key information extracted from UI.

This approach prevents context saturation while maintaining important information.
"""

import json
import math
import re
import traceback
from typing import Any

from loguru import logger

from mobile_world.agents.base import MCPAgent
from mobile_world.agents.utils.agent_mapping import QWENVL2AW_ACTION_MAP
from mobile_world.agents.utils.helpers import pil_to_base64
from mobile_world.agents.utils.prompts.memgui import MEMGUI_SYSTEM_PROMPT, MEMGUI_USER_TEMPLATE
from mobile_world.runtime.utils.models import UNKNOWN, WAIT, JSONAction

# Coordinate scale used by MemGUI agent (0-1000)
MEMGUI_COORD_SCALE = 1000

# Memory-only action types that do not map to env actions
MEMORY_ACTION_TYPES = {"memory_add", "memory_update", "memory_delete"}

# Exact action vocabulary advertised by the frozen MobileWorld MemGUI prompt.
MEMGUI_ACTION_TYPES = {
    "click",
    "long_press",
    "swipe",
    "type",
    "answer",
    "system_button",
    "wait",
    "terminate",
    *MEMORY_ACTION_TYPES,
}
MEMGUI_SYSTEM_BUTTONS = {"back", "home", "menu", "enter"}


def _extract_tag(text: str, tag: str) -> str:
    """Extract content from an XML-like tag in the model output."""
    match = re.search(
        rf"<{tag}>\s*(.*?)\s*</{tag}>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _extract_tool_call(text: str) -> dict | None:
    """Extract and parse the <tool_call> JSON block from model output."""
    match = re.search(
        r"<tool_call>\s*(.*?)\s*</tool_call>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    try:
        tool_call = json.loads(match.group(1).strip())
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse tool_call JSON: {e}")
        return None

    if not isinstance(tool_call, dict):
        raise ValueError("The <tool_call> payload must be a JSON object")

    action_name = tool_call.get("name")
    if not isinstance(action_name, str) or not action_name.strip():
        raise ValueError("The <tool_call> payload requires a non-empty string 'name'")

    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("The <tool_call> payload requires object-valued 'arguments'")

    return {
        "name": action_name.strip(),
        "arguments": dict(arguments),
    }


def _extract_folding_directive(text: str, current_step: int) -> dict | None:
    """Extract the <folding> directive from model output.

    Returns a dict with 'range' and 'summary' keys, or None if absent.
    """
    match = re.search(r"<folding>\s*(.*?)\s*</folding>", text, re.DOTALL | re.IGNORECASE)
    if not match:
        return None

    folding_text = match.group(1).strip()
    try:
        directive = json.loads(folding_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Failed to parse <folding> JSON: {error}") from error
    if not isinstance(directive, dict):
        raise ValueError("The <folding> payload must be a JSON object")

    normalized = dict(directive)
    _validate_folding_directive(normalized, current_step=current_step)
    return normalized


def _validate_folding_directive(
    directive: dict | None,
    *,
    current_step: int,
) -> None:
    """Validate a ConAct folding update without mutating adapter state."""

    if directive is None:
        if current_step > 1:
            raise ValueError("A <folding> directive is required from step 2 onwards")
        return

    fold_range = directive.get("range")
    summary = directive.get("summary")
    if not isinstance(fold_range, list) or len(fold_range) != 2:
        raise ValueError("The folding 'range' must be a two-element list")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in fold_range):
        raise ValueError("The folding 'range' values must be integers")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("The folding 'summary' must be a non-empty string")

    start_step, end_step = fold_range
    if start_step < 1 or start_step > end_step or end_step > current_step:
        raise ValueError(f"Invalid folding range [{start_step}, {end_step}] at step {current_step}")


def _memory_args_from_action(action_type: str, action_args: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one prompt-advertised memory operation."""

    memory_id = action_args.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id:
        raise ValueError(f"{action_type} requires a non-empty string 'memory_id'")

    description = action_args.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{action_type} requires a string 'description' when provided")

    content = action_args.get("content")
    if action_type in {"memory_add", "memory_update"} and (
        not isinstance(content, str) or not content
    ):
        raise ValueError(f"{action_type} requires a non-empty string 'content'")

    return {
        "operation": action_type.removeprefix("memory_"),
        "memory_id": memory_id,
        "description": description,
        "content": content,
    }


def _validate_memory_preconditions(
    memory_args: dict[str, Any],
    memory_state: dict[str, dict],
) -> None:
    """Reject memory operations that contradict their add/update/delete semantics."""

    operation = memory_args["operation"]
    memory_id = memory_args["memory_id"]
    exists = memory_id in memory_state
    if operation == "add" and exists:
        raise ValueError(f"memory_add cannot overwrite existing memory_id '{memory_id}'")
    if operation in {"update", "delete"} and not exists:
        raise ValueError(f"memory_{operation} requires existing memory_id '{memory_id}'")


def parse_memgui_response(
    text: str,
    image_height: int,
    image_width: int,
    current_step: int,
) -> dict:
    """Parse the full MemGUI model response into structured components.

    Returns a dict with keys:
        thinking, folding_directive, action_json, action_name,
        ui_observation, action_intent, memory_args (or None)
    """
    # Released MemGUI training examples vary in tag order, so require complete
    # blocks without imposing a new ordering policy on the model output.
    required_tags = ["thinking", "tool_call", "ui_observation", "action_intent"]
    tag_matches = {
        tag: re.search(
            rf"<{tag}>\s*(.*?)\s*</{tag}>",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        for tag in required_tags
    }
    missing_tags = [tag for tag, match in tag_matches.items() if match is None]
    if missing_tags:
        raise ValueError(f"Missing complete response tag(s): {', '.join(missing_tags)}")

    thinking = _extract_tag(text, "thinking")
    ui_observation = _extract_tag(text, "ui_observation")
    action_intent = _extract_tag(text, "action_intent")
    folding_directive = _extract_folding_directive(text, current_step)
    _validate_folding_directive(folding_directive, current_step=current_step)

    tool_call = _extract_tool_call(text)
    if tool_call is None:
        raise ValueError("No <tool_call> block found in model output")

    action_name = tool_call["name"]
    if action_name != "mobile_use":
        raise ValueError(f"Unsupported MemGUI tool call: {action_name}")

    action_args = tool_call["arguments"]
    action_type = action_args.get("action")
    if not isinstance(action_type, str) or not action_type.strip():
        raise ValueError("The tool-call arguments require a non-empty string 'action'")
    action_type = action_type.strip()
    if action_type not in MEMGUI_ACTION_TYPES:
        raise ValueError(f"Unsupported MemGUI action: {action_type}")
    action_args["action"] = action_type

    if action_type == "system_button":
        button = action_args.get("button")
        if not isinstance(button, str) or button.lower() not in MEMGUI_SYSTEM_BUTTONS:
            raise ValueError(f"Unsupported MemGUI system_button: {button or '<missing>'}")

    memory_args = None
    if action_type in MEMORY_ACTION_TYPES:
        memory_args = _memory_args_from_action(action_type, action_args)

    # Normalise coordinates from 0-1000 scale to 0.0-1.0 relative coords,
    # then multiply by image dimensions when building the JSONAction.
    if "coordinate" in action_args:
        action_args["coordinate"] = _normalize_coordinate(
            action_args["coordinate"],
            field_name="coordinate",
        )

    if "coordinate2" in action_args:
        action_args["coordinate2"] = _normalize_coordinate(
            action_args["coordinate2"],
            field_name="coordinate2",
        )

    return {
        "thinking": thinking,
        "folding_directive": folding_directive,
        "action_json": action_args,
        "action_name": action_name,
        "ui_observation": ui_observation,
        "action_intent": action_intent,
        "memory_args": memory_args,
    }


def _normalize_coordinate(value: Any, *, field_name: str) -> list[float]:
    """Validate one MemGUI 0-1000 coordinate pair and normalize it."""

    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field_name} must be a two-element list")

    normalized = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{field_name} values must be finite numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{field_name} values must be finite numbers")
        normalized.append(number / MEMGUI_COORD_SCALE)
    return normalized


def _normalized_coordinate_to_pixel(value: Any, extent: int) -> int:
    """Scale one normalized coordinate and clamp it to the screenshot boundary."""

    if extent <= 0:
        raise ValueError(f"Image extent must be positive, got {extent}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Coordinate values must be finite")
    scaled = round(number * extent)
    return min(max(scaled, 0), extent - 1)


def _require_normalized_coordinate(
    action_json: dict[str, Any],
    field_name: str,
    *,
    action_type: str,
) -> list[Any]:
    coordinate = action_json.get(field_name)
    if not isinstance(coordinate, list) or len(coordinate) != 2:
        raise ValueError(f"{action_type} action requires a two-element {field_name}")
    return coordinate


def build_json_action(parsed: dict, image_height: int, image_width: int) -> JSONAction:
    """Convert parsed MemGUI action_json to a MobileWorld JSONAction.

    Coordinate values in action_json are expected to be in the [0, 1] range
    (already normalised from 0-1000 by parse_memgui_response).
    """
    action_name = parsed.get("action_name")
    if action_name != "mobile_use":
        return JSONAction(
            action_type=UNKNOWN,
            text=f"Unsupported MemGUI tool call: {action_name or '<missing>'}",
        )

    action_json = parsed["action_json"]
    action_type = action_json.get("action")

    if action_type == "click":
        coord = _require_normalized_coordinate(
            action_json,
            "coordinate",
            action_type=action_type,
        )
        x = _normalized_coordinate_to_pixel(coord[0], image_width)
        y = _normalized_coordinate_to_pixel(coord[1], image_height)
        return JSONAction(action_type=QWENVL2AW_ACTION_MAP["click"], x=x, y=y)

    elif action_type == "long_press":
        coord = _require_normalized_coordinate(
            action_json,
            "coordinate",
            action_type=action_type,
        )
        x = _normalized_coordinate_to_pixel(coord[0], image_width)
        y = _normalized_coordinate_to_pixel(coord[1], image_height)
        return JSONAction(action_type=QWENVL2AW_ACTION_MAP["long_press"], x=x, y=y)

    elif action_type == "swipe":
        coord = _require_normalized_coordinate(
            action_json,
            "coordinate",
            action_type=action_type,
        )
        coord2 = _require_normalized_coordinate(
            action_json,
            "coordinate2",
            action_type=action_type,
        )
        start_x = _normalized_coordinate_to_pixel(coord[0], image_width)
        start_y = _normalized_coordinate_to_pixel(coord[1], image_height)
        end_x = _normalized_coordinate_to_pixel(coord2[0], image_width)
        end_y = _normalized_coordinate_to_pixel(coord2[1], image_height)
        return JSONAction(
            action_type=QWENVL2AW_ACTION_MAP["swipe"],
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
        )

    elif action_type == "type":
        return JSONAction(
            action_type=QWENVL2AW_ACTION_MAP["type"],
            text=action_json.get("text", ""),
        )

    elif action_type == "answer":
        return JSONAction(
            action_type=QWENVL2AW_ACTION_MAP["answer"],
            text=action_json.get("text", ""),
        )

    elif action_type == "system_button":
        button = action_json.get("button", "").lower()
        button_map = {
            "back": QWENVL2AW_ACTION_MAP["back"],
            "home": QWENVL2AW_ACTION_MAP["home"],
            "enter": QWENVL2AW_ACTION_MAP["enter"],
        }
        mapped = button_map.get(button)
        if mapped is None:
            return JSONAction(
                action_type=UNKNOWN,
                text=f"Unsupported MemGUI system_button: {button or '<missing>'}",
            )
        return JSONAction(action_type=mapped)

    elif action_type == "wait":
        return JSONAction(action_type=QWENVL2AW_ACTION_MAP["wait"])

    elif action_type == "terminate":
        status = action_json.get("status", "success")
        return JSONAction(
            action_type=QWENVL2AW_ACTION_MAP["terminate"],
            text=status,
        )

    elif action_type in MEMORY_ACTION_TYPES:
        # Memory operations do not correspond to env actions; return a wait
        return JSONAction(action_type=WAIT)

    return JSONAction(
        action_type=UNKNOWN,
        text=f"Unsupported MemGUI action: {action_type or '<missing>'}",
    )


class MemGUIAgent(MCPAgent):
    """MemGUI Agent with ConAct design for MobileWorld.

    Adapts MemGUIAgent26010302 from the reference MemGUI-Rollout codebase to
    the MobileWorld agent interface (MCPAgent.predict).

    State maintained across steps:
        current_step: int
        state_summaries: list of (start_step, end_step, summary_text) — Folded Action History
        latest_interaction: dict with details of the most recent step — Recent Step Record
        memory_state: dict mapping memory_id -> {description, content} — Folded UI State
    """

    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        runtime_conf: dict | None = None,
        *args: Any,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.model_name = model_name
        self.llm_base_url = llm_base_url
        self.runtime_conf = {"temperature": 0.0, **(runtime_conf or {})}
        self.build_openai_client(self.llm_base_url, api_key)

        # Folding state
        self.current_step: int = 0
        self.state_summaries: list[tuple[int, int, str]] = []
        self.latest_interaction: dict | None = None
        self.memory_state: dict[str, dict] = {}

        # History for logging / compatibility
        self.history_responses: list[str] = []
        self.thoughts: list[str] = []
        self.ui_observations: list[str] = []
        self.action_intents: list[str] = []

        # Folding statistics
        self.folding_stats = {
            "step_level_distillations": 0,
            "span_level_abstractions": 0,
            "total_steps_folded": 0,
        }

    # ------------------------------------------------------------------
    # Context helpers
    # ------------------------------------------------------------------

    def _format_state_summaries(self) -> str:
        if not self.state_summaries:
            return "  (no previous steps)"
        return "\n".join(f"  {summary}" for _, _, summary in self.state_summaries)

    def _format_latest_interaction(self) -> str:
        if not self.latest_interaction:
            return "  (no previous interaction)"
        li = self.latest_interaction
        parts = [f"  Step {li['step']}:"]
        if li.get("ui_observation"):
            parts.append(f"    UI Observation: {li['ui_observation']}")
        if li.get("action_intent"):
            parts.append(f"    Action Intent: {li['action_intent']}")
        if li.get("action_summary"):
            parts.append(f"    Action Taken: {li['action_summary']}")
        return "\n".join(parts)

    def _format_memory_state(self) -> str:
        if not self.memory_state:
            return "  (empty)"
        entries = []
        for mem_id, mem_data in self.memory_state.items():
            if isinstance(mem_data, dict):
                entries.append(
                    f"  [{mem_id}]\n"
                    f"    Description: {mem_data.get('description', '')}\n"
                    f"    Content: {mem_data.get('content', '')}"
                )
            else:
                entries.append(f"  [{mem_id}]: {mem_data}")
        return "\n".join(entries)

    # ------------------------------------------------------------------
    # Folding
    # ------------------------------------------------------------------

    def _prepare_folding_update(
        self,
        directive: dict,
    ) -> tuple[list[tuple[int, int, str]], dict[str, int], str]:
        """Compute a folding update without mutating the accepted history state."""

        _validate_folding_directive(
            directive,
            current_step=self.current_step,
        )
        start_step, end_step = directive["range"]
        summary = directive["summary"].strip()
        folding_stats = dict(self.folding_stats)

        if start_step == end_step:
            folding_stats["step_level_distillations"] += 1
            fold_type = "Step-level Distillation"
        else:
            folding_stats["span_level_abstractions"] += 1
            fold_type = "Span-level Abstraction"

        folding_stats["total_steps_folded"] += end_step - start_step + 1

        # Preserve the official adapter's destructive replacement of every
        # overlapping summary, including partially overlapping spans.
        state_summaries = [
            (s, e, t) for (s, e, t) in self.state_summaries if e < start_step or s > end_step
        ]
        state_summaries.append((start_step, end_step, summary))
        state_summaries.sort(key=lambda x: x[0])
        log_message = f'[FOLD] {fold_type}: Steps [{start_step}-{end_step}] -> "{summary}"'
        return state_summaries, folding_stats, log_message

    def _apply_folding_directive(self, directive: dict) -> None:
        try:
            state_summaries, folding_stats, log_message = self._prepare_folding_update(directive)
        except ValueError as error:
            logger.warning(f"Invalid folding directive: {error}")
            return

        self.state_summaries = state_summaries
        self.folding_stats = folding_stats
        logger.info(log_message)

    # ------------------------------------------------------------------
    # Memory operations
    # ------------------------------------------------------------------

    def _execute_memory_operation(
        self,
        memory_args: dict,
        *,
        memory_state: dict[str, dict] | None = None,
    ) -> str:
        target_state = self.memory_state if memory_state is None else memory_state
        operation = memory_args.get("operation", "none")
        memory_id = memory_args.get("memory_id")
        description = memory_args.get("description", "")
        content = memory_args.get("content")
        if operation in {"add", "update", "delete"}:
            _validate_memory_preconditions(memory_args, target_state)

        if operation == "add":
            if isinstance(memory_id, str) and memory_id and isinstance(content, str) and content:
                target_state[memory_id] = {"description": description, "content": content}
                preview = content[:50] + "..." if len(content) > 50 else content
                return f"Added memory [{memory_id}]: {description} | {preview}"
            return "Failed to add memory: missing memory_id or content"

        elif operation == "update":
            if isinstance(memory_id, str) and memory_id and isinstance(content, str) and content:
                old = target_state.get(memory_id, {})
                target_state[memory_id] = {
                    "description": description or old.get("description", ""),
                    "content": content,
                }
                return f"Updated memory [{memory_id}]"
            return "Failed to update memory: missing memory_id or content"

        elif operation == "delete":
            if isinstance(memory_id, str) and memory_id and memory_id in target_state:
                target_state.pop(memory_id)
                return f"Deleted memory [{memory_id}]"
            return f"Failed to delete memory [{memory_id}]: not found"

        return "No memory operation performed"

    # ------------------------------------------------------------------
    # predict
    # ------------------------------------------------------------------

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Predict the next action based on current screenshot and folded context."""
        self.current_step += 1

        screenshot = observation["screenshot"]
        encoded_string = pil_to_base64(screenshot)
        image_width, image_height = screenshot.width, screenshot.height

        # Build user prompt
        folding_instruction = (
            "Skip <folding> for the first step"
            if self.current_step == 1
            else "Output <folding> to compress your previous step(s)"
        )
        user_content = MEMGUI_USER_TEMPLATE.format(
            instruction=self.instruction,
            state_summaries=self._format_state_summaries(),
            latest_interaction=self._format_latest_interaction(),
            memory_state=self._format_memory_state(),
            folding_instruction=folding_instruction,
        )

        messages = [
            {"role": "system", "content": [{"type": "text", "text": MEMGUI_SYSTEM_PROMPT}]},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded_string}"},
                    },
                ],
            },
        ]

        audit_retry_group = self._begin_outer_model_audit_retry_group()

        try_times = 3
        adapter_attempt_index = 0
        parsed_response = None
        prediction = None
        json_action = None
        next_state_summaries = None
        next_folding_stats = None
        next_memory_state = None
        action_summary = None
        folding_log_message = None
        memory_log_message = None
        last_validation_error: Exception | None = None

        while try_times > 0:
            adapter_attempt_index += 1
            if audit_retry_group is None:
                prediction = self.openai_chat_completions_create(
                    model=self.model_name,
                    messages=messages,
                    retry_times=3,
                    **self.runtime_conf,
                )
            else:
                with self._outer_model_audit_attempt_scope(
                    audit_retry_group,
                    adapter_attempt_index=adapter_attempt_index,
                    # Provider failure exits this adapter; only a successful but
                    # malformed response reaches the outer parse retry.
                    adapter_retry_planned=False,
                ):
                    prediction = self.openai_chat_completions_create(
                        model=self.model_name,
                        messages=messages,
                        retry_times=3,
                        **self.runtime_conf,
                    )

            if prediction is None:
                raise RuntimeError("Error when fetching response from model")

            try:
                candidate_response = parse_memgui_response(
                    prediction,
                    image_height=image_height,
                    image_width=image_width,
                    current_step=self.current_step,
                )
                _validate_folding_directive(
                    candidate_response["folding_directive"],
                    current_step=self.current_step,
                )
                candidate_action = build_json_action(
                    candidate_response,
                    image_height,
                    image_width,
                )
                if candidate_action.action_type == UNKNOWN:
                    raise ValueError(candidate_action.text or "Unsupported MemGUI action")

                candidate_state_summaries = list(self.state_summaries)
                candidate_folding_stats = dict(self.folding_stats)
                candidate_folding_log = None
                folding_directive = candidate_response["folding_directive"]
                if self.current_step > 1:
                    assert folding_directive is not None
                    (
                        candidate_state_summaries,
                        candidate_folding_stats,
                        candidate_folding_log,
                    ) = self._prepare_folding_update(folding_directive)

                candidate_memory_state = dict(self.memory_state)
                candidate_memory_log = None
                memory_args = candidate_response.get("memory_args")
                if memory_args:
                    result = self._execute_memory_operation(
                        memory_args,
                        memory_state=candidate_memory_state,
                    )
                    if result.startswith("Failed"):
                        raise ValueError(result)
                    candidate_memory_log = f"[MemGUI Memory] {result}"
                    candidate_action_summary = f"Memory: {result}"
                else:
                    candidate_action_summary = candidate_action.action_type or "unknown"

                parsed_response = candidate_response
                json_action = candidate_action
                next_state_summaries = candidate_state_summaries
                next_folding_stats = candidate_folding_stats
                next_memory_state = candidate_memory_state
                action_summary = candidate_action_summary
                folding_log_message = candidate_folding_log
                memory_log_message = candidate_memory_log
                logger.info(f"[MemGUI Step {self.current_step}] Parsed response OK")
                break
            except Exception as error:
                last_validation_error = error
                try_times -= 1
                logger.error(
                    f"Error parsing or validating model response (remaining retries: {try_times})"
                )
                logger.error(traceback.format_exc())
                prediction = None

        if (
            parsed_response is None
            or prediction is None
            or json_action is None
            or next_state_summaries is None
            or next_folding_stats is None
            or next_memory_state is None
            or action_summary is None
        ):
            detail = (
                f"{type(last_validation_error).__name__}: {last_validation_error}"
                if last_validation_error is not None
                else "unknown model-output validation failure"
            )
            logger.error(f"Failed to validate model response after maximum retries: {detail}")
            return (
                f"memgui output invalid after multiple retries: {detail}",
                JSONAction(action_type=UNKNOWN, text=detail),
            )

        # Commit the accepted folding and memory state together only after the
        # entire candidate response has parsed, converted, and passed stateful
        # memory preconditions.
        self.state_summaries = next_state_summaries
        self.folding_stats = next_folding_stats
        self.memory_state = next_memory_state
        if folding_log_message is not None:
            logger.info(folding_log_message)
        if memory_log_message is not None:
            logger.info(memory_log_message)

        # ------------------------------------------------------------------
        # Update agent state for next step
        # ------------------------------------------------------------------
        self.latest_interaction = {
            "step": self.current_step,
            "ui_observation": parsed_response["ui_observation"],
            "action_intent": parsed_response["action_intent"],
            "action_summary": action_summary,
        }

        self.history_responses.append(prediction)
        self.thoughts.append(parsed_response["thinking"])
        self.ui_observations.append(parsed_response["ui_observation"])
        self.action_intents.append(parsed_response["action_intent"])

        logger.info(
            f"[MemGUI Step {self.current_step}] action={json_action.action_type} "
            f"summaries={len(self.state_summaries)} memory_keys={list(self.memory_state.keys())}"
        )

        return prediction, json_action

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset agent state for the next task."""
        self.current_step = 0
        self.state_summaries = []
        self.latest_interaction = None
        self.memory_state = {}
        self.history_responses = []
        self.thoughts = []
        self.ui_observations = []
        self.action_intents = []
        self.folding_stats = {
            "step_level_distillations": 0,
            "span_level_abstractions": 0,
            "total_steps_folded": 0,
        }
