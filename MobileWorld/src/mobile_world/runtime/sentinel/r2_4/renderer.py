"""Exact-span renderer for an authority-bound R2.4 vertical slice."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, NoReturn, cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CapabilityLevel,
    CodecCapabilities,
    CodecScope,
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    FrozenTextSlice,
    HistoryFamily,
    HistoryIR,
    HistoryRecord,
    HistoryRelationship,
    JsonPath,
    JsonValue,
    OperationKind,
    RecordCoordinates,
    RecordModality,
    RegionAvailability,
    RegionKind,
    RelatedContentKind,
    RelatedContentRef,
    RelationshipKind,
    RequestRegion,
    SourceSpan,
    SourceVersionRef,
    SpanRole,
)
from mobile_world.runtime.sentinel.r2_2.contracts import RuntimeOperationKind
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalAdmittedPlanV1,
    RuntimeVerticalExecutionScope,
    RuntimeVerticalOperationV1,
    canonical_json_bytes,
    canonical_sha256,
    replacement_text_for_template,
    snapshot_json_value,
    snapshot_vertical_plan,
    vertical_plan_sha256,
)

RUNTIME_VERTICAL_RENDER_RESULT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-render-result/v1"
_MAX_GRAPH_DEPTH = 64
_MAX_GRAPH_VISITS = 262_144
_RENDER_CHECKS = (
    "R24_EXECUTION_SCOPE_BOUND",
    "R24_SOURCE_AND_HISTORY_IR_BOUND",
    "R24_EDITABLE_HISTORY_SPANS_BOUND",
    "R24_FIXED_REPLACEMENT_TEMPLATE_ONLY",
    "R24_EXACT_DIFF_RECOMPUTED",
    "R24_NON_HISTORY_BYTES_PRESERVED",
    "R24_EXISTING_BLOCK_ORDER_PRESERVED",
    "R24_CURRENT_IMAGE_BYTES_PRESERVED",
    "R24_REVERSIBLE_SOURCE_MAPPING",
    "R24_CALLER_INPUT_IMMUTABLE",
    "R24_ZERO_TARGET_NOOP_VALID",
)


class RuntimeVerticalMappingKind(StrEnum):
    COPIED = "COPIED"
    EDITED = "EDITED"


def _fail(code: str, message: str) -> NoReturn:
    raise R24ContractError(code, message)


def _require_path(value: object, name: str) -> JsonPath:
    if type(value) is not tuple:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact tuple")
    path = cast(tuple[object, ...], value)
    if not path or any(
        (type(token) is not str and type(token) is not int)
        or (type(token) is int and token < 0)
        or (type(token) is str and not token)
        for token in path
    ):
        _fail("INVALID_JSON_PATH", f"{name} contains an invalid token")
    return cast(JsonPath, path)


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _path_key(path: JsonPath) -> tuple[tuple[int, str | int], ...]:
    return tuple((0, token) if type(token) is str else (1, token) for token in path)


def _path_is_prefix(prefix: JsonPath, path: JsonPath) -> bool:
    return len(prefix) <= len(path) and path[: len(prefix)] == prefix


def _get_at_path(root: JsonValue, path: JsonPath) -> JsonValue:
    current: JsonValue = root
    for token in _require_path(path, "path"):
        if type(token) is str and type(current) is dict and token in current:
            current = current[token]
        elif type(token) is int and type(current) is list and 0 <= token < len(current):
            current = current[token]
        else:
            _fail("PATH_RESOLUTION_FAILED", "JSON path does not resolve exactly")
    return current


def _set_at_path(root: JsonValue, path: JsonPath, value: JsonValue) -> None:
    path = _require_path(path, "path")
    parent = _get_at_path(root, path[:-1]) if len(path) > 1 else root
    token = path[-1]
    if type(token) is str and type(parent) is dict and token in parent:
        parent[token] = value
        return
    if type(token) is int and type(parent) is list and 0 <= token < len(parent):
        parent[token] = value
        return
    _fail("PATH_RESOLUTION_FAILED", "JSON path cannot be assigned exactly")


@dataclass(frozen=True, slots=True)
class RuntimeVerticalTextDiffV1:
    operation_id: str
    kind: RuntimeOperationKind
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    rendered_char_start: int
    rendered_char_end: int
    original_text: str
    rendered_text: str
    original_sha256: str
    rendered_sha256: str

    def __post_init__(self) -> None:
        if type(self.operation_id) is not str or not self.operation_id:
            _fail("INVALID_SEMANTIC_ID", "diff operation_id is empty")
        if type(self.kind) is not RuntimeOperationKind or self.kind not in {
            RuntimeOperationKind.DROP,
            RuntimeOperationKind.REPLACE,
        }:
            _fail("INVALID_RENDER_DIFF", "diff kind is not material")
        _require_path(self.container_path, "diff container_path")
        for value, name in (
            (self.source_char_start, "source_char_start"),
            (self.source_char_end, "source_char_end"),
            (self.rendered_char_start, "rendered_char_start"),
            (self.rendered_char_end, "rendered_char_end"),
        ):
            if type(value) is not int or value < 0:
                _fail("INVALID_RENDER_DIFF", f"{name} must be non-negative")
        if self.source_char_end <= self.source_char_start:
            _fail("INVALID_RENDER_DIFF", "source diff must be non-empty")
        if self.rendered_char_end < self.rendered_char_start:
            _fail("INVALID_RENDER_DIFF", "rendered diff offsets are inverted")
        if type(self.original_text) is not str or not self.original_text:
            _fail("INVALID_RENDER_DIFF", "original diff text is empty")
        if type(self.rendered_text) is not str:
            _fail("INVALID_RENDER_DIFF", "rendered diff text is not exact str")
        if self.kind is RuntimeOperationKind.DROP and self.rendered_text:
            _fail("INVALID_RENDER_DIFF", "DROP must render empty text")
        if self.kind is RuntimeOperationKind.REPLACE and not self.rendered_text:
            _fail("INVALID_RENDER_DIFF", "REPLACE must render the closed template")
        if _text_sha256(self.original_text) != self.original_sha256 or (
            _text_sha256(self.rendered_text) != self.rendered_sha256
        ):
            _fail("DIFF_HASH_MISMATCH", "diff text hash differs")
        if len(self.rendered_text) != self.rendered_char_end - self.rendered_char_start:
            _fail("INVALID_RENDER_DIFF", "rendered offsets do not match replacement width")


@dataclass(frozen=True, slots=True)
class RuntimeVerticalSourceMappingV1:
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    rendered_char_start: int
    rendered_char_end: int
    kind: RuntimeVerticalMappingKind
    operation_id: str | None

    def __post_init__(self) -> None:
        _require_path(self.container_path, "mapping container_path")
        for value in (
            self.source_char_start,
            self.source_char_end,
            self.rendered_char_start,
            self.rendered_char_end,
        ):
            if type(value) is not int or value < 0:
                _fail("INVALID_SOURCE_MAPPING", "mapping offsets must be non-negative")
        if self.source_char_end < self.source_char_start or (
            self.rendered_char_end < self.rendered_char_start
        ):
            _fail("INVALID_SOURCE_MAPPING", "mapping offsets are inverted")
        if type(self.kind) is not RuntimeVerticalMappingKind:
            _fail("UNTRUSTED_RUNTIME_TYPE", "mapping kind is untrusted")
        if self.kind is RuntimeVerticalMappingKind.COPIED:
            if self.operation_id is not None or (
                self.source_char_end - self.source_char_start
                != self.rendered_char_end - self.rendered_char_start
            ):
                _fail("INVALID_SOURCE_MAPPING", "COPIED mapping is not one-to-one")
        elif type(self.operation_id) is not str or not self.operation_id:
            _fail("INVALID_SOURCE_MAPPING", "EDITED mapping must bind an operation")


def vertical_text_diff_projection(value: RuntimeVerticalTextDiffV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalTextDiffV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "text diff must use the exact type")
    return {
        "operation_id": value.operation_id,
        "kind": value.kind.value,
        "container_path": list(value.container_path),
        "source_char_start": value.source_char_start,
        "source_char_end": value.source_char_end,
        "rendered_char_start": value.rendered_char_start,
        "rendered_char_end": value.rendered_char_end,
        "original_text": value.original_text,
        "rendered_text": value.rendered_text,
        "original_sha256": value.original_sha256,
        "rendered_sha256": value.rendered_sha256,
    }


def vertical_source_mapping_projection(
    value: RuntimeVerticalSourceMappingV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalSourceMappingV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "source mapping must use the exact type")
    return {
        "container_path": list(value.container_path),
        "source_char_start": value.source_char_start,
        "source_char_end": value.source_char_end,
        "rendered_char_start": value.rendered_char_start,
        "rendered_char_end": value.rendered_char_end,
        "kind": value.kind.value,
        "operation_id": value.operation_id,
    }


def _diff_projection(
    diffs: tuple[RuntimeVerticalTextDiffV1, ...],
    mappings: tuple[RuntimeVerticalSourceMappingV1, ...],
) -> dict[str, JsonValue]:
    if type(diffs) is not tuple or any(
        type(item) is not RuntimeVerticalTextDiffV1 for item in diffs
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "diff tuple is untrusted")
    if type(mappings) is not tuple or any(
        type(item) is not RuntimeVerticalSourceMappingV1 for item in mappings
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "mapping tuple is untrusted")
    return {
        "text_diffs": [vertical_text_diff_projection(item) for item in diffs],
        "source_mappings": [vertical_source_mapping_projection(item) for item in mappings],
    }


@dataclass(frozen=True, slots=True)
class RuntimeVerticalRenderResultV1:
    source_request_canonical_bytes: bytes
    candidate_request_canonical_bytes: bytes
    source_request_sha256: str
    candidate_request_sha256: str
    admitted_plan_sha256: str
    exact_diff_sha256: str
    text_diffs: tuple[RuntimeVerticalTextDiffV1, ...]
    source_mappings: tuple[RuntimeVerticalSourceMappingV1, ...]
    validation_checks: tuple[str, ...]
    execution_scope: RuntimeVerticalExecutionScope = RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
    schema_version: str = RUNTIME_VERTICAL_RENDER_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            RUNTIME_VERTICAL_RENDER_RESULT_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown vertical render-result schema")
        if type(self.source_request_canonical_bytes) is not bytes or (
            type(self.candidate_request_canonical_bytes) is not bytes
        ):
            _fail("UNTRUSTED_RUNTIME_TYPE", "render snapshots must use immutable bytes")
        source = _parse_canonical_bytes(self.source_request_canonical_bytes)
        candidate = _parse_canonical_bytes(self.candidate_request_canonical_bytes)
        if canonical_sha256(source) != self.source_request_sha256 or (
            canonical_sha256(candidate) != self.candidate_request_sha256
        ):
            _fail("REQUEST_HASH_MISMATCH", "render request hash differs")
        if type(self.admitted_plan_sha256) is not str or len(self.admitted_plan_sha256) != 64:
            _fail("INVALID_SHA256", "plan hash is invalid")
        exact_diff = _diff_projection(self.text_diffs, self.source_mappings)
        if canonical_sha256(cast(JsonValue, exact_diff)) != self.exact_diff_sha256:
            _fail("EXACT_DIFF_HASH_MISMATCH", "exact diff hash differs")
        if (
            type(self.validation_checks) is not tuple
            or not self.validation_checks
            or any(type(item) is not str for item in self.validation_checks)
        ):
            _fail("VALIDATION_CHECKS_MISSING", "render checks are invalid")
        if type(self.execution_scope) is not RuntimeVerticalExecutionScope:
            _fail("UNTRUSTED_RUNTIME_TYPE", "render execution scope is untrusted")

    @property
    def original_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.source_request_canonical_bytes)

    @property
    def candidate_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.candidate_request_canonical_bytes)

    @property
    def rendered_request(self) -> JsonValue:
        return self.candidate_request

    @property
    def rendered_request_sha256(self) -> str:
        return self.candidate_request_sha256

    @property
    def diffs(self) -> tuple[RuntimeVerticalTextDiffV1, ...]:
        return self.text_diffs

    @property
    def edit_applied(self) -> bool:
        return bool(self.text_diffs)


def _parse_canonical_bytes(value: bytes) -> JsonValue:
    if type(value) is not bytes:
        _fail("UNTRUSTED_RUNTIME_TYPE", "canonical snapshot must use bytes")
    import json

    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise R24ContractError("NON_CANONICAL_JSON", "snapshot JSON is malformed") from exc
    projected = snapshot_json_value(cast(JsonValue, decoded))
    if canonical_json_bytes(projected) != value:
        _fail("NON_CANONICAL_JSON", "snapshot bytes are not canonical")
    return projected


def vertical_render_result_projection(
    value: RuntimeVerticalRenderResultV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeVerticalRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use the exact type")
    return {
        "schema_version": value.schema_version,
        "execution_scope": value.execution_scope.value,
        "source_request_sha256": value.source_request_sha256,
        "candidate_request_sha256": value.candidate_request_sha256,
        "admitted_plan_sha256": value.admitted_plan_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "text_diff_count": len(value.text_diffs),
        "source_mapping_count": len(value.source_mappings),
        "edit_applied": value.edit_applied,
        "validation_checks": list(value.validation_checks),
    }


def vertical_render_result_sha256(value: RuntimeVerticalRenderResultV1) -> str:
    return canonical_sha256(cast(JsonValue, vertical_render_result_projection(value)))


_HISTORY_RECORD_TYPES = {
    CodecCapabilities,
    CorrectionAnchor,
    FrozenTextSlice,
    HistoryIR,
    HistoryRecord,
    HistoryRelationship,
    RecordCoordinates,
    RelatedContentRef,
    RequestRegion,
    SourceSpan,
    SourceVersionRef,
}
_HISTORY_ENUM_TYPES = {
    ArmKind,
    CapabilityLevel,
    CodecScope,
    CorrectionContextKind,
    CorrectionPlacement,
    HistoryFamily,
    OperationKind,
    RecordModality,
    RegionAvailability,
    RegionKind,
    RelatedContentKind,
    RelationshipKind,
    SpanRole,
}


def _validate_history_graph(value: object) -> None:
    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    visits = 0
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.remove(id(item))
            continue
        visits += 1
        if visits > _MAX_GRAPH_VISITS:
            _fail("GRAPH_NODE_LIMIT", "History IR graph visit budget exceeded")
        if depth > _MAX_GRAPH_DEPTH:
            _fail("GRAPH_DEPTH_LIMIT", "History IR graph depth exceeded")
        if (
            item is None
            or type(item) in {bool, int, float, str}
            or type(item) in _HISTORY_ENUM_TYPES
        ):
            continue
        if type(item) is tuple:
            children = cast(tuple[object, ...], item)
        elif type(item) is list:
            children = tuple(cast(list[object], item))
        elif type(item) is dict:
            mapping = cast(dict[object, object], item)
            if any(type(key) is not str for key in mapping):
                _fail("NON_CANONICAL_JSON", "History IR JSON object key is not text")
            children = tuple(mapping.values())
        elif type(item) in _HISTORY_RECORD_TYPES:
            children = tuple(getattr(item, field.name) for field in fields(cast(Any, item)))
        else:
            _fail("UNTRUSTED_RUNTIME_TYPE", "History IR graph contains a foreign node")
        identity = id(item)
        if identity in active:
            _fail("GRAPH_CYCLE", "History IR graph contains a cycle")
        active.add(identity)
        stack.append((item, depth, True))
        for child in reversed(children):
            stack.append((child, depth + 1, False))


@dataclass(frozen=True, slots=True)
class _ResolvedOperation:
    operation: RuntimeVerticalOperationV1
    record: HistoryRecord
    span: SourceSpan
    rendered_text: str


def _validate_span(source: JsonValue, span: SourceSpan) -> str:
    if type(span) is not SourceSpan or type(span.span_role) is not SpanRole:
        _fail("UNTRUSTED_RUNTIME_TYPE", "target span is untrusted")
    container = _get_at_path(source, span.container_path)
    if type(container) is not str:
        _fail("SPAN_CONTAINER_NOT_TEXT", "target span container is not exact text")
    if (
        type(span.char_start) is not int
        or type(span.char_end) is not int
        or span.char_start < 0
        or span.char_end <= span.char_start
        or span.char_end > len(container)
    ):
        _fail("INVALID_SOURCE_SPAN", "target span is out of bounds")
    selected = container[span.char_start : span.char_end]
    if selected != span.exact_text or _text_sha256(selected) != span.span_sha256:
        _fail("SOURCE_SPAN_DRIFT", "target span differs from request")
    if (
        len(container[: span.char_start].encode("utf-8")) != span.utf8_byte_start
        or len(container[: span.char_end].encode("utf-8")) != span.utf8_byte_end
    ):
        _fail("UTF8_OFFSET_DRIFT", "target UTF-8 offsets differ")
    return container


def _validate_non_history_overlap(span: SourceSpan, history_ir: HistoryIR) -> None:
    for region in history_ir.regions:
        if type(region) is not RequestRegion or type(region.kind) is not RegionKind:
            _fail("UNTRUSTED_RUNTIME_TYPE", "History IR region is untrusted")
        if region.kind is RegionKind.HISTORY or (
            region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
        ):
            continue
        for path in region.paths:
            path = _require_path(path, "region path")
            if _path_is_prefix(path, span.container_path) or _path_is_prefix(
                span.container_path, path
            ):
                _fail("NON_HISTORY_TARGET_FORBIDDEN", "target overlaps a non-history path")
        for text_slice in region.text_slices:
            if type(text_slice) is not FrozenTextSlice:
                _fail("UNTRUSTED_RUNTIME_TYPE", "non-history slice is untrusted")
            if (
                text_slice.container_path == span.container_path
                and span.char_start < text_slice.char_end
                and text_slice.char_start < span.char_end
            ):
                _fail("NON_HISTORY_TARGET_FORBIDDEN", "target overlaps non-history bytes")


def _resolve_plan(
    source: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeVerticalAdmittedPlanV1,
) -> tuple[_ResolvedOperation, ...]:
    _validate_history_graph(history_ir)
    if type(history_ir) is not HistoryIR or type(plan) is not RuntimeVerticalAdmittedPlanV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "renderer inputs require exact contracts")
    if canonical_sha256(source) != history_ir.raw_request_sha256 or (
        canonical_sha256(source) != plan.source_request_sha256
    ):
        _fail("SOURCE_REQUEST_BINDING_MISMATCH", "request, IR, and plan hashes differ")
    if (
        plan.host_id != history_ir.host_id
        or plan.history_family != history_ir.history_family.value
        or plan.history_codec_id != history_ir.codec_id
        or plan.history_codec_contract_version != history_ir.codec_contract_version
    ):
        _fail("HISTORY_IR_BINDING_MISMATCH", "plan binds another History IR")
    history_regions = {
        region.region_id: region
        for region in history_ir.regions
        if region.kind is RegionKind.HISTORY
    }
    if len(history_regions) != sum(
        region.kind is RegionKind.HISTORY for region in history_ir.regions
    ):
        _fail("DUPLICATE_HISTORY_REGION", "history region IDs repeat")
    resolved: list[_ResolvedOperation] = []
    occupied: list[tuple[JsonPath, int, int]] = []
    for operation in plan.operations:
        if type(operation) is not RuntimeVerticalOperationV1:
            _fail("UNTRUSTED_RUNTIME_TYPE", "plan operation is untrusted")
        records = [
            record
            for record in history_ir.records
            if record.record_id == operation.target_record_id
        ]
        if len(records) != 1:
            _fail("UNKNOWN_OR_DUPLICATE_RECORD", "target record must resolve exactly once")
        record = records[0]
        if type(record) is not HistoryRecord or record.region_id not in history_regions:
            _fail("NON_HISTORY_TARGET_FORBIDDEN", "target record is not in a history region")
        spans = [
            span
            for span in record.editable_spans
            if type(span) is SourceSpan
            and span.span_role is SpanRole.EDITABLE_CLAIM
            and span.span_sha256 == operation.target_span_sha256
        ]
        if len(spans) != 1:
            _fail("TARGET_SPAN_BINDING_MISMATCH", "editable span must resolve exactly once")
        span = spans[0]
        _validate_span(source, span)
        _validate_non_history_overlap(span, history_ir)
        for protected in record.protected_spans:
            _validate_span(source, protected)
            if (
                protected.container_path == span.container_path
                and span.char_start < protected.char_end
                and protected.char_start < span.char_end
            ):
                _fail("TARGET_PROTECTED_OVERLAP", "target overlaps protected bytes")
        for path, start, end in occupied:
            if path == span.container_path and span.char_start < end and start < span.char_end:
                _fail("OVERLAPPING_RUNTIME_EDITS", "runtime spans overlap")
        occupied.append((span.container_path, span.char_start, span.char_end))
        if operation.kind is RuntimeOperationKind.DROP:
            rendered_text = ""
        else:
            if operation.replacement_template is None:
                _fail("FIXED_REPLACEMENT_TEMPLATE_REQUIRED", "REPLACE template is absent")
            rendered_text = replacement_text_for_template(operation.replacement_template)
        resolved.append(
            _ResolvedOperation(
                operation=operation,
                record=record,
                span=span,
                rendered_text=rendered_text,
            )
        )
    return tuple(
        sorted(
            resolved,
            key=lambda item: (
                _path_key(item.span.container_path),
                item.span.char_start,
                item.operation.operation_id,
            ),
        )
    )


def _render_candidate(
    source: JsonValue,
    resolved: tuple[_ResolvedOperation, ...],
) -> tuple[
    JsonValue,
    tuple[RuntimeVerticalTextDiffV1, ...],
    tuple[RuntimeVerticalSourceMappingV1, ...],
]:
    candidate = snapshot_json_value(source)
    grouped: dict[JsonPath, list[_ResolvedOperation]] = defaultdict(list)
    for item in resolved:
        grouped[item.span.container_path].append(item)
    diffs: list[RuntimeVerticalTextDiffV1] = []
    mappings: list[RuntimeVerticalSourceMappingV1] = []
    for path in sorted(grouped, key=_path_key):
        source_text = _get_at_path(source, path)
        if type(source_text) is not str:
            _fail("SPAN_CONTAINER_NOT_TEXT", "target container is not text")
        chunks: list[str] = []
        source_cursor = 0
        rendered_cursor = 0
        for item in sorted(grouped[path], key=lambda value: value.span.char_start):
            span = item.span
            copied = source_text[source_cursor : span.char_start]
            if copied:
                chunks.append(copied)
                mappings.append(
                    RuntimeVerticalSourceMappingV1(
                        container_path=path,
                        source_char_start=source_cursor,
                        source_char_end=span.char_start,
                        rendered_char_start=rendered_cursor,
                        rendered_char_end=rendered_cursor + len(copied),
                        kind=RuntimeVerticalMappingKind.COPIED,
                        operation_id=None,
                    )
                )
                rendered_cursor += len(copied)
            replacement = item.rendered_text
            replacement_start = rendered_cursor
            chunks.append(replacement)
            rendered_cursor += len(replacement)
            mappings.append(
                RuntimeVerticalSourceMappingV1(
                    container_path=path,
                    source_char_start=span.char_start,
                    source_char_end=span.char_end,
                    rendered_char_start=replacement_start,
                    rendered_char_end=rendered_cursor,
                    kind=RuntimeVerticalMappingKind.EDITED,
                    operation_id=item.operation.operation_id,
                )
            )
            diffs.append(
                RuntimeVerticalTextDiffV1(
                    operation_id=item.operation.operation_id,
                    kind=item.operation.kind,
                    container_path=path,
                    source_char_start=span.char_start,
                    source_char_end=span.char_end,
                    rendered_char_start=replacement_start,
                    rendered_char_end=rendered_cursor,
                    original_text=span.exact_text,
                    rendered_text=replacement,
                    original_sha256=span.span_sha256,
                    rendered_sha256=_text_sha256(replacement),
                )
            )
            source_cursor = span.char_end
        tail = source_text[source_cursor:]
        if tail:
            chunks.append(tail)
            mappings.append(
                RuntimeVerticalSourceMappingV1(
                    container_path=path,
                    source_char_start=source_cursor,
                    source_char_end=len(source_text),
                    rendered_char_start=rendered_cursor,
                    rendered_char_end=rendered_cursor + len(tail),
                    kind=RuntimeVerticalMappingKind.COPIED,
                    operation_id=None,
                )
            )
        _set_at_path(candidate, path, "".join(chunks))
    return candidate, tuple(diffs), tuple(mappings)


def _validate_only_target_paths_changed(
    source: JsonValue,
    candidate: JsonValue,
    allowed_paths: frozenset[JsonPath],
) -> None:
    stack: list[tuple[JsonValue, JsonValue, JsonPath]] = [(source, candidate, ())]
    while stack:
        left, right, path = stack.pop()
        if path in allowed_paths:
            continue
        if type(left) is not type(right):
            _fail("NON_HISTORY_BYTES_CHANGED", "candidate JSON type changed")
        if type(left) is dict:
            left_map = left
            right_map = cast(dict[str, JsonValue], right)
            if tuple(left_map) != tuple(right_map):
                _fail("EXISTING_BLOCK_ORDER_CHANGED", "object key order changed")
            for key in reversed(tuple(left_map)):
                stack.append((left_map[key], right_map[key], (*path, key)))
        elif type(left) is list:
            left_list = left
            right_list = cast(list[JsonValue], right)
            if len(left_list) != len(right_list):
                _fail("EXISTING_BLOCK_ORDER_CHANGED", "array length changed")
            for index in range(len(left_list) - 1, -1, -1):
                stack.append((left_list[index], right_list[index], (*path, index)))
        elif left != right:
            _fail("NON_HISTORY_BYTES_CHANGED", "a non-target leaf changed")


def _validate_image_paths(source: JsonValue, candidate: JsonValue, history_ir: HistoryIR) -> None:
    paths: set[JsonPath] = set()
    for record in history_ir.records:
        for related in record.related_content:
            if type(related) is RelatedContentRef and related.kind is RelatedContentKind.IMAGE:
                paths.add(related.path)
    for region in history_ir.regions:
        if region.kind is RegionKind.CURRENT_OBSERVATION:
            paths.update(region.paths)
    for path in paths:
        if canonical_json_bytes(_get_at_path(source, path)) != canonical_json_bytes(
            _get_at_path(candidate, path)
        ):
            _fail("CURRENT_IMAGE_BYTES_CHANGED", "image/observation bytes changed")


def restore_vertical_original(result: RuntimeVerticalRenderResultV1) -> JsonValue:
    if type(result) is not RuntimeVerticalRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use the exact type")
    restored = result.candidate_request
    grouped: dict[JsonPath, list[RuntimeVerticalTextDiffV1]] = defaultdict(list)
    for diff in result.text_diffs:
        grouped[diff.container_path].append(diff)
    for path, diffs in grouped.items():
        candidate_text = _get_at_path(restored, path)
        if type(candidate_text) is not str:
            _fail("SPAN_CONTAINER_NOT_TEXT", "candidate diff container is not text")
        value = candidate_text
        for diff in sorted(diffs, key=lambda item: item.rendered_char_start, reverse=True):
            if value[diff.rendered_char_start : diff.rendered_char_end] != diff.rendered_text:
                _fail("RENDER_DIFF_BINDING_MISMATCH", "candidate replacement differs from diff")
            value = (
                value[: diff.rendered_char_start]
                + diff.original_text
                + value[diff.rendered_char_end :]
            )
        _set_at_path(restored, path, value)
    if canonical_json_bytes(restored) != result.source_request_canonical_bytes:
        _fail("NON_REVERSIBLE_MAPPING", "candidate cannot restore exact source")
    return restored


def validate_vertical_render_result(
    source_request: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeVerticalAdmittedPlanV1,
    result: RuntimeVerticalRenderResultV1,
) -> tuple[str, ...]:
    if type(result) is not RuntimeVerticalRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use the exact type")
    source_bytes = canonical_json_bytes(source_request)
    plan_snapshot = snapshot_vertical_plan(plan)
    resolved = _resolve_plan(snapshot_json_value(source_request), history_ir, plan_snapshot)
    expected, diffs, mappings = _render_candidate(snapshot_json_value(source_request), resolved)
    if result.source_request_canonical_bytes != source_bytes or (
        result.candidate_request_canonical_bytes != canonical_json_bytes(expected)
    ):
        _fail("CANDIDATE_REQUEST_MISMATCH", "render snapshots differ from independent render")
    if result.admitted_plan_sha256 != vertical_plan_sha256(plan_snapshot):
        _fail("RUNTIME_PLAN_HASH_MISMATCH", "render binds another admitted plan")
    if result.execution_scope is not plan_snapshot.execution_scope:
        _fail("EXECUTION_SCOPE_MISMATCH", "render and admitted plan scopes differ")
    if _diff_projection(result.text_diffs, result.source_mappings) != _diff_projection(
        diffs, mappings
    ):
        _fail("EXACT_DIFF_MISMATCH", "render diff differs from independent recomputation")
    restored = restore_vertical_original(result)
    if canonical_json_bytes(restored) != source_bytes:
        _fail("NON_REVERSIBLE_MAPPING", "restored request differs from source")
    allowed_paths = frozenset(item.span.container_path for item in resolved)
    _validate_only_target_paths_changed(
        snapshot_json_value(source_request), expected, allowed_paths
    )
    _validate_image_paths(snapshot_json_value(source_request), expected, history_ir)
    if result.validation_checks != _RENDER_CHECKS:
        _fail("RENDER_VALIDATION_CHECKS_MISMATCH", "render check census differs")
    if canonical_json_bytes(source_request) != source_bytes:
        _fail("CALLER_INPUT_MUTATED", "render validation mutated caller request")
    return _RENDER_CHECKS


def render_vertical_admitted_plan(
    source_request: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeVerticalAdmittedPlanV1,
) -> RuntimeVerticalRenderResultV1:
    """Build a candidate without itself authorizing transport or actions."""

    source_bytes = canonical_json_bytes(source_request)
    source = snapshot_json_value(source_request)
    _validate_history_graph(history_ir)
    try:
        history_snapshot = deepcopy(history_ir)
    except (TypeError, ValueError, RecursionError) as exc:
        raise R24ContractError("GRAPH_SNAPSHOT_FAILED", "History IR detach failed") from exc
    plan_snapshot = snapshot_vertical_plan(plan)
    resolved = _resolve_plan(source, history_snapshot, plan_snapshot)
    candidate, diffs, mappings = _render_candidate(source, resolved)
    exact_diff = _diff_projection(diffs, mappings)
    result = RuntimeVerticalRenderResultV1(
        source_request_canonical_bytes=source_bytes,
        candidate_request_canonical_bytes=canonical_json_bytes(candidate),
        source_request_sha256=canonical_sha256(source),
        candidate_request_sha256=canonical_sha256(candidate),
        admitted_plan_sha256=vertical_plan_sha256(plan_snapshot),
        exact_diff_sha256=canonical_sha256(cast(JsonValue, exact_diff)),
        text_diffs=diffs,
        source_mappings=mappings,
        validation_checks=_RENDER_CHECKS,
        execution_scope=plan_snapshot.execution_scope,
    )
    validate_vertical_render_result(source, history_snapshot, plan_snapshot, result)
    if canonical_json_bytes(source_request) != source_bytes:
        _fail("CALLER_INPUT_MUTATED", "renderer mutated caller request")
    return result


def snapshot_vertical_render_result(
    value: RuntimeVerticalRenderResultV1,
) -> RuntimeVerticalRenderResultV1:
    if type(value) is not RuntimeVerticalRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use the exact type")
    diffs = tuple(
        RuntimeVerticalTextDiffV1(
            operation_id=item.operation_id,
            kind=item.kind,
            container_path=tuple(item.container_path),
            source_char_start=item.source_char_start,
            source_char_end=item.source_char_end,
            rendered_char_start=item.rendered_char_start,
            rendered_char_end=item.rendered_char_end,
            original_text=item.original_text,
            rendered_text=item.rendered_text,
            original_sha256=item.original_sha256,
            rendered_sha256=item.rendered_sha256,
        )
        for item in value.text_diffs
    )
    mappings = tuple(
        RuntimeVerticalSourceMappingV1(
            container_path=tuple(item.container_path),
            source_char_start=item.source_char_start,
            source_char_end=item.source_char_end,
            rendered_char_start=item.rendered_char_start,
            rendered_char_end=item.rendered_char_end,
            kind=item.kind,
            operation_id=item.operation_id,
        )
        for item in value.source_mappings
    )
    return RuntimeVerticalRenderResultV1(
        source_request_canonical_bytes=bytes(value.source_request_canonical_bytes),
        candidate_request_canonical_bytes=bytes(value.candidate_request_canonical_bytes),
        source_request_sha256=value.source_request_sha256,
        candidate_request_sha256=value.candidate_request_sha256,
        admitted_plan_sha256=value.admitted_plan_sha256,
        exact_diff_sha256=value.exact_diff_sha256,
        text_diffs=diffs,
        source_mappings=mappings,
        validation_checks=tuple(value.validation_checks),
        execution_scope=value.execution_scope,
        schema_version=value.schema_version,
    )


# Naming aliases for call-site symmetry with the accepted R2.2 renderer.
render_runtime_vertical_plan = render_vertical_admitted_plan
restore_runtime_vertical_original = restore_vertical_original
validate_runtime_vertical_render_result = validate_vertical_render_result


__all__ = [
    "RUNTIME_VERTICAL_RENDER_RESULT_SCHEMA_VERSION",
    "RuntimeVerticalMappingKind",
    "RuntimeVerticalRenderResultV1",
    "RuntimeVerticalSourceMappingV1",
    "RuntimeVerticalTextDiffV1",
    "render_runtime_vertical_plan",
    "render_vertical_admitted_plan",
    "restore_runtime_vertical_original",
    "restore_vertical_original",
    "snapshot_vertical_render_result",
    "validate_runtime_vertical_render_result",
    "validate_vertical_render_result",
    "vertical_render_result_projection",
    "vertical_render_result_sha256",
    "vertical_source_mapping_projection",
    "vertical_text_diff_projection",
]
