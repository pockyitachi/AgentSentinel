from __future__ import annotations

import argparse
import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from mobile_world.core.subcommands import eval as eval_module


def _parse(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    eval_module.configure_parser(subparsers)
    return parser.parse_args(["eval", *arguments])


def test_ui_tree_help_discloses_bounded_actor_text_and_raw_audit(capsys) -> None:
    with pytest.raises(SystemExit) as stopped:
        _parse("--help")
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "UI labels and non-password input values enter the actor prompt" in help_text
    assert "raw XML is saved in the audit" in help_text


def test_ui_tree_option_requires_enabled_ledger_before_resources(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid option reached resource bootstrap")

    monkeypatch.setattr(eval_module, "_start_eval_audit", unexpected)
    args = _parse("--agent-type", "qwen3vl", "--gui-ledger-ui-tree")
    with pytest.raises(ValueError, match="requires --gui-ledger"):
        eval_module._run_evaluation_once(args=args, api_key=None)


@pytest.mark.parametrize("degraded", [False, True])
def test_ui_tree_option_forwarded_and_disabled_with_degraded_audit(
    tmp_path: Path, monkeypatch, degraded: bool
) -> None:
    from types import SimpleNamespace

    captured = []
    lifecycle = SimpleNamespace(enabled=not degraded, degraded=degraded, finalize=lambda **_: None)
    monkeypatch.setattr(eval_module, "_start_eval_audit", lambda *_, **__: lifecycle)
    monkeypatch.setattr(
        eval_module, "run_agent_with_evaluation", lambda **kwargs: captured.append(kwargs)
    )
    args = _parse(
        "--agent-type",
        "qwen3vl",
        "--gui-ledger",
        "full",
        "--gui-ledger-ui-tree",
        "--audit-log-root",
        str(tmp_path / "audit"),
    )
    eval_module._run_evaluation_once(args=args, api_key=None)
    assert captured[0]["gui_ledger_ui_tree"] is not degraded
    assert captured[0]["gui_ledger_mode"] == ("off" if degraded else "full")


def test_audit_cli_defaults_off_and_accepts_explicit_chunk_policy() -> None:
    defaults = _parse("--agent-type", "fixture")
    assert defaults.enable_audit is False
    assert defaults.audit_log_root is None
    assert not hasattr(defaults, "audit_collector_mode")
    assert defaults.audit_store_stream_chunks is True
    assert defaults.gui_ledger_mode == "off"
    assert defaults.gui_ledger_ui_tree is False
    assert not any(name.startswith("sentinel") for name in vars(defaults))

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

    ledger = _parse(
        "--agent-type",
        "qwen3vl",
        "--gui-ledger",
        "full",
    )
    assert ledger.gui_ledger_mode == "full"
    assert ledger.gui_ledger_ui_tree is False

    for retired_arguments in (
        ("--sentinel", "active"),
        ("--sentinel-mode", "active"),
        ("--sentinel-literal-memory",),
        ("--sentinel-api-key-env", "OPENAI_API_KEY"),
        ("--sentinel-base-url", "https://provider.invalid/v1"),
    ):
        with pytest.raises(SystemExit):
            _parse("--agent-type", "fixture", *retired_arguments)
    with pytest.raises(SystemExit):
        _parse("--agent-type", "qwen3vl", "--gui-ledger", "shadow")

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


@pytest.mark.parametrize("mode", ["off", "inform", "full"])
def test_ledger_cli_never_loads_a_policy_or_constructs_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    original_import = builtins.__import__
    calls: list[dict[str, Any]] = []
    forbidden_imports: list[str] = []

    def checked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith(("mobile_world.runtime.sentinel", "openai")):
            forbidden_imports.append(name)
            raise AssertionError("evaluation wrapper must not load a policy provider")
        if mode == "off" and name.startswith("mobile_world.runtime.gui_ledger"):
            forbidden_imports.append(name)
            raise AssertionError("default-off must not initialize GUI Ledger")
        return original_import(name, *args, **kwargs)

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        calls.append(kwargs)
        return [], []

    class Lifecycle:
        enabled = True
        degraded = False

        def finalize(self, *, runtime_status: str) -> None:
            assert runtime_status == "completed"

    monkeypatch.setattr(builtins, "__import__", checked_import)
    monkeypatch.setattr(eval_module, "_start_eval_audit", lambda *args, **kwargs: Lifecycle())
    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    args = _parse(
        "--agent-type",
        "qwen3vl",
        "--gui-ledger",
        mode,
        "--audit-log-root",
        str(tmp_path / "audit"),
    )
    assert eval_module._run_evaluation_once(args=args, api_key=None) == ([], [])
    assert len(calls) == 1
    assert calls[0]["gui_ledger_mode"] == mode
    assert forbidden_imports == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode", ["inform", "full"])
@pytest.mark.parametrize(
    "arguments",
    [
        ("--agent-type", "mai_ui_agent"),
        ("--agent-type", "qwen3vl", "--enable-mcp"),
        ("--agent-type", "qwen3vl", "--enable-user-interaction"),
    ],
)
def test_ledger_rejects_unsupported_hosts_and_tasks_before_resources(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    arguments: tuple[str, ...],
) -> None:
    def reject_side_effect(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unsupported GUI Ledger request must fail before bootstrap")

    monkeypatch.setattr(eval_module, "_start_eval_audit", reject_side_effect)
    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", reject_side_effect)
    args = _parse(*arguments, "--gui-ledger", mode)
    with pytest.raises(ValueError, match="GUI Ledger"):
        eval_module._run_evaluation_once(args=args, api_key=None)
    assert args.audit_log_root is None


@pytest.mark.parametrize(
    "runner_kwargs",
    [{"agent_type": "mai_ui_agent"}, {"enable_mcp": True}, {"enable_user_interaction": True}],
)
def test_ledger_checks_effective_runner_configuration_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch, runner_kwargs: dict[str, Any]
) -> None:
    def reject_side_effect(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unsupported runner configuration must fail before bootstrap")

    monkeypatch.setattr(eval_module, "_start_eval_audit", reject_side_effect)
    args = _parse("--agent-type", "qwen3vl", "--gui-ledger", "full")
    with pytest.raises(ValueError, match="GUI Ledger"):
        eval_module._run_evaluation_once(args=args, api_key=None, **runner_kwargs)


@pytest.mark.parametrize("mode", ["inform", "full"])
@pytest.mark.asyncio
async def test_ledger_auto_enables_collector_and_records_deterministic_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        calls.append(kwargs)
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    args = _parse(
        "--agent-type",
        "qwen3vl",
        "--gui-ledger",
        mode,
        "--audit-log-root",
        str(tmp_path / "audit"),
        "--task",
        "FixtureTask",
    )
    assert args.enable_audit is False
    await eval_module.execute(args)

    assert len(calls) == 1
    assert calls[0]["gui_ledger_mode"] == mode
    assert "prompt_sentinel_runtime_factory" not in calls[0]
    lifecycle = calls[0]["audit_lifecycle"]
    config = json.loads(lifecycle.recorder.manifest_start_path.read_text())["resolved_cli_config"]
    assert config["gui_ledger_mode"] == mode
    assert config["gui_ledger_driver"] == "deterministic"
    assert config["gui_ledger_extra_model_calls"] == 0
    assert config["enable_audit"] is False
    assert config["audit_enabled"] is True
    assert not any(name.startswith("sentinel") for name in config)
    assert lifecycle.recorder.manifest_final_path.is_file()


@pytest.mark.parametrize("bootstrap_raises", [False, True])
def test_ledger_audit_failure_continues_without_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_raises: bool,
) -> None:
    def fail_bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        if bootstrap_raises:
            raise OSError("injected audit bootstrap failure")
        return eval_module.DEGRADED_AUDIT_LIFECYCLE

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        assert kwargs["audit_lifecycle"] is None
        assert kwargs["gui_ledger_mode"] == "off"
        return [], []

    monkeypatch.setattr(eval_module, "_start_eval_audit", fail_bootstrap)
    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    args = _parse(
        "--agent-type",
        "qwen3vl",
        "--gui-ledger",
        "full",
        "--audit-log-root",
        str(tmp_path / "audit"),
    )

    assert eval_module._run_evaluation_once(args=args, api_key="actor-key") == ([], [])


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
    assert calls[0]["gui_ledger_mode"] == "off"
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
