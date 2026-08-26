"""Thin, passive runner-side capture helpers.

This module deliberately does not import or call the MobileWorld runner,
environment, or any model client.  It turns objects already present on the
runner call path into v1 task events.  Disabled instances return before ID
allocation, serialization, hashing, or recorder access.
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mobile_world.runtime.audit.config import CollectorMode
from mobile_world.runtime.audit.context import ModelCallTrace
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.secret_policy import is_placeholder_credential
from mobile_world.runtime.audit.serializer import (
    ArtifactSerializer,
    ArtifactSnapshot,
    SerializationError,
    canonical_json_bytes,
)

_INLINE_TEXT_BYTES = 64 * 1024
_DEFAULT_AGENT_OBSERVATION_KEYS = ("screenshot", "tool_call", "ask_user_response")
_REDACTED_SECRET = "[REDACTED_CONFIGURED_SECRET]"
_REDACTED_CREDENTIAL = "[REDACTED_CREDENTIAL]"
_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "authorization_header",
        "authorization_headers",
        "bearer_token",
        "cookie",
        "cookies",
        "password",
        "passwd",
        "access_token",
        "refresh_token",
        "set_cookie",
        "client_secret",
        "secret",
        "x_api_key",
    }
)
_CREDENTIAL_HEADER_KEYS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)
_SIGNED_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "credential",
        "key",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-security-token",
        "x-amz-signature",
        "x-goog-credential",
        "x-goog-signature",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|api[_-]?key|cookie|password|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret)\s*[:=]\s*([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")


def _runtime_hook(
    scope: str,
    missing: Sequence[str],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Make a public collector hook incapable of changing task control flow."""

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(self: RunnerTaskCapture, *args: Any, **kwargs: Any) -> Any:
            try:
                return method(self, *args, **kwargs)
            except Exception as error:
                try:
                    self._record_runtime_hook_failure(
                        scope=scope,
                        error=error,
                        missing=missing,
                    )
                except Exception:
                    pass
                return None

        return wrapped

    return decorate


@dataclass(frozen=True, slots=True)
class StepAuditRef:
    """Stable IDs allocated before one ``agent.predict()`` invocation."""

    step_id: str
    decision_id: str
    step_started_event_id: str | None
    step_index: int


@dataclass(frozen=True, slots=True)
class RunnerTaskMetadata:
    """Non-secret task identity supplied by the run-lifecycle integration."""

    run_id: str
    task_run_id: str
    task_index: int
    suite_family: str
    agent: Mapping[str, Any]
    environment: Mapping[str, Any]
    whole_task_attempt_index: int
    store_stream_chunks: bool = True

    def __post_init__(self) -> None:
        for name in ("task_index", "whole_task_attempt_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.store_stream_chunks, bool):
            raise TypeError("store_stream_chunks must be a bool")


@dataclass(frozen=True, slots=True)
class DecisionAuditRef:
    """Decision identity, untouched live action, and its immutable event snapshot."""

    decision_id: str
    event_id: str | None
    action: Any
    _action_snapshot_bytes: bytes | None = field(repr=False)

    @property
    def action_snapshot(self) -> Any:
        """Return a fresh copy; callers cannot mutate the authoritative snapshot."""

        return _json_from_bytes(self._action_snapshot_bytes)


@dataclass(frozen=True, slots=True)
class ExecutionAuditRef:
    """One attempted execution linked to the untouched action and its snapshot."""

    execution_id: str
    event_id: str | None
    execution_kind: str
    action: Any
    _action_snapshot_bytes: bytes | None = field(repr=False)

    @property
    def action_snapshot(self) -> Any:
        """Return a fresh copy; callers cannot mutate the authoritative snapshot."""

        return _json_from_bytes(self._action_snapshot_bytes)


class RunnerTaskCapture:
    """Capture one physical task attempt without changing its control flow.

    Collector exceptions are converted into best-effort ``collector_error``
    events and never escape a runtime hook.  ``collector_mode`` is retained as
    provenance/configuration metadata, but it never changes live task control
    flow.  Business/runtime exceptions are only serialized when explicitly
    passed to a method; this class never catches or transforms exceptions from
    the agent or environment.
    """

    def __init__(
        self,
        task_recorder: Any,
        *,
        configured_secrets: Iterable[str | bytes] = (),
    ) -> None:
        self._task_recorder = task_recorder
        self._configured_secrets = _normalize_secrets(configured_secrets)
        self.enabled = bool(getattr(task_recorder, "enabled", False))
        self._serializer: ArtifactSerializer | None = None
        self._collector_mode = CollectorMode.FAIL_OPEN_WITH_INCOMPLETE_MARKER
        if self.enabled:
            self._collector_mode = CollectorMode(task_recorder.collector_mode)
            self._serializer = ArtifactSerializer(
                task_recorder.blob_store,
                forbidden_values=self._configured_secrets,
            )

        self.capture_complete = True
        self.missing_artifacts: list[str] = []
        self.collector_error_event_ids: list[str] = []
        self._last_event_id: str | None = None
        self._task_started_event_id: str | None = None
        self._current_step: StepAuditRef | None = None
        self._current_decision: DecisionAuditRef | None = None
        self._current_execution: ExecutionAuditRef | None = None

    @property
    def collector_mode(self) -> CollectorMode:
        return self._collector_mode

    @property
    def task_recorder(self) -> Any:
        """Return the injected recorder for task-scoped ContextVar binding."""

        return self._task_recorder

    @property
    def configured_secrets(self) -> tuple[str, ...]:
        """Return in-memory secret values for sibling hooks; never persist them."""

        return self._configured_secrets

    @property
    def last_event_id(self) -> str | None:
        """Return the latest successfully emitted non-error task event ID."""

        return self._last_event_id

    @property
    def current_step(self) -> StepAuditRef | None:
        return self._current_step

    def mark_incomplete(self, *missing_artifacts: str) -> None:
        """Factually mark missing collector artifacts without emitting a label."""

        if not self.enabled:
            return
        try:
            self.capture_complete = False
            for artifact in missing_artifacts:
                if artifact and artifact not in self.missing_artifacts:
                    self.missing_artifacts.append(artifact)
        except Exception:
            # This state is collector-owned.  Even a malformed injected
            # recorder/artifact must not escape into the task control path.
            pass
        try:
            recorder_marker = getattr(self._task_recorder, "mark_incomplete", None)
            if callable(recorder_marker):
                recorder_marker(*missing_artifacts)
        except Exception:
            pass

    @_runtime_hook("task_started", ("task_started",))
    def start_task(
        self,
        *,
        task_name: str,
        task_goal: str | None,
        task_goal_status: str,
        task_index: int,
        suite_family: str,
        agent: Mapping[str, Any],
        environment: Mapping[str, Any],
        whole_task_attempt_index: int,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None

        event = self._emit(
            "task_started",
            lambda: {
                "task_name": self._safe_text(task_name),
                "task_goal": (
                    self._semantic_text_or_none(task_goal, "task_started.task_goal")
                    if task_goal is not None
                    else None
                ),
                "task_goal_status": task_goal_status,
                "task_index": task_index,
                "suite_family": self._safe_text(suite_family),
                "agent": self._safe_metadata_copy(agent, "task_started.agent"),
                "environment": self._safe_metadata_copy(environment, "task_started.environment"),
                "whole_task_attempt_index": whole_task_attempt_index,
            },
            caused_by_event_id=None,
            missing=("task_started",),
        )
        self._task_started_event_id = _event_id(event)
        return event

    @_runtime_hook("step_started", ("step_started.observation",))
    def start_step(
        self,
        *,
        step_index: int,
        observation: Any,
        agent_observation_keys: Sequence[str] = _DEFAULT_AGENT_OBSERVATION_KEYS,
        source_screenshot_bytes: bytes | None = None,
        caused_by_event_id: str | None = None,
    ) -> StepAuditRef | None:
        if not self.enabled:
            return None

        step_id = new_ulid()
        decision_id = new_ulid()
        reference = StepAuditRef(
            step_id=step_id,
            decision_id=decision_id,
            step_started_event_id=None,
            step_index=step_index,
        )
        # Bind the IDs before serialization so a best-effort collector_error
        # can still identify the physical decision step that lost evidence.
        self._current_step = reference
        self._current_decision = None
        self._current_execution = None
        parent = caused_by_event_id or self._last_event_id or self._task_started_event_id
        event = self._emit(
            "step_started",
            lambda: {
                "step_id": step_id,
                "step_index": step_index,
                "observation": self._observation(
                    observation,
                    source_screenshot_bytes=source_screenshot_bytes,
                ),
                "agent_observation_keys": list(agent_observation_keys),
            },
            caused_by_event_id=parent,
            missing=("step_started.observation",),
        )
        reference = StepAuditRef(
            step_id=step_id,
            decision_id=decision_id,
            step_started_event_id=_event_id(event),
            step_index=step_index,
        )
        self._current_step = reference
        return reference

    @_runtime_hook("agent_decision", ("agent_decision",))
    def record_decision(
        self,
        *,
        prediction: Any,
        action: Any,
        step: StepAuditRef | None = None,
        parse_outcome: str | None = None,
        parse_exception: BaseException | None = None,
        source_model_call_ids: Sequence[str] | None = None,
        model_call_trace: ModelCallTrace | None = None,
        caused_by_event_id: str | None = None,
    ) -> DecisionAuditRef | None:
        if not self.enabled:
            return None
        active_step = step or self._require_step()
        if source_model_call_ids is None:
            source_model_call_ids = model_call_trace.snapshot() if model_call_trace else ()
        source_ids = list(source_model_call_ids)
        outcome = parse_outcome
        if outcome is None:
            outcome = (
                "raised"
                if parse_exception is not None
                else ("returned_prediction_none" if prediction is None else "returned")
            )

        # Serialize once so later execution events reuse the exact same
        # immutable snapshot.  The live action remains only in the in-memory
        # reference and is never rewritten or substituted on the runner path.
        try:
            action_snapshot = self._parsed_action(action)
            execution_action_snapshot = (
                None if action_snapshot is None else action_snapshot["value"]
            )
        except Exception as error:
            return self._handle_decision_serialization_failure(
                error,
                active_step=active_step,
                action=action,
            )

        model_terminal_event_id = (
            model_call_trace.latest_terminal_event_id() if model_call_trace else None
        )
        parent = caused_by_event_id or model_terminal_event_id or active_step.step_started_event_id
        event = self._emit(
            "agent_decision",
            lambda: {
                "step_id": active_step.step_id,
                "decision_id": active_step.decision_id,
                **self._prediction_fields(prediction),
                "parsed_action": action_snapshot,
                "parse_outcome": outcome,
                "parse_exception": self._exception(parse_exception),
                "source_model_call_ids": source_ids,
            },
            caused_by_event_id=parent,
            missing=("agent_decision",),
        )
        reference = DecisionAuditRef(
            decision_id=active_step.decision_id,
            event_id=_event_id(event),
            action=action,
            _action_snapshot_bytes=canonical_json_bytes(execution_action_snapshot),
        )
        self._current_decision = reference
        return reference

    @_runtime_hook("action_execution_started", ("action_execution_started",))
    def execution_started(
        self,
        *,
        decision: DecisionAuditRef | None = None,
        execution_kind: str | None = None,
        caused_by_event_id: str | None = None,
    ) -> ExecutionAuditRef | None:
        if not self.enabled:
            return None
        active_decision = decision or self._require_decision()
        execution_id = new_ulid()
        action_for_kind = (
            active_decision.action_snapshot
            if active_decision.action_snapshot is not None
            else active_decision.action
        )
        kind = execution_kind or _execution_kind(action_for_kind)
        if active_decision.event_id is None:
            error = SerializationError("agent_decision event is unavailable")
            self.mark_incomplete("action_execution_started")
            self._emit_collector_error(
                scope="action_execution_started",
                error=error,
                missing=("action_execution_started",),
                related_event_id=self._current_step_event_id(),
            )
            reference = ExecutionAuditRef(
                execution_id=execution_id,
                event_id=None,
                execution_kind=kind,
                action=active_decision.action,
                _action_snapshot_bytes=active_decision._action_snapshot_bytes,
            )
            self._current_execution = reference
            return reference
        event = self._emit(
            "action_execution_started",
            lambda: {
                "step_id": self._require_step().step_id,
                "decision_id": active_decision.decision_id,
                "execution_id": execution_id,
                "execution_kind": kind,
                "action": active_decision.action_snapshot,
            },
            caused_by_event_id=caused_by_event_id or active_decision.event_id,
            missing=("action_execution_started",),
        )
        reference = ExecutionAuditRef(
            execution_id=execution_id,
            event_id=_event_id(event),
            execution_kind=kind,
            action=active_decision.action,
            _action_snapshot_bytes=active_decision._action_snapshot_bytes,
        )
        self._current_execution = reference
        return reference

    @_runtime_hook("transition_completed", ("transition_completed",))
    def transition_completed(
        self,
        *,
        post_observation: Any,
        execution: ExecutionAuditRef | None = None,
        execution_result: Mapping[str, Any] | None = None,
        duration_ns: int | None = None,
        source_screenshot_bytes: bytes | None = None,
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        active_step = self._require_step()
        active_execution = execution or self._require_execution()
        if active_execution.event_id is None:
            error = SerializationError("action_execution_started event is unavailable")
            self.mark_incomplete("transition_completed")
            self._emit_collector_error(
                scope="transition_completed",
                error=error,
                missing=("transition_completed",),
                related_event_id=self._current_decision_event_id(),
            )
            return None
        return self._emit(
            "transition_completed",
            lambda: {
                "step_id": active_step.step_id,
                "decision_id": self._require_decision().decision_id,
                "execution_id": active_execution.execution_id,
                "pre_observation_event_id": active_step.step_started_event_id,
                "action_execution_event_id": active_execution.event_id,
                "action": active_execution.action_snapshot,
                "execution_result": self._execution_result(
                    active_execution.execution_kind,
                    post_observation,
                    execution_result,
                ),
                "post_observation": self._observation(
                    post_observation,
                    source_screenshot_bytes=source_screenshot_bytes,
                ),
                "duration_ns": self._validated_duration(
                    duration_ns,
                    "transition_completed.duration_ns",
                ),
            },
            caused_by_event_id=caused_by_event_id or active_execution.event_id,
            missing=("transition_completed.post_observation",),
        )

    @_runtime_hook("transition_failed", ("transition_failed",))
    def transition_failed(
        self,
        *,
        exception: BaseException,
        execution: ExecutionAuditRef | None = None,
        available_execution_result: Any = None,
        post_observation: Any = None,
        duration_ns: int | None = None,
        source_screenshot_bytes: bytes | None = None,
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        active_step = self._require_step()
        active_execution = execution or self._require_execution()
        if active_execution.event_id is None:
            error = SerializationError("action_execution_started event is unavailable")
            self.mark_incomplete("transition_failed")
            self._emit_collector_error(
                scope="transition_failed",
                error=error,
                missing=("transition_failed",),
                related_event_id=self._current_decision_event_id(),
            )
            return None
        return self._emit(
            "transition_failed",
            lambda: {
                "step_id": active_step.step_id,
                "decision_id": self._require_decision().decision_id,
                "execution_id": active_execution.execution_id,
                "pre_observation_event_id": active_step.step_started_event_id,
                "action_execution_event_id": active_execution.event_id,
                "action": active_execution.action_snapshot,
                "available_execution_result": self._runtime_value(
                    self._sanitize_opaque_value(
                        available_execution_result,
                        "transition_failed.available_execution_result",
                    ),
                    "transition_failed.available_execution_result",
                ),
                "post_observation": (
                    self._observation(
                        post_observation,
                        source_screenshot_bytes=source_screenshot_bytes,
                    )
                    if post_observation is not None
                    else None
                ),
                "exception": self._exception(exception),
                "duration_ns": self._validated_duration(
                    duration_ns,
                    "transition_failed.duration_ns",
                ),
            },
            caused_by_event_id=caused_by_event_id or active_execution.event_id,
            missing=("transition_failed",),
        )

    @_runtime_hook("transition_not_executed", ("transition_not_executed",))
    def transition_not_executed(
        self,
        *,
        reason: str,
        decision: DecisionAuditRef | None = None,
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        active_step = self._require_step()
        active_decision = decision or self._require_decision()
        if active_decision.event_id is None:
            error = SerializationError("agent_decision event is unavailable")
            self.mark_incomplete("transition_not_executed")
            self._emit_collector_error(
                scope="transition_not_executed",
                error=error,
                missing=("transition_not_executed",),
                related_event_id=active_step.step_started_event_id,
            )
            return None
        return self._emit(
            "transition_not_executed",
            lambda: {
                "step_id": active_step.step_id,
                "decision_id": active_decision.decision_id,
                "pre_observation_event_id": active_step.step_started_event_id,
                "action": active_decision.action_snapshot,
                "reason": reason,
                "post_observation": None,
            },
            caused_by_event_id=caused_by_event_id or active_decision.event_id,
            missing=("transition_not_executed",),
        )

    @_runtime_hook("task_ended", ("task_ended",))
    def end_task(
        self,
        *,
        runtime_status: str,
        termination_source: str,
        final_step_index: int,
        termination_exception: BaseException | None = None,
        score: float | None = None,
        reason: str | None = None,
        evaluation_exception: BaseException | None = None,
        teardown_attempted: bool = False,
        teardown_result: Any = None,
        teardown_exception: BaseException | None = None,
        token_usage: Mapping[str, Any] | None = None,
        caused_by_event_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        self._merge_recorder_capture_state()
        parent = caused_by_event_id or self._last_event_id
        try:
            payload = {
                "runtime_status": runtime_status,
                "termination": {
                    "source": self._safe_text(termination_source),
                    "step_index": final_step_index,
                    "exception": self._exception(termination_exception),
                },
                "environment_evaluation": {
                    "score": score,
                    "reason": (
                        self._semantic_text_or_none(
                            reason,
                            "task_ended.environment_evaluation.reason",
                        )
                        if reason is not None
                        else None
                    ),
                    "exception": self._exception(evaluation_exception),
                },
                "teardown": self._teardown(
                    attempted=teardown_attempted,
                    result=teardown_result,
                    exception=teardown_exception,
                ),
                "token_usage": self._plain_json_copy(token_usage or {}, "task_ended.token_usage"),
                "capture_complete": self.capture_complete,
                "missing_artifacts": list(self.missing_artifacts),
                "collector_error_event_ids": list(self.collector_error_event_ids),
            }
        except Exception as error:
            return self._emit_minimal_task_end(
                error=error,
                runtime_status=runtime_status,
                termination_source=termination_source,
                final_step_index=final_step_index,
                termination_exception=termination_exception,
                parent_event_id=parent,
            )
        return self._emit(
            "task_ended",
            lambda: payload,
            caused_by_event_id=parent,
            missing=("task_ended",),
        )

    def _emit_minimal_task_end(
        self,
        *,
        error: Exception,
        runtime_status: str,
        termination_source: str,
        final_step_index: int,
        termination_exception: BaseException | None,
        parent_event_id: str | None,
    ) -> dict[str, Any] | None:
        """Best-effort contract terminal after full task-result serialization fails."""

        self.mark_incomplete("task_ended.full_payload")
        self._emit_collector_error(
            scope="task_ended",
            error=error,
            missing=("task_ended.full_payload",),
            related_event_id=parent_event_id,
        )
        self._merge_recorder_capture_state()
        minimal_payload = {
            "runtime_status": (
                runtime_status
                if runtime_status in {"completed", "aborted", "crashed"}
                else "crashed"
            ),
            "termination": {
                "source": self._safe_text(termination_source),
                "step_index": (
                    final_step_index
                    if isinstance(final_step_index, int) and not isinstance(final_step_index, bool)
                    else 0
                ),
                "exception": self._exception(termination_exception),
            },
            "environment_evaluation": {
                "score": None,
                "reason": None,
                "exception": None,
            },
            "teardown": {
                "returned": False,
                "result_snapshot_blob": None,
                "exception": None,
            },
            "token_usage": {},
            "capture_complete": False,
            "missing_artifacts": list(self.missing_artifacts),
            "collector_error_event_ids": list(self.collector_error_event_ids),
        }
        try:
            event = self._task_recorder.append_event(
                "task_ended",
                minimal_payload,
                caused_by_event_id=parent_event_id,
            )
        except Exception:
            event = None
        if isinstance(event, dict):
            self._last_event_id = _event_id(event) or self._last_event_id
        return event

    def _emit(
        self,
        event_type: str,
        payload_factory: Callable[[], Mapping[str, Any]],
        *,
        caused_by_event_id: str | None,
        missing: Sequence[str],
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            payload = payload_factory()
            event = self._task_recorder.append_event(
                event_type,
                payload,
                caused_by_event_id=caused_by_event_id,
            )
        except Exception as error:
            self.mark_incomplete(*missing)
            self._emit_collector_error(
                scope=event_type,
                error=error,
                missing=missing,
                related_event_id=caused_by_event_id,
            )
            return None

        event_id = _event_id(event)
        if event_id is not None:
            self._last_event_id = event_id
        return event

    def _emit_collector_error(
        self,
        *,
        scope: str,
        error: Exception,
        missing: Sequence[str],
        related_event_id: str | None,
    ) -> None:
        try:
            event = self._task_recorder.append_event(
                "collector_error",
                {
                    "scope": scope,
                    "related_event_id": related_event_id,
                    "step_id": self._current_step.step_id if self._current_step else None,
                    "exception": self._exception(error),
                    "missing_artifacts": list(missing),
                    "agent_execution_continued": True,
                },
                caused_by_event_id=related_event_id,
            )
        except Exception:
            # Recorder failure can make even the emergency task event
            # unavailable.  The in-memory incomplete state remains available
            # for run finalization; a separate emergency file is Phase 4 work.
            return
        event_id = _event_id(event)
        if event_id and event_id not in self.collector_error_event_ids:
            self.collector_error_event_ids.append(event_id)

    def _record_runtime_hook_failure(
        self,
        *,
        scope: str,
        error: Exception,
        missing: Sequence[str],
    ) -> None:
        """Retain best-effort diagnostics without ever rethrowing a hook fault."""

        self.mark_incomplete(*missing)
        # task_started must remain the first task-stream event.  If that hook
        # itself fails, retain only in-memory state for lifecycle finalization.
        if scope == "task_started" and self._task_started_event_id is None:
            return
        self._emit_collector_error(
            scope=scope,
            error=error,
            missing=missing,
            related_event_id=self._last_event_id,
        )

    def _observation(
        self,
        observation: Any,
        *,
        source_screenshot_bytes: bytes | None,
    ) -> dict[str, Any]:
        serializer = self._require_serializer()
        screenshot = _observation_field(observation, "screenshot")
        return {
            "screenshot": serializer.serialize_observation_image(
                screenshot,
                source_bytes=source_screenshot_bytes,
            ),
            "accessibility_tree": self._runtime_value(
                self._sanitize_semantic_value(
                    _observation_field(observation, "accessibility_tree", default=None),
                    "observation.accessibility_tree",
                ),
                "observation.accessibility_tree",
            ),
            "tool_call": self._runtime_value(
                self._sanitize_semantic_value(
                    _observation_field(observation, "tool_call", default=None),
                    "observation.tool_call",
                ),
                "observation.tool_call",
            ),
            "ask_user_response": self._runtime_value(
                self._sanitize_semantic_value(
                    _observation_field(observation, "ask_user_response", default=None),
                    "observation.ask_user_response",
                ),
                "observation.ask_user_response",
            ),
        }

    def _prediction_fields(self, prediction: Any) -> dict[str, Any]:
        if prediction is None or isinstance(prediction, (bool, int, float)):
            if isinstance(prediction, float) and not math.isfinite(prediction):
                raise SerializationError("prediction contains a non-finite float")
            return {"prediction_raw": prediction, "prediction_snapshot_blob": None}
        if isinstance(prediction, str) and len(prediction.encode("utf-8")) <= _INLINE_TEXT_BYTES:
            return {
                "prediction_raw": self._semantic_text_or_none(
                    prediction,
                    "agent_decision.prediction_raw",
                ),
                "prediction_snapshot_blob": None,
            }

        safe_prediction = self._sanitize_semantic_value(
            prediction,
            "agent_decision.prediction_raw",
        )
        snapshot = self._require_serializer().snapshot(safe_prediction)
        self._accept_snapshot_fidelity(snapshot.serialization_fidelity, "agent_decision.prediction")
        return {
            "prediction_raw": self._artifact_placeholder(snapshot),
            "prediction_snapshot_blob": snapshot.snapshot_blob,
        }

    def _parsed_action(self, action: Any) -> dict[str, Any] | None:
        if action is None:
            return None
        serializer_name = "plain_mapping"
        if hasattr(action, "model_dump") and callable(action.model_dump):
            try:
                value = action.model_dump(mode="json", exclude_none=False)
                serializer_name = "pydantic model_dump(mode=json, exclude_none=false)"
            except TypeError:
                value = action.model_dump(exclude_none=False)
                serializer_name = "pydantic model_dump(exclude_none=false)"
        elif isinstance(action, Mapping):
            value = action
        else:
            snapshot = self._require_serializer().snapshot(action)
            self._accept_snapshot_fidelity(snapshot.serialization_fidelity, "agent_decision.action")
            value = self._require_serializer().rehydrate(snapshot.artifact_graph)
            serializer_name = "mobileworld typed artifact graph"

        sanitized_value, changed = _sanitize_semantic_json_value(
            value,
            secrets=self._configured_secrets,
        )
        if changed:
            self.mark_incomplete("agent_decision.parsed_action.value.credential_redaction")
        value = sanitized_value

        return {
            "class": _qualified_class_name(action),
            "serializer": serializer_name,
            "serializer_version": _pydantic_version() if "pydantic" in serializer_name else None,
            "value": self._plain_json_copy(value, "agent_decision.parsed_action.value"),
        }

    def _execution_result(
        self,
        execution_kind: str,
        post_observation: Any,
        provided: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        result_kind = {
            "gui": "gui_transport",
            "mcp": "mcp_tool",
            "ask_user": "gui_transport",
            "answer": "gui_transport",
        }.get(execution_kind, execution_kind)
        if provided is None:
            missing = ("transition_completed.execution_result.transport_evidence",)
            self._mark_missing_required(*missing)
            return {
                "kind": result_kind,
                "request_endpoint": None,
                "request_body_snapshot_blob": None,
                "http_status": None,
                "response_body_blob": None,
                "response_headers": {},
                "excluded_transport_fields": [],
                "raw_tool_result_blob": None,
                "agent_visible_tool_result": self._runtime_value(
                    self._sanitize_semantic_value(
                        _observation_field(post_observation, "tool_call", default=None),
                        "transition_completed.execution_result.agent_visible_tool_result",
                    ),
                    "transition_completed.execution_result.agent_visible_tool_result",
                ),
                "agent_visible_tool_result_snapshot_blob": None,
                "ask_user_response": self._runtime_value(
                    self._sanitize_semantic_value(
                        _observation_field(post_observation, "ask_user_response", default=None),
                        "transition_completed.execution_result.ask_user_response",
                    ),
                    "transition_completed.execution_result.ask_user_response",
                ),
                "ask_user_response_snapshot_blob": None,
                "exception": None,
            }

        missing_fields: list[str] = []
        for key in (
            "kind",
            "request_body_snapshot_blob",
            "http_status",
            "response_body_blob",
            "response_headers",
            "raw_tool_result_blob",
            "agent_visible_tool_result",
            "ask_user_response",
            "exception",
        ):
            if key not in provided:
                missing_fields.append(f"transition_completed.execution_result.{key}")
        provided_kind = provided.get("kind")
        if provided_kind != result_kind:
            missing_fields.append("transition_completed.execution_result.kind")

        result: dict[str, Any] = {
            "kind": result_kind,
            "request_endpoint": self._safe_request_endpoint(
                provided.get("request_endpoint"),
                missing_fields,
                required=execution_kind != "mcp",
            ),
            "request_body_snapshot_blob": self._validated_blob_reference(
                provided.get("request_body_snapshot_blob"),
                "transition_completed.execution_result.request_body_snapshot_blob",
                missing_fields,
            ),
            "http_status": provided.get("http_status"),
            "response_body_blob": self._validated_blob_reference(
                provided.get("response_body_blob"),
                "transition_completed.execution_result.response_body_blob",
                missing_fields,
            ),
            "response_headers": self._safe_response_headers(
                provided.get("response_headers", {}),
                missing_fields,
            ),
            "excluded_transport_fields": self._safe_metadata_copy(
                provided.get("excluded_transport_fields", []),
                "transition_completed.execution_result.excluded_transport_fields",
            ),
            "raw_tool_result_blob": self._validated_blob_reference(
                provided.get("raw_tool_result_blob"),
                "transition_completed.execution_result.raw_tool_result_blob",
                missing_fields,
                required=execution_kind == "mcp",
            ),
            "agent_visible_tool_result": self._runtime_value(
                self._sanitize_semantic_value(
                    provided.get("agent_visible_tool_result"),
                    "transition_completed.execution_result.agent_visible_tool_result",
                ),
                "transition_completed.execution_result.agent_visible_tool_result",
            ),
            "agent_visible_tool_result_snapshot_blob": self._validated_blob_reference(
                provided.get("agent_visible_tool_result_snapshot_blob"),
                ("transition_completed.execution_result.agent_visible_tool_result_snapshot_blob"),
                missing_fields,
            ),
            "ask_user_response": self._runtime_value(
                self._sanitize_semantic_value(
                    provided.get("ask_user_response"),
                    "transition_completed.execution_result.ask_user_response",
                ),
                "transition_completed.execution_result.ask_user_response",
            ),
            "ask_user_response_snapshot_blob": self._validated_blob_reference(
                provided.get("ask_user_response_snapshot_blob"),
                "transition_completed.execution_result.ask_user_response_snapshot_blob",
                missing_fields,
            ),
            "exception": self._safe_execution_exception(provided.get("exception")),
        }
        if execution_kind != "mcp":
            for field_name in (
                "request_body_snapshot_blob",
                "response_body_blob",
            ):
                if result[field_name] is None:
                    missing_fields.append(f"transition_completed.execution_result.{field_name}")
            status = result["http_status"]
            if isinstance(status, bool) or not isinstance(status, int):
                missing_fields.append("transition_completed.execution_result.http_status")
                result["http_status"] = None
        if execution_kind == "ask_user" and result["ask_user_response"] is None:
            missing_fields.append("transition_completed.execution_result.ask_user_response")
        self._mark_missing_required(*missing_fields)
        return result

    def _safe_request_endpoint(
        self,
        value: Any,
        missing_fields: list[str],
        *,
        required: bool,
    ) -> str | None:
        field = "transition_completed.execution_result.request_endpoint"
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            missing_fields.append(field)
            return None
        try:
            parsed = urlsplit(value)
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                raise ValueError
            hostname = parsed.hostname
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            netloc = hostname
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        except (TypeError, ValueError):
            missing_fields.append(field)
            return None

    def _teardown(
        self,
        *,
        attempted: bool,
        result: Any,
        exception: BaseException | None,
    ) -> dict[str, Any]:
        if result is None:
            snapshot_blob = None
        else:
            safe_result = self._sanitize_semantic_value(result, "task_ended.teardown.result")
            snapshot = self._require_serializer().snapshot(safe_result)
            self._accept_snapshot_fidelity(snapshot.serialization_fidelity, "task_ended.teardown")
            snapshot_blob = snapshot.snapshot_blob
        return {
            "returned": attempted and exception is None,
            "result_snapshot_blob": snapshot_blob,
            "exception": self._exception(exception),
        }

    def _runtime_value(
        self,
        value: Any,
        field: str,
    ) -> Any:
        if _is_plain_json(value):
            encoded = canonical_json_bytes(value)
            if len(encoded) <= _INLINE_TEXT_BYTES:
                try:
                    return json.loads(encoded)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    raise SerializationError(f"could not copy {field}: {error}") from error
        snapshot = self._require_serializer().snapshot(value)
        self._accept_snapshot_fidelity(snapshot.serialization_fidelity, field)
        return self._artifact_placeholder(snapshot)

    @staticmethod
    def _artifact_placeholder(snapshot: ArtifactSnapshot) -> dict[str, Any]:
        return {
            "$artifact_snapshot": {
                "kind": "mobileworld_typed_artifact_graph",
                "snapshot_blob": snapshot.snapshot_blob,
                "canonical_sha256": snapshot.canonical_sha256,
                "canonical_byte_length": snapshot.canonical_byte_length,
                "serialization_fidelity": snapshot.serialization_fidelity,
            }
        }

    @staticmethod
    def _plain_json_copy(value: Any, field: str) -> Any:
        if not _is_plain_json(value):
            raise SerializationError(f"{field} must be plain finite JSON data")
        try:
            return json.loads(canonical_json_bytes(value))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise SerializationError(f"could not copy {field}: {error}") from error

    def _safe_metadata_copy(self, value: Any, field: str) -> Any:
        sanitized, _ = _sanitize_json_value(
            value,
            secrets=self._configured_secrets,
            drop_credential_keys=True,
        )
        return self._plain_json_copy(sanitized, field)

    def _sanitize_opaque_value(self, value: Any, field: str = "runtime_artifact") -> Any:
        if _is_plain_json(value):
            sanitized, changed = _sanitize_json_value(
                value,
                secrets=self._configured_secrets,
                drop_credential_keys=False,
            )
            if changed:
                self.mark_incomplete(f"{field}.credential_redaction")
            return sanitized

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(mode="json", exclude_none=False)
            except TypeError:
                dumped = model_dump(exclude_none=False)
            sanitized, changed = _sanitize_json_value(
                dumped,
                secrets=self._configured_secrets,
                drop_credential_keys=False,
            )
            if changed:
                self.mark_incomplete(f"{field}.credential_redaction")
                return {
                    "$sanitized_object": {
                        "class": _qualified_class_name(value),
                        "value": sanitized,
                    }
                }
        return value

    def _sanitize_semantic_value(self, value: Any, field: str) -> Any:
        """Exclude only exact configured secrets from model-visible evidence.

        Credential-shaped task, model, action, tool, and user text is part of
        the authoritative semantic trace.  Broad credential heuristics belong
        only on transport/configuration/error surfaces.
        """

        if _is_plain_json(value):
            sanitized, changed = _sanitize_semantic_json_value(
                value,
                secrets=self._configured_secrets,
            )
            if changed:
                self.mark_incomplete(f"{field}.credential_redaction")
            return sanitized

        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                dumped = model_dump(mode="json", exclude_none=False)
            except TypeError:
                dumped = model_dump(exclude_none=False)
            sanitized, changed = _sanitize_semantic_json_value(
                dumped,
                secrets=self._configured_secrets,
            )
            if changed:
                self.mark_incomplete(f"{field}.credential_redaction")
                return {
                    "$sanitized_object": {
                        "class": _qualified_class_name(value),
                        "value": sanitized,
                    }
                }
        return value

    def _safe_text(self, value: str) -> str:
        return _sanitize_text(value, self._configured_secrets)

    def _semantic_text_or_none(self, value: str, field: str) -> str | None:
        """Exclude configured secrets without interpreting semantic text."""

        sanitized = _sanitize_configured_secrets(value, self._configured_secrets)
        if sanitized == value:
            return value
        self._mark_missing_required(f"{field}.configured_secret_excluded")
        return None

    def _safe_response_headers(
        self,
        value: Any,
        missing_fields: list[str],
    ) -> dict[str, Any]:
        field_name = "transition_completed.execution_result.response_headers"
        if not isinstance(value, Mapping):
            missing_fields.append(field_name)
            return {}
        headers: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                missing_fields.append(field_name)
                continue
            if key.casefold() in _CREDENTIAL_HEADER_KEYS:
                continue
            sanitized, _ = _sanitize_json_value(
                item,
                secrets=self._configured_secrets,
                drop_credential_keys=True,
            )
            if not _is_plain_json(sanitized):
                missing_fields.append(field_name)
                continue
            headers[key] = sanitized
        return self._plain_json_copy(headers, field_name)

    def _safe_execution_exception(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, BaseException):
            return self._exception(value)
        sanitized, _ = _sanitize_json_value(
            value,
            secrets=self._configured_secrets,
            drop_credential_keys=True,
        )
        if _is_plain_json(sanitized):
            return self._plain_json_copy(
                sanitized,
                "transition_completed.execution_result.exception",
            )
        return {
            "class": _qualified_class_name(value),
            "message": self._safe_text(str(value)),
            "details_blob": None,
        }

    def _validated_blob_reference(
        self,
        value: Any,
        field_name: str,
        missing_fields: list[str],
        *,
        required: bool = False,
    ) -> dict[str, Any] | None:
        if value is None:
            if required:
                missing_fields.append(field_name)
            return None
        if not isinstance(value, dict):
            missing_fields.append(field_name)
            return None
        try:
            reference = self._plain_json_copy(value, field_name)
            self._require_serializer().blob_store.verify(reference)
        except Exception:
            missing_fields.append(field_name)
            return None
        return reference

    def _mark_missing_required(self, *fields: str) -> None:
        missing = tuple(dict.fromkeys(field for field in fields if field))
        if not missing:
            return
        self.mark_incomplete(*missing)

    def _validated_duration(self, value: int | None, field_name: str) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        self._mark_missing_required(field_name)
        return None

    def _accept_snapshot_fidelity(self, fidelity: str, artifact: str) -> None:
        if fidelity != "lossless":
            self.mark_incomplete(artifact)

    def _exception(self, exception: BaseException | None) -> dict[str, Any] | None:
        if exception is None:
            return None
        return {
            "class": _qualified_class_name(exception),
            "message": self._safe_text(str(exception)),
            "details_blob": None,
        }

    def _handle_decision_serialization_failure(
        self,
        error: Exception,
        *,
        active_step: StepAuditRef,
        action: Any,
    ) -> DecisionAuditRef | None:
        self.mark_incomplete("agent_decision.parsed_action")
        self._emit_collector_error(
            scope="agent_decision",
            error=error,
            missing=("agent_decision.parsed_action",),
            related_event_id=active_step.step_started_event_id,
        )
        reference = DecisionAuditRef(
            decision_id=active_step.decision_id,
            event_id=None,
            action=action,
            _action_snapshot_bytes=None,
        )
        self._current_decision = reference
        return reference

    def _require_serializer(self) -> ArtifactSerializer:
        if self._serializer is None:
            raise RuntimeError("audit serializer is unavailable on a disabled capture")
        return self._serializer

    def _require_step(self) -> StepAuditRef:
        if self._current_step is None:
            raise RuntimeError("no audit step has been started")
        return self._current_step

    def _require_decision(self) -> DecisionAuditRef:
        if self._current_decision is None:
            raise RuntimeError("no audit decision has been recorded")
        return self._current_decision

    def _require_execution(self) -> ExecutionAuditRef:
        if self._current_execution is None:
            raise RuntimeError("no audit execution has been started")
        return self._current_execution

    def _current_step_event_id(self) -> str | None:
        return self._current_step.step_started_event_id if self._current_step else None

    def _current_decision_event_id(self) -> str | None:
        return self._current_decision.event_id if self._current_decision else None

    def _merge_recorder_capture_state(self) -> None:
        if getattr(self._task_recorder, "capture_complete", True) is False:
            self.capture_complete = False
        for artifact in getattr(self._task_recorder, "missing_artifacts", ()):
            if isinstance(artifact, str) and artifact not in self.missing_artifacts:
                self.missing_artifacts.append(artifact)
        for event_id in getattr(self._task_recorder, "collector_error_event_ids", ()):
            if isinstance(event_id, str) and event_id not in self.collector_error_event_ids:
                self.collector_error_event_ids.append(event_id)


def _normalize_secrets(values: Iterable[str | bytes]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for value in values:
        if isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        elif isinstance(value, str):
            text = value
        else:
            raise TypeError("configured secrets must be strings or bytes")
        if text and not is_placeholder_credential(text):
            normalized.add(text)
    return tuple(sorted(normalized, key=len, reverse=True))


def _sanitize_json_value(
    value: Any,
    *,
    secrets: Sequence[str],
    drop_credential_keys: bool,
) -> tuple[Any, bool]:
    if isinstance(value, Mapping):
        sanitized_mapping: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationError("collector metadata keys must be strings")
            normalized_key = _normalize_key(key)
            if drop_credential_keys and normalized_key in _CREDENTIAL_KEYS:
                changed = True
                continue
            sanitized_item, item_changed = _sanitize_json_value(
                item,
                secrets=secrets,
                drop_credential_keys=drop_credential_keys,
            )
            sanitized_mapping[key] = sanitized_item
            changed = changed or item_changed
        return sanitized_mapping, changed
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized_items = []
        changed = not isinstance(value, list)
        for item in value:
            sanitized_item, item_changed = _sanitize_json_value(
                item,
                secrets=secrets,
                drop_credential_keys=drop_credential_keys,
            )
            sanitized_items.append(sanitized_item)
            changed = changed or item_changed
        return sanitized_items, changed
    if isinstance(value, str):
        sanitized = _sanitize_text(value, secrets)
        return sanitized, sanitized != value
    return value, False


def _sanitize_semantic_json_value(
    value: Any,
    *,
    secrets: Sequence[str],
) -> tuple[Any, bool]:
    """Copy semantic JSON while redacting only configured secret values."""

    if isinstance(value, Mapping):
        sanitized_mapping: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            if not isinstance(key, str):
                raise SerializationError("semantic artifact keys must be strings")
            sanitized_key = _sanitize_configured_secrets(key, secrets)
            sanitized_item, item_changed = _sanitize_semantic_json_value(
                item,
                secrets=secrets,
            )
            sanitized_mapping[sanitized_key] = sanitized_item
            changed = changed or sanitized_key != key or item_changed
        return sanitized_mapping, changed
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sanitized_items = []
        changed = False
        for item in value:
            sanitized_item, item_changed = _sanitize_semantic_json_value(
                item,
                secrets=secrets,
            )
            sanitized_items.append(sanitized_item)
            changed = changed or item_changed
        return sanitized_items, changed
    if isinstance(value, str):
        sanitized = _sanitize_configured_secrets(value, secrets)
        return sanitized, sanitized != value
    return value, False


def _sanitize_configured_secrets(value: str, secrets: Sequence[str]) -> str:
    sanitized = value
    for secret in secrets:
        sanitized = sanitized.replace(secret, _REDACTED_SECRET)
    return sanitized


def _sanitize_text(value: str, secrets: Sequence[str]) -> str:
    sanitized = _sanitize_configured_secrets(value, secrets)
    sanitized = _URL_PATTERN.sub(lambda match: _sanitize_url(match.group(0)), sanitized)
    sanitized = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={_REDACTED_CREDENTIAL}",
        sanitized,
    )
    return _BEARER_PATTERN.sub(f"Bearer {_REDACTED_CREDENTIAL}", sanitized)


def _sanitize_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.query:
        return value
    changed = False
    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in _SIGNED_QUERY_KEYS or key.casefold().startswith("x-amz-"):
            query.append((key, _REDACTED_CREDENTIAL))
            changed = True
        else:
            query.append((key, item))
    if not changed:
        return value
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _json_from_bytes(value: bytes | None) -> Any:
    return None if value is None else json.loads(value)


def _event_id(event: Any) -> str | None:
    if isinstance(event, Mapping):
        event_id = event.get("event_id")
        return event_id if isinstance(event_id, str) else None
    return None


_MISSING = object()


def _observation_field(observation: Any, field: str, *, default: Any = _MISSING) -> Any:
    if isinstance(observation, Mapping):
        if field in observation:
            return observation[field]
    elif hasattr(observation, field):
        return getattr(observation, field)
    if default is not _MISSING:
        return default
    raise SerializationError(f"observation is missing required field: {field}")


def _execution_kind(action: Any) -> str:
    action_type = (
        action.get("action_type")
        if isinstance(action, Mapping)
        else getattr(action, "action_type", None)
    )
    return {
        "mcp": "mcp",
        "ask_user": "ask_user",
        "answer": "answer",
    }.get(action_type, "gui")


def _is_plain_json(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if type(value) is list:
        return all(_is_plain_json(item) for item in value)
    if type(value) is dict:
        return all(isinstance(key, str) and _is_plain_json(item) for key, item in value.items())
    return False


def _qualified_class_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _pydantic_version() -> str | None:
    try:
        return importlib.metadata.version("pydantic")
    except importlib.metadata.PackageNotFoundError:
        return None


def elapsed_ns(start_ns: int) -> int:
    """Return a non-negative monotonic duration for runner call sites."""

    return max(0, monotonic_ns() - start_ns)


__all__ = [
    "DecisionAuditRef",
    "ExecutionAuditRef",
    "RunnerTaskMetadata",
    "RunnerTaskCapture",
    "StepAuditRef",
    "elapsed_ns",
]
