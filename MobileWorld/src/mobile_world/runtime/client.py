import base64
import copy
import math
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO

import backoff
import requests
from loguru import logger
from markdownify import markdownify
from PIL import Image

from mobile_world.runtime.audit.execution_io import (
    record_gui_request,
    record_gui_response,
    record_mcp_raw_result,
    record_mcp_request,
    record_mcp_visible_result,
    record_screenshot_source,
)
from mobile_world.runtime.mcp_server import init_mcp_clients
from mobile_world.runtime.utils.models import MCP, NAVIGATE_HOME, JSONAction, Observation, Response
from mobile_world.runtime.utils.trajectory_logger import SCORE_FILE_NAME
from mobile_world.tasks.registry import TaskRegistry

TASK_META_DATA_PATH = "./new_task_metadata.json"
DEFAULT_MAX_STEP = 15
# A same-request recovery can include a 15s snapshot-console call plus 45s
# stability barrier, the bounded 150s emulator restart, a 5s controller query
# plus 45s replacement barrier, and one full task reinitialization.  Keep the
# HTTP budget above that bounded infrastructure path so the client cannot retry
# while the original request still owns the lifecycle.
TASK_INITIALIZATION_TIMEOUT_SECONDS = 600


class CleanupTaskTeardownStatusV1(StrEnum):
    """Closed outcomes for cleanup teardown that is forbidden to initialize."""

    SUCCEEDED = "SUCCEEDED"
    NOT_INITIALIZED_NO_IO = "NOT_INITIALIZED_NO_IO"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class CleanupTaskTeardownResultV1:
    """Typed result from the cleanup-only, no-initialization teardown path."""

    status: CleanupTaskTeardownStatusV1
    message: str
    request_dispatched: bool


def _safe_audit_hook(hook, *args, **kwargs) -> None:
    """Keep passive audit observers outside the environment control plane."""

    try:
        hook(*args, **kwargs)
    except Exception:
        # Collector state is best-effort and is finalized as incomplete by its
        # own boundary.  Never replace a live environment result/exception.
        pass


class AndroidEnvClient:
    """Client for interacting with the new Android environment server (server.py)."""

    def __init__(
        self,
        url: str = "http://localhost:8000",
        device: str = "emulator-5554",
        step_wait_time: float = 1.0,
        *,
        trust_env: bool = True,
        request_deadline_monotonic_ns: int | None = None,
    ):
        logger.info(
            "Setting up Android environment using new server design - Initial setup may take"
            " 5-10 minutes. Please wait..."
        )
        self.base_url = url
        self.device = device
        self.step_wait_time = step_wait_time
        self._task_metadata = {}
        self._current_task_type = None
        self._initialized = False
        self._task_registry = TaskRegistry()
        self.tools = []
        if type(trust_env) is not bool:
            raise TypeError("trust_env must be an exact bool")
        if request_deadline_monotonic_ns is not None and (
            type(request_deadline_monotonic_ns) is not int
            or request_deadline_monotonic_ns <= time.monotonic_ns()
        ):
            raise ValueError("request deadline must be a future monotonic timestamp")
        self._session = requests.Session()
        self._session.trust_env = trust_env
        self._request_deadline_monotonic_ns = request_deadline_monotonic_ns

    def _request_timeout(self, ceiling_seconds: float | int | None = None) -> float | int | None:
        deadline = self._request_deadline_monotonic_ns
        if deadline is None:
            return ceiling_seconds
        remaining = (deadline - time.monotonic_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TimeoutError("MobileWorld case request deadline elapsed")
        return remaining if ceiling_seconds is None else min(float(ceiling_seconds), remaining)

    @contextmanager
    def request_deadline_scope(self, deadline_monotonic_ns: int) -> Iterator[None]:
        """Temporarily narrow/extend this unit client to an already-authorized deadline."""

        if type(deadline_monotonic_ns) is not int or deadline_monotonic_ns <= time.monotonic_ns():
            raise ValueError("request deadline must be a future monotonic timestamp")
        prior = self._request_deadline_monotonic_ns
        self._request_deadline_monotonic_ns = deadline_monotonic_ns
        try:
            yield
        finally:
            self._request_deadline_monotonic_ns = prior

    @property
    def is_initialized(self) -> bool:
        """Return the local initialization confirmation without performing I/O."""

        return self._initialized is True

    def _ensure_initialized(self):
        """Ensure the device is initialized."""
        if not self._initialized:
            # Initialize the device controller
            init_data = {
                "device": self.device,
            }
            response = self._session.post(
                f"{self.base_url}/init", json=init_data, timeout=self._request_timeout()
            )
            response.raise_for_status()
            self._initialized = True

    def switch_suite_family(self, target_family: str) -> dict:
        """Switch to a different suite family.

        This will restart the emulator with appropriate AVD and reinitialize task registry.

        Args:
            target_family: Either "mobile_world"

        Returns:
            dict: Response from the suite family switch endpoint

        Raises:
            RuntimeError: If the suite family switch fails
        """
        logger.info(f"Switching to suite_family: {target_family}")

        try:
            response = self._session.post(
                f"{self.base_url}/suite_family/switch",
                params={"target_family": target_family},
                timeout=self._request_timeout(300),  # Allow time for emulator restart
            )
            response.raise_for_status()
            result = response.json()
            logger.info(f"Suite family switch result: {result}")

            if result.get("switched"):
                logger.info(
                    f"Successfully switched to {target_family} "
                    f"(AVD: {result.get('avd_name')}, Device: {result.get('emulator_device_id')})"
                )
                # Reset initialization flag since we have a new emulator
                self._initialized = False

            return result
        except requests.RequestException as e:
            logger.error(f"Failed to switch suite family: {e}")
            raise RuntimeError(f"Failed to switch to suite_family {target_family}: {e}")

    def reset(self, go_home: bool) -> Response:
        """Resets the environment by going home if requested."""
        self._ensure_initialized()

        if go_home:
            self.execute_action(JSONAction(action_type=NAVIGATE_HOME))

        return Response(status="success", message="Environment reset")

    @backoff.on_exception(
        backoff.expo,
        Exception,
        max_tries=3,
        on_backoff=lambda details: logger.warning(
            f"Retrying get_screenshot after error (attempt {details['tries']}/3)"
        ),
    )
    def get_screenshot(self, wait_to_stabilize: bool = False) -> Image.Image:
        """Gets the current screenshot of the environment."""
        self._ensure_initialized()

        if wait_to_stabilize:
            time.sleep(self.step_wait_time)

        response = self._session.get(
            f"{self.base_url}/screenshot",
            params={"device": self.device, "return_b64": True},
            timeout=self._request_timeout(),
        )
        # response.raise_for_status()
        if not response.ok:
            logger.error(f"Failed to get screenshot: {response.text}")
            raise RuntimeError(f"Failed to get screenshot: {response.text}")

        # Convert base64 to numpy array
        image_base64 = response.json()["b64_png"]
        image = self._base64_to_pil(image_base64)

        return image

    def get_observation(self, type="screenshot", wait_to_stabilize: bool = True) -> dict:
        """Gets the current observation of the environment."""
        if type == "screenshot":
            return {
                "screenshot": self.get_screenshot(wait_to_stabilize=wait_to_stabilize),
                "accessibility_tree": None,
            }
        elif type == "accessibility_tree":
            raise ValueError("Accessibility tree is not supported yet")
        elif type == "screenshot_and_accessibility_tree":
            raise ValueError("Screenshot and accessibility tree is not supported yet")
        else:
            raise ValueError(f"Unsupported observation type: {type}")

    def execute_action(self, action: JSONAction) -> Observation:
        """Executes an action in the environment."""
        self._ensure_initialized()

        logger.debug(f"Executing action: {action.model_dump_json(exclude_none=True)}")

        # Send JSONAction directly to server
        step_data = {
            "device": self.device,
            "action": action.model_dump(),
        }

        request_endpoint = f"{self.base_url}/step"
        _safe_audit_hook(
            record_gui_request,
            step_data,
            request_endpoint=request_endpoint,
        )
        response = self._session.post(
            request_endpoint, json=step_data, timeout=self._request_timeout()
        )
        _safe_audit_hook(record_gui_response, response)
        logger.debug(f"""execute_action response: {{
            "status": {response.status_code},
            "message": {response.text},
        }}""")

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError(
                f"Step action failed (HTTP {response.status_code}): {response.text}"
            ) from exc
        try:
            response_payload = response.json()
        except Exception as exc:
            raise RuntimeError("Step action returned a non-JSON success envelope") from exc
        expected_action = action.model_dump(mode="json")
        if (
            type(response_payload) is not dict
            or response_payload.get("device") != self.device
            or response_payload.get("action") != expected_action
            or "result" not in response_payload
        ):
            raise RuntimeError("Step action returned an invalid or mismatched success envelope")

        res = self.get_screenshot(wait_to_stabilize=True)
        ask_user_response = None
        if action.action_type == "ask_user":
            ask_user_response = response_payload.get("result", "")
            logger.debug(f"ask_user_response: {ask_user_response}")

        return Observation(
            screenshot=res,
            ask_user_response=ask_user_response,
        )

    def get_suite_task_list(
        self, enable_mcp: bool = False, enable_user_interaction: bool = False
    ) -> list[str]:
        """Gets the list of tasks in the suite.

        Args:
            enable_mcp: If True, include agent-mcp tasks. Default False excludes them.
            enable_user_interaction: If True, include agent-user-interaction tasks. Default False excludes them.

        Returns:
            List of task names filtered by the specified criteria.
            By default (both False), returns only GUI-only tasks.
        """
        self._ensure_initialized()

        response = self._session.get(f"{self.base_url}/task/list", timeout=self._request_timeout())
        response.raise_for_status()
        task_list = response.json()

        # Filter tasks based on tags
        filtered_tasks = []
        for task in task_list:
            tags = task.get("tags", [])
            # Skip agent-mcp tasks if not enabled
            if not enable_mcp and "agent-mcp" in tags:
                continue
            # Skip agent-user-interaction tasks if not enabled
            if not enable_user_interaction and "agent-user-interaction" in tags:
                continue
            filtered_tasks.append(task["name"])
        return filtered_tasks

    def get_suite_task_length(self, task_type: str) -> int:
        """Gets the length of the suite of tasks."""
        # Return 1 since we're simulating single task execution
        return 1

    def reinitialize_suite(
        self,
        n_task_combinations: int = 1,
        seed: int = 42,
        task_family: str = "mobile_world",
    ) -> Response:
        """Reinitializes the suite of tasks."""
        # For the new server, this is just a no-op
        return Response(status="success", message="Suite reinitialized")

    def initialize_task(
        self,
        task_name: str,
        *,
        task_trial: int | None = None,
        task_parameters_sha256: str | None = None,
        reset_seed: int | None = None,
    ) -> Observation:
        """Initializes the task in the environment."""
        self._ensure_initialized()

        try:
            init_data = {"task_name": task_name, "req_device": self.device}
            frozen_values = (task_trial, task_parameters_sha256, reset_seed)
            if any(value is not None for value in frozen_values):
                if (
                    type(task_trial) is not int
                    or not 1 <= task_trial <= 1_000_000
                    or type(task_parameters_sha256) is not str
                    or len(task_parameters_sha256) != 64
                    or any(
                        character not in "0123456789abcdef" for character in task_parameters_sha256
                    )
                    or type(reset_seed) is not int
                    or not 0 <= reset_seed <= 2_147_483_647
                ):
                    raise ValueError("frozen task initialization binding is incomplete or invalid")
                init_data.update(
                    {
                        "task_trial": task_trial,
                        "task_parameters_sha256": task_parameters_sha256,
                        "reset_seed": reset_seed,
                    }
                )
            response = self._session.post(
                f"{self.base_url}/task/init",
                json=init_data,
                timeout=self._request_timeout(TASK_INITIALIZATION_TIMEOUT_SECONDS),
            )
            response.raise_for_status()

            self._current_task_type = task_name

            logger.debug(f"initialize_task response: Task {task_name} initialized")
            res = self.get_screenshot(wait_to_stabilize=True)
            return Observation(
                screenshot=res,
                ask_user_response=None,
            )
        except Exception as e:
            logger.error(f"Failed to initialize task {task_name}: {e}")
            raise RuntimeError(f"Failed to initialize task {task_name}: {e}")

    def tear_down_task(
        self,
        task_type: str,
        *,
        dispatch_started: Callable[[], None] | None = None,
    ) -> Response:
        """Tears down the task in the environment."""
        self._ensure_initialized()

        result = self.tear_down_task_if_initialized(
            task_type,
            dispatch_started=dispatch_started,
        )
        return Response(
            status=(
                "success" if result.status is CleanupTaskTeardownStatusV1.SUCCEEDED else "error"
            ),
            message=result.message,
        )

    def tear_down_task_if_initialized(
        self,
        task_type: str,
        *,
        dispatch_started: Callable[[], None] | None = None,
    ) -> CleanupTaskTeardownResultV1:
        """Tear down only an already initialized client; never call ``/init``.

        This is the production cleanup boundary.  An unknown initialization
        outcome is preserved as ``NOT_INITIALIZED_NO_IO`` so recovery cannot
        borrow cleanup authority to create a new environment session.
        """

        if dispatch_started is not None and not callable(dispatch_started):
            raise TypeError("dispatch_started must be callable")
        if self._initialized is not True:
            return CleanupTaskTeardownResultV1(
                status=CleanupTaskTeardownStatusV1.NOT_INITIALIZED_NO_IO,
                message="Task teardown skipped because environment initialization is unconfirmed",
                request_dispatched=False,
            )

        request_dispatched = False
        try:
            tear_down_data = {"task_name": task_type, "req_device": self.device}
            timeout = self._request_timeout()
            if dispatch_started is not None:
                dispatch_started()
            request_dispatched = True
            response = self._session.post(
                f"{self.base_url}/task/tear_down",
                json=tear_down_data,
                timeout=timeout,
            )
            response.raise_for_status()

            self._current_task_type = None
            return CleanupTaskTeardownResultV1(
                status=CleanupTaskTeardownStatusV1.SUCCEEDED,
                message=f"Task {task_type} torn down",
                request_dispatched=True,
            )
        except Exception as e:
            logger.error(f"Failed to tear down task {task_type}: {e}")
            return CleanupTaskTeardownResultV1(
                status=CleanupTaskTeardownStatusV1.FAILED,
                message=f"Failed to tear down task {task_type}: {str(e)}",
                request_dispatched=request_dispatched,
            )

    def get_task_score(self, task_type: str) -> tuple[float, str]:
        """Gets the score of the current task."""
        self._ensure_initialized()

        try:
            response = self._session.get(
                f"{self.base_url}/task/eval",
                json={"task_name": task_type, "req_device": self.device},
                timeout=self._request_timeout(),
            )
            response.raise_for_status()
            result = response.json()
            if type(result) is not dict or set(result) != {
                "device",
                "reason",
                "score",
                "task_name",
            }:
                raise ValueError("task evaluation returned an invalid success envelope")
            if result["device"] != self.device or result["task_name"] != task_type:
                raise ValueError("task evaluation response binding does not match the request")
            score = result["score"]
            reason = result["reason"]
            if (
                type(score) not in (int, float)
                or not 0.0 <= score <= 1.0
                or not math.isfinite(float(score))
            ):
                raise ValueError("task evaluation score must be a finite number in [0, 1]")
            if type(reason) is not str or not reason or len(reason) > 16_384:
                raise ValueError("task evaluation reason must be a bounded non-empty string")
            return float(score), reason
        except Exception:
            logger.exception(f"Failed to get task score for {task_type}")
            raise RuntimeError(f"Failed to get task score for {task_type}")

    def get_task_goal(self, task_type: str) -> str:
        """Gets the goal of the current task."""
        self._ensure_initialized()

        response = self._session.get(
            f"{self.base_url}/task/goal",
            params={"task_name": task_type},
            timeout=self._request_timeout(),
        )
        response.raise_for_status()
        return response.json()

    def get_task_metadata(self, task_type: str) -> dict:
        """Gets the metadata of the current task."""
        self._ensure_initialized()

        response = self._session.get(
            f"{self.base_url}/task/metadata",
            params={"task_name": task_type},
            timeout=self._request_timeout(),
        )
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        """Closes the environment."""
        self._session.close()

    def health(self) -> bool:
        """Checks the health of the environment."""
        try:
            response = self._session.get(f"{self.base_url}/health", timeout=self._request_timeout())
            response.raise_for_status()
            result = response.json()
            return result.get("ok", False)
        except Exception as e:
            print(f"Environment is not healthy: {e}")
            return False

    def get_task_complexity(self, task_type: str) -> float:
        """Gets the complexity of the current task."""
        self._ensure_initialized()

        response = self._session.get(
            f"{self.base_url}/task/complexity",
            params={"task_name": task_type},
            timeout=self._request_timeout(),
        )
        response.raise_for_status()
        return float(response.json())

    def _base64_to_pil(self, base64_str: str) -> Image.Image:
        """Convert base64 string to numpy array."""
        # Remove data URL prefix if present
        if "," in base64_str:
            base64_str = base64_str.split(",")[-1]

        image_data = base64.b64decode(base64_str)
        image = Image.open(BytesIO(image_data))
        _safe_audit_hook(record_screenshot_source, image, image_data)
        return image

    def get_task_list(self) -> list[str]:
        """Get the list of tasks."""
        response = self._session.get(f"{self.base_url}/task/list", timeout=self._request_timeout())
        response.raise_for_status()
        return response.json()


class AndroidMCPEnvClient(AndroidEnvClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # initialize the MCP client
        logger.debug("initializing the MCP client")
        mcp_client = init_mcp_clients()
        self.tools = []
        self.tools = mcp_client.list_tools_sync()
        self.complete_tool_set = copy.deepcopy(self.tools)
        self.tool_map = {tool["name"]: mcp_client for tool in self.tools}

        logger.debug(f"loaded {len(self.tools)} tools: {[tool['name'] for tool in self.tools]}")

    def reset_tools(self, filters: list[str] = None, task_type=None):
        is_not_mcp_task = True
        if task_type is not None:
            metadata = self.get_task_metadata(task_type=task_type)
            filters = []  # we should set empty tools if task has no mcp tag
            if "agent-mcp" in metadata["tags"]:
                is_not_mcp_task = False
                for app in metadata.get("apps", []):
                    if "MCP" in app:
                        filters.append(app.split("-")[-1])
            logger.debug(f"setting filters for task {task_type}: {filters}")

        if filters is not None:
            self.tools = [
                tool
                for tool in self.complete_tool_set
                if any(f.lower() in tool["name"].lower() for f in filters)
            ]
            assert len(self.tools) > 0 or is_not_mcp_task, f"No tools found for task {task_type}"
            logger.debug(f"reset tools: {self.tools}")

    def _truncate_tool_call(self, tool_call: dict) -> dict:
        """Truncate the tool call to 1000 characters."""
        if tool_call is not None:
            if "text" in tool_call and tool_call["text"].startswith("<!DOCTYPE html>"):
                tool_call["text"] = markdownify(tool_call["text"])
        return tool_call

    def execute_action(self, action: JSONAction) -> Observation:
        if action.action_type == MCP:
            action_name = action.action_name
            action_args = action.action_json
            client = self.tool_map[action_name]
            _safe_audit_hook(
                record_mcp_request,
                action_name=action_name,
                action_arguments=action_args,
            )
            result = client.call_tool_sync(action_name, action_args)
            _safe_audit_hook(record_mcp_raw_result, result)
            result = self._truncate_tool_call(result)
            _safe_audit_hook(record_mcp_visible_result, result)

            res = self.get_screenshot(wait_to_stabilize=True)
            return Observation(
                screenshot=res,
                ask_user_response=None,
                tool_call=result,
            )
        else:
            return super().execute_action(action)


def parse_result_file(result_file: str) -> tuple[float, str | None]:
    """Parse the result file."""
    with open(result_file) as f:
        lines = f.readlines()
        if len(lines) > 0 and "score:" in lines[0]:
            score = float(lines[0].split("score:")[1].strip())
        else:
            score = None

        if len(lines) > 1:
            reason = lines[1].strip()
        else:
            reason = None
        return score, reason


def scan_finished_tasks(
    log_file_root: str, task_list: list[str] = None
) -> tuple[list[str], list[float]]:
    """Scan for finished tasks in log directory."""
    if not os.path.exists(log_file_root):
        return [], []

    dirs = [
        d
        for d in os.listdir(log_file_root)
        if os.path.exists(os.path.join(log_file_root, d, SCORE_FILE_NAME))
        and "backup" not in d
        and (task_list is None or d in task_list)
    ]

    result_files = [os.path.join(log_file_root, d, SCORE_FILE_NAME) for d in dirs]
    results = []
    for result_file in result_files:
        score, _ = parse_result_file(result_file)
        results.append(score)
    return dirs, results
