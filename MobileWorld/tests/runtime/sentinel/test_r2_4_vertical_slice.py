from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from mobile_world.offline.causal_replay.contracts import (
    HistoryCodecResolver,
    HistoryIR,
    JsonValue,
    PortableContractError,
    SpanRole,
    canonical_sha256,
)
from mobile_world.offline.causal_replay.history_codec import HistoryCodec
from mobile_world.offline.g1_history_codecs import (
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.runtime.sentinel.r2_4 import (
    R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON,
    R24_NO_HISTORY_REASON,
    R24_RUNTIME_TARGET_DISCOVERY_VERSION,
    RuntimeHistoryCodecResolverV1,
    RuntimeHistoryExtractionStatusV1,
    build_runtime_history_codec_resolver,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_ROOT = REPO_ROOT / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs"


@dataclass(frozen=True)
class _RuntimeCodecCase:
    name: str
    fixture_name: str
    codec_type: type[QwenFlatProgressHistoryCodec] | type[MaiRawReplayHistoryCodec]
    expected_editable_text: tuple[str, ...]
    expected_fully_protected_records: int


CASES = (
    _RuntimeCodecCase(
        name="qwen",
        fixture_name="qwen_flat_progress.captured.v1.json",
        codec_type=QwenFlatProgressHistoryCodec,
        expected_editable_text=("已打开设置🙂", "已进入显示页面", "已查看主题选项"),
        expected_fully_protected_records=0,
    ),
    _RuntimeCodecCase(
        name="mai",
        fixture_name="mai_raw_replay.captured.v1.json",
        codec_type=MaiRawReplayHistoryCodec,
        expected_editable_text=("已打开设置🙂", "已进入显示页面", "已查看主题选项"),
        expected_fully_protected_records=1,
    ),
)


def _request(case: _RuntimeCodecCase) -> dict[str, JsonValue]:
    value = cast(
        dict[str, Any],
        json.loads((FIXTURE_ROOT / case.fixture_name).read_text(encoding="utf-8")),
    )["application_request"]
    return cast(dict[str, JsonValue], deepcopy(value))


def _without_binding_provenance(ir: HistoryIR) -> tuple[tuple[object, ...], ...]:
    ignored = {
        "curated_binding_ids",
        "curated_shell_binding_ids",
        "curated_binding_catalog_sha256",
        "binding_source_request_sha256",
    }
    return tuple(
        (
            record.record_id,
            record.record_key,
            record.record_class,
            record.region_id,
            record.role,
            record.author,
            record.modality,
            record.coordinates,
            record.record_sha256,
            record.source_span,
            record.protected_spans,
            record.write_time,
            record.exposure_time,
            {key: value for key, value in record.provenance.items() if key not in ignored},
            record.correction_anchors,
            record.relationships,
            record.related_content,
            record.version,
            record.source_version_ids,
        )
        for record in ir.records
    )


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.name)
def test_runtime_resolver_discovers_exact_gaps_without_changing_frozen_structure(
    case: _RuntimeCodecCase,
) -> None:
    request = _request(case)
    request_before = deepcopy(request)
    structural = case.codec_type().extract(cast(JsonValue, request))
    assert all(not record.editable_spans for record in structural.records)

    resolver = build_runtime_history_codec_resolver()
    assert isinstance(resolver, HistoryCodecResolver)
    codec = resolver.by_id(structural.codec_id, structural.codec_contract_version)
    assert isinstance(codec, HistoryCodec)
    discovered = codec.extract(cast(JsonValue, request))

    editables = tuple(span for record in discovered.records for span in record.editable_spans)
    assert tuple(span.exact_text for span in editables) == case.expected_editable_text
    assert all(span.span_role is SpanRole.EDITABLE_CLAIM for span in editables)
    assert all(span.claim_id is not None for span in editables)
    assert (
        len([record for record in discovered.records if not record.editable_spans])
        == case.expected_fully_protected_records
    )

    assert discovered.regions == structural.regions
    assert discovered.source_versions == structural.source_versions
    assert discovered.capabilities == structural.capabilities
    assert discovered.warnings[:-1] == structural.warnings
    assert discovered.warnings[-1].startswith("R24_RUNTIME_TARGET_DISCOVERY_OVERLAY:")
    assert _without_binding_provenance(discovered) == _without_binding_provenance(structural)
    assert discovered.raw_request_sha256 == canonical_sha256(cast(JsonValue, request))
    assert request == request_before

    for record in discovered.records:
        for editable in record.editable_spans:
            assert record.source_span.char_start <= editable.char_start
            assert editable.char_end <= record.source_span.char_end
            assert not any(
                editable.container_path == protected.container_path
                and editable.char_start < protected.char_end
                and protected.char_start < editable.char_end
                for protected in record.protected_spans
            )


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.name)
def test_runtime_resolver_is_deterministic_and_returns_fresh_codecs(
    case: _RuntimeCodecCase,
) -> None:
    request = _request(case)
    resolver = RuntimeHistoryCodecResolverV1()
    first_codec = resolver.by_id(case.codec_type().codec_id)
    second_codec = resolver.by_id(case.codec_type().codec_id)
    first = first_codec.extract(cast(JsonValue, request))
    second = second_codec.extract(cast(JsonValue, request))

    assert first_codec is not second_codec
    assert first == second
    assert first is not second
    assert tuple(
        span.claim_id for record in first.records for span in record.editable_spans
    ) == tuple(span.claim_id for record in second.records for span in record.editable_spans)


def test_runtime_resolver_rejects_unknown_codec_without_family_or_model_guessing() -> None:
    resolver = RuntimeHistoryCodecResolverV1()

    with pytest.raises(PortableContractError, match="not registered"):
        resolver.by_id("mobileworld.g1.history-codec.unknown")

    with pytest.raises(PortableContractError, match="not registered"):
        resolver.by_id(QwenFlatProgressHistoryCodec().codec_id, "v2")


def test_runtime_overlay_declarations_are_host_bound_and_keep_frozen_readiness() -> None:
    resolver = RuntimeHistoryCodecResolverV1()
    declarations = resolver.overlay_declarations

    assert len(declarations) == 2
    assert len({item.overlay_id for item in declarations}) == 2
    assert len({item.host_id for item in declarations}) == 2
    assert len({item.base_codec_id for item in declarations}) == 2
    assert {item.schema_version for item in declarations} == {R24_RUNTIME_TARGET_DISCOVERY_VERSION}
    assert len({item.implementation_sha256 for item in declarations}) == 1
    assert all(not item.live_ready for item in declarations)
    assert all("model" not in item.to_dict() for item in declarations)
    assert all(
        not resolver.by_id(item.base_codec_id).capabilities.live_ready for item in declarations
    )


@pytest.mark.parametrize("case", CASES, ids=lambda item: item.name)
def test_extract_runtime_ready_binds_overlay_identity(case: _RuntimeCodecCase) -> None:
    request = _request(case)
    codec = RuntimeHistoryCodecResolverV1().by_id(case.codec_type().codec_id)

    result = codec.extract_runtime(cast(JsonValue, request))

    assert result.status is RuntimeHistoryExtractionStatusV1.READY
    assert result.history_ir is not None
    assert result.reason_code is None
    assert result.overlay == codec.overlay_declaration
    assert result.raw_request_sha256 == canonical_sha256(cast(JsonValue, request))
    assert result.history_ir.warnings == result.warnings
    assert result.to_dict()["overlay"] == codec.overlay_declaration.to_dict()


def test_qwen_first_call_is_typed_no_history_without_weakening_g1_ir() -> None:
    case = CASES[0]
    request = _request(case)
    text_block = cast(
        dict[str, JsonValue],
        cast(list[JsonValue], request["messages"])[1]["content"][0],  # type: ignore[index]
    )
    text = cast(str, text_block["text"])
    text_block["text"] = f"{text[: text.index('Step 1: ')]}\n"
    codec = RuntimeHistoryCodecResolverV1().by_id(case.codec_type().codec_id)

    with pytest.raises(PortableContractError) as error:
        codec.extract(cast(JsonValue, request))
    assert error.value.code == "EMPTY_HISTORY_IR"

    result = codec.extract_runtime(cast(JsonValue, request))
    assert result.status is RuntimeHistoryExtractionStatusV1.NO_HISTORY
    assert result.history_ir is None
    assert result.reason_code == R24_NO_HISTORY_REASON
    assert "qwen_first_call_exact_shape" in result.validation_checks


def test_mai_first_call_is_typed_no_history_without_synthetic_record() -> None:
    case = CASES[1]
    request = _request(case)
    messages = cast(list[JsonValue], request["messages"])
    request["messages"] = [deepcopy(messages[0]), deepcopy(messages[1]), deepcopy(messages[-1])]
    codec = RuntimeHistoryCodecResolverV1().by_id(case.codec_type().codec_id)

    with pytest.raises(PortableContractError) as error:
        codec.extract(cast(JsonValue, request))
    assert error.value.code == "MAI_MESSAGE_SHAPE_MISMATCH"

    result = codec.extract_runtime(cast(JsonValue, request))
    assert result.status is RuntimeHistoryExtractionStatusV1.NO_HISTORY
    assert result.history_ir is None
    assert result.reason_code == R24_NO_HISTORY_REASON
    assert "mai_first_call_exact_shape" in result.validation_checks


@pytest.mark.parametrize(
    "current_text",
    (
        'Tool call result: {"status":"screen unchanged"}',
        "继续",
    ),
    ids=("tool-result", "ask-user-response"),
)
def test_mai_current_text_is_typed_unsupported_without_guessing_a_screenshot(
    current_text: str,
) -> None:
    case = CASES[1]
    request = _request(case)
    messages = cast(list[JsonValue], request["messages"])
    current = cast(dict[str, JsonValue], messages[-1])
    current["content"] = [{"type": "text", "text": current_text}]
    codec = RuntimeHistoryCodecResolverV1().by_id(case.codec_type().codec_id)

    with pytest.raises(PortableContractError) as error:
        codec.extract(cast(JsonValue, request))
    assert error.value.code == "MAI_CONTENT_SHAPE_MISMATCH"

    result = codec.extract_runtime(cast(JsonValue, request))
    assert result.status is RuntimeHistoryExtractionStatusV1.UNSUPPORTED
    assert result.history_ir is None
    assert result.reason_code == R24_MAI_CURRENT_TEXT_UNSUPPORTED_REASON
    assert "current_screenshot_not_model_visible" in result.validation_checks
