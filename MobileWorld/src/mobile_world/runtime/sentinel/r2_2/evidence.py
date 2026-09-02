"""Deterministic construction of causally bounded R2.2 evidence packets."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    FrozenTextSlice,
    HistoryIR,
    HistoryRecord,
    JsonPath,
    JsonValue,
    RegionKind,
    RequestRegion,
    SourceSpan,
    SpanRole,
)
from mobile_world.runtime.sentinel.contracts import SentinelContext
from mobile_world.runtime.sentinel.r2_2.contracts import (
    CurrentObservationV1,
    EligibleHistoryTargetV1,
    EvidenceCutoffV1,
    EvidenceEntryV1,
    EvidenceInputExclusionsV1,
    EvidencePacketV1,
    ImageEvidenceProjectionV1,
    R22ContractError,
    ReplacementEvidenceRefV1,
    ReplacementFactV1,
    RuntimeTargetSpanRole,
    TaskInstructionDataV1,
    TemporalProvenanceStatus,
    TemporalProvenanceV1,
    TextEvidenceProjectionV1,
    evidence_packet_projection,
    exact_canonical_json_text,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class CausalEvidenceSnapshotV1:
    """Trusted host projection captured before one actor request is sent."""

    cutoff: EvidenceCutoffV1
    task: TaskInstructionDataV1
    current_observation: CurrentObservationV1
    evidence_index: tuple[EvidenceEntryV1, ...]
    replacement_facts: tuple[ReplacementFactV1, ...] = ()
    input_exclusions: EvidenceInputExclusionsV1 = EvidenceInputExclusionsV1()

    def __post_init__(self) -> None:
        if type(self.cutoff) is not EvidenceCutoffV1:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "cutoff must be exact EvidenceCutoffV1"
            )
        if type(self.task) is not TaskInstructionDataV1:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "task must be exact TaskInstructionDataV1"
            )
        if type(self.current_observation) is not CurrentObservationV1:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "current observation must use the exact R2.2 type"
            )
        if type(self.evidence_index) is not tuple or not self.evidence_index:
            raise R22ContractError(
                "EVIDENCE_INDEX_EMPTY", "snapshot must include current screenshot"
            )
        if any(type(item) is not EvidenceEntryV1 for item in self.evidence_index):
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "evidence index must contain exact R2.2 entries"
            )
        if type(self.replacement_facts) is not tuple or any(
            type(item) is not ReplacementFactV1 for item in self.replacement_facts
        ):
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "replacement facts must use exact R2.2 values"
            )
        if type(self.input_exclusions) is not EvidenceInputExclusionsV1:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "input exclusions must use exact R2.2 value"
            )


@runtime_checkable
class EvidenceSnapshotProvider(Protocol):
    """Injectable read-only evidence boundary; it never writes Collector events."""

    def snapshot_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> CausalEvidenceSnapshotV1: ...


class StaticEvidenceSnapshotProvider:
    """CPU-test provider for a preconstructed immutable snapshot."""

    def __init__(self, snapshot: CausalEvidenceSnapshotV1) -> None:
        if type(snapshot) is not CausalEvidenceSnapshotV1:
            raise TypeError("snapshot must be exact CausalEvidenceSnapshotV1")
        self._snapshot = _snapshot_causal_evidence(snapshot)

    def snapshot_for_call(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> CausalEvidenceSnapshotV1:
        del request, context, history_ir
        return _snapshot_causal_evidence(self._snapshot)


def _snapshot_evidence_projection(
    value: TextEvidenceProjectionV1 | ImageEvidenceProjectionV1,
) -> TextEvidenceProjectionV1 | ImageEvidenceProjectionV1:
    if type(value) is TextEvidenceProjectionV1:
        return TextEvidenceProjectionV1(
            projection_type=value.projection_type,
            exact_text=value.exact_text,
            text_sha256=value.text_sha256,
        )
    if type(value) is ImageEvidenceProjectionV1:
        return ImageEvidenceProjectionV1(
            content_sha256=value.content_sha256,
            request_value_sha256=value.request_value_sha256,
            media_type=value.media_type,
            width=value.width,
            height=value.height,
            projection_type=value.projection_type,
        )
    raise R22ContractError(
        "UNTRUSTED_RUNTIME_TYPE", "snapshot evidence projection type is untrusted"
    )


def _snapshot_causal_evidence(value: CausalEvidenceSnapshotV1) -> CausalEvidenceSnapshotV1:
    """Rebuild every host-owned node before it can enter a packet or hash."""

    if type(value) is not CausalEvidenceSnapshotV1:
        raise R22ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "causal evidence must use the exact snapshot type"
        )
    cutoff = EvidenceCutoffV1(
        run_id=value.cutoff.run_id,
        task_run_id=value.cutoff.task_run_id,
        step_id=value.cutoff.step_id,
        current_observation_event_id=value.cutoff.current_observation_event_id,
        cutoff_event_seq=value.cutoff.cutoff_event_seq,
        actor_request_sha256=value.cutoff.actor_request_sha256,
        kind=value.cutoff.kind,
    )
    task = TaskInstructionDataV1(
        source_event_id=value.task.source_event_id,
        source_event_seq=value.task.source_event_seq,
        exact_text=value.task.exact_text,
        text_sha256=value.task.text_sha256,
        role=value.task.role,
        source_event_type=value.task.source_event_type,
    )
    current = CurrentObservationV1(
        source_event_id=value.current_observation.source_event_id,
        source_event_seq=value.current_observation.source_event_seq,
        screenshot_evidence_id=value.current_observation.screenshot_evidence_id,
        screenshot_content_sha256=value.current_observation.screenshot_content_sha256,
        actor_request_image_path=tuple(value.current_observation.actor_request_image_path),
        actor_request_image_value_sha256=(
            value.current_observation.actor_request_image_value_sha256
        ),
        media_type=value.current_observation.media_type,
        width=value.current_observation.width,
        height=value.current_observation.height,
        accessibility_evidence_ids=tuple(value.current_observation.accessibility_evidence_ids),
        source_event_type=value.current_observation.source_event_type,
    )
    evidence = tuple(
        EvidenceEntryV1(
            evidence_id=item.evidence_id,
            role=item.role,
            semantic_scope=item.semantic_scope,
            source_event_id=item.source_event_id,
            source_event_type=item.source_event_type,
            source_event_seq=item.source_event_seq,
            task_run_id=item.task_run_id,
            caused_by_event_id=item.caused_by_event_id,
            wall_time=item.wall_time,
            monotonic_ns=item.monotonic_ns,
            payload_sha256=item.payload_sha256,
            projection=_snapshot_evidence_projection(item.projection),
            observed_by_cutoff=item.observed_by_cutoff,
        )
        for item in value.evidence_index
    )
    replacement_facts = tuple(
        ReplacementFactV1(
            replacement_fact_id=item.replacement_fact_id,
            target_id=item.target_id,
            exact_text=item.exact_text,
            text_sha256=item.text_sha256,
            evidence_refs=tuple(
                ReplacementEvidenceRefV1(
                    evidence_id=ref.evidence_id,
                    payload_sha256=ref.payload_sha256,
                )
                for ref in item.evidence_refs
            ),
            author=item.author,
            minimal_fact=item.minimal_fact,
            retroactive_actor_speech=item.retroactive_actor_speech,
            contains_action_or_tool_directive=item.contains_action_or_tool_directive,
        )
        for item in value.replacement_facts
    )
    exclusions = value.input_exclusions
    input_exclusions = EvidenceInputExclusionsV1(
        future_event_included=exclusions.future_event_included,
        target_actor_response_included=exclusions.target_actor_response_included,
        target_action_included=exclusions.target_action_included,
        target_result_or_post_state_included=(exclusions.target_result_or_post_state_included),
        task_outcome_included=exclusions.task_outcome_included,
        benchmark_checker_included=exclusions.benchmark_checker_included,
        replay_result_included=exclusions.replay_result_included,
        peer_decision_included=exclusions.peer_decision_included,
        host_history_used_as_evidence=exclusions.host_history_used_as_evidence,
        collector_raw_mutated=exclusions.collector_raw_mutated,
    )
    return CausalEvidenceSnapshotV1(
        cutoff=cutoff,
        task=task,
        current_observation=current,
        evidence_index=evidence,
        replacement_facts=replacement_facts,
        input_exclusions=input_exclusions,
    )


class EvidencePacketBuilder:
    """Build one closed packet from trusted evidence plus exact History IR spans."""

    def build(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        snapshot: CausalEvidenceSnapshotV1,
    ) -> EvidencePacketV1:
        if type(context) is not SentinelContext:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "context must be exact SentinelContext"
            )
        if type(history_ir) is not HistoryIR:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "history_ir must be exact HistoryIR")
        if type(snapshot) is not CausalEvidenceSnapshotV1:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "snapshot must be exact CausalEvidenceSnapshotV1"
            )
        snapshot = _snapshot_causal_evidence(snapshot)
        request_sha256 = _exact_json_sha256(request)
        if request_sha256 != history_ir.raw_request_sha256:
            raise R22ContractError("HISTORY_IR_REQUEST_DRIFT", "History IR binds another request")
        if request_sha256 != snapshot.cutoff.actor_request_sha256:
            raise R22ContractError("CUTOFF_REQUEST_DRIFT", "evidence cutoff binds another request")
        if context.host_id != history_ir.host_id:
            raise R22ContractError("HOST_BINDING_MISMATCH", "context and History IR hosts differ")
        _validate_task_against_request(request, history_ir, snapshot.task)
        _validate_current_observation_against_request(
            request, history_ir, snapshot.current_observation
        )
        targets = _build_targets(request, history_ir)
        replacement_facts = tuple(
            sorted(snapshot.replacement_facts, key=lambda item: item.replacement_fact_id)
        )
        evidence_index = tuple(
            sorted(
                snapshot.evidence_index, key=lambda item: (item.source_event_seq, item.evidence_id)
            )
        )
        seed: dict[str, JsonValue] = {
            "logical_call_id": context.logical_call_id,
            "host_id": context.host_id,
            "history_codec_id": history_ir.codec_id,
            "codec_contract_version": history_ir.codec_contract_version,
            "raw_request_sha256": request_sha256,
            "cutoff_event_seq": snapshot.cutoff.cutoff_event_seq,
            "task_text_sha256": snapshot.task.text_sha256,
            "current_image_sha256": snapshot.current_observation.actor_request_image_value_sha256,
            "target_ids": [item.target_id for item in targets],
            "evidence_ids": [item.evidence_id for item in evidence_index],
            "replacement_fact_ids": [item.replacement_fact_id for item in replacement_facts],
        }
        packet_id = f"r22pkt:{_canonical_sha256(seed)[:32]}"
        return EvidencePacketV1(
            packet_id=packet_id,
            logical_call_id=context.logical_call_id,
            host_id=context.host_id,
            history_codec_id=history_ir.codec_id,
            codec_contract_version=history_ir.codec_contract_version,
            raw_request_sha256=request_sha256,
            cutoff=snapshot.cutoff,
            task=snapshot.task,
            current_observation=snapshot.current_observation,
            targets=targets,
            evidence_index=evidence_index,
            replacement_facts=replacement_facts,
            input_exclusions=snapshot.input_exclusions,
        )

    def build_from_provider(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        provider: EvidenceSnapshotProvider,
    ) -> EvidencePacketV1:
        if not isinstance(provider, EvidenceSnapshotProvider):
            raise TypeError("provider must implement EvidenceSnapshotProvider")
        snapshot = provider.snapshot_for_call(
            request=request,
            context=context,
            history_ir=history_ir,
        )
        return self.build(
            request=request,
            context=context,
            history_ir=history_ir,
            snapshot=snapshot,
        )


def validate_evidence_packet_for_call(
    *,
    request: JsonValue,
    context: SentinelContext,
    history_ir: HistoryIR,
    packet: EvidencePacketV1,
) -> EvidencePacketV1:
    """Rebuild a packet from its source snapshot and bind it to this actor call."""

    if type(packet) is not EvidencePacketV1:
        raise R22ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "packet must use the exact R2.2 contract type"
        )
    snapshot = CausalEvidenceSnapshotV1(
        cutoff=packet.cutoff,
        task=packet.task,
        current_observation=packet.current_observation,
        evidence_index=packet.evidence_index,
        replacement_facts=packet.replacement_facts,
        input_exclusions=packet.input_exclusions,
    )
    rebuilt = EvidencePacketBuilder().build(
        request=request,
        context=context,
        history_ir=history_ir,
        snapshot=snapshot,
    )
    if exact_canonical_json_text(evidence_packet_projection(rebuilt)) != (
        exact_canonical_json_text(evidence_packet_projection(packet))
    ):
        raise R22ContractError(
            "EVIDENCE_PACKET_REBUILD_MISMATCH",
            "packet differs from the module-owned actor-call rebuild",
        )
    return rebuilt


def current_screenshot_request_value(request: JsonValue, packet: EvidencePacketV1) -> JsonValue:
    """Return a detached exact request image value after rechecking packet binding."""

    if type(packet) is not EvidencePacketV1:
        raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "packet must be exact EvidencePacketV1")
    if _exact_json_sha256(request) != packet.raw_request_sha256:
        raise R22ContractError("REQUEST_DRIFT", "request differs from evidence packet")
    value = _get_at_path(request, packet.current_observation.actor_request_image_path)
    if _canonical_sha256(value) != packet.current_observation.actor_request_image_value_sha256:
        raise R22ContractError("CURRENT_IMAGE_DRIFT", "current screenshot request value drifted")
    return cast(JsonValue, json.loads(exact_canonical_json_text(value)))


def current_screenshot_image_url(request: JsonValue, packet: EvidencePacketV1) -> str:
    """Resolve the one request-bound image URL without persisting it in the packet."""

    value = current_screenshot_request_value(request, packet)
    urls = _image_urls(value)
    if len(urls) != 1:
        raise R22ContractError("AMBIGUOUS_CURRENT_IMAGE", "image path must resolve one URL")
    return urls[0]


def _build_targets(
    request: JsonValue, history_ir: HistoryIR
) -> tuple[EligibleHistoryTargetV1, ...]:
    targets: list[EligibleHistoryTargetV1] = []
    locations: list[tuple[JsonPath, int, int]] = []
    for record in history_ir.records:
        if type(record) is not HistoryRecord:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "History IR record type is untrusted")
        if type(record.editable_spans) is not tuple or type(record.protected_spans) is not tuple:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "record spans must be exact tuples")
        for span in record.editable_spans:
            if type(span) is not SourceSpan:
                raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "editable span type is untrusted")
            if type(span.span_role) is not SpanRole:
                raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "span role type is untrusted")
            if span.span_role is not SpanRole.EDITABLE_CLAIM:
                continue
            if span.claim_id is None:
                raise R22ContractError("TARGET_CLAIM_ID_MISSING", "editable claim has no stable ID")
            _validate_source_span(request, span)
            for protected in record.protected_spans:
                if type(protected) is not SourceSpan:
                    raise R22ContractError(
                        "UNTRUSTED_RUNTIME_TYPE", "protected span type is untrusted"
                    )
                if _overlaps(span, protected):
                    raise R22ContractError(
                        "TARGET_PROTECTED_OVERLAP", "eligible claim overlaps protected content"
                    )
            for path, start, end in locations:
                if path == span.container_path and span.char_start < end and start < span.char_end:
                    raise R22ContractError("AMBIGUOUS_TARGET_OVERLAP", "eligible targets overlap")
            locations.append((span.container_path, span.char_start, span.char_end))
            target_seed: dict[str, JsonValue] = {
                "record_id": record.record_id,
                "claim_id": span.claim_id,
                "source_request_sha256": history_ir.raw_request_sha256,
                "span_sha256": span.span_sha256,
            }
            targets.append(
                EligibleHistoryTargetV1(
                    target_id=f"target-{_canonical_sha256(target_seed)[:32]}",
                    record_id=record.record_id,
                    claim_id=span.claim_id,
                    source_request_sha256=history_ir.raw_request_sha256,
                    record_sha256=record.record_sha256,
                    container_path=span.container_path,
                    char_start=span.char_start,
                    char_end=span.char_end,
                    utf8_byte_start=span.utf8_byte_start,
                    utf8_byte_end=span.utf8_byte_end,
                    exact_text=span.exact_text,
                    span_sha256=span.span_sha256,
                    span_role=RuntimeTargetSpanRole.EDITABLE_CLAIM,
                    source_provenance=_record_temporal_provenance(record),
                )
            )
    return tuple(
        sorted(
            targets,
            key=lambda item: (
                tuple(str(token) for token in item.container_path),
                item.char_start,
                item.target_id,
            ),
        )
    )


def _record_temporal_provenance(record: HistoryRecord) -> TemporalProvenanceV1:
    if type(record.provenance) is not dict:
        raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "record provenance must be exact dict")
    exact_canonical_json_text(record.provenance)
    provenance = cast(dict[str, JsonValue], record.provenance)
    event_id = provenance.get("source_event_id")
    event_seq = provenance.get("source_event_seq")
    wall_time = provenance.get("source_wall_time")
    monotonic_ns = provenance.get("source_monotonic_ns")
    if (
        type(event_id) is str
        and _EVENT_ID.fullmatch(event_id) is not None
        and type(event_seq) is int
        and event_seq >= 1
        and type(wall_time) is str
        and type(monotonic_ns) is int
        and monotonic_ns >= 0
    ):
        try:
            return TemporalProvenanceV1(
                status=TemporalProvenanceStatus.BOUND,
                source_event_id=event_id,
                source_event_seq=event_seq,
                source_wall_time=wall_time,
                source_monotonic_ns=monotonic_ns,
            )
        except R22ContractError:
            pass
    return TemporalProvenanceV1.unavailable()


def _validate_task_against_request(
    request: JsonValue, history_ir: HistoryIR, task: TaskInstructionDataV1
) -> None:
    task_values: list[str] = []
    task_candidates: set[str] = set()
    for region in history_ir.regions:
        if type(region) is not RequestRegion:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "History IR region type is untrusted")
        if type(region.kind) is not RegionKind:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "region kind type is untrusted")
        if region.kind is not RegionKind.TASK:
            continue
        if type(region.text_slices) is not tuple or type(region.paths) is not tuple:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "region paths/slices must be tuples")
        for text_slice in region.text_slices:
            if type(text_slice) is not FrozenTextSlice:
                raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "task text slice is untrusted")
            _validate_frozen_text_slice(request, text_slice)
            task_values.append(text_slice.exact_text)
            task_candidates.update(_task_instruction_candidates(text_slice.exact_text))
        for path in region.paths:
            _validate_path(path)
            value = _get_at_path(request, path)
            request_strings = _task_content_strings(value)
            task_values.extend(request_strings)
            task_candidates.update(item.strip() for item in request_strings if item.strip())
    occurrence_count = sum(item.count(task.exact_text) for item in task_values)
    if task.exact_text not in task_candidates or occurrence_count != 1:
        raise R22ContractError(
            "TASK_REQUEST_BINDING_MISMATCH",
            "task text must resolve as one complete instruction inside the declared task region",
        )


def _task_instruction_candidates(value: str) -> set[str]:
    """Return complete instruction candidates, never arbitrary substrings."""

    if type(value) is not str:
        raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "task region text must be exact str")
    candidates: set[str] = set()
    stripped = value.strip()
    if stripped:
        candidates.add(stripped)
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidates.add(line)
        separators = [index for token in (":", "：") if (index := line.find(token)) >= 0]
        if separators:
            suffix = line[min(separators) + 1 :].strip()
            if suffix:
                candidates.add(suffix)
    return candidates


def _task_content_strings(value: JsonValue) -> list[str]:
    """Extract task content while excluding transport role/type labels."""

    if type(value) is str:
        return [cast(str, value)]
    if type(value) is list:
        result: list[str] = []
        for item in cast(list[JsonValue], value):
            result.extend(_task_content_strings(item))
        return result
    if type(value) is dict:
        mapping = cast(dict[str, JsonValue], value)
        if "content" in mapping:
            return _task_content_strings(mapping["content"])
        result = []
        for key, item in mapping.items():
            if key in {"role", "type", "name"}:
                continue
            result.extend(_task_content_strings(item))
        return result
    return []


def _validate_current_observation_against_request(
    request: JsonValue, history_ir: HistoryIR, current: CurrentObservationV1
) -> None:
    current_paths: list[JsonPath] = []
    for region in history_ir.regions:
        if type(region) is not RequestRegion or type(region.kind) is not RegionKind:
            raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "History IR region is untrusted")
        if region.kind is RegionKind.CURRENT_OBSERVATION:
            if type(region.paths) is not tuple:
                raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "current paths must be tuple")
            current_paths.extend(region.paths)
    image_path = current.actor_request_image_path
    if not any(_path_is_within(image_path, owner) for owner in current_paths):
        raise R22ContractError(
            "CURRENT_IMAGE_OUTSIDE_REGION", "image is outside current observation"
        )
    image_value = _get_at_path(request, image_path)
    if _canonical_sha256(image_value) != current.actor_request_image_value_sha256:
        raise R22ContractError("CURRENT_IMAGE_HASH_MISMATCH", "request image hash differs")
    if len(_image_urls(image_value)) != 1:
        raise R22ContractError("AMBIGUOUS_CURRENT_IMAGE", "request image must resolve one URL")


def _validate_source_span(request: JsonValue, span: SourceSpan) -> None:
    _validate_path(span.container_path)
    container = _get_at_path(request, span.container_path)
    if type(container) is not str:
        raise R22ContractError("SPAN_CONTAINER_NOT_TEXT", "target container is not exact text")
    text = cast(str, container)
    if (
        type(span.char_start) is not int
        or type(span.char_end) is not int
        or span.char_start < 0
        or span.char_end <= span.char_start
        or span.char_end > len(text)
    ):
        raise R22ContractError("INVALID_SOURCE_SPAN", "target offsets are invalid")
    selected = text[span.char_start : span.char_end]
    if selected != span.exact_text or _sha256_text(selected) != span.span_sha256:
        raise R22ContractError("SOURCE_SPAN_DRIFT", "target source bytes drifted")
    if (
        len(text[: span.char_start].encode("utf-8")) != span.utf8_byte_start
        or len(text[: span.char_end].encode("utf-8")) != span.utf8_byte_end
    ):
        raise R22ContractError("UTF8_OFFSET_DRIFT", "target byte offsets drifted")


def _validate_frozen_text_slice(request: JsonValue, value: FrozenTextSlice) -> None:
    _validate_path(value.container_path)
    container = _get_at_path(request, value.container_path)
    if type(container) is not str:
        raise R22ContractError("SPAN_CONTAINER_NOT_TEXT", "task container is not text")
    text = cast(str, container)
    if (
        value.char_start < 0
        or value.char_end <= value.char_start
        or value.char_end > len(text)
        or text[value.char_start : value.char_end] != value.exact_text
        or _sha256_text(value.exact_text) != value.span_sha256
    ):
        raise R22ContractError("TASK_SPAN_DRIFT", "task source slice drifted")


def _validate_path(path: object) -> None:
    if type(path) is not tuple or not path:
        raise R22ContractError("INVALID_JSON_PATH", "path must be a non-empty exact tuple")
    for token in cast(tuple[object, ...], path):
        if type(token) is int and cast(int, token) >= 0:
            continue
        if type(token) is str and cast(str, token):
            continue
        raise R22ContractError("INVALID_JSON_PATH", "path has an invalid token")


def _get_at_path(root: JsonValue, path: JsonPath) -> JsonValue:
    _validate_path(path)
    node: JsonValue = root
    for token in path:
        if type(token) is int and type(node) is list:
            index = cast(int, token)
            if index >= len(node):
                raise R22ContractError("REQUEST_PATH_NOT_FOUND", "list path is out of bounds")
            node = node[index]
        elif type(token) is str and type(node) is dict and token in node:
            node = node[token]
        else:
            raise R22ContractError("REQUEST_PATH_NOT_FOUND", "request path cannot be resolved")
    return node


def _path_is_within(path: JsonPath, owner: JsonPath) -> bool:
    return len(path) >= len(owner) and path[: len(owner)] == owner


def _all_strings(value: JsonValue) -> list[str]:
    if type(value) is str:
        return [cast(str, value)]
    if type(value) is list:
        result: list[str] = []
        for item in cast(list[JsonValue], value):
            result.extend(_all_strings(item))
        return result
    if type(value) is dict:
        result = []
        for item in cast(dict[str, JsonValue], value).values():
            result.extend(_all_strings(item))
        return result
    return []


def _image_urls(value: JsonValue) -> list[str]:
    if type(value) is str:
        candidate = cast(str, value)
        if candidate.startswith(("data:image/", "https://", "http://")):
            return [candidate]
        return []
    if type(value) is list:
        result: list[str] = []
        for item in cast(list[JsonValue], value):
            result.extend(_image_urls(item))
        return result
    if type(value) is dict:
        result = []
        for item in cast(dict[str, JsonValue], value).values():
            result.extend(_image_urls(item))
        return result
    return []


def _overlaps(left: SourceSpan, right: SourceSpan) -> bool:
    return (
        left.container_path == right.container_path
        and left.char_start < right.char_end
        and right.char_start < left.char_end
    )


def _exact_json_sha256(value: object) -> str:
    return hashlib.sha256(exact_canonical_json_text(value).encode("utf-8")).hexdigest()


def _canonical_sha256(value: JsonValue) -> str:
    return hashlib.sha256(exact_canonical_json_text(value).encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CausalEvidenceSnapshotV1",
    "EvidencePacketBuilder",
    "EvidenceSnapshotProvider",
    "StaticEvidenceSnapshotProvider",
    "current_screenshot_image_url",
    "current_screenshot_request_value",
    "validate_evidence_packet_for_call",
]
