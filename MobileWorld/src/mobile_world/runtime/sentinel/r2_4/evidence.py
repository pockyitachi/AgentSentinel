"""Read-only Collector-v1 to R2.2 evidence plumbing for the R2.4 runtime.

The provider in this module is deliberately narrow.  It reads the task stream
already bound to the current :class:`AuditContext`, freezes the causal cutoff at
that step's persisted ``step_started`` event, and projects only the closed R2.2
evidence roles.  It never appends to Collector, reads a benchmark checker, or
uses actor history as evidence.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PIL import Image

try:  # pragma: no cover - MobileWorld's supported runtime is POSIX.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonPath,
    JsonValue,
    RegionKind,
    canonical_sha256,
)
from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.context import AuditContext, get_audit_context
from mobile_world.runtime.audit.recorder import TaskRecorder
from mobile_world.runtime.audit.schemas import validate_event_envelope
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    CurrentObservationV1,
    EvidenceCutoffV1,
    EvidenceEntryV1,
    EvidenceInputExclusionsV1,
    EvidenceMediaType,
    EvidencePacketV1,
    EvidenceRole,
    EvidenceSemanticScope,
    ImageEvidenceProjectionV1,
    SourceEventType,
    TaskInstructionDataV1,
    TextEvidenceProjectionV1,
    exact_canonical_json_text,
)
from mobile_world.runtime.sentinel.r2_2.evidence import (
    CausalEvidenceSnapshotV1,
    EvidencePacketBuilder,
)
from mobile_world.runtime.sentinel.r2_2.gpt56_policy import GPT56EvidenceInputV1
from mobile_world.runtime.sentinel.r2_2.runtime_overlay import make_gpt_evidence_input
from mobile_world.runtime.sentinel.r2_3.contracts import (
    CurrentObservationBindingV1 as RubricCurrentObservationBindingV1,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    EvidenceMediaType as RubricEvidenceMediaType,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    EvidenceProjectionKind as RubricEvidenceProjectionKind,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    ImageEvidenceProjectionV1 as RubricImageEvidenceProjectionV1,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    RubricCutoffV1,
    RubricEvidenceRole,
    RubricEvidenceV1,
    RubricSourceEventType,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    TaskInstructionV1 as RubricTaskInstructionV1,
)
from mobile_world.runtime.sentinel.r2_3.contracts import (
    TextEvidenceProjectionV1 as RubricTextEvidenceProjectionV1,
)
from mobile_world.runtime.sentinel.r2_3.packet import RubricEvidenceSnapshotV1

_DATA_IMAGE = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})")
_ARTIFACT_SNAPSHOT_KEY = "$artifact_snapshot"
R23_STIMULUS_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-rubric-stimulus/v1"


class CollectorEvidenceError(RuntimeError):
    """Typed, stable fail-closed error at the Collector evidence boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class CollectorEvidenceLimitsV1:
    """Resource limits applied before parsing any Collector-owned bytes."""

    max_stream_bytes: int = 32 * 1024 * 1024
    max_event_line_bytes: int = 2 * 1024 * 1024
    max_events: int = 8192
    max_image_bytes: int = 40 * 1024 * 1024
    max_image_pixels: int = 32 * 1024 * 1024
    max_packet_evidence: int = 512
    max_request_nodes: int = 262_144
    max_request_depth: int = 64

    def __post_init__(self) -> None:
        for name in (
            "max_stream_bytes",
            "max_event_line_bytes",
            "max_events",
            "max_image_bytes",
            "max_image_pixels",
            "max_packet_evidence",
            "max_request_nodes",
            "max_request_depth",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive exact integer")
        if self.max_event_line_bytes > self.max_stream_bytes:
            raise ValueError("max_event_line_bytes cannot exceed max_stream_bytes")
        if self.max_packet_evidence > 512:
            raise ValueError("max_packet_evidence cannot exceed the R2.2 schema bound")


@dataclass(frozen=True, slots=True)
class _CurrentImage:
    path: JsonPath
    request_value_sha256: str
    data_url: str
    content_sha256: str
    media_type: EvidenceMediaType
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class _BuiltSnapshot:
    snapshot: CausalEvidenceSnapshotV1
    rubric_snapshot: RubricEvidenceSnapshotV1
    current_image_data_url: str


@dataclass(frozen=True, slots=True)
class CollectorEvidenceBundleV1:
    """One-read detached projections for both independent runtime axes."""

    r22_snapshot: CausalEvidenceSnapshotV1
    r22_packet: EvidencePacketV1
    gpt56_input: GPT56EvidenceInputV1
    r23_snapshot: RubricEvidenceSnapshotV1

    def __post_init__(self) -> None:
        for value, expected, name in (
            (self.r22_snapshot, CausalEvidenceSnapshotV1, "r22_snapshot"),
            (self.r22_packet, EvidencePacketV1, "r22_packet"),
            (self.gpt56_input, GPT56EvidenceInputV1, "gpt56_input"),
            (self.r23_snapshot, RubricEvidenceSnapshotV1, "r23_snapshot"),
        ):
            if type(value) is not expected:
                raise TypeError(f"{name} must use its exact trusted contract type")
        if (
            self.r22_packet.cutoff != self.r22_snapshot.cutoff
            or self.r22_packet.task != self.r22_snapshot.task
            or self.r22_packet.current_observation != self.r22_snapshot.current_observation
            or set(item.evidence_id for item in self.r22_packet.evidence_index)
            != set(item.evidence_id for item in self.r22_snapshot.evidence_index)
            or self.gpt56_input.packet != self.r22_packet
        ):
            raise ValueError("R2.2 bundle components do not bind the same evidence snapshot")
        if (
            self.r23_snapshot.task_run_id != self.r22_snapshot.cutoff.task_run_id
            or self.r23_snapshot.step_id != self.r22_snapshot.cutoff.step_id
            or self.r23_snapshot.cutoff.run_id != self.r22_snapshot.cutoff.run_id
            or self.r23_snapshot.cutoff.current_observation_event_id
            != self.r22_snapshot.cutoff.current_observation_event_id
            or self.r23_snapshot.cutoff.cutoff_event_seq
            != self.r22_snapshot.cutoff.cutoff_event_seq
            or self.r23_snapshot.task.exact_text != self.r22_snapshot.task.exact_text
            or self.r23_snapshot.current_observation.screenshot_content_sha256
            != self.r22_snapshot.current_observation.screenshot_content_sha256
        ):
            raise ValueError("R2.3 bundle component does not bind the R2.2 causal cutoff")

    @property
    def r23_snapshot_sha256(self) -> str:
        """Hash only the history-free rubric stimulus, never actor history/request."""

        return rubric_evidence_snapshot_sha256(self.r23_snapshot)


@dataclass(frozen=True, slots=True)
class CollectorRubricOnlyBundleV1:
    """Detached history-free projection for an exact ``NO_HISTORY`` call.

    This bundle deliberately cannot be consumed by the R2.2 history policy. It
    contains only the R2.3 stimulus and the current screenshot bytes required by
    the rubric provider. The screenshot remains bound to the same Collector
    cutoff and request image as the ordinary combined evidence bundle.
    """

    r23_snapshot: RubricEvidenceSnapshotV1
    current_image_data_url: str
    current_image_sha256: str

    def __post_init__(self) -> None:
        if type(self.r23_snapshot) is not RubricEvidenceSnapshotV1:
            raise TypeError("r23_snapshot must use its exact trusted contract type")
        if type(self.current_image_data_url) is not str or not self.current_image_data_url:
            raise TypeError("current_image_data_url must be non-empty exact text")
        if (
            type(self.current_image_sha256) is not str
            or len(self.current_image_sha256) != 64
            or any(item not in "0123456789abcdef" for item in self.current_image_sha256)
            or self.r23_snapshot.current_observation.screenshot_content_sha256
            != self.current_image_sha256
        ):
            raise ValueError("rubric-only current image binding differs")

    @property
    def r23_snapshot_sha256(self) -> str:
        return rubric_evidence_snapshot_sha256(self.r23_snapshot)


class CollectorEvidenceFactoryV1:
    """Build detached R2.2/GPT56 evidence from the current Collector task stream.

    Instances are stateless and callable with the exact signature required by
    :class:`GPT56SentinelPolicy`.  Every invocation rereads and revalidates the
    authoritative append-only task stream; no caller-owned result is cached.
    """

    def __init__(self, *, limits: CollectorEvidenceLimitsV1 | None = None) -> None:
        if limits is not None and type(limits) is not CollectorEvidenceLimitsV1:
            raise TypeError("limits must use exact CollectorEvidenceLimitsV1")
        self._limits = limits or CollectorEvidenceLimitsV1()

    @property
    def limits(self) -> CollectorEvidenceLimitsV1:
        """Return a fresh exact copy of the configured limits."""

        value = self._limits
        return CollectorEvidenceLimitsV1(
            max_stream_bytes=value.max_stream_bytes,
            max_event_line_bytes=value.max_event_line_bytes,
            max_events=value.max_events,
            max_image_bytes=value.max_image_bytes,
            max_image_pixels=value.max_image_pixels,
            max_packet_evidence=value.max_packet_evidence,
            max_request_nodes=value.max_request_nodes,
            max_request_depth=value.max_request_depth,
        )

    def snapshot_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> CausalEvidenceSnapshotV1:
        """Return the closed, detached causal evidence snapshot for one call."""

        return self._build(request=request, context=context, history_ir=history_ir).snapshot

    def rubric_snapshot_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RubricEvidenceSnapshotV1:
        """Return the history-free R2.3 view from the same Collector cutoff."""

        return self._build(
            request=request,
            context=context,
            history_ir=history_ir,
        ).rubric_snapshot

    def packet_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> EvidencePacketV1:
        """Return the module-owned R2.2 packet after rebuilding all bindings."""

        built = self._build(request=request, context=context, history_ir=history_ir)
        return self._packet(
            request=request,
            context=context,
            history_ir=history_ir,
            snapshot=built.snapshot,
        )

    def __call__(
        self,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> GPT56EvidenceInputV1:
        """Build the exact transport-safe input consumed by GPT56 policy code."""

        return self.bundle_for_call(
            request=request,
            context=context,
            history_ir=history_ir,
        ).gpt56_input

    def bundle_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> CollectorEvidenceBundleV1:
        """Read Collector once and build both R2.2 and history-free R2.3 views."""

        built = self._build(request=request, context=context, history_ir=history_ir)
        packet = self._packet(
            request=request,
            context=context,
            history_ir=history_ir,
            snapshot=built.snapshot,
        )
        try:
            gpt56_input = make_gpt_evidence_input(
                packet,
                current_image_data_url=built.current_image_data_url,
            )
        except Exception as exc:
            raise CollectorEvidenceError(
                "GPT56_EVIDENCE_INPUT_REJECTED",
                "trusted R2.2 packet could not enter the GPT56 input contract",
            ) from exc
        return CollectorEvidenceBundleV1(
            r22_snapshot=built.snapshot,
            r22_packet=packet,
            gpt56_input=gpt56_input,
            r23_snapshot=built.rubric_snapshot,
        )

    def rubric_only_bundle_for_no_history_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
    ) -> CollectorRubricOnlyBundleV1:
        """Build only the independent R2.3 axis for a typed no-history call.

        The runtime codec has already admitted one of the two exact first-call
        host shapes before this method is entered. This boundary independently
        revalidates the host-specific current-image location against Collector
        pixels and never manufactures a History IR or an R2.2 target packet.
        """

        if type(context) is not SentinelContext:
            raise CollectorEvidenceError(
                "UNTRUSTED_CALL_INPUT", "context must use its exact trusted runtime type"
            )
        try:
            request_sha256 = canonical_sha256(request)
        except Exception as exc:
            raise CollectorEvidenceError(
                "NON_CANONICAL_ACTOR_REQUEST", "actor request is outside exact JSON"
            ) from exc
        current_roots = _no_history_current_roots(request, context.host_id)
        audit_context = get_audit_context()
        recorder = _trusted_task_recorder(audit_context)
        events = _read_task_events(
            recorder=recorder,
            audit_context=cast(AuditContext, audit_context),
            limits=self._limits,
        )
        current_event, task_event = _resolve_cutoff_events(
            events=events,
            audit_context=cast(AuditContext, audit_context),
        )
        current_image = _bind_current_image_at_roots(
            request=request,
            current_roots=current_roots,
            current_event=current_event,
            recorder=recorder,
            limits=self._limits,
        )
        try:
            snapshot = _project_snapshot(
                events=events,
                current_event=current_event,
                task_event=task_event,
                audit_context=cast(AuditContext, audit_context),
                request_sha256=request_sha256,
                current_image=current_image,
                limits=self._limits,
            )
            rubric_snapshot = _rubric_snapshot_from_r22(snapshot)
        except CollectorEvidenceError:
            raise
        except Exception as exc:
            raise CollectorEvidenceError(
                "EVIDENCE_PROJECTION_REJECTED",
                "Collector events could not enter the closed R2.3 evidence types",
            ) from exc
        return CollectorRubricOnlyBundleV1(
            r23_snapshot=rubric_snapshot,
            current_image_data_url=current_image.data_url,
            current_image_sha256=current_image.content_sha256,
        )

    @staticmethod
    def _packet(
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        snapshot: CausalEvidenceSnapshotV1,
    ) -> EvidencePacketV1:
        try:
            return EvidencePacketBuilder().build(
                request=request,
                context=context,
                history_ir=history_ir,
                snapshot=snapshot,
            )
        except Exception as exc:
            raise CollectorEvidenceError(
                "EVIDENCE_PACKET_REJECTED",
                "Collector projection did not satisfy the R2.2 actor-call bindings",
            ) from exc

    def _build(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> _BuiltSnapshot:
        if type(context) is not SentinelContext or type(history_ir) is not HistoryIR:
            raise CollectorEvidenceError(
                "UNTRUSTED_CALL_INPUT",
                "context and History IR must use their exact trusted runtime types",
            )
        try:
            request_sha256 = canonical_sha256(request)
        except Exception as exc:
            raise CollectorEvidenceError(
                "NON_CANONICAL_ACTOR_REQUEST", "actor request is outside exact JSON"
            ) from exc
        if request_sha256 != history_ir.raw_request_sha256:
            raise CollectorEvidenceError(
                "HISTORY_IR_REQUEST_DRIFT", "History IR binds a different actor request"
            )

        audit_context = get_audit_context()
        recorder = _trusted_task_recorder(audit_context)
        events = _read_task_events(
            recorder=recorder,
            audit_context=cast(AuditContext, audit_context),
            limits=self._limits,
        )
        current_event, task_event = _resolve_cutoff_events(
            events=events,
            audit_context=cast(AuditContext, audit_context),
        )
        current_image = _bind_current_image(
            request=request,
            history_ir=history_ir,
            current_event=current_event,
            recorder=recorder,
            limits=self._limits,
        )
        try:
            snapshot = _project_snapshot(
                events=events,
                current_event=current_event,
                task_event=task_event,
                audit_context=cast(AuditContext, audit_context),
                request_sha256=request_sha256,
                current_image=current_image,
                limits=self._limits,
            )
        except CollectorEvidenceError:
            raise
        except Exception as exc:
            raise CollectorEvidenceError(
                "EVIDENCE_PROJECTION_REJECTED",
                "Collector events could not enter the closed R2.2 evidence types",
            ) from exc
        return _BuiltSnapshot(
            snapshot=snapshot,
            rubric_snapshot=_rubric_snapshot_from_r22(snapshot),
            current_image_data_url=current_image.data_url,
        )


def build_collector_gpt56_evidence_factory(
    *, limits: CollectorEvidenceLimitsV1 | None = None
) -> CollectorEvidenceFactoryV1:
    """Return the callable, CPU-only Collector evidence factory for R2.4."""

    return CollectorEvidenceFactoryV1(limits=limits)


def rubric_evidence_snapshot_projection(
    value: RubricEvidenceSnapshotV1,
) -> dict[str, JsonValue]:
    """Project the exact R2.3 history-free stimulus for matched-pair hashing."""

    if type(value) is not RubricEvidenceSnapshotV1:
        raise TypeError("value must use exact RubricEvidenceSnapshotV1")
    return {
        "schema_version": R23_STIMULUS_SCHEMA_VERSION,
        "task_run_id": value.task_run_id,
        "step_id": value.step_id,
        "cutoff": {
            "run_id": value.cutoff.run_id,
            "task_run_id": value.cutoff.task_run_id,
            "step_id": value.cutoff.step_id,
            "current_observation_event_id": value.cutoff.current_observation_event_id,
            "cutoff_event_seq": value.cutoff.cutoff_event_seq,
        },
        "task": {
            "source_event_id": value.task.source_event_id,
            "source_event_seq": value.task.source_event_seq,
            "exact_text": value.task.exact_text,
            "text_sha256": value.task.text_sha256,
            "source_event_type": value.task.source_event_type.value,
        },
        "current_observation": {
            "source_event_id": value.current_observation.source_event_id,
            "source_event_seq": value.current_observation.source_event_seq,
            "screenshot_evidence_id": value.current_observation.screenshot_evidence_id,
            "screenshot_content_sha256": (value.current_observation.screenshot_content_sha256),
            "accessibility_evidence_ids": list(
                value.current_observation.accessibility_evidence_ids
            ),
        },
        "evidence_index": [_rubric_evidence_projection(item) for item in value.evidence_index],
    }


def rubric_evidence_snapshot_sha256(value: RubricEvidenceSnapshotV1) -> str:
    """Return the module-owned hash of the history-free R2.3 stimulus."""

    projection = rubric_evidence_snapshot_projection(value)
    return hashlib.sha256(exact_canonical_json_text(projection).encode("utf-8")).hexdigest()


def _rubric_evidence_projection(value: RubricEvidenceV1) -> dict[str, JsonValue]:
    if type(value) is not RubricEvidenceV1:
        raise TypeError("rubric evidence must use exact RubricEvidenceV1")
    projection = value.projection
    if type(projection) is RubricImageEvidenceProjectionV1:
        projected: dict[str, JsonValue] = {
            "kind": projection.kind.value,
            "content_sha256": projection.content_sha256,
            "media_type": projection.media_type.value,
            "width": projection.width,
            "height": projection.height,
        }
    elif type(projection) is RubricTextEvidenceProjectionV1:
        projected = {
            "kind": projection.kind.value,
            "exact_text": projection.exact_text,
            "text_sha256": projection.text_sha256,
        }
    else:  # pragma: no cover - guarded by the R2.3 exact contract.
        raise TypeError("rubric evidence projection type is untrusted")
    return {
        "evidence_id": value.evidence_id,
        "role": value.role.value,
        "source_event_id": value.source_event_id,
        "source_event_type": value.source_event_type.value,
        "source_event_seq": value.source_event_seq,
        "task_run_id": value.task_run_id,
        "caused_by_event_id": value.caused_by_event_id,
        "payload_sha256": value.payload_sha256,
        "projection": projected,
        "observed_by_cutoff": value.observed_by_cutoff,
    }


def _rubric_snapshot_from_r22(
    value: CausalEvidenceSnapshotV1,
) -> RubricEvidenceSnapshotV1:
    role_map = {
        EvidenceRole.CURRENT_UI_SCREENSHOT: RubricEvidenceRole.CURRENT_UI_SCREENSHOT,
        EvidenceRole.CURRENT_ACCESSIBILITY: RubricEvidenceRole.CURRENT_ACCESSIBILITY,
        EvidenceRole.PRIOR_TRANSITION_STATUS: (RubricEvidenceRole.COMPLETED_TRANSITION_STATUS),
        EvidenceRole.PRIOR_POST_UI_STATE: RubricEvidenceRole.COMPLETED_POST_UI_STATE,
        EvidenceRole.AGENT_VISIBLE_TOOL_RESULT: (RubricEvidenceRole.AGENT_VISIBLE_TOOL_RESULT),
        EvidenceRole.USER_RESPONSE: RubricEvidenceRole.USER_RESPONSE,
    }
    evidence: list[RubricEvidenceV1] = []
    for item in value.evidence_index:
        role = role_map.get(item.role)
        if role is None:
            # R2.3 intentionally excludes action-attempt and executor transport
            # evidence; neither is a milestone-truth authority.
            continue
        source_type = RubricSourceEventType(item.source_event_type.value)
        source_projection = item.projection
        if type(source_projection) is ImageEvidenceProjectionV1:
            projection: RubricImageEvidenceProjectionV1 | RubricTextEvidenceProjectionV1 = (
                RubricImageEvidenceProjectionV1(
                    content_sha256=source_projection.content_sha256,
                    media_type=RubricEvidenceMediaType(source_projection.media_type.value),
                    width=source_projection.width,
                    height=source_projection.height,
                )
            )
        elif type(source_projection) is TextEvidenceProjectionV1:
            projection = RubricTextEvidenceProjectionV1(
                kind=RubricEvidenceProjectionKind(source_projection.projection_type.value),
                exact_text=source_projection.exact_text,
                text_sha256=source_projection.text_sha256,
            )
        else:  # pragma: no cover - guarded by the R2.2 exact contract.
            raise TypeError("R2.2 evidence projection type is untrusted")
        evidence.append(
            RubricEvidenceV1(
                evidence_id=item.evidence_id,
                role=role,
                source_event_id=item.source_event_id,
                source_event_type=source_type,
                source_event_seq=item.source_event_seq,
                task_run_id=item.task_run_id,
                caused_by_event_id=item.caused_by_event_id,
                payload_sha256=item.payload_sha256,
                projection=projection,
                observed_by_cutoff=item.observed_by_cutoff,
            )
        )
    return RubricEvidenceSnapshotV1(
        task_run_id=value.cutoff.task_run_id,
        step_id=value.cutoff.step_id,
        cutoff=RubricCutoffV1(
            run_id=value.cutoff.run_id,
            task_run_id=value.cutoff.task_run_id,
            step_id=value.cutoff.step_id,
            current_observation_event_id=value.cutoff.current_observation_event_id,
            cutoff_event_seq=value.cutoff.cutoff_event_seq,
        ),
        task=RubricTaskInstructionV1(
            source_event_id=value.task.source_event_id,
            source_event_seq=value.task.source_event_seq,
            exact_text=value.task.exact_text,
            text_sha256=value.task.text_sha256,
        ),
        current_observation=RubricCurrentObservationBindingV1(
            source_event_id=value.current_observation.source_event_id,
            source_event_seq=value.current_observation.source_event_seq,
            screenshot_evidence_id=value.current_observation.screenshot_evidence_id,
            screenshot_content_sha256=value.current_observation.screenshot_content_sha256,
            accessibility_evidence_ids=value.current_observation.accessibility_evidence_ids,
        ),
        evidence_index=tuple(evidence),
    )


def _trusted_task_recorder(context: AuditContext | None) -> TaskRecorder:
    if context is None:
        raise CollectorEvidenceError("NO_AUDIT_CONTEXT", "no Collector context is bound")
    if type(context) is not AuditContext:
        raise CollectorEvidenceError(
            "UNTRUSTED_AUDIT_CONTEXT", "audit context must use exact AuditContext"
        )
    recorder = context.recorder
    if type(recorder) is not TaskRecorder:
        raise CollectorEvidenceError(
            "UNTRUSTED_TASK_RECORDER", "current audit recorder is not an exact TaskRecorder"
        )
    if recorder.enabled is not True:
        raise CollectorEvidenceError("COLLECTOR_DISABLED", "current task recorder is disabled")
    if (
        type(context.run_id) is not str
        or type(context.task_run_id) is not str
        or type(context.step_id) is not str
        or type(context.parent_event_id) is not str
    ):
        raise CollectorEvidenceError(
            "INCOMPLETE_AUDIT_CONTEXT", "run, task, step, and step-event IDs are required"
        )
    if context.task_run_id != recorder.task_run_id:
        raise CollectorEvidenceError(
            "TASK_RECORDER_BINDING_MISMATCH", "audit context binds another task recorder"
        )
    if type(recorder.blob_store) is not BlobStore:
        raise CollectorEvidenceError(
            "UNTRUSTED_BLOB_STORE", "task recorder does not expose the exact BlobStore"
        )
    return recorder


def _read_task_events(
    *,
    recorder: TaskRecorder,
    audit_context: AuditContext,
    limits: CollectorEvidenceLimitsV1,
) -> tuple[dict[str, JsonValue], ...]:
    path = recorder.path
    if not isinstance(path, Path):
        raise CollectorEvidenceError("INVALID_STREAM_PATH", "task stream path is untrusted")
    expected = (
        recorder.blob_store.root / "tasks" / cast(str, audit_context.task_run_id) / "events.jsonl"
    )
    try:
        if path.resolve(strict=True) != expected.resolve(strict=True) or path.is_symlink():
            raise CollectorEvidenceError(
                "INVALID_STREAM_PATH", "task stream is outside its Collector run root"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except CollectorEvidenceError:
        raise
    except OSError as exc:
        raise CollectorEvidenceError(
            "TASK_STREAM_UNAVAILABLE", "task stream cannot be opened safely"
        ) from exc

    try:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectorEvidenceError(
                    "TASK_STREAM_BUSY", "task stream is being appended; retry at a stable boundary"
                ) from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CollectorEvidenceError(
                "INVALID_STREAM_PATH", "task stream must be a regular file"
            )
        if metadata.st_size < 1 or metadata.st_size > limits.max_stream_bytes:
            raise CollectorEvidenceError(
                "TASK_STREAM_SIZE_REJECTED", "task stream is empty or exceeds its byte bound"
            )
        data = bytearray()
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise CollectorEvidenceError(
                    "TASK_STREAM_TRUNCATED", "task stream changed during bounded read"
                )
            data.extend(chunk)
            remaining -= len(chunk)
        if os.fstat(descriptor).st_size != metadata.st_size:
            raise CollectorEvidenceError(
                "TASK_STREAM_CHANGED", "task stream changed during its evidence snapshot"
            )
    except CollectorEvidenceError:
        raise
    except OSError as exc:
        raise CollectorEvidenceError(
            "TASK_STREAM_READ_FAILED", "task stream could not be read safely"
        ) from exc
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)

    raw = bytes(data)
    if not raw.endswith(b"\n"):
        raise CollectorEvidenceError(
            "INCOMPLETE_TASK_STREAM", "task stream has an incomplete JSONL tail"
        )
    lines = raw[:-1].split(b"\n")
    if not lines or len(lines) > limits.max_events:
        raise CollectorEvidenceError(
            "EVENT_COUNT_REJECTED", "task stream event count exceeds its bound"
        )

    events: list[dict[str, JsonValue]] = []
    seen_ids: set[str] = set()
    for index, line in enumerate(lines, start=1):
        if not line or len(line) > limits.max_event_line_bytes:
            raise CollectorEvidenceError(
                "EVENT_LINE_SIZE_REJECTED", "task event line is empty or oversized"
            )
        try:
            decoded = json.loads(line)
            if type(decoded) is not dict:
                raise TypeError("event root is not exact object")
            event = cast(dict[str, JsonValue], decoded)
            validate_event_envelope(event)
            canonical = exact_canonical_json_text(event).encode("utf-8")
        except Exception as exc:
            raise CollectorEvidenceError(
                "INVALID_EVENT_ENVELOPE", "task event is not exact canonical Collector v1"
            ) from exc
        if canonical != line:
            raise CollectorEvidenceError(
                "NON_CANONICAL_EVENT", "task event bytes are not canonical Collector JSON"
            )
        if event["seq"] != index:
            raise CollectorEvidenceError(
                "EVENT_SEQUENCE_MISMATCH", "task stream sequence is not contiguous"
            )
        if (
            event["run_id"] != audit_context.run_id
            or event["task_run_id"] != audit_context.task_run_id
            or event["stream_id"] != audit_context.task_run_id
        ):
            raise CollectorEvidenceError(
                "EVENT_STREAM_BINDING_MISMATCH", "task event binds another run or task"
            )
        event_id = cast(str, event["event_id"])
        if event_id in seen_ids:
            raise CollectorEvidenceError("DUPLICATE_EVENT_ID", "task stream repeats an event ID")
        parent = event["caused_by_event_id"]
        if parent is not None and parent not in seen_ids:
            raise CollectorEvidenceError(
                "INVALID_CAUSAL_PARENT", "task event parent is not an earlier task event"
            )
        seen_ids.add(event_id)
        events.append(event)
    return tuple(events)


def _resolve_cutoff_events(
    *,
    events: tuple[dict[str, JsonValue], ...],
    audit_context: AuditContext,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    task_events = [event for event in events if event["event_type"] == "task_started"]
    if len(task_events) != 1 or task_events[0]["seq"] != 1:
        raise CollectorEvidenceError(
            "TASK_START_BINDING_REJECTED", "task stream needs one first task_started event"
        )
    task_payload = _payload(task_events[0])
    task_goal = task_payload.get("task_goal")
    if (
        task_payload.get("task_goal_status") != "resolved"
        or type(task_goal) is not str
        or not task_goal
        or len(task_goal) > 32768
    ):
        raise CollectorEvidenceError(
            "TASK_GOAL_UNAVAILABLE", "resolved bounded task goal is unavailable"
        )

    matching = [
        event
        for event in events
        if event["event_id"] == audit_context.parent_event_id
        and event["event_type"] == "step_started"
    ]
    if len(matching) != 1:
        raise CollectorEvidenceError(
            "CURRENT_STEP_EVENT_MISSING", "bound current step_started event is unavailable"
        )
    current = matching[0]
    payload = _payload(current)
    if payload.get("step_id") != audit_context.step_id:
        raise CollectorEvidenceError(
            "CURRENT_STEP_BINDING_MISMATCH", "step ID differs from the current audit context"
        )
    same_step = [
        event
        for event in events
        if event["event_type"] == "step_started"
        and _payload(event).get("step_id") == audit_context.step_id
    ]
    if len(same_step) != 1:
        raise CollectorEvidenceError(
            "CURRENT_STEP_BINDING_MISMATCH", "current step ID is not unique in the task stream"
        )
    return current, task_events[0]


def _bind_current_image(
    *,
    request: JsonValue,
    history_ir: HistoryIR,
    current_event: dict[str, JsonValue],
    recorder: TaskRecorder,
    limits: CollectorEvidenceLimitsV1,
) -> _CurrentImage:
    current_roots: list[JsonPath] = []
    for region in history_ir.regions:
        if region.kind is RegionKind.CURRENT_OBSERVATION:
            current_roots.extend(region.paths)
    return _bind_current_image_at_roots(
        request=request,
        current_roots=tuple(current_roots),
        current_event=current_event,
        recorder=recorder,
        limits=limits,
    )


def _no_history_current_roots(request: JsonValue, host_id: str) -> tuple[JsonPath, ...]:
    """Resolve only the two codec-admitted no-history current-image roots."""

    if type(request) is not dict or type(request.get("messages")) is not list:
        raise CollectorEvidenceError(
            "NO_HISTORY_REQUEST_SHAPE_REJECTED", "no-history request messages are unavailable"
        )
    messages = cast(list[JsonValue], request["messages"])
    if host_id == "mobileworld.qwen3vl.actor" and len(messages) == 2:
        return (("messages", 1, "content", 1),)
    if host_id == "mobileworld.mai-ui.actor" and len(messages) == 3:
        return (("messages", 2, "content", 0),)
    raise CollectorEvidenceError(
        "NO_HISTORY_REQUEST_SHAPE_REJECTED",
        "host/request does not match an exact codec-admitted no-history shape",
    )


def _bind_current_image_at_roots(
    *,
    request: JsonValue,
    current_roots: tuple[JsonPath, ...],
    current_event: dict[str, JsonValue],
    recorder: TaskRecorder,
    limits: CollectorEvidenceLimitsV1,
) -> _CurrentImage:
    found: dict[JsonPath, tuple[dict[str, JsonValue], str]] = {}
    for root_path in current_roots:
        try:
            root = _get_at_path(request, root_path)
        except Exception as exc:
            raise CollectorEvidenceError(
                "CURRENT_REGION_PATH_REJECTED", "current observation path cannot be resolved"
            ) from exc
        _find_image_blocks(root, root_path, found, limits=limits)
    if len(found) != 1:
        raise CollectorEvidenceError(
            "AMBIGUOUS_CURRENT_IMAGE",
            "current observation must contain exactly one supported image block",
        )
    image_path, (image_block, data_url) = next(iter(found.items()))
    image_bytes, media_type = _decode_data_image(data_url, limits=limits)

    payload = _payload(current_event)
    observation = payload.get("observation")
    if type(observation) is not dict:
        raise CollectorEvidenceError(
            "CURRENT_OBSERVATION_REJECTED", "step_started observation is not an exact object"
        )
    screenshot = observation.get("screenshot")
    if type(screenshot) is not dict:
        raise CollectorEvidenceError(
            "CURRENT_SCREENSHOT_REJECTED", "step_started screenshot metadata is unavailable"
        )
    pixel_ref = screenshot.get("pixel_blob")
    width = screenshot.get("width")
    height = screenshot.get("height")
    if (
        screenshot.get("representation") != "canonical_png_from_runtime_pixels"
        or type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
        or width > 32768
        or height > 32768
        or width * height > limits.max_image_pixels
        or type(pixel_ref) is not dict
    ):
        raise CollectorEvidenceError(
            "CURRENT_SCREENSHOT_REJECTED", "step screenshot metadata violates its bounds"
        )
    try:
        pixel_bytes = recorder.blob_store.read_bytes(cast(Any, pixel_ref))
    except Exception as exc:
        raise CollectorEvidenceError(
            "CURRENT_SCREENSHOT_BLOB_REJECTED", "step screenshot blob failed integrity checks"
        ) from exc
    if len(pixel_bytes) > limits.max_image_bytes:
        raise CollectorEvidenceError(
            "CURRENT_SCREENSHOT_BLOB_REJECTED", "step screenshot blob exceeds its byte bound"
        )
    try:
        actual_format, actual_size, request_mode, request_pixels = _bounded_image_pixels(
            image_bytes, limits=limits
        )
        collector_format, collector_size, collector_mode, collector_pixels = _bounded_image_pixels(
            pixel_bytes, limits=limits
        )
    except CollectorEvidenceError:
        raise
    except Exception as exc:
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_DECODE_REJECTED", "current request image is not a valid image"
        ) from exc
    expected_format = {
        EvidenceMediaType.PNG: "PNG",
        EvidenceMediaType.JPEG: "JPEG",
        EvidenceMediaType.WEBP: "WEBP",
    }[media_type]
    if (
        actual_format != expected_format
        or collector_format != "PNG"
        or actual_size != (width, height)
        or collector_size != (width, height)
    ):
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_DIMENSION_DRIFT",
            "request image header differs from Collector screenshot metadata",
        )
    if (
        request_mode != collector_mode
        or screenshot.get("mode") != collector_mode
        or request_pixels != collector_pixels
    ):
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_PIXEL_DRIFT",
            "actor request image pixels differ from the bound Collector screenshot",
        )
    digest = hashlib.sha256(image_bytes).hexdigest()
    if pixel_ref.get("digest") != hashlib.sha256(pixel_bytes).hexdigest():
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_HASH_DRIFT", "Collector screenshot differs from its content address"
        )
    return _CurrentImage(
        path=image_path,
        request_value_sha256=canonical_sha256(cast(JsonValue, image_block)),
        data_url=data_url,
        content_sha256=digest,
        media_type=media_type,
        width=width,
        height=height,
    )


def _bounded_image_pixels(
    data: bytes, *, limits: CollectorEvidenceLimitsV1
) -> tuple[str | None, tuple[int, int], str, bytes]:
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
        if (
            type(width) is not int
            or type(height) is not int
            or width < 1
            or height < 1
            or width > 32768
            or height > 32768
            or width * height > limits.max_image_pixels
        ):
            raise CollectorEvidenceError(
                "CURRENT_IMAGE_SIZE_REJECTED", "decoded image dimensions exceed their bound"
            )
        image_format = image.format
        image.load()
        return image_format, (width, height), image.mode, image.tobytes()


def _project_snapshot(
    *,
    events: tuple[dict[str, JsonValue], ...],
    current_event: dict[str, JsonValue],
    task_event: dict[str, JsonValue],
    audit_context: AuditContext,
    request_sha256: str,
    current_image: _CurrentImage,
    limits: CollectorEvidenceLimitsV1,
) -> CausalEvidenceSnapshotV1:
    cutoff_seq = cast(int, current_event["seq"])
    current_payload = _payload(current_event)
    observation = cast(dict[str, JsonValue], current_payload["observation"])
    payload_sha256 = _payload_sha256(current_payload)
    task_payload = _payload(task_event)

    screenshot_id = _evidence_id(current_event, "screen")
    evidence: list[EvidenceEntryV1] = [
        EvidenceEntryV1(
            evidence_id=screenshot_id,
            role=EvidenceRole.CURRENT_UI_SCREENSHOT,
            semantic_scope=EvidenceSemanticScope.CURRENT_STATE_ONLY,
            source_event_id=cast(str, current_event["event_id"]),
            source_event_type=SourceEventType.STEP_STARTED,
            source_event_seq=cutoff_seq,
            task_run_id=cast(str, audit_context.task_run_id),
            caused_by_event_id=None,
            wall_time=cast(str, current_event["wall_time"]),
            monotonic_ns=cast(int, current_event["monotonic_ns"]),
            payload_sha256=payload_sha256,
            projection=ImageEvidenceProjectionV1(
                content_sha256=current_image.content_sha256,
                request_value_sha256=current_image.request_value_sha256,
                media_type=current_image.media_type,
                width=current_image.width,
                height=current_image.height,
            ),
        )
    ]
    accessibility_ids: tuple[str, ...] = ()
    accessibility = observation.get("accessibility_tree")
    if accessibility is not None and not _is_artifact_placeholder(accessibility):
        accessibility_id = _evidence_id(current_event, "accessibility")
        evidence.append(
            _text_evidence(
                event=current_event,
                evidence_id=accessibility_id,
                role=EvidenceRole.CURRENT_ACCESSIBILITY,
                semantic_scope=EvidenceSemanticScope.ACCESSIBILITY_STATE_ONLY,
                projection=TextEvidenceProjectionV1.from_json(accessibility),
                task_run_id=cast(str, audit_context.task_run_id),
                current=True,
            )
        )
        accessibility_ids = (accessibility_id,)

    by_id = {cast(str, event["event_id"]): event for event in events}
    step_events = {
        cast(str, _payload(event)["step_id"]): event
        for event in events
        if event["event_type"] == "step_started" and type(_payload(event).get("step_id")) is str
    }
    for event in events:
        seq = cast(int, event["seq"])
        if seq >= cutoff_seq:
            continue
        event_type = event["event_type"]
        if event_type == "action_execution_started":
            _validate_action_event(event, by_id=by_id, step_events=step_events)
            payload = _payload(event)
            projection_value: JsonValue = {
                "action": payload.get("action"),
                "execution_kind": payload.get("execution_kind"),
            }
            evidence.append(
                _text_evidence(
                    event=event,
                    evidence_id=_evidence_id(event, "action"),
                    role=EvidenceRole.PRIOR_ACTION_ATTEMPT,
                    semantic_scope=EvidenceSemanticScope.PAST_EVENT_FACT,
                    projection=TextEvidenceProjectionV1.from_json(projection_value),
                    task_run_id=cast(str, audit_context.task_run_id),
                )
            )
        elif event_type in {
            "transition_completed",
            "transition_failed",
            "transition_not_executed",
        }:
            _validate_transition_event(event, by_id=by_id, step_events=step_events)
            evidence.extend(
                _transition_evidence(event, task_run_id=cast(str, audit_context.task_run_id))
            )
        if len(evidence) > limits.max_packet_evidence:
            raise CollectorEvidenceError(
                "EVIDENCE_COUNT_REJECTED", "projected evidence exceeds the R2.2 packet bound"
            )

    current = CurrentObservationV1(
        source_event_id=cast(str, current_event["event_id"]),
        source_event_seq=cutoff_seq,
        screenshot_evidence_id=screenshot_id,
        screenshot_content_sha256=current_image.content_sha256,
        actor_request_image_path=current_image.path,
        actor_request_image_value_sha256=current_image.request_value_sha256,
        media_type=current_image.media_type,
        width=current_image.width,
        height=current_image.height,
        accessibility_evidence_ids=accessibility_ids,
    )
    return CausalEvidenceSnapshotV1(
        cutoff=EvidenceCutoffV1(
            run_id=audit_context.run_id,
            task_run_id=cast(str, audit_context.task_run_id),
            step_id=cast(str, audit_context.step_id),
            current_observation_event_id=cast(str, current_event["event_id"]),
            cutoff_event_seq=cutoff_seq,
            actor_request_sha256=request_sha256,
        ),
        task=TaskInstructionDataV1.create(
            source_event_id=cast(str, task_event["event_id"]),
            source_event_seq=cast(int, task_event["seq"]),
            exact_text=cast(str, task_payload["task_goal"]),
        ),
        current_observation=current,
        evidence_index=tuple(evidence),
        replacement_facts=(),
        input_exclusions=EvidenceInputExclusionsV1(),
    )


def _transition_evidence(
    event: dict[str, JsonValue], *, task_run_id: str
) -> tuple[EvidenceEntryV1, ...]:
    event_type = cast(str, event["event_type"])
    payload = _payload(event)
    if event_type == "transition_completed":
        status: JsonValue = {
            "status": "completed",
            "duration_ns": payload.get("duration_ns"),
        }
    elif event_type == "transition_failed":
        status = {
            "status": "failed",
            "duration_ns": payload.get("duration_ns"),
            "exception": payload.get("exception"),
        }
    else:
        status = {"status": "not_executed", "reason": payload.get("reason")}
    result: list[EvidenceEntryV1] = [
        _text_evidence(
            event=event,
            evidence_id=_evidence_id(event, "transition"),
            role=EvidenceRole.PRIOR_TRANSITION_STATUS,
            semantic_scope=EvidenceSemanticScope.PAST_EVENT_FACT,
            projection=TextEvidenceProjectionV1.from_json(status),
            task_run_id=task_run_id,
        )
    ]
    if event_type == "transition_not_executed":
        return tuple(result)

    post = payload.get("post_observation")
    if post is not None and type(post) is not dict:
        raise CollectorEvidenceError(
            "INVALID_PRIOR_POST_OBSERVATION", "prior post observation is not exact JSON object"
        )
    post_mapping = post if type(post) is dict else {}
    accessibility = post_mapping.get("accessibility_tree")
    if accessibility is not None and not _is_artifact_placeholder(accessibility):
        result.append(
            _text_evidence(
                event=event,
                evidence_id=_evidence_id(event, "post-ui"),
                role=EvidenceRole.PRIOR_POST_UI_STATE,
                semantic_scope=EvidenceSemanticScope.PAST_EVENT_FACT,
                projection=TextEvidenceProjectionV1.from_json(accessibility),
                task_run_id=task_run_id,
            )
        )

    raw_execution = (
        payload.get("execution_result")
        if event_type == "transition_completed"
        else payload.get("available_execution_result")
    )
    execution = raw_execution if type(raw_execution) is dict else None
    if raw_execution is not None:
        transport: JsonValue
        if execution is None:
            transport = raw_execution
        else:
            transport = {
                key: value
                for key, value in execution.items()
                if key not in {"agent_visible_tool_result", "ask_user_response"}
            }
        if not _is_artifact_placeholder(transport):
            result.append(
                _text_evidence(
                    event=event,
                    evidence_id=_evidence_id(event, "executor"),
                    role=EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
                    semantic_scope=EvidenceSemanticScope.EXECUTION_TRANSPORT_ONLY,
                    projection=TextEvidenceProjectionV1.from_json(transport),
                    task_run_id=task_run_id,
                )
            )

    if event_type == "transition_completed":
        tool = execution.get("agent_visible_tool_result") if execution is not None else None
        if tool is None:
            tool = post_mapping.get("tool_call")
        if tool is not None and not _is_artifact_placeholder(tool):
            result.append(
                _text_evidence(
                    event=event,
                    evidence_id=_evidence_id(event, "tool-result"),
                    role=EvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
                    semantic_scope=EvidenceSemanticScope.TOOL_OR_USER_CONTENT,
                    projection=TextEvidenceProjectionV1.from_json(tool),
                    task_run_id=task_run_id,
                )
            )
        user = execution.get("ask_user_response") if execution is not None else None
        if user is None:
            user = post_mapping.get("ask_user_response")
        if user is not None and not _is_artifact_placeholder(user):
            result.append(
                _text_evidence(
                    event=event,
                    evidence_id=_evidence_id(event, "user-response"),
                    role=EvidenceRole.USER_RESPONSE,
                    semantic_scope=EvidenceSemanticScope.TOOL_OR_USER_CONTENT,
                    projection=TextEvidenceProjectionV1.from_json(user),
                    task_run_id=task_run_id,
                )
            )
    return tuple(result)


def _validate_action_event(
    event: dict[str, JsonValue],
    *,
    by_id: dict[str, dict[str, JsonValue]],
    step_events: dict[str, dict[str, JsonValue]],
) -> None:
    payload = _payload(event)
    step_id = payload.get("step_id")
    decision_id = payload.get("decision_id")
    execution_id = payload.get("execution_id")
    if any(type(value) is not str for value in (step_id, decision_id, execution_id)):
        raise CollectorEvidenceError(
            "INVALID_PRIOR_ACTION", "prior action lacks exact step/decision/execution IDs"
        )
    step = step_events.get(cast(str, step_id))
    parent = by_id.get(cast(str, event["caused_by_event_id"]))
    if (
        step is None
        or cast(int, step["seq"]) >= cast(int, event["seq"])
        or parent is None
        or parent["event_type"] != "agent_decision"
    ):
        raise CollectorEvidenceError(
            "INVALID_PRIOR_ACTION", "prior action does not bind an earlier decision step"
        )
    parent_payload = _payload(parent)
    if parent_payload.get("step_id") != step_id or parent_payload.get("decision_id") != decision_id:
        raise CollectorEvidenceError(
            "INVALID_PRIOR_ACTION", "prior action and decision IDs disagree"
        )


def _validate_transition_event(
    event: dict[str, JsonValue],
    *,
    by_id: dict[str, dict[str, JsonValue]],
    step_events: dict[str, dict[str, JsonValue]],
) -> None:
    payload = _payload(event)
    step_id = payload.get("step_id")
    decision_id = payload.get("decision_id")
    if type(step_id) is not str or type(decision_id) is not str:
        raise CollectorEvidenceError(
            "INVALID_PRIOR_TRANSITION", "prior transition lacks exact step and decision IDs"
        )
    step = step_events.get(step_id)
    parent = by_id.get(cast(str, event["caused_by_event_id"]))
    if step is None or parent is None or cast(int, step["seq"]) >= cast(int, event["seq"]):
        raise CollectorEvidenceError(
            "INVALID_PRIOR_TRANSITION", "prior transition does not bind an earlier step"
        )
    if payload.get("pre_observation_event_id") != step["event_id"]:
        raise CollectorEvidenceError(
            "INVALID_PRIOR_TRANSITION", "prior transition pre-observation differs"
        )
    if event["event_type"] == "transition_not_executed":
        if parent["event_type"] != "agent_decision":
            raise CollectorEvidenceError(
                "INVALID_PRIOR_TRANSITION", "not-executed transition must bind its decision"
            )
        parent_payload = _payload(parent)
        if parent_payload.get("decision_id") != decision_id:
            raise CollectorEvidenceError(
                "INVALID_PRIOR_TRANSITION", "not-executed decision ID differs"
            )
        return
    if (
        parent["event_type"] != "action_execution_started"
        or payload.get("action_execution_event_id") != parent["event_id"]
    ):
        raise CollectorEvidenceError(
            "INVALID_PRIOR_TRANSITION", "transition must bind its action execution"
        )
    parent_payload = _payload(parent)
    if (
        parent_payload.get("step_id") != step_id
        or parent_payload.get("decision_id") != decision_id
        or parent_payload.get("execution_id") != payload.get("execution_id")
    ):
        raise CollectorEvidenceError(
            "INVALID_PRIOR_TRANSITION", "transition execution IDs disagree"
        )


def _text_evidence(
    *,
    event: dict[str, JsonValue],
    evidence_id: str,
    role: EvidenceRole,
    semantic_scope: EvidenceSemanticScope,
    projection: TextEvidenceProjectionV1,
    task_run_id: str,
    current: bool = False,
) -> EvidenceEntryV1:
    event_type = SourceEventType(cast(str, event["event_type"]))
    return EvidenceEntryV1(
        evidence_id=evidence_id,
        role=role,
        semantic_scope=semantic_scope,
        source_event_id=cast(str, event["event_id"]),
        source_event_type=event_type,
        source_event_seq=cast(int, event["seq"]),
        task_run_id=task_run_id,
        caused_by_event_id=None if current else cast(str, event["caused_by_event_id"]),
        wall_time=cast(str, event["wall_time"]),
        monotonic_ns=cast(int, event["monotonic_ns"]),
        payload_sha256=_payload_sha256(_payload(event)),
        projection=projection,
    )


def _payload(event: dict[str, JsonValue]) -> dict[str, JsonValue]:
    payload = event.get("payload")
    if type(payload) is not dict:
        raise CollectorEvidenceError(
            "INVALID_EVENT_PAYLOAD", "Collector event payload must be an exact object"
        )
    return payload


def _payload_sha256(payload: dict[str, JsonValue]) -> str:
    try:
        encoded = exact_canonical_json_text(payload).encode("utf-8")
    except Exception as exc:
        raise CollectorEvidenceError(
            "INVALID_EVENT_PAYLOAD", "Collector payload is not exact canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _evidence_id(event: dict[str, JsonValue], suffix: str) -> str:
    return f"r24e:{cast(str, event['event_id'])}:{suffix}"


def _decode_data_image(
    value: str, *, limits: CollectorEvidenceLimitsV1
) -> tuple[bytes, EvidenceMediaType]:
    if type(value) is not str or len(value) > limits.max_image_bytes * 4 // 3 + 128:
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_SIZE_REJECTED", "current image data URL exceeds its bound"
        )
    matched = _DATA_IMAGE.fullmatch(value)
    if matched is None:
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_URL_REJECTED", "current image must be a supported base64 data URL"
        )
    try:
        data = base64.b64decode(matched.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_URL_REJECTED", "current image base64 is malformed"
        ) from exc
    if not data or len(data) > limits.max_image_bytes:
        raise CollectorEvidenceError(
            "CURRENT_IMAGE_SIZE_REJECTED", "current image bytes are empty or oversized"
        )
    return data, EvidenceMediaType(matched.group(1))


def _find_image_blocks(
    value: JsonValue,
    path: JsonPath,
    found: dict[JsonPath, tuple[dict[str, JsonValue], str]],
    *,
    limits: CollectorEvidenceLimitsV1,
) -> None:
    pending: list[tuple[JsonValue, JsonPath, int]] = [(value, path, 0)]
    visits = 0
    while pending:
        item, item_path, depth = pending.pop()
        visits += 1
        if visits > limits.max_request_nodes or depth > limits.max_request_depth:
            raise CollectorEvidenceError(
                "CURRENT_REGION_GRAPH_REJECTED",
                "current request region exceeds its depth or node bound",
            )
        if type(item) is dict:
            image_url = item.get("image_url")
            if item.get("type") == "image_url" and type(image_url) is dict:
                url = image_url.get("url")
                if type(url) is str and _DATA_IMAGE.fullmatch(url) is not None:
                    found[item_path] = (item, url)
            pending.extend(
                (child, (*item_path, key), depth + 1)
                for key, child in reversed(tuple(item.items()))
            )
        elif type(item) is list:
            pending.extend(
                (child, (*item_path, index), depth + 1)
                for index, child in reversed(tuple(enumerate(item)))
            )


def _get_at_path(root: JsonValue, path: JsonPath) -> JsonValue:
    node = root
    for token in path:
        if type(token) is int and type(node) is list and token < len(node):
            node = node[token]
        elif type(token) is str and type(node) is dict and token in node:
            node = node[token]
        else:
            raise KeyError(path)
    return node


def _is_artifact_placeholder(value: JsonValue) -> bool:
    return type(value) is dict and _ARTIFACT_SNAPSHOT_KEY in value


__all__ = [
    "R23_STIMULUS_SCHEMA_VERSION",
    "CollectorEvidenceBundleV1",
    "CollectorEvidenceError",
    "CollectorEvidenceFactoryV1",
    "CollectorEvidenceLimitsV1",
    "CollectorRubricOnlyBundleV1",
    "build_collector_gpt56_evidence_factory",
    "rubric_evidence_snapshot_projection",
    "rubric_evidence_snapshot_sha256",
]
