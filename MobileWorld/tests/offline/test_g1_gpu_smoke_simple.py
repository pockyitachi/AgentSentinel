from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import inspect
import io
import json
import signal
import sys
import types
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "run_g1_gpu_smoke_simple.py"
REQUESTS = Path(__file__).parent / "fixtures/g1_gpu_smoke_simple/requests.v1.json"
SPEC = importlib.util.spec_from_file_location("run_g1_gpu_smoke_simple_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        self.returncode = 0
        return 0


def _identity(pid: int, *, state: str = "S") -> Any:
    return smoke.ProcessIdentity(
        pid=pid,
        ppid=111,
        pgid=pid,
        sid=pid,
        starttime_ticks=123456,
        uid=1000,
        state=state,
    )


def _response(model_id: str) -> bytes:
    tool_call = '{"name":"mobile_use","arguments":{"action":"wait"}}'
    if model_id == "qwen3vl_8b":
        content = f"Thought: inspect only\nAction: <tool_call>{tool_call}</tool_call>"
    else:
        content = f"<thinking>inspect only</thinking><tool_call>{tool_call}</tool_call>"
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode()


def test_request_fixture_is_exact_closed_22_call_schedule() -> None:
    assert hashlib.sha256(REQUESTS.read_bytes()).hexdigest() == smoke.FIXTURE_SHA256
    value = json.loads(REQUESTS.read_bytes())
    assert set(value) == {"schema_version", "calls"}
    assert value["schema_version"] == "mobileworld.g1.gpu-smoke-simple-requests/v1"
    calls = smoke._load_calls(REQUESTS)
    assert len(calls) == 22
    assert (
        tuple(
            tuple(call[key] for key in ("call_id", "model_id", "phase", "seed", "repeat", "arm"))
            for call in calls
        )
        == smoke.EXPECTED_CALLS
    )
    assert [call["model_id"] for call in calls] == ["qwen3vl_8b"] * 11 + ["mai_ui_8b"] * 11
    expected_keys = {
        "call_id",
        "model_id",
        "phase",
        "seed",
        "repeat",
        "arm",
        "application_request",
    }
    assert all(set(call) == expected_keys for call in calls)
    assert all("seed" not in call["application_request"] for call in calls)
    assert all("stream" not in call["application_request"] for call in calls)


def test_fixture_uses_existing_production_prompts_and_only_history_varies() -> None:
    smoke._load_host_parsers()
    prompts = importlib.import_module("mobile_world.agents.utils.prompts")
    expected_qwen_system = prompts.MOBILE_QWEN3VL_PROMPT_WITH_ASK_USER.render(tools="")
    expected_mai_system = prompts.MAI_MOBILE_SYS_PROMPT_ASK_USER_MCP.render(tools=None)
    calls = smoke._load_calls(REQUESTS)
    qwen_calls = [call for call in calls if call["model_id"] == "qwen3vl_8b"]
    mai_calls = [call for call in calls if call["model_id"] == "mai_ui_8b"]
    qwen_requests = [call["application_request"] for call in qwen_calls]
    mai_requests = [call["application_request"] for call in mai_calls]

    qwen_systems = [request["messages"][0]["content"][0]["text"] for request in qwen_requests]
    assert qwen_systems == [expected_qwen_system] * 11
    assert all('"arguments": <args-json-object>' in system for system in qwen_systems)
    qwen_base = qwen_requests[0]
    qwen_marker = "Task progress (You have done the following operation on the current device): "
    qwen_prefixes: list[str] = []
    for call, request in zip(qwen_calls, qwen_requests, strict=True):
        assert {key: request[key] for key in request if key != "messages"} == {
            key: qwen_base[key] for key in qwen_base if key != "messages"
        }
        assert request["messages"][0] == qwen_base["messages"][0]
        assert request["messages"][1]["role"] == qwen_base["messages"][1]["role"]
        assert request["messages"][1]["content"][-1] == qwen_base["messages"][1]["content"][-1]
        correction_blocks = request["messages"][1]["content"][1:-1]
        if call["arm"] == "MASK_CORRECTION":
            assert correction_blocks == [
                {
                    "type": "text",
                    "text": "SENTINEL correction: 设置页面尚未确认亮度已调整。",
                }
            ]
        else:
            assert correction_blocks == []
        text = request["messages"][1]["content"][0]["text"]
        prefix, _history = text.split(qwen_marker, 1)
        qwen_prefixes.append(prefix)
        assert text.endswith("\n")
    assert qwen_prefixes == [qwen_prefixes[0]] * 11

    mai_systems = [request["messages"][0]["content"] for request in mai_requests]
    assert mai_systems == [expected_mai_system] * 11
    assert all('"arguments": <args-json-object>' in system for system in mai_systems)
    mai_base = mai_requests[0]
    for call, request in zip(mai_calls, mai_requests, strict=True):
        assert {key: request[key] for key in request if key != "messages"} == {
            key: mai_base[key] for key in mai_base if key != "messages"
        }
        assert request["messages"][0] == mai_base["messages"][0]
        assert request["messages"][1] == mai_base["messages"][1]
        assert [message["role"] for message in request["messages"]] == [
            message["role"] for message in mai_base["messages"]
        ]
        assert request["messages"][-1]["content"][-1] == mai_base["messages"][-1]["content"][-1]
        correction_blocks = request["messages"][-1]["content"][:-1]
        if call["arm"] == "MASK_CORRECTION":
            assert correction_blocks == [
                {
                    "type": "text",
                    "text": "SENTINEL correction: 当前界面未证明显示选项已经生效。",
                }
            ]
        else:
            assert correction_blocks == []

    reconstructed_old = deepcopy({"schema_version": smoke.FIXTURE_SCHEMA, "calls": calls})
    old_qwen_system = (
        '# Tools\n<tools>\n{"name":"mobile_use"}\n</tools>\n'
        "Return Thought, Action, and one <tool_call>."
    )
    old_mai_system = "You are a GUI agent. Return <thinking> and one <tool_call> wrapper."
    for call in reconstructed_old["calls"]:
        if call["model_id"] == "qwen3vl_8b":
            call["application_request"]["messages"][0]["content"][0]["text"] = old_qwen_system
        else:
            call["application_request"]["messages"][0]["content"] = old_mai_system
    old_bytes = (
        json.dumps(
            reconstructed_old,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    assert hashlib.sha256(old_bytes).hexdigest() == (
        "98e202d3b09d9480ecede41681ada51d3fda5d92ae8baea87cda9b2db6d6605e"
    )


def test_request_fixture_rejects_byte_drift(tmp_path: Path) -> None:
    changed = tmp_path / "requests.v1.json"
    changed.write_bytes(REQUESTS.read_bytes() + b"\n")

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._load_calls(changed)

    assert caught.value.code == "FIXTURE_INVALID"


def test_runner_has_no_nvidia_or_gpu_process_census_surface() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in (
        "nvidia-smi",
        "pynvml",
        "nvmlinit",
        "/proc/driver/nvidia",
        "gpu process census",
        "chmod",
    ):
        assert forbidden not in lowered


def test_start_server_uses_explicit_gpu_four_without_mutating_parent_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}
    process = FakeProcess(41001)

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(smoke, "_assert_port_free", lambda: None)
    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        smoke, "_capture_owned_identity", lambda candidate: _identity(candidate.pid)
    )

    child_only_environment = {
        "CUDA_VISIBLE_DEVICES": "4",
        "TRITON_PTXAS_PATH": "/usr/local/cuda/bin/ptxas",
        "TRITON_CUOBJDUMP_PATH": "/usr/local/cuda/bin/cuobjdump",
        "TRITON_NVDISASM_PATH": "/usr/local/cuda/bin/nvdisasm",
    }
    for key in child_only_environment:
        monkeypatch.setenv(key, f"parent-{key}")
    server = smoke._start_server(smoke.MODELS[0], io.BytesIO(), tmp_path, 4)

    assert server.process is process
    assert server.identity == _identity(process.pid)
    assert captured["command"] == smoke._server_command(smoke.MODELS[0])
    assert "--swap-space" not in captured["command"]
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["cwd"] == tmp_path
    assert {key: kwargs["env"][key] for key in child_only_environment} == child_only_environment
    assert {key: smoke.os.environ[key] for key in child_only_environment} == {
        key: f"parent-{key}" for key in child_only_environment
    }


def test_gpu_index_is_required_and_only_four_is_accepted() -> None:
    with pytest.raises(SystemExit):
        smoke._parser().parse_args(["--output-dir", "/tmp/out"])
    with pytest.raises(SystemExit):
        smoke._parser().parse_args(["--output-dir", "/tmp/out", "--gpu-index", "0"])

    args = smoke._parser().parse_args(["--output-dir", "/tmp/out", "--gpu-index", "4"])
    assert args.gpu_index == 4

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._server_environment(0)
    assert caught.value.code == "GPU_INDEX_INVALID"


def test_term_flagged_as_popen_returns_cleans_before_ownership_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(41201)
    termination = smoke._TerminationController()
    signals: list[tuple[int, signal.Signals]] = []
    capture_calls: list[int] = []

    def fake_popen(*_args: Any, **_kwargs: Any) -> FakeProcess:
        termination._handle(signal.SIGTERM, None)
        return process

    def forbidden_capture(candidate: FakeProcess) -> None:
        capture_calls.append(candidate.pid)
        pytest.fail("termination must be observed before ownership capture")

    monkeypatch.setattr(smoke, "_assert_port_free", lambda: None)
    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke, "_capture_owned_identity", forbidden_capture)
    monkeypatch.setattr(
        smoke, "_provisional_session_matches", lambda candidate: candidate is process
    )
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: signals.append((pgid, signum)))
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._start_server(smoke.MODELS[0], io.BytesIO(), tmp_path, 4, termination)

    assert caught.value.code == "RUN_INTERRUPTED"
    assert capture_calls == []
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert process.wait_timeouts == [smoke.STOP_TIMEOUT_SECONDS]


def test_capture_failure_cleanup_defers_term_until_provisional_session_is_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(41501)
    termination = smoke._TerminationController()
    signals: list[tuple[int, signal.Signals]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(smoke, "_assert_port_free", lambda: None)
    monkeypatch.setattr(smoke.subprocess, "Popen", lambda *_args, **_kwargs: process)

    def fail_capture(_process: FakeProcess) -> None:
        raise smoke.SmokeError("OWN_PROCESS_IDENTITY_LOST", "synthetic capture failure")

    monkeypatch.setattr(smoke, "_capture_owned_identity", fail_capture)
    monkeypatch.setattr(
        smoke, "_provisional_session_matches", lambda candidate: candidate is process
    )
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: signals.append((pgid, signum)))

    def interrupt_cleanup(seconds: float) -> None:
        sleeps.append(seconds)
        termination._handle(signal.SIGTERM, None)
        termination._handle(signal.SIGHUP, None)

    monkeypatch.setattr(smoke.time, "sleep", interrupt_cleanup)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._start_server(smoke.MODELS[0], io.BytesIO(), tmp_path, 4, termination)

    assert caught.value.code == "OWN_PROCESS_IDENTITY_LOST"
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]
    assert sleeps == [smoke.STOP_GRACE_SECONDS]
    assert process.wait_timeouts == [smoke.STOP_TIMEOUT_SECONDS]
    with pytest.raises(smoke.SmokeError) as interrupted:
        termination.raise_if_requested()
    assert interrupted.value.code == "RUN_INTERRUPTED"


def test_busy_loopback_port_fails_before_popen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"closed": False, "popen": 0}

    class BusySocket:
        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 18007)
            raise OSError("busy")

        def close(self) -> None:
            state["closed"] = True

    def forbidden_popen(*_args: Any, **_kwargs: Any) -> None:
        state["popen"] += 1
        pytest.fail("Popen must not run when the fixed loopback port is busy")

    monkeypatch.setattr(smoke.socket, "socket", lambda *_args, **_kwargs: BusySocket())
    monkeypatch.setattr(smoke.subprocess, "Popen", forbidden_popen)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._start_server(smoke.MODELS[0], io.BytesIO(), tmp_path, 4)

    assert caught.value.code == "PORT_BUSY"
    assert state == {"closed": True, "popen": 0}


def test_http_failure_is_attempted_once_and_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    counts = {"constructed": 0, "request": 0, "closed": 0}
    request = {"model": "fixture", "messages": []}
    expected_body = json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()

    class FailingConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port, timeout) == (
                smoke.HOST,
                smoke.PORT,
                smoke.REQUEST_TIMEOUT_SECONDS,
            )
            counts["constructed"] += 1

        def request(
            self,
            method: str,
            path: str,
            *,
            body: bytes,
            headers: dict[str, str],
        ) -> None:
            assert method == "POST"
            assert path == smoke.REQUEST_PATH
            assert body == expected_body
            assert headers == {
                "Content-Type": "application/json",
                "Content-Length": str(len(expected_body)),
            }
            counts["request"] += 1
            raise OSError("synthetic transport failure")

        def close(self) -> None:
            counts["closed"] += 1

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", FailingConnection)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._post_json(request)

    assert caught.value.code == "HTTP_FAILED"
    assert counts == {"constructed": 1, "request": 1, "closed": 1}


def test_runner_binds_and_calls_the_existing_host_parsers_from_this_worktree() -> None:
    parsers = smoke._load_host_parsers()
    assert set(parsers) == {"qwen3vl_8b", "mai_ui_8b"}
    for model_id, module_name in smoke.HOST_PARSER_MODULES.items():
        parser_path = inspect.getsourcefile(parsers[model_id])
        expected_path = smoke.MOBILEWORLD_SOURCE_ROOT / Path(*module_name.split(".")).with_suffix(
            ".py"
        )
        assert parser_path is not None
        assert Path(parser_path).resolve() == expected_path.resolve()

    qwen_content = (
        "Thought: inspect only\nAction: wait\n"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"click",'
        '"coordinate":[100,200,300,400]}}'
    )
    qwen_expected = parsers["qwen3vl_8b"](qwen_content)
    assert smoke._parse_host_response("qwen3vl_8b", qwen_content, parsers) == qwen_expected
    assert qwen_expected["action_json"]["coordinate"] == [200 / 999, 300 / 999]

    mai_content = (
        "inspect only</think>"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"drag",'
        '"start_coordinate":[99,198],"end_coordinate":[297,396]}}</tool_call>'
    )
    mai_expected = parsers["mai_ui_8b"](mai_content)
    assert smoke._parse_host_response("mai_ui_8b", mai_content, parsers) == mai_expected
    assert mai_expected["action_json"]["start_coordinate"] == [99 / 999, 198 / 999]


def test_host_parser_import_rejects_a_preloaded_wrong_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poisoned = types.ModuleType("mobile_world")
    poisoned.__file__ = "/tmp/not-this-worktree/mobile_world/__init__.py"
    monkeypatch.setitem(sys.modules, "mobile_world", poisoned)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._load_host_parsers()

    assert caught.value.code == "HOST_PARSER_IMPORT_FAILED"


def test_host_parser_receives_exact_content_once_and_has_no_fallback() -> None:
    content = "exact response bytes projected as text"
    received: list[str] = []

    def parser(value: str) -> dict[str, Any]:
        received.append(value)
        raise ValueError("synthetic production-parser rejection")

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._parse_host_response("qwen3vl_8b", content, {"qwen3vl_8b": parser})

    assert caught.value.code == "HOST_PARSE_FAILED"
    assert received == [content]


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), {"not-json"}])
def test_host_parser_output_must_be_strict_json(invalid: Any) -> None:
    def parser(_content: str) -> dict[str, Any]:
        return {"nested": [invalid]}

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._parse_host_response("qwen3vl_8b", "exact", {"qwen3vl_8b": parser})

    assert caught.value.code == "HOST_PARSE_FAILED"


def test_stop_zombie_root_signals_only_the_revalidated_owned_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(42001)
    server = smoke.OwnedServer(
        process=process, identity=_identity(process.pid), model=smoke.MODELS[0]
    )
    signals: list[tuple[int, signal.Signals]] = []
    sleeps: list[float] = []
    monkeypatch.setattr(
        smoke,
        "_read_process_identity",
        lambda pid: _identity(pid, state="Z"),
    )
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: signals.append((pgid, signum)))
    monkeypatch.setattr(smoke.time, "sleep", sleeps.append)

    assert smoke._stop_server(server) == "session_terminated"
    assert signals == [
        (server.identity.pgid, signal.SIGTERM),
        (server.identity.pgid, signal.SIGKILL),
    ]
    assert sleeps == [smoke.STOP_GRACE_SECONDS]
    assert process.wait_timeouts == [smoke.STOP_TIMEOUT_SECONDS]


def test_stop_refuses_identity_drift_without_sending_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(43001)
    server = smoke.OwnedServer(
        process=process, identity=_identity(process.pid), model=smoke.MODELS[0]
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(smoke, "_identity_matches", lambda _candidate: False)
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: signals.append((pgid, signum)))

    with pytest.raises(smoke.SmokeError) as caught:
        smoke._stop_server(server)

    assert caught.value.code == "OWN_PROCESS_IDENTITY_MISMATCH"
    assert signals == []


def test_term_unwinds_into_owned_cleanup_and_hup_cannot_interrupt_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(43501)
    server = smoke.OwnedServer(
        process=process,
        identity=_identity(process.pid),
        model=smoke.MODELS[0],
    )
    previous = {signal.SIGTERM: object(), signal.SIGHUP: object()}
    active_handlers: dict[signal.Signals, Any] = {}
    handler_sets: list[tuple[signal.Signals, Any]] = []
    sent: list[tuple[int, signal.Signals]] = []
    starts: list[str] = []

    def fake_signal(signum: signal.Signals, handler: Any) -> Any:
        active_handlers[signum] = handler
        handler_sets.append((signum, handler))
        return previous[signum]

    def fake_start(
        model: Any,
        _log_handle: Any,
        _output_dir: Path,
        gpu_index: int,
        _termination: Any,
    ) -> Any:
        assert gpu_index == 4
        starts.append(model.model_id)
        return server

    def interrupt_ready(_server: Any, _termination: Any) -> None:
        active_handlers[signal.SIGTERM](signal.SIGTERM, None)

    def interrupt_during_cleanup(seconds: float) -> None:
        assert seconds == smoke.STOP_GRACE_SECONDS
        # The second signal is deliberately delivered from inside owned cleanup.  It must return,
        # not raise and skip the KILL sweep or exact wait.
        active_handlers[signal.SIGHUP](signal.SIGHUP, None)

    monkeypatch.setattr(smoke.signal, "getsignal", lambda signum: previous[signum])
    monkeypatch.setattr(smoke.signal, "signal", fake_signal)
    monkeypatch.setattr(smoke, "_start_server", fake_start)
    monkeypatch.setattr(smoke, "_wait_for_ready", interrupt_ready)
    monkeypatch.setattr(smoke, "_read_process_identity", lambda _pid: _identity(process.pid))
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: sent.append((pgid, signum)))
    monkeypatch.setattr(smoke.time, "sleep", interrupt_during_cleanup)
    monkeypatch.setattr(
        smoke,
        "_post_json",
        lambda _request: pytest.fail("no request may be sent after supervisor termination"),
    )

    with pytest.raises(smoke.SmokeError) as caught:
        smoke.run(REQUESTS, tmp_path / "interrupted", 4)

    assert caught.value.code == "RUN_INTERRUPTED"
    assert starts == ["qwen3vl_8b"]
    assert sent == [
        (server.identity.pgid, signal.SIGTERM),
        (server.identity.pgid, signal.SIGKILL),
    ]
    assert process.wait_timeouts == [smoke.STOP_TIMEOUT_SECONDS]
    assert active_handlers == previous
    assert handler_sets[-2:] == [
        (signal.SIGHUP, previous[signal.SIGHUP]),
        (signal.SIGTERM, previous[signal.SIGTERM]),
    ]
    events = [
        json.loads(line) for line in (tmp_path / "interrupted/run.jsonl").read_text().splitlines()
    ]
    assert events[-1]["event"] == "run_failed"
    assert events[-1]["error_code"] == "RUN_INTERRUPTED"


def test_term_flagged_as_start_returns_is_checked_after_caller_registers_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(43701)
    server = smoke.OwnedServer(
        process=process,
        identity=_identity(process.pid),
        model=smoke.MODELS[0],
    )
    previous = {signal.SIGTERM: object(), signal.SIGHUP: object()}
    sent: list[tuple[int, signal.Signals]] = []
    starts: list[str] = []

    def fake_start(
        model: Any,
        _log_handle: Any,
        _output_dir: Path,
        gpu_index: int,
        termination: Any,
    ) -> Any:
        assert gpu_index == 4
        starts.append(model.model_id)
        termination._handle(signal.SIGTERM, None)
        return server

    monkeypatch.setattr(smoke.signal, "getsignal", lambda signum: previous[signum])
    monkeypatch.setattr(smoke.signal, "signal", lambda _signum, _handler: None)
    monkeypatch.setattr(smoke, "_start_server", fake_start)
    monkeypatch.setattr(
        smoke,
        "_wait_for_ready",
        lambda *_args: pytest.fail("caller must observe termination before readiness"),
    )
    monkeypatch.setattr(smoke, "_read_process_identity", lambda _pid: _identity(process.pid))
    monkeypatch.setattr(smoke.os, "killpg", lambda pgid, signum: sent.append((pgid, signum)))
    monkeypatch.setattr(smoke.time, "sleep", lambda _seconds: None)

    with pytest.raises(smoke.SmokeError) as caught:
        smoke.run(REQUESTS, tmp_path / "return-race", 4)

    assert caught.value.code == "RUN_INTERRUPTED"
    assert starts == ["qwen3vl_8b"]
    assert sent == [
        (server.identity.pgid, signal.SIGTERM),
        (server.identity.pgid, signal.SIGKILL),
    ]
    assert process.wait_timeouts == [smoke.STOP_TIMEOUT_SECONDS]


def test_run_orders_qwen_cleanup_before_mai_and_only_parses_actions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    trace: list[tuple[str, str]] = []
    active_model: list[str] = []
    submitted_requests: list[dict[str, Any]] = []

    class FakeTerminationController:
        def install(self) -> None:
            return None

        def raise_if_requested(self) -> None:
            return None

        def begin_cleanup(self) -> None:
            return None

        def end_cleanup(self) -> None:
            return None

        def restore(self) -> None:
            return None

    def fake_start(
        model: Any,
        _log_handle: Any,
        _output_dir: Path,
        gpu_index: int,
        _termination: Any,
    ) -> Any:
        assert gpu_index == 4
        assert active_model == []
        active_model.append(model.model_id)
        trace.append(("start", model.model_id))
        process = FakeProcess(44001 if model.model_id == "qwen3vl_8b" else 44002)
        return smoke.OwnedServer(process=process, identity=_identity(process.pid), model=model)

    def fake_ready(server: Any, _termination: Any) -> None:
        assert active_model == [server.model.model_id]
        trace.append(("ready", server.model.model_id))

    def fake_post(request: dict[str, Any]) -> tuple[int, bytes]:
        assert len(active_model) == 1
        submitted_requests.append(request)
        trace.append(("post", active_model[0]))
        return 200, _response(active_model[0])

    def fake_stop(server: Any) -> str:
        assert active_model == [server.model.model_id]
        trace.append(("stop", server.model.model_id))
        active_model.clear()
        return "terminated"

    monkeypatch.setattr(smoke, "_start_server", fake_start)
    monkeypatch.setattr(smoke, "_wait_for_ready", fake_ready)
    monkeypatch.setattr(smoke, "_post_json", fake_post)
    monkeypatch.setattr(smoke, "_stop_server", fake_stop)
    monkeypatch.setattr(smoke, "_TerminationController", FakeTerminationController)

    output_dir = tmp_path / "out"
    smoke.run(REQUESTS, output_dir, 4)

    assert active_model == []
    assert trace.count(("post", "qwen3vl_8b")) == 11
    assert trace.count(("post", "mai_ui_8b")) == 11
    assert trace.index(("stop", "qwen3vl_8b")) < trace.index(("start", "mai_ui_8b"))
    assert trace[-1] == ("stop", "mai_ui_8b")
    assert [request["seed"] for request in submitted_requests] == [
        expected[3] for expected in smoke.EXPECTED_CALLS
    ]
    assert all("stream" not in request for request in submitted_requests)

    events = [json.loads(line) for line in (output_dir / "run.jsonl").read_text().splitlines()]
    run_started = events[0]
    assert run_started["runner_python"] == sys.executable
    assert "runner_argv" not in run_started
    call_events = [event for event in events if event["event"] == "call_succeeded"]
    assert [event["call_id"] for event in call_events] == [
        expected[0] for expected in smoke.EXPECTED_CALLS
    ]
    assert all(event["generated_action_executed"] is False for event in call_events)
    assert all(
        event["host_parser_output"].get("action_name", "mobile_use") == "mobile_use"
        and event["host_parser_output"].get("tool_name", "mobile_use") == "mobile_use"
        for event in call_events
    )
    response_events = [event for event in events if event["event"] == "response_received"]
    assert len(response_events) == 22
    assert all(
        base64.b64decode(event["response_body_base64"], validate=True)
        == _response(event["model_id"])
        for event in response_events
    )
    assert events[-1] == {
        "sequence": len(events) - 1,
        "event": "run_completed",
        "successful_call_count": 22,
    }


def test_parse_failure_preserves_exact_raw_response_before_stopping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(45001)
    server = smoke.OwnedServer(
        process=process,
        identity=_identity(process.pid),
        model=smoke.MODELS[0],
    )
    raw_payload = json.dumps(
        {"choices": [{"message": {"content": "not a valid host response"}}]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    post_calls: list[dict[str, Any]] = []

    class FakeTerminationController:
        def install(self) -> None:
            return None

        def raise_if_requested(self) -> None:
            return None

        def begin_cleanup(self) -> None:
            return None

        def end_cleanup(self) -> None:
            return None

        def restore(self) -> None:
            return None

    def reject(_content: str) -> dict[str, Any]:
        raise ValueError("synthetic production-parser rejection")

    monkeypatch.setattr(smoke, "_TerminationController", FakeTerminationController)
    monkeypatch.setattr(smoke, "_load_host_parsers", lambda: {"qwen3vl_8b": reject})
    monkeypatch.setattr(smoke, "_start_server", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(smoke, "_wait_for_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_stop_server", lambda _server: "terminated")

    def fake_post(request: dict[str, Any]) -> tuple[int, bytes]:
        post_calls.append(request)
        return 200, raw_payload

    monkeypatch.setattr(smoke, "_post_json", fake_post)

    output_dir = tmp_path / "parse-failure"
    with pytest.raises(smoke.SmokeError) as caught:
        smoke.run(REQUESTS, output_dir, 4)

    assert caught.value.code == "HOST_PARSE_FAILED"
    assert len(post_calls) == 1
    events = [json.loads(line) for line in (output_dir / "run.jsonl").read_text().splitlines()]
    response_event = next(event for event in events if event["event"] == "response_received")
    assert response_event["call_id"] == smoke.EXPECTED_CALLS[0][0]
    assert response_event["http_status"] == 200
    assert response_event["response_byte_count"] == len(raw_payload)
    assert response_event["response_sha256"] == hashlib.sha256(raw_payload).hexdigest()
    assert base64.b64decode(response_event["response_body_base64"], validate=True) == raw_payload
    assert [event["event"] for event in events[-3:]] == [
        "response_received",
        "server_stopped",
        "run_failed",
    ]
    assert events[-1]["error_code"] == "HOST_PARSE_FAILED"


def test_termination_after_http_return_preserves_raw_before_parser_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(46001)
    server = smoke.OwnedServer(
        process=process,
        identity=_identity(process.pid),
        model=smoke.MODELS[0],
    )
    raw_payload = b'{"choices":[{"message":{"content":"must be preserved"}}]}'
    controllers: list[Any] = []
    post_count = 0
    parser_count = 0
    stop_count = 0

    class FlagTerminationController:
        def __init__(self) -> None:
            self.requested = False
            controllers.append(self)

        def install(self) -> None:
            return None

        def raise_if_requested(self) -> None:
            if self.requested:
                raise smoke.SmokeError("RUN_INTERRUPTED", "synthetic termination")

        def begin_cleanup(self) -> None:
            return None

        def end_cleanup(self) -> None:
            return None

        def restore(self) -> None:
            return None

    def parser(_content: str) -> dict[str, Any]:
        nonlocal parser_count
        parser_count += 1
        return {"action_name": "mobile_use", "action_json": {"action": "wait"}}

    def fake_post(_request: dict[str, Any]) -> tuple[int, bytes]:
        nonlocal post_count
        post_count += 1
        controllers[0].requested = True
        return 200, raw_payload

    def fake_stop(_server: Any) -> str:
        nonlocal stop_count
        stop_count += 1
        return "terminated"

    monkeypatch.setattr(smoke, "_TerminationController", FlagTerminationController)
    monkeypatch.setattr(smoke, "_load_host_parsers", lambda: {"qwen3vl_8b": parser})
    monkeypatch.setattr(smoke, "_start_server", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(smoke, "_wait_for_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_post_json", fake_post)
    monkeypatch.setattr(smoke, "_stop_server", fake_stop)

    output_dir = tmp_path / "termination-after-http"
    with pytest.raises(smoke.SmokeError) as caught:
        smoke.run(REQUESTS, output_dir, 4)

    assert caught.value.code == "RUN_INTERRUPTED"
    assert (post_count, parser_count, stop_count) == (1, 0, 1)
    events = [json.loads(line) for line in (output_dir / "run.jsonl").read_text().splitlines()]
    response_event = next(event for event in events if event["event"] == "response_received")
    assert response_event["response_byte_count"] == len(raw_payload)
    assert response_event["response_sha256"] == hashlib.sha256(raw_payload).hexdigest()
    assert base64.b64decode(response_event["response_body_base64"], validate=True) == raw_payload
    assert [event["event"] for event in events[-3:]] == [
        "response_received",
        "server_stopped",
        "run_failed",
    ]
    assert events[-1]["error_code"] == "RUN_INTERRUPTED"
