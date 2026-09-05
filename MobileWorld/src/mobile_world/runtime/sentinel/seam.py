"""R2.1 provider-free Prompt Sentinel runtime seam."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token, copy_context
from copy import deepcopy
from dataclasses import dataclass, fields
from hashlib import sha256
from threading import Event, Lock, Thread
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import (
    RENDER_RESULT_SCHEMA_VERSION,
    TRANSFORMATION_PLAN_SCHEMA_VERSION,
    ArmKind,
    CapabilityLevel,
    CodecCapabilities,
    CodecScope,
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    FallbackState,
    FrozenTextSlice,
    HistoryCodecResolver,
    HistoryFamily,
    HistoryIR,
    HistoryRecord,
    HistoryRelationship,
    JsonValue,
    ListInsertionDiff,
    MappingKind,
    OperationKind,
    PlanOperation,
    PortableContractError,
    RecordCoordinates,
    RecordModality,
    RegionAvailability,
    RegionKind,
    RelatedContentKind,
    RelatedContentRef,
    RelationshipKind,
    RenderDiff,
    RenderResult,
    RequestRegion,
    SourceMapping,
    SourceSpan,
    SourceVersionRef,
    SpanRole,
    TransformationPlan,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)
from mobile_world.offline.causal_replay.core import (
    render_request,
    restore_original,
    validate_history_ir,
    validate_plan,
)
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.sentinel.contracts import (
    SentinelBypassReason,
    SentinelCallRole,
    SentinelContext,
    SentinelContractError,
    SentinelDecision,
    SentinelDecisionKind,
    SentinelFallbackReason,
    SentinelHostConfig,
    SentinelMode,
    SentinelPolicy,
    SentinelPolicyOutput,
    SentinelReceipt,
    SentinelReceiptSink,
    SentinelReceiptTransaction,
    SentinelResult,
    SentinelValidationStatus,
)
from mobile_world.runtime.sentinel.policies import NoOpSentinelPolicy
from mobile_world.runtime.sentinel.r2_2.contracts import (
    PolicyExecutionControlV1,
    RuntimeEvidencePolicyV1,
    RuntimeExecutionScope,
    RuntimeSentinelPolicyOutputV1,
    runtime_policy_output_projection,
)
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import (
    RuntimeRenderResultV1,
    render_runtime_admitted_plan,
    validate_runtime_render_result,
)
from mobile_world.runtime.sentinel.r2_4.audit_detail import (
    ParserResultStatusV1,
    RuntimeAuditDetailV1,
)
from mobile_world.runtime.sentinel.r2_4.audit_runtime import R24RuntimeAuditV1
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    RuntimeEditableSpanCodecV1,
    RuntimeHistoryExtractionStatusV1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicy,
    RuntimeVerticalPolicyOutputV1,
    RuntimeVerticalReceiptBridgeV1,
    RuntimeVerticalSentinelResultV1,
    snapshot_vertical_output,
    snapshot_vertical_sentinel_result,
    vertical_output_projection,
    vertical_output_sha256,
)
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    ProductionOpenAIAttemptCallV1,
)
from mobile_world.runtime.sentinel.r2_4.live_policy import (
    OwnerAuthorizedLivePerCallPolicyV1,
    R22OwnerAuthorizedLivePolicyAdapter,
)
from mobile_world.runtime.sentinel.r2_4.orchestration import R24CoordinatedCallRecordV1
from mobile_world.runtime.sentinel.r2_4.policy import R22CpuFakeActivePolicyAdapter
from mobile_world.runtime.sentinel.r2_4.production_audit import (
    ProductionRuntimeAuditFailureReceiptV1,
    ProductionRuntimeAuditReceiptV1,
    ProductionRuntimeAuditV1,
)
from mobile_world.runtime.sentinel.r2_4.renderer import (
    RuntimeVerticalRenderResultV1,
    render_vertical_admitted_plan,
    validate_vertical_render_result,
)

_EMPTY_DIFF_SHA256 = canonical_sha256({"diffs": [], "list_insertions": []})
_EMPTY_POLICY_OUTPUT_SHA256 = canonical_sha256({"decisions": [], "transformation_plan": None})
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CHECK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_CURRENT_LOGICAL_CALL: ContextVar[SentinelLogicalCall | None] = ContextVar(
    "mobileworld_prompt_sentinel_logical_call", default=None
)


def _implements_sentinel_policy(value: object) -> bool:
    """Keep runtime protocol admission separate from static protocol narrowing."""

    return isinstance(value, SentinelPolicy)


def _is_strict_json_value(value: Any) -> bool:
    """Return whether value is an acyclic exact built-in, finite JSON tree."""

    def visit(item: Any, active_container_ids: set[int]) -> bool:
        item_type = type(item)
        if item is None or item_type in {str, bool, int}:
            return True
        if item_type is float:
            return math.isfinite(item)
        if item_type not in {list, dict}:
            return False
        identity = id(item)
        if identity in active_container_ids:
            return False
        active_container_ids.add(identity)
        try:
            if item_type is list:
                return all(visit(value, active_container_ids) for value in item)
            return all(
                type(key) is str and visit(value, active_container_ids)
                for key, value in item.items()
            )
        finally:
            active_container_ids.remove(identity)

    return visit(value, set())


def _require_canonical_json_domain(value: Any) -> None:
    if not _is_strict_json_value(value):
        raise SentinelContractError("request is outside canonical-JSON admission domain")


class SentinelGlobalSwitch:
    """Process-wide, thread-safe emergency kill switch."""

    def __init__(self, *, active: bool = False) -> None:
        self._active = bool(active)
        self._activation_generation = 1 if self._active else 0
        self._lock = Lock()

    @property
    def active(self) -> bool:
        return self.snapshot()[0]

    def snapshot(self) -> tuple[bool, int]:
        """Return one atomic level/activation-edge snapshot."""

        with self._lock:
            return self._active, self._activation_generation

    def set_active(self, active: bool) -> None:
        if type(active) is not bool:
            raise TypeError("kill switch state must be bool")
        with self._lock:
            if active and not self._active:
                self._activation_generation += 1
            self._active = active


GLOBAL_SENTINEL_KILL_SWITCH = SentinelGlobalSwitch()


def set_global_sentinel_kill_switch(active: bool) -> None:
    GLOBAL_SENTINEL_KILL_SWITCH.set_active(active)


def global_sentinel_kill_switch_active() -> bool:
    return GLOBAL_SENTINEL_KILL_SWITCH.active


def current_sentinel_logical_call() -> SentinelLogicalCall | None:
    return _CURRENT_LOGICAL_CALL.get()


@contextmanager
def bind_sentinel_logical_call(call: SentinelLogicalCall):
    if not isinstance(call, SentinelLogicalCall):
        raise TypeError("call must be SentinelLogicalCall")
    token: Token[SentinelLogicalCall | None] = _CURRENT_LOGICAL_CALL.set(call)
    try:
        yield call
    finally:
        _CURRENT_LOGICAL_CALL.reset(token)


@dataclass(frozen=True)
class _EvaluationFailure(Exception):
    reason: SentinelFallbackReason
    check: str


class _PolicyExecutionFence(PolicyExecutionControlV1):
    """Seam-owned transport/receipt linearization fence for one R2.2 worker."""

    def __init__(self, *, deadline_ns: int, clock_ns: Callable[[], int]) -> None:
        self._deadline_ns = deadline_ns
        self._clock_ns = clock_ns
        self._cancelled = Event()
        self._generic_transport_started = False
        self._production_attempt_count = 0
        self._receipt_published = False
        self._active_cancellable_call: ProductionOpenAIAttemptCallV1 | None = None
        self._transport_lock = Lock()
        self._publication_lock = Lock()

    def _require_open(self) -> None:
        if self._cancelled.is_set() or self._clock_ns() >= self._deadline_ns:
            raise TimeoutError("R2.2 policy execution deadline has closed")

    def run_transport[T](self, call: Callable[[], T]) -> T:
        if not callable(call):
            raise TypeError("transport callback must be callable")
        trusted_cancellable = call if type(call) is ProductionOpenAIAttemptCallV1 else None
        gate_closed = False
        with self._transport_lock:
            try:
                self._require_open()
            except TimeoutError:
                gate_closed = True
            else:
                if trusted_cancellable is None:
                    if self._generic_transport_started or self._production_attempt_count:
                        raise RuntimeError("R2.2 policy transport was already started")
                    self._generic_transport_started = True
                else:
                    if self._generic_transport_started or self._active_cancellable_call is not None:
                        raise RuntimeError("production policy attempts must be sequential")
                    self._production_attempt_count += 1
                self._active_cancellable_call = trusted_cancellable
        if gate_closed:
            if trusted_cancellable is not None:
                trusted_cancellable.cancel_and_join()
            raise TimeoutError("R2.2 policy execution deadline has closed")
        try:
            return call()
        finally:
            if trusted_cancellable is not None:
                with self._transport_lock:
                    if self._active_cancellable_call is trusted_cancellable:
                        self._active_cancellable_call = None

    def publish_receipt(self, publish: Callable[[], None]) -> None:
        if not callable(publish):
            raise TypeError("receipt publish callback must be callable")
        with self._publication_lock:
            self._require_open()
            if self._receipt_published:
                raise RuntimeError("R2.2 policy receipt was already published")
            publish()
            self._receipt_published = True

    def cancel(self) -> None:
        """Close both gates and join an exact production child before fallback."""

        self._cancelled.set()
        with self._transport_lock:
            cancellable = self._active_cancellable_call
        if cancellable is not None:
            cancellable.cancel_and_join()
        # Publication is a module-owned constant-time append.  Waiting for this
        # short critical section guarantees that no receipt appears after the
        # actor receives its timeout fallback, without waiting for transport.
        with self._publication_lock:
            pass


_HISTORY_IR_DATACLASS_TYPES = frozenset(
    {
        HistoryIR,
        RequestRegion,
        FrozenTextSlice,
        HistoryRecord,
        HistoryRelationship,
        SourceVersionRef,
        RecordCoordinates,
        RelatedContentRef,
        CodecCapabilities,
        SourceSpan,
        CorrectionAnchor,
    }
)
_HISTORY_IR_ENUM_TYPES = frozenset(
    {
        HistoryFamily,
        RegionKind,
        RegionAvailability,
        RecordModality,
        RelatedContentKind,
        RelationshipKind,
        CapabilityLevel,
        CodecScope,
        OperationKind,
        ArmKind,
        SpanRole,
        CorrectionPlacement,
        CorrectionContextKind,
    }
)


def _snapshot_history_ir_node(value: Any) -> Any:
    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return value
    if value_type is float:
        if not math.isfinite(value):
            raise _EvaluationFailure(
                SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                "history_ir_untrusted_type",
            )
        return value
    if value_type in _HISTORY_IR_ENUM_TYPES:
        return value
    if value_type is tuple:
        return tuple(_snapshot_history_ir_node(item) for item in value)
    if value_type in {list, dict}:
        if not _is_strict_json_value(value):
            raise _EvaluationFailure(
                SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                "history_ir_untrusted_type",
            )
        return copy_json(value)
    if value_type in _HISTORY_IR_DATACLASS_TYPES:
        return value_type(
            **{
                item.name: _snapshot_history_ir_node(getattr(value, item.name))
                for item in fields(value)
            }
        )
    raise _EvaluationFailure(
        SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
        "history_ir_untrusted_type",
    )


def _snapshot_history_ir(value: Any) -> HistoryIR:
    if type(value) is not HistoryIR:
        raise _EvaluationFailure(
            SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
            "history_ir_untrusted_type",
        )
    snapshot = _snapshot_history_ir_node(value)
    assert type(snapshot) is HistoryIR
    return snapshot


@dataclass(frozen=True)
class _PolicyOutputSnapshot:
    """Detached trusted graph and its immutable canonical hash preimage."""

    output: SentinelPolicyOutput
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class _RuntimePolicyOutputSnapshot:
    """Detached R2.2 graph and the exact preimage shared by all later gates."""

    output: RuntimeSentinelPolicyOutputV1
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class _VerticalPolicyOutputSnapshot:
    """Detached R2.4 graph and its exact module-owned canonical preimage."""

    output: RuntimeVerticalPolicyOutputV1
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class _ActorProviderTraceSnapshot:
    """Safe metadata accumulated across retries for one actor logical call."""

    attempt_count: int
    latency_ns: int
    response_id: str | None
    model_id: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


def _untrusted_policy_output() -> _EvaluationFailure:
    return _EvaluationFailure(
        SentinelFallbackReason.INVALID_POLICY_OUTPUT,
        "policy_output_untrusted_type",
    )


def _require_exact_string(value: Any) -> str:
    if type(value) is not str:
        raise _untrusted_policy_output()
    return value


def _require_exact_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _require_exact_string(value)


def _snapshot_json_path(value: Any) -> tuple[str | int, ...]:
    if type(value) is not tuple or any(type(item) not in {str, int} for item in value):
        raise _untrusted_policy_output()
    return tuple(value)


def _snapshot_source_span(value: Any) -> SourceSpan:
    if type(value) is not SourceSpan:
        raise _untrusted_policy_output()
    if (
        any(
            type(item) is not int
            for item in (
                value.char_start,
                value.char_end,
                value.utf8_byte_start,
                value.utf8_byte_end,
            )
        )
        or type(value.span_role) is not SpanRole
    ):
        raise _untrusted_policy_output()
    return SourceSpan(
        container_path=_snapshot_json_path(value.container_path),
        char_start=value.char_start,
        char_end=value.char_end,
        utf8_byte_start=value.utf8_byte_start,
        utf8_byte_end=value.utf8_byte_end,
        exact_text=_require_exact_string(value.exact_text),
        span_sha256=_require_exact_string(value.span_sha256),
        span_role=value.span_role,
        claim_id=_require_exact_optional_string(value.claim_id),
    )


def _snapshot_evidence_ref(value: Any) -> EvidenceRef:
    if type(value) is not EvidenceRef or (
        value.event_seq is not None and type(value.event_seq) is not int
    ):
        raise _untrusted_policy_output()
    return EvidenceRef(
        evidence_id=_require_exact_string(value.evidence_id),
        sha256=_require_exact_string(value.sha256),
        role=_require_exact_string(value.role),
        event_seq=value.event_seq,
    )


def _snapshot_correction_anchor(value: Any) -> CorrectionAnchor:
    if (
        type(value) is not CorrectionAnchor
        or type(value.insert_index) is not int
        or type(value.placement) is not CorrectionPlacement
        or type(value.context_kind) is not CorrectionContextKind
    ):
        raise _untrusted_policy_output()
    return CorrectionAnchor(
        container_path=_snapshot_json_path(value.container_path),
        insert_index=value.insert_index,
        source_container_sha256=_require_exact_string(value.source_container_sha256),
        owner_region_id=_require_exact_string(value.owner_region_id),
        host_context_path=_snapshot_json_path(value.host_context_path),
        host_context_sha256=_require_exact_string(value.host_context_sha256),
        role_path=_snapshot_json_path(value.role_path),
        expected_role=_require_exact_string(value.expected_role),
        reference_path=_snapshot_json_path(value.reference_path),
        reference_sha256=_require_exact_string(value.reference_sha256),
        placement=value.placement,
        context_kind=value.context_kind,
        visible_prefix=_require_exact_string(value.visible_prefix),
        visible_suffix=_require_exact_string(value.visible_suffix),
    )


def _snapshot_plan_operation(value: Any) -> PlanOperation:
    if (
        type(value) is not PlanOperation
        or type(value.kind) is not OperationKind
        or type(value.evidence_refs) is not tuple
        or type(value.protocol_shell_for) is not tuple
        or any(type(item) is not str for item in value.protocol_shell_for)
        or not _is_strict_json_value(value.rendered_correction_context)
    ):
        raise _untrusted_policy_output()
    return PlanOperation(
        operation_id=_require_exact_string(value.operation_id),
        kind=value.kind,
        target_record_id=_require_exact_string(value.target_record_id),
        target_span=_snapshot_source_span(value.target_span),
        replacement_text=_require_exact_optional_string(value.replacement_text),
        replacement_author=_require_exact_optional_string(value.replacement_author),
        evidence_refs=tuple(_snapshot_evidence_ref(item) for item in value.evidence_refs),
        protocol_shell_for=tuple(value.protocol_shell_for),
        correction_anchor=(
            None
            if value.correction_anchor is None
            else _snapshot_correction_anchor(value.correction_anchor)
        ),
        rendered_correction_context=copy_json(value.rendered_correction_context),
    )


def _snapshot_transformation_plan(value: Any) -> TransformationPlan:
    if (
        type(value) is not TransformationPlan
        or type(value.history_family) is not HistoryFamily
        or type(value.arm) is not ArmKind
        or type(value.operations) is not tuple
        or type(value.curated) is not bool  # type: ignore[redundant-expr]
        or type(value.deployment_prediction) is not bool
    ):
        raise _untrusted_policy_output()
    return TransformationPlan(
        plan_id=_require_exact_string(value.plan_id),
        host_id=_require_exact_string(value.host_id),
        history_family=value.history_family,
        codec_id=_require_exact_string(value.codec_id),
        codec_contract_version=_require_exact_string(value.codec_contract_version),
        source_request_sha256=_require_exact_string(value.source_request_sha256),
        arm=value.arm,
        operations=tuple(_snapshot_plan_operation(item) for item in value.operations),
        curated=value.curated,
        deployment_prediction=value.deployment_prediction,
    )


def _snapshot_sentinel_decision(value: Any) -> SentinelDecision:
    if type(value) is not SentinelDecision or type(value.kind) is not SentinelDecisionKind:
        raise _untrusted_policy_output()
    return SentinelDecision(
        decision_id=_require_exact_string(value.decision_id),
        kind=value.kind,
        operation_id=_require_exact_optional_string(value.operation_id),
        record_id=_require_exact_optional_string(value.record_id),
        reason_code=_require_exact_string(value.reason_code),
    )


def _policy_output_canonical_view(value: Any) -> dict[str, JsonValue]:
    if type(value) is not SentinelPolicyOutput or type(value.decisions) is not tuple:
        raise _untrusted_policy_output()
    decisions: list[JsonValue] = []
    for decision in value.decisions:
        if (
            type(decision) is not SentinelDecision
            or type(decision.kind) is not SentinelDecisionKind
        ):
            raise _untrusted_policy_output()
        decisions.append(
            {
                "decision_id": _require_exact_string(decision.decision_id),
                "kind": decision.kind.value,
                "operation_id": _require_exact_optional_string(decision.operation_id),
                "record_id": _require_exact_optional_string(decision.record_id),
                "reason_code": _require_exact_string(decision.reason_code),
            }
        )
    plan = value.transformation_plan
    if plan is None:
        plan_view: JsonValue = None
    else:
        if (
            type(plan) is not TransformationPlan
            or type(plan.history_family) is not HistoryFamily
            or type(plan.arm) is not ArmKind
            or type(plan.operations) is not tuple
            or type(plan.curated) is not bool  # type: ignore[redundant-expr]
            or type(plan.deployment_prediction) is not bool
        ):
            raise _untrusted_policy_output()
        operations: list[JsonValue] = []
        for operation in plan.operations:
            if (
                type(operation) is not PlanOperation
                or type(operation.kind) is not OperationKind
                or type(operation.evidence_refs) is not tuple
                or type(operation.protocol_shell_for) is not tuple
                or any(type(item) is not str for item in operation.protocol_shell_for)
                or not _is_strict_json_value(operation.rendered_correction_context)
            ):
                raise _untrusted_policy_output()
            span = operation.target_span
            if type(span) is not SourceSpan or (
                any(
                    type(item) is not int
                    for item in (
                        span.char_start,
                        span.char_end,
                        span.utf8_byte_start,
                        span.utf8_byte_end,
                    )
                )
                or type(span.span_role) is not SpanRole
            ):
                raise _untrusted_policy_output()
            evidence: list[JsonValue] = []
            for item in operation.evidence_refs:
                if type(item) is not EvidenceRef or (
                    item.event_seq is not None and type(item.event_seq) is not int
                ):
                    raise _untrusted_policy_output()
                evidence.append(
                    {
                        "evidence_id": _require_exact_string(item.evidence_id),
                        "sha256": _require_exact_string(item.sha256),
                        "role": _require_exact_string(item.role),
                        "event_seq": item.event_seq,
                    }
                )
            anchor = operation.correction_anchor
            if anchor is None:
                anchor_view: JsonValue = None
            else:
                if (
                    type(anchor) is not CorrectionAnchor
                    or type(anchor.insert_index) is not int
                    or type(anchor.placement) is not CorrectionPlacement
                    or type(anchor.context_kind) is not CorrectionContextKind
                ):
                    raise _untrusted_policy_output()
                anchor_view = {
                    "container_path": list(_snapshot_json_path(anchor.container_path)),
                    "insert_index": anchor.insert_index,
                    "source_container_sha256": _require_exact_string(
                        anchor.source_container_sha256
                    ),
                    "owner_region_id": _require_exact_string(anchor.owner_region_id),
                    "host_context_path": list(_snapshot_json_path(anchor.host_context_path)),
                    "host_context_sha256": _require_exact_string(anchor.host_context_sha256),
                    "role_path": list(_snapshot_json_path(anchor.role_path)),
                    "expected_role": _require_exact_string(anchor.expected_role),
                    "reference_path": list(_snapshot_json_path(anchor.reference_path)),
                    "reference_sha256": _require_exact_string(anchor.reference_sha256),
                    "placement": anchor.placement.value,
                    "context_kind": anchor.context_kind.value,
                    "visible_prefix": _require_exact_string(anchor.visible_prefix),
                    "visible_suffix": _require_exact_string(anchor.visible_suffix),
                }
            operations.append(
                {
                    "operation_id": _require_exact_string(operation.operation_id),
                    "kind": operation.kind.value,
                    "target_record_id": _require_exact_string(operation.target_record_id),
                    "target_span": {
                        "container_path": list(_snapshot_json_path(span.container_path)),
                        "char_start": span.char_start,
                        "char_end": span.char_end,
                        "utf8_byte_start": span.utf8_byte_start,
                        "utf8_byte_end": span.utf8_byte_end,
                        "exact_text": _require_exact_string(span.exact_text),
                        "span_sha256": _require_exact_string(span.span_sha256),
                        "span_role": span.span_role.value,
                        "claim_id": _require_exact_optional_string(span.claim_id),
                    },
                    "replacement_text": _require_exact_optional_string(operation.replacement_text),
                    "replacement_author": _require_exact_optional_string(
                        operation.replacement_author
                    ),
                    "evidence_refs": evidence,
                    "protocol_shell_for": list(operation.protocol_shell_for),
                    "correction_anchor": anchor_view,
                    "rendered_correction_context": copy_json(operation.rendered_correction_context),
                }
            )
        plan_view = {
            "schema_version": TRANSFORMATION_PLAN_SCHEMA_VERSION,
            "plan_id": _require_exact_string(plan.plan_id),
            "host_id": _require_exact_string(plan.host_id),
            "history_family": plan.history_family.value,
            "codec_id": _require_exact_string(plan.codec_id),
            "codec_contract_version": _require_exact_string(plan.codec_contract_version),
            "source_request_sha256": _require_exact_string(plan.source_request_sha256),
            "arm": plan.arm.value,
            "curated": plan.curated,
            "deployment_prediction": plan.deployment_prediction,
            "operations": operations,
        }
    return {"decisions": decisions, "transformation_plan": plan_view}


def _snapshot_policy_output(
    value: Any,
    *,
    canonical_bytes: bytes,
    canonical_digest: str,
) -> _PolicyOutputSnapshot:
    trusted = SentinelPolicyOutput(
        decisions=tuple(_snapshot_sentinel_decision(item) for item in value.decisions),
        transformation_plan=(
            None
            if value.transformation_plan is None
            else _snapshot_transformation_plan(value.transformation_plan)
        ),
    )
    trusted_bytes = canonical_json_bytes(SentinelPolicyOutput.to_dict(trusted))
    if trusted_bytes != canonical_bytes:
        raise _EvaluationFailure(
            SentinelFallbackReason.INVALID_POLICY_OUTPUT,
            "policy_output_snapshot_mismatch",
        )
    return _PolicyOutputSnapshot(
        output=trusted,
        canonical_bytes=canonical_bytes,
        sha256=canonical_digest,
    )


def _snapshot_runtime_policy_output(value: Any) -> _RuntimePolicyOutputSnapshot:
    """Detach an exact R2.2 output without dispatching policy-owned serializers."""

    canonical_bytes = canonical_json_bytes(runtime_policy_output_projection(value))
    canonical_digest = sha256(canonical_bytes).hexdigest()
    trusted = deepcopy(value)
    if (
        type(trusted) is not RuntimeSentinelPolicyOutputV1
        or canonical_json_bytes(runtime_policy_output_projection(trusted)) != canonical_bytes
    ):
        raise _EvaluationFailure(
            SentinelFallbackReason.INVALID_POLICY_OUTPUT,
            "r2_2_policy_output_snapshot_mismatch",
        )
    return _RuntimePolicyOutputSnapshot(
        output=trusted,
        canonical_bytes=canonical_bytes,
        sha256=canonical_digest,
    )


def _snapshot_vertical_policy_output(value: Any) -> _VerticalPolicyOutputSnapshot:
    """Detach the R2.4 output without invoking policy-owned serializers."""

    canonical_bytes = canonical_json_bytes(vertical_output_projection(value))
    canonical_digest = sha256(canonical_bytes).hexdigest()
    trusted = snapshot_vertical_output(value)
    if canonical_json_bytes(vertical_output_projection(trusted)) != canonical_bytes:
        raise _EvaluationFailure(
            SentinelFallbackReason.INVALID_POLICY_OUTPUT,
            "r2_4_policy_output_snapshot_mismatch",
        )
    return _VerticalPolicyOutputSnapshot(
        output=trusted,
        canonical_bytes=canonical_bytes,
        sha256=canonical_digest,
    )


def _render_json_path(value: Any) -> list[str | int]:
    if type(value) is not tuple or any(type(item) not in {str, int} for item in value):
        raise SentinelContractError("renderer result contains an untrusted JSON path")
    return list(value)


def _render_exact_string(value: Any) -> str:
    if type(value) is not str:
        raise SentinelContractError("renderer result contains an untrusted string")
    return value


def _render_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return _render_exact_string(value)


def _render_result_canonical_view(value: Any) -> dict[str, JsonValue]:
    if (
        type(value) is not RenderResult
        or not _is_strict_json_value(value.original_request)
        or not _is_strict_json_value(value.rendered_request)
        or type(value.requested_arm) is not ArmKind
        or (value.effective_arm is not None and type(value.effective_arm) is not ArmKind)
        or type(value.execution_mode) is not ExecutionMode
        or type(value.failure_policy) is not FailurePolicy
        or type(value.diffs) is not tuple
        or type(value.list_insertions) is not tuple
        or type(value.source_mappings) is not tuple
        or type(value.warnings) is not tuple
        or any(type(item) is not str for item in value.warnings)
        or type(value.fallback_state) is not FallbackState
        or type(value.count_as_treatment) is not bool
    ):
        raise SentinelContractError("renderer result is outside the trusted result domain")
    diffs: list[JsonValue] = []
    for diff_item in value.diffs:
        if (
            type(diff_item) is not RenderDiff
            or type(diff_item.source_char_start) is not int
            or type(diff_item.source_char_end) is not int
            or type(diff_item.mapping_kind) is not MappingKind
        ):
            raise SentinelContractError("renderer diff is outside the trusted result domain")
        diffs.append(
            {
                "operation_id": _render_exact_string(diff_item.operation_id),
                "container_path": cast(JsonValue, _render_json_path(diff_item.container_path)),
                "source_char_start": diff_item.source_char_start,
                "source_char_end": diff_item.source_char_end,
                "original_text": _render_exact_string(diff_item.original_text),
                "rendered_text": _render_exact_string(diff_item.rendered_text),
                "original_sha256": _render_exact_string(diff_item.original_sha256),
                "rendered_sha256": _render_exact_string(diff_item.rendered_sha256),
                "mapping_kind": diff_item.mapping_kind.value,
            }
        )
    insertions: list[JsonValue] = []
    for insertion in value.list_insertions:
        if (
            type(insertion) is not ListInsertionDiff
            or type(insertion.source_index) is not int
            or type(insertion.rendered_index) is not int
            or not _is_strict_json_value(insertion.inserted_value)
        ):
            raise SentinelContractError("renderer insertion is outside the trusted result domain")
        insertions.append(
            {
                "operation_id": _render_exact_string(insertion.operation_id),
                "container_path": cast(JsonValue, _render_json_path(insertion.container_path)),
                "source_index": insertion.source_index,
                "rendered_index": insertion.rendered_index,
                "inserted_value": copy_json(insertion.inserted_value),
                "inserted_value_sha256": _render_exact_string(insertion.inserted_value_sha256),
            }
        )
    mappings: list[JsonValue] = []
    for mapping in value.source_mappings:
        if (
            type(mapping) is not SourceMapping
            or type(mapping.source_char_start) is not int
            or type(mapping.source_char_end) is not int
            or type(mapping.rendered_char_start) is not int
            or type(mapping.rendered_char_end) is not int
            or type(mapping.kind) is not MappingKind
        ):
            raise SentinelContractError("renderer mapping is outside the trusted result domain")
        mappings.append(
            {
                "container_path": cast(JsonValue, _render_json_path(mapping.container_path)),
                "source_char_start": mapping.source_char_start,
                "source_char_end": mapping.source_char_end,
                "rendered_char_start": mapping.rendered_char_start,
                "rendered_char_end": mapping.rendered_char_end,
                "kind": mapping.kind.value,
                "operation_id": _render_optional_string(mapping.operation_id),
            }
        )
    return {
        "schema_version": RENDER_RESULT_SCHEMA_VERSION,
        "original_request": copy_json(value.original_request),
        "rendered_request": copy_json(value.rendered_request),
        "source_request_sha256": _render_exact_string(value.source_request_sha256),
        "rendered_request_sha256": _render_exact_string(value.rendered_request_sha256),
        "plan_sha256": _render_exact_string(value.plan_sha256),
        "capability_sha256": _render_exact_string(value.capability_sha256),
        "requested_arm": value.requested_arm.value,
        "effective_arm": None if value.effective_arm is None else value.effective_arm.value,
        "execution_mode": value.execution_mode.value,
        "failure_policy": value.failure_policy.value,
        "diffs": diffs,
        "list_insertions": insertions,
        "source_mappings": mappings,
        "warnings": list(value.warnings),
        "fallback_state": value.fallback_state.value,
        "count_as_treatment": value.count_as_treatment,
        "unsupported_reason": _render_optional_string(value.unsupported_reason),
    }


class PromptSentinel:
    """Single pre-provider hook with typed Original fallback semantics."""

    def __init__(
        self,
        *,
        policy: Any,
        codec_registry: HistoryCodecResolver,
        host_configs: dict[str, SentinelHostConfig] | None = None,
        default_host_config: SentinelHostConfig | None = None,
        receipt_sink: SentinelReceiptSink | None = None,
        runtime_audit: R24RuntimeAuditV1 | ProductionRuntimeAuditV1 | None = None,
        global_switch: SentinelGlobalSwitch = GLOBAL_SENTINEL_KILL_SWITCH,
        logical_call_id_factory: Any = new_ulid,
        clock_ns: Any = time.monotonic_ns,
    ) -> None:
        policy_object = cast(object, policy)
        if not _implements_sentinel_policy(policy_object):
            raise TypeError("policy must implement SentinelPolicy")
        if not isinstance(codec_registry, HistoryCodecResolver):
            raise TypeError("codec_registry must implement HistoryCodecResolver")
        if not isinstance(global_switch, SentinelGlobalSwitch):
            raise TypeError("global_switch must be SentinelGlobalSwitch")
        configs = dict(host_configs or {})
        if any(
            not key or not isinstance(value, SentinelHostConfig) for key, value in configs.items()
        ):
            raise TypeError("host_configs must map non-empty host IDs to SentinelHostConfig")
        if runtime_audit is not None and type(runtime_audit) not in {
            R24RuntimeAuditV1,
            ProductionRuntimeAuditV1,
        }:
            raise TypeError("runtime_audit must use one exact R2.4 audit type")
        if type(runtime_audit) is ProductionRuntimeAuditV1:
            if type(policy_object) is NoOpSentinelPolicy:
                if runtime_audit.live_policy is not None:
                    raise TypeError("OFF production audit cannot bind live policy authority")
                configured_modes = tuple(value.mode for value in configs.values())
                effective_default = default_host_config or SentinelHostConfig()
                if any(mode is not SentinelMode.OFF for mode in configured_modes) or (
                    effective_default.mode is not SentinelMode.OFF
                ):
                    raise TypeError("NoOp production audit is restricted to exact OFF hosts")
            elif type(policy_object) is OwnerAuthorizedLivePerCallPolicyV1:
                if runtime_audit.live_policy is not policy_object:
                    raise TypeError("production audit must bind the identical live policy")
            else:
                raise TypeError("production audit supports only exact OFF or per-call live policy")
        if not callable(logical_call_id_factory) or not callable(clock_ns):
            raise TypeError("ID factory and clock must be callable")
        policy_id = cast(SentinelPolicy, policy_object).policy_id
        if _SEMANTIC_ID.fullmatch(policy_id) is None:
            raise TypeError("policy.policy_id must be a bounded safe identifier")
        runtime_vertical_policy = (
            isinstance(policy_object, RuntimeVerticalPolicy)
            and type(policy_object.execution_scope) is RuntimeVerticalExecutionScope
        )
        if runtime_vertical_policy:
            vertical_policy = cast(RuntimeVerticalPolicy, policy_object)
            if vertical_policy.execution_scope is RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE:
                if type(policy_object) is not R22CpuFakeActivePolicyAdapter:
                    raise TypeError("R2.4 CPU fake ACTIVE requires the exact trusted adapter")
                if runtime_audit is not None and type(runtime_audit) is not R24RuntimeAuditV1:
                    raise TypeError("R2.4 CPU fake policy requires the exact CPU audit")
            elif (
                vertical_policy.execution_scope
                is RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE
            ):
                if type(policy_object) not in {
                    R22OwnerAuthorizedLivePolicyAdapter,
                    OwnerAuthorizedLivePerCallPolicyV1,
                }:
                    raise TypeError("R2.4 live ACTIVE requires the exact owner-authorized adapter")
                if type(policy_object) is OwnerAuthorizedLivePerCallPolicyV1:
                    if type(runtime_audit) is not ProductionRuntimeAuditV1:
                        raise TypeError("per-call live policy requires the exact production audit")
                elif runtime_audit is not None:
                    raise TypeError("the CPU/fake detail sink cannot attest a live policy")
            else:
                raise TypeError("R2.4 policy declares an unsupported execution scope")
        runtime_evidence_policy = not runtime_vertical_policy and isinstance(
            policy_object, RuntimeEvidencePolicyV1
        )
        if runtime_evidence_policy:
            evidence_policy = cast(RuntimeEvidencePolicyV1, policy_object)
            if (
                type(evidence_policy.execution_scope) is not RuntimeExecutionScope
                or evidence_policy.execution_scope is not RuntimeExecutionScope.SHADOW_ONLY
            ):
                raise TypeError("R2.2 runtime policy must declare the exact SHADOW_ONLY scope")
        self._policy: Any = policy
        self._policy_id = policy_id
        self._runtime_evidence_policy = runtime_evidence_policy
        self._runtime_vertical_policy = runtime_vertical_policy
        self._codec_registry = codec_registry
        self._host_configs = configs
        self._default_host_config = default_host_config or SentinelHostConfig()
        self._receipt_sink = receipt_sink
        self._runtime_audit = runtime_audit
        self._global_switch = global_switch
        self._logical_call_id_factory = logical_call_id_factory
        self._clock_ns = clock_ns

    @property
    def policy(self) -> SentinelPolicy:
        return cast(SentinelPolicy, self._policy)

    @property
    def kill_switch_active(self) -> bool:
        return self._global_switch.active

    @property
    def strict_provider_audit(self) -> bool:
        """Whether provider dispatch requires the production audit chain."""

        return type(self._runtime_audit) is ProductionRuntimeAuditV1

    def host_config(self, host_id: str) -> SentinelHostConfig:
        return self._host_configs.get(host_id, self._default_host_config)

    def logical_call(
        self,
        *,
        host_id: str,
        history_codec_id: str | None,
        call_role: SentinelCallRole = SentinelCallRole.ACTOR,
        attributes: dict[str, JsonValue] | None = None,
    ) -> SentinelLogicalCall:
        if not host_id:
            raise ValueError("host_id is required")
        if history_codec_id is not None and _SEMANTIC_ID.fullmatch(history_codec_id) is None:
            raise ValueError("history_codec_id must be a bounded safe identifier")
        logical_call_id = self._logical_call_id_factory()
        if not isinstance(logical_call_id, str) or not logical_call_id:
            raise SentinelContractError("logical-call ID factory returned an invalid value")
        context = SentinelContext(
            logical_call_id=logical_call_id,
            host_id=host_id,
            attributes={} if attributes is None else attributes,
        )
        return SentinelLogicalCall(
            sentinel=self,
            context=context,
            history_codec_id=history_codec_id,
            call_role=call_role,
        )

    def finalize_actor_output(
        self,
        *,
        logical_call_id: str,
        attempt_id: str,
        raw_provider_response: JsonValue,
        raw_parser_input: JsonValue,
        parsed_action: JsonValue,
        parser_id: str,
        parser_status: ParserResultStatusV1,
        parser_attempt_count: int,
        provider_ns: int,
        parser_ns: int,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> RuntimeAuditDetailV1 | None:
        """Complete an optional R2.4 detail after the unchanged host parser."""

        if self._runtime_audit is None:
            return None
        return self._runtime_audit.finalize_actor_output(
            logical_call_id=logical_call_id,
            attempt_id=attempt_id,
            raw_provider_response=raw_provider_response,
            raw_parser_input=raw_parser_input,
            parsed_action=parsed_action,
            parser_id=parser_id,
            parser_status=parser_status,
            parser_attempt_count=parser_attempt_count,
            provider_ns=provider_ns,
            parser_ns=parser_ns,
            response_id=response_id,
            model_id=model_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def bind_actor_sdk_arguments(
        self,
        *,
        logical_call_id: str,
        result: SentinelResult | RuntimeVerticalSentinelResultV1,
        sdk_arguments: JsonValue,
        collector_request_locator: JsonValue | None,
        stream: bool,
    ) -> str | None:
        """Bind exact actor SDK arguments immediately before live dispatch."""

        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return None
        return self._runtime_audit.bind_actor_sdk_arguments(
            logical_call_id=logical_call_id,
            result=result,
            sdk_arguments=sdk_arguments,
            collector_request_locator=collector_request_locator,
            stream=stream,
        )

    def record_production_actor_provider_attempt(
        self,
        *,
        logical_call_id: str,
        succeeded: bool,
        latency_ns: int,
        raw_response: Any = None,
        collector_terminal_locator: JsonValue | None = None,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return
        self._runtime_audit.record_actor_provider_attempt(
            logical_call_id=logical_call_id,
            succeeded=succeeded,
            latency_ns=latency_ns,
            raw_response=raw_response,
            collector_terminal_locator=collector_terminal_locator,
            response_id=response_id,
            model_id=model_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def finalize_action_execution(
        self,
        *,
        logical_call_id: str,
        parsed_action: JsonValue,
        action_executed: bool,
        action_execution_ns: int = 0,
    ) -> ProductionRuntimeAuditReceiptV1 | None:
        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return None
        return self._runtime_audit.finalize_action_execution(
            logical_call_id=logical_call_id,
            parsed_action=parsed_action,
            action_executed=action_executed,
            action_execution_ns=action_execution_ns,
        )

    def production_runtime_audit_receipt_for(
        self, logical_call_id: str
    ) -> ProductionRuntimeAuditReceiptV1 | None:
        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return None
        return self._runtime_audit.receipt_for(logical_call_id)

    def finalize_production_actor_failure(
        self,
        *,
        logical_call_id: str,
        failure_phase: str,
        failure_code: str,
    ) -> ProductionRuntimeAuditFailureReceiptV1 | None:
        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return None
        return self._runtime_audit.finalize_actor_failure(
            logical_call_id=logical_call_id,
            failure_phase=failure_phase,
            failure_code=failure_code,
        )

    def production_runtime_audit_failure_for(
        self, logical_call_id: str
    ) -> ProductionRuntimeAuditFailureReceiptV1 | None:
        if type(self._runtime_audit) is not ProductionRuntimeAuditV1:
            return None
        return self._runtime_audit.failure_receipt_for(logical_call_id)

    def cancel_runtime_audit(self, logical_call_id: str) -> None:
        """Discard an optional pending detail when no actor output is produced."""

        if self._runtime_audit is not None:
            self._runtime_audit.cancel(logical_call_id)

    def before_model_call(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole | str,
    ) -> SentinelResult | RuntimeVerticalSentinelResultV1:
        """Evaluate one actor request and return an immutable separate final object."""

        started = self._clock_ns()
        audit_started_ns = time.monotonic_ns()
        history_extract_ns = 0
        policy_ns = 0
        render_ns = 0
        validator_ns = 0
        policy_output_sha256 = _EMPTY_POLICY_OUTPUT_SHA256
        policy_evaluation_started = Event()
        overlay_declaration_sha256: str | None = None
        try:
            role = SentinelCallRole(call_role)
        except ValueError as exc:
            raise SentinelContractError("call_role must be actor or sentinel") from exc
        _require_canonical_json_domain(request)
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        raw_sha256 = canonical_sha256(raw)
        config = self.host_config(context.host_id)
        kill_switch_active, kill_switch_generation = self._global_switch.snapshot()

        if role is SentinelCallRole.SENTINEL:
            return self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.CALL_ROLE_SENTINEL,
                kill_switch_active=kill_switch_active,
                started=started,
            )
        if kill_switch_active:
            return self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.GLOBAL_KILL_SWITCH,
                kill_switch_active=True,
                started=started,
            )
        if config.mode is SentinelMode.OFF:
            result = self._bypass_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                bypass_reason=SentinelBypassReason.MODE_OFF,
                kill_switch_active=False,
                started=started,
            )
            if type(self._runtime_audit) is ProductionRuntimeAuditV1:
                self._runtime_audit.begin_off_pre_provider(
                    logical_call_id=context.logical_call_id,
                    host_id=context.host_id,
                    raw_request=raw,
                    result=result,
                    pre_provider_total_ns=time.monotonic_ns() - audit_started_ns,
                )
            return result
        if self._receipt_sink is None:
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.SIDECAR_FAILURE,
                check="semantic_mode_requires_receipt_sink",
                started=started,
                persist=False,
                policy_evaluated=False,
            )

        try:
            receipt_transaction = self._begin_receipt_transaction(context.logical_call_id)
        except Exception:
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.SIDECAR_FAILURE,
                check="sidecar_admission_failed",
                started=started,
                persist=False,
                policy_evaluated=False,
            )

        if self._runtime_evidence_policy and config.mode is not SentinelMode.SHADOW:
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                check="r2_2_policy_is_shadow_only",
                started=started,
                transaction=receipt_transaction,
                policy_evaluated=False,
            )

        try:
            self._validate_request_schema(raw)
            if not history_codec_id:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "history_codec_id_missing",
                )
            history_extract_started_ns = time.monotonic_ns()
            extraction = None
            try:
                codec = self._codec_registry.by_id(
                    history_codec_id, config.history_codec_contract_version
                )
            except PortableContractError as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_resolution_failed",
                ) from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_resolution_exception",
                ) from exc
            if not isinstance(codec, HistoryCodec):
                raise _EvaluationFailure(
                    SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                    "codec_has_no_runtime_renderer",
                )
            try:
                if self._runtime_vertical_policy and type(codec) is RuntimeEditableSpanCodecV1:
                    extraction = codec.extract_runtime(copy_json(raw))
                    overlay_declaration_sha256 = extraction.overlay.sha256
                    if extraction.status is RuntimeHistoryExtractionStatusV1.NO_HISTORY:
                        no_history_record: R24CoordinatedCallRecordV1 | None = None
                        if type(self._policy) is OwnerAuthorizedLivePerCallPolicyV1:
                            prepared = self._evaluate_policy_with_timeout(
                                request=raw,
                                context=context,
                                history_ir=None,
                                timeout_ms=config.policy_timeout_ms,
                                evaluation_started=policy_evaluation_started,
                                no_history=True,
                            )
                            if type(prepared) is not R24CoordinatedCallRecordV1:
                                raise SentinelContractError(
                                    "no-history rubric preparation returned an untrusted record"
                                )
                            no_history_record = prepared
                        base_result = self._fallback_result(
                            raw=raw,
                            raw_json=raw_json,
                            raw_sha256=raw_sha256,
                            context=context,
                            config=config,
                            role=role,
                            history_codec_id=history_codec_id,
                            reason=SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                            check="r2_4_no_history_r21_v1_compatibility",
                            started=started,
                            transaction=receipt_transaction,
                            policy_evaluated=False,
                            no_history_record=no_history_record,
                        )
                        return RuntimeVerticalSentinelResultV1(
                            base_result=base_result,
                            bridge=RuntimeVerticalReceiptBridgeV1.no_history(
                                context.logical_call_id
                            ),
                            overlay_declaration_sha256=overlay_declaration_sha256,
                        )
                    if extraction.status is RuntimeHistoryExtractionStatusV1.UNSUPPORTED:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.UNSUPPORTED_HISTORY_FAMILY,
                            "r2_4_runtime_history_shape_unsupported",
                        )
                    if extraction.history_ir is None:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                            "r2_4_runtime_extraction_invalid",
                        )
                    ir = extraction.history_ir
                else:
                    ir = codec.extract(copy_json(raw))
            except PortableContractError as exc:
                reason = (
                    SentinelFallbackReason.AMBIGUOUS_HISTORY_SPAN
                    if exc.code
                    in {
                        "TARGET_BINDING_AMBIGUOUS",
                        "OVERLAPPING_HISTORY_SPAN",
                    }
                    else SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
                )
                raise _EvaluationFailure(reason, "history_extract_failed") from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_extract_exception",
                ) from exc
            try:
                ir = _snapshot_history_ir(ir)
            except _EvaluationFailure:
                raise
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_ir_snapshot_exception",
                ) from exc
            try:
                self._validate_extracted_ir(
                    raw=raw,
                    context=context,
                    history_codec_id=history_codec_id,
                    config=config,
                    codec=codec,
                    ir=ir,
                )
            except PortableContractError as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_ir_validation_failed",
                ) from exc
            except Exception as exc:
                raise _EvaluationFailure(
                    SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE,
                    "history_ir_validation_exception",
                ) from exc
            history_extract_ns = time.monotonic_ns() - history_extract_started_ns

            policy_started_ns = time.monotonic_ns()
            output = self._evaluate_policy_with_timeout(
                request=raw,
                context=context,
                history_ir=ir,
                timeout_ms=config.policy_timeout_ms,
                evaluation_started=policy_evaluation_started,
            )
            policy_ns = time.monotonic_ns() - policy_started_ns
            candidate = copy_json(raw)
            legacy_render_result: RenderResult | None = None
            runtime_render_result: RuntimeRenderResultV1 | None = None
            vertical_render_result: RuntimeVerticalRenderResultV1 | None = None
            vertical_output: RuntimeVerticalPolicyOutputV1 | None = None
            decision_kinds: tuple[SentinelDecisionKind, ...]
            checks = ["request_schema", "codec_extract"]
            if self._runtime_vertical_policy:
                try:
                    vertical_output_snapshot = _snapshot_vertical_policy_output(output)
                    policy_output_sha256 = vertical_output_snapshot.sha256
                    vertical_output = vertical_output_snapshot.output
                    if overlay_declaration_sha256 is None:
                        raise SentinelContractError(
                            "R2.4 vertical output has no runtime overlay binding"
                        )
                    self._validate_vertical_output_binding(
                        raw_sha256=raw_sha256,
                        context=context,
                        history_ir=ir,
                        output=vertical_output,
                    )
                    render_started_ns = time.monotonic_ns()
                    vertical_render_result = render_vertical_admitted_plan(
                        raw,
                        ir,
                        vertical_output.admitted_plan,
                    )
                    render_ns = time.monotonic_ns() - render_started_ns
                    validator_started_ns = time.monotonic_ns()
                    validate_vertical_render_result(
                        raw,
                        ir,
                        vertical_output.admitted_plan,
                        vertical_render_result,
                    )
                    validator_ns = time.monotonic_ns() - validator_started_ns
                except _EvaluationFailure:
                    raise
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                        "r2_4_runtime_output_or_render_invalid",
                    ) from exc
                candidate = vertical_render_result.candidate_request
                decision_kinds = vertical_output.receipt_decision_kinds
                if not decision_kinds:
                    base_result = self._fallback_result(
                        raw=raw,
                        raw_json=raw_json,
                        raw_sha256=raw_sha256,
                        context=context,
                        config=config,
                        role=role,
                        history_codec_id=history_codec_id,
                        reason=SentinelFallbackReason.INVARIANT_FAILURE,
                        check="r2_4_zero_target_r21_v1_compatibility",
                        started=started,
                        policy_output_sha256=policy_output_sha256,
                        transaction=receipt_transaction,
                        policy_evaluated=True,
                    )
                    return RuntimeVerticalSentinelResultV1(
                        base_result=base_result,
                        bridge=RuntimeVerticalReceiptBridgeV1.from_policy_output(
                            context.logical_call_id,
                            vertical_output,
                        ),
                        overlay_declaration_sha256=overlay_declaration_sha256,
                    )
                checks.extend(
                    dict.fromkeys(
                        (
                            "r2_4_runtime_output_schema",
                            *vertical_output.validation_checks,
                            *vertical_render_result.validation_checks,
                            "r2_4_execution_authority_bound",
                            "r2_4_runtime_codec_overlay_bound",
                            "caller_input_immutable",
                        )
                    )
                )
            elif self._runtime_evidence_policy:
                try:
                    runtime_output_snapshot = _snapshot_runtime_policy_output(output)
                    policy_output_sha256 = runtime_output_snapshot.sha256
                    runtime_output = runtime_output_snapshot.output
                except _EvaluationFailure:
                    raise
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                        "r2_2_policy_output_validation_exception",
                    ) from exc
                if not runtime_output.decisions:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVARIANT_FAILURE,
                        "r2_2_zero_target_r21_v1_unrepresentable",
                    )
                try:
                    runtime_render_result = render_runtime_admitted_plan(
                        raw,
                        ir,
                        runtime_output.admitted_plan,
                    )
                    validate_runtime_render_result(
                        raw,
                        ir,
                        runtime_output.admitted_plan,
                        runtime_render_result,
                    )
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVARIANT_FAILURE,
                        "r2_2_runtime_render_invalid",
                    ) from exc
                candidate = runtime_render_result.candidate_request
                decision_kinds = tuple(
                    SentinelDecisionKind(item.proposed_operation.value)
                    for item in runtime_output.decisions
                )
                checks.extend(
                    dict.fromkeys(
                        (
                            "r2_2_runtime_output_schema",
                            *runtime_output.validation_checks,
                            *runtime_render_result.validation_checks,
                            "r2_2_shadow_only",
                            "caller_input_immutable",
                        )
                    )
                )
            else:
                try:
                    policy_output_bytes = canonical_json_bytes(
                        _policy_output_canonical_view(output)
                    )
                    policy_output_sha256 = sha256(policy_output_bytes).hexdigest()
                    output_snapshot = _snapshot_policy_output(
                        output,
                        canonical_bytes=policy_output_bytes,
                        canonical_digest=policy_output_sha256,
                    )
                    legacy_output = output_snapshot.output
                    self._validate_policy_output_admission(legacy_output)
                    if legacy_output.transformation_plan is None:
                        self._validate_no_plan_decisions(legacy_output)
                    else:
                        self._validate_decision_plan_binding(legacy_output)
                        validate_plan(raw, ir, legacy_output.transformation_plan)
                except _EvaluationFailure:
                    raise
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                        "policy_output_validation_exception",
                    ) from exc
                checks.append("policy_output_schema")
                decision_kinds = tuple(item.kind for item in legacy_output.decisions)
                if legacy_output.transformation_plan is not None:
                    try:
                        expected_render_result = render_request(
                            copy_json(raw),
                            ir,
                            legacy_output.transformation_plan,
                            execution_mode=ExecutionMode.RUNTIME,
                            failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
                        )
                    except Exception as exc:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.INVARIANT_FAILURE,
                            "independent_render_exception",
                        ) from exc
                    try:
                        observed_render_result = codec.render(
                            copy_json(raw),
                            deepcopy(ir),
                            deepcopy(legacy_output.transformation_plan),
                            execution_mode=ExecutionMode.RUNTIME,
                            failure_policy=FailurePolicy.FAIL_OPEN_ORIGINAL,
                        )
                    except PortableContractError as exc:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.RENDERER_FAILURE,
                            "render_failed",
                        ) from exc
                    except Exception as exc:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.RENDERER_FAILURE,
                            "render_exception",
                        ) from exc
                    try:
                        if canonical_json_bytes(
                            _render_result_canonical_view(observed_render_result)
                        ) != canonical_json_bytes(
                            _render_result_canonical_view(expected_render_result)
                        ):
                            raise SentinelContractError(
                                "renderer result differs from precomputed G1.2 result"
                            )
                        self._validate_render_result(raw, expected_render_result)
                    except (PortableContractError, SentinelContractError) as exc:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.INVARIANT_FAILURE,
                            "invariant_validation_failed",
                        ) from exc
                    except Exception as exc:
                        raise _EvaluationFailure(
                            SentinelFallbackReason.INVARIANT_FAILURE,
                            "invariant_validation_exception",
                        ) from exc
                    legacy_render_result = expected_render_result
                    candidate = copy_json(legacy_render_result.rendered_request)
                    checks.extend(
                        (
                            "g1_2_exact_span_render",
                            "independent_render_recomputed",
                            "reversible_source_mapping",
                            "caller_input_immutable",
                        )
                    )
                else:
                    checks.append("no_transform_proposed")

            if canonical_sha256(request) != raw_sha256:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVARIANT_FAILURE,
                    "caller_input_mutated",
                )
            current_kill_switch_active, current_kill_switch_generation = (
                self._global_switch.snapshot()
            )
            if (
                current_kill_switch_active
                or current_kill_switch_generation != kill_switch_generation
            ):
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVARIANT_FAILURE,
                    "global_kill_switch_activated_during_evaluation",
                )
            candidate_json = canonical_json_bytes(candidate)
            candidate_sha256 = canonical_sha256(candidate)
            would_edit = candidate_sha256 != raw_sha256
            edit_applied = config.mode is SentinelMode.ACTIVE and would_edit
            final = candidate if edit_applied else raw
            final_json = canonical_json_bytes(final)
            diff_sha256 = (
                vertical_render_result.exact_diff_sha256
                if vertical_render_result is not None
                else (
                    runtime_render_result.exact_diff_sha256
                    if runtime_render_result is not None
                    else self._diff_sha256(legacy_render_result)
                )
            )
            audit_prepared = False
            if self._runtime_vertical_policy and self._runtime_audit is not None:
                assert extraction is not None
                assert vertical_output is not None
                assert vertical_render_result is not None
                try:
                    self._runtime_audit.begin_pre_provider(
                        logical_call_id=context.logical_call_id,
                        raw_request=raw,
                        extraction=extraction,
                        policy_output=vertical_output,
                        render_result=vertical_render_result,
                        configured_mode=config.mode,
                        effective_mode=config.mode,
                        final_request=final,
                        history_extract_ns=history_extract_ns,
                        policy_ns=policy_ns,
                        render_ns=render_ns,
                        validator_ns=validator_ns,
                        pre_provider_total_ns=time.monotonic_ns() - audit_started_ns,
                    )
                    audit_prepared = True
                except Exception as exc:
                    raise _EvaluationFailure(
                        SentinelFallbackReason.SIDECAR_FAILURE,
                        "r2_4_audit_pre_provider_failed",
                    ) from exc
            receipt = SentinelReceipt(
                logical_call_id=context.logical_call_id,
                host_id=context.host_id,
                call_role=role,
                configured_mode=config.mode,
                effective_mode=config.mode,
                bypass_reason=None,
                global_kill_switch_active=False,
                history_codec_id=history_codec_id,
                history_codec_contract_version=config.history_codec_contract_version,
                policy_id=self._policy_id,
                policy_output_sha256=policy_output_sha256,
                raw_request_sha256=raw_sha256,
                candidate_request_sha256=candidate_sha256,
                final_request_sha256=canonical_sha256(final),
                exact_diff_sha256=diff_sha256,
                decision_kinds=decision_kinds,
                policy_evaluated=True,
                would_edit=would_edit,
                edit_applied=edit_applied,
                fallback_reason=None,
                validation_status=SentinelValidationStatus.PASSED,
                validation_checks=tuple(checks),
                latency_ns=self._elapsed(started),
            )
            base_result = self._finalize(
                receipt=receipt,
                raw_json=raw_json,
                candidate_json=candidate_json,
                final_json=final_json,
                fallback_context=(raw, context, config, role, history_codec_id, started),
                transaction=receipt_transaction,
            )
            if audit_prepared and (
                base_result.receipt.validation_status is not SentinelValidationStatus.PASSED
            ):
                assert self._runtime_audit is not None
                self._runtime_audit.cancel(context.logical_call_id)
            if self._runtime_vertical_policy:
                assert vertical_output is not None
                return RuntimeVerticalSentinelResultV1(
                    base_result=base_result,
                    bridge=RuntimeVerticalReceiptBridgeV1.from_policy_output(
                        context.logical_call_id,
                        vertical_output,
                    ),
                    overlay_declaration_sha256=overlay_declaration_sha256,
                )
            return base_result
        except _EvaluationFailure as failure:
            if self._runtime_audit is not None:
                self._runtime_audit.cancel(context.logical_call_id)
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=failure.reason,
                check=failure.check,
                started=started,
                policy_output_sha256=policy_output_sha256,
                transaction=receipt_transaction,
                policy_evaluated=policy_evaluation_started.is_set(),
            )
        except Exception:
            if self._runtime_audit is not None:
                self._runtime_audit.cancel(context.logical_call_id)
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=raw_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.INVARIANT_FAILURE,
                check="internal_evaluation_exception",
                started=started,
                policy_output_sha256=policy_output_sha256,
                transaction=receipt_transaction,
                policy_evaluated=policy_evaluation_started.is_set(),
            )

    def _evaluate_policy_with_timeout(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: Any,
        timeout_ms: int,
        evaluation_started: Event,
        no_history: bool = False,
    ) -> (
        SentinelPolicyOutput
        | RuntimeSentinelPolicyOutputV1
        | RuntimeVerticalPolicyOutputV1
        | R24CoordinatedCallRecordV1
    ):
        """Run the replaceable backend behind a real, bounded daemon wait."""

        finished = Event()
        cancel_before_policy = Event()
        policy_start_gate = Lock()
        outcome: list[tuple[bool, Any]] = []
        policy_started = self._clock_ns()
        execution_fence = (
            _PolicyExecutionFence(
                deadline_ns=policy_started + timeout_ms * 1_000_000,
                clock_ns=self._clock_ns,
            )
            if self._runtime_evidence_policy or self._runtime_vertical_policy
            else None
        )
        thread_context = copy_context()
        policy_request = copy_json(request)
        policy_call_context = SentinelContext(
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            attributes=cast(dict[str, JsonValue], copy_json(context.attributes)),
        )
        policy_history_ir = None if no_history else deepcopy(history_ir)

        def evaluate() -> None:
            try:
                with policy_start_gate:
                    if cancel_before_policy.is_set():
                        return
                    evaluation_started.set()
                value: (
                    SentinelPolicyOutput
                    | RuntimeSentinelPolicyOutputV1
                    | RuntimeVerticalPolicyOutputV1
                    | R24CoordinatedCallRecordV1
                )
                if no_history:
                    if (
                        execution_fence is None
                        or type(self._policy) is not OwnerAuthorizedLivePerCallPolicyV1
                    ):
                        raise SentinelContractError(
                            "no-history rubric execution requires the exact production policy"
                        )
                    value = self._policy.prepare_no_history_with_control(
                        request=policy_request,
                        context=policy_call_context,
                        execution_control=execution_fence,
                    )
                elif execution_fence is None:
                    value = self._policy.evaluate(
                        request=policy_request,
                        context=policy_call_context,
                        history_ir=policy_history_ir,
                    )
                elif self._runtime_evidence_policy:
                    assert policy_history_ir is not None
                    runtime_policy = cast(RuntimeEvidencePolicyV1, self._policy)
                    value = runtime_policy.evaluate_with_control(
                        request=policy_request,
                        context=policy_call_context,
                        history_ir=policy_history_ir,
                        execution_control=execution_fence,
                    )
                else:
                    vertical_policy = cast(RuntimeVerticalPolicy, self._policy)
                    assert execution_fence is not None
                    assert policy_history_ir is not None
                    value = vertical_policy.evaluate_with_control(
                        request=policy_request,
                        context=policy_call_context,
                        history_ir=policy_history_ir,
                        execution_control=execution_fence,
                    )
            except BaseException as error:
                outcome.append((False, error))
            else:
                outcome.append((True, value))
            finally:
                finished.set()

        worker = Thread(
            target=thread_context.run,
            args=(evaluate,),
            name="mobileworld-prompt-sentinel-policy",
            daemon=True,
        )
        worker.start()
        if not finished.wait(timeout_ms / 1000):
            with policy_start_gate:
                cancel_before_policy.set()
            if execution_fence is not None:
                execution_fence.cancel()
            if type(self._policy) is OwnerAuthorizedLivePerCallPolicyV1:
                # This exact module-owned policy can prepare more than one child
                # (rubric then history).  Do not expose Original until a child
                # prepared concurrently with cancellation has observed the
                # closed fence, published its terminal receipt, and been joined.
                worker.join()
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_TIMEOUT,
                "policy_deadline_exceeded",
            )
        policy_elapsed = self._clock_ns() - policy_started
        if policy_elapsed > timeout_ms * 1_000_000:
            if execution_fence is not None:
                execution_fence.cancel()
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_TIMEOUT,
                "policy_latency_budget_exceeded",
            )
        if not outcome:
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_EXCEPTION,
                "policy_worker_terminated_without_result",
            )
        succeeded, value = outcome[0]
        if not succeeded:
            if isinstance(value, TimeoutError):
                raise _EvaluationFailure(
                    SentinelFallbackReason.POLICY_TIMEOUT,
                    "policy_timeout_exception",
                )
            raise _EvaluationFailure(
                SentinelFallbackReason.POLICY_EXCEPTION,
                "policy_exception",
            )
        return cast(
            SentinelPolicyOutput
            | RuntimeSentinelPolicyOutputV1
            | RuntimeVerticalPolicyOutputV1
            | R24CoordinatedCallRecordV1,
            value,
        )

    @staticmethod
    def _validate_extracted_ir(
        *,
        raw: JsonValue,
        context: SentinelContext,
        history_codec_id: str,
        config: SentinelHostConfig,
        codec: HistoryCodec,
        ir: Any,
    ) -> None:
        if not isinstance(ir, HistoryIR):
            raise PortableContractError(
                "HISTORY_IR_TYPE_MISMATCH",
                "codec extraction must return the canonical HistoryIR type",
            )
        validate_history_ir(raw, ir)
        if (
            ir.host_id != context.host_id
            or ir.codec_id != history_codec_id
            or ir.codec_contract_version != config.history_codec_contract_version
            or ir.history_family is not codec.history_family
            or ir.capabilities != codec.capabilities
        ):
            raise PortableContractError(
                "HISTORY_IR_BINDING_MISMATCH",
                "extracted IR differs from the selected host or codec declaration",
            )

    @staticmethod
    def _validate_request_schema(request: JsonValue) -> None:
        if not isinstance(request, dict):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_root_not_object"
            )
        if not isinstance(request.get("model"), str) or not request["model"]:
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_model_missing"
            )
        if not isinstance(request.get("messages"), list):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_REQUEST_SCHEMA, "request_messages_missing"
            )

    def _validate_vertical_output_binding(
        self,
        *,
        raw_sha256: str,
        context: SentinelContext,
        history_ir: HistoryIR,
        output: RuntimeVerticalPolicyOutputV1,
    ) -> None:
        """Bind an admitted R2.4 output to this exact actor call and authority."""

        if type(output) is not RuntimeVerticalPolicyOutputV1:
            raise SentinelContractError("R2.4 policy output type is untrusted")
        if type(self._policy) not in {
            R22CpuFakeActivePolicyAdapter,
            R22OwnerAuthorizedLivePolicyAdapter,
            OwnerAuthorizedLivePerCallPolicyV1,
        }:
            raise SentinelContractError("R2.4 policy adapter type is untrusted")
        vertical_policy = cast(RuntimeVerticalPolicy, self._policy)
        policy_object = cast(object, self._policy)
        if type(policy_object) is OwnerAuthorizedLivePerCallPolicyV1:
            per_call_policy = policy_object
            call_binding = per_call_policy.call_binding(context.logical_call_id)
            if (
                call_binding.actor_request_sha256 != raw_sha256
                or call_binding.output_sha256 != vertical_output_sha256(output)
                or call_binding.policy_id != self._policy_id
            ):
                raise SentinelContractError("R2.4 per-call authority binds another output")
            source_descriptor_sha256 = call_binding.source_transport_descriptor_sha256
            source_transport_binding_sha256 = call_binding.source_transport_binding_sha256
            execution_authority_sha256 = call_binding.execution_authority_sha256
            execution_scope = RuntimeVerticalExecutionScope.OWNER_AUTHORIZED_LIVE_ACTIVE
        else:
            source_descriptor_sha256 = vertical_policy.source_transport_descriptor_sha256
            source_transport_binding_sha256 = vertical_policy.source_transport_binding_sha256
            execution_scope = vertical_policy.execution_scope
            execution_authority_sha256 = vertical_policy.execution_authority_sha256
        plan = output.admitted_plan
        if (
            output.policy_id != self._policy_id
            or output.execution_scope is not execution_scope
            or plan.execution_scope is not execution_scope
            or output.execution_authority_sha256 != execution_authority_sha256
            or plan.execution_authority_sha256 != execution_authority_sha256
            or output.source_transport_descriptor_sha256 != source_descriptor_sha256
            or plan.source_transport_descriptor_sha256 != source_descriptor_sha256
            or output.source_transport_binding_sha256 != source_transport_binding_sha256
            or plan.source_transport_binding_sha256 != source_transport_binding_sha256
            or plan.logical_call_id != context.logical_call_id
            or plan.host_id != context.host_id
            or plan.source_request_sha256 != raw_sha256
            or plan.history_family != history_ir.history_family.value
            or plan.history_codec_id != history_ir.codec_id
            or plan.history_codec_contract_version != history_ir.codec_contract_version
        ):
            raise SentinelContractError("R2.4 output binds another actor request or History IR")

    @staticmethod
    def _validate_policy_output_admission(output: SentinelPolicyOutput) -> None:
        if len({item.decision_id for item in output.decisions}) != len(output.decisions):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT, "duplicate_decision_id"
            )

    @staticmethod
    def _validate_no_plan_decisions(output: SentinelPolicyOutput) -> None:
        if any(
            item.kind not in {SentinelDecisionKind.KEEP, SentinelDecisionKind.KEEP_UNCERTAIN}
            or item.operation_id is not None
            for item in output.decisions
        ):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "no_plan_decision_requires_keep_or_abstain",
            )

    @staticmethod
    def _validate_decision_plan_binding(output: SentinelPolicyOutput) -> None:
        plan = output.transformation_plan
        assert plan is not None
        if any(item.kind is OperationKind.REPLACE for item in plan.operations):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "r2_1_replace_requires_later_runtime_plan_overlay",
            )
        if any(
            item.operation_id is None
            and item.kind
            not in {
                SentinelDecisionKind.KEEP,
                SentinelDecisionKind.KEEP_UNCERTAIN,
            }
            for item in output.decisions
        ):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "material_decision_missing_operation_id",
            )
        operations = {item.operation_id: item for item in plan.operations}
        decision_operations = {
            item.operation_id: item for item in output.decisions if item.operation_id is not None
        }
        bound_decision_count = sum(item.operation_id is not None for item in output.decisions)
        if len(decision_operations) != bound_decision_count:
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "duplicate_decision_operation_binding",
            )
        if set(operations) != set(decision_operations):
            raise _EvaluationFailure(
                SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                "decision_operation_census_mismatch",
            )
        kind_map = {
            OperationKind.KEEP: SentinelDecisionKind.KEEP,
            OperationKind.DROP: SentinelDecisionKind.DROP,
            OperationKind.REPLACE: SentinelDecisionKind.REPLACE,
            OperationKind.KEEP_UNCERTAIN: SentinelDecisionKind.KEEP_UNCERTAIN,
        }
        for operation_id, operation in operations.items():
            expected = kind_map.get(operation.kind)
            decision = decision_operations[operation_id]
            if expected is None or decision.kind is not expected:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                    "decision_operation_kind_mismatch",
                )
            if decision.record_id != operation.target_record_id:
                raise _EvaluationFailure(
                    SentinelFallbackReason.INVALID_POLICY_OUTPUT,
                    "decision_operation_record_mismatch",
                )

    @staticmethod
    def _validate_render_result(
        raw: JsonValue,
        result: RenderResult,
    ) -> None:
        if result.list_insertions:
            raise SentinelContractError(
                "R2.1 history-only runtime cannot insert current-observation blocks"
            )
        if result.fallback_state is not FallbackState.NOT_NEEDED:
            raise SentinelContractError("renderer returned a fallback instead of a candidate")
        if result.original_request != raw or result.source_request_sha256 != canonical_sha256(raw):
            raise SentinelContractError("renderer source binding differs from raw request")
        if result.rendered_request_sha256 != canonical_sha256(result.rendered_request):
            raise SentinelContractError("renderer final hash is invalid")
        if restore_original(result) != raw:
            raise SentinelContractError("renderer mapping cannot restore Original")

    @staticmethod
    def _diff_sha256(result: RenderResult | None) -> str:
        if result is None:
            return _EMPTY_DIFF_SHA256
        view = _render_result_canonical_view(result)
        return canonical_sha256(
            {
                "diffs": view["diffs"],
                "list_insertions": view["list_insertions"],
            }
        )

    def _bypass_result(
        self,
        *,
        raw: JsonValue,
        raw_json: bytes,
        raw_sha256: str,
        context: SentinelContext,
        config: SentinelHostConfig,
        role: SentinelCallRole,
        history_codec_id: str | None,
        bypass_reason: SentinelBypassReason,
        kill_switch_active: bool,
        started: int,
        persist: bool = True,
    ) -> SentinelResult:
        receipt = SentinelReceipt(
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            call_role=role,
            configured_mode=config.mode,
            effective_mode=SentinelMode.OFF,
            bypass_reason=bypass_reason,
            global_kill_switch_active=kill_switch_active,
            history_codec_id=history_codec_id,
            history_codec_contract_version=(
                None if history_codec_id is None else config.history_codec_contract_version
            ),
            policy_id=None,
            policy_output_sha256=_EMPTY_POLICY_OUTPUT_SHA256,
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=_EMPTY_DIFF_SHA256,
            decision_kinds=(),
            policy_evaluated=False,
            would_edit=False,
            edit_applied=False,
            fallback_reason=None,
            validation_status=SentinelValidationStatus.BYPASSED,
            validation_checks=(bypass_reason.value,),
            latency_ns=self._elapsed(started),
        )
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=raw_json,
            _final_request_json=raw_json,
        )
        finalized = (
            result
            if not persist
            else self._finalize(
                receipt=receipt,
                raw_json=raw_json,
                candidate_json=raw_json,
                final_json=raw_json,
                fallback_context=(raw, context, config, role, history_codec_id, started),
                transaction=None,
            )
        )
        if (
            type(self._runtime_audit) is ProductionRuntimeAuditV1
            and role is SentinelCallRole.ACTOR
            and config.mode in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
        ):
            self._runtime_audit.begin_bypass_pre_provider(
                logical_call_id=context.logical_call_id,
                host_id=context.host_id,
                raw_request=raw,
                result=finalized,
                pre_provider_total_ns=finalized.receipt.latency_ns,
            )
        return finalized

    def bypass_reuse(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
        prior_receipt: SentinelReceipt,
    ) -> SentinelResult:
        """Bind the current Original without semantic work after a cached bypass."""

        if prior_receipt.bypass_reason is None:
            raise SentinelContractError("bypass reuse requires a prior bypass receipt")
        started = self._clock_ns()
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        return self._bypass_result(
            raw=raw,
            raw_json=raw_json,
            raw_sha256=canonical_sha256(raw),
            context=context,
            config=self.host_config(context.host_id),
            role=call_role,
            history_codec_id=history_codec_id,
            bypass_reason=prior_receipt.bypass_reason,
            kill_switch_active=prior_receipt.global_kill_switch_active,
            started=started,
            persist=False,
        )

    def _fallback_result(
        self,
        *,
        raw: JsonValue,
        raw_json: bytes,
        raw_sha256: str,
        context: SentinelContext,
        config: SentinelHostConfig,
        role: SentinelCallRole,
        history_codec_id: str | None,
        reason: SentinelFallbackReason,
        check: str,
        started: int,
        policy_evaluated: bool,
        persist: bool = True,
        policy_output_sha256: str = _EMPTY_POLICY_OUTPUT_SHA256,
        transaction: SentinelReceiptTransaction | None = None,
        no_history_record: R24CoordinatedCallRecordV1 | None = None,
    ) -> SentinelResult:
        receipt = SentinelReceipt(
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            call_role=role,
            configured_mode=config.mode,
            effective_mode=SentinelMode.OFF,
            bypass_reason=None,
            global_kill_switch_active=self._global_switch.active,
            history_codec_id=history_codec_id,
            history_codec_contract_version=(
                None if history_codec_id is None else config.history_codec_contract_version
            ),
            policy_id=self._policy_id,
            policy_output_sha256=policy_output_sha256,
            raw_request_sha256=raw_sha256,
            candidate_request_sha256=raw_sha256,
            final_request_sha256=raw_sha256,
            exact_diff_sha256=_EMPTY_DIFF_SHA256,
            decision_kinds=(),
            policy_evaluated=policy_evaluated,
            would_edit=False,
            edit_applied=False,
            fallback_reason=reason,
            validation_status=SentinelValidationStatus.FALLBACK_ORIGINAL,
            validation_checks=(self._safe_check_code(check),),
            latency_ns=self._elapsed(started),
        )
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=raw_json,
            _final_request_json=raw_json,
        )
        finalized = (
            result
            if not persist
            else self._finalize(
                receipt=receipt,
                raw_json=raw_json,
                candidate_json=raw_json,
                final_json=raw_json,
                fallback_context=(raw, context, config, role, history_codec_id, started),
                transaction=transaction,
            )
        )
        if (
            type(self._runtime_audit) is ProductionRuntimeAuditV1
            and role is SentinelCallRole.ACTOR
            and config.mode in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
        ):
            safe_check = (
                finalized.receipt.validation_checks[0]
                if finalized.receipt.validation_checks
                else "fallback_check_missing"
            )
            if (
                no_history_record is not None
                and finalized.receipt.fallback_reason
                is SentinelFallbackReason.HISTORY_EXTRACTION_FAILURE
                and safe_check == "r2_4_no_history_r21_v1_compatibility"
            ):
                self._runtime_audit.begin_no_history_pre_provider(
                    logical_call_id=context.logical_call_id,
                    host_id=context.host_id,
                    raw_request=raw,
                    result=finalized,
                    coordinated=no_history_record,
                    fallback_check=safe_check,
                    pre_provider_total_ns=finalized.receipt.latency_ns,
                )
            else:
                self._runtime_audit.begin_fallback_pre_provider(
                    logical_call_id=context.logical_call_id,
                    host_id=context.host_id,
                    raw_request=raw,
                    result=finalized,
                    fallback_check=safe_check,
                    pre_provider_total_ns=finalized.receipt.latency_ns,
                )
        return finalized

    def _finalize(
        self,
        *,
        receipt: SentinelReceipt,
        raw_json: bytes,
        candidate_json: bytes,
        final_json: bytes,
        fallback_context: tuple[
            JsonValue,
            SentinelContext,
            SentinelHostConfig,
            SentinelCallRole,
            str | None,
            int,
        ],
        transaction: SentinelReceiptTransaction | None,
    ) -> SentinelResult:
        result = SentinelResult(
            receipt=receipt,
            _raw_request_json=raw_json,
            _candidate_request_json=candidate_json,
            _final_request_json=final_json,
        )
        if self._receipt_sink is None:
            return result
        publication_receipt = SentinelReceipt(
            **{item.name: getattr(receipt, item.name) for item in fields(SentinelReceipt)}
        )
        publication_receipt_json = canonical_json_bytes(
            SentinelReceipt.to_dict(publication_receipt)
        )
        selected_transaction = transaction
        try:
            if selected_transaction is None:
                selected_transaction = self._begin_receipt_transaction(receipt.logical_call_id)
            selected_transaction.commit(publication_receipt)
            if (
                canonical_json_bytes(SentinelReceipt.to_dict(publication_receipt))
                != publication_receipt_json
            ):
                raise SentinelContractError("receipt transaction mutated its detached input")
        except Exception:
            if selected_transaction is not None:
                try:
                    selected_transaction.abort()
                except Exception:
                    pass
            if receipt.validation_status is SentinelValidationStatus.BYPASSED:
                return result
            raw, context, config, role, history_codec_id, started = fallback_context
            return self._fallback_result(
                raw=raw,
                raw_json=raw_json,
                raw_sha256=receipt.raw_request_sha256,
                context=context,
                config=config,
                role=role,
                history_codec_id=history_codec_id,
                reason=SentinelFallbackReason.SIDECAR_FAILURE,
                check="sidecar_commit_failed",
                started=started,
                persist=False,
                policy_evaluated=receipt.policy_evaluated,
                policy_output_sha256=receipt.policy_output_sha256,
            )
        return result

    def _begin_receipt_transaction(self, logical_call_id: str) -> SentinelReceiptTransaction:
        sink = self._receipt_sink
        if sink is None:
            raise RuntimeError("Sentinel receipt sink is unavailable")
        begin = getattr(sink, "begin", None)
        if not callable(begin):
            raise TypeError("Sentinel receipt sink has no admission boundary")
        transaction = begin(logical_call_id)
        if not isinstance(transaction, SentinelReceiptTransaction):
            raise TypeError("Sentinel receipt sink returned an invalid transaction")
        return transaction

    def request_drift_fallback(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
        policy_evaluated: bool,
        policy_output_sha256: str,
    ) -> SentinelResult:
        started = self._clock_ns()
        raw = copy_json(request)
        raw_json = canonical_json_bytes(raw)
        return self._fallback_result(
            raw=raw,
            raw_json=raw_json,
            raw_sha256=canonical_sha256(raw),
            context=context,
            config=self.host_config(context.host_id),
            role=call_role,
            history_codec_id=history_codec_id,
            reason=SentinelFallbackReason.REQUEST_DRIFT,
            check="cached_raw_request_sha256_mismatch",
            started=started,
            policy_evaluated=policy_evaluated,
            persist=False,
            policy_output_sha256=policy_output_sha256,
        )

    def _elapsed(self, started: int) -> int:
        value = self._clock_ns() - started
        return max(0, int(value))

    @staticmethod
    def _safe_check_code(check: Any) -> str:
        if isinstance(check, str) and _CHECK_CODE.fullmatch(check) is not None:
            return check
        return "unsafe_external_check_code_redacted"


class SentinelLogicalCall:
    """Short-lived evaluate-once cache for one already assembled actor request."""

    def __init__(
        self,
        *,
        sentinel: PromptSentinel,
        context: SentinelContext,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
    ) -> None:
        if not isinstance(call_role, SentinelCallRole):
            raise TypeError("call_role must be SentinelCallRole")
        self._sentinel = sentinel
        self._context = context
        self._history_codec_id = history_codec_id
        self._call_role = call_role
        self._result: SentinelResult | RuntimeVerticalSentinelResultV1 | None = None
        self._provider_attempt_count = 0
        self._provider_latency_ns = 0
        self._provider_response_id: str | None = None
        self._provider_model_id: str | None = None
        self._provider_finish_reason: str | None = None
        self._provider_input_tokens: int | None = None
        self._provider_output_tokens: int | None = None
        self._provider_total_tokens: int | None = None
        self._runtime_audit_finalized = False
        self._lock = Lock()

    @property
    def sentinel(self) -> PromptSentinel:
        return self._sentinel

    @property
    def context(self) -> SentinelContext:
        return self._context

    @property
    def history_codec_id(self) -> str | None:
        return self._history_codec_id

    @property
    def call_role(self) -> SentinelCallRole:
        return self._call_role

    @property
    def requires_strict_provider_audit(self) -> bool:
        return self._sentinel.strict_provider_audit

    @property
    def result(self) -> SentinelResult | RuntimeVerticalSentinelResultV1 | None:
        with self._lock:
            if type(self._result) is RuntimeVerticalSentinelResultV1:
                return snapshot_vertical_sentinel_result(self._result)
            return self._result

    def before_model_call(
        self, request: JsonValue
    ) -> SentinelResult | RuntimeVerticalSentinelResultV1:
        _require_canonical_json_domain(request)
        request_sha256 = canonical_sha256(request)
        with self._lock:
            if self._result is not None:
                if self._result.receipt.raw_request_sha256 == request_sha256:
                    if type(self._result) is RuntimeVerticalSentinelResultV1:
                        return snapshot_vertical_sentinel_result(self._result)
                    return self._result
                if self._result.receipt.validation_status is SentinelValidationStatus.BYPASSED:
                    return self._sentinel.bypass_reuse(
                        request=request,
                        context=self._context,
                        history_codec_id=self._history_codec_id,
                        call_role=self._call_role,
                        prior_receipt=self._result.receipt,
                    )
                return self._sentinel.request_drift_fallback(
                    request=request,
                    context=self._context,
                    history_codec_id=self._history_codec_id,
                    call_role=self._call_role,
                    policy_evaluated=self._result.receipt.policy_evaluated,
                    policy_output_sha256=self._result.receipt.policy_output_sha256,
                )
            result = self._sentinel.before_model_call(
                request,
                self._context,
                self._history_codec_id,
                self._call_role,
            )
            if type(result) is RuntimeVerticalSentinelResultV1:
                self._result = snapshot_vertical_sentinel_result(result)
                return snapshot_vertical_sentinel_result(self._result)
            self._result = result
            return result

    def record_actor_provider_attempt(
        self,
        *,
        latency_ns: int,
        succeeded: bool,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        raw_response: Any = None,
        collector_terminal_locator: JsonValue | None = None,
    ) -> None:
        """Accumulate safe provider metadata without retaining provider content."""

        if type(latency_ns) is not int or latency_ns < 0 or type(succeeded) is not bool:
            raise TypeError("provider attempt metadata is invalid")
        for text_value in (response_id, model_id, finish_reason):
            if text_value is not None and type(text_value) is not str:
                raise TypeError("provider text metadata must use exact strings")
        for count_value in (input_tokens, output_tokens, total_tokens):
            if count_value is not None and (type(count_value) is not int or count_value < 0):
                raise TypeError("provider token metadata must be non-negative integers")
        self._sentinel.record_production_actor_provider_attempt(
            logical_call_id=self._context.logical_call_id,
            succeeded=succeeded,
            latency_ns=latency_ns,
            raw_response=raw_response,
            collector_terminal_locator=collector_terminal_locator,
            response_id=response_id,
            model_id=model_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
        with self._lock:
            self._provider_attempt_count += 1
            self._provider_latency_ns += latency_ns
            if succeeded:
                self._provider_response_id = response_id
                self._provider_model_id = model_id
                self._provider_finish_reason = finish_reason
                self._provider_input_tokens = input_tokens
                self._provider_output_tokens = output_tokens
                self._provider_total_tokens = total_tokens

    def bind_actor_sdk_arguments(
        self,
        sdk_arguments: JsonValue,
        *,
        stream: bool,
        collector_request_locator: JsonValue | None = None,
    ) -> str | None:
        """Bind one physical SDK attempt immediately before provider dispatch."""

        with self._lock:
            result = self._result
        if result is None:
            raise SentinelContractError("actor SDK binding ran before Sentinel evaluation")
        if self.requires_strict_provider_audit:
            deadline_ns = self._context.attributes.get("r24_case_deadline_monotonic_ns")
            if deadline_ns is None:
                policy_object = cast(object, self._sentinel.policy)
                if type(policy_object) is OwnerAuthorizedLivePerCallPolicyV1:
                    raise TimeoutError("production actor case deadline is absent")
            else:
                if type(deadline_ns) is not int or self._sentinel._clock_ns() >= deadline_ns:
                    raise TimeoutError("production actor case deadline has closed")
        return self._sentinel.bind_actor_sdk_arguments(
            logical_call_id=self._context.logical_call_id,
            result=result,
            sdk_arguments=sdk_arguments,
            collector_request_locator=collector_request_locator,
            stream=stream,
        )

    def finalize_actor_output(
        self,
        *,
        raw_provider_response: JsonValue,
        raw_parser_input: JsonValue,
        parsed_action: JsonValue,
        parser_id: str,
        parser_status: ParserResultStatusV1,
        parser_attempt_count: int,
        parser_ns: int,
    ) -> RuntimeAuditDetailV1 | None:
        """Complete the optional detail once the host parser returns an action."""

        with self._lock:
            production = self.requires_strict_provider_audit
            if self._runtime_audit_finalized or (
                not production
                and (
                    type(self._result) is not RuntimeVerticalSentinelResultV1
                    or self._result.receipt.validation_status is not SentinelValidationStatus.PASSED
                )
            ):
                return None
            if production and self._result is None:
                raise SentinelContractError("production actor output has no Sentinel result")
            if self._provider_attempt_count < 1:
                raise SentinelContractError("actor output has no recorded provider attempt")
            trace = _ActorProviderTraceSnapshot(
                attempt_count=self._provider_attempt_count,
                latency_ns=self._provider_latency_ns,
                response_id=self._provider_response_id,
                model_id=self._provider_model_id,
                finish_reason=self._provider_finish_reason,
                input_tokens=self._provider_input_tokens,
                output_tokens=self._provider_output_tokens,
                total_tokens=self._provider_total_tokens,
            )
            self._runtime_audit_finalized = True
        return self._sentinel.finalize_actor_output(
            logical_call_id=self._context.logical_call_id,
            attempt_id=f"r24-provider-attempt-{trace.attempt_count}",
            raw_provider_response=raw_provider_response,
            raw_parser_input=raw_parser_input,
            parsed_action=parsed_action,
            parser_id=parser_id,
            parser_status=parser_status,
            parser_attempt_count=parser_attempt_count,
            provider_ns=trace.latency_ns,
            parser_ns=parser_ns,
            response_id=trace.response_id,
            model_id=trace.model_id,
            finish_reason=trace.finish_reason,
            input_tokens=trace.input_tokens,
            output_tokens=trace.output_tokens,
            total_tokens=trace.total_tokens,
        )

    def cancel_runtime_audit(self) -> None:
        """Discard a pending detail when the host produces no actor action."""

        with self._lock:
            if self._runtime_audit_finalized:
                return
            self._runtime_audit_finalized = True
        self._sentinel.cancel_runtime_audit(self._context.logical_call_id)

    def finalize_actor_failure(
        self,
        *,
        failure_phase: str,
        failure_code: str,
    ) -> ProductionRuntimeAuditFailureReceiptV1 | None:
        """Publish an exact failed terminal instead of deleting incurred work."""

        with self._lock:
            if self._runtime_audit_finalized:
                return None
            self._runtime_audit_finalized = True
        return self._sentinel.finalize_production_actor_failure(
            logical_call_id=self._context.logical_call_id,
            failure_phase=failure_phase,
            failure_code=failure_code,
        )

    def finalize_action_execution(
        self,
        *,
        parsed_action: JsonValue,
        action_executed: bool,
        action_execution_ns: int = 0,
    ) -> ProductionRuntimeAuditReceiptV1 | None:
        return self._sentinel.finalize_action_execution(
            logical_call_id=self._context.logical_call_id,
            parsed_action=parsed_action,
            action_executed=action_executed,
            action_execution_ns=action_execution_ns,
        )

    def matches(
        self,
        sentinel: PromptSentinel,
        *,
        host_id: str,
        history_codec_id: str | None,
        call_role: SentinelCallRole,
    ) -> bool:
        return (
            self._sentinel is sentinel
            and self._context.host_id == host_id
            and self._history_codec_id == history_codec_id
            and self._call_role is call_role
        )


__all__ = [
    "GLOBAL_SENTINEL_KILL_SWITCH",
    "PromptSentinel",
    "SentinelGlobalSwitch",
    "SentinelLogicalCall",
    "bind_sentinel_logical_call",
    "current_sentinel_logical_call",
    "global_sentinel_kill_switch_active",
    "set_global_sentinel_kill_switch",
]
