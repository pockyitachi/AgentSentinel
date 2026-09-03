"""Transparent, event-sourced capture at provider SDK call boundaries.

This module never invokes a provider itself.  It snapshots application-layer
arguments immediately before an invocation and records the object or chunks
that the existing call path observes.  All artifactization operates on a
private serialized graph and never mutates live request/response objects.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
from collections.abc import Callable, Iterator, Mapping
from typing import Any, TypeVar
from urllib.parse import urlsplit

from mobile_world.runtime.audit.context import AuditContext, get_audit_context
from mobile_world.runtime.audit.ids import new_ulid
from mobile_world.runtime.audit.secret_policy import is_placeholder_credential
from mobile_world.runtime.audit.serializer import (
    ArtifactSerializer,
    ArtifactSnapshot,
)
from mobile_world.runtime.audit.serializer import (
    canonical_json_bytes as audit_canonical_json_bytes,
)

_T = TypeVar("_T")
_UNSET = object()
_CREDENTIAL_HEADER_NAMES = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-amz-security-token",
    }
)
_CREDENTIAL_ARGUMENT_NAMES = frozenset(
    {
        "api-key",
        "api_key",
        "authorization",
        "cookie",
        "cookies",
    }
)


def begin_model_call(
    *,
    call_role: str,
    component: str,
    client: Any,
) -> ModelCallAudit | None:
    """Return an enabled logical-call recorder for the current context.

    The default-off path returns before constructing a serializer or touching
    the filesystem.  ``None`` also represents a fail-open collector setup
    error; the original SDK path must continue unchanged in that case.
    """

    try:
        context = get_audit_context()
        if context is None or not getattr(context.recorder, "enabled", False):
            return None
        recorder = context.recorder
        secrets = context.known_secrets
    except Exception:
        # Even resolving collector-local state must not affect a provider call.
        return None

    try:
        secrets = tuple(dict.fromkeys((*secrets, *_client_secret_values(client))))
        if context.task_run_id is None or context.step_id is None:
            raise ValueError("enabled model capture requires task_run_id and step_id")
        blob_store = getattr(recorder, "blob_store", None)
        if blob_store is None:
            raise ValueError("enabled task recorder does not expose a blob_store")
        serializer = ArtifactSerializer(blob_store, forbidden_values=secrets)
        return ModelCallAudit(
            context=context,
            recorder=recorder,
            serializer=serializer,
            call_role=call_role,
            component=component,
            endpoint=_endpoint_view(client),
            client_configuration=_client_configuration(client),
            secrets=secrets,
        )
    except Exception as error:
        try:
            _handle_capture_error(
                recorder=recorder,
                context=context,
                error=error,
                scope="model_request",
                missing_artifacts=("sdk_arguments_snapshot_blob",),
                secrets=secrets,
            )
        except Exception:
            pass
        return None


class ModelCallAudit:
    """Capture all application-visible SDK attempts for one logical call."""

    def __init__(
        self,
        *,
        context: AuditContext,
        recorder: Any,
        serializer: ArtifactSerializer,
        call_role: str,
        component: str,
        endpoint: dict[str, Any],
        client_configuration: dict[str, Any],
        secrets: tuple[str, ...],
    ) -> None:
        self.context = context
        self.recorder = recorder
        self.serializer = serializer
        self.call_role = call_role
        self.component = component
        self.endpoint = endpoint
        self.client_configuration = client_configuration
        self.secrets = secrets
        self.model_call_id = context.model_call_id or new_ulid()
        self.retry_group_id = context.retry_group_id or self.model_call_id
        self.adapter_attempt_index = context.adapter_attempt_index
        self.adapter_retry_planned = context.adapter_retry_planned
        self._attempt_index = 0
        self._traced = False
        self._last_attempt_terminal_event_id: str | None = None

    def begin_attempt(
        self,
        sdk_arguments: Mapping[str, Any],
        *,
        stream: bool,
    ) -> ModelAttemptAudit:
        """Record one exact physical SDK invocation before it occurs."""

        self._attempt_index += 1
        # Construct the inert attempt before any fallible collector work.  If
        # preparation fails, callers still receive the normal no-op surface and
        # proceed to the provider with their original arguments.
        attempt = ModelAttemptAudit(
            call=self,
            request_id="collector-unavailable",
            attempt_index=self._attempt_index,
            stream=stream,
        )
        try:
            self._capture(
                lambda: self._prepare_attempt(attempt, sdk_arguments, stream=stream),
                scope="model_request",
                missing_artifacts=("model_request", "sdk_arguments_snapshot_blob"),
            )
        except Exception:
            pass
        return attempt

    def _prepare_attempt(
        self,
        attempt: ModelAttemptAudit,
        sdk_arguments: Mapping[str, Any],
        *,
        stream: bool,
    ) -> None:
        """Perform all fallible request preparation inside one fail-open guard."""

        if not self._traced:
            self._capture(
                lambda: self.context.record_model_call(self.model_call_id),
                scope="model_request",
                missing_artifacts=("model_call_trace",),
            )
            self._traced = True

        attempt.request_id = new_ulid()
        audit_arguments, excluded_transport_fields = _sdk_arguments_for_audit(sdk_arguments)
        snapshot = self._capture(
            lambda: self.serializer.snapshot_sdk_arguments(audit_arguments),
            scope="model_request",
            missing_artifacts=("sdk_arguments_snapshot_blob",),
        )
        if snapshot is None:
            return
        unavailable_images = [
            image for image in snapshot.request_images if image.get("content_blob") is None
        ]
        if unavailable_images:
            self._capture(
                lambda: self.recorder.mark_incomplete("model_request.request_image_content"),
                scope="model_request",
                missing_artifacts=("model_request.request_image_content",),
            )

        payload = {
            **attempt.correlation_payload,
            "call_role": self.call_role,
            "component": self.component,
            "sdk": {
                "package": "openai",
                "version": _package_version("openai"),
                "method": "chat.completions.create",
                "client_configuration": self.client_configuration,
                "transparent_retry_attempts_observable": False,
            },
            "endpoint": self.endpoint,
            "stream": stream,
            "sdk_arguments_snapshot_blob": snapshot.snapshot_blob,
            "request_view": snapshot.request_view,
            "request_images": list(snapshot.request_images),
            "excluded_transport_fields": excluded_transport_fields,
        }
        event = self._capture(
            lambda: self.recorder.append_event(
                "model_request",
                payload,
                caused_by_event_id=(
                    self._last_attempt_terminal_event_id
                    or self.context.latest_model_terminal_event_id()
                    or self.context.parent_event_id
                ),
            ),
            scope="model_request",
            missing_artifacts=("model_request",),
        )
        if isinstance(event, Mapping) and isinstance(event.get("event_id"), str):
            attempt.request_event_id = event["event_id"]
            attempt.request_event_sha256 = hashlib.sha256(
                audit_canonical_json_bytes(event)
            ).hexdigest()
            attempt.request_snapshot_blob = dict(snapshot.snapshot_blob)

    def _capture(
        self,
        operation: Callable[[], _T],
        *,
        scope: str,
        missing_artifacts: tuple[str, ...],
        related_event_id: str | None = None,
    ) -> _T | None:
        try:
            return operation()
        except Exception as error:
            try:
                _handle_capture_error(
                    recorder=self.recorder,
                    context=self.context,
                    error=error,
                    scope=scope,
                    missing_artifacts=missing_artifacts,
                    related_event_id=related_event_id,
                    secrets=self.secrets,
                )
            except Exception:
                pass
            return None


class ModelAttemptAudit:
    """Capture one request and its exactly-one provider terminal outcome."""

    def __init__(
        self,
        *,
        call: ModelCallAudit,
        request_id: str,
        attempt_index: int,
        stream: bool,
    ) -> None:
        self.call = call
        self.request_id = request_id
        self.attempt_index = attempt_index
        self.stream = stream
        self.request_event_id: str | None = None
        self.request_event_sha256: str | None = None
        self.request_snapshot_blob: dict[str, Any] | None = None
        self.terminal_event_id: str | None = None
        self.terminal_event_sha256: str | None = None
        self.terminal_event_type: str | None = None
        self.terminal_snapshot_blob: dict[str, Any] | None = None
        self._terminal = False
        self._observed_chunk_count = 0
        self._chunk_event_ids: list[str] = []
        self._chunk_views: list[Any] = []

    @property
    def request_recorded(self) -> bool:
        """Whether the authoritative request event was persisted."""

        return self.request_event_id is not None

    @property
    def terminal(self) -> bool:
        """Whether the provider attempt already has an in-memory terminal state."""

        return self._terminal

    @property
    def adapter_retry_planned(self) -> bool:
        """Whether the enclosing adapter has declared a later retry."""

        return self.call.adapter_retry_planned

    @property
    def request_artifact_locator(self) -> dict[str, Any] | None:
        """Return the exact existing Collector request event/blob locator."""

        if (
            self.request_event_id is None
            or self.request_event_sha256 is None
            or self.request_snapshot_blob is None
        ):
            return None
        return {
            "run_id": self.call.context.run_id,
            "task_run_id": self.call.context.task_run_id,
            "event_type": "model_request",
            "event_id": self.request_event_id,
            "event_sha256": self.request_event_sha256,
            "snapshot_blob": dict(self.request_snapshot_blob),
        }

    @property
    def terminal_artifact_locator(self) -> dict[str, Any] | None:
        """Return the exact existing Collector response/failure event locator."""

        if self.terminal_event_id is None or self.terminal_event_sha256 is None:
            return None
        return {
            "run_id": self.call.context.run_id,
            "task_run_id": self.call.context.task_run_id,
            "event_type": self.terminal_event_type,
            "event_id": self.terminal_event_id,
            "event_sha256": self.terminal_event_sha256,
            "snapshot_blob": (
                None if self.terminal_snapshot_blob is None else dict(self.terminal_snapshot_blob)
            ),
        }

    @property
    def correlation_payload(self) -> dict[str, Any]:
        """Return the contract correlation fields shared by attempt events."""

        return {
            "step_id": self.call.context.step_id,
            "model_call_id": self.call.model_call_id,
            "retry_group_id": self.call.retry_group_id,
            "adapter_attempt_index": self.call.adapter_attempt_index,
            "request_id": self.request_id,
            "attempt_index": self.attempt_index,
        }

    def record_nonstream_response(self, response: Any, returned_value: Any = _UNSET) -> None:
        """Close a non-stream attempt with its raw and wrapper-returned values."""

        try:
            self.call._capture(
                lambda: self._record_nonstream_response(response, returned_value),
                scope="model_response",
                missing_artifacts=("model_response",),
                related_event_id=self.request_event_id,
            )
        except Exception:
            pass

    def _record_nonstream_response(self, response: Any, returned_value: Any) -> None:
        """Implementation kept behind the public no-throw capture boundary."""

        if self._terminal:
            return
        self._terminal = True
        if not self.request_recorded:
            return

        raw_snapshot = self._snapshot(
            response,
            scope="model_response",
            missing_artifacts=("raw_response.snapshot_blob",),
            allow_repr_fallback=False,
        )
        if raw_snapshot is None:
            return

        returned_snapshot = None
        if returned_value is not _UNSET:
            returned_snapshot = self._snapshot(
                returned_value,
                scope="model_response",
                missing_artifacts=("returned_value_snapshot_blob",),
                allow_repr_fallback=False,
            )

        normalized_response = self.call._capture(
            lambda: normalize_nonstream_response(raw_snapshot.request_view),
            scope="model_response",
            missing_artifacts=("normalized_response",),
            related_event_id=self.request_event_id,
        )

        payload = {
            **self.correlation_payload,
            "response_mode": "non_stream",
            "raw_response": {
                "kind": "single_response",
                "snapshot_blob": raw_snapshot.snapshot_blob,
                "chunk_event_ids": [],
                "chunk_count": 0,
            },
            "raw_response_view": raw_snapshot.request_view,
            "normalized_response": normalized_response,
            "returned_value_snapshot_blob": (
                returned_snapshot.snapshot_blob if returned_snapshot is not None else None
            ),
            "stream_state": None,
        }
        self._append_terminal("model_response", payload)

    def record_failure(
        self,
        error: Exception,
        *,
        failure_phase: str,
        retry_planned: bool,
        raw_response: Any = _UNSET,
    ) -> None:
        """Close an attempt with a provider/iteration/wrapper failure."""

        try:
            self.call._capture(
                lambda: self._record_failure(
                    error,
                    failure_phase=failure_phase,
                    retry_planned=retry_planned,
                    raw_response=raw_response,
                ),
                scope="model_attempt_failed",
                missing_artifacts=("model_attempt_failed",),
                related_event_id=self.request_event_id,
            )
        except Exception:
            pass

    def _record_failure(
        self,
        error: Exception,
        *,
        failure_phase: str,
        retry_planned: bool,
        raw_response: Any,
    ) -> None:
        """Implementation kept behind the public no-throw capture boundary."""

        if self._terminal:
            return
        self._terminal = True
        if not self.request_recorded:
            return

        raw_snapshot = None
        if raw_response is not _UNSET:
            raw_snapshot = self._snapshot(
                raw_response,
                scope="model_response",
                missing_artifacts=("raw_response.snapshot_blob",),
                allow_repr_fallback=False,
            )

        normalized_partial = None
        if self._chunk_views:
            normalized_partial = self.call._capture(
                lambda: normalize_stream_response(self._chunk_views),
                scope="model_attempt_failed",
                missing_artifacts=("normalized_partial_response",),
                related_event_id=self.request_event_id,
            )

        payload = {
            **self.correlation_payload,
            "failure_phase": failure_phase,
            "exception": _exception_view(error, self.call.secrets),
            "partial_chunk_event_ids": list(self._chunk_event_ids),
            "normalized_partial_response": normalized_partial,
            "retry_planned": retry_planned,
        }
        if raw_snapshot is not None:
            payload["raw_response_snapshot_blob"] = raw_snapshot.snapshot_blob
            payload["raw_response_view"] = raw_snapshot.request_view
        self._append_terminal("model_attempt_failed", payload)

    def record_stream_chunk(self, chunk: Any) -> None:
        """Persist a chunk before the exact same object is yielded to the consumer."""

        try:
            self.call._capture(
                lambda: self._record_stream_chunk(chunk),
                scope="model_stream_chunk",
                missing_artifacts=("model_stream_chunk",),
                related_event_id=self.request_event_id,
            )
        except Exception:
            pass

    def _record_stream_chunk(self, chunk: Any) -> None:
        """Implementation kept behind the public no-throw capture boundary."""

        if self._terminal:
            return
        chunk_index = self._observed_chunk_count
        self._observed_chunk_count += 1
        if not self.request_recorded:
            return
        if not self.call.context.store_stream_chunks:
            self.call._capture(
                self._mark_stream_chunks_omitted,
                scope="model_stream_chunk",
                missing_artifacts=("model_stream_chunks",),
                related_event_id=self.request_event_id,
            )
            return

        snapshot = self._snapshot(
            chunk,
            scope="model_stream_chunk",
            missing_artifacts=("raw_chunk_snapshot_blob",),
            allow_repr_fallback=False,
        )
        if snapshot is None:
            return
        self._chunk_views.append(snapshot.request_view)
        payload = {
            **self.correlation_payload,
            "chunk_index": chunk_index,
            "raw_chunk_snapshot_blob": snapshot.snapshot_blob,
            "chunk_view": snapshot.request_view,
        }
        event = self.call._capture(
            lambda: self.call.recorder.append_event(
                "model_stream_chunk",
                payload,
                caused_by_event_id=self._last_causal_event_id,
            ),
            scope="model_stream_chunk",
            missing_artifacts=("model_stream_chunk",),
            related_event_id=self.request_event_id,
        )
        if isinstance(event, Mapping) and isinstance(event.get("event_id"), str):
            self._chunk_event_ids.append(event["event_id"])

    def complete_stream(self, *, stream_state: str) -> None:
        """Close an exhausted or deliberately abandoned stream exactly once."""

        try:
            self.call._capture(
                lambda: self._complete_stream(stream_state=stream_state),
                scope="model_response",
                missing_artifacts=("model_response",),
                related_event_id=self.request_event_id,
            )
        except Exception:
            pass

    def _complete_stream(self, *, stream_state: str) -> None:
        """Implementation kept behind the public no-throw capture boundary."""

        if self._terminal:
            return
        self._terminal = True
        if not self.request_recorded:
            return

        if self.call.context.store_stream_chunks:
            normalized_response = self.call._capture(
                lambda: normalize_stream_response(self._chunk_views),
                scope="model_response",
                missing_artifacts=("normalized_response",),
                related_event_id=self.request_event_id,
            )
            raw_response_kind = "stream_chunks"
        else:
            normalized_response = None
            raw_response_kind = "stream_chunks_omitted_by_config"

        payload = {
            **self.correlation_payload,
            "response_mode": "stream",
            "raw_response": {
                "kind": raw_response_kind,
                "snapshot_blob": None,
                "chunk_event_ids": list(self._chunk_event_ids),
                "chunk_count": len(self._chunk_event_ids),
                "observed_chunk_count": self._observed_chunk_count,
            },
            "raw_response_view": None,
            "normalized_response": normalized_response,
            "returned_value_snapshot_blob": None,
            "stream_state": stream_state,
        }
        self._append_terminal("model_response", payload)

    def _mark_stream_chunks_omitted(self) -> None:
        marker = getattr(self.call.recorder, "mark_incomplete", None)
        if not callable(marker):
            raise RuntimeError("enabled task recorder does not expose mark_incomplete")
        marker("model_stream_chunks")

    def wrap_stream(self, stream: Any, usage_callback: Callable[[Any], None]) -> Iterator[Any]:
        """Yield the original chunks lazily while recording their observed order."""

        try:
            iterator = iter(stream)
        except Exception as error:
            try:
                self.record_failure(
                    error,
                    failure_phase="stream_iteration",
                    retry_planned=self.adapter_retry_planned,
                )
            except Exception:
                pass
            raise
        final_usage_chunk = None
        while True:
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            except Exception as error:
                try:
                    self.record_failure(
                        error,
                        failure_phase="stream_iteration",
                        retry_planned=self.adapter_retry_planned,
                    )
                except Exception:
                    pass
                raise

            try:
                if hasattr(chunk, "usage") and chunk.usage is not None:
                    final_usage_chunk = chunk
            except Exception as error:
                try:
                    self.record_failure(
                        error,
                        failure_phase="response_serialization",
                        retry_planned=self.adapter_retry_planned,
                    )
                except Exception:
                    pass
                raise

            try:
                self.record_stream_chunk(chunk)
            except Exception:
                pass
            try:
                yield chunk
            except GeneratorExit:
                try:
                    self.complete_stream(stream_state="consumer_abandoned")
                except Exception:
                    pass
                raise
            except BaseException:
                try:
                    self.complete_stream(stream_state="consumer_abandoned")
                except Exception:
                    pass
                raise

        try:
            if final_usage_chunk is not None:
                usage_callback(final_usage_chunk)
        except Exception as error:
            try:
                self.record_failure(
                    error,
                    failure_phase="response_serialization",
                    retry_planned=self.adapter_retry_planned,
                )
            except Exception:
                pass
            raise
        try:
            self.complete_stream(stream_state="complete")
        except Exception:
            pass

    @property
    def _last_causal_event_id(self) -> str | None:
        if self._chunk_event_ids:
            return self._chunk_event_ids[-1]
        return self.request_event_id

    def _snapshot(
        self,
        value: Any,
        *,
        scope: str,
        missing_artifacts: tuple[str, ...],
        allow_repr_fallback: bool,
    ) -> ArtifactSnapshot | None:
        return self.call._capture(
            lambda: self.call.serializer.snapshot(
                value,
                allow_repr_fallback=allow_repr_fallback,
            ),
            scope=scope,
            missing_artifacts=missing_artifacts,
            related_event_id=self.request_event_id,
        )

    def _append_terminal(self, event_type: str, payload: Mapping[str, Any]) -> None:
        event = self.call._capture(
            lambda: self.call.recorder.append_event(
                event_type,
                payload,
                caused_by_event_id=self._last_causal_event_id,
            ),
            scope=event_type,
            missing_artifacts=(event_type,),
            related_event_id=self.request_event_id,
        )
        if isinstance(event, Mapping) and isinstance(event.get("event_id"), str):
            self.terminal_event_id = event["event_id"]
            self.terminal_event_type = event_type
            self.terminal_event_sha256 = hashlib.sha256(
                audit_canonical_json_bytes(event)
            ).hexdigest()
            raw_response = payload.get("raw_response")
            if isinstance(raw_response, Mapping) and isinstance(
                raw_response.get("snapshot_blob"), Mapping
            ):
                self.terminal_snapshot_blob = dict(raw_response["snapshot_blob"])
            elif isinstance(payload.get("raw_response_snapshot_blob"), Mapping):
                self.terminal_snapshot_blob = dict(payload["raw_response_snapshot_blob"])
            self.call._last_attempt_terminal_event_id = event["event_id"]
            trace = self.call.context.model_call_trace
            if trace is not None:
                self.call._capture(
                    lambda: trace.record_terminal(
                        self.call.model_call_id,
                        event["event_id"],
                    ),
                    scope=event_type,
                    missing_artifacts=("model_call_trace.terminal_event_id",),
                    related_event_id=event["event_id"],
                )


def normalize_nonstream_response(response_view: Any) -> dict[str, Any]:
    """Build a provider-neutral convenience view without replacing raw data."""

    response = _mapping(response_view)
    choices = []
    raw_choices = response.get("choices")
    if isinstance(raw_choices, list):
        for position, raw_choice in enumerate(raw_choices):
            choice = _mapping(raw_choice)
            message = _mapping(choice.get("message"))
            tool_calls = message.get("tool_calls")
            choices.append(
                {
                    "index": choice.get("index", position),
                    "content": message.get("content"),
                    "reasoning_content": message.get("reasoning_content"),
                    "tool_calls": tool_calls if isinstance(tool_calls, list) else [],
                    "finish_reason": choice.get("finish_reason"),
                }
            )
    return {
        "response_id": response.get("id"),
        "choices": choices,
        "usage": _normalize_usage(response.get("usage")),
    }


def normalize_stream_response(chunk_views: list[Any]) -> dict[str, Any]:
    """Assemble a convenience response solely from already-observed chunks."""

    response_id = None
    usage = None
    choices: dict[Any, dict[str, Any]] = {}
    for chunk_view in chunk_views:
        chunk = _mapping(chunk_view)
        if response_id is None:
            response_id = chunk.get("id")
        if chunk.get("usage") is not None:
            usage = _normalize_usage(chunk.get("usage"))
        raw_choices = chunk.get("choices")
        if not isinstance(raw_choices, list):
            continue
        for position, raw_choice in enumerate(raw_choices):
            choice = _mapping(raw_choice)
            index = choice.get("index", position)
            state = choices.setdefault(
                index,
                {
                    "index": index,
                    "content_parts": [],
                    "reasoning_parts": [],
                    "tool_calls": [],
                    "finish_reason": None,
                },
            )
            delta = _mapping(choice.get("delta"))
            content = delta.get("content")
            if isinstance(content, str):
                state["content_parts"].append(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                state["reasoning_parts"].append(reasoning)
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                state["tool_calls"].extend(tool_calls)
            if choice.get("finish_reason") is not None:
                state["finish_reason"] = choice.get("finish_reason")

    normalized_choices = []
    for state in choices.values():
        content_parts = state.pop("content_parts")
        reasoning_parts = state.pop("reasoning_parts")
        state["content"] = "".join(content_parts) if content_parts else None
        state["reasoning_content"] = "".join(reasoning_parts) if reasoning_parts else None
        normalized_choices.append(state)
    return {
        "response_id": response_id,
        "choices": normalized_choices,
        "usage": usage,
    }


def _normalize_usage(value: Any) -> dict[str, Any] | None:
    usage = _mapping(value)
    if not usage:
        return None
    details = _mapping(usage.get("prompt_tokens_details"))
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cached_tokens": details.get("cached_tokens", 0) or 0,
        "provider_usage": dict(usage),
    }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _handle_capture_error(
    *,
    recorder: Any,
    context: AuditContext,
    error: Exception,
    scope: str,
    missing_artifacts: tuple[str, ...],
    secrets: tuple[str, ...],
    related_event_id: str | None = None,
) -> None:
    """Best-effort factual marker that can never escape to the live agent."""

    try:
        mark_incomplete = getattr(recorder, "mark_incomplete", None)
        if callable(mark_incomplete):
            mark_incomplete(*missing_artifacts)
    except Exception:
        # In-memory completeness tracking is itself collector state.  A broken
        # tracker must never mask the application/provider path.
        pass

    try:
        payload = {
            "scope": scope,
            "related_event_id": related_event_id,
            "step_id": context.step_id,
            "exception": _exception_view(error, secrets),
            "missing_artifacts": list(missing_artifacts),
            "agent_execution_continued": True,
        }
        recorder.append_event(
            "collector_error",
            payload,
            caused_by_event_id=(related_event_id or context.parent_event_id),
        )
    except Exception:
        # The emergency marker can share the same failed writer.  Never turn a
        # fail-open audit failure into an agent/provider failure.
        pass


def _exception_view(error: Exception, secrets: tuple[str, ...]) -> dict[str, Any]:
    try:
        message = str(error)
    except Exception:
        message = "<exception message unavailable>"
    return {
        "class": f"{type(error).__module__}.{type(error).__qualname__}",
        "message": _scrub_message(message, secrets),
        "details_blob": None,
    }


def _scrub_message(message: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        message = message.replace(secret, "[REDACTED]")
    return message


def _client_secret_values(client: Any) -> tuple[str, ...]:
    values = []
    api_key = getattr(client, "api_key", None)
    if isinstance(api_key, str) and api_key and not is_placeholder_credential(api_key):
        values.append(api_key)
    return tuple(values)


def _sdk_arguments_for_audit(
    arguments: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Return a private SDK-argument view with transport credentials omitted.

    The provider still receives the original mapping unchanged.  Only fields
    that configure HTTP authentication/cookies are omitted here; model-visible
    messages, tools, and provider parameters retain their exact values.
    """

    captured = dict(arguments)
    excluded = ["api_key", "authorization_headers", "cookies"]
    for raw_key in list(captured):
        normalized = raw_key.casefold().replace("_", "-")
        if normalized in _CREDENTIAL_ARGUMENT_NAMES:
            captured.pop(raw_key)
            excluded.append(raw_key)
            continue
        if normalized not in {"extra-headers", "headers", "extra-query"}:
            continue
        container = captured[raw_key]
        if not isinstance(container, Mapping):
            captured.pop(raw_key)
            excluded.append(raw_key)
            continue
        clean_container: dict[Any, Any] = {}
        for field_name, field_value in container.items():
            normalized_field = (
                field_name.casefold().replace("_", "-") if isinstance(field_name, str) else ""
            )
            if normalized_field in _CREDENTIAL_HEADER_NAMES:
                excluded.append(f"{raw_key}.{field_name}")
                continue
            clean_container[field_name] = field_value
        captured[raw_key] = clean_container
    return captured, list(dict.fromkeys(excluded))


def _endpoint_view(client: Any) -> dict[str, Any]:
    raw_base_url = getattr(client, "base_url", None)
    if raw_base_url is None:
        raw_base_url = getattr(client, "_base_url", None)
    parsed = urlsplit(str(raw_base_url or ""))
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    authority = hostname
    if parsed.port is not None:
        authority = f"{authority}:{parsed.port}"
    origin = f"{parsed.scheme}://{authority}" if parsed.scheme and authority else None
    base_path = parsed.path.rstrip("/")
    return {
        "origin": origin,
        "path": f"{base_path}/chat/completions" or "/chat/completions",
        "query_removed": True,
    }


def _client_configuration(client: Any) -> dict[str, Any]:
    max_retries = getattr(client, "max_retries", None)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        max_retries = None

    timeout = getattr(client, "timeout", None)
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        timeout_view: Any = {"all_seconds": float(timeout)}
    else:
        timeout_view = {}
        for field in ("connect", "read", "write", "pool"):
            field_value = getattr(timeout, field, None)
            if isinstance(field_value, (int, float)) and not isinstance(field_value, bool):
                timeout_view[field] = float(field_value)
        if not timeout_view:
            timeout_view = None
    return {
        "max_retries": max_retries,
        "timeout": timeout_view,
    }


def _package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


__all__ = [
    "ModelAttemptAudit",
    "ModelCallAudit",
    "begin_model_call",
    "normalize_nonstream_response",
    "normalize_stream_response",
]
