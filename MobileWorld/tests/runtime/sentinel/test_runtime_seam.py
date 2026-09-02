from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations import mai_ui_agent as mai_module
from mobile_world.agents.implementations import qwen3vl as qwen_module
from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CorrectionAnchor,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    HistoryIR,
    JsonValue,
    OperationKind,
    PlanOperation,
    PortableContractError,
    RenderResult,
    SourceSpan,
    SpanRole,
    TransformationPlan,
    canonical_json_bytes,
    canonical_sha256,
    stable_id,
)
from mobile_world.offline.causal_replay.core import render_request
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.causal_replay.registry import HistoryCodecRegistry
from mobile_world.offline.g1_history_codecs import (
    CuratedSpanBinding,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.runtime.sentinel import (
    DeterministicFakeSentinelPolicy,
    ExternalSentinelReceiptSink,
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelCallRole,
    SentinelContext,
    SentinelContractError,
    SentinelDecision,
    SentinelDecisionKind,
    SentinelFallbackReason,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
    SentinelPolicyOutput,
    SentinelResult,
)
from mobile_world.runtime.sentinel import seam as sentinel_seam_module

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_PATH = (
    REPO_ROOT
    / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)
RECEIPT_SCHEMA_PATH = (
    REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_1/sentinel_receipt.v1.schema.json"
)
QWEN_HOST_ID = "mobileworld.qwen3vl.actor"
QWEN_CODEC_ID = "mobileworld.g1.history-codec.qwen-flat-progress"


class _Agent(BaseAgent):
    sentinel_host_id = QWEN_HOST_ID
    sentinel_history_codec_id = QWEN_CODEC_ID

    def predict(self, observation: dict[str, Any]) -> tuple[str, Any]:
        raise NotImplementedError


class _Response:
    def __init__(self, content: str = "ok") -> None:
        self.usage = None
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _Chunk:
    def __init__(self, content: str) -> None:
        self.usage = None
        self.choices = [SimpleNamespace(delta=SimpleNamespace(content=content))]


class _Completions:
    def __init__(self, create: Any) -> None:
        self.create = create


def _client(create: Any) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions(create)))


def _fixture() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


def _fixture_codec_and_request(
    *, request_updates: dict[str, JsonValue] | None = None
) -> tuple[QwenFlatProgressHistoryCodec, dict[str, Any]]:
    data = _fixture()
    request = deepcopy(data["application_request"])
    if request_updates is not None:
        request.update(request_updates)
    request_sha256 = canonical_sha256(cast(JsonValue, request))
    bindings = tuple(
        CuratedSpanBinding(
            binding_id=item["binding_id"],
            source_request_sha256=request_sha256,
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
    return QwenFlatProgressHistoryCodec(bindings), request


def _first_editable(ir: HistoryIR) -> tuple[Any, Any]:
    for record in ir.records:
        if record.editable_spans:
            return record, record.editable_spans[0]
    raise AssertionError("fixture has no editable span")


def _drop_output(
    _request: JsonValue, context: SentinelContext, ir: HistoryIR
) -> SentinelPolicyOutput:
    record, span = _first_editable(ir)
    operation_id = f"{context.logical_call_id}:drop-1"
    operation = PlanOperation(
        operation_id=operation_id,
        kind=OperationKind.DROP,
        target_record_id=record.record_id,
        target_span=span,
    )
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": ArmKind.MASK.value,
        "operations": [operation.to_dict()],
    }
    plan = TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=ArmKind.MASK,
        operations=(operation,),
        curated=True,
        deployment_prediction=False,
    )
    return SentinelPolicyOutput(
        decisions=(
            SentinelDecision(
                decision_id=f"{context.logical_call_id}:decision-1",
                kind=SentinelDecisionKind.DROP,
                operation_id=operation_id,
                record_id=record.record_id,
            ),
        ),
        transformation_plan=plan,
    )


def _alternate_drop_plan(ir: HistoryIR, plan: TransformationPlan) -> TransformationPlan:
    original_span = plan.operations[0].target_span
    alternatives = [
        (record, span)
        for record in ir.records
        for span in record.editable_spans
        if span != original_span
    ]
    assert alternatives
    record, span = alternatives[0]
    operation = replace(
        plan.operations[0],
        target_record_id=record.record_id,
        target_span=span,
    )
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": plan.arm.value,
        "operations": [operation.to_dict()],
    }
    return replace(
        plan,
        plan_id=stable_id("plan", subject),
        operations=(operation,),
    )


def _keep_output(
    _request: JsonValue, context: SentinelContext, _ir: HistoryIR
) -> SentinelPolicyOutput:
    return SentinelPolicyOutput(
        decisions=(
            SentinelDecision(
                decision_id=f"{context.logical_call_id}:keep",
                kind=SentinelDecisionKind.KEEP,
            ),
        ),
        transformation_plan=None,
    )


def _replace_output(
    _request: JsonValue, context: SentinelContext, ir: HistoryIR
) -> SentinelPolicyOutput:
    record, span = _first_editable(ir)
    anchor = record.correction_anchors[0]
    operation_id = f"{context.logical_call_id}:replace-1"
    correction_text = "Visible evidence does not support that prior claim."
    operation = PlanOperation(
        operation_id=operation_id,
        kind=OperationKind.REPLACE,
        target_record_id=record.record_id,
        target_span=span,
        replacement_text=correction_text,
        replacement_author="SENTINEL",
        evidence_refs=(
            EvidenceRef(
                evidence_id="r21-secret-free-current-observation",
                sha256="e" * 64,
                role="current_observation_pre_cutoff",
                event_seq=7,
            ),
        ),
        correction_anchor=anchor,
        rendered_correction_context={
            "type": "text",
            "text": f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}",
        },
    )
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": ArmKind.MASK_CORRECTION.value,
        "operations": [operation.to_dict()],
    }
    plan = TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=ArmKind.MASK_CORRECTION,
        operations=(operation,),
        curated=True,
        deployment_prediction=False,
    )
    return SentinelPolicyOutput(
        decisions=(
            SentinelDecision(
                decision_id=f"{context.logical_call_id}:replace-decision-1",
                kind=SentinelDecisionKind.REPLACE,
                operation_id=operation_id,
                record_id=record.record_id,
            ),
        ),
        transformation_plan=plan,
    )


def _sentinel(
    *,
    mode: SentinelMode,
    policy_factory: Any = _drop_output,
    codec: HistoryCodec | None = None,
    sink: Any = None,
    switch: SentinelGlobalSwitch | None = None,
    clock_ns: Any = None,
) -> tuple[PromptSentinel, DeterministicFakeSentinelPolicy]:
    selected = codec or _fixture_codec_and_request()[0]
    registry = HistoryCodecRegistry()
    registry.register(selected)
    policy = DeterministicFakeSentinelPolicy(policy_factory)
    kwargs: dict[str, Any] = {}
    if clock_ns is not None:
        kwargs["clock_ns"] = clock_ns
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=mode)},
        receipt_sink=MemorySentinelReceiptSink() if sink is None else sink,
        global_switch=switch or SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "01K_SENTINEL_LOGICAL_CALL_01",
        **kwargs,
    )
    return sentinel, policy


def _call_base(
    agent: _Agent,
    request: dict[str, Any],
    *,
    retry_times: int = 2,
) -> str | None:
    kwargs = {key: value for key, value in request.items() if key not in {"model", "messages"}}
    return agent.openai_chat_completions_create(
        model=request["model"],
        messages=request["messages"],
        retry_times=retry_times,
        **kwargs,
    )


def test_off_is_identity_preserving_and_does_no_semantic_work() -> None:
    codec, request = _fixture_codec_and_request()

    def forbidden(*_args: Any) -> SentinelPolicyOutput:
        raise AssertionError("OFF must not evaluate policy")

    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.OFF,
        policy_factory=forbidden,
        codec=codec,
        sink=sink,
    )
    captured: list[dict[str, Any]] = []
    agent = _Agent(prompt_sentinel=sentinel)
    original_messages = request["messages"]

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        return _Response()

    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert policy.evaluate_count == 0
    assert len(captured) == 1
    assert captured[0]["messages"] is original_messages
    assert sink.receipts[0].policy_evaluated is False
    assert sink.receipts[0].final_request_sha256 == canonical_sha256(request)


def test_shadow_computes_edit_once_but_provider_receives_original_identity() -> None:
    codec, request = _fixture_codec_and_request()
    original = deepcopy(request)
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(mode=SentinelMode.SHADOW, codec=codec, sink=sink)
    captured: list[dict[str, Any]] = []
    agent = _Agent(prompt_sentinel=sentinel)

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        return _Response()

    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert policy.evaluate_count == 1
    assert request == original
    assert captured[0]["messages"] is request["messages"]
    receipt = sink.receipts[0]
    assert receipt.would_edit is True
    assert receipt.edit_applied is False
    assert receipt.raw_request_sha256 == receipt.final_request_sha256
    assert receipt.candidate_request_sha256 != receipt.raw_request_sha256


def test_active_reuses_one_validated_history_only_edit_across_transport_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request()
    original = deepcopy(request)
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(mode=SentinelMode.ACTIVE, codec=codec, sink=sink)
    captured: list[dict[str, Any]] = []
    agent = _Agent(prompt_sentinel=sentinel)

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        if len(captured) == 1:
            raise RuntimeError("retry once")
        return _Response()

    monkeypatch.setattr("mobile_world.agents.base.time.sleep", lambda _seconds: None)
    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert policy.evaluate_count == 1
    assert len(captured) == 2
    assert captured[0]["messages"] is captured[1]["messages"]
    assert captured[0]["messages"] is not request["messages"]
    final_text = captured[0]["messages"][1]["content"][0]["text"]
    assert "已打开设置🙂" not in final_text
    assert captured[0]["messages"][0] == request["messages"][0]
    assert captured[0]["messages"][1]["content"][1] == request["messages"][1]["content"][1]
    assert captured[0]["model"] == request["model"]
    assert captured[0]["temperature"] == request["temperature"]
    assert request == original
    assert len(sink.receipts) == 1
    assert sink.receipts[0].edit_applied is True
    assert sink.receipts[0].policy_output_sha256 != canonical_sha256(
        {"decisions": [], "transformation_plan": None}
    )


def test_r21_rejects_correction_insertions_that_change_current_observation() -> None:
    codec, request = _fixture_codec_and_request()
    original = deepcopy(request)
    outputs: list[SentinelPolicyOutput] = []

    def rejected_replace(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _replace_output(request_value, context, ir)
        outputs.append(output)
        return output

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=rejected_replace,
        codec=codec,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    cached = call.before_model_call(request)
    assert policy.evaluate_count == 1
    assert cached is result
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVALID_POLICY_OUTPUT
    assert result.receipt.policy_output_sha256 == canonical_sha256(outputs[0].to_dict())
    assert result.receipt.policy_output_sha256 != canonical_sha256(
        {"decisions": [], "transformation_plan": None}
    )
    assert result.receipt.edit_applied is False
    assert result.final_request == original
    assert request == original


def test_rejected_duplicate_decision_ids_bind_the_complete_policy_output_hash() -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []
    sink = MemorySentinelReceiptSink()

    def duplicate_decisions(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        valid = _keep_output(request_value, context, ir)
        output = replace(valid, decisions=(valid.decisions[0], valid.decisions[0]))
        outputs.append(output)
        return output

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=duplicate_decisions,
        codec=codec,
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    cached = call.before_model_call(request)
    assert cached is result
    assert policy.evaluate_count == 1
    assert len(outputs) == 1
    assert len(sink.receipts) == 1
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVALID_POLICY_OUTPUT
    assert result.receipt.validation_checks == ("duplicate_decision_id",)
    assert result.receipt.policy_evaluated is True
    assert result.receipt.policy_output_sha256 == canonical_sha256(outputs[0].to_dict())
    assert result.receipt.policy_output_sha256 != canonical_sha256(
        {"decisions": [], "transformation_plan": None}
    )
    assert result.receipt.decision_kinds == ()
    assert result.receipt.raw_request_sha256 == result.receipt.candidate_request_sha256
    assert result.receipt.raw_request_sha256 == result.receipt.final_request_sha256
    assert result.final_request == request
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result.receipt.to_dict())


@pytest.mark.parametrize(
    "untrusted_layer",
    (
        "output",
        "decision",
        "plan",
        "operation",
        "source_span",
        "evidence_ref",
        "correction_anchor",
        "decision_tuple",
        "rendered_json",
    ),
)
def test_policy_output_snapshot_rejects_untrusted_recursive_graph_before_hash_or_render(
    untrusted_layer: str,
) -> None:
    codec, request = _fixture_codec_and_request()
    original = deepcopy(request)
    serializer_calls: list[str] = []

    def lie(_value: Any) -> dict[str, JsonValue]:
        serializer_calls.append(untrusted_layer)
        return {"decisions": [], "transformation_plan": None}

    class LyingOutput(SentinelPolicyOutput):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingDecision(SentinelDecision):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingPlan(TransformationPlan):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingOperation(PlanOperation):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingSourceSpan(SourceSpan):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingEvidenceRef(EvidenceRef):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class LyingCorrectionAnchor(CorrectionAnchor):
        def to_dict(self) -> dict[str, JsonValue]:
            return lie(self)

    class DecisionTuple(tuple):
        pass

    class RenderedJson(dict):
        pass

    def kwargs(value: Any) -> dict[str, Any]:
        return {item.name: getattr(value, item.name) for item in fields(value)}

    def untrusted_output(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = (
            _replace_output(request_value, context, ir)
            if untrusted_layer in {"evidence_ref", "correction_anchor", "rendered_json"}
            else _drop_output(request_value, context, ir)
        )
        assert output.transformation_plan is not None
        plan = output.transformation_plan
        operation = plan.operations[0]
        if untrusted_layer == "output":
            return LyingOutput(**kwargs(output))
        if untrusted_layer == "decision":
            decision = LyingDecision(**kwargs(output.decisions[0]))
            return replace(output, decisions=(decision,))
        if untrusted_layer == "plan":
            return replace(output, transformation_plan=LyingPlan(**kwargs(plan)))
        if untrusted_layer == "operation":
            operation = LyingOperation(**kwargs(operation))
        elif untrusted_layer == "source_span":
            operation = replace(
                operation,
                target_span=LyingSourceSpan(**kwargs(operation.target_span)),
            )
        elif untrusted_layer == "evidence_ref":
            operation = replace(
                operation,
                evidence_refs=(LyingEvidenceRef(**kwargs(operation.evidence_refs[0])),),
            )
        elif untrusted_layer == "correction_anchor":
            assert operation.correction_anchor is not None
            operation = replace(
                operation,
                correction_anchor=LyingCorrectionAnchor(**kwargs(operation.correction_anchor)),
            )
        elif untrusted_layer == "rendered_json":
            assert isinstance(operation.rendered_correction_context, dict)
            operation = replace(
                operation,
                rendered_correction_context=RenderedJson(operation.rendered_correction_context),
            )
        elif untrusted_layer == "decision_tuple":
            return replace(output, decisions=DecisionTuple(output.decisions))
        return replace(output, transformation_plan=replace(plan, operations=(operation,)))

    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=untrusted_output,
        codec=codec,
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    cached = call.before_model_call(request)
    assert cached is result
    assert policy.evaluate_count == 1
    assert serializer_calls == []
    assert len(sink.receipts) == 1
    assert result.final_request == original
    assert result.receipt.edit_applied is False
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVALID_POLICY_OUTPUT
    assert result.receipt.validation_checks == ("policy_output_untrusted_type",)
    assert result.receipt.policy_output_sha256 == canonical_sha256(
        {"decisions": [], "transformation_plan": None}
    )
    assert result.receipt.decision_kinds == ()
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result.receipt.to_dict())


def test_policy_output_snapshot_ignores_instance_serializer_shadow_and_detaches_renderer() -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []
    rendered_plans: list[TransformationPlan] = []
    serializer_calls: list[str] = []

    class CapturingCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, request_value: JsonValue) -> HistoryIR:
            return codec.extract(request_value)

        def render(
            self,
            request_value: JsonValue,
            ir: HistoryIR,
            plan: TransformationPlan,
            *,
            execution_mode: ExecutionMode,
            failure_policy: FailurePolicy,
        ) -> RenderResult:
            rendered_plans.append(plan)
            return codec.render(
                request_value,
                ir,
                plan,
                execution_mode=execution_mode,
                failure_policy=failure_policy,
            )

    def shadowed_output(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        outputs.append(output)

        def lie() -> dict[str, JsonValue]:
            serializer_calls.append("instance-shadow")
            return {"decisions": [], "transformation_plan": None}

        object.__setattr__(output, "to_dict", lie)
        return output

    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=shadowed_output,
        codec=cast(HistoryCodec, CapturingCodec()),
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 1
    assert serializer_calls == []
    assert result.receipt.fallback_reason is None
    assert result.receipt.edit_applied is True
    assert len(rendered_plans) == 1
    assert outputs[0].transformation_plan is not None
    assert rendered_plans[0] is not outputs[0].transformation_plan
    assert type(rendered_plans[0]) is TransformationPlan
    assert type(rendered_plans[0].operations[0]) is PlanOperation
    assert type(rendered_plans[0].operations[0].target_span) is SourceSpan
    expected = canonical_sha256(SentinelPolicyOutput.to_dict(outputs[0]))
    assert result.receipt.policy_output_sha256 == expected


def test_policy_receives_detached_request_context_and_history_ir() -> None:
    codec, request = _fixture_codec_and_request()
    original = deepcopy(request)

    def mutating_policy(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        assert isinstance(request_value, dict)
        request_value["model"] = "policy-mutated-model"
        object.__setattr__(context, "host_id", "policy-mutated-host")
        object.__setattr__(ir, "records", ())
        return output

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=mutating_policy,
        codec=codec,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 1
    assert request == original
    assert result.receipt.host_id == QWEN_HOST_ID
    assert result.receipt.fallback_reason is None
    assert result.receipt.edit_applied is True
    assert result.final_request["model"] == original["model"]


@pytest.mark.parametrize("invalid_shape", ("empty_decisions", "empty_decision_id"))
def test_canonical_exact_policy_output_hash_is_fixed_before_semantic_construction_rejection(
    invalid_shape: str,
) -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []

    def invalid_but_canonical(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        if invalid_shape == "empty_decisions":
            object.__setattr__(output, "decisions", ())
        else:
            object.__setattr__(output.decisions[0], "decision_id", "")
        outputs.append(output)
        return output

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=invalid_but_canonical,
        codec=codec,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    expected = canonical_sha256(SentinelPolicyOutput.to_dict(outputs[0]))
    assert policy.evaluate_count == 1
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVALID_POLICY_OUTPUT
    assert result.receipt.policy_output_sha256 == expected
    assert result.receipt.policy_output_sha256 != canonical_sha256(
        {"decisions": [], "transformation_plan": None}
    )


@pytest.mark.parametrize("renderer_attack", ("mutate_plan", "lying_result_serializer"))
def test_renderer_cannot_mutate_or_lie_about_the_receipt_bound_policy_snapshot(
    renderer_attack: str,
) -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []
    serializer_calls: list[str] = []

    class LyingRenderResult(RenderResult):
        def to_dict(self) -> dict[str, JsonValue]:
            serializer_calls.append("lying-result")
            return {"spoofed": True}

    class AdversarialRendererCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, request_value: JsonValue) -> HistoryIR:
            return codec.extract(request_value)

        def render(
            self,
            request_value: JsonValue,
            ir: HistoryIR,
            plan: TransformationPlan,
            *,
            execution_mode: ExecutionMode,
            failure_policy: FailurePolicy,
        ) -> RenderResult:
            alternate = _alternate_drop_plan(ir, plan)
            if renderer_attack == "mutate_plan":
                for item in fields(plan):
                    object.__setattr__(plan, item.name, getattr(alternate, item.name))
                return render_request(
                    request_value,
                    ir,
                    plan,
                    execution_mode=execution_mode,
                    failure_policy=failure_policy,
                )
            actual = render_request(
                request_value,
                ir,
                alternate,
                execution_mode=execution_mode,
                failure_policy=failure_policy,
            )
            return LyingRenderResult(
                **{item.name: getattr(actual, item.name) for item in fields(actual)}
            )

    def captured_drop(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        outputs.append(output)
        return output

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=captured_drop,
        codec=cast(HistoryCodec, AdversarialRendererCodec()),
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 1
    assert serializer_calls == []
    assert result.final_request == request
    assert result.receipt.edit_applied is False
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVARIANT_FAILURE
    assert result.receipt.policy_output_sha256 == canonical_sha256(
        SentinelPolicyOutput.to_dict(outputs[0])
    )


def test_history_ir_subclass_is_rejected_before_policy_or_renderer() -> None:
    codec, request = _fixture_codec_and_request()

    class LyingHistoryIR(HistoryIR):
        def record_by_id(self, _record_id: str) -> Any:
            raise AssertionError("subclass method must never execute")

    class UntrustedIrCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, request_value: JsonValue) -> HistoryIR:
            original = codec.extract(request_value)
            return LyingHistoryIR(
                **{item.name: getattr(original, item.name) for item in fields(original)}
            )

        def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
            raise AssertionError("untrusted IR must fail before renderer")

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=cast(HistoryCodec, UntrustedIrCodec()),
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 0
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert result.receipt.validation_checks == ("history_ir_untrusted_type",)


def test_cyclic_history_ir_json_fails_before_policy_with_truthful_evaluation_census() -> None:
    codec, request = _fixture_codec_and_request()

    class CyclicIrCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, request_value: JsonValue) -> HistoryIR:
            ir = codec.extract(request_value)
            cycle: dict[str, Any] = {}
            cycle["self"] = cycle
            object.__setattr__(ir.records[0], "provenance", cycle)
            return ir

        def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
            raise AssertionError("cyclic IR must fail before renderer")

    def forbidden(*_args: Any) -> SentinelPolicyOutput:
        raise AssertionError("cyclic IR must fail before policy")

    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=forbidden,
        codec=cast(HistoryCodec, CyclicIrCodec()),
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    assert call.before_model_call(request) is result
    assert policy.evaluate_count == 0
    assert len(sink.receipts) == 1
    assert result.final_request == request
    assert result.receipt.policy_evaluated is False
    assert result.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert result.receipt.validation_checks == ("history_ir_untrusted_type",)
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result.receipt.to_dict())


def test_mai_host_off_scope_preserves_request_and_existing_parser() -> None:
    def forbidden(*_args: Any) -> SentinelPolicyOutput:
        raise AssertionError("MAI OFF path must not evaluate policy")

    sink = MemorySentinelReceiptSink()
    policy = DeterministicFakeSentinelPolicy(forbidden)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=HistoryCodecRegistry(),
        host_configs={
            mai_module.MAIUINaivigationAgent.sentinel_host_id: SentinelHostConfig(
                mode=SentinelMode.OFF
            )
        },
        receipt_sink=sink,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "mai-off-host-boundary",
    )
    agent = object.__new__(mai_module.MAIUINaivigationAgent)
    BaseAgent.__init__(agent, prompt_sentinel=sentinel)
    agent.instruction = "Inspect the current settings screen."
    agent.model_name = "MAI-UI-8B"
    agent.max_tokens = 2048
    agent.temperature = 0.0
    agent.top_p = 1.0
    agent.history_n = 3
    agent.history_images = []
    agent.history_responses = []
    agent.tools = []
    captured: list[dict[str, Any]] = []
    raw_response = (
        "<thinking>wait for the stable screen</thinking>"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
    )

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        return _Response(raw_response)

    agent.openai_client = _client(create)
    returned, action = agent.predict({"screenshot": Image.new("RGB", (2, 2))})
    assert returned == raw_response
    assert action.action_type == "wait"
    assert mai_module.parse_action_to_structure_output(returned)["action_json"] == {
        "action": "wait"
    }
    assert policy.evaluate_count == 0
    assert len(captured) == 1
    assert len(sink.receipts) == 1
    assert sink.receipts[0].host_id == mai_module.MAIUINaivigationAgent.sentinel_host_id
    assert sink.receipts[0].history_codec_id == (
        mai_module.MAIUINaivigationAgent.sentinel_history_codec_id
    )
    assert sink.receipts[0].raw_request_sha256 == canonical_sha256(captured[0])


def test_existing_max_token_wire_alias_does_not_repeat_sentinel_or_change_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request(request_updates={"max_tokens": 20})
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    captured: list[dict[str, Any]] = []
    agent = _Agent(prompt_sentinel=sentinel)

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        if len(captured) == 1:
            raise ValueError("max_tokens is unsupported; use max_completion_tokens")
        return _Response()

    monkeypatch.setattr("mobile_world.agents.base.time.sleep", lambda _seconds: None)
    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1
    assert len(captured) == 2
    assert captured[0]["messages"] is captured[1]["messages"]
    assert captured[0]["messages"] is not request["messages"]
    assert captured[0]["max_tokens"] == captured[1]["max_completion_tokens"] == 20
    assert "max_completion_tokens" not in captured[0]
    assert "max_tokens" not in captured[1]
    first_without_alias = {key: value for key, value in captured[0].items() if key != "max_tokens"}
    second_without_alias = {
        key: value for key, value in captured[1].items() if key != "max_completion_tokens"
    }
    assert first_without_alias == second_without_alias


def test_opaque_sdk_parameter_stays_original_outside_canonical_json_admission() -> None:
    codec, request = _fixture_codec_and_request()
    opaque_parameter = object()
    request["opaque_sdk_parameter"] = opaque_parameter
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    agent = _Agent(prompt_sentinel=sentinel)
    captured: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        return _Response()

    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert captured[0]["opaque_sdk_parameter"] is opaque_parameter
    assert captured[0]["messages"] is request["messages"]
    assert policy.evaluate_count == 0
    assert sink.receipts == ()


@pytest.mark.parametrize(
    ("field", "non_json_value"),
    (
        ("stop", ("END",)),
        ("metadata", {1: "one"}),
    ),
    ids=("tuple", "integer-dictionary-key"),
)
def test_json_coercible_non_json_parameters_stay_exactly_original_without_semantic_work(
    field: str,
    non_json_value: Any,
) -> None:
    codec, request = _fixture_codec_and_request(
        request_updates=cast(dict[str, JsonValue], {field: non_json_value})
    )

    class CountingMemorySink(MemorySentinelReceiptSink):
        def __init__(self) -> None:
            super().__init__()
            self.begin_count = 0

        def begin(self, logical_call_id: str) -> Any:
            self.begin_count += 1
            return super().begin(logical_call_id)

    sink = CountingMemorySink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    agent = _Agent(prompt_sentinel=sentinel)
    captured: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        return _Response()

    agent.openai_client = _client(create)
    assert _call_base(agent, request) == "ok"
    assert len(captured) == 1
    assert captured[0][field] is non_json_value
    assert captured[0]["messages"] is request["messages"]
    assert policy.evaluate_count == 0
    assert sink.begin_count == 0
    assert sink.receipts == ()


@pytest.mark.parametrize(
    ("valid_value", "non_json_value"),
    (
        (["END"], ("END",)),
        ({"1": "one"}, {1: "one"}),
    ),
    ids=("list-vs-tuple", "string-vs-integer-key"),
)
def test_non_json_request_cannot_reuse_a_canonical_hash_collision_from_cache(
    valid_value: JsonValue,
    non_json_value: Any,
) -> None:
    codec, valid_request = _fixture_codec_and_request(request_updates={"metadata": valid_value})
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    valid_result = call.before_model_call(valid_request)
    non_json_request = deepcopy(valid_request)
    non_json_request["metadata"] = non_json_value
    assert canonical_sha256(cast(JsonValue, non_json_request)) == canonical_sha256(valid_request)
    with pytest.raises(SentinelContractError, match="outside canonical-JSON admission domain"):
        call.before_model_call(cast(JsonValue, non_json_request))
    assert valid_result.receipt.edit_applied is True
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1


def test_global_kill_switch_and_sentinel_role_bypass_before_codec_or_policy() -> None:
    _codec, request = _fixture_codec_and_request()
    policy = DeterministicFakeSentinelPolicy(
        lambda *_args: (_ for _ in ()).throw(AssertionError("policy must be bypassed"))
    )
    registry = HistoryCodecRegistry()
    switch = SentinelGlobalSwitch(active=True)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE)},
        global_switch=switch,
        logical_call_id_factory=lambda: "kill-switch-call",
    )
    killed = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert killed.receipt.bypass_reason.value == "GLOBAL_KILL_SWITCH"
    assert killed.receipt.policy_evaluated is False
    switch.set_active(False)
    recursive = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
        call_role=SentinelCallRole.SENTINEL,
    ).before_model_call(request)
    assert recursive.receipt.bypass_reason.value == "CALL_ROLE_SENTINEL"
    assert recursive.receipt.policy_evaluated is False
    assert policy.evaluate_count == 0


def test_sentinel_owned_provider_call_bypasses_without_recursive_evaluation() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    sink = MemorySentinelReceiptSink()
    agent = _Agent()

    class RecursiveFakePolicy:
        policy_id = "mobileworld.runtime.sentinel-policy.recursion-probe/v1"

        def __init__(self) -> None:
            self.evaluate_count = 0

        def evaluate(
            self,
            *,
            request: JsonValue,
            context: SentinelContext,
            history_ir: HistoryIR,
        ) -> SentinelPolicyOutput:
            self.evaluate_count += 1
            assert (
                agent.openai_chat_completions_create(
                    model="sentinel-fake",
                    messages=[{"role": "user", "content": "classify"}],
                    call_role="sentinel",
                )
                == "inner"
            )
            return _keep_output(request, context, history_ir)

    policy = RecursiveFakePolicy()
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.SHADOW)},
        receipt_sink=sink,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=iter(("outer-call", "sentinel-owned-call")).__next__,
    )
    agent._prompt_sentinel = sentinel
    captured: list[str] = []

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs["model"])
        return _Response("inner" if kwargs["model"] == "sentinel-fake" else "outer")

    agent.openai_client = _client(create)
    with agent._sentinel_logical_call_scope(attributes={"test": "recursion"}):
        assert _call_base(agent, request) == "outer"
    assert captured == ["sentinel-fake", request["model"]]
    assert policy.evaluate_count == 1
    assert [receipt.call_role for receipt in sink.receipts] == [
        SentinelCallRole.SENTINEL,
        SentinelCallRole.ACTOR,
    ]
    assert sink.receipts[0].policy_evaluated is False


def test_policy_deadline_returns_original_without_waiting_for_worker_completion() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    release = Event()

    class BlockingPolicy:
        policy_id = "mobileworld.runtime.sentinel-policy.blocking-fake/v1"

        def evaluate(self, **_kwargs: Any) -> SentinelPolicyOutput:
            release.wait(timeout=1)
            return _keep_output(_kwargs["request"], _kwargs["context"], _kwargs["history_ir"])

    sentinel = PromptSentinel(
        policy=BlockingPolicy(),
        codec_registry=registry,
        host_configs={
            QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE, policy_timeout_ms=5)
        },
        receipt_sink=MemorySentinelReceiptSink(),
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "hard-timeout-call",
    )
    started = time.monotonic()
    try:
        result = sentinel.logical_call(
            host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
        ).before_model_call(request)
    finally:
        release.set()
    assert time.monotonic() - started < 0.5
    assert result.receipt.fallback_reason is SentinelFallbackReason.POLICY_TIMEOUT
    assert result.receipt.validation_checks == ("policy_deadline_exceeded",)
    assert result.final_request == request


def test_policy_thread_start_failure_does_not_claim_policy_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request()
    sink = MemorySentinelReceiptSink()

    class StartFailingThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("worker unavailable")

    monkeypatch.setattr(sentinel_seam_module, "Thread", StartFailingThread)
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 0
    assert len(sink.receipts) == 1
    assert result.final_request == request
    assert result.receipt.policy_evaluated is False
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVARIANT_FAILURE
    assert result.receipt.validation_checks == ("internal_evaluation_exception",)


def test_policy_worker_deferred_past_timeout_is_cancelled_before_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request()
    sink = MemorySentinelReceiptSink()
    deferred_invocations: list[tuple[Any, tuple[Any, ...]]] = []

    class DeferredThread:
        def __init__(self, *, target: Any, args: tuple[Any, ...], **_kwargs: Any) -> None:
            deferred_invocations.append((target, args))

        def start(self) -> None:
            pass

    monkeypatch.setattr(sentinel_seam_module, "Thread", DeferredThread)
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert policy.evaluate_count == 0
    assert len(deferred_invocations) == 1
    target, args = deferred_invocations[0]
    target(*args)
    assert policy.evaluate_count == 0
    assert len(sink.receipts) == 1
    assert result.final_request == request
    assert result.receipt.policy_evaluated is False
    assert result.receipt.fallback_reason is SentinelFallbackReason.POLICY_TIMEOUT
    assert result.receipt.validation_checks == ("policy_deadline_exceeded",)


def test_kill_switch_activation_during_evaluation_discards_candidate() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    switch = SentinelGlobalSwitch()

    def activate_then_edit(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        switch.set_active(True)
        return _drop_output(request_value, context, ir)

    sentinel = PromptSentinel(
        policy=DeterministicFakeSentinelPolicy(activate_then_edit),
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE)},
        receipt_sink=MemorySentinelReceiptSink(),
        global_switch=switch,
        logical_call_id_factory=lambda: "kill-during-evaluation",
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVARIANT_FAILURE
    assert result.receipt.global_kill_switch_active is True
    assert result.receipt.policy_evaluated is True
    assert result.receipt.edit_applied is False


def test_kill_switch_activation_pulse_during_evaluation_discards_candidate() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    switch = SentinelGlobalSwitch()

    def pulse_then_edit(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        switch.set_active(True)
        switch.set_active(False)
        return _drop_output(request_value, context, ir)

    policy = DeterministicFakeSentinelPolicy(pulse_then_edit)
    sink = MemorySentinelReceiptSink()
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE)},
        receipt_sink=sink,
        global_switch=switch,
        logical_call_id_factory=lambda: "kill-pulse-during-evaluation",
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    cached = call.before_model_call(request)
    assert cached is result
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVARIANT_FAILURE
    assert result.receipt.validation_checks == ("global_kill_switch_activated_during_evaluation",)
    assert result.receipt.global_kill_switch_active is False
    assert result.receipt.policy_evaluated is True
    assert result.receipt.edit_applied is False
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1


def test_per_host_modes_are_independent_and_default_off() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    policy = DeterministicFakeSentinelPolicy(_keep_output)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.SHADOW)},
        receipt_sink=MemorySentinelReceiptSink(),
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=iter(("configured-host", "default-host")).__next__,
    )
    configured = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    defaulted = sentinel.logical_call(
        host_id="mobileworld.unconfigured.actor", history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert configured.receipt.effective_mode is SentinelMode.SHADOW
    assert defaulted.receipt.bypass_reason.value == "MODE_OFF"
    assert policy.evaluate_count == 1


def test_semantic_modes_require_receipt_sink_before_policy_work() -> None:
    codec, request = _fixture_codec_and_request()
    registry = HistoryCodecRegistry()
    registry.register(codec)
    policy = DeterministicFakeSentinelPolicy(_drop_output)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE)},
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "missing-sidecar-sink",
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
    assert result.receipt.validation_checks == ("semantic_mode_requires_receipt_sink",)
    assert result.receipt.policy_evaluated is False
    assert result.final_request == request
    assert policy.evaluate_count == 0


def test_qwen_outer_parse_retry_reuses_one_sentinel_result() -> None:
    codec = QwenFlatProgressHistoryCodec()
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.SHADOW,
        policy_factory=_keep_output,
        codec=codec,
        sink=sink,
    )
    agent = qwen_module.Qwen3VLAgentMCP.__new__(qwen_module.Qwen3VLAgentMCP)
    BaseAgent.__init__(agent, prompt_sentinel=sentinel)
    agent.model_name = "fake-qwen"
    agent.runtime_conf = {"temperature": 0.0}
    agent.instruction = "wait"
    agent.tools = []
    agent.actions = [{"action_type": "wait"}]
    agent.thoughts = ["previous"]
    agent.conclusions = ["wait"]
    agent.history_images = []
    agent.history_responses = []
    contents = iter(
        (
            "malformed",
            'Thought: done\nAction: wait\n<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>',
        )
    )
    captured_messages: list[Any] = []

    def create(**kwargs: Any) -> _Response:
        captured_messages.append(kwargs["messages"])
        return _Response(next(contents))

    agent.openai_client = _client(create)
    prediction, _action = agent.predict({"screenshot": Image.new("RGB", (2, 2))})
    assert prediction.startswith("Thought: done")
    assert len(captured_messages) == 2
    assert captured_messages[0] is captured_messages[1]
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1


def test_streaming_attempts_in_one_scope_reuse_result_and_original_identity() -> None:
    _bound_codec, request = _fixture_codec_and_request()
    codec = QwenFlatProgressHistoryCodec()
    sink = MemorySentinelReceiptSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.SHADOW,
        policy_factory=_keep_output,
        codec=codec,
        sink=sink,
    )
    agent = _Agent(prompt_sentinel=sentinel)
    attempts = 0
    captured_messages: list[Any] = []

    def create(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        captured_messages.append(kwargs["messages"])

        def chunks():
            yield _Chunk("a")
            if attempts == 1:
                raise RuntimeError("partial stream")
            yield _Chunk("b")

        return chunks()

    agent.openai_client = _client(create)
    kwargs = {key: value for key, value in request.items() if key not in {"model", "messages"}}
    with agent._sentinel_logical_call_scope(attributes={"test": "stream-retry"}):
        first = agent.openai_chat_completions_create(
            model=request["model"], messages=request["messages"], stream=True, **kwargs
        )
        with pytest.raises(RuntimeError, match="partial stream"):
            list(first)
        second = agent.openai_chat_completions_create(
            model=request["model"], messages=request["messages"], stream=True, **kwargs
        )
        assert len(list(second)) == 2
    assert attempts == 2
    assert captured_messages == [request["messages"], request["messages"]]
    assert captured_messages[0] is captured_messages[1]
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1


def test_same_scope_request_drift_is_typed_original_without_second_policy_call() -> None:
    codec, request = _fixture_codec_and_request()
    sentinel, policy = _sentinel(
        mode=SentinelMode.SHADOW,
        policy_factory=_keep_output,
        codec=codec,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    first = call.before_model_call(request)
    drifted = deepcopy(request)
    drifted["temperature"] = 0.1
    second = call.before_model_call(drifted)
    assert first is call.result
    assert second.receipt.fallback_reason is SentinelFallbackReason.REQUEST_DRIFT
    assert second.final_request == drifted
    assert policy.evaluate_count == 1


@pytest.mark.parametrize("call_role", (SentinelCallRole.ACTOR, SentinelCallRole.SENTINEL))
def test_changed_request_after_bypass_remains_bypassed_and_schema_valid(
    call_role: SentinelCallRole,
) -> None:
    codec, request = _fixture_codec_and_request()
    sentinel, policy = _sentinel(mode=SentinelMode.OFF, codec=codec)
    call = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
        call_role=call_role,
    )
    first = call.before_model_call(request)
    changed = deepcopy(request)
    changed["temperature"] = 0.25
    second = call.before_model_call(changed)
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(second.receipt.to_dict())
    assert second.receipt.bypass_reason is first.receipt.bypass_reason
    assert second.receipt.fallback_reason is None
    assert second.final_request == changed
    assert policy.evaluate_count == 0


@pytest.mark.parametrize(
    ("case", "reason"),
    (
        ("invalid_request", SentinelFallbackReason.INVALID_REQUEST_SCHEMA),
        ("unsupported", SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY),
        ("timeout", SentinelFallbackReason.POLICY_TIMEOUT),
        ("exception", SentinelFallbackReason.POLICY_EXCEPTION),
        ("invalid_output", SentinelFallbackReason.INVALID_POLICY_OUTPUT),
        ("renderer", SentinelFallbackReason.INVALID_POLICY_OUTPUT),
        ("renderer_exception", SentinelFallbackReason.RENDERER_FAILURE),
        ("ambiguous", SentinelFallbackReason.AMBIGUOUS_HISTORY_SPAN),
        ("extract_exception", SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE),
        ("invalid_ir", SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE),
        ("wrong_ir_binding", SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE),
        ("invariant", SentinelFallbackReason.INVARIANT_FAILURE),
        ("sidecar", SentinelFallbackReason.SIDECAR_FAILURE),
    ),
)
def test_typed_failures_never_expose_partial_transform(case: str, reason: Any) -> None:
    codec, request = _fixture_codec_and_request()
    selected_request = request
    selected_policy: Any = DeterministicFakeSentinelPolicy(_drop_output)
    selected_codec: Any = codec
    sink: Any = MemorySentinelReceiptSink()

    if case == "invalid_request":
        selected_request = {"model": "x"}
    elif case == "unsupported":
        selected_codec = None
    elif case == "timeout":
        selected_policy = DeterministicFakeSentinelPolicy(
            lambda *_args: (_ for _ in ()).throw(TimeoutError("deadline"))
        )
    elif case == "exception":
        selected_policy = DeterministicFakeSentinelPolicy(
            lambda *_args: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    elif case == "invalid_output":

        class InvalidPolicy:
            policy_id = "invalid-policy"

            def evaluate(self, **_kwargs: Any) -> Any:
                return {"not": "a SentinelPolicyOutput"}

        selected_policy = InvalidPolicy()
    elif case == "renderer":

        def invalid_plan(request_value: JsonValue, context: SentinelContext, ir: HistoryIR):
            output = _drop_output(request_value, context, ir)
            assert output.transformation_plan is not None
            return replace(
                output,
                transformation_plan=replace(
                    output.transformation_plan,
                    source_request_sha256="0" * 64,
                ),
            )

        selected_policy = DeterministicFakeSentinelPolicy(invalid_plan)
    elif case == "renderer_exception":

        class ExplodingRendererCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, request_value: JsonValue) -> HistoryIR:
                return codec.extract(request_value)

            def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
                raise RuntimeError("renderer implementation failed")

        selected_codec = ExplodingRendererCodec()
    elif case == "ambiguous":

        class AmbiguousCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, _request: JsonValue) -> HistoryIR:
                raise PortableContractError("TARGET_BINDING_AMBIGUOUS", "ambiguous")

            def render(self, *args: Any, **kwargs: Any) -> RenderResult:
                raise AssertionError("render must not run")

        selected_codec = AmbiguousCodec()
    elif case == "extract_exception":

        class ExplodingExtractorCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, _request: JsonValue) -> HistoryIR:
                raise RuntimeError("extractor implementation failed")

            def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
                raise AssertionError("render must not run")

        selected_codec = ExplodingExtractorCodec()
    elif case == "invalid_ir":

        class InvalidIrCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, _request: JsonValue) -> Any:
                return {}

            def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
                raise AssertionError("render must not run")

        selected_codec = InvalidIrCodec()
    elif case == "wrong_ir_binding":

        class WrongBindingCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, request_value: JsonValue) -> HistoryIR:
                return replace(codec.extract(request_value), host_id="wrong-host")

            def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
                raise AssertionError("render must not run")

        selected_codec = WrongBindingCodec()
    elif case == "invariant":

        class TamperedCodec:
            codec_id = codec.codec_id
            contract_version = codec.contract_version
            history_family = codec.history_family
            capabilities = codec.capabilities

            def extract(self, request_value: JsonValue) -> HistoryIR:
                return codec.extract(request_value)

            def render(
                self,
                request_value: JsonValue,
                ir: HistoryIR,
                plan: TransformationPlan,
                *,
                execution_mode: ExecutionMode,
                failure_policy: FailurePolicy,
            ) -> RenderResult:
                valid = codec.render(
                    request_value,
                    ir,
                    plan,
                    execution_mode=execution_mode,
                    failure_policy=failure_policy,
                )
                return replace(valid, rendered_request_sha256="0" * 64)

        selected_codec = TamperedCodec()
    elif case == "sidecar":

        class BrokenSink:
            def emit(self, _receipt: Any) -> None:
                raise OSError("disk unavailable")

        sink = BrokenSink()

    registry = HistoryCodecRegistry()
    if selected_codec is not None:
        registry.register(cast(HistoryCodec, selected_codec))
    sentinel = PromptSentinel(
        policy=selected_policy,
        codec_registry=registry,
        host_configs={QWEN_HOST_ID: SentinelHostConfig(mode=SentinelMode.ACTIVE)},
        receipt_sink=sink,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: f"failure-{case}",
    )
    call = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    )
    result = call.before_model_call(selected_request)
    assert result.receipt.fallback_reason is reason
    assert result.final_request == selected_request
    assert result.receipt.edit_applied is False
    assert result.receipt.final_request_sha256 == result.receipt.raw_request_sha256
    if case == "sidecar":
        assert call.before_model_call(selected_request) is result
        assert result.receipt.validation_checks == ("sidecar_admission_failed",)
        assert selected_policy.evaluate_count == 0
    if case == "invalid_output":
        assert result.receipt.policy_output_sha256 == canonical_sha256(
            {"decisions": [], "transformation_plan": None}
        )


def test_sidecar_commit_failure_falls_back_after_one_policy_evaluation() -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []

    def captured_drop(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        outputs.append(output)
        return output

    class FailingTransaction:
        def __init__(self) -> None:
            self.commit_count = 0
            self.abort_count = 0

        def commit(self, _receipt: Any) -> None:
            self.commit_count += 1
            raise OSError("commit unavailable")

        def abort(self) -> None:
            self.abort_count += 1

    class CommitFailingSink:
        def __init__(self) -> None:
            self.begin_count = 0
            self.transaction = FailingTransaction()

        def begin(self, _logical_call_id: str) -> FailingTransaction:
            self.begin_count += 1
            return self.transaction

    sink = CommitFailingSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=captured_drop,
        codec=codec,
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    cached = call.before_model_call(request)
    assert cached is result
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
    assert result.receipt.validation_checks == ("sidecar_commit_failed",)
    assert result.receipt.policy_evaluated is True
    assert result.receipt.policy_output_sha256 == canonical_sha256(outputs[0].to_dict())
    assert policy.evaluate_count == 1
    assert sink.begin_count == 1
    assert sink.transaction.commit_count == 1
    assert sink.transaction.abort_count == 1


def test_sidecar_transaction_cannot_mutate_the_authoritative_result_receipt() -> None:
    codec, request = _fixture_codec_and_request()
    outputs: list[SentinelPolicyOutput] = []

    def captured_drop(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        output = _drop_output(request_value, context, ir)
        outputs.append(output)
        return output

    class MutatingTransaction:
        def __init__(self) -> None:
            self.commit_count = 0
            self.abort_count = 0
            self.receipt: Any = None

        def commit(self, receipt: Any) -> None:
            self.commit_count += 1
            self.receipt = receipt
            object.__setattr__(receipt, "policy_output_sha256", "0" * 64)
            object.__setattr__(receipt, "edit_applied", False)

        def abort(self) -> None:
            self.abort_count += 1

    class MutatingSink:
        def __init__(self) -> None:
            self.begin_count = 0
            self.transaction = MutatingTransaction()

        def begin(self, _logical_call_id: str) -> MutatingTransaction:
            self.begin_count += 1
            return self.transaction

    sink = MutatingSink()
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=captured_drop,
        codec=codec,
        sink=sink,
    )
    call = sentinel.logical_call(host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID)
    result = call.before_model_call(request)
    assert call.before_model_call(request) is result
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
    assert result.receipt.validation_checks == ("sidecar_commit_failed",)
    assert result.receipt.policy_evaluated is True
    assert result.receipt.edit_applied is False
    assert result.receipt.policy_output_sha256 == canonical_sha256(
        SentinelPolicyOutput.to_dict(outputs[0])
    )
    assert sink.transaction.receipt is not result.receipt
    assert policy.evaluate_count == 1
    assert sink.begin_count == 1
    assert sink.transaction.commit_count == 1
    assert sink.transaction.abort_count == 1
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(result.receipt.to_dict())


@pytest.mark.parametrize("case", ("unbound_material", "record_mismatch", "duplicate_operation"))
def test_policy_decisions_must_bind_exact_plan_operations(case: str) -> None:
    codec, request = _fixture_codec_and_request()

    def invalid_binding(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        valid = _drop_output(request_value, context, ir)
        if case == "unbound_material":
            extra = SentinelDecision(
                decision_id=f"{context.logical_call_id}:unbound-drop",
                kind=SentinelDecisionKind.DROP,
                record_id=valid.decisions[0].record_id,
            )
            return replace(valid, decisions=(*valid.decisions, extra))
        if case == "duplicate_operation":
            duplicate = replace(
                valid.decisions[0],
                decision_id=f"{context.logical_call_id}:duplicate-operation",
            )
            return replace(valid, decisions=(*valid.decisions, duplicate))
        return replace(
            valid,
            decisions=(replace(valid.decisions[0], record_id="wrong-record"),),
        )

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=invalid_binding,
        codec=codec,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.receipt.fallback_reason is SentinelFallbackReason.INVALID_POLICY_OUTPUT
    assert result.final_request == request
    assert policy.evaluate_count == 1


def test_bypass_sidecar_failure_preserves_bypass_semantics_and_original() -> None:
    codec, request = _fixture_codec_and_request()

    class BrokenSink:
        def emit(self, _receipt: Any) -> None:
            raise OSError("disk unavailable")

    sentinel, policy = _sentinel(
        mode=SentinelMode.OFF,
        codec=codec,
        sink=BrokenSink(),
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.receipt.bypass_reason.value == "MODE_OFF"
    assert result.receipt.fallback_reason is None
    assert result.receipt.policy_evaluated is False
    assert result.final_request == request
    assert policy.evaluate_count == 0


def test_runtime_ids_are_path_safe_before_external_sidecar_publication() -> None:
    with pytest.raises(ValueError, match="path-safe"):
        SentinelContext(logical_call_id="../escape", host_id=QWEN_HOST_ID)
    with pytest.raises(ValueError, match="path-safe"):
        SentinelContext(logical_call_id="safe", host_id="host/escape")


def test_external_receipt_sink_is_repo_external_owner_only_and_hash_only(tmp_path: Path) -> None:
    codec, request = _fixture_codec_and_request()
    root = tmp_path / "sentinel-sidecars"
    sink = ExternalSentinelReceiptSink(root, repository_root=REPO_ROOT)
    sentinel, _policy = _sentinel(
        mode=SentinelMode.SHADOW,
        policy_factory=_keep_output,
        codec=codec,
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    files = list(root.iterdir())
    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload == result.receipt.to_dict()
    serialized = files[0].read_text(encoding="utf-8")
    assert "已打开设置" not in serialized
    assert "messages" not in payload
    assert payload["request_views_persisted"] is False
    assert payload["exact_diffs_persisted"] is False


def test_external_receipt_transaction_has_no_replaceable_named_temp_during_policy(
    tmp_path: Path,
) -> None:
    codec, request = _fixture_codec_and_request()
    root = tmp_path / "anonymous-sentinel-sidecars"
    sink = ExternalSentinelReceiptSink(root, repository_root=REPO_ROOT)

    def inspect_root_then_keep(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        assert list(root.iterdir()) == []
        return _keep_output(request_value, context, ir)

    sentinel, policy = _sentinel(
        mode=SentinelMode.SHADOW,
        policy_factory=inspect_root_then_keep,
        codec=codec,
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert result.receipt.fallback_reason is None
    assert policy.evaluate_count == 1
    assert [path.name for path in root.iterdir()] == [
        f"{result.receipt.logical_call_id}.sentinel-receipt.v1.json"
    ]


def test_external_receipt_fd_link_capability_fails_before_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request()
    root = tmp_path / "unavailable-fd-link-sidecars"
    sink = ExternalSentinelReceiptSink(root, repository_root=REPO_ROOT)
    real_stat = os.stat

    def hide_proc_fd(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if isinstance(path, str) and path.startswith("/proc/self/fd/"):
            raise FileNotFoundError("injected missing procfs")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("mobile_world.runtime.sentinel.sidecar.os.stat", hide_proc_fd)
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert result.final_request == request
    assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
    assert result.receipt.validation_checks == ("sidecar_admission_failed",)
    assert result.receipt.policy_evaluated is False
    assert policy.evaluate_count == 0
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("mutation", ("chmod", "replace"))
def test_external_receipt_root_change_before_commit_forces_original(
    tmp_path: Path,
    mutation: str,
) -> None:
    codec, request = _fixture_codec_and_request()
    root = tmp_path / "mutable-sentinel-sidecars"
    moved = tmp_path / "moved-sentinel-sidecars"
    sink = ExternalSentinelReceiptSink(root, repository_root=REPO_ROOT)

    def mutate_root_then_edit(
        request_value: JsonValue, context: SentinelContext, ir: HistoryIR
    ) -> SentinelPolicyOutput:
        if mutation == "chmod":
            root.chmod(0o777)
        else:
            root.rename(moved)
            root.mkdir(mode=0o700)
        return _drop_output(request_value, context, ir)

    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        policy_factory=mutate_root_then_edit,
        codec=codec,
        sink=sink,
    )
    try:
        result = sentinel.logical_call(
            host_id=QWEN_HOST_ID,
            history_codec_id=QWEN_CODEC_ID,
        ).before_model_call(request)
        assert result.final_request == request
        assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
        assert result.receipt.validation_checks == ("sidecar_commit_failed",)
        assert result.receipt.policy_evaluated is True
        assert policy.evaluate_count == 1
        assert list(root.iterdir()) == []
        if moved.exists():
            assert list(moved.iterdir()) == []
    finally:
        if mutation == "chmod":
            root.chmod(0o700)


def test_external_receipt_sink_failure_never_exposes_a_partial_final_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec, request = _fixture_codec_and_request()
    root = tmp_path / "transactional-sentinel-sidecars"
    sink = ExternalSentinelReceiptSink(root, repository_root=REPO_ROOT)
    sentinel, policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=codec,
        sink=sink,
    )
    real_write = os.write
    writes = 0

    def partial_then_fail(fd: int, payload: Any) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, payload)
        if writes == 2:
            prefix = memoryview(payload)[:17]
            return real_write(fd, prefix)
        raise OSError("injected transactional write failure")

    monkeypatch.setattr("mobile_world.runtime.sentinel.sidecar.os.write", partial_then_fail)
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    assert result.receipt.fallback_reason is SentinelFallbackReason.SIDECAR_FAILURE
    assert result.receipt.validation_checks == ("sidecar_commit_failed",)
    assert result.receipt.policy_evaluated is True
    assert result.final_request == request
    assert policy.evaluate_count == 1
    assert list(root.iterdir()) == []


def test_receipts_validate_against_checked_in_closed_schema() -> None:
    codec, request = _fixture_codec_and_request()
    sink = MemorySentinelReceiptSink()
    sentinel, _policy = _sentinel(mode=SentinelMode.ACTIVE, codec=codec, sink=sink)
    sentinel.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
    ).before_model_call(request)
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    payload = sink.receipts[0].to_dict()
    validator.validate(payload)

    invalid_mode = deepcopy(payload)
    invalid_mode["configured_mode"] = "OFF"
    with pytest.raises(ValidationError):
        validator.validate(invalid_mode)

    invalid_bypass = deepcopy(payload)
    invalid_bypass["effective_mode"] = "OFF"
    invalid_bypass["decision_kinds"] = []
    invalid_bypass["would_edit"] = False
    invalid_bypass["edit_applied"] = False
    invalid_bypass["fallback_reason"] = None
    invalid_bypass["validation_status"] = "BYPASSED"
    invalid_bypass["bypass_reason"] = None
    invalid_bypass["final_request_sha256"] = invalid_bypass["raw_request_sha256"]
    with pytest.raises(ValidationError):
        validator.validate(invalid_bypass)

    for field, value in (
        ("global_kill_switch_active", True),
        ("history_codec_id", None),
        ("policy_id", None),
        ("decision_kinds", []),
    ):
        invalid_passed = deepcopy(payload)
        invalid_passed[field] = value
        if field == "history_codec_id":
            invalid_passed["history_codec_contract_version"] = None
        with pytest.raises(ValidationError):
            validator.validate(invalid_passed)


def test_closed_schema_accepts_bypass_precedence_with_configured_off() -> None:
    codec, request = _fixture_codec_and_request()
    schema = json.loads(RECEIPT_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    off, _policy = _sentinel(mode=SentinelMode.OFF, codec=codec)
    mode_off = off.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    recursive = off.logical_call(
        host_id=QWEN_HOST_ID,
        history_codec_id=QWEN_CODEC_ID,
        call_role=SentinelCallRole.SENTINEL,
    ).before_model_call(request)

    switch = SentinelGlobalSwitch(active=True)
    killed, _policy = _sentinel(mode=SentinelMode.OFF, codec=codec, switch=switch)
    kill_switch = killed.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)

    for result in (mode_off, recursive, kill_switch):
        validator.validate(result.receipt.to_dict())
    assert mode_off.receipt.bypass_reason.value == "MODE_OFF"
    assert recursive.receipt.bypass_reason.value == "CALL_ROLE_SENTINEL"
    assert kill_switch.receipt.bypass_reason.value == "GLOBAL_KILL_SWITCH"


def test_cached_result_exposes_fresh_objects_without_mutating_raw_or_final() -> None:
    codec, request = _fixture_codec_and_request()
    sentinel, _policy = _sentinel(mode=SentinelMode.ACTIVE, codec=codec)
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    first_final = cast(dict[str, Any], result.final_request)
    first_raw = cast(dict[str, Any], result.raw_request)
    first_final["model"] = "mutated-copy"
    first_raw["model"] = "mutated-copy"
    assert cast(dict[str, Any], result.final_request)["model"] == request["model"]
    assert cast(dict[str, Any], result.raw_request)["model"] == request["model"]
    assert result.final_request is not result.raw_request


def test_receipt_and_result_constructors_reject_cross_field_or_hash_drift() -> None:
    codec, request = _fixture_codec_and_request()
    sentinel, _policy = _sentinel(mode=SentinelMode.ACTIVE, codec=codec)
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)

    with pytest.raises(ValueError, match="configured OFF"):
        replace(result.receipt, configured_mode=SentinelMode.OFF)
    with pytest.raises(ValueError, match="sentinel-role"):
        replace(result.receipt, call_role=SentinelCallRole.SENTINEL)
    with pytest.raises(ValueError, match="kill-off"):
        replace(result.receipt, global_kill_switch_active=True)
    with pytest.raises(ValueError, match="codec, policy, and decisions"):
        replace(result.receipt, policy_id=None)

    tampered = deepcopy(cast(dict[str, Any], result.raw_request))
    tampered["model"] = "tampered-model"
    with pytest.raises(ValueError, match="raw request bytes"):
        SentinelResult(
            receipt=result.receipt,
            _raw_request_json=json.dumps(
                tampered,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            _candidate_request_json=canonical_json_bytes(result.candidate_request),
            _final_request_json=canonical_json_bytes(result.final_request),
        )


def test_external_error_codes_are_redacted_before_receipt_emission() -> None:
    codec, request = _fixture_codec_and_request()

    class UnsafeErrorCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, _request: JsonValue) -> HistoryIR:
            raise PortableContractError(
                "sk_live_secret_ABC123",
                "not persisted",
            )

        def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
            raise AssertionError("render must not run")

    sink = MemorySentinelReceiptSink()
    sentinel, _policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=cast(HistoryCodec, UnsafeErrorCodec()),
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert result.receipt.validation_checks == ("history_extract_failed",)
    assert "sk_live_secret_ABC123" not in json.dumps(result.receipt.to_dict())


def test_external_exception_class_names_are_redacted_before_receipt_emission() -> None:
    codec, request = _fixture_codec_and_request()
    secret_exception = type("sk_live_secret_ABC123", (RuntimeError,), {})

    class UnsafeExceptionCodec:
        codec_id = codec.codec_id
        contract_version = codec.contract_version
        history_family = codec.history_family
        capabilities = codec.capabilities

        def extract(self, _request: JsonValue) -> HistoryIR:
            raise secret_exception("not persisted")

        def render(self, *_args: Any, **_kwargs: Any) -> RenderResult:
            raise AssertionError("render must not run")

    sink = MemorySentinelReceiptSink()
    sentinel, _policy = _sentinel(
        mode=SentinelMode.ACTIVE,
        codec=cast(HistoryCodec, UnsafeExceptionCodec()),
        sink=sink,
    )
    result = sentinel.logical_call(
        host_id=QWEN_HOST_ID, history_codec_id=QWEN_CODEC_ID
    ).before_model_call(request)
    assert result.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert result.receipt.validation_checks == ("history_extract_exception",)
    assert "sk_live_secret_ABC123" not in json.dumps(result.receipt.to_dict())
