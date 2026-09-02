"""Deterministic admission and SHADOW rendering for the R2.2 runtime overlay.

The untrusted semantic backend may name only packet target/evidence/fact IDs.
This module resolves those IDs against exact trusted values, constructs the
automatic (never curated) runtime plan, and renders a reversible candidate for
SHADOW inspection.  It deliberately does not reuse the G1.2 curated
``TransformationPlan`` or dispatch any object-owned serializer.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn, cast

from mobile_world.offline.causal_replay.contracts import (
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    HistoryFamily,
    HistoryIR,
    HistoryRecord,
    JsonPath,
    JsonValue,
    RegionAvailability,
    RegionKind,
    RequestRegion,
    SourceSpan,
    SpanRole,
)
from mobile_world.runtime.sentinel.r2_2.contracts import (
    POLICY_PROPOSAL_SCHEMA_VERSION,
    EligibleHistoryTargetV1,
    EvidencePacketV1,
    EvidenceRelation,
    EvidenceRole,
    FactualVerdict,
    ProposalEvidenceRefV1,
    R22ContractError,
    RuntimeAdmissionBundleV1,
    RuntimeAdmittedOperationV1,
    RuntimeAdmittedPlanV1,
    RuntimeClaimProposalV1,
    RuntimeFallbackStatus,
    RuntimeOperationKind,
    RuntimePolicyProposalV1,
    RuntimeProposalStatus,
    RuntimeReasonCode,
    RuntimeSentinelPolicyOutputV1,
    RuntimeTargetSpanRole,
    RuntimeUncertaintyCode,
    TemporalProvenanceStatus,
    TemporalValidity,
    evidence_packet_projection,
    evidence_packet_sha256,
    runtime_admitted_operation_projection,
    runtime_admitted_plan_sha256,
    runtime_policy_proposal_sha256,
    validate_runtime_policy_proposal,
)
from mobile_world.runtime.sentinel.r2_2.contracts import (
    bind_policy_receipt as _bind_policy_receipt,
)

if TYPE_CHECKING:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
        AdmissionReceiptProjectionV1,
        GPT56EvidenceInputV1,
        PolicyCallProvenanceV1,
    )


RUNTIME_RENDER_RESULT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-runtime-render-result/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CHECK_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


class RuntimeMappingKind(StrEnum):
    """Closed mapping vocabulary for the provenance-neutral renderer."""

    COPIED = "COPIED"
    DELETED = "DELETED"


def _fail(code: str, message: str) -> NoReturn:
    raise R22ContractError(code, message)


def _require_sha256(value: object, name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(cast(str, value)) is None:
        _fail("INVALID_SHA256", f"{name} must be lowercase SHA-256")


def _require_semantic_id(value: object, name: str) -> None:
    if type(value) is not str or _SEMANTIC_ID.fullmatch(cast(str, value)) is None:
        _fail("INVALID_SEMANTIC_ID", f"{name} must be a bounded semantic ID")


def _require_path(value: object, name: str) -> JsonPath:
    if type(value) is not tuple or not value or len(value) > 64:
        _fail("INVALID_JSON_PATH", f"{name} must be a bounded exact tuple")
    for token in cast(tuple[object, ...], value):
        if type(token) is int and cast(int, token) >= 0:
            continue
        if type(token) is str and 0 < len(cast(str, token)) <= 256:
            continue
        _fail("INVALID_JSON_PATH", f"{name} contains an invalid token")
    return cast(JsonPath, value)


def _path_text(path: JsonPath) -> str:
    rendered = "$"
    for token in path:
        rendered += f"[{token}]" if type(token) is int else f".{token}"
    return rendered


def _path_is_prefix(prefix: JsonPath, path: JsonPath) -> bool:
    return len(prefix) <= len(path) and path[: len(prefix)] == prefix


def _exact_json_copy(
    value: object, *, path: str = "$", active: set[int] | None = None
) -> JsonValue:
    """Copy only an acyclic finite tree of exact built-in JSON types."""

    if active is None:
        active = set()
    value_type = type(value)
    if value is None or value_type in {bool, int, str}:
        return cast(JsonValue, value)
    if value_type is float:
        if not math.isfinite(cast(float, value)):
            _fail("NON_CANONICAL_JSON", f"non-finite number at {path}")
        return cast(float, value)
    if value_type not in {list, dict}:
        _fail("NON_CANONICAL_JSON", f"non-exact JSON type at {path}")
    identity = id(value)
    if identity in active:
        _fail("NON_CANONICAL_JSON", f"cyclic JSON container at {path}")
    active.add(identity)
    try:
        if value_type is list:
            return [
                _exact_json_copy(item, path=f"{path}[{index}]", active=active)
                for index, item in enumerate(cast(list[object], value))
            ]
        result: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                _fail("NON_CANONICAL_JSON", f"non-string key at {path}")
            result[cast(str, key)] = _exact_json_copy(item, path=f"{path}.{key}", active=active)
        return result
    finally:
        active.remove(identity)


def _canonical_bytes(value: object) -> bytes:
    copied = _exact_json_copy(value)
    return json.dumps(
        copied,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("NON_CANONICAL_JSON", "duplicate JSON object key")
        result[key] = value
    return result


def _parse_canonical_bytes(value: bytes) -> JsonValue:
    if type(value) is not bytes:
        _fail("UNTRUSTED_RUNTIME_TYPE", "canonical snapshot must use exact bytes")
    try:
        decoded = json.loads(value, object_pairs_hook=_reject_duplicate_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise R22ContractError("NON_CANONICAL_JSON", "snapshot is invalid JSON") from exc
    copied = _exact_json_copy(decoded)
    if _canonical_bytes(copied) != value:
        _fail("NON_CANONICAL_JSON", "snapshot bytes are not canonical")
    return copied


def _get_at_path(root: JsonValue, path: JsonPath) -> JsonValue:
    _require_path(path, "request path")
    node = root
    for token in path:
        if type(token) is int and type(node) is list:
            index = cast(int, token)
            if index >= len(node):
                _fail("REQUEST_PATH_NOT_FOUND", f"index missing at {_path_text(path)}")
            node = node[index]
        elif type(token) is str and type(node) is dict and token in node:
            node = node[token]
        else:
            _fail("REQUEST_PATH_NOT_FOUND", f"path missing at {_path_text(path)}")
    return node


def _set_at_path(root: JsonValue, path: JsonPath, value: JsonValue) -> None:
    _require_path(path, "request path")
    parent = _get_at_path(root, path[:-1]) if len(path) > 1 else root
    token = path[-1]
    if type(token) is int and type(parent) is list:
        index = cast(int, token)
        if index >= len(parent):
            _fail("REQUEST_PATH_NOT_FOUND", "list assignment index is out of bounds")
        parent[index] = value
        return
    if type(token) is str and type(parent) is dict and token in parent:
        parent[token] = value
        return
    _fail("REQUEST_PATH_NOT_FOUND", "request path cannot be assigned")


@dataclass(frozen=True, slots=True)
class RuntimeTextDiffV1:
    operation_id: str
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    original_text: str
    rendered_text: str
    original_sha256: str
    rendered_sha256: str
    mapping_kind: RuntimeMappingKind = RuntimeMappingKind.DELETED

    def __post_init__(self) -> None:
        _require_semantic_id(self.operation_id, "operation_id")
        _require_path(self.container_path, "diff container_path")
        if (
            type(self.source_char_start) is not int
            or type(self.source_char_end) is not int
            or self.source_char_start < 0
            or self.source_char_end <= self.source_char_start
        ):
            _fail("INVALID_RENDER_DIFF", "diff offsets must be a non-empty range")
        if type(self.original_text) is not str or not self.original_text:
            _fail("INVALID_RENDER_DIFF", "diff original_text must be non-empty")
        if type(self.rendered_text) is not str or self.rendered_text:
            _fail("INVALID_RENDER_DIFF", "runtime target rendering is deletion-only")
        _require_sha256(self.original_sha256, "original_sha256")
        _require_sha256(self.rendered_sha256, "rendered_sha256")
        if _text_sha256(self.original_text) != self.original_sha256:
            _fail("DIFF_HASH_MISMATCH", "original diff text hash differs")
        if _text_sha256(self.rendered_text) != self.rendered_sha256:
            _fail("DIFF_HASH_MISMATCH", "rendered diff text hash differs")
        if (
            type(self.mapping_kind) is not RuntimeMappingKind
            or self.mapping_kind is not RuntimeMappingKind.DELETED
        ):
            _fail("INVALID_RENDER_DIFF", "runtime text edits use exact DELETED mapping")


@dataclass(frozen=True, slots=True)
class RuntimeSourceMappingV1:
    container_path: JsonPath
    source_char_start: int
    source_char_end: int
    rendered_char_start: int
    rendered_char_end: int
    kind: RuntimeMappingKind
    operation_id: str | None

    def __post_init__(self) -> None:
        _require_path(self.container_path, "mapping container_path")
        for value, name in (
            (self.source_char_start, "source_char_start"),
            (self.source_char_end, "source_char_end"),
            (self.rendered_char_start, "rendered_char_start"),
            (self.rendered_char_end, "rendered_char_end"),
        ):
            if type(value) is not int or value < 0:
                _fail("INVALID_SOURCE_MAPPING", f"{name} must be non-negative")
        if (
            self.source_char_end < self.source_char_start
            or self.rendered_char_end < self.rendered_char_start
        ):
            _fail("INVALID_SOURCE_MAPPING", "mapping offsets are inverted")
        if type(self.kind) is not RuntimeMappingKind:
            _fail("UNTRUSTED_RUNTIME_TYPE", "mapping kind must use exact runtime enum")
        if self.kind is RuntimeMappingKind.COPIED:
            if self.operation_id is not None:
                _fail("INVALID_SOURCE_MAPPING", "COPIED mapping cannot bind an operation")
            if (
                self.source_char_end - self.source_char_start
                != self.rendered_char_end - self.rendered_char_start
            ):
                _fail("INVALID_SOURCE_MAPPING", "COPIED mapping widths must match")
        else:
            _require_semantic_id(self.operation_id, "mapping operation_id")
            if self.rendered_char_start != self.rendered_char_end:
                _fail("INVALID_SOURCE_MAPPING", "DELETED mapping renders zero characters")


@dataclass(frozen=True, slots=True)
class RuntimeListInsertionDiffV1:
    operation_id: str
    container_path: JsonPath
    source_index: int
    rendered_index: int
    inserted_value_canonical_bytes: bytes
    inserted_value_sha256: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.operation_id, "operation_id")
        _require_path(self.container_path, "insertion container_path")
        if (
            type(self.source_index) is not int
            or type(self.rendered_index) is not int
            or self.source_index < 0
            or self.rendered_index < 0
        ):
            _fail("INVALID_LIST_INSERTION", "insertion indexes must be non-negative")
        inserted = _parse_canonical_bytes(self.inserted_value_canonical_bytes)
        _require_sha256(self.inserted_value_sha256, "inserted_value_sha256")
        if _canonical_sha256(inserted) != self.inserted_value_sha256:
            _fail("INSERTION_HASH_MISMATCH", "inserted value hash differs")

    @property
    def inserted_value(self) -> JsonValue:
        return _parse_canonical_bytes(self.inserted_value_canonical_bytes)


def runtime_text_diff_projection(value: RuntimeTextDiffV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeTextDiffV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "text diff must use exact trusted type")
    return {
        "operation_id": value.operation_id,
        "container_path": list(value.container_path),
        "source_char_start": value.source_char_start,
        "source_char_end": value.source_char_end,
        "original_text": value.original_text,
        "rendered_text": value.rendered_text,
        "original_sha256": value.original_sha256,
        "rendered_sha256": value.rendered_sha256,
        "mapping_kind": value.mapping_kind.value,
    }


def runtime_source_mapping_projection(value: RuntimeSourceMappingV1) -> dict[str, JsonValue]:
    if type(value) is not RuntimeSourceMappingV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "source mapping must use exact trusted type")
    return {
        "container_path": list(value.container_path),
        "source_char_start": value.source_char_start,
        "source_char_end": value.source_char_end,
        "rendered_char_start": value.rendered_char_start,
        "rendered_char_end": value.rendered_char_end,
        "kind": value.kind.value,
        "operation_id": value.operation_id,
    }


def runtime_list_insertion_projection(
    value: RuntimeListInsertionDiffV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeListInsertionDiffV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "list insertion must use exact trusted type")
    return {
        "operation_id": value.operation_id,
        "container_path": list(value.container_path),
        "source_index": value.source_index,
        "rendered_index": value.rendered_index,
        "inserted_value": value.inserted_value,
        "inserted_value_sha256": value.inserted_value_sha256,
    }


def _exact_diff_projection(
    diffs: tuple[RuntimeTextDiffV1, ...],
    insertions: tuple[RuntimeListInsertionDiffV1, ...],
    mappings: tuple[RuntimeSourceMappingV1, ...],
) -> dict[str, JsonValue]:
    if type(diffs) is not tuple or any(type(item) is not RuntimeTextDiffV1 for item in diffs):
        _fail("UNTRUSTED_RUNTIME_TYPE", "text diffs must be an exact trusted tuple")
    if type(insertions) is not tuple or any(
        type(item) is not RuntimeListInsertionDiffV1 for item in insertions
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "insertions must be an exact trusted tuple")
    if type(mappings) is not tuple or any(
        type(item) is not RuntimeSourceMappingV1 for item in mappings
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "source mappings must be an exact trusted tuple")
    return {
        "text_diffs": [runtime_text_diff_projection(item) for item in diffs],
        "list_insertions": [runtime_list_insertion_projection(item) for item in insertions],
        "source_mappings": [runtime_source_mapping_projection(item) for item in mappings],
    }


@dataclass(frozen=True, slots=True)
class RuntimeRenderResultV1:
    """Immutable request snapshots plus exact reversible SHADOW diff metadata."""

    source_request_canonical_bytes: bytes
    candidate_request_canonical_bytes: bytes
    source_request_sha256: str
    candidate_request_sha256: str
    admitted_plan_sha256: str
    exact_diff_sha256: str
    text_diffs: tuple[RuntimeTextDiffV1, ...]
    list_insertions: tuple[RuntimeListInsertionDiffV1, ...]
    source_mappings: tuple[RuntimeSourceMappingV1, ...]
    validation_checks: tuple[str, ...]
    schema_version: str = RUNTIME_RENDER_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != RUNTIME_RENDER_RESULT_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown runtime render-result schema")
        source = _parse_canonical_bytes(self.source_request_canonical_bytes)
        candidate = _parse_canonical_bytes(self.candidate_request_canonical_bytes)
        for digest, name in (
            (self.source_request_sha256, "source_request_sha256"),
            (self.candidate_request_sha256, "candidate_request_sha256"),
            (self.admitted_plan_sha256, "admitted_plan_sha256"),
            (self.exact_diff_sha256, "exact_diff_sha256"),
        ):
            _require_sha256(digest, name)
        if _canonical_sha256(source) != self.source_request_sha256:
            _fail("SOURCE_REQUEST_HASH_MISMATCH", "source snapshot hash differs")
        if _canonical_sha256(candidate) != self.candidate_request_sha256:
            _fail("CANDIDATE_REQUEST_HASH_MISMATCH", "candidate snapshot hash differs")
        exact_diff = _exact_diff_projection(
            self.text_diffs, self.list_insertions, self.source_mappings
        )
        if _canonical_sha256(exact_diff) != self.exact_diff_sha256:
            _fail("EXACT_DIFF_HASH_MISMATCH", "exact diff projection hash differs")
        if type(self.validation_checks) is not tuple or not self.validation_checks:
            _fail("VALIDATION_CHECKS_MISSING", "render result needs validation checks")
        if any(
            type(item) is not str or _CHECK_CODE.fullmatch(item) is None
            for item in self.validation_checks
        ):
            _fail("INVALID_VALIDATION_CHECK", "render checks must use closed codes")
        if len(self.validation_checks) != len(set(self.validation_checks)):
            _fail("DUPLICATE_VALIDATION_CHECK", "render checks must be unique")

    @property
    def original_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.source_request_canonical_bytes)

    @property
    def candidate_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.candidate_request_canonical_bytes)

    @property
    def rendered_request(self) -> JsonValue:
        """Compatibility name for seam-side candidate selection."""

        return self.candidate_request

    @property
    def rendered_request_sha256(self) -> str:
        return self.candidate_request_sha256

    @property
    def diffs(self) -> tuple[RuntimeTextDiffV1, ...]:
        return self.text_diffs

    @property
    def edit_applied(self) -> bool:
        return bool(self.text_diffs or self.list_insertions)


def runtime_render_result_projection(value: RuntimeRenderResultV1) -> dict[str, JsonValue]:
    """Return the hash-only safe projection; request/diff preimages stay in memory."""

    if type(value) is not RuntimeRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use exact trusted type")
    return {
        "schema_version": value.schema_version,
        "source_request_sha256": value.source_request_sha256,
        "candidate_request_sha256": value.candidate_request_sha256,
        "admitted_plan_sha256": value.admitted_plan_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "text_diff_count": len(value.text_diffs),
        "list_insertion_count": len(value.list_insertions),
        "source_mapping_count": len(value.source_mappings),
        "edit_applied": value.edit_applied,
        "validation_checks": list(value.validation_checks),
    }


def runtime_render_result_sha256(value: RuntimeRenderResultV1) -> str:
    return _canonical_sha256(runtime_render_result_projection(value))


def _exact_object(value: object, keys: frozenset[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact object")
    projected = cast(dict[object, object], value)
    if any(type(key) is not str for key in projected):
        _fail("NON_CANONICAL_JSON", f"{name} contains a non-string key")
    if set(cast(dict[str, object], projected)) != keys:
        _fail("PROPOSAL_SHAPE_MISMATCH", f"{name} fields differ from the closed schema")
    return cast(dict[str, object], projected)


def _exact_array(value: object, name: str, *, maximum: int) -> list[object]:
    if type(value) is not list:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact array")
    items = cast(list[object], value)
    if len(items) > maximum:
        _fail("PROPOSAL_SIZE_EXCEEDED", f"{name} exceeds its schema bound")
    return items


def _exact_string(value: object, name: str) -> str:
    if type(value) is not str:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact string")
    return cast(str, value)


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact boolean")
    return cast(bool, value)


def _exact_int(value: object, name: str) -> int:
    if type(value) is not int:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} must be an exact integer")
    return cast(int, value)


def _enum_value[EnumT: StrEnum](value: object, enum_type: type[EnumT], name: str) -> EnumT:
    text = _exact_string(value, name)
    try:
        result = enum_type(text)
    except ValueError as exc:
        raise R22ContractError("UNKNOWN_ENUM_VALUE", f"{name} is not recognized") from exc
    if type(result) is not enum_type:
        _fail("UNTRUSTED_RUNTIME_TYPE", f"{name} did not create an exact enum")
    return result


_PROPOSAL_KEYS = frozenset(
    {
        "schema_version",
        "packet_id",
        "evidence_packet_sha256",
        "status",
        "automatic",
        "curated",
        "deployment_prediction",
        "action_or_tool_authority",
        "decisions",
    }
)
_DECISION_KEYS = frozenset(
    {
        "decision_id",
        "target_id",
        "factual_verdict",
        "temporal_validity",
        "proposed_operation",
        "evidence_refs",
        "confidence_millis",
        "reason_code",
        "uncertainty_codes",
        "rationale_summary",
        "replacement_fact_id",
        "fallback_status",
    }
)
_EVIDENCE_REF_KEYS = frozenset({"evidence_id", "payload_sha256", "relation"})


def parse_runtime_policy_proposal(value: object) -> RuntimePolicyProposalV1:
    """Parse a schema-shaped JSON object without invoking virtual serializers."""

    root = _exact_object(value, _PROPOSAL_KEYS, "policy proposal")
    schema_version = _exact_string(root["schema_version"], "schema_version")
    if schema_version != POLICY_PROPOSAL_SCHEMA_VERSION:
        _fail("UNKNOWN_SCHEMA_VERSION", "proposal schema version is not R2.2 v1")
    decisions: list[RuntimeClaimProposalV1] = []
    for decision_index, decision_value in enumerate(
        _exact_array(root["decisions"], "decisions", maximum=256)
    ):
        decision = _exact_object(
            decision_value,
            _DECISION_KEYS,
            f"decisions[{decision_index}]",
        )
        evidence_refs: list[ProposalEvidenceRefV1] = []
        for ref_index, ref_value in enumerate(
            _exact_array(
                decision["evidence_refs"],
                f"decisions[{decision_index}].evidence_refs",
                maximum=32,
            )
        ):
            ref = _exact_object(
                ref_value,
                _EVIDENCE_REF_KEYS,
                f"decisions[{decision_index}].evidence_refs[{ref_index}]",
            )
            evidence_refs.append(
                ProposalEvidenceRefV1(
                    evidence_id=_exact_string(ref["evidence_id"], "evidence_id"),
                    payload_sha256=_exact_string(ref["payload_sha256"], "payload_sha256"),
                    relation=_enum_value(ref["relation"], EvidenceRelation, "evidence relation"),
                )
            )
        uncertainty_codes = tuple(
            _enum_value(item, RuntimeUncertaintyCode, "uncertainty code")
            for item in _exact_array(
                decision["uncertainty_codes"],
                f"decisions[{decision_index}].uncertainty_codes",
                maximum=16,
            )
        )
        replacement_value = decision["replacement_fact_id"]
        replacement_fact_id = (
            None
            if replacement_value is None
            else _exact_string(replacement_value, "replacement_fact_id")
        )
        decisions.append(
            RuntimeClaimProposalV1(
                decision_id=_exact_string(decision["decision_id"], "decision_id"),
                target_id=_exact_string(decision["target_id"], "target_id"),
                factual_verdict=_enum_value(
                    decision["factual_verdict"], FactualVerdict, "factual_verdict"
                ),
                temporal_validity=_enum_value(
                    decision["temporal_validity"],
                    TemporalValidity,
                    "temporal_validity",
                ),
                proposed_operation=_enum_value(
                    decision["proposed_operation"],
                    RuntimeOperationKind,
                    "proposed_operation",
                ),
                evidence_refs=tuple(evidence_refs),
                confidence_millis=_exact_int(decision["confidence_millis"], "confidence_millis"),
                reason_code=_enum_value(decision["reason_code"], RuntimeReasonCode, "reason_code"),
                uncertainty_codes=uncertainty_codes,
                rationale_summary=_exact_string(decision["rationale_summary"], "rationale_summary"),
                replacement_fact_id=replacement_fact_id,
                fallback_status=_enum_value(
                    decision["fallback_status"],
                    RuntimeFallbackStatus,
                    "fallback_status",
                ),
            )
        )
    return RuntimePolicyProposalV1(
        packet_id=_exact_string(root["packet_id"], "packet_id"),
        evidence_packet_sha256=_exact_string(
            root["evidence_packet_sha256"], "evidence_packet_sha256"
        ),
        status=_enum_value(root["status"], RuntimeProposalStatus, "status"),
        decisions=tuple(decisions),
        automatic=_exact_bool(root["automatic"], "automatic"),
        curated=_exact_bool(root["curated"], "curated"),
        deployment_prediction=_exact_bool(root["deployment_prediction"], "deployment_prediction"),
        action_or_tool_authority=_exact_bool(
            root["action_or_tool_authority"], "action_or_tool_authority"
        ),
        schema_version=schema_version,
    )


def _correction_anchor_projection(value: CorrectionAnchor) -> dict[str, JsonValue]:
    if type(value) is not CorrectionAnchor:
        _fail("UNTRUSTED_RUNTIME_TYPE", "correction anchor must use exact trusted type")
    if (
        type(value.placement) is not CorrectionPlacement
        or type(value.context_kind) is not CorrectionContextKind
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "correction anchor enums must be exact")
    return {
        "container_path": list(_require_path(value.container_path, "anchor container_path")),
        "insert_index": value.insert_index,
        "source_container_sha256": value.source_container_sha256,
        "owner_region_id": value.owner_region_id,
        "host_context_path": list(
            _require_path(value.host_context_path, "anchor host_context_path")
        ),
        "host_context_sha256": value.host_context_sha256,
        "role_path": list(_require_path(value.role_path, "anchor role_path")),
        "expected_role": value.expected_role,
        "reference_path": list(_require_path(value.reference_path, "anchor reference_path")),
        "reference_sha256": value.reference_sha256,
        "placement": value.placement.value,
        "context_kind": value.context_kind.value,
        "visible_prefix": value.visible_prefix,
        "visible_suffix": value.visible_suffix,
    }


def _correction_anchor_sha256(value: CorrectionAnchor) -> str:
    return _canonical_sha256(_correction_anchor_projection(value))


def _validate_source_span(request: JsonValue, span: SourceSpan) -> str:
    if type(span) is not SourceSpan:
        _fail("UNTRUSTED_RUNTIME_TYPE", "source span must use exact trusted type")
    path = _require_path(span.container_path, "source span container_path")
    if type(span.span_role) is not SpanRole:
        _fail("UNTRUSTED_RUNTIME_TYPE", "source span role must use exact enum")
    container = _get_at_path(request, path)
    if type(container) is not str:
        _fail("SPAN_CONTAINER_NOT_TEXT", "source span container is not exact text")
    text = cast(str, container)
    for offset, name in (
        (span.char_start, "char_start"),
        (span.char_end, "char_end"),
        (span.utf8_byte_start, "utf8_byte_start"),
        (span.utf8_byte_end, "utf8_byte_end"),
    ):
        if type(offset) is not int or offset < 0:
            _fail("INVALID_SOURCE_SPAN", f"{name} must be a non-negative integer")
    if span.char_end <= span.char_start or span.char_end > len(text):
        _fail("INVALID_SOURCE_SPAN", "source span character range is invalid")
    selected = text[span.char_start : span.char_end]
    if type(span.exact_text) is not str or selected != span.exact_text:
        _fail("SOURCE_SPAN_DRIFT", "source span text differs from request")
    _require_sha256(span.span_sha256, "source span_sha256")
    if _text_sha256(selected) != span.span_sha256:
        _fail("SOURCE_SPAN_DRIFT", "source span hash differs from request")
    if (
        len(text[: span.char_start].encode("utf-8")) != span.utf8_byte_start
        or len(text[: span.char_end].encode("utf-8")) != span.utf8_byte_end
    ):
        _fail("UTF8_OFFSET_DRIFT", "source span byte offsets differ")
    return text


def _span_matches_target(span: SourceSpan, target: EligibleHistoryTargetV1) -> bool:
    return (
        type(span) is SourceSpan
        and span.container_path == target.container_path
        and span.char_start == target.char_start
        and span.char_end == target.char_end
        and span.utf8_byte_start == target.utf8_byte_start
        and span.utf8_byte_end == target.utf8_byte_end
        and span.exact_text == target.exact_text
        and span.span_sha256 == target.span_sha256
        and span.claim_id == target.claim_id
        and type(span.span_role) is SpanRole
        and span.span_role is SpanRole.EDITABLE_CLAIM
    )


def _history_region_for_record(ir: HistoryIR, record: HistoryRecord) -> RequestRegion:
    if type(ir.regions) is not tuple:
        _fail("UNTRUSTED_RUNTIME_TYPE", "History IR regions must be an exact tuple")
    matches = [
        region
        for region in ir.regions
        if type(region) is RequestRegion and region.region_id == record.region_id
    ]
    if len(matches) != 1:
        _fail("UNKNOWN_OR_DUPLICATE_REGION", "record region must resolve exactly once")
    region = matches[0]
    if type(region.kind) is not RegionKind or region.kind is not RegionKind.HISTORY:
        _fail("NON_HISTORY_TARGET_FORBIDDEN", "runtime target is outside history")
    if (
        type(region.availability) is not RegionAvailability
        or region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    ):
        _fail("NON_HISTORY_TARGET_FORBIDDEN", "runtime target history region is absent")
    if type(region.paths) is not tuple or type(region.text_slices) is not tuple:
        _fail("UNTRUSTED_RUNTIME_TYPE", "history region locators must be exact tuples")
    return region


def _span_inside_region(span: SourceSpan, region: RequestRegion) -> bool:
    if any(_path_is_prefix(path, span.container_path) for path in region.paths):
        return True
    return any(
        text_slice.container_path == span.container_path
        and text_slice.char_start <= span.char_start
        and span.char_end <= text_slice.char_end
        for text_slice in region.text_slices
    )


def _resolve_target(
    request: JsonValue,
    ir: HistoryIR,
    target: EligibleHistoryTargetV1,
) -> tuple[HistoryRecord, SourceSpan]:
    if type(target) is not EligibleHistoryTargetV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "target must use exact trusted type")
    if target.span_role is not RuntimeTargetSpanRole.EDITABLE_CLAIM:
        _fail("NON_EDITABLE_RUNTIME_TARGET", "R2.2 material targets must be editable claims")
    if target.source_request_sha256 != ir.raw_request_sha256:
        _fail("TARGET_REQUEST_BINDING_MISMATCH", "target binds another request")
    if type(ir.records) is not tuple or any(type(item) is not HistoryRecord for item in ir.records):
        _fail("UNTRUSTED_RUNTIME_TYPE", "History IR records must use exact trusted types")
    matches = [record for record in ir.records if record.record_id == target.record_id]
    if len(matches) != 1:
        _fail("UNKNOWN_OR_DUPLICATE_RECORD", "target record must resolve exactly once")
    record = matches[0]
    if record.record_sha256 != target.record_sha256:
        _fail("TARGET_RECORD_HASH_MISMATCH", "target record hash differs")
    region = _history_region_for_record(ir, record)
    if type(record.editable_spans) is not tuple or type(record.protected_spans) is not tuple:
        _fail("UNTRUSTED_RUNTIME_TYPE", "record spans must be exact tuples")
    if any(
        type(item) is not SourceSpan for item in (*record.editable_spans, *record.protected_spans)
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "record span values must use exact trusted types")
    spans = [span for span in record.editable_spans if _span_matches_target(span, target)]
    if len(spans) != 1:
        _fail("TARGET_SPAN_BINDING_MISMATCH", "target span must resolve exactly once")
    span = spans[0]
    _validate_source_span(request, span)
    if not _span_inside_region(span, region):
        _fail("NON_HISTORY_TARGET_FORBIDDEN", "target span is outside its history region")
    for protected in record.protected_spans:
        _validate_source_span(request, protected)
        if (
            protected.container_path == span.container_path
            and span.char_start < protected.char_end
            and protected.char_start < span.char_end
        ):
            _fail("TARGET_PROTECTED_OVERLAP", "target overlaps protected history bytes")
    return record, span


def _validate_packet_target_bindings(
    request: JsonValue, ir: HistoryIR, packet: EvidencePacketV1
) -> dict[str, tuple[HistoryRecord, SourceSpan]]:
    resolved: dict[str, tuple[HistoryRecord, SourceSpan]] = {}
    locations: list[tuple[JsonPath, int, int]] = []
    for target in packet.targets:
        record, span = _resolve_target(request, ir, target)
        for path, start, end in locations:
            if path == span.container_path and span.char_start < end and start < span.char_end:
                _fail("AMBIGUOUS_TARGET_OVERLAP", "eligible packet targets overlap")
        locations.append((span.container_path, span.char_start, span.char_end))
        resolved[target.target_id] = (record, span)
    return resolved


def _validate_ir_packet_binding(
    request: JsonValue, ir: HistoryIR, packet: EvidencePacketV1
) -> dict[str, tuple[HistoryRecord, SourceSpan]]:
    if type(ir) is not HistoryIR or type(packet) is not EvidencePacketV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "IR and packet must use exact trusted types")
    if type(ir.history_family) is not HistoryFamily:
        _fail("UNTRUSTED_RUNTIME_TYPE", "History IR family must use exact enum")
    request_hash = _canonical_sha256(request)
    if request_hash != packet.raw_request_sha256 or request_hash != ir.raw_request_sha256:
        _fail("SOURCE_REQUEST_BINDING_MISMATCH", "packet or IR binds another request")
    if packet.host_id != ir.host_id:
        _fail("HOST_BINDING_MISMATCH", "packet and IR hosts differ")
    if packet.history_codec_id != ir.codec_id:
        _fail("CODEC_BINDING_MISMATCH", "packet and IR codecs differ")
    if packet.codec_contract_version != ir.codec_contract_version:
        _fail("CODEC_VERSION_BINDING_MISMATCH", "packet and IR codec versions differ")
    return _validate_packet_target_bindings(request, ir, packet)


def _validate_correction_anchor(
    request: JsonValue,
    ir: HistoryIR,
    anchor: CorrectionAnchor,
) -> None:
    projection = _correction_anchor_projection(anchor)
    del projection
    if type(anchor.insert_index) is not int or anchor.insert_index < 0:
        _fail("CORRECTION_ANCHOR_OUT_OF_BOUNDS", "anchor index is invalid")
    for digest, name in (
        (anchor.source_container_sha256, "source_container_sha256"),
        (anchor.host_context_sha256, "host_context_sha256"),
        (anchor.reference_sha256, "reference_sha256"),
    ):
        _require_sha256(digest, name)
    if type(anchor.owner_region_id) is not str or not anchor.owner_region_id:
        _fail("INVALID_CORRECTION_ANCHOR", "owner_region_id must be non-empty")
    if type(anchor.expected_role) is not str or anchor.expected_role != "user":
        _fail(
            "CORRECTION_ANCHOR_ACTOR_OWNED",
            "correction context must be Sentinel data in a user context",
        )
    if (
        type(anchor.visible_prefix) is not str
        or "SENTINEL" not in anchor.visible_prefix
        or type(anchor.visible_suffix) is not str
    ):
        _fail("INVALID_CORRECTION_ANCHOR", "correction must be visibly Sentinel-authored")
    regions = [
        region
        for region in ir.regions
        if type(region) is RequestRegion and region.region_id == anchor.owner_region_id
    ]
    if len(regions) != 1:
        _fail("UNKNOWN_CORRECTION_REGION", "anchor owner region must resolve once")
    region = regions[0]
    if (
        type(region.kind) is not RegionKind
        or region.kind is not RegionKind.CURRENT_OBSERVATION
        or type(region.availability) is not RegionAvailability
        or region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
    ):
        _fail(
            "CORRECTION_ANCHOR_REGION_INVALID",
            "correction must be in the current-observation region",
        )
    if type(region.paths) is not tuple or type(region.text_slices) is not tuple:
        _fail("UNTRUSTED_RUNTIME_TYPE", "correction owner region is untrusted")
    if not any(
        _path_is_prefix(path, anchor.container_path) or _path_is_prefix(anchor.container_path, path)
        for path in region.paths
    ) and not any(
        _path_is_prefix(text_slice.container_path, anchor.container_path)
        or _path_is_prefix(anchor.container_path, text_slice.container_path)
        for text_slice in region.text_slices
    ):
        _fail("CORRECTION_ANCHOR_REGION_MISMATCH", "anchor is outside current observation")
    if anchor.context_kind is CorrectionContextKind.TEXT_CONTENT_BLOCK:
        context_path_valid = _path_is_prefix(anchor.host_context_path, anchor.container_path)
    elif anchor.context_kind is CorrectionContextKind.CHAT_MESSAGE:
        context_path_valid = _path_is_prefix(anchor.container_path, anchor.host_context_path)
    else:
        _fail("UNKNOWN_CORRECTION_CONTEXT", "correction context kind is unsupported")
    if not context_path_valid:
        _fail("CORRECTION_CONTEXT_PATH_MISMATCH", "anchor is outside host context")
    if (
        _canonical_sha256(_get_at_path(request, anchor.host_context_path))
        != anchor.host_context_sha256
    ):
        _fail("CORRECTION_CONTEXT_DRIFT", "correction host context changed")
    if not _path_is_prefix(anchor.host_context_path, anchor.role_path):
        _fail("CORRECTION_ROLE_PATH_MISMATCH", "role path is outside host context")
    if _get_at_path(request, anchor.role_path) != "user":
        _fail("CORRECTION_ANCHOR_ACTOR_OWNED", "anchor role is not exact user context")
    if (
        len(anchor.reference_path) != len(anchor.container_path) + 1
        or anchor.reference_path[:-1] != anchor.container_path
        or type(anchor.reference_path[-1]) is not int
    ):
        _fail(
            "CORRECTION_REFERENCE_PATH_MISMATCH",
            "reference must be one exact item in insertion container",
        )
    reference_index = cast(int, anchor.reference_path[-1])
    expected_index = (
        reference_index if anchor.placement is CorrectionPlacement.BEFORE else reference_index + 1
    )
    if anchor.insert_index != expected_index:
        _fail(
            "CORRECTION_INSERTION_COORDINATE_MISMATCH",
            "anchor index differs from reference placement",
        )
    source_container = _get_at_path(request, anchor.container_path)
    if type(source_container) is not list:
        _fail("CORRECTION_ANCHOR_NOT_LIST", "correction container is not an exact array")
    if _canonical_sha256(source_container) != anchor.source_container_sha256:
        _fail("CORRECTION_ANCHOR_DRIFT", "correction source container changed")
    if not 0 <= reference_index < len(source_container) or not 0 <= anchor.insert_index <= len(
        source_container
    ):
        _fail("CORRECTION_ANCHOR_OUT_OF_BOUNDS", "correction coordinate is out of bounds")
    reference = _get_at_path(request, anchor.reference_path)
    if _canonical_sha256(reference) != anchor.reference_sha256:
        _fail("CORRECTION_REFERENCE_DRIFT", "correction reference changed")
    if not any(_path_is_prefix(path, anchor.reference_path) for path in region.paths) and not any(
        text_slice.container_path == anchor.reference_path for text_slice in region.text_slices
    ):
        _fail(
            "CORRECTION_REFERENCE_REGION_MISMATCH",
            "correction reference is outside current observation",
        )
    for record in ir.records:
        if type(record) is not HistoryRecord or type(record.source_span) is not SourceSpan:
            _fail("UNTRUSTED_RUNTIME_TYPE", "History IR record envelope is untrusted")
        record_path = record.source_span.container_path
        if _path_is_prefix(record_path, anchor.container_path):
            _fail(
                "CORRECTION_ANCHOR_ACTOR_OWNED",
                "correction container cannot be nested inside historical actor text",
            )
        if _path_is_prefix(anchor.container_path, record_path):
            child_token = record_path[len(anchor.container_path)]
            if type(child_token) is not int or anchor.insert_index <= child_token:
                _fail(
                    "CORRECTION_PRECEDES_HISTORY",
                    "Sentinel correction must follow any history block in a shared list",
                )


def _resolve_correction_anchor(
    request: JsonValue,
    ir: HistoryIR,
    record: HistoryRecord,
    *,
    expected_sha256: str | None = None,
) -> CorrectionAnchor:
    if type(record.correction_anchors) is not tuple or any(
        type(item) is not CorrectionAnchor for item in record.correction_anchors
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "record correction anchors are untrusted")
    candidates = list(record.correction_anchors)
    if expected_sha256 is not None:
        _require_sha256(expected_sha256, "correction_anchor_sha256")
        candidates = [
            item for item in candidates if _correction_anchor_sha256(item) == expected_sha256
        ]
    if len(candidates) != 1:
        _fail("AMBIGUOUS_CORRECTION_ANCHOR", "replacement anchor must resolve exactly once")
    anchor = candidates[0]
    _validate_correction_anchor(request, ir, anchor)
    return anchor


def _validate_semantic_evidence_basis(
    packet: EvidencePacketV1,
    proposal: RuntimePolicyProposalV1,
) -> None:
    evidence = {item.evidence_id: item for item in packet.evidence_index}
    weak_material_roles = {
        EvidenceRole.CURRENT_UI_SCREENSHOT,
        EvidenceRole.CURRENT_ACCESSIBILITY,
        EvidenceRole.PRIOR_ACTION_ATTEMPT,
        EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
    }
    for decision in proposal.decisions:
        if decision.proposed_operation is RuntimeOperationKind.KEEP:
            supporting = [
                evidence[item.evidence_id]
                for item in decision.evidence_refs
                if item.relation is EvidenceRelation.SUPPORTS
            ]
            if not supporting:
                _fail("SUPPORTING_EVIDENCE_MISSING", "KEEP needs explicit supporting evidence")
            if {item.role for item in supporting} <= {
                EvidenceRole.PRIOR_ACTION_ATTEMPT,
                EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
            }:
                _fail(
                    "EXECUTOR_STATUS_ONLY",
                    "action/executor success alone cannot establish semantic support",
                )
            if decision.reason_code is not RuntimeReasonCode.DIRECT_EVIDENCE_SUPPORT:
                _fail("REASON_CODE_MISMATCH", "KEEP reason must bind direct support")
            continue
        if decision.proposed_operation is RuntimeOperationKind.KEEP_UNCERTAIN:
            continue
        decisive_relations = (
            {EvidenceRelation.REFUTES}
            if decision.factual_verdict is FactualVerdict.REFUTED
            else {EvidenceRelation.INVALIDATES}
        )
        decisive = [
            evidence[item.evidence_id]
            for item in decision.evidence_refs
            if item.relation in decisive_relations
        ]
        if not decisive:
            _fail("DECISIVE_EVIDENCE_MISSING", "material edit lacks decisive evidence")
        if {item.role for item in decisive} <= weak_material_roles:
            _fail(
                "WEAK_MATERIAL_EVIDENCE",
                "current-screen absence/action/executor status cannot alone authorize an edit",
            )
        if decision.factual_verdict is FactualVerdict.REFUTED:
            if decision.reason_code is not RuntimeReasonCode.DIRECT_EVIDENCE_REFUTATION:
                _fail("REASON_CODE_MISMATCH", "refutation reason code differs")
        elif (
            decision.factual_verdict is FactualVerdict.SUPPORTED
            and decision.temporal_validity is TemporalValidity.INVALIDATED
        ):
            target = next(item for item in packet.targets if item.target_id == decision.target_id)
            if target.source_provenance.status is TemporalProvenanceStatus.UNAVAILABLE:
                _fail(
                    "TEMPORAL_PROVENANCE_MISSING",
                    "invalidation cannot be admitted without target provenance",
                )
            if decision.reason_code is not RuntimeReasonCode.LATER_EVIDENCE_INVALIDATES:
                _fail("REASON_CODE_MISMATCH", "invalidation reason code differs")
        else:
            _fail("MATERIAL_OPERATION_MAPPING_INVALID", "material decision basis is invalid")


def _operation_id(
    decision: RuntimeClaimProposalV1,
    target: EligibleHistoryTargetV1,
) -> str:
    seed: dict[str, JsonValue] = {
        "decision_id": decision.decision_id,
        "target_id": target.target_id,
        "target_record_id": target.record_id,
        "target_span_sha256": target.span_sha256,
        "kind": decision.proposed_operation.value,
    }
    return f"r22op:{_canonical_sha256(seed)[:32]}"


def _validate_policy_call_provenance(
    provenance: PolicyCallProvenanceV1,
    packet: EvidencePacketV1,
) -> None:
    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import PolicyCallProvenanceV1

    if type(provenance) is not PolicyCallProvenanceV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "policy provenance must use exact trusted type")
    for digest, name in (
        (provenance.prompt_sha256, "prompt_sha256"),
        (provenance.output_schema_sha256, "output_schema_sha256"),
        (provenance.request_config_sha256, "request_config_sha256"),
        (provenance.evidence_packet_sha256, "evidence_packet_sha256"),
        (provenance.current_image_sha256, "current_image_sha256"),
        (provenance.response_envelope_sha256, "response_envelope_sha256"),
        (provenance.provider_output_sha256, "provider_output_sha256"),
    ):
        _require_sha256(digest, name)
    for value, name in (
        (provenance.policy_id, "policy_id"),
        (provenance.requested_model, "requested_model"),
        (provenance.response_id, "response_id"),
        (provenance.returned_model, "returned_model"),
        (provenance.response_status, "response_status"),
    ):
        _require_semantic_id(value, name)
    if provenance.evidence_packet_sha256 != evidence_packet_sha256(packet):
        _fail("POLICY_PACKET_BINDING_MISMATCH", "policy call evaluated another packet")
    if provenance.current_image_sha256 != packet.current_observation.screenshot_content_sha256:
        _fail("POLICY_IMAGE_BINDING_MISMATCH", "policy call evaluated another image")
    for latency, latency_name in (
        (provenance.packet_build_latency_ns, "packet_build_latency_ns"),
        (provenance.transport_latency_ns, "transport_latency_ns"),
        (provenance.parse_latency_ns, "parse_latency_ns"),
    ):
        if type(latency) is not int or latency < 0:
            _fail("INVALID_POLICY_PROVENANCE", f"{latency_name} must be non-negative")


def proposal_admission(
    packet: EvidencePacketV1,
    proposal_value: object,
    provenance: PolicyCallProvenanceV1,
    *,
    source_request: JsonValue,
    history_ir: HistoryIR,
) -> RuntimeAdmissionBundleV1:
    """Admit an untrusted proposal against one exact packet/request/IR snapshot."""

    if type(packet) is not EvidencePacketV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "packet must use exact trusted type")
    source_snapshot = _exact_json_copy(source_request)
    source_bytes = _canonical_bytes(source_snapshot)
    _validate_policy_call_provenance(provenance, packet)
    resolved = _validate_ir_packet_binding(source_snapshot, history_ir, packet)
    proposal = parse_runtime_policy_proposal(proposal_value)
    proposal_checks = validate_runtime_policy_proposal(proposal, packet)
    _validate_semantic_evidence_basis(packet, proposal)

    targets = {item.target_id: item for item in packet.targets}
    facts = {item.replacement_fact_id: item for item in packet.replacement_facts}
    operations: list[RuntimeAdmittedOperationV1] = []
    for decision in proposal.decisions:
        if decision.proposed_operation not in {
            RuntimeOperationKind.DROP,
            RuntimeOperationKind.REPLACE,
        }:
            continue
        target = targets[decision.target_id]
        record, span = resolved[decision.target_id]
        operation_id = _operation_id(decision, target)
        if decision.proposed_operation is RuntimeOperationKind.DROP:
            operations.append(
                RuntimeAdmittedOperationV1(
                    operation_id=operation_id,
                    decision_id=decision.decision_id,
                    target_id=decision.target_id,
                    target_record_id=record.record_id,
                    target_span_sha256=span.span_sha256,
                    kind=RuntimeOperationKind.DROP,
                    evidence_refs=decision.evidence_refs,
                    reason_code=decision.reason_code,
                )
            )
            continue
        fact = facts.get(cast(str, decision.replacement_fact_id))
        if fact is None or fact.target_id != decision.target_id:
            _fail("REPLACEMENT_FACT_BINDING_MISMATCH", "replacement fact target differs")
        anchor = _resolve_correction_anchor(source_snapshot, history_ir, record)
        operations.append(
            RuntimeAdmittedOperationV1(
                operation_id=operation_id,
                decision_id=decision.decision_id,
                target_id=decision.target_id,
                target_record_id=record.record_id,
                target_span_sha256=span.span_sha256,
                kind=RuntimeOperationKind.REPLACE,
                evidence_refs=decision.evidence_refs,
                reason_code=decision.reason_code,
                replacement_fact_id=fact.replacement_fact_id,
                replacement_text=fact.exact_text,
                replacement_text_sha256=fact.text_sha256,
                replacement_author=fact.author,
                correction_anchor_sha256=_correction_anchor_sha256(anchor),
            )
        )

    operations.sort(key=lambda item: (item.target_id, item.operation_id))
    proposal_sha256 = runtime_policy_proposal_sha256(proposal)
    plan_seed: dict[str, JsonValue] = {
        "logical_call_id": packet.logical_call_id,
        "host_id": packet.host_id,
        "history_family": history_ir.history_family.value,
        "history_codec_id": packet.history_codec_id,
        "history_codec_contract_version": packet.codec_contract_version,
        "source_request_sha256": packet.raw_request_sha256,
        "evidence_packet_sha256": evidence_packet_sha256(packet),
        "policy_proposal_sha256": proposal_sha256,
        "operations": [runtime_admitted_operation_projection(item) for item in operations],
        "origin": "AUTOMATIC_SENTINEL_POLICY",
        "execution_scope": "SHADOW_ONLY",
        "deployment_prediction": True,
        "curated": False,
    }
    plan = RuntimeAdmittedPlanV1(
        plan_id=f"r22plan:{_canonical_sha256(plan_seed)[:32]}",
        logical_call_id=packet.logical_call_id,
        host_id=packet.host_id,
        history_family=history_ir.history_family.value,
        history_codec_id=packet.history_codec_id,
        history_codec_contract_version=packet.codec_contract_version,
        source_request_sha256=packet.raw_request_sha256,
        evidence_packet_sha256=evidence_packet_sha256(packet),
        policy_proposal_sha256=proposal_sha256,
        operations=tuple(operations),
    )
    checks = tuple(
        dict.fromkeys(
            (
                *proposal_checks,
                "r22.policy_call_bound",
                "r22.packet_ir_request_bound",
                "r22.editable_spans_bound",
                "r22.weak_material_evidence_rejected",
                "r22.shadow_only_runtime_plan",
            )
        )
    )
    bundle = RuntimeAdmissionBundleV1(
        proposal=proposal,
        admitted_plan=plan,
        validation_checks=checks,
    )
    if _canonical_bytes(source_request) != source_bytes:
        _fail("CALLER_INPUT_MUTATED", "proposal admission mutated caller request")
    return bundle


def make_proposal_admission(
    *,
    packet: EvidencePacketV1,
    source_request: JsonValue,
    history_ir: HistoryIR,
) -> Callable[
    [dict[str, JsonValue], dict[str, JsonValue], PolicyCallProvenanceV1],
    RuntimeAdmissionBundleV1,
]:
    """Capture the trusted packet so the GPT callback cannot reconstruct it loosely."""

    if type(packet) is not EvidencePacketV1 or type(history_ir) is not HistoryIR:
        _fail("UNTRUSTED_RUNTIME_TYPE", "admission closure inputs must be exact")
    source_bytes = _canonical_bytes(source_request)
    expected_packet_bytes = _canonical_bytes(evidence_packet_projection(packet))
    trusted_packet = deepcopy(packet)
    trusted_history_ir = deepcopy(history_ir)
    if (
        type(trusted_packet) is not EvidencePacketV1
        or type(trusted_history_ir) is not HistoryIR
        or _canonical_bytes(evidence_packet_projection(trusted_packet)) != expected_packet_bytes
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "admission closure snapshot is not exact")

    def admit(
        packet_projection: dict[str, JsonValue],
        proposal_projection: dict[str, JsonValue],
        provenance: PolicyCallProvenanceV1,
    ) -> RuntimeAdmissionBundleV1:
        if _canonical_bytes(packet_projection) != expected_packet_bytes:
            _fail("EVIDENCE_PACKET_PROJECTION_DRIFT", "callback packet projection differs")
        source_snapshot = _parse_canonical_bytes(source_bytes)
        return proposal_admission(
            deepcopy(trusted_packet),
            proposal_projection,
            provenance,
            source_request=source_snapshot,
            history_ir=deepcopy(trusted_history_ir),
        )

    return admit


def make_gpt_evidence_input(
    packet: EvidencePacketV1,
    *,
    current_image_data_url: str,
) -> GPT56EvidenceInputV1:
    """Build the exact GPT transport snapshot from a trusted packet and image URL."""

    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import GPT56EvidenceInputV1

    if type(packet) is not EvidencePacketV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "packet must use exact trusted type")
    if type(current_image_data_url) is not str:
        _fail("UNTRUSTED_RUNTIME_TYPE", "current image URL must be an exact string")
    packet_bytes = _canonical_bytes(evidence_packet_projection(packet))
    packet_sha256 = evidence_packet_sha256(packet)
    if hashlib.sha256(packet_bytes).hexdigest() != packet_sha256:
        _fail("EVIDENCE_PACKET_HASH_MISMATCH", "packet projection hash differs")
    return GPT56EvidenceInputV1(
        packet_id=packet.packet_id,
        packet_canonical_bytes=packet_bytes,
        packet_sha256=packet_sha256,
        packet=deepcopy(packet),
        current_image_data_url=current_image_data_url,
        current_image_sha256=packet.current_observation.screenshot_content_sha256,
        target_count=len(packet.targets),
    )


def admission_receipt_projector(
    bundle: RuntimeAdmissionBundleV1,
) -> AdmissionReceiptProjectionV1:
    """Project exact admission metadata into the separate hash-only policy receipt."""

    from mobile_world.runtime.sentinel.r2_2.gpt56_policy import AdmissionReceiptProjectionV1
    from mobile_world.runtime.sentinel.r2_2.metrics import PolicyDecisionMetricV1

    if type(bundle) is not RuntimeAdmissionBundleV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "admission bundle must use exact trusted type")
    checks: list[str] = []
    for item in bundle.validation_checks:
        normalized = re.sub(r"[^A-Z0-9]+", "_", item.upper()).strip("_")
        if _CHECK_CODE.fullmatch(normalized) is None:
            _fail("INVALID_VALIDATION_CHECK", "admission check cannot enter receipt")
        if normalized not in checks:
            checks.append(normalized)
    metrics = tuple(
        PolicyDecisionMetricV1(
            verdict=item.factual_verdict.value,
            temporal_validity=item.temporal_validity.value,
            operation=item.operation.value,
        )
        for item in bundle.metric_decisions
    )
    return AdmissionReceiptProjectionV1(
        admitted_plan_sha256=bundle.admitted_plan_sha256,
        validation_checks=tuple(checks),
        metric_decisions=metrics,
    )


def bind_policy_receipt(
    bundle: RuntimeAdmissionBundleV1,
    policy_receipt_sha256: str,
) -> RuntimeSentinelPolicyOutputV1:
    """Bind the already committed R2.2 policy receipt to the admitted output."""

    return _bind_policy_receipt(bundle, policy_receipt_sha256)


@dataclass(frozen=True, slots=True)
class _ResolvedRuntimeOperation:
    operation: RuntimeAdmittedOperationV1
    record: HistoryRecord
    span: SourceSpan
    correction_anchor: CorrectionAnchor | None

    def __post_init__(self) -> None:
        if (
            type(self.operation) is not RuntimeAdmittedOperationV1
            or type(self.record) is not HistoryRecord
            or type(self.span) is not SourceSpan
            or (
                self.correction_anchor is not None
                and type(self.correction_anchor) is not CorrectionAnchor
            )
        ):
            _fail("UNTRUSTED_RUNTIME_TYPE", "resolved operation contains an untrusted type")


def _validate_target_outside_non_history_regions(
    span: SourceSpan,
    ir: HistoryIR,
) -> None:
    for region in ir.regions:
        if type(region) is not RequestRegion or type(region.kind) is not RegionKind:
            _fail("UNTRUSTED_RUNTIME_TYPE", "History IR region is untrusted")
        if type(region.availability) is not RegionAvailability:
            _fail("UNTRUSTED_RUNTIME_TYPE", "region availability is untrusted")
        if (
            region.kind is RegionKind.HISTORY
            or region.availability is RegionAvailability.ABSENT_NOT_IN_HOST_CONTRACT
        ):
            continue
        if type(region.paths) is not tuple or type(region.text_slices) is not tuple:
            _fail("UNTRUSTED_RUNTIME_TYPE", "region locators must be exact tuples")
        if any(
            _path_is_prefix(path, span.container_path) or _path_is_prefix(span.container_path, path)
            for path in region.paths
        ):
            _fail("NON_HISTORY_TARGET_FORBIDDEN", "target overlaps a non-history path")
        for text_slice in region.text_slices:
            if (
                text_slice.container_path == span.container_path
                and span.char_start < text_slice.char_end
                and text_slice.char_start < span.char_end
            ):
                _fail(
                    "NON_HISTORY_TARGET_FORBIDDEN",
                    "target overlaps non-history text bytes",
                )


def _resolve_runtime_plan(
    source_request: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeAdmittedPlanV1,
) -> tuple[_ResolvedRuntimeOperation, ...]:
    if type(history_ir) is not HistoryIR or type(plan) is not RuntimeAdmittedPlanV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "runtime renderer inputs must use exact types")
    if type(history_ir.history_family) is not HistoryFamily:
        _fail("UNTRUSTED_RUNTIME_TYPE", "History IR family must use exact enum")
    source_hash = _canonical_sha256(source_request)
    if source_hash != history_ir.raw_request_sha256 or source_hash != plan.source_request_sha256:
        _fail("SOURCE_REQUEST_BINDING_MISMATCH", "plan or IR binds another source request")
    if plan.host_id != history_ir.host_id:
        _fail("HOST_BINDING_MISMATCH", "plan and History IR hosts differ")
    if plan.history_family != history_ir.history_family.value:
        _fail("HISTORY_FAMILY_BINDING_MISMATCH", "plan and History IR families differ")
    if plan.history_codec_id != history_ir.codec_id:
        _fail("CODEC_BINDING_MISMATCH", "plan and History IR codecs differ")
    if plan.history_codec_contract_version != history_ir.codec_contract_version:
        _fail("CODEC_VERSION_BINDING_MISMATCH", "plan and History IR codec versions differ")
    if type(plan.operations) is not tuple or any(
        type(item) is not RuntimeAdmittedOperationV1 for item in plan.operations
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "runtime plan operations must use exact types")
    if type(history_ir.records) is not tuple or any(
        type(item) is not HistoryRecord for item in history_ir.records
    ):
        _fail("UNTRUSTED_RUNTIME_TYPE", "History IR records must use exact types")

    resolved: list[_ResolvedRuntimeOperation] = []
    occupied: list[tuple[JsonPath, int, int]] = []
    for operation in plan.operations:
        records = [
            record
            for record in history_ir.records
            if record.record_id == operation.target_record_id
        ]
        if len(records) != 1:
            _fail("UNKNOWN_OR_DUPLICATE_RECORD", "operation record must resolve once")
        record = records[0]
        _history_region_for_record(history_ir, record)
        if type(record.editable_spans) is not tuple or any(
            type(item) is not SourceSpan for item in record.editable_spans
        ):
            _fail("UNTRUSTED_RUNTIME_TYPE", "record editable spans are untrusted")
        spans = [
            span
            for span in record.editable_spans
            if span.span_sha256 == operation.target_span_sha256
            and type(span.span_role) is SpanRole
            and span.span_role is SpanRole.EDITABLE_CLAIM
        ]
        if len(spans) != 1:
            _fail("TARGET_SPAN_BINDING_MISMATCH", "operation span must resolve once")
        span = spans[0]
        _validate_source_span(source_request, span)
        _validate_target_outside_non_history_regions(span, history_ir)
        for path, start, end in occupied:
            if path == span.container_path and span.char_start < end and start < span.char_end:
                _fail("OVERLAPPING_RUNTIME_EDITS", "runtime plan target spans overlap")
        occupied.append((span.container_path, span.char_start, span.char_end))
        if type(record.protected_spans) is not tuple or any(
            type(item) is not SourceSpan for item in record.protected_spans
        ):
            _fail("UNTRUSTED_RUNTIME_TYPE", "record protected spans are untrusted")
        for protected in record.protected_spans:
            _validate_source_span(source_request, protected)
            if (
                protected.container_path == span.container_path
                and span.char_start < protected.char_end
                and protected.char_start < span.char_end
            ):
                _fail("TARGET_PROTECTED_OVERLAP", "operation overlaps protected bytes")
        anchor: CorrectionAnchor | None = None
        if operation.kind is RuntimeOperationKind.REPLACE:
            if (
                type(operation.replacement_text) is not str
                or _text_sha256(operation.replacement_text) != operation.replacement_text_sha256
                or operation.replacement_author != "SENTINEL"
            ):
                _fail("REPLACEMENT_FACT_BINDING_MISMATCH", "replacement payload differs")
            anchor = _resolve_correction_anchor(
                source_request,
                history_ir,
                record,
                expected_sha256=operation.correction_anchor_sha256,
            )
            if _path_is_prefix(span.container_path, anchor.container_path):
                _fail(
                    "CORRECTION_TARGET_PATH_CONFLICT",
                    "correction insertion container is nested inside history target text",
                )
        resolved.append(
            _ResolvedRuntimeOperation(
                operation=operation,
                record=record,
                span=span,
                correction_anchor=anchor,
            )
        )
    return tuple(
        sorted(
            resolved,
            key=lambda item: (
                _path_text(item.span.container_path),
                item.span.char_start,
                item.operation.operation_id,
            ),
        )
    )


def _rendered_correction_context(
    anchor: CorrectionAnchor,
    replacement_text: str,
) -> JsonValue:
    if type(replacement_text) is not str or not replacement_text:
        _fail("REPLACEMENT_FACT_MISSING", "replacement text is empty")
    visible_text = f"{anchor.visible_prefix}{replacement_text}{anchor.visible_suffix}"
    if anchor.context_kind is CorrectionContextKind.TEXT_CONTENT_BLOCK:
        return {"type": "text", "text": visible_text}
    if anchor.context_kind is CorrectionContextKind.CHAT_MESSAGE:
        return {"role": "user", "content": visible_text}
    _fail("UNKNOWN_CORRECTION_CONTEXT", "correction context kind is unsupported")


def _render_text_operations(
    source_request: JsonValue,
    candidate: JsonValue,
    resolved: tuple[_ResolvedRuntimeOperation, ...],
) -> tuple[tuple[RuntimeTextDiffV1, ...], tuple[RuntimeSourceMappingV1, ...]]:
    grouped: dict[JsonPath, list[_ResolvedRuntimeOperation]] = defaultdict(list)
    for item in resolved:
        grouped[item.span.container_path].append(item)
    diffs: list[RuntimeTextDiffV1] = []
    mappings: list[RuntimeSourceMappingV1] = []
    for path in sorted(grouped, key=_path_text):
        source = _get_at_path(source_request, path)
        if type(source) is not str:
            _fail("SPAN_CONTAINER_NOT_TEXT", "runtime target container is not exact text")
        source_text = cast(str, source)
        cursor = 0
        rendered_cursor = 0
        chunks: list[str] = []
        for item in sorted(grouped[path], key=lambda value: value.span.char_start):
            span = item.span
            if span.char_start < cursor:
                _fail("OVERLAPPING_RUNTIME_EDITS", "runtime target spans overlap")
            copied = source_text[cursor : span.char_start]
            if copied:
                chunks.append(copied)
                mappings.append(
                    RuntimeSourceMappingV1(
                        container_path=path,
                        source_char_start=cursor,
                        source_char_end=span.char_start,
                        rendered_char_start=rendered_cursor,
                        rendered_char_end=rendered_cursor + len(copied),
                        kind=RuntimeMappingKind.COPIED,
                        operation_id=None,
                    )
                )
                rendered_cursor += len(copied)
            mappings.append(
                RuntimeSourceMappingV1(
                    container_path=path,
                    source_char_start=span.char_start,
                    source_char_end=span.char_end,
                    rendered_char_start=rendered_cursor,
                    rendered_char_end=rendered_cursor,
                    kind=RuntimeMappingKind.DELETED,
                    operation_id=item.operation.operation_id,
                )
            )
            diffs.append(
                RuntimeTextDiffV1(
                    operation_id=item.operation.operation_id,
                    container_path=path,
                    source_char_start=span.char_start,
                    source_char_end=span.char_end,
                    original_text=span.exact_text,
                    rendered_text="",
                    original_sha256=span.span_sha256,
                    rendered_sha256=_text_sha256(""),
                )
            )
            cursor = span.char_end
        tail = source_text[cursor:]
        if tail:
            chunks.append(tail)
            mappings.append(
                RuntimeSourceMappingV1(
                    container_path=path,
                    source_char_start=cursor,
                    source_char_end=len(source_text),
                    rendered_char_start=rendered_cursor,
                    rendered_char_end=rendered_cursor + len(tail),
                    kind=RuntimeMappingKind.COPIED,
                    operation_id=None,
                )
            )
        _set_at_path(candidate, path, "".join(chunks))
    return tuple(diffs), tuple(mappings)


def _insertion_specs(
    resolved: tuple[_ResolvedRuntimeOperation, ...],
) -> list[tuple[JsonPath, int, str, CorrectionAnchor, JsonValue]]:
    specs: list[tuple[JsonPath, int, str, CorrectionAnchor, JsonValue]] = []
    for item in resolved:
        anchor = item.correction_anchor
        if anchor is None:
            continue
        replacement_text = item.operation.replacement_text
        if type(replacement_text) is not str:
            _fail("REPLACEMENT_FACT_MISSING", "replacement text is absent")
        specs.append(
            (
                anchor.container_path,
                anchor.insert_index,
                item.operation.operation_id,
                anchor,
                _rendered_correction_context(anchor, replacement_text),
            )
        )
    if len({(path, index) for path, index, _, _, _ in specs}) != len(specs):
        _fail("AMBIGUOUS_CORRECTION_ANCHOR", "corrections share an insertion coordinate")
    paths = {path for path, _, _, _, _ in specs}
    for left in paths:
        for right in paths:
            if left != right and (_path_is_prefix(left, right) or _path_is_prefix(right, left)):
                _fail(
                    "AMBIGUOUS_CORRECTION_ANCHOR",
                    "nested correction containers are not supported",
                )
    return sorted(specs, key=lambda item: (_path_text(item[0]), item[1], item[2]))


def _apply_correction_insertions(
    source_request: JsonValue,
    candidate: JsonValue,
    resolved: tuple[_ResolvedRuntimeOperation, ...],
) -> tuple[RuntimeListInsertionDiffV1, ...]:
    offsets: dict[JsonPath, int] = defaultdict(int)
    insertions: list[RuntimeListInsertionDiffV1] = []
    for path, source_index, operation_id, anchor, inserted in _insertion_specs(resolved):
        source_container = _get_at_path(source_request, path)
        candidate_container = _get_at_path(candidate, path)
        if type(source_container) is not list or type(candidate_container) is not list:
            _fail("CORRECTION_ANCHOR_NOT_LIST", "correction container is not an exact array")
        if _canonical_sha256(source_container) != anchor.source_container_sha256:
            _fail("CORRECTION_ANCHOR_DRIFT", "correction source container changed")
        rendered_index = source_index + offsets[path]
        if not 0 <= rendered_index <= len(candidate_container):
            _fail("CORRECTION_ANCHOR_OUT_OF_BOUNDS", "rendered insertion is out of bounds")
        inserted_bytes = _canonical_bytes(inserted)
        candidate_container.insert(rendered_index, _parse_canonical_bytes(inserted_bytes))
        offsets[path] += 1
        insertions.append(
            RuntimeListInsertionDiffV1(
                operation_id=operation_id,
                container_path=path,
                source_index=source_index,
                rendered_index=rendered_index,
                inserted_value_canonical_bytes=inserted_bytes,
                inserted_value_sha256=_canonical_sha256(inserted),
            )
        )
    return tuple(insertions)


_RUNTIME_RENDER_CHECKS = (
    "R22_RUNTIME_PLAN_BOUND",
    "R22_EDITABLE_SPANS_BOUND",
    "R22_EXACT_DIFF_RECOMPUTED",
    "R22_NON_HISTORY_BYTES_PRESERVED",
    "R22_EXISTING_BLOCK_ORDER_PRESERVED",
    "R22_CURRENT_SCREENSHOT_BYTES_PRESERVED",
    "R22_REVERSIBLE_SOURCE_MAPPING",
    "R22_CALLER_INPUT_IMMUTABLE",
    "R22_SHADOW_ONLY_CANDIDATE",
)


def _independent_candidate(
    source_request: JsonValue,
    resolved: tuple[_ResolvedRuntimeOperation, ...],
) -> JsonValue:
    """Reconstruct the only admitted candidate with a second, reverse-slice algorithm."""

    candidate = _exact_json_copy(source_request)
    grouped: dict[JsonPath, list[_ResolvedRuntimeOperation]] = defaultdict(list)
    for item in resolved:
        grouped[item.span.container_path].append(item)
    for path, items in grouped.items():
        source = _get_at_path(source_request, path)
        if type(source) is not str:
            _fail("SPAN_CONTAINER_NOT_TEXT", "independent target container is not text")
        expected = cast(str, source)
        for item in sorted(items, key=lambda value: value.span.char_start, reverse=True):
            span = item.span
            if expected[span.char_start : span.char_end] != span.exact_text:
                _fail("SOURCE_SPAN_DRIFT", "independent source slice differs")
            expected = expected[: span.char_start] + expected[span.char_end :]
        _set_at_path(candidate, path, expected)
    offsets: dict[JsonPath, int] = defaultdict(int)
    for path, source_index, _, _, inserted in _insertion_specs(resolved):
        container = _get_at_path(candidate, path)
        if type(container) is not list:
            _fail("CORRECTION_ANCHOR_NOT_LIST", "independent insertion container is not array")
        rendered_index = source_index + offsets[path]
        container.insert(rendered_index, _exact_json_copy(inserted))
        offsets[path] += 1
    return candidate


def _validate_render_receipt_bindings(
    result: RuntimeRenderResultV1,
    resolved: tuple[_ResolvedRuntimeOperation, ...],
) -> None:
    operation_ids = {item.operation.operation_id for item in resolved}
    if {item.operation_id for item in result.text_diffs} != operation_ids:
        _fail("RENDER_DIFF_CENSUS_MISMATCH", "text diff census differs from plan")
    if len(result.text_diffs) != len(operation_ids):
        _fail("DUPLICATE_RENDER_DIFF", "text diff operation IDs are not unique")
    resolved_by_id = {item.operation.operation_id: item for item in resolved}
    for diff in result.text_diffs:
        item = resolved_by_id[diff.operation_id]
        span = item.span
        expected = RuntimeTextDiffV1(
            operation_id=item.operation.operation_id,
            container_path=span.container_path,
            source_char_start=span.char_start,
            source_char_end=span.char_end,
            original_text=span.exact_text,
            rendered_text="",
            original_sha256=span.span_sha256,
            rendered_sha256=_text_sha256(""),
        )
        if runtime_text_diff_projection(diff) != runtime_text_diff_projection(expected):
            _fail("RENDER_DIFF_BINDING_MISMATCH", "text diff differs from admitted span")

    replace_ids = {
        item.operation.operation_id
        for item in resolved
        if item.operation.kind is RuntimeOperationKind.REPLACE
    }
    if {item.operation_id for item in result.list_insertions} != replace_ids:
        _fail("INSERTION_CENSUS_MISMATCH", "correction insertion census differs")
    expected_specs = {
        operation_id: (path, source_index, inserted)
        for path, source_index, operation_id, _, inserted in _insertion_specs(resolved)
    }
    offsets: dict[JsonPath, int] = defaultdict(int)
    for insertion in sorted(
        result.list_insertions,
        key=lambda item: (_path_text(item.container_path), item.source_index, item.operation_id),
    ):
        path, source_index, inserted = expected_specs[insertion.operation_id]
        expected_index = source_index + offsets[path]
        if (
            insertion.container_path != path
            or insertion.source_index != source_index
            or insertion.rendered_index != expected_index
            or _canonical_bytes(insertion.inserted_value) != _canonical_bytes(inserted)
        ):
            _fail("INSERTION_BINDING_MISMATCH", "correction insertion differs from anchor")
        offsets[path] += 1


def _remove_runtime_insertions(
    candidate: JsonValue,
    insertions: tuple[RuntimeListInsertionDiffV1, ...],
) -> JsonValue:
    without_insertions = _exact_json_copy(candidate)
    seen: set[tuple[JsonPath, int]] = set()
    for insertion in insertions:
        key = (insertion.container_path, insertion.rendered_index)
        if key in seen:
            _fail("DUPLICATE_LIST_INSERTION", "insertion coordinates are duplicated")
        seen.add(key)
    for insertion in sorted(
        insertions,
        key=lambda item: (_path_text(item.container_path), item.rendered_index),
        reverse=True,
    ):
        container = _get_at_path(without_insertions, insertion.container_path)
        if type(container) is not list or not 0 <= insertion.rendered_index < len(container):
            _fail("INSERTION_MAPPING_INVALID", "inserted value cannot be resolved")
        if _canonical_bytes(container[insertion.rendered_index]) != _canonical_bytes(
            insertion.inserted_value
        ):
            _fail("INSERTION_MAPPING_DRIFT", "inserted value differs from receipt")
        container.pop(insertion.rendered_index)
    return without_insertions


def restore_runtime_original(result: RuntimeRenderResultV1) -> JsonValue:
    """Restore source bytes from candidate plus exact insertion/diff mappings."""

    if type(result) is not RuntimeRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use exact trusted type")
    source = result.original_request
    restored = _remove_runtime_insertions(
        result.candidate_request,
        result.list_insertions,
    )
    diffs: dict[str, RuntimeTextDiffV1] = {}
    for diff in result.text_diffs:
        if diff.operation_id in diffs:
            _fail("DUPLICATE_RENDER_DIFF", "text diff IDs must be unique")
        diffs[diff.operation_id] = diff
    mappings_by_path: dict[JsonPath, list[RuntimeSourceMappingV1]] = defaultdict(list)
    for mapping in result.source_mappings:
        mappings_by_path[mapping.container_path].append(mapping)
    consumed_diffs: set[str] = set()
    for path, mappings in mappings_by_path.items():
        candidate_text = _get_at_path(restored, path)
        if type(candidate_text) is not str:
            _fail("SPAN_CONTAINER_NOT_TEXT", "mapped candidate container is not text")
        source_cursor = 0
        rendered_cursor = 0
        source_chunks: list[str] = []
        for mapping in sorted(
            mappings,
            key=lambda item: (
                item.source_char_start,
                item.source_char_end,
                item.kind.value,
                item.operation_id or "",
            ),
        ):
            if (
                mapping.source_char_start != source_cursor
                or mapping.rendered_char_start != rendered_cursor
            ):
                _fail("SOURCE_MAPPING_COVERAGE_INVALID", "mapping has a gap or overlap")
            if mapping.kind is RuntimeMappingKind.COPIED:
                copied = candidate_text[mapping.rendered_char_start : mapping.rendered_char_end]
                if len(copied) != mapping.source_char_end - mapping.source_char_start:
                    _fail("COPIED_MAPPING_DRIFT", "copied mapping width differs")
                source_chunks.append(copied)
            else:
                mapped_diff = diffs.get(cast(str, mapping.operation_id))
                if mapped_diff is None:
                    _fail("MAPPING_DIFF_MISSING", "deleted mapping has no text diff")
                if (
                    mapped_diff.container_path != path
                    or mapped_diff.source_char_start != mapping.source_char_start
                    or mapped_diff.source_char_end != mapping.source_char_end
                ):
                    _fail("MAPPING_DIFF_MISMATCH", "deleted mapping differs from text diff")
                source_chunks.append(mapped_diff.original_text)
                consumed_diffs.add(mapped_diff.operation_id)
            source_cursor = mapping.source_char_end
            rendered_cursor = mapping.rendered_char_end
        if rendered_cursor != len(candidate_text):
            _fail("SOURCE_MAPPING_COVERAGE_INVALID", "mapping misses candidate suffix")
        source_text = _get_at_path(source, path)
        if type(source_text) is not str or "".join(source_chunks) != source_text:
            _fail("NON_REVERSIBLE_MAPPING", "mapping does not reconstruct source text")
        _set_at_path(restored, path, "".join(source_chunks))
    if consumed_diffs != set(diffs):
        _fail("UNCONSUMED_RENDER_DIFF", "not every text diff has one mapping")
    if _canonical_bytes(restored) != result.source_request_canonical_bytes:
        _fail("NON_REVERSIBLE_MAPPING", "restored request differs from source snapshot")
    return restored


def validate_runtime_render_result(
    source_request: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeAdmittedPlanV1,
    result: RuntimeRenderResultV1,
) -> tuple[str, ...]:
    """Independently reconstruct candidate, diff bindings, and reversibility."""

    if type(result) is not RuntimeRenderResultV1:
        _fail("UNTRUSTED_RUNTIME_TYPE", "render result must use exact trusted type")
    source_bytes = _canonical_bytes(source_request)
    resolved = _resolve_runtime_plan(_parse_canonical_bytes(source_bytes), history_ir, plan)
    if result.source_request_canonical_bytes != source_bytes:
        _fail("SOURCE_REQUEST_BINDING_MISMATCH", "render result source snapshot differs")
    if result.admitted_plan_sha256 != runtime_admitted_plan_sha256(plan):
        _fail("RUNTIME_PLAN_HASH_MISMATCH", "render result binds another runtime plan")
    expected_candidate = _independent_candidate(
        _parse_canonical_bytes(source_bytes),
        resolved,
    )
    if result.candidate_request_canonical_bytes != _canonical_bytes(expected_candidate):
        _fail("CANDIDATE_REQUEST_MISMATCH", "candidate differs from independent render")
    _validate_render_receipt_bindings(result, resolved)
    exact_diff = _exact_diff_projection(
        result.text_diffs,
        result.list_insertions,
        result.source_mappings,
    )
    if _canonical_sha256(exact_diff) != result.exact_diff_sha256:
        _fail("EXACT_DIFF_HASH_MISMATCH", "exact diff hash differs")
    restored = restore_runtime_original(result)
    if _canonical_bytes(restored) != source_bytes:
        _fail("NON_REVERSIBLE_MAPPING", "candidate did not restore to exact source")
    if result.validation_checks != _RUNTIME_RENDER_CHECKS:
        _fail("RENDER_VALIDATION_CHECKS_MISMATCH", "render checks differ from v1 contract")
    if _canonical_bytes(source_request) != source_bytes:
        _fail("CALLER_INPUT_MUTATED", "runtime render validation mutated caller request")
    return _RUNTIME_RENDER_CHECKS


def render_runtime_admitted_plan(
    source_request: JsonValue,
    history_ir: HistoryIR,
    plan: RuntimeAdmittedPlanV1,
) -> RuntimeRenderResultV1:
    """Render a reversible candidate only; R2.2 never authorizes provider transport."""

    source_bytes = _canonical_bytes(source_request)
    source_snapshot = _parse_canonical_bytes(source_bytes)
    resolved = _resolve_runtime_plan(source_snapshot, history_ir, plan)
    candidate = _parse_canonical_bytes(source_bytes)
    diffs, mappings = _render_text_operations(source_snapshot, candidate, resolved)
    insertions = _apply_correction_insertions(
        source_snapshot,
        candidate,
        resolved,
    )
    candidate_bytes = _canonical_bytes(candidate)
    exact_diff = _exact_diff_projection(diffs, insertions, mappings)
    result = RuntimeRenderResultV1(
        source_request_canonical_bytes=source_bytes,
        candidate_request_canonical_bytes=candidate_bytes,
        source_request_sha256=hashlib.sha256(source_bytes).hexdigest(),
        candidate_request_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        admitted_plan_sha256=runtime_admitted_plan_sha256(plan),
        exact_diff_sha256=_canonical_sha256(exact_diff),
        text_diffs=diffs,
        list_insertions=insertions,
        source_mappings=mappings,
        validation_checks=_RUNTIME_RENDER_CHECKS,
    )
    validate_runtime_render_result(source_snapshot, history_ir, plan, result)
    if _canonical_bytes(source_request) != source_bytes:
        _fail("CALLER_INPUT_MUTATED", "runtime renderer mutated caller request")
    return result


__all__ = [
    "RUNTIME_RENDER_RESULT_SCHEMA_VERSION",
    "RuntimeListInsertionDiffV1",
    "RuntimeMappingKind",
    "RuntimeRenderResultV1",
    "RuntimeSourceMappingV1",
    "RuntimeTextDiffV1",
    "admission_receipt_projector",
    "bind_policy_receipt",
    "make_gpt_evidence_input",
    "make_proposal_admission",
    "parse_runtime_policy_proposal",
    "proposal_admission",
    "render_runtime_admitted_plan",
    "restore_runtime_original",
    "runtime_list_insertion_projection",
    "runtime_render_result_projection",
    "runtime_render_result_sha256",
    "runtime_source_mapping_projection",
    "runtime_text_diff_projection",
    "validate_runtime_render_result",
]
