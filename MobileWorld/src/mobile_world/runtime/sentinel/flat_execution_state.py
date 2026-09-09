"""Minimal local execution facts for the active Sentinel path.

The fold consumes an already validated Collector prefix and only counts
``action_execution_started`` events.  Action payloads are used transiently for
exact equality and are never exposed or rendered.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from PIL import Image

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_EVENTS = 8192
_MAX_ACTIONS = 64
_MAX_ACTION_BYTES = 64 * 1024
_MAX_REPEAT_FACTS = 4
_MAX_IMAGE_BYTES = 40 * 1024 * 1024
_MAX_IMAGE_PIXELS = 32 * 1024 * 1024
_MAX_TASK_IMAGE_BYTES = 256 * 1024 * 1024

PixelIdentity = Callable[[Mapping[str, Any]], str | None]


@dataclass(frozen=True, slots=True)
class FlatRepeatFact:
    occurrence_count: int
    pre_screen_pixels_exact_same: bool | None
    executor_returned_count: int
    executor_raised_count: int
    executor_unknown_count: int


@dataclass(frozen=True, slots=True)
class FlatExecutionState:
    repeat_facts: tuple[FlatRepeatFact, ...] = ()
    error: str | None = None

    @property
    def repeat_count(self) -> int:
        return len(self.repeat_facts)


class BlobReader(Protocol):
    def read_bytes(self, reference: Any) -> bytes: ...


@dataclass(slots=True)
class _Attempt:
    event_id: str
    event_seq: int
    execution_id: str
    step_id: str
    step_event_id: str
    decision_id: str
    decision_event_id: str
    action: bytes
    terminal: str = "unknown"


class _StateError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CollectorPixelIdentity:
    """Resolve exact pixel equality through Collector's verified blob reader."""

    def __init__(
        self,
        blob_reader: BlobReader,
        *,
        max_image_bytes: int = _MAX_IMAGE_BYTES,
        max_image_pixels: int = _MAX_IMAGE_PIXELS,
        max_task_image_bytes: int = _MAX_TASK_IMAGE_BYTES,
    ) -> None:
        self._reader = blob_reader
        self._max_bytes = max_image_bytes
        self._max_pixels = max_image_pixels
        self._max_task_bytes = max_task_image_bytes
        self._consumed_bytes = 0
        self._cache: dict[tuple[str, int, int, str], str | None] = {}

    def __call__(self, step_event: Mapping[str, Any]) -> str | None:
        try:
            payload = _payload(step_event)
            observation = payload.get("observation")
            screenshot = observation.get("screenshot") if isinstance(observation, dict) else None
            if not isinstance(screenshot, dict):
                return None
            reference = screenshot.get("pixel_blob")
            width, height, mode = (
                screenshot.get("width"),
                screenshot.get("height"),
                screenshot.get("mode"),
            )
            if (
                screenshot.get("representation") != "canonical_png_from_runtime_pixels"
                or not isinstance(reference, dict)
                or not isinstance(width, int)
                or isinstance(width, bool)
                or not isinstance(height, int)
                or isinstance(height, bool)
                or not isinstance(mode, str)
                or width < 1
                or height < 1
                or width * height > self._max_pixels
            ):
                return None
            digest, byte_length = reference.get("digest"), reference.get("byte_length")
            if (
                reference.get("algorithm") != "sha256"
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
                or not isinstance(byte_length, int)
                or isinstance(byte_length, bool)
                or not 0 < byte_length <= self._max_bytes
            ):
                return None
            cache_key = (digest, width, height, mode)
            if cache_key in self._cache:
                return self._cache[cache_key]
            if self._consumed_bytes + byte_length > self._max_task_bytes:
                self._cache[cache_key] = None
                return None
            self._consumed_bytes += byte_length
            try:
                encoded = self._reader.read_bytes(cast(Any, reference))
                if len(encoded) != byte_length:
                    self._cache[cache_key] = None
                    return None
                with Image.open(io.BytesIO(encoded)) as image:
                    if image.format != "PNG" or image.size != (width, height) or image.mode != mode:
                        self._cache[cache_key] = None
                        return None
                    image.load()
                    rgba = image.convert("RGBA")
                    pixels = rgba.tobytes()
            except Exception:
                self._cache[cache_key] = None
                return None
            hasher = hashlib.sha256()
            for part in (b"RGBA", str(width).encode(), str(height).encode(), pixels):
                hasher.update(part)
                hasher.update(b"\0")
            identity = hasher.hexdigest()
            self._cache[cache_key] = identity
            return identity
        except Exception:
            return None


def build_flat_execution_state(
    events: Sequence[Mapping[str, Any]],
    current_event: Mapping[str, Any],
    *,
    pixel_identity: PixelIdentity | None = None,
) -> FlatExecutionState:
    """Fold only the causal prefix ending at ``current_event``.

    Invalid local evidence makes this optional channel unavailable; it never
    raises into the actor path.
    """

    try:
        return _build(events, current_event, pixel_identity)
    except _StateError as exc:
        return FlatExecutionState(error=exc.code)
    except Exception:
        return FlatExecutionState(error="EXECUTION_STATE_UNAVAILABLE")


def format_flat_execution_state(state: FlatExecutionState) -> str | None:
    if not state.repeat_facts:
        return None
    lines = ["Sentinel execution facts from earlier steps:"]
    for fact in state.repeat_facts:
        if fact.pre_screen_pixels_exact_same is True:
            screen = "Their recorded starting screen pixels were exactly equal."
        elif fact.pre_screen_pixels_exact_same is False:
            screen = "Their recorded starting screen pixels were not all equal."
        else:
            screen = "Their starting-screen pixel equality is not established."
        lines.append(
            "- The same exact executor-dispatched action occurred at least "
            f"{fact.occurrence_count} times. {screen} Recorded executor outcomes: "
            f"returned={fact.executor_returned_count}, "
            f"raised={fact.executor_raised_count}, "
            f"unknown={fact.executor_unknown_count}."
        )
    lines.append(
        "These are past execution facts only. They do not establish task success, failure, "
        "or progress, and they do not recommend a next action."
    )
    return "\n".join(lines)


def _build(
    events: Sequence[Mapping[str, Any]],
    current_event: Mapping[str, Any],
    pixel_identity: PixelIdentity | None,
) -> FlatExecutionState:
    cutoff_id = _text(current_event.get("event_id"), "INVALID_CUTOFF")
    cutoff_seq = current_event.get("seq")
    if current_event.get("event_type") != "step_started" or not isinstance(cutoff_seq, int):
        raise _StateError("INVALID_CUTOFF")
    steps: dict[str, Mapping[str, Any]] = {}
    decisions: dict[str, Mapping[str, Any]] = {}
    terminals: dict[str, Mapping[str, Any]] = {}
    starts: list[_Attempt] = []
    found_cutoff = False
    for index, event in enumerate(events):
        if index >= _MAX_EVENTS:
            raise _StateError("EVENT_LIMIT_EXCEEDED")
        if event.get("event_id") == cutoff_id:
            if event.get("event_type") != "step_started" or event.get("seq") != cutoff_seq:
                raise _StateError("CUTOFF_MISMATCH")
            found_cutoff = True
            break
        event_type = event.get("event_type")
        if event_type == "step_started":
            step_id = _text(_payload(event).get("step_id"), "INVALID_STEP")
            if step_id in steps:
                raise _StateError("DUPLICATE_STEP")
            steps[step_id] = event
        elif event_type == "agent_decision":
            decision_id = _text(_payload(event).get("decision_id"), "INVALID_DECISION")
            if decision_id in decisions:
                raise _StateError("DUPLICATE_DECISION")
            decisions[decision_id] = event
        elif event_type == "action_execution_started":
            if len(starts) >= _MAX_ACTIONS:
                raise _StateError("ACTION_LIMIT_EXCEEDED")
            payload = _payload(event)
            action = payload.get("action")
            encoded = _encode_action(action)
            if len(encoded) > _MAX_ACTION_BYTES:
                raise _StateError("ACTION_LIMIT_EXCEEDED")
            starts.append(
                _Attempt(
                    event_id=_text(event.get("event_id"), "INVALID_ACTION_EVENT"),
                    event_seq=_seq(event, "INVALID_ACTION_EVENT"),
                    execution_id=_text(payload.get("execution_id"), "INVALID_ACTION_EVENT"),
                    step_id=_text(payload.get("step_id"), "INVALID_ACTION_EVENT"),
                    step_event_id="",
                    decision_id=_text(payload.get("decision_id"), "INVALID_ACTION_EVENT"),
                    decision_event_id=_text(
                        event.get("caused_by_event_id"), "INVALID_ACTION_EVENT"
                    ),
                    action=encoded,
                )
            )
        elif event_type in {"transition_completed", "transition_failed"}:
            action_event_id = _text(
                _payload(event).get("action_execution_event_id"), "INVALID_TRANSITION"
            )
            if action_event_id in terminals:
                raise _StateError("DUPLICATE_TRANSITION")
            terminals[action_event_id] = event
    if not found_cutoff:
        raise _StateError("CUTOFF_NOT_FOUND")

    by_action: dict[bytes, list[_Attempt]] = {}
    seen_event_ids: set[str] = set()
    seen_execution_ids: set[str] = set()
    for attempt in starts:
        if attempt.event_id in seen_event_ids or attempt.execution_id in seen_execution_ids:
            raise _StateError("DUPLICATE_ACTION")
        seen_event_ids.add(attempt.event_id)
        seen_execution_ids.add(attempt.execution_id)
        step = steps.get(attempt.step_id)
        decision = decisions.get(attempt.decision_id)
        if step is None:
            raise _StateError("ACTION_STEP_MISSING")
        attempt.step_event_id = _text(step.get("event_id"), "INVALID_STEP")
        decision_payload = _payload(decision) if decision is not None else {}
        parsed_action = decision_payload.get("parsed_action")
        if (
            decision is None
            or decision.get("event_id") != attempt.decision_event_id
            or decision_payload.get("decision_id") != attempt.decision_id
            or decision_payload.get("step_id") != attempt.step_id
            or decision_payload.get("parse_outcome") != "returned"
            or type(parsed_action) is not dict
            or "value" not in parsed_action
            or _encode_action(parsed_action["value"]) != attempt.action
            or not _seq(step, "INVALID_STEP")
            < _seq(decision, "INVALID_DECISION")
            < attempt.event_seq
        ):
            raise _StateError("ACTION_DECISION_BINDING_MISMATCH")
        terminal = terminals.get(attempt.event_id)
        if terminal is not None:
            _bind_terminal(attempt, terminal)
            attempt.terminal = (
                "returned" if terminal.get("event_type") == "transition_completed" else "raised"
            )
        by_action.setdefault(attempt.action, []).append(attempt)

    if set(terminals) - seen_event_ids:
        raise _StateError("ORPHAN_TRANSITION")

    repeated = [items for items in by_action.values() if len(items) >= 2]
    repeated.sort(key=lambda items: starts.index(items[-1]), reverse=True)
    facts: list[FlatRepeatFact] = []
    for items in repeated[:_MAX_REPEAT_FACTS]:
        identities: list[str | None] = []
        if pixel_identity is not None:
            identities = [pixel_identity(steps[item.step_id]) for item in items]
        all_identities_known = bool(identities) and all(
            identity is not None and _SHA256.fullmatch(identity) for identity in identities
        )
        exact_screen = len(set(identities)) == 1 if all_identities_known else None
        facts.append(
            FlatRepeatFact(
                occurrence_count=len(items),
                pre_screen_pixels_exact_same=exact_screen,
                executor_returned_count=sum(item.terminal == "returned" for item in items),
                executor_raised_count=sum(item.terminal == "raised" for item in items),
                executor_unknown_count=sum(item.terminal == "unknown" for item in items),
            )
        )
    return FlatExecutionState(tuple(facts))


def _bind_terminal(attempt: _Attempt, terminal: Mapping[str, Any]) -> None:
    payload = _payload(terminal)
    terminal_action = _encode_action(payload.get("action"))
    if (
        terminal.get("caused_by_event_id") != attempt.event_id
        or payload.get("action_execution_event_id") != attempt.event_id
        or payload.get("execution_id") != attempt.execution_id
        or payload.get("step_id") != attempt.step_id
        or payload.get("decision_id") != attempt.decision_id
        or payload.get("pre_observation_event_id") != attempt.step_event_id
        or terminal_action != attempt.action
        or _seq(terminal, "INVALID_TRANSITION") <= attempt.event_seq
    ):
        raise _StateError("TRANSITION_BINDING_MISMATCH")


def _encode_action(value: object) -> bytes:
    if type(value) is not dict:
        raise _StateError("INVALID_ACTION")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise _StateError("INVALID_ACTION") from exc


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise _StateError("INVALID_EVENT")
    return payload


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise _StateError(code)
    return value


def _seq(event: Mapping[str, Any], code: str) -> int:
    value = event.get("seq")
    if type(value) is not int or value < 1:
        raise _StateError(code)
    return value


__all__ = [
    "BlobReader",
    "CollectorPixelIdentity",
    "FlatExecutionState",
    "FlatRepeatFact",
    "PixelIdentity",
    "build_flat_execution_state",
    "format_flat_execution_state",
]
