"""One-way production run-fatal latch for unsafe live-attempt terminals.

The latch is deliberately small and module-owned.  It carries no authority and
cannot make an execution path live; the production driver shares one instance
across every per-call audit so an unconfirmed worker termination or incomplete
post-dispatch cost accounting permanently blocks the current run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from threading import Lock
from typing import Final

from mobile_world.offline.causal_replay.contracts import JsonValue
from mobile_world.runtime.sentinel.r2_4.contracts import R24ContractError, canonical_sha256
from mobile_world.runtime.sentinel.r2_4.live_attempt import (
    LiveAttemptCostStatusV1,
    LiveAttemptReceiptV1,
    LiveAttemptStatusV1,
    live_attempt_receipt_sha256,
    snapshot_live_attempt_receipt,
)

PRODUCTION_RUN_FATAL_STATE_SCHEMA_VERSION: Final[str] = (
    "mobileworld.runtime.sentinel-r2.4-production-run-fatal-state/v1"
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LATCH_SEAL: Final[object] = object()
_TERMINATION_UNCONFIRMED: Final[str] = "TERMINATION_UNCONFIRMED"
_LIVE_COST_ACCOUNTING_UNKNOWN: Final[str] = "LIVE_COST_ACCOUNTING_UNKNOWN"
_FATAL_REASONS: Final[frozenset[str]] = frozenset(
    {_TERMINATION_UNCONFIRMED, _LIVE_COST_ACCOUNTING_UNKNOWN}
)


class ProductionRunFatalError(R24ContractError):
    """Typed refusal after a production attempt makes dispatch unsafe."""


@dataclass(frozen=True, slots=True)
class ProductionRunFatalStateV1:
    """Content-addressed identity of the first run-fatal attempt."""

    logical_call_id: str
    attempt_receipt_sha256: str
    failure_code: str = _TERMINATION_UNCONFIRMED
    schema_version: str = PRODUCTION_RUN_FATAL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PRODUCTION_RUN_FATAL_STATE_SCHEMA_VERSION:
            raise ProductionRunFatalError("UNKNOWN_SCHEMA_VERSION", "run-fatal state differs")
        if (
            type(self.logical_call_id) is not str
            or _RUNTIME_ID.fullmatch(self.logical_call_id) is None
        ):
            raise ProductionRunFatalError("INVALID_RUNTIME_ID", "logical call ID is invalid")
        if (
            type(self.attempt_receipt_sha256) is not str
            or _SHA256.fullmatch(self.attempt_receipt_sha256) is None
        ):
            raise ProductionRunFatalError("INVALID_SHA256", "attempt receipt hash is invalid")
        if type(self.failure_code) is not str or self.failure_code not in _FATAL_REASONS:
            raise ProductionRunFatalError("INVALID_FATAL_REASON", "run-fatal reason differs")


class ProductionRunFatalLatchV1:
    """Irreversible process-local latch shared by one production run."""

    __slots__ = ("_lock", "_state")

    def __init__(self, *, _seal: object) -> None:
        if _seal is not _LATCH_SEAL:
            raise PermissionError("production run-fatal latch is module-owned")
        self._lock = Lock()
        self._state: ProductionRunFatalStateV1 | None = None

    def observe_attempts(
        self,
        *,
        logical_call_id: str,
        attempts: tuple[LiveAttemptReceiptV1, ...],
    ) -> ProductionRunFatalStateV1 | None:
        """Trip on the first unsafe terminal, with worker uncertainty first."""

        if type(attempts) is not tuple:
            raise ProductionRunFatalError("UNTRUSTED_TYPE", "attempts must be an exact tuple")
        trusted = tuple(snapshot_live_attempt_receipt(item) for item in attempts)
        if any(item.logical_call_id != logical_call_id for item in trusted):
            raise ProductionRunFatalError("TRACE_BINDING_MISMATCH", "attempt logical call differs")
        terminal = next(
            (
                item
                for item in trusted
                if item.status is LiveAttemptStatusV1.TERMINATION_UNCONFIRMED
            ),
            None,
        )
        failure_code = _TERMINATION_UNCONFIRMED
        if terminal is None:
            terminal = next(
                (
                    item
                    for item in trusted
                    if item.dispatch_count > 0
                    and item.cost_status is LiveAttemptCostStatusV1.UNKNOWN
                ),
                None,
            )
            failure_code = _LIVE_COST_ACCOUNTING_UNKNOWN
        if terminal is None:
            return self.state
        candidate = ProductionRunFatalStateV1(
            logical_call_id=logical_call_id,
            attempt_receipt_sha256=live_attempt_receipt_sha256(terminal),
            failure_code=failure_code,
        )
        with self._lock:
            if self._state is None:
                self._state = candidate
            return self._state

    def require_clear(self) -> None:
        """Fail closed once any unsafe live-attempt terminal was observed."""

        with self._lock:
            state = self._state
        if state is not None:
            if state.failure_code == _TERMINATION_UNCONFIRMED:
                error_code = "RUN_FATAL_TERMINATION_UNCONFIRMED"
                message = "a live attempt worker remains unconfirmed; this run cannot dispatch"
            else:
                error_code = "RUN_FATAL_LIVE_COST_ACCOUNTING_UNKNOWN"
                message = (
                    "a dispatched live attempt lacks exact cost accounting; "
                    "this run cannot dispatch"
                )
            raise ProductionRunFatalError(
                error_code,
                message,
            )

    @property
    def state(self) -> ProductionRunFatalStateV1 | None:
        with self._lock:
            return self._state


def build_production_run_fatal_latch_v1() -> ProductionRunFatalLatchV1:
    """Build the exact one-way latch used by the sealed production driver."""

    return ProductionRunFatalLatchV1(_seal=_LATCH_SEAL)


def production_run_fatal_state_projection(value: ProductionRunFatalStateV1) -> JsonValue:
    if type(value) is not ProductionRunFatalStateV1:
        raise ProductionRunFatalError("UNTRUSTED_TYPE", "run-fatal state type differs")
    return {
        "attempt_receipt_sha256": value.attempt_receipt_sha256,
        "failure_code": value.failure_code,
        "logical_call_id": value.logical_call_id,
        "schema_version": value.schema_version,
    }


def production_run_fatal_state_sha256(value: ProductionRunFatalStateV1) -> str:
    return canonical_sha256(production_run_fatal_state_projection(value))


__all__ = [
    "PRODUCTION_RUN_FATAL_STATE_SCHEMA_VERSION",
    "ProductionRunFatalError",
    "ProductionRunFatalLatchV1",
    "ProductionRunFatalStateV1",
    "build_production_run_fatal_latch_v1",
    "production_run_fatal_state_projection",
    "production_run_fatal_state_sha256",
]
