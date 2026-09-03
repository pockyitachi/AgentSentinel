from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from mobile_world.agents.base import BaseAgent
from mobile_world.agents.implementations import mai_ui_agent as mai_module
from mobile_world.agents.implementations import qwen3vl as qwen_module
from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue, copy_json
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel import (
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelContext,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
)
from mobile_world.runtime.sentinel.contracts import SentinelCallRole
from mobile_world.runtime.sentinel.r2_2.contracts import (
    POLICY_PROPOSAL_SCHEMA_VERSION,
    EvidenceRelation,
    EvidenceRole,
    evidence_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56_REQUESTED_MODEL,
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
    proposal_admission,
)
from mobile_world.runtime.sentinel.r2_2.sidecar import MemoryR22PolicyReceiptSink
from mobile_world.runtime.sentinel.r2_3.contracts import (
    GraphRefKind,
    GraphRefV1,
    InstructionSpanRole,
    InstructionSpanV1,
    MilestoneKind,
    MilestonePredicateKind,
    MilestoneReasonCode,
    MilestoneState,
    MilestoneStateRecordV1,
    MilestoneV1,
    MultiPathRubricV1,
    PathKind,
    RevisionKind,
    RevisionReason,
    RubricBackendDescriptorV1,
    RubricPathV1,
    RubricRevisionRequestV1,
    RubricRevisionV1,
    RubricTrackerProposalV1,
    RubricTrackingPacketV1,
    TaskInstructionV1,
    TaskStartRubricRequestV1,
    TrackerProposalStatus,
    rubric_tracking_state_sha256,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.session import RubricTaskSession
from mobile_world.runtime.sentinel.r2_4.audit_detail import (
    MemoryRuntimeAuditDetailSinkV1,
    ParserResultStatusV1,
    RuntimeAuditDetailSinkV1,
    RuntimeAuditDetailV1,
    runtime_audit_detail_projection,
)
from mobile_world.runtime.sentinel.r2_4.audit_runtime import R24RuntimeAuditV1
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    canonical_json_bytes,
    issue_cpu_fake_active_authority,
)
from mobile_world.runtime.sentinel.r2_4.evidence import CollectorEvidenceFactoryV1
from mobile_world.runtime.sentinel.r2_4.orchestration import R24RuntimeCoordinatorV1
from mobile_world.runtime.sentinel.r2_4.policy import R22CpuFakeActivePolicyAdapter
from mobile_world.runtime.utils.models import WAIT

QWEN_HOST_ID = cast(str, qwen_module.Qwen3VLAgentMCP.sentinel_host_id)
MAI_HOST_ID = cast(str, mai_module.MAIUINaivigationAgent.sentinel_host_id)

TASK_GOAL = "Wait on the current screen."
QWEN_STALE_CLAIM = "qwen stale claim contradicted by the completed UI transition"
MAI_STALE_CLAIM = "mai stale claim contradicted by the completed UI transition"
QWEN_PRIVATE_REASONING = "QWEN_PRIVATE_REASONING_MUST_NOT_PERSIST"
MAI_PRIVATE_REASONING = "MAI_PRIVATE_REASONING_MUST_NOT_PERSIST"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _qwen_response(reasoning: str = QWEN_PRIVATE_REASONING) -> str:
    return (
        f"Thought: {reasoning}\nAction: wait\n"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
    )


def _mai_response(reasoning: str = MAI_PRIVATE_REASONING) -> str:
    return (
        f"<thinking>{reasoning}</thinking>"
        '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
    )


class _ActorResponse:
    def __init__(self, content: str, *, response_id: str) -> None:
        self.id = response_id
        self.model = "cpu-fake-actor"
        self.usage = None
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]


def _actor_client(create: Any) -> Any:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )


def _qwen_agent(prompt_sentinel: PromptSentinel) -> Any:
    agent = qwen_module.Qwen3VLAgentMCP.__new__(qwen_module.Qwen3VLAgentMCP)
    BaseAgent.__init__(agent, prompt_sentinel=prompt_sentinel)
    agent.model_name = "cpu-fake-qwen"
    agent.runtime_conf = {"temperature": 0.0}
    agent.instruction = TASK_GOAL
    agent.tools = []
    agent.actions = [{"action_type": WAIT}]
    agent.thoughts = ["prior private thought"]
    agent.conclusions = [QWEN_STALE_CLAIM]
    agent.history_images = []
    agent.history_responses = []
    return agent


def _mai_agent(prompt_sentinel: PromptSentinel) -> Any:
    agent = mai_module.MAIUINaivigationAgent.__new__(mai_module.MAIUINaivigationAgent)
    BaseAgent.__init__(agent, prompt_sentinel=prompt_sentinel)
    agent.model_name = "cpu-fake-mai"
    agent.instruction = TASK_GOAL
    agent.max_tokens = 2_048
    agent.temperature = 0.0
    agent.top_p = 1.0
    agent.history_n = 3
    agent.tools = []
    agent.history_images = [(Image.new("RGB", (4, 4), "red"), None, None)]
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


def _rubric_descriptor() -> RubricBackendDescriptorV1:
    return RubricBackendDescriptorV1(
        backend_id="r24-host-audit-fake-rubric",
        backend_version="v1",
        prompt_sha256=_sha("host audit rubric prompt"),
        rubric_schema_sha256=_sha("host audit rubric schema"),
        tracking_packet_schema_sha256=_sha("host audit packet schema"),
        tracker_schema_sha256=_sha("host audit tracker schema"),
        config_sha256=_sha("host audit cpu fake config"),
    )


def _rubric(task_run_id: str, task: TaskInstructionV1) -> MultiPathRubricV1:
    span = InstructionSpanV1(
        span_id="r24-host-audit-task-span",
        role=InstructionSpanRole.HARD_REQUIREMENT,
        char_start=0,
        char_end=len(task.exact_text),
        utf8_byte_start=0,
        utf8_byte_end=len(task.exact_text.encode("utf-8")),
        exact_text=task.exact_text,
        span_sha256=task.text_sha256,
    )
    milestone = MilestoneV1(
        milestone_id="r24-host-audit-milestone",
        kind=MilestoneKind.HARD_REQUIREMENT,
        predicate_kind=MilestonePredicateKind.INSTRUCTION_REQUIREMENT,
        state_description=task.exact_text,
        description_sha256=task.text_sha256,
        instruction_span_id=span.span_id,
    )
    return MultiPathRubricV1(
        rubric_id="r24-host-audit-rubric",
        task_run_id=task_run_id,
        rubric_version=1,
        task=task,
        revision=RubricRevisionV1(
            revision_id="r24-host-audit-initial-revision",
            revision_event_id=task.source_event_id,
            kind=RevisionKind.INITIAL,
            reason=RevisionReason.TASK_START,
            previous_rubric_version=None,
            previous_rubric_sha256=None,
            hard_requirement_deltas=(),
            changed_node_ids=(),
        ),
        instruction_spans=(span,),
        milestones=(milestone,),
        gates=(),
        common_root=None,
        paths=(
            RubricPathV1(
                path_id="r24-host-audit-primary-path",
                kind=PathKind.LEGAL_ALTERNATIVE,
                root=GraphRefV1(
                    ref_kind=GraphRefKind.MILESTONE,
                    ref_id=milestone.milestone_id,
                ),
            ),
            RubricPathV1(
                path_id="r24-host-audit-other-unknown",
                kind=PathKind.OTHER_UNKNOWN,
                root=None,
            ),
        ),
        backend=_rubric_descriptor(),
    )


class _RubricBuilder:
    def __init__(self, rubric: MultiPathRubricV1) -> None:
        self._rubric = rubric

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        return self._rubric.backend

    def generate(self, request: TaskStartRubricRequestV1) -> MultiPathRubricV1:
        assert request.task == self._rubric.task
        return self._rubric

    def revise(self, request: RubricRevisionRequestV1) -> MultiPathRubricV1:
        del request
        raise AssertionError("host audit integration must not revise its task rubric")


class _RubricTracker:
    def __init__(self, descriptor: RubricBackendDescriptorV1) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        return self._descriptor

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1:
        states = tuple(
            MilestoneStateRecordV1(
                milestone_id=item.milestone_id,
                state=MilestoneState.UNKNOWN,
                evidence_refs=(),
                reason_code=MilestoneReasonCode.AMBIGUOUS_GUI,
            )
            for item in packet.prior_state.milestone_states
        )
        return RubricTrackerProposalV1(
            proposal_id=f"r24-host-audit-proposal-{packet.logical_call_id}",
            packet_id=packet.packet_id,
            packet_sha256=tracking_packet_sha256(packet),
            rubric_binding=packet.rubric_binding,
            prior_state_sha256=rubric_tracking_state_sha256(packet.prior_state),
            proposal_status=TrackerProposalStatus.ABSTAIN,
            milestone_states=states,
        )


def _session_factory(task_run_id: str, task: TaskInstructionV1) -> RubricTaskSession:
    rubric = _rubric(task_run_id, task)
    return RubricTaskSession(
        task_run_id=task_run_id,
        task=task,
        builder_backend=_RubricBuilder(rubric),
        tracker_backend=_RubricTracker(rubric.backend),
    )


def _r22_proposal(packet: Any) -> dict[str, JsonValue]:
    refutation = next(
        item for item in packet.evidence_index if item.role is EvidenceRole.PRIOR_POST_UI_STATE
    )
    evidence_ref: dict[str, JsonValue] = {
        "evidence_id": refutation.evidence_id,
        "payload_sha256": refutation.payload_sha256,
        "relation": EvidenceRelation.REFUTES.value,
    }
    decisions: list[JsonValue] = []
    for index, target in enumerate(packet.targets):
        if index == 0:
            decisions.append(
                {
                    "decision_id": "r24-host-audit-drop",
                    "target_id": target.target_id,
                    "factual_verdict": "REFUTED",
                    "temporal_validity": "N_A",
                    "proposed_operation": "DROP",
                    "evidence_refs": [evidence_ref],
                    "confidence_millis": 950,
                    "reason_code": "DIRECT_EVIDENCE_REFUTATION",
                    "uncertainty_codes": [],
                    "rationale_summary": "Prior post-UI evidence directly refutes the claim.",
                    "replacement_fact_id": None,
                    "fallback_status": "NONE",
                }
            )
        else:
            decisions.append(
                {
                    "decision_id": f"r24-host-audit-keep-{index}",
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
        "status": "COMPLETE" if len(decisions) == 1 else "PARTIAL_ABSTAIN",
        "automatic": True,
        "curated": False,
        "deployment_prediction": True,
        "action_or_tool_authority": False,
        "decisions": decisions,
    }


class _PolicyTransport:
    def __init__(self) -> None:
        self.calls: list[ResponsesRequestV1] = []

    @property
    def descriptor(self) -> TransportDescriptorV1:
        return TransportDescriptorV1.cpu_fake()

    def create(
        self,
        request: ResponsesRequestV1,
        *,
        call_role: SentinelCallRole = SentinelCallRole.SENTINEL,
        timeout_seconds: float,
    ) -> ResponsesEnvelopeV1:
        assert call_role is SentinelCallRole.SENTINEL
        assert timeout_seconds > 0
        self.calls.append(request)
        return ResponsesEnvelopeV1(
            response_id="r24-host-audit-policy-response",
            requested_model=GPT56_REQUESTED_MODEL,
            returned_model=GPT56_REQUESTED_MODEL,
            status="completed",
            service_tier="default",
            output_text=json.dumps(
                _r22_proposal(request.evidence.packet),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
        )


class _AdmissionBridge:
    def __init__(self, coordinator: R24RuntimeCoordinatorV1) -> None:
        self._coordinator = coordinator
        self._packet: Any = None
        self._request: JsonValue | None = None
        self._history_ir: HistoryIR | None = None

    def evidence(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> Any:
        evidence = self._coordinator(request, context, history_ir)
        self._packet = deepcopy(evidence.packet)
        self._request = copy_json(request)
        self._history_ir = deepcopy(history_ir)
        return evidence

    def admit(
        self,
        packet_projection: dict[str, JsonValue],
        proposal_projection: dict[str, JsonValue],
        provenance: Any,
    ) -> Any:
        del packet_projection
        assert self._packet is not None
        assert self._request is not None
        assert self._history_ir is not None
        return proposal_admission(
            deepcopy(self._packet),
            proposal_projection,
            provenance,
            source_request=copy_json(self._request),
            history_ir=deepcopy(self._history_ir),
        )


def _transport_result(run: RunRecorder) -> dict[str, Any]:
    request_blob = run.blob_store.put_bytes(b'{"action":"click"}', "application/json")
    response_blob = run.blob_store.put_bytes(b'{"status":"ok"}', "application/json")
    return {
        "kind": "gui_transport",
        "request_endpoint": "http://fixture.invalid/step",
        "request_body_snapshot_blob": request_blob,
        "http_status": 200,
        "response_body_blob": response_blob,
        "response_headers": {"content-type": "application/json"},
        "raw_tool_result_blob": None,
        "agent_visible_tool_result": {"tool": "visible", "ok": True},
        "ask_user_response": "confirmed",
        "exception": None,
    }


@dataclass(slots=True)
class _CollectorRun:
    run: RunRecorder
    task: TaskRecorder
    context: AuditContext
    screenshot: Image.Image


def _collector_run(tmp_path: Any, *, host: str) -> _CollectorRun:
    screenshot = Image.new("RGB", (4, 4), "blue")
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="r2.4-test", worker_id=f"host-audit-{host}"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    started = capture.start_task(
        task_name=f"R24HostAudit{host}",
        task_goal=TASK_GOAL,
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": host, "model": "cpu-fake", "configuration": {}},
        environment={"backend_id": "cpu-fixture", "device_id": "none"},
        whole_task_attempt_index=1,
    )
    assert started is not None
    prior = capture.start_step(
        step_index=1,
        observation={
            "screenshot": Image.new("RGB", screenshot.size, "red"),
            "accessibility_tree": {"screen": "prior"},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert prior is not None
    decision = capture.record_decision(
        prediction="cpu fixture prior response",
        action={"action_type": "click", "x": 1, "y": 1},
        step=prior,
    )
    assert decision is not None
    execution = capture.execution_started(decision=decision)
    assert execution is not None
    transition = capture.transition_completed(
        execution=execution,
        post_observation={
            "screenshot": Image.new("RGB", screenshot.size, "green"),
            "accessibility_tree": {
                "screen": "post",
                "contradicts_prior_history": True,
            },
            "tool_call": {"tool": "visible", "ok": True},
            "ask_user_response": "confirmed",
        },
        execution_result=_transport_result(run),
        duration_ns=1_234,
    )
    assert transition is not None
    current = capture.start_step(
        step_index=2,
        observation={
            "screenshot": screenshot,
            "accessibility_tree": {"screen": "current", "buttons": ["OK"]},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert current is not None
    return _CollectorRun(
        run=run,
        task=task,
        context=AuditContext(
            run_id=run.run_id,
            recorder=task,
            task_run_id=task.task_run_id,
            step_id=current.step_id,
            decision_id=current.decision_id,
            parent_event_id=current.step_started_event_id,
        ),
        screenshot=screenshot,
    )


class _FailingDetailSink:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, detail: RuntimeAuditDetailV1) -> None:
        assert type(detail) is RuntimeAuditDetailV1
        self.calls += 1
        raise RuntimeError("injected derived-detail sink failure")


@dataclass(slots=True)
class _HostHarness:
    agent: Any
    audit: R24RuntimeAuditV1
    detail_sink: RuntimeAuditDetailSinkV1
    policy: GPT56SentinelPolicy[Any, Any]
    policy_transport: _PolicyTransport
    sentinel_receipts: MemorySentinelReceiptSink
    actor_calls: list[dict[str, Any]]


def _host_harness(
    *,
    host: str,
    outcomes: tuple[str | BaseException, ...],
    detail_sink: RuntimeAuditDetailSinkV1 | None = None,
) -> _HostHarness:
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=_session_factory,
    )
    bridge = _AdmissionBridge(coordinator)
    policy_transport = _PolicyTransport()
    source_policy = GPT56SentinelPolicy(
        transport=policy_transport,
        evidence_packet_factory=bridge.evidence,
        proposal_admission=bridge.admit,
        admission_receipt_projector=admission_receipt_projector,
        bind_policy_receipt=bind_policy_receipt,
        receipt_sink=MemoryR22PolicyReceiptSink(),
        metrics=R22PolicyMetrics(),
        output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
        timeout_seconds=0.05,
        seam_policy_deadline_seconds=2.0,
    )
    adapter = R22CpuFakeActivePolicyAdapter(
        source_policy,
        authority=issue_cpu_fake_active_authority(),
    )
    sink = detail_sink or MemoryRuntimeAuditDetailSinkV1()
    runtime_audit = R24RuntimeAuditV1(
        coordinator=coordinator,
        topology_comparison_sha256=_sha("r24 host audit topology comparison"),
        sink=sink,
    )
    host_id = QWEN_HOST_ID if host == "qwen" else MAI_HOST_ID
    receipts = MemorySentinelReceiptSink()
    logical_ids = iter(f"r24-{host}-host-audit-{index}" for index in range(1, 10))
    sentinel = PromptSentinel(
        policy=adapter,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={
            host_id: SentinelHostConfig(mode=SentinelMode.ACTIVE, policy_timeout_ms=1_000)
        },
        receipt_sink=receipts,
        runtime_audit=runtime_audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=logical_ids.__next__,
    )
    agent = _qwen_agent(sentinel) if host == "qwen" else _mai_agent(sentinel)
    scripted = iter(outcomes)
    actor_calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _ActorResponse:
        actor_calls.append(deepcopy(kwargs))
        outcome = next(scripted)
        if isinstance(outcome, BaseException):
            raise outcome
        return _ActorResponse(outcome, response_id=f"actor-response-{len(actor_calls)}")

    agent.openai_client = _actor_client(create)
    return _HostHarness(
        agent=agent,
        audit=runtime_audit,
        detail_sink=sink,
        policy=source_policy,
        policy_transport=policy_transport,
        sentinel_receipts=receipts,
        actor_calls=actor_calls,
    )


def _run_host(
    tmp_path: Any,
    *,
    host: str,
    outcomes: tuple[str | BaseException, ...],
    detail_sink: RuntimeAuditDetailSinkV1 | None = None,
) -> tuple[_HostHarness, str, Any]:
    collector = _collector_run(tmp_path, host=host)
    harness = _host_harness(host=host, outcomes=outcomes, detail_sink=detail_sink)
    try:
        with bind_audit_context(collector.context):
            returned, action = harness.agent.predict({"screenshot": collector.screenshot})
    finally:
        collector.run.close()
    return harness, returned, action


@pytest.mark.parametrize(
    ("host", "response", "private_reasoning", "stale_claim"),
    (
        ("qwen", _qwen_response(), QWEN_PRIVATE_REASONING, QWEN_STALE_CLAIM),
        ("mai", _mai_response(), MAI_PRIVATE_REASONING, MAI_STALE_CLAIM),
    ),
)
def test_qwen_and_mai_emit_one_complete_hash_bound_detail_without_cot(
    tmp_path: Any,
    host: str,
    response: str,
    private_reasoning: str,
    stale_claim: str,
) -> None:
    harness, returned, action = _run_host(
        tmp_path,
        host=host,
        outcomes=(response,),
    )

    assert returned == response
    assert action.action_type == WAIT
    assert len(harness.actor_calls) == 1
    assert stale_claim not in json.dumps(harness.actor_calls[0]["messages"], ensure_ascii=False)
    assert harness.policy.evaluate_count == 1
    assert len(harness.policy_transport.calls) == 1
    assert len(harness.sentinel_receipts.receipts) == 1
    assert harness.audit.pending_count == 0

    assert isinstance(harness.detail_sink, MemoryRuntimeAuditDetailSinkV1)
    details = harness.detail_sink.details
    assert len(details) == 1
    detail = details[0]
    projection = runtime_audit_detail_projection(detail)
    encoded = canonical_json_bytes(cast(JsonValue, projection))
    provider = cast(dict[str, JsonValue], detail.provider_response.value)
    parser = cast(dict[str, JsonValue], detail.parser_result.value)
    actor = cast(dict[str, JsonValue], detail.actor_action.value)

    assert detail.logical_call_id == harness.sentinel_receipts.receipts[0].logical_call_id
    assert detail.edit_applied is True
    assert detail.raw_request.sha256 != detail.final_request.sha256
    assert provider["logical_call_id"] == detail.logical_call_id
    assert provider["final_request_sha256"] == detail.final_request.sha256
    assert provider["response_content_persisted"] is False
    assert provider["reasoning_persisted"] is False
    assert parser["logical_call_id"] == detail.logical_call_id
    assert parser["final_request_sha256"] == detail.final_request.sha256
    assert parser["raw_provider_response_sha256"] == provider["raw_provider_response_sha256"]
    assert parser["actor_action_sha256"] == actor["action_sha256"]
    assert parser["status"] == ParserResultStatusV1.PARSED.value
    action_projection = cast(dict[str, JsonValue], actor["action"])
    assert action_projection["action_type"] == WAIT
    assert detail.resources.action_executed is False
    assert private_reasoning.encode("utf-8") not in encoded
    assert b"prior private thought" not in encoded


@pytest.mark.parametrize(
    ("host", "outcomes", "expected_parser_attempts"),
    (
        (
            "qwen",
            ("malformed outer-parser response", _qwen_response()),
            2,
        ),
        (
            "mai",
            (
                ValueError("max_tokens is incompatible with max_completion_tokens"),
                _mai_response(),
            ),
            1,
        ),
    ),
)
def test_host_retries_reuse_one_logical_call_and_emit_one_detail(
    tmp_path: Any,
    host: str,
    outcomes: tuple[str | BaseException, ...],
    expected_parser_attempts: int,
) -> None:
    harness, _returned, action = _run_host(tmp_path, host=host, outcomes=outcomes)

    assert action.action_type == WAIT
    assert len(harness.actor_calls) == 2
    assert harness.policy.evaluate_count == 1
    assert len(harness.policy_transport.calls) == 1
    assert len(harness.sentinel_receipts.receipts) == 1
    assert harness.audit.pending_count == 0
    assert isinstance(harness.detail_sink, MemoryRuntimeAuditDetailSinkV1)
    assert len(harness.detail_sink.details) == 1
    detail = harness.detail_sink.details[0]
    provider = cast(dict[str, JsonValue], detail.provider_response.value)
    parser = cast(dict[str, JsonValue], detail.parser_result.value)
    assert provider["attempt_id"] == "r24-provider-attempt-2"
    assert parser["attempt_count"] == expected_parser_attempts


@pytest.mark.parametrize(
    ("host", "response"),
    (("qwen", _qwen_response()), ("mai", _mai_response())),
)
def test_derived_detail_sink_failure_does_not_change_actor_behavior(
    tmp_path: Any,
    host: str,
    response: str,
) -> None:
    sink = _FailingDetailSink()
    assert isinstance(sink, RuntimeAuditDetailSinkV1)

    harness, returned, action = _run_host(
        tmp_path,
        host=host,
        outcomes=(response,),
        detail_sink=sink,
    )

    assert returned == response
    assert action.action_type == WAIT
    assert sink.calls == 1
    assert harness.audit.pending_count == 0
    assert harness.policy.evaluate_count == 1
    assert len(harness.actor_calls) == 1
