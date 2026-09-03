from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from mobile_world.runtime.sentinel.r2_4.live_executor import (
    PRODUCTION_ROOT_HOOKS_V1,
    CpuTestFaultV1,
    CpuTestR24R25ExecutorV1,
    ExecutorStateV1,
    ProductionR24R25ExecutorV1,
    UnavailableSecureSecretLeaseProviderV1,
    build_cpu_test_executor_v1,
    production_executor_available_v1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveRunContractError,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    OwnerAuthorizationV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceRunResultV1,
    SequenceSafetyV1,
    SequenceStageExecutorV1,
    SequenceStatusV1,
    SmokeModeV1,
    SnapshotResourceV1,
    authority_manifest_sha256,
    run_authorized_sequence_with_executor,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    FrozenPilotManifestV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _pilot(tmp_path: Path) -> FrozenPilotManifestV1:
    tasks = tuple(
        PilotTaskV1(
            task_id=f"task-{index:02d}",
            task_parameters_sha256=_sha(f"parameters-{index}"),
            reset_seed=10_000 + index,
        )
        for index in range(20)
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="cpu-executor-pilot",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path=str(tmp_path / "inputs" / "tasks.json"),
        task_manifest_sha256=_sha("task-manifest"),
        task_manifest_byte_count=100,
        topology_comparison_artifact_path=str(tmp_path / "inputs" / "topology.json"),
        topology_comparison_artifact_sha256="0" * 64,
        topology_comparison_artifact_byte_count=1,
        cohort_selection_artifact_path=str(tmp_path / "inputs" / "cohort-selection.json"),
        cohort_selection_artifact_sha256="1" * 64,
        cohort_selection_artifact_byte_count=1,
        cohort_selection_sha256="1" * 64,
        task_time_authority=PilotTaskTimeAuthorityV1.STATIC_WALL_CLOCK_INDEPENDENT_ONLY,
        dynamic_wall_clock_tasks_excluded=True,
        tasks=tasks,
        hosts=(PilotHostV1.QWEN3_VL, PilotHostV1.MAI_UI),
        arms=(PilotArmV1.BASELINE, PilotArmV1.JOINT_SENTINEL),
        topology=PilotTopologyV1.ISOLATED_HISTORY_FREE,
        seed_policy=PilotSeedPolicyV1.FIXED_PER_TASK_SHARED_ACROSS_HOSTS_AND_ARMS,
        baseline_mode="OFF",
        joint_mode="ACTIVE",
        environment_reset_between_cells=True,
        matched_task_ids=True,
        matched_task_parameters=True,
        official_success_metric_required=True,
        max_steps_per_cell=3,
        per_cell_timeout_seconds=60,
        max_total_wall_time_seconds=10_000,
        max_total_actor_calls=160,
        max_total_openai_calls=160,
        max_total_cost_usd_micros=1_000_000,
    )


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
        snapshot_tree_sha256=_sha(f"snapshot-{host.value}"),
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"cpu-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )


def _smokes(tmp_path: Path, host: PilotHostV1) -> HostLiveSmokePlanV1:
    return HostLiveSmokePlanV1(
        host=host,
        cases=tuple(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=str(
                    tmp_path / "inputs" / f"{host.value.lower()}-{mode.value}.json"
                ),
                request_fixture_sha256=_sha(f"fixture-{host.value}-{mode.value}"),
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


def _manifest(tmp_path: Path) -> R24R25RunAuthorityManifestV1:
    repository = tmp_path / "repo"
    repository.mkdir()
    secret = tmp_path / "credentials" / "openai.key"
    secret.parent.mkdir()
    secret.write_text("secret-canary-never-read-or-written", encoding="utf-8")
    secret.chmod(0o600)
    pilot = _pilot(tmp_path)
    smokes = (
        _smokes(tmp_path, PilotHostV1.QWEN3_VL),
        _smokes(tmp_path, PilotHostV1.MAI_UI),
    )
    return R24R25RunAuthorityManifestV1(
        schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
        run_id="cpu-executor-run",
        source_commit="a" * 40,
        authorization=OwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
            authorization_id="cpu-owner-authority",
            authorized_by="owner",
            issued_at_utc="2026-09-03T00:00:00Z",
            expires_at_utc="2026-09-04T00:00:00Z",
            network_allowed=True,
            gpu_allowed=True,
            docker_allowed=True,
            model_loading_allowed=True,
            backend_allowed=True,
            actor_model_calls_allowed=True,
            sentinel_provider_calls_allowed=True,
            pilot_gui_actions_allowed=True,
            smoke_gui_actions_allowed=False,
            merge_allowed=False,
            linear_update_allowed=False,
            frozen_artifact_mutation_allowed=False,
        ),
        safety=SequenceSafetyV1(
            stages=(
                RunStageV1.RESOURCE_PREFLIGHT,
                RunStageV1.QWEN_LIVE_SMOKE,
                RunStageV1.MAI_LIVE_SMOKE,
                RunStageV1.R25_PILOT,
            ),
            stop_on_failure=True,
            pilot_only_after_both_smokes_pass=True,
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
        openai_stages=(
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.RUBRIC,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=8192,
                timeout_ms=30_000,
                max_attempts=1,
                store=False,
            ),
            OpenAIResponsesStageV1(
                role=OpenAIRoleV1.HISTORY_POLICY,
                model="gpt-5.6-sol",
                endpoint="https://api.openai.com/v1/responses",
                transport_kind="OPENAI_RESPONSES",
                transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
                openai_sdk_version="1.106.1",
                sdk_max_retries=0,
                external_network_on_call=True,
                model_on_call=True,
                max_output_tokens=4096,
                timeout_ms=30_000,
                max_attempts=1,
                store=False,
            ),
        ),
        actor_resources=(
            _resource(tmp_path, PilotHostV1.QWEN3_VL, 18081),
            _resource(tmp_path, PilotHostV1.MAI_UI, 18082),
        ),
        smoke_plans=smokes,
        pilot=pilot,
        topology_comparison_artifact_sha256=pilot.topology_comparison_artifact_sha256,
        output_root=str(tmp_path / "external-output"),
        max_resource_preflight_wall_time_seconds=100,
        max_sequence_wall_time_seconds=10_460,
        max_sequence_openai_calls=172,
        max_sequence_actor_calls=166,
        max_sequence_cost_usd_micros=1_000_600,
    )


def _run(
    manifest: R24R25RunAuthorityManifestV1,
    fault: CpuTestFaultV1 = CpuTestFaultV1.NONE,
) -> tuple[CpuTestR24R25ExecutorV1, SequenceRunResultV1]:
    executor = build_cpu_test_executor_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=Path(manifest.output_root).parent / "repo",
        fault=fault,
    )
    result = run_authorized_sequence_with_executor(
        manifest,
        executor,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    return executor, result


def _assert_failure_bundle(
    manifest: R24R25RunAuthorityManifestV1,
    *,
    stage: RunStageV1,
    code: str,
) -> None:
    output = Path(manifest.output_root)
    assert output.is_dir()
    payload = json.loads((output / "failure.json").read_text(encoding="utf-8"))
    assert payload["status"] == "FAILED"
    assert payload["failed_stage"] == stage.value
    assert payload["failure_code"] == code
    assert payload["manifest_sha256"] == authority_manifest_sha256(manifest)
    if payload["stage_failure_evidence"] is not None:
        assert type(payload["stage_failure_evidence"]) is dict
        preimage = json.dumps(
            payload["stage_failure_evidence"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(preimage).hexdigest() == payload["stage_failure_evidence_sha256"]


def test_cpu_executor_commits_complete_hash_bound_external_stage_evidence(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    executor, result = _run(manifest)

    assert result.status is SequenceStatusV1.COMPLETE
    assert tuple(receipt.stage for receipt in result.receipts) == manifest.safety.stages
    assert all(
        receipt.manifest_sha256 == authority_manifest_sha256(manifest)
        for receipt in result.receipts
    )
    census = executor.census
    assert isinstance(executor, SequenceStageExecutorV1)
    assert census.state is ExecutorStateV1.COMPLETE
    assert census.completed_stages == manifest.safety.stages
    assert (census.actor_calls, census.openai_calls, census.actor_actions) == (86, 12, 80)
    assert census.secret_leases_acquired == census.secret_leases_closed == 3
    assert census.cleanup_attempted and census.cleanup_succeeded and census.output_committed

    output = Path(manifest.output_root)
    assert output.is_dir()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    files = sorted(output.iterdir())
    assert [path.name for path in files] == [
        "00-resource-preflight.json",
        "01-qwen-live-smoke.json",
        "02-mai-live-smoke.json",
        "03-r25-pilot.json",
        "manifest-binding.json",
    ]
    serialized = b"".join(path.read_bytes() for path in files)
    assert authority_manifest_sha256(manifest).encode() in serialized
    assert b"secret-canary-never-read-or-written" not in serialized
    assert str(manifest.secret.path).encode() not in serialized
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    for receipt, path in zip(result.receipts, files[:4], strict=True):
        payload = json.loads(path.read_text(encoding="utf-8"))
        evidence_preimage = json.dumps(
            payload["evidence"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        assert hashlib.sha256(evidence_preimage).hexdigest() == receipt.evidence_sha256


def test_output_transaction_fsyncs_files_staging_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_executor

    manifest = _manifest(tmp_path)
    original_fsync = os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        original_fsync(descriptor)

    monkeypatch.setattr(live_executor.os, "fsync", recording_fsync)
    _, result = _run(manifest)

    assert result.status is SequenceStatusV1.COMPLETE
    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    # Staging entries and the final rename are each followed by directory fsync.
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) >= 7


def test_smoke_failure_is_redacted_stops_sequence_and_publishes_failure_evidence(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    executor, result = _run(manifest, CpuTestFaultV1.QWEN_SMOKE_FAILURE)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.QWEN_LIVE_SMOKE
    assert result.failure_code == "STAGE_ADAPTER_FAILED"
    assert tuple(receipt.stage for receipt in result.receipts) == (RunStageV1.RESOURCE_PREFLIGHT,)
    _assert_failure_bundle(manifest, stage=RunStageV1.QWEN_LIVE_SMOKE, code="STAGE_ADAPTER_FAILED")
    assert "adapter-private-detail" not in repr(result)
    census = executor.census
    assert census.state is ExecutorStateV1.FAILED
    assert census.secret_leases_acquired == census.secret_leases_closed == 1
    assert census.cleanup_attempted and census.cleanup_succeeded
    assert not census.output_committed
    with pytest.raises(LiveRunContractError) as raised:
        executor.run_stage(RunStageV1.MAI_LIVE_SMOKE, manifest)
    assert raised.value.code == "EXECUTOR_STOPPED"


def test_smoke_failure_plus_stubborn_cleanup_persists_primary_and_residual_evidence(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    executor, result = _run(
        manifest,
        CpuTestFaultV1.QWEN_SMOKE_AND_RESOURCE_CLEANUP_FAILURE,
    )

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.QWEN_LIVE_SMOKE
    assert result.failure_code == "EXECUTOR_CLEANUP_FAILED"
    assert tuple(receipt.stage for receipt in result.receipts) == (RunStageV1.RESOURCE_PREFLIGHT,)
    assert executor.census.cleanup_attempted
    assert not executor.census.cleanup_succeeded
    failure = json.loads((Path(manifest.output_root) / "failure.json").read_text(encoding="utf-8"))
    assert failure["failed_stage"] == RunStageV1.QWEN_LIVE_SMOKE.value
    assert failure["failure_code"] == "STAGE_ADAPTER_FAILED"
    assert failure["stage_failure_evidence"]["failure_code"] == ("STAGE_ADAPTER_FAILED")
    assert failure["resource_cleanup_status"] == "RETRY_REQUIRED"
    assert failure["resource_cleanup_failure_code"] == "RESOURCE_CLEANUP_FAILED"
    cleanup = failure["resource_cleanup_evidence"]
    assert cleanup["status"] == "FAILED_CLEANUP_RETRY_REQUIRED"
    assert cleanup["cleanup_status"] == "RETRY_REQUIRED"
    assert cleanup["residual_capabilities"] == {
        "backend_container_id": "cpu-stubborn-container",
        "model_pids": [10_000, 10_001],
    }
    cleanup_preimage = json.dumps(
        cleanup,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert (
        failure["resource_cleanup_evidence_sha256"] == hashlib.sha256(cleanup_preimage).hexdigest()
    )


def test_budget_overrun_fails_before_receipt_admission_and_cleans_everything(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    executor, result = _run(manifest, CpuTestFaultV1.PILOT_ACTOR_BUDGET_OVERRUN)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.R25_PILOT
    assert result.failure_code == "STAGE_BUDGET_OR_CENSUS_MISMATCH"
    assert tuple(receipt.stage for receipt in result.receipts) == manifest.safety.stages[:3]
    assert executor.census.completed_stages == manifest.safety.stages[:3]
    assert executor.census.secret_leases_acquired == executor.census.secret_leases_closed == 3
    assert executor.census.cleanup_succeeded
    _assert_failure_bundle(
        manifest,
        stage=RunStageV1.R25_PILOT,
        code="STAGE_BUDGET_OR_CENSUS_MISMATCH",
    )


def test_adapter_success_then_executor_wall_overrun_preserves_stage_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_executor

    manifest = replace(
        _manifest(tmp_path),
        max_resource_preflight_wall_time_seconds=1,
        max_sequence_wall_time_seconds=10_361,
    )
    executor = build_cpu_test_executor_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=tmp_path / "repo",
    )
    base_ns = time.monotonic_ns()
    calls = 0

    def monotonic_with_stage_overrun() -> int:
        nonlocal calls
        calls += 1
        return base_ns if calls <= 2 else base_ns + 2_000_000_000

    monkeypatch.setattr(live_executor.time, "monotonic_ns", monotonic_with_stage_overrun)
    with pytest.raises(LiveRunContractError) as raised:
        executor.run_stage(RunStageV1.RESOURCE_PREFLIGHT, manifest)
    assert raised.value.code == "STAGE_BUDGET_OR_CENSUS_MISMATCH"
    output = Path(manifest.output_root)
    failure = json.loads((output / "failure.json").read_text(encoding="utf-8"))
    assert failure["stage_failure_evidence"] == {
        "execution_scope": "CPU_TEST_LOCAL",
        "manifest_sha256": authority_manifest_sha256(manifest),
        "schema_version": "mobileworld.runtime.sentinel-r2.4-r2.5-executor-binding/v1",
        "stage": RunStageV1.RESOURCE_PREFLIGHT.value,
    }
    assert (
        failure["stage_failure_evidence_sha256"]
        == hashlib.sha256(
            json.dumps(
                failure["stage_failure_evidence"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("failure_point", ("write", "rename"))
def test_failure_publication_error_preserves_prior_stage_bytes_and_recovery_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_executor

    manifest = _manifest(tmp_path)
    executor = build_cpu_test_executor_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=tmp_path / "repo",
        fault=CpuTestFaultV1.QWEN_SMOKE_FAILURE,
    )
    executor.run_stage(RunStageV1.RESOURCE_PREFLIGHT, manifest)
    if failure_point == "write":
        original_write_once = live_executor.AtomicExternalOutputTransactionV1._write_once

        def fail_failure_write(
            transaction: live_executor.AtomicExternalOutputTransactionV1,
            name: str,
            payload: bytes,
        ) -> None:
            if name == "failure.json":
                raise OSError("injected failure.json write failure")
            original_write_once(transaction, name, payload)

        monkeypatch.setattr(
            live_executor.AtomicExternalOutputTransactionV1,
            "_write_once",
            fail_failure_write,
        )
    else:
        monkeypatch.setattr(
            live_executor.os,
            "replace",
            lambda _source, _target: (_ for _ in ()).throw(
                OSError("injected failure rename failure")
            ),
        )

    with pytest.raises(LiveRunContractError) as raised:
        executor.run_stage(RunStageV1.QWEN_LIVE_SMOKE, manifest)
    assert raised.value.code == "EXECUTOR_FAILURE_PUBLICATION_FAILED"
    staging = Path(manifest.output_root).parent / (
        f".{Path(manifest.output_root).name}.{authority_manifest_sha256(manifest)[:16]}.partial"
    )
    assert staging.is_dir()
    assert not Path(manifest.output_root).exists()
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert (staging / "manifest-binding.json").is_file()
    assert (staging / "00-resource-preflight.json").is_file()
    recovery = json.loads((staging / "recovery.json").read_text(encoding="utf-8"))
    assert recovery["status"] == "FAILURE_PUBLICATION_INCOMPLETE"
    assert recovery["completed_stages"] == [RunStageV1.RESOURCE_PREFLIGHT.value]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in staging.iterdir())


def test_missing_secret_lease_fails_before_smoke_adapter_and_never_reads_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_executor

    manifest = _manifest(tmp_path)
    secret = Path(manifest.secret.path)
    original_read_bytes = Path.read_bytes
    original_os_open = os.open

    def guarded_read_bytes(path: Path) -> bytes:
        if path == secret:
            raise AssertionError("executor must not read secret content")
        return original_read_bytes(path)

    def guarded_os_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(os.fsdecode(path)) == secret:
            raise AssertionError("executor must not open secret content")
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(live_executor.os, "open", guarded_os_open)
    executor, result = _run(manifest, CpuTestFaultV1.SECRET_LEASE_UNAVAILABLE)

    assert result.failure_code == "SECRET_LEASE_UNAVAILABLE"
    assert result.failed_stage is RunStageV1.QWEN_LIVE_SMOKE
    assert executor.census.secret_leases_acquired == 0
    assert executor.census.secret_leases_closed == 0
    assert executor.census.cleanup_succeeded
    _assert_failure_bundle(
        manifest, stage=RunStageV1.QWEN_LIVE_SMOKE, code="SECRET_LEASE_UNAVAILABLE"
    )


def test_cleanup_failure_is_explicit_and_transaction_never_commits(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    executor, result = _run(manifest, CpuTestFaultV1.RESOURCE_CLEANUP_FAILURE)

    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.R25_PILOT
    assert result.failure_code == "EXECUTOR_CLEANUP_FAILED"
    assert executor.census.cleanup_attempted
    assert not executor.census.cleanup_succeeded
    assert not executor.census.output_committed
    _assert_failure_bundle(manifest, stage=RunStageV1.R25_PILOT, code="EXECUTOR_CLEANUP_FAILED")


def test_stage_order_and_manifest_drift_are_terminal_before_external_work(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    executor = build_cpu_test_executor_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=tmp_path / "repo",
    )
    with pytest.raises(LiveRunContractError) as raised:
        executor.run_stage(RunStageV1.QWEN_LIVE_SMOKE, manifest)
    assert raised.value.code == "STAGE_ORDER_VIOLATION"
    assert executor.census.state is ExecutorStateV1.FAILED
    assert executor.census.cleanup_succeeded
    _assert_failure_bundle(manifest, stage=RunStageV1.QWEN_LIVE_SMOKE, code="STAGE_ORDER_VIOLATION")

    second_manifest = replace(manifest, output_root=str(tmp_path / "external-output-drift"))
    drifted = replace(second_manifest, run_id="cpu-executor-run-drift")
    second = build_cpu_test_executor_v1(
        second_manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(second_manifest),
        repository_root=tmp_path / "repo",
    )
    with pytest.raises(LiveRunContractError) as drift:
        second.run_stage(RunStageV1.RESOURCE_PREFLIGHT, drifted)
    assert drift.value.code == "MANIFEST_BINDING_MISMATCH"
    assert second.census.cleanup_succeeded
    _assert_failure_bundle(
        second_manifest,
        stage=RunStageV1.RESOURCE_PREFLIGHT,
        code="MANIFEST_BINDING_MISMATCH",
    )


def test_output_must_be_fresh_and_outside_repository(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inside = replace(manifest, output_root=str(tmp_path / "repo" / "run-output"))
    with pytest.raises(ValueError, match="repository-external"):
        build_cpu_test_executor_v1(
            inside,
            confirmed_manifest_sha256=authority_manifest_sha256(inside),
            repository_root=tmp_path / "repo",
        )


def test_production_executor_is_unconstructible_until_exact_module_adapters_exist(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    assert production_executor_available_v1() is True
    assert len(PRODUCTION_ROOT_HOOKS_V1) == 5
    assert all("command" not in hook.lower() for hook in PRODUCTION_ROOT_HOOKS_V1)
    with pytest.raises(LiveRunContractError) as raised:
        ProductionR24R25ExecutorV1(
            manifest,
            confirmed_manifest_sha256=authority_manifest_sha256(manifest),
            repository_root=tmp_path / "repo",
            module_owned_adapters=object(),
        )
    assert raised.value.code == "PRODUCTION_ADAPTERS_UNAVAILABLE"

    unavailable = UnavailableSecureSecretLeaseProviderV1()
    with pytest.raises(RuntimeError, match="unavailable") as lease_failure:
        unavailable.acquire(
            manifest.secret,
            manifest_sha256=authority_manifest_sha256(manifest),
        )
    assert "secret-canary" not in str(lease_failure.value)
    assert str(manifest.secret.path) not in str(lease_failure.value)


def test_cpu_factory_rejects_callable_or_open_ended_fault_injection(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    arbitrary_callback = cast(CpuTestFaultV1, lambda: None)
    with pytest.raises(ValueError, match="module-owned factory"):
        build_cpu_test_executor_v1(
            manifest,
            confirmed_manifest_sha256=authority_manifest_sha256(manifest),
            repository_root=tmp_path / "repo",
            fault=arbitrary_callback,
        )
