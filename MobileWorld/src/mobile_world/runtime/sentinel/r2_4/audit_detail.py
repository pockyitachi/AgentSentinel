"""Access-controlled, full-detail audit records for the R2.4 CPU slice.

The accepted R2.1 receipt deliberately retains hashes only.  R2.4 needs a
separate detail channel for operator-authorized debugging and pilot evidence.
This module keeps that channel explicit: production-shaped details may be
written only to an owner-only directory outside the Git repository.  The
in-memory sink exists solely for CPU tests and embeddings.

Collector events are not imported or mutated here.  Every retained artifact is
captured as bounded canonical JSON bytes, checked for common credential and
reasoning fields, and bound to a module-computed SHA-256.  No caller-provided
``to_dict`` implementation participates in hashing.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from copy import deepcopy
from dataclasses import InitVar, dataclass, fields
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    HISTORY_IR_SCHEMA_VERSION,
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
from mobile_world.offline.causal_replay.core import validate_history_ir
from mobile_world.runtime.sentinel.contracts import SentinelMode
from mobile_world.runtime.sentinel.r2_3.contracts import (
    PathRelevanceOutputV1,
    TopologyKind,
    path_relevance_output_projection,
    snapshot_path_relevance_output,
)
from mobile_world.runtime.sentinel.r2_4.capabilities import (
    RuntimeCodecOverlayDeclarationV1,
    RuntimeHistoryExtractionResultV1,
    RuntimeHistoryExtractionStatusV1,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    R24ContractError,
    RuntimeVerticalExecutionScope,
    RuntimeVerticalPolicyOutputV1,
    canonical_json_bytes,
    canonical_sha256,
    snapshot_json_value,
    snapshot_vertical_output,
    vertical_output_projection,
)
from mobile_world.runtime.sentinel.r2_4.renderer import (
    RuntimeVerticalRenderResultV1,
    snapshot_vertical_render_result,
    validate_vertical_render_result,
    vertical_render_result_projection,
    vertical_source_mapping_projection,
    vertical_text_diff_projection,
)

R24_AUDIT_ARTIFACT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-audit-artifact/v1"
R24_AUDIT_DETAIL_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-audit-detail/v1"
R24_PROVIDER_BINDING_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-provider-response-binding/v1"
)
R24_PARSER_BINDING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-parser-binding/v1"
R24_ACTION_BINDING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-action-binding/v1"
R24_VALIDATOR_BINDING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-validator-binding/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CHECK_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_DETAIL_BYTES = 256 * 1024 * 1024
_MAX_GRAPH_DEPTH = 64
_MAX_GRAPH_VISITS = 262_144
_AUDIT_DETAIL_BUILDER_TOKEN = object()

_FORBIDDEN_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "apikey",
        "openai_api_key",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "environment",
        "env",
        "chain_of_thought",
        "reasoning",
        "reasoning_content",
        "reasoning_text",
        "analysis",
        "thought",
    }
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:openai_)?api_key\s*=\s*[^\s]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
)

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


@dataclass(slots=True)
class _ProjectionBudget:
    visits: int = 0


def _trusted_history_node_projection(
    value: object,
    *,
    active: set[int],
    budget: _ProjectionBudget,
    depth: int,
) -> JsonValue:
    budget.visits += 1
    if budget.visits > _MAX_GRAPH_VISITS:
        raise R24ContractError("GRAPH_NODE_LIMIT", "History IR projection budget exceeded")
    if depth > _MAX_GRAPH_DEPTH:
        raise R24ContractError("GRAPH_DEPTH_LIMIT", "History IR projection depth exceeded")
    if value is None or type(value) in {bool, int, float, str}:
        return cast(JsonValue, value)
    if type(value) in _HISTORY_ENUM_TYPES:
        return cast(JsonValue, cast(Any, value).value)
    if type(value) in {tuple, list, dict} or type(value) in _HISTORY_RECORD_TYPES:
        identity = id(value)
        if identity in active:
            raise R24ContractError("GRAPH_CYCLE", "History IR projection contains a cycle")
        active.add(identity)
        try:
            if type(value) is tuple:
                return [
                    _trusted_history_node_projection(
                        item,
                        active=active,
                        budget=budget,
                        depth=depth + 1,
                    )
                    for item in cast(tuple[object, ...], value)
                ]
            if type(value) is list:
                return [
                    _trusted_history_node_projection(
                        item,
                        active=active,
                        budget=budget,
                        depth=depth + 1,
                    )
                    for item in cast(list[object], value)
                ]
            if type(value) is dict:
                mapping = cast(dict[object, object], value)
                if any(type(key) is not str for key in mapping):
                    raise R24ContractError(
                        "NON_CANONICAL_JSON", "History IR mapping key is not exact text"
                    )
                return {
                    cast(str, key): _trusted_history_node_projection(
                        child,
                        active=active,
                        budget=budget,
                        depth=depth + 1,
                    )
                    for key, child in mapping.items()
                }
            return {
                field.name: _trusted_history_node_projection(
                    getattr(value, field.name),
                    active=active,
                    budget=budget,
                    depth=depth + 1,
                )
                for field in fields(cast(Any, value))
            }
        finally:
            active.remove(identity)
    raise R24ContractError(
        "UNTRUSTED_RUNTIME_TYPE", "History IR projection contains an untrusted node"
    )


def trusted_history_ir_projection(value: HistoryIR) -> dict[str, JsonValue]:
    """Module-owned History IR projection; no ``to_dict`` method is consulted."""

    if type(value) is not HistoryIR:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "History IR must use exact type")
    projected = _trusted_history_node_projection(
        value,
        active=set(),
        budget=_ProjectionBudget(),
        depth=0,
    )
    if type(projected) is not dict:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "History IR projection is not an object")
    result = projected
    return {"schema_version": HISTORY_IR_SCHEMA_VERSION, **result}


def _codec_overlay_projection(
    value: RuntimeCodecOverlayDeclarationV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeCodecOverlayDeclarationV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "codec overlay must use exact type")
    return {
        "schema_version": value.schema_version,
        "overlay_id": value.overlay_id,
        "host_id": value.host_id,
        "history_family": value.history_family.value,
        "base_codec_id": value.base_codec_id,
        "base_codec_contract_version": value.base_codec_contract_version,
        "base_capability_sha256": value.base_capability_sha256,
        "implementation_sha256": value.implementation_sha256,
        "discovery_mode": value.discovery_mode.value,
        "live_ready": value.live_ready,
    }


class RuntimeAuditArtifactKindV1(StrEnum):
    RAW_REQUEST = "RAW_REQUEST"
    HISTORY_IR = "HISTORY_IR"
    POLICY_OUTPUT = "POLICY_OUTPUT"
    RUBRIC_OUTPUT = "RUBRIC_OUTPUT"
    RENDER_RESULT = "RENDER_RESULT"
    CANDIDATE_REQUEST = "CANDIDATE_REQUEST"
    EXACT_DIFF = "EXACT_DIFF"
    VALIDATOR_RESULT = "VALIDATOR_RESULT"
    FINAL_REQUEST = "FINAL_REQUEST"
    PROVIDER_RESPONSE = "PROVIDER_RESPONSE"
    PARSER_RESULT = "PARSER_RESULT"
    ACTOR_ACTION = "ACTOR_ACTION"


class RuntimeAuditOutcomeV1(StrEnum):
    BYPASSED = "BYPASSED"
    COMPLETED = "COMPLETED"
    FALLBACK_ORIGINAL = "FALLBACK_ORIGINAL"


class CpuFakeProviderKindV1(StrEnum):
    IN_PROCESS_FAKE = "IN_PROCESS_FAKE"


class ParserResultStatusV1(StrEnum):
    PARSED = "PARSED"
    PARSE_FALLBACK = "PARSE_FALLBACK"


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise R24ContractError("INVALID_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _require_id(value: object, name: str, *, semantic: bool = False) -> str:
    pattern = _SEMANTIC_ID if semantic else _RUNTIME_ID
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise R24ContractError("INVALID_RUNTIME_ID", f"{name} is not a bounded safe ID")
    return value


def _normalise_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _key_looks_sensitive(value: str) -> bool:
    key = _normalise_key(value)
    return key in _FORBIDDEN_KEYS or any(
        key.endswith(suffix) for suffix in ("_api_key", "_password", "_secret", "_access_token")
    )


def _reject_sensitive_material(value: JsonValue) -> None:
    """Reject obvious credentials, environment dumps, and reasoning payloads."""

    stack: list[JsonValue] = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            mapping = item
            for key, child in mapping.items():
                if _key_looks_sensitive(key):
                    raise R24ContractError(
                        "FORBIDDEN_AUDIT_MATERIAL",
                        "audit detail contains a credential, environment, or reasoning field",
                    )
                stack.append(child)
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is str and any(
            pattern.search(item) is not None for pattern in _SECRET_VALUE_PATTERNS
        ):
            raise R24ContractError(
                "FORBIDDEN_AUDIT_MATERIAL",
                "audit detail contains credential-shaped text",
            )


def _decode_canonical(payload: bytes) -> JsonValue:
    if type(payload) is not bytes:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "artifact snapshot must use bytes")
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise R24ContractError("AUDIT_ARTIFACT_TOO_LARGE", "artifact exceeds its byte budget")
    try:
        decoded = cast(JsonValue, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise R24ContractError("NON_CANONICAL_JSON", "artifact is not canonical JSON") from exc
    if canonical_json_bytes(decoded) != payload:
        raise R24ContractError("NON_CANONICAL_JSON", "artifact bytes are not canonical")
    _reject_sensitive_material(decoded)
    return decoded


def _require_optional_sha256(value: object, name: str) -> None:
    if value is not None:
        _require_sha256(value, name)


def _require_optional_nonnegative_int(value: object, name: str) -> None:
    if value is not None and (type(value) is not int or value < 0):
        raise R24ContractError(
            "INVALID_PROVIDER_METADATA", f"{name} must be null or a non-negative integer"
        )


def _are_exact_bools(*values: object) -> bool:
    return all(type(value) is bool for value in values)


def _validate_provider_binding(value: JsonValue) -> None:
    if type(value) is not dict:
        raise R24ContractError(
            "UNSAFE_PROVIDER_RESPONSE_DETAIL",
            "provider response detail must use the safe hash-only projection",
        )
    mapping = value
    required = {
        "schema_version",
        "provider_kind",
        "logical_call_id",
        "attempt_id",
        "final_request_sha256",
        "raw_provider_response_sha256",
        "response_id_sha256",
        "model_id_sha256",
        "finish_reason",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "response_content_persisted",
        "reasoning_persisted",
    }
    if set(mapping) != required:
        raise R24ContractError(
            "UNSAFE_PROVIDER_RESPONSE_DETAIL",
            "provider response detail has fields outside the safe projection",
        )
    if mapping["schema_version"] != R24_PROVIDER_BINDING_SCHEMA_VERSION or (
        mapping["provider_kind"] != CpuFakeProviderKindV1.IN_PROCESS_FAKE.value
    ):
        raise R24ContractError(
            "UNSAFE_PROVIDER_RESPONSE_DETAIL", "provider response binding is not CPU fake v1"
        )
    _require_id(mapping["logical_call_id"], "provider.logical_call_id")
    _require_id(mapping["attempt_id"], "provider.attempt_id")
    _require_sha256(mapping["final_request_sha256"], "provider.final_request_sha256")
    _require_sha256(mapping["raw_provider_response_sha256"], "raw_provider_response_sha256")
    _require_optional_sha256(mapping["response_id_sha256"], "response_id_sha256")
    _require_optional_sha256(mapping["model_id_sha256"], "model_id_sha256")
    finish_reason = mapping["finish_reason"]
    if finish_reason is not None and (
        type(finish_reason) is not str or _SEMANTIC_ID.fullmatch(finish_reason) is None
    ):
        raise R24ContractError(
            "INVALID_PROVIDER_METADATA", "finish_reason must be null or a safe identifier"
        )
    for name in ("input_tokens", "output_tokens", "total_tokens"):
        _require_optional_nonnegative_int(mapping[name], name)
    if mapping["response_content_persisted"] is not False or (
        mapping["reasoning_persisted"] is not False
    ):
        raise R24ContractError(
            "UNSAFE_PROVIDER_RESPONSE_DETAIL", "provider content/reasoning must not be persisted"
        )


def _validate_parser_binding(value: JsonValue) -> None:
    if type(value) is not dict:
        raise R24ContractError(
            "UNSAFE_PARSER_DETAIL", "parser detail must use the safe hash-only projection"
        )
    mapping = value
    required = {
        "schema_version",
        "parser_id",
        "status",
        "logical_call_id",
        "attempt_id",
        "final_request_sha256",
        "raw_provider_response_sha256",
        "actor_action_sha256",
        "raw_parser_input_sha256",
        "normalized_actor_output_sha256",
        "attempt_count",
        "parser_input_persisted",
        "reasoning_persisted",
    }
    if set(mapping) != required or mapping["schema_version"] != R24_PARSER_BINDING_SCHEMA_VERSION:
        raise R24ContractError(
            "UNSAFE_PARSER_DETAIL", "parser detail has fields outside the safe projection"
        )
    _require_id(mapping["parser_id"], "parser_id", semantic=True)
    _require_id(mapping["logical_call_id"], "parser.logical_call_id")
    _require_id(mapping["attempt_id"], "parser.attempt_id")
    if mapping["status"] not in {
        ParserResultStatusV1.PARSED.value,
        ParserResultStatusV1.PARSE_FALLBACK.value,
    }:
        raise R24ContractError("INVALID_PARSER_METADATA", "parser status is not closed")
    _require_sha256(mapping["raw_parser_input_sha256"], "raw_parser_input_sha256")
    _require_sha256(mapping["final_request_sha256"], "parser.final_request_sha256")
    _require_sha256(mapping["raw_provider_response_sha256"], "parser.raw_provider_response_sha256")
    _require_sha256(mapping["actor_action_sha256"], "parser.actor_action_sha256")
    _require_sha256(mapping["normalized_actor_output_sha256"], "normalized_actor_output_sha256")
    if type(mapping["attempt_count"]) is not int or mapping["attempt_count"] < 1:
        raise R24ContractError("INVALID_PARSER_METADATA", "attempt_count must be positive")
    if mapping["parser_input_persisted"] is not False or mapping["reasoning_persisted"] is not (
        False
    ):
        raise R24ContractError(
            "UNSAFE_PARSER_DETAIL", "raw parser input/reasoning must not be persisted"
        )


def _reject_reasoning_shaped_action(value: JsonValue) -> None:
    patterns = (
        re.compile(r"(?is)<thinking(?:\s[^>]*)?>.*?</thinking>"),
        re.compile(r"(?im)^\s*(?:thought|analysis|reasoning)\s*:"),
    )
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is dict:
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
        elif type(item) is str and any(pattern.search(item) for pattern in patterns):
            raise R24ContractError(
                "REASONING_SHAPED_ACTION", "actor-action projection contains reasoning text"
            )


def _validate_action_binding(value: JsonValue) -> None:
    if type(value) is not dict:
        raise R24ContractError(
            "UNSAFE_ACTION_DETAIL", "action detail must use the exact action projection"
        )
    mapping = value
    if set(mapping) != {"schema_version", "action_sha256", "action"} or (
        mapping["schema_version"] != R24_ACTION_BINDING_SCHEMA_VERSION
    ):
        raise R24ContractError(
            "UNSAFE_ACTION_DETAIL", "action detail has fields outside the safe projection"
        )
    action = mapping["action"]
    _require_sha256(mapping["action_sha256"], "action_sha256")
    if canonical_sha256(action) != mapping["action_sha256"]:
        raise R24ContractError("ACTION_HASH_MISMATCH", "action hash differs from projection")
    _reject_reasoning_shaped_action(action)


def _validate_validator_binding(value: JsonValue) -> None:
    if type(value) is not dict:
        raise R24ContractError(
            "UNSAFE_VALIDATOR_DETAIL", "validator detail must use the exact safe projection"
        )
    mapping = value
    required = {
        "schema_version",
        "status",
        "logical_call_id",
        "host_id",
        "effective_mode",
        "topology_kind",
        "codec_overlay_sha256",
        "raw_request_sha256",
        "history_ir_sha256",
        "policy_output_sha256",
        "rubric_output_sha256",
        "render_result_sha256",
        "candidate_request_sha256",
        "exact_diff_sha256",
        "final_request_sha256",
        "validation_checks",
    }
    if set(mapping) != required or mapping["schema_version"] != (
        R24_VALIDATOR_BINDING_SCHEMA_VERSION
    ):
        raise R24ContractError(
            "UNSAFE_VALIDATOR_DETAIL", "validator detail has fields outside its projection"
        )
    if mapping["status"] != "PASSED":
        raise R24ContractError("UNSAFE_VALIDATOR_DETAIL", "completed detail requires PASSED")
    _require_id(mapping["logical_call_id"], "validator.logical_call_id")
    _require_id(mapping["host_id"], "validator.host_id")
    if mapping["effective_mode"] not in {item.value for item in SentinelMode}:
        raise R24ContractError("UNSAFE_VALIDATOR_DETAIL", "validator mode is unknown")
    if mapping["topology_kind"] not in {item.value for item in TopologyKind}:
        raise R24ContractError("UNSAFE_VALIDATOR_DETAIL", "validator topology is unknown")
    for name in (
        "codec_overlay_sha256",
        "raw_request_sha256",
        "history_ir_sha256",
        "policy_output_sha256",
        "rubric_output_sha256",
        "render_result_sha256",
        "candidate_request_sha256",
        "exact_diff_sha256",
        "final_request_sha256",
    ):
        _require_sha256(mapping[name], f"validator.{name}")
    checks = mapping["validation_checks"]
    if (
        type(checks) is not list
        or not checks
        or any(type(item) is not str or _CHECK_CODE.fullmatch(item) is None for item in checks)
    ):
        raise R24ContractError(
            "UNSAFE_VALIDATOR_DETAIL", "validator checks must be a closed non-empty list"
        )


@dataclass(frozen=True, slots=True)
class CanonicalAuditArtifactV1:
    """One detached exact projection and its module-owned canonical hash."""

    kind: RuntimeAuditArtifactKindV1
    canonical_bytes: bytes
    sha256: str
    schema_version: str = R24_AUDIT_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R24_AUDIT_ARTIFACT_SCHEMA_VERSION:
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown audit artifact schema")
        if type(self.kind) is not RuntimeAuditArtifactKindV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "artifact kind is untrusted")
        decoded = _decode_canonical(self.canonical_bytes)
        if self.kind is RuntimeAuditArtifactKindV1.PROVIDER_RESPONSE:
            _validate_provider_binding(decoded)
        elif self.kind is RuntimeAuditArtifactKindV1.PARSER_RESULT:
            _validate_parser_binding(decoded)
        elif self.kind is RuntimeAuditArtifactKindV1.ACTOR_ACTION:
            _validate_action_binding(decoded)
        elif self.kind is RuntimeAuditArtifactKindV1.VALIDATOR_RESULT:
            _validate_validator_binding(decoded)
        _require_sha256(self.sha256, "artifact.sha256")
        if canonical_sha256(decoded) != self.sha256:
            raise R24ContractError("ARTIFACT_HASH_MISMATCH", "artifact hash differs from bytes")

    @classmethod
    def capture(
        cls,
        kind: RuntimeAuditArtifactKindV1,
        value: JsonValue,
    ) -> CanonicalAuditArtifactV1:
        if type(kind) is not RuntimeAuditArtifactKindV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "artifact kind is untrusted")
        payload = canonical_json_bytes(value)
        if len(payload) > _MAX_ARTIFACT_BYTES:
            raise R24ContractError("AUDIT_ARTIFACT_TOO_LARGE", "artifact exceeds its byte budget")
        detached = _decode_canonical(payload)
        return cls(
            kind=kind,
            canonical_bytes=payload,
            sha256=canonical_sha256(detached),
        )

    @property
    def value(self) -> JsonValue:
        return _decode_canonical(bytes(self.canonical_bytes))


def capture_cpu_fake_provider_response_binding(
    raw_provider_response: JsonValue,
    *,
    logical_call_id: str,
    attempt_id: str,
    final_request_sha256: str,
    response_id: str | None = None,
    model_id: str | None = None,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> CanonicalAuditArtifactV1:
    """Hash a raw fake-provider response while retaining no response content."""

    for value, name in ((response_id, "response_id"), (model_id, "model_id")):
        if value is not None and type(value) is not str:
            raise R24ContractError(
                "INVALID_PROVIDER_METADATA", f"{name} must be exact text or null"
            )
    raw_hash = canonical_sha256(raw_provider_response)
    projection: dict[str, JsonValue] = {
        "schema_version": R24_PROVIDER_BINDING_SCHEMA_VERSION,
        "provider_kind": CpuFakeProviderKindV1.IN_PROCESS_FAKE.value,
        "logical_call_id": logical_call_id,
        "attempt_id": attempt_id,
        "final_request_sha256": final_request_sha256,
        "raw_provider_response_sha256": raw_hash,
        "response_id_sha256": None if response_id is None else canonical_sha256(response_id),
        "model_id_sha256": None if model_id is None else canonical_sha256(model_id),
        "finish_reason": finish_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "response_content_persisted": False,
        "reasoning_persisted": False,
    }
    _validate_provider_binding(projection)
    return CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.PROVIDER_RESPONSE,
        projection,
    )


def capture_parser_result_binding(
    raw_parser_input: JsonValue,
    normalized_actor_output: JsonValue,
    *,
    parser_id: str,
    status: ParserResultStatusV1,
    attempt_count: int,
    logical_call_id: str,
    attempt_id: str,
    final_request_sha256: str,
    raw_provider_response_sha256: str,
    actor_action_sha256: str,
) -> CanonicalAuditArtifactV1:
    """Retain parser identity/status and hashes, never its reasoning-bearing input."""

    if type(status) is not ParserResultStatusV1:
        raise R24ContractError("INVALID_PARSER_METADATA", "parser status is untrusted")
    projection: dict[str, JsonValue] = {
        "schema_version": R24_PARSER_BINDING_SCHEMA_VERSION,
        "parser_id": parser_id,
        "status": status.value,
        "logical_call_id": logical_call_id,
        "attempt_id": attempt_id,
        "final_request_sha256": final_request_sha256,
        "raw_provider_response_sha256": raw_provider_response_sha256,
        "actor_action_sha256": actor_action_sha256,
        "raw_parser_input_sha256": canonical_sha256(raw_parser_input),
        "normalized_actor_output_sha256": canonical_sha256(normalized_actor_output),
        "attempt_count": attempt_count,
        "parser_input_persisted": False,
        "reasoning_persisted": False,
    }
    _validate_parser_binding(projection)
    return CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.PARSER_RESULT,
        projection,
    )


def capture_actor_action_binding(action: JsonValue) -> CanonicalAuditArtifactV1:
    """Retain only the ordinary parsed actor action, never model reasoning."""

    projection: dict[str, JsonValue] = {
        "schema_version": R24_ACTION_BINDING_SCHEMA_VERSION,
        "action_sha256": canonical_sha256(action),
        "action": action,
    }
    _validate_action_binding(projection)
    return CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.ACTOR_ACTION,
        projection,
    )


@dataclass(frozen=True, slots=True)
class RuntimeAuditStageLatenciesV1:
    evidence_snapshot_ns: int
    history_extract_ns: int
    rubric_ns: int
    policy_ns: int
    render_ns: int
    validator_ns: int
    provider_ns: int
    parser_ns: int
    total_ns: int

    def __post_init__(self) -> None:
        values = (
            self.evidence_snapshot_ns,
            self.history_extract_ns,
            self.rubric_ns,
            self.policy_ns,
            self.render_ns,
            self.validator_ns,
            self.provider_ns,
            self.parser_ns,
            self.total_ns,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise R24ContractError(
                "INVALID_STAGE_LATENCY", "stage latencies must be non-negative exact integers"
            )
        if self.total_ns < max(values[:-1], default=0):
            raise R24ContractError(
                "INVALID_STAGE_LATENCY", "total latency cannot be below a stage latency"
            )


@dataclass(frozen=True, slots=True)
class CpuFakeAuditResourceFlagsV1:
    """Closed negative resource census for the authorized CPU/fake checkpoint."""

    cpu_only: bool = True
    offline: bool = True
    fake_policy_backend: bool = True
    fake_rubric_backend: bool = True
    fake_actor_provider: bool = True
    external_network_attempted: bool = False
    live_model_call_attempted: bool = False
    gpu_used: bool = False
    mobileworld_backend_used: bool = False
    emulator_used: bool = False
    gui_tool_invoked: bool = False
    action_executed: bool = False
    sentinel_selected_action: bool = False
    collector_raw_mutated: bool = False
    detail_written_to_collector: bool = False
    detail_written_inside_repository: bool = False
    secret_material_persisted: bool = False
    reasoning_persisted: bool = False

    def __post_init__(self) -> None:
        expected = {
            "cpu_only": True,
            "offline": True,
            "fake_policy_backend": True,
            "fake_rubric_backend": True,
            "fake_actor_provider": True,
            "external_network_attempted": False,
            "live_model_call_attempted": False,
            "gpu_used": False,
            "mobileworld_backend_used": False,
            "emulator_used": False,
            "gui_tool_invoked": False,
            "action_executed": False,
            "sentinel_selected_action": False,
            "collector_raw_mutated": False,
            "detail_written_to_collector": False,
            "detail_written_inside_repository": False,
            "secret_material_persisted": False,
            "reasoning_persisted": False,
        }
        for name, required in expected.items():
            value = getattr(self, name)
            if type(value) is not bool or value is not required:
                raise R24ContractError(
                    "CPU_FAKE_RESOURCE_BOUNDARY",
                    f"{name} exceeds the CPU/offline/fake audit authority",
                )


_ARTIFACT_FIELDS: tuple[tuple[str, RuntimeAuditArtifactKindV1], ...] = (
    ("raw_request", RuntimeAuditArtifactKindV1.RAW_REQUEST),
    ("history_ir", RuntimeAuditArtifactKindV1.HISTORY_IR),
    ("policy_output", RuntimeAuditArtifactKindV1.POLICY_OUTPUT),
    ("rubric_output", RuntimeAuditArtifactKindV1.RUBRIC_OUTPUT),
    ("render_result", RuntimeAuditArtifactKindV1.RENDER_RESULT),
    ("candidate_request", RuntimeAuditArtifactKindV1.CANDIDATE_REQUEST),
    ("exact_diff", RuntimeAuditArtifactKindV1.EXACT_DIFF),
    ("validator_result", RuntimeAuditArtifactKindV1.VALIDATOR_RESULT),
    ("final_request", RuntimeAuditArtifactKindV1.FINAL_REQUEST),
    ("provider_response", RuntimeAuditArtifactKindV1.PROVIDER_RESPONSE),
    ("parser_result", RuntimeAuditArtifactKindV1.PARSER_RESULT),
    ("actor_action", RuntimeAuditArtifactKindV1.ACTOR_ACTION),
)


@dataclass(frozen=True, slots=True)
class RuntimeAuditDetailV1:
    detail_id: str
    logical_call_id: str
    host_id: str
    configured_mode: SentinelMode
    effective_mode: SentinelMode
    outcome: RuntimeAuditOutcomeV1
    topology_kind: TopologyKind
    topology_comparison_sha256: str
    history_codec_id: str
    history_codec_contract_version: str
    codec_overlay_sha256: str
    raw_request: CanonicalAuditArtifactV1
    history_ir: CanonicalAuditArtifactV1
    policy_output: CanonicalAuditArtifactV1
    rubric_output: CanonicalAuditArtifactV1
    render_result: CanonicalAuditArtifactV1
    candidate_request: CanonicalAuditArtifactV1
    exact_diff: CanonicalAuditArtifactV1
    validator_result: CanonicalAuditArtifactV1
    final_request: CanonicalAuditArtifactV1
    provider_response: CanonicalAuditArtifactV1
    parser_result: CanonicalAuditArtifactV1
    actor_action: CanonicalAuditArtifactV1
    latencies: RuntimeAuditStageLatenciesV1
    resources: CpuFakeAuditResourceFlagsV1
    would_edit: bool
    edit_applied: bool
    validation_checks: tuple[str, ...]
    reason_code: str | None = None
    schema_version: str = R24_AUDIT_DETAIL_SCHEMA_VERSION
    _builder_token: InitVar[object | None] = None

    def __post_init__(self, _builder_token: object | None) -> None:
        if _builder_token is not _AUDIT_DETAIL_BUILDER_TOKEN:
            raise R24ContractError(
                "MODULE_OWNED_AUDIT_BUILDER_REQUIRED",
                "full trace details can only be emitted by the trusted module builder",
            )
        if self.schema_version != R24_AUDIT_DETAIL_SCHEMA_VERSION:
            raise R24ContractError("UNKNOWN_SCHEMA_VERSION", "unknown audit detail schema")
        _require_id(self.detail_id, "detail_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_id(self.host_id, "host_id")
        _require_id(self.history_codec_id, "history_codec_id", semantic=True)
        _require_id(
            self.history_codec_contract_version,
            "history_codec_contract_version",
            semantic=True,
        )
        if type(self.configured_mode) is not SentinelMode or type(self.effective_mode) is not (
            SentinelMode
        ):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "Sentinel modes are untrusted")
        if type(self.outcome) is not RuntimeAuditOutcomeV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "audit outcome is untrusted")
        if type(self.topology_kind) is not TopologyKind:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "topology kind is untrusted")
        _require_sha256(self.topology_comparison_sha256, "topology_comparison_sha256")
        _require_sha256(self.codec_overlay_sha256, "codec_overlay_sha256")

        for field_name, kind in _ARTIFACT_FIELDS:
            artifact = getattr(self, field_name)
            if type(artifact) is not CanonicalAuditArtifactV1 or artifact.kind is not kind:
                raise R24ContractError(
                    "AUDIT_ARTIFACT_BINDING_MISMATCH",
                    f"{field_name} does not carry the required exact artifact kind",
                )
            # Revalidate current fields instead of trusting that a frozen instance was
            # never altered with ``object.__setattr__`` after construction.
            CanonicalAuditArtifactV1(
                kind=artifact.kind,
                canonical_bytes=bytes(artifact.canonical_bytes),
                sha256=artifact.sha256,
                schema_version=artifact.schema_version,
            )
        if type(self.latencies) is not RuntimeAuditStageLatenciesV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "latencies are untrusted")
        RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=self.latencies.evidence_snapshot_ns,
            history_extract_ns=self.latencies.history_extract_ns,
            rubric_ns=self.latencies.rubric_ns,
            policy_ns=self.latencies.policy_ns,
            render_ns=self.latencies.render_ns,
            validator_ns=self.latencies.validator_ns,
            provider_ns=self.latencies.provider_ns,
            parser_ns=self.latencies.parser_ns,
            total_ns=self.latencies.total_ns,
        )
        if type(self.resources) is not CpuFakeAuditResourceFlagsV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "resource flags are untrusted")
        _snapshot_resource_flags(self.resources)

        if not _are_exact_bools(self.would_edit, self.edit_applied):
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "edit flags must be exact booleans")
        candidate_differs = self.raw_request.sha256 != self.candidate_request.sha256
        final_differs = self.raw_request.sha256 != self.final_request.sha256
        if self.would_edit is not candidate_differs or self.edit_applied is not final_differs:
            raise R24ContractError(
                "EDIT_CENSUS_MISMATCH", "edit flags differ from exact request hashes"
            )
        if self.effective_mode in {SentinelMode.OFF, SentinelMode.SHADOW} and final_differs:
            raise R24ContractError(
                "ORIGINAL_PARITY_VIOLATION", "OFF/SHADOW final request must be exact Original"
            )
        if self.outcome is RuntimeAuditOutcomeV1.FALLBACK_ORIGINAL and final_differs:
            raise R24ContractError(
                "FALLBACK_PARITY_VIOLATION", "fallback must bind the exact Original request"
            )
        if self.outcome is RuntimeAuditOutcomeV1.COMPLETED:
            if self.reason_code is not None:
                raise R24ContractError(
                    "UNEXPECTED_REASON_CODE", "completed audit detail cannot carry a reason"
                )
            if self.effective_mode is SentinelMode.ACTIVE and (
                self.final_request.sha256 != self.candidate_request.sha256
            ):
                raise R24ContractError(
                    "ACTIVE_CANDIDATE_MISMATCH",
                    "completed ACTIVE detail must bind candidate as final",
                )
        else:
            if type(self.reason_code) is not str or _CHECK_CODE.fullmatch(self.reason_code) is None:
                raise R24ContractError(
                    "MISSING_REASON_CODE", "bypass/fallback requires one closed reason code"
                )
        if self.edit_applied and (
            self.effective_mode is not SentinelMode.ACTIVE
            or self.outcome is not RuntimeAuditOutcomeV1.COMPLETED
        ):
            raise R24ContractError(
                "ACTIVE_AUTHORITY_REQUIRED", "only completed ACTIVE may apply a history edit"
            )
        if type(self.validation_checks) is not tuple or not self.validation_checks:
            raise R24ContractError(
                "VALIDATION_CHECKS_MISSING", "audit detail needs validation checks"
            )
        if any(
            type(item) is not str or _CHECK_CODE.fullmatch(item) is None
            for item in self.validation_checks
        ):
            raise R24ContractError(
                "INVALID_VALIDATION_CHECK", "audit validation check is not closed"
            )
        if len(self.validation_checks) != len(set(self.validation_checks)):
            raise R24ContractError("DUPLICATE_VALIDATION_CHECK", "audit validation checks repeat")
        _validate_trace_cross_bindings(self)


def _require_object_artifact(
    value: CanonicalAuditArtifactV1,
    name: str,
) -> dict[str, JsonValue]:
    projected = value.value
    if type(projected) is not dict:
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", f"{name} artifact must be an object projection"
        )
    return projected


def _validate_trace_cross_bindings(value: RuntimeAuditDetailV1) -> None:
    """Recompute the completed trace chain across every retained stage."""

    if value.outcome is not RuntimeAuditOutcomeV1.COMPLETED:
        raise R24ContractError(
            "COMPLETED_TRACE_ENVELOPE_ONLY",
            "R2.4 v1 full-detail envelope represents completed calls only",
        )
    history = _require_object_artifact(value.history_ir, "history_ir")
    policy = _require_object_artifact(value.policy_output, "policy_output")
    rubric = _require_object_artifact(value.rubric_output, "rubric_output")
    render = _require_object_artifact(value.render_result, "render_result")
    validator = _require_object_artifact(value.validator_result, "validator_result")
    provider = _require_object_artifact(value.provider_response, "provider_response")
    parser = _require_object_artifact(value.parser_result, "parser_result")
    action = _require_object_artifact(value.actor_action, "actor_action")

    plan = policy.get("admitted_plan")
    rubric_topology = rubric.get("topology")
    if type(plan) is not dict or type(rubric_topology) is not dict:
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", "policy plan/rubric topology projection is missing"
        )
    plan_mapping = plan
    topology_mapping = rubric_topology
    expected = {
        "history.raw_request_sha256": (history.get("raw_request_sha256"), value.raw_request.sha256),
        "history.host_id": (history.get("host_id"), value.host_id),
        "history.codec_id": (history.get("codec_id"), value.history_codec_id),
        "history.codec_contract_version": (
            history.get("codec_contract_version"),
            value.history_codec_contract_version,
        ),
        "plan.logical_call_id": (plan_mapping.get("logical_call_id"), value.logical_call_id),
        "plan.host_id": (plan_mapping.get("host_id"), value.host_id),
        "plan.history_codec_id": (
            plan_mapping.get("history_codec_id"),
            value.history_codec_id,
        ),
        "plan.history_codec_contract_version": (
            plan_mapping.get("history_codec_contract_version"),
            value.history_codec_contract_version,
        ),
        "plan.source_request_sha256": (
            plan_mapping.get("source_request_sha256"),
            value.raw_request.sha256,
        ),
        "rubric.logical_call_id": (rubric.get("logical_call_id"), value.logical_call_id),
        "rubric.topology": (topology_mapping.get("kind"), value.topology_kind.value),
        "render.source_request_sha256": (
            render.get("source_request_sha256"),
            value.raw_request.sha256,
        ),
        "render.candidate_request_sha256": (
            render.get("candidate_request_sha256"),
            value.candidate_request.sha256,
        ),
        "render.admitted_plan_sha256": (
            render.get("admitted_plan_sha256"),
            canonical_sha256(cast(JsonValue, plan_mapping)),
        ),
        "render.exact_diff_sha256": (render.get("exact_diff_sha256"), value.exact_diff.sha256),
        "validator.logical_call_id": (validator.get("logical_call_id"), value.logical_call_id),
        "validator.host_id": (validator.get("host_id"), value.host_id),
        "validator.effective_mode": (
            validator.get("effective_mode"),
            value.effective_mode.value,
        ),
        "validator.topology_kind": (
            validator.get("topology_kind"),
            value.topology_kind.value,
        ),
        "validator.codec_overlay_sha256": (
            validator.get("codec_overlay_sha256"),
            value.codec_overlay_sha256,
        ),
        "validator.raw_request_sha256": (
            validator.get("raw_request_sha256"),
            value.raw_request.sha256,
        ),
        "validator.history_ir_sha256": (
            validator.get("history_ir_sha256"),
            value.history_ir.sha256,
        ),
        "validator.policy_output_sha256": (
            validator.get("policy_output_sha256"),
            value.policy_output.sha256,
        ),
        "validator.rubric_output_sha256": (
            validator.get("rubric_output_sha256"),
            value.rubric_output.sha256,
        ),
        "validator.render_result_sha256": (
            validator.get("render_result_sha256"),
            value.render_result.sha256,
        ),
        "validator.candidate_request_sha256": (
            validator.get("candidate_request_sha256"),
            value.candidate_request.sha256,
        ),
        "validator.exact_diff_sha256": (
            validator.get("exact_diff_sha256"),
            value.exact_diff.sha256,
        ),
        "validator.final_request_sha256": (
            validator.get("final_request_sha256"),
            value.final_request.sha256,
        ),
        "provider.logical_call_id": (provider.get("logical_call_id"), value.logical_call_id),
        "provider.final_request_sha256": (
            provider.get("final_request_sha256"),
            value.final_request.sha256,
        ),
        "parser.logical_call_id": (parser.get("logical_call_id"), value.logical_call_id),
        "parser.attempt_id": (parser.get("attempt_id"), provider.get("attempt_id")),
        "parser.final_request_sha256": (
            parser.get("final_request_sha256"),
            value.final_request.sha256,
        ),
        "parser.raw_provider_response_sha256": (
            parser.get("raw_provider_response_sha256"),
            provider.get("raw_provider_response_sha256"),
        ),
        "parser.actor_action_sha256": (
            parser.get("actor_action_sha256"),
            action.get("action_sha256"),
        ),
        "parser.normalized_actor_output_sha256": (
            parser.get("normalized_actor_output_sha256"),
            action.get("action_sha256"),
        ),
    }
    mismatch = next((name for name, pair in expected.items() if pair[0] != pair[1]), None)
    if mismatch is not None:
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", f"trace stage binding differs at {mismatch}"
        )
    if validator.get("validation_checks") != list(value.validation_checks):
        raise R24ContractError("TRACE_CROSS_BINDING_MISMATCH", "validator and detail checks differ")


_COMPLETED_TRACE_CHECKS = (
    "TRUSTED_STAGE_TYPES_SNAPSHOTTED",
    "RAW_HISTORY_POLICY_RUBRIC_BOUND",
    "RENDER_AND_EXACT_DIFF_REVALIDATED",
    "FINAL_PROVIDER_ATTEMPT_BOUND",
    "PROVIDER_CONTENT_HASH_ONLY",
    "PARSER_AND_ACTOR_ACTION_BOUND",
    "ACTION_NOT_EXECUTED",
    "COLLECTOR_UNTOUCHED",
)


def build_completed_cpu_fake_audit_detail(
    *,
    detail_id: str,
    raw_request: JsonValue,
    extraction: RuntimeHistoryExtractionResultV1,
    policy_output: RuntimeVerticalPolicyOutputV1,
    rubric_output: PathRelevanceOutputV1,
    render_result: RuntimeVerticalRenderResultV1,
    configured_mode: SentinelMode,
    effective_mode: SentinelMode,
    final_request: JsonValue,
    topology_comparison_sha256: str,
    attempt_id: str,
    raw_provider_response: JsonValue,
    raw_parser_input: JsonValue,
    parsed_action: JsonValue,
    parser_id: str,
    parser_status: ParserResultStatusV1,
    parser_attempt_count: int,
    latencies: RuntimeAuditStageLatenciesV1,
    response_id: str | None = None,
    model_id: str | None = None,
    finish_reason: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
) -> RuntimeAuditDetailV1:
    """Build the only v1 full-detail envelope from exact trusted stage types."""

    if type(extraction) is not RuntimeHistoryExtractionResultV1 or (
        extraction.status is not RuntimeHistoryExtractionStatusV1.READY
        or type(extraction.history_ir) is not HistoryIR
    ):
        raise R24ContractError(
            "INCOMPLETE_TRUSTED_TRACE", "completed detail requires one READY extraction"
        )
    if type(policy_output) is not RuntimeVerticalPolicyOutputV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "policy output must use exact type")
    if type(rubric_output) is not PathRelevanceOutputV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "rubric output must use exact type")
    if type(render_result) is not RuntimeVerticalRenderResultV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "render result must use exact type")
    if type(latencies) is not RuntimeAuditStageLatenciesV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "latencies must use exact type")

    raw = snapshot_json_value(raw_request)
    final = snapshot_json_value(final_request)
    policy = snapshot_vertical_output(policy_output)
    rubric = snapshot_path_relevance_output(rubric_output)
    render = snapshot_vertical_render_result(render_result)
    if (
        policy.execution_scope is not RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
        or render.execution_scope is not RuntimeVerticalExecutionScope.CPU_FAKE_ACTIVE
    ):
        raise R24ContractError(
            "CPU_FAKE_TRACE_REQUIRED",
            "the CPU/fake detail contract cannot attest a live execution",
        )
    history = extraction.history_ir
    validate_history_ir(raw, history)
    validate_vertical_render_result(raw, history, policy.admitted_plan, render)

    overlay_hash = canonical_sha256(cast(JsonValue, _codec_overlay_projection(extraction.overlay)))
    if extraction.raw_request_sha256 != canonical_sha256(raw) or (
        extraction.overlay.host_id != history.host_id
        or extraction.overlay.base_codec_id != history.codec_id
        or extraction.overlay.base_codec_contract_version != history.codec_contract_version
    ):
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", "extraction differs from raw request/History IR"
        )
    plan = policy.admitted_plan
    if (
        plan.logical_call_id != rubric.logical_call_id
        or plan.host_id != history.host_id
        or plan.history_family != history.history_family.value
        or plan.history_codec_id != history.codec_id
        or plan.history_codec_contract_version != history.codec_contract_version
        or plan.source_request_sha256 != canonical_sha256(raw)
        or rubric.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE
    ):
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", "policy/rubric/history authority differs"
        )
    record_ids = {record.record_id for record in history.records}
    if any(record.record_id not in record_ids for record in rubric.records):
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", "rubric output references a foreign history record"
        )
    if effective_mode in {SentinelMode.OFF, SentinelMode.SHADOW}:
        if canonical_sha256(final) != canonical_sha256(raw):
            raise R24ContractError(
                "ORIGINAL_PARITY_VIOLATION", "OFF/SHADOW provider request must be Original"
            )
    elif effective_mode is SentinelMode.ACTIVE:
        if canonical_sha256(final) != render.candidate_request_sha256:
            raise R24ContractError(
                "ACTIVE_CANDIDATE_MISMATCH", "ACTIVE provider request must be validated candidate"
            )
    else:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "effective mode is untrusted")

    history_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.HISTORY_IR,
        cast(JsonValue, trusted_history_ir_projection(history)),
    )
    policy_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.POLICY_OUTPUT,
        cast(JsonValue, vertical_output_projection(policy)),
    )
    rubric_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.RUBRIC_OUTPUT,
        cast(JsonValue, path_relevance_output_projection(rubric)),
    )
    render_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.RENDER_RESULT,
        cast(JsonValue, vertical_render_result_projection(render)),
    )
    exact_diff: dict[str, JsonValue] = {
        "text_diffs": [vertical_text_diff_projection(item) for item in render.text_diffs],
        "source_mappings": [
            vertical_source_mapping_projection(item) for item in render.source_mappings
        ],
    }
    if canonical_sha256(exact_diff) != render.exact_diff_sha256:
        raise R24ContractError(
            "TRACE_CROSS_BINDING_MISMATCH", "render result differs from exact diff projection"
        )
    exact_diff_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.EXACT_DIFF,
        exact_diff,
    )
    raw_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.RAW_REQUEST,
        raw,
    )
    candidate_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.CANDIDATE_REQUEST,
        render.candidate_request,
    )
    final_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.FINAL_REQUEST,
        final,
    )
    action_snapshot = snapshot_json_value(parsed_action)
    action_artifact = capture_actor_action_binding(action_snapshot)
    provider_artifact = capture_cpu_fake_provider_response_binding(
        raw_provider_response,
        logical_call_id=plan.logical_call_id,
        attempt_id=attempt_id,
        final_request_sha256=final_artifact.sha256,
        response_id=response_id,
        model_id=model_id,
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
    provider_projection = cast(dict[str, JsonValue], provider_artifact.value)
    parser_artifact = capture_parser_result_binding(
        raw_parser_input,
        action_snapshot,
        parser_id=parser_id,
        status=parser_status,
        attempt_count=parser_attempt_count,
        logical_call_id=plan.logical_call_id,
        attempt_id=attempt_id,
        final_request_sha256=final_artifact.sha256,
        raw_provider_response_sha256=cast(str, provider_projection["raw_provider_response_sha256"]),
        actor_action_sha256=cast(
            str,
            cast(dict[str, JsonValue], action_artifact.value)["action_sha256"],
        ),
    )
    validator_projection: dict[str, JsonValue] = {
        "schema_version": R24_VALIDATOR_BINDING_SCHEMA_VERSION,
        "status": "PASSED",
        "logical_call_id": plan.logical_call_id,
        "host_id": plan.host_id,
        "effective_mode": effective_mode.value,
        "topology_kind": rubric.topology.kind.value,
        "codec_overlay_sha256": overlay_hash,
        "raw_request_sha256": raw_artifact.sha256,
        "history_ir_sha256": history_artifact.sha256,
        "policy_output_sha256": policy_artifact.sha256,
        "rubric_output_sha256": rubric_artifact.sha256,
        "render_result_sha256": render_artifact.sha256,
        "candidate_request_sha256": candidate_artifact.sha256,
        "exact_diff_sha256": exact_diff_artifact.sha256,
        "final_request_sha256": final_artifact.sha256,
        "validation_checks": list(_COMPLETED_TRACE_CHECKS),
    }
    _validate_validator_binding(validator_projection)
    validator_artifact = CanonicalAuditArtifactV1.capture(
        RuntimeAuditArtifactKindV1.VALIDATOR_RESULT,
        validator_projection,
    )
    return RuntimeAuditDetailV1(
        detail_id=detail_id,
        logical_call_id=plan.logical_call_id,
        host_id=plan.host_id,
        configured_mode=configured_mode,
        effective_mode=effective_mode,
        outcome=RuntimeAuditOutcomeV1.COMPLETED,
        topology_kind=rubric.topology.kind,
        topology_comparison_sha256=topology_comparison_sha256,
        history_codec_id=plan.history_codec_id,
        history_codec_contract_version=plan.history_codec_contract_version,
        codec_overlay_sha256=overlay_hash,
        raw_request=raw_artifact,
        history_ir=history_artifact,
        policy_output=policy_artifact,
        rubric_output=rubric_artifact,
        render_result=render_artifact,
        candidate_request=candidate_artifact,
        exact_diff=exact_diff_artifact,
        validator_result=validator_artifact,
        final_request=final_artifact,
        provider_response=provider_artifact,
        parser_result=parser_artifact,
        actor_action=action_artifact,
        latencies=RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=latencies.evidence_snapshot_ns,
            history_extract_ns=latencies.history_extract_ns,
            rubric_ns=latencies.rubric_ns,
            policy_ns=latencies.policy_ns,
            render_ns=latencies.render_ns,
            validator_ns=latencies.validator_ns,
            provider_ns=latencies.provider_ns,
            parser_ns=latencies.parser_ns,
            total_ns=latencies.total_ns,
        ),
        resources=CpuFakeAuditResourceFlagsV1(),
        would_edit=raw_artifact.sha256 != candidate_artifact.sha256,
        edit_applied=raw_artifact.sha256 != final_artifact.sha256,
        validation_checks=_COMPLETED_TRACE_CHECKS,
        _builder_token=_AUDIT_DETAIL_BUILDER_TOKEN,
    )


def _snapshot_ready_extraction(
    value: RuntimeHistoryExtractionResultV1,
) -> RuntimeHistoryExtractionResultV1:
    """Detach the exact extraction authority without retaining backend-owned graphs."""

    if type(value) is not RuntimeHistoryExtractionResultV1 or (
        value.status is not RuntimeHistoryExtractionStatusV1.READY
        or type(value.history_ir) is not HistoryIR
    ):
        raise R24ContractError(
            "INCOMPLETE_TRUSTED_TRACE", "completed detail requires one READY extraction"
        )
    overlay = value.overlay
    if type(overlay) is not RuntimeCodecOverlayDeclarationV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "codec overlay must use exact type")
    detached_overlay = RuntimeCodecOverlayDeclarationV1(
        overlay_id=overlay.overlay_id,
        host_id=overlay.host_id,
        history_family=overlay.history_family,
        base_codec_id=overlay.base_codec_id,
        base_codec_contract_version=overlay.base_codec_contract_version,
        base_capability_sha256=overlay.base_capability_sha256,
        implementation_sha256=overlay.implementation_sha256,
        discovery_mode=overlay.discovery_mode,
        live_ready=overlay.live_ready,
        schema_version=overlay.schema_version,
    )
    try:
        detached_capabilities = deepcopy(value.capabilities)
        detached_history = deepcopy(value.history_ir)
    except (RecursionError, TypeError, ValueError) as exc:
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_GRAPH", "runtime extraction could not be detached"
        ) from exc
    if type(detached_capabilities) is not CodecCapabilities or type(detached_history) is not (
        HistoryIR
    ):
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_TYPE", "extraction graph contains an untrusted runtime type"
        )
    # This module-owned traversal supplies the cycle/depth/node budget that the
    # historical HistoryIR serializer itself does not guarantee.
    trusted_history_ir_projection(detached_history)
    try:
        return RuntimeHistoryExtractionResultV1(
            status=value.status,
            raw_request_sha256=value.raw_request_sha256,
            overlay=detached_overlay,
            capabilities=detached_capabilities,
            history_ir=detached_history,
            reason_code=value.reason_code,
            validation_checks=tuple(value.validation_checks),
            warnings=tuple(value.warnings),
            schema_version=value.schema_version,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise R24ContractError(
            "UNTRUSTED_RUNTIME_GRAPH", "runtime extraction snapshot failed validation"
        ) from exc


class RuntimeAuditDetailBuilderV1:
    """One-shot two-phase builder for a fully cross-bound CPU/fake trace.

    ``begin_pre_provider`` accepts only exact trusted pre-provider stage types and
    immediately rebuilds detached authority snapshots.  ``finalize_actor_output``
    receives the transient provider/parser content, hashes it, and retains only
    safe metadata plus the ordinary parsed action projection.  Neither method
    performs transport, parsing, action selection, or action execution.
    """

    _configured_mode: SentinelMode
    _detail_id: str
    _effective_mode: SentinelMode
    _extraction: RuntimeHistoryExtractionResultV1
    _final_request: JsonValue
    _finished: bool
    _lock: Any
    _policy_output: RuntimeVerticalPolicyOutputV1
    _pre_provider_latencies: RuntimeAuditStageLatenciesV1
    _raw_request: JsonValue
    _render_result: RuntimeVerticalRenderResultV1
    _rubric_output: PathRelevanceOutputV1
    _topology_comparison_sha256: str

    __slots__ = (
        "_configured_mode",
        "_detail_id",
        "_effective_mode",
        "_extraction",
        "_final_request",
        "_finished",
        "_lock",
        "_policy_output",
        "_pre_provider_latencies",
        "_raw_request",
        "_render_result",
        "_rubric_output",
        "_topology_comparison_sha256",
    )

    def __init__(self) -> None:
        raise TypeError("use RuntimeAuditDetailBuilderV1.begin_pre_provider")

    @classmethod
    def begin_pre_provider(
        cls,
        *,
        detail_id: str,
        raw_request: JsonValue,
        extraction: RuntimeHistoryExtractionResultV1,
        policy_output: RuntimeVerticalPolicyOutputV1,
        rubric_output: PathRelevanceOutputV1,
        render_result: RuntimeVerticalRenderResultV1,
        configured_mode: SentinelMode,
        effective_mode: SentinelMode,
        final_request: JsonValue,
        topology_comparison_sha256: str,
        pre_provider_latencies: RuntimeAuditStageLatenciesV1,
    ) -> RuntimeAuditDetailBuilderV1:
        """Bind and detach every stage available before provider transport."""

        if type(pre_provider_latencies) is not RuntimeAuditStageLatenciesV1:
            raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "latencies must use exact type")
        pre_latencies = RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=pre_provider_latencies.evidence_snapshot_ns,
            history_extract_ns=pre_provider_latencies.history_extract_ns,
            rubric_ns=pre_provider_latencies.rubric_ns,
            policy_ns=pre_provider_latencies.policy_ns,
            render_ns=pre_provider_latencies.render_ns,
            validator_ns=pre_provider_latencies.validator_ns,
            provider_ns=pre_provider_latencies.provider_ns,
            parser_ns=pre_provider_latencies.parser_ns,
            total_ns=pre_provider_latencies.total_ns,
        )
        if pre_latencies.provider_ns != 0 or pre_latencies.parser_ns != 0:
            raise R24ContractError(
                "PRE_PROVIDER_LATENCY_SCOPE",
                "pre-provider latency snapshot cannot claim provider/parser work",
            )
        raw = snapshot_json_value(raw_request)
        final = snapshot_json_value(final_request)
        detached_extraction = _snapshot_ready_extraction(extraction)
        detached_policy = snapshot_vertical_output(policy_output)
        detached_rubric = snapshot_path_relevance_output(rubric_output)
        detached_render = snapshot_vertical_render_result(render_result)

        # Reuse the complete trusted construction path as a preflight.  The
        # placeholders are local, safe, and never returned or written to a sink.
        build_completed_cpu_fake_audit_detail(
            detail_id=detail_id,
            raw_request=raw,
            extraction=detached_extraction,
            policy_output=detached_policy,
            rubric_output=detached_rubric,
            render_result=detached_render,
            configured_mode=configured_mode,
            effective_mode=effective_mode,
            final_request=final,
            topology_comparison_sha256=topology_comparison_sha256,
            attempt_id="pre-provider-preflight",
            raw_provider_response={"kind": "pre_provider_preflight"},
            raw_parser_input={"kind": "pre_provider_preflight"},
            parsed_action={"action_type": "pre_provider_preflight"},
            parser_id="r24.pre-provider-preflight",
            parser_status=ParserResultStatusV1.PARSED,
            parser_attempt_count=1,
            latencies=pre_latencies,
        )

        instance = object.__new__(cls)
        instance._detail_id = detail_id
        instance._raw_request = raw
        instance._extraction = detached_extraction
        instance._policy_output = detached_policy
        instance._rubric_output = detached_rubric
        instance._render_result = detached_render
        instance._configured_mode = configured_mode
        instance._effective_mode = effective_mode
        instance._final_request = final
        instance._topology_comparison_sha256 = topology_comparison_sha256
        instance._pre_provider_latencies = pre_latencies
        instance._lock = Lock()
        instance._finished = False
        return instance

    def finalize_actor_output(
        self,
        *,
        attempt_id: str,
        raw_provider_response: JsonValue,
        raw_parser_input: JsonValue,
        parsed_action: JsonValue,
        parser_id: str,
        parser_status: ParserResultStatusV1,
        parser_attempt_count: int,
        provider_ns: int,
        parser_ns: int,
        total_ns: int,
        response_id: str | None = None,
        model_id: str | None = None,
        finish_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> RuntimeAuditDetailV1:
        """Finalize once; provider/parser inputs are hashed but never retained."""

        with self._lock:
            if self._finished:
                raise R24ContractError(
                    "AUDIT_BUILDER_ALREADY_FINALIZED", "audit detail builder is one-shot"
                )
            self._finished = True
        pre = self._pre_provider_latencies
        latencies = RuntimeAuditStageLatenciesV1(
            evidence_snapshot_ns=pre.evidence_snapshot_ns,
            history_extract_ns=pre.history_extract_ns,
            rubric_ns=pre.rubric_ns,
            policy_ns=pre.policy_ns,
            render_ns=pre.render_ns,
            validator_ns=pre.validator_ns,
            provider_ns=provider_ns,
            parser_ns=parser_ns,
            total_ns=total_ns,
        )
        return build_completed_cpu_fake_audit_detail(
            detail_id=self._detail_id,
            raw_request=self._raw_request,
            extraction=self._extraction,
            policy_output=self._policy_output,
            rubric_output=self._rubric_output,
            render_result=self._render_result,
            configured_mode=self._configured_mode,
            effective_mode=self._effective_mode,
            final_request=self._final_request,
            topology_comparison_sha256=self._topology_comparison_sha256,
            attempt_id=attempt_id,
            raw_provider_response=raw_provider_response,
            raw_parser_input=raw_parser_input,
            parsed_action=parsed_action,
            parser_id=parser_id,
            parser_status=parser_status,
            parser_attempt_count=parser_attempt_count,
            latencies=latencies,
            response_id=response_id,
            model_id=model_id,
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )


def audit_artifact_projection(value: CanonicalAuditArtifactV1) -> dict[str, JsonValue]:
    value = _snapshot_artifact(value)
    return {
        "schema_version": value.schema_version,
        "kind": value.kind.value,
        "sha256": value.sha256,
        "value": value.value,
    }


def audit_stage_latencies_projection(
    value: RuntimeAuditStageLatenciesV1,
) -> dict[str, JsonValue]:
    if type(value) is not RuntimeAuditStageLatenciesV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "latencies must use the exact type")
    value = RuntimeAuditStageLatenciesV1(
        evidence_snapshot_ns=value.evidence_snapshot_ns,
        history_extract_ns=value.history_extract_ns,
        rubric_ns=value.rubric_ns,
        policy_ns=value.policy_ns,
        render_ns=value.render_ns,
        validator_ns=value.validator_ns,
        provider_ns=value.provider_ns,
        parser_ns=value.parser_ns,
        total_ns=value.total_ns,
    )
    return {
        "evidence_snapshot_ns": value.evidence_snapshot_ns,
        "history_extract_ns": value.history_extract_ns,
        "rubric_ns": value.rubric_ns,
        "policy_ns": value.policy_ns,
        "render_ns": value.render_ns,
        "validator_ns": value.validator_ns,
        "provider_ns": value.provider_ns,
        "parser_ns": value.parser_ns,
        "total_ns": value.total_ns,
    }


def audit_resource_flags_projection(
    value: CpuFakeAuditResourceFlagsV1,
) -> dict[str, JsonValue]:
    value = _snapshot_resource_flags(value)
    return {
        "cpu_only": value.cpu_only,
        "offline": value.offline,
        "fake_policy_backend": value.fake_policy_backend,
        "fake_rubric_backend": value.fake_rubric_backend,
        "fake_actor_provider": value.fake_actor_provider,
        "external_network_attempted": value.external_network_attempted,
        "live_model_call_attempted": value.live_model_call_attempted,
        "gpu_used": value.gpu_used,
        "mobileworld_backend_used": value.mobileworld_backend_used,
        "emulator_used": value.emulator_used,
        "gui_tool_invoked": value.gui_tool_invoked,
        "action_executed": value.action_executed,
        "sentinel_selected_action": value.sentinel_selected_action,
        "collector_raw_mutated": value.collector_raw_mutated,
        "detail_written_to_collector": value.detail_written_to_collector,
        "detail_written_inside_repository": value.detail_written_inside_repository,
        "secret_material_persisted": value.secret_material_persisted,
        "reasoning_persisted": value.reasoning_persisted,
    }


def runtime_audit_detail_projection(value: RuntimeAuditDetailV1) -> dict[str, JsonValue]:
    value = snapshot_runtime_audit_detail(value)
    artifacts: dict[str, JsonValue] = {
        name: cast(JsonValue, audit_artifact_projection(getattr(value, name)))
        for name, _kind in _ARTIFACT_FIELDS
    }
    return {
        "schema_version": value.schema_version,
        "detail_id": value.detail_id,
        "logical_call_id": value.logical_call_id,
        "host_id": value.host_id,
        "configured_mode": value.configured_mode.value,
        "effective_mode": value.effective_mode.value,
        "outcome": value.outcome.value,
        "topology_kind": value.topology_kind.value,
        "topology_comparison_sha256": value.topology_comparison_sha256,
        "history_codec_id": value.history_codec_id,
        "history_codec_contract_version": value.history_codec_contract_version,
        "codec_overlay_sha256": value.codec_overlay_sha256,
        "artifacts": artifacts,
        "latencies": cast(JsonValue, audit_stage_latencies_projection(value.latencies)),
        "resources": cast(JsonValue, audit_resource_flags_projection(value.resources)),
        "would_edit": value.would_edit,
        "edit_applied": value.edit_applied,
        "validation_checks": list(value.validation_checks),
        "reason_code": value.reason_code,
    }


def runtime_audit_detail_sha256(value: RuntimeAuditDetailV1) -> str:
    return canonical_sha256(cast(JsonValue, runtime_audit_detail_projection(value)))


def _snapshot_artifact(value: CanonicalAuditArtifactV1) -> CanonicalAuditArtifactV1:
    if type(value) is not CanonicalAuditArtifactV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "artifact must use the exact type")
    return CanonicalAuditArtifactV1(
        kind=value.kind,
        canonical_bytes=bytes(value.canonical_bytes),
        sha256=value.sha256,
        schema_version=value.schema_version,
    )


def _snapshot_resource_flags(value: CpuFakeAuditResourceFlagsV1) -> CpuFakeAuditResourceFlagsV1:
    if type(value) is not CpuFakeAuditResourceFlagsV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "resource flags must use exact type")
    return CpuFakeAuditResourceFlagsV1(
        cpu_only=value.cpu_only,
        offline=value.offline,
        fake_policy_backend=value.fake_policy_backend,
        fake_rubric_backend=value.fake_rubric_backend,
        fake_actor_provider=value.fake_actor_provider,
        external_network_attempted=value.external_network_attempted,
        live_model_call_attempted=value.live_model_call_attempted,
        gpu_used=value.gpu_used,
        mobileworld_backend_used=value.mobileworld_backend_used,
        emulator_used=value.emulator_used,
        gui_tool_invoked=value.gui_tool_invoked,
        action_executed=value.action_executed,
        sentinel_selected_action=value.sentinel_selected_action,
        collector_raw_mutated=value.collector_raw_mutated,
        detail_written_to_collector=value.detail_written_to_collector,
        detail_written_inside_repository=value.detail_written_inside_repository,
        secret_material_persisted=value.secret_material_persisted,
        reasoning_persisted=value.reasoning_persisted,
    )


def snapshot_runtime_audit_detail(value: RuntimeAuditDetailV1) -> RuntimeAuditDetailV1:
    if type(value) is not RuntimeAuditDetailV1:
        raise R24ContractError("UNTRUSTED_RUNTIME_TYPE", "detail must use the exact type")
    artifacts = {name: _snapshot_artifact(getattr(value, name)) for name, _ in _ARTIFACT_FIELDS}
    latencies = RuntimeAuditStageLatenciesV1(
        evidence_snapshot_ns=value.latencies.evidence_snapshot_ns,
        history_extract_ns=value.latencies.history_extract_ns,
        rubric_ns=value.latencies.rubric_ns,
        policy_ns=value.latencies.policy_ns,
        render_ns=value.latencies.render_ns,
        validator_ns=value.latencies.validator_ns,
        provider_ns=value.latencies.provider_ns,
        parser_ns=value.latencies.parser_ns,
        total_ns=value.latencies.total_ns,
    )
    resources = _snapshot_resource_flags(value.resources)
    return RuntimeAuditDetailV1(
        detail_id=value.detail_id,
        logical_call_id=value.logical_call_id,
        host_id=value.host_id,
        configured_mode=value.configured_mode,
        effective_mode=value.effective_mode,
        outcome=value.outcome,
        topology_kind=value.topology_kind,
        topology_comparison_sha256=value.topology_comparison_sha256,
        history_codec_id=value.history_codec_id,
        history_codec_contract_version=value.history_codec_contract_version,
        codec_overlay_sha256=value.codec_overlay_sha256,
        latencies=latencies,
        resources=resources,
        would_edit=value.would_edit,
        edit_applied=value.edit_applied,
        validation_checks=tuple(value.validation_checks),
        reason_code=value.reason_code,
        schema_version=value.schema_version,
        raw_request=artifacts["raw_request"],
        history_ir=artifacts["history_ir"],
        policy_output=artifacts["policy_output"],
        rubric_output=artifacts["rubric_output"],
        render_result=artifacts["render_result"],
        candidate_request=artifacts["candidate_request"],
        exact_diff=artifacts["exact_diff"],
        validator_result=artifacts["validator_result"],
        final_request=artifacts["final_request"],
        provider_response=artifacts["provider_response"],
        parser_result=artifacts["parser_result"],
        actor_action=artifacts["actor_action"],
        _builder_token=_AUDIT_DETAIL_BUILDER_TOKEN,
    )


@runtime_checkable
class RuntimeAuditDetailSinkV1(Protocol):
    """Derived-detail sink; implementations must never target Collector storage."""

    def emit(self, detail: RuntimeAuditDetailV1) -> None: ...


class MemoryRuntimeAuditDetailSinkV1:
    """Thread-safe CPU test sink; no value is written to disk or Collector."""

    def __init__(self) -> None:
        self._details: list[RuntimeAuditDetailV1] = []
        self._logical_call_ids: set[str] = set()
        self._lock = Lock()

    def emit(self, detail: RuntimeAuditDetailV1) -> None:
        snapshot = snapshot_runtime_audit_detail(detail)
        with self._lock:
            if snapshot.logical_call_id in self._logical_call_ids:
                raise FileExistsError("runtime audit detail already exists for logical call")
            self._logical_call_ids.add(snapshot.logical_call_id)
            self._details.append(snapshot)

    @property
    def details(self) -> tuple[RuntimeAuditDetailV1, ...]:
        with self._lock:
            return tuple(snapshot_runtime_audit_detail(item) for item in self._details)


def _discover_repository_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate.resolve()
    raise RuntimeError("cannot discover Git repository root for audit exclusion")


class ExternalRuntimeAuditDetailSinkV1:
    """Publish canonical details atomically under an owner-only external root."""

    def __init__(self, root: Path, *, repository_root: Path | None = None) -> None:
        if not root.is_absolute():
            raise ValueError("runtime audit detail root must be an absolute Path")
        repositories = {_discover_repository_root()}
        if repository_root is not None:
            repositories.add(repository_root.resolve(strict=True))
        parent = root.parent.resolve(strict=True)
        resolved = parent / root.name
        if any(
            resolved == repository or resolved.is_relative_to(repository)
            for repository in repositories
        ):
            raise ValueError("runtime audit details must remain outside the Git repository")
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("runtime audit detail root must be a real directory")
        if (
            stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
            or info.st_gid != os.getegid()
        ):
            raise PermissionError("runtime audit detail root must be owner-only")
        self._root = resolved
        self._identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        self._logical_call_ids: set[str] = set()
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self._root, flags)
        self._validate_open_root(descriptor)
        return descriptor

    def _validate_open_root(self, descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != self._identity
        ):
            raise OSError("runtime audit detail root identity or mode changed")

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short runtime audit detail write")
            remaining = remaining[written:]

    def emit(self, detail: RuntimeAuditDetailV1) -> None:
        snapshot = snapshot_runtime_audit_detail(detail)
        payload = canonical_json_bytes(cast(JsonValue, runtime_audit_detail_projection(snapshot)))
        if len(payload) > _MAX_DETAIL_BYTES:
            raise R24ContractError("AUDIT_DETAIL_TOO_LARGE", "detail exceeds its byte budget")
        destination = f"{snapshot.logical_call_id}.runtime-audit-detail.v1.json"
        temporary = f".{snapshot.logical_call_id}.{secrets.token_hex(12)}.tmp"
        with self._lock:
            if snapshot.logical_call_id in self._logical_call_ids:
                raise FileExistsError("runtime audit detail already emitted for logical call")
            self._logical_call_ids.add(snapshot.logical_call_id)

        directory_fd = -1
        file_fd = -1
        created = False
        published = False
        try:
            directory_fd = self._open_root()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            created = True
            self._write_all(file_fd, payload)
            os.fsync(file_fd)
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_nlink != 1
                or info.st_size != len(payload)
            ):
                raise OSError("runtime audit detail file metadata changed")
            self._validate_open_root(directory_fd)
            os.link(
                temporary,
                destination,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary, dir_fd=directory_fd)
            created = False
            os.fsync(directory_fd)
        except Exception:
            if created and directory_fd >= 0:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            if not published:
                with self._lock:
                    self._logical_call_ids.discard(snapshot.logical_call_id)
            raise
        finally:
            if file_fd >= 0:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
            if directory_fd >= 0:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass


__all__ = [
    "CanonicalAuditArtifactV1",
    "CpuFakeAuditResourceFlagsV1",
    "CpuFakeProviderKindV1",
    "ExternalRuntimeAuditDetailSinkV1",
    "MemoryRuntimeAuditDetailSinkV1",
    "R24_AUDIT_ARTIFACT_SCHEMA_VERSION",
    "R24_AUDIT_DETAIL_SCHEMA_VERSION",
    "R24_ACTION_BINDING_SCHEMA_VERSION",
    "R24_PARSER_BINDING_SCHEMA_VERSION",
    "R24_PROVIDER_BINDING_SCHEMA_VERSION",
    "ParserResultStatusV1",
    "RuntimeAuditArtifactKindV1",
    "RuntimeAuditDetailBuilderV1",
    "RuntimeAuditDetailSinkV1",
    "RuntimeAuditDetailV1",
    "RuntimeAuditOutcomeV1",
    "RuntimeAuditStageLatenciesV1",
    "audit_artifact_projection",
    "audit_resource_flags_projection",
    "audit_stage_latencies_projection",
    "build_completed_cpu_fake_audit_detail",
    "capture_actor_action_binding",
    "capture_cpu_fake_provider_response_binding",
    "capture_parser_result_binding",
    "runtime_audit_detail_projection",
    "runtime_audit_detail_sha256",
    "snapshot_runtime_audit_detail",
]
