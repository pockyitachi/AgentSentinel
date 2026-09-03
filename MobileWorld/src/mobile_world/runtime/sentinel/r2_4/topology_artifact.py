"""Strict persisted envelope for the module-owned CPU topology evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_3.contracts import TopologyKind
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.topology import (
    PilotTopologySelectionStatusV1,
    R24CpuTopologyComparisonV1,
    R24TopologyOutcomeV1,
    parse_r24_topology_comparison,
    r24_topology_comparison_projection,
    r24_topology_comparison_sha256,
)

R24_CPU_TOPOLOGY_ARTIFACT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-cpu-topology-artifact/v1"
)
R24_CPU_TOPOLOGY_PRODUCER_ID = "REAL_R23_SESSION_GPT56_POLICY_CPU_FAKE_V1"


@dataclass(frozen=True, slots=True)
class CpuTopologyComponentCensusV1:
    topology: TopologyKind
    setup_rubric_provider_calls: int
    comparison_provider_dispatches: int
    rubric_session_receipts: int
    policy_admission_adapter_calls: int
    policy_receipts: int
    policy_evaluations: int
    rubric_output_admitted: bool
    history_policy_output_admitted: bool

    def __post_init__(self) -> None:
        if type(self.topology) is not TopologyKind:
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH", "topology census kind differs"
            )
        observed = (
            self.setup_rubric_provider_calls,
            self.comparison_provider_dispatches,
            self.rubric_session_receipts,
            self.policy_admission_adapter_calls,
            self.policy_receipts,
            self.policy_evaluations,
        )
        if any(type(value) is not int for value in observed):
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "real CPU producer census contains a non-integer",
            )
        for value in (self.rubric_output_admitted, self.history_policy_output_admitted):
            if type(value) is not bool:
                raise R24ContractError(
                    "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                    "component admission census contains a non-bool",
                )
        admitted = self.rubric_output_admitted and self.history_policy_output_admitted
        if admitted:
            expected = (
                1,
                2 if self.topology is TopologyKind.ISOLATED_HISTORY_FREE else 1,
                3,
                1,
                1,
                1,
            )
        elif self.topology is TopologyKind.JOINT_NON_INDEPENDENT and not (
            self.rubric_output_admitted or self.history_policy_output_admitted
        ):
            expected = (1, 1, 2, 1, 1, 1)
        else:
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "only one output from a joint provider call was admitted",
            )
        if observed != expected:
            raise R24ContractError(
                "CPU_TOPOLOGY_COMPONENT_CENSUS_MISMATCH",
                "real CPU producer did not execute the exact R2.3/R2.2 component census",
            )


@dataclass(frozen=True, slots=True)
class CpuTopologyJointFailureProbeV1:
    provider_dispatches: int
    rubric_output_admitted: bool
    history_policy_output_admitted: bool
    failure_coupled: bool

    def __post_init__(self) -> None:
        if (
            type(self.provider_dispatches) is not int
            or self.provider_dispatches != 1
            or type(self.rubric_output_admitted) is not bool  # type: ignore[redundant-expr]
            or self.rubric_output_admitted
            or type(self.history_policy_output_admitted) is not bool  # type: ignore[redundant-expr]
            or self.history_policy_output_admitted
            or type(self.failure_coupled) is not bool  # type: ignore[redundant-expr]
            or not self.failure_coupled
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_FAILURE_COUPLING_MISMATCH",
                "one failed joint dispatch must make both component outputs unavailable",
            )


@dataclass(frozen=True, slots=True)
class R24CpuTopologyArtifactV1:
    comparison: R24CpuTopologyComparisonV1
    isolated_components: CpuTopologyComponentCensusV1
    joint_components: CpuTopologyComponentCensusV1
    joint_failure_probe: CpuTopologyJointFailureProbeV1
    producer_id: str = R24_CPU_TOPOLOGY_PRODUCER_ID
    schema_version: str = R24_CPU_TOPOLOGY_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R24_CPU_TOPOLOGY_ARTIFACT_SCHEMA_VERSION:
            raise R24ContractError("UNKNOWN_SCHEMA", "CPU topology artifact schema differs")
        if self.producer_id != R24_CPU_TOPOLOGY_PRODUCER_ID:
            raise R24ContractError("UNKNOWN_TOPOLOGY_PRODUCER", "CPU producer id differs")
        if type(self.comparison) is not R24CpuTopologyComparisonV1:
            raise R24ContractError(
                "UNTRUSTED_TOPOLOGY_COMPARISON", "producer comparison type differs"
            )
        if (
            type(self.isolated_components) is not CpuTopologyComponentCensusV1
            or type(self.joint_components) is not CpuTopologyComponentCensusV1
            or type(self.joint_failure_probe) is not CpuTopologyJointFailureProbeV1
        ):
            raise R24ContractError(
                "UNTRUSTED_COMPONENT_CENSUS", "producer component census type differs"
            )
        if (
            self.isolated_components.topology is not TopologyKind.ISOLATED_HISTORY_FREE
            or self.joint_components.topology is not TopologyKind.JOINT_NON_INDEPENDENT
            or not self.isolated_components.rubric_output_admitted
            or not self.isolated_components.history_policy_output_admitted
            or not self.joint_components.rubric_output_admitted
            or not self.joint_components.history_policy_output_admitted
            or self.comparison.outcome is not R24TopologyOutcomeV1.BOTH_ADMITTED_AGREE
            or self.comparison.output_agreement is not True
            or self.comparison.pilot_selection_status  # type: ignore[redundant-expr]
            is not PilotTopologySelectionStatusV1.FROZEN_FOR_R25
            or self.comparison.proposed_pilot_topology is not TopologyKind.ISOLATED_HISTORY_FREE
            or not self.comparison.deployment_topology_frozen
        ):
            raise R24ContractError(
                "CPU_TOPOLOGY_ARTIFACT_NOT_ADMISSIBLE",
                "persisted CPU evidence cannot freeze the isolated pilot topology",
            )


def _component_projection(value: CpuTopologyComponentCensusV1) -> dict[str, JsonValue]:
    if type(value) is not CpuTopologyComponentCensusV1:
        raise R24ContractError("UNTRUSTED_COMPONENT_CENSUS", "component census type differs")
    trusted = CpuTopologyComponentCensusV1(
        topology=value.topology,
        setup_rubric_provider_calls=value.setup_rubric_provider_calls,
        comparison_provider_dispatches=value.comparison_provider_dispatches,
        rubric_session_receipts=value.rubric_session_receipts,
        policy_admission_adapter_calls=value.policy_admission_adapter_calls,
        policy_receipts=value.policy_receipts,
        policy_evaluations=value.policy_evaluations,
        rubric_output_admitted=value.rubric_output_admitted,
        history_policy_output_admitted=value.history_policy_output_admitted,
    )
    return {
        "comparison_provider_dispatches": trusted.comparison_provider_dispatches,
        "history_policy_output_admitted": trusted.history_policy_output_admitted,
        "policy_admission_adapter_calls": trusted.policy_admission_adapter_calls,
        "policy_evaluations": trusted.policy_evaluations,
        "policy_receipts": trusted.policy_receipts,
        "rubric_output_admitted": trusted.rubric_output_admitted,
        "rubric_session_receipts": trusted.rubric_session_receipts,
        "setup_rubric_provider_calls": trusted.setup_rubric_provider_calls,
        "topology": trusted.topology.value,
    }


def _failure_probe_projection(value: CpuTopologyJointFailureProbeV1) -> dict[str, JsonValue]:
    if type(value) is not CpuTopologyJointFailureProbeV1:
        raise R24ContractError("UNTRUSTED_FAILURE_PROBE", "failure probe type differs")
    trusted = CpuTopologyJointFailureProbeV1(
        provider_dispatches=value.provider_dispatches,
        rubric_output_admitted=value.rubric_output_admitted,
        history_policy_output_admitted=value.history_policy_output_admitted,
        failure_coupled=value.failure_coupled,
    )
    return {
        "failure_coupled": trusted.failure_coupled,
        "history_policy_output_admitted": trusted.history_policy_output_admitted,
        "provider_dispatches": trusted.provider_dispatches,
        "rubric_output_admitted": trusted.rubric_output_admitted,
    }


def r24_cpu_topology_artifact_projection(
    value: R24CpuTopologyArtifactV1,
) -> dict[str, JsonValue]:
    if type(value) is not R24CpuTopologyArtifactV1:
        raise R24ContractError("UNTRUSTED_TOPOLOGY_ARTIFACT", "artifact type differs")
    trusted = R24CpuTopologyArtifactV1(
        comparison=value.comparison,
        isolated_components=value.isolated_components,
        joint_components=value.joint_components,
        joint_failure_probe=value.joint_failure_probe,
        producer_id=value.producer_id,
        schema_version=value.schema_version,
    )
    comparison = cast(JsonValue, r24_topology_comparison_projection(trusted.comparison))
    return {
        "comparison": comparison,
        "comparison_sha256": r24_topology_comparison_sha256(trusted.comparison),
        "isolated_component_census": _component_projection(trusted.isolated_components),
        "joint_component_census": _component_projection(trusted.joint_components),
        "joint_failure_probe": _failure_probe_projection(trusted.joint_failure_probe),
        "producer_id": trusted.producer_id,
        "schema_version": trusted.schema_version,
    }


_ARTIFACT_FIELDS = frozenset(
    {
        "comparison",
        "comparison_sha256",
        "isolated_component_census",
        "joint_component_census",
        "joint_failure_probe",
        "producer_id",
        "schema_version",
    }
)
_COMPONENT_FIELDS = frozenset(
    {
        "comparison_provider_dispatches",
        "history_policy_output_admitted",
        "policy_admission_adapter_calls",
        "policy_evaluations",
        "policy_receipts",
        "rubric_output_admitted",
        "rubric_session_receipts",
        "setup_rubric_provider_calls",
        "topology",
    }
)
_FAILURE_PROBE_FIELDS = frozenset(
    {
        "failure_coupled",
        "history_policy_output_admitted",
        "provider_dispatches",
        "rubric_output_admitted",
    }
)


def _exact_object(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", f"{name} fields differ")
    return cast(dict[str, object], value)


def _topology(value: object) -> TopologyKind:
    if type(value) is not str:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "census topology is not a string")
    try:
        return TopologyKind(value)
    except ValueError as exc:
        raise R24ContractError("INVALID_TOPOLOGY", "census topology differs") from exc


def _parse_component(value: object) -> CpuTopologyComponentCensusV1:
    item = _exact_object(value, _COMPONENT_FIELDS, "component census")
    return CpuTopologyComponentCensusV1(
        topology=_topology(item["topology"]),
        setup_rubric_provider_calls=cast(int, item["setup_rubric_provider_calls"]),
        comparison_provider_dispatches=cast(int, item["comparison_provider_dispatches"]),
        rubric_session_receipts=cast(int, item["rubric_session_receipts"]),
        policy_admission_adapter_calls=cast(int, item["policy_admission_adapter_calls"]),
        policy_receipts=cast(int, item["policy_receipts"]),
        policy_evaluations=cast(int, item["policy_evaluations"]),
        rubric_output_admitted=cast(bool, item["rubric_output_admitted"]),
        history_policy_output_admitted=cast(bool, item["history_policy_output_admitted"]),
    )


def _parse_failure_probe(value: object) -> CpuTopologyJointFailureProbeV1:
    item = _exact_object(value, _FAILURE_PROBE_FIELDS, "joint failure probe")
    return CpuTopologyJointFailureProbeV1(
        provider_dispatches=cast(int, item["provider_dispatches"]),
        rubric_output_admitted=cast(bool, item["rubric_output_admitted"]),
        history_policy_output_admitted=cast(bool, item["history_policy_output_admitted"]),
        failure_coupled=cast(bool, item["failure_coupled"]),
    )


def parse_r24_cpu_topology_artifact(value: object) -> R24CpuTopologyArtifactV1:
    """Strictly parse the exact persisted artifact, including real call census."""

    item = _exact_object(value, _ARTIFACT_FIELDS, "CPU topology artifact")
    comparison = parse_r24_topology_comparison(item["comparison"])
    artifact = R24CpuTopologyArtifactV1(
        comparison=comparison,
        isolated_components=_parse_component(item["isolated_component_census"]),
        joint_components=_parse_component(item["joint_component_census"]),
        joint_failure_probe=_parse_failure_probe(item["joint_failure_probe"]),
        producer_id=cast(str, item["producer_id"]),
        schema_version=cast(str, item["schema_version"]),
    )
    if item["comparison_sha256"] != r24_topology_comparison_sha256(comparison):
        raise R24ContractError(
            "TOPOLOGY_COMPARISON_BINDING_MISMATCH", "nested comparison hash differs"
        )
    if r24_cpu_topology_artifact_projection(artifact) != value:
        raise R24ContractError(
            "TOPOLOGY_ARTIFACT_DERIVATION_MISMATCH",
            "CPU topology artifact differs from its recomputed projection",
        )
    return artifact


def r24_cpu_topology_artifact_sha256(value: R24CpuTopologyArtifactV1) -> str:
    return canonical_sha256(cast(JsonValue, r24_cpu_topology_artifact_projection(value)))


__all__ = [
    "CpuTopologyComponentCensusV1",
    "CpuTopologyJointFailureProbeV1",
    "R24CpuTopologyArtifactV1",
    "R24_CPU_TOPOLOGY_ARTIFACT_SCHEMA_VERSION",
    "R24_CPU_TOPOLOGY_PRODUCER_ID",
    "parse_r24_cpu_topology_artifact",
    "r24_cpu_topology_artifact_projection",
    "r24_cpu_topology_artifact_sha256",
]
