"""Frozen prompts for the two-phase failure-link Codex review.

The prompts deliberately preserve the distinction between observed failure
linkage and counterfactual causation.  Phase A is outcome blind.  Phase B may
use only the outcome and evaluator evidence already embedded in its validated
card.  Neither phase may relabel the frozen strict-MHR determination.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from mobile_world.offline.motivation_review import canonical_json_bytes

LEGACY_PROMPT_VERSION = "mobileworld.audit.failure-link-codex-prompt/v3"
PROMPT_VERSION = "mobileworld.audit.failure-link-codex-prompt/v4"
INLINE_CARD_TRANSPORT_ENCODING = "mobileworld.audit.failure-link-card-inline-canonical-json/v1"
ASSISTANT_EXPOSURES_TRANSPORT_ENCODING = (
    "mobileworld.audit.failure-link-assistant-exposures-columnar-dictionary/v1"
)
LARGE_CARD_ENCODING_THRESHOLD_BYTES = 512 * 1024

_ASSISTANT_EXPOSURE_REFS_KEY = "$failure_link_assistant_exposure_refs"
_ASSISTANT_EXPOSURES_DECODE_RULES = (
    "Start with payload.card and recursively visit every object. Only a value whose "
    "key is assistant_exposures is encoded.",
    f"Each encoded assistant_exposures value is an object with the sole key "
    f"{_ASSISTANT_EXPOSURE_REFS_KEY}; its value is the ordered list of "
    "[table_index,row_index] references.",
    "For each reference, select payload.card_transport.tables[table_index], then zip "
    "that table's columns with tables[table_index].rows[row_index] to reconstruct one "
    "assistant-exposure object. Recursively decode values in that row and preserve "
    "reference order.",
    "Replace the marker object with the reconstructed exposure list. No other card "
    "field is removed or semantically transformed. The decoded contract-canonical "
    "JSONL bytes (compact sorted JSON plus one trailing LF) must match "
    "original_card_canonical_byte_count and original_card_canonical_sha256.",
)
_INLINE_CARD_DECODE_RULES = (
    "payload.card is the original card represented directly as canonical JSON data; "
    "no card field is encoded, omitted, or transformed.",
    "The contract-canonical JSONL bytes of payload.card (compact sorted JSON plus one "
    "trailing LF) must match original_card_canonical_byte_count and "
    "original_card_canonical_sha256.",
)

_PHASE_A_RULES = """
Phase A is outcome blind. Do not infer or guess the task score, success/failure
stratum, evaluator reason, or any hidden terminal verdict. Use the supplied
full trajectory outline plus contiguous prefix and target-to-terminal trace.
Follow each frozen strict-MHR chain from its reuse target through the last
observable trace step for the chain's main mechanism judgment; inspect the
prefix only for independently observable competing defects.

For each chain:
- affected_predicate names the concrete task-success predicate that the reused
  history could affect. Give it a stable lowercase predicate_id; chains about
  the same predicate in one task must use the same ID and description.
- target_contribution records the observed bridge from reuse to that predicate:
  creation, false-completion preservation, amplification, subgoal abandonment,
  an unbroken loop, mere concurrency, or UNKNOWN. Do not infer a contribution
  merely because strict MHR was already frozen.
- recovery_status describes whether the harmful mechanism is visibly corrected
  after the reuse target. A later unrelated useful action is not recovery.
- continuity_status is CONTINUOUS only when the same mechanism remains on the
  observed path without an intervening correction or independent break.
- final_observable_predicate concerns that same affected predicate and only what
  is visibly supported by the supplied trace; use NOT_OBSERVABLE or UNKNOWN
  when evidence is absent.
- competing_trace_defects must be independently observable defects in the
  complete supplied trajectory, including prefix defects before the earliest
  reuse target. Never invent one from a presumed bad score. Cite its true first
  step and only supplied step_id values.
- main chain evidence_step_ids must be sorted, unique supplied step_id values at
  or after that chain's reuse target. Prefix step IDs belong only inside a
  competing_trace_defect's evidence_step_ids.

Do not relabel strict MHR/MHR-OH, and do not make a causal claim.
""".strip()

_PHASE_B_RULES = """
Phase B opens only the outcome/evaluator evidence embedded in the validated
card after Phase A was frozen. Do not relabel strict MHR/MHR-OH or alter any
frozen Phase-A recovery, affected-predicate, or target-contribution judgment.

For a SUCCESS control, every failure-only field must be
NOT_APPLICABLE_SUCCESS_CONTROL and both alternative evidence lists must be
empty. Still copy the frozen affected_predicate_id, target_contribution, and
recovery_status exactly. For every card:
- evaluator_predicate.affected_predicate_id must copy the frozen Phase-A ID.
  Its description states exactly what the revealed evaluator tested or reported
  with respect to that predicate; do not silently substitute a different task
  predicate. Its evaluator_evidence must give one allowed field_path and a
  non-empty excerpt that is an actual substring of that card field after the
  contract's normalization (collapsed whitespace for strings; canonical JSON
  for scalar or structured values). Never cite an absent or empty field.

For a FAILURE task:
- verifier_alignment is DIRECT only when the evaluator's failed predicate or
  reason identifies the same frozen affected predicate and observed mechanism;
  thematic similarity is not direct alignment. INDIRECT requires an explicit,
  traceable bridge stated in the rationale. Evidence from evaluator score alone
  can never establish DIRECT alignment.
- alternative_sufficient_failure is PRESENT when either a frozen Phase-A defect
  or an evaluator-revealed independent defect suffices to explain failure.
  alternative_defect_ids may cite only frozen Phase-A defect IDs.
  evaluator_revealed_alternatives may add a defect absent from Phase A only when
  an allowed task_ended field directly supports it; use a stable lowercase ID,
  the exact allowed field_path, and a non-empty excerpt that is an actual
  normalized substring of the cited card field. Do not backfill an alternative
  merely from the zero score or from the existence of failure.
- ABSENT requires the supplied FULL_RECONSTRUCTION_PROJECTION and no supported
  frozen or evaluator-revealed alternative; use UNKNOWN when absence cannot be
  established.
- STRONG_OBSERVED_CONTRIBUTION requires NOT_RECOVERED, CONTINUOUS, DIRECT
  verifier alignment, an UNSATISFIED or PARTIAL final observable predicate,
  a known non-concurrent target contribution, ABSENT sufficient alternative
  failure, no frozen Phase-A competing defect at all, and at least one cited
  reuse-target-or-later evidence step.
- PLAUSIBLE_OBSERVED_CONTRIBUTION requires DIRECT or INDIRECT alignment and
  a known non-concurrent target contribution; it cannot be used for a
  recovered/no-harm chain.
- INDETERMINATE requires at least one UNKNOWN prerequisite.
- CO_OCCURRENCE_ONLY is the conservative label when linkage requirements are
  not met without an unresolved prerequisite.
- task_failure_link_level is the highest chain level under the supplied schema's
  precedence; a success control remains NOT_APPLICABLE_SUCCESS_CONTROL.

These are observational linkage levels, never counterfactual causation. The
output must leave causal_claim_supported false by respecting the supplied
schema and card contract.
""".strip()

_COMMON_RULES = """
Use only the supplied card, exact identities, and evidence. Do not use the
task's apparent difficulty or the existence of a review as evidence. Complete
every chain exactly once in sorted candidate_id order. Evidence IDs, defect
IDs, and evaluator alternative IDs must be sorted and unique. Prefer
UNKNOWN/INDETERMINATE over unsupported
specificity. Return exactly one JSON object matching the supplied JSON Schema,
with no Markdown or surrounding prose.
""".strip()


def build_review_prompt(
    *,
    phase: str,
    stage: str,
    reviewer_id: str,
    identity: Mapping[str, Any],
    card: Mapping[str, Any],
    schema: Mapping[str, Any],
    attachment_map: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
    card_transport_encoding: str | None = None,
) -> str:
    """Render one independent primary or secondary review prompt."""

    if phase not in {"A", "B"}:
        raise ValueError(f"unsupported failure-link phase: {phase}")
    if stage not in {"PRIMARY", "SECONDARY"}:
        raise ValueError(f"unsupported independent review stage: {stage}")
    phase_rules = _PHASE_A_RULES if phase == "A" else _PHASE_B_RULES
    prompt_card, card_transport = prepare_card_for_prompt(
        card,
        transport_encoding=card_transport_encoding,
    )
    payload = {
        "attachment_map": list(attachment_map),
        "card": prompt_card,
        "identity": dict(identity),
        "output_schema": dict(schema),
        "phase": phase,
        "prompt_version": PROMPT_VERSION,
        "reviewer_id": reviewer_id,
        "stage": stage,
    }
    payload["card_transport"] = card_transport
    sections = [
        "You are an independent MobileWorld failure-link evidence reviewer.",
        _COMMON_RULES,
        phase_rules,
    ]
    if validation_feedback is not None:
        sections.append(
            "The previous response was rejected by deterministic validation. "
            "Correct only the contract violation described here:\n"
            f"{validation_feedback}"
        )
    if card_transport["encoding"] == ASSISTANT_EXPOSURES_TRANSPORT_ENCODING:
        sections.append(
            "The card is losslessly transport-encoded only to avoid duplicating "
            "assistant_exposures. Apply payload.card_transport.decode_rules before "
            "reasoning; the decoded card is byte-bound to the original canonical card."
        )
    sections.append(
        "Review payload (attachment indices refer only to explicitly attached "
        "images; filesystem paths are intentionally absent):\n" + _compact_json(payload)
    )
    return "\n\n".join(sections)


def build_legacy_v3_review_prompt(
    *,
    phase: str,
    stage: str,
    reviewer_id: str,
    identity: Mapping[str, Any],
    card: Mapping[str, Any],
    schema: Mapping[str, Any],
    attachment_map: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
) -> str:
    """Rebuild frozen v3 bytes solely for explicit migration verification."""

    if phase not in {"A", "B"}:
        raise ValueError(f"unsupported failure-link phase: {phase}")
    if stage not in {"PRIMARY", "SECONDARY"}:
        raise ValueError(f"unsupported independent review stage: {stage}")
    phase_rules = _PHASE_A_RULES if phase == "A" else _PHASE_B_RULES
    payload = {
        "attachment_map": list(attachment_map),
        "card": dict(card),
        "identity": dict(identity),
        "output_schema": dict(schema),
        "phase": phase,
        "prompt_version": LEGACY_PROMPT_VERSION,
        "reviewer_id": reviewer_id,
        "stage": stage,
    }
    sections = [
        "You are an independent MobileWorld failure-link evidence reviewer.",
        _COMMON_RULES,
        phase_rules,
    ]
    if validation_feedback is not None:
        sections.append(
            "The previous response was rejected by deterministic validation. "
            "Correct only the contract violation described here:\n"
            f"{validation_feedback}"
        )
    sections.append(
        "Review payload (attachment indices refer only to explicitly attached "
        "images; filesystem paths are intentionally absent):\n" + _compact_json(payload)
    )
    return "\n\n".join(sections)


def build_adjudication_prompt(
    *,
    phase: str,
    reviewer_id: str,
    identity: Mapping[str, Any],
    card: Mapping[str, Any],
    schema: Mapping[str, Any],
    primary_review: Mapping[str, Any],
    secondary_review: Mapping[str, Any],
    material_disagreement: Mapping[str, Any],
    attachment_map: Sequence[Mapping[str, Any]],
    validation_feedback: str | None = None,
    card_transport_encoding: str | None = None,
) -> str:
    """Render one evidence-bound material-disagreement adjudication prompt."""

    if phase not in {"A", "B"}:
        raise ValueError(f"unsupported failure-link phase: {phase}")
    phase_rules = _PHASE_A_RULES if phase == "A" else _PHASE_B_RULES
    prompt_card, card_transport = prepare_card_for_prompt(
        card,
        transport_encoding=card_transport_encoding,
    )
    payload = {
        "attachment_map": list(attachment_map),
        "card": prompt_card,
        "identity": dict(identity),
        "material_disagreement": dict(material_disagreement),
        "output_schema": dict(schema),
        "phase": phase,
        "primary_review": dict(primary_review),
        "prompt_version": PROMPT_VERSION,
        "reviewer_id": reviewer_id,
        "secondary_review": dict(secondary_review),
        "stage": "ADJUDICATION",
    }
    payload["card_transport"] = card_transport
    sections = [
        "You are the independent adjudicator for one MobileWorld failure-link case.",
        _COMMON_RULES,
        phase_rules,
        (
            "Resolve only the supplied material disagreement from the evidence; "
            "do not choose a reviewer by identity, confidence, verbosity, or model. "
            "Return a complete review object so deterministic resolution can freeze it."
        ),
    ]
    if validation_feedback is not None:
        sections.append(
            "The previous response was rejected by deterministic validation. "
            "Correct only the contract violation described here:\n"
            f"{validation_feedback}"
        )
    if card_transport["encoding"] == ASSISTANT_EXPOSURES_TRANSPORT_ENCODING:
        sections.append(
            "The card is losslessly transport-encoded only to avoid duplicating "
            "assistant_exposures. Apply payload.card_transport.decode_rules before "
            "reasoning; the decoded card is byte-bound to the original canonical card."
        )
    sections.append("Adjudication payload:\n" + _compact_json(payload))
    return "\n\n".join(sections)


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def select_card_transport_encoding(card: Mapping[str, Any]) -> str:
    """Select the deterministic prompt-only transport for a new review unit."""

    canonical = canonical_json_bytes(dict(card))
    if len(canonical) <= LARGE_CARD_ENCODING_THRESHOLD_BYTES:
        return INLINE_CARD_TRANSPORT_ENCODING
    encoded_card, transport, exposure_count = _encode_assistant_exposures(card, canonical)
    if exposure_count == 0:
        return INLINE_CARD_TRANSPORT_ENCODING
    encoded_size = len(
        _compact_json({"card": encoded_card, "card_transport": transport}).encode("utf-8")
    )
    if encoded_size >= len(canonical):
        return INLINE_CARD_TRANSPORT_ENCODING
    return ASSISTANT_EXPOSURES_TRANSPORT_ENCODING


def prepare_card_for_prompt(
    card: Mapping[str, Any],
    *,
    transport_encoding: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the prompt representation and its self-describing transport metadata."""

    if transport_encoding is None:
        transport_encoding = select_card_transport_encoding(card)
    if transport_encoding == INLINE_CARD_TRANSPORT_ENCODING:
        canonical = canonical_json_bytes(dict(card))
        return dict(card), {
            "decode_rules": list(_INLINE_CARD_DECODE_RULES),
            "encoding": INLINE_CARD_TRANSPORT_ENCODING,
            "original_card_canonical_byte_count": len(canonical),
            "original_card_canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    if transport_encoding != ASSISTANT_EXPOSURES_TRANSPORT_ENCODING:
        raise ValueError(f"unsupported failure-link card transport: {transport_encoding}")
    canonical = canonical_json_bytes(dict(card))
    if len(canonical) <= LARGE_CARD_ENCODING_THRESHOLD_BYTES:
        raise ValueError(
            "assistant_exposures transport is forbidden below the large-card threshold"
        )
    encoded_card, transport, exposure_count = _encode_assistant_exposures(card, canonical)
    if exposure_count == 0:
        raise ValueError("large-card assistant_exposures transport found no exposure rows")
    reconstructed = decode_card_transport(encoded_card, transport)
    if canonical_json_bytes(reconstructed) != canonical:
        raise AssertionError("assistant_exposures transport failed lossless reconstruction")
    return encoded_card, transport


def decode_card_transport(
    encoded_card: Mapping[str, Any],
    transport: Mapping[str, Any],
) -> dict[str, Any]:
    """Strictly decode one prompt card and verify its canonical-byte commitment."""

    if transport.get("encoding") == INLINE_CARD_TRANSPORT_ENCODING:
        if set(transport) != {
            "decode_rules",
            "encoding",
            "original_card_canonical_byte_count",
            "original_card_canonical_sha256",
        } or transport.get("decode_rules") != list(_INLINE_CARD_DECODE_RULES):
            raise ValueError("inline failure-link card transport metadata is malformed")
        reconstructed = dict(encoded_card)
        canonical = canonical_json_bytes(reconstructed)
        if transport.get("original_card_canonical_byte_count") != len(canonical):
            raise ValueError("inline failure-link card byte count mismatch")
        if transport.get("original_card_canonical_sha256") != hashlib.sha256(canonical).hexdigest():
            raise ValueError("inline failure-link card SHA-256 mismatch")
        return reconstructed
    if transport.get("encoding") != ASSISTANT_EXPOSURES_TRANSPORT_ENCODING:
        raise ValueError("unsupported assistant_exposures transport encoding")
    if set(transport) != {
        "decode_rules",
        "encoding",
        "marker_key",
        "original_card_canonical_byte_count",
        "original_card_canonical_sha256",
        "tables",
    } or transport.get("decode_rules") != list(_ASSISTANT_EXPOSURES_DECODE_RULES):
        raise ValueError("assistant_exposures transport metadata is malformed")
    if transport.get("marker_key") != _ASSISTANT_EXPOSURE_REFS_KEY:
        raise ValueError("assistant_exposures transport marker mismatch")
    tables = transport.get("tables")
    if not isinstance(tables, list):
        raise ValueError("assistant_exposures transport tables must be a list")

    normalized_tables: list[tuple[tuple[str, ...], list[Any]]] = []
    for table_index, table in enumerate(tables):
        if not isinstance(table, Mapping) or set(table) != {"columns", "rows"}:
            raise ValueError(f"assistant_exposures table {table_index} is malformed")
        columns = table["columns"]
        rows = table["rows"]
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) for column in columns)
            or columns != sorted(set(columns))
            or not isinstance(rows, list)
        ):
            raise ValueError(f"assistant_exposures table {table_index} columns are invalid")
        if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            raise ValueError(f"assistant_exposures table {table_index} rows are invalid")
        normalized_tables.append((tuple(columns), rows))

    def decode(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, child in value.items():
                if key != "assistant_exposures":
                    result[key] = decode(child)
                    continue
                if not isinstance(child, Mapping) or set(child) != {_ASSISTANT_EXPOSURE_REFS_KEY}:
                    raise ValueError("encoded assistant_exposures marker is malformed")
                references = child[_ASSISTANT_EXPOSURE_REFS_KEY]
                if not isinstance(references, list):
                    raise ValueError("assistant_exposures references must be a list")
                exposures: list[dict[str, Any]] = []
                for reference in references:
                    if (
                        not isinstance(reference, list)
                        or len(reference) != 2
                        or any(type(index) is not int for index in reference)
                    ):
                        raise ValueError("assistant_exposures reference is malformed")
                    table_index, row_index = reference
                    if not 0 <= table_index < len(normalized_tables):
                        raise ValueError("assistant_exposures table reference is out of range")
                    columns, rows = normalized_tables[table_index]
                    if not 0 <= row_index < len(rows):
                        raise ValueError("assistant_exposures row reference is out of range")
                    exposures.append(
                        {
                            column: decode(cell)
                            for column, cell in zip(columns, rows[row_index], strict=True)
                        }
                    )
                result[key] = exposures
            return result
        if isinstance(value, list):
            return [decode(child) for child in value]
        return value

    reconstructed = decode(encoded_card)
    if not isinstance(reconstructed, dict):
        raise ValueError("decoded failure-link card must be an object")
    canonical = canonical_json_bytes(reconstructed)
    if transport.get("original_card_canonical_byte_count") != len(canonical):
        raise ValueError("decoded failure-link card byte count mismatch")
    if transport.get("original_card_canonical_sha256") != hashlib.sha256(canonical).hexdigest():
        raise ValueError("decoded failure-link card SHA-256 mismatch")
    return reconstructed


def _encode_assistant_exposures(
    card: Mapping[str, Any],
    canonical: bytes,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    tables: list[dict[str, Any]] = []
    table_indexes: dict[tuple[str, ...], int] = {}
    row_indexes: list[dict[str, int]] = []
    exposure_count = 0

    def encode(value: Any) -> Any:
        nonlocal exposure_count
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key in sorted(value):
                child = value[key]
                if key != "assistant_exposures":
                    result[key] = encode(child)
                    continue
                if not isinstance(child, list):
                    raise ValueError("assistant_exposures must be a list for transport encoding")
                references: list[list[int]] = []
                for exposure in child:
                    if not isinstance(exposure, Mapping) or not exposure:
                        raise ValueError("assistant_exposures rows must be non-empty JSON objects")
                    columns = tuple(sorted(exposure))
                    table_index = table_indexes.get(columns)
                    if table_index is None:
                        table_index = len(tables)
                        table_indexes[columns] = table_index
                        tables.append({"columns": list(columns), "rows": []})
                        row_indexes.append({})
                    row = [encode(exposure[column]) for column in columns]
                    row_signature = _compact_json(row)
                    row_index = row_indexes[table_index].get(row_signature)
                    if row_index is None:
                        row_index = len(tables[table_index]["rows"])
                        row_indexes[table_index][row_signature] = row_index
                        tables[table_index]["rows"].append(row)
                    references.append([table_index, row_index])
                    exposure_count += 1
                result[key] = {_ASSISTANT_EXPOSURE_REFS_KEY: references}
            return result
        if isinstance(value, list):
            return [encode(child) for child in value]
        return value

    encoded_card = encode(card)
    if not isinstance(encoded_card, dict):
        raise ValueError("failure-link card must be an object")
    transport = {
        "decode_rules": list(_ASSISTANT_EXPOSURES_DECODE_RULES),
        "encoding": ASSISTANT_EXPOSURES_TRANSPORT_ENCODING,
        "marker_key": _ASSISTANT_EXPOSURE_REFS_KEY,
        "original_card_canonical_byte_count": len(canonical),
        "original_card_canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "tables": tables,
    }
    return encoded_card, transport, exposure_count


__all__ = [
    "ASSISTANT_EXPOSURES_TRANSPORT_ENCODING",
    "INLINE_CARD_TRANSPORT_ENCODING",
    "LARGE_CARD_ENCODING_THRESHOLD_BYTES",
    "LEGACY_PROMPT_VERSION",
    "PROMPT_VERSION",
    "build_adjudication_prompt",
    "build_legacy_v3_review_prompt",
    "build_review_prompt",
    "decode_card_transport",
    "prepare_card_for_prompt",
    "select_card_transport_encoding",
]
