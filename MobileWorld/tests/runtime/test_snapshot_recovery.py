from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import threading
import time
from collections.abc import Callable
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from PIL import Image

from mobile_world.core import server
from mobile_world.runtime import controller as controller_module
from mobile_world.runtime.client import TASK_INITIALIZATION_TIMEOUT_SECONDS, AndroidEnvClient
from mobile_world.runtime.controller import (
    SNAPSHOT_STABILITY_SECONDS,
    SNAPSHOT_STABILITY_TIMEOUT_SECONDS,
    AndroidController,
    DeviceUnhealthyError,
)
from mobile_world.runtime.utils import helpers as helpers_module
from mobile_world.runtime.utils.helpers import AdbResponse
from mobile_world.runtime.utils.models import JSONAction, StepRequest, TaskOperationRequest
from mobile_world.tasks.base import BaseTask


def test_execute_adb_timeout_kills_children_and_prevents_late_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_adb = fake_bin / "adb"
    child_pid_path = tmp_path / "child.pid"
    late_write_path = tmp_path / "late-write"
    fake_adb.write_text(
        "#!/usr/bin/env bash\n"
        '(sleep 0.6; printf late > "$ADB_LATE_WRITE_PATH") &\n'
        'printf \'%s\\n\' "$!" > "$ADB_CHILD_PID_PATH"\n'
        "wait\n",
        encoding="utf-8",
    )
    fake_adb.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("ADB_CHILD_PID_PATH", str(child_pid_path))
    monkeypatch.setenv("ADB_LATE_WRITE_PATH", str(late_write_path))
    monkeypatch.setattr(helpers_module, "ADB_TERMINATION_GRACE_SECONDS", 0.2)

    response = helpers_module.execute_adb(
        "adb shell fixture",
        timeout_seconds=0.1,
    )

    assert response.success is False
    assert "timed out" in response.error
    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("timed-out ADB child process remained alive")
    time.sleep(0.7)
    assert not late_write_path.exists()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _controller_without_adb(device: str = "emulator-fixture") -> AndroidController:
    controller = object.__new__(AndroidController)
    controller.device = device
    return controller


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color="white").save(output, format="PNG")
    return output.getvalue()


def test_snapshot_console_ok_rejects_late_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    controller = _controller_without_adb()
    checks: list[float] = []

    monkeypatch.setattr(
        controller_module,
        "execute_adb",
        lambda *_args, **_kwargs: AdbResponse(success=True, output="OK"),
    )
    monkeypatch.setattr(controller_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(controller_module.time, "sleep", clock.sleep)

    def health(*, try_times: int = 0, timeout_seconds: float | None = None) -> bool:
        assert try_times == 0
        assert timeout_seconds is not None and timeout_seconds <= 3
        checks.append(clock.now)
        return clock.now < 8.6

    controller.check_health = health
    controller.check_screenshot_readiness = lambda *, timeout_seconds: True

    with pytest.raises(DeviceUnhealthyError, match="Device is not healthy"):
        controller.load_snapshot(
            "init_state",
            stable_for_seconds=12,
            timeout_seconds=15,
            poll_interval_seconds=1,
        )

    assert any(check >= 9 for check in checks)
    assert clock.now == 15


def test_snapshot_stability_succeeds_only_after_continuous_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    controller = _controller_without_adb()
    checks: list[float] = []

    monkeypatch.setattr(
        controller_module,
        "execute_adb",
        lambda *_args, **_kwargs: AdbResponse(success=True, output="OK"),
    )
    monkeypatch.setattr(controller_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(controller_module.time, "sleep", clock.sleep)

    def health(*, try_times: int = 0, timeout_seconds: float | None = None) -> bool:
        assert try_times == 0
        assert timeout_seconds is not None and timeout_seconds <= 3
        checks.append(clock.now)
        return True

    controller.check_health = health
    controller.check_screenshot_readiness = lambda *, timeout_seconds: True

    assert (
        controller.load_snapshot(
            "init_state",
            stable_for_seconds=3,
            timeout_seconds=5,
            poll_interval_seconds=1,
        )
        is True
    )
    assert checks == [0, 1, 2, 3]
    assert SNAPSHOT_STABILITY_SECONDS >= 12
    assert SNAPSHOT_STABILITY_TIMEOUT_SECONDS > SNAPSHOT_STABILITY_SECONDS


@pytest.mark.parametrize("payload", [b"", b"not-a-png"])
def test_screenshot_readiness_rejects_empty_or_invalid_png(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
) -> None:
    controller = _controller_without_adb()
    monkeypatch.setattr(
        controller_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=payload, stderr=b""),
    )

    assert controller.check_screenshot_readiness(timeout_seconds=0.25) is False


def test_screenshot_readiness_accepts_decodable_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller_without_adb()
    observed: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.update(args=args, **kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=_png_bytes(), stderr=b"")

    monkeypatch.setattr(controller_module.subprocess, "run", run)

    assert controller.check_screenshot_readiness(timeout_seconds=0.25) is True
    assert observed["timeout"] == 0.25
    assert observed["args"][-3:] == ["exec-out", "screencap", "-p"]


def test_snapshot_console_failure_recovers_only_when_device_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller_without_adb()
    observed_timeouts: list[float | None] = []
    monkeypatch.setattr(
        controller_module,
        "execute_adb",
        lambda *_args, **kwargs: observed_timeouts.append(kwargs.get("timeout_seconds"))
        or AdbResponse(success=False, error="console failure"),
    )
    controller.check_health = lambda **_kwargs: True

    assert controller.load_snapshot("missing_snapshot") is False
    assert observed_timeouts == [15]

    controller.check_health = lambda **_kwargs: False
    with pytest.raises(DeviceUnhealthyError, match="Device is not healthy"):
        controller.load_snapshot("init_state")


def test_normal_screenshot_falls_back_after_empty_rc_zero_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller_without_adb()
    controller.screenshot_dir = "/sdcard"
    controller.backslash = "\\"
    local_path = tmp_path / "screen.png"
    commands: list[str] = []

    def execute(command: str, **_kwargs: Any) -> AdbResponse:
        commands.append(command)
        if "exec-out screencap" in command:
            local_path.write_bytes(b"")
        elif " pull " in command:
            local_path.write_bytes(_png_bytes())
        return AdbResponse(success=True, output="OK", command=command)

    monkeypatch.setattr(controller_module, "execute_adb", execute)

    result = controller.get_screenshot("screen", str(tmp_path))

    assert result.success is True
    assert result.output == str(local_path)
    assert any("shell screencap" in command for command in commands)
    assert any(" pull " in command for command in commands)


def test_normal_screenshot_rejects_invalid_fallback_png(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = _controller_without_adb()
    controller.screenshot_dir = "/sdcard"
    controller.backslash = "\\"
    local_path = tmp_path / "screen.png"

    def execute(command: str, **_kwargs: Any) -> AdbResponse:
        if "exec-out screencap" in command or " pull " in command:
            local_path.write_bytes(b"not-a-png")
        return AdbResponse(success=True, output="OK", command=command)

    monkeypatch.setattr(controller_module, "execute_adb", execute)

    result = controller.get_screenshot("screen", str(tmp_path))

    assert result.success is False
    assert "invalid PNG" in result.error
    assert not local_path.exists()


def test_adb_health_probe_timeout_is_a_factual_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimeoutProcess:
        pid = 12345
        returncode = -1
        calls = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(cmd="adb fixture", timeout=timeout)
            return "", ""

    process = TimeoutProcess()
    monkeypatch.setattr(helpers_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(helpers_module.os, "killpg", lambda *_args: None)

    result = helpers_module.execute_adb("adb devices", timeout_seconds=0.25)

    assert result.success is False
    assert result.return_code == -1
    assert result.error == "ADB command timed out after 0.25s"


def test_global_adb_timeout_bounds_root_setup_and_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    observed_timeouts: list[float] = []
    responses = iter(["shell", "restarting", "root", "done"])

    class SuccessfulProcess:
        pid = 12345
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            assert timeout is not None
            observed_timeouts.append(timeout)
            clock.now += 10
            return next(responses), ""

    def popen(*_args: Any, **kwargs: Any) -> SuccessfulProcess:
        assert kwargs["start_new_session"] is True
        return SuccessfulProcess()

    monkeypatch.setattr(helpers_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(helpers_module.subprocess, "Popen", popen)

    result = helpers_module.execute_adb("adb fixture", root_required=True)

    assert result.success is True
    assert observed_timeouts == [60, 50, 40, 30]


def test_stability_rejects_probe_that_returns_after_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    controller = _controller_without_adb()
    monkeypatch.setattr(controller_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(controller_module.time, "sleep", clock.sleep)

    def slow_health(*, try_times: int = 0, timeout_seconds: float | None = None) -> bool:
        assert try_times == 0
        assert timeout_seconds == 3
        clock.now = 6
        return True

    controller.check_health = slow_health

    with pytest.raises(DeviceUnhealthyError, match="Device is not healthy"):
        controller.wait_for_device_stability(
            stable_for_seconds=0,
            timeout_seconds=5,
            poll_interval_seconds=1,
        )


def test_task_init_client_budget_outlives_bounded_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "emulator-fixture"
    client._current_task_type = None
    client._ensure_initialized = lambda: None
    client.get_screenshot = lambda *, wait_to_stabilize: object()
    observed: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        observed.update(url=url, **kwargs)
        return Response()

    client._session = type("Session", (), {"post": staticmethod(post)})()
    client._request_deadline_monotonic_ns = None

    observation = client.initialize_task("FixtureTask")

    assert observation.screenshot is not None
    assert observed["timeout"] == TASK_INITIALIZATION_TIMEOUT_SECONDS
    assert TASK_INITIALIZATION_TIMEOUT_SECONDS == 600


def test_task_init_client_sends_complete_frozen_episode_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "emulator-fixture"
    client._current_task_type = None
    client._ensure_initialized = lambda: None
    client.get_screenshot = lambda *, wait_to_stabilize: object()
    observed: dict[str, Any] = {}
    parameters_sha256 = hashlib.sha256(b'{"task_name":"FixtureTask","trial":1}').hexdigest()

    class Response:
        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        observed.update(url=url, **kwargs)
        return Response()

    client._session = type("Session", (), {"post": staticmethod(post)})()
    client._request_deadline_monotonic_ns = None

    client.initialize_task(
        "FixtureTask",
        task_trial=1,
        task_parameters_sha256=parameters_sha256,
        reset_seed=73021,
    )

    assert observed["json"] == {
        "req_device": "emulator-fixture",
        "reset_seed": 73021,
        "task_name": "FixtureTask",
        "task_parameters_sha256": parameters_sha256,
        "task_trial": 1,
    }


def test_task_init_client_rejects_partial_frozen_episode_binding() -> None:
    client = object.__new__(AndroidEnvClient)
    client.device = "emulator-fixture"
    client._ensure_initialized = lambda: None

    with pytest.raises(RuntimeError, match="binding is incomplete"):
        client.initialize_task("FixtureTask", task_trial=1)


class _JsonResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_action_client_rejects_mismatched_success_echo_before_screenshot() -> None:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "emulator-fixture"
    client._ensure_initialized = lambda: None
    client._request_deadline_monotonic_ns = None
    screenshot_calls = 0

    def get_screenshot(*, wait_to_stabilize: bool) -> object:
        nonlocal screenshot_calls
        screenshot_calls += 1
        return object()

    client.get_screenshot = get_screenshot
    action = JSONAction(action_type="click", x=12, y=34)
    mismatched = action.model_dump(mode="json")
    mismatched["x"] = 99
    client._session = type(
        "Session",
        (),
        {
            "post": staticmethod(
                lambda *_args, **_kwargs: _JsonResponse(
                    {
                        "action": mismatched,
                        "device": "emulator-fixture",
                        "result": "OK",
                    }
                )
            )
        },
    )()

    with pytest.raises(RuntimeError, match="mismatched success envelope"):
        client.execute_action(action)

    assert screenshot_calls == 0


def test_task_score_client_requires_exact_bound_envelope() -> None:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "emulator-fixture"
    client._ensure_initialized = lambda: None
    client._request_deadline_monotonic_ns = None
    client._session = type(
        "Session",
        (),
        {
            "get": staticmethod(
                lambda *_args, **_kwargs: _JsonResponse(
                    {
                        "device": "emulator-fixture",
                        "reason": "fixture evidence",
                        "score": 0.75,
                        "task_name": "FixtureTask",
                    }
                )
            )
        },
    )()

    assert client.get_task_score("FixtureTask") == (0.75, "fixture evidence")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "device": "emulator-fixture",
            "reason": "fixture evidence",
            "score": True,
            "task_name": "FixtureTask",
        },
        {
            "device": "emulator-other",
            "reason": "fixture evidence",
            "score": 1.0,
            "task_name": "FixtureTask",
        },
        {
            "device": "emulator-fixture",
            "reason": "fixture evidence",
            "score": 1.0,
            "task_name": "DifferentTask",
        },
        {
            "device": "emulator-fixture",
            "reason": "",
            "score": 1.0,
            "task_name": "FixtureTask",
        },
    ],
)
def test_task_score_client_rejects_coercible_or_unbound_envelope(payload: dict[str, Any]) -> None:
    client = object.__new__(AndroidEnvClient)
    client.base_url = "http://fixture.invalid"
    client.device = "emulator-fixture"
    client._ensure_initialized = lambda: None
    client._request_deadline_monotonic_ns = None
    client._session = type(
        "Session",
        (),
        {"get": staticmethod(lambda *_args, **_kwargs: _JsonResponse(payload))},
    )()

    with pytest.raises(RuntimeError, match="Failed to get task score"):
        client.get_task_score("FixtureTask")


class _SnapshotFailureTask(BaseTask):
    @property
    def app_names(self) -> set[str]:
        return set()

    @property
    def goal(self) -> str:
        return "fixture"


class _SnapshotFailureController:
    device = "emulator-fixture"

    def load_snapshot(self, _tag: str) -> bool:
        return False


def test_failed_reinitialization_clears_stale_initialized_state() -> None:
    task = _SnapshotFailureTask()
    task.initialized = True

    assert task.initialize_task(_SnapshotFailureController()) is False
    assert task.initialized is False


class _Registry:
    def __init__(self, task: Any) -> None:
        self.task = task

    def get_task(self, task_name: str) -> Any:
        assert task_name == self.task.name
        return self.task


class _HealthyController:
    viewport_size = (1080, 2400)

    def __init__(self, device: str = "emulator-fixture") -> None:
        self.device = device
        self.stability_checks = 0

    def check_health(self, try_times: int = 0) -> bool:
        return True

    def wait_for_device_stability(self) -> None:
        self.stability_checks += 1


class _Task:
    name = "FixtureTask"

    def __init__(self, initialize: Callable[[int, _HealthyController], bool | None]) -> None:
        self.initialized = False
        self.calls = 0
        self._initialize = initialize

    def initialize_task(self, controller: _HealthyController) -> bool | None:
        self.calls += 1
        return self._initialize(self.calls, controller)


class _ScoredTask(_Task):
    def __init__(self, score: Any) -> None:
        super().__init__(lambda _attempt, _controller: True)
        self._score = score

    def is_successful(self, _controller: _HealthyController) -> Any:
        return self._score


@pytest.fixture(autouse=True)
def _isolated_server_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "CONTROLLERS", {})
    monkeypatch.setattr(server, "RUNNING_TASK", None)
    monkeypatch.setattr(server, "_last_restart_success", None)
    monkeypatch.setattr(server, "_lifecycle_lock", threading.RLock())
    monkeypatch.setattr(server, "_lifecycle_state_lock", threading.Lock())
    monkeypatch.setattr(server, "_lifecycle_transition_started", None)
    monkeypatch.setattr(server, "_lifecycle_transition_name", None)
    yield


def _request() -> TaskOperationRequest:
    return TaskOperationRequest(task_name="FixtureTask", req_device="emulator-fixture")


def _response_json(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def test_server_rejects_empty_input_text_as_typed_client_error() -> None:
    controller = _HealthyController()
    server.CONTROLLERS["emulator-fixture"] = controller

    with pytest.raises(HTTPException) as raised:
        server.step(
            StepRequest(
                device="emulator-fixture",
                action=JSONAction(action_type="input_text", text=""),
            )
        )

    assert raised.value.status_code == 400
    assert "non-empty text" in raised.value.detail


def test_server_task_score_is_bound_to_running_task_and_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _ScoredTask((0.5, "fixture evidence"))
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    server.RUNNING_TASK = task

    response = server.eval_task(_request())

    assert _response_json(response) == {
        "device": "emulator-fixture",
        "reason": "fixture evidence",
        "score": 0.5,
        "task_name": "FixtureTask",
    }


@pytest.mark.parametrize(
    "score",
    [True, float("nan"), -0.1, 1.1, 10**1_000, (1.0, "")],
)
def test_server_rejects_invalid_task_score(score: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _ScoredTask(score)
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    server.RUNNING_TASK = task

    with pytest.raises(HTTPException) as raised:
        server.eval_task(_request())

    assert raised.value.status_code == 500


def test_server_rejects_explicit_false_even_if_task_sets_initialized_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        task.initialized = True
        return False

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-fixture",
    )

    with pytest.raises(HTTPException) as raised:
        server.init_task(_request())

    assert raised.value.status_code == 500
    assert "returned False" in raised.value.detail
    assert task.initialized is False
    assert server.RUNNING_TASK is None
    assert restarts == []


def test_snapshot_failure_recovers_once_inside_same_init_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_controller = _HealthyController()

    def initialize(attempt: int, controller: _HealthyController) -> bool:
        if attempt == 1:
            raise DeviceUnhealthyError("Device is not healthy: delayed snapshot disconnect")
        assert controller is not initial_controller
        task.initialized = True
        return True

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = initial_controller
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-recovered",
    )
    replacements: list[_HealthyController] = []

    def controller_factory(device: str) -> _HealthyController:
        replacement = _HealthyController(device)
        replacements.append(replacement)
        return replacement

    monkeypatch.setattr(server, "AndroidController", controller_factory)

    response = server.init_task(_request())

    assert response.status_code == 200
    assert task.calls == 2
    assert restarts == [server.AVD_MAPPING[server.SUITE_FAMILY]]
    assert replacements[0].stability_checks == 1
    assert server.CONTROLLERS["emulator-fixture"] is replacements[0]
    assert server.RUNNING_TASK is task
    assert server._last_restart_success is not None


def test_frozen_task_seed_is_replayed_on_same_request_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_controller = _HealthyController()
    observed_random_values: list[float] = []

    def initialize(attempt: int, controller: _HealthyController) -> bool:
        observed_random_values.append(random.random())
        if attempt == 1:
            raise DeviceUnhealthyError("Device is not healthy: fixture restart")
        assert controller is not initial_controller
        task.initialized = True
        return True

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = initial_controller
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda _avd: "emulator-recovered",
    )
    monkeypatch.setattr(server, "AndroidController", _HealthyController)
    parameters_sha256 = hashlib.sha256(b'{"task_name":"FixtureTask","trial":1}').hexdigest()

    response = server.init_task(
        TaskOperationRequest(
            task_name="FixtureTask",
            req_device="emulator-fixture",
            task_trial=1,
            task_parameters_sha256=parameters_sha256,
            reset_seed=73021,
        )
    )

    assert response.status_code == 200
    assert len(observed_random_values) == 2
    assert observed_random_values[0] == observed_random_values[1]


def test_frozen_task_init_rejects_parameter_hash_drift_before_device_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _Task(lambda _attempt, _controller: True)
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))

    with pytest.raises(HTTPException) as raised:
        server.init_task(
            TaskOperationRequest(
                task_name="FixtureTask",
                req_device="emulator-fixture",
                task_trial=1,
                task_parameters_sha256="0" * 64,
                reset_seed=73021,
            )
        )

    assert raised.value.status_code == 400
    assert task.calls == 0
    assert server.RUNNING_TASK is None


def test_failed_recovery_retry_returns_500_after_one_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        raise DeviceUnhealthyError("Device is not healthy: snapshot generation failed")

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-recovered",
    )
    monkeypatch.setattr(server, "AndroidController", _HealthyController)

    with pytest.raises(HTTPException) as raised:
        server.init_task(_request())

    assert raised.value.status_code == 500
    assert "Device is not healthy" in raised.value.detail
    assert task.calls == 2
    assert len(restarts) == 1
    assert task.initialized is False
    assert server.RUNNING_TASK is None


def test_recovery_does_not_publish_controller_with_invalid_viewport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        raise DeviceUnhealthyError("Device is not healthy: delayed snapshot disconnect")

    class InvalidViewportController(_HealthyController):
        viewport_size = (None, None)

    task = _Task(initialize)
    original = _HealthyController()
    server.CONTROLLERS["emulator-fixture"] = original
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-recovered",
    )
    monkeypatch.setattr(server, "AndroidController", InvalidViewportController)

    with pytest.raises(HTTPException) as raised:
        server.init_task(_request())

    assert raised.value.status_code == 500
    assert "Device is not healthy" in raised.value.detail
    assert task.calls == 1
    assert len(restarts) == 1
    assert server.CONTROLLERS["emulator-fixture"] is original
    assert server._last_restart_success is None


def test_health_during_task_transition_returns_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        entered.set()
        assert release.wait(timeout=2)
        task.initialized = True
        return True

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = _HealthyController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-fixture",
    )
    result: list[Any] = []
    worker = threading.Thread(target=lambda: result.append(server.init_task(_request())))
    worker.start()
    assert entered.wait(timeout=2)

    health_response = server.health()

    assert health_response.status_code == 200
    assert _response_json(health_response)["transition_in_progress"] is True
    assert restarts == []
    monkeypatch.setattr(server, "MAX_LIFECYCLE_TRANSITION_SECONDS", 0)
    overdue_response = server.health()
    assert overdue_response.status_code == 503
    assert _response_json(overdue_response)["transition_overdue"] is True
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result[0].status_code == 200


def test_device_operation_and_task_transition_use_one_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation_entered = threading.Event()
    release_operation = threading.Event()
    task_entered = threading.Event()
    init_attempted = threading.Event()

    class StateController(_HealthyController):
        viewport_size = (1080, 2400)

        def get_current_activity(self) -> str:
            operation_entered.set()
            assert release_operation.wait(timeout=2)
            return "fixture.activity"

        def get_current_app(self) -> str:
            return "fixture.app"

    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        task_entered.set()
        task.initialized = True
        return True

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = StateController()
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    state_result: list[Any] = []
    init_result: list[Any] = []
    state_worker = threading.Thread(
        target=lambda: state_result.append(server.get_state("emulator-fixture"))
    )

    def initialize_in_thread() -> None:
        init_attempted.set()
        init_result.append(server.init_task(_request()))

    init_worker = threading.Thread(target=initialize_in_thread)
    state_worker.start()
    assert operation_entered.wait(timeout=2)
    init_worker.start()
    assert init_attempted.wait(timeout=2)
    assert not task_entered.wait(timeout=0.05)

    release_operation.set()
    state_worker.join(timeout=2)
    init_worker.join(timeout=2)

    assert not state_worker.is_alive()
    assert not init_worker.is_alive()
    assert state_result[0]["current_activity"] == "fixture.activity"
    assert init_result[0].status_code == 200
    assert task_entered.is_set()


def test_concurrent_health_probes_perform_one_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyController(_HealthyController):
        def check_health(self, try_times: int = 0) -> bool:
            return False

    server.CONTROLLERS["emulator-fixture"] = UnhealthyController()
    restart_entered = threading.Event()
    release_restart = threading.Event()
    restart_count = 0

    def restart(_avd: str) -> str:
        nonlocal restart_count
        restart_count += 1
        restart_entered.set()
        assert release_restart.wait(timeout=2)
        return "emulator-recovered"

    monkeypatch.setattr(server, "restart_emulator_with_avd", restart)
    monkeypatch.setattr(server, "AndroidController", _HealthyController)
    first_result: list[Any] = []
    first = threading.Thread(target=lambda: first_result.append(server.health()))
    first.start()
    assert restart_entered.wait(timeout=2)

    concurrent = server.health()

    assert concurrent.status_code == 200
    assert _response_json(concurrent)["transition_in_progress"] is True
    assert restart_count == 1
    release_restart.set()
    first.join(timeout=2)
    assert not first.is_alive()
    assert first_result[0].status_code == 200
    assert restart_count == 1


def test_health_defers_recovery_until_the_next_task_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyController(_HealthyController):
        def check_health(self, try_times: int = 0) -> bool:
            return False

    def initialize(_attempt: int, _controller: _HealthyController) -> bool:
        task.initialized = True
        return True

    task = _Task(initialize)
    server.CONTROLLERS["emulator-fixture"] = UnhealthyController()
    server.RUNNING_TASK = task
    monkeypatch.setattr(server, "task_registry", _Registry(task))
    restarts: list[str] = []
    monkeypatch.setattr(
        server,
        "restart_emulator_with_avd",
        lambda avd: restarts.append(avd) or "emulator-recovered",
    )
    monkeypatch.setattr(server, "AndroidController", _HealthyController)

    health_response = server.health()

    assert health_response.status_code == 503
    health_payload = _response_json(health_response)
    assert health_payload["recovery_deferred_for_active_task"] is True
    assert health_payload["active_task"] == task.name
    assert restarts == []

    init_response = server.init_task(_request())

    assert init_response.status_code == 200
    assert len(restarts) == 1
    assert task.calls == 1
    assert server.RUNNING_TASK is task


def test_failed_restart_does_not_arm_health_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyController(_HealthyController):
        def check_health(self, try_times: int = 0) -> bool:
            return False

    server.CONTROLLERS["emulator-fixture"] = UnhealthyController()
    restart_count = 0

    def restart(_avd: str) -> str:
        nonlocal restart_count
        restart_count += 1
        if restart_count == 1:
            raise RuntimeError("first restart failed")
        return "emulator-recovered"

    monkeypatch.setattr(server, "restart_emulator_with_avd", restart)
    monkeypatch.setattr(server, "AndroidController", _HealthyController)

    first = server.health()
    second = server.health()

    assert first.status_code == 503
    assert second.status_code == 200
    assert restart_count == 2
    assert server._last_restart_success is not None
