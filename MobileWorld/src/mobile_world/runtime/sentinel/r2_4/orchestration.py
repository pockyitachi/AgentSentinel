"""History-free orchestration for the independent R2.2 and R2.3 axes.

The coordinator is injected as the existing GPT56 policy's evidence factory.
That placement is deliberate: one Collector snapshot feeds both axes, the
history-free rubric is admitted before the history-policy transport can start,
and the outer runtime seam continues to see the exact R2.4 policy adapter it
already trusts.

Only the Collector bundle builder receives the actor request and History IR.
The rubric session factory receives a detached task instruction, while tracking
receives only the closed :class:`RubricEvidenceSnapshotV1` projection.  After
tracking, the deterministic relevance linker receives opaque History-IR record
IDs only; it receives no record text and has no archive authority.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic_ns
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
)
from mobile_world.offline.causal_replay.contracts import (
    canonical_sha256 as portable_canonical_sha256,
)
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import evidence_packet_sha256
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import GPT56EvidenceInputV1
from mobile_world.runtime.sentinel.r2_3.contracts import (
    R23ContractError,
    RecordPathBindingV1,
    RelevanceDisposition,
    TaskInstructionV1,
    TopologyDeclarationV1,
    TopologyKind,
    TopologyRunStatus,
    TopologyRunV1,
    path_relevance_output_sha256,
    rubric_sha256,
    rubric_tracking_state_sha256,
    snapshot_multi_path_rubric,
    snapshot_path_relevance_output,
    snapshot_task_instruction,
    snapshot_tracker_proposal,
    snapshot_tracking_state,
    tracker_proposal_sha256,
    tracking_packet_sha256,
)
from mobile_world.runtime.sentinel.r2_3.packet import (
    HistoryFreeTrackingPacketBuilderV1,
    RubricEvidenceSnapshotV1,
)
from mobile_world.runtime.sentinel.r2_3.session import (
    RubricSessionFallbackV1,
    RubricSessionResultV1,
    RubricSessionStage,
    RubricSessionStatus,
    RubricTaskSession,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceBundleV1,
    CollectorEvidenceError,
    CollectorEvidenceFactoryV1,
    CollectorRubricOnlyBundleV1,
    rubric_evidence_snapshot_sha256,
)

R24_ORCHESTRATED_CALL_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-orchestrated-call/v1"


class R24OrchestrationError(R24ContractError):
    """Typed fail-closed error raised before history-policy transport."""


RubricSessionFactoryV1 = Callable[[str, TaskInstructionV1], RubricTaskSession]


@runtime_checkable
class RubricCollectorCallObserverV1(Protocol):
    """History-free hook that binds only the current Collector screenshot.

    A production implementation pre-registers its case lease separately; this
    observer never receives that lease, actor history, History IR, or R2.2
    history targets.
    """

    def bind_collector_projection(
        self,
        *,
        stimulus: RubricEvidenceSnapshotV1,
        current_image_data_url: str,
        current_image_sha256: str,
        logical_call_id: str,
        actor_request_sha256: str,
    ) -> None: ...


def _snapshot_gpt56_input(value: GPT56EvidenceInputV1) -> GPT56EvidenceInputV1:
    if type(value) is not GPT56EvidenceInputV1:
        raise R24OrchestrationError(
            "UNTRUSTED_COLLECTOR_BUNDLE", "GPT evidence input has an untrusted type"
        )
    try:
        packet = deepcopy(value.packet)
        result = GPT56EvidenceInputV1(
            packet_id=value.packet_id,
            packet_canonical_bytes=bytes(value.packet_canonical_bytes),
            packet_sha256=value.packet_sha256,
            packet=packet,
            current_image_data_url=value.current_image_data_url,
            current_image_sha256=value.current_image_sha256,
            target_count=value.target_count,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24OrchestrationError(
            "UNTRUSTED_COLLECTOR_BUNDLE", "GPT evidence input could not be detached"
        ) from exc
    return result


def _snapshot_rubric_evidence(
    value: RubricEvidenceSnapshotV1,
) -> RubricEvidenceSnapshotV1:
    if type(value) is not RubricEvidenceSnapshotV1:
        raise R24OrchestrationError(
            "UNTRUSTED_COLLECTOR_BUNDLE", "rubric stimulus has an untrusted type"
        )
    try:
        result = deepcopy(value)
        if type(result) is not RubricEvidenceSnapshotV1:
            raise TypeError("rubric stimulus detach changed type")
        before = rubric_evidence_snapshot_sha256(value)
        after = rubric_evidence_snapshot_sha256(result)
    except (TypeError, ValueError, R23ContractError, RecursionError) as exc:
        raise R24OrchestrationError(
            "UNTRUSTED_COLLECTOR_BUNDLE", "rubric stimulus could not be detached"
        ) from exc
    if before != after:
        raise R24OrchestrationError(
            "COLLECTOR_BUNDLE_DRIFT", "rubric stimulus changed while being detached"
        )
    return result


def _snapshot_session_result(value: RubricSessionResultV1) -> RubricSessionResultV1:
    """Rebuild a detached public result without using session-private helpers."""

    if type(value) is not RubricSessionResultV1:
        raise R24OrchestrationError(
            "UNTRUSTED_RUBRIC_RESULT", "rubric session returned an untrusted result"
        )
    try:
        return RubricSessionResultV1(
            stage=value.stage,
            status=value.status,
            rubric=(None if value.rubric is None else snapshot_multi_path_rubric(value.rubric)),
            state=None if value.state is None else snapshot_tracking_state(value.state),
            proposal=(
                None if value.proposal is None else snapshot_tracker_proposal(value.proposal)
            ),
            relevance=(
                None if value.relevance is None else snapshot_path_relevance_output(value.relevance)
            ),
            fallback=(
                None
                if value.fallback is None
                else RubricSessionFallbackV1(
                    code=value.fallback.code,
                    contract_code=value.fallback.contract_code,
                )
            ),
            backend_called=value.backend_called,
            receipt_sha256=value.receipt_sha256,
        )
    except (TypeError, ValueError, R23ContractError, RecursionError) as exc:
        raise R24OrchestrationError(
            "UNTRUSTED_RUBRIC_RESULT", "rubric result could not be detached"
        ) from exc


def rubric_session_result_projection(
    value: RubricSessionResultV1,
) -> dict[str, JsonValue]:
    """Hash-only module-owned projection of an exact R2.3 session result."""

    trusted = _snapshot_session_result(value)
    return {
        "stage": trusted.stage.value,
        "status": trusted.status.value,
        "rubric_sha256": (None if trusted.rubric is None else rubric_sha256(trusted.rubric)),
        "state_sha256": (
            None if trusted.state is None else rubric_tracking_state_sha256(trusted.state)
        ),
        "proposal_sha256": (
            None if trusted.proposal is None else tracker_proposal_sha256(trusted.proposal)
        ),
        "relevance_sha256": (
            None if trusted.relevance is None else path_relevance_output_sha256(trusted.relevance)
        ),
        "fallback": (
            None
            if trusted.fallback is None
            else {
                "code": trusted.fallback.code.value,
                "contract_code": trusted.fallback.contract_code,
            }
        ),
        "backend_called": trusted.backend_called,
        "receipt_sha256": trusted.receipt_sha256,
    }


def rubric_session_result_sha256(value: RubricSessionResultV1) -> str:
    return canonical_sha256(cast(JsonValue, rubric_session_result_projection(value)))


def _snapshot_topology_run(value: TopologyRunV1) -> TopologyRunV1:
    if type(value) is not TopologyRunV1:
        raise R24OrchestrationError("UNTRUSTED_TOPOLOGY_RUN", "topology run has an untrusted type")
    return TopologyRunV1(
        topology=TopologyDeclarationV1(
            kind=value.topology.kind,
            independent_grounding_claim_eligible=(
                value.topology.independent_grounding_claim_eligible
            ),
        ),
        status=value.status,
        rubric_input_sha256=value.rubric_input_sha256,
        rubric_output_sha256=value.rubric_output_sha256,
        rubric_receipt_sha256=value.rubric_receipt_sha256,
        history_policy_input_sha256=value.history_policy_input_sha256,
        history_policy_output_sha256=value.history_policy_output_sha256,
        failure_code=value.failure_code,
        total_latency_ns=value.total_latency_ns,
    )


@dataclass(frozen=True, slots=True)
class R24CoordinatedCallRecordV1:
    """Detached in-memory binding for one admitted or failed rubric call."""

    logical_call_id: str
    task_run_id: str
    call_input_sha256: str
    evidence_snapshot_latency_ns: int
    history_free_stimulus_sha256: str
    gpt56_evidence_packet_sha256: str | None
    generation_result_sha256: str
    generation_result: RubricSessionResultV1
    tracking_packet_sha256: str | None
    rubric_result_sha256: str
    rubric_result: RubricSessionResultV1
    topology_run: TopologyRunV1
    schema_version: str = R24_ORCHESTRATED_CALL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R24_ORCHESTRATED_CALL_SCHEMA_VERSION:
            raise R24OrchestrationError(
                "UNKNOWN_SCHEMA_VERSION", "unknown orchestrated-call schema"
            )
        if type(self.logical_call_id) is not str or not self.logical_call_id:
            raise R24OrchestrationError(
                "INVALID_RUNTIME_ID", "logical call ID must be non-empty text"
            )
        if type(self.task_run_id) is not str or not self.task_run_id:
            raise R24OrchestrationError("INVALID_RUNTIME_ID", "task run ID must be non-empty text")
        if (
            type(self.evidence_snapshot_latency_ns) is not int
            or self.evidence_snapshot_latency_ns < 0
        ):
            raise R24OrchestrationError(
                "INVALID_LATENCY", "evidence snapshot latency must be non-negative"
            )
        for name in (
            "call_input_sha256",
            "history_free_stimulus_sha256",
            "generation_result_sha256",
            "rubric_result_sha256",
        ):
            value = getattr(self, name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(item not in "0123456789abcdef" for item in value)
            ):
                raise R24OrchestrationError("INVALID_SHA256", f"{name} is invalid")
        if self.gpt56_evidence_packet_sha256 is not None and (
            type(self.gpt56_evidence_packet_sha256) is not str
            or len(self.gpt56_evidence_packet_sha256) != 64
            or any(item not in "0123456789abcdef" for item in self.gpt56_evidence_packet_sha256)
        ):
            raise R24OrchestrationError("INVALID_SHA256", "gpt56_evidence_packet_sha256 is invalid")
        if self.tracking_packet_sha256 is not None and (
            type(self.tracking_packet_sha256) is not str
            or len(self.tracking_packet_sha256) != 64
            or any(item not in "0123456789abcdef" for item in self.tracking_packet_sha256)
        ):
            raise R24OrchestrationError("INVALID_SHA256", "tracking_packet_sha256 is invalid")
        if (
            type(self.generation_result) is not RubricSessionResultV1
            or type(self.rubric_result) is not RubricSessionResultV1
        ):
            raise R24OrchestrationError(
                "UNTRUSTED_RUBRIC_RESULT", "call record needs exact rubric results"
            )
        if type(self.topology_run) is not TopologyRunV1:
            raise R24OrchestrationError(
                "UNTRUSTED_TOPOLOGY_RUN", "call record needs an exact topology run"
            )
        if (
            rubric_session_result_sha256(self.generation_result) != self.generation_result_sha256
            or self.generation_result.stage is not RubricSessionStage.TASK_START_GENERATE
            or rubric_session_result_sha256(self.rubric_result) != self.rubric_result_sha256
        ):
            raise R24OrchestrationError(
                "RUBRIC_RESULT_HASH_MISMATCH", "rubric result differs from its bound hash"
            )
        if (
            self.topology_run.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE
            or not self.topology_run.topology.independent_grounding_claim_eligible
            or self.topology_run.history_policy_input_sha256 is not None
            or self.topology_run.history_policy_output_sha256 is not None
            or self.topology_run.rubric_input_sha256 != self.history_free_stimulus_sha256
        ):
            raise R24OrchestrationError(
                "RUBRIC_HISTORY_ISOLATION_BROKEN",
                "topology run is not bound to the history-free stimulus",
            )
        admitted = self.rubric_result.status is RubricSessionStatus.ADMITTED
        if admitted:
            if (
                self.generation_result.status is not RubricSessionStatus.ADMITTED
                or self.rubric_result.stage is not RubricSessionStage.LINK_RELEVANCE
                or self.rubric_result.rubric is None
                or self.rubric_result.state is None
                or self.rubric_result.relevance is None
                or self.rubric_result.fallback is not None
                or self.rubric_result.receipt_sha256 is None
                or self.tracking_packet_sha256 is None
                or self.topology_run.status is not TopologyRunStatus.ADMITTED
                or self.topology_run.rubric_output_sha256
                != path_relevance_output_sha256(self.rubric_result.relevance)
                or self.topology_run.rubric_receipt_sha256 != self.rubric_result.receipt_sha256
                or self.topology_run.failure_code is not None
            ):
                raise R24OrchestrationError(
                    "ORCHESTRATION_BINDING_MISMATCH",
                    "admitted rubric result and topology run differ",
                )
            if self.rubric_result.rubric.task_run_id != self.task_run_id:
                raise R24OrchestrationError(
                    "ORCHESTRATION_BINDING_MISMATCH", "rubric binds another task run"
                )
            if self.rubric_result.state.logical_call_id != self.logical_call_id:
                raise R24OrchestrationError(
                    "ORCHESTRATION_BINDING_MISMATCH", "rubric state binds another call"
                )
            if self.rubric_result.relevance.logical_call_id != self.logical_call_id or any(
                item.disposition is not RelevanceDisposition.RETAIN
                or item.supported_record_binding_sha256 is not None
                for item in self.rubric_result.relevance.records
            ):
                raise R24OrchestrationError(
                    "UNAUTHORIZED_ARCHIVE",
                    "R2.4 has no trusted R2.2 resolver and must retain every record",
                )
        elif (
            self.rubric_result.fallback is None
            or self.topology_run.status is not TopologyRunStatus.FALLBACK
            or self.topology_run.failure_code is None
            or self.topology_run.failure_code
            != R24RuntimeCoordinatorV1._fallback_code(self.rubric_result)
            or self.topology_run.rubric_output_sha256 is not None
            or self.topology_run.rubric_receipt_sha256 is not None
            or (
                self.rubric_result.stage is RubricSessionStage.TASK_START_GENERATE
                and (
                    self.generation_result_sha256 != self.rubric_result_sha256
                    or self.tracking_packet_sha256 is not None
                )
            )
            or (
                self.rubric_result.stage
                in {RubricSessionStage.TRACK, RubricSessionStage.LINK_RELEVANCE}
                and (
                    self.generation_result.status is not RubricSessionStatus.ADMITTED
                    or self.tracking_packet_sha256 is None
                )
            )
            or self.rubric_result.stage is RubricSessionStage.EXPLICIT_REVISION
        ):
            raise R24OrchestrationError(
                "ORCHESTRATION_BINDING_MISMATCH",
                "fallback rubric result and topology run differ",
            )


def _snapshot_call_record(value: R24CoordinatedCallRecordV1) -> R24CoordinatedCallRecordV1:
    if type(value) is not R24CoordinatedCallRecordV1:
        raise R24OrchestrationError("UNTRUSTED_CALL_RECORD", "call record has an untrusted type")
    return R24CoordinatedCallRecordV1(
        logical_call_id=value.logical_call_id,
        task_run_id=value.task_run_id,
        call_input_sha256=value.call_input_sha256,
        evidence_snapshot_latency_ns=value.evidence_snapshot_latency_ns,
        history_free_stimulus_sha256=value.history_free_stimulus_sha256,
        gpt56_evidence_packet_sha256=value.gpt56_evidence_packet_sha256,
        generation_result_sha256=value.generation_result_sha256,
        generation_result=_snapshot_session_result(value.generation_result),
        tracking_packet_sha256=value.tracking_packet_sha256,
        rubric_result_sha256=value.rubric_result_sha256,
        rubric_result=_snapshot_session_result(value.rubric_result),
        topology_run=_snapshot_topology_run(value.topology_run),
        schema_version=value.schema_version,
    )


def r24_coordinated_call_record_projection(
    value: R24CoordinatedCallRecordV1,
) -> dict[str, JsonValue]:
    trusted = _snapshot_call_record(value)
    run = trusted.topology_run
    return {
        "schema_version": trusted.schema_version,
        "logical_call_id": trusted.logical_call_id,
        "task_run_id": trusted.task_run_id,
        "call_input_sha256": trusted.call_input_sha256,
        "evidence_snapshot_latency_ns": trusted.evidence_snapshot_latency_ns,
        "history_free_stimulus_sha256": trusted.history_free_stimulus_sha256,
        "gpt56_evidence_packet_sha256": trusted.gpt56_evidence_packet_sha256,
        "generation_result_sha256": trusted.generation_result_sha256,
        "tracking_packet_sha256": trusted.tracking_packet_sha256,
        "rubric_result_sha256": trusted.rubric_result_sha256,
        "topology_run": {
            "kind": run.topology.kind.value,
            "independent_grounding_claim_eligible": (
                run.topology.independent_grounding_claim_eligible
            ),
            "status": run.status.value,
            "rubric_input_sha256": run.rubric_input_sha256,
            "rubric_output_sha256": run.rubric_output_sha256,
            "rubric_receipt_sha256": run.rubric_receipt_sha256,
            "history_policy_input_sha256": run.history_policy_input_sha256,
            "history_policy_output_sha256": run.history_policy_output_sha256,
            "failure_code": run.failure_code,
            "total_latency_ns": run.total_latency_ns,
        },
    }


def r24_coordinated_call_record_sha256(value: R24CoordinatedCallRecordV1) -> str:
    return canonical_sha256(cast(JsonValue, r24_coordinated_call_record_projection(value)))


@dataclass(frozen=True, slots=True)
class _CachedCall:
    call_input_sha256: str
    gpt56_input: GPT56EvidenceInputV1 | None
    record: R24CoordinatedCallRecordV1 | None
    failure_code: str | None


class R24RuntimeCoordinatorV1:
    """One-read, rubric-first evidence factory for the common policy seam."""

    def __init__(
        self,
        *,
        collector: CollectorEvidenceFactoryV1,
        session_factory: RubricSessionFactoryV1,
        rubric_call_observer: RubricCollectorCallObserverV1 | None = None,
    ) -> None:
        if type(collector) is not CollectorEvidenceFactoryV1:
            raise TypeError("collector must use exact CollectorEvidenceFactoryV1")
        if not callable(session_factory):
            raise TypeError("session_factory must be callable")
        if rubric_call_observer is not None and not isinstance(
            rubric_call_observer, RubricCollectorCallObserverV1
        ):
            raise TypeError("rubric_call_observer must implement its history-free interface")
        # Bind the method once so later attribute replacement cannot redirect an
        # already-configured coordinator.
        self._bundle_for_call = collector.bundle_for_call
        self._rubric_only_bundle_for_no_history_call = (
            collector.rubric_only_bundle_for_no_history_call
        )
        self._session_factory = session_factory
        self._bind_rubric_collector_projection = (
            None if rubric_call_observer is None else rubric_call_observer.bind_collector_projection
        )
        self._lock = RLock()
        self._sessions: dict[str, tuple[str, RubricTaskSession]] = {}
        self._calls: dict[str, _CachedCall] = {}
        self._stimulus_calls: dict[tuple[str, str], str] = {}
        self._collector_bundle_calls = 0

    @staticmethod
    def _call_input_sha256(
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> str:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise R24OrchestrationError(
                "UNTRUSTED_CALL_INPUT", "context and History IR need exact trusted types"
            )
        try:
            request_sha256 = portable_canonical_sha256(request)
            history_ir_sha256 = portable_canonical_sha256(cast(JsonValue, history_ir.to_dict()))
            return canonical_sha256(
                cast(
                    JsonValue,
                    {
                        "request_sha256": request_sha256,
                        "context": {
                            "logical_call_id": context.logical_call_id,
                            "host_id": context.host_id,
                            "attributes": context.attributes,
                        },
                        "history_ir_sha256": history_ir_sha256,
                    },
                )
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise R24OrchestrationError(
                "UNTRUSTED_CALL_INPUT", "call input could not be canonically bound"
            ) from exc

    @staticmethod
    def _no_history_call_input_sha256(request: JsonValue, context: SentinelContext) -> str:
        if type(context) is not SentinelContext:
            raise R24OrchestrationError(
                "UNTRUSTED_CALL_INPUT", "context needs its exact trusted type"
            )
        try:
            return canonical_sha256(
                cast(
                    JsonValue,
                    {
                        "request_sha256": portable_canonical_sha256(request),
                        "context": {
                            "logical_call_id": context.logical_call_id,
                            "host_id": context.host_id,
                            "attributes": context.attributes,
                        },
                        "history_status": "NO_HISTORY",
                    },
                )
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise R24OrchestrationError(
                "UNTRUSTED_CALL_INPUT", "no-history input could not be canonically bound"
            ) from exc

    @staticmethod
    def _validate_bundle_axis_binding(
        bundle: CollectorEvidenceBundleV1,
        gpt56_input: GPT56EvidenceInputV1,
        stimulus: RubricEvidenceSnapshotV1,
    ) -> None:
        if type(bundle) is not CollectorEvidenceBundleV1:
            raise R24OrchestrationError(
                "UNTRUSTED_COLLECTOR_BUNDLE", "Collector returned an untrusted bundle"
            )
        packet = gpt56_input.packet
        if (
            evidence_packet_sha256(packet) != gpt56_input.packet_sha256
            or evidence_packet_sha256(bundle.r22_packet) != gpt56_input.packet_sha256
            or packet != bundle.r22_packet
            or packet.cutoff.run_id != stimulus.cutoff.run_id
            or packet.cutoff.task_run_id != stimulus.task_run_id
            or packet.cutoff.step_id != stimulus.step_id
            or packet.cutoff.current_observation_event_id
            != stimulus.cutoff.current_observation_event_id
            or packet.cutoff.cutoff_event_seq != stimulus.cutoff.cutoff_event_seq
            or packet.task.source_event_id != stimulus.task.source_event_id
            or packet.task.source_event_seq != stimulus.task.source_event_seq
            or packet.task.exact_text != stimulus.task.exact_text
            or packet.task.text_sha256 != stimulus.task.text_sha256
            or packet.current_observation.source_event_id
            != stimulus.current_observation.source_event_id
            or packet.current_observation.source_event_seq
            != stimulus.current_observation.source_event_seq
            or packet.current_observation.screenshot_evidence_id
            != stimulus.current_observation.screenshot_evidence_id
            or packet.current_observation.screenshot_content_sha256
            != stimulus.current_observation.screenshot_content_sha256
            or packet.current_observation.accessibility_evidence_ids
            != stimulus.current_observation.accessibility_evidence_ids
        ):
            raise R24OrchestrationError(
                "COLLECTOR_AXIS_BINDING_MISMATCH",
                "R2.2 and R2.3 projections do not share one causal stimulus",
            )

    def _session_for(self, task_run_id: str, task: TaskInstructionV1) -> RubricTaskSession:
        task_snapshot = snapshot_task_instruction(task)
        task_hash = canonical_sha256(
            cast(
                JsonValue,
                {
                    "source_event_id": task_snapshot.source_event_id,
                    "source_event_seq": task_snapshot.source_event_seq,
                    "exact_text": task_snapshot.exact_text,
                    "text_sha256": task_snapshot.text_sha256,
                    "source_event_type": task_snapshot.source_event_type.value,
                },
            )
        )
        cached = self._sessions.get(task_run_id)
        if cached is not None:
            cached_task_hash, session = cached
            if cached_task_hash != task_hash:
                raise R24OrchestrationError(
                    "TASK_INSTRUCTION_DRIFT",
                    "one task run cannot silently replace its instruction",
                )
            return session
        try:
            untrusted = self._session_factory(
                task_run_id,
                snapshot_task_instruction(task_snapshot),
            )
        except Exception as exc:
            raise R24OrchestrationError(
                "RUBRIC_SESSION_FACTORY_FAILED", "rubric session construction failed"
            ) from exc
        if type(untrusted) is not RubricTaskSession:
            raise R24OrchestrationError(
                "UNTRUSTED_RUBRIC_SESSION",
                "session factory must return exact RubricTaskSession",
            )
        self._sessions[task_run_id] = (task_hash, untrusted)
        return untrusted

    @staticmethod
    def _fallback_code(result: RubricSessionResultV1) -> str:
        if result.stage is RubricSessionStage.TASK_START_GENERATE:
            return "RUBRIC_TASK_START_FALLBACK"
        if result.stage is RubricSessionStage.LINK_RELEVANCE:
            return "RUBRIC_RELEVANCE_FALLBACK"
        return "RUBRIC_TRACK_FALLBACK"

    @staticmethod
    def _topology_run(
        *,
        stimulus_sha256: str,
        result: RubricSessionResultV1,
        latency_ns: int,
    ) -> TopologyRunV1:
        admitted = result.status is RubricSessionStatus.ADMITTED
        return TopologyRunV1(
            topology=TopologyDeclarationV1(
                kind=TopologyKind.ISOLATED_HISTORY_FREE,
                independent_grounding_claim_eligible=True,
            ),
            status=(TopologyRunStatus.ADMITTED if admitted else TopologyRunStatus.FALLBACK),
            rubric_input_sha256=stimulus_sha256,
            rubric_output_sha256=(
                path_relevance_output_sha256(result.relevance)
                if admitted and result.relevance is not None
                else None
            ),
            rubric_receipt_sha256=(result.receipt_sha256 if admitted else None),
            history_policy_input_sha256=None,
            history_policy_output_sha256=None,
            failure_code=None if admitted else R24RuntimeCoordinatorV1._fallback_code(result),
            total_latency_ns=latency_ns,
        )

    def _record(
        self,
        *,
        context: SentinelContext,
        call_input_sha256: str,
        evidence_snapshot_latency_ns: int,
        gpt56_input: GPT56EvidenceInputV1 | None,
        stimulus: RubricEvidenceSnapshotV1,
        generation: RubricSessionResultV1,
        tracking_packet_hash: str | None,
        result: RubricSessionResultV1,
        topology_run: TopologyRunV1,
    ) -> R24CoordinatedCallRecordV1:
        record = R24CoordinatedCallRecordV1(
            logical_call_id=context.logical_call_id,
            task_run_id=stimulus.task_run_id,
            call_input_sha256=call_input_sha256,
            evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
            history_free_stimulus_sha256=rubric_evidence_snapshot_sha256(stimulus),
            gpt56_evidence_packet_sha256=(
                None if gpt56_input is None else gpt56_input.packet_sha256
            ),
            generation_result_sha256=rubric_session_result_sha256(generation),
            generation_result=_snapshot_session_result(generation),
            tracking_packet_sha256=tracking_packet_hash,
            rubric_result_sha256=rubric_session_result_sha256(result),
            rubric_result=_snapshot_session_result(result),
            topology_run=_snapshot_topology_run(topology_run),
        )
        return _snapshot_call_record(record)

    def __call__(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> GPT56EvidenceInputV1:
        """Run isolated rubric tracking before returning policy evidence."""

        call_input_sha256 = self._call_input_sha256(request, context, history_ir)
        with self._lock:
            cached = self._calls.get(context.logical_call_id)
            if cached is not None:
                if cached.call_input_sha256 != call_input_sha256:
                    raise R24OrchestrationError(
                        "LOGICAL_CALL_INPUT_DRIFT",
                        "one logical call was reused with different authority inputs",
                    )
                if cached.failure_code is not None:
                    raise R24OrchestrationError(
                        cached.failure_code, "cached rubric orchestration fallback"
                    )
                assert cached.gpt56_input is not None
                return _snapshot_gpt56_input(cached.gpt56_input)

            evidence_started_ns = monotonic_ns()
            try:
                self._collector_bundle_calls += 1
                bundle = self._bundle_for_call(
                    request=request,
                    context=context,
                    history_ir=history_ir,
                )
            except CollectorEvidenceError as exc:
                failure_code = "COLLECTOR_EVIDENCE_FAILED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, f"Collector evidence failed with {exc.code}"
                ) from exc
            except Exception as exc:
                failure_code = "COLLECTOR_EVIDENCE_FAILED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "Collector evidence construction failed"
                ) from exc
            evidence_snapshot_latency_ns = monotonic_ns() - evidence_started_ns

            try:
                if type(bundle) is not CollectorEvidenceBundleV1:
                    raise R24OrchestrationError(
                        "UNTRUSTED_COLLECTOR_BUNDLE",
                        "Collector returned an untrusted bundle",
                    )
                gpt56_input = _snapshot_gpt56_input(bundle.gpt56_input)
                stimulus = _snapshot_rubric_evidence(bundle.r23_snapshot)
                self._validate_bundle_axis_binding(bundle, gpt56_input, stimulus)
                if self._bind_rubric_collector_projection is not None:
                    self._bind_rubric_collector_projection(
                        stimulus=_snapshot_rubric_evidence(stimulus),
                        current_image_data_url=gpt56_input.current_image_data_url,
                        current_image_sha256=gpt56_input.current_image_sha256,
                        logical_call_id=context.logical_call_id,
                        actor_request_sha256=history_ir.raw_request_sha256,
                    )
                stimulus_sha256 = rubric_evidence_snapshot_sha256(stimulus)
                stimulus_key = (stimulus.task_run_id, stimulus_sha256)
                prior_logical_call_id = self._stimulus_calls.get(stimulus_key)
                if (
                    prior_logical_call_id is not None
                    and prior_logical_call_id != context.logical_call_id
                ):
                    raise R24OrchestrationError(
                        "DUPLICATE_RUBRIC_STIMULUS",
                        "one Collector observation cannot advance two logical calls",
                    )
                self._stimulus_calls[stimulus_key] = context.logical_call_id
                session = self._session_for(stimulus.task_run_id, stimulus.task)
            except R24OrchestrationError as exc:
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=exc.code,
                )
                raise
            except Exception as exc:
                failure_code = "RUBRIC_INPUT_REJECTED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "rubric input binding failed") from exc

            rubric_started_ns = monotonic_ns()
            try:
                generation = _snapshot_session_result(session.start())
            except Exception as exc:
                failure_code = "RUBRIC_TASK_START_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "rubric task-start escaped its typed boundary"
                ) from exc
            if generation.status is not RubricSessionStatus.ADMITTED:
                topology = self._topology_run(
                    stimulus_sha256=stimulus_sha256,
                    result=generation,
                    latency_ns=monotonic_ns() - rubric_started_ns,
                )
                record = self._record(
                    context=context,
                    call_input_sha256=call_input_sha256,
                    evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                    gpt56_input=gpt56_input,
                    stimulus=stimulus,
                    generation=generation,
                    tracking_packet_hash=None,
                    result=generation,
                    topology_run=topology,
                )
                failure_code = self._fallback_code(generation)
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "rubric task-start failed closed")

            current_rubric = session.rubric
            current_state = session.state
            if current_rubric is None or current_state is None:
                raise R24OrchestrationError(
                    "UNTRUSTED_RUBRIC_RESULT", "admitted task-start omitted rubric state"
                )
            if (
                current_rubric.task_run_id != stimulus.task_run_id
                or current_rubric.task != stimulus.task
            ):
                failure_code = "RUBRIC_TASK_BINDING_MISMATCH"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "generated rubric binds another Collector task"
                )

            packet_hash: str | None = None
            try:
                packet = HistoryFreeTrackingPacketBuilderV1().build(
                    packet_id=f"r24-track-{stimulus_sha256[:32]}-{context.logical_call_id[:32]}",
                    logical_call_id=context.logical_call_id,
                    rubric=snapshot_multi_path_rubric(current_rubric),
                    prior_state=snapshot_tracking_state(current_state),
                    snapshot=_snapshot_rubric_evidence(stimulus),
                )
                packet_hash = tracking_packet_sha256(packet)
                result = _snapshot_session_result(session.track(packet))
            except R23ContractError:
                # Ask the session to turn a construction rejection into its
                # normal typed/audited fallback result.
                result = _snapshot_session_result(
                    session.track_step(
                        logical_call_id=context.logical_call_id,
                        cutoff=stimulus.cutoff,
                        current_observation=stimulus.current_observation,
                        evidence_index=stimulus.evidence_index,
                        packet_id=(
                            f"r24-track-{stimulus_sha256[:32]}-{context.logical_call_id[:32]}"
                        ),
                    )
                )
            except Exception as exc:
                failure_code = "RUBRIC_TRACK_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "rubric tracking escaped its typed boundary"
                ) from exc

            if result.status is not RubricSessionStatus.ADMITTED:
                topology = self._topology_run(
                    stimulus_sha256=stimulus_sha256,
                    result=result,
                    latency_ns=monotonic_ns() - rubric_started_ns,
                )
                record = self._record(
                    context=context,
                    call_input_sha256=call_input_sha256,
                    evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                    gpt56_input=gpt56_input,
                    stimulus=stimulus,
                    generation=generation,
                    tracking_packet_hash=packet_hash,
                    result=result,
                    topology_run=topology,
                )
                failure_code = self._fallback_code(result)
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "rubric tracking failed closed")

            if result.state is None:
                raise R24OrchestrationError(
                    "UNTRUSTED_RUBRIC_RESULT", "admitted tracking omitted its state"
                )
            # There is no trusted record-level R2.2 SUPPORTED+KEEP resolver in
            # this checkpoint.  Bind only opaque record IDs, leave every path
            # association unknown, and require the R2.3 linker to emit RETAIN.
            # No record text, History IR, request, model identity, or policy
            # verdict enters rubric generation/tracking.
            try:
                record_bindings = tuple(
                    RecordPathBindingV1(
                        record_id=record.record_id,
                        linked_path_ids=(),
                        path_independent=False,
                    )
                    for record in history_ir.records
                )
                relevance = _snapshot_session_result(
                    session.link_records(
                        state=snapshot_tracking_state(result.state),
                        record_bindings=record_bindings,
                        supported_records=(),
                        logical_call_id=context.logical_call_id,
                    )
                )
            except Exception as exc:
                failure_code = "RUBRIC_RELEVANCE_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "path-relevance linking escaped its typed boundary"
                ) from exc
            topology = self._topology_run(
                stimulus_sha256=stimulus_sha256,
                result=relevance,
                latency_ns=monotonic_ns() - rubric_started_ns,
            )
            record = self._record(
                context=context,
                call_input_sha256=call_input_sha256,
                evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                gpt56_input=gpt56_input,
                stimulus=stimulus,
                generation=generation,
                tracking_packet_hash=packet_hash,
                result=relevance,
                topology_run=topology,
            )
            if relevance.status is not RubricSessionStatus.ADMITTED:
                failure_code = "RUBRIC_RELEVANCE_FALLBACK"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "path-relevance linking failed closed")

            cached_input = _snapshot_gpt56_input(gpt56_input)
            self._calls[context.logical_call_id] = _CachedCall(
                call_input_sha256=call_input_sha256,
                gpt56_input=cached_input,
                record=record,
                failure_code=None,
            )
            return _snapshot_gpt56_input(cached_input)

    def prepare_no_history(
        self,
        request: JsonValue,
        context: SentinelContext,
    ) -> R24CoordinatedCallRecordV1:
        """Advance only the history-free rubric axis for a typed no-history call.

        No History IR, actor-history target packet, or history-policy transport is
        created. The task session is shared with later history-bearing calls, so
        task-start generation remains exactly once while every new causal cutoff
        is tracked exactly once.
        """

        call_input_sha256 = self._no_history_call_input_sha256(request, context)
        actor_request_sha256 = portable_canonical_sha256(request)
        with self._lock:
            cached = self._calls.get(context.logical_call_id)
            if cached is not None:
                if cached.call_input_sha256 != call_input_sha256:
                    raise R24OrchestrationError(
                        "LOGICAL_CALL_INPUT_DRIFT",
                        "one logical call was reused with different authority inputs",
                    )
                if cached.failure_code is not None:
                    raise R24OrchestrationError(
                        cached.failure_code, "cached rubric orchestration fallback"
                    )
                if cached.gpt56_input is not None or cached.record is None:
                    raise R24OrchestrationError(
                        "NO_HISTORY_CACHE_MISMATCH",
                        "cached no-history call contains a history-policy packet",
                    )
                return _snapshot_call_record(cached.record)

            evidence_started_ns = monotonic_ns()
            try:
                self._collector_bundle_calls += 1
                bundle = self._rubric_only_bundle_for_no_history_call(
                    request=request,
                    context=context,
                )
            except CollectorEvidenceError as exc:
                failure_code = "COLLECTOR_EVIDENCE_FAILED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, f"Collector evidence failed with {exc.code}"
                ) from exc
            except Exception as exc:
                failure_code = "COLLECTOR_EVIDENCE_FAILED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "Collector rubric-only evidence construction failed"
                ) from exc
            evidence_snapshot_latency_ns = monotonic_ns() - evidence_started_ns

            try:
                if type(bundle) is not CollectorRubricOnlyBundleV1:
                    raise R24OrchestrationError(
                        "UNTRUSTED_COLLECTOR_BUNDLE",
                        "Collector returned an untrusted rubric-only bundle",
                    )
                stimulus = _snapshot_rubric_evidence(bundle.r23_snapshot)
                if self._bind_rubric_collector_projection is not None:
                    self._bind_rubric_collector_projection(
                        stimulus=_snapshot_rubric_evidence(stimulus),
                        current_image_data_url=bundle.current_image_data_url,
                        current_image_sha256=bundle.current_image_sha256,
                        logical_call_id=context.logical_call_id,
                        actor_request_sha256=actor_request_sha256,
                    )
                stimulus_sha256 = rubric_evidence_snapshot_sha256(stimulus)
                stimulus_key = (stimulus.task_run_id, stimulus_sha256)
                prior_logical_call_id = self._stimulus_calls.get(stimulus_key)
                if (
                    prior_logical_call_id is not None
                    and prior_logical_call_id != context.logical_call_id
                ):
                    raise R24OrchestrationError(
                        "DUPLICATE_RUBRIC_STIMULUS",
                        "one Collector observation cannot advance two logical calls",
                    )
                self._stimulus_calls[stimulus_key] = context.logical_call_id
                session = self._session_for(stimulus.task_run_id, stimulus.task)
            except R24OrchestrationError as exc:
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=exc.code,
                )
                raise
            except Exception as exc:
                failure_code = "RUBRIC_INPUT_REJECTED"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "rubric-only input binding failed"
                ) from exc

            rubric_started_ns = monotonic_ns()
            try:
                generation = _snapshot_session_result(session.start())
            except Exception as exc:
                failure_code = "RUBRIC_TASK_START_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "rubric task-start escaped its typed boundary"
                ) from exc
            if generation.status is not RubricSessionStatus.ADMITTED:
                topology = self._topology_run(
                    stimulus_sha256=stimulus_sha256,
                    result=generation,
                    latency_ns=monotonic_ns() - rubric_started_ns,
                )
                record = self._record(
                    context=context,
                    call_input_sha256=call_input_sha256,
                    evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                    gpt56_input=None,
                    stimulus=stimulus,
                    generation=generation,
                    tracking_packet_hash=None,
                    result=generation,
                    topology_run=topology,
                )
                failure_code = self._fallback_code(generation)
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "rubric task-start failed closed")

            current_rubric = session.rubric
            current_state = session.state
            if current_rubric is None or current_state is None:
                raise R24OrchestrationError(
                    "UNTRUSTED_RUBRIC_RESULT", "admitted task-start omitted rubric state"
                )
            if (
                current_rubric.task_run_id != stimulus.task_run_id
                or current_rubric.task != stimulus.task
            ):
                failure_code = "RUBRIC_TASK_BINDING_MISMATCH"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "generated rubric binds another Collector task"
                )

            packet_hash: str | None = None
            try:
                packet = HistoryFreeTrackingPacketBuilderV1().build(
                    packet_id=f"r24-track-{stimulus_sha256[:32]}-{context.logical_call_id[:32]}",
                    logical_call_id=context.logical_call_id,
                    rubric=snapshot_multi_path_rubric(current_rubric),
                    prior_state=snapshot_tracking_state(current_state),
                    snapshot=_snapshot_rubric_evidence(stimulus),
                )
                packet_hash = tracking_packet_sha256(packet)
                result = _snapshot_session_result(session.track(packet))
            except R23ContractError:
                result = _snapshot_session_result(
                    session.track_step(
                        logical_call_id=context.logical_call_id,
                        cutoff=stimulus.cutoff,
                        current_observation=stimulus.current_observation,
                        evidence_index=stimulus.evidence_index,
                        packet_id=(
                            f"r24-track-{stimulus_sha256[:32]}-{context.logical_call_id[:32]}"
                        ),
                    )
                )
            except Exception as exc:
                failure_code = "RUBRIC_TRACK_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "rubric tracking escaped its typed boundary"
                ) from exc
            if result.status is not RubricSessionStatus.ADMITTED:
                topology = self._topology_run(
                    stimulus_sha256=stimulus_sha256,
                    result=result,
                    latency_ns=monotonic_ns() - rubric_started_ns,
                )
                record = self._record(
                    context=context,
                    call_input_sha256=call_input_sha256,
                    evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                    gpt56_input=None,
                    stimulus=stimulus,
                    generation=generation,
                    tracking_packet_hash=packet_hash,
                    result=result,
                    topology_run=topology,
                )
                failure_code = self._fallback_code(result)
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "rubric tracking failed closed")
            if result.state is None:
                raise R24OrchestrationError(
                    "UNTRUSTED_RUBRIC_RESULT", "admitted tracking omitted its state"
                )
            try:
                relevance = _snapshot_session_result(
                    session.link_records(
                        state=snapshot_tracking_state(result.state),
                        record_bindings=(),
                        supported_records=(),
                        logical_call_id=context.logical_call_id,
                    )
                )
            except Exception as exc:
                failure_code = "RUBRIC_RELEVANCE_ERROR"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=None,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(
                    failure_code, "empty path-relevance linking escaped its typed boundary"
                ) from exc
            topology = self._topology_run(
                stimulus_sha256=stimulus_sha256,
                result=relevance,
                latency_ns=monotonic_ns() - rubric_started_ns,
            )
            record = self._record(
                context=context,
                call_input_sha256=call_input_sha256,
                evidence_snapshot_latency_ns=evidence_snapshot_latency_ns,
                gpt56_input=None,
                stimulus=stimulus,
                generation=generation,
                tracking_packet_hash=packet_hash,
                result=relevance,
                topology_run=topology,
            )
            if relevance.status is not RubricSessionStatus.ADMITTED:
                failure_code = "RUBRIC_RELEVANCE_FALLBACK"
                self._calls[context.logical_call_id] = _CachedCall(
                    call_input_sha256=call_input_sha256,
                    gpt56_input=None,
                    record=record,
                    failure_code=failure_code,
                )
                raise R24OrchestrationError(failure_code, "path-relevance linking failed closed")
            self._calls[context.logical_call_id] = _CachedCall(
                call_input_sha256=call_input_sha256,
                gpt56_input=None,
                record=record,
                failure_code=None,
            )
            return _snapshot_call_record(record)

    def record_for(self, logical_call_id: str) -> R24CoordinatedCallRecordV1 | None:
        if type(logical_call_id) is not str or not logical_call_id:
            raise TypeError("logical_call_id must be non-empty exact text")
        with self._lock:
            cached = self._calls.get(logical_call_id)
            if cached is None or cached.record is None:
                return None
            return _snapshot_call_record(cached.record)

    @property
    def records(self) -> tuple[R24CoordinatedCallRecordV1, ...]:
        with self._lock:
            return tuple(
                _snapshot_call_record(value.record)
                for value in self._calls.values()
                if value.record is not None
            )

    @property
    def task_session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    @property
    def logical_call_count(self) -> int:
        with self._lock:
            return len(self._calls)

    @property
    def collector_bundle_calls(self) -> int:
        with self._lock:
            return self._collector_bundle_calls


__all__ = [
    "R24_ORCHESTRATED_CALL_SCHEMA_VERSION",
    "R24CoordinatedCallRecordV1",
    "R24OrchestrationError",
    "R24RuntimeCoordinatorV1",
    "RubricCollectorCallObserverV1",
    "RubricSessionFactoryV1",
    "r24_coordinated_call_record_projection",
    "r24_coordinated_call_record_sha256",
    "rubric_session_result_projection",
    "rubric_session_result_sha256",
]
