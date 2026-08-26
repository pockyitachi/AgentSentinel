"""Context-local, passive capture of environment execution evidence.

The hooks in this module observe only objects and bytes already present on the
``AndroidEnvClient`` call path.  They never issue an HTTP, screenshot, MCP, or
user-interaction request themselves and never replace a live action,
``Observation``, response, result, or exception object.
"""

from __future__ import annotations

import re
import time
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from mobile_world.runtime.audit.context import (
    AuditContext,
    bind_audit_scope,
    get_audit_context,
)
from mobile_world.runtime.audit.serializer import ArtifactSerializer, ArtifactSnapshot

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
_SIGNED_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "expires",
        "key",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-signature",
        "x-amz-security-token",
        "x-goog-credential",
        "x-goog-signature",
    }
)
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """Runner-facing immutable holder for one execution's recorded facts."""

    execution_result: Mapping[str, Any] | None
    duration_ns: int


def _runtime_trace_hook(
    scope: str,
    missing_artifacts: tuple[str, ...],
    *,
    fallback: Any = None,
) -> Any:
    """Make an evidence observer incapable of changing environment control flow."""

    def decorate(method: Any) -> Any:
        def wrapped(self: ExecutionEvidenceTrace, *args: Any, **kwargs: Any) -> Any:
            try:
                return method(self, *args, **kwargs)
            except Exception as error:
                try:
                    _handle_capture_error(
                        recorder=self.recorder,
                        context=get_audit_context(),
                        error=error,
                        scope=scope,
                        missing_artifacts=missing_artifacts,
                        secrets=self.known_secrets,
                    )
                except Exception:
                    pass
                return fallback

        return wrapped

    return decorate


@dataclass(slots=True)
class _ExecutionState:
    execution_kind: str
    started_ns: int
    result_kind: str
    request_endpoint: str | None = None
    request_body_snapshot_blob: Mapping[str, Any] | None = None
    http_status: int | None = None
    response_body_blob: Mapping[str, Any] | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    excluded_transport_fields: list[str] = field(default_factory=list)
    raw_tool_result_blob: Mapping[str, Any] | None = None
    agent_visible_tool_result: Any = None
    agent_visible_tool_result_snapshot_blob: Mapping[str, Any] | None = None
    agent_visible_tool_result_observed: bool = False
    ask_user_response: Any = None
    ask_user_response_snapshot_blob: Mapping[str, Any] | None = None
    exception: Mapping[str, Any] | None = None


class ExecutionEvidenceTrace:
    """Per-task trace of environment facts, scoped through ``AuditContext``.

    A single instance lives for one physical task attempt so screenshots from
    initialization and every returned post-state can be associated with their
    already-decoded source PNG bytes.  ``begin_execution``/``finish_execution``
    delimit one runner action at a time; failed attempts are finalized before
    a subsequent begin, so evidence is never overwritten by a retry.
    """

    def __init__(
        self,
        recorder: Any,
        *,
        known_secrets: Iterable[str] = (),
    ) -> None:
        if not getattr(recorder, "enabled", False):
            raise ValueError("ExecutionEvidenceTrace requires an enabled task recorder")
        blob_store = getattr(recorder, "blob_store", None)
        if blob_store is None:
            raise ValueError("enabled task recorder does not expose a blob_store")

        secrets: list[str] = []
        for secret in known_secrets:
            if not isinstance(secret, str) or not secret:
                raise TypeError("known_secrets must contain only non-empty strings")
            if secret not in secrets:
                secrets.append(secret)

        self.known_secrets = tuple(secrets)
        self.recorder = recorder
        self.serializer = ArtifactSerializer(
            blob_store,
            forbidden_values=self.known_secrets,
        )
        self._lock = RLock()
        self._active: _ExecutionState | None = None
        self._screenshot_sources: dict[int, tuple[weakref.ReferenceType[Any], bytes]] = {}

    @classmethod
    def from_context(
        cls,
        context: AuditContext | None = None,
        *,
        known_secrets: Iterable[str] | None = None,
    ) -> ExecutionEvidenceTrace | None:
        """Create an enabled task trace, or return before work when disabled."""

        try:
            active_context = context if context is not None else get_audit_context()
            if active_context is None or not bool(
                getattr(active_context.recorder, "enabled", False)
            ):
                return None
            secrets = active_context.known_secrets if known_secrets is None else known_secrets
        except Exception:
            return None

        try:
            return cls(active_context.recorder, known_secrets=secrets)
        except Exception as error:
            try:
                safe_secrets = tuple(
                    secret for secret in secrets if isinstance(secret, str) and secret
                )
            except Exception:
                safe_secrets = ()
            _handle_capture_error(
                recorder=active_context.recorder,
                context=active_context,
                error=error,
                scope="execution_evidence_setup",
                missing_artifacts=("execution_evidence_trace",),
                secrets=safe_secrets,
            )
            return None

    @_runtime_trace_hook(
        "execution_evidence.begin",
        ("transition.execution_result",),
    )
    def begin_execution(self, *, execution_kind: str) -> None:
        """Begin one runner-delimited action without inspecting the live action."""

        if not isinstance(execution_kind, str) or not execution_kind:
            self._capture(
                lambda: (_raise_value_error("execution_kind must be a non-empty string")),
                scope="execution_evidence.begin",
                missing_artifacts=("transition.execution_result",),
            )
            return

        with self._lock:
            if self._active is not None:
                self._capture(
                    lambda: (_raise_runtime_error("previous execution was not finalized")),
                    scope="execution_evidence.begin",
                    missing_artifacts=("transition.execution_result",),
                )
            self._active = _ExecutionState(
                execution_kind=execution_kind,
                started_ns=time.monotonic_ns(),
                result_kind=("mcp_tool" if execution_kind == "mcp" else "gui_transport"),
            )

    @_runtime_trace_hook(
        "execution_evidence.finalize",
        ("transition.execution_result",),
        fallback=ExecutionEvidence(execution_result=None, duration_ns=0),
    )
    def finish_execution(self, *, observation: Any) -> ExecutionEvidence:
        """Finalize a successful call using the exact returned observation."""

        with self._lock:
            state = self._take_active_or_error()
            if state is None:
                return ExecutionEvidence(execution_result=None, duration_ns=0)

            tool_call = self._capture(
                lambda: _observation_field(observation, "tool_call"),
                scope="execution_evidence.agent_visible_tool_result",
                missing_artifacts=("agent_visible_tool_result",),
            )
            ask_user_response = self._capture(
                lambda: _observation_field(observation, "ask_user_response"),
                scope="execution_evidence.ask_user_response",
                missing_artifacts=("ask_user_response",),
            )
            if tool_call is not None and not state.agent_visible_tool_result_observed:
                self._record_agent_visible_tool_result(
                    state,
                    tool_call,
                    scope="execution_evidence.agent_visible_tool_result",
                )
            if ask_user_response is not None:
                snapshot = self._snapshot_value(
                    ask_user_response,
                    scope="execution_evidence.ask_user_response",
                    missing_artifacts=("ask_user_response",),
                )
                if snapshot is not None:
                    state.ask_user_response = ask_user_response
                    state.ask_user_response_snapshot_blob = snapshot.snapshot_blob

            return self._finalize(state)

    @_runtime_trace_hook(
        "execution_evidence.finalize",
        ("transition.execution_result",),
        fallback=ExecutionEvidence(execution_result=None, duration_ns=0),
    )
    def fail_execution(self, exception: BaseException) -> ExecutionEvidence:
        """Finalize partial evidence while leaving the original exception untouched."""

        with self._lock:
            state = self._take_active_or_error()
            if state is None:
                return ExecutionEvidence(execution_result=None, duration_ns=0)
            state.exception = _exception_view(exception, self.known_secrets)
            return self._finalize(state)

    @_runtime_trace_hook(
        "screenshot_source",
        ("observation.screenshot.source_blob",),
    )
    def record_screenshot_source(self, image: Any, source_bytes: bytes) -> None:
        """Associate an existing PIL object with its already-decoded PNG bytes."""

        if not isinstance(source_bytes, bytes):
            self._capture(
                lambda: (_raise_type_error("screenshot source must be bytes")),
                scope="screenshot_source",
                missing_artifacts=("observation.screenshot.source_blob",),
            )
            return

        image_id = id(image)

        def discard(reference: weakref.ReferenceType[Any]) -> None:
            with self._lock:
                current = self._screenshot_sources.get(image_id)
                if current is not None and current[0] is reference:
                    self._screenshot_sources.pop(image_id, None)

        try:
            reference = weakref.ref(image, discard)
        except TypeError as error:
            _handle_capture_error(
                recorder=self.recorder,
                context=get_audit_context(),
                error=error,
                scope="screenshot_source",
                missing_artifacts=("observation.screenshot.source_blob",),
                secrets=self.known_secrets,
            )
            return
        with self._lock:
            self._screenshot_sources[image_id] = (reference, source_bytes)

    @_runtime_trace_hook(
        "screenshot_source_lookup",
        ("observation.screenshot.source_blob",),
    )
    def source_screenshot_bytes(self, image: Any) -> bytes | None:
        """Return exact associated PNG bytes only for the same live PIL object."""

        with self._lock:
            entry = self._screenshot_sources.get(id(image))
            if entry is None:
                return None
            reference, source_bytes = entry
            if reference() is not image:
                self._screenshot_sources.pop(id(image), None)
                return None
            return source_bytes

    @_runtime_trace_hook(
        "execution_evidence.gui_request",
        ("request_body_snapshot_blob",),
    )
    def record_gui_request(
        self,
        step_request: Mapping[str, Any],
        *,
        request_endpoint: str | None = None,
    ) -> None:
        """Snapshot the exact JSON object passed to ``requests.post(json=...)``."""

        with self._lock:
            state = self._active
            if state is None:
                return
            state.result_kind = "gui_transport"
            if request_endpoint is not None:
                endpoint = self._capture(
                    lambda: _sanitize_request_endpoint(
                        request_endpoint,
                        self.known_secrets,
                    ),
                    scope="execution_evidence.gui_request_endpoint",
                    missing_artifacts=("request_endpoint",),
                )
                if endpoint is not None:
                    state.request_endpoint = endpoint
            snapshot = self._snapshot_value(
                step_request,
                scope="execution_evidence.gui_request",
                missing_artifacts=("request_body_snapshot_blob",),
            )
            if snapshot is not None:
                state.request_body_snapshot_blob = snapshot.snapshot_blob

    @_runtime_trace_hook(
        "execution_evidence.gui_response",
        ("response_body_blob",),
    )
    def record_gui_response(self, response: Any) -> None:
        """Store exact returned bytes/status and a credential-clean header view."""

        with self._lock:
            state = self._active
            if state is None:
                return

            status_code = self._capture(
                lambda: getattr(response, "status_code", None),
                scope="execution_evidence.gui_response",
                missing_artifacts=("http_status",),
            )
            if isinstance(status_code, int) and not isinstance(status_code, bool):
                state.http_status = status_code
            else:
                self._capture(
                    lambda: (_raise_type_error("HTTP status_code must be an integer")),
                    scope="execution_evidence.gui_response",
                    missing_artifacts=("http_status",),
                )

            headers = self._capture(
                lambda: _sanitize_headers(
                    getattr(response, "headers", {}),
                    self.known_secrets,
                ),
                scope="execution_evidence.gui_response_headers",
                missing_artifacts=("response_headers",),
            )
            if headers is not None:
                state.response_headers, state.excluded_transport_fields = headers

            response_bytes = self._capture(
                lambda: _exact_response_bytes(response),
                scope="execution_evidence.gui_response_body",
                missing_artifacts=("response_body_blob",),
            )
            if response_bytes is None:
                return
            response_blob = self._capture(
                lambda: self._store_sensitive_checked_bytes(
                    response_bytes,
                    _response_media_type(state.response_headers),
                ),
                scope="execution_evidence.gui_response_body",
                missing_artifacts=("response_body_blob",),
            )
            if response_blob is not None:
                state.response_body_blob = response_blob

    @_runtime_trace_hook(
        "execution_evidence.mcp_request",
        ("request_body_snapshot_blob",),
    )
    def record_mcp_request(
        self,
        *,
        action_name: Any,
        action_arguments: Any,
    ) -> None:
        """Snapshot exact MCP application arguments before the client call."""

        with self._lock:
            state = self._active
            if state is None:
                return
            state.result_kind = "mcp_tool"
            request_snapshot = self._snapshot_value(
                {
                    "action_name": action_name,
                    "action_json": action_arguments,
                },
                scope="execution_evidence.mcp_request",
                missing_artifacts=("request_body_snapshot_blob",),
            )
            if request_snapshot is not None:
                state.request_body_snapshot_blob = request_snapshot.snapshot_blob

    @_runtime_trace_hook(
        "execution_evidence.raw_tool_result",
        ("raw_tool_result_blob",),
    )
    def record_mcp_raw_result(self, raw_result: Any) -> None:
        """Snapshot the returned MCP value before in-place post-processing."""

        with self._lock:
            state = self._active
            if state is None:
                return
            state.result_kind = "mcp_tool"
            result_snapshot = self._snapshot_value(
                raw_result,
                scope="execution_evidence.raw_tool_result",
                missing_artifacts=("raw_tool_result_blob",),
            )
            if result_snapshot is not None:
                state.raw_tool_result_blob = result_snapshot.snapshot_blob

    @_runtime_trace_hook(
        "execution_evidence.agent_visible_tool_result",
        ("agent_visible_tool_result",),
    )
    def record_mcp_visible_result(self, visible_result: Any) -> None:
        """Snapshot the post-processed value before any later screenshot failure."""

        with self._lock:
            state = self._active
            if state is None:
                return
            state.result_kind = "mcp_tool"
            self._record_agent_visible_tool_result(
                state,
                visible_result,
                scope="execution_evidence.agent_visible_tool_result",
            )

    def _record_agent_visible_tool_result(
        self,
        state: _ExecutionState,
        value: Any,
        *,
        scope: str,
    ) -> None:
        state.agent_visible_tool_result_observed = True
        snapshot = self._snapshot_value(
            value,
            scope=scope,
            missing_artifacts=("agent_visible_tool_result",),
        )
        if snapshot is not None:
            # Keep the exact runtime object on the collector-only side channel;
            # the snapshot remains authoritative for large or typed values.
            state.agent_visible_tool_result = value
            state.agent_visible_tool_result_snapshot_blob = snapshot.snapshot_blob

    def _snapshot_value(
        self,
        value: Any,
        *,
        scope: str,
        missing_artifacts: tuple[str, ...],
    ) -> ArtifactSnapshot | None:
        return self._capture(
            lambda: self._snapshot_secret_checked(value),
            scope=scope,
            missing_artifacts=missing_artifacts,
        )

    def _snapshot_secret_checked(self, value: Any) -> ArtifactSnapshot:
        _reject_sensitive_value(value, self.known_secrets)
        return self.serializer.snapshot(value, allow_repr_fallback=False)

    def _store_sensitive_checked_bytes(
        self,
        value: bytes,
        media_type: str,
    ) -> Mapping[str, Any]:
        _reject_sensitive_value(value, self.known_secrets)
        if _contains_signed_url_query(value):
            raise ValueError("response bytes contain a signed URL query and were not persisted")
        return self.serializer.blob_store.put_bytes(value, media_type)

    def _take_active_or_error(self) -> _ExecutionState | None:
        state = self._active
        if state is not None:
            self._active = None
            return state
        self._capture(
            lambda: (_raise_runtime_error("no active execution evidence scope")),
            scope="execution_evidence.finalize",
            missing_artifacts=("transition.execution_result",),
        )
        return None

    @staticmethod
    def _finalize(state: _ExecutionState) -> ExecutionEvidence:
        duration_ns = max(0, time.monotonic_ns() - state.started_ns)
        result = {
            "kind": state.result_kind,
            "request_endpoint": state.request_endpoint,
            "request_body_snapshot_blob": state.request_body_snapshot_blob,
            "http_status": state.http_status,
            "response_body_blob": state.response_body_blob,
            "response_headers": state.response_headers,
            "excluded_transport_fields": list(state.excluded_transport_fields),
            "raw_tool_result_blob": state.raw_tool_result_blob,
            "agent_visible_tool_result": state.agent_visible_tool_result,
            "agent_visible_tool_result_snapshot_blob": (
                state.agent_visible_tool_result_snapshot_blob
            ),
            "ask_user_response": state.ask_user_response,
            "ask_user_response_snapshot_blob": state.ask_user_response_snapshot_blob,
            "exception": state.exception,
        }
        return ExecutionEvidence(execution_result=result, duration_ns=duration_ns)

    def _capture(
        self,
        operation: Any,
        *,
        scope: str,
        missing_artifacts: tuple[str, ...],
    ) -> Any:
        try:
            return operation()
        except Exception as error:
            context = get_audit_context()
            _handle_capture_error(
                recorder=self.recorder,
                context=context,
                error=error,
                scope=scope,
                missing_artifacts=missing_artifacts,
                secrets=self.known_secrets,
            )
            return None


@contextmanager
def bind_execution_evidence_trace(
    trace: ExecutionEvidenceTrace | None,
) -> Iterator[ExecutionEvidenceTrace | None]:
    """Bind a task trace without deriving any context on the disabled path."""

    if trace is None:
        yield None
        return
    try:
        context = get_audit_context()
        active_trace = context.execution_evidence_trace if context is not None else None
    except Exception:
        context = None
        active_trace = None
    if context is None:
        # Binding is collector-only; a missing/malformed scope must not block
        # the already-established business execution path.
        yield None
        return
    if active_trace is trace:
        yield trace
        return
    with bind_audit_scope(execution_evidence_trace=trace):
        yield trace


def get_execution_evidence_trace() -> ExecutionEvidenceTrace | None:
    """Return the enabled context-local trace without touching disabled state."""

    try:
        context = get_audit_context()
        if context is None or not getattr(context.recorder, "enabled", False):
            return None
        trace = context.execution_evidence_trace
    except Exception:
        return None
    return trace if isinstance(trace, ExecutionEvidenceTrace) else None


def _invoke_trace_hook(method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Invoke a context-local observer with a final no-throw containment layer."""

    try:
        trace = get_execution_evidence_trace()
        if trace is None:
            return None
        method = getattr(trace, method_name)
        return method(*args, **kwargs)
    except Exception:
        return None


def record_screenshot_source(image: Any, source_bytes: bytes) -> None:
    """Client-hook helper that is a true no-op when collection is disabled."""

    _invoke_trace_hook("record_screenshot_source", image, source_bytes)


def record_gui_request(
    step_request: Mapping[str, Any],
    *,
    request_endpoint: str | None = None,
) -> None:
    """Client-hook helper that snapshots only an already-built request mapping."""

    _invoke_trace_hook(
        "record_gui_request",
        step_request,
        request_endpoint=request_endpoint,
    )


def record_gui_response(response: Any) -> None:
    """Client-hook helper that observes only an already-returned HTTP response."""

    _invoke_trace_hook("record_gui_response", response)


def record_mcp_request(
    *,
    action_name: Any,
    action_arguments: Any,
) -> None:
    """Client-hook helper called immediately before the existing MCP call."""

    _invoke_trace_hook(
        "record_mcp_request",
        action_name=action_name,
        action_arguments=action_arguments,
    )


def record_mcp_raw_result(raw_result: Any) -> None:
    """Client-hook helper called immediately after the existing MCP call."""

    _invoke_trace_hook("record_mcp_raw_result", raw_result)


def record_mcp_visible_result(visible_result: Any) -> None:
    """Client-hook helper called after existing MCP result post-processing."""

    _invoke_trace_hook("record_mcp_visible_result", visible_result)


def _handle_capture_error(
    *,
    recorder: Any,
    context: AuditContext | None,
    error: Exception,
    scope: str,
    missing_artifacts: tuple[str, ...],
    secrets: tuple[str, ...],
) -> None:
    """Best-effort error evidence; this function is itself strictly no-throw."""

    try:
        marker = getattr(recorder, "mark_incomplete", None)
        if callable(marker):
            marker(*missing_artifacts)
    except Exception:
        pass

    try:
        step_id = context.step_id if context is not None else None
    except Exception:
        step_id = None
    try:
        parent_event_id = context.parent_event_id if context is not None else None
    except Exception:
        parent_event_id = None
    try:
        payload = {
            "scope": scope,
            "related_event_id": None,
            "step_id": step_id,
            "exception": _exception_view(error, secrets),
            "missing_artifacts": list(missing_artifacts),
            "agent_execution_continued": True,
        }
        recorder.append_event(
            "collector_error",
            payload,
            caused_by_event_id=parent_event_id,
        )
    except Exception:
        pass


def _sanitize_headers(
    headers: Any,
    secrets: tuple[str, ...],
) -> tuple[dict[str, str], list[str]]:
    if not isinstance(headers, Mapping):
        raise TypeError("HTTP response headers must be a mapping")
    sanitized: dict[str, str] = {}
    excluded: list[str] = []
    for raw_name, raw_value in headers.items():
        name = str(raw_name)
        if name.casefold() in _CREDENTIAL_HEADER_NAMES:
            excluded.append(name)
            continue
        sanitized[name] = _sanitize_text(str(raw_value), secrets)
    return sanitized, excluded


def _sanitize_request_endpoint(value: str, secrets: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("GUI request endpoint must be a non-empty string")
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("GUI request endpoint must be an absolute HTTP(S) URL")
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        endpoint = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError) as error:
        raise ValueError("GUI request endpoint could not be sanitized") from error
    return _sanitize_text(endpoint, secrets)


def _sanitize_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return _URL_PATTERN.sub(_sanitize_url_match, value)


def _sanitize_url_match(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        query = "" if query_keys & _SIGNED_QUERY_KEYS else parsed.query
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return "[REDACTED_URL]"


def _exception_view(error: BaseException, secrets: tuple[str, ...]) -> dict[str, Any]:
    try:
        message = str(error)
    except Exception:
        message = "<exception message unavailable>"
    return {
        "class": f"{type(error).__module__}.{type(error).__qualname__}",
        "message": _sanitize_text(message, secrets),
        "details_blob": None,
    }


def _exact_response_bytes(response: Any) -> bytes:
    content = getattr(response, "content")
    if not isinstance(content, bytes):
        raise TypeError("HTTP response content must be bytes")
    return content


def _response_media_type(headers: Mapping[str, str]) -> str:
    for name, value in headers.items():
        if name.casefold() == "content-type" and value.strip():
            return value
    return "application/octet-stream"


def _reject_sensitive_value(value: Any, secrets: tuple[str, ...]) -> None:
    seen: set[int] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            if any(secret in node for secret in secrets):
                raise ValueError("configured secret was excluded from execution evidence")
            if _contains_signed_url_query(node.encode("utf-8")):
                raise ValueError("signed URL query was excluded from execution evidence")
            return
        if isinstance(node, (bytes, bytearray, memoryview)):
            raw = bytes(node)
            if any(secret.encode("utf-8") in raw for secret in secrets):
                raise ValueError("configured secret was excluded from execution evidence")
            if _contains_signed_url_query(raw):
                raise ValueError("signed URL query was excluded from execution evidence")
            return
        if isinstance(node, Mapping):
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)
            for key, child in node.items():
                visit(key)
                visit(child)
            return
        if isinstance(node, Sequence) and not isinstance(node, str):
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)
            for child in node:
                visit(child)

    visit(value)


def _contains_signed_url_query(value: bytes) -> bool:
    text = value.decode("utf-8", errors="ignore")
    for match in _URL_PATTERN.finditer(text):
        try:
            query_keys = {
                key.casefold()
                for key, _ in parse_qsl(
                    urlsplit(match.group(0)).query,
                    keep_blank_values=True,
                )
            }
        except (TypeError, ValueError):
            continue
        if query_keys & _SIGNED_QUERY_KEYS:
            return True
    return False


def _observation_field(observation: Any, name: str) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(name)
    return getattr(observation, name, None)


def _raise_value_error(message: str) -> None:
    raise ValueError(message)


def _raise_type_error(message: str) -> None:
    raise TypeError(message)


def _raise_runtime_error(message: str) -> None:
    raise RuntimeError(message)


__all__ = [
    "ExecutionEvidence",
    "ExecutionEvidenceTrace",
    "bind_execution_evidence_trace",
    "get_execution_evidence_trace",
    "record_gui_request",
    "record_gui_response",
    "record_mcp_raw_result",
    "record_mcp_request",
    "record_mcp_visible_result",
    "record_screenshot_source",
]
