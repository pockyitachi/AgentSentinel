from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from _r2_4_topology_fixture import write_cpu_topology_artifact

from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptError,
    LiveAttemptPricingV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    MemoryLiveAttemptReceiptSinkV1,
    ProductionOpenAIAttemptRunnerV1,
    build_canonical_openai_request,
    live_attempt_pricing_sha256,
    live_attempt_worst_case_cost_usd_micros,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
    SNAPSHOT_TREE_ALGORITHM_V1,
    HostLiveSmokePlanV1,
    LiveSmokeCaseV1,
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    OwnerAuthorizationV1,
    R24R25RunAuthorityManifestV1,
    RunAuthorizationStatusV1,
    RunStageV1,
    SecretFileReferenceV1,
    SequenceSafetyV1,
    SmokeModeV1,
    SnapshotResourceV1,
    authority_manifest_sha256,
    compute_snapshot_tree_digest,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CASE_EXECUTION_LEASE_SCHEMA_VERSION,
    CaseExecutionLeaseV1,
    CaseExecutionScopeV1,
    SecureOpenAISecretLeaseV1,
    case_execution_lease_projection,
    production_activation_available_v1,
    production_preflight_report_projection,
    production_preflight_report_sha256,
    require_production_post_preflight_factory_v1,
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
    executable_pilot_task_source_projection,
)

_NOW = datetime(2026, 9, 3, 12, tzinfo=UTC)
_SECRET_PLACEHOLDER = b"fixture-secret-must-never-appear"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sealed_openai_request_kwargs(role: LiveAttemptRoleV1) -> dict[str, object]:
    if role is LiveAttemptRoleV1.HISTORY_POLICY:
        from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
            GPT56_OUTPUT_SCHEMA_NAME,
            GPT56_POLICY_INSTRUCTIONS,
            GPT56_REASONING_EFFORT,
            ProposalSchemaSnapshotV1,
        )

        instructions = GPT56_POLICY_INSTRUCTIONS
        reasoning_effort = GPT56_REASONING_EFFORT
        schema_name = GPT56_OUTPUT_SCHEMA_NAME
        schema = ProposalSchemaSnapshotV1.from_checked_in().as_dict()
        max_output_tokens = 4096
        content: list[dict[str, object]] = [
            {"type": "input_text", "text": "{}"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,AA==",
                "detail": "high",
            },
        ]
    else:
        from mobile_world.runtime.sentinel.r2_4.rubric_live import (
            _GENERATE_INSTRUCTIONS,
            LIVE_RUBRIC_REASONING_EFFORT,
            live_rubric_generate_schema,
        )

        schema_snapshot = live_rubric_generate_schema()
        instructions = _GENERATE_INSTRUCTIONS
        reasoning_effort = LIVE_RUBRIC_REASONING_EFFORT
        schema_name = schema_snapshot.name
        schema = schema_snapshot.as_dict()
        max_output_tokens = 8192
        content = [{"type": "input_text", "text": "{}"}]
    return {
        "model": "gpt-5.6-sol",
        "instructions": instructions,
        "input": [{"role": "user", "content": content}],
        "reasoning": {"effort": reasoning_effort},
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
            "verbosity": "low",
        },
        "tools": [],
        "tool_choice": "none",
        "parallel_tool_calls": False,
        "store": False,
        "stream": False,
        "truncation": "disabled",
        "max_output_tokens": max_output_tokens,
    }


def _write(path: Path, raw: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return _sha(raw), len(raw)


def _git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True)
    (repo / "README").write_text("sealed source\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "README"], check=True)
    subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(repo),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        check=True,
    )
    head = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return repo, head


def _pilot(tmp_path: Path) -> FrozenPilotManifestV1:
    declared_inputs = tmp_path / "declared-inputs"
    registry = current_registry_metadata()
    cohort_source = declared_inputs / "gui-only-task-source.jsonl"
    cohort_source.parent.mkdir(parents=True, exist_ok=True)
    cohort_source.write_text(
        "".join(
            json.dumps({"task_name": record.task_id, "trial": 1}, sort_keys=True) + "\n"
            for record in registry
        ),
        encoding="utf-8",
    )
    selection = select_gui_only_cohort(cohort_source, registry)
    parameters = tuple(
        MobileWorldTaskParametersV1(task_name=member.task_id, trial=member.trial)
        for member in selection.members
    )
    parameter_bytes = tuple(
        json.dumps(
            {"task_name": item.task_name, "trial": item.trial},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        for item in parameters
    )
    tasks = tuple(
        PilotTaskV1(
            task_id=item.task_name,
            task_parameters_sha256=_sha(raw),
            reset_seed=selection.members[index].reset_seed,
        )
        for index, (item, raw) in enumerate(zip(parameters, parameter_bytes, strict=True))
    )
    bindings = tuple(
        InlinePilotTaskParametersV1(task_id=item.task_name, parameters=item) for item in parameters
    )
    source = executable_pilot_task_source_projection(
        "production-preflight-fixture", tasks, bindings
    )
    raw = json.dumps(
        source,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    path = declared_inputs / "pilot-tasks.json"
    digest, size = _write(path, raw)
    topology_path = tmp_path / "declared-inputs" / "topology.json"
    topology_sha256, topology_byte_count = write_cpu_topology_artifact(topology_path)
    selection_path = declared_inputs / "cohort-selection.json"
    selection_raw = json.dumps(
        cohort_selection_projection(selection),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    selection_sha256, selection_byte_count = _write(selection_path, selection_raw)
    return FrozenPilotManifestV1(
        schema_version=FROZEN_PILOT_SCHEMA_VERSION,
        cohort_id="production-preflight-fixture",
        frozen_at_utc="2026-09-03T00:00:00Z",
        task_manifest_path=str(path),
        task_manifest_sha256=digest,
        task_manifest_byte_count=size,
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
        max_total_actor_calls=80,
        max_total_openai_calls=80,
        max_total_cost_usd_micros=1_000_000,
    )


def _snapshot(tmp_path: Path, host: PilotHostV1, port: int) -> SnapshotResourceV1:
    storage = tmp_path / "declared-models" / host.value.lower()
    snapshot = storage / "snapshots" / "fixed-revision"
    _write(snapshot / "config.json", f'{{"host":"{host.value}"}}\n'.encode())
    _write(snapshot / "weights.bin", f"{host.value}-weights".encode())
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


def _smoke(tmp_path: Path, host: PilotHostV1) -> HostLiveSmokePlanV1:
    cases: list[LiveSmokeCaseV1] = []
    for mode in SmokeModeV1:
        path = tmp_path / "declared-inputs" / f"{host.value.lower()}-{mode.value}.json"
        digest, size = _write(path, f'{{"mode":"{mode.value}"}}\n'.encode())
        cases.append(
            LiveSmokeCaseV1(
                case_id=f"{host.value.lower()}-{mode.value.lower()}",
                task_id="smoke-task",
                mode=mode,
                request_fixture_path=str(path),
                request_fixture_sha256=digest,
                request_fixture_byte_count=size,
                max_actor_calls=1,
                max_openai_calls=0 if mode is SmokeModeV1.OFF else 3,
                max_wall_time_seconds=10,
                max_cost_usd_micros=100,
                actor_action_allowed=False,
                provider_final_request_proof_required=True,
            )
        )
    return HostLiveSmokePlanV1(host=host, cases=tuple(cases))


def _history_policy_stage() -> OpenAIResponsesStageV1:
    return OpenAIResponsesStageV1(
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
    )


def _rubric_stage() -> OpenAIResponsesStageV1:
    return OpenAIResponsesStageV1(
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
    )


def _manifest(tmp_path: Path) -> tuple[R24R25RunAuthorityManifestV1, Path]:
    repo, source_commit = _git_repo(tmp_path)
    secret = tmp_path / "credentials" / "openai.key"
    _write(secret, _SECRET_PLACEHOLDER)
    secret.chmod(0o600)
    (tmp_path / "outputs").mkdir()
    pilot = _pilot(tmp_path)
    smokes = (
        _smoke(tmp_path, PilotHostV1.QWEN3_VL),
        _smoke(tmp_path, PilotHostV1.MAI_UI),
    )

    def construct(stages: tuple[OpenAIResponsesStageV1, ...]) -> R24R25RunAuthorityManifestV1:
        return R24R25RunAuthorityManifestV1(
            schema_version=R24_R25_RUN_AUTHORITY_SCHEMA_VERSION,
            run_id="production-preflight-fixture",
            source_commit=source_commit,
            authorization=OwnerAuthorizationV1(
                status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
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
            openai_stages=stages,
            actor_resources=(
                _snapshot(tmp_path, PilotHostV1.QWEN3_VL, 18081),
                _snapshot(tmp_path, PilotHostV1.MAI_UI, 18082),
            ),
            smoke_plans=smokes,
            pilot=pilot,
            topology_comparison_artifact_sha256=(pilot.topology_comparison_artifact_sha256),
            output_root=str(tmp_path / "outputs" / "fresh-run"),
            max_resource_preflight_wall_time_seconds=100,
            max_sequence_wall_time_seconds=10_160,
            max_sequence_openai_calls=92,
            max_sequence_actor_calls=86,
            max_sequence_cost_usd_micros=1_000_600,
        )

    return construct((_rubric_stage(), _history_policy_stage())), repo


def test_preflight_is_deep_sealed_and_never_reads_secret_or_connects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, repo = _manifest(tmp_path)
    secret = Path(manifest.secret.path)
    original_open = Path.open

    def guarded_path_open(path, *args, **kwargs):
        if path == secret:
            raise AssertionError("preflight opened secret content")
        return original_open(path, *args, **kwargs)

    def forbidden_connection(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("preflight attempted an endpoint connection")

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(socket, "create_connection", forbidden_connection)
    monkeypatch.setattr(
        SecureOpenAISecretLeaseV1,
        "_read_exact_secret",
        staticmethod(lambda _: (_ for _ in ()).throw(AssertionError("secret lease activated"))),
    )
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repo,
        now=_NOW,
    )
    projection = production_preflight_report_projection(report)
    serialized = json.dumps(projection, sort_keys=True)

    assert report.all_checks_passed is True
    assert report.eligible_for_post_preflight_factory is True
    assert report.production_activation_available is True
    assert report.secret_content_reads == report.endpoint_connections == 0
    assert report.gpu_operations == report.docker_operations == report.model_loads == 0
    assert report.backend_operations == report.actor_actions == report.files_written == 0
    assert report.actor_loopback_ports == (18081, 18082)
    assert production_preflight_report_sha256(report) == production_preflight_report_sha256(report)
    assert _SECRET_PLACEHOLDER.decode() not in serialized
    assert not Path(manifest.output_root).exists()


def test_preflight_rejects_wrong_owner_pin_before_any_secret_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, repo = _manifest(tmp_path)
    secret = Path(manifest.secret.path)
    original_open = Path.open

    def guarded_path_open(path, *args, **kwargs):
        if path == secret:
            raise AssertionError("owner-pin failure opened secret content")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    with pytest.raises(ValueError, match="owner-pinned"):
        run_production_preflight_v1(
            manifest,
            confirmed_manifest_sha256="0" * 64,
            repository_root=repo,
            now=_NOW,
        )


def test_preflight_reports_dirty_source_and_changed_snapshot(tmp_path: Path) -> None:
    manifest, repo = _manifest(tmp_path)
    (repo / "README").write_text("dirty source\n", encoding="utf-8")
    snapshot_file = Path(manifest.actor_resources[0].snapshot_path) / "weights.bin"
    snapshot_file.write_bytes(b"changed-after-owner-pin")

    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repo,
        now=_NOW,
    )
    by_id = {check.check_id: check.passed for check in report.checks}

    assert by_id["git_source_commit"] is True
    assert by_id["git_worktree_clean"] is False
    assert by_id["snapshot_content:QWEN3_VL"] is False
    assert by_id["deep_snapshot_hashes_verified"] is False
    assert report.all_checks_passed is False
    assert report.eligible_for_post_preflight_factory is False


@pytest.mark.parametrize("secret_kind", ["mode", "symlink", "hardlink"])
def test_preflight_secret_metadata_fails_closed_without_reading(
    tmp_path: Path,
    secret_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, repo = _manifest(tmp_path)
    secret = Path(manifest.secret.path)
    if secret_kind == "mode":
        secret.chmod(0o640)
    elif secret_kind == "symlink":
        target = secret.with_name("actual.key")
        secret.rename(target)
        secret.symlink_to(target)
    else:
        os.link(secret, secret.with_name("second-secret-link.key"))
    original_open = Path.open

    def guarded_path_open(path, *args, **kwargs):
        if path == secret:
            raise AssertionError("metadata preflight opened secret content")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repo,
        now=_NOW,
    )
    by_id = {check.check_id: check.passed for check in report.checks}
    assert by_id["openai_secret_external_regular_0600"] is False
    assert report.secret_content_reads == 0
    assert report.eligible_for_post_preflight_factory is False


def test_report_seal_and_factory_use_one_owner_preflight_pricing_chain(tmp_path: Path) -> None:
    manifest, repo = _manifest(tmp_path)
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repo,
        now=_NOW,
    )
    with pytest.raises(PermissionError, match="module-owned"):
        replace(report, _seal=object())
    assert production_activation_available_v1() is True
    pricing_sha256 = "5" * 64
    factory = require_production_post_preflight_factory_v1(
        manifest,
        report,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        confirmed_preflight_report_sha256=production_preflight_report_sha256(report),
        confirmed_pricing_sha256=pricing_sha256,
    )
    assert factory.manifest_sha256 == authority_manifest_sha256(manifest)
    assert factory.preflight_report_sha256 == production_preflight_report_sha256(report)
    assert factory.pricing_binding_sha256 == pricing_sha256
    manifest_snapshot = factory.manifest_snapshot()
    assert manifest_snapshot is not manifest
    assert authority_manifest_sha256(manifest_snapshot) == factory.manifest_sha256
    object.__setattr__(manifest_snapshot.authorization, "authorization_id", "caller-drift")
    assert factory.manifest_snapshot().authorization.authorization_id == "owner-approval-fixture"
    lease = factory.issue_case_execution_lease(
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=PilotHostV1.QWEN3_VL,
        mode=SmokeModeV1.ACTIVE,
        case_id="qwen3_vl-active",
        task_id="smoke-task",
        task_parameters_sha256=None,
        reset_seed=None,
        actor_call_index=1,
        request_sha256="6" * 64,
        now=_NOW,
    )
    assert lease.pricing_binding_sha256 == pricing_sha256
    assert lease.task_id == "smoke-task"
    assert lease.task_parameters_sha256 is None
    assert lease.reset_seed is None
    assert lease.actor_call_index == 1
    with pytest.raises(ValueError, match="outside the owner-pinned manifest"):
        factory.issue_case_execution_lease(
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=PilotHostV1.QWEN3_VL,
            mode=SmokeModeV1.ACTIVE,
            case_id="qwen3_vl-active",
            task_id="another-task",
            task_parameters_sha256=None,
            reset_seed=None,
            actor_call_index=1,
            request_sha256="6" * 64,
            now=_NOW,
        )
    with pytest.raises(ValueError, match="outside the owner-pinned manifest"):
        factory.issue_case_execution_lease(
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=PilotHostV1.QWEN3_VL,
            mode=SmokeModeV1.OFF,
            case_id="qwen3_vl-off",
            task_id="smoke-task",
            task_parameters_sha256=None,
            reset_seed=None,
            actor_call_index=1,
            request_sha256="6" * 64,
            now=_NOW,
        )
    with pytest.raises(PermissionError, match="child-process-only"):
        factory.acquire_secret_lease(lease)


def test_case_execution_lease_cannot_be_caller_forged() -> None:
    with pytest.raises(PermissionError, match="module-owned"):
        CaseExecutionLeaseV1(
            schema_version=CASE_EXECUTION_LEASE_SCHEMA_VERSION,
            manifest_sha256="1" * 64,
            preflight_report_sha256="2" * 64,
            factory_binding_sha256="3" * 64,
            execution_scope=CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
            openai_stage_set_sha256="5" * 64,
            pricing_binding_sha256="6" * 64,
            stage=RunStageV1.QWEN_LIVE_SMOKE,
            host=PilotHostV1.QWEN3_VL,
            mode=SmokeModeV1.ACTIVE,
            case_id="qwen-active",
            task_id="smoke-task",
            task_parameters_sha256=None,
            reset_seed=None,
            actor_call_index=1,
            request_sha256="4" * 64,
            issued_at_utc="2026-09-03T00:00:00Z",
            expires_at_utc="2026-09-03T00:01:00Z",
            _seal=object(),
        )
    with pytest.raises(TypeError, match="module-owned"):
        case_execution_lease_projection(cast(CaseExecutionLeaseV1, object()))


@pytest.mark.parametrize(
    ("role", "openai_role"),
    [
        (LiveAttemptRoleV1.RUBRIC, OpenAIRoleV1.RUBRIC),
        (LiveAttemptRoleV1.HISTORY_POLICY, OpenAIRoleV1.HISTORY_POLICY),
    ],
)
def test_exact_role_bound_child_can_cancel_before_secret_or_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: LiveAttemptRoleV1,
    openai_role: OpenAIRoleV1,
) -> None:
    from mobile_world.runtime.sentinel.r2_4 import live_attempt as live_attempt_module

    original_get_context = live_attempt_module.multiprocessing.get_context
    process_start_methods: list[str | None] = []

    def observed_get_context(method: str | None = None):
        process_start_methods.append(method)
        return original_get_context(method)

    monkeypatch.setattr(live_attempt_module.multiprocessing, "get_context", observed_get_context)
    lease_now = datetime.now(UTC).replace(microsecond=0)
    manifest, repo = _manifest(tmp_path)
    report = run_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        repository_root=repo,
        now=_NOW,
    )
    pricing = LiveAttemptPricingV1(
        pricing_id="owner-cli-pin",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1_000_000,
        cached_input_usd_micros_per_million_tokens=100_000,
        output_usd_micros_per_million_tokens=2_000_000,
        source_sha256=_sha(b"operator-pinned-pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    factory = require_production_post_preflight_factory_v1(
        manifest,
        report,
        confirmed_manifest_sha256=authority_manifest_sha256(manifest),
        confirmed_preflight_report_sha256=production_preflight_report_sha256(report),
        confirmed_pricing_sha256=pricing_sha256,
    )
    lease = factory.issue_case_execution_lease(
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=PilotHostV1.QWEN3_VL,
        mode=SmokeModeV1.ACTIVE,
        case_id="qwen3_vl-active",
        task_id="smoke-task",
        task_parameters_sha256=None,
        reset_seed=None,
        actor_call_index=1,
        request_sha256=_sha(b"assembled-actor-request"),
        now=lease_now,
    )
    monkeypatch.setattr(
        SecureOpenAISecretLeaseV1,
        "_read_exact_secret",
        staticmethod(lambda _: (_ for _ in ()).throw(AssertionError("secret read"))),
    )
    sink = MemoryLiveAttemptReceiptSinkV1()
    runner = ProductionOpenAIAttemptRunnerV1(
        factory=factory,
        role=role,
        sink=sink,
        pricing=pricing,
        confirmed_pricing_sha256=pricing_sha256,
    )
    request = build_canonical_openai_request(_sealed_openai_request_kwargs(role))
    reservation = live_attempt_worst_case_cost_usd_micros(
        pricing,
        request_byte_count=request.byte_count,
        max_output_tokens=runner.openai_stage.max_output_tokens,
    )
    with pytest.raises(LiveAttemptError) as insufficient:
        runner.begin(
            case_lease=lease,
            attempt_id=f"insufficient-budget-{role.value.lower()}",
            logical_call_id=f"logical-insufficient-{role.value.lower()}",
            request=request,
            transport_binding_sha256=_sha(f"transport-{role.value}".encode()),
            deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
            max_cost_usd_micros=max(0, reservation - 1),
        )
    assert insufficient.value.code == "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY"
    rejected = sink.receipt_for(f"insufficient-budget-{role.value.lower()}")
    assert rejected.dispatch_count == 0
    assert rejected.cost_status is LiveAttemptCostStatusV1.EXACT
    assert rejected.cost_usd_micros == 0
    assert rejected.failure_code == "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY"
    # Manifest/topology fixture construction may itself use sealed child
    # processes.  Observe only the production attempt launches below.
    process_start_methods.clear()
    call = runner.begin(
        case_lease=lease,
        attempt_id=f"pre-dispatch-{role.value.lower()}",
        logical_call_id=f"logical-{role.value.lower()}",
        request=request,
        transport_binding_sha256=_sha(f"transport-{role.value}".encode()),
        deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
        max_cost_usd_micros=reservation,
    )

    receipt = call.cancel_and_join()

    assert runner.openai_stage.role is openai_role
    assert receipt.role is role
    assert receipt.stage_sha256 == runner.openai_stage_sha256
    assert receipt.status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
    assert receipt.dispatch_count == 0
    assert receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert receipt.cost_usd_micros == 0
    assert receipt.worker_reaped
    assert sink.receipts == (rejected, receipt)
    assert process_start_methods == ["spawn"]

    # A secret that drifts after preflight must fail before the SDK/provider
    # dispatch linearization point and remain exact zero-cost evidence.
    Path(manifest.secret.path).chmod(0o640)
    failed_call = runner.begin(
        case_lease=lease,
        attempt_id=f"secret-drift-{role.value.lower()}",
        logical_call_id=f"logical-secret-drift-{role.value.lower()}",
        request=request,
        transport_binding_sha256=_sha(f"transport-drift-{role.value}".encode()),
        deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
        max_cost_usd_micros=reservation,
    )
    with pytest.raises(LiveAttemptError) as secret_drift:
        failed_call()
    assert secret_drift.value.code == "PROVIDER_CHILD_FAILED"
    failed_receipt = failed_call.terminal_receipt
    assert failed_receipt is not None
    assert failed_receipt.status is LiveAttemptStatusV1.FAILED
    assert failed_receipt.dispatch_count == 0
    assert failed_receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert failed_receipt.cost_usd_micros == 0
    assert failed_receipt.worker_reaped
    assert sink.receipts == (rejected, receipt, failed_receipt)
    assert process_start_methods == ["spawn", "spawn"]

    # A second hard link added after the owner preflight is also a credential
    # lifecycle drift.  The child must reject it before the SDK dispatch point.
    Path(manifest.secret.path).chmod(0o600)
    os.link(
        manifest.secret.path,
        str(Path(manifest.secret.path).with_name("late-secret-hardlink.key")),
    )
    hardlink_call = runner.begin(
        case_lease=lease,
        attempt_id=f"secret-hardlink-drift-{role.value.lower()}",
        logical_call_id=f"logical-secret-hardlink-drift-{role.value.lower()}",
        request=request,
        transport_binding_sha256=_sha(f"transport-hardlink-{role.value}".encode()),
        deadline_monotonic_ns=time.monotonic_ns() + 5_000_000_000,
        max_cost_usd_micros=reservation,
    )
    with pytest.raises(LiveAttemptError) as hardlink_drift:
        hardlink_call()
    assert hardlink_drift.value.code == "PROVIDER_CHILD_FAILED"
    hardlink_receipt = hardlink_call.terminal_receipt
    assert hardlink_receipt is not None
    assert hardlink_receipt.status is LiveAttemptStatusV1.FAILED
    assert hardlink_receipt.dispatch_count == 0
    assert hardlink_receipt.cost_status is LiveAttemptCostStatusV1.EXACT
    assert hardlink_receipt.cost_usd_micros == 0
    assert hardlink_receipt.worker_reaped
    assert sink.receipts == (rejected, receipt, failed_receipt, hardlink_receipt)
    assert process_start_methods == ["spawn", "spawn", "spawn"]
