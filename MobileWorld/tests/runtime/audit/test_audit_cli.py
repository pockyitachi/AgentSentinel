from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from mobile_world.core.subcommands import eval as eval_module


def _parse(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    eval_module.configure_parser(subparsers)
    return parser.parse_args(["eval", *arguments])


def test_audit_cli_defaults_off_and_accepts_explicit_chunk_policy() -> None:
    defaults = _parse("--agent-type", "fixture")
    assert defaults.enable_audit is False
    assert defaults.audit_log_root is None
    assert not hasattr(defaults, "audit_collector_mode")
    assert defaults.audit_store_stream_chunks is True

    configured = _parse(
        "--agent-type",
        "fixture",
        "--enable_audit",
        "--audit_log_root",
        "/external/audit",
        "--no_audit_store_stream_chunks",
    )
    assert configured.enable_audit is True
    assert configured.audit_log_root == "/external/audit"
    assert configured.audit_store_stream_chunks is False

    with pytest.raises(SystemExit):
        _parse(
            "--agent-type",
            "fixture",
            "--audit-collector-mode",
            "unsupported",
        )


@pytest.mark.parametrize("runner_raises", [False, True])
@pytest.mark.parametrize("finalize_raises", [False, True])
def test_run_wrapper_finalizes_exactly_once_on_normal_and_exceptional_exit(
    monkeypatch: pytest.MonkeyPatch,
    runner_raises: bool,
    finalize_raises: bool,
) -> None:
    statuses: list[str] = []
    original_error = RuntimeError("fixture runner failure")

    class Lifecycle:
        enabled = True
        degraded = False

        def finalize(self, *, runtime_status: str) -> None:
            statuses.append(runtime_status)
            if finalize_raises:
                raise OSError("fixture collector finalization failure")

    lifecycle = Lifecycle()
    monkeypatch.setattr(eval_module, "_start_eval_audit", lambda *args, **kwargs: lifecycle)

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        assert kwargs["audit_lifecycle"] is lifecycle
        if runner_raises:
            raise original_error
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    if runner_raises:
        with pytest.raises(RuntimeError) as raised:
            eval_module._run_evaluation_once(
                args=argparse.Namespace(),
                api_key=None,
            )
        assert raised.value is original_error
        assert statuses == ["crashed"]
    else:
        assert eval_module._run_evaluation_once(
            args=argparse.Namespace(),
            api_key=None,
        ) == ([], [])
        assert statuses == ["completed"]


def test_run_wrapper_bootstrap_failure_preserves_runner_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ([{"task": "result"}], ["pending"])

    def fail_bootstrap(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture collector bootstrap failure")

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        assert kwargs["audit_lifecycle"] is None
        return expected

    monkeypatch.setattr(eval_module, "_start_eval_audit", fail_bootstrap)
    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)

    assert (
        eval_module._run_evaluation_once(
            args=argparse.Namespace(),
            api_key=None,
        )
        is expected
    )


@pytest.mark.parametrize("dirty_state", [True, False, None])
def test_enabled_cli_passes_detected_repository_dirty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_state: bool | None,
) -> None:
    captured: dict[str, Any] = {}
    lifecycle = object()
    args = argparse.Namespace(
        enable_audit=True,
        audit_log_root=str(tmp_path / "audit"),
        audit_store_stream_chunks=True,
        agent_type="fixture",
        model_name="fixture-model",
        suite_family="mobile_world",
        env_image="fixture-image",
    )

    monkeypatch.setattr(eval_module, "detect_repository_dirty", lambda: dirty_state)

    def fake_bootstrap(config: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return lifecycle

    monkeypatch.setattr(eval_module, "bootstrap_audit_run", fake_bootstrap)

    assert eval_module._start_eval_audit(args, effective_api_key=None) is lifecycle
    assert captured["repository_dirty"] is dirty_state


def test_enabled_cli_git_detection_exception_becomes_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    lifecycle = object()
    args = argparse.Namespace(
        enable_audit=True,
        audit_log_root=str(tmp_path / "audit"),
        audit_store_stream_chunks=True,
        agent_type="fixture",
        model_name="fixture-model",
        suite_family="mobile_world",
        env_image="fixture-image",
    )

    def fail_detection() -> bool:
        raise OSError("fixture repository inspection failure")

    monkeypatch.setattr(eval_module, "detect_repository_dirty", fail_detection)

    def fake_bootstrap(config: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return lifecycle

    monkeypatch.setattr(eval_module, "bootstrap_audit_run", fake_bootstrap)

    assert eval_module._start_eval_audit(args, effective_api_key=None) is lifecycle
    assert captured["repository_dirty"] is None


def test_disabled_cli_does_not_inspect_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = argparse.Namespace(
        enable_audit=False,
        audit_log_root=None,
        audit_store_stream_chunks=True,
    )

    def fail_if_called() -> None:
        raise AssertionError("disabled audit inspected the repository")

    monkeypatch.setattr(eval_module, "detect_repository_dirty", fail_if_called)

    assert eval_module._start_eval_audit(args, effective_api_key=None).enabled is False


@pytest.mark.asyncio
async def test_default_off_execute_passes_no_lifecycle_and_creates_no_audit_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        calls.append(kwargs)
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    monkeypatch.delenv("API_KEY", raising=False)
    args = _parse(
        "--agent-type",
        "fixture",
        "--task",
        "FixtureTask",
        "--aw-host",
        "http://127.0.0.1:5000",
        "--log-file-root",
        str(tmp_path / "trajectory"),
    )

    await eval_module.execute(args)

    assert len(calls) == 1
    assert calls[0]["audit_lifecycle"] is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_enabled_execute_creates_one_finalized_external_run_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "cli-api-secret-value"
    received_lifecycles: list[Any] = []

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        received_lifecycles.append(kwargs["audit_lifecycle"])
        assert kwargs["api_key"] == secret
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    audit_root = tmp_path / "audit-data"
    args = _parse(
        "--agent-type",
        "fixture",
        "--model-name",
        "fixture-model",
        "--api-key",
        secret,
        "--task",
        "FixtureTask",
        "--aw-host",
        "http://127.0.0.1:5000",
        "--log-file-root",
        str(tmp_path / "trajectory"),
        "--enable-audit",
        "--audit-log-root",
        str(audit_root),
    )

    await eval_module.execute(args)

    assert len(received_lifecycles) == 1
    lifecycle = received_lifecycles[0]
    run_root = lifecycle.recorder.run_root
    assert run_root.parent.parent.parent == audit_root
    assert (run_root / "manifest.start.json").is_file()
    assert (run_root / "manifest.final.json").is_file()
    assert secret.encode() not in b"".join(
        path.read_bytes() for path in run_root.rglob("*") if path.is_file()
    )


@pytest.mark.asyncio
async def test_enabled_cli_missing_root_degrades_and_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        calls.append(kwargs)
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    args = _parse(
        "--agent-type",
        "fixture",
        "--task",
        "FixtureTask",
        "--aw-host",
        "http://127.0.0.1:5000",
        "--enable-audit",
    )

    await eval_module.execute(args)

    assert len(calls) == 1
    assert calls[0]["audit_lifecycle"] is None


@pytest.mark.asyncio
async def test_pass_k_creates_independent_audit_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycles: list[Any] = []

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        lifecycles.append(kwargs["audit_lifecycle"])
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    audit_root = tmp_path / "audit-data"
    args = _parse(
        "--agent-type",
        "fixture",
        "--task",
        "FixtureTask",
        "--aw-host",
        "http://127.0.0.1:5000",
        "--log-file-root",
        str(tmp_path / "trajectory"),
        "--pass-k",
        "2",
        "--enable-audit",
        "--audit-log-root",
        str(audit_root),
    )

    await eval_module.execute(args)

    assert len(lifecycles) == 2
    assert len({lifecycle.run_id for lifecycle in lifecycles}) == 2
    assert all(lifecycle.recorder.manifest_final_path.is_file() for lifecycle in lifecycles)
