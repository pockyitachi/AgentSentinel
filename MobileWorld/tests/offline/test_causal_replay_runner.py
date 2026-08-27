from __future__ import annotations

import hashlib
import json
import socket
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import FrozenInstanceError, dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, RefResolver

from mobile_world.offline.causal_replay import (
    ArmKind,
    DeclarativeFixtureHistoryCodec,
    ExecutionMode,
    FailurePolicy,
    HistoryCodecRegistry,
    HistoryFamily,
    HistoryIR,
    PortableContractError,
    ProviderCodecRegistry,
)
from mobile_world.offline.causal_replay.conformance import (
    build_fixture_plan,
    materialize_fixture_mapping,
)
from mobile_world.offline.causal_replay.contracts import (
    CodecCapabilities,
    CodecScope,
    JsonValue,
    OperationKind,
    RawProviderResponse,
    SpanRole,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
    get_at_path,
    set_at_path,
)
from mobile_world.offline.causal_replay.core import render_request
from mobile_world.offline.causal_replay_runner import (
    DeterministicFakeProviderCodec,
    ExecutionDomain,
    FakeScenario,
    JsonActionParser,
    LoadedReplayCapsule,
    OpenAICompatibleProviderCodec,
    ProviderTransportFailure,
    ReplayArtifactStore,
    ReplayRunnerError,
    UnitKind,
    arm_order_for_block,
    build_blinded_packet,
    execute_fake_arm,
    execute_live_arm,
    load_replay_capsule,
    order_blinded_packets,
    parser_adapter,
    preflight_block,
    prepare_blinding,
    record_preflight_blocked,
    schedule_for_unit,
    validate_blinded_packet,
    validate_schedule,
)
from mobile_world.offline.causal_replay_runner import capsule_loader as capsule_loader_module
from mobile_world.offline.causal_replay_runner import runner as runner_module
from mobile_world.offline.causal_replay_runner.blinding import _make_blinded_packet
from mobile_world.offline.causal_replay_runner.cli import main as cli_main
from mobile_world.offline.causal_replay_runner.contracts import (
    ATTEMPT_EVENT_SCHEMA_VERSION,
    CPU_REQUIRED_CHECKS,
    MAXIMUM_PROVIDER_ATTEMPTS,
    PROTOCOL_VERSION,
    REPLAY_SEEDS,
    RETRYABLE_FAILURES,
    AttemptEventKind,
    BlindedActionPacket,
    CpuReadinessManifest,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MOBILEWORLD_ROOT = REPO_ROOT / "MobileWorld"
VECTOR_PATH = Path(__file__).parent / "fixtures/causal_replay/six_family_vectors.v1.json"
SCENARIO_PATH = (
    Path(__file__).parent / "fixtures/causal_replay_runner/fake_provider_scenarios.v1.json"
)
G14_SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/g1_4"
G12_SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/g1_2"
ACTIVE_PUBLICATION_ROOT = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_3/capsules/sha256/"
    "8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402"
)

STRICT_UNIT_ID = "g1case-000000000000000000000001"
CLEAN_UNIT_ID = "g1control-000000000000000000000001"
MODEL_ID = "qwen3vl_8b"
ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64
BLINDING_CONFIDENTIAL_VALUES = (
    MODEL_ID,
    "mobileworld.g1.provider.fake-conformance/v1",
    "https://provider.example/v1/chat/completions",
)


def _vectors() -> list[dict[str, Any]]:
    payload = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == ("mobileworld.g1.portable-sentinel.conformance-vectors/v1")
    return [
        cast(dict[str, Any], {**deepcopy(item), "capabilities": deepcopy(payload["capabilities"])})
        for item in payload["vectors"]
    ]


@dataclass(frozen=True)
class _LiveCodecDeclaration:
    codec_id: str
    contract_version: str
    history_family: HistoryFamily
    capabilities: CodecCapabilities
    frozen_ir: HistoryIR

    def extract(self, application_request: JsonValue) -> HistoryIR:
        assert canonical_sha256(application_request) == self.frozen_ir.raw_request_sha256
        return self.frozen_ir


@dataclass(frozen=True)
class _ReplayFixture:
    request: dict[str, JsonValue]
    history_ir: HistoryIR
    plans: tuple[Any, ...]
    history_registry: HistoryCodecRegistry
    capsule: LoadedReplayCapsule


def _history_registry(declaration: _LiveCodecDeclaration) -> HistoryCodecRegistry:
    registry = HistoryCodecRegistry()
    registry.register_factory(
        history_family=declaration.history_family,
        contract_version=declaration.contract_version,
        codec_id=declaration.codec_id,
        factory=lambda: declaration,
    )
    return registry


def _provider_registry(
    scenarios: tuple[FakeScenario, ...],
) -> tuple[ProviderCodecRegistry, DeterministicFakeProviderCodec]:
    provider = DeterministicFakeProviderCodec(scenarios)
    registry = ProviderCodecRegistry()
    registry.register(provider)
    return registry, provider


def _replace_projection_spans(
    request: JsonValue, spans: tuple[Any, ...]
) -> tuple[JsonValue, tuple[dict[str, JsonValue], ...]]:
    projection = copy_json(request)
    regions: list[dict[str, JsonValue]] = []
    grouped: defaultdict[tuple[str | int, ...], list[tuple[int, int]]] = defaultdict(list)
    for span in spans:
        source_value = get_at_path(request, span.container_path)
        assert isinstance(source_value, str)
        grouped[span.container_path].append((span.char_start, span.char_end))
        regions.append(
            {
                "region_kind": "HISTORY",
                "ownership_role": "OWNER",
                "bindings": [
                    {
                        "binding_kind": "TEXT_SLICE",
                        "path": list(span.container_path),
                        "value_sha256": canonical_sha256(source_value),
                        "text_slice": {
                            "container_path": list(span.container_path),
                            "char_start": span.char_start,
                            "char_end": span.char_end,
                            "utf8_byte_start": span.utf8_byte_start,
                            "utf8_byte_end": span.utf8_byte_end,
                            "exact_text": span.exact_text,
                            "span_sha256": span.span_sha256,
                        },
                        "artifact_ref": None,
                        "semantic_role": "HISTORY_EDITABLE_SPAN",
                        "visibility_class": "MUTABLE_HISTORY_TREATMENT",
                    }
                ],
            }
        )
    for path, path_spans in grouped.items():
        current = get_at_path(projection, path)
        assert isinstance(current, str)
        for start, end in sorted(path_spans, reverse=True):
            current = current[:start] + "<MUTABLE_HISTORY_TREATMENT>" + current[end:]
        set_at_path(projection, path, current)
    return projection, tuple(regions)


def _capsule(
    *,
    unit_kind: UnitKind,
    request: dict[str, JsonValue],
    mutable_spans: tuple[Any, ...],
) -> LoadedReplayCapsule:
    projection, regions = _replace_projection_spans(request, mutable_spans)
    unit_id = STRICT_UNIT_ID if unit_kind is UnitKind.STRICT_MHR else CLEAN_UNIT_ID
    semantic_sha = canonical_sha256(request)
    capsule_body_sha = canonical_sha256(
        {
            "unit_id": unit_id,
            "semantic_request_sha256": semantic_sha,
            "fixture_only": True,
        }
    )
    return LoadedReplayCapsule(
        publication_manifest_sha256=ZERO_SHA,
        capsule_file_sha256=ONE_SHA,
        capsule_body_sha256=capsule_body_sha,
        capsule_id=f"g1capsule-{capsule_body_sha[:24]}",
        unit_kind=unit_kind,
        unit_id=unit_id,
        model_id=MODEL_ID,
        history_family="flat_progress",
        semantic_request=copy_json(request),
        semantic_request_sha256=semantic_sha,
        region_partition=regions,
        non_history_projection_sha256=canonical_sha256(projection),
        treatment_surface={"fixture_only": True},
        replay_binding={
            "binding_version": "mobileworld.g1.replay-binding/v1",
            "host": {"adapter_id": "qwen3vl", "component": "fixture", "call_role": "actor"},
            "model": {
                "model_id": MODEL_ID,
                "served_model_name": "fixture-flat-progress",
                "repository": "fixture/network-forbidden",
                "revision": "fixture-cpu-v1",
                "model_config_manifest_sha256": ZERO_SHA,
                "model_config_record_sha256": ONE_SHA,
            },
            "provider": {
                "sdk_package": "fake",
                "sdk_version": "0",
                "sdk_method": "chat.completions.create",
                "endpoint_origin": "fake://network-forbidden",
                "endpoint_path": "/v1",
                "query_removed": True,
                "stream": False,
                "excluded_transport_fields": [],
                "excluded_transport_fields_send_eligible": False,
            },
            "parser": {
                "binding_id": JsonActionParser.binding_id,
                "implementation_sha256": ZERO_SHA,
            },
        },
        restore_descriptor={
            "mode": "SERIALIZED_REQUEST_ONLY",
            "external_state_consulted": False,
            "checkpoint_required": False,
        },
        parser_descriptor={"binding_id": JsonActionParser.binding_id, "fixture_only": True},
        decoding_configuration=cast(
            dict[str, JsonValue],
            copy_json({key: value for key, value in request.items() if key != "messages"}),
        ),
        source_safety={
            "execution_ready": False,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
            "provider_invoked": False,
        },
    )


def _replay_fixture(unit_kind: UnitKind) -> _ReplayFixture:
    vector = _vectors()[1]
    vector["capabilities"]["supported_arms"] = [arm.value for arm in ArmKind]
    records = {item["record_key"]: item for item in vector["mapping"]["records"]}
    if unit_kind is UnitKind.STRICT_MHR:
        records["progress_step_2"]["editable_spans"][0]["span_role"] = "BENIGN_SHAM"
    else:
        records["progress_step_1"]["editable_spans"][0]["span_role"] = "BENIGN_SHAM"
    fixture_codec = DeclarativeFixtureHistoryCodec(materialize_fixture_mapping(vector))
    request = cast(dict[str, JsonValue], copy_json(vector["application_request"]))
    fixture_ir = fixture_codec.extract(request)
    live_capabilities = replace(
        fixture_codec.capabilities,
        scope=CodecScope.LIVE,
        live_ready=True,
    )
    history_ir = replace(fixture_ir, capabilities=live_capabilities)
    declaration = _LiveCodecDeclaration(
        codec_id=fixture_codec.codec_id,
        contract_version=fixture_codec.contract_version,
        history_family=fixture_codec.history_family,
        capabilities=live_capabilities,
        frozen_ir=history_ir,
    )
    if unit_kind is UnitKind.STRICT_MHR:
        focal_key = "progress_step_1"
        arms_and_keys = (
            (ArmKind.ORIGINAL, focal_key),
            (ArmKind.MASK, focal_key),
            (ArmKind.MASK_CORRECTION, focal_key),
            (ArmKind.ORACLE_CLEAN, focal_key),
            (ArmKind.SHAM_BENIGN_EDIT, "progress_step_2"),
        )
    else:
        focal_key = "progress_step_1"
        arms_and_keys = (
            (ArmKind.ORIGINAL, focal_key),
            (ArmKind.SHAM_BENIGN_EDIT, focal_key),
        )
    plans = tuple(
        build_fixture_plan(
            ir=history_ir,
            record_key=record_key,
            arm=arm,
            correction_text=(vector["correction_text"] if arm is ArmKind.MASK_CORRECTION else None),
        )
        for arm, record_key in arms_and_keys
    )
    span_by_key = {
        record.record_key: next(
            span
            for span in record.editable_spans
            if span.span_role in {SpanRole.EDITABLE_CLAIM, SpanRole.BENIGN_SHAM}
        )
        for record in history_ir.records
        if record.record_key in {record_key for _, record_key in arms_and_keys}
    }
    mutable_spans = tuple(
        span_by_key[key] for key in dict.fromkeys(key for _, key in arms_and_keys)
    )
    return _ReplayFixture(
        request=request,
        history_ir=history_ir,
        plans=plans,
        history_registry=_history_registry(declaration),
        capsule=_capsule(unit_kind=unit_kind, request=request, mutable_spans=mutable_spans),
    )


def _block(unit_kind: UnitKind, block_index: int = 1) -> tuple[Any, ...]:
    entries = schedule_for_unit(
        unit_kind=unit_kind,
        unit_id=STRICT_UNIT_ID if unit_kind is UnitKind.STRICT_MHR else CLEAN_UNIT_ID,
        model_id=MODEL_ID,
    )
    return tuple(item for item in entries if item.block_index == block_index)


def _preflight(
    fixture: _ReplayFixture,
    provider_registry: ProviderCodecRegistry,
    provider: DeterministicFakeProviderCodec,
    *,
    code_sha256: str = "2" * 64,
    config_sha256: str = "3" * 64,
    preflight_store: ReplayArtifactStore | None = None,
) -> tuple[Any, ...]:
    return preflight_block(
        capsule=fixture.capsule,
        history_ir=fixture.history_ir,
        paired_plans=fixture.plans,
        schedule_block=_block(fixture.capsule.unit_kind),
        history_registry=fixture.history_registry,
        provider_registry=provider_registry,
        provider_codec_id=provider.codec_id,
        provider_contract_version=provider.contract_version,
        execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
        code_sha256=code_sha256,
        config_sha256=config_sha256,
        timeout_seconds=17,
        preflight_store=preflight_store,
    )


def _completed_original_run(
    output_root: Path,
) -> tuple[Any, ReplayArtifactStore, DeterministicFakeProviderCodec]:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        output_root,
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    return original, store, provider


def _invoke_formal_completed_entrypoint(
    entrypoint: str,
    prepared: Any,
    *,
    provider_registry: ProviderCodecRegistry,
    store: ReplayArtifactStore,
) -> Any:
    if entrypoint == "reuse":
        return execute_fake_arm(
            prepared,
            provider_registry=provider_registry,
            store=store,
        )
    assert entrypoint == "blinded-export"
    return build_blinded_packet(prepared, store=store)


def _append_allowed_preflight_prefix(
    store: ReplayArtifactStore,
    *,
    run_id: str,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    artifact_ref = store.put_json({"fixture": "closed-preflight-payload"})
    planned = store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.PLANNED,
        provider_attempt_index=None,
        payload={
            "invocation_plan_sha256": "a" * 64,
            "selected_plan_ref": artifact_ref,
            "paired_plan_set_ref": artifact_ref,
            "invariance_report_ref": artifact_ref,
            "render_result_ref": artifact_ref,
            "validation_receipt_ref": artifact_ref,
            "final_application_request_ref": artifact_ref,
            "target_diff_ref": artifact_ref,
            "blinding_commitment": {
                "blinding_mapping_sha256": "b" * 64,
                "key_commitment_sha256": "c" * 64,
                "mapping_persisted_before_response": True,
            },
        },
    )
    allowed = store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.PREFLIGHT_ALLOWED,
        provider_attempt_index=None,
        payload={
            "fake_conformance": True,
            "external_provider_invocation_allowed": False,
            "encoded_request_ref": artifact_ref,
        },
    )
    return planned.to_dict(), allowed.to_dict()


def _output_tree_snapshot(root: Path) -> tuple[tuple[str, str, str | bytes | None], ...]:
    snapshot: list[tuple[str, str, str | bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot.append((relative, "symlink", path.readlink().as_posix()))
        elif path.is_dir():
            snapshot.append((relative, "directory", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return tuple(snapshot)


def _canonical_attempt_event_record(
    *,
    run_id: str,
    seq: int,
    previous_event_sha256: str | None,
    event_kind: AttemptEventKind,
    payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    subject: dict[str, JsonValue] = {
        "run_id": run_id,
        "seq": seq,
        "previous_event_sha256": previous_event_sha256,
        "event_kind": event_kind.value,
        "provider_attempt_index": None,
        "payload": copy_json(payload),
    }
    return {
        "schema_version": ATTEMPT_EVENT_SCHEMA_VERSION,
        "record_type": "g1_replay_attempt_event",
        "protocol_version": PROTOCOL_VERSION,
        "event_id": f"g1attempt-event-{canonical_sha256(subject)[:24]}",
        **subject,
        "raw_collector_event": False,
        "generated_action_executed": False,
    }


def _schema_store() -> dict[str, dict[str, Any]]:
    schemas = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for root in (G12_SCHEMA_ROOT, G14_SCHEMA_ROOT)
        for path in sorted(root.glob("*.json"))
    }
    return {schema["$id"]: schema for schema in schemas.values()}


def _validator(name: str) -> Draft202012Validator:
    selected = json.loads((G14_SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    return Draft202012Validator(
        selected,
        resolver=RefResolver.from_schema(selected, store=_schema_store()),
    )


def _content_ref(name: str, data: bytes) -> dict[str, JsonValue]:
    return {
        "store_id": "G1_3_PUBLICATION",
        "relative_path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "media_type": "application/json",
    }


def _synthetic_publication(
    parent: Path,
    *,
    safety_field: str | None = None,
    safety_value: object = False,
    remove_safety_field: bool = False,
) -> tuple[Path, str, dict[str, Any]]:
    unit_id = CLEAN_UNIT_ID
    semantic_request: dict[str, JsonValue] = {
        "model": "fixture-flat-progress",
        "messages": [{"role": "user", "content": "history then current state"}],
        "temperature": 0.0,
    }
    decoding = {"model": "fixture-flat-progress", "temperature": 0.0}
    parser = {"binding_id": "qwen3vl_8b:production-next-action-parser/v1"}
    semantic_bytes = canonical_json_bytes(semantic_request)
    decoding_bytes = canonical_json_bytes(decoding)
    parser_bytes = canonical_json_bytes(parser)
    safety: dict[str, object] = {
        "execution_ready": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "provider_invoked": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "generated_action_executed": False,
        "raw_collector_mutated": False,
        "automatic_semantic_inference_performed": False,
        "runtime_sentinel_enabled": False,
        "treatment_response_count": 0,
    }
    if safety_field is not None:
        if remove_safety_field:
            safety.pop(safety_field)
        else:
            safety[safety_field] = safety_value
    body: dict[str, JsonValue] = {
        "capsule_id": "g1capsule-0123456789abcdef01234567",
        "unit": {
            "unit_id": unit_id,
            "unit_kind": "CLEAN_CONTROL",
            "model_id": MODEL_ID,
            "history_family": "flat_progress",
        },
        "runtime": {
            "model_visible": {
                "semantic_request": {
                    "canonical_semantic_request_ref": _content_ref(
                        "semantic-request.json", semantic_bytes
                    ),
                    "canonical_semantic_request_sha256": hashlib.sha256(semantic_bytes).hexdigest(),
                },
                "region_partition": [],
                "non_history_projection_sha256": canonical_sha256(semantic_request),
            },
            "non_history_envelope": {
                "replay_binding": {
                    "parser": {
                        "implementation_ref": _content_ref("parser.json", parser_bytes),
                        "implementation_sha256": hashlib.sha256(parser_bytes).hexdigest(),
                    },
                    "provider": {
                        "decoding_configuration_ref": _content_ref("decoding.json", decoding_bytes),
                        "decoding_configuration_sha256": hashlib.sha256(decoding_bytes).hexdigest(),
                    },
                },
                "provider_envelope_sha256": hashlib.sha256(decoding_bytes).hexdigest(),
                "restore_descriptor": {
                    "mode": "SERIALIZED_REQUEST_ONLY",
                    "external_state_consulted": False,
                    "checkpoint_required": False,
                },
            },
            "treatment_surface": {},
        },
        "curator_only": {"must_not_be_exposed": "sealed"},
        "post_action_audit": {"natural_action": "must_not_be_exposed"},
        "safety": cast(JsonValue, safety),
    }
    body_sha = canonical_sha256(body)
    capsule = {
        "schema_version": "mobileworld.g1.replay-capsule/v1.1",
        "record_type": "g1_replay_capsule_envelope",
        "capsule_body_sha256": body_sha,
        "capsule": body,
    }
    capsule_bytes = canonical_json_bytes(capsule)
    capsule_name = "capsule.json"
    manifest: dict[str, JsonValue] = {
        "schema_version": "mobileworld.g1.replay-capsule-manifest/v1.1",
        "publication_phase": "FORMAL_PUBLICATION_READY",
        "capsule_set_sha256": ONE_SHA,
        "counts": {"capsuled_count": 190, "excluded_count": 0},
        "units": [
            {
                "unit_id": unit_id,
                "unit_kind": "CLEAN_CONTROL",
                "model_id": MODEL_ID,
                "history_family": "flat_progress",
                "disposition": "CAPSULED",
                "capsule_body_sha256": body_sha,
                "capsule_ref": {
                    "relative_path": capsule_name,
                    "sha256": hashlib.sha256(capsule_bytes).hexdigest(),
                    "byte_count": len(capsule_bytes),
                },
            }
        ],
    }
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    root = parent / manifest_sha
    root.mkdir()
    for name, data in (
        ("capsule_manifest.json", manifest_bytes),
        (capsule_name, capsule_bytes),
        ("semantic-request.json", semantic_bytes),
        ("decoding.json", decoding_bytes),
        ("parser.json", parser_bytes),
    ):
        (root / name).write_bytes(data)
    receipt: dict[str, Any] = {
        "valid": True,
        "structural_valid": True,
        "formal_publication_valid": True,
        "source_bound_valid": True,
        "source_rebuild_performed": True,
        "source_rebuild_byte_identical": True,
        "exact_file_set": True,
        "regular_files_only": True,
        "zero_symlinks": True,
        "read_only": True,
        "validation_scope": "SOURCE_BOUND",
        "artifact_schema_generation": "ACTIVE_V1_1",
        "capsule_schema_version": "mobileworld.g1.replay-capsule/v1.1",
        "superseded_for_formal_g1": False,
        "manifest_sha256": manifest_sha,
        "capsule_set_sha256": ONE_SHA,
        "file_count": 5,
        "total_byte_count": sum(
            len(data)
            for data in (
                manifest_bytes,
                capsule_bytes,
                semantic_bytes,
                decoding_bytes,
                parser_bytes,
            )
        ),
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "raw_collector_mutated": False,
    }
    return root, unit_id, receipt


def _pin_synthetic_loader(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    receipt: dict[str, Any],
) -> None:
    monkeypatch.setattr(
        capsule_loader_module,
        "ACTIVE_PUBLICATION_MANIFEST_SHA256",
        root.name,
    )
    monkeypatch.setattr(
        capsule_loader_module,
        "ACTIVE_CAPSULE_SET_SHA256",
        receipt["capsule_set_sha256"],
    )
    monkeypatch.setattr(capsule_loader_module, "ACTIVE_FILE_COUNT", receipt["file_count"])
    monkeypatch.setattr(
        capsule_loader_module,
        "ACTIVE_TOTAL_BYTE_COUNT",
        receipt["total_byte_count"],
    )


def test_locked_schedule_exact_vectors_balance_and_determinism() -> None:
    strict = schedule_for_unit(
        unit_kind=UnitKind.STRICT_MHR,
        unit_id=STRICT_UNIT_ID,
        model_id=MODEL_ID,
    )
    strict_orders = [
        [entry.arm.value for entry in strict if entry.block_index == block] for block in range(1, 7)
    ]
    assert strict_orders == [
        ["SHAM_BENIGN_EDIT", "ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN"],
        ["ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT"],
        ["MASK", "MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT", "ORIGINAL"],
        ["MASK_CORRECTION", "ORACLE_CLEAN", "SHAM_BENIGN_EDIT", "ORIGINAL", "MASK"],
        ["ORACLE_CLEAN", "SHAM_BENIGN_EDIT", "ORIGINAL", "MASK", "MASK_CORRECTION"],
        ["SHAM_BENIGN_EDIT", "ORIGINAL", "MASK", "MASK_CORRECTION", "ORACLE_CLEAN"],
    ]
    assert strict == schedule_for_unit(
        unit_kind=UnitKind.STRICT_MHR,
        unit_id=STRICT_UNIT_ID,
        model_id=MODEL_ID,
    )
    assert len(strict) == 30
    positions = {arm: Counter() for arm in ArmKind}
    for entry in strict:
        positions[entry.arm][entry.arm_order_index] += 1
    assert all(max(counts.values()) - min(counts.values()) == 1 for counts in positions.values())

    clean = schedule_for_unit(
        unit_kind=UnitKind.CLEAN_CONTROL,
        unit_id=CLEAN_UNIT_ID,
        model_id=MODEL_ID,
    )
    assert len(clean) == 12
    assert [
        [entry.arm.value for entry in clean if entry.block_index == block] for block in range(1, 7)
    ] == [
        ["ORIGINAL", "SHAM_BENIGN_EDIT"],
        ["SHAM_BENIGN_EDIT", "ORIGINAL"],
        ["ORIGINAL", "SHAM_BENIGN_EDIT"],
        ["SHAM_BENIGN_EDIT", "ORIGINAL"],
        ["ORIGINAL", "SHAM_BENIGN_EDIT"],
        ["SHAM_BENIGN_EDIT", "ORIGINAL"],
    ]
    assert Counter(entry.arm for entry in clean if entry.arm_order_index == 0) == {
        ArmKind.ORIGINAL: 3,
        ArmKind.SHAM_BENIGN_EDIT: 3,
    }
    validate_schedule(strict)
    validate_schedule(clean)
    for block in range(6):
        assert arm_order_for_block(
            unit_kind=UnitKind.STRICT_MHR,
            unit_id=STRICT_UNIT_ID,
            model_id=MODEL_ID,
            block_zero_index=block,
        ) == tuple(entry.arm for entry in strict if entry.block_index == block + 1)


_INVALID_SCHEDULE_IDENTITIES = (
    pytest.param(UnitKind.STRICT_MHR, CLEAN_UNIT_ID, MODEL_ID, id="strict-with-control-id"),
    pytest.param(UnitKind.CLEAN_CONTROL, STRICT_UNIT_ID, MODEL_ID, id="clean-with-case-id"),
    pytest.param(
        UnitKind.STRICT_MHR,
        f"g1case-{'g' * 24}",
        MODEL_ID,
        id="non-hex-case-id",
    ),
    pytest.param(
        UnitKind.CLEAN_CONTROL,
        CLEAN_UNIT_ID,
        "unregistered_model",
        id="model-outside-catalog",
    ),
)


@pytest.mark.parametrize(
    ("unit_kind", "unit_id", "model_id"),
    _INVALID_SCHEDULE_IDENTITIES,
)
def test_schedule_api_rejects_invalid_unit_patterns_and_model_catalog(
    unit_kind: UnitKind,
    unit_id: str,
    model_id: str,
) -> None:
    with pytest.raises(ReplayRunnerError, match="INVALID_SCHEDULE_IDENTITY"):
        schedule_for_unit(
            unit_kind=unit_kind,
            unit_id=unit_id,
            model_id=model_id,
        )


@pytest.mark.parametrize(
    ("unit_kind", "unit_id", "model_id"),
    _INVALID_SCHEDULE_IDENTITIES,
)
def test_schedule_cli_returns_two_for_invalid_unit_patterns_and_model_catalog(
    capsys: pytest.CaptureFixture[str],
    unit_kind: UnitKind,
    unit_id: str,
    model_id: str,
) -> None:
    assert (
        cli_main(
            [
                "schedule",
                "--unit-kind",
                unit_kind.value,
                "--unit-id",
                unit_id,
                "--model-id",
                model_id,
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["valid"] is False
    assert output["error_code"] == "INVALID_SCHEDULE_IDENTITY"
    assert output["provider_invocation_allowed"] is False


@pytest.mark.parametrize(
    "field",
    (
        "schedule_id",
        "arm_order_input_sha256",
        "block_arm_order_sha256",
        "arm_order_index",
    ),
)
def test_schedule_binding_tamper_fails_validation_and_preflight_before_encode(
    field: str,
) -> None:
    entries = list(
        schedule_for_unit(
            unit_kind=UnitKind.CLEAN_CONTROL,
            unit_id=CLEAN_UNIT_ID,
            model_id=MODEL_ID,
        )
    )
    original = getattr(entries[0], field)
    if isinstance(original, int):
        tampered: object = original + 10
    else:
        tampered = original[:-1] + ("0" if original[-1] != "0" else "1")
    entries[0] = replace(entries[0], **{field: tampered})

    with pytest.raises(ReplayRunnerError, match="SCHEDULE_"):
        validate_schedule(entries)

    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    tampered_block = tuple(item for item in entries if item.block_index == 1)
    with pytest.raises(ReplayRunnerError, match="SCHEDULE_"):
        preflight_block(
            capsule=fixture.capsule,
            history_ir=fixture.history_ir,
            paired_plans=fixture.plans,
            schedule_block=tampered_block,
            history_registry=fixture.history_registry,
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
        )
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0


def test_capsule_loader_requires_source_bound_receipt_and_returns_runtime_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, unit_id, receipt = _synthetic_publication(tmp_path)
    _pin_synthetic_loader(monkeypatch, root, receipt)
    loaded = load_replay_capsule(root, unit_id=unit_id, directory_receipt=receipt)
    assert loaded.unit_id == unit_id
    assert loaded.semantic_request == {
        "model": "fixture-flat-progress",
        "messages": [{"role": "user", "content": "history then current state"}],
        "temperature": 0.0,
    }
    assert loaded.source_safety["execution_ready"] is False
    assert loaded.source_safety["provider_invocation_allowed"] is False
    assert loaded.source_safety["treatment_response_generation_allowed"] is False
    assert not hasattr(loaded, "curator_only")
    assert not hasattr(loaded, "post_action_audit")
    assert not hasattr(loaded, "publication_root")
    assert not hasattr(loaded, "capsule_relative_path")

    invalid_receipt = {**receipt, "source_rebuild_byte_identical": False}
    with pytest.raises(ReplayRunnerError, match="CAPSULE_SOURCE_BOUND_RECEIPT_REQUIRED"):
        load_replay_capsule(root, unit_id=unit_id, directory_receipt=invalid_receipt)


@pytest.mark.skipif(
    not ACTIVE_PUBLICATION_ROOT.is_dir(),
    reason="active repo-external G1.3 v1.1 publication is unavailable",
)
def test_capsule_loader_reads_the_pinned_formal_v11_publication() -> None:
    manifest = json.loads((ACTIVE_PUBLICATION_ROOT / "capsule_manifest.json").read_bytes())
    unit_id = cast(str, manifest["units"][0]["unit_id"])
    receipt = {
        "valid": True,
        "structural_valid": True,
        "formal_publication_valid": True,
        "source_bound_valid": True,
        "source_rebuild_performed": True,
        "source_rebuild_byte_identical": True,
        "exact_file_set": True,
        "regular_files_only": True,
        "zero_symlinks": True,
        "read_only": True,
        "validation_scope": "SOURCE_BOUND",
        "artifact_schema_generation": "ACTIVE_V1_1",
        "capsule_schema_version": "mobileworld.g1.replay-capsule/v1.1",
        "superseded_for_formal_g1": False,
        "manifest_sha256": ACTIVE_PUBLICATION_ROOT.name,
        "capsule_set_sha256": ("7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed"),
        "file_count": 1600,
        "total_byte_count": 116_169_862,
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "raw_collector_mutated": False,
    }
    loaded = load_replay_capsule(
        ACTIVE_PUBLICATION_ROOT,
        unit_id=unit_id,
        directory_receipt=receipt,
    )
    assert loaded.publication_manifest_sha256 == ACTIVE_PUBLICATION_ROOT.name
    assert loaded.unit_id == unit_id
    assert loaded.restore_descriptor["mode"] == "SERIALIZED_REQUEST_ONLY"
    assert all(
        loaded.source_safety[key] is False
        for key in (
            "execution_ready",
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
            "provider_invoked",
        )
    )


@pytest.mark.parametrize(
    ("field", "mode", "value"),
    [
        (field, mode, value)
        for field in (
            "execution_ready",
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
        )
        for mode, value in (("missing", None), ("true", True), ("non_boolean", "false"))
    ],
)
def test_capsule_loader_rejects_missing_true_or_non_boolean_authorization_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mode: str,
    value: object,
) -> None:
    root, unit_id, receipt = _synthetic_publication(
        tmp_path,
        safety_field=field,
        safety_value=value,
        remove_safety_field=mode == "missing",
    )
    _pin_synthetic_loader(monkeypatch, root, receipt)
    with pytest.raises(ReplayRunnerError, match="CAPSULE_AUTHORIZATION_GUARD_INVALID"):
        load_replay_capsule(root, unit_id=unit_id, directory_receipt=receipt)


@pytest.mark.parametrize(
    ("field", "mode", "value"),
    [
        (field, mode, value)
        for field in (
            "execution_ready",
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
            "provider_invoked",
        )
        for mode, value in (("missing", None), ("true", True), ("non_boolean", "false"))
    ],
)
def test_preflight_rechecks_all_capsule_source_safety_guards_before_encode(
    field: str,
    mode: str,
    value: object,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    source_safety = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, fixture.capsule.source_safety)),
    )
    if mode == "missing":
        del source_safety[field]
    else:
        source_safety[field] = cast(JsonValue, value)
    tampered_fixture = replace(
        fixture,
        capsule=replace(fixture.capsule, source_safety=source_safety),
    )
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))

    with pytest.raises(ReplayRunnerError, match="CAPSULE_AUTHORIZATION_GUARD_INVALID"):
        _preflight(tampered_fixture, provider_registry, provider)
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0


_RESTORE_DESCRIPTOR_MUTATIONS = (
    pytest.param("mode", "EXACT_CHECKPOINT", id="exact-checkpoint"),
    pytest.param("mode", "PREFIX_REPLAY", id="prefix-replay"),
    pytest.param("external_state_consulted", True, id="external-state-true"),
    pytest.param("external_state_consulted", "false", id="external-state-non-boolean"),
    pytest.param("checkpoint_required", True, id="checkpoint-true"),
    pytest.param("checkpoint_required", 0, id="checkpoint-non-boolean"),
)


@pytest.mark.parametrize(("field", "value"), _RESTORE_DESCRIPTOR_MUTATIONS)
def test_preflight_rejects_non_serialized_restore_descriptor_before_store_or_provider_calls(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    restore_descriptor = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, fixture.capsule.restore_descriptor)),
    )
    restore_descriptor[field] = value
    tampered_fixture = replace(
        fixture,
        capsule=replace(fixture.capsule, restore_descriptor=restore_descriptor),
    )
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )

    with pytest.raises(ReplayRunnerError, match="LIVE_EXTERNAL_STATE_NOT_RESTORED"):
        _preflight(
            tampered_fixture,
            provider_registry,
            provider,
            preflight_store=store,
        )
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0


@pytest.mark.parametrize(("field", "value"), _RESTORE_DESCRIPTOR_MUTATIONS)
def test_execute_rechecks_restore_descriptor_before_store_or_provider_calls(
    tmp_path: Path,
    field: str,
    value: JsonValue,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    restore_descriptor = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, original.capsule.restore_descriptor)),
    )
    restore_descriptor[field] = value
    tampered = replace(
        original,
        capsule=replace(original.capsule, restore_descriptor=restore_descriptor),
    )
    encode_count = provider.encode_calls
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )

    with pytest.raises(ReplayRunnerError, match="LIVE_EXTERNAL_STATE_NOT_RESTORED"):
        execute_fake_arm(tampered, provider_registry=provider_registry, store=store)
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == encode_count
    assert provider.send_calls == provider.normalize_calls == 0


@pytest.mark.parametrize("unit_kind", [UnitKind.CLEAN_CONTROL, UnitKind.STRICT_MHR])
def test_clean_and_strict_pair_blocks_preflight_before_any_send(
    unit_kind: UnitKind,
) -> None:
    fixture = _replay_fixture(unit_kind)
    request_before = canonical_json_bytes(fixture.request)
    ir_before = canonical_json_bytes(fixture.history_ir.to_dict())
    plans_before = canonical_json_bytes([plan.to_dict() for plan in fixture.plans])
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    prepared = _preflight(fixture, provider_registry, provider)
    assert len(prepared) == (2 if unit_kind is UnitKind.CLEAN_CONTROL else 5)
    assert provider.encode_calls == len(prepared)
    assert provider.send_calls == provider.normalize_calls == 0
    assert {item.schedule.arm for item in prepared} == {plan.arm for plan in fixture.plans}
    for item in prepared:
        assert item.invariance_report.valid is True
        assert item.invariance_report.target_only_diff is True
        assert item.invariance_report.encoded_request_sha256 is not None
        assert item.invocation_plan.to_dict()["provider_invocation_allowed"] is False
        assert item.invocation_plan.to_dict()["treatment_response_generation_allowed"] is False
        assert item.invocation_plan.live_run_ready_seal_sha256 is None
        assert item.authorized_request.prepared.model_parameters["sdk_arguments"]["seed"] == 1729
        encoded = json.loads(item.authorized_request.encoded_request)
        assert encoded == {
            **cast(dict[str, JsonValue], item.render_result.rendered_request),
            "seed": 1729,
        }
        plan_record = item.invocation_plan.to_dict()
        assert plan_record["model_binding_sha256"] == canonical_sha256(
            fixture.capsule.replay_binding["model"]
        )
        assert plan_record["provider_binding_sha256"] == canonical_sha256(
            fixture.capsule.replay_binding["provider"]
        )
        assert plan_record["history_codec_sha256"] == canonical_sha256(
            fixture.history_ir.capabilities.to_dict()
        )
        _validator("invocation_plan.schema.json").validate(item.invocation_plan.to_dict())
        _validator("invariance_report.schema.json").validate(item.invariance_report.to_dict())
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    assert original.render_result.rendered_request == fixture.request
    assert original.render_result.rendered_request_sha256 == fixture.capsule.semantic_request_sha256
    assert canonical_json_bytes(fixture.request) == request_before
    assert canonical_json_bytes(fixture.history_ir.to_dict()) == ir_before
    assert canonical_json_bytes([plan.to_dict() for plan in fixture.plans]) == plans_before


def test_invariance_failure_blocks_encoder_sender_and_normalizer_for_whole_block(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    tampered = replace(fixture.capsule, non_history_projection_sha256="f" * 64)
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    with pytest.raises(ReplayRunnerError, match="NON_HISTORY_PROJECTION_MISMATCH"):
        preflight_block(
            capsule=tampered,
            history_ir=fixture.history_ir,
            paired_plans=fixture.plans,
            schedule_block=_block(UnitKind.CLEAN_CONTROL),
            history_registry=fixture.history_registry,
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
            preflight_store=store,
        )
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0
    run_roots = sorted((store.root / "runs").iterdir())
    assert len(run_roots) == 2
    for run_root in run_roots:
        events = store.load_events(run_root.name)
        assert [event["event_kind"] for event in events] == [
            "PLANNED",
            "PREFLIGHT_BLOCKED",
        ]
        assert events[-1]["payload"]["reason_code"] == "NON_HISTORY_PROJECTION_MISMATCH"


def test_unsupported_history_capability_blocks_before_encode_or_send() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    blocked_capabilities = replace(
        fixture.history_ir.capabilities,
        supported_arms=(ArmKind.ORIGINAL,),
    )
    blocked_ir = replace(fixture.history_ir, capabilities=blocked_capabilities)
    declaration = _LiveCodecDeclaration(
        codec_id=blocked_ir.codec_id,
        contract_version=blocked_ir.codec_contract_version,
        history_family=blocked_ir.history_family,
        capabilities=blocked_capabilities,
        frozen_ir=blocked_ir,
    )
    blocked_plans = tuple(
        build_fixture_plan(
            ir=blocked_ir,
            record_key="progress_step_1",
            arm=plan.arm,
        )
        for plan in fixture.plans
    )
    with pytest.raises(PortableContractError, match="UNSUPPORTED_PLAN_SET"):
        preflight_block(
            capsule=fixture.capsule,
            history_ir=blocked_ir,
            paired_plans=blocked_plans,
            schedule_block=_block(UnitKind.CLEAN_CONTROL),
            history_registry=_history_registry(declaration),
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
        )
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0


def test_live_execution_domain_is_hard_blocked_before_codec_methods() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    with pytest.raises(ReplayRunnerError, match="LIVE_EXECUTION_DEFERRED"):
        preflight_block(
            capsule=fixture.capsule,
            history_ir=fixture.history_ir,
            paired_plans=fixture.plans,
            schedule_block=_block(UnitKind.CLEAN_CONTROL),
            history_registry=fixture.history_registry,
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.LIVE_G1_SCIENTIFIC,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
        )
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0


def _scenarios() -> list[dict[str, Any]]:
    payload = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    assert payload["execution_domain"] == "FAKE_CONFORMANCE"
    assert payload["network_allowed"] is False
    assert payload["provider_invocation_allowed"] is False
    return cast(list[dict[str, Any]], payload["scenarios"])


@pytest.mark.parametrize("case", _scenarios(), ids=lambda item: item["id"])
def test_fake_provider_scenario_retry_and_stream_matrix(
    case: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"network path reached: {args!r} {kwargs!r}")

    monkeypatch.setattr(socket, "create_connection", forbidden_socket)
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    script = tuple(FakeScenario(value) for value in case["script"])
    provider_registry, provider = _provider_registry(script)
    prepared = _preflight(fixture, provider_registry, provider)
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert terminal["status"] == case["terminal_status"]
    assert terminal["provider_attempt_count"] == case["attempt_count"]
    assert terminal["generated_action_executed"] is False
    assert terminal["response_fed_to_later_request"] is False
    assert terminal["scientific_count_eligible"] is False
    assert provider.send_calls == case["attempt_count"]
    expected_normalize = 0 if case["terminal_status"] == "RETRY_EXHAUSTED" else 1
    assert provider.normalize_calls == expected_normalize
    assert len(provider.scenario_history) == case["attempt_count"]
    _validator("terminal_attempt.schema.json").validate(terminal)

    def assert_ref(ref: dict[str, Any]) -> bytes:
        data = (store.root / cast(str, ref["relative_path"])).read_bytes()
        assert len(data) == ref["byte_count"]
        assert hashlib.sha256(data).hexdigest() == ref["sha256"]
        return data

    events = store.load_events(original.invocation_plan.run_id)
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert events[0]["event_kind"] == "PLANNED"
    assert events[1]["event_kind"] == "PREFLIGHT_ALLOWED"
    assert events[-1]["event_kind"] == "TERMINAL"
    assert all(event["generated_action_executed"] is False for event in events)
    for event in events:
        _validator("attempt_event.schema.json").validate(event)
        ref = event["payload"].get("exchange_ref")
        if isinstance(ref, dict):
            exchange = json.loads((store.root / cast(str, ref["relative_path"])).read_bytes())
            _validator("provider_exchange.schema.json").validate(exchange)
            assert exchange["simulated"] is True
            assert exchange["external_provider_invoked"] is False
            assert exchange["gpu_used"] is False
            assert_ref(exchange["encoded_request_ref"])
            if exchange["raw_response_ref"] is not None:
                assert_ref(exchange["raw_response_ref"])
            for chunk in exchange["chunks"]:
                assert_ref(chunk["content_ref"])
    attempt_hashes = {
        event["payload"]["encoded_request_sha256"]
        for event in events
        if event["event_kind"] == "ATTEMPT_STARTED"
    }
    assert attempt_hashes == {original.authorized_request.encoded_request_sha256}
    failed_events = [event for event in events if event["event_kind"] == "FAILED"]
    assert all(
        event["payload"]["error_code"] in RETRYABLE_FAILURES
        and event["payload"]["retryable"] is True
        for event in failed_events
    )
    if case["terminal_status"] == "RETRY_EXHAUSTED":
        assert terminal["provider_attempt_count"] == MAXIMUM_PROVIDER_ATTEMPTS == 3
        assert [event["provider_attempt_index"] for event in failed_events] == [1, 2, 3]
        assert terminal["retry_reason"] in RETRYABLE_FAILURES
    provider_result = terminal["provider_result"]
    if provider_result is not None and provider_result["raw_response_ref"] is not None:
        assert_ref(provider_result["raw_response_ref"])
    chunk_events = [event for event in events if event["event_kind"] == "CHUNK"]
    if case["id"] == "streaming":
        assert [event["payload"]["chunk_index"] for event in chunk_events] == [0, 1, 2]
        assert chunk_events[-1]["payload"]["is_final"] is True
    if case["id"] == "partial_stream_retry":
        assert [event["payload"]["chunk_index"] for event in chunk_events] == [0, 1]
        assert all(event["payload"]["is_final"] is False for event in chunk_events)
    if case["terminal_status"] in {
        "PARSE_ERROR",
        "REFUSAL",
        "EMPTY_RESPONSE",
        "NO_OP",
    }:
        assert case["attempt_count"] == 1


@pytest.mark.parametrize(
    ("script", "expected_status", "expected_outcome", "expected_error"),
    (
        pytest.param(
            (FakeScenario.SUCCESS,),
            "SUCCESS",
            "PARSED",
            None,
            id="parsed",
        ),
        pytest.param(
            (FakeScenario.NO_OP,),
            "NO_OP",
            "PARSED",
            None,
            id="parsed-no-op",
        ),
        pytest.param(
            (FakeScenario.MALFORMED_RESPONSE,),
            "PARSE_ERROR",
            "FAILED",
            "MALFORMED_RESPONSE",
            id="failed",
        ),
        pytest.param(
            (FakeScenario.REFUSAL,),
            "REFUSAL",
            "REFUSAL",
            "REFUSAL",
            id="refusal",
        ),
        pytest.param(
            (FakeScenario.EMPTY_RESPONSE,),
            "EMPTY_RESPONSE",
            "EMPTY_RESPONSE",
            "EMPTY_RESPONSE",
            id="empty-response",
        ),
        pytest.param(
            (
                FakeScenario.TIMEOUT,
                FakeScenario.HTTP_5XX,
                FakeScenario.CONNECTION_ERROR,
            ),
            "RETRY_EXHAUSTED",
            "NOT_RUN",
            None,
            id="not-run",
        ),
    ),
)
def test_terminal_parser_diagnostics_exact_scenarios_are_schema_and_store_valid(
    tmp_path: Path,
    script: tuple[FakeScenario, ...],
    expected_status: str,
    expected_outcome: str,
    expected_error: str | None,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry(script)
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-diagnostics-valid",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    diagnostics = cast(dict[str, JsonValue], terminal["parser_diagnostics"])

    assert terminal["status"] == expected_status
    assert diagnostics["parse_outcome"] == expected_outcome
    if expected_outcome == "PARSED":
        assert set(diagnostics) == {
            "parser_binding_id",
            "parse_outcome",
            "action_count",
            "action_sha256",
        }
        assert diagnostics["action_count"] == 1
        assert isinstance(diagnostics["action_sha256"], str)
    elif expected_outcome == "NOT_RUN":
        assert diagnostics == {"parse_outcome": "NOT_RUN"}
    else:
        assert set(diagnostics) == {
            "parser_binding_id",
            "parse_outcome",
            "action_count",
            "error_code",
        }
        assert diagnostics["action_count"] == 0
        assert diagnostics["error_code"] == expected_error
    _validator("terminal_attempt.schema.json").validate(terminal)
    assert store._read_structural_terminal(original.invocation_plan.run_id) == terminal


def test_formal_terminal_commit_runs_full_closure_before_writing_terminal(
    tmp_path: Path,
) -> None:
    class ExchangeTamperingStore(ReplayArtifactStore):
        structural_commit_calls = 0
        tree_after_tamper: tuple[tuple[str, str, str | bytes | None], ...] | None = None

        def append_event(
            self,
            *,
            run_id: str,
            event_kind: AttemptEventKind,
            provider_attempt_index: int | None,
            payload: dict[str, JsonValue],
        ) -> Any:
            record = super().append_event(
                run_id=run_id,
                event_kind=event_kind,
                provider_attempt_index=provider_attempt_index,
                payload=payload,
            )
            if event_kind is AttemptEventKind.TERMINAL:
                returned = next(
                    event
                    for event in reversed(self.load_events(run_id))
                    if event["event_kind"] == AttemptEventKind.RETURNED.value
                )
                returned_payload = cast(dict[str, JsonValue], returned["payload"])
                exchange_ref = cast(dict[str, JsonValue], returned_payload["exchange_ref"])
                (self.root / cast(str, exchange_ref["relative_path"])).unlink()
                self.tree_after_tamper = _output_tree_snapshot(self.root)
            return record

        def _commit_structural_terminal(
            self,
            run_id: str,
            value: dict[str, JsonValue],
        ) -> bool:
            self.structural_commit_calls += 1
            return super()._commit_structural_terminal(run_id, value)

    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ExchangeTamperingStore(
        tmp_path / "formal-commit-full-closure",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal_path = store.root / "runs" / original.invocation_plan.run_id / "terminal.json"

    with pytest.raises(ReplayRunnerError):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert store.tree_after_tamper is not None
    assert _output_tree_snapshot(store.root) == store.tree_after_tamper
    assert store.structural_commit_calls == 0
    assert not terminal_path.exists()
    assert provider.send_calls == provider.normalize_calls == 1


@pytest.mark.parametrize(
    ("scenario", "mutation"),
    (
        pytest.param(FakeScenario.SUCCESS, "nested-missing", id="success-missing"),
        pytest.param(FakeScenario.SUCCESS, "nested-parse-error", id="success-parse-error"),
        pytest.param(
            FakeScenario.MALFORMED_RESPONSE,
            "wrong-status",
            id="parse-error-wrong-status",
        ),
        pytest.param(
            FakeScenario.MALFORMED_RESPONSE,
            "wrong-error",
            id="parse-error-wrong-error",
        ),
        pytest.param(
            FakeScenario.EMPTY_RESPONSE,
            "wrong-error",
            id="empty-response-wrong-error",
        ),
        pytest.param(FakeScenario.REFUSAL, "wrong-status", id="refusal-wrong-status"),
        pytest.param(FakeScenario.REFUSAL, "wrong-error", id="refusal-wrong-error"),
    ),
)
def test_terminal_schema_rejects_top_status_and_nested_provider_result_mismatch(
    tmp_path: Path,
    scenario: FakeScenario,
    mutation: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((scenario,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-schema-provider-binding",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    invalid = cast(dict[str, JsonValue], copy_json(cast(JsonValue, terminal)))
    provider_result = cast(dict[str, JsonValue], invalid["provider_result"])
    error = cast(dict[str, JsonValue], provider_result["error"])
    if mutation == "nested-missing":
        provider_result["status"] = "MISSING"
        provider_result["response_sha256"] = None
        provider_result["raw_response_ref"] = None
        provider_result["normalized_action"] = None
        provider_result["normalized_action_sha256"] = None
        provider_result["error"] = {
            "code": "MISSING",
            "message": "schema-only missing provider result",
            "retryable": False,
        }
    elif mutation == "nested-parse-error":
        provider_result["status"] = "PARSE_ERROR"
        provider_result["normalized_action"] = None
        provider_result["normalized_action_sha256"] = None
        provider_result["error"] = {
            "code": "MALFORMED_RESPONSE",
            "message": "schema-only parse error",
            "retryable": False,
        }
    elif mutation == "wrong-status":
        provider_result["status"] = (
            "PROVIDER_ERROR" if terminal["status"] == "PARSE_ERROR" else "PARSE_ERROR"
        )
    elif terminal["status"] == "PARSE_ERROR":
        error["code"] = "EMPTY_RESPONSE"
    else:
        error["code"] = "MALFORMED_RESPONSE"

    provider_schema = json.loads(
        (G12_SCHEMA_ROOT / "provider_result.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(provider_schema).validate(provider_result)
    validator = _validator("terminal_attempt.schema.json")
    validator.validate(terminal)
    assert not validator.is_valid(invalid)


@pytest.mark.parametrize("operation", ("commit", "read"))
@pytest.mark.parametrize(
    "mutation",
    (
        "missing-top-level",
        "missing-required-member",
        "extra-member",
        "boolean-action-count",
        "confidential-value-in-hash-slot",
    ),
)
def test_terminal_schema_commit_and_read_reject_nonclosed_parser_diagnostics(
    tmp_path: Path,
    operation: str,
    mutation: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-diagnostics-invalid",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    invalid = cast(dict[str, JsonValue], copy_json(cast(JsonValue, terminal)))
    if mutation == "missing-top-level":
        del invalid["parser_diagnostics"]
    else:
        diagnostics = cast(dict[str, JsonValue], invalid["parser_diagnostics"])
        if mutation == "missing-required-member":
            del diagnostics["action_sha256"]
        elif mutation == "extra-member":
            diagnostics["unexpected"] = False
        elif mutation == "boolean-action-count":
            diagnostics["action_count"] = True
        else:
            diagnostics["action_sha256"] = MODEL_ID
    validator = _validator("terminal_attempt.schema.json")
    assert not validator.is_valid(invalid)
    run_id = original.invocation_plan.run_id
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    if operation == "read":
        terminal_path.write_bytes(canonical_json_bytes(invalid))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        if operation == "commit":
            store._commit_structural_terminal(run_id, invalid)
        else:
            store._read_structural_terminal(run_id)
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.exists() is (operation == "read")
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


def test_terminal_commit_rejects_provider_result_and_parser_diagnostic_cross_splices(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    prepared = _preflight(fixture, provider_registry, provider)
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    sham = next(item for item in prepared if item.schedule.arm is ArmKind.SHAM_BENIGN_EDIT)
    store = ReplayArtifactStore(
        tmp_path / "terminal-cross-splice",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    original_terminal = execute_fake_arm(
        original,
        provider_registry=provider_registry,
        store=store,
    )
    sham_terminal = execute_fake_arm(
        sham,
        provider_registry=provider_registry,
        store=store,
    )
    no_op_registry, no_op_provider = _provider_registry((FakeScenario.NO_OP,))
    no_op_original = next(
        item
        for item in _preflight(fixture, no_op_registry, no_op_provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    no_op_store = ReplayArtifactStore(
        tmp_path / "terminal-cross-splice-no-op",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    no_op_terminal = execute_fake_arm(
        no_op_original,
        provider_registry=no_op_registry,
        store=no_op_store,
    )
    provider_splice = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, original_terminal)),
    )
    provider_splice["provider_result"] = copy_json(
        cast(JsonValue, sham_terminal["provider_result"])
    )
    diagnostics_splice = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, original_terminal)),
    )
    diagnostics_splice["parser_diagnostics"] = copy_json(
        cast(JsonValue, no_op_terminal["parser_diagnostics"])
    )
    events = store.load_events(original.invocation_plan.run_id)
    parser_payload = cast(dict[str, JsonValue], events[-2]["payload"])
    assert parser_payload["provider_result_sha256"] != canonical_sha256(
        cast(JsonValue, provider_splice["provider_result"])
    )
    assert canonical_json_bytes(parser_payload["parser_diagnostics"]) != canonical_json_bytes(
        diagnostics_splice["parser_diagnostics"]
    )
    validator = _validator("terminal_attempt.schema.json")
    validator.validate(provider_splice)
    validator.validate(diagnostics_splice)
    run_id = original.invocation_plan.run_id
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    for invalid in (provider_splice, diagnostics_splice):
        terminal_path.write_bytes(canonical_json_bytes(invalid))
        tree_before = _output_tree_snapshot(store.root)
        with pytest.raises(ReplayRunnerError) as raised:
            execute_fake_arm(original, provider_registry=provider_registry, store=store)
        assert raised.value.code == "TERMINAL_RECORD_INVALID"
        assert _output_tree_snapshot(store.root) == tree_before
        terminal_path.unlink()
    assert provider.send_calls == send_count == 2
    assert provider.normalize_calls == normalize_count == 2
    assert no_op_provider.send_calls == no_op_provider.normalize_calls == 1


@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
@pytest.mark.parametrize(
    ("scenario", "expected_parser_kind"),
    (
        pytest.param(FakeScenario.SUCCESS, AttemptEventKind.PARSED, id="parsed-action-sha"),
        pytest.param(
            FakeScenario.MALFORMED_RESPONSE,
            AttemptEventKind.PARSE_FAILED,
            id="parse-failed-error-code",
        ),
    ),
)
def test_formal_entrypoints_reject_rechained_diagnostics_result_mismatch(
    tmp_path: Path,
    entrypoint: str,
    scenario: FakeScenario,
    expected_parser_kind: AttemptEventKind,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((scenario,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-diagnostics-result-mismatch",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    events = store.load_events(run_id)
    parser_event = deepcopy(events[-2])
    terminal_event = deepcopy(events[-1])
    assert parser_event["event_kind"] == expected_parser_kind.value
    assert terminal_event["event_kind"] == AttemptEventKind.TERMINAL.value
    forged_diagnostics = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal["parser_diagnostics"])),
    )
    provider_result = cast(dict[str, JsonValue], terminal["provider_result"])
    if expected_parser_kind is AttemptEventKind.PARSED:
        assert forged_diagnostics["action_sha256"] == provider_result["normalized_action_sha256"]
        forged_diagnostics["action_sha256"] = "f" * 64
        assert forged_diagnostics["action_sha256"] != provider_result["normalized_action_sha256"]
    else:
        provider_error = cast(dict[str, JsonValue], provider_result["error"])
        assert forged_diagnostics["error_code"] == provider_error["code"]
        forged_diagnostics["error_code"] = "PARSER_FAILURE"
        assert forged_diagnostics["error_code"] != provider_error["code"]
    parser_payload = cast(dict[str, JsonValue], parser_event["payload"])
    parser_payload["parser_diagnostics"] = copy_json(forged_diagnostics)

    def rebind(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    preceding_sha256 = canonical_sha256(cast(JsonValue, events[-3]))
    parser_sha256 = rebind(parser_event, preceding_sha256)
    terminal_payload = cast(dict[str, JsonValue], terminal_event["payload"])
    terminal_payload["preceding_event_sha256"] = parser_sha256
    terminal_sha256 = rebind(terminal_event, parser_sha256)
    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[-2:]:
        stale_path = event_dir / (
            f"{stale['seq']:04d}-{canonical_sha256(cast(JsonValue, stale))}.json"
        )
        stale_path.unlink()
    for forged, digest in (
        (parser_event, parser_sha256),
        (terminal_event, terminal_sha256),
    ):
        (event_dir / f"{forged['seq']:04d}-{digest}.json").write_bytes(
            canonical_json_bytes(cast(JsonValue, forged))
        )
    assert store.load_events(run_id)[-2]["payload"] == parser_event["payload"]

    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_terminal["parser_diagnostics"] = copy_json(forged_diagnostics)
    forged_terminal["final_event_sha256"] = terminal_sha256
    validator = _validator("terminal_attempt.schema.json")
    if expected_parser_kind is AttemptEventKind.PARSED:
        validator.validate(forged_terminal)
    else:
        assert not validator.is_valid(forged_terminal)
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
@pytest.mark.parametrize(
    "identity_override",
    (
        pytest.param(
            {"provider_codec_id": "mobileworld.g1.provider.external/v1"},
            id="external-codec-id",
        ),
        pytest.param(
            {"provider_contract_version": "live-v1"},
            id="live-contract-version",
        ),
        pytest.param(
            {"endpoint_revision": "https://provider.example/v1/chat/completions"},
            id="https-endpoint",
        ),
        pytest.param(
            {
                "provider_codec_id": "mobileworld.g1.provider.external/v1",
                "provider_contract_version": "live-v1",
                "endpoint_revision": "https://provider.example/v1/chat/completions",
            },
            id="combined-live-identity",
        ),
    ),
)
def test_terminal_schema_and_formal_entrypoints_reject_rechained_nonfake_provider_identity(
    tmp_path: Path,
    entrypoint: str,
    identity_override: dict[str, str],
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-nonfake-provider-identity",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_result = cast(dict[str, JsonValue], forged_terminal["provider_result"])
    forged_result.update(identity_override)
    provider_schema = json.loads(
        (G12_SCHEMA_ROOT / "provider_result.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(provider_schema).validate(forged_result)
    events = store.load_events(run_id)
    parser_event = deepcopy(events[-2])
    terminal_event = deepcopy(events[-1])
    parser_payload = cast(dict[str, JsonValue], parser_event["payload"])
    parser_payload["provider_result_sha256"] = canonical_sha256(forged_result)

    def rebind(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    preceding_sha256 = canonical_sha256(cast(JsonValue, events[-3]))
    parser_sha256 = rebind(parser_event, preceding_sha256)
    terminal_payload = cast(dict[str, JsonValue], terminal_event["payload"])
    terminal_payload["preceding_event_sha256"] = parser_sha256
    terminal_sha256 = rebind(terminal_event, parser_sha256)
    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[-2:]:
        (event_dir / f"{stale['seq']:04d}-{canonical_sha256(stale)}.json").unlink()
    for forged, digest in (
        (parser_event, parser_sha256),
        (terminal_event, terminal_sha256),
    ):
        (event_dir / f"{forged['seq']:04d}-{digest}.json").write_bytes(
            canonical_json_bytes(cast(JsonValue, forged))
        )
    assert store.load_events(run_id)[-2]["payload"] == parser_event["payload"]
    forged_terminal["final_event_sha256"] = terminal_sha256
    validator = _validator("terminal_attempt.schema.json")
    assert not validator.is_valid(forged_terminal)
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
@pytest.mark.parametrize(
    ("scenario", "mutation"),
    (
        pytest.param(FakeScenario.REFUSAL, "missing-response", id="refusal-missing-response"),
        pytest.param(
            FakeScenario.MALFORMED_RESPONSE,
            "retryable-true",
            id="parse-error-retryable",
        ),
        pytest.param(FakeScenario.REFUSAL, "retryable-true", id="refusal-retryable"),
        pytest.param(
            FakeScenario.EMPTY_RESPONSE,
            "retryable-true",
            id="empty-response-retryable",
        ),
        pytest.param(FakeScenario.SUCCESS, "wrong-ref-path", id="response-ref-path"),
        pytest.param(FakeScenario.SUCCESS, "wrong-ref-media", id="response-ref-media"),
        pytest.param(FakeScenario.SUCCESS, "wrong-ref-schema", id="response-ref-schema"),
    ),
)
def test_terminal_schema_and_formal_entrypoints_reject_rechained_invalid_result_receipt(
    tmp_path: Path,
    entrypoint: str,
    scenario: FakeScenario,
    mutation: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((scenario,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-invalid-result-receipt",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_result = cast(dict[str, JsonValue], forged_terminal["provider_result"])
    if mutation == "missing-response":
        forged_result["response_sha256"] = None
        forged_result["raw_response_ref"] = None
    elif mutation == "retryable-true":
        forged_error = cast(dict[str, JsonValue], forged_result["error"])
        forged_error["retryable"] = True
    else:
        response_ref = cast(dict[str, JsonValue], forged_result["raw_response_ref"])
        if mutation == "wrong-ref-path":
            response_ref["relative_path"] = f"external/responses/{response_ref['sha256']}"
        elif mutation == "wrong-ref-media":
            response_ref["media_type"] = "application/json"
        else:
            response_ref["schema_version"] = "mobileworld.g1.external-response/v1"
    provider_schema = json.loads(
        (G12_SCHEMA_ROOT / "provider_result.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(provider_schema).validate(forged_result)
    events = store.load_events(run_id)
    parser_event = deepcopy(events[-2])
    terminal_event = deepcopy(events[-1])
    parser_payload = cast(dict[str, JsonValue], parser_event["payload"])
    parser_payload["provider_result_sha256"] = canonical_sha256(forged_result)

    def rebind(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    preceding_sha256 = canonical_sha256(cast(JsonValue, events[-3]))
    parser_sha256 = rebind(parser_event, preceding_sha256)
    terminal_payload = cast(dict[str, JsonValue], terminal_event["payload"])
    terminal_payload["preceding_event_sha256"] = parser_sha256
    terminal_sha256 = rebind(terminal_event, parser_sha256)
    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[-2:]:
        (event_dir / f"{stale['seq']:04d}-{canonical_sha256(stale)}.json").unlink()
    for forged, digest in (
        (parser_event, parser_sha256),
        (terminal_event, terminal_sha256),
    ):
        (event_dir / f"{forged['seq']:04d}-{digest}.json").write_bytes(
            canonical_json_bytes(cast(JsonValue, forged))
        )
    assert store.load_events(run_id)[-2]["payload"] == parser_event["payload"]
    forged_terminal["final_event_sha256"] = terminal_sha256
    validator = _validator("terminal_attempt.schema.json")
    assert not validator.is_valid(forged_terminal)
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
def test_formal_entrypoints_reject_alternate_response_not_bound_to_returned_event(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-alternate-response",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    second_response = canonical_json_bytes(
        {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"type": "click", "coordinate": [303, 404]},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    }
                }
            ]
        }
    )
    second_ref = store.put_bytes(second_response, media_type="application/octet-stream")
    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_result = cast(dict[str, JsonValue], forged_terminal["provider_result"])
    forged_result["response_sha256"] = second_ref["sha256"]
    forged_result["raw_response_ref"] = {
        **second_ref,
        "schema_version": None,
    }
    run_id = original.invocation_plan.run_id
    events = store.load_events(run_id)
    returned_payload = cast(dict[str, JsonValue], events[-3]["payload"])
    assert returned_payload["response_ref"] != second_ref
    parser_event = deepcopy(events[-2])
    terminal_event = deepcopy(events[-1])
    parser_payload = cast(dict[str, JsonValue], parser_event["payload"])
    parser_payload["provider_result_sha256"] = canonical_sha256(forged_result)

    def rebind(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    returned_sha256 = canonical_sha256(cast(JsonValue, events[-3]))
    parser_sha256 = rebind(parser_event, returned_sha256)
    terminal_payload = cast(dict[str, JsonValue], terminal_event["payload"])
    terminal_payload["preceding_event_sha256"] = parser_sha256
    terminal_sha256 = rebind(terminal_event, parser_sha256)
    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[-2:]:
        (event_dir / f"{stale['seq']:04d}-{canonical_sha256(stale)}.json").unlink()
    for forged, digest in (
        (parser_event, parser_sha256),
        (terminal_event, terminal_sha256),
    ):
        (event_dir / f"{forged['seq']:04d}-{digest}.json").write_bytes(
            canonical_json_bytes(cast(JsonValue, forged))
        )
    assert store.load_events(run_id)[-2]["payload"] == parser_event["payload"]
    forged_terminal["final_event_sha256"] = terminal_sha256
    _validator("terminal_attempt.schema.json").validate(forged_terminal)
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert store.read_artifact_ref(second_ref) == second_response
    assert terminal_path.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


@pytest.mark.parametrize("damage", ("deleted", "dangling"))
@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
def test_terminal_read_rejects_unresolved_returned_exchange_without_side_effects(
    tmp_path: Path,
    damage: str,
    entrypoint: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "terminal-unresolved-returned-exchange",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    events = store.load_events(run_id)
    assert events[-3]["event_kind"] == AttemptEventKind.RETURNED.value
    returned_payload = cast(dict[str, JsonValue], events[-3]["payload"])
    exchange_ref = cast(dict[str, JsonValue], returned_payload["exchange_ref"])
    exchange_path = store.root / cast(str, exchange_ref["relative_path"])
    exchange_path.unlink()
    dangling_target = tmp_path / "missing-exchange-target.json"
    if damage == "dangling":
        exchange_path.symlink_to(dangling_target)
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "ARTIFACT_REF_UNRESOLVED"
    assert _output_tree_snapshot(store.root) == tree_before
    if damage == "dangling":
        assert exchange_path.is_symlink()
        assert exchange_path.readlink() == dangling_target
        assert not dangling_target.exists()
    else:
        assert not exchange_path.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


@pytest.mark.parametrize("entrypoint", ("reuse", "blinded-export"))
@pytest.mark.parametrize(
    "telemetry_mutation",
    (
        pytest.param("latency-negative", id="latency-negative"),
        pytest.param("latency-null", id="latency-null"),
        pytest.param("latency-bool", id="latency-bool"),
        pytest.param("token-missing", id="token-missing"),
        pytest.param("token-extra", id="token-extra"),
        pytest.param("token-negative", id="token-negative"),
        pytest.param("token-bool", id="token-bool"),
    ),
)
def test_formal_entrypoints_reject_rechained_invalid_returned_telemetry_without_side_effects(
    tmp_path: Path,
    entrypoint: str,
    telemetry_mutation: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "invalid-returned-telemetry",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    events = store.load_events(run_id)
    returned_index = next(
        index
        for index, event in enumerate(events)
        if event["event_kind"] == AttemptEventKind.RETURNED.value
    )
    assert [event["event_kind"] for event in events[returned_index:]] == [
        AttemptEventKind.RETURNED.value,
        AttemptEventKind.PARSED.value,
        AttemptEventKind.TERMINAL.value,
    ]
    returned_event = deepcopy(events[returned_index])
    returned_payload = cast(dict[str, JsonValue], returned_event["payload"])
    exchange_ref = cast(dict[str, JsonValue], returned_payload["exchange_ref"])
    exchange = cast(
        dict[str, JsonValue],
        json.loads(store.read_artifact_ref(exchange_ref)),
    )
    exchange_validator = _validator("provider_exchange.schema.json")
    exchange_validator.validate(exchange)
    token_usage = cast(dict[str, JsonValue], exchange["token_usage"])
    if telemetry_mutation == "latency-negative":
        exchange["latency_ms"] = -1
    elif telemetry_mutation == "latency-null":
        exchange["latency_ms"] = None
    elif telemetry_mutation == "latency-bool":
        exchange["latency_ms"] = True
    elif telemetry_mutation == "token-missing":
        del token_usage["input_tokens"]
    elif telemetry_mutation == "token-extra":
        token_usage["unexpected"] = 0
    elif telemetry_mutation == "token-negative":
        token_usage["input_tokens"] = -1
    else:
        assert telemetry_mutation == "token-bool"
        token_usage["input_tokens"] = True
    assert not exchange_validator.is_valid(exchange)
    returned_payload["exchange_ref"] = store.put_json(exchange)

    parser_event = deepcopy(events[returned_index + 1])
    terminal_event = deepcopy(events[returned_index + 2])

    def rebind(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    previous_sha256 = canonical_sha256(cast(JsonValue, events[returned_index - 1]))
    returned_sha256 = rebind(returned_event, previous_sha256)
    parser_sha256 = rebind(parser_event, returned_sha256)
    terminal_payload = cast(dict[str, JsonValue], terminal_event["payload"])
    terminal_payload["preceding_event_sha256"] = parser_sha256
    terminal_sha256 = rebind(terminal_event, parser_sha256)
    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[returned_index:]:
        (event_dir / f"{stale['seq']:04d}-{canonical_sha256(stale)}.json").unlink()
    for forged, digest in (
        (returned_event, returned_sha256),
        (parser_event, parser_sha256),
        (terminal_event, terminal_sha256),
    ):
        _validator("attempt_event.schema.json").validate(forged)
        (event_dir / f"{forged['seq']:04d}-{digest}.json").write_bytes(
            canonical_json_bytes(cast(JsonValue, forged))
        )
    assert store.load_events(run_id)[returned_index:] == (
        returned_event,
        parser_event,
        terminal_event,
    )

    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_terminal["final_event_sha256"] = terminal_sha256
    _validator("terminal_attempt.schema.json").validate(forged_terminal)
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))
    tree_before = _output_tree_snapshot(store.root)
    send_count = provider.send_calls
    normalize_count = provider.normalize_calls
    confidential_binding = store.root / "runs" / run_id / "confidential/blinded-packet-binding.json"
    assert not (store.root / "scorer").exists()
    assert not confidential_binding.exists()

    with pytest.raises(ReplayRunnerError) as raised:
        _invoke_formal_completed_entrypoint(
            entrypoint,
            original,
            provider_registry=provider_registry,
            store=store,
        )
    assert raised.value.code == "PREPARED_ARM_BINDING_MISMATCH"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.exists()
    assert not (store.root / "scorer").exists()
    assert not confidential_binding.exists()
    assert provider.send_calls == send_count == 1
    assert provider.normalize_calls == normalize_count == 1


def test_retry_exhausted_terminal_reuse_rejects_catalog_reason_not_bound_to_last_failure(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry(
        (
            FakeScenario.TIMEOUT,
            FakeScenario.HTTP_5XX,
            FakeScenario.CONNECTION_ERROR,
        )
    )
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    events_before = store.load_events(original.invocation_plan.run_id)
    failed_codes = tuple(
        event["payload"]["error_code"]
        for event in events_before
        if event["event_kind"] == AttemptEventKind.FAILED.value
    )
    assert failed_codes == RETRYABLE_FAILURES
    assert terminal["status"] == "RETRY_EXHAUSTED"
    assert terminal["retry_reason"] == failed_codes[-1] == "CONNECTION_ERROR"
    tampered_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    tampered_terminal["retry_reason"] = "TIMEOUT"
    terminal_path = store.root / "runs" / original.invocation_plan.run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(tampered_terminal))
    send_count = provider.send_calls

    with pytest.raises(ReplayRunnerError, match="TERMINAL_RECORD_INVALID"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert provider.send_calls == send_count == MAXIMUM_PROVIDER_ATTEMPTS
    assert provider.normalize_calls == 0
    assert store.load_events(original.invocation_plan.run_id) == events_before


def test_execute_persists_blinding_mapping_and_commitment_before_first_send(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    pre_send_observations: list[int] = []

    class ObservingReplayArtifactStore(ReplayArtifactStore):
        def append_event(
            self,
            *,
            run_id: str,
            event_kind: AttemptEventKind,
            provider_attempt_index: int | None,
            payload: dict[str, JsonValue],
        ) -> Any:
            record = super().append_event(
                run_id=run_id,
                event_kind=event_kind,
                provider_attempt_index=provider_attempt_index,
                payload=payload,
            )
            if event_kind is AttemptEventKind.ATTEMPT_STARTED:
                events = self.load_events(run_id)
                assert [event["event_kind"] for event in events] == [
                    "PLANNED",
                    "PREFLIGHT_ALLOWED",
                    "ATTEMPT_STARTED",
                ]
                mapping_path = self.root / "runs" / run_id / "confidential/blinding-map.json"
                assert mapping_path.is_file()
                mapping_bytes = mapping_path.read_bytes()
                mapping = json.loads(mapping_bytes)
                _validator("blinding_mapping.schema.json").validate(mapping)
                commitment = events[0]["payload"]["blinding_commitment"]
                assert commitment["mapping_persisted_before_response"] is True
                assert (
                    commitment["blinding_mapping_sha256"]
                    == hashlib.sha256(mapping_bytes).hexdigest()
                )
                assert commitment["key_commitment_sha256"] == mapping["key_commitment_sha256"]
                assert set(commitment) == {
                    "blinding_mapping_sha256",
                    "key_commitment_sha256",
                    "mapping_persisted_before_response",
                }
                assert mapping["run_id"] == run_id
                assert mapping["arm_id"] == original.schedule.arm.value
                assert mapping["schedule_id"] == original.schedule.schedule_id
                assert mapping["blinded_packet_id"].startswith("g1blind-")
                public_events = json.dumps(events, sort_keys=True)
                assert "blinded_packet_id" not in public_events
                assert mapping["blinded_packet_id"] not in public_events
                assert "blinding-map" not in public_events
                assert "confidential/" not in public_events
                pre_send_observations.append(provider.send_calls)
            return record

    store = ObservingReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert terminal["status"] == "SUCCESS"
    assert pre_send_observations == [0]
    assert provider.send_calls == 1


def test_retry_catalog_is_exact_and_send_descriptor_mutation_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert RETRYABLE_FAILURES == ("TIMEOUT", "HTTP_5XX", "CONNECTION_ERROR")
    assert MAXIMUM_PROVIDER_ATTEMPTS == 3
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    send_calls = 0

    def fail_with_unregistered_code(codec: Any, authorized: Any) -> Any:
        nonlocal send_calls
        del codec, authorized
        send_calls += 1
        raise ProviderTransportFailure("RATE_LIMIT", "not registered", True)

    monkeypatch.setattr(
        DeterministicFakeProviderCodec,
        "send",
        fail_with_unregistered_code,
    )
    with pytest.raises(ReplayRunnerError, match="FAKE_PROVIDER_IMPLEMENTATION_MISMATCH"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert send_calls == provider.send_calls == provider.normalize_calls == 0
    assert tuple(store.root.rglob("*")) == ()


def test_custom_parser_diagnostics_are_preserved_by_normalize_only_path() -> None:
    expected_action: dict[str, JsonValue] = {
        "type": "click",
        "coordinate": [7, 9],
    }
    expected_diagnostics: dict[str, JsonValue] = {
        "parser_binding_id": "mobileworld.g1.fixture-exact-diagnostics/v1",
        "parse_outcome": "FIXTURE_EXACT",
        "action_count": 1,
        "host_trace": {"stage": "target-pre", "code": 17},
    }

    def parse_exact(response_bytes: bytes) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        assert response_bytes
        return (
            cast(dict[str, JsonValue], copy_json(expected_action)),
            cast(dict[str, JsonValue], copy_json(expected_diagnostics)),
        )

    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, preflight_provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, preflight_provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    provider = OpenAICompatibleProviderCodec(
        codec_id=original.authorized_request.provider_codec_id,
        endpoint_revision=original.authorized_request.endpoint_revision,
        parser=parser_adapter(
            "mobileworld.g1.fixture-exact-diagnostics/v1",
            parse_exact,
            implementation_sha256="6" * 64,
        ),
    )
    encoded = provider.encode(
        original.render_result.rendered_request,
        original.authorized_request.prepared.model_parameters,
    )
    assert encoded == original.authorized_request.prepared
    response = RawProviderResponse(
        response_bytes=canonical_json_bytes({"fixture": "exact-parser-diagnostics"})
    )
    result = provider.normalize(original.authorized_request, response)
    diagnostics = provider.consume_parser_diagnostics()

    assert result.normalized_action == expected_action
    assert diagnostics == expected_diagnostics
    assert provider.encode_calls == provider.normalize_calls == 1
    assert provider.send_calls == 0
    assert preflight_provider.send_calls == preflight_provider.normalize_calls == 0


def test_custom_parser_is_rejected_at_preflight_without_store_or_provider_calls(
    tmp_path: Path,
) -> None:
    provider = DeterministicFakeProviderCodec(
        (FakeScenario.SUCCESS,),
        parser=parser_adapter(
            "mobileworld.g1.fixture-normalize-only-parser/v1",
            lambda _: ({"type": "wait"}, {"parse_outcome": "PARSED"}),
            implementation_sha256="6" * 64,
        ),
    )
    provider_registry = ProviderCodecRegistry()
    provider_registry.register(provider)
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )

    with pytest.raises(ReplayRunnerError, match="EXECUTABLE_PARSER_DEFERRED"):
        _preflight(
            fixture,
            provider_registry,
            provider,
            preflight_store=store,
        )
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0
    assert getattr(provider, "_active_run_id") is None
    assert getattr(provider, "_scenario_history_by_run") == {}


@pytest.mark.parametrize("hash_field", ("code_sha256", "config_sha256"))
@pytest.mark.parametrize(
    "invalid_sha",
    (
        pytest.param("not-a-sha256", id="non-sha"),
        pytest.param("A" * 64, id="uppercase"),
        pytest.param(cast(str, True), id="boolean"),
    ),
)
def test_preflight_rejects_invalid_code_and_config_sha_admission_without_side_effects(
    tmp_path: Path,
    hash_field: str,
    invalid_sha: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    hashes = {
        "code_sha256": "2" * 64,
        "config_sha256": "3" * 64,
    }
    hashes[hash_field] = invalid_sha

    with pytest.raises(ReplayRunnerError, match="INVOCATION_PLAN_INVALID"):
        _preflight(
            fixture,
            provider_registry,
            provider,
            code_sha256=hashes["code_sha256"],
            config_sha256=hashes["config_sha256"],
            preflight_store=store,
        )
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0
    assert getattr(provider, "_active_run_id") is None


@pytest.mark.parametrize(
    ("binding_field", "invalid_value"),
    (
        ("model_id", "not_a_registered_model"),
        ("unit_id", "not-a-g1-unit"),
        ("capsule_id", "not-a-g1-capsule"),
        ("history_family", "unknown_history_family"),
    ),
)
def test_invocation_plan_rejects_invalid_synthetic_capsule_binding_without_side_effects(
    tmp_path: Path,
    binding_field: str,
    invalid_value: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    binding = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, original.invocation_plan.capsule_binding)),
    )
    binding[binding_field] = invalid_value
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    encode_count = provider.encode_calls

    with pytest.raises(ReplayRunnerError, match="INVOCATION_PLAN_INVALID"):
        replace(original.invocation_plan, capsule_binding=binding)
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == encode_count
    assert provider.send_calls == provider.normalize_calls == 0
    assert getattr(provider, "_active_run_id") is None


def _assert_execution_rejected_without_side_effects(
    *,
    prepared: Any,
    provider_registry: ProviderCodecRegistry,
    providers: tuple[DeterministicFakeProviderCodec, ...],
    output_root: Path,
    expected_error: str = "PREPARED_ARM_BINDING_MISMATCH",
) -> None:
    store = ReplayArtifactStore(
        output_root,
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    encode_counts = tuple(provider.encode_calls for provider in providers)
    with pytest.raises(ReplayRunnerError, match=expected_error):
        execute_fake_arm(prepared, provider_registry=provider_registry, store=store)
    assert tuple(store.root.rglob("*")) == ()
    assert tuple(provider.encode_calls for provider in providers) == encode_counts
    for provider in providers:
        assert provider.send_calls == provider.normalize_calls == 0
        assert provider.scenario_history == []
        assert getattr(provider, "_active_run_id") is None
        assert getattr(provider, "_scenario_history_by_run") == {}


def test_execute_rejects_authorized_request_spliced_from_another_arm_without_side_effects(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    prepared = _preflight(fixture, provider_registry, provider)
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    sham = next(item for item in prepared if item.schedule.arm is ArmKind.SHAM_BENIGN_EDIT)
    assert original.authorized_request.encoded_request_sha256 != (
        sham.authorized_request.encoded_request_sha256
    )
    spliced = replace(original, authorized_request=sham.authorized_request)

    _assert_execution_rejected_without_side_effects(
        prepared=spliced,
        provider_registry=provider_registry,
        providers=(provider,),
        output_root=tmp_path / "spliced-request",
    )


def test_execute_rejects_replaced_invocation_run_id_without_side_effects(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    replacement_run_id = "g1run-000000000000000000000000"
    if original.invocation_plan.run_id == replacement_run_id:
        replacement_run_id = "g1run-111111111111111111111111"
    tampered = replace(
        original,
        invocation_plan=replace(
            original.invocation_plan,
            run_id=replacement_run_id,
        ),
    )

    _assert_execution_rejected_without_side_effects(
        prepared=tampered,
        provider_registry=provider_registry,
        providers=(provider,),
        output_root=tmp_path / "replaced-run-id",
        expected_error="INVOCATION_PLAN_BINDING_MISMATCH",
    )


def test_execute_rejects_mutated_preflight_scenario_script_without_side_effects(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    provider._scenarios = (FakeScenario.NO_OP,)

    _assert_execution_rejected_without_side_effects(
        prepared=original,
        provider_registry=provider_registry,
        providers=(provider,),
        output_root=tmp_path / "mutated-scenarios",
    )


@pytest.mark.parametrize(
    "attribute",
    ("send", "normalize", "begin_run", "configuration"),
)
def test_fake_provider_instance_rejects_same_instance_method_assignment(
    attribute: str,
) -> None:
    provider = DeterministicFakeProviderCodec((FakeScenario.SUCCESS,))
    assert not hasattr(provider, "__dict__")

    with pytest.raises(AttributeError):
        setattr(provider, attribute, object())
    assert callable(getattr(provider, attribute))


def test_json_parser_instance_rejects_same_instance_parse_assignment() -> None:
    def parse_wait(_: bytes) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
        return {"type": "wait"}, {"parse_outcome": "PARSED"}

    parser = JsonActionParser()
    assert not hasattr(parser, "__dict__")
    with pytest.raises(AttributeError):
        setattr(parser, "parse", parse_wait)
    assert cast(Any, parser.parse).__func__ is JsonActionParser.parse


_SEALED_DESCRIPTOR_CASES = (
    pytest.param(OpenAICompatibleProviderCodec, "codec_id", id="base-codec-id"),
    pytest.param(OpenAICompatibleProviderCodec, "contract_version", id="base-contract-version"),
    pytest.param(OpenAICompatibleProviderCodec, "encode", id="base-encode"),
    pytest.param(OpenAICompatibleProviderCodec, "normalize", id="base-normalize"),
    pytest.param(
        OpenAICompatibleProviderCodec,
        "consume_parser_diagnostics",
        id="base-consume-parser-diagnostics",
    ),
    pytest.param(DeterministicFakeProviderCodec, "simulated", id="fake-simulated"),
    pytest.param(DeterministicFakeProviderCodec, "configuration", id="fake-configuration"),
    pytest.param(DeterministicFakeProviderCodec, "begin_run", id="fake-begin-run"),
    pytest.param(DeterministicFakeProviderCodec, "_next_scenario", id="fake-next-scenario"),
    pytest.param(DeterministicFakeProviderCodec, "send", id="fake-send"),
    pytest.param(DeterministicFakeProviderCodec, "normalize", id="fake-normalize"),
    pytest.param(JsonActionParser, "parse", id="json-parser-parse"),
    pytest.param(JsonActionParser, "binding_id", id="json-parser-binding-id"),
    pytest.param(
        JsonActionParser,
        "implementation_sha256",
        id="json-parser-implementation-sha256",
    ),
)


def _replacement_descriptor(owner: type[Any], attribute: str) -> object:
    descriptor = owner.__dict__[attribute]
    if isinstance(descriptor, property):
        return property(
            descriptor.fget,
            descriptor.fset,
            descriptor.fdel,
            descriptor.__doc__,
        )
    if isinstance(descriptor, str):
        if attribute == "implementation_sha256":
            return "f" * 64 if descriptor != "f" * 64 else "e" * 64
        return f"{descriptor}.mutated"

    def forbidden_replacement(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"mutated descriptor reached: {attribute} {args!r} {kwargs!r}")

    return forbidden_replacement


@pytest.mark.parametrize(("owner", "attribute"), _SEALED_DESCRIPTOR_CASES)
def test_preflight_rejects_every_mutated_fake_descriptor_before_store_or_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: type[Any],
    attribute: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    monkeypatch.setattr(owner, attribute, _replacement_descriptor(owner, attribute))

    with pytest.raises(ReplayRunnerError, match="FAKE_PROVIDER_IMPLEMENTATION_MISMATCH"):
        _preflight(
            fixture,
            provider_registry,
            provider,
            preflight_store=store,
        )
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == provider.send_calls == provider.normalize_calls == 0
    assert getattr(provider, "_active_run_id") is None
    assert getattr(provider, "_scenario_history_by_run") == {}


@pytest.mark.parametrize(("owner", "attribute"), _SEALED_DESCRIPTOR_CASES)
def test_execute_rejects_every_post_preflight_fake_descriptor_mutation_before_store_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: type[Any],
    attribute: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    encode_count = provider.encode_calls
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    monkeypatch.setattr(owner, attribute, _replacement_descriptor(owner, attribute))

    with pytest.raises(ReplayRunnerError, match="FAKE_PROVIDER_IMPLEMENTATION_MISMATCH"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert tuple(store.root.rglob("*")) == ()
    assert provider.encode_calls == encode_count
    assert provider.send_calls == provider.normalize_calls == 0
    assert provider.scenario_history == []
    assert getattr(provider, "_active_run_id") is None
    assert getattr(provider, "_scenario_history_by_run") == {}


def test_terminal_rerun_is_idempotent_and_never_sends_twice(tmp_path: Path) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    first = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    event_count = len(store.load_events(original.invocation_plan.run_id))
    send_count = provider.send_calls
    second = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert first["idempotent_reuse"] is False
    assert second["idempotent_reuse"] is True
    assert provider.send_calls == send_count == 1
    assert len(store.load_events(original.invocation_plan.run_id)) == event_count


@pytest.mark.parametrize(
    ("event_index", "reference_key"),
    (
        pytest.param(0, "selected_plan_ref", id="selected-plan"),
        pytest.param(1, "encoded_request_ref", id="encoded-request"),
    ),
)
def test_terminal_reuse_rejects_missing_required_artifact_without_resend(
    tmp_path: Path,
    event_index: int,
    reference_key: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    events = store.load_events(original.invocation_plan.run_id)
    payload = cast(dict[str, JsonValue], events[event_index]["payload"])
    reference = cast(dict[str, JsonValue], payload[reference_key])
    artifact_path = store.root / cast(str, reference["relative_path"])
    artifact_path.unlink()
    send_count = provider.send_calls
    scenario_history = tuple(provider.scenario_history)

    with pytest.raises(ReplayRunnerError, match="ARTIFACT_REF_UNRESOLVED"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert provider.send_calls == send_count == 1
    assert tuple(provider.scenario_history) == scenario_history


def test_terminal_reuse_rejects_cross_spliced_json_artifact_without_resend(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    planned_payload = cast(
        dict[str, JsonValue],
        store.load_events(original.invocation_plan.run_id)[0]["payload"],
    )
    selected_ref = cast(dict[str, JsonValue], planned_payload["selected_plan_ref"])
    paired_ref = cast(dict[str, JsonValue], planned_payload["paired_plan_set_ref"])
    assert selected_ref["sha256"] != paired_ref["sha256"]
    selected_path = store.root / cast(str, selected_ref["relative_path"])
    paired_path = store.root / cast(str, paired_ref["relative_path"])
    spliced_bytes = paired_path.read_bytes()
    assert isinstance(json.loads(spliced_bytes), dict)
    selected_path.unlink()
    selected_path.write_bytes(spliced_bytes)
    send_count = provider.send_calls

    with pytest.raises(ReplayRunnerError, match="ARTIFACT_REF_INVALID"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert provider.send_calls == send_count == 1


def test_terminal_reuse_reparses_raw_response_and_rejects_coherently_forged_action_chain(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    run_id = original.invocation_plan.run_id
    events = store.load_events(run_id)
    parsed_index = next(
        index for index, event in enumerate(events) if event["event_kind"] == "PARSED"
    )
    terminal_index = parsed_index + 1
    assert events[terminal_index]["event_kind"] == "TERMINAL"

    forged_action: dict[str, JsonValue] = {
        "type": "click",
        "coordinate": [303, 404],
    }
    forged_action_sha256 = canonical_sha256(forged_action)
    forged_diagnostics = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal["parser_diagnostics"])),
    )
    forged_diagnostics["action_sha256"] = forged_action_sha256
    forged_result = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal["provider_result"])),
    )
    forged_result["normalized_action"] = forged_action
    forged_result["normalized_action_sha256"] = forged_action_sha256

    forged_parsed = deepcopy(events[parsed_index])
    forged_parsed["payload"] = {
        "provider_result_sha256": canonical_sha256(forged_result),
        "parser_diagnostics": forged_diagnostics,
    }

    def rebind_event(record: dict[str, Any], previous_sha256: str) -> str:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        return canonical_sha256(cast(JsonValue, record))

    previous_sha256 = canonical_sha256(cast(JsonValue, events[parsed_index - 1]))
    forged_parsed_sha256 = rebind_event(forged_parsed, previous_sha256)
    forged_terminal = deepcopy(events[terminal_index])
    forged_terminal["payload"]["preceding_event_sha256"] = forged_parsed_sha256
    forged_terminal_sha256 = rebind_event(forged_terminal, forged_parsed_sha256)

    event_dir = store.root / "runs" / run_id / "events"
    for stale in events[parsed_index : terminal_index + 1]:
        stale_path = event_dir / (
            f"{stale['seq']:04d}-{canonical_sha256(cast(JsonValue, stale))}.json"
        )
        stale_path.unlink()
    for forged, digest in (
        (forged_parsed, forged_parsed_sha256),
        (forged_terminal, forged_terminal_sha256),
    ):
        path = event_dir / f"{forged['seq']:04d}-{digest}.json"
        path.write_bytes(canonical_json_bytes(cast(JsonValue, forged)))

    forged_terminal_record = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_terminal_record["provider_result"] = forged_result
    forged_terminal_record["parser_diagnostics"] = forged_diagnostics
    forged_terminal_record["final_event_sha256"] = forged_terminal_sha256
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal_record))

    assert store.load_events(run_id)[parsed_index]["payload"] == forged_parsed["payload"]
    structurally_valid_terminal = store._read_structural_terminal(run_id)
    assert structurally_valid_terminal is not None
    assert structurally_valid_terminal["provider_result"]["normalized_action"] == forged_action
    send_count = provider.send_calls

    with pytest.raises(ReplayRunnerError, match="PREPARED_ARM_BINDING_MISMATCH"):
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert provider.send_calls == send_count == 1


def test_terminal_idempotent_reuse_requires_the_current_invocation_plan(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    conflicting = replace(
        original,
        invocation_plan=replace(original.invocation_plan, config_sha256="f" * 64),
    )

    with pytest.raises(ReplayRunnerError, match="INVOCATION_PLAN_BINDING_MISMATCH"):
        execute_fake_arm(conflicting, provider_registry=provider_registry, store=store)
    assert provider.send_calls == 1


def test_record_preflight_blocked_is_two_event_idempotent_and_terminal(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    reason = "CAPSULE_AUTHORIZATION_GUARD_INVALID"

    first = record_preflight_blocked(
        original.invocation_plan,
        store=store,
        reason_code=reason,
    )
    first_events = store.load_events(original.invocation_plan.run_id)
    assert [event["event_kind"] for event in first_events] == [
        "PLANNED",
        "PREFLIGHT_BLOCKED",
    ]
    assert all(event["provider_attempt_index"] is None for event in first_events)
    assert first_events[-1]["payload"] == {
        "reason_code": reason,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "external_provider_invoked": False,
    }
    assert first == first_events[-1]
    assert provider.send_calls == provider.normalize_calls == 0

    second = record_preflight_blocked(
        original.invocation_plan,
        store=store,
        reason_code=reason,
    )
    assert second == first
    assert store.load_events(original.invocation_plan.run_id) == first_events
    assert provider.send_calls == 0

    with pytest.raises(ReplayRunnerError, match="PREFLIGHT_BLOCK_RECORD_COLLISION"):
        record_preflight_blocked(
            original.invocation_plan,
            store=store,
            reason_code="ANOTHER_BLOCK_REASON",
        )
    with pytest.raises(ReplayRunnerError, match="ATTEMPT_LEDGER_INVALID"):
        store.append_event(
            run_id=original.invocation_plan.run_id,
            event_kind=AttemptEventKind.ATTEMPT_STARTED,
            provider_attempt_index=1,
            payload={"simulated": True},
        )
    assert store.load_events(original.invocation_plan.run_id) == first_events


@pytest.mark.parametrize(
    ("event_index", "operation", "key", "value", "expected_code"),
    (
        pytest.param(
            0,
            "set",
            "preflight_outcome",
            True,
            "ATTEMPT_LEDGER_INVALID",
            id="planned-true",
        ),
        pytest.param(
            0,
            "delete",
            "preflight_outcome",
            None,
            "ATTEMPT_LEDGER_INVALID",
            id="planned-missing",
        ),
        pytest.param(
            0,
            "set",
            "unexpected",
            False,
            "ATTEMPT_LEDGER_INVALID",
            id="planned-extra",
        ),
        pytest.param(
            0,
            "set",
            "invocation_plan_sha256",
            "f" * 64,
            "PREFLIGHT_BLOCK_RECORD_COLLISION",
            id="planned-wrong-plan",
        ),
        pytest.param(
            1,
            "set",
            "provider_invocation_allowed",
            True,
            "ATTEMPT_LEDGER_INVALID",
            id="blocked-true",
        ),
        pytest.param(
            1,
            "delete",
            "provider_invocation_allowed",
            None,
            "ATTEMPT_LEDGER_INVALID",
            id="blocked-missing",
        ),
        pytest.param(
            1,
            "set",
            "unexpected",
            False,
            "ATTEMPT_LEDGER_INVALID",
            id="blocked-extra",
        ),
    ),
)
def test_record_preflight_blocked_rejects_tampered_two_event_closure_without_new_write(
    tmp_path: Path,
    event_index: int,
    operation: str,
    key: str,
    value: JsonValue,
    expected_code: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    run_id = original.invocation_plan.run_id
    reason = "CAPSULE_AUTHORIZATION_GUARD_INVALID"
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    record_preflight_blocked(original.invocation_plan, store=store, reason_code=reason)
    original_events = store.load_events(run_id)
    forged_events = [deepcopy(event) for event in original_events]
    payload = forged_events[event_index]["payload"]
    if operation == "delete":
        del payload[key]
    else:
        payload[key] = value

    previous_sha256: str | None = None
    forged_records: list[tuple[dict[str, Any], str]] = []
    for record in forged_events:
        record["previous_event_sha256"] = previous_sha256
        subject: dict[str, JsonValue] = {
            "run_id": cast(str, record["run_id"]),
            "seq": cast(int, record["seq"]),
            "previous_event_sha256": previous_sha256,
            "event_kind": cast(str, record["event_kind"]),
            "provider_attempt_index": cast(int | None, record["provider_attempt_index"]),
            "payload": cast(dict[str, JsonValue], record["payload"]),
        }
        record["event_id"] = f"g1attempt-event-{canonical_sha256(subject)[:24]}"
        previous_sha256 = canonical_sha256(cast(JsonValue, record))
        forged_records.append((record, previous_sha256))

    event_dir = store.root / "runs" / run_id / "events"
    for path in event_dir.iterdir():
        path.unlink()
    for record, digest in forged_records:
        path = event_dir / f"{record['seq']:04d}-{digest}.json"
        path.write_bytes(canonical_json_bytes(cast(JsonValue, record)))
    files_before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ReplayRunnerError) as raised:
        record_preflight_blocked(
            original.invocation_plan,
            store=store,
            reason_code=reason,
        )
    assert raised.value.code == expected_code
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_before
    assert provider.send_calls == provider.normalize_calls == 0


def test_record_preflight_blocked_recovers_a_matching_planned_only_prefix(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    plan = original.invocation_plan
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    store.bind_plan(plan)
    planned = store.append_event(
        run_id=plan.run_id,
        event_kind=AttemptEventKind.PLANNED,
        provider_attempt_index=None,
        payload={
            "invocation_plan_sha256": canonical_sha256(plan.to_dict()),
            "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
        },
    )

    blocked = record_preflight_blocked(
        plan,
        store=store,
        reason_code="INVARIANCE_VALIDATION_FAILED",
    )
    events = store.load_events(plan.run_id)
    assert [event["event_kind"] for event in events] == ["PLANNED", "PREFLIGHT_BLOCKED"]
    assert events[0] == planned.to_dict()
    assert events[1] == blocked
    assert events[1]["payload"]["reason_code"] == "INVARIANCE_VALIDATION_FAILED"
    assert provider.send_calls == provider.normalize_calls == 0


def test_record_preflight_blocked_rejects_noncanonical_run_identity_before_store_write(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    noncanonical_run_id = f"g1run-{'f' * 24}"
    assert original.invocation_plan.run_id != noncanonical_run_id
    noncanonical_plan = replace(
        original.invocation_plan,
        run_id=noncanonical_run_id,
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    assert tuple(store.root.rglob("*")) == ()

    with pytest.raises(ReplayRunnerError) as raised:
        record_preflight_blocked(
            noncanonical_plan,
            store=store,
            reason_code="INVARIANCE_VALIDATION_FAILED",
        )
    assert raised.value.code == "INVOCATION_PLAN_BINDING_MISMATCH"
    assert tuple(store.root.rglob("*")) == ()
    assert not (store.root / "runs").exists()
    assert provider.send_calls == provider.normalize_calls == 0


@pytest.mark.parametrize(
    ("ledger_kind", "expected_code"),
    (
        pytest.param("forged", "PREFLIGHT_BLOCK_RECORD_COLLISION", id="forged-ledger"),
        pytest.param("corrupt", "ATTEMPT_LEDGER_INVALID", id="corrupt-ledger"),
    ),
)
def test_record_preflight_blocked_does_not_bind_or_repair_planless_existing_ledger(
    tmp_path: Path,
    ledger_kind: str,
    expected_code: str,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    plan = original.invocation_plan
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    if ledger_kind == "forged":
        store.append_event(
            run_id=plan.run_id,
            event_kind=AttemptEventKind.PLANNED,
            provider_attempt_index=None,
            payload={
                "invocation_plan_sha256": "f" * 64,
                "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
            },
        )
        store.append_event(
            run_id=plan.run_id,
            event_kind=AttemptEventKind.PREFLIGHT_BLOCKED,
            provider_attempt_index=None,
            payload={
                "reason_code": "FORGED_REASON",
                "provider_invocation_allowed": False,
                "external_provider_invoked": False,
                "treatment_response_generation_allowed": False,
            },
        )
    else:
        event_dir = store.root / "runs" / plan.run_id / "events"
        event_dir.mkdir(parents=True)
        (event_dir / f"0000-{'a' * 64}.json").write_bytes(b"not-json")
    plan_path = store.root / "runs" / plan.run_id / "invocation-plan.json"
    assert not plan_path.exists()
    files_before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ReplayRunnerError) as raised:
        record_preflight_blocked(
            plan,
            store=store,
            reason_code="CAPSULE_AUTHORIZATION_GUARD_INVALID",
        )
    assert raised.value.code == expected_code
    assert not plan_path.exists()
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_before
    assert provider.send_calls == provider.normalize_calls == 0


def test_store_exposes_no_public_structural_terminal_shortcuts(tmp_path: Path) -> None:
    store = ReplayArtifactStore(
        tmp_path / "no-public-terminal-shortcuts",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    for attribute in (
        "terminal_record",
        "commit_terminal",
        "assert_resumable_without_send",
    ):
        assert not hasattr(store, attribute)


def test_forbidden_store_constructor_does_not_leave_a_directory(tmp_path: Path) -> None:
    synthetic_repo = tmp_path / "synthetic-repo"
    synthetic_repo.mkdir()
    forbidden_root = synthetic_repo / "derived-must-not-exist"

    with pytest.raises(ReplayRunnerError, match="OUTPUT_INSIDE_REPOSITORY"):
        ReplayArtifactStore(
            forbidden_root,
            repo_root=synthetic_repo,
            immutable_roots=(),
        )
    assert not forbidden_root.exists()


def test_dangling_output_root_symlink_is_rejected_without_creating_target(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "derived-dangling-root"
    dangling_target = tmp_path / "must-not-be-created"
    output_root.symlink_to(dangling_target, target_is_directory=True)
    tree_before = _output_tree_snapshot(tmp_path)

    with pytest.raises(ReplayRunnerError) as raised:
        ReplayArtifactStore(
            output_root,
            repo_root=REPO_ROOT,
            immutable_roots=(MOBILEWORLD_ROOT,),
        )
    assert raised.value.code == "UNSAFE_OUTPUT_ROOT"
    assert _output_tree_snapshot(tmp_path) == tree_before
    assert output_root.is_symlink()
    assert output_root.readlink() == dangling_target
    assert not dangling_target.exists()


def test_dangling_event_ledger_rejects_load_and_execute_without_side_effects(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived-dangling-events",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = original.invocation_plan.run_id
    event_dir = store.root / "runs" / run_id / "events"
    event_dir.parent.mkdir(parents=True)
    dangling_target = tmp_path / "missing-events-target"
    event_dir.symlink_to(dangling_target, target_is_directory=True)
    tree_before = _output_tree_snapshot(store.root)

    with pytest.raises(ReplayRunnerError) as load_raised:
        store.load_events(run_id)
    assert load_raised.value.code == "ATTEMPT_LEDGER_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before

    with pytest.raises(ReplayRunnerError) as execute_raised:
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert execute_raised.value.code == "ATTEMPT_LEDGER_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert event_dir.is_symlink()
    assert event_dir.readlink() == dangling_target
    assert not dangling_target.exists()
    assert provider.send_calls == provider.normalize_calls == 0
    assert provider.scenario_history == []


def test_dangling_terminal_symlink_rejects_execute_before_any_side_effect(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived-dangling-terminal",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = original.invocation_plan.run_id
    terminal_path = store.root / "runs" / run_id / "terminal.json"
    terminal_path.parent.mkdir(parents=True)
    dangling_target = tmp_path / "missing-terminal-target.json"
    terminal_path.symlink_to(dangling_target)
    tree_before = _output_tree_snapshot(store.root)

    with pytest.raises(ReplayRunnerError) as raised:
        execute_fake_arm(original, provider_registry=provider_registry, store=store)
    assert raised.value.code == "TERMINAL_RECORD_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert terminal_path.is_symlink()
    assert terminal_path.readlink() == dangling_target
    assert not dangling_target.exists()
    assert provider.send_calls == provider.normalize_calls == 0
    assert provider.scenario_history == []


@pytest.mark.parametrize("target_kind", ("outside", "immutable"))
def test_nested_output_symlink_is_rejected_without_creating_target_content(
    tmp_path: Path,
    target_kind: str,
) -> None:
    target = tmp_path / target_kind
    target.mkdir()
    store = ReplayArtifactStore(
        tmp_path / f"derived-{target_kind}",
        repo_root=REPO_ROOT,
        immutable_roots=((target,) if target_kind == "immutable" else ()),
    )
    (store.root / "nested").symlink_to(target, target_is_directory=True)

    with pytest.raises(ReplayRunnerError, match="UNSAFE_OUTPUT_PATH"):
        store.write_once("nested/must-not-exist/artifact.json", b"{}")
    assert not (target / "must-not-exist").exists()
    assert tuple(target.iterdir()) == ()


def test_append_only_store_collision_protected_roots_and_ambiguous_delivery(
    tmp_path: Path,
) -> None:
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    assert store.write_once("objects/example", b"one") is True
    assert store.write_once("objects/example", b"one") is False
    with pytest.raises(ReplayRunnerError, match="IDEMPOTENCE_COLLISION"):
        store.write_once("objects/example", b"two")
    with pytest.raises(ReplayRunnerError, match="OUTPUT_INSIDE_REPOSITORY"):
        ReplayArtifactStore(
            MOBILEWORLD_ROOT,
            repo_root=REPO_ROOT,
            immutable_roots=(MOBILEWORLD_ROOT,),
        )
    immutable = tmp_path / "immutable-capsule-source"
    immutable.mkdir()
    with pytest.raises(ReplayRunnerError, match="OUTPUT_OVERLAPS_IMMUTABLE_SOURCE"):
        ReplayArtifactStore(
            immutable,
            repo_root=REPO_ROOT,
            immutable_roots=(immutable,),
        )

    run_id = "g1run-aaaaaaaaaaaaaaaaaaaaaaaa"
    _append_allowed_preflight_prefix(store, run_id=run_id)
    store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.ATTEMPT_STARTED,
        provider_attempt_index=1,
        payload={
            "encoded_request_sha256": "a" * 64,
            "simulated": True,
            "external_provider_invoked": False,
        },
    )
    with pytest.raises(ReplayRunnerError, match="AMBIGUOUS_PROVIDER_DELIVERY"):
        store._assert_no_ambiguous_delivery(run_id)


def test_ambiguous_second_attempt_after_prior_failure_is_still_blocked(tmp_path: Path) -> None:
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = "g1run-bbbbbbbbbbbbbbbbbbbbbbbb"
    _, allowed = _append_allowed_preflight_prefix(store, run_id=run_id)
    exchange_ref = cast(
        dict[str, JsonValue],
        allowed["payload"]["encoded_request_ref"],
    )
    store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.ATTEMPT_STARTED,
        provider_attempt_index=1,
        payload={
            "encoded_request_sha256": "a" * 64,
            "simulated": True,
            "external_provider_invoked": False,
        },
    )
    store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.FAILED,
        provider_attempt_index=1,
        payload={
            "error_code": "TIMEOUT",
            "retryable": True,
            "exchange_ref": exchange_ref,
        },
    )
    store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.ATTEMPT_STARTED,
        provider_attempt_index=2,
        payload={
            "encoded_request_sha256": "a" * 64,
            "simulated": True,
            "external_provider_invoked": False,
        },
    )
    with pytest.raises(ReplayRunnerError, match="AMBIGUOUS_PROVIDER_DELIVERY"):
        store._assert_no_ambiguous_delivery(run_id)


def test_append_event_rejects_invalid_state_before_writing(tmp_path: Path) -> None:
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = "g1run-eeeeeeeeeeeeeeeeeeeeeeee"
    event_dir = store.root / "runs" / run_id / "events"

    with pytest.raises(ReplayRunnerError, match="ATTEMPT_LEDGER_INVALID"):
        store.append_event(
            run_id=run_id,
            event_kind=AttemptEventKind.ATTEMPT_STARTED,
            provider_attempt_index=1,
            payload={"simulated": True},
        )
    assert not event_dir.exists()
    assert store.load_events(run_id) == ()


@pytest.mark.parametrize(
    ("event_kind", "invalid_payload"),
    (
        pytest.param(
            AttemptEventKind.PLANNED,
            {"junk": True},
            id="planned-extra-junk",
        ),
        pytest.param(
            AttemptEventKind.PREFLIGHT_BLOCKED,
            {
                "reason_code": "FIXTURE_BLOCKED",
                "provider_invocation_allowed": True,
                "external_provider_invoked": False,
                "treatment_response_generation_allowed": False,
            },
            id="blocked-true",
        ),
        pytest.param(
            AttemptEventKind.PREFLIGHT_BLOCKED,
            {
                "reason_code": "FIXTURE_BLOCKED",
                "external_provider_invoked": False,
                "treatment_response_generation_allowed": False,
            },
            id="blocked-missing",
        ),
        pytest.param(
            AttemptEventKind.PREFLIGHT_BLOCKED,
            {
                "reason_code": "FIXTURE_BLOCKED",
                "provider_invocation_allowed": False,
                "external_provider_invoked": False,
                "treatment_response_generation_allowed": False,
                "unexpected": False,
            },
            id="blocked-extra",
        ),
    ),
)
def test_append_event_rejects_nonclosed_preflight_payload_before_write_and_schema(
    tmp_path: Path,
    event_kind: AttemptEventKind,
    invalid_payload: dict[str, JsonValue],
) -> None:
    run_id = "g1run-eeeeeeeeeeeeeeeeeeeeeeee"
    planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": "d" * 64,
        "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
    }
    blocked_payload: dict[str, JsonValue] = {
        "reason_code": "FIXTURE_BLOCKED",
        "provider_invocation_allowed": False,
        "external_provider_invoked": False,
        "treatment_response_generation_allowed": False,
    }
    reference_store = ReplayArtifactStore(
        tmp_path / "reference",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    valid_planned = reference_store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.PLANNED,
        provider_attempt_index=None,
        payload=planned_payload,
    )
    valid_blocked = reference_store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.PREFLIGHT_BLOCKED,
        provider_attempt_index=None,
        payload=blocked_payload,
    )
    valid_record = (
        valid_planned.to_dict()
        if event_kind is AttemptEventKind.PLANNED
        else valid_blocked.to_dict()
    )
    invalid_record = deepcopy(valid_record)
    invalid_record["payload"] = copy_json(invalid_payload)
    validator = _validator("attempt_event.schema.json")
    validator.validate(valid_record)
    assert not validator.is_valid(invalid_record)

    store = ReplayArtifactStore(
        tmp_path / "subject",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    if event_kind is AttemptEventKind.PREFLIGHT_BLOCKED:
        store.append_event(
            run_id=run_id,
            event_kind=AttemptEventKind.PLANNED,
            provider_attempt_index=None,
            payload=planned_payload,
        )
    files_before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ReplayRunnerError) as raised:
        store.append_event(
            run_id=run_id,
            event_kind=event_kind,
            provider_attempt_index=None,
            payload=invalid_payload,
        )
    assert raised.value.code == "ATTEMPT_LEDGER_INVALID"
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_before


@pytest.mark.parametrize(
    "planned_is_blocked",
    (
        pytest.param(True, id="blocked-planned-to-allowed"),
        pytest.param(False, id="allowed-planned-to-blocked"),
    ),
)
def test_append_event_rejects_cross_preflight_branch_before_second_write(
    tmp_path: Path,
    planned_is_blocked: bool,
) -> None:
    store = ReplayArtifactStore(
        tmp_path / "derived-cross-preflight",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = "g1run-ffffffffffffffffffffffff"
    artifact_ref = store.put_json({"fixture": "cross-preflight-branch"})
    allowed_planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": "a" * 64,
        "selected_plan_ref": artifact_ref,
        "paired_plan_set_ref": artifact_ref,
        "invariance_report_ref": artifact_ref,
        "render_result_ref": artifact_ref,
        "validation_receipt_ref": artifact_ref,
        "final_application_request_ref": artifact_ref,
        "target_diff_ref": artifact_ref,
        "blinding_commitment": {
            "blinding_mapping_sha256": "b" * 64,
            "key_commitment_sha256": "c" * 64,
            "mapping_persisted_before_response": True,
        },
    }
    blocked_planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": "a" * 64,
        "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
    }
    allowed_payload: dict[str, JsonValue] = {
        "fake_conformance": True,
        "external_provider_invocation_allowed": False,
        "encoded_request_ref": artifact_ref,
    }
    blocked_payload: dict[str, JsonValue] = {
        "reason_code": "FIXTURE_BLOCKED",
        "provider_invocation_allowed": False,
        "external_provider_invoked": False,
        "treatment_response_generation_allowed": False,
    }
    store.append_event(
        run_id=run_id,
        event_kind=AttemptEventKind.PLANNED,
        provider_attempt_index=None,
        payload=blocked_planned_payload if planned_is_blocked else allowed_planned_payload,
    )
    tree_before = _output_tree_snapshot(store.root)

    with pytest.raises(ReplayRunnerError) as raised:
        store.append_event(
            run_id=run_id,
            event_kind=(
                AttemptEventKind.PREFLIGHT_ALLOWED
                if planned_is_blocked
                else AttemptEventKind.PREFLIGHT_BLOCKED
            ),
            provider_attempt_index=None,
            payload=allowed_payload if planned_is_blocked else blocked_payload,
        )
    assert raised.value.code == "ATTEMPT_LEDGER_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before
    assert len(store.load_events(run_id)) == 1


@pytest.mark.parametrize(
    "planned_is_blocked",
    (
        pytest.param(True, id="self-hashed-blocked-planned-to-allowed"),
        pytest.param(False, id="self-hashed-allowed-planned-to-blocked"),
    ),
)
def test_load_events_rejects_canonical_self_hashed_cross_preflight_branch(
    tmp_path: Path,
    planned_is_blocked: bool,
) -> None:
    store = ReplayArtifactStore(
        tmp_path / "derived-forged-cross-preflight",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    run_id = "g1run-ffffffffffffffffffffffff"
    artifact_ref = store.put_json({"fixture": "self-hashed-cross-preflight"})
    allowed_planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": "a" * 64,
        "selected_plan_ref": artifact_ref,
        "paired_plan_set_ref": artifact_ref,
        "invariance_report_ref": artifact_ref,
        "render_result_ref": artifact_ref,
        "validation_receipt_ref": artifact_ref,
        "final_application_request_ref": artifact_ref,
        "target_diff_ref": artifact_ref,
        "blinding_commitment": {
            "blinding_mapping_sha256": "b" * 64,
            "key_commitment_sha256": "c" * 64,
            "mapping_persisted_before_response": True,
        },
    }
    blocked_planned_payload: dict[str, JsonValue] = {
        "invocation_plan_sha256": "a" * 64,
        "preflight_outcome": "BLOCKED_BEFORE_FAKE_PROVIDER",
    }
    allowed_payload: dict[str, JsonValue] = {
        "fake_conformance": True,
        "external_provider_invocation_allowed": False,
        "encoded_request_ref": artifact_ref,
    }
    blocked_payload: dict[str, JsonValue] = {
        "reason_code": "FIXTURE_BLOCKED",
        "provider_invocation_allowed": False,
        "external_provider_invoked": False,
        "treatment_response_generation_allowed": False,
    }
    planned = _canonical_attempt_event_record(
        run_id=run_id,
        seq=0,
        previous_event_sha256=None,
        event_kind=AttemptEventKind.PLANNED,
        payload=blocked_planned_payload if planned_is_blocked else allowed_planned_payload,
    )
    planned_sha256 = canonical_sha256(planned)
    decision = _canonical_attempt_event_record(
        run_id=run_id,
        seq=1,
        previous_event_sha256=planned_sha256,
        event_kind=(
            AttemptEventKind.PREFLIGHT_ALLOWED
            if planned_is_blocked
            else AttemptEventKind.PREFLIGHT_BLOCKED
        ),
        payload=allowed_payload if planned_is_blocked else blocked_payload,
    )
    validator = _validator("attempt_event.schema.json")
    validator.validate(planned)
    validator.validate(decision)
    event_dir = store.root / "runs" / run_id / "events"
    event_dir.mkdir(parents=True)
    for record in (planned, decision):
        digest = canonical_sha256(record)
        (event_dir / f"{record['seq']:04d}-{digest}.json").write_bytes(canonical_json_bytes(record))
    tree_before = _output_tree_snapshot(store.root)

    with pytest.raises(ReplayRunnerError) as raised:
        store.load_events(run_id)
    assert raised.value.code == "ATTEMPT_LEDGER_INVALID"
    assert _output_tree_snapshot(store.root) == tree_before


@pytest.mark.parametrize(
    "identity_override",
    (
        {"codec_id": "mobileworld.g1.provider.fake-conformance/not-v1"},
        {"endpoint_revision": "fake://different-endpoint/v1"},
    ),
)
def test_fake_codec_rejects_nonfixed_identity(identity_override: dict[str, str]) -> None:
    with pytest.raises(ReplayRunnerError):
        DeterministicFakeProviderCodec((FakeScenario.SUCCESS,), **identity_override)


def test_fake_codec_send_override_subclass_is_rejected_before_preflight_or_execute(
    tmp_path: Path,
) -> None:
    class SendOverrideFakeCodec(DeterministicFakeProviderCodec):
        def send(self, authorized: Any) -> Any:
            del authorized
            raise AssertionError("overridden fake send must never be reached")

    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    unsafe = SendOverrideFakeCodec((FakeScenario.SUCCESS,))
    unsafe_registry = ProviderCodecRegistry()
    unsafe_registry.register(unsafe)
    with pytest.raises(ReplayRunnerError, match="FAKE_PROVIDER_REQUIRED"):
        _preflight(fixture, unsafe_registry, unsafe)
    assert unsafe.encode_calls == unsafe.send_calls == unsafe.normalize_calls == 0

    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    with pytest.raises(ReplayRunnerError, match="FAKE_PROVIDER_REQUIRED"):
        execute_fake_arm(original, provider_registry=unsafe_registry, store=store)
    assert unsafe.send_calls == 0
    assert not (store.root / "runs" / original.invocation_plan.run_id).exists()


def test_runner_build_blinded_packet_persists_schema_valid_binding_and_hashes(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    packet = build_blinded_packet(original, store=store)
    run_id = original.invocation_plan.run_id
    packet_path = store.root / f"scorer/packets/{packet.blinded_packet_id}.json"
    binding_path = store.root / f"runs/{run_id}/confidential/blinded-packet-binding.json"
    mapping_path = store.root / f"runs/{run_id}/confidential/blinding-map.json"
    packet_bytes = packet_path.read_bytes()
    binding = json.loads(binding_path.read_bytes())
    mapping_bytes = mapping_path.read_bytes()

    assert json.loads(packet_bytes) == packet.to_dict()
    assert packet.normalized_action == {"type": "click", "coordinate": [101, 202]}
    assert packet_bytes == canonical_json_bytes(packet.to_dict())
    _validator("blinded_action.schema.json").validate(packet.to_dict())
    _validator("blinded_packet_binding.schema.json").validate(binding)
    assert binding["blinded_packet_id"] == packet.blinded_packet_id
    assert binding["run_id"] == run_id
    assert binding["terminal_final_event_sha256"] == terminal["final_event_sha256"]
    assert (
        binding["normalized_action_sha256"]
        == terminal["provider_result"]["normalized_action_sha256"]
    )
    assert binding["terminal_diagnostics_sha256"] == canonical_sha256(
        terminal["parser_diagnostics"]
    )
    assert binding["parser_diagnostics_sha256"] == canonical_sha256(packet.parser_diagnostics)
    assert binding["packet_sha256"] == hashlib.sha256(packet_bytes).hexdigest()
    assert binding["mapping_sha256"] == hashlib.sha256(mapping_bytes).hexdigest()
    assert binding["source_artifacts_valid"] is True
    assert binding["scorer_visible"] is False
    scorer_json = json.dumps(packet.to_dict(), sort_keys=True)
    assert run_id not in scorer_json
    assert original.schedule.schedule_id not in scorer_json
    assert original.schedule.arm.value not in scorer_json

    files_before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    repeated = build_blinded_packet(original, store=store)
    assert repeated == packet
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_before
    assert provider.send_calls == 1


@pytest.mark.parametrize(
    "scenario",
    (FakeScenario.MALFORMED_RESPONSE, FakeScenario.PARSER_FAILURE),
)
def test_blinded_packet_binding_separately_hashes_terminal_and_public_diagnostics(
    tmp_path: Path,
    scenario: FakeScenario,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((scenario,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    packet = build_blinded_packet(original, store=store)
    binding_path = (
        store.root
        / f"runs/{original.invocation_plan.run_id}/confidential/blinded-packet-binding.json"
    )
    binding = json.loads(binding_path.read_bytes())
    terminal_diagnostics = cast(dict[str, JsonValue], terminal["parser_diagnostics"])

    assert terminal_diagnostics["parse_outcome"] == "FAILED"
    assert packet.parser_outcome == "PARSE_ERROR"
    assert packet.parser_diagnostics["parse_outcome"] == "PARSE_ERROR"
    assert binding["terminal_diagnostics_sha256"] == canonical_sha256(terminal_diagnostics)
    assert binding["parser_diagnostics_sha256"] == canonical_sha256(packet.parser_diagnostics)
    assert binding["terminal_diagnostics_sha256"] != binding["parser_diagnostics_sha256"]
    _validator("blinded_packet_binding.schema.json").validate(binding)
    assert provider.send_calls == provider.normalize_calls == 1


@pytest.mark.parametrize("conflict_target", ("packet", "binding"))
def test_build_blinded_packet_conflict_is_atomic_across_public_and_binding_outputs(
    tmp_path: Path,
    conflict_target: str,
) -> None:
    reference_prepared, reference_store, _ = _completed_original_run(tmp_path / "reference")
    reference_packet = build_blinded_packet(reference_prepared, store=reference_store)
    reference_run_id = reference_prepared.invocation_plan.run_id
    packet_relative = f"scorer/packets/{reference_packet.blinded_packet_id}.json"
    binding_relative = f"runs/{reference_run_id}/confidential/blinded-packet-binding.json"
    expected_packet_bytes = reference_store.read_logical(packet_relative)
    expected_binding_bytes = reference_store.read_logical(binding_relative)

    prepared, store, provider = _completed_original_run(tmp_path / "subject")
    assert prepared.invocation_plan.run_id == reference_run_id
    conflicting_relative = packet_relative if conflict_target == "packet" else binding_relative
    counterpart_relative = binding_relative if conflict_target == "packet" else packet_relative
    conflicting_bytes = b'{"different":true}'
    assert store.write_once(conflicting_relative, conflicting_bytes) is True
    files_before = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert counterpart_relative not in files_before

    with pytest.raises(ReplayRunnerError) as raised:
        build_blinded_packet(prepared, store=store)
    assert raised.value.code == "IDEMPOTENCE_COLLISION"
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_before
    assert store.read_logical(conflicting_relative) == conflicting_bytes
    assert not (store.root / counterpart_relative).exists()
    assert expected_packet_bytes != conflicting_bytes
    assert expected_binding_bytes != conflicting_bytes
    assert provider.send_calls == 1


@pytest.mark.parametrize("preexisting_target", ("packet", "binding"))
def test_build_blinded_packet_accepts_identical_partial_output_idempotently(
    tmp_path: Path,
    preexisting_target: str,
) -> None:
    reference_prepared, reference_store, _ = _completed_original_run(tmp_path / "reference")
    reference_packet = build_blinded_packet(reference_prepared, store=reference_store)
    run_id = reference_prepared.invocation_plan.run_id
    packet_relative = f"scorer/packets/{reference_packet.blinded_packet_id}.json"
    binding_relative = f"runs/{run_id}/confidential/blinded-packet-binding.json"
    expected = {
        "packet": reference_store.read_logical(packet_relative),
        "binding": reference_store.read_logical(binding_relative),
    }

    prepared, store, provider = _completed_original_run(tmp_path / "subject")
    assert prepared.invocation_plan.run_id == run_id
    relative_by_target = {
        "packet": packet_relative,
        "binding": binding_relative,
    }
    assert (
        store.write_once(
            relative_by_target[preexisting_target],
            expected[preexisting_target],
        )
        is True
    )

    packet = build_blinded_packet(prepared, store=store)
    assert packet == reference_packet
    assert store.read_logical(packet_relative) == expected["packet"]
    assert store.read_logical(binding_relative) == expected["binding"]
    files_after_first_build = {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }
    assert build_blinded_packet(prepared, store=store) == reference_packet
    assert {
        path.relative_to(store.root).as_posix(): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    } == files_after_first_build
    assert provider.send_calls == 1


@pytest.mark.parametrize(
    ("target_kind", "expected_code"),
    (
        pytest.param("symlink", "OUTPUT_UNREADABLE", id="symlink"),
        pytest.param("directory", "OUTPUT_NOT_REGULAR", id="nonregular-directory"),
        pytest.param("unreadable", "OUTPUT_UNREADABLE", id="unreadable-file"),
    ),
)
def test_build_blinded_packet_rejects_unsafe_binding_target_before_public_write(
    tmp_path: Path,
    target_kind: str,
    expected_code: str,
) -> None:
    prepared, store, provider = _completed_original_run(tmp_path / "derived")
    run_id = prepared.invocation_plan.run_id
    seal = runner_module._fake_blinding_seal(prepared)
    binding_path = store.root / f"runs/{run_id}/confidential/blinded-packet-binding.json"
    packet_path = store.root / f"scorer/packets/{seal.blinded_packet_id}.json"
    outside = tmp_path / "outside-binding-target.json"
    outside_bytes = b'{"outside":"unchanged"}'
    outside.write_bytes(outside_bytes)
    restore_permissions = False
    if target_kind == "symlink":
        binding_path.symlink_to(outside)
    elif target_kind == "directory":
        binding_path.mkdir()
    else:
        binding_path.write_bytes(b'{"unreadable":"unchanged"}')
        binding_path.chmod(0)
        restore_permissions = True

    try:
        with pytest.raises(ReplayRunnerError) as raised:
            build_blinded_packet(prepared, store=store)
        assert raised.value.code == expected_code
        assert not packet_path.exists()
        assert outside.read_bytes() == outside_bytes
        assert provider.send_calls == 1
    finally:
        if restore_permissions:
            binding_path.chmod(0o600)

    if target_kind == "symlink":
        assert binding_path.is_symlink()
    elif target_kind == "directory":
        assert binding_path.is_dir()
        assert tuple(binding_path.iterdir()) == ()
    else:
        assert binding_path.read_bytes() == b'{"unreadable":"unchanged"}'


def test_blinded_packet_snapshots_return_nested_copies_and_order_revalidates_denysets(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    prepared = _preflight(fixture, provider_registry, provider)
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    sham = next(item for item in prepared if item.schedule.arm is ArmKind.SHAM_BENIGN_EDIT)
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    execute_fake_arm(sham, provider_registry=provider_registry, store=store)
    packets = (
        build_blinded_packet(original, store=store),
        build_blinded_packet(sham, store=store),
    )
    snapshot = packets[0].to_dict()
    order_before = tuple(
        item.blinded_packet_id
        for item in order_blinded_packets(packets, presentation_nonce="opaque-ordering")
    )

    action_copy = packets[0].normalized_action
    assert action_copy is not None
    cast(list[JsonValue], action_copy["coordinate"])[0] = 999
    diagnostics_copy = packets[0].parser_diagnostics
    diagnostics_copy["action_count"] = 999
    dictionary_copy = packets[0].to_dict()
    cast(dict[str, JsonValue], dictionary_copy["normalized_action"])["type"] = "wait"

    assert packets[0].to_dict() == snapshot
    assert (
        tuple(
            item.blinded_packet_id
            for item in order_blinded_packets(packets, presentation_nonce="opaque-ordering")
        )
        == order_before
    )
    assert provider.send_calls == 2


def test_direct_blinded_packet_constructor_cannot_drop_or_mutate_private_denyset() -> None:
    packet_id = f"g1blind-{'a' * 24}"
    diagnostics_bytes = canonical_json_bytes({"parse_outcome": "PARSED", "action_count": 1})
    with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_INVALID"):
        BlindedActionPacket(
            blinded_packet_id=packet_id,
            _normalized_action_json=canonical_json_bytes({"type": "click"}),
            parser_outcome="PARSED",
            _parser_diagnostics_json=diagnostics_bytes,
            _confidential_values=(),
        )
    with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_INVALID"):
        BlindedActionPacket(
            blinded_packet_id=packet_id,
            _normalized_action_json=b'{"type": "click"}',
            parser_outcome="PARSED",
            _parser_diagnostics_json=diagnostics_bytes,
            _confidential_values=("qwen3vl",),
        )

    forged = BlindedActionPacket(
        blinded_packet_id=packet_id,
        _normalized_action_json=canonical_json_bytes(
            {"type": "click", "metadata": {"value": "ZZqwen3vlYY"}}
        ),
        parser_outcome="PARSED",
        _parser_diagnostics_json=diagnostics_bytes,
        _confidential_values=("qwen3vl",),
    )
    with pytest.raises(FrozenInstanceError):
        forged._confidential_values = ()
    with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE"):
        order_blinded_packets((forged,), presentation_nonce="forged-packet-check")


def test_blinded_packet_order_revalidates_against_union_of_all_packet_denysets() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    foreign_plan_id = next(
        plan.plan_id for plan in fixture.plans if plan.arm is ArmKind.SHAM_BENIGN_EDIT
    )
    diagnostics_bytes = canonical_json_bytes({"parse_outcome": "PARSED", "action_count": 1})
    packet_a = BlindedActionPacket(
        blinded_packet_id=f"g1blind-{'a' * 24}",
        _normalized_action_json=canonical_json_bytes(
            {
                "type": "click",
                "metadata": {"foreign_packet_plan": foreign_plan_id},
            }
        ),
        parser_outcome="PARSED",
        _parser_diagnostics_json=diagnostics_bytes,
        _confidential_values=("packet-a-only-secret",),
    )
    packet_b = BlindedActionPacket(
        blinded_packet_id=f"g1blind-{'b' * 24}",
        _normalized_action_json=canonical_json_bytes({"type": "click", "coordinate": [17, 23]}),
        parser_outcome="PARSED",
        _parser_diagnostics_json=diagnostics_bytes,
        _confidential_values=(foreign_plan_id,),
    )
    validate_blinded_packet(
        packet_a.to_dict(),
        confidential_values=packet_a._confidential_values,
    )
    validate_blinded_packet(
        packet_b.to_dict(),
        confidential_values=packet_b._confidential_values,
    )

    with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
        order_blinded_packets((packet_a, packet_b), presentation_nonce="denyset-union")
    assert raised.value.json_path == "/normalized_action/metadata/foreign_packet_plan"


def test_build_blinded_packet_rejects_cross_arm_prepared_and_terminal_splices(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    prepared = _preflight(fixture, provider_registry, provider)
    original = next(item for item in prepared if item.schedule.arm is ArmKind.ORIGINAL)
    sham = next(item for item in prepared if item.schedule.arm is ArmKind.SHAM_BENIGN_EDIT)
    store = ReplayArtifactStore(
        tmp_path / "derived",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    execute_fake_arm(original, provider_registry=provider_registry, store=store)
    execute_fake_arm(sham, provider_registry=provider_registry, store=store)
    spliced_prepared = replace(original, invocation_plan=sham.invocation_plan)

    with pytest.raises(ReplayRunnerError, match="PREPARED_ARM_BINDING_MISMATCH"):
        build_blinded_packet(spliced_prepared, store=store)
    assert not (store.root / "scorer").exists()

    original_terminal_path = store.root / "runs" / original.invocation_plan.run_id / "terminal.json"
    sham_terminal_path = store.root / "runs" / sham.invocation_plan.run_id / "terminal.json"
    sham_terminal_path.unlink()
    sham_terminal_path.write_bytes(original_terminal_path.read_bytes())
    with pytest.raises(ReplayRunnerError, match="TERMINAL_RECORD_INVALID"):
        build_blinded_packet(sham, store=store)
    assert not (store.root / "scorer").exists()
    assert provider.send_calls == 2


def test_build_blinded_packet_requires_untampered_terminal_and_never_writes_scorer_early(
    tmp_path: Path,
) -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    missing_store = ReplayArtifactStore(
        tmp_path / "missing-terminal",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    with pytest.raises(ReplayRunnerError, match="BLINDED_EXPORT_TERMINAL_REQUIRED"):
        build_blinded_packet(original, store=missing_store)
    assert tuple(missing_store.root.rglob("*")) == ()

    store = ReplayArtifactStore(
        tmp_path / "tampered-terminal",
        repo_root=REPO_ROOT,
        immutable_roots=(MOBILEWORLD_ROOT,),
    )
    terminal = execute_fake_arm(original, provider_registry=provider_registry, store=store)
    forged_terminal = cast(
        dict[str, JsonValue],
        copy_json(cast(JsonValue, terminal)),
    )
    forged_terminal["final_event_sha256"] = "f" * 64
    terminal_path = store.root / "runs" / original.invocation_plan.run_id / "terminal.json"
    terminal_path.unlink()
    terminal_path.write_bytes(canonical_json_bytes(forged_terminal))

    with pytest.raises(ReplayRunnerError, match="TERMINAL_RECORD_INVALID"):
        build_blinded_packet(original, store=store)
    assert not (store.root / "scorer").exists()
    assert not (
        store.root
        / "runs"
        / original.invocation_plan.run_id
        / "confidential/blinded-packet-binding.json"
    ).exists()
    assert provider.send_calls == 1


@pytest.mark.parametrize(
    ("run_id", "schedule_id"),
    (
        (
            "not-a-run-id",
            "g1schedule-dddddddddddddddddddddddd",
        ),
        (
            "g1run-cccccccccccccccccccccccc",
            "not-a-schedule-id",
        ),
    ),
)
def test_prepare_blinding_rejects_invalid_run_and_schedule_identities(
    run_id: str,
    schedule_id: str,
) -> None:
    with pytest.raises(ReplayRunnerError, match="BLINDING_KEY_INVALID"):
        prepare_blinding(
            run_id=run_id,
            arm=ArmKind.MASK,
            schedule_id=schedule_id,
            secret_key=b"k" * 32,
            nonce="fixture-nonce",
            confidential_values=BLINDING_CONFIDENTIAL_VALUES,
        )


def test_runner_owned_blinding_seal_rejects_capsule_provider_and_parser_identities() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    model_binding = cast(dict[str, JsonValue], fixture.capsule.replay_binding["model"])
    provider_binding = cast(dict[str, JsonValue], fixture.capsule.replay_binding["provider"])
    parser_binding = cast(dict[str, JsonValue], fixture.capsule.replay_binding["parser"])
    sensitive_by_field = {
        "served_model_name": cast(str, model_binding["served_model_name"]),
        "repository": cast(str, model_binding["repository"]),
        "revision": cast(str, model_binding["revision"]),
        "model_config_manifest_sha256": cast(str, model_binding["model_config_manifest_sha256"]),
        "model_config_record_sha256": cast(str, model_binding["model_config_record_sha256"]),
        "sdk_method": cast(str, provider_binding["sdk_method"]),
        "endpoint_path": cast(str, provider_binding["endpoint_path"]),
        "parser_binding_id": cast(str, parser_binding["binding_id"]),
    }
    assert set(sensitive_by_field.values()).issubset(seal.confidential_values)
    assert (
        seal.mapping.to_dict()["forbidden_value_set_sha256"]
        == hashlib.sha256(canonical_json_bytes(list(seal.confidential_values))).hexdigest()
    )

    for field, sensitive_value in sensitive_by_field.items():
        with pytest.raises(
            ReplayRunnerError,
            match="BLINDED_PACKET_LEAKAGE",
        ):
            _make_blinded_packet(
                seal=seal,
                normalized_action={
                    "type": "click",
                    "metadata": {"field": field, "value": sensitive_value},
                },
                parser_outcome="PARSED",
                parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
            )
    assert provider.send_calls == provider.normalize_calls == 0


def test_runner_owned_blinding_seal_rejects_embedded_short_binding_tokens() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    short_binding_tokens = (
        "qwen3vl",
        "fake",
        "/v1",
        "v1",
        "fixture",
        "actor",
        "0",
    )
    assert set(short_binding_tokens).issubset(seal.confidential_values)

    for token in short_binding_tokens:
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
            _make_blinded_packet(
                seal=seal,
                normalized_action={
                    "type": "click",
                    "metadata": {"embedded": f"ZZ{token}YY"},
                },
                parser_outcome="PARSED",
                parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
            )
        assert raised.value.json_path == "/normalized_action/metadata/embedded"
    assert provider.send_calls == provider.normalize_calls == 0


def test_original_runner_blinding_seal_rejects_paired_plan_and_correction_text() -> None:
    fixture = _replay_fixture(UnitKind.STRICT_MHR)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    paired_plan = next(plan for plan in fixture.plans if plan.arm is ArmKind.SHAM_BENIGN_EDIT)
    correction_text = cast(str, _vectors()[1]["correction_text"])
    sensitive_values = (paired_plan.plan_id, correction_text)
    assert set(sensitive_values).issubset(seal.confidential_values)

    for sensitive_value in sensitive_values:
        for candidate in (sensitive_value, f"safe-prefix-{sensitive_value}-safe-suffix"):
            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {"candidate": candidate},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert raised.value.json_path == "/normalized_action/metadata/candidate"
    assert provider.send_calls == provider.normalize_calls == 0


def test_runner_owned_blinding_rejects_exact_and_embedded_identity_action_keys() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    provider_binding = cast(dict[str, JsonValue], fixture.capsule.replay_binding["provider"])
    identity_values = {
        "confidential": cast(str, provider_binding["endpoint_origin"]),
        "model": fixture.capsule.model_id,
        "arm": original.schedule.arm.value,
        "run": original.invocation_plan.run_id,
    }
    assert set(identity_values.values()).issubset(seal.confidential_values)

    for identity in identity_values.values():
        for candidate_key in (identity, f"safe-prefix-{identity}-safe-suffix"):
            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {candidate_key: "safe"},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert raised.value.json_path == f"/normalized_action/metadata/{candidate_key}"
    assert provider.send_calls == provider.normalize_calls == 0


def test_runner_owned_blinding_rejects_casefolded_identity_values_and_keys() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    casefolded_tokens = ("mask", "original", "Fixture-Flat-Progress", "QWEN3VL")

    for token in casefolded_tokens:
        for candidate in (token, f"safe-prefix-{token}-safe-suffix"):
            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as value_error:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {"candidate": candidate},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert value_error.value.json_path == "/normalized_action/metadata/candidate"

            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as key_error:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {candidate: "safe"},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert key_error.value.json_path == f"/normalized_action/metadata/{candidate}"
    assert provider.send_calls == provider.normalize_calls == 0


def test_blinded_action_rejects_nested_schedule_identity_keys_and_all_replay_seeds() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)

    identity_values: dict[str, JsonValue] = {
        "replay_seed": 1729,
        "arm": original.schedule.arm.value,
        "arm_order_index": original.schedule.arm_order_index,
        "block_index": original.schedule.block_index,
        "repeat_index": original.schedule.repeat_index,
        "schedule": original.schedule.schedule_id,
        "schedule_id": original.schedule.schedule_id,
        "run": original.invocation_plan.run_id,
        "run_id": original.invocation_plan.run_id,
        "plan": original.plan.plan_id,
        "plan_id": original.plan.plan_id,
    }
    for identity_key, identity_value in identity_values.items():
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
            _make_blinded_packet(
                seal=seal,
                normalized_action={
                    "type": "click",
                    "metadata": {"nested": {identity_key: identity_value}},
                },
                parser_outcome="PARSED",
                parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
            )
        assert raised.value.json_path == f"/normalized_action/metadata/nested/{identity_key}"

    assert REPLAY_SEEDS == (1729, 2718, 31415)
    for replay_seed in REPLAY_SEEDS:
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as raised:
            _make_blinded_packet(
                seal=seal,
                normalized_action={
                    "type": "click",
                    "metadata": {"nested": {"candidate_number": replay_seed}},
                },
                parser_outcome="PARSED",
                parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
            )
        assert raised.value.json_path == "/normalized_action/metadata/nested/candidate_number"
    assert provider.send_calls == provider.normalize_calls == 0


def test_blinded_action_rejects_seed_strings_keys_floats_and_exponent_json() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, provider = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, provider)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    seal = runner_module._fake_blinding_seal(original)
    exponent_literals = {
        1729: "1.729e3",
        2718: "2.718e3",
        31415: "3.1415e4",
    }

    for replay_seed in REPLAY_SEEDS:
        seed_text = str(replay_seed)
        for candidate in (seed_text, f"safe-prefix-{seed_text}-safe-suffix"):
            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as value_error:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {"candidate": candidate},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert value_error.value.json_path == "/normalized_action/metadata/candidate"

            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as key_error:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {candidate: "safe"},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert key_error.value.json_path == f"/normalized_action/metadata/{candidate}"

        exponent_value = json.loads(exponent_literals[replay_seed])
        assert type(exponent_value) is float
        for numeric_value in (float(replay_seed), cast(float, exponent_value)):
            with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE") as numeric_error:
                _make_blinded_packet(
                    seal=seal,
                    normalized_action={
                        "type": "click",
                        "metadata": {"candidate_number": numeric_value},
                    },
                    parser_outcome="PARSED",
                    parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
                )
            assert numeric_error.value.json_path == ("/normalized_action/metadata/candidate_number")
    assert provider.send_calls == provider.normalize_calls == 0


def test_blinding_is_precommitted_separate_and_recursively_leak_checked() -> None:
    seal = prepare_blinding(
        run_id="g1run-cccccccccccccccccccccccc",
        arm=ArmKind.MASK,
        schedule_id="g1schedule-dddddddddddddddddddddddd",
        secret_key=b"k" * 32,
        nonce="fixture-nonce",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    assert seal == prepare_blinding(
        run_id="g1run-cccccccccccccccccccccccc",
        arm=ArmKind.MASK,
        schedule_id="g1schedule-dddddddddddddddddddddddd",
        secret_key=b"k" * 32,
        nonce="fixture-nonce",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    with pytest.raises(TypeError):
        cast(dict[str, JsonValue], seal.confidential_mapping)["arm_id"] = "ORIGINAL"
    different_arm = prepare_blinding(
        run_id="g1run-cccccccccccccccccccccccc",
        arm=ArmKind.ORIGINAL,
        schedule_id="g1schedule-dddddddddddddddddddddddd",
        secret_key=b"k" * 32,
        nonce="fixture-nonce",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    different_schedule = prepare_blinding(
        run_id="g1run-cccccccccccccccccccccccc",
        arm=ArmKind.MASK,
        schedule_id="g1schedule-eeeeeeeeeeeeeeeeeeeeeeee",
        secret_key=b"k" * 32,
        nonce="fixture-nonce",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    assert (
        len(
            {
                seal.blinded_packet_id,
                different_arm.blinded_packet_id,
                different_schedule.blinded_packet_id,
            }
        )
        == 3
    )
    assert {
        seal.key_commitment_sha256,
        different_arm.key_commitment_sha256,
        different_schedule.key_commitment_sha256,
    } == {hashlib.sha256(b"k" * 32).hexdigest()}
    _validator("blinding_mapping.schema.json").validate(seal.mapping.to_dict())
    assert seal.confidential_mapping["arm_id"] == "MASK"
    with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE"):
        _make_blinded_packet(
            seal=seal,
            normalized_action={
                "metadata": {"nested": ["safe", seal.confidential_mapping["run_id"]]}
            },
            parser_outcome="PARSED",
            parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
        )


def test_blinding_rejects_exact_model_provider_and_endpoint_values_in_visible_fields() -> None:
    seal = prepare_blinding(
        run_id="g1run-ffffffffffffffffffffffff",
        arm=ArmKind.ORIGINAL,
        schedule_id="g1schedule-111111111111111111111111",
        secret_key=b"p" * 32,
        nonce="confidential-value-fixture",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    packet = _make_blinded_packet(
        seal=seal,
        normalized_action={"type": "click", "coordinate": [3, 4]},
        parser_outcome="PARSED",
        parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
    ).to_dict()
    validator = _validator("blinded_action.schema.json")

    for confidential_value in BLINDING_CONFIDENTIAL_VALUES:
        leaking_action = deepcopy(packet)
        leaking_action["normalized_action"] = {"metadata": {"nested": ["safe", confidential_value]}}
        assert validator.is_valid(leaking_action)
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_LEAKAGE"):
            validate_blinded_packet(
                cast(dict[str, JsonValue], leaking_action),
                confidential_values=seal.confidential_values,
            )

        leaking_outcome = deepcopy(packet)
        leaking_outcome["parser_outcome"] = confidential_value
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_INVALID"):
            validate_blinded_packet(
                cast(dict[str, JsonValue], leaking_outcome),
                confidential_values=seal.confidential_values,
            )

        leaking_diagnostics = deepcopy(packet)
        leaking_diagnostics["parser_diagnostics"]["error_code"] = confidential_value
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_INVALID"):
            validate_blinded_packet(
                cast(dict[str, JsonValue], leaking_diagnostics),
                confidential_values=seal.confidential_values,
            )


def test_blinded_packet_runtime_and_schema_validation_are_in_parity() -> None:
    seal = prepare_blinding(
        run_id="g1run-ffffffffffffffffffffffff",
        arm=ArmKind.ORIGINAL,
        schedule_id="g1schedule-111111111111111111111111",
        secret_key=b"p" * 32,
        nonce="parity-fixture",
        confidential_values=BLINDING_CONFIDENTIAL_VALUES,
    )
    packet = _make_blinded_packet(
        seal=seal,
        normalized_action={"type": "click", "coordinate": [3, 4]},
        parser_outcome="PARSED",
        parser_diagnostics={"parse_outcome": "PARSED", "action_count": 1},
    ).to_dict()
    validator = _validator("blinded_action.schema.json")
    validator.validate(packet)

    missing = deepcopy(packet)
    del missing["normalized_action"]
    wrong_version = deepcopy(packet)
    wrong_version["schema_version"] = "mobileworld.g1.blinded-action-packet/not-v1"
    empty_outcome = deepcopy(packet)
    empty_outcome["parser_outcome"] = ""
    boolean_count = deepcopy(packet)
    boolean_count["parser_diagnostics"]["action_count"] = True
    null_diagnostic_outcome = deepcopy(packet)
    null_diagnostic_outcome["parser_diagnostics"]["parse_outcome"] = None
    parsed_with_no_op_diagnostic = deepcopy(packet)
    parsed_with_no_op_diagnostic["parser_diagnostics"]["parse_outcome"] = "NO_OP"
    not_run_with_provider_error_diagnostic = deepcopy(packet)
    not_run_with_provider_error_diagnostic["normalized_action"] = None
    not_run_with_provider_error_diagnostic["parser_outcome"] = "NOT_RUN"
    not_run_with_provider_error_diagnostic["parser_diagnostics"] = {
        "parse_outcome": "PROVIDER_ERROR"
    }
    unknown_top_level = deepcopy(packet)
    unknown_top_level["run_id"] = "g1run-must-not-be-scorer-visible"

    for invalid in (
        missing,
        wrong_version,
        empty_outcome,
        boolean_count,
        null_diagnostic_outcome,
        parsed_with_no_op_diagnostic,
        not_run_with_provider_error_diagnostic,
        unknown_top_level,
    ):
        assert not validator.is_valid(invalid)
        with pytest.raises(ReplayRunnerError, match="BLINDED_PACKET_INVALID"):
            validate_blinded_packet(cast(dict[str, JsonValue], invalid))


def test_runtime_fail_open_is_explicit_original_and_not_a_treatment() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    sham = next(plan for plan in fixture.plans if plan.arm is ArmKind.SHAM_BENIGN_EDIT)
    blocked_ir = replace(
        fixture.history_ir,
        capabilities=replace(
            fixture.history_ir.capabilities,
            supported_arms=(ArmKind.ORIGINAL,),
            supported_operations=(OperationKind.KEEP, OperationKind.KEEP_UNCERTAIN),
            opaque_or_server_managed=True,
        ),
    )
    fallback = render_request(
        fixture.request,
        blocked_ir,
        sham,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    assert fallback.rendered_request == fixture.request
    assert fallback.effective_arm is ArmKind.ORIGINAL
    assert fallback.count_as_treatment is False
    assert fallback.diffs == fallback.list_insertions == ()


def test_openai_compatible_live_send_and_runner_live_entrypoint_are_fail_only() -> None:
    fixture = _replay_fixture(UnitKind.CLEAN_CONTROL)
    provider_registry, fake = _provider_registry((FakeScenario.SUCCESS,))
    original = next(
        item
        for item in _preflight(fixture, provider_registry, fake)
        if item.schedule.arm is ArmKind.ORIGINAL
    )
    live_codec = OpenAICompatibleProviderCodec(
        codec_id="mobileworld.g1.provider.openai-compatible/deferred-v1",
        endpoint_revision="deferred://no-transport",
        parser=JsonActionParser(),
    )
    with pytest.raises(ReplayRunnerError, match="LIVE_TRANSPORT_DEFERRED"):
        live_codec.send(original.authorized_request)
    assert live_codec.send_calls == 1
    assert live_codec.normalize_calls == 0
    with pytest.raises(ReplayRunnerError, match="LIVE_EXECUTION_DEFERRED"):
        execute_live_arm(original)


def test_g14_schemas_meta_validate_and_cpu_manifest_stays_fail_closed() -> None:
    for path in sorted(G14_SCHEMA_ROOT.glob("*.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
    manifest = CpuReadinessManifest(
        code_sha256="4" * 64,
        schema_set_sha256="5" * 64,
        fake_scenarios=tuple(item.value for item in FakeScenario),
        focused_test_count=1,
        checks=CPU_REQUIRED_CHECKS,
    ).to_dict()
    _validator("cpu_manifest.schema.json").validate(manifest)
    assert all(
        manifest["readiness"][key] is False
        for key in (
            "live_transport_validation_complete",
            "live_history_codec_ready",
            "curated_transformations_ready",
            "run_ready_seal_present",
            "provider_invocation_allowed",
            "treatment_response_generation_allowed",
            "formal_replay_ready",
        )
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"code_sha256": "nonsense"},
        {"schema_set_sha256": "NOT-A-SHA256"},
        {"focused_test_count": True},
    ),
)
def test_cpu_readiness_manifest_rejects_nonsense(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "code_sha256": "4" * 64,
        "schema_set_sha256": "5" * 64,
        "fake_scenarios": tuple(item.value for item in FakeScenario),
        "focused_test_count": 1,
        "checks": CPU_REQUIRED_CHECKS,
    }
    values.update(overrides)
    with pytest.raises(ReplayRunnerError, match="CPU_READINESS"):
        CpuReadinessManifest(**values)  # type: ignore[arg-type]


def test_cli_live_status_is_no_send_and_hard_deferred(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["live-status"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "curated_transformations_ready": False,
        "gpu_used": False,
        "live_history_codec_ready": False,
        "provider_invocation_allowed": False,
        "run_ready_seal_present": False,
        "status": "DEFERRED_PENDING_OWNER_GPU_RESOURCE_REVIEW",
        "treatment_response_generation_allowed": False,
    }
