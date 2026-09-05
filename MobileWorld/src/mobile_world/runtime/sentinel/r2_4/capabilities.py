"""CPU-only R2.4 runtime target discovery over the frozen G1.5 codecs.

The G1.5 codec publication intentionally requires an external, request-bound
catalog before it exposes editable spans.  Runtime requests cannot carry a
pre-published catalog, so this module performs two independent parses:

1. extract the host structure with an empty catalog;
2. derive the single semantic gap inside each protected record envelope;
3. rebuild exact ``CuratedSpanBinding`` values for the current request; and
4. extract again and verify that only the editable-span projection changed.

The frozen G1.5 modules and their ``live_ready=false`` declarations remain
unchanged.  This is an additive CPU capability overlay, not a live-readiness
claim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import cast

from mobile_world.offline.causal_replay.contracts import (
    CodecCapabilities,
    ExecutionMode,
    FailurePolicy,
    HistoryFamily,
    HistoryIR,
    HistoryRecord,
    JsonValue,
    PortableContractError,
    RecordModality,
    RenderResult,
    SourceSpan,
    SpanRole,
    TransformationPlan,
    canonical_sha256,
    copy_json,
    get_at_path,
    stable_id,
)
from mobile_world.offline.causal_replay.core import validate_history_ir
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.g1_history_codecs import (
    CuratedSpanBinding,
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)

R24_RUNTIME_TARGET_DISCOVERY_VERSION = "mobileworld.runtime.sentinel.r2-4-target-discovery/v1"
R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION = (
    "mobileworld.runtime.sentinel.r2-4-history-extraction/v1"
)
R24_NO_HISTORY_REASON = "R24_NO_HISTORY_FIRST_ACTOR_CALL"
R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON = "R24_MAI_CURRENT_TEXT_WITHOUT_SCREENSHOT_UNSUPPORTED"

_QWEN_QUERY_MARKER = "\nThe user query: "
_QWEN_PROGRESS_MARKER = (
    "\nTask progress (You have done the following operation on the current device): "
)
_OVERLAY_IMPLEMENTATION_SHA256 = canonical_sha256(
    {
        "overlay_version": R24_RUNTIME_TARGET_DISCOVERY_VERSION,
        "structural_parser": "frozen_g1_5_empty_binding_extract",
        "discovery": "single_nonempty_text_gap_bounded_by_protected_spans",
        "binding": "current_request_hash_exact_char_and_utf8_offsets",
        "verification": "bound_reextract_exact_structure_and_target_census",
    }
)

_PROTECTED_RUNTIME_ROLES = frozenset(
    {
        SpanRole.PROTECTED_PROTOCOL,
        SpanRole.PROTECTED_EXTERNAL_RESULT,
    }
)
_BINDING_PROVENANCE_KEYS = frozenset(
    {
        "curated_binding_ids",
        "curated_shell_binding_ids",
        "curated_binding_catalog_sha256",
        "binding_source_request_sha256",
    }
)

RuntimeCodecFactory = Callable[[tuple[CuratedSpanBinding, ...]], HistoryCodec]


class RuntimeTargetDiscoveryModeV1(StrEnum):
    PROTECTED_SINGLE_GAP_REEXTRACT = "PROTECTED_SINGLE_GAP_REEXTRACT"


@dataclass(frozen=True)
class RuntimeCodecOverlayDeclarationV1:
    """Versioned identity for the additive runtime interpretation layer."""

    overlay_id: str
    host_id: str
    history_family: HistoryFamily
    base_codec_id: str
    base_codec_contract_version: str
    base_capability_sha256: str
    implementation_sha256: str = _OVERLAY_IMPLEMENTATION_SHA256
    discovery_mode: RuntimeTargetDiscoveryModeV1 = (
        RuntimeTargetDiscoveryModeV1.PROTECTED_SINGLE_GAP_REEXTRACT
    )
    live_ready: bool = False
    schema_version: str = R24_RUNTIME_TARGET_DISCOVERY_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R24_RUNTIME_TARGET_DISCOVERY_VERSION:
            raise ValueError("unknown runtime codec overlay schema version")
        if any(
            not isinstance(value, str)  # type: ignore[redundant-expr]
            or not value
            for value in (
                self.overlay_id,
                self.host_id,
                self.base_codec_id,
                self.base_codec_contract_version,
            )
        ):
            raise ValueError("runtime codec overlay identity fields must be non-empty")
        if not isinstance(self.history_family, HistoryFamily):
            raise TypeError("history_family must be HistoryFamily")
        if not isinstance(self.discovery_mode, RuntimeTargetDiscoveryModeV1):
            raise TypeError("discovery_mode must be RuntimeTargetDiscoveryModeV1")
        if type(self.live_ready) is not bool or self.live_ready:  # type: ignore[redundant-expr]
            raise ValueError("the CPU runtime codec overlay cannot claim live readiness")
        for digest in (self.base_capability_sha256, self.implementation_sha256):
            if (
                not isinstance(digest, str)  # type: ignore[redundant-expr]
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("runtime codec overlay hashes must be lowercase SHA-256")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "overlay_id": self.overlay_id,
            "host_id": self.host_id,
            "history_family": self.history_family.value,
            "base_codec_id": self.base_codec_id,
            "base_codec_contract_version": self.base_codec_contract_version,
            "base_capability_sha256": self.base_capability_sha256,
            "implementation_sha256": self.implementation_sha256,
            "discovery_mode": self.discovery_mode.value,
            "live_ready": self.live_ready,
        }


class RuntimeHistoryExtractionStatusV1(StrEnum):
    READY = "READY"
    NO_HISTORY = "NO_HISTORY"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class RuntimeHistoryExtractionResultV1:
    """Exact R2.4 pre-admission result, including the zero-target bridge."""

    status: RuntimeHistoryExtractionStatusV1
    raw_request_sha256: str
    overlay: RuntimeCodecOverlayDeclarationV1
    capabilities: CodecCapabilities
    history_ir: HistoryIR | None
    reason_code: str | None
    validation_checks: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: str = R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION:
            raise ValueError("unknown runtime history extraction schema version")
        if not isinstance(self.status, RuntimeHistoryExtractionStatusV1):
            raise TypeError("status must be RuntimeHistoryExtractionStatusV1")
        if type(self.overlay) is not RuntimeCodecOverlayDeclarationV1:
            raise TypeError("overlay must be an exact RuntimeCodecOverlayDeclarationV1")
        if type(self.capabilities) is not CodecCapabilities:
            raise TypeError("capabilities must be an exact CodecCapabilities")
        if canonical_sha256(self.capabilities.to_dict()) != self.overlay.base_capability_sha256:
            raise ValueError("runtime extraction capabilities differ from the overlay declaration")
        if (
            not isinstance(self.raw_request_sha256, str)  # type: ignore[redundant-expr]
            or len(self.raw_request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.raw_request_sha256)
        ):
            raise ValueError("runtime extraction request hash must be lowercase SHA-256")
        if (
            not isinstance(self.validation_checks, tuple)  # type: ignore[redundant-expr]
            or not self.validation_checks
            or any(
                not isinstance(item, str) or not item  # type: ignore[redundant-expr]
                for item in self.validation_checks
            )
        ):
            raise ValueError("runtime extraction validation checks must be non-empty strings")
        if not isinstance(self.warnings, tuple) or any(  # type: ignore[redundant-expr]
            not isinstance(item, str) or not item  # type: ignore[redundant-expr]
            for item in self.warnings
        ):
            raise ValueError("runtime extraction warnings must be non-empty strings")
        if self.status is RuntimeHistoryExtractionStatusV1.READY:
            if type(self.history_ir) is not HistoryIR or self.reason_code is not None:
                raise ValueError("READY extraction requires one HistoryIR and no reason code")
            if (
                self.history_ir.raw_request_sha256 != self.raw_request_sha256
                or self.history_ir.host_id != self.overlay.host_id
                or self.history_ir.codec_id != self.overlay.base_codec_id
                or self.history_ir.codec_contract_version
                != self.overlay.base_codec_contract_version
                or self.history_ir.capabilities != self.capabilities
                or self.history_ir.warnings != self.warnings
            ):
                raise ValueError("READY extraction is not bound to its request and overlay")
        elif (
            self.history_ir is not None
            or not isinstance(self.reason_code, str)
            or not self.reason_code
        ):
            raise ValueError("non-READY extraction requires no HistoryIR and one reason code")
        if (
            self.status is RuntimeHistoryExtractionStatusV1.NO_HISTORY
            and self.reason_code != R24_NO_HISTORY_REASON
        ):
            raise ValueError("NO_HISTORY extraction requires the canonical reason code")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "raw_request_sha256": self.raw_request_sha256,
            "overlay": self.overlay.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "history_ir": None if self.history_ir is None else self.history_ir.to_dict(),
            "reason_code": self.reason_code,
            "validation_checks": list(self.validation_checks),
            "warnings": list(self.warnings),
        }


def _portable_error(code: str, message: str) -> PortableContractError:
    return PortableContractError(code, message)


def _request_messages(
    request: JsonValue,
) -> tuple[dict[str, JsonValue], list[JsonValue]] | None:
    if not isinstance(request, dict) or not isinstance(request.get("model"), str):
        return None
    if not request["model"]:
        return None
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    return request, messages


def _message(
    messages: list[JsonValue],
    index: int,
) -> dict[str, JsonValue] | None:
    try:
        value = messages[index]
    except IndexError:
        return None
    return value if isinstance(value, dict) else None


def _single_content_block(
    message: dict[str, JsonValue],
) -> dict[str, JsonValue] | None:
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    return block if isinstance(block, dict) else None


def _is_exact_image_block(block: dict[str, JsonValue]) -> bool:
    image_url = block.get("image_url")
    return (
        block.get("type") == "image_url"
        and isinstance(image_url, dict)
        and isinstance(image_url.get("url"), str)
        and bool(image_url["url"])
    )


def _qwen_no_history_checks(request: JsonValue) -> tuple[str, ...] | None:
    parsed = _request_messages(request)
    if parsed is None:
        return None
    _root, messages = parsed
    if len(messages) != 2:
        return None
    system = _message(messages, 0)
    user = _message(messages, 1)
    if system is None or user is None:
        return None
    if system.get("role") != "system" or user.get("role") != "user":
        return None
    system_content = system.get("content")
    user_content = user.get("content")
    if (
        not isinstance(system_content, list)
        or len(system_content) != 1
        or not isinstance(system_content[0], dict)
        or system_content[0].get("type") != "text"
        or not isinstance(system_content[0].get("text"), str)
        or not system_content[0]["text"]
        or not isinstance(user_content, list)
        or len(user_content) != 2
        or not isinstance(user_content[0], dict)
        or not isinstance(user_content[1], dict)
        or user_content[0].get("type") != "text"
        or not _is_exact_image_block(user_content[1])
    ):
        return None
    text = user_content[0].get("text")
    if not isinstance(text, str):
        return None
    if text.count(_QWEN_QUERY_MARKER) != 1 or text.count(_QWEN_PROGRESS_MARKER) != 1:
        return None
    query_start = text.index(_QWEN_QUERY_MARKER)
    progress_start = text.index(_QWEN_PROGRESS_MARKER)
    history_start = progress_start + len(_QWEN_PROGRESS_MARKER)
    if (
        query_start != 0
        or progress_start <= query_start + len(_QWEN_QUERY_MARKER)
        or not text[query_start + len(_QWEN_QUERY_MARKER) : progress_start].strip()
        or not text.endswith("\n")
        or history_start != len(text) - 1
    ):
        return None
    return (
        "canonical_request_bound",
        "qwen_first_call_exact_shape",
        "current_image_present",
        "history_empty",
    )


def _mai_common_messages(
    request: JsonValue,
) -> tuple[list[JsonValue], dict[str, JsonValue]] | None:
    parsed = _request_messages(request)
    if parsed is None:
        return None
    _root, messages = parsed
    if len(messages) < 3:
        return None
    system = _message(messages, 0)
    task = _message(messages, 1)
    current = _message(messages, len(messages) - 1)
    if system is None or task is None or current is None:
        return None
    task_block = _single_content_block(task)
    if (
        system.get("role") != "system"
        or not isinstance(system.get("content"), str)
        or not system["content"]
        or task.get("role") != "user"
        or task_block is None
        or task_block.get("type") != "text"
        or not isinstance(task_block.get("text"), str)
        or not task_block["text"]
        or current.get("role") != "user"
    ):
        return None
    return messages, current


def _mai_no_history_checks(request: JsonValue) -> tuple[str, ...] | None:
    parsed = _mai_common_messages(request)
    if parsed is None:
        return None
    messages, current = parsed
    block = _single_content_block(current)
    if len(messages) != 3 or block is None or not _is_exact_image_block(block):
        return None
    return (
        "canonical_request_bound",
        "mai_first_call_exact_shape",
        "current_image_present",
        "history_empty",
    )


def _base_provenance(record: HistoryRecord) -> dict[str, JsonValue]:
    return {
        key: value
        for key, value in record.provenance.items()
        if key not in _BINDING_PROVENANCE_KEYS
    }


def _validate_structural_record(record: HistoryRecord) -> None:
    if record.editable_spans:
        raise _portable_error(
            "R24_STRUCTURAL_EDITABLE_SPAN_PRESENT",
            "the first runtime extraction must not inherit a target catalog",
        )
    if not record.protected_spans:
        raise _portable_error(
            "R24_PROTECTED_BOUNDARY_MISSING",
            "runtime target discovery requires an exact protected record envelope",
        )
    source = record.source_span
    prior_end = source.char_start
    for protected in sorted(
        record.protected_spans,
        key=lambda item: (item.char_start, item.char_end, item.span_sha256),
    ):
        if (
            protected.container_path != source.container_path
            or protected.span_role not in _PROTECTED_RUNTIME_ROLES
            or protected.char_start < source.char_start
            or protected.char_end > source.char_end
        ):
            raise _portable_error(
                "R24_PROTECTED_BOUNDARY_INVALID",
                "protected spans must be trusted, in-record spans on the record container",
            )
        if protected.char_start < prior_end:
            raise _portable_error(
                "R24_PROTECTED_BOUNDARY_OVERLAP",
                "protected spans overlap inside one structural record",
            )
        prior_end = protected.char_end


def _record_uncovered_ranges(record: HistoryRecord) -> tuple[tuple[int, int], ...]:
    """Return exact source-span ranges not occupied by protected bytes."""

    _validate_structural_record(record)
    source = record.source_span
    cursor = source.char_start
    gaps: list[tuple[int, int]] = []
    for protected in sorted(
        record.protected_spans,
        key=lambda item: (item.char_start, item.char_end, item.span_sha256),
    ):
        if cursor < protected.char_start:
            gaps.append((cursor, protected.char_start))
        cursor = protected.char_end
    if cursor < source.char_end:
        gaps.append((cursor, source.char_end))
    return tuple(gaps)


def discover_runtime_editable_bindings(
    application_request: JsonValue,
    structural_ir: HistoryIR,
) -> tuple[CuratedSpanBinding, ...]:
    """Derive current-request target bindings from one frozen structural IR.

    A record is targetable only when its protected spans leave exactly one
    non-empty semantic gap.  Fully protected records remain non-editable, and
    multiple gaps fail closed instead of guessing.
    """

    request = copy_json(application_request)
    validate_history_ir(request, structural_ir)
    request_sha256 = canonical_sha256(request)
    if structural_ir.raw_request_sha256 != request_sha256:
        raise _portable_error(
            "R24_STRUCTURAL_REQUEST_MISMATCH",
            "structural History IR is not bound to the current request",
        )

    bindings: list[CuratedSpanBinding] = []
    for record in structural_ir.records:
        gaps = _record_uncovered_ranges(record)
        if not gaps:
            continue
        if len(gaps) != 1:
            raise _portable_error(
                "R24_EDITABLE_GAP_AMBIGUOUS",
                "one structural history record exposes multiple editable gaps",
            )
        if record.modality is not RecordModality.TEXT:
            raise _portable_error(
                "R24_EDITABLE_GAP_NON_TEXT",
                "runtime editable gaps are restricted to text history records",
            )
        start, end = gaps[0]
        if start == record.source_span.char_start or end == record.source_span.char_end:
            raise _portable_error(
                "R24_EDITABLE_GAP_UNBOUNDED",
                "runtime editable text must be bounded by protected bytes on both sides",
            )
        container = get_at_path(request, record.source_span.container_path)
        if not isinstance(container, str):
            raise _portable_error(
                "R24_EDITABLE_CONTAINER_NOT_TEXT",
                "runtime editable gap does not resolve to a text container",
            )
        exact_text = container[start:end]
        if not exact_text or not exact_text.strip():
            raise _portable_error(
                "R24_EDITABLE_GAP_EMPTY",
                "runtime editable gap must contain non-whitespace semantic text",
            )
        binding_id = stable_id(
            "r24-runtime-target",
            {
                "overlay_version": R24_RUNTIME_TARGET_DISCOVERY_VERSION,
                "host_id": structural_ir.host_id,
                "codec_id": structural_ir.codec_id,
                "codec_contract_version": structural_ir.codec_contract_version,
                "source_request_sha256": request_sha256,
                "record_id": record.record_id,
                "container_path": list(record.source_span.container_path),
                "char_start": start,
                "char_end": end,
                "exact_text_sha256": canonical_sha256(exact_text),
            },
        )
        bindings.append(
            CuratedSpanBinding.from_text(
                binding_id=binding_id,
                source_request_sha256=request_sha256,
                container_path=record.source_span.container_path,
                container_text=container,
                char_start=start,
                char_end=end,
                span_role=SpanRole.EDITABLE_CLAIM,
            )
        )

    return tuple(
        sorted(
            bindings,
            key=lambda item: (
                tuple(
                    (0, token) if isinstance(token, str) else (1, token)
                    for token in item.container_path
                ),
                item.char_start,
                item.char_end,
                item.binding_id,
            ),
        )
    )


def _validate_record_stability(
    structural: HistoryRecord,
    discovered: HistoryRecord,
) -> None:
    for item in fields(HistoryRecord):
        if item.name == "editable_spans":
            continue
        if item.name == "provenance":
            if _base_provenance(structural) != _base_provenance(discovered):
                raise _portable_error(
                    "R24_RECORD_PROVENANCE_DRIFT",
                    "runtime target discovery changed structural record provenance",
                )
            continue
        if getattr(structural, item.name) != getattr(discovered, item.name):
            raise _portable_error(
                "R24_RECORD_STRUCTURE_DRIFT",
                f"runtime target discovery changed record field {item.name}",
            )


def _binding_key(binding: CuratedSpanBinding) -> tuple[object, ...]:
    return (
        binding.container_path,
        binding.char_start,
        binding.char_end,
        binding.utf8_byte_start,
        binding.utf8_byte_end,
        binding.exact_text,
        binding.span_sha256,
        binding.span_role,
    )


def _editable_key(item: SourceSpan) -> tuple[object, ...]:
    return (
        item.container_path,
        item.char_start,
        item.char_end,
        item.utf8_byte_start,
        item.utf8_byte_end,
        item.exact_text,
        item.span_sha256,
        item.span_role,
    )


def _validate_discovered_ir(
    request: JsonValue,
    structural_ir: HistoryIR,
    discovered_ir: HistoryIR,
    bindings: tuple[CuratedSpanBinding, ...],
) -> None:
    validate_history_ir(request, discovered_ir)
    for label in (
        "host_id",
        "history_family",
        "codec_id",
        "codec_contract_version",
        "raw_request_sha256",
        "regions",
        "source_versions",
        "capabilities",
        "warnings",
    ):
        if getattr(structural_ir, label) != getattr(discovered_ir, label):
            raise _portable_error(
                "R24_HISTORY_IR_STRUCTURE_DRIFT",
                f"runtime target discovery changed History IR field {label}",
            )
    if len(structural_ir.records) != len(discovered_ir.records):
        raise _portable_error(
            "R24_RECORD_CENSUS_DRIFT",
            "runtime target discovery changed the structural record census",
        )
    for structural, discovered in zip(structural_ir.records, discovered_ir.records, strict=True):
        _validate_record_stability(structural, discovered)

    expected = {_binding_key(item) for item in bindings}
    observed = {
        _editable_key(span) for record in discovered_ir.records for span in record.editable_spans
    }
    if expected != observed or len(expected) != len(bindings):
        raise _portable_error(
            "R24_EDITABLE_BINDING_CENSUS_MISMATCH",
            "second extraction did not expose exactly the discovered runtime bindings",
        )
    for record in discovered_ir.records:
        for editable in record.editable_spans:
            if editable.span_role is not SpanRole.EDITABLE_CLAIM or not editable.claim_id:
                raise _portable_error(
                    "R24_EDITABLE_BINDING_INVALID",
                    "runtime target must be an exact content-addressed editable claim",
                )
            if any(
                editable.container_path == protected.container_path
                and editable.char_start < protected.char_end
                and protected.char_start < editable.char_end
                for protected in record.protected_spans
            ):
                raise _portable_error(
                    "R24_EDITABLE_PROTECTED_OVERLAP",
                    "runtime target overlaps protected protocol or external-result bytes",
                )


class RuntimeEditableSpanCodecV1:
    """Stateless two-pass adapter around one frozen G1.5 codec factory."""

    def __init__(self, factory: RuntimeCodecFactory) -> None:
        if not callable(factory):
            raise TypeError("factory must be callable")
        prototype = factory(())
        if not isinstance(prototype, HistoryCodec):
            raise TypeError("factory must produce a HistoryCodec")
        self._factory = factory
        self._codec_id = prototype.codec_id
        self._contract_version = prototype.contract_version
        self._history_family = prototype.history_family
        self._capabilities = prototype.capabilities
        host_id = getattr(prototype, "host_id", None)
        if not isinstance(host_id, str) or not host_id:
            raise TypeError("frozen runtime codec must declare a non-empty host_id")
        self._host_id = host_id
        if self._capabilities.live_ready:
            raise ValueError("the frozen G1.5 codec must retain live_ready=false")
        self._overlay = RuntimeCodecOverlayDeclarationV1(
            overlay_id=f"{self._codec_id}.r2-4-runtime-overlay",
            host_id=self._host_id,
            history_family=self._history_family,
            base_codec_id=self._codec_id,
            base_codec_contract_version=self._contract_version,
            base_capability_sha256=canonical_sha256(self._capabilities.to_dict()),
        )
        self._overlay_warning = f"R24_RUNTIME_TARGET_DISCOVERY_OVERLAY:{self._overlay.sha256}"

    @property
    def codec_id(self) -> str:
        return self._codec_id

    @property
    def contract_version(self) -> str:
        return self._contract_version

    @property
    def history_family(self) -> HistoryFamily:
        return self._history_family

    @property
    def capabilities(self) -> CodecCapabilities:
        return self._capabilities

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def overlay_declaration(self) -> RuntimeCodecOverlayDeclarationV1:
        return self._overlay

    def _fresh_structural_codec(self) -> HistoryCodec:
        codec = self._factory(())
        if (
            not isinstance(codec, HistoryCodec)  # type: ignore[redundant-expr]
            or codec.codec_id != self.codec_id
            or codec.contract_version != self.contract_version
            or codec.history_family is not self.history_family
            or codec.capabilities != self.capabilities
            or getattr(codec, "host_id", None) != self.host_id
        ):
            raise _portable_error(
                "R24_CODEC_FACTORY_DRIFT",
                "runtime codec factory drifted from its registered declaration",
            )
        return codec

    def extract(self, application_request: JsonValue) -> HistoryIR:
        request = copy_json(application_request)
        structural_codec = self._fresh_structural_codec()
        structural_ir = structural_codec.extract(request)
        bindings = discover_runtime_editable_bindings(request, structural_ir)
        bound_codec = self._factory(bindings)
        if (
            not isinstance(bound_codec, HistoryCodec)  # type: ignore[redundant-expr]
            or bound_codec.codec_id != self.codec_id
            or bound_codec.contract_version != self.contract_version
            or bound_codec.history_family is not self.history_family
            or bound_codec.capabilities != self.capabilities
            or getattr(bound_codec, "host_id", None) != self.host_id
        ):
            raise _portable_error(
                "R24_CODEC_FACTORY_DRIFT",
                "bound runtime codec drifted from its registered declaration",
            )
        discovered_ir = bound_codec.extract(request)
        _validate_discovered_ir(request, structural_ir, discovered_ir, bindings)
        result = replace(
            discovered_ir,
            warnings=(*discovered_ir.warnings, self._overlay_warning),
        )
        validate_history_ir(request, result)
        return result

    def _mai_current_text_shape_is_valid(self, request: JsonValue) -> bool:
        parsed = _mai_common_messages(request)
        if parsed is None:
            return False
        messages, current = parsed
        block = _single_content_block(current)
        if (
            block is None
            or block.get("type") != "text"
            or not isinstance(block.get("text"), str)
            or not block["text"]
        ):
            return False
        if len(messages) == 3:
            return True

        probe = copy_json(request)
        if not isinstance(probe, dict) or not isinstance(probe.get("messages"), list):
            return False
        probe_messages = cast(list[JsonValue], probe["messages"])
        # The extra value is only a frozen-parser shape sentinel.  It is never
        # returned, hashed as evidence, or substituted for the missing current
        # screenshot.  Leaving the real text in place makes the frozen parser
        # verify its historical assistant/result adjacency as well.
        probe_messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,R24_SHAPE_PROBE"},
                    }
                ],
            }
        )
        try:
            self._fresh_structural_codec().extract(probe)
        except PortableContractError:
            return False
        return True

    def extract_runtime(
        self,
        application_request: JsonValue,
    ) -> RuntimeHistoryExtractionResultV1:
        """Extract targets or return an exact pre-policy bypass classification."""

        request = copy_json(application_request)
        request_sha256 = canonical_sha256(request)
        try:
            history_ir = self.extract(request)
        except PortableContractError as error:
            no_history_checks: tuple[str, ...] | None = None
            if (
                self.history_family is HistoryFamily.FLAT_PROGRESS
                and error.code == "EMPTY_HISTORY_IR"
            ):
                no_history_checks = _qwen_no_history_checks(request)
            elif (
                self.history_family is HistoryFamily.RAW_REPLAY
                and error.code == "MAI_MESSAGE_SHAPE_MISMATCH"
            ):
                no_history_checks = _mai_no_history_checks(request)
            if no_history_checks is not None:
                return RuntimeHistoryExtractionResultV1(
                    status=RuntimeHistoryExtractionStatusV1.NO_HISTORY,
                    raw_request_sha256=request_sha256,
                    overlay=self.overlay_declaration,
                    capabilities=self.capabilities,
                    history_ir=None,
                    reason_code=R24_NO_HISTORY_REASON,
                    validation_checks=no_history_checks,
                    warnings=(R24_NO_HISTORY_REASON, self._overlay_warning),
                )
            if (
                self.history_family is HistoryFamily.RAW_REPLAY
                and self._mai_current_text_shape_is_valid(request)
            ):
                return RuntimeHistoryExtractionResultV1(
                    status=RuntimeHistoryExtractionStatusV1.UNSUPPORTED,
                    raw_request_sha256=request_sha256,
                    overlay=self.overlay_declaration,
                    capabilities=self.capabilities,
                    history_ir=None,
                    reason_code=R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON,
                    validation_checks=(
                        "canonical_request_bound",
                        "mai_current_text_exact_host_shape",
                        "current_screenshot_not_model_visible",
                    ),
                    warnings=(self._overlay_warning,),
                )
            return RuntimeHistoryExtractionResultV1(
                status=RuntimeHistoryExtractionStatusV1.UNSUPPORTED,
                raw_request_sha256=request_sha256,
                overlay=self.overlay_declaration,
                capabilities=self.capabilities,
                history_ir=None,
                reason_code=error.code,
                validation_checks=(
                    "canonical_request_bound",
                    "frozen_structure_extract_rejected",
                ),
                warnings=(self._overlay_warning,),
            )
        return RuntimeHistoryExtractionResultV1(
            status=RuntimeHistoryExtractionStatusV1.READY,
            raw_request_sha256=request_sha256,
            overlay=self.overlay_declaration,
            capabilities=self.capabilities,
            history_ir=history_ir,
            reason_code=None,
            validation_checks=(
                "frozen_structure_extract",
                "protected_single_gap_discovery",
                "request_bound_reextract",
                "record_region_invariance",
            ),
            warnings=history_ir.warnings,
        )

    def render(
        self,
        application_request: JsonValue,
        ir: HistoryIR,
        plan: TransformationPlan,
        *,
        execution_mode: ExecutionMode,
        failure_policy: FailurePolicy,
    ) -> RenderResult:
        return self._fresh_structural_codec().render(
            application_request,
            ir,
            plan,
            execution_mode=execution_mode,
            failure_policy=failure_policy,
        )


def _qwen_factory(bindings: tuple[CuratedSpanBinding, ...]) -> HistoryCodec:
    return QwenFlatProgressHistoryCodec(bindings)


def _mai_factory(bindings: tuple[CuratedSpanBinding, ...]) -> HistoryCodec:
    return MaiRawReplayHistoryCodec(bindings)


_DEFAULT_FACTORIES: tuple[RuntimeCodecFactory, ...] = (
    _qwen_factory,
    _mai_factory,
)


class RuntimeHistoryCodecResolverV1:
    """Resolver with codec-ID registration and no actor-model branching."""

    def __init__(
        self,
        factories: tuple[RuntimeCodecFactory, ...] = _DEFAULT_FACTORIES,
    ) -> None:
        declarations: dict[
            tuple[str, str],
            tuple[RuntimeCodecOverlayDeclarationV1, RuntimeCodecFactory],
        ] = {}
        for factory in factories:
            codec = RuntimeEditableSpanCodecV1(factory)
            key = (codec.codec_id, codec.contract_version)
            if key in declarations:
                raise _portable_error(
                    "R24_DUPLICATE_CODEC_ID",
                    "runtime codec ID and contract version must be unique",
                )
            declarations[key] = (codec.overlay_declaration, factory)
        self._declarations = declarations

    @property
    def overlay_declarations(self) -> tuple[RuntimeCodecOverlayDeclarationV1, ...]:
        return tuple(
            declaration
            for declaration, _factory in (
                self._declarations[key] for key in sorted(self._declarations)
            )
        )

    def by_id(
        self,
        codec_id: str,
        contract_version: str = "v1",
    ) -> RuntimeEditableSpanCodecV1:
        try:
            declaration, factory = self._declarations[(codec_id, contract_version)]
        except KeyError as exc:
            raise _portable_error(
                "UNKNOWN_CODEC",
                "runtime history codec is not registered",
            ) from exc
        codec = RuntimeEditableSpanCodecV1(factory)
        if codec.overlay_declaration != declaration:
            raise _portable_error(
                "R24_CODEC_FACTORY_DRIFT",
                "resolved runtime codec differs from its registered declaration",
            )
        return codec


def build_runtime_history_codec_resolver() -> RuntimeHistoryCodecResolverV1:
    """Build the default Qwen/MAI runtime resolver for ``PromptSentinel``."""

    return RuntimeHistoryCodecResolverV1()


__all__ = [
    "R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON",
    "R24_NO_HISTORY_REASON",
    "R24_RUNTIME_HISTORY_EXTRACTION_SCHEMA_VERSION",
    "R24_RUNTIME_TARGET_DISCOVERY_VERSION",
    "RuntimeCodecFactory",
    "RuntimeCodecOverlayDeclarationV1",
    "RuntimeEditableSpanCodecV1",
    "RuntimeHistoryExtractionResultV1",
    "RuntimeHistoryExtractionStatusV1",
    "RuntimeHistoryCodecResolverV1",
    "RuntimeTargetDiscoveryModeV1",
    "build_runtime_history_codec_resolver",
    "discover_runtime_editable_bindings",
]
