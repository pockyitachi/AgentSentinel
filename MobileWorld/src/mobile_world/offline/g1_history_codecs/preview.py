"""Pure G1.5 five-arm preview API for an already human-curated draft.

The API performs no extraction of semantic claims and has no provider, model,
network, GPU, replay, server, or action path.  A caller supplies exact frozen
request/history records, exact human-selected spans, correction candidates,
and any delimiter repairs.  The selected G1.5 codec and frozen G1.2 renderer
then produce deterministic read-only previews.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    CorrectionAnchor,
    CorrectionContextKind,
    EvidenceRef,
    ExecutionMode,
    FailurePolicy,
    HistoryIR,
    HistoryRecord,
    JsonPath,
    JsonValue,
    OperationKind,
    PlanOperation,
    PlanSetProfile,
    PortableContractError,
    ProviderDecision,
    RenderResult,
    SourceSpan,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    stable_id,
    text_sha256,
)
from mobile_world.offline.causal_replay.core import (
    restore_original,
    validate_plan_set,
    validate_pre_send,
)
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.causal_replay.registry import HistoryCodecRegistry
from mobile_world.offline.g1_history_codecs.codecs import CuratedSpanBinding
from mobile_world.offline.g1_history_codecs.diff import render_human_diff

PREVIEW_SCHEMA_VERSION = "mobileworld.g1.history-codec-preview/v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPAIR_PATTERNS = {
    "DELETE_EMPTY_DELIMITER": (
        re.compile(r"<thinking>\s*"),
        re.compile(r"\s*</thinking>"),
    ),
    "DELETE_ORPHAN_SEPARATOR": (
        re.compile(r"(?:Step\s+[1-9]\d*|Thought)\s*:\s*"),
        re.compile(r"\s*;\s*"),
    ),
}


@dataclass(frozen=True)
class PinnedTokenCounter:
    """Caller-injected, locally pinned tokenizer counter.

    G1.5 never imports or loads a tokenizer.  The callback contract is exact:
    count ``text`` with special tokens disabled.  G1.6 must bind the declared
    tokenizer artifact set and pass the publication's canonical tokenizer-
    record digest as ``tokenizer_sha256``; it blocks when any artifact is absent.
    """

    tokenizer_id: str
    tokenizer_sha256: str
    count_without_special_tokens: Callable[[str], int] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        raw_id = cast(object, self.tokenizer_id)
        raw_sha256 = cast(object, self.tokenizer_sha256)
        raw_counter = cast(object, self.count_without_special_tokens)
        if not isinstance(raw_id, str) or not raw_id:
            raise PortableContractError(
                "TOKENIZER_BINDING_INVALID", "pinned tokenizer ID must be non-empty text"
            )
        if not isinstance(raw_sha256, str) or _SHA256.fullmatch(raw_sha256) is None:
            raise PortableContractError(
                "TOKENIZER_BINDING_INVALID", "pinned tokenizer digest must be lowercase SHA-256"
            )
        if not callable(raw_counter):
            raise PortableContractError(
                "TOKENIZER_BINDING_INVALID", "pinned token counter must be callable"
            )

    def count(self, text: str) -> int:
        try:
            first = self.count_without_special_tokens(text)
            second = self.count_without_special_tokens(text)
        except Exception as exc:
            raise PortableContractError(
                "TOKEN_COUNTER_INVALID",
                "pinned token counter failed without producing two deterministic counts",
            ) from exc
        if type(first) is not int or first < 0 or type(second) is not int or second != first:
            raise PortableContractError(
                "TOKEN_COUNTER_INVALID",
                "pinned token counter must return one deterministic non-negative integer",
            )
        return first


@dataclass(frozen=True)
class CorrectionCandidateMetric:
    text: str
    text_sha256: str
    token_count: int
    utf8_byte_count: int
    codepoint_count: int
    rank: int

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "text": self.text,
            "text_sha256": self.text_sha256,
            "token_count": self.token_count,
            "utf8_byte_count": self.utf8_byte_count,
            "codepoint_count": self.codepoint_count,
            "rank": self.rank,
        }


@dataclass(frozen=True)
class CorrectionRanking:
    tokenizer_id: str
    tokenizer_sha256: str
    special_tokens_enabled: bool
    candidates: tuple[CorrectionCandidateMetric, ...]

    @property
    def selected_text(self) -> str:
        return self.candidates[0].text

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_sha256": self.tokenizer_sha256,
            "special_tokens_enabled": self.special_tokens_enabled,
            "tie_break_order": [
                "token_count",
                "utf8_byte_count",
                "codepoint_count",
                "lexicographic_utf8_bytes",
            ],
            "selected_text_sha256": self.candidates[0].text_sha256,
            "candidates": [item.to_dict() for item in self.candidates],
        }


@dataclass(frozen=True)
class ShamTokenMatch:
    tokenizer_id: str
    tokenizer_sha256: str
    focal_binding_ids: tuple[str, ...]
    focal_span_sha256s: tuple[str, ...]
    focal_token_count: int
    sham_binding_id: str
    sham_span_sha256: str
    sham_token_count: int
    matched: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_sha256": self.tokenizer_sha256,
            "special_tokens_enabled": False,
            "focal_binding_ids": list(self.focal_binding_ids),
            "focal_span_sha256s": list(self.focal_span_sha256s),
            "focal_token_count": self.focal_token_count,
            "sham_binding_id": self.sham_binding_id,
            "sham_span_sha256": self.sham_span_sha256,
            "sham_token_count": self.sham_token_count,
            "match_formula": ("(5*sham>=4*focal && 4*sham<=5*focal) || abs(sham-focal)<=4"),
            "matched": self.matched,
        }


@dataclass(frozen=True)
class PreviewCorrectionAnchor:
    binding_id: str
    target_record_id: str
    anchor: CorrectionAnchor

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "binding_id": self.binding_id,
            "target_record_id": self.target_record_id,
            "anchor": self.anchor.to_dict(),
        }


@dataclass(frozen=True)
class DelimiterRepairBinding:
    repair_id: str
    arm: ArmKind
    operation: str
    shell_binding_id: str
    target_binding_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        raw_id = cast(object, self.repair_id)
        raw_arm = cast(object, self.arm)
        raw_operation = cast(object, self.operation)
        raw_shell_id = cast(object, self.shell_binding_id)
        raw_targets = cast(object, self.target_binding_ids)
        if not isinstance(raw_id, str) or not raw_id:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "delimiter repair ID must be non-empty text"
            )
        if not isinstance(raw_arm, ArmKind) or raw_arm is ArmKind.ORIGINAL:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "Original cannot contain a delimiter repair"
            )
        if not isinstance(raw_operation, str) or raw_operation not in _REPAIR_PATTERNS:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "delimiter repair operation is not whitelisted"
            )
        if not isinstance(raw_shell_id, str) or not raw_shell_id:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "delimiter repair needs one shell binding ID"
            )
        if (
            not isinstance(raw_targets, tuple)
            or not raw_targets
            or any(not isinstance(item, str) or not item for item in raw_targets)
            or len(set(raw_targets)) != len(raw_targets)
        ):
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair target bindings must be a unique non-empty tuple",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "repair_id": self.repair_id,
            "arm": self.arm.value,
            "operation": self.operation,
            "shell_binding_id": self.shell_binding_id,
            "target_binding_ids": list(self.target_binding_ids),
        }


@dataclass(frozen=True)
class PreviewArm:
    arm: ArmKind
    render_result: RenderResult
    rendered_history: tuple[dict[str, JsonValue], ...]
    validation_receipt_sha256: str
    target_only_diff: bool
    source_mapping_reversible: bool

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "arm": self.arm.value,
            "rendered_request_sha256": self.render_result.rendered_request_sha256,
            "rendered_history": [copy_json(item) for item in self.rendered_history],
            "diffs": [item.to_dict() for item in self.render_result.diffs],
            "list_insertions": [item.to_dict() for item in self.render_result.list_insertions],
            "source_mappings": [item.to_dict() for item in self.render_result.source_mappings],
            "human_diff": render_human_diff(self.render_result),
            "validation_receipt_sha256": self.validation_receipt_sha256,
            "target_only_diff": self.target_only_diff,
            "source_mapping_reversible": self.source_mapping_reversible,
            "provider_invocation_allowed": False,
        }


@dataclass(frozen=True)
class FiveArmPreview:
    codec_id: str
    source_request_sha256: str
    history_ir_sha256: str
    plan_set_sha256: str
    correction_ranking: CorrectionRanking
    correction_anchors: tuple[PreviewCorrectionAnchor, ...]
    sham_token_match: ShamTokenMatch
    delimiter_repairs: tuple[DelimiterRepairBinding, ...]
    arms: tuple[PreviewArm, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "preview_scope": "CPU_ONLY_READ_ONLY",
            "plan_set_profile": PlanSetProfile.G1_STRICT_MHR.value,
            "codec_id": self.codec_id,
            "source_request_sha256": self.source_request_sha256,
            "history_ir_sha256": self.history_ir_sha256,
            "plan_set_sha256": self.plan_set_sha256,
            "correction_ranking": self.correction_ranking.to_dict(),
            "correction_anchors": [item.to_dict() for item in self.correction_anchors],
            "sham_token_match": self.sham_token_match.to_dict(),
            "delimiter_repairs": [item.to_dict() for item in self.delimiter_repairs],
            "arms": [item.to_dict() for item in self.arms],
            "provider_invocation_allowed": False,
            "provider_invocation_count": 0,
            "treatment_response_generation_allowed": False,
            "treatment_response_count": 0,
            "network_used": False,
            "gpu_used": False,
            "replay_executed": False,
            "gui_action_executed": False,
        }


@dataclass(frozen=True)
class CleanControlPreview:
    codec_id: str
    source_request_sha256: str
    history_ir_sha256: str
    plan_set_sha256: str
    sham_token_match: ShamTokenMatch
    delimiter_repairs: tuple[DelimiterRepairBinding, ...]
    arms: tuple[PreviewArm, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "preview_scope": "CPU_ONLY_READ_ONLY",
            "plan_set_profile": PlanSetProfile.G1_CLEAN_CONTROL.value,
            "codec_id": self.codec_id,
            "source_request_sha256": self.source_request_sha256,
            "history_ir_sha256": self.history_ir_sha256,
            "plan_set_sha256": self.plan_set_sha256,
            "correction_ranking": None,
            "correction_anchors": [],
            "sham_token_match": self.sham_token_match.to_dict(),
            "delimiter_repairs": [item.to_dict() for item in self.delimiter_repairs],
            "arms": [item.to_dict() for item in self.arms],
            "provider_invocation_allowed": False,
            "provider_invocation_count": 0,
            "treatment_response_generation_allowed": False,
            "treatment_response_count": 0,
            "network_used": False,
            "gpu_used": False,
            "replay_executed": False,
            "gui_action_executed": False,
        }


@dataclass(frozen=True)
class _BoundSourceRecord:
    container_path: JsonPath
    container_start: int
    exact_text: str


def rank_correction_candidates(
    candidates: Sequence[str], *, token_counter: PinnedTokenCounter | None
) -> CorrectionRanking:
    """Apply the frozen tokens/bytes/codepoints/UTF-8 tie-break mechanically."""

    if token_counter is None:
        raise PortableContractError(
            "PINNED_TOKENIZER_UNAVAILABLE",
            "token-dependent preview is blocked until the pinned local tokenizer is available",
        )
    if not isinstance(token_counter, PinnedTokenCounter):
        raise PortableContractError(
            "TOKENIZER_BINDING_INVALID", "token counter does not carry the pinned binding"
        )
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise PortableContractError(
            "CORRECTION_CANDIDATES_INVALID",
            "correction candidates must be a sequence of complete human-authored strings",
        )
    raw = cast(tuple[object, ...], tuple(candidates))
    if (
        not raw
        or any(not isinstance(item, str) or not item.strip() for item in raw)
        or len(set(raw)) != len(raw)
    ):
        raise PortableContractError(
            "CORRECTION_CANDIDATES_INVALID",
            "correction candidates must be unique non-empty human-authored text",
        )
    texts = cast(tuple[str, ...], raw)
    counted = [
        (
            token_counter.count(text),
            len(text.encode("utf-8")),
            len(text),
            text.encode("utf-8"),
            text,
        )
        for text in texts
    ]
    ordered = sorted(counted)
    metrics = tuple(
        CorrectionCandidateMetric(
            text=text,
            text_sha256=text_sha256(text),
            token_count=token_count,
            utf8_byte_count=utf8_count,
            codepoint_count=codepoint_count,
            rank=index,
        )
        for index, (token_count, utf8_count, codepoint_count, _, text) in enumerate(
            ordered, start=1
        )
    )
    return CorrectionRanking(
        tokenizer_id=token_counter.tokenizer_id,
        tokenizer_sha256=token_counter.tokenizer_sha256,
        special_tokens_enabled=False,
        candidates=metrics,
    )


def bind_human_record_spans(
    *,
    application_request: JsonValue,
    base_codec: HistoryCodec,
    source_records: Sequence[Mapping[str, JsonValue]],
    selections: Sequence[Mapping[str, JsonValue]],
) -> tuple[CuratedSpanBinding, ...]:
    """Convert exact record-relative human selections to host-request coordinates.

    ``source_records`` use the frozen G1.3 treatment-surface shape.  ``selections``
    use G1.6's half-open record-relative span shape plus ``binding_id`` and
    ``span_role``.  The function matches records structurally; it never fuzzy
    searches or infers claim semantics.
    """

    request_snapshot = canonical_sha256(application_request)
    base_ir = base_codec.extract(application_request)
    if any(
        record.editable_spans
        or any(
            span.span_role is SpanRole.ELIGIBLE_PROTOCOL_SHELL for span in record.protected_spans
        )
        for record in base_ir.records
    ):
        raise PortableContractError(
            "BASE_CODEC_ALREADY_BOUND",
            "record-relative binding requires a selected codec with an empty catalog",
        )

    record_by_external_id: dict[str, _BoundSourceRecord] = {}
    required_record_keys = {
        "record_id",
        "container_path",
        "message_index",
        "content_block_index",
        "author_role",
        "exact_text",
        "record_sha256",
    }
    for raw_record_value in source_records:
        if not isinstance(raw_record_value, Mapping):
            raise PortableContractError(
                "SOURCE_RECORD_SHAPE_INVALID", "source history record must be an object"
            )
        raw_record = raw_record_value
        if set(raw_record) != required_record_keys:
            raise PortableContractError(
                "SOURCE_RECORD_SHAPE_INVALID", "source history record shape is not closed"
            )
        record_id = raw_record["record_id"]
        path_value = raw_record["container_path"]
        exact_text = raw_record["exact_text"]
        record_sha = raw_record["record_sha256"]
        if (
            not isinstance(record_id, str)
            or not record_id
            or not isinstance(path_value, list)
            or not path_value
            or any(
                not (
                    (isinstance(token, str) and bool(token)) or (type(token) is int and token >= 0)
                )
                for token in path_value
            )
            or not isinstance(exact_text, str)
            or not exact_text
            or not isinstance(record_sha, str)
            or record_sha != text_sha256(exact_text)
            or record_id in record_by_external_id
        ):
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_INVALID", "source history record is malformed or duplicate"
            )
        path: JsonPath = tuple(cast(list[str | int], path_value))
        container = get_at_path(application_request, path)
        if not isinstance(container, str):
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_INVALID", "source history record path is not text"
            )
        message_index = raw_record["message_index"]
        content_block_index = raw_record["content_block_index"]
        author_role = raw_record["author_role"]
        if (
            type(message_index) is not int
            or (
                content_block_index is not None
                and (type(content_block_index) is not int or content_block_index < 0)
            )
            or not isinstance(author_role, str)
            or not author_role
        ):
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_INVALID",
                "source record host coordinates or role are invalid",
            )
        structural_matches = [
            record
            for record in base_ir.records
            if record.coordinates.request_path == path
            and record.coordinates.message_index == message_index
            and record.coordinates.content_block_index == content_block_index
            and record.role == author_role
        ]
        if not structural_matches:
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_AMBIGUOUS",
                "source history record has no structurally matching codec record",
            )
        exact_record_matches = [
            record
            for record in structural_matches
            if record.source_span.exact_text == exact_text and record.record_sha256 == record_sha
        ]
        if container == exact_text and text_sha256(container) == record_sha:
            container_start = 0
        elif len(exact_record_matches) == 1:
            container_start = exact_record_matches[0].source_span.char_start
        else:
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_AMBIGUOUS",
                "source history record must be the exact container or one exact codec record",
            )
        record_by_external_id[record_id] = _BoundSourceRecord(
            container_path=path,
            container_start=container_start,
            exact_text=exact_text,
        )

    required_selection_keys = {
        "binding_id",
        "record_id",
        "char_start",
        "char_end",
        "utf8_byte_start",
        "utf8_byte_end",
        "exact_text",
        "span_sha256",
        "span_role",
        "human_selected",
    }
    bindings: list[CuratedSpanBinding] = []
    for selection_value in selections:
        if not isinstance(selection_value, Mapping):
            raise PortableContractError(
                "HUMAN_SPAN_SHAPE_INVALID", "human span selection must be an object"
            )
        selection = selection_value
        if set(selection) != required_selection_keys or selection["human_selected"] is not True:
            raise PortableContractError(
                "HUMAN_SPAN_SHAPE_INVALID",
                "human span selection must be closed and explicitly selected",
            )
        binding_id = selection["binding_id"]
        external_record_id = selection["record_id"]
        if not isinstance(binding_id, str) or not binding_id:
            raise PortableContractError(
                "HUMAN_SPAN_SHAPE_INVALID", "human span binding ID must be non-empty text"
            )
        resolved = (
            record_by_external_id.get(external_record_id)
            if isinstance(external_record_id, str)
            else None
        )
        if resolved is None:
            raise PortableContractError(
                "HUMAN_SPAN_RECORD_UNKNOWN", "human span references an unknown source record"
            )
        source_text = resolved.exact_text
        start = selection["char_start"]
        end = selection["char_end"]
        byte_start = selection["utf8_byte_start"]
        byte_end = selection["utf8_byte_end"]
        exact = selection["exact_text"]
        digest = selection["span_sha256"]
        if (
            type(start) is not int
            or type(end) is not int
            or type(byte_start) is not int
            or type(byte_end) is not int
            or not 0 <= start < end <= len(source_text)
            or source_text[start:end] != exact
            or len(source_text[:start].encode("utf-8")) != byte_start
            or len(source_text[:end].encode("utf-8")) != byte_end
            or not isinstance(exact, str)
            or digest != text_sha256(exact)
        ):
            raise PortableContractError(
                "HUMAN_SPAN_STALE", "human span text, digest, or dual coordinates drifted"
            )
        try:
            role = SpanRole(cast(str, selection["span_role"]))
        except (TypeError, ValueError) as exc:
            raise PortableContractError(
                "HUMAN_SPAN_ROLE_INVALID", "human span role is unsupported"
            ) from exc
        if role not in {
            SpanRole.EDITABLE_CLAIM,
            SpanRole.BENIGN_SHAM,
            SpanRole.ELIGIBLE_PROTOCOL_SHELL,
        }:
            raise PortableContractError("HUMAN_SPAN_ROLE_INVALID", "human span role is unsupported")
        container_path = resolved.container_path
        container = get_at_path(application_request, container_path)
        if not isinstance(container, str):
            raise PortableContractError(
                "SOURCE_RECORD_BINDING_INVALID", "resolved source record is not text"
            )
        bindings.append(
            CuratedSpanBinding.from_text(
                binding_id=binding_id,
                source_request_sha256=request_snapshot,
                container_path=container_path,
                container_text=container,
                char_start=resolved.container_start + start,
                char_end=resolved.container_start + end,
                span_role=role,
            )
        )
    if canonical_sha256(application_request) != request_snapshot:
        raise PortableContractError(
            "PREVIEW_INPUT_MUTATED", "record binding mutated the exact source request"
        )
    return tuple(bindings)


def _binding_index(ir: HistoryIR) -> dict[str, tuple[HistoryRecord, SourceSpan]]:
    index: dict[str, tuple[HistoryRecord, SourceSpan]] = {}
    for record in ir.records:
        editable_ids = record.provenance.get("curated_binding_ids", [])
        if not isinstance(editable_ids, list) or len(editable_ids) != len(record.editable_spans):
            raise PortableContractError(
                "PREVIEW_BINDING_INDEX_INVALID", "editable binding provenance is inconsistent"
            )
        for binding_id, span in zip(editable_ids, record.editable_spans, strict=True):
            if not isinstance(binding_id, str) or binding_id in index:
                raise PortableContractError(
                    "PREVIEW_BINDING_INDEX_INVALID", "editable binding ID is invalid or duplicate"
                )
            index[binding_id] = (record, span)
        shell_ids = record.provenance.get("curated_shell_binding_ids", [])
        shell_spans = tuple(
            span
            for span in record.protected_spans
            if span.span_role is SpanRole.ELIGIBLE_PROTOCOL_SHELL
        )
        if not isinstance(shell_ids, list) or len(shell_ids) != len(shell_spans):
            raise PortableContractError(
                "PREVIEW_BINDING_INDEX_INVALID", "shell binding provenance is inconsistent"
            )
        for binding_id, span in zip(shell_ids, shell_spans, strict=True):
            if not isinstance(binding_id, str) or binding_id in index:
                raise PortableContractError(
                    "PREVIEW_BINDING_INDEX_INVALID", "shell binding ID is invalid or duplicate"
                )
            index[binding_id] = (record, span)
    return index


def _sham_token_match(
    *,
    binding_index: Mapping[str, tuple[HistoryRecord, SourceSpan]],
    focal_binding_ids: Sequence[str],
    sham_binding_id: str,
    token_counter: PinnedTokenCounter | None,
) -> ShamTokenMatch:
    if token_counter is None:
        raise PortableContractError(
            "PINNED_TOKENIZER_UNAVAILABLE",
            "token-dependent preview is blocked until the pinned local tokenizer is available",
        )
    if not isinstance(token_counter, PinnedTokenCounter):
        raise PortableContractError(
            "TOKENIZER_BINDING_INVALID", "token counter does not carry the pinned binding"
        )
    focal_ids = tuple(focal_binding_ids)
    focal = tuple(binding_index.get(item) for item in focal_ids)
    sham = binding_index.get(sham_binding_id)
    if not focal_ids or any(item is None for item in focal) or sham is None:
        raise PortableContractError(
            "PREVIEW_TARGET_UNKNOWN", "sham token check references an absent selected binding"
        )
    focal_spans = tuple(cast(tuple[HistoryRecord, SourceSpan], item)[1] for item in focal)
    sham_span = sham[1]
    if (
        any(span.span_role is not SpanRole.EDITABLE_CLAIM for span in focal_spans)
        or sham_span.span_role is not SpanRole.BENIGN_SHAM
    ):
        raise PortableContractError(
            "PREVIEW_TARGET_ROLE_INVALID", "sham token check uses the wrong span roles"
        )
    focal_count = sum(token_counter.count(span.exact_text) for span in focal_spans)
    sham_count = token_counter.count(sham_span.exact_text)
    matched = (5 * sham_count >= 4 * focal_count and 4 * sham_count <= 5 * focal_count) or abs(
        sham_count - focal_count
    ) <= 4
    return ShamTokenMatch(
        tokenizer_id=token_counter.tokenizer_id,
        tokenizer_sha256=token_counter.tokenizer_sha256,
        focal_binding_ids=focal_ids,
        focal_span_sha256s=tuple(span.span_sha256 for span in focal_spans),
        focal_token_count=focal_count,
        sham_binding_id=sham_binding_id,
        sham_span_sha256=sham_span.span_sha256,
        sham_token_count=sham_count,
        matched=matched,
    )


def _operation_sort_key(operation: PlanOperation) -> tuple[object, ...]:
    return (
        tuple(
            (0, token) if isinstance(token, str) else (1, token)
            for token in operation.target_span.container_path
        ),
        operation.target_span.char_start,
        operation.target_span.char_end,
        operation.target_span.span_sha256,
        operation.target_record_id,
        operation.operation_id,
    )


def _operation_id(
    *, arm: ArmKind, binding_id: str, record_id: str, span_sha256: str, shell: bool
) -> str:
    return stable_id(
        "operation",
        {
            "preview_schema_version": PREVIEW_SCHEMA_VERSION,
            "arm": arm.value,
            "binding_id": binding_id,
            "record_id": record_id,
            "span_sha256": span_sha256,
            "protocol_shell": shell,
        },
    )


def _plan(
    *,
    ir: HistoryIR,
    arm: ArmKind,
    target_binding_ids: tuple[str, ...],
    correction_text: str,
    correction_evidence_refs: tuple[EvidenceRef, ...],
    repairs: tuple[DelimiterRepairBinding, ...],
    binding_index: dict[str, tuple[HistoryRecord, SourceSpan]],
) -> TransformationPlan:
    operations: list[PlanOperation] = []
    operation_ids: dict[str, str] = {}
    for binding_id in target_binding_ids:
        located = binding_index.get(binding_id)
        if located is None:
            raise PortableContractError(
                "PREVIEW_TARGET_UNKNOWN", "preview target binding is absent from the selected IR"
            )
        record, span = located
        span_role = span.span_role
        expected_role = (
            SpanRole.BENIGN_SHAM if arm is ArmKind.SHAM_BENIGN_EDIT else SpanRole.EDITABLE_CLAIM
        )
        if span_role is not expected_role:
            raise PortableContractError(
                "PREVIEW_TARGET_ROLE_INVALID", "preview target has the wrong semantic span role"
            )
        record_id = record.record_id
        operation_id = _operation_id(
            arm=arm,
            binding_id=binding_id,
            record_id=record_id,
            span_sha256=span.span_sha256,
            shell=False,
        )
        operation_ids[binding_id] = operation_id
        if arm is ArmKind.MASK_CORRECTION:
            anchors = record.correction_anchors
            if len(anchors) != 1:
                raise PortableContractError(
                    "PREVIEW_CORRECTION_ANCHOR_INVALID",
                    "preview correction target needs one exact host-safe anchor",
                )
            anchor = anchors[0]
            if anchor.context_kind is not CorrectionContextKind.TEXT_CONTENT_BLOCK:
                raise PortableContractError(
                    "PREVIEW_CORRECTION_CONTEXT_UNSUPPORTED",
                    "G1.5 preview supports the selected codecs' text-block correction anchor",
                )
            rendered_context: JsonValue = {
                "type": "text",
                "text": (anchor.visible_prefix + correction_text + anchor.visible_suffix),
            }
            operations.append(
                PlanOperation(
                    operation_id=operation_id,
                    kind=OperationKind.REPLACE,
                    target_record_id=record_id,
                    target_span=span,
                    replacement_text=correction_text,
                    replacement_author="SENTINEL",
                    evidence_refs=correction_evidence_refs,
                    correction_anchor=anchor,
                    rendered_correction_context=rendered_context,
                )
            )
        else:
            operations.append(
                PlanOperation(
                    operation_id=operation_id,
                    kind=OperationKind.DROP,
                    target_record_id=record_id,
                    target_span=span,
                )
            )
    for repair in repairs:
        located = binding_index.get(repair.shell_binding_id)
        if located is None:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "delimiter repair shell binding is absent from the IR"
            )
        record, span = located
        if span.span_role is not SpanRole.ELIGIBLE_PROTOCOL_SHELL:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID", "delimiter repair does not select an eligible shell"
            )
        try:
            bound_target_ids = tuple(operation_ids[item] for item in repair.target_binding_ids)
        except KeyError as exc:
            raise PortableContractError(
                "DELIMITER_REPAIR_INVALID",
                "delimiter repair must bind selected semantic targets in the same arm",
            ) from exc
        record_id = record.record_id
        operations.append(
            PlanOperation(
                operation_id=_operation_id(
                    arm=arm,
                    binding_id=repair.shell_binding_id,
                    record_id=record_id,
                    span_sha256=span.span_sha256,
                    shell=True,
                ),
                kind=OperationKind.DROP,
                target_record_id=record_id,
                target_span=span,
                protocol_shell_for=bound_target_ids,
            )
        )
    operations.sort(key=_operation_sort_key)
    subject: dict[str, JsonValue] = {
        "host_id": ir.host_id,
        "history_family": ir.history_family.value,
        "codec_id": ir.codec_id,
        "codec_contract_version": ir.codec_contract_version,
        "source_request_sha256": ir.raw_request_sha256,
        "arm": arm.value,
        "operations": [item.to_dict() for item in operations],
    }
    return TransformationPlan(
        plan_id=stable_id("plan", subject),
        host_id=ir.host_id,
        history_family=ir.history_family,
        codec_id=ir.codec_id,
        codec_contract_version=ir.codec_contract_version,
        source_request_sha256=ir.raw_request_sha256,
        arm=arm,
        operations=tuple(operations),
        curated=True,
        deployment_prediction=False,
    )


def _repair_is_causally_empty(
    text: str,
    selected_syntax: str,
    *,
    start: int,
    end: int,
    target_intervals: list[tuple[int, int]],
    repair_intervals: list[tuple[int, int]],
    replacement_intervals: list[tuple[int, int, str]],
) -> bool:
    stripped = selected_syntax.strip()
    all_intervals = [*target_intervals, *repair_intervals]
    if re.fullmatch(r"(?:Step\s+[1-9]\d*|Thought)\s*:", stripped) is not None:
        if stripped.startswith("Step"):
            line_start = text.rfind("\n", 0, start) + 1
            previous_separator = text.rfind(";", line_start, start)
            scope_start = max(line_start, previous_separator + 1)
            next_separator = text.find(";", end)
            line_end = text.find("\n", end)
            if line_end < 0:
                line_end = len(text)
            scope_end = next_separator + 1 if 0 <= next_separator < line_end else line_end
        else:
            scope_start = text.rfind("\n", 0, start) + 1
            scope_end = text.find("\n", end)
            if scope_end < 0:
                scope_end = len(text)
    elif stripped in {"<thinking>", "</thinking>"}:
        if stripped == "<thinking>":
            opening_start = start + selected_syntax.index("<thinking>")
            opening_end = opening_start + len("<thinking>")
            closing_start = text.find("</thinking>", opening_end)
        else:
            closing_start = start + selected_syntax.index("</thinking>")
            opening_start = text.rfind("<thinking>", 0, closing_start)
            opening_end = opening_start + len("<thinking>")
        if opening_start < 0 or closing_start < 0:
            return False
        closing_end = closing_start + len("</thinking>")
        if not (
            any(left <= opening_start and opening_end <= right for left, right in repair_intervals)
            and any(
                left <= closing_start and closing_end <= right for left, right in repair_intervals
            )
        ):
            return False
        scope_start, scope_end = opening_end, closing_start
    elif stripped == ";":
        semicolon = start + selected_syntax.index(";")
        line_start = text.rfind("\n", 0, semicolon) + 1
        previous_separator = text.rfind(";", line_start, semicolon)
        scope_start = max(line_start, previous_separator + 1)
        scope_end = semicolon + 1
        if not any(
            scope_start <= target_start < target_end <= semicolon
            for target_start, target_end in target_intervals
        ):
            return False
    else:
        return False
    if not any(
        scope_start <= target_start < target_end <= scope_end
        for target_start, target_end in target_intervals
    ):
        return False
    cursor = scope_start
    remaining: list[str] = []
    for left, right in sorted(all_intervals):
        clipped_left = max(scope_start, left)
        clipped_right = min(scope_end, right)
        if clipped_left >= clipped_right or clipped_right <= cursor:
            continue
        if clipped_left > cursor:
            remaining.append(text[cursor:clipped_left])
        cursor = max(cursor, clipped_right)
    if cursor < scope_end:
        remaining.append(text[cursor:scope_end])
    remaining.extend(
        replacement
        for left, right, replacement in replacement_intervals
        if scope_start <= left < right <= scope_end
    )
    return "".join(remaining).strip() == ""


def _validate_repairs(
    *,
    request: JsonValue,
    ir: HistoryIR,
    plans: tuple[TransformationPlan, ...],
    repairs: tuple[DelimiterRepairBinding, ...],
    binding_index: dict[str, tuple[HistoryRecord, SourceSpan]],
) -> None:
    for plan in plans:
        arm_repairs = tuple(item for item in repairs if item.arm is plan.arm)
        if not arm_repairs:
            continue
        regular = tuple(item for item in plan.operations if not item.protocol_shell_for)
        shell = tuple(item for item in plan.operations if item.protocol_shell_for)
        by_record_targets: dict[str, list[tuple[int, int]]] = {}
        by_record_replacements: dict[str, list[tuple[int, int, str]]] = {}
        by_record_repairs: dict[str, list[tuple[int, int]]] = {}
        for operation in regular:
            span = operation.target_span
            by_record_targets.setdefault(operation.target_record_id, []).append(
                (span.char_start, span.char_end)
            )
            if operation.kind is OperationKind.REPLACE:
                assert operation.replacement_text is not None
                by_record_replacements.setdefault(operation.target_record_id, []).append(
                    (span.char_start, span.char_end, operation.replacement_text)
                )
        for operation in shell:
            span = operation.target_span
            by_record_repairs.setdefault(operation.target_record_id, []).append(
                (span.char_start, span.char_end)
            )
        for repair in arm_repairs:
            record, span = binding_index[repair.shell_binding_id]
            selected = span.exact_text
            if not any(
                pattern.fullmatch(selected) for pattern in _REPAIR_PATTERNS[repair.operation]
            ):
                raise PortableContractError(
                    "DELIMITER_REPAIR_INVALID",
                    "delimiter bytes do not match the declared operation",
                )
            record_id = record.record_id
            targets = by_record_targets.get(record_id, [])
            if not targets:
                raise PortableContractError(
                    "DELIMITER_REPAIR_INVALID", "delimiter repair changes an unselected record"
                )
            path = span.container_path
            text = get_at_path(request, path)
            if not isinstance(text, str):
                raise PortableContractError(
                    "DELIMITER_REPAIR_INVALID", "delimiter repair source is not text"
                )
            start = span.char_start
            end = span.char_end
            if not any(
                (
                    end <= target_start
                    and (not text[end:target_start] or text[end:target_start].isspace())
                )
                or (
                    target_end <= start
                    and (not text[target_end:start] or text[target_end:start].isspace())
                )
                for target_start, target_end in targets
            ):
                raise PortableContractError(
                    "DELIMITER_REPAIR_INVALID", "delimiter repair is not directly target-adjacent"
                )
            if not _repair_is_causally_empty(
                text,
                selected,
                start=start,
                end=end,
                target_intervals=targets,
                repair_intervals=by_record_repairs[record_id],
                replacement_intervals=by_record_replacements.get(record_id, []),
            ):
                raise PortableContractError(
                    "DELIMITER_REPAIR_INVALID",
                    "delimiter repair is not causally empty after the complete arm edit",
                )


def _assert_only_allowed_paths(
    source: JsonValue,
    rendered: JsonValue,
    *,
    allowed_paths: set[JsonPath],
    path: JsonPath = (),
) -> None:
    if path in allowed_paths:
        return
    if type(source) is not type(rendered):
        raise PortableContractError(
            "PREVIEW_NON_TARGET_DIFF", "render changed a value outside declared target paths"
        )
    if isinstance(source, dict):
        if set(source) != set(cast(dict[str, JsonValue], rendered)):
            raise PortableContractError(
                "PREVIEW_NON_TARGET_DIFF", "render changed keys outside declared target paths"
            )
        rendered_object = cast(dict[str, JsonValue], rendered)
        for key, value in source.items():
            _assert_only_allowed_paths(
                value,
                rendered_object[key],
                allowed_paths=allowed_paths,
                path=(*path, key),
            )
    elif isinstance(source, list):
        rendered_list = cast(list[JsonValue], rendered)
        if len(source) != len(rendered_list):
            raise PortableContractError(
                "PREVIEW_NON_TARGET_DIFF", "render changed a list outside a declared insertion"
            )
        for index, value in enumerate(source):
            _assert_only_allowed_paths(
                value,
                rendered_list[index],
                allowed_paths=allowed_paths,
                path=(*path, index),
            )
    elif source != rendered:
        raise PortableContractError(
            "PREVIEW_NON_TARGET_DIFF", "render changed a scalar outside declared target paths"
        )


def _rendered_history(ir: HistoryIR, result: RenderResult) -> tuple[dict[str, JsonValue], ...]:
    records_by_path: dict[JsonPath, list[str]] = {}
    for record in ir.records:
        records_by_path.setdefault(record.coordinates.request_path, []).append(record.record_id)
    return tuple(
        {
            "container_path": list(path),
            "record_ids": cast(list[JsonValue], record_ids),
            "source_text": cast(str, get_at_path(result.original_request, path)),
            "rendered_text": cast(str, get_at_path(result.rendered_request, path)),
        }
        for path, record_ids in sorted(
            records_by_path.items(),
            key=lambda item: tuple(
                (0, token) if isinstance(token, str) else (1, token) for token in item[0]
            ),
        )
    )


def _render_preview_arms(
    *,
    application_request: JsonValue,
    codec: HistoryCodec,
    ir: HistoryIR,
    plans: tuple[TransformationPlan, ...],
    plan_set_profile: PlanSetProfile,
) -> tuple[str, tuple[PreviewArm, ...]]:
    registry = HistoryCodecRegistry()
    registry.register(codec)
    plan_set_sha256 = validate_plan_set(
        application_request,
        ir,
        plans,
        codec_registry=registry,
        codec_contract_version=codec.contract_version,
        plan_set_profile=plan_set_profile,
        execution_mode=ExecutionMode.G1_SCIENTIFIC,
        failure_policy=FailurePolicy.BLOCK,
    )
    arms: list[PreviewArm] = []
    for plan in plans:
        result = codec.render(
            application_request,
            ir,
            plan,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        receipt = validate_pre_send(
            application_request,
            ir,
            plan,
            result,
            codec_registry=registry,
            codec_contract_version=codec.contract_version,
            paired_plans=plans,
            plan_set_profile=plan_set_profile,
            execution_mode=ExecutionMode.G1_SCIENTIFIC,
            failure_policy=FailurePolicy.BLOCK,
        )
        if (
            receipt.provider_invocation_allowed
            or receipt.provider_decision is not ProviderDecision.BLOCK
            or receipt.invocation_attempted
        ):
            raise PortableContractError(
                "PREVIEW_PROVIDER_AUTHORIZATION_LEAK",
                "read-only preview must stop before provider encoding or send",
            )
        allowed_paths = {operation.target_span.container_path for operation in plan.operations}
        allowed_paths.update(
            operation.correction_anchor.container_path
            for operation in plan.operations
            if operation.correction_anchor is not None
        )
        _assert_only_allowed_paths(
            application_request,
            result.rendered_request,
            allowed_paths=allowed_paths,
        )
        if restore_original(result) != application_request:
            raise PortableContractError(
                "PREVIEW_MAPPING_NOT_REVERSIBLE", "preview cannot restore the exact source request"
            )
        arms.append(
            PreviewArm(
                arm=plan.arm,
                render_result=result,
                rendered_history=_rendered_history(ir, result),
                validation_receipt_sha256=canonical_sha256(receipt.to_dict()),
                target_only_diff=True,
                source_mapping_reversible=True,
            )
        )
    return plan_set_sha256, tuple(arms)


def build_five_arm_preview(
    *,
    application_request: JsonValue,
    codec: HistoryCodec,
    focal_binding_ids: Sequence[str],
    oracle_binding_ids: Sequence[str],
    sham_binding_id: str,
    correction_candidates: Sequence[str],
    correction_evidence_refs: Sequence[EvidenceRef],
    token_counter: PinnedTokenCounter | None,
    delimiter_repairs: Sequence[DelimiterRepairBinding] = (),
) -> FiveArmPreview:
    """Build the strict five-arm CPU preview and stop before provider encoding."""

    request_snapshot = canonical_sha256(application_request)
    ranking = rank_correction_candidates(correction_candidates, token_counter=token_counter)
    ir = codec.extract(application_request)
    ir_snapshot = canonical_sha256(ir.to_dict())
    binding_index = _binding_index(ir)
    if (
        isinstance(focal_binding_ids, (str, bytes))
        or not isinstance(focal_binding_ids, Sequence)
        or isinstance(oracle_binding_ids, (str, bytes))
        or not isinstance(oracle_binding_ids, Sequence)
        or isinstance(correction_evidence_refs, (str, bytes))
        or not isinstance(correction_evidence_refs, Sequence)
        or isinstance(delimiter_repairs, (str, bytes))
        or not isinstance(delimiter_repairs, Sequence)
    ):
        raise PortableContractError(
            "PREVIEW_PLAN_INPUT_INVALID", "preview plan inputs must use closed typed sequences"
        )
    focal = tuple(focal_binding_ids)
    oracle = tuple(oracle_binding_ids)
    correction_refs = tuple(correction_evidence_refs)
    repairs = tuple(delimiter_repairs)
    raw_sham_id = cast(object, sham_binding_id)
    if (
        not focal
        or any(not isinstance(item, str) or not item for item in focal)
        or len(set(focal)) != len(focal)
        or not oracle
        or any(not isinstance(item, str) or not item for item in oracle)
        or len(set(oracle)) != len(oracle)
        or not set(focal) <= set(oracle)
        or not isinstance(raw_sham_id, str)
        or not raw_sham_id
        or not correction_refs
        or any(not isinstance(item, EvidenceRef) for item in correction_refs)
        or any(not isinstance(item, DelimiterRepairBinding) for item in repairs)
    ):
        raise PortableContractError(
            "PREVIEW_PLAN_INPUT_INVALID",
            "strict preview needs unique focal/oracle targets, one sham, and correction evidence",
        )
    correction_anchors: list[PreviewCorrectionAnchor] = []
    for binding_id in focal:
        located = binding_index.get(binding_id)
        if located is None:
            raise PortableContractError(
                "PREVIEW_TARGET_UNKNOWN", "correction anchor references an absent focal binding"
            )
        record, span = located
        if span.span_role is not SpanRole.EDITABLE_CLAIM or len(record.correction_anchors) != 1:
            raise PortableContractError(
                "PREVIEW_CORRECTION_ANCHOR_INVALID",
                "each focal binding must resolve to one exact host-safe correction anchor",
            )
        correction_anchors.append(
            PreviewCorrectionAnchor(
                binding_id=binding_id,
                target_record_id=record.record_id,
                anchor=record.correction_anchors[0],
            )
        )
    by_arm = {
        ArmKind.ORIGINAL: (),
        ArmKind.MASK: focal,
        ArmKind.MASK_CORRECTION: focal,
        ArmKind.ORACLE_CLEAN: oracle,
        ArmKind.SHAM_BENIGN_EDIT: (sham_binding_id,),
    }
    plans = tuple(
        _plan(
            ir=ir,
            arm=arm,
            target_binding_ids=by_arm[arm],
            correction_text=ranking.selected_text,
            correction_evidence_refs=correction_refs,
            repairs=tuple(item for item in repairs if item.arm is arm),
            binding_index=binding_index,
        )
        for arm in ArmKind
    )
    _validate_repairs(
        request=application_request,
        ir=ir,
        plans=plans,
        repairs=repairs,
        binding_index=binding_index,
    )
    sham_match = _sham_token_match(
        binding_index=binding_index,
        focal_binding_ids=focal,
        sham_binding_id=sham_binding_id,
        token_counter=token_counter,
    )
    plan_set_sha256, arms = _render_preview_arms(
        application_request=application_request,
        codec=codec,
        ir=ir,
        plans=plans,
        plan_set_profile=PlanSetProfile.G1_STRICT_MHR,
    )
    if (
        canonical_sha256(application_request) != request_snapshot
        or canonical_sha256(ir.to_dict()) != ir_snapshot
        or tuple(item.arm for item in arms) != tuple(ArmKind)
    ):
        raise PortableContractError(
            "PREVIEW_INPUT_MUTATED", "preview mutated input state or changed canonical arm order"
        )
    return FiveArmPreview(
        codec_id=codec.codec_id,
        source_request_sha256=request_snapshot,
        history_ir_sha256=ir_snapshot,
        plan_set_sha256=plan_set_sha256,
        correction_ranking=ranking,
        correction_anchors=tuple(correction_anchors),
        sham_token_match=sham_match,
        delimiter_repairs=repairs,
        arms=arms,
    )


def build_clean_control_preview(
    *,
    application_request: JsonValue,
    codec: HistoryCodec,
    focal_reference_binding_id: str,
    sham_binding_id: str,
    token_counter: PinnedTokenCounter | None,
    delimiter_repairs: Sequence[DelimiterRepairBinding] = (),
) -> CleanControlPreview:
    """Build the frozen Original/Sham clean-control CPU preview."""

    request_snapshot = canonical_sha256(application_request)
    ir = codec.extract(application_request)
    ir_snapshot = canonical_sha256(ir.to_dict())
    binding_index = _binding_index(ir)
    raw_focal_id = cast(object, focal_reference_binding_id)
    raw_sham_id = cast(object, sham_binding_id)
    if isinstance(delimiter_repairs, (str, bytes)) or not isinstance(delimiter_repairs, Sequence):
        raise PortableContractError(
            "PREVIEW_PLAN_INPUT_INVALID", "delimiter repairs must be a typed sequence"
        )
    repairs = tuple(delimiter_repairs)
    if (
        not isinstance(raw_focal_id, str)
        or not raw_focal_id
        or not isinstance(raw_sham_id, str)
        or not raw_sham_id
        or any(not isinstance(item, DelimiterRepairBinding) for item in repairs)
        or any(item.arm is not ArmKind.SHAM_BENIGN_EDIT for item in repairs)
    ):
        raise PortableContractError(
            "PREVIEW_PLAN_INPUT_INVALID",
            "clean-control preview needs one focal reference, one sham, and only Sham repairs",
        )
    sham_match = _sham_token_match(
        binding_index=binding_index,
        focal_binding_ids=(focal_reference_binding_id,),
        sham_binding_id=sham_binding_id,
        token_counter=token_counter,
    )
    arm_order = (ArmKind.ORIGINAL, ArmKind.SHAM_BENIGN_EDIT)
    by_arm = {
        ArmKind.ORIGINAL: (),
        ArmKind.SHAM_BENIGN_EDIT: (sham_binding_id,),
    }
    plans = tuple(
        _plan(
            ir=ir,
            arm=arm,
            target_binding_ids=by_arm[arm],
            correction_text="",
            correction_evidence_refs=(),
            repairs=tuple(item for item in repairs if item.arm is arm),
            binding_index=binding_index,
        )
        for arm in arm_order
    )
    _validate_repairs(
        request=application_request,
        ir=ir,
        plans=plans,
        repairs=repairs,
        binding_index=binding_index,
    )
    plan_set_sha256, arms = _render_preview_arms(
        application_request=application_request,
        codec=codec,
        ir=ir,
        plans=plans,
        plan_set_profile=PlanSetProfile.G1_CLEAN_CONTROL,
    )
    if (
        canonical_sha256(application_request) != request_snapshot
        or canonical_sha256(ir.to_dict()) != ir_snapshot
        or tuple(item.arm for item in arms) != arm_order
    ):
        raise PortableContractError(
            "PREVIEW_INPUT_MUTATED", "preview mutated input state or changed canonical arm order"
        )
    return CleanControlPreview(
        codec_id=codec.codec_id,
        source_request_sha256=request_snapshot,
        history_ir_sha256=ir_snapshot,
        plan_set_sha256=plan_set_sha256,
        sham_token_match=sham_match,
        delimiter_repairs=repairs,
        arms=arms,
    )
