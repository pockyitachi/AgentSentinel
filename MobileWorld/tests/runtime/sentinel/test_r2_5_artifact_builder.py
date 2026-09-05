from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from _r2_4_topology_fixture import write_cpu_topology_artifact
from jsonschema import (  # type: ignore[import-untyped]
    Draft202012Validator,
    RefResolver,
    ValidationError,
)

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.authority_promotion import (
    AuthorityPromotionError,
    load_canonical_draft_authority_v1,
    promote_draft_authority_v1,
    write_fresh_owner_authority_v1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import canonical_json_bytes
from mobile_world.runtime.sentinel.r2_4.live_run import (
    OpenAIRoleV1,
    RunAuthorizationStatusV1,
    SmokeModeV1,
    authority_manifest_projection,
    authority_manifest_sha256,
    parse_authority_manifest,
)
from mobile_world.runtime.sentinel.r2_5.artifact_builder import (
    ARTIFACT_BUNDLE_FILENAME,
    COHORT_SELECTION_ALGORITHM,
    COHORT_SELECTION_FILENAME,
    GUI_ONLY_TASK_SOURCE_FILENAME,
    PILOT_TASK_SOURCE_FILENAME,
    RUN_AUTHORITY_MANIFEST_FILENAME,
    TOPOLOGY_COMPARISON_FILENAME,
    AuthorityArtifactInputsV1,
    R25ArtifactBuildError,
    RegistryTaskMetadataV1,
    RegistryTaskTimeDependencyV1,
    SnapshotDeclarationV1,
    artifact_bundle_output,
    artifact_bundle_projection,
    build_authority_artifact_bundle,
    cohort_selection_projection,
    cohort_selection_sha256,
    current_registry_metadata,
    parse_cohort_selection,
    select_gui_only_cohort,
    write_artifact_bundle,
)
from mobile_world.runtime.sentinel.r2_5.pilot import (
    EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION,
    frozen_pilot_manifest_projection,
    parse_frozen_pilot_manifest,
    resolve_pilot_task_inputs_v1,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_PATHS = (
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_4/topology_comparison.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_4/cpu_topology_artifact.v1.schema.json",
    REPOSITORY_ROOT
    / "mobileworld_audit_handoff/schemas/r2_4/run_authority_manifest.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/frozen_pilot_manifest.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/cohort_selection.v1.schema.json",
    REPOSITORY_ROOT
    / "mobileworld_audit_handoff/schemas/r2_5/executable_task_source.v1.schema.json",
    REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/r2_5/artifact_bundle.v1.schema.json",
)


def _schemas() -> tuple[dict[str, object], ...]:
    return tuple(json.loads(path.read_text(encoding="utf-8")) for path in SCHEMA_PATHS)


def _validator(schema: dict[str, object]) -> Draft202012Validator:
    schemas = _schemas()
    store = {str(item["$id"]): item for item in schemas}
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store=store),
    )


def _record(
    task_id: str,
    *,
    tags: tuple[str, ...] = (),
    apps: tuple[str, ...] = ("Settings",),
    time_dependency: RegistryTaskTimeDependencyV1 = (
        RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
    ),
) -> RegistryTaskMetadataV1:
    return RegistryTaskMetadataV1(
        task_id=task_id,
        task_tags=tags,
        app_names=apps,
        task_time_dependency=time_dependency,
        definition_source_sha256=hashlib.sha256(f"source:{task_id}".encode()).hexdigest(),
    )


def _source_and_registry(tmp_path: Path) -> tuple[Path, tuple[RegistryTaskMetadataV1, ...]]:
    task_ids = [f"GuiTask{index:02d}" for index in range(23)]
    source_rows: list[dict[str, JsonValue]] = [
        {"task_name": task_id, "trial": 1} for task_id in task_ids
    ]
    source_rows.extend(
        (
            {"task_name": "MissingTask", "trial": 1},
            {"task_name": "NeedsAskUserTask", "trial": 1},
            {"task_name": "TaggedInteractionTask", "trial": 1},
            {"task_name": "TaggedMcpTask", "trial": 1},
            {"task_name": "McpAppTask", "trial": 1},
        )
    )
    source = tmp_path / "gui-only.jsonl"
    source.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in source_rows),
        encoding="utf-8",
    )
    records = [_record(task_id) for task_id in task_ids]
    records.extend(
        (
            _record("NeedsAskUserTask"),
            _record("TaggedInteractionTask", tags=("agent-user-interaction",)),
            _record("TaggedMcpTask", tags=("agent-mcp",)),
            _record("McpAppTask", apps=("MCP-Github",)),
        )
    )
    records.sort(key=lambda item: item.task_id.encode("utf-8"))
    return source, tuple(records)


def _snapshot(tmp_path: Path, name: str, port: int) -> SnapshotDeclarationV1:
    return SnapshotDeclarationV1(
        snapshot_path=str(tmp_path / "models" / name / "snapshot"),
        snapshot_storage_root=str(tmp_path / "models" / name),
        snapshot_tree_sha256=hashlib.sha256(name.encode()).hexdigest(),
        snapshot_total_bytes=123,
        snapshot_file_count=2,
        actor_endpoint=f"http://127.0.0.1:{port}/v1",
        served_model_id=f"fixture-{name}",
    )


def _inputs(tmp_path: Path) -> tuple[AuthorityArtifactInputsV1, tuple[RegistryTaskMetadataV1, ...]]:
    source, records = _source_and_registry(tmp_path)
    repository = tmp_path / "repository"
    external = tmp_path / "external"
    repository.mkdir()
    external.mkdir()
    qwen_fixture = tmp_path / "qwen-request.json"
    mai_fixture = tmp_path / "mai-request.json"
    qwen_fixture.write_bytes(b'{"host":"qwen"}')
    mai_fixture.write_bytes(b'{"host":"mai"}')
    topology_artifact = tmp_path / "topology-comparison.json"
    write_cpu_topology_artifact(topology_artifact)
    return (
        AuthorityArtifactInputsV1(
            source_task_jsonl=source,
            repository_root=repository,
            bundle_directory=external / "authority-bundle",
            runtime_output_root=external / "runtime-output",
            secret_file=external / "not-created-or-read.key",
            topology_comparison_artifact=topology_artifact,
            qwen_snapshot=_snapshot(tmp_path, "qwen", 18_081),
            mai_snapshot=_snapshot(tmp_path, "mai", 18_082),
            qwen_smoke_fixture=qwen_fixture,
            mai_smoke_fixture=mai_fixture,
            qwen_smoke_task_id="GuiTask00",
            mai_smoke_task_id="GuiTask01",
            source_commit="a" * 40,
            cohort_id="r25-deterministic-20",
            run_id="r24-r25-draft",
            frozen_at_utc="2026-09-03T03:00:00Z",
            authorization_id="pending-owner-authorization",
            authorized_by="owner-pending",
            issued_at_utc="2026-09-03T03:00:00Z",
            expires_at_utc="2026-09-10T03:00:00Z",
        ),
        records,
    )


def test_selection_is_deterministic_and_excludes_non_gui_dependencies(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)

    first = select_gui_only_cohort(source, records)
    second = select_gui_only_cohort(source, tuple(reversed(records)))

    assert first == second
    assert len(first.members) == 20
    assert first.eligible_task_count == 23
    assert first.excluded_missing_registry == 1
    assert first.excluded_user_interaction == 2
    assert first.excluded_mcp == 2
    assert first.excluded_dynamic_time == 0
    assert len(first.source_task_audit) == 28
    assert tuple(record.source_row_index for record in first.source_task_audit) == tuple(
        range(1, 29)
    )
    assert sum(record.disposition.value == "ELIGIBLE" for record in first.source_task_audit) == 23
    assert len({member.task_id for member in first.members}) == 20
    assert all(1 <= member.reset_seed <= 2_147_483_647 for member in first.members)
    assert all(member.task_id.startswith("GuiTask") for member in first.members)


def test_selection_excludes_dynamic_or_unknown_wall_clock_tasks(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    dynamic = replace(
        records[0],
        task_time_dependency=(RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK),
    )
    audited = (dynamic, *records[1:])

    selection = select_gui_only_cohort(source, audited)

    assert selection.excluded_dynamic_time == 1
    assert selection.eligible_task_count == 22
    assert dynamic.task_id not in {member.task_id for member in selection.members}


def test_current_registry_time_audit_is_conservative_and_source_bound() -> None:
    records = {record.task_id: record for record in current_registry_metadata()}

    assert records["MattermostTechnicalDebtTriageTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
    )
    assert records["CheckGithubInfoTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.DYNAMIC_OR_UNKNOWN_WALL_CLOCK
    )
    assert records["ScheduleLunchViaSmsTask"].task_time_dependency is (
        RegistryTaskTimeDependencyV1.STATIC_WALL_CLOCK_INDEPENDENT
    )
    assert all(len(record.definition_source_sha256) == 64 for record in records.values())


def test_bundle_has_executable_inline_source_and_80_matched_cells(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)

    bundle = build_authority_artifact_bundle(inputs, records)

    assert bundle.task_source["schema_version"] == EXECUTABLE_PILOT_TASK_SOURCE_SCHEMA_VERSION
    raw_tasks = bundle.task_source["tasks"]
    assert isinstance(raw_tasks, list)
    assert len(raw_tasks) == 20
    assert len(bundle.pilot_manifest.cells) == 80
    for item in raw_tasks:
        assert isinstance(item, dict)
        source = item["parameter_source"]
        assert isinstance(source, dict)
        assert source["kind"] == "INLINE_CANONICAL_JSON"
        payload = source["payload"]
        assert isinstance(payload, dict)
        assert source["sha256"] == hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    authority = bundle.authority_manifest
    assert authority.authorization.status is RunAuthorizationStatusV1.DRAFT_NOT_AUTHORIZED
    assert tuple(stage.role for stage in authority.openai_stages) == (
        OpenAIRoleV1.RUBRIC,
        OpenAIRoleV1.HISTORY_POLICY,
    )
    assert tuple(case.mode for plan in authority.smoke_plans for case in plan.cases) == (
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
        SmokeModeV1.OFF,
        SmokeModeV1.SHADOW,
        SmokeModeV1.ACTIVE,
    )
    assert parse_authority_manifest(authority_manifest_projection(authority)) == authority
    assert (
        authority.topology_comparison_artifact_sha256
        == bundle.pilot_manifest.topology_comparison_artifact_sha256
    )
    assert bundle.pilot_manifest.dynamic_wall_clock_tasks_excluded is True
    assert not inputs.secret_file.exists()


def test_persisted_authority_artifacts_match_schemas_and_module_round_trips(
    tmp_path: Path,
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    schemas = _schemas()
    by_id = {str(schema["$id"]): schema for schema in schemas}
    for schema in schemas:
        Draft202012Validator.check_schema(schema)

    frozen = frozen_pilot_manifest_projection(bundle.pilot_manifest)
    authority = authority_manifest_projection(bundle.authority_manifest)
    executable = bundle.task_source
    selection = cohort_selection_projection(bundle.selection)
    bundle_output = artifact_bundle_output(bundle)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/frozen_pilot_manifest.v1.schema.json"]
    ).validate(frozen)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_4/run_authority_manifest.v1.schema.json"]
    ).validate(authority)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/cohort_selection.v1.schema.json"]
    ).validate(selection)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/executable_task_source.v1.schema.json"]
    ).validate(executable)
    _validator(
        by_id["https://agentsentinel.local/schemas/r2_5/artifact_bundle.v1.schema.json"]
    ).validate(bundle_output)

    assert parse_frozen_pilot_manifest(json.loads(canonical_json_bytes(frozen))) == (
        bundle.pilot_manifest
    )
    assert parse_authority_manifest(json.loads(canonical_json_bytes(authority))) == (
        bundle.authority_manifest
    )
    assert parse_cohort_selection(json.loads(canonical_json_bytes(selection))) == bundle.selection
    assert cohort_selection_sha256(bundle.selection) == (
        bundle.pilot_manifest.cohort_selection_artifact_sha256
    )
    projected_bundle = artifact_bundle_projection(bundle)
    assert (
        bundle_output["artifact_bundle_sha256"]
        == hashlib.sha256(canonical_json_bytes(projected_bundle)).hexdigest()
    )

    tampered = json.loads(canonical_json_bytes(bundle_output))
    tampered["artifact_bundle"]["cohort_selection"]["source_task_audit"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        _validator(
            by_id["https://agentsentinel.local/schemas/r2_5/artifact_bundle.v1.schema.json"]
        ).validate(tampered)


def test_written_source_resolves_to_exact_task_reset_inputs(tmp_path: Path) -> None:
    inputs, _ = _inputs(tmp_path)
    records = current_registry_metadata()
    source = tmp_path / "current-registry-tasks.jsonl"
    source.write_text(
        "".join(
            json.dumps({"task_name": record.task_id, "trial": 1}, sort_keys=True) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    selected = select_gui_only_cohort(source, records)
    inputs = replace(
        inputs,
        source_task_jsonl=source,
        qwen_smoke_task_id=selected.members[0].task_id,
        mai_smoke_task_id=selected.members[1].task_id,
    )
    bundle = build_authority_artifact_bundle(inputs, records)

    written = write_artifact_bundle(bundle, repository_root=inputs.repository_root)

    assert {path.name for path in written} >= {
        PILOT_TASK_SOURCE_FILENAME,
        RUN_AUTHORITY_MANIFEST_FILENAME,
        ARTIFACT_BUNDLE_FILENAME,
        TOPOLOGY_COMPARISON_FILENAME,
        COHORT_SELECTION_FILENAME,
        GUI_ONLY_TASK_SOURCE_FILENAME,
    }
    resolved = resolve_pilot_task_inputs_v1(
        bundle.pilot_manifest,
        authorized_input_root=inputs.bundle_directory,
        repository_root=inputs.repository_root,
    )
    assert len(resolved.tasks) == 20
    assert tuple(task.task_id for task in resolved.tasks) == tuple(
        member.task_id for member in bundle.selection.members
    )
    assert all(task.trial == 1 for task in resolved.tasks)
    assert (
        canonical_json_bytes(artifact_bundle_output(bundle))
        == (inputs.bundle_directory / ARTIFACT_BUNDLE_FILENAME).read_bytes()
    )
    topology_bytes = (inputs.bundle_directory / TOPOLOGY_COMPARISON_FILENAME).read_bytes()
    assert hashlib.sha256(topology_bytes).hexdigest() == (
        bundle.pilot_manifest.topology_comparison_artifact_sha256
    )
    selection_bytes = (inputs.bundle_directory / COHORT_SELECTION_FILENAME).read_bytes()
    assert hashlib.sha256(selection_bytes).hexdigest() == (
        bundle.pilot_manifest.cohort_selection_artifact_sha256
    )
    assert (inputs.bundle_directory / GUI_ONLY_TASK_SOURCE_FILENAME).read_bytes() == (
        source.read_bytes()
    )


def test_owner_promotion_changes_only_status_and_writes_fresh_external_0600(
    tmp_path: Path,
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(bundle, repository_root=inputs.repository_root)
    draft_path = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    draft = load_canonical_draft_authority_v1(
        draft_path,
        repository_root=inputs.repository_root,
    )
    draft_sha256 = authority_manifest_sha256(draft)
    promoted = promote_draft_authority_v1(
        draft,
        confirmed_draft_sha256=draft_sha256,
    )

    before = json.loads(canonical_json_bytes(authority_manifest_projection(draft)))
    after = json.loads(canonical_json_bytes(authority_manifest_projection(promoted)))
    assert before["authorization"]["status"] == "DRAFT_NOT_AUTHORIZED"
    before["authorization"]["status"] = "OWNER_AUTHORIZED"
    assert after == before
    assert promoted.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED

    output = tmp_path / "owner-authorized-manifest.json"
    digest = write_fresh_owner_authority_v1(
        promoted,
        output,
        repository_root=inputs.repository_root,
    )
    assert digest == authority_manifest_sha256(promoted)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert output.read_bytes() == canonical_json_bytes(authority_manifest_projection(promoted))
    with pytest.raises(AuthorityPromotionError, match="OUTPUT_NOT_FRESH"):
        write_fresh_owner_authority_v1(
            promoted,
            output,
            repository_root=inputs.repository_root,
        )
    with pytest.raises(AuthorityPromotionError, match="DRAFT_CONFIRMATION_MISMATCH"):
        promote_draft_authority_v1(draft, confirmed_draft_sha256="0" * 64)


def _load_promotion_cli() -> ModuleType:
    script = REPOSITORY_ROOT / "MobileWorld/scripts/promote_r2_4_r2_5_authority.py"
    spec = importlib.util.spec_from_file_location("r24_r25_authority_promotion_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owner_promotion_cli_requires_explicit_assertion_and_exact_draft_hash(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(bundle, repository_root=inputs.repository_root)
    draft = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    output = tmp_path / "promoted.json"
    cli = _load_promotion_cli()
    base = [
        "--draft-manifest",
        str(draft),
        "--confirm-draft-sha256",
        authority_manifest_sha256(bundle.authority_manifest),
        "--output",
        str(output),
        "--repository-root",
        str(inputs.repository_root),
    ]

    assert cli.main(base) == 2
    capsys.readouterr()
    assert not output.exists()
    wrong_hash = list(base)
    wrong_hash[wrong_hash.index("--confirm-draft-sha256") + 1] = "0" * 64
    assert cli.main([*wrong_hash, "--owner-approved"]) == 2
    capsys.readouterr()
    assert not output.exists()
    assert cli.main([*base, "--owner-approved"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["projection_change"] == "AUTHORIZATION_STATUS_ONLY"
    assert emitted["draft_manifest_sha256"] == authority_manifest_sha256(bundle.authority_manifest)
    assert emitted["authorized_manifest_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    parsed = parse_authority_manifest(json.loads(output.read_bytes()))
    assert parsed.authorization.status is RunAuthorizationStatusV1.OWNER_AUTHORIZED


def test_owner_promotion_rejects_noncanonical_or_already_promoted_input(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    write_artifact_bundle(bundle, repository_root=inputs.repository_root)
    draft_path = inputs.bundle_directory / RUN_AUTHORITY_MANIFEST_FILENAME
    draft = load_canonical_draft_authority_v1(
        draft_path,
        repository_root=inputs.repository_root,
    )
    promoted = promote_draft_authority_v1(
        draft,
        confirmed_draft_sha256=authority_manifest_sha256(draft),
    )
    with pytest.raises(AuthorityPromotionError, match="DRAFT_REQUIRED"):
        promote_draft_authority_v1(
            promoted,
            confirmed_draft_sha256=authority_manifest_sha256(promoted),
        )

    symlink = tmp_path / "draft-link.json"
    symlink.symlink_to(draft_path)
    with pytest.raises(AuthorityPromotionError, match="INVALID_DRAFT_FILE"):
        load_canonical_draft_authority_v1(
            symlink,
            repository_root=inputs.repository_root,
        )

    draft_path.write_text(
        json.dumps(authority_manifest_projection(draft), indent=2),
        encoding="utf-8",
    )
    draft_path.chmod(0o600)
    with pytest.raises(AuthorityPromotionError, match="NONCANONICAL_DRAFT"):
        load_canonical_draft_authority_v1(
            draft_path,
            repository_root=inputs.repository_root,
        )


def test_selection_parser_rejects_forged_static_member_derivations(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    selection = select_gui_only_cohort(source, records)
    projection = cohort_selection_projection(selection)

    forged_seed = json.loads(canonical_json_bytes(projection))
    forged_seed["members"][0]["reset_seed"] += 1
    with pytest.raises(R25ArtifactBuildError, match="INVALID_SELECTION"):
        parse_cohort_selection(forged_seed)

    forged_static = json.loads(canonical_json_bytes(projection))
    excluded = next(
        record
        for record in forged_static["source_task_audit"]
        if record["disposition"] != "ELIGIBLE"
    )
    excluded["disposition"] = "ELIGIBLE"
    excluded["selection_sha256"] = "0" * 64
    with pytest.raises(R25ArtifactBuildError):
        parse_cohort_selection(forged_static)


def test_builder_rejects_noncanonical_or_forged_topology_preimage(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)
    parsed = json.loads(inputs.topology_comparison_artifact.read_bytes())
    inputs.topology_comparison_artifact.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    with pytest.raises(R25ArtifactBuildError, match="NONCANONICAL_TOPOLOGY_COMPARISON"):
        build_authority_artifact_bundle(inputs, records)

    parsed["comparison"]["total_call_count"] += 1
    inputs.topology_comparison_artifact.write_bytes(canonical_json_bytes(parsed))
    with pytest.raises(R25ArtifactBuildError, match="INVALID_TOPOLOGY_COMPARISON"):
        build_authority_artifact_bundle(inputs, records)


def test_writer_rejects_existing_or_repo_internal_directory(tmp_path: Path) -> None:
    inputs, records = _inputs(tmp_path)
    bundle = build_authority_artifact_bundle(inputs, records)
    inputs.bundle_directory.mkdir()

    with pytest.raises(R25ArtifactBuildError, match="OUTPUT_DIRECTORY_NOT_FRESH"):
        write_artifact_bundle(bundle, repository_root=inputs.repository_root)

    internal_inputs = replace(
        inputs,
        bundle_directory=inputs.repository_root / "authority-bundle",
    )
    with pytest.raises(R25ArtifactBuildError, match="REPOSITORY_PATH_FORBIDDEN"):
        build_authority_artifact_bundle(internal_inputs, records)


def test_malformed_or_duplicate_source_rows_fail_closed(tmp_path: Path) -> None:
    source, records = _source_and_registry(tmp_path)
    source.write_text('{"task_name":"GuiTask00","task_name":"GuiTask01","trial":1}\n')
    with pytest.raises(R25ArtifactBuildError, match="DUPLICATE_JSON_KEY"):
        select_gui_only_cohort(source, records)

    source.write_text(
        '{"task_name":"GuiTask00","trial":1}\n{"task_name":"GuiTask00","trial":2}\n',
        encoding="utf-8",
    )
    with pytest.raises(R25ArtifactBuildError, match="DUPLICATE_SOURCE_TASK"):
        select_gui_only_cohort(source, records)


def test_selection_algorithm_identifier_is_frozen() -> None:
    assert COHORT_SELECTION_ALGORITHM == "SHA256_R25_PILOT_V1"
