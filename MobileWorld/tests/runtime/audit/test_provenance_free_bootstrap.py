from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import mobile_world.runtime.audit.lifecycle as lifecycle_module
from mobile_world.core.subcommands import eval as eval_module
from mobile_world.runtime.audit.lifecycle import AuditLifecycle


def test_active_sentinel_eval_bootstraps_without_git_or_upstream_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "installed-src"
    source_root.mkdir()
    audit_root = tmp_path / "audit"
    monkeypatch.setenv("FIXTURE_SENTINEL_KEY", "fixture-secret")
    monkeypatch.setattr(lifecycle_module, "_default_source_root", lambda: source_root)
    original_subprocess_run = subprocess.run

    def reject_git(command: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(command, (list, tuple)) and command and command[0] == "git":
            raise AssertionError("eval bootstrap attempted git status")
        return original_subprocess_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", reject_git)
    captured: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> tuple[list[Any], list[Any]]:
        captured.update(kwargs)
        return [], []

    monkeypatch.setattr(eval_module, "run_agent_with_evaluation", fake_runner)
    args = argparse.Namespace(
        sentinel_mode="active",
        sentinel_api_key_env="FIXTURE_SENTINEL_KEY",
        sentinel_base_url="https://example.invalid/v1",
        enable_audit=False,
        audit_log_root=str(audit_root),
        audit_store_stream_chunks=True,
        agent_type="fixture",
        model_name="fixture-model",
        suite_family="mobile_world",
        env_image=None,
        executor_llm_base_url=None,
        executor_model_name=None,
        executor_agent_class=None,
        scale_factor=1000,
    )

    assert eval_module._run_evaluation_once(args=args, api_key="actor-secret") == ([], [])
    lifecycle = captured["audit_lifecycle"]
    assert isinstance(lifecycle, AuditLifecycle)
    start = json.loads(lifecycle.recorder.manifest_start_path.read_text(encoding="utf-8"))
    final = json.loads(lifecycle.recorder.manifest_final_path.read_text(encoding="utf-8"))
    assert start["git_commit"] is None
    assert start["git_dirty"] is None
    assert start["mobile_world_snapshot"] == {
        "path": "MobileWorld",
        "upstream_repository_url": None,
        "upstream_commit": None,
        "provenance_file": None,
    }
    assert final["capture_complete"] is True
    assert captured["prompt_sentinel_runtime_factory"] is not None
