from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mobile_world.runtime.sentinel.r2_4 import live_executor
from mobile_world.runtime.sentinel.r2_4.live_executor import (
    CpuTestFaultV1,
    ExecutorStateV1,
    build_cpu_test_r24_smoke_executor_v1,
    build_production_r24_smoke_executor_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveRunContractError,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceStatusV1,
    SmokeModeV1,
    SnapshotResourceV1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
    R24SmokeOwnerAuthorizationV1,
    R24SmokeRunAuthorityManifestV1,
    R24SmokeSequenceSafetyV1,
    SequenceExecutionScopeV1,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _resource(tmp_path: Path, host: PilotHostV1, port: int) -> SnapshotResourceV1:
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    return SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=str(tmp_path / "models" / host.value / "snapshot"),
        snapshot_storage_root=str(tmp_path / "models" / host.value),
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256=_sha(host.value),
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"smoke-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )


def _plan(tmp_path: Path, host: PilotHostV1) -> HostLiveSmokePlanV1:
    return HostLiveSmokePlanV1(
        host=host,
        cases=tuple(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=str(tmp_path / "inputs" / f"{host.value}-{mode.value}.json"),
                request_fixture_sha256=_sha(f"{host.value}-{mode.value}"),
                request_fixture_byte_count=10,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=60,
                max_cost_usd_micros=100,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
            for mode in SmokeModeV1
        ),
    )


def _manifest(tmp_path: Path) -> R24SmokeRunAuthorityManifestV1:
    now = datetime.now(UTC).replace(microsecond=0)
    secret = tmp_path / "secret" / "openai.key"
    secret.parent.mkdir(parents=True)
    secret.write_text("cpu-not-read", encoding="utf-8")
    secret.chmod(0o600)
    plans = (
        _plan(tmp_path, PilotHostV1.QWEN3_VL),
        _plan(tmp_path, PilotHostV1.MAI_UI),
    )
    return R24SmokeRunAuthorityManifestV1(
        schema_version=R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
        execution_scope=SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
        run_id="r24-smoke-executor-cpu",
        source_commit="a" * 40,
        authorization=R24SmokeOwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
            authorization_id="r24-smoke-executor-owner",
            authorized_by="owner",
            issued_at_utc=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            expires_at_utc=(now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=R24SmokeSequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
            ),
            stop_on_failure=True,
            pilot_stage_forbidden=True,
            default_dry_run=True,
            arbitrary_commands_forbidden=True,
            secrets_in_logs_forbidden=True,
            repo_external_output_required=True,
        ),
        secret=SecretFileReferenceV1(
            path=str(secret),
            environment_key="OPENAI_API_KEY",
            required_mode=0o600,
            content_may_be_read_by_preflight=False,
            persist_value_or_hash=False,
        ),
        openai_stages=tuple(
            OpenAIResponsesStageV1(
                role=role,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=8192 if role is OpenAIRoleV1.RUBRIC else 4096,
                timeout_ms=30_000,
                max_attempts=1,
                store=False,
            )
            for role in (OpenAIRoleV1.RUBRIC, OpenAIRoleV1.HISTORY_POLICY)
        ),
        actor_resources=(
            _resource(tmp_path, PilotHostV1.QWEN3_VL, 18081),
            _resource(tmp_path, PilotHostV1.MAI_UI, 18082),
        ),
        smoke_plans=plans,
        resource_topology="SINGLE_GPU_SEQUENTIAL_SHARED",
        runtime_config_sha256=_sha("shared-runtime-config"),
        output_root=str(tmp_path / "smoke-output"),
        max_resource_preflight_wall_time_seconds=100,
        max_qwen_to_mai_handoff_wall_time_seconds=900,
        max_resource_cleanup_wall_time_seconds=120,
        max_sequence_wall_time_seconds=1480,
        max_sequence_openai_calls=12,
        max_sequence_actor_calls=6,
        max_sequence_cost_usd_micros=600,
    )


def _executor(
    tmp_path: Path,
    fault: CpuTestFaultV1 = CpuTestFaultV1.NONE,
) -> tuple[R24SmokeRunAuthorityManifestV1, object]:
    repository = tmp_path / "repository"
    repository.mkdir()
    manifest = _manifest(tmp_path)
    executor = build_cpu_test_r24_smoke_executor_v1(
        manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        repository_root=repository,
        fault=fault,
    )
    return manifest, executor


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _install_marker_fault(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output: Path,
    marker_name: str,
    phase: str,
) -> dict[str, bool]:
    fired = {"value": False}
    temporary_prefix = f".{marker_name}."

    if phase == "write":
        original_fdopen = os.fdopen

        class _FailingWriteStream:
            def __init__(self, stream: object) -> None:
                self._stream = stream

            def __enter__(self) -> _FailingWriteStream:
                self._stream.__enter__()
                return self

            def __exit__(self, *args: object) -> object:
                return self._stream.__exit__(*args)

            def write(self, payload: bytes) -> int:
                del payload
                fired["value"] = True
                raise OSError("injected marker write failure")

            def flush(self) -> None:
                self._stream.flush()

            def fileno(self) -> int:
                return self._stream.fileno()

        def failing_fdopen(descriptor: int, *args: object, **kwargs: object) -> object:
            stream = original_fdopen(descriptor, *args, **kwargs)
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if not fired["value"] and target.name.startswith(temporary_prefix):
                return _FailingWriteStream(stream)
            return stream

        monkeypatch.setattr(live_executor.os, "fdopen", failing_fdopen)
    elif phase == "file_fsync":
        original_fsync = os.fsync

        def failing_fsync(descriptor: int) -> None:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if not fired["value"] and target.name.startswith(temporary_prefix):
                fired["value"] = True
                raise OSError("injected marker file fsync failure")
            original_fsync(descriptor)

        monkeypatch.setattr(live_executor.os, "fsync", failing_fsync)
    elif phase in {"rename", "output_rename"}:
        original_replace = os.replace

        def failing_replace(source: object, destination: object) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            marker_rename = (
                phase == "rename"
                and source_path.name.startswith(temporary_prefix)
                and destination_path.name == marker_name
            )
            output_rename = (
                phase == "output_rename" and destination_path == output and source_path.is_dir()
            )
            if not fired["value"] and (marker_rename or output_rename):
                fired["value"] = True
                raise OSError("injected marker rename failure")
            original_replace(source, destination)

        monkeypatch.setattr(live_executor.os, "replace", failing_replace)
    elif phase in {"root_fsync", "parent_fsync"}:
        transaction_type = live_executor.AtomicR24SmokeOutputTransactionV1
        original_directory_fsync = transaction_type._fsync_directory

        def failing_directory_fsync(path: Path) -> None:
            marker_visible = (path / marker_name).is_file()
            parent_after_publish = path == output.parent and (output / marker_name).is_file()
            should_fail = (phase == "root_fsync" and marker_visible) or (
                phase == "parent_fsync" and parent_after_publish
            )
            if not fired["value"] and should_fail:
                fired["value"] = True
                raise OSError("injected marker directory fsync failure")
            original_directory_fsync(path)

        monkeypatch.setattr(
            transaction_type,
            "_fsync_directory",
            staticmethod(failing_directory_fsync),
        )
    else:
        raise AssertionError(f"unknown marker fault phase: {phase}")
    return fired


def test_smoke_executor_happy_path_is_three_stage_cleanup_bound_and_pilot_free(
    tmp_path: Path,
) -> None:
    manifest, executor = _executor(tmp_path)
    result = executor.execute(manifest)

    assert result.status is SequenceStatusV1.COMPLETE
    assert result.execution_scope is SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY
    assert result.completed_stages == manifest.safety.stages
    assert result.pilot_executed is False
    assert result.resource_cleanup_status == "SUCCEEDED"
    assert result.resource_cleanup_upper_bound_seconds == 8
    assert result.successful_output_committed is True
    assert executor.census.state is ExecutorStateV1.COMPLETE
    assert executor.census.actor_calls == 6
    assert executor.census.openai_calls == 12
    assert executor.census.actor_actions == 0
    assert executor.census.secret_leases_acquired == 2
    assert executor.census.secret_leases_closed == 2
    assert executor.census.cleanup_attempted is True
    assert executor.census.cleanup_succeeded is True
    assert not hasattr(executor._adapters, "pilot")

    output = Path(manifest.output_root)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert {path.name for path in output.iterdir()} == {
        "00-resource-preflight.json",
        "01-qwen-live-smoke.json",
        "02-mai-live-smoke.json",
        "03-resource-cleanup.json",
        "manifest-binding.json",
        "terminal.json",
    }
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())

    terminal = _load(output / "terminal.json")
    assert terminal["execution_scope"] == "R24_LIVE_SMOKE_ONLY"
    assert terminal["pilot_executed"] is False
    assert terminal["runtime_config_sha256"] == manifest.runtime_config_sha256
    cleanup_bound = terminal["resource_cleanup_upper_bound"]
    assert terminal["resource_cleanup_upper_bound_seconds"] == 8
    assert (
        terminal["resource_cleanup_upper_bound_sha256"]
        == hashlib.sha256(_canonical(cleanup_bound)).hexdigest()
    )
    assert terminal["result"]["resource_cleanup_upper_bound_seconds"] == 8
    assert terminal["result"]["resource_cleanup_upper_bound"] == cleanup_bound
    assert (
        terminal["result"]["resource_cleanup_upper_bound_sha256"]
        == terminal["resource_cleanup_upper_bound_sha256"]
    )
    assert terminal["result"]["completed_stages"] == [
        stage.value for stage in manifest.safety.stages
    ]
    assert terminal["result_sha256"] == hashlib.sha256(_canonical(terminal["result"])).hexdigest()
    for stage, filename in (
        (RunStageV1.RESOURCE_PREFLIGHT, "00-resource-preflight.json"),
        (RunStageV1.QWEN_LIVE_SMOKE, "01-qwen-live-smoke.json"),
        (RunStageV1.MAI_LIVE_SMOKE, "02-mai-live-smoke.json"),
    ):
        assert (
            terminal["stage_file_sha256s"][stage.value]
            == hashlib.sha256((output / filename).read_bytes()).hexdigest()
        )

    mai = _load(output / "02-mai-live-smoke.json")["evidence"]
    assert mai["status"] == "COMPLETED_AND_CLEANED"
    for key in ("handoff_evidence", "smoke_evidence", "resource_cleanup_evidence"):
        assert mai[f"{key}_sha256"] == hashlib.sha256(_canonical(mai[key])).hexdigest()
    cleanup = _load(output / "03-resource-cleanup.json")
    assert cleanup["resource_cleanup_evidence_sha256"] == result.resource_cleanup_evidence_sha256

    with pytest.raises(LiveRunContractError, match="EXECUTOR_STOPPED"):
        executor.run_stage(RunStageV1.R25_PILOT, manifest)


@pytest.mark.parametrize(
    ("fault", "expected_code"),
    [
        (CpuTestFaultV1.QWEN_SMOKE_FAILURE, "STAGE_ADAPTER_FAILED"),
        (CpuTestFaultV1.QWEN_TO_MAI_HANDOFF_FAILURE, "RESOURCE_HANDOFF_FAILED"),
        (CpuTestFaultV1.MAI_SMOKE_FAILURE, "STAGE_ADAPTER_FAILED"),
    ],
)
def test_smoke_executor_failure_stops_cleans_and_publishes_exact_proof(
    tmp_path: Path,
    fault: CpuTestFaultV1,
    expected_code: str,
) -> None:
    manifest, executor = _executor(tmp_path, fault)
    result = executor.execute(manifest)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failure_code == expected_code
    assert result.resource_cleanup_status == "SUCCEEDED"
    assert result.successful_output_committed is False
    assert executor.census.state is ExecutorStateV1.FAILED
    assert executor.census.cleanup_attempted is True
    assert executor.census.cleanup_succeeded is True
    assert executor.census.actor_actions == 0
    output = Path(manifest.output_root)
    assert (output / "failure.json").is_file()
    assert not (output / "terminal.json").exists()
    failure = _load(output / "failure.json")
    assert failure["result"]["pilot_executed"] is False
    assert failure["result"]["failure_code"] == expected_code
    assert (
        failure["resource_cleanup_evidence_sha256"]
        == hashlib.sha256(_canonical(failure["resource_cleanup_evidence"])).hexdigest()
    )
    if fault is CpuTestFaultV1.MAI_SMOKE_FAILURE:
        evidence = failure["stage_failure_evidence"]
        assert evidence["status"] == "MAI_SMOKE_FAILED_AFTER_HANDOFF"
        assert (
            evidence["handoff_evidence_sha256"]
            == hashlib.sha256(_canonical(evidence["handoff_evidence"])).hexdigest()
        )
        assert (
            evidence["smoke_evidence_sha256"]
            == hashlib.sha256(_canonical(evidence["smoke_evidence"])).hexdigest()
        )


def test_smoke_executor_cleanup_failure_cannot_publish_mai_or_complete(tmp_path: Path) -> None:
    manifest, executor = _executor(tmp_path, CpuTestFaultV1.RESOURCE_CLEANUP_FAILURE)
    result = executor.execute(manifest)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.MAI_LIVE_SMOKE
    assert result.failure_code == "EXECUTOR_CLEANUP_FAILED"
    assert result.completed_stages == (
        RunStageV1.RESOURCE_PREFLIGHT,
        RunStageV1.QWEN_LIVE_SMOKE,
    )
    assert result.resource_cleanup_status == "RETRY_REQUIRED"
    assert executor.census.cleanup_succeeded is False
    output = Path(manifest.output_root)
    assert not (output / "02-mai-live-smoke.json").exists()
    assert not (output / "03-resource-cleanup.json").exists()
    assert not (output / "terminal.json").exists()
    failure = _load(output / "failure.json")
    stage_evidence = failure["stage_failure_evidence"]
    assert stage_evidence["status"] == "MAI_SMOKE_COMPLETED_CLEANUP_PENDING"
    assert stage_evidence["handoff_evidence"] is not None
    assert stage_evidence["smoke_evidence"] is not None
    cleanup_evidence = failure["resource_cleanup_evidence"]
    assert cleanup_evidence["status"] == "RETRY_REQUIRED"
    assert cleanup_evidence["adapter_cleanup_evidence"] is not None


@pytest.mark.parametrize(
    ("fault", "failed_stage"),
    [
        (
            CpuTestFaultV1.QWEN_CASE_BROKER_CLOSE_FAILURE,
            RunStageV1.QWEN_LIVE_SMOKE,
        ),
        (
            CpuTestFaultV1.MAI_CASE_BROKER_CLOSE_FAILURE,
            RunStageV1.MAI_LIVE_SMOKE,
        ),
    ],
)
def test_broker_close_failure_retains_completed_smoke_evidence_before_cleanup(
    tmp_path: Path,
    fault: CpuTestFaultV1,
    failed_stage: RunStageV1,
) -> None:
    manifest, executor = _executor(tmp_path, fault)
    result = executor.execute(manifest)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is failed_stage
    assert result.failure_code == "CASE_AUTHORITY_BROKER_CLOSE_FAILED"
    assert result.resource_cleanup_status == "SUCCEEDED"
    failure = _load(Path(manifest.output_root) / "failure.json")
    evidence = failure["stage_failure_evidence"]
    if failed_stage is RunStageV1.QWEN_LIVE_SMOKE:
        assert evidence["stage"] == "QWEN_LIVE_SMOKE"
    else:
        assert evidence["status"] == "MAI_SMOKE_FAILED_AFTER_HANDOFF"
        assert (
            evidence["handoff_evidence_sha256"]
            == hashlib.sha256(_canonical(evidence["handoff_evidence"])).hexdigest()
        )
        assert (
            evidence["smoke_evidence_sha256"]
            == hashlib.sha256(_canonical(evidence["smoke_evidence"])).hexdigest()
        )
    assert (
        failure["stage_failure_evidence_sha256"] == hashlib.sha256(_canonical(evidence)).hexdigest()
    )


def test_success_commit_failure_becomes_durable_failed_mai_with_full_terminal_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, executor = _executor(tmp_path)

    def fail_commit(result: object) -> None:
        del result
        raise OSError("CPU commit fixture failed")

    monkeypatch.setattr(executor._output, "commit", fail_commit)
    result = executor.execute(manifest)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.MAI_LIVE_SMOKE
    assert result.failure_code == "OUTPUT_TRANSACTION_FAILED"
    assert result.completed_stages == manifest.safety.stages
    assert result.resource_cleanup_status == "SUCCEEDED"
    output = Path(manifest.output_root)
    assert not (output / "terminal.json").exists()
    failure = _load(output / "failure.json")
    assert failure["stage_failure_evidence"]["status"] == "COMPLETED_AND_CLEANED"
    assert (
        failure["stage_failure_evidence_sha256"]
        == hashlib.sha256(_canonical(failure["stage_failure_evidence"])).hexdigest()
    )


@pytest.mark.parametrize(
    "phase",
    (
        "write",
        "file_fsync",
        "rename",
        "root_fsync",
        "parent_fsync",
        "output_rename",
    ),
)
def test_success_terminal_publication_faults_revoke_complete_before_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    manifest, executor = _executor(tmp_path)
    output = Path(manifest.output_root)
    fired = _install_marker_fault(
        monkeypatch,
        output=output,
        marker_name="terminal.json",
        phase=phase,
    )

    result = executor.execute(manifest)

    assert fired["value"] is True
    assert result.status is SequenceStatusV1.FAILED
    assert result.failure_code == "OUTPUT_TRANSACTION_FAILED"
    assert not (output / "terminal.json").exists()
    assert not (output / "recovery.json").exists()
    assert not tuple(output.glob(".terminal.json.*.partial"))
    terminal = live_executor.AtomicR24SmokeOutputTransactionV1.read_terminal_marker(output)
    assert terminal["status"] == "FAILED"
    assert terminal["stage_failure_evidence"]["status"] == "COMPLETED_AND_CLEANED"


@pytest.mark.parametrize("revocation_phase", ("unlink", "directory_fsync"))
def test_unconfirmed_success_terminal_revocation_publishes_only_recovery_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revocation_phase: str,
) -> None:
    manifest, executor = _executor(tmp_path)
    output = Path(manifest.output_root)
    transaction_type = live_executor.AtomicR24SmokeOutputTransactionV1
    original_directory_fsync = transaction_type._fsync_directory
    commit_faulted = False
    revocation_faulted = False

    def failing_directory_fsync(path: Path) -> None:
        nonlocal commit_faulted, revocation_faulted
        if not commit_faulted and path == output and (output / "terminal.json").is_file():
            commit_faulted = True
            raise OSError("injected post-terminal-rename fsync failure")
        if (
            revocation_phase == "directory_fsync"
            and commit_faulted
            and not revocation_faulted
            and path == output
            and not (output / "terminal.json").exists()
        ):
            revocation_faulted = True
            raise OSError("injected terminal revocation fsync failure")
        original_directory_fsync(path)

    monkeypatch.setattr(
        transaction_type,
        "_fsync_directory",
        staticmethod(failing_directory_fsync),
    )
    if revocation_phase == "unlink":
        original_unlink = Path.unlink

        def failing_unlink(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal revocation_faulted
            if path == output / "terminal.json" and not revocation_faulted:
                revocation_faulted = True
                raise OSError("injected terminal revocation unlink failure")
            original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)

    with pytest.raises(LiveRunContractError, match="EXECUTOR_FAILURE_PUBLICATION_FAILED"):
        executor.execute(manifest)

    assert commit_faulted is True
    assert revocation_faulted is True
    assert not (output / "failure.json").exists()
    recovery = _load(output / "recovery.json")
    assert recovery["status"] == "SUCCESS_TERMINAL_REVOCATION_UNCONFIRMED"
    failure_envelope = recovery["failure_envelope"]
    assert (
        recovery["failure_envelope_sha256"]
        == hashlib.sha256(_canonical(failure_envelope)).hexdigest()
    )
    assert failure_envelope["stage_failure_evidence"]["status"] == "COMPLETED_AND_CLEANED"
    assert failure_envelope["resource_cleanup_evidence"] is not None
    if revocation_phase == "unlink":
        assert (output / "terminal.json").is_file()
        with pytest.raises(RuntimeError, match="ambiguous terminal markers"):
            transaction_type.read_terminal_marker(output)
    else:
        assert not (output / "terminal.json").exists()
        assert transaction_type.read_terminal_marker(output)["status"] == (
            "SUCCESS_TERMINAL_REVOCATION_UNCONFIRMED"
        )


@pytest.mark.parametrize(
    "phase",
    ("write", "file_fsync", "rename", "root_fsync", "output_rename", "parent_fsync"),
)
def test_failure_publication_recovery_retains_complete_failure_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    manifest, executor = _executor(tmp_path, CpuTestFaultV1.QWEN_SMOKE_FAILURE)
    output = Path(manifest.output_root)
    transaction_type = live_executor.AtomicR24SmokeOutputTransactionV1
    fired = _install_marker_fault(
        monkeypatch,
        output=output,
        marker_name="failure.json",
        phase=phase,
    )

    with pytest.raises(LiveRunContractError, match="EXECUTOR_FAILURE_PUBLICATION_FAILED"):
        executor.execute(manifest)

    assert fired["value"] is True
    recovery = _load(output / "recovery.json")
    failure_envelope = recovery["failure_envelope"]
    assert (
        recovery["failure_envelope_sha256"]
        == hashlib.sha256(_canonical(failure_envelope)).hexdigest()
    )
    assert failure_envelope["status"] == "FAILED"
    assert failure_envelope["result"]["failure_code"] == "STAGE_ADAPTER_FAILED"
    assert failure_envelope["stage_failure_evidence"]["stage"] == "QWEN_LIVE_SMOKE"
    assert failure_envelope["resource_cleanup_evidence"] is not None
    assert (
        failure_envelope["resource_cleanup_evidence_sha256"]
        == hashlib.sha256(_canonical(failure_envelope["resource_cleanup_evidence"])).hexdigest()
    )
    assert not tuple(output.glob(".failure.json.*.partial"))
    marker_count = sum(
        (output / name).exists() for name in ("terminal.json", "failure.json", "recovery.json")
    )
    if marker_count == 1:
        assert transaction_type.read_terminal_marker(output)["status"] == (
            "FAILURE_PUBLICATION_INCOMPLETE"
        )
    else:
        assert marker_count == 2
        with pytest.raises(RuntimeError, match="ambiguous terminal markers"):
            transaction_type.read_terminal_marker(output)


def test_smoke_executor_carves_cleanup_deadline_before_any_later_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, executor = _executor(tmp_path)
    executor.run_stage(RunStageV1.RESOURCE_PREFLIGHT, manifest)
    execution_deadline = executor._execution_deadline_monotonic_ns
    cleanup_deadline = executor._cleanup_deadline_monotonic_ns
    assert execution_deadline is not None and cleanup_deadline is not None
    assert cleanup_deadline - execution_deadline == (
        manifest.max_resource_cleanup_wall_time_seconds * 1_000_000_000
    )

    monkeypatch.setattr(live_executor.time, "monotonic_ns", lambda: execution_deadline)
    with pytest.raises(LiveRunContractError, match="SMOKE_EXECUTION_DEADLINE_EXCEEDED"):
        executor.run_stage(RunStageV1.QWEN_LIVE_SMOKE, manifest)
    result = executor.terminal_result
    assert result is not None
    assert result.completed_stages == (RunStageV1.RESOURCE_PREFLIGHT,)
    assert result.resource_cleanup_status == "SUCCEEDED"
    assert executor.census.secret_leases_acquired == 0


def test_smoke_executor_rejects_pilot_before_output_or_any_adapter_call(tmp_path: Path) -> None:
    manifest, executor = _executor(tmp_path)
    with pytest.raises(LiveRunContractError, match="R25_STAGE_FORBIDDEN"):
        executor.run_stage(RunStageV1.R25_PILOT, manifest)
    assert executor.census.state is ExecutorStateV1.READY
    assert executor.census.completed_stages == ()
    assert executor.census.cleanup_attempted is False
    assert executor.census.actor_actions == 0
    assert not Path(manifest.output_root).exists()
    assert not hasattr(executor._adapters, "pilot")


def test_smoke_executor_pilot_injection_after_prepare_cleans_and_fails_closed(
    tmp_path: Path,
) -> None:
    manifest, executor = _executor(tmp_path)
    executor.run_stage(RunStageV1.RESOURCE_PREFLIGHT, manifest)
    with pytest.raises(LiveRunContractError, match="R25_STAGE_FORBIDDEN"):
        executor.run_stage(RunStageV1.R25_PILOT, manifest)
    result = executor.terminal_result
    assert result is not None
    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.QWEN_LIVE_SMOKE
    assert result.failure_code == "R25_STAGE_FORBIDDEN"
    assert result.resource_cleanup_status == "SUCCEEDED"
    assert executor.census.cleanup_succeeded is True
    assert executor.census.secret_leases_acquired == 0
    assert executor.census.actor_actions == 0
    failure = _load(Path(manifest.output_root) / "failure.json")
    assert failure["stage_failure_evidence"]["attempted_stage"] == "R25_PILOT"
    assert not hasattr(executor._adapters, "pilot")


def test_smoke_executor_rejects_runtime_and_manifest_drift_before_resource_work(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    repository = tmp_path / "repository"
    repository.mkdir()
    digest = smoke_authority_manifest_sha256(manifest)
    cleanup_bound_preimage = _canonical(
        {
            "domain": "cpu-test-resource-cleanup-bound",
            "schema_version": (
                "mobileworld.runtime.sentinel-r2.4-cpu-test-resource-cleanup-bound/v1"
            ),
            "value": {"cleanup_upper_bound_seconds": 8, "manifest_sha256": digest},
        }
    )
    with pytest.raises(ValueError, match="runtime config"):
        live_executor._R24SmokeExecutorCoreV1(
            manifest,
            confirmed_manifest_sha256=digest,
            preflight_report_sha256="1" * 64,
            factory_binding_sha256="2" * 64,
            confirmed_runtime_config_sha256="3" * 64,
            resource_cleanup_upper_bound_seconds=8,
            resource_cleanup_upper_bound_preimage=cleanup_bound_preimage,
            resource_cleanup_upper_bound_sha256=hashlib.sha256(cleanup_bound_preimage).hexdigest(),
            repository_root=repository,
            adapters=object(),
        )
    executor = build_cpu_test_r24_smoke_executor_v1(
        manifest,
        confirmed_manifest_sha256=digest,
        repository_root=repository,
    )
    drifted = replace(manifest, run_id="r24-smoke-executor-drift")
    with pytest.raises(LiveRunContractError, match="MANIFEST_BINDING_MISMATCH"):
        executor.run_stage(RunStageV1.RESOURCE_PREFLIGHT, drifted)
    assert executor.census.state is ExecutorStateV1.READY
    assert not Path(manifest.output_root).exists()


@pytest.mark.parametrize(
    "scenario",
    ("driver", "broker", "cleanup_reserve_277", "cleanup_reserve_278"),
)
def test_production_smoke_builder_cross_binds_factory_and_cleanup_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    from mobile_world.runtime.sentinel.r2_4.production_driver import (
        PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
        ProductionCaseAuthorityBrokerProviderV1,
        ProductionDriverAdaptersV1,
        ProductionResourceLifecycleAdapterV1,
    )
    from mobile_world.runtime.sentinel.r2_4.production_preflight import (
        ProductionPostPreflightFactoryV1,
    )

    manifest = _manifest(tmp_path)
    if scenario.startswith("cleanup_reserve_"):
        reserve = int(scenario.rpartition("_")[2])
        manifest = replace(
            manifest,
            max_resource_cleanup_wall_time_seconds=reserve,
            max_sequence_wall_time_seconds=(
                manifest.max_sequence_wall_time_seconds
                - manifest.max_resource_cleanup_wall_time_seconds
                + reserve
            ),
        )
    manifest_sha256 = smoke_authority_manifest_sha256(manifest)
    repository = tmp_path / "repository"
    repository.mkdir()

    def factory(binding: str) -> ProductionPostPreflightFactoryV1:
        value = object.__new__(ProductionPostPreflightFactoryV1)
        object.__setattr__(value, "_manifest", manifest)
        object.__setattr__(value, "_manifest_sha256", manifest_sha256)
        object.__setattr__(
            value,
            "_sequence_execution_scope",
            SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
        )
        object.__setattr__(value, "_runtime_config_sha256", manifest.runtime_config_sha256)
        object.__setattr__(value, "_preflight_report_sha256", _sha("preflight-A"))
        object.__setattr__(value, "_factory_binding_sha256", _sha(binding))
        return value

    factory_a = factory("factory-A")
    factory_b = factory("factory-B-different-pricing-root")
    resource = object.__new__(ProductionResourceLifecycleAdapterV1)
    driver = object.__new__(ProductionDriverAdaptersV1)
    driver_factory = factory_b if scenario == "driver" else factory_a
    object.__setattr__(
        driver,
        "_port",
        SimpleNamespace(_factory=driver_factory, _resource_lifecycle=resource),
    )
    object.__setattr__(driver, "_smoke", object())
    object.__setattr__(driver, "_pilot", object())
    broker = object.__new__(ProductionCaseAuthorityBrokerProviderV1)
    object.__setattr__(
        broker,
        "_factory",
        factory_b if scenario == "broker" else factory_a,
    )
    cleanup_projection = {
        "admitted_backend_cleanup_upper_bound_seconds": 45,
        "admitted_model_cleanup_upper_bound_seconds": 53,
        "backend_cleanup_upper_bound_seconds": 105,
        "cleanup_upper_bound_seconds": 278,
        "docker_command_timeout_seconds": 15,
        "final_shared_gpu_attestation_command_slots": 4,
        "final_shared_gpu_attestation_upper_bound_seconds": 120,
        "health_poll_interval_ceiling_seconds": 1,
        "health_poll_interval_ms": 250,
        "model_cleanup_upper_bound_seconds": 53,
        "model_leader_wait_slots": 2,
        "model_poll_overshoot_slots": 3,
        "model_port_wait_slots": 1,
        "model_session_wait_slots": 2,
        "nvidia_attestation_command_timeout_seconds": 30,
        "partial_model_cleanup_upper_bound_seconds": 32,
        "pending_backend_cleanup_command_slots": 7,
        "pending_backend_cleanup_upper_bound_seconds": 105,
        "resource_topology": "SINGLE_GPU_SEQUENTIAL_SHARED",
        "runtime_config_sha256": manifest.runtime_config_sha256,
        "shutdown_grace_seconds": 10,
    }
    cleanup_preimage = _canonical(
        {
            "domain": "production-resource-cleanup-bound",
            "schema_version": PRODUCTION_RESOURCE_CLEANUP_BOUND_SCHEMA_VERSION_V1,
            "value": cleanup_projection,
        }
    )
    reads = {
        "bound": 0,
        "preimage": 0,
        "sha256": 0,
        "prepare": 0,
        "broker": 0,
        "driver": 0,
    }

    def cleanup_bound(_: object) -> int:
        reads["bound"] += 1
        return 278

    def cleanup_bound_preimage(_: object) -> bytes:
        reads["preimage"] += 1
        return cleanup_preimage

    def cleanup_bound_sha256(_: object) -> str:
        reads["sha256"] += 1
        return hashlib.sha256(cleanup_preimage).hexdigest()

    def prepare_spy(*args: object, **kwargs: object) -> object:
        del args, kwargs
        reads["prepare"] += 1
        raise AssertionError("resource I/O must not run during executor construction")

    def broker_spy(*args: object, **kwargs: object) -> object:
        del args, kwargs
        reads["broker"] += 1
        raise AssertionError("broker must not run during executor construction")

    def driver_spy(*args: object, **kwargs: object) -> object:
        del args, kwargs
        reads["driver"] += 1
        raise AssertionError("driver must not run during executor construction")

    monkeypatch.setattr(
        ProductionResourceLifecycleAdapterV1,
        "runtime_config_sha256",
        property(lambda _: manifest.runtime_config_sha256),
    )
    monkeypatch.setattr(
        ProductionResourceLifecycleAdapterV1,
        "cleanup_upper_bound_seconds",
        property(cleanup_bound),
    )
    monkeypatch.setattr(
        ProductionResourceLifecycleAdapterV1,
        "cleanup_upper_bound_preimage",
        property(cleanup_bound_preimage),
    )
    monkeypatch.setattr(
        ProductionResourceLifecycleAdapterV1,
        "cleanup_upper_bound_sha256",
        property(cleanup_bound_sha256),
    )
    monkeypatch.setattr(ProductionResourceLifecycleAdapterV1, "prepare", prepare_spy)
    monkeypatch.setattr(ProductionCaseAuthorityBrokerProviderV1, "acquire", broker_spy)
    object.__setattr__(driver, "_smoke", SimpleNamespace(run_host=driver_spy))
    monkeypatch.setattr(
        ProductionDriverAdaptersV1,
        "resource_lifecycle",
        property(lambda _: resource),
    )

    arguments = {
        "confirmed_manifest_sha256": manifest_sha256,
        "confirmed_runtime_config_sha256": manifest.runtime_config_sha256,
        "repository_root": repository,
        "post_preflight_factory": factory_a,
        "resource_adapter": resource,
        "driver_adapters": driver,
        "case_authority_broker_provider": broker,
    }
    if scenario == "cleanup_reserve_278":
        executor = build_production_r24_smoke_executor_v1(
            manifest,
            **arguments,
        )
        binding = executor._output._binding
        assert binding["resource_cleanup_upper_bound"] == json.loads(cleanup_preimage)
        assert binding["resource_cleanup_upper_bound_seconds"] == 278
        assert (
            binding["resource_cleanup_upper_bound_sha256"]
            == hashlib.sha256(cleanup_preimage).hexdigest()
        )
    else:
        expected_error = (
            "INSUFFICIENT_RESOURCE_CLEANUP_RESERVE"
            if scenario == "cleanup_reserve_277"
            else "SMOKE_FACTORY_COMPONENT_BINDING_MISMATCH"
        )
        with pytest.raises(LiveRunContractError, match=expected_error):
            build_production_r24_smoke_executor_v1(manifest, **arguments)

    assert not Path(manifest.output_root).exists()
    assert reads["prepare"] == reads["broker"] == reads["driver"] == 0
    if scenario.startswith("cleanup_reserve_"):
        assert reads == {
            "bound": 1,
            "preimage": 1,
            "sha256": 1,
            "prepare": 0,
            "broker": 0,
            "driver": 0,
        }
    else:
        assert reads == {
            "bound": 0,
            "preimage": 0,
            "sha256": 0,
            "prepare": 0,
            "broker": 0,
            "driver": 0,
        }
