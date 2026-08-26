from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import mobile_world.runtime.audit.lifecycle as lifecycle_module
from mobile_world.runtime.audit.config import AuditConfig
from mobile_world.runtime.audit.integrity import check_run_integrity
from mobile_world.runtime.audit.lifecycle import (
    DEGRADED_AUDIT_LIFECYCLE,
    NULL_AUDIT_LIFECYCLE,
    AuditLifecycle,
    TaskAuditBinding,
    bootstrap_audit_run,
    detect_repository_dirty,
)

_MONOREPO_COMMIT = "b" * 40
_UPSTREAM_COMMIT = "0dcd0980eac64d76f498f93568a1ec0594b743c4"


class _Agent:
    model_name = "fixture-model"


class _Environment:
    base_url = "http://127.0.0.1:5000/private/path?token=not-persisted"
    device = "emulator-fixture"


class _PoisonMapping(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise AssertionError(f"disabled lifecycle inspected key {key}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("disabled lifecycle iterated metadata")

    def __len__(self) -> int:
        raise AssertionError("disabled lifecycle measured metadata")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "checkout"
    git_dir = repository / ".git"
    ref = git_dir / "refs" / "heads" / "main"
    ref.parent.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    ref.write_text(f"{_MONOREPO_COMMIT}\n", encoding="ascii")
    (repository / "UPSTREAM.md").write_text(
        "\n".join(
            [
                "# Upstream source provenance",
                "",
                "## MobileWorld",
                "",
                "- Upstream repository: `https://github.com/Tongyi-MAI/MobileWorld.git`",
                f"- Imported commit: `{_UPSTREAM_COMMIT}`",
                "",
                "## Another snapshot",
            ]
        ),
        encoding="utf-8",
    )
    return repository


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [(b"", False), (b" M MobileWorld/example.py\0", True)],
)
def test_detect_repository_dirty_uses_read_only_git_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    expected: bool,
) -> None:
    repository = _repository(tmp_path)

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command == [
            "git",
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
        ]
        assert kwargs["cwd"] == repository
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["check"] is False
        assert kwargs["timeout"] == 5.0
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(lifecycle_module.subprocess, "run", fake_run)

    assert detect_repository_dirty(repository) is expected


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(cmd="git status", timeout=5.0),
        OSError("fixture git executable failure"),
    ],
)
def test_detect_repository_dirty_faults_are_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    repository = _repository(tmp_path)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise failure

    monkeypatch.setattr(lifecycle_module.subprocess, "run", fail_run)

    assert detect_repository_dirty(repository) is None


def test_detect_repository_dirty_nonzero_exit_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)

    def failed_status(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 128, stdout=b"", stderr=b"")

    monkeypatch.setattr(lifecycle_module.subprocess, "run", failed_status)

    assert detect_repository_dirty(repository) is None


def _bootstrap(
    tmp_path: Path,
    *,
    repository_dirty: bool | None = False,
    store_stream_chunks: bool = True,
    configured_secrets: tuple[str, ...] = (),
    cli_config: Mapping[str, Any] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
) -> AuditLifecycle:
    repository = _repository(tmp_path)
    lifecycle = bootstrap_audit_run(
        AuditConfig(
            enabled=True,
            log_root=tmp_path / "audit-data",
            store_stream_chunks=store_stream_chunks,
        ),
        repository_root=repository,
        repository_dirty=repository_dirty,
        resolved_cli_config=cli_config,
        resolved_agent_runtime_config=runtime_config,
        agent_type="fixture-agent",
        model_name="fixture-model",
        environment_image="fixture-image",
        configured_secrets=configured_secrets,
        sync=False,
    )
    assert isinstance(lifecycle, AuditLifecycle)
    return lifecycle


def _binding(lifecycle: AuditLifecycle, attempt: int = 1) -> TaskAuditBinding:
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent=_Agent(),
        environment=_Environment(),
        whole_task_attempt_index=attempt,
    )
    assert binding is not None
    event = binding.capture.start_task(
        task_name="FixtureTask",
        task_goal="perform the exact fixture task",
        task_goal_status="resolved",
        task_index=binding.metadata.task_index,
        suite_family=binding.metadata.suite_family,
        agent=binding.metadata.agent,
        environment=binding.metadata.environment,
        whole_task_attempt_index=binding.metadata.whole_task_attempt_index,
    )
    assert event is not None
    return binding


def _end_binding(
    lifecycle: AuditLifecycle,
    binding: TaskAuditBinding,
    *,
    completed: bool,
    retry_planned: bool = False,
) -> None:
    exception = None if completed else RuntimeError("fixture runtime failure")
    ended = binding.capture.end_task(
        runtime_status="completed" if completed else "crashed",
        termination_source="fixture_complete" if completed else "uncaught_exception",
        final_step_index=0,
        termination_exception=exception,
        score=1.0 if completed else None,
    )
    assert ended is not None
    lifecycle.finish_task_attempt(
        binding=binding,
        result=(0, 1.0) if completed else None,
        exception=exception,
        retry_planned=retry_planned,
        runtime_status="completed" if completed else "crashed",
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_disabled_bootstrap_returns_before_metadata_ids_or_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled lifecycle performed enabled bootstrap work")

    monkeypatch.setattr(lifecycle_module, "_find_repository_root", fail)
    monkeypatch.setattr(lifecycle_module, "RunRecorder", fail)
    destination = tmp_path / "must-not-exist"

    lifecycle = bootstrap_audit_run(
        AuditConfig(),
        repository_root=_PoisonMapping(),  # type: ignore[arg-type]
        resolved_cli_config=_PoisonMapping(),
        configured_secrets=_PoisonMapping(),  # type: ignore[arg-type]
    )

    assert lifecycle is NULL_AUDIT_LIFECYCLE
    assert lifecycle.start_task_attempt(poison=_PoisonMapping()) is None
    assert lifecycle.finish_task_attempt(poison=_PoisonMapping()) is None
    assert lifecycle.finalize(poison=_PoisonMapping()) is None
    assert not destination.exists()


def test_fail_open_bootstrap_storage_error_degrades_without_blocking_eval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)

    def fail_recorder(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture storage unavailable")

    monkeypatch.setattr(lifecycle_module, "RunRecorder", fail_recorder)
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit-data"),
        repository_root=repository,
        repository_dirty=False,
    )
    assert lifecycle is DEGRADED_AUDIT_LIFECYCLE
    assert lifecycle.enabled is False
    assert lifecycle.degraded is True
    assert lifecycle.capture_complete is False
    assert lifecycle.missing_artifacts == ("audit_bootstrap",)


def test_preflight_failure_degrades_before_creating_audit_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    audit_root = tmp_path / "audit-data"

    def fail_upstream(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture provenance read failure")

    monkeypatch.setattr(lifecycle_module, "_read_mobileworld_upstream", fail_upstream)
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=audit_root),
        repository_root=repository,
        repository_dirty=False,
    )

    assert lifecycle is DEGRADED_AUDIT_LIFECYCLE
    assert not audit_root.exists()


def test_repository_internal_audit_root_degrades_without_writing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    audit_root = repository / "forbidden-audit-data"

    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=audit_root),
        repository_root=repository,
        repository_dirty=False,
    )

    assert lifecycle is DEGRADED_AUDIT_LIFECYCLE
    assert not audit_root.exists()


def test_runtime_bootstrap_forces_per_event_sync_off(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit-data"),
        repository_root=repository,
        repository_dirty=False,
        sync=True,
    )

    assert isinstance(lifecycle, AuditLifecycle)
    assert lifecycle.recorder._sync is False
    lifecycle.close()


def test_manifest_separates_monorepo_head_from_mobileworld_upstream_and_scrubs_secrets(
    tmp_path: Path,
) -> None:
    api_secret = "api-secret-value"
    executor_secret = "executor-secret-value"
    url_password = "url-password-value"
    query_secret = "signed-query-value"
    lifecycle = _bootstrap(
        tmp_path,
        configured_secrets=(api_secret,),
        cli_config={
            "api_key": api_secret,
            "llm_base_url": (
                f"https://user:{url_password}@models.example/v1?api_key={query_secret}"
            ),
            "note": f"prefix-{api_secret}-suffix",
            "max_step": 3,
        },
        runtime_config={"executor_api_key": executor_secret, "scale_factor": 1000},
    )
    binding = _binding(lifecycle)
    assert {api_secret, executor_secret, url_password, query_secret}.issubset(
        set(binding.capture.configured_secrets)
    )
    _end_binding(lifecycle, binding, completed=True)
    final_path = lifecycle.finalize()
    assert final_path is not None

    start = _read_json(lifecycle.recorder.manifest_start_path)
    assert start["repository"] == "pockyitachi/AgentSentinel"
    assert start["git_commit"] == _MONOREPO_COMMIT
    assert start["monorepo"]["commit"] == _MONOREPO_COMMIT
    assert start["mobile_world_snapshot"] == {
        "path": "MobileWorld",
        "upstream_repository_url": "https://github.com/Tongyi-MAI/MobileWorld.git",
        "upstream_commit": _UPSTREAM_COMMIT,
        "provenance_file": "UPSTREAM.md",
    }
    assert start["resolved_cli_config"]["llm_base_url"] == "https://models.example"
    assert "api_key" not in start["resolved_cli_config"]
    assert "executor_api_key" not in start["resolved_agent_runtime_config"]
    assert start["resolved_cli_config"]["note"] == "[REDACTED_CONFIGURED_SECRET]"
    assert start["provider_sdk_configuration"]["actor"] == {
        "timeout_seconds": 120.0,
        "max_retries": 2,
        "max_retries_source": "openai_sdk_default",
    }
    assert start["provider_sdk_configuration"]["transparent_http_attempts_observable"] is False

    evidence = b"".join(
        path.read_bytes() for path in lifecycle.recorder.run_root.rglob("*") if path.is_file()
    )
    for secret in (api_secret, executor_secret, url_password, query_secret):
        assert secret.encode() not in evidence

    report = check_run_integrity(
        lifecycle.recorder.run_root,
        configured_secrets=(api_secret, executor_secret, url_password, query_secret),
    )
    assert report["valid"] is True, report["errors"]


@pytest.mark.parametrize("placeholder", ["empty", "EMPTY", "EmPtY", b"EMPTY"])
def test_local_api_key_placeholder_is_not_registered_as_a_configured_secret(
    placeholder: str | bytes,
) -> None:
    safe, excluded, discovered = lifecycle_module.sanitize_collector_config(
        {"api_key": placeholder, "note": "harmless EMPTY marker"},
        configured_secrets=(placeholder,),
        root_path="resolved_cli_config",
    )

    assert safe == {"note": "harmless EMPTY marker"}
    assert excluded == ["resolved_cli_config.api_key"]
    assert discovered == ()
    assert lifecycle_module._normalize_configured_secrets((placeholder,)) == ()


@pytest.mark.parametrize(
    "value",
    [" empty", "empty ", "xEMPTY", "EMPTYx", "EMPTY-extra", b"EMPTY\0"],
)
def test_near_placeholder_values_remain_configured_secrets(value: str | bytes) -> None:
    assert lifecycle_module._normalize_configured_secrets((value,)) == (value,)


def test_direct_manifest_and_task_metadata_fields_scrub_configured_secrets(
    tmp_path: Path,
) -> None:
    secret = "direct-metadata-secret"
    repository = _repository(tmp_path)
    lifecycle = bootstrap_audit_run(
        AuditConfig(enabled=True, log_root=tmp_path / "audit-data"),
        repository_root=repository,
        repository=f"repo-{secret}",
        repository_url=f"https://user:{secret}@example.invalid/project?token=query-secret",
        repository_dirty=False,
        agent_type=f"agent-{secret}",
        model_name=f"model-{secret}",
        suite_family=f"suite-{secret}",
        environment_image=f"image-{secret}",
        configured_secrets=(secret,),
        sync=False,
    )
    assert isinstance(lifecycle, AuditLifecycle)
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent={
            "adapter": f"adapter-{secret}",
            "model": f"task-model-{secret}",
            "api_key": secret,
        },
        environment={
            "base_url": f"https://user:{secret}@backend.invalid/private?token=hidden",
            "device_id": f"device-{secret}",
            "authorization": f"Bearer {secret}",
        },
        whole_task_attempt_index=1,
    )
    assert binding is not None
    started = binding.capture.start_task(
        task_name="FixtureTask",
        task_goal="safe goal",
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent=binding.metadata.agent,
        environment=binding.metadata.environment,
        whole_task_attempt_index=1,
    )
    assert started is not None
    _end_binding(lifecycle, binding, completed=True)
    final_path = lifecycle.finalize()
    assert final_path is not None

    start = _read_json(lifecycle.recorder.manifest_start_path)
    assert start["repository"] == "repo-[REDACTED_CONFIGURED_SECRET]"
    assert start["repository_url"] == "https://example.invalid/project"
    assert start["agent_type"] == "agent-[REDACTED_CONFIGURED_SECRET]"
    assert start["model_name"] == "model-[REDACTED_CONFIGURED_SECRET]"
    assert start["suite_family"] == "suite-[REDACTED_CONFIGURED_SECRET]"
    assert start["environment_image"] == "image-[REDACTED_CONFIGURED_SECRET]"
    assert "api_key" not in binding.metadata.agent
    assert "authorization" not in binding.metadata.environment
    evidence = b"".join(
        path.read_bytes() for path in lifecycle.recorder.run_root.rglob("*") if path.is_file()
    )
    assert secret.encode() not in evidence
    assert b"query-secret" not in evidence
    assert b"hidden" not in evidence


def test_whole_task_retry_gets_distinct_stream_and_final_counts(tmp_path: Path) -> None:
    lifecycle = _bootstrap(tmp_path)
    first = _binding(lifecycle, attempt=1)
    _end_binding(lifecycle, first, completed=False, retry_planned=True)
    second = _binding(lifecycle, attempt=2)
    _end_binding(lifecycle, second, completed=True)

    final_path = lifecycle.finalize()
    assert final_path is not None
    assert first.metadata.task_run_id != second.metadata.task_run_id
    run_events = _events(lifecycle.recorder.run_root / "run.events.jsonl")
    assert [event["event_type"] for event in run_events] == ["run_started", "run_ended"]
    assert run_events[-1]["payload"]["task_counts"] == {
        "started": 2,
        "completed": 1,
        "crashed": 1,
    }

    final = _read_json(final_path)
    assert [item["retry_planned"] for item in final["task_streams"]] == [True, False]
    for item in final["task_streams"]:
        stream = lifecycle.recorder.run_root / item["relative_path"]
        data = stream.read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["byte_count"] == len(data)


def test_explicit_aborted_status_overrides_successful_python_return(tmp_path: Path) -> None:
    lifecycle = _bootstrap(tmp_path)
    binding = _binding(lifecycle)
    ended = binding.capture.end_task(
        runtime_status="aborted",
        termination_source="prediction_none",
        final_step_index=1,
        score=0.0,
    )
    assert ended is not None
    lifecycle.finish_task_attempt(
        binding=binding,
        result=(1, 0.0),
        exception=None,
        retry_planned=False,
        runtime_status="aborted",
    )

    final_path = lifecycle.finalize()
    assert final_path is not None
    final = _read_json(final_path)
    assert final["task_streams"][0]["runtime_status"] == "aborted"
    assert final["task_streams"][0]["retry_planned"] is False
    run_ended = _events(lifecycle.recorder.run_root / "run.events.jsonl")[-1]
    assert run_ended["payload"]["task_counts"] == {
        "started": 1,
        "completed": 0,
        "crashed": 1,
    }


def test_requested_agent_metadata_can_bind_before_agent_construction(tmp_path: Path) -> None:
    lifecycle = _bootstrap(tmp_path)
    requested_agent = {
        "adapter": "requested-adapter",
        "model": "requested-model",
        "configuration": {},
    }
    binding = lifecycle.start_task_attempt(
        task_name="FixtureTask",
        task_index=1,
        suite_family="mobile_world",
        agent=requested_agent,
        environment=_Environment(),
        whole_task_attempt_index=1,
    )
    assert binding is not None
    assert binding.metadata.agent == requested_agent
    started = binding.capture.start_task(
        task_name="FixtureTask",
        task_goal=None,
        task_goal_status="retrieval_failed",
        task_index=binding.metadata.task_index,
        suite_family=binding.metadata.suite_family,
        agent=binding.metadata.agent,
        environment=binding.metadata.environment,
        whole_task_attempt_index=binding.metadata.whole_task_attempt_index,
    )
    assert started is not None
    ended = binding.capture.end_task(
        runtime_status="crashed",
        termination_source="agent_construction",
        final_step_index=0,
        termination_exception=RuntimeError("fixture construction failure"),
    )
    assert ended is not None
    lifecycle.finish_task_attempt(
        binding=binding,
        result=None,
        exception=RuntimeError("fixture construction failure"),
        retry_planned=False,
        runtime_status="crashed",
    )
    final_path = lifecycle.finalize()
    assert final_path is not None
    final = _read_json(final_path)
    assert final["task_streams"][0]["runtime_status"] == "crashed"


def test_run_scoped_attempt_index_remains_monotonic_when_caller_hint_resets(
    tmp_path: Path,
) -> None:
    lifecycle = _bootstrap(tmp_path)
    bindings = [_binding(lifecycle, attempt=1) for _ in range(3)]
    assert [binding.metadata.whole_task_attempt_index for binding in bindings] == [1, 2, 3]
    for binding in bindings:
        _end_binding(lifecycle, binding, completed=True)
    assert lifecycle.finalize() is not None


def test_unknown_dirty_state_is_factual_and_marks_only_run_incomplete(
    tmp_path: Path,
) -> None:
    lifecycle = _bootstrap(tmp_path, repository_dirty=None)
    final_path = lifecycle.finalize()
    assert final_path is not None

    start = _read_json(lifecycle.recorder.manifest_start_path)
    final = _read_json(final_path)
    run_ended = _events(lifecycle.recorder.run_root / "run.events.jsonl")[-1]
    assert start["git_dirty"] is None
    assert start["git_dirty_status"] == "not_checked"
    assert final["capture_complete"] is False
    assert final["missing_artifacts"] == ["repository_dirty_state"]
    assert run_ended["payload"]["capture_complete"] is False


def test_disabling_chunk_storage_propagates_and_marks_task_and_run_incomplete(
    tmp_path: Path,
) -> None:
    lifecycle = _bootstrap(tmp_path, store_stream_chunks=False)
    binding = _binding(lifecycle)
    assert binding.store_stream_chunks is False
    assert binding.metadata.store_stream_chunks is False
    assert binding.capture.capture_complete is False
    assert binding.task_recorder.missing_artifacts == ("model_stream_chunks",)
    _end_binding(lifecycle, binding, completed=True)
    final_path = lifecycle.finalize()
    assert final_path is not None

    task_ended = _events(binding.task_recorder.path)[-1]
    final = _read_json(final_path)
    assert task_ended["payload"]["capture_complete"] is False
    assert task_ended["payload"]["missing_artifacts"] == ["model_stream_chunks"]
    assert final["capture_complete"] is False
    assert final["missing_artifacts"] == ["model_stream_chunks"]


def test_task_attempt_open_and_close_faults_are_fail_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _bootstrap(tmp_path)

    def fail_open_task(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture task stream open failure")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.recorder, "open_task", fail_open_task)
        assert (
            lifecycle.start_task_attempt(
                task_name="FixtureTask",
                task_index=1,
                suite_family="mobile_world",
                agent=_Agent(),
                environment=_Environment(),
                whole_task_attempt_index=1,
            )
            is None
        )

    binding = _binding(lifecycle, attempt=2)
    ended = binding.capture.end_task(
        runtime_status="completed",
        termination_source="fixture_complete",
        final_step_index=0,
        score=1.0,
    )
    assert ended is not None

    def fail_task_close() -> None:
        raise OSError("fixture task stream close failure")

    with monkeypatch.context() as patch:
        patch.setattr(binding.task_recorder, "close", fail_task_close)
        assert (
            lifecycle.finish_task_attempt(
                binding=binding,
                result=(0, 1.0),
                exception=None,
                retry_planned=False,
                runtime_status="completed",
            )
            is None
        )

    assert "task_stream_close" in binding.task_recorder.missing_artifacts
    lifecycle.close()


def test_finalize_and_close_storage_faults_never_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle = _bootstrap(tmp_path)

    def fail_final_manifest(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture final manifest failure")

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.recorder, "write_manifest_final", fail_final_manifest)
        assert lifecycle.finalize() is None

    second = _bootstrap(tmp_path / "second")

    def fail_close() -> None:
        raise OSError("fixture close failure")

    with monkeypatch.context() as patch:
        patch.setattr(second.recorder, "close", fail_close)
        final_path = second.finalize()
        assert final_path is not None
        assert final_path.is_file()
        assert second.close() is None
    second.close()


def test_invalid_finalize_status_is_fail_open(tmp_path: Path) -> None:
    lifecycle = _bootstrap(tmp_path)

    assert lifecycle.finalize(runtime_status="invalid") is None
    assert lifecycle.finalize(runtime_status="completed") is not None
