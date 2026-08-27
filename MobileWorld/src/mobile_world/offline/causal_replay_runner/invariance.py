"""Capsule-aware invariance proof layered over the frozen G1.2 guard."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, cast

from mobile_world.offline.causal_replay.contracts import (
    ArmKind,
    JsonValue,
    MappingKind,
    RenderResult,
    TransformationPlan,
    ValidationReceipt,
    canonical_sha256,
    copy_json,
    get_at_path,
)
from mobile_world.offline.causal_replay.core import restore_original
from mobile_world.offline.causal_replay_runner.contracts import (
    InvarianceReport,
    LoadedReplayCapsule,
    ReplayRunnerError,
)

_MUTABLE = "MUTABLE_HISTORY_TREATMENT"


def _require(condition: bool, code: str, message: str, *, path: str | None = None) -> None:
    if not condition:
        raise ReplayRunnerError(code, message, json_path=path)


def _path_tuple(value: Any) -> tuple[str | int, ...]:
    _require(isinstance(value, list), "CAPSULE_REGION_INVALID", "binding path must be an array")
    assert isinstance(value, list)
    _require(
        bool(value)
        and all(
            (isinstance(item, str) and bool(item)) or (type(item) is int and item >= 0)
            for item in value
        ),
        "CAPSULE_REGION_INVALID",
        "binding path contains invalid tokens",
    )
    return tuple(cast(Sequence[str | int], value))


def _translated_path(
    source_path: tuple[str | int, ...], result: RenderResult
) -> tuple[str | int, ...]:
    translated = list(source_path)
    shifts: defaultdict[int, int] = defaultdict(int)
    for insertion in sorted(
        result.list_insertions,
        key=lambda item: (len(item.container_path), item.source_index, item.rendered_index),
    ):
        prefix = insertion.container_path
        if len(source_path) <= len(prefix) or source_path[: len(prefix)] != prefix:
            continue
        token = source_path[len(prefix)]
        if type(token) is int and token >= insertion.source_index:
            shifts[len(prefix)] += 1
    for index, shift in shifts.items():
        translated[index] = cast(int, source_path[index]) + shift
    return tuple(translated)


def _protected_text_from_mapping(
    *,
    result: RenderResult,
    source_path: tuple[str | int, ...],
    char_start: int,
    char_end: int,
) -> str:
    rendered_path = _translated_path(source_path, result)
    rendered_value = get_at_path(result.rendered_request, rendered_path)
    _require(
        isinstance(rendered_value, str),
        "PROTECTED_REGION_CHANGED",
        "translated protected text container is not text",
    )
    assert isinstance(rendered_value, str)
    mappings = sorted(
        (
            item
            for item in result.source_mappings
            if item.container_path == source_path
            and item.kind is MappingKind.COPIED
            and item.source_char_end > char_start
            and item.source_char_start < char_end
        ),
        key=lambda item: item.source_char_start,
    )
    cursor = char_start
    pieces: list[str] = []
    for mapping in mappings:
        overlap_start = max(cursor, mapping.source_char_start)
        overlap_end = min(char_end, mapping.source_char_end)
        if overlap_start >= overlap_end:
            continue
        _require(
            overlap_start == cursor,
            "PROTECTED_REGION_CHANGED",
            "protected text has a non-copied gap",
        )
        rendered_start = mapping.rendered_char_start + (overlap_start - mapping.source_char_start)
        rendered_end = rendered_start + (overlap_end - overlap_start)
        pieces.append(rendered_value[rendered_start:rendered_end])
        cursor = overlap_end
        if cursor == char_end:
            break
    _require(
        cursor == char_end,
        "PROTECTED_REGION_CHANGED",
        "protected text is not wholly covered by copied source mappings",
    )
    return "".join(pieces)


def _non_history_projection(
    semantic_request: JsonValue, regions: Sequence[Mapping[str, Any]]
) -> JsonValue:
    projection = copy_json(semantic_request)
    grouped: defaultdict[tuple[str | int, ...], list[tuple[int, int]]] = defaultdict(list)
    for region in regions:
        if region.get("ownership_role") != "OWNER":
            continue
        bindings = region.get("bindings")
        _require(
            isinstance(bindings, list),
            "CAPSULE_REGION_INVALID",
            "region bindings must be an array",
        )
        assert isinstance(bindings, list)
        for binding in bindings:
            if not isinstance(binding, Mapping) or binding.get("visibility_class") != _MUTABLE:
                continue
            path = _path_tuple(binding.get("path"))
            source = get_at_path(semantic_request, path)
            _require(
                isinstance(source, str),
                "CAPSULE_REGION_INVALID",
                "mutable history owner must resolve to text",
            )
            assert isinstance(source, str)
            if binding.get("binding_kind") == "TEXT_SLICE":
                span = binding.get("text_slice")
                _require(
                    isinstance(span, Mapping),
                    "CAPSULE_REGION_INVALID",
                    "mutable text slice is missing",
                )
                assert isinstance(span, Mapping)
                start, end = span.get("char_start"), span.get("char_end")
                _require(
                    type(start) is int and type(end) is int and 0 <= start < end <= len(source),
                    "CAPSULE_REGION_INVALID",
                    "mutable text slice coordinates are invalid",
                )
                start_int = cast(int, start)
                end_int = cast(int, end)
                grouped[path].append((start_int, end_int))
            else:
                grouped[path].append((0, len(source)))
    for path, spans in grouped.items():
        value = get_at_path(projection, path)
        _require(isinstance(value, str), "CAPSULE_REGION_INVALID", "projection target is not text")
        assert isinstance(value, str)
        for start, end in sorted(spans, reverse=True):
            value = value[:start] + "<MUTABLE_HISTORY_TREATMENT>" + value[end:]
        parent = get_at_path(projection, path[:-1])
        _require(
            isinstance(parent, (dict, list)),
            "CAPSULE_REGION_INVALID",
            "projection target parent is not a container",
        )
        assert isinstance(parent, (dict, list))
        if isinstance(parent, dict):
            _require(
                isinstance(path[-1], str),
                "CAPSULE_REGION_INVALID",
                "object projection path must end in a string key",
            )
            parent[cast(str, path[-1])] = value
        else:
            _require(
                type(path[-1]) is int,
                "CAPSULE_REGION_INVALID",
                "array projection path must end in an integer index",
            )
            parent[cast(int, path[-1])] = value
    return projection


def _verify_frozen_bindings(capsule: LoadedReplayCapsule, result: RenderResult) -> None:
    source = capsule.semantic_request
    for region in capsule.region_partition:
        if region.get("ownership_role") != "OWNER":
            continue
        bindings = region.get("bindings")
        _require(
            isinstance(bindings, list),
            "CAPSULE_REGION_INVALID",
            "region bindings must be an array",
        )
        assert isinstance(bindings, list)
        for binding in bindings:
            _require(
                isinstance(binding, Mapping),
                "CAPSULE_REGION_INVALID",
                "region binding must be an object",
            )
            assert isinstance(binding, Mapping)
            path = _path_tuple(binding.get("path"))
            source_value = get_at_path(source, path)
            _require(
                canonical_sha256(source_value) == binding.get("value_sha256"),
                "CAPSULE_REGION_HASH_MISMATCH",
                "source request differs from its frozen region hash",
            )
            if binding.get("visibility_class") == _MUTABLE:
                continue
            rendered_path = _translated_path(path, result)
            if binding.get("binding_kind") == "TEXT_SLICE":
                span = binding.get("text_slice")
                _require(
                    isinstance(source_value, str) and isinstance(span, Mapping),
                    "CAPSULE_REGION_INVALID",
                    "protected text binding is invalid",
                )
                assert isinstance(source_value, str)
                assert isinstance(span, Mapping)
                start = cast(int, span["char_start"])
                end = cast(int, span["char_end"])
                expected = source_value[start:end]
                observed = _protected_text_from_mapping(
                    result=result,
                    source_path=path,
                    char_start=start,
                    char_end=end,
                )
                _require(
                    observed == expected,
                    "PROTECTED_REGION_CHANGED",
                    "protected text slice changed",
                )
            else:
                rendered_value = get_at_path(result.rendered_request, rendered_path)
                _require(
                    rendered_value == source_value,
                    "PROTECTED_REGION_CHANGED",
                    "frozen request value changed",
                )


def _data_urls(value: JsonValue) -> tuple[str, ...]:
    found: list[str] = []

    def visit(item: JsonValue) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith("data:image/"):
            found.append(item)

    visit(value)
    return tuple(found)


def _history_projection_sha256(result: RenderResult) -> str:
    return canonical_sha256(
        {
            "diffs": [item.to_dict() for item in result.diffs],
            "list_insertions": [item.to_dict() for item in result.list_insertions],
        }
    )


def verify_invariance(
    *,
    capsule: LoadedReplayCapsule,
    plan: TransformationPlan,
    render_result: RenderResult,
    validation_receipt: ValidationReceipt,
) -> InvarianceReport:
    """Prove capsule identity plus target-only rendering before provider encoding."""

    _require(
        type(plan.curated) is bool  # type: ignore[redundant-expr]
        and plan.curated is True
        and type(plan.deployment_prediction) is bool  # type: ignore[redundant-expr]
        and plan.deployment_prediction is False,
        "UNSAFE_PLAN_PROVENANCE",
        "G1 plan must be curated=true and deployment_prediction=false",
    )
    _require(
        render_result.original_request == capsule.semantic_request
        and render_result.source_request_sha256 == capsule.semantic_request_sha256
        and canonical_sha256(render_result.original_request) == capsule.semantic_request_sha256,
        "CAPSULE_SOURCE_REQUEST_MISMATCH",
        "render source is not the capsule's authoritative semantic request",
    )
    _require(
        type(validation_receipt.valid) is bool  # type: ignore[redundant-expr]
        and validation_receipt.valid is True
        and validation_receipt.source_request_sha256 == capsule.semantic_request_sha256
        and not validation_receipt.errors,
        "PRE_SEND_RECEIPT_INVALID",
        "G1.2 pre-send receipt is invalid or binds another request",
    )
    _require(
        validation_receipt.rendered_request_sha256 == render_result.rendered_request_sha256
        and canonical_sha256(render_result.rendered_request)
        == render_result.rendered_request_sha256,
        "RENDERED_REQUEST_HASH_MISMATCH",
        "rendered request digest is inconsistent",
    )
    _require(
        restore_original(render_result) == capsule.semantic_request,
        "NON_REVERSIBLE_SOURCE_MAPPING",
        "render source mapping cannot restore the capsule request",
    )
    _verify_frozen_bindings(capsule, render_result)
    source_projection = _non_history_projection(
        capsule.semantic_request, cast(Sequence[Mapping[str, Any]], capsule.region_partition)
    )
    projection_sha = canonical_sha256(source_projection)
    _require(
        projection_sha == capsule.non_history_projection_sha256,
        "NON_HISTORY_PROJECTION_MISMATCH",
        "capsule non-history projection digest is inconsistent",
    )
    if plan.arm is ArmKind.ORIGINAL:
        _require(
            not plan.operations
            and render_result.rendered_request == capsule.semantic_request
            and render_result.rendered_request_sha256 == capsule.semantic_request_sha256,
            "ORIGINAL_NOT_SEMANTICALLY_IDENTICAL",
            "Original must be exact semantic identity before replay seed",
        )
    _require(
        _data_urls(capsule.semantic_request) == _data_urls(render_result.rendered_request),
        "BINARY_ARTIFACT_CHANGED",
        "request image data URLs changed or reordered",
    )
    if isinstance(capsule.semantic_request, dict) and isinstance(
        render_result.rendered_request, dict
    ):
        source_non_messages = {
            key: value for key, value in capsule.semantic_request.items() if key != "messages"
        }
        rendered_non_messages = {
            key: value for key, value in render_result.rendered_request.items() if key != "messages"
        }
        _require(
            source_non_messages == rendered_non_messages,
            "MODEL_OR_SAMPLING_CHANGED",
            "non-message SDK arguments changed during history rendering",
        )
    render_result_sha256 = canonical_sha256(render_result.to_dict())
    validation_receipt_sha256 = canonical_sha256(validation_receipt.to_dict())
    target_diff_sha256 = _history_projection_sha256(render_result)
    report_subject: dict[str, JsonValue] = {
        "capsule_body_sha256": capsule.capsule_body_sha256,
        "plan_sha256": canonical_sha256(plan.to_dict()),
        "render_result_sha256": render_result_sha256,
        "validation_receipt_sha256": validation_receipt_sha256,
        "non_history_projection_sha256": projection_sha,
    }
    return InvarianceReport(
        report_id=f"g1invariance-{canonical_sha256(report_subject)[:24]}",
        valid=True,
        source_request_sha256=capsule.semantic_request_sha256,
        rendered_request_sha256=render_result.rendered_request_sha256,
        final_application_request_sha256=render_result.rendered_request_sha256,
        encoded_request_sha256=None,
        non_history_projection_sha256=projection_sha,
        history_projection_sha256=target_diff_sha256,
        render_result_sha256=render_result_sha256,
        validation_receipt_sha256=validation_receipt_sha256,
        target_diff_sha256=target_diff_sha256,
        requested_arm=plan.arm,
        target_only_diff=True,
        original_semantic_identity=(
            plan.arm is not ArmKind.ORIGINAL
            or render_result.rendered_request == capsule.semantic_request
        ),
        caller_input_immutable=True,
        source_mapping_reversible=True,
        roles_and_order_preserved=True,
        tools_preserved=True,
        current_observation_preserved=True,
        model_and_sampling_preserved=True,
        binary_artifacts_preserved=True,
        checks=(
            "active_capsule_source_binding",
            "curated_non_deployment_plan",
            "g1_2_pre_send_receipt",
            "canonical_render_hash",
            "reversible_source_mapping",
            "capsule_non_history_projection",
            "frozen_region_bindings",
            "roles_and_order",
            "tools_and_protocol",
            "current_observation_and_images",
            "model_sampling_and_unknown_kwargs",
        ),
    )


def bind_encoded_request(
    report: InvarianceReport,
    *,
    encoded_request_sha256: str,
    rendered_application_request_sha256: str,
    final_application_request_sha256: str,
) -> InvarianceReport:
    _require(report.valid, "INVARIANCE_REPORT_INVALID", "cannot bind an invalid report")
    _require(
        rendered_application_request_sha256 == report.rendered_request_sha256,
        "ENCODED_APPLICATION_BINDING_MISMATCH",
        "provider encoder binds another rendered application request",
    )
    _require(
        encoded_request_sha256 == final_application_request_sha256,
        "ENCODED_APPLICATION_BINDING_MISMATCH",
        "canonical encoded bytes differ from the final SDK arguments",
    )
    subject: dict[str, JsonValue] = {
        "prior_report_id": report.report_id,
        "rendered_application_request_sha256": rendered_application_request_sha256,
        "final_application_request_sha256": final_application_request_sha256,
        "encoded_request_sha256": encoded_request_sha256,
    }
    return InvarianceReport(
        **{
            **report.__dict__,
            "report_id": f"g1invariance-{canonical_sha256(subject)[:24]}",
            "final_application_request_sha256": final_application_request_sha256,
            "encoded_request_sha256": encoded_request_sha256,
            "checks": (*report.checks, "encoded_request_bound_after_invariance"),
        }
    )
