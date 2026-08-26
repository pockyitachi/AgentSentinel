"""Deterministic transformation core and independent pre-send validator."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
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
    HistoryCodecDeclaration,
    HistoryCodecResolver,
    HistoryFamily,
    HistoryIR,
    JsonPath,
    JsonValue,
    ListInsertionDiff,
    MappingKind,
    OperationKind,
    PlanOperation,
    PlanSetProfile,
    PortableContractError,
    ProviderDecision,
    RecordModality,
    RegionAvailability,
    RegionKind,
    RelatedContentKind,
    RelationshipKind,
    RenderDiff,
    RenderResult,
    RequestRegion,
    SourceMapping,
    SourceSpan,
    SpanRole,
    TransformationPlan,
    ValidationReceipt,
    canonical_sha256,
    copy_json,
    get_at_path,
    json_path_text,
    set_at_path,
    stable_id,
    text_sha256,
)

_MATERIAL_OPERATION_KINDS = {
    OperationKind.DROP,
    OperationKind.REPLACE,
    OperationKind.ARCHIVE,
}
_G1_OPERATION_KINDS = {OperationKind.DROP, OperationKind.REPLACE}
_PLAN_SET_ARMS = {
    PlanSetProfile.PORTABLE_CORE: (
        ArmKind.ORIGINAL,
        ArmKind.MASK,
        ArmKind.MASK_CORRECTION,
        ArmKind.ORACLE_CLEAN,
    ),
    PlanSetProfile.G1_STRICT_MHR: (
        ArmKind.ORIGINAL,
        ArmKind.MASK,
        ArmKind.MASK_CORRECTION,
        ArmKind.ORACLE_CLEAN,
        ArmKind.SHAM_BENIGN_EDIT,
    ),
    PlanSetProfile.G1_CLEAN_CONTROL: (
        ArmKind.ORIGINAL,
        ArmKind.SHAM_BENIGN_EDIT,
    ),
}


def _require_sha256(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise PortableContractError("INVALID_SHA256", f"{label} is not lowercase SHA-256")


def _is_nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _require_bool(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise PortableContractError("INVALID_BOOLEAN", f"{label} must be an exact boolean")


def _validate_json_path(path: object, label: str) -> None:
    if not isinstance(path, tuple) or not path:
        raise PortableContractError("INVALID_JSON_PATH", f"{label} must be a non-empty tuple")
    if any(
        not (
            (isinstance(token, str) and bool(token))
            or (isinstance(token, int) and not isinstance(token, bool) and token >= 0)
        )
        for token in path
    ):
        raise PortableContractError("INVALID_JSON_PATH", f"{label} has an invalid path token")


def _validate_stable_id(value: object, prefix: str, label: str) -> None:
    expected_prefix = f"{prefix}-"
    if (
        not isinstance(value, str)
        or not value.startswith(expected_prefix)
        or len(value) != len(expected_prefix) + 32
        or any(char not in "0123456789abcdef" for char in value[len(expected_prefix) :])
    ):
        raise PortableContractError("INVALID_STABLE_ID", f"{label} is not a stable ID")


def _validate_span_envelope(span: SourceSpan, *, target_record_id: str | None = None) -> None:
    _validate_json_path(span.container_path, "source span path")
    numeric = (
        span.char_start,
        span.char_end,
        span.utf8_byte_start,
        span.utf8_byte_end,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric):
        raise PortableContractError("INVALID_SOURCE_SPAN", "span offsets must be integers")
    if (
        span.char_start < 0
        or span.char_end <= span.char_start
        or span.utf8_byte_start < 0
        or span.utf8_byte_end <= span.utf8_byte_start
        or not _is_nonempty_text(span.exact_text)
    ):
        raise PortableContractError("INVALID_SOURCE_SPAN", "span envelope is empty or inverted")
    _require_sha256(span.span_sha256, "source span")
    if text_sha256(span.exact_text) != span.span_sha256:
        raise PortableContractError("SOURCE_SPAN_DRIFT", "span text and digest disagree")
    if span.utf8_byte_end - span.utf8_byte_start != len(span.exact_text.encode("utf-8")):
        raise PortableContractError(
            "UTF8_OFFSET_DRIFT", "span UTF-8 extent differs from its exact text"
        )
    if not isinstance(span.span_role, SpanRole):
        raise PortableContractError("INVALID_SPAN_ROLE", "span role is not recognized")
    editable = span.span_role in {SpanRole.EDITABLE_CLAIM, SpanRole.BENIGN_SHAM}
    if editable:
        _validate_stable_id(span.claim_id, "claim", "editable claim")
        if target_record_id is not None:
            expected_claim_id = stable_id(
                "claim",
                {
                    "record_id": target_record_id,
                    "container_path": list(span.container_path),
                    "char_start": span.char_start,
                    "char_end": span.char_end,
                    "span_sha256": span.span_sha256,
                },
            )
            if span.claim_id != expected_claim_id:
                raise PortableContractError("CLAIM_ID_DRIFT", "claim ID is not content-addressed")
    elif span.claim_id is not None:
        raise PortableContractError(
            "NON_EDITABLE_CLAIM_ID", "protected and record spans cannot carry claim IDs"
        )


def _validate_frozen_text_slice(text_slice: FrozenTextSlice) -> None:
    numeric = (
        text_slice.char_start,
        text_slice.char_end,
        text_slice.utf8_byte_start,
        text_slice.utf8_byte_end,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric):
        raise PortableContractError(
            "INVALID_REGION_TEXT_SLICE", "region text-slice offsets must be exact integers"
        )
    if (
        text_slice.char_start < 0
        or text_slice.char_end <= text_slice.char_start
        or text_slice.utf8_byte_start < 0
        or text_slice.utf8_byte_end <= text_slice.utf8_byte_start
        or not _is_nonempty_text(text_slice.exact_text)
    ):
        raise PortableContractError(
            "INVALID_REGION_TEXT_SLICE", "region text-slice coordinates are invalid"
        )
    _require_sha256(text_slice.span_sha256, "region text slice")


def _path_is_prefix(prefix: JsonPath, path: JsonPath) -> bool:
    return len(prefix) <= len(path) and path[: len(prefix)] == prefix


def _span_within_region(span: SourceSpan, region: RequestRegion) -> bool:
    if any(_path_is_prefix(path, span.container_path) for path in region.paths):
        return True
    return any(
        text_slice.container_path == span.container_path
        and text_slice.char_start <= span.char_start
        and span.char_end <= text_slice.char_end
        for text_slice in region.text_slices
    )


def _validate_correction_anchor_envelope(anchor: CorrectionAnchor) -> None:
    for label, path in (
        ("correction container", anchor.container_path),
        ("correction host context", anchor.host_context_path),
        ("correction role", anchor.role_path),
        ("correction reference", anchor.reference_path),
    ):
        _validate_json_path(path, label)
    if not _is_nonnegative_int(anchor.insert_index):
        raise PortableContractError(
            "CORRECTION_ANCHOR_OUT_OF_BOUNDS", "correction insertion index is invalid"
        )
    for value, label in (
        (anchor.source_container_sha256, "correction container"),
        (anchor.host_context_sha256, "correction context"),
        (anchor.reference_sha256, "correction reference"),
    ):
        _require_sha256(value, label)
    _validate_stable_id(anchor.owner_region_id, "region", "correction owner region")
    if (
        not isinstance(cast(object, anchor.placement), CorrectionPlacement)
        or not isinstance(cast(object, anchor.context_kind), CorrectionContextKind)
        or not isinstance(cast(object, anchor.visible_prefix), str)
        or "SENTINEL" not in anchor.visible_prefix
        or not isinstance(cast(object, anchor.visible_suffix), str)
    ):
        raise PortableContractError(
            "INVALID_CORRECTION_ANCHOR", "correction anchor envelope is malformed"
        )
    if anchor.expected_role != "user":
        raise PortableContractError(
            "CORRECTION_ANCHOR_ACTOR_OWNED",
            "correction context cannot be inserted into assistant/tool/actor speech",
        )


def _validate_correction_anchor(
    request: JsonValue,
    ir: HistoryIR,
    anchor: CorrectionAnchor,
    regions_by_id: dict[str, RequestRegion],
) -> None:
    _validate_correction_anchor_envelope(anchor)
    try:
        region = regions_by_id[anchor.owner_region_id]
    except KeyError as exc:
        raise PortableContractError(
            "UNKNOWN_CORRECTION_REGION", "correction anchor region is unknown"
        ) from exc
    if (
        region.kind is not RegionKind.CURRENT_OBSERVATION
        or region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    ):
        raise PortableContractError(
            "CORRECTION_ANCHOR_REGION_INVALID",
            "Sentinel context must be anchored in the current-observation region",
        )
    if not any(
        _path_is_prefix(path, anchor.container_path) or _path_is_prefix(anchor.container_path, path)
        for path in region.paths
    ) and not any(
        _path_is_prefix(text_slice.container_path, anchor.container_path)
        or _path_is_prefix(anchor.container_path, text_slice.container_path)
        for text_slice in region.text_slices
    ):
        raise PortableContractError(
            "CORRECTION_ANCHOR_REGION_MISMATCH", "anchor is outside its declared region"
        )
    context_path_valid = (
        _path_is_prefix(anchor.host_context_path, anchor.container_path)
        if anchor.context_kind is CorrectionContextKind.TEXT_CONTENT_BLOCK
        else _path_is_prefix(anchor.container_path, anchor.host_context_path)
    )
    if not context_path_valid:
        raise PortableContractError(
            "CORRECTION_CONTEXT_PATH_MISMATCH", "anchor is outside its host context"
        )
    if (
        canonical_sha256(get_at_path(request, anchor.host_context_path))
        != anchor.host_context_sha256
    ):
        raise PortableContractError("CORRECTION_CONTEXT_DRIFT", "host context hash changed")
    if not _path_is_prefix(anchor.host_context_path, anchor.role_path):
        raise PortableContractError(
            "CORRECTION_ROLE_PATH_MISMATCH", "role path is outside its host context"
        )
    actual_role = get_at_path(request, anchor.role_path)
    if actual_role != anchor.expected_role or anchor.expected_role != "user":
        raise PortableContractError(
            "CORRECTION_ANCHOR_ACTOR_OWNED",
            "correction context cannot be inserted into assistant/tool/actor speech",
        )
    if (
        len(anchor.reference_path) != len(anchor.container_path) + 1
        or anchor.reference_path[:-1] != anchor.container_path
        or not isinstance(anchor.reference_path[-1], int)
    ):
        raise PortableContractError(
            "CORRECTION_REFERENCE_PATH_MISMATCH",
            "correction reference must be one exact item in the insertion container",
        )
    reference_index = anchor.reference_path[-1]
    expected_insert_index = (
        reference_index if anchor.placement is CorrectionPlacement.BEFORE else reference_index + 1
    )
    if anchor.insert_index != expected_insert_index:
        raise PortableContractError(
            "CORRECTION_INSERTION_COORDINATE_MISMATCH",
            "correction insertion index does not match its reference and placement",
        )
    reference_value = get_at_path(request, anchor.reference_path)
    if canonical_sha256(reference_value) != anchor.reference_sha256:
        raise PortableContractError(
            "CORRECTION_REFERENCE_DRIFT", "correction reference value changed"
        )
    if not any(_path_is_prefix(path, anchor.reference_path) for path in region.paths) and not any(
        text_slice.container_path == anchor.reference_path for text_slice in region.text_slices
    ):
        raise PortableContractError(
            "CORRECTION_REFERENCE_REGION_MISMATCH",
            "correction reference is not part of the current observation",
        )
    for record in ir.records:
        if _path_is_prefix(record.source_span.container_path, anchor.container_path):
            raise PortableContractError(
                "CORRECTION_ANCHOR_ACTOR_OWNED",
                "correction container is nested inside a historical record",
            )


def validate_codec_capabilities(capabilities: CodecCapabilities) -> None:
    """Reject internally contradictory capability declarations."""

    if not _is_nonempty_text(capabilities.codec_id) or not _is_nonempty_text(
        capabilities.contract_version
    ):
        raise PortableContractError(
            "CODEC_IDENTITY_MISSING", "capability codec ID and contract version must be non-empty"
        )
    if (
        not isinstance(cast(object, capabilities.history_family), HistoryFamily)
        or not isinstance(cast(object, capabilities.level), CapabilityLevel)
        or not isinstance(cast(object, capabilities.scope), CodecScope)
        or any(
            not isinstance(cast(object, operation), OperationKind)
            for operation in capabilities.supported_operations
        )
        or any(not isinstance(cast(object, arm), ArmKind) for arm in capabilities.supported_arms)
    ):
        raise PortableContractError(
            "INVALID_CAPABILITY_ENUM", "capability enum values must use the canonical contract"
        )
    for label, value in (
        ("preserves_roles", capabilities.preserves_roles),
        ("preserves_order", capabilities.preserves_order),
        ("preserves_multimodal_blocks", capabilities.preserves_multimodal_blocks),
        ("preserves_tool_adjacency", capabilities.preserves_tool_adjacency),
        ("preserves_protocol_shell", capabilities.preserves_protocol_shell),
        ("live_ready", capabilities.live_ready),
        ("opaque_or_server_managed", capabilities.opaque_or_server_managed),
    ):
        _require_bool(value, f"codec capability {label}")
    operations = capabilities.supported_operations
    arms = capabilities.supported_arms
    if len(operations) != len(set(operations)) or len(arms) != len(set(arms)):
        raise PortableContractError("DUPLICATE_CAPABILITY", "capability lists must be unique")
    if ArmKind.ORIGINAL not in arms:
        raise PortableContractError(
            "ORIGINAL_CAPABILITY_MISSING", "every codec must support Original"
        )
    if capabilities.scope is CodecScope.FIXTURE_ONLY and capabilities.live_ready:
        raise PortableContractError("FIXTURE_CODEC_LIVE_FORBIDDEN", "fixture codec cannot be live")
    treatment_arms = set(arms) - {ArmKind.ORIGINAL}
    if capabilities.opaque_or_server_managed and (
        treatment_arms or any(item in _MATERIAL_OPERATION_KINDS for item in operations)
    ):
        raise PortableContractError(
            "OPAQUE_TREATMENT_CAPABILITY", "opaque history cannot claim treatment support"
        )
    if capabilities.level in {CapabilityLevel.AUDIT_ONLY, CapabilityLevel.ANNOTATION_ONLY}:
        if treatment_arms or any(item in _MATERIAL_OPERATION_KINDS for item in operations):
            raise PortableContractError(
                "CAPABILITY_LEVEL_MISMATCH", "audit/annotation codecs cannot claim G1 edits"
            )
        if capabilities.live_ready:
            raise PortableContractError(
                "CAPABILITY_LEVEL_MISMATCH", "audit/annotation codecs are not treatment-ready"
            )
    if (
        capabilities.level is CapabilityLevel.VALIDITY_TRANSFORMATION
        and OperationKind.ARCHIVE in operations
    ):
        raise PortableContractError(
            "CAPABILITY_LEVEL_MISMATCH",
            "ARCHIVE requires a full-transformation codec",
        )
    requirements = {
        ArmKind.MASK: OperationKind.DROP,
        ArmKind.ORACLE_CLEAN: OperationKind.DROP,
        ArmKind.SHAM_BENIGN_EDIT: OperationKind.DROP,
        ArmKind.MASK_CORRECTION: OperationKind.REPLACE,
    }
    for arm, operation in requirements.items():
        if arm in arms and operation not in operations:
            raise PortableContractError(
                "CAPABILITY_ARM_OPERATION_MISMATCH",
                f"{arm.value} requires {operation.value}",
            )
    if capabilities.live_ready and not all(
        (
            capabilities.preserves_roles,
            capabilities.preserves_order,
            capabilities.preserves_multimodal_blocks,
            capabilities.preserves_tool_adjacency,
            capabilities.preserves_protocol_shell,
        )
    ):
        raise PortableContractError(
            "LIVE_PRESERVATION_INCOMPLETE", "live codec must preserve every host invariant"
        )


def validate_history_ir(request: JsonValue, ir: HistoryIR) -> None:
    """Validate the IR against source bytes without inferring any semantic verdict."""

    if not all(
        _is_nonempty_text(value) for value in (ir.host_id, ir.codec_id, ir.codec_contract_version)
    ):
        raise PortableContractError("IR_IDENTITY_MISSING", "IR identity fields must be non-empty")
    if not isinstance(cast(object, ir.history_family), HistoryFamily):
        raise PortableContractError("INVALID_HISTORY_FAMILY", "IR history family is invalid")
    if any(not isinstance(warning, str) for warning in ir.warnings):
        raise PortableContractError("INVALID_IR_WARNING", "IR warnings must be strings")
    if canonical_sha256(request) != ir.raw_request_sha256:
        raise PortableContractError("REQUEST_HASH_DRIFT", "IR does not bind the source request")
    if ir.capabilities.codec_id != ir.codec_id:
        raise PortableContractError("CAPABILITY_CODEC_MISMATCH", "capability binds another codec")
    if ir.capabilities.history_family != ir.history_family:
        raise PortableContractError("CAPABILITY_FAMILY_MISMATCH", "capability binds another family")
    if ir.capabilities.contract_version != ir.codec_contract_version:
        raise PortableContractError(
            "CAPABILITY_VERSION_MISMATCH", "capability binds another codec contract version"
        )
    validate_codec_capabilities(ir.capabilities)

    region_ids: set[str] = set()
    regions_by_id = {}
    for region in ir.regions:
        _validate_stable_id(region.region_id, "region", "region ID")
        if not isinstance(cast(object, region.kind), RegionKind) or not isinstance(
            cast(object, region.availability), RegionAvailability
        ):
            raise PortableContractError("INVALID_REGION_ENUM", "region enum value is invalid")
        _require_bool(region.preserve_exact, "region preserve_exact")
        if region.region_id in region_ids:
            raise PortableContractError("DUPLICATE_REGION_ID", "region IDs must be unique")
        region_ids.add(region.region_id)
        regions_by_id[region.region_id] = region
        for path in region.paths:
            _validate_json_path(path, "request region")
        projection: list[JsonValue] = [get_at_path(request, path) for path in region.paths]
        for text_slice in region.text_slices:
            _validate_json_path(text_slice.container_path, "request region text slice")
            _validate_frozen_text_slice(text_slice)
            text_slice.validate_against(request)
            projection.append(text_slice.exact_text)
        if region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT:
            if region.paths or region.text_slices or not _is_nonempty_text(region.absence_reason):
                raise PortableContractError(
                    "INVALID_ABSENT_REGION", "absent region needs only an explicit reason"
                )
        elif not region.paths and not region.text_slices:
            raise PortableContractError("EMPTY_REGION", "present region needs an exact locator")
        elif region.absence_reason is not None:
            raise PortableContractError(
                "INVALID_PRESENT_REGION", "present region cannot carry an absence reason"
            )
        if region.availability is RegionAvailability.COLOCATED and not region.text_slices:
            raise PortableContractError(
                "COLOCATED_REGION_SLICE_MISSING", "co-located region needs an exact text slice"
            )
        if canonical_sha256(projection) != region.source_sha256:
            raise PortableContractError("REGION_HASH_DRIFT", "region projection hash changed")
    required_kinds = {
        RegionKind.SYSTEM,
        RegionKind.TASK,
        RegionKind.HISTORY,
        RegionKind.CURRENT_OBSERVATION,
        RegionKind.TOOL_PROTOCOL,
    }
    missing_kinds = required_kinds - {region.kind for region in ir.regions}
    if missing_kinds:
        raise PortableContractError(
            "UNIDENTIFIED_REQUEST_REGION",
            "codec must identify or explicitly mark absent every semantic region",
            context={"missing": cast(JsonValue, sorted(item.value for item in missing_kinds))},
        )
    for kind in required_kinds - {RegionKind.HISTORY}:
        if sum(region.kind is kind for region in ir.regions) != 1:
            raise PortableContractError(
                "AMBIGUOUS_REQUEST_REGION", f"{kind.value} must be declared exactly once"
            )
    history_regions = [
        region
        for region in ir.regions
        if region.kind is RegionKind.HISTORY
        and region.availability is not RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    ]
    if not ir.capabilities.opaque_or_server_managed and not ir.records:
        raise PortableContractError(
            "EMPTY_HISTORY_IR", "non-opaque history must expose at least one canonical record"
        )
    if ir.capabilities.opaque_or_server_managed and not ir.records and history_regions:
        raise PortableContractError(
            "OPAQUE_HISTORY_REGION_PRESENT",
            "opaque history without records must mark every HISTORY region absent",
        )
    non_history_regions = [
        region
        for region in ir.regions
        if region.kind is not RegionKind.HISTORY
        and region.availability is not RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    ]
    for history_region in history_regions:
        for protected_region in non_history_regions:
            if protected_region.availability is RegionAvailability.COLOCATED:
                continue
            if any(
                _path_is_prefix(history_path, protected_path)
                or _path_is_prefix(protected_path, history_path)
                for history_path in history_region.paths
                for protected_path in protected_region.paths
            ) or any(
                _path_is_prefix(protected_path, history_slice.container_path)
                for history_slice in history_region.text_slices
                for protected_path in protected_region.paths
            ):
                raise PortableContractError(
                    "AMBIGUOUS_HISTORY_REGION",
                    "history cannot overlap a separately present non-history region",
                )

    record_ids = [record.record_id for record in ir.records]
    if len(record_ids) != len(set(record_ids)):
        raise PortableContractError("DUPLICATE_RECORD_ID", "record IDs must be unique")
    record_keys = [record.record_key for record in ir.records]
    if len(record_keys) != len(set(record_keys)):
        raise PortableContractError("DUPLICATE_RECORD_KEY", "record keys must be unique")
    coordinate_keys = [
        (
            record.coordinates.message_index,
            record.coordinates.content_block_index,
            record.coordinates.representation_record_index,
        )
        for record in ir.records
    ]
    if len(coordinate_keys) != len(set(coordinate_keys)):
        raise PortableContractError(
            "DUPLICATE_RECORD_COORDINATE", "representation record coordinates must be unique"
        )
    claim_ids: list[str] = []
    representation_groups: dict[JsonPath, list[int]] = defaultdict(list)
    for record in ir.records:
        if not all(
            _is_nonempty_text(value)
            for value in (
                record.record_key,
                record.record_class,
                record.role,
                record.author,
                record.exposure_time,
            )
        ):
            raise PortableContractError(
                "RECORD_IDENTITY_MISSING", "record identity and exposure fields must be non-empty"
            )
        if not isinstance(cast(object, record.modality), RecordModality):
            raise PortableContractError("INVALID_RECORD_MODALITY", "record modality is invalid")
        if record.write_time is not None and not isinstance(record.write_time, str):
            raise PortableContractError(
                "INVALID_RECORD_WRITE_TIME", "record write_time must be null or text"
            )
        if not isinstance(record.provenance, dict):
            raise PortableContractError(
                "INVALID_RECORD_PROVENANCE", "record provenance must be an object"
            )
        if not _is_positive_int(record.version):
            raise PortableContractError("INVALID_RECORD_VERSION", "record version must be >= 1")
        if record.region_id not in region_ids:
            raise PortableContractError(
                "UNKNOWN_RECORD_REGION", "record references an unknown region"
            )
        record_region = regions_by_id[record.region_id]
        if (
            record_region.kind is not RegionKind.HISTORY
            or record_region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
        ):
            raise PortableContractError(
                "RECORD_OUTSIDE_HISTORY", "record must bind a present HISTORY region"
            )
        _validate_span_envelope(record.source_span)
        record.source_span.validate_against(request)
        if (
            record.source_span.span_role is not SpanRole.RECORD_EXTENT
            or record.source_span.claim_id is not None
        ):
            raise PortableContractError(
                "RECORD_EXTENT_ROLE_INVALID",
                "record source span must be an unclaimed RECORD_EXTENT",
            )
        if not _span_within_region(record.source_span, record_region):
            raise PortableContractError(
                "RECORD_REGION_MISMATCH", "record source span is outside its HISTORY region"
            )
        if record.record_sha256 != record.source_span.span_sha256:
            raise PortableContractError("RECORD_HASH_DRIFT", "record hash differs from its bytes")
        expected_record_id = stable_id(
            "record",
            {
                "host_id": ir.host_id,
                "history_family": ir.history_family.value,
                "source_request_sha256": ir.raw_request_sha256,
                "container_path": list(record.source_span.container_path),
                "char_start": record.source_span.char_start,
                "char_end": record.source_span.char_end,
                "span_sha256": record.source_span.span_sha256,
                "record_class": record.record_class,
                "role": record.role,
                "author": record.author,
                "modality": record.modality.value,
            },
        )
        if record.record_id != expected_record_id:
            raise PortableContractError("RECORD_ID_DRIFT", "record ID is not content-addressed")
        if record.coordinates.request_path != record.source_span.container_path:
            raise PortableContractError(
                "RECORD_COORDINATE_MISMATCH", "record coordinates bind another request path"
            )
        coordinate_values = (
            record.coordinates.message_index,
            record.coordinates.content_block_index,
            record.coordinates.representation_record_index,
        )
        if (
            any(value is not None and not _is_nonnegative_int(value) for value in coordinate_values)
            or record.coordinates.representation_record_index is None
        ):
            raise PortableContractError(
                "INVALID_RECORD_COORDINATE", "record coordinates must be non-negative integers"
            )
        path = record.coordinates.request_path
        expected_message_index = (
            path[1]
            if len(path) >= 2
            and path[0] == "messages"
            and isinstance(path[1], int)
            and not isinstance(path[1], bool)
            else None
        )
        expected_content_index = (
            path[3]
            if len(path) >= 4
            and path[0] == "messages"
            and path[2] == "content"
            and isinstance(path[3], int)
            and not isinstance(path[3], bool)
            else None
        )
        if (
            record.coordinates.message_index != expected_message_index
            or record.coordinates.content_block_index != expected_content_index
        ):
            raise PortableContractError(
                "RECORD_COORDINATE_MISMATCH",
                "message/content coordinates do not match the exact request path",
            )
        representation_groups[path].append(record.coordinates.representation_record_index)
        if expected_message_index is not None:
            request_role = get_at_path(
                request,
                ("messages", expected_message_index, "role"),
            )
            if request_role != record.role:
                raise PortableContractError(
                    "RECORD_ROLE_DRIFT", "record role differs from its host message"
                )
        if record.source_span.container_path != tuple(record.source_span.container_path):
            raise PortableContractError("NON_CANONICAL_PATH", "record path is not canonical")
        spans = (*record.editable_spans, *record.protected_spans)
        if any(
            span.span_role not in {SpanRole.EDITABLE_CLAIM, SpanRole.BENIGN_SHAM}
            for span in record.editable_spans
        ) or any(
            span.span_role
            not in {
                SpanRole.PROTECTED_PROTOCOL,
                SpanRole.PROTECTED_EXTERNAL_RESULT,
                SpanRole.PROTECTED_MULTIMODAL,
                SpanRole.ELIGIBLE_PROTOCOL_SHELL,
            }
            for span in record.protected_spans
        ):
            raise PortableContractError(
                "RECORD_SPAN_CLASS_INVALID",
                "editable and protected span collections must retain their declared classes",
            )
        for span in spans:
            _validate_span_envelope(span, target_record_id=record.record_id)
            span.validate_against(request)
            if span.container_path != record.source_span.container_path:
                raise PortableContractError(
                    "RECORD_SPAN_PATH_MISMATCH", "record child span uses another container"
                )
            if not (
                record.source_span.char_start <= span.char_start
                and span.char_end <= record.source_span.char_end
            ):
                raise PortableContractError(
                    "RECORD_SPAN_OUTSIDE_RECORD", "record child span is outside its source record"
                )
            if span.claim_id is not None:
                claim_ids.append(span.claim_id)
                expected_claim_id = stable_id(
                    "claim",
                    {
                        "record_id": record.record_id,
                        "container_path": list(span.container_path),
                        "char_start": span.char_start,
                        "char_end": span.char_end,
                        "span_sha256": span.span_sha256,
                    },
                )
                if span.claim_id != expected_claim_id:
                    raise PortableContractError(
                        "CLAIM_ID_DRIFT", "claim ID is not content-addressed"
                    )
            if (
                span.span_role in {SpanRole.EDITABLE_CLAIM, SpanRole.BENIGN_SHAM}
                and span.claim_id is None
            ):
                raise PortableContractError(
                    "EDITABLE_CLAIM_ID_MISSING", "editable spans need stable claim IDs"
                )
        _reject_overlapping_spans(spans, code="OVERLAPPING_IR_SPANS")
        for anchor in record.correction_anchors:
            container = get_at_path(request, anchor.container_path)
            if not isinstance(container, list):
                raise PortableContractError(
                    "CORRECTION_ANCHOR_NOT_LIST", "correction anchor container must be an array"
                )
            if not 0 <= anchor.insert_index <= len(container):
                raise PortableContractError(
                    "CORRECTION_ANCHOR_OUT_OF_BOUNDS", "correction insertion index is invalid"
                )
            if canonical_sha256(container) != anchor.source_container_sha256:
                raise PortableContractError(
                    "CORRECTION_ANCHOR_DRIFT", "correction anchor container hash changed"
                )
            if "SENTINEL" not in anchor.visible_prefix:
                raise PortableContractError(
                    "CORRECTION_AUTHORSHIP_NOT_VISIBLE",
                    "correction context must visibly identify Sentinel",
                )
            _validate_correction_anchor(request, ir, anchor, regions_by_id)
        for related in record.related_content:
            if not isinstance(cast(object, related.kind), RelatedContentKind):
                raise PortableContractError(
                    "INVALID_RELATED_CONTENT_KIND", "related content kind is invalid"
                )
            _validate_json_path(related.path, "related content")
            value = get_at_path(request, related.path)
            if canonical_sha256(value) != related.value_sha256:
                raise PortableContractError(
                    "RELATED_CONTENT_HASH_DRIFT", "related multimodal/tool content changed"
                )
            if related.blob_sha256 is not None:
                _require_sha256(related.blob_sha256, "related content blob")
                if not isinstance(value, dict) or value.get("sha256") != related.blob_sha256:
                    raise PortableContractError(
                        "RELATED_BLOB_HASH_DRIFT", "related blob digest changed"
                    )

    for path, indexes in representation_groups.items():
        if sorted(indexes) != list(range(len(indexes))):
            raise PortableContractError(
                "NON_CANONICAL_REPRESENTATION_COORDINATE",
                "representation record indexes must be contiguous from zero per container",
                path=json_path_text(path),
            )
    if len(claim_ids) != len(set(claim_ids)):
        raise PortableContractError("DUPLICATE_CLAIM_ID", "claim IDs must be unique")
    known_records = set(record_ids)
    known_versions = {version.version_id for version in ir.source_versions}
    if len(known_versions) != len(ir.source_versions):
        raise PortableContractError("DUPLICATE_SOURCE_VERSION", "version IDs must be unique")
    for version in ir.source_versions:
        if not _is_nonempty_text(version.source_record_id):
            raise PortableContractError(
                "SOURCE_VERSION_SOURCE_MISSING", "source version record identity is required"
            )
        if version.write_time is not None and not isinstance(version.write_time, str):
            raise PortableContractError(
                "INVALID_SOURCE_VERSION_WRITE_TIME",
                "source version write_time must be null or text",
            )
        if not isinstance(version.provenance, dict):
            raise PortableContractError(
                "INVALID_SOURCE_VERSION_PROVENANCE",
                "source version provenance must be an object",
            )
        if not _is_positive_int(version.version):
            raise PortableContractError(
                "INVALID_SOURCE_VERSION", "source version number must be >= 1"
            )
        _require_bool(
            version.model_visible_in_current_request,
            "source version model_visible_in_current_request",
        )
        if version.model_visible_in_current_request is not False:
            raise PortableContractError(
                "SOURCE_VERSION_VISIBILITY_INVALID",
                "external source-version refs cannot claim current-request visibility",
            )
        _require_sha256(version.source_request_sha256, "source version request")
        _require_sha256(version.source_span_sha256, "source version span")
        if version.version_id != stable_id(
            "version",
            {
                "source_record_id": version.source_record_id,
                "version": version.version,
                "source_request_sha256": version.source_request_sha256,
                "source_span_sha256": version.source_span_sha256,
            },
        ):
            raise PortableContractError(
                "SOURCE_VERSION_ID_DRIFT", "source version ID is not content-addressed"
            )
    relationship_ids: set[str] = set()
    for record in ir.records:
        if set(record.source_version_ids) - (known_records | known_versions):
            raise PortableContractError(
                "UNKNOWN_SOURCE_VERSION", "record source-version lineage is unresolved"
            )
        lineage_targets = {
            relationship.target_record_id or relationship.target_version_id
            for relationship in record.relationships
            if relationship.kind
            in {RelationshipKind.SOURCE_VERSION, RelationshipKind.CURRENT_VERSION_OF}
        }
        lineage_targets.discard(None)
        if set(record.source_version_ids) != lineage_targets:
            raise PortableContractError(
                "SOURCE_VERSION_RELATION_MISMATCH",
                "source-version IDs and typed lineage relationships must match",
            )
        for relationship_index, relationship in enumerate(record.relationships):
            _validate_stable_id(relationship.relationship_id, "relationship", "relationship ID")
            if not isinstance(cast(object, relationship.kind), RelationshipKind):
                raise PortableContractError(
                    "INVALID_RELATIONSHIP_KIND", "relationship kind is invalid"
                )
            if relationship.relationship_id in relationship_ids:
                raise PortableContractError(
                    "DUPLICATE_RELATIONSHIP_ID", "relationship IDs must be unique"
                )
            relationship_ids.add(relationship.relationship_id)
            if relationship.source_record_id != record.record_id:
                raise PortableContractError(
                    "RELATIONSHIP_SOURCE_MISMATCH", "relationship is attached to another record"
                )
            expected_relationship_id = stable_id(
                "relationship",
                {
                    "source_record_id": record.record_id,
                    "index": relationship_index,
                    "kind": relationship.kind.value,
                    "target_record_id": relationship.target_record_id,
                    "target_path": (
                        None if relationship.target_path is None else list(relationship.target_path)
                    ),
                    "target_version_id": relationship.target_version_id,
                },
            )
            if relationship.relationship_id != expected_relationship_id:
                raise PortableContractError(
                    "RELATIONSHIP_ID_DRIFT", "relationship ID is not content-addressed"
                )
            if (
                sum(
                    target is not None
                    for target in (
                        relationship.target_record_id,
                        relationship.target_path,
                        relationship.target_version_id,
                    )
                )
                != 1
            ):
                raise PortableContractError(
                    "RELATIONSHIP_TARGET_INVALID",
                    "relationship needs exactly one record or request-path target",
                )
            target_kind = (
                "record"
                if relationship.target_record_id is not None
                else "version"
                if relationship.target_version_id is not None
                else "path"
            )
            allowed_target_kinds = {
                RelationshipKind.SOURCE_VERSION: {"record", "version"},
                RelationshipKind.CURRENT_VERSION_OF: {"record", "version"},
                RelationshipKind.TOOL_CALL_RESULT: {"record", "path"},
                RelationshipKind.ACTION_OBSERVATION: {"path"},
                RelationshipKind.SECTION_MEMBER: {"record"},
                RelationshipKind.ALIGNED_RECORD: {"record"},
            }[relationship.kind]
            if target_kind not in allowed_target_kinds:
                raise PortableContractError(
                    "RELATIONSHIP_TARGET_KIND_MISMATCH",
                    "relationship kind cannot use the supplied target type",
                )
            if relationship.target_record_id is not None:
                if relationship.target_record_id not in known_records:
                    raise PortableContractError(
                        "UNKNOWN_RELATIONSHIP_TARGET", "relationship target record is unknown"
                    )
            elif relationship.target_version_id is not None:
                if relationship.target_version_id not in known_versions:
                    raise PortableContractError(
                        "UNKNOWN_RELATIONSHIP_TARGET", "relationship target version is unknown"
                    )
            else:
                _validate_json_path(relationship.target_path, "relationship target")
                get_at_path(request, relationship.target_path or ())


def validate_plan(request: JsonValue, ir: HistoryIR, plan: TransformationPlan) -> None:
    """Validate a supplied curated plan; never invent targets or corrections."""

    _validate_plan_envelope(request, ir, plan)

    operation_ids = [operation.operation_id for operation in plan.operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise PortableContractError("DUPLICATE_OPERATION_ID", "operation IDs must be unique")
    canonical_operations = sorted(plan.operations, key=_operation_coordinate_key)
    if list(plan.operations) != canonical_operations:
        raise PortableContractError(
            "NON_CANONICAL_OPERATION_ORDER",
            "plan operations must use deterministic request-coordinate order",
        )

    for operation in plan.operations:
        _validate_operation(request, ir, plan.arm, operation)
    evidence_by_id: dict[str, object] = {}
    for operation in plan.operations:
        if len(operation.protocol_shell_for) != len(set(operation.protocol_shell_for)):
            raise PortableContractError(
                "DUPLICATE_SHELL_TARGET",
                "protocol-shell target operation IDs must be unique",
            )
        evidence_ids = [item.evidence_id for item in operation.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise PortableContractError(
                "DUPLICATE_EVIDENCE_ID", "operation evidence IDs must be unique"
            )
        for evidence in operation.evidence_refs:
            _validate_evidence_ref(evidence)
            existing = evidence_by_id.get(evidence.evidence_id)
            if existing is not None and existing != evidence:
                raise PortableContractError(
                    "EVIDENCE_ID_COLLISION", "one evidence ID resolves to different records"
                )
            evidence_by_id[evidence.evidence_id] = evidence
    target_operation_ids = {
        operation.operation_id for operation in plan.operations if not operation.protocol_shell_for
    }
    for operation in plan.operations:
        if operation.protocol_shell_for and not set(operation.protocol_shell_for).issubset(
            target_operation_ids
        ):
            raise PortableContractError(
                "ORPHAN_SHELL_REPAIR", "shell repair references an unknown target operation"
            )
    targets_by_id = {
        operation.operation_id: operation
        for operation in plan.operations
        if not operation.protocol_shell_for
    }
    for operation in plan.operations:
        for target_id in operation.protocol_shell_for:
            target = targets_by_id[target_id]
            if (
                operation.target_record_id != target.target_record_id
                or operation.target_span.container_path != target.target_span.container_path
            ):
                raise PortableContractError(
                    "CROSS_RECORD_SHELL_REPAIR",
                    "protocol-shell repair must belong to the same record and text container",
                )
    _reject_overlapping_spans(
        [operation.target_span for operation in plan.operations], code="OVERLAPPING_PLAN_EDITS"
    )
    _validate_protocol_shells_are_causally_empty(request, ir, plan.operations)

    regular = [operation for operation in plan.operations if not operation.protocol_shell_for]
    if plan.arm is ArmKind.SHAM_BENIGN_EDIT and len(regular) != 1:
        raise PortableContractError(
            "SHAM_TARGET_COUNT_INVALID",
            "Sham must delete exactly one complete benign history span",
        )
    if plan.arm in {ArmKind.MASK, ArmKind.ORACLE_CLEAN, ArmKind.SHAM_BENIGN_EDIT}:
        if any(operation.kind is not OperationKind.DROP for operation in regular):
            raise PortableContractError(
                "ARM_OPERATION_MISMATCH", "arm accepts only DROP operations"
            )
    elif plan.arm is ArmKind.MASK_CORRECTION:
        if not regular or any(operation.kind is not OperationKind.REPLACE for operation in regular):
            raise PortableContractError(
                "ARM_OPERATION_MISMATCH", "Mask + correction requires REPLACE for every target"
            )


def _validate_plan_operations_envelope(plan: TransformationPlan) -> None:
    """Validate schema-level operation semantics without resolving host records.

    Opaque/server-managed history still needs a schema-valid, curated request envelope
    before the core may emit a typed unsupported result.  Only source-record lookup,
    containment, and causal-empty shell checks are deferred to the supported path.
    """

    if not isinstance(plan.operations, tuple):
        raise PortableContractError(
            "NON_CANONICAL_PLAN_OPERATIONS", "plan operations must be an immutable tuple"
        )
    operation_ids = [operation.operation_id for operation in plan.operations]
    if any(not _is_nonempty_text(operation_id) for operation_id in operation_ids):
        raise PortableContractError("OPERATION_ID_MISSING", "operation IDs must be non-empty")
    if len(operation_ids) != len(set(operation_ids)):
        raise PortableContractError("DUPLICATE_OPERATION_ID", "operation IDs must be unique")
    if list(plan.operations) != sorted(plan.operations, key=_operation_coordinate_key):
        raise PortableContractError(
            "NON_CANONICAL_OPERATION_ORDER",
            "plan operations must use deterministic request-coordinate order",
        )

    evidence_by_id: dict[str, object] = {}
    for operation in plan.operations:
        if not isinstance(operation.kind, OperationKind):
            raise PortableContractError("INVALID_OPERATION_KIND", "operation kind is invalid")
        if operation.kind not in _G1_OPERATION_KINDS:
            raise PortableContractError(
                "NON_EXECUTABLE_G1_OPERATION",
                "KEEP/ARCHIVE/KEEP_UNCERTAIN are portable vocabulary, not G1 edits",
            )
        _validate_stable_id(operation.target_record_id, "record", "operation target record")
        _validate_span_envelope(operation.target_span, target_record_id=operation.target_record_id)
        if not isinstance(cast(object, operation.evidence_refs), tuple) or not isinstance(
            cast(object, operation.protocol_shell_for), tuple
        ):
            raise PortableContractError(
                "NON_CANONICAL_OPERATION_ENVELOPE",
                "evidence and shell bindings must be immutable tuples",
            )
        if len(operation.protocol_shell_for) != len(set(operation.protocol_shell_for)) or any(
            not _is_nonempty_text(target_id) for target_id in operation.protocol_shell_for
        ):
            raise PortableContractError(
                "DUPLICATE_SHELL_TARGET",
                "protocol-shell targets must be unique non-empty operation IDs",
            )
        evidence_ids = [item.evidence_id for item in operation.evidence_refs]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise PortableContractError(
                "DUPLICATE_EVIDENCE_ID", "operation evidence IDs must be unique"
            )
        for evidence in operation.evidence_refs:
            _validate_evidence_ref(evidence)
            existing = evidence_by_id.get(evidence.evidence_id)
            if existing is not None and existing != evidence:
                raise PortableContractError(
                    "EVIDENCE_ID_COLLISION", "one evidence ID resolves to different records"
                )
            evidence_by_id[evidence.evidence_id] = evidence

        if operation.kind is OperationKind.DROP:
            if (
                operation.replacement_text is not None
                or operation.replacement_author is not None
                or operation.correction_anchor is not None
                or operation.rendered_correction_context is not None
            ):
                raise PortableContractError(
                    "DROP_INSERTION_FORBIDDEN", "DROP cannot insert replacement context"
                )
        else:
            replacement_text = operation.replacement_text
            if not _is_nonempty_text(replacement_text):
                raise PortableContractError("EMPTY_CORRECTION", "correction text is missing")
            assert isinstance(replacement_text, str)
            if operation.replacement_author != "SENTINEL":
                raise PortableContractError(
                    "CORRECTION_ATTRIBUTION_INVALID", "correction author must be SENTINEL"
                )
            if not operation.evidence_refs:
                raise PortableContractError(
                    "CORRECTION_EVIDENCE_MISSING", "correction needs evidence provenance"
                )
            if operation.correction_anchor is None:
                raise PortableContractError(
                    "CORRECTION_ANCHOR_MISSING", "correction needs a host-safe anchor"
                )
            _validate_correction_anchor_envelope(operation.correction_anchor)
            expected_context = _expected_correction_context(
                operation.correction_anchor, replacement_text
            )
            if operation.rendered_correction_context != expected_context:
                raise PortableContractError(
                    "CORRECTION_CONTEXT_MISMATCH",
                    "rendered context is not the exact visible Sentinel correction",
                )

    target_operation_ids = {
        operation.operation_id for operation in plan.operations if not operation.protocol_shell_for
    }
    for operation in plan.operations:
        if operation.protocol_shell_for:
            if (
                operation.kind is not OperationKind.DROP
                or operation.target_span.span_role is not SpanRole.ELIGIBLE_PROTOCOL_SHELL
                or not set(operation.protocol_shell_for).issubset(target_operation_ids)
            ):
                raise PortableContractError(
                    "ORPHAN_SHELL_REPAIR", "protocol shell must bind a valid target deletion"
                )
        else:
            expected_role = (
                SpanRole.BENIGN_SHAM
                if plan.arm is ArmKind.SHAM_BENIGN_EDIT
                else SpanRole.EDITABLE_CLAIM
            )
            if operation.target_span.span_role is not expected_role:
                raise PortableContractError(
                    "TARGET_SPAN_ROLE_MISMATCH", "operation uses the wrong target span class"
                )

    regular = [operation for operation in plan.operations if not operation.protocol_shell_for]
    if plan.arm is ArmKind.SHAM_BENIGN_EDIT and len(regular) != 1:
        raise PortableContractError(
            "SHAM_TARGET_COUNT_INVALID", "Sham requires exactly one benign target"
        )
    if plan.arm in {ArmKind.MASK, ArmKind.ORACLE_CLEAN, ArmKind.SHAM_BENIGN_EDIT}:
        if not regular or any(operation.kind is not OperationKind.DROP for operation in regular):
            raise PortableContractError(
                "ARM_OPERATION_MISMATCH", "arm accepts only DROP target operations"
            )
    elif plan.arm is ArmKind.MASK_CORRECTION:
        if not regular or any(operation.kind is not OperationKind.REPLACE for operation in regular):
            raise PortableContractError(
                "ARM_OPERATION_MISMATCH", "Mask + correction requires REPLACE targets"
            )


def _validate_plan_envelope(request: JsonValue, ir: HistoryIR, plan: TransformationPlan) -> None:
    """Validate provenance and requested-arm shape without resolving edit targets."""

    validate_history_ir(request, ir)
    if not _is_nonempty_text(plan.plan_id):
        raise PortableContractError("PLAN_ID_MISSING", "plan ID must be non-empty text")
    if not isinstance(cast(object, plan.history_family), HistoryFamily) or not isinstance(
        cast(object, plan.arm), ArmKind
    ):
        raise PortableContractError("INVALID_PLAN_ENUM", "plan family or arm is invalid")
    if not all(
        _is_nonempty_text(value)
        for value in (plan.host_id, plan.codec_id, plan.codec_contract_version)
    ):
        raise PortableContractError("PLAN_IDENTITY_MISSING", "plan identity fields are required")
    _require_sha256(plan.source_request_sha256, "plan source request")
    if plan.curated is not True or plan.deployment_prediction is not False:
        raise PortableContractError(
            "UNSAFE_PLAN_PROVENANCE",
            "G1 accepts only curated plans with deployment_prediction=false",
        )
    if plan.source_request_sha256 != canonical_sha256(request):
        raise PortableContractError("PLAN_REQUEST_MISMATCH", "plan binds another source request")
    if (
        plan.host_id,
        plan.history_family,
        plan.codec_id,
        plan.codec_contract_version,
    ) != (
        ir.host_id,
        ir.history_family,
        ir.codec_id,
        ir.codec_contract_version,
    ):
        raise PortableContractError("PLAN_CODEC_MISMATCH", "plan host/family/codec do not match IR")
    if plan.arm is ArmKind.ORIGINAL and plan.operations:
        raise PortableContractError("ORIGINAL_HAS_EDITS", "Original must contain zero operations")
    if plan.arm is not ArmKind.ORIGINAL and not plan.operations:
        raise PortableContractError("EMPTY_TREATMENT_PLAN", "treatment requires an exact operation")
    _validate_plan_operations_envelope(plan)


def _validate_evidence_ref(evidence: EvidenceRef) -> None:
    if not _is_nonempty_text(evidence.evidence_id) or not _is_nonempty_text(evidence.role):
        raise PortableContractError(
            "INVALID_EVIDENCE_IDENTITY", "evidence identity and role must be non-empty text"
        )
    if evidence.event_seq is not None and not _is_nonnegative_int(evidence.event_seq):
        raise PortableContractError(
            "INVALID_EVIDENCE_EVENT_SEQ",
            "evidence event_seq must be null or a non-negative integer",
        )
    _require_sha256(evidence.sha256, "correction evidence")


def _path_coordinate_key(path: JsonPath) -> tuple[tuple[int, str | int], ...]:
    """Keep numeric request coordinates numeric rather than lexicographic."""

    return tuple((0, token) if isinstance(token, str) else (1, token) for token in path)


def _operation_coordinate_key(operation: PlanOperation) -> tuple[object, ...]:
    span = operation.target_span
    return (
        _path_coordinate_key(span.container_path),
        span.char_start,
        span.char_end,
        span.span_sha256,
        operation.target_record_id,
        operation.operation_id,
    )


def _validate_protocol_shells_are_causally_empty(
    request: JsonValue,
    ir: HistoryIR,
    operations: tuple[PlanOperation, ...],
) -> None:
    """Allow shell removal only when the same plan empties its semantic record.

    Linkage, record identity, and adjacency are not enough: deleting ``Step N:``
    while retaining part of the action would change host syntax beyond the curated
    target.  For every record that has a shell repair, independently replay all
    deletions in that record extent and require that only whitespace remains.
    Corrections are inserted in Sentinel-authored current-observation context, so
    their historical target spans are deletions for this check.
    """

    repaired_record_ids = {
        operation.target_record_id for operation in operations if operation.protocol_shell_for
    }
    for record_id in repaired_record_ids:
        record = ir.record_by_id(record_id)
        source = get_at_path(request, record.source_span.container_path)
        if not isinstance(source, str):
            raise PortableContractError(
                "SPAN_CONTAINER_NOT_TEXT", "protocol-shell record container is not text"
            )
        record_operations = sorted(
            (
                operation
                for operation in operations
                if operation.target_record_id == record_id
                and operation.target_span.container_path == record.source_span.container_path
            ),
            key=lambda item: item.target_span.char_start,
        )
        cursor = record.source_span.char_start
        remaining: list[str] = []
        for operation in record_operations:
            span = operation.target_span
            if (
                span.char_start < record.source_span.char_start
                or span.char_end > record.source_span.char_end
            ):
                raise PortableContractError(
                    "SHELL_REPAIR_OUTSIDE_RECORD",
                    "protocol-shell plan operation falls outside the semantic record",
                )
            remaining.append(source[cursor : span.char_start])
            cursor = span.char_end
        remaining.append(source[cursor : record.source_span.char_end])
        if "".join(remaining).strip():
            raise PortableContractError(
                "PROTOCOL_SHELL_NOT_CAUSALLY_EMPTY",
                "protocol shell may be removed only when the same plan empties its record",
                context={"record_id": record_id},
            )


def _validate_operation(
    request: JsonValue,
    ir: HistoryIR,
    arm: ArmKind,
    operation: PlanOperation,
) -> None:
    if operation.kind not in _G1_OPERATION_KINDS:
        raise PortableContractError(
            "NON_EXECUTABLE_G1_OPERATION",
            "KEEP/ARCHIVE/KEEP_UNCERTAIN are portable annotations, not G1 edits",
        )
    record = ir.record_by_id(operation.target_record_id)
    operation.target_span.validate_against(request)
    candidates = (*record.editable_spans, *record.protected_spans)
    if operation.target_span not in candidates:
        raise PortableContractError(
            "UNDECLARED_TARGET_SPAN", "operation span is not frozen in the target record"
        )

    if operation.protocol_shell_for:
        if operation.kind is not OperationKind.DROP:
            raise PortableContractError(
                "PROTOCOL_SHELL_REPLACEMENT_FORBIDDEN", "protocol-shell repair may only be deleted"
            )
        if operation.target_span.span_role is not SpanRole.ELIGIBLE_PROTOCOL_SHELL:
            raise PortableContractError(
                "PROTOCOL_SHELL_ROLE_MISMATCH", "shell repair must target a declared protocol span"
            )
        if not set(operation.protocol_shell_for):
            raise PortableContractError("ORPHAN_SHELL_REPAIR", "shell repair must bind a target")
    else:
        expected_role = (
            SpanRole.BENIGN_SHAM if arm is ArmKind.SHAM_BENIGN_EDIT else SpanRole.EDITABLE_CLAIM
        )
        if operation.target_span.span_role is not expected_role:
            raise PortableContractError(
                "TARGET_SPAN_ROLE_MISMATCH", "operation targets a protected or wrong-class span"
            )

    if operation.kind is OperationKind.DROP:
        if (
            operation.replacement_text is not None
            or operation.replacement_author is not None
            or operation.correction_anchor is not None
            or operation.rendered_correction_context is not None
        ):
            raise PortableContractError(
                "DROP_INSERTION_FORBIDDEN", "DROP is exact deletion and cannot insert a marker"
            )
    if operation.kind is OperationKind.REPLACE:
        if not operation.replacement_text:
            raise PortableContractError(
                "EMPTY_CORRECTION", "correction text must be supplied by the curated plan"
            )
        if operation.replacement_author != "SENTINEL":
            raise PortableContractError(
                "CORRECTION_ATTRIBUTION_INVALID",
                "correction must be Sentinel-authored context, not old actor speech",
            )
        if not operation.evidence_refs:
            raise PortableContractError(
                "CORRECTION_EVIDENCE_MISSING", "correction requires evidence provenance"
            )
        for evidence in operation.evidence_refs:
            if len(evidence.sha256) != 64 or any(
                char not in "0123456789abcdef" for char in evidence.sha256
            ):
                raise PortableContractError(
                    "INVALID_EVIDENCE_DIGEST", "correction evidence needs a lowercase SHA-256"
                )
        if operation.correction_anchor is None:
            raise PortableContractError(
                "CORRECTION_ANCHOR_MISSING",
                "correction must use a codec-declared Sentinel context anchor",
            )
        if operation.correction_anchor not in record.correction_anchors:
            raise PortableContractError(
                "UNDECLARED_CORRECTION_ANCHOR", "correction anchor is not frozen in the record"
            )
        expected_context = _expected_correction_context(
            operation.correction_anchor, operation.replacement_text
        )
        if operation.rendered_correction_context != expected_context:
            raise PortableContractError(
                "CORRECTION_CONTEXT_MISMATCH",
                "rendered context is not the exact visible Sentinel correction",
            )


def validate_capabilities(
    plans: tuple[TransformationPlan, ...],
    capabilities: CodecCapabilities,
    *,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
) -> None:
    """Preflight an entire paired unit before any provider response can exist."""

    validate_codec_capabilities(capabilities)
    if execution_mode is ExecutionMode.G1_SCIENTIFIC and failure_policy is not FailurePolicy.BLOCK:
        raise PortableContractError(
            "G1_FAIL_OPEN_FORBIDDEN", "G1 scientific execution must block unsupported arms"
        )
    unsupported = sorted(
        {plan.arm.value for plan in plans if plan.arm not in capabilities.supported_arms}
    )
    unsupported.extend(
        sorted(
            {
                f"OPERATION:{operation.kind.value}"
                for plan in plans
                for operation in plan.operations
                if operation.kind not in capabilities.supported_operations
            }
        )
    )
    if capabilities.opaque_or_server_managed:
        unsupported.append("OPAQUE_OR_SERVER_MANAGED_HISTORY")
    if unsupported:
        raise PortableContractError(
            "UNSUPPORTED_PLAN_SET",
            "codec cannot execute every planned arm",
            context={"unsupported": cast(JsonValue, unsupported)},
        )


def validate_plan_set(
    request: JsonValue,
    ir: HistoryIR,
    plans: tuple[TransformationPlan, ...],
    *,
    codec_registry: HistoryCodecResolver,
    codec_contract_version: str,
    plan_set_profile: PlanSetProfile,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
) -> str:
    """Freeze paired-arm invariants before any arm can reach a provider."""

    expected_codec = _resolve_history_codec(codec_registry, ir, codec_contract_version)
    _validate_codec_binding(request, ir, expected_codec)
    plan_set_sha256 = _validate_plan_set_structure(
        request,
        ir,
        plans,
        expected_codec,
        plan_set_profile,
        resolve_operations=True,
    )
    validate_capabilities(
        plans,
        expected_codec.capabilities,
        execution_mode=execution_mode,
        failure_policy=failure_policy,
    )
    planned_arms = {plan.arm for plan in plans}
    required_arms = set(_PLAN_SET_ARMS[plan_set_profile])
    supported_arms = set(expected_codec.capabilities.supported_arms)
    if planned_arms != required_arms:
        raise PortableContractError(
            "INCOMPLETE_PLAN_SET",
            "paired preflight must cover every arm in its frozen profile exactly once",
            context={
                "missing": cast(
                    JsonValue, sorted(arm.value for arm in required_arms - planned_arms)
                ),
                "extra": cast(JsonValue, sorted(arm.value for arm in planned_arms - required_arms)),
            },
        )
    if not required_arms.issubset(supported_arms):
        raise PortableContractError(
            "UNSUPPORTED_PLAN_PROFILE",
            "codec capability does not cover every arm in the frozen profile",
            context={
                "unsupported": cast(
                    JsonValue, sorted(arm.value for arm in required_arms - supported_arms)
                )
            },
        )
    return plan_set_sha256


def _resolve_history_codec(
    codec_registry: HistoryCodecResolver,
    ir: HistoryIR,
    codec_contract_version: str,
) -> HistoryCodecDeclaration:
    if not _is_nonempty_text(codec_contract_version):
        raise PortableContractError(
            "CODEC_CONTRACT_VERSION_MISSING", "codec contract version must be non-empty"
        )
    try:
        expected_codec = codec_registry.by_id(ir.codec_id, codec_contract_version)
    except (KeyError, TypeError, AttributeError) as exc:
        raise PortableContractError(
            "CODEC_REGISTRY_RESOLUTION_FAILED", "history codec is not registry-resolved"
        ) from exc
    if not isinstance(expected_codec, HistoryCodecDeclaration):
        raise PortableContractError(
            "CODEC_REGISTRY_RESOLUTION_FAILED", "registry returned an invalid declaration"
        )
    return expected_codec


def _validate_codec_binding(
    request: JsonValue,
    ir: HistoryIR,
    expected_codec: HistoryCodecDeclaration,
) -> None:
    if not expected_codec.contract_version:
        raise PortableContractError(
            "CODEC_CONTRACT_VERSION_MISSING", "codec declaration needs a contract version"
        )
    if (
        expected_codec.codec_id != ir.codec_id
        or expected_codec.history_family is not ir.history_family
        or expected_codec.contract_version != ir.codec_contract_version
    ):
        raise PortableContractError(
            "CODEC_DECLARATION_MISMATCH", "IR belongs to another codec declaration"
        )
    validate_codec_capabilities(expected_codec.capabilities)
    validate_codec_capabilities(ir.capabilities)
    if canonical_sha256(expected_codec.capabilities.to_dict()) != canonical_sha256(
        ir.capabilities.to_dict()
    ):
        raise PortableContractError(
            "CODEC_CAPABILITY_BINDING_MISMATCH", "IR capability differs from codec declaration"
        )
    extracted_ir = expected_codec.extract(request)
    validate_history_ir(request, extracted_ir)
    if canonical_sha256(extracted_ir.to_dict()) != canonical_sha256(ir.to_dict()):
        raise PortableContractError(
            "CODEC_EXTRACTION_BINDING_MISMATCH",
            "registry-resolved codec independently extracted a different History IR",
        )


def _validate_plan_set_structure(
    request: JsonValue,
    ir: HistoryIR,
    plans: tuple[TransformationPlan, ...],
    expected_codec: HistoryCodecDeclaration,
    plan_set_profile: PlanSetProfile,
    *,
    resolve_operations: bool,
) -> str:
    if not plans:
        raise PortableContractError("EMPTY_PLAN_SET", "paired plan set cannot be empty")
    if not isinstance(cast(object, plan_set_profile), PlanSetProfile):
        raise PortableContractError("UNKNOWN_PLAN_SET_PROFILE", "plan-set profile is invalid")
    if (
        plan_set_profile is PlanSetProfile.PORTABLE_CORE
        and expected_codec.capabilities.scope is not CodecScope.FIXTURE_ONLY
    ):
        raise PortableContractError(
            "PORTABLE_PROFILE_LIVE_FORBIDDEN",
            "PORTABLE_CORE is reserved for fixture-only conformance",
        )
    arms = [plan.arm for plan in plans]
    if len(arms) != len(set(arms)):
        raise PortableContractError("DUPLICATE_PLAN_ARM", "paired plan arms must be unique")
    expected_arm_order = _PLAN_SET_ARMS[plan_set_profile]
    if set(arms) != set(expected_arm_order):
        raise PortableContractError(
            "INCOMPLETE_PLAN_SET",
            "paired preflight must cover every arm in its frozen profile exactly once",
            context={
                "missing": cast(
                    JsonValue,
                    sorted(arm.value for arm in set(expected_arm_order) - set(arms)),
                ),
                "extra": cast(
                    JsonValue,
                    sorted(arm.value for arm in set(arms) - set(expected_arm_order)),
                ),
            },
        )
    if tuple(arms) != expected_arm_order:
        raise PortableContractError(
            "NON_CANONICAL_PLAN_SET_ORDER",
            "paired plans must follow the exact frozen profile order",
        )
    for plan in plans:
        if resolve_operations:
            validate_plan(request, ir, plan)
        else:
            _validate_plan_envelope(request, ir, plan)
    if ArmKind.ORIGINAL not in arms:
        raise PortableContractError("ORIGINAL_PLAN_MISSING", "paired plan set needs Original")

    def target_set(arm: ArmKind) -> tuple[tuple[str, JsonPath, int, int, str], ...] | None:
        matches = [plan for plan in plans if plan.arm is arm]
        if not matches:
            return None
        return tuple(
            (
                operation.target_record_id,
                operation.target_span.container_path,
                operation.target_span.char_start,
                operation.target_span.char_end,
                operation.target_span.span_sha256,
            )
            for operation in matches[0].operations
            if not operation.protocol_shell_for
        )

    mask_targets = target_set(ArmKind.MASK)
    correction_targets = target_set(ArmKind.MASK_CORRECTION)
    if (
        mask_targets is not None
        and correction_targets is not None
        and mask_targets != correction_targets
    ):
        raise PortableContractError(
            "PAIRED_TARGET_SET_MISMATCH", "Mask and correction must bind identical target spans"
        )
    oracle_targets = target_set(ArmKind.ORACLE_CLEAN)
    if (
        mask_targets is not None
        and oracle_targets is not None
        and not set(mask_targets).issubset(set(oracle_targets))
    ):
        raise PortableContractError(
            "ORACLE_NOT_TARGET_SUPERSET", "Oracle-clean must include every Mask target"
        )
    payload: JsonValue = {
        "codec_id": expected_codec.codec_id,
        "codec_contract_version": expected_codec.contract_version,
        "history_family": expected_codec.history_family.value,
        "capability_sha256": canonical_sha256(expected_codec.capabilities.to_dict()),
        "plan_set_profile": plan_set_profile.value,
        "required_arms": [arm.value for arm in expected_arm_order],
        "plans": [
            {"arm": plan.arm.value, "plan_sha256": canonical_sha256(plan.to_dict())}
            for plan in sorted(plans, key=lambda item: item.arm.value)
        ],
    }
    return canonical_sha256(payload)


def render_request(
    request: JsonValue,
    ir: HistoryIR,
    plan: TransformationPlan,
    *,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
) -> RenderResult:
    """Apply an already curated plan without mutating the caller-owned request."""

    source_snapshot = copy_json(request)
    source_hash = canonical_sha256(source_snapshot)
    _validate_plan_envelope(source_snapshot, ir, plan)
    if execution_mode is ExecutionMode.G1_SCIENTIFIC and failure_policy is not FailurePolicy.BLOCK:
        raise PortableContractError(
            "G1_FAIL_OPEN_FORBIDDEN", "G1 scientific execution cannot fail open"
        )
    unsupported = (
        plan.arm not in ir.capabilities.supported_arms
        or any(
            operation.kind not in ir.capabilities.supported_operations
            for operation in plan.operations
        )
        or ir.capabilities.opaque_or_server_managed
    )
    unsupported_reason = (
        "OPAQUE_OR_SERVER_MANAGED_HISTORY"
        if ir.capabilities.opaque_or_server_managed
        else "UNSUPPORTED_ARM_OR_OPERATION"
    )
    if unsupported:
        if (
            execution_mode is ExecutionMode.RUNTIME
            and failure_policy is FailurePolicy.FAIL_OPEN_ORIGINAL
        ):
            result = _original_result(
                source_snapshot,
                ir.capabilities,
                plan,
                execution_mode=execution_mode,
                failure_policy=failure_policy,
                warning="explicit runtime fail-open: unsupported history treatment",
                fallback_state=FallbackState.EXPLICIT_ORIGINAL,
                unsupported_reason=unsupported_reason,
            )
            if canonical_sha256(request) != source_hash:
                raise PortableContractError("CALLER_INPUT_MUTATED", "render mutated caller input")
            return result
        result = _original_result(
            source_snapshot,
            ir.capabilities,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
            warning=f"blocked before provider: {unsupported_reason}",
            fallback_state=FallbackState.BLOCKED_BEFORE_PROVIDER,
            unsupported_reason=unsupported_reason,
        )
        if canonical_sha256(request) != source_hash:
            raise PortableContractError("CALLER_INPUT_MUTATED", "render mutated caller input")
        return result
    validate_plan(source_snapshot, ir, plan)
    if plan.arm is ArmKind.ORIGINAL:
        result = _original_result(
            source_snapshot,
            ir.capabilities,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )
    else:
        result = _apply_operations(
            source_snapshot,
            ir.capabilities,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )
    if canonical_sha256(request) != source_hash:
        raise PortableContractError("CALLER_INPUT_MUTATED", "render mutated caller input")
    return result


def _original_result(
    request: JsonValue,
    capabilities: CodecCapabilities,
    plan: TransformationPlan,
    *,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
    warning: str | None = None,
    fallback_state: FallbackState = FallbackState.NOT_NEEDED,
    unsupported_reason: str | None = None,
) -> RenderResult:
    original = copy_json(request)
    rendered = copy_json(request)
    return RenderResult(
        original_request=original,
        rendered_request=rendered,
        source_request_sha256=canonical_sha256(original),
        rendered_request_sha256=canonical_sha256(rendered),
        plan_sha256=canonical_sha256(plan.to_dict()),
        capability_sha256=canonical_sha256(capabilities.to_dict()),
        requested_arm=plan.arm,
        effective_arm=(
            None if fallback_state is FallbackState.BLOCKED_BEFORE_PROVIDER else ArmKind.ORIGINAL
        ),
        execution_mode=execution_mode,
        failure_policy=failure_policy,
        diffs=(),
        list_insertions=(),
        source_mappings=(),
        warnings=() if warning is None else (warning,),
        fallback_state=fallback_state,
        count_as_treatment=False,
        unsupported_reason=unsupported_reason,
    )


def _apply_operations(
    request: JsonValue,
    capabilities: CodecCapabilities,
    plan: TransformationPlan,
    *,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
) -> RenderResult:
    rendered = copy_json(request)
    operations_by_path: dict[JsonPath, list[PlanOperation]] = defaultdict(list)
    for operation in plan.operations:
        operations_by_path[operation.target_span.container_path].append(operation)

    diffs: list[RenderDiff] = []
    mappings: list[SourceMapping] = []
    for path in sorted(operations_by_path, key=json_path_text):
        source_container = get_at_path(request, path)
        if not isinstance(source_container, str):
            raise PortableContractError(
                "SPAN_CONTAINER_NOT_TEXT", "edit container is not text", path=json_path_text(path)
            )
        final_text, path_diffs, path_mappings = _render_text_container(
            source_container, operations_by_path[path]
        )
        set_at_path(rendered, path, final_text)
        diffs.extend(path_diffs)
        mappings.extend(path_mappings)

    insertions = _apply_correction_insertions(rendered, request, plan.operations)

    return RenderResult(
        original_request=copy_json(request),
        rendered_request=rendered,
        source_request_sha256=canonical_sha256(request),
        rendered_request_sha256=canonical_sha256(rendered),
        plan_sha256=canonical_sha256(plan.to_dict()),
        capability_sha256=canonical_sha256(capabilities.to_dict()),
        requested_arm=plan.arm,
        effective_arm=plan.arm,
        execution_mode=execution_mode,
        failure_policy=failure_policy,
        diffs=tuple(diffs),
        list_insertions=tuple(insertions),
        source_mappings=tuple(mappings),
        warnings=(),
        fallback_state=FallbackState.NOT_NEEDED,
        count_as_treatment=True,
        unsupported_reason=None,
    )


def _render_text_container(
    source: str, operations: list[PlanOperation]
) -> tuple[str, list[RenderDiff], list[SourceMapping]]:
    ordered = sorted(operations, key=lambda item: item.target_span.char_start)
    cursor = 0
    rendered_cursor = 0
    chunks: list[str] = []
    diffs: list[RenderDiff] = []
    mappings: list[SourceMapping] = []
    for operation in ordered:
        span = operation.target_span
        if span.char_start < cursor:
            raise PortableContractError("OVERLAPPING_PLAN_EDITS", "plan spans overlap")
        copied = source[cursor : span.char_start]
        if copied:
            chunks.append(copied)
            mappings.append(
                SourceMapping(
                    container_path=span.container_path,
                    source_char_start=cursor,
                    source_char_end=span.char_start,
                    rendered_char_start=rendered_cursor,
                    rendered_char_end=rendered_cursor + len(copied),
                    kind=MappingKind.COPIED,
                    operation_id=None,
                )
            )
            rendered_cursor += len(copied)
        replacement_text = ""
        mappings.append(
            SourceMapping(
                container_path=span.container_path,
                source_char_start=span.char_start,
                source_char_end=span.char_end,
                rendered_char_start=rendered_cursor,
                rendered_char_end=rendered_cursor,
                kind=(
                    MappingKind.SYNTAX_REPAIR
                    if operation.protocol_shell_for
                    else MappingKind.DELETED
                ),
                operation_id=operation.operation_id,
            )
        )
        diffs.append(
            RenderDiff(
                operation_id=operation.operation_id,
                container_path=span.container_path,
                source_char_start=span.char_start,
                source_char_end=span.char_end,
                original_text=span.exact_text,
                rendered_text=replacement_text,
                original_sha256=span.span_sha256,
                rendered_sha256=text_sha256(replacement_text),
                mapping_kind=(
                    MappingKind.SYNTAX_REPAIR
                    if operation.protocol_shell_for
                    else MappingKind.DELETED
                ),
            )
        )
        cursor = span.char_end
    tail = source[cursor:]
    if tail:
        chunks.append(tail)
        mappings.append(
            SourceMapping(
                container_path=ordered[-1].target_span.container_path,
                source_char_start=cursor,
                source_char_end=len(source),
                rendered_char_start=rendered_cursor,
                rendered_char_end=rendered_cursor + len(tail),
                kind=MappingKind.COPIED,
                operation_id=None,
            )
        )
    return "".join(chunks), diffs, mappings


def _expected_correction_context(anchor: CorrectionAnchor, correction_text: str) -> JsonValue:
    visible_text = f"{anchor.visible_prefix}{correction_text}{anchor.visible_suffix}"
    if anchor.context_kind is CorrectionContextKind.TEXT_CONTENT_BLOCK:
        return {"type": "text", "text": visible_text}
    if anchor.context_kind is CorrectionContextKind.CHAT_MESSAGE:
        return {"role": "user", "content": visible_text}
    raise PortableContractError("UNKNOWN_CORRECTION_CONTEXT", "unsupported context kind")


def _apply_correction_insertions(
    rendered: JsonValue,
    source: JsonValue,
    operations: tuple[PlanOperation, ...],
) -> list[ListInsertionDiff]:
    pending = [operation for operation in operations if operation.correction_anchor is not None]
    keyed = [
        (
            json_path_text(operation.correction_anchor.container_path),
            operation.correction_anchor.insert_index,
            operation.operation_id,
            operation,
        )
        for operation in pending
        if operation.correction_anchor is not None
    ]
    if len({(item[0], item[1]) for item in keyed}) != len(keyed):
        raise PortableContractError(
            "AMBIGUOUS_CORRECTION_ANCHOR", "multiple corrections use the same insertion point"
        )
    offsets: dict[JsonPath, int] = defaultdict(int)
    insertions: list[ListInsertionDiff] = []
    for _, _, _, operation in sorted(keyed):
        anchor = operation.correction_anchor
        if anchor is None:
            continue
        source_container = get_at_path(source, anchor.container_path)
        rendered_container = get_at_path(rendered, anchor.container_path)
        if not isinstance(source_container, list) or not isinstance(rendered_container, list):
            raise PortableContractError(
                "CORRECTION_ANCHOR_NOT_LIST", "correction anchor container must be an array"
            )
        if canonical_sha256(source_container) != anchor.source_container_sha256:
            raise PortableContractError(
                "CORRECTION_ANCHOR_DRIFT", "correction anchor source list changed"
            )
        rendered_index = anchor.insert_index + offsets[anchor.container_path]
        inserted = copy_json(operation.rendered_correction_context)
        rendered_container.insert(rendered_index, inserted)
        offsets[anchor.container_path] += 1
        insertions.append(
            ListInsertionDiff(
                operation_id=operation.operation_id,
                container_path=anchor.container_path,
                source_index=anchor.insert_index,
                rendered_index=rendered_index,
                inserted_value=inserted,
                inserted_value_sha256=canonical_sha256(inserted),
            )
        )
    return insertions


def validate_pre_send(
    source_request: JsonValue,
    ir: HistoryIR,
    plan: TransformationPlan,
    result: RenderResult,
    *,
    codec_registry: HistoryCodecResolver,
    codec_contract_version: str,
    paired_plans: tuple[TransformationPlan, ...],
    plan_set_profile: PlanSetProfile,
    execution_mode: ExecutionMode,
    failure_policy: FailurePolicy,
    intended_provider_codec_id: str | None = None,
    intended_provider_contract_version: str | None = None,
    intended_endpoint_revision: str | None = None,
    model_parameters: dict[str, JsonValue] | None = None,
) -> ValidationReceipt:
    """Independently reconstruct the allowed request and prove pre-send invariants."""

    checks: list[str] = []
    _require_bool(result.count_as_treatment, "render result count_as_treatment")
    if execution_mode is ExecutionMode.G1_SCIENTIFIC and failure_policy is not FailurePolicy.BLOCK:
        raise PortableContractError(
            "G1_FAIL_OPEN_FORBIDDEN", "G1 scientific validation requires BLOCK policy"
        )
    expected_codec = _resolve_history_codec(codec_registry, ir, codec_contract_version)
    _validate_codec_binding(source_request, ir, expected_codec)
    if result.fallback_state is FallbackState.NOT_NEEDED:
        plan_set_sha256 = validate_plan_set(
            source_request,
            ir,
            paired_plans,
            codec_registry=codec_registry,
            codec_contract_version=codec_contract_version,
            plan_set_profile=plan_set_profile,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )
    else:
        plan_set_sha256 = _validate_plan_set_structure(
            source_request,
            ir,
            paired_plans,
            expected_codec,
            plan_set_profile,
            resolve_operations=False,
        )
    matching_plans = [item for item in paired_plans if item.to_dict() == plan.to_dict()]
    if len(matching_plans) != 1:
        raise PortableContractError(
            "PLAN_NOT_IN_PAIRED_SET", "validated plan must occur exactly once in the paired set"
        )
    checks.extend(
        (
            "source_ir_plan_binding",
            "codec_declaration_binding",
            "paired_plan_set_recomputed",
        )
    )
    source_hash = canonical_sha256(source_request)
    expected_result = render_request(
        source_request,
        ir,
        plan,
        execution_mode=execution_mode,
        failure_policy=failure_policy,
    )
    if canonical_sha256(result.to_dict()) != canonical_sha256(expected_result.to_dict()):
        raise PortableContractError(
            "RENDER_RECEIPT_MISMATCH",
            "rendered request, diff, insertion, mapping, or hash receipt is non-canonical",
        )
    expected_capability_sha256 = canonical_sha256(expected_codec.capabilities.to_dict())
    if result.capability_sha256 != expected_capability_sha256:
        raise PortableContractError(
            "RENDER_CAPABILITY_MISMATCH",
            "render result does not bind the registry-resolved capability",
        )
    checks.extend(("canonical_render_receipt", "untouched_request_hash"))
    _validate_non_history_preservation(source_request, result.rendered_request, ir, plan)
    checks.extend(("independent_target_only_diff", "non_history_projection_equal"))

    restored = restore_original(result)
    if restored != source_request:
        raise PortableContractError(
            "NON_REVERSIBLE_MAPPING", "source mapping cannot restore request"
        )
    checks.append("reversible_source_mapping")
    if canonical_sha256(source_request) != source_hash:
        raise PortableContractError("CALLER_INPUT_MUTATED", "pre-send validation mutated input")
    checks.append("caller_input_immutable")

    normal_live = (
        result.fallback_state is FallbackState.NOT_NEEDED
        and expected_codec.capabilities.live_ready
        and expected_codec.capabilities.scope is CodecScope.LIVE
    )
    runtime_bypass = (
        result.fallback_state is FallbackState.EXPLICIT_ORIGINAL
        and execution_mode is ExecutionMode.RUNTIME
        and failure_policy is FailurePolicy.FAIL_OPEN_ORIGINAL
        and expected_codec.capabilities.live_ready
        and expected_codec.capabilities.scope is CodecScope.LIVE
    )
    invocation_allowed = normal_live or runtime_bypass
    if runtime_bypass:
        provider_decision = ProviderDecision.BYPASS_ORIGINAL
    elif normal_live:
        provider_decision = ProviderDecision.ALLOW
    else:
        provider_decision = ProviderDecision.BLOCK
    if not invocation_allowed:
        checks.append("provider_blocked_before_invocation")
    provider_metadata = (
        intended_provider_codec_id,
        intended_provider_contract_version,
        intended_endpoint_revision,
        model_parameters,
    )
    if any(item is not None for item in provider_metadata) and not all(
        item is not None for item in provider_metadata
    ):
        raise PortableContractError(
            "INCOMPLETE_PROVIDER_BINDING", "provider codec, endpoint, and parameters bind together"
        )
    if intended_provider_codec_id is not None and not _is_nonempty_text(intended_provider_codec_id):
        raise PortableContractError(
            "INVALID_PROVIDER_BINDING", "provider codec identity must be non-empty text"
        )
    if intended_provider_contract_version is not None and not _is_nonempty_text(
        intended_provider_contract_version
    ):
        raise PortableContractError(
            "INVALID_PROVIDER_BINDING", "provider contract version must be non-empty text"
        )
    if intended_endpoint_revision is not None and not _is_nonempty_text(intended_endpoint_revision):
        raise PortableContractError(
            "INVALID_PROVIDER_BINDING", "provider endpoint revision must be non-empty text"
        )
    if model_parameters is not None and not isinstance(model_parameters, dict):
        raise PortableContractError(
            "INVALID_PROVIDER_BINDING", "model parameters must be a canonical object"
        )
    parameters_sha = (
        None if model_parameters is None else canonical_sha256(cast(JsonValue, model_parameters))
    )
    if invocation_allowed and (
        intended_provider_codec_id is None
        or intended_endpoint_revision is None
        or parameters_sha is None
    ):
        raise PortableContractError(
            "PROVIDER_BINDING_MISSING",
            "sendable receipt needs paired-plan, provider, endpoint, and parameter bindings",
        )
    return ValidationReceipt(
        valid=True,
        provider_invocation_allowed=invocation_allowed,
        provider_decision=provider_decision,
        execution_mode=execution_mode,
        failure_policy=failure_policy,
        invocation_attempted=False,
        source_request_sha256=source_hash,
        rendered_request_sha256=result.rendered_request_sha256,
        plan_sha256=canonical_sha256(plan.to_dict()),
        plan_set_sha256=plan_set_sha256,
        plan_set_profile=plan_set_profile,
        capability_sha256=canonical_sha256(expected_codec.capabilities.to_dict()),
        history_codec_contract_version=expected_codec.contract_version,
        intended_provider_codec_id=intended_provider_codec_id,
        intended_provider_contract_version=intended_provider_contract_version,
        intended_endpoint_revision=intended_endpoint_revision,
        model_parameters_sha256=parameters_sha,
        checks=tuple(checks),
    )


def _validate_non_history_preservation(
    source: JsonValue,
    rendered: JsonValue,
    ir: HistoryIR,
    plan: TransformationPlan,
) -> None:
    del source, rendered
    for region in ir.regions:
        if (
            region.kind is RegionKind.HISTORY
            or region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
        ):
            continue
        for text_slice in region.text_slices:
            for operation in plan.operations:
                span = operation.target_span
                if (
                    text_slice.container_path == span.container_path
                    and span.char_start < text_slice.char_end
                    and text_slice.char_start < span.char_end
                ):
                    raise PortableContractError(
                        "NON_HISTORY_TARGET_FORBIDDEN",
                        "plan target overlaps a task/system/current/tool region",
                    )
        for protected_path in region.paths:
            for operation in plan.operations:
                span = operation.target_span
                if _path_is_prefix(protected_path, span.container_path) or _path_is_prefix(
                    span.container_path, protected_path
                ):
                    raise PortableContractError(
                        "NON_HISTORY_TARGET_FORBIDDEN",
                        "plan target overlaps a task/system/current/tool region",
                    )


def _independent_expected_request(source_request: JsonValue, plan: TransformationPlan) -> JsonValue:
    expected = copy_json(source_request)
    operations_by_path: dict[JsonPath, list[PlanOperation]] = defaultdict(list)
    for operation in plan.operations:
        operations_by_path[operation.target_span.container_path].append(operation)
    for path, operations in operations_by_path.items():
        source = get_at_path(source_request, path)
        if not isinstance(source, str):
            raise PortableContractError(
                "SPAN_CONTAINER_NOT_TEXT", "edit container is not text", path=json_path_text(path)
            )
        cursor = 0
        pieces: list[str] = []
        for operation in sorted(operations, key=lambda item: item.target_span.char_start):
            span = operation.target_span
            if span.char_start < cursor:
                raise PortableContractError("OVERLAPPING_PLAN_EDITS", "plan spans overlap")
            pieces.append(source[cursor : span.char_start])
            pieces.append("")
            cursor = span.char_end
        pieces.append(source[cursor:])
        set_at_path(expected, path, "".join(pieces))
    _apply_correction_insertions(expected, source_request, plan.operations)
    return expected


def restore_original(result: RenderResult) -> JsonValue:
    """Restore source request from rendered bytes plus reversible diff/mapping receipt."""

    if canonical_sha256(result.original_request) != result.source_request_sha256:
        raise PortableContractError("RENDER_ORIGINAL_DRIFT", "original request hash is invalid")
    if canonical_sha256(result.rendered_request) != result.rendered_request_sha256:
        raise PortableContractError("RENDER_HASH_MISMATCH", "rendered request hash is invalid")
    restored = copy_json(result.rendered_request)
    insertions_by_path: dict[JsonPath, list[ListInsertionDiff]] = defaultdict(list)
    for insertion in result.list_insertions:
        if canonical_sha256(insertion.inserted_value) != insertion.inserted_value_sha256:
            raise PortableContractError(
                "INSERTION_HASH_MISMATCH", "inserted value hash is inconsistent"
            )
        insertions_by_path[insertion.container_path].append(insertion)
    for path, insertions in insertions_by_path.items():
        ordered = sorted(insertions, key=lambda item: (item.source_index, item.operation_id))
        for offset, insertion in enumerate(ordered):
            if insertion.rendered_index != insertion.source_index + offset:
                raise PortableContractError(
                    "INSERTION_INDEX_MISMATCH", "rendered insertion index is non-canonical"
                )
    for insertion in sorted(
        result.list_insertions,
        key=lambda item: (json_path_text(item.container_path), item.rendered_index),
        reverse=True,
    ):
        container = get_at_path(restored, insertion.container_path)
        if not isinstance(container, list) or not 0 <= insertion.rendered_index < len(container):
            raise PortableContractError(
                "INSERTION_MAPPING_INVALID", "inserted list item cannot be resolved"
            )
        if container[insertion.rendered_index] != insertion.inserted_value:
            raise PortableContractError(
                "INSERTION_MAPPING_DRIFT", "inserted list item does not match receipt"
            )
        container.pop(insertion.rendered_index)
    diffs: dict[str, RenderDiff] = {}
    for diff in result.diffs:
        if diff.operation_id in diffs:
            raise PortableContractError("DUPLICATE_RENDER_DIFF", "diff IDs must be unique")
        if text_sha256(diff.original_text) != diff.original_sha256:
            raise PortableContractError("DIFF_HASH_MISMATCH", "original diff hash is invalid")
        if text_sha256(diff.rendered_text) != diff.rendered_sha256:
            raise PortableContractError("DIFF_HASH_MISMATCH", "rendered diff hash is invalid")
        diffs[diff.operation_id] = diff
    mappings_by_path: dict[JsonPath, list[SourceMapping]] = defaultdict(list)
    for mapping in result.source_mappings:
        mappings_by_path[mapping.container_path].append(mapping)
    consumed_diffs: set[str] = set()
    for path, mappings in mappings_by_path.items():
        rendered_text = get_at_path(restored, path)
        source_text = get_at_path(result.original_request, path)
        if not isinstance(rendered_text, str) or not isinstance(source_text, str):
            raise PortableContractError("SPAN_CONTAINER_NOT_TEXT", "rendered container is not text")
        ordered_mappings = sorted(
            mappings,
            key=lambda item: (
                item.source_char_start,
                item.source_char_end,
                item.kind.value,
                item.operation_id or "",
            ),
        )
        source_cursor = 0
        rendered_cursor = 0
        source_pieces: list[str] = []
        for mapping in ordered_mappings:
            if (
                mapping.source_char_start != source_cursor
                or mapping.rendered_char_start != rendered_cursor
                or mapping.source_char_end < mapping.source_char_start
                or mapping.rendered_char_end < mapping.rendered_char_start
            ):
                raise PortableContractError(
                    "SOURCE_MAPPING_COVERAGE_INVALID", "mapping has a gap, overlap, or bad offset"
                )
            if mapping.kind is MappingKind.COPIED:
                if mapping.operation_id is not None:
                    raise PortableContractError(
                        "COPIED_MAPPING_OPERATION_INVALID", "copied mapping cannot bind an edit"
                    )
                source_piece = source_text[mapping.source_char_start : mapping.source_char_end]
                rendered_piece = rendered_text[
                    mapping.rendered_char_start : mapping.rendered_char_end
                ]
                if source_piece != rendered_piece:
                    raise PortableContractError(
                        "COPIED_MAPPING_DRIFT", "copied bytes differ from the source"
                    )
                source_pieces.append(rendered_piece)
            elif mapping.kind in {MappingKind.DELETED, MappingKind.SYNTAX_REPAIR}:
                if mapping.rendered_char_start != mapping.rendered_char_end:
                    raise PortableContractError(
                        "DELETION_MAPPING_INVALID", "deletion must consume zero rendered text"
                    )
                mapped_diff = diffs.get(mapping.operation_id or "")
                if mapped_diff is None:
                    raise PortableContractError(
                        "MAPPING_DIFF_MISSING", "edit mapping has no exact diff"
                    )
                if (
                    mapped_diff.container_path != path
                    or mapped_diff.source_char_start != mapping.source_char_start
                    or mapped_diff.source_char_end != mapping.source_char_end
                    or mapped_diff.mapping_kind is not mapping.kind
                    or source_text[mapped_diff.source_char_start : mapped_diff.source_char_end]
                    != mapped_diff.original_text
                    or mapped_diff.rendered_text != ""
                ):
                    raise PortableContractError(
                        "MAPPING_DIFF_MISMATCH", "mapping and exact diff disagree"
                    )
                if mapped_diff.operation_id in consumed_diffs:
                    raise PortableContractError(
                        "DUPLICATE_DIFF_MAPPING", "one diff is mapped more than once"
                    )
                consumed_diffs.add(mapped_diff.operation_id)
                source_pieces.append(mapped_diff.original_text)
            else:
                raise PortableContractError(
                    "UNSUPPORTED_SOURCE_MAPPING", "text mapping kind is unsupported"
                )
            source_cursor = mapping.source_char_end
            rendered_cursor = mapping.rendered_char_end
        if source_cursor != len(source_text) or rendered_cursor != len(rendered_text):
            raise PortableContractError(
                "SOURCE_MAPPING_COVERAGE_INVALID", "mapping does not cover both containers"
            )
        reconstructed = "".join(source_pieces)
        if reconstructed != source_text:
            raise PortableContractError("NON_REVERSIBLE_MAPPING", "mapping restored wrong text")
        set_at_path(restored, path, reconstructed)
    if consumed_diffs != set(diffs):
        raise PortableContractError("UNMAPPED_RENDER_DIFF", "not every diff has one mapping")
    if restored != result.original_request:
        raise PortableContractError(
            "NON_REVERSIBLE_MAPPING", "receipt does not restore the untouched request"
        )
    return restored


def _reject_overlapping_spans(spans: Sequence[SourceSpan], *, code: str) -> None:
    by_path: dict[JsonPath, list[SourceSpan]] = defaultdict(list)
    for span in spans:
        by_path[span.container_path].append(span)
    for path_spans in by_path.values():
        ordered = sorted(path_spans, key=lambda item: (item.char_start, item.char_end))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.char_start < previous.char_end:
                raise PortableContractError(code, "source spans overlap")


def with_capability(
    ir: HistoryIR,
    *,
    supported_arms: tuple[ArmKind, ...],
    opaque_or_server_managed: bool = False,
) -> HistoryIR:
    """Test/conformance helper that keeps IR bytes while narrowing capabilities."""

    return replace(
        ir,
        capabilities=replace(
            ir.capabilities,
            supported_arms=supported_arms,
            opaque_or_server_managed=opaque_or_server_managed,
        ),
    )
