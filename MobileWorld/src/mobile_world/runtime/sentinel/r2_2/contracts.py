"""Trusted value contracts for the R2.2 SHADOW-only runtime policy.

R2.2 values are automatic deployment predictions.  They deliberately do not
reuse or impersonate the frozen, human-curated G1.2 ``TransformationPlan``.
Canonical views are built only by module-owned functions which recursively
require exact trusted types; no overridable serializer participates in a hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import HistoryIR, JsonPath, JsonValue
from mobile_world.runtime.sentinel.contracts import SentinelContext

EVIDENCE_PACKET_SCHEMA_VERSION = "mobileworld.runtime.sentinel-evidence-packet/v1"
POLICY_PROPOSAL_SCHEMA_VERSION = "mobileworld.runtime.sentinel-policy-proposal/v1"
RUNTIME_ADMITTED_PLAN_SCHEMA_VERSION = "mobileworld.runtime.sentinel-runtime-admitted-plan/v1"
RUNTIME_POLICY_OUTPUT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-policy-output/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CONTRACT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_MAX_RATIONALE_CHARS = 256
_COORDINATE_DIRECTIVE = re.compile(
    r"(?:\(\s*-?\d{1,6}\s*,\s*-?\d{1,6}\s*\)|\b[xy]\s*[=:]\s*-?\d{1,6}\b)",
    re.IGNORECASE,
)
_ACTION_OR_TOOL_DIRECTIVE = re.compile(
    r"(?:"
    r"\b(?:tap|click|swipe|scroll|press|type|select)\b|"
    r"^\s*(?:open|close|delete|remove|submit|send|create|change|set|enter|choose|"
    r"navigate|launch|install|uninstall|enable|disable|turn|move|drag|upload|"
    r"download|save|edit|add|reply|post|like|favorite)\b|"
    r"\b(?:call|invoke|run)\s+(?:the\s+)?(?:tool|function)\b|"
    r"</?(?:tool_call|function_call)>|"
    r"[\"']action(?:_type)?[\"']\s*:|"
    r"\bmobile_use\b"
    r")",
    re.IGNORECASE,
)
_RETROACTIVE_ACTOR_VOICE = re.compile(
    r"(?:"
    r"^\s*(?:assistant|agent|actor)\s*:|"
    r"\bI\s+(?:have\s+)?(?:clicked|tapped|swiped|opened|closed|selected|entered|"
    r"typed|completed|finished|submitted|deleted|created|changed|set)\b"
    r")",
    re.IGNORECASE,
)


class R22ContractError(ValueError):
    """Deterministic local rejection with a stable validation code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class EvidenceCutoffKind(StrEnum):
    ACTOR_REQUEST_PRE_SEND = "ACTOR_REQUEST_PRE_SEND"


class TaskDataRole(StrEnum):
    TASK_INSTRUCTION_DATA = "TASK_INSTRUCTION_DATA"


class SourceEventType(StrEnum):
    TASK_STARTED = "task_started"
    STEP_STARTED = "step_started"
    AGENT_DECISION = "agent_decision"
    ACTION_EXECUTION_STARTED = "action_execution_started"
    TRANSITION_COMPLETED = "transition_completed"
    TRANSITION_FAILED = "transition_failed"
    TRANSITION_NOT_EXECUTED = "transition_not_executed"


class EvidenceRole(StrEnum):
    CURRENT_UI_SCREENSHOT = "CURRENT_UI_SCREENSHOT"
    CURRENT_ACCESSIBILITY = "CURRENT_ACCESSIBILITY"
    PRIOR_ACTION_ATTEMPT = "PRIOR_ACTION_ATTEMPT"
    PRIOR_TRANSITION_STATUS = "PRIOR_TRANSITION_STATUS"
    PRIOR_POST_UI_STATE = "PRIOR_POST_UI_STATE"
    EXECUTOR_TRANSPORT_RESULT = "EXECUTOR_TRANSPORT_RESULT"
    AGENT_VISIBLE_TOOL_RESULT = "AGENT_VISIBLE_TOOL_RESULT"
    USER_RESPONSE = "USER_RESPONSE"


class EvidenceSemanticScope(StrEnum):
    CURRENT_STATE_ONLY = "CURRENT_STATE_ONLY"
    ACCESSIBILITY_STATE_ONLY = "ACCESSIBILITY_STATE_ONLY"
    PAST_EVENT_FACT = "PAST_EVENT_FACT"
    EXECUTION_TRANSPORT_ONLY = "EXECUTION_TRANSPORT_ONLY"
    TOOL_OR_USER_CONTENT = "TOOL_OR_USER_CONTENT"


class EvidenceProjectionType(StrEnum):
    TEXT = "TEXT"
    CANONICAL_JSON_TEXT = "CANONICAL_JSON_TEXT"
    IMAGE_REFERENCE = "IMAGE_REFERENCE"


class EvidenceMediaType(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"


class TemporalProvenanceStatus(StrEnum):
    BOUND = "BOUND"
    UNAVAILABLE = "UNAVAILABLE"


class RuntimeTargetSpanRole(StrEnum):
    EDITABLE_CLAIM = "EDITABLE_CLAIM"
    RECORD_EXTENT = "RECORD_EXTENT"


class RuntimeTargetDataRole(StrEnum):
    UNTRUSTED_HOST_HISTORY_DATA = "UNTRUSTED_HOST_HISTORY_DATA"


class EvidenceRelation(StrEnum):
    SUPPORTS = "SUPPORTS"
    REFUTES = "REFUTES"
    INVALIDATES = "INVALIDATES"


class FactualVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class TemporalValidity(StrEnum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "N_A"


class RuntimeOperationKind(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    REPLACE = "REPLACE"
    KEEP_UNCERTAIN = "KEEP_UNCERTAIN"


class RuntimeReasonCode(StrEnum):
    DIRECT_EVIDENCE_SUPPORT = "DIRECT_EVIDENCE_SUPPORT"
    DIRECT_EVIDENCE_REFUTATION = "DIRECT_EVIDENCE_REFUTATION"
    LATER_EVIDENCE_INVALIDATES = "LATER_EVIDENCE_INVALIDATES"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    TEMPORAL_PROVENANCE_MISSING = "TEMPORAL_PROVENANCE_MISSING"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    CURRENT_SCREEN_ABSENCE_ONLY = "CURRENT_SCREEN_ABSENCE_ONLY"
    EXECUTOR_STATUS_ONLY = "EXECUTOR_STATUS_ONLY"
    CLEAN_HISTORY = "CLEAN_HISTORY"


class RuntimeUncertaintyCode(StrEnum):
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    TEMPORAL_PROVENANCE_MISSING = "TEMPORAL_PROVENANCE_MISSING"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    COMPOUND_CLAIM = "COMPOUND_CLAIM"
    CURRENT_SCREEN_ABSENCE_ONLY = "CURRENT_SCREEN_ABSENCE_ONLY"
    EXECUTOR_STATUS_ONLY = "EXECUTOR_STATUS_ONLY"
    SEMANTIC_ENTAILMENT_UNCERTAIN = "SEMANTIC_ENTAILMENT_UNCERTAIN"


class RuntimeFallbackStatus(StrEnum):
    NONE = "NONE"
    ABSTAIN_TO_ORIGINAL = "ABSTAIN_TO_ORIGINAL"


class RuntimeProposalStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_ABSTAIN = "PARTIAL_ABSTAIN"
    ABSTAIN = "ABSTAIN"


class RuntimePolicyOrigin(StrEnum):
    AUTOMATIC_SENTINEL_POLICY = "AUTOMATIC_SENTINEL_POLICY"


class RuntimeExecutionScope(StrEnum):
    SHADOW_ONLY = "SHADOW_ONLY"


_EXPECTED_SCOPE: dict[EvidenceRole, EvidenceSemanticScope] = {
    EvidenceRole.CURRENT_UI_SCREENSHOT: EvidenceSemanticScope.CURRENT_STATE_ONLY,
    EvidenceRole.CURRENT_ACCESSIBILITY: EvidenceSemanticScope.ACCESSIBILITY_STATE_ONLY,
    EvidenceRole.PRIOR_ACTION_ATTEMPT: EvidenceSemanticScope.PAST_EVENT_FACT,
    EvidenceRole.PRIOR_TRANSITION_STATUS: EvidenceSemanticScope.PAST_EVENT_FACT,
    EvidenceRole.PRIOR_POST_UI_STATE: EvidenceSemanticScope.PAST_EVENT_FACT,
    EvidenceRole.EXECUTOR_TRANSPORT_RESULT: EvidenceSemanticScope.EXECUTION_TRANSPORT_ONLY,
    EvidenceRole.AGENT_VISIBLE_TOOL_RESULT: EvidenceSemanticScope.TOOL_OR_USER_CONTENT,
    EvidenceRole.USER_RESPONSE: EvidenceSemanticScope.TOOL_OR_USER_CONTENT,
}

_EXPECTED_EVENT_TYPES: dict[EvidenceRole, frozenset[SourceEventType]] = {
    EvidenceRole.CURRENT_UI_SCREENSHOT: frozenset({SourceEventType.STEP_STARTED}),
    EvidenceRole.CURRENT_ACCESSIBILITY: frozenset({SourceEventType.STEP_STARTED}),
    EvidenceRole.PRIOR_ACTION_ATTEMPT: frozenset({SourceEventType.ACTION_EXECUTION_STARTED}),
    EvidenceRole.PRIOR_TRANSITION_STATUS: frozenset(
        {
            SourceEventType.TRANSITION_COMPLETED,
            SourceEventType.TRANSITION_FAILED,
            SourceEventType.TRANSITION_NOT_EXECUTED,
        }
    ),
    EvidenceRole.PRIOR_POST_UI_STATE: frozenset(
        {SourceEventType.TRANSITION_COMPLETED, SourceEventType.TRANSITION_FAILED}
    ),
    EvidenceRole.EXECUTOR_TRANSPORT_RESULT: frozenset(
        {SourceEventType.TRANSITION_COMPLETED, SourceEventType.TRANSITION_FAILED}
    ),
    EvidenceRole.AGENT_VISIBLE_TOOL_RESULT: frozenset({SourceEventType.TRANSITION_COMPLETED}),
    EvidenceRole.USER_RESPONSE: frozenset({SourceEventType.TRANSITION_COMPLETED}),
}

_CURRENT_EVIDENCE_ROLES = frozenset(
    {EvidenceRole.CURRENT_UI_SCREENSHOT, EvidenceRole.CURRENT_ACCESSIBILITY}
)


def _require_exact(value: object, expected: type[object], field_name: str) -> None:
    if type(value) is not expected:
        raise R22ContractError(
            "UNTRUSTED_RUNTIME_TYPE", f"{field_name} must use exact {expected.__name__}"
        )


def _require_enum(value: object, expected: type[StrEnum], field_name: str) -> None:
    if type(value) is not expected:
        raise R22ContractError(
            "UNTRUSTED_RUNTIME_TYPE", f"{field_name} must use exact {expected.__name__}"
        )


def _require_runtime_id(value: object, field_name: str) -> None:
    if type(value) is not str or _RUNTIME_ID.fullmatch(cast(str, value)) is None:
        raise R22ContractError("INVALID_RUNTIME_ID", f"{field_name} is not a safe runtime ID")


def _require_semantic_id(value: object, field_name: str) -> None:
    if type(value) is not str or _SEMANTIC_ID.fullmatch(cast(str, value)) is None:
        raise R22ContractError("INVALID_SEMANTIC_ID", f"{field_name} is not a safe semantic ID")


def _require_contract_version(value: object, field_name: str) -> None:
    if type(value) is not str or _CONTRACT_VERSION.fullmatch(cast(str, value)) is None:
        raise R22ContractError(
            "INVALID_CONTRACT_VERSION", f"{field_name} is not a safe contract version"
        )


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(cast(str, value)) is None:
        raise R22ContractError("INVALID_SHA256", f"{field_name} must be lowercase SHA-256")


def _require_datetime(value: object, field_name: str) -> None:
    if type(value) is not str or not value:
        raise R22ContractError("INVALID_DATETIME", f"{field_name} must be a date-time string")
    candidate = cast(str, value)
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise R22ContractError("INVALID_DATETIME", f"{field_name} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        raise R22ContractError("INVALID_DATETIME", f"{field_name} needs a timezone")


def _require_tuple(
    value: object, item_type: type[object], field_name: str, *, allow_empty: bool = True
) -> None:
    if type(value) is not tuple:
        raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", f"{field_name} must be an exact tuple")
    items = cast(tuple[object, ...], value)
    if not allow_empty and not items:
        raise R22ContractError("EMPTY_REQUIRED_COLLECTION", f"{field_name} cannot be empty")
    if any(type(item) is not item_type for item in items):
        raise R22ContractError(
            "UNTRUSTED_RUNTIME_TYPE",
            f"{field_name} must contain exact {item_type.__name__} values",
        )


def _require_unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise R22ContractError("DUPLICATE_RUNTIME_ID", f"{field_name} contains duplicates")


def _require_path(value: object, field_name: str) -> None:
    if type(value) is not tuple or not value or len(value) > 64:
        raise R22ContractError("INVALID_JSON_PATH", f"{field_name} must be a bounded exact tuple")
    for token in cast(tuple[object, ...], value):
        if type(token) is int:
            if cast(int, token) < 0:
                raise R22ContractError("INVALID_JSON_PATH", f"{field_name} has a negative index")
        elif type(token) is str:
            if not token or len(cast(str, token)) > 256:
                raise R22ContractError("INVALID_JSON_PATH", f"{field_name} has an invalid key")
        else:
            raise R22ContractError("INVALID_JSON_PATH", f"{field_name} has a non-JSON token")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_exact_json(value: object, *, path: str = "$") -> JsonValue:
    """Reject serializer-coercible objects before copying or hashing."""

    if value is None:
        return None
    if type(value) is bool:
        return cast(bool, value)
    if type(value) is int:
        return cast(int, value)
    if type(value) is float:
        if not math.isfinite(cast(float, value)):
            raise R22ContractError("NON_CANONICAL_JSON", f"non-finite float at {path}")
        return cast(float, value)
    if type(value) is str:
        return cast(str, value)
    if type(value) is list:
        return [
            _validate_exact_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(cast(list[object], value))
        ]
    if type(value) is dict:
        copied: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise R22ContractError("NON_CANONICAL_JSON", f"non-string key at {path}")
            copied[cast(str, key)] = _validate_exact_json(item, path=f"{path}.{key}")
        return copied
    raise R22ContractError("NON_CANONICAL_JSON", f"non-JSON exact type at {path}")


def exact_canonical_json_text(value: object) -> str:
    checked = _validate_exact_json(value)
    return json.dumps(
        checked,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_sha256(value: JsonValue) -> str:
    return hashlib.sha256(exact_canonical_json_text(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidenceCutoffV1:
    run_id: str
    task_run_id: str
    step_id: str
    current_observation_event_id: str
    cutoff_event_seq: int
    actor_request_sha256: str
    kind: EvidenceCutoffKind = EvidenceCutoffKind.ACTOR_REQUEST_PRE_SEND

    def __post_init__(self) -> None:
        _require_enum(self.kind, EvidenceCutoffKind, "kind")
        for field_name in ("run_id", "task_run_id", "step_id", "current_observation_event_id"):
            _require_runtime_id(getattr(self, field_name), field_name)
        if type(self.cutoff_event_seq) is not int or self.cutoff_event_seq < 1:
            raise R22ContractError("INVALID_CUTOFF", "cutoff_event_seq must be a positive integer")
        _require_sha256(self.actor_request_sha256, "actor_request_sha256")


@dataclass(frozen=True, slots=True)
class TaskInstructionDataV1:
    source_event_id: str
    source_event_seq: int
    exact_text: str
    text_sha256: str
    role: TaskDataRole = TaskDataRole.TASK_INSTRUCTION_DATA
    source_event_type: SourceEventType = SourceEventType.TASK_STARTED

    def __post_init__(self) -> None:
        _require_enum(self.role, TaskDataRole, "task role")
        _require_enum(self.source_event_type, SourceEventType, "task source_event_type")
        if self.source_event_type is not SourceEventType.TASK_STARTED:
            raise R22ContractError("INVALID_TASK_EVENT", "task must bind task_started")
        _require_runtime_id(self.source_event_id, "task source_event_id")
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            raise R22ContractError("INVALID_TASK_EVENT", "task source_event_seq must be positive")
        if type(self.exact_text) is not str or not self.exact_text or len(self.exact_text) > 32768:
            raise R22ContractError("INVALID_TASK_TEXT", "task text must be bounded and non-empty")
        _require_sha256(self.text_sha256, "task text_sha256")
        if _sha256_text(self.exact_text) != self.text_sha256:
            raise R22ContractError("TASK_TEXT_HASH_MISMATCH", "task text hash drifted")

    @classmethod
    def create(
        cls, *, source_event_id: str, source_event_seq: int, exact_text: str
    ) -> TaskInstructionDataV1:
        return cls(
            source_event_id=source_event_id,
            source_event_seq=source_event_seq,
            exact_text=exact_text,
            text_sha256=_sha256_text(exact_text),
        )


@dataclass(frozen=True, slots=True)
class CurrentObservationV1:
    source_event_id: str
    source_event_seq: int
    screenshot_evidence_id: str
    screenshot_content_sha256: str
    actor_request_image_path: JsonPath
    actor_request_image_value_sha256: str
    media_type: EvidenceMediaType
    width: int
    height: int
    accessibility_evidence_ids: tuple[str, ...]
    source_event_type: SourceEventType = SourceEventType.STEP_STARTED

    def __post_init__(self) -> None:
        _require_runtime_id(self.source_event_id, "current observation source_event_id")
        _require_enum(self.source_event_type, SourceEventType, "current source_event_type")
        if self.source_event_type is not SourceEventType.STEP_STARTED:
            raise R22ContractError(
                "INVALID_CURRENT_EVENT", "current observation binds step_started"
            )
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            raise R22ContractError("INVALID_CURRENT_EVENT", "source_event_seq must be positive")
        _require_semantic_id(self.screenshot_evidence_id, "screenshot_evidence_id")
        _require_sha256(self.screenshot_content_sha256, "screenshot_content_sha256")
        _require_path(self.actor_request_image_path, "actor_request_image_path")
        _require_sha256(self.actor_request_image_value_sha256, "actor_request_image_value_sha256")
        _require_enum(self.media_type, EvidenceMediaType, "media_type")
        for value, field_name in ((self.width, "width"), (self.height, "height")):
            if type(value) is not int or not 1 <= value <= 32768:
                raise R22ContractError("INVALID_IMAGE_DIMENSION", f"{field_name} is out of bounds")
        if type(self.accessibility_evidence_ids) is not tuple or any(
            type(item) is not str for item in self.accessibility_evidence_ids
        ):
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "accessibility evidence IDs must be exact strings"
            )
        if len(self.accessibility_evidence_ids) > 64:
            raise R22ContractError(
                "EVIDENCE_COLLECTION_TOO_LARGE",
                "accessibility evidence IDs exceed the schema bound",
            )
        for item in self.accessibility_evidence_ids:
            _require_semantic_id(item, "accessibility_evidence_id")
        _require_unique(self.accessibility_evidence_ids, "accessibility evidence IDs")


@dataclass(frozen=True, slots=True)
class TemporalProvenanceV1:
    status: TemporalProvenanceStatus
    source_event_id: str | None
    source_event_seq: int | None
    source_wall_time: str | None
    source_monotonic_ns: int | None

    def __post_init__(self) -> None:
        _require_enum(self.status, TemporalProvenanceStatus, "temporal provenance status")
        values = (
            self.source_event_id,
            self.source_event_seq,
            self.source_wall_time,
            self.source_monotonic_ns,
        )
        if self.status is TemporalProvenanceStatus.UNAVAILABLE:
            if any(item is not None for item in values):
                raise R22ContractError(
                    "PARTIAL_TEMPORAL_PROVENANCE", "UNAVAILABLE provenance must be all-null"
                )
            return
        if any(item is None for item in values):
            raise R22ContractError(
                "PARTIAL_TEMPORAL_PROVENANCE", "BOUND provenance must be complete"
            )
        _require_runtime_id(self.source_event_id, "source_event_id")
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            raise R22ContractError("INVALID_EVENT_SEQUENCE", "source_event_seq must be positive")
        _require_datetime(self.source_wall_time, "source_wall_time")
        if type(self.source_monotonic_ns) is not int or self.source_monotonic_ns < 0:
            raise R22ContractError(
                "INVALID_MONOTONIC_TIME", "source_monotonic_ns must be non-negative"
            )

    @classmethod
    def unavailable(cls) -> TemporalProvenanceV1:
        return cls(
            status=TemporalProvenanceStatus.UNAVAILABLE,
            source_event_id=None,
            source_event_seq=None,
            source_wall_time=None,
            source_monotonic_ns=None,
        )


@dataclass(frozen=True, slots=True)
class EligibleHistoryTargetV1:
    target_id: str
    record_id: str
    claim_id: str | None
    source_request_sha256: str
    record_sha256: str
    container_path: JsonPath
    char_start: int
    char_end: int
    utf8_byte_start: int
    utf8_byte_end: int
    exact_text: str
    span_sha256: str
    span_role: RuntimeTargetSpanRole
    source_provenance: TemporalProvenanceV1
    data_role: RuntimeTargetDataRole = RuntimeTargetDataRole.UNTRUSTED_HOST_HISTORY_DATA

    def __post_init__(self) -> None:
        _require_semantic_id(self.target_id, "target_id")
        if (
            type(self.record_id) is not str
            or re.fullmatch(r"record-[0-9a-f]{32}", self.record_id) is None
        ):
            raise R22ContractError(
                "INVALID_RECORD_ID", "record_id must use the portable stable form"
            )
        _require_enum(self.span_role, RuntimeTargetSpanRole, "span_role")
        if self.span_role is RuntimeTargetSpanRole.EDITABLE_CLAIM:
            if (
                type(self.claim_id) is not str
                or re.fullmatch(r"claim-[0-9a-f]{32}", self.claim_id) is None
            ):
                raise R22ContractError(
                    "INVALID_CLAIM_ID", "editable targets require stable claim_id"
                )
        elif self.claim_id is not None:
            raise R22ContractError("INVALID_CLAIM_ID", "record extents cannot carry claim_id")
        _require_sha256(self.source_request_sha256, "source_request_sha256")
        _require_sha256(self.record_sha256, "record_sha256")
        _require_path(self.container_path, "container_path")
        for value, name in (
            (self.char_start, "char_start"),
            (self.char_end, "char_end"),
            (self.utf8_byte_start, "utf8_byte_start"),
            (self.utf8_byte_end, "utf8_byte_end"),
        ):
            if type(value) is not int or value < 0:
                raise R22ContractError("INVALID_TARGET_OFFSET", f"{name} must be non-negative")
        if self.char_end <= self.char_start or self.utf8_byte_end <= self.utf8_byte_start:
            raise R22ContractError("INVALID_TARGET_OFFSET", "target span must be non-empty")
        if type(self.exact_text) is not str or not self.exact_text or len(self.exact_text) > 32768:
            raise R22ContractError("INVALID_TARGET_TEXT", "target text must be bounded")
        _require_sha256(self.span_sha256, "span_sha256")
        if _sha256_text(self.exact_text) != self.span_sha256:
            raise R22ContractError("TARGET_SPAN_HASH_MISMATCH", "target text hash drifted")
        if len(self.exact_text.encode("utf-8")) != self.utf8_byte_end - self.utf8_byte_start:
            raise R22ContractError("TARGET_UTF8_OFFSET_MISMATCH", "target byte width drifted")
        _require_enum(self.data_role, RuntimeTargetDataRole, "data_role")
        _require_exact(self.source_provenance, TemporalProvenanceV1, "source_provenance")


@dataclass(frozen=True, slots=True)
class TextEvidenceProjectionV1:
    projection_type: EvidenceProjectionType
    exact_text: str
    text_sha256: str

    def __post_init__(self) -> None:
        _require_enum(self.projection_type, EvidenceProjectionType, "projection_type")
        if self.projection_type not in {
            EvidenceProjectionType.TEXT,
            EvidenceProjectionType.CANONICAL_JSON_TEXT,
        }:
            raise R22ContractError("INVALID_EVIDENCE_PROJECTION", "text projection type is invalid")
        if type(self.exact_text) is not str or not self.exact_text or len(self.exact_text) > 65536:
            raise R22ContractError("INVALID_EVIDENCE_TEXT", "evidence text must be bounded")
        _require_sha256(self.text_sha256, "text_sha256")
        if _sha256_text(self.exact_text) != self.text_sha256:
            raise R22ContractError("EVIDENCE_TEXT_HASH_MISMATCH", "evidence text hash drifted")
        if self.projection_type is EvidenceProjectionType.CANONICAL_JSON_TEXT:
            try:
                decoded = json.loads(self.exact_text)
            except (TypeError, ValueError) as exc:
                raise R22ContractError(
                    "NON_CANONICAL_JSON", "evidence text is invalid JSON"
                ) from exc
            if exact_canonical_json_text(decoded) != self.exact_text:
                raise R22ContractError("NON_CANONICAL_JSON", "evidence text is not canonical JSON")

    @classmethod
    def from_text(cls, value: str) -> TextEvidenceProjectionV1:
        return cls(
            projection_type=EvidenceProjectionType.TEXT,
            exact_text=value,
            text_sha256=_sha256_text(value),
        )

    @classmethod
    def from_json(cls, value: object) -> TextEvidenceProjectionV1:
        canonical = exact_canonical_json_text(value)
        return cls(
            projection_type=EvidenceProjectionType.CANONICAL_JSON_TEXT,
            exact_text=canonical,
            text_sha256=_sha256_text(canonical),
        )


@dataclass(frozen=True, slots=True)
class ImageEvidenceProjectionV1:
    content_sha256: str
    request_value_sha256: str
    media_type: EvidenceMediaType
    width: int
    height: int
    projection_type: EvidenceProjectionType = EvidenceProjectionType.IMAGE_REFERENCE

    def __post_init__(self) -> None:
        _require_enum(self.projection_type, EvidenceProjectionType, "projection_type")
        if self.projection_type is not EvidenceProjectionType.IMAGE_REFERENCE:
            raise R22ContractError(
                "INVALID_EVIDENCE_PROJECTION", "image projection type is invalid"
            )
        _require_sha256(self.content_sha256, "content_sha256")
        _require_sha256(self.request_value_sha256, "request_value_sha256")
        _require_enum(self.media_type, EvidenceMediaType, "media_type")
        for value, name in ((self.width, "width"), (self.height, "height")):
            if type(value) is not int or not 1 <= value <= 32768:
                raise R22ContractError("INVALID_IMAGE_DIMENSION", f"{name} is out of bounds")


EvidenceProjectionV1 = TextEvidenceProjectionV1 | ImageEvidenceProjectionV1


@dataclass(frozen=True, slots=True)
class EvidenceEntryV1:
    evidence_id: str
    role: EvidenceRole
    semantic_scope: EvidenceSemanticScope
    source_event_id: str
    source_event_type: SourceEventType
    source_event_seq: int
    task_run_id: str
    caused_by_event_id: str | None
    wall_time: str
    monotonic_ns: int
    payload_sha256: str
    projection: EvidenceProjectionV1
    observed_by_cutoff: bool = True

    def __post_init__(self) -> None:
        _require_semantic_id(self.evidence_id, "evidence_id")
        _require_enum(self.role, EvidenceRole, "role")
        _require_enum(self.semantic_scope, EvidenceSemanticScope, "semantic_scope")
        if self.semantic_scope is not _EXPECTED_SCOPE[self.role]:
            raise R22ContractError(
                "EVIDENCE_SCOPE_ROLE_MISMATCH", "semantic scope does not match evidence role"
            )
        _require_runtime_id(self.source_event_id, "source_event_id")
        _require_enum(self.source_event_type, SourceEventType, "source_event_type")
        if self.source_event_type not in _EXPECTED_EVENT_TYPES[self.role]:
            raise R22ContractError(
                "EVIDENCE_ROLE_EVENT_MISMATCH",
                "evidence role is not available from the declared Collector event",
            )
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            raise R22ContractError("INVALID_EVENT_SEQUENCE", "source_event_seq must be positive")
        _require_runtime_id(self.task_run_id, "task_run_id")
        if self.caused_by_event_id is not None:
            _require_runtime_id(self.caused_by_event_id, "caused_by_event_id")
        if self.role in _CURRENT_EVIDENCE_ROLES:
            if self.caused_by_event_id is not None:
                raise R22ContractError(
                    "CURRENT_EVIDENCE_CAUSAL_PARENT_FORBIDDEN",
                    "step-started current evidence has no prior execution parent",
                )
        elif self.caused_by_event_id is None:
            raise R22ContractError(
                "CAUSAL_PARENT_MISSING",
                "prior execution evidence must bind its causal parent event",
            )
        _require_datetime(self.wall_time, "wall_time")
        if type(self.monotonic_ns) is not int or self.monotonic_ns < 0:
            raise R22ContractError("INVALID_MONOTONIC_TIME", "monotonic_ns must be non-negative")
        _require_sha256(self.payload_sha256, "payload_sha256")
        if type(self.projection) not in {TextEvidenceProjectionV1, ImageEvidenceProjectionV1}:
            raise R22ContractError(
                "UNTRUSTED_RUNTIME_TYPE", "projection must use an exact R2.2 type"
            )
        if self.role is EvidenceRole.CURRENT_UI_SCREENSHOT:
            _require_exact(self.projection, ImageEvidenceProjectionV1, "screenshot projection")
        elif type(self.projection) is not TextEvidenceProjectionV1:
            raise R22ContractError("INVALID_EVIDENCE_PROJECTION", "non-image evidence must be text")
        if type(self.observed_by_cutoff) is not bool or not self.observed_by_cutoff:
            raise R22ContractError("FUTURE_EVIDENCE", "evidence must be observed by cutoff")


@dataclass(frozen=True, slots=True)
class ReplacementEvidenceRefV1:
    evidence_id: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.evidence_id, "evidence_id")
        _require_sha256(self.payload_sha256, "payload_sha256")


@dataclass(frozen=True, slots=True)
class ReplacementFactV1:
    replacement_fact_id: str
    target_id: str
    exact_text: str
    text_sha256: str
    evidence_refs: tuple[ReplacementEvidenceRefV1, ...]
    author: str = "SENTINEL"
    minimal_fact: bool = True
    retroactive_actor_speech: bool = False
    contains_action_or_tool_directive: bool = False

    def __post_init__(self) -> None:
        _require_semantic_id(self.replacement_fact_id, "replacement_fact_id")
        _require_semantic_id(self.target_id, "target_id")
        if (
            type(self.exact_text) is not str
            or not self.exact_text
            or len(self.exact_text) > 512
            or any(token in self.exact_text for token in ("\r", "\n", "<", ">"))
        ):
            raise R22ContractError(
                "INVALID_REPLACEMENT_FACT", "replacement text must be minimal single-line data"
            )
        _require_sha256(self.text_sha256, "text_sha256")
        if _sha256_text(self.exact_text) != self.text_sha256:
            raise R22ContractError(
                "REPLACEMENT_FACT_HASH_MISMATCH", "replacement text hash drifted"
            )
        _require_tuple(
            self.evidence_refs,
            ReplacementEvidenceRefV1,
            "replacement evidence refs",
            allow_empty=False,
        )
        if len(self.evidence_refs) > 32:
            raise R22ContractError(
                "EVIDENCE_COLLECTION_TOO_LARGE",
                "replacement evidence refs exceed the schema bound",
            )
        _require_unique(tuple(item.evidence_id for item in self.evidence_refs), "replacement refs")
        if type(self.author) is not str or self.author != "SENTINEL":
            raise R22ContractError(
                "INVALID_REPLACEMENT_AUTHOR", "replacement author must be Sentinel"
            )
        if type(self.minimal_fact) is not bool or not self.minimal_fact:
            raise R22ContractError("NON_MINIMAL_REPLACEMENT", "replacement must be a minimal fact")
        if type(self.retroactive_actor_speech) is not bool or self.retroactive_actor_speech:
            raise R22ContractError(
                "RETROACTIVE_ACTOR_SPEECH", "replacement cannot impersonate earlier actor speech"
            )
        if (
            type(self.contains_action_or_tool_directive) is not bool
            or self.contains_action_or_tool_directive
        ):
            raise R22ContractError(
                "ACTION_OR_TOOL_DIRECTIVE", "replacement cannot contain an action/tool directive"
            )
        if _COORDINATE_DIRECTIVE.search(self.exact_text) is not None:
            raise R22ContractError(
                "ACTION_OR_TOOL_DIRECTIVE",
                "replacement cannot carry action coordinates",
            )
        if _ACTION_OR_TOOL_DIRECTIVE.search(self.exact_text) is not None:
            raise R22ContractError(
                "ACTION_OR_TOOL_DIRECTIVE",
                "replacement cannot carry an action or tool directive",
            )
        if _RETROACTIVE_ACTOR_VOICE.search(self.exact_text) is not None:
            raise R22ContractError(
                "RETROACTIVE_ACTOR_SPEECH",
                "replacement cannot impersonate a prior actor",
            )

    @classmethod
    def create(
        cls,
        *,
        replacement_fact_id: str,
        target_id: str,
        exact_text: str,
        evidence_refs: tuple[ReplacementEvidenceRefV1, ...],
    ) -> ReplacementFactV1:
        return cls(
            replacement_fact_id=replacement_fact_id,
            target_id=target_id,
            exact_text=exact_text,
            text_sha256=_sha256_text(exact_text),
            evidence_refs=evidence_refs,
        )


@dataclass(frozen=True, slots=True)
class EvidenceInputExclusionsV1:
    future_event_included: bool = False
    target_actor_response_included: bool = False
    target_action_included: bool = False
    target_result_or_post_state_included: bool = False
    task_outcome_included: bool = False
    benchmark_checker_included: bool = False
    replay_result_included: bool = False
    peer_decision_included: bool = False
    host_history_used_as_evidence: bool = False
    collector_raw_mutated: bool = False

    def __post_init__(self) -> None:
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if type(value) is not bool or value:
                raise R22ContractError(
                    "PROHIBITED_INPUT_INCLUDED", f"{field_name} must be exact false"
                )


@dataclass(frozen=True, slots=True)
class EvidencePacketV1:
    packet_id: str
    logical_call_id: str
    host_id: str
    history_codec_id: str
    codec_contract_version: str
    raw_request_sha256: str
    cutoff: EvidenceCutoffV1
    task: TaskInstructionDataV1
    current_observation: CurrentObservationV1
    targets: tuple[EligibleHistoryTargetV1, ...]
    evidence_index: tuple[EvidenceEntryV1, ...]
    replacement_facts: tuple[ReplacementFactV1, ...]
    input_exclusions: EvidenceInputExclusionsV1
    schema_version: str = EVIDENCE_PACKET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != EVIDENCE_PACKET_SCHEMA_VERSION
        ):
            raise R22ContractError("UNKNOWN_SCHEMA_VERSION", "unknown evidence packet schema")
        _require_runtime_id(self.packet_id, "packet_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_runtime_id(self.host_id, "host_id")
        _require_semantic_id(self.history_codec_id, "history_codec_id")
        _require_contract_version(self.codec_contract_version, "codec_contract_version")
        _require_sha256(self.raw_request_sha256, "raw_request_sha256")
        _require_exact(self.cutoff, EvidenceCutoffV1, "cutoff")
        _require_exact(self.task, TaskInstructionDataV1, "task")
        _require_exact(self.current_observation, CurrentObservationV1, "current_observation")
        _require_tuple(self.targets, EligibleHistoryTargetV1, "targets")
        _require_tuple(self.evidence_index, EvidenceEntryV1, "evidence_index", allow_empty=False)
        _require_tuple(self.replacement_facts, ReplacementFactV1, "replacement_facts")
        if (
            len(self.targets) > 256
            or len(self.evidence_index) > 512
            or len(self.replacement_facts) > 256
        ):
            raise R22ContractError(
                "EVIDENCE_COLLECTION_TOO_LARGE",
                "packet collection exceeds its checked schema bound",
            )
        _require_exact(self.input_exclusions, EvidenceInputExclusionsV1, "input_exclusions")
        _require_unique(tuple(item.target_id for item in self.targets), "target IDs")
        _require_unique(tuple(item.evidence_id for item in self.evidence_index), "evidence IDs")
        _require_unique(
            tuple(item.replacement_fact_id for item in self.replacement_facts),
            "replacement fact IDs",
        )
        _validate_packet_bindings(self)


@dataclass(frozen=True, slots=True)
class ProposalEvidenceRefV1:
    evidence_id: str
    payload_sha256: str
    relation: EvidenceRelation

    def __post_init__(self) -> None:
        _require_semantic_id(self.evidence_id, "evidence_id")
        _require_sha256(self.payload_sha256, "payload_sha256")
        _require_enum(self.relation, EvidenceRelation, "relation")


@dataclass(frozen=True, slots=True)
class RuntimeClaimProposalV1:
    decision_id: str
    target_id: str
    factual_verdict: FactualVerdict
    temporal_validity: TemporalValidity
    proposed_operation: RuntimeOperationKind
    evidence_refs: tuple[ProposalEvidenceRefV1, ...]
    confidence_millis: int
    reason_code: RuntimeReasonCode
    uncertainty_codes: tuple[RuntimeUncertaintyCode, ...]
    rationale_summary: str
    replacement_fact_id: str | None
    fallback_status: RuntimeFallbackStatus

    def __post_init__(self) -> None:
        _require_semantic_id(self.decision_id, "decision_id")
        _require_semantic_id(self.target_id, "target_id")
        _require_enum(self.factual_verdict, FactualVerdict, "factual_verdict")
        _require_enum(self.temporal_validity, TemporalValidity, "temporal_validity")
        _require_enum(self.proposed_operation, RuntimeOperationKind, "proposed_operation")
        _require_tuple(self.evidence_refs, ProposalEvidenceRefV1, "evidence_refs")
        if len(self.evidence_refs) > 32:
            raise R22ContractError(
                "EVIDENCE_COLLECTION_TOO_LARGE",
                "proposal evidence refs exceed the schema bound",
            )
        _require_unique(tuple(item.evidence_id for item in self.evidence_refs), "evidence refs")
        if type(self.confidence_millis) is not int or not 0 <= self.confidence_millis <= 1000:
            raise R22ContractError("INVALID_CONFIDENCE", "confidence_millis must be in [0, 1000]")
        _require_enum(self.reason_code, RuntimeReasonCode, "reason_code")
        _require_tuple(self.uncertainty_codes, RuntimeUncertaintyCode, "uncertainty_codes")
        if len(self.uncertainty_codes) > 16:
            raise R22ContractError(
                "EVIDENCE_COLLECTION_TOO_LARGE",
                "uncertainty codes exceed the schema bound",
            )
        _require_unique(tuple(item.value for item in self.uncertainty_codes), "uncertainty codes")
        if (
            type(self.rationale_summary) is not str
            or not self.rationale_summary
            or len(self.rationale_summary) > _MAX_RATIONALE_CHARS
            or "\n" in self.rationale_summary
            or "\r" in self.rationale_summary
        ):
            raise R22ContractError(
                "INVALID_RATIONALE_SUMMARY", "rationale must be concise one-line data"
            )
        if self.replacement_fact_id is not None:
            _require_semantic_id(self.replacement_fact_id, "replacement_fact_id")
        _require_enum(self.fallback_status, RuntimeFallbackStatus, "fallback_status")
        _validate_claim_shape(self)

    @property
    def operation(self) -> RuntimeOperationKind:
        """Compatibility alias for seam-side decision census."""

        return self.proposed_operation


@dataclass(frozen=True, slots=True)
class RuntimePolicyProposalV1:
    packet_id: str
    evidence_packet_sha256: str
    status: RuntimeProposalStatus
    decisions: tuple[RuntimeClaimProposalV1, ...]
    automatic: bool = True
    curated: bool = False
    deployment_prediction: bool = True
    action_or_tool_authority: bool = False
    schema_version: str = POLICY_PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != POLICY_PROPOSAL_SCHEMA_VERSION
        ):
            raise R22ContractError("UNKNOWN_SCHEMA_VERSION", "unknown policy proposal schema")
        _require_runtime_id(self.packet_id, "packet_id")
        _require_sha256(self.evidence_packet_sha256, "evidence_packet_sha256")
        _require_enum(self.status, RuntimeProposalStatus, "status")
        _require_tuple(self.decisions, RuntimeClaimProposalV1, "decisions")
        if len(self.decisions) > 256:
            raise R22ContractError(
                "PROPOSAL_TOO_LARGE", "proposal decisions exceed the schema bound"
            )
        _require_unique(tuple(item.decision_id for item in self.decisions), "decision IDs")
        _require_unique(tuple(item.target_id for item in self.decisions), "proposal target IDs")
        for value, expected, name in (
            (self.automatic, True, "automatic"),
            (self.curated, False, "curated"),
            (self.deployment_prediction, True, "deployment_prediction"),
            (self.action_or_tool_authority, False, "action_or_tool_authority"),
        ):
            if type(value) is not bool or value is not expected:
                raise R22ContractError("INVALID_PROPOSAL_AUTHORITY", f"{name} has invalid value")
        uncertain_count = sum(
            item.proposed_operation is RuntimeOperationKind.KEEP_UNCERTAIN
            for item in self.decisions
        )
        if self.status is RuntimeProposalStatus.COMPLETE and uncertain_count:
            raise R22ContractError(
                "PROPOSAL_STATUS_MISMATCH", "COMPLETE cannot contain abstentions"
            )
        if self.status is RuntimeProposalStatus.PARTIAL_ABSTAIN and not (
            0 < uncertain_count < len(self.decisions)
        ):
            raise R22ContractError(
                "PROPOSAL_STATUS_MISMATCH", "PARTIAL_ABSTAIN needs mixed decisions"
            )
        if self.status is RuntimeProposalStatus.ABSTAIN and uncertain_count != len(self.decisions):
            raise R22ContractError("PROPOSAL_STATUS_MISMATCH", "ABSTAIN permits only abstentions")


@dataclass(frozen=True, slots=True)
class RuntimeAdmittedOperationV1:
    operation_id: str
    decision_id: str
    target_id: str
    target_record_id: str
    target_span_sha256: str
    kind: RuntimeOperationKind
    evidence_refs: tuple[ProposalEvidenceRefV1, ...]
    reason_code: RuntimeReasonCode
    replacement_fact_id: str | None = None
    replacement_text: str | None = None
    replacement_text_sha256: str | None = None
    replacement_author: str | None = None
    correction_anchor_sha256: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("operation_id", "decision_id", "target_id"):
            _require_semantic_id(getattr(self, field_name), field_name)
        _require_semantic_id(self.target_record_id, "target_record_id")
        _require_sha256(self.target_span_sha256, "target_span_sha256")
        _require_enum(self.kind, RuntimeOperationKind, "kind")
        if self.kind not in {RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE}:
            raise R22ContractError(
                "NON_MATERIAL_ADMITTED_OPERATION", "admitted plans contain only DROP/REPLACE"
            )
        _require_tuple(
            self.evidence_refs, ProposalEvidenceRefV1, "evidence_refs", allow_empty=False
        )
        _require_unique(tuple(item.evidence_id for item in self.evidence_refs), "evidence refs")
        _require_enum(self.reason_code, RuntimeReasonCode, "reason_code")
        if self.kind is RuntimeOperationKind.REPLACE:
            if self.replacement_fact_id is None or self.replacement_text is None:
                raise R22ContractError("REPLACEMENT_FACT_MISSING", "REPLACE needs a trusted fact")
            _require_semantic_id(self.replacement_fact_id, "replacement_fact_id")
            _require_sha256(self.replacement_text_sha256, "replacement_text_sha256")
            if _sha256_text(self.replacement_text) != self.replacement_text_sha256:
                raise R22ContractError("REPLACEMENT_FACT_HASH_MISMATCH", "replacement hash drifted")
            if type(self.replacement_author) is not str or self.replacement_author != "SENTINEL":
                raise R22ContractError("INVALID_REPLACEMENT_AUTHOR", "replacement must be Sentinel")
            _require_sha256(self.correction_anchor_sha256, "correction_anchor_sha256")
        elif any(
            item is not None
            for item in (
                self.replacement_fact_id,
                self.replacement_text,
                self.replacement_text_sha256,
                self.replacement_author,
                self.correction_anchor_sha256,
            )
        ):
            raise R22ContractError("UNEXPECTED_REPLACEMENT", "DROP cannot carry replacement data")


@dataclass(frozen=True, slots=True)
class RuntimeAdmittedPlanV1:
    plan_id: str
    logical_call_id: str
    host_id: str
    history_family: str
    history_codec_id: str
    history_codec_contract_version: str
    source_request_sha256: str
    evidence_packet_sha256: str
    policy_proposal_sha256: str
    operations: tuple[RuntimeAdmittedOperationV1, ...]
    origin: RuntimePolicyOrigin = RuntimePolicyOrigin.AUTOMATIC_SENTINEL_POLICY
    execution_scope: RuntimeExecutionScope = RuntimeExecutionScope.SHADOW_ONLY
    deployment_prediction: bool = True
    curated: bool = False
    schema_version: str = RUNTIME_ADMITTED_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != RUNTIME_ADMITTED_PLAN_SCHEMA_VERSION
        ):
            raise R22ContractError("UNKNOWN_SCHEMA_VERSION", "unknown runtime plan schema")
        _require_semantic_id(self.plan_id, "plan_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_runtime_id(self.host_id, "host_id")
        _require_semantic_id(self.history_family, "history_family")
        _require_semantic_id(self.history_codec_id, "history_codec_id")
        _require_contract_version(
            self.history_codec_contract_version, "history_codec_contract_version"
        )
        for field_name in (
            "source_request_sha256",
            "evidence_packet_sha256",
            "policy_proposal_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        _require_tuple(self.operations, RuntimeAdmittedOperationV1, "operations")
        _require_unique(tuple(item.operation_id for item in self.operations), "operation IDs")
        _require_unique(tuple(item.decision_id for item in self.operations), "operation decisions")
        _require_unique(tuple(item.target_id for item in self.operations), "operation targets")
        _require_enum(self.origin, RuntimePolicyOrigin, "origin")
        _require_enum(self.execution_scope, RuntimeExecutionScope, "execution_scope")
        if self.origin is not RuntimePolicyOrigin.AUTOMATIC_SENTINEL_POLICY:
            raise R22ContractError("INVALID_POLICY_ORIGIN", "runtime plan must be automatic")
        if self.execution_scope is not RuntimeExecutionScope.SHADOW_ONLY:
            raise R22ContractError("ACTIVE_SCOPE_FORBIDDEN", "R2.2 is SHADOW-only")
        if type(self.deployment_prediction) is not bool or not self.deployment_prediction:
            raise R22ContractError(
                "DEPLOYMENT_PREDICTION_REQUIRED", "runtime plan is a deployment prediction"
            )
        if type(self.curated) is not bool or self.curated:
            raise R22ContractError(
                "AUTOMATIC_PLAN_MISLABELED_CURATED", "R2.2 plans are never G1.2 curated"
            )


@dataclass(frozen=True, slots=True)
class RuntimeDecisionMetricV1:
    target_id: str
    factual_verdict: FactualVerdict
    temporal_validity: TemporalValidity
    operation: RuntimeOperationKind
    fallback_status: RuntimeFallbackStatus

    def __post_init__(self) -> None:
        _require_semantic_id(self.target_id, "target_id")
        _require_enum(self.factual_verdict, FactualVerdict, "factual_verdict")
        _require_enum(self.temporal_validity, TemporalValidity, "temporal_validity")
        _require_enum(self.operation, RuntimeOperationKind, "operation")
        _require_enum(self.fallback_status, RuntimeFallbackStatus, "fallback_status")


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionBundleV1:
    """Validated intermediate created before a separate receipt transaction commits."""

    proposal: RuntimePolicyProposalV1
    admitted_plan: RuntimeAdmittedPlanV1
    validation_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_exact(self.proposal, RuntimePolicyProposalV1, "proposal")
        _require_exact(self.admitted_plan, RuntimeAdmittedPlanV1, "admitted_plan")
        if type(self.validation_checks) is not tuple or not self.validation_checks:
            raise R22ContractError("VALIDATION_CHECKS_MISSING", "validation checks are required")
        if any(
            type(item) is not str or _SEMANTIC_ID.fullmatch(item) is None
            for item in self.validation_checks
        ):
            raise R22ContractError("INVALID_VALIDATION_CHECK", "validation checks are unsafe")
        _require_unique(self.validation_checks, "validation checks")
        _validate_proposal_plan_binding(self.proposal, self.admitted_plan)

    @property
    def admitted_plan_sha256(self) -> str:
        return runtime_admitted_plan_sha256(self.admitted_plan)

    @property
    def metric_decisions(self) -> tuple[RuntimeDecisionMetricV1, ...]:
        return tuple(
            RuntimeDecisionMetricV1(
                target_id=item.target_id,
                factual_verdict=item.factual_verdict,
                temporal_validity=item.temporal_validity,
                operation=item.proposed_operation,
                fallback_status=item.fallback_status,
            )
            for item in self.proposal.decisions
        )


@dataclass(frozen=True, slots=True)
class RuntimeSentinelPolicyOutputV1:
    proposal: RuntimePolicyProposalV1
    admitted_plan: RuntimeAdmittedPlanV1
    policy_receipt_sha256: str
    validation_checks: tuple[str, ...]
    schema_version: str = RUNTIME_POLICY_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not str
            or self.schema_version != RUNTIME_POLICY_OUTPUT_SCHEMA_VERSION
        ):
            raise R22ContractError("UNKNOWN_SCHEMA_VERSION", "unknown runtime output schema")
        RuntimeAdmissionBundleV1(
            proposal=self.proposal,
            admitted_plan=self.admitted_plan,
            validation_checks=self.validation_checks,
        )
        _require_sha256(self.policy_receipt_sha256, "policy_receipt_sha256")

    @property
    def decisions(self) -> tuple[RuntimeClaimProposalV1, ...]:
        return self.proposal.decisions


@runtime_checkable
class PolicyExecutionControlV1(Protocol):
    """Seam-owned linearization gates for one bounded policy worker."""

    def run_transport[T](self, call: Callable[[], T]) -> T: ...

    def publish_receipt(self, publish: Callable[[], None]) -> None: ...


@runtime_checkable
class RuntimeEvidencePolicyV1(Protocol):
    """Marker protocol that lets the seam reject non-SHADOW mode pre-evaluation."""

    @property
    def policy_id(self) -> str: ...

    @property
    def execution_scope(self) -> RuntimeExecutionScope: ...

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> RuntimeSentinelPolicyOutputV1: ...

    def evaluate_with_control(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
        execution_control: PolicyExecutionControlV1,
    ) -> RuntimeSentinelPolicyOutputV1: ...


def bind_policy_receipt(
    bundle: RuntimeAdmissionBundleV1, policy_receipt_sha256: str
) -> RuntimeSentinelPolicyOutputV1:
    """Bind an already committed receipt without creating a hash cycle."""

    _require_exact(bundle, RuntimeAdmissionBundleV1, "admission bundle")
    _require_sha256(policy_receipt_sha256, "policy_receipt_sha256")
    return RuntimeSentinelPolicyOutputV1(
        proposal=bundle.proposal,
        admitted_plan=bundle.admitted_plan,
        policy_receipt_sha256=policy_receipt_sha256,
        validation_checks=bundle.validation_checks,
    )


def _validate_packet_bindings(packet: EvidencePacketV1) -> None:
    if packet.raw_request_sha256 != packet.cutoff.actor_request_sha256:
        raise R22ContractError("CUTOFF_REQUEST_MISMATCH", "cutoff binds a different actor request")
    if packet.task.source_event_seq > packet.cutoff.cutoff_event_seq:
        raise R22ContractError("FUTURE_TASK_EVIDENCE", "task event occurs after cutoff")
    if packet.current_observation.source_event_id != packet.cutoff.current_observation_event_id:
        raise R22ContractError("CURRENT_EVENT_MISMATCH", "current observation event differs")
    if packet.current_observation.source_event_seq > packet.cutoff.cutoff_event_seq:
        raise R22ContractError("FUTURE_CURRENT_EVIDENCE", "current observation occurs after cutoff")
    entries = {item.evidence_id: item for item in packet.evidence_index}
    screenshot = entries.get(packet.current_observation.screenshot_evidence_id)
    if screenshot is None or screenshot.role is not EvidenceRole.CURRENT_UI_SCREENSHOT:
        raise R22ContractError("SCREENSHOT_EVIDENCE_MISSING", "current screenshot is not indexed")
    projection = cast(ImageEvidenceProjectionV1, screenshot.projection)
    current = packet.current_observation
    if (
        screenshot.source_event_id != current.source_event_id
        or screenshot.source_event_seq != current.source_event_seq
        or projection.content_sha256 != current.screenshot_content_sha256
        or projection.request_value_sha256 != current.actor_request_image_value_sha256
        or projection.media_type is not current.media_type
        or projection.width != current.width
        or projection.height != current.height
    ):
        raise R22ContractError("SCREENSHOT_BINDING_MISMATCH", "screenshot metadata differs")
    expected_accessibility = tuple(
        item.evidence_id
        for item in packet.evidence_index
        if item.role is EvidenceRole.CURRENT_ACCESSIBILITY
    )
    if expected_accessibility != current.accessibility_evidence_ids:
        raise R22ContractError("ACCESSIBILITY_BINDING_MISMATCH", "accessibility index differs")
    for item in packet.evidence_index:
        if item.task_run_id != packet.cutoff.task_run_id:
            raise R22ContractError("CROSS_TASK_EVIDENCE", "evidence belongs to another task run")
        if item.source_event_seq > packet.cutoff.cutoff_event_seq:
            raise R22ContractError("FUTURE_EVIDENCE", "evidence occurs after cutoff")
        if item.role in {EvidenceRole.CURRENT_UI_SCREENSHOT, EvidenceRole.CURRENT_ACCESSIBILITY}:
            if (
                item.source_event_id != current.source_event_id
                or item.source_event_seq != current.source_event_seq
            ):
                raise R22ContractError(
                    "CURRENT_EVIDENCE_MISMATCH", "current evidence event differs"
                )
        elif item.source_event_seq >= current.source_event_seq:
            raise R22ContractError(
                "NON_PRIOR_EVIDENCE", "completed prior evidence must precede current observation"
            )
    evidence = {item.evidence_id: item for item in packet.evidence_index}
    targets = {item.target_id for item in packet.targets}
    for fact in packet.replacement_facts:
        if fact.target_id not in targets:
            raise R22ContractError("UNKNOWN_REPLACEMENT_TARGET", "replacement target is absent")
        for ref in fact.evidence_refs:
            entry = evidence.get(ref.evidence_id)
            if entry is None or entry.payload_sha256 != ref.payload_sha256:
                raise R22ContractError(
                    "REPLACEMENT_EVIDENCE_BINDING_MISMATCH", "replacement evidence differs"
                )
        roles = {evidence[item.evidence_id].role for item in fact.evidence_refs}
        if roles <= {
            EvidenceRole.PRIOR_ACTION_ATTEMPT,
            EvidenceRole.PRIOR_TRANSITION_STATUS,
            EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
        }:
            raise R22ContractError(
                "WEAK_REPLACEMENT_EVIDENCE",
                "action/transition/executor status alone cannot establish a replacement",
            )


def _validate_claim_shape(value: RuntimeClaimProposalV1) -> None:
    relations = {item.relation for item in value.evidence_refs}
    uncertain = (
        value.factual_verdict is FactualVerdict.UNVERIFIABLE
        or value.temporal_validity is TemporalValidity.UNKNOWN
    )
    if uncertain:
        if (
            value.proposed_operation is not RuntimeOperationKind.KEEP_UNCERTAIN
            or not value.uncertainty_codes
            or value.fallback_status is not RuntimeFallbackStatus.ABSTAIN_TO_ORIGINAL
        ):
            raise R22ContractError("UNCERTAINTY_MAPPING_INVALID", "uncertain claims must abstain")
    elif value.proposed_operation is RuntimeOperationKind.KEEP_UNCERTAIN:
        raise R22ContractError("UNCERTAINTY_MAPPING_INVALID", "abstention needs uncertainty")
    if value.proposed_operation is RuntimeOperationKind.KEEP:
        if not (
            value.factual_verdict is FactualVerdict.SUPPORTED
            and value.temporal_validity
            in {TemporalValidity.ACTIVE, TemporalValidity.NOT_APPLICABLE}
            and not value.uncertainty_codes
            and value.fallback_status is RuntimeFallbackStatus.NONE
        ):
            raise R22ContractError("KEEP_MAPPING_INVALID", "KEEP needs supported active evidence")
        if (
            EvidenceRelation.SUPPORTS not in relations
            or value.reason_code is not RuntimeReasonCode.DIRECT_EVIDENCE_SUPPORT
        ):
            raise R22ContractError(
                "SUPPORTING_EVIDENCE_MISSING",
                "KEEP must cite supporting evidence and its matching reason code",
            )
    if value.proposed_operation in {RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE}:
        material_basis = value.factual_verdict is FactualVerdict.REFUTED or (
            value.factual_verdict is FactualVerdict.SUPPORTED
            and value.temporal_validity is TemporalValidity.INVALIDATED
        )
        if (
            not material_basis
            or not value.evidence_refs
            or value.uncertainty_codes
            or value.fallback_status is not RuntimeFallbackStatus.NONE
        ):
            raise R22ContractError(
                "MATERIAL_OPERATION_MAPPING_INVALID",
                "DROP/REPLACE need refutation or later invalidation without uncertainty",
            )
        if value.factual_verdict is FactualVerdict.REFUTED:
            if (
                EvidenceRelation.REFUTES not in relations
                or value.reason_code is not RuntimeReasonCode.DIRECT_EVIDENCE_REFUTATION
            ):
                raise R22ContractError(
                    "REFUTING_EVIDENCE_MISSING",
                    "refuted material operations need REFUTES evidence and reason",
                )
        else:
            if (
                EvidenceRelation.SUPPORTS not in relations
                or EvidenceRelation.INVALIDATES not in relations
                or value.reason_code is not RuntimeReasonCode.LATER_EVIDENCE_INVALIDATES
            ):
                raise R22ContractError(
                    "INVALIDATION_EVIDENCE_MISSING",
                    "invalidated claims need support plus later invalidation evidence",
                )
    if value.proposed_operation is RuntimeOperationKind.REPLACE:
        if value.replacement_fact_id is None:
            raise R22ContractError("REPLACEMENT_FACT_MISSING", "REPLACE needs a fact ID")
    elif value.replacement_fact_id is not None:
        raise R22ContractError("UNEXPECTED_REPLACEMENT", "only REPLACE may select a fact")


def _reject_replace_admission(kind: RuntimeOperationKind) -> None:
    if kind is RuntimeOperationKind.REPLACE:
        raise R22ContractError(
            "REPLACE_NOT_ADMITTED",
            "R2.2 v1 parses REPLACE proposals but does not admit or render them",
        )


def validate_runtime_policy_proposal(
    proposal: RuntimePolicyProposalV1, packet: EvidencePacketV1
) -> tuple[str, ...]:
    """Bind an untrusted proposal to exact packet targets and causal evidence."""

    _require_exact(proposal, RuntimePolicyProposalV1, "proposal")
    _require_exact(packet, EvidencePacketV1, "packet")
    if proposal.packet_id != packet.packet_id:
        raise R22ContractError("PACKET_ID_MISMATCH", "proposal binds a different packet")
    if proposal.evidence_packet_sha256 != evidence_packet_sha256(packet):
        raise R22ContractError("EVIDENCE_PACKET_HASH_MISMATCH", "proposal packet hash differs")
    targets = {item.target_id: item for item in packet.targets}
    if {item.target_id for item in proposal.decisions} != set(targets):
        raise R22ContractError(
            "TARGET_CENSUS_MISMATCH", "proposal needs exactly one decision per target"
        )
    if not targets and proposal.status is not RuntimeProposalStatus.ABSTAIN:
        raise R22ContractError("CLEAN_HISTORY_STATUS_MISMATCH", "zero targets must ABSTAIN")
    evidence = {item.evidence_id: item for item in packet.evidence_index}
    facts = {item.replacement_fact_id: item for item in packet.replacement_facts}
    for decision in proposal.decisions:
        for ref in decision.evidence_refs:
            entry = evidence.get(ref.evidence_id)
            if entry is None or entry.payload_sha256 != ref.payload_sha256:
                raise R22ContractError("UNKNOWN_EVIDENCE_REFERENCE", "evidence binding differs")
        _reject_replace_admission(decision.proposed_operation)
        material = decision.proposed_operation in {
            RuntimeOperationKind.DROP,
            RuntimeOperationKind.REPLACE,
        }
        if material:
            decisive = [
                ref
                for ref in decision.evidence_refs
                if ref.relation in {EvidenceRelation.REFUTES, EvidenceRelation.INVALIDATES}
            ]
            if not decisive:
                raise R22ContractError("DECISIVE_EVIDENCE_MISSING", "material edit lacks evidence")
            roles = {evidence[item.evidence_id].role for item in decisive}
            if roles <= {
                EvidenceRole.CURRENT_UI_SCREENSHOT,
                EvidenceRole.CURRENT_ACCESSIBILITY,
            }:
                raise R22ContractError(
                    "CURRENT_SCREEN_ABSENCE_ONLY", "current screen alone cannot refute a past event"
                )
            if roles <= {
                EvidenceRole.EXECUTOR_TRANSPORT_RESULT,
                EvidenceRole.PRIOR_ACTION_ATTEMPT,
                EvidenceRole.PRIOR_TRANSITION_STATUS,
            }:
                raise R22ContractError(
                    "EXECUTOR_STATUS_ONLY", "executor status alone cannot prove semantic success"
                )
        if decision.temporal_validity is TemporalValidity.INVALIDATED:
            invalidators = [
                evidence[item.evidence_id]
                for item in decision.evidence_refs
                if item.relation is EvidenceRelation.INVALIDATES
            ]
            supporters = [
                evidence[item.evidence_id]
                for item in decision.evidence_refs
                if item.relation is EvidenceRelation.SUPPORTS
            ]
            if not invalidators:
                raise R22ContractError(
                    "TEMPORAL_INVALIDATION_EVIDENCE_MISSING", "invalidation needs later evidence"
                )
            provenance = targets[decision.target_id].source_provenance
            if provenance.status is TemporalProvenanceStatus.UNAVAILABLE:
                raise R22ContractError(
                    "TEMPORAL_PROVENANCE_MISSING",
                    "automatic invalidation requires bound target provenance",
                )
            if any(
                item.source_event_seq <= cast(int, provenance.source_event_seq)
                for item in invalidators
            ):
                raise R22ContractError("NON_LATER_INVALIDATION", "invalidation is not later")
            if not supporters or min(item.source_event_seq for item in invalidators) <= max(
                item.source_event_seq for item in supporters
            ):
                raise R22ContractError(
                    "NON_LATER_INVALIDATION",
                    "invalidation must follow every cited supporting observation",
                )
        if decision.proposed_operation is RuntimeOperationKind.REPLACE:
            fact = facts.get(cast(str, decision.replacement_fact_id))
            if fact is None or fact.target_id != decision.target_id:
                raise R22ContractError("REPLACEMENT_FACT_BINDING_MISMATCH", "fact target differs")
            decision_refs = {
                (item.evidence_id, item.payload_sha256) for item in decision.evidence_refs
            }
            fact_refs = {(item.evidence_id, item.payload_sha256) for item in fact.evidence_refs}
            if not fact_refs.issubset(decision_refs):
                raise R22ContractError(
                    "REPLACEMENT_EVIDENCE_BINDING_MISMATCH", "fact evidence was not retained"
                )
    return (
        "r22.schema_exact",
        "r22.target_census_bound",
        "r22.evidence_refs_bound",
        "r22.temporal_cutoff_bound",
        "r22.replacement_fact_bound",
        "r22.no_action_surface",
    )


def _validate_proposal_plan_binding(
    proposal: RuntimePolicyProposalV1, plan: RuntimeAdmittedPlanV1
) -> None:
    if proposal.evidence_packet_sha256 != plan.evidence_packet_sha256:
        raise R22ContractError("EVIDENCE_PACKET_BINDING_MISMATCH", "proposal and plan differ")
    if runtime_policy_proposal_sha256(proposal) != plan.policy_proposal_sha256:
        raise R22ContractError("POLICY_PROPOSAL_HASH_MISMATCH", "plan does not bind proposal")
    material = {
        item.decision_id: item
        for item in proposal.decisions
        if item.proposed_operation in {RuntimeOperationKind.DROP, RuntimeOperationKind.REPLACE}
    }
    if set(material) != {item.decision_id for item in plan.operations}:
        raise R22ContractError(
            "ADMITTED_OPERATION_CENSUS_MISMATCH", "plan must bind every material proposal"
        )
    for operation in plan.operations:
        decision = material[operation.decision_id]
        if (
            operation.target_id != decision.target_id
            or operation.kind is not decision.proposed_operation
            or operation.evidence_refs != decision.evidence_refs
        ):
            raise R22ContractError(
                "ADMITTED_OPERATION_BINDING_MISMATCH", "operation differs from proposal"
            )


def cutoff_projection(value: EvidenceCutoffV1) -> dict[str, JsonValue]:
    _require_exact(value, EvidenceCutoffV1, "cutoff")
    return {
        "kind": value.kind.value,
        "run_id": value.run_id,
        "task_run_id": value.task_run_id,
        "step_id": value.step_id,
        "current_observation_event_id": value.current_observation_event_id,
        "cutoff_event_seq": value.cutoff_event_seq,
        "actor_request_sha256": value.actor_request_sha256,
    }


def task_projection(value: TaskInstructionDataV1) -> dict[str, JsonValue]:
    _require_exact(value, TaskInstructionDataV1, "task")
    return {
        "role": value.role.value,
        "source_event_id": value.source_event_id,
        "source_event_type": value.source_event_type.value,
        "source_event_seq": value.source_event_seq,
        "exact_text": value.exact_text,
        "text_sha256": value.text_sha256,
    }


def current_observation_projection(value: CurrentObservationV1) -> dict[str, JsonValue]:
    _require_exact(value, CurrentObservationV1, "current observation")
    return {
        "source_event_id": value.source_event_id,
        "source_event_type": value.source_event_type.value,
        "source_event_seq": value.source_event_seq,
        "screenshot_evidence_id": value.screenshot_evidence_id,
        "screenshot_content_sha256": value.screenshot_content_sha256,
        "actor_request_image_path": list(value.actor_request_image_path),
        "actor_request_image_value_sha256": value.actor_request_image_value_sha256,
        "media_type": value.media_type.value,
        "width": value.width,
        "height": value.height,
        "accessibility_evidence_ids": list(value.accessibility_evidence_ids),
    }


def temporal_provenance_projection(value: TemporalProvenanceV1) -> dict[str, JsonValue]:
    _require_exact(value, TemporalProvenanceV1, "temporal provenance")
    return {
        "status": value.status.value,
        "source_event_id": value.source_event_id,
        "source_event_seq": value.source_event_seq,
        "source_wall_time": value.source_wall_time,
        "source_monotonic_ns": value.source_monotonic_ns,
    }


def eligible_target_projection(value: EligibleHistoryTargetV1) -> dict[str, JsonValue]:
    _require_exact(value, EligibleHistoryTargetV1, "eligible target")
    return {
        "target_id": value.target_id,
        "record_id": value.record_id,
        "claim_id": value.claim_id,
        "source_request_sha256": value.source_request_sha256,
        "record_sha256": value.record_sha256,
        "container_path": list(value.container_path),
        "char_start": value.char_start,
        "char_end": value.char_end,
        "utf8_byte_start": value.utf8_byte_start,
        "utf8_byte_end": value.utf8_byte_end,
        "exact_text": value.exact_text,
        "span_sha256": value.span_sha256,
        "span_role": value.span_role.value,
        "data_role": value.data_role.value,
        "source_provenance": temporal_provenance_projection(value.source_provenance),
    }


def evidence_projection_projection(value: EvidenceProjectionV1) -> dict[str, JsonValue]:
    if type(value) is TextEvidenceProjectionV1:
        text_item = value
        return {
            "projection_type": text_item.projection_type.value,
            "exact_text": text_item.exact_text,
            "text_sha256": text_item.text_sha256,
        }
    if type(value) is ImageEvidenceProjectionV1:
        image_item = value
        return {
            "projection_type": image_item.projection_type.value,
            "content_sha256": image_item.content_sha256,
            "request_value_sha256": image_item.request_value_sha256,
            "media_type": image_item.media_type.value,
            "width": image_item.width,
            "height": image_item.height,
        }
    raise R22ContractError("UNTRUSTED_RUNTIME_TYPE", "unknown evidence projection type")


def evidence_entry_projection(value: EvidenceEntryV1) -> dict[str, JsonValue]:
    _require_exact(value, EvidenceEntryV1, "evidence entry")
    return {
        "evidence_id": value.evidence_id,
        "role": value.role.value,
        "semantic_scope": value.semantic_scope.value,
        "source_event_id": value.source_event_id,
        "source_event_type": value.source_event_type.value,
        "source_event_seq": value.source_event_seq,
        "task_run_id": value.task_run_id,
        "caused_by_event_id": value.caused_by_event_id,
        "wall_time": value.wall_time,
        "monotonic_ns": value.monotonic_ns,
        "payload_sha256": value.payload_sha256,
        "observed_by_cutoff": value.observed_by_cutoff,
        "projection": evidence_projection_projection(value.projection),
    }


def replacement_evidence_ref_projection(value: ReplacementEvidenceRefV1) -> dict[str, JsonValue]:
    _require_exact(value, ReplacementEvidenceRefV1, "replacement evidence ref")
    return {"evidence_id": value.evidence_id, "payload_sha256": value.payload_sha256}


def replacement_fact_projection(value: ReplacementFactV1) -> dict[str, JsonValue]:
    _require_exact(value, ReplacementFactV1, "replacement fact")
    return {
        "replacement_fact_id": value.replacement_fact_id,
        "target_id": value.target_id,
        "exact_text": value.exact_text,
        "text_sha256": value.text_sha256,
        "author": value.author,
        "evidence_refs": [
            replacement_evidence_ref_projection(item) for item in value.evidence_refs
        ],
        "minimal_fact": value.minimal_fact,
        "retroactive_actor_speech": value.retroactive_actor_speech,
        "contains_action_or_tool_directive": value.contains_action_or_tool_directive,
    }


def input_exclusions_projection(value: EvidenceInputExclusionsV1) -> dict[str, JsonValue]:
    _require_exact(value, EvidenceInputExclusionsV1, "input exclusions")
    return {field_name: getattr(value, field_name) for field_name in value.__dataclass_fields__}


def evidence_packet_projection(value: EvidencePacketV1) -> dict[str, JsonValue]:
    _require_exact(value, EvidencePacketV1, "evidence packet")
    return {
        "schema_version": value.schema_version,
        "packet_id": value.packet_id,
        "logical_call_id": value.logical_call_id,
        "host_id": value.host_id,
        "history_codec_id": value.history_codec_id,
        "codec_contract_version": value.codec_contract_version,
        "raw_request_sha256": value.raw_request_sha256,
        "cutoff": cutoff_projection(value.cutoff),
        "task": task_projection(value.task),
        "current_observation": current_observation_projection(value.current_observation),
        "targets": [eligible_target_projection(item) for item in value.targets],
        "evidence_index": [evidence_entry_projection(item) for item in value.evidence_index],
        "replacement_facts": [
            replacement_fact_projection(item) for item in value.replacement_facts
        ],
        "input_exclusions": input_exclusions_projection(value.input_exclusions),
    }


def proposal_evidence_ref_projection(value: ProposalEvidenceRefV1) -> dict[str, JsonValue]:
    _require_exact(value, ProposalEvidenceRefV1, "proposal evidence ref")
    return {
        "evidence_id": value.evidence_id,
        "payload_sha256": value.payload_sha256,
        "relation": value.relation.value,
    }


def runtime_claim_proposal_projection(value: RuntimeClaimProposalV1) -> dict[str, JsonValue]:
    _require_exact(value, RuntimeClaimProposalV1, "runtime claim proposal")
    return {
        "decision_id": value.decision_id,
        "target_id": value.target_id,
        "factual_verdict": value.factual_verdict.value,
        "temporal_validity": value.temporal_validity.value,
        "proposed_operation": value.proposed_operation.value,
        "evidence_refs": [proposal_evidence_ref_projection(item) for item in value.evidence_refs],
        "confidence_millis": value.confidence_millis,
        "reason_code": value.reason_code.value,
        "uncertainty_codes": [item.value for item in value.uncertainty_codes],
        "rationale_summary": value.rationale_summary,
        "replacement_fact_id": value.replacement_fact_id,
        "fallback_status": value.fallback_status.value,
    }


def runtime_policy_proposal_projection(value: RuntimePolicyProposalV1) -> dict[str, JsonValue]:
    _require_exact(value, RuntimePolicyProposalV1, "runtime policy proposal")
    return {
        "schema_version": value.schema_version,
        "packet_id": value.packet_id,
        "evidence_packet_sha256": value.evidence_packet_sha256,
        "status": value.status.value,
        "automatic": value.automatic,
        "curated": value.curated,
        "deployment_prediction": value.deployment_prediction,
        "action_or_tool_authority": value.action_or_tool_authority,
        "decisions": [runtime_claim_proposal_projection(item) for item in value.decisions],
    }


def runtime_admitted_operation_projection(
    value: RuntimeAdmittedOperationV1,
) -> dict[str, JsonValue]:
    _require_exact(value, RuntimeAdmittedOperationV1, "runtime admitted operation")
    return {
        "operation_id": value.operation_id,
        "decision_id": value.decision_id,
        "target_id": value.target_id,
        "target_record_id": value.target_record_id,
        "target_span_sha256": value.target_span_sha256,
        "kind": value.kind.value,
        "evidence_refs": [proposal_evidence_ref_projection(item) for item in value.evidence_refs],
        "reason_code": value.reason_code.value,
        "replacement_fact_id": value.replacement_fact_id,
        "replacement_text": value.replacement_text,
        "replacement_text_sha256": value.replacement_text_sha256,
        "replacement_author": value.replacement_author,
        "correction_anchor_sha256": value.correction_anchor_sha256,
    }


def runtime_admitted_plan_projection(value: RuntimeAdmittedPlanV1) -> dict[str, JsonValue]:
    _require_exact(value, RuntimeAdmittedPlanV1, "runtime admitted plan")
    return {
        "schema_version": value.schema_version,
        "plan_id": value.plan_id,
        "logical_call_id": value.logical_call_id,
        "host_id": value.host_id,
        "history_family": value.history_family,
        "history_codec_id": value.history_codec_id,
        "history_codec_contract_version": value.history_codec_contract_version,
        "source_request_sha256": value.source_request_sha256,
        "evidence_packet_sha256": value.evidence_packet_sha256,
        "policy_proposal_sha256": value.policy_proposal_sha256,
        "origin": value.origin.value,
        "execution_scope": value.execution_scope.value,
        "deployment_prediction": value.deployment_prediction,
        "curated": value.curated,
        "operations": [runtime_admitted_operation_projection(item) for item in value.operations],
    }


def runtime_policy_output_projection(value: RuntimeSentinelPolicyOutputV1) -> dict[str, JsonValue]:
    _require_exact(value, RuntimeSentinelPolicyOutputV1, "runtime policy output")
    return {
        "schema_version": value.schema_version,
        "proposal": runtime_policy_proposal_projection(value.proposal),
        "admitted_plan": runtime_admitted_plan_projection(value.admitted_plan),
        "policy_receipt_sha256": value.policy_receipt_sha256,
        "validation_checks": list(value.validation_checks),
    }


def evidence_packet_sha256(value: EvidencePacketV1) -> str:
    return _canonical_sha256(cast(JsonValue, evidence_packet_projection(value)))


def runtime_policy_proposal_sha256(value: RuntimePolicyProposalV1) -> str:
    return _canonical_sha256(cast(JsonValue, runtime_policy_proposal_projection(value)))


def runtime_admitted_plan_sha256(value: RuntimeAdmittedPlanV1) -> str:
    return _canonical_sha256(cast(JsonValue, runtime_admitted_plan_projection(value)))


def runtime_policy_output_sha256(value: RuntimeSentinelPolicyOutputV1) -> str:
    return _canonical_sha256(cast(JsonValue, runtime_policy_output_projection(value)))


__all__ = [
    "EVIDENCE_PACKET_SCHEMA_VERSION",
    "POLICY_PROPOSAL_SCHEMA_VERSION",
    "PolicyExecutionControlV1",
    "RUNTIME_ADMITTED_PLAN_SCHEMA_VERSION",
    "RUNTIME_POLICY_OUTPUT_SCHEMA_VERSION",
    "CurrentObservationV1",
    "EligibleHistoryTargetV1",
    "EvidenceCutoffKind",
    "EvidenceCutoffV1",
    "EvidenceEntryV1",
    "EvidenceInputExclusionsV1",
    "EvidenceMediaType",
    "EvidencePacketV1",
    "EvidenceProjectionType",
    "EvidenceProjectionV1",
    "EvidenceRelation",
    "EvidenceRole",
    "EvidenceSemanticScope",
    "FactualVerdict",
    "ImageEvidenceProjectionV1",
    "ProposalEvidenceRefV1",
    "R22ContractError",
    "ReplacementEvidenceRefV1",
    "ReplacementFactV1",
    "RuntimeAdmissionBundleV1",
    "RuntimeAdmittedOperationV1",
    "RuntimeAdmittedPlanV1",
    "RuntimeClaimProposalV1",
    "RuntimeDecisionMetricV1",
    "RuntimeExecutionScope",
    "RuntimeFallbackStatus",
    "RuntimeOperationKind",
    "RuntimePolicyOrigin",
    "RuntimePolicyProposalV1",
    "RuntimeProposalStatus",
    "RuntimeEvidencePolicyV1",
    "RuntimeReasonCode",
    "RuntimeSentinelPolicyOutputV1",
    "RuntimeTargetDataRole",
    "RuntimeTargetSpanRole",
    "RuntimeUncertaintyCode",
    "SourceEventType",
    "TaskDataRole",
    "TaskInstructionDataV1",
    "TemporalProvenanceStatus",
    "TemporalProvenanceV1",
    "TemporalValidity",
    "TextEvidenceProjectionV1",
    "bind_policy_receipt",
    "current_observation_projection",
    "cutoff_projection",
    "eligible_target_projection",
    "evidence_entry_projection",
    "evidence_packet_projection",
    "evidence_packet_sha256",
    "evidence_projection_projection",
    "exact_canonical_json_text",
    "input_exclusions_projection",
    "proposal_evidence_ref_projection",
    "replacement_evidence_ref_projection",
    "replacement_fact_projection",
    "runtime_admitted_operation_projection",
    "runtime_admitted_plan_projection",
    "runtime_admitted_plan_sha256",
    "runtime_claim_proposal_projection",
    "runtime_policy_output_projection",
    "runtime_policy_output_sha256",
    "runtime_policy_proposal_projection",
    "runtime_policy_proposal_sha256",
    "task_projection",
    "temporal_provenance_projection",
    "validate_runtime_policy_proposal",
]
