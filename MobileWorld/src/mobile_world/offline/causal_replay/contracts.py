"""Portable, model-agnostic contracts for G1 history transformations.

This module is intentionally dependency-light and side-effect free.  It describes
captured application requests, canonical history records, curated transformations,
codec capabilities, render receipts, provider results, and derived replay sidecars.
It does not inspect evidence, infer verdicts, call a provider, or execute an action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, cast, runtime_checkable

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
PathToken = str | int
JsonPath = tuple[PathToken, ...]

SCHEMA_PREFIX = "mobileworld.g1.portable-sentinel"
HISTORY_IR_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.history-ir/v1"
TRANSFORMATION_PLAN_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.transformation-plan/v1"
CAPABILITIES_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.codec-capabilities/v1"
PROVIDER_RESULT_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.provider-result/v1"
SIDECAR_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.sidecar/v1"
RENDER_RESULT_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.render-result/v1"
VALIDATION_RECEIPT_SCHEMA_VERSION = f"{SCHEMA_PREFIX}.validation-receipt/v1"


class HistoryFamily(str, Enum):
    RAW_REPLAY = "raw_replay"
    FLAT_PROGRESS = "flat_progress"
    ROLLING_SUMMARY = "rolling_summary"
    FLAT_PREVIOUS_ACTIONS = "flat_previous_actions"
    HYBRID_FOLDING = "hybrid_folding"
    STRUCTURED_FOLDING = "structured_folding"


class RegionKind(str, Enum):
    SYSTEM = "SYSTEM"
    TASK = "TASK"
    HISTORY = "HISTORY"
    CURRENT_OBSERVATION = "CURRENT_OBSERVATION"
    TOOL_PROTOCOL = "TOOL_PROTOCOL"
    PROVIDER_CONTROL = "PROVIDER_CONTROL"


class RegionAvailability(str, Enum):
    PRESENT = "PRESENT"
    COLOCATED = "COLOCATED"
    ABSENT_NOT_IN_HOST_CONTRACT = "ABSENT_NOT_IN_HOST_CONTRACT"


class RecordModality(str, Enum):
    TEXT = "TEXT"
    MULTIMODAL_TEXT = "MULTIMODAL_TEXT"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    ROLLING_SUMMARY = "ROLLING_SUMMARY"
    STRUCTURED_SECTION = "STRUCTURED_SECTION"


class RelatedContentKind(str, Enum):
    IMAGE = "IMAGE"
    TOOL_SCHEMA = "TOOL_SCHEMA"
    TOOL_RESULT = "TOOL_RESULT"


class RelationshipKind(str, Enum):
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    ACTION_OBSERVATION = "ACTION_OBSERVATION"
    SOURCE_VERSION = "SOURCE_VERSION"
    CURRENT_VERSION_OF = "CURRENT_VERSION_OF"
    SECTION_MEMBER = "SECTION_MEMBER"
    ALIGNED_RECORD = "ALIGNED_RECORD"


class SpanRole(str, Enum):
    RECORD_EXTENT = "RECORD_EXTENT"
    EDITABLE_CLAIM = "EDITABLE_CLAIM"
    BENIGN_SHAM = "BENIGN_SHAM"
    PROTECTED_PROTOCOL = "PROTECTED_PROTOCOL"
    PROTECTED_EXTERNAL_RESULT = "PROTECTED_EXTERNAL_RESULT"
    PROTECTED_MULTIMODAL = "PROTECTED_MULTIMODAL"
    ELIGIBLE_PROTOCOL_SHELL = "ELIGIBLE_PROTOCOL_SHELL"


class OperationKind(str, Enum):
    KEEP = "KEEP"
    DROP = "DROP"
    REPLACE = "REPLACE"
    ARCHIVE = "ARCHIVE"
    KEEP_UNCERTAIN = "KEEP_UNCERTAIN"


class ArmKind(str, Enum):
    ORIGINAL = "ORIGINAL"
    MASK = "MASK"
    MASK_CORRECTION = "MASK_CORRECTION"
    ORACLE_CLEAN = "ORACLE_CLEAN"
    SHAM_BENIGN_EDIT = "SHAM_BENIGN_EDIT"


class PlanSetProfile(str, Enum):
    """Frozen paired-arm profiles; distinct from a codec's maximum capability."""

    PORTABLE_CORE = "PORTABLE_CORE"
    G1_STRICT_MHR = "G1_STRICT_MHR"
    G1_CLEAN_CONTROL = "G1_CLEAN_CONTROL"


class CapabilityLevel(str, Enum):
    AUDIT_ONLY = "AUDIT_ONLY"
    ANNOTATION_ONLY = "ANNOTATION_ONLY"
    VALIDITY_TRANSFORMATION = "VALIDITY_TRANSFORMATION"
    FULL_TRANSFORMATION = "FULL_TRANSFORMATION"


class CodecScope(str, Enum):
    FIXTURE_ONLY = "FIXTURE_ONLY"
    LIVE = "LIVE"


class ExecutionMode(str, Enum):
    G1_SCIENTIFIC = "G1_SCIENTIFIC"
    RUNTIME = "RUNTIME"


class FailurePolicy(str, Enum):
    BLOCK = "BLOCK"
    FAIL_OPEN_ORIGINAL = "FAIL_OPEN_ORIGINAL"


class FallbackState(str, Enum):
    NOT_NEEDED = "NOT_NEEDED"
    BLOCKED_BEFORE_PROVIDER = "BLOCKED_BEFORE_PROVIDER"
    EXPLICIT_ORIGINAL = "EXPLICIT_ORIGINAL"


class MappingKind(str, Enum):
    COPIED = "COPIED"
    DELETED = "DELETED"
    INSERTED = "INSERTED"
    SYNTAX_REPAIR = "SYNTAX_REPAIR"


class CorrectionContextKind(str, Enum):
    TEXT_CONTENT_BLOCK = "TEXT_CONTENT_BLOCK"
    CHAT_MESSAGE = "CHAT_MESSAGE"


class CorrectionPlacement(str, Enum):
    BEFORE = "BEFORE"
    AFTER = "AFTER"


class ProviderResultStatus(str, Enum):
    RETURNED = "RETURNED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    MISSING = "MISSING"


class ProviderDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    BYPASS_ORIGINAL = "BYPASS_ORIGINAL"


class PortableContractError(ValueError):
    """Machine-readable failure that is never permission to invoke a provider."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        context: dict[str, JsonValue] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.context = context or {}
        self.provider_invocation_allowed = False
        detail = f"{code}: {message}"
        if path:
            detail += f" at {path}"
        super().__init__(detail)


def canonical_json_bytes(value: JsonValue) -> bytes:
    """Return the single canonical JSON encoding used by the portable contract."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PortableContractError("NON_CANONICAL_JSON", str(exc)) from exc


def canonical_sha256(value: JsonValue) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def copy_json(value: JsonValue) -> JsonValue:
    """Deep-copy while also rejecting non-JSON caller-owned values."""

    return cast(JsonValue, json.loads(canonical_json_bytes(value)))


def json_path_text(path: JsonPath) -> str:
    rendered = "$"
    for token in path:
        if isinstance(token, int):
            rendered += f"[{token}]"
        else:
            rendered += f".{token}"
    return rendered


def get_at_path(root: JsonValue, path: JsonPath) -> JsonValue:
    node = root
    for token in path:
        try:
            if isinstance(token, int) and isinstance(node, list):
                node = node[token]
            elif isinstance(token, str) and isinstance(node, dict):
                node = node[token]
            else:
                raise KeyError(token)
        except (IndexError, KeyError) as exc:
            raise PortableContractError(
                "REQUEST_PATH_NOT_FOUND",
                "request path cannot be resolved",
                path=json_path_text(path),
            ) from exc
    return node


def set_at_path(root: JsonValue, path: JsonPath, value: JsonValue) -> None:
    if not path:
        raise PortableContractError("ROOT_REPLACEMENT_FORBIDDEN", "root request cannot be replaced")
    parent = get_at_path(root, path[:-1])
    token = path[-1]
    if isinstance(token, int) and isinstance(parent, list):
        if token < 0 or token >= len(parent):
            raise PortableContractError(
                "REQUEST_PATH_NOT_FOUND", "list index is out of bounds", path=json_path_text(path)
            )
        parent[token] = value
        return
    if isinstance(token, str) and isinstance(parent, dict) and token in parent:
        parent[token] = value
        return
    raise PortableContractError(
        "REQUEST_PATH_NOT_FOUND", "request path cannot be assigned", path=json_path_text(path)
    )


def stable_id(prefix: str, payload: dict[str, JsonValue]) -> str:
    return f"{prefix}-{canonical_sha256(payload)[:32]}"


@dataclass(frozen=True)
class SourceSpan:
    container_path: JsonPath
    char_start: int
    char_end: int
    utf8_byte_start: int
    utf8_byte_end: int
    exact_text: str
    span_sha256: str
    span_role: SpanRole
    claim_id: str | None = None

    @classmethod
    def from_text(
        cls,
        *,
        container_path: JsonPath,
        container_text: str,
        char_start: int,
        char_end: int,
        span_role: SpanRole,
        claim_id: str | None = None,
    ) -> SourceSpan:
        if char_start < 0 or char_end <= char_start or char_end > len(container_text):
            raise PortableContractError(
                "INVALID_SOURCE_SPAN", "source span must be non-empty and in bounds"
            )
        exact_text = container_text[char_start:char_end]
        return cls(
            container_path=container_path,
            char_start=char_start,
            char_end=char_end,
            utf8_byte_start=len(container_text[:char_start].encode("utf-8")),
            utf8_byte_end=len(container_text[:char_end].encode("utf-8")),
            exact_text=exact_text,
            span_sha256=text_sha256(exact_text),
            span_role=span_role,
            claim_id=claim_id,
        )

    def validate_against(self, request: JsonValue) -> str:
        container = get_at_path(request, self.container_path)
        if not isinstance(container, str):
            raise PortableContractError(
                "SPAN_CONTAINER_NOT_TEXT",
                "source span container is not text",
                path=json_path_text(self.container_path),
            )
        if (
            self.char_start < 0
            or self.char_end <= self.char_start
            or self.char_end > len(container)
        ):
            raise PortableContractError(
                "INVALID_SOURCE_SPAN",
                "source span is empty or out of bounds",
                path=json_path_text(self.container_path),
            )
        selected = container[self.char_start : self.char_end]
        if selected != self.exact_text or text_sha256(selected) != self.span_sha256:
            raise PortableContractError(
                "SOURCE_SPAN_DRIFT",
                "source span bytes do not match the frozen span",
                path=json_path_text(self.container_path),
            )
        byte_start = len(container[: self.char_start].encode("utf-8"))
        byte_end = len(container[: self.char_end].encode("utf-8"))
        if (byte_start, byte_end) != (self.utf8_byte_start, self.utf8_byte_end):
            raise PortableContractError(
                "UTF8_OFFSET_DRIFT",
                "UTF-8 byte offsets do not match character offsets",
                path=json_path_text(self.container_path),
            )
        return container

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_path": list(self.container_path),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "utf8_byte_start": self.utf8_byte_start,
            "utf8_byte_end": self.utf8_byte_end,
            "exact_text": self.exact_text,
            "span_sha256": self.span_sha256,
            "span_role": self.span_role.value,
            "claim_id": self.claim_id,
        }


@dataclass(frozen=True)
class FrozenTextSlice:
    container_path: JsonPath
    char_start: int
    char_end: int
    utf8_byte_start: int
    utf8_byte_end: int
    exact_text: str
    span_sha256: str

    def validate_against(self, request: JsonValue) -> str:
        span = SourceSpan(
            container_path=self.container_path,
            char_start=self.char_start,
            char_end=self.char_end,
            utf8_byte_start=self.utf8_byte_start,
            utf8_byte_end=self.utf8_byte_end,
            exact_text=self.exact_text,
            span_sha256=self.span_sha256,
            span_role=SpanRole.RECORD_EXTENT,
        )
        return span.validate_against(request)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_path": list(self.container_path),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "utf8_byte_start": self.utf8_byte_start,
            "utf8_byte_end": self.utf8_byte_end,
            "exact_text": self.exact_text,
            "span_sha256": self.span_sha256,
        }


@dataclass(frozen=True)
class RequestRegion:
    region_id: str
    kind: RegionKind
    paths: tuple[JsonPath, ...]
    text_slices: tuple[FrozenTextSlice, ...]
    source_sha256: str
    availability: RegionAvailability
    absence_reason: str | None
    preserve_exact: bool = True

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "region_id": self.region_id,
            "kind": self.kind.value,
            "paths": [list(path) for path in self.paths],
            "text_slices": [item.to_dict() for item in self.text_slices],
            "source_sha256": self.source_sha256,
            "availability": self.availability.value,
            "absence_reason": self.absence_reason,
            "preserve_exact": self.preserve_exact,
        }


@dataclass(frozen=True)
class HistoryRelationship:
    relationship_id: str
    kind: RelationshipKind
    source_record_id: str
    target_record_id: str | None = None
    target_path: JsonPath | None = None
    target_version_id: str | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "relationship_id": self.relationship_id,
            "kind": self.kind.value,
            "source_record_id": self.source_record_id,
            "target_record_id": self.target_record_id,
            "target_path": None if self.target_path is None else list(self.target_path),
            "target_version_id": self.target_version_id,
        }


@dataclass(frozen=True)
class SourceVersionRef:
    version_id: str
    source_record_id: str
    version: int
    source_request_sha256: str
    source_span_sha256: str
    write_time: str | None
    model_visible_in_current_request: bool
    provenance: dict[str, JsonValue]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "version_id": self.version_id,
            "source_record_id": self.source_record_id,
            "version": self.version,
            "source_request_sha256": self.source_request_sha256,
            "source_span_sha256": self.source_span_sha256,
            "write_time": self.write_time,
            "model_visible_in_current_request": self.model_visible_in_current_request,
            "provenance": copy_json(self.provenance),
        }


@dataclass(frozen=True)
class RecordCoordinates:
    request_path: JsonPath
    message_index: int | None
    content_block_index: int | None
    representation_record_index: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "request_path": list(self.request_path),
            "message_index": self.message_index,
            "content_block_index": self.content_block_index,
            "representation_record_index": self.representation_record_index,
        }


@dataclass(frozen=True)
class RelatedContentRef:
    path: JsonPath
    kind: RelatedContentKind
    value_sha256: str
    blob_sha256: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "path": list(self.path),
            "kind": self.kind.value,
            "value_sha256": self.value_sha256,
            "blob_sha256": self.blob_sha256,
        }


@dataclass(frozen=True)
class HistoryRecord:
    record_id: str
    record_key: str
    record_class: str
    region_id: str
    role: str
    author: str
    modality: RecordModality
    coordinates: RecordCoordinates
    record_sha256: str
    source_span: SourceSpan
    editable_spans: tuple[SourceSpan, ...]
    protected_spans: tuple[SourceSpan, ...]
    write_time: str | None
    exposure_time: str
    provenance: dict[str, JsonValue]
    correction_anchors: tuple[CorrectionAnchor, ...] = ()
    relationships: tuple[HistoryRelationship, ...] = ()
    related_content: tuple[RelatedContentRef, ...] = ()
    version: int = 1
    source_version_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "record_id": self.record_id,
            "record_key": self.record_key,
            "record_class": self.record_class,
            "region_id": self.region_id,
            "role": self.role,
            "author": self.author,
            "modality": self.modality.value,
            "coordinates": self.coordinates.to_dict(),
            "record_sha256": self.record_sha256,
            "source_span": self.source_span.to_dict(),
            "editable_spans": [span.to_dict() for span in self.editable_spans],
            "protected_spans": [span.to_dict() for span in self.protected_spans],
            "write_time": self.write_time,
            "exposure_time": self.exposure_time,
            "provenance": copy_json(self.provenance),
            "correction_anchors": [anchor.to_dict() for anchor in self.correction_anchors],
            "relationships": [item.to_dict() for item in self.relationships],
            "related_content": [item.to_dict() for item in self.related_content],
            "version": self.version,
            "source_version_ids": list(self.source_version_ids),
        }


@dataclass(frozen=True)
class CodecCapabilities:
    codec_id: str
    contract_version: str
    history_family: HistoryFamily
    level: CapabilityLevel
    scope: CodecScope
    supported_operations: tuple[OperationKind, ...]
    supported_arms: tuple[ArmKind, ...]
    preserves_roles: bool
    preserves_order: bool
    preserves_multimodal_blocks: bool
    preserves_tool_adjacency: bool
    preserves_protocol_shell: bool
    live_ready: bool
    opaque_or_server_managed: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": CAPABILITIES_SCHEMA_VERSION,
            "codec_id": self.codec_id,
            "contract_version": self.contract_version,
            "history_family": self.history_family.value,
            "level": self.level.value,
            "scope": self.scope.value,
            "supported_operations": [item.value for item in self.supported_operations],
            "supported_arms": [item.value for item in self.supported_arms],
            "preservation": {
                "roles": self.preserves_roles,
                "ordering": self.preserves_order,
                "multimodal_blocks": self.preserves_multimodal_blocks,
                "tool_call_result_adjacency": self.preserves_tool_adjacency,
                "protocol_shell": self.preserves_protocol_shell,
            },
            "live_ready": self.live_ready,
            "opaque_or_server_managed": self.opaque_or_server_managed,
        }


@runtime_checkable
class HistoryCodecDeclaration(Protocol):
    """Authoritative registry-resolved identity used by the pre-send guard."""

    @property
    def codec_id(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    @property
    def history_family(self) -> HistoryFamily: ...

    @property
    def capabilities(self) -> CodecCapabilities: ...

    def extract(self, application_request: JsonValue) -> HistoryIR: ...


@runtime_checkable
class HistoryCodecResolver(Protocol):
    """Registry boundary used by every provider-authorizing validator."""

    def by_id(self, codec_id: str, contract_version: str = "v1") -> HistoryCodecDeclaration: ...


@dataclass(frozen=True)
class HistoryIR:
    host_id: str
    history_family: HistoryFamily
    codec_id: str
    codec_contract_version: str
    raw_request_sha256: str
    regions: tuple[RequestRegion, ...]
    records: tuple[HistoryRecord, ...]
    source_versions: tuple[SourceVersionRef, ...]
    capabilities: CodecCapabilities
    warnings: tuple[str, ...] = ()

    def record_by_id(self, record_id: str) -> HistoryRecord:
        matches = [record for record in self.records if record.record_id == record_id]
        if len(matches) != 1:
            raise PortableContractError(
                "UNKNOWN_OR_DUPLICATE_RECORD", "target record must resolve exactly once"
            )
        return matches[0]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": HISTORY_IR_SCHEMA_VERSION,
            "host_id": self.host_id,
            "history_family": self.history_family.value,
            "codec_id": self.codec_id,
            "codec_contract_version": self.codec_contract_version,
            "raw_request_sha256": self.raw_request_sha256,
            "regions": [region.to_dict() for region in self.regions],
            "records": [record.to_dict() for record in self.records],
            "source_versions": [item.to_dict() for item in self.source_versions],
            "capabilities": self.capabilities.to_dict(),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    sha256: str
    role: str
    event_seq: int | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "evidence_id": self.evidence_id,
            "sha256": self.sha256,
            "role": self.role,
            "event_seq": self.event_seq,
        }


@dataclass(frozen=True)
class CorrectionAnchor:
    container_path: JsonPath
    insert_index: int
    source_container_sha256: str
    owner_region_id: str
    host_context_path: JsonPath
    host_context_sha256: str
    role_path: JsonPath
    expected_role: str
    reference_path: JsonPath
    reference_sha256: str
    placement: CorrectionPlacement
    context_kind: CorrectionContextKind
    visible_prefix: str
    visible_suffix: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_path": list(self.container_path),
            "insert_index": self.insert_index,
            "source_container_sha256": self.source_container_sha256,
            "owner_region_id": self.owner_region_id,
            "host_context_path": list(self.host_context_path),
            "host_context_sha256": self.host_context_sha256,
            "role_path": list(self.role_path),
            "expected_role": self.expected_role,
            "reference_path": list(self.reference_path),
            "reference_sha256": self.reference_sha256,
            "placement": self.placement.value,
            "context_kind": self.context_kind.value,
            "visible_prefix": self.visible_prefix,
            "visible_suffix": self.visible_suffix,
        }


@dataclass(frozen=True)
class PlanOperation:
    operation_id: str
    kind: OperationKind
    target_record_id: str
    target_span: SourceSpan
    replacement_text: str | None = None
    replacement_author: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    protocol_shell_for: tuple[str, ...] = ()
    correction_anchor: CorrectionAnchor | None = None
    rendered_correction_context: JsonValue = None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "kind": self.kind.value,
            "target_record_id": self.target_record_id,
            "target_span": self.target_span.to_dict(),
            "replacement_text": self.replacement_text,
            "replacement_author": self.replacement_author,
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "protocol_shell_for": list(self.protocol_shell_for),
            "correction_anchor": (
                None if self.correction_anchor is None else self.correction_anchor.to_dict()
            ),
            "rendered_correction_context": copy_json(self.rendered_correction_context),
        }


@dataclass(frozen=True)
class TransformationPlan:
    plan_id: str
    host_id: str
    history_family: HistoryFamily
    codec_id: str
    codec_contract_version: str
    source_request_sha256: str
    arm: ArmKind
    operations: tuple[PlanOperation, ...]
    curated: bool = True
    deployment_prediction: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": TRANSFORMATION_PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "host_id": self.host_id,
            "history_family": self.history_family.value,
            "codec_id": self.codec_id,
            "codec_contract_version": self.codec_contract_version,
            "source_request_sha256": self.source_request_sha256,
            "arm": self.arm.value,
            "curated": self.curated,
            "deployment_prediction": self.deployment_prediction,
            "operations": [operation.to_dict() for operation in self.operations],
        }


@dataclass(frozen=True)
class RenderDiff:
    operation_id: str
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    original_text: str
    rendered_text: str
    original_sha256: str
    rendered_sha256: str
    mapping_kind: MappingKind

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "container_path": list(self.container_path),
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "original_text": self.original_text,
            "rendered_text": self.rendered_text,
            "original_sha256": self.original_sha256,
            "rendered_sha256": self.rendered_sha256,
            "mapping_kind": self.mapping_kind.value,
        }


@dataclass(frozen=True)
class SourceMapping:
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    rendered_char_start: int
    rendered_char_end: int
    kind: MappingKind
    operation_id: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_path": list(self.container_path),
            "source_char_start": self.source_char_start,
            "source_char_end": self.source_char_end,
            "rendered_char_start": self.rendered_char_start,
            "rendered_char_end": self.rendered_char_end,
            "kind": self.kind.value,
            "operation_id": self.operation_id,
        }


@dataclass(frozen=True)
class ListInsertionDiff:
    operation_id: str
    container_path: JsonPath
    source_index: int
    rendered_index: int
    inserted_value: JsonValue
    inserted_value_sha256: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation_id": self.operation_id,
            "container_path": list(self.container_path),
            "source_index": self.source_index,
            "rendered_index": self.rendered_index,
            "inserted_value": copy_json(self.inserted_value),
            "inserted_value_sha256": self.inserted_value_sha256,
        }


@dataclass(frozen=True)
class RenderResult:
    original_request: JsonValue
    rendered_request: JsonValue
    source_request_sha256: str
    rendered_request_sha256: str
    plan_sha256: str
    capability_sha256: str
    requested_arm: ArmKind
    effective_arm: ArmKind | None
    execution_mode: ExecutionMode
    failure_policy: FailurePolicy
    diffs: tuple[RenderDiff, ...]
    list_insertions: tuple[ListInsertionDiff, ...]
    source_mappings: tuple[SourceMapping, ...]
    warnings: tuple[str, ...]
    fallback_state: FallbackState
    count_as_treatment: bool
    unsupported_reason: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": RENDER_RESULT_SCHEMA_VERSION,
            "original_request": copy_json(self.original_request),
            "rendered_request": copy_json(self.rendered_request),
            "source_request_sha256": self.source_request_sha256,
            "rendered_request_sha256": self.rendered_request_sha256,
            "plan_sha256": self.plan_sha256,
            "capability_sha256": self.capability_sha256,
            "requested_arm": self.requested_arm.value,
            "effective_arm": None if self.effective_arm is None else self.effective_arm.value,
            "execution_mode": self.execution_mode.value,
            "failure_policy": self.failure_policy.value,
            "diffs": [item.to_dict() for item in self.diffs],
            "list_insertions": [item.to_dict() for item in self.list_insertions],
            "source_mappings": [item.to_dict() for item in self.source_mappings],
            "warnings": list(self.warnings),
            "fallback_state": self.fallback_state.value,
            "count_as_treatment": self.count_as_treatment,
            "unsupported_reason": self.unsupported_reason,
        }


@dataclass(frozen=True)
class ValidationReceipt:
    valid: bool
    provider_invocation_allowed: bool
    provider_decision: ProviderDecision
    execution_mode: ExecutionMode
    failure_policy: FailurePolicy
    invocation_attempted: bool
    source_request_sha256: str
    rendered_request_sha256: str
    plan_sha256: str
    plan_set_sha256: str
    plan_set_profile: PlanSetProfile
    capability_sha256: str
    history_codec_contract_version: str
    intended_provider_codec_id: str | None
    intended_provider_contract_version: str | None
    intended_endpoint_revision: str | None
    model_parameters_sha256: str | None
    checks: tuple[str, ...]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": VALIDATION_RECEIPT_SCHEMA_VERSION,
            "valid": self.valid,
            "provider_invocation_allowed": self.provider_invocation_allowed,
            "provider_decision": self.provider_decision.value,
            "execution_mode": self.execution_mode.value,
            "failure_policy": self.failure_policy.value,
            "invocation_attempted": self.invocation_attempted,
            "source_request_sha256": self.source_request_sha256,
            "rendered_request_sha256": self.rendered_request_sha256,
            "plan_sha256": self.plan_sha256,
            "plan_set_sha256": self.plan_set_sha256,
            "plan_set_profile": self.plan_set_profile.value,
            "capability_sha256": self.capability_sha256,
            "history_codec_contract_version": self.history_codec_contract_version,
            "intended_provider_codec_id": self.intended_provider_codec_id,
            "intended_provider_contract_version": self.intended_provider_contract_version,
            "intended_endpoint_revision": self.intended_endpoint_revision,
            "model_parameters_sha256": self.model_parameters_sha256,
            "checks": list(self.checks),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class PreparedProviderRequest:
    provider_codec_id: str
    provider_contract_version: str
    endpoint_revision: str
    application_request_sha256: str
    encoded_request_sha256: str
    encoded_request: bytes
    model_parameters: dict[str, JsonValue]
    model_parameters_sha256: str


@dataclass(frozen=True)
class AuthorizedProviderRequest:
    provider_codec_id: str
    provider_contract_version: str
    endpoint_revision: str
    application_request_sha256: str
    encoded_request_sha256: str
    encoded_request: bytes
    model_parameters_json: bytes
    model_parameters_sha256: str
    validation_receipt: ValidationReceipt

    @property
    def prepared(self) -> PreparedProviderRequest:
        """Return a fresh envelope; caller mutation cannot alter the authorization."""

        decoded = json.loads(self.model_parameters_json)
        if not isinstance(decoded, dict):
            raise PortableContractError(
                "INVALID_MODEL_PARAMETERS", "authorized model parameters are not an object"
            )
        return PreparedProviderRequest(
            provider_codec_id=self.provider_codec_id,
            provider_contract_version=self.provider_contract_version,
            endpoint_revision=self.endpoint_revision,
            application_request_sha256=self.application_request_sha256,
            encoded_request_sha256=self.encoded_request_sha256,
            encoded_request=bytes(self.encoded_request),
            model_parameters=cast(dict[str, JsonValue], decoded),
            model_parameters_sha256=self.model_parameters_sha256,
        )


@dataclass(frozen=True)
class RawProviderResponse:
    response_bytes: bytes
    transport_metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    provider_codec_id: str
    provider_contract_version: str
    endpoint_revision: str
    status: ProviderResultStatus
    application_request_sha256: str
    encoded_request_sha256: str
    response_sha256: str | None
    raw_response_ref: dict[str, JsonValue] | None
    normalized_action: dict[str, JsonValue] | None
    normalized_action_sha256: str | None
    error: dict[str, JsonValue] | None
    model_parameters: dict[str, JsonValue]
    model_parameters_sha256: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PROVIDER_RESULT_SCHEMA_VERSION,
            "provider_codec_id": self.provider_codec_id,
            "provider_contract_version": self.provider_contract_version,
            "endpoint_revision": self.endpoint_revision,
            "status": self.status.value,
            "application_request_sha256": self.application_request_sha256,
            "encoded_request_sha256": self.encoded_request_sha256,
            "response_sha256": self.response_sha256,
            "raw_response_ref": copy_json(self.raw_response_ref),
            "normalized_action": copy_json(self.normalized_action),
            "normalized_action_sha256": self.normalized_action_sha256,
            "error": copy_json(self.error),
            "model_parameters": copy_json(self.model_parameters),
            "model_parameters_sha256": self.model_parameters_sha256,
        }


@dataclass(frozen=True)
class ProviderAttemptMetadata:
    invocation_attempted: bool
    provider_codec_id: str | None
    provider_contract_version: str | None
    endpoint_revision: str | None
    application_request_sha256: str | None
    encoded_request_sha256: str | None
    model_parameters_sha256: str | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "invocation_attempted": self.invocation_attempted,
            "provider_codec_id": self.provider_codec_id,
            "provider_contract_version": self.provider_contract_version,
            "endpoint_revision": self.endpoint_revision,
            "application_request_sha256": self.application_request_sha256,
            "encoded_request_sha256": self.encoded_request_sha256,
            "model_parameters_sha256": self.model_parameters_sha256,
        }


@runtime_checkable
class ProviderCodec(Protocol):
    """Interface only; ALE-320 supplies no network-capable implementation."""

    @property
    def codec_id(self) -> str: ...

    @property
    def contract_version(self) -> str: ...

    def encode(
        self, application_request: JsonValue, model_parameters: dict[str, JsonValue]
    ) -> PreparedProviderRequest: ...

    def send(self, authorized: AuthorizedProviderRequest) -> RawProviderResponse: ...

    def normalize(
        self, authorized: AuthorizedProviderRequest, response: RawProviderResponse
    ) -> ProviderResult: ...


@runtime_checkable
class ProviderCodecResolver(Protocol):
    """Registry boundary used by the final provider authorization guard."""

    def by_id(self, codec_id: str, contract_version: str = "v1") -> ProviderCodec: ...


@dataclass(frozen=True)
class ReplaySidecar:
    sidecar_id: str
    history_ir: HistoryIR
    transformation_plan: TransformationPlan
    paired_plan_set: tuple[TransformationPlan, ...]
    plan_set_profile: PlanSetProfile
    render_result: RenderResult
    validation_receipt: ValidationReceipt
    evidence_refs: tuple[EvidenceRef, ...]
    provider_attempt: ProviderAttemptMetadata
    provider_result: ProviderResult | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "sidecar_id": self.sidecar_id,
            "untouched_request": {
                "sha256": self.render_result.source_request_sha256,
                "value": copy_json(self.render_result.original_request),
            },
            "history_ir_sha256": canonical_sha256(self.history_ir.to_dict()),
            "history_ir": self.history_ir.to_dict(),
            "transformation_plan_sha256": canonical_sha256(self.transformation_plan.to_dict()),
            "transformation_plan": self.transformation_plan.to_dict(),
            "paired_plan_set_sha256": self.validation_receipt.plan_set_sha256,
            "plan_set_profile": self.plan_set_profile.value,
            "paired_plan_set": [plan.to_dict() for plan in self.paired_plan_set],
            "render_result_sha256": canonical_sha256(self.render_result.to_dict()),
            "exact_diff": [item.to_dict() for item in self.render_result.diffs],
            "list_insertions": [item.to_dict() for item in self.render_result.list_insertions],
            "source_mapping": [item.to_dict() for item in self.render_result.source_mappings],
            "final_request": {
                "sha256": self.render_result.rendered_request_sha256,
                "value": copy_json(self.render_result.rendered_request),
            },
            "evidence_refs": [item.to_dict() for item in self.evidence_refs],
            "capability": self.history_ir.capabilities.to_dict(),
            "fallback": {
                "state": self.render_result.fallback_state.value,
                "count_as_treatment": self.render_result.count_as_treatment,
                "warnings": list(self.render_result.warnings),
            },
            "execution": {
                "requested_arm": self.render_result.requested_arm.value,
                "effective_arm": (
                    None
                    if self.render_result.effective_arm is None
                    else self.render_result.effective_arm.value
                ),
                "execution_mode": self.render_result.execution_mode.value,
                "failure_policy": self.render_result.failure_policy.value,
                "capability_sha256": self.render_result.capability_sha256,
                "unsupported_reason": self.render_result.unsupported_reason,
            },
            "validation_receipt": self.validation_receipt.to_dict(),
            "provider_attempt": self.provider_attempt.to_dict(),
            "provider_result": None
            if self.provider_result is None
            else self.provider_result.to_dict(),
        }


def flattened_evidence_refs(plan: TransformationPlan) -> tuple[EvidenceRef, ...]:
    by_id: dict[str, EvidenceRef] = {}
    for operation in plan.operations:
        for evidence in operation.evidence_refs:
            existing = by_id.get(evidence.evidence_id)
            if existing is not None and existing != evidence:
                raise PortableContractError(
                    "EVIDENCE_ID_COLLISION", "one evidence ID resolves to different records"
                )
            by_id[evidence.evidence_id] = evidence
    return tuple(by_id[key] for key in sorted(by_id))


def assert_json_round_trip(value: Any) -> JsonValue:
    """Public helper used by codecs to reject opaque Python/provider objects."""

    copied = copy_json(value)
    if canonical_json_bytes(copied) != canonical_json_bytes(value):
        raise PortableContractError("NON_DETERMINISTIC_JSON", "JSON round trip changed value")
    return copied
