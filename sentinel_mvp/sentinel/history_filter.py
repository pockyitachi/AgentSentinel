"""Conservative claim/span-level rendering of an active history view."""

from __future__ import annotations

import copy
from dataclasses import is_dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    Claim,
    EpistemicStatus,
    GateOperation,
    PromptOperation,
    SentinelOutput,
    StepInput,
    Verdict,
)


MASK_MARKER = "[SENTINEL: directly refuted claim removed]"
REPLACE_MARKER = "[SENTINEL: directly refuted claim removed; see correction block]"
ARCHIVE_MARKER = "[SENTINEL: inactive-branch detail archived]"
CORRECTION_HEADER = "[SENTINEL CORRECTIONS]"
CORRECTION_FOOTER = "[/SENTINEL CORRECTIONS]"

_ID_FALLBACKS = ("id", "record_id", "step_id", "target_step_id")
_TEXT_FALLBACKS = ("text", "content", "response", "prediction")
_EDITING_OPERATIONS = {
    GateOperation.DROP,
    GateOperation.REPLACE,
    GateOperation.ARCHIVE,
}


def _record_value(record: Any, names: Sequence[str]) -> tuple[str | None, Any]:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return name, record[name]
        return None, None
    for name in names:
        if hasattr(record, name):
            return name, getattr(record, name)
    return None, None


def _record_id(record: Any, index: int, record_id_field: str) -> str:
    if isinstance(record, str):
        return str(index)
    _, value = _record_value(record, (record_id_field, *_ID_FALLBACKS))
    return str(index if value is None else value)


def _record_text(record: Any, text_field: str) -> tuple[str | None, str | None]:
    if isinstance(record, str):
        return None, record
    name, value = _record_value(record, (text_field, *_TEXT_FALLBACKS))
    return name, value if isinstance(value, str) else None


def _with_text(record: Any, field_name: str | None, text: str) -> Any:
    """Return a shallow, non-mutating copy of ``record`` with new text."""

    if isinstance(record, str):
        return text
    if field_name is None:
        raise TypeError("history record has no writable text field")
    if isinstance(record, Mapping):
        updated = dict(record)
        updated[field_name] = text
        return updated
    if is_dataclass(record):
        return replace(record, **{field_name: text})
    updated = copy.copy(record)
    setattr(updated, field_name, text)
    return updated


def _keep_uncertain(claim: Claim, original_text: str, reason: str) -> PromptOperation:
    return PromptOperation(
        record_id=claim.record_id,
        claim_id=claim.claim_id,
        source_span=claim.source_span,
        operation=GateOperation.KEEP_UNCERTAIN,
        original_text=original_text,
        evidence_refs=claim.evidence_refs,
        epistemic_status=claim.epistemic_status,
        reason=reason,
        applied=False,
        requested_verdict=claim.verdict,
    )


def _operation_for_claim(claim: Claim, original_text: str) -> PromptOperation:
    """Apply the safety policy and return the *effective* operation."""

    requested = claim.gate_operation

    if requested is GateOperation.KEEP:
        return PromptOperation(
            record_id=claim.record_id,
            claim_id=claim.claim_id,
            source_span=claim.source_span,
            operation=GateOperation.KEEP,
            original_text=original_text,
            evidence_refs=claim.evidence_refs,
            epistemic_status=claim.epistemic_status,
            reason=claim.rationale or "claim retained",
            applied=False,
            requested_verdict=claim.verdict,
        )

    if requested is GateOperation.KEEP_UNCERTAIN:
        return _keep_uncertain(
            claim,
            original_text,
            claim.rationale or "evidence is insufficient; original text preserved",
        )

    if requested in {GateOperation.DROP, GateOperation.REPLACE}:
        if claim.epistemic_status is not EpistemicStatus.REFUTED:
            return _keep_uncertain(
                claim,
                original_text,
                "destructive factual edit rejected: claim is not REFUTED",
            )
        if not claim.has_direct_evidence:
            return _keep_uncertain(
                claim,
                original_text,
                "destructive factual edit rejected: no direct evidence",
            )

    if requested is GateOperation.REPLACE and not (claim.correction or "").strip():
        return _keep_uncertain(
            claim,
            original_text,
            "replacement rejected: no evidence-grounded correction was provided",
        )

    replacement_text: str | None
    if requested is GateOperation.DROP:
        replacement_text = MASK_MARKER
    elif requested is GateOperation.REPLACE:
        # Keep the verified correction on the operation/correction block, not
        # inside the old assistant record.  Host adapters are the only prompt
        # renderers and can attach it as Sentinel-authored context.
        replacement_text = claim.correction.strip() if claim.correction else None
    elif requested is GateOperation.ARCHIVE:
        replacement_text = ARCHIVE_MARKER
    else:  # Defensive guard if the enum grows.
        return _keep_uncertain(claim, original_text, "unsupported operation")

    return PromptOperation(
        record_id=claim.record_id,
        claim_id=claim.claim_id,
        source_span=claim.source_span,
        operation=requested,
        original_text=original_text,
        replacement_text=replacement_text,
        evidence_refs=claim.evidence_refs,
        epistemic_status=claim.epistemic_status,
        reason=claim.rationale,
        applied=True,
        requested_verdict=claim.verdict,
    )


def _overlap(first: PromptOperation, second: PromptOperation) -> bool:
    return first.start < second.end and second.start < first.end


def _downgrade_overlaps(
    operations: list[PromptOperation], warnings: list[str]
) -> list[PromptOperation]:
    """Preserve original text whenever material edits overlap.

    Applying one overlapping edit would change the source offsets of another.
    More importantly, competing claim boundaries usually signal parser
    ambiguity.  Conservatively abstain from *all* edits in that overlap set.
    """

    conflicts: set[int] = set()
    by_record: dict[str, list[tuple[int, PromptOperation]]] = {}
    for index, operation in enumerate(operations):
        if operation.applied and operation.operation in _EDITING_OPERATIONS:
            by_record.setdefault(operation.record_id, []).append((index, operation))

    for record_operations in by_record.values():
        for left_index, (operation_index, operation) in enumerate(record_operations):
            for other_index, other in record_operations[left_index + 1 :]:
                if _overlap(operation, other):
                    conflicts.update((operation_index, other_index))

    for index in sorted(conflicts):
        operation = operations[index]
        warnings.append(
            f"{operation.claim_id}: overlapping material edits; preserved original text"
        )
        operations[index] = PromptOperation(
            record_id=operation.record_id,
            claim_id=operation.claim_id,
            source_span=operation.source_span,
            operation=GateOperation.KEEP_UNCERTAIN,
            original_text=operation.original_text,
            evidence_refs=operation.evidence_refs,
            epistemic_status=operation.epistemic_status,
            reason="overlapping claim spans; conservative abstention",
            applied=False,
            requested_verdict=operation.requested_verdict,
        )
    return operations


def render_correction_block(operations: Iterable[PromptOperation]) -> str:
    """Render only evidence-grounded interventions, without repeating bad text.

    The original, potentially misleading claim remains available in the
    sidecar through ``PromptOperation.original_text`` but is intentionally not
    echoed into the actor prompt.
    """

    lines: list[str] = []
    for operation in operations:
        if not operation.applied:
            continue
        evidence_ids = ", ".join(
            evidence.evidence_id
            for evidence in operation.evidence_refs
            if evidence.direct
        )
        evidence_suffix = f" Evidence: {evidence_ids}." if evidence_ids else ""
        location = f"record {operation.record_id}, claim {operation.claim_id}"
        if operation.operation is GateOperation.REPLACE:
            lines.append(
                f"- {location}: use verified correction: "
                f"{operation.replacement_text}.{evidence_suffix}"
            )
        elif operation.operation is GateOperation.DROP:
            lines.append(
                f"- {location}: a directly refuted historical claim was removed; "
                f"do not use it as a premise.{evidence_suffix}"
            )

    if not lines:
        return ""
    return "\n".join((CORRECTION_HEADER, *lines, CORRECTION_FOOTER))


def filter_history(
    history: Sequence[Any],
    claims: Iterable[Claim],
    *,
    task_id: str | None = None,
    step_index: int | None = None,
    record_id_field: str = "id",
    text_field: str = "text",
) -> SentinelOutput:
    """Create a filtered history while preserving record order and structure.

    ``history`` may contain strings, mappings, dataclass instances, or simple
    objects.  For mappings/objects, the function first tries the configured
    ``id``/``text`` fields and then common adapter aliases.  It never mutates
    the input records.
    """

    source_history = list(history)
    claims_list = list(claims)
    filtered_history = list(source_history)
    warnings: list[str] = []
    operations: list[PromptOperation] = []

    record_locations: dict[str, list[int]] = {}
    record_text: dict[int, tuple[str | None, str | None]] = {}
    for index, record in enumerate(source_history):
        record_id = _record_id(record, index, record_id_field)
        record_locations.setdefault(record_id, []).append(index)
        record_text[index] = _record_text(record, text_field)

    for claim in claims_list:
        locations = record_locations.get(claim.record_id, [])
        if len(locations) != 1:
            reason = (
                "target record was not found"
                if not locations
                else "target record id is ambiguous"
            )
            warnings.append(f"{claim.claim_id}: {reason}; preserved original history")
            operations.append(_keep_uncertain(claim, claim.text, reason))
            continue

        record_index = locations[0]
        _, text = record_text[record_index]
        if text is None:
            reason = "target record has no string text field"
            warnings.append(f"{claim.claim_id}: {reason}; preserved original history")
            operations.append(_keep_uncertain(claim, claim.text, reason))
            continue
        if claim.end > len(text):
            reason = "claim span is outside the original record"
            warnings.append(f"{claim.claim_id}: {reason}; preserved original text")
            operations.append(_keep_uncertain(claim, claim.text, reason))
            continue

        original_text = text[claim.start : claim.end]
        if original_text != claim.text:
            reason = "claim text does not match its source span"
            warnings.append(f"{claim.claim_id}: {reason}; preserved original text")
            operations.append(_keep_uncertain(claim, original_text, reason))
            continue

        operation = _operation_for_claim(claim, original_text)
        if operation.operation is GateOperation.KEEP_UNCERTAIN and (
            claim.verdict is not Verdict.ABSTAIN
        ):
            warnings.append(f"{claim.claim_id}: {operation.reason}")
        operations.append(operation)

    operations = _downgrade_overlaps(operations, warnings)

    operations_by_record: dict[str, list[PromptOperation]] = {}
    for operation in operations:
        if operation.applied:
            operations_by_record.setdefault(operation.record_id, []).append(operation)

    for record_id, record_operations in operations_by_record.items():
        record_index = record_locations[record_id][0]
        field_name, text = record_text[record_index]
        assert text is not None  # Applied operations necessarily passed validation.
        rendered = text
        for operation in sorted(record_operations, key=lambda item: item.start, reverse=True):
            replacement_text = (
                REPLACE_MARKER
                if operation.operation is GateOperation.REPLACE
                else operation.replacement_text or ""
            )
            rendered = rendered[: operation.start] + replacement_text + rendered[operation.end :]
        filtered_history[record_index] = _with_text(
            source_history[record_index], field_name, rendered
        )

    correction_block = render_correction_block(operations)
    return SentinelOutput(
        task_id=task_id,
        step_index=step_index,
        claims=claims_list,
        operations=operations,
        filtered_history=filtered_history,
        correction_block=correction_block,
        warnings=warnings,
    )


class HistoryFilter:
    """Small state-free facade suitable for middleware dependency injection."""

    def __init__(self, *, record_id_field: str = "id", text_field: str = "text") -> None:
        self.record_id_field = record_id_field
        self.text_field = text_field

    def filter(
        self,
        step: StepInput,
        claims: Iterable[Claim] | None = None,
    ) -> SentinelOutput:
        """Filter ``step.history`` using supplied claims or ``step.claims``."""

        return filter_history(
            step.history,
            step.claims if claims is None else claims,
            task_id=step.task_id,
            step_index=step.step_index,
            record_id_field=self.record_id_field,
            text_field=self.text_field,
        )
