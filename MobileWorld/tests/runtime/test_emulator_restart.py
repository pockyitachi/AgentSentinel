from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from mobile_world.runtime.utils import docker as docker_utils

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "docker" / "start_emulator.sh"


def _configure_restart_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    proc_root = tmp_path / "proc"
    log_path = tmp_path / "emulator.log"
    state_dir.mkdir()
    proc_root.mkdir()
    log_path.write_text("previous generation\n", encoding="utf-8")
    monkeypatch.setattr(docker_utils, "_EMULATOR_SCRIPT_PATH", SCRIPT_PATH)
    monkeypatch.setattr(docker_utils, "_EMULATOR_STATE_DIR", state_dir)
    monkeypatch.setattr(docker_utils, "_EMULATOR_LOG_PATH", log_path)
    monkeypatch.setattr(docker_utils, "_PROC_ROOT", proc_root)
    return state_dir, proc_root


def _write_ready_generation(
    *,
    state_dir: Path,
    proc_root: Path,
    generation_id: str,
    avd_name: str,
    log_path: Path,
    pid: int = 4312,
    device_id: str = "emulator-5554",
) -> None:
    process_dir = proc_root / str(pid)
    process_dir.mkdir(parents=True, exist_ok=True)
    process_dir.joinpath("cmdline").write_bytes(
        b"/opt/android-sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64\0"
        + b"-avd\0"
        + avd_name.encode("utf-8")
        + b"\0"
    )
    state_dir.joinpath("emulator.state.json").write_text(
        json.dumps(
            {
                "generation_id": generation_id,
                "pid": pid,
                "avd_name": avd_name,
                "device_id": device_id,
                "log_path": str(log_path),
                "ready": True,
                "ready_at": "2026-08-21T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _adb_result(command: list[str]) -> subprocess.CompletedProcess[str]:
    if command == ["adb", "devices"]:
        return subprocess.CompletedProcess(
            command,
            0,
            "List of devices attached\nemulator-5554\tdevice\n",
            "",
        )
    if command == [
        "adb",
        "-s",
        "emulator-5554",
        "shell",
        "getprop",
        "sys.boot_completed",
    ]:
        return subprocess.CompletedProcess(command, 0, "1\n", "")
    raise AssertionError(f"unexpected command: {command}")


def test_restart_uses_verified_generation_even_when_script_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir, proc_root = _configure_restart_paths(monkeypatch, tmp_path)

    def fake_restart(env: dict[str, str]) -> tuple[int | None, str]:
        _write_ready_generation(
            state_dir=state_dir,
            proc_root=proc_root,
            generation_id=env["MOBILEWORLD_EMULATOR_GENERATION_ID"],
            avd_name=env["AVD_NAME"],
            log_path=docker_utils._EMULATOR_LOG_PATH,
        )
        assert env["EMULATOR_TIMEOUT"] == "110"
        assert env["EMULATOR_ROOT_RECONNECT_TIMEOUT"] == "15"
        assert env["EMULATOR_SHUTDOWN_TIMEOUT"] == "8"
        assert env["EMULATOR_TERM_TIMEOUT"] == "3"
        assert env["EMULATOR_KILL_TIMEOUT"] == "2"
        assert env["EMULATOR_POLL_INTERVAL"] == "2"
        assert env["MOBILEWORLD_PROXY_STARTUP_WAIT"] == "1"
        return 1, "adb root returned during transport disconnect"

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _adb_result(command)

    monkeypatch.setattr(docker_utils, "_run_emulator_restart_script", fake_restart)
    monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

    assert docker_utils.restart_emulator_with_avd("Pixel_8_API_34_x86_64") == "emulator-5554"


def test_restart_rejects_zero_exit_without_this_generation_postcondition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir, proc_root = _configure_restart_paths(monkeypatch, tmp_path)

    def fake_restart(env: dict[str, str]) -> tuple[int | None, str]:
        _write_ready_generation(
            state_dir=state_dir,
            proc_root=proc_root,
            generation_id="an-older-generation",
            avd_name=env["AVD_NAME"],
            log_path=docker_utils._EMULATOR_LOG_PATH,
        )
        return 0, "script said success"

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("ADB must not be queried for a stale generation")

    monkeypatch.setattr(docker_utils, "_run_emulator_restart_script", fake_restart)
    monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="verified new emulator generation"):
        docker_utils.restart_emulator_with_avd("Pixel_8_API_34_x86_64")


def test_restart_rejects_reused_process_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir, proc_root = _configure_restart_paths(monkeypatch, tmp_path)
    old_pid = 9123
    _write_ready_generation(
        state_dir=state_dir,
        proc_root=proc_root,
        generation_id="old-generation",
        avd_name="Pixel_8_API_34_x86_64",
        log_path=docker_utils._EMULATOR_LOG_PATH,
        pid=old_pid,
    )

    def fake_restart(env: dict[str, str]) -> tuple[int | None, str]:
        _write_ready_generation(
            state_dir=state_dir,
            proc_root=proc_root,
            generation_id=env["MOBILEWORLD_EMULATOR_GENERATION_ID"],
            avd_name=env["AVD_NAME"],
            log_path=docker_utils._EMULATOR_LOG_PATH,
            pid=old_pid,
        )
        return 0, ""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("ADB must not be queried for a reused process")

    monkeypatch.setattr(docker_utils, "_run_emulator_restart_script", fake_restart)
    monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="verified new emulator generation"):
        docker_utils.restart_emulator_with_avd("Pixel_8_API_34_x86_64")


def test_restart_rejects_ambiguous_online_emulators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir, proc_root = _configure_restart_paths(monkeypatch, tmp_path)

    def fake_restart(env: dict[str, str]) -> tuple[int | None, str]:
        _write_ready_generation(
            state_dir=state_dir,
            proc_root=proc_root,
            generation_id=env["MOBILEWORLD_EMULATOR_GENERATION_ID"],
            avd_name=env["AVD_NAME"],
            log_path=docker_utils._EMULATOR_LOG_PATH,
        )
        return 0, ""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["adb", "devices"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "List of devices attached\nemulator-5554\tdevice\nemulator-5556\tdevice\n",
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(docker_utils, "_run_emulator_restart_script", fake_restart)
    monkeypatch.setattr(docker_utils.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="verified new emulator generation"):
        docker_utils.restart_emulator_with_avd("Pixel_8_API_34_x86_64")


def test_process_matcher_requires_android_emulator_binary_and_exact_avd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    proc_root = tmp_path / "proc"
    process_dir = proc_root / "1234"
    process_dir.mkdir(parents=True)
    monkeypatch.setattr(docker_utils, "_PROC_ROOT", proc_root)
    process_dir.joinpath("cmdline").write_bytes(
        b"/opt/android-sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64\0"
        b"-avd\0Pixel_8_API_34_x86_64\0-no-window\0"
    )

    assert docker_utils._emulator_process_matches(1234, "Pixel_8_API_34_x86_64") is True
    assert docker_utils._emulator_process_matches(1234, "Another_AVD") is False

    process_dir.joinpath("cmdline").write_bytes(
        b"/usr/bin/qemu-system-x86_64\0-machine\0pc\0-name\0unrelated-vm\0"
    )
    assert docker_utils._emulator_process_matches(1234, "Pixel_8_API_34_x86_64") is False

    process_dir.joinpath("cmdline").write_bytes(
        b"/usr/bin/python3\0worker.py\0-avd\0Pixel_8_API_34_x86_64\0"
    )
    assert docker_utils._emulator_process_matches(1234, "Pixel_8_API_34_x86_64") is False


def test_restart_script_uses_pipes_and_a_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popen_kwargs: dict[str, Any] = {}

    class FakeProcess:
        pid = 777
        returncode = 9

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            assert timeout == docker_utils._RESTART_HARD_DEADLINE_SECONDS
            return "bounded stdout", "bounded stderr"

    def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
        assert command == ["/bin/bash", str(docker_utils._EMULATOR_SCRIPT_PATH)]
        popen_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(docker_utils.subprocess, "Popen", fake_popen)

    returncode, diagnostic = docker_utils._run_emulator_restart_script({})

    assert returncode == 9
    assert "bounded stdout" in diagnostic
    assert "bounded stderr" in diagnostic
    assert popen_kwargs["stdout"] is subprocess.PIPE
    assert popen_kwargs["stderr"] is subprocess.PIPE
    assert popen_kwargs["text"] is True
    assert popen_kwargs["start_new_session"] is True


def test_restart_hard_deadline_terminates_only_its_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, signal.Signals]] = []

    class TimeoutProcess:
        pid = 778
        returncode = -signal.SIGTERM
        calls = 0

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                assert timeout == docker_utils._RESTART_HARD_DEADLINE_SECONDS
                raise subprocess.TimeoutExpired("start_emulator.sh", timeout, "partial", "warning")
            assert timeout == docker_utils._RESTART_TERMINATION_GRACE_SECONDS
            return "terminated stdout", "terminated stderr"

    process = TimeoutProcess()
    monkeypatch.setattr(docker_utils.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        docker_utils.os,
        "killpg",
        lambda pid, sig: signals.append((pid, signal.Signals(sig))),
    )

    returncode, diagnostic = docker_utils._run_emulator_restart_script({})

    assert returncode is None
    assert signals == [(process.pid, signal.SIGTERM)]
    assert "150s hard deadline" in diagnostic
    assert "terminated stdout" in diagnostic


def test_restart_hard_deadline_leaves_no_child_or_late_state_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    child_pid_path = tmp_path / "child.pid"
    late_state_path = tmp_path / "late-state"
    hanging_script = tmp_path / "hanging-restart.sh"
    _write_executable(
        hanging_script,
        f"""#!/usr/bin/env bash
(
    trap 'exit 0' TERM INT
    printf '%s\n' "$BASHPID" > {child_pid_path!s}
    sleep 1
    printf 'late mutation\n' > {late_state_path!s}
) &
wait
""",
    )
    monkeypatch.setattr(docker_utils, "_EMULATOR_SCRIPT_PATH", hanging_script)
    monkeypatch.setattr(docker_utils, "_RESTART_HARD_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(docker_utils, "_RESTART_TERMINATION_GRACE_SECONDS", 0.5)

    returncode, diagnostic = docker_utils._run_emulator_restart_script({})

    assert returncode is None
    assert "0.2s hard deadline" in diagnostic
    child_pid = int(child_pid_path.read_text().strip())
    _wait_until_gone(child_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    time.sleep(1.1)
    assert not late_state_path.exists()


def test_restart_diagnostic_is_bounded_and_redacts_transport_secrets() -> None:
    diagnostic = docker_utils._safe_bounded_diagnostic(
        "x" * 20_000,
        "proxy=http://alice:supersecret@example.invalid:8080\n",
        "Authorization: bearer-value\n",
    )

    assert "supersecret" not in diagnostic
    assert "bearer-value" not in diagnostic
    assert "<redacted>" in diagnostic
    assert len(diagnostic) <= docker_utils._RESTART_DIAGNOSTIC_LIMIT + 100


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _wait_until_gone(pid: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)


def test_start_script_handles_offline_old_process_root_disconnect_and_reuses_proxy(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    started_marker = tmp_path / "emulator-started"
    state_dir = tmp_path / "state"
    emulator_log = tmp_path / "emulator.log"
    proxy_log = tmp_path / "proxy.log"
    proxy_script = tmp_path / "proxy_chain.py"
    emulator_log.write_text("preserved previous emulator log\n", encoding="utf-8")

    _write_executable(
        fake_bin / "emulator",
        """#!/usr/bin/env bash
set -u
if [ "${FAKE_STAY_OFFLINE:-0}" != "1" ]; then
    touch "$FAKE_EMULATOR_STARTED"
fi
cleanup() {
    rm -f "$FAKE_EMULATOR_STARTED"
    exit 0
}
trap cleanup TERM INT
while true; do sleep 0.05; done
""",
    )
    _write_executable(
        fake_bin / "adb",
        """#!/usr/bin/env bash
set -u
if [ "${1:-}" = "devices" ]; then
    printf 'List of devices attached\n'
    if [ -f "$FAKE_EMULATOR_STARTED" ]; then
        printf 'emulator-5554\tdevice\n'
    fi
    exit 0
fi
case " $* " in
    *" emu kill "*) exit 1 ;;
    *" get-state "*) printf 'device\n'; exit 0 ;;
    *" getprop sys.boot_completed "*) printf '1\n'; exit 0 ;;
    *" root "*) printf 'transport disconnected while adbd restarts\n' >&2; exit 1 ;;
    *" input keyevent 82 "*) exit 0 ;;
    *" settings put global "*) exit 0 ;;
esac
printf 'unexpected fake adb call: %s\n' "$*" >&2
exit 2
""",
    )
    _write_executable(
        fake_bin / "qemu-system-x86_64",
        """#!/usr/bin/env bash
trap 'exit 0' TERM INT
while true; do sleep 0.05; done
""",
    )
    proxy_script.write_text(
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_: raise_exit())\n"
        "def raise_exit():\n"
        "    raise SystemExit(0)\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )

    base_env = os.environ.copy()
    base_env.update(
        {
            "PATH": f"{fake_bin}:{base_env['PATH']}",
            "AVD_NAME": "Pixel_8_API_34_x86_64",
            "MOBILEWORLD_EMULATOR_STATE_DIR": str(state_dir),
            "MOBILEWORLD_EMULATOR_LOG_PATH": str(emulator_log),
            "MOBILEWORLD_PROXY_LOG_PATH": str(proxy_log),
            "MOBILEWORLD_PROXY_CHAIN_SCRIPT": str(proxy_script),
            "FAKE_EMULATOR_STARTED": str(started_marker),
            "EMULATOR_TIMEOUT": "3",
            "EMULATOR_SHUTDOWN_TIMEOUT": "0",
            "EMULATOR_TERM_TIMEOUT": "1",
            "EMULATOR_KILL_TIMEOUT": "1",
            "EMULATOR_ROOT_RECONNECT_TIMEOUT": "2",
            "EMULATOR_POLL_INTERVAL": "0",
            "MOBILEWORLD_PROXY_STARTUP_WAIT": "0",
            "HTTP_PROXY": "http://alice:supersecret@proxy.example.invalid:8080",
            "http_proxy": "",
        }
    )

    old_log = tmp_path / "old-emulator.log"
    old_log_handle = old_log.open("w", encoding="utf-8")
    old_env = base_env | {"FAKE_STAY_OFFLINE": "1"}
    old_process = subprocess.Popen(
        [str(fake_bin / "emulator"), "-avd", "Old_AVD"],
        env=old_env,
        stdout=old_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    unrelated_log = tmp_path / "unrelated-qemu.log"
    unrelated_log_handle = unrelated_log.open("w", encoding="utf-8")
    unrelated_qemu = subprocess.Popen(
        [str(fake_bin / "qemu-system-x86_64"), "-machine", "pc"],
        env=base_env,
        stdout=unrelated_log_handle,
        stderr=subprocess.STDOUT,
        text=True,
    )
    decoy_process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "qemu-system-x86_64",
            "-avd",
            "Pixel_8_API_34_x86_64",
        ],
        env=base_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    state_dir.mkdir()
    state_dir.joinpath("emulator.pid").write_text(
        f"{old_process.pid}\tOld_AVD\told-generation\n", encoding="utf-8"
    )

    emulator_pids: set[int] = set()
    proxy_pid = 0
    try:
        first_env = base_env | {"MOBILEWORLD_EMULATOR_GENERATION_ID": "generation-one"}
        first = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH)],
            env=first_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert first.returncode == 0, first.stdout + first.stderr
        old_process.wait(timeout=3)
        first_state = json.loads(state_dir.joinpath("emulator.state.json").read_text())
        emulator_pids.add(first_state["pid"])
        assert first_state["generation_id"] == "generation-one"
        assert first_state["ready"] is True
        assert unrelated_qemu.poll() is None
        assert decoy_process.poll() is None
        assert "adb root returned 1" in first.stderr
        assert "supersecret" not in first.stdout + first.stderr
        proxy_pid = int(state_dir.joinpath("proxy-chain.pid").read_text().strip())
        os.kill(proxy_pid, 0)

        second_env = base_env | {"MOBILEWORLD_EMULATOR_GENERATION_ID": "generation-two"}
        second = subprocess.run(
            ["/bin/bash", str(SCRIPT_PATH)],
            env=second_env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode == 0, second.stdout + second.stderr
        second_state = json.loads(state_dir.joinpath("emulator.state.json").read_text())
        emulator_pids.add(second_state["pid"])
        assert second_state["generation_id"] == "generation-two"
        assert int(state_dir.joinpath("proxy-chain.pid").read_text().strip()) == proxy_pid
        assert "reusing existing in-container proxy chain" in second.stdout
        assert "supersecret" not in second.stdout + second.stderr
        assert unrelated_qemu.poll() is None
        assert decoy_process.poll() is None

        retained_log = emulator_log.read_text(encoding="utf-8")
        assert retained_log.startswith("preserved previous emulator log\n")
        assert "generation generation-one" in retained_log
        assert "generation generation-two" in retained_log
    finally:
        old_log_handle.close()
        unrelated_log_handle.close()
        for pid in emulator_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            _wait_until_gone(pid)
        if proxy_pid:
            try:
                os.kill(proxy_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            _wait_until_gone(proxy_pid)
        if old_process.poll() is None:
            old_process.terminate()
            old_process.wait(timeout=3)
        if unrelated_qemu.poll() is None:
            unrelated_qemu.terminate()
            unrelated_qemu.wait(timeout=3)
        if decoy_process.poll() is None:
            decoy_process.terminate()
            decoy_process.wait(timeout=3)


def test_start_emulator_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
