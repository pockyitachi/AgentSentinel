"""Cancellable, process-isolated OpenAI provider-attempt boundary.

CPU tests use a closed module-owned worker with no request, secret, model, or
network access.  The production runner accepts only the exact sealed
post-preflight factory and case lease.  Its child process, never the actor
process, owns the secret and OpenAI client.  Construction requires the exact
owner-confirmed manifest, sealed preflight, case lease, stage, and pricing
binding; only an explicit call dispatches the child to the provider.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Final, cast

from mobile_world.runtime.sentinel.r2_4.live_run import OpenAIResponsesStageV1, OpenAIRoleV1
from mobile_world.runtime.sentinel.r2_4.production_preflight import (
    CaseExecutionLeaseV1,
    CaseExecutionScopeV1,
    ProductionPostPreflightFactoryV1,
    case_execution_lease_sha256,
)

LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-attempt-authority/v1"
)
LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt/v1"
LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel-r2.4-live-attempt-receipt-root/v1"
)
OPENAI_REQUEST_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-openai-provider-request/v1"
HISTORY_POLICY_REQUEST_SCHEMA_VERSION = OPENAI_REQUEST_SCHEMA_VERSION
LIVE_ATTEMPT_PRICING_SCHEMA_VERSION = "mobileworld.runtime.sentinel-r2.4-live-pricing/v1"

_ID: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_FAILURE_CODE: Final[re.Pattern[str]] = re.compile(r"[A-Z][A-Z0-9_]{0,95}")
_CPU_INPUT_TOKENS: Final[int] = 7
_CPU_OUTPUT_TOKENS: Final[int] = 3
_CPU_COST_USD_MICROS: Final[int] = 1
_MAX_DURATION_NS: Final[int] = 7 * 24 * 60 * 60 * 1_000_000_000
_MAX_PROVIDER_REQUEST_BYTES: Final[int] = 8 * 1024 * 1024
_REQUEST_SEAL: Final[object] = object()


class LiveAttemptError(RuntimeError):
    """Typed failure raised before an attempt can be safely represented."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LiveAttemptRoleV1(StrEnum):
    """Closed roles; production dispatch permits RUBRIC and HISTORY_POLICY."""

    RUBRIC = "RUBRIC"
    HISTORY_POLICY = "HISTORY_POLICY"
    ACTOR = "ACTOR"


class LiveAttemptStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    CANCELLED_PRE_DISPATCH = "CANCELLED_PRE_DISPATCH"
    CANCELLED_POST_DISPATCH = "CANCELLED_POST_DISPATCH"
    TERMINATION_UNCONFIRMED = "TERMINATION_UNCONFIRMED"
    FAILED = "FAILED"


class LiveAttemptCostStatusV1(StrEnum):
    EXACT = "EXACT"
    UNKNOWN = "UNKNOWN"


class LiveAttemptTerminationV1(StrEnum):
    NONE = "NONE"
    COOPERATIVE = "COOPERATIVE"
    TERM = "TERM"
    KILL = "KILL"
    UNCONFIRMED = "UNCONFIRMED"


class LiveAttemptExecutionKindV1(StrEnum):
    CPU_FIXED_SUBPROCESS = "CPU_FIXED_SUBPROCESS"
    OPENAI_RESPONSES_CHILD_PROCESS = "OPENAI_RESPONSES_CHILD_PROCESS"


class CpuFixedAttemptScriptV1(StrEnum):
    """Module-owned, data-only child behaviors used by CPU tests."""

    COMPLETE_ONCE = "COMPLETE_ONCE"
    BLOCK_AFTER_DISPATCH = "BLOCK_AFTER_DISPATCH"
    IGNORE_TERM_AFTER_DISPATCH = "IGNORE_TERM_AFTER_DISPATCH"


def _require_id(value: object, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise LiveAttemptError("INVALID_ATTEMPT_AUTHORITY", f"{label} is invalid")
    return value


def _require_sha256(value: object, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise LiveAttemptError("INVALID_SHA256", f"{label} is not lowercase SHA-256")
    return value


def _require_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise LiveAttemptError("INVALID_INTEGER", f"{label} is outside its bound")
    return value


def _canonical_sha256(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveAttemptError("CANONICALIZATION_FAILED", "attempt projection is invalid") from exc
    return hashlib.sha256(raw).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LiveAttemptError("CANONICALIZATION_FAILED", "provider request is invalid") from exc


@dataclass(frozen=True, slots=True)
class CanonicalHistoryPolicyRequestV1:
    """In-memory canonical OpenAI request; receipts retain only its hash."""

    canonical_bytes: bytes
    request_sha256: str
    byte_count: int
    schema_version: str = HISTORY_POLICY_REQUEST_SCHEMA_VERSION
    _seal: object = _REQUEST_SEAL

    def __post_init__(self) -> None:
        if self._seal is not _REQUEST_SEAL:
            raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request snapshot is untrusted")
        if self.schema_version != HISTORY_POLICY_REQUEST_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "provider request schema differs")
        if type(self.canonical_bytes) is not bytes:
            raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request bytes are mutable")
        if (
            type(self.byte_count) is not int
            or self.byte_count != len(self.canonical_bytes)
            or not 2 <= self.byte_count <= _MAX_PROVIDER_REQUEST_BYTES
        ):
            raise LiveAttemptError("INVALID_PROVIDER_REQUEST", "request byte count differs")
        if hashlib.sha256(self.canonical_bytes).hexdigest() != _require_sha256(
            self.request_sha256, "request_sha256"
        ):
            raise LiveAttemptError("REQUEST_HASH_DRIFT", "provider request hash differs")
        try:
            parsed = json.loads(self.canonical_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
            raise LiveAttemptError("INVALID_PROVIDER_REQUEST", "request JSON is invalid") from exc
        if type(parsed) is not dict or _canonical_bytes(parsed) != self.canonical_bytes:
            raise LiveAttemptError(
                "NONCANONICAL_PROVIDER_REQUEST", "request bytes are not canonical"
            )


def build_canonical_history_policy_request(
    request_kwargs: dict[str, object],
) -> CanonicalHistoryPolicyRequestV1:
    if type(request_kwargs) is not dict:
        raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request must be an exact dict")
    raw = _canonical_bytes(request_kwargs)
    return CanonicalHistoryPolicyRequestV1(
        canonical_bytes=raw,
        request_sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        _seal=_REQUEST_SEAL,
    )


def snapshot_canonical_history_policy_request(
    value: CanonicalHistoryPolicyRequestV1,
) -> CanonicalHistoryPolicyRequestV1:
    if type(value) is not CanonicalHistoryPolicyRequestV1:
        raise LiveAttemptError("UNTRUSTED_PROVIDER_REQUEST", "request type differs")
    return CanonicalHistoryPolicyRequestV1(
        canonical_bytes=bytes(value.canonical_bytes),
        request_sha256=value.request_sha256,
        byte_count=value.byte_count,
        schema_version=value.schema_version,
        _seal=_REQUEST_SEAL,
    )


CanonicalOpenAIRequestV1 = CanonicalHistoryPolicyRequestV1
build_canonical_openai_request = build_canonical_history_policy_request
snapshot_canonical_openai_request = snapshot_canonical_history_policy_request


@dataclass(frozen=True, slots=True)
class LiveAttemptPricingV1:
    """Explicit operator-pinned token price table used for bounded accounting."""

    pricing_id: str
    model: str
    input_usd_micros_per_million_tokens: int
    cached_input_usd_micros_per_million_tokens: int
    output_usd_micros_per_million_tokens: int
    source_sha256: str
    effective_at_utc: str
    rounding_policy: str = "CEIL_PER_ATTEMPT_USD_MICRO"
    schema_version: str = LIVE_ATTEMPT_PRICING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_PRICING_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "pricing schema differs")
        if self.rounding_policy != "CEIL_PER_ATTEMPT_USD_MICRO":
            raise LiveAttemptError("INVALID_PRICING", "pricing rounding policy differs")
        _require_id(self.pricing_id, "pricing_id")
        _require_id(self.model, "model")
        _require_sha256(self.source_sha256, "source_sha256")
        for value, label in (
            (self.input_usd_micros_per_million_tokens, "input token price"),
            (self.cached_input_usd_micros_per_million_tokens, "cached input token price"),
            (self.output_usd_micros_per_million_tokens, "output token price"),
        ):
            _require_int(value, label, 0, 1_000_000_000_000)
        if type(self.effective_at_utc) is not str:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is invalid")
        try:
            parsed = datetime.strptime(self.effective_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as exc:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is invalid") from exc
        if parsed.year < 2020:
            raise LiveAttemptError("INVALID_PRICING", "pricing timestamp is implausible")


def live_attempt_pricing_projection(value: LiveAttemptPricingV1) -> dict[str, object]:
    if type(value) is not LiveAttemptPricingV1:
        raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
    trusted = LiveAttemptPricingV1(
        pricing_id=value.pricing_id,
        model=value.model,
        input_usd_micros_per_million_tokens=value.input_usd_micros_per_million_tokens,
        cached_input_usd_micros_per_million_tokens=(
            value.cached_input_usd_micros_per_million_tokens
        ),
        output_usd_micros_per_million_tokens=value.output_usd_micros_per_million_tokens,
        source_sha256=value.source_sha256,
        effective_at_utc=value.effective_at_utc,
        rounding_policy=value.rounding_policy,
        schema_version=value.schema_version,
    )
    return {
        "effective_at_utc": trusted.effective_at_utc,
        "input_usd_micros_per_million_tokens": (trusted.input_usd_micros_per_million_tokens),
        "cached_input_usd_micros_per_million_tokens": (
            trusted.cached_input_usd_micros_per_million_tokens
        ),
        "model": trusted.model,
        "output_usd_micros_per_million_tokens": (trusted.output_usd_micros_per_million_tokens),
        "pricing_id": trusted.pricing_id,
        "rounding_policy": trusted.rounding_policy,
        "schema_version": trusted.schema_version,
        "source_sha256": trusted.source_sha256,
    }


def live_attempt_pricing_sha256(value: LiveAttemptPricingV1) -> str:
    return _canonical_sha256(live_attempt_pricing_projection(value))


def snapshot_live_attempt_pricing(value: LiveAttemptPricingV1) -> LiveAttemptPricingV1:
    if type(value) is not LiveAttemptPricingV1:
        raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
    return LiveAttemptPricingV1(
        pricing_id=value.pricing_id,
        model=value.model,
        input_usd_micros_per_million_tokens=value.input_usd_micros_per_million_tokens,
        cached_input_usd_micros_per_million_tokens=(
            value.cached_input_usd_micros_per_million_tokens
        ),
        output_usd_micros_per_million_tokens=value.output_usd_micros_per_million_tokens,
        source_sha256=value.source_sha256,
        effective_at_utc=value.effective_at_utc,
        rounding_policy=value.rounding_policy,
        schema_version=value.schema_version,
    )


@dataclass(frozen=True, slots=True)
class LiveAttemptAuthorityV1:
    """Data binding for one call; this value alone grants no execution right."""

    attempt_id: str
    role: LiveAttemptRoleV1
    manifest_sha256: str
    preflight_sha256: str
    case_execution_lease_sha256: str
    stage_sha256: str
    case_id: str
    logical_call_id: str
    actor_request_sha256: str
    request_sha256: str
    transport_binding_sha256: str
    pricing_binding_sha256: str
    deadline_monotonic_ns: int
    max_cost_usd_micros: int
    max_output_tokens: int
    schema_version: str = LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "unknown attempt authority schema")
        _require_id(self.attempt_id, "attempt_id")
        if type(self.role) is not LiveAttemptRoleV1:
            raise LiveAttemptError("INVALID_ATTEMPT_AUTHORITY", "role is untrusted")
        for value, label in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.preflight_sha256, "preflight_sha256"),
            (self.case_execution_lease_sha256, "case_execution_lease_sha256"),
            (self.stage_sha256, "stage_sha256"),
            (self.actor_request_sha256, "actor_request_sha256"),
            (self.request_sha256, "request_sha256"),
            (self.transport_binding_sha256, "transport_binding_sha256"),
            (self.pricing_binding_sha256, "pricing_binding_sha256"),
        ):
            _require_sha256(value, label)
        _require_id(self.case_id, "case_id")
        _require_id(self.logical_call_id, "logical_call_id")
        _require_int(
            self.deadline_monotonic_ns,
            "deadline_monotonic_ns",
            1,
            (1 << 63) - 1,
        )
        _require_int(self.max_cost_usd_micros, "max_cost_usd_micros", 0, 100_000_000_000)
        _require_int(self.max_output_tokens, "max_output_tokens", 1, 1_000_000)


def snapshot_live_attempt_authority(value: LiveAttemptAuthorityV1) -> LiveAttemptAuthorityV1:
    if type(value) is not LiveAttemptAuthorityV1:
        raise LiveAttemptError("UNTRUSTED_TYPE", "attempt authority has an untrusted type")
    return LiveAttemptAuthorityV1(
        attempt_id=value.attempt_id,
        role=value.role,
        manifest_sha256=value.manifest_sha256,
        preflight_sha256=value.preflight_sha256,
        case_execution_lease_sha256=value.case_execution_lease_sha256,
        stage_sha256=value.stage_sha256,
        case_id=value.case_id,
        logical_call_id=value.logical_call_id,
        actor_request_sha256=value.actor_request_sha256,
        request_sha256=value.request_sha256,
        transport_binding_sha256=value.transport_binding_sha256,
        pricing_binding_sha256=value.pricing_binding_sha256,
        deadline_monotonic_ns=value.deadline_monotonic_ns,
        max_cost_usd_micros=value.max_cost_usd_micros,
        max_output_tokens=value.max_output_tokens,
        schema_version=value.schema_version,
    )


def live_attempt_authority_projection(value: LiveAttemptAuthorityV1) -> dict[str, object]:
    trusted = snapshot_live_attempt_authority(value)
    return {
        "attempt_id": trusted.attempt_id,
        "actor_request_sha256": trusted.actor_request_sha256,
        "case_id": trusted.case_id,
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "deadline_monotonic_ns": trusted.deadline_monotonic_ns,
        "logical_call_id": trusted.logical_call_id,
        "manifest_sha256": trusted.manifest_sha256,
        "max_cost_usd_micros": trusted.max_cost_usd_micros,
        "max_output_tokens": trusted.max_output_tokens,
        "preflight_sha256": trusted.preflight_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "request_sha256": trusted.request_sha256,
        "role": trusted.role.value,
        "schema_version": trusted.schema_version,
        "stage_sha256": trusted.stage_sha256,
        "transport_binding_sha256": trusted.transport_binding_sha256,
    }


def live_attempt_authority_sha256(value: LiveAttemptAuthorityV1) -> str:
    return _canonical_sha256(live_attempt_authority_projection(value))


@dataclass(frozen=True, slots=True)
class LiveAttemptReceiptV1:
    """Terminal, content-free proof for one bounded provider attempt."""

    attempt_id: str
    role: LiveAttemptRoleV1
    authority_sha256: str
    manifest_sha256: str
    preflight_sha256: str
    case_execution_lease_sha256: str
    stage_sha256: str
    case_id: str
    logical_call_id: str
    actor_request_sha256: str
    request_sha256: str
    transport_binding_sha256: str
    pricing_binding_sha256: str
    execution_kind: LiveAttemptExecutionKindV1
    status: LiveAttemptStatusV1
    dispatch_count: int
    response_envelope_sha256: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_status: LiveAttemptCostStatusV1
    cost_usd_micros: int | None
    cancellation_requested: bool
    termination: LiveAttemptTerminationV1
    worker_pid: int | None
    worker_exit_code: int | None
    worker_reaped: bool
    late_output_detected: bool
    duration_ns: int
    failure_code: str | None
    schema_version: str = LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION:
            raise LiveAttemptError("UNKNOWN_SCHEMA_VERSION", "unknown attempt receipt schema")
        _require_id(self.attempt_id, "attempt_id")
        if type(self.role) is not LiveAttemptRoleV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "receipt role is untrusted")
        for value, label in (
            (self.authority_sha256, "authority_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
            (self.preflight_sha256, "preflight_sha256"),
            (self.case_execution_lease_sha256, "case_execution_lease_sha256"),
            (self.stage_sha256, "stage_sha256"),
            (self.actor_request_sha256, "actor_request_sha256"),
            (self.request_sha256, "request_sha256"),
            (self.transport_binding_sha256, "transport_binding_sha256"),
            (self.pricing_binding_sha256, "pricing_binding_sha256"),
        ):
            _require_sha256(value, label)
        _require_id(self.case_id, "case_id")
        _require_id(self.logical_call_id, "logical_call_id")
        if type(self.execution_kind) is not LiveAttemptExecutionKindV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "execution kind is untrusted")
        if type(self.status) is not LiveAttemptStatusV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "attempt status is untrusted")
        _require_int(self.dispatch_count, "dispatch_count", 0, 1)
        if self.response_envelope_sha256 is not None:
            _require_sha256(self.response_envelope_sha256, "response_envelope_sha256")
        for token_value, label in (
            (self.input_tokens, "input_tokens"),
            (self.cached_input_tokens, "cached_input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if token_value is not None:
                _require_int(token_value, label, 0, 100_000_000)
        if type(self.cost_status) is not LiveAttemptCostStatusV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "cost status is untrusted")
        if self.cost_status is LiveAttemptCostStatusV1.EXACT:
            if self.cost_usd_micros is None:
                raise LiveAttemptError("INCOMPLETE_ACCOUNTING", "exact cost is absent")
            _require_int(self.cost_usd_micros, "cost_usd_micros", 0, 100_000_000_000)
        elif self.cost_usd_micros is not None:
            raise LiveAttemptError("FALSE_ACCOUNTING_CLAIM", "unknown cost cannot carry a value")
        for bool_value, label in (
            (self.cancellation_requested, "cancellation_requested"),
            (self.worker_reaped, "worker_reaped"),
            (self.late_output_detected, "late_output_detected"),
        ):
            if type(bool_value) is not bool:
                raise LiveAttemptError("UNTRUSTED_TYPE", f"{label} is not an exact bool")
        if type(self.termination) is not LiveAttemptTerminationV1:
            raise LiveAttemptError("UNTRUSTED_TYPE", "termination kind is untrusted")
        if self.worker_pid is not None:
            _require_int(self.worker_pid, "worker_pid", 1, (1 << 31) - 1)
        if self.worker_exit_code is not None and type(self.worker_exit_code) is not int:
            raise LiveAttemptError("INVALID_INTEGER", "worker_exit_code is invalid")
        _require_int(self.duration_ns, "duration_ns", 0, _MAX_DURATION_NS)
        if self.failure_code is not None and (
            type(self.failure_code) is not str or _FAILURE_CODE.fullmatch(self.failure_code) is None
        ):
            raise LiveAttemptError("INVALID_FAILURE_CODE", "failure_code is invalid")
        self._validate_state()

    def _validate_state(self) -> None:
        tokens = (
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            self.total_tokens,
        )
        complete_response_accounting = (
            self.response_envelope_sha256 is not None
            and all(value is not None for value in tokens)
            and self.cost_status is LiveAttemptCostStatusV1.EXACT
            and self.cost_usd_micros is not None
        )
        if self.worker_reaped != (self.worker_exit_code is not None):
            raise LiveAttemptError("INVALID_TERMINATION_PROOF", "reap and exit code disagree")
        if self.total_tokens is not None and (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise LiveAttemptError("INVALID_TOKEN_CENSUS", "token counts are inconsistent")
        if self.cached_input_tokens is not None and (
            self.input_tokens is None or self.cached_input_tokens > self.input_tokens
        ):
            raise LiveAttemptError("INVALID_TOKEN_CENSUS", "cached input exceeds total input")
        if self.status is LiveAttemptStatusV1.COMPLETED:
            if (
                self.dispatch_count != 1
                or self.response_envelope_sha256 is None
                or any(value is None for value in tokens)
                or self.cost_status is not LiveAttemptCostStatusV1.EXACT
                or self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.worker_exit_code != 0
                or self.late_output_detected
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_COMPLETED_RECEIPT", "completed receipt is incomplete"
                )
            return
        if (
            self.response_envelope_sha256 is not None or any(value is not None for value in tokens)
        ) and not (
            self.status is LiveAttemptStatusV1.FAILED
            and self.failure_code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY"
            and complete_response_accounting
            and self.dispatch_count == 1
            and not self.cancellation_requested
            and self.termination is LiveAttemptTerminationV1.NONE
            and self.worker_reaped
        ):
            raise LiveAttemptError(
                "INVALID_NONCOMPLETED_RECEIPT", "failed attempt claims a response"
            )
        if self.status is LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH:
            if (
                self.dispatch_count != 0
                or self.cost_status is not LiveAttemptCostStatusV1.EXACT
                or self.cost_usd_micros != 0
                or not self.cancellation_requested
                or self.termination is LiveAttemptTerminationV1.NONE
                or not self.worker_reaped
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_CANCELLED_RECEIPT", "pre-dispatch cancellation is inconsistent"
                )
            return
        if self.status is LiveAttemptStatusV1.CANCELLED_POST_DISPATCH:
            if (
                self.dispatch_count != 1
                or self.cost_status is not LiveAttemptCostStatusV1.UNKNOWN
                or not self.cancellation_requested
                or self.termination
                not in {LiveAttemptTerminationV1.TERM, LiveAttemptTerminationV1.KILL}
                or not self.worker_reaped
                or self.failure_code is not None
            ):
                raise LiveAttemptError(
                    "INVALID_CANCELLED_RECEIPT", "post-dispatch cancellation is inconsistent"
                )
            return
        if self.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED:
            if (
                self.cost_status is not LiveAttemptCostStatusV1.UNKNOWN
                or not self.cancellation_requested
                or self.termination is not LiveAttemptTerminationV1.UNCONFIRMED
                or self.worker_reaped
                or self.worker_exit_code is not None
                or self.failure_code != "TERMINATION_UNCONFIRMED"
            ):
                raise LiveAttemptError(
                    "INVALID_TERMINATION_PROOF", "unconfirmed termination is inconsistent"
                )
            return
        if self.failure_code is None:
            raise LiveAttemptError("INVALID_FAILED_RECEIPT", "failed attempt needs a typed code")
        exact_failed_response = (
            self.failure_code == "PROVIDER_RESULT_EXCEEDS_AUTHORITY"
            and complete_response_accounting
        )
        expected_cost = (
            LiveAttemptCostStatusV1.EXACT
            if self.dispatch_count == 0 or exact_failed_response
            else LiveAttemptCostStatusV1.UNKNOWN
        )
        if (
            self.cost_status is not expected_cost
            or (self.dispatch_count == 0 and self.cost_usd_micros != 0)
            or (exact_failed_response and self.dispatch_count != 1)
        ):
            raise LiveAttemptError("FALSE_ACCOUNTING_CLAIM", "failed-attempt cost is inconsistent")

    @property
    def accounting_complete(self) -> bool:
        return self.cost_status is LiveAttemptCostStatusV1.EXACT

    @property
    def passed(self) -> bool:
        return self.status is LiveAttemptStatusV1.COMPLETED and self.accounting_complete


def snapshot_live_attempt_receipt(value: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
    if type(value) is not LiveAttemptReceiptV1:
        raise LiveAttemptError("UNTRUSTED_TYPE", "attempt receipt has an untrusted type")
    return LiveAttemptReceiptV1(
        attempt_id=value.attempt_id,
        role=value.role,
        authority_sha256=value.authority_sha256,
        manifest_sha256=value.manifest_sha256,
        preflight_sha256=value.preflight_sha256,
        case_execution_lease_sha256=value.case_execution_lease_sha256,
        stage_sha256=value.stage_sha256,
        case_id=value.case_id,
        logical_call_id=value.logical_call_id,
        actor_request_sha256=value.actor_request_sha256,
        request_sha256=value.request_sha256,
        transport_binding_sha256=value.transport_binding_sha256,
        pricing_binding_sha256=value.pricing_binding_sha256,
        execution_kind=value.execution_kind,
        status=value.status,
        dispatch_count=value.dispatch_count,
        response_envelope_sha256=value.response_envelope_sha256,
        input_tokens=value.input_tokens,
        cached_input_tokens=value.cached_input_tokens,
        output_tokens=value.output_tokens,
        total_tokens=value.total_tokens,
        cost_status=value.cost_status,
        cost_usd_micros=value.cost_usd_micros,
        cancellation_requested=value.cancellation_requested,
        termination=value.termination,
        worker_pid=value.worker_pid,
        worker_exit_code=value.worker_exit_code,
        worker_reaped=value.worker_reaped,
        late_output_detected=value.late_output_detected,
        duration_ns=value.duration_ns,
        failure_code=value.failure_code,
        schema_version=value.schema_version,
    )


def live_attempt_receipt_projection(value: LiveAttemptReceiptV1) -> dict[str, object]:
    trusted = snapshot_live_attempt_receipt(value)
    return {
        "actor_request_sha256": trusted.actor_request_sha256,
        "attempt_id": trusted.attempt_id,
        "authority_sha256": trusted.authority_sha256,
        "cancellation_requested": trusted.cancellation_requested,
        "case_id": trusted.case_id,
        "case_execution_lease_sha256": trusted.case_execution_lease_sha256,
        "cost_status": trusted.cost_status.value,
        "cost_usd_micros": trusted.cost_usd_micros,
        "dispatch_count": trusted.dispatch_count,
        "duration_ns": trusted.duration_ns,
        "execution_kind": trusted.execution_kind.value,
        "failure_code": trusted.failure_code,
        "input_tokens": trusted.input_tokens,
        "cached_input_tokens": trusted.cached_input_tokens,
        "late_output_detected": trusted.late_output_detected,
        "logical_call_id": trusted.logical_call_id,
        "manifest_sha256": trusted.manifest_sha256,
        "output_tokens": trusted.output_tokens,
        "preflight_sha256": trusted.preflight_sha256,
        "pricing_binding_sha256": trusted.pricing_binding_sha256,
        "request_sha256": trusted.request_sha256,
        "response_envelope_sha256": trusted.response_envelope_sha256,
        "role": trusted.role.value,
        "schema_version": trusted.schema_version,
        "stage_sha256": trusted.stage_sha256,
        "status": trusted.status.value,
        "termination": trusted.termination.value,
        "total_tokens": trusted.total_tokens,
        "transport_binding_sha256": trusted.transport_binding_sha256,
        "worker_exit_code": trusted.worker_exit_code,
        "worker_pid": trusted.worker_pid,
        "worker_reaped": trusted.worker_reaped,
    }


def live_attempt_receipt_sha256(value: LiveAttemptReceiptV1) -> str:
    return _canonical_sha256(live_attempt_receipt_projection(value))


def live_attempt_receipt_root_sha256(values: tuple[LiveAttemptReceiptV1, ...]) -> str:
    """Bind an ordered, duplicate-free terminal-attempt census."""

    if type(values) is not tuple or any(
        type(value) is not LiveAttemptReceiptV1 for value in values
    ):
        raise LiveAttemptError("UNTRUSTED_TYPE", "receipt root requires exact receipt values")
    trusted = tuple(snapshot_live_attempt_receipt(value) for value in values)
    attempt_ids = tuple(value.attempt_id for value in trusted)
    if len(set(attempt_ids)) != len(attempt_ids):
        raise LiveAttemptError("DUPLICATE_ATTEMPT_ID", "receipt root repeats an attempt")
    return _canonical_sha256(
        {
            "receipt_sha256s": [live_attempt_receipt_sha256(value) for value in trusted],
            "schema_version": LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION,
        }
    )


class MemoryLiveAttemptReceiptSinkV1:
    """Append-only, in-memory CPU sink with start-before-dispatch admission."""

    def __init__(self) -> None:
        self._started: dict[str, LiveAttemptAuthorityV1] = {}
        self._terminal: dict[str, LiveAttemptReceiptV1] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def _reserve(self, authority: LiveAttemptAuthorityV1) -> None:
        trusted = snapshot_live_attempt_authority(authority)
        with self._lock:
            if trusted.attempt_id in self._started:
                raise LiveAttemptError("DUPLICATE_ATTEMPT_ID", "attempt ID was already admitted")
            self._started[trusted.attempt_id] = trusted
            self._order.append(trusted.attempt_id)

    def _commit(self, receipt: LiveAttemptReceiptV1) -> None:
        trusted = snapshot_live_attempt_receipt(receipt)
        with self._lock:
            authority = self._started.get(trusted.attempt_id)
            if authority is None:
                raise LiveAttemptError("ATTEMPT_NOT_ADMITTED", "attempt has no start admission")
            expected_fields = (
                authority.role,
                authority.manifest_sha256,
                authority.preflight_sha256,
                authority.case_execution_lease_sha256,
                authority.stage_sha256,
                authority.case_id,
                authority.logical_call_id,
                authority.actor_request_sha256,
                authority.request_sha256,
                authority.transport_binding_sha256,
                authority.pricing_binding_sha256,
            )
            receipt_fields = (
                trusted.role,
                trusted.manifest_sha256,
                trusted.preflight_sha256,
                trusted.case_execution_lease_sha256,
                trusted.stage_sha256,
                trusted.case_id,
                trusted.logical_call_id,
                trusted.actor_request_sha256,
                trusted.request_sha256,
                trusted.transport_binding_sha256,
                trusted.pricing_binding_sha256,
            )
            if (
                live_attempt_authority_sha256(authority) != trusted.authority_sha256
                or expected_fields != receipt_fields
            ):
                raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "terminal receipt changed authority")
            if trusted.attempt_id in self._terminal:
                raise LiveAttemptError("DUPLICATE_TERMINAL_RECEIPT", "attempt is already terminal")
            self._terminal[trusted.attempt_id] = trusted

    @property
    def started_count(self) -> int:
        with self._lock:
            return len(self._started)

    @property
    def terminal_count(self) -> int:
        with self._lock:
            return len(self._terminal)

    @property
    def receipts(self) -> tuple[LiveAttemptReceiptV1, ...]:
        with self._lock:
            return tuple(
                snapshot_live_attempt_receipt(self._terminal[attempt_id])
                for attempt_id in self._order
                if attempt_id in self._terminal
            )

    @property
    def receipt_root_sha256(self) -> str:
        return live_attempt_receipt_root_sha256(self.receipts)

    def receipt_for(self, attempt_id: str) -> LiveAttemptReceiptV1 | None:
        _require_id(attempt_id, "attempt_id")
        with self._lock:
            value = self._terminal.get(attempt_id)
            return None if value is None else snapshot_live_attempt_receipt(value)


def _cpu_response_sha256(authority_sha256: str, request_sha256: str) -> str:
    return _canonical_sha256(
        {
            "authority_sha256": authority_sha256,
            "fixed_cpu_output": "ok",
            "request_sha256": request_sha256,
        }
    )


def _cpu_fixed_attempt_worker(
    connection: Connection,
    script_value: str,
    authority_sha256: str,
    request_sha256: str,
) -> None:
    """Run one closed CPU behavior; arguments cannot carry code, argv, or secrets."""

    try:
        script = CpuFixedAttemptScriptV1(script_value)
        if script is CpuFixedAttemptScriptV1.IGNORE_TERM_AFTER_DISPATCH:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        connection.send(("READY", authority_sha256))
        command = connection.recv()
        if command != ("DISPATCH", authority_sha256):
            return
        connection.send(("DISPATCHED", authority_sha256))
        if script is CpuFixedAttemptScriptV1.COMPLETE_ONCE:
            connection.send(
                (
                    "COMPLETED",
                    authority_sha256,
                    _cpu_response_sha256(authority_sha256, request_sha256),
                    _CPU_INPUT_TOKENS,
                    _CPU_OUTPUT_TOKENS,
                    _CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                    _CPU_COST_USD_MICROS,
                )
            )
            return
        while True:
            signal.pause()
    finally:
        connection.close()


@dataclass(frozen=True, slots=True)
class _StopResult:
    termination: LiveAttemptTerminationV1
    worker_reaped: bool
    worker_exit_code: int | None


class CpuFixedLiveAttemptHandleV1:
    """One CPU attempt handle with a concurrent-safe cancellation entry point."""

    def __init__(
        self,
        *,
        authority: LiveAttemptAuthorityV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        process: BaseProcess | None,
        connection: Connection | None,
        started_ns: int,
        cancel_grace_seconds: float,
        terminal_receipt: LiveAttemptReceiptV1 | None = None,
    ) -> None:
        self._authority = snapshot_live_attempt_authority(authority)
        self._authority_sha256 = live_attempt_authority_sha256(self._authority)
        self._sink = sink
        self._process = process
        self._connection = connection
        self._started_ns = started_ns
        self._cancel_grace_seconds = cancel_grace_seconds
        self._terminal_receipt = terminal_receipt
        self._dispatch_count = 0
        self._dispatch_command_sent = False
        self._execute_started = False
        self._cancel_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._finalize_lock = threading.Lock()

    @property
    def dispatch_count(self) -> int:
        with self._state_lock:
            return self._dispatch_count

    @property
    def worker_alive(self) -> bool:
        with self._state_lock:
            process = self._process
            terminal = self._terminal_receipt
        return terminal is None and process is not None and process.is_alive()

    @property
    def terminal_receipt(self) -> LiveAttemptReceiptV1 | None:
        with self._state_lock:
            value = self._terminal_receipt
        return None if value is None else snapshot_live_attempt_receipt(value)

    def _receive(self, timeout_seconds: float) -> tuple[object, ...] | None:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return None
            try:
                if not connection.poll(max(0.0, timeout_seconds)):
                    return None
                value = connection.recv()
            except (EOFError, OSError):
                return None
        return value if type(value) is tuple else ("INVALID",)

    def _send(self, value: tuple[str, str]) -> bool:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.send(value)
            except (BrokenPipeError, EOFError, OSError):
                return False
        return True

    def _drain(self) -> tuple[tuple[object, ...], ...]:
        messages: list[tuple[object, ...]] = []
        while True:
            value = self._receive(0.0)
            if value is None:
                return tuple(messages)
            messages.append(value)

    def _observe_dispatch(self, messages: tuple[tuple[object, ...], ...]) -> None:
        expected = self._authority_sha256
        if any(message == ("DISPATCHED", expected) for message in messages):
            with self._state_lock:
                self._dispatch_count = 1

    def _stop_worker(self, *, cooperative: bool) -> _StopResult:
        process = self._process
        if process is None:
            return _StopResult(LiveAttemptTerminationV1.COOPERATIVE, True, 0)
        if not process.is_alive():
            process.join(0)
            return _StopResult(
                LiveAttemptTerminationV1.COOPERATIVE,
                True,
                process.exitcode,
            )
        if cooperative:
            process.join(self._cancel_grace_seconds)
            if not process.is_alive():
                return _StopResult(
                    LiveAttemptTerminationV1.COOPERATIVE,
                    True,
                    process.exitcode,
                )
        process.terminate()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.TERM, True, process.exitcode)
        process.kill()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.KILL, True, process.exitcode)
        return _StopResult(LiveAttemptTerminationV1.UNCONFIRMED, False, None)

    def _make_receipt(
        self,
        *,
        status: LiveAttemptStatusV1,
        dispatch_count: int,
        response_envelope_sha256: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        cost_status: LiveAttemptCostStatusV1,
        cost_usd_micros: int | None,
        cancellation_requested: bool,
        stop: _StopResult,
        late_output_detected: bool = False,
        failure_code: str | None = None,
    ) -> LiveAttemptReceiptV1:
        authority = self._authority
        process = self._process
        return LiveAttemptReceiptV1(
            attempt_id=authority.attempt_id,
            role=authority.role,
            authority_sha256=self._authority_sha256,
            manifest_sha256=authority.manifest_sha256,
            preflight_sha256=authority.preflight_sha256,
            case_execution_lease_sha256=authority.case_execution_lease_sha256,
            stage_sha256=authority.stage_sha256,
            case_id=authority.case_id,
            logical_call_id=authority.logical_call_id,
            actor_request_sha256=authority.actor_request_sha256,
            request_sha256=authority.request_sha256,
            transport_binding_sha256=authority.transport_binding_sha256,
            pricing_binding_sha256=authority.pricing_binding_sha256,
            execution_kind=LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS,
            status=status,
            dispatch_count=dispatch_count,
            response_envelope_sha256=response_envelope_sha256,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_status=cost_status,
            cost_usd_micros=cost_usd_micros,
            cancellation_requested=cancellation_requested,
            termination=stop.termination,
            worker_pid=None if process is None else process.pid,
            worker_exit_code=stop.worker_exit_code,
            worker_reaped=stop.worker_reaped,
            late_output_detected=late_output_detected,
            duration_ns=min(_MAX_DURATION_NS, max(0, time.monotonic_ns() - self._started_ns)),
            failure_code=failure_code,
        )

    def _publish(self, receipt: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
        trusted = snapshot_live_attempt_receipt(receipt)
        self._sink._commit(trusted)
        with self._state_lock:
            self._terminal_receipt = trusted
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.close()
        return snapshot_live_attempt_receipt(trusted)

    def cancel_and_join(self) -> LiveAttemptReceiptV1:
        """Cancel once and return only after a terminal process observation."""

        self._cancel_requested.set()
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            with self._state_lock:
                command_sent = self._dispatch_command_sent
            self._send(("CANCEL", self._authority_sha256))
            stop = self._stop_worker(cooperative=not command_sent)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            late_output = any(
                len(message) > 0 and message[0] == "COMPLETED" for message in messages
            )
            if not stop.worker_reaped:
                return self._publish(
                    self._make_receipt(
                        status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
                        dispatch_count=dispatch_count,
                        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                        cost_usd_micros=None,
                        cancellation_requested=True,
                        stop=stop,
                        late_output_detected=late_output,
                        failure_code="TERMINATION_UNCONFIRMED",
                    )
                )
            status = (
                LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
                if dispatch_count == 0
                else LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
            )
            cost_status = (
                LiveAttemptCostStatusV1.EXACT
                if dispatch_count == 0
                else LiveAttemptCostStatusV1.UNKNOWN
            )
            return self._publish(
                self._make_receipt(
                    status=status,
                    dispatch_count=dispatch_count,
                    cost_status=cost_status,
                    cost_usd_micros=0 if dispatch_count == 0 else None,
                    cancellation_requested=True,
                    stop=stop,
                    late_output_detected=late_output,
                )
            )

    def _failed(self, code: str) -> LiveAttemptReceiptV1:
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            stop = self._stop_worker(cooperative=False)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            return self._publish(
                self._make_receipt(
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=dispatch_count,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if dispatch_count == 0
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=0 if dispatch_count == 0 else None,
                    cancellation_requested=False,
                    stop=stop,
                    late_output_detected=any(
                        len(message) > 0 and message[0] == "COMPLETED" for message in messages
                    ),
                    failure_code=code,
                )
            )

    def execute(self) -> LiveAttemptReceiptV1:
        """Dispatch the fixed worker once, enforcing the authority deadline."""

        with self._state_lock:
            if self._terminal_receipt is not None:
                return snapshot_live_attempt_receipt(self._terminal_receipt)
            if self._execute_started:
                raise LiveAttemptError("DUPLICATE_EXECUTION", "attempt execute was called twice")
            self._execute_started = True
            if self._cancel_requested.is_set():
                should_cancel = True
            else:
                should_cancel = False
                self._dispatch_command_sent = True
        if should_cancel:
            return self.cancel_and_join()
        if time.monotonic_ns() >= self._authority.deadline_monotonic_ns:
            return self.cancel_and_join()
        if not self._send(("DISPATCH", self._authority_sha256)):
            return self._failed("CPU_WORKER_DISPATCH_FAILED")

        expected_authority = self._authority_sha256
        expected_response = _cpu_response_sha256(expected_authority, self._authority.request_sha256)
        while True:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            if self._cancel_requested.is_set():
                return self.cancel_and_join()
            remaining_ns = self._authority.deadline_monotonic_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return self.cancel_and_join()
            message = self._receive(min(0.01, remaining_ns / 1_000_000_000))
            if message is None:
                process = self._process
                if process is not None and not process.is_alive():
                    return self._failed("CPU_WORKER_EXITED_WITHOUT_OUTPUT")
                continue
            if message == ("DISPATCHED", expected_authority):
                self._observe_dispatch((message,))
                continue
            if (
                len(message) == 7
                and message[0] == "COMPLETED"
                and message[1] == expected_authority
                and message[2] == expected_response
                and message[3:]
                == (
                    _CPU_INPUT_TOKENS,
                    _CPU_OUTPUT_TOKENS,
                    _CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                    _CPU_COST_USD_MICROS,
                )
            ):
                self._observe_dispatch((("DISPATCHED", expected_authority),))
                with self._finalize_lock:
                    terminal = self.terminal_receipt
                    if terminal is not None:
                        return terminal
                    process = self._process
                    if process is not None:
                        process.join(self._cancel_grace_seconds)
                    if process is not None and process.is_alive():
                        stop = self._stop_worker(cooperative=False)
                        return self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                stop=stop,
                                failure_code="CPU_WORKER_DID_NOT_EXIT",
                            )
                        )
                    stop = _StopResult(
                        termination=LiveAttemptTerminationV1.NONE,
                        worker_reaped=True,
                        worker_exit_code=0 if process is None else process.exitcode,
                    )
                    if (
                        self._authority.max_output_tokens < _CPU_OUTPUT_TOKENS
                        or self._authority.max_cost_usd_micros < _CPU_COST_USD_MICROS
                    ):
                        return self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                stop=stop,
                                failure_code="CPU_FIXED_RESULT_EXCEEDS_AUTHORITY",
                            )
                        )
                    return self._publish(
                        self._make_receipt(
                            status=LiveAttemptStatusV1.COMPLETED,
                            dispatch_count=1,
                            response_envelope_sha256=expected_response,
                            input_tokens=_CPU_INPUT_TOKENS,
                            cached_input_tokens=0,
                            output_tokens=_CPU_OUTPUT_TOKENS,
                            total_tokens=_CPU_INPUT_TOKENS + _CPU_OUTPUT_TOKENS,
                            cost_status=LiveAttemptCostStatusV1.EXACT,
                            cost_usd_micros=_CPU_COST_USD_MICROS,
                            cancellation_requested=False,
                            stop=stop,
                        )
                    )
            return self._failed("CPU_WORKER_PROTOCOL_VIOLATION")


class CpuFixedLiveAttemptRunnerV1:
    """Launch only the module-owned, no-I/O CPU worker above."""

    def __init__(
        self,
        *,
        sink: MemoryLiveAttemptReceiptSinkV1,
        startup_timeout_ms: int = 1_000,
        cancel_grace_ms: int = 50,
    ) -> None:
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "CPU runner requires the exact memory sink")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 10_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 10_000)
        if os.name != "posix":
            raise LiveAttemptError(
                "CPU_PROCESS_CONTROL_UNAVAILABLE", "POSIX process control required"
            )
        self._sink = sink
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000

    def begin(
        self,
        authority: LiveAttemptAuthorityV1,
        *,
        confirmed_authority_sha256: str,
        script: CpuFixedAttemptScriptV1,
    ) -> CpuFixedLiveAttemptHandleV1:
        """Admit and start one fixed worker without authorizing dispatch yet."""

        trusted = snapshot_live_attempt_authority(authority)
        confirmed = _require_sha256(confirmed_authority_sha256, "confirmed_authority_sha256")
        actual = live_attempt_authority_sha256(trusted)
        if confirmed != actual:
            raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "confirmed authority hash differs")
        if type(script) is not CpuFixedAttemptScriptV1:
            raise LiveAttemptError("UNTRUSTED_CPU_SCRIPT", "CPU script must use the closed enum")
        self._sink._reserve(trusted)
        started_ns = time.monotonic_ns()
        if started_ns >= trusted.deadline_monotonic_ns:
            handle = CpuFixedLiveAttemptHandleV1(
                authority=trusted,
                sink=self._sink,
                process=None,
                connection=None,
                started_ns=started_ns,
                cancel_grace_seconds=self._cancel_grace_seconds,
            )
            handle.cancel_and_join()
            return handle
        context = multiprocessing.get_context("fork")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_cpu_fixed_attempt_worker,
                args=(child_connection, script.value, actual, trusted.request_sha256),
                name="mobileworld-r24-fixed-live-attempt",
                daemon=False,
            ),
        )
        try:
            process.start()
            child_connection.close()
        except Exception as exc:
            parent_connection.close()
            child_connection.close()
            failed = CpuFixedLiveAttemptHandleV1(
                authority=trusted,
                sink=self._sink,
                process=None,
                connection=None,
                started_ns=started_ns,
                cancel_grace_seconds=self._cancel_grace_seconds,
            )
            failed._failed("CPU_WORKER_START_FAILED")
            raise LiveAttemptError(
                "CPU_WORKER_START_FAILED", "fixed worker failed to start"
            ) from exc
        handle = CpuFixedLiveAttemptHandleV1(
            authority=trusted,
            sink=self._sink,
            process=process,
            connection=parent_connection,
            started_ns=started_ns,
            cancel_grace_seconds=self._cancel_grace_seconds,
        )
        ready_deadline_ns = min(
            trusted.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        remaining_ns = max(0, ready_deadline_ns - time.monotonic_ns())
        ready = handle._receive(remaining_ns / 1_000_000_000)
        if ready != ("READY", actual):
            handle._failed("CPU_WORKER_READY_FAILED")
            raise LiveAttemptError("CPU_WORKER_READY_FAILED", "fixed worker did not become ready")
        return handle


def _production_openai_attempt_worker(
    connection: Connection,
    factory: ProductionPostPreflightFactoryV1,
    case_lease: CaseExecutionLeaseV1,
    request_bytes: bytes,
    authority_sha256: str,
    role_value: str,
) -> None:
    """Own the secret, SDK client, and one provider call inside the child."""

    secret_lease = None
    client = None
    http_client = None
    try:
        connection.send(("READY", authority_sha256))
        if connection.recv() != ("DISPATCH", authority_sha256):
            return
        secret_lease, api_key = factory._acquire_openai_secret_for_child_process(case_lease)
        role = OpenAIRoleV1(role_value)
        stage = factory.openai_stage(role)
        request_kwargs = json.loads(request_bytes)
        if type(request_kwargs) is not dict:
            raise TypeError("provider request root differs")

        from openai import DefaultHttpxClient, OpenAI, Timeout
        from openai.types.responses.response_usage import InputTokensDetails, ResponseUsage

        from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
            _project_openai_response,
        )

        timeout_seconds = stage.timeout_ms / 1_000
        timeout = Timeout(timeout_seconds)
        http_client = DefaultHttpxClient(timeout=timeout, trust_env=False)
        client = OpenAI(
            api_key=api_key,
            base_url=stage.endpoint.removesuffix("/responses"),
            timeout=timeout,
            max_retries=stage.sdk_max_retries,
            http_client=http_client,
        )
        # Linearize the potentially billable provider attempt immediately
        # before entering the SDK.  Secret/config/request/client failures that
        # happen above this point are exact zero-dispatch failures.
        connection.send(("DISPATCHED", authority_sha256))
        raw = client.responses.create(**request_kwargs, timeout=timeout_seconds)
        envelope = _project_openai_response(raw, requested_model=stage.model)
        usage = getattr(raw, "usage", None)
        if type(usage) is not ResponseUsage or type(usage.input_tokens_details) is not (
            InputTokensDetails
        ):
            raise TypeError("provider response omitted exact cached-token usage")
        cached_input_tokens = usage.input_tokens_details.cached_tokens
        if (
            type(cached_input_tokens) is not int
            or cached_input_tokens < 0
            or cached_input_tokens > usage.input_tokens
        ):
            raise ValueError("provider cached-token usage differs")
        connection.send(("COMPLETED", authority_sha256, envelope, cached_input_tokens))
    except BaseException:
        try:
            connection.send(("FAILED", authority_sha256, "PROVIDER_CHILD_FAILED"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        elif http_client is not None:
            try:
                http_client.close()
            except Exception:
                pass
        if secret_lease is not None:
            secret_lease.close()
        connection.close()


def live_attempt_cost_usd_micros(
    pricing: LiveAttemptPricingV1,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> int:
    trusted = snapshot_live_attempt_pricing(pricing)
    for value, label in (
        (input_tokens, "input_tokens"),
        (cached_input_tokens, "cached_input_tokens"),
        (output_tokens, "output_tokens"),
    ):
        _require_int(value, label, 0, 100_000_000)
    if not 0 <= cached_input_tokens <= input_tokens:
        raise LiveAttemptError("INVALID_TOKEN_CENSUS", "cached input exceeds total input")
    numerator = (
        (input_tokens - cached_input_tokens) * trusted.input_usd_micros_per_million_tokens
        + cached_input_tokens * trusted.cached_input_usd_micros_per_million_tokens
        + output_tokens * trusted.output_usd_micros_per_million_tokens
    )
    return (numerator + 999_999) // 1_000_000


def live_attempt_worst_case_cost_usd_micros(
    pricing: LiveAttemptPricingV1,
    *,
    request_byte_count: int,
    max_output_tokens: int,
) -> int:
    """Return a conservative pre-dispatch reservation for one request.

    OpenAI tokenization operates on the UTF-8 byte stream, so the canonical
    request byte count is an upper bound on input-token count.  Treat every
    input byte as uncached (or at the higher cached rate, if configured) and
    reserve the full authorized output-token ceiling.  This is deliberately a
    cost admission bound, not a prediction of provider usage.
    """

    trusted = snapshot_live_attempt_pricing(pricing)
    _require_int(
        request_byte_count,
        "request_byte_count",
        2,
        _MAX_PROVIDER_REQUEST_BYTES,
    )
    _require_int(max_output_tokens, "max_output_tokens", 1, 1_000_000)
    input_rate = max(
        trusted.input_usd_micros_per_million_tokens,
        trusted.cached_input_usd_micros_per_million_tokens,
    )
    numerator = (
        request_byte_count * input_rate
        + max_output_tokens * trusted.output_usd_micros_per_million_tokens
    )
    return (numerator + 999_999) // 1_000_000


class ProductionOpenAIAttemptCallV1:
    """Exact callable recognized by the seam's cancel-and-join fence."""

    def __init__(
        self,
        *,
        authority: LiveAttemptAuthorityV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        pricing: LiveAttemptPricingV1,
        process: BaseProcess,
        connection: Connection,
        started_ns: int,
        cancel_grace_seconds: float,
        execution_kind: LiveAttemptExecutionKindV1,
    ) -> None:
        self._authority = snapshot_live_attempt_authority(authority)
        self._authority_sha256 = live_attempt_authority_sha256(self._authority)
        self._sink = sink
        self._pricing = snapshot_live_attempt_pricing(pricing)
        self._process = process
        self._connection: Connection | None = connection
        self._started_ns = started_ns
        self._cancel_grace_seconds = cancel_grace_seconds
        if type(execution_kind) is not LiveAttemptExecutionKindV1:
            raise LiveAttemptError("UNTRUSTED_EXECUTION_KIND", "execution kind differs")
        self._execution_kind = execution_kind
        self._dispatch_count = 0
        self._dispatch_command_sent = False
        self._execute_started = False
        self._cancel_requested = threading.Event()
        self._terminal_receipt: LiveAttemptReceiptV1 | None = None
        self._result: object | None = None
        self._state_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._finalize_lock = threading.Lock()

    @property
    def authority_sha256(self) -> str:
        return self._authority_sha256

    @property
    def dispatch_count(self) -> int:
        with self._state_lock:
            return self._dispatch_count

    @property
    def terminal_receipt(self) -> LiveAttemptReceiptV1 | None:
        with self._state_lock:
            value = self._terminal_receipt
        return None if value is None else snapshot_live_attempt_receipt(value)

    @property
    def terminal_receipt_sha256(self) -> str | None:
        receipt = self.terminal_receipt
        return None if receipt is None else live_attempt_receipt_sha256(receipt)

    @property
    def receipt_root_sha256(self) -> str:
        return self._sink.receipt_root_sha256

    def _receive(self, timeout_seconds: float) -> tuple[object, ...] | None:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return None
            try:
                if not connection.poll(max(0.0, timeout_seconds)):
                    return None
                value = connection.recv()
            except (EOFError, OSError):
                return None
        return value if type(value) is tuple else ("INVALID",)

    def _send(self, value: tuple[str, str]) -> bool:
        with self._io_lock:
            connection = self._connection
            if connection is None:
                return False
            try:
                connection.send(value)
            except (BrokenPipeError, EOFError, OSError):
                return False
        return True

    def _drain(self) -> tuple[tuple[object, ...], ...]:
        messages: list[tuple[object, ...]] = []
        while True:
            message = self._receive(0.0)
            if message is None:
                return tuple(messages)
            messages.append(message)

    def _observe_dispatch(self, messages: tuple[tuple[object, ...], ...]) -> None:
        if any(message == ("DISPATCHED", self._authority_sha256) for message in messages):
            with self._state_lock:
                self._dispatch_count = 1

    def _stop_worker(self, *, cooperative: bool) -> _StopResult:
        process = self._process
        if not process.is_alive():
            process.join(0)
            return _StopResult(LiveAttemptTerminationV1.COOPERATIVE, True, process.exitcode)
        if cooperative:
            process.join(self._cancel_grace_seconds)
            if not process.is_alive():
                return _StopResult(
                    LiveAttemptTerminationV1.COOPERATIVE,
                    True,
                    process.exitcode,
                )
        process.terminate()
        process.join(self._cancel_grace_seconds)
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.TERM, True, process.exitcode)
        process.kill()
        process.join(max(5.0, self._cancel_grace_seconds))
        if not process.is_alive():
            return _StopResult(LiveAttemptTerminationV1.KILL, True, process.exitcode)
        return _StopResult(LiveAttemptTerminationV1.UNCONFIRMED, False, None)

    def _make_receipt(
        self,
        *,
        status: LiveAttemptStatusV1,
        dispatch_count: int,
        stop: _StopResult,
        cost_status: LiveAttemptCostStatusV1,
        cost_usd_micros: int | None,
        cancellation_requested: bool,
        response_envelope_sha256: str | None = None,
        input_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        output_tokens: int | None = None,
        total_tokens: int | None = None,
        late_output_detected: bool = False,
        failure_code: str | None = None,
    ) -> LiveAttemptReceiptV1:
        authority = self._authority
        return LiveAttemptReceiptV1(
            attempt_id=authority.attempt_id,
            role=authority.role,
            authority_sha256=self._authority_sha256,
            manifest_sha256=authority.manifest_sha256,
            preflight_sha256=authority.preflight_sha256,
            case_execution_lease_sha256=authority.case_execution_lease_sha256,
            stage_sha256=authority.stage_sha256,
            case_id=authority.case_id,
            logical_call_id=authority.logical_call_id,
            actor_request_sha256=authority.actor_request_sha256,
            request_sha256=authority.request_sha256,
            transport_binding_sha256=authority.transport_binding_sha256,
            pricing_binding_sha256=authority.pricing_binding_sha256,
            execution_kind=self._execution_kind,
            status=status,
            dispatch_count=dispatch_count,
            response_envelope_sha256=response_envelope_sha256,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_status=cost_status,
            cost_usd_micros=cost_usd_micros,
            cancellation_requested=cancellation_requested,
            termination=stop.termination,
            worker_pid=self._process.pid,
            worker_exit_code=stop.worker_exit_code,
            worker_reaped=stop.worker_reaped,
            late_output_detected=late_output_detected,
            duration_ns=min(_MAX_DURATION_NS, max(0, time.monotonic_ns() - self._started_ns)),
            failure_code=failure_code,
        )

    def _publish(self, receipt: LiveAttemptReceiptV1) -> LiveAttemptReceiptV1:
        trusted = snapshot_live_attempt_receipt(receipt)
        self._sink._commit(trusted)
        with self._state_lock:
            self._terminal_receipt = trusted
            connection = self._connection
            self._connection = None
        if connection is not None:
            connection.close()
        return snapshot_live_attempt_receipt(trusted)

    def _failed(self, code: str) -> LiveAttemptReceiptV1:
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            stop = self._stop_worker(cooperative=False)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            return self._publish(
                self._make_receipt(
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=dispatch_count,
                    stop=stop,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if dispatch_count == 0
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=0 if dispatch_count == 0 else None,
                    cancellation_requested=False,
                    late_output_detected=any(
                        message and message[0] == "COMPLETED" for message in messages
                    ),
                    failure_code=code,
                )
            )

    def cancel_and_join(self) -> LiveAttemptReceiptV1:
        """TERM, then KILL if needed, and publish only after waitpid observation."""

        self._cancel_requested.set()
        with self._finalize_lock:
            terminal = self.terminal_receipt
            if terminal is not None:
                return terminal
            with self._state_lock:
                command_sent = self._dispatch_command_sent
            self._send(("CANCEL", self._authority_sha256))
            stop = self._stop_worker(cooperative=not command_sent)
            messages = self._drain()
            self._observe_dispatch(messages)
            dispatch_count = self.dispatch_count
            late_output = any(message and message[0] == "COMPLETED" for message in messages)
            if not stop.worker_reaped:
                return self._publish(
                    self._make_receipt(
                        status=LiveAttemptStatusV1.TERMINATION_UNCONFIRMED,
                        dispatch_count=dispatch_count,
                        stop=stop,
                        cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                        cost_usd_micros=None,
                        cancellation_requested=True,
                        late_output_detected=late_output,
                        failure_code="TERMINATION_UNCONFIRMED",
                    )
                )
            pre_dispatch = dispatch_count == 0
            return self._publish(
                self._make_receipt(
                    status=(
                        LiveAttemptStatusV1.CANCELLED_PRE_DISPATCH
                        if pre_dispatch
                        else LiveAttemptStatusV1.CANCELLED_POST_DISPATCH
                    ),
                    dispatch_count=dispatch_count,
                    stop=stop,
                    cost_status=(
                        LiveAttemptCostStatusV1.EXACT
                        if pre_dispatch
                        else LiveAttemptCostStatusV1.UNKNOWN
                    ),
                    cost_usd_micros=0 if pre_dispatch else None,
                    cancellation_requested=True,
                    late_output_detected=late_output,
                )
            )

    def __call__(self) -> object:
        with self._state_lock:
            if self._execute_started:
                raise LiveAttemptError("DUPLICATE_EXECUTION", "attempt was called twice")
            self._execute_started = True
            cancelled = self._cancel_requested.is_set()
            if not cancelled:
                self._dispatch_command_sent = True
        if cancelled or time.monotonic_ns() >= self._authority.deadline_monotonic_ns:
            self.cancel_and_join()
            raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt was cancelled")
        if not self._send(("DISPATCH", self._authority_sha256)):
            self._failed("PROVIDER_CHILD_DISPATCH_FAILED")
            raise LiveAttemptError("PROVIDER_CHILD_DISPATCH_FAILED", "child dispatch failed")

        while True:
            if self._cancel_requested.is_set():
                self.cancel_and_join()
                raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt was cancelled")
            remaining_ns = self._authority.deadline_monotonic_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                self.cancel_and_join()
                raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt deadline elapsed")
            message = self._receive(min(0.01, remaining_ns / 1_000_000_000))
            if message is None:
                if not self._process.is_alive():
                    self._failed("PROVIDER_CHILD_EXITED_WITHOUT_OUTPUT")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_EXITED_WITHOUT_OUTPUT", "child exited without output"
                    )
                continue
            if message == ("DISPATCHED", self._authority_sha256):
                self._observe_dispatch((message,))
                continue
            if (
                len(message) == 3
                and message[0] == "FAILED"
                and message[1] == self._authority_sha256
                and message[2] == "PROVIDER_CHILD_FAILED"
            ):
                self._failed("PROVIDER_CHILD_FAILED")
                raise LiveAttemptError("PROVIDER_CHILD_FAILED", "provider child failed")
            if (
                len(message) == 4
                and message[0] == "COMPLETED"
                and message[1] == self._authority_sha256
            ):
                self._observe_dispatch((("DISPATCHED", self._authority_sha256),))
                from mobile_world.runtime.sentinel.r2_2.gpt56_policy import (
                    ResponsesEnvelopeV1,
                    _detach_envelope,
                )

                try:
                    raw_envelope = message[2]
                    cached_input_tokens = message[3]
                    if type(raw_envelope) is not ResponsesEnvelopeV1:
                        raise TypeError("provider child envelope type differs")
                    if type(cached_input_tokens) is not int or cached_input_tokens < 0:
                        raise TypeError("provider child cached-token usage differs")
                    envelope = _detach_envelope(raw_envelope)
                except Exception as exc:
                    self._failed("PROVIDER_CHILD_RESPONSE_INVALID")
                    raise LiveAttemptError(
                        "PROVIDER_CHILD_RESPONSE_INVALID", "child response differs"
                    ) from exc
                with self._finalize_lock:
                    terminal = self.terminal_receipt
                    if terminal is not None:
                        raise LiveAttemptError("LIVE_ATTEMPT_CANCELLED", "attempt is terminal")
                    self._process.join(self._cancel_grace_seconds)
                    if self._process.is_alive():
                        stop = self._stop_worker(cooperative=False)
                        self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                failure_code="PROVIDER_CHILD_DID_NOT_EXIT",
                            )
                        )
                        raise LiveAttemptError(
                            "PROVIDER_CHILD_DID_NOT_EXIT", "child remained alive"
                        )
                    stop = _StopResult(
                        LiveAttemptTerminationV1.NONE,
                        True,
                        self._process.exitcode,
                    )
                    if (
                        envelope.input_tokens is None
                        or envelope.output_tokens is None
                        or envelope.total_tokens is None
                    ):
                        self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.UNKNOWN,
                                cost_usd_micros=None,
                                cancellation_requested=False,
                                failure_code="PROVIDER_USAGE_MISSING",
                            )
                        )
                        raise LiveAttemptError(
                            "PROVIDER_USAGE_MISSING", "provider omitted usage accounting"
                        )
                    cost = live_attempt_cost_usd_micros(
                        self._pricing,
                        input_tokens=envelope.input_tokens,
                        cached_input_tokens=cached_input_tokens,
                        output_tokens=envelope.output_tokens,
                    )
                    if (
                        envelope.output_tokens > self._authority.max_output_tokens
                        or cost > self._authority.max_cost_usd_micros
                        or self._process.exitcode != 0
                    ):
                        self._publish(
                            self._make_receipt(
                                status=LiveAttemptStatusV1.FAILED,
                                dispatch_count=1,
                                stop=stop,
                                cost_status=LiveAttemptCostStatusV1.EXACT,
                                cost_usd_micros=cost,
                                cancellation_requested=False,
                                response_envelope_sha256=envelope.sha256,
                                input_tokens=envelope.input_tokens,
                                cached_input_tokens=cached_input_tokens,
                                output_tokens=envelope.output_tokens,
                                total_tokens=envelope.total_tokens,
                                failure_code="PROVIDER_RESULT_EXCEEDS_AUTHORITY",
                            )
                        )
                        raise LiveAttemptError(
                            "PROVIDER_RESULT_EXCEEDS_AUTHORITY", "provider result exceeds authority"
                        )
                    self._result = envelope
                    self._publish(
                        self._make_receipt(
                            status=LiveAttemptStatusV1.COMPLETED,
                            dispatch_count=1,
                            stop=stop,
                            cost_status=LiveAttemptCostStatusV1.EXACT,
                            cost_usd_micros=cost,
                            cancellation_requested=False,
                            response_envelope_sha256=envelope.sha256,
                            input_tokens=envelope.input_tokens,
                            cached_input_tokens=cached_input_tokens,
                            output_tokens=envelope.output_tokens,
                            total_tokens=envelope.total_tokens,
                        )
                    )
                    return _detach_envelope(envelope)
            self._failed("PROVIDER_CHILD_PROTOCOL_VIOLATION")
            raise LiveAttemptError("PROVIDER_CHILD_PROTOCOL_VIOLATION", "child protocol differs")


class ProductionOpenAIAttemptRunnerV1:
    """Role-bound live runner for exact RUBRIC or HISTORY_POLICY attempts."""

    def __init__(
        self,
        *,
        factory: ProductionPostPreflightFactoryV1,
        role: LiveAttemptRoleV1,
        sink: MemoryLiveAttemptReceiptSinkV1,
        pricing: LiveAttemptPricingV1,
        confirmed_pricing_sha256: str,
        startup_timeout_ms: int = 5_000,
        cancel_grace_ms: int = 1_000,
    ) -> None:
        if type(factory) is not ProductionPostPreflightFactoryV1:
            raise LiveAttemptError("UNTRUSTED_PRODUCTION_FACTORY", "factory type differs")
        if (
            role not in {LiveAttemptRoleV1.RUBRIC, LiveAttemptRoleV1.HISTORY_POLICY}
            or type(role) is not LiveAttemptRoleV1
        ):
            raise LiveAttemptError("UNSUPPORTED_LIVE_ROLE", "live attempt role differs")
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "sink type differs")
        if type(pricing) is not LiveAttemptPricingV1:
            raise LiveAttemptError("UNTRUSTED_PRICING", "pricing type differs")
        pricing_sha256 = live_attempt_pricing_sha256(pricing)
        if _require_sha256(confirmed_pricing_sha256, "confirmed_pricing_sha256") != pricing_sha256:
            raise LiveAttemptError("PRICING_HASH_DRIFT", "confirmed pricing hash differs")
        if factory.pricing_binding_sha256 != pricing_sha256:
            raise LiveAttemptError(
                "PRICING_AUTHORITY_MISMATCH",
                "pricing differs from the post-preflight factory",
            )
        stage_role = (
            OpenAIRoleV1.RUBRIC if role is LiveAttemptRoleV1.RUBRIC else OpenAIRoleV1.HISTORY_POLICY
        )
        stage = factory.openai_stage(stage_role)
        if stage.model != pricing.model:
            raise LiveAttemptError("PRICING_MODEL_DRIFT", "pricing model differs from stage")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 30_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 30_000)
        if os.name != "posix":
            raise LiveAttemptError(
                "PRODUCTION_PROCESS_CONTROL_UNAVAILABLE", "POSIX process control required"
            )
        self._factory = factory
        self._role = role
        self._stage = stage
        self._sink = sink
        self._pricing = snapshot_live_attempt_pricing(pricing)
        self._pricing_sha256 = pricing_sha256
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000

    @property
    def factory_binding_sha256(self) -> str:
        return _require_sha256(self._factory.factory_binding_sha256, "factory binding")

    @property
    def pricing_binding_sha256(self) -> str:
        return self._pricing_sha256

    @property
    def manifest_sha256(self) -> str:
        return _require_sha256(self._factory.manifest_sha256, "manifest binding")

    @property
    def preflight_report_sha256(self) -> str:
        return _require_sha256(self._factory.preflight_report_sha256, "preflight binding")

    @property
    def role(self) -> LiveAttemptRoleV1:
        return self._role

    @property
    def openai_stage(self) -> OpenAIResponsesStageV1:
        return OpenAIResponsesStageV1(
            role=self._stage.role,
            model=self._stage.model,
            endpoint=self._stage.endpoint,
            transport_kind=self._stage.transport_kind,
            transport_authority=self._stage.transport_authority,
            openai_sdk_version=self._stage.openai_sdk_version,
            sdk_max_retries=self._stage.sdk_max_retries,
            external_network_on_call=self._stage.external_network_on_call,
            model_on_call=self._stage.model_on_call,
            max_output_tokens=self._stage.max_output_tokens,
            timeout_ms=self._stage.timeout_ms,
            max_attempts=self._stage.max_attempts,
            store=self._stage.store,
        )

    @property
    def openai_stage_sha256(self) -> str:
        return _require_sha256(
            self._factory.openai_stage_sha256(self._stage.role), "OpenAI stage binding"
        )

    def attest_case_execution_lease(self, case_lease: CaseExecutionLeaseV1) -> CaseExecutionLeaseV1:
        return self._factory.validate_case_execution_lease(case_lease)

    def begin(
        self,
        *,
        case_lease: CaseExecutionLeaseV1,
        attempt_id: str,
        logical_call_id: str,
        request: CanonicalHistoryPolicyRequestV1,
        transport_binding_sha256: str,
        deadline_monotonic_ns: int,
        max_cost_usd_micros: int,
    ) -> ProductionOpenAIAttemptCallV1:
        lease = self._factory.validate_case_execution_lease(case_lease)
        if lease.execution_scope is not CaseExecutionScopeV1.OWNER_AUTHORIZED_LIVE:
            raise LiveAttemptError("LIVE_SCOPE_REQUIRED", "case lease is not live")
        trusted_request = snapshot_canonical_history_policy_request(request)
        authority = LiveAttemptAuthorityV1(
            attempt_id=attempt_id,
            role=self._role,
            manifest_sha256=lease.manifest_sha256,
            preflight_sha256=lease.preflight_report_sha256,
            case_execution_lease_sha256=case_execution_lease_sha256(lease),
            stage_sha256=self.openai_stage_sha256,
            case_id=lease.case_id,
            logical_call_id=logical_call_id,
            actor_request_sha256=lease.request_sha256,
            request_sha256=trusted_request.request_sha256,
            transport_binding_sha256=_require_sha256(
                transport_binding_sha256, "transport_binding_sha256"
            ),
            pricing_binding_sha256=lease.pricing_binding_sha256,
            deadline_monotonic_ns=deadline_monotonic_ns,
            max_cost_usd_micros=max_cost_usd_micros,
            max_output_tokens=self._stage.max_output_tokens,
        )
        started_ns = time.monotonic_ns()
        if started_ns >= authority.deadline_monotonic_ns:
            raise LiveAttemptError("ATTEMPT_DEADLINE_ELAPSED", "attempt deadline elapsed")
        reserved_cost = live_attempt_worst_case_cost_usd_micros(
            self._pricing,
            request_byte_count=trusted_request.byte_count,
            max_output_tokens=authority.max_output_tokens,
        )
        self._sink._reserve(authority)
        if reserved_cost > authority.max_cost_usd_micros:
            self._sink._commit(
                LiveAttemptReceiptV1(
                    attempt_id=authority.attempt_id,
                    role=authority.role,
                    authority_sha256=live_attempt_authority_sha256(authority),
                    manifest_sha256=authority.manifest_sha256,
                    preflight_sha256=authority.preflight_sha256,
                    case_execution_lease_sha256=authority.case_execution_lease_sha256,
                    stage_sha256=authority.stage_sha256,
                    case_id=authority.case_id,
                    logical_call_id=authority.logical_call_id,
                    actor_request_sha256=authority.actor_request_sha256,
                    request_sha256=authority.request_sha256,
                    transport_binding_sha256=authority.transport_binding_sha256,
                    pricing_binding_sha256=authority.pricing_binding_sha256,
                    execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=0,
                    response_envelope_sha256=None,
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_status=LiveAttemptCostStatusV1.EXACT,
                    cost_usd_micros=0,
                    cancellation_requested=False,
                    termination=LiveAttemptTerminationV1.NONE,
                    worker_pid=None,
                    worker_exit_code=None,
                    worker_reaped=False,
                    late_output_detected=False,
                    duration_ns=min(
                        _MAX_DURATION_NS,
                        max(0, time.monotonic_ns() - started_ns),
                    ),
                    failure_code="ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY",
                )
            )
            raise LiveAttemptError(
                "ATTEMPT_COST_RESERVATION_EXCEEDS_AUTHORITY",
                "worst-case request cost exceeds attempt authority",
            )
        # Production attempts may be prepared from the Sentinel worker thread.
        # A clean spawned interpreter avoids inheriting unrelated thread locks,
        # clients, loggers, or allocator state as POSIX ``fork`` would.
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_production_openai_attempt_worker,
                args=(
                    child_connection,
                    self._factory,
                    lease,
                    trusted_request.canonical_bytes,
                    live_attempt_authority_sha256(authority),
                    self._role.value,
                ),
                name="mobileworld-r24-openai-attempt",
                daemon=False,
            ),
        )
        try:
            process.start()
            child_connection.close()
        except Exception as exc:
            parent_connection.close()
            child_connection.close()
            self._sink._commit(
                LiveAttemptReceiptV1(
                    attempt_id=authority.attempt_id,
                    role=authority.role,
                    authority_sha256=live_attempt_authority_sha256(authority),
                    manifest_sha256=authority.manifest_sha256,
                    preflight_sha256=authority.preflight_sha256,
                    case_execution_lease_sha256=authority.case_execution_lease_sha256,
                    stage_sha256=authority.stage_sha256,
                    case_id=authority.case_id,
                    logical_call_id=authority.logical_call_id,
                    actor_request_sha256=authority.actor_request_sha256,
                    request_sha256=authority.request_sha256,
                    transport_binding_sha256=authority.transport_binding_sha256,
                    pricing_binding_sha256=authority.pricing_binding_sha256,
                    execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
                    status=LiveAttemptStatusV1.FAILED,
                    dispatch_count=0,
                    response_envelope_sha256=None,
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_status=LiveAttemptCostStatusV1.EXACT,
                    cost_usd_micros=0,
                    cancellation_requested=False,
                    termination=LiveAttemptTerminationV1.COOPERATIVE,
                    worker_pid=None,
                    worker_exit_code=0,
                    worker_reaped=True,
                    late_output_detected=False,
                    duration_ns=min(
                        _MAX_DURATION_NS,
                        max(0, time.monotonic_ns() - started_ns),
                    ),
                    failure_code="PROVIDER_CHILD_START_FAILED",
                )
            )
            raise LiveAttemptError(
                "PROVIDER_CHILD_START_FAILED", "provider child failed to start"
            ) from exc
        call = ProductionOpenAIAttemptCallV1(
            authority=authority,
            sink=self._sink,
            pricing=self._pricing,
            process=process,
            connection=parent_connection,
            started_ns=started_ns,
            cancel_grace_seconds=self._cancel_grace_seconds,
            execution_kind=LiveAttemptExecutionKindV1.OPENAI_RESPONSES_CHILD_PROCESS,
        )
        ready_deadline_ns = min(
            authority.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        ready = call._receive(max(0, ready_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if ready != ("READY", live_attempt_authority_sha256(authority)):
            call._failed("PROVIDER_CHILD_READY_FAILED")
            raise LiveAttemptError("PROVIDER_CHILD_READY_FAILED", "provider child is not ready")
        return call


# Backward-compatible semantic names for the only currently seam-wired role.
# They are aliases, not a second implementation or activation gate.
ProductionHistoryPolicyAttemptCallV1 = ProductionOpenAIAttemptCallV1
ProductionHistoryPolicyAttemptRunnerV1 = ProductionOpenAIAttemptRunnerV1


class CpuFixedCancellableAttemptRunnerV1:
    """CPU-only constructor for exercising the exact seam cancellation call."""

    def __init__(
        self,
        *,
        sink: MemoryLiveAttemptReceiptSinkV1,
        startup_timeout_ms: int = 1_000,
        cancel_grace_ms: int = 100,
    ) -> None:
        if type(sink) is not MemoryLiveAttemptReceiptSinkV1:
            raise LiveAttemptError("UNTRUSTED_SINK", "CPU runner requires the exact sink")
        _require_int(startup_timeout_ms, "startup_timeout_ms", 1, 10_000)
        _require_int(cancel_grace_ms, "cancel_grace_ms", 1, 10_000)
        self._sink = sink
        self._startup_timeout_ns = startup_timeout_ms * 1_000_000
        self._cancel_grace_seconds = cancel_grace_ms / 1_000

    def begin(
        self,
        authority: LiveAttemptAuthorityV1,
        *,
        confirmed_authority_sha256: str,
        script: CpuFixedAttemptScriptV1,
    ) -> ProductionOpenAIAttemptCallV1:
        trusted = snapshot_live_attempt_authority(authority)
        actual = live_attempt_authority_sha256(trusted)
        if _require_sha256(confirmed_authority_sha256, "confirmed_authority_sha256") != actual:
            raise LiveAttemptError("AUTHORITY_HASH_DRIFT", "confirmed authority hash differs")
        if (
            script
            not in {
                CpuFixedAttemptScriptV1.BLOCK_AFTER_DISPATCH,
                CpuFixedAttemptScriptV1.IGNORE_TERM_AFTER_DISPATCH,
            }
            or type(script) is not CpuFixedAttemptScriptV1
        ):
            raise LiveAttemptError(
                "UNTRUSTED_CPU_SCRIPT", "cancellable CPU call requires a blocking script"
            )
        self._sink._reserve(trusted)
        context = multiprocessing.get_context("fork")
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = cast(
            BaseProcess,
            context.Process(
                target=_cpu_fixed_attempt_worker,
                args=(child_connection, script.value, actual, trusted.request_sha256),
                name="mobileworld-r24-fixed-cancellable-attempt",
                daemon=False,
            ),
        )
        process.start()
        child_connection.close()
        pricing = LiveAttemptPricingV1(
            pricing_id="cpu-fixed-test-price",
            model="gpt-5.6-sol",
            input_usd_micros_per_million_tokens=0,
            cached_input_usd_micros_per_million_tokens=0,
            output_usd_micros_per_million_tokens=0,
            source_sha256=_canonical_sha256({"cpu_fixed": True}),
            effective_at_utc="2026-01-01T00:00:00Z",
        )
        call = ProductionOpenAIAttemptCallV1(
            authority=trusted,
            sink=self._sink,
            pricing=pricing,
            process=process,
            connection=parent_connection,
            started_ns=time.monotonic_ns(),
            cancel_grace_seconds=self._cancel_grace_seconds,
            execution_kind=LiveAttemptExecutionKindV1.CPU_FIXED_SUBPROCESS,
        )
        ready_deadline_ns = min(
            trusted.deadline_monotonic_ns,
            time.monotonic_ns() + self._startup_timeout_ns,
        )
        ready = call._receive(max(0, ready_deadline_ns - time.monotonic_ns()) / 1_000_000_000)
        if ready != ("READY", actual):
            call._failed("CPU_WORKER_READY_FAILED")
            raise LiveAttemptError("CPU_WORKER_READY_FAILED", "CPU worker did not become ready")
        return call


def production_live_attempt_runner_available_v1() -> bool:
    from mobile_world.runtime.sentinel.r2_4.production_preflight import (
        production_activation_available_v1,
    )

    available = production_activation_available_v1()
    if type(available) is not bool:
        raise LiveAttemptError("INVALID_ACTIVATION_STATE", "activation state is untrusted")
    return available


__all__ = [
    "CpuFixedAttemptScriptV1",
    "CpuFixedLiveAttemptHandleV1",
    "CpuFixedLiveAttemptRunnerV1",
    "CpuFixedCancellableAttemptRunnerV1",
    "CanonicalHistoryPolicyRequestV1",
    "CanonicalOpenAIRequestV1",
    "HISTORY_POLICY_REQUEST_SCHEMA_VERSION",
    "OPENAI_REQUEST_SCHEMA_VERSION",
    "LIVE_ATTEMPT_AUTHORITY_SCHEMA_VERSION",
    "LIVE_ATTEMPT_RECEIPT_ROOT_SCHEMA_VERSION",
    "LIVE_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "LIVE_ATTEMPT_PRICING_SCHEMA_VERSION",
    "LiveAttemptAuthorityV1",
    "LiveAttemptCostStatusV1",
    "LiveAttemptError",
    "LiveAttemptExecutionKindV1",
    "LiveAttemptPricingV1",
    "LiveAttemptReceiptV1",
    "LiveAttemptRoleV1",
    "LiveAttemptStatusV1",
    "LiveAttemptTerminationV1",
    "MemoryLiveAttemptReceiptSinkV1",
    "ProductionHistoryPolicyAttemptCallV1",
    "ProductionHistoryPolicyAttemptRunnerV1",
    "ProductionOpenAIAttemptCallV1",
    "ProductionOpenAIAttemptRunnerV1",
    "live_attempt_authority_projection",
    "live_attempt_authority_sha256",
    "live_attempt_cost_usd_micros",
    "live_attempt_worst_case_cost_usd_micros",
    "live_attempt_pricing_projection",
    "live_attempt_pricing_sha256",
    "live_attempt_receipt_projection",
    "live_attempt_receipt_root_sha256",
    "live_attempt_receipt_sha256",
    "production_live_attempt_runner_available_v1",
    "build_canonical_history_policy_request",
    "build_canonical_openai_request",
    "snapshot_canonical_openai_request",
    "snapshot_canonical_history_policy_request",
    "snapshot_live_attempt_authority",
    "snapshot_live_attempt_pricing",
    "snapshot_live_attempt_receipt",
]
