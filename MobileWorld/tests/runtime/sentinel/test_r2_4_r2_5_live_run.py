from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from _r2_4_topology_fixture import write_cpu_topology_artifact

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptPricingV1,
    live_attempt_pricing_projection,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_executor import ProductionR24R25ExecutorV1
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
    StageExecutionReceiptV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    compute_snapshot_tree_digest,
    inspect_local_resources,
    load_authority_manifest,
    parse_authority_manifest,
    run_authorized_sequence_with_executor,
)
from mobile_world.runtime.sentinel.r2_4.production_driver import (
    ProductionRuntimeConfigV1,
    production_runtime_config_projection,
    production_runtime_config_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    production_preflight_report_sha256,
    run_production_preflight_v1,
)
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    cohort_selection_projection,
    current_registry_metadata,
    select_gui_only_cohort,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    FROZEN_PILOT_SCHEMA_VERSION,
    FrozenPilotManifestV1,
    InlinePilotTaskParametersV1,
    MobileWorldTaskParametersV1,
    PilotArmV1,
    PilotHostV1,
    PilotSeedPolicyV1,
    PilotTaskTimeAuthorityV1,
    PilotTaskV1,
    PilotTopologyV1,
    R25PilotContractError,
    executable_pilot_task_source_projection,
    frozen_pilot_manifest_projection,
    frozen_pilot_manifest_sha256,
    parse_frozen_pilot_manifest,
    pilot_task_source_projection,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_bound(path: Path, raw: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _sha(raw), len(raw)


def _pilot(tmp_path: Path, *, count: int = 20) -> FrozenPilotManifestV1:
    if not 20 <= count <= 30:
        raise R25PilotContractError("INVALID_COHORT_SIZE", "pilot needs 20--30 tasks")
    registry = current_registry_metadata()
    cohort_source = tmp_path / "inputs" / "gui-only-task-source.jsonl"
    cohort_source.parent.mkdir(parents=True, exist_ok=True)
    cohort_source.write_text(
        "".join(
            json.dumps({"task_name": record.task_id, "trial": 1}, sort_keys=True) + "\n"
            for record in registry
        ),
        encoding="utf-8",
    )
    selection = select_gui_only_cohort(cohort_source, registry, cohort_size=count)
    tasks = tuple(
        PilotTaskV1(
            task_id=member.task_id,
            task_parameters_sha256=member.task_parameters_sha256,
            reset_seed=member.reset_seed,
        )
        for member in selection.members
    )
    task_manifest = tmp_path / "inputs" / "pilot_tasks.json"
    task_source = executable_pilot_task_source_projection(
        "r25-pilot-fixture",
        tasks,
        tuple(
            InlinePilotTaskParametersV1(
                task_id=task.task_id,
                parameters=MobileWorldTaskParametersV1(task_name=task.task_id, trial=1),
            )
            for task in tasks
        ),
    )
    task_hash, task_bytes = _write_bound(
        task_manifest,
        json.dumps(
            task_source,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    topology_path = tmp_path / "inputs" / "topology.json"
    topology_sha256, topology_byte_count = write_cpu_topology_artifact(topology_path)
    selection_path = tmp_path / "inputs" / "cohort-selection.json"
    selection_sha256, selection_byte_count = _write_bound(
        selection_path,
        canonical_json_bytes(cohort_selection_projection(selection)),
    )
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="r25-pilot-fixture",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path=str(task_manifest),
        task_manifest_sha256=task_hash,
        task_manifest_byte_count=task_bytes,
        topology_comparison_artifact_path=str(topology_path),
        topology_comparison_artifact_sha256=topology_sha256,
        topology_comparison_artifact_byte_count=topology_byte_count,
        cohort_selection_artifact_path=str(selection_path),
        cohort_selection_artifact_sha256=selection_sha256,
        cohort_selection_artifact_byte_count=selection_byte_count,
        cohort_selection_sha256=selection_sha256,
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


def _snapshot_resource(tmp_path: Path, host: PilotHostV1, port: int) -> SnapshotResourceV1:
    storage = tmp_path / "models" / host.value.lower()
    snapshot = storage / "snapshots" / "revision"
    blob = storage / "blobs" / "weights"
    _write_bound(blob, f"{host.value}-weights".encode())
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text('{"model":"fixture"}\n', encoding="utf-8")
    (snapshot / "weights.bin").symlink_to(blob)
    codec = (
        "mobileworld.g1.history-codec.qwen-flat-progress"
        if host is PilotHostV1.QWEN3_VL
        else "mobileworld.g1.history-codec.mai-raw-replay"
    )
    provisional = SnapshotResourceV1(
        host=host,
        history_codec_id=codec,
        snapshot_path=str(snapshot),
        snapshot_storage_root=str(storage),
        snapshot_tree_algorithm=SNAPSHOT_TREE_ALGORITHM_V1,
        snapshot_tree_sha256="0" * 64,
        snapshot_total_bytes=1,
        snapshot_file_count=1,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"fixture-{host.value.lower()}",
        host_enabled=True,
        independent_kill_switch=True,
    )
    digest = compute_snapshot_tree_digest(provisional)
    return replace(
        provisional,
        snapshot_tree_sha256=digest.sha256,
        snapshot_total_bytes=digest.total_bytes,
        snapshot_file_count=digest.file_count,
    )


def _smoke_plan(tmp_path: Path, host: PilotHostV1) -> HostLiveSmokePlanV1:
    cases: list[LiveSmokeCaseV1] = []
    for mode in SmokeModeV1:
        fixture = tmp_path / "inputs" / f"{host.value.lower()}-{mode.value.lower()}.json"
        digest, size = _write_bound(fixture, f'{{"mode":"{mode.value}"}}\n'.encode())
        cases.append(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=str(fixture),
                request_fixture_sha256=digest,
                request_fixture_byte_count=size,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=60,
                max_cost_usd_micros=100,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
        )
    return HostLiveSmokePlanV1(host=host, cases=tuple(cases))


def _manifest(
    tmp_path: Path,
    *,
    status: RunAuthorizationStatusV1 = RunAuthorizationStatusV1.OWNER_AUTHORIZED,
) -> R24R25RunAuthorityManifestV1:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    secret = tmp_path / "credentials" / "openai.key"
    secret.parent.mkdir(exist_ok=True)
    secret.write_bytes(b"fixture-value-never-read")
    secret.chmod(0o600)
    pilot = _pilot(tmp_path)
    smokes = (
        _smoke_plan(tmp_path, PilotHostV1.QWEN3_VL),
        _smoke_plan(tmp_path, PilotHostV1.MAI_UI),
    )
    return R24R25RunAuthorityManifestV1(
        schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
        run_id="r24-r25-fixture",
        source_commit="a" * 40,
        authorization=OwnerAuthorizationV1(
            status=status,
            authorization_id="owner-approval-fixture",
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
            _snapshot_resource(tmp_path, PilotHostV1.QWEN3_VL, 18081),
            _snapshot_resource(tmp_path, PilotHostV1.MAI_UI, 18082),
        ),
        smoke_plans=smokes,
        pilot=pilot,
        topology_comparison_artifact_sha256=pilot.topology_comparison_artifact_sha256,
        output_root=str(tmp_path / "new-output"),
        max_resource_preflight_wall_time_seconds=100,
        max_sequence_wall_time_seconds=10_460,
        max_sequence_openai_calls=172,
        max_sequence_actor_calls=166,
        max_sequence_cost_usd_micros=1_000_600,
    )


def test_frozen_pilot_is_exact_20_task_matched_matrix(tmp_path: Path) -> None:
    pilot = _pilot(tmp_path)
    assert len(pilot.cells) == 80
    first_four = pilot.cells[:4]
    assert [(cell.host.value, cell.arm.value, cell.sentinel_mode) for cell in first_four] == [
        ("QWEN3_VL", "BASELINE", "OFF"),
        ("QWEN3_VL", "JOINT_SENTINEL", "ACTIVE"),
        ("MAI_UI", "BASELINE", "OFF"),
        ("MAI_UI", "JOINT_SENTINEL", "ACTIVE"),
    ]
    projected = frozen_pilot_manifest_projection(pilot)
    assert parse_frozen_pilot_manifest(projected) == pilot
    assert frozen_pilot_manifest_sha256(pilot) == _sha(
        json.dumps(
            projected, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )


@pytest.mark.parametrize("count", [19, 31])
def test_pilot_rejects_cohort_outside_20_to_30(tmp_path: Path, count: int) -> None:
    with pytest.raises(R25PilotContractError, match="INVALID_COHORT_SIZE"):
        _pilot(tmp_path, count=count)


def test_authority_projection_round_trips_and_rejects_command_fields(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    projection = authority_manifest_projection(manifest)
    assert parse_authority_manifest(projection) == manifest
    assert manifest.topology_comparison_artifact_sha256 == (
        manifest.pilot.topology_comparison_artifact_sha256
    )
    assert authority_manifest_sha256(manifest) == _sha(
        json.dumps(
            projection, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    unsafe = cast(dict[str, JsonValue], dict(projection))
    unsafe["commands"] = ["sh", "-c", "do-not-run"]
    with pytest.raises(LiveRunContractError, match="INVALID_FIELDS"):
        parse_authority_manifest(unsafe)


def test_manifest_binds_exact_modes_models_endpoints_commit_and_budgets(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert tuple(case.mode for plan in manifest.smoke_plans for case in plan.cases) == (
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
    )
    with pytest.raises(LiveRunContractError, match="ACTOR_ENDPOINT_NOT_LOOPBACK"):
        replace(manifest.actor_resources[0], actor_endpoint="http://192.0.2.1:18081/v1")
    with pytest.raises(LiveRunContractError, match="RETRIES_FORBIDDEN"):
        replace(manifest.openai_stages[0], max_attempts=2)
    with pytest.raises(LiveRunContractError, match="BUDGET_BINDING_MISMATCH"):
        replace(manifest, max_sequence_actor_calls=167)
    with pytest.raises(LiveRunContractError, match="TOPOLOGY_BINDING_MISMATCH"):
        replace(manifest, topology_comparison_artifact_sha256="f" * 64)


def test_secret_preflight_uses_stat_only_and_deep_hashes_nonsecret_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_run

    manifest = _manifest(tmp_path)
    secret = Path(manifest.secret.path)
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path == secret:
            raise AssertionError("preflight must never open the secret")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(live_run, "_git_state", lambda _: (manifest.source_commit, True))
    report = inspect_local_resources(
        manifest,
        repo_root=tmp_path / "repo",
        deep_snapshot_hashes=True,
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    assert all(check.passed for check in report.checks)
    assert report.deep_snapshot_hashes_verified is True
    assert report.secret_content_read is False
    assert report.network_calls == report.gpu_operations == report.docker_operations == 0
    assert report.model_loads == report.backend_operations == report.actor_actions == 0
    assert report.files_written == 0


def test_secret_mode_symlink_and_output_inside_repo_fail_metadata_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_run

    manifest = _manifest(tmp_path)
    monkeypatch.setattr(live_run, "_git_state", lambda _: (manifest.source_commit, True))
    secret = Path(manifest.secret.path)
    secret.chmod(0o644)
    changed = replace(manifest, output_root=str(tmp_path / "repo" / "output"))
    report = inspect_local_resources(
        changed,
        repo_root=tmp_path / "repo",
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    by_id = {check.check_id: check.passed for check in report.checks}
    assert by_id["openai_secret_external_regular_0600"] is False
    assert by_id["fresh_repo_external_output_root"] is False
    assert report.deep_snapshot_hashes_verified is False


def test_authority_preflight_rejects_legacy_hash_only_task_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_run

    manifest = _manifest(tmp_path)
    legacy_source = pilot_task_source_projection(manifest.pilot.cohort_id, manifest.pilot.tasks)
    legacy_raw = canonical_json_bytes(legacy_source)
    legacy_path = Path(manifest.pilot.task_manifest_path)
    legacy_path.write_bytes(legacy_raw)
    legacy_pilot = replace(
        manifest.pilot,
        task_manifest_sha256=_sha(legacy_raw),
        task_manifest_byte_count=len(legacy_raw),
    )
    manifest = replace(manifest, pilot=legacy_pilot)
    monkeypatch.setattr(live_run, "_git_state", lambda _: (manifest.source_commit, True))

    report = inspect_local_resources(
        manifest,
        repo_root=tmp_path / "repo",
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )

    checks = {check.check_id: check.passed for check in report.checks}
    assert checks["frozen_pilot_task_manifest"] is False


def test_load_manifest_rejects_duplicate_json_keys_without_echoing_values(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text('{"secret":"never-echo","secret":"still-never-echo"}', encoding="utf-8")
    with pytest.raises(LiveRunContractError) as raised:
        load_authority_manifest(path)
    assert raised.value.code == "DUPLICATE_JSON_KEY"
    assert "never-echo" not in str(raised.value)


def test_load_manifest_rejects_nonfinite_json_number(tmp_path: Path) -> None:
    path = tmp_path / "authority.json"
    path.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(LiveRunContractError) as raised:
        load_authority_manifest(path)
    assert raised.value.code == "NON_CANONICAL_JSON"


class _RecordingExecutor(SequenceStageExecutorV1):
    def __init__(self, *, fail: RunStageV1 | None = None) -> None:
        self.calls: list[RunStageV1] = []
        self.fail = fail

    def run_stage(
        self, stage: RunStageV1, manifest: R24R25RunAuthorityManifestV1
    ) -> StageExecutionReceiptV1:
        self.calls.append(stage)
        if stage is self.fail:
            raise RuntimeError("fixture failure must be redacted")
        if stage is RunStageV1.RESOURCE_PREFLIGHT:
            units = ("resources",)
            actor_calls = openai_calls = actions = cost = 0
            wall_time_ms = 10
            provider_proof = False
        elif stage in {RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE}:
            host = "QWEN3_VL" if stage is RunStageV1.QWEN_LIVE_SMOKE else "MAI_UI"
            units = tuple(f"{host}:{mode.value}" for mode in SmokeModeV1)
            actor_calls, openai_calls, actions, cost = 3, 6, 0, 200
            wall_time_ms = 2_000
            provider_proof = True
        else:
            units = tuple(f"pilot-cell-{index:03d}" for index, _ in enumerate(manifest.pilot.cells))
            actor_calls, openai_calls, actions, cost = 80, 80, 20, 10_000
            wall_time_ms = 10_000
            provider_proof = True
        return StageExecutionReceiptV1(
            stage=stage,
            manifest_sha256=authority_manifest_sha256(manifest),
            passed=True,
            evidence_sha256=_sha(stage.value.encode()),
            actor_calls=actor_calls,
            openai_calls=openai_calls,
            actor_actions=actions,
            cost_usd_micros=cost,
            wall_time_ms=wall_time_ms,
            completed_units=units,
            provider_final_request_proven=provider_proof,
        )


def test_sequence_runs_exact_order_and_pilot_only_after_both_smokes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    executor = _RecordingExecutor()
    result = run_authorized_sequence_with_executor(
        manifest,
        executor,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    assert result.status is SequenceStatusV1.COMPLETE
    assert executor.calls == list(manifest.safety.stages)
    assert tuple(receipt.stage for receipt in result.receipts) == manifest.safety.stages


def test_sequence_stops_immediately_and_never_enters_pilot_after_smoke_failure(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    executor = _RecordingExecutor(fail=RunStageV1.QWEN_LIVE_SMOKE)
    result = run_authorized_sequence_with_executor(
        manifest,
        executor,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        now=datetime(2026, 9, 3, 12, tzinfo=UTC),
    )
    assert result.status is SequenceStatusV1.FAILED
    assert result.failed_stage is RunStageV1.QWEN_LIVE_SMOKE
    assert result.failure_code == "STAGE_EXECUTOR_ERROR"
    assert executor.calls == [RunStageV1.RESOURCE_PREFLIGHT, RunStageV1.QWEN_LIVE_SMOKE]


def test_sequence_requires_owner_authority_current_window_and_exact_hash(tmp_path: Path) -> None:
    draft = _manifest(tmp_path, status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED)
    executor = _RecordingExecutor()
    with pytest.raises(LiveRunContractError, match="OWNER_AUTHORITY_REQUIRED"):
        run_authorized_sequence_with_executor(
            draft,
            executor,
            confirmed_manifest_sha256=authority_manifest_sha256(draft),
            now=datetime(2026, 9, 3, 12, tzinfo=UTC),
        )
    live = replace(
        draft,
        authorization=replace(
            draft.authorization, status=RunAuthorizationStatusV1.OWNER_AUTHORIZED
        ),
    )
    with pytest.raises(LiveRunContractError, match="MANIFEST_CONFIRMATION_MISMATCH"):
        run_authorized_sequence_with_executor(
            live,
            executor,
            confirmed_manifest_sha256="0" * 64,
            now=datetime(2026, 9, 3, 12, tzinfo=UTC),
        )
    with pytest.raises(LiveRunContractError, match="OWNER_AUTHORITY_EXPIRED"):
        run_authorized_sequence_with_executor(
            live,
            executor,
            confirmed_manifest_sha256=authority_manifest_sha256(live),
            now=datetime(2026, 9, 5, 12, tzinfo=UTC),
        )
    assert executor.calls == []


def _load_cli_module() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "run_r2_4_r2_5.py"
    spec = importlib.util.spec_from_file_location("r24_r25_cli_for_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_execute_requires_complete_owner_pinned_inputs_and_emits_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _manifest(tmp_path)
    path = tmp_path / "authority.json"
    path.write_text(
        json.dumps(authority_manifest_projection(manifest), sort_keys=True), encoding="utf-8"
    )
    module = _load_cli_module()
    result = module.main(
        [
            "--authority-manifest",
            str(path),
            "--execute",
            "--confirm-manifest-sha256",
            authority_manifest_sha256(manifest),
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_code": "EXECUTE_ARGUMENTS_REQUIRED",
        "ok": False,
    }
    assert "fixture-value-never-read" not in captured.err


def test_cli_execute_constructs_exact_production_executor_before_dispatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_run

    manifest = _manifest(tmp_path)
    manifest_path = tmp_path / "authority.json"
    manifest_path.write_text(
        json.dumps(authority_manifest_projection(manifest), sort_keys=True), encoding="utf-8"
    )
    module = _load_cli_module()
    repository = tmp_path / "repo"
    module.REPOSITORY_ROOT = repository
    monkeypatch.setattr(live_run, "_git_state", lambda _: (manifest.source_commit, True))

    executable = Path(sys.executable).resolve(strict=True)
    environment_file = tmp_path / "runtime" / "backend.env"
    environment_file.parent.mkdir()
    environment_file.write_text("MOBILEWORLD_CPU_TEST=1\n", encoding="utf-8")
    environment_file.chmod(0o600)
    process_logs = tmp_path / "runtime" / "logs"
    process_logs.mkdir(mode=0o700)
    source_root = repository / "MobileWorld" / "src"
    source_root.mkdir(parents=True)
    environment_info = environment_file.stat()
    runtime_config = ProductionRuntimeConfigV1(
        backend_port=18082,
        backend_device="emulator-5554",
        qwen_gpu_index=0,
        mai_gpu_index=1,
        process_log_root=str(process_logs),
        authorized_pilot_input_root=str(tmp_path / "inputs"),
        repository_root=str(repository),
        mobileworld_source_root=str(source_root),
        vllm_python_executable=str(executable),
        vllm_python_realpath=str(executable),
        vllm_python_sha256=_sha(executable.read_bytes()),
        vllm_python_byte_count=executable.stat().st_size,
        vllm_version="0.10.1",
        backend_image_id_sha256=_sha(b"cpu-construction-image-id"),
        backend_environment_file=str(environment_file),
        backend_environment_file_device=environment_info.st_dev,
        backend_environment_file_inode=environment_info.st_ino,
        backend_environment_file_mode=0o600,
        backend_environment_file_uid=os.geteuid(),
        backend_environment_file_byte_count=environment_info.st_size,
        backend_environment_file_mtime_ns=environment_info.st_mtime_ns,
    )
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(
        json.dumps(production_runtime_config_projection(runtime_config), sort_keys=True),
        encoding="utf-8",
    )
    pricing = LiveAttemptPricingV1(
        pricing_id="owner-cli-cpu-construction",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1_000_000,
        cached_input_usd_micros_per_million_tokens=100_000,
        output_usd_micros_per_million_tokens=2_000_000,
        source_sha256=_sha(b"owner-pinned-pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_text(
        json.dumps(live_attempt_pricing_projection(pricing), sort_keys=True), encoding="utf-8"
    )
    preflight_now = datetime.now(UTC).replace(microsecond=0)
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repository,
        now=preflight_now,
    )
    assert report.eligible_for_post_preflight_factory
    seen: list[ProductionR24R25ExecutorV1] = []

    def run_without_external_operations(
        candidate: R24R25RunAuthorityManifestV1,
        executor: object,
        *,
        confirmed_manifest_sha256: str,
    ) -> SequenceRunResultV1:
        assert candidate is manifest or authority_manifest_sha256(candidate) == (
            authority_manifest_sha256(manifest)
        )
        assert confirmed_manifest_sha256 == authority_manifest_sha256(manifest)
        assert type(executor) is ProductionR24R25ExecutorV1
        seen.append(executor)
        return SequenceRunResultV1(
            schema_version="mobileworld.runtime.sentinel-r2.4-r2.5-sequence-result/v1",
            run_id=manifest.run_id,
            manifest_sha256=authority_manifest_sha256(manifest),
            status=SequenceStatusV1.COMPLETE,
            receipts=(),
            failed_stage=None,
            failure_code=None,
        )

    monkeypatch.setattr(
        module, "run_authorized_sequence_with_executor", run_without_external_operations
    )
    result = module.main(
        [
            "--authority-manifest",
            str(manifest_path),
            "--execute",
            "--confirm-manifest-sha256",
            authority_manifest_sha256(manifest),
            "--preflight-checked-at-utc",
            preflight_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--confirm-preflight-report-sha256",
            production_preflight_report_sha256(report),
            "--runtime-config",
            str(runtime_path),
            "--confirm-runtime-config-sha256",
            production_runtime_config_sha256(runtime_config),
            "--pricing",
            str(pricing_path),
            "--confirm-pricing-sha256",
            live_attempt_pricing_sha256(pricing),
            "--production-audit-root",
            str(tmp_path / "runtime" / "audit"),
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    assert len(seen) == 1
    assert json.loads(captured.out)["dry_run"] is False
    assert not Path(manifest.output_root).exists()


def test_cli_default_dry_run_hashes_only_declared_nonsecret_resources(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_run

    manifest = _manifest(tmp_path)
    path = tmp_path / "authority.json"
    path.write_text(
        json.dumps(authority_manifest_projection(manifest), sort_keys=True), encoding="utf-8"
    )
    secret = Path(manifest.secret.path)
    original_open = Path.open

    def guarded_open(target: Path, *args: object, **kwargs: object):
        if target == secret:
            raise AssertionError("CLI dry-run must never open the secret")
        return original_open(target, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(live_run, "_git_state", lambda _: (manifest.source_commit, True))
    module = _load_cli_module()
    module.REPOSITORY_ROOT = tmp_path / "repo"
    result = module.main(
        [
            "--authority-manifest",
            str(path),
            "--dry-run",
            "--deep-snapshot-hash",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    projection = json.loads(captured.out)
    assert projection["dry_run"] is True
    assert projection["preflight"]["deep_snapshot_hashes_verified"] is True
    assert projection["preflight"]["production_executor_installed"] is True
    assert projection["preflight"]["secret_content_read"] is False
    assert not Path(manifest.output_root).exists()
    assert "fixture-value-never-read" not in captured.out
