"""Independent execution-state result, receipt, and hash-only sidecar channel.

The channel composes after the history Sentinel result.  It never rewrites or
relabels the history receipt, so an Original fallback from history policy can
truthfully coexist with an applied deterministic execution-state prompt view.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import NoReturn, Protocol, cast, runtime_checkable

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
    canonical_sha256,
)
from mobile_world.runtime.sentinel.contracts import SentinelReceipt, SentinelResult
from mobile_world.runtime.sentinel.prompt_view import (
    ExecutionStateViewV1,
    PromptViewRenderResultV1,
    execution_state_view_sha256,
    prompt_view_render_result_sha256,
)
from mobile_world.runtime.sentinel.r2_4.contracts import (
    RuntimeVerticalSentinelResultV1,
    snapshot_vertical_sentinel_result,
)

EXECUTION_STATE_CHANNEL_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel.execution-state-channel-receipt/v1"
)
EXECUTION_STATE_COMPOSITE_RESULT_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel.execution-state-composite-result/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ADMISSION_PROBE = b"mobileworld-execution-state-receipt-admission-v1\n"
_SUCCESS_CHECKS = (
    "EXECUTION_STATE_HISTORY_RECEIPT_BOUND_WITHOUT_MUTATION",
    "EXECUTION_STATE_RAW_REQUEST_BOUND",
    "EXECUTION_STATE_HISTORY_FINAL_BOUND",
    "EXECUTION_STATE_VIEW_AND_RENDER_RESULT_BOUND",
    "EXECUTION_STATE_FINAL_SELECTION_VALIDATED",
    "EXECUTION_STATE_REQUEST_VIEWS_NOT_PERSISTED",
)
_FALLBACK_CHECKS = (
    "EXECUTION_STATE_HISTORY_RECEIPT_BOUND_WITHOUT_MUTATION",
    "EXECUTION_STATE_RAW_REQUEST_BOUND",
    "EXECUTION_STATE_HISTORY_FINAL_PRESERVED",
    "EXECUTION_STATE_REQUEST_VIEWS_NOT_PERSISTED",
)
_CANCELLED_CHECKS = (
    "EXECUTION_STATE_HISTORY_RECEIPT_BOUND_WITHOUT_MUTATION",
    "EXECUTION_STATE_RAW_REQUEST_BOUND",
    "EXECUTION_STATE_GLOBAL_KILL_SWITCH_FORCED_ORIGINAL",
    "EXECUTION_STATE_REQUEST_VIEWS_NOT_PERSISTED",
)


class ExecutionStateChannelError(ValueError):
    """Typed local contract failure; it grants no model/action authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.provider_invocation_allowed = False
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str) -> NoReturn:
    raise ExecutionStateChannelError(code, message)


def _require_sha256(value: object, name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail("INVALID_EXECUTION_STATE_SHA256", f"{name} must be lowercase SHA-256")
    return value


def _require_runtime_id(value: object, name: str) -> str:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        _fail("INVALID_EXECUTION_STATE_ID", f"{name} must be a bounded path-safe ID")
    return value


def _parse_canonical_bytes(value: bytes, name: str) -> JsonValue:
    if type(value) is not bytes:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", f"{name} must be canonical bytes")
    try:
        decoded = cast(JsonValue, json.loads(value))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ExecutionStateChannelError("NON_CANONICAL_JSON", f"{name} is malformed") from exc
    if canonical_json_bytes(decoded) != value:
        _fail("NON_CANONICAL_JSON", f"{name} is not canonical JSON")
    return decoded


class ExecutionStateChannelStatusV1(StrEnum):
    APPLIED = "APPLIED"
    SHADOW = "SHADOW"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"
    CANCELLED = "CANCELLED"


class ExecutionStateFallbackReasonV1(StrEnum):
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
    STATE_BUILD_FAILED = "STATE_BUILD_FAILED"
    HISTORY_BIND_FAILED = "HISTORY_BIND_FAILED"
    RENDER_FAILED = "RENDER_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    GLOBAL_KILL_SWITCH_CHANGED = "GLOBAL_KILL_SWITCH_CHANGED"


@dataclass(frozen=True, slots=True)
class ExecutionStateChannelReceiptV1:
    """Hash-only receipt for the execution-state channel alone."""

    logical_call_id: str
    status: ExecutionStateChannelStatusV1
    history_receipt_sha256: str
    raw_request_sha256: str
    history_final_request_sha256: str
    candidate_request_sha256: str
    final_request_sha256: str
    execution_state_view_sha256: str | None
    prompt_view_render_result_sha256: str | None
    adapter_declaration_sha256: str | None
    exact_diff_sha256: str | None
    would_augment: bool
    augmentation_applied: bool
    fallback_reason: ExecutionStateFallbackReasonV1 | None
    validation_checks: tuple[str, ...]
    history_policy_admission_claimed: bool = False
    action_recommendation_present: bool = False
    request_views_persisted: bool = False
    exact_diff_preimages_persisted: bool = False
    schema_version: str = EXECUTION_STATE_CHANNEL_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not str or self.schema_version != (
            EXECUTION_STATE_CHANNEL_RECEIPT_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_EXECUTION_STATE_SCHEMA", "unknown channel receipt schema")
        _require_runtime_id(self.logical_call_id, "logical_call_id")
        for value, name in (
            (self.history_receipt_sha256, "history_receipt_sha256"),
            (self.raw_request_sha256, "raw_request_sha256"),
            (self.history_final_request_sha256, "history_final_request_sha256"),
            (self.candidate_request_sha256, "candidate_request_sha256"),
            (self.final_request_sha256, "final_request_sha256"),
        ):
            _require_sha256(value, name)
        for optional_value, name in (
            (self.execution_state_view_sha256, "execution_state_view_sha256"),
            (self.prompt_view_render_result_sha256, "prompt_view_render_result_sha256"),
            (self.adapter_declaration_sha256, "adapter_declaration_sha256"),
            (self.exact_diff_sha256, "exact_diff_sha256"),
        ):
            if optional_value is not None:
                _require_sha256(optional_value, name)
        if type(self.status) is not ExecutionStateChannelStatusV1:
            _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "status must use the exact enum")
        if (
            type(cast(object, self.would_augment)) is not bool
            or type(cast(object, self.augmentation_applied)) is not bool
        ):
            _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "selection flags must be exact bools")
        if self.fallback_reason is not None and (
            type(self.fallback_reason) is not ExecutionStateFallbackReasonV1
        ):
            _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "fallback reason must use the exact enum")
        if self.status in (
            ExecutionStateChannelStatusV1.APPLIED,
            ExecutionStateChannelStatusV1.SHADOW,
        ):
            if None in (
                self.execution_state_view_sha256,
                self.prompt_view_render_result_sha256,
                self.adapter_declaration_sha256,
                self.exact_diff_sha256,
            ):
                _fail("EXECUTION_STATE_PROOF_MISSING", "successful channel lacks render proofs")
            if not self.would_augment or self.fallback_reason is not None:
                _fail("EXECUTION_STATE_SELECTION_INVALID", "successful channel flags conflict")
            if self.validation_checks != _SUCCESS_CHECKS:
                _fail("EXECUTION_STATE_CHECK_CENSUS_MISMATCH", "success checks differ")
            expected_applied = self.status is ExecutionStateChannelStatusV1.APPLIED
            if self.augmentation_applied is not expected_applied:
                _fail("EXECUTION_STATE_SELECTION_INVALID", "status and application differ")
            expected_final = (
                self.candidate_request_sha256
                if expected_applied
                else self.history_final_request_sha256
            )
            if self.final_request_sha256 != expected_final:
                _fail("EXECUTION_STATE_SELECTION_INVALID", "final request selection differs")
        elif self.status in (
            ExecutionStateChannelStatusV1.FAILED,
            ExecutionStateChannelStatusV1.UNAVAILABLE,
        ):
            if self.would_augment or self.augmentation_applied:
                _fail("EXECUTION_STATE_SELECTION_INVALID", "fallback cannot claim augmentation")
            if self.fallback_reason is None:
                _fail("EXECUTION_STATE_FALLBACK_REASON_MISSING", "fallback reason is required")
            if self.candidate_request_sha256 != self.history_final_request_sha256 or (
                self.final_request_sha256 != self.history_final_request_sha256
            ):
                _fail("EXECUTION_STATE_FALLBACK_CHANGED_REQUEST", "fallback must preserve history")
            if any(
                value is not None
                for value in (
                    self.prompt_view_render_result_sha256,
                    self.adapter_declaration_sha256,
                    self.exact_diff_sha256,
                )
            ):
                _fail("EXECUTION_STATE_FALLBACK_PROOF_INVALID", "fallback cannot bind a render")
            if self.validation_checks != _FALLBACK_CHECKS:
                _fail("EXECUTION_STATE_CHECK_CENSUS_MISMATCH", "fallback checks differ")
        elif self.status is ExecutionStateChannelStatusV1.CANCELLED:
            if self.would_augment or self.augmentation_applied:
                _fail("EXECUTION_STATE_SELECTION_INVALID", "cancelled channel cannot augment")
            if self.fallback_reason is not (
                ExecutionStateFallbackReasonV1.GLOBAL_KILL_SWITCH_CHANGED
            ):
                _fail("EXECUTION_STATE_FALLBACK_REASON_MISSING", "kill cancellation is required")
            if self.candidate_request_sha256 != self.raw_request_sha256 or (
                self.final_request_sha256 != self.raw_request_sha256
            ):
                _fail("EXECUTION_STATE_KILL_SWITCH_CHANGED_REQUEST", "kill must select Original")
            if any(
                value is not None
                for value in (
                    self.prompt_view_render_result_sha256,
                    self.adapter_declaration_sha256,
                    self.exact_diff_sha256,
                )
            ):
                _fail("EXECUTION_STATE_FALLBACK_PROOF_INVALID", "kill cannot bind a render")
            if self.validation_checks != _CANCELLED_CHECKS:
                _fail("EXECUTION_STATE_CHECK_CENSUS_MISMATCH", "kill checks differ")
        else:  # pragma: no cover - exact enum census above is exhaustive.
            _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "status must use the exact enum")
        if any(
            value is not False
            for value in (
                self.history_policy_admission_claimed,
                self.action_recommendation_present,
                self.request_views_persisted,
                self.exact_diff_preimages_persisted,
            )
        ):
            _fail(
                "EXECUTION_STATE_SCOPE_VIOLATION",
                "state receipt cannot claim history admission/advice or persist request bytes",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return execution_state_channel_receipt_projection(self)


def execution_state_channel_receipt_projection(
    value: ExecutionStateChannelReceiptV1,
) -> dict[str, JsonValue]:
    if type(value) is not ExecutionStateChannelReceiptV1:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "receipt must use the exact v1 contract")
    return {
        "schema_version": value.schema_version,
        "logical_call_id": value.logical_call_id,
        "status": value.status.value,
        "history_receipt_sha256": value.history_receipt_sha256,
        "raw_request_sha256": value.raw_request_sha256,
        "history_final_request_sha256": value.history_final_request_sha256,
        "candidate_request_sha256": value.candidate_request_sha256,
        "final_request_sha256": value.final_request_sha256,
        "execution_state_view_sha256": value.execution_state_view_sha256,
        "prompt_view_render_result_sha256": value.prompt_view_render_result_sha256,
        "adapter_declaration_sha256": value.adapter_declaration_sha256,
        "exact_diff_sha256": value.exact_diff_sha256,
        "would_augment": value.would_augment,
        "augmentation_applied": value.augmentation_applied,
        "fallback_reason": None if value.fallback_reason is None else value.fallback_reason.value,
        "validation_checks": list(value.validation_checks),
        "history_policy_admission_claimed": value.history_policy_admission_claimed,
        "action_recommendation_present": value.action_recommendation_present,
        "request_views_persisted": value.request_views_persisted,
        "exact_diff_preimages_persisted": value.exact_diff_preimages_persisted,
    }


def snapshot_execution_state_channel_receipt(
    value: ExecutionStateChannelReceiptV1,
) -> ExecutionStateChannelReceiptV1:
    if type(value) is not ExecutionStateChannelReceiptV1:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "receipt must use the exact v1 contract")
    return ExecutionStateChannelReceiptV1(
        **{field.name: getattr(value, field.name) for field in fields(value)}
    )


def execution_state_channel_receipt_sha256(value: ExecutionStateChannelReceiptV1) -> str:
    trusted = snapshot_execution_state_channel_receipt(value)
    return canonical_sha256(cast(JsonValue, trusted.to_dict()))


HistorySentinelResultV1 = SentinelResult | RuntimeVerticalSentinelResultV1


def _snapshot_sentinel_receipt(value: SentinelReceipt) -> SentinelReceipt:
    if type(value) is not SentinelReceipt:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "history receipt must use the exact type")
    return SentinelReceipt(**{item.name: getattr(value, item.name) for item in fields(value)})


def _snapshot_sentinel_result(value: SentinelResult) -> SentinelResult:
    if type(value) is not SentinelResult:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "history result must use the exact type")
    return SentinelResult(
        receipt=_snapshot_sentinel_receipt(value.receipt),
        _raw_request_json=canonical_json_bytes(value.raw_request),
        _candidate_request_json=canonical_json_bytes(value.candidate_request),
        _final_request_json=canonical_json_bytes(value.final_request),
    )


def _snapshot_history_result(value: HistorySentinelResultV1) -> HistorySentinelResultV1:
    if type(value) is SentinelResult:
        return _snapshot_sentinel_result(value)
    if type(value) is RuntimeVerticalSentinelResultV1:
        return snapshot_vertical_sentinel_result(value)
    _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "history result uses an unsupported contract")


def _history_receipt(value: HistorySentinelResultV1) -> SentinelReceipt:
    return _snapshot_sentinel_receipt(value.receipt)


def history_result_receipt_sha256(value: HistorySentinelResultV1) -> str:
    """Return the canonical base history-receipt hash without changing its meaning."""

    trusted = _snapshot_history_result(value)
    return canonical_sha256(cast(JsonValue, _history_receipt(trusted).to_dict()))


@dataclass(frozen=True, slots=True, init=False)
class ExecutionStateCompositeResultV1:
    """Detached transport result that preserves the independent history result."""

    _history_result: HistorySentinelResultV1
    _execution_state_receipt: ExecutionStateChannelReceiptV1
    _candidate_request_json: bytes = field(repr=False)
    _final_request_json: bytes = field(repr=False)
    schema_version: str

    def __init__(
        self,
        *,
        history_result: HistorySentinelResultV1,
        execution_state_receipt: ExecutionStateChannelReceiptV1,
        candidate_request: JsonValue,
        final_request: JsonValue,
        schema_version: str = EXECUTION_STATE_COMPOSITE_RESULT_SCHEMA_VERSION,
    ) -> None:
        if type(schema_version) is not str or schema_version != (
            EXECUTION_STATE_COMPOSITE_RESULT_SCHEMA_VERSION
        ):
            _fail("UNKNOWN_EXECUTION_STATE_SCHEMA", "unknown composite-result schema")
        history = _snapshot_history_result(history_result)
        receipt = snapshot_execution_state_channel_receipt(execution_state_receipt)
        candidate_bytes = canonical_json_bytes(candidate_request)
        final_bytes = canonical_json_bytes(final_request)
        base_receipt = _history_receipt(history)
        if receipt.logical_call_id != base_receipt.logical_call_id:
            _fail("EXECUTION_STATE_CALL_BINDING_MISMATCH", "history and state calls differ")
        if receipt.history_receipt_sha256 != canonical_sha256(
            cast(JsonValue, base_receipt.to_dict())
        ):
            _fail("EXECUTION_STATE_HISTORY_RECEIPT_MISMATCH", "history receipt hash differs")
        raw = history.raw_request
        history_final = history.final_request
        if canonical_sha256(raw) != receipt.raw_request_sha256 or (
            canonical_sha256(history_final) != receipt.history_final_request_sha256
        ):
            _fail("EXECUTION_STATE_REQUEST_BINDING_MISMATCH", "base request hashes differ")
        candidate = _parse_canonical_bytes(candidate_bytes, "state candidate")
        final = _parse_canonical_bytes(final_bytes, "effective final request")
        if canonical_sha256(candidate) != receipt.candidate_request_sha256 or (
            canonical_sha256(final) != receipt.final_request_sha256
        ):
            _fail("EXECUTION_STATE_REQUEST_BINDING_MISMATCH", "composite request hashes differ")
        expected_final = (
            candidate
            if receipt.status is ExecutionStateChannelStatusV1.APPLIED
            else raw
            if receipt.status is ExecutionStateChannelStatusV1.CANCELLED
            else history_final
        )
        if final != expected_final:
            _fail("EXECUTION_STATE_SELECTION_INVALID", "composite final selection differs")
        object.__setattr__(self, "_history_result", history)
        object.__setattr__(self, "_execution_state_receipt", receipt)
        object.__setattr__(self, "_candidate_request_json", candidate_bytes)
        object.__setattr__(self, "_final_request_json", final_bytes)
        object.__setattr__(self, "schema_version", schema_version)

    @property
    def history_result(self) -> HistorySentinelResultV1:
        return _snapshot_history_result(self._history_result)

    @property
    def history_receipt(self) -> SentinelReceipt:
        return _history_receipt(self._history_result)

    @property
    def execution_state_receipt(self) -> ExecutionStateChannelReceiptV1:
        return snapshot_execution_state_channel_receipt(self._execution_state_receipt)

    @property
    def raw_request(self) -> JsonValue:
        return self._history_result.raw_request

    @property
    def raw_request_sha256(self) -> str:
        return self._execution_state_receipt.raw_request_sha256

    @property
    def candidate_request(self) -> JsonValue:
        return _parse_canonical_bytes(self._candidate_request_json, "state candidate")

    @property
    def final_request(self) -> JsonValue:
        return _parse_canonical_bytes(self._final_request_json, "effective final request")

    @property
    def use_transformed_request(self) -> bool:
        return self.final_request != self.raw_request


def build_execution_state_channel_receipt(
    *,
    history_result: HistorySentinelResultV1,
    status: ExecutionStateChannelStatusV1,
    execution_state_view: ExecutionStateViewV1 | None = None,
    render_result: PromptViewRenderResultV1 | None = None,
    fallback_reason: ExecutionStateFallbackReasonV1 | None = None,
) -> ExecutionStateChannelReceiptV1:
    """Validate inputs and build a hash-only state-channel receipt."""

    history = _snapshot_history_result(history_result)
    history_receipt = _history_receipt(history)
    raw = history.raw_request
    history_final = history.final_request
    raw_sha256 = canonical_sha256(raw)
    history_final_sha256 = canonical_sha256(history_final)
    view_sha256: str | None = None
    render_sha256: str | None = None
    adapter_sha256: str | None = None
    exact_diff_sha256: str | None = None

    if status in (
        ExecutionStateChannelStatusV1.APPLIED,
        ExecutionStateChannelStatusV1.SHADOW,
    ):
        if type(execution_state_view) is not ExecutionStateViewV1 or (
            type(render_result) is not PromptViewRenderResultV1
        ):
            _fail("EXECUTION_STATE_PROOF_MISSING", "success requires exact view and render types")
        if fallback_reason is not None:
            _fail("EXECUTION_STATE_SELECTION_INVALID", "success cannot carry fallback reason")
        if execution_state_view.logical_call_id != history_receipt.logical_call_id or (
            execution_state_view.source_request_sha256 != raw_sha256
        ):
            _fail("EXECUTION_STATE_VIEW_BINDING_MISMATCH", "view binds another actor call")
        if render_result.raw_request != raw or render_result.history_candidate != history_final:
            _fail("EXECUTION_STATE_RENDER_BINDING_MISMATCH", "render binds another base result")
        view_sha256 = execution_state_view_sha256(execution_state_view)
        if render_result.execution_state_view_sha256 != view_sha256:
            _fail("EXECUTION_STATE_RENDER_BINDING_MISMATCH", "render binds another state view")
        candidate = render_result.candidate_request
        render_sha256 = prompt_view_render_result_sha256(render_result)
        adapter_sha256 = render_result.adapter_declaration_sha256
        exact_diff_sha256 = render_result.exact_diff_sha256
        final = candidate if status is ExecutionStateChannelStatusV1.APPLIED else history_final
        validation_checks: tuple[str, ...] = _SUCCESS_CHECKS
        would_augment = True
        augmentation_applied = status is ExecutionStateChannelStatusV1.APPLIED
    elif status in (
        ExecutionStateChannelStatusV1.FAILED,
        ExecutionStateChannelStatusV1.UNAVAILABLE,
    ):
        if render_result is not None:
            _fail("EXECUTION_STATE_FALLBACK_PROOF_INVALID", "fallback cannot accept a render")
        if fallback_reason is None:
            _fail("EXECUTION_STATE_FALLBACK_REASON_MISSING", "fallback reason is required")
        if execution_state_view is not None:
            if type(execution_state_view) is not ExecutionStateViewV1:
                _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "state view has a foreign type")
            if execution_state_view.logical_call_id != history_receipt.logical_call_id or (
                execution_state_view.source_request_sha256 != raw_sha256
            ):
                _fail("EXECUTION_STATE_VIEW_BINDING_MISMATCH", "view binds another actor call")
            view_sha256 = execution_state_view_sha256(execution_state_view)
        candidate = history_final
        final = history_final
        validation_checks = _FALLBACK_CHECKS
        would_augment = False
        augmentation_applied = False
    elif status is ExecutionStateChannelStatusV1.CANCELLED:
        if render_result is not None:
            _fail("EXECUTION_STATE_FALLBACK_PROOF_INVALID", "kill cannot accept a render")
        if fallback_reason is not ExecutionStateFallbackReasonV1.GLOBAL_KILL_SWITCH_CHANGED:
            _fail("EXECUTION_STATE_FALLBACK_REASON_MISSING", "kill cancellation is required")
        if execution_state_view is not None:
            if type(execution_state_view) is not ExecutionStateViewV1:
                _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "state view has a foreign type")
            if execution_state_view.logical_call_id != history_receipt.logical_call_id or (
                execution_state_view.source_request_sha256 != raw_sha256
            ):
                _fail("EXECUTION_STATE_VIEW_BINDING_MISMATCH", "view binds another actor call")
            view_sha256 = execution_state_view_sha256(execution_state_view)
        candidate = raw
        final = raw
        validation_checks = _CANCELLED_CHECKS
        would_augment = False
        augmentation_applied = False
    else:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "status must use the exact enum")

    return ExecutionStateChannelReceiptV1(
        logical_call_id=history_receipt.logical_call_id,
        status=status,
        history_receipt_sha256=history_result_receipt_sha256(history),
        raw_request_sha256=raw_sha256,
        history_final_request_sha256=history_final_sha256,
        candidate_request_sha256=canonical_sha256(candidate),
        final_request_sha256=canonical_sha256(final),
        execution_state_view_sha256=view_sha256,
        prompt_view_render_result_sha256=render_sha256,
        adapter_declaration_sha256=adapter_sha256,
        exact_diff_sha256=exact_diff_sha256,
        would_augment=would_augment,
        augmentation_applied=augmentation_applied,
        fallback_reason=fallback_reason,
        validation_checks=validation_checks,
    )


def build_execution_state_composite_result(
    *,
    history_result: HistorySentinelResultV1,
    status: ExecutionStateChannelStatusV1,
    execution_state_view: ExecutionStateViewV1 | None = None,
    render_result: PromptViewRenderResultV1 | None = None,
    fallback_reason: ExecutionStateFallbackReasonV1 | None = None,
) -> ExecutionStateCompositeResultV1:
    """Build a detached transport result after receipt publication succeeds."""

    history = _snapshot_history_result(history_result)
    receipt = build_execution_state_channel_receipt(
        history_result=history,
        status=status,
        execution_state_view=execution_state_view,
        render_result=render_result,
        fallback_reason=fallback_reason,
    )
    if status in (
        ExecutionStateChannelStatusV1.APPLIED,
        ExecutionStateChannelStatusV1.SHADOW,
    ):
        if type(render_result) is not PromptViewRenderResultV1:
            _fail("EXECUTION_STATE_PROOF_MISSING", "success requires an exact render result")
        candidate = render_result.candidate_request
    elif status is ExecutionStateChannelStatusV1.CANCELLED:
        candidate = history.raw_request
    else:
        candidate = history.final_request
    final = (
        candidate
        if status is ExecutionStateChannelStatusV1.APPLIED
        else history.raw_request
        if status is ExecutionStateChannelStatusV1.CANCELLED
        else history.final_request
    )
    return ExecutionStateCompositeResultV1(
        history_result=history,
        execution_state_receipt=receipt,
        candidate_request=candidate,
        final_request=final,
    )


def snapshot_execution_state_composite_result(
    value: ExecutionStateCompositeResultV1,
) -> ExecutionStateCompositeResultV1:
    if type(value) is not ExecutionStateCompositeResultV1:
        _fail("UNTRUSTED_EXECUTION_STATE_TYPE", "result must use the exact v1 contract")
    return ExecutionStateCompositeResultV1(
        history_result=value.history_result,
        execution_state_receipt=value.execution_state_receipt,
        candidate_request=value.candidate_request,
        final_request=value.final_request,
        schema_version=value.schema_version,
    )


def execution_state_composite_result_sha256(value: ExecutionStateCompositeResultV1) -> str:
    trusted = snapshot_execution_state_composite_result(value)
    return canonical_sha256(
        cast(
            JsonValue,
            {
                "schema_version": trusted.schema_version,
                "execution_state_receipt_sha256": execution_state_channel_receipt_sha256(
                    trusted.execution_state_receipt
                ),
                "raw_request_sha256": trusted.raw_request_sha256,
                "candidate_request_sha256": canonical_sha256(trusted.candidate_request),
                "final_request_sha256": canonical_sha256(trusted.final_request),
            },
        )
    )


@runtime_checkable
class ExecutionStateReceiptTransactionV1(Protocol):
    def commit(self, receipt: ExecutionStateChannelReceiptV1) -> None: ...

    def abort(self) -> None: ...


@runtime_checkable
class ExecutionStateReceiptSinkV1(Protocol):
    def begin(self, logical_call_id: str) -> ExecutionStateReceiptTransactionV1: ...


class _MemoryExecutionStateReceiptTransactionV1:
    def __init__(self, sink: MemoryExecutionStateReceiptSinkV1, logical_call_id: str) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._finished = False
        self._lock = Lock()

    def commit(self, receipt: ExecutionStateChannelReceiptV1) -> None:
        with self._lock:
            if self._finished:
                raise RuntimeError("execution-state receipt transaction is already finished")
            trusted = snapshot_execution_state_channel_receipt(receipt)
            if trusted.logical_call_id != self._logical_call_id:
                self._sink._abort(self._logical_call_id)
                self._finished = True
                raise ValueError("receipt logical_call_id differs from its transaction")
            self._sink._commit(self._logical_call_id, trusted)
            self._finished = True

    def abort(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._sink._abort(self._logical_call_id)
            self._finished = True


class MemoryExecutionStateReceiptSinkV1:
    """Thread-safe in-memory hash-only receipt sink."""

    def __init__(self) -> None:
        self._receipts: list[ExecutionStateChannelReceiptV1] = []
        self._active_ids: set[str] = set()
        self._committed_ids: set[str] = set()
        self._lock = Lock()

    @property
    def receipts(self) -> tuple[ExecutionStateChannelReceiptV1, ...]:
        with self._lock:
            return tuple(snapshot_execution_state_channel_receipt(item) for item in self._receipts)

    def begin(self, logical_call_id: str) -> _MemoryExecutionStateReceiptTransactionV1:
        _require_runtime_id(logical_call_id, "logical_call_id")
        with self._lock:
            if logical_call_id in self._active_ids or logical_call_id in self._committed_ids:
                raise FileExistsError("logical-call execution-state receipt already exists")
            self._active_ids.add(logical_call_id)
        return _MemoryExecutionStateReceiptTransactionV1(self, logical_call_id)

    def _commit(self, logical_call_id: str, receipt: ExecutionStateChannelReceiptV1) -> None:
        with self._lock:
            if logical_call_id not in self._active_ids:
                raise RuntimeError("execution-state receipt transaction is not active")
            self._active_ids.remove(logical_call_id)
            self._committed_ids.add(logical_call_id)
            self._receipts.append(receipt)

    def _abort(self, logical_call_id: str) -> None:
        with self._lock:
            self._active_ids.discard(logical_call_id)


class _ExternalExecutionStateReceiptTransactionV1:
    def __init__(
        self,
        *,
        sink: ExternalExecutionStateReceiptSinkV1,
        logical_call_id: str,
        directory_fd: int,
        file_fd: int,
        destination: str,
    ) -> None:
        self._sink = sink
        self._logical_call_id = logical_call_id
        self._directory_fd = directory_fd
        self._file_fd = file_fd
        self._destination = destination
        self._finished = False
        self._lock = Lock()

    def commit(self, receipt: ExecutionStateChannelReceiptV1) -> None:
        trusted = snapshot_execution_state_channel_receipt(receipt)
        payload = canonical_json_bytes(cast(JsonValue, trusted.to_dict()))
        with self._lock:
            if self._finished:
                raise RuntimeError("execution-state receipt transaction is already finished")
            if trusted.logical_call_id != self._logical_call_id:
                self._finish()
                raise ValueError("receipt logical_call_id differs from its transaction")
            published = False
            try:
                os.ftruncate(self._file_fd, 0)
                os.lseek(self._file_fd, 0, os.SEEK_SET)
                self._write_all(self._file_fd, payload)
                os.fsync(self._file_fd)
                info = os.fstat(self._file_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 0
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid()
                    or info.st_gid != os.getegid()
                    or info.st_size != len(payload)
                ):
                    raise OSError("execution-state receipt metadata changed before publication")
                self._sink._validate_open_root(self._directory_fd)
                os.link(
                    f"/proc/self/fd/{self._file_fd}",
                    self._destination,
                    dst_dir_fd=self._directory_fd,
                    follow_symlinks=True,
                )
                published = True
                os.fsync(self._directory_fd)
            except Exception:
                if published:
                    self._rollback_published()
                self._finish()
                raise
            self._finish()

    def abort(self) -> None:
        with self._lock:
            if not self._finished:
                self._finish()

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short execution-state receipt write")
            view = view[written:]

    def _rollback_published(self) -> None:
        try:
            os.unlink(self._destination, dir_fd=self._directory_fd)
        except FileNotFoundError:
            return
        except OSError:
            return
        try:
            os.fsync(self._directory_fd)
        except OSError:
            pass

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            os.close(self._file_fd)
        except OSError:
            pass
        try:
            os.close(self._directory_fd)
        except OSError:
            pass
        self._sink._release(self._logical_call_id)


class ExternalExecutionStateReceiptSinkV1:
    """Owner-only atomic no-replace external sink for state-channel receipts."""

    def __init__(self, root: Path, *, repository_root: Path | None = None) -> None:
        if not isinstance(cast(object, root), Path) or not root.is_absolute():
            raise ValueError("execution-state receipt root must be an absolute path")
        repo = (
            Path(__file__).resolve().parents[5]
            if repository_root is None
            else repository_root.resolve()
        )
        resolved_parent = root.parent.resolve(strict=True)
        resolved = resolved_parent / root.name
        if resolved == repo or resolved.is_relative_to(repo):
            raise ValueError("execution-state sidecars must remain outside the Git repository")
        root.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("execution-state receipt root must be a real directory")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError("execution-state receipt root must be owner-only")
        self._root = resolved
        self._root_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_gid)
        self._active_ids: set[str] = set()
        self._lock = Lock()

    @property
    def root(self) -> Path:
        return self._root

    def begin(self, logical_call_id: str) -> _ExternalExecutionStateReceiptTransactionV1:
        _require_runtime_id(logical_call_id, "logical_call_id")
        destination = f"{logical_call_id}.execution-state-receipt.v1.json"
        with self._lock:
            if logical_call_id in self._active_ids:
                raise FileExistsError("logical-call execution-state receipt already exists")
            self._active_ids.add(logical_call_id)
        directory_fd = -1
        file_fd = -1
        try:
            directory_fd = self._open_root()
            try:
                os.stat(destination, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("logical-call execution-state receipt already exists")
            if not hasattr(os, "O_TMPFILE"):
                raise OSError("anonymous execution-state receipt transactions are unavailable")
            file_fd = os.open(".", os.O_RDWR | os.O_TMPFILE, 0o600, dir_fd=directory_fd)
            _ExternalExecutionStateReceiptTransactionV1._write_all(file_fd, _ADMISSION_PROBE)
            os.fsync(file_fd)
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 0
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_gid != os.getegid()
                or info.st_size != len(_ADMISSION_PROBE)
            ):
                raise OSError("execution-state receipt admission metadata is invalid")
            linked = os.stat(f"/proc/self/fd/{file_fd}", follow_symlinks=True)
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                raise OSError("execution-state receipt fd link source is unavailable")
            return _ExternalExecutionStateReceiptTransactionV1(
                sink=self,
                logical_call_id=logical_call_id,
                directory_fd=directory_fd,
                file_fd=file_fd,
                destination=destination,
            )
        except Exception:
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
            self._release(logical_call_id)
            raise

    def _open_root(self) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(self._root, flags)
        except OSError as exc:
            raise OSError("execution-state receipt root cannot be reopened safely") from exc
        try:
            info = os.fstat(directory_fd)
        except OSError:
            os.close(directory_fd)
            raise
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != self._root_identity
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            os.close(directory_fd)
            raise OSError("execution-state receipt root identity changed")
        return directory_fd

    def _validate_open_root(self, directory_fd: int) -> None:
        pinned = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(pinned.st_mode)
            or (pinned.st_dev, pinned.st_ino, pinned.st_uid, pinned.st_gid) != self._root_identity
            or stat.S_IMODE(pinned.st_mode) & 0o077
        ):
            raise OSError("pinned execution-state receipt root identity changed")
        reopened_fd = self._open_root()
        try:
            reopened = os.fstat(reopened_fd)
            if (reopened.st_dev, reopened.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise OSError("configured execution-state receipt root was replaced")
        finally:
            os.close(reopened_fd)

    def _release(self, logical_call_id: str) -> None:
        with self._lock:
            self._active_ids.discard(logical_call_id)


__all__ = [
    "EXECUTION_STATE_CHANNEL_RECEIPT_SCHEMA_VERSION",
    "EXECUTION_STATE_COMPOSITE_RESULT_SCHEMA_VERSION",
    "ExecutionStateChannelError",
    "ExecutionStateChannelReceiptV1",
    "ExecutionStateChannelStatusV1",
    "ExecutionStateCompositeResultV1",
    "ExecutionStateFallbackReasonV1",
    "ExecutionStateReceiptSinkV1",
    "ExecutionStateReceiptTransactionV1",
    "ExternalExecutionStateReceiptSinkV1",
    "HistorySentinelResultV1",
    "MemoryExecutionStateReceiptSinkV1",
    "build_execution_state_channel_receipt",
    "build_execution_state_composite_result",
    "execution_state_channel_receipt_projection",
    "execution_state_channel_receipt_sha256",
    "execution_state_composite_result_sha256",
    "history_result_receipt_sha256",
    "snapshot_execution_state_channel_receipt",
    "snapshot_execution_state_composite_result",
]
