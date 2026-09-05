"""Module-owned CPU producer for the R2.4 topology comparison artifact.

This module is deliberately incapable of live work.  It builds one Collector
snapshot from a checked-in captured Qwen request, then runs the exact same
snapshot through two fresh executions of the real R2.3 ``RubricTaskSession``
and the real R2.2 ``GPT56SentinelPolicy``.  Their transports are injected,
data-only CPU fakes.  No network client, GPU, backend, emulator, tool, or actor
action is constructed.

The isolated execution authorizes two separately controlled stages.  The
joint comparison authorizes one non-independent compound stage.  Both still
exercise the component contracts, schema validation, admission, receipts, and
measured wall-clock latency; neither accepts a caller-supplied ``TopologyRun``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from PIL import Image

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonValue
from mobile_world.runtime.audit.context import AuditContext, bind_audit_context
from mobile_world.runtime.audit.recorder import RunRecorder
from mobile_world.runtime.audit.runner_capture import RunnerTaskCapture
from mobile_world.runtime.audit.schemas import Producer
from mobile_world.runtime.sentinel.contracts import SentinelCallRole, SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    POLICY_PROPOSAL_SCHEMA_VERSION,
    RuntimeSentinelPolicyOutputV1,
    evidence_packet_sha256,
    runtime_policy_output_sha256,
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
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import (
    admission_receipt_projector,
    bind_policy_receipt,
    make_proposal_admission,
)
from mobile_world.runtime.sentinel.r2_2.sidecar import (
    MemoryR22PolicyReceiptSink,
    PolicyEvaluationStatus,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    RecordPathBindingV1,
    RubricBackendDescriptorV1,
    RubricTrackerProposalV1,
    RubricTrackingPacketV1,
    TopologyDeclarationV1,
    TopologyKind,
    TopologyRunStatus,
    TopologyRunV1,
    path_relevance_output_sha256,
)
from mobile_world.runtime.sentinel.r2_3.packet import HistoryFreeTrackingPacketBuilderV1
from mobile_world.runtime.sentinel.r2_3.session import RubricSessionStatus, RubricTaskSession
from mobile_world.runtime.sentinel.r2_4.capabilities import build_runtime_history_codec_resolver
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    canonical_json_bytes,
    canonical_sha256,
    issue_cpu_fake_active_authority,
)
from mobile_world.runtime.sentinel.r2_4.evidence import (
    CollectorEvidenceBundleV1,
    CollectorEvidenceFactoryV1,
    rubric_evidence_snapshot_projection,
)
from mobile_world.runtime.sentinel.r2_4.rubric_live import (
    CpuFakeRubricProviderPortV1,
    LiveOpenAIRubricBackendV1,
    LiveRubricExecutionScopeV1,
    _parse_tracker_proposal,
    _strict_json_object,
    live_rubric_track_schema,
)
from mobile_world.runtime.sentinel.r2_4.topology import (
    CpuFakeTopologyBackendInvocationV1,
    CpuFakeTopologyComparisonRunnerV1,
    CpuFakeTopologyExecutionControlV1,
    CpuFakeTopologyStimulusV1,
    TopologyBackendStageV1,
    build_cpu_fake_topology_stimulus,
)
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    CpuTopologyComponentCensusV1,
    CpuTopologyJointFailureProbeV1,
    r24_cpu_topology_artifact_projection,
)
from mobile_world.runtime.sentinel.r2_4.topology_artifact import (
    R24CpuTopologyArtifactV1 as CpuTopologyProducerResultV1,
)

_QWEN_CODEC_ID = "mobileworld.g1.history-codec.qwen-flat-progress"
_FIXTURE_RELATIVE_PATH = Path(
    "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)


@dataclass(frozen=True, slots=True)
class _RuntimeStimulusV1:
    request: dict[str, JsonValue]
    history_ir: HistoryIR
    context: SentinelContext
    bundle: CollectorEvidenceBundleV1


@dataclass(frozen=True, slots=True)
class _ScriptsV1:
    rubric_generate: str
    rubric_track: str
    policy: str

    @property
    def isolated_rubric_projection(self) -> dict[str, JsonValue]:
        return {
            "setup_generate": cast(JsonValue, json.loads(self.rubric_generate)),
            "runtime_track": cast(JsonValue, json.loads(self.rubric_track)),
        }

    @property
    def policy_projection(self) -> JsonValue:
        return cast(JsonValue, json.loads(self.policy))

    @property
    def joint_projection(self) -> dict[str, JsonValue]:
        return {
            "setup_generate": cast(JsonValue, json.loads(self.rubric_generate)),
            "runtime_rubric_track": cast(JsonValue, json.loads(self.rubric_track)),
            "history_policy": cast(JsonValue, json.loads(self.policy)),
        }


class _CpuFakeResponsesTransportV1:
    def __init__(self, output_text: str) -> None:
        if type(output_text) is not str or not output_text:
            raise R24ContractError("INVALID_FAKE_SCRIPT", "policy fake output is empty")
        self._output_text = output_text
        self._descriptor = TransportDescriptorV1.cpu_fake()
        self.calls = 0

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
        if (
            type(request) is not ResponsesRequestV1
            or call_role is not SentinelCallRole.SENTINEL
            or type(timeout_seconds) is not float
            or timeout_seconds <= 0
        ):
            raise R24ContractError(
                "CPU_FAKE_POLICY_CALL_REJECTED", "policy transport call contract differs"
            )
        self.calls += 1
        return ResponsesEnvelopeV1(
            response_id="r24-topology-cpu-response",
            requested_model=GPT56_REQUESTED_MODEL,
            returned_model=GPT56_REQUESTED_MODEL,
            status="completed",
            service_tier="default",
            output_text=self._output_text,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        )


@dataclass(frozen=True, slots=True)
class _JointProviderEnvelopeV1:
    rubric_track_output: str
    history_policy_output: str


class _JointCpuFakeProviderV1:
    """One dispatch yielding both non-independent component outputs."""

    def __init__(self, scripts: _ScriptsV1, *, fail_dispatch: bool = False) -> None:
        if type(scripts) is not _ScriptsV1 or type(fail_dispatch) is not bool:
            raise R24ContractError("INVALID_JOINT_FAKE", "joint fake configuration differs")
        self._scripts = scripts
        self._fail_dispatch = fail_dispatch
        self._invocation: CpuFakeTopologyBackendInvocationV1 | None = None
        self._envelope: _JointProviderEnvelopeV1 | None = None
        self._failure: R24ContractError | None = None
        self.dispatches = 0
        self.rubric_consumers = 0
        self.policy_consumers = 0

    def bind_invocation(self, invocation: CpuFakeTopologyBackendInvocationV1) -> None:
        if (
            self._invocation is not None
            or type(invocation) is not CpuFakeTopologyBackendInvocationV1
            or invocation.topology is not TopologyKind.JOINT_NON_INDEPENDENT
            or invocation.stage is not TopologyBackendStageV1.JOINT_RUBRIC_POLICY
            or invocation.script_sha256
            != canonical_sha256(cast(JsonValue, self._scripts.joint_projection))
        ):
            raise R24ContractError("JOINT_INVOCATION_DRIFT", "joint invocation was rebound")
        self._invocation = invocation

    def _dispatch(self) -> _JointProviderEnvelopeV1:
        if self._envelope is not None:
            return self._envelope
        if self._failure is not None:
            raise self._failure
        if self._invocation is None:
            raise R24ContractError("JOINT_INVOCATION_MISSING", "joint call was not controlled")
        self.dispatches += 1
        if self.dispatches != 1:
            raise R24ContractError("JOINT_CALL_CENSUS_MISMATCH", "joint provider dispatched twice")
        if self._fail_dispatch:
            self._failure = R24ContractError(
                "JOINT_PROVIDER_ERROR", "injected joint provider failure"
            )
            raise self._failure
        combined = canonical_json_bytes(
            cast(
                JsonValue,
                {
                    "history_policy": json.loads(self._scripts.policy),
                    "rubric_track": json.loads(self._scripts.rubric_track),
                },
            )
        )
        parsed = json.loads(combined)
        if type(parsed) is not dict or set(parsed) != {"history_policy", "rubric_track"}:
            raise R24ContractError("JOINT_RESPONSE_INVALID", "joint response shape differs")
        self._envelope = _JointProviderEnvelopeV1(
            rubric_track_output=canonical_json_bytes(
                cast(JsonValue, parsed["rubric_track"])
            ).decode(),
            history_policy_output=canonical_json_bytes(
                cast(JsonValue, parsed["history_policy"])
            ).decode(),
        )
        return self._envelope

    def rubric_track_output(self) -> str:
        self.rubric_consumers += 1
        if self.rubric_consumers != 1:
            raise R24ContractError("JOINT_CONSUMER_CENSUS_MISMATCH", "rubric output reused")
        return self._dispatch().rubric_track_output

    def history_policy_output(self) -> str:
        self.policy_consumers += 1
        if self.policy_consumers != 1:
            raise R24ContractError("JOINT_CONSUMER_CENSUS_MISMATCH", "policy output reused")
        return self._dispatch().history_policy_output


class _JointTrackerAdmissionBackendV1:
    """R2.3 tracker adapter over the already single-dispatch joint response."""

    def __init__(
        self,
        *,
        descriptor: RubricBackendDescriptorV1,
        provider: _JointCpuFakeProviderV1,
    ) -> None:
        if type(descriptor) is not RubricBackendDescriptorV1 or type(provider) is not (
            _JointCpuFakeProviderV1
        ):
            raise R24ContractError("INVALID_JOINT_FAKE", "joint tracker binding differs")
        self._descriptor = descriptor
        self._provider = provider
        self.calls = 0

    @property
    def descriptor(self) -> RubricBackendDescriptorV1:
        value = self._descriptor
        return RubricBackendDescriptorV1(
            **{name: getattr(value, name) for name in value.__dataclass_fields__}
        )

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1:
        self.calls += 1
        if self.calls != 1 or type(packet) is not RubricTrackingPacketV1:
            raise R24ContractError("JOINT_CONSUMER_CENSUS_MISMATCH", "joint tracker reused")
        parsed = _strict_json_object(self._provider.rubric_track_output())
        if tuple(Draft202012Validator(live_rubric_track_schema().as_dict()).iter_errors(parsed)):
            raise R24ContractError("JOINT_RESPONSE_INVALID", "joint rubric output violates schema")
        return _parse_tracker_proposal(parsed, packet=packet)


class _JointPolicyAdmissionTransportV1:
    """GPT56 transport adapter consuming the other half of one joint response."""

    def __init__(self, provider: _JointCpuFakeProviderV1) -> None:
        if type(provider) is not _JointCpuFakeProviderV1:
            raise R24ContractError("INVALID_JOINT_FAKE", "joint policy binding differs")
        self._provider = provider
        self._descriptor = TransportDescriptorV1.cpu_fake()
        self.calls = 0

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
        if (
            type(request) is not ResponsesRequestV1
            or call_role is not SentinelCallRole.SENTINEL
            or type(timeout_seconds) is not float
            or timeout_seconds <= 0
        ):
            raise R24ContractError(
                "CPU_FAKE_POLICY_CALL_REJECTED", "joint policy adapter call differs"
            )
        self.calls += 1
        if self.calls != 1:
            raise R24ContractError("JOINT_CONSUMER_CENSUS_MISMATCH", "joint policy reused")
        return ResponsesEnvelopeV1(
            response_id="r24-topology-joint-cpu-response",
            requested_model=GPT56_REQUESTED_MODEL,
            returned_model=GPT56_REQUESTED_MODEL,
            status="completed",
            service_tier="default",
            output_text=self._provider.history_policy_output(),
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
        )


@dataclass(frozen=True, slots=True)
class _RubricExecutionV1:
    output_sha256: str | None
    receipt_sha256: str | None
    session_receipts: int
    admitted: bool


@dataclass(frozen=True, slots=True)
class _PolicyExecutionV1:
    output_sha256: str | None
    adapter_calls: int
    policy_receipts: int
    evaluations: int
    admitted: bool


@dataclass(frozen=True, slots=True)
class _PreparedRubricV1:
    session: RubricTaskSession
    packet: RubricTrackingPacketV1
    setup_backend: LiveOpenAIRubricBackendV1


class _RealComponentExecutorV1:
    def __init__(
        self,
        *,
        kind: TopologyKind,
        runtime: _RuntimeStimulusV1,
        scripts: _ScriptsV1,
        joint_provider_failure: bool = False,
    ) -> None:
        if kind not in {TopologyKind.ISOLATED_HISTORY_FREE, TopologyKind.JOINT_NON_INDEPENDENT}:
            raise R24ContractError("INVALID_TOPOLOGY", "CPU producer topology is unknown")
        if type(joint_provider_failure) is not bool or (  # type: ignore[redundant-expr]
            joint_provider_failure and kind is not TopologyKind.JOINT_NON_INDEPENDENT
        ):
            raise R24ContractError(
                "INVALID_TOPOLOGY_FAILURE_PROBE", "only the joint fake may inject a failure"
            )
        self._kind = kind
        self._runtime = runtime
        self._scripts = scripts
        self._joint_provider_failure = joint_provider_failure
        self.component_census: CpuTopologyComponentCensusV1 | None = None

    @staticmethod
    def _session_id_factory() -> Callable[[str], str]:
        counts: dict[str, int] = {}

        def issue(prefix: str) -> str:
            next_count = counts.get(prefix, 0) + 1
            counts[prefix] = next_count
            digest = hashlib.sha256(f"{prefix}:{next_count}".encode()).hexdigest()
            return f"{prefix}-{digest[:32]}"

        return issue

    def _prepare_rubric(
        self,
        *,
        joint_provider: _JointCpuFakeProviderV1 | None,
    ) -> _PreparedRubricV1:
        runtime = self._runtime
        port = CpuFakeRubricProviderPortV1(
            generate_outputs=(self._scripts.rubric_generate,),
            # Both topology setups bind the same backend configuration.  The
            # joint tracker never dispatches this queued standalone output.
            track_outputs=(self._scripts.rubric_track,),
        )
        backend = LiveOpenAIRubricBackendV1(provider_port=port)
        backend.bind_collector_call(
            bundle=runtime.bundle,
            logical_call_id=runtime.context.logical_call_id,
            actor_request_sha256=runtime.history_ir.raw_request_sha256,
            deadline_monotonic_ns=time.monotonic_ns() + 10_000_000_000,
            max_cost_usd_micros=1,
            case_lease=None,
        )
        session = RubricTaskSession(
            task_run_id=runtime.bundle.r23_snapshot.task_run_id,
            task=runtime.bundle.r23_snapshot.task,
            builder_backend=backend,
            tracker_backend=(
                backend
                if joint_provider is None
                else _JointTrackerAdmissionBackendV1(
                    descriptor=backend.descriptor,
                    provider=joint_provider,
                )
            ),
            id_factory=self._session_id_factory(),
        )
        generated = session.start()
        if (
            generated.status is not RubricSessionStatus.ADMITTED
            or generated.rubric is None
            or generated.state is None
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_RUBRIC_GENERATE_FAILED", "R2.3 generation did not admit"
            )
        if (
            len(backend.call_receipts) != 1
            or session.task_start_generation_calls != 1
            or any(
                receipt.execution_scope is not LiveRubricExecutionScopeV1.CPU_TEST_LOCAL
                or receipt.raw_task_or_image_persisted
                or receipt.provider_output_persisted
                or receipt.actor_history_included
                or receipt.history_ir_included
                or receipt.action_or_tool_authority
                for receipt in backend.call_receipts
            )
            or backend.descriptor.external_network_attempted
            or backend.descriptor.model_call_attempted
            or backend.descriptor.local_gpu_used
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "R2.3 task-start setup census differs",
            )
        packet = HistoryFreeTrackingPacketBuilderV1().build(
            packet_id="r24-topology-track-packet",
            logical_call_id=runtime.context.logical_call_id,
            rubric=generated.rubric,
            prior_state=generated.state,
            snapshot=runtime.bundle.r23_snapshot,
        )
        return _PreparedRubricV1(session=session, packet=packet, setup_backend=backend)

    def _run_rubric(self, prepared: _PreparedRubricV1) -> _RubricExecutionV1:
        runtime = self._runtime
        session = prepared.session
        tracked = session.track(prepared.packet)
        if tracked.status is not RubricSessionStatus.ADMITTED or tracked.state is None:
            receipts = cast(Any, session.receipt_sink).receipts
            if len(receipts) != 2 or session.runtime_tracking_calls != 1:
                raise R24ContractError(
                    "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                    "failed R2.3 tracking census differs",
                )
            return _RubricExecutionV1(
                output_sha256=None,
                receipt_sha256=tracked.receipt_sha256,
                session_receipts=len(receipts),
                admitted=False,
            )
        relevance = session.link_records(
            state=tracked.state,
            record_bindings=tuple(
                RecordPathBindingV1(
                    record_id=record.record_id,
                    linked_path_ids=(),
                    path_independent=False,
                )
                for record in runtime.history_ir.records
            ),
            supported_records=(),
            logical_call_id=runtime.context.logical_call_id,
        )
        if (
            relevance.status is not RubricSessionStatus.ADMITTED
            or relevance.relevance is None
            or relevance.receipt_sha256 is None
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_RUBRIC_RELEVANCE_FAILED", "R2.3 relevance did not admit"
            )
        rubric_receipts = cast(Any, session.receipt_sink).receipts
        if (
            len(rubric_receipts) != 3
            or session.runtime_tracking_calls != 1
            or session.relevance_link_calls != 1
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "R2.3 CPU component calls/receipts differ",
            )
        return _RubricExecutionV1(
            output_sha256=path_relevance_output_sha256(relevance.relevance),
            receipt_sha256=relevance.receipt_sha256,
            session_receipts=len(rubric_receipts),
            admitted=True,
        )

    def _run_policy(
        self,
        transport: _CpuFakeResponsesTransportV1 | _JointPolicyAdmissionTransportV1,
    ) -> _PolicyExecutionV1:
        runtime = self._runtime
        sink = MemoryR22PolicyReceiptSink()
        policy = GPT56SentinelPolicy(
            transport=transport,
            evidence_packet_factory=lambda _request, _context, _history: runtime.bundle.gpt56_input,
            proposal_admission=make_proposal_admission(
                packet=runtime.bundle.r22_packet,
                source_request=cast(JsonValue, runtime.request),
                history_ir=runtime.history_ir,
            ),
            admission_receipt_projector=admission_receipt_projector,
            bind_policy_receipt=bind_policy_receipt,
            receipt_sink=sink,
            metrics=R22PolicyMetrics(),
            output_schema=ProposalSchemaSnapshotV1.from_checked_in(),
            timeout_seconds=0.5,
            seam_policy_deadline_seconds=5.0,
        )
        try:
            output = policy.evaluate(
                request=cast(JsonValue, runtime.request),
                context=runtime.context,
                history_ir=runtime.history_ir,
            )
        except GPT56PolicyError:
            receipts = sink.receipts
            if (
                transport.calls != 1
                or policy.evaluate_count != 1
                or len(receipts) != 1
                or receipts[0].evaluation_status is not PolicyEvaluationStatus.TRANSPORT_ERROR
                or receipts[0].external_network_attempted
                or receipts[0].model_call_attempted
                or receipts[0].local_gpu_used
                or receipts[0].mobileworld_action_executed
            ):
                raise R24ContractError(
                    "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                    "failed GPT56 admission census differs",
                ) from None
            return _PolicyExecutionV1(
                output_sha256=None,
                adapter_calls=transport.calls,
                policy_receipts=len(receipts),
                evaluations=policy.evaluate_count,
                admitted=False,
            )
        if type(output) is not RuntimeSentinelPolicyOutputV1:
            raise R24ContractError("CPU_TOPOLOGY_POLICY_FAILED", "GPT56 policy output type differs")
        receipts = sink.receipts
        if (
            transport.calls != 1
            or policy.evaluate_count != 1
            or len(receipts) != 1
            or receipts[0].evaluation_status is not PolicyEvaluationStatus.ADMITTED
            or receipts[0].sha256 != output.policy_receipt_sha256
            or receipts[0].external_network_attempted
            or receipts[0].model_call_attempted
            or receipts[0].local_gpu_used
            or receipts[0].mobileworld_action_executed
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "GPT56 CPU component calls/receipts differ",
            )
        return _PolicyExecutionV1(
            output_sha256=runtime_policy_output_sha256(output),
            adapter_calls=transport.calls,
            policy_receipts=len(receipts),
            evaluations=policy.evaluate_count,
            admitted=True,
        )

    def _joint_components(
        self,
        invocation: CpuFakeTopologyBackendInvocationV1,
        *,
        provider: _JointCpuFakeProviderV1,
        prepared: _PreparedRubricV1,
        transport: _JointPolicyAdmissionTransportV1,
    ) -> tuple[_RubricExecutionV1, _PolicyExecutionV1]:
        provider.bind_invocation(invocation)
        rubric = self._run_rubric(prepared)
        policy = self._run_policy(transport)
        if rubric.admitted is not policy.admitted:
            raise R24ContractError(
                "JOINT_FAILURE_NOT_COUPLED",
                "one single-call joint output admitted while the other failed",
            )
        return rubric, policy

    def execute(
        self,
        *,
        stimulus: CpuFakeTopologyStimulusV1,
        control: CpuFakeTopologyExecutionControlV1,
    ) -> TopologyRunV1:
        joint_provider = (
            _JointCpuFakeProviderV1(
                self._scripts,
                fail_dispatch=self._joint_provider_failure,
            )
            if self._kind is TopologyKind.JOINT_NON_INDEPENDENT
            else None
        )
        prepared = self._prepare_rubric(joint_provider=joint_provider)
        started_ns = time.monotonic_ns()
        if self._kind is TopologyKind.ISOLATED_HISTORY_FREE:
            rubric = control.run_backend(
                TopologyBackendStageV1.ISOLATED_RUBRIC,
                script_sha256=stimulus.isolated_rubric_script_sha256,
                call=lambda _invocation: self._run_rubric(prepared),
            )
            isolated_transport = _CpuFakeResponsesTransportV1(self._scripts.policy)
            policy = control.run_backend(
                TopologyBackendStageV1.ISOLATED_HISTORY_POLICY,
                script_sha256=stimulus.isolated_policy_script_sha256,
                call=lambda _invocation: self._run_policy(isolated_transport),
            )
            comparison_provider_dispatches = (
                len(prepared.setup_backend.call_receipts) - 1 + isolated_transport.calls
            )
        else:
            if joint_provider is None:
                raise AssertionError("joint provider construction is exhaustive")
            joint_transport = _JointPolicyAdmissionTransportV1(joint_provider)
            rubric, policy = control.run_backend(
                TopologyBackendStageV1.JOINT_RUBRIC_POLICY,
                script_sha256=stimulus.joint_script_sha256,
                call=lambda invocation: self._joint_components(
                    invocation,
                    provider=joint_provider,
                    prepared=prepared,
                    transport=joint_transport,
                ),
            )
            comparison_provider_dispatches = joint_provider.dispatches
        self.component_census = CpuTopologyComponentCensusV1(
            topology=self._kind,
            setup_rubric_provider_calls=1,
            comparison_provider_dispatches=comparison_provider_dispatches,
            rubric_session_receipts=rubric.session_receipts,
            policy_admission_adapter_calls=policy.adapter_calls,
            policy_receipts=policy.policy_receipts,
            policy_evaluations=policy.evaluations,
            rubric_output_admitted=rubric.admitted,
            history_policy_output_admitted=policy.admitted,
        )
        binding = control.expected_input_sha256
        admitted = rubric.admitted and policy.admitted
        return TopologyRunV1(
            topology=TopologyDeclarationV1(
                kind=self._kind,
                independent_grounding_claim_eligible=(
                    self._kind is TopologyKind.ISOLATED_HISTORY_FREE
                ),
            ),
            status=(TopologyRunStatus.ADMITTED if admitted else TopologyRunStatus.FALLBACK),
            rubric_input_sha256=binding,
            rubric_output_sha256=rubric.output_sha256 if admitted else None,
            rubric_receipt_sha256=rubric.receipt_sha256 if admitted else None,
            history_policy_input_sha256=(
                binding if admitted and self._kind is TopologyKind.JOINT_NON_INDEPENDENT else None
            ),
            history_policy_output_sha256=(
                policy.output_sha256
                if admitted and self._kind is TopologyKind.JOINT_NON_INDEPENDENT
                else None
            ),
            failure_code=None if admitted else "JOINT_PROVIDER_ERROR",
            total_latency_ns=time.monotonic_ns() - started_ns,
        )


def _extract_current_image(request: dict[str, JsonValue]) -> Image.Image:
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
    if not urls or not urls[-1].startswith("data:image/"):
        raise R24ContractError("CPU_FIXTURE_IMAGE_MISSING", "captured request has no image")
    try:
        image_bytes = base64.b64decode(urls[-1].split(",", 1)[1], validate=True)
        with Image.open(io.BytesIO(image_bytes)) as opened:
            opened.load()
            return cast(Image.Image, opened.copy())
    except Exception as exc:
        raise R24ContractError(
            "CPU_FIXTURE_IMAGE_INVALID", "captured request image cannot be decoded"
        ) from exc


def _build_runtime_stimulus(repository_root: Path, audit_root: Path) -> _RuntimeStimulusV1:
    fixture_path = repository_root / _FIXTURE_RELATIVE_PATH
    try:
        raw_fixture = fixture_path.read_bytes()
        fixture = json.loads(raw_fixture)
        if type(fixture) is not dict or type(fixture.get("application_request")) is not dict:
            raise TypeError("fixture shape differs")
        request = cast(dict[str, JsonValue], deepcopy(fixture["application_request"]))
        history_ir = (
            build_runtime_history_codec_resolver()
            .by_id(_QWEN_CODEC_ID)
            .extract(cast(JsonValue, request))
        )
    except Exception as exc:
        raise R24ContractError(
            "CPU_TOPOLOGY_FIXTURE_REJECTED", "checked-in Qwen fixture cannot be loaded"
        ) from exc
    run = RunRecorder(
        audit_root,
        producer=Producer.local(version="r2.4-topology-cpu-v1", worker_id="topology-cpu"),
        sync=False,
    )
    try:
        run.write_manifest_start({"run_id": run.run_id})
        task = run.open_task()
        capture = RunnerTaskCapture(task)
        started = capture.start_task(
            task_name="R24CpuTopologyComparison",
            task_goal="调整显示亮度。",
            task_goal_status="resolved",
            task_index=1,
            suite_family="mobile_world_cpu_fixture",
            agent={"adapter": "qwen", "model": "captured-fixture", "configuration": {}},
            environment={"backend_id": "none", "device_id": "none"},
            whole_task_attempt_index=1,
        )
        step = capture.start_step(
            step_index=1,
            observation={
                "screenshot": _extract_current_image(request),
                "accessibility_tree": {"screen": "display", "slider": "brightness"},
                "tool_call": None,
                "ask_user_response": None,
            },
        )
        if started is None or step is None:
            raise R24ContractError(
                "CPU_TOPOLOGY_COLLECTOR_FAILED", "Collector fixture events were not emitted"
            )
        context = SentinelContext(
            logical_call_id="r24-topology-cpu-logical-call",
            host_id=history_ir.host_id,
        )
        audit_context = AuditContext(
            run_id=run.run_id,
            recorder=task,
            task_run_id=task.task_run_id,
            step_id=step.step_id,
            decision_id=step.decision_id,
            parent_event_id=cast(str, step.step_started_event_id),
        )
        with bind_audit_context(audit_context):
            bundle = CollectorEvidenceFactoryV1().bundle_for_call(
                request=cast(JsonValue, request),
                context=context,
                history_ir=history_ir,
            )
        return _RuntimeStimulusV1(
            request=request,
            history_ir=history_ir,
            context=context,
            bundle=bundle,
        )
    finally:
        run.close()


def _rubric_generate_script(task_text: str) -> str:
    value: dict[str, JsonValue] = {
        "instruction_spans": [
            {
                "span_id": "task-goal",
                "role": "HARD_REQUIREMENT",
                "char_start": 0,
                "char_end": len(task_text),
                "utf8_byte_start": 0,
                "utf8_byte_end": len(task_text.encode("utf-8")),
                "exact_text": task_text,
            }
        ],
        "milestones": [
            {
                "milestone_id": "task-goal-state",
                "kind": "HARD_REQUIREMENT",
                "predicate_kind": "INSTRUCTION_REQUIREMENT",
                "state_description": task_text,
                "instruction_span_id": "task-goal",
            }
        ],
        "gates": [],
        "common_root": None,
        "paths": [
            {
                "path_id": "direct-path",
                "kind": "LEGAL_ALTERNATIVE",
                "root": {"ref_kind": "MILESTONE", "ref_id": "task-goal-state"},
            },
            {"path_id": "other-unknown", "kind": "OTHER_UNKNOWN", "root": None},
        ],
    }
    return canonical_json_bytes(cast(JsonValue, value)).decode("utf-8")


def _rubric_track_script(bundle: CollectorEvidenceBundleV1) -> str:
    evidence_id = bundle.r23_snapshot.current_observation.screenshot_evidence_id
    evidence = next(
        item for item in bundle.r23_snapshot.evidence_index if item.evidence_id == evidence_id
    )
    value: dict[str, JsonValue] = {
        "proposal_status": "COMPLETE",
        "milestone_states": [
            {
                "milestone_id": "task-goal-state",
                "state": "satisfied",
                "evidence_refs": [
                    {
                        "evidence_id": evidence.evidence_id,
                        "payload_sha256": evidence.payload_sha256,
                        "relation": "SUPPORTS_STATE",
                    }
                ],
                "reason_code": "CURRENT_GUI_SUPPORT",
            }
        ],
    }
    return canonical_json_bytes(cast(JsonValue, value)).decode("utf-8")


def _policy_script(bundle: CollectorEvidenceBundleV1) -> str:
    decisions: list[JsonValue] = [
        {
            "decision_id": f"decision-{index}",
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
        for index, target in enumerate(bundle.r22_packet.targets)
    ]
    value: dict[str, JsonValue] = {
        "schema_version": POLICY_PROPOSAL_SCHEMA_VERSION,
        "packet_id": bundle.r22_packet.packet_id,
        "evidence_packet_sha256": evidence_packet_sha256(bundle.r22_packet),
        "status": "ABSTAIN",
        "automatic": True,
        "curated": False,
        "deployment_prediction": True,
        "action_or_tool_authority": False,
        "decisions": decisions,
    }
    return canonical_json_bytes(cast(JsonValue, value)).decode("utf-8")


def produce_cpu_fake_topology_comparison(
    *,
    repository_root: Path,
) -> CpuTopologyProducerResultV1:
    """Execute the exact real-component CPU comparison in an auto-cleaned temp root."""

    if not isinstance(repository_root, Path) or not repository_root.is_absolute():  # type: ignore[redundant-expr]
        raise R24ContractError("INVALID_REPOSITORY_ROOT", "repository root must be absolute")
    try:
        trusted_root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise R24ContractError(
            "INVALID_REPOSITORY_ROOT", "repository root cannot be resolved"
        ) from exc
    if not trusted_root.is_dir():
        raise R24ContractError("INVALID_REPOSITORY_ROOT", "repository root is not a directory")
    with TemporaryDirectory(prefix="r24-topology-cpu-") as temporary:
        runtime = _build_runtime_stimulus(trusted_root, Path(temporary))
        scripts = _ScriptsV1(
            rubric_generate=_rubric_generate_script(runtime.bundle.r23_snapshot.task.exact_text),
            rubric_track=_rubric_track_script(runtime.bundle),
            policy=_policy_script(runtime.bundle),
        )
        authority = issue_cpu_fake_active_authority()
        rubric_projection = rubric_evidence_snapshot_projection(runtime.bundle.r23_snapshot)
        stimulus = build_cpu_fake_topology_stimulus(
            pair_id="r24-real-components-pair",
            logical_call_id=runtime.context.logical_call_id,
            task_instruction=runtime.bundle.r23_snapshot.task.exact_text,
            causal_cutoff=rubric_projection["cutoff"],
            current_observation=rubric_projection["current_observation"],
            isolated_rubric_script=cast(JsonValue, scripts.isolated_rubric_projection),
            isolated_policy_script=scripts.policy_projection,
            joint_script=cast(JsonValue, scripts.joint_projection),
            authority=authority,
        )
        isolated = _RealComponentExecutorV1(
            kind=TopologyKind.ISOLATED_HISTORY_FREE,
            runtime=runtime,
            scripts=scripts,
        )
        joint = _RealComponentExecutorV1(
            kind=TopologyKind.JOINT_NON_INDEPENDENT,
            runtime=runtime,
            scripts=scripts,
        )
        comparison = CpuFakeTopologyComparisonRunnerV1(authority).execute(
            comparison_id="r24-real-components-comparison",
            stimulus=stimulus,
            isolated_executor=isolated,
            joint_executor=joint,
        )
        if isolated.component_census is None or joint.component_census is None:
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISSING", "component execution was not recorded"
            )
        failure_executor = _RealComponentExecutorV1(
            kind=TopologyKind.JOINT_NON_INDEPENDENT,
            runtime=runtime,
            scripts=scripts,
            joint_provider_failure=True,
        )
        failure_control = CpuFakeTopologyExecutionControlV1(
            stimulus,
            TopologyKind.JOINT_NON_INDEPENDENT,
        )
        failure_run = failure_executor.execute(
            stimulus=stimulus,
            control=failure_control,
        )
        failure_control.validate_result(failure_run)
        failure_census = failure_executor.component_census
        if (
            failure_run.status is not TopologyRunStatus.FALLBACK
            or failure_run.failure_code != "JOINT_PROVIDER_ERROR"
            or type(failure_census) is not CpuTopologyComponentCensusV1
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_FAILURE_COUPLING_MISMATCH",
                "joint failure probe did not fail both component outputs",
            )
        failure_probe = CpuTopologyJointFailureProbeV1(
            provider_dispatches=failure_census.comparison_provider_dispatches,
            rubric_output_admitted=failure_census.rubric_output_admitted,
            history_policy_output_admitted=(failure_census.history_policy_output_admitted),
            failure_coupled=(
                not failure_census.rubric_output_admitted
                and not failure_census.history_policy_output_admitted
            ),
        )
        return CpuTopologyProducerResultV1(
            comparison=comparison,
            isolated_components=isolated.component_census,
            joint_components=joint.component_census,
            joint_failure_probe=failure_probe,
        )


def produce_cpu_fake_topology_artifact_bytes(*, repository_root: Path) -> bytes:
    result = produce_cpu_fake_topology_comparison(repository_root=repository_root)
    return canonical_json_bytes(cast(JsonValue, r24_cpu_topology_artifact_projection(result)))


__all__ = [
    "CpuTopologyComponentCensusV1",
    "CpuTopologyJointFailureProbeV1",
    "CpuTopologyProducerResultV1",
    "produce_cpu_fake_topology_artifact_bytes",
    "produce_cpu_fake_topology_comparison",
]
