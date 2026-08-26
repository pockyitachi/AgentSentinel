"""Reusable fixture-level conformance kit for portable History Codecs."""

from __future__ import annotations

from dataclasses import dataclass, replace

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CodecCapabilities,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    HistoryFamily,
    HistoryIR,
    JsonValue,
    OperationKind,
    PlanOperation,
    PlanSetProfile,
    PortableContractError,
    RenderResult,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    stable_id,
    text_sha256,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    restore_original,
    validate_plan_set,
    validate_pre_send,
    with_capability,
)
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.causal_replay.registry import HistoryCodecRegistry

CONFORMANCE_SCHEMA_VERSION = "mobileworld.g1.portable-sentinel.conformance-report/v1"


@dataclass(frozen=True)
class _CodecDeclaration:
    codec_id: str
    contract_version: str
    history_family: HistoryFamily
    capabilities: CodecCapabilities
    frozen_ir: HistoryIR

    def extract(self, application_request: JsonValue) -> HistoryIR:
        if canonical_sha256(application_request) != self.frozen_ir.raw_request_sha256:
            raise PortableContractError(
                "REQUEST_HASH_DRIFT", "declaration fixture binds another request"
            )
        return self.frozen_ir

    def render(
        self,
        application_request: JsonValue,
        ir: HistoryIR,
        plan: TransformationPlan,
        *,
        execution_mode: ExecutionMode,
        failure_policy: FailurePolicy,
    ) -> RenderResult:
        return render_request(
            application_request,
            ir,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )


def materialize_fixture_mapping(vector: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Expand explicit fixture starts/text into complete frozen coordinate records.

    This helper never searches for text.  Every vector supplies the exact container
    path and character start; a single-byte drift fails closed.
    """

    request = copy_json(vector.get("application_request"))
    mapping = copy_json(vector.get("mapping"))
    if not isinstance(mapping, dict):
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "mapping must be an object")
    if "capabilities" not in mapping:
        capabilities = copy_json(vector.get("capabilities"))
        if not isinstance(capabilities, dict):
            raise PortableContractError(
                "INVALID_CONFORMANCE_VECTOR", "capabilities must be an object"
            )
        mapping["capabilities"] = capabilities
    regions = mapping.get("regions")
    if not isinstance(regions, list):
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "regions must be an array")
    for raw_region in regions:
        if not isinstance(raw_region, dict):
            raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "region must be an object")
        slices = raw_region.get("text_slices")
        if not isinstance(slices, list):
            raise PortableContractError(
                "INVALID_CONFORMANCE_VECTOR", "region text_slices must be an array"
            )
        for raw_slice in slices:
            if not isinstance(raw_slice, dict):
                raise PortableContractError(
                    "INVALID_CONFORMANCE_VECTOR", "region text slice must be an object"
                )
            path = _vector_path(raw_slice.get("container_path"))
            container = get_at_path(request, path)
            if not isinstance(container, str):
                raise PortableContractError(
                    "SPAN_CONTAINER_NOT_TEXT", "fixture region slice is not text"
                )
            _freeze_template_span(raw_slice, prefix="", container=container)
    records = mapping.get("records")
    if not isinstance(records, list):
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "records must be an array")
    for raw_record in records:
        if not isinstance(raw_record, dict):
            raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "record must be an object")
        path = _vector_path(raw_record.get("container_path"))
        container = get_at_path(request, path)
        if not isinstance(container, str):
            raise PortableContractError("SPAN_CONTAINER_NOT_TEXT", "fixture record is not text")
        _freeze_template_span(raw_record, prefix="source_", container=container)
        for key in ("editable_spans", "protected_spans"):
            spans = raw_record.get(key)
            if not isinstance(spans, list):
                raise PortableContractError("INVALID_CONFORMANCE_VECTOR", f"{key} must be an array")
            for raw_span in spans:
                if not isinstance(raw_span, dict):
                    raise PortableContractError(
                        "INVALID_CONFORMANCE_VECTOR", "span must be an object"
                    )
                if raw_span.get("container_path") != list(path):
                    raise PortableContractError(
                        "INVALID_CONFORMANCE_VECTOR", "child span must repeat the exact record path"
                    )
                _freeze_template_span(raw_span, prefix="", container=container)
    return mapping


def _freeze_template_span(span: dict[str, JsonValue], *, prefix: str, container: str) -> None:
    start_key = f"{prefix}char_start"
    text_key = f"{prefix}exact_text"
    start = span.get(start_key)
    text = span.get(text_key)
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", f"{start_key} is invalid")
    if not isinstance(text, str) or not text:
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", f"{text_key} is invalid")
    end = start + len(text)
    if container[start:end] != text:
        raise PortableContractError(
            "FIXTURE_SPAN_DRIFT", "fixture exact text is not present at the declared start"
        )
    span[f"{prefix}char_end"] = end
    span[f"{prefix}utf8_byte_start"] = len(container[:start].encode("utf-8"))
    span[f"{prefix}utf8_byte_end"] = len(container[:end].encode("utf-8"))
    span[f"{prefix}span_sha256"] = text_sha256(text)


def _vector_path(raw: JsonValue) -> tuple[str | int, ...]:
    if not isinstance(raw, list) or not raw:
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "path must be non-empty")
    if any(
        isinstance(token, bool) or not isinstance(token, (str, int)) or token == "" for token in raw
    ):
        raise PortableContractError("INVALID_CONFORMANCE_VECTOR", "path token is invalid")
    return tuple(
        token for token in raw if isinstance(token, (str, int)) and not isinstance(token, bool)
    )


def build_fixture_plan(
    *,
    ir: HistoryIR,
    record_key: str,
    arm: ArmKind,
    correction_text: str | None = None,
) -> TransformationPlan:
    """Build an explicitly curated synthetic plan for a checked-in fixture target."""

    records = [record for record in ir.records if record.record_key == record_key]
    if len(records) != 1:
        raise PortableContractError(
            "CONFORMANCE_TARGET_INVALID", "fixture record key must resolve exactly once"
        )
    record = records[0]
    if arm is ArmKind.ORIGINAL:
        operations: tuple[PlanOperation, ...] = ()
    else:
        expected_role = (
            SpanRole.BENIGN_SHAM if arm is ArmKind.SHAM_BENIGN_EDIT else SpanRole.EDITABLE_CLAIM
        )
        targets = [span for span in record.editable_spans if span.span_role is expected_role]
        if not targets:
            raise PortableContractError(
                "CONFORMANCE_TARGET_INVALID", "fixture has no compatible editable span"
            )
        target = targets[0]
        if arm is ArmKind.MASK_CORRECTION:
            if not record.correction_anchors:
                raise PortableContractError(
                    "CONFORMANCE_TARGET_INVALID", "fixture target has no correction anchor"
                )
            anchor = record.correction_anchors[0]
            if correction_text is None:
                raise PortableContractError(
                    "CONFORMANCE_TARGET_INVALID", "fixture correction text is missing"
                )
            visible_text = f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}"
            if anchor.context_kind.value == "TEXT_CONTENT_BLOCK":
                rendered_context: JsonValue = {"type": "text", "text": visible_text}
            else:
                rendered_context = {"role": "user", "content": visible_text}
            evidence = (
                EvidenceRef(
                    evidence_id="fixture-evidence-pre-cutoff",
                    sha256="a" * 64,
                    role="current_gui_pre_cutoff",
                    event_seq=1,
                ),
            )
            operation = PlanOperation(
                operation_id="fixture-replace-target",
                kind=OperationKind.REPLACE,
                target_record_id=record.record_id,
                target_span=target,
                replacement_text=correction_text,
                replacement_author="SENTINEL",
                evidence_refs=evidence,
                correction_anchor=anchor,
                rendered_correction_context=rendered_context,
            )
        else:
            operation = PlanOperation(
                operation_id="fixture-drop-target",
                kind=OperationKind.DROP,
                target_record_id=record.record_id,
                target_span=target,
            )
        operations = (operation,)
    payload: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": arm.value,
        "operations": [operation.to_dict() for operation in operations],
    }
    return TransformationPlan(
        plan_id=stable_id("plan", payload),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=arm,
        operations=operations,
        curated=True,
        deployment_prediction=False,
    )


def run_fixture_conformance(
    *,
    vector_id: str,
    codec: HistoryCodec,
    application_request: JsonValue,
    target_record_key: str,
    correction_text: str,
) -> dict[str, JsonValue]:
    """Run the common six-family CPU-only conformance assertions."""

    source_hash = canonical_sha256(application_request)
    codec_registry = HistoryCodecRegistry()
    codec_registry.register(codec)
    ir = codec.extract(application_request)
    second_ir = codec.extract(application_request)
    if canonical_sha256(ir.to_dict()) != canonical_sha256(second_ir.to_dict()):
        raise PortableContractError("NON_DETERMINISTIC_EXTRACTION", "IR extraction changed")

    original_plan = build_fixture_plan(ir=ir, record_key=target_record_key, arm=ArmKind.ORIGINAL)
    mask_plan = build_fixture_plan(ir=ir, record_key=target_record_key, arm=ArmKind.MASK)
    correction_plan = build_fixture_plan(
        ir=ir,
        record_key=target_record_key,
        arm=ArmKind.MASK_CORRECTION,
        correction_text=correction_text,
    )
    oracle_plan = build_fixture_plan(ir=ir, record_key=target_record_key, arm=ArmKind.ORACLE_CLEAN)
    ir_snapshot = canonical_sha256(ir.to_dict())
    plan_snapshots = {
        plan.arm: canonical_sha256(plan.to_dict())
        for plan in (original_plan, mask_plan, correction_plan, oracle_plan)
    }
    paired_plans = (original_plan, mask_plan, correction_plan, oracle_plan)
    plan_set_sha256 = validate_plan_set(
        application_request,
        ir,
        paired_plans,
        codec_registry=codec_registry,
        codec_contract_version=codec.contract_version,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    original = codec.render(
        application_request,
        ir,
        original_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    original_receipt = validate_pre_send(
        application_request,
        ir,
        original_plan,
        original,
        codec_registry=codec_registry,
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    if original.rendered_request != application_request:
        raise PortableContractError(
            "ORIGINAL_NOT_IDENTICAL", "Original changed application request"
        )

    masked = codec.render(
        application_request,
        ir,
        mask_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    mask_receipt = validate_pre_send(
        application_request,
        ir,
        mask_plan,
        masked,
        codec_registry=codec_registry,
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    corrected = codec.render(
        application_request,
        ir,
        correction_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    correction_receipt = validate_pre_send(
        application_request,
        ir,
        correction_plan,
        corrected,
        codec_registry=codec_registry,
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    if restore_original(corrected) != application_request:
        raise PortableContractError(
            "NON_REVERSIBLE_MAPPING", "correction mapping cannot restore source"
        )

    oracle = codec.render(
        application_request,
        ir,
        oracle_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    oracle_receipt = validate_pre_send(
        application_request,
        ir,
        oracle_plan,
        oracle,
        codec_registry=codec_registry,
        codec_contract_version=codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    if any(
        receipt.plan_set_sha256 != plan_set_sha256
        for receipt in (
            original_receipt,
            mask_receipt,
            correction_receipt,
            oracle_receipt,
        )
    ):
        raise PortableContractError(
            "PLAN_SET_RECEIPT_MISMATCH", "per-arm receipts bind another paired plan set"
        )

    unsupported_ir = with_capability(ir, supported_arms=(ArmKind.ORIGINAL,))
    unsupported_codec = _CodecDeclaration(
        codec_id=codec.codec_id,
        contract_version=codec.contract_version,
        history_family=codec.history_family,
        capabilities=unsupported_ir.capabilities,
        frozen_ir=unsupported_ir,
    )
    unsupported_registry = HistoryCodecRegistry()
    unsupported_registry.register_factory(
        history_family=unsupported_codec.history_family,
        contract_version=unsupported_codec.contract_version,
        codec_id=unsupported_codec.codec_id,
        factory=lambda: unsupported_codec,
    )
    fail_open = render_request(
        application_request,
        unsupported_ir,
        mask_plan,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    fail_open_receipt = validate_pre_send(
        application_request,
        unsupported_ir,
        mask_plan,
        fail_open,
        codec_registry=unsupported_registry,
        codec_contract_version=unsupported_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    if fail_open.rendered_request != application_request or fail_open.count_as_treatment:
        raise PortableContractError(
            "INVALID_FAIL_OPEN", "runtime fallback is not pristine Original"
        )
    blocked = render_request(
        application_request,
        unsupported_ir,
        mask_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    blocked_receipt = validate_pre_send(
        application_request,
        unsupported_ir,
        mask_plan,
        blocked,
        codec_registry=unsupported_registry,
        codec_contract_version=unsupported_codec.contract_version,
        paired_plans=paired_plans,
        plan_set_profile=PlanSetProfile.PORTABLE_CORE,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    if (
        blocked.fallback_state.value != "BLOCKED_BEFORE_PROVIDER"
        or blocked_receipt.provider_invocation_allowed
    ):
        raise PortableContractError(
            "G1_UNSUPPORTED_DID_NOT_BLOCK", "unsupported scientific treatment was not blocked"
        )

    for plan, result in (
        (original_plan, original),
        (mask_plan, masked),
        (correction_plan, corrected),
        (oracle_plan, oracle),
    ):
        repeated = codec.render(
            application_request,
            ir,
            plan,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        if result.to_dict() != repeated.to_dict():
            raise PortableContractError(
                "NON_DETERMINISTIC_RENDER", f"{plan.arm.value} rendering changed"
            )
        if restore_original(result) != application_request:
            raise PortableContractError(
                "NON_REVERSIBLE_MAPPING", f"{plan.arm.value} mapping cannot restore source"
            )
    repeated_fail_open = render_request(
        application_request,
        unsupported_ir,
        mask_plan,
        execution_mode=ExecutionMode.RUNTIME,
        failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
    )
    repeated_blocked = render_request(
        application_request,
        unsupported_ir,
        mask_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    if (
        fail_open.to_dict() != repeated_fail_open.to_dict()
        or blocked.to_dict() != repeated_blocked.to_dict()
    ):
        raise PortableContractError(
            "NON_DETERMINISTIC_RENDER", "unsupported/fallback rendering changed"
        )

    if canonical_sha256(application_request) != source_hash:
        raise PortableContractError("CALLER_INPUT_MUTATED", "conformance mutated fixture input")
    if canonical_sha256(ir.to_dict()) != ir_snapshot or any(
        canonical_sha256(plan.to_dict()) != plan_snapshots[plan.arm]
        for plan in (original_plan, mask_plan, correction_plan, oracle_plan)
    ):
        raise PortableContractError(
            "CONTRACT_INPUT_MUTATED", "rendering mutated the IR or curated plans"
        )
    return {
        "schema_version": CONFORMANCE_SCHEMA_VERSION,
        "vector_id": vector_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "fixture_only": ir.capabilities.scope.value == "FIXTURE_ONLY",
        "production_ready": ir.capabilities.live_ready,
        "source_request_sha256": source_hash,
        "history_ir_sha256": canonical_sha256(ir.to_dict()),
        "checks": {
            "deterministic_extraction": True,
            "original_identity": original_receipt.valid,
            "target_only_mask": mask_receipt.valid,
            "sentinel_correction": correction_receipt.valid,
            "oracle_clean": oracle_receipt.valid,
            "raw_input_immutable": True,
            "reversible_mapping": True,
            "runtime_explicit_fail_open": fail_open_receipt.valid,
            "g1_unsupported_fail_closed": True,
            "blocked_artifact_recorded": True,
        },
        "render_hashes": {
            "original": original.rendered_request_sha256,
            "mask": masked.rendered_request_sha256,
            "mask_correction": corrected.rendered_request_sha256,
            "oracle_clean": oracle.rendered_request_sha256,
            "runtime_fallback": fail_open.rendered_request_sha256,
        },
    }


def audit_only_ir(ir: HistoryIR) -> HistoryIR:
    """Return a narrowed fixture IR for capability-negative tests."""

    return replace(
        ir,
        capabilities=replace(
            ir.capabilities,
            supported_operations=(OperationKind.KEEP,),
            supported_arms=(ArmKind.ORIGINAL,),
        ),
    )
