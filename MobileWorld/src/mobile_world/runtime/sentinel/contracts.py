"""Versioned contracts for the R2.1 pre-call Prompt Sentinel seam."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    HistoryIR,
    JsonValue,
    TransformationPlan,
    canonical_json_bytes,
    canonical_sha256,
    copy_json,
)

SENTINEL_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-receipt/v1"
SENTINEL_RUNTIME_CONTRACT_VERSION = "v1"

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SEMANTIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}")
_CONTRACT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
_CHECK_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


class SentinelMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


class SentinelCallRole(StrEnum):
    ACTOR = "actor"
    SENTINEL = "sentinel"


class SentinelDecisionKind(StrEnum):
    KEEP = "KEEP"
    DROP = "DROP"
    REPLACE = "REPLACE"
    KEEP_UNCERTAIN = "KEEP_UNCERTAIN"


class SentinelFallbackReason(StrEnum):
    POLICY_TIMEOUT = "POLICY_TIMEOUT"
    POLICY_EXCEPTION = "POLICY_EXCEPTION"
    INVALID_POLICY_OUTPUT = "INVALID_POLICY_OUTPUT"
    INVALID_REQUEST_SCHEMA = "INVALID_REQUEST_SCHEMA"
    UNSUPPORTED_HISTORY_FAMILY = "UNSUPPORTED_HISTORY_FAMILY"
    AMBIGUOUS_HISTORY_SPAN = "AMBIGUOUS_HISTORY_SPAN"
    HISTORY_EXTRACTION_FAILURE = "HISTORY_EXTRACTION_FAILURE"
    RENDERER_FAILURE = "RENDERER_FAILURE"
    INVARIANT_FAILURE = "INVARIANT_FAILURE"
    SIDECAR_FAILURE = "SIDECAR_FAILURE"
    REQUEST_DRIFT = "REQUEST_DRIFT"


class SentinelBypassReason(StrEnum):
    MODE_OFF = "MODE_OFF"
    GLOBAL_KILL_SWITCH = "GLOBAL_KILL_SWITCH"
    CALL_ROLE_SENTINEL = "CALL_ROLE_SENTINEL"


class SentinelValidationStatus(StrEnum):
    BYPASSED = "BYPASSED"
    PASSED = "PASSED"
    FALLBACK_ORIGINAL = "FALLBACK_ORIGINAL"


@dataclass(frozen=True)
class SentinelHostConfig:
    """Per-host mode and bounded synchronous policy budget."""

    mode: SentinelMode = SentinelMode.OFF
    history_codec_contract_version: str = "v1"
    policy_timeout_ms: int = 250

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SentinelMode):
            raise TypeError("mode must be SentinelMode")
        if (
            not isinstance(self.history_codec_contract_version, str)
            or _CONTRACT_VERSION.fullmatch(self.history_codec_contract_version) is None
        ):
            raise ValueError("history codec contract version must be a bounded safe identifier")
        if type(self.policy_timeout_ms) is not int or self.policy_timeout_ms <= 0:
            raise ValueError("policy_timeout_ms must be a positive integer")


@dataclass(frozen=True)
class SentinelContext:
    """Small, request-external context bound to one logical actor decision."""

    logical_call_id: str
    host_id: str
    attributes: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if _RUNTIME_ID.fullmatch(self.logical_call_id) is None:
            raise ValueError("logical_call_id must be a bounded path-safe runtime ID")
        if _RUNTIME_ID.fullmatch(self.host_id) is None:
            raise ValueError("host_id must be a bounded path-safe runtime ID")
        copied = copy_json(cast(JsonValue, self.attributes))
        if not isinstance(copied, dict):
            raise TypeError("context attributes must be a canonical JSON object")
        object.__setattr__(self, "attributes", cast(dict[str, JsonValue], copied))


@dataclass(frozen=True)
class SentinelDecision:
    """One schema-bounded policy decision, without request or evidence bytes."""

    decision_id: str
    kind: SentinelDecisionKind
    operation_id: str | None = None
    record_id: str | None = None
    reason_code: str = "DETERMINISTIC_R21"

    def __post_init__(self) -> None:
        if not isinstance(self.decision_id, str) or not self.decision_id:
            raise ValueError("decision_id must be a non-empty string")
        if not isinstance(self.reason_code, str) or not self.reason_code:
            raise ValueError("reason_code must be a non-empty string")
        if not isinstance(self.kind, SentinelDecisionKind):
            raise TypeError("kind must be SentinelDecisionKind")
        for value in (self.operation_id, self.record_id):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("optional decision IDs must be non-empty strings when present")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "operation_id": self.operation_id,
            "record_id": self.record_id,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class SentinelPolicyOutput:
    """Replaceable policy result consumed by deterministic rendering and guards."""

    decisions: tuple[SentinelDecision, ...]
    transformation_plan: TransformationPlan | None

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, tuple) or not self.decisions:
            raise ValueError("policy output needs at least one decision")
        if any(not isinstance(item, SentinelDecision) for item in self.decisions):
            raise TypeError("policy decisions must be SentinelDecision values")
        if self.transformation_plan is not None and not isinstance(
            self.transformation_plan, TransformationPlan
        ):
            raise TypeError("transformation_plan must be TransformationPlan or None")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "decisions": [item.to_dict() for item in self.decisions],
            "transformation_plan": (
                None if self.transformation_plan is None else self.transformation_plan.to_dict()
            ),
        }


@runtime_checkable
class SentinelPolicy(Protocol):
    @property
    def policy_id(self) -> str: ...

    def evaluate(
        self,
        *,
        request: JsonValue,
        context: SentinelContext,
        history_ir: HistoryIR,
    ) -> SentinelPolicyOutput: ...


@dataclass(frozen=True)
class SentinelReceipt:
    """Hash-only sidecar receipt safe for ordinary runtime telemetry."""

    logical_call_id: str
    host_id: str
    call_role: SentinelCallRole
    configured_mode: SentinelMode
    effective_mode: SentinelMode
    bypass_reason: SentinelBypassReason | None
    global_kill_switch_active: bool
    history_codec_id: str | None
    history_codec_contract_version: str | None
    policy_id: str | None
    policy_output_sha256: str
    raw_request_sha256: str
    candidate_request_sha256: str
    final_request_sha256: str
    exact_diff_sha256: str
    decision_kinds: tuple[SentinelDecisionKind, ...]
    policy_evaluated: bool
    would_edit: bool
    edit_applied: bool
    fallback_reason: SentinelFallbackReason | None
    validation_status: SentinelValidationStatus
    validation_checks: tuple[str, ...]
    latency_ns: int
    request_views_persisted: bool = False
    exact_diffs_persisted: bool = False
    schema_version: str = SENTINEL_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SENTINEL_RECEIPT_SCHEMA_VERSION:
            raise ValueError("unknown Sentinel receipt schema version")
        if _RUNTIME_ID.fullmatch(self.logical_call_id) is None:
            raise ValueError("receipt logical_call_id must be a bounded path-safe runtime ID")
        if _RUNTIME_ID.fullmatch(self.host_id) is None:
            raise ValueError("receipt host_id must be a bounded path-safe runtime ID")
        if not isinstance(self.call_role, SentinelCallRole):
            raise TypeError("call_role must be SentinelCallRole")
        if not isinstance(self.configured_mode, SentinelMode) or not isinstance(
            self.effective_mode, SentinelMode
        ):
            raise TypeError("configured_mode and effective_mode must be SentinelMode")
        if self.bypass_reason is not None and not isinstance(
            self.bypass_reason, SentinelBypassReason
        ):
            raise TypeError("bypass_reason must be SentinelBypassReason or None")
        if self.fallback_reason is not None and not isinstance(
            self.fallback_reason, SentinelFallbackReason
        ):
            raise TypeError("fallback_reason must be SentinelFallbackReason or None")
        if not isinstance(self.validation_status, SentinelValidationStatus):
            raise TypeError("validation_status must be SentinelValidationStatus")
        if any(not isinstance(item, SentinelDecisionKind) for item in self.decision_kinds):
            raise TypeError("decision_kinds must contain SentinelDecisionKind values")
        if (self.history_codec_id is None) != (self.history_codec_contract_version is None):
            raise ValueError("codec ID and contract version must be present together")
        if self.history_codec_id is not None and (
            not isinstance(self.history_codec_id, str)
            or _SEMANTIC_ID.fullmatch(self.history_codec_id) is None
        ):
            raise ValueError("history_codec_id must be a bounded safe identifier")
        if self.history_codec_contract_version is not None and (
            not isinstance(self.history_codec_contract_version, str)
            or _CONTRACT_VERSION.fullmatch(self.history_codec_contract_version) is None
        ):
            raise ValueError("codec contract version must be a bounded safe identifier")
        if self.policy_id is not None and (
            not isinstance(self.policy_id, str) or _SEMANTIC_ID.fullmatch(self.policy_id) is None
        ):
            raise ValueError("policy_id must be a bounded safe identifier")
        for digest in (
            self.policy_output_sha256,
            self.raw_request_sha256,
            self.candidate_request_sha256,
            self.final_request_sha256,
            self.exact_diff_sha256,
        ):
            if _SHA256.fullmatch(digest) is None:
                raise ValueError("receipt hashes must be lowercase SHA-256")
        for value in (
            self.global_kill_switch_active,
            self.policy_evaluated,
            self.would_edit,
            self.edit_applied,
            self.request_views_persisted,
            self.exact_diffs_persisted,
        ):
            if type(value) is not bool:
                raise TypeError("receipt boolean fields must use exact bool values")
        if type(self.latency_ns) is not int or self.latency_ns < 0:
            raise ValueError("latency_ns must be a non-negative integer")
        if not isinstance(self.validation_checks, tuple) or not self.validation_checks:
            raise ValueError("validation_checks must be a non-empty tuple")
        if any(
            not isinstance(item, str) or _CHECK_CODE.fullmatch(item) is None
            for item in self.validation_checks
        ):
            raise ValueError("validation checks must be bounded safe codes")
        if self.request_views_persisted or self.exact_diffs_persisted:
            raise ValueError("the lightweight v1 receipt cannot persist request views or diffs")
        if self.edit_applied and self.effective_mode is not SentinelMode.ACTIVE:
            raise ValueError("only ACTIVE mode may apply an edit")
        if self.effective_mode is SentinelMode.OFF and (
            self.decision_kinds or self.would_edit or self.edit_applied
        ):
            raise ValueError("effective OFF receipts cannot retain decisions or edits")
        if self.bypass_reason is not None:
            if (
                self.effective_mode is not SentinelMode.OFF
                or self.policy_evaluated
                or self.validation_status is not SentinelValidationStatus.BYPASSED
                or self.fallback_reason is not None
            ):
                raise ValueError("bypass receipt semantics are inconsistent")
        elif self.validation_status is SentinelValidationStatus.BYPASSED:
            raise ValueError("BYPASSED validation requires a bypass reason")
        if self.validation_status is SentinelValidationStatus.FALLBACK_ORIGINAL:
            if self.fallback_reason is None:
                raise ValueError("fallback validation needs a typed fallback reason")
            if self.effective_mode is not SentinelMode.OFF:
                raise ValueError("fallback validation must force effective OFF")
        elif self.fallback_reason is not None:
            raise ValueError("non-fallback receipt cannot carry fallback_reason")
        if self.effective_mode is SentinelMode.SHADOW and self.edit_applied:
            raise ValueError("SHADOW mode cannot apply an edit")
        if not self.edit_applied and self.final_request_sha256 != self.raw_request_sha256:
            raise ValueError("non-applied receipts must bind final to Original")
        if self.would_edit != (self.candidate_request_sha256 != self.raw_request_sha256):
            raise ValueError("would_edit must match the candidate/raw hash difference")
        if self.edit_applied and (
            not self.would_edit
            or not self.policy_evaluated
            or self.validation_status is not SentinelValidationStatus.PASSED
        ):
            raise ValueError("applied edits require a validated material policy result")
        if self.call_role is SentinelCallRole.SENTINEL:
            if self.bypass_reason is not SentinelBypassReason.CALL_ROLE_SENTINEL:
                raise ValueError("sentinel-role calls must use the recursion bypass")
        elif self.bypass_reason is SentinelBypassReason.CALL_ROLE_SENTINEL:
            raise ValueError("actor calls cannot use the recursion bypass")
        if self.bypass_reason is SentinelBypassReason.MODE_OFF and (
            self.configured_mode is not SentinelMode.OFF
            or self.call_role is not SentinelCallRole.ACTOR
            or self.global_kill_switch_active
        ):
            raise ValueError("MODE_OFF bypass precedence is inconsistent")
        if self.bypass_reason is SentinelBypassReason.GLOBAL_KILL_SWITCH and (
            self.call_role is not SentinelCallRole.ACTOR or not self.global_kill_switch_active
        ):
            raise ValueError("kill-switch bypass precedence is inconsistent")
        if (
            self.configured_mode is SentinelMode.OFF
            and self.call_role is SentinelCallRole.ACTOR
            and not self.global_kill_switch_active
            and self.bypass_reason is not SentinelBypassReason.MODE_OFF
        ):
            raise ValueError("configured OFF actor calls must use MODE_OFF bypass")
        if (
            self.call_role is SentinelCallRole.ACTOR
            and self.global_kill_switch_active
            and self.validation_status is SentinelValidationStatus.BYPASSED
            and self.bypass_reason is not SentinelBypassReason.GLOBAL_KILL_SWITCH
        ):
            raise ValueError("active kill switch must use its actor bypass")
        if self.effective_mode in {SentinelMode.SHADOW, SentinelMode.ACTIVE}:
            if (
                self.configured_mode is not self.effective_mode
                or self.bypass_reason is not None
                or not self.policy_evaluated
                or self.validation_status is not SentinelValidationStatus.PASSED
            ):
                raise ValueError("semantic effective mode receipt is inconsistent")
        if self.validation_status is SentinelValidationStatus.PASSED and (
            self.effective_mode not in {SentinelMode.SHADOW, SentinelMode.ACTIVE}
            or not self.policy_evaluated
        ):
            raise ValueError("PASSED validation requires evaluated SHADOW or ACTIVE mode")
        if self.validation_status is SentinelValidationStatus.PASSED and (
            self.global_kill_switch_active
            or self.history_codec_id is None
            or self.policy_id is None
            or not self.decision_kinds
        ):
            raise ValueError("PASSED validation requires kill-off, codec, policy, and decisions")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "logical_call_id": self.logical_call_id,
            "host_id": self.host_id,
            "call_role": self.call_role.value,
            "configured_mode": self.configured_mode.value,
            "effective_mode": self.effective_mode.value,
            "bypass_reason": None if self.bypass_reason is None else self.bypass_reason.value,
            "global_kill_switch_active": self.global_kill_switch_active,
            "history_codec_id": self.history_codec_id,
            "history_codec_contract_version": self.history_codec_contract_version,
            "policy_id": self.policy_id,
            "policy_output_sha256": self.policy_output_sha256,
            "raw_request_sha256": self.raw_request_sha256,
            "candidate_request_sha256": self.candidate_request_sha256,
            "final_request_sha256": self.final_request_sha256,
            "exact_diff_sha256": self.exact_diff_sha256,
            "decision_kinds": [item.value for item in self.decision_kinds],
            "policy_evaluated": self.policy_evaluated,
            "would_edit": self.would_edit,
            "edit_applied": self.edit_applied,
            "fallback_reason": (
                None if self.fallback_reason is None else self.fallback_reason.value
            ),
            "validation_status": self.validation_status.value,
            "validation_checks": list(self.validation_checks),
            "latency_ns": self.latency_ns,
            "request_views_persisted": self.request_views_persisted,
            "exact_diffs_persisted": self.exact_diffs_persisted,
        }


@dataclass(frozen=True)
class SentinelResult:
    """Immutable cached result exposing fresh raw/final request objects on access."""

    receipt: SentinelReceipt
    _raw_request_json: bytes = field(repr=False)
    _candidate_request_json: bytes = field(repr=False)
    _final_request_json: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for name in ("_raw_request_json", "_candidate_request_json", "_final_request_json"):
            value = getattr(self, name)
            if not isinstance(value, bytes):
                raise TypeError(f"{name} must be canonical JSON bytes")
            decoded = cast(JsonValue, json.loads(value))
            if canonical_json_bytes(decoded) != value:
                raise ValueError(f"{name} must use canonical JSON encoding")
        raw = cast(JsonValue, json.loads(self._raw_request_json))
        candidate = cast(JsonValue, json.loads(self._candidate_request_json))
        final = cast(JsonValue, json.loads(self._final_request_json))
        if canonical_sha256(raw) != self.receipt.raw_request_sha256:
            raise ValueError("raw request bytes do not match the receipt hash")
        if canonical_sha256(candidate) != self.receipt.candidate_request_sha256:
            raise ValueError("candidate request bytes do not match the receipt hash")
        if canonical_sha256(final) != self.receipt.final_request_sha256:
            raise ValueError("final request bytes do not match the receipt hash")
        expected_final = candidate if self.receipt.edit_applied else raw
        if final != expected_final:
            raise ValueError("final request bytes do not match receipt selection semantics")

    @property
    def raw_request(self) -> JsonValue:
        return cast(JsonValue, json.loads(self._raw_request_json))

    @property
    def candidate_request(self) -> JsonValue:
        return cast(JsonValue, json.loads(self._candidate_request_json))

    @property
    def final_request(self) -> JsonValue:
        return cast(JsonValue, json.loads(self._final_request_json))

    @property
    def use_transformed_request(self) -> bool:
        return self.receipt.edit_applied


@runtime_checkable
class SentinelReceiptTransaction(Protocol):
    """One admitted, single-use receipt publication transaction."""

    def commit(self, receipt: SentinelReceipt) -> None: ...

    def abort(self) -> None: ...


@runtime_checkable
class SentinelReceiptSink(Protocol):
    """Admit durable receipt storage before any semantic Sentinel work."""

    def begin(self, logical_call_id: str) -> SentinelReceiptTransaction: ...


class SentinelContractError(ValueError):
    """Local configuration/usage fault distinct from a typed Original fallback."""


__all__ = [
    "SENTINEL_RECEIPT_SCHEMA_VERSION",
    "SENTINEL_RUNTIME_CONTRACT_VERSION",
    "SentinelBypassReason",
    "SentinelCallRole",
    "SentinelContext",
    "SentinelContractError",
    "SentinelDecision",
    "SentinelDecisionKind",
    "SentinelFallbackReason",
    "SentinelHostConfig",
    "SentinelMode",
    "SentinelPolicy",
    "SentinelPolicyOutput",
    "SentinelReceipt",
    "SentinelReceiptSink",
    "SentinelReceiptTransaction",
    "SentinelResult",
    "SentinelValidationStatus",
]
