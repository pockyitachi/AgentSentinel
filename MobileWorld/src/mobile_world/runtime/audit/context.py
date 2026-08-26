"""Context-local audit bindings for concurrent task execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Any

from mobile_world.runtime.audit.secret_policy import is_placeholder_credential


class ModelCallTrace:
    """Thread-safe, per-step ordering of logical model calls.

    Actor and nested grounder hooks share this small factual accumulator via
    the current :class:`AuditContext`.  The runner snapshots it after
    ``agent.predict()`` so ``agent_decision.source_model_call_ids`` does not
    depend on mutable agent-instance state.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._call_ids: list[str] = []
        self._seen: set[str] = set()
        self._terminal_event_ids: list[str] = []

    def record(self, model_call_id: str) -> None:
        """Record a logical call once, preserving first-observed order."""

        if not isinstance(model_call_id, str) or not model_call_id:
            raise ValueError("model_call_id must be a non-empty string")
        with self._lock:
            if model_call_id not in self._seen:
                self._seen.add(model_call_id)
                self._call_ids.append(model_call_id)

    def snapshot(self) -> tuple[str, ...]:
        """Return an immutable point-in-time view for a decision event."""

        with self._lock:
            return tuple(self._call_ids)

    def record_terminal(self, model_call_id: str, event_id: str) -> None:
        """Record a persisted provider terminal in observed causal order."""

        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")
        self.record(model_call_id)
        with self._lock:
            self._terminal_event_ids.append(event_id)

    def latest_terminal_event_id(self) -> str | None:
        """Return the most recently persisted provider terminal, if any."""

        with self._lock:
            return self._terminal_event_ids[-1] if self._terminal_event_ids else None


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Immutable correlation state for one point on the runtime call path."""

    run_id: str
    recorder: Any
    task_run_id: str | None = None
    step_id: str | None = None
    decision_id: str | None = None
    model_call_id: str | None = None
    retry_group_id: str | None = None
    adapter_attempt_index: int = 1
    adapter_retry_planned: bool = False
    store_stream_chunks: bool = True
    model_call_trace: ModelCallTrace | None = None
    execution_evidence_trace: Any = field(default=None, repr=False)
    known_secrets: tuple[str, ...] = field(default=(), repr=False)
    parent_event_id: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.adapter_attempt_index, bool)
            or not isinstance(self.adapter_attempt_index, int)
            or self.adapter_attempt_index < 1
        ):
            raise ValueError("adapter_attempt_index must be a positive integer")
        if not isinstance(self.adapter_retry_planned, bool):
            raise TypeError("adapter_retry_planned must be a bool")
        if not isinstance(self.store_stream_chunks, bool):
            raise TypeError("store_stream_chunks must be a bool")
        if not isinstance(self.known_secrets, tuple) or any(
            not isinstance(secret, str) or not secret for secret in self.known_secrets
        ):
            raise TypeError("known_secrets must be a tuple of non-empty strings")
        filtered_secrets = tuple(
            secret for secret in self.known_secrets if not is_placeholder_credential(secret)
        )
        if filtered_secrets != self.known_secrets:
            object.__setattr__(self, "known_secrets", filtered_secrets)

    def derive(self, **changes: Any) -> AuditContext:
        """Return a child scope while leaving the parent binding untouched."""

        return replace(self, **changes)

    def record_model_call(self, model_call_id: str) -> None:
        """Add a logical call to this step's trace when one is configured."""

        if self.model_call_trace is not None:
            self.model_call_trace.record(model_call_id)

    def source_model_call_ids(self) -> tuple[str, ...]:
        """Return logical calls observed for the current decision step."""

        if self.model_call_trace is None:
            return ()
        return self.model_call_trace.snapshot()

    def latest_model_terminal_event_id(self) -> str | None:
        """Return the latest provider terminal linked to this decision step."""

        if self.model_call_trace is None:
            return None
        return self.model_call_trace.latest_terminal_event_id()


CURRENT_AUDIT_CONTEXT: ContextVar[AuditContext | None] = ContextVar(
    "mobile_world_audit_context",
    default=None,
)


def get_audit_context() -> AuditContext | None:
    """Return the current binding, or ``None`` when audit is not bound."""

    return CURRENT_AUDIT_CONTEXT.get()


def require_audit_context() -> AuditContext:
    """Return the current binding or raise when an enabled hook is miswired."""

    context = get_audit_context()
    if context is None:
        raise LookupError("no audit context is bound")
    return context


def get_current_recorder(default: Any = None) -> Any:
    """Return the bound recorder without importing a concrete recorder type."""

    context = get_audit_context()
    return default if context is None else context.recorder


@contextmanager
def bind_audit_context(context: AuditContext) -> Iterator[AuditContext]:
    """Bind ``context`` and always restore the previous value in ``finally``."""

    token = CURRENT_AUDIT_CONTEXT.set(context)
    try:
        yield context
    finally:
        CURRENT_AUDIT_CONTEXT.reset(token)


@contextmanager
def bind_audit_scope(**changes: Any) -> Iterator[AuditContext]:
    """Derive and temporarily bind a nested scope from the current context."""

    child = require_audit_context().derive(**changes)
    with bind_audit_context(child):
        yield child
