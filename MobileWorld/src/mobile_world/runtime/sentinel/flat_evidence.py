"""One-pass Collector projection for the flat Runtime Sentinel.

This module reads the already-recorded task prefix exactly once for an actor
decision.  It produces two things from that same prefix: a small JSON packet
for the history classifier and local execution facts.  It deliberately has
no rubric, policy receipt, runtime authority, or R2.3 dependency.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, cast

from PIL import Image

try:  # pragma: no cover - MobileWorld's supported runtime is POSIX.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from mobile_world.runtime.audit.blob_store import BlobStore
from mobile_world.runtime.audit.context import AuditContext, get_audit_context
from mobile_world.runtime.audit.recorder import TaskRecorder
from mobile_world.runtime.audit.schemas import validate_event_envelope
from mobile_world.runtime.sentinel.flat_codec import FlatHistory, JsonPath
from mobile_world.runtime.sentinel.flat_contracts import JsonValue, strict_json_bytes
from mobile_world.runtime.sentinel.flat_execution_state import (
    CollectorPixelIdentity,
    FlatExecutionState,
    build_flat_execution_state,
)
from mobile_world.runtime.sentinel.flat_policy import FLAT_POLICY_INPUT_VERSION

_DATA_IMAGE = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]*={0,2})")
_MAX_STREAM_BYTES = 32 * 1024 * 1024
_MAX_EVENT_LINE_BYTES = 2 * 1024 * 1024
_MAX_EVENTS = 8192
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_IMAGE_PIXELS = 32 * 1024 * 1024
_MAX_EVIDENCE_ITEMS = 512
_MAX_TEXT_PROJECTION_BYTES = 64 * 1024


class FlatEvidenceError(RuntimeError):
    """Bounded reason for using the actor's Original request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class FlatEvidence:
    """The only evidence object retained by one flat Sentinel evaluation."""

    packet: bytes | None
    current_image_data_url: str | None
    execution_state: FlatExecutionState
    history_error: str | None = None


@dataclass(frozen=True, slots=True)
class _CurrentImage:
    data_url: str
    encoded_sha256: str
    width: int
    height: int
    media_type: str


class FlatCollectorEvidenceSource:
    """Build a history packet and execution facts from one causal prefix read."""

    def __init__(self) -> None:
        self._blob_store: BlobStore | None = None
        self._pixel_identity: CollectorPixelIdentity | None = None

    def build(
        self,
        *,
        request: JsonValue,
        logical_call_id: str,
        host_id: str,
        history: FlatHistory,
    ) -> FlatEvidence:
        if not logical_call_id or not host_id:
            raise FlatEvidenceError("INVALID_CALL", "call and host IDs are required")
        if host_id != history.host_id:
            raise FlatEvidenceError(
                "HISTORY_BINDING_MISMATCH", "history does not bind this actor request"
            )

        audit_context = get_audit_context()
        recorder = _trusted_task_recorder(audit_context)
        assert audit_context is not None  # guarded by _trusted_task_recorder
        cutoff_id = audit_context.parent_event_id
        task_run_id = audit_context.task_run_id
        if cutoff_id is None or task_run_id is None:
            raise FlatEvidenceError("INCOMPLETE_AUDIT_CONTEXT", "task cutoff is unavailable")
        events = _read_prefix(recorder, audit_context, cutoff_id)
        current_event = _resolve_current_event(events, audit_context)

        if self._blob_store is not recorder.blob_store:
            self._blob_store = recorder.blob_store
            self._pixel_identity = CollectorPixelIdentity(recorder.blob_store)
        try:
            execution_state = build_flat_execution_state(
                events,
                current_event,
                pixel_identity=self._pixel_identity,
            )
        except Exception:
            execution_state = FlatExecutionState(error="EXECUTION_STATE_UNAVAILABLE")

        packet: bytes | None = None
        current_image_data_url: str | None = None
        history_error: str | None = None
        if history.spans:
            try:
                if (
                    recorder.capture_complete is not True
                    or recorder.collector_error_event_ids
                    or any(event.get("event_type") == "collector_error" for event in events)
                ):
                    raise FlatEvidenceError(
                        "COLLECTOR_INCOMPLETE",
                        "incomplete Collector data cannot authorize a history edit",
                    )
                task_event = _resolve_task_event(events)
                current_image = _bind_current_image(request, history, current_event, recorder)
                packet = _build_packet(
                    logical_call_id=logical_call_id,
                    host_id=host_id,
                    history=history,
                    events=events,
                    current_event=current_event,
                    task_event=task_event,
                    current_image=current_image,
                )
                current_image_data_url = current_image.data_url
            except Exception as exc:
                history_error = _error_code(exc, "HISTORY_EVIDENCE_UNAVAILABLE")
        return FlatEvidence(
            packet=packet,
            current_image_data_url=current_image_data_url,
            execution_state=execution_state,
            history_error=history_error,
        )


def _trusted_task_recorder(context: AuditContext | None) -> TaskRecorder:
    if context is None or type(context.recorder) is not TaskRecorder:
        raise FlatEvidenceError("NO_AUDIT_CONTEXT", "no task Collector is bound")
    recorder = context.recorder
    if (
        type(context.run_id) is not str
        or type(context.task_run_id) is not str
        or type(context.step_id) is not str
        or type(context.parent_event_id) is not str
        or context.task_run_id != recorder.task_run_id
    ):
        raise FlatEvidenceError("INCOMPLETE_AUDIT_CONTEXT", "Collector binding is incomplete")
    if type(recorder.blob_store) is not BlobStore:
        raise FlatEvidenceError("INVALID_BLOB_STORE", "Collector blob store is unavailable")
    return recorder


def _read_prefix(
    recorder: TaskRecorder,
    context: AuditContext,
    cutoff_event_id: str,
) -> tuple[dict[str, JsonValue], ...]:
    path = recorder.path
    expected = recorder.blob_store.root / "tasks" / cast(str, context.task_run_id) / "events.jsonl"
    try:
        if path.resolve(strict=True) != expected.resolve(strict=True) or path.is_symlink():
            raise FlatEvidenceError("INVALID_STREAM_PATH", "task stream path is not canonical")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FlatEvidenceError:
        raise
    except OSError as exc:
        raise FlatEvidenceError("TASK_STREAM_UNAVAILABLE", "task stream cannot be opened") from exc

    try:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FlatEvidenceError(
                    "TASK_STREAM_BUSY", "task stream is being appended"
                ) from exc
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise FlatEvidenceError("INVALID_STREAM", "task stream is not a non-empty file")
        if metadata.st_size > _MAX_STREAM_BYTES:
            # We still read only until the current event, but refuse a prefix
            # whose cutoff is not found inside the bounded window.
            read_size = _MAX_STREAM_BYTES
        else:
            read_size = metadata.st_size
        raw = bytearray()
        while len(raw) < read_size:
            chunk = os.read(descriptor, min(64 * 1024, read_size - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    except FlatEvidenceError:
        raise
    except OSError as exc:
        raise FlatEvidenceError("TASK_STREAM_READ_FAILED", "task stream read failed") from exc
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)

    lines = bytes(raw).split(b"\n")
    events: list[dict[str, JsonValue]] = []
    seen_ids: set[str] = set()
    cutoff_found = False
    for line in lines:
        if not line:
            continue
        if len(events) >= _MAX_EVENTS or len(line) > _MAX_EVENT_LINE_BYTES:
            raise FlatEvidenceError("PREFIX_LIMIT_EXCEEDED", "Collector prefix exceeds bounds")
        try:
            decoded = json.loads(line)
            if type(decoded) is not dict:
                raise TypeError("event is not an object")
            event = cast(dict[str, JsonValue], decoded)
            validate_event_envelope(event)
            if strict_json_bytes(event) != line:
                raise ValueError("event is not canonical JSON")
        except Exception as exc:
            raise FlatEvidenceError("INVALID_EVENT", "Collector event is invalid") from exc
        expected_seq = len(events) + 1
        if event.get("seq") != expected_seq:
            raise FlatEvidenceError("EVENT_SEQUENCE_MISMATCH", "event sequence is not contiguous")
        if (
            event.get("run_id") != context.run_id
            or event.get("task_run_id") != context.task_run_id
            or event.get("stream_id") != context.task_run_id
        ):
            raise FlatEvidenceError("EVENT_STREAM_MISMATCH", "event belongs to another task")
        event_id = event.get("event_id")
        if type(event_id) is not str or event_id in seen_ids:
            raise FlatEvidenceError("DUPLICATE_EVENT_ID", "event ID is invalid or repeated")
        parent = event.get("caused_by_event_id")
        if parent is not None and parent not in seen_ids:
            raise FlatEvidenceError("INVALID_CAUSAL_PARENT", "event parent is not earlier")
        seen_ids.add(event_id)
        events.append(event)
        if event_id == cutoff_event_id:
            cutoff_found = True
            break
    if not cutoff_found:
        raise FlatEvidenceError("CUTOFF_NOT_FOUND", "current step is outside the bounded prefix")
    return tuple(events)


def _resolve_current_event(
    events: tuple[dict[str, JsonValue], ...],
    context: AuditContext,
) -> dict[str, JsonValue]:
    current = [
        item
        for item in events
        if item.get("event_id") == context.parent_event_id
        and item.get("event_type") == "step_started"
    ]
    if len(current) != 1 or _payload(current[0]).get("step_id") != context.step_id:
        raise FlatEvidenceError("CUTOFF_MISMATCH", "current step binding is inconsistent")
    return current[0]


def _resolve_task_event(
    events: tuple[dict[str, JsonValue], ...],
) -> dict[str, JsonValue]:
    starts = [item for item in events if item.get("event_type") == "task_started"]
    if len(starts) != 1 or starts[0].get("seq") != 1:
        raise FlatEvidenceError("TASK_START_MISSING", "one first task_started event is required")
    task_payload = _payload(starts[0])
    if (
        task_payload.get("task_goal_status") != "resolved"
        or type(task_payload.get("task_goal")) is not str
        or not task_payload["task_goal"]
    ):
        raise FlatEvidenceError("TASK_GOAL_MISSING", "resolved task instruction is unavailable")
    return starts[0]


def _bind_current_image(
    request: JsonValue,
    history: FlatHistory,
    current_event: dict[str, JsonValue],
    recorder: TaskRecorder,
) -> _CurrentImage:
    path = history.current_image_path
    if path is None:
        raise FlatEvidenceError("CURRENT_IMAGE_PATH", "current image path is unavailable")
    try:
        block = _get_at_path(request, path)
    except (KeyError, IndexError, TypeError) as exc:
        raise FlatEvidenceError("CURRENT_IMAGE_PATH", "current image path is invalid") from exc
    image_url = block.get("image_url") if type(block) is dict else None
    data_url = image_url.get("url") if type(image_url) is dict else None
    if type(block) is not dict or block.get("type") != "image_url" or type(data_url) is not str:
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "current image block is invalid")
    request_bytes, media_type = _decode_data_image(data_url)

    observation = _payload(current_event).get("observation")
    screenshot = observation.get("screenshot") if type(observation) is dict else None
    if type(screenshot) is not dict or type(screenshot.get("pixel_blob")) is not dict:
        raise FlatEvidenceError("CURRENT_SCREENSHOT_MISSING", "Collector screenshot is unavailable")
    width = screenshot.get("width")
    height = screenshot.get("height")
    if (
        screenshot.get("representation") != "canonical_png_from_runtime_pixels"
        or type(width) is not int
        or type(height) is not int
        or width < 1
        or height < 1
        or width * height > _MAX_IMAGE_PIXELS
    ):
        raise FlatEvidenceError("CURRENT_SCREENSHOT_INVALID", "screenshot metadata is invalid")
    try:
        collector_bytes = recorder.blob_store.read_bytes(cast(Any, screenshot["pixel_blob"]))
    except Exception as exc:
        raise FlatEvidenceError("CURRENT_SCREENSHOT_INVALID", "screenshot blob is invalid") from exc
    request_image = _decode_pixels(request_bytes)
    collector_image = _decode_pixels(collector_bytes)
    if (
        request_image[0] != (width, height)
        or collector_image[0] != (width, height)
        or request_image[1:] != collector_image[1:]
    ):
        raise FlatEvidenceError("CURRENT_IMAGE_DRIFT", "actor image differs from Collector pixels")
    return _CurrentImage(
        data_url=data_url,
        encoded_sha256=hashlib.sha256(request_bytes).hexdigest(),
        width=width,
        height=height,
        media_type=media_type,
    )


def _decode_data_image(value: str) -> tuple[bytes, str]:
    if type(value) is not str or len(value) > (_MAX_IMAGE_BYTES * 4 // 3 + 128):
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "image data URL is too large")
    matched = _DATA_IMAGE.fullmatch(value)
    if matched is None:
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "unsupported image data URL")
    try:
        raw = base64.b64decode(matched.group(2), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "image base64 is invalid") from exc
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "image bytes exceed bounds")
    return raw, matched.group(1)


def _decode_pixels(value: bytes) -> tuple[tuple[int, int], str, bytes]:
    try:
        with Image.open(io.BytesIO(value)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
                raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "decoded image is too large")
            image.load()
            rgba = image.convert("RGBA")
            return rgba.size, rgba.mode, rgba.tobytes()
    except FlatEvidenceError:
        raise
    except Exception as exc:
        raise FlatEvidenceError("CURRENT_IMAGE_INVALID", "image cannot be decoded") from exc


def _build_packet(
    *,
    logical_call_id: str,
    host_id: str,
    history: FlatHistory,
    events: tuple[dict[str, JsonValue], ...],
    current_event: dict[str, JsonValue],
    task_event: dict[str, JsonValue],
    current_image: _CurrentImage,
) -> bytes:
    targets: list[dict[str, JsonValue]] = []
    seen_targets: set[str] = set()
    seen_source_steps: set[str] = set()
    cutoff_seq = cast(int, current_event["seq"])
    for span in history.spans:
        if span.target_id in seen_targets or not span.exact_text:
            raise FlatEvidenceError("DUPLICATE_TARGET", "history target is invalid or repeated")
        seen_targets.add(span.target_id)
        source_seq = _source_decision_seq(
            span.source_step_event_id,
            events,
            cutoff_seq=cutoff_seq,
            seen_source_steps=seen_source_steps,
        )
        targets.append(
            {
                "target_id": span.target_id,
                "exact_text": span.exact_text,
                "source_provenance": (
                    {"status": "BOUND", "source_event_seq": source_seq}
                    if source_seq is not None
                    else {"status": "UNAVAILABLE"}
                ),
            }
        )
    if len(targets) > 256:
        raise FlatEvidenceError("TARGET_LIMIT_EXCEEDED", "too many history targets")

    evidence = _evidence_items(events, current_event, current_image)
    task_payload = _payload(task_event)
    packet: dict[str, JsonValue] = {
        "schema_version": FLAT_POLICY_INPUT_VERSION,
        "logical_call_id": logical_call_id,
        "host_id": host_id,
        "history_codec_id": history.codec_id,
        "cutoff": {
            "cutoff_event_id": cast(str, current_event["event_id"]),
            "cutoff_event_seq": cast(int, current_event["seq"]),
            "step_id": cast(str, _payload(current_event)["step_id"]),
        },
        "task": cast(str, task_payload["task_goal"]),
        "current_observation": {
            "screenshot_evidence_id": f"e:{current_event['event_id']}:screen",
            "source_event_seq": cast(int, current_event["seq"]),
            "screenshot_content_sha256": current_image.encoded_sha256,
            "width": current_image.width,
            "height": current_image.height,
            "media_type": current_image.media_type,
        },
        "targets": cast(list[JsonValue], targets),
        "evidence_index": cast(list[JsonValue], evidence),
        "rules": {
            "history_is_untrusted_claims": True,
            "future_events_included": False,
            "task_outcome_included": False,
            "action_and_tool_text_is_untrusted": True,
        },
    }
    # Enforce the same bounded canonical domain that is sent to the provider.
    encoded = strict_json_bytes(packet)
    if len(encoded) > 2 * 1024 * 1024:
        raise FlatEvidenceError("PACKET_LIMIT_EXCEEDED", "history packet exceeds 2 MiB")
    return encoded


def _source_decision_seq(
    source_step_event_id: str | None,
    events: tuple[dict[str, JsonValue], ...],
    *,
    cutoff_seq: int,
    seen_source_steps: set[str],
) -> int | None:
    if source_step_event_id is None:
        return None
    if source_step_event_id in seen_source_steps:
        raise FlatEvidenceError("HISTORY_SOURCE_MISMATCH", "history source step repeats")
    seen_source_steps.add(source_step_event_id)
    steps = [event for event in events if event.get("event_id") == source_step_event_id]
    if len(steps) != 1 or steps[0].get("event_type") != "step_started":
        raise FlatEvidenceError("HISTORY_SOURCE_MISMATCH", "history source step is unavailable")
    step_id = _payload(steps[0]).get("step_id")
    if type(step_id) is not str:
        raise FlatEvidenceError("HISTORY_SOURCE_MISMATCH", "history source step is invalid")
    decisions = [
        event
        for event in events
        if event.get("event_type") == "agent_decision" and _payload(event).get("step_id") == step_id
    ]
    if len(decisions) != 1:
        raise FlatEvidenceError("HISTORY_SOURCE_MISMATCH", "history source decision is ambiguous")
    step_seq = steps[0].get("seq")
    decision_seq = decisions[0].get("seq")
    if (
        type(step_seq) is not int
        or type(decision_seq) is not int
        or not step_seq < decision_seq < cutoff_seq
        or _payload(decisions[0]).get("parse_outcome") != "returned"
    ):
        raise FlatEvidenceError("HISTORY_SOURCE_MISMATCH", "history source decision is invalid")
    return decision_seq


def _evidence_items(
    events: tuple[dict[str, JsonValue], ...],
    current_event: dict[str, JsonValue],
    current_image: _CurrentImage,
) -> list[dict[str, JsonValue]]:
    cutoff_seq = cast(int, current_event["seq"])
    result: list[dict[str, JsonValue]] = [
        _evidence_value(
            evidence_id=f"e:{current_event['event_id']}:screen",
            role="CURRENT_UI_SCREENSHOT",
            source_event_seq=cutoff_seq,
            payload={"content_sha256": current_image.encoded_sha256},
        )
    ]
    observation = _payload(current_event).get("observation")
    if type(observation) is dict and observation.get("accessibility_tree") is not None:
        result.append(
            _bounded_evidence(
                current_event,
                "CURRENT_ACCESSIBILITY",
                observation.get("accessibility_tree"),
            )
        )
    allowed = {
        "action_execution_started",
        "transition_completed",
        "transition_failed",
        "transition_not_executed",
        "step_started",
    }
    for event in events:
        if cast(int, event["seq"]) >= cutoff_seq or event.get("event_type") not in allowed:
            continue
        payload = _payload(event)
        event_type = cast(str, event["event_type"])
        if event_type == "step_started":
            prior_observation = payload.get("observation")
            if type(prior_observation) is not dict:
                continue
            for key, role in (
                ("tool_call", "AGENT_VISIBLE_TOOL_RESULT"),
                ("ask_user_response", "USER_RESPONSE"),
                ("accessibility_tree", "PRIOR_POST_UI_STATE"),
            ):
                value = prior_observation.get(key)
                if value is not None:
                    result.append(_bounded_evidence(event, role, value))
        elif event_type == "action_execution_started":
            result.append(
                _bounded_evidence(
                    event,
                    "PRIOR_ACTION_ATTEMPT",
                    {
                        "execution_kind": payload.get("execution_kind"),
                        "action": payload.get("action"),
                    },
                )
            )
        else:
            terminal_projection: dict[str, JsonValue] = {"terminal": event_type}
            if payload.get("duration_ns") is not None:
                terminal_projection["duration_ns"] = payload.get("duration_ns")
            if event_type == "transition_failed" and payload.get("exception") is not None:
                terminal_projection["exception"] = payload.get("exception")
            if event_type == "transition_not_executed" and payload.get("reason") is not None:
                terminal_projection["reason"] = payload.get("reason")
            result.append(_bounded_evidence(event, "PRIOR_TRANSITION_STATUS", terminal_projection))
            if event_type != "transition_not_executed":
                post_value = payload.get("post_observation")
                post = post_value if type(post_value) is dict else {}
                if post.get("accessibility_tree") is not None:
                    result.append(
                        _bounded_evidence(
                            event,
                            "PRIOR_POST_UI_STATE",
                            post["accessibility_tree"],
                        )
                    )
                raw_execution = (
                    payload.get("execution_result")
                    if event_type == "transition_completed"
                    else payload.get("available_execution_result")
                )
                execution = raw_execution if type(raw_execution) is dict else None
                if raw_execution is not None:
                    transport: JsonValue
                    if execution is None:
                        transport = raw_execution
                    else:
                        transport = {
                            key: value
                            for key, value in execution.items()
                            if key
                            not in {
                                "agent_visible_tool_result",
                                "agent_visible_tool_result_snapshot_blob",
                                "ask_user_response",
                                "ask_user_response_snapshot_blob",
                            }
                        }
                    result.append(_bounded_evidence(event, "EXECUTOR_TRANSPORT_RESULT", transport))
                if event_type == "transition_completed":
                    tool = (
                        execution.get("agent_visible_tool_result")
                        if execution is not None
                        else None
                    )
                    if tool is None:
                        tool = post.get("tool_call")
                    if tool is not None:
                        result.append(_bounded_evidence(event, "AGENT_VISIBLE_TOOL_RESULT", tool))
                    user = execution.get("ask_user_response") if execution is not None else None
                    if user is None:
                        user = post.get("ask_user_response")
                    if user is not None:
                        result.append(_bounded_evidence(event, "USER_RESPONSE", user))
        if len(result) > _MAX_EVIDENCE_ITEMS:
            raise FlatEvidenceError("EVIDENCE_LIMIT_EXCEEDED", "too many evidence items")
    return result


def _bounded_evidence(
    event: dict[str, JsonValue], role: str, payload: JsonValue
) -> dict[str, JsonValue]:
    encoded = strict_json_bytes(payload)
    if len(encoded) > _MAX_TEXT_PROJECTION_BYTES:
        payload = {"omitted": True}
    return _evidence_value(
        evidence_id=f"e:{event['event_id']}:{role.lower()}",
        role=role,
        source_event_seq=cast(int, event["seq"]),
        payload=payload,
    )


def _evidence_value(
    *,
    evidence_id: str,
    role: str,
    source_event_seq: int,
    payload: JsonValue,
) -> dict[str, JsonValue]:
    return {
        "evidence_id": evidence_id,
        "role": role,
        "source_event_seq": source_event_seq,
        "projection": payload,
    }


def _payload(event: dict[str, JsonValue]) -> dict[str, JsonValue]:
    value = event.get("payload")
    if type(value) is not dict:
        raise FlatEvidenceError("INVALID_EVENT", "event payload is not an object")
    return value


def _get_at_path(value: JsonValue, path: JsonPath) -> JsonValue:
    current = value
    for part in path:
        if type(part) is int:
            if type(current) is not list:
                raise TypeError("path expected a list")
            current = current[part]
        else:
            if type(current) is not dict:
                raise TypeError("path expected an object")
            current = current[cast(str, part)]
    return current


def _error_code(error: BaseException, default: str) -> str:
    value = getattr(error, "code", None)
    if (
        type(value) is str
        and value
        and len(value) <= 128
        and value[0].isupper()
        and all(character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in value)
    ):
        return value
    return default


__all__ = [
    "FlatCollectorEvidenceSource",
    "FlatEvidence",
    "FlatEvidenceError",
]
