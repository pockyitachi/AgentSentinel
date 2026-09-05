from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]

from mobile_world.runtime.sentinel.r2_4 import authority_promotion, production_preflight, smoke_run
from mobile_world.runtime.sentinel.r2_4.authority_promotion import (
    AuthorityPromotionError,
    load_canonical_draft_smoke_authority_v1,
    load_owner_authorized_smoke_authority_v1,
    promote_draft_smoke_authority_v1,
    write_fresh_owner_smoke_authority_v1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptPricingV1,
    live_attempt_pricing_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    build_owner_authorized_live_per_call_policy_v1,
    build_production_live_budget_ledger_v1,
    issue_owner_authorized_live_policy_authority,
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
    SmokeModeV1,
    SnapshotResourceV1,
    compute_snapshot_tree_digest,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    r24_smoke_production_preflight_report_projection,
    r24_smoke_production_preflight_report_sha256,
    require_production_post_preflight_factory_v1,
    run_r24_smoke_production_preflight_v1,
)
from mobile_world.runtime.sentinel.r2_4.smoke_run import (
    R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
    R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS,
    R24SmokeOwnerAuthorizationV1,
    R24SmokeRunAuthorityManifestV1,
    R24SmokeSequenceSafetyV1,
    SequenceExecutionScopeV1,
    parse_smoke_authority_manifest,
    smoke_authority_manifest_projection,
    smoke_authority_manifest_sha256,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


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
    secret = tmp_path / "secret" / "openai.key"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text("not-read", encoding="utf-8")
    secret.chmod(0o600)
    plans = (
        _plan(tmp_path, PilotHostV1.QWEN3_VL),
        _plan(tmp_path, PilotHostV1.MAI_UI),
    )
    return R24SmokeRunAuthorityManifestV1(
        schema_version=R24_SMOKE_AUTHORITY_SCHEMA_VERSION,
        execution_scope=SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY,
        run_id="r24-smoke-only",
        source_commit="a" * 40,
        authorization=R24SmokeOwnerAuthorizationV1(
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
            authorization_id="r24-smoke-owner",
            authorized_by="owner",
            issued_at_utc="2026-09-04T00:00:00Z",
            expires_at_utc="2099-09-05T00:00:00Z",
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
        runtime_config_sha256=_sha("shared-gpu-runtime-config"),
        output_root=str(tmp_path / "output"),
        max_resource_preflight_wall_time_seconds=100,
        max_qwen_to_mai_handoff_wall_time_seconds=900,
        max_resource_cleanup_wall_time_seconds=120,
        max_sequence_wall_time_seconds=1480,
        max_sequence_openai_calls=12,
        max_sequence_actor_calls=6,
        max_sequence_cost_usd_micros=600,
    )


def _validator() -> Draft202012Validator:
    schema_path = (
        REPOSITORY_ROOT
        / "mobileworld_audit_handoff/schemas/r2_4/smoke_run_authority.v1.schema.json"
    )
    legacy_path = (
        REPOSITORY_ROOT
        / "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store={legacy["$id"]: legacy}),
    )


def test_smoke_authority_is_closed_pilot_free_and_schema_valid(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    projection = smoke_authority_manifest_projection(manifest)
    _validator().validate(projection)
    assert parse_smoke_authority_manifest(projection) == manifest
    assert smoke_authority_manifest_sha256(parse_smoke_authority_manifest(projection)) == (
        smoke_authority_manifest_sha256(manifest)
    )
    forbidden = {
        "pilot",
        "cohort",
        "task_source",
        "topology_comparison_artifact_sha256",
    }
    assert forbidden.isdisjoint(projection)
    assert "pilot_gui_actions_allowed" not in projection["authorization"]
    assert projection["execution_scope"] == "R24_LIVE_SMOKE_ONLY"
    assert projection["resource_topology"] == "SINGLE_GPU_SEQUENTIAL_SHARED"


@pytest.mark.parametrize("field", ["pilot", "cohort", "topology_comparison_artifact_sha256"])
def test_smoke_authority_rejects_pilot_field_injection(tmp_path: Path, field: str) -> None:
    projection = smoke_authority_manifest_projection(_manifest(tmp_path))
    projection[field] = None
    with pytest.raises(LiveRunContractError, match="INVALID_FIELDS"):
        parse_smoke_authority_manifest(projection)
    assert list(_validator().iter_errors(projection))


def test_smoke_authority_binds_shared_runtime_and_handoff_budget(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(LiveRunContractError, match="INVALID_RESOURCE_TOPOLOGY"):
        replace(manifest, resource_topology="INDEPENDENT_GPU_CONCURRENT")
    with pytest.raises(LiveRunContractError, match="BUDGET_BINDING_MISMATCH"):
        replace(
            manifest, max_sequence_wall_time_seconds=manifest.max_sequence_wall_time_seconds - 1
        )
    with pytest.raises(LiveRunContractError, match="INVALID_SHA256"):
        replace(manifest, runtime_config_sha256="0" * 63)
    with pytest.raises(LiveRunContractError, match="INVALID_BOUND"):
        replace(
            manifest,
            max_resource_cleanup_wall_time_seconds=(R24_SMOKE_MIN_CLEANUP_RESERVE_SECONDS - 1),
        )
    schema = json.loads(
        (
            REPOSITORY_ROOT
            / "mobileworld_audit_handoff/schemas/r2_4/smoke_run_authority.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert schema["properties"]["max_resource_cleanup_wall_time_seconds"]["minimum"] == 8


def test_smoke_promotion_changes_only_status_and_writes_canonical_0600(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    draft = _manifest(tmp_path)
    draft_path = tmp_path / "draft.json"
    draft_path.write_bytes(canonical_json_bytes(smoke_authority_manifest_projection(draft)))
    draft_path.chmod(0o600)
    loaded = load_canonical_draft_smoke_authority_v1(
        draft_path,
        repository_root=repository,
    )
    promoted = promote_draft_smoke_authority_v1(
        loaded,
        confirmed_draft_sha256=smoke_authority_manifest_sha256(loaded),
    )
    before = smoke_authority_manifest_projection(loaded)
    after = smoke_authority_manifest_projection(promoted)
    assert before["authorization"]["status"] == "DRAFT_NOT_AUTHORIZED"
    before["authorization"]["status"] = "OWNER_AUTHORIZED"
    assert after == before
    output = tmp_path / "authorized.json"
    digest = write_fresh_owner_smoke_authority_v1(
        promoted,
        output,
        repository_root=repository,
    )
    assert digest == smoke_authority_manifest_sha256(promoted)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() == canonical_json_bytes(after)
    assert load_owner_authorized_smoke_authority_v1(output, repository_root=repository) == promoted


def test_smoke_promotion_rejects_wrong_confirmation(tmp_path: Path) -> None:
    with pytest.raises(AuthorityPromotionError, match="DRAFT_CONFIRMATION_MISMATCH"):
        promote_draft_smoke_authority_v1(
            _manifest(tmp_path),
            confirmed_draft_sha256="0" * 64,
        )


def test_permissive_smoke_manifest_loader_is_not_public() -> None:
    assert not hasattr(smoke_run, "load_smoke_authority_manifest")
    assert "load_smoke_authority_manifest" not in smoke_run.__all__


@pytest.mark.parametrize("kind", ["mode", "repository", "symlink"])
def test_production_smoke_authority_loader_rejects_unsafe_files(
    tmp_path: Path,
    kind: str,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    draft = _manifest(tmp_path)
    manifest = replace(
        draft,
        authorization=replace(
            draft.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    external = tmp_path / "authorized.json"
    external.write_bytes(canonical_json_bytes(smoke_authority_manifest_projection(manifest)))
    external.chmod(0o600)
    candidate = external
    if kind == "mode":
        external.chmod(0o640)
    elif kind == "repository":
        candidate = repository / "authorized.json"
        candidate.write_bytes(external.read_bytes())
        candidate.chmod(0o600)
    else:
        candidate = tmp_path / "authorized-link.json"
        candidate.symlink_to(external)
    with pytest.raises(AuthorityPromotionError):
        load_owner_authorized_smoke_authority_v1(candidate, repository_root=repository)


def test_production_smoke_authority_loader_rejects_chmod_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    draft = _manifest(tmp_path)
    manifest = replace(
        draft,
        authorization=replace(
            draft.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    authority = tmp_path / "authorized.json"
    authority.write_bytes(canonical_json_bytes(smoke_authority_manifest_projection(manifest)))
    authority.chmod(0o600)
    real_read = authority_promotion.os.read
    changed = False

    def _chmod_after_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, count)
        if chunk and not changed:
            changed = True
            authority.chmod(0o640)
        return chunk

    monkeypatch.setattr(authority_promotion.os, "read", _chmod_after_read)
    with pytest.raises(AuthorityPromotionError, match="INVALID_DRAFT_FILE"):
        load_owner_authorized_smoke_authority_v1(authority, repository_root=repository)
    assert changed


def test_production_smoke_authority_loader_rejects_path_symlink_swap_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    draft = _manifest(tmp_path)
    manifest = replace(
        draft,
        authorization=replace(
            draft.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    authority = tmp_path / "authorized.json"
    authority.write_bytes(canonical_json_bytes(smoke_authority_manifest_projection(manifest)))
    authority.chmod(0o600)
    moved = tmp_path / "authorized-moved.json"
    real_read = authority_promotion.os.read
    swapped = False

    def _swap_after_read(descriptor: int, count: int) -> bytes:
        nonlocal swapped
        chunk = real_read(descriptor, count)
        if chunk and not swapped:
            swapped = True
            authority.rename(moved)
            authority.symlink_to(moved)
        return chunk

    monkeypatch.setattr(authority_promotion.os, "read", _swap_after_read)
    with pytest.raises(AuthorityPromotionError, match="INVALID_DRAFT_FILE"):
        load_owner_authorized_smoke_authority_v1(authority, repository_root=repository)
    assert swapped


def test_production_smoke_authority_loader_rejects_repo_hardlink_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    draft = _manifest(tmp_path)
    manifest = replace(
        draft,
        authorization=replace(
            draft.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
    )
    repository_authority = repository / "authorized.json"
    repository_authority.write_bytes(
        canonical_json_bytes(smoke_authority_manifest_projection(manifest))
    )
    repository_authority.chmod(0o600)
    external_alias = tmp_path / "authorized-hardlink.json"
    try:
        os.link(repository_authority, external_alias)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    def _unexpected_read(_: int, __: int) -> bytes:
        raise AssertionError("unsafe hardlink must be rejected before content read")

    monkeypatch.setattr(authority_promotion.os, "read", _unexpected_read)
    with pytest.raises(AuthorityPromotionError, match="INVALID_DRAFT_FILE"):
        load_owner_authorized_smoke_authority_v1(external_alias, repository_root=repository)


def _materialized_owner_manifest(tmp_path: Path) -> R24SmokeRunAuthorityManifestV1:
    manifest = _manifest(tmp_path)
    resources: list[SnapshotResourceV1] = []
    for resource in manifest.actor_resources:
        snapshot = Path(resource.snapshot_path)
        snapshot.mkdir(parents=True)
        (snapshot / "weights.bin").write_bytes(resource.host.value.encode())
        digest = compute_snapshot_tree_digest(resource)
        resources.append(
            replace(
                resource,
                snapshot_tree_sha256=digest.sha256,
                snapshot_total_bytes=digest.total_bytes,
                snapshot_file_count=digest.file_count,
            )
        )
    plans: list[HostLiveSmokePlanV1] = []
    for plan in manifest.smoke_plans:
        cases: list[LiveSmokeCaseV1] = []
        for case in plan.cases:
            payload = f"fixture:{plan.host.value}:{case.mode.value}".encode()
            path = Path(case.request_fixture_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            cases.append(
                replace(
                    case,
                    request_fixture_sha256=hashlib.sha256(payload).hexdigest(),
                    request_fixture_byte_count=len(payload),
                )
            )
        plans.append(replace(plan, cases=tuple(cases)))
    return replace(
        manifest,
        authorization=replace(
            manifest.authorization,
            status=RunAuthorizationStatusV1.OWNER_AUTHORIZED,
        ),
        actor_resources=tuple(resources),
        smoke_plans=tuple(plans),
    )


def _install_content_read_spies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    secret_path: Path,
) -> list[str]:
    secret_metadata = secret_path.stat()
    secret_identity = (secret_metadata.st_dev, secret_metadata.st_ino)
    reads: list[str] = []
    original_read = os.read
    original_path_open = Path.open

    def read_spy(descriptor: int, byte_count: int) -> bytes:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        reads.append(f"descriptor:{identity[0]}:{identity[1]}")
        if identity == secret_identity:
            raise AssertionError("preflight read the secret descriptor")
        return original_read(descriptor, byte_count)

    def path_open_spy(path: Path, *args: object, **kwargs: object) -> object:
        try:
            metadata = path.stat()
        except OSError:
            metadata = None
        if metadata is not None:
            identity = (metadata.st_dev, metadata.st_ino)
            reads.append(f"path:{identity[0]}:{identity[1]}")
            if identity == secret_identity:
                raise AssertionError("preflight opened the secret as content")
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(production_preflight.os, "read", read_spy)
    monkeypatch.setattr(Path, "open", path_open_spy)
    return reads


def _run_smoke_preflight(
    manifest: R24SmokeRunAuthorityManifestV1,
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> object:
    repository = tmp_path / "preflight-repository"
    repository.mkdir()
    monkeypatch.setattr(
        production_preflight,
        "_git_state",
        lambda root: (manifest.source_commit, True),
    )
    return run_r24_smoke_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        confirmed_runtime_config_sha256=manifest.runtime_config_sha256,
        repository_root=repository,
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


def _assert_disjointness_failure_without_content_reads(
    report: object,
    reads: list[str],
) -> None:
    assert isinstance(report, production_preflight.R24SmokeProductionPreflightReportV1)
    by_id = {check.check_id: check.passed for check in report.checks}
    assert not by_id["content_read_inputs_disjoint_from_secret"]
    assert not report.all_checks_passed
    assert report.secret_content_reads == 0
    assert reads == []
    assert not any(
        passed
        for check_id, passed in by_id.items()
        if check_id.startswith("smoke_fixture:") or check_id.startswith("snapshot_content:")
    )


def test_smoke_preflight_does_not_read_direct_secret_fixture_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _materialized_owner_manifest(tmp_path)
    secret = Path(manifest.secret.path)
    payload = secret.read_bytes()
    plan = manifest.smoke_plans[0]
    alias = replace(
        plan.cases[0],
        request_fixture_path=str(secret),
        request_fixture_sha256=hashlib.sha256(payload).hexdigest(),
        request_fixture_byte_count=len(payload),
    )
    manifest = replace(
        manifest,
        smoke_plans=(replace(plan, cases=(alias, *plan.cases[1:])), manifest.smoke_plans[1]),
    )
    reads = _install_content_read_spies(monkeypatch, secret_path=secret)
    report = _run_smoke_preflight(manifest, tmp_path=tmp_path, monkeypatch=monkeypatch)
    _assert_disjointness_failure_without_content_reads(report, reads)


def test_smoke_preflight_does_not_read_hardlinked_secret_fixture_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _materialized_owner_manifest(tmp_path)
    secret = Path(manifest.secret.path)
    payload = secret.read_bytes()
    alias_path = tmp_path / "inputs" / "secret-hardlink.json"
    try:
        os.link(secret, alias_path)
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")
    plan = manifest.smoke_plans[0]
    alias = replace(
        plan.cases[0],
        request_fixture_path=str(alias_path),
        request_fixture_sha256=hashlib.sha256(payload).hexdigest(),
        request_fixture_byte_count=len(payload),
    )
    manifest = replace(
        manifest,
        smoke_plans=(replace(plan, cases=(alias, *plan.cases[1:])), manifest.smoke_plans[1]),
    )
    reads = _install_content_read_spies(monkeypatch, secret_path=secret)
    report = _run_smoke_preflight(manifest, tmp_path=tmp_path, monkeypatch=monkeypatch)
    _assert_disjointness_failure_without_content_reads(report, reads)


def test_smoke_preflight_rechecks_fixture_descriptor_after_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _materialized_owner_manifest(tmp_path)
    secret = Path(manifest.secret.path)
    fixture = Path(manifest.smoke_plans[0].cases[0].request_fixture_path)
    reads = _install_content_read_spies(monkeypatch, secret_path=secret)
    original_current_check = production_preflight._secret_binding_is_current
    swapped = False

    def swap_after_metadata_gate(binding: object) -> bool:
        nonlocal swapped
        result = original_current_check(binding)
        if not swapped:
            fixture.unlink()
            try:
                os.link(secret, fixture)
            except OSError as exc:
                pytest.skip(f"hardlinks unavailable: {exc}")
            swapped = True
        return result

    monkeypatch.setattr(
        production_preflight,
        "_secret_binding_is_current",
        swap_after_metadata_gate,
    )
    report = _run_smoke_preflight(manifest, tmp_path=tmp_path, monkeypatch=monkeypatch)
    assert isinstance(report, production_preflight.R24SmokeProductionPreflightReportV1)
    by_id = {check.check_id: check.passed for check in report.checks}
    assert not by_id["smoke_fixture:QWEN3_VL:OFF"]
    assert not report.all_checks_passed
    assert report.secret_content_reads == 0
    assert reads == []


def test_smoke_preflight_does_not_read_secret_in_snapshot_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _materialized_owner_manifest(tmp_path)
    secret = Path(manifest.actor_resources[0].snapshot_path) / "weights.bin"
    secret.chmod(0o600)
    manifest = replace(manifest, secret=replace(manifest.secret, path=str(secret)))
    reads = _install_content_read_spies(monkeypatch, secret_path=secret)
    report = _run_smoke_preflight(manifest, tmp_path=tmp_path, monkeypatch=monkeypatch)
    _assert_disjointness_failure_without_content_reads(report, reads)


def test_smoke_preflight_does_not_follow_snapshot_symlink_to_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _materialized_owner_manifest(tmp_path)
    secret = Path(manifest.secret.path)
    symlink = Path(manifest.actor_resources[0].snapshot_path) / "secret-link.bin"
    symlink.symlink_to(secret)
    reads = _install_content_read_spies(monkeypatch, secret_path=secret)
    report = _run_smoke_preflight(manifest, tmp_path=tmp_path, monkeypatch=monkeypatch)
    _assert_disjointness_failure_without_content_reads(report, reads)


def _passing_report_and_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[R24SmokeRunAuthorityManifestV1, object, object, LiveAttemptPricingV1]:
    repository = tmp_path / "repo"
    repository.mkdir()
    manifest = _materialized_owner_manifest(tmp_path)
    monkeypatch.setattr(
        production_preflight,
        "_git_state",
        lambda root: (manifest.source_commit, True),
    )
    report = run_r24_smoke_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        confirmed_runtime_config_sha256=manifest.runtime_config_sha256,
        repository_root=repository,
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    pricing = LiveAttemptPricingV1(
        pricing_id="smoke-pricing",
        model="gpt-5.6-sol",
        input_usd_micros_per_million_tokens=1,
        cached_input_usd_micros_per_million_tokens=1,
        output_usd_micros_per_million_tokens=1,
        source_sha256=_sha("pricing-source"),
        effective_at_utc="2026-09-04T00:00:00Z",
    )
    factory = require_production_post_preflight_factory_v1(
        manifest,
        report,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        confirmed_preflight_report_sha256=r24_smoke_production_preflight_report_sha256(report),
        confirmed_pricing_sha256=live_attempt_pricing_sha256(pricing),
    )
    return manifest, report, factory, pricing


def test_smoke_preflight_and_factory_never_need_pilot_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, report, factory, pricing = _passing_report_and_factory(tmp_path, monkeypatch)
    projection = r24_smoke_production_preflight_report_projection(report)
    assert report.all_checks_passed
    assert projection["execution_scope"] == "R24_LIVE_SMOKE_ONLY"
    assert projection["authorized_stages"] == [
        "RESOURCE_PREFLIGHT",
        "QWEN_LIVE_SMOKE",
        "MAI_LIVE_SMOKE",
    ]
    assert all("pilot" not in key and "cohort" not in key for key in projection)
    assert factory.runtime_config_sha256 == manifest.runtime_config_sha256
    assert factory.sequence_execution_scope is SequenceExecutionScopeV1.R24_LIVE_SMOKE_ONLY
    assert factory.openai_stage(OpenAIRoleV1.RUBRIC).role is OpenAIRoleV1.RUBRIC
    assert factory.openai_stage(OpenAIRoleV1.HISTORY_POLICY).role is OpenAIRoleV1.HISTORY_POLICY
    ledger = build_production_live_budget_ledger_v1(factory)
    assert ledger is not None
    case = manifest.smoke_plans[0].cases[1]
    policy = build_owner_authorized_live_per_call_policy_v1(
        factory=factory,
        pricing=pricing,
        budget_ledger=ledger,
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=PilotHostV1.QWEN3_VL,
        mode=SmokeModeV1.SHADOW,
        case_id=case.case_id,
        case_deadline_monotonic_ns=time.monotonic_ns() + 30_000_000_000,
    )
    assert policy is not None
    live_authority = issue_owner_authorized_live_policy_authority(
        manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    assert live_authority.manifest_snapshot() == manifest


def test_smoke_preflight_rejects_runtime_hash_draft_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    manifest = _materialized_owner_manifest(tmp_path)
    monkeypatch.setattr(
        production_preflight,
        "_git_state",
        lambda root: (manifest.source_commit, True),
    )
    drift = run_r24_smoke_production_preflight_v1(
        manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
        confirmed_runtime_config_sha256="0" * 64,
        repository_root=repository,
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    assert not drift.eligible_for_post_preflight_factory
    with pytest.raises(ValueError, match="post-preflight factory bindings differ"):
        require_production_post_preflight_factory_v1(
            manifest,
            drift,
            confirmed_manifest_sha256=smoke_authority_manifest_sha256(manifest),
            confirmed_preflight_report_sha256=r24_smoke_production_preflight_report_sha256(drift),
            confirmed_pricing_sha256=_sha("pricing"),
        )
    draft = replace(
        manifest,
        authorization=replace(
            manifest.authorization,
            status=RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED,
        ),
    )
    draft_report = run_r24_smoke_production_preflight_v1(
        draft,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(draft),
        confirmed_runtime_config_sha256=draft.runtime_config_sha256,
        repository_root=repository,
        now=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    assert not draft_report.eligible_for_post_preflight_factory
    with pytest.raises(ValueError, match="post-preflight factory bindings differ"):
        require_production_post_preflight_factory_v1(
            draft,
            draft_report,
            confirmed_manifest_sha256=smoke_authority_manifest_sha256(draft),
            confirmed_preflight_report_sha256=r24_smoke_production_preflight_report_sha256(
                draft_report
            ),
            confirmed_pricing_sha256=_sha("pricing"),
        )
    expired_manifest = replace(
        manifest,
        authorization=replace(
            manifest.authorization,
            expires_at_utc="2026-09-05T00:00:00Z",
        ),
    )
    expired = run_r24_smoke_production_preflight_v1(
        expired_manifest,
        confirmed_manifest_sha256=smoke_authority_manifest_sha256(expired_manifest),
        confirmed_runtime_config_sha256=expired_manifest.runtime_config_sha256,
        repository_root=repository,
        now=datetime(2026, 9, 6, 12, tzinfo=UTC),
    )
    assert not expired.eligible_for_post_preflight_factory
    with pytest.raises(ValueError, match="post-preflight factory bindings differ"):
        require_production_post_preflight_factory_v1(
            expired_manifest,
            expired,
            confirmed_manifest_sha256=smoke_authority_manifest_sha256(expired_manifest),
            confirmed_preflight_report_sha256=r24_smoke_production_preflight_report_sha256(expired),
            confirmed_pricing_sha256=_sha("pricing"),
        )


def test_smoke_factory_issues_smoke_lease_but_rejects_pilot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, factory, _ = _passing_report_and_factory(tmp_path, monkeypatch)
    for stage, plan in zip(
        (RunStageV1.QWEN_LIVE_SMOKE, RunStageV1.MAI_LIVE_SMOKE),
        manifest.smoke_plans,
        strict=True,
    ):
        case = plan.cases[1]
        lease = factory.issue_case_execution_lease(
            stage=stage,
            host=plan.host,
            mode=case.mode,
            case_id=case.case_id,
            task_id=case.task_id,
            task_parameters_sha256=None,
            reset_seed=None,
            actor_call_index=1,
            request_sha256=_sha(f"request:{stage.value}"),
            now=datetime(2026, 9, 4, 12, tzinfo=UTC),
        )
        assert lease.stage is stage
    with pytest.raises(ValueError, match="outside the owner-pinned manifest"):
        factory.issue_case_execution_lease(
            stage=RunStageV1.R25_PILOT,
            host=PilotHostV1.QWEN3_VL,
            mode=SmokeModeV1.ACTIVE,
            case_id="pilot-cell-000",
            task_id="pilot-task",
            task_parameters_sha256=_sha("params"),
            reset_seed=1,
            actor_call_index=1,
            request_sha256=_sha("pilot-request"),
            now=datetime(2026, 9, 4, 12, tzinfo=UTC),
        )
