from __future__ import annotations

import base64
import hashlib
import io
import json
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
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
from mobile_world.runtime.sentinel.contracts import SentinelCallRole, SentinelContext
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
    tracking_packet_projection,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.session import (
    RubricSessionStage,
    RubricSessionStatus,
    RubricTaskSession,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    build_runtime_history_codec_resolver,
)
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError
from mobile_world.runtime.sentinel.r2_4.evidence import CollectorEvidenceFactoryV1
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptExecutionKindV1,
    LiveAttemptReceiptV1,
    LiveAttemptRoleV1,
    LiveAttemptStatusV1,
    LiveAttemptTerminationV1,
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
    LiveRubricCallReceiptV1,
    LiveRubricExecutionScopeV1,
    LiveRubricOperationV1,
    LiveRubricTransportAuthorityV1,
    LiveRubricTransportKindV1,
    R24RubricBackendExtensionDescriptorV1,
    live_rubric_call_receipt_sha256,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
QWEN_FIXTURE = (
    REPO_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
    "qwen_flat_progress.captured.v1.json"
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_live_rubric_cross_binding_proof() -> tuple[
    R24RubricBackendExtensionDescriptorV1,
    tuple[LiveAttemptReceiptV1, ...],
    tuple[LiveRubricCallReceiptV1, ...],
    ResolvedLivePolicyCallBindingV1,
]:
    logical_call_id = "r24-cross-binding-call-1"
    actor_request_sha256 = _sha("cross-binding-actor-request")
    manifest_sha256 = _sha("cross-binding-manifest")
    preflight_sha256 = _sha("cross-binding-preflight")
    lease_sha256 = _sha("cross-binding-lease")
    stage_sha256 = _sha("cross-binding-rubric-stage")
    pricing_sha256 = _sha("cross-binding-pricing")
    extension = R24RubricBackendExtensionDescriptorV1(
        descriptor_id="r24-cross-binding-extension",
        descriptor_version="v1",
        execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
        transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
        transport_authority=(LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION),
        r23_compatibility_descriptor_sha256=_sha("cross-binding-r23-descriptor"),
        provider_config_sha256=_sha("cross-binding-provider-config"),
        prompt_sha256=_sha("cross-binding-prompts"),
        rubric_schema_sha256=_sha("cross-binding-rubric-schema"),
        tracking_packet_schema_sha256=_sha("cross-binding-packet-schema"),
        tracker_schema_sha256=_sha("cross-binding-tracker-schema"),
        generate_output_schema_sha256=_sha("cross-binding-generate-schema"),
        track_output_schema_sha256=_sha("cross-binding-track-schema"),
        configured_model=LIVE_RUBRIC_MODEL,
        external_network_attempted=True,
        model_call_attempted=True,
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
            request_sha256=_sha(f"cross-binding-request-{index}"),
            transport_binding_sha256=_sha(f"cross-binding-transport-{index}"),
            pricing_binding_sha256=pricing_sha256,
            execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
            status=LiveAttemptStatusV1.COMPLETED,
            dispatch_count=1,
            response_envelope_sha256=_sha(f"cross-binding-envelope-{index}"),
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
        for index in (1, 2)
    )
    attempt_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
    operations = (LiveRubricOperationV1.GENERATE, LiveRubricOperationV1.TRACK)
    receipts = tuple(
        LiveRubricCallReceiptV1(
            receipt_id=f"r24-cross-binding-receipt-{index}",
            operation=operation,
            execution_scope=extension.execution_scope,
            task_run_id="r24-cross-binding-task-run",
            logical_call_id=logical_call_id,
            backend_extension_descriptor_sha256=extension.sha256,
            r23_compatibility_descriptor_sha256=(extension.r23_compatibility_descriptor_sha256),
            transport_kind=extension.transport_kind,
            transport_authority=extension.transport_authority,
            prompt_sha256=_sha(f"cross-binding-prompt-{index}"),
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
            provider_output_sha256=_sha(f"cross-binding-output-{index}"),
            transport_binding_sha256=attempt.transport_binding_sha256,
            pricing_binding_sha256=attempt.pricing_binding_sha256,
            current_image_binding_sha256=(
                None
                if operation is LiveRubricOperationV1.GENERATE
                else _sha("cross-binding-current-image")
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
        for index, (operation, attempt, attempt_hash) in enumerate(
            zip(operations, attempts, attempt_hashes, strict=True),
            start=1,
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
    return extension, attempts, receipts, binding


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
def test_live_rubric_cross_binding_rejects_every_field_drift(drift: str) -> None:
    extension, attempts, receipts, binding = _complete_live_rubric_cross_binding_proof()
    logical_call_id = binding.logical_call_id
    actor_request_sha256 = binding.actor_request_sha256
    validate_live_rubric_cross_bindings_v1(
        logical_call_id=logical_call_id,
        actor_request_sha256=actor_request_sha256,
        attempts=attempts,
        rubric_call_receipts=receipts,
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
            rubric_call_receipts=receipts,
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
def test_live_rubric_cross_binding_rejects_nonprefix_sequences(drift: str) -> None:
    extension, attempts, receipts, binding = _complete_live_rubric_cross_binding_proof()
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
        selected_binding = None
        selected_attempts = attempts
        expect_history = False
    elif drift == "missing_first_receipt":
        receipts = (receipts[1],)
        selected_binding = None
        selected_attempts = attempts
        expect_history = False
    elif drift == "history_before_rubric":
        receipts = ()
        selected_binding = None
        selected_attempts = (history_attempt, attempts[0])
        expect_history = True
    elif drift == "unmatched_rubric_followed_by_history":
        receipts = (receipts[0],)
        selected_binding = None
        selected_attempts = (*attempts, history_attempt)
        expect_history = True
    elif drift == "partial_with_completed_binding":
        receipts = (receipts[0],)
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
            rubric_call_receipts=receipts,
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
                request_sha256=_sha(f"rubric-request-{index}"),
                execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                status=LiveAttemptStatusV1.COMPLETED,
                dispatch_count=1,
                response_envelope_sha256=_sha(f"rubric-response-{index}"),
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
            for index in (1, 2)
        )
        attempt_hashes = tuple(live_attempt_receipt_sha256(item) for item in attempts)
        rubric_backend_extension = R24RubricBackendExtensionDescriptorV1(
            descriptor_id="r24-no-history-rubric-extension",
            descriptor_version="v1",
            execution_scope=LiveRubricExecutionScopeV1.OWNER_AUTHORIZED_LIVE,
            transport_kind=LiveRubricTransportKindV1.OPENAI_RESPONSES,
            transport_authority=(LiveRubricTransportAuthorityV1.EXPLICIT_OWNER_AUTHORIZATION),
            r23_compatibility_descriptor_sha256=_sha("r23-compatibility"),
            provider_config_sha256=_sha("rubric-provider-config"),
            prompt_sha256=_sha("rubric-extension-prompt"),
            rubric_schema_sha256=_sha("rubric-schema"),
            tracking_packet_schema_sha256=_sha("tracking-packet-schema"),
            tracker_schema_sha256=_sha("tracker-schema"),
            generate_output_schema_sha256=_sha("rubric-generate-schema"),
            track_output_schema_sha256=_sha("rubric-track-schema"),
            configured_model=LIVE_RUBRIC_MODEL,
            external_network_attempted=True,
            model_call_attempted=True,
        )
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
                prompt_sha256=_sha(f"rubric-prompt-{index}"),
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
                provider_output_sha256=_sha(f"rubric-output-{index}"),
                transport_binding_sha256=attempt.transport_binding_sha256,
                pricing_binding_sha256=attempt.pricing_binding_sha256,
                current_image_binding_sha256=(None if index == 1 else _sha("rubric-current-image")),
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
            for index, (attempt, attempt_hash) in enumerate(
                zip(attempts, attempt_hashes, strict=True), start=1
            )
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
