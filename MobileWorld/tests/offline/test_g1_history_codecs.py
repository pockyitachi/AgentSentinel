from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    JsonPath,
    JsonValue,
    OperationKind,
    PlanOperation,
    PortableContractError,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    set_at_path,
    stable_id,
)
from mobile_world.offline.causal_replay.core import render_request, restore_original, validate_plan
from mobile_world.offline.causal_replay.registry import (
    HistoryCodecRegistry,
    ProviderCodecRegistry,
)
from mobile_world.offline.causal_replay_runner import (
    DeterministicFakeProviderCodec,
    ExecutionDomain,
    FakeScenario,
    LoadedReplayCapsule,
    ReplayRunnerError,
    UnitKind,
    execute_live_arm,
    preflight_block,
    schedule_for_unit,
)
from mobile_world.offline.g1_history_codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
    render_human_diff,
    run_history_codec_cpu_checkpoint,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = Path(__file__).parent / "fixtures/g1_5_history_codecs"
SCHEMA_ROOT = REPO_ROOT / "mobileworld_audit_handoff/schemas/g1_5"
PUBLICATION_ROOT = REPO_ROOT / "mobileworld_audit_handoff/g1_5"
STRICT_UNIT_ID = "g1case-111111111111111111111111"
ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64


@dataclass(frozen=True)
class CodecCase:
    fixture_name: str
    codec_type: type[QwenFlatProgressHistoryCodec] | type[MaiRawReplayHistoryCodec]
    model_id: str


CASES = (
    CodecCase(
        fixture_name="qwen_flat_progress.captured.v1.json",
        codec_type=QwenFlatProgressHistoryCodec,
        model_id="qwen3vl_8b",
    ),
    CodecCase(
        fixture_name="mai_raw_replay.captured.v1.json",
        codec_type=MaiRawReplayHistoryCodec,
        model_id="mai_ui_8b",
    ),
)


def _load(case: CodecCase) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURE_ROOT / case.fixture_name).read_text(encoding="utf-8")),
    )


def _bindings(data: dict[str, Any]) -> tuple[CuratedSpanBinding, ...]:
    return tuple(
        CuratedSpanBinding(
            binding_id=item["binding_id"],
            source_request_sha256=item["source_request_sha256"],
            container_path=tuple(item["container_path"]),
            char_start=item["char_start"],
            char_end=item["char_end"],
            utf8_byte_start=item["utf8_byte_start"],
            utf8_byte_end=item["utf8_byte_end"],
            exact_text=item["exact_text"],
            span_sha256=item["span_sha256"],
            span_role=SpanRole(item["span_role"]),
        )
        for item in data["curated_span_bindings"]
    )


def _codec(
    case: CodecCase, data: dict[str, Any], bindings: tuple[CuratedSpanBinding, ...] | None = None
) -> QwenFlatProgressHistoryCodec | MaiRawReplayHistoryCodec:
    return case.codec_type(_bindings(data) if bindings is None else bindings)


def _targets(ir: Any) -> dict[str, tuple[Any, Any]]:
    found: dict[str, tuple[Any, Any]] = {}
    for record in ir.records:
        raw_ids = record.provenance.get("curated_binding_ids")
        assert isinstance(raw_ids, list)
        ids = [str(item) for item in raw_ids]
        assert len(ids) == len(record.editable_spans)
        for binding_id, span in zip(ids, record.editable_spans, strict=True):
            found[binding_id] = (record, span)
    return found


def _operation_key(operation: PlanOperation) -> tuple[object, ...]:
    path_key = tuple(
        (0, token) if isinstance(token, str) else (1, token)
        for token in operation.target_span.container_path
    )
    span = operation.target_span
    return (
        path_key,
        span.char_start,
        span.char_end,
        span.span_sha256,
        operation.target_record_id,
        operation.operation_id,
    )


def _make_plan(
    *,
    ir: Any,
    arm: ArmKind,
    binding_ids: list[str],
    correction_text: str,
) -> TransformationPlan:
    by_binding = _targets(ir)
    operations: list[PlanOperation] = []
    for index, binding_id in enumerate(binding_ids):
        record, span = by_binding[binding_id]
        operation_id = f"g15-{arm.value.lower()}-{index:02d}-{binding_id}"
        if arm is ArmKind.MASK_CORRECTION:
            anchor = record.correction_anchors[0]
            rendered_context: JsonValue = {
                "type": "text",
                "text": f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}",
            }
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.REPLACE,
                target_record_id=record.record_id,
                target_span=span,
                replacement_text=correction_text,
                replacement_author="SENTINEL",
                evidence_refs=(
                    EvidenceRef(
                        evidence_id="g15-secret-free-pre-cutoff-evidence",
                        sha256="e" * 64,
                        role="current_observation_pre_cutoff",
                        event_seq=7,
                    ),
                ),
                correction_anchor=anchor,
                rendered_correction_context=rendered_context,
            )
        else:
            operation = PlanOperation(
                operation_id=operation_id,
                kind=OperationKind.DROP,
                target_record_id=record.record_id,
                target_span=span,
            )
        operations.append(operation)
    operations.sort(key=_operation_key)
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": arm.value,
        "operations": [item.to_dict() for item in operations],
    }
    return TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=arm,
        operations=tuple(operations),
        curated=True,
        deployment_prediction=False,
    )


def _plans(data: dict[str, Any], ir: Any) -> tuple[TransformationPlan, ...]:
    targets = data["plan_targets"]
    correction = data["correction_text"]
    return (
        _make_plan(ir=ir, arm=ArmKind.ORIGINAL, binding_ids=[], correction_text=correction),
        _make_plan(
            ir=ir,
            arm=ArmKind.MASK,
            binding_ids=targets["mask"],
            correction_text=correction,
        ),
        _make_plan(
            ir=ir,
            arm=ArmKind.MASK_CORRECTION,
            binding_ids=targets["mask_correction"],
            correction_text=correction,
        ),
        _make_plan(
            ir=ir,
            arm=ArmKind.ORACLE_CLEAN,
            binding_ids=targets["oracle_clean"],
            correction_text=correction,
        ),
        _make_plan(
            ir=ir,
            arm=ArmKind.SHAM_BENIGN_EDIT,
            binding_ids=targets["sham_benign_edit"],
            correction_text=correction,
        ),
    )


def _replace_projection_spans(
    request: JsonValue, spans: tuple[Any, ...]
) -> tuple[JsonValue, tuple[dict[str, JsonValue], ...]]:
    projection = copy_json(request)
    by_path: dict[JsonPath, list[tuple[int, int]]] = {}
    bindings: list[JsonValue] = []
    for span in spans:
        source_value = get_at_path(request, span.container_path)
        assert isinstance(source_value, str)
        by_path.setdefault(span.container_path, []).append((span.char_start, span.char_end))
        bindings.append(
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
        )
    for path, path_spans in by_path.items():
        value = get_at_path(projection, path)
        assert isinstance(value, str)
        for start, end in sorted(path_spans, reverse=True):
            value = value[:start] + "<MUTABLE_HISTORY_TREATMENT>" + value[end:]
        set_at_path(projection, path, value)
    region: dict[str, JsonValue] = {
        "region_kind": "HISTORY",
        "ownership_role": "OWNER",
        "bindings": bindings,
    }
    return projection, (region,)


def _capsule(case: CodecCase, data: dict[str, Any], ir: Any) -> LoadedReplayCapsule:
    spans = tuple(span for record in ir.records for span in record.editable_spans)
    projection, regions = _replace_projection_spans(data["application_request"], spans)
    request = copy_json(data["application_request"])
    request_sha = canonical_sha256(request)
    body_sha = canonical_sha256(
        {"fixture_id": data["fixture_id"], "semantic_request_sha256": request_sha}
    )
    root = cast(dict[str, JsonValue], request)
    return LoadedReplayCapsule(
        publication_manifest_sha256=ZERO_SHA,
        capsule_file_sha256=ONE_SHA,
        capsule_body_sha256=body_sha,
        capsule_id=f"g1capsule-{body_sha[:24]}",
        unit_kind=UnitKind.STRICT_MHR,
        unit_id=STRICT_UNIT_ID,
        model_id=case.model_id,
        history_family=data["history_family"],
        semantic_request=request,
        semantic_request_sha256=request_sha,
        region_partition=regions,
        non_history_projection_sha256=canonical_sha256(projection),
        treatment_surface={"fixture_only": True, "g1_5_cpu_checkpoint": True},
        replay_binding={
            "model": {"model_id": case.model_id, "fixture_only": True},
            "provider": {"transport": "fake-conformance-only"},
        },
        restore_descriptor={
            "mode": "SERIALIZED_REQUEST_ONLY",
            "external_state_consulted": False,
            "checkpoint_required": False,
        },
        parser_descriptor={"binding_id": "g15-fixture-parser/v1"},
        decoding_configuration=cast(
            dict[str, JsonValue],
            copy_json({key: value for key, value in root.items() if key != "messages"}),
        ),
        source_safety={
            "execution_ready": False,
            "provider_invocation_allowed": False,
            "treatment_response_generation_allowed": False,
            "provider_invoked": False,
            "treatment_response_count": 0,
        },
    )


def _data_urls(value: JsonValue) -> tuple[str, ...]:
    found: list[str] = []

    def visit(item: JsonValue) -> None:
        if isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)
        elif isinstance(item, str) and item.startswith("data:image/"):
            found.append(item)

    visit(value)
    return tuple(found)


def _stable_error_code(action: Any, expected: str) -> None:
    observations: list[tuple[str, str, bool]] = []
    for _ in range(2):
        with pytest.raises(PortableContractError) as caught:
            action()
        observations.append(
            (
                caught.value.code,
                str(caught.value),
                caught.value.provider_invocation_allowed,
            )
        )
    assert observations[0] == observations[1]
    assert observations[0][0] == expected
    assert observations[0][2] is False


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_captured_fixture_schema_and_secret_boundary(case: CodecCase) -> None:
    data = _load(case)
    schema = json.loads((SCHEMA_ROOT / "captured_request_fixture.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(data)
    assert canonical_sha256(data["application_request"]) == data["fixture_request_sha256"]
    golden_path = FIXTURE_ROOT / data["human_diff_golden"]
    golden_bytes = golden_path.read_bytes()
    assert hashlib.sha256(golden_bytes).hexdigest() == data["human_diff_sha256"]
    golden = golden_bytes.decode("utf-8")
    serialized = (json.dumps(data, ensure_ascii=False) + golden).lower()
    for forbidden in (
        "api_key",
        "authorization",
        "bearer ",
        "cookie",
        "https://",
        "http://",
    ):
        assert forbidden not in serialized
    assert "data:image/png;base64," in serialized
    assert data["sanitization"]["source_bytes_copied"] is False
    assert data["sanitization"]["fixture_is_formal_g1_data"] is False


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_common_five_arm_cpu_checkpoint(case: CodecCase) -> None:
    data = _load(case)
    codec = _codec(case, data)
    request_snapshot = canonical_sha256(data["application_request"])
    ir = codec.extract(data["application_request"])
    assert ir.to_dict() == codec.extract(data["application_request"]).to_dict()
    assert ir.capabilities.scope.value == "LIVE"
    assert ir.capabilities.live_ready is False
    assert set(ir.capabilities.supported_arms) == set(ArmKind)
    catalog_hashes = {record.provenance["curated_binding_catalog_sha256"] for record in ir.records}
    assert len(catalog_hashes) == 1
    assert all(
        record.provenance["binding_source_request_sha256"] == ir.raw_request_sha256
        for record in ir.records
    )
    assert {region.kind.value for region in ir.regions} >= {
        "SYSTEM",
        "TASK",
        "HISTORY",
        "CURRENT_OBSERVATION",
        "TOOL_PROTOCOL",
    }
    for record in ir.records:
        for span in (*record.editable_spans, *record.protected_spans):
            container = get_at_path(data["application_request"], span.container_path)
            assert isinstance(container, str)
            assert len(container[: span.char_start].encode("utf-8")) == span.utf8_byte_start
            assert len(container[: span.char_end].encode("utf-8")) == span.utf8_byte_end
    assert any(
        span.utf8_byte_end - span.utf8_byte_start > span.char_end - span.char_start
        for record in ir.records
        for span in record.editable_spans
    )
    plans = _plans(data, ir)
    ir_snapshot = canonical_sha256(ir.to_dict())
    plan_snapshots = tuple(canonical_sha256(plan.to_dict()) for plan in plans)
    capsule = _capsule(case, data, ir)
    checkpoint = run_history_codec_cpu_checkpoint(
        capsule=capsule,
        codec=codec,
        paired_plans=plans,
    )
    assert [item.arm for item in checkpoint.arms] == list(ArmKind)
    assert all(not item.validation_receipt.provider_invocation_allowed for item in checkpoint.arms)
    assert all(
        item.validation_receipt.provider_decision.value == "BLOCK" for item in checkpoint.arms
    )
    assert all(item.invariance_report.target_only_diff for item in checkpoint.arms)
    assert all(item.invariance_report.source_mapping_reversible for item in checkpoint.arms)
    manifest = checkpoint.to_dict()
    manifest_schema = json.loads((SCHEMA_ROOT / "codec_cpu_checkpoint.schema.json").read_text())
    Draft202012Validator.check_schema(manifest_schema)
    Draft202012Validator(manifest_schema).validate(manifest)
    receipt_name = case.fixture_name.replace(".captured.v1.json", ".cpu_checkpoint.v1.json")
    receipt_text = (PUBLICATION_ROOT / receipt_name).read_text(encoding="utf-8")
    checked_in_receipt = json.loads(receipt_text)
    Draft202012Validator(manifest_schema).validate(checked_in_receipt)
    assert checked_in_receipt == manifest
    assert receipt_text == json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    assert manifest["live_smoke_completed"] is False
    assert manifest["provider_invocation_count"] == 0
    assert manifest["treatment_response_count"] == 0
    assert manifest["gpu_used"] is False
    assert manifest["network_used"] is False

    by_arm = {item.arm: item.render_result for item in checkpoint.arms}
    assert {arm.value: result.rendered_request_sha256 for arm, result in by_arm.items()} == data[
        "expected_rendered_request_sha256"
    ]
    rendered_golden = "".join(
        f"### {item.arm.value}\n{render_human_diff(item.render_result)}" for item in checkpoint.arms
    )
    assert rendered_golden == (FIXTURE_ROOT / data["human_diff_golden"]).read_text(encoding="utf-8")
    original = by_arm[ArmKind.ORIGINAL]
    assert original.rendered_request == data["application_request"]
    assert not original.diffs and not original.list_insertions
    assert render_human_diff(original).endswith("changes=NONE\n")

    expected_by_id = {
        item["binding_id"]: item["exact_text"] for item in data["curated_span_bindings"]
    }
    mask_text = expected_by_id[data["plan_targets"]["mask"][0]]
    oracle_texts = [expected_by_id[item] for item in data["plan_targets"]["oracle_clean"]]
    sham_text = expected_by_id[data["plan_targets"]["sham_benign_edit"][0]]
    rendered_mask = json.dumps(by_arm[ArmKind.MASK].rendered_request, ensure_ascii=False)
    rendered_oracle = json.dumps(by_arm[ArmKind.ORACLE_CLEAN].rendered_request, ensure_ascii=False)
    rendered_sham = json.dumps(
        by_arm[ArmKind.SHAM_BENIGN_EDIT].rendered_request, ensure_ascii=False
    )
    assert mask_text not in rendered_mask
    assert all(item not in rendered_oracle for item in oracle_texts)
    assert sham_text in rendered_oracle
    assert sham_text not in rendered_sham

    correction = by_arm[ArmKind.MASK_CORRECTION]
    assert len(correction.diffs) == 1 and len(correction.list_insertions) == 1
    inserted = correction.list_insertions[0].inserted_value
    assert isinstance(inserted, dict)
    assert inserted["text"] == f"SENTINEL correction: {data['correction_text']}"
    assert mask_text not in json.dumps(correction.rendered_request, ensure_ascii=False)

    for arm, result in by_arm.items():
        assert restore_original(result) == data["application_request"]
        assert _data_urls(result.rendered_request) == _data_urls(data["application_request"])
        repeated = codec.render(
            data["application_request"],
            ir,
            next(plan for plan in plans if plan.arm is arm),
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        assert repeated.to_dict() == result.to_dict()
        assert render_human_diff(repeated) == render_human_diff(result)
    assert canonical_sha256(data["application_request"]) == request_snapshot
    assert canonical_sha256(ir.to_dict()) == ir_snapshot
    assert tuple(canonical_sha256(plan.to_dict()) for plan in plans) == plan_snapshots


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_multiple_mask_and_oracle_exact_diff(case: CodecCase) -> None:
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    plan = _make_plan(
        ir=ir,
        arm=ArmKind.MASK,
        binding_ids=data["plan_targets"]["oracle_clean"],
        correction_text=data["correction_text"],
    )
    validate_plan(data["application_request"], ir, plan)
    result = codec.render(
        data["application_request"],
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert len(result.diffs) == 2
    assert [item.operation_id for item in result.diffs] == [
        item.operation_id for item in plan.operations
    ]
    assert restore_original(result) == data["application_request"]


def test_qwen_preserves_shell_tool_result_and_current_image() -> None:
    case = CASES[0]
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    plan = _plans(data, ir)[1]
    result = codec.render(
        data["application_request"],
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    rendered = cast(dict[str, Any], result.rendered_request)
    text = rendered["messages"][1]["content"][0]["text"]
    assert "Step 1: ; Tool call result:" in text
    assert "<tool_response>{screen:settings}</tool_response>" in text
    assert (
        rendered["messages"][1]["content"][1]
        == data["application_request"]["messages"][1]["content"][1]
    )

    request = data["application_request"]
    source_text = request["messages"][1]["content"][0]["text"]
    focal = _bindings(data)[0]
    early = CuratedSpanBinding.from_text(
        binding_id="qwen-same-record-early",
        source_request_sha256=canonical_sha256(request),
        container_path=focal.container_path,
        container_text=source_text,
        char_start=focal.char_start,
        char_end=focal.char_start + 3,
    )
    later = CuratedSpanBinding.from_text(
        binding_id="qwen-same-record-later",
        source_request_sha256=canonical_sha256(request),
        container_path=focal.container_path,
        container_text=source_text,
        char_start=focal.char_start + 3,
        char_end=focal.char_end,
    )
    canonical_ir = _codec(case, data, (early, later)).extract(request)
    reversed_ir = _codec(case, data, (later, early)).extract(request)
    assert reversed_ir.to_dict() == canonical_ir.to_dict()
    mapped = _targets(reversed_ir)
    assert mapped[early.binding_id][1].exact_text == early.exact_text
    assert mapped[later.binding_id][1].exact_text == later.exact_text


def test_mai_preserves_message_identity_wrappers_and_adjacency() -> None:
    case = CASES[1]
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    plan = _plans(data, ir)[1]
    result = codec.render(
        data["application_request"],
        ir,
        plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    source_messages = data["application_request"]["messages"]
    rendered = cast(dict[str, Any], result.rendered_request)
    rendered_messages = rendered["messages"]
    assert len(rendered_messages) == len(source_messages)
    assert [item["role"] for item in rendered_messages] == [
        item["role"] for item in source_messages
    ]
    assert rendered_messages[2]["content"].startswith("<thinking>\n\n</thinking>")
    assert "<tool_call>" in rendered_messages[2]["content"]
    assert rendered_messages[3:6] == source_messages[3:6]
    assistant = next(
        record for record in ir.records if record.record_key == "assistant-message-0003"
    )
    assert assistant.relationships[0].target_path == ("messages", 4, "content", 0)
    assert assistant.related_content[0].kind.value == "TOOL_RESULT"


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_binding_stale_overlap_missing_and_protected_fail_closed(case: CodecCase) -> None:
    data = _load(case)
    original = _bindings(data)
    request_drift = deepcopy(data["application_request"])
    request_drift["temperature"] = 0.125
    _stable_error_code(
        lambda: _codec(case, data).extract(request_drift),
        "TARGET_BINDING_REQUEST_MISMATCH",
    )

    stale = replace(original[0], span_sha256="f" * 64)
    _stable_error_code(
        lambda: _codec(case, data, (stale, *original[1:])).extract(data["application_request"]),
        "TARGET_BINDING_STALE",
    )

    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, (original[0], replace(original[0], binding_id="overlap")))
    assert caught.value.code == "OVERLAPPING_TARGET_BINDINGS"

    with pytest.raises(PortableContractError) as caught:
        _codec(
            case,
            data,
            (
                original[0],
                replace(original[1], source_request_sha256="f" * 64),
            ),
        )
    assert caught.value.code == "TARGET_BINDING_REQUEST_SET_MISMATCH"

    with pytest.raises(PortableContractError) as caught:
        case.codec_type(cast(Any, (["not", "a", "binding"],)))
    assert caught.value.code == "TARGET_BINDING_OBJECT_INVALID"

    mutable_path = replace(original[0], container_path=cast(Any, list(original[0].container_path)))
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, (mutable_path,))
    assert caught.value.code == "TARGET_BINDING_PATH_NON_CANONICAL"

    malformed_bindings = (
        (replace(original[0], binding_id=cast(Any, [])), "TARGET_BINDING_ID_MISSING"),
        (
            replace(original[0], source_request_sha256=cast(Any, 7)),
            "TARGET_BINDING_REQUEST_DIGEST_INVALID",
        ),
        (
            replace(original[0], char_start=cast(Any, "0")),
            "TARGET_BINDING_COORDINATE_INVALID",
        ),
        (
            replace(original[0], exact_text=cast(Any, [])),
            "TARGET_BINDING_TEXT_INVALID",
        ),
        (
            replace(original[0], span_sha256=cast(Any, [])),
            "TARGET_BINDING_SPAN_DIGEST_INVALID",
        ),
        (
            replace(original[0], span_role=cast(Any, "EDITABLE_CLAIM")),
            "TARGET_BINDING_ROLE_INVALID",
        ),
    )
    for malformed, expected_code in malformed_bindings:
        with pytest.raises(PortableContractError) as caught:
            _codec(case, data, (malformed,))
        assert caught.value.code == expected_code

    missing = replace(original[0], container_path=("messages", 99, "content"))
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, (missing, *original[1:])).extract(data["application_request"])
    assert caught.value.code == "TARGET_BINDING_PATH_MISSING"

    if case.codec_type is QwenFlatProgressHistoryCodec:
        request = data["application_request"]
        text = request["messages"][1]["content"][0]["text"]
        start = text.index("Step 1: ")
        protected = CuratedSpanBinding.from_text(
            binding_id="protected-qwen-shell",
            source_request_sha256=canonical_sha256(request),
            container_path=("messages", 1, "content", 0, "text"),
            container_text=text,
            char_start=start,
            char_end=start + len("Step 1: "),
        )
    else:
        request = data["application_request"]
        text = request["messages"][2]["content"]
        start = text.index("<tool_call>")
        protected = CuratedSpanBinding.from_text(
            binding_id="protected-mai-tool",
            source_request_sha256=canonical_sha256(request),
            container_path=("messages", 2, "content"),
            container_text=text,
            char_start=start,
            char_end=start + len("<tool_call>"),
        )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, (protected,)).extract(request)
    assert caught.value.code == "TARGET_BINDING_OUTSIDE_EDITABLE_HISTORY"
    assert caught.value.provider_invocation_allowed is False


def test_qwen_shape_ambiguity_empty_and_ordinal_fail_closed() -> None:
    case = CASES[0]
    data = _load(case)
    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] += (
        "Task progress (You have done the following operation on the current device): Step 1: x; \n"
    )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_PROGRESS_BLOCK_AMBIGUOUS"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].lstrip("\n")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_PROGRESS_BLOCK_AMBIGUOUS"

    request = deepcopy(data["application_request"])
    text = request["messages"][1]["content"][0]["text"]
    history_start = text.index("Task progress (")
    request["messages"][1]["content"][0]["text"] = text[:history_start] + (
        "Task progress (You have done the following operation on the current device): \n"
    )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "EMPTY_HISTORY_IR"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].replace("Step 2:", "Step 4:")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_STEP_ORDINAL_MISMATCH"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].replace("Step 3: 已查看主题选项; \n", "Step 3: 已查看主题选项\n")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_STEP_TERMINATOR_MISMATCH"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].replace("</tool_response>", "")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_TOOL_RESULT_WRAPPER_INVALID"

    request = deepcopy(data["application_request"])
    del request["messages"][1]["content"][1]["image_url"]["url"]
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "CURRENT_IMAGE_INVALID"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].replace("{screen:settings}", "")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "QWEN_TOOL_RESULT_WRAPPER_INVALID"

    request = deepcopy(data["application_request"])
    request["messages"][1]["content"][0]["text"] = request["messages"][1]["content"][0][
        "text"
    ].replace(
        "</tool_response>",
        "</tool_response>; Ask user response: 同时收到回答",
        1,
    )
    combined_ir = _codec(case, data, ()).extract(request)
    combined_suffix = combined_ir.records[0].protected_spans[-1].exact_text
    assert "</tool_response>; Ask user response: 同时收到回答; " in combined_suffix


def test_mai_wrapper_role_empty_and_hidden_image_shapes() -> None:
    case = CASES[1]
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    assert [record.record_key for record in ir.records[:2]] == [
        "assistant-message-0002",
        "assistant-message-0003",
    ]

    retained_initial_image = deepcopy(data["application_request"])
    retained_initial_image["messages"].insert(
        2,
        {
            "role": "user",
            "content": [deepcopy(retained_initial_image["messages"][-1]["content"][0])],
        },
    )
    retained_ir = _codec(case, data, ()).extract(retained_initial_image)
    assert retained_ir.records[0].record_key == "assistant-message-0003"

    legacy_close = deepcopy(data["application_request"])
    canonical = legacy_close["messages"][2]["content"]
    thinking_start = canonical.index("<thinking>") + len("<thinking>")
    thinking_end = canonical.index("</thinking>")
    legacy_content = (
        canonical[thinking_start:thinking_end]
        + "</think>"
        + canonical[thinking_end + len("</thinking>") :]
    )
    legacy_close["messages"][2]["content"] = legacy_content
    legacy_claim_start = len(legacy_content) - len(legacy_content.lstrip())
    legacy_inner = legacy_content[: legacy_content.index("</think>")]
    legacy_claim_end = len(legacy_inner.rstrip())
    legacy_binding = CuratedSpanBinding.from_text(
        binding_id="mai-legacy-think-close",
        source_request_sha256=canonical_sha256(legacy_close),
        container_path=("messages", 2, "content"),
        container_text=legacy_content,
        char_start=legacy_claim_start,
        char_end=legacy_claim_end,
    )
    legacy_codec = _codec(case, data, (legacy_binding,))
    legacy_ir = legacy_codec.extract(legacy_close)
    assert legacy_ir.records[0].provenance["thinking_wrapper_variant"] == "legacy_think_close"
    legacy_plan = _make_plan(
        ir=legacy_ir,
        arm=ArmKind.MASK,
        binding_ids=[legacy_binding.binding_id],
        correction_text=data["correction_text"],
    )
    legacy_result = legacy_codec.render(
        legacy_close,
        legacy_ir,
        legacy_plan,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    legacy_rendered = cast(dict[str, Any], legacy_result.rendered_request)
    assert legacy_rendered["messages"][2]["content"].startswith("\n\n</think>")
    assert "<tool_call>" in legacy_rendered["messages"][2]["content"]
    assert restore_original(legacy_result) == legacy_close

    for closing in ("</thinking>", "</think>"):
        request = deepcopy(
            data["application_request"] if closing == "</thinking>" else legacy_close
        )
        request["messages"][2]["content"] = request["messages"][2]["content"].replace(
            f"{closing}\n<tool_call>",
            f"{closing}\nUNWRAPPED ACTOR TEXT\n<tool_call>",
        )
        with pytest.raises(PortableContractError) as caught:
            _codec(case, data, ()).extract(request)
        assert caught.value.code == "MAI_WRAPPER_ORDER_MISMATCH"

    request = deepcopy(data["application_request"])
    request["messages"][2]["content"] = request["messages"][2]["content"].replace("</thinking>", "")
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_WRAPPER_AMBIGUOUS"

    request = deepcopy(data["application_request"])
    request["messages"][2]["content"] = request["messages"][2]["content"].replace(
        '{"name":"mobile_use"', '{"name":mobile_use'
    )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_TOOL_WRAPPER_INVALID"

    request = deepcopy(data["application_request"])
    content = request["messages"][2]["content"]
    claim_start = content.index("<thinking>") + len("<thinking>")
    claim_end = content.index("</thinking>")
    request["messages"][2]["content"] = content[:claim_start] + "\n\n" + content[claim_end:]
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_EMPTY_REASONING"

    request = deepcopy(data["application_request"])
    request["messages"][-1]["role"] = "assistant"
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_CURRENT_OBSERVATION_MISSING"

    request = deepcopy(data["application_request"])
    request["messages"] = request["messages"][:2] + [request["messages"][-1]]
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_MESSAGE_SHAPE_MISMATCH"

    request = deepcopy(data["application_request"])
    request["messages"][-1]["content"][0] = {"type": "text", "text": "not an image"}
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_CONTENT_SHAPE_MISMATCH"

    request = deepcopy(data["application_request"])
    request["messages"].insert(
        4,
        {"role": "user", "content": [{"type": "text", "text": "orphan observation"}]},
    )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_BROKEN_OBSERVATION_ADJACENCY"

    request = deepcopy(data["application_request"])
    request["messages"].insert(
        3,
        {
            "role": "user",
            "content": [deepcopy(request["messages"][-1]["content"][0])],
        },
    )
    request["messages"].insert(
        4,
        {
            "role": "user",
            "content": [deepcopy(request["messages"][-1]["content"][0])],
        },
    )
    with pytest.raises(PortableContractError) as caught:
        _codec(case, data, ()).extract(request)
    assert caught.value.code == "MAI_BROKEN_OBSERVATION_ADJACENCY"


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_unsupported_treatment_is_not_silent_original(case: CodecCase) -> None:
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    mask = _plans(data, ir)[1]
    narrowed_ir = replace(
        ir,
        capabilities=replace(
            ir.capabilities,
            supported_operations=(),
            supported_arms=(ArmKind.ORIGINAL,),
        ),
    )
    result = render_request(
        data["application_request"],
        narrowed_ir,
        mask,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    assert result.fallback_state.value == "BLOCKED_BEFORE_PROVIDER"
    assert result.effective_arm is None
    assert result.count_as_treatment is False
    assert result.rendered_request == data["application_request"]
    assert result.unsupported_reason == "UNSUPPORTED_ARM_OR_OPERATION"


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.fixture_name.split(".")[0])
def test_g14_runner_integration_blocks_before_fake_encode_or_send(case: CodecCase) -> None:
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    plans = _plans(data, ir)
    capsule = _capsule(case, data, ir)
    history_registry = HistoryCodecRegistry()
    history_registry.register(codec)
    provider = DeterministicFakeProviderCodec((FakeScenario.SUCCESS,))
    provider_registry = ProviderCodecRegistry()
    provider_registry.register(provider)
    schedule = schedule_for_unit(
        unit_kind=UnitKind.STRICT_MHR,
        unit_id=STRICT_UNIT_ID,
        model_id=case.model_id,
    )
    block = tuple(item for item in schedule if item.block_index == 1)
    with pytest.raises(ReplayRunnerError) as caught:
        preflight_block(
            capsule=capsule,
            history_ir=ir,
            paired_plans=plans,
            schedule_block=block,
            history_registry=history_registry,
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.FAKE_CONFORMANCE,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
        )
    assert caught.value.code == "PREFLIGHT_PROVIDER_AUTHORIZATION_BLOCKED"
    assert caught.value.provider_invocation_allowed is False
    assert provider.encode_calls == 0
    assert provider.send_calls == 0
    assert provider.normalize_calls == 0
    assert provider.scenario_history == []

    with pytest.raises(ReplayRunnerError) as caught:
        preflight_block(
            capsule=capsule,
            history_ir=ir,
            paired_plans=plans,
            schedule_block=block,
            history_registry=history_registry,
            provider_registry=provider_registry,
            provider_codec_id=provider.codec_id,
            provider_contract_version=provider.contract_version,
            execution_domain=ExecutionDomain.LIVE_G1_SCIENTIFIC,
            code_sha256="2" * 64,
            config_sha256="3" * 64,
        )
    assert caught.value.code == "LIVE_EXECUTION_DEFERRED"
    with pytest.raises(ReplayRunnerError) as caught:
        execute_live_arm(cast(Any, None))
    assert caught.value.code == "LIVE_EXECUTION_DEFERRED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("execution_ready", True),
        ("provider_invocation_allowed", True),
        ("treatment_response_generation_allowed", True),
        ("provider_invoked", True),
        ("treatment_response_count", 1),
    ],
)
def test_cpu_checkpoint_rejects_capsule_authorization_drift(field: str, value: JsonValue) -> None:
    case = CASES[0]
    data = _load(case)
    codec = _codec(case, data)
    ir = codec.extract(data["application_request"])
    capsule = _capsule(case, data, ir)
    source_safety = cast(dict[str, JsonValue], copy_json(capsule.source_safety))
    source_safety[field] = value
    with pytest.raises(PortableContractError) as caught:
        run_history_codec_cpu_checkpoint(
            capsule=replace(capsule, source_safety=source_safety),
            codec=codec,
            paired_plans=_plans(data, ir),
        )
    assert caught.value.code == "G15_CAPSULE_GUARD_INVALID"
    assert caught.value.provider_invocation_allowed is False


def test_static_shared_core_and_cpu_only_import_boundaries() -> None:
    shared_paths = (
        REPO_ROOT / "MobileWorld/src/mobile_world/offline/causal_replay/core.py",
        REPO_ROOT / "MobileWorld/src/mobile_world/offline/causal_replay_runner/runner.py",
        REPO_ROOT / "MobileWorld/src/mobile_world/offline/g1_history_codecs/cpu_checkpoint.py",
    )
    for path in shared_paths:
        source = path.read_text(encoding="utf-8")
        assert "qwen3vl_8b" not in source
        assert "mai_ui_8b" not in source

    forbidden_import_roots = {
        "aiohttp",
        "docker",
        "httpx",
        "openai",
        "requests",
        "socket",
        "subprocess",
        "torch",
        "transformers",
        "urllib",
        "vllm",
    }
    package_root = REPO_ROOT / "MobileWorld/src/mobile_world/offline/g1_history_codecs"
    for path in package_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert imported_roots.isdisjoint(forbidden_import_roots)

    publication_schema = json.loads(
        (SCHEMA_ROOT / "codec_cpu_publication.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(publication_schema)
    publication = json.loads(
        (PUBLICATION_ROOT / "cpu_publication_manifest.v1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(publication_schema).validate(publication)
    coordinate_schema = json.loads(
        (SCHEMA_ROOT / "host_coordinate_binding.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(coordinate_schema)
    coordinate_binding = json.loads(
        (PUBLICATION_ROOT / "host_coordinate_binding.v1.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(coordinate_schema).validate(coordinate_binding)

    shared = publication["shared_bindings"]
    for artifact in shared.values():
        path = REPO_ROOT / artifact["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    assert shared["tokenizer_binding"]["tokenizer_required"] is False
    for selected, case in zip(publication["selected_codecs"], CASES, strict=True):
        assert selected["codec_id"] == _codec(case, _load(case)).codec_id
        capability = selected["capability"]
        assert canonical_sha256(capability["declaration"]) == capability["sha256"]
        implementation = selected["implementation"]
        assert (
            hashlib.sha256((REPO_ROOT / implementation["path"]).read_bytes()).hexdigest()
            == implementation["sha256"]
        )
        source_fixture = selected["source_fixture"]
        source_path = REPO_ROOT / source_fixture["path"]
        assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_fixture["file_sha256"]
        source_data = json.loads(source_path.read_text(encoding="utf-8"))
        assert (
            canonical_sha256(source_data["application_request"])
            == source_fixture["source_request_sha256"]
        )
        receipt_ref = selected["conformance_receipt"]
        receipt_path = REPO_ROOT / receipt_ref["path"]
        assert hashlib.sha256(receipt_path.read_bytes()).hexdigest() == receipt_ref["file_sha256"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["codec_id"] == selected["codec_id"]
        assert receipt["codec_contract_version"] == selected["codec_contract_version"]
        assert receipt["history_family"] == selected["history_family"]
        assert receipt["capability_sha256"] == capability["sha256"]
        assert receipt["source_request_sha256"] == source_fixture["source_request_sha256"]
