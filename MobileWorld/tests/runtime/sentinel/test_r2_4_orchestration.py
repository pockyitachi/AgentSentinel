from __future__ import annotations

import base64
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
)
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder, TaskRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel import (
    MemorySentinelReceiptSink,
    PromptSentinel,
    SentinelFallbackReason,
    SentinelGlobalSwitch,
    SentinelHostConfig,
    SentinelMode,
)
from mobile_world.runtime.sentinel.contracts import (
    SentinelCallRole,
    SentinelContext,
    SentinelReceipt,
    SentinelResult,
    SentinelValidationStatus,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
    GPT56_REQUESTED_MODEL,
    GPT56PolicyError,
    GPT56SentinelPolicy,
    ProposalSchemaSnapshotV1,
    ResponsesEnvelopeV1,
    ResponsesRequestV1,
    TransportDescriptorV1,
)
from mobile_world.runtime.sentinel.r2_2.metrics import R22PolicyMetrics
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
    RelevanceDisposition,
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
    TopologyKind,
    TopologyRunStatus,
    TrackerProposalStatus,
    path_relevance_output_sha256,
    rubric_tracking_state_sha256,
    task_start_request_projection,
    tracking_packet_projection,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.session import (
    RubricSessionStage,
    RubricSessionStatus,
    RubricTaskSession,
)
from mobile_world.runtime.sentinel.r2_4 import production_audit as production_audit_module
from mobile_world.runtime.sentinel.r2_4 import rubric_live as rubric_live_module
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceFactoryV1,
    rubric_evidence_snapshot_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    CanonicalHistoryPolicyRequestV1,
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
    ProductionOpenAIAttemptRunnerV1,
    live_attempt_receipt_projection,
    live_attempt_receipt_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    OwnerAuthorizedLivePerCallPolicyV1,
    ResolvedLivePolicyCallBindingV1,
    validate_live_rubric_cross_bindings_v1,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import (
    R24OrchestrationError,
    R24RuntimeCoordinatorV1,
    r24_coordinated_call_record_sha256,
    rubric_session_result_sha256,
)
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    MemoryProductionRuntimeAuditSinkV1,
    ParserResultStatusV1,
    ProductionRuntimeAuditPreProviderOutcomeV1,
    ProductionRuntimeAuditPreProviderStatusV1,
    ProductionRuntimeAuditV1,
    production_runtime_audit_detail_projection,
    production_runtime_audit_pre_provider_sha256,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
    LIVE_RUBRIC_MODEL,
    LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
    LiveRubricAttemptRequestAnchorV1,
    LiveRubricCallReceiptV1,
    LiveRubricCallTrustAnchorV1,
    LiveRubricError,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    LiveRubricTransportAuthorityV1,
    LiveRubricTransportKindV1,
    R24RubricBackendExtensionDescriptorV1,
    bind_current_collector_image,
    bind_current_collector_image_projection,
    build_live_rubric_provider_request_v1,
    live_rubric_attempt_request_proof_projection,
    live_rubric_call_receipt_sha256,
    live_rubric_generate_schema,
    live_rubric_operation_prompt_sha256,
    live_rubric_prompt_bundle_sha256,
    live_rubric_track_schema,
    rubric_backend_descriptor_sha256,
    validate_live_rubric_request_proof_projection_v1,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
QWEN_FIXTURE = (
    REPO_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
    "qwen_flat_progress.captured.v1.json"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checked_in_r23_schema_hash(filename: str) -> str:
    return hashlib.sha256(
        (REPO_ROOT / "mobileworld_audit_handoff/schemas/r2_3" / filename).read_bytes()
    ).hexdigest()


def _live_rubric_request_material(
    *,
    stimulus: Any,
    current_image: Any,
    logical_call_id: str,
    packet: RubricTrackingPacketV1,
    descriptor_id: str,
) -> tuple[
    R24RubricBackendExtensionDescriptorV1,
    tuple[dict[str, JsonValue], dict[str, JsonValue]],
    tuple[CanonicalHistoryPolicyRequestV1, CanonicalHistoryPolicyRequestV1],
]:
    descriptor = RubricBackendDescriptorV1(
        backend_id=f"{descriptor_id}-r23",
        backend_version="r2.4-v1",
        prompt_sha256=live_rubric_prompt_bundle_sha256(),
        rubric_schema_sha256=_checked_in_r23_schema_hash("rubric.v1.schema.json"),
        tracking_packet_schema_sha256=_checked_in_r23_schema_hash("tracking_packet.v1.schema.json"),
        tracker_schema_sha256=_checked_in_r23_schema_hash("tracker_output.v1.schema.json"),
        config_sha256=_sha(f"{descriptor_id}-r23-config"),
    )
    extension = R24RubricBackendExtensionDescriptorV1(
        descriptor_id=descriptor_id,
        descriptor_version="v1",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION,
        r23_compatibility_descriptor_sha256=rubric_backend_descriptor_sha256(descriptor),
        provider_config_sha256=_sha(f"{descriptor_id}-provider-config"),
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
        request_id=f"{descriptor_id}-task-start",
        task_run_id=stimulus.task_run_id,
        task=stimulus.task,
        backend=descriptor,
    )
    common: dict[str, JsonValue] = {
        "backend_extension_descriptor_sha256": extension.sha256,
        "r23_compatibility_descriptor_sha256": extension.r23_compatibility_descriptor_sha256,
    }
    generate_input: dict[str, JsonValue] = {
        **common,
        "schema_version": LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION,
        "request": cast(JsonValue, task_start_request_projection(task_start)),
    }
    track_input: dict[str, JsonValue] = {
        **common,
        "schema_version": LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
        "current_image_binding_sha256": current_image.binding_sha256,
        "packet": cast(JsonValue, tracking_packet_projection(packet)),
    }
    inputs = (generate_input, track_input)
    requests = (
        build_live_rubric_provider_request_v1(
            operation=LiveRubricOperationV1.GENERATE,
            provider_input=generate_input,
            current_image_data_url=None,
        ),
        build_live_rubric_provider_request_v1(
            operation=LiveRubricOperationV1.TRACK,
            provider_input=track_input,
            current_image_data_url=current_image.data_url,
        ),
    )
    return extension, inputs, requests


def _complete_live_rubric_cross_binding_proof(
    tmp_path: Path,
) -> tuple[
    R24RubricBackendExtensionDescriptorV1,
    tuple[LiveAttemptReceiptV1, ...],
    tuple[LiveRubricCallReceiptV1, ...],
    tuple[LiveRubricCallTrustAnchorV1, ...],
    ResolvedLivePolicyCallBindingV1,
]:
    logical_call_id = "r24-cross-binding-call-1"
    actor_request_sha256 = _sha("cross-binding-actor-request")
    manifest_sha256 = _sha("cross-binding-manifest")
    preflight_sha256 = _sha("cross-binding-preflight")
    lease_sha256 = _sha("cross-binding-lease")
    stage_sha256 = _sha("cross-binding-rubric-stage")
    pricing_sha256 = _sha("cross-binding-pricing")
    runtime = _runtime_case(tmp_path)
    cross_context = SentinelContext(
        logical_call_id=logical_call_id,
        host_id=runtime.context.host_id,
    )
    sessions = _harness()
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    try:
        with bind_audit_context(runtime.audit_context):
            coordinator(cast(JsonValue, runtime.request), cross_context, runtime.history_ir)
            bundle = CollectorEvidenceFactoryV1().bundle_for_call(
                request=cast(JsonValue, runtime.request),
                context=cross_context,
                history_ir=runtime.history_ir,
            )
    finally:
        runtime.run.close()
    current_image = bind_current_collector_image(
        bundle,
        logical_call_id=logical_call_id,
    )
    extension, provider_inputs, provider_requests = _live_rubric_request_material(
        stimulus=bundle.r23_snapshot,
        current_image=current_image,
        logical_call_id=logical_call_id,
        packet=sessions.trackers[0].packets[0],
        descriptor_id="r24-cross-binding-extension",
    )
    operations = (LiveRubricOperationV1.GENERATE, LiveRubricOperationV1.TRACK)
    envelopes = tuple(
        ResponsesEnvelopeV1(
            response_id=f"r24-cross-binding-response-{index}",
            requested_model=LIVE_RUBRIC_MODEL,
            returned_model=LIVE_RUBRIC_MODEL,
            status="completed",
            service_tier="default",
            output_text=json.dumps({"operation": operation.value, "index": index}),
            input_tokens=2 + index,
            output_tokens=1,
            total_tokens=3 + index,
        )
        for index, operation in enumerate(operations, start=1)
    )
    attempts = tuple(
        LiveAttemptReceiptV1(
            attempt_id=f"r24-cross-binding-attempt-{index}",
            role=LiveAttemptRoleV1.RUBRIC,
            authority_sha256=_sha(f"cross-binding-authority-{index}"),
            manifest_sha256=manifest_sha256,
            preflight_sha256=preflight_sha256,
            case_execution_lease_sha256=lease_sha256,
            stage_sha256=stage_sha256,
            case_id="r24-cross-binding-case",
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            request_sha256=provider_request.request_sha256,
            transport_binding_sha256=_sha(f"cross-binding-transport-{index}"),
            pricing_binding_sha256=pricing_sha256,
            execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
            status=LiveAttemptStatusV1.COMPLETED,
            dispatch_count=1,
            response_envelope_sha256=envelope.sha256,
            requested_model=LIVE_RUBRIC_MODEL,
            returned_model=LIVE_RUBRIC_MODEL,
            input_tokens=2 + index,
            cached_input_tokens=0,
            output_tokens=1,
            total_tokens=3 + index,
            cost_status=LiveAttemptCostStatusV1.EXACT,
            cost_usd_micros=index,
            cancellation_requested=False,
            termination=LiveAttemptTerminationV1.NONE,
            worker_pid=20_000 + index,
            worker_exit_code=0,
            worker_reaped=True,
            late_output_detected=False,
            duration_ns=index,
            failure_code=None,
        )
        for index, (envelope, provider_request) in enumerate(
            zip(envelopes, provider_requests, strict=True), start=1
        )
    )
    attempt_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
    receipts = tuple(
        LiveRubricCallReceiptV1(
            receipt_id=f"r24-cross-binding-receipt-{index}",
            operation=operation,
            execution_scope=extension.execution_scope,
            task_run_id=bundle.r23_snapshot.task_run_id,
            logical_call_id=logical_call_id,
            backend_extension_descriptor_sha256=extension.sha256,
            r23_compatibility_descriptor_sha256=(extension.r23_compatibility_descriptor_sha256),
            transport_kind=extension.transport_kind,
            transport_authority=extension.transport_authority,
            prompt_sha256=live_rubric_operation_prompt_sha256(operation),
            provider_input_schema_version=(
                LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION
                if operation is LiveRubricOperationV1.GENERATE
                else LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION
            ),
            provider_output_schema_sha256=(
                extension.generate_output_schema_sha256
                if operation is LiveRubricOperationV1.GENERATE
                else extension.track_output_schema_sha256
            ),
            provider_request_sha256=attempt.request_sha256,
            provider_output_sha256=envelope.output_text_sha256,
            transport_binding_sha256=attempt.transport_binding_sha256,
            pricing_binding_sha256=attempt.pricing_binding_sha256,
            current_image_binding_sha256=(
                None
                if operation is LiveRubricOperationV1.GENERATE
                else current_image.binding_sha256
            ),
            manifest_sha256=attempt.manifest_sha256,
            preflight_sha256=attempt.preflight_sha256,
            case_execution_lease_sha256=attempt.case_execution_lease_sha256,
            stage_sha256=attempt.stage_sha256,
            attempt_authority_sha256=attempt.authority_sha256,
            attempt_receipt_sha256=attempt_hash,
            requested_model=extension.configured_model,
            returned_model=extension.configured_model,
            dispatch_count=attempt.dispatch_count,
            input_tokens=attempt.input_tokens,
            output_tokens=attempt.output_tokens,
            total_tokens=attempt.total_tokens,
            cost_usd_micros=attempt.cost_usd_micros,
        )
        for index, (operation, attempt, attempt_hash, envelope) in enumerate(
            zip(operations, attempts, attempt_hashes, envelopes, strict=True),
            start=1,
        )
    )
    anchors = tuple(
        rubric_live_module._build_live_rubric_call_trust_anchor(
            operation=operation,
            task_run_id=bundle.r23_snapshot.task_run_id,
            logical_call_id=logical_call_id,
            collector_stimulus=bundle.r23_snapshot,
            current_image=(None if operation is LiveRubricOperationV1.GENERATE else current_image),
            provider_input=provider_input,
            provider_request=provider_request,
            response_envelope=envelope,
        )
        for operation, envelope, provider_input, provider_request in zip(
            operations,
            envelopes,
            provider_inputs,
            provider_requests,
            strict=True,
        )
    )
    binding = ResolvedLivePolicyCallBindingV1(
        logical_call_id=logical_call_id,
        actor_call_index=1,
        actor_request_sha256=actor_request_sha256,
        policy_id="r24-cross-binding-policy",
        execution_authority_sha256=manifest_sha256,
        source_transport_descriptor_sha256=_sha("cross-binding-history-descriptor"),
        source_transport_binding_sha256=None,
        case_execution_lease_sha256=lease_sha256,
        preflight_report_sha256=preflight_sha256,
        factory_binding_sha256=_sha("cross-binding-factory"),
        pricing_binding_sha256=pricing_sha256,
        rubric_backend_extension_descriptor_sha256=extension.sha256,
        rubric_attempt_receipt_sha256s=attempt_hashes,
        rubric_call_receipt_sha256s=tuple(
            live_rubric_call_receipt_sha256(item) for item in receipts
        ),
        history_policy_attempt_receipt_sha256=None,
        output_sha256=None,
        openai_calls=2,
        cost_usd_micros=3,
    )
    return extension, attempts, receipts, anchors, binding


def _anchored_tracking_packet_sha256(
    anchors: tuple[LiveRubricCallTrustAnchorV1, ...],
) -> str:
    track = tuple(item for item in anchors if item.operation is LiveRubricOperationV1.TRACK)
    assert len(track) == 1
    packet = track[0].provider_input["packet"]
    assert type(packet) is dict
    return canonical_sha256(cast(JsonValue, packet))


def _attempt_request_anchors(
    attempts: tuple[LiveAttemptReceiptV1, ...],
    call_anchors: tuple[LiveRubricCallTrustAnchorV1, ...],
) -> tuple[LiveRubricAttemptRequestAnchorV1, ...]:
    rubric_attempts = tuple(value for value in attempts if value.role is LiveAttemptRoleV1.RUBRIC)
    return tuple(
        rubric_live_module._build_live_rubric_attempt_request_anchor(
            operation=call_anchor.operation,
            task_run_id=call_anchor.task_run_id,
            logical_call_id=call_anchor.logical_call_id,
            attempt_id=attempt.attempt_id,
            attempt_order=index,
            collector_stimulus=call_anchor.collector_stimulus,
            current_image=call_anchor.current_image,
            provider_input=call_anchor.provider_input,
            provider_request=call_anchor.provider_request,
        )
        for index, (attempt, call_anchor) in enumerate(
            zip(rubric_attempts, call_anchors, strict=False),
            start=1,
        )
    )


@pytest.mark.parametrize(
    "drift",
    (
        "attempt_logical_call_id",
        "attempt_actor_request",
        "attempt_role",
        "attempt_status",
        "attempt_requested_model",
        "attempt_returned_model",
        "receipt_logical_call_id",
        "receipt_manifest",
        "receipt_preflight",
        "receipt_case_lease",
        "receipt_stage",
        "receipt_attempt_authority",
        "receipt_attempt_hash",
        "receipt_provider_request",
        "receipt_transport_binding",
        "receipt_pricing_binding",
        "receipt_dispatch_count",
        "receipt_input_tokens",
        "receipt_output_tokens",
        "receipt_total_tokens",
        "receipt_cost",
        "receipt_extension_hash",
        "receipt_r23_hash",
        "receipt_scope",
        "receipt_transport_kind",
        "receipt_transport_authority",
        "receipt_requested_model",
        "receipt_returned_model",
        "receipt_input_schema",
        "receipt_output_schema",
        "receipt_operation",
        "receipt_current_image",
        "extension_configured_model",
        "binding_logical_call_id",
        "binding_actor_request",
        "binding_case_lease",
        "binding_preflight",
        "binding_pricing",
        "binding_extension_hash",
        "binding_attempt_order",
        "binding_receipt_order",
        "binding_census",
        "binding_cost",
    ),
)
def test_live_rubric_cross_binding_rejects_every_field_drift(
    drift: str,
    tmp_path: Path,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    logical_call_id = binding.logical_call_id
    actor_request_sha256 = binding.actor_request_sha256
    collector_stimulus_sha256 = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=logical_call_id,
        actor_request_sha256=actor_request_sha256,
        attempts=attempts,
        rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
        rubric_call_receipts=receipts,
        rubric_call_trust_anchors=anchors,
        expected_collector_stimulus_sha256=collector_stimulus_sha256,
        expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
        rubric_backend_extension=extension,
        binding=binding,
        actor_call_index=1,
        expect_history_policy=False,
        allow_incomplete=False,
    )

    def replace_attempt(index: int = 0, **changes: object) -> None:
        nonlocal attempts
        values = list(attempts)
        values[index] = replace(values[index], **changes)
        attempts = tuple(values)

    def replace_receipt(index: int = 0, **changes: object) -> None:
        nonlocal receipts
        values = list(receipts)
        values[index] = replace(values[index], **changes)
        receipts = tuple(values)

    def corrupt_attempt(field: str, value: object, *, index: int = 0) -> None:
        nonlocal attempts
        values = list(attempts)
        item = replace(values[index])
        object.__setattr__(item, field, value)
        values[index] = item
        attempts = tuple(values)

    def corrupt_receipt(field: str, value: object, *, index: int = 0) -> None:
        nonlocal receipts
        values = list(receipts)
        item = replace(values[index])
        object.__setattr__(item, field, value)
        values[index] = item
        receipts = tuple(values)

    changed_sha256 = _sha(f"cross-binding-drift:{drift}")
    if drift == "attempt_logical_call_id":
        replace_attempt(logical_call_id="r24-cross-binding-other-call")
    elif drift == "attempt_actor_request":
        replace_attempt(actor_request_sha256=changed_sha256)
    elif drift == "attempt_role":
        replace_attempt(role=LiveAttemptRoleV1.HISTORY_POLICY)
    elif drift == "attempt_status":
        corrupt_attempt("status", LiveAttemptStatusV1.FAILED)
    elif drift == "attempt_requested_model":
        corrupt_attempt("requested_model", "different-model")
    elif drift == "attempt_returned_model":
        corrupt_attempt("returned_model", "different-model")
    elif drift == "receipt_logical_call_id":
        replace_receipt(logical_call_id="r24-cross-binding-other-call")
    elif drift == "receipt_manifest":
        replace_receipt(manifest_sha256=changed_sha256)
    elif drift == "receipt_preflight":
        replace_receipt(preflight_sha256=changed_sha256)
    elif drift == "receipt_case_lease":
        replace_receipt(case_execution_lease_sha256=changed_sha256)
    elif drift == "receipt_stage":
        replace_receipt(stage_sha256=changed_sha256)
    elif drift == "receipt_attempt_authority":
        replace_receipt(attempt_authority_sha256=changed_sha256)
    elif drift == "receipt_attempt_hash":
        replace_receipt(attempt_receipt_sha256=changed_sha256)
    elif drift == "receipt_provider_request":
        replace_receipt(provider_request_sha256=changed_sha256)
    elif drift == "receipt_transport_binding":
        replace_receipt(transport_binding_sha256=changed_sha256)
    elif drift == "receipt_pricing_binding":
        replace_receipt(pricing_binding_sha256=changed_sha256)
    elif drift == "receipt_dispatch_count":
        corrupt_receipt("dispatch_count", 0)
    elif drift == "receipt_input_tokens":
        replace_receipt(input_tokens=10, total_tokens=11)
    elif drift == "receipt_output_tokens":
        replace_receipt(output_tokens=10, total_tokens=13)
    elif drift == "receipt_total_tokens":
        corrupt_receipt("total_tokens", 99)
    elif drift == "receipt_cost":
        replace_receipt(cost_usd_micros=99)
    elif drift == "receipt_extension_hash":
        replace_receipt(backend_extension_descriptor_sha256=changed_sha256)
    elif drift == "receipt_r23_hash":
        replace_receipt(r23_compatibility_descriptor_sha256=changed_sha256)
    elif drift == "receipt_scope":
        corrupt_receipt("execution_scope", LiveRubricExecutionScopeV1.CPU_TEST_LOCAL)
    elif drift == "receipt_transport_kind":
        corrupt_receipt("transport_kind", LiveRubricTransportKindV1.INJECTED_FAKE)
    elif drift == "receipt_transport_authority":
        corrupt_receipt("transport_authority", LiveRubricTransportAuthorityV1.CPU_OFFLINE_FAKE)
    elif drift == "receipt_requested_model":
        corrupt_receipt("requested_model", "different-model")
    elif drift == "receipt_returned_model":
        corrupt_receipt("returned_model", "different-model")
    elif drift == "receipt_input_schema":
        replace_receipt(provider_input_schema_version="r24-drift-input-v1")
    elif drift == "receipt_output_schema":
        replace_receipt(provider_output_schema_sha256=changed_sha256)
    elif drift == "receipt_operation":
        replace_receipt(
            operation=LiveRubricOperationV1.TRACK,
            provider_input_schema_version=LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION,
            provider_output_schema_sha256=extension.track_output_schema_sha256,
            current_image_binding_sha256=_sha("cross-binding-drift-image"),
        )
    elif drift == "receipt_current_image":
        corrupt_receipt("current_image_binding_sha256", None, index=1)
    elif drift == "extension_configured_model":
        extension = replace(extension)
        object.__setattr__(extension, "configured_model", "different-model")
    elif drift == "binding_logical_call_id":
        binding = replace(binding, logical_call_id="r24-cross-binding-other-call")
    elif drift == "binding_actor_request":
        binding = replace(binding, actor_request_sha256=changed_sha256)
    elif drift == "binding_case_lease":
        binding = replace(binding, case_execution_lease_sha256=changed_sha256)
    elif drift == "binding_preflight":
        binding = replace(binding, preflight_report_sha256=changed_sha256)
    elif drift == "binding_pricing":
        binding = replace(binding, pricing_binding_sha256=changed_sha256)
    elif drift == "binding_extension_hash":
        binding = replace(binding, rubric_backend_extension_descriptor_sha256=changed_sha256)
    elif drift == "binding_attempt_order":
        binding = replace(
            binding,
            rubric_attempt_receipt_sha256s=tuple(reversed(binding.rubric_attempt_receipt_sha256s)),
        )
    elif drift == "binding_receipt_order":
        binding = replace(
            binding,
            rubric_call_receipt_sha256s=tuple(reversed(binding.rubric_call_receipt_sha256s)),
        )
    elif drift == "binding_census":
        object.__setattr__(binding, "openai_calls", 3)
    elif drift == "binding_cost":
        binding = replace(binding, cost_usd_micros=99)
    else:
        raise AssertionError(f"unknown drift: {drift}")

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=logical_call_id,
            actor_request_sha256=actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=collector_stimulus_sha256,
            expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
            rubric_backend_extension=extension,
            binding=binding,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=False,
        )


@pytest.mark.parametrize("proof_path", ("success", "no_history", "generic_fallback"))
def test_live_rubric_anchor_rejects_joint_request_hash_and_root_rewrite(
    proof_path: str,
    tmp_path: Path,
) -> None:
    """Rehashing attempt/call/binding roots cannot replace the dispatched request."""

    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    expect_history_policy = proof_path == "success"
    if expect_history_policy:
        history_attempt = replace(
            attempts[-1],
            attempt_id="r24-joint-rewrite-history-attempt",
            role=LiveAttemptRoleV1.HISTORY_POLICY,
            authority_sha256=_sha("joint-rewrite-history-authority"),
            request_sha256=_sha("joint-rewrite-history-request"),
            transport_binding_sha256=_sha("joint-rewrite-history-transport"),
            response_envelope_sha256=_sha("joint-rewrite-history-envelope"),
            cost_usd_micros=4,
        )
        attempts = (*attempts, history_attempt)
        binding = replace(
            binding,
            source_transport_binding_sha256=history_attempt.transport_binding_sha256,
            history_policy_attempt_receipt_sha256=live_attempt_receipt_sha256(history_attempt),
            output_sha256=_sha("joint-rewrite-history-output"),
            openai_calls=3,
            cost_usd_micros=7,
        )

    changed_request_sha256 = _sha(f"joint-rewrite:{proof_path}")
    changed_attempt = replace(attempts[0], request_sha256=changed_request_sha256)
    attempts = (changed_attempt, *attempts[1:])
    changed_receipt = replace(
        receipts[0],
        provider_request_sha256=changed_request_sha256,
        attempt_receipt_sha256=live_attempt_receipt_sha256(changed_attempt),
    )
    receipts = (changed_receipt, *receipts[1:])
    selected_binding: ResolvedLivePolicyCallBindingV1 | None = (
        None
        if proof_path == "generic_fallback"
        else replace(
            binding,
            rubric_attempt_receipt_sha256s=tuple(
                live_attempt_receipt_sha256(item)
                for item in attempts
                if item.role is LiveAttemptRoleV1.RUBRIC
            ),
            rubric_call_receipt_sha256s=tuple(
                live_rubric_call_receipt_sha256(item) for item in receipts
            ),
        )
    )

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
            rubric_backend_extension=extension,
            binding=selected_binding,
            actor_call_index=1,
            expect_history_policy=expect_history_policy,
            allow_incomplete=proof_path == "generic_fallback",
        )


def test_live_rubric_anchor_binds_complete_coordinator_tracking_packet(tmp_path: Path) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    expected_tracking_packet_sha256 = _anchored_tracking_packet_sha256(anchors)
    changed_input = deepcopy(anchors[1].provider_input)
    packet = cast(dict[str, JsonValue], changed_input["packet"])
    prior_state = cast(dict[str, JsonValue], packet["prior_state"])
    prior_state["state_id"] = "rewritten-prior-state"
    changed_request = build_live_rubric_provider_request_v1(
        operation=LiveRubricOperationV1.TRACK,
        provider_input=changed_input,
        current_image_data_url=cast(Any, anchors[1].current_image).data_url,
    )
    changed_anchor = rubric_live_module._build_live_rubric_call_trust_anchor(
        operation=anchors[1].operation,
        task_run_id=anchors[1].task_run_id,
        logical_call_id=anchors[1].logical_call_id,
        collector_stimulus=anchors[1].collector_stimulus,
        current_image=anchors[1].current_image,
        provider_input=changed_input,
        provider_request=changed_request,
        response_envelope=anchors[1].response_envelope,
    )
    anchors = (anchors[0], changed_anchor)
    changed_attempt = replace(attempts[1], request_sha256=changed_request.request_sha256)
    attempts = (attempts[0], changed_attempt)
    changed_receipt = replace(
        receipts[1],
        provider_request_sha256=changed_request.request_sha256,
        attempt_receipt_sha256=live_attempt_receipt_sha256(changed_attempt),
    )
    receipts = (receipts[0], changed_receipt)
    binding = replace(
        binding,
        rubric_attempt_receipt_sha256s=tuple(
            live_attempt_receipt_sha256(item) for item in attempts
        ),
        rubric_call_receipt_sha256s=tuple(
            live_rubric_call_receipt_sha256(item) for item in receipts
        ),
    )

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=expected_tracking_packet_sha256,
            rubric_backend_extension=extension,
            binding=binding,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=False,
        )


def test_durable_live_rubric_request_proof_is_independently_reconstructable(
    tmp_path: Path,
) -> None:
    extension, attempts, _, anchors, _ = _complete_live_rubric_cross_binding_proof(tmp_path)
    attempt_anchors = _attempt_request_anchors(attempts, anchors)
    proofs = tuple(
        live_rubric_attempt_request_proof_projection(
            anchor,
            attempt_receipt=attempt,
            backend_extension=extension,
        )
        for anchor, attempt in zip(attempt_anchors, attempts, strict=True)
    )
    for index, (proof, attempt) in enumerate(zip(proofs, attempts, strict=True), start=1):
        validate_live_rubric_request_proof_projection_v1(
            cast(JsonValue, proof),
            attempt_receipt=attempt,
            expected_attempt_order=index,
        )
        request = cast(dict[str, JsonValue], proof["provider_request"])
        assert request["model"] == LIVE_RUBRIC_MODEL
        assert request["store"] is False
        assert request["stream"] is False
        assert request["max_output_tokens"] == 8192
    assert proofs[0]["tracking_packet_sha256"] is None
    assert proofs[1]["tracking_packet_sha256"] == _anchored_tracking_packet_sha256(anchors)


def test_live_rubric_request_anchor_cannot_be_publicly_fabricated(tmp_path: Path) -> None:
    _, attempts, _, anchors, _ = _complete_live_rubric_cross_binding_proof(tmp_path)
    trusted = anchors[0]
    with pytest.raises(LiveRubricError, match="UNTRUSTED_REQUEST_ANCHOR"):
        LiveRubricCallTrustAnchorV1(
            operation=trusted.operation,
            task_run_id=trusted.task_run_id,
            logical_call_id=trusted.logical_call_id,
            collector_stimulus=trusted.collector_stimulus,
            current_image=trusted.current_image,
            provider_input=trusted.provider_input,
            provider_request=trusted.provider_request,
            response_envelope=trusted.response_envelope,
        )
    trusted_attempt = _attempt_request_anchors(attempts, anchors)[0]
    with pytest.raises(LiveRubricError, match="UNTRUSTED_REQUEST_ANCHOR"):
        LiveRubricAttemptRequestAnchorV1(
            operation=trusted_attempt.operation,
            task_run_id=trusted_attempt.task_run_id,
            logical_call_id=trusted_attempt.logical_call_id,
            attempt_id=trusted_attempt.attempt_id,
            attempt_order=trusted_attempt.attempt_order,
            collector_stimulus=trusted_attempt.collector_stimulus,
            current_image=trusted_attempt.current_image,
            provider_input=trusted_attempt.provider_input,
            provider_request=trusted_attempt.provider_request,
        )


def test_durable_live_rubric_request_proof_uses_type_sensitive_task_binding(
    tmp_path: Path,
) -> None:
    extension, attempts, _, anchors, _ = _complete_live_rubric_cross_binding_proof(tmp_path)
    proof = live_rubric_attempt_request_proof_projection(
        _attempt_request_anchors(attempts, anchors)[0],
        attempt_receipt=attempts[0],
        backend_extension=extension,
    )
    stimulus = cast(dict[str, JsonValue], proof["collector_stimulus"])
    stimulus_task = cast(dict[str, JsonValue], stimulus["task"])
    stimulus_task["source_event_seq"] = 1
    proof["collector_stimulus_sha256"] = canonical_sha256(cast(JsonValue, stimulus))
    provider_input = cast(dict[str, JsonValue], proof["provider_input"])
    request = cast(dict[str, JsonValue], provider_input["request"])
    provider_task = cast(dict[str, JsonValue], request["task"])
    provider_task["source_event_seq"] = True
    proof["provider_input_sha256"] = canonical_sha256(cast(JsonValue, provider_input))
    provider_request = build_live_rubric_provider_request_v1(
        operation=LiveRubricOperationV1.GENERATE,
        provider_input=provider_input,
        current_image_data_url=None,
    )
    proof["provider_request"] = cast(JsonValue, json.loads(provider_request.canonical_bytes))
    proof["provider_request_sha256"] = provider_request.request_sha256
    proof["provider_request_byte_count"] = provider_request.byte_count

    with pytest.raises(LiveRubricError, match="PROVIDER_INPUT_BINDING_MISMATCH"):
        rubric_live_module._validate_provider_input_stimulus_projection(
            operation=LiveRubricOperationV1.GENERATE,
            provider_input=provider_input,
            stimulus=stimulus,
            logical_call_id=anchors[0].logical_call_id,
        )
    with pytest.raises(LiveRubricError, match="INVALID_REQUEST_PROOF"):
        validate_live_rubric_request_proof_projection_v1(cast(JsonValue, proof))


def test_durable_live_rubric_request_proof_rejects_boolean_byte_count(tmp_path: Path) -> None:
    extension, attempts, _, anchors, _ = _complete_live_rubric_cross_binding_proof(tmp_path)
    proof = live_rubric_attempt_request_proof_projection(
        _attempt_request_anchors(attempts, anchors)[0],
        attempt_receipt=attempts[0],
        backend_extension=extension,
    )
    proof["provider_request_byte_count"] = True
    with pytest.raises(LiveRubricError, match="INVALID_REQUEST_PROOF"):
        validate_live_rubric_request_proof_projection_v1(cast(JsonValue, proof))


def test_durable_live_rubric_request_proof_parser_is_not_limited_to_provider_output() -> None:
    # The restricted audit sink is 256 MiB.  A proof can exceed the 8 MiB
    # provider-output cap because it contains both semantic and request preimages.
    oversized_for_output: JsonValue = {"padding": "x" * (8 * 1024 * 1024 + 1)}
    with pytest.raises(LiveRubricError) as rejected:
        validate_live_rubric_request_proof_projection_v1(oversized_for_output)
    assert "byte count is outside bounds" not in str(rejected.value)


def test_incomplete_fallback_keeps_generate_proof_when_track_attempt_fails(
    tmp_path: Path,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    failed_track = replace(
        attempts[1],
        status=LiveAttemptStatusV1.FAILED,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
        cost_usd_micros=None,
        failure_code="INJECTED_TRACK_PROVIDER_FAILURE",
    )
    incomplete_attempts = (attempts[0], failed_track)
    attempt_anchors = _attempt_request_anchors(incomplete_attempts, anchors)
    expected_tracking_packet_sha256 = _anchored_tracking_packet_sha256(anchors)

    validate_live_rubric_cross_bindings_v1(
        logical_call_id=binding.logical_call_id,
        actor_request_sha256=binding.actor_request_sha256,
        attempts=incomplete_attempts,
        rubric_attempt_request_anchors=attempt_anchors,
        rubric_call_receipts=(receipts[0],),
        rubric_call_trust_anchors=(anchors[0],),
        expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
            anchors[0].collector_stimulus
        ),
        expected_tracking_packet_sha256=expected_tracking_packet_sha256,
        rubric_backend_extension=extension,
        binding=None,
        actor_call_index=1,
        expect_history_policy=False,
        allow_incomplete=True,
    )
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=incomplete_attempts,
            rubric_attempt_request_anchors=(attempt_anchors[0],),
            rubric_call_receipts=(receipts[0],),
            rubric_call_trust_anchors=(anchors[0],),
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=expected_tracking_packet_sha256,
            rubric_backend_extension=extension,
            binding=None,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=True,
        )
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=(attempts[0],),
            rubric_attempt_request_anchors=attempt_anchors,
            rubric_call_receipts=(receipts[0],),
            rubric_call_trust_anchors=(anchors[0],),
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=expected_tracking_packet_sha256,
            rubric_backend_extension=extension,
            binding=None,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=True,
        )
    request_proofs = production_audit_module._rubric_request_proof_detail_projection(
        attempt_anchors,
        incomplete_attempts,
        extension,
        expected_tracking_packet_sha256,
    )
    assert len(request_proofs) == 2
    assert cast(dict[str, JsonValue], request_proofs[0])["operation"] == "GENERATE"
    assert cast(dict[str, JsonValue], request_proofs[1])["operation"] == "TRACK"
    assert cast(dict[str, JsonValue], request_proofs[1])["attempt_status"] == "FAILED"
    assert len((receipts[0],)) == 1
    assert live_attempt_receipt_projection(failed_track)["status"] == "FAILED"
    for index, (proof, attempt) in enumerate(
        zip(request_proofs, incomplete_attempts, strict=True), start=1
    ):
        validate_live_rubric_request_proof_projection_v1(
            proof,
            attempt_receipt=attempt,
            expected_attempt_order=index,
        )

    tampered_attempts = (
        attempts[0],
        replace(failed_track, request_sha256=_sha("tampered-failed-track-request")),
    )
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=tampered_attempts,
            rubric_attempt_request_anchors=attempt_anchors,
            rubric_call_receipts=(receipts[0],),
            rubric_call_trust_anchors=(anchors[0],),
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=expected_tracking_packet_sha256,
            rubric_backend_extension=extension,
            binding=None,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=True,
        )


def test_incomplete_fallback_rejects_completed_attempt_without_call_proof(
    tmp_path: Path,
) -> None:
    extension, attempts, _, anchors, binding = _complete_live_rubric_cross_binding_proof(tmp_path)
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=(attempts[0],),
            rubric_attempt_request_anchors=_attempt_request_anchors((attempts[0],), (anchors[0],)),
            rubric_call_receipts=(),
            rubric_call_trust_anchors=(),
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=None,
            rubric_backend_extension=extension,
            binding=None,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=True,
        )


def _patch_incomplete_audit_policy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    attempts: tuple[LiveAttemptReceiptV1, ...],
    request_anchors: tuple[LiveRubricAttemptRequestAnchorV1, ...],
    call_receipts: tuple[LiveRubricCallReceiptV1, ...],
    call_anchors: tuple[LiveRubricCallTrustAnchorV1, ...],
    extension: R24RubricBackendExtensionDescriptorV1,
    collector_root: str,
    packet_root: str | None,
) -> OwnerAuthorizedLivePerCallPolicyV1:
    policy = object.__new__(OwnerAuthorizedLivePerCallPolicyV1)
    policy._policy_id = "r24-failed-track-audit-policy"
    policy._authority = cast(
        Any,
        SimpleNamespace(manifest_sha256=attempts[0].manifest_sha256),
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "attempt_receipts_for_call",
        lambda _self, _logical_call_id: attempts,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_attempt_request_anchors_for_call",
        lambda _self, _logical_call_id: request_anchors,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_call_receipts_for_call",
        lambda _self, _logical_call_id: call_receipts,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_call_trust_anchors_for_call",
        lambda _self, _logical_call_id: call_anchors,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_backend_extension_descriptor",
        lambda _self: extension,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_collector_stimulus_sha256_for_call",
        lambda _self, _logical_call_id: collector_root,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "rubric_tracking_packet_sha256_for_call",
        lambda _self, _logical_call_id: packet_root,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "actor_call_index_for_call",
        lambda _self, _logical_call_id: 1,
    )
    monkeypatch.setattr(
        OwnerAuthorizedLivePerCallPolicyV1,
        "failure_for_call",
        lambda _self, _logical_call_id: "RUBRIC_TRACK_ERROR",
    )

    def no_binding(_self: object, _logical_call_id: str) -> object:
        raise R24ContractError(
            "PER_CALL_BINDING_UNAVAILABLE", "injected failed call has no binding"
        )

    monkeypatch.setattr(OwnerAuthorizedLivePerCallPolicyV1, "call_binding", no_binding)
    return policy


def test_generic_fallback_audit_retains_failed_track_attempt_request_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    raw: JsonValue = {"model": "cpu-actor", "messages": []}
    raw_bytes = canonical_json_bytes(raw)
    raw_sha256 = canonical_sha256(raw)
    completed_generate = replace(attempts[0], actor_request_sha256=raw_sha256)
    failed_track = replace(
        attempts[1],
        actor_request_sha256=raw_sha256,
        status=LiveAttemptStatusV1.FAILED,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
        cost_usd_micros=None,
        failure_code="INJECTED_TRACK_PROVIDER_FAILURE",
    )
    incomplete_attempts = (completed_generate, failed_track)
    completed_call = replace(
        receipts[0],
        attempt_receipt_sha256=live_attempt_receipt_sha256(completed_generate),
    )
    attempt_anchors = _attempt_request_anchors(incomplete_attempts, anchors)
    collector_root = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    packet_root = _anchored_tracking_packet_sha256(anchors)
    policy = _patch_incomplete_audit_policy(
        monkeypatch,
        attempts=incomplete_attempts,
        request_anchors=attempt_anchors,
        call_receipts=(completed_call,),
        call_anchors=(anchors[0],),
        extension=extension,
        collector_root=collector_root,
        packet_root=packet_root,
    )
    result = SentinelResult(
        receipt=SentinelReceipt(
            logical_call_id=binding.logical_call_id,
            host_id="mobileworld.qwen3vl.actor",
            call_role=SentinelCallRole.ACTOR,
            configured_mode=SentinelMode.ACTIVE,
            effective_mode=SentinelMode.OFF,
            bypass_reason=None,
            global_kill_switch_active=False,
            history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
            history_codec_contract_version="v1",
            policy_id=policy.policy_id,
            policy_output_sha256=_sha("failed-policy-output"),
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=canonical_sha256({"diffs": [], "list_insertions": []}),
            decision_kinds=(),
            policy_evaluated=True,
            would_edit=False,
            edit_applied=False,
            fallback_reason=SentinelFallbackReason.POLICY_EXCEPTION,
            validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
            validation_checks=("POLICY_EXCEPTION",),
            latency_ns=1,
        ),
        _raw_request_json=raw_bytes,
        _candidate_request_json=raw_bytes,
        _final_request_json=raw_bytes,
    )
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
    audit.begin_fallback_pre_provider(
        logical_call_id=binding.logical_call_id,
        host_id="mobileworld.qwen3vl.actor",
        raw_request=raw,
        result=result,
        fallback_check="r2_4_failed_track_request_proof",
        pre_provider_total_ns=1,
    )
    pending = audit._pending[binding.logical_call_id]
    stage = cast(dict[str, JsonValue], pending.pre_provider.restricted_stage_projection)
    persisted_attempts = cast(list[dict[str, JsonValue]], stage["live_attempt_receipts"])
    persisted_calls = cast(list[dict[str, JsonValue]], stage["r2_4_rubric_call_receipts"])
    persisted_proofs = cast(list[JsonValue], stage["r2_4_rubric_request_proofs"])
    assert len(persisted_attempts) == 2
    assert persisted_attempts[1]["status"] == "FAILED"
    assert len(persisted_calls) == 1
    assert len(persisted_proofs) == 2
    for index, (proof, attempt) in enumerate(
        zip(persisted_proofs, persisted_attempts, strict=True), start=1
    ):
        validate_live_rubric_request_proof_projection_v1(
            proof,
            attempt_receipt=attempt,
            expected_attempt_order=index,
        )
    invalid_receipt = deepcopy(persisted_attempts[1])
    invalid_receipt["worker_reaped"] = False
    invalid_proof = cast(dict[str, JsonValue], deepcopy(persisted_proofs[1]))
    invalid_proof["attempt_receipt_sha256"] = canonical_sha256(cast(JsonValue, invalid_receipt))
    with pytest.raises(LiveRubricError, match="INVALID_REQUEST_PROOF"):
        validate_live_rubric_request_proof_projection_v1(
            cast(JsonValue, invalid_proof),
            attempt_receipt=invalid_receipt,
            expected_attempt_order=2,
        )
    pending.transaction.abort()


def test_begin_tu_fallback_commits_request_proof_before_run_fatal_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    raw: JsonValue = {"model": "cpu-actor", "messages": []}
    raw_bytes = canonical_json_bytes(raw)
    raw_sha256 = canonical_sha256(raw)
    tu_generate = replace(
        attempts[0],
        actor_request_sha256=raw_sha256,
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
        worker_exit_code=None,
        worker_reaped=False,
        failure_code="TERMINATION_UNCONFIRMED",
    )
    incomplete_attempts = (tu_generate,)
    request_anchors = _attempt_request_anchors(
        incomplete_attempts,
        (anchors[0],),
    )
    policy = _patch_incomplete_audit_policy(
        monkeypatch,
        attempts=incomplete_attempts,
        request_anchors=request_anchors,
        call_receipts=(),
        call_anchors=(),
        extension=extension,
        collector_root=rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus),
        packet_root=None,
    )
    result = SentinelResult(
        receipt=SentinelReceipt(
            logical_call_id=binding.logical_call_id,
            host_id="mobileworld.qwen3vl.actor",
            call_role=SentinelCallRole.ACTOR,
            configured_mode=SentinelMode.ACTIVE,
            effective_mode=SentinelMode.OFF,
            bypass_reason=None,
            global_kill_switch_active=False,
            history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
            history_codec_contract_version="v1",
            policy_id=policy.policy_id,
            policy_output_sha256=_sha("tu-policy-output"),
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=canonical_sha256({"diffs": [], "list_insertions": []}),
            decision_kinds=(),
            policy_evaluated=True,
            would_edit=False,
            edit_applied=False,
            fallback_reason=SentinelFallbackReason.POLICY_EXCEPTION,
            validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
            validation_checks=("POLICY_EXCEPTION",),
            latency_ns=1,
        ),
        _raw_request_json=raw_bytes,
        _candidate_request_json=raw_bytes,
        _final_request_json=raw_bytes,
    )
    sink = MemoryProductionRuntimeAuditSinkV1()
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)

    with pytest.raises(
        production_audit_module.ProductionRuntimeAuditError,
        match="RUN_FATAL_TERMINATION_UNCONFIRMED",
    ):
        audit.begin_fallback_pre_provider(
            logical_call_id=binding.logical_call_id,
            host_id="mobileworld.qwen3vl.actor",
            raw_request=raw,
            result=result,
            fallback_check="r2_4_tu_request_proof",
            pre_provider_total_ns=1,
        )

    assert binding.logical_call_id not in audit._pending
    fatal = audit.run_fatal_latch.state
    assert fatal is not None
    assert fatal.attempt_receipt_sha256 == live_attempt_receipt_sha256(tu_generate)
    terminal = audit.latest_failure_receipt
    assert terminal is not None
    assert terminal.provider_attempt_count == 0
    assert terminal.live_openai_calls == 0
    (failure_detail,) = sink.failure_details
    failure = cast(dict[str, JsonValue], failure_detail)
    assert failure["actor_provider_attempts"] == []
    pre = cast(dict[str, JsonValue], failure["pre_provider"])
    stage = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    persisted_attempts = cast(list[dict[str, JsonValue]], stage["live_attempt_receipts"])
    persisted_proofs = cast(list[JsonValue], stage["r2_4_rubric_request_proofs"])
    assert len(persisted_attempts) == len(persisted_proofs) == 1
    assert persisted_attempts[0]["status"] == "TERMINATION_UNCONFIRMED"
    assert persisted_attempts[0]["dispatch_count"] == 0
    for index, (proof, attempt) in enumerate(
        zip(persisted_proofs, persisted_attempts, strict=True), start=1
    ):
        validate_live_rubric_request_proof_projection_v1(
            proof,
            attempt_receipt=attempt,
            expected_attempt_order=index,
        )
    with pytest.raises(
        production_audit_module.ProductionRuntimeAuditError,
        match="RUN_FATAL_TERMINATION_UNCONFIRMED",
    ):
        audit.bind_actor_sdk_arguments(
            logical_call_id=binding.logical_call_id,
            result=result,
            sdk_arguments=raw,
            collector_request_locator={},
            stream=False,
        )


def test_production_port_registers_sink_confirmed_begin_tu_request_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, attempts, _, anchors, _ = _complete_live_rubric_cross_binding_proof(tmp_path)
    source = anchors[0]
    tu = replace(
        attempts[0],
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
        worker_exit_code=None,
        worker_reaped=False,
        failure_code="TERMINATION_UNCONFIRMED",
    )
    runner = object.__new__(ProductionOpenAIAttemptRunnerV1)
    runner._sink = cast(
        Any,
        SimpleNamespace(receipt_for=lambda attempt_id: tu if attempt_id == tu.attempt_id else None),
    )
    runner._factory = cast(
        Any,
        SimpleNamespace(
            manifest_sha256=tu.manifest_sha256,
            preflight_report_sha256=tu.preflight_sha256,
            openai_stage_sha256=lambda _role: tu.stage_sha256,
        ),
    )
    runner._stage = cast(Any, SimpleNamespace(role="RUBRIC"))
    runner._pricing_sha256 = tu.pricing_binding_sha256
    port = object.__new__(rubric_live_module.ProductionRubricProviderPortV1)
    rubric_live_module._BaseRubricProviderPortV1.__init__(port)
    port._runner = runner
    current_image = anchors[1].current_image
    assert current_image is not None
    context = rubric_live_module._RubricCallContextV1(
        logical_call_id=tu.logical_call_id,
        task_run_id=source.task_run_id,
        actor_request_sha256=tu.actor_request_sha256,
        deadline_monotonic_ns=1,
        max_cost_usd_micros=1,
        stimulus=source.collector_stimulus,
        image=current_image,
        case_lease=cast(Any, object()),
        execution_control=cast(
            Any,
            SimpleNamespace(
                run_transport=lambda call: call(),
                publish_receipt=lambda publish: publish(),
            ),
        ),
    )
    monkeypatch.setattr(
        rubric_live_module,
        "case_execution_lease_sha256",
        lambda _lease: tu.case_execution_lease_sha256,
    )

    assert port._register_begin_termination_unconfirmed_request_anchor(
        operation=LiveRubricOperationV1.GENERATE,
        context=context,
        attempt_id=tu.attempt_id,
        transport_binding_sha256=tu.transport_binding_sha256,
        provider_input=source.provider_input,
        provider_request=source.provider_request,
    )
    (request_anchor,) = port.attempt_request_anchors
    assert request_anchor.attempt_id == tu.attempt_id
    assert request_anchor.attempt_order == 1
    assert request_anchor.provider_request.request_sha256 == tu.request_sha256


def test_begin_tu_commit_fault_recovery_retains_independent_request_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extension, attempts, _, anchors, binding = _complete_live_rubric_cross_binding_proof(tmp_path)
    raw: JsonValue = {"model": "cpu-actor", "messages": []}
    raw_bytes = canonical_json_bytes(raw)
    raw_sha256 = canonical_sha256(raw)
    tu_generate = replace(
        attempts[0],
        actor_request_sha256=raw_sha256,
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
        worker_exit_code=None,
        worker_reaped=False,
        failure_code="TERMINATION_UNCONFIRMED",
    )
    policy = _patch_incomplete_audit_policy(
        monkeypatch,
        attempts=(tu_generate,),
        request_anchors=_attempt_request_anchors((tu_generate,), (anchors[0],)),
        call_receipts=(),
        call_anchors=(),
        extension=extension,
        collector_root=rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus),
        packet_root=None,
    )
    result = SentinelResult(
        receipt=SentinelReceipt(
            logical_call_id=binding.logical_call_id,
            host_id="mobileworld.qwen3vl.actor",
            call_role=SentinelCallRole.ACTOR,
            configured_mode=SentinelMode.ACTIVE,
            effective_mode=SentinelMode.OFF,
            bypass_reason=None,
            global_kill_switch_active=False,
            history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
            history_codec_contract_version="v1",
            policy_id=policy.policy_id,
            policy_output_sha256=_sha("tu-commit-fault-policy-output"),
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=canonical_sha256({"diffs": [], "list_insertions": []}),
            decision_kinds=(),
            policy_evaluated=True,
            would_edit=False,
            edit_applied=False,
            fallback_reason=SentinelFallbackReason.POLICY_EXCEPTION,
            validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
            validation_checks=("POLICY_EXCEPTION",),
            latency_ns=1,
        ),
        _raw_request_json=raw_bytes,
        _candidate_request_json=raw_bytes,
        _final_request_json=raw_bytes,
    )
    attempted_failures: list[JsonValue] = []
    aborts: list[bool] = []

    def begin_fault(pre: object) -> object:
        trusted = cast(production_audit_module.ProductionRuntimeAuditPreProviderV1, pre)

        def commit_failure(detail: JsonValue) -> None:
            attempted_failures.append(deepcopy(detail))
            raise OSError("injected TU failed-terminal commit fault")

        return SimpleNamespace(
            logical_call_id=trusted.logical_call_id,
            pre_provider_sha256=production_runtime_audit_pre_provider_sha256(trusted),
            commit=lambda _detail: pytest.fail("TU must publish a failure terminal"),
            commit_failure=commit_failure,
            abort=lambda: aborts.append(True),
        )

    sink = cast(Any, SimpleNamespace(begin=begin_fault))
    audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)

    with pytest.raises(
        production_audit_module.ProductionRuntimeAuditError,
        match="AUDIT_TERMINAL_COMMIT_FAILED",
    ):
        audit.begin_fallback_pre_provider(
            logical_call_id=binding.logical_call_id,
            host_id="mobileworld.qwen3vl.actor",
            raw_request=raw,
            result=result,
            fallback_check="r2_4_begin_tu_commit_fault_request_proof",
            pre_provider_total_ns=1,
        )

    assert audit.run_fatal_latch.state is not None
    assert audit.latest_failure_receipt is None
    recovery = audit.latest_commit_failure_receipt
    assert recovery is not None
    recovery_projection = (
        production_audit_module.production_runtime_audit_commit_failure_receipt_projection(recovery)
    )
    attempted_terminal = cast(
        dict[str, JsonValue], recovery_projection["attempted_terminal_receipt"]
    )
    assert attempted_terminal["provider_attempt_count"] == 0
    assert attempted_terminal["live_openai_calls"] == 0
    pre = cast(dict[str, JsonValue], recovery_projection["pre_provider"])
    assert canonical_sha256(cast(JsonValue, pre)) == attempted_terminal["pre_provider_sha256"]
    stage = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    persisted_attempts = cast(list[dict[str, JsonValue]], stage["live_attempt_receipts"])
    persisted_proofs = cast(list[JsonValue], stage["r2_4_rubric_request_proofs"])
    assert len(persisted_attempts) == len(persisted_proofs) == 1
    validate_live_rubric_request_proof_projection_v1(
        persisted_proofs[0],
        attempt_receipt=persisted_attempts[0],
        expected_attempt_order=1,
    )
    assert len(attempted_failures) == 1
    assert aborts == [True]
    with pytest.raises(
        production_audit_module.ProductionRuntimeAuditError,
        match="RUN_FATAL_TERMINATION_UNCONFIRMED",
    ):
        audit.bind_actor_sdk_arguments(
            logical_call_id=binding.logical_call_id,
            result=result,
            sdk_arguments=raw,
            collector_request_locator={},
            stream=False,
        )


@pytest.mark.parametrize(
    ("status", "dispatch_count"),
    (
        (LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH, 0),
        (LiveAttemptStatusV1.CANCELLED_POST_DISPATCH, 1),
        (LiveAttemptStatusV1.TERMINATION_UNCONFIRMED, 1),
    ),
)
def test_incomplete_track_attempt_request_proof_covers_cancellation_and_tu(
    tmp_path: Path,
    status: LiveAttemptStatusV1,
    dispatch_count: int,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    termination = (
        LiveAttemptTerminationV1.UNCONFIRMED
        if status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
        else LiveAttemptTerminationV1.KILL
    )
    track = replace(
        attempts[1],
        status=status,
        dispatch_count=dispatch_count,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=(
            LiveAttemptCostStatusV1.EXACT
            if status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
            else LiveAttemptCostStatusV1.UNKNOWN
        ),
        cost_usd_micros=(0 if status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH else None),
        cancellation_requested=True,
        termination=termination,
        worker_exit_code=(None if status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED else -9),
        worker_reaped=status is not LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
        failure_code=(
            "TERMINATION_UNCONFIRMED"
            if status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
            else None
        ),
    )
    incomplete_attempts = (attempts[0], track)
    attempt_anchors = _attempt_request_anchors(incomplete_attempts, anchors)
    packet_sha256 = _anchored_tracking_packet_sha256(anchors)
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=binding.logical_call_id,
        actor_request_sha256=binding.actor_request_sha256,
        attempts=incomplete_attempts,
        rubric_attempt_request_anchors=attempt_anchors,
        rubric_call_receipts=(receipts[0],),
        rubric_call_trust_anchors=(anchors[0],),
        expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
            anchors[0].collector_stimulus
        ),
        expected_tracking_packet_sha256=packet_sha256,
        rubric_backend_extension=extension,
        binding=None,
        actor_call_index=1,
        expect_history_policy=False,
        allow_incomplete=True,
    )
    proofs = production_audit_module._rubric_request_proof_detail_projection(
        attempt_anchors,
        incomplete_attempts,
        extension,
        packet_sha256,
    )
    assert len(proofs) == 2
    assert cast(dict[str, JsonValue], proofs[1])["attempt_status"] == status.value
    assert cast(dict[str, JsonValue], proofs[1])["attempt_dispatch_count"] == dispatch_count
    if status in {
        LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH,
        LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
    }:
        with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
            validate_live_rubric_cross_bindings_v1(
                logical_call_id=binding.logical_call_id,
                actor_request_sha256=binding.actor_request_sha256,
                attempts=incomplete_attempts,
                rubric_attempt_request_anchors=(attempt_anchors[0],),
                rubric_call_receipts=(receipts[0],),
                rubric_call_trust_anchors=(anchors[0],),
                expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                    anchors[0].collector_stimulus
                ),
                expected_tracking_packet_sha256=packet_sha256,
                rubric_backend_extension=extension,
                binding=None,
                actor_call_index=1,
                expect_history_policy=False,
                allow_incomplete=True,
            )
        with pytest.raises(
            production_audit_module.ProductionRuntimeAuditError,
            match="request-proof census differs",
        ):
            production_audit_module._rubric_request_proof_detail_projection(
                (attempt_anchors[0],),
                incomplete_attempts,
                extension,
                packet_sha256,
            )
    if status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED:
        pre_dispatch_tu_attempts = (attempts[0], replace(track, dispatch_count=0))
        with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
            validate_live_rubric_cross_bindings_v1(
                logical_call_id=binding.logical_call_id,
                actor_request_sha256=binding.actor_request_sha256,
                attempts=pre_dispatch_tu_attempts,
                rubric_attempt_request_anchors=(attempt_anchors[0],),
                rubric_call_receipts=(receipts[0],),
                rubric_call_trust_anchors=(anchors[0],),
                expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                    anchors[0].collector_stimulus
                ),
                expected_tracking_packet_sha256=packet_sha256,
                rubric_backend_extension=extension,
                binding=None,
                actor_call_index=1,
                expect_history_policy=False,
                allow_incomplete=True,
            )


def test_begin_failure_before_callable_is_the_only_unanchored_attempt_boundary(
    tmp_path: Path,
) -> None:
    extension, attempts, _, anchors, binding = _complete_live_rubric_cross_binding_proof(tmp_path)
    begin_failure = replace(
        attempts[0],
        status=LiveAttemptStatusV1.FAILED,
        dispatch_count=0,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.EXACT,
        cost_usd_micros=0,
        cancellation_requested=False,
        termination=LiveAttemptTerminationV1.NONE,
        worker_pid=None,
        worker_exit_code=None,
        worker_reaped=False,
        failure_code="ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY",
    )
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=binding.logical_call_id,
        actor_request_sha256=binding.actor_request_sha256,
        attempts=(begin_failure,),
        rubric_attempt_request_anchors=(),
        rubric_call_receipts=(),
        rubric_call_trust_anchors=(),
        expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
            anchors[0].collector_stimulus
        ),
        expected_tracking_packet_sha256=None,
        rubric_backend_extension=extension,
        binding=None,
        actor_call_index=1,
        expect_history_policy=False,
        allow_incomplete=True,
    )
    assert (
        production_audit_module._rubric_request_proof_detail_projection(
            (),
            (begin_failure,),
            extension,
            None,
        )
        == []
    )


def test_failed_generate_attempt_request_hash_rewrite_is_rejected(tmp_path: Path) -> None:
    extension, attempts, _, anchors, binding = _complete_live_rubric_cross_binding_proof(tmp_path)
    failed_generate = replace(
        attempts[0],
        status=LiveAttemptStatusV1.FAILED,
        response_envelope_sha256=None,
        requested_model=None,
        returned_model=None,
        input_tokens=None,
        cached_input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
        cost_usd_micros=None,
        failure_code="INJECTED_GENERATE_PROVIDER_FAILURE",
    )
    attempt_anchors = _attempt_request_anchors((failed_generate,), (anchors[0],))
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=binding.logical_call_id,
        actor_request_sha256=binding.actor_request_sha256,
        attempts=(failed_generate,),
        rubric_attempt_request_anchors=attempt_anchors,
        rubric_call_receipts=(),
        rubric_call_trust_anchors=(),
        expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
            anchors[0].collector_stimulus
        ),
        expected_tracking_packet_sha256=None,
        rubric_backend_extension=extension,
        binding=None,
        actor_call_index=1,
        expect_history_policy=False,
        allow_incomplete=True,
    )
    tampered = replace(
        failed_generate,
        request_sha256=_sha("tampered-failed-generate-request"),
    )
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=(tampered,),
            rubric_attempt_request_anchors=attempt_anchors,
            rubric_call_receipts=(),
            rubric_call_trust_anchors=(),
            expected_collector_stimulus_sha256=rubric_evidence_snapshot_sha256(
                anchors[0].collector_stimulus
            ),
            expected_tracking_packet_sha256=None,
            rubric_backend_extension=extension,
            binding=None,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=True,
        )


@pytest.mark.parametrize("proof_path", ("success", "no_history", "generic_fallback"))
@pytest.mark.parametrize(
    "tampered_anchor_field",
    ("prompt_sha256", "current_image_binding_sha256", "provider_output_sha256"),
)
def test_live_rubric_trust_anchors_reject_rehashed_receipt_graphs(
    proof_path: str,
    tampered_anchor_field: str,
    tmp_path: Path,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    collector_stimulus_sha256 = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    tracking_packet_root = _anchored_tracking_packet_sha256(anchors)
    expect_history_policy = proof_path == "success"
    allow_incomplete = proof_path == "generic_fallback"
    if expect_history_policy:
        history_attempt = replace(
            attempts[-1],
            attempt_id="r24-cross-binding-history-attempt",
            role=LiveAttemptRoleV1.HISTORY_POLICY,
            authority_sha256=_sha("cross-binding-history-authority"),
            request_sha256=_sha("cross-binding-history-request"),
            transport_binding_sha256=_sha("cross-binding-history-transport"),
            response_envelope_sha256=_sha("cross-binding-history-envelope"),
            cost_usd_micros=4,
        )
        attempts = (*attempts, history_attempt)
        binding = replace(
            binding,
            source_transport_binding_sha256=history_attempt.transport_binding_sha256,
            history_policy_attempt_receipt_sha256=live_attempt_receipt_sha256(history_attempt),
            output_sha256=_sha("cross-binding-live-output"),
            openai_calls=3,
            cost_usd_micros=7,
        )
    selected_binding = None if allow_incomplete else binding
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=binding.logical_call_id,
        actor_request_sha256=binding.actor_request_sha256,
        attempts=attempts,
        rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
        rubric_call_receipts=receipts,
        rubric_call_trust_anchors=anchors,
        expected_collector_stimulus_sha256=collector_stimulus_sha256,
        expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
        rubric_backend_extension=extension,
        binding=selected_binding,
        actor_call_index=1,
        expect_history_policy=expect_history_policy,
        allow_incomplete=allow_incomplete,
    )

    values = list(receipts)
    index = 1 if tampered_anchor_field == "current_image_binding_sha256" else 0
    values[index] = replace(
        values[index],
        **{tampered_anchor_field: _sha(f"tampered:{proof_path}:{tampered_anchor_field}")},
    )
    receipts = tuple(values)
    if selected_binding is not None:
        selected_binding = replace(
            selected_binding,
            rubric_call_receipt_sha256s=tuple(
                live_rubric_call_receipt_sha256(item) for item in receipts
            ),
        )

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=collector_stimulus_sha256,
            expected_tracking_packet_sha256=tracking_packet_root,
            rubric_backend_extension=extension,
            binding=selected_binding,
            actor_call_index=1,
            expect_history_policy=expect_history_policy,
            allow_incomplete=allow_incomplete,
        )


def test_live_rubric_fixed_prompt_rejects_rehashed_extension_graph(tmp_path: Path) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    collector_stimulus_sha256 = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    extension = replace(extension, prompt_sha256=_sha("tampered prompt bundle"))
    receipts = tuple(
        replace(item, backend_extension_descriptor_sha256=extension.sha256) for item in receipts
    )
    binding = replace(
        binding,
        rubric_backend_extension_descriptor_sha256=extension.sha256,
        rubric_call_receipt_sha256s=tuple(
            live_rubric_call_receipt_sha256(item) for item in receipts
        ),
    )
    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=collector_stimulus_sha256,
            expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
            rubric_backend_extension=extension,
            binding=binding,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=False,
        )


def test_live_rubric_collector_root_rejects_rehashed_self_consistent_context(
    tmp_path: Path,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path / "authority"
    )
    collector_stimulus_sha256 = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    _, _, _, alternate_anchors, _ = _complete_live_rubric_cross_binding_proof(
        tmp_path / "alternate"
    )
    alternate_task_run_id = alternate_anchors[0].task_run_id
    alternate_image = alternate_anchors[1].current_image
    assert alternate_image is not None
    receipts = tuple(
        replace(
            item,
            task_run_id=alternate_task_run_id,
            current_image_binding_sha256=(
                None
                if item.operation is LiveRubricOperationV1.GENERATE
                else alternate_image.binding_sha256
            ),
        )
        for item in receipts
    )
    binding = replace(
        binding,
        rubric_call_receipt_sha256s=tuple(
            live_rubric_call_receipt_sha256(item) for item in receipts
        ),
    )

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(attempts, alternate_anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=alternate_anchors,
            expected_collector_stimulus_sha256=collector_stimulus_sha256,
            expected_tracking_packet_sha256=_anchored_tracking_packet_sha256(anchors),
            rubric_backend_extension=extension,
            binding=binding,
            actor_call_index=1,
            expect_history_policy=False,
            allow_incomplete=False,
        )


@pytest.mark.parametrize(
    "drift",
    (
        "receipt_order",
        "missing_first_receipt",
        "history_before_rubric",
        "unmatched_rubric_followed_by_history",
        "partial_with_completed_binding",
    ),
)
def test_live_rubric_cross_binding_rejects_nonprefix_sequences(
    drift: str,
    tmp_path: Path,
) -> None:
    extension, attempts, receipts, anchors, binding = _complete_live_rubric_cross_binding_proof(
        tmp_path
    )
    collector_stimulus_sha256 = rubric_evidence_snapshot_sha256(anchors[0].collector_stimulus)
    tracking_packet_root = _anchored_tracking_packet_sha256(anchors)
    history_attempt = replace(
        attempts[-1],
        attempt_id="r24-cross-binding-history-attempt",
        role=LiveAttemptRoleV1.HISTORY_POLICY,
        authority_sha256=_sha("cross-binding-history-authority"),
        request_sha256=_sha("cross-binding-history-request"),
        transport_binding_sha256=_sha("cross-binding-history-transport"),
        response_envelope_sha256=_sha("cross-binding-history-envelope"),
    )
    if drift == "receipt_order":
        receipts = tuple(reversed(receipts))
        anchors = tuple(reversed(anchors))
        selected_binding = None
        selected_attempts = attempts
        expect_history = False
    elif drift == "missing_first_receipt":
        receipts = (receipts[1],)
        anchors = (anchors[1],)
        selected_binding = None
        selected_attempts = attempts
        expect_history = False
    elif drift == "history_before_rubric":
        receipts = ()
        anchors = ()
        selected_binding = None
        selected_attempts = (history_attempt, attempts[0])
        expect_history = True
    elif drift == "unmatched_rubric_followed_by_history":
        receipts = (receipts[0],)
        anchors = (anchors[0],)
        selected_binding = None
        selected_attempts = (*attempts, history_attempt)
        expect_history = True
    elif drift == "partial_with_completed_binding":
        receipts = (receipts[0],)
        anchors = (anchors[0],)
        selected_binding = binding
        selected_attempts = attempts
        expect_history = False
    else:
        raise AssertionError(f"unknown drift: {drift}")

    with pytest.raises(R24ContractError, match="RUBRIC_CROSS_BINDING_MISMATCH"):
        validate_live_rubric_cross_bindings_v1(
            logical_call_id=binding.logical_call_id,
            actor_request_sha256=binding.actor_request_sha256,
            attempts=selected_attempts,
            rubric_attempt_request_anchors=_attempt_request_anchors(selected_attempts, anchors),
            rubric_call_receipts=receipts,
            rubric_call_trust_anchors=anchors,
            expected_collector_stimulus_sha256=collector_stimulus_sha256,
            expected_tracking_packet_sha256=tracking_packet_root,
            rubric_backend_extension=extension,
            binding=selected_binding,
            actor_call_index=1,
            expect_history_policy=expect_history,
            allow_incomplete=True,
        )


@dataclass(slots=True)
class _RuntimeCase:
    run: RunRecorder
    task: TaskRecorder
    capture: RunnerTaskCapture
    request: dict[str, JsonValue]
    history_ir: HistoryIR
    context: SentinelContext
    audit_context: AuditContext


def _request_image(request: dict[str, JsonValue]) -> Image.Image:
    urls: list[str] = []

    def visit(value: JsonValue) -> None:
        if type(value) is dict:
            image_url = value.get("image_url")
            if value.get("type") == "image_url" and type(image_url) is dict:
                url = image_url.get("url")
                if type(url) is str:
                    urls.append(url)
            for child in value.values():
                visit(child)
        elif type(value) is list:
            for child in value:
                visit(child)

    visit(cast(JsonValue, request))
    assert urls
    raw = base64.b64decode(urls[-1].split(",", 1)[1], validate=True)
    with Image.open(io.BytesIO(raw)) as opened:
        opened.load()
        return cast(Image.Image, opened.copy())


def _runtime_case(tmp_path: Path) -> _RuntimeCase:
    fixture = cast(dict[str, Any], json.loads(QWEN_FIXTURE.read_text(encoding="utf-8")))
    request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
    resolver = build_runtime_history_codec_resolver()
    structural = resolver.by_id("mobileworld.g1.history-codec.qwen-flat-progress")
    history_ir = structural.extract(cast(JsonValue, request))
    run = RunRecorder(
        tmp_path,
        producer=Producer.local(version="r2.4-test", worker_id="orchestration"),
        sync=False,
    )
    run.write_manifest_start({"run_id": run.run_id})
    task = run.open_task()
    capture = RunnerTaskCapture(task)
    started = capture.start_task(
        task_name="R24Orchestration",
        task_goal="调整显示亮度。",
        task_goal_status="resolved",
        task_index=1,
        suite_family="mobile_world",
        agent={"adapter": "qwen", "model": "fixture", "configuration": {}},
        environment={"backend_id": "cpu-fixture", "device_id": "none"},
        whole_task_attempt_index=1,
    )
    assert started is not None
    current = capture.start_step(
        step_index=1,
        observation={
            "screenshot": _request_image(request),
            "accessibility_tree": {"screen": "display", "slider": "brightness"},
            "tool_call": None,
            "ask_user_response": None,
        },
    )
    assert current is not None
    context = SentinelContext(
        logical_call_id="r24-orchestration-call-1",
        host_id=history_ir.host_id,
    )
    return _RuntimeCase(
        run=run,
        task=task,
        capture=capture,
        request=request,
        history_ir=history_ir,
        context=context,
        audit_context=AuditContext(
            run_id=run.run_id,
            recorder=task,
            task_run_id=task.task_run_id,
            step_id=current.step_id,
            decision_id=current.decision_id,
            parent_event_id=current.step_started_event_id,
        ),
    )


def _no_history_request(runtime: _RuntimeCase) -> dict[str, JsonValue]:
    request = cast(dict[str, JsonValue], deepcopy(runtime.request))
    messages = cast(list[JsonValue], request["messages"])
    content = cast(list[JsonValue], cast(dict[str, JsonValue], messages[1])["content"])
    text_block = cast(dict[str, JsonValue], content[0])
    text = cast(str, text_block["text"])
    text_block["text"] = text[: text.index("Step 1: ")] + "\n"
    return request


def _descriptor() -> RubricBackendDescriptorV1:
    return RubricBackendDescriptorV1(
        backend_id="r24-fake-rubric",
        backend_version="v1",
        prompt_sha256=_sha("r24 rubric prompt"),
        rubric_schema_sha256=_sha("r24 rubric schema"),
        tracking_packet_schema_sha256=_sha("r24 tracking schema"),
        tracker_schema_sha256=_sha("r24 tracker schema"),
        config_sha256=_sha("r24 offline fake config"),
    )


def _rubric(task_run_id: str, task: TaskInstructionV1) -> MultiPathRubricV1:
    span = InstructionSpanV1(
        span_id="r24-task-span",
        role=InstructionSpanRole.HARD_REQUIREMENT,
        char_start=0,
        char_end=len(task.exact_text),
        utf8_byte_start=0,
        utf8_byte_end=len(task.exact_text.encode("utf-8")),
        exact_text=task.exact_text,
        span_sha256=task.text_sha256,
    )
    milestone = MilestoneV1(
        milestone_id="r24-task-milestone",
        kind=MilestoneKind.HARD_REQUIREMENT,
        predicate_kind=MilestonePredicateKind.INSTRUCTION_REQUIREMENT,
        state_description=task.exact_text,
        description_sha256=task.text_sha256,
        instruction_span_id=span.span_id,
    )
    return MultiPathRubricV1(
        rubric_id="r24-rubric",
        task_run_id=task_run_id,
        rubric_version=1,
        task=task,
        revision=RubricRevisionV1(
            revision_id="r24-initial-revision",
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
                path_id="r24-primary-path",
                kind=PathKind.LEGAL_ALTERNATIVE,
                root=GraphRefV1(
                    ref_kind=GraphRefKind.MILESTONE,
                    ref_id=milestone.milestone_id,
                ),
            ),
            RubricPathV1(
                path_id="r24-other-unknown",
                kind=PathKind.OTHER_UNKNOWN,
                root=None,
            ),
        ),
        backend=_descriptor(),
    )


class _Builder:
    def __init__(self, rubric: MultiPathRubricV1, *, fail: bool = False) -> None:
        self.rubric = rubric
        self.fail = fail
        self.generate_calls = 0

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        return self.rubric.backend

    def generate(self, request: TaskStartRubricRequestV1) -> MultiPathRubricV1:
        self.generate_calls += 1
        assert request.task == self.rubric.task
        if self.fail:
            raise RuntimeError("injected rubric generation failure")
        return self.rubric

    def revise(self, request: RubricRevisionRequestV1) -> MultiPathRubricV1:
        del request
        raise AssertionError("runtime orchestration must not revise the task rubric")


class _Tracker:
    def __init__(self, descriptor: RubricBackendDescriptorV1) -> None:
        self._descriptor = descriptor
        self.track_calls = 0
        self.packets: list[RubricTrackingPacketV1] = []

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        return self._descriptor

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1:
        self.track_calls += 1
        self.packets.append(packet)
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
            proposal_id=f"r24-proposal-{packet.logical_call_id}",
            packet_id=packet.packet_id,
            packet_sha256=tracking_packet_sha256(packet),
            rubric_binding=packet.rubric_binding,
            prior_state_sha256=rubric_tracking_state_sha256(packet.prior_state),
            proposal_status=TrackerProposalStatus.ABSTAIN,
            milestone_states=states,
        )


@dataclass(slots=True)
class _SessionHarness:
    sessions: list[RubricTaskSession]
    builders: list[_Builder]
    trackers: list[_Tracker]
    fail_generation: bool = False

    def __call__(self, task_run_id: str, task: TaskInstructionV1) -> RubricTaskSession:
        rubric = _rubric(task_run_id, task)
        builder = _Builder(rubric, fail=self.fail_generation)
        tracker = _Tracker(rubric.backend)
        session = RubricTaskSession(
            task_run_id=task_run_id,
            task=task,
            builder_backend=builder,
            tracker_backend=tracker,
        )
        self.sessions.append(session)
        self.builders.append(builder)
        self.trackers.append(tracker)
        return session


def _harness(*, fail_generation: bool = False) -> _SessionHarness:
    return _SessionHarness([], [], [], fail_generation=fail_generation)


def test_coordinator_runs_one_read_rubric_first_and_reuses_per_call_cache(
    tmp_path: Path,
) -> None:
    runtime = _runtime_case(tmp_path)
    sessions = _harness()
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    try:
        with bind_audit_context(runtime.audit_context):
            first = coordinator(
                cast(JsonValue, runtime.request), runtime.context, runtime.history_ir
            )
            cached = coordinator(
                cast(JsonValue, runtime.request), runtime.context, runtime.history_ir
            )
        next_step = runtime.capture.start_step(
            step_index=2,
            observation={
                "screenshot": _request_image(runtime.request),
                "accessibility_tree": {"screen": "display", "slider": "brightness"},
                "tool_call": None,
                "ask_user_response": None,
            },
        )
        assert next_step is not None
        second_context = SentinelContext(
            logical_call_id="r24-orchestration-call-2",
            host_id=runtime.context.host_id,
        )
        second_audit_context = AuditContext(
            run_id=runtime.run.run_id,
            recorder=runtime.task,
            task_run_id=runtime.task.task_run_id,
            step_id=next_step.step_id,
            decision_id=next_step.decision_id,
            parent_event_id=next_step.step_started_event_id,
        )
        with bind_audit_context(second_audit_context):
            second = coordinator(
                cast(JsonValue, runtime.request), second_context, runtime.history_ir
            )
    finally:
        runtime.run.close()

    assert first == cached
    assert first is not cached
    assert first.packet is not cached.packet
    assert second.packet.logical_call_id == "r24-orchestration-call-2"
    assert coordinator.collector_bundle_calls == 2
    assert coordinator.logical_call_count == 2
    assert coordinator.task_session_count == 1
    assert len(sessions.sessions) == 1
    assert sessions.builders[0].generate_calls == 1
    assert sessions.trackers[0].track_calls == 2
    assert sessions.sessions[0].task_start_generation_calls == 1
    assert sessions.sessions[0].runtime_tracking_calls == 2
    assert sessions.sessions[0].relevance_link_calls == 2

    record = coordinator.record_for(runtime.context.logical_call_id)
    assert record is not None
    assert record.rubric_result.status is RubricSessionStatus.ADMITTED
    assert record.rubric_result.stage is RubricSessionStage.LINK_RELEVANCE
    assert record.rubric_result.relevance is not None
    assert len(record.rubric_result.relevance.records) == len(runtime.history_ir.records)
    assert all(
        item.disposition is RelevanceDisposition.RETAIN
        and item.supported_record_binding_sha256 is None
        for item in record.rubric_result.relevance.records
    )
    assert record.topology_run.status is TopologyRunStatus.ADMITTED
    assert record.topology_run.topology.kind is TopologyKind.ISOLATED_HISTORY_FREE
    assert record.topology_run.rubric_input_sha256 == (record.history_free_stimulus_sha256)
    assert record.topology_run.history_policy_input_sha256 is None
    assert record.topology_run.history_policy_output_sha256 is None
    assert record.gpt56_evidence_packet_sha256 == first.packet_sha256
    assert len(r24_coordinated_call_record_sha256(record)) == 64

    packet_projection = json.dumps(
        tracking_packet_projection(sessions.trackers[0].packets[0]),
        ensure_ascii=False,
        sort_keys=True,
    )
    fixture_text = QWEN_FIXTURE.read_text(encoding="utf-8")
    assert '"model"' not in packet_projection
    assert "raw_request_sha256" not in packet_projection
    assert "HistoryIR" not in packet_projection
    assert "已打开设置🙂" in fixture_text
    assert "已打开设置🙂" not in packet_projection


def test_no_history_first_call_generates_and_tracks_then_history_call_only_tracks(
    tmp_path: Path,
) -> None:
    runtime = _runtime_case(tmp_path)
    sessions = _harness()
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    no_history = _no_history_request(runtime)
    try:
        with bind_audit_context(runtime.audit_context):
            first = coordinator.prepare_no_history(cast(JsonValue, no_history), runtime.context)
            cached = coordinator.prepare_no_history(cast(JsonValue, no_history), runtime.context)
        next_step = runtime.capture.start_step(
            step_index=2,
            observation={
                "screenshot": _request_image(runtime.request),
                "accessibility_tree": {"screen": "display", "slider": "brightness"},
                "tool_call": None,
                "ask_user_response": None,
            },
        )
        assert next_step is not None
        second_context = SentinelContext(
            logical_call_id="r24-orchestration-call-2",
            host_id=runtime.context.host_id,
        )
        second_audit_context = AuditContext(
            run_id=runtime.run.run_id,
            recorder=runtime.task,
            task_run_id=runtime.task.task_run_id,
            step_id=next_step.step_id,
            decision_id=next_step.decision_id,
            parent_event_id=next_step.step_started_event_id,
        )
        with bind_audit_context(second_audit_context):
            second = coordinator(
                cast(JsonValue, runtime.request), second_context, runtime.history_ir
            )
    finally:
        runtime.run.close()

    assert first == cached
    assert first is not cached
    assert first.gpt56_evidence_packet_sha256 is None
    assert first.rubric_result.relevance is not None
    assert first.rubric_result.relevance.records == ()
    assert second.packet.logical_call_id == second_context.logical_call_id
    assert coordinator.collector_bundle_calls == 2
    assert coordinator.logical_call_count == 2
    assert coordinator.task_session_count == 1
    assert sessions.builders[0].generate_calls == 1
    assert sessions.trackers[0].track_calls == 2
    assert sessions.sessions[0].task_start_generation_calls == 1
    assert sessions.sessions[0].runtime_tracking_calls == 2
    assert sessions.sessions[0].relevance_link_calls == 2


def test_no_history_terminal_audit_persists_rubric_preimages_and_retry_stability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime_case(tmp_path)
    sessions = _harness()
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    no_history = _no_history_request(runtime)
    raw_hash = canonical_sha256(cast(JsonValue, no_history))
    try:
        with bind_audit_context(runtime.audit_context):
            coordinated = coordinator.prepare_no_history(
                cast(JsonValue, no_history), runtime.context
            )
            rubric_only = CollectorEvidenceFactoryV1().rubric_only_bundle_for_no_history_call(
                request=cast(JsonValue, no_history),
                context=runtime.context,
            )

        current_image = bind_current_collector_image_projection(
            stimulus=rubric_only.r23_snapshot,
            current_image_data_url=rubric_only.current_image_data_url,
            current_image_sha256=rubric_only.current_image_sha256,
            logical_call_id=runtime.context.logical_call_id,
        )
        operations = (LiveRubricOperationV1.GENERATE, LiveRubricOperationV1.TRACK)
        (
            rubric_backend_extension,
            provider_inputs,
            provider_requests,
        ) = _live_rubric_request_material(
            stimulus=rubric_only.r23_snapshot,
            current_image=current_image,
            logical_call_id=runtime.context.logical_call_id,
            packet=sessions.trackers[0].packets[0],
            descriptor_id="r24-no-history-rubric-extension",
        )
        envelopes = tuple(
            ResponsesEnvelopeV1(
                response_id=f"no-history-rubric-response-{index}",
                requested_model=LIVE_RUBRIC_MODEL,
                returned_model=LIVE_RUBRIC_MODEL,
                status="completed",
                service_tier="default",
                output_text=json.dumps({"operation": operation.value, "index": index}),
                input_tokens=2,
                output_tokens=1,
                total_tokens=3,
            )
            for index, operation in enumerate(operations, start=1)
        )

        common = {
            "manifest_sha256": _sha("manifest"),
            "preflight_sha256": _sha("preflight"),
            "case_execution_lease_sha256": _sha("lease"),
            "stage_sha256": _sha("rubric-stage"),
            "case_id": "no-history-smoke",
            "logical_call_id": runtime.context.logical_call_id,
            "actor_request_sha256": raw_hash,
            "transport_binding_sha256": _sha("rubric-transport"),
            "pricing_binding_sha256": _sha("pricing"),
        }
        attempts = tuple(
            LiveAttemptReceiptV1(
                attempt_id=f"no-history-rubric-{index}",
                role=LiveAttemptRoleV1.RUBRIC,
                authority_sha256=_sha(f"rubric-authority-{index}"),
                request_sha256=provider_request.request_sha256,
                execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                status=LiveAttemptStatusV1.COMPLETED,
                dispatch_count=1,
                response_envelope_sha256=envelope.sha256,
                requested_model=LIVE_RUBRIC_MODEL,
                returned_model=LIVE_RUBRIC_MODEL,
                input_tokens=2,
                cached_input_tokens=0,
                output_tokens=1,
                total_tokens=3,
                cost_status=LiveAttemptCostStatusV1.EXACT,
                cost_usd_micros=1,
                cancellation_requested=False,
                termination=LiveAttemptTerminationV1.NONE,
                worker_pid=10_000 + index,
                worker_exit_code=0,
                worker_reaped=True,
                late_output_detected=False,
                duration_ns=index,
                failure_code=None,
                **common,
            )
            for index, (envelope, provider_request) in enumerate(
                zip(envelopes, provider_requests, strict=True), start=1
            )
        )
        attempt_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
        rubric_call_receipts = tuple(
            LiveRubricCallReceiptV1(
                receipt_id=f"r24-rubric-call-{index}",
                operation=(
                    LiveRubricOperationV1.GENERATE if index == 1 else LiveRubricOperationV1.TRACK
                ),
                execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
                task_run_id=runtime.task.task_run_id,
                logical_call_id=runtime.context.logical_call_id,
                backend_extension_descriptor_sha256=rubric_backend_extension.sha256,
                r23_compatibility_descriptor_sha256=(
                    rubric_backend_extension.r23_compatibility_descriptor_sha256
                ),
                transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
                transport_authority=(LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION),
                prompt_sha256=live_rubric_operation_prompt_sha256(operation),
                provider_input_schema_version=(
                    LIVE_RUBRIC_GENERATE_INPUT_SCHEMA_VERSION
                    if index == 1
                    else LIVE_RUBRIC_TRACK_INPUT_SCHEMA_VERSION
                ),
                provider_output_schema_sha256=(
                    rubric_backend_extension.generate_output_schema_sha256
                    if index == 1
                    else rubric_backend_extension.track_output_schema_sha256
                ),
                provider_request_sha256=attempt.request_sha256,
                provider_output_sha256=envelope.output_text_sha256,
                transport_binding_sha256=attempt.transport_binding_sha256,
                pricing_binding_sha256=attempt.pricing_binding_sha256,
                current_image_binding_sha256=(
                    None
                    if operation is LiveRubricOperationV1.GENERATE
                    else current_image.binding_sha256
                ),
                manifest_sha256=attempt.manifest_sha256,
                preflight_sha256=attempt.preflight_sha256,
                case_execution_lease_sha256=attempt.case_execution_lease_sha256,
                stage_sha256=attempt.stage_sha256,
                attempt_authority_sha256=attempt.authority_sha256,
                attempt_receipt_sha256=attempt_hash,
                requested_model=LIVE_RUBRIC_MODEL,
                returned_model=LIVE_RUBRIC_MODEL,
                dispatch_count=1,
                input_tokens=cast(int, attempt.input_tokens),
                output_tokens=cast(int, attempt.output_tokens),
                total_tokens=cast(int, attempt.total_tokens),
                cost_usd_micros=cast(int, attempt.cost_usd_micros),
            )
            for index, (operation, attempt, attempt_hash, envelope) in enumerate(
                zip(operations, attempts, attempt_hashes, envelopes, strict=True), start=1
            )
        )
        rubric_call_trust_anchors = tuple(
            rubric_live_module._build_live_rubric_call_trust_anchor(
                operation=operation,
                task_run_id=runtime.task.task_run_id,
                logical_call_id=runtime.context.logical_call_id,
                collector_stimulus=rubric_only.r23_snapshot,
                current_image=(
                    None if operation is LiveRubricOperationV1.GENERATE else current_image
                ),
                provider_input=provider_input,
                provider_request=provider_request,
                response_envelope=envelope,
            )
            for operation, envelope, provider_input, provider_request in zip(
                operations,
                envelopes,
                provider_inputs,
                provider_requests,
                strict=True,
            )
        )
        rubric_attempt_request_anchors = _attempt_request_anchors(
            attempts, rubric_call_trust_anchors
        )
        rubric_call_hashes = tuple(
            live_rubric_call_receipt_sha256(item) for item in rubric_call_receipts
        )
        policy_id = "r24-no-history-audit-live-policy"
        binding = ResolvedLivePolicyCallBindingV1(
            logical_call_id=runtime.context.logical_call_id,
            actor_call_index=1,
            actor_request_sha256=raw_hash,
            policy_id=policy_id,
            execution_authority_sha256=cast(str, common["manifest_sha256"]),
            source_transport_descriptor_sha256=_sha("history-descriptor"),
            source_transport_binding_sha256=None,
            case_execution_lease_sha256=cast(str, common["case_execution_lease_sha256"]),
            preflight_report_sha256=cast(str, common["preflight_sha256"]),
            factory_binding_sha256=_sha("factory-binding"),
            pricing_binding_sha256=cast(str, common["pricing_binding_sha256"]),
            rubric_backend_extension_descriptor_sha256=(rubric_backend_extension.sha256),
            rubric_attempt_receipt_sha256s=attempt_hashes,
            rubric_call_receipt_sha256s=rubric_call_hashes,
            history_policy_attempt_receipt_sha256=None,
            output_sha256=None,
            openai_calls=2,
            cost_usd_micros=2,
        )
        policy = object.__new__(OwnerAuthorizedLivePerCallPolicyV1)
        policy._policy_id = policy_id
        prepare_calls: list[str] = []

        def prepare_no_history(
            _self: OwnerAuthorizedLivePerCallPolicyV1,
            *,
            request: JsonValue,
            context: SentinelContext,
            execution_control: object,
        ) -> object:
            del execution_control
            assert canonical_sha256(request) == raw_hash
            prepare_calls.append(context.logical_call_id)
            return coordinated

        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "prepare_no_history_with_control",
            prepare_no_history,
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "coordinated_record_for_call",
            lambda _self, logical_call_id: (
                coordinated
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "call_binding",
            lambda _self, logical_call_id: (
                binding
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "attempt_receipts_for_call",
            lambda _self, logical_call_id: (
                attempts
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "rubric_call_receipts_for_call",
            lambda _self, logical_call_id: (
                rubric_call_receipts
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "rubric_call_trust_anchors_for_call",
            lambda _self, logical_call_id: (
                rubric_call_trust_anchors
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "rubric_attempt_request_anchors_for_call",
            lambda _self, logical_call_id: (
                rubric_attempt_request_anchors
                if logical_call_id == runtime.context.logical_call_id
                else pytest.fail("unexpected logical call")
            ),
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "rubric_backend_extension_descriptor",
            lambda _self: rubric_backend_extension,
        )
        monkeypatch.setattr(
            OwnerAuthorizedLivePerCallPolicyV1,
            "failure_for_call",
            lambda _self, _logical_call_id: None,
        )

        sink = MemoryProductionRuntimeAuditSinkV1()
        audit = ProductionRuntimeAuditV1(policy=policy, sink=sink)
        sentinel = PromptSentinel(
            policy=policy,
            codec_registry=build_runtime_history_codec_resolver(),
            host_configs={
                runtime.context.host_id: SentinelHostConfig(
                    mode=SentinelMode.ACTIVE,
                    policy_timeout_ms=1_000,
                )
            },
            receipt_sink=MemorySentinelReceiptSink(),
            runtime_audit=audit,
            global_switch=SentinelGlobalSwitch(),
            logical_call_id_factory=lambda: runtime.context.logical_call_id,
        )
        logical_call = sentinel.logical_call(
            host_id=runtime.context.host_id,
            history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
        )
        first = logical_call.before_model_call(cast(JsonValue, no_history))
        second = logical_call.before_model_call(cast(JsonValue, no_history))
        assert (
            canonical_json_bytes(first.final_request)
            == canonical_json_bytes(second.final_request)
            == canonical_json_bytes(cast(JsonValue, no_history))
        )
        assert prepare_calls == [runtime.context.logical_call_id]

        locator_base: dict[str, JsonValue] = {
            "run_id": runtime.run.run_id,
            "task_run_id": runtime.task.task_run_id,
            "snapshot_blob": {"sha256": _sha("actor-blob")},
        }
        audit.bind_actor_sdk_arguments(
            logical_call_id=runtime.context.logical_call_id,
            result=first,
            sdk_arguments=cast(JsonValue, no_history),
            collector_request_locator={
                **locator_base,
                "event_type": "model_request",
                "event_id": "actor-request-event",
                "event_sha256": _sha("actor-request-event"),
            },
            stream=False,
        )
        audit.record_actor_provider_attempt(
            logical_call_id=runtime.context.logical_call_id,
            succeeded=True,
            latency_ns=1,
            collector_terminal_locator={
                **locator_base,
                "event_type": "model_response",
                "event_id": "actor-response-event",
                "event_sha256": _sha("actor-response-event"),
            },
            raw_response={"output": "wait"},
            response_id="actor-response",
            model_id="cpu-fake-actor",
            finish_reason="stop",
            input_tokens=2,
            output_tokens=1,
            total_tokens=3,
        )
        action: JsonValue = {"action_type": "wait"}
        audit.finalize_actor_output(
            logical_call_id=runtime.context.logical_call_id,
            raw_provider_response="wait",
            raw_parser_input="wait",
            parsed_action=action,
            parser_id="no-history-test-parser",
            parser_status=ParserResultStatusV1.PARSED,
            parser_attempt_count=1,
            parser_ns=1,
        )
        receipt = audit.finalize_action_execution(
            logical_call_id=runtime.context.logical_call_id,
            parsed_action=action,
            action_executed=False,
        )
    finally:
        runtime.run.close()

    assert receipt.live_openai_calls == 2
    assert (
        receipt.pre_provider_outcome
        is ProductionRuntimeAuditPreProviderOutcomeV1.NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL
    )
    assert receipt.fallback_reason is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
    assert receipt.fallback_check == "r2_4_no_history_r21_v1_compatibility"
    assert len(sink.details) == 1
    detail = sink.details[0]
    assert detail.pre_provider.status is ProductionRuntimeAuditPreProviderStatusV1.FALLBACK_ORIGINAL
    assert detail.pre_provider.coordinated_record_sha256 == (
        r24_coordinated_call_record_sha256(coordinated)
    )
    assert detail.pre_provider.rubric_result_sha256 == rubric_session_result_sha256(
        coordinated.rubric_result
    )
    assert coordinated.rubric_result.relevance is not None
    assert detail.pre_provider.path_relevance_output_sha256 == path_relevance_output_sha256(
        coordinated.rubric_result.relevance
    )
    assert detail.pre_provider.live_attempt_receipt_sha256s == attempt_hashes
    assert detail.pre_provider_sha256 == production_runtime_audit_pre_provider_sha256(
        detail.pre_provider
    )
    projection = production_runtime_audit_detail_projection(detail)
    pre = cast(dict[str, JsonValue], projection["pre_provider"])
    stage = cast(dict[str, JsonValue], pre["restricted_stage_projection"])
    assert pre["content_persistence"]["rubric_output"] is True
    assert stage["kind"] == "NO_HISTORY_RUBRIC_FALLBACK_ORIGINAL"
    assert stage["coordinated_record"] is not None
    assert stage["rubric_generation_result"] is not None
    assert stage["rubric_result"] is not None
    assert stage["path_relevance_output"] is not None
    assert [item["role"] for item in stage["live_attempt_receipts"]] == [
        "RUBRIC",
        "RUBRIC",
    ]
    assert [item["requested_model"] for item in stage["r2_4_rubric_call_receipts"]] == [
        LIVE_RUBRIC_MODEL,
        LIVE_RUBRIC_MODEL,
    ]
    assert [item["returned_model"] for item in stage["r2_4_rubric_call_receipts"]] == [
        LIVE_RUBRIC_MODEL,
        LIVE_RUBRIC_MODEL,
    ]
    request_proofs = cast(list[JsonValue], stage["r2_4_rubric_request_proofs"])
    assert len(request_proofs) == 2
    attempt_projections = cast(list[dict[str, JsonValue]], stage["live_attempt_receipts"])
    for index, (request_proof, attempt_projection) in enumerate(
        zip(request_proofs, attempt_projections, strict=True), start=1
    ):
        validate_live_rubric_request_proof_projection_v1(
            request_proof,
            attempt_receipt=attempt_projection,
            expected_attempt_order=index,
        )
    assert cast(dict[str, JsonValue], request_proofs[0])["tracking_packet_sha256"] is None
    assert (
        cast(dict[str, JsonValue], request_proofs[1])["tracking_packet_sha256"]
        == coordinated.tracking_packet_sha256
    )
    assert stage["r2_4_rubric_backend_extension"]["configured_model"] == LIVE_RUBRIC_MODEL


def test_new_logical_call_cannot_reuse_one_collector_observation(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path)
    sessions = _harness()
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    try:
        with bind_audit_context(runtime.audit_context):
            coordinator(cast(JsonValue, runtime.request), runtime.context, runtime.history_ir)
            duplicate = SentinelContext(
                logical_call_id="r24-duplicate-observation-call",
                host_id=runtime.context.host_id,
            )
            with pytest.raises(R24OrchestrationError, match="DUPLICATE_RUBRIC_STIMULUS") as error:
                coordinator(cast(JsonValue, runtime.request), duplicate, runtime.history_ir)
    finally:
        runtime.run.close()

    assert error.value.code == "DUPLICATE_RUBRIC_STIMULUS"
    assert coordinator.collector_bundle_calls == 2
    assert sessions.builders[0].generate_calls == 1
    assert sessions.trackers[0].track_calls == 1


def test_coordinator_returns_detached_records_and_rejects_logical_call_drift(
    tmp_path: Path,
) -> None:
    runtime = _runtime_case(tmp_path)
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=_harness(),
    )
    try:
        with bind_audit_context(runtime.audit_context):
            coordinator(cast(JsonValue, runtime.request), runtime.context, runtime.history_ir)
            first = coordinator.record_for(runtime.context.logical_call_id)
            assert first is not None and first.rubric_result.relevance is not None
            expected_hash = rubric_session_result_sha256(first.rubric_result)
            object.__setattr__(first.rubric_result.relevance, "linkage_id", "caller-mutated")
            fresh = coordinator.record_for(runtime.context.logical_call_id)
            assert fresh is not None
            assert rubric_session_result_sha256(fresh.rubric_result) == expected_hash
            with pytest.raises(R24OrchestrationError, match="RUBRIC_RESULT_HASH_MISMATCH"):
                replace(fresh, generation_result_sha256="0" * 64)

            drifted_context = SentinelContext(
                logical_call_id=runtime.context.logical_call_id,
                host_id=runtime.context.host_id,
                attributes={"drift": True},
            )
            with pytest.raises(R24OrchestrationError, match="LOGICAL_CALL_INPUT_DRIFT") as error:
                coordinator(
                    cast(JsonValue, runtime.request),
                    drifted_context,
                    runtime.history_ir,
                )
    finally:
        runtime.run.close()

    assert error.value.code == "LOGICAL_CALL_INPUT_DRIFT"
    assert coordinator.collector_bundle_calls == 1


class _NeverTransport:
    def __init__(self) -> None:
        self.calls = 0

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
        del request, call_role, timeout_seconds
        self.calls += 1
        raise AssertionError("rubric fallback must prevent GPT transport")


def _unreachable(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("post-transport callback must be unreachable")


def test_rubric_fallback_prevents_gpt_transport_and_is_cached(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path)
    sessions = _harness(fail_generation=True)
    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=sessions,
    )
    transport = _NeverTransport()
    policy = GPT56SentinelPolicy(
        transport=transport,
        evidence_packet_factory=coordinator,
        proposal_admission=_unreachable,
        admission_receipt_projector=_unreachable,
        bind_policy_receipt=_unreachable,
        receipt_sink=MemoryR22PolicyReceiptSink(),
        metrics=R22PolicyMetrics(),
        output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
        timeout_seconds=0.05,
        seam_policy_deadline_seconds=2.0,
    )
    try:
        with bind_audit_context(runtime.audit_context):
            with pytest.raises(GPT56PolicyError, match="EVIDENCE_PACKET_REJECTED"):
                policy.evaluate(
                    request=cast(JsonValue, runtime.request),
                    context=runtime.context,
                    history_ir=runtime.history_ir,
                )
            with pytest.raises(R24OrchestrationError, match="RUBRIC_TASK_START_FALLBACK"):
                coordinator(cast(JsonValue, runtime.request), runtime.context, runtime.history_ir)
    finally:
        runtime.run.close()

    assert transport.calls == 0
    assert coordinator.collector_bundle_calls == 1
    assert sessions.builders[0].generate_calls == 1
    assert sessions.trackers[0].track_calls == 0
    record = coordinator.record_for(runtime.context.logical_call_id)
    assert record is not None
    assert record.rubric_result.status is RubricSessionStatus.FALLBACK
    assert record.rubric_result.stage is RubricSessionStage.TASK_START_GENERATE
    assert record.topology_run.status is TopologyRunStatus.FALLBACK
    assert record.topology_run.failure_code == "RUBRIC_TASK_START_FALLBACK"
    assert record.topology_run.rubric_input_sha256 == (record.history_free_stimulus_sha256)


def test_session_factory_is_untrusted_and_receives_only_task_fields(tmp_path: Path) -> None:
    runtime = _runtime_case(tmp_path)
    observed: list[tuple[str, TaskInstructionV1]] = []

    def invalid_factory(task_run_id: str, task: TaskInstructionV1) -> Any:
        observed.append((task_run_id, task))
        return object()

    coordinator = R24RuntimeCoordinatorV1(
        collector=CollectorEvidenceFactoryV1(),
        session_factory=invalid_factory,
    )
    try:
        with bind_audit_context(runtime.audit_context):
            with pytest.raises(R24OrchestrationError, match="UNTRUSTED_RUBRIC_SESSION") as error:
                coordinator(cast(JsonValue, runtime.request), runtime.context, runtime.history_ir)
    finally:
        runtime.run.close()

    assert error.value.code == "UNTRUSTED_RUBRIC_SESSION"
    assert len(observed) == 1
    task_run_id, task = observed[0]
    assert task_run_id == runtime.task.task_run_id
    assert type(task) is TaskInstructionV1
    assert task.exact_text == "调整显示亮度。"
    assert GPT56_REQUESTED_MODEL not in task.exact_text
    assert coordinator.record_for(runtime.context.logical_call_id) is None
