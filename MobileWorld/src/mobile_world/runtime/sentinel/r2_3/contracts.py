"""Closed value contracts for the R2.3 multi-path rubric axis.

R2.3 is independent from history factual validity.  Its grounding packet has
no History IR or actor-history field, and its outputs have no authority to
edit history, verify factual claims, select actions, or execute ``ARCHIVE``.
The initial implementation boundary is injected-fake, CPU/offline and
SHADOW-only.

Canonical projections are module-owned and require exact trusted dataclass
types.  Replaceable backends never supply serializers used for hashing or
admission.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn, Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import JsonValue

RUBRIC_SCHEMA_VERSION = "mobileworld.runtime.multi-path-rubric/v1"
TRACKING_PACKET_SCHEMA_VERSION = "mobileworld.runtime.rubric-tracking-packet/v1"
TRACKER_OUTPUT_SCHEMA_VERSION = "mobileworld.runtime.rubric-tracker-output/v1"
RUBRIC_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.rubric-receipt/v1"
TOPOLOGY_COMPARISON_SCHEMA_VERSION = "mobileworld.runtime.rubric-topology-comparison/v1"
R23_CONTRACT_VERSION = "v1"

# Trusted R2.3 graphs are shallow by contract even when a rubric contains the
# maximum 512 gates. These limits protect the pre-validation snapshot/hash
# boundary from post-construction cycles and structurally poisoned graphs while
# leaving ample room for the largest valid v1 rubric and tracking packet.
TRUSTED_GRAPH_MAX_DEPTH = 64
TRUSTED_GRAPH_MAX_NODES = 262_144

# These constants are part of the public safety boundary, not feature flags.
RUBRIC_FACTUAL_AUTHORITY = False
RUBRIC_HISTORY_EDIT_AUTHORITY = False
RUBRIC_ACTION_OR_TOOL_AUTHORITY = False
RUBRIC_ARCHIVE_EXECUTION_AUTHORITY = False

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CONTRACT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


class R23ContractError(ValueError):
    """Deterministic contract rejection with a stable safe code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class InstructionSpanRole(StrEnum):
    HARD_REQUIREMENT = "HARD_REQUIREMENT"
    CONSTRAINT = "CONSTRAINT"
    TERMINAL_REQUIREMENT = "TERMINAL_REQUIREMENT"


class MilestoneKind(StrEnum):
    HARD_REQUIREMENT = "HARD_REQUIREMENT"
    DERIVED_CHECKPOINT = "DERIVED_CHECKPOINT"
    OPTIONAL_CHECKPOINT = "OPTIONAL_CHECKPOINT"
    CONSTRAINT = "CONSTRAINT"
    TERMINAL_REQUIREMENT = "TERMINAL_REQUIREMENT"


class MilestonePredicateKind(StrEnum):
    INSTRUCTION_REQUIREMENT = "INSTRUCTION_REQUIREMENT"
    GUI_STATE = "GUI_STATE"
    COMPLETED_TRANSITION = "COMPLETED_TRANSITION"
    USER_VISIBLE_RESULT = "USER_VISIBLE_RESULT"
    DERIVED_STATE = "DERIVED_STATE"
    UNKNOWN = "UNKNOWN"


class GateOperator(StrEnum):
    AND = "AND"
    OR = "OR"


class GraphRefKind(StrEnum):
    MILESTONE = "MILESTONE"
    GATE = "GATE"


class PathKind(StrEnum):
    LEGAL_ALTERNATIVE = "LEGAL_ALTERNATIVE"
    OTHER_UNKNOWN = "OTHER_UNKNOWN"


class RevisionKind(StrEnum):
    INITIAL = "INITIAL"
    EXPLICIT_REVISION = "EXPLICIT_REVISION"


class RevisionReason(StrEnum):
    TASK_START = "TASK_START"
    TASK_INSTRUCTION_CHANGED = "TASK_INSTRUCTION_CHANGED"
    GRAPH_DEFECT_CORRECTION = "GRAPH_DEFECT_CORRECTION"
    AUTHORIZED_CONFIG_CHANGE = "AUTHORIZED_CONFIG_CHANGE"


class RequirementDeltaKind(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"


class TopologyKind(StrEnum):
    ISOLATED_HISTORY_FREE = "ISOLATED_HISTORY_FREE"
    JOINT_NON_INDEPENDENT = "JOINT_NON_INDEPENDENT"


class RubricExecutionScope(StrEnum):
    SHADOW_ONLY = "SHADOW_ONLY"


class RubricBackendKind(StrEnum):
    INJECTED_FAKE = "INJECTED_FAKE"


class RubricTransportAuthority(StrEnum):
    CPU_OFFLINE_FAKE = "CPU_OFFLINE_FAKE"


class RubricSourceEventType(StrEnum):
    TASK_STARTED = "task_started"
    STEP_STARTED = "step_started"
    TRANSITION_COMPLETED = "transition_completed"
    TRANSITION_FAILED = "transition_failed"
    TRANSITION_NOT_EXECUTED = "transition_not_executed"


class RubricEvidenceRole(StrEnum):
    CURRENT_UI_SCREENSHOT = "CURRENT_UI_SCREENSHOT"
    CURRENT_ACCESSIBILITY = "CURRENT_ACCESSIBILITY"
    COMPLETED_TRANSITION_STATUS = "COMPLETED_TRANSITION_STATUS"
    COMPLETED_POST_UI_STATE = "COMPLETED_POST_UI_STATE"
    AGENT_VISIBLE_TOOL_RESULT = "AGENT_VISIBLE_TOOL_RESULT"
    USER_RESPONSE = "USER_RESPONSE"


class EvidenceProjectionKind(StrEnum):
    TEXT = "TEXT"
    CANONICAL_JSON_TEXT = "CANONICAL_JSON_TEXT"
    IMAGE_REFERENCE = "IMAGE_REFERENCE"


class EvidenceMediaType(StrEnum):
    PNG = "image/png"
    JPEG = "image/jpeg"
    WEBP = "image/webp"


class MilestoneState(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


class MilestoneEvidenceRelation(StrEnum):
    SUPPORTS_STATE = "SUPPORTS_STATE"
    REFUTES_STATE = "REFUTES_STATE"
    OBSERVES_PROGRESS = "OBSERVES_PROGRESS"


class MilestoneReasonCode(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    CURRENT_GUI_SUPPORT = "CURRENT_GUI_SUPPORT"
    COMPLETED_TRANSITION_SUPPORT = "COMPLETED_TRANSITION_SUPPORT"
    CURRENT_GUI_REFUTATION = "CURRENT_GUI_REFUTATION"
    COMPLETED_TRANSITION_REFUTATION = "COMPLETED_TRANSITION_REFUTATION"
    PROGRESS_OBSERVED = "PROGRESS_OBSERVED"
    PRESERVE_PRIOR_STATE = "PRESERVE_PRIOR_STATE"
    AMBIGUOUS_GUI = "AMBIGUOUS_GUI"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


class TrackerProposalStatus(StrEnum):
    COMPLETE = "COMPLETE"
    ABSTAIN = "ABSTAIN"


class TrackerOutputKind(StrEnum):
    MILESTONE_PROPOSAL = "MILESTONE_PROPOSAL"
    TRACKING_STATE = "TRACKING_STATE"
    PATH_RELEVANCE = "PATH_RELEVANCE"


class PathViability(StrEnum):
    VIABLE = "viable"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class RecordRelevance(StrEnum):
    ACTIVE_PATH = "active_path"
    INACTIVE_BRANCH = "inactive_branch"
    PATH_INDEPENDENT = "path_independent"
    UNKNOWN = "unknown"


class RelevanceDisposition(StrEnum):
    RETAIN = "RETAIN"
    ARCHIVE_SHADOW = "ARCHIVE_SHADOW"


class ExternalValidityVerdict(StrEnum):
    SUPPORTED = "SUPPORTED"


class ExternalValidityOperation(StrEnum):
    KEEP = "KEEP"


class TopologyRunStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    ADMITTED = "ADMITTED"
    FALLBACK = "FALLBACK"


class TopologyDeploymentDecision(StrEnum):
    UNDECIDED_R2_4 = "UNDECIDED_R2_4"


def _fail(code: str, message: str) -> NoReturn:
    raise R23ContractError(code, message)


def _require_exact(value: object, expected: type[object], name: str) -> None:
    if type(value) is not expected:
        _fail("UNTRUSTED_TYPE", f"{name} must use exact {expected.__name__}")


def _require_enum(value: object, expected: type[StrEnum], name: str) -> None:
    if type(value) is not expected:
        _fail("INVALID_ENUM", f"{name} must use exact {expected.__name__}")


def _require_runtime_id(value: object, name: str) -> None:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        _fail("INVALID_RUNTIME_ID", f"{name} must be a bounded runtime ID")


def _require_semantic_id(value: object, name: str) -> None:
    if type(value) is not str or _SEMANTIC_ID.fullmatch(value) is None:
        _fail("INVALID_SEMANTIC_ID", f"{name} must be a bounded semantic ID")


def _require_contract_version(value: object, name: str) -> None:
    if type(value) is not str or _CONTRACT_VERSION.fullmatch(value) is None:
        _fail("INVALID_CONTRACT_VERSION", f"{name} must be a bounded version")


def _require_sha256(value: object, name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("INVALID_SHA256", f"{name} must be lowercase SHA-256")


def _require_safe_code(value: object, name: str) -> None:
    if type(value) is not str or _SAFE_CODE.fullmatch(value) is None:
        _fail("INVALID_SAFE_CODE", f"{name} must use the safe code grammar")


def _require_string(value: object, name: str, *, maximum: int, minimum: int = 1) -> None:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        _fail("INVALID_STRING", f"{name} length is outside [{minimum}, {maximum}]")


def _require_nonnegative_int(value: object, name: str) -> None:
    if type(value) is not int or value < 0:
        _fail("INVALID_COUNT", f"{name} must be a non-negative integer")


def _require_positive_int(value: object, name: str) -> None:
    if type(value) is not int or value <= 0:
        _fail("INVALID_COUNT", f"{name} must be a positive integer")


def _require_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        _fail("INVALID_BOOLEAN", f"{name} must use exact bool")


def _require_tuple(
    value: object,
    item_type: type[object],
    name: str,
    *,
    maximum: int,
    minimum: int = 0,
) -> None:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        _fail("INVALID_COLLECTION", f"{name} must be a bounded exact tuple")
    if any(type(item) is not item_type for item in cast(tuple[object, ...], value)):
        _fail("UNTRUSTED_TYPE", f"{name} contains an untrusted item type")


def _require_unique(values: tuple[str, ...], name: str) -> None:
    if len(values) != len(set(values)):
        _fail("DUPLICATE_ID", f"{name} must be unique")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: JsonValue) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RubricAuthorityV1:
    factual_truth_authority: bool = RUBRIC_FACTUAL_AUTHORITY
    history_edit_authority: bool = RUBRIC_HISTORY_EDIT_AUTHORITY
    action_or_tool_authority: bool = RUBRIC_ACTION_OR_TOOL_AUTHORITY
    archive_execution_authority: bool = RUBRIC_ARCHIVE_EXECUTION_AUTHORITY

    def __post_init__(self) -> None:
        for name, value in (
            ("factual_truth_authority", self.factual_truth_authority),
            ("history_edit_authority", self.history_edit_authority),
            ("action_or_tool_authority", self.action_or_tool_authority),
            ("archive_execution_authority", self.archive_execution_authority),
        ):
            _require_bool(value, name)
            if value:
                _fail("FORBIDDEN_AUTHORITY", f"R2.3 cannot grant {name}")


@dataclass(frozen=True, slots=True)
class TaskInstructionV1:
    source_event_id: str
    source_event_seq: int
    exact_text: str
    text_sha256: str
    source_event_type: RubricSourceEventType = RubricSourceEventType.TASK_STARTED

    def __post_init__(self) -> None:
        _require_semantic_id(self.source_event_id, "task.source_event_id")
        _require_positive_int(self.source_event_seq, "task.source_event_seq")
        _require_string(self.exact_text, "task.exact_text", maximum=32768)
        _require_sha256(self.text_sha256, "task.text_sha256")
        _require_enum(self.source_event_type, RubricSourceEventType, "task.source_event_type")
        if self.source_event_type is not RubricSourceEventType.TASK_STARTED:
            _fail("INVALID_TASK_SOURCE", "task instruction must come from task_started")
        if self.text_sha256 != _text_sha256(self.exact_text):
            _fail("TASK_HASH_MISMATCH", "task text hash does not match exact text")


@dataclass(frozen=True, slots=True)
class InstructionSpanV1:
    span_id: str
    role: InstructionSpanRole
    char_start: int
    char_end: int
    utf8_byte_start: int
    utf8_byte_end: int
    exact_text: str
    span_sha256: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.span_id, "span_id")
        _require_enum(self.role, InstructionSpanRole, "span.role")
        for name, value in (
            ("char_start", self.char_start),
            ("utf8_byte_start", self.utf8_byte_start),
        ):
            _require_nonnegative_int(value, f"span.{name}")
        for name, value in (("char_end", self.char_end), ("utf8_byte_end", self.utf8_byte_end)):
            _require_positive_int(value, f"span.{name}")
        if self.char_end <= self.char_start or self.utf8_byte_end <= self.utf8_byte_start:
            _fail("EMPTY_INSTRUCTION_SPAN", "instruction span must be non-empty")
        _require_string(self.exact_text, "span.exact_text", maximum=32768)
        _require_sha256(self.span_sha256, "span.span_sha256")
        if self.span_sha256 != _text_sha256(self.exact_text):
            _fail("SPAN_HASH_MISMATCH", "instruction span hash does not match exact text")


@dataclass(frozen=True, slots=True)
class MilestoneV1:
    milestone_id: str
    kind: MilestoneKind
    predicate_kind: MilestonePredicateKind
    state_description: str
    description_sha256: str
    instruction_span_id: str | None

    def __post_init__(self) -> None:
        _require_semantic_id(self.milestone_id, "milestone_id")
        _require_enum(self.kind, MilestoneKind, "milestone.kind")
        _require_enum(self.predicate_kind, MilestonePredicateKind, "milestone.predicate_kind")
        _require_string(self.state_description, "milestone.state_description", maximum=2048)
        _require_sha256(self.description_sha256, "milestone.description_sha256")
        if self.description_sha256 != _text_sha256(self.state_description):
            _fail("DESCRIPTION_HASH_MISMATCH", "milestone description hash does not match")
        instruction_bound = self.kind in {
            MilestoneKind.HARD_REQUIREMENT,
            MilestoneKind.CONSTRAINT,
            MilestoneKind.TERMINAL_REQUIREMENT,
        }
        if instruction_bound:
            if self.instruction_span_id is None:
                _fail("MISSING_INSTRUCTION_SPAN", "instruction-bound milestone needs a span")
            _require_semantic_id(self.instruction_span_id, "milestone.instruction_span_id")
            if self.predicate_kind is not MilestonePredicateKind.INSTRUCTION_REQUIREMENT:
                _fail("INVALID_PREDICATE_KIND", "instruction-bound milestone must use exact span")
        else:
            if self.instruction_span_id is not None:
                _fail(
                    "DERIVED_REQUIREMENT_ESCALATION",
                    "derived/optional milestone cannot cite a span",
                )
            if self.predicate_kind is MilestonePredicateKind.INSTRUCTION_REQUIREMENT:
                _fail(
                    "DERIVED_REQUIREMENT_ESCALATION", "derived milestone cannot pose as instruction"
                )

    @property
    def blocking(self) -> bool:
        return self.kind in {
            MilestoneKind.HARD_REQUIREMENT,
            MilestoneKind.CONSTRAINT,
            MilestoneKind.TERMINAL_REQUIREMENT,
        }


@dataclass(frozen=True, slots=True)
class GraphRefV1:
    ref_kind: GraphRefKind
    ref_id: str

    def __post_init__(self) -> None:
        _require_enum(self.ref_kind, GraphRefKind, "graph_ref.ref_kind")
        _require_semantic_id(self.ref_id, "graph_ref.ref_id")


@dataclass(frozen=True, slots=True)
class GateV1:
    gate_id: str
    operator: GateOperator
    children: tuple[GraphRefV1, ...]

    def __post_init__(self) -> None:
        _require_semantic_id(self.gate_id, "gate_id")
        _require_enum(self.operator, GateOperator, "gate.operator")
        _require_tuple(self.children, GraphRefV1, "gate.children", minimum=2, maximum=128)
        _require_unique(
            tuple(f"{item.ref_kind.value}:{item.ref_id}" for item in self.children), "gate children"
        )


@dataclass(frozen=True, slots=True)
class RubricPathV1:
    path_id: str
    kind: PathKind
    root: GraphRefV1 | None

    def __post_init__(self) -> None:
        _require_semantic_id(self.path_id, "path_id")
        _require_enum(self.kind, PathKind, "path.kind")
        if self.kind is PathKind.OTHER_UNKNOWN:
            if self.root is not None:
                _fail("OTHER_PATH_HAS_ROOT", "OTHER/unknown path cannot force a graph route")
        else:
            if type(self.root) is not GraphRefV1:
                _fail("LEGAL_PATH_MISSING_ROOT", "legal alternative requires an exact graph root")


@dataclass(frozen=True, slots=True)
class RequirementDeltaV1:
    kind: RequirementDeltaKind
    requirement_key: str
    span_id: str
    span_role: InstructionSpanRole

    def __post_init__(self) -> None:
        _require_enum(self.kind, RequirementDeltaKind, "requirement_delta.kind")
        _require_sha256(self.requirement_key, "requirement_delta.requirement_key")
        _require_semantic_id(self.span_id, "requirement_delta.span_id")
        _require_enum(self.span_role, InstructionSpanRole, "requirement_delta.span_role")


@dataclass(frozen=True, slots=True)
class RubricRevisionV1:
    revision_id: str
    revision_event_id: str
    kind: RevisionKind
    reason: RevisionReason
    previous_rubric_version: int | None
    previous_rubric_sha256: str | None
    hard_requirement_deltas: tuple[RequirementDeltaV1, ...]
    changed_node_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_runtime_id(self.revision_id, "revision_id")
        _require_semantic_id(self.revision_event_id, "revision_event_id")
        _require_enum(self.kind, RevisionKind, "revision.kind")
        _require_enum(self.reason, RevisionReason, "revision.reason")
        _require_tuple(
            self.hard_requirement_deltas,
            RequirementDeltaV1,
            "revision.hard_requirement_deltas",
            maximum=512,
        )
        if type(self.changed_node_ids) is not tuple or len(self.changed_node_ids) > 1024:
            _fail("INVALID_COLLECTION", "revision.changed_node_ids must be a bounded tuple")
        for value in self.changed_node_ids:
            _require_semantic_id(value, "revision.changed_node_ids item")
        _require_unique(self.changed_node_ids, "revision.changed_node_ids")
        delta_keys = tuple(
            f"{item.kind.value}:{item.requirement_key}" for item in self.hard_requirement_deltas
        )
        _require_unique(delta_keys, "revision hard-requirement deltas")
        if self.kind is RevisionKind.INITIAL:
            if self.reason is not RevisionReason.TASK_START:
                _fail("INVALID_INITIAL_REVISION", "initial rubric must use TASK_START")
            if self.previous_rubric_version is not None or self.previous_rubric_sha256 is not None:
                _fail("INVALID_INITIAL_REVISION", "initial rubric cannot bind a previous version")
            if self.hard_requirement_deltas or self.changed_node_ids:
                _fail("INVALID_INITIAL_REVISION", "initial rubric has no revision delta")
        else:
            if type(self.previous_rubric_version) is not int or self.previous_rubric_version < 1:
                _fail("MISSING_PREVIOUS_VERSION", "explicit revision must bind previous version")
            _require_sha256(self.previous_rubric_sha256, "revision.previous_rubric_sha256")
            if self.reason is RevisionReason.TASK_START:
                _fail("INVALID_REVISION_REASON", "explicit revision cannot use TASK_START")


@dataclass(frozen=True, slots=True)
class RubricBackendDescriptorV1:
    backend_id: str
    backend_version: str
    prompt_sha256: str
    rubric_schema_sha256: str
    tracking_packet_schema_sha256: str
    tracker_schema_sha256: str
    config_sha256: str
    backend_kind: RubricBackendKind = RubricBackendKind.INJECTED_FAKE
    transport_authority: RubricTransportAuthority = RubricTransportAuthority.CPU_OFFLINE_FAKE
    external_network_attempted: bool = False
    model_call_attempted: bool = False
    local_gpu_used: bool = False

    def __post_init__(self) -> None:
        _require_semantic_id(self.backend_id, "backend_id")
        _require_contract_version(self.backend_version, "backend_version")
        for name, value in (
            ("prompt_sha256", self.prompt_sha256),
            ("rubric_schema_sha256", self.rubric_schema_sha256),
            ("tracking_packet_schema_sha256", self.tracking_packet_schema_sha256),
            ("tracker_schema_sha256", self.tracker_schema_sha256),
            ("config_sha256", self.config_sha256),
        ):
            _require_sha256(value, name)
        _require_enum(self.backend_kind, RubricBackendKind, "backend_kind")
        _require_enum(self.transport_authority, RubricTransportAuthority, "transport_authority")
        # Both enums intentionally have one member; the exact enum checks above
        # therefore also enforce the fixed CPU/offline/fake values.
        for flag_name, flag_value in (
            ("external_network_attempted", self.external_network_attempted),
            ("model_call_attempted", self.model_call_attempted),
            ("local_gpu_used", self.local_gpu_used),
        ):
            _require_bool(flag_value, flag_name)
            if flag_value:
                _fail("UNAUTHORIZED_RESOURCE_USE", f"{flag_name} must remain false")


@dataclass(frozen=True, slots=True)
class MultiPathRubricV1:
    rubric_id: str
    task_run_id: str
    rubric_version: int
    task: TaskInstructionV1
    revision: RubricRevisionV1
    instruction_spans: tuple[InstructionSpanV1, ...]
    milestones: tuple[MilestoneV1, ...]
    gates: tuple[GateV1, ...]
    common_root: GraphRefV1 | None
    paths: tuple[RubricPathV1, ...]
    backend: RubricBackendDescriptorV1
    authority: RubricAuthorityV1 = RubricAuthorityV1()
    schema_version: str = RUBRIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != RUBRIC_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown rubric schema version")
        _require_semantic_id(self.rubric_id, "rubric_id")
        _require_runtime_id(self.task_run_id, "task_run_id")
        _require_positive_int(self.rubric_version, "rubric_version")
        _require_exact(self.task, TaskInstructionV1, "task")
        _require_exact(self.revision, RubricRevisionV1, "revision")
        _require_exact(self.backend, RubricBackendDescriptorV1, "backend")
        _require_exact(self.authority, RubricAuthorityV1, "authority")
        _require_tuple(
            self.instruction_spans,
            InstructionSpanV1,
            "instruction_spans",
            maximum=512,
        )
        _require_tuple(self.milestones, MilestoneV1, "milestones", minimum=1, maximum=512)
        _require_tuple(self.gates, GateV1, "gates", maximum=512)
        _require_tuple(self.paths, RubricPathV1, "paths", minimum=2, maximum=128)
        if self.common_root is not None:
            _require_exact(self.common_root, GraphRefV1, "common_root")

        if self.revision.kind is RevisionKind.INITIAL:
            if self.rubric_version != 1:
                _fail("INVALID_INITIAL_VERSION", "initial rubric version must be 1")
        elif self.revision.previous_rubric_version != self.rubric_version - 1:
            _fail("NONCONTIGUOUS_REVISION", "rubric revisions must increment by exactly one")

        span_ids = tuple(value.span_id for value in self.instruction_spans)
        milestone_ids = tuple(value.milestone_id for value in self.milestones)
        gate_ids = tuple(value.gate_id for value in self.gates)
        path_ids = tuple(value.path_id for value in self.paths)
        _require_unique(span_ids, "instruction span IDs")
        _require_unique(milestone_ids, "milestone IDs")
        _require_unique(gate_ids, "gate IDs")
        _require_unique(path_ids, "path IDs")
        if set(milestone_ids) & set(gate_ids):
            _fail("GRAPH_ID_COLLISION", "milestone and gate IDs must be disjoint")

        _validate_instruction_bindings(self)
        _validate_graph(self)


@dataclass(frozen=True, slots=True)
class TaskStartRubricRequestV1:
    request_id: str
    task_run_id: str
    task: TaskInstructionV1
    backend: RubricBackendDescriptorV1

    def __post_init__(self) -> None:
        _require_runtime_id(self.request_id, "task_start.request_id")
        _require_runtime_id(self.task_run_id, "task_start.task_run_id")
        _require_exact(self.task, TaskInstructionV1, "task_start.task")
        _require_exact(self.backend, RubricBackendDescriptorV1, "task_start.backend")


@dataclass(frozen=True, slots=True)
class RubricRevisionRequestV1:
    request_id: str
    task_run_id: str
    previous_rubric_id: str
    previous_rubric_version: int
    previous_rubric_sha256: str
    revision_event_id: str
    reason: RevisionReason
    task: TaskInstructionV1
    backend: RubricBackendDescriptorV1

    def __post_init__(self) -> None:
        _require_runtime_id(self.request_id, "revision_request.request_id")
        _require_runtime_id(self.task_run_id, "revision_request.task_run_id")
        _require_semantic_id(self.previous_rubric_id, "revision_request.previous_rubric_id")
        _require_positive_int(
            self.previous_rubric_version, "revision_request.previous_rubric_version"
        )
        _require_sha256(self.previous_rubric_sha256, "revision_request.previous_rubric_sha256")
        _require_semantic_id(self.revision_event_id, "revision_request.revision_event_id")
        _require_enum(self.reason, RevisionReason, "revision_request.reason")
        if self.reason is RevisionReason.TASK_START:
            _fail("INVALID_REVISION_REASON", "explicit revision cannot use TASK_START")
        _require_exact(self.task, TaskInstructionV1, "revision_request.task")
        _require_exact(self.backend, RubricBackendDescriptorV1, "revision_request.backend")


def _validate_instruction_bindings(rubric: MultiPathRubricV1) -> None:
    spans = {value.span_id: value for value in rubric.instruction_spans}
    ordered = sorted(rubric.instruction_spans, key=lambda value: value.char_start)
    previous_end = -1
    for span in ordered:
        if span.char_start < previous_end:
            _fail("OVERLAPPING_INSTRUCTION_SPANS", "instruction spans must not overlap")
        previous_end = span.char_end
        if span.char_end > len(rubric.task.exact_text):
            _fail("INSTRUCTION_SPAN_OUT_OF_RANGE", "instruction span exceeds task text")
        exact = rubric.task.exact_text[span.char_start : span.char_end]
        byte_start = len(rubric.task.exact_text[: span.char_start].encode("utf-8"))
        byte_end = len(rubric.task.exact_text[: span.char_end].encode("utf-8"))
        if (
            exact != span.exact_text
            or byte_start != span.utf8_byte_start
            or byte_end != span.utf8_byte_end
        ):
            _fail("INSTRUCTION_SPAN_DRIFT", "instruction span coordinates/text drifted")

    expected_roles = {
        MilestoneKind.HARD_REQUIREMENT: InstructionSpanRole.HARD_REQUIREMENT,
        MilestoneKind.CONSTRAINT: InstructionSpanRole.CONSTRAINT,
        MilestoneKind.TERMINAL_REQUIREMENT: InstructionSpanRole.TERMINAL_REQUIREMENT,
    }
    cited: list[str] = []
    for milestone in rubric.milestones:
        expected_role = expected_roles.get(milestone.kind)
        if expected_role is None:
            continue
        assert milestone.instruction_span_id is not None
        bound_span = spans.get(milestone.instruction_span_id)
        if bound_span is None:
            _fail("UNKNOWN_INSTRUCTION_SPAN", "milestone cites an unknown instruction span")
        if bound_span.role is not expected_role:
            _fail("INSTRUCTION_ROLE_MISMATCH", "milestone kind and instruction role differ")
        if (
            milestone.state_description != bound_span.exact_text
            or milestone.description_sha256 != bound_span.span_sha256
        ):
            _fail("SILENT_REQUIREMENT_REWRITE", "instruction requirement must remain exact")
        cited.append(bound_span.span_id)
    _require_unique(tuple(cited), "milestone instruction-span references")
    if set(cited) != set(spans):
        _fail("UNBOUND_INSTRUCTION_SPAN", "every declared instruction span needs one milestone")


def _validate_graph_ref(
    value: GraphRefV1,
    milestones: dict[str, MilestoneV1],
    gates: dict[str, GateV1],
) -> None:
    if value.ref_kind is GraphRefKind.MILESTONE and value.ref_id not in milestones:
        _fail("UNKNOWN_GRAPH_REFERENCE", "graph references an unknown milestone")
    if value.ref_kind is GraphRefKind.GATE and value.ref_id not in gates:
        _fail("UNKNOWN_GRAPH_REFERENCE", "graph references an unknown gate")


def _validate_graph(rubric: MultiPathRubricV1) -> None:
    milestones = {value.milestone_id: value for value in rubric.milestones}
    gates = {value.gate_id: value for value in rubric.gates}
    for gate in rubric.gates:
        for child in gate.children:
            _validate_graph_ref(child, milestones, gates)
    if rubric.common_root is not None:
        _validate_graph_ref(rubric.common_root, milestones, gates)

    legal_paths = [value for value in rubric.paths if value.kind is PathKind.LEGAL_ALTERNATIVE]
    other_paths = [value for value in rubric.paths if value.kind is PathKind.OTHER_UNKNOWN]
    if not legal_paths or len(other_paths) != 1:
        _fail("INVALID_PATH_CENSUS", "rubric needs legal path(s) and exactly one OTHER path")
    for path in legal_paths:
        assert path.root is not None
        _validate_graph_ref(path.root, milestones, gates)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_gate(gate_id: str) -> None:
        if gate_id in visiting:
            _fail("CYCLIC_RUBRIC_GRAPH", "AND/OR graph must be acyclic")
        if gate_id in visited:
            return
        visiting.add(gate_id)
        for child in gates[gate_id].children:
            if child.ref_kind is GraphRefKind.GATE:
                visit_gate(child.ref_id)
        visiting.remove(gate_id)
        visited.add(gate_id)

    for gate_id in gates:
        visit_gate(gate_id)

    reachable_milestones: set[str] = set()
    reachable_gates: set[str] = set()

    def mark(value: GraphRefV1) -> None:
        if value.ref_kind is GraphRefKind.MILESTONE:
            reachable_milestones.add(value.ref_id)
            return
        if value.ref_id in reachable_gates:
            return
        reachable_gates.add(value.ref_id)
        for child in gates[value.ref_id].children:
            mark(child)

    roots = [path.root for path in legal_paths]
    if rubric.common_root is not None:
        roots.append(rubric.common_root)
    for root in roots:
        assert root is not None
        mark(root)
    if reachable_milestones != set(milestones) or reachable_gates != set(gates):
        _fail("UNREACHABLE_GRAPH_NODE", "every graph node must be reachable from a declared root")


@dataclass(frozen=True, slots=True)
class RubricBindingV1:
    rubric_id: str
    rubric_version: int
    rubric_sha256: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.rubric_id, "rubric_binding.rubric_id")
        _require_positive_int(self.rubric_version, "rubric_binding.rubric_version")
        _require_sha256(self.rubric_sha256, "rubric_binding.rubric_sha256")


@dataclass(frozen=True, slots=True)
class TopologyDeclarationV1:
    kind: TopologyKind
    independent_grounding_claim_eligible: bool

    def __post_init__(self) -> None:
        _require_enum(self.kind, TopologyKind, "topology.kind")
        _require_bool(
            self.independent_grounding_claim_eligible,
            "topology.independent_grounding_claim_eligible",
        )
        expected = self.kind is TopologyKind.ISOLATED_HISTORY_FREE
        if self.independent_grounding_claim_eligible is not expected:
            _fail("FALSE_INDEPENDENCE_CLAIM", "topology independence flag is inconsistent")


@dataclass(frozen=True, slots=True)
class RubricCutoffV1:
    run_id: str
    task_run_id: str
    step_id: str
    current_observation_event_id: str
    cutoff_event_seq: int

    def __post_init__(self) -> None:
        _require_runtime_id(self.run_id, "cutoff.run_id")
        _require_runtime_id(self.task_run_id, "cutoff.task_run_id")
        _require_runtime_id(self.step_id, "cutoff.step_id")
        _require_semantic_id(
            self.current_observation_event_id, "cutoff.current_observation_event_id"
        )
        _require_positive_int(self.cutoff_event_seq, "cutoff.cutoff_event_seq")


@dataclass(frozen=True, slots=True)
class TextEvidenceProjectionV1:
    kind: EvidenceProjectionKind
    exact_text: str
    text_sha256: str

    def __post_init__(self) -> None:
        _require_enum(self.kind, EvidenceProjectionKind, "text_projection.kind")
        if self.kind not in {
            EvidenceProjectionKind.TEXT,
            EvidenceProjectionKind.CANONICAL_JSON_TEXT,
        }:
            _fail("INVALID_TEXT_PROJECTION", "text evidence cannot use an image projection")
        _require_string(self.exact_text, "text_projection.exact_text", maximum=65536)
        _require_sha256(self.text_sha256, "text_projection.text_sha256")
        if self.text_sha256 != _text_sha256(self.exact_text):
            _fail("EVIDENCE_HASH_MISMATCH", "text projection hash does not match")


@dataclass(frozen=True, slots=True)
class ImageEvidenceProjectionV1:
    content_sha256: str
    media_type: EvidenceMediaType
    width: int
    height: int
    kind: EvidenceProjectionKind = EvidenceProjectionKind.IMAGE_REFERENCE

    def __post_init__(self) -> None:
        _require_enum(self.kind, EvidenceProjectionKind, "image_projection.kind")
        if self.kind is not EvidenceProjectionKind.IMAGE_REFERENCE:
            _fail("INVALID_IMAGE_PROJECTION", "image evidence must be an image reference")
        _require_sha256(self.content_sha256, "image_projection.content_sha256")
        _require_enum(self.media_type, EvidenceMediaType, "image_projection.media_type")
        for name, value in (("width", self.width), ("height", self.height)):
            _require_positive_int(value, f"image_projection.{name}")
            if value > 32768:
                _fail("INVALID_IMAGE_DIMENSION", f"image {name} is too large")


RubricEvidenceProjectionV1 = TextEvidenceProjectionV1 | ImageEvidenceProjectionV1


_CURRENT_ROLES = frozenset(
    {RubricEvidenceRole.CURRENT_UI_SCREENSHOT, RubricEvidenceRole.CURRENT_ACCESSIBILITY}
)
_TRANSITION_ROLES = frozenset(
    {
        RubricEvidenceRole.COMPLETED_TRANSITION_STATUS,
        RubricEvidenceRole.COMPLETED_POST_UI_STATE,
        RubricEvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
        RubricEvidenceRole.USER_RESPONSE,
    }
)
_WEAK_EVIDENCE_ROLES = frozenset(
    {
        RubricEvidenceRole.COMPLETED_TRANSITION_STATUS,
        RubricEvidenceRole.COMPLETED_POST_UI_STATE,
        RubricEvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
    }
)


@dataclass(frozen=True, slots=True)
class RubricEvidenceV1:
    evidence_id: str
    role: RubricEvidenceRole
    source_event_id: str
    source_event_type: RubricSourceEventType
    source_event_seq: int
    task_run_id: str
    caused_by_event_id: str | None
    payload_sha256: str
    projection: RubricEvidenceProjectionV1
    observed_by_cutoff: bool = True

    def __post_init__(self) -> None:
        _require_semantic_id(self.evidence_id, "evidence_id")
        _require_enum(self.role, RubricEvidenceRole, "evidence.role")
        _require_semantic_id(self.source_event_id, "evidence.source_event_id")
        _require_enum(self.source_event_type, RubricSourceEventType, "evidence.source_event_type")
        _require_positive_int(self.source_event_seq, "evidence.source_event_seq")
        _require_runtime_id(self.task_run_id, "evidence.task_run_id")
        if self.caused_by_event_id is not None:
            _require_semantic_id(self.caused_by_event_id, "evidence.caused_by_event_id")
        _require_sha256(self.payload_sha256, "evidence.payload_sha256")
        if type(self.projection) not in {TextEvidenceProjectionV1, ImageEvidenceProjectionV1}:
            _fail("UNTRUSTED_TYPE", "evidence projection must use an exact trusted type")
        _require_bool(self.observed_by_cutoff, "evidence.observed_by_cutoff")
        if not self.observed_by_cutoff:
            _fail("FUTURE_EVIDENCE", "rubric evidence must be observed by cutoff")

        if self.role in _CURRENT_ROLES:
            if self.source_event_type is not RubricSourceEventType.STEP_STARTED:
                _fail("INVALID_EVIDENCE_SOURCE", "current GUI evidence must come from step_started")
            if self.caused_by_event_id is not None:
                _fail("INVALID_CAUSAL_PARENT", "current GUI evidence has no transition parent")
        else:
            if self.role not in _TRANSITION_ROLES:
                _fail("INVALID_EVIDENCE_ROLE", "unknown rubric evidence role")
            if self.source_event_type not in {
                RubricSourceEventType.TRANSITION_COMPLETED,
                RubricSourceEventType.TRANSITION_FAILED,
                RubricSourceEventType.TRANSITION_NOT_EXECUTED,
            }:
                _fail("INVALID_EVIDENCE_SOURCE", "runtime evidence must be a completed transition")
            if self.caused_by_event_id is None:
                _fail("MISSING_CAUSAL_PARENT", "completed transition evidence needs a parent")
            if (
                self.role
                in {
                    RubricEvidenceRole.AGENT_VISIBLE_TOOL_RESULT,
                    RubricEvidenceRole.USER_RESPONSE,
                }
                and self.source_event_type is not RubricSourceEventType.TRANSITION_COMPLETED
            ):
                _fail("INVALID_EVIDENCE_SOURCE", "tool/user result needs transition_completed")
            if (
                self.role is RubricEvidenceRole.COMPLETED_POST_UI_STATE
                and self.source_event_type is RubricSourceEventType.TRANSITION_NOT_EXECUTED
            ):
                _fail(
                    "INVALID_EVIDENCE_SOURCE",
                    "post-transition UI state requires an executed transition",
                )

        if self.role is RubricEvidenceRole.CURRENT_UI_SCREENSHOT:
            if type(self.projection) is not ImageEvidenceProjectionV1:
                _fail("INVALID_EVIDENCE_PROJECTION", "screenshot needs an image reference")
        elif type(self.projection) is not TextEvidenceProjectionV1:
            _fail("INVALID_EVIDENCE_PROJECTION", "non-image evidence needs a text projection")


@dataclass(frozen=True, slots=True)
class CurrentObservationBindingV1:
    source_event_id: str
    source_event_seq: int
    screenshot_evidence_id: str
    screenshot_content_sha256: str
    accessibility_evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_semantic_id(self.source_event_id, "current_observation.source_event_id")
        _require_positive_int(self.source_event_seq, "current_observation.source_event_seq")
        _require_semantic_id(
            self.screenshot_evidence_id, "current_observation.screenshot_evidence_id"
        )
        _require_sha256(
            self.screenshot_content_sha256, "current_observation.screenshot_content_sha256"
        )
        if (
            type(self.accessibility_evidence_ids) is not tuple
            or len(self.accessibility_evidence_ids) > 64
        ):
            _fail("INVALID_COLLECTION", "accessibility evidence IDs must be a bounded tuple")
        for value in self.accessibility_evidence_ids:
            _require_semantic_id(value, "accessibility_evidence_ids item")
        _require_unique(self.accessibility_evidence_ids, "accessibility evidence IDs")


@dataclass(frozen=True, slots=True)
class TrackingInputExclusionsV1:
    natural_language_actor_history_included: bool = False
    history_ir_included: bool = False
    history_policy_output_used_as_truth: bool = False
    future_event_included: bool = False
    task_outcome_included: bool = False
    benchmark_checker_included: bool = False
    replay_result_included: bool = False
    collector_raw_mutated: bool = False

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            _require_bool(value, name)
            if value:
                _fail("FORBIDDEN_TRACKING_INPUT", f"{name} must remain false")


@dataclass(frozen=True, slots=True)
class MilestoneEvidenceRefV1:
    evidence_id: str
    payload_sha256: str
    relation: MilestoneEvidenceRelation

    def __post_init__(self) -> None:
        _require_semantic_id(self.evidence_id, "milestone_evidence_ref.evidence_id")
        _require_sha256(self.payload_sha256, "milestone_evidence_ref.payload_sha256")
        _require_enum(self.relation, MilestoneEvidenceRelation, "milestone_evidence_ref.relation")


@dataclass(frozen=True, slots=True)
class MilestoneStateRecordV1:
    milestone_id: str
    state: MilestoneState
    evidence_refs: tuple[MilestoneEvidenceRefV1, ...]
    reason_code: MilestoneReasonCode

    def __post_init__(self) -> None:
        _require_semantic_id(self.milestone_id, "milestone_state.milestone_id")
        _require_enum(self.state, MilestoneState, "milestone_state.state")
        _require_tuple(
            self.evidence_refs,
            MilestoneEvidenceRefV1,
            "milestone_state.evidence_refs",
            maximum=32,
        )
        _require_unique(
            tuple(value.evidence_id for value in self.evidence_refs),
            "milestone evidence references",
        )
        _require_enum(self.reason_code, MilestoneReasonCode, "milestone_state.reason_code")
        if self.reason_code is MilestoneReasonCode.PRESERVE_PRIOR_STATE:
            if self.state is MilestoneState.PENDING:
                _fail("INVALID_PRESERVED_STATE", "pending is represented by NOT_STARTED")
        elif self.state is MilestoneState.PENDING:
            if self.evidence_refs or self.reason_code is not MilestoneReasonCode.NOT_STARTED:
                _fail("INVALID_PENDING_STATE", "pending requires no evidence and NOT_STARTED")
        elif self.state is MilestoneState.SATISFIED:
            if not self.evidence_refs or not any(
                value.relation is MilestoneEvidenceRelation.SUPPORTS_STATE
                for value in self.evidence_refs
            ):
                _fail("UNSUPPORTED_SATISFACTION", "satisfied state needs supporting evidence")
            if self.reason_code not in {
                MilestoneReasonCode.CURRENT_GUI_SUPPORT,
                MilestoneReasonCode.COMPLETED_TRANSITION_SUPPORT,
            }:
                _fail("STATE_REASON_MISMATCH", "satisfied state needs a support reason")
        elif self.state is MilestoneState.VIOLATED:
            if not self.evidence_refs or not any(
                value.relation is MilestoneEvidenceRelation.REFUTES_STATE
                for value in self.evidence_refs
            ):
                _fail("UNSUPPORTED_VIOLATION", "violated state needs refuting evidence")
            if self.reason_code not in {
                MilestoneReasonCode.CURRENT_GUI_REFUTATION,
                MilestoneReasonCode.COMPLETED_TRANSITION_REFUTATION,
            }:
                _fail("STATE_REASON_MISMATCH", "violated state needs a refutation reason")
        elif self.state is MilestoneState.IN_PROGRESS:
            if not self.evidence_refs or not any(
                value.relation is MilestoneEvidenceRelation.OBSERVES_PROGRESS
                for value in self.evidence_refs
            ):
                _fail("UNSUPPORTED_PROGRESS", "in-progress state needs progress evidence")
            if self.reason_code is not MilestoneReasonCode.PROGRESS_OBSERVED:
                _fail("STATE_REASON_MISMATCH", "in-progress state needs PROGRESS_OBSERVED")
        elif self.reason_code not in {
            MilestoneReasonCode.AMBIGUOUS_GUI,
            MilestoneReasonCode.INSUFFICIENT_EVIDENCE,
            MilestoneReasonCode.CONFLICTING_EVIDENCE,
            MilestoneReasonCode.PRESERVE_PRIOR_STATE,
        }:
            _fail("INVALID_UNKNOWN_STATE", "unknown needs an uncertainty reason")


@dataclass(frozen=True, slots=True)
class PathStateV1:
    path_id: str
    state: PathViability

    def __post_init__(self) -> None:
        _require_semantic_id(self.path_id, "path_state.path_id")
        _require_enum(self.state, PathViability, "path_state.state")


@dataclass(frozen=True, slots=True)
class FrontierItemV1:
    path_id: str
    milestone_id: str

    def __post_init__(self) -> None:
        _require_semantic_id(self.path_id, "frontier.path_id")
        _require_semantic_id(self.milestone_id, "frontier.milestone_id")


@dataclass(frozen=True, slots=True)
class ActorVisibleRubricStateV1:
    enabled: bool
    exact_text: str | None
    text_sha256: str | None
    content_kind: str = "DETERMINISTIC_STATUS_ONLY"
    independently_configured: bool = True
    actor_request_injected: bool = False
    history_filtering_controlled: bool = False

    def __post_init__(self) -> None:
        _require_bool(self.enabled, "actor_visible.enabled")
        if self.content_kind != "DETERMINISTIC_STATUS_ONLY":
            _fail("UNSAFE_ACTOR_PROJECTION", "actor-visible content must use status-only template")
        for name, value in (
            ("independently_configured", self.independently_configured),
            ("actor_request_injected", self.actor_request_injected),
            ("history_filtering_controlled", self.history_filtering_controlled),
        ):
            _require_bool(value, f"actor_visible.{name}")
        if (
            not self.independently_configured
            or self.actor_request_injected
            or self.history_filtering_controlled
        ):
            _fail("PRESENTATION_COUPLING", "R2.3 presentation must remain detached and independent")
        if self.enabled:
            _require_string(self.exact_text, "actor_visible.exact_text", maximum=8192)
            _require_sha256(self.text_sha256, "actor_visible.text_sha256")
            assert self.exact_text is not None
            if self.text_sha256 != _text_sha256(self.exact_text):
                _fail("PRESENTATION_HASH_MISMATCH", "actor-visible text hash does not match")
        elif self.exact_text is not None or self.text_sha256 is not None:
            _fail("DISABLED_PRESENTATION_HAS_TEXT", "disabled actor-visible state must be empty")


@dataclass(frozen=True, slots=True)
class RubricTrackingStateV1:
    state_id: str
    rubric_binding: RubricBindingV1
    state_version: int
    source_packet_id: str | None
    logical_call_id: str | None
    prior_state_sha256: str | None
    milestone_states: tuple[MilestoneStateRecordV1, ...]
    path_states: tuple[PathStateV1, ...]
    frontier: tuple[FrontierItemV1, ...]
    topology: TopologyDeclarationV1
    actor_visible: ActorVisibleRubricStateV1
    authority: RubricAuthorityV1 = RubricAuthorityV1()
    output_kind: TrackerOutputKind = TrackerOutputKind.TRACKING_STATE
    schema_version: str = TRACKER_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACKER_OUTPUT_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown tracker output schema")
        _require_enum(self.output_kind, TrackerOutputKind, "tracking_state.output_kind")
        if self.output_kind is not TrackerOutputKind.TRACKING_STATE:
            _fail("INVALID_OUTPUT_KIND", "tracking state has wrong output kind")
        _require_semantic_id(self.state_id, "state_id")
        _require_exact(self.rubric_binding, RubricBindingV1, "rubric_binding")
        _require_nonnegative_int(self.state_version, "state_version")
        if self.state_version == 0:
            if (
                self.source_packet_id is not None
                or self.logical_call_id is not None
                or self.prior_state_sha256 is not None
            ):
                _fail("INVALID_INITIAL_STATE", "initial state cannot bind a runtime update")
        else:
            _require_runtime_id(self.source_packet_id, "source_packet_id")
            _require_runtime_id(self.logical_call_id, "logical_call_id")
            _require_sha256(self.prior_state_sha256, "prior_state_sha256")
        _require_tuple(
            self.milestone_states,
            MilestoneStateRecordV1,
            "milestone_states",
            minimum=1,
            maximum=512,
        )
        _require_tuple(self.path_states, PathStateV1, "path_states", minimum=2, maximum=128)
        _require_tuple(self.frontier, FrontierItemV1, "frontier", maximum=4096)
        _require_unique(
            tuple(value.milestone_id for value in self.milestone_states),
            "tracking-state milestone IDs",
        )
        _require_unique(tuple(value.path_id for value in self.path_states), "path-state IDs")
        _require_unique(
            tuple(f"{value.path_id}:{value.milestone_id}" for value in self.frontier),
            "frontier pairs",
        )
        _require_exact(self.topology, TopologyDeclarationV1, "topology")
        _require_exact(self.actor_visible, ActorVisibleRubricStateV1, "actor_visible")
        _require_exact(self.authority, RubricAuthorityV1, "authority")


def derive_actor_visible_rubric_state(
    *,
    enabled: bool,
    milestone_states: tuple[MilestoneStateRecordV1, ...],
    path_states: tuple[PathStateV1, ...],
) -> ActorVisibleRubricStateV1:
    """Build the only status text admitted into a history-free state packet."""

    _require_bool(enabled, "actor_visible.enabled")
    _require_tuple(
        milestone_states,
        MilestoneStateRecordV1,
        "actor_visible milestone states",
        minimum=1,
        maximum=512,
    )
    _require_tuple(
        path_states,
        PathStateV1,
        "actor_visible path states",
        minimum=2,
        maximum=128,
    )
    if not enabled:
        return ActorVisibleRubricStateV1(enabled=False, exact_text=None, text_sha256=None)

    milestone_counts = {state: 0 for state in MilestoneState}
    path_counts = {state: 0 for state in PathViability}
    for milestone_item in milestone_states:
        milestone_counts[milestone_item.state] += 1
    for path_item in path_states:
        path_counts[path_item.state] += 1
    text = (
        "Rubric status: "
        f"pending={milestone_counts[MilestoneState.PENDING]}, "
        f"in_progress={milestone_counts[MilestoneState.IN_PROGRESS]}, "
        f"satisfied={milestone_counts[MilestoneState.SATISFIED]}, "
        f"violated={milestone_counts[MilestoneState.VIOLATED]}, "
        f"unknown={milestone_counts[MilestoneState.UNKNOWN]}; "
        f"viable_paths={path_counts[PathViability.VIABLE]}, "
        f"inactive_paths={path_counts[PathViability.INACTIVE]}, "
        f"unknown_paths={path_counts[PathViability.UNKNOWN]}."
    )
    return ActorVisibleRubricStateV1(
        enabled=True,
        exact_text=text,
        text_sha256=_text_sha256(text),
    )


@dataclass(frozen=True, slots=True)
class RubricTrackingPacketV1:
    packet_id: str
    logical_call_id: str
    task_run_id: str
    step_id: str
    rubric_binding: RubricBindingV1
    prior_state: RubricTrackingStateV1
    cutoff: RubricCutoffV1
    task: TaskInstructionV1
    current_observation: CurrentObservationBindingV1
    evidence_index: tuple[RubricEvidenceV1, ...]
    input_exclusions: TrackingInputExclusionsV1
    topology: TopologyDeclarationV1 = TopologyDeclarationV1(
        kind=TopologyKind.ISOLATED_HISTORY_FREE,
        independent_grounding_claim_eligible=True,
    )
    schema_version: str = TRACKING_PACKET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACKING_PACKET_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown tracking packet schema")
        _require_runtime_id(self.packet_id, "packet_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_runtime_id(self.task_run_id, "task_run_id")
        _require_runtime_id(self.step_id, "step_id")
        _require_exact(self.rubric_binding, RubricBindingV1, "rubric_binding")
        _require_exact(self.prior_state, RubricTrackingStateV1, "prior_state")
        _require_exact(self.cutoff, RubricCutoffV1, "cutoff")
        _require_exact(self.task, TaskInstructionV1, "task")
        _require_exact(self.current_observation, CurrentObservationBindingV1, "current_observation")
        _require_tuple(
            self.evidence_index,
            RubricEvidenceV1,
            "evidence_index",
            minimum=1,
            maximum=512,
        )
        _require_unique(tuple(value.evidence_id for value in self.evidence_index), "evidence IDs")
        _require_exact(self.input_exclusions, TrackingInputExclusionsV1, "input_exclusions")
        _require_exact(self.topology, TopologyDeclarationV1, "topology")
        if self.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE:
            _fail("NON_ISOLATED_TRACKING_PACKET", "R2.3 tracking packet is history-free only")
        if self.prior_state.topology != self.topology:
            _fail(
                "PRIOR_STATE_TOPOLOGY_DRIFT",
                "history-free packet requires an isolated prior state",
            )


@dataclass(frozen=True, slots=True)
class RubricTrackerProposalV1:
    proposal_id: str
    packet_id: str
    packet_sha256: str
    rubric_binding: RubricBindingV1
    prior_state_sha256: str
    proposal_status: TrackerProposalStatus
    milestone_states: tuple[MilestoneStateRecordV1, ...]
    authority: RubricAuthorityV1 = RubricAuthorityV1()
    output_kind: TrackerOutputKind = TrackerOutputKind.MILESTONE_PROPOSAL
    schema_version: str = TRACKER_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACKER_OUTPUT_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown tracker output schema")
        _require_enum(self.output_kind, TrackerOutputKind, "proposal.output_kind")
        if self.output_kind is not TrackerOutputKind.MILESTONE_PROPOSAL:
            _fail("INVALID_OUTPUT_KIND", "milestone proposal has wrong output kind")
        _require_semantic_id(self.proposal_id, "proposal_id")
        _require_runtime_id(self.packet_id, "proposal.packet_id")
        _require_sha256(self.packet_sha256, "proposal.packet_sha256")
        _require_exact(self.rubric_binding, RubricBindingV1, "proposal.rubric_binding")
        _require_sha256(self.prior_state_sha256, "proposal.prior_state_sha256")
        _require_enum(self.proposal_status, TrackerProposalStatus, "proposal.proposal_status")
        _require_tuple(
            self.milestone_states,
            MilestoneStateRecordV1,
            "proposal.milestone_states",
            minimum=1,
            maximum=512,
        )
        _require_unique(
            tuple(value.milestone_id for value in self.milestone_states),
            "proposal milestone IDs",
        )
        if self.proposal_status is TrackerProposalStatus.ABSTAIN and any(
            value.state is not MilestoneState.UNKNOWN for value in self.milestone_states
        ):
            _fail("INVALID_ABSTAIN", "ABSTAIN proposal must mark every milestone unknown")
        _require_exact(self.authority, RubricAuthorityV1, "proposal.authority")


@dataclass(frozen=True, slots=True)
class SupportedRecordBindingV1:
    """Hash binding to a separately admitted history-validity result."""

    record_id: str
    policy_receipt_sha256: str
    policy_output_sha256: str
    factual_verdict: ExternalValidityVerdict = ExternalValidityVerdict.SUPPORTED
    validity_operation: ExternalValidityOperation = ExternalValidityOperation.KEEP

    def __post_init__(self) -> None:
        _require_semantic_id(self.record_id, "supported_record.record_id")
        _require_sha256(self.policy_receipt_sha256, "supported_record.policy_receipt_sha256")
        _require_sha256(self.policy_output_sha256, "supported_record.policy_output_sha256")
        _require_enum(
            self.factual_verdict, ExternalValidityVerdict, "supported_record.factual_verdict"
        )
        _require_enum(
            self.validity_operation,
            ExternalValidityOperation,
            "supported_record.validity_operation",
        )


@dataclass(frozen=True, slots=True)
class RecordPathBindingV1:
    record_id: str
    linked_path_ids: tuple[str, ...]
    path_independent: bool

    def __post_init__(self) -> None:
        _require_semantic_id(self.record_id, "record_path_binding.record_id")
        if type(self.linked_path_ids) is not tuple or len(self.linked_path_ids) > 128:
            _fail("INVALID_COLLECTION", "linked_path_ids must be a bounded tuple")
        for value in self.linked_path_ids:
            _require_semantic_id(value, "linked_path_ids item")
        _require_unique(self.linked_path_ids, "linked_path_ids")
        _require_bool(self.path_independent, "record_path_binding.path_independent")
        if self.path_independent and self.linked_path_ids:
            _fail("AMBIGUOUS_RECORD_BINDING", "path-independent record cannot bind a path")


@dataclass(frozen=True, slots=True)
class RecordRelevanceResultV1:
    record_id: str
    relevance: RecordRelevance
    linked_path_ids: tuple[str, ...]
    supported_record_binding_sha256: str | None
    disposition: RelevanceDisposition

    def __post_init__(self) -> None:
        _require_semantic_id(self.record_id, "record_relevance.record_id")
        _require_enum(self.relevance, RecordRelevance, "record_relevance.relevance")
        if type(self.linked_path_ids) is not tuple or len(self.linked_path_ids) > 128:
            _fail("INVALID_COLLECTION", "relevance linked paths must be a bounded tuple")
        for value in self.linked_path_ids:
            _require_semantic_id(value, "record_relevance linked path")
        _require_unique(self.linked_path_ids, "record relevance linked paths")
        if self.supported_record_binding_sha256 is not None:
            _require_sha256(
                self.supported_record_binding_sha256,
                "record_relevance.supported_record_binding_sha256",
            )
        _require_enum(self.disposition, RelevanceDisposition, "record_relevance.disposition")
        if self.disposition is RelevanceDisposition.ARCHIVE_SHADOW:
            if self.relevance is not RecordRelevance.INACTIVE_BRANCH:
                _fail("UNSAFE_ARCHIVE", "ARCHIVE requires inactive-branch relevance")
            if self.supported_record_binding_sha256 is None:
                _fail("UNSUPPORTED_ARCHIVE", "ARCHIVE requires separately supported history")


@dataclass(frozen=True, slots=True)
class PathRelevanceOutputV1:
    linkage_id: str
    logical_call_id: str
    rubric_state_sha256: str
    records: tuple[RecordRelevanceResultV1, ...]
    topology: TopologyDeclarationV1
    execution_scope: RubricExecutionScope = RubricExecutionScope.SHADOW_ONLY
    authority: RubricAuthorityV1 = RubricAuthorityV1()
    output_kind: TrackerOutputKind = TrackerOutputKind.PATH_RELEVANCE
    schema_version: str = TRACKER_OUTPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TRACKER_OUTPUT_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown tracker output schema")
        _require_enum(self.output_kind, TrackerOutputKind, "path_relevance.output_kind")
        if self.output_kind is not TrackerOutputKind.PATH_RELEVANCE:
            _fail("INVALID_OUTPUT_KIND", "path relevance has wrong output kind")
        _require_runtime_id(self.linkage_id, "linkage_id")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        _require_sha256(self.rubric_state_sha256, "rubric_state_sha256")
        _require_tuple(
            self.records,
            RecordRelevanceResultV1,
            "path_relevance.records",
            maximum=512,
        )
        _require_unique(tuple(value.record_id for value in self.records), "relevance record IDs")
        _require_exact(self.topology, TopologyDeclarationV1, "path_relevance.topology")
        _require_enum(self.execution_scope, RubricExecutionScope, "execution_scope")
        _require_exact(self.authority, RubricAuthorityV1, "path_relevance.authority")
        if (
            any(value.disposition is RelevanceDisposition.ARCHIVE_SHADOW for value in self.records)
            and self.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE
        ):
            _fail("NON_INDEPENDENT_ARCHIVE", "joint topology cannot propose ARCHIVE in R2.3")


@dataclass(frozen=True, slots=True)
class TopologyRunV1:
    topology: TopologyDeclarationV1
    status: TopologyRunStatus
    rubric_input_sha256: str | None
    rubric_output_sha256: str | None
    rubric_receipt_sha256: str | None
    history_policy_input_sha256: str | None
    history_policy_output_sha256: str | None
    failure_code: str | None
    total_latency_ns: int

    def __post_init__(self) -> None:
        _require_exact(self.topology, TopologyDeclarationV1, "topology_run.topology")
        _require_enum(self.status, TopologyRunStatus, "topology_run.status")
        for name in (
            "rubric_input_sha256",
            "rubric_output_sha256",
            "rubric_receipt_sha256",
            "history_policy_input_sha256",
            "history_policy_output_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_sha256(value, f"topology_run.{name}")
        if self.failure_code is not None:
            _require_safe_code(self.failure_code, "topology_run.failure_code")
        _require_nonnegative_int(self.total_latency_ns, "topology_run.total_latency_ns")

        if self.topology.kind is TopologyKind.ISOLATED_HISTORY_FREE:
            if (
                self.history_policy_input_sha256 is not None
                or self.history_policy_output_sha256 is not None
            ):
                _fail(
                    "ISOLATED_HISTORY_LEAK", "isolated rubric run cannot bind history-policy data"
                )
        elif self.status is TopologyRunStatus.ADMITTED and (
            self.history_policy_input_sha256 is None or self.history_policy_output_sha256 is None
        ):
            _fail("INCOMPLETE_JOINT_BINDING", "admitted joint run must bind policy input/output")

        if self.status is TopologyRunStatus.NOT_RUN:
            if (
                any(
                    value is not None
                    for value in (
                        self.rubric_input_sha256,
                        self.rubric_output_sha256,
                        self.rubric_receipt_sha256,
                        self.history_policy_input_sha256,
                        self.history_policy_output_sha256,
                        self.failure_code,
                    )
                )
                or self.total_latency_ns != 0
            ):
                _fail("INVALID_NOT_RUN", "NOT_RUN topology has no hashes, failure, or latency")
        elif self.status is TopologyRunStatus.ADMITTED:
            if (
                self.rubric_input_sha256 is None
                or self.rubric_output_sha256 is None
                or self.rubric_receipt_sha256 is None
                or self.failure_code is not None
            ):
                _fail("INCOMPLETE_TOPOLOGY_RUN", "admitted topology needs complete rubric hashes")
        elif self.failure_code is None:
            _fail("MISSING_TOPOLOGY_FAILURE", "fallback topology needs a typed failure")


@dataclass(frozen=True, slots=True)
class TopologyComparisonV1:
    comparison_id: str
    logical_call_id: str
    isolated: TopologyRunV1
    joint: TopologyRunV1 | None
    deployment_decision: TopologyDeploymentDecision = TopologyDeploymentDecision.UNDECIDED_R2_4
    independent_grounding_source: str = "ISOLATED_ONLY"
    joint_may_replace_isolated: bool = False
    deployment_topology_frozen: bool = False
    schema_version: str = TOPOLOGY_COMPARISON_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TOPOLOGY_COMPARISON_SCHEMA_VERSION:
            _fail("UNKNOWN_SCHEMA_VERSION", "unknown topology comparison schema")
        _require_runtime_id(self.comparison_id, "comparison_id")
        _require_runtime_id(self.logical_call_id, "comparison.logical_call_id")
        _require_exact(self.isolated, TopologyRunV1, "comparison.isolated")
        if self.isolated.topology.kind is not TopologyKind.ISOLATED_HISTORY_FREE:
            _fail("MISSING_ISOLATED_RUN", "isolated slot must contain isolated topology")
        if self.joint is not None:
            _require_exact(self.joint, TopologyRunV1, "comparison.joint")
            if self.joint.topology.kind is not TopologyKind.JOINT_NON_INDEPENDENT:
                _fail("INVALID_JOINT_RUN", "joint slot must be explicitly non-independent")
        _require_enum(self.deployment_decision, TopologyDeploymentDecision, "deployment_decision")
        if self.independent_grounding_source != "ISOLATED_ONLY":
            _fail("FALSE_INDEPENDENCE_CLAIM", "only isolated output can support independence")
        for name, value in (
            ("joint_may_replace_isolated", self.joint_may_replace_isolated),
            ("deployment_topology_frozen", self.deployment_topology_frozen),
        ):
            _require_bool(value, name)
            if value:
                _fail("TOPOLOGY_PREMATURELY_FROZEN", f"{name} must remain false in R2.3")


@runtime_checkable
class RubricExecutionControlV1(Protocol):
    """Owner of one bounded backend and one sidecar publication gate."""

    def run_backend[T](self, call: Callable[[], T]) -> T: ...

    def publish_receipt(self, publish: Callable[[], None]) -> None: ...


@runtime_checkable
class RubricBuilderBackendV1(Protocol):
    @property
    def descriptor(self) -> RubricBackendDescriptorV1: ...

    def generate(self, request: TaskStartRubricRequestV1) -> MultiPathRubricV1: ...

    def revise(self, request: RubricRevisionRequestV1) -> MultiPathRubricV1: ...


@runtime_checkable
class RubricTrackerBackendV1(Protocol):
    @property
    def descriptor(self) -> RubricBackendDescriptorV1: ...

    def track(self, packet: RubricTrackingPacketV1) -> RubricTrackerProposalV1: ...


@runtime_checkable
class PathRelevanceInterfaceV1(Protocol):
    def link(
        self,
        *,
        state: RubricTrackingStateV1,
        record_bindings: tuple[RecordPathBindingV1, ...],
        supported_records: tuple[SupportedRecordBindingV1, ...],
        logical_call_id: str,
    ) -> PathRelevanceOutputV1: ...


def _require_rubric_binding(binding: RubricBindingV1, rubric: MultiPathRubricV1) -> None:
    expected = rubric_binding(rubric)
    if binding != expected:
        _fail("RUBRIC_BINDING_MISMATCH", "value binds a different frozen rubric")


def validate_rubric_revision(previous: MultiPathRubricV1, current: MultiPathRubricV1) -> None:
    """Require a complete explicit delta from one frozen graph to the next."""

    _require_exact(previous, MultiPathRubricV1, "previous rubric")
    _require_exact(current, MultiPathRubricV1, "current rubric")
    if previous.rubric_id != current.rubric_id or previous.task_run_id != current.task_run_id:
        _fail("REVISION_IDENTITY_DRIFT", "revision changed rubric/task-run identity")
    if current.revision.kind is not RevisionKind.EXPLICIT_REVISION:
        _fail("REVISION_REQUIRED", "a changed rubric requires an explicit revision")
    if current.rubric_version != previous.rubric_version + 1:
        _fail("NONCONTIGUOUS_REVISION", "revision version is not previous + 1")
    if current.revision.previous_rubric_sha256 != rubric_sha256(previous):
        _fail("PREVIOUS_RUBRIC_HASH_MISMATCH", "revision binds a different prior graph")
    task_changed = current.task.text_sha256 != previous.task.text_sha256
    if task_changed != (current.revision.reason is RevisionReason.TASK_INSTRUCTION_CHANGED):
        _fail("REVISION_REASON_MISMATCH", "task hash change and revision reason disagree")

    old_requirements = _requirement_inventory(previous)
    new_requirements = _requirement_inventory(current)
    expected_deltas = {
        (
            RequirementDeltaKind.REMOVED,
            key,
            old_requirements[key].span_id,
            old_requirements[key].role,
        )
        for key in old_requirements.keys() - new_requirements.keys()
    } | {
        (
            RequirementDeltaKind.ADDED,
            key,
            new_requirements[key].span_id,
            new_requirements[key].role,
        )
        for key in new_requirements.keys() - old_requirements.keys()
    }
    actual_deltas = {
        (value.kind, value.requirement_key, value.span_id, value.span_role)
        for value in current.revision.hard_requirement_deltas
    }
    if actual_deltas != expected_deltas:
        _fail("REQUIREMENT_DELTA_MISMATCH", "hard-requirement delta is incomplete or invented")

    old_nodes = _node_hashes(previous)
    new_nodes = _node_hashes(current)
    expected_changed = {
        key
        for key in old_nodes.keys() | new_nodes.keys()
        if old_nodes.get(key) != new_nodes.get(key)
    }
    if set(current.revision.changed_node_ids) != expected_changed:
        _fail("GRAPH_DELTA_MISMATCH", "changed_node_ids does not match exact graph changes")


def validate_tracking_packet(packet: RubricTrackingPacketV1, rubric: MultiPathRubricV1) -> None:
    _require_exact(packet, RubricTrackingPacketV1, "tracking packet")
    _require_exact(rubric, MultiPathRubricV1, "rubric")
    _require_rubric_binding(packet.rubric_binding, rubric)
    if packet.task_run_id != rubric.task_run_id or packet.cutoff.task_run_id != rubric.task_run_id:
        _fail("CROSS_TASK_TRACKING", "packet and rubric task run differ")
    if packet.step_id != packet.cutoff.step_id:
        _fail("STEP_BINDING_MISMATCH", "packet and cutoff step differ")
    if packet.task != rubric.task:
        _fail("TASK_BINDING_MISMATCH", "tracking packet task differs from frozen rubric")
    if packet.task.source_event_seq > packet.cutoff.cutoff_event_seq:
        _fail("FUTURE_TASK_INSTRUCTION", "task instruction is after the causal cutoff")
    if packet.task.source_event_seq >= packet.current_observation.source_event_seq:
        _fail(
            "TASK_AFTER_CURRENT_OBSERVATION",
            "task_started must precede the current step_started observation",
        )
    if packet.prior_state.rubric_binding != packet.rubric_binding:
        _fail("PRIOR_STATE_RUBRIC_DRIFT", "prior state binds a different rubric")
    validate_tracking_state(packet.prior_state, rubric)
    if packet.prior_state.topology != packet.topology:
        _fail(
            "PRIOR_STATE_TOPOLOGY_DRIFT",
            "history-free tracking cannot consume a state from another topology",
        )

    evidence = {value.evidence_id: value for value in packet.evidence_index}
    screenshot_ids = tuple(
        value.evidence_id
        for value in packet.evidence_index
        if value.role is RubricEvidenceRole.CURRENT_UI_SCREENSHOT
    )
    accessibility_ids = tuple(
        value.evidence_id
        for value in packet.evidence_index
        if value.role is RubricEvidenceRole.CURRENT_ACCESSIBILITY
    )
    if screenshot_ids != (packet.current_observation.screenshot_evidence_id,):
        _fail(
            "CURRENT_SCREENSHOT_CENSUS_MISMATCH",
            "tracking packet needs exactly its bound current screenshot",
        )
    if set(accessibility_ids) != set(packet.current_observation.accessibility_evidence_ids):
        _fail(
            "ACCESSIBILITY_CENSUS_MISMATCH",
            "current accessibility evidence must exactly match its binding",
        )
    screenshot = evidence.get(packet.current_observation.screenshot_evidence_id)
    if screenshot is None or screenshot.role is not RubricEvidenceRole.CURRENT_UI_SCREENSHOT:
        _fail("CURRENT_SCREENSHOT_MISSING", "current screenshot evidence is missing")
    assert type(screenshot.projection) is ImageEvidenceProjectionV1
    if screenshot.projection.content_sha256 != packet.current_observation.screenshot_content_sha256:
        _fail("CURRENT_SCREENSHOT_DRIFT", "current screenshot content hash differs")
    if (
        packet.current_observation.source_event_id != packet.cutoff.current_observation_event_id
        or packet.current_observation.source_event_id != screenshot.source_event_id
        or packet.current_observation.source_event_seq != screenshot.source_event_seq
        or packet.current_observation.source_event_seq > packet.cutoff.cutoff_event_seq
    ):
        _fail("CURRENT_OBSERVATION_DRIFT", "current observation event differs")
    for evidence_id in packet.current_observation.accessibility_evidence_ids:
        item = evidence.get(evidence_id)
        if (
            item is None
            or item.role is not RubricEvidenceRole.CURRENT_ACCESSIBILITY
            or item.source_event_id != packet.current_observation.source_event_id
            or item.source_event_seq != packet.current_observation.source_event_seq
        ):
            _fail("ACCESSIBILITY_BINDING_MISMATCH", "accessibility evidence binding differs")
    for item in packet.evidence_index:
        if item.task_run_id != packet.task_run_id:
            _fail("CROSS_TASK_EVIDENCE", "evidence belongs to another task")
        if item.source_event_seq > packet.cutoff.cutoff_event_seq:
            _fail("FUTURE_EVIDENCE", "evidence is after the causal cutoff")
        if item.role in _CURRENT_ROLES and (
            item.source_event_id != packet.current_observation.source_event_id
            or item.source_event_seq != packet.current_observation.source_event_seq
        ):
            _fail(
                "CURRENT_OBSERVATION_DRIFT",
                "all current GUI evidence must bind the same step_started event",
            )
        if (
            item.role in _TRANSITION_ROLES
            and item.source_event_seq >= packet.current_observation.source_event_seq
        ):
            _fail(
                "NON_PRIOR_TRANSITION_EVIDENCE",
                "transition evidence must precede the current observation",
            )


def validate_tracker_proposal(
    proposal: RubricTrackerProposalV1,
    packet: RubricTrackingPacketV1,
    rubric: MultiPathRubricV1,
) -> None:
    _require_exact(proposal, RubricTrackerProposalV1, "tracker proposal")
    validate_tracking_packet(packet, rubric)
    if proposal.packet_id != packet.packet_id or proposal.packet_sha256 != tracking_packet_sha256(
        packet
    ):
        _fail("PACKET_BINDING_MISMATCH", "proposal binds a different tracking packet")
    _require_rubric_binding(proposal.rubric_binding, rubric)
    if proposal.prior_state_sha256 != rubric_tracking_state_sha256(packet.prior_state):
        _fail("PRIOR_STATE_HASH_MISMATCH", "proposal binds a different prior state")
    expected_milestones = {value.milestone_id for value in rubric.milestones}
    if {value.milestone_id for value in proposal.milestone_states} != expected_milestones:
        _fail("MILESTONE_CENSUS_MISMATCH", "proposal needs one state per rubric milestone")
    evidence = {value.evidence_id: value for value in packet.evidence_index}
    prior_states = {value.milestone_id: value for value in packet.prior_state.milestone_states}
    for state in proposal.milestone_states:
        prior_state = prior_states[state.milestone_id]
        if state.reason_code is MilestoneReasonCode.PRESERVE_PRIOR_STATE:
            if (
                state.state is not prior_state.state
                or state.evidence_refs != prior_state.evidence_refs
            ):
                _fail(
                    "INVALID_PRESERVED_STATE",
                    "PRESERVE_PRIOR_STATE must carry forward prior state and evidence",
                )
            continue
        if (
            prior_state.state is not MilestoneState.PENDING
            and state.state is MilestoneState.PENDING
        ):
            _fail(
                "MILESTONE_STATE_REGRESSION",
                "an observed milestone cannot silently reset to pending",
            )
        cited: list[tuple[MilestoneEvidenceRefV1, RubricEvidenceV1]] = []
        for reference in state.evidence_refs:
            item = evidence.get(reference.evidence_id)
            if item is None or item.payload_sha256 != reference.payload_sha256:
                _fail("EVIDENCE_BINDING_MISMATCH", "proposal cites missing or drifted evidence")
            cited.append((reference, item))
        if (
            state.state is MilestoneState.IN_PROGRESS
            and state.reason_code is not MilestoneReasonCode.PROGRESS_OBSERVED
        ):
            _fail("STATE_REASON_MISMATCH", "in-progress state must use PROGRESS_OBSERVED")
        if state.state not in {MilestoneState.SATISFIED, MilestoneState.VIOLATED}:
            continue
        decisive_relation = (
            MilestoneEvidenceRelation.SUPPORTS_STATE
            if state.state is MilestoneState.SATISFIED
            else MilestoneEvidenceRelation.REFUTES_STATE
        )
        conflicting_relation = (
            MilestoneEvidenceRelation.REFUTES_STATE
            if state.state is MilestoneState.SATISFIED
            else MilestoneEvidenceRelation.SUPPORTS_STATE
        )
        if any(reference.relation is conflicting_relation for reference, _ in cited):
            _fail(
                "CONFLICTING_DECISIVE_EVIDENCE",
                "conflicting support/refutation must yield unknown",
            )
        decisive = tuple(
            (reference, item)
            for reference, item in cited
            if reference.relation is decisive_relation and item.role not in _WEAK_EVIDENCE_ROLES
        )
        if not decisive:
            _fail(
                "WEAK_EVIDENCE_ONLY",
                "transition status cannot establish a decisive milestone state",
            )
        current_reason = (
            MilestoneReasonCode.CURRENT_GUI_SUPPORT
            if state.state is MilestoneState.SATISFIED
            else MilestoneReasonCode.CURRENT_GUI_REFUTATION
        )
        transition_reason = (
            MilestoneReasonCode.COMPLETED_TRANSITION_SUPPORT
            if state.state is MilestoneState.SATISFIED
            else MilestoneReasonCode.COMPLETED_TRANSITION_REFUTATION
        )
        if state.reason_code is current_reason:
            if not any(item.role in _CURRENT_ROLES for _, item in decisive):
                _fail(
                    "STATE_REASON_MISMATCH",
                    "current-GUI reason requires decisive current-GUI evidence",
                )
        elif state.reason_code is transition_reason:
            if not any(item.role not in _CURRENT_ROLES for _, item in decisive):
                _fail(
                    "STATE_REASON_MISMATCH",
                    "transition reason requires decisive prior-transition evidence",
                )
        else:
            _fail(
                "STATE_REASON_MISMATCH",
                "decisive milestone state uses an incompatible reason code",
            )


def derive_path_states_and_frontier(
    rubric: MultiPathRubricV1,
    milestone_states: tuple[MilestoneStateRecordV1, ...],
) -> tuple[tuple[PathStateV1, ...], tuple[FrontierItemV1, ...]]:
    """Derive the only admitted path/frontier view in bounded DAG time."""

    _require_exact(rubric, MultiPathRubricV1, "rubric")
    _require_tuple(
        milestone_states,
        MilestoneStateRecordV1,
        "milestone_states",
        minimum=1,
        maximum=512,
    )
    expected_milestone_ids = {value.milestone_id for value in rubric.milestones}
    if {value.milestone_id for value in milestone_states} != expected_milestone_ids:
        _fail("MILESTONE_CENSUS_MISMATCH", "state needs one entry per milestone")

    milestone_state = {value.milestone_id: value.state for value in milestone_states}
    milestones = {value.milestone_id: value for value in rubric.milestones}
    gates = {value.gate_id: value for value in rubric.gates}
    viability_cache: dict[tuple[GraphRefKind, str], PathViability] = {}
    satisfied_cache: dict[tuple[GraphRefKind, str], bool] = {}
    blocking_cache: dict[tuple[GraphRefKind, str], bool] = {}

    def reference_key(reference: GraphRefV1) -> tuple[GraphRefKind, str]:
        return (reference.ref_kind, reference.ref_id)

    def combine_and(values: tuple[PathViability, ...]) -> PathViability:
        if any(value is PathViability.INACTIVE for value in values):
            return PathViability.INACTIVE
        if any(value is PathViability.UNKNOWN for value in values):
            return PathViability.UNKNOWN
        return PathViability.VIABLE

    def contains_blocking(reference: GraphRefV1) -> bool:
        key = reference_key(reference)
        cached = blocking_cache.get(key)
        if cached is not None:
            return cached
        if reference.ref_kind is GraphRefKind.MILESTONE:
            result = milestones[reference.ref_id].blocking
        else:
            result = any(contains_blocking(child) for child in gates[reference.ref_id].children)
        blocking_cache[key] = result
        return result

    def evaluate(reference: GraphRefV1) -> PathViability:
        key = reference_key(reference)
        cached = viability_cache.get(key)
        if cached is not None:
            return cached
        if reference.ref_kind is GraphRefKind.MILESTONE:
            milestone = milestones[reference.ref_id]
            state = milestone_state[reference.ref_id]
            if not milestone.blocking:
                result = PathViability.VIABLE
            elif state is MilestoneState.VIOLATED:
                result = PathViability.INACTIVE
            elif state is MilestoneState.UNKNOWN:
                result = PathViability.UNKNOWN
            else:
                result = PathViability.VIABLE
        else:
            gate = gates[reference.ref_id]
            if gate.operator is GateOperator.AND:
                children = tuple(evaluate(value) for value in gate.children)
                result = combine_and(children)
            else:
                blocking_children = tuple(
                    value for value in gate.children if contains_blocking(value)
                )
                relevant_children = blocking_children or gate.children
                children = tuple(evaluate(value) for value in relevant_children)
                if any(value is PathViability.VIABLE for value in children):
                    result = PathViability.VIABLE
                elif any(value is PathViability.UNKNOWN for value in children):
                    result = PathViability.UNKNOWN
                else:
                    result = PathViability.INACTIVE
        viability_cache[key] = result
        return result

    def is_satisfied(reference: GraphRefV1) -> bool:
        key = reference_key(reference)
        cached = satisfied_cache.get(key)
        if cached is not None:
            return cached
        if reference.ref_kind is GraphRefKind.MILESTONE:
            result = milestone_state[reference.ref_id] is MilestoneState.SATISFIED
        else:
            gate = gates[reference.ref_id]
            if gate.operator is GateOperator.AND:
                result = all(is_satisfied(child) for child in gate.children)
            else:
                blocking_children = tuple(
                    value for value in gate.children if contains_blocking(value)
                )
                relevant_children = blocking_children or gate.children
                result = any(is_satisfied(child) for child in relevant_children)
        satisfied_cache[key] = result
        return result

    path_values: list[PathStateV1] = []
    for path in rubric.paths:
        if path.kind is PathKind.OTHER_UNKNOWN:
            value = PathViability.UNKNOWN
        else:
            assert path.root is not None
            roots = (path.root,) if rubric.common_root is None else (rubric.common_root, path.root)
            value = combine_and(tuple(evaluate(root) for root in roots))
        path_values.append(PathStateV1(path_id=path.path_id, state=value))
    path_states = tuple(path_values)
    path_lookup = {value.path_id: value.state for value in path_states}

    frontier: list[FrontierItemV1] = []
    seen_frontier: set[tuple[str, str]] = set()
    visited: set[tuple[str, GraphRefKind, str]] = set()

    def collect(path_id: str, reference: GraphRefV1) -> None:
        visit_key = (path_id, reference.ref_kind, reference.ref_id)
        if visit_key in visited:
            return
        visited.add(visit_key)
        if evaluate(reference) is PathViability.INACTIVE or is_satisfied(reference):
            return
        if reference.ref_kind is GraphRefKind.MILESTONE:
            state = milestone_state[reference.ref_id]
            if state in {
                MilestoneState.PENDING,
                MilestoneState.IN_PROGRESS,
                MilestoneState.UNKNOWN,
            }:
                key = (path_id, reference.ref_id)
                if key not in seen_frontier:
                    seen_frontier.add(key)
                    frontier.append(FrontierItemV1(path_id=path_id, milestone_id=reference.ref_id))
            return
        for child in gates[reference.ref_id].children:
            collect(path_id, child)

    for path in rubric.paths:
        if (
            path.kind is PathKind.OTHER_UNKNOWN
            or path_lookup[path.path_id] is PathViability.INACTIVE
        ):
            continue
        if rubric.common_root is not None:
            collect(path.path_id, rubric.common_root)
        assert path.root is not None
        collect(path.path_id, path.root)
    if len(frontier) > 4096:
        _fail("FRONTIER_LIMIT_EXCEEDED", "derived frontier exceeds the contract bound")
    return path_states, tuple(frontier)


def validate_tracking_state(state: RubricTrackingStateV1, rubric: MultiPathRubricV1) -> None:
    _require_exact(state, RubricTrackingStateV1, "tracking state")
    _require_exact(rubric, MultiPathRubricV1, "rubric")
    _require_rubric_binding(state.rubric_binding, rubric)
    if {value.milestone_id for value in state.milestone_states} != {
        value.milestone_id for value in rubric.milestones
    }:
        _fail("MILESTONE_CENSUS_MISMATCH", "state needs one entry per milestone")
    paths = {value.path_id: value for value in rubric.paths}
    if {value.path_id for value in state.path_states} != set(paths):
        _fail("PATH_CENSUS_MISMATCH", "state needs one entry per path")
    expected_path_states, expected_frontier = derive_path_states_and_frontier(
        rubric,
        state.milestone_states,
    )
    if state.path_states != expected_path_states:
        _fail(
            "PATH_STATE_DERIVATION_MISMATCH",
            "path states must be recomputed from admitted milestone state",
        )
    if state.frontier != expected_frontier:
        _fail(
            "FRONTIER_DERIVATION_MISMATCH",
            "frontier must be recomputed from admitted milestone state",
        )
    path_states = {value.path_id: value.state for value in state.path_states}
    for path in rubric.paths:
        if (
            path.kind is PathKind.OTHER_UNKNOWN
            and path_states[path.path_id] is not PathViability.UNKNOWN
        ):
            _fail("OTHER_PATH_FORCED", "OTHER/unknown path must remain unknown")
    milestone_ids = {value.milestone_id for value in rubric.milestones}
    for item in state.frontier:
        if item.path_id not in paths or item.milestone_id not in milestone_ids:
            _fail("UNKNOWN_FRONTIER_REFERENCE", "frontier references an unknown path/milestone")
        if path_states[item.path_id] is PathViability.INACTIVE:
            _fail("INACTIVE_PATH_FRONTIER", "inactive path cannot contribute a frontier")
    expected_actor_visible = derive_actor_visible_rubric_state(
        enabled=state.actor_visible.enabled,
        milestone_states=state.milestone_states,
        path_states=state.path_states,
    )
    if state.actor_visible != expected_actor_visible:
        _fail(
            "ACTOR_VISIBLE_PROJECTION_MISMATCH",
            "actor-visible state must equal the module-owned status-only projection",
        )


def validate_path_relevance_output(
    output: PathRelevanceOutputV1,
    state: RubricTrackingStateV1,
    rubric: MultiPathRubricV1,
    record_bindings: tuple[RecordPathBindingV1, ...],
    supported_records: tuple[SupportedRecordBindingV1, ...],
) -> None:
    _require_exact(output, PathRelevanceOutputV1, "path relevance output")
    validate_tracking_state(state, rubric)
    _require_tuple(record_bindings, RecordPathBindingV1, "record_bindings", maximum=512)
    _require_tuple(supported_records, SupportedRecordBindingV1, "supported_records", maximum=512)
    if output.rubric_state_sha256 != rubric_tracking_state_sha256(state):
        _fail("STATE_BINDING_MISMATCH", "relevance output binds a different rubric state")
    if output.topology != state.topology:
        _fail("TOPOLOGY_BINDING_MISMATCH", "relevance output changed the state topology")
    if state.logical_call_id is None or output.logical_call_id != state.logical_call_id:
        _fail(
            "LOGICAL_CALL_BINDING_MISMATCH",
            "relevance output must bind the state-producing logical call",
        )
    bindings = {value.record_id: value for value in record_bindings}
    supported_ids = {value.record_id for value in supported_records}
    if len(bindings) != len(record_bindings) or len(supported_ids) != len(supported_records):
        _fail("DUPLICATE_ID", "record bindings must be unique")
    if {value.record_id for value in output.records} != set(bindings):
        _fail("RECORD_CENSUS_MISMATCH", "relevance output needs one result per record")
    path_states = {value.path_id: value.state for value in state.path_states}
    legal_path_ids = {
        value.path_id for value in rubric.paths if value.kind is PathKind.LEGAL_ALTERNATIVE
    }
    for result in output.records:
        binding = bindings[result.record_id]
        if result.linked_path_ids != binding.linked_path_ids:
            _fail("PATH_BINDING_MISMATCH", "relevance result changed record-path binding")
        if not set(binding.linked_path_ids) <= legal_path_ids:
            _fail("UNKNOWN_PATH_BINDING", "record binds an unknown/non-legal path")
        if binding.path_independent:
            expected = RecordRelevance.PATH_INDEPENDENT
        elif not binding.linked_path_ids:
            expected = RecordRelevance.UNKNOWN
        elif any(
            path_states[path_id] is PathViability.VIABLE for path_id in binding.linked_path_ids
        ):
            expected = RecordRelevance.ACTIVE_PATH
        elif any(
            path_states[path_id] is PathViability.UNKNOWN for path_id in binding.linked_path_ids
        ):
            expected = RecordRelevance.UNKNOWN
        else:
            expected = RecordRelevance.INACTIVE_BRANCH
        if result.relevance is not expected:
            _fail("RELEVANCE_DERIVATION_MISMATCH", "record relevance is not graph-derived")
        if (
            result.supported_record_binding_sha256 is not None
            or result.disposition is not RelevanceDisposition.RETAIN
        ):
            _fail(
                "R22_SUPPORT_RESOLVER_REQUIRED",
                "R2.3 cannot admit a support binding or ARCHIVE without a trusted R2.2 resolver",
            )


_PROJECTED_ENUM_TYPES = {
    InstructionSpanRole,
    MilestoneKind,
    MilestonePredicateKind,
    GateOperator,
    GraphRefKind,
    PathKind,
    RevisionKind,
    RevisionReason,
    RequirementDeltaKind,
    TopologyKind,
    RubricExecutionScope,
    RubricBackendKind,
    RubricTransportAuthority,
    RubricSourceEventType,
    RubricEvidenceRole,
    EvidenceProjectionKind,
    EvidenceMediaType,
    MilestoneState,
    MilestoneEvidenceRelation,
    MilestoneReasonCode,
    TrackerProposalStatus,
    TrackerOutputKind,
    PathViability,
    RecordRelevance,
    RelevanceDisposition,
    ExternalValidityVerdict,
    ExternalValidityOperation,
    TopologyRunStatus,
    TopologyDeploymentDecision,
}
_PROJECTED_DATACLASS_TYPES = {
    RubricAuthorityV1,
    TaskInstructionV1,
    InstructionSpanV1,
    MilestoneV1,
    GraphRefV1,
    GateV1,
    RubricPathV1,
    RequirementDeltaV1,
    RubricRevisionV1,
    RubricBackendDescriptorV1,
    MultiPathRubricV1,
    TaskStartRubricRequestV1,
    RubricRevisionRequestV1,
    RubricBindingV1,
    TopologyDeclarationV1,
    RubricCutoffV1,
    TextEvidenceProjectionV1,
    ImageEvidenceProjectionV1,
    RubricEvidenceV1,
    CurrentObservationBindingV1,
    TrackingInputExclusionsV1,
    MilestoneEvidenceRefV1,
    MilestoneStateRecordV1,
    PathStateV1,
    FrontierItemV1,
    ActorVisibleRubricStateV1,
    RubricTrackingStateV1,
    RubricTrackingPacketV1,
    RubricTrackerProposalV1,
    SupportedRecordBindingV1,
    RecordPathBindingV1,
    RecordRelevanceResultV1,
    PathRelevanceOutputV1,
    TopologyRunV1,
    TopologyComparisonV1,
}


class _TrustedGraphBudget:
    """Active-path cycle detector plus deterministic traversal budgets."""

    __slots__ = ("active_ids", "node_count")

    def __init__(self) -> None:
        self.active_ids: set[int] = set()
        self.node_count = 0

    def visit(self, *, depth: int) -> None:
        if depth > TRUSTED_GRAPH_MAX_DEPTH:
            _fail(
                "TRUSTED_GRAPH_DEPTH_EXCEEDED",
                "trusted graph exceeds the maximum nesting depth",
            )
        self.node_count += 1
        if self.node_count > TRUSTED_GRAPH_MAX_NODES:
            _fail(
                "TRUSTED_GRAPH_NODE_LIMIT_EXCEEDED",
                "trusted graph exceeds the maximum node count",
            )

    def enter(self, value: object) -> int:
        identity = id(value)
        if identity in self.active_ids:
            _fail("TRUSTED_GRAPH_CYCLE", "trusted graph contains a cycle")
        self.active_ids.add(identity)
        return identity

    def leave(self, identity: int) -> None:
        self.active_ids.remove(identity)


def _project_trusted_graph(
    value: object,
    *,
    budget: _TrustedGraphBudget,
    depth: int,
) -> JsonValue:
    budget.visit(depth=depth)
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonValue, value)
    if type(value) in _PROJECTED_ENUM_TYPES:
        return cast(StrEnum, value).value
    if type(value) is tuple:
        identity = budget.enter(value)
        try:
            return [
                _project_trusted_graph(item, budget=budget, depth=depth + 1)
                for item in cast(tuple[object, ...], value)
            ]
        finally:
            budget.leave(identity)
    if type(value) in _PROJECTED_DATACLASS_TYPES:
        identity = budget.enter(value)
        try:
            field_names = cast(dict[str, object], getattr(type(value), "__dataclass_fields__"))
            return {
                name: _project_trusted_graph(getattr(value, name), budget=budget, depth=depth + 1)
                for name in field_names
            }
        finally:
            budget.leave(identity)
    _fail("UNTRUSTED_TYPE", "value has no R2.3 canonical projection")


def _trusted_projection(value: object) -> JsonValue:
    """Build one bounded canonical view without virtual serialization."""

    try:
        return _project_trusted_graph(value, budget=_TrustedGraphBudget(), depth=0)
    except RecursionError:
        _fail(
            "TRUSTED_GRAPH_RECURSION_LIMIT",
            "trusted graph exceeded the interpreter recursion limit",
        )


def _snapshot_trusted_graph(
    value: object,
    *,
    budget: _TrustedGraphBudget,
    depth: int,
) -> object:
    budget.visit(depth=depth)
    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) in _PROJECTED_ENUM_TYPES:
        return value
    if type(value) is tuple:
        identity = budget.enter(value)
        try:
            return tuple(
                _snapshot_trusted_graph(item, budget=budget, depth=depth + 1)
                for item in cast(tuple[object, ...], value)
            )
        finally:
            budget.leave(identity)
    if type(value) in _PROJECTED_DATACLASS_TYPES:
        identity = budget.enter(value)
        try:
            field_names = cast(dict[str, object], getattr(type(value), "__dataclass_fields__"))
            constructor = cast(Callable[..., object], type(value))
            return constructor(
                **{
                    name: _snapshot_trusted_graph(
                        getattr(value, name), budget=budget, depth=depth + 1
                    )
                    for name in field_names
                }
            )
        finally:
            budget.leave(identity)
    _fail("UNTRUSTED_TYPE", "value has no R2.3 trusted snapshot")


def _trusted_snapshot(value: object) -> object:
    """Rebuild one bounded detached graph without virtual serialization."""

    try:
        return _snapshot_trusted_graph(value, budget=_TrustedGraphBudget(), depth=0)
    except RecursionError:
        _fail(
            "TRUSTED_GRAPH_RECURSION_LIMIT",
            "trusted graph exceeded the interpreter recursion limit",
        )


def _typed_snapshot(value: object, expected: type[object], name: str) -> object:
    _require_exact(value, expected, name)
    snapshot = _trusted_snapshot(value)
    assert type(snapshot) is expected
    return snapshot


def snapshot_multi_path_rubric(value: MultiPathRubricV1) -> MultiPathRubricV1:
    return cast(
        MultiPathRubricV1,
        _typed_snapshot(value, MultiPathRubricV1, "rubric"),
    )


def snapshot_tracker_proposal(value: RubricTrackerProposalV1) -> RubricTrackerProposalV1:
    return cast(
        RubricTrackerProposalV1,
        _typed_snapshot(value, RubricTrackerProposalV1, "tracker proposal"),
    )


def snapshot_task_instruction(value: TaskInstructionV1) -> TaskInstructionV1:
    return cast(
        TaskInstructionV1,
        _typed_snapshot(value, TaskInstructionV1, "task instruction"),
    )


def snapshot_backend_descriptor(
    value: RubricBackendDescriptorV1,
) -> RubricBackendDescriptorV1:
    return cast(
        RubricBackendDescriptorV1,
        _typed_snapshot(value, RubricBackendDescriptorV1, "backend descriptor"),
    )


def snapshot_task_start_request(
    value: TaskStartRubricRequestV1,
) -> TaskStartRubricRequestV1:
    return cast(
        TaskStartRubricRequestV1,
        _typed_snapshot(value, TaskStartRubricRequestV1, "task-start request"),
    )


def snapshot_revision_request(
    value: RubricRevisionRequestV1,
) -> RubricRevisionRequestV1:
    return cast(
        RubricRevisionRequestV1,
        _typed_snapshot(value, RubricRevisionRequestV1, "revision request"),
    )


def snapshot_tracking_state(
    value: RubricTrackingStateV1,
) -> RubricTrackingStateV1:
    return cast(
        RubricTrackingStateV1,
        _typed_snapshot(value, RubricTrackingStateV1, "tracking state"),
    )


def snapshot_tracking_packet(
    value: RubricTrackingPacketV1,
) -> RubricTrackingPacketV1:
    return cast(
        RubricTrackingPacketV1,
        _typed_snapshot(value, RubricTrackingPacketV1, "tracking packet"),
    )


def snapshot_path_relevance_output(
    value: PathRelevanceOutputV1,
) -> PathRelevanceOutputV1:
    return cast(
        PathRelevanceOutputV1,
        _typed_snapshot(value, PathRelevanceOutputV1, "path relevance output"),
    )


def snapshot_record_path_binding(value: RecordPathBindingV1) -> RecordPathBindingV1:
    return cast(
        RecordPathBindingV1,
        _typed_snapshot(value, RecordPathBindingV1, "record path binding"),
    )


def snapshot_supported_record_binding(
    value: SupportedRecordBindingV1,
) -> SupportedRecordBindingV1:
    return cast(
        SupportedRecordBindingV1,
        _typed_snapshot(value, SupportedRecordBindingV1, "supported record binding"),
    )


def _typed_projection(value: object, expected: type[object]) -> dict[str, JsonValue]:
    _require_exact(value, expected, expected.__name__)
    projected = _trusted_projection(value)
    assert type(projected) is dict
    return projected


def task_instruction_projection(value: TaskInstructionV1) -> dict[str, JsonValue]:
    return _typed_projection(value, TaskInstructionV1)


def multi_path_rubric_projection(value: MultiPathRubricV1) -> dict[str, JsonValue]:
    return _typed_projection(value, MultiPathRubricV1)


def task_start_request_projection(value: TaskStartRubricRequestV1) -> dict[str, JsonValue]:
    return _typed_projection(value, TaskStartRubricRequestV1)


def rubric_revision_request_projection(
    value: RubricRevisionRequestV1,
) -> dict[str, JsonValue]:
    return _typed_projection(value, RubricRevisionRequestV1)


def tracking_packet_projection(value: RubricTrackingPacketV1) -> dict[str, JsonValue]:
    return _typed_projection(value, RubricTrackingPacketV1)


def tracker_proposal_projection(value: RubricTrackerProposalV1) -> dict[str, JsonValue]:
    return _typed_projection(value, RubricTrackerProposalV1)


def rubric_tracking_state_projection(
    value: RubricTrackingStateV1,
) -> dict[str, JsonValue]:
    return _typed_projection(value, RubricTrackingStateV1)


def supported_record_binding_projection(
    value: SupportedRecordBindingV1,
) -> dict[str, JsonValue]:
    return _typed_projection(value, SupportedRecordBindingV1)


def path_relevance_output_projection(
    value: PathRelevanceOutputV1,
) -> dict[str, JsonValue]:
    return _typed_projection(value, PathRelevanceOutputV1)


def topology_comparison_projection(value: TopologyComparisonV1) -> dict[str, JsonValue]:
    return _typed_projection(value, TopologyComparisonV1)


def _projection_sha256(value: object) -> str:
    return _canonical_sha256(_trusted_projection(value))


def rubric_sha256(value: MultiPathRubricV1) -> str:
    _require_exact(value, MultiPathRubricV1, "rubric")
    return _projection_sha256(value)


def task_start_request_sha256(value: TaskStartRubricRequestV1) -> str:
    _require_exact(value, TaskStartRubricRequestV1, "task-start request")
    return _projection_sha256(value)


def rubric_revision_request_sha256(value: RubricRevisionRequestV1) -> str:
    _require_exact(value, RubricRevisionRequestV1, "revision request")
    return _projection_sha256(value)


def tracking_packet_sha256(value: RubricTrackingPacketV1) -> str:
    _require_exact(value, RubricTrackingPacketV1, "tracking packet")
    return _projection_sha256(value)


def tracker_proposal_sha256(value: RubricTrackerProposalV1) -> str:
    _require_exact(value, RubricTrackerProposalV1, "tracker proposal")
    return _projection_sha256(value)


def rubric_tracking_state_sha256(value: RubricTrackingStateV1) -> str:
    _require_exact(value, RubricTrackingStateV1, "rubric tracking state")
    return _projection_sha256(value)


def supported_record_binding_sha256(value: SupportedRecordBindingV1) -> str:
    _require_exact(value, SupportedRecordBindingV1, "supported record binding")
    return _projection_sha256(value)


def path_relevance_output_sha256(value: PathRelevanceOutputV1) -> str:
    _require_exact(value, PathRelevanceOutputV1, "path relevance output")
    return _projection_sha256(value)


def topology_comparison_sha256(value: TopologyComparisonV1) -> str:
    _require_exact(value, TopologyComparisonV1, "topology comparison")
    return _projection_sha256(value)


def rubric_binding(value: MultiPathRubricV1) -> RubricBindingV1:
    _require_exact(value, MultiPathRubricV1, "rubric")
    return RubricBindingV1(
        rubric_id=value.rubric_id,
        rubric_version=value.rubric_version,
        rubric_sha256=rubric_sha256(value),
    )


def _requirement_key(task: TaskInstructionV1, span: InstructionSpanV1) -> str:
    return _canonical_sha256(
        {
            "task_text_sha256": task.text_sha256,
            "role": span.role.value,
            "char_start": span.char_start,
            "char_end": span.char_end,
            "utf8_byte_start": span.utf8_byte_start,
            "utf8_byte_end": span.utf8_byte_end,
            "span_sha256": span.span_sha256,
        }
    )


def _requirement_inventory(value: MultiPathRubricV1) -> dict[str, InstructionSpanV1]:
    return {_requirement_key(value.task, span): span for span in value.instruction_spans}


def _node_hashes(value: MultiPathRubricV1) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for milestone in value.milestones:
        hashes[f"milestone:{milestone.milestone_id}"] = _projection_sha256(milestone)
    for gate in value.gates:
        hashes[f"gate:{gate.gate_id}"] = _projection_sha256(gate)
    for path in value.paths:
        hashes[f"path:{path.path_id}"] = _projection_sha256(path)
    if value.common_root is not None:
        hashes["common-root"] = _projection_sha256(value.common_root)
    return hashes


__all__ = [
    "ActorVisibleRubricStateV1",
    "CurrentObservationBindingV1",
    "EvidenceMediaType",
    "EvidenceProjectionKind",
    "ExternalValidityOperation",
    "ExternalValidityVerdict",
    "FrontierItemV1",
    "GateOperator",
    "GateV1",
    "GraphRefKind",
    "GraphRefV1",
    "ImageEvidenceProjectionV1",
    "InstructionSpanRole",
    "InstructionSpanV1",
    "MilestoneEvidenceRefV1",
    "MilestoneEvidenceRelation",
    "MilestoneKind",
    "MilestonePredicateKind",
    "MilestoneReasonCode",
    "MilestoneState",
    "MilestoneStateRecordV1",
    "MilestoneV1",
    "MultiPathRubricV1",
    "PathKind",
    "PathRelevanceInterfaceV1",
    "PathRelevanceOutputV1",
    "PathStateV1",
    "PathViability",
    "R23ContractError",
    "RecordPathBindingV1",
    "RecordRelevance",
    "RecordRelevanceResultV1",
    "RelevanceDisposition",
    "RequirementDeltaKind",
    "RequirementDeltaV1",
    "RevisionKind",
    "RevisionReason",
    "RubricAuthorityV1",
    "RubricBackendDescriptorV1",
    "RubricBackendKind",
    "RubricBindingV1",
    "RubricBuilderBackendV1",
    "RubricCutoffV1",
    "RubricEvidenceRole",
    "RubricEvidenceV1",
    "RubricExecutionControlV1",
    "RubricExecutionScope",
    "RubricPathV1",
    "RubricRevisionRequestV1",
    "RubricRevisionV1",
    "RubricSourceEventType",
    "RubricTrackerBackendV1",
    "RubricTrackerProposalV1",
    "RubricTrackingPacketV1",
    "RubricTrackingStateV1",
    "RubricTransportAuthority",
    "SupportedRecordBindingV1",
    "TaskInstructionV1",
    "TaskStartRubricRequestV1",
    "TRUSTED_GRAPH_MAX_DEPTH",
    "TRUSTED_GRAPH_MAX_NODES",
    "TextEvidenceProjectionV1",
    "TopologyComparisonV1",
    "TopologyDeclarationV1",
    "TopologyDeploymentDecision",
    "TopologyKind",
    "TopologyRunStatus",
    "TopologyRunV1",
    "TrackerOutputKind",
    "TrackerProposalStatus",
    "TrackingInputExclusionsV1",
    "derive_actor_visible_rubric_state",
    "derive_path_states_and_frontier",
    "multi_path_rubric_projection",
    "path_relevance_output_projection",
    "path_relevance_output_sha256",
    "rubric_binding",
    "rubric_revision_request_projection",
    "rubric_revision_request_sha256",
    "rubric_sha256",
    "rubric_tracking_state_projection",
    "rubric_tracking_state_sha256",
    "snapshot_multi_path_rubric",
    "snapshot_backend_descriptor",
    "snapshot_path_relevance_output",
    "snapshot_record_path_binding",
    "snapshot_revision_request",
    "snapshot_supported_record_binding",
    "snapshot_task_instruction",
    "snapshot_task_start_request",
    "snapshot_tracking_packet",
    "snapshot_tracking_state",
    "snapshot_tracker_proposal",
    "supported_record_binding_projection",
    "supported_record_binding_sha256",
    "task_instruction_projection",
    "task_start_request_projection",
    "task_start_request_sha256",
    "topology_comparison_projection",
    "topology_comparison_sha256",
    "tracker_proposal_projection",
    "tracker_proposal_sha256",
    "tracking_packet_projection",
    "tracking_packet_sha256",
    "validate_path_relevance_output",
    "validate_rubric_revision",
    "validate_tracker_proposal",
    "validate_tracking_packet",
    "validate_tracking_state",
]
