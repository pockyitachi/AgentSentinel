"""Seed history adapter for the Sentinel MVP.

The MobileWorld ``SeedAgent`` stores every previous model response as a raw
string.  Immediately before inference it splits a response of the form
``<think>reasoning</think>content`` into an OpenAI-compatible assistant
message, while retaining only the latest ``history_n`` *image* observation
messages.  Text responses are not windowed.

This module implements the same history rendering boundary without importing
MobileWorld.  It deliberately creates a derived prompt view and never mutates
the caller's responses, observation references, or Sentinel operations.

Canonical operation names match the proposal:

``KEEP``
    Preserve the selected history.
``DROP``
    Remove a directly-refuted record/span from active history.
``REPLACE``
    Remove a directly-refuted record/span and put the verified replacement in
    a Sentinel-authored user correction block.  We do not rewrite the old
    assistant message as though the actor originally said the correction.
``ARCHIVE``
    Remove a true but inactive-branch record/span from active history.
``KEEP_UNCERTAIN``
    Preserve the text and tell the actor that it remains unverified.

For compatibility with early analysis artifacts, the adapter accepts
``MASK/CORRECT/LOW_RELEVANCE/ABSTAIN`` as input aliases.  All emitted operation
results use the canonical names above.

Span coordinates are offsets into the *raw Seed response*, including its
``<think>`` wrapper.  :func:`extract_seed_records` exposes exactly this text to
the generic Sentinel core, avoiding an ambiguous reasoning-vs-content offset
space.  A destructive ``DROP`` or ``REPLACE`` is applied only when an evidence
item explicitly carries ``direct=True``.  Invalid or mismatched spans fail
closed to ``KEEP_UNCERTAIN``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any, Literal, Protocol, TypedDict, cast


CanonicalOperation = Literal[
    "KEEP", "DROP", "REPLACE", "ARCHIVE", "KEEP_UNCERTAIN"
]

_OPERATION_ALIASES: dict[str, CanonicalOperation] = {
    "KEEP": "KEEP",
    "DROP": "DROP",
    "MASK": "DROP",
    "REPLACE": "REPLACE",
    "CORRECT": "REPLACE",
    "ARCHIVE": "ARCHIVE",
    "LOW_RELEVANCE": "ARCHIVE",
    "KEEP_UNCERTAIN": "KEEP_UNCERTAIN",
    "ABSTAIN": "KEEP_UNCERTAIN",
}


class SeedOperationDict(TypedDict, total=False):
    """Mapping form accepted from the Sentinel core or an offline replay."""

    record_id: str
    target_step_id: str
    step_id: str
    claim_id: str
    operation: str
    verdict: str
    start: int | None
    end: int | None
    original_text: str
    replacement_text: str | None
    replacement: str | None
    correction: str | None
    rationale: str
    evidence: Sequence[Any]
    evidence_refs: Sequence[Any]
    epistemic_status: str


class OperationLike(Protocol):
    """Structural interface for dataclass-style Sentinel operations."""

    record_id: str
    verdict: Any
    start: int | None
    end: int | None
    original_text: str
    replacement_text: str | None
    rationale: str
    evidence: Sequence[Any]


@dataclass(frozen=True)
class SeedAssistantStep:
    """One filtered history response plus stable sidecar identity."""

    step_id: str
    source_index: int
    raw_response: str
    reasoning_content: str
    content: str

    def actor_message(self) -> dict[str, Any]:
        """Return a protocol-valid Seed/OpenAI assistant message.

        ``step_id`` intentionally stays in this wrapper rather than being sent
        as an unknown top-level chat-message field.
        """

        return {
            "role": "assistant",
            "content": self.content,
            "reasoning_content": self.reasoning_content,
        }


@dataclass(frozen=True)
class AppliedOperation:
    """Auditable result of rendering one canonical operation."""

    record_id: str
    claim_id: str | None
    operation: CanonicalOperation
    applied: bool
    detail: str


@dataclass(frozen=True)
class SeedAdapterOutput:
    """Derived prompt view returned to a Seed-style actor."""

    filtered_assistant_history: tuple[SeedAssistantStep, ...]
    correction_user_block: dict[str, Any] | None
    retained_observation_refs: tuple[Any, ...]
    actor_messages: tuple[dict[str, Any], ...]
    operation_results: tuple[AppliedOperation, ...]

    @property
    def filtered_history_responses(self) -> tuple[str, ...]:
        """Raw-response representation suitable for ``history_responses``."""

        return tuple(step.raw_response for step in self.filtered_assistant_history)


@dataclass(frozen=True)
class _SourceRecord:
    step_id: str
    source_index: int
    raw_response: str


@dataclass(frozen=True)
class _NormalizedOperation:
    record_id: str
    claim_id: str | None
    operation: CanonicalOperation
    start: int | None
    end: int | None
    original_text: str | None
    replacement_text: str | None
    rationale: str
    evidence: tuple[Any, ...]
    epistemic_status: str


@dataclass(frozen=True)
class _Correction:
    record_id: str
    replacement_text: str
    evidence_refs: tuple[str, ...]


def _read(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _string_enum(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    return str(value).strip().upper()


def _canonical_operation(value: Any) -> CanonicalOperation:
    # An unknown verdict must not accidentally remove history.
    return _OPERATION_ALIASES.get(_string_enum(value), "KEEP_UNCERTAIN")


def _record_id(value: Any, fallback: str) -> str:
    candidate = _read(value, "record_id", "target_step_id", "step_id", "id")
    return str(candidate) if candidate is not None else fallback


def _raw_response(value: Any) -> str:
    if isinstance(value, str):
        return value
    raw = _read(value, "raw_response", "response", "text")
    if raw is not None:
        return str(raw)
    reasoning = str(_read(value, "reasoning_content", "reasoning", default="") or "")
    content = str(_read(value, "content", default="") or "")
    if reasoning:
        return f"<think>{reasoning}</think>{content}"
    return content


def _source_records(history_responses: Sequence[Any]) -> tuple[_SourceRecord, ...]:
    records: list[_SourceRecord] = []
    for index, response in enumerate(history_responses):
        records.append(
            _SourceRecord(
                step_id=_record_id(response, f"R{index + 1}"),
                source_index=index,
                raw_response=_raw_response(response),
            )
        )
    return tuple(records)


def extract_seed_records(history_responses: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """Expose stable generic records for claim parsing and ``filter_history``.

    The ``text`` field is deliberately the complete raw response.  Therefore a
    claim's ``start``/``end`` offsets can be passed back to this adapter without
    losing whether the claim came from reasoning or visible content.
    """

    return tuple(
        {
            "id": record.step_id,
            "step_id": record.step_id,
            "source_index": record.source_index,
            "text": record.raw_response,
        }
        for record in _source_records(history_responses)
    )


def _normalize_operation(value: Any) -> _NormalizedOperation:
    verdict = _read(value, "operation", "verdict", default="KEEP_UNCERTAIN")
    replacement = _read(
        value,
        "replacement_text",
        "replacement",
        "correction",
        default=None,
    )
    # ``PromptOperation`` from the generic Sentinel core exposes
    # ``evidence_refs``; early replay artifacts used ``evidence``.  Accept both
    # so the core output can be rendered by this host adapter directly.
    evidence = _read(value, "evidence_refs", "evidence", default=()) or ()
    if isinstance(evidence, (str, bytes, Mapping)):
        evidence = (evidence,)
    return _NormalizedOperation(
        record_id=_record_id(value, ""),
        claim_id=(
            str(_read(value, "claim_id"))
            if _read(value, "claim_id") is not None
            else None
        ),
        operation=_canonical_operation(verdict),
        start=_optional_int(_read(value, "start", default=None)),
        end=_optional_int(_read(value, "end", default=None)),
        original_text=(
            str(_read(value, "original_text"))
            if _read(value, "original_text") is not None
            else None
        ),
        replacement_text=str(replacement) if replacement is not None else None,
        rationale=str(_read(value, "rationale", "reason", default="") or ""),
        evidence=tuple(evidence),
        epistemic_status=_string_enum(
            _read(value, "epistemic_status", default="UNVERIFIABLE")
        ),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_direct_evidence_item(evidence: Any) -> bool:
    direct = _read(evidence, "direct", "is_direct", default=False)
    return direct is True


def _has_direct_evidence(operation: _NormalizedOperation) -> bool:
    return any(_is_direct_evidence_item(item) for item in operation.evidence)


def _evidence_ref(evidence: Any) -> str | None:
    if isinstance(evidence, str):
        return evidence
    ref = _read(
        evidence,
        "ref",
        "evidence_ref",
        "evidence_id",
        "locator",
        "id",
        "source",
        default=None,
    )
    return str(ref) if ref is not None else None


def _resolve_span(
    raw: str, operation: _NormalizedOperation
) -> tuple[int, int] | None:
    """Resolve a claimed span without silently deleting the wrong text."""

    start, end = operation.start, operation.end
    original = operation.original_text

    if start is None and end is None:
        return (0, len(raw))
    if start is not None and end is not None and 0 <= start <= end <= len(raw):
        if original is None or raw[start:end] == original:
            return (start, end)

    # Offline annotations can move when a response serialization changes.  A
    # unique exact source string is a safe recovery; an ambiguous match is not.
    if original:
        first = raw.find(original)
        if first >= 0 and raw.find(original, first + 1) < 0:
            return (first, first + len(original))
    return None


def _split_seed_response(raw_response: str) -> tuple[str, str]:
    """Mirror MobileWorld SeedAgent's ``</think>`` splitting semantics."""

    parts = raw_response.split("</think>")
    if len(parts) == 1:
        return "", raw_response
    reasoning = parts[0].replace("<think>", "")
    return reasoning, parts[-1]


def _remove_spans(raw: str, spans: Sequence[tuple[int, int]]) -> str:
    result = raw
    for start, end in sorted(spans, reverse=True):
        result = result[:start] + result[end:]
    return result


def _operation_for_missing_record(operation: _NormalizedOperation) -> AppliedOperation:
    return AppliedOperation(
        record_id=operation.record_id,
        claim_id=operation.claim_id,
        operation="KEEP_UNCERTAIN",
        applied=False,
        detail="target record was not present; no history was changed",
    )


def _render_records(
    records: Sequence[_SourceRecord], operations: Sequence[Any]
) -> tuple[
    tuple[SeedAssistantStep, ...],
    tuple[AppliedOperation, ...],
    tuple[_Correction, ...],
    tuple[str, ...],
]:
    normalized = tuple(_normalize_operation(operation) for operation in operations)
    by_record: dict[str, list[_NormalizedOperation]] = {}
    results: list[AppliedOperation] = []
    known_ids = {record.step_id for record in records}
    for operation in normalized:
        if operation.record_id not in known_ids:
            results.append(_operation_for_missing_record(operation))
            continue
        by_record.setdefault(operation.record_id, []).append(operation)

    filtered: list[SeedAssistantStep] = []
    corrections: list[_Correction] = []
    uncertain_ids: list[str] = []

    for record in records:
        spans_to_remove: list[tuple[int, int]] = []
        record_operations = by_record.get(record.step_id, [])
        for operation in record_operations:
            canonical = operation.operation
            if canonical == "KEEP":
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP",
                        True,
                        "history preserved",
                    )
                )
                continue

            if canonical == "KEEP_UNCERTAIN":
                uncertain_ids.append(record.step_id)
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP_UNCERTAIN",
                        True,
                        "history preserved and marked unverified",
                    )
                )
                continue

            if canonical in {"DROP", "REPLACE"} and (
                operation.epistemic_status != "REFUTED"
            ):
                uncertain_ids.append(record.step_id)
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP_UNCERTAIN",
                        False,
                        f"{canonical} was not authorized by a REFUTED evidence status; history preserved",
                    )
                )
                continue

            if canonical in {"DROP", "REPLACE"} and not _has_direct_evidence(operation):
                uncertain_ids.append(record.step_id)
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP_UNCERTAIN",
                        False,
                        f"{canonical} lacked evidence with direct=True; history preserved",
                    )
                )
                continue

            if canonical == "REPLACE" and not operation.replacement_text:
                uncertain_ids.append(record.step_id)
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP_UNCERTAIN",
                        False,
                        "REPLACE lacked replacement_text; history preserved",
                    )
                )
                continue

            span = _resolve_span(record.raw_response, operation)
            if span is None:
                uncertain_ids.append(record.step_id)
                results.append(
                    AppliedOperation(
                        record.step_id,
                        operation.claim_id,
                        "KEEP_UNCERTAIN",
                        False,
                        f"{canonical} span did not match source text; history preserved",
                    )
                )
                continue

            # Overlap is harmless because every active operation removes text.
            # Merge later before editing so source coordinates remain stable.
            spans_to_remove.append(span)
            results.append(
                AppliedOperation(
                    record.step_id,
                    operation.claim_id,
                    canonical,
                    True,
                    (
                        "source span removed; verified replacement emitted separately"
                        if canonical == "REPLACE"
                        else "source span removed from active history"
                    ),
                )
            )
            if canonical == "REPLACE":
                refs = tuple(
                    ref
                    for ref in (_evidence_ref(item) for item in operation.evidence)
                    if ref
                )
                corrections.append(
                    _Correction(
                        record_id=record.step_id,
                        replacement_text=cast(str, operation.replacement_text),
                        evidence_refs=refs,
                    )
                )

        filtered_raw = _remove_spans(record.raw_response, _merge_spans(spans_to_remove))
        reasoning, content = _split_seed_response(filtered_raw)
        # If a whole record was removed, omit its assistant message.  Its step
        # identity remains auditable in ``operation_results``.
        if filtered_raw:
            filtered.append(
                SeedAssistantStep(
                    step_id=record.step_id,
                    source_index=record.source_index,
                    raw_response=filtered_raw,
                    reasoning_content=reasoning,
                    content=content,
                )
            )

    return (
        tuple(filtered),
        tuple(results),
        tuple(corrections),
        tuple(dict.fromkeys(uncertain_ids)),
    )


def _merge_spans(spans: Sequence[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    if not spans:
        return ()
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


def _correction_block(
    corrections: Sequence[_Correction], uncertain_ids: Sequence[str]
) -> dict[str, Any] | None:
    if not corrections and not uncertain_ids:
        return None
    lines = ["<sentinel_history_gate>"]
    if corrections:
        lines.append("Verified history corrections for this decision:")
        for correction in corrections:
            evidence = (
                f" (evidence: {', '.join(correction.evidence_refs)})"
                if correction.evidence_refs
                else ""
            )
            lines.append(
                f"- [{correction.record_id}] {correction.replacement_text}{evidence}"
            )
    if uncertain_ids:
        lines.append("History retained but not verified:")
        for record_id in dict.fromkeys(uncertain_ids):
            lines.append(
                f"- [{record_id}] UNVERIFIED: do not treat this record as established fact."
            )
    lines.append("</sentinel_history_gate>")
    return {"type": "text", "text": "\n".join(lines)}


def _is_image_message(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    return bool(
        message.get("role") == "user"
        and isinstance(content, list)
        and content
        and isinstance(content[0], Mapping)
        and content[0].get("type") == "image_url"
    )


def _as_data_url(image_b64_or_url: Any, *, definitely_base64: bool = False) -> str:
    value = str(image_b64_or_url)
    if definitely_base64 and not value.startswith("data:"):
        return f"data:image/png;base64,{value}"
    return value


def _observation_message(reference: Any) -> dict[str, Any] | None:
    if reference is None:
        return None

    # SeedAgent's native tuple is (image_b64, tool_call_result,
    # ask_user_response).  Supporting it here does not require importing QR-MW.
    if isinstance(reference, tuple) and len(reference) == 3:
        image_b64, tool_result, ask_user_response = reference
        if tool_result is not None:
            rendered = (
                json.dumps(tool_result, ensure_ascii=False)
                if isinstance(tool_result, (dict, list))
                else str(tool_result)
            )
            return {
                "role": "user",
                "content": [{"type": "text", "text": f"Tool call result: {rendered}"}],
            }
        if ask_user_response is not None:
            return {
                "role": "user",
                "content": [{"type": "text", "text": str(ask_user_response)}],
            }
        return {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _as_data_url(image_b64, definitely_base64=True)
                    },
                }
            ],
        }

    if isinstance(reference, Mapping):
        ready_message = reference.get("message")
        if isinstance(ready_message, Mapping):
            return deepcopy(dict(ready_message))
        if "role" in reference and "content" in reference:
            return deepcopy(dict(reference))

        tool_result = _read(reference, "tool_call_result", "tool_call", default=None)
        if tool_result is not None:
            rendered = (
                json.dumps(tool_result, ensure_ascii=False)
                if isinstance(tool_result, (dict, list))
                else str(tool_result)
            )
            return {
                "role": "user",
                "content": [{"type": "text", "text": f"Tool call result: {rendered}"}],
            }
        ask_user_response = _read(reference, "ask_user_response", default=None)
        if ask_user_response is not None:
            return {
                "role": "user",
                "content": [{"type": "text", "text": str(ask_user_response)}],
            }

        if "image_b64" in reference:
            url = _as_data_url(reference["image_b64"], definitely_base64=True)
        else:
            url_value = _read(
                reference,
                "image_url",
                "url",
                "screenshot_ref",
                "ref",
                default=None,
            )
            if isinstance(url_value, Mapping):
                url_value = url_value.get("url")
            if url_value is None:
                # Preserve an opaque sidecar reference without inventing image
                # bytes.  A host can resolve it before calling the model.
                return {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": f"Observation reference: {json.dumps(dict(reference), ensure_ascii=False, default=str)}",
                        }
                    ],
                }
            url = _as_data_url(url_value)
        return {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": url}}],
        }

    return {
        "role": "user",
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": _as_data_url(reference)},
            }
        ],
    }


def _limit_seed_images(
    messages: Sequence[dict[str, Any]], history_n: int
) -> tuple[dict[str, Any], ...]:
    retained_reversed: list[dict[str, Any]] = []
    image_count = 0
    for message in reversed(messages):
        if _is_image_message(message):
            image_count += 1
            if image_count > history_n:
                # A Sentinel correction may share the current user message
                # with the image.  Drop only image blocks; never drop the
                # correction merely because the host configured history_n=0.
                content = message.get("content")
                if isinstance(content, list):
                    non_image_blocks = [
                        deepcopy(block)
                        for block in content
                        if not (
                            isinstance(block, Mapping)
                            and block.get("type") == "image_url"
                        )
                    ]
                    if non_image_blocks:
                        retained_reversed.append(
                            {**deepcopy(message), "content": non_image_blocks}
                        )
                continue
        retained_reversed.append(message)
    return tuple(reversed(retained_reversed))


def _attach_correction(
    current_message: dict[str, Any] | None,
    correction: dict[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    if correction is None:
        return (current_message,) if current_message is not None else ()
    if current_message is None:
        return ({"role": "user", "content": [deepcopy(correction)]},)

    message = deepcopy(current_message)
    content = message.get("content")
    if isinstance(content, list):
        content.append(deepcopy(correction))
    elif content is None:
        message["content"] = [deepcopy(correction)]
    else:
        message["content"] = [
            {"type": "text", "text": str(content)},
            deepcopy(correction),
        ]
    return (message,)


def adapt_seed_history(
    history_responses: Sequence[Any],
    historical_observation_refs: Sequence[Any] = (),
    current_observation_ref: Any | None = None,
    operations: Sequence[Any] = (),
    *,
    history_n: int = 3,
) -> SeedAdapterOutput:
    """Create the filtered next-turn history for a Seed-style GUI actor.

    ``historical_observation_refs[i]`` is the observation immediately before
    ``history_responses[i]``.  ``current_observation_ref`` is the GUI/tool/user
    observation for the next action.  Observation values can be ready chat
    messages, ``{"image_b64": ...}``, image URL/reference mappings, bare URLs,
    or Seed's native three-tuple.  Opaque references are preserved in
    ``retained_observation_refs`` even if the host still needs to resolve them.

    The returned ``actor_messages`` contain history/observation messages only;
    the host remains responsible for prepending its system prompts and task.
    """

    if history_n < 0:
        raise ValueError("history_n must be non-negative")

    # Snapshot caller-owned containers before any rendering.  ``deepcopy`` is
    # also what guarantees that attaching the correction cannot mutate a ready
    # current-observation message supplied by the host.
    history_snapshot = deepcopy(tuple(history_responses))
    observations_snapshot = deepcopy(tuple(historical_observation_refs))
    current_snapshot = deepcopy(current_observation_ref)
    operations_snapshot = deepcopy(tuple(operations))

    records = _source_records(history_snapshot)
    filtered, operation_results, corrections, uncertain_ids = _render_records(
        records, operations_snapshot
    )
    correction = _correction_block(corrections, uncertain_ids)

    filtered_by_index = {step.source_index: step for step in filtered}
    messages: list[dict[str, Any]] = []
    used_observation_refs: list[Any] = []
    for index, _record in enumerate(records):
        if index < len(observations_snapshot):
            obs_message = _observation_message(observations_snapshot[index])
            if obs_message is not None:
                messages.append(obs_message)
                used_observation_refs.append(observations_snapshot[index])
        step = filtered_by_index.get(index)
        if step is not None:
            messages.append(step.actor_message())

    current_message = _observation_message(current_snapshot)
    messages.extend(_attach_correction(current_message, correction))
    if current_snapshot is not None:
        used_observation_refs.append(current_snapshot)

    limited_messages = _limit_seed_images(messages, history_n)

    # Derive retained references from the same image-window rule.  Non-image
    # refs remain available, exactly as Seed keeps tool/user-result messages.
    retained_refs_reversed: list[Any] = []
    image_count = 0
    for reference in reversed(used_observation_refs):
        message = _observation_message(reference)
        if message is not None and _is_image_message(message):
            image_count += 1
            if image_count > history_n:
                continue
        retained_refs_reversed.append(deepcopy(reference))

    return SeedAdapterOutput(
        filtered_assistant_history=filtered,
        correction_user_block=deepcopy(correction),
        retained_observation_refs=tuple(reversed(retained_refs_reversed)),
        actor_messages=tuple(deepcopy(limited_messages)),
        operation_results=operation_results,
    )


class SeedHistoryAdapter:
    """Small state-free façade convenient for middleware registration."""

    adapter_id = "mobileworld.seed"

    @staticmethod
    def extract(history_responses: Sequence[Any]) -> tuple[dict[str, Any], ...]:
        return extract_seed_records(history_responses)

    @staticmethod
    def render(
        history_responses: Sequence[Any],
        historical_observation_refs: Sequence[Any] = (),
        current_observation_ref: Any | None = None,
        operations: Sequence[Any] = (),
        *,
        history_n: int = 3,
    ) -> SeedAdapterOutput:
        return adapt_seed_history(
            history_responses,
            historical_observation_refs,
            current_observation_ref,
            operations,
            history_n=history_n,
        )


__all__ = [
    "AppliedOperation",
    "CanonicalOperation",
    "OperationLike",
    "SeedAdapterOutput",
    "SeedAssistantStep",
    "SeedHistoryAdapter",
    "SeedOperationDict",
    "adapt_seed_history",
    "extract_seed_records",
]
