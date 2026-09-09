"""Causal, model-independent execution facts for the next actor call.

The ledger deliberately describes only what Collector observed before the
current ``step_started`` cutoff.  An executor returning is not task success,
pixel equality is not semantic stagnation, and no value in this module chooses
or recommends a future action.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast

from PIL import Image

from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_sha256, copy_json
from mobile_world.runtime.audit.blob_store import BlobRef, BlobStore

EXECUTION_LEDGER_SCHEMA_VERSION = "mobileworld.runtime.sentinel-execution-ledger/v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MAX_ATTEMPTS = 64
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_IMAGE_PIXELS = 32 * 1024 * 1024
_MAX_VERIFIED_IMAGE_BYTES = 512 * 1024 * 1024


class ExecutionLedgerError(ValueError):
    """A deterministic rejection at the Collector-to-ledger boundary."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class _VerifiedPixelBlob:
    fingerprint: tuple[int, int, int, int, int]
    width: int
    height: int
    mode: str
    pixel_identity_sha256: str


class ExecutionLedgerBlobCacheV1:
    """Task-factory-local verification cache for immutable Collector CAS blobs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str, int, str], _VerifiedPixelBlob] = {}

    def verified_pixel_identity(
        self,
        *,
        blob_store: BlobStore,
        reference: BlobRef,
        width: int,
        height: int,
        mode: str,
        max_image_bytes: int,
        max_image_pixels: int,
    ) -> str:
        digest, byte_length, relative_path = _validate_pixel_ref(
            reference,
            max_image_bytes=max_image_bytes,
        )
        if width * height > max_image_pixels:
            raise ExecutionLedgerError("INVALID_OBSERVATION", "image pixel count is unbounded")
        expected = PurePosixPath("blobs", "sha256", digest[:2], digest)
        if PurePosixPath(relative_path) != expected:
            raise ExecutionLedgerError("PIXEL_BLOB_INVALID", "pixel blob path is noncanonical")
        root = str(blob_store.root.resolve())
        key = (root, digest, byte_length, relative_path)
        path = blob_store.root.joinpath(*expected.parts)
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                try:
                    metadata = path.stat(follow_symlinks=False)
                except OSError:
                    cached = None
                else:
                    fingerprint = _file_fingerprint(metadata)
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or fingerprint != cached.fingerprint
                        or (cached.width, cached.height, cached.mode) != (width, height, mode)
                    ):
                        cached = None
                if cached is not None:
                    return cached.pixel_identity_sha256

            try:
                encoded = blob_store.read_bytes(reference)
                with Image.open(io.BytesIO(encoded)) as opened:
                    if (
                        opened.format != "PNG"
                        or opened.size != (width, height)
                        or opened.mode != mode
                    ):
                        raise ExecutionLedgerError(
                            "PIXEL_METADATA_DRIFT",
                            "Collector screenshot metadata differs from canonical PNG pixels",
                        )
                    actual_width, actual_height = opened.size
                    if actual_width * actual_height > max_image_pixels:
                        raise ExecutionLedgerError(
                            "INVALID_OBSERVATION",
                            "decoded image pixel count is unbounded",
                        )
                    opened.load()
                    pixels = opened.tobytes()
                    palette = opened.getpalette() if opened.mode == "P" else None
            except ExecutionLedgerError:
                raise
            except Exception as exc:
                raise ExecutionLedgerError(
                    "PIXEL_BLOB_INVALID", "pixel blob failed bounded decoding"
                ) from exc
            identity_hasher = hashlib.sha256()
            identity_hasher.update(mode.encode("utf-8"))
            identity_hasher.update(b"\0")
            identity_hasher.update(str(width).encode("ascii"))
            identity_hasher.update(b"\0")
            identity_hasher.update(str(height).encode("ascii"))
            identity_hasher.update(b"\0")
            identity_hasher.update(pixels)
            if palette is not None:
                identity_hasher.update(b"\0palette\0")
                identity_hasher.update(bytes(palette))
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ExecutionLedgerError(
                    "PIXEL_BLOB_INVALID", "pixel blob vanished after verification"
                ) from exc
            verified = _VerifiedPixelBlob(
                fingerprint=_file_fingerprint(metadata),
                width=width,
                height=height,
                mode=mode,
                pixel_identity_sha256=identity_hasher.hexdigest(),
            )
            self._entries[key] = verified
            return verified.pixel_identity_sha256


@dataclass(slots=True)
class _VerificationBudget:
    maximum_bytes: int
    seen: set[tuple[str, int, str]]
    used_bytes: int = 0

    def admit(self, reference: BlobRef, *, max_image_bytes: int) -> None:
        digest, byte_length, relative_path = _validate_pixel_ref(
            reference, max_image_bytes=max_image_bytes
        )
        key = (digest, byte_length, relative_path)
        if key in self.seen:
            return
        self.seen.add(key)
        self.used_bytes += key[1]
        if self.used_bytes > self.maximum_bytes:
            raise ExecutionLedgerError(
                "PIXEL_BUDGET_EXCEEDED", "distinct screenshot bytes exceed ledger budget"
            )


class ExecutorTerminalStatusV1(StrEnum):
    """Transport/executor facts; none of these values is a task verdict."""

    EXECUTOR_RETURNED = "EXECUTOR_RETURNED"
    EXECUTOR_RAISED = "EXECUTOR_RAISED"
    NOT_DISPATCHED = "NOT_DISPATCHED"
    UNKNOWN = "UNKNOWN"


class LedgerExecutionKindV1(StrEnum):
    MOBILE_ACTION = "MOBILE_ACTION"
    TERMINAL_CONTROL = "TERMINAL_CONTROL"
    USER_INTERACTION = "USER_INTERACTION"
    TOOL_ACTION = "TOOL_ACTION"
    UNKNOWN = "UNKNOWN"


class ScreenPixelRelationV1(StrEnum):
    """An exact relationship between two captured pixel matrices."""

    SCREEN_PIXELS_EXACTLY_SAME = "SCREEN_PIXELS_EXACTLY_SAME"
    SCREEN_PIXELS_DIFFERENT = "SCREEN_PIXELS_DIFFERENT"
    UNKNOWN = "UNKNOWN"


class ExecutionSemanticOutcomeV1(StrEnum):
    """Closed by design: Collector transport facts do not prove semantics."""

    UNKNOWN = "UNKNOWN"


class RepeatFactKindV1(StrEnum):
    EXACT_ACTION_REPEAT = "EXACT_ACTION_REPEAT"
    EXACT_ACTION_AND_SCREEN_REPEAT = "EXACT_ACTION_AND_SCREEN_REPEAT"


class LedgerActionKindV1(StrEnum):
    """Closed action vocabulary; actor-provided free text is never retained."""

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


class LedgerActionDirectionV1(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class LedgerSystemButtonV1(StrEnum):
    BACK = "BACK"
    HOME = "HOME"
    MENU = "MENU"
    ENTER = "ENTER"
    OTHER = "OTHER"


_ACTION_KINDS = {
    "click": LedgerActionKindV1.CLICK,
    "long_press": LedgerActionKindV1.LONG_PRESS,
    "double_tap": LedgerActionKindV1.DOUBLE_TAP,
    "swipe": LedgerActionKindV1.SWIPE,
    "drag": LedgerActionKindV1.DRAG,
    "type": LedgerActionKindV1.TYPE,
    "input_text": LedgerActionKindV1.INPUT_TEXT,
    "open_app": LedgerActionKindV1.OPEN_APP,
    "system_button": LedgerActionKindV1.SYSTEM_BUTTON,
    "navigate_back": LedgerActionKindV1.NAVIGATE_BACK,
    "navigate_home": LedgerActionKindV1.NAVIGATE_HOME,
    "keyboard_enter": LedgerActionKindV1.KEYBOARD_ENTER,
    "scroll": LedgerActionKindV1.SCROLL,
    "wait": LedgerActionKindV1.WAIT,
    "answer": LedgerActionKindV1.ANSWER,
    "ask_user": LedgerActionKindV1.ASK_USER,
    "finished": LedgerActionKindV1.TERMINATE,
    "terminate": LedgerActionKindV1.TERMINATE,
    "status": LedgerActionKindV1.STATUS,
    "mcp": LedgerActionKindV1.MCP,
}
_ACTION_DIRECTIONS = {
    "up": LedgerActionDirectionV1.UP,
    "down": LedgerActionDirectionV1.DOWN,
    "left": LedgerActionDirectionV1.LEFT,
    "right": LedgerActionDirectionV1.RIGHT,
}
_ACTION_BUTTONS = {
    "back": LedgerSystemButtonV1.BACK,
    "home": LedgerSystemButtonV1.HOME,
    "menu": LedgerSystemButtonV1.MENU,
    "enter": LedgerSystemButtonV1.ENTER,
}
_SENSITIVE_ACTION_KEYS = {
    "action_json",
    "action_name",
    "answer",
    "app_name",
    "content",
    "goal_status",
    "query",
    "target",
    "target_end",
    "target_start",
    "text",
    "value",
}


@dataclass(frozen=True, slots=True)
class LedgerActionProjectionV1:
    action_kind: LedgerActionKindV1
    coordinate: tuple[int, int] | None = None
    coordinate2: tuple[int, int] | None = None
    direction: LedgerActionDirectionV1 | None = None
    button: LedgerSystemButtonV1 | None = None
    duration_ms: int | None = None
    sensitive_value_present: bool = False

    def __post_init__(self) -> None:
        if type(self.action_kind) is not LedgerActionKindV1:
            raise ExecutionLedgerError("INVALID_ACTION_PROJECTION", "action kind is untrusted")
        for name in ("coordinate", "coordinate2"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not tuple
                or len(value) != 2
                or any(type(item) is not int or not 0 <= item <= 1_000_000_000 for item in value)
            ):
                raise ExecutionLedgerError(
                    "INVALID_ACTION_PROJECTION", f"{name} is not a bounded coordinate"
                )
        if self.direction is not None and type(self.direction) is not LedgerActionDirectionV1:
            raise ExecutionLedgerError("INVALID_ACTION_PROJECTION", "direction is untrusted")
        if self.button is not None and type(self.button) is not LedgerSystemButtonV1:
            raise ExecutionLedgerError("INVALID_ACTION_PROJECTION", "system button is untrusted")
        if self.duration_ms is not None and (
            type(self.duration_ms) is not int or not 0 <= self.duration_ms <= 86_400_000
        ):
            raise ExecutionLedgerError("INVALID_ACTION_PROJECTION", "duration is unbounded")
        if type(self.sensitive_value_present) is not bool:
            raise ExecutionLedgerError(
                "INVALID_ACTION_PROJECTION", "sensitive marker must be exact bool"
            )


@dataclass(frozen=True, slots=True)
class ObservationRefV1:
    source_event_id: str
    source_event_seq: int
    pixel_blob_sha256: str
    pixel_identity_sha256: str
    width: int
    height: int
    mode: str

    def __post_init__(self) -> None:
        _require_runtime_id(self.source_event_id, "source_event_id")
        _require_positive_int(self.source_event_seq, "source_event_seq")
        _require_sha256(self.pixel_blob_sha256, "pixel_blob_sha256")
        _require_sha256(self.pixel_identity_sha256, "pixel_identity_sha256")
        _require_positive_int(self.width, "width")
        _require_positive_int(self.height, "height")
        if self.width > 32768 or self.height > 32768:
            raise ExecutionLedgerError("INVALID_OBSERVATION", "image dimensions are unbounded")
        if type(self.mode) is not str or not 1 <= len(self.mode) <= 32:
            raise ExecutionLedgerError("INVALID_OBSERVATION", "image mode is invalid")


@dataclass(frozen=True, slots=True)
class ExecutionAttemptV1:
    attempt_id: str
    step_id: str
    step_index: int
    decision_id: str
    decision_event_id: str
    decision_event_seq: int
    execution_id: str | None
    action_event_id: str | None
    action_event_seq: int | None
    execution_kind: LedgerExecutionKindV1
    action_sha256: str
    action_projection: LedgerActionProjectionV1
    terminal_event_id: str | None
    terminal_event_seq: int | None
    terminal_status: ExecutorTerminalStatusV1
    pre_observation: ObservationRefV1
    post_observation: ObservationRefV1 | None
    screen_pixel_relation: ScreenPixelRelationV1
    semantic_outcome: ExecutionSemanticOutcomeV1 = ExecutionSemanticOutcomeV1.UNKNOWN

    def __post_init__(self) -> None:
        for name in ("attempt_id", "step_id", "decision_id", "decision_event_id"):
            _require_runtime_id(getattr(self, name), name)
        _require_positive_int(self.step_index, "step_index")
        _require_positive_int(self.decision_event_seq, "decision_event_seq")
        if self.execution_id is not None:
            _require_runtime_id(self.execution_id, "execution_id")
        if self.action_event_id is not None:
            _require_runtime_id(self.action_event_id, "action_event_id")
            _require_positive_int(self.action_event_seq, "action_event_seq")
        elif self.action_event_seq is not None:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "action event sequence has no event")
        if type(self.execution_kind) is not LedgerExecutionKindV1:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "execution kind is untrusted")
        _require_sha256(self.action_sha256, "action_sha256")
        if type(self.action_projection) is not LedgerActionProjectionV1:
            raise ExecutionLedgerError(
                "INVALID_ACTION_PROJECTION", "action projection is untrusted"
            )
        if self.terminal_event_id is not None:
            _require_runtime_id(self.terminal_event_id, "terminal_event_id")
            _require_positive_int(self.terminal_event_seq, "terminal_event_seq")
        elif self.terminal_event_seq is not None:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "terminal sequence has no event")
        if type(self.terminal_status) is not ExecutorTerminalStatusV1:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "terminal status is untrusted")
        if type(self.pre_observation) is not ObservationRefV1:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "pre observation is untrusted")
        if (
            self.post_observation is not None
            and type(self.post_observation) is not ObservationRefV1
        ):
            raise ExecutionLedgerError("INVALID_ATTEMPT", "post observation is untrusted")
        if type(self.screen_pixel_relation) is not ScreenPixelRelationV1:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "pixel relation is untrusted")
        if self.semantic_outcome is not ExecutionSemanticOutcomeV1.UNKNOWN:
            raise ExecutionLedgerError(
                "SEMANTIC_OUTCOME_FORBIDDEN", "execution evidence cannot assert task semantics"
            )
        dispatched = self.action_event_id is not None
        if self.terminal_status is ExecutorTerminalStatusV1.NOT_DISPATCHED:
            if dispatched or self.execution_id is not None or self.post_observation is not None:
                raise ExecutionLedgerError(
                    "INVALID_ATTEMPT", "not-dispatched attempt carries execution facts"
                )
        elif not dispatched or self.execution_id is None:
            raise ExecutionLedgerError(
                "INVALID_ATTEMPT", "dispatched attempt lacks execution identity"
            )
        if (
            self.terminal_status
            in {
                ExecutorTerminalStatusV1.EXECUTOR_RETURNED,
                ExecutorTerminalStatusV1.EXECUTOR_RAISED,
                ExecutorTerminalStatusV1.NOT_DISPATCHED,
            }
            and self.terminal_event_id is None
        ):
            raise ExecutionLedgerError("INVALID_ATTEMPT", "terminal attempt lacks terminal event")
        if (
            self.terminal_status is ExecutorTerminalStatusV1.UNKNOWN
            and self.terminal_event_id is not None
        ):
            raise ExecutionLedgerError("INVALID_ATTEMPT", "unknown terminal has a terminal event")
        if self.action_event_seq is not None and self.decision_event_seq >= self.action_event_seq:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "decision does not precede dispatch")
        if self.pre_observation.source_event_seq >= self.decision_event_seq:
            raise ExecutionLedgerError("INVALID_ATTEMPT", "pre observation is not causal")
        if self.terminal_event_seq is not None:
            if (
                self.action_event_seq is not None
                and self.terminal_event_seq <= self.action_event_seq
            ):
                raise ExecutionLedgerError("INVALID_ATTEMPT", "terminal does not follow dispatch")
            if self.pre_observation.source_event_seq >= self.terminal_event_seq:
                raise ExecutionLedgerError(
                    "INVALID_ATTEMPT", "terminal does not follow pre observation"
                )
            if self.decision_event_seq >= self.terminal_event_seq:
                raise ExecutionLedgerError(
                    "INVALID_ATTEMPT", "terminal does not follow its decision"
                )
        if self.screen_pixel_relation is not ScreenPixelRelationV1.UNKNOWN:
            if self.post_observation is None:
                raise ExecutionLedgerError(
                    "INVALID_ATTEMPT", "pixel relation lacks a post observation"
                )
            if self.terminal_status is not ExecutorTerminalStatusV1.EXECUTOR_RETURNED:
                raise ExecutionLedgerError(
                    "INVALID_ATTEMPT", "pixel relation requires an executor-returned observation"
                )


@dataclass(frozen=True, slots=True)
class ExecutionRepeatFactV1:
    repeat_id: str
    fact_kind: RepeatFactKindV1
    action_sha256: str
    member_attempt_ids: tuple[str, ...]
    lower_bound: int
    evidence_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_runtime_id(self.repeat_id, "repeat_id")
        if type(self.fact_kind) is not RepeatFactKindV1:
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat kind is untrusted")
        _require_sha256(self.action_sha256, "action_sha256")
        if (
            type(self.member_attempt_ids) is not tuple
            or not 2 <= len(self.member_attempt_ids) <= 64
        ):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat members are invalid")
        if len(set(self.member_attempt_ids)) != len(self.member_attempt_ids):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat members are not unique")
        for value in self.member_attempt_ids:
            _require_runtime_id(value, "member_attempt_id")
        if (
            type(self.lower_bound) is not int
            or self.lower_bound != len(self.member_attempt_ids)
            or self.lower_bound > _MAX_ATTEMPTS
        ):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat lower bound is invalid")
        if (
            type(self.evidence_event_ids) is not tuple
            or len(self.evidence_event_ids) != len(self.member_attempt_ids)
            or len(self.evidence_event_ids) > _MAX_ATTEMPTS
        ):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat evidence is missing")
        if len(set(self.evidence_event_ids)) != len(self.evidence_event_ids):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat evidence is not unique")
        for value in self.evidence_event_ids:
            _require_runtime_id(value, "evidence_event_id")


@dataclass(frozen=True, slots=True)
class MobileExecutionLedgerV1:
    run_id: str
    task_run_id: str
    cutoff_event_id: str
    cutoff_step_id: str
    cutoff_step_index: int
    cutoff_event_seq: int
    source_event_count: int
    source_event_ids_sha256: str
    source_prefix_sha256: str
    attempts: tuple[ExecutionAttemptV1, ...]
    repeat_facts: tuple[ExecutionRepeatFactV1, ...]
    complete: bool
    evidence_gaps: tuple[str, ...]
    schema_version: str = EXECUTION_LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EXECUTION_LEDGER_SCHEMA_VERSION:
            raise ExecutionLedgerError("UNKNOWN_SCHEMA_VERSION", "unknown ledger schema")
        for name in ("run_id", "task_run_id", "cutoff_event_id", "cutoff_step_id"):
            _require_runtime_id(getattr(self, name), name)
        _require_positive_int(self.cutoff_step_index, "cutoff_step_index")
        _require_positive_int(self.cutoff_event_seq, "cutoff_event_seq")
        _require_positive_int(self.source_event_count, "source_event_count")
        if self.source_event_count != self.cutoff_event_seq:
            raise ExecutionLedgerError(
                "PREFIX_BINDING_MISMATCH", "source count must end exactly at cutoff sequence"
            )
        _require_sha256(self.source_event_ids_sha256, "source_event_ids_sha256")
        _require_sha256(self.source_prefix_sha256, "source_prefix_sha256")
        if type(self.attempts) is not tuple or len(self.attempts) > _MAX_ATTEMPTS:
            raise ExecutionLedgerError("ATTEMPT_COUNT_REJECTED", "attempt count is unbounded")
        if any(type(value) is not ExecutionAttemptV1 for value in self.attempts):
            raise ExecutionLedgerError("INVALID_ATTEMPT", "attempt type is untrusted")
        if len({item.attempt_id for item in self.attempts}) != len(self.attempts):
            raise ExecutionLedgerError("DUPLICATE_ATTEMPT", "attempt IDs repeat")
        if type(self.repeat_facts) is not tuple or len(self.repeat_facts) > _MAX_ATTEMPTS:
            raise ExecutionLedgerError("REPEAT_COUNT_REJECTED", "repeat facts are unbounded")
        if any(type(value) is not ExecutionRepeatFactV1 for value in self.repeat_facts):
            raise ExecutionLedgerError("INVALID_REPEAT_FACT", "repeat fact type is untrusted")
        attempts_by_id = {item.attempt_id: item for item in self.attempts}
        for item in self.attempts:
            terminal_seq = item.terminal_event_seq or item.action_event_seq
            if item.step_index >= self.cutoff_step_index or (
                terminal_seq is not None and terminal_seq >= self.cutoff_event_seq
            ):
                raise ExecutionLedgerError(
                    "ATTEMPT_AFTER_CUTOFF", "attempt is not strictly before the actor cutoff"
                )
        for fact in self.repeat_facts:
            try:
                members = tuple(attempts_by_id[item] for item in fact.member_attempt_ids)
            except KeyError as exc:
                raise ExecutionLedgerError(
                    "INVALID_REPEAT_FACT", "repeat member is absent from attempts"
                ) from exc
            if any(
                member.terminal_status is ExecutorTerminalStatusV1.NOT_DISPATCHED
                or member.action_sha256 != fact.action_sha256
                or member.action_event_id != evidence_id
                for member, evidence_id in zip(members, fact.evidence_event_ids, strict=True)
            ):
                raise ExecutionLedgerError(
                    "INVALID_REPEAT_FACT", "repeat evidence does not bind dispatched attempts"
                )
            pre_identities = {member.pre_observation.pixel_identity_sha256 for member in members}
            expected_kind = (
                RepeatFactKindV1.EXACT_ACTION_AND_SCREEN_REPEAT
                if len(pre_identities) == 1
                else RepeatFactKindV1.EXACT_ACTION_REPEAT
            )
            if fact.fact_kind is not expected_kind:
                raise ExecutionLedgerError(
                    "INVALID_REPEAT_FACT", "repeat classification differs from its members"
                )
        if type(self.complete) is not bool:
            raise ExecutionLedgerError("INVALID_COMPLETENESS", "complete must be exact boolean")
        if (
            type(self.evidence_gaps) is not tuple
            or len(self.evidence_gaps) > 32
            or any(
                type(value) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value)
                for value in self.evidence_gaps
            )
            or len(set(self.evidence_gaps)) != len(self.evidence_gaps)
        ):
            raise ExecutionLedgerError("INVALID_EVIDENCE_GAPS", "evidence gaps are invalid")

    @property
    def sha256(self) -> str:
        return execution_ledger_sha256(self)


def build_mobile_execution_ledger(
    *,
    events: tuple[dict[str, JsonValue], ...],
    current_event: dict[str, JsonValue],
    run_id: str,
    task_run_id: str,
    blob_store: BlobStore,
    blob_cache: ExecutionLedgerBlobCacheV1 | None = None,
    max_image_bytes: int = _MAX_IMAGE_BYTES,
    max_image_pixels: int = _MAX_IMAGE_PIXELS,
    max_verified_image_bytes: int = _MAX_VERIFIED_IMAGE_BYTES,
) -> MobileExecutionLedgerV1:
    """Fold an already validated, cutoff-bounded Collector prefix."""

    if type(events) is not tuple or not events:
        raise ExecutionLedgerError("EMPTY_EVENT_PREFIX", "Collector prefix is empty")
    if type(current_event) is not dict or current_event.get("event_type") != "step_started":
        raise ExecutionLedgerError("INVALID_CUTOFF", "cutoff must be the current step_started")
    if type(blob_store) is not BlobStore:
        raise ExecutionLedgerError("UNTRUSTED_BLOB_STORE", "blob store type is untrusted")
    if blob_cache is not None and type(blob_cache) is not ExecutionLedgerBlobCacheV1:
        raise ExecutionLedgerError("UNTRUSTED_BLOB_CACHE", "blob cache type is untrusted")
    for name, value in (
        ("max_image_bytes", max_image_bytes),
        ("max_image_pixels", max_image_pixels),
        ("max_verified_image_bytes", max_verified_image_bytes),
    ):
        if type(value) is not int or value < 1:
            raise ExecutionLedgerError("INVALID_RESOURCE_BOUND", f"{name} must be positive")
    cache = blob_cache or ExecutionLedgerBlobCacheV1()
    verification_budget = _VerificationBudget(max_verified_image_bytes, set())
    cutoff_seq = _positive_int(current_event.get("seq"), "cutoff_event_seq")
    cutoff_id = _runtime_id(current_event.get("event_id"), "cutoff_event_id")
    cutoff_payload = _payload(current_event)
    cutoff_step_id = _runtime_id(cutoff_payload.get("step_id"), "cutoff_step_id")
    cutoff_step_index = _positive_int(cutoff_payload.get("step_index"), "cutoff_step_index")
    if (
        len(events) != cutoff_seq
        or events[-1].get("event_id") != cutoff_id
        or canonical_sha256(cast(JsonValue, current_event))
        != canonical_sha256(cast(JsonValue, events[-1]))
    ):
        raise ExecutionLedgerError(
            "PREFIX_BINDING_MISMATCH", "event prefix must terminate at the exact cutoff"
        )
    if any(
        event.get("run_id") != run_id or event.get("task_run_id") != task_run_id for event in events
    ):
        raise ExecutionLedgerError("EVENT_STREAM_BINDING_MISMATCH", "ledger events cross streams")

    step_events: dict[str, dict[str, JsonValue]] = {}
    decision_events: dict[str, dict[str, JsonValue]] = {}
    for item in events:
        event_type = item.get("event_type")
        if event_type == "step_started":
            step_id = _runtime_id(_payload(item).get("step_id"), "step_id")
            if step_id in step_events:
                raise ExecutionLedgerError("DUPLICATE_STEP_ID", "step ID repeats in causal prefix")
            step_events[step_id] = item
        elif event_type == "agent_decision":
            decision_id = _runtime_id(_payload(item).get("decision_id"), "decision_id")
            if decision_id in decision_events:
                raise ExecutionLedgerError(
                    "DUPLICATE_DECISION_ID", "decision ID repeats in causal prefix"
                )
            decision_events[decision_id] = item
    terminal_by_action: dict[str, dict[str, JsonValue]] = {}
    terminal_by_decision: dict[str, dict[str, JsonValue]] = {}
    for event in events[:-1]:
        event_type = event.get("event_type")
        if event_type in {"transition_completed", "transition_failed"}:
            payload = _payload(event)
            action_event_id = _runtime_id(
                payload.get("action_execution_event_id"), "action_execution_event_id"
            )
            if action_event_id in terminal_by_action:
                raise ExecutionLedgerError(
                    "DUPLICATE_TERMINAL", "one action execution has multiple terminals"
                )
            terminal_by_action[action_event_id] = event
        elif event_type == "transition_not_executed":
            decision_id = _runtime_id(_payload(event).get("decision_id"), "decision_id")
            if decision_id in terminal_by_decision:
                raise ExecutionLedgerError(
                    "DUPLICATE_TERMINAL", "one decision has multiple not-dispatched terminals"
                )
            terminal_by_decision[decision_id] = event

    attempts: list[ExecutionAttemptV1] = []
    covered_decisions: set[str] = set()
    covered_action_event_ids: set[str] = set()
    execution_ids: set[str] = set()
    for event in events[:-1]:
        if event.get("event_type") != "action_execution_started":
            continue
        payload = _payload(event)
        action_event_id = _runtime_id(event.get("event_id"), "action_event_id")
        action_event_seq = _positive_int(event.get("seq"), "action_event_seq")
        step_id = _runtime_id(payload.get("step_id"), "step_id")
        decision_id = _runtime_id(payload.get("decision_id"), "decision_id")
        decision_event_id = _runtime_id(event.get("caused_by_event_id"), "decision_event_id")
        execution_id = _runtime_id(payload.get("execution_id"), "execution_id")
        if decision_id in covered_decisions:
            raise ExecutionLedgerError(
                "DUPLICATE_DECISION_EXECUTION", "one decision was dispatched more than once"
            )
        if execution_id in execution_ids:
            raise ExecutionLedgerError(
                "DUPLICATE_EXECUTION_ID", "execution ID repeats in causal prefix"
            )
        covered_decisions.add(decision_id)
        covered_action_event_ids.add(action_event_id)
        execution_ids.add(execution_id)
        step_event = _require_step(step_events, step_id, action_event_seq)
        decision_event = _require_decision(
            decision_events=decision_events,
            decision_id=decision_id,
            decision_event_id=decision_event_id,
            step_id=step_id,
            before_seq=action_event_seq,
        )
        decision_event_seq = _positive_int(decision_event.get("seq"), "decision_event_seq")
        pre = _observation_ref(
            step_event,
            blob_store=blob_store,
            blob_cache=cache,
            verification_budget=verification_budget,
            max_image_bytes=max_image_bytes,
            max_image_pixels=max_image_pixels,
        )
        terminal = terminal_by_action.get(action_event_id)
        status = ExecutorTerminalStatusV1.UNKNOWN
        terminal_id: str | None = None
        terminal_seq: int | None = None
        post: ObservationRefV1 | None = None
        if terminal is not None:
            terminal_id = _runtime_id(terminal.get("event_id"), "terminal_event_id")
            terminal_seq = _positive_int(terminal.get("seq"), "terminal_event_seq")
            terminal_payload = _payload(terminal)
            if (
                terminal.get("caused_by_event_id") != action_event_id
                or terminal_payload.get("step_id") != step_id
                or terminal_payload.get("decision_id") != decision_id
                or terminal_payload.get("execution_id") != execution_id
                or terminal_payload.get("action_execution_event_id") != action_event_id
                or terminal_payload.get("pre_observation_event_id") != step_event.get("event_id")
                or canonical_sha256(terminal_payload.get("action"))
                != canonical_sha256(payload.get("action"))
            ):
                raise ExecutionLedgerError(
                    "TRANSITION_BINDING_MISMATCH",
                    "terminal does not bind the exact step, decision, execution, and action",
                )
            if terminal.get("event_type") == "transition_completed":
                status = ExecutorTerminalStatusV1.EXECUTOR_RETURNED
            else:
                status = ExecutorTerminalStatusV1.EXECUTOR_RAISED
            if terminal_payload.get("post_observation") is not None:
                post = _observation_ref(
                    terminal,
                    blob_store=blob_store,
                    blob_cache=cache,
                    verification_budget=verification_budget,
                    max_image_bytes=max_image_bytes,
                    max_image_pixels=max_image_pixels,
                    post=True,
                )
        action = payload.get("action")
        if canonical_sha256(action) != canonical_sha256(_decision_action(decision_event)):
            raise ExecutionLedgerError(
                "ACTION_BINDING_MISMATCH",
                "dispatched action differs from the exact parsed decision action",
            )
        attempts.append(
            _attempt(
                attempt_id=execution_id,
                step_event=step_event,
                decision_id=decision_id,
                decision_event_id=decision_event_id,
                decision_event_seq=decision_event_seq,
                execution_id=execution_id,
                action_event_id=action_event_id,
                action_event_seq=action_event_seq,
                execution_kind=_execution_kind(
                    payload.get("execution_kind"), _safe_action_projection(action)
                ),
                action=action,
                terminal_event_id=terminal_id,
                terminal_event_seq=terminal_seq,
                terminal_status=status,
                pre=pre,
                post=post,
            )
        )

    for decision_id, terminal in terminal_by_decision.items():
        if decision_id in covered_decisions:
            raise ExecutionLedgerError(
                "TRANSITION_BINDING_MISMATCH", "a dispatched decision is also not-dispatched"
            )
        payload = _payload(terminal)
        step_id = _runtime_id(payload.get("step_id"), "step_id")
        terminal_seq = _positive_int(terminal.get("seq"), "terminal_event_seq")
        step_event = _require_step(step_events, step_id, terminal_seq)
        terminal_id = _runtime_id(terminal.get("event_id"), "terminal_event_id")
        decision_event_id = _runtime_id(terminal.get("caused_by_event_id"), "decision_event_id")
        decision_event = _require_decision(
            decision_events=decision_events,
            decision_id=decision_id,
            decision_event_id=decision_event_id,
            step_id=step_id,
            before_seq=terminal_seq,
        )
        decision_event_seq = _positive_int(decision_event.get("seq"), "decision_event_seq")
        action = payload.get("action")
        if payload.get("pre_observation_event_id") != step_event.get(
            "event_id"
        ) or canonical_sha256(action) != canonical_sha256(_decision_action(decision_event)):
            raise ExecutionLedgerError(
                "TRANSITION_BINDING_MISMATCH",
                "not-dispatched terminal does not bind the exact decision and pre-observation",
            )
        attempts.append(
            _attempt(
                attempt_id=terminal_id,
                step_event=step_event,
                decision_id=decision_id,
                decision_event_id=decision_event_id,
                decision_event_seq=decision_event_seq,
                execution_id=None,
                action_event_id=None,
                action_event_seq=None,
                execution_kind=_execution_kind(None, _safe_action_projection(action)),
                action=action,
                terminal_event_id=terminal_id,
                terminal_event_seq=terminal_seq,
                terminal_status=ExecutorTerminalStatusV1.NOT_DISPATCHED,
                pre=_observation_ref(
                    step_event,
                    blob_store=blob_store,
                    blob_cache=cache,
                    verification_budget=verification_budget,
                    max_image_bytes=max_image_bytes,
                    max_image_pixels=max_image_pixels,
                ),
                post=None,
            )
        )

    if set(terminal_by_action) != covered_action_event_ids:
        raise ExecutionLedgerError(
            "ORPHAN_TRANSITION_TERMINAL", "terminal references an absent action execution"
        )
    if set(decision_events) != covered_decisions | set(terminal_by_decision):
        raise ExecutionLedgerError(
            "ORPHAN_DECISION", "a prior decision has no exact execution terminal"
        )

    attempts.sort(
        key=lambda item: (item.step_index, item.action_event_seq or item.terminal_event_seq or 0)
    )
    if len(attempts) > _MAX_ATTEMPTS:
        raise ExecutionLedgerError("ATTEMPT_COUNT_REJECTED", "attempt count exceeds 64")
    repeat_facts = _repeat_facts(tuple(attempts))
    gaps: list[str] = []
    if any(item.terminal_status is ExecutorTerminalStatusV1.UNKNOWN for item in attempts):
        gaps.append("EXECUTOR_TERMINAL_UNKNOWN")
    if any(item.action_projection.action_kind is LedgerActionKindV1.UNKNOWN for item in attempts):
        gaps.append("ACTION_DETAILS_OMITTED")
    # This invariant is intentional even when every executor call returned.
    gaps.append("TASK_SEMANTIC_OUTCOME_UNVERIFIED")
    prefix_projection = cast(JsonValue, [copy_json(cast(JsonValue, event)) for event in events])
    return MobileExecutionLedgerV1(
        run_id=run_id,
        task_run_id=task_run_id,
        cutoff_event_id=cutoff_id,
        cutoff_step_id=cutoff_step_id,
        cutoff_step_index=cutoff_step_index,
        cutoff_event_seq=cutoff_seq,
        source_event_count=len(events),
        source_event_ids_sha256=canonical_sha256(
            cast(JsonValue, [cast(str, event["event_id"]) for event in events])
        ),
        source_prefix_sha256=canonical_sha256(prefix_projection),
        attempts=tuple(attempts),
        repeat_facts=repeat_facts,
        complete=not any(
            item.terminal_status is ExecutorTerminalStatusV1.UNKNOWN for item in attempts
        ),
        evidence_gaps=tuple(gaps),
    )


def execution_ledger_projection(value: MobileExecutionLedgerV1) -> dict[str, JsonValue]:
    if type(value) is not MobileExecutionLedgerV1:
        raise ExecutionLedgerError("UNTRUSTED_LEDGER", "ledger type is untrusted")
    return {
        "schema_version": value.schema_version,
        "run_id": value.run_id,
        "task_run_id": value.task_run_id,
        "cutoff_event_id": value.cutoff_event_id,
        "cutoff_step_id": value.cutoff_step_id,
        "cutoff_step_index": value.cutoff_step_index,
        "cutoff_event_seq": value.cutoff_event_seq,
        "source_event_count": value.source_event_count,
        "source_event_ids_sha256": value.source_event_ids_sha256,
        "source_prefix_sha256": value.source_prefix_sha256,
        "attempts": [_attempt_projection(item) for item in value.attempts],
        "repeat_facts": [_repeat_projection(item) for item in value.repeat_facts],
        "complete": value.complete,
        "evidence_gaps": list(value.evidence_gaps),
    }


def execution_ledger_sha256(value: MobileExecutionLedgerV1) -> str:
    return canonical_sha256(cast(JsonValue, execution_ledger_projection(value)))


def validate_execution_ledger(value: MobileExecutionLedgerV1) -> None:
    """Rebuild all nested contracts and validate their cross-field relationships."""

    snapshot_execution_ledger(value)


def snapshot_execution_ledger(value: MobileExecutionLedgerV1) -> MobileExecutionLedgerV1:
    if type(value) is not MobileExecutionLedgerV1:
        raise ExecutionLedgerError("UNTRUSTED_LEDGER", "ledger type is untrusted")
    attempts = tuple(
        ExecutionAttemptV1(
            attempt_id=item.attempt_id,
            step_id=item.step_id,
            step_index=item.step_index,
            decision_id=item.decision_id,
            decision_event_id=item.decision_event_id,
            decision_event_seq=item.decision_event_seq,
            execution_id=item.execution_id,
            action_event_id=item.action_event_id,
            action_event_seq=item.action_event_seq,
            execution_kind=item.execution_kind,
            action_sha256=item.action_sha256,
            action_projection=_snapshot_action_projection(item.action_projection),
            terminal_event_id=item.terminal_event_id,
            terminal_event_seq=item.terminal_event_seq,
            terminal_status=item.terminal_status,
            pre_observation=_snapshot_observation(item.pre_observation),
            post_observation=(
                None
                if item.post_observation is None
                else _snapshot_observation(item.post_observation)
            ),
            screen_pixel_relation=item.screen_pixel_relation,
            semantic_outcome=item.semantic_outcome,
        )
        for item in value.attempts
    )
    result = MobileExecutionLedgerV1(
        run_id=value.run_id,
        task_run_id=value.task_run_id,
        cutoff_event_id=value.cutoff_event_id,
        cutoff_step_id=value.cutoff_step_id,
        cutoff_step_index=value.cutoff_step_index,
        cutoff_event_seq=value.cutoff_event_seq,
        source_event_count=value.source_event_count,
        source_event_ids_sha256=value.source_event_ids_sha256,
        source_prefix_sha256=value.source_prefix_sha256,
        attempts=attempts,
        repeat_facts=tuple(
            ExecutionRepeatFactV1(
                repeat_id=item.repeat_id,
                fact_kind=item.fact_kind,
                action_sha256=item.action_sha256,
                member_attempt_ids=tuple(item.member_attempt_ids),
                lower_bound=item.lower_bound,
                evidence_event_ids=tuple(item.evidence_event_ids),
            )
            for item in value.repeat_facts
        ),
        complete=value.complete,
        evidence_gaps=tuple(value.evidence_gaps),
    )
    if result.sha256 != value.sha256:
        raise ExecutionLedgerError("LEDGER_SNAPSHOT_DRIFT", "ledger changed while detached")
    return result


def _snapshot_observation(value: ObservationRefV1) -> ObservationRefV1:
    if type(value) is not ObservationRefV1:
        raise ExecutionLedgerError("INVALID_OBSERVATION", "observation type is untrusted")
    return ObservationRefV1(
        source_event_id=value.source_event_id,
        source_event_seq=value.source_event_seq,
        pixel_blob_sha256=value.pixel_blob_sha256,
        pixel_identity_sha256=value.pixel_identity_sha256,
        width=value.width,
        height=value.height,
        mode=value.mode,
    )


def _snapshot_action_projection(value: LedgerActionProjectionV1) -> LedgerActionProjectionV1:
    if type(value) is not LedgerActionProjectionV1:
        raise ExecutionLedgerError("INVALID_ACTION_PROJECTION", "action projection is untrusted")
    return LedgerActionProjectionV1(
        action_kind=value.action_kind,
        coordinate=value.coordinate,
        coordinate2=value.coordinate2,
        direction=value.direction,
        button=value.button,
        duration_ms=value.duration_ms,
        sensitive_value_present=value.sensitive_value_present,
    )


def _attempt(
    *,
    attempt_id: str,
    step_event: dict[str, JsonValue],
    decision_id: str,
    decision_event_id: str,
    decision_event_seq: int,
    execution_id: str | None,
    action_event_id: str | None,
    action_event_seq: int | None,
    execution_kind: LedgerExecutionKindV1,
    action: JsonValue,
    terminal_event_id: str | None,
    terminal_event_seq: int | None,
    terminal_status: ExecutorTerminalStatusV1,
    pre: ObservationRefV1,
    post: ObservationRefV1 | None,
) -> ExecutionAttemptV1:
    action_sha256 = canonical_sha256(action)
    action_projection = _safe_action_projection(action)
    relation = ScreenPixelRelationV1.UNKNOWN
    if post is not None and terminal_status is ExecutorTerminalStatusV1.EXECUTOR_RETURNED:
        relation = (
            ScreenPixelRelationV1.SCREEN_PIXELS_EXACTLY_SAME
            if pre.pixel_identity_sha256 == post.pixel_identity_sha256
            else ScreenPixelRelationV1.SCREEN_PIXELS_DIFFERENT
        )
    step_payload = _payload(step_event)
    return ExecutionAttemptV1(
        attempt_id=attempt_id,
        step_id=_runtime_id(step_payload.get("step_id"), "step_id"),
        step_index=_positive_int(step_payload.get("step_index"), "step_index"),
        decision_id=decision_id,
        decision_event_id=decision_event_id,
        decision_event_seq=decision_event_seq,
        execution_id=execution_id,
        action_event_id=action_event_id,
        action_event_seq=action_event_seq,
        execution_kind=execution_kind,
        action_sha256=action_sha256,
        action_projection=action_projection,
        terminal_event_id=terminal_event_id,
        terminal_event_seq=terminal_event_seq,
        terminal_status=terminal_status,
        pre_observation=pre,
        post_observation=post,
        screen_pixel_relation=relation,
    )


def _observation_ref(
    event: dict[str, JsonValue],
    *,
    blob_store: BlobStore,
    blob_cache: ExecutionLedgerBlobCacheV1,
    verification_budget: _VerificationBudget,
    max_image_bytes: int,
    max_image_pixels: int,
    post: bool = False,
) -> ObservationRefV1:
    payload = _payload(event)
    observation = payload.get("post_observation") if post else payload.get("observation")
    if type(observation) is not dict or type(observation.get("screenshot")) is not dict:
        raise ExecutionLedgerError("OBSERVATION_UNAVAILABLE", "screenshot projection is missing")
    screenshot = cast(dict[str, JsonValue], observation["screenshot"])
    if screenshot.get("representation") != "canonical_png_from_runtime_pixels":
        raise ExecutionLedgerError("INVALID_OBSERVATION", "screenshot representation is invalid")
    pixel_ref = screenshot.get("pixel_blob")
    if type(pixel_ref) is not dict:
        raise ExecutionLedgerError("INVALID_OBSERVATION", "pixel blob reference is missing")
    width = _positive_int(screenshot.get("width"), "screenshot.width")
    height = _positive_int(screenshot.get("height"), "screenshot.height")
    mode = _bounded_text(screenshot.get("mode"), "screenshot.mode", 32)
    reference = cast(BlobRef, pixel_ref)
    verification_budget.admit(reference, max_image_bytes=max_image_bytes)
    identity = blob_cache.verified_pixel_identity(
        blob_store=blob_store,
        reference=reference,
        width=width,
        height=height,
        mode=mode,
        max_image_bytes=max_image_bytes,
        max_image_pixels=max_image_pixels,
    )
    digest = _sha256(pixel_ref.get("digest"), "pixel_blob.digest")
    return ObservationRefV1(
        source_event_id=_runtime_id(event.get("event_id"), "source_event_id"),
        source_event_seq=_positive_int(event.get("seq"), "source_event_seq"),
        pixel_blob_sha256=digest,
        pixel_identity_sha256=identity,
        width=width,
        height=height,
        mode=mode,
    )


def _repeat_facts(attempts: tuple[ExecutionAttemptV1, ...]) -> tuple[ExecutionRepeatFactV1, ...]:
    groups: dict[str, list[ExecutionAttemptV1]] = {}
    for item in attempts:
        if item.terminal_status is ExecutorTerminalStatusV1.NOT_DISPATCHED:
            continue
        groups.setdefault(item.action_sha256, []).append(item)
    facts: list[ExecutionRepeatFactV1] = []
    for action_sha256, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        pre_hashes = {item.pre_observation.pixel_identity_sha256 for item in group}
        kind = (
            RepeatFactKindV1.EXACT_ACTION_AND_SCREEN_REPEAT
            if len(pre_hashes) == 1
            else RepeatFactKindV1.EXACT_ACTION_REPEAT
        )
        members = tuple(item.attempt_id for item in group)
        evidence = tuple(
            cast(str, item.action_event_id or item.terminal_event_id) for item in group
        )
        digest = hashlib.sha256(
            f"{kind.value}\0{action_sha256}\0{'\0'.join(members)}".encode()
        ).hexdigest()
        facts.append(
            ExecutionRepeatFactV1(
                repeat_id=f"execution-repeat-{digest[:32]}",
                fact_kind=kind,
                action_sha256=action_sha256,
                member_attempt_ids=members,
                lower_bound=len(group),
                evidence_event_ids=evidence,
            )
        )
    return tuple(facts)


def _safe_action_projection(value: JsonValue) -> LedgerActionProjectionV1:
    if type(value) is not dict:
        return LedgerActionProjectionV1(
            action_kind=LedgerActionKindV1.UNKNOWN,
            sensitive_value_present=value is not None,
        )
    action = value
    raw_kind = action.get("action_type", action.get("type"))
    kind_text = raw_kind.lower() if type(raw_kind) is str else ""
    coordinate = _coordinate(action.get("coordinate"))
    if coordinate is None:
        coordinate = _xy(action.get("x"), action.get("y"))
    if coordinate is None:
        coordinate = _coordinate(action.get("start_coordinate"))
    coordinate2 = _coordinate(action.get("end_coordinate"))
    if coordinate2 is None:
        coordinate2 = _xy(action.get("x2"), action.get("y2"))
    raw_direction = action.get("direction")
    direction = (
        _ACTION_DIRECTIONS.get(raw_direction.lower()) if type(raw_direction) is str else None
    )
    raw_button = action.get("button")
    button = (
        _ACTION_BUTTONS.get(raw_button.lower(), LedgerSystemButtonV1.OTHER)
        if type(raw_button) is str
        else None
    )
    raw_duration = action.get("duration_ms")
    duration_ms = (
        raw_duration if type(raw_duration) is int and 0 <= raw_duration <= 86_400_000 else None
    )
    sensitive = any(
        key in action and action[key] is not None and action[key] != ""
        for key in _SENSITIVE_ACTION_KEYS
    )
    return LedgerActionProjectionV1(
        action_kind=_ACTION_KINDS.get(kind_text, LedgerActionKindV1.UNKNOWN),
        coordinate=coordinate,
        coordinate2=coordinate2,
        direction=direction,
        button=button,
        duration_ms=duration_ms,
        sensitive_value_present=sensitive,
    )


def _execution_kind(raw_value: object, action: LedgerActionProjectionV1) -> LedgerExecutionKindV1:
    normalized = raw_value.lower() if type(raw_value) is str else ""
    if normalized == "gui":
        return LedgerExecutionKindV1.MOBILE_ACTION
    if normalized == "mcp" or action.action_kind is LedgerActionKindV1.MCP:
        return LedgerExecutionKindV1.TOOL_ACTION
    if normalized == "ask_user" or action.action_kind is LedgerActionKindV1.ASK_USER:
        return LedgerExecutionKindV1.USER_INTERACTION
    if action.action_kind in {
        LedgerActionKindV1.ANSWER,
        LedgerActionKindV1.STATUS,
        LedgerActionKindV1.TERMINATE,
    }:
        return LedgerExecutionKindV1.TERMINAL_CONTROL
    return LedgerExecutionKindV1.UNKNOWN


def _coordinate(value: JsonValue | None) -> tuple[int, int] | None:
    if (
        type(value) is list
        and len(value) == 2
        and all(type(item) is int and 0 <= item <= 1_000_000_000 for item in value)
    ):
        return cast(tuple[int, int], tuple(value))
    return None


def _xy(x_value: JsonValue | None, y_value: JsonValue | None) -> tuple[int, int] | None:
    if (
        type(x_value) is int
        and type(y_value) is int
        and 0 <= x_value <= 1_000_000_000
        and 0 <= y_value <= 1_000_000_000
    ):
        return x_value, y_value
    return None


def _validate_pixel_ref(value: object, *, max_image_bytes: int) -> tuple[str, int, str]:
    if type(value) is not dict or set(value) != {
        "algorithm",
        "digest",
        "byte_length",
        "media_type",
        "relative_path",
    }:
        raise ExecutionLedgerError("PIXEL_BLOB_INVALID", "pixel blob reference is malformed")
    reference = cast(dict[str, object], value)
    if reference["algorithm"] != "sha256" or reference["media_type"] != "image/png":
        raise ExecutionLedgerError("PIXEL_BLOB_INVALID", "pixel blob encoding is unsupported")
    digest = _sha256(reference["digest"], "pixel_blob.digest")
    byte_length = reference["byte_length"]
    relative_path = reference["relative_path"]
    if (
        type(byte_length) is not int
        or not 1 <= byte_length <= max_image_bytes
        or type(relative_path) is not str
        or not relative_path
    ):
        raise ExecutionLedgerError("PIXEL_BLOB_INVALID", "pixel blob exceeds its bounds")
    return digest, byte_length, relative_path


def _file_fingerprint(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _attempt_projection(value: ExecutionAttemptV1) -> dict[str, JsonValue]:
    return {
        "attempt_id": value.attempt_id,
        "step_id": value.step_id,
        "step_index": value.step_index,
        "decision_id": value.decision_id,
        "decision_event_id": value.decision_event_id,
        "decision_event_seq": value.decision_event_seq,
        "execution_id": value.execution_id,
        "action_event_id": value.action_event_id,
        "action_event_seq": value.action_event_seq,
        "execution_kind": value.execution_kind.value,
        "action_sha256": value.action_sha256,
        "action_projection": _action_projection(value.action_projection),
        "terminal_event_id": value.terminal_event_id,
        "terminal_event_seq": value.terminal_event_seq,
        "terminal_status": value.terminal_status.value,
        "pre_observation": _observation_projection(value.pre_observation),
        "post_observation": (
            None
            if value.post_observation is None
            else _observation_projection(value.post_observation)
        ),
        "screen_pixel_relation": value.screen_pixel_relation.value,
        "semantic_outcome": value.semantic_outcome.value,
    }


def _observation_projection(value: ObservationRefV1) -> dict[str, JsonValue]:
    return {
        "source_event_id": value.source_event_id,
        "source_event_seq": value.source_event_seq,
        "pixel_blob_sha256": value.pixel_blob_sha256,
        "pixel_identity_sha256": value.pixel_identity_sha256,
        "width": value.width,
        "height": value.height,
        "mode": value.mode,
    }


def _action_projection(value: LedgerActionProjectionV1) -> dict[str, JsonValue]:
    return {
        "action_kind": value.action_kind.value,
        "coordinate": None if value.coordinate is None else list(value.coordinate),
        "coordinate2": None if value.coordinate2 is None else list(value.coordinate2),
        "direction": None if value.direction is None else value.direction.value,
        "button": None if value.button is None else value.button.value,
        "duration_ms": value.duration_ms,
        "sensitive_value_present": value.sensitive_value_present,
    }


def _repeat_projection(value: ExecutionRepeatFactV1) -> dict[str, JsonValue]:
    return {
        "repeat_id": value.repeat_id,
        "fact_kind": value.fact_kind.value,
        "action_sha256": value.action_sha256,
        "member_attempt_ids": list(value.member_attempt_ids),
        "lower_bound": value.lower_bound,
        "evidence_event_ids": list(value.evidence_event_ids),
    }


def _payload(event: dict[str, JsonValue]) -> dict[str, JsonValue]:
    payload = event.get("payload")
    if type(payload) is not dict:
        raise ExecutionLedgerError("INVALID_EVENT_PAYLOAD", "event payload is not an object")
    return payload


def _require_step(
    step_events: dict[str, dict[str, JsonValue]], step_id: str, before_seq: int
) -> dict[str, JsonValue]:
    step = step_events.get(step_id)
    if step is None or _positive_int(step.get("seq"), "step_event_seq") >= before_seq:
        raise ExecutionLedgerError("STEP_BINDING_MISMATCH", "attempt does not bind a prior step")
    return step


def _require_decision(
    *,
    decision_events: dict[str, dict[str, JsonValue]],
    decision_id: str,
    decision_event_id: str,
    step_id: str,
    before_seq: int,
) -> dict[str, JsonValue]:
    decision = decision_events.get(decision_id)
    if (
        decision is None
        or decision.get("event_id") != decision_event_id
        or _positive_int(decision.get("seq"), "decision_event_seq") >= before_seq
    ):
        raise ExecutionLedgerError(
            "DECISION_BINDING_MISMATCH", "attempt does not bind its exact prior decision"
        )
    payload = _payload(decision)
    if payload.get("step_id") != step_id or payload.get("decision_id") != decision_id:
        raise ExecutionLedgerError(
            "DECISION_BINDING_MISMATCH", "decision belongs to another step or identity"
        )
    return decision


def _decision_action(decision_event: dict[str, JsonValue]) -> JsonValue:
    parsed = _payload(decision_event).get("parsed_action")
    if parsed is None:
        return None
    if type(parsed) is not dict or "value" not in parsed:
        raise ExecutionLedgerError(
            "DECISION_BINDING_MISMATCH", "decision parsed action is malformed"
        )
    return parsed["value"]


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ExecutionLedgerError("INVALID_SHA256", f"{name} is not SHA-256")
    return value


def _sha256(value: object, name: str) -> str:
    return _require_sha256(value, name)


def _require_runtime_id(value: object, name: str) -> str:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        raise ExecutionLedgerError("INVALID_RUNTIME_ID", f"{name} is invalid")
    return value


def _runtime_id(value: object, name: str) -> str:
    return _require_runtime_id(value, name)


def _require_positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ExecutionLedgerError("INVALID_INTEGER", f"{name} must be positive")
    return value


def _positive_int(value: object, name: str) -> int:
    return _require_positive_int(value, name)


def _bounded_text(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ExecutionLedgerError("INVALID_TEXT", f"{name} is invalid")
    return value


__all__ = [
    "EXECUTION_LEDGER_SCHEMA_VERSION",
    "ExecutionAttemptV1",
    "ExecutionLedgerBlobCacheV1",
    "ExecutionLedgerError",
    "ExecutionRepeatFactV1",
    "ExecutionSemanticOutcomeV1",
    "ExecutorTerminalStatusV1",
    "LedgerActionDirectionV1",
    "LedgerActionKindV1",
    "LedgerActionProjectionV1",
    "LedgerSystemButtonV1",
    "LedgerExecutionKindV1",
    "MobileExecutionLedgerV1",
    "ObservationRefV1",
    "RepeatFactKindV1",
    "ScreenPixelRelationV1",
    "build_mobile_execution_ledger",
    "execution_ledger_projection",
    "execution_ledger_sha256",
    "snapshot_execution_ledger",
    "validate_execution_ledger",
]
