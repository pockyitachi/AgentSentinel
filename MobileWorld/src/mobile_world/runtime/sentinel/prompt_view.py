"""Model-agnostic, evidence-only execution-state prompt augmentation.

This module does not inspect a target model, choose an action, or accept free-form
policy prose.  It renders one closed, locally generated execution-state view at a
registered host/History-Codec boundary, immediately before the current image.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, NoReturn, cast

from mobile_world.offline.causal_replay.contracts import (
    CorrectionAnchor,
    CorrectionContextKind,
    CorrectionPlacement,
    HistoryIR,
    HistoryRecord,
    JsonPath,
    JsonValue,
    PortableContractError,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
    get_at_path,
)
from mobile_world.offline.causal_replay.core import validate_history_ir

if TYPE_CHECKING:
    from mobile_world.runtime.sentinel.r2_4.contracts import RuntimeVerticalAdmittedPlanV1
    from mobile_world.runtime.sentinel.r2_4.renderer import RuntimeVerticalRenderResultV1


EXECUTION_STATE_VIEW_SCHEMA_VERSION = "mobileworld.runtime.sentinel.execution-state-view/v1"
PROMPT_VIEW_ADAPTER_SCHEMA_VERSION = "mobileworld.runtime.sentinel.prompt-view-adapter/v1"
PROMPT_VIEW_RENDER_RESULT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel.prompt-view-render-result/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_MAX_RECENT_ATTEMPTS = 32
_MAX_REPEAT_CLUSTERS = 16
_MAX_SOURCE_EVENTS = 1_000_000
_MAX_COORDINATE = 1_000_000_000
_MAX_DURATION_MS = 86_400_000
_VALIDATION_CHECKS = (
    "PROMPT_VIEW_REGISTERED_HOST_CODEC_BOUND",
    "PROMPT_VIEW_SOURCE_IR_AND_STATE_BOUND",
    "PROMPT_VIEW_HISTORY_CANDIDATE_REVALIDATED",
    "PROMPT_VIEW_UNIQUE_CURRENT_IMAGE_ANCHOR",
    "PROMPT_VIEW_FIXED_LOCAL_TEMPLATE_ONLY",
    "PROMPT_VIEW_INSERTED_BEFORE_CURRENT_IMAGE",
    "PROMPT_VIEW_CURRENT_IMAGE_BYTES_PRESERVED",
    "PROMPT_VIEW_ALL_EXISTING_BYTES_AND_ORDER_PRESERVED",
    "PROMPT_VIEW_INSERTION_REVERSIBLE",
    "PROMPT_VIEW_CALLER_INPUT_IMMUTABLE",
)
_HISTORY_PROOF_SEAL = object()


class PromptViewError(ValueError):
    """Typed failure that never authorizes provider transport."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.provider_invocation_allowed = False
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> NoReturn:
    raise PromptViewError(code, message)


def _require_safe_id(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        _fail("INVALID_PROMPT_VIEW_ID", f"{name} must be a bounded safe identifier")
    return value


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("INVALID_PROMPT_VIEW_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _require_coordinate(value: object, name: str) -> tuple[int, int] | None:
    if value is None:
        return None
    if (
        type(value) is not tuple
        or len(value) != 2
        or any(type(item) is not int or not 0 <= item <= _MAX_COORDINATE for item in value)
    ):
        _fail("INVALID_ACTION_PROJECTION", f"{name} must be a bounded integer pair")
    return cast(tuple[int, int], value)


def _require_path(value: object, name: str) -> JsonPath:
    if (
        type(value) is not tuple
        or not value
        or any(
            (type(token) is not str and type(token) is not int)
            or (type(token) is str and not token)
            or (type(token) is int and token < 0)
            for token in value
        )
    ):
        _fail("INVALID_PROMPT_VIEW_PATH", f"{name} is not a canonical JSON path")
    return cast(JsonPath, value)


def _parse_canonical_bytes(value: bytes, name: str) -> JsonValue:
    if type(value) is not bytes:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", f"{name} must be immutable canonical bytes")
    try:
        decoded = cast(JsonValue, json.loads(value))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise PromptViewError("NON_CANONICAL_JSON", f"{name} is malformed") from exc
    if canonical_json_bytes(decoded) != value:
        _fail("NON_CANONICAL_JSON", f"{name} is not canonical JSON")
    return decoded


class ExecutionKindV1(StrEnum):
    MOBILE_ACTION = "MOBILE_ACTION"
    TERMINAL_CONTROL = "TERMINAL_CONTROL"
    USER_INTERACTION = "USER_INTERACTION"
    TOOL_ACTION = "TOOL_ACTION"
    UNKNOWN = "UNKNOWN"


class ActionKindV1(StrEnum):
    CLICK = "CLICK"
    LONG_PRESS = "LONG_PRESS"
    DOUBLE_TAP = "DOUBLE_TAP"
    SWIPE = "SWIPE"
    DRAG = "DRAG"
    TYPE = "TYPE"
    INPUT_TEXT = "INPUT_TEXT"
    OPEN_APP = "OPEN_APP"
    SYSTEM_BUTTON = "SYSTEM_BUTTON"
    NAVIGATE_BACK = "NAVIGATE_BACK"
    NAVIGATE_HOME = "NAVIGATE_HOME"
    KEYBOARD_ENTER = "KEYBOARD_ENTER"
    SCROLL = "SCROLL"
    WAIT = "WAIT"
    ANSWER = "ANSWER"
    ASK_USER = "ASK_USER"
    TERMINATE = "TERMINATE"
    STATUS = "STATUS"
    MCP = "MCP"
    UNKNOWN = "UNKNOWN"


class ActionDirectionV1(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class SystemButtonV1(StrEnum):
    BACK = "BACK"
    HOME = "HOME"
    MENU = "MENU"
    ENTER = "ENTER"
    OTHER = "OTHER"


class TerminalStatusV1(StrEnum):
    EXECUTOR_RETURNED = "EXECUTOR_RETURNED"
    EXECUTOR_RAISED = "EXECUTOR_RAISED"
    NOT_DISPATCHED = "NOT_DISPATCHED"
    UNKNOWN = "UNKNOWN"


class VisibleDeltaV1(StrEnum):
    SCREEN_PIXELS_EXACTLY_SAME = "SCREEN_PIXELS_EXACTLY_SAME"
    SCREEN_PIXELS_DIFFERENT = "SCREEN_PIXELS_DIFFERENT"
    UNKNOWN = "UNKNOWN"


class SemanticOutcomeV1(StrEnum):
    UNKNOWN = "UNKNOWN"


class RepeatClassificationV1(StrEnum):
    EXACT_ACTION_REPEAT = "EXACT_ACTION_REPEAT"
    EXACT_ACTION_AND_SCREEN_REPEAT = "EXACT_ACTION_AND_SCREEN_REPEAT"


class RubricStateChangeV1(StrEnum):
    CHANGED = "CHANGED"
    UNCHANGED = "UNCHANGED"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class EvidenceGapV1(StrEnum):
    TASK_COMPLETION_UNVERIFIED = "TASK_COMPLETION_UNVERIFIED"
    SEMANTIC_OUTCOME_UNVERIFIED = "SEMANTIC_OUTCOME_UNVERIFIED"
    VISIBLE_DELTA_UNKNOWN = "VISIBLE_DELTA_UNKNOWN"
    REPEATED_ATTEMPT_OUTCOME_UNKNOWN = "REPEATED_ATTEMPT_OUTCOME_UNKNOWN"
    RUBRIC_STATE_UNRESOLVED = "RUBRIC_STATE_UNRESOLVED"
    RUBRIC_UNAVAILABLE = "RUBRIC_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ActionProjectionV1:
    """Closed action facts; actor-provided free-text values are intentionally absent."""

    action_kind: ActionKindV1
    coordinate: tuple[int, int] | None = None
    coordinate2: tuple[int, int] | None = None
    direction: ActionDirectionV1 | None = None
    button: SystemButtonV1 | None = None
    duration_ms: int | None = None
    sensitive_value_present: bool = False

    def __post_init__(self) -> None:
        if type(self.action_kind) is not ActionKindV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "action_kind must use ActionKindV1")
        _require_coordinate(self.coordinate, "coordinate")
        _require_coordinate(self.coordinate2, "coordinate2")
        if self.direction is not None and type(self.direction) is not ActionDirectionV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "direction must use ActionDirectionV1")
        if self.button is not None and type(self.button) is not SystemButtonV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "button must use SystemButtonV1")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or not 0 <= self.duration_ms <= _MAX_DURATION_MS
        ):
            _fail("INVALID_ACTION_PROJECTION", "duration_ms is outside the bounded range")
        if type(self.sensitive_value_present) is not bool:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "sensitive_value_present must be exact bool")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_kind": self.action_kind.value,
            "coordinate": None if self.coordinate is None else list(self.coordinate),
            "coordinate2": None if self.coordinate2 is None else list(self.coordinate2),
            "direction": None if self.direction is None else self.direction.value,
            "button": None if self.button is None else self.button.value,
            "duration_ms": self.duration_ms,
            "sensitive_value_present": self.sensitive_value_present,
        }


@dataclass(frozen=True, slots=True)
class ExecutionAttemptSummaryV1:
    attempt_id: str
    source_event_seq: int
    execution_kind: ExecutionKindV1
    action_sha256: str
    action_projection: ActionProjectionV1
    terminal_status: TerminalStatusV1
    visible_delta: VisibleDeltaV1
    semantic_outcome: SemanticOutcomeV1 = SemanticOutcomeV1.UNKNOWN

    def __post_init__(self) -> None:
        _require_safe_id(self.attempt_id, "attempt_id")
        if type(self.source_event_seq) is not int or self.source_event_seq < 1:
            _fail("INVALID_EXECUTION_ATTEMPT", "source_event_seq must be positive")
        if type(self.execution_kind) is not ExecutionKindV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "execution_kind must use ExecutionKindV1")
        _require_sha256(self.action_sha256, "action_sha256")
        if type(self.action_projection) is not ActionProjectionV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "action_projection has a foreign type")
        if type(self.terminal_status) is not TerminalStatusV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "terminal_status must use TerminalStatusV1")
        if type(self.visible_delta) is not VisibleDeltaV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "visible_delta must use VisibleDeltaV1")
        if (
            type(self.semantic_outcome) is not SemanticOutcomeV1
            or self.semantic_outcome is not SemanticOutcomeV1.UNKNOWN
        ):
            _fail("SEMANTIC_OUTCOME_CLAIM_FORBIDDEN", "semantic outcome must remain UNKNOWN")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "source_event_seq": self.source_event_seq,
            "execution_kind": self.execution_kind.value,
            "action_sha256": self.action_sha256,
            "action_projection": self.action_projection.to_dict(),
            "terminal_status": self.terminal_status.value,
            "visible_delta": self.visible_delta.value,
            "semantic_outcome": self.semantic_outcome.value,
        }


@dataclass(frozen=True, slots=True)
class RepeatClusterV1:
    action_sha256: str
    member_attempt_ids: tuple[str, ...]
    lower_bound: int
    classification: RepeatClassificationV1

    def __post_init__(self) -> None:
        _require_sha256(self.action_sha256, "repeat action_sha256")
        if (
            type(self.member_attempt_ids) is not tuple
            or len(self.member_attempt_ids) < 2
            or any(type(item) is not str for item in self.member_attempt_ids)
        ):
            _fail("INVALID_REPEAT_CLUSTER", "repeat members must be an exact tuple of IDs")
        for item in self.member_attempt_ids:
            _require_safe_id(item, "repeat member attempt_id")
        if len(set(self.member_attempt_ids)) != len(self.member_attempt_ids):
            _fail("INVALID_REPEAT_CLUSTER", "repeat member IDs must be unique")
        if type(self.lower_bound) is not int or self.lower_bound < len(self.member_attempt_ids):
            _fail("INVALID_REPEAT_CLUSTER", "repeat lower_bound is smaller than bound members")
        if type(self.classification) is not RepeatClassificationV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "repeat classification is untrusted")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action_sha256": self.action_sha256,
            "member_attempt_ids": list(self.member_attempt_ids),
            "lower_bound": self.lower_bound,
            "classification": self.classification.value,
        }


@dataclass(frozen=True, slots=True)
class ExecutionStateViewV1:
    logical_call_id: str
    source_request_sha256: str
    cutoff_event_id: str
    cutoff_event_seq: int
    ledger_sha256: str
    source_event_count: int
    source_event_ids_sha256: str
    recent_attempts: tuple[ExecutionAttemptSummaryV1, ...]
    repeat_clusters: tuple[RepeatClusterV1, ...]
    rubric_state_change: RubricStateChangeV1
    evidence_gaps: tuple[EvidenceGapV1, ...]
    omitted_attempt_count: int
    semantic_outcomes_verified: bool = False
    action_recommendation_present: bool = False
    schema_version: str = EXECUTION_STATE_VIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            EXECUTION_STATE_VIEW_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_PROMPT_VIEW_SCHEMA", "unknown execution-state-view schema")
        _require_safe_id(self.logical_call_id, "logical_call_id")
        _require_sha256(self.source_request_sha256, "source_request_sha256")
        _require_safe_id(self.cutoff_event_id, "cutoff_event_id")
        if type(self.cutoff_event_seq) is not int or self.cutoff_event_seq < 1:
            _fail("INVALID_EXECUTION_STATE_VIEW", "cutoff_event_seq must be positive")
        _require_sha256(self.ledger_sha256, "ledger_sha256")
        if (
            type(self.source_event_count) is not int
            or not 0 <= self.source_event_count <= _MAX_SOURCE_EVENTS
        ):
            _fail("INVALID_EXECUTION_STATE_VIEW", "source_event_count is outside bounds")
        _require_sha256(self.source_event_ids_sha256, "source_event_ids_sha256")
        if (
            type(self.recent_attempts) is not tuple
            or len(self.recent_attempts) > _MAX_RECENT_ATTEMPTS
            or any(type(item) is not ExecutionAttemptSummaryV1 for item in self.recent_attempts)
        ):
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "recent_attempts must use exact contracts")
        ordered_attempts = tuple(
            sorted(self.recent_attempts, key=lambda item: (item.source_event_seq, item.attempt_id))
        )
        if self.recent_attempts != ordered_attempts:
            _fail("NON_CANONICAL_EXECUTION_STATE", "recent attempts are not causally ordered")
        attempt_ids = tuple(item.attempt_id for item in self.recent_attempts)
        if len(set(attempt_ids)) != len(attempt_ids):
            _fail("NON_CANONICAL_EXECUTION_STATE", "execution IDs repeat")
        if (
            type(self.repeat_clusters) is not tuple
            or len(self.repeat_clusters) > _MAX_REPEAT_CLUSTERS
            or any(type(item) is not RepeatClusterV1 for item in self.repeat_clusters)
        ):
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "repeat_clusters must use exact contracts")
        ordered_clusters = tuple(
            sorted(
                self.repeat_clusters,
                key=lambda item: (
                    item.action_sha256,
                    item.classification.value,
                    item.member_attempt_ids,
                ),
            )
        )
        if self.repeat_clusters != ordered_clusters:
            _fail("NON_CANONICAL_EXECUTION_STATE", "repeat clusters are not canonically ordered")
        known_attempts = set(attempt_ids)
        action_by_attempt = {item.attempt_id: item.action_sha256 for item in self.recent_attempts}
        for cluster in self.repeat_clusters:
            if not set(cluster.member_attempt_ids).issubset(known_attempts) or any(
                action_by_attempt[item] != cluster.action_sha256
                for item in cluster.member_attempt_ids
            ):
                _fail("REPEAT_CLUSTER_BINDING_MISMATCH", "repeat cluster does not bind attempts")
        if type(self.rubric_state_change) is not RubricStateChangeV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "rubric_state_change is untrusted")
        if (
            type(self.evidence_gaps) is not tuple
            or any(type(item) is not EvidenceGapV1 for item in self.evidence_gaps)
            or len(set(self.evidence_gaps)) != len(self.evidence_gaps)
            or self.evidence_gaps != tuple(sorted(self.evidence_gaps, key=lambda item: item.value))
        ):
            _fail("NON_CANONICAL_EXECUTION_STATE", "evidence gaps must be unique and sorted")
        if type(self.omitted_attempt_count) is not int or self.omitted_attempt_count < 0:
            _fail("INVALID_EXECUTION_STATE_VIEW", "omitted_attempt_count must be non-negative")
        if self.source_event_count < len(self.recent_attempts):
            _fail("INVALID_EXECUTION_STATE_VIEW", "attempts exceed source event count")
        if self.semantic_outcomes_verified is not False:
            _fail(
                "SEMANTIC_OUTCOME_CLAIM_FORBIDDEN",
                "execution-state view cannot claim verified semantic outcomes",
            )
        if self.action_recommendation_present is not False:
            _fail(
                "ACTION_RECOMMENDATION_FORBIDDEN",
                "execution-state view cannot contain action recommendations",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return execution_state_view_projection(self)


def execution_state_view_projection(value: ExecutionStateViewV1) -> dict[str, JsonValue]:
    if type(value) is not ExecutionStateViewV1:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "execution state must use exact v1 contract")
    return {
        "schema_version": value.schema_version,
        "logical_call_id": value.logical_call_id,
        "source_request_sha256": value.source_request_sha256,
        "cutoff_event_id": value.cutoff_event_id,
        "cutoff_event_seq": value.cutoff_event_seq,
        "ledger_sha256": value.ledger_sha256,
        "source_event_count": value.source_event_count,
        "source_event_ids_sha256": value.source_event_ids_sha256,
        "recent_attempts": [item.to_dict() for item in value.recent_attempts],
        "repeat_clusters": [item.to_dict() for item in value.repeat_clusters],
        "rubric_state_change": value.rubric_state_change.value,
        "evidence_gaps": [item.value for item in value.evidence_gaps],
        "omitted_attempt_count": value.omitted_attempt_count,
        "semantic_outcomes_verified": value.semantic_outcomes_verified,
        "action_recommendation_present": value.action_recommendation_present,
    }


def execution_state_view_sha256(value: ExecutionStateViewV1) -> str:
    return canonical_sha256(cast(JsonValue, execution_state_view_projection(value)))


def format_execution_state_view(value: ExecutionStateViewV1) -> str:
    """Render compact repeat facts without translating executor actions for a host."""

    if type(value) is not ExecutionStateViewV1:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "execution state must use exact v1 contract")
    lines = [
        "SENTINEL execution state (evidence only; not action advice):",
        (
            "These are prior harness execution facts. Executor returns and screen-pixel "
            "similarity or difference do not establish task success, failure, or semantic outcome."
        ),
        "Exact repeat evidence:",
    ]
    for index, cluster in enumerate(value.repeat_clusters, start=1):
        screen_fact = (
            "all recorded starting screen pixels were exactly equal"
            if cluster.classification is RepeatClassificationV1.EXACT_ACTION_AND_SCREEN_REPEAT
            else "no all-members starting-screen pixel equality is asserted"
        )
        lines.append(
            f"- Cluster {index}: the same exact executor-dispatched action occurred at least "
            f"{cluster.lower_bound} times; {screen_fact}."
        )
    lines.append(
        "No actor-native action label, task-outcome claim, or next-action recommendation is inferred."
    )
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PromptViewAdapterKeyV1:
    host_id: str
    history_codec_id: str
    history_codec_contract_version: str

    def __post_init__(self) -> None:
        _require_safe_id(self.host_id, "host_id")
        _require_safe_id(self.history_codec_id, "history_codec_id")
        _require_safe_id(self.history_codec_contract_version, "history_codec_contract_version")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "host_id": self.host_id,
            "history_codec_id": self.history_codec_id,
            "history_codec_contract_version": self.history_codec_contract_version,
        }


@dataclass(frozen=True, slots=True)
class PromptViewAdapterDeclarationV1:
    key: PromptViewAdapterKeyV1
    context_kind: CorrectionContextKind = CorrectionContextKind.TEXT_CONTENT_BLOCK
    placement: CorrectionPlacement = CorrectionPlacement.BEFORE
    expected_role: str = "user"
    reference_block_type: str = "image_url"
    schema_version: str = PROMPT_VIEW_ADAPTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            PROMPT_VIEW_ADAPTER_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_PROMPT_VIEW_SCHEMA", "unknown prompt-view adapter schema")
        if type(self.key) is not PromptViewAdapterKeyV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "adapter key has a foreign type")
        if self.context_kind is not CorrectionContextKind.TEXT_CONTENT_BLOCK:
            _fail("UNSUPPORTED_PROMPT_VIEW_CONTEXT", "v1 requires a text content block")
        if self.placement is not CorrectionPlacement.BEFORE:
            _fail("UNSUPPORTED_PROMPT_VIEW_PLACEMENT", "v1 inserts before its reference")
        if self.expected_role != "user" or self.reference_block_type != "image_url":
            _fail(
                "UNSAFE_PROMPT_VIEW_ADAPTER",
                "v1 must bind a user-owned current-image reference",
            )

    @property
    def sha256(self) -> str:
        return canonical_sha256(cast(JsonValue, self.to_dict()))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "context_kind": self.context_kind.value,
            "placement": self.placement.value,
            "expected_role": self.expected_role,
            "reference_block_type": self.reference_block_type,
        }


QWEN_PROMPT_VIEW_KEY_V1 = PromptViewAdapterKeyV1(
    host_id="mobileworld.qwen3vl.actor",
    history_codec_id="mobileworld.g1.history-codec.qwen-flat-progress",
    history_codec_contract_version="v1",
)
MAI_PROMPT_VIEW_KEY_V1 = PromptViewAdapterKeyV1(
    host_id="mobileworld.mai-ui.actor",
    history_codec_id="mobileworld.g1.history-codec.mai-raw-replay",
    history_codec_contract_version="v1",
)


class PromptViewAdapterRegistryV1:
    """Exact host/representation registry; target model identity is not an input."""

    def __init__(self, declarations: tuple[PromptViewAdapterDeclarationV1, ...]) -> None:
        if type(declarations) is not tuple or any(
            type(item) is not PromptViewAdapterDeclarationV1 for item in declarations
        ):
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "adapter declarations must be an exact tuple")
        by_key: dict[PromptViewAdapterKeyV1, PromptViewAdapterDeclarationV1] = {}
        for declaration in declarations:
            if declaration.key in by_key:
                _fail("DUPLICATE_PROMPT_VIEW_ADAPTER", "adapter key is registered twice")
            by_key[declaration.key] = declaration
        self._declarations = by_key

    @property
    def declarations(self) -> tuple[PromptViewAdapterDeclarationV1, ...]:
        return tuple(
            self._declarations[key]
            for key in sorted(
                self._declarations,
                key=lambda item: (
                    item.host_id,
                    item.history_codec_id,
                    item.history_codec_contract_version,
                ),
            )
        )

    def by_key(self, key: PromptViewAdapterKeyV1) -> PromptViewAdapterDeclarationV1:
        if type(key) is not PromptViewAdapterKeyV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "adapter lookup key has a foreign type")
        try:
            return self._declarations[key]
        except KeyError as exc:
            raise PromptViewError(
                "UNKNOWN_PROMPT_VIEW_ADAPTER",
                "host and History-Codec tuple is not registered",
            ) from exc

    def for_history_ir(self, history_ir: HistoryIR) -> PromptViewAdapterDeclarationV1:
        if type(history_ir) is not HistoryIR:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "History IR must use the exact contract")
        return self.by_key(
            PromptViewAdapterKeyV1(
                host_id=history_ir.host_id,
                history_codec_id=history_ir.codec_id,
                history_codec_contract_version=history_ir.codec_contract_version,
            )
        )


def build_prompt_view_adapter_registry() -> PromptViewAdapterRegistryV1:
    return PromptViewAdapterRegistryV1(
        (
            PromptViewAdapterDeclarationV1(key=MAI_PROMPT_VIEW_KEY_V1),
            PromptViewAdapterDeclarationV1(key=QWEN_PROMPT_VIEW_KEY_V1),
        )
    )


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedHistoryCandidateV1:
    """Module-sealed proof that the existing history renderer revalidated a candidate."""

    key: PromptViewAdapterKeyV1
    logical_call_id: str
    raw_request_canonical_bytes: bytes
    candidate_request_canonical_bytes: bytes
    raw_request_sha256: str
    candidate_request_sha256: str
    history_ir_sha256: str
    validation_proof_sha256: str
    validation_checks: tuple[str, ...]
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _HISTORY_PROOF_SEAL:
            _fail("UNSEALED_HISTORY_CANDIDATE", "history candidate lacks validator provenance")
        if type(self.key) is not PromptViewAdapterKeyV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "history proof key has a foreign type")
        _require_safe_id(self.logical_call_id, "logical_call_id")
        raw = _parse_canonical_bytes(self.raw_request_canonical_bytes, "raw request")
        candidate = _parse_canonical_bytes(
            self.candidate_request_canonical_bytes, "history candidate"
        )
        if canonical_sha256(raw) != self.raw_request_sha256 or (
            canonical_sha256(candidate) != self.candidate_request_sha256
        ):
            _fail("PROMPT_VIEW_REQUEST_HASH_MISMATCH", "history proof request hashes differ")
        _require_sha256(self.history_ir_sha256, "history_ir_sha256")
        _require_sha256(self.validation_proof_sha256, "validation_proof_sha256")
        if (
            type(self.validation_checks) is not tuple
            or not self.validation_checks
            or any(type(item) is not str or not item for item in self.validation_checks)
        ):
            _fail("PROMPT_VIEW_HISTORY_PROOF_INVALID", "history validation census is missing")

    @property
    def raw_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.raw_request_canonical_bytes, "raw request")

    @property
    def candidate_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.candidate_request_canonical_bytes, "history candidate")


def bind_r2_4_validated_history_candidate(
    source_request: JsonValue,
    history_ir: HistoryIR,
    admitted_plan: RuntimeVerticalAdmittedPlanV1,
    render_result: RuntimeVerticalRenderResultV1,
) -> ValidatedHistoryCandidateV1:
    """Revalidate and seal the current model-agnostic R2.4 history candidate."""

    from mobile_world.runtime.sentinel.r2_4.contracts import RuntimeVerticalAdmittedPlanV1
    from mobile_world.runtime.sentinel.r2_4.renderer import (
        RuntimeVerticalRenderResultV1,
        validate_vertical_render_result,
        vertical_render_result_sha256,
    )

    if type(history_ir) is not HistoryIR:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "History IR must use the exact contract")
    if type(admitted_plan) is not RuntimeVerticalAdmittedPlanV1 or (
        type(render_result) is not RuntimeVerticalRenderResultV1
    ):
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "history proof inputs require exact R2.4 contracts")
    source_bytes = canonical_json_bytes(source_request)
    try:
        validate_history_ir(source_request, history_ir)
        checks = validate_vertical_render_result(
            source_request, history_ir, admitted_plan, render_result
        )
    except PortableContractError as exc:
        raise PromptViewError(exc.code, "History IR validation failed") from exc
    if render_result.source_request_canonical_bytes != source_bytes:
        _fail("PROMPT_VIEW_SOURCE_BINDING_MISMATCH", "history result binds another request")
    key = PromptViewAdapterKeyV1(
        host_id=history_ir.host_id,
        history_codec_id=history_ir.codec_id,
        history_codec_contract_version=history_ir.codec_contract_version,
    )
    return ValidatedHistoryCandidateV1(
        key=key,
        logical_call_id=admitted_plan.logical_call_id,
        raw_request_canonical_bytes=bytes(render_result.source_request_canonical_bytes),
        candidate_request_canonical_bytes=bytes(render_result.candidate_request_canonical_bytes),
        raw_request_sha256=render_result.source_request_sha256,
        candidate_request_sha256=render_result.candidate_request_sha256,
        history_ir_sha256=canonical_sha256(cast(JsonValue, history_ir.to_dict())),
        validation_proof_sha256=vertical_render_result_sha256(render_result),
        validation_checks=tuple(checks),
        _seal=_HISTORY_PROOF_SEAL,
    )


@dataclass(frozen=True, slots=True)
class PromptViewInsertionDiffV1:
    container_path: JsonPath
    source_index: int
    rendered_index: int
    source_reference_path: JsonPath
    rendered_reference_path: JsonPath
    reference_sha256: str
    inserted_value_sha256: str
    execution_state_view_sha256: str

    def __post_init__(self) -> None:
        container_path = _require_path(self.container_path, "insertion container_path")
        source_reference_path = _require_path(self.source_reference_path, "source reference_path")
        rendered_reference_path = _require_path(
            self.rendered_reference_path, "rendered reference_path"
        )
        if (
            type(self.source_index) is not int
            or self.source_index < 0
            or type(self.rendered_index) is not int
            or self.rendered_index != self.source_index
        ):
            _fail("INVALID_PROMPT_VIEW_INSERTION", "one insertion must retain source index")
        if (
            source_reference_path[:-1] != container_path
            or source_reference_path[-1] != self.source_index
            or rendered_reference_path[:-1] != container_path
            or rendered_reference_path[-1] != self.source_index + 1
        ):
            _fail("INVALID_PROMPT_VIEW_INSERTION", "reference paths do not bind insertion")
        _require_sha256(self.reference_sha256, "reference_sha256")
        _require_sha256(self.inserted_value_sha256, "inserted_value_sha256")
        _require_sha256(self.execution_state_view_sha256, "execution_state_view_sha256")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "container_path": list(self.container_path),
            "source_index": self.source_index,
            "rendered_index": self.rendered_index,
            "source_reference_path": list(self.source_reference_path),
            "rendered_reference_path": list(self.rendered_reference_path),
            "reference_sha256": self.reference_sha256,
            "inserted_value_sha256": self.inserted_value_sha256,
            "execution_state_view_sha256": self.execution_state_view_sha256,
        }


@dataclass(frozen=True, slots=True)
class PromptViewRenderResultV1:
    raw_request_canonical_bytes: bytes
    history_candidate_canonical_bytes: bytes
    candidate_request_canonical_bytes: bytes
    raw_request_sha256: str
    history_candidate_sha256: str
    candidate_request_sha256: str
    history_ir_sha256: str
    history_validation_proof_sha256: str
    adapter_declaration_sha256: str
    execution_state_view_sha256: str
    exact_diff_sha256: str
    insertion: PromptViewInsertionDiffV1
    validation_checks: tuple[str, ...]
    schema_version: str = PROMPT_VIEW_RENDER_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            PROMPT_VIEW_RENDER_RESULT_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_PROMPT_VIEW_SCHEMA", "unknown prompt-view render-result schema")
        raw = _parse_canonical_bytes(self.raw_request_canonical_bytes, "raw request")
        history = _parse_canonical_bytes(
            self.history_candidate_canonical_bytes, "history candidate"
        )
        candidate = _parse_canonical_bytes(
            self.candidate_request_canonical_bytes, "prompt-view candidate"
        )
        for actual, expected, name in (
            (canonical_sha256(raw), self.raw_request_sha256, "raw request"),
            (canonical_sha256(history), self.history_candidate_sha256, "history candidate"),
            (canonical_sha256(candidate), self.candidate_request_sha256, "prompt candidate"),
        ):
            if actual != _require_sha256(expected, f"{name} sha256"):
                _fail("PROMPT_VIEW_REQUEST_HASH_MISMATCH", f"{name} hash differs")
        for digest, name in (
            (self.history_ir_sha256, "history_ir_sha256"),
            (self.history_validation_proof_sha256, "history_validation_proof_sha256"),
            (self.adapter_declaration_sha256, "adapter_declaration_sha256"),
            (self.execution_state_view_sha256, "execution_state_view_sha256"),
            (self.exact_diff_sha256, "exact_diff_sha256"),
        ):
            _require_sha256(digest, name)
        if type(self.insertion) is not PromptViewInsertionDiffV1:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "insertion diff has a foreign type")
        if self.insertion.execution_state_view_sha256 != self.execution_state_view_sha256:
            _fail("PROMPT_VIEW_STATE_BINDING_MISMATCH", "diff binds another state view")
        if canonical_sha256(cast(JsonValue, self.insertion.to_dict())) != self.exact_diff_sha256:
            _fail("PROMPT_VIEW_DIFF_HASH_MISMATCH", "insertion diff hash differs")
        if self.validation_checks != _VALIDATION_CHECKS:
            _fail("PROMPT_VIEW_CHECK_CENSUS_MISMATCH", "validation check census differs")

    @property
    def raw_request(self) -> JsonValue:
        return _parse_canonical_bytes(self.raw_request_canonical_bytes, "raw request")

    @property
    def history_candidate(self) -> JsonValue:
        return _parse_canonical_bytes(self.history_candidate_canonical_bytes, "history candidate")

    @property
    def candidate_request(self) -> JsonValue:
        return _parse_canonical_bytes(
            self.candidate_request_canonical_bytes, "prompt-view candidate"
        )


def prompt_view_render_result_projection(value: PromptViewRenderResultV1) -> dict[str, JsonValue]:
    if type(value) is not PromptViewRenderResultV1:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "render result must use exact v1 contract")
    return {
        "schema_version": value.schema_version,
        "raw_request_sha256": value.raw_request_sha256,
        "history_candidate_sha256": value.history_candidate_sha256,
        "candidate_request_sha256": value.candidate_request_sha256,
        "history_ir_sha256": value.history_ir_sha256,
        "history_validation_proof_sha256": value.history_validation_proof_sha256,
        "adapter_declaration_sha256": value.adapter_declaration_sha256,
        "execution_state_view_sha256": value.execution_state_view_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "insertion": value.insertion.to_dict(),
        "validation_checks": list(value.validation_checks),
    }


def prompt_view_render_result_sha256(value: PromptViewRenderResultV1) -> str:
    return canonical_sha256(cast(JsonValue, prompt_view_render_result_projection(value)))


def _resolve_unique_anchor(source: JsonValue, history_ir: HistoryIR) -> CorrectionAnchor:
    if not history_ir.records:
        _fail("PROMPT_VIEW_ANCHOR_MISSING", "execution state requires non-empty History IR")
    anchors: list[CorrectionAnchor] = []
    for record in history_ir.records:
        if type(record) is not HistoryRecord or type(record.correction_anchors) is not tuple:
            _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "History IR record/anchor graph is untrusted")
        if len(record.correction_anchors) != 1 or (
            type(record.correction_anchors[0]) is not CorrectionAnchor
        ):
            _fail(
                "PROMPT_VIEW_ANCHOR_MISSING_OR_AMBIGUOUS",
                "every history record must bind one current-observation anchor",
            )
        anchors.append(record.correction_anchors[0])
    unique = {canonical_json_bytes(cast(JsonValue, item.to_dict())): item for item in anchors}
    if len(unique) != 1:
        _fail("AMBIGUOUS_PROMPT_VIEW_ANCHOR", "History IR declares multiple prompt anchors")
    anchor = next(iter(unique.values()))
    try:
        container = get_at_path(source, anchor.container_path)
        role = get_at_path(source, anchor.role_path)
        reference = get_at_path(source, anchor.reference_path)
    except PortableContractError as exc:
        raise PromptViewError(exc.code, "prompt-view anchor path does not resolve") from exc
    if type(container) is not list or canonical_sha256(container) != anchor.source_container_sha256:
        _fail("CORRECTION_ANCHOR_DRIFT", "prompt-view source container hash changed")
    if role != anchor.expected_role or role != "user":
        _fail("CORRECTION_ANCHOR_ACTOR_OWNED", "prompt view is not in user-owned context")
    if canonical_sha256(reference) != anchor.reference_sha256:
        _fail("CORRECTION_REFERENCE_DRIFT", "prompt-view current reference hash changed")
    return anchor


def _validate_candidate_anchor(
    candidate: JsonValue,
    anchor: CorrectionAnchor,
    declaration: PromptViewAdapterDeclarationV1,
) -> None:
    try:
        container = get_at_path(candidate, anchor.container_path)
        role = get_at_path(candidate, anchor.role_path)
        reference = get_at_path(candidate, anchor.reference_path)
    except PortableContractError as exc:
        raise PromptViewError(exc.code, "history candidate anchor path does not resolve") from exc
    if type(container) is not list or not 0 <= anchor.insert_index < len(container):
        _fail("PROMPT_VIEW_ANCHOR_OUT_OF_BOUNDS", "history candidate anchor is invalid")
    if role != declaration.expected_role or anchor.expected_role != declaration.expected_role:
        _fail("PROMPT_VIEW_ROLE_DRIFT", "history candidate role changed")
    if canonical_sha256(reference) != anchor.reference_sha256:
        _fail("PROMPT_VIEW_REFERENCE_DRIFT", "history candidate current image changed")
    if type(reference) is not dict or reference.get("type") != declaration.reference_block_type:
        _fail("PROMPT_VIEW_REFERENCE_NOT_IMAGE", "registered reference is not current image")
    if (
        anchor.context_kind is not declaration.context_kind
        or anchor.placement is not declaration.placement
        or anchor.insert_index != cast(int, anchor.reference_path[-1])
    ):
        _fail("PROMPT_VIEW_ADAPTER_ANCHOR_MISMATCH", "anchor differs from adapter declaration")


def _inserted_value(state: ExecutionStateViewV1) -> JsonValue:
    return {"type": "text", "text": format_execution_state_view(state)}


def _render_execution_state_view(
    source_request: JsonValue,
    history_ir: HistoryIR,
    history_candidate: ValidatedHistoryCandidateV1,
    state_view: ExecutionStateViewV1,
    adapter_registry: PromptViewAdapterRegistryV1,
) -> PromptViewRenderResultV1:
    if type(history_ir) is not HistoryIR or type(history_candidate) is not (
        ValidatedHistoryCandidateV1
    ):
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "History IR/candidate requires exact contracts")
    if history_candidate._seal is not _HISTORY_PROOF_SEAL:
        _fail("UNSEALED_HISTORY_CANDIDATE", "history candidate proof is not module-owned")
    if type(state_view) is not ExecutionStateViewV1 or type(adapter_registry) is not (
        PromptViewAdapterRegistryV1
    ):
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "state/registry requires exact v1 contracts")
    source_bytes = canonical_json_bytes(source_request)
    source = copy_json(source_request)
    if source_bytes != history_candidate.raw_request_canonical_bytes:
        _fail("PROMPT_VIEW_SOURCE_BINDING_MISMATCH", "history proof binds another request")
    history_ir_sha256 = canonical_sha256(cast(JsonValue, history_ir.to_dict()))
    if history_ir_sha256 != history_candidate.history_ir_sha256:
        _fail("PROMPT_VIEW_HISTORY_IR_BINDING_MISMATCH", "history proof binds another IR")
    key = PromptViewAdapterKeyV1(
        host_id=history_ir.host_id,
        history_codec_id=history_ir.codec_id,
        history_codec_contract_version=history_ir.codec_contract_version,
    )
    if key != history_candidate.key:
        _fail("PROMPT_VIEW_ADAPTER_BINDING_MISMATCH", "history proof binds another adapter key")
    if state_view.source_request_sha256 != history_candidate.raw_request_sha256 or (
        state_view.logical_call_id != history_candidate.logical_call_id
    ):
        _fail("PROMPT_VIEW_STATE_BINDING_MISMATCH", "state view binds another logical call")
    declaration = adapter_registry.by_key(key)
    try:
        validate_history_ir(source, history_ir)
    except PortableContractError as exc:
        raise PromptViewError(exc.code, "History IR validation failed") from exc
    anchor = _resolve_unique_anchor(source, history_ir)
    base = history_candidate.candidate_request
    base_bytes = history_candidate.candidate_request_canonical_bytes
    _validate_candidate_anchor(base, anchor, declaration)
    inserted = _inserted_value(state_view)
    inserted_sha256 = canonical_sha256(inserted)
    state_sha256 = execution_state_view_sha256(state_view)
    candidate = copy_json(base)
    container = get_at_path(candidate, anchor.container_path)
    if type(container) is not list:
        _fail("PROMPT_VIEW_ANCHOR_NOT_LIST", "candidate anchor container is not an array")
    container.insert(anchor.insert_index, copy_json(inserted))
    rendered_reference_path = (*anchor.container_path, anchor.insert_index + 1)
    if canonical_sha256(get_at_path(candidate, rendered_reference_path)) != anchor.reference_sha256:
        _fail("PROMPT_VIEW_CURRENT_IMAGE_CHANGED", "current image differs after insertion")
    insertion = PromptViewInsertionDiffV1(
        container_path=tuple(anchor.container_path),
        source_index=anchor.insert_index,
        rendered_index=anchor.insert_index,
        source_reference_path=tuple(anchor.reference_path),
        rendered_reference_path=rendered_reference_path,
        reference_sha256=anchor.reference_sha256,
        inserted_value_sha256=inserted_sha256,
        execution_state_view_sha256=state_sha256,
    )
    candidate_bytes = canonical_json_bytes(candidate)
    result = PromptViewRenderResultV1(
        raw_request_canonical_bytes=bytes(source_bytes),
        history_candidate_canonical_bytes=bytes(base_bytes),
        candidate_request_canonical_bytes=candidate_bytes,
        raw_request_sha256=history_candidate.raw_request_sha256,
        history_candidate_sha256=history_candidate.candidate_request_sha256,
        candidate_request_sha256=canonical_sha256(candidate),
        history_ir_sha256=history_ir_sha256,
        history_validation_proof_sha256=history_candidate.validation_proof_sha256,
        adapter_declaration_sha256=declaration.sha256,
        execution_state_view_sha256=state_sha256,
        exact_diff_sha256=canonical_sha256(cast(JsonValue, insertion.to_dict())),
        insertion=insertion,
        validation_checks=_VALIDATION_CHECKS,
    )
    restored = restore_prompt_view_history_candidate(result)
    if canonical_json_bytes(restored) != base_bytes:
        _fail("PROMPT_VIEW_NOT_REVERSIBLE", "state insertion does not restore history candidate")
    if canonical_json_bytes(source_request) != source_bytes:
        _fail("PROMPT_VIEW_CALLER_MUTATED", "prompt renderer mutated caller request")
    return result


def render_execution_state_view(
    source_request: JsonValue,
    history_ir: HistoryIR,
    history_candidate: ValidatedHistoryCandidateV1,
    state_view: ExecutionStateViewV1,
    *,
    adapter_registry: PromptViewAdapterRegistryV1 | None = None,
) -> PromptViewRenderResultV1:
    """Render and independently revalidate one execution-state prompt view."""

    registry = adapter_registry or build_prompt_view_adapter_registry()
    source_bytes = canonical_json_bytes(source_request)
    result = _render_execution_state_view(
        source_request, history_ir, history_candidate, state_view, registry
    )
    validate_prompt_view_render_result(
        source_request,
        history_ir,
        history_candidate,
        state_view,
        result,
        adapter_registry=registry,
    )
    if canonical_json_bytes(source_request) != source_bytes:
        _fail("PROMPT_VIEW_CALLER_MUTATED", "prompt renderer mutated caller request")
    return result


def restore_prompt_view_history_candidate(result: PromptViewRenderResultV1) -> JsonValue:
    if type(result) is not PromptViewRenderResultV1:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "render result must use exact v1 contract")
    restored = result.candidate_request
    insertion = result.insertion
    container = get_at_path(restored, insertion.container_path)
    if (
        type(container) is not list
        or not 0 <= insertion.rendered_index < len(container)
        or canonical_sha256(container[insertion.rendered_index]) != insertion.inserted_value_sha256
    ):
        _fail("PROMPT_VIEW_INSERTION_DRIFT", "inserted state block differs from receipt")
    container.pop(insertion.rendered_index)
    if canonical_json_bytes(restored) != result.history_candidate_canonical_bytes:
        _fail("PROMPT_VIEW_NOT_REVERSIBLE", "restored request differs from history candidate")
    return restored


def validate_prompt_view_render_result(
    source_request: JsonValue,
    history_ir: HistoryIR,
    history_candidate: ValidatedHistoryCandidateV1,
    state_view: ExecutionStateViewV1,
    result: PromptViewRenderResultV1,
    *,
    adapter_registry: PromptViewAdapterRegistryV1 | None = None,
) -> tuple[str, ...]:
    if type(result) is not PromptViewRenderResultV1:
        _fail("UNTRUSTED_PROMPT_VIEW_TYPE", "render result must use exact v1 contract")
    registry = adapter_registry or build_prompt_view_adapter_registry()
    source_bytes = canonical_json_bytes(source_request)
    expected = _render_execution_state_view(
        source_request, history_ir, history_candidate, state_view, registry
    )
    if (
        prompt_view_render_result_projection(result)
        != prompt_view_render_result_projection(expected)
        or result.raw_request_canonical_bytes != expected.raw_request_canonical_bytes
        or result.history_candidate_canonical_bytes != expected.history_candidate_canonical_bytes
        or result.candidate_request_canonical_bytes != expected.candidate_request_canonical_bytes
    ):
        _fail("PROMPT_VIEW_RENDER_RESULT_MISMATCH", "result differs from recomputation")
    restore_prompt_view_history_candidate(result)
    if canonical_json_bytes(source_request) != source_bytes:
        _fail("PROMPT_VIEW_CALLER_MUTATED", "prompt validation mutated caller request")
    return _VALIDATION_CHECKS


__all__ = [
    "EXECUTION_STATE_VIEW_SCHEMA_VERSION",
    "MAI_PROMPT_VIEW_KEY_V1",
    "PROMPT_VIEW_ADAPTER_SCHEMA_VERSION",
    "PROMPT_VIEW_RENDER_RESULT_SCHEMA_VERSION",
    "QWEN_PROMPT_VIEW_KEY_V1",
    "ActionDirectionV1",
    "ActionKindV1",
    "ActionProjectionV1",
    "EvidenceGapV1",
    "ExecutionAttemptSummaryV1",
    "ExecutionKindV1",
    "ExecutionStateViewV1",
    "PromptViewAdapterDeclarationV1",
    "PromptViewAdapterKeyV1",
    "PromptViewAdapterRegistryV1",
    "PromptViewError",
    "PromptViewInsertionDiffV1",
    "PromptViewRenderResultV1",
    "RepeatClassificationV1",
    "RepeatClusterV1",
    "RubricStateChangeV1",
    "SemanticOutcomeV1",
    "SystemButtonV1",
    "TerminalStatusV1",
    "ValidatedHistoryCandidateV1",
    "VisibleDeltaV1",
    "bind_r2_4_validated_history_candidate",
    "build_prompt_view_adapter_registry",
    "execution_state_view_projection",
    "execution_state_view_sha256",
    "format_execution_state_view",
    "prompt_view_render_result_projection",
    "prompt_view_render_result_sha256",
    "render_execution_state_view",
    "restore_prompt_view_history_candidate",
    "validate_prompt_view_render_result",
]
