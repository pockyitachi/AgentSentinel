"""In-process task session for the R2.3 CPU/offline/fake rubric boundary.

The session owns one frozen rubric version and one current tracking state.  It
does not receive actor history, mutate an actor request, call a provider, or
execute an action.  Semantic generation/tracking is delegated only to the
injected fake backend protocols from :mod:`.contracts`; graph/path state and
record relevance are derived again locally.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import TypeVar, cast
from uuid import uuid4

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_3.contracts import (
    CurrentObservationBindingV1,
    FrontierItemV1,
    GateOperator,
    GraphRefKind,
    GraphRefV1,
    MilestoneReasonCode,
    MilestoneState,
    MilestoneStateRecordV1,
    MilestoneV1,
    MultiPathRubricV1,
    PathKind,
    PathRelevanceInterfaceV1,
    PathRelevanceOutputV1,
    PathStateV1,
    PathViability,
    R23ContractError,
    RecordPathBindingV1,
    RecordRelevance,
    RecordRelevanceResultV1,
    RelevanceDisposition,
    RevisionKind,
    RevisionReason,
    RubricBackendDescriptorV1,
    RubricBuilderBackendV1,
    RubricCutoffV1,
    RubricEvidenceV1,
    RubricExecutionControlV1,
    RubricRevisionRequestV1,
    RubricTrackerBackendV1,
    RubricTrackerProposalV1,
    RubricTrackingPacketV1,
    RubricTrackingStateV1,
    SupportedRecordBindingV1,
    TaskInstructionV1,
    TaskStartRubricRequestV1,
    TopologyDeclarationV1,
    TopologyKind,
    TrackerProposalStatus,
    TrackingInputExclusionsV1,
    derive_actor_visible_rubric_state,
    path_relevance_output_sha256,
    rubric_binding,
    rubric_revision_request_sha256,
    rubric_sha256,
    rubric_tracking_state_sha256,
    supported_record_binding_sha256,
    task_start_request_sha256,
    tracker_proposal_sha256,
    tracking_packet_sha256,
    validate_path_relevance_output,
    validate_rubric_revision,
    validate_tracker_proposal,
    validate_tracking_packet,
    validate_tracking_state,
)
from mobile_world.runtime.sentinel.r2_3.metrics import (
    RubricMetricsV1,
    RubricRuntimeMetricV1,
)
from mobile_world.runtime.sentinel.r2_3.sidecar import (
    MemoryRubricReceiptSinkV1,
    RubricEvaluationStatus,
    RubricReceiptOperation,
    RubricReceiptSinkV1,
    RubricReceiptV1,
)

_T = TypeVar("_T")


class RubricSessionStage(StrEnum):
    """Operation that produced a session result."""

    TASK_START_GENERATE = "TASK_START_GENERATE"
    EXPLICIT_REVISION = "EXPLICIT_REVISION"
    TRACK = "TRACK"
    LINK_RELEVANCE = "LINK_RELEVANCE"


class RubricSessionStatus(StrEnum):
    """Closed outcome vocabulary for the in-process session."""

    ADMITTED = "ADMITTED"
    FALLBACK = "FALLBACK"


class RubricSessionFallbackCode(StrEnum):
    """Safe coarse fallback categories; contract codes remain separately bound."""

    NOT_INITIALIZED = "NOT_INITIALIZED"
    INPUT_REJECTED = "INPUT_REJECTED"
    STATE_CONFLICT = "STATE_CONFLICT"
    LOGICAL_CALL_DRIFT = "LOGICAL_CALL_DRIFT"
    BACKEND_ERROR = "BACKEND_ERROR"
    OUTPUT_REJECTED = "OUTPUT_REJECTED"
    SIDECAR_FAILURE = "SIDECAR_FAILURE"


@dataclass(frozen=True, slots=True)
class RubricSessionFallbackV1:
    code: RubricSessionFallbackCode
    contract_code: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not RubricSessionFallbackCode:
            raise TypeError("code must be an exact RubricSessionFallbackCode")
        if self.contract_code is not None and (
            type(self.contract_code) is not str
            or not self.contract_code
            or len(self.contract_code) > 128
            or not self.contract_code.replace("_", "A").isalnum()
            or self.contract_code.upper() != self.contract_code
        ):
            raise ValueError("contract_code must be a bounded safe code")


@dataclass(frozen=True, slots=True)
class RubricSessionResultV1:
    """Typed admitted/fallback result shared by the four session operations."""

    stage: RubricSessionStage
    status: RubricSessionStatus
    rubric: MultiPathRubricV1 | None
    state: RubricTrackingStateV1 | None
    proposal: RubricTrackerProposalV1 | None = None
    relevance: PathRelevanceOutputV1 | None = None
    fallback: RubricSessionFallbackV1 | None = None
    backend_called: bool = False
    receipt_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.stage) is not RubricSessionStage:
            raise TypeError("stage must be an exact RubricSessionStage")
        if type(self.status) is not RubricSessionStatus:
            raise TypeError("status must be an exact RubricSessionStatus")
        if type(self.backend_called) is not bool:
            raise TypeError("backend_called must be an exact bool")
        if self.receipt_sha256 is not None and (
            type(self.receipt_sha256) is not str
            or len(self.receipt_sha256) != 64
            or any(value not in "0123456789abcdef" for value in self.receipt_sha256)
        ):
            raise ValueError("receipt_sha256 must be lowercase SHA-256 or null")
        for value, expected, name in (
            (self.rubric, MultiPathRubricV1, "rubric"),
            (self.state, RubricTrackingStateV1, "state"),
            (self.proposal, RubricTrackerProposalV1, "proposal"),
            (self.relevance, PathRelevanceOutputV1, "relevance"),
            (self.fallback, RubricSessionFallbackV1, "fallback"),
        ):
            if value is not None and type(value) is not expected:
                raise TypeError(f"{name} must use its exact trusted type")
        if self.status is RubricSessionStatus.ADMITTED:
            if self.fallback is not None or self.rubric is None or self.state is None:
                raise ValueError("admitted session result needs rubric/state and no fallback")
            if self.stage is RubricSessionStage.TRACK and self.proposal is None:
                raise ValueError("admitted tracking result needs its admitted proposal")
            if self.stage is RubricSessionStage.LINK_RELEVANCE and self.relevance is None:
                raise ValueError("admitted relevance result needs its derived output")
        elif self.fallback is None:
            raise ValueError("fallback session result needs a typed fallback")
        if self.stage is not RubricSessionStage.TRACK and self.proposal is not None:
            raise ValueError("only tracking results may contain a proposal")
        if self.stage is not RubricSessionStage.LINK_RELEVANCE and self.relevance is not None:
            raise ValueError("only relevance results may contain relevance output")


class _DirectExecutionControl:
    """Default synchronous CPU control; deliberately contains no timeout machinery."""

    def run_backend(self, call: Callable[[], _T]) -> _T:
        return call()

    def publish_receipt(self, publish: Callable[[], None]) -> None:
        publish()


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _canonical_json_sha256_if_exact(value: object) -> str | None:
    """Hash an exact finite canonical-JSON tree without coercing SDK values."""

    active_container_ids: set[int] = set()
    node_count = 0

    def snapshot(item: object, *, depth: int) -> JsonValue:
        nonlocal node_count
        node_count += 1
        if node_count > 100_000 or depth > 64:
            raise ValueError("canonical JSON output exceeds the bounded snapshot domain")
        if item is None or type(item) in {str, bool, int}:
            return cast(JsonValue, item)
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("canonical JSON output contains a non-finite float")
            return cast(JsonValue, item)
        if type(item) not in {list, dict}:
            raise TypeError("backend output is outside the exact canonical-JSON domain")
        container_id = id(item)
        if container_id in active_container_ids:
            raise ValueError("canonical JSON output contains a cycle")
        active_container_ids.add(container_id)
        try:
            if type(item) is list:
                return [snapshot(child, depth=depth + 1) for child in cast(list[object], item)]
            mapping = cast(dict[object, object], item)
            if any(type(key) is not str for key in mapping):
                raise TypeError("canonical JSON object keys must be exact strings")
            return {
                cast(str, key): snapshot(child, depth=depth + 1) for key, child in mapping.items()
            }
        finally:
            active_container_ids.remove(container_id)

    try:
        projected = snapshot(value, depth=0)
        payload = json.dumps(
            projected,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _path_relevance_input_sha256(
    *,
    state: RubricTrackingStateV1,
    logical_call_id: str,
    record_bindings: tuple[RecordPathBindingV1, ...],
    supported_records: tuple[SupportedRecordBindingV1, ...],
) -> str:
    """Bind one relevance input without delimiter-ambiguous ID concatenation."""

    projection: JsonValue = {
        "schema_version": "mobileworld.runtime.rubric-path-relevance-input/v1",
        "rubric_state_sha256": rubric_tracking_state_sha256(state),
        "logical_call_id": logical_call_id,
        "record_bindings": [
            {
                "record_id": item.record_id,
                "linked_path_ids": list(item.linked_path_ids),
                "path_independent": item.path_independent,
            }
            for item in record_bindings
        ],
        "supported_records": [
            {
                "record_id": item.record_id,
                "policy_receipt_sha256": item.policy_receipt_sha256,
                "policy_output_sha256": item.policy_output_sha256,
                "factual_verdict": item.factual_verdict.value,
                "validity_operation": item.validity_operation.value,
            }
            for item in supported_records
        ],
    }
    digest = _canonical_json_sha256_if_exact(projection)
    assert digest is not None
    return digest


def _contract_failure(error: R23ContractError) -> RubricSessionFallbackV1:
    return RubricSessionFallbackV1(
        code=RubricSessionFallbackCode.OUTPUT_REJECTED,
        contract_code=error.code,
    )


class RubricTaskSession(PathRelevanceInterfaceV1):
    """One task-run's generate/revise/track/link state machine.

    The public tracking entry points intentionally accept only the closed
    history-free packet fields.  The same logical call and packet hash always
    reuse the same result, including typed fallbacks.
    """

    def __init__(
        self,
        *,
        task_run_id: str,
        task: TaskInstructionV1,
        builder_backend: RubricBuilderBackendV1,
        tracker_backend: RubricTrackerBackendV1,
        actor_visible_enabled: bool = False,
        execution_control: RubricExecutionControlV1 | None = None,
        receipt_sink: RubricReceiptSinkV1 | None = None,
        metrics: RubricMetricsV1 | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if type(task_run_id) is not str:
            raise TypeError("task_run_id must be an exact string")
        if type(task) is not TaskInstructionV1:
            raise TypeError("task must be an exact TaskInstructionV1")
        if type(actor_visible_enabled) is not bool:
            raise TypeError("actor_visible_enabled must be an exact bool")
        builder_descriptor = builder_backend.descriptor
        tracker_descriptor = tracker_backend.descriptor
        # TaskStartRubricRequestV1 performs the exact descriptor/scope check.
        TaskStartRubricRequestV1(
            request_id="r23-constructor-check",
            task_run_id=task_run_id,
            task=task,
            backend=builder_descriptor,
        )
        TaskStartRubricRequestV1(
            request_id="r23-tracker-check",
            task_run_id=task_run_id,
            task=task,
            backend=tracker_descriptor,
        )
        self._task_run_id = task_run_id
        self._task = task
        self._builder_backend = builder_backend
        self._tracker_backend = tracker_backend
        self._execution_control = execution_control or _DirectExecutionControl()
        self._receipt_sink = receipt_sink or MemoryRubricReceiptSinkV1()
        self._metrics = metrics or RubricMetricsV1()
        self._id_factory = id_factory or _default_id_factory
        self._actor_visible_enabled = actor_visible_enabled
        self._lock = RLock()
        self._rubric: MultiPathRubricV1 | None = None
        self._state: RubricTrackingStateV1 | None = None
        self._generation_result: RubricSessionResultV1 | None = None
        self._revision_cache: dict[str, tuple[RubricRevisionRequestV1, RubricSessionResultV1]] = {}
        self._tracking_cache: dict[str, tuple[str, RubricSessionResultV1]] = {}
        self._relevance_cache: dict[
            str,
            tuple[
                tuple[
                    str,
                    tuple[RecordPathBindingV1, ...],
                    tuple[SupportedRecordBindingV1, ...],
                ],
                RubricSessionResultV1,
            ],
        ] = {}
        self._task_start_generation_calls = 0
        self._explicit_revision_calls = 0
        self._runtime_tracking_calls = 0
        self._relevance_link_calls = 0

    @property
    def rubric(self) -> MultiPathRubricV1 | None:
        with self._lock:
            return self._rubric

    @property
    def state(self) -> RubricTrackingStateV1 | None:
        with self._lock:
            return self._state

    @property
    def task_start_generation_calls(self) -> int:
        with self._lock:
            return self._task_start_generation_calls

    @property
    def explicit_revision_calls(self) -> int:
        with self._lock:
            return self._explicit_revision_calls

    @property
    def runtime_tracking_calls(self) -> int:
        with self._lock:
            return self._runtime_tracking_calls

    @property
    def relevance_link_calls(self) -> int:
        with self._lock:
            return self._relevance_link_calls

    @property
    def receipt_sink(self) -> RubricReceiptSinkV1:
        return self._receipt_sink

    @property
    def metrics(self) -> RubricMetricsV1:
        return self._metrics

    def _run_backend(self, call: Callable[[], _T]) -> _T:
        return self._execution_control.run_backend(call)

    @staticmethod
    def _operation(stage: RubricSessionStage) -> RubricReceiptOperation:
        return RubricReceiptOperation(stage.value)

    @staticmethod
    def _evaluation_status(
        result_status: RubricSessionStatus,
        fallback: RubricSessionFallbackV1 | None,
    ) -> RubricEvaluationStatus:
        if result_status is RubricSessionStatus.ADMITTED:
            return RubricEvaluationStatus.ADMITTED
        assert fallback is not None
        return {
            RubricSessionFallbackCode.NOT_INITIALIZED: RubricEvaluationStatus.INPUT_REJECTED,
            RubricSessionFallbackCode.INPUT_REJECTED: RubricEvaluationStatus.INPUT_REJECTED,
            RubricSessionFallbackCode.STATE_CONFLICT: RubricEvaluationStatus.STATE_CONFLICT,
            RubricSessionFallbackCode.LOGICAL_CALL_DRIFT: RubricEvaluationStatus.STATE_CONFLICT,
            RubricSessionFallbackCode.BACKEND_ERROR: RubricEvaluationStatus.BACKEND_ERROR,
            RubricSessionFallbackCode.OUTPUT_REJECTED: RubricEvaluationStatus.ADMISSION_REJECTED,
            RubricSessionFallbackCode.SIDECAR_FAILURE: RubricEvaluationStatus.SIDECAR_FAILURE,
        }[fallback.code]

    def _receipt(
        self,
        *,
        stage: RubricSessionStage,
        status: RubricSessionStatus,
        fallback: RubricSessionFallbackV1 | None,
        descriptor: RubricBackendDescriptorV1,
        input_sha256: str,
        logical_call_id: str | None,
        rubric: MultiPathRubricV1 | None,
        prior_state: RubricTrackingStateV1 | None,
        final_state: RubricTrackingStateV1 | None,
        raw_backend_output_sha256: str | None,
        parsed_output_sha256: str | None,
        admitted_output_sha256: str | None,
        backend_calls: int,
        backend_latency_ns: int,
        admission_latency_ns: int,
        state_update_latency_ns: int,
        total_latency_ns: int,
        relevance: PathRelevanceOutputV1 | None = None,
        validation_checks: tuple[str, ...] = (),
    ) -> RubricReceiptV1:
        milestone_states = () if final_state is None else final_state.milestone_states
        path_states = () if final_state is None else final_state.path_states
        relevance_records = () if relevance is None else relevance.records
        topology = (
            final_state.topology.kind.value
            if final_state is not None
            else TopologyKind.ISOLATED_HISTORY_FREE.value
        )
        input_schema_sha256 = (
            descriptor.tracking_packet_schema_sha256 if stage is RubricSessionStage.TRACK else None
        )
        output_schema_sha256 = (
            descriptor.tracker_schema_sha256
            if stage in {RubricSessionStage.TRACK, RubricSessionStage.LINK_RELEVANCE}
            else descriptor.rubric_schema_sha256
        )
        receipt_status = self._evaluation_status(status, fallback)
        return RubricReceiptV1(
            receipt_id=self._id_factory("r23-receipt"),
            task_run_id=self._task_run_id,
            logical_call_id=logical_call_id,
            operation=self._operation(stage),
            topology_kind=topology,
            status=receipt_status,
            fallback_code=(
                None if fallback is None else fallback.contract_code or fallback.code.value
            ),
            backend_id=descriptor.backend_id,
            backend_version=descriptor.backend_version,
            prompt_sha256=descriptor.prompt_sha256,
            input_schema_sha256=input_schema_sha256,
            output_schema_sha256=output_schema_sha256,
            config_sha256=descriptor.config_sha256,
            input_sha256=input_sha256,
            raw_backend_output_sha256=raw_backend_output_sha256,
            parsed_output_sha256=parsed_output_sha256,
            admitted_output_sha256=admitted_output_sha256,
            rubric_id=None if rubric is None else rubric.rubric_id,
            rubric_version=None if rubric is None else rubric.rubric_version,
            rubric_sha256=None if rubric is None else rubric_sha256(rubric),
            prior_state_sha256=(
                None if prior_state is None else rubric_tracking_state_sha256(prior_state)
            ),
            final_state_sha256=(
                None if final_state is None else rubric_tracking_state_sha256(final_state)
            ),
            backend_calls=backend_calls,
            task_start_generation_calls=self._task_start_generation_calls,
            explicit_revision_calls=self._explicit_revision_calls,
            runtime_tracking_calls=self._runtime_tracking_calls,
            relevance_link_calls=self._relevance_link_calls,
            packet_build_latency_ns=0,
            backend_latency_ns=backend_latency_ns,
            admission_latency_ns=admission_latency_ns,
            state_update_latency_ns=state_update_latency_ns,
            total_latency_ns=total_latency_ns,
            pending_count=sum(value.state is MilestoneState.PENDING for value in milestone_states),
            in_progress_count=sum(
                value.state is MilestoneState.IN_PROGRESS for value in milestone_states
            ),
            satisfied_count=sum(
                value.state is MilestoneState.SATISFIED for value in milestone_states
            ),
            violated_count=sum(
                value.state is MilestoneState.VIOLATED for value in milestone_states
            ),
            unknown_milestone_count=sum(
                value.state is MilestoneState.UNKNOWN for value in milestone_states
            ),
            viable_path_count=sum(value.state is PathViability.VIABLE for value in path_states),
            inactive_path_count=sum(value.state is PathViability.INACTIVE for value in path_states),
            unknown_path_count=sum(value.state is PathViability.UNKNOWN for value in path_states),
            frontier_count=0 if final_state is None else len(final_state.frontier),
            active_path_relevance_count=sum(
                value.relevance is RecordRelevance.ACTIVE_PATH for value in relevance_records
            ),
            inactive_branch_relevance_count=sum(
                value.relevance is RecordRelevance.INACTIVE_BRANCH for value in relevance_records
            ),
            path_independent_relevance_count=sum(
                value.relevance is RecordRelevance.PATH_INDEPENDENT for value in relevance_records
            ),
            unknown_relevance_count=sum(
                value.relevance is RecordRelevance.UNKNOWN for value in relevance_records
            ),
            archive_shadow_count=sum(
                value.disposition is RelevanceDisposition.ARCHIVE_SHADOW
                for value in relevance_records
            ),
            unknown_or_abstain_count=(
                sum(value.state is MilestoneState.UNKNOWN for value in milestone_states)
                + sum(value.state is PathViability.UNKNOWN for value in path_states)
                + sum(value.relevance is RecordRelevance.UNKNOWN for value in relevance_records)
            ),
            validation_checks=validation_checks,
        )

    def _emit(self, receipt: RubricReceiptV1) -> str:
        self._execution_control.publish_receipt(lambda: self._receipt_sink.emit(receipt))
        return receipt.sha256

    def _record_metric(
        self,
        *,
        stage: RubricSessionStage,
        status: RubricEvaluationStatus,
        latency_ns: int,
        backend_calls: int,
        state: RubricTrackingStateV1 | None,
        relevance: PathRelevanceOutputV1 | None = None,
        duplicate_cache_reuse: bool = False,
    ) -> None:
        try:
            records = () if relevance is None else relevance.records
            self._metrics.record_runtime(
                RubricRuntimeMetricV1(
                    operation=self._operation(stage).value,
                    status=status.value,
                    latency_ns=latency_ns,
                    backend_calls=backend_calls,
                    duplicate_cache_reuse=duplicate_cache_reuse,
                    milestone_states=(
                        ()
                        if state is None
                        else tuple(value.state.value for value in state.milestone_states)
                    ),
                    path_states=(
                        ()
                        if state is None
                        else tuple(value.state.value for value in state.path_states)
                    ),
                    relevance=tuple(value.relevance.value for value in records),
                    archive_shadow_count=sum(
                        value.disposition is RelevanceDisposition.ARCHIVE_SHADOW
                        for value in records
                    ),
                )
            )
        except Exception:
            # Metrics are intentionally best-effort and have no admission authority.
            return

    def _record_cache_reuse(
        self,
        *,
        stage: RubricSessionStage,
        result: RubricSessionResultV1,
    ) -> None:
        self._record_metric(
            stage=stage,
            status=self._evaluation_status(result.status, result.fallback),
            latency_ns=0,
            backend_calls=0,
            state=None,
            relevance=None,
            duplicate_cache_reuse=True,
        )

    def _fallback(
        self,
        *,
        stage: RubricSessionStage,
        code: RubricSessionFallbackCode,
        contract_code: str | None = None,
        backend_called: bool = False,
        receipt_sha256: str | None = None,
    ) -> RubricSessionResultV1:
        return RubricSessionResultV1(
            stage=stage,
            status=RubricSessionStatus.FALLBACK,
            rubric=self._rubric,
            state=self._state,
            fallback=RubricSessionFallbackV1(code=code, contract_code=contract_code),
            backend_called=backend_called,
            receipt_sha256=receipt_sha256,
        )

    def _audited_fallback(
        self,
        *,
        stage: RubricSessionStage,
        code: RubricSessionFallbackCode,
        descriptor: RubricBackendDescriptorV1,
        input_sha256: str,
        logical_call_id: str | None,
        prior_state: RubricTrackingStateV1 | None,
        backend_called: bool,
        operation_started_ns: int,
        contract_code: str | None = None,
        raw_backend_output_sha256: str | None = None,
        parsed_output_sha256: str | None = None,
    ) -> RubricSessionResultV1:
        fallback = RubricSessionFallbackV1(code=code, contract_code=contract_code)
        status = self._evaluation_status(RubricSessionStatus.FALLBACK, fallback)
        receipt = self._receipt(
            stage=stage,
            status=RubricSessionStatus.FALLBACK,
            fallback=fallback,
            descriptor=descriptor,
            input_sha256=input_sha256,
            logical_call_id=logical_call_id,
            rubric=self._rubric,
            prior_state=prior_state,
            final_state=prior_state,
            raw_backend_output_sha256=raw_backend_output_sha256,
            parsed_output_sha256=parsed_output_sha256,
            admitted_output_sha256=None,
            backend_calls=int(backend_called),
            backend_latency_ns=0,
            admission_latency_ns=0,
            state_update_latency_ns=0,
            total_latency_ns=time.monotonic_ns() - operation_started_ns,
            validation_checks=(contract_code or code.value,),
        )
        try:
            receipt_sha256 = self._emit(receipt)
        except Exception:
            result = self._fallback(
                stage=stage,
                code=RubricSessionFallbackCode.SIDECAR_FAILURE,
                backend_called=backend_called,
            )
            self._record_metric(
                stage=stage,
                status=RubricEvaluationStatus.SIDECAR_FAILURE,
                latency_ns=time.monotonic_ns() - operation_started_ns,
                backend_calls=int(backend_called),
                state=prior_state,
            )
            return result
        result = self._fallback(
            stage=stage,
            code=code,
            contract_code=contract_code,
            backend_called=backend_called,
            receipt_sha256=receipt_sha256,
        )
        self._record_metric(
            stage=stage,
            status=status,
            latency_ns=time.monotonic_ns() - operation_started_ns,
            backend_calls=int(backend_called),
            state=prior_state,
        )
        return result

    def _new_initial_state(self, rubric: MultiPathRubricV1) -> RubricTrackingStateV1:
        milestones = tuple(
            MilestoneStateRecordV1(
                milestone_id=item.milestone_id,
                state=MilestoneState.PENDING,
                evidence_refs=(),
                reason_code=MilestoneReasonCode.NOT_STARTED,
            )
            for item in rubric.milestones
        )
        paths, frontier = self._derive_paths_and_frontier(rubric, milestones)
        state = RubricTrackingStateV1(
            state_id=_stable_id(
                "r23-state",
                rubric_sha256(rubric),
                "initial",
            ),
            rubric_binding=rubric_binding(rubric),
            state_version=0,
            source_packet_id=None,
            logical_call_id=None,
            prior_state_sha256=None,
            milestone_states=milestones,
            path_states=paths,
            frontier=frontier,
            topology=TopologyDeclarationV1(
                kind=TopologyKind.ISOLATED_HISTORY_FREE,
                independent_grounding_claim_eligible=True,
            ),
            actor_visible=derive_actor_visible_rubric_state(
                enabled=self._actor_visible_enabled,
                milestone_states=milestones,
                path_states=paths,
            ),
        )
        validate_tracking_state(state, rubric)
        return state

    def start(self) -> RubricSessionResultV1:
        """Generate exactly once for this task-run and cache success or fallback."""

        with self._lock:
            if self._generation_result is not None:
                self._record_cache_reuse(
                    stage=RubricSessionStage.TASK_START_GENERATE,
                    result=self._generation_result,
                )
                return self._generation_result
            stage = RubricSessionStage.TASK_START_GENERATE
            operation_started = time.monotonic_ns()
            try:
                request = TaskStartRubricRequestV1(
                    request_id=self._id_factory("r23-generate"),
                    task_run_id=self._task_run_id,
                    task=self._task,
                    backend=self._builder_backend.descriptor,
                )
            except R23ContractError as error:
                result = self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    contract_code=error.code,
                )
                self._generation_result = result
                return result
            self._task_start_generation_calls += 1
            backend_started = time.monotonic_ns()
            try:
                candidate = self._run_backend(lambda: self._builder_backend.generate(request))
            except Exception:
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.BACKEND_ERROR,
                    descriptor=request.backend,
                    input_sha256=task_start_request_sha256(request),
                    logical_call_id=None,
                    prior_state=None,
                    backend_called=True,
                    operation_started_ns=operation_started,
                )
                self._generation_result = result
                return result
            backend_latency = time.monotonic_ns() - backend_started
            admission_started = time.monotonic_ns()
            try:
                if type(candidate) is not MultiPathRubricV1:
                    raise R23ContractError(
                        "UNTRUSTED_TYPE", "builder must return an exact MultiPathRubricV1"
                    )
                if (
                    candidate.task_run_id != request.task_run_id
                    or candidate.task != request.task
                    or candidate.backend != request.backend
                    or candidate.revision.kind is not RevisionKind.INITIAL
                    or candidate.revision.revision_event_id != request.task.source_event_id
                ):
                    raise R23ContractError(
                        "TASK_START_BINDING_MISMATCH",
                        "generated rubric differs from the task-start request",
                    )
                state = self._new_initial_state(candidate)
            except R23ContractError as error:
                raw_hash = (
                    rubric_sha256(candidate)
                    if type(candidate) is MultiPathRubricV1
                    else _canonical_json_sha256_if_exact(candidate)
                )
                parsed_hash = raw_hash if type(candidate) is MultiPathRubricV1 else None
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.OUTPUT_REJECTED,
                    descriptor=request.backend,
                    input_sha256=task_start_request_sha256(request),
                    logical_call_id=None,
                    prior_state=None,
                    contract_code=error.code,
                    backend_called=True,
                    operation_started_ns=operation_started,
                    raw_backend_output_sha256=raw_hash,
                    parsed_output_sha256=parsed_hash,
                )
                self._generation_result = result
                return result
            admission_latency = time.monotonic_ns() - admission_started
            candidate_hash = rubric_sha256(candidate)
            total_latency = time.monotonic_ns() - operation_started
            receipt = self._receipt(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                fallback=None,
                descriptor=request.backend,
                input_sha256=task_start_request_sha256(request),
                logical_call_id=None,
                rubric=candidate,
                prior_state=None,
                final_state=state,
                raw_backend_output_sha256=candidate_hash,
                parsed_output_sha256=candidate_hash,
                admitted_output_sha256=rubric_tracking_state_sha256(state),
                backend_calls=1,
                backend_latency_ns=backend_latency,
                admission_latency_ns=admission_latency,
                state_update_latency_ns=0,
                total_latency_ns=total_latency,
                validation_checks=("RUBRIC_GENERATED", "INITIAL_STATE_DERIVED"),
            )
            try:
                receipt_sha256 = self._emit(receipt)
            except Exception:
                result = self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.SIDECAR_FAILURE,
                    backend_called=True,
                )
                self._record_metric(
                    stage=stage,
                    status=RubricEvaluationStatus.SIDECAR_FAILURE,
                    latency_ns=time.monotonic_ns() - operation_started,
                    backend_calls=1,
                    state=None,
                )
                self._generation_result = result
                return result
            self._rubric = candidate
            self._state = state
            result = RubricSessionResultV1(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                rubric=candidate,
                state=state,
                backend_called=True,
                receipt_sha256=receipt_sha256,
            )
            self._record_metric(
                stage=stage,
                status=RubricEvaluationStatus.ADMITTED,
                latency_ns=time.monotonic_ns() - operation_started,
                backend_calls=1,
                state=state,
            )
            self._generation_result = result
            return result

    def generate_once(self) -> RubricSessionResultV1:
        return self.start()

    def make_revision_request(
        self,
        *,
        revision_event_id: str,
        reason: RevisionReason,
        task: TaskInstructionV1,
        request_id: str | None = None,
    ) -> RubricRevisionRequestV1:
        """Bind an explicit revision request to the current frozen rubric."""

        with self._lock:
            if self._rubric is None:
                raise R23ContractError("NOT_INITIALIZED", "task rubric has not been generated")
            # RubricRevisionRequestV1 performs the exact RevisionReason check.
            return RubricRevisionRequestV1(
                request_id=request_id or self._id_factory("r23-revision"),
                task_run_id=self._task_run_id,
                previous_rubric_id=self._rubric.rubric_id,
                previous_rubric_version=self._rubric.rubric_version,
                previous_rubric_sha256=rubric_sha256(self._rubric),
                revision_event_id=revision_event_id,
                reason=reason,
                task=task,
                backend=self._builder_backend.descriptor,
            )

    def revise(self, request: RubricRevisionRequestV1) -> RubricSessionResultV1:
        """Apply one explicit, parent-hash-bound revision and reset tracker state."""

        with self._lock:
            stage = RubricSessionStage.EXPLICIT_REVISION
            if type(request) is not RubricRevisionRequestV1:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    contract_code="UNTRUSTED_TYPE",
                )
            operation_started = time.monotonic_ns()
            cached = self._revision_cache.get(request.revision_event_id)
            if cached is not None:
                cached_request, cached_result = cached
                if cached_request == request:
                    self._record_cache_reuse(stage=stage, result=cached_result)
                    return cached_result
                return self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.LOGICAL_CALL_DRIFT,
                    descriptor=self._builder_backend.descriptor,
                    input_sha256=rubric_revision_request_sha256(request),
                    logical_call_id=None,
                    prior_state=self._state,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="REVISION_EVENT_DRIFT",
                )
            previous = self._rubric
            if previous is None or self._state is None:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.NOT_INITIALIZED,
                )
            previous_state = self._state
            if (
                request.task_run_id != self._task_run_id
                or request.previous_rubric_id != previous.rubric_id
                or request.previous_rubric_version != previous.rubric_version
                or request.previous_rubric_sha256 != rubric_sha256(previous)
                or request.backend != self._builder_backend.descriptor
            ):
                return self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.STATE_CONFLICT,
                    descriptor=request.backend,
                    input_sha256=rubric_revision_request_sha256(request),
                    logical_call_id=None,
                    prior_state=previous_state,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="REVISION_PARENT_MISMATCH",
                )
            self._explicit_revision_calls += 1
            backend_started = time.monotonic_ns()
            try:
                candidate = self._run_backend(lambda: self._builder_backend.revise(request))
            except Exception:
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.BACKEND_ERROR,
                    descriptor=request.backend,
                    input_sha256=rubric_revision_request_sha256(request),
                    logical_call_id=None,
                    prior_state=previous_state,
                    backend_called=True,
                    operation_started_ns=operation_started,
                )
                self._revision_cache[request.revision_event_id] = (request, result)
                return result
            backend_latency = time.monotonic_ns() - backend_started
            admission_started = time.monotonic_ns()
            try:
                if type(candidate) is not MultiPathRubricV1:
                    raise R23ContractError(
                        "UNTRUSTED_TYPE", "builder must return an exact MultiPathRubricV1"
                    )
                if (
                    candidate.task != request.task
                    or candidate.backend != request.backend
                    or candidate.revision.revision_event_id != request.revision_event_id
                    or candidate.revision.reason is not request.reason
                ):
                    raise R23ContractError(
                        "REVISION_REQUEST_BINDING_MISMATCH",
                        "revised rubric differs from the explicit request",
                    )
                validate_rubric_revision(previous, candidate)
                state = self._new_initial_state(candidate)
            except R23ContractError as error:
                raw_hash = (
                    rubric_sha256(candidate)
                    if type(candidate) is MultiPathRubricV1
                    else _canonical_json_sha256_if_exact(candidate)
                )
                parsed_hash = raw_hash if type(candidate) is MultiPathRubricV1 else None
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.OUTPUT_REJECTED,
                    descriptor=request.backend,
                    input_sha256=rubric_revision_request_sha256(request),
                    logical_call_id=None,
                    prior_state=previous_state,
                    contract_code=error.code,
                    backend_called=True,
                    operation_started_ns=operation_started,
                    raw_backend_output_sha256=raw_hash,
                    parsed_output_sha256=parsed_hash,
                )
                self._revision_cache[request.revision_event_id] = (request, result)
                return result
            admission_latency = time.monotonic_ns() - admission_started
            candidate_hash = rubric_sha256(candidate)
            receipt = self._receipt(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                fallback=None,
                descriptor=request.backend,
                input_sha256=rubric_revision_request_sha256(request),
                logical_call_id=None,
                rubric=candidate,
                prior_state=previous_state,
                final_state=state,
                raw_backend_output_sha256=candidate_hash,
                parsed_output_sha256=candidate_hash,
                admitted_output_sha256=rubric_tracking_state_sha256(state),
                backend_calls=1,
                backend_latency_ns=backend_latency,
                admission_latency_ns=admission_latency,
                state_update_latency_ns=0,
                total_latency_ns=time.monotonic_ns() - operation_started,
                validation_checks=("EXPLICIT_REVISION_BOUND", "INITIAL_STATE_DERIVED"),
            )
            try:
                receipt_sha256 = self._emit(receipt)
            except Exception:
                result = self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.SIDECAR_FAILURE,
                    backend_called=True,
                )
                self._record_metric(
                    stage=stage,
                    status=RubricEvaluationStatus.SIDECAR_FAILURE,
                    latency_ns=time.monotonic_ns() - operation_started,
                    backend_calls=1,
                    state=previous_state,
                )
                self._revision_cache[request.revision_event_id] = (request, result)
                return result
            self._rubric = candidate
            self._state = state
            self._tracking_cache.clear()
            self._relevance_cache.clear()
            result = RubricSessionResultV1(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                rubric=candidate,
                state=state,
                backend_called=True,
                receipt_sha256=receipt_sha256,
            )
            self._record_metric(
                stage=stage,
                status=RubricEvaluationStatus.ADMITTED,
                latency_ns=time.monotonic_ns() - operation_started,
                backend_calls=1,
                state=state,
            )
            self._revision_cache[request.revision_event_id] = (request, result)
            return result

    def make_tracking_packet(
        self,
        *,
        logical_call_id: str,
        cutoff: RubricCutoffV1,
        current_observation: CurrentObservationBindingV1,
        evidence_index: tuple[RubricEvidenceV1, ...],
        packet_id: str | None = None,
        input_exclusions: TrackingInputExclusionsV1 | None = None,
    ) -> RubricTrackingPacketV1:
        """Build a packet whose signature has no actor-history or History-IR input."""

        with self._lock:
            packet = self._compose_tracking_packet(
                logical_call_id=logical_call_id,
                cutoff=cutoff,
                current_observation=current_observation,
                evidence_index=evidence_index,
                packet_id=packet_id,
                input_exclusions=input_exclusions,
            )
            assert self._rubric is not None
            validate_tracking_packet(packet, self._rubric)
            return packet

    def _compose_tracking_packet(
        self,
        *,
        logical_call_id: str,
        cutoff: RubricCutoffV1,
        current_observation: CurrentObservationBindingV1,
        evidence_index: tuple[RubricEvidenceV1, ...],
        packet_id: str | None,
        input_exclusions: TrackingInputExclusionsV1 | None,
    ) -> RubricTrackingPacketV1:
        if self._rubric is None or self._state is None:
            raise R23ContractError("NOT_INITIALIZED", "task rubric has not been generated")
        evidence_key = "|".join(
            f"{item.evidence_id}:{item.payload_sha256}:{item.source_event_seq}"
            for item in evidence_index
        )
        resolved_packet_id = packet_id or _stable_id(
            "r23-track",
            self._task_run_id,
            logical_call_id,
            cutoff.step_id,
            rubric_sha256(self._rubric),
            rubric_tracking_state_sha256(self._state),
            cutoff.current_observation_event_id,
            str(cutoff.cutoff_event_seq),
            current_observation.screenshot_content_sha256,
            evidence_key,
        )
        return RubricTrackingPacketV1(
            packet_id=resolved_packet_id,
            logical_call_id=logical_call_id,
            task_run_id=self._task_run_id,
            step_id=cutoff.step_id,
            rubric_binding=rubric_binding(self._rubric),
            prior_state=self._state,
            cutoff=cutoff,
            task=self._rubric.task,
            current_observation=current_observation,
            evidence_index=evidence_index,
            input_exclusions=input_exclusions or TrackingInputExclusionsV1(),
        )

    def track_step(
        self,
        *,
        logical_call_id: str,
        cutoff: RubricCutoffV1,
        current_observation: CurrentObservationBindingV1,
        evidence_index: tuple[RubricEvidenceV1, ...],
        packet_id: str | None = None,
        input_exclusions: TrackingInputExclusionsV1 | None = None,
    ) -> RubricSessionResultV1:
        try:
            with self._lock:
                packet = self._compose_tracking_packet(
                    logical_call_id=logical_call_id,
                    cutoff=cutoff,
                    current_observation=current_observation,
                    evidence_index=evidence_index,
                    packet_id=packet_id,
                    input_exclusions=input_exclusions,
                )
        except R23ContractError as error:
            with self._lock:
                return self._fallback(
                    stage=RubricSessionStage.TRACK,
                    code=(
                        RubricSessionFallbackCode.NOT_INITIALIZED
                        if error.code == "NOT_INITIALIZED"
                        else RubricSessionFallbackCode.INPUT_REJECTED
                    ),
                    contract_code=error.code,
                )
        return self.track(packet)

    def track(self, packet: RubricTrackingPacketV1) -> RubricSessionResultV1:
        """Run the fake tracker once and CAS its proposal into current state."""

        with self._lock:
            stage = RubricSessionStage.TRACK
            if type(packet) is not RubricTrackingPacketV1:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    contract_code="UNTRUSTED_TYPE",
                )
            try:
                packet_hash = tracking_packet_sha256(packet)
            except R23ContractError as error:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    contract_code=error.code,
                )
            operation_started = time.monotonic_ns()
            cached = self._tracking_cache.get(packet.logical_call_id)
            if cached is not None:
                cached_hash, cached_result = cached
                if cached_hash == packet_hash:
                    self._record_cache_reuse(stage=stage, result=cached_result)
                    return cached_result
                return self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.LOGICAL_CALL_DRIFT,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=packet_hash,
                    logical_call_id=packet.logical_call_id,
                    prior_state=self._state,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="LOGICAL_CALL_PACKET_DRIFT",
                )
            rubric = self._rubric
            prior = self._state
            if rubric is None or prior is None:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.NOT_INITIALIZED,
                )
            try:
                validate_tracking_packet(packet, rubric)
            except R23ContractError as error:
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=packet_hash,
                    logical_call_id=packet.logical_call_id,
                    prior_state=prior,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code=error.code,
                )
                self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
                return result
            if packet.prior_state != prior or rubric_tracking_state_sha256(
                packet.prior_state
            ) != rubric_tracking_state_sha256(prior):
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.STATE_CONFLICT,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=packet_hash,
                    logical_call_id=packet.logical_call_id,
                    prior_state=prior,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="PRIOR_STATE_CAS_FAILED",
                )
                self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
                return result
            self._runtime_tracking_calls += 1
            backend_started = time.monotonic_ns()
            try:
                proposal = self._run_backend(lambda: self._tracker_backend.track(packet))
            except Exception:
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.BACKEND_ERROR,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=packet_hash,
                    logical_call_id=packet.logical_call_id,
                    prior_state=prior,
                    backend_called=True,
                    operation_started_ns=operation_started,
                )
                self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
                return result
            backend_latency = time.monotonic_ns() - backend_started
            admission_started = time.monotonic_ns()
            try:
                validate_tracker_proposal(proposal, packet, rubric)
                state = self._admit_tracking_state(packet, proposal, rubric)
            except R23ContractError as error:
                raw_hash = (
                    tracker_proposal_sha256(proposal)
                    if type(proposal) is RubricTrackerProposalV1
                    else _canonical_json_sha256_if_exact(proposal)
                )
                parsed_hash = raw_hash if type(proposal) is RubricTrackerProposalV1 else None
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.OUTPUT_REJECTED,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=packet_hash,
                    logical_call_id=packet.logical_call_id,
                    prior_state=prior,
                    contract_code=error.code,
                    backend_called=True,
                    operation_started_ns=operation_started,
                    raw_backend_output_sha256=raw_hash,
                    parsed_output_sha256=parsed_hash,
                )
                self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
                return result
            admission_latency = time.monotonic_ns() - admission_started
            proposal_hash = tracker_proposal_sha256(proposal)
            state_hash = rubric_tracking_state_sha256(state)
            receipt = self._receipt(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                fallback=None,
                descriptor=self._tracker_backend.descriptor,
                input_sha256=packet_hash,
                logical_call_id=packet.logical_call_id,
                rubric=rubric,
                prior_state=prior,
                final_state=state,
                raw_backend_output_sha256=proposal_hash,
                parsed_output_sha256=proposal_hash,
                admitted_output_sha256=state_hash,
                backend_calls=1,
                backend_latency_ns=backend_latency,
                admission_latency_ns=admission_latency,
                state_update_latency_ns=0,
                total_latency_ns=time.monotonic_ns() - operation_started,
                validation_checks=(
                    "HISTORY_FREE_PACKET_BOUND",
                    "TRACKER_PROPOSAL_ADMITTED",
                    "PATH_STATE_DERIVED",
                ),
            )
            try:
                receipt_sha256 = self._emit(receipt)
            except Exception:
                result = self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.SIDECAR_FAILURE,
                    backend_called=True,
                )
                self._record_metric(
                    stage=stage,
                    status=RubricEvaluationStatus.SIDECAR_FAILURE,
                    latency_ns=time.monotonic_ns() - operation_started,
                    backend_calls=1,
                    state=prior,
                )
                self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
                return result
            self._state = state
            result = RubricSessionResultV1(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                rubric=rubric,
                state=state,
                proposal=proposal,
                backend_called=True,
                receipt_sha256=receipt_sha256,
            )
            self._record_metric(
                stage=stage,
                status=RubricEvaluationStatus.ADMITTED,
                latency_ns=time.monotonic_ns() - operation_started,
                backend_calls=1,
                state=state,
            )
            self._tracking_cache[packet.logical_call_id] = (packet_hash, result)
            return result

    def _admit_tracking_state(
        self,
        packet: RubricTrackingPacketV1,
        proposal: RubricTrackerProposalV1,
        rubric: MultiPathRubricV1,
    ) -> RubricTrackingStateV1:
        milestone_states = proposal.milestone_states
        if proposal.proposal_status is TrackerProposalStatus.ABSTAIN and any(
            value.state is not MilestoneState.UNKNOWN for value in milestone_states
        ):
            raise R23ContractError(
                "AMBIGUOUS_STATE_FORCED", "an abstaining tracker must preserve unknown"
            )
        path_states, frontier = self._derive_paths_and_frontier(rubric, milestone_states)
        prior_hash = rubric_tracking_state_sha256(packet.prior_state)
        state = RubricTrackingStateV1(
            state_id=_stable_id(
                "r23-state",
                tracking_packet_sha256(packet),
                tracker_proposal_sha256(proposal),
                prior_hash,
            ),
            rubric_binding=rubric_binding(rubric),
            state_version=packet.prior_state.state_version + 1,
            source_packet_id=packet.packet_id,
            logical_call_id=packet.logical_call_id,
            prior_state_sha256=prior_hash,
            milestone_states=milestone_states,
            path_states=path_states,
            frontier=frontier,
            topology=packet.topology,
            actor_visible=derive_actor_visible_rubric_state(
                enabled=self._actor_visible_enabled,
                milestone_states=milestone_states,
                path_states=path_states,
            ),
        )
        validate_tracking_state(state, rubric)
        return state

    def _derive_paths_and_frontier(
        self,
        rubric: MultiPathRubricV1,
        milestone_states: tuple[MilestoneStateRecordV1, ...],
    ) -> tuple[tuple[PathStateV1, ...], tuple[FrontierItemV1, ...]]:
        milestone_state = {value.milestone_id: value.state for value in milestone_states}
        milestones: dict[str, MilestoneV1] = {
            value.milestone_id: value for value in rubric.milestones
        }
        gates = {value.gate_id: value for value in rubric.gates}

        def combine_and(values: tuple[PathViability, ...]) -> PathViability:
            if any(value is PathViability.INACTIVE for value in values):
                return PathViability.INACTIVE
            if any(value is PathViability.UNKNOWN for value in values):
                return PathViability.UNKNOWN
            return PathViability.VIABLE

        def evaluate(reference: GraphRefV1) -> PathViability:
            if reference.ref_kind is GraphRefKind.MILESTONE:
                value = milestone_state[reference.ref_id]
                if value is MilestoneState.VIOLATED and milestones[reference.ref_id].blocking:
                    return PathViability.INACTIVE
                if value is MilestoneState.UNKNOWN:
                    return PathViability.UNKNOWN
                return PathViability.VIABLE
            gate = gates[reference.ref_id]
            children = tuple(evaluate(value) for value in gate.children)
            if gate.operator is GateOperator.AND:
                return combine_and(children)
            if any(value is PathViability.VIABLE for value in children):
                return PathViability.VIABLE
            if any(value is PathViability.UNKNOWN for value in children):
                return PathViability.UNKNOWN
            return PathViability.INACTIVE

        def is_satisfied(reference: GraphRefV1) -> bool:
            if reference.ref_kind is GraphRefKind.MILESTONE:
                return milestone_state[reference.ref_id] is MilestoneState.SATISFIED
            gate = gates[reference.ref_id]
            child_values = tuple(is_satisfied(child) for child in gate.children)
            if gate.operator is GateOperator.AND:
                return all(child_values)
            return any(child_values)

        path_values: list[PathStateV1] = []
        for path in rubric.paths:
            if path.kind is PathKind.OTHER_UNKNOWN:
                value = PathViability.UNKNOWN
            else:
                assert path.root is not None
                roots = (
                    (path.root,) if rubric.common_root is None else (rubric.common_root, path.root)
                )
                value = combine_and(tuple(evaluate(root) for root in roots))
            path_values.append(PathStateV1(path_id=path.path_id, state=value))
        path_states = tuple(path_values)
        path_lookup = {value.path_id: value.state for value in path_states}

        frontier: list[FrontierItemV1] = []
        seen: set[tuple[str, str]] = set()

        def collect(path_id: str, reference: GraphRefV1) -> None:
            if evaluate(reference) is PathViability.INACTIVE:
                return
            # A satisfied OR branch completes that gate.  Its unresolved legal
            # alternatives are not current frontier items and remain available
            # only as alternative graph structure.
            if is_satisfied(reference):
                return
            if reference.ref_kind is GraphRefKind.MILESTONE:
                state = milestone_state[reference.ref_id]
                if state in {
                    MilestoneState.PENDING,
                    MilestoneState.IN_PROGRESS,
                    MilestoneState.UNKNOWN,
                }:
                    key = (path_id, reference.ref_id)
                    if key not in seen:
                        seen.add(key)
                        frontier.append(
                            FrontierItemV1(path_id=path_id, milestone_id=reference.ref_id)
                        )
                return
            for child in gates[reference.ref_id].children:
                collect(path_id, child)

        for path in rubric.paths:
            if (
                path.kind is PathKind.OTHER_UNKNOWN
                or path_lookup[path.path_id] is PathViability.INACTIVE
            ):
                continue
            if rubric.common_root is not None:
                collect(path.path_id, rubric.common_root)
            assert path.root is not None
            collect(path.path_id, path.root)
        if len(frontier) > 4096:
            raise R23ContractError(
                "FRONTIER_LIMIT_EXCEEDED", "derived frontier exceeds the contract bound"
            )
        return path_states, tuple(frontier)

    def link_records(
        self,
        *,
        state: RubricTrackingStateV1,
        record_bindings: tuple[RecordPathBindingV1, ...],
        supported_records: tuple[SupportedRecordBindingV1, ...],
        logical_call_id: str,
    ) -> RubricSessionResultV1:
        """Link records only after the state for ``logical_call_id`` is admitted."""

        with self._lock:
            stage = RubricSessionStage.LINK_RELEVANCE
            rubric = self._rubric
            current = self._state
            if rubric is None or current is None:
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.NOT_INITIALIZED,
                )
            if (
                type(state) is not RubricTrackingStateV1
                or type(record_bindings) is not tuple
                or any(type(value) is not RecordPathBindingV1 for value in record_bindings)
                or type(supported_records) is not tuple
                or any(type(value) is not SupportedRecordBindingV1 for value in supported_records)
                or type(logical_call_id) is not str
            ):
                return self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    contract_code="UNTRUSTED_TYPE",
                )
            cache_key = (
                rubric_tracking_state_sha256(state),
                record_bindings,
                supported_records,
            )
            link_input_hash = _path_relevance_input_sha256(
                state=state,
                logical_call_id=logical_call_id,
                record_bindings=record_bindings,
                supported_records=supported_records,
            )
            operation_started = time.monotonic_ns()
            if (
                state != current
                or state.state_version == 0
                or state.logical_call_id != logical_call_id
            ):
                return self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.STATE_CONFLICT,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=link_input_hash,
                    logical_call_id=logical_call_id,
                    prior_state=current,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="POST_STATE_LINK_REQUIRED",
                )
            cached = self._relevance_cache.get(logical_call_id)
            if cached is not None:
                cached_key, cached_result = cached
                if cached_key == cache_key:
                    self._record_cache_reuse(stage=stage, result=cached_result)
                    return cached_result
                return self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.LOGICAL_CALL_DRIFT,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=link_input_hash,
                    logical_call_id=logical_call_id,
                    prior_state=state,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="RELEVANCE_INPUT_DRIFT",
                )
            binding_ids = tuple(value.record_id for value in record_bindings)
            support_ids = tuple(value.record_id for value in supported_records)
            if len(binding_ids) != len(set(binding_ids)) or len(support_ids) != len(
                set(support_ids)
            ):
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=link_input_hash,
                    logical_call_id=logical_call_id,
                    prior_state=current,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="DUPLICATE_ID",
                )
                self._relevance_cache[logical_call_id] = (cache_key, result)
                return result
            if not set(support_ids) <= set(binding_ids):
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.INPUT_REJECTED,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=link_input_hash,
                    logical_call_id=logical_call_id,
                    prior_state=current,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code="UNBOUND_SUPPORTED_RECORD",
                )
                self._relevance_cache[logical_call_id] = (cache_key, result)
                return result
            self._relevance_link_calls += 1
            path_states = {value.path_id: value.state for value in state.path_states}
            supported = {value.record_id: value for value in supported_records}
            records: list[RecordRelevanceResultV1] = []
            try:
                for binding in record_bindings:
                    if binding.path_independent:
                        relevance = RecordRelevance.PATH_INDEPENDENT
                    elif not binding.linked_path_ids:
                        relevance = RecordRelevance.UNKNOWN
                    elif any(
                        path_states.get(path_id) is PathViability.VIABLE
                        for path_id in binding.linked_path_ids
                    ):
                        relevance = RecordRelevance.ACTIVE_PATH
                    elif any(
                        path_states.get(path_id) is PathViability.UNKNOWN
                        for path_id in binding.linked_path_ids
                    ):
                        relevance = RecordRelevance.UNKNOWN
                    else:
                        relevance = RecordRelevance.INACTIVE_BRANCH
                    support = supported.get(binding.record_id)
                    archive = (
                        relevance is RecordRelevance.INACTIVE_BRANCH
                        and support is not None
                        and bool(binding.linked_path_ids)
                        and all(
                            path_states.get(path_id) is PathViability.INACTIVE
                            for path_id in binding.linked_path_ids
                        )
                        and state.topology.kind is TopologyKind.ISOLATED_HISTORY_FREE
                    )
                    records.append(
                        RecordRelevanceResultV1(
                            record_id=binding.record_id,
                            relevance=relevance,
                            linked_path_ids=binding.linked_path_ids,
                            supported_record_binding_sha256=(
                                None
                                if support is None
                                else supported_record_binding_sha256(support)
                            ),
                            disposition=(
                                RelevanceDisposition.ARCHIVE_SHADOW
                                if archive
                                else RelevanceDisposition.RETAIN
                            ),
                        )
                    )
                output = PathRelevanceOutputV1(
                    linkage_id=_stable_id("r23-link", link_input_hash),
                    logical_call_id=logical_call_id,
                    rubric_state_sha256=rubric_tracking_state_sha256(state),
                    records=tuple(records),
                    topology=state.topology,
                )
                validate_path_relevance_output(
                    output,
                    state,
                    rubric,
                    record_bindings,
                    supported_records,
                )
            except R23ContractError as error:
                result = self._audited_fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.OUTPUT_REJECTED,
                    descriptor=self._tracker_backend.descriptor,
                    input_sha256=link_input_hash,
                    logical_call_id=logical_call_id,
                    prior_state=current,
                    backend_called=False,
                    operation_started_ns=operation_started,
                    contract_code=error.code,
                )
                self._relevance_cache[logical_call_id] = (cache_key, result)
                return result
            output_hash = path_relevance_output_sha256(output)
            receipt = self._receipt(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                fallback=None,
                descriptor=self._tracker_backend.descriptor,
                input_sha256=link_input_hash,
                logical_call_id=logical_call_id,
                rubric=rubric,
                prior_state=state,
                final_state=state,
                raw_backend_output_sha256=None,
                parsed_output_sha256=None,
                admitted_output_sha256=output_hash,
                backend_calls=0,
                backend_latency_ns=0,
                admission_latency_ns=time.monotonic_ns() - operation_started,
                state_update_latency_ns=0,
                total_latency_ns=time.monotonic_ns() - operation_started,
                relevance=output,
                validation_checks=(
                    "POST_STATE_LINK_BOUND",
                    "RELEVANCE_DERIVED",
                    "ARCHIVE_SHADOW_ONLY",
                ),
            )
            try:
                receipt_sha256 = self._emit(receipt)
            except Exception:
                result = self._fallback(
                    stage=stage,
                    code=RubricSessionFallbackCode.SIDECAR_FAILURE,
                )
                self._record_metric(
                    stage=stage,
                    status=RubricEvaluationStatus.SIDECAR_FAILURE,
                    latency_ns=time.monotonic_ns() - operation_started,
                    backend_calls=0,
                    state=state,
                )
                self._relevance_cache[logical_call_id] = (cache_key, result)
                return result
            result = RubricSessionResultV1(
                stage=stage,
                status=RubricSessionStatus.ADMITTED,
                rubric=rubric,
                state=state,
                relevance=output,
                backend_called=False,
                receipt_sha256=receipt_sha256,
            )
            self._record_metric(
                stage=stage,
                status=RubricEvaluationStatus.ADMITTED,
                latency_ns=time.monotonic_ns() - operation_started,
                backend_calls=0,
                state=state,
                relevance=output,
            )
            self._relevance_cache[logical_call_id] = (cache_key, result)
            return result

    def link(
        self,
        *,
        state: RubricTrackingStateV1,
        record_bindings: tuple[RecordPathBindingV1, ...],
        supported_records: tuple[SupportedRecordBindingV1, ...],
        logical_call_id: str,
    ) -> PathRelevanceOutputV1:
        """Protocol adapter; callers wanting fallback values should use ``link_records``."""

        result = self.link_records(
            state=state,
            record_bindings=record_bindings,
            supported_records=supported_records,
            logical_call_id=logical_call_id,
        )
        if result.relevance is None:
            assert result.fallback is not None
            raise R23ContractError(
                result.fallback.contract_code or result.fallback.code.value,
                "path relevance was not admitted",
            )
        return result.relevance


__all__ = [
    "RubricSessionFallbackCode",
    "RubricSessionFallbackV1",
    "RubricSessionResultV1",
    "RubricSessionStage",
    "RubricSessionStatus",
    "RubricTaskSession",
]
