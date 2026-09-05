from __future__ import annotations

import json
import stat
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import monotonic_ns
from types import SimpleNamespace
from typing import Any, cast

import pytest
from httpx import MockTransport, Request, Response
from openai import DefaultHttpxClient, OpenAI

from mobile_world.agents.base import BaseAgent
from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_sha256
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel import (
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelFallbackReason,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
    bind_sentinel_logical_call,
)
from mobile_world.runtime.sentinel.policies import NoOpSentinelPolicy
from mobile_world.runtime.sentinel.r2_3.contracts import (
    CurrentObservationBindingV1,
    EvidenceMediaType,
    ImageEvidenceProjectionV1,
    RubricBackendDescriptorV1,
    RubricCutoffV1,
    RubricEvidenceRole,
    RubricEvidenceV1,
    RubricSourceEventType,
    TaskInstructionV1,
    TaskStartRubricRequestV1,
    task_start_request_projection,
)
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1
from mobile_world.runtime.sentinel.r2_4 import production_audit as production_audit_module
from mobile_world.runtime.sentinel.r2_4 import production_preflight as production_preflight_module
from mobile_world.runtime.sentinel.r2_4 import rubric_live as rubric_live_module
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalSentinelResultV1,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptAuthorityV1,
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptPricingV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    MemoryLiveAttemptReceiptSinkV1,
    live_attempt_authority_sha256,
    live_attempt_pricing_sha256,
    live_attempt_receipt_projection,
    live_attempt_receipt_root_sha256,
    live_attempt_receipt_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    OwnerAuthorizedLivePerCallPolicyV1,
)
from mobile_world.runtime.sentinel.r2_4.live_run import (
    OpenAIResponsesStageV1,
    OpenAIRoleV1,
    RunStageV1,
    SmokeModeV1,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ExternalProductionRuntimeAuditSinkV1,
    MemoryProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditAdmissionFailureReceiptV1,
    ProductionRuntimeAuditAdmissionStageV1,
    ProductionRuntimeAuditCommitFailureReceiptV1,
    ProductionRuntimeAuditDetailV1,
    ProductionRuntimeAuditError,
    ProductionRuntimeAuditPreProviderStatusV1,
    ProductionRuntimeAuditPreProviderV1,
    ProductionRuntimeAuditPublicationStatusV1,
    ProductionRuntimeAuditSinkV1,
    ProductionRuntimeAuditTerminalKindV1,
    ProductionRuntimeAuditTransactionV1,
    ProductionRuntimeAuditV1,
    production_runtime_audit_admission_failure_receipt_projection,
    production_runtime_audit_admission_failure_receipt_sha256,
    production_runtime_audit_commit_failure_receipt_projection,
    production_runtime_audit_detail_projection,
)
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CASE_EXECUTION_LEASE_SCHEMA_VERSION,
    CaseExecutionLeaseV1,
    CaseExecutionScopeV1,
    case_execution_lease_projection,
    case_execution_lease_sha256,
    openai_stage_set_sha256,
    openai_stage_sha256,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
    LIVE_RUBRIC_MAX_OUTPUT_TOKENS,
    LIVE_RUBRIC_MODEL,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    LiveRubricTransportAuthorityV1,
    LiveRubricTransportKindV1,
    R24RubricBackendExtensionDescriptorV1,
    build_live_rubric_provider_request_v1,
    live_rubric_attempt_request_proof_projection,
    live_rubric_generate_schema,
    live_rubric_operation_prompt_sha256,
    live_rubric_prompt_bundle_sha256,
    live_rubric_track_schema,
    rubric_backend_descriptor_sha256,
    validate_live_rubric_request_proof_projection_v1,
)
from mobile_world.runtime.sentinel.r2_5.pilot import PilotHostV1
from mobile_world.runtime.utils.models import WAIT, JSONAction

QWEN_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)


def _sha(value: str | bytes) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return sha256(raw).hexdigest()


def _expected_request_proof_roots(
    attempt: LiveAttemptReceiptV1 | dict[str, JsonValue],
    proof: JsonValue | None = None,
) -> dict[str, str]:
    if type(attempt) is LiveAttemptReceiptV1:
        projection = cast(dict[str, JsonValue], live_attempt_receipt_projection(attempt))
    else:
        projection = attempt
    proof_projection = proof if type(proof) is dict else {}
    constraint = proof_projection.get(
        "attempt_constraint_binding",
        proof_projection.get("constraint_binding"),
    )
    return {
        "expected_attempt_authority_sha256": cast(str, projection["authority_sha256"]),
        "expected_constraint_binding_sha256": (
            canonical_sha256(cast(JsonValue, constraint))
            if type(constraint) is dict
            else _sha("caller-known-attempt-constraint")
        ),
        "expected_manifest_sha256": cast(str, projection["manifest_sha256"]),
        "expected_preflight_sha256": cast(str, projection["preflight_sha256"]),
        "expected_case_execution_lease_sha256": cast(
            str, projection["case_execution_lease_sha256"]
        ),
        "expected_stage_sha256": cast(str, projection["stage_sha256"]),
        "expected_pricing_binding_sha256": cast(str, projection["pricing_binding_sha256"]),
        "expected_transport_binding_sha256": cast(str, projection["transport_binding_sha256"]),
        "expected_request_sha256": cast(str, projection["request_sha256"]),
    }


class _Agent(BaseAgent):
    sentinel_host_id = "mobileworld.production-audit-test.actor"
    sentinel_history_codec_id = "mobileworld.g1.history-codec.qwen-flat-progress"

    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


class _FallbackAgent(BaseAgent):
    def predict(self, observation: dict[str, Any]) -> tuple[str, JSONAction]:
        del observation
        raise NotImplementedError


class _ActorResponse:
    def __init__(self, content: str) -> None:
        self.id = "actor-response-1"
        self.model = "cpu-fake-actor"
        self.usage = SimpleNamespace(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
        )
        self.choices = [
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ]
        self._content = content

    def model_dump(self, *, mode: str, exclude_none: bool = False) -> dict[str, Any]:
        assert mode == "json"
        assert not exclude_none
        return {
            "id": self.id,
            "model": self.model,
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": self._content,
                        "reasoning_content": "PRIVATE_PROVIDER_REASONING",
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }


class _CommitFaultTransaction:
    def __init__(self, pre_provider: ProductionRuntimeAuditPreProviderV1) -> None:
        self._logical_call_id = pre_provider.logical_call_id
        self._pre_provider_sha256 = (
            production_audit_module.production_runtime_audit_pre_provider_sha256(pre_provider)
        )
        self.attempted_detail: ProductionRuntimeAuditDetailV1 | None = None
        self.attempted_failure_detail: JsonValue | None = None
        self.abort_count = 0

    @property
    def logical_call_id(self) -> str:
        return self._logical_call_id

    @property
    def pre_provider_sha256(self) -> str:
        return self._pre_provider_sha256

    def commit(self, detail: ProductionRuntimeAuditDetailV1) -> None:
        self.attempted_detail = detail
        raise OSError("injected terminal commit fault")

    def commit_failure(self, detail: JsonValue) -> None:
        self.attempted_failure_detail = deepcopy(detail)
        raise OSError("injected failed-terminal commit fault")

    def abort(self) -> None:
        self.abort_count += 1


class _CommitFaultSink:
    def __init__(self) -> None:
        self.transactions: list[_CommitFaultTransaction] = []

    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        transaction = _CommitFaultTransaction(pre_provider)
        self.transactions.append(transaction)
        return transaction


class _BeginFaultSink:
    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        del pre_provider
        raise OSError("injected private admission failure")


class _MutatingBeginFaultSink:
    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        restricted = cast(dict[str, JsonValue], pre_provider.restricted_stage_projection)
        restricted["raw_request"] = {"messages": [{"role": "user", "content": "MUTATED"}]}
        raise OSError("injected mutating admission failure")


class _MismatchedBeginSink:
    def __init__(self) -> None:
        self.transaction: _CommitFaultTransaction | None = None

    def begin(
        self, pre_provider: ProductionRuntimeAuditPreProviderV1
    ) -> ProductionRuntimeAuditTransactionV1:
        transaction = _CommitFaultTransaction(pre_provider)
        transaction._logical_call_id = "another-logical-call"
        self.transaction = transaction
        return transaction


def _collector_context(tmp_path: Path) -> tuple[RunRecorder, AuditContext]:
    run = RunRecorder(
        tmp_path / "collector",
        producer=Producer.local(version="r2.4-test", worker_id="production-audit"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    assert (
        capture.start_task(
            task_name="ProductionAudit",
            task_goal="Wait on the current screen.",
            task_goal_status="resolved",
            task_index=1,
            suite_family="mobile_world",
            agent={"adapter": "cpu-test", "model": "cpu-fake", "configuration": {}},
            environment={"backend_id": "cpu-fixture", "device_id": "none"},
            whole_task_attempt_index=1,
        )
        is not None
    )
    step = capture.start_step(
        step_index=1,
        observation={
            "screenshot": None,
            "accessibility_tree": {"screen": "current"},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert step is not None
    return run, AuditContext(
        run_id=run.run_id,
        recorder=task,
        task_run_id=task.task_run_id,
        step_id=step.step_id,
        decision_id=step.decision_id,
        parent_event_id=step.step_started_event_id,
    )


def _tu_generate_request_proof(
    *,
    actor_request_sha256: str,
) -> tuple[LiveAttemptReceiptV1, dict[str, JsonValue]]:
    """Build one exact production-shaped, zero-dispatch TU request proof."""

    repository_root = Path(__file__).resolve().parents[4]
    schema_root = repository_root / "mobileworld_audit_handoff/schemas/r2_3"
    task_text = "Wait on the current screen."
    task = TaskInstructionV1(
        source_event_id="event-task-1",
        source_event_seq=1,
        exact_text=task_text,
        text_sha256=_sha(task_text),
    )
    screenshot_sha256 = _sha(b"tu-admission-image")
    stimulus = RubricEvidenceSnapshotV1(
        task_run_id="task-run-tu-admission-1",
        step_id="step-tu-admission-1",
        cutoff=RubricCutoffV1(
            run_id="run-tu-admission-1",
            task_run_id="task-run-tu-admission-1",
            step_id="step-tu-admission-1",
            current_observation_event_id="event-step-1",
            cutoff_event_seq=2,
        ),
        task=task,
        current_observation=CurrentObservationBindingV1(
            source_event_id="event-step-1",
            source_event_seq=2,
            screenshot_evidence_id="evidence-screen-1",
            screenshot_content_sha256=screenshot_sha256,
            accessibility_evidence_ids=(),
        ),
        evidence_index=(
            RubricEvidenceV1(
                evidence_id="evidence-screen-1",
                role=RubricEvidenceRole.CURRENT_UI_SCREENSHOT,
                source_event_id="event-step-1",
                source_event_type=RubricSourceEventType.STEP_STARTED,
                source_event_seq=2,
                task_run_id="task-run-tu-admission-1",
                caused_by_event_id=None,
                payload_sha256=_sha("tu-admission-image-payload"),
                projection=ImageEvidenceProjectionV1(
                    content_sha256=screenshot_sha256,
                    media_type=EvidenceMediaType.PNG,
                    width=1,
                    height=1,
                ),
            ),
        ),
    )
    descriptor = RubricBackendDescriptorV1(
        backend_id="tu-admission-r23-backend",
        backend_version="r2.4-v1",
        prompt_sha256=live_rubric_prompt_bundle_sha256(),
        rubric_schema_sha256=_sha((schema_root / "rubric.v1.schema.json").read_bytes()),
        tracking_packet_schema_sha256=_sha(
            (schema_root / "tracking_packet.v1.schema.json").read_bytes()
        ),
        tracker_schema_sha256=_sha((schema_root / "tracker_output.v1.schema.json").read_bytes()),
        config_sha256=_sha("tu-admission-r23-config"),
    )
    extension = R24RubricBackendExtensionDescriptorV1(
        descriptor_id="tu-admission-r24-extension",
        descriptor_version="r2.4-v1",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION,
        r23_compatibility_descriptor_sha256=rubric_backend_descriptor_sha256(descriptor),
        provider_config_sha256=_sha("tu-admission-provider-config"),
        prompt_sha256=descriptor.prompt_sha256,
        rubric_schema_sha256=descriptor.rubric_schema_sha256,
        tracking_packet_schema_sha256=descriptor.tracking_packet_schema_sha256,
        tracker_schema_sha256=descriptor.tracker_schema_sha256,
        generate_output_schema_sha256=live_rubric_generate_schema().sha256,
        track_output_schema_sha256=live_rubric_track_schema().sha256,
        configured_model=LIVE_RUBRIC_MODEL,
        external_network_attempted=True,
        model_call_attempted=True,
    )
    task_start = TaskStartRubricRequestV1(
        request_id="tu-admission-generate-request",
        task_run_id=stimulus.task_run_id,
        task=task,
        backend=descriptor,
    )
    provider_input: dict[str, JsonValue] = {
        "backend_extension_descriptor_sha256": extension.sha256,
        "r23_compatibility_descriptor_sha256": (extension.r23_compatibility_descriptor_sha256),
        "request": cast(JsonValue, task_start_request_projection(task_start)),
        "schema_version": LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
    }
    provider_request = build_live_rubric_provider_request_v1(
        operation=LiveRubricOperationV1.GENERATE,
        provider_input=provider_input,
        current_image_data_url=None,
    )
    pricing = LiveAttemptPricingV1(
        pricing_id="tu-admission-pricing",
        model=LIVE_RUBRIC_MODEL,
        input_usd_micros_per_million_tokens=0,
        cached_input_usd_micros_per_million_tokens=0,
        output_usd_micros_per_million_tokens=0,
        source_sha256=_sha("tu-admission-pricing-source"),
        effective_at_utc="2026-09-03T00:00:00Z",
    )
    manifest_sha256 = _sha("tu-admission-manifest")
    preflight_sha256 = _sha("tu-admission-preflight")
    factory_sha256 = _sha("tu-admission-factory")
    pricing_sha256 = live_attempt_pricing_sha256(pricing)
    stage = OpenAIResponsesStageV1(
        role=OpenAIRoleV1.RUBRIC,
        model=LIVE_RUBRIC_MODEL,
        endpoint="https://api.openai.com/v1/responses",
        transport_kind="OPENAI_RESPONSES",
        transport_authority="EXPLICIT_OWNER_AUTHORIZATION",
        openai_sdk_version="1.106.1",
        sdk_max_retries=0,
        external_network_on_call=True,
        model_on_call=True,
        max_output_tokens=LIVE_RUBRIC_MAX_OUTPUT_TOKENS,
        timeout_ms=1_000,
        max_attempts=1,
        store=False,
    )
    history_stage = OpenAIResponsesStageV1(
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
        timeout_ms=1_000,
        max_attempts=1,
        store=False,
    )
    lease = CaseExecutionLeaseV1(
        schema_version=CASE_EXECUTION_LEASE_SCHEMA_VERSION,
        manifest_sha256=manifest_sha256,
        preflight_report_sha256=preflight_sha256,
        factory_binding_sha256=factory_sha256,
        execution_scope=CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        openai_stage_set_sha256=openai_stage_set_sha256((stage, history_stage)),
        pricing_binding_sha256=pricing_sha256,
        stage=RunStageV1.QWEN_LIVE_SMOKE,
        host=PilotHostV1.QWEN3_VL,
        mode=SmokeModeV1.ACTIVE,
        case_id="tu-admission-case-1",
        task_id="tu-admission-task-1",
        task_parameters_sha256=None,
        reset_seed=None,
        actor_call_index=1,
        request_sha256=actor_request_sha256,
        issued_at_utc="2026-09-03T00:00:00Z",
        expires_at_utc="2026-09-03T01:00:00Z",
        _seal=production_preflight_module._LEASE_SEAL,
    )
    attempt_id = "tu-admission-rubric-attempt-1"
    lease_projection = case_execution_lease_projection(lease)
    stage_sha256 = openai_stage_sha256(stage)
    transport_binding: dict[str, JsonValue] = {
        "execution_scope": LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE.value,
        "factory_binding_sha256": factory_sha256,
        "manifest_sha256": manifest_sha256,
        "model": LIVE_RUBRIC_MODEL,
        "preflight_sha256": preflight_sha256,
        "pricing_binding_sha256": pricing_sha256,
        "role": LiveAttemptRoleV1.RUBRIC.value,
        "stage_sha256": stage_sha256,
        "backend_extension_descriptor_sha256": extension.sha256,
        "input_schema_version": LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
        "operation": LiveRubricOperationV1.GENERATE.value,
        "output_schema_sha256": extension.generate_output_schema_sha256,
        "prompt_sha256": live_rubric_operation_prompt_sha256(LiveRubricOperationV1.GENERATE),
    }
    issued_monotonic_ns = monotonic_ns()
    constraint = rubric_live_module.build_live_rubric_attempt_constraint_binding_v1(
        issued_monotonic_ns=issued_monotonic_ns,
        case_execution_deadline_monotonic_ns=issued_monotonic_ns + 1_000_000_000,
        history_stage=history_stage,
        rubric_stage=stage,
        case_stage=lease.stage.value,
        case_host=lease.host.value,
        case_mode=lease.mode.value,
        case_id=lease.case_id,
        task_id=lease.task_id,
        task_parameters_sha256=lease.task_parameters_sha256,
        reset_seed=lease.reset_seed,
        max_actor_calls=1,
        max_openai_calls=3,
        max_wall_time_seconds=10,
        case_max_cost_usd_micros=3,
    )
    authority = LiveAttemptAuthorityV1(
        attempt_id=attempt_id,
        role=LiveAttemptRoleV1.RUBRIC,
        manifest_sha256=manifest_sha256,
        preflight_sha256=preflight_sha256,
        case_execution_lease_sha256=case_execution_lease_sha256(lease),
        stage_sha256=stage_sha256,
        case_id=lease.case_id,
        logical_call_id="tu-admission-logical-call-1",
        actor_request_sha256=actor_request_sha256,
        request_sha256=provider_request.request_sha256,
        transport_binding_sha256=canonical_sha256(cast(JsonValue, transport_binding)),
        pricing_binding_sha256=pricing_sha256,
        deadline_monotonic_ns=constraint.effective_deadline_monotonic_ns,
        max_cost_usd_micros=1,
        max_output_tokens=LIVE_RUBRIC_MAX_OUTPUT_TOKENS,
    )
    anchor = rubric_live_module._build_live_rubric_attempt_request_anchor(
        operation=LiveRubricOperationV1.GENERATE,
        task_run_id=stimulus.task_run_id,
        logical_call_id=authority.logical_call_id,
        attempt_id=attempt_id,
        attempt_order=1,
        attempt_authority=authority,
        constraint_binding=constraint,
        case_execution_lease=lease_projection,
        openai_stage=stage,
        pricing=pricing,
        transport_binding=transport_binding,
        collector_stimulus=stimulus,
        current_image=None,
        provider_input=provider_input,
        provider_request=provider_request,
    )
    receipt = LiveAttemptReceiptV1(
        attempt_id=attempt_id,
        role=LiveAttemptRoleV1.RUBRIC,
        authority_sha256=live_attempt_authority_sha256(authority),
        manifest_sha256=authority.manifest_sha256,
        preflight_sha256=authority.preflight_sha256,
        case_execution_lease_sha256=authority.case_execution_lease_sha256,
        stage_sha256=authority.stage_sha256,
        case_id=authority.case_id,
        logical_call_id=authority.logical_call_id,
        actor_request_sha256=actor_request_sha256,
        request_sha256=provider_request.request_sha256,
        transport_binding_sha256=authority.transport_binding_sha256,
        pricing_binding_sha256=authority.pricing_binding_sha256,
        execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
        dispatch_count=0,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
        cost_usd_micros=None,
        cancellation_requested=True,
        termination=LiveAttemptTerminationV1.UNCONFIRMED,
        worker_pid=1234,
        worker_exit_code=None,
        worker_reaped=False,
        late_output_detected=False,
        duration_ns=1,
        failure_code="TERMINATION_UNCONFIRMED",
    )
    proof = live_rubric_attempt_request_proof_projection(
        anchor,
        attempt_receipt=receipt,
        backend_extension=extension,
    )
    validate_live_rubric_request_proof_projection_v1(
        cast(JsonValue, proof),
        attempt_receipt=receipt,
        expected_attempt_order=1,
        **_expected_request_proof_roots(receipt, cast(JsonValue, proof)),
    )
    return receipt, proof


def _off_agent(
    sink: ProductionRuntimeAuditSinkV1,
) -> tuple[_Agent, ProductionRuntimeAuditV1, list[dict[str, Any]]]:
    audit = ProductionRuntimeAuditV1(policy=None, sink=sink)
    sentinel = PromptSentinel(
        policy=NoOpSentinelPolicy(),
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={_Agent.sentinel_host_id: SentinelHostConfig(mode=SentinelMode.OFF)},
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: "production-audit-off-call-1",
    )
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        return _ActorResponse(
            "PRIVATE_PROVIDER_OUTPUT "
            '<tool_call>{"name":"mobile_use","arguments":{"action":"wait"}}</tool_call>'
        )

    agent = _Agent(prompt_sentinel=sentinel)
    agent.openai_client = SimpleNamespace(
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        timeout=1.0,
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    return agent, audit, calls


def _unstarted_exact_live_policy() -> OwnerAuthorizedLivePerCallPolicyV1:
    """Build no executable authority; tests only a pre-policy typed fallback."""

    policy = object.__new__(OwnerAuthorizedLivePerCallPolicyV1)
    policy._policy_id = "r24-production-audit-unstarted-live-policy"
    policy._lock = Lock()
    policy._call_inputs = {}
    policy._bindings = {}
    policy._failures = {}
    return policy


def _run_off_call(
    tmp_path: Path,
    sink: ProductionRuntimeAuditSinkV1,
) -> tuple[ProductionRuntimeAuditV1, list[dict[str, Any]]]:
    run, context = _collector_context(tmp_path)
    agent, audit, calls = _off_agent(sink)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                temperature=0.0,
            )
            assert type(prediction) is str
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            receipt = agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
            assert receipt is not None
    finally:
        run.close()
    return audit, calls


def _run_off_until_admission_failure(
    tmp_path: Path,
    sink: ProductionRuntimeAuditSinkV1,
) -> tuple[ProductionRuntimeAuditV1, list[dict[str, Any]], ProductionRuntimeAuditError]:
    run, context = _collector_context(tmp_path)
    agent, audit, calls = _off_agent(sink)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope():
            with pytest.raises(ProductionRuntimeAuditError) as raised:
                agent.openai_chat_completions_create(
                    model="cpu-fake-actor",
                    messages=[{"role": "user", "content": "Wait now."}],
                    temperature=0.0,
                )
    finally:
        run.close()
    return audit, calls, raised.value


def _assert_admission_failure_recovery(
    audit: ProductionRuntimeAuditV1,
    *,
    expected_stage: ProductionRuntimeAuditAdmissionStageV1,
    expected_exception_type: str,
) -> dict[str, JsonValue]:
    assert audit.pending_count == 0
    assert audit.latest_completed_receipt is None
    assert audit.latest_failure_receipt is None
    assert audit.latest_commit_failure_receipt is None
    recovery = audit.latest_admission_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditAdmissionFailureReceiptV1
    queried = audit.admission_failure_receipt_for(recovery.logical_call_id)
    assert queried is not recovery
    assert production_runtime_audit_admission_failure_receipt_projection(queried) == (
        production_runtime_audit_admission_failure_receipt_projection(recovery)
    )
    assert recovery.admission_stage is expected_stage
    assert recovery.sink_exception_type == expected_exception_type
    projection = production_runtime_audit_admission_failure_receipt_projection(recovery)
    assert projection["publication_status"] == "ADMISSION_OUTCOME_UNKNOWN"
    assert projection["failure_phase"] == "AUDIT_PRE_PROVIDER_ADMISSION"
    assert projection["failure_code"] == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert projection["recovery_required"] is True
    assert projection["sentinel_receipt_sha256"] is not None
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == projection["pre_provider_sha256"]
    restricted = cast(dict[str, JsonValue], pre_provider["restricted_stage_projection"])
    assert cast(dict[str, JsonValue], restricted["raw_request"])["messages"] == [
        {"role": "user", "content": "Wait now."}
    ]
    assert len(production_runtime_audit_admission_failure_receipt_sha256(recovery)) == 64
    return projection


def test_sink_begin_failure_retains_complete_pre_provider_without_admission(
    tmp_path: Path,
) -> None:
    audit, calls, failure = _run_off_until_admission_failure(
        tmp_path,
        cast(ProductionRuntimeAuditSinkV1, _BeginFaultSink()),
    )

    assert failure.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert calls == []
    _assert_admission_failure_recovery(
        audit,
        expected_stage=ProductionRuntimeAuditAdmissionStageV1.SINK_BEGIN,
        expected_exception_type="OSError",
    )


def test_sink_cannot_mutate_private_admission_recovery_preimage(tmp_path: Path) -> None:
    audit, calls, failure = _run_off_until_admission_failure(
        tmp_path,
        cast(ProductionRuntimeAuditSinkV1, _MutatingBeginFaultSink()),
    )

    assert failure.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert calls == []
    _assert_admission_failure_recovery(
        audit,
        expected_stage=ProductionRuntimeAuditAdmissionStageV1.SINK_BEGIN,
        expected_exception_type="OSError",
    )
    returned = audit.latest_admission_failure_receipt
    assert returned is not None
    restricted = cast(dict[str, JsonValue], returned.pre_provider.restricted_stage_projection)
    restricted["raw_request"] = {"messages": [{"role": "user", "content": "MUTATED"}]}
    # Public reads are detached; mutating one returned graph cannot corrupt the
    # module-owned recovery state needed by the outer journal.
    _assert_admission_failure_recovery(
        audit,
        expected_stage=ProductionRuntimeAuditAdmissionStageV1.SINK_BEGIN,
        expected_exception_type="OSError",
    )


def test_sink_transaction_binding_failure_is_recovery_only_and_aborts(
    tmp_path: Path,
) -> None:
    sink = _MismatchedBeginSink()
    audit, calls, failure = _run_off_until_admission_failure(
        tmp_path,
        cast(ProductionRuntimeAuditSinkV1, sink),
    )

    assert failure.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert calls == []
    _assert_admission_failure_recovery(
        audit,
        expected_stage=ProductionRuntimeAuditAdmissionStageV1.TRANSACTION_BINDING,
        expected_exception_type="SinkAdmissionMismatch",
    )
    assert sink.transaction is not None
    assert sink.transaction.abort_count == 1


def test_tu_request_proof_survives_begin_failure_without_opening_actor_gate() -> None:
    raw_request: JsonValue = {"model": "cpu-actor", "messages": []}
    raw_sha256 = canonical_sha256(raw_request)
    attempt, request_proof = _tu_generate_request_proof(actor_request_sha256=raw_sha256)
    attempt_projection = cast(JsonValue, live_attempt_receipt_projection(attempt))
    restricted_stage: JsonValue = {
        "kind": "FALLBACK_ORIGINAL",
        "raw_request": raw_request,
        "final_request": raw_request,
        "live_attempt_receipts": [attempt_projection],
        "r2_4_rubric_request_proofs": [cast(JsonValue, request_proof)],
        "provider_reasoning_persisted": False,
    }
    pre = ProductionRuntimeAuditPreProviderV1(
        logical_call_id=attempt.logical_call_id,
        host_id="mobileworld.qwen3vl.actor",
        status=ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
        outcome=(
            production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL
        ),
        configured_mode=SentinelMode.ACTIVE,
        effective_mode=SentinelMode.OFF,
        fallback_reason=SentinelFallbackReason.POLICY_EXCEPTION,
        fallback_check="tu_audit_admission_failure",
        raw_request_sha256=raw_sha256,
        extraction_sha256=None,
        history_ir_sha256=None,
        codec_overlay_sha256=None,
        vertical_output_sha256=None,
        coordinated_record_sha256=None,
        rubric_result_sha256=None,
        path_relevance_output_sha256=None,
        render_result_sha256=None,
        candidate_request_sha256=raw_sha256,
        exact_diff_sha256=canonical_sha256({"diffs": [], "list_insertions": []}),
        validator_result_sha256=_sha("tu-admission-validator"),
        final_request_sha256=raw_sha256,
        live_call_binding_sha256=None,
        live_attempt_receipt_sha256s=(live_attempt_receipt_sha256(attempt),),
        live_attempt_receipt_root_sha256=live_attempt_receipt_root_sha256((attempt,)),
        case_execution_lease_sha256=attempt.case_execution_lease_sha256,
        preflight_report_sha256=attempt.preflight_sha256,
        factory_binding_sha256=None,
        execution_authority_sha256=None,
        source_transport_binding_sha256=None,
        pricing_binding_sha256=attempt.pricing_binding_sha256,
        live_openai_calls=0,
        live_cost_usd_micros=0,
        live_cost_exact=False,
        restricted_stage_projection=restricted_stage,
        restricted_stage_projection_sha256=canonical_sha256(restricted_stage),
        evidence_snapshot_ns=0,
        history_extract_ns=0,
        rubric_ns=1,
        policy_ns=1,
        render_ns=0,
        validator_ns=0,
        pre_provider_total_ns=1,
        _seal=production_audit_module._PRE_PROVIDER_SEAL,
    )
    audit = ProductionRuntimeAuditV1(
        policy=None,
        sink=cast(ProductionRuntimeAuditSinkV1, _BeginFaultSink()),
    )
    audit.run_fatal_latch.observe_attempts(
        logical_call_id=attempt.logical_call_id,
        attempts=(attempt,),
    )

    with pytest.raises(ProductionRuntimeAuditError) as raised:
        audit._admit_pre_provider(
            pre,
            sentinel_receipt_sha256=_sha("tu-admission-sentinel-receipt"),
        )

    assert raised.value.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert audit.pending_count == 0
    assert audit.latest_completed_receipt is None
    assert audit.latest_failure_receipt is None
    assert audit.latest_commit_failure_receipt is None
    fatal = audit.run_fatal_latch.state
    assert fatal is not None
    assert fatal.attempt_receipt_sha256 == live_attempt_receipt_sha256(attempt)
    recovery = audit.latest_admission_failure_receipt
    assert recovery is not None
    recovery_projection = production_runtime_audit_admission_failure_receipt_projection(recovery)
    persisted_pre = cast(dict[str, JsonValue], recovery_projection["pre_provider"])
    persisted_stage = cast(dict[str, JsonValue], persisted_pre["restricted_stage_projection"])
    (persisted_attempt,) = cast(
        list[dict[str, JsonValue]], persisted_stage["live_attempt_receipts"]
    )
    (persisted_proof,) = cast(list[JsonValue], persisted_stage["r2_4_rubric_request_proofs"])
    validate_live_rubric_request_proof_projection_v1(
        persisted_proof,
        attempt_receipt=persisted_attempt,
        expected_attempt_order=1,
        **_expected_request_proof_roots(persisted_attempt, persisted_proof),
    )
    with pytest.raises(ProductionRuntimeAuditError) as fatal_rejection:
        audit._observe_and_require_run_not_fatal(
            attempt.logical_call_id,
            known_attempts=(attempt,),
        )
    assert fatal_rejection.value.code == "RUN_FATAL_TERMINATION_UNCONFIRMED"
    with pytest.raises(ProductionRuntimeAuditError) as actor_gate_rejection:
        audit.bind_actor_sdk_arguments(
            logical_call_id=attempt.logical_call_id,
            result=cast(Any, object()),
            sdk_arguments=raw_request,
            collector_request_locator={},
            stream=False,
        )
    assert actor_gate_rejection.value.code == "RUN_FATAL_TERMINATION_UNCONFIRMED"


@pytest.mark.parametrize(
    ("injected_phase", "expected_stage"),
    (
        ("root_open", ProductionRuntimeAuditAdmissionStageV1.ROOT_OPEN),
        ("destination_check", ProductionRuntimeAuditAdmissionStageV1.DESTINATION_CHECK),
        ("temporary_create", ProductionRuntimeAuditAdmissionStageV1.TEMPORARY_CREATE),
        ("write", ProductionRuntimeAuditAdmissionStageV1.ADMISSION_WRITE),
        ("file_fsync", ProductionRuntimeAuditAdmissionStageV1.ADMISSION_FILE_FSYNC),
        (
            "directory_fsync",
            ProductionRuntimeAuditAdmissionStageV1.ADMISSION_DIRECTORY_FSYNC,
        ),
    ),
)
def test_external_sink_begin_stage_faults_retain_recoverable_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    injected_phase: str,
    expected_stage: ProductionRuntimeAuditAdmissionStageV1,
) -> None:
    output = tmp_path / "owner-only-production-audit"
    sink = ExternalProductionRuntimeAuditSinkV1(output)
    run, context = _collector_context(tmp_path / "case")
    agent, audit, calls = _off_agent(sink)

    original_open = production_audit_module.os.open
    original_stat = production_audit_module.os.stat
    original_fsync = production_audit_module.os.fsync
    fsync_calls = 0

    def fail_root_open() -> int:
        raise OSError("private root-open failure")

    def fail_destination_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("dir_fd") is not None and str(path).endswith(
            ".production-runtime-audit.v1.json"
        ):
            raise OSError("private destination-check failure")
        return original_stat(path, *args, **kwargs)

    def fail_temporary_open(path: Any, *args: Any, **kwargs: Any) -> int:
        if kwargs.get("dir_fd") is not None and str(path).endswith(".tmp"):
            raise OSError("private temporary-create failure")
        return cast(int, original_open(path, *args, **kwargs))

    def fail_write(descriptor: int, payload: bytes) -> None:
        del descriptor, payload
        raise OSError("private admission-write failure")

    def fail_selected_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if (injected_phase == "file_fsync" and fsync_calls == 1) or (
            injected_phase == "directory_fsync" and fsync_calls == 2
        ):
            raise OSError("private admission-fsync failure")
        original_fsync(descriptor)

    try:
        with monkeypatch.context() as patcher:
            if injected_phase == "root_open":
                patcher.setattr(sink, "_open_root", fail_root_open)
            elif injected_phase == "destination_check":
                patcher.setattr(production_audit_module.os, "stat", fail_destination_stat)
            elif injected_phase == "temporary_create":
                patcher.setattr(production_audit_module.os, "open", fail_temporary_open)
            elif injected_phase == "write":
                patcher.setattr(sink, "_write_all", fail_write)
            else:
                patcher.setattr(production_audit_module.os, "fsync", fail_selected_fsync)
            with bind_audit_context(context), agent._sentinel_logical_call_scope():
                with pytest.raises(ProductionRuntimeAuditError) as raised:
                    agent.openai_chat_completions_create(
                        model="cpu-fake-actor",
                        messages=[{"role": "user", "content": "Wait now."}],
                        temperature=0.0,
                    )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_PRE_PROVIDER_ADMISSION_FAILED"
    assert calls == []
    _assert_admission_failure_recovery(
        audit,
        expected_stage=expected_stage,
        expected_exception_type="OSError",
    )
    assert tuple(output.glob("*.tmp")) == ()


@pytest.mark.parametrize(
    ("stage_shape", "action_executed"),
    (("SMOKE_ACTOR_SUCCESS", False), ("PILOT_ACTION_EXECUTED", True)),
)
def test_terminal_commit_fault_retains_recoverable_actor_action_and_cost_preimage(
    tmp_path: Path,
    stage_shape: str,
    action_executed: bool,
) -> None:
    sink = _CommitFaultSink()
    run, context = _collector_context(tmp_path)
    agent, audit, calls = _off_agent(sink)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": f"{stage_shape}: wait now."}],
                temperature=0.0,
            )
            assert type(prediction) is str
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            with pytest.raises(ProductionRuntimeAuditError) as raised:
                agent.finalize_prompt_sentinel_action_execution(
                    action=action,
                    action_executed=action_executed,
                    action_execution_ns=11 if action_executed else 0,
                )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert len(calls) == 1
    assert audit.pending_count == 0
    assert audit.latest_completed_receipt is None
    assert audit.latest_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditCommitFailureReceiptV1
    assert recovery is not audit.commit_failure_receipt_for(recovery.logical_call_id)
    assert recovery.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTION_EXECUTION
    assert (
        recovery.publication_status
        is ProductionRuntimeAuditPublicationStatusV1.COMMIT_OUTCOME_UNKNOWN
    )
    projection = production_runtime_audit_commit_failure_receipt_projection(recovery)
    terminal = cast(dict[str, JsonValue], projection["attempted_terminal_receipt"])
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == terminal["pre_provider_sha256"]
    assert terminal["action_executed"] is action_executed
    assert (terminal["executed_action_sha256"] is not None) is action_executed
    assert terminal["provider_attempt_count"] == 1
    assert terminal["live_openai_calls"] == 0
    assert terminal["live_cost_usd_micros"] == 0
    assert terminal["live_cost_exact"] is True
    assert cast(dict[str, JsonValue], projection["parsed_action"])["action_type"] == WAIT
    attempts = cast(list[dict[str, JsonValue]], projection["actor_provider_attempts"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUCCEEDED"
    assert attempts[0]["collector_terminal_locator"]["event_type"] == "model_response"
    assert sink.transactions[0].attempted_detail is not None
    assert sink.transactions[0].abort_count == 1

    # Public recovery reads are detached: mutating a caller-owned projection
    # cannot corrupt the module's only copy after an uncertain publication.
    assert type(recovery.pre_provider.restricted_stage_projection) is dict
    recovery.pre_provider.restricted_stage_projection["caller_tamper"] = True
    if type(recovery.parsed_action) is dict:
        recovery.parsed_action["caller_tamper"] = True
    retained = audit.commit_failure_receipt_for(recovery.logical_call_id)
    retained_projection = production_runtime_audit_commit_failure_receipt_projection(retained)
    retained_pre_provider = cast(dict[str, JsonValue], retained_projection["pre_provider"])
    retained_restricted = cast(
        dict[str, JsonValue], retained_pre_provider["restricted_stage_projection"]
    )
    assert "caller_tamper" not in retained_restricted
    retained_action = cast(dict[str, JsonValue], retained_projection["parsed_action"])
    assert "caller_tamper" not in retained_action


def test_failed_actor_terminal_commit_fault_retains_negative_receipt(tmp_path: Path) -> None:
    sink = _CommitFaultSink()
    agent, audit, calls = _off_agent(sink)
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction is None
            with pytest.raises(ProductionRuntimeAuditError) as raised:
                agent._finalize_prompt_sentinel_actor_failure(
                    logical_call,
                    failure_phase="ACTOR_DEADLINE",
                    failure_code="ACTOR_CASE_DEADLINE_ELAPSED",
                )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert calls == []
    assert audit.latest_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditCommitFailureReceiptV1
    assert recovery.terminal_kind is ProductionRuntimeAuditTerminalKindV1.ACTOR_FAILURE
    projection = production_runtime_audit_commit_failure_receipt_projection(recovery)
    terminal = cast(dict[str, JsonValue], projection["attempted_terminal_receipt"])
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == terminal["pre_provider_sha256"]
    assert terminal["failure_code"] == "ACTOR_CASE_DEADLINE_ELAPSED"
    assert terminal["provider_attempt_count"] == 0
    assert projection["actor_provider_attempts"] == []
    assert projection["parsed_action"] is None
    assert sink.transactions[0].attempted_failure_detail is not None


def test_off_base_path_reaches_provider_and_commits_collector_bound_detail(
    tmp_path: Path,
) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit, calls = _run_off_call(tmp_path, sink)

    assert len(calls) == 1
    receipt = audit.latest_completed_receipt
    assert receipt is not None
    assert receipt.provider_attempt_count == 1
    assert receipt.live_openai_calls == 0
    assert receipt.live_cost_usd_micros == 0
    assert receipt.live_cost_exact
    detail = sink.details[0]
    assert detail.pre_provider.status is ProductionRuntimeAuditPreProviderStatusV1.OFF
    attempt = detail.actor_provider_attempts[0]
    assert attempt.collector_request_locator["event_type"] == "model_request"
    assert attempt.collector_terminal_locator["event_type"] == "model_response"
    assert attempt.collector_request_locator["snapshot_blob"] is not None
    assert attempt.collector_terminal_locator["snapshot_blob"] is not None
    projection = production_runtime_audit_detail_projection(detail)
    encoded = json.dumps(projection, sort_keys=True)
    assert "Wait now." in encoded
    assert "PRIVATE_PROVIDER_OUTPUT" not in encoded
    assert "PRIVATE_PROVIDER_REASONING" not in encoded
    assert projection["terminal"]["parsed_action"]["action_type"] == WAIT


def test_external_sink_is_owner_only_and_transactionally_publishes(tmp_path: Path) -> None:
    output = tmp_path / "owner-only-production-audit"
    sink = ExternalProductionRuntimeAuditSinkV1(output)
    audit, _ = _run_off_call(tmp_path / "case", sink)
    receipt = audit.latest_completed_receipt
    assert receipt is not None
    destination = output / f"{receipt.logical_call_id}.production-runtime-audit.v1.json"
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not tuple(output.glob("*.tmp"))
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["logical_call_id"] == receipt.logical_call_id


def test_external_terminal_atomic_publish_fault_keeps_existing_commit_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "owner-only-production-audit"
    sink = ExternalProductionRuntimeAuditSinkV1(output)
    run, context = _collector_context(tmp_path / "case")
    agent, audit, calls = _off_agent(sink)
    action = JSONAction(action_type=WAIT)

    def fail_atomic_publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("private atomic publication failure")

    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                temperature=0.0,
            )
            assert type(prediction) is str
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            with monkeypatch.context() as patcher:
                # The sink uses no-overwrite hard-link publication rather than
                # replacement rename; both are the terminal atomic-publish phase.
                patcher.setattr(production_audit_module.os, "link", fail_atomic_publish)
                with pytest.raises(ProductionRuntimeAuditError) as raised:
                    agent.finalize_prompt_sentinel_action_execution(
                        action=action,
                        action_executed=False,
                    )
    finally:
        run.close()

    assert raised.value.code == "AUDIT_TERMINAL_COMMIT_FAILED"
    assert len(calls) == 1
    assert audit.latest_admission_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert type(recovery) is ProductionRuntimeAuditCommitFailureReceiptV1
    projection = production_runtime_audit_commit_failure_receipt_projection(recovery)
    terminal = cast(dict[str, JsonValue], projection["attempted_terminal_receipt"])
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre_provider)) == terminal["pre_provider_sha256"]
    assert tuple(output.glob("*.tmp")) == ()
    assert tuple(output.glob("*.production-runtime-audit.v1.json")) == ()


def test_inner_retry_has_exact_collector_and_provider_attempt_census(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    outcomes: list[BaseException | _ActorResponse] = [
        RuntimeError("transient provider failure"),
        _ActorResponse("actor retry output"),
    ]

    def create(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    agent.openai_client.chat.completions.create = create
    run, context = _collector_context(tmp_path)
    action = JSONAction(action_type=WAIT)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=2,
            )
            assert prediction == "actor retry output"
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
    finally:
        run.close()
    assert len(calls) == 2
    assert audit.latest_completed_receipt is not None
    assert audit.latest_completed_receipt.provider_attempt_count == 2
    attempts = sink.details[0].actor_provider_attempts
    assert [item.attempt_index for item in attempts] == [1, 2]
    assert attempts[0].collector_terminal_locator["event_type"] == "model_attempt_failed"
    assert attempts[1].collector_terminal_locator["event_type"] == "model_response"


def test_strict_actor_dispatch_rechecks_deadline_and_returned_model_before_use() -> None:
    agent, _, _ = _off_agent(MemoryProductionRuntimeAuditSinkV1())
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    with bind_sentinel_logical_call(logical_call):
        assert agent._production_safe_logging_active()
        with pytest.raises(TimeoutError, match="deadline elapsed"):
            agent._production_dispatch_client()
        with pytest.raises(RuntimeError, match="returned model differs"):
            agent._require_production_response_model(
                SimpleNamespace(model="another-served-model"), "cpu-fake-actor"
            )


def test_elapsed_actor_deadline_has_no_dispatch_and_commits_typed_failure(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    sentinel = agent._prompt_sentinel
    logical_call = sentinel.logical_call(
        host_id=_Agent.sentinel_host_id,
        history_codec_id=_Agent.sentinel_history_codec_id,
        attributes={"r24_case_deadline_monotonic_ns": 1},
    )
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=3,
            )
            assert prediction is None
            failure = agent._finalize_prompt_sentinel_actor_failure(
                logical_call,
                failure_phase="ACTOR_DEADLINE",
                failure_code="ACTOR_CASE_DEADLINE_ELAPSED",
            )
    finally:
        run.close()
    assert calls == []
    assert failure.provider_attempt_count == 0
    assert failure.live_openai_calls == 0
    assert failure.failure_code == "ACTOR_CASE_DEADLINE_ELAPSED"
    assert audit.latest_failure_receipt == failure


def test_actor_provider_failure_publishes_terminal_negative_audit(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)

    def fail(**kwargs: Any) -> _ActorResponse:
        calls.append(kwargs)
        raise RuntimeError("private provider failure")

    agent.openai_client.chat.completions.create = fail
    run, context = _collector_context(tmp_path)
    try:
        with bind_audit_context(context), agent._sentinel_logical_call_scope() as logical_call:
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction is None
            failure = agent._finalize_prompt_sentinel_actor_failure(
                logical_call,
                failure_phase="ACTOR_PROVIDER",
                failure_code="ACTOR_PROVIDER_FAILED",
            )
            assert failure is not None
    finally:
        run.close()

    assert len(calls) == 1
    assert audit.pending_count == 0
    receipt = audit.latest_failure_receipt
    assert receipt is not None
    assert receipt.failure_code == "ACTOR_PROVIDER_FAILED"
    assert receipt.provider_attempt_count == 1
    assert receipt.live_openai_calls == 0
    assert receipt.live_cost_exact
    assert len(sink.failure_details) == 1
    detail = sink.failure_details[0]
    assert type(detail) is dict
    assert detail["status"] == "FAILED"
    assert detail["actor_provider_attempts"][0]["status"] == "FAILED"


def test_missing_collector_request_locator_blocks_before_provider(tmp_path: Path) -> None:
    sink = MemoryProductionRuntimeAuditSinkV1()
    agent, audit, calls = _off_agent(sink)
    with agent._sentinel_logical_call_scope():
        with pytest.raises(ProductionRuntimeAuditError) as failure:
            agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
    assert failure.value.code == "COLLECTOR_LOCATOR_INVALID"
    assert calls == []
    assert audit.pending_count == 1


@pytest.mark.parametrize(
    ("host_id", "mode"),
    (
        ("mobileworld.qwen3vl.actor", SentinelMode.SHADOW),
        ("mobileworld.mai-ui.actor", SentinelMode.ACTIVE),
    ),
)
def test_qwen_and_mai_unsupported_shape_fallback_reaches_actor_provider(
    tmp_path: Path,
    host_id: str,
    mode: SentinelMode,
) -> None:
    policy = _unstarted_exact_live_policy()
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={host_id: SentinelHostConfig(mode=mode)},
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: f"fallback-{mode.value.lower()}-call-1",
    )
    calls: list[dict[str, Any]] = []

    def respond(request: Request) -> Response:
        calls.append(json.loads(request.content))
        timeout = request.extensions.get("timeout")
        assert type(timeout) is dict
        assert all(float(value) <= 1.0 for value in timeout.values() if value is not None)
        return Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {"content": "actor fallback output", "role": "assistant"},
                    }
                ],
                "created": 1,
                "id": "actor-response-1",
                "model": "cpu-fake-actor",
                "object": "chat.completion",
                "usage": {
                    "completion_tokens": 7,
                    "prompt_tokens": 11,
                    "total_tokens": 18,
                },
            },
        )

    agent = _FallbackAgent(
        prompt_sentinel=sentinel,
        sentinel_host_id=host_id,
        sentinel_history_codec_id=None,
    )
    # Base's fallback to its class declaration is absent, so this is the
    # ordinary typed unsupported-family input before any live call occurs.
    agent._sentinel_history_codec_id = None
    agent.openai_client = OpenAI(
        base_url="http://127.0.0.1:1/v1",
        api_key="empty",
        max_retries=0,
        http_client=DefaultHttpxClient(
            transport=MockTransport(respond), trust_env=False, timeout=1.0
        ),
    )
    run, context = _collector_context(tmp_path)
    action = JSONAction(action_type=WAIT)
    try:
        logical_call = sentinel.logical_call(
            host_id=host_id,
            history_codec_id=None,
            attributes={"r24_case_deadline_monotonic_ns": monotonic_ns() + 1_000_000_000},
        )
        with bind_audit_context(context), bind_sentinel_logical_call(logical_call):
            prediction = agent.openai_chat_completions_create(
                model="cpu-fake-actor",
                messages=[{"role": "user", "content": "Wait now."}],
                retry_times=1,
            )
            assert prediction == "actor fallback output"
            agent._finalize_prompt_sentinel_actor_output(
                logical_call,
                prediction=prediction,
                action=action,
                parser_id="production-audit-test-parser",
                parser_succeeded=True,
                parser_attempt_count=1,
                parser_ns=10,
            )
            receipt = agent.finalize_prompt_sentinel_action_execution(
                action=action,
                action_executed=False,
            )
            assert receipt is not None
    finally:
        agent.openai_client.close()
        run.close()

    assert len(calls) == 1
    detail = sink.details[0]
    assert detail.pre_provider.status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
    assert (
        receipt.pre_provider_outcome
        is production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL
    )
    assert receipt.fallback_reason is SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY
    assert detail.pre_provider.raw_request_sha256 == detail.pre_provider.final_request_sha256
    assert detail.pre_provider.live_openai_calls == 0
    assert detail.pre_provider.live_cost_exact
    projection = production_runtime_audit_detail_projection(detail)
    pre_provider = cast(dict[str, JsonValue], projection["pre_provider"])
    restricted = cast(dict[str, JsonValue], pre_provider["restricted_stage_projection"])
    assert restricted["r2_4_rubric_request_proofs"] == []


def test_production_no_history_untrusted_rubric_record_fails_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = cast(dict[str, Any], json.loads(QWEN_FIXTURE.read_text(encoding="utf-8")))
    request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
    messages = cast(list[JsonValue], request["messages"])
    content = cast(list[JsonValue], cast(dict[str, JsonValue], messages[1])["content"])
    text_block = cast(dict[str, JsonValue], content[0])
    text = cast(str, text_block["text"])
    text_block["text"] = text[: text.index("Step 1: ")] + "\n"

    policy = _unstarted_exact_live_policy()
    policy._attempt_sink = MemoryLiveAttemptReceiptSinkV1()
    calls: list[str] = []

    def prepare_no_history(
        self: OwnerAuthorizedLivePerCallPolicyV1,
        *,
        request: JsonValue,
        context: Any,
        execution_control: Any,
    ) -> object:
        del request, execution_control
        calls.append(context.logical_call_id)
        self._call_inputs[context.logical_call_id] = "registered-no-history"
        return object()

    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "prepare_no_history_with_control",
        prepare_no_history,
    )
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
    logical_call_id = "production-no-history-call-1"
    sentinel = PromptSentinel(
        policy=policy,
        codec_registry=build_runtime_history_codec_resolver(),
        host_configs={
            "mobileworld.qwen3vl.actor": SentinelHostConfig(
                mode=SentinelMode.ACTIVE,
                policy_timeout_ms=1_000,
            )
        },
        receipt_sink=MemorySentinelReceiptSink(),
        runtime_audit=audit,
        global_switch=SentinelGlobalSwitch(),
        logical_call_id_factory=lambda: logical_call_id,
    )
    logical_call = sentinel.logical_call(
        host_id="mobileworld.qwen3vl.actor",
        history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
    )

    first = logical_call.before_model_call(cast(JsonValue, request))
    second = logical_call.before_model_call(cast(JsonValue, request))

    assert type(first) is type(second)
    assert type(first) is not RuntimeVerticalSentinelResultV1
    assert first.receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert first.raw_request == first.candidate_request == first.final_request == request
    assert second.raw_request == second.final_request == request
    assert calls == [logical_call_id]
    assert audit.pending_count == 1
    audit.cancel(logical_call_id)


def test_failed_live_attempt_root_does_not_require_completed_policy_binding() -> None:
    """Negative attempt evidence survives an Original fallback without inventing a call."""

    digest = "a" * 64
    attempt_hashes = ("b" * 64, "c" * 64)
    restricted: Any = {"kind": "FALLBACK_ORIGINAL"}
    restricted_hash = sha256(
        json.dumps(
            restricted,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    pre = production_audit_module.ProductionRuntimeAuditPreProviderV1(
        logical_call_id="fallback-negative-attempt-call-1",
        host_id="mobileworld.production-audit-test.actor",
        status=production_audit_module.ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL,
        outcome=production_audit_module.ProductionRuntimeAuditPreProviderOutcomeV1.GENERIC_FALLBACK_ORIGINAL,
        configured_mode=SentinelMode.ACTIVE,
        effective_mode=SentinelMode.OFF,
        fallback_reason=SentinelFallbackReason.POLICY_TIMEOUT,
        fallback_check="policy_timeout",
        raw_request_sha256=digest,
        extraction_sha256=None,
        history_ir_sha256=None,
        codec_overlay_sha256=None,
        vertical_output_sha256=None,
        coordinated_record_sha256=None,
        rubric_result_sha256=None,
        path_relevance_output_sha256=None,
        render_result_sha256=None,
        candidate_request_sha256=digest,
        exact_diff_sha256=digest,
        validator_result_sha256=digest,
        final_request_sha256=digest,
        live_call_binding_sha256=None,
        live_attempt_receipt_sha256s=attempt_hashes,
        live_attempt_receipt_root_sha256="d" * 64,
        case_execution_lease_sha256="e" * 64,
        preflight_report_sha256="f" * 64,
        factory_binding_sha256=None,
        execution_authority_sha256="1" * 64,
        source_transport_binding_sha256=None,
        pricing_binding_sha256="2" * 64,
        live_openai_calls=1,
        live_cost_usd_micros=7,
        live_cost_exact=True,
        restricted_stage_projection=restricted,
        restricted_stage_projection_sha256=restricted_hash,
        evidence_snapshot_ns=0,
        history_extract_ns=0,
        rubric_ns=1,
        policy_ns=1,
        render_ns=0,
        validator_ns=0,
        pre_provider_total_ns=1,
        _seal=production_audit_module._PRE_PROVIDER_SEAL,
    )
    assert pre.live_openai_calls == 1
    assert len(pre.live_attempt_receipt_sha256s) == 2

    receipt = production_audit_module.ProductionRuntimeAuditReceiptV1(
        detail_id="fallback-negative-attempt-detail-1",
        logical_call_id=pre.logical_call_id,
        raw_request_sha256=digest,
        final_request_sha256=digest,
        provider_request_sha256=digest,
        provider_response_sha256=digest,
        exact_diff_sha256=digest,
        pre_provider_sha256=digest,
        pre_provider_status=pre.status,
        pre_provider_outcome=pre.outcome,
        fallback_reason=pre.fallback_reason,
        fallback_check=pre.fallback_check,
        live_call_binding_sha256=None,
        live_attempt_receipt_root_sha256="d" * 64,
        actor_provider_attempt_root_sha256=digest,
        sentinel_receipt_sha256=digest,
        parser_input_sha256=digest,
        parser_result_sha256=digest,
        parsed_action_sha256=digest,
        action_executed=False,
        executed_action_sha256=None,
        provider_attempt_count=1,
        live_openai_calls=1,
        live_cost_usd_micros=7,
        live_cost_exact=True,
        total_ns=1,
        detail_sha256=digest,
        _seal=production_audit_module._RECEIPT_SEAL,
    )
    assert receipt.live_call_binding_sha256 is None
    assert receipt.live_attempt_receipt_root_sha256 == "d" * 64
