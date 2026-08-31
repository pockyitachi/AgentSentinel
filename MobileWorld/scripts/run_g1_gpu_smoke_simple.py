#!/usr/bin/env python3
"""Run the 22-call G1 GPU smoke sequentially on GPU ordinal 4.

This intentionally small runner owns only the two server processes it creates.  It never
enumerates, signals, or otherwise manages GPU processes or unrelated host processes, and it never
executes a returned tool call.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import importlib
import inspect
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, TextIO

HOST = "127.0.0.1"
PORT = 18007
SERVER_PYTHON = "/shared/miniconda3/bin/python3.12"
SERVER_SITE_PACKAGES = "/shared/linqiang/MobileWorld/vllm_env/lib/python3.12/site-packages"
REQUEST_PATH = "/v1/chat/completions"
HEALTH_PATH = "/health"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 180.0
READINESS_TIMEOUT_SECONDS = 900.0
STOP_GRACE_SECONDS = 3.0
STOP_TIMEOUT_SECONDS = 30.0
FIXTURE_SCHEMA = "mobileworld.g1.gpu-smoke-simple-requests/v1"
FIXTURE_SHA256 = "fee3b47688f06be09f5f9f56abc64fc8dd82d5a0f571b258be31f572e676a195"
MOBILEWORLD_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
HOST_PARSER_MODULES = {
    "qwen3vl_8b": "mobile_world.agents.implementations.qwen3vl",
    "mai_ui_8b": "mobile_world.agents.implementations.mai_ui_agent",
}
HostParser = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    snapshot: str
    served_name: str
    log_name: str


MODELS = (
    ModelConfig(
        model_id="qwen3vl_8b",
        snapshot=(
            "/shared/huggingface/hub/models--Qwen--Qwen3-VL-8B-Instruct/"
            "snapshots/0c351dd01ed87e9c1b53cbc748cba10e6187ff3b"
        ),
        served_name="Qwen3-VL-8B-Instruct",
        log_name="qwen.server.log",
    ),
    ModelConfig(
        model_id="mai_ui_8b",
        snapshot=(
            "/shared/linqiang/hf_home/hub/models--Tongyi-MAI--MAI-UI-8B/"
            "snapshots/e00a0097abb9cc621cac5172d8c4809f0839c94e"
        ),
        served_name="MAI-UI-8B",
        log_name="mai.server.log",
    ),
)

EXPECTED_CALLS = (
    ("g14-qwen-s1729-r1", "qwen3vl_8b", "G1_4_CANARY", 1729, 1, None),
    ("g14-qwen-s1729-r2", "qwen3vl_8b", "G1_4_CANARY", 1729, 2, None),
    ("g14-qwen-s2718-r1", "qwen3vl_8b", "G1_4_CANARY", 2718, 1, None),
    ("g14-qwen-s2718-r2", "qwen3vl_8b", "G1_4_CANARY", 2718, 2, None),
    ("g14-qwen-s31415-r1", "qwen3vl_8b", "G1_4_CANARY", 31415, 1, None),
    ("g14-qwen-s31415-r2", "qwen3vl_8b", "G1_4_CANARY", 31415, 2, None),
    ("g15-qwen-original-s1729", "qwen3vl_8b", "G1_5_CODEC", 1729, None, "ORIGINAL"),
    ("g15-qwen-mask-s1729", "qwen3vl_8b", "G1_5_CODEC", 1729, None, "MASK"),
    (
        "g15-qwen-mask-correction-s1729",
        "qwen3vl_8b",
        "G1_5_CODEC",
        1729,
        None,
        "MASK_CORRECTION",
    ),
    (
        "g15-qwen-oracle-clean-s1729",
        "qwen3vl_8b",
        "G1_5_CODEC",
        1729,
        None,
        "ORACLE_CLEAN",
    ),
    (
        "g15-qwen-sham-benign-edit-s1729",
        "qwen3vl_8b",
        "G1_5_CODEC",
        1729,
        None,
        "SHAM_BENIGN_EDIT",
    ),
    ("g14-mai-s1729-r1", "mai_ui_8b", "G1_4_CANARY", 1729, 1, None),
    ("g14-mai-s1729-r2", "mai_ui_8b", "G1_4_CANARY", 1729, 2, None),
    ("g14-mai-s2718-r1", "mai_ui_8b", "G1_4_CANARY", 2718, 1, None),
    ("g14-mai-s2718-r2", "mai_ui_8b", "G1_4_CANARY", 2718, 2, None),
    ("g14-mai-s31415-r1", "mai_ui_8b", "G1_4_CANARY", 31415, 1, None),
    ("g14-mai-s31415-r2", "mai_ui_8b", "G1_4_CANARY", 31415, 2, None),
    ("g15-mai-original-s1729", "mai_ui_8b", "G1_5_CODEC", 1729, None, "ORIGINAL"),
    ("g15-mai-mask-s1729", "mai_ui_8b", "G1_5_CODEC", 1729, None, "MASK"),
    (
        "g15-mai-mask-correction-s1729",
        "mai_ui_8b",
        "G1_5_CODEC",
        1729,
        None,
        "MASK_CORRECTION",
    ),
    (
        "g15-mai-oracle-clean-s1729",
        "mai_ui_8b",
        "G1_5_CODEC",
        1729,
        None,
        "ORACLE_CLEAN",
    ),
    (
        "g15-mai-sham-benign-edit-s1729",
        "mai_ui_8b",
        "G1_5_CODEC",
        1729,
        None,
        "SHAM_BENIGN_EDIT",
    ),
)


class SmokeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _TerminationController:
    """Defer TERM/HUP to explicit safe points outside owned cleanup."""

    _SIGNALS = (signal.SIGTERM, signal.SIGHUP)

    def __init__(self) -> None:
        self._previous: dict[signal.Signals, Any] = {}
        self._installed: list[signal.Signals] = []
        self._cleanup_depth = 0
        self._requested: signal.Signals | None = None
        self._restoring = False

    def install(self) -> None:
        try:
            for signum in self._SIGNALS:
                self._previous[signum] = signal.getsignal(signum)
                self._installed.append(signum)
                signal.signal(signum, self._handle)
        except BaseException as exc:
            self.restore()
            if isinstance(exc, SmokeError):
                raise
            raise SmokeError(
                "SIGNAL_HANDLER_INSTALL_FAILED",
                "TERM/HUP handlers must be installed by the main thread",
            ) from exc

    def begin_cleanup(self) -> None:
        self._cleanup_depth += 1

    def end_cleanup(self) -> None:
        if self._cleanup_depth <= 0:
            raise SmokeError("SIGNAL_STATE_INVALID", "cleanup signal guard is unbalanced")
        self._cleanup_depth -= 1

    def raise_if_requested(self) -> None:
        if self._requested is not None and self._cleanup_depth == 0:
            raise SmokeError(
                "RUN_INTERRUPTED",
                f"received {self._requested.name}; stopping owned session",
            )

    def restore(self) -> None:
        self._restoring = True
        try:
            for signum in reversed(self._installed):
                signal.signal(signum, self._previous[signum])
            self._installed.clear()
        finally:
            self._restoring = False

    def _handle(self, signum: int, _frame: Any) -> None:
        caught = signal.Signals(signum)
        if self._restoring or self._requested is not None:
            return
        self._requested = caught


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    pgid: int
    sid: int
    starttime_ticks: int
    uid: int
    state: str


@dataclass
class OwnedServer:
    process: subprocess.Popen[bytes]
    identity: ProcessIdentity
    model: ModelConfig


class JsonlWriter:
    def __init__(self, handle: TextIO) -> None:
        self._handle = handle
        self._sequence = 0

    def write(self, event: str, **fields: Any) -> None:
        value = {"sequence": self._sequence, "event": event, **fields}
        self._sequence += 1
        self._handle.write(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self._handle.flush()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SmokeError("FIXTURE_INVALID", f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_calls(path: Path) -> list[dict[str, Any]]:
    try:
        raw_fixture = path.read_bytes()
        if hashlib.sha256(raw_fixture).hexdigest() != FIXTURE_SHA256:
            raise SmokeError("FIXTURE_INVALID", "request fixture bytes changed")
        value = json.loads(raw_fixture, object_pairs_hook=_reject_duplicate_keys)
    except SmokeError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SmokeError("FIXTURE_INVALID", "request fixture is unreadable JSON") from exc
    if type(value) is not dict or set(value) != {"schema_version", "calls"}:
        raise SmokeError("FIXTURE_INVALID", "request fixture has unexpected top-level fields")
    if value["schema_version"] != FIXTURE_SCHEMA or type(value["calls"]) is not list:
        raise SmokeError("FIXTURE_INVALID", "request fixture schema or call list is invalid")
    calls = value["calls"]
    if len(calls) != len(EXPECTED_CALLS):
        raise SmokeError("FIXTURE_INVALID", "request fixture must contain exactly 22 calls")
    call_keys = {
        "call_id",
        "model_id",
        "phase",
        "seed",
        "repeat",
        "arm",
        "application_request",
    }
    for index, (call, expected) in enumerate(zip(calls, EXPECTED_CALLS, strict=True)):
        if type(call) is not dict or set(call) != call_keys:
            raise SmokeError("FIXTURE_INVALID", f"call {index} has unexpected fields")
        metadata = tuple(
            call[key] for key in ("call_id", "model_id", "phase", "seed", "repeat", "arm")
        )
        if metadata != expected:
            raise SmokeError("FIXTURE_INVALID", f"call {index} metadata or order changed")
        request = call["application_request"]
        if type(request) is not dict or type(request.get("messages")) is not list:
            raise SmokeError("FIXTURE_INVALID", f"call {index} application request is invalid")
        expected_model = next(
            model.served_name for model in MODELS if model.model_id == call["model_id"]
        )
        if request.get("model") != expected_model or "seed" in request or "stream" in request:
            raise SmokeError("FIXTURE_INVALID", f"call {index} request model or controls changed")
    return calls


def _server_command(model: ModelConfig) -> list[str]:
    return [
        SERVER_PYTHON,
        "-P",
        "-B",
        "-m",
        "vllm.entrypoints.cli.main",
        "serve",
        model.snapshot,
        "--served-model-name",
        model.served_name,
        "--host",
        HOST,
        "--port",
        str(PORT),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "32768",
        "--enforce-eager",
        "--gpu-memory-utilization",
        "0.24",
        "--limit-mm-per-prompt",
        '{"image":3,"video":0}',
        "--mm-processor-cache-gb",
        "1",
        "--max-num-batched-tokens",
        "8192",
        "--max-num-seqs",
        "1",
        "--tensor-parallel-size",
        "1",
        "--pipeline-parallel-size",
        "1",
        "--data-parallel-size",
        "1",
        "--seed",
        "0",
        "--no-enable-prefix-caching",
        "--tokenizer-mode",
        "auto",
        "--no-trust-remote-code",
        "--load-format",
        "auto",
        "--kv-cache-dtype",
        "auto",
        "--generation-config",
        "auto",
    ]


def _validate_gpu_index(gpu_index: int) -> None:
    if type(gpu_index) is not int or gpu_index != 4:
        raise SmokeError("GPU_INDEX_INVALID", "this smoke attempt requires GPU index 4")


def _server_environment(gpu_index: int) -> dict[str, str]:
    _validate_gpu_index(gpu_index)
    environment = dict(os.environ)
    environment.update(
        {
            "CUDA_VISIBLE_DEVICES": str(gpu_index),
            "TRITON_PTXAS_PATH": "/usr/local/cuda/bin/ptxas",
            "TRITON_CUOBJDUMP_PATH": "/usr/local/cuda/bin/cuobjdump",
            "TRITON_NVDISASM_PATH": "/usr/local/cuda/bin/nvdisasm",
            "PYTHONPATH": SERVER_SITE_PACKAGES,
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return environment


def _assert_port_free() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((HOST, PORT))
    except OSError as exc:
        raise SmokeError(
            "PORT_BUSY", f"{HOST}:{PORT} is already in use; nothing was stopped"
        ) from exc
    finally:
        probe.close()


def _read_process_identity(pid: int) -> ProcessIdentity:
    try:
        proc = Path("/proc") / str(pid)
        metadata = proc.stat()
        stat_text = (proc / "stat").read_text(encoding="ascii")
        fields = stat_text.rsplit(")", 1)[1].strip().split()
        return ProcessIdentity(
            pid=pid,
            ppid=int(fields[1]),
            pgid=int(fields[2]),
            sid=int(fields[3]),
            starttime_ticks=int(fields[19]),
            uid=metadata.st_uid,
            state=fields[0],
        )
    except (OSError, IndexError, ValueError) as exc:
        raise SmokeError("OWN_PROCESS_IDENTITY_LOST", f"cannot identify owned pid {pid}") from exc


def _capture_owned_identity(process: subprocess.Popen[bytes]) -> ProcessIdentity:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if process.returncode is not None:
            raise SmokeError("SERVER_EXITED", "server exited before ownership capture")
        try:
            identity = _read_process_identity(process.pid)
        except SmokeError:
            time.sleep(0.01)
            continue
        if (
            identity.ppid == os.getpid()
            and identity.pgid == process.pid
            and identity.sid == process.pid
            and identity.uid == os.getuid()
        ):
            if identity.state == "Z":
                raise SmokeError("SERVER_EXITED", "server exited before ownership capture")
            return identity
        raise SmokeError("OWN_PROCESS_IDENTITY_MISMATCH", "server is not our direct new session")
    raise SmokeError("OWN_PROCESS_IDENTITY_LOST", "server ownership capture timed out")


def _start_server(
    model: ModelConfig,
    log_handle: BinaryIO,
    output_dir: Path,
    gpu_index: int,
    termination: _TerminationController | None = None,
) -> OwnedServer:
    _assert_port_free()
    try:
        process = subprocess.Popen(
            _server_command(model),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=output_dir,
            env=_server_environment(gpu_index),
            close_fds=True,
            start_new_session=True,
            shell=False,
        )
    except OSError as exc:
        raise SmokeError("SERVER_START_FAILED", f"could not start {model.model_id}") from exc
    try:
        if termination is not None:
            termination.raise_if_requested()
        identity = _capture_owned_identity(process)
        if termination is not None:
            termination.raise_if_requested()
    except BaseException as exc:
        try:
            if termination is not None:
                termination.begin_cleanup()
            try:
                _stop_provisional_session(process)
            finally:
                if termination is not None:
                    termination.end_cleanup()
        except BaseException as cleanup_exc:
            raise SmokeError(
                "SERVER_CAPTURE_CLEANUP_FAILED",
                f"ownership capture failed and the new session could not be cleaned: {cleanup_exc}",
            ) from exc
        raise
    return OwnedServer(process=process, identity=identity, model=model)


def _stable_identity_equal(left: ProcessIdentity, right: ProcessIdentity) -> bool:
    return (
        left.pid == right.pid
        and left.ppid == right.ppid
        and left.pgid == right.pgid
        and left.sid == right.sid
        and left.starttime_ticks == right.starttime_ticks
        and left.uid == right.uid
    )


def _identity_matches(server: OwnedServer) -> bool:
    try:
        current = _read_process_identity(server.identity.pid)
    except SmokeError:
        return False
    return (
        _stable_identity_equal(current, server.identity)
        and server.process.pid == server.identity.pid
    )


def _provisional_session_matches(process: subprocess.Popen[bytes]) -> bool:
    if process.returncode is not None or process.pid <= 0:
        return False
    try:
        return (
            os.getpgid(process.pid) == process.pid
            and os.getsid(process.pid) == process.pid
            and (Path("/proc") / str(process.pid)).stat().st_uid == os.getuid()
        )
    except OSError:
        return False


def _bounded_session_cleanup(
    process: subprocess.Popen[bytes],
    pgid: int,
    identity_matches: Callable[[], bool],
) -> str:
    if process.returncode is not None:
        return "already_reaped"
    if not identity_matches():
        raise SmokeError("OWN_PROCESS_IDENTITY_MISMATCH", "refusing to signal an unproven session")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise SmokeError("SERVER_STOP_FAILED", "owned server could not be reaped") from exc
        return "exited_before_term"

    # Do not poll/wait here: retaining the direct child unreaped prevents its PID/PGID from being
    # reused while the grace period and final owned-session sweep run.
    time.sleep(STOP_GRACE_SECONDS)
    if not identity_matches():
        raise SmokeError("OWN_PROCESS_IDENTITY_MISMATCH", "refusing KILL after identity drift")
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise SmokeError("SERVER_STOP_FAILED", "owned server did not exit after KILL") from exc
    return "session_terminated"


def _stop_provisional_session(process: subprocess.Popen[bytes]) -> str:
    """Clean only the direct session just returned by Popen(start_new_session=True)."""

    return _bounded_session_cleanup(
        process,
        process.pid,
        lambda: _provisional_session_matches(process),
    )


def _stop_server(server: OwnedServer) -> str:
    return _bounded_session_cleanup(
        server.process,
        server.identity.pgid,
        lambda: _identity_matches(server),
    )


def _health_request() -> int:
    connection = http.client.HTTPConnection(HOST, PORT, timeout=2.0)
    try:
        connection.request("GET", HEALTH_PATH)
        response = connection.getresponse()
        response.read(MAX_RESPONSE_BYTES + 1)
        return response.status
    finally:
        connection.close()


def _wait_for_ready(server: OwnedServer, termination: _TerminationController | None = None) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if termination is not None:
            termination.raise_if_requested()
        if server.process.returncode is not None:
            raise SmokeError(
                "SERVER_EXITED", f"{server.model.model_id} exited with {server.process.returncode}"
            )
        try:
            current = _read_process_identity(server.identity.pid)
        except SmokeError as exc:
            raise SmokeError(
                "SERVER_EXITED", f"{server.model.model_id} identity disappeared"
            ) from exc
        if not _stable_identity_equal(current, server.identity):
            raise SmokeError(
                "OWN_PROCESS_IDENTITY_MISMATCH", "server identity changed before ready"
            )
        if current.state == "Z":
            raise SmokeError("SERVER_EXITED", f"{server.model.model_id} exited before ready")
        try:
            status = _health_request()
        except (OSError, http.client.HTTPException):
            if termination is not None:
                termination.raise_if_requested()
            time.sleep(0.25)
            continue
        if termination is not None:
            termination.raise_if_requested()
        if status != 200:
            raise SmokeError("SERVER_HEALTH_FAILED", f"health endpoint returned HTTP {status}")
        return
    raise SmokeError("SERVER_READY_TIMEOUT", f"{server.model.model_id} did not become ready")


def _post_json(request: dict[str, Any]) -> tuple[int, bytes]:
    body = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    connection = http.client.HTTPConnection(HOST, PORT, timeout=REQUEST_TIMEOUT_SECONDS)
    try:
        connection.request(
            "POST",
            REQUEST_PATH,
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        response = connection.getresponse()
        payload = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise SmokeError("RESPONSE_TOO_LARGE", "response exceeded the byte limit")
        return response.status, payload
    except (OSError, http.client.HTTPException) as exc:
        raise SmokeError("HTTP_FAILED", "single HTTP request failed; it was not retried") from exc
    finally:
        connection.close()


def _load_host_parsers() -> dict[str, HostParser]:
    """Load the existing host parsers from this exact worktree, never an editable checkout."""

    source_root = MOBILEWORLD_SOURCE_ROOT.resolve()
    source_root_text = str(source_root)
    for name, module in tuple(sys.modules.items()):
        if name != "mobile_world" and not name.startswith("mobile_world."):
            continue
        module_file = getattr(module, "__file__", None)
        module_paths = tuple(getattr(module, "__path__", ()))
        candidates = ([module_file] if module_file is not None else []) + list(module_paths)
        if not candidates or any(
            not Path(candidate).resolve().is_relative_to(source_root) for candidate in candidates
        ):
            raise SmokeError(
                "HOST_PARSER_IMPORT_FAILED",
                "a MobileWorld module was already loaded from outside this worktree",
            )
    if source_root_text in sys.path:
        sys.path.remove(source_root_text)
    sys.path.insert(0, source_root_text)

    parsers: dict[str, HostParser] = {}
    try:
        for model_id, module_name in HOST_PARSER_MODULES.items():
            module = importlib.import_module(module_name)
            parser = getattr(module, "parse_action_to_structure_output")
            if not callable(parser):
                raise TypeError(f"{module_name}.parse_action_to_structure_output is not callable")
            module_path = inspect.getsourcefile(parser)
            expected_path = source_root / Path(*module_name.split(".")).with_suffix(".py")
            if module_path is None or Path(module_path).resolve() != expected_path:
                raise RuntimeError(f"{module_name} resolved outside the runner worktree")
            parsers[model_id] = parser
    except Exception as exc:
        raise SmokeError(
            "HOST_PARSER_IMPORT_FAILED",
            "the existing MobileWorld host parsers could not be bound from this worktree",
        ) from exc
    return parsers


def _validate_json_value(value: Any, *, path: str = "$") -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise SmokeError("HOST_PARSE_FAILED", f"host parser output {path} is not finite JSON")
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise SmokeError(
                    "HOST_PARSE_FAILED", f"host parser output {path} has a non-string key"
                )
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise SmokeError("HOST_PARSE_FAILED", f"host parser output {path} is not strict JSON")


def _parse_host_response(
    model_id: str,
    content: str,
    host_parsers: dict[str, HostParser],
) -> dict[str, Any]:
    parser = host_parsers.get(model_id)
    if parser is None:
        raise SmokeError("HOST_PARSE_FAILED", "unknown model parser")
    try:
        parsed = parser(content)
    except Exception as exc:
        raise SmokeError(
            "HOST_PARSE_FAILED", "the existing host parser rejected the response"
        ) from exc
    if type(parsed) is not dict:
        raise SmokeError("HOST_PARSE_FAILED", "the existing host parser returned a non-object")
    _validate_json_value(parsed)
    return parsed


def _validate_response(
    model_id: str,
    status: int,
    payload: bytes,
    host_parsers: dict[str, HostParser],
) -> tuple[str, dict[str, Any]]:
    if status != 200:
        raise SmokeError(
            "HTTP_STATUS_FAILED", f"request returned HTTP {status}; it was not retried"
        )
    try:
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_keys)
        choices = value["choices"]
        content = choices[0]["message"]["content"]
    except (
        KeyError,
        IndexError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        SmokeError,
    ) as exc:
        raise SmokeError("RESPONSE_INVALID", "response is not a single usable choice") from exc
    if type(content) is not str or not content.strip():
        raise SmokeError("RESPONSE_EMPTY", "choice content is empty")
    return content, _parse_host_response(model_id, content, host_parsers)


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def run(requests_path: Path, output_dir: Path, gpu_index: int) -> None:
    _validate_gpu_index(gpu_index)
    calls = _load_calls(requests_path)
    # Import and bind the existing host parsers before any model process is started.
    host_parsers = _load_host_parsers()
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_path = output_dir / "run.jsonl"
    with run_path.open("x", encoding="utf-8") as run_handle:
        writer = JsonlWriter(run_handle)
        writer.write(
            "run_started",
            call_count=len(calls),
            gpu_index=gpu_index,
            host=HOST,
            port=PORT,
            runner_python=sys.executable,
        )
        termination = _TerminationController()
        termination.install()
        current_call_id: str | None = None
        try:
            for model in MODELS:
                termination.raise_if_requested()
                model_calls = [call for call in calls if call["model_id"] == model.model_id]
                log_path = output_dir / model.log_name
                with log_path.open("xb", buffering=0) as log_handle:
                    server: OwnedServer | None = None
                    try:
                        server = _start_server(
                            model, log_handle, output_dir, gpu_index, termination
                        )
                        termination.raise_if_requested()
                        writer.write(
                            "server_started", model_id=model.model_id, pid=server.identity.pid
                        )
                        _wait_for_ready(server, termination)
                        termination.raise_if_requested()
                        writer.write("server_ready", model_id=model.model_id)
                        for call in model_calls:
                            termination.raise_if_requested()
                            current_call_id = call["call_id"]
                            request = dict(call["application_request"])
                            request["seed"] = call["seed"]
                            termination.raise_if_requested()
                            status, payload = _post_json(request)
                            response_sha256 = hashlib.sha256(payload).hexdigest()
                            writer.write(
                                "response_received",
                                call_id=call["call_id"],
                                model_id=model.model_id,
                                http_status=status,
                                response_byte_count=len(payload),
                                response_sha256=response_sha256,
                                response_body_base64=base64.b64encode(payload).decode("ascii"),
                            )
                            termination.raise_if_requested()
                            content, host_parser_output = _validate_response(
                                model.model_id,
                                status,
                                payload,
                                host_parsers,
                            )
                            writer.write(
                                "call_succeeded",
                                call_id=call["call_id"],
                                model_id=model.model_id,
                                phase=call["phase"],
                                seed=call["seed"],
                                repeat=call["repeat"],
                                arm=call["arm"],
                                http_status=status,
                                request_sha256=_canonical_sha256(request),
                                response_sha256=response_sha256,
                                content=content,
                                host_parser_output=host_parser_output,
                                generated_action_executed=False,
                            )
                            current_call_id = None
                    finally:
                        if server is not None:
                            termination.begin_cleanup()
                            try:
                                stop_result = _stop_server(server)
                            finally:
                                termination.end_cleanup()
                            writer.write(
                                "server_stopped",
                                model_id=model.model_id,
                                result=stop_result,
                            )
                    termination.raise_if_requested()
            writer.write("run_completed", successful_call_count=len(calls))
        except BaseException as exc:
            writer.write(
                "run_failed",
                call_id=current_call_id,
                error_code=exc.code if isinstance(exc, SmokeError) else type(exc).__name__,
                error=str(exc),
            )
            raise
        finally:
            termination.restore()


def _default_fixture_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "tests/offline/fixtures/g1_gpu_smoke_simple/requests.v1.json"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--gpu-index", required=True, type=int, choices=(4,))
    parser.add_argument("--requests", type=Path, default=_default_fixture_path())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run(args.requests, args.output_dir, args.gpu_index)
    except BaseException as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
