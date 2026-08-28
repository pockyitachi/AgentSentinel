"""Pure, family-bound G1.5 codecs for exact captured request structures.

These codecs parse host syntax, not model output semantics.  Editable spans are
supplied by an already curated G1.2 transformation binding.  Exact coordinates,
text, and hashes are rechecked on every extraction; stale, ambiguous,
overlapping, or protocol-adjacent bindings fail closed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CapabilityLevel,
    CodecCapabilities,
    CodecScope,
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    ExecutionMode,
    FailurePolicy,
    FrozenTextSlice,
    HistoryFamily,
    HistoryIR,
    HistoryRecord,
    HistoryRelationship,
    JsonPath,
    JsonValue,
    OperationKind,
    PortableContractError,
    RecordCoordinates,
    RecordModality,
    RegionAvailability,
    RegionKind,
    RelatedContentKind,
    RelatedContentRef,
    RelationshipKind,
    RenderResult,
    RequestRegion,
    SourceSpan,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    stable_id,
    text_sha256,
)
from mobile_world.offline.causal_replay.core import render_request, validate_history_ir

_QWEN_QUERY_MARKER = "\nThe user query: "
_QWEN_PROGRESS_MARKER = (
    "\nTask progress (You have done the following operation on the current device): "
)
_QWEN_STEP = re.compile(r"Step ([1-9][0-9]*): ")
_QWEN_TOOL_RESULT_PREFIX = "; Tool call result: "
_QWEN_TOOL_RESULT_OPEN = f"{_QWEN_TOOL_RESULT_PREFIX}<tool_response>"
_QWEN_TOOL_RESULT_CLOSE = "</tool_response>"
_QWEN_ASK_RESPONSE_PREFIX = "; Ask user response: "

_THINK_OPEN = "<thinking>"
_THINK_CLOSE = "</thinking>"
_THINK_LEGACY_CLOSE = "</think>"
_TOOL_OPEN = "<tool_call>"
_TOOL_CLOSE = "</tool_call>"


@dataclass(frozen=True)
class CuratedSpanBinding:
    """An exact G1.2-curated target locator consumed without reinterpretation."""

    binding_id: str
    source_request_sha256: str
    container_path: JsonPath
    char_start: int
    char_end: int
    utf8_byte_start: int
    utf8_byte_end: int
    exact_text: str
    span_sha256: str
    span_role: SpanRole

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "binding_id": self.binding_id,
            "source_request_sha256": self.source_request_sha256,
            "container_path": list(self.container_path),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "utf8_byte_start": self.utf8_byte_start,
            "utf8_byte_end": self.utf8_byte_end,
            "exact_text": self.exact_text,
            "span_sha256": self.span_sha256,
            "span_role": self.span_role.value,
        }

    @classmethod
    def from_text(
        cls,
        *,
        binding_id: str,
        source_request_sha256: str,
        container_path: JsonPath,
        container_text: str,
        char_start: int,
        char_end: int,
        span_role: SpanRole = SpanRole.EDITABLE_CLAIM,
    ) -> CuratedSpanBinding:
        span = SourceSpan.from_text(
            container_path=container_path,
            container_text=container_text,
            char_start=char_start,
            char_end=char_end,
            span_role=span_role,
            claim_id=None,
        )
        return cls(
            binding_id=binding_id,
            source_request_sha256=source_request_sha256,
            container_path=container_path,
            char_start=span.char_start,
            char_end=span.char_end,
            utf8_byte_start=span.utf8_byte_start,
            utf8_byte_end=span.utf8_byte_end,
            exact_text=span.exact_text,
            span_sha256=span.span_sha256,
            span_role=span_role,
        )

    def validate_against(self, request: JsonValue) -> str:
        if not self.binding_id:
            raise PortableContractError("TARGET_BINDING_ID_MISSING", "binding ID is required")
        if canonical_sha256(request) != self.source_request_sha256:
            raise PortableContractError(
                "TARGET_BINDING_REQUEST_MISMATCH",
                "curated target belongs to another captured request",
            )
        if self.span_role not in {SpanRole.EDITABLE_CLAIM, SpanRole.BENIGN_SHAM}:
            raise PortableContractError(
                "TARGET_BINDING_ROLE_INVALID",
                "curated target must be EDITABLE_CLAIM or BENIGN_SHAM",
            )
        try:
            value = get_at_path(request, self.container_path)
        except PortableContractError as exc:
            raise PortableContractError(
                "TARGET_BINDING_PATH_MISSING",
                "curated target path cannot be resolved",
            ) from exc
        if not isinstance(value, str):
            raise PortableContractError(
                "TARGET_BINDING_CONTAINER_NOT_TEXT",
                "curated target container is not text",
            )
        if (
            type(self.char_start) is not int
            or type(self.char_end) is not int
            or not 0 <= self.char_start < self.char_end <= len(value)
        ):
            raise PortableContractError(
                "TARGET_BINDING_COORDINATE_MISMATCH",
                "curated target coordinates are invalid",
            )
        selected = value[self.char_start : self.char_end]
        byte_start = len(value[: self.char_start].encode("utf-8"))
        byte_end = len(value[: self.char_end].encode("utf-8"))
        if (
            selected != self.exact_text
            or text_sha256(selected) != self.span_sha256
            or byte_start != self.utf8_byte_start
            or byte_end != self.utf8_byte_end
        ):
            raise PortableContractError(
                "TARGET_BINDING_STALE",
                "curated target text, digest, or UTF-8 coordinates drifted",
            )
        return value


@dataclass(frozen=True)
class _RecordSpec:
    record_key: str
    record_class: str
    region_id: str
    role: str
    author: str
    modality: RecordModality
    container_path: JsonPath
    char_start: int
    char_end: int
    editable_start: int | None
    editable_end: int | None
    message_index: int
    content_block_index: int | None
    representation_record_index: int
    provenance: dict[str, JsonValue]
    protected_spans: tuple[SourceSpan, ...]
    related_content: tuple[tuple[JsonPath, RelatedContentKind], ...] = ()


def _require_object(value: JsonValue, *, code: str, message: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise PortableContractError(code, message)
    return value


def _require_messages(request: JsonValue) -> tuple[dict[str, JsonValue], list[JsonValue]]:
    root = _require_object(
        request,
        code="CAPTURED_REQUEST_NOT_OBJECT",
        message="captured application request must be an object",
    )
    raw = root.get("messages")
    if not isinstance(raw, list):
        raise PortableContractError("MESSAGES_MISSING", "captured request needs a messages array")
    messages: list[JsonValue] = raw
    return root, messages


def _message(messages: list[JsonValue], index: int) -> dict[str, JsonValue]:
    try:
        value = messages[index]
    except IndexError as exc:
        raise PortableContractError("MESSAGE_MISSING", "required host message is missing") from exc
    return _require_object(
        value, code="MESSAGE_NOT_OBJECT", message="host message must be an object"
    )


def _content_blocks(message: dict[str, JsonValue], *, label: str) -> list[JsonValue]:
    value = message.get("content")
    if not isinstance(value, list) or not value:
        raise PortableContractError("CONTENT_BLOCKS_INVALID", f"{label} content must be non-empty")
    return value


def _typed_block(value: JsonValue, *, label: str) -> dict[str, JsonValue]:
    block = _require_object(
        value, code="CONTENT_BLOCK_INVALID", message=f"{label} must be an object"
    )
    if not isinstance(block.get("type"), str) or not block["type"]:
        raise PortableContractError("CONTENT_BLOCK_INVALID", f"{label} needs an exact type")
    return block


def _require_image_url_block(block: dict[str, JsonValue], *, label: str) -> None:
    image_url = block.get("image_url")
    if not isinstance(image_url, dict):
        raise PortableContractError(
            "CURRENT_IMAGE_INVALID", f"{label} image_url payload must be an object"
        )
    url = image_url.get("url")
    if not isinstance(url, str) or not url:
        raise PortableContractError(
            "CURRENT_IMAGE_INVALID", f"{label} image_url payload needs a non-empty URL"
        )


def _text_slice(path: JsonPath, text: str, start: int, end: int) -> FrozenTextSlice:
    span = SourceSpan.from_text(
        container_path=path,
        container_text=text,
        char_start=start,
        char_end=end,
        span_role=SpanRole.RECORD_EXTENT,
    )
    return FrozenTextSlice(
        container_path=path,
        char_start=span.char_start,
        char_end=span.char_end,
        utf8_byte_start=span.utf8_byte_start,
        utf8_byte_end=span.utf8_byte_end,
        exact_text=span.exact_text,
        span_sha256=span.span_sha256,
    )


def _region(
    *,
    host_id: str,
    family: HistoryFamily,
    kind: RegionKind,
    ordinal: int,
    request: JsonValue,
    paths: tuple[JsonPath, ...] = (),
    text_slices: tuple[FrozenTextSlice, ...] = (),
    availability: RegionAvailability = RegionAvailability.PRESENT,
    absence_reason: str | None = None,
) -> RequestRegion:
    projection: list[JsonValue] = [get_at_path(request, path) for path in paths]
    projection.extend(item.exact_text for item in text_slices)
    source_sha = canonical_sha256(projection)
    return RequestRegion(
        region_id=stable_id(
            "region",
            {
                "host_id": host_id,
                "history_family": family.value,
                "kind": kind.value,
                "ordinal": ordinal,
                "source_sha256": source_sha,
            },
        ),
        kind=kind,
        paths=paths,
        text_slices=text_slices,
        source_sha256=source_sha,
        availability=availability,
        absence_reason=absence_reason,
        preserve_exact=True,
    )


def _span_overlap(left: CuratedSpanBinding, right: CuratedSpanBinding) -> bool:
    return (
        left.container_path == right.container_path
        and left.char_start < right.char_end
        and right.char_start < left.char_end
    )


def _binding_sort_key(binding: CuratedSpanBinding) -> tuple[object, ...]:
    path_key = tuple(
        (0, token) if isinstance(token, str) else (1, token) for token in binding.container_path
    )
    return (
        path_key,
        binding.char_start,
        binding.char_end,
        binding.span_sha256,
        binding.binding_id,
    )


def _source_span(
    *,
    path: JsonPath,
    text: str,
    start: int,
    end: int,
    role: SpanRole,
) -> SourceSpan:
    return SourceSpan.from_text(
        container_path=path,
        container_text=text,
        char_start=start,
        char_end=end,
        span_role=role,
    )


class _CapturedHistoryCodec:
    _codec_id: str
    _host_id: str
    _family: HistoryFamily

    def __init__(self, curated_bindings: tuple[CuratedSpanBinding, ...] = ()) -> None:
        raw_values = cast(tuple[object, ...], tuple(curated_bindings))
        validated_bindings: list[CuratedSpanBinding] = []
        for raw_item in raw_values:
            if not isinstance(raw_item, CuratedSpanBinding):
                raise PortableContractError(
                    "TARGET_BINDING_OBJECT_INVALID",
                    "curated binding catalog contains a non-binding value",
                )
            item = raw_item
            raw_binding_id = cast(object, item.binding_id)
            if not isinstance(raw_binding_id, str) or not raw_binding_id:
                raise PortableContractError(
                    "TARGET_BINDING_ID_MISSING",
                    "curated binding ID must be non-empty text",
                )
            raw_path = cast(object, item.container_path)
            if (
                not isinstance(raw_path, tuple)
                or not raw_path
                or any(
                    not (
                        (isinstance(token, str) and bool(token))
                        or (isinstance(token, int) and not isinstance(token, bool) and token >= 0)
                    )
                    for token in raw_path
                )
            ):
                raise PortableContractError(
                    "TARGET_BINDING_PATH_NON_CANONICAL",
                    "curated binding path must be a non-empty immutable JSON path",
                )
            raw_request_digest = cast(object, item.source_request_sha256)
            if (
                not isinstance(raw_request_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", raw_request_digest) is None
            ):
                raise PortableContractError(
                    "TARGET_BINDING_REQUEST_DIGEST_INVALID",
                    "curated binding source request digest must be lowercase SHA-256",
                )
            raw_coordinates = cast(
                tuple[object, object, object, object],
                (
                    item.char_start,
                    item.char_end,
                    item.utf8_byte_start,
                    item.utf8_byte_end,
                ),
            )
            if (
                any(type(value) is not int for value in raw_coordinates)
                or item.char_start < 0
                or item.char_start >= item.char_end
                or item.utf8_byte_start < 0
                or item.utf8_byte_start >= item.utf8_byte_end
            ):
                raise PortableContractError(
                    "TARGET_BINDING_COORDINATE_INVALID",
                    "curated binding coordinates must be non-empty non-negative integer ranges",
                )
            raw_exact_text = cast(object, item.exact_text)
            if not isinstance(raw_exact_text, str) or not raw_exact_text:
                raise PortableContractError(
                    "TARGET_BINDING_TEXT_INVALID",
                    "curated binding exact text must be non-empty",
                )
            raw_span_digest = cast(object, item.span_sha256)
            if (
                not isinstance(raw_span_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", raw_span_digest) is None
            ):
                raise PortableContractError(
                    "TARGET_BINDING_SPAN_DIGEST_INVALID",
                    "curated binding span digest must be lowercase SHA-256",
                )
            raw_span_role = cast(object, item.span_role)
            if (
                raw_span_role is not SpanRole.EDITABLE_CLAIM
                and raw_span_role is not SpanRole.BENIGN_SHAM
            ):
                raise PortableContractError(
                    "TARGET_BINDING_ROLE_INVALID",
                    "curated target must be EDITABLE_CLAIM or BENIGN_SHAM",
                )
            validated_bindings.append(item)
        raw_bindings = tuple(validated_bindings)
        self._curated_bindings = tuple(sorted(raw_bindings, key=_binding_sort_key))
        ids = [item.binding_id for item in self._curated_bindings]
        if len(ids) != len(set(ids)):
            raise PortableContractError(
                "DUPLICATE_TARGET_BINDING", "curated binding IDs must be unique"
            )
        request_hashes = {item.source_request_sha256 for item in self._curated_bindings}
        if len(request_hashes) > 1:
            raise PortableContractError(
                "TARGET_BINDING_REQUEST_SET_MISMATCH",
                "curated bindings must belong to one captured request",
            )
        for index, left in enumerate(self._curated_bindings):
            for right in self._curated_bindings[index + 1 :]:
                if _span_overlap(left, right):
                    raise PortableContractError(
                        "OVERLAPPING_TARGET_BINDINGS", "curated target spans overlap"
                    )
        self._binding_catalog_sha256 = canonical_sha256(
            {
                "codec_id": self._codec_id,
                "codec_contract_version": self.contract_version,
                "bindings": [item.to_dict() for item in self._curated_bindings],
            }
        )
        self._capabilities = CodecCapabilities(
            codec_id=self._codec_id,
            contract_version=self.contract_version,
            history_family=self._family,
            level=CapabilityLevel.VALIDITY_TRANSFORMATION,
            scope=CodecScope.LIVE,
            supported_operations=(OperationKind.DROP, OperationKind.REPLACE),
            supported_arms=tuple(ArmKind),
            preserves_roles=True,
            preserves_order=True,
            preserves_multimodal_blocks=True,
            preserves_tool_adjacency=True,
            preserves_protocol_shell=True,
            # CPU checkpoint only.  G1.5 live smoke is a separately sealed GPU item.
            live_ready=False,
            opaque_or_server_managed=False,
        )

    @property
    def codec_id(self) -> str:
        return self._codec_id

    @property
    def contract_version(self) -> str:
        return "v1"

    @property
    def history_family(self) -> HistoryFamily:
        return self._family

    @property
    def capabilities(self) -> CodecCapabilities:
        return self._capabilities

    @property
    def host_id(self) -> str:
        return self._host_id

    def render(
        self,
        application_request: JsonValue,
        ir: HistoryIR,
        plan: TransformationPlan,
        *,
        execution_mode: ExecutionMode,
        failure_policy: FailurePolicy,
    ) -> RenderResult:
        if (
            ir.codec_id != self.codec_id
            or ir.codec_contract_version != self.contract_version
            or ir.history_family is not self.history_family
            or ir.capabilities != self.capabilities
        ):
            raise PortableContractError("CODEC_IR_MISMATCH", "IR belongs to another codec")
        return render_request(
            application_request,
            ir,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )

    def _copy_bound_request(self, application_request: JsonValue) -> JsonValue:
        request = copy_json(application_request)
        request_sha256 = canonical_sha256(request)
        if any(item.source_request_sha256 != request_sha256 for item in self._curated_bindings):
            raise PortableContractError(
                "TARGET_BINDING_REQUEST_MISMATCH",
                "curated target belongs to another captured request",
            )
        return request

    def _correction_anchor(
        self,
        request: JsonValue,
        *,
        current_region: RequestRegion,
        message_index: int,
        reference_index: int,
    ) -> CorrectionAnchor:
        container_path: JsonPath = ("messages", message_index, "content")
        host_path: JsonPath = ("messages", message_index)
        role_path: JsonPath = ("messages", message_index, "role")
        reference_path: JsonPath = (*container_path, reference_index)
        container = get_at_path(request, container_path)
        if not isinstance(container, list):
            raise PortableContractError(
                "CORRECTION_ANCHOR_NOT_LIST", "current observation content must be an array"
            )
        return CorrectionAnchor(
            container_path=container_path,
            insert_index=reference_index,
            source_container_sha256=canonical_sha256(container),
            owner_region_id=current_region.region_id,
            host_context_path=host_path,
            host_context_sha256=canonical_sha256(get_at_path(request, host_path)),
            role_path=role_path,
            expected_role="user",
            reference_path=reference_path,
            reference_sha256=canonical_sha256(get_at_path(request, reference_path)),
            placement=CorrectionPlacement.BEFORE,
            context_kind=CorrectionContextKind.TEXT_CONTENT_BLOCK,
            visible_prefix="SENTINEL correction: ",
            visible_suffix="",
        )

    def _finish_ir(
        self,
        request: JsonValue,
        *,
        regions: tuple[RequestRegion, ...],
        specs: tuple[_RecordSpec, ...],
        correction_anchor: CorrectionAnchor,
    ) -> HistoryIR:
        for binding in self._curated_bindings:
            binding.validate_against(request)

        assignments: dict[int, list[CuratedSpanBinding]] = {
            index: [] for index in range(len(specs))
        }
        for binding in self._curated_bindings:
            candidates = [
                index
                for index, spec in enumerate(specs)
                if spec.container_path == binding.container_path
                and spec.editable_start is not None
                and spec.editable_end is not None
                and spec.char_start <= binding.char_start
                and binding.char_end <= spec.char_end
                and spec.editable_start <= binding.char_start
                and binding.char_end <= spec.editable_end
            ]
            if len(candidates) != 1:
                code = (
                    "TARGET_BINDING_AMBIGUOUS"
                    if len(candidates) > 1
                    else "TARGET_BINDING_OUTSIDE_EDITABLE_HISTORY"
                )
                raise PortableContractError(
                    code, "curated target must resolve to one structural history claim"
                )
            assignments[candidates[0]].append(binding)

        records: list[HistoryRecord] = []
        source_hash = canonical_sha256(request)
        for spec_index, spec in enumerate(specs):
            container = get_at_path(request, spec.container_path)
            if not isinstance(container, str):
                raise PortableContractError(
                    "RECORD_CONTAINER_NOT_TEXT", "structural history record is not text"
                )
            extent = _source_span(
                path=spec.container_path,
                text=container,
                start=spec.char_start,
                end=spec.char_end,
                role=SpanRole.RECORD_EXTENT,
            )
            record_id = stable_id(
                "record",
                {
                    "host_id": self.host_id,
                    "history_family": self.history_family.value,
                    "source_request_sha256": source_hash,
                    "container_path": list(spec.container_path),
                    "char_start": extent.char_start,
                    "char_end": extent.char_end,
                    "span_sha256": extent.span_sha256,
                    "record_class": spec.record_class,
                    "role": spec.role,
                    "author": spec.author,
                    "modality": spec.modality.value,
                },
            )
            editable: list[SourceSpan] = []
            record_bindings = tuple(sorted(assignments[spec_index], key=_binding_sort_key))
            for binding in record_bindings:
                editable.append(
                    SourceSpan(
                        container_path=binding.container_path,
                        char_start=binding.char_start,
                        char_end=binding.char_end,
                        utf8_byte_start=binding.utf8_byte_start,
                        utf8_byte_end=binding.utf8_byte_end,
                        exact_text=binding.exact_text,
                        span_sha256=binding.span_sha256,
                        span_role=binding.span_role,
                        claim_id=stable_id(
                            "claim",
                            {
                                "record_id": record_id,
                                "container_path": list(binding.container_path),
                                "char_start": binding.char_start,
                                "char_end": binding.char_end,
                                "span_sha256": binding.span_sha256,
                            },
                        ),
                    )
                )
            for target in editable:
                for protected in spec.protected_spans:
                    if (
                        target.container_path == protected.container_path
                        and target.char_start < protected.char_end
                        and protected.char_start < target.char_end
                    ):
                        raise PortableContractError(
                            "TARGET_BINDING_PROTOCOL_OVERLAP",
                            "curated target overlaps a protected wrapper/result",
                        )

            relationships: list[HistoryRelationship] = []
            related_refs: list[RelatedContentRef] = []
            for relation_index, (path, kind) in enumerate(spec.related_content):
                relationships.append(
                    HistoryRelationship(
                        relationship_id=stable_id(
                            "relationship",
                            {
                                "source_record_id": record_id,
                                "index": relation_index,
                                "kind": RelationshipKind.ACTION_OBSERVATION.value,
                                "target_record_id": None,
                                "target_path": list(path),
                                "target_version_id": None,
                            },
                        ),
                        kind=RelationshipKind.ACTION_OBSERVATION,
                        source_record_id=record_id,
                        target_path=path,
                    )
                )
                related_refs.append(
                    RelatedContentRef(
                        path=path,
                        kind=kind,
                        value_sha256=canonical_sha256(get_at_path(request, path)),
                        blob_sha256=None,
                    )
                )
            records.append(
                HistoryRecord(
                    record_id=record_id,
                    record_key=spec.record_key,
                    record_class=spec.record_class,
                    region_id=spec.region_id,
                    role=spec.role,
                    author=spec.author,
                    modality=spec.modality,
                    coordinates=RecordCoordinates(
                        request_path=spec.container_path,
                        message_index=spec.message_index,
                        content_block_index=spec.content_block_index,
                        representation_record_index=spec.representation_record_index,
                    ),
                    record_sha256=extent.span_sha256,
                    source_span=extent,
                    editable_spans=tuple(editable),
                    protected_spans=spec.protected_spans,
                    write_time=None,
                    exposure_time="CAPTURED_REQUEST_PRE_SEND",
                    provenance={
                        **spec.provenance,
                        "curated_binding_ids": [item.binding_id for item in record_bindings],
                        "curated_binding_catalog_sha256": self._binding_catalog_sha256,
                        "binding_source_request_sha256": source_hash,
                    },
                    correction_anchors=(correction_anchor,),
                    relationships=tuple(relationships),
                    related_content=tuple(related_refs),
                )
            )

        ir = HistoryIR(
            host_id=self.host_id,
            history_family=self.history_family,
            codec_id=self.codec_id,
            codec_contract_version=self.contract_version,
            raw_request_sha256=source_hash,
            regions=regions,
            records=tuple(records),
            source_versions=(),
            capabilities=self.capabilities,
            warnings=("CPU_CHECKPOINT_LIVE_SMOKE_DEFERRED",),
        )
        validate_history_ir(request, ir)
        return ir


class QwenFlatProgressHistoryCodec(_CapturedHistoryCodec):
    """Exact codec for the captured Qwen flat ``Task progress`` request family."""

    _codec_id = "mobileworld.g1.history-codec.qwen-flat-progress"
    _host_id = "mobileworld.qwen3vl.actor"
    _family = HistoryFamily.FLAT_PROGRESS

    def extract(self, application_request: JsonValue) -> HistoryIR:
        request = self._copy_bound_request(application_request)
        root, messages = _require_messages(request)
        if len(messages) != 2:
            raise PortableContractError(
                "QWEN_MESSAGE_SHAPE_MISMATCH", "flat-progress host requires exactly two messages"
            )
        system = _message(messages, 0)
        user = _message(messages, 1)
        if system.get("role") != "system" or user.get("role") != "user":
            raise PortableContractError(
                "QWEN_ROLE_MISMATCH", "flat-progress system/user role order changed"
            )
        system_blocks = _content_blocks(system, label="Qwen system")
        user_blocks = _content_blocks(user, label="Qwen user")
        if len(system_blocks) != 1 or len(user_blocks) != 2:
            raise PortableContractError(
                "QWEN_CONTENT_SHAPE_MISMATCH", "Qwen content block count changed"
            )
        system_text_block = _typed_block(system_blocks[0], label="Qwen system block")
        user_text_block = _typed_block(user_blocks[0], label="Qwen user text block")
        image_block = _typed_block(user_blocks[1], label="Qwen current observation")
        if (
            system_text_block.get("type") != "text"
            or user_text_block.get("type") != "text"
            or image_block.get("type") != "image_url"
        ):
            raise PortableContractError(
                "QWEN_CONTENT_SHAPE_MISMATCH", "Qwen text/image block order changed"
            )
        _require_image_url_block(image_block, label="Qwen current observation")
        system_text = system_text_block.get("text")
        user_text = user_text_block.get("text")
        if not isinstance(system_text, str) or not system_text:
            raise PortableContractError("QWEN_SYSTEM_TEXT_INVALID", "Qwen system text is empty")
        if not isinstance(user_text, str) or not user_text:
            raise PortableContractError("QWEN_USER_TEXT_INVALID", "Qwen user text is empty")
        if user_text.count(_QWEN_QUERY_MARKER) != 1 or user_text.count(_QWEN_PROGRESS_MARKER) != 1:
            raise PortableContractError(
                "QWEN_PROGRESS_BLOCK_AMBIGUOUS", "task/progress markers must each resolve once"
            )
        query_start = user_text.index(_QWEN_QUERY_MARKER)
        progress_start = user_text.index(_QWEN_PROGRESS_MARKER)
        history_start = user_text.index(_QWEN_PROGRESS_MARKER) + len(_QWEN_PROGRESS_MARKER)
        task_text = user_text[query_start + len(_QWEN_QUERY_MARKER) : progress_start]
        if (
            query_start != 0
            or not task_text
            or not task_text.strip()
            or progress_start <= query_start + len(_QWEN_QUERY_MARKER)
            or not user_text.endswith("\n")
        ):
            raise PortableContractError(
                "QWEN_PROGRESS_BLOCK_MISMATCH", "captured Qwen prompt framing changed"
            )
        history_end = len(user_text) - 1
        if history_start >= history_end:
            raise PortableContractError("EMPTY_HISTORY_IR", "Qwen progress block has no records")
        history_text = user_text[history_start:history_end]
        matches = list(_QWEN_STEP.finditer(history_text))
        if not matches or matches[0].start() != 0:
            raise PortableContractError(
                "QWEN_STEP_PARSE_FAILED", "progress history must start with Step 1"
            )
        if [int(match.group(1)) for match in matches] != list(range(1, len(matches) + 1)):
            raise PortableContractError(
                "QWEN_STEP_ORDINAL_MISMATCH", "progress step ordinals must be contiguous"
            )
        for match in matches[1:]:
            if history_text[match.start() - 2 : match.start()] != "; ":
                raise PortableContractError(
                    "QWEN_STEP_BOUNDARY_AMBIGUOUS", "step boundary is not exact host syntax"
                )

        system_path: JsonPath = ("messages", 0, "content", 0, "text")
        history_path: JsonPath = ("messages", 1, "content", 0, "text")
        system_slice = _text_slice(system_path, system_text, 0, len(system_text))
        task_slice = _text_slice(history_path, user_text, 0, history_start)
        history_slice = _text_slice(history_path, user_text, history_start, history_end)
        regions: list[RequestRegion] = [
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.SYSTEM,
                ordinal=0,
                request=request,
                paths=(("messages", 0, "role"),),
                text_slices=(system_slice,),
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.TASK,
                ordinal=1,
                request=request,
                text_slices=(task_slice,),
                availability=RegionAvailability.COLOCATED,
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.HISTORY,
                ordinal=2,
                request=request,
                text_slices=(history_slice,),
                availability=RegionAvailability.COLOCATED,
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.CURRENT_OBSERVATION,
                ordinal=3,
                request=request,
                paths=(("messages", 1, "role"), ("messages", 1, "content", 1)),
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.TOOL_PROTOCOL,
                ordinal=4,
                request=request,
                text_slices=(system_slice,),
                availability=RegionAvailability.COLOCATED,
            ),
        ]
        provider_paths = tuple((key,) for key in sorted(root) if key != "messages")
        if provider_paths:
            regions.append(
                _region(
                    host_id=self.host_id,
                    family=self.history_family,
                    kind=RegionKind.PROVIDER_CONTROL,
                    ordinal=5,
                    request=request,
                    paths=provider_paths,
                )
            )
        current_region = regions[3]
        anchor = self._correction_anchor(
            request, current_region=current_region, message_index=1, reference_index=1
        )

        specs: list[_RecordSpec] = []
        for index, match in enumerate(matches):
            local_start = match.start()
            local_end = (
                matches[index + 1].start() if index + 1 < len(matches) else len(history_text)
            )
            if not history_text[local_start:local_end].endswith("; "):
                raise PortableContractError(
                    "QWEN_STEP_TERMINATOR_MISMATCH", "progress step lacks exact '; ' terminator"
                )
            body_start = match.end()
            body_end = local_end - 2
            body = history_text[body_start:body_end]
            if not body:
                raise PortableContractError("QWEN_EMPTY_STEP", "progress step claim is empty")
            tool_count = body.count(_QWEN_TOOL_RESULT_PREFIX)
            ask_count = body.count(_QWEN_ASK_RESPONSE_PREFIX)
            if tool_count > 1 or ask_count > 1:
                raise PortableContractError(
                    "QWEN_EXTERNAL_RESULT_AMBIGUOUS", "external result marker is repeated"
                )
            marker_positions: list[int] = []
            tool_position = body.index(_QWEN_TOOL_RESULT_PREFIX) if tool_count else None
            ask_position = body.index(_QWEN_ASK_RESPONSE_PREFIX) if ask_count else None
            if (
                tool_position is not None
                and ask_position is not None
                and tool_position >= ask_position
            ):
                raise PortableContractError(
                    "QWEN_EXTERNAL_RESULT_AMBIGUOUS",
                    "tool result must precede ask-user response",
                )
            if tool_count:
                assert tool_position is not None
                suffix = body[tool_position:]
                if (
                    not suffix.startswith(_QWEN_TOOL_RESULT_OPEN)
                    or body.count("<tool_response>") != 1
                    or body.count(_QWEN_TOOL_RESULT_CLOSE) != 1
                ):
                    raise PortableContractError(
                        "QWEN_TOOL_RESULT_WRAPPER_INVALID",
                        "tool result must contain one exact tool_response wrapper",
                    )
                payload_start = tool_position + len(_QWEN_TOOL_RESULT_OPEN)
                close_start = body.index(_QWEN_TOOL_RESULT_CLOSE, payload_start)
                close_end = close_start + len(_QWEN_TOOL_RESULT_CLOSE)
                payload = body[payload_start:close_start]
                if not payload:
                    raise PortableContractError(
                        "QWEN_TOOL_RESULT_WRAPPER_INVALID",
                        "tool_response payload must be non-empty",
                    )
                if ask_position is None:
                    if close_end != len(body):
                        raise PortableContractError(
                            "QWEN_TOOL_RESULT_WRAPPER_INVALID",
                            "tool_response must be terminal unless an exact ask response follows",
                        )
                elif ask_position != close_end:
                    raise PortableContractError(
                        "QWEN_EXTERNAL_RESULT_AMBIGUOUS",
                        "ask-user response must immediately follow the tool_response wrapper",
                    )
                marker_positions.append(tool_position)
            if ask_count:
                assert ask_position is not None
                response = body[ask_position + len(_QWEN_ASK_RESPONSE_PREFIX) :]
                if not response:
                    raise PortableContractError(
                        "QWEN_ASK_RESPONSE_INVALID", "ask-user response is empty"
                    )
                if "<tool_response>" in response or _QWEN_TOOL_RESULT_CLOSE in response:
                    raise PortableContractError(
                        "QWEN_EXTERNAL_RESULT_AMBIGUOUS",
                        "ask-user response contains a tool_response wrapper",
                    )
                marker_positions.append(ask_position)
            if (
                not tool_count
                and not ask_count
                and (
                    "<tool_response>" in body
                    or _QWEN_TOOL_RESULT_CLOSE in body
                    or "Tool call result:" in body
                    or "Ask user response:" in body
                )
            ):
                raise PortableContractError(
                    "QWEN_EXTERNAL_RESULT_AMBIGUOUS",
                    "external result syntax is present outside an exact host marker",
                )
            claim_local_end = body_start + (
                min(marker_positions) if marker_positions else len(body)
            )
            if claim_local_end <= body_start:
                raise PortableContractError("QWEN_EMPTY_STEP", "progress semantic claim is empty")
            absolute_start = history_start + local_start
            absolute_end = history_start + local_end
            claim_start = history_start + body_start
            claim_end = history_start + claim_local_end
            protected = [
                _source_span(
                    path=history_path,
                    text=user_text,
                    start=absolute_start,
                    end=claim_start,
                    role=SpanRole.PROTECTED_PROTOCOL,
                ),
                _source_span(
                    path=history_path,
                    text=user_text,
                    start=claim_end,
                    end=absolute_end,
                    role=(
                        SpanRole.PROTECTED_EXTERNAL_RESULT
                        if marker_positions
                        else SpanRole.PROTECTED_PROTOCOL
                    ),
                ),
            ]
            specs.append(
                _RecordSpec(
                    record_key=f"step-{index + 1:04d}",
                    record_class="flat_progress_step",
                    region_id=regions[2].region_id,
                    role="user",
                    author="captured_actor_progress",
                    modality=RecordModality.TEXT,
                    container_path=history_path,
                    char_start=absolute_start,
                    char_end=absolute_end,
                    editable_start=claim_start,
                    editable_end=claim_end,
                    message_index=1,
                    content_block_index=0,
                    representation_record_index=index,
                    provenance={
                        "host_representation": "qwen_flat_progress",
                        "step_ordinal": index + 1,
                        "source": "captured_application_request",
                    },
                    protected_spans=tuple(protected),
                    related_content=((("messages", 1, "content", 1), RelatedContentKind.IMAGE),),
                )
            )
        return self._finish_ir(
            request,
            regions=tuple(regions),
            specs=tuple(specs),
            correction_anchor=anchor,
        )


class MaiRawReplayHistoryCodec(_CapturedHistoryCodec):
    """Exact codec for captured MAI prior-assistant raw replay messages."""

    _codec_id = "mobileworld.g1.history-codec.mai-raw-replay"
    _host_id = "mobileworld.mai-ui.actor"
    _family = HistoryFamily.RAW_REPLAY

    def extract(self, application_request: JsonValue) -> HistoryIR:
        request = self._copy_bound_request(application_request)
        root, messages = _require_messages(request)
        if len(messages) < 4:
            raise PortableContractError(
                "MAI_MESSAGE_SHAPE_MISMATCH", "raw replay needs system, task, history, and current"
            )
        system = _message(messages, 0)
        task = _message(messages, 1)
        current_index = len(messages) - 1
        current = _message(messages, current_index)
        if system.get("role") != "system" or task.get("role") != "user":
            raise PortableContractError(
                "MAI_ROLE_MISMATCH", "raw replay system/task role order changed"
            )
        if current.get("role") != "user":
            raise PortableContractError(
                "MAI_CURRENT_OBSERVATION_MISSING", "last MAI message must be current user context"
            )
        system_text = system.get("content")
        if not isinstance(system_text, str) or not system_text:
            raise PortableContractError("MAI_SYSTEM_TEXT_INVALID", "MAI system text is empty")
        task_blocks = _content_blocks(task, label="MAI task")
        current_blocks = _content_blocks(current, label="MAI current observation")
        if len(task_blocks) != 1 or len(current_blocks) != 1:
            raise PortableContractError(
                "MAI_CONTENT_SHAPE_MISMATCH", "MAI task/current block count changed"
            )
        task_block = _typed_block(task_blocks[0], label="MAI task block")
        current_block = _typed_block(current_blocks[0], label="MAI current block")
        if task_block.get("type") != "text" or current_block.get("type") != "image_url":
            raise PortableContractError(
                "MAI_CONTENT_SHAPE_MISMATCH",
                "MAI task must be text and current observation must be one image",
            )
        task_text = task_block.get("text")
        if not isinstance(task_text, str) or not task_text:
            raise PortableContractError("MAI_TASK_TEXT_INVALID", "MAI task text is empty")
        _require_image_url_block(current_block, label="MAI current observation")

        system_path: JsonPath = ("messages", 0, "content")
        system_slice = _text_slice(system_path, system_text, 0, len(system_text))
        history_paths = tuple(("messages", index) for index in range(2, current_index))
        regions: list[RequestRegion] = [
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.SYSTEM,
                ordinal=0,
                request=request,
                paths=(("messages", 0, "role"),),
                text_slices=(system_slice,),
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.TASK,
                ordinal=1,
                request=request,
                paths=(("messages", 1),),
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.HISTORY,
                ordinal=2,
                request=request,
                paths=history_paths,
            ),
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.CURRENT_OBSERVATION,
                ordinal=3,
                request=request,
                paths=(
                    ("messages", current_index, "role"),
                    ("messages", current_index, "content", 0),
                ),
            ),
        ]
        current_region = regions[3]
        anchor = self._correction_anchor(
            request,
            current_region=current_region,
            message_index=current_index,
            reference_index=0,
        )

        specs: list[_RecordSpec] = []
        tool_slices: list[FrozenTextSlice] = [system_slice]
        assistant_count = 0
        for message_index in range(2, current_index):
            message = _message(messages, message_index)
            role = message.get("role")
            if role == "assistant":
                content = message.get("content")
                if not isinstance(content, str) or not content:
                    raise PortableContractError(
                        "MAI_ASSISTANT_CONTENT_INVALID", "assistant replay content must be text"
                    )
                counts = {
                    token: content.count(token)
                    for token in (
                        _THINK_OPEN,
                        _THINK_CLOSE,
                        _THINK_LEGACY_CLOSE,
                        _TOOL_OPEN,
                        _TOOL_CLOSE,
                    )
                }
                canonical_thinking = (
                    counts[_THINK_OPEN] == 1
                    and counts[_THINK_CLOSE] == 1
                    and counts[_THINK_LEGACY_CLOSE] == 0
                )
                legacy_thinking = (
                    counts[_THINK_OPEN] == 0
                    and counts[_THINK_CLOSE] == 0
                    and counts[_THINK_LEGACY_CLOSE] == 1
                )
                if (
                    not (canonical_thinking or legacy_thinking)
                    or counts[_TOOL_OPEN] != 1
                    or counts[_TOOL_CLOSE] != 1
                ):
                    raise PortableContractError(
                        "MAI_WRAPPER_AMBIGUOUS", "thinking/tool wrappers must each resolve once"
                    )
                tool_open = content.index(_TOOL_OPEN)
                tool_close = content.index(_TOOL_CLOSE)
                if canonical_thinking:
                    think_open = content.index(_THINK_OPEN)
                    think_close = content.index(_THINK_CLOSE)
                    if content[:think_open].strip():
                        raise PortableContractError(
                            "MAI_WRAPPER_ORDER_MISMATCH",
                            "assistant wrapper order or shell changed",
                        )
                    inner_start = think_open + len(_THINK_OPEN)
                    thinking_close_end = think_close + len(_THINK_CLOSE)
                    wrapper_variant = "thinking"
                else:
                    think_open = -1
                    think_close = content.index(_THINK_LEGACY_CLOSE)
                    inner_start = 0
                    thinking_close_end = think_close + len(_THINK_LEGACY_CLOSE)
                    wrapper_variant = "legacy_think_close"
                if (
                    not inner_start <= think_close < tool_open < tool_close
                    or content[thinking_close_end:tool_open].strip()
                    or content[tool_close + len(_TOOL_CLOSE) :].strip()
                ):
                    raise PortableContractError(
                        "MAI_WRAPPER_ORDER_MISMATCH", "assistant wrapper order or shell changed"
                    )
                tool_text = content[tool_open + len(_TOOL_OPEN) : tool_close].strip()
                try:
                    parsed_tool = json.loads(tool_text)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise PortableContractError(
                        "MAI_TOOL_WRAPPER_INVALID", "historical tool call JSON is invalid"
                    ) from exc
                if not isinstance(parsed_tool, dict):
                    raise PortableContractError(
                        "MAI_TOOL_WRAPPER_INVALID", "historical tool call must be an object"
                    )
                inner_end = think_close
                inner = content[inner_start:inner_end]
                left_trim = len(inner) - len(inner.lstrip())
                right_trim = len(inner) - len(inner.rstrip())
                claim_start = inner_start + left_trim
                claim_end = inner_end - right_trim
                if claim_start >= claim_end:
                    raise PortableContractError(
                        "MAI_EMPTY_REASONING", "assistant replay reasoning is empty"
                    )
                path: JsonPath = ("messages", message_index, "content")
                protected = tuple(
                    _source_span(
                        path=path,
                        text=content,
                        start=start,
                        end=end,
                        role=SpanRole.PROTECTED_PROTOCOL,
                    )
                    for start, end in ((0, claim_start), (claim_end, len(content)))
                    if start < end
                )
                tool_slices.append(_text_slice(path, content, tool_open, len(content)))
                related: tuple[tuple[JsonPath, RelatedContentKind], ...] = ()
                if message_index + 1 < len(messages):
                    following = _message(messages, message_index + 1)
                    if following.get("role") == "user":
                        blocks = _content_blocks(following, label="MAI adjacent observation")
                        if len(blocks) != 1:
                            raise PortableContractError(
                                "MAI_OBSERVATION_SHAPE_MISMATCH",
                                "adjacent user observation must contain one block",
                            )
                        block = _typed_block(blocks[0], label="MAI adjacent observation block")
                        block_type = block.get("type")
                        if block_type not in {"image_url", "text"}:
                            raise PortableContractError(
                                "MAI_OBSERVATION_SHAPE_MISMATCH",
                                "adjacent observation block type is unsupported",
                            )
                        related = (
                            (
                                ("messages", message_index + 1, "content", 0),
                                (
                                    RelatedContentKind.IMAGE
                                    if block_type == "image_url"
                                    else RelatedContentKind.TOOL_RESULT
                                ),
                            ),
                        )
                specs.append(
                    _RecordSpec(
                        record_key=f"assistant-message-{message_index:04d}",
                        record_class="raw_replay_assistant_message",
                        region_id=regions[2].region_id,
                        role="assistant",
                        author="assistant",
                        modality=RecordModality.TEXT,
                        container_path=path,
                        char_start=0,
                        char_end=len(content),
                        editable_start=claim_start,
                        editable_end=claim_end,
                        message_index=message_index,
                        content_block_index=None,
                        representation_record_index=0,
                        provenance={
                            "host_representation": "mai_raw_replay",
                            "message_index": message_index,
                            "thinking_wrapper_variant": wrapper_variant,
                            "source": "captured_application_request",
                        },
                        protected_spans=protected,
                        related_content=related,
                    )
                )
                assistant_count += 1
            elif role == "user":
                blocks = _content_blocks(message, label="MAI historical user observation")
                if len(blocks) != 1:
                    raise PortableContractError(
                        "MAI_OBSERVATION_SHAPE_MISMATCH",
                        "historical user observation must contain one block",
                    )
                block = _typed_block(blocks[0], label="MAI historical observation block")
                block_type = block.get("type")
                if block_type == "text":
                    if (
                        message_index == 2
                        or _message(messages, message_index - 1).get("role") != "assistant"
                    ):
                        raise PortableContractError(
                            "MAI_BROKEN_OBSERVATION_ADJACENCY",
                            "visible text observation must immediately follow an assistant",
                        )
                    text = block.get("text")
                    if not isinstance(text, str) or not text:
                        raise PortableContractError(
                            "MAI_OBSERVATION_TEXT_INVALID", "historical observation text is empty"
                        )
                    path = ("messages", message_index, "content", 0, "text")
                    extent = _source_span(
                        path=path,
                        text=text,
                        start=0,
                        end=len(text),
                        role=SpanRole.PROTECTED_EXTERNAL_RESULT,
                    )
                    specs.append(
                        _RecordSpec(
                            record_key=f"user-observation-{message_index:04d}",
                            record_class="raw_replay_user_observation",
                            region_id=regions[2].region_id,
                            role="user",
                            author="host_observation",
                            modality=RecordModality.TOOL_RESULT,
                            container_path=path,
                            char_start=0,
                            char_end=len(text),
                            editable_start=None,
                            editable_end=None,
                            message_index=message_index,
                            content_block_index=0,
                            representation_record_index=0,
                            provenance={
                                "host_representation": "mai_raw_replay",
                                "message_index": message_index,
                                "observation_type": "text",
                                "source": "captured_application_request",
                            },
                            protected_spans=(extent,),
                        )
                    )
                elif block_type == "image_url":
                    _require_image_url_block(block, label="MAI historical observation")
                    if (
                        message_index != 2
                        and _message(messages, message_index - 1).get("role") != "assistant"
                    ):
                        raise PortableContractError(
                            "MAI_BROKEN_OBSERVATION_ADJACENCY",
                            "historical image must be the initial screenshot or follow an assistant",
                        )
                else:
                    raise PortableContractError(
                        "MAI_OBSERVATION_SHAPE_MISMATCH",
                        "historical observation must be text or image_url",
                    )
            else:
                raise PortableContractError(
                    "MAI_HISTORY_ROLE_UNSUPPORTED",
                    "raw replay history accepts only assistant/user host messages",
                )
        if assistant_count == 0:
            raise PortableContractError("EMPTY_HISTORY_IR", "MAI history has no assistant record")

        regions.append(
            _region(
                host_id=self.host_id,
                family=self.history_family,
                kind=RegionKind.TOOL_PROTOCOL,
                ordinal=4,
                request=request,
                text_slices=tuple(tool_slices),
                availability=RegionAvailability.COLOCATED,
            )
        )
        provider_paths = tuple((key,) for key in sorted(root) if key != "messages")
        if provider_paths:
            regions.append(
                _region(
                    host_id=self.host_id,
                    family=self.history_family,
                    kind=RegionKind.PROVIDER_CONTROL,
                    ordinal=5,
                    request=request,
                    paths=provider_paths,
                )
            )
        return self._finish_ir(
            request,
            regions=tuple(regions),
            specs=tuple(specs),
            correction_anchor=anchor,
        )
