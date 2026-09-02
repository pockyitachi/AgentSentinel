"""History-free evidence snapshot and tracking-packet builder for R2.3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mobile_world.runtime.sentinel.r2_3.contracts import (
    CurrentObservationBindingV1,
    MultiPathRubricV1,
    RubricCutoffV1,
    RubricEvidenceV1,
    RubricTrackingPacketV1,
    RubricTrackingStateV1,
    TaskInstructionV1,
    TopologyDeclarationV1,
    TopologyKind,
    TrackingInputExclusionsV1,
    rubric_binding,
    validate_tracking_packet,
)


@dataclass(frozen=True, slots=True)
class RubricEvidenceSnapshotV1:
    """Only task/current-GUI/completed-transition evidence at one cutoff."""

    task_run_id: str
    step_id: str
    cutoff: RubricCutoffV1
    task: TaskInstructionV1
    current_observation: CurrentObservationBindingV1
    evidence_index: tuple[RubricEvidenceV1, ...]

    def __post_init__(self) -> None:
        if type(self.task_run_id) is not str or not self.task_run_id:
            raise ValueError("task_run_id must be a non-empty exact string")
        if type(self.step_id) is not str or not self.step_id:
            raise ValueError("step_id must be a non-empty exact string")
        if type(self.cutoff) is not RubricCutoffV1:
            raise TypeError("cutoff must use the exact R2.3 contract type")
        if type(self.task) is not TaskInstructionV1:
            raise TypeError("task must use the exact R2.3 contract type")
        if type(self.current_observation) is not CurrentObservationBindingV1:
            raise TypeError("current_observation must use the exact R2.3 contract type")
        if (
            type(self.evidence_index) is not tuple
            or not self.evidence_index
            or any(type(item) is not RubricEvidenceV1 for item in self.evidence_index)
        ):
            raise TypeError("evidence_index must contain exact R2.3 evidence values")
        if self.task_run_id != self.cutoff.task_run_id:
            raise ValueError("snapshot task and cutoff task differ")
        if self.step_id != self.cutoff.step_id:
            raise ValueError("snapshot step and cutoff step differ")


@runtime_checkable
class RubricEvidenceSnapshotProviderV1(Protocol):
    """R2.4 may bind Collector evidence behind this history-free interface."""

    def snapshot_for_step(
        self,
        *,
        task_run_id: str,
        step_id: str,
        logical_call_id: str,
    ) -> RubricEvidenceSnapshotV1: ...


class StaticRubricEvidenceSnapshotProviderV1:
    """Injected CPU/offline provider used by the R2.3 checkpoint tests."""

    def __init__(self, snapshot: RubricEvidenceSnapshotV1) -> None:
        if type(snapshot) is not RubricEvidenceSnapshotV1:
            raise TypeError("snapshot must be an exact RubricEvidenceSnapshotV1")
        self._snapshot = snapshot

    def snapshot_for_step(
        self,
        *,
        task_run_id: str,
        step_id: str,
        logical_call_id: str,
    ) -> RubricEvidenceSnapshotV1:
        del logical_call_id
        if task_run_id != self._snapshot.task_run_id or step_id != self._snapshot.step_id:
            raise ValueError("static rubric evidence binding drifted")
        return self._snapshot


class HistoryFreeTrackingPacketBuilderV1:
    """Construct the tracker input without accepting actor request or History IR."""

    def build(
        self,
        *,
        packet_id: str,
        logical_call_id: str,
        rubric: MultiPathRubricV1,
        prior_state: RubricTrackingStateV1,
        snapshot: RubricEvidenceSnapshotV1,
    ) -> RubricTrackingPacketV1:
        for value, expected, name in (
            (rubric, MultiPathRubricV1, "rubric"),
            (prior_state, RubricTrackingStateV1, "prior_state"),
            (snapshot, RubricEvidenceSnapshotV1, "snapshot"),
        ):
            if type(value) is not expected:
                raise TypeError(f"{name} must use the exact R2.3 contract type")
        packet = RubricTrackingPacketV1(
            packet_id=packet_id,
            logical_call_id=logical_call_id,
            task_run_id=snapshot.task_run_id,
            step_id=snapshot.step_id,
            rubric_binding=rubric_binding(rubric),
            prior_state=prior_state,
            cutoff=snapshot.cutoff,
            task=snapshot.task,
            current_observation=snapshot.current_observation,
            evidence_index=snapshot.evidence_index,
            input_exclusions=TrackingInputExclusionsV1(),
            topology=TopologyDeclarationV1(
                kind=TopologyKind.ISOLATED_HISTORY_FREE,
                independent_grounding_claim_eligible=True,
            ),
        )
        validate_tracking_packet(packet, rubric)
        return packet

    def build_from_provider(
        self,
        *,
        packet_id: str,
        logical_call_id: str,
        task_run_id: str,
        step_id: str,
        rubric: MultiPathRubricV1,
        prior_state: RubricTrackingStateV1,
        provider: RubricEvidenceSnapshotProviderV1,
    ) -> RubricTrackingPacketV1:
        if not isinstance(provider, RubricEvidenceSnapshotProviderV1):
            raise TypeError("provider does not implement the history-free snapshot interface")
        snapshot = provider.snapshot_for_step(
            task_run_id=task_run_id,
            step_id=step_id,
            logical_call_id=logical_call_id,
        )
        return self.build(
            packet_id=packet_id,
            logical_call_id=logical_call_id,
            rubric=rubric,
            prior_state=prior_state,
            snapshot=snapshot,
        )


__all__ = [
    "HistoryFreeTrackingPacketBuilderV1",
    "RubricEvidenceSnapshotProviderV1",
    "RubricEvidenceSnapshotV1",
    "StaticRubricEvidenceSnapshotProviderV1",
]
