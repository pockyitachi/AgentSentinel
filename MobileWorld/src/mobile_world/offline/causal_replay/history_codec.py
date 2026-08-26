"""History Codec interface and declarative fixture-only implementation.

The declarative codec proves the portable contract against six frozen fixture
families.  It is deliberately not a live Qwen, MAI, or other model adapter.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

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
    HistoryCodecDeclaration,
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
    SourceVersionRef,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    stable_id,
    text_sha256,
)
from mobile_world.offline.causal_replay.core import render_request, validate_history_ir


@runtime_checkable
class HistoryCodec(HistoryCodecDeclaration, Protocol):
    def extract(self, application_request: JsonValue) -> HistoryIR: ...

    def render(
        self,
        application_request: JsonValue,
        ir: HistoryIR,
        plan: TransformationPlan,
        *,
        execution_mode: ExecutionMode,
        failure_policy: FailurePolicy,
    ) -> RenderResult: ...


class DeclarativeFixtureHistoryCodec:
    """Exact path/offset codec used only by checked-in conformance vectors."""

    def __init__(self, mapping: dict[str, JsonValue]) -> None:
        copied_mapping = copy_json(mapping)
        if not isinstance(copied_mapping, dict):
            raise PortableContractError("INVALID_FIXTURE_MAPPING", "mapping must be an object")
        self._mapping = copied_mapping
        self._codec_id = _required_string(self._mapping, "codec_id")
        try:
            self._family = HistoryFamily(_required_string(self._mapping, "history_family"))
        except ValueError as exc:
            raise PortableContractError("UNKNOWN_HISTORY_FAMILY", str(exc)) from exc
        self._host_id = _required_string(self._mapping, "host_id")
        self._capabilities = _parse_capabilities(
            self._mapping, self._codec_id, self.contract_version, self._family
        )
        if self._capabilities.scope is not CodecScope.FIXTURE_ONLY:
            raise PortableContractError(
                "DECLARATIVE_CODEC_SCOPE_INVALID", "fixture codec cannot claim live readiness"
            )
        if self._capabilities.live_ready:
            raise PortableContractError(
                "DECLARATIVE_CODEC_LIVE_FORBIDDEN", "fixture codec must set live_ready=false"
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

    def extract(self, application_request: JsonValue) -> HistoryIR:
        request = copy_json(application_request)
        source_hash = canonical_sha256(request)
        regions = self._extract_regions(request)
        regions_by_key = {
            _required_string(_object(raw, "region"), "region_key"): region
            for raw, region in zip(_required_list(self._mapping, "regions"), regions, strict=True)
        }
        source_versions, source_version_ids = self._parse_source_versions()
        record_specs = _required_list(self._mapping, "records")
        if not record_specs:
            raise PortableContractError("EMPTY_HISTORY_IR", "fixture must contain a history record")
        preliminary: list[
            tuple[
                dict[str, JsonValue],
                str,
                SourceSpan,
                tuple[SourceSpan, ...],
                tuple[SourceSpan, ...],
            ]
        ] = []
        record_ids: dict[str, str] = {}
        for raw_spec in record_specs:
            spec = _object(raw_spec, "record")
            record_key = _required_string(spec, "record_key")
            if record_key in record_ids:
                raise PortableContractError("DUPLICATE_RECORD_KEY", "record keys must be unique")
            path = _parse_path(spec.get("container_path"))
            container = get_at_path(request, path)
            if not isinstance(container, str):
                raise PortableContractError("SPAN_CONTAINER_NOT_TEXT", "record path is not text")
            source_span = _parse_frozen_span(
                spec,
                prefix="source_",
                path=path,
                container=container,
                span_role=SpanRole.RECORD_EXTENT,
                claim_id=None,
            )
            record_class = _required_string(spec, "record_class")
            role = _required_string(spec, "role")
            author = _required_string(spec, "author")
            modality_text = _required_string(spec, "modality")
            record_id = stable_id(
                "record",
                {
                    "host_id": self._host_id,
                    "history_family": self._family.value,
                    "source_request_sha256": source_hash,
                    "container_path": list(path),
                    "char_start": source_span.char_start,
                    "char_end": source_span.char_end,
                    "span_sha256": source_span.span_sha256,
                    "record_class": record_class,
                    "role": role,
                    "author": author,
                    "modality": modality_text,
                },
            )
            record_ids[record_key] = record_id
            editable = self._parse_child_spans(
                request=request,
                record_id=record_id,
                record_spec=spec,
                key="editable_spans",
                default_role=SpanRole.EDITABLE_CLAIM,
            )
            protected = self._parse_child_spans(
                request=request,
                record_id=record_id,
                record_spec=spec,
                key="protected_spans",
                default_role=SpanRole.PROTECTED_PROTOCOL,
            )
            preliminary.append((spec, record_id, source_span, editable, protected))

        known_regions = {region.region_id for region in regions}
        records: list[HistoryRecord] = []
        for spec, record_id, source_span, editable, protected in preliminary:
            region_key = _required_string(spec, "region_key")
            region_id = stable_id(
                "region",
                {
                    "host_id": self._host_id,
                    "history_family": self._family.value,
                    "region_key": region_key,
                    "source_request_sha256": source_hash,
                },
            )
            if region_id not in known_regions:
                raise PortableContractError(
                    "UNKNOWN_RECORD_REGION", "record region is not declared"
                )
            provenance = _object(spec.get("provenance", {}), "provenance")
            _reject_forbidden_derived_labels(provenance)
            relationships = self._parse_relationships(
                spec, record_id, record_ids, source_version_ids
            )
            correction_anchors = self._parse_correction_anchors(request, spec, regions_by_key)
            source_version_links: list[str] = []
            for key in _string_list(spec.get("source_version_keys", []), "source_version_keys"):
                if key in record_ids:
                    source_version_links.append(record_ids[key])
                elif key in source_version_ids:
                    source_version_links.append(source_version_ids[key])
                else:
                    raise PortableContractError(
                        "UNKNOWN_SOURCE_VERSION", "source version key is unknown"
                    )
            coordinates = self._parse_coordinates(spec, source_span.container_path)
            modality = _enum_value(
                RecordModality, _required_string(spec, "modality"), "UNKNOWN_RECORD_MODALITY"
            )
            related_content = self._parse_related_content(request, spec)
            records.append(
                HistoryRecord(
                    record_id=record_id,
                    record_key=_required_string(spec, "record_key"),
                    record_class=_required_string(spec, "record_class"),
                    region_id=region_id,
                    role=_required_string(spec, "role"),
                    author=_required_string(spec, "author"),
                    modality=modality,
                    coordinates=coordinates,
                    record_sha256=source_span.span_sha256,
                    source_span=source_span,
                    editable_spans=editable,
                    protected_spans=protected,
                    write_time=_optional_string(spec.get("write_time"), "write_time"),
                    exposure_time=_required_string(spec, "exposure_time"),
                    provenance=provenance,
                    correction_anchors=correction_anchors,
                    relationships=relationships,
                    related_content=related_content,
                    version=_required_int(spec, "version", minimum=1),
                    source_version_ids=tuple(source_version_links),
                )
            )
        ir = HistoryIR(
            host_id=self._host_id,
            history_family=self._family,
            codec_id=self._codec_id,
            codec_contract_version=self.contract_version,
            raw_request_sha256=source_hash,
            regions=tuple(regions),
            records=tuple(records),
            source_versions=source_versions,
            capabilities=self._capabilities,
        )
        validate_history_ir(request, ir)
        return ir

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

    def _extract_regions(self, request: JsonValue) -> list[RequestRegion]:
        raw_regions = _required_list(self._mapping, "regions")
        regions: list[RequestRegion] = []
        source_hash = canonical_sha256(request)
        for raw_region in raw_regions:
            region = _object(raw_region, "region")
            region_key = _required_string(region, "region_key")
            try:
                kind = RegionKind(_required_string(region, "kind"))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_REGION_KIND", str(exc)) from exc
            try:
                availability = RegionAvailability(_required_string(region, "availability"))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_REGION_AVAILABILITY", str(exc)) from exc
            paths = tuple(_parse_path(item) for item in _required_list(region, "paths"))
            text_slices = tuple(
                self._parse_region_text_slice(request, _object(item, "region_text_slice"))
                for item in _required_list(region, "text_slices")
            )
            absence_reason = _optional_string(region.get("absence_reason"), "absence_reason")
            if availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT:
                if paths or text_slices or absence_reason is None:
                    raise PortableContractError(
                        "INVALID_ABSENT_REGION", "absent region needs only a reason"
                    )
            elif not paths and not text_slices:
                raise PortableContractError("EMPTY_REGION", "present region needs a locator")
            elif absence_reason is not None:
                raise PortableContractError(
                    "INVALID_PRESENT_REGION", "present region cannot have an absence reason"
                )
            projection: list[JsonValue] = [get_at_path(request, path) for path in paths]
            projection.extend(item.exact_text for item in text_slices)
            regions.append(
                RequestRegion(
                    region_id=stable_id(
                        "region",
                        {
                            "host_id": self._host_id,
                            "history_family": self._family.value,
                            "region_key": region_key,
                            "source_request_sha256": source_hash,
                        },
                    ),
                    kind=kind,
                    paths=paths,
                    text_slices=text_slices,
                    source_sha256=canonical_sha256(projection),
                    availability=availability,
                    absence_reason=absence_reason,
                    preserve_exact=bool(region.get("preserve_exact", True)),
                )
            )
        return regions

    def _parse_region_text_slice(
        self, request: JsonValue, spec: dict[str, JsonValue]
    ) -> FrozenTextSlice:
        path = _parse_path(spec.get("container_path"))
        container = get_at_path(request, path)
        if not isinstance(container, str):
            raise PortableContractError("SPAN_CONTAINER_NOT_TEXT", "region slice is not text")
        span = _parse_frozen_span(
            spec,
            prefix="",
            path=path,
            container=container,
            span_role=SpanRole.RECORD_EXTENT,
            claim_id=None,
        )
        return FrozenTextSlice(
            container_path=span.container_path,
            char_start=span.char_start,
            char_end=span.char_end,
            utf8_byte_start=span.utf8_byte_start,
            utf8_byte_end=span.utf8_byte_end,
            exact_text=span.exact_text,
            span_sha256=span.span_sha256,
        )

    def _parse_child_spans(
        self,
        *,
        request: JsonValue,
        record_id: str,
        record_spec: dict[str, JsonValue],
        key: str,
        default_role: SpanRole,
    ) -> tuple[SourceSpan, ...]:
        spans: list[SourceSpan] = []
        for raw_span in _required_list(record_spec, key):
            spec = _object(raw_span, key)
            path = _parse_path(spec.get("container_path"))
            container = get_at_path(request, path)
            if not isinstance(container, str):
                raise PortableContractError("SPAN_CONTAINER_NOT_TEXT", "child span is not text")
            try:
                role = SpanRole(str(spec.get("span_role", default_role.value)))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_SPAN_ROLE", str(exc)) from exc
            claim_key = _optional_string(spec.get("claim_key"), "claim_key")
            claim_id = None
            if claim_key is not None:
                claim_id = stable_id(
                    "claim",
                    {
                        "record_id": record_id,
                        "container_path": list(path),
                        "char_start": _required_int(spec, "char_start", minimum=0),
                        "char_end": _required_int(spec, "char_end", minimum=1),
                        "span_sha256": _required_string(spec, "span_sha256"),
                    },
                )
            spans.append(
                _parse_frozen_span(
                    spec,
                    prefix="",
                    path=path,
                    container=container,
                    span_role=role,
                    claim_id=claim_id,
                )
            )
        return tuple(spans)

    def _parse_relationships(
        self,
        record_spec: dict[str, JsonValue],
        source_record_id: str,
        record_ids: dict[str, str],
        source_version_ids: dict[str, str],
    ) -> tuple[HistoryRelationship, ...]:
        relationships: list[HistoryRelationship] = []
        for index, raw_relationship in enumerate(_required_list(record_spec, "relationships")):
            item = _object(raw_relationship, "relationship")
            try:
                kind = RelationshipKind(_required_string(item, "kind"))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_RELATIONSHIP_KIND", str(exc)) from exc
            target_key = _optional_string(item.get("target_record_key"), "target_record_key")
            target_version_key = _optional_string(
                item.get("target_version_key"), "target_version_key"
            )
            target_path_raw = item.get("target_path")
            if (
                sum(
                    value is not None for value in (target_key, target_version_key, target_path_raw)
                )
                != 1
            ):
                raise PortableContractError(
                    "RELATIONSHIP_TARGET_INVALID", "relationship needs one target"
                )
            target_record_id = None
            target_path = None
            target_version_id = None
            if target_key is not None:
                try:
                    target_record_id = record_ids[target_key]
                except KeyError as exc:
                    raise PortableContractError(
                        "UNKNOWN_RELATIONSHIP_TARGET", "target record key is unknown"
                    ) from exc
            elif target_version_key is not None:
                try:
                    target_version_id = source_version_ids[target_version_key]
                except KeyError as exc:
                    raise PortableContractError(
                        "UNKNOWN_RELATIONSHIP_TARGET", "target version key is unknown"
                    ) from exc
            else:
                target_path = _parse_path(target_path_raw)
            relationships.append(
                HistoryRelationship(
                    relationship_id=stable_id(
                        "relationship",
                        {
                            "source_record_id": source_record_id,
                            "index": index,
                            "kind": kind.value,
                            "target_record_id": target_record_id,
                            "target_path": None if target_path is None else list(target_path),
                            "target_version_id": target_version_id,
                        },
                    ),
                    kind=kind,
                    source_record_id=source_record_id,
                    target_record_id=target_record_id,
                    target_path=target_path,
                    target_version_id=target_version_id,
                )
            )
        return tuple(relationships)

    def _parse_correction_anchors(
        self,
        request: JsonValue,
        record_spec: dict[str, JsonValue],
        regions_by_key: dict[str, RequestRegion],
    ) -> tuple[CorrectionAnchor, ...]:
        anchors: list[CorrectionAnchor] = []
        for raw_anchor in _required_list(record_spec, "correction_anchors"):
            item = _object(raw_anchor, "correction_anchor")
            path = _parse_path(item.get("container_path"))
            container = get_at_path(request, path)
            if not isinstance(container, list):
                raise PortableContractError(
                    "CORRECTION_ANCHOR_NOT_LIST", "fixture correction anchor must be an array"
                )
            index = _required_int(item, "insert_index", minimum=0)
            if index > len(container):
                raise PortableContractError(
                    "CORRECTION_ANCHOR_OUT_OF_BOUNDS", "fixture insertion index is invalid"
                )
            try:
                context_kind = CorrectionContextKind(_required_string(item, "context_kind"))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_CORRECTION_CONTEXT", str(exc)) from exc
            owner_region_key = _required_string(item, "owner_region_key")
            try:
                owner_region = regions_by_key[owner_region_key]
            except KeyError as exc:
                raise PortableContractError(
                    "UNKNOWN_CORRECTION_REGION", "correction owner region is unknown"
                ) from exc
            host_context_path = _parse_path(item.get("host_context_path"))
            host_context = get_at_path(request, host_context_path)
            role_path = _parse_path(item.get("role_path"))
            expected_role = _required_string(item, "expected_role")
            reference_path = _parse_path(item.get("reference_path"))
            reference_value = get_at_path(request, reference_path)
            try:
                placement = CorrectionPlacement(_required_string(item, "placement"))
            except ValueError as exc:
                raise PortableContractError("UNKNOWN_CORRECTION_PLACEMENT", str(exc)) from exc
            anchors.append(
                CorrectionAnchor(
                    container_path=path,
                    insert_index=index,
                    source_container_sha256=canonical_sha256(container),
                    owner_region_id=owner_region.region_id,
                    host_context_path=host_context_path,
                    host_context_sha256=canonical_sha256(host_context),
                    role_path=role_path,
                    expected_role=expected_role,
                    reference_path=reference_path,
                    reference_sha256=canonical_sha256(reference_value),
                    placement=placement,
                    context_kind=context_kind,
                    visible_prefix=_required_string(item, "visible_prefix"),
                    visible_suffix=_required_string(item, "visible_suffix", allow_empty=True),
                )
            )
        return tuple(anchors)

    def _parse_source_versions(
        self,
    ) -> tuple[tuple[SourceVersionRef, ...], dict[str, str]]:
        versions: list[SourceVersionRef] = []
        by_key: dict[str, str] = {}
        for raw_item in _required_list(self._mapping, "source_versions"):
            item = _object(raw_item, "source_version")
            key = _required_string(item, "version_key")
            if key in by_key:
                raise PortableContractError(
                    "DUPLICATE_SOURCE_VERSION", "source version keys must be unique"
                )
            provenance = _object(item.get("provenance", {}), "provenance")
            _reject_forbidden_derived_labels(provenance)
            source_record_id = _required_string(item, "source_record_id")
            version_number = _required_int(item, "version", minimum=1)
            source_request_sha256 = _required_sha256(item, "source_request_sha256")
            source_span_sha256 = _required_sha256(item, "source_span_sha256")
            payload: dict[str, JsonValue] = {
                "source_record_id": source_record_id,
                "version": version_number,
                "source_request_sha256": source_request_sha256,
                "source_span_sha256": source_span_sha256,
            }
            version_id = stable_id("version", payload)
            by_key[key] = version_id
            versions.append(
                SourceVersionRef(
                    version_id=version_id,
                    source_record_id=source_record_id,
                    version=version_number,
                    source_request_sha256=source_request_sha256,
                    source_span_sha256=source_span_sha256,
                    write_time=_optional_string(item.get("write_time"), "write_time"),
                    model_visible_in_current_request=_required_bool(
                        item, "model_visible_in_current_request"
                    ),
                    provenance=provenance,
                )
            )
        return tuple(versions), by_key

    def _parse_coordinates(
        self, record_spec: dict[str, JsonValue], request_path: JsonPath
    ) -> RecordCoordinates:
        raw = _object(record_spec.get("coordinates"), "coordinates")
        message_index = _optional_int(raw.get("message_index"), "message_index", minimum=0)
        block_index = _optional_int(
            raw.get("content_block_index"), "content_block_index", minimum=0
        )
        inferred_message = (
            request_path[1]
            if len(request_path) >= 2
            and request_path[0] == "messages"
            and isinstance(request_path[1], int)
            else None
        )
        inferred_block = None
        for index, token in enumerate(request_path[:-1]):
            if token == "content" and isinstance(request_path[index + 1], int):
                inferred_block = request_path[index + 1]
                break
        if message_index != inferred_message or block_index != inferred_block:
            raise PortableContractError(
                "RECORD_COORDINATE_MISMATCH", "explicit coordinates do not match request path"
            )
        return RecordCoordinates(
            request_path=request_path,
            message_index=message_index,
            content_block_index=block_index,
            representation_record_index=_required_int(
                raw, "representation_record_index", minimum=0
            ),
        )

    def _parse_related_content(
        self, request: JsonValue, record_spec: dict[str, JsonValue]
    ) -> tuple[RelatedContentRef, ...]:
        refs: list[RelatedContentRef] = []
        for raw_ref in _required_list(record_spec, "related_content"):
            item = _object(raw_ref, "related_content")
            path = _parse_path(item.get("path"))
            value = get_at_path(request, path)
            kind = _enum_value(
                RelatedContentKind,
                _required_string(item, "kind"),
                "UNKNOWN_RELATED_CONTENT_KIND",
            )
            blob_sha256 = None
            if isinstance(value, dict):
                candidate = value.get("sha256")
                if candidate is not None:
                    if not isinstance(candidate, str) or not _is_sha256(candidate):
                        raise PortableContractError(
                            "RELATED_BLOB_HASH_INVALID", "related blob digest is invalid"
                        )
                    blob_sha256 = candidate
            refs.append(
                RelatedContentRef(
                    path=path,
                    kind=kind,
                    value_sha256=canonical_sha256(value),
                    blob_sha256=blob_sha256,
                )
            )
        return tuple(refs)


def _parse_capabilities(
    mapping: dict[str, JsonValue],
    codec_id: str,
    contract_version: str,
    family: HistoryFamily,
) -> CodecCapabilities:
    raw = _object(mapping.get("capabilities"), "capabilities")
    try:
        operations = tuple(
            OperationKind(item)
            for item in _string_list(raw.get("supported_operations"), "supported_operations")
        )
        arms = tuple(
            ArmKind(item) for item in _string_list(raw.get("supported_arms"), "supported_arms")
        )
        return CodecCapabilities(
            codec_id=codec_id,
            contract_version=contract_version,
            history_family=family,
            level=CapabilityLevel(_required_string(raw, "level")),
            scope=CodecScope(_required_string(raw, "scope")),
            supported_operations=operations,
            supported_arms=arms,
            preserves_roles=_required_bool(raw, "preserves_roles"),
            preserves_order=_required_bool(raw, "preserves_order"),
            preserves_multimodal_blocks=_required_bool(raw, "preserves_multimodal_blocks"),
            preserves_tool_adjacency=_required_bool(raw, "preserves_tool_adjacency"),
            preserves_protocol_shell=_required_bool(raw, "preserves_protocol_shell"),
            live_ready=_required_bool(raw, "live_ready"),
            opaque_or_server_managed=_required_bool(raw, "opaque_or_server_managed"),
        )
    except ValueError as exc:
        raise PortableContractError("INVALID_CAPABILITY_ENUM", str(exc)) from exc


def _parse_frozen_span(
    spec: dict[str, JsonValue],
    *,
    prefix: str,
    path: JsonPath,
    container: str,
    span_role: SpanRole,
    claim_id: str | None,
) -> SourceSpan:
    char_start = _required_int(spec, f"{prefix}char_start", minimum=0)
    char_end = _required_int(spec, f"{prefix}char_end", minimum=1)
    exact_text = _required_string(spec, f"{prefix}exact_text", allow_empty=False)
    digest = _required_string(spec, f"{prefix}span_sha256")
    utf8_start = _required_int(spec, f"{prefix}utf8_byte_start", minimum=0)
    utf8_end = _required_int(spec, f"{prefix}utf8_byte_end", minimum=1)
    span = SourceSpan(
        container_path=path,
        char_start=char_start,
        char_end=char_end,
        utf8_byte_start=utf8_start,
        utf8_byte_end=utf8_end,
        exact_text=exact_text,
        span_sha256=digest,
        span_role=span_role,
        claim_id=claim_id,
    )
    span.validate_against(container_to_request(path, container))
    return span


def container_to_request(path: JsonPath, text: str) -> JsonValue:
    """Build a minimal request tree for validating an already resolved text path."""

    root: JsonValue = text
    for token in reversed(path):
        if isinstance(token, int):
            if token < 0:
                raise PortableContractError("NEGATIVE_PATH_INDEX", "path index cannot be negative")
            items: list[JsonValue] = [None] * (token + 1)
            items[token] = root
            root = items
        else:
            root = {token: root}
    return root


def _parse_path(raw: JsonValue) -> JsonPath:
    if not isinstance(raw, list) or not raw:
        raise PortableContractError("INVALID_REQUEST_PATH", "path must be a non-empty array")
    path: list[str | int] = []
    for token in raw:
        if isinstance(token, bool) or not isinstance(token, (str, int)):
            raise PortableContractError("INVALID_REQUEST_PATH", "path token must be string/integer")
        if isinstance(token, int) and token < 0:
            raise PortableContractError("INVALID_REQUEST_PATH", "path index cannot be negative")
        if isinstance(token, str) and not token:
            raise PortableContractError("INVALID_REQUEST_PATH", "path key cannot be empty")
        path.append(token)
    return tuple(path)


def _reject_forbidden_derived_labels(value: JsonValue) -> None:
    forbidden = {
        "outcome",
        "failure",
        "failure_link",
        "gold",
        "harm",
        "mhr_oh",
        "treatment_response",
        "verdict",
    }
    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in forbidden:
                raise PortableContractError(
                    "DERIVED_LABEL_IN_IR", "History IR cannot carry audit/outcome labels"
                )
            _reject_forbidden_derived_labels(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden_derived_labels(nested)


def _object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{label} must be an object")
    return value


def _required_list(mapping: dict[str, JsonValue], key: str) -> list[JsonValue]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{key} must be an array")
    return value


def _required_string(mapping: dict[str, JsonValue], key: str, *, allow_empty: bool = False) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{key} must be text")
    return value


def _optional_string(value: JsonValue, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{label} must be text or null")
    return value


def _required_int(mapping: dict[str, JsonValue], key: str, *, minimum: int) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{key} is invalid")
    return value


def _required_bool(mapping: dict[str, JsonValue], key: str) -> bool:
    value = mapping.get(key)
    if not isinstance(value, bool):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{key} must be boolean")
    return value


def _optional_int(value: JsonValue, label: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{label} is invalid")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _required_sha256(mapping: dict[str, JsonValue], key: str) -> str:
    value = _required_string(mapping, key)
    if not _is_sha256(value):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{key} is not sha256")
    return value


def _enum_value[EnumT: Enum](enum_type: type[EnumT], value: str, code: str) -> EnumT:
    try:
        return enum_type(value)
    except ValueError as exc:
        raise PortableContractError(code, str(exc)) from exc


def _string_list(value: JsonValue, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PortableContractError("INVALID_FIXTURE_MAPPING", f"{label} must be a text array")
    return [str(item) for item in value]


def span_sha_matches(text: str, expected: str) -> bool:
    """Small public helper for conformance-vector diagnostics."""

    return text_sha256(text) == expected
