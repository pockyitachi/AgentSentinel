"""Docker utility helpers for MobileWorld CLI.

This module centralizes common Docker operations (run, ps, inspect, exec, rm)
and a consistent command runner with improved error messaging.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger

_EMULATOR_SCRIPT_PATH = Path("/app/docker/start_emulator.sh")
_EMULATOR_STATE_DIR = Path("/run/mobileworld-emulator")
_EMULATOR_LOG_PATH = Path("/var/log/emulator.log")
_PROC_ROOT = Path("/proc")
_RESTART_DIAGNOSTIC_LIMIT = 6_000
_RESTART_LOG_TAIL_BYTES = 12_000
_RESTART_HARD_DEADLINE_SECONDS = 150
_RESTART_TERMINATION_GRACE_SECONDS = 5
_URI_USERINFO_PATTERN = re.compile(r"(?i)(https?://)[^/@\s]+@")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)\s*[:=]\s*[^\s]+"
)


def run_command(
    cmd: list[str],
    capture: bool = True,
    allowed_exit_codes: set[int] | None = None,
) -> subprocess.CompletedProcess:
    """Run a shell command, logging failures and exiting on error.

    For Docker commands, provide a clearer message when permission errors occur.

    Args:
        cmd: Command to run as list of strings
        capture: Whether to capture stdout/stderr
        allowed_exit_codes: Set of exit codes to treat as success (in addition to 0)
    """
    try:
        if capture:
            result = subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            result = subprocess.run(cmd, check=True)
        return result
    except subprocess.CalledProcessError as e:
        # Check if this exit code is allowed
        if allowed_exit_codes and e.returncode in allowed_exit_codes:
            logger.debug(
                "Command returned allowed exit code {}: {}",
                e.returncode,
                " ".join(cmd),
            )
            # Return a successful CompletedProcess with the actual exit code
            return subprocess.CompletedProcess(
                args=e.cmd,
                returncode=e.returncode,
                stdout=e.stdout,
                stderr=e.stderr,
            )

        stderr_text = e.stderr or ""
        logger.error("Command failed: {}", " ".join(cmd))
        logger.error("Exit code: {}", e.returncode)
        if stderr_text:
            logger.error("Error output: {}", stderr_text)
            if "permission denied" in stderr_text.lower() and "docker" in stderr_text.lower():
                _log_docker_permission_help()
        sys.exit(1)


def _log_docker_permission_help() -> None:
    logger.error("{}", "=" * 80)
    logger.error("Docker Permission Error Detected")
    logger.error("{}", "=" * 80)
    logger.error("Your user doesn't have permission to access the Docker daemon.")
    logger.error("To fix this issue, try one of the following:")
    logger.error("  1. Add your user to the docker group:")
    logger.error("     $ sudo usermod -aG docker $USER")
    logger.error("     $ newgrp docker")
    logger.error("  2. Run the command with sudo:")
    logger.error("     $ sudo mobile-world env list")
    logger.error("  3. Check Docker daemon is running:")
    logger.error("     $ sudo systemctl status docker")


def docker_ps(include_all: bool = False) -> list[dict[str, Any]]:
    """Return a list of containers from `docker ps` as dicts."""
    cmd = ["docker", "ps", "--format", "{{json .}}"]
    if include_all:
        cmd.insert(2, "-a")
    result = run_command(cmd)
    containers: list[dict[str, Any]] = []
    for line in (result.stdout or "").strip().split("\n"):
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("Skipping unparsable docker ps line: {}", line)
    return containers


def list_containers_by_image_substring(
    image_substring: str, *, include_all: bool = False
) -> list[dict[str, Any]]:
    """Filter `docker ps` by image substring (case-insensitive)."""
    substring = (image_substring or "").lower()
    return [
        c for c in docker_ps(include_all=include_all) if substring in (c.get("Image", "").lower())
    ]


def docker_inspect(container_name: str) -> dict[str, Any] | None:
    """Return `docker inspect` result for a container or None if missing."""
    result = run_command(["docker", "inspect", container_name])
    try:
        data = json.loads(result.stdout or "[]")
        return data[0] if data else None
    except json.JSONDecodeError:
        logger.error("Failed to parse docker inspect output for {}", container_name)
        return None


def docker_rm(container_name: str, *, force: bool = True, volumes: bool = False) -> None:
    """Remove a container by name."""
    cmd = ["docker", "rm"]
    if force:
        cmd.append("-f")
    if volumes:
        cmd.append("-v")
    cmd.append(container_name)
    run_command(cmd)


def build_run_command(
    *,
    name: str,
    image: str,
    port_mappings: Iterable[tuple[int, int]] | None = None,  # (host, container)
    env_vars: dict[str, str] | None = None,
    volumes: Iterable[tuple[str, str]] | None = None,  # (host, container)
    detach: bool = True,
    privileged: bool = True,
    remove: bool = True,
) -> list[str]:
    """Construct a `docker run` command list with common flags."""
    cmd: list[str] = [
        "docker",
        "run",
    ]
    if remove:
        cmd.append("--rm")
    if privileged:
        cmd.append("--privileged")
    cmd.extend(["--name", name])

    for host_port, container_port in port_mappings or []:
        cmd.extend(["-p", f"{host_port}:{container_port}"])

    for host_path, container_path in volumes or []:
        cmd.extend(["-v", f"{host_path}:{container_path}"])

    for key, value in (env_vars or {}).items():
        cmd.extend(["-e", f"{key}={value}"])

    if detach:
        cmd.append("-d")

    cmd.append(image)
    return cmd


def docker_exec_bash(
    container_name: str,
    bash_command: str,
    *,
    detach: bool = False,
    allowed_exit_codes: set[int] | None = None,
) -> None:
    """Execute a bash command in a container. Detach if requested.

    Args:
        container_name: Name of the container to exec into
        bash_command: Bash command to execute
        detach: Run in detached mode
        allowed_exit_codes: Set of exit codes to treat as success (in addition to 0)
    """
    base = ["docker", "exec"]
    if detach:
        base.append("-d")
    base.extend(
        [
            container_name,
            "/bin/bash",
            "-c",
            bash_command,
        ]
    )
    run_command(base, allowed_exit_codes=allowed_exit_codes)


def docker_exec_replace(container_name: str, command: str, *, interactive: bool = True) -> None:
    """Replace current process with `docker exec` into a container."""
    cmd = ["docker", "exec"]
    if interactive:
        cmd.append("-it")
    cmd.extend([container_name, "/bin/bash", "-c", command])
    # Replace current process to properly handle terminal I/O
    os.execvp("docker", cmd)


def discover_backends(
    image_filter: str = "mobile_world", prefix: str = "mobile_world_env"
) -> tuple[list[str], list[str]]:
    """Discover backend URLs from running containers.

    Args:
        image_filter: Image name substring to filter containers (default: mobile_world)

    Returns:
        list[str]: List of backend URLs in format http://localhost:PORT
    """
    containers = list_containers_by_image_substring(image_filter, include_all=False)

    if not containers:
        logger.warning("No running containers found with image filter: {}", image_filter)
        return [], []

    backend_urls = []
    container_names = []
    for container in containers:
        container_name = container.get("Names", "")
        if not container_name or not container_name.startswith(prefix):
            continue

        container_info = docker_inspect(container_name)
        if not container_info:
            continue

        ports = container_info.get("NetworkSettings", {}).get("Ports", {})
        for container_port, host_bindings in ports.items():
            if "6800/tcp" in container_port and host_bindings:
                host_port = host_bindings[0].get("HostPort", "")
                if host_port:
                    backend_url = f"http://localhost:{host_port}"
                    backend_urls.append(backend_url)
                    logger.info(
                        "Discovered backend: {} (container: {})", backend_url, container_name
                    )
                    break
        container_names.append(container_name)
    return backend_urls, container_names


def _emulator_state_path() -> Path:
    return _EMULATOR_STATE_DIR / "emulator.state.json"


def _load_emulator_state() -> dict[str, Any] | None:
    try:
        value = json.loads(_emulator_state_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _safe_bounded_diagnostic(*parts: str | bytes | None) -> str:
    text_parts: list[str] = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, bytes):
            text_parts.append(part.decode("utf-8", errors="replace"))
        else:
            text_parts.append(part)
    text = "\n".join(text_parts)
    text = _URI_USERINFO_PATTERN.sub(r"\1<redacted>@", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1=<redacted>", text)
    text = "".join(char if char in "\n\t" or char.isprintable() else "?" for char in text)
    if len(text) > _RESTART_DIAGNOSTIC_LIMIT:
        text = f"<truncated to final {_RESTART_DIAGNOSTIC_LIMIT} characters>\n{text[-_RESTART_DIAGNOSTIC_LIMIT:]}"
    return text.strip()


def _read_emulator_log_tail() -> str:
    try:
        with _EMULATOR_LOG_PATH.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            byte_length = log_file.tell()
            log_file.seek(max(0, byte_length - _RESTART_LOG_TAIL_BYTES))
            return _safe_bounded_diagnostic(log_file.read())
    except OSError:
        return ""


def _emulator_process_matches(pid: int, avd_name: str) -> bool:
    try:
        cmdline = (_PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    tokens = [
        token.decode("utf-8", errors="surrogateescape") for token in cmdline.split(b"\0") if token
    ]
    if not tokens:
        return False

    executable = Path(tokens[0]).name
    if executable != "emulator" and not executable.startswith(("emulator64-", "qemu-system-")):
        return False

    for index, token in enumerate(tokens):
        if token == "-avd" and index + 1 < len(tokens) and tokens[index + 1] == avd_name:
            return True
        if token == f"@{avd_name}":
            return True
        if token.endswith(f"/{avd_name}.avd") or f"/{avd_name}.avd/" in token:
            return True
    return False


def _run_adb(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["adb", *args],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _online_emulator_devices(output: str) -> list[str]:
    devices: list[str] = []
    for line in output.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[0].startswith("emulator-") and fields[1] == "device":
            devices.append(fields[0])
    return devices


def _verify_emulator_generation(
    *, expected_generation: str, avd_name: str, previous_pid: int | None
) -> tuple[str | None, str]:
    state = _load_emulator_state()
    if state is None:
        return None, "ready-state file is missing or invalid"
    if state.get("generation_id") != expected_generation:
        return None, "ready-state generation does not match this restart"
    if state.get("avd_name") != avd_name:
        return None, "ready-state AVD does not match the requested AVD"
    if state.get("ready") is not True:
        return None, "ready-state marker is not ready"
    if state.get("log_path") != str(_EMULATOR_LOG_PATH):
        return None, "ready-state log path is unexpected"

    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None, "ready-state emulator PID is invalid"
    if previous_pid is not None and pid == previous_pid:
        return None, "emulator process identity did not change"
    if not _emulator_process_matches(pid, avd_name):
        return None, "new emulator process is absent or has the wrong AVD"

    device_id = state.get("device_id")
    if not isinstance(device_id, str) or re.fullmatch(r"emulator-[0-9]+", device_id) is None:
        return None, "ready-state device ID is invalid"

    try:
        devices_result = _run_adb(["devices"])
    except (OSError, subprocess.SubprocessError):
        return None, "could not query ADB devices"
    if devices_result.returncode != 0:
        return None, "ADB devices query failed"
    online_devices = _online_emulator_devices(devices_result.stdout or "")
    if online_devices != [device_id]:
        return None, "the new emulator is not the unique online ADB emulator"

    try:
        boot_result = _run_adb(["-s", device_id, "shell", "getprop", "sys.boot_completed"])
    except (OSError, subprocess.SubprocessError):
        return None, "could not verify emulator boot completion"
    if boot_result.returncode != 0 or (boot_result.stdout or "").strip() != "1":
        return None, "the new emulator is not boot-complete"
    if not _emulator_process_matches(pid, avd_name):
        return None, "new emulator process exited during postcondition verification"
    return device_id, "verified"


def _bounded_restart_timeout(env: dict[str, str], key: str, default: int, maximum: int) -> str:
    try:
        value = int(env.get(key, str(default)))
    except ValueError:
        value = default
    return str(min(max(value, 0), maximum))


def _terminate_restart_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return process.communicate(timeout=_RESTART_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.communicate()


def _run_emulator_restart_script(env: dict[str, str]) -> tuple[int | None, str]:
    try:
        process = subprocess.Popen(
            ["/bin/bash", str(_EMULATOR_SCRIPT_PATH)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as error:
        return None, _safe_bounded_diagnostic(
            f"script execution error: {type(error).__name__}: {error}"
        )

    try:
        stdout, stderr = process.communicate(timeout=_RESTART_HARD_DEADLINE_SECONDS)
        return process.returncode, _safe_bounded_diagnostic(stdout, stderr)
    except subprocess.TimeoutExpired as error:
        stdout, stderr = _terminate_restart_process_group(process)
        return None, _safe_bounded_diagnostic(
            error.stdout,
            error.stderr,
            stdout,
            stderr,
            f"restart exceeded the {_RESTART_HARD_DEADLINE_SECONDS}s hard deadline; "
            "its process group was terminated",
        )


def restart_emulator_with_avd(avd_name: str) -> str:
    """Restart emulator with the specified AVD using existing script.

    This function calls the existing /app/docker/start_emulator.sh script

    Args:
        avd_name: Name of the AVD to start

    Returns:
        Device ID of the started emulator
    """

    logger.info("Restarting emulator with AVD: {}", avd_name)

    previous_state = _load_emulator_state() or {}
    previous_pid_value = previous_state.get("pid")
    previous_pid = (
        previous_pid_value
        if isinstance(previous_pid_value, int) and not isinstance(previous_pid_value, bool)
        else None
    )
    generation_id = uuid.uuid4().hex
    env = os.environ.copy()
    env["AVD_NAME"] = avd_name
    env["MOBILEWORLD_EMULATOR_GENERATION_ID"] = generation_id
    env["MOBILEWORLD_EMULATOR_STATE_DIR"] = str(_EMULATOR_STATE_DIR)
    env["MOBILEWORLD_EMULATOR_LOG_PATH"] = str(_EMULATOR_LOG_PATH)
    env["EMULATOR_SHUTDOWN_TIMEOUT"] = _bounded_restart_timeout(
        env, "EMULATOR_SHUTDOWN_TIMEOUT", 8, 8
    )
    env["EMULATOR_TERM_TIMEOUT"] = _bounded_restart_timeout(env, "EMULATOR_TERM_TIMEOUT", 3, 3)
    env["EMULATOR_KILL_TIMEOUT"] = _bounded_restart_timeout(env, "EMULATOR_KILL_TIMEOUT", 2, 2)
    env["EMULATOR_TIMEOUT"] = _bounded_restart_timeout(env, "EMULATOR_TIMEOUT", 110, 110)
    env["EMULATOR_ROOT_RECONNECT_TIMEOUT"] = _bounded_restart_timeout(
        env, "EMULATOR_ROOT_RECONNECT_TIMEOUT", 15, 15
    )
    env["EMULATOR_POLL_INTERVAL"] = _bounded_restart_timeout(env, "EMULATOR_POLL_INTERVAL", 2, 2)
    env["MOBILEWORLD_PROXY_STARTUP_WAIT"] = _bounded_restart_timeout(
        env, "MOBILEWORLD_PROXY_STARTUP_WAIT", 1, 1
    )

    logger.info(
        "Calling {} for emulator generation {}; generation log: {}",
        _EMULATOR_SCRIPT_PATH,
        generation_id,
        _EMULATOR_LOG_PATH,
    )
    script_returncode, script_diagnostic = _run_emulator_restart_script(env)

    device_id, verification = _verify_emulator_generation(
        expected_generation=generation_id,
        avd_name=avd_name,
        previous_pid=previous_pid,
    )
    if device_id is None:
        logger.error(
            "Emulator generation {} failed its verified postcondition: {}; script rc: {}; log: {}",
            generation_id,
            verification,
            script_returncode if script_returncode is not None else "unavailable",
            _EMULATOR_LOG_PATH,
        )
        log_tail = _read_emulator_log_tail()
        combined_diagnostic = _safe_bounded_diagnostic(script_diagnostic, log_tail)
        if combined_diagnostic:
            logger.error("Bounded emulator restart diagnostics:\n{}", combined_diagnostic)
        raise RuntimeError(
            f"Failed to start a verified new emulator generation; see {_EMULATOR_LOG_PATH}"
        )

    if script_returncode not in (None, 0):
        logger.warning(
            "Emulator restart script returned {}, but generation {} passed the independent "
            "process/ADB/boot postcondition",
            script_returncode,
            generation_id,
        )
    elif script_returncode is None:
        logger.warning(
            "Emulator restart script did not return normally, but generation {} passed the "
            "independent process/ADB/boot postcondition",
            generation_id,
        )
    if script_diagnostic:
        logger.debug("Bounded emulator restart script diagnostics:\n{}", script_diagnostic)
    logger.info(
        "Emulator generation {} verified successfully: {} (log: {})",
        generation_id,
        device_id,
        _EMULATOR_LOG_PATH,
    )
    return device_id


__all__ = [
    "run_command",
    "docker_ps",
    "list_containers_by_image_substring",
    "docker_inspect",
    "docker_rm",
    "build_run_command",
    "docker_exec_bash",
    "docker_exec_replace",
    "discover_backends",
    "restart_emulator_with_avd",
]
