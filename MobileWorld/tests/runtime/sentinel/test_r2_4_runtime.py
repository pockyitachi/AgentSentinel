from __future__ import annotations

import base64
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations import mai_ui_agent as mai_module
from mobile_world.agents.implementations import qwen3vl as qwen_module
from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    HistoryIR,
    JsonPath,
    JsonValue,
    OperationKind,
    PlanOperation,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    set_at_path,
    stable_id,
)
from mobile_world.runtime.sentinel import (
    DeterministicFakeSentinelPolicy,
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelContext,
    SentinelDecision,
    SentinelDecisionKind,
    SentinelFallbackReason,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
    SentinelPolicyOutput,
)
from mobile_world.runtime.sentinel.contracts import SentinelCallRole
from mobile_world.runtime.sentinel.r2_2.contracts import (
    POLICY_PROPOSAL_SCHEMA_VERSION,
    CurrentObservationV1,
    EvidenceCutoffV1,
    EvidenceEntryV1,
    EvidenceInputExclusionsV1,
    EvidenceMediaType,
    EvidenceRelation,
    EvidenceRole,
    EvidenceSemanticScope,
    ImageEvidenceProjectionV1,
    RuntimeOperationKind,
    SourceEventType,
    TaskInstructionDataV1,
    TextEvidenceProjectionV1,
    evidence_packet_sha256,
    validate_runtime_policy_proposal,
)
from mobile_world.runtime.sentinel.r2_2.evidence import (
    CausalEvidenceSnapshotV1,
    EvidencePacketBuilder,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56_REQUESTED_MODEL,
    SUPPORTED_OPENAI_SDK_VERSION,
    GPT56SentinelPolicy,
    ProposalSchemaSnapshotV1,
    ResponsesEnvelopeV1,
    ResponsesRequestV1,
    TransportDescriptorV1,
)
from mobile_world.runtime.sentinel.r2_2.metrics import R22PolicyMetrics
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import (
    admission_receipt_projector,
    bind_policy_receipt,
    make_gpt_evidence_input,
    make_proposal_admission,
    parse_runtime_policy_proposal,
)
from mobile_world.runtime.sentinel.r2_2.sidecar import (
    MemoryR22PolicyReceiptSink,
    PolicyEvaluationStatus,
)
from mobile_world.runtime.sentinel.r2_4 import (
    R22CpuFakeActivePolicyAdapter,
    RuntimeEditableSpanCodecV1,
    RuntimeHistoryExtractionResultV1,
    RuntimeHistoryExtractionStatusV1,
    RuntimeVerticalBridgeStatus,
    RuntimeVerticalReceiptBridgeV1,
    RuntimeVerticalSentinelResultV1,
    RuntimeVerticalStatus,
    build_runtime_history_codec_resolver,
    issue_cpu_fake_active_authority,
    vertical_sentinel_result_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeReplacementTemplate,
    RuntimeVerticalAdmittedPlanV1,
    RuntimeVerticalOperationV1,
    replacement_text_for_template,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import history_policy_transport_schema_v1
from mobile_world.runtime.sentinel.r2_4.renderer import (
    render_vertical_admitted_plan,
    restore_vertical_original,
    validate_vertical_render_result,
)

QWEN_HOST_ID = cast(str, qwen_module.Qwen3VLAgentMCP.sentinel_host_id)
MAI_HOST_ID = cast(str, mai_module.MAIUINaivigationAgent.sentinel_host_id)
QWEN_CODEC_ID = cast(str, qwen_module.Qwen3VLAgentMCP.sentinel_history_codec_id)
MAI_CODEC_ID = cast(str, mai_module.MAIUINaivigationAgent.sentinel_history_codec_id)

QWEN_STALE_CLAIM = "stale qwen history claim"
MAI_STALE_CLAIM = "stale mai history claim"
QWEN_RESPONSE = (
    "Thought: done\nAction: wait\n"
    '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
)
MAI_RESPONSE = (
    "<thinking>wait for the stable screen</thinking>"
    '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
)


class _Response:
    def __init__(self, content: str) -> None:
        self.usage = None
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


def _client(create: Any) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create),
        )
    )


def _first_editable(ir: HistoryIR) -> tuple[Any, Any]:
    for record in ir.records:
        for span in record.editable_spans:
            if span.span_role is SpanRole.EDITABLE_CLAIM:
                return record, span
    raise AssertionError("production-shaped request has no runtime editable span")


def _drop_output(
    _request: JsonValue,
    context: SentinelContext,
    ir: HistoryIR,
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


def _forbidden_output(
    _request: JsonValue,
    _context: SentinelContext,
    _ir: HistoryIR,
) -> SentinelPolicyOutput:
    raise AssertionError("OFF mode must not perform semantic work")


def _sentinel(
    *,
    mode: SentinelMode,
    policy_factory: Any = _drop_output,
    host_modes: dict[str, SentinelMode] | None = None,
) -> tuple[PromptSentinel, DeterministicFakeSentinelPolicy, MemorySentinelReceiptSink]:
    policy = DeterministicFakeSentinelPolicy(policy_factory)
    sink = MemorySentinelReceiptSink()
    modes = host_modes or {QWEN_HOST_ID: mode, MAI_HOST_ID: mode}
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={
            host_id: SentinelHostConfig(mode=host_mode) for host_id, host_mode in modes.items()
        },
        receipt_sink=sink,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=iter(
            f"r24-logical-call-{index}" for index in range(1, 20)
        ).__next__,
    )
    return sentinel, policy, sink


def _qwen_agent(prompt_sentinel: PromptSentinel) -> Any:
    agent = qwen_module.Qwen3VLAgentMCP.__new__(qwen_module.Qwen3VLAgentMCP)
    BaseAgent.__init__(agent, prompt_sentinel=prompt_sentinel)
    agent.model_name = "fake-qwen"
    agent.runtime_conf = {"temperature": 0.0}
    agent.instruction = "Wait on the current screen."
    agent.tools = []
    agent.actions = [{"action_type": "wait"}]
    agent.thoughts = ["old thought"]
    agent.conclusions = [QWEN_STALE_CLAIM]
    agent.history_images = []
    agent.history_responses = []
    return agent


def _mai_agent(prompt_sentinel: PromptSentinel) -> Any:
    agent = mai_module.MAIUINaivigationAgent.__new__(mai_module.MAIUINaivigationAgent)
    BaseAgent.__init__(agent, prompt_sentinel=prompt_sentinel)
    agent.model_name = "fake-mai"
    agent.instruction = "Wait on the current screen."
    agent.max_tokens = 2048
    agent.temperature = 0.0
    agent.top_p = 1.0
    agent.history_n = 3
    agent.tools = []
    agent.history_images = [(Image.new("RGB", (3, 3), "red"), None, None)]
    agent.history_responses = [
        {
            "role": "assistant",
            "content": (
                f"<thinking>{MAI_STALE_CLAIM}</thinking>"
                '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}'
                "</tool_call>"
            ),
        }
    ]
    return agent


def _run_host(
    host: str,
    prompt_sentinel: PromptSentinel,
    *,
    responses: tuple[str | BaseException, ...] | None = None,
) -> tuple[str, Any, tuple[dict[str, Any], ...]]:
    if host == "qwen":
        agent = _qwen_agent(prompt_sentinel)
        scripted = iter(responses or (QWEN_RESPONSE,))
    elif host == "mai":
        agent = _mai_agent(prompt_sentinel)
        scripted = iter(responses or (MAI_RESPONSE,))
    else:  # pragma: no cover - closed test helper input
        raise AssertionError(f"unsupported test host: {host}")
    captured: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _Response:
        captured.append(kwargs)
        outcome = next(scripted)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response(outcome)

    agent.openai_client = _client(create)
    returned, action = agent.predict({"screenshot": Image.new("RGB", (4, 4), "blue")})
    return returned, action, tuple(captured)


@dataclass(frozen=True)
class _AdapterCase:
    host: str
    request: dict[str, JsonValue]
    history_ir: HistoryIR
    context: SentinelContext
    packet: Any
    current_image_data_url: str


def _current_image(
    request: dict[str, JsonValue],
) -> tuple[JsonPath, dict[str, JsonValue], str, int, int]:
    found: list[tuple[JsonPath, dict[str, JsonValue], str]] = []

    def visit(value: JsonValue, path: JsonPath) -> None:
        if type(value) is dict:
            mapping = cast(dict[str, JsonValue], value)
            image_url = mapping.get("image_url")
            if mapping.get("type") == "image_url" and type(image_url) is dict:
                url = cast(dict[str, JsonValue], image_url).get("url")
                if type(url) is str:
                    found.append((path, mapping, url))
            for key, child in mapping.items():
                visit(child, (*path, key))
        elif type(value) is list:
            for index, child in enumerate(cast(list[JsonValue], value)):
                visit(child, (*path, index))

    visit(cast(JsonValue, request), ())
    assert found
    path, block, data_url = found[-1]
    encoded = data_url.split(",", 1)[1]
    with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as image:
        width, height = image.size
    return path, block, data_url, width, height


def _evidence_text(
    *,
    evidence_id: str,
    event_seq: int,
    text: str,
) -> EvidenceEntryV1:
    projection = TextEvidenceProjectionV1.from_text(text)
    return EvidenceEntryV1(
        evidence_id=evidence_id,
        role=EvidenceRole.PRIOR_POST_UI_STATE,
        semantic_scope=EvidenceSemanticScope.PAST_EVENT_FACT,
        source_event_id=f"event-{evidence_id}",
        source_event_type=SourceEventType.TRANSITION_FAILED,
        source_event_seq=event_seq,
        task_run_id="r24-task-run",
        caused_by_event_id=f"action-parent-{event_seq}",
        wall_time=f"2026-09-02T00:00:{event_seq:02d}Z",
        monotonic_ns=event_seq * 1_000,
        payload_sha256=projection.text_sha256,
        projection=projection,
    )


def _build_adapter_case(
    host: str,
    *,
    zero_targets: bool = False,
    request_override: dict[str, JsonValue] | None = None,
) -> _AdapterCase:
    codec_id = QWEN_CODEC_ID if host == "qwen" else MAI_CODEC_ID
    host_id = QWEN_HOST_ID if host == "qwen" else MAI_HOST_ID
    if request_override is None:
        off, _policy, _sink = _sentinel(
            mode=SentinelMode.OFF,
            policy_factory=_forbidden_output,
        )
        _returned, _action, provider_calls = _run_host(host, off)
        request = cast(dict[str, JsonValue], copy_json(cast(JsonValue, provider_calls[0])))
    else:
        request = cast(dict[str, JsonValue], copy_json(cast(JsonValue, request_override)))
    history_ir = build_runtime_history_codec_resolver().by_id(codec_id).extract(request)
    if zero_targets:
        history_ir = replace(
            history_ir,
            records=tuple(replace(record, editable_spans=()) for record in history_ir.records),
        )
    context = SentinelContext(
        logical_call_id=f"r24-adapter-{host}",
        host_id=host_id,
    )
    image_path, image_block, image_url, width, height = _current_image(request)
    image_bytes = base64.b64decode(image_url.split(",", 1)[1], validate=True)
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    image_value_sha256 = canonical_sha256(cast(JsonValue, image_block))
    current = CurrentObservationV1(
        source_event_id="r24-current-observation-10",
        source_event_seq=10,
        screenshot_evidence_id="r24-current-screen",
        screenshot_content_sha256=image_sha256,
        actor_request_image_path=image_path,
        actor_request_image_value_sha256=image_value_sha256,
        media_type=EvidenceMediaType.PNG,
        width=width,
        height=height,
        accessibility_evidence_ids=(),
    )
    screenshot = EvidenceEntryV1(
        evidence_id=current.screenshot_evidence_id,
        role=EvidenceRole.CURRENT_UI_SCREENSHOT,
        semantic_scope=EvidenceSemanticScope.CURRENT_STATE_ONLY,
        source_event_id=current.source_event_id,
        source_event_type=SourceEventType.STEP_STARTED,
        source_event_seq=current.source_event_seq,
        task_run_id="r24-task-run",
        caused_by_event_id=None,
        wall_time="2026-09-02T00:00:10Z",
        monotonic_ns=10_000,
        payload_sha256=image_sha256,
        projection=ImageEvidenceProjectionV1(
            content_sha256=image_sha256,
            request_value_sha256=image_value_sha256,
            media_type=EvidenceMediaType.PNG,
            width=width,
            height=height,
        ),
    )
    snapshot = CausalEvidenceSnapshotV1(
        cutoff=EvidenceCutoffV1(
            run_id="r24-run",
            task_run_id="r24-task-run",
            step_id="r24-step-10",
            current_observation_event_id=current.source_event_id,
            cutoff_event_seq=10,
            actor_request_sha256=canonical_sha256(cast(JsonValue, request)),
        ),
        task=TaskInstructionDataV1.create(
            source_event_id="r24-task-started",
            source_event_seq=1,
            exact_text="Wait on the current screen.",
        ),
        current_observation=current,
        evidence_index=(
            _evidence_text(
                evidence_id="r24-direct-refute",
                event_seq=6,
                text="The post-action UI directly contradicts the retained history claim.",
            ),
            screenshot,
        ),
        input_exclusions=EvidenceInputExclusionsV1(),
    )
    packet = EvidencePacketBuilder().build(
        request=cast(JsonValue, request),
        context=context,
        history_ir=history_ir,
        snapshot=snapshot,
    )
    return _AdapterCase(
        host=host,
        request=request,
        history_ir=history_ir,
        context=context,
        packet=packet,
        current_image_data_url=image_url,
    )


def _synthetic_history_output_with_observed_violation_shape() -> str:
    decisions: list[JsonValue] = []
    for index in range(3):
        decisions.append(
            {
                "confidence_millis": 990,
                "decision_id": f"decision-synthetic-{index}",
                "evidence_refs": [],
                "factual_verdict": "UNVERIFIABLE",
                "fallback_status": "NONE",
                "proposed_operation": "KEEP_UNCERTAIN",
                "rationale_summary": "Synthetic evidence is insufficient.",
                "reason_code": "TEMPORAL_PROVENANCE_MISSING",
                "replacement_fact_id": None,
                "target_id": f"target-synthetic-{index}",
                "temporal_validity": "UNKNOWN",
                "uncertainty_codes": [
                    "EVIDENCE_MISSING",
                    "TEMPORAL_PROVENANCE_MISSING",
                ],
            }
        )
    value: dict[str, JsonValue] = {
        "action_or_tool_authority": False,
        "automatic": True,
        "curated": False,
        "decisions": decisions,
        "deployment_prediction": True,
        "evidence_packet_sha256": hashlib.sha256(b"synthetic-actor-request-not-packet").hexdigest(),
        "packet_id": "r22pkt:synthetic-observed-regression",
        "schema_version": "mobileworld.runtime.sentinel-policy-proposal/v1",
        "status": "COMPLETE",
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_observed_violation_shape_stays_rejected_and_corrected_output_is_admitted() -> None:
    """Regress the observed violations using synthetic, repository-safe bytes."""

    raw_text = _synthetic_history_output_with_observed_violation_shape()
    raw = cast(dict[str, JsonValue], json.loads(raw_text))
    assert json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == raw_text
    Draft202012Validator(history_policy_transport_schema_v1().as_dict()).validate(raw)

    full_validator = Draft202012Validator(ProposalSchemaSnapshotV1.from_checked_in().as_dict())
    errors = tuple(full_validator.iter_errors(raw))
    assert len(errors) == 4
    assert {tuple(error.absolute_path) for error in errors} == {
        ("decisions",),
        ("decisions", 0, "fallback_status"),
        ("decisions", 1, "fallback_status"),
        ("decisions", 2, "fallback_status"),
    }

    built = _build_adapter_case("qwen")
    decisions = cast(list[dict[str, JsonValue]], raw["decisions"])
    target_ids = tuple(cast(str, decision["target_id"]) for decision in decisions)
    assert len(built.packet.targets) == 1
    packet = replace(
        built.packet,
        packet_id=cast(str, raw["packet_id"]),
        targets=tuple(
            replace(built.packet.targets[0], target_id=target_id) for target_id in target_ids
        ),
    )
    corrected = cast(dict[str, JsonValue], deepcopy(raw))
    corrected["status"] = "ABSTAIN"
    corrected["evidence_packet_sha256"] = evidence_packet_sha256(packet)
    for decision in cast(list[dict[str, JsonValue]], corrected["decisions"]):
        decision["fallback_status"] = "ABSTAIN_TO_ORIGINAL"

    assert json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == raw_text
    assert not tuple(full_validator.iter_errors(corrected))
    proposal = parse_runtime_policy_proposal(corrected)
    assert validate_runtime_policy_proposal(proposal, packet) == (
        "r22.schema_exact",
        "r22.target_census_bound",
        "r22.evidence_refs_bound",
        "r22.temporal_cutoff_bound",
        "r22.replacement_fact_bound",
        "r22.no_action_surface",
    )


def _no_history_request(built: _AdapterCase) -> dict[str, JsonValue]:
    request = cast(dict[str, JsonValue], copy_json(cast(JsonValue, built.request)))
    messages = cast(list[JsonValue], request["messages"])
    if built.host == "qwen":
        user = cast(dict[str, JsonValue], messages[1])
        content = cast(list[JsonValue], user["content"])
        text_block = cast(dict[str, JsonValue], content[0])
        text = cast(str, text_block["text"])
        text_block["text"] = text[: text.index("Step 1: ")] + "\n"
    else:
        request["messages"] = [
            copy_json(messages[0]),
            copy_json(messages[1]),
            copy_json(messages[-1]),
        ]
    return request


def _evidence_ref(packet: Any) -> dict[str, JsonValue]:
    evidence = next(
        item for item in packet.evidence_index if item.evidence_id == "r24-direct-refute"
    )
    return {
        "evidence_id": evidence.evidence_id,
        "payload_sha256": evidence.payload_sha256,
        "relation": EvidenceRelation.REFUTES.value,
    }


def _r22_proposal(packet: Any) -> dict[str, JsonValue]:
    decisions: list[JsonValue] = []
    for index, target in enumerate(packet.targets):
        if index == 0:
            decisions.append(
                {
                    "decision_id": "r24-source-decision-0",
                    "target_id": target.target_id,
                    "factual_verdict": "REFUTED",
                    "temporal_validity": "N_A",
                    "proposed_operation": "DROP",
                    "evidence_refs": [_evidence_ref(packet)],
                    "confidence_millis": 950,
                    "reason_code": "DIRECT_EVIDENCE_REFUTATION",
                    "uncertainty_codes": [],
                    "rationale_summary": "Direct prior UI evidence refutes the claim.",
                    "replacement_fact_id": None,
                    "fallback_status": "NONE",
                }
            )
        else:
            decisions.append(
                {
                    "decision_id": f"r24-source-decision-{index}",
                    "target_id": target.target_id,
                    "factual_verdict": "UNVERIFIABLE",
                    "temporal_validity": "UNKNOWN",
                    "proposed_operation": "KEEP_UNCERTAIN",
                    "evidence_refs": [],
                    "confidence_millis": 0,
                    "reason_code": "INSUFFICIENT_EVIDENCE",
                    "uncertainty_codes": ["EVIDENCE_MISSING"],
                    "rationale_summary": "The bounded evidence is insufficient.",
                    "replacement_fact_id": None,
                    "fallback_status": "ABSTAIN_TO_ORIGINAL",
                }
            )
    return {
        "schema_version": POLICY_PROPOSAL_SCHEMA_VERSION,
        "packet_id": packet.packet_id,
        "evidence_packet_sha256": evidence_packet_sha256(packet),
        "status": (
            "ABSTAIN" if not decisions else "PARTIAL_ABSTAIN" if len(decisions) > 1 else "COMPLETE"
        ),
        "automatic": True,
        "curated": False,
        "deployment_prediction": True,
        "action_or_tool_authority": False,
        "decisions": decisions,
    }


class _R22FakeTransport:
    def __init__(
        self,
        output_text: str,
        *,
        descriptor: TransportDescriptorV1 | None = None,
        entered: Event | None = None,
        release: Event | None = None,
    ) -> None:
        self._descriptor = descriptor or TransportDescriptorV1.cpu_fake()
        self._output_text = output_text
        self._entered = entered
        self._release = release
        self.calls: list[tuple[ResponsesRequestV1, SentinelCallRole, float]] = []

    @property
    def descriptor(self) -> TransportDescriptorV1:
        return self._descriptor

    def create(
        self,
        request: ResponsesRequestV1,
        *,
        call_role: SentinelCallRole = SentinelCallRole.SENTINEL,
        timeout_seconds: float,
    ) -> ResponsesEnvelopeV1:
        self.calls.append((request, call_role, timeout_seconds))
        if self._entered is not None:
            self._entered.set()
        if self._release is not None and not self._release.wait(timeout=2):
            raise TimeoutError("test transport release was not signalled")
        return ResponsesEnvelopeV1(
            response_id="r24-fake-response",
            requested_model=GPT56_REQUESTED_MODEL,
            returned_model=GPT56_REQUESTED_MODEL,
            status="completed",
            service_tier="default",
            output_text=self._output_text,
            input_tokens=100,
            output_tokens=40,
            total_tokens=140,
        )


def _source_policy(
    built: _AdapterCase,
    transport: _R22FakeTransport,
) -> tuple[GPT56SentinelPolicy[Any, Any], MemoryR22PolicyReceiptSink]:
    receipt_sink = MemoryR22PolicyReceiptSink()
    evidence = make_gpt_evidence_input(
        built.packet,
        current_image_data_url=built.current_image_data_url,
    )
    policy = GPT56SentinelPolicy(
        transport=transport,
        evidence_packet_factory=lambda _request, _context, _history_ir: evidence,
        proposal_admission=make_proposal_admission(
            packet=built.packet,
            source_request=cast(JsonValue, built.request),
            history_ir=built.history_ir,
        ),
        admission_receipt_projector=admission_receipt_projector,
        bind_policy_receipt=bind_policy_receipt,
        receipt_sink=receipt_sink,
        metrics=R22PolicyMetrics(),
        output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
        timeout_seconds=0.05,
        seam_policy_deadline_seconds=2.0,
    )
    return policy, receipt_sink


def _active_adapter(
    built: _AdapterCase,
    transport: _R22FakeTransport,
    *,
    replace_first_target: bool = False,
) -> tuple[
    R22CpuFakeActivePolicyAdapter,
    GPT56SentinelPolicy[Any, Any],
    MemoryR22PolicyReceiptSink,
]:
    source, receipt_sink = _source_policy(built, transport)
    replace_targets = (
        (built.packet.targets[0].target_id,)
        if replace_first_target and built.packet.targets
        else ()
    )
    adapter = R22CpuFakeActivePolicyAdapter(
        source,
        authority=issue_cpu_fake_active_authority(),
        replace_drop_target_ids=replace_targets,
    )
    return adapter, source, receipt_sink


def _vertical_sentinel(
    built: _AdapterCase,
    adapter: R22CpuFakeActivePolicyAdapter,
    *,
    policy_timeout_ms: int = 1_000,
) -> tuple[PromptSentinel, MemorySentinelReceiptSink]:
    sink = MemorySentinelReceiptSink()
    sentinel = PromptSentinel(
        policy=adapter,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={
            built.context.host_id: SentinelHostConfig(
                mode=SentinelMode.ACTIVE,
                policy_timeout_ms=policy_timeout_ms,
            )
        },
        receipt_sink=sink,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: built.context.logical_call_id,
    )
    return sentinel, sink


def _expected_single_drop(
    original: dict[str, Any],
    *,
    codec_id: str,
) -> dict[str, Any]:
    codec = build_runtime_history_codec_resolver().by_id(codec_id)
    ir = codec.extract(cast(JsonValue, original))
    _record, span = _first_editable(ir)
    old_text = get_at_path(cast(JsonValue, original), span.container_path)
    assert isinstance(old_text, str)
    expected = copy_json(cast(JsonValue, original))
    set_at_path(
        expected,
        span.container_path,
        old_text[: span.char_start] + old_text[span.char_end :],
    )
    return cast(dict[str, Any], expected)


def _vertical_plan(
    original: dict[str, Any],
    *,
    codec_id: str,
    kind: RuntimeOperationKind,
) -> tuple[HistoryIR, RuntimeVerticalAdmittedPlanV1, str]:
    codec = build_runtime_history_codec_resolver().by_id(codec_id)
    ir = codec.extract(cast(JsonValue, original))
    record, span = _first_editable(ir)
    operation = RuntimeVerticalOperationV1(
        operation_id="r24-op-1",
        decision_id="r24-decision-1",
        target_id="r24-target-1",
        target_record_id=record.record_id,
        target_span_sha256=span.span_sha256,
        kind=kind,
        source_operation_sha256="a" * 64,
        replacement_template=(
            RuntimeReplacementTemplate.REFUTED_HISTORY_FACT_V1
            if kind is RuntimeOperationKind.REPLACE
            else None
        ),
    )
    plan = RuntimeVerticalAdmittedPlanV1(
        plan_id="r24-plan-1",
        logical_call_id="r24-logical-call-render",
        host_id=ir.host_id,
        history_family=ir.history_family.value,
        history_codec_id=ir.codec_id,
        history_codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        source_policy_output_sha256="b" * 64,
        source_policy_receipt_sha256="c" * 64,
        source_r22_admitted_plan_sha256="d" * 64,
        source_transport_descriptor_sha256="e" * 64,
        operations=(operation,),
    )
    replacement = (
        replacement_text_for_template(RuntimeReplacementTemplate.REFUTED_HISTORY_FACT_V1)
        if kind is RuntimeOperationKind.REPLACE
        else ""
    )
    return ir, plan, replacement


@pytest.mark.parametrize(
    ("host", "codec_id", "stale_claim"),
    (
        ("qwen", QWEN_CODEC_ID, QWEN_STALE_CLAIM),
        ("mai", MAI_CODEC_ID, MAI_STALE_CLAIM),
    ),
)
def test_production_host_seam_matrix_with_deterministic_fake(
    host: str,
    codec_id: str,
    stale_claim: str,
) -> None:
    off, off_policy, off_sink = _sentinel(
        mode=SentinelMode.OFF,
        policy_factory=_forbidden_output,
    )
    off_returned, off_action, off_calls = _run_host(host, off)
    assert off_policy.evaluate_count == 0
    assert len(off_calls) == len(off_sink.receipts) == 1
    original = off_calls[0]

    shadow, shadow_policy, shadow_sink = _sentinel(mode=SentinelMode.SHADOW)
    shadow_returned, shadow_action, shadow_calls = _run_host(host, shadow)
    assert shadow_policy.evaluate_count == 1
    assert len(shadow_calls) == len(shadow_sink.receipts) == 1
    assert shadow_calls[0] == original
    assert shadow_sink.receipts[0].would_edit is True
    assert shadow_sink.receipts[0].edit_applied is False

    active, active_policy, active_sink = _sentinel(mode=SentinelMode.ACTIVE)
    active_returned, active_action, active_calls = _run_host(host, active)
    assert active_policy.evaluate_count == 1
    assert len(active_calls) == len(active_sink.receipts) == 1
    expected = _expected_single_drop(original, codec_id=codec_id)
    assert active_calls[0] == expected
    assert stale_claim not in repr(active_calls[0])
    assert active_sink.receipts[0].would_edit is True
    assert active_sink.receipts[0].edit_applied is True

    # The unchanged host provider normalization and parser still own the action.
    assert off_returned == shadow_returned == active_returned
    assert off_action == shadow_action == active_action
    assert active_action.action_type == "wait"

    # Exact expected-request equality proves every non-target field, role/order,
    # multimodal block, image data URL, and provider envelope remained invariant.
    assert active_calls[0]["model"] == original["model"]
    assert active_calls[0]["messages"] != original["messages"]


def test_qwen_outer_parser_retry_reuses_one_active_runtime_result() -> None:
    sentinel, policy, sink = _sentinel(mode=SentinelMode.ACTIVE)
    returned, action, calls = _run_host(
        "qwen",
        sentinel,
        responses=("malformed", QWEN_RESPONSE),
    )

    assert returned == QWEN_RESPONSE
    assert action.action_type == "wait"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 1
    assert sink.receipts[0].edit_applied is True


def test_per_host_off_mode_does_not_disable_the_other_active_host() -> None:
    sentinel, policy, sink = _sentinel(
        mode=SentinelMode.OFF,
        host_modes={
            QWEN_HOST_ID: SentinelMode.OFF,
            MAI_HOST_ID: SentinelMode.ACTIVE,
        },
    )

    _qwen_returned, _qwen_action, qwen_calls = _run_host("qwen", sentinel)
    _mai_returned, _mai_action, mai_calls = _run_host("mai", sentinel)

    assert QWEN_STALE_CLAIM in repr(qwen_calls[0])
    assert MAI_STALE_CLAIM not in repr(mai_calls[0])
    assert policy.evaluate_count == 1
    assert len(sink.receipts) == 2
    by_host = {receipt.host_id: receipt for receipt in sink.receipts}
    assert by_host[QWEN_HOST_ID].policy_evaluated is False
    assert by_host[MAI_HOST_ID].edit_applied is True


@pytest.mark.parametrize(
    ("host", "codec_id"),
    (("qwen", QWEN_CODEC_ID), ("mai", MAI_CODEC_ID)),
)
@pytest.mark.parametrize(
    "kind",
    (RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE),
)
def test_runtime_vertical_renderer_exact_edit_and_restore_for_both_hosts(
    host: str,
    codec_id: str,
    kind: RuntimeOperationKind,
) -> None:
    off, _policy, _sink = _sentinel(
        mode=SentinelMode.OFF,
        policy_factory=_forbidden_output,
    )
    _returned, _action, calls = _run_host(host, off)
    original = calls[0]
    original_before = copy_json(cast(JsonValue, original))
    ir, plan, replacement = _vertical_plan(
        original,
        codec_id=codec_id,
        kind=kind,
    )

    result = render_vertical_admitted_plan(cast(JsonValue, original), ir, plan)
    assert (
        validate_vertical_render_result(cast(JsonValue, original), ir, plan, result)
        == result.validation_checks
    )
    assert restore_vertical_original(result) == original
    assert original == original_before
    assert result.edit_applied is True
    assert len(result.text_diffs) == 1
    assert result.text_diffs[0].rendered_text == replacement

    record, span = _first_editable(ir)
    assert record.record_id == plan.operations[0].target_record_id
    expected = copy_json(cast(JsonValue, original))
    source_text = get_at_path(expected, span.container_path)
    assert isinstance(source_text, str)
    set_at_path(
        expected,
        span.container_path,
        source_text[: span.char_start] + replacement + source_text[span.char_end :],
    )
    assert result.candidate_request == expected


@pytest.mark.parametrize(
    ("host", "codec_id", "stale_claim"),
    (
        ("qwen", QWEN_CODEC_ID, QWEN_STALE_CLAIM),
        ("mai", MAI_CODEC_ID, MAI_STALE_CLAIM),
    ),
)
@pytest.mark.parametrize(
    ("replace_first_target", "expected_kind", "expected_text"),
    (
        (False, RuntimeOperationKind.DROP, ""),
        (
            True,
            RuntimeOperationKind.REPLACE,
            replacement_text_for_template(RuntimeReplacementTemplate.REFUTED_HISTORY_FACT_V1),
        ),
    ),
    ids=("drop", "fixed-template-replace"),
)
def test_real_r22_adapter_active_path_edits_production_host_request(
    host: str,
    codec_id: str,
    stale_claim: str,
    replace_first_target: bool,
    expected_kind: RuntimeOperationKind,
    expected_text: str,
) -> None:
    built = _build_adapter_case(host)
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(
        built,
        transport,
        replace_first_target=replace_first_target,
    )
    sentinel, seam_sink = _vertical_sentinel(built, adapter)

    returned, action, provider_calls = _run_host(host, sentinel)

    assert returned == (QWEN_RESPONSE if host == "qwen" else MAI_RESPONSE)
    assert action.action_type == "wait"
    assert source.evaluate_count == len(transport.calls) == 1
    assert transport.calls[0][1] is SentinelCallRole.SENTINEL
    assert transport.descriptor == TransportDescriptorV1.cpu_fake()
    assert len(policy_sink.receipts) == len(seam_sink.receipts) == 1
    assert policy_sink.receipts[0].evaluation_status is PolicyEvaluationStatus.ADMITTED
    assert seam_sink.receipts[0].fallback_reason is None
    assert seam_sink.receipts[0].edit_applied is True
    assert seam_sink.receipts[0].decision_kinds == (SentinelDecisionKind(expected_kind.value),)

    provider_request = cast(dict[str, JsonValue], provider_calls[0])
    record, span = _first_editable(built.history_ir)
    source_text = get_at_path(cast(JsonValue, built.request), span.container_path)
    assert isinstance(source_text, str)
    expected = copy_json(cast(JsonValue, built.request))
    set_at_path(
        expected,
        span.container_path,
        source_text[: span.char_start] + expected_text + source_text[span.char_end :],
    )
    assert provider_request == expected
    assert stale_claim not in repr(provider_request)
    assert record.record_id


@pytest.mark.parametrize(
    ("host", "codec_id"),
    (("qwen", QWEN_CODEC_ID), ("mai", MAI_CODEC_ID)),
)
def test_material_vertical_result_binds_overlay_and_cache_returns_fresh_snapshots(
    host: str,
    codec_id: str,
) -> None:
    built = _build_adapter_case(host)
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)
    logical_call = sentinel.logical_call(
        host_id=built.context.host_id,
        history_codec_id=codec_id,
    )

    first = logical_call.before_model_call(cast(JsonValue, built.request))
    second = logical_call.before_model_call(cast(JsonValue, built.request))
    cached = logical_call.result

    assert type(first) is type(second) is type(cached) is RuntimeVerticalSentinelResultV1
    assert first is not second and second is not cached
    assert vertical_sentinel_result_sha256(first) == vertical_sentinel_result_sha256(second)
    assert first.bridge.status is RuntimeVerticalBridgeStatus.EVALUATED
    assert first.bridge.policy_evaluated is True
    assert first.bridge.target_count == len(built.packet.targets) == 1
    assert first.bridge.policy_output_sha256 == first.receipt.policy_output_sha256
    assert first.final_request != first.raw_request
    overlay = build_runtime_history_codec_resolver().by_id(codec_id).overlay_declaration
    assert first.overlay_declaration_sha256 == overlay.sha256
    assert source.evaluate_count == len(transport.calls) == 1
    assert len(policy_sink.receipts) == len(seam_sink.receipts) == 1


@pytest.mark.parametrize(
    ("host", "codec_id"),
    (("qwen", QWEN_CODEC_ID), ("mai", MAI_CODEC_ID)),
)
def test_no_history_returns_explicit_overlay_bound_bridge_without_policy(
    host: str,
    codec_id: str,
) -> None:
    built = _build_adapter_case(host)
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)
    request = _no_history_request(built)
    logical_call = sentinel.logical_call(
        host_id=built.context.host_id,
        history_codec_id=codec_id,
    )

    first = logical_call.before_model_call(cast(JsonValue, request))
    second = logical_call.before_model_call(cast(JsonValue, request))

    assert type(first) is type(second) is RuntimeVerticalSentinelResultV1
    assert first is not second
    assert first.bridge.status is RuntimeVerticalBridgeStatus.NO_HISTORY_AVAILABLE
    assert first.bridge.policy_evaluated is False
    assert first.bridge.target_count == 0
    assert first.bridge.decision_kinds == ()
    assert first.bridge.policy_output_sha256 is None
    assert first.raw_request == first.candidate_request == first.final_request == request
    assert first.use_transformed_request is False
    overlay = build_runtime_history_codec_resolver().by_id(codec_id).overlay_declaration
    assert first.overlay_declaration_sha256 == overlay.sha256
    assert source.evaluate_count == 0
    assert transport.calls == []
    assert policy_sink.receipts == ()
    assert len(seam_sink.receipts) == 1


def test_zero_target_seam_returns_explicit_overlay_bound_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _build_adapter_case("qwen", zero_targets=True)
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)
    codec = build_runtime_history_codec_resolver().by_id(QWEN_CODEC_ID)
    extraction = RuntimeHistoryExtractionResultV1(
        status=RuntimeHistoryExtractionStatusV1.READY,
        raw_request_sha256=canonical_sha256(cast(JsonValue, built.request)),
        overlay=codec.overlay_declaration,
        capabilities=codec.capabilities,
        history_ir=built.history_ir,
        reason_code=None,
        validation_checks=("canonical_request_bound", "zero_runtime_targets"),
        warnings=built.history_ir.warnings,
    )
    monkeypatch.setattr(
        RuntimeEditableSpanCodecV1,
        "extract_runtime",
        lambda _codec, _request: extraction,
    )
    logical_call = sentinel.logical_call(
        host_id=built.context.host_id,
        history_codec_id=QWEN_CODEC_ID,
    )

    first = logical_call.before_model_call(cast(JsonValue, built.request))
    second = logical_call.before_model_call(cast(JsonValue, built.request))

    assert type(first) is type(second) is RuntimeVerticalSentinelResultV1
    assert first is not second
    assert first.bridge.status is RuntimeVerticalBridgeStatus.NO_ELIGIBLE_HISTORY
    assert first.bridge.policy_evaluated is True
    assert first.bridge.target_count == 0
    assert first.bridge.decision_kinds == ()
    assert first.bridge.policy_output_sha256 == first.receipt.policy_output_sha256
    assert first.raw_request == first.candidate_request == first.final_request == built.request
    assert first.use_transformed_request is False
    assert first.overlay_declaration_sha256 == codec.overlay_declaration.sha256
    assert source.evaluate_count == len(transport.calls) == len(policy_sink.receipts) == 1
    assert len(seam_sink.receipts) == 1

    mismatched_base = replace(
        first.base_result,
        receipt=replace(first.receipt, policy_output_sha256="0" * 64),
    )
    with pytest.raises(R24ContractError, match="POLICY_OUTPUT_BINDING_MISMATCH"):
        RuntimeVerticalSentinelResultV1(
            base_result=mismatched_base,
            bridge=first.bridge,
            overlay_declaration_sha256=first.overlay_declaration_sha256,
        )


def test_real_r22_adapter_qwen_parse_retry_reuses_one_active_result() -> None:
    built = _build_adapter_case("qwen")
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)

    returned, action, provider_calls = _run_host(
        "qwen",
        sentinel,
        responses=("malformed", QWEN_RESPONSE),
    )

    assert returned == QWEN_RESPONSE
    assert action.action_type == "wait"
    assert len(provider_calls) == 2
    assert provider_calls[0] == provider_calls[1]
    assert source.evaluate_count == len(transport.calls) == 1
    assert len(policy_sink.receipts) == len(seam_sink.receipts) == 1
    assert seam_sink.receipts[0].edit_applied is True


def test_real_r22_adapter_base_transport_retry_reuses_one_active_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = _build_adapter_case("mai")
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)
    monkeypatch.setattr("mobile_world.agents.base.time.sleep", lambda _seconds: None)

    returned, action, provider_calls = _run_host(
        "mai",
        sentinel,
        responses=(RuntimeError("offline fake retry"), MAI_RESPONSE),
    )

    assert returned == MAI_RESPONSE
    assert action.action_type == "wait"
    assert len(provider_calls) == 2
    assert provider_calls[0] == provider_calls[1]
    assert source.evaluate_count == len(transport.calls) == 1
    assert len(policy_sink.receipts) == len(seam_sink.receipts) == 1
    assert seam_sink.receipts[0].edit_applied is True


def test_real_r22_adapter_stream_retry_scope_reuses_one_active_result() -> None:
    baseline = _build_adapter_case("qwen")
    stream_request = cast(dict[str, JsonValue], copy_json(cast(JsonValue, baseline.request)))
    stream_request["stream_options"] = {"include_usage": True}
    stream_request["stream"] = True
    built = _build_adapter_case("qwen", request_override=stream_request)
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)
    agent = _qwen_agent(sentinel)
    provider_calls: list[dict[str, Any]] = []
    attempts = 0

    def create(**kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        provider_calls.append(kwargs)

        def chunks():
            yield SimpleNamespace(usage=None)
            if attempts == 1:
                raise RuntimeError("partial offline stream")
            yield SimpleNamespace(usage=None)

        return chunks()

    agent.openai_client = _client(create)
    provider_kwargs = {
        key: value for key, value in baseline.request.items() if key not in {"model", "messages"}
    }
    with agent._sentinel_logical_call_scope(attributes={"test": "r24-stream-retry"}):
        first = agent.openai_chat_completions_create(
            model=cast(str, baseline.request["model"]),
            messages=cast(list[dict], baseline.request["messages"]),
            stream=True,
            **provider_kwargs,
        )
        with pytest.raises(RuntimeError, match="partial offline stream"):
            list(cast(Any, first))
        second = agent.openai_chat_completions_create(
            model=cast(str, baseline.request["model"]),
            messages=cast(list[dict], baseline.request["messages"]),
            stream=True,
            **provider_kwargs,
        )
        assert len(list(cast(Any, second))) == 2

    assert len(provider_calls) == 2
    assert provider_calls[0] == provider_calls[1]
    assert provider_calls[0] != built.request
    assert source.evaluate_count == len(transport.calls) == 1
    assert len(policy_sink.receipts) == len(seam_sink.receipts) == 1
    assert seam_sink.receipts[0].edit_applied is True


def test_r22_adapter_rejects_openai_descriptor_before_any_call() -> None:
    built = _build_adapter_case("qwen")
    transport = _R22FakeTransport(
        json.dumps(_r22_proposal(built.packet)),
        descriptor=TransportDescriptorV1(
            transport_kind="OPENAI_RESPONSES",
            transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
            openai_sdk_version=SUPPORTED_OPENAI_SDK_VERSION,
            sdk_max_retries=0,
            external_network_on_call=True,
            model_on_call=True,
        ),
    )
    source, policy_sink = _source_policy(built, transport)

    with pytest.raises(R24ContractError, match="CPU_FAKE_SOURCE_ATTESTATION_REQUIRED"):
        R22CpuFakeActivePolicyAdapter(
            source,
            authority=issue_cpu_fake_active_authority(),
        )

    assert source.evaluate_count == 0
    assert transport.calls == []
    assert policy_sink.receipts == ()


def test_r22_adapter_zero_target_has_typed_empty_census() -> None:
    built = _build_adapter_case("qwen", zero_targets=True)
    assert built.packet.targets == ()
    transport = _R22FakeTransport(json.dumps(_r22_proposal(built.packet)))
    adapter, source, policy_sink = _active_adapter(built, transport)

    output = adapter.evaluate(
        request=cast(JsonValue, built.request),
        context=built.context,
        history_ir=built.history_ir,
    )
    bridge = RuntimeVerticalReceiptBridgeV1.from_policy_output(
        built.context.logical_call_id,
        output,
    )

    assert output.status is RuntimeVerticalStatus.NO_ELIGIBLE_HISTORY
    assert output.decisions == output.admitted_plan.operations == ()
    assert output.receipt_decision_kinds == ()
    assert bridge.policy_evaluated is True
    assert bridge.target_count == 0
    assert bridge.decision_kinds == ()
    assert bridge.policy_output_sha256 is not None
    assert source.evaluate_count == len(transport.calls) == len(policy_sink.receipts) == 1
    assert policy_sink.receipts[0].evaluation_status is PolicyEvaluationStatus.ADMITTED


def test_invalid_r22_response_falls_back_before_active_provider_edit() -> None:
    built = _build_adapter_case("qwen")
    transport = _R22FakeTransport("not-json")
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter)

    _returned, action, provider_calls = _run_host("qwen", sentinel)

    assert action.action_type == "wait"
    assert provider_calls[0] == built.request
    assert source.evaluate_count == len(transport.calls) == 1
    assert len(policy_sink.receipts) == 1
    assert policy_sink.receipts[0].evaluation_status is PolicyEvaluationStatus.INVALID_RESPONSE
    assert len(seam_sink.receipts) == 1
    assert seam_sink.receipts[0].fallback_reason is SentinelFallbackReason.POLICY_EXCEPTION
    assert seam_sink.receipts[0].edit_applied is False


def test_r22_adapter_seam_timeout_returns_original_and_has_no_late_policy_receipt() -> None:
    built = _build_adapter_case("qwen")
    entered = Event()
    release = Event()
    transport = _R22FakeTransport(
        json.dumps(_r22_proposal(built.packet)),
        entered=entered,
        release=release,
    )
    adapter, source, policy_sink = _active_adapter(built, transport)
    sentinel, seam_sink = _vertical_sentinel(built, adapter, policy_timeout_ms=500)

    started = time.monotonic()
    _returned, action, provider_calls = _run_host("qwen", sentinel)
    elapsed = time.monotonic() - started

    assert entered.is_set()
    assert elapsed < 1.0
    assert action.action_type == "wait"
    assert provider_calls[0] == built.request
    assert source.evaluate_count == len(transport.calls) == 1
    assert policy_sink.receipts == ()
    assert len(seam_sink.receipts) == 1
    assert seam_sink.receipts[0].fallback_reason is SentinelFallbackReason.POLICY_TIMEOUT
    assert seam_sink.receipts[0].edit_applied is False

    release.set()
    time.sleep(0.1)
    assert len(transport.calls) == 1
    assert policy_sink.receipts == ()
