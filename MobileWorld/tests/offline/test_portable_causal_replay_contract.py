from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

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
    build_sidecar,
    validate_capabilities,
    validate_codec_capabilities,
    validate_history_ir,
    validate_plan_set,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.conformance import (
    build_fixture_plan,
    materialize_fixture_mapping,
    run_fixture_conformance,
)
from mobile_world.offline.causal_replay.contracts import (
    CapabilityLevel,
    CodecCapabilities,
    CodecScope,
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    EvidenceRef,
    FallbackState,
    MappingKind,
    OperationKind,
    PlanOperation,
    PlanSetProfile,
    PreparedProviderRequest,
    ProviderDecision,
    ProviderResult,
    ProviderResultStatus,
    RegionAvailability,
    RegionKind,
    RelationshipKind,
    SourceSpan,
    SpanRole,
    canonical_sha256,
    stable_id,
    text_sha256,
)
from mobile_world.offline.causal_replay.core import (
    _operation_coordinate_key,
    render_request,
    restore_original,
)
from mobile_world.offline.causal_replay.provider import (
    NoProviderInG12,
    authorize_prepared_request,
    validate_provider_result,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VECTOR_PATH = Path(__file__).parent / "fixtures/causal_replay/six_family_vectors.v1.json"
SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/g1_2"
EXPECTED_FAMILIES = {
    "raw_replay",
    "flat_progress",
    "rolling_summary",
    "flat_previous_actions",
    "hybrid_folding",
    "structured_folding",
}


def _vectors() -> list[dict]:
    payload = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == ("mobileworld.g1.portable-sentinel.conformance-vectors/v1")
    return [{**item, "capabilities": payload["capabilities"]} for item in payload["vectors"]]


def _codec(vector: dict) -> DeclarativeFixtureHistoryCodec:
    return DeclarativeFixtureHistoryCodec(materialize_fixture_mapping(vector))


@dataclass(frozen=True)
class _CodecDeclaration:
    codec_id: str
    contract_version: str
    history_family: HistoryFamily
    capabilities: CodecCapabilities
    frozen_ir: HistoryIR

    def extract(self, application_request) -> HistoryIR:
        assert canonical_sha256(application_request) == self.frozen_ir.raw_request_sha256
        return self.frozen_ir


def _declaration(codec, ir, capabilities: CodecCapabilities | None = None) -> _CodecDeclaration:
    return _CodecDeclaration(
        codec_id=codec.codec_id,
        contract_version=codec.contract_version,
        history_family=codec.history_family,
        capabilities=codec.capabilities if capabilities is None else capabilities,
        frozen_ir=ir,
    )


def _registry(codec) -> HistoryCodecRegistry:
    registry = HistoryCodecRegistry()
    registry.register_factory(
        history_family=codec.history_family,
        contract_version=codec.contract_version,
        codec_id=codec.codec_id,
        factory=lambda: codec,
    )
    return registry


def _provider_registry() -> tuple[ProviderCodecRegistry, NoProviderInG12]:
    provider = NoProviderInG12()
    registry = ProviderCodecRegistry()
    registry.register(provider)
    return registry, provider


def _paired_plans(vector: dict, ir) -> tuple:
    return tuple(
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=arm,
            correction_text=(vector["correction_text"] if arm is ArmKind.MASK_CORRECTION else None),
        )
        for arm in (ArmKind.ORIGINAL, ArmKind.MASK, ArmKind.MASK_CORRECTION, ArmKind.ORACLE_CLEAN)
    )


def _clean_fixture(vector_index: int = 0):
    vector = deepcopy(_vectors()[vector_index])
    vector["capabilities"]["supported_arms"] = [arm.value for arm in ArmKind]
    target_mapping = next(
        record
        for record in vector["mapping"]["records"]
        if record["record_key"] == vector["target_record_key"]
    )
    target_mapping["editable_spans"][0]["span_role"] = "BENIGN_SHAM"
    codec = _codec(vector)
    ir = codec.extract(vector["application_request"])
    plans = (
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=ArmKind.ORIGINAL,
        ),
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=ArmKind.SHAM_BENIGN_EDIT,
        ),
    )
    return vector, codec, ir, plans


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    schemas = {path.name: _schema(path.name) for path in sorted(SCHEMA_ROOT.glob("*.json"))}
    store = {schema["$id"]: schema for schema in schemas.values()}
    selected = schemas[name]
    return Draft202012Validator(
        selected,
        resolver=RefResolver.from_schema(selected, store=store),
    )


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: item["vector_id"])
def test_six_family_conformance(vector: dict) -> None:
    original = deepcopy(vector["application_request"])
    report = run_fixture_conformance(
        vector_id=vector["vector_id"],
        codec=_codec(vector),
        application_request=vector["application_request"],
        target_record_key=vector["target_record_key"],
        correction_text=vector["correction_text"],
    )
    assert report["history_family"] in EXPECTED_FAMILIES
    assert report["fixture_only"] is True
    assert report["production_ready"] is False
    assert all(report["checks"].values())
    assert vector["application_request"] == original


def test_exactly_six_frozen_families_register_without_checkpoint_logic() -> None:
    registry = HistoryCodecRegistry()
    for vector in _vectors():
        registry.register(_codec(vector))
    assert {item["history_family"] for item in registry.manifest()} == EXPECTED_FAMILIES
    assert {family.value for family in HistoryFamily} == EXPECTED_FAMILIES
    assert all(item["scope"] == "FIXTURE_ONLY" for item in registry.manifest())
    with pytest.raises(PortableContractError, match="DUPLICATE_CODEC_ID"):
        registry.register(_codec(_vectors()[0]))
    good = _codec(_vectors()[0])
    drifting = _codec(_vectors()[1])
    products = iter((good, drifting))
    drift_registry = HistoryCodecRegistry()
    drift_registry.register_factory(
        history_family=good.history_family,
        contract_version=good.contract_version,
        codec_id=good.codec_id,
        factory=lambda: next(products),
    )
    with pytest.raises(PortableContractError, match="CODEC_FACTORY_DRIFT"):
        drift_registry.by_id(good.codec_id)


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: item["vector_id"])
def test_history_ir_has_exact_codepoint_utf8_coordinates_and_relationships(vector: dict) -> None:
    ir = _codec(vector).extract(vector["application_request"])
    assert ir.records
    assert any(record.relationships or record.source_version_ids for record in ir.records)
    all_spans = [
        span
        for record in ir.records
        for span in (record.source_span, *record.editable_spans, *record.protected_spans)
    ]
    assert any(
        (span.utf8_byte_end - span.utf8_byte_start) > (span.char_end - span.char_start)
        for span in all_spans
    )
    for span in all_spans:
        span.validate_against(vector["application_request"])
        assert span.span_sha256 == text_sha256(span.exact_text)


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: item["vector_id"])
def test_versioned_schemas_accept_ir_plan_capability_and_sidecar(vector: dict) -> None:
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(
        ir=ir,
        record_key=vector["target_record_key"],
        arm=ArmKind.MASK_CORRECTION,
        correction_text=vector["correction_text"],
    )
    paired_plans = _paired_plans(vector, ir)
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    receipt = validate_pre_send(
        request,
        ir,
        plan,
        result,
        codec_registry=_registry(codec),
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    sidecar = build_sidecar(
        ir=ir,
        plan=plan,
        render_result=result,
        validation_receipt=receipt,
        codec_registry=_registry(codec),
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
    )
    _validator("codec_capabilities.schema.json").validate(ir.capabilities.to_dict())
    _validator("history_ir.schema.json").validate(ir.to_dict())
    _validator("transformation_plan.schema.json").validate(plan.to_dict())
    _validator("sidecar.schema.json").validate(sidecar.to_dict())
    payload = sidecar.to_dict()
    assert payload["plan_set_profile"] == "PORTABLE_CORE"
    assert [item["arm"] for item in payload["paired_plan_set"]] == [
        "ORIGINAL",
        "MASK",
        "MASK_CORRECTION",
        "ORACLE_CLEAN",
    ]
    assert payload["history_ir"]["codec_contract_version"] == codec.contract_version
    assert payload["transformation_plan"]["codec_contract_version"] == codec.contract_version
    missing_arm = deepcopy(payload)
    missing_arm["paired_plan_set"].pop()
    assert list(_validator("sidecar.schema.json").iter_errors(missing_arm))
    swapped_arms = deepcopy(payload)
    swapped_arms["paired_plan_set"][1:3] = reversed(swapped_arms["paired_plan_set"][1:3])
    assert list(_validator("sidecar.schema.json").iter_errors(swapped_arms))
    mismatched_profile = deepcopy(payload)
    mismatched_profile["validation_receipt"]["plan_set_profile"] = "G1_CLEAN_CONTROL"
    assert list(_validator("sidecar.schema.json").iter_errors(mismatched_profile))
    assert sidecar.provider_result is None
    assert sidecar.validation_receipt.invocation_attempted is False
    assert sidecar.validation_receipt.provider_decision is ProviderDecision.BLOCK


def test_all_new_schemas_are_draft_2020_12_and_reject_unknown_fields() -> None:
    paths = sorted(SCHEMA_ROOT.glob("*.json"))
    assert {path.name for path in paths} == {
        "codec_capabilities.schema.json",
        "history_ir.schema.json",
        "provider_result.schema.json",
        "sidecar.schema.json",
        "transformation_plan.schema.json",
    }
    for path in paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)
    capability = _codec(_vectors()[0]).capabilities.to_dict()
    capability["checkpoint_name"] = "forbidden-model-specific-field"
    assert list(_validator("codec_capabilities.schema.json").iter_errors(capability))


def test_provider_result_contract_is_pure_hash_bound_and_schema_valid() -> None:
    action = {"action_type": "click", "x": 12, "y": 34}
    response_sha = "c" * 64
    parameters = {"temperature": 0.0}
    result = ProviderResult(
        provider_codec_id="fixture-provider-contract-only",
        provider_contract_version="v1",
        endpoint_revision="fixture-endpoint-v1",
        status=ProviderResultStatus.RETURNED,
        application_request_sha256="a" * 64,
        encoded_request_sha256="b" * 64,
        response_sha256=response_sha,
        raw_response_ref={
            "sha256": response_sha,
            "byte_count": 42,
            "media_type": "application/json",
            "schema_version": None,
            "relative_path": "blobs/sha256/cc/fixture-response",
        },
        normalized_action=action,
        normalized_action_sha256=canonical_sha256(action),
        error=None,
        model_parameters=parameters,
        model_parameters_sha256=canonical_sha256(parameters),
    )
    validate_provider_result(result)
    _validator("provider_result.schema.json").validate(result.to_dict())
    with pytest.raises(PortableContractError, match="NORMALIZED_ACTION_HASH_MISMATCH"):
        validate_provider_result(replace(result, normalized_action_sha256="d" * 64))
    malformed_ref = dict(result.raw_response_ref or {})
    malformed_ref["unexpected"] = True
    for malformed, code in (
        (replace(result, provider_codec_id=""), "PROVIDER_IDENTITY_MISSING"),
        (
            replace(
                result,
                response_sha256="x",
                raw_response_ref={**(result.raw_response_ref or {}), "sha256": "x"},
            ),
            "INVALID_SHA256",
        ),
        (replace(result, raw_response_ref=malformed_ref), "INVALID_ARTIFACT_REF"),
    ):
        with pytest.raises(PortableContractError, match=code):
            validate_provider_result(malformed)
        assert list(_validator("provider_result.schema.json").iter_errors(malformed.to_dict()))


def test_mask_deletes_only_target_and_preserves_tool_images_parameters() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK)
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    before = request["messages"][2]["content"]
    after = result.rendered_request["messages"][2]["content"]
    assert "地图在左侧" in before and "地图在左侧" not in after
    assert "<tool_call>" in after
    assert result.rendered_request["messages"][3:] == request["messages"][3:]
    assert result.rendered_request["temperature"] == request["temperature"]
    assert all(diff.rendered_text == "" for diff in result.diffs)
    assert all(diff.mapping_kind is MappingKind.DELETED for diff in result.diffs)


def test_correction_is_separate_visible_sentinel_context_not_actor_speech() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(
        ir=ir,
        record_key=vector["target_record_key"],
        arm=ArmKind.MASK_CORRECTION,
        correction_text=vector["correction_text"],
    )
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    old_actor = result.rendered_request["messages"][2]["content"]
    current_context = result.rendered_request["messages"][4]["content"]
    assert vector["correction_text"] not in old_actor
    assert current_context[1] == {
        "type": "text",
        "text": f"[SENTINEL CONTEXT]\n{vector['correction_text']}",
    }
    assert len(result.list_insertions) == 1
    assert plan.operations[0].evidence_refs


def test_correction_without_declared_anchor_or_evidence_fails_closed() -> None:
    vector = _vectors()[1]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(
        ir=ir,
        record_key=vector["target_record_key"],
        arm=ArmKind.MASK_CORRECTION,
        correction_text=vector["correction_text"],
    )
    operation = plan.operations[0]
    bad_operation = replace(
        operation,
        correction_anchor=None,
        rendered_correction_context=None,
        evidence_refs=(),
    )
    with pytest.raises(PortableContractError, match="CORRECTION_EVIDENCE_MISSING"):
        codec.render(
            request,
            ir,
            replace(plan, operations=(bad_operation,)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_hash_drift_ambiguous_overlap_and_prediction_plan_all_block() -> None:
    vector = _vectors()[1]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK)
    altered = deepcopy(request)
    altered["messages"][1]["content"][0]["text"] += "x"
    with pytest.raises(PortableContractError, match="REQUEST_HASH_DRIFT"):
        codec.render(
            altered,
            ir,
            plan,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    duplicate = replace(plan.operations[0], operation_id="second-overlapping-operation")
    with pytest.raises(PortableContractError, match="OVERLAPPING_PLAN_EDITS"):
        codec.render(
            request,
            ir,
            replace(plan, operations=(*plan.operations, duplicate)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    with pytest.raises(PortableContractError, match="UNSAFE_PLAN_PROVENANCE"):
        codec.render(
            request,
            ir,
            replace(plan, deployment_prediction=True),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    for forged in (
        replace(plan, curated=1),
        replace(plan, curated="yes"),
        replace(plan, deployment_prediction=0),
        replace(plan, deployment_prediction=None),
    ):
        with pytest.raises(PortableContractError, match="UNSAFE_PLAN_PROVENANCE"):
            codec.render(
                request,
                ir,
                forged,
                execution_mode=ExecutionMode.G1_SCIENTIFIC,
                failure_policy=FailurePolicy.BLOCK,
            )
        assert list(_validator("transformation_plan.schema.json").iter_errors(forged.to_dict()))


def test_fixture_span_materialization_never_fuzzy_relocates_text() -> None:
    vector = deepcopy(_vectors()[2])
    vector["mapping"]["records"][0]["editable_spans"][0]["char_start"] += 1
    with pytest.raises(PortableContractError, match="FIXTURE_SPAN_DRIFT"):
        materialize_fixture_mapping(vector)


def test_full_plan_set_capability_preflight_happens_before_provider() -> None:
    vector = _vectors()[3]
    codec = _codec(vector)
    ir = codec.extract(vector["application_request"])
    plans = tuple(
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=arm,
            correction_text=(vector["correction_text"] if arm is ArmKind.MASK_CORRECTION else None),
        )
        for arm in (ArmKind.ORIGINAL, ArmKind.MASK, ArmKind.MASK_CORRECTION)
    )
    narrowed = replace(codec.capabilities, supported_arms=(ArmKind.ORIGINAL,))
    with pytest.raises(PortableContractError, match="UNSUPPORTED_PLAN_SET"):
        validate_capabilities(
            plans,
            narrowed,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_fixture_codec_cannot_authorize_provider_and_g12_provider_is_unavailable() -> None:
    vector = _vectors()[4]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL)
    paired_plans = _paired_plans(vector, ir)
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    blocked_receipt = validate_pre_send(
        request,
        ir,
        plan,
        result,
        codec_registry=_registry(codec),
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert blocked_receipt.provider_decision is ProviderDecision.BLOCK
    assert blocked_receipt.provider_invocation_allowed is False
    receipt = validate_pre_send(
        request,
        ir,
        plan,
        result,
        codec_registry=_registry(codec),
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    prepared = PreparedProviderRequest(
        provider_codec_id="fake",
        provider_contract_version="v1",
        endpoint_revision="fixture-endpoint-v1",
        application_request_sha256=canonical_sha256(request),
        encoded_request_sha256=text_sha256("fixture"),
        encoded_request=b"fixture",
        model_parameters={},
        model_parameters_sha256=canonical_sha256({}),
    )
    with pytest.raises(PortableContractError, match="PROVIDER_INVOCATION_NOT_AUTHORIZED"):
        authorize_prepared_request(
            prepared,
            receipt,
            ir=ir,
            plan=plan,
            render_result=result,
            codec_registry=_registry(codec),
            provider_registry=_provider_registry()[0],
            codec_contract_version=codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        )
    provider = NoProviderInG12()
    with pytest.raises(PortableContractError, match="PROVIDER_NOT_IMPLEMENTED_G1_2"):
        provider.encode(request, {})
    ProviderCodecRegistry().register(provider)


def test_runtime_fail_open_is_explicit_pristine_and_never_a_treatment() -> None:
    vector, codec, ir, paired_plans = _clean_fixture(5)
    request = vector["application_request"]
    plan = paired_plans[1]
    blocked_ir = replace(
        ir,
        capabilities=replace(
            ir.capabilities,
            supported_arms=(ArmKind.ORIGINAL,),
            supported_operations=(OperationKind.KEEP, OperationKind.KEEP_UNCERTAIN),
            opaque_or_server_managed=True,
        ),
    )
    with pytest.raises(PortableContractError, match="CODEC_IR_MISMATCH"):
        codec.render(
            request,
            blocked_ir,
            plan,
            execution_mode=ExecutionMode.RUNTIME,
            failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
        )
    fallback = render_request(
        request,
        blocked_ir,
        plan,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    assert fallback.rendered_request == request
    assert fallback.fallback_state is FallbackState.EXPLICIT_ORIGINAL
    assert fallback.count_as_treatment is False
    assert fallback.diffs == fallback.list_insertions == ()
    blocked = render_request(
        request,
        blocked_ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert blocked.fallback_state is FallbackState.BLOCKED_BEFORE_PROVIDER
    assert blocked.effective_arm is None
    blocked_codec = _declaration(codec, blocked_ir, blocked_ir.capabilities)
    blocked_receipt = validate_pre_send(
        request,
        blocked_ir,
        plan,
        blocked,
        codec_registry=_registry(blocked_codec),
        codec_contract_version=blocked_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    blocked_sidecar = build_sidecar(
        ir=blocked_ir,
        plan=plan,
        render_result=blocked,
        validation_receipt=blocked_receipt,
        codec_registry=_registry(blocked_codec),
        codec_contract_version=blocked_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
    )
    _validator("sidecar.schema.json").validate(blocked_sidecar.to_dict())
    assert blocked_sidecar.provider_attempt.invocation_attempted is False

    live_capabilities = replace(
        blocked_ir.capabilities,
        scope=CodecScope.LIVE,
        live_ready=True,
    )
    live_ir = replace(blocked_ir, capabilities=live_capabilities)
    live_codec = _declaration(codec, live_ir, live_capabilities)
    live_fallback = render_request(
        request,
        live_ir,
        plan,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    provider_registry, provider = _provider_registry()
    runtime_parameters = {"temperature": 0.0}
    bypass_receipt = validate_pre_send(
        request,
        live_ir,
        plan,
        live_fallback,
        codec_registry=_registry(live_codec),
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
        intended_provider_codec_id=provider.codec_id,
        intended_provider_contract_version="v1",
        intended_endpoint_revision="endpoint-v1",
        model_parameters=runtime_parameters,
    )
    assert bypass_receipt.provider_decision is ProviderDecision.BYPASS_ORIGINAL
    assert bypass_receipt.provider_invocation_allowed is True
    prepared = PreparedProviderRequest(
        provider_codec_id=provider.codec_id,
        provider_contract_version="v1",
        endpoint_revision="endpoint-v1",
        application_request_sha256=canonical_sha256(request),
        encoded_request_sha256=text_sha256("runtime-original"),
        encoded_request=b"runtime-original",
        model_parameters=runtime_parameters,
        model_parameters_sha256=canonical_sha256(runtime_parameters),
    )
    authorized = authorize_prepared_request(
        prepared,
        bypass_receipt,
        ir=live_ir,
        plan=plan,
        render_result=live_fallback,
        codec_registry=_registry(live_codec),
        provider_registry=provider_registry,
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
    )
    fallback_sidecar = build_sidecar(
        ir=live_ir,
        plan=plan,
        render_result=live_fallback,
        validation_receipt=bypass_receipt,
        codec_registry=_registry(live_codec),
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        provider_registry=provider_registry,
        authorized_request=authorized,
    )
    _validator("sidecar.schema.json").validate(fallback_sidecar.to_dict())
    assert fallback_sidecar.provider_attempt.invocation_attempted is False
    forged_fallback = fallback_sidecar.to_dict()
    forged_fallback["fallback"]["state"] = "NOT_NEEDED"
    forged_fallback["execution"]["unsupported_reason"] = None
    assert list(_validator("sidecar.schema.json").iter_errors(forged_fallback))


def test_legacy_annotation_operations_cannot_silently_replace_g1_treatment() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK)
    uncertain = replace(plan.operations[0], kind=OperationKind.KEEP_UNCERTAIN)
    with pytest.raises(PortableContractError, match="NON_EXECUTABLE_G1_OPERATION"):
        codec.render(
            request,
            ir,
            replace(plan, operations=(uncertain,)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_portable_package_has_no_legacy_runtime_or_provider_imports() -> None:
    package_root = REPO_ROOT / "MobileWorld/src/mobile_world/offline/causal_replay"
    forbidden = ("sentinel_mvp", "openai", "requests", "PIL", "mobile_world.runtime")
    for path in package_root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not any(f"import {name}" in text or f"from {name}" in text for name in forbidden)


def test_validator_recomputes_entire_render_receipt_and_mapping() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK)
    paired_plans = _paired_plans(vector, ir)
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    forged_diff = replace(result.diffs[0], original_sha256="0" * 64)
    forged_mapping = replace(result.source_mappings[0], rendered_char_end=0)
    for forged in (
        replace(result, plan_sha256="0" * 64),
        replace(result, diffs=(forged_diff,)),
        replace(result, source_mappings=(forged_mapping, *result.source_mappings[1:])),
    ):
        with pytest.raises(PortableContractError, match="RENDER_RECEIPT_MISMATCH"):
            validate_pre_send(
                request,
                ir,
                plan,
                forged,
                codec_registry=_registry(codec),
                codec_contract_version=codec.contract_version,
                paired_plans=paired_plans,
                plan_set_profile=PlanSetProfile.PORTABLE_CORE,
                execution_mode=ExecutionMode.G1_SCIENTIFIC,
                failure_policy=FailurePolicy.BLOCK,
            )


def test_adjacent_drop_spans_restore_in_source_order() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    record = ir.records[0]
    target = record.editable_spans[0]
    split = target.char_start + 1
    container = target.validate_against(request)

    def adjacent_claim_id(start: int, end: int) -> str:
        return stable_id(
            "claim",
            {
                "record_id": record.record_id,
                "container_path": list(target.container_path),
                "char_start": start,
                "char_end": end,
                "span_sha256": text_sha256(container[start:end]),
            },
        )

    first = SourceSpan.from_text(
        container_path=target.container_path,
        container_text=container,
        char_start=target.char_start,
        char_end=split,
        span_role=SpanRole.EDITABLE_CLAIM,
        claim_id=adjacent_claim_id(target.char_start, split),
    )
    second = SourceSpan.from_text(
        container_path=target.container_path,
        container_text=container,
        char_start=split,
        char_end=target.char_end,
        span_role=SpanRole.EDITABLE_CLAIM,
        claim_id=adjacent_claim_id(split, target.char_end),
    )
    split_record = replace(record, editable_spans=(first, second))
    split_ir = replace(ir, records=(split_record, *ir.records[1:]))
    operations = tuple(
        PlanOperation(
            operation_id=f"drop-adjacent-{index}",
            kind=OperationKind.DROP,
            target_record_id=record.record_id,
            target_span=span,
        )
        for index, span in enumerate((first, second))
    )
    base_plan = build_fixture_plan(ir=ir, record_key=record.record_key, arm=ArmKind.MASK)
    plan = replace(base_plan, operations=operations)
    result = codec.render(
        request,
        split_ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert restore_original(result) == request
    with pytest.raises(PortableContractError, match="NON_CANONICAL_OPERATION_ORDER"):
        codec.render(
            request,
            split_ir,
            replace(base_plan, operations=tuple(reversed(operations))),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_records_and_corrections_cannot_escape_history_or_become_actor_speech() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    task_region = next(region for region in ir.regions if region.kind is RegionKind.TASK)
    with pytest.raises(PortableContractError, match="RECORD_OUTSIDE_HISTORY"):
        codec.render(
            request,
            replace(ir, records=(replace(ir.records[0], region_id=task_region.region_id),)),
            build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    record = ir.records[0]
    anchor = replace(record.correction_anchors[0], expected_role="assistant")
    bad_ir = replace(
        ir,
        records=(replace(record, correction_anchors=(anchor,)), *ir.records[1:]),
    )
    with pytest.raises(PortableContractError, match="CORRECTION_ANCHOR_ACTOR_OWNED"):
        codec.render(
            request,
            bad_ir,
            build_fixture_plan(
                ir=bad_ir,
                record_key=vector["target_record_key"],
                arm=ArmKind.MASK_CORRECTION,
                correction_text=vector["correction_text"],
            ),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    misplaced_anchor = replace(record.correction_anchors[0], insert_index=0)
    misplaced_ir = replace(
        ir,
        records=(replace(record, correction_anchors=(misplaced_anchor,)), *ir.records[1:]),
    )
    with pytest.raises(PortableContractError, match="CORRECTION_INSERTION_COORDINATE_MISMATCH"):
        codec.render(
            request,
            misplaced_ir,
            build_fixture_plan(
                ir=misplaced_ir,
                record_key=vector["target_record_key"],
                arm=ArmKind.MASK_CORRECTION,
                correction_text=vector["correction_text"],
            ),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    with pytest.raises(PortableContractError, match="RECORD_ID_DRIFT"):
        codec.render(
            request,
            replace(ir, records=(replace(record, author="SENTINEL"), *ir.records[1:])),
            build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    history_region = next(region for region in ir.regions if region.kind is RegionKind.HISTORY)
    system_region = next(region for region in ir.regions if region.kind is RegionKind.SYSTEM)
    overlapping_paths = (*history_region.paths, *system_region.paths)
    overlapping_projection = [
        request["messages"][path[1]] for path in overlapping_paths if path[0] == "messages"
    ]
    overlapping_history = replace(
        history_region,
        paths=overlapping_paths,
        source_sha256=canonical_sha256(overlapping_projection),
    )
    overlapping_ir = replace(
        ir,
        regions=tuple(
            overlapping_history if item.region_id == history_region.region_id else item
            for item in ir.regions
        ),
    )
    with pytest.raises(PortableContractError, match="AMBIGUOUS_HISTORY_REGION"):
        codec.render(
            request,
            overlapping_ir,
            build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    wrong_extent = replace(
        record,
        source_span=replace(
            record.source_span,
            span_role=SpanRole.EDITABLE_CLAIM,
            claim_id=record.editable_spans[0].claim_id,
        ),
    )
    with pytest.raises(PortableContractError, match="RECORD_EXTENT_ROLE_INVALID"):
        codec.render(
            request,
            replace(ir, records=(wrong_extent, *ir.records[1:])),
            build_fixture_plan(
                ir=ir,
                record_key=vector["target_record_key"],
                arm=ArmKind.MASK,
            ),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_capability_semantics_and_pre_send_support_are_fail_closed() -> None:
    vector = _vectors()[1]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    invalid = replace(
        codec.capabilities,
        level=CapabilityLevel.AUDIT_ONLY,
        scope=CodecScope.LIVE,
        live_ready=True,
    )
    with pytest.raises(PortableContractError, match="CAPABILITY_LEVEL_MISMATCH"):
        validate_codec_capabilities(invalid)
    with pytest.raises(PortableContractError, match="CAPABILITY_LEVEL_MISMATCH"):
        validate_codec_capabilities(
            replace(
                codec.capabilities,
                level=CapabilityLevel.AUDIT_ONLY,
                supported_arms=(ArmKind.ORIGINAL,),
                supported_operations=(OperationKind.ARCHIVE,),
                live_ready=False,
            )
        )
    for forged in (
        replace(codec.capabilities, codec_id=""),
        replace(codec.capabilities, live_ready=1),
        replace(codec.capabilities, preserves_roles=1),
        replace(codec.capabilities, opaque_or_server_managed=0),
    ):
        with pytest.raises(PortableContractError, match="CODEC_IDENTITY_MISSING|INVALID_BOOLEAN"):
            validate_codec_capabilities(forged)
        assert list(_validator("codec_capabilities.schema.json").iter_errors(forged.to_dict()))
    with pytest.raises(PortableContractError, match="CAPABILITY_LEVEL_MISMATCH"):
        validate_codec_capabilities(
            replace(
                codec.capabilities,
                level=CapabilityLevel.VALIDITY_TRANSFORMATION,
                supported_operations=(
                    *codec.capabilities.supported_operations,
                    OperationKind.ARCHIVE,
                ),
            )
        )
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK)
    result = codec.render(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    unsupported = replace(
        codec.capabilities,
        supported_arms=(ArmKind.ORIGINAL,),
        supported_operations=(OperationKind.KEEP,),
        opaque_or_server_managed=True,
    )
    unsupported_ir = replace(ir, capabilities=unsupported)
    unsupported_codec = _declaration(codec, unsupported_ir, unsupported)
    paired_plans = _paired_plans(vector, unsupported_ir)
    with pytest.raises(PortableContractError, match="UNSUPPORTED_PLAN_SET"):
        validate_pre_send(
            request,
            unsupported_ir,
            plan,
            result,
            codec_registry=_registry(unsupported_codec),
            codec_contract_version=unsupported_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.PORTABLE_CORE,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_clean_control_profile_is_independent_of_codec_maximum_capability() -> None:
    vector = deepcopy(_vectors()[0])
    vector["capabilities"]["supported_arms"] = [arm.value for arm in ArmKind]
    record_mapping = vector["mapping"]["records"][0]
    record_mapping["editable_spans"][0]["span_role"] = "BENIGN_SHAM"
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plans = (
        build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL),
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=ArmKind.SHAM_BENIGN_EDIT,
        ),
    )
    digest = validate_plan_set(
        request,
        ir,
        plans,
        codec_registry=_registry(codec),
        codec_contract_version=codec.contract_version,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert len(digest) == 64


def test_live_codec_cannot_use_fixture_conformance_profile() -> None:
    vector, codec, fixture_ir, plans = _clean_fixture(0)
    request = vector["application_request"]
    live_capabilities = replace(codec.capabilities, scope=CodecScope.LIVE, live_ready=True)
    live_ir = replace(fixture_ir, capabilities=live_capabilities)
    live_codec = _declaration(codec, live_ir, live_capabilities)
    with pytest.raises(PortableContractError, match="PORTABLE_PROFILE_LIVE_FORBIDDEN"):
        validate_plan_set(
            request,
            live_ir,
            plans,
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            plan_set_profile=PlanSetProfile.PORTABLE_CORE,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_sham_requires_exactly_one_complete_benign_target() -> None:
    vector = deepcopy(_vectors()[1])
    vector["capabilities"]["supported_arms"] = [arm.value for arm in ArmKind]
    for record in vector["mapping"]["records"]:
        record["editable_spans"][0]["span_role"] = "BENIGN_SHAM"
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    original = build_fixture_plan(
        ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL
    )
    first = build_fixture_plan(ir=ir, record_key="progress_step_1", arm=ArmKind.SHAM_BENIGN_EDIT)
    second = build_fixture_plan(ir=ir, record_key="progress_step_2", arm=ArmKind.SHAM_BENIGN_EDIT)
    second_operation = replace(second.operations[0], operation_id="fixture-drop-target-2")
    multi = replace(first, operations=(*first.operations, second_operation))
    with pytest.raises(PortableContractError, match="SHAM_TARGET_COUNT_INVALID"):
        validate_plan_set(
            request,
            ir,
            (original, multi),
            codec_registry=_registry(codec),
            codec_contract_version=codec.contract_version,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    assert list(_validator("transformation_plan.schema.json").iter_errors(multi.to_dict()))


def test_operation_path_order_keeps_numeric_indices_numeric() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    ir = codec.extract(vector["application_request"])
    operation = build_fixture_plan(
        ir=ir, record_key=vector["target_record_key"], arm=ArmKind.MASK
    ).operations[0]
    at_two = replace(
        operation,
        operation_id="at-message-2",
        target_span=replace(operation.target_span, container_path=("messages", 2, "content")),
    )
    at_ten = replace(
        operation,
        operation_id="at-message-10",
        target_span=replace(operation.target_span, container_path=("messages", 10, "content")),
    )
    assert sorted((at_ten, at_two), key=_operation_coordinate_key) == [at_two, at_ten]


def test_history_and_plan_contract_versions_are_registry_bound() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL)
    with pytest.raises(PortableContractError, match="PLAN_CODEC_MISMATCH"):
        render_request(
            request,
            ir,
            replace(plan, codec_contract_version="v2"),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    with pytest.raises(PortableContractError, match="CAPABILITY_VERSION_MISMATCH"):
        validate_history_ir(request, replace(ir, codec_contract_version="v2"))


def test_opaque_history_without_records_emits_typed_block_and_sidecar() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    source_ir = codec.extract(request)
    paired_plans = _paired_plans(vector, source_ir)
    history_region = next(
        region for region in source_ir.regions if region.kind is RegionKind.HISTORY
    )
    opaque_capabilities = replace(
        source_ir.capabilities,
        level=CapabilityLevel.AUDIT_ONLY,
        supported_operations=(OperationKind.KEEP,),
        supported_arms=(ArmKind.ORIGINAL,),
        opaque_or_server_managed=True,
    )
    opaque_ir = replace(
        source_ir,
        regions=tuple(
            replace(
                region,
                availability=RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT,
                absence_reason="history is server-managed and unavailable",
                paths=(),
                text_slices=(),
                source_sha256=canonical_sha256([]),
            )
            if region.region_id == history_region.region_id
            else region
            for region in source_ir.regions
        ),
        records=(),
        source_versions=(),
        capabilities=opaque_capabilities,
    )
    opaque_codec = _declaration(codec, opaque_ir, opaque_capabilities)
    present_opaque_ir = replace(
        opaque_ir,
        regions=source_ir.regions,
    )
    with pytest.raises(PortableContractError, match="OPAQUE_HISTORY_REGION_PRESENT"):
        validate_history_ir(request, present_opaque_ir)
    assert list(_validator("history_ir.schema.json").iter_errors(present_opaque_ir.to_dict()))
    mask = paired_plans[1]
    blocked = render_request(
        request,
        opaque_ir,
        mask,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert blocked.fallback_state is FallbackState.BLOCKED_BEFORE_PROVIDER
    assert blocked.unsupported_reason == "OPAQUE_OR_SERVER_MANAGED_HISTORY"
    assert blocked.requested_arm is ArmKind.MASK
    assert blocked.effective_arm is None
    receipt = validate_pre_send(
        request,
        opaque_ir,
        mask,
        blocked,
        codec_registry=_registry(opaque_codec),
        codec_contract_version=opaque_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert receipt.provider_decision is ProviderDecision.BLOCK
    assert receipt.invocation_attempted is False
    sidecar = build_sidecar(
        ir=opaque_ir,
        plan=mask,
        render_result=blocked,
        validation_receipt=receipt,
        codec_registry=_registry(opaque_codec),
        codec_contract_version=opaque_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
    )
    _validator("sidecar.schema.json").validate(sidecar.to_dict())

    malformed_mask = replace(mask, operations=(replace(mask.operations[0], operation_id=""),))
    with pytest.raises(PortableContractError, match="OPERATION_ID_MISSING"):
        render_request(
            request,
            opaque_ir,
            malformed_mask,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    correction = paired_plans[2]
    malformed_evidence = replace(
        correction.operations[0],
        evidence_refs=(EvidenceRef("", "a" * 64, "", -1),),
    )
    with pytest.raises(PortableContractError, match="INVALID_EVIDENCE_IDENTITY"):
        render_request(
            request,
            opaque_ir,
            replace(correction, operations=(malformed_evidence,)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_nonopaque_history_requires_at_least_one_record() -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = replace(codec.extract(request), records=(), source_versions=())
    with pytest.raises(PortableContractError, match="EMPTY_HISTORY_IR"):
        validate_history_ir(request, ir)
    assert list(_validator("history_ir.schema.json").iter_errors(ir.to_dict()))


def test_history_ir_identity_and_versions_match_schema_primitives() -> None:
    raw_vector = _vectors()[0]
    raw_codec = _codec(raw_vector)
    raw_request = raw_vector["application_request"]
    raw_ir = raw_codec.extract(raw_request)
    forged_warnings = replace(raw_ir, warnings=(1,))
    with pytest.raises(PortableContractError, match="INVALID_IR_WARNING"):
        validate_history_ir(raw_request, forged_warnings)
    assert list(_validator("history_ir.schema.json").iter_errors(forged_warnings.to_dict()))
    first = raw_ir.records[0]
    for forged_record, error in (
        (replace(first, record_key=""), "RECORD_IDENTITY_MISSING"),
        (replace(first, exposure_time=""), "RECORD_IDENTITY_MISSING"),
        (replace(first, version=True), "INVALID_RECORD_VERSION"),
        (replace(first, write_time=1), "INVALID_RECORD_WRITE_TIME"),
        (replace(first, provenance=[]), "INVALID_RECORD_PROVENANCE"),
    ):
        forged_ir = replace(raw_ir, records=(forged_record, *raw_ir.records[1:]))
        with pytest.raises(PortableContractError, match=error):
            validate_history_ir(raw_request, forged_ir)
        assert list(_validator("history_ir.schema.json").iter_errors(forged_ir.to_dict()))

    related_index = next(
        index for index, record in enumerate(raw_ir.records) if record.related_content
    )
    related_record = raw_ir.records[related_index]
    forged_related = replace(related_record.related_content[0], kind=RegionKind.SYSTEM)
    forged_related_record = replace(
        related_record,
        related_content=(forged_related, *related_record.related_content[1:]),
    )
    forged_related_ir = replace(
        raw_ir,
        records=tuple(
            forged_related_record if index == related_index else record
            for index, record in enumerate(raw_ir.records)
        ),
    )
    with pytest.raises(PortableContractError, match="INVALID_RELATED_CONTENT_KIND"):
        validate_history_ir(raw_request, forged_related_ir)
    assert list(_validator("history_ir.schema.json").iter_errors(forged_related_ir.to_dict()))
    forged_blob = replace(related_record.related_content[0], blob_sha256="not-a-digest")
    forged_blob_record = replace(
        related_record,
        related_content=(forged_blob, *related_record.related_content[1:]),
    )
    forged_blob_ir = replace(
        raw_ir,
        records=tuple(
            forged_blob_record if index == related_index else record
            for index, record in enumerate(raw_ir.records)
        ),
    )
    with pytest.raises(PortableContractError, match="INVALID_SHA256"):
        validate_history_ir(raw_request, forged_blob_ir)
    assert list(_validator("history_ir.schema.json").iter_errors(forged_blob_ir.to_dict()))

    rolling_vector = _vectors()[2]
    rolling_codec = _codec(rolling_vector)
    rolling_request = rolling_vector["application_request"]
    rolling_ir = rolling_codec.extract(rolling_request)
    absent_index = next(
        index
        for index, region in enumerate(rolling_ir.regions)
        if region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    )
    forged_absent = replace(
        rolling_ir,
        regions=tuple(
            replace(region, absence_reason=1) if index == absent_index else region
            for index, region in enumerate(rolling_ir.regions)
        ),
    )
    with pytest.raises(PortableContractError, match="INVALID_ABSENT_REGION"):
        validate_history_ir(rolling_request, forged_absent)
    assert list(_validator("history_ir.schema.json").iter_errors(forged_absent.to_dict()))
    version = rolling_ir.source_versions[0]
    for forged_version, error in (
        (replace(version, source_record_id=""), "SOURCE_VERSION_SOURCE_MISSING"),
        (replace(version, version=True), "INVALID_SOURCE_VERSION"),
        (replace(version, model_visible_in_current_request=0), "INVALID_BOOLEAN"),
        (replace(version, write_time=1), "INVALID_SOURCE_VERSION_WRITE_TIME"),
        (replace(version, provenance=[]), "INVALID_SOURCE_VERSION_PROVENANCE"),
    ):
        forged_ir = replace(rolling_ir, source_versions=(forged_version,))
        with pytest.raises(PortableContractError, match=error):
            validate_history_ir(rolling_request, forged_ir)
        assert list(_validator("history_ir.schema.json").iter_errors(forged_ir.to_dict()))

    flat_vector = _vectors()[1]
    flat_codec = _codec(flat_vector)
    flat_request = flat_vector["application_request"]
    flat_ir = flat_codec.extract(flat_request)
    slice_region_index = next(
        index for index, region in enumerate(flat_ir.regions) if region.text_slices
    )
    slice_region = flat_ir.regions[slice_region_index]
    forged_slice = replace(slice_region.text_slices[0], char_start=False)
    forged_slice_ir = replace(
        flat_ir,
        regions=tuple(
            replace(region, text_slices=(forged_slice, *region.text_slices[1:]))
            if index == slice_region_index
            else region
            for index, region in enumerate(flat_ir.regions)
        ),
    )
    with pytest.raises(PortableContractError, match="INVALID_REGION_TEXT_SLICE"):
        validate_history_ir(flat_request, forged_slice_ir)
    assert list(_validator("history_ir.schema.json").iter_errors(forged_slice_ir.to_dict()))


@pytest.mark.parametrize(
    ("evidence", "error"),
    (
        (EvidenceRef("", "a" * 64, "current_gui", 1), "INVALID_EVIDENCE_IDENTITY"),
        (EvidenceRef("evidence", "a" * 64, "", 1), "INVALID_EVIDENCE_IDENTITY"),
        (EvidenceRef("evidence", "a" * 64, "current_gui", -1), "INVALID_EVIDENCE_EVENT_SEQ"),
        (EvidenceRef("evidence", "a" * 64, "current_gui", True), "INVALID_EVIDENCE_EVENT_SEQ"),
    ),
)
def test_correction_evidence_runtime_matches_schema(evidence: EvidenceRef, error: str) -> None:
    vector = _vectors()[0]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    plan = build_fixture_plan(
        ir=ir,
        record_key=vector["target_record_key"],
        arm=ArmKind.MASK_CORRECTION,
        correction_text=vector["correction_text"],
    )
    bad_operation = replace(plan.operations[0], evidence_refs=(evidence,))
    with pytest.raises(PortableContractError, match=error):
        render_request(
            request,
            ir,
            replace(plan, operations=(bad_operation,)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_paired_plan_set_requires_same_mask_and_correction_targets() -> None:
    vector = _vectors()[1]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    first_record, second_record = ir.records
    ir = replace(
        ir,
        records=(
            first_record,
            replace(second_record, correction_anchors=first_record.correction_anchors),
        ),
    )
    original = build_fixture_plan(
        ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL
    )
    mask = build_fixture_plan(ir=ir, record_key="progress_step_1", arm=ArmKind.MASK)
    correction = build_fixture_plan(
        ir=ir,
        record_key="progress_step_2",
        arm=ArmKind.MASK_CORRECTION,
        correction_text=vector["correction_text"],
    )
    oracle = build_fixture_plan(ir=ir, record_key="progress_step_1", arm=ArmKind.ORACLE_CLEAN)
    with pytest.raises(PortableContractError, match="PAIRED_TARGET_SET_MISMATCH"):
        validate_plan_set(
            request,
            ir,
            (original, mask, correction, oracle),
            codec_registry=_registry(_declaration(codec, ir)),
            codec_contract_version=codec.contract_version,
            plan_set_profile=PlanSetProfile.PORTABLE_CORE,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_family_specific_fixture_contracts_are_machine_visible() -> None:
    by_family = {
        vector["mapping"]["history_family"]: (_codec(vector), vector) for vector in _vectors()
    }
    for codec, vector in by_family.values():
        ir = codec.extract(vector["application_request"])
        assert {
            RegionKind.SYSTEM,
            RegionKind.TASK,
            RegionKind.HISTORY,
            RegionKind.CURRENT_OBSERVATION,
            RegionKind.TOOL_PROTOCOL,
        }.issubset({region.kind for region in ir.regions})
        for record in ir.records:
            assert record.record_sha256 == record.source_span.span_sha256
            assert record.coordinates.request_path == record.source_span.container_path
            assert record.related_content
    flat_ir = by_family["flat_progress"][0].extract(
        by_family["flat_progress"][1]["application_request"]
    )
    assert len(flat_ir.records) >= 2
    rolling_ir = by_family["rolling_summary"][0].extract(
        by_family["rolling_summary"][1]["application_request"]
    )
    assert rolling_ir.source_versions
    assert any(
        relationship.kind is RelationshipKind.SOURCE_VERSION
        and relationship.target_version_id is not None
        for record in rolling_ir.records
        for relationship in record.relationships
    )
    previous_ir = by_family["flat_previous_actions"][0].extract(
        by_family["flat_previous_actions"][1]["application_request"]
    )
    assert len(previous_ir.records) >= 2
    assert [record.coordinates.representation_record_index for record in previous_ir.records] == [
        0,
        1,
    ]
    assert all(
        record.provenance["historical_result_visibility"] == "NOT_MODEL_VISIBLE"
        for record in previous_ir.records
    )
    hybrid_ir = by_family["hybrid_folding"][0].extract(
        by_family["hybrid_folding"][1]["application_request"]
    )
    assert len(hybrid_ir.records) >= 2
    assert any(
        relationship.kind is RelationshipKind.ALIGNED_RECORD
        for record in hybrid_ir.records
        for relationship in record.relationships
    )
    assert any(
        span.span_role is SpanRole.ELIGIBLE_PROTOCOL_SHELL
        for record in hybrid_ir.records
        for span in record.protected_spans
    )


def test_explicit_protocol_shell_and_chat_message_correction_are_reversible() -> None:
    hybrid_vector = _vectors()[4]
    hybrid_codec = _codec(hybrid_vector)
    hybrid_request = hybrid_vector["application_request"]
    hybrid_ir = hybrid_codec.extract(hybrid_request)
    action_record = next(
        record for record in hybrid_ir.records if record.record_key == "collapsed_action_step_1"
    )
    base_mask = build_fixture_plan(
        ir=hybrid_ir, record_key=action_record.record_key, arm=ArmKind.MASK
    )
    shell_span = next(
        span
        for span in action_record.protected_spans
        if span.span_role is SpanRole.ELIGIBLE_PROTOCOL_SHELL
    )
    shell = PlanOperation(
        operation_id="fixture-drop-empty-shell",
        kind=OperationKind.DROP,
        target_record_id=action_record.record_id,
        target_span=shell_span,
        protocol_shell_for=(base_mask.operations[0].operation_id,),
    )
    shell_plan = replace(base_mask, operations=(shell, *base_mask.operations))
    shell_result = hybrid_codec.render(
        hybrid_request,
        hybrid_ir,
        shell_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert any(diff.mapping_kind is MappingKind.SYNTAX_REPAIR for diff in shell_result.diffs)
    assert restore_original(shell_result) == hybrid_request

    raw_vector = _vectors()[0]
    raw_codec = _codec(raw_vector)
    raw_request = raw_vector["application_request"]
    raw_ir = raw_codec.extract(raw_request)
    raw_record = raw_ir.records[0]
    current_region = next(
        region for region in raw_ir.regions if region.kind is RegionKind.CURRENT_OBSERVATION
    )
    chat_anchor = CorrectionAnchor(
        container_path=("messages",),
        insert_index=4,
        source_container_sha256=canonical_sha256(raw_request["messages"]),
        owner_region_id=current_region.region_id,
        host_context_path=("messages", 4),
        host_context_sha256=canonical_sha256(raw_request["messages"][4]),
        role_path=("messages", 4, "role"),
        expected_role="user",
        reference_path=("messages", 4),
        reference_sha256=canonical_sha256(raw_request["messages"][4]),
        placement=CorrectionPlacement.BEFORE,
        context_kind=CorrectionContextKind.CHAT_MESSAGE,
        visible_prefix="[SENTINEL CONTEXT]\n",
        visible_suffix="",
    )
    chat_ir = replace(
        raw_ir,
        records=(replace(raw_record, correction_anchors=(chat_anchor,)),),
    )
    chat_plan = build_fixture_plan(
        ir=chat_ir,
        record_key=raw_record.record_key,
        arm=ArmKind.MASK_CORRECTION,
        correction_text=raw_vector["correction_text"],
    )
    chat_result = raw_codec.render(
        raw_request,
        chat_ir,
        chat_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert chat_result.rendered_request["messages"][4]["role"] == "user"
    assert "SENTINEL CONTEXT" in chat_result.rendered_request["messages"][4]["content"]
    assert restore_original(chat_result) == raw_request


def test_protocol_shell_requires_the_same_plan_to_empty_its_record() -> None:
    vector = deepcopy(_vectors()[4])
    action_mapping = next(
        record
        for record in vector["mapping"]["records"]
        if record["record_key"] == "collapsed_action_step_1"
    )
    action_mapping["editable_spans"] = [
        {
            "container_path": ["messages", 1, "content", 0, "text"],
            "char_start": 31,
            "exact_text": "9",
            "span_role": "EDITABLE_CLAIM",
            "claim_key": "partial-coordinate-only",
        }
    ]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    record = next(item for item in ir.records if item.record_key == "collapsed_action_step_1")
    mask = build_fixture_plan(ir=ir, record_key=record.record_key, arm=ArmKind.MASK)
    shell_span = next(
        span
        for span in record.protected_spans
        if span.span_role is SpanRole.ELIGIBLE_PROTOCOL_SHELL
    )
    shell = PlanOperation(
        operation_id="forged-partial-target-shell",
        kind=OperationKind.DROP,
        target_record_id=record.record_id,
        target_span=shell_span,
        protocol_shell_for=(mask.operations[0].operation_id,),
    )
    with pytest.raises(PortableContractError, match="PROTOCOL_SHELL_NOT_CAUSALLY_EMPTY"):
        codec.render(
            request,
            ir,
            replace(mask, operations=(shell, *mask.operations)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_protocol_shell_repair_cannot_attach_to_another_history_record() -> None:
    vector = _vectors()[1]
    codec = _codec(vector)
    request = vector["application_request"]
    ir = codec.extract(request)
    first, second = ir.records
    shell_source = first.protected_spans[0]
    shell_span = replace(shell_source, span_role=SpanRole.ELIGIBLE_PROTOCOL_SHELL)
    modified_ir = replace(
        ir,
        records=(
            replace(
                first,
                protected_spans=(shell_span, *first.protected_spans[1:]),
            ),
            second,
        ),
    )
    target_plan = build_fixture_plan(
        ir=modified_ir,
        record_key=second.record_key,
        arm=ArmKind.MASK,
    )
    shell_operation = PlanOperation(
        operation_id="cross-record-shell",
        kind=OperationKind.DROP,
        target_record_id=first.record_id,
        target_span=shell_span,
        protocol_shell_for=(target_plan.operations[0].operation_id,),
    )
    with pytest.raises(PortableContractError, match="CROSS_RECORD_SHELL_REPAIR"):
        render_request(
            request,
            modified_ir,
            replace(target_plan, operations=(shell_operation, *target_plan.operations)),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )


def test_provider_authorization_hashes_actual_bytes_endpoint_and_parameters() -> None:
    vector, codec, fixture_ir, fixture_plans = _clean_fixture(0)
    request = vector["application_request"]
    live_capabilities = replace(
        codec.capabilities,
        scope=CodecScope.LIVE,
        live_ready=True,
    )
    ir = replace(fixture_ir, capabilities=live_capabilities)
    live_codec = _declaration(codec, ir, live_capabilities)
    plan = build_fixture_plan(ir=ir, record_key=vector["target_record_key"], arm=ArmKind.ORIGINAL)
    paired_plans = tuple(
        build_fixture_plan(
            ir=ir,
            record_key=vector["target_record_key"],
            arm=item.arm,
        )
        for item in fixture_plans
    )
    with pytest.raises(PortableContractError, match="INCOMPLETE_PLAN_SET"):
        validate_plan_set(
            request,
            ir,
            (plan,),
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
    plan_set_sha256 = validate_plan_set(
        request,
        ir,
        paired_plans,
        codec_registry=_registry(live_codec),
        codec_contract_version=live_codec.contract_version,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    result = render_request(
        request,
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    parameters = {"temperature": 0.0}
    provider_registry, provider = _provider_registry()
    receipt = validate_pre_send(
        request,
        ir,
        plan,
        result,
        codec_registry=_registry(live_codec),
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
        intended_provider_codec_id=provider.codec_id,
        intended_provider_contract_version="v1",
        intended_endpoint_revision="endpoint-v1",
        model_parameters=parameters,
    )
    assert receipt.plan_set_sha256 == plan_set_sha256
    assert receipt.provider_decision is ProviderDecision.ALLOW
    with pytest.raises(
        PortableContractError, match="RENDER_RECEIPT_MISMATCH|RENDER_CAPABILITY_MISMATCH"
    ):
        validate_pre_send(
            request,
            ir,
            plan,
            replace(result, capability_sha256="0" * 64),
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
            intended_provider_codec_id=provider.codec_id,
            intended_provider_contract_version="v1",
            intended_endpoint_revision="endpoint-v1",
            model_parameters=parameters,
        )
    forged_count = replace(result, count_as_treatment=1)
    with pytest.raises(PortableContractError, match="INVALID_BOOLEAN"):
        validate_pre_send(
            request,
            ir,
            plan,
            forged_count,
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
            intended_provider_codec_id=provider.codec_id,
            intended_provider_contract_version="v1",
            intended_endpoint_revision="endpoint-v1",
            model_parameters=parameters,
        )
    invalid_prepared = PreparedProviderRequest(
        provider_codec_id=provider.codec_id,
        provider_contract_version="v1",
        endpoint_revision="endpoint-v1",
        application_request_sha256=canonical_sha256(request),
        encoded_request_sha256="2" * 64,
        encoded_request=b"actual bytes",
        model_parameters=parameters,
        model_parameters_sha256=canonical_sha256(parameters),
    )
    with pytest.raises(PortableContractError, match="ENCODED_REQUEST_HASH_MISMATCH"):
        authorize_prepared_request(
            invalid_prepared,
            receipt,
            ir=ir,
            plan=plan,
            render_result=result,
            codec_registry=_registry(live_codec),
            provider_registry=provider_registry,
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        )

    prepared = replace(
        invalid_prepared,
        encoded_request_sha256=text_sha256("actual bytes"),
    )
    with pytest.raises(PortableContractError, match="UNKNOWN_PROVIDER_CODEC"):
        authorize_prepared_request(
            prepared,
            receipt,
            ir=ir,
            plan=plan,
            render_result=result,
            codec_registry=_registry(live_codec),
            provider_registry=ProviderCodecRegistry(),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        )
    authorized = authorize_prepared_request(
        prepared,
        receipt,
        ir=ir,
        plan=plan,
        render_result=result,
        codec_registry=_registry(live_codec),
        provider_registry=provider_registry,
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
    )
    for field, value in (
        ("valid", 1),
        ("provider_invocation_allowed", 1),
        ("invocation_attempted", 0),
    ):
        with pytest.raises(PortableContractError, match="INVALID_VALIDATION_RECEIPT_BOOLEAN"):
            authorize_prepared_request(
                prepared,
                replace(receipt, **{field: value}),
                ir=ir,
                plan=plan,
                render_result=result,
                codec_registry=_registry(live_codec),
                provider_registry=provider_registry,
                codec_contract_version=live_codec.contract_version,
                paired_plans=paired_plans,
                plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            )
    parameters["temperature"] = 0.75
    assert authorized.prepared.model_parameters == {"temperature": 0.0}
    assert canonical_sha256(authorized.prepared.model_parameters) == (
        authorized.prepared.model_parameters_sha256
    )
    exposed_parameters = authorized.prepared.model_parameters
    exposed_parameters["temperature"] = 9.0
    assert authorized.prepared.model_parameters == {"temperature": 0.0}
    parameters["temperature"] = 0.0
    with pytest.raises(PortableContractError, match="VALIDATION_RECEIPT_MISMATCH"):
        authorize_prepared_request(
            prepared,
            replace(receipt, plan_set_sha256="0" * 64),
            ir=ir,
            plan=plan,
            render_result=result,
            codec_registry=_registry(live_codec),
            provider_registry=provider_registry,
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        )
    response_sha = "c" * 64
    action = {"action_type": "click", "x": 12, "y": 34}
    provider_result = ProviderResult(
        provider_codec_id=prepared.provider_codec_id,
        provider_contract_version=prepared.provider_contract_version,
        endpoint_revision=prepared.endpoint_revision,
        status=ProviderResultStatus.RETURNED,
        application_request_sha256=prepared.application_request_sha256,
        encoded_request_sha256=prepared.encoded_request_sha256,
        response_sha256=response_sha,
        raw_response_ref={
            "sha256": response_sha,
            "byte_count": 42,
            "media_type": "application/json",
            "schema_version": None,
            "relative_path": "blobs/sha256/cc/fixture-response",
        },
        normalized_action=action,
        normalized_action_sha256=canonical_sha256(action),
        error=None,
        model_parameters=parameters,
        model_parameters_sha256=canonical_sha256(parameters),
    )
    sidecar = build_sidecar(
        ir=ir,
        plan=plan,
        render_result=result,
        validation_receipt=receipt,
        codec_registry=_registry(live_codec),
        codec_contract_version=live_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
        provider_registry=provider_registry,
        authorized_request=authorized,
        provider_result=provider_result,
    )
    _validator("sidecar.schema.json").validate(sidecar.to_dict())
    assert sidecar.validation_receipt.invocation_attempted is False
    assert sidecar.provider_attempt.invocation_attempted is True
    with pytest.raises(PortableContractError, match="INVALID_VALIDATION_RECEIPT_BOOLEAN"):
        build_sidecar(
            ir=ir,
            plan=plan,
            render_result=result,
            validation_receipt=replace(receipt, valid=1),
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            provider_registry=provider_registry,
            authorized_request=authorized,
            provider_result=provider_result,
        )
    with pytest.raises(PortableContractError, match="SIDECAR_VALIDATION_RECEIPT_MISMATCH"):
        build_sidecar(
            ir=ir,
            plan=plan,
            render_result=result,
            validation_receipt=replace(receipt, source_request_sha256="0" * 64),
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            provider_registry=provider_registry,
            authorized_request=authorized,
            provider_result=provider_result,
        )
    blocked_payload = sidecar.to_dict()
    blocked_payload["fallback"]["state"] = "BLOCKED_BEFORE_PROVIDER"
    blocked_payload["execution"]["effective_arm"] = None
    blocked_payload["execution"]["unsupported_reason"] = "fixture-forgery"
    assert list(_validator("sidecar.schema.json").iter_errors(blocked_payload))
    blocked_receipt_payload = sidecar.to_dict()
    blocked_receipt_payload["validation_receipt"]["provider_decision"] = "BLOCK"
    blocked_receipt_payload["validation_receipt"]["provider_invocation_allowed"] = False
    blocked_receipt_payload["provider_attempt"]["invocation_attempted"] = False
    blocked_receipt_payload["provider_result"] = None
    assert list(_validator("sidecar.schema.json").iter_errors(blocked_receipt_payload))
    bypass_payload = sidecar.to_dict()
    bypass_payload["fallback"]["state"] = "EXPLICIT_ORIGINAL"
    bypass_payload["execution"]["effective_arm"] = "ORIGINAL"
    bypass_payload["execution"]["execution_mode"] = "RUNTIME"
    bypass_payload["execution"]["failure_policy"] = "FAIL_OPEN_ORIGINAL"
    bypass_payload["execution"]["unsupported_reason"] = "fixture-forgery"
    assert list(_validator("sidecar.schema.json").iter_errors(bypass_payload))
    with pytest.raises(PortableContractError, match="PROVIDER_RESULT_BINDING_MISMATCH"):
        build_sidecar(
            ir=ir,
            plan=plan,
            render_result=result,
            validation_receipt=receipt,
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            provider_registry=provider_registry,
            authorized_request=authorized,
            provider_result=replace(provider_result, endpoint_revision="other-endpoint"),
        )
    with pytest.raises(PortableContractError, match="PROVIDER_RESULT_BINDING_MISMATCH"):
        build_sidecar(
            ir=ir,
            plan=plan,
            render_result=result,
            validation_receipt=receipt,
            codec_registry=_registry(live_codec),
            codec_contract_version=live_codec.contract_version,
            paired_plans=paired_plans,
            plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
            provider_registry=provider_registry,
            authorized_request=authorized,
            provider_result=replace(provider_result, provider_contract_version="v2"),
        )
