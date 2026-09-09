"""Small, dependency-light contracts for the active Prompt Sentinel path.

The active path has one result for one logical actor call.  History-policy and
local execution-state outcomes are fields on that result; they are not nested
results or receipts.  Request values are stored as canonical JSON bytes so the
Original is immutable and callers receive a fresh tree when reading it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

FLAT_SENTINEL_CONTRACT_VERSION = "mobileworld.runtime.sentinel.flat/v2"
FLAT_SENTINEL_CALL_RECORD_VERSION = "mobileworld.runtime.sentinel.flat-call-record/v2"

_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_MAX_JSON_NODES = 262_144
_MAX_JSON_DEPTH = 64


class FlatSentinelMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class FlatChannelStatus(StrEnum):
    """One closed vocabulary shared by the two independent channels."""

    SKIPPED = "SKIPPED"
    NO_HISTORY = "NO_HISTORY"
    UNCHANGED = "UNCHANGED"
    WOULD_APPLY = "WOULD_APPLY"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


class FlatContractError(ValueError):
    """A bounded local contract failure that carries no request content."""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or _REASON.fullmatch(code) is None:
            raise ValueError("flat contract error code must be bounded uppercase text")
        self.code = code
        super().__init__(f"{code}: {message}")


def strict_json_bytes(value: object) -> bytes:
    """Validate one finite exact-JSON tree and encode it canonically.

    This is the single copy boundary used by the flat runtime.  It rejects
    serializer-coercible Python values, cycles, excessive depth, and excessive
    size before ``json.dumps`` can silently change their meaning.
    """

    stack: list[tuple[object, int, bool]] = [(value, 0, False)]
    active_container_ids: set[int] = set()
    visits = 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active_container_ids.remove(id(item))
            continue
        visits += 1
        if visits > _MAX_JSON_NODES:
            raise FlatContractError("JSON_NODE_LIMIT", "JSON tree exceeds its node bound")
        if depth > _MAX_JSON_DEPTH:
            raise FlatContractError("JSON_DEPTH_LIMIT", "JSON tree exceeds its depth bound")
        if item is None or type(item) in {bool, int, str}:
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise FlatContractError("NONFINITE_JSON_NUMBER", "JSON number must be finite")
            continue
        if type(item) not in {list, dict}:
            raise FlatContractError("NON_JSON_TYPE", "request contains a non-JSON Python type")
        identity = id(item)
        if identity in active_container_ids:
            raise FlatContractError("CYCLIC_JSON", "JSON containers must not form a cycle")
        active_container_ids.add(identity)
        stack.append((item, depth, True))
        if type(item) is list:
            for child in reversed(cast(list[object], item)):
                stack.append((child, depth + 1, False))
            continue
        mapping = cast(dict[object, object], item)
        if any(type(key) is not str for key in mapping):
            raise FlatContractError("NON_STRING_JSON_KEY", "JSON object keys must be strings")
        for child in reversed(tuple(mapping.values())):
            stack.append((child, depth + 1, False))
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - guarded above.
        raise FlatContractError("JSON_ENCODING_FAILED", "JSON encoding failed") from exc


def decode_canonical_json(value: bytes) -> JsonValue:
    """Decode canonical JSON bytes into a fresh exact-JSON tree."""

    if type(value) is not bytes or not value:
        raise FlatContractError("INVALID_JSON_BYTES", "canonical JSON must be non-empty bytes")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise FlatContractError("DUPLICATE_JSON_KEY", "JSON object repeats a key")
            result[key] = item
        return result

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                FlatContractError("NONFINITE_JSON_NUMBER", "JSON number must be finite")
            ),
        )
    except FlatContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FlatContractError(
            "INVALID_JSON_BYTES", "canonical JSON could not be decoded"
        ) from exc
    encoded = strict_json_bytes(decoded)
    if encoded != value:
        raise FlatContractError("NONCANONICAL_JSON", "JSON bytes are not canonical")
    return cast(JsonValue, decoded)


def json_sha256(value: bytes) -> str:
    if type(value) is not bytes:
        raise FlatContractError("INVALID_JSON_BYTES", "hash input must be exact bytes")
    return hashlib.sha256(value).hexdigest()


def _require_reason(value: object, name: str) -> None:
    if value is not None and (type(value) is not str or _REASON.fullmatch(value) is None):
        raise FlatContractError("INVALID_REASON", f"{name} must be a bounded reason code")


@dataclass(frozen=True, slots=True, repr=False)
class FlatSentinelResult:
    """The sole result returned for one logical actor call."""

    logical_call_id: str
    mode: FlatSentinelMode
    original_json: bytes
    final_json: bytes
    history_status: FlatChannelStatus
    history_reason: str | None
    history_policy_called: bool
    history_target_count: int
    history_drop_count: int
    execution_state_status: FlatChannelStatus
    execution_state_reason: str | None
    execution_repeat_count: int
    would_edit: bool
    edit_applied: bool
    latency_ns: int
    schema_version: str = FLAT_SENTINEL_CONTRACT_VERSION
    _provider_request: dict[str, JsonValue] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.schema_version != FLAT_SENTINEL_CONTRACT_VERSION:
            raise FlatContractError("UNKNOWN_SCHEMA_VERSION", "unknown flat result version")
        if (
            type(self.logical_call_id) is not str
            or _RUNTIME_ID.fullmatch(self.logical_call_id) is None
        ):
            raise FlatContractError("INVALID_LOGICAL_CALL_ID", "logical call ID is invalid")
        if type(self.mode) is not FlatSentinelMode:
            raise FlatContractError("INVALID_MODE", "mode must use FlatSentinelMode")
        if (
            type(self.history_status) is not FlatChannelStatus
            or type(self.execution_state_status) is not FlatChannelStatus
        ):
            raise FlatContractError("INVALID_CHANNEL_STATUS", "channel status is invalid")
        if type(self.original_json) is not bytes or type(self.final_json) is not bytes:
            raise FlatContractError("INVALID_JSON_BYTES", "requests must use canonical JSON bytes")
        _require_reason(self.history_reason, "history_reason")
        _require_reason(self.execution_state_reason, "execution_state_reason")
        for value, name in (
            (self.history_target_count, "history_target_count"),
            (self.history_drop_count, "history_drop_count"),
            (self.execution_repeat_count, "execution_repeat_count"),
            (self.latency_ns, "latency_ns"),
        ):
            if type(value) is not int or value < 0:
                raise FlatContractError("INVALID_COUNT", f"{name} must be non-negative")
        if self.history_drop_count > self.history_target_count:
            raise FlatContractError("INVALID_COUNT", "drop count exceeds target count")
        for value, name in (
            (self.history_policy_called, "history_policy_called"),
            (self.would_edit, "would_edit"),
            (self.edit_applied, "edit_applied"),
        ):
            if type(value) is not bool:
                raise FlatContractError("INVALID_BOOLEAN", f"{name} must be exact bool")
        if not self.history_policy_called and self.history_drop_count != 0:
            raise FlatContractError(
                "POLICY_CALL_MISMATCH",
                "an uncalled history policy cannot have drops or output",
            )
        if self.history_policy_called and self.history_target_count < 1:
            raise FlatContractError(
                "POLICY_CALL_MISMATCH",
                "a called history policy requires at least one target",
            )
        requests_differ = self.original_json != self.final_json
        if requests_differ != self.edit_applied:
            raise FlatContractError(
                "FINAL_SELECTION_MISMATCH",
                "edit_applied must describe whether final differs from Original",
            )
        if self.edit_applied and self.mode is not FlatSentinelMode.ACTIVE:
            raise FlatContractError("EDIT_OUTSIDE_ACTIVE", "only ACTIVE may change the request")
        if self.edit_applied and not self.would_edit:
            raise FlatContractError("FINAL_SELECTION_MISMATCH", "an applied edit must be detected")
        applying_statuses = {
            self.history_status,
            self.execution_state_status,
        }
        if self.edit_applied != (FlatChannelStatus.APPLIED in applying_statuses):
            raise FlatContractError(
                "FINAL_SELECTION_MISMATCH",
                "channel status differs from the final request selection",
            )
        if self.would_edit != bool(
            applying_statuses & {FlatChannelStatus.APPLIED, FlatChannelStatus.WOULD_APPLY}
        ):
            raise FlatContractError(
                "FINAL_SELECTION_MISMATCH",
                "channel status differs from would_edit",
            )
        if FlatChannelStatus.APPLIED in applying_statuses and self.mode is not (
            FlatSentinelMode.ACTIVE
        ):
            raise FlatContractError("EDIT_OUTSIDE_ACTIVE", "APPLIED requires ACTIVE mode")
        if FlatChannelStatus.WOULD_APPLY in applying_statuses and self.mode is not (
            FlatSentinelMode.SHADOW
        ):
            raise FlatContractError("INVALID_CHANNEL_STATUS", "WOULD_APPLY requires SHADOW mode")
        if self.history_status in {
            FlatChannelStatus.APPLIED,
            FlatChannelStatus.WOULD_APPLY,
        } and (not self.history_policy_called or self.history_drop_count < 1):
            raise FlatContractError(
                "POLICY_CALL_MISMATCH",
                "history edit requires a called policy and an admitted output",
            )
        if (
            self.execution_state_status
            in {
                FlatChannelStatus.APPLIED,
                FlatChannelStatus.WOULD_APPLY,
            }
            and self.execution_repeat_count < 1
        ):
            raise FlatContractError("INVALID_COUNT", "state insertion requires a repeat fact")
        if self.history_status is FlatChannelStatus.NO_HISTORY and (
            self.history_policy_called
            or self.history_target_count != 0
            or self.history_drop_count != 0
        ):
            raise FlatContractError(
                "NO_HISTORY_SEMANTIC_WORK",
                "NO_HISTORY cannot call policy or report targets",
            )
        if self.mode is FlatSentinelMode.OFF and (
            self.history_status is not FlatChannelStatus.SKIPPED
            or self.execution_state_status is not FlatChannelStatus.SKIPPED
            or self.history_policy_called
            or self.history_target_count != 0
            or self.history_drop_count != 0
            or self.execution_repeat_count != 0
            or self.history_reason is not None
            or self.execution_state_reason is not None
            or self.would_edit
            or self.edit_applied
        ):
            raise FlatContractError("OFF_MODE_SEMANTIC_WORK", "OFF mode must be an exact bypass")
        if self._provider_request is not None and type(self._provider_request) is not dict:
            raise FlatContractError("INVALID_JSON_BYTES", "provider request must be a JSON object")

    @property
    def original_request(self) -> JsonValue:
        return decode_canonical_json(self.original_json)

    @property
    def final_request(self) -> JsonValue:
        # This one task-local provider tree is intentionally reused across
        # transport and adapter retries. The immutable canonical bytes remain
        # the source for logs and equality checks.
        if self._provider_request is None:
            try:
                decoded = json.loads(self.final_json)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise FlatContractError(
                    "INVALID_JSON_BYTES", "final request is invalid JSON"
                ) from exc
            if type(decoded) is not dict:
                raise FlatContractError("INVALID_JSON_BYTES", "final request must be a JSON object")
            object.__setattr__(self, "_provider_request", decoded)
        return self._provider_request

    @property
    def original_sha256(self) -> str:
        return json_sha256(self.original_json)

    @property
    def final_sha256(self) -> str:
        return json_sha256(self.final_json)

    @property
    def use_transformed_request(self) -> bool:
        return self.edit_applied

    def to_log_dict(self) -> dict[str, JsonValue]:
        """Return the sole optional, secret-free log projection."""

        return {
            "schema_version": FLAT_SENTINEL_CALL_RECORD_VERSION,
            "logical_call_id": self.logical_call_id,
            "mode": self.mode.value,
            "original_sha256": self.original_sha256,
            "final_sha256": self.final_sha256,
            "history_status": self.history_status.value,
            "history_reason": self.history_reason,
            "history_policy_called": self.history_policy_called,
            "history_target_count": self.history_target_count,
            "history_drop_count": self.history_drop_count,
            "execution_state_status": self.execution_state_status.value,
            "execution_state_reason": self.execution_state_reason,
            "execution_repeat_count": self.execution_repeat_count,
            "would_edit": self.would_edit,
            "edit_applied": self.edit_applied,
            "latency_ns": self.latency_ns,
        }


__all__ = [
    "FLAT_SENTINEL_CALL_RECORD_VERSION",
    "FLAT_SENTINEL_CONTRACT_VERSION",
    "FlatChannelStatus",
    "FlatContractError",
    "FlatSentinelMode",
    "FlatSentinelResult",
    "JsonValue",
    "decode_canonical_json",
    "json_sha256",
    "strict_json_bytes",
]
