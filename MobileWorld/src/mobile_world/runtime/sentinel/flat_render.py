"""Apply flat history drops and one local execution-state notice."""

from __future__ import annotations

from collections import defaultdict
from typing import cast

from mobile_world.runtime.sentinel.flat_codec import (
    FlatEditableSpan,
    FlatHistory,
    JsonPath,
)
from mobile_world.runtime.sentinel.flat_contracts import JsonValue
from mobile_world.runtime.sentinel.flat_execution_state import (
    FlatExecutionState,
    format_flat_execution_state,
)


class FlatRenderError(RuntimeError):
    """A render failure that always leaves the actor request unchanged."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def render_flat_request(
    *,
    original: JsonValue,
    history: FlatHistory,
    drop_target_ids: tuple[str, ...],
    execution_state: FlatExecutionState,
) -> tuple[JsonValue, int, int, str | None]:
    """Create one candidate without a result wrapper or intermediate receipt."""

    candidate = original
    if len(set(drop_target_ids)) != len(drop_target_ids):
        raise FlatRenderError("DUPLICATE_DROP", "drop target repeats")
    targets = _target_spans(original, history) if drop_target_ids else {}
    if set(drop_target_ids) - set(targets):
        raise FlatRenderError("UNKNOWN_DROP", "policy selected an unknown history target")
    _apply_drops(candidate, targets, drop_target_ids)

    state_error = execution_state.error
    notice = None if state_error is not None else format_flat_execution_state(execution_state)
    repeat_count = execution_state.repeat_count if notice is not None else 0
    if notice is not None:
        try:
            _insert_notice(candidate, history, notice)
        except FlatRenderError as exc:
            notice = None
            repeat_count = 0
            state_error = exc.code
    return candidate, len(drop_target_ids), repeat_count, state_error


def _target_spans(
    original: JsonValue,
    history: FlatHistory,
) -> dict[str, tuple[JsonPath, FlatEditableSpan]]:
    targets: dict[str, tuple[JsonPath, FlatEditableSpan]] = {}
    occupied: list[tuple[JsonPath, int, int]] = []
    for span in history.spans:
        if span.target_id in targets or not span.exact_text:
            raise FlatRenderError("INVALID_TARGET", "history target is invalid or repeated")
        value = _get_at_path(original, span.container_path)
        if (
            type(value) is not str
            or type(span.char_start) is not int
            or type(span.char_end) is not int
            or not 0 <= span.char_start < span.char_end <= len(value)
            or value[span.char_start : span.char_end] != span.exact_text
        ):
            raise FlatRenderError("TARGET_DRIFT", "history target text changed")
        for path, start, end in occupied:
            if path == span.container_path and span.char_start < end and start < span.char_end:
                raise FlatRenderError("OVERLAPPING_TARGETS", "history targets overlap")
        occupied.append((span.container_path, span.char_start, span.char_end))
        targets[span.target_id] = (span.container_path, span)
    return targets


def _apply_drops(
    candidate: JsonValue,
    targets: dict[str, tuple[JsonPath, FlatEditableSpan]],
    drop_target_ids: tuple[str, ...],
) -> None:
    grouped: dict[JsonPath, list[FlatEditableSpan]] = defaultdict(list)
    for target_id in drop_target_ids:
        path, span = targets[target_id]
        grouped[path].append(span)
    for path, spans in grouped.items():
        value = _get_at_path(candidate, path)
        if type(value) is not str:
            raise FlatRenderError("TARGET_NOT_TEXT", "history target container is not text")
        rendered = value
        for span in sorted(spans, key=lambda item: item.char_start, reverse=True):
            if rendered[span.char_start : span.char_end] != span.exact_text:
                raise FlatRenderError("TARGET_DRIFT", "history target text changed")
            rendered = rendered[: span.char_start] + rendered[span.char_end :]
        _set_at_path(candidate, path, rendered)


def _insert_notice(
    candidate: JsonValue,
    history: FlatHistory,
    notice: str,
) -> None:
    path = history.insert_container_path
    index = history.insert_index
    if path is None or type(index) is not int:
        raise FlatRenderError("STATE_ANCHOR_INVALID", "current observation anchor is unavailable")
    candidate_container = _get_at_path(candidate, path)
    if type(candidate_container) is not list or not 0 <= index < len(candidate_container):
        raise FlatRenderError("STATE_ANCHOR_INVALID", "state insertion position is invalid")
    reference = candidate_container[index]
    if type(reference) is not dict or reference.get("type") not in {"image_url", "text"}:
        raise FlatRenderError("STATE_ANCHOR_INVALID", "state anchor is not a current observation")
    inserted: JsonValue = {"type": "text", "text": notice}
    candidate_container.insert(index, inserted)


def _get_at_path(value: JsonValue, path: JsonPath) -> JsonValue:
    current = value
    for part in path:
        if type(part) is int:
            if type(current) is not list:
                raise FlatRenderError("INVALID_PATH", "path expected a list")
            current = current[part]
        else:
            if type(current) is not dict:
                raise FlatRenderError("INVALID_PATH", "path expected an object")
            current = current[cast(str, part)]
    return current


def _set_at_path(value: JsonValue, path: JsonPath, replacement: JsonValue) -> None:
    if not path:
        raise FlatRenderError("INVALID_PATH", "cannot replace the request root")
    parent = _get_at_path(value, path[:-1])
    final = path[-1]
    if type(final) is int:
        if type(parent) is not list:
            raise FlatRenderError("INVALID_PATH", "path expected a list")
        parent[final] = replacement
    else:
        if type(parent) is not dict:
            raise FlatRenderError("INVALID_PATH", "path expected an object")
        parent[cast(str, final)] = replacement


__all__ = ["FlatRenderError", "render_flat_request"]
