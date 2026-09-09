"""Small registered history adapters for the active Sentinel path.

The adapters inspect the actor request once.  They do not build a historical
``HistoryIR`` graph: the caller only needs exact editable text spans and the
place immediately before the current screenshot.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

JsonPath = tuple[str | int, ...]

QWEN_CODEC_ID = "mobileworld.g1.history-codec.qwen-flat-progress"
QWEN_HOST_ID = "mobileworld.qwen3vl.actor"
MAI_CODEC_ID = "mobileworld.g1.history-codec.mai-raw-replay"
MAI_HOST_ID = "mobileworld.mai-ui.actor"
QWEN_SOURCE_BOUND_HISTORY_ENCODING = "mobileworld.qwen-flat-progress.source-bound.v1"

_QWEN_QUERY = "\nThe user query: "
_QWEN_PROGRESS = "\nTask progress (You have done the following operation on the current device): "
_THINK_OPEN = "<thinking>"
_THINK_CLOSE = "</thinking>"
_LEGACY_THINK_CLOSE = "</think>"
_TOOL_OPEN = "<tool_call>"
_TOOL_CLOSE = "</tool_call>"


class FlatHistoryStatus(StrEnum):
    READY = "READY"
    NO_HISTORY = "NO_HISTORY"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class FlatEditableSpan:
    target_id: str
    container_path: JsonPath
    char_start: int
    char_end: int
    exact_text: str
    source_step_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class FlatHistory:
    status: FlatHistoryStatus
    host_id: str
    codec_id: str
    spans: tuple[FlatEditableSpan, ...]
    current_image_path: JsonPath | None
    insert_container_path: JsonPath | None
    insert_index: int | None
    reason: str | None = None


class FlatHistoryAdapter(Protocol):
    host_id: str
    codec_id: str

    def extract(
        self,
        request: Mapping[str, Any],
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> FlatHistory: ...


class _Unsupported(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FlatCodecRegistry:
    """Registry keyed by representation, never by target-model identity."""

    def __init__(self, adapters: Iterable[FlatHistoryAdapter] = ()) -> None:
        self._adapters: dict[str, FlatHistoryAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: FlatHistoryAdapter) -> None:
        codec_id = adapter.codec_id
        if not codec_id or codec_id in self._adapters:
            raise ValueError(f"duplicate or empty flat history codec: {codec_id!r}")
        self._adapters[codec_id] = adapter

    def by_id(self, codec_id: str) -> FlatHistoryAdapter:
        try:
            return self._adapters[codec_id]
        except KeyError as exc:
            raise KeyError(f"unregistered flat history codec: {codec_id}") from exc


class QwenFlatProgressAdapter:
    host_id = QWEN_HOST_ID
    codec_id = QWEN_CODEC_ID

    def extract(
        self,
        request: Mapping[str, Any],
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> FlatHistory:
        try:
            claim_ranges = _qwen_claim_ranges(attributes)
            spans, image_path, container, index = _extract_qwen(request, claim_ranges)
        except _Unsupported as exc:
            return _unsupported(self, exc.code)
        return FlatHistory(
            status=FlatHistoryStatus.READY if spans else FlatHistoryStatus.NO_HISTORY,
            host_id=self.host_id,
            codec_id=self.codec_id,
            spans=spans,
            current_image_path=image_path,
            insert_container_path=container,
            insert_index=index,
            reason=None if spans else "NO_HISTORY",
        )


class MaiRawReplayAdapter:
    host_id = MAI_HOST_ID
    codec_id = MAI_CODEC_ID

    def extract(
        self,
        request: Mapping[str, Any],
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> FlatHistory:
        try:
            source_ids = _history_source_step_ids(attributes)
            spans, image_path, container, index = _extract_mai(request, source_ids)
        except _Unsupported as exc:
            return _unsupported(self, exc.code)
        return FlatHistory(
            status=FlatHistoryStatus.READY if spans else FlatHistoryStatus.NO_HISTORY,
            host_id=self.host_id,
            codec_id=self.codec_id,
            spans=spans,
            current_image_path=image_path,
            insert_container_path=container,
            insert_index=index,
            reason=None if spans else "NO_HISTORY",
        )


def build_flat_codec_registry(
    additional: Iterable[FlatHistoryAdapter] = (),
) -> FlatCodecRegistry:
    return FlatCodecRegistry((QwenFlatProgressAdapter(), MaiRawReplayAdapter(), *additional))


def _unsupported(adapter: FlatHistoryAdapter, reason: str) -> FlatHistory:
    return FlatHistory(
        status=FlatHistoryStatus.UNSUPPORTED,
        host_id=adapter.host_id,
        codec_id=adapter.codec_id,
        spans=(),
        current_image_path=None,
        insert_container_path=None,
        insert_index=None,
        reason=reason,
    )


def _qwen_claim_ranges(
    attributes: Mapping[str, Any] | None,
) -> tuple[tuple[int, int, str | None], ...]:
    if (
        attributes is None
        or attributes.get("history_encoding") != QWEN_SOURCE_BOUND_HISTORY_ENCODING
    ):
        raise _Unsupported("QWEN_UNTRUSTED_HISTORY_ENCODING")
    count = attributes.get("history_step_count")
    if type(count) is not int or not 0 <= count <= 512:
        raise _Unsupported("QWEN_STEP_COUNT_INVALID")
    value = attributes.get("history_claim_ranges")
    source_ids = attributes.get("history_source_step_event_ids")
    if (
        type(value) is not tuple
        or len(value) != count
        or type(source_ids) is not tuple
        or len(source_ids) != count
    ):
        raise _Unsupported("QWEN_CLAIM_RANGES_INVALID")
    ranges: list[tuple[int, int, str | None]] = []
    for item, source_id in zip(value, source_ids, strict=True):
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not int
            or type(item[1]) is not int
            or not 0 <= item[0] < item[1]
        ):
            raise _Unsupported("QWEN_CLAIM_RANGES_INVALID")
        if source_id is not None and (
            type(source_id) is not str or not source_id or len(source_id) > 128
        ):
            raise _Unsupported("QWEN_SOURCE_STEP_INVALID")
        ranges.append((item[0], item[1], source_id))
    if any(previous[1] >= current[0] for previous, current in zip(ranges, ranges[1:])):
        raise _Unsupported("QWEN_CLAIM_RANGES_INVALID")
    return tuple(ranges)


def _messages(request: Mapping[str, Any]) -> Sequence[Any]:
    messages = request.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes, bytearray)):
        raise _Unsupported("MESSAGES_MISSING")
    return messages


def _message(messages: Sequence[Any], index: int) -> Mapping[str, Any]:
    try:
        message = messages[index]
    except IndexError as exc:
        raise _Unsupported("MESSAGE_MISSING") from exc
    if not isinstance(message, Mapping):
        raise _Unsupported("MESSAGE_INVALID")
    return message


def _blocks(message: Mapping[str, Any]) -> Sequence[Any]:
    blocks = message.get("content")
    if (
        not isinstance(blocks, Sequence)
        or isinstance(blocks, (str, bytes, bytearray))
        or not blocks
    ):
        raise _Unsupported("CONTENT_INVALID")
    return blocks


def _block(blocks: Sequence[Any], index: int, block_type: str) -> Mapping[str, Any]:
    try:
        block = blocks[index]
    except IndexError as exc:
        raise _Unsupported("CONTENT_INVALID") from exc
    if not isinstance(block, Mapping) or block.get("type") != block_type:
        raise _Unsupported("CONTENT_INVALID")
    return block


def _image(block: Mapping[str, Any]) -> None:
    value = block.get("image_url")
    if not isinstance(value, Mapping) or not isinstance(value.get("url"), str) or not value["url"]:
        raise _Unsupported("CURRENT_IMAGE_INVALID")


def _extract_qwen(
    request: Mapping[str, Any],
    claim_ranges: tuple[tuple[int, int, str | None], ...],
) -> tuple[tuple[FlatEditableSpan, ...], JsonPath, JsonPath, int]:
    messages = _messages(request)
    if len(messages) != 2:
        raise _Unsupported("QWEN_MESSAGE_SHAPE")
    system, user = _message(messages, 0), _message(messages, 1)
    if system.get("role") != "system" or user.get("role") != "user":
        raise _Unsupported("QWEN_ROLE_ORDER")
    system_blocks, user_blocks = _blocks(system), _blocks(user)
    if len(system_blocks) != 1 or len(user_blocks) != 2:
        raise _Unsupported("QWEN_CONTENT_SHAPE")
    system_text = _block(system_blocks, 0, "text").get("text")
    user_text = _block(user_blocks, 0, "text").get("text")
    image = _block(user_blocks, 1, "image_url")
    _image(image)
    if not isinstance(system_text, str) or not system_text:
        raise _Unsupported("QWEN_SYSTEM_TEXT")
    if not isinstance(user_text, str) or not user_text:
        raise _Unsupported("QWEN_USER_TEXT")
    if user_text.count(_QWEN_QUERY) != 1 or user_text.count(_QWEN_PROGRESS) != 1:
        raise _Unsupported("QWEN_MARKERS")
    query = user_text.index(_QWEN_QUERY)
    progress = user_text.index(_QWEN_PROGRESS)
    history_start = progress + len(_QWEN_PROGRESS)
    if query != 0 or progress <= len(_QWEN_QUERY) or not user_text.endswith("\n"):
        raise _Unsupported("QWEN_FRAME")
    if not claim_ranges and user_text[history_start:-1]:
        raise _Unsupported("QWEN_CLAIM_RANGE_MISMATCH")
    spans: list[FlatEditableSpan] = []
    path: JsonPath = ("messages", 1, "content", 0, "text")
    for ordinal, (start, end, source_step_event_id) in enumerate(claim_ranges, 1):
        label = f"Step {ordinal}: "
        label_start = start - len(label)
        if (
            start < history_start
            or end > len(user_text) - 1
            or label_start < history_start
            or user_text[label_start:start] != label
            or (ordinal == 1 and label_start != history_start)
            or (ordinal > 1 and user_text[label_start - 2 : label_start] != "; ")
        ):
            raise _Unsupported("QWEN_CLAIM_RANGE_MISMATCH")
        exact = user_text[start:end]
        if not exact:
            raise _Unsupported("QWEN_EMPTY_STEP")
        spans.append(
            FlatEditableSpan(
                target_id=f"qwen-step-{ordinal}",
                container_path=path,
                char_start=start,
                char_end=end,
                exact_text=exact,
                source_step_event_id=source_step_event_id,
            )
        )
    image_path = ("messages", 1, "content", 1)
    return tuple(spans), image_path, image_path[:-1], 1


def _extract_mai(
    request: Mapping[str, Any],
    source_step_event_ids: tuple[str | None, ...],
) -> tuple[tuple[FlatEditableSpan, ...], JsonPath | None, JsonPath, int]:
    messages = _messages(request)
    if len(messages) < 3:
        raise _Unsupported("MAI_MESSAGE_SHAPE")
    system, task, current = (
        _message(messages, 0),
        _message(messages, 1),
        _message(messages, len(messages) - 1),
    )
    if system.get("role") != "system" or task.get("role") != "user":
        raise _Unsupported("MAI_ROLE_ORDER")
    if not isinstance(system.get("content"), str) or not system["content"]:
        raise _Unsupported("MAI_SYSTEM_TEXT")
    task_blocks = _blocks(task)
    if len(task_blocks) != 1 or not isinstance(_block(task_blocks, 0, "text").get("text"), str):
        raise _Unsupported("MAI_TASK_TEXT")
    if current.get("role") != "user":
        raise _Unsupported("MAI_CURRENT_ROLE")
    current_blocks = _blocks(current)
    if len(current_blocks) != 1:
        raise _Unsupported("MAI_CURRENT_SHAPE")
    current_block = current_blocks[0]
    if not isinstance(current_block, Mapping):
        raise _Unsupported("MAI_CURRENT_SHAPE")
    current_kind = current_block.get("type")
    if current_kind == "image_url":
        _image(current_block)
        image_path: JsonPath | None = ("messages", len(messages) - 1, "content", 0)
    elif current_kind == "text":
        if not isinstance(current_block.get("text"), str) or not current_block["text"]:
            raise _Unsupported("MAI_CURRENT_SHAPE")
        image_path = None
    else:
        raise _Unsupported("MAI_CURRENT_SHAPE")

    spans: list[FlatEditableSpan] = []
    assistant_index = 0
    for message_index in range(2, len(messages) - 1):
        message = _message(messages, message_index)
        role = message.get("role")
        if role == "assistant":
            content = message.get("content")
            if not isinstance(content, str) or not content:
                raise _Unsupported("MAI_ASSISTANT_CONTENT")
            if assistant_index >= len(source_step_event_ids):
                raise _Unsupported("MAI_STEP_COUNT_MISMATCH")
            start, end = _mai_claim(content)
            spans.append(
                FlatEditableSpan(
                    target_id=f"mai-assistant-{message_index}",
                    container_path=("messages", message_index, "content"),
                    char_start=start,
                    char_end=end,
                    exact_text=content[start:end],
                    source_step_event_id=source_step_event_ids[assistant_index],
                )
            )
            assistant_index += 1
        elif role == "user":
            blocks = _blocks(message)
            if len(blocks) != 1 or not isinstance(blocks[0], Mapping):
                raise _Unsupported("MAI_HISTORY_OBSERVATION")
            kind = blocks[0].get("type")
            if kind == "image_url":
                _image(blocks[0])
            elif kind == "text":
                if not isinstance(blocks[0].get("text"), str) or not blocks[0]["text"]:
                    raise _Unsupported("MAI_HISTORY_OBSERVATION")
            else:
                raise _Unsupported("MAI_HISTORY_OBSERVATION")
        else:
            raise _Unsupported("MAI_HISTORY_ROLE")
    if assistant_index != len(source_step_event_ids):
        raise _Unsupported("MAI_STEP_COUNT_MISMATCH")
    insert_path: JsonPath = ("messages", len(messages) - 1, "content")
    return tuple(spans), image_path, insert_path, 0


def _history_source_step_ids(
    attributes: Mapping[str, Any] | None,
) -> tuple[str | None, ...]:
    if attributes is None:
        return ()
    count = attributes.get("history_step_count")
    value = attributes.get("history_source_step_event_ids")
    if type(count) is not int or not 0 <= count <= 512 or type(value) is not tuple:
        raise _Unsupported("HISTORY_SOURCE_STEPS_INVALID")
    if len(value) != count:
        raise _Unsupported("HISTORY_SOURCE_STEPS_INVALID")
    for source_id in value:
        if source_id is not None and (
            type(source_id) is not str or not source_id or len(source_id) > 128
        ):
            raise _Unsupported("HISTORY_SOURCE_STEPS_INVALID")
    return value


def _mai_claim(content: str) -> tuple[int, int]:
    canonical = content.count(_THINK_OPEN) == 1 and content.count(_THINK_CLOSE) == 1
    legacy = (
        _THINK_OPEN not in content
        and _THINK_CLOSE not in content
        and content.count(_LEGACY_THINK_CLOSE) == 1
    )
    if (
        not (canonical or legacy)
        or content.count(_TOOL_OPEN) != 1
        or content.count(_TOOL_CLOSE) != 1
    ):
        raise _Unsupported("MAI_WRAPPER")
    tool_open, tool_close = content.index(_TOOL_OPEN), content.index(_TOOL_CLOSE)
    if canonical:
        think_open, think_close = content.index(_THINK_OPEN), content.index(_THINK_CLOSE)
        start = think_open + len(_THINK_OPEN)
        close_end = think_close + len(_THINK_CLOSE)
        if content[:think_open].strip():
            raise _Unsupported("MAI_WRAPPER_ORDER")
    else:
        think_close = content.index(_LEGACY_THINK_CLOSE)
        start = 0
        close_end = think_close + len(_LEGACY_THINK_CLOSE)
    if (
        not start <= think_close < tool_open < tool_close
        or content[close_end:tool_open].strip()
        or content[tool_close + len(_TOOL_CLOSE) :].strip()
    ):
        raise _Unsupported("MAI_WRAPPER_ORDER")
    try:
        tool = json.loads(content[tool_open + len(_TOOL_OPEN) : tool_close].strip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise _Unsupported("MAI_TOOL_WRAPPER") from exc
    if not isinstance(tool, dict):
        raise _Unsupported("MAI_TOOL_WRAPPER")
    inner = content[start:think_close]
    start += len(inner) - len(inner.lstrip())
    end = think_close - (len(inner) - len(inner.rstrip()))
    marker = re.match(r"Thought\s*:\s*", content[start:end])
    if marker is not None:
        start += marker.end()
    if start >= end:
        raise _Unsupported("MAI_EMPTY_REASONING")
    return start, end


__all__ = [
    "FlatCodecRegistry",
    "FlatEditableSpan",
    "FlatHistory",
    "FlatHistoryAdapter",
    "FlatHistoryStatus",
    "MAI_CODEC_ID",
    "MAI_HOST_ID",
    "MaiRawReplayAdapter",
    "QWEN_CODEC_ID",
    "QWEN_HOST_ID",
    "QWEN_SOURCE_BOUND_HISTORY_ENCODING",
    "QwenFlatProgressAdapter",
    "build_flat_codec_registry",
]
