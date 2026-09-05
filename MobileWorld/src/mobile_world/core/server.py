# server.py
"""FastAPI server for Mobile GUI Agent Benchmark."""

import asyncio
import base64
import hashlib
import json
import math
import os
import random
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from mobile_world.runtime.app_helpers.mall import get_config, write_callback_file
from mobile_world.runtime.controller import AndroidController, DeviceUnhealthyError
from mobile_world.runtime.user_agent_config import validate_user_agent_environment
from mobile_world.runtime.utils.constants import ARTIFACTS_ROOT, device_dir
from mobile_world.runtime.utils.docker import restart_emulator_with_avd
from mobile_world.runtime.utils.helpers import AdbResponse
from mobile_world.runtime.utils.models import (
    ANSWER,
    ASK_USER,
    CLICK,
    DOUBLE_TAP,
    DRAG,
    INPUT_TEXT,
    KEYBOARD_ENTER,
    LONG_PRESS,
    NAVIGATE_BACK,
    NAVIGATE_HOME,
    OPEN_APP,
    SCROLL,
    STATUS,
    SWIPE,
    UNKNOWN,
    WAIT,
    InitRequest,
    SmsRequest,
    StepRequest,
    TaskCallbackRequest,
    TaskOperationRequest,
)
from mobile_world.tasks.registry import TaskRegistry

SUITE_FAMILY: str = "mobile_world"
RUNNING_TASK = None
AVD_MAPPING: dict[str, str] = {
    "mobile_world": "Pixel_8_API_34_x86_64",
}


def initialize_suite_family(suite_family: str) -> None:
    """Initialize the suite family and task registry.

    Args:
        suite_family: Either "mobile_world"
    """
    global SUITE_FAMILY, task_registry

    # Any GUI agent can emit ``ask_user``, including on tasks selected as GUI-only.
    # Fail before the backend accepts an eval rather than during a task transition.
    validate_user_agent_environment()

    SUITE_FAMILY = suite_family
    logger.info(f"Initializing suite_family: {suite_family}")

    task_registry = TaskRegistry()
    logger.info(f"Loaded {len(task_registry.tasks)} mobile_world tasks")


CONTROLLERS: dict[str, AndroidController] = {}

# Snapshot restore and emulator restart both transition the same global emulator.
# Keep the lock for the entire transition, not merely for the restart decision.
_lifecycle_lock = threading.RLock()
_lifecycle_state_lock = threading.Lock()
_lifecycle_transition_started: float | None = None
_lifecycle_transition_name: str | None = None
_last_restart_success: float | None = None
RESTART_COOLDOWN_SECONDS = 300  # Minimum time between restart attempts
# Match the host client's task-initialization budget: a probe stays liveness-OK
# while that request may still complete, then becomes unhealthy at the same bound.
MAX_LIFECYCLE_TRANSITION_SECONDS = 600


class TaskInitializationError(RuntimeError):
    """Raised when a task reports that its initialization did not complete."""


def _serialized_device_operation(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Keep a complete device operation on one emulator lifecycle generation."""

    @wraps(operation)
    def serialized(*args: Any, **kwargs: Any) -> Any:
        with _lifecycle_lock:
            owns_transition = _begin_lifecycle_transition(operation.__name__)
            try:
                return operation(*args, **kwargs)
            finally:
                _end_lifecycle_transition(owns_transition)

    return serialized


def _begin_lifecycle_transition(name: str) -> bool:
    global _lifecycle_transition_name, _lifecycle_transition_started
    with _lifecycle_state_lock:
        if _lifecycle_transition_started is not None:
            return False
        _lifecycle_transition_started = time.monotonic()
        _lifecycle_transition_name = name
        return True


def _end_lifecycle_transition(owns_transition: bool) -> None:
    global _lifecycle_transition_name, _lifecycle_transition_started
    if not owns_transition:
        return
    with _lifecycle_state_lock:
        _lifecycle_transition_started = None
        _lifecycle_transition_name = None


def _lifecycle_transition_snapshot() -> tuple[str | None, float | None]:
    with _lifecycle_state_lock:
        if _lifecycle_transition_started is None:
            return None, None
        return (
            _lifecycle_transition_name,
            max(0.0, time.monotonic() - _lifecycle_transition_started),
        )


def _ensure_controller_healthy(req_device: str) -> AndroidController:
    if req_device not in CONTROLLERS:
        logger.info(f"[INIT] Device {req_device} not initialized, initializing...")
        ctr = AndroidController(device=req_device)
        CONTROLLERS[req_device] = ctr
    viewport_size = getattr(CONTROLLERS[req_device], "viewport_size", (None, None))
    if not (
        isinstance(viewport_size, tuple)
        and len(viewport_size) == 2
        and all(isinstance(value, int) and value > 0 for value in viewport_size)
    ):
        raise DeviceUnhealthyError(f"Device is not healthy: invalid viewport for {req_device}")
    if not CONTROLLERS[req_device].check_health(try_times=3):
        logger.error(f"[INIT] Device {req_device} is not healthy")
        raise DeviceUnhealthyError(f"Device is not healthy: {req_device}")
    return CONTROLLERS[req_device]


def ensure_controller(req_device: str) -> AndroidController:
    with _lifecycle_lock:
        try:
            return _ensure_controller_healthy(req_device)
        except DeviceUnhealthyError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error


def _restart_and_verify_emulator(req_device: str) -> AndroidController:
    """Perform one global emulator restart and install only a verified controller."""
    avd_name = AVD_MAPPING[SUITE_FAMILY]
    try:
        device_id = restart_emulator_with_avd(avd_name)
        replacement = AndroidController(device=device_id)
        viewport_size = replacement.viewport_size
        if not all(isinstance(value, int) and value > 0 for value in viewport_size):
            raise DeviceUnhealthyError(
                f"Device is not healthy after emulator restart for {req_device}: invalid viewport"
            )
        replacement.wait_for_device_stability()
    except Exception as error:
        if isinstance(error, DeviceUnhealthyError):
            raise
        raise DeviceUnhealthyError(
            f"Device is not healthy after emulator restart for {req_device}: {error}"
        ) from error

    # A restart replaces the global emulator generation.  Publish the new
    # controller atomically only after its continuous-health barrier succeeds.
    CONTROLLERS.clear()
    CONTROLLERS[req_device] = replacement
    logger.info(
        f"[RECOVERY] Verified restarted emulator with AVD {avd_name}, device_id={device_id}"
    )
    return replacement


def _mark_restart_success() -> None:
    global _last_restart_success
    _last_restart_success = time.monotonic()


app = FastAPI(title="Mobile GUI Agent Benchmark Server", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


task_registry = None


@app.get("/health")
def health():
    """Check health of all registered devices.

    If any device is unhealthy, automatically restarts the emulator for the current
    suite family. Implements locking to prevent concurrent restart attempts.
    """
    # Health probes must never race a planned snapshot/restart transition, and
    # must not wait behind a potentially long emulator operation.  HTTP 200
    # keeps container liveness distinct from temporary device unavailability.
    if not _lifecycle_lock.acquire(blocking=False):
        transition_name, transition_elapsed = _lifecycle_transition_snapshot()
        transition_overdue = (
            transition_elapsed is not None
            and transition_elapsed >= MAX_LIFECYCLE_TRANSITION_SECONDS
        )
        return JSONResponse(
            status_code=503 if transition_overdue else 200,
            content={
                "ok": False,
                "devices": list(CONTROLLERS.keys()),
                "device_status": {},
                "transition_in_progress": True,
                "transition_name": transition_name,
                "transition_elapsed_seconds": transition_elapsed,
                "transition_overdue": transition_overdue,
            },
        )

    owns_transition = _begin_lifecycle_transition("health")
    try:
        device_status = {}
        unhealthy_devices = []

        for device_id, controller in CONTROLLERS.items():
            is_healthy = controller.check_health(try_times=2)
            device_status[device_id] = is_healthy
            if not is_healthy:
                unhealthy_devices.append(device_id)

        all_healthy = not unhealthy_devices
        if not all_healthy:
            if RUNNING_TASK is not None:
                # Never replace the emulator underneath an active task attempt.
                # The next task operation must observe the factual device
                # failure; the following /task/init may then recover from a
                # clean snapshot inside the new attempt boundary.
                logger.warning(
                    f"[HEALTH] Recovery deferred for active task "
                    f"{RUNNING_TASK.name}; unhealthy devices: {unhealthy_devices}"
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "ok": False,
                        "devices": list(CONTROLLERS.keys()),
                        "device_status": device_status,
                        "recovery_deferred_for_active_task": True,
                        "active_task": RUNNING_TASK.name,
                    },
                )

            now = time.monotonic()
            cooldown_active = (
                _last_restart_success is not None
                and now - _last_restart_success < RESTART_COOLDOWN_SECONDS
            )

            if not cooldown_active:
                try:
                    logger.warning(
                        f"[HEALTH] Unhealthy devices detected: {unhealthy_devices}. "
                        f"Restarting emulator for suite family: {SUITE_FAMILY}"
                    )
                    req_device = unhealthy_devices[0]
                    _restart_and_verify_emulator(req_device)
                    _mark_restart_success()
                    device_status = {device_id: True for device_id in CONTROLLERS}
                    all_healthy = True
                except Exception as error:
                    # A failed restart does not arm the cooldown.  The old
                    # controller generation remains unavailable and must not be
                    # reported healthy.
                    logger.error(
                        f"[HEALTH] Failed to restart emulator for suite family "
                        f"{SUITE_FAMILY}: {error}",
                        exc_info=True,
                    )
            else:
                time_since_last = now - _last_restart_success
                logger.debug(
                    f"[HEALTH] Restart skipped - cooldown after verified restart active "
                    f"(last success: {time_since_last:.1f}s ago, "
                    f"cooldown: {RESTART_COOLDOWN_SECONDS}s)"
                )

        return JSONResponse(
            status_code=200 if all_healthy else 503,
            content={
                "ok": all_healthy,
                "devices": list(CONTROLLERS.keys()),
                "device_status": device_status,
            },
        )
    finally:
        _end_lifecycle_transition(owns_transition)
        _lifecycle_lock.release()


def _init_controller(device: str) -> dict[str, Any]:
    """Helper function to initialize controller and return response."""
    logger.info(f"[INIT] Request: device={device}")

    ctr = ensure_controller(device)
    width, height = ctr.viewport_size
    response = {
        "device": device,
        "viewport_size": [width, height],
    }
    logger.info(f"[INIT] Success: {response}")
    return response


@app.get("/init")
@_serialized_device_operation
def init_controller_get(device: str = Query("emulator-5554", description="adb device ID")):
    """Initialize controller via GET request."""
    return _init_controller(device)


@app.post("/init")
@_serialized_device_operation
def init_controller_post(req: InitRequest):
    """Initialize controller via POST request."""
    return _init_controller(req.device)


@app.get("/state")
@_serialized_device_operation
def get_state(device: str = Query(..., description="adb device ID")):
    logger.info(f"[STATE] Request: device={device}")

    ctr = ensure_controller(device)
    activity = ctr.get_current_activity()
    app_pkg = ctr.get_current_app()
    width, height = ctr.viewport_size
    response = {
        "device": device,
        "viewport_size": [width, height],
        "current_activity": activity,
        "current_app": app_pkg,
    }
    logger.info(f"[STATE] Response: {response}")
    return response


@app.get("/screenshot")
@_serialized_device_operation
def get_screenshot(
    device: str = Query(...),
    prefix: str | None = Query(None),
    return_b64: bool = Query(False),
):
    logger.info(f"[SCREENSHOT] Request: device={device}, prefix={prefix}, return_b64={return_b64}")

    ctr = ensure_controller(device)
    ddir = device_dir(ARTIFACTS_ROOT, device) / "screens"
    ddir.mkdir(parents=True, exist_ok=True)
    name = prefix or time.strftime("%Y%m%d_%H%M%S")
    result = ctr.get_screenshot(name, str(ddir), try_times=2)
    if not result.success:
        logger.error(f"[SCREENSHOT] Failed to capture screenshot for device {device}")
        raise HTTPException(status_code=500, detail=f"screencap/pull failed: {result.error}")

    if return_b64:
        with open(result.output, "rb") as f:
            b = base64.b64encode(f.read()).decode("utf-8")
        response = {"device": device, "path": str(result.output), "b64_png": b}
        logger.info(f"[SCREENSHOT] Success (b64): device={device}, path={result.output}")
        return response
    # Default return file path (can also be retrieved via /download)
    response = {"device": device, "path": str(result.output)}
    logger.info(f"[SCREENSHOT] Success: {response}")
    return response


@app.get("/download")
def download(path: str = Query(..., description="absolute path of the file on the server")):
    p = Path(path)
    if not p.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(p))


# Static task assets (e.g. images embedded in chat). Tasks reference these from the
# device as http://10.0.2.2:6800/task-asset/<path>, hosting them locally instead of
# depending on flaky external image services.
_TASK_DEFINITIONS_ROOT = (
    Path(__file__).resolve().parent.parent / "tasks" / "definitions"
).resolve()


@app.get("/task-asset/{asset_path:path}")
def get_task_asset(asset_path: str):
    """Serve a file from a task's ``assets/`` directory under tasks/definitions."""
    target = (_TASK_DEFINITIONS_ROOT / asset_path).resolve()
    rel = os.path.relpath(target, _TASK_DEFINITIONS_ROOT)
    if rel.startswith("..") or "assets" not in Path(rel).parts:
        raise HTTPException(status_code=400, detail="invalid asset path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(str(target))


@app.get("/xml")
@_serialized_device_operation
def get_xml(
    device: str = Query(...),
    prefix: str | None = Query(None),
    mode: Literal["uia", "ac"] = Query("uia"),
    return_content: bool = Query(False),
):
    logger.info(
        f"[XML] Request: device={device}, prefix={prefix}, mode={mode}, return_content={return_content}"
    )

    ctr = ensure_controller(device)
    ddir = device_dir(ARTIFACTS_ROOT, device) / "xml"
    ddir.mkdir(parents=True, exist_ok=True)
    name = prefix or time.strftime("%Y%m%d_%H%M%S")

    if mode == "uia":
        local_path = ctr.get_xml(name, str(ddir))
    else:
        local_path = ctr.get_ac_xml(name, str(ddir))

    if local_path == "ERROR":
        logger.error(f"[XML] Failed to get {mode} XML for device {device}")
        raise HTTPException(status_code=500, detail=f"xml {mode} pull failed")

    resp: dict[str, Any] = {"device": device, "mode": mode, "path": str(local_path)}
    if return_content:
        try:
            content = Path(local_path).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            content = Path(local_path).read_text(errors="ignore")
        resp["content"] = content
        logger.info(
            f"[XML] Success with content: device={device}, mode={mode}, path={local_path}, content_length={len(content)}"
        )
    else:
        logger.info(f"[XML] Success: {resp}")
    return resp


@app.post("/sms")
@_serialized_device_operation
def simulate_sms(req: SmsRequest):
    """Send a simulated SMS to the device."""
    logger.info(f"[SMS] Request: device={req.device}, sender={req.sender}, message={req.message}")

    ctr = ensure_controller(req.device)
    ret = ctr.simulate_sms(req.sender, req.message)

    if not ret.success:
        logger.error(f"[SMS] Failed to send SMS to device {req.device}: {ret.error}")
        raise HTTPException(status_code=500, detail=f"Failed to send SMS: {ret.error}")

    response = {
        "device": req.device,
        "sender": req.sender,
        "message": req.message,
        "result": ret.output,
    }
    logger.info(f"[SMS] Success: {response}")
    return response


@app.post("/step")
@_serialized_device_operation
def step(req: StepRequest):
    logger.info(f"[STEP] Request: device={req.device}, action={req.action}")

    ctr = ensure_controller(req.device)

    try:
        action = req.action
        action_type = action.action_type

        if action_type == CLICK:
            x, y = int(action.x), int(action.y)
            logger.info(f"[STEP] Executing click at ({x}, {y})")
            ret = ctr.tap(x, y)

        elif action_type == SWIPE:
            direction = action.direction or "up"
            logger.info(
                f"[STEP] Executing swipe: x={action.x}, y={action.y}, direction={direction}"
            )
            ret = ctr.swipe(action.x, action.y, direction)

        elif action_type == INPUT_TEXT:
            text = action.text
            logger.info(f"[STEP] Executing text input: '{text}'")
            if type(text) is not str or not text:
                raise HTTPException(status_code=400, detail="input_text requires non-empty text")
            ret = ctr.text(text)

        elif action_type == NAVIGATE_BACK:
            logger.info("[STEP] Executing back button")
            ret = ctr.back()

        elif action_type == NAVIGATE_HOME:
            logger.info("[STEP] Executing home button")
            ret = ctr.home()

        elif action_type == KEYBOARD_ENTER:
            logger.info("[STEP] Executing enter key")
            ret = ctr.enter()

        elif action_type == LONG_PRESS:
            x, y = int(action.x), int(action.y)
            logger.info(f"[STEP] Executing long_press at ({x}, {y})")
            ret = ctr.long_press(x, y, 1000)

        elif action_type == DOUBLE_TAP:
            x, y = int(action.x), int(action.y)
            logger.info(f"[STEP] Executing double_tap at ({x}, {y})")
            ret = ctr.double_tap(x, y)

        elif action_type == DRAG:
            start_x, start_y = int(action.start_x), int(action.start_y)
            end_x, end_y = int(action.end_x), int(action.end_y)
            logger.info(f"[STEP] Executing drag from ({start_x}, {start_y}) to ({end_x}, {end_y})")
            ret = ctr.drag(start_x, start_y, end_x, end_y)

        elif action_type == SCROLL:
            # Map scroll to swipe for compatibility
            if action.direction in ["left", "right"]:
                direction = action.direction
            else:
                # scroll direction is reversed compared to swipe
                direction = "down" if action.direction == "up" else "up"
            logger.info(
                f"[STEP] Executing scroll: direction={action.direction}; equivalent to swipe {direction}"
            )
            ret = ctr.swipe(None, None, direction)

        elif action_type == OPEN_APP:
            app_name = action.app_name
            logger.info(f"[STEP] Executing open_app: {app_name}")
            ret = ctr.launch_app(app_name)

        elif action_type == WAIT:
            logger.info("[STEP] Executing wait for 1 second")
            time.sleep(1.0)
            ret = "OK"

        elif action_type == ANSWER:
            text = action.text or ""
            logger.info(f"[STEP] Executing answer: '{text}'")
            ctr.answer(text)
            ret = "OK"

        elif action_type == STATUS:
            status = action.goal_status or "unknown"
            logger.info(f"[STEP] Executing status: {status}")
            ret = status

        elif action_type == ASK_USER:
            logger.info("[STEP] Executing ask_user")
            agent_question = action.text
            ret = ctr.ask_user(agent_question)

        elif action_type == UNKNOWN:
            logger.info("[STEP] Executing unknown action")
            ret = "UNKNOWN_ACTION"

        else:
            logger.error(f"[STEP] Unknown action: {action_type}")
            raise HTTPException(status_code=400, detail=f"unknown action: {action_type}")

        if isinstance(ret, AdbResponse):
            ret = ret.output
        else:
            ret = ret if ret is not None else "OK"
        response = {
            "device": req.device,
            "action": action,
            "result": ret,
        }
        logger.info(f"[STEP] Success: {response}")
        return response
    except KeyError as e:
        logger.error(f"[STEP] Missing parameter: {e}")
        raise HTTPException(status_code=400, detail=f"missing param: {e}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[STEP] Error executing action {action_type}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/task/list")
def get_task_list():
    """Get list of available tasks with their metadata."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    logger.info("[TASK_LIST] Getting available tasks")

    task_list = []
    for task_name, task in task_registry.tasks.items():
        task_list.append(
            {
                "name": task_name,
                "tags": list(task.task_tags) if hasattr(task, "task_tags") else [],
                "apps": list(task.app_names) if hasattr(task, "app_names") else [],
            }
        )

    logger.info(f"[TASK_LIST] Returning {len(task_list)} tasks")
    return JSONResponse(status_code=200, content=task_list)


@app.get("/task/goal")
def get_task_goal(task_name: str):
    """Get goal of a task."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    logger.info(f"[TASK_GOAL] Getting goal for task: {task_name}")
    task = task_registry.get_task(task_name)
    return JSONResponse(status_code=200, content=task.goal)


@app.get("/task/metadata")
def get_task_metadata(task_name: str):
    """Get metadata of a task."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )
    logger.info(f"[TASK_METADATA] Getting metadata for task: {task_name}")
    task = task_registry.get_task(task_name)
    metadata = {
        "name": task_name,
        "tags": list(task.task_tags) if hasattr(task, "task_tags") else [],
        "apps": list(task.app_names) if hasattr(task, "app_names") else [],
    }
    return JSONResponse(status_code=200, content=metadata)


def _initialize_task_once(
    task: Any,
    controller: AndroidController,
    *,
    reset_seed: int | None = None,
) -> None:
    if reset_seed is not None:
        # Task initialization is serialized by ``_serialized_device_operation``.
        # Re-seed on every infrastructure retry so a failed first attempt cannot
        # advance the matched episode's task-generation stream.
        random.seed(reset_seed, version=2)
    result = task.initialize_task(controller)
    if result is False:
        task.initialized = False
        raise TaskInitializationError(
            f"Failed to initialize task: {task.name} (initialize_task returned False)"
        )
    if not task.initialized:
        raise TaskInitializationError(f"Failed to initialize task: {task.name}")


@app.post("/task/init")
@_serialized_device_operation
def init_task(req: TaskOperationRequest):
    """Initialize a task."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    frozen_values = (req.task_trial, req.task_parameters_sha256, req.reset_seed)
    if any(value is not None for value in frozen_values):
        if (
            type(req.task_trial) is not int
            or not 1 <= req.task_trial <= 1_000_000
            or type(req.task_parameters_sha256) is not str
            or len(req.task_parameters_sha256) != 64
            or any(character not in "0123456789abcdef" for character in req.task_parameters_sha256)
            or type(req.reset_seed) is not int
            or not 0 <= req.reset_seed <= 2_147_483_647
        ):
            raise HTTPException(
                status_code=400,
                detail="Frozen task initialization binding is incomplete or invalid",
            )
        canonical_parameters = json.dumps(
            {"task_name": req.task_name, "trial": req.task_trial},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(canonical_parameters).hexdigest() != req.task_parameters_sha256:
            raise HTTPException(
                status_code=400,
                detail="Frozen task parameters do not match their canonical hash",
            )

    global RUNNING_TASK
    logger.info(f"[TASK_INIT] Initializing task: {req.task_name}")

    with _lifecycle_lock:
        # Do not leave the previous singleton task addressable if any part of a
        # new initialization fails.
        RUNNING_TASK = None
        task = None
        try:
            task = task_registry.get_task(req.task_name)
            try:
                ctr = _ensure_controller_healthy(req.req_device)
                _initialize_task_once(task, ctr, reset_seed=req.reset_seed)
            except DeviceUnhealthyError as initial_error:
                # Snapshot acknowledgement can be followed by a delayed ADB
                # disconnect.  Recover once, inside this same request, so a
                # successful repair never becomes a runner-level crashed attempt.
                logger.warning(
                    f"[TASK_INIT] Device transition failed for {req.task_name}; "
                    f"performing one serialized emulator recovery: {initial_error}"
                )
                task.initialized = False
                ctr = _restart_and_verify_emulator(req.req_device)
                _mark_restart_success()
                _initialize_task_once(task, ctr, reset_seed=req.reset_seed)
        except Exception as error:
            if task is not None:
                task.initialized = False
            logger.error(f"[TASK_INIT] Error initializing task: {error}")
            raise HTTPException(
                status_code=500,
                detail=f"Error initializing task: {error}",
            ) from error

        RUNNING_TASK = task
    return JSONResponse(status_code=200, content="OK")


@app.get("/task/eval")
@_serialized_device_operation
def eval_task(req: TaskOperationRequest):
    """Check if a task is successful."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    ctr = ensure_controller(req.req_device)
    logger.info(f"[TASK_IS_SUCCESSFUL] Checking if task is successful: {req.task_name}")
    task = task_registry.get_task(req.task_name)
    if RUNNING_TASK is None or RUNNING_TASK is not task:
        raise HTTPException(
            status_code=409,
            detail="Requested task is not the currently running task",
        )
    ret = task.is_successful(ctr)
    if type(ret) is tuple:
        if len(ret) != 2:
            raise HTTPException(status_code=500, detail="Task evaluator returned an invalid tuple")
        score, reason = ret
    else:
        score = ret
        reason = "TASK_EVALUATOR_RETURNED_SCALAR"
    if (
        type(score) not in (int, float)
        or not 0.0 <= score <= 1.0
        or not math.isfinite(float(score))
    ):
        raise HTTPException(
            status_code=500,
            detail="Task evaluator score must be a finite number in [0, 1]",
        )
    if type(reason) is not str or not reason or len(reason) > 16_384:
        raise HTTPException(
            status_code=500,
            detail="Task evaluator reason must be a bounded non-empty string",
        )
    return JSONResponse(
        status_code=200,
        content={
            "device": req.req_device,
            "reason": reason,
            "score": float(score),
            "task_name": req.task_name,
        },
    )


@app.post("/task/tear_down")
@_serialized_device_operation
def tear_down_task(req: TaskOperationRequest):
    """Tear down a task."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    logger.info(f"[TASK_TEAR_DOWN] Tearing down task: {req.task_name}")
    ctr = ensure_controller(req.req_device)
    task = task_registry.get_task(req.task_name)
    task.tear_down(ctr)
    global RUNNING_TASK
    RUNNING_TASK = None
    return JSONResponse(status_code=200, content="OK")


@app.get("/task/complexity")
def get_task_complexity(task_name: str):
    """Get complexity of a task."""
    if task_registry is None:
        raise HTTPException(
            status_code=500, detail="Task registry not initialized. Server not properly configured."
        )

    logger.info(f"[TASK_COMPLEXITY] Getting complexity for task: {task_name}")
    try:
        task = task_registry.get_task(task_name)
        return JSONResponse(status_code=200, content=task.complexity)
    except Exception as e:
        logger.error(f"[TASK_COMPLEXITY] Error getting complexity for task: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting complexity for task: {str(e)}")


@app.post("/task/callback")
def save_task_callback(req: TaskCallbackRequest):
    """Save task callback data to a temporary file for evaluation.

    Args:
        req: TaskCallbackRequest containing device, task_name, and callback_data

    Returns:
        JSONResponse with the path to the saved callback file
    """

    logger.info(
        f"[TASK_CALLBACK] Saving callback data for task: {RUNNING_TASK.__class__.__name__} on device: {req.device}"
    )

    try:
        callback_file = write_callback_file(
            req.callback_data, RUNNING_TASK.__class__.__name__, req.device
        )

        response = {
            "device": req.device,
            "callback_file": callback_file,
        }
        logger.info(f"[TASK_CALLBACK] Successfully saved callback to: {callback_file}")
        return JSONResponse(status_code=200, content=response)

    except Exception as e:
        logger.error(f"[TASK_CALLBACK] Failed to save callback data: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save callback data: {str(e)}")


@app.get("/config/callback")
def get_mall_config():
    """Get configuration for mall app.

    Returns:
        JSONResponse with the mall app configuration
    """
    logger.info("[CONFIG] Getting mall app configuration")

    config = get_config()
    return JSONResponse(status_code=200, content=config.model_dump())


@app.post("/suite_family/switch")
@_serialized_device_operation
def switch_suite_family(target_family: str = Query(..., description="Target suite family")):
    """Switch to a different suite family.

    This will:
    1. Clear controller registry (clients need to re-initialize)
    2. Restart emulator with appropriate AVD (calls /app/docker/start_emulator.sh)
    3. Reinitialize the task registry

    The emulator restart script handles:
    - Killing existing emulators
    - Starting new emulator with target AVD
    - Waiting for boot completion
    - Disabling animations

    Args:
        target_family: Either "mobile_world"
    """
    global CONTROLLERS

    logger.info(f"[SUITE_FAMILY_SWITCH] Switching from {SUITE_FAMILY} to {target_family}")

    # Validate target family
    if target_family not in ["mobile_world"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid suite_family: {target_family}. Must be 'mobile_world'",
        )

    with _lifecycle_lock:
        is_healthy = all(ctr.check_health() for ctr in CONTROLLERS.values())

        if SUITE_FAMILY == target_family and is_healthy:
            logger.info(f"[SUITE_FAMILY_SWITCH] Already on {target_family}, no switch needed")
            return JSONResponse(
                status_code=200,
                content={
                    "message": f"Already on suite_family {target_family}",
                    "suite_family": target_family,
                    "switched": False,
                },
            )

        try:
            target_avd = AVD_MAPPING[target_family]

            logger.info("[SUITE_FAMILY_SWITCH] Clearing controller registry")
            CONTROLLERS.clear()

            logger.info(f"[SUITE_FAMILY_SWITCH] Restarting emulator with AVD {target_avd}")
            device_id = restart_emulator_with_avd(target_avd)

            logger.info(f"[SUITE_FAMILY_SWITCH] Reinitializing task registry for {target_family}")
            initialize_suite_family(target_family)

            response = {
                "message": f"Successfully switched to {target_family}",
                "suite_family": target_family,
                "switched": True,
                "emulator_device_id": device_id,
                "avd_name": target_avd,
                "num_tasks": (
                    len(task_registry.tasks)
                    if hasattr(task_registry, "tasks")
                    else len(task_registry.list_tasks())
                ),
            }

            logger.info(f"[SUITE_FAMILY_SWITCH] Success: {response}")
            return JSONResponse(status_code=200, content=response)

        except Exception as e:
            logger.error(f"[SUITE_FAMILY_SWITCH] Error switching suite family: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to switch suite family: {str(e)}")


def main():
    # Initialize with default suite family
    initialize_suite_family("mobile_world")

    asyncio.run(uvicorn.run(app, host="0.0.0.0", port=6800, reload=True, log_level="debug"))


if __name__ == "__main__":
    main()
