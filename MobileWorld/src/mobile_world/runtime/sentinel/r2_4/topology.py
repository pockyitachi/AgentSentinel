"""Deterministic CPU/fake comparison of the R2.3 runtime call topologies.

The isolated, history-free rubric result remains the only primary grounding
source.  A joint result is recorded for latency/failure/output comparison and
is always labelled non-independent; it can never replace the isolated result.
This CPU evidence proposes the isolated topology for R2.5, but deliberately
cannot freeze a pilot deployment without a later owner resource/run authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import InitVar, dataclass
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_3.contracts import (
    TopologyComparisonV1,
    TopologyDeclarationV1,
    TopologyKind,
    TopologyRunStatus,
    TopologyRunV1,
    topology_comparison_projection,
    topology_comparison_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    CpuFakeActiveAuthorityV1,
    R24ContractError,
    canonical_sha256,
    snapshot_json_value,
)

R24_TOPOLOGY_COMPARISON_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-topology-comparison/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TIMEOUT_CODE = re.compile(r"(?:TIMEOUT|[A-Z][A-Z0-9_]*_TIMEOUT|TIMEOUT_[A-Z0-9_]+)")
_STIMULUS_BUILDER_TOKEN = object()
_TOPOLOGY_RUNNER_TOKEN = object()


class R24TopologyOutcomeV1(StrEnum):
    BOTH_ADMITTED_AGREE = "BOTH_ADMITTED_AGREE"
    BOTH_ADMITTED_DIVERGE = "BOTH_ADMITTED_DIVERGE"
    ISOLATED_ONLY_ADMITTED = "ISOLATED_ONLY_ADMITTED"
    JOINT_ONLY_ADMITTED = "JOINT_ONLY_ADMITTED"
    BOTH_FAILED = "BOTH_FAILED"
    ISOLATED_TIMEOUT = "ISOLATED_TIMEOUT"
    JOINT_TIMEOUT = "JOINT_TIMEOUT"
    BOTH_TIMEOUT = "BOTH_TIMEOUT"


class TopologyFailureObservationV1(StrEnum):
    NO_FAILURE = "NO_FAILURE"
    ISOLATED_ONLY_FAILURE = "ISOLATED_ONLY_FAILURE"
    JOINT_ONLY_FAILURE = "JOINT_ONLY_FAILURE"
    BOTH_FAILED_SAME_TRIAL = "BOTH_FAILED_SAME_TRIAL"


class PilotTopologySelectionStatusV1(StrEnum):
    FROZEN_FOR_R25 = "FROZEN_FOR_R25"


class TopologyBackendStageV1(StrEnum):
    ISOLATED_RUBRIC = "ISOLATED_RUBRIC"
    ISOLATED_HISTORY_POLICY = "ISOLATED_HISTORY_POLICY"
    JOINT_RUBRIC_POLICY = "JOINT_RUBRIC_POLICY"


_CLAIM_LIMITATIONS = (
    "CPU_FAKE_ONLY",
    "NO_LIVE_OR_EFFECTIVENESS_CLAIM",
    "JOINT_GROUNDING_NON_INDEPENDENT",
    "LIVE_EXECUTION_REQUIRES_SEPARATE_OWNER_AUTHORITY",
)


@dataclass(frozen=True, slots=True)
class CpuFakeTopologyStimulusV1:
    pair_id: str
    logical_call_id: str
    task_instruction_sha256: str
    causal_cutoff_sha256: str
    current_observation_sha256: str
    isolated_rubric_script_sha256: str
    isolated_policy_script_sha256: str
    joint_script_sha256: str
    matched_task_cutoff_observation: bool = True
    cpu_only: bool = True
    offline: bool = True
    external_network_allowed: bool = False
    gpu_allowed: bool = False
    action_execution_allowed: bool = False
    _builder_token: InitVar[object | None] = None

    def __post_init__(self, _builder_token: object | None) -> None:
        if _builder_token is not _STIMULUS_BUILDER_TOKEN:
            raise R24ContractError(
                "MODULE_OWNED_STIMULUS_BUILDER_REQUIRED",
                "matched topology stimulus must be built from exact source values",
            )
        _require_runtime_id(self.pair_id, "pair_id")
        _require_runtime_id(self.logical_call_id, "stimulus.logical_call_id")
        for value, name in (
            (self.task_instruction_sha256, "task_instruction_sha256"),
            (self.causal_cutoff_sha256, "causal_cutoff_sha256"),
            (self.current_observation_sha256, "current_observation_sha256"),
            (self.isolated_rubric_script_sha256, "isolated_rubric_script_sha256"),
            (self.isolated_policy_script_sha256, "isolated_policy_script_sha256"),
            (self.joint_script_sha256, "joint_script_sha256"),
        ):
            _require_sha256(value, name)
        fixed = {
            "matched_task_cutoff_observation": True,
            "cpu_only": True,
            "offline": True,
            "external_network_allowed": False,
            "gpu_allowed": False,
            "action_execution_allowed": False,
        }
        for name, required in fixed.items():
            value = getattr(self, name)
            if type(value) is not bool or value is not required:
                raise R24ContractError(
                    "UNMATCHED_OR_UNAUTHORIZED_STIMULUS",
                    f"{name} violates the matched CPU/fake stimulus authority",
                )


def cpu_fake_topology_stimulus_projection(
    value: CpuFakeTopologyStimulusV1,
) -> dict[str, JsonValue]:
    if type(value) is not CpuFakeTopologyStimulusV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "stimulus must use exact type")
    trusted = CpuFakeTopologyStimulusV1(
        pair_id=value.pair_id,
        logical_call_id=value.logical_call_id,
        task_instruction_sha256=value.task_instruction_sha256,
        causal_cutoff_sha256=value.causal_cutoff_sha256,
        current_observation_sha256=value.current_observation_sha256,
        isolated_rubric_script_sha256=value.isolated_rubric_script_sha256,
        isolated_policy_script_sha256=value.isolated_policy_script_sha256,
        joint_script_sha256=value.joint_script_sha256,
        matched_task_cutoff_observation=value.matched_task_cutoff_observation,
        cpu_only=value.cpu_only,
        offline=value.offline,
        external_network_allowed=value.external_network_allowed,
        gpu_allowed=value.gpu_allowed,
        action_execution_allowed=value.action_execution_allowed,
        _builder_token=_STIMULUS_BUILDER_TOKEN,
    )
    return {
        "pair_id": trusted.pair_id,
        "logical_call_id": trusted.logical_call_id,
        "task_instruction_sha256": trusted.task_instruction_sha256,
        "causal_cutoff_sha256": trusted.causal_cutoff_sha256,
        "current_observation_sha256": trusted.current_observation_sha256,
        "isolated_rubric_script_sha256": trusted.isolated_rubric_script_sha256,
        "isolated_policy_script_sha256": trusted.isolated_policy_script_sha256,
        "joint_script_sha256": trusted.joint_script_sha256,
        "matched_task_cutoff_observation": trusted.matched_task_cutoff_observation,
        "cpu_only": trusted.cpu_only,
        "offline": trusted.offline,
        "external_network_allowed": trusted.external_network_allowed,
        "gpu_allowed": trusted.gpu_allowed,
        "action_execution_allowed": trusted.action_execution_allowed,
    }


def cpu_fake_topology_stimulus_sha256(value: CpuFakeTopologyStimulusV1) -> str:
    return canonical_sha256(cast(JsonValue, cpu_fake_topology_stimulus_projection(value)))


def build_cpu_fake_topology_stimulus(
    *,
    pair_id: str,
    logical_call_id: str,
    task_instruction: str,
    causal_cutoff: JsonValue,
    current_observation: JsonValue,
    isolated_rubric_script: JsonValue,
    isolated_policy_script: JsonValue,
    joint_script: JsonValue,
    authority: CpuFakeActiveAuthorityV1,
) -> CpuFakeTopologyStimulusV1:
    if type(authority) is not CpuFakeActiveAuthorityV1:
        raise R24ContractError("CPU_FAKE_AUTHORITY_REQUIRED", "authority is untrusted")
    CpuFakeActiveAuthorityV1(
        offline=authority.offline,
        fake_provider=authority.fake_provider,
        network_allowed=authority.network_allowed,
        gpu_allowed=authority.gpu_allowed,
        actor_actions_allowed=authority.actor_actions_allowed,
        scope=authority.scope,
    )
    if type(task_instruction) is not str or not task_instruction:
        raise R24ContractError("INVALID_STIMULUS", "task instruction must be non-empty text")
    return CpuFakeTopologyStimulusV1(
        pair_id=pair_id,
        logical_call_id=logical_call_id,
        task_instruction_sha256=canonical_sha256(task_instruction),
        causal_cutoff_sha256=canonical_sha256(snapshot_json_value(causal_cutoff)),
        current_observation_sha256=canonical_sha256(snapshot_json_value(current_observation)),
        isolated_rubric_script_sha256=canonical_sha256(snapshot_json_value(isolated_rubric_script)),
        isolated_policy_script_sha256=canonical_sha256(snapshot_json_value(isolated_policy_script)),
        joint_script_sha256=canonical_sha256(snapshot_json_value(joint_script)),
        _builder_token=_STIMULUS_BUILDER_TOKEN,
    )


def topology_input_binding_sha256(
    stimulus: CpuFakeTopologyStimulusV1,
    topology: TopologyKind,
) -> str:
    if type(stimulus) is not CpuFakeTopologyStimulusV1 or type(topology) is not TopologyKind:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "stimulus/topology is untrusted")
    stimulus_hash = cpu_fake_topology_stimulus_sha256(stimulus)
    if topology is TopologyKind.ISOLATED_HISTORY_FREE:
        scripts: list[JsonValue] = [
            stimulus.isolated_rubric_script_sha256,
            stimulus.isolated_policy_script_sha256,
        ]
    else:
        scripts = [stimulus.joint_script_sha256]
    projection: dict[str, JsonValue] = {
        "pair_stimulus_sha256": stimulus_hash,
        "topology": topology.value,
        "fake_backend_script_sha256s": scripts,
    }
    return canonical_sha256(projection)


def _require_sha256(value: object, name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R24ContractError("INVALID_SHA256", f"{name} must be lowercase SHA-256")


def _require_runtime_id(value: object, name: str) -> None:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        raise R24ContractError("INVALID_RUNTIME_ID", f"{name} is not a bounded runtime ID")


_ISOLATED_STAGES = (
    TopologyBackendStageV1.ISOLATED_RUBRIC,
    TopologyBackendStageV1.ISOLATED_HISTORY_POLICY,
)
_JOINT_STAGES = (TopologyBackendStageV1.JOINT_RUBRIC_POLICY,)


@dataclass(frozen=True, slots=True)
class CpuFakeTopologyBackendInvocationV1:
    """Exact matched input authority delivered to one trusted fake callback."""

    pair_id: str
    logical_call_id: str
    stimulus_sha256: str
    input_binding_sha256: str
    topology: TopologyKind
    stage: TopologyBackendStageV1
    script_sha256: str

    def __post_init__(self) -> None:
        _require_runtime_id(self.pair_id, "invocation.pair_id")
        _require_runtime_id(self.logical_call_id, "invocation.logical_call_id")
        for value, name in (
            (self.stimulus_sha256, "invocation.stimulus_sha256"),
            (self.input_binding_sha256, "invocation.input_binding_sha256"),
            (self.script_sha256, "invocation.script_sha256"),
        ):
            _require_sha256(value, name)
        if type(self.topology) is not TopologyKind or type(self.stage) is not (
            TopologyBackendStageV1
        ):
            raise R24ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "backend invocation enums are untrusted"
            )


class CpuFakeTopologyExecutionControlV1:
    """Counts and script-binds every fake backend call made by one topology."""

    def __init__(self, stimulus: CpuFakeTopologyStimulusV1, topology: TopologyKind) -> None:
        if type(stimulus) is not CpuFakeTopologyStimulusV1 or type(topology) is not TopologyKind:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "execution control input is untrusted")
        self._stimulus = CpuFakeTopologyStimulusV1(
            pair_id=stimulus.pair_id,
            logical_call_id=stimulus.logical_call_id,
            task_instruction_sha256=stimulus.task_instruction_sha256,
            causal_cutoff_sha256=stimulus.causal_cutoff_sha256,
            current_observation_sha256=stimulus.current_observation_sha256,
            isolated_rubric_script_sha256=stimulus.isolated_rubric_script_sha256,
            isolated_policy_script_sha256=stimulus.isolated_policy_script_sha256,
            joint_script_sha256=stimulus.joint_script_sha256,
            matched_task_cutoff_observation=stimulus.matched_task_cutoff_observation,
            cpu_only=stimulus.cpu_only,
            offline=stimulus.offline,
            external_network_allowed=stimulus.external_network_allowed,
            gpu_allowed=stimulus.gpu_allowed,
            action_execution_allowed=stimulus.action_execution_allowed,
            _builder_token=_STIMULUS_BUILDER_TOKEN,
        )
        self._topology = topology
        self._stages: list[TopologyBackendStageV1] = []

    @property
    def expected_input_sha256(self) -> str:
        return topology_input_binding_sha256(self._stimulus, self._topology)

    @property
    def observed_stages(self) -> tuple[TopologyBackendStageV1, ...]:
        return tuple(self._stages)

    def run_backend[T](
        self,
        stage: TopologyBackendStageV1,
        *,
        script_sha256: str,
        call: Callable[[CpuFakeTopologyBackendInvocationV1], T],
    ) -> T:
        if type(stage) is not TopologyBackendStageV1 or not callable(call):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "backend call is untrusted")
        expected_stages = (
            _ISOLATED_STAGES
            if self._topology is TopologyKind.ISOLATED_HISTORY_FREE
            else _JOINT_STAGES
        )
        position = len(self._stages)
        if position >= len(expected_stages) or stage is not expected_stages[position]:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "backend stage is duplicated or out of order"
            )
        expected_script = {
            TopologyBackendStageV1.ISOLATED_RUBRIC: (self._stimulus.isolated_rubric_script_sha256),
            TopologyBackendStageV1.ISOLATED_HISTORY_POLICY: (
                self._stimulus.isolated_policy_script_sha256
            ),
            TopologyBackendStageV1.JOINT_RUBRIC_POLICY: self._stimulus.joint_script_sha256,
        }[stage]
        if script_sha256 != expected_script:
            raise R24ContractError(
                "TOPOLOGY_SCRIPT_BINDING_MISMATCH", "backend call used another fake script"
            )
        self._stages.append(stage)
        invocation = CpuFakeTopologyBackendInvocationV1(
            pair_id=self._stimulus.pair_id,
            logical_call_id=self._stimulus.logical_call_id,
            stimulus_sha256=cpu_fake_topology_stimulus_sha256(self._stimulus),
            input_binding_sha256=self.expected_input_sha256,
            topology=self._topology,
            stage=stage,
            script_sha256=expected_script,
        )
        return call(invocation)

    def validate_result(self, value: TopologyRunV1) -> None:
        if type(value) is not TopologyRunV1 or value.topology.kind is not self._topology:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "executor returned another topology")
        if value.status is TopologyRunStatus.NOT_RUN or not self._stages:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "topology must execute at least one backend stage"
            )
        expected_stages = (
            _ISOLATED_STAGES
            if self._topology is TopologyKind.ISOLATED_HISTORY_FREE
            else _JOINT_STAGES
        )
        observed = tuple(self._stages)
        if observed != expected_stages[: len(observed)]:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "observed backend stages are not a valid prefix"
            )
        if value.status is TopologyRunStatus.ADMITTED and observed != expected_stages:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "admitted topology omitted a backend stage"
            )
        if value.rubric_input_sha256 != self.expected_input_sha256:
            raise R24ContractError(
                "TOPOLOGY_STIMULUS_BINDING_MISMATCH", "run input differs from matched stimulus"
            )
        if (
            self._topology is TopologyKind.JOINT_NON_INDEPENDENT
            and value.status is TopologyRunStatus.ADMITTED
            and value.history_policy_input_sha256 != self.expected_input_sha256
        ):
            raise R24ContractError(
                "TOPOLOGY_STIMULUS_BINDING_MISMATCH",
                "joint policy and rubric did not consume the same matched input",
            )


@runtime_checkable
class CpuFakeTopologyExecutorV1(Protocol):
    def execute(
        self,
        *,
        stimulus: CpuFakeTopologyStimulusV1,
        control: CpuFakeTopologyExecutionControlV1,
    ) -> TopologyRunV1: ...


def _declaration_projection(value: TopologyDeclarationV1) -> dict[str, JsonValue]:
    if type(value) is not TopologyDeclarationV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology declaration is untrusted")
    return {
        "kind": value.kind.value,
        "independent_grounding_claim_eligible": value.independent_grounding_claim_eligible,
    }


def r23_topology_run_projection(value: TopologyRunV1) -> dict[str, JsonValue]:
    """Project an exact R2.3 run without consulting an overridable serializer."""

    value = snapshot_r23_topology_run(value)
    return {
        "topology": cast(JsonValue, _declaration_projection(value.topology)),
        "status": value.status.value,
        "rubric_input_sha256": value.rubric_input_sha256,
        "rubric_output_sha256": value.rubric_output_sha256,
        "rubric_receipt_sha256": value.rubric_receipt_sha256,
        "history_policy_input_sha256": value.history_policy_input_sha256,
        "history_policy_output_sha256": value.history_policy_output_sha256,
        "failure_code": value.failure_code,
        "total_latency_ns": value.total_latency_ns,
    }


def snapshot_r23_topology_run(value: TopologyRunV1) -> TopologyRunV1:
    """Rebuild a detached exact R2.3 run and rerun its closed invariants."""

    if type(value) is not TopologyRunV1 or type(value.topology) is not TopologyDeclarationV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology run is untrusted")
    declaration = TopologyDeclarationV1(
        kind=value.topology.kind,
        independent_grounding_claim_eligible=(value.topology.independent_grounding_claim_eligible),
    )
    return TopologyRunV1(
        topology=declaration,
        status=value.status,
        rubric_input_sha256=value.rubric_input_sha256,
        rubric_output_sha256=value.rubric_output_sha256,
        rubric_receipt_sha256=value.rubric_receipt_sha256,
        history_policy_input_sha256=value.history_policy_input_sha256,
        history_policy_output_sha256=value.history_policy_output_sha256,
        failure_code=value.failure_code,
        total_latency_ns=value.total_latency_ns,
    )


def r23_topology_run_sha256(value: TopologyRunV1) -> str:
    return canonical_sha256(cast(JsonValue, r23_topology_run_projection(value)))


def _is_timeout(value: TopologyRunV1) -> bool:
    return value.status is TopologyRunStatus.FALLBACK and (
        value.failure_code is not None and _TIMEOUT_CODE.fullmatch(value.failure_code) is not None
    )


def _classify_outcome(isolated: TopologyRunV1, joint: TopologyRunV1) -> R24TopologyOutcomeV1:
    isolated_timeout = _is_timeout(isolated)
    joint_timeout = _is_timeout(joint)
    if isolated_timeout and joint_timeout:
        return R24TopologyOutcomeV1.BOTH_TIMEOUT
    if isolated_timeout:
        return R24TopologyOutcomeV1.ISOLATED_TIMEOUT
    if joint_timeout:
        return R24TopologyOutcomeV1.JOINT_TIMEOUT

    isolated_ok = isolated.status is TopologyRunStatus.ADMITTED
    joint_ok = joint.status is TopologyRunStatus.ADMITTED
    if isolated_ok and joint_ok:
        if isolated.rubric_output_sha256 == joint.rubric_output_sha256:
            return R24TopologyOutcomeV1.BOTH_ADMITTED_AGREE
        return R24TopologyOutcomeV1.BOTH_ADMITTED_DIVERGE
    if isolated_ok:
        return R24TopologyOutcomeV1.ISOLATED_ONLY_ADMITTED
    if joint_ok:
        return R24TopologyOutcomeV1.JOINT_ONLY_ADMITTED
    return R24TopologyOutcomeV1.BOTH_FAILED


def _failure_observation(
    isolated: TopologyRunV1,
    joint: TopologyRunV1,
) -> TopologyFailureObservationV1:
    isolated_failed = isolated.status is not TopologyRunStatus.ADMITTED
    joint_failed = joint.status is not TopologyRunStatus.ADMITTED
    if isolated_failed and joint_failed:
        return TopologyFailureObservationV1.BOTH_FAILED_SAME_TRIAL
    if isolated_failed:
        return TopologyFailureObservationV1.ISOLATED_ONLY_FAILURE
    if joint_failed:
        return TopologyFailureObservationV1.JOINT_ONLY_FAILURE
    return TopologyFailureObservationV1.NO_FAILURE


@dataclass(frozen=True, slots=True)
class R24CpuTopologyComparisonV1:
    comparison_id: str
    logical_call_id: str
    pair_id: str
    stimulus_sha256: str
    task_instruction_sha256: str
    causal_cutoff_sha256: str
    current_observation_sha256: str
    isolated_rubric_script_sha256: str
    isolated_policy_script_sha256: str
    joint_script_sha256: str
    isolated_input_binding_sha256: str
    joint_input_binding_sha256: str
    source_r23_comparison_sha256: str
    source_isolated_run_sha256: str
    source_joint_run_sha256: str
    source_isolated_run: TopologyRunV1
    source_joint_run: TopologyRunV1
    outcome: R24TopologyOutcomeV1
    failure_observation: TopologyFailureObservationV1
    isolated_status: TopologyRunStatus
    joint_status: TopologyRunStatus
    isolated_succeeded: bool
    joint_succeeded: bool
    isolated_timed_out: bool
    joint_timed_out: bool
    output_agreement: bool | None
    isolated_observed_stages: tuple[TopologyBackendStageV1, ...]
    joint_observed_stages: tuple[TopologyBackendStageV1, ...]
    isolated_call_count: int
    joint_call_count: int
    isolated_latency_ns: int
    joint_latency_ns: int
    primary_topology: TopologyKind = TopologyKind.ISOLATED_HISTORY_FREE
    joint_classification: TopologyKind = TopologyKind.JOINT_NON_INDEPENDENT
    independent_grounding_source: str = "ISOLATED_ONLY"
    joint_may_replace_isolated: bool = False
    proposed_pilot_topology: TopologyKind = TopologyKind.ISOLATED_HISTORY_FREE
    pilot_selection_status: PilotTopologySelectionStatusV1 = (
        PilotTopologySelectionStatusV1.FROZEN_FOR_R25
    )
    owner_authority_present: bool = False
    deployment_topology_frozen: bool = True
    claim_limitations: tuple[str, ...] = _CLAIM_LIMITATIONS
    cpu_only: bool = True
    offline: bool = True
    external_network_attempted: bool = False
    gpu_used: bool = False
    mobileworld_backend_used: bool = False
    action_executed: bool = False
    schema_version: str = R24_TOPOLOGY_COMPARISON_SCHEMA_VERSION
    _runner_token: InitVar[object | None] = None

    def __post_init__(self, _runner_token: object | None) -> None:
        if _runner_token is not _TOPOLOGY_RUNNER_TOKEN:
            raise R24ContractError(
                "MODULE_OWNED_TOPOLOGY_RUNNER_REQUIRED",
                "comparison must be emitted by the matched-pair runner",
            )
        if self.schema_version != R24_TOPOLOGY_COMPARISON_SCHEMA_VERSION:
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown R2.4 topology schema")
        _require_runtime_id(self.comparison_id, "comparison_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_runtime_id(self.pair_id, "pair_id")
        for value, name in (
            (self.stimulus_sha256, "stimulus_sha256"),
            (self.task_instruction_sha256, "task_instruction_sha256"),
            (self.causal_cutoff_sha256, "causal_cutoff_sha256"),
            (self.current_observation_sha256, "current_observation_sha256"),
            (self.isolated_rubric_script_sha256, "isolated_rubric_script_sha256"),
            (self.isolated_policy_script_sha256, "isolated_policy_script_sha256"),
            (self.joint_script_sha256, "joint_script_sha256"),
            (self.isolated_input_binding_sha256, "isolated_input_binding_sha256"),
            (self.joint_input_binding_sha256, "joint_input_binding_sha256"),
            (self.source_r23_comparison_sha256, "source_r23_comparison_sha256"),
            (self.source_isolated_run_sha256, "source_isolated_run_sha256"),
            (self.source_joint_run_sha256, "source_joint_run_sha256"),
        ):
            _require_sha256(value, name)
        isolated = snapshot_r23_topology_run(self.source_isolated_run)
        joint = snapshot_r23_topology_run(self.source_joint_run)
        stimulus = CpuFakeTopologyStimulusV1(
            pair_id=self.pair_id,
            logical_call_id=self.logical_call_id,
            task_instruction_sha256=self.task_instruction_sha256,
            causal_cutoff_sha256=self.causal_cutoff_sha256,
            current_observation_sha256=self.current_observation_sha256,
            isolated_rubric_script_sha256=self.isolated_rubric_script_sha256,
            isolated_policy_script_sha256=self.isolated_policy_script_sha256,
            joint_script_sha256=self.joint_script_sha256,
            _builder_token=_STIMULUS_BUILDER_TOKEN,
        )
        if self.stimulus_sha256 != cpu_fake_topology_stimulus_sha256(stimulus) or (
            self.isolated_input_binding_sha256
            != topology_input_binding_sha256(stimulus, TopologyKind.ISOLATED_HISTORY_FREE)
            or self.joint_input_binding_sha256
            != topology_input_binding_sha256(stimulus, TopologyKind.JOINT_NON_INDEPENDENT)
        ):
            raise R24ContractError(
                "TOPOLOGY_STIMULUS_BINDING_MISMATCH",
                "comparison does not bind its exact matched pair stimulus",
            )
        if (
            isolated.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE
            or joint.topology.kind is not TopologyKind.JOINT_NON_INDEPENDENT
        ):
            raise R24ContractError(
                "TOPOLOGY_SOURCE_KIND_MISMATCH",
                "source runs do not preserve isolated/joint slot semantics",
            )
        if (
            isolated.rubric_input_sha256 != self.isolated_input_binding_sha256
            or joint.rubric_input_sha256 != self.joint_input_binding_sha256
            or (
                joint.status is TopologyRunStatus.ADMITTED
                and joint.history_policy_input_sha256 != self.joint_input_binding_sha256
            )
        ):
            raise R24ContractError(
                "TOPOLOGY_STIMULUS_BINDING_MISMATCH",
                "source runs do not bind the matched topology inputs",
            )
        source = TopologyComparisonV1(
            comparison_id=self.comparison_id,
            logical_call_id=self.logical_call_id,
            isolated=isolated,
            joint=joint,
        )
        if (
            self.source_isolated_run_sha256 != r23_topology_run_sha256(isolated)
            or self.source_joint_run_sha256 != r23_topology_run_sha256(joint)
            or self.source_r23_comparison_sha256 != topology_comparison_sha256(source)
        ):
            raise R24ContractError(
                "SOURCE_TOPOLOGY_BINDING_MISMATCH",
                "source run preimages do not match their recorded hashes",
            )
        if type(self.outcome) is not R24TopologyOutcomeV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology outcome is untrusted")
        if type(self.failure_observation) is not TopologyFailureObservationV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "failure observation is untrusted")
        if (
            type(self.isolated_status) is not TopologyRunStatus
            or type(self.joint_status) is not TopologyRunStatus
        ):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology status is untrusted")
        if self.isolated_status is TopologyRunStatus.NOT_RUN or self.joint_status is (
            TopologyRunStatus.NOT_RUN
        ):
            raise R24ContractError(
                "INCOMPLETE_TOPOLOGY_COMPARISON", "both CPU topology runs must execute"
            )
        for name in (
            "isolated_succeeded",
            "joint_succeeded",
            "isolated_timed_out",
            "joint_timed_out",
        ):
            if type(getattr(self, name)) is not bool:
                raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", f"{name} must be bool")
        if self.output_agreement is not None and type(self.output_agreement) is not bool:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "output agreement must be bool/null")
        if self.output_agreement is not None and not (
            self.isolated_succeeded and self.joint_succeeded
        ):
            raise R24ContractError(
                "INVALID_OUTPUT_AGREEMENT", "agreement exists only when both runs succeeded"
            )
        if (
            self.isolated_succeeded
            and self.joint_succeeded
            and type(self.output_agreement) is not bool
        ):
            raise R24ContractError(
                "INVALID_OUTPUT_AGREEMENT", "two admitted runs require an agreement verdict"
            )
        if self.isolated_succeeded is not (
            self.isolated_status is TopologyRunStatus.ADMITTED
        ) or self.joint_succeeded is not (self.joint_status is TopologyRunStatus.ADMITTED):
            raise R24ContractError(
                "TOPOLOGY_CENSUS_MISMATCH", "success flags differ from run status"
            )
        if (
            self.isolated_status is not isolated.status
            or self.joint_status is not joint.status
            or self.isolated_latency_ns != isolated.total_latency_ns
            or self.joint_latency_ns != joint.total_latency_ns
            or self.isolated_timed_out is not _is_timeout(isolated)
            or self.joint_timed_out is not _is_timeout(joint)
        ):
            raise R24ContractError(
                "TOPOLOGY_CENSUS_MISMATCH",
                "status, timeout, or latency differs from source run preimages",
            )
        expected_agreement = (
            isolated.rubric_output_sha256 == joint.rubric_output_sha256
            if isolated.status is TopologyRunStatus.ADMITTED
            and joint.status is TopologyRunStatus.ADMITTED
            else None
        )
        if self.output_agreement is not expected_agreement:
            raise R24ContractError(
                "INVALID_OUTPUT_AGREEMENT",
                "agreement differs from the source rubric-output hashes",
            )
        expected_failure = (
            TopologyFailureObservationV1.BOTH_FAILED_SAME_TRIAL
            if not self.isolated_succeeded and not self.joint_succeeded
            else TopologyFailureObservationV1.ISOLATED_ONLY_FAILURE
            if not self.isolated_succeeded
            else TopologyFailureObservationV1.JOINT_ONLY_FAILURE
            if not self.joint_succeeded
            else TopologyFailureObservationV1.NO_FAILURE
        )
        if self.failure_observation is not expected_failure:
            raise R24ContractError(
                "TOPOLOGY_CENSUS_MISMATCH", "failure observation differs from run status"
            )
        if self.isolated_timed_out and self.joint_timed_out:
            expected_outcome = R24TopologyOutcomeV1.BOTH_TIMEOUT
        elif self.isolated_timed_out:
            expected_outcome = R24TopologyOutcomeV1.ISOLATED_TIMEOUT
        elif self.joint_timed_out:
            expected_outcome = R24TopologyOutcomeV1.JOINT_TIMEOUT
        elif self.isolated_succeeded and self.joint_succeeded:
            expected_outcome = (
                R24TopologyOutcomeV1.BOTH_ADMITTED_AGREE
                if self.output_agreement
                else R24TopologyOutcomeV1.BOTH_ADMITTED_DIVERGE
            )
        elif self.isolated_succeeded:
            expected_outcome = R24TopologyOutcomeV1.ISOLATED_ONLY_ADMITTED
        elif self.joint_succeeded:
            expected_outcome = R24TopologyOutcomeV1.JOINT_ONLY_ADMITTED
        else:
            expected_outcome = R24TopologyOutcomeV1.BOTH_FAILED
        if self.outcome is not expected_outcome:
            raise R24ContractError(
                "TOPOLOGY_CENSUS_MISMATCH", "outcome differs from success/timeout census"
            )
        for call_count, name in (
            (self.isolated_call_count, "isolated_call_count"),
            (self.joint_call_count, "joint_call_count"),
        ):
            if type(call_count) is not int or call_count < 1:
                raise R24ContractError("INVALID_CALL_COUNT", f"{name} must be positive")
        for stages, allowed, name in (
            (self.isolated_observed_stages, _ISOLATED_STAGES, "isolated_observed_stages"),
            (self.joint_observed_stages, _JOINT_STAGES, "joint_observed_stages"),
        ):
            if (
                type(stages) is not tuple
                or not stages
                or any(type(stage) is not TopologyBackendStageV1 for stage in stages)
                or stages != allowed[: len(stages)]
            ):
                raise R24ContractError(
                    "TOPOLOGY_CALL_CENSUS_MISMATCH", f"{name} is not a valid controlled prefix"
                )
        if self.isolated_call_count != len(self.isolated_observed_stages) or (
            self.joint_call_count != len(self.joint_observed_stages)
        ):
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "call counts differ from controlled stages"
            )
        if self.isolated_succeeded and self.isolated_observed_stages != _ISOLATED_STAGES:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "admitted isolated run omitted a stage"
            )
        if self.joint_succeeded and self.joint_observed_stages != _JOINT_STAGES:
            raise R24ContractError(
                "TOPOLOGY_CALL_CENSUS_MISMATCH", "admitted joint run omitted its stage"
            )
        for latency_ns, name in (
            (self.isolated_latency_ns, "isolated_latency_ns"),
            (self.joint_latency_ns, "joint_latency_ns"),
        ):
            if type(latency_ns) is not int or latency_ns < 0:
                raise R24ContractError("INVALID_STAGE_LATENCY", f"{name} must be non-negative")

        if (
            type(self.primary_topology) is not TopologyKind
            or self.primary_topology is not TopologyKind.ISOLATED_HISTORY_FREE
            or type(self.joint_classification) is not TopologyKind
            or self.joint_classification is not TopologyKind.JOINT_NON_INDEPENDENT
            or type(self.proposed_pilot_topology) is not TopologyKind
            or self.proposed_pilot_topology is not TopologyKind.ISOLATED_HISTORY_FREE
            or type(self.pilot_selection_status) is not PilotTopologySelectionStatusV1
            or self.pilot_selection_status  # type: ignore[redundant-expr]
            is not PilotTopologySelectionStatusV1.FROZEN_FOR_R25
            or type(self.independent_grounding_source) is not str
            or self.independent_grounding_source != "ISOLATED_ONLY"
            or type(self.claim_limitations) is not tuple
            or self.claim_limitations != _CLAIM_LIMITATIONS
            or any(type(item) is not str for item in self.claim_limitations)
        ):
            raise R24ContractError(
                "TOPOLOGY_AUTHORITY_VIOLATION",
                "joint cannot replace isolated and the R2.5 selection must remain frozen",
            )
        bool_fixed = {
            "joint_may_replace_isolated": False,
            "owner_authority_present": False,
            "deployment_topology_frozen": True,
            "cpu_only": True,
            "offline": True,
            "external_network_attempted": False,
            "gpu_used": False,
            "mobileworld_backend_used": False,
            "action_executed": False,
        }
        for name, required in bool_fixed.items():
            value = getattr(self, name)
            if type(value) is not bool or value is not required:
                raise R24ContractError(
                    "TOPOLOGY_AUTHORITY_VIOLATION", f"{name} exceeds CPU comparison authority"
                )

    @property
    def total_call_count(self) -> int:
        return self.isolated_call_count + self.joint_call_count

    @property
    def total_latency_ns(self) -> int:
        return self.isolated_latency_ns + self.joint_latency_ns


def r24_topology_comparison_projection(
    value: R24CpuTopologyComparisonV1,
) -> dict[str, JsonValue]:
    value = snapshot_r24_topology_comparison(value)
    return {
        "schema_version": value.schema_version,
        "comparison_id": value.comparison_id,
        "logical_call_id": value.logical_call_id,
        "pair_id": value.pair_id,
        "stimulus_sha256": value.stimulus_sha256,
        "task_instruction_sha256": value.task_instruction_sha256,
        "causal_cutoff_sha256": value.causal_cutoff_sha256,
        "current_observation_sha256": value.current_observation_sha256,
        "isolated_rubric_script_sha256": value.isolated_rubric_script_sha256,
        "isolated_policy_script_sha256": value.isolated_policy_script_sha256,
        "joint_script_sha256": value.joint_script_sha256,
        "isolated_input_binding_sha256": value.isolated_input_binding_sha256,
        "joint_input_binding_sha256": value.joint_input_binding_sha256,
        "source_r23_comparison_sha256": value.source_r23_comparison_sha256,
        "source_isolated_run_sha256": value.source_isolated_run_sha256,
        "source_joint_run_sha256": value.source_joint_run_sha256,
        "source_isolated_run": cast(
            JsonValue, r23_topology_run_projection(value.source_isolated_run)
        ),
        "source_joint_run": cast(JsonValue, r23_topology_run_projection(value.source_joint_run)),
        "outcome": value.outcome.value,
        "failure_observation": value.failure_observation.value,
        "isolated_status": value.isolated_status.value,
        "joint_status": value.joint_status.value,
        "isolated_succeeded": value.isolated_succeeded,
        "joint_succeeded": value.joint_succeeded,
        "isolated_timed_out": value.isolated_timed_out,
        "joint_timed_out": value.joint_timed_out,
        "output_agreement": value.output_agreement,
        "isolated_observed_stages": [item.value for item in value.isolated_observed_stages],
        "joint_observed_stages": [item.value for item in value.joint_observed_stages],
        "isolated_call_count": value.isolated_call_count,
        "joint_call_count": value.joint_call_count,
        "total_call_count": value.total_call_count,
        "isolated_latency_ns": value.isolated_latency_ns,
        "joint_latency_ns": value.joint_latency_ns,
        "total_latency_ns": value.total_latency_ns,
        "primary_topology": value.primary_topology.value,
        "joint_classification": value.joint_classification.value,
        "independent_grounding_source": value.independent_grounding_source,
        "joint_may_replace_isolated": value.joint_may_replace_isolated,
        "proposed_pilot_topology": value.proposed_pilot_topology.value,
        "pilot_selection_status": value.pilot_selection_status.value,
        "owner_authority_present": value.owner_authority_present,
        "deployment_topology_frozen": value.deployment_topology_frozen,
        "claim_limitations": list(value.claim_limitations),
        "cpu_only": value.cpu_only,
        "offline": value.offline,
        "external_network_attempted": value.external_network_attempted,
        "gpu_used": value.gpu_used,
        "mobileworld_backend_used": value.mobileworld_backend_used,
        "action_executed": value.action_executed,
    }


def snapshot_r24_topology_comparison(
    value: R24CpuTopologyComparisonV1,
) -> R24CpuTopologyComparisonV1:
    """Reconstruct and fully revalidate a comparison before projection/use."""

    if type(value) is not R24CpuTopologyComparisonV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "comparison must use exact type")
    return R24CpuTopologyComparisonV1(
        comparison_id=value.comparison_id,
        logical_call_id=value.logical_call_id,
        pair_id=value.pair_id,
        stimulus_sha256=value.stimulus_sha256,
        task_instruction_sha256=value.task_instruction_sha256,
        causal_cutoff_sha256=value.causal_cutoff_sha256,
        current_observation_sha256=value.current_observation_sha256,
        isolated_rubric_script_sha256=value.isolated_rubric_script_sha256,
        isolated_policy_script_sha256=value.isolated_policy_script_sha256,
        joint_script_sha256=value.joint_script_sha256,
        isolated_input_binding_sha256=value.isolated_input_binding_sha256,
        joint_input_binding_sha256=value.joint_input_binding_sha256,
        source_r23_comparison_sha256=value.source_r23_comparison_sha256,
        source_isolated_run_sha256=value.source_isolated_run_sha256,
        source_joint_run_sha256=value.source_joint_run_sha256,
        source_isolated_run=snapshot_r23_topology_run(value.source_isolated_run),
        source_joint_run=snapshot_r23_topology_run(value.source_joint_run),
        outcome=value.outcome,
        failure_observation=value.failure_observation,
        isolated_status=value.isolated_status,
        joint_status=value.joint_status,
        isolated_succeeded=value.isolated_succeeded,
        joint_succeeded=value.joint_succeeded,
        isolated_timed_out=value.isolated_timed_out,
        joint_timed_out=value.joint_timed_out,
        output_agreement=value.output_agreement,
        isolated_observed_stages=tuple(value.isolated_observed_stages),
        joint_observed_stages=tuple(value.joint_observed_stages),
        isolated_call_count=value.isolated_call_count,
        joint_call_count=value.joint_call_count,
        isolated_latency_ns=value.isolated_latency_ns,
        joint_latency_ns=value.joint_latency_ns,
        primary_topology=value.primary_topology,
        joint_classification=value.joint_classification,
        independent_grounding_source=value.independent_grounding_source,
        joint_may_replace_isolated=value.joint_may_replace_isolated,
        proposed_pilot_topology=value.proposed_pilot_topology,
        pilot_selection_status=value.pilot_selection_status,
        owner_authority_present=value.owner_authority_present,
        deployment_topology_frozen=value.deployment_topology_frozen,
        claim_limitations=tuple(value.claim_limitations),
        cpu_only=value.cpu_only,
        offline=value.offline,
        external_network_attempted=value.external_network_attempted,
        gpu_used=value.gpu_used,
        mobileworld_backend_used=value.mobileworld_backend_used,
        action_executed=value.action_executed,
        schema_version=value.schema_version,
        _runner_token=_TOPOLOGY_RUNNER_TOKEN,
    )


def r24_topology_comparison_sha256(value: R24CpuTopologyComparisonV1) -> str:
    return canonical_sha256(cast(JsonValue, r24_topology_comparison_projection(value)))


_TOPOLOGY_DECLARATION_FIELDS = frozenset({"independent_grounding_claim_eligible", "kind"})
_TOPOLOGY_RUN_FIELDS = frozenset(
    {
        "failure_code",
        "history_policy_input_sha256",
        "history_policy_output_sha256",
        "rubric_input_sha256",
        "rubric_output_sha256",
        "rubric_receipt_sha256",
        "status",
        "topology",
        "total_latency_ns",
    }
)
_R24_TOPOLOGY_COMPARISON_FIELDS = frozenset(
    {
        "action_executed",
        "claim_limitations",
        "comparison_id",
        "cpu_only",
        "current_observation_sha256",
        "deployment_topology_frozen",
        "external_network_attempted",
        "failure_observation",
        "gpu_used",
        "independent_grounding_source",
        "isolated_call_count",
        "isolated_input_binding_sha256",
        "isolated_latency_ns",
        "isolated_observed_stages",
        "isolated_policy_script_sha256",
        "isolated_rubric_script_sha256",
        "isolated_status",
        "isolated_succeeded",
        "isolated_timed_out",
        "joint_call_count",
        "joint_classification",
        "joint_input_binding_sha256",
        "joint_latency_ns",
        "joint_may_replace_isolated",
        "joint_observed_stages",
        "joint_script_sha256",
        "joint_status",
        "joint_succeeded",
        "joint_timed_out",
        "logical_call_id",
        "mobileworld_backend_used",
        "offline",
        "outcome",
        "output_agreement",
        "owner_authority_present",
        "pair_id",
        "pilot_selection_status",
        "primary_topology",
        "proposed_pilot_topology",
        "schema_version",
        "source_isolated_run",
        "source_isolated_run_sha256",
        "source_joint_run",
        "source_joint_run_sha256",
        "source_r23_comparison_sha256",
        "stimulus_sha256",
        "task_instruction_sha256",
        "causal_cutoff_sha256",
        "total_call_count",
        "total_latency_ns",
    }
)


def _exact_object(
    value: object,
    expected: frozenset[str],
    name: str,
) -> dict[str, object]:
    if type(value) is not dict:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an object")
    mapping = cast(dict[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != expected:
        raise R24ContractError("INVALID_FIELDS", f"{name} fields do not match the contract")
    return cast(dict[str, object], mapping)


def _parse_enum[T: StrEnum](enum_type: type[T], value: object, name: str) -> T:
    if type(value) is not str:
        raise R24ContractError("INVALID_ENUM", f"{name} is not text")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise R24ContractError("INVALID_ENUM", f"{name} is unknown") from exc


def _parse_r23_topology_run(value: object, expected_kind: TopologyKind) -> TopologyRunV1:
    item = _exact_object(value, _TOPOLOGY_RUN_FIELDS, "source topology run")
    raw_declaration = _exact_object(
        item["topology"],
        _TOPOLOGY_DECLARATION_FIELDS,
        "source topology declaration",
    )
    declaration = TopologyDeclarationV1(
        kind=_parse_enum(TopologyKind, raw_declaration["kind"], "source topology kind"),
        independent_grounding_claim_eligible=cast(
            bool, raw_declaration["independent_grounding_claim_eligible"]
        ),
    )
    if declaration.kind is not expected_kind:
        raise R24ContractError(
            "TOPOLOGY_SOURCE_KIND_MISMATCH", "source run is in the wrong comparison slot"
        )
    return TopologyRunV1(
        topology=declaration,
        status=_parse_enum(TopologyRunStatus, item["status"], "source topology status"),
        rubric_input_sha256=cast(str | None, item["rubric_input_sha256"]),
        rubric_output_sha256=cast(str | None, item["rubric_output_sha256"]),
        rubric_receipt_sha256=cast(str | None, item["rubric_receipt_sha256"]),
        history_policy_input_sha256=cast(str | None, item["history_policy_input_sha256"]),
        history_policy_output_sha256=cast(str | None, item["history_policy_output_sha256"]),
        failure_code=cast(str | None, item["failure_code"]),
        total_latency_ns=cast(int, item["total_latency_ns"]),
    )


def parse_r24_topology_comparison(value: object) -> R24CpuTopologyComparisonV1:
    """Strictly parse and recompute every derived field in one CPU artifact."""

    item = _exact_object(value, _R24_TOPOLOGY_COMPARISON_FIELDS, "topology comparison")
    raw_isolated_stages = item["isolated_observed_stages"]
    raw_joint_stages = item["joint_observed_stages"]
    raw_limitations = item["claim_limitations"]
    if any(
        type(raw) is not list for raw in (raw_isolated_stages, raw_joint_stages, raw_limitations)
    ):
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "topology comparison collections must be arrays"
        )
    isolated = _parse_r23_topology_run(
        item["source_isolated_run"], TopologyKind.ISOLATED_HISTORY_FREE
    )
    joint = _parse_r23_topology_run(item["source_joint_run"], TopologyKind.JOINT_NON_INDEPENDENT)
    comparison = R24CpuTopologyComparisonV1(
        comparison_id=cast(str, item["comparison_id"]),
        logical_call_id=cast(str, item["logical_call_id"]),
        pair_id=cast(str, item["pair_id"]),
        stimulus_sha256=cast(str, item["stimulus_sha256"]),
        task_instruction_sha256=cast(str, item["task_instruction_sha256"]),
        causal_cutoff_sha256=cast(str, item["causal_cutoff_sha256"]),
        current_observation_sha256=cast(str, item["current_observation_sha256"]),
        isolated_rubric_script_sha256=cast(str, item["isolated_rubric_script_sha256"]),
        isolated_policy_script_sha256=cast(str, item["isolated_policy_script_sha256"]),
        joint_script_sha256=cast(str, item["joint_script_sha256"]),
        isolated_input_binding_sha256=cast(str, item["isolated_input_binding_sha256"]),
        joint_input_binding_sha256=cast(str, item["joint_input_binding_sha256"]),
        source_r23_comparison_sha256=cast(str, item["source_r23_comparison_sha256"]),
        source_isolated_run_sha256=cast(str, item["source_isolated_run_sha256"]),
        source_joint_run_sha256=cast(str, item["source_joint_run_sha256"]),
        source_isolated_run=isolated,
        source_joint_run=joint,
        outcome=_parse_enum(R24TopologyOutcomeV1, item["outcome"], "topology outcome"),
        failure_observation=_parse_enum(
            TopologyFailureObservationV1,
            item["failure_observation"],
            "failure observation",
        ),
        isolated_status=_parse_enum(TopologyRunStatus, item["isolated_status"], "isolated status"),
        joint_status=_parse_enum(TopologyRunStatus, item["joint_status"], "joint status"),
        isolated_succeeded=cast(bool, item["isolated_succeeded"]),
        joint_succeeded=cast(bool, item["joint_succeeded"]),
        isolated_timed_out=cast(bool, item["isolated_timed_out"]),
        joint_timed_out=cast(bool, item["joint_timed_out"]),
        output_agreement=cast(bool | None, item["output_agreement"]),
        isolated_observed_stages=tuple(
            _parse_enum(TopologyBackendStageV1, stage, "isolated backend stage")
            for stage in cast(list[object], raw_isolated_stages)
        ),
        joint_observed_stages=tuple(
            _parse_enum(TopologyBackendStageV1, stage, "joint backend stage")
            for stage in cast(list[object], raw_joint_stages)
        ),
        isolated_call_count=cast(int, item["isolated_call_count"]),
        joint_call_count=cast(int, item["joint_call_count"]),
        isolated_latency_ns=cast(int, item["isolated_latency_ns"]),
        joint_latency_ns=cast(int, item["joint_latency_ns"]),
        primary_topology=_parse_enum(TopologyKind, item["primary_topology"], "primary topology"),
        joint_classification=_parse_enum(
            TopologyKind, item["joint_classification"], "joint classification"
        ),
        independent_grounding_source=cast(str, item["independent_grounding_source"]),
        joint_may_replace_isolated=cast(bool, item["joint_may_replace_isolated"]),
        proposed_pilot_topology=_parse_enum(
            TopologyKind, item["proposed_pilot_topology"], "pilot topology"
        ),
        pilot_selection_status=_parse_enum(
            PilotTopologySelectionStatusV1,
            item["pilot_selection_status"],
            "pilot selection status",
        ),
        owner_authority_present=cast(bool, item["owner_authority_present"]),
        deployment_topology_frozen=cast(bool, item["deployment_topology_frozen"]),
        claim_limitations=tuple(cast(list[str], raw_limitations)),
        cpu_only=cast(bool, item["cpu_only"]),
        offline=cast(bool, item["offline"]),
        external_network_attempted=cast(bool, item["external_network_attempted"]),
        gpu_used=cast(bool, item["gpu_used"]),
        mobileworld_backend_used=cast(bool, item["mobileworld_backend_used"]),
        action_executed=cast(bool, item["action_executed"]),
        schema_version=cast(str, item["schema_version"]),
        _runner_token=_TOPOLOGY_RUNNER_TOKEN,
    )
    # Constructor checks all semantic/hash bindings.  Exact projection equality
    # also rejects a forged derived total that is not accepted by the constructor.
    if r24_topology_comparison_projection(comparison) != value:
        raise R24ContractError(
            "TOPOLOGY_DERIVATION_MISMATCH",
            "topology artifact contains a non-recomputed derived field",
        )
    return comparison


class CpuFakeTopologyComparisonRunnerV1:
    """Execute each trusted CPU/fake topology callback exactly once and compare."""

    def __init__(self, authority: CpuFakeActiveAuthorityV1) -> None:
        if type(authority) is not CpuFakeActiveAuthorityV1:
            raise R24ContractError("CPU_FAKE_AUTHORITY_REQUIRED", "authority is untrusted")
        # Reconstruct instead of retaining the caller-owned authority object.
        self._authority = CpuFakeActiveAuthorityV1(
            offline=authority.offline,
            fake_provider=authority.fake_provider,
            network_allowed=authority.network_allowed,
            gpu_allowed=authority.gpu_allowed,
            actor_actions_allowed=authority.actor_actions_allowed,
            scope=authority.scope,
        )

    @staticmethod
    def _snapshot_stimulus(value: CpuFakeTopologyStimulusV1) -> CpuFakeTopologyStimulusV1:
        if type(value) is not CpuFakeTopologyStimulusV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "stimulus must use exact type")
        return CpuFakeTopologyStimulusV1(
            pair_id=value.pair_id,
            logical_call_id=value.logical_call_id,
            task_instruction_sha256=value.task_instruction_sha256,
            causal_cutoff_sha256=value.causal_cutoff_sha256,
            current_observation_sha256=value.current_observation_sha256,
            isolated_rubric_script_sha256=value.isolated_rubric_script_sha256,
            isolated_policy_script_sha256=value.isolated_policy_script_sha256,
            joint_script_sha256=value.joint_script_sha256,
            matched_task_cutoff_observation=value.matched_task_cutoff_observation,
            cpu_only=value.cpu_only,
            offline=value.offline,
            external_network_allowed=value.external_network_allowed,
            gpu_allowed=value.gpu_allowed,
            action_execution_allowed=value.action_execution_allowed,
            _builder_token=_STIMULUS_BUILDER_TOKEN,
        )

    def execute(
        self,
        *,
        comparison_id: str,
        stimulus: CpuFakeTopologyStimulusV1,
        isolated_executor: object,
        joint_executor: object,
    ) -> R24CpuTopologyComparisonV1:
        if type(self._authority) is not CpuFakeActiveAuthorityV1:
            raise R24ContractError("CPU_FAKE_AUTHORITY_REQUIRED", "authority state is invalid")
        if not isinstance(isolated_executor, CpuFakeTopologyExecutorV1) or not isinstance(
            joint_executor, CpuFakeTopologyExecutorV1
        ):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology executor is untrusted")
        authority_stimulus = self._snapshot_stimulus(stimulus)
        isolated_control = CpuFakeTopologyExecutionControlV1(
            authority_stimulus,
            TopologyKind.ISOLATED_HISTORY_FREE,
        )
        joint_control = CpuFakeTopologyExecutionControlV1(
            authority_stimulus,
            TopologyKind.JOINT_NON_INDEPENDENT,
        )
        isolated = snapshot_r23_topology_run(
            isolated_executor.execute(
                stimulus=self._snapshot_stimulus(authority_stimulus),
                control=isolated_control,
            )
        )
        joint = snapshot_r23_topology_run(
            joint_executor.execute(
                stimulus=self._snapshot_stimulus(authority_stimulus),
                control=joint_control,
            )
        )
        isolated_control.validate_result(isolated)
        joint_control.validate_result(joint)
        if isolated.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE:
            raise R24ContractError(
                "MISSING_ISOLATED_RUN", "primary input must be isolated history-free"
            )
        if joint.topology.kind is not TopologyKind.JOINT_NON_INDEPENDENT:
            raise R24ContractError(
                "INVALID_JOINT_RUN", "comparison input must label joint non-independent"
            )
        if isolated.status is TopologyRunStatus.NOT_RUN or joint.status is (
            TopologyRunStatus.NOT_RUN
        ):
            raise R24ContractError(
                "INCOMPLETE_TOPOLOGY_COMPARISON", "both CPU topology runs must execute"
            )
        source = TopologyComparisonV1(
            comparison_id=comparison_id,
            logical_call_id=authority_stimulus.logical_call_id,
            isolated=isolated,
            joint=joint,
        )
        # Force the exact trusted projection before deriving any comparison field.
        source_projection = topology_comparison_projection(source)
        source_hash = topology_comparison_sha256(source)
        if canonical_sha256(cast(JsonValue, source_projection)) != source_hash:
            raise R24ContractError(
                "SOURCE_TOPOLOGY_BINDING_MISMATCH", "R2.3 comparison hash is inconsistent"
            )
        isolated_ok = isolated.status is TopologyRunStatus.ADMITTED
        joint_ok = joint.status is TopologyRunStatus.ADMITTED
        agreement = (
            isolated.rubric_output_sha256 == joint.rubric_output_sha256
            if isolated_ok and joint_ok
            else None
        )
        return R24CpuTopologyComparisonV1(
            comparison_id=comparison_id,
            logical_call_id=authority_stimulus.logical_call_id,
            pair_id=authority_stimulus.pair_id,
            stimulus_sha256=cpu_fake_topology_stimulus_sha256(authority_stimulus),
            task_instruction_sha256=authority_stimulus.task_instruction_sha256,
            causal_cutoff_sha256=authority_stimulus.causal_cutoff_sha256,
            current_observation_sha256=authority_stimulus.current_observation_sha256,
            isolated_rubric_script_sha256=(authority_stimulus.isolated_rubric_script_sha256),
            isolated_policy_script_sha256=(authority_stimulus.isolated_policy_script_sha256),
            joint_script_sha256=authority_stimulus.joint_script_sha256,
            isolated_input_binding_sha256=isolated_control.expected_input_sha256,
            joint_input_binding_sha256=joint_control.expected_input_sha256,
            source_r23_comparison_sha256=source_hash,
            source_isolated_run_sha256=r23_topology_run_sha256(isolated),
            source_joint_run_sha256=r23_topology_run_sha256(joint),
            source_isolated_run=isolated,
            source_joint_run=joint,
            outcome=_classify_outcome(isolated, joint),
            failure_observation=_failure_observation(isolated, joint),
            isolated_status=isolated.status,
            joint_status=joint.status,
            isolated_succeeded=isolated_ok,
            joint_succeeded=joint_ok,
            isolated_timed_out=_is_timeout(isolated),
            joint_timed_out=_is_timeout(joint),
            output_agreement=agreement,
            isolated_observed_stages=isolated_control.observed_stages,
            joint_observed_stages=joint_control.observed_stages,
            isolated_call_count=len(isolated_control.observed_stages),
            joint_call_count=len(joint_control.observed_stages),
            isolated_latency_ns=isolated.total_latency_ns,
            joint_latency_ns=joint.total_latency_ns,
            _runner_token=_TOPOLOGY_RUNNER_TOKEN,
        )


__all__ = [
    "CpuFakeTopologyBackendInvocationV1",
    "CpuFakeTopologyComparisonRunnerV1",
    "CpuFakeTopologyExecutionControlV1",
    "CpuFakeTopologyExecutorV1",
    "CpuFakeTopologyStimulusV1",
    "PilotTopologySelectionStatusV1",
    "R24CpuTopologyComparisonV1",
    "R24TopologyOutcomeV1",
    "R24_TOPOLOGY_COMPARISON_SCHEMA_VERSION",
    "TopologyFailureObservationV1",
    "TopologyBackendStageV1",
    "build_cpu_fake_topology_stimulus",
    "cpu_fake_topology_stimulus_projection",
    "cpu_fake_topology_stimulus_sha256",
    "parse_r24_topology_comparison",
    "r23_topology_run_projection",
    "r23_topology_run_sha256",
    "r24_topology_comparison_projection",
    "r24_topology_comparison_sha256",
    "snapshot_r23_topology_run",
    "snapshot_r24_topology_comparison",
    "topology_input_binding_sha256",
]
