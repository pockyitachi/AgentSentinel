import json
import traceback
from typing import Any

from loguru import logger

from mobile_world.agents.base import MCPAgent
from mobile_world.agents.utils.agent_mapping import GUIOWL2AW_ACTION_MAP
from mobile_world.agents.utils.helpers import add_period_robustly, pil_to_base64
from mobile_world.agents.utils.prompts import (
    GUI_OWL_1_5_SYSTEM_PROMPT_TEMPLATE,
    GUI_OWL_1_5_USER_PROMPT_TEMPLATE,
    GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE,
)
from mobile_world.runtime.utils.helpers import pretty_print_messages
from mobile_world.runtime.utils.models import MCP, UNKNOWN, JSONAction

SCALE_FACTOR = 999

_DEFAULT_RUNTIME_CONF = {
    "history_n": 1,
    "max_tokens": 2048,
    "temperature": 0.0,
    "top_p": 1.0,
}


def parse_tagged_text(text: str) -> dict:
    """
    Parse model output text into structured components.

    Expected format:
        <thinking content>
        Action: "<conclusion>"
        <tool_call>
        {"name": ..., "arguments": ...}
        </tool_call>

    Returns a dict with keys: thinking, conclusion, tool_call.
    """
    result = {"thinking": None, "conclusion": None, "tool_call": None}

    action_parts = text.split("Action:", 1)
    if len(action_parts) > 1:
        result["thinking"] = action_parts[0].strip()
        action_content = action_parts[1]
    else:
        # No "Action:" tag found; treat entire text as action content
        action_content = text

    # Parse conclusion and tool_call from action content
    tool_parts = action_content.split("<tool_call>", 1)
    if len(tool_parts) > 1:
        conclusion_content = tool_parts[0].strip()
        # Strip surrounding quotes if present
        if conclusion_content.startswith('"') and conclusion_content.endswith('"'):
            conclusion_content = conclusion_content[1:-1]
        result["conclusion"] = conclusion_content

        if "</tool_call>" not in tool_parts[1]:
            raise ValueError("Missing </tool_call> closing tag in model output.")
        tool_call_raw = tool_parts[1].split("</tool_call>", 1)[0].strip()
        try:
            result["tool_call"] = json.loads(tool_call_raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse tool_call JSON: {e}")

    return result


def parse_action_to_structure_output(text: str) -> dict:
    """
    Parse raw model output into a structured response dict.

    Returns:
        {
            "thinking":     str | None,
            "conclusion":   str | None,
            "action_json":  dict,
            "action_name":  str,
        }
    """
    text = text.strip()

    results = parse_tagged_text(text)
    thinking = results["thinking"]
    conclusion = results["conclusion"]
    tool_call = results["tool_call"]

    if tool_call is None:
        raise ValueError("No <tool_call> block found in model output.")

    if not isinstance(tool_call, dict):
        raise ValueError("The <tool_call> payload must be a JSON object.")

    action_name = tool_call.get("name")
    if not isinstance(action_name, str) or not action_name.strip():
        raise ValueError("The <tool_call> payload requires a non-empty string 'name'.")

    arguments = tool_call.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("The <tool_call> payload requires an object-valued 'arguments'.")

    # Work on a private copy. Parsing must not mutate the decoded provider response.
    action = dict(arguments)
    action_name = action_name.strip()

    # Normalize 'coordinate' to a 2-element [x, y] list in [0, 1] range
    if "coordinate" in action:
        coordinates = action["coordinate"]
        if not isinstance(coordinates, list):
            raise ValueError(f"Unexpected coordinate type: {type(coordinates).__name__}")
        if len(coordinates) == 2:
            point_x, point_y = coordinates
        elif len(coordinates) == 4:
            x1, y1, x2, y2 = coordinates
            point_x = (x1 + x2) / 2
            point_y = (y1 + y2) / 2
        else:
            raise ValueError(f"Unexpected coordinate length: {coordinates}")
        action["coordinate"] = [point_x / SCALE_FACTOR, point_y / SCALE_FACTOR]

    # Normalize 'coordinate2' to a 2-element [x, y] list in [0, 1] range
    if "coordinate2" in action:
        coordinates = action["coordinate2"]
        if not isinstance(coordinates, list):
            raise ValueError(f"Unexpected coordinate2 type: {type(coordinates).__name__}")
        if len(coordinates) == 2:
            point_x, point_y = coordinates
        elif len(coordinates) == 4:
            x1, y1, x2, y2 = coordinates
            point_x = (x1 + x2) / 2
            point_y = (y1 + y2) / 2
        else:
            raise ValueError(f"Unexpected coordinate2 length: {coordinates}")
        action["coordinate2"] = [point_x / SCALE_FACTOR, point_y / SCALE_FACTOR]

    return {
        "thinking": thinking,
        "action_json": action,
        "conclusion": conclusion,
        "action_name": action_name,
    }


def _normalized_coordinate_to_pixel(value: Any, extent: int) -> int:
    """Scale one normalized coordinate and clamp it to the screenshot boundary."""

    if extent <= 0:
        raise ValueError(f"Image extent must be positive, got {extent}.")
    scaled = round(float(value) * extent)
    return min(max(scaled, 0), extent - 1)


def parsing_response_to_andoid_world_env_action(
    structured_response: dict, image_height: int, image_width: int
) -> dict:
    """Convert a structured model response into an AndroidWorld environment action dict."""
    action_json = structured_response.get("action_json")
    action_type = action_json.get("action")

    result = {}

    if action_type == "type":
        result = {
            "action_type": GUIOWL2AW_ACTION_MAP["type"],
            "text": action_json.get("text", ""),
        }

    elif action_type == "swipe":
        start_box = action_json.get("coordinate")
        end_box = action_json.get("coordinate2")
        if start_box and end_box:
            x1, y1 = start_box
            x2, y2 = end_box
            result = {
                "action_type": GUIOWL2AW_ACTION_MAP["swipe"],
                "start_x": _normalized_coordinate_to_pixel(x1, image_width),
                "start_y": _normalized_coordinate_to_pixel(y1, image_height),
                "end_x": _normalized_coordinate_to_pixel(x2, image_width),
                "end_y": _normalized_coordinate_to_pixel(y2, image_height),
            }
        else:
            raise ValueError("Invalid swipe: missing coordinate or coordinate2.")

    elif action_type in ("click", "long_press"):
        start_box = action_json.get("coordinate")
        if start_box:
            try:
                if len(start_box) == 4:
                    x1, y1, x2, y2 = start_box
                elif len(start_box) == 2:
                    x1, y1 = start_box
                    x2, y2 = x1, y1
                else:
                    raise ValueError(f"Invalid coordinate format: {start_box}")
                x = _normalized_coordinate_to_pixel((x1 + x2) / 2, image_width)
                y = _normalized_coordinate_to_pixel((y1 + y2) / 2, image_height)
                result = {
                    "action_type": GUIOWL2AW_ACTION_MAP[action_type],
                    "x": x,
                    "y": y,
                }
            except Exception as e:
                logger.error(f"Error parsing coordinates: {e}")
                raise
        else:
            raise ValueError(f"Missing coordinate for action_type '{action_type}'.")

    elif action_type == "system_button":
        button = action_json.get("button", "").title()
        if button == "Home":
            result = {"action_type": GUIOWL2AW_ACTION_MAP["home"]}
        elif button == "Back":
            result = {"action_type": GUIOWL2AW_ACTION_MAP["back"]}
        elif button == "Enter":
            result = {"action_type": GUIOWL2AW_ACTION_MAP["enter"]}
        else:
            result = {
                "action_type": UNKNOWN,
                "text": f"Unsupported GUI-Owl system_button: {button or '<missing>'}",
            }

    elif action_type == "interact":
        result = {
            "action_type": GUIOWL2AW_ACTION_MAP["interact"],
            "text": action_json.get("text", ""),
        }

    elif action_type == "open":
        # Kept for backward compatibility; current prompt does not emit this action.
        result = {
            "action_type": "open_app",
            "app_name": action_json.get("text", ""),
        }

    elif action_type == "terminate":
        result = {
            "action_type": GUIOWL2AW_ACTION_MAP["terminate"],
            "text": action_json.get("status", ""),
        }

    elif action_type == "answer":
        result = {
            "action_type": GUIOWL2AW_ACTION_MAP["answer"],
            "text": action_json.get("text", ""),
        }

    elif action_type == "wait":
        result = {"action_type": GUIOWL2AW_ACTION_MAP["wait"]}

    else:
        result = {
            "action_type": UNKNOWN,
            "text": f"Unsupported GUI-Owl mobile_use action: {action_type!r}",
        }

    return result


def _make_image_content(encoded_string: str) -> dict:
    """Return an OpenAI-compatible image_url content block from a base64 string."""
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{encoded_string}"},
    }


class GUIOWL15AgentMCP(MCPAgent):
    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        observation_type: str = "screenshot",
        runtime_conf: dict | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.model_name = model_name
        logger.info(f"Running Task with policy model name: {model_name}")
        self.llm_base_url = llm_base_url
        self.observation_type = observation_type
        self.runtime_conf = {**_DEFAULT_RUNTIME_CONF, **(runtime_conf or {})}
        self.build_openai_client(self.llm_base_url, api_key)

        # Per-task state (reset between tasks)
        self.thoughts: list[str] = []
        self.actions: list[dict] = []
        self.conclusions: list[str] = []
        self.history_images: list[str] = []  # base64-encoded strings
        self.history_responses: list[str] = []  # raw assistant text (excludes current)
        self.history_user_content: list[
            tuple
        ] = []  # (encoded_string, tool_call, ask_user_response)

        # Frequently used hyper-parameters
        self.temperature = self.runtime_conf.pop("temperature", 0.0)
        self.top_p = self.runtime_conf.pop("top_p", 1.0)
        self.max_tokens = self.runtime_conf.pop("max_tokens", 2048)
        self.history_n = self.runtime_conf.pop("history_n", 1)
        self.is_memory_mode = self.runtime_conf.pop("is_memory_mode", False)
        if isinstance(self.history_n, bool) or not isinstance(self.history_n, int):
            raise ValueError("GUI-Owl history_n must be an integer greater than or equal to 1.")
        if self.history_n < 1:
            raise ValueError("GUI-Owl history_n must be greater than or equal to 1.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_user_message(
        self,
        encoded_string: str,
        tool_call_res: str | None,
        ask_user_response_res: str | None,
    ) -> dict:
        """
        Build an OpenAI user message that wraps a tool response and a screenshot.
        """
        user_content = [
            {"type": "text", "text": "<tool_response>\n"},
        ]

        if tool_call_res is not None:
            user_content.append({"type": "text", "text": str(tool_call_res)})
        elif ask_user_response_res is not None:
            user_content.append(
                {
                    "type": "text",
                    "text": f"(Ask_user_response){ask_user_response_res}",
                }
            )
        else:
            user_content.append({"type": "text", "text": "None"})

        user_content.append(_make_image_content(encoded_string))
        user_content.append({"type": "text", "text": "\n</tool_response>"})

        return {"role": "user", "content": user_content}

    def _available_mcp_tool_names(self) -> set[str]:
        """Return the tool names the environment actually exposed to this agent."""

        names: set[str] = set()
        for tool in self.tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str):
                function = tool.get("function")
                name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str) and name:
                names.add(name)
        return names

    def _format_previous_steps(
        self,
        start_idx: int,
        end_idx: int,
        history_user_content: list[tuple] | None = None,
    ) -> str:
        """
        Render history steps [start_idx, end_idx) as plain text.

        Each line follows the pattern:
            Step<N>: <conclusion>  Tool response: <tool_response>
        """
        user_content = (
            self.history_user_content if history_user_content is None else history_user_content
        )
        previous_steps = []
        for i in range(start_idx, end_idx):
            step_num = i + 1
            conclusion = add_period_robustly(self.conclusions[i])
            step_info = f"Step{step_num}: {conclusion}"

            # Action i is produced from observation i. Its tool/ask result is
            # delivered with the following observation, at index i + 1.
            result_idx = i + 1
            tool_call_res = user_content[result_idx][1] if result_idx < len(user_content) else None
            ask_user_res = user_content[result_idx][2] if result_idx < len(user_content) else None

            if tool_call_res is not None:
                step_info += f" Tool response: {tool_call_res}"
            elif ask_user_res is not None:
                step_info += f" Tool response: (Ask_user_response){ask_user_res}"
            else:
                step_info += " Tool response: None"

            previous_steps.append(step_info)

        return "\n".join(previous_steps)

    def _build_messages(
        self,
        current_user_content: tuple[str, Any, Any],
    ) -> list[dict[str, Any]]:
        """Build the next provider request without mutating per-task history."""

        request_user_content = [*self.history_user_content, current_user_content]
        total_history_count = len(self.history_responses)
        if len(request_user_content) != total_history_count + 1:
            raise ValueError(
                "GUI-Owl observation history must be exactly one ahead of completed responses."
            )

        system_prompt = GUI_OWL_1_5_SYSTEM_PROMPT_TEMPLATE.render(
            tools="\n".join([json.dumps(tool, ensure_ascii=False) for tool in self.tools])
        )

        # history_n includes the current observation. Recent completed turns
        # remain as assistant/user message pairs; older turns are text-only.
        keep_as_messages = min(self.history_n - 1, total_history_count)
        text_history_count = total_history_count - keep_as_messages

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_prompt,
            }
        ]

        first_user_content: list[dict[str, Any]] = []
        if text_history_count > 0:
            previous_steps_text = self._format_previous_steps(
                0,
                text_history_count,
                request_user_content,
            )
            first_user_content.append(
                {
                    "type": "text",
                    "text": GUI_OWL_1_5_USER_PROMPT_WITH_HISTSTEPS_TEMPLATE.format(
                        instruction=self.instruction,
                        previous_steps=previous_steps_text,
                    ),
                }
            )
        else:
            first_user_content.append(
                {
                    "type": "text",
                    "text": GUI_OWL_1_5_USER_PROMPT_TEMPLATE.format(instruction=self.instruction),
                }
            )

        # The first kept observation's result is already attached to the
        # preceding collapsed step (when one exists), so this message carries
        # only its screenshot.
        first_img_encoded, _, _ = request_user_content[text_history_count]
        first_user_content.append(_make_image_content(first_img_encoded))
        messages.append({"role": "user", "content": first_user_content})

        for i in range(text_history_count, total_history_count):
            messages.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": self.history_responses[i].strip()}],
                }
            )

            next_encoded, tool_call_res, ask_user_response_res = request_user_content[i + 1]
            messages.append(
                self._get_user_message(
                    next_encoded,
                    tool_call_res,
                    ask_user_response_res,
                )
            )

        logger.debug(
            f"Constructed messages: {keep_as_messages} user-assistant pair(s) "
            f"with images, {text_history_count} text-only history step(s)."
        )
        return messages

    # ------------------------------------------------------------------
    # Main prediction method
    # ------------------------------------------------------------------

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        """Predict the next action based on the current observation."""

        assert (
            len(self.actions)
            == len(self.thoughts)
            == len(self.conclusions)
            == len(self.history_images)
            == len(self.history_responses)
            == len(self.history_user_content)
        ), "Mismatch between GUI-Owl per-turn history counts."

        # ── Encode current screenshot ──────────────────────────────────
        obs_image = observation["screenshot"]
        encoded_string: str = pil_to_base64(obs_image)

        tool_call = observation.get("tool_call", None)
        ask_user_response = observation.get("ask_user_response", None)

        current_user_content = (encoded_string, tool_call, ask_user_response)

        logger.debug(f"Prospective history images count: {len(self.history_images) + 1}")
        logger.debug(f"History responses count: {len(self.history_responses)}")

        messages = self._build_messages(current_user_content)
        pretty_print_messages(messages, max_messages=4)
        logger.debug("*" * 100)

        # ── LLM inference with retry ───────────────────────────────────
        origin_h, origin_w = obs_image.height, obs_image.width
        parsed_response = None
        json_action = None
        action_record = None
        prediction = None
        last_validation_error: Exception | None = None
        max_retries = 5

        audit_retry_group = self._begin_outer_model_audit_retry_group()

        for attempt in range(1, max_retries + 1):
            if audit_retry_group is None:
                prediction = self.openai_chat_completions_create(
                    model=self.model_name,
                    messages=messages,
                    retry_times=3,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    **self.runtime_conf,
                )
            else:
                with self._outer_model_audit_attempt_scope(
                    audit_retry_group,
                    adapter_attempt_index=attempt,
                    # Provider failure exits this adapter; only a successful but
                    # malformed response reaches the outer parse retry.
                    adapter_retry_planned=False,
                ):
                    prediction = self.openai_chat_completions_create(
                        model=self.model_name,
                        messages=messages,
                        retry_times=3,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_tokens=self.max_tokens,
                        **self.runtime_conf,
                    )
            logger.info(f"Raw prediction (attempt {attempt}):\n{prediction}")

            if prediction is None:
                raise RuntimeError("Received None response from the LLM client.")

            try:
                candidate_response = parse_action_to_structure_output(prediction)
                logger.info(f"Parsed response:\n{candidate_response}")

                if candidate_response["action_name"] == "mobile_use":
                    candidate_action_record = parsing_response_to_andoid_world_env_action(
                        candidate_response,
                        origin_h,
                        origin_w,
                    )
                    candidate_json_action = JSONAction(**candidate_action_record)
                else:
                    tool_name = candidate_response["action_name"]
                    if tool_name not in self._available_mcp_tool_names():
                        detail = f"Unregistered GUI-Owl tool call: {tool_name}"
                        candidate_action_record = {
                            "action_type": UNKNOWN,
                            "text": detail,
                        }
                        candidate_json_action = JSONAction(
                            action_type=UNKNOWN,
                            text=detail,
                        )
                    else:
                        candidate_action_record = {
                            "action_name": tool_name,
                            "action_args": candidate_response["action_json"],
                        }
                        candidate_json_action = JSONAction(
                            action_type=MCP,
                            action_json=candidate_response["action_json"],
                            action_name=tool_name,
                        )

                parsed_response = candidate_response
                action_record = candidate_action_record
                json_action = candidate_json_action
                break
            except Exception as error:
                last_validation_error = error
                logger.error(f"Failed to parse or validate response on attempt {attempt}.")
                logger.error(traceback.format_exc())
                prediction = None
                if attempt == max_retries:
                    logger.error("Max retries reached; giving up on this step.")

        # ── Parse/action validation failure fallback ───────────────────
        if parsed_response is None or json_action is None or action_record is None:
            detail = (
                f"{type(last_validation_error).__name__}: {last_validation_error}"
                if last_validation_error is not None
                else "unknown model-output validation failure"
            )
            return (
                f"llm output invalid after multiple retries: {detail}",
                JSONAction(action_type=UNKNOWN, text=detail),
            )

        # Commit the observation and accepted model turn atomically only after
        # parsing, conversion, and JSONAction validation have all succeeded.
        self.history_images.append(encoded_string)
        self.history_user_content.append(current_user_content)
        self.history_responses.append(prediction)
        self.thoughts.append(parsed_response["thinking"])
        self.conclusions.append(parsed_response["conclusion"])
        self.actions.append(action_record)

        return prediction, json_action

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self):
        """Reset all per-task state so the agent is ready for a new task."""
        self.thoughts = []
        self.actions = []
        self.conclusions = []
        self.history_images = []
        self.history_responses = []
        self.history_user_content = []
