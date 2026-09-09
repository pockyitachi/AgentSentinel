"""Flat Prompt Sentinel used by ordinary audited ``mw eval``.

One actor call has one immutable Original, at most one history-policy provider
call, one final request, and at most one best-effort log record. Local
execution facts do not depend on the provider call. The historical rubric,
promotion, nested-result, and multi-receipt stacks are not imported here.
"""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread, current_thread
from time import monotonic_ns
from typing import Any, Protocol, cast

from openai import DefaultHttpxClient, OpenAI, Timeout

from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.sentinel.flat_codec import (
    FlatCodecRegistry,
    FlatHistory,
    FlatHistoryStatus,
    build_flat_codec_registry,
)
from mobile_world.runtime.sentinel.flat_contracts import (
    FlatChannelStatus,
    FlatSentinelMode,
    FlatSentinelResult,
    JsonValue,
    strict_json_bytes,
)
from mobile_world.runtime.sentinel.flat_evidence import (
    FlatCollectorEvidenceSource,
    FlatEvidence,
)
from mobile_world.runtime.sentinel.flat_policy import (
    DirectOpenAIResponsesTransport,
    FlatHistoryPolicy,
    FlatPolicyOutcome,
    FlatPolicyTransport,
)
from mobile_world.runtime.sentinel.flat_render import render_flat_request


class FlatCallLog(Protocol):
    """Optional sink for one secret-free record per logical actor call."""

    def write(self, record: dict[str, JsonValue]) -> None: ...


class FlatEvidenceSource(Protocol):
    def build(
        self,
        *,
        request: JsonValue,
        logical_call_id: str,
        host_id: str,
        history: FlatHistory,
    ) -> FlatEvidence: ...


class NullFlatCallLog:
    def write(self, record: dict[str, JsonValue]) -> None:
        del record


class ExternalFlatCallLog:
    """Write one file directly; logging never participates in selection."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("flat Sentinel log root must be an absolute Path")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("flat Sentinel log root must be a real directory")
        os.chmod(root, 0o700)
        self._root = root

    def write(self, record: dict[str, JsonValue]) -> None:
        data = strict_json_bytes(cast(JsonValue, record)) + b"\n"
        logical_call_id = record.get("logical_call_id")
        if type(logical_call_id) is not str:
            raise ValueError("flat Sentinel log record lacks a logical call ID")
        target = self._root / f"{logical_call_id}.flat-sentinel.json"
        descriptor = -1
        created = False
        try:
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            created = True
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, data[offset:])
                if written <= 0:
                    raise OSError("flat Sentinel log write made no progress")
                offset += written
            os.fsync(descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OSError("flat Sentinel log file metadata is invalid")
        except Exception:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = -1
            if created:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class FlatSentinelSwitch:
    """Small process-local kill switch checked before and after semantic work."""

    def __init__(self) -> None:
        self._active = False
        self._generation = 0
        self._lock = Lock()

    def set_active(self, active: bool) -> None:
        if type(active) is not bool:
            raise TypeError("kill switch state must be exact bool")
        with self._lock:
            if self._active != active:
                self._active = active
                self._generation += 1

    @property
    def active(self) -> bool:
        return self.snapshot()[0]

    def snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._active, self._generation


FLAT_SENTINEL_KILL_SWITCH = FlatSentinelSwitch()
GLOBAL_SENTINEL_KILL_SWITCH = FLAT_SENTINEL_KILL_SWITCH


def set_global_sentinel_kill_switch(active: bool) -> None:
    """Set the emergency switch used by ordinary flat ``mw eval``."""

    FLAT_SENTINEL_KILL_SWITCH.set_active(active)


def global_sentinel_kill_switch_active() -> bool:
    return FLAT_SENTINEL_KILL_SWITCH.active


@dataclass(frozen=True, slots=True)
class _FlatCallContext:
    logical_call_id: str
    host_id: str
    history_codec_id: str
    call_role: str
    attributes: Mapping[str, Any]


class FlatLogicalCall:
    """Cache one result across provider and adapter retries of one decision."""

    def __init__(self, sentinel: FlatPromptSentinel, context: _FlatCallContext) -> None:
        self._sentinel = sentinel
        self._context = context
        self._result: FlatSentinelResult | None = None
        self._lock = Lock()

    def matches(
        self,
        sentinel: object,
        *,
        host_id: object,
        history_codec_id: object,
        call_role: object,
    ) -> bool:
        role = call_role.value if hasattr(call_role, "value") else call_role
        return (
            sentinel is self._sentinel
            and host_id == self._context.host_id
            and history_codec_id == self._context.history_codec_id
            and role == self._context.call_role
        )

    def before_model_call(self, request: JsonValue) -> FlatSentinelResult:
        with self._lock:
            if self._result is not None:
                return self._result
            encoded = strict_json_bytes(request)
            self._result = self._sentinel._evaluate(
                original_json=encoded,
                context=self._context,
            )
            return self._result


class FlatPromptSentinel:
    """Complete flat pre-call implementation for one MobileWorld task."""

    def __init__(
        self,
        *,
        mode: FlatSentinelMode,
        policy: FlatHistoryPolicy,
        evidence_source: FlatEvidenceSource,
        codec_registry: FlatCodecRegistry,
        log_sink: FlatCallLog,
        policy_timeout_seconds: float,
        kill_switch: FlatSentinelSwitch = FLAT_SENTINEL_KILL_SWITCH,
    ) -> None:
        if type(mode) is not FlatSentinelMode:
            raise TypeError("mode must use FlatSentinelMode")
        _positive_seconds(policy_timeout_seconds)
        self.mode = mode
        self.policy = policy
        self._evidence_source = evidence_source
        self._codec_registry = codec_registry
        self._log_sink = log_sink
        self._policy_timeout_seconds = float(policy_timeout_seconds)
        self._kill_switch = kill_switch
        self._current_call: ContextVar[FlatLogicalCall | None] = ContextVar(
            f"mobileworld_flat_sentinel_call_{id(self)}", default=None
        )
        self._poisoned = False
        self._state_lock = Lock()
        self._workers: set[Thread] = set()
        self._idle_callbacks: list[Callable[[], None]] = []

    def logical_call(
        self,
        *,
        host_id: str,
        history_codec_id: str,
        call_role: str = "actor",
        attributes: dict[str, Any] | None = None,
    ) -> FlatLogicalCall:
        if type(host_id) is not str or not host_id:
            raise ValueError("host_id must be non-empty text")
        if type(history_codec_id) is not str or not history_codec_id:
            raise ValueError("history_codec_id must be non-empty text")
        role = call_role.value if hasattr(call_role, "value") else call_role
        if type(role) is not str or not role:
            raise ValueError("call_role must be non-empty text")
        return FlatLogicalCall(
            self,
            _FlatCallContext(
                new_ulid(),
                host_id,
                history_codec_id,
                role,
                {} if attributes is None else dict(attributes),
            ),
        )

    def current_logical_call(self) -> FlatLogicalCall | None:
        return self._current_call.get()

    @contextmanager
    def bind_logical_call(self, call: FlatLogicalCall) -> Iterator[FlatLogicalCall]:
        if type(call) is not FlatLogicalCall or call._sentinel is not self:
            raise TypeError("logical call belongs to another flat Sentinel")
        token = self._current_call.set(call)
        try:
            yield call
        finally:
            self._current_call.reset(token)

    def close_when_idle(self, callback: Callable[[], None]) -> None:
        """Close a task resource now, or once its sole late provider call returns."""

        close_now = False
        with self._state_lock:
            if self._workers:
                self._idle_callbacks.append(callback)
            else:
                close_now = True
        if close_now:
            try:
                callback()
            except Exception:
                pass

    def _evaluate(
        self,
        *,
        original_json: bytes,
        context: _FlatCallContext,
    ) -> FlatSentinelResult:
        started_ns = monotonic_ns()
        switch_active, switch_generation = self._kill_switch.snapshot()
        if self.mode is FlatSentinelMode.OFF:
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.SKIPPED,
                None,
                FlatChannelStatus.SKIPPED,
                None,
            )
        if context.call_role != "actor":
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.SKIPPED,
                "SENTINEL_CALL_BYPASS",
                FlatChannelStatus.SKIPPED,
                "SENTINEL_CALL_BYPASS",
            )
        if switch_active:
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.CANCELLED,
                "KILL_SWITCH_ACTIVE",
                FlatChannelStatus.CANCELLED,
                "KILL_SWITCH_ACTIVE",
            )
        try:
            working_request = cast(JsonValue, json.loads(original_json))
            codec = self._codec_registry.by_id(context.history_codec_id)
            if type(working_request) is not dict:
                raise TypeError("actor request must be a JSON object")
            extracted = codec.extract(working_request, attributes=context.attributes)
        except Exception as exc:
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.FAILED,
                _reason(exc, "HISTORY_EXTRACTION_FAILED"),
                FlatChannelStatus.SKIPPED,
                "HISTORY_EXTRACTION_FAILED",
            )
        if (
            extracted.status not in {FlatHistoryStatus.READY, FlatHistoryStatus.NO_HISTORY}
            or extracted.host_id != context.host_id
            or extracted.codec_id != context.history_codec_id
        ):
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.FAILED,
                _bounded_reason(extracted.reason, "HISTORY_UNSUPPORTED"),
                FlatChannelStatus.SKIPPED,
                "HISTORY_UNSUPPORTED",
            )
        history = extracted
        target_count = len(history.spans)

        try:
            evidence = self._evidence_source.build(
                request=working_request,
                logical_call_id=context.logical_call_id,
                host_id=context.host_id,
                history=history,
            )
        except Exception as exc:
            reason = _reason(exc, "EVIDENCE_UNAVAILABLE")
            return self._original_result(
                original_json,
                context,
                started_ns,
                FlatChannelStatus.FAILED,
                reason,
                FlatChannelStatus.FAILED,
                reason,
                history_target_count=target_count,
            )
        with self._state_lock:
            poisoned = self._poisoned
        deadline_ns = started_ns + round(self._policy_timeout_seconds * 1_000_000_000)
        if target_count == 0:
            policy_outcome = None
            policy_error = None
            policy_called = False
        elif evidence.packet is None or evidence.current_image_data_url is None:
            policy_outcome = None
            policy_error = evidence.history_error or "HISTORY_EVIDENCE_UNAVAILABLE"
            policy_called = False
        elif poisoned:
            policy_outcome = None
            policy_error = "POLICY_DISABLED_AFTER_TIMEOUT"
            policy_called = False
        else:
            policy_outcome, policy_error, policy_called = self._evaluate_policy(
                evidence, deadline_ns
            )
        if policy_outcome is None:
            drops: tuple[str, ...] = ()
            if target_count == 0:
                history_status = FlatChannelStatus.NO_HISTORY
            elif policy_error in {"POLICY_TIMEOUT", "POLICY_DISABLED_AFTER_TIMEOUT"}:
                history_status = FlatChannelStatus.TIMED_OUT
            else:
                history_status = FlatChannelStatus.FAILED
            history_reason = policy_error
        else:
            drops = policy_outcome.drop_target_ids
            history_status = FlatChannelStatus.UNCHANGED
            history_reason = None

        try:
            render_request = cast(JsonValue, json.loads(original_json))
            candidate, applied_drops, repeat_count, state_error = render_flat_request(
                original=render_request,
                history=history,
                drop_target_ids=drops,
                execution_state=evidence.execution_state,
            )
        except Exception as exc:
            if drops:
                history_status = FlatChannelStatus.FAILED
                history_reason = _reason(exc, "HISTORY_RENDER_FAILED")
                try:
                    state_only_request = cast(JsonValue, json.loads(original_json))
                    candidate, applied_drops, repeat_count, state_error = render_flat_request(
                        original=state_only_request,
                        history=history,
                        drop_target_ids=(),
                        execution_state=evidence.execution_state,
                    )
                except Exception as state_exc:
                    candidate, applied_drops, repeat_count = state_only_request, 0, 0
                    state_error = _reason(state_exc, "EXECUTION_STATE_RENDER_FAILED")
            else:
                candidate, applied_drops, repeat_count = render_request, 0, 0
                state_error = _reason(exc, "EXECUTION_STATE_RENDER_FAILED")

        if policy_outcome is not None and applied_drops:
            history_status = (
                FlatChannelStatus.WOULD_APPLY
                if self.mode is FlatSentinelMode.SHADOW
                else FlatChannelStatus.APPLIED
            )
        if state_error is not None:
            state_status = FlatChannelStatus.FAILED
            state_reason = _bounded_reason(state_error, "EXECUTION_STATE_RENDER_FAILED")
        elif repeat_count:
            state_status = (
                FlatChannelStatus.WOULD_APPLY
                if self.mode is FlatSentinelMode.SHADOW
                else FlatChannelStatus.APPLIED
            )
            state_reason = None
        else:
            state_status = FlatChannelStatus.UNCHANGED
            state_reason = None

        try:
            candidate_json = strict_json_bytes(candidate)
        except Exception:
            candidate_json = original_json
            history_status = FlatChannelStatus.FAILED
            history_reason = "FINAL_REQUEST_INVALID"
            state_status = FlatChannelStatus.FAILED
            state_reason = "FINAL_REQUEST_INVALID"
            applied_drops = 0
            repeat_count = 0
        would_edit = candidate_json != original_json
        current_active, current_generation = self._kill_switch.snapshot()
        cancelled = current_active or current_generation != switch_generation
        if cancelled:
            if applied_drops:
                history_status = FlatChannelStatus.CANCELLED
                history_reason = "KILL_SWITCH_CHANGED"
            if repeat_count:
                state_status = FlatChannelStatus.CANCELLED
                state_reason = "KILL_SWITCH_CHANGED"
            would_edit = False
        edit_applied = self.mode is FlatSentinelMode.ACTIVE and would_edit and not cancelled
        final_json = candidate_json if edit_applied else original_json
        result = FlatSentinelResult(
            logical_call_id=context.logical_call_id,
            mode=self.mode,
            original_json=original_json,
            final_json=final_json,
            history_status=history_status,
            history_reason=history_reason,
            history_policy_called=policy_called,
            history_target_count=target_count,
            history_drop_count=applied_drops,
            execution_state_status=state_status,
            execution_state_reason=state_reason,
            execution_repeat_count=repeat_count,
            would_edit=would_edit,
            edit_applied=edit_applied,
            latency_ns=monotonic_ns() - started_ns,
            _provider_request=(cast(dict[str, JsonValue], candidate) if edit_applied else None),
        )
        self._publish(result)
        return result

    def _evaluate_policy(
        self, evidence: FlatEvidence, deadline_ns: int
    ) -> tuple[FlatPolicyOutcome | None, str | None, bool]:
        remaining = (deadline_ns - monotonic_ns()) / 1_000_000_000
        if remaining <= 0.01:
            self._poison()
            return None, "POLICY_TIMEOUT", False
        finished = Event()
        outcomes: list[FlatPolicyOutcome] = []
        failures: list[str] = []
        dispatch_lock = Lock()
        dispatch_cancelled = False
        dispatched = False
        packet = evidence.packet
        current_image_data_url = evidence.current_image_data_url
        if packet is None or current_image_data_url is None:
            return None, "HISTORY_EVIDENCE_UNAVAILABLE", False

        def before_dispatch() -> bool:
            nonlocal dispatched
            with dispatch_lock:
                if dispatch_cancelled or monotonic_ns() >= deadline_ns:
                    return False
                dispatched = True
                return True

        def run() -> None:
            try:
                outcomes.append(
                    self.policy.evaluate(
                        packet,
                        current_image_data_url,
                        timeout_seconds=max(0.001, remaining - 0.01),
                        before_dispatch=before_dispatch,
                    )
                )
            except Exception as exc:
                failures.append(_reason(exc, "POLICY_FAILED"))
            finally:
                callbacks = self._finish_worker(current_thread())
                finished.set()
                for callback in callbacks:
                    try:
                        callback()
                    except Exception:
                        pass

        worker = Thread(target=run, name="flat-sentinel-policy", daemon=True)
        with self._state_lock:
            self._workers.add(worker)
        try:
            worker.start()
        except Exception as exc:
            callbacks = self._finish_worker(worker)
            for callback in callbacks:
                try:
                    callback()
                except Exception:
                    pass
            return None, _reason(exc, "POLICY_WORKER_START_FAILED"), False
        wait_seconds = max(0.0, (deadline_ns - monotonic_ns()) / 1_000_000_000)
        if not finished.wait(wait_seconds):
            with dispatch_lock:
                dispatch_cancelled = True
                policy_called = dispatched
            self._poison()
            return None, "POLICY_TIMEOUT", policy_called
        with dispatch_lock:
            policy_called = dispatched
        if failures:
            return None, failures[0], policy_called
        if len(outcomes) != 1:
            return None, "POLICY_FAILED", policy_called
        return outcomes[0], None, policy_called

    def _finish_worker(self, worker: Thread) -> tuple[Callable[[], None], ...]:
        with self._state_lock:
            self._workers.discard(worker)
            if self._workers:
                return ()
            callbacks = tuple(self._idle_callbacks)
            self._idle_callbacks.clear()
            return callbacks

    def _poison(self) -> None:
        with self._state_lock:
            self._poisoned = True

    def _original_result(
        self,
        original_json: bytes,
        context: _FlatCallContext,
        started_ns: int,
        history_status: FlatChannelStatus,
        history_reason: str | None,
        state_status: FlatChannelStatus,
        state_reason: str | None,
        *,
        history_target_count: int = 0,
    ) -> FlatSentinelResult:
        result = FlatSentinelResult(
            logical_call_id=context.logical_call_id,
            mode=self.mode,
            original_json=original_json,
            final_json=original_json,
            history_status=history_status,
            history_reason=history_reason,
            history_policy_called=False,
            history_target_count=history_target_count,
            history_drop_count=0,
            execution_state_status=state_status,
            execution_state_reason=state_reason,
            execution_repeat_count=0,
            would_edit=False,
            edit_applied=False,
            latency_ns=monotonic_ns() - started_ns,
        )
        self._publish(result)
        return result

    def _publish(self, result: FlatSentinelResult) -> None:
        try:
            self._log_sink.write(result.to_log_dict())
        except Exception:
            pass


class FlatSentinelTaskRuntime:
    """Task-local owner used only to close a live transport once."""

    def __init__(
        self,
        *,
        sentinel: FlatPromptSentinel,
        close_transport: Callable[[], None] | None = None,
    ) -> None:
        self.sentinel = sentinel
        self._close_transport = close_transport
        self._closed = False
        self._lock = Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._close_transport is not None:
            self.sentinel.close_when_idle(self._close_transport)


class InjectedFlatSentinelFactory:
    """Injection-friendly factory for focused CPU tests and custom harnesses."""

    def __init__(
        self,
        *,
        mode: FlatSentinelMode,
        history_policy_transport: FlatPolicyTransport,
        log_sink: FlatCallLog,
        evidence_source: FlatEvidenceSource | None = None,
        codec_registry: FlatCodecRegistry | None = None,
        policy_timeout_seconds: float = 240.0,
        transport_timeout_seconds: float | None = None,
    ) -> None:
        if type(mode) is not FlatSentinelMode:
            raise TypeError("mode must use FlatSentinelMode")
        _positive_seconds(policy_timeout_seconds)
        if transport_timeout_seconds is None:
            transport_timeout_seconds = min(220.0, float(policy_timeout_seconds) * 0.9)
        _positive_seconds(transport_timeout_seconds)
        if transport_timeout_seconds >= policy_timeout_seconds:
            raise ValueError("transport timeout must be below the actor-call policy timeout")
        self._mode = mode
        self._transport = history_policy_transport
        self._log_sink = log_sink
        self._evidence_source = evidence_source or FlatCollectorEvidenceSource()
        self._codec_registry = codec_registry or build_flat_codec_registry()
        self._policy_timeout_seconds = float(policy_timeout_seconds)
        self._transport_timeout_seconds = float(transport_timeout_seconds)

    def __call__(self) -> FlatSentinelTaskRuntime:
        return FlatSentinelTaskRuntime(
            sentinel=FlatPromptSentinel(
                mode=self._mode,
                policy=FlatHistoryPolicy(
                    self._transport, timeout_seconds=self._transport_timeout_seconds
                ),
                evidence_source=self._evidence_source,
                codec_registry=self._codec_registry,
                log_sink=self._log_sink,
                policy_timeout_seconds=self._policy_timeout_seconds,
            )
        )


class FlatSentinelRunFactory:
    """Construct one direct, retry-disabled Sentinel provider per eval task."""

    def __init__(
        self,
        *,
        mode: FlatSentinelMode,
        api_key: str,
        log_root: Path,
        base_url: str = "https://api.openai.com/v1",
        policy_timeout_seconds: float = 240.0,
        transport_timeout_seconds: float = 220.0,
    ) -> None:
        if mode not in {FlatSentinelMode.SHADOW, FlatSentinelMode.ACTIVE}:
            raise ValueError("flat Sentinel mode must be SHADOW or ACTIVE")
        if type(api_key) is not str or not api_key:
            raise ValueError("flat Sentinel needs a non-empty API key")
        if not log_root.is_absolute():
            raise ValueError("log_root must be an absolute Path")
        if type(base_url) is not str or not base_url:
            raise ValueError("base_url must be non-empty text")
        _positive_seconds(policy_timeout_seconds)
        _positive_seconds(transport_timeout_seconds)
        if transport_timeout_seconds >= policy_timeout_seconds:
            raise ValueError("transport timeout must be below the actor-call policy timeout")
        self._mode = mode
        self._api_key = api_key
        self._log_root = log_root
        self._base_url = base_url
        self._policy_timeout_seconds = float(policy_timeout_seconds)
        self._transport_timeout_seconds = float(transport_timeout_seconds)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(mode={self._mode.value!r}, "
            f"log_root={str(self._log_root)!r}, base_url={self._base_url!r})"
        )

    def __call__(self) -> FlatSentinelTaskRuntime:
        timeout = Timeout(self._transport_timeout_seconds)
        client = OpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            max_retries=0,
            timeout=timeout,
            http_client=DefaultHttpxClient(timeout=timeout, trust_env=False),
        )
        transport = DirectOpenAIResponsesTransport(client)
        try:
            try:
                log_sink: FlatCallLog = ExternalFlatCallLog(self._log_root)
            except Exception:
                log_sink = NullFlatCallLog()
            sentinel = FlatPromptSentinel(
                mode=self._mode,
                policy=FlatHistoryPolicy(
                    transport, timeout_seconds=self._transport_timeout_seconds
                ),
                evidence_source=FlatCollectorEvidenceSource(),
                codec_registry=build_flat_codec_registry(),
                log_sink=log_sink,
                policy_timeout_seconds=self._policy_timeout_seconds,
            )
            return FlatSentinelTaskRuntime(
                sentinel=sentinel,
                close_transport=transport.close,
            )
        except Exception:
            transport.close()
            raise


def _reason(error: BaseException, default: str) -> str:
    return _bounded_reason(getattr(error, "code", None), default)


def _bounded_reason(value: object, default: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        return default
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
    if not value[0].isupper() or any(character not in allowed for character in value):
        return default
    return value


def _positive_seconds(value: object) -> None:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise TypeError("timeout must be an exact number")
    if not math.isfinite(cast(float | int, value)) or cast(float | int, value) <= 0:
        raise ValueError("timeout must be positive and finite")


__all__ = [
    "FLAT_SENTINEL_KILL_SWITCH",
    "GLOBAL_SENTINEL_KILL_SWITCH",
    "ExternalFlatCallLog",
    "FlatCallLog",
    "FlatLogicalCall",
    "FlatPromptSentinel",
    "FlatSentinelRunFactory",
    "FlatSentinelSwitch",
    "FlatSentinelTaskRuntime",
    "InjectedFlatSentinelFactory",
    "NullFlatCallLog",
    "global_sentinel_kill_switch_active",
    "set_global_sentinel_kill_switch",
]
