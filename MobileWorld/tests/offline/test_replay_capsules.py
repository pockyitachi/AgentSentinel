"""Focused conformance and adversarial tests for ALE-321 replay capsules."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

import mobile_world.offline.replay_capsules as replay_capsules

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FROZEN_REGISTRY_ROOT = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_1/registry/sha256/"
    + replay_capsules.G1_REGISTRY_MANIFEST_SHA256
)
FROZEN_SOURCE_BASE = Path("/shared/linqiang/mobileworld_audit_data")
LEGACY_G1_3_PUBLICATION = Path(
    "/shared/linqiang/mobileworld_causal_replay_data/g1_3/capsules/sha256/"
    "c2af8b8393e2df2da21bedcc98614e60a08b8254dc03da373ce72d67fe7c76c5"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _registry_unit() -> replay_capsules.RegistryUnit:
    record = {"case_status": "CANDIDATE_FROZEN"}
    line = replay_capsules.canonical_json_line(record)
    return replay_capsules.RegistryUnit(
        unit_kind="STRICT_MHR",
        unit_id="g1case-000000000000000000000001",
        registry_file="case_registry.pre_gold.jsonl",
        registry_file_sha256="1" * 64,
        registry_file_byte_count=len(line),
        line_number=1,
        line_sha256=_sha256(line),
        record=record,
    )


def _cli_artifacts(*, phase: str = "BUILD_CANDIDATE") -> dict[str, Any]:
    return {
        "manifest": {
            "publication_phase": phase,
            "capsule_set_sha256": "2" * 64,
            "counts": {"capsuled_count": 190, "excluded_count": 0},
        },
        "file_payloads": {
            "capsule_manifest.json": b"manifest\n",
            "capsule_index.jsonl": b"index\n",
        },
    }


def _synthetic_disposition_index() -> list[dict[str, Any]]:
    units = []
    for index in range(replay_capsules.TARGET_POPULATION):
        strict = index < replay_capsules.STRICT_TARGET_COUNT
        unit_id = (
            f"g1case-{index:024x}"
            if strict
            else f"g1control-{index - replay_capsules.STRICT_TARGET_COUNT:024x}"
        )
        units.append(
            {
                "unit_kind": "STRICT_MHR" if strict else "CLEAN_CONTROL",
                "unit_id": unit_id,
                "disposition": "CAPSULED",
                "capsule_body_sha256": _sha256(unit_id.encode()),
                "exclusion_record_sha256": None,
            }
        )
    return units


def _real_capsule_for_family(artifacts: dict[str, Any], history_family: str) -> dict[str, Any]:
    return next(
        envelope
        for envelope in artifacts["capsules"]
        if envelope["capsule"]["unit"]["history_family"] == history_family
    )


def _publication_json(artifacts: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    assert reference["store_id"] == "G1_3_PUBLICATION"
    payload = artifacts["file_payloads"][reference["relative_path"]]
    parsed = replay_capsules._parse_json_bytes(payload, Path(reference["relative_path"]))
    assert isinstance(parsed, dict)
    return parsed


def _registry_unit_for_capsule(
    frozen: dict[str, Any], envelope: dict[str, Any]
) -> replay_capsules.RegistryUnit:
    unit_id = envelope["capsule"]["unit"]["unit_id"]
    return next(unit for unit in frozen["population"] if unit.unit_id == unit_id)


def _request_pair_for_capsule(
    artifacts: dict[str, Any], envelope: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    semantic = envelope["capsule"]["runtime"]["model_visible"]["semantic_request"]
    return (
        _publication_json(artifacts, semantic["canonical_semantic_request_ref"]),
        _publication_json(artifacts, semantic["inspectable_request_view_ref"]),
    )


def _collector_image_record(
    store: replay_capsules.BlobStore, *, content_path: str, data_url: str, pixels: bytes
) -> dict[str, Any]:
    return {
        "content_path": content_path,
        "content_blob": store.put_bytes(pixels, "image/png"),
        "original_text_blob": store.put_bytes(data_url.encode("utf-8"), "text/plain"),
        "media_type": "image/png",
        "width": 1,
        "height": 1,
        "canonical_base64": True,
        "capture_status": "captured",
    }


def _refresh_closure_metadata(closure: dict[str, Any]) -> None:
    unique = {
        (
            entry["reference"]["store_id"],
            entry["reference"]["relative_path"],
            entry["reference"]["sha256"],
        ): entry["reference"]
        for entry in closure["entries"]
    }
    closure["unique_artifact_count"] = len(unique)
    closure["total_byte_count"] = sum(reference["byte_count"] for reference in unique.values())
    closure["aggregate_sha256"] = replay_capsules.canonical_sha256(closure["entries"])


def _synthetic_semantic_partition() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    semantic_request = {
        "messages": [
            {"role": "system", "content": "system protocol"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "mutable history"},
            {"role": "user", "content": "current observation"},
        ],
        "model": "pinned-model",
    }
    system_text = semantic_request["messages"][0]["content"]
    system_protocol = replay_capsules._text_binding(
        ("messages", 0, "content"),
        system_text,
        0,
        len(system_text),
        role="SYSTEM_ACTION_PROTOCOL",
        visibility_class="FROZEN_MODEL_VISIBLE",
    )
    regions = [
        replay_capsules._region(
            "system",
            "SYSTEM",
            "PRESENT",
            [
                replay_capsules._whole_value_binding(
                    ("messages", 0, "role"),
                    "system",
                    role="SYSTEM_MESSAGE_ROLE",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                ),
                system_protocol,
            ],
        ),
        replay_capsules._region(
            "task",
            "TASK",
            "PRESENT",
            [
                replay_capsules._whole_value_binding(
                    ("messages", 1),
                    semantic_request["messages"][1],
                    role="TASK_INSTRUCTION",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                )
            ],
        ),
        replay_capsules._region(
            "history",
            "HISTORY",
            "PRESENT",
            [
                replay_capsules._whole_value_binding(
                    ("messages", 2, "role"),
                    "assistant",
                    role="HISTORY_MESSAGE_ROLE",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                ),
                replay_capsules._whole_value_binding(
                    ("messages", 2, "content"),
                    semantic_request["messages"][2]["content"],
                    role="ASSISTANT_HISTORY_CONTENT",
                    visibility_class="MUTABLE_HISTORY_TREATMENT",
                ),
            ],
        ),
        replay_capsules._region(
            "current_observation",
            "CURRENT_OBSERVATION",
            "PRESENT",
            [
                replay_capsules._whole_value_binding(
                    ("messages", 3),
                    semantic_request["messages"][3],
                    role="CURRENT_SCREENSHOT",
                    visibility_class="FROZEN_MODEL_VISIBLE",
                )
            ],
        ),
        replay_capsules._region(
            "tool_protocol",
            "TOOL_PROTOCOL",
            "COLOCATED",
            [system_protocol],
            ownership_role="PROTECTED_OVERLAY",
        ),
        replay_capsules._region(
            "provider_control",
            "PROVIDER_CONTROL",
            "PRESENT",
            [
                replay_capsules._whole_value_binding(
                    ("model",),
                    semantic_request["model"],
                    role="PROVIDER_PARAMETER",
                    visibility_class="FROZEN_NON_HISTORY_ENVELOPE",
                )
            ],
        ),
    ]
    return semantic_request, regions


def _synthetic_visibility_capsule() -> dict[str, Any]:
    return {
        "unit": {"request_cutoff": {"event_seq": 10}},
        "source_provenance": {"task_stream_sha256": "a" * 64},
        "runtime": {"request_event": {"seq": 10}},
        "curator_only": {"evidence_event": {"seq": 9}},
        "post_action_audit": {
            "runtime_eligible": False,
            "curator_eligible": False,
        },
    }


def _synthetic_runtime_binding() -> tuple[dict[str, Any], dict[str, bytes], dict[str, Any]]:
    parser_value = {
        "path": "MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py",
        "symbols": ["parse_action_to_structure_output"],
    }
    parser_data = replay_capsules.canonical_json_bytes(parser_value)
    parser_name = f"artifact-{_sha256(parser_data)}"
    parser_ref = {
        "store_id": "G1_3_PUBLICATION",
        **replay_capsules._file_summary(parser_data, parser_name),
    }
    semantic_request = {
        "messages": [{"role": "user", "content": "current observation"}],
        "model": "Qwen3-VL-8B-Instruct",
        "temperature": 0.0,
    }
    decoding_configuration = {
        key: value for key, value in semantic_request.items() if key != "messages"
    }
    decoding_data = replay_capsules.canonical_json_bytes(decoding_configuration)
    decoding_name = f"artifact-{_sha256(decoding_data)}"
    decoding_ref = {
        "store_id": "G1_3_PUBLICATION",
        **replay_capsules._file_summary(decoding_data, decoding_name),
    }
    provenance = {
        "host": {"adapter_id": "qwen3vl", "component": "actor", "call_role": "actor"},
        "model": {
            "model_id": "qwen3vl_8b",
            "served_model_name": "Qwen3-VL-8B-Instruct",
            "repository": "Qwen/Qwen3-VL-8B-Instruct",
            "revision": "0" * 40,
            "model_config_manifest_sha256": "1" * 64,
            "model_config_record_sha256": "2" * 64,
            "parser_implementation_sha256": replay_capsules.canonical_sha256(parser_value),
        },
        "provider": {
            "sdk_package": "openai",
            "sdk_version": "1.106.1",
            "sdk_method": "chat.completions.create",
            "endpoint_origin": "http://127.0.0.1:18007",
            "endpoint_path": "/v1/chat/completions",
            "query_removed": True,
            "stream": False,
            "decoding_configuration_ref": decoding_ref,
            "decoding_configuration_sha256": replay_capsules.canonical_sha256(
                decoding_configuration
            ),
            "excluded_transport_fields": ["authorization", "provider_request_id"],
        },
    }
    binding = replay_capsules._runtime_replay_binding(provenance, parser_ref)
    capsule = {
        "source_provenance": provenance,
        "runtime": {
            "non_history_envelope": {
                "provider_envelope_sha256": replay_capsules.canonical_sha256(
                    decoding_configuration
                ),
                "replay_binding": binding,
            }
        },
    }
    return (
        capsule,
        {parser_name: parser_data, decoding_name: decoding_data},
        semantic_request,
    )


def _synthetic_collector_event(
    seq: int, *, caused_by_event_id: str | None = None
) -> dict[str, Any]:
    return {
        "schema_version": "mobileworld.audit.event/v1",
        "event_id": f"0198a000-0000-7000-8000-{100 + seq:012d}",
        "event_type": "task_started" if seq == 1 else "step_started",
        "run_id": "0198a000-0000-7000-8000-000000000001",
        "task_run_id": "0198a000-0000-7000-8000-000000000002",
        "stream_id": "0198a000-0000-7000-8000-000000000002",
        "seq": seq,
        "wall_time": f"2026-08-18T14:00:0{seq}.000000Z",
        "monotonic_ns": seq,
        "caused_by_event_id": caused_by_event_id,
        "producer": {
            "component": "mobile_world.audit",
            "version": "0.1.0",
            "process_id": 1,
            "worker_id": "bounded-test",
        },
        "payload": {},
    }


@pytest.fixture(scope="session")
def frozen_inputs() -> dict[str, Any]:
    """Load only the small frozen G1.1 publication, never Collector source data."""

    if not FROZEN_REGISTRY_ROOT.is_dir():
        pytest.skip("frozen external G1.1 registry publication is not mounted")
    return replay_capsules._load_frozen_inputs(REPOSITORY_ROOT, FROZEN_REGISTRY_ROOT)


@pytest.fixture(scope="session")
def real_formal_artifacts() -> dict[str, Any]:
    """Build the real 190-unit formal set once and share its in-memory bytes."""

    if not FROZEN_REGISTRY_ROOT.is_dir() or not FROZEN_SOURCE_BASE.is_dir():
        pytest.skip("frozen G1.1 registry or Collector source base is not mounted")
    return replay_capsules.build_verified_capsule_artifacts(
        repo_root=REPOSITORY_ROOT,
        registry_root=FROZEN_REGISTRY_ROOT,
        source_base=FROZEN_SOURCE_BASE,
    )


def test_frozen_population_constants_match_the_repository_lock() -> None:
    lock_bytes = (
        REPOSITORY_ROOT / "mobileworld_audit_handoff/g1/registry.lock.v1.json"
    ).read_bytes()
    lock = json.loads(lock_bytes)

    assert _sha256(lock_bytes) == replay_capsules.G1_REGISTRY_LOCK_SHA256
    assert replay_capsules.TARGET_POPULATION == 190
    assert replay_capsules.STRICT_TARGET_COUNT == 152
    assert replay_capsules.SELECTED_CLEAN_TARGET_COUNT == 38
    assert replay_capsules.RESERVE_CONTROL_COUNT == 38
    assert replay_capsules.EXPECTED_MODEL_COUNTS == {"qwen3vl_8b": 169, "mai_ui_8b": 21}
    assert lock["counts"]["strict_mhr_case_count"] == 152
    assert lock["counts"]["clean_control_selected_count"] == 38
    assert lock["counts"]["clean_control_reserve_count"] == 38
    assert lock["readiness"] == {
        "curation_and_admission_sealed": False,
        "admission_ready": False,
        "execution_ready": False,
        "run_ready": False,
        "treatment_response_generation_allowed": False,
    }


def test_frozen_registry_selects_only_strict_and_selected_units(
    frozen_inputs: dict[str, Any],
) -> None:
    population = frozen_inputs["population"]
    kinds = Counter(unit.unit_kind for unit in population)
    models = Counter(unit.record["model_id"] for unit in population)

    assert len(population) == 190
    assert len({unit.unit_id for unit in population}) == 190
    assert kinds == {"STRICT_MHR": 152, "CLEAN_CONTROL": 38}
    assert models == {"qwen3vl_8b": 169, "mai_ui_8b": 21}
    assert all(
        unit.record["case_status"] == "CANDIDATE_FROZEN"
        for unit in population
        if unit.unit_kind == "STRICT_MHR"
    )
    assert all(
        unit.record["control_status"] == "SELECTED"
        for unit in population
        if unit.unit_kind == "CLEAN_CONTROL"
    )
    assert not any(unit.record.get("control_status") == "RESERVE" for unit in population)


def test_visibility_contract_is_exact_and_deny_by_default() -> None:
    policy = replay_capsules.field_visibility_policy()

    assert policy["default_policy"] == "DENY"
    assert policy["classification_complete"] is True
    assert tuple(rule["classification"] for rule in policy["rules"]) == (
        replay_capsules.VISIBILITY_CLASSES
    )
    assert [rule["root_json_pointer"] for rule in policy["rules"]] == [
        "/runtime/model_visible",
        "/runtime/non_history_envelope",
        "/runtime/treatment_surface",
        "/curator_only",
        "/post_action_audit",
    ]
    assert all(rule["direct_provider_input"] is False for rule in policy["rules"])
    assert policy["renderer_input_roots"] == [
        "/runtime/model_visible",
        "/runtime/treatment_surface",
    ]
    assert policy["harness_input_roots"] == ["/runtime/non_history_envelope"]
    assert {
        "/unit",
        "/source_provenance",
        "/curator_only",
        "/post_action_audit",
        "/field_visibility",
        "/artifact_closure",
        "/integrity_binding",
        "/safety",
    } == set(policy["forbidden_runtime_roots"])


def test_canonical_hash_subjects_distinguish_body_bytes_from_jsonl() -> None:
    value = {"z": "雪", "a": [True, None, 1]}
    canonical_body = '{"a":[true,null,1],"z":"雪"}'.encode()

    assert replay_capsules.canonical_json_line(value) == canonical_body + b"\n"
    assert replay_capsules.canonical_sha256(value) == _sha256(canonical_body)
    assert replay_capsules.g1_1_canonical_sha256(value) == _sha256(canonical_body + b"\n")
    assert replay_capsules.canonical_sha256(value) != replay_capsules.g1_1_canonical_sha256(value)


def test_190_unit_capsule_set_hash_binds_exact_order_and_disposition() -> None:
    units = _synthetic_disposition_index()
    subject = [
        {
            "unit_kind": unit["unit_kind"],
            "unit_id": unit["unit_id"],
            "disposition": unit["disposition"],
            "capsule_body_sha256": unit["capsule_body_sha256"],
            "exclusion_record_sha256": unit["exclusion_record_sha256"],
        }
        for unit in units
    ]

    assert len(units) == 190
    assert len({unit["unit_id"] for unit in units}) == 190
    assert replay_capsules._capsule_set_sha256(units) == replay_capsules.canonical_sha256(subject)
    assert replay_capsules._capsule_set_sha256(list(reversed(units))) != (
        replay_capsules._capsule_set_sha256(units)
    )

    tampered = [dict(unit) for unit in units]
    tampered[0]["capsule_body_sha256"] = "f" * 64
    assert replay_capsules._capsule_set_sha256(tampered) != (
        replay_capsules._capsule_set_sha256(units)
    )


def test_request_view_rejects_scalar_artifact_mismatch(tmp_path: Path) -> None:
    semantic_request = {"messages": [], "model": "captured-model"}
    request_view = {"messages": [], "model": "different-model"}

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._verify_request_view(
            semantic_request=semantic_request,
            request_view=request_view,
            blob_store=replay_capsules.BlobStore(tmp_path),
        )
    assert raised.value.code == "REQUEST_VIEW_MISMATCH"


def test_request_view_rejects_missing_transitive_externalized_blob(tmp_path: Path) -> None:
    data_url = "data:image/png;base64,AA=="
    digest = "0" * 64
    missing_ref = {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(data_url.encode("utf-8")),
        "media_type": "text/plain",
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }
    semantic_request = {"image": data_url}
    request_view = {
        "image": {
            "$externalized_data_url": {
                "original_text_blob": missing_ref,
                "content_blob": {
                    **missing_ref,
                    "byte_length": 1,
                    "media_type": "image/png",
                },
                "content_path": "image",
            }
        }
    }

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._verify_request_view(
            semantic_request=semantic_request,
            request_view=request_view,
            blob_store=replay_capsules.BlobStore(tmp_path),
        )
    assert raised.value.code == "BLOB_MISSING"


def test_request_view_rejects_non_current_raw_data_image_string(tmp_path: Path) -> None:
    store = replay_capsules.BlobStore(tmp_path)
    prior_url = "data:image/png;base64,cHJpb3I="
    current_url = "data:image/png;base64,Y3VycmVudA=="
    current_path = "messages[0].content[1].image_url.url"
    current = _collector_image_record(
        store,
        content_path=current_path,
        data_url=current_url,
        pixels=b"current-pixels",
    )
    semantic_request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": prior_url}},
                    {"type": "image_url", "image_url": {"url": current_url}},
                ],
            }
        ]
    }
    request_view = deepcopy(semantic_request)
    request_view["messages"][0]["content"][1]["image_url"]["url"] = {
        "$externalized_data_url": {
            "content_path": current_path,
            "content_blob": current["content_blob"],
            "original_text_blob": current["original_text_blob"],
        }
    }

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._verify_request_view(
            semantic_request=semantic_request,
            request_view=request_view,
            blob_store=store,
        )
    assert raised.value.code == "REQUEST_VIEW_MISMATCH"


def test_request_image_inventory_rejects_missing_non_current_image(tmp_path: Path) -> None:
    store = replay_capsules.BlobStore(tmp_path)
    prior_url = "data:image/png;base64,cHJpb3I="
    current_url = "data:image/png;base64,Y3VycmVudA=="
    prior_path = "messages[0].content[0].image_url.url"
    current_path = "messages[0].content[1].image_url.url"
    semantic_request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": prior_url}},
                    {"type": "image_url", "image_url": {"url": current_url}},
                ],
            }
        ]
    }
    prior = _collector_image_record(
        store,
        content_path=prior_path,
        data_url=prior_url,
        pixels=b"prior-pixels",
    )
    current = _collector_image_record(
        store,
        content_path=current_path,
        data_url=current_url,
        pixels=b"current-pixels",
    )
    current_only = [current]
    externalized = [
        {
            "content_path": record["content_path"],
            "semantic_request_path": replay_capsules._parse_dot_path(record["content_path"]),
            "content_blob": record["content_blob"],
            "original_text_blob": record["original_text_blob"],
        }
        for record in (prior, current)
    ]

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._schema_request_images(
            request_images=current_only,
            externalized_request_images=externalized,
            semantic_request=semantic_request,
            current_path=replay_capsules._parse_dot_path(current_path),
            run_id="synthetic-run",
            blob_store=store,
        )
    assert raised.value.code == "REQUEST_VIEW_MISMATCH"
    assert prior_path not in {record["content_path"] for record in current_only}


def test_semantic_partition_has_exact_owner_coverage_and_projection() -> None:
    semantic_request, regions = _synthetic_semantic_partition()
    original = deepcopy(semantic_request)

    replay_capsules._validate_semantic_request_partition(semantic_request, regions)
    projection = replay_capsules._non_history_projection(semantic_request, regions)

    assert semantic_request == original
    assert projection["messages"][2]["content"] == "<MUTABLE_HISTORY_TREATMENT>"
    assert projection["messages"][:2] == semantic_request["messages"][:2]
    assert projection["messages"][3:] == semantic_request["messages"][3:]
    assert projection["model"] == semantic_request["model"]


def test_semantic_partition_rejects_unowned_string_suffix() -> None:
    semantic_request, regions = _synthetic_semantic_partition()
    history_index = next(
        index for index, region in enumerate(regions) if region["kind"] == "HISTORY"
    )
    history_text = semantic_request["messages"][2]["content"]
    role_binding = regions[history_index]["bindings"][0]
    incomplete_text = replay_capsules._text_binding(
        ("messages", 2, "content"),
        history_text,
        0,
        len(history_text) - 1,
        role="ASSISTANT_HISTORY_CONTENT",
        visibility_class="MUTABLE_HISTORY_TREATMENT",
    )
    regions[history_index] = replay_capsules._region(
        "history", "HISTORY", "PRESENT", [role_binding, incomplete_text]
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_semantic_request_partition(semantic_request, regions)
    assert raised.value.code == "REQUEST_PARTITION_INCOMPLETE"


def test_semantic_partition_rejects_overlapping_owner_slices() -> None:
    semantic_request, regions = _synthetic_semantic_partition()
    history_index = next(
        index for index, region in enumerate(regions) if region["kind"] == "HISTORY"
    )
    history_text = semantic_request["messages"][2]["content"]
    role_binding = regions[history_index]["bindings"][0]
    complete_text = replay_capsules._text_binding(
        ("messages", 2, "content"),
        history_text,
        0,
        len(history_text),
        role="ASSISTANT_HISTORY_CONTENT",
        visibility_class="MUTABLE_HISTORY_TREATMENT",
    )
    duplicate_prefix = replay_capsules._text_binding(
        ("messages", 2, "content"),
        history_text,
        0,
        1,
        role="DUPLICATE_HISTORY_OWNER",
        visibility_class="MUTABLE_HISTORY_TREATMENT",
    )
    regions[history_index] = replay_capsules._region(
        "history",
        "HISTORY",
        "PRESENT",
        [role_binding, complete_text, duplicate_prefix],
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_semantic_request_partition(semantic_request, regions)
    assert raised.value.code == "REQUEST_PARTITION_AMBIGUOUS"


def test_semantic_partition_requires_mutable_history_visibility() -> None:
    semantic_request, regions = _synthetic_semantic_partition()
    history_index = next(
        index for index, region in enumerate(regions) if region["kind"] == "HISTORY"
    )
    history_text = semantic_request["messages"][2]["content"]
    frozen_history = replay_capsules._whole_value_binding(
        ("messages", 2, "content"),
        history_text,
        role="ASSISTANT_HISTORY_CONTENT",
        visibility_class="FROZEN_MODEL_VISIBLE",
    )
    regions[history_index] = replay_capsules._region(
        "history",
        "HISTORY",
        "PRESENT",
        [regions[history_index]["bindings"][0], frozen_history],
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_semantic_request_partition(semantic_request, regions)
    assert raised.value.code == "FIELD_VISIBILITY_INVALID"


def test_visibility_boundary_accepts_only_pre_cutoff_projections() -> None:
    replay_capsules._validate_visibility_boundary(_synthetic_visibility_capsule())


def test_visibility_boundary_rejects_post_cutoff_event() -> None:
    capsule = _synthetic_visibility_capsule()
    capsule["curator_only"]["evidence_event"]["seq"] = 11

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_visibility_boundary(capsule)
    assert raised.value.code == "FUTURE_EVIDENCE_LEAKAGE"


def test_visibility_boundary_rejects_full_stream_alias() -> None:
    capsule = _synthetic_visibility_capsule()
    capsule["runtime"]["source_alias"] = capsule["source_provenance"]["task_stream_sha256"]

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_visibility_boundary(capsule)
    assert raised.value.code == "FUTURE_EVIDENCE_LEAKAGE"


def test_visibility_boundary_rejects_audit_suffix_reachability() -> None:
    capsule = _synthetic_visibility_capsule()
    capsule["runtime"]["post_action_audit"] = {}

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_visibility_boundary(capsule)
    assert raised.value.code == "FIELD_VISIBILITY_INVALID"


def test_runtime_replay_binding_is_exact_source_projection() -> None:
    capsule, payloads, semantic_request = _synthetic_runtime_binding()
    binding = capsule["runtime"]["non_history_envelope"]["replay_binding"]

    replay_capsules._validate_runtime_replay_binding(capsule, payloads, semantic_request)

    assert binding["binding_version"] == "mobileworld.g1.replay-binding/v1"
    assert binding["provider"]["excluded_transport_fields_send_eligible"] is False
    assert binding["parser"]["binding_id"] == ("qwen3vl_8b:production-next-action-parser/v1")
    assert (
        binding["provider"]["endpoint_path"]
        == capsule["source_provenance"]["provider"]["endpoint_path"]
    )
    assert binding["model"]["revision"] == capsule["source_provenance"]["model"]["revision"]


def test_runtime_replay_binding_rejects_provider_tamper() -> None:
    capsule, payloads, semantic_request = _synthetic_runtime_binding()
    capsule["runtime"]["non_history_envelope"]["replay_binding"]["provider"]["endpoint_path"] = (
        "/v1/different"
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_runtime_replay_binding(capsule, payloads, semantic_request)
    assert raised.value.code == "REGISTRY_BINDING_INVALID"


def test_runtime_replay_binding_rejects_parser_artifact_tamper() -> None:
    capsule, payloads, semantic_request = _synthetic_runtime_binding()
    parser_name = capsule["runtime"]["non_history_envelope"]["replay_binding"]["parser"][
        "implementation_ref"
    ]["relative_path"]
    payloads[parser_name] += b" "

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_runtime_replay_binding(capsule, payloads, semantic_request)
    assert raised.value.code == "CAPSULE_HASH_MISMATCH"


def test_runtime_replay_binding_rejects_self_consistent_decoding_semantic_drift() -> None:
    capsule, payloads, semantic_request = _synthetic_runtime_binding()
    provenance = capsule["source_provenance"]
    provider = provenance["provider"]
    old_name = provider["decoding_configuration_ref"]["relative_path"]
    parser_ref = capsule["runtime"]["non_history_envelope"]["replay_binding"]["parser"][
        "implementation_ref"
    ]
    tampered_decoding = {
        "model": "Qwen3-VL-8B-Instruct",
        "temperature": 0.5,
    }
    tampered_data = replay_capsules.canonical_json_bytes(tampered_decoding)
    tampered_name = f"artifact-{_sha256(tampered_data)}"
    tampered_ref = {
        "store_id": "G1_3_PUBLICATION",
        **replay_capsules._file_summary(tampered_data, tampered_name),
    }
    del payloads[old_name]
    payloads[tampered_name] = tampered_data
    provider["decoding_configuration_ref"] = tampered_ref
    provider["decoding_configuration_sha256"] = replay_capsules.canonical_sha256(tampered_decoding)
    capsule["runtime"]["non_history_envelope"]["provider_envelope_sha256"] = (
        replay_capsules.canonical_sha256(tampered_decoding)
    )
    capsule["runtime"]["non_history_envelope"]["replay_binding"] = (
        replay_capsules._runtime_replay_binding(provenance, parser_ref)
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_runtime_replay_binding(capsule, payloads, semantic_request)
    assert raised.value.code == "REQUEST_HASH_MISMATCH"


def test_event_stream_cutoff_ignores_malformed_late_suffix_but_full_load_rejects(
    tmp_path: Path,
) -> None:
    relative_path = PurePosixPath("events/task.jsonl")
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    first = _synthetic_collector_event(1)
    malformed_suffix = {"not": "a Collector event envelope"}
    data = replay_capsules.canonical_json_line(first) + replay_capsules.canonical_json_line(
        malformed_suffix
    )
    path.write_bytes(data)
    expected_sha256 = _sha256(data)
    task_run_id = first["task_run_id"]

    prefix = replay_capsules._load_event_stream(
        tmp_path,
        relative_path,
        expected_sha256=expected_sha256,
        expected_task_run_id=task_run_id,
        max_seq=1,
    )

    assert prefix.events == (first,)
    assert prefix.sha256 == expected_sha256
    assert prefix.byte_count == len(data)
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._load_event_stream(
            tmp_path,
            relative_path,
            expected_sha256=expected_sha256,
            expected_task_run_id=task_run_id,
        )
    assert raised.value.code == "RAW_EVENT_CHAIN_INVALID"


def test_pre_cutoff_request_failure_precedes_malformed_suffix_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_base = tmp_path / "source"
    (source_base / "run").mkdir(parents=True)
    cutoff = 1
    loader_cutoffs: list[int | None] = []
    prefix = replay_capsules.EventStream(
        relative_path="events/task.jsonl",
        sha256="a" * 64,
        byte_count=1,
        events=(),
        line_sha256_by_id={},
        event_by_id={},
    )

    def staged_stream_loader(*args: Any, max_seq: int | None = None, **kwargs: Any) -> Any:
        del args, kwargs
        loader_cutoffs.append(max_seq)
        if max_seq is None:
            raise replay_capsules.ReplayCapsuleError(
                "RAW_EVENT_CHAIN_INVALID", "malformed post-cutoff suffix", stage="SOURCE"
            )
        return prefix

    semantic_request = {"messages": [], "model": "pinned-model"}

    class SyntheticSerializer:
        def __init__(self, blob_store: Any) -> None:
            del blob_store

        def load_graph(self, reference: Any) -> dict[str, Any]:
            del reference
            return {}

        def rehydrate(self, graph: Any) -> dict[str, Any]:
            del graph
            return semantic_request

    blob_digest = "b" * 64
    blob_ref = {
        "algorithm": "sha256",
        "digest": blob_digest,
        "byte_length": 1,
        "media_type": "application/json",
        "relative_path": f"blobs/sha256/{blob_digest[:2]}/{blob_digest}",
    }
    unit = replace(
        _registry_unit(),
        record={
            "curated": True,
            "deployment_prediction": False,
            "protocol_version": replay_capsules.PROTOCOL_VERSION,
            "frozen_capsule": {
                "source_locator": {
                    "source_relative_run_path": "run",
                    "task_stream_relative_path": "events/task.jsonl",
                    "task_stream_sha256": "a" * 64,
                },
                "sdk_arguments_canonical_sha256": "0" * 64,
            },
            "decision": {"request_cutoff": {"event_seq": cutoff}},
            "task": {"task_run_id": "0198a000-0000-7000-8000-000000000002"},
        },
    )
    chain = {
        "request": {"payload": {"sdk_arguments_snapshot_blob": blob_ref}},
        "pre": {},
        "task_started": {},
    }
    monkeypatch.setattr(replay_capsules, "_load_event_stream", staged_stream_loader)
    monkeypatch.setattr(replay_capsules, "_load_run_manifest", lambda *args: {})
    monkeypatch.setattr(replay_capsules, "_validate_run_manifest_binding", lambda **kwargs: None)
    monkeypatch.setattr(replay_capsules, "_resolve_pre_cutoff_chain", lambda *args: chain)
    monkeypatch.setattr(replay_capsules, "_verify_blob", lambda *args: None)
    monkeypatch.setattr(replay_capsules, "ArtifactSerializer", SyntheticSerializer)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._materialize_capsule(
            unit=unit,
            repo_root=REPOSITORY_ROOT,
            source_base=source_base,
            source_spec={},
            model_record={},
            visibility_sha256="c" * 64,
            stream_cache={},
            sink=replay_capsules.DerivedArtifactSink({}),
        )
    assert raised.value.code == "REQUEST_HASH_MISMATCH"
    assert loader_cutoffs == [cutoff]


def test_prefix_full_stream_cross_bind_rejects_restored_source_bytes(
    frozen_inputs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    if not FROZEN_SOURCE_BASE.is_dir():
        pytest.skip("Collector source base is not mounted")
    unit = frozen_inputs["population"][0]
    source_by_key = {
        record["source_key"]: record for record in frozen_inputs["source_config"]["sources"]
    }
    model_by_id = {
        record["model_id"]: record for record in frozen_inputs["model_manifest"]["models"]
    }
    original_loader = replay_capsules._load_event_stream
    loader_modes: list[tuple[int | None, bool]] = []

    def restored_source_loader(*args: Any, **kwargs: Any) -> Any:
        stream = original_loader(*args, **kwargs)
        max_seq = kwargs.get("max_seq")
        verify_full_identity = kwargs.get("verify_full_identity", True)
        loader_modes.append((max_seq, verify_full_identity))
        if max_seq is None:
            return stream
        events = deepcopy(list(stream.events))
        tampered = events[0]
        producer_version = tampered["producer"]["version"]
        tampered["producer"]["version"] = "x" * len(producer_version)
        event_id = tampered["event_id"]
        event_by_id = dict(stream.event_by_id)
        event_by_id[event_id] = tampered
        line_sha256_by_id = dict(stream.line_sha256_by_id)
        line_sha256_by_id[event_id] = _sha256(replay_capsules.canonical_json_line(tampered))
        return replace(
            stream,
            events=tuple(events),
            event_by_id=event_by_id,
            line_sha256_by_id=line_sha256_by_id,
        )

    monkeypatch.setattr(replay_capsules, "_load_event_stream", restored_source_loader)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._materialize_capsule(
            unit=unit,
            repo_root=REPOSITORY_ROOT,
            source_base=FROZEN_SOURCE_BASE,
            source_spec=source_by_key[unit.record["source_key"]],
            model_record=model_by_id[unit.record["model_id"]],
            visibility_sha256=replay_capsules.canonical_sha256(
                replay_capsules.field_visibility_policy()
            ),
            stream_cache={},
            sink=replay_capsules.DerivedArtifactSink({}),
        )
    assert raised.value.code == "SOURCE_HASH_MISMATCH"
    assert str(raised.value) == (
        "pre-cutoff event bytes changed between prefix validation and full-stream sealing"
    )
    assert loader_modes == [
        (unit.record["decision"]["request_cutoff"]["event_seq"], False),
        (None, True),
    ]


def test_real_formal_phase_capsules_all_190_units(
    real_formal_artifacts: dict[str, Any],
) -> None:
    manifest = real_formal_artifacts["manifest"]
    integrity = real_formal_artifacts["integrity"]

    assert manifest["publication_phase"] == "FORMAL_PUBLICATION_READY"
    assert manifest["finalization"] == {
        "integrity_report_phase": "FORMAL_PUBLICATION_READY",
        "double_build_status": "PASSED",
        "manifest_hash_subject": "EXACT_CANONICAL_MANIFEST_FILE_BYTES",
        "publication_install_status": "PREINSTALL_NOT_PUBLISHED",
        "formal_publication_allowed": True,
        "post_install_facts_location": "EXTERNAL_READ_ONLY_DIRECTORY_VALIDATION_RECEIPT",
    }
    assert manifest["counts"]["target_unit_count"] == 190
    assert manifest["counts"]["capsuled_count"] == 190
    assert manifest["counts"]["excluded_count"] == 0
    assert len(real_formal_artifacts["capsules"]) == 190
    assert real_formal_artifacts["exclusions"] == []
    assert len(manifest["units"]) == 190
    assert manifest["readiness"]["all_target_units_capsuled"] is True
    assert manifest["readiness"]["formal_acceptance_ready"] is True
    assert manifest["readiness"]["execution_ready"] is False
    assert manifest["readiness"]["provider_invocation_allowed"] is False
    assert manifest["readiness"]["treatment_response_generation_allowed"] is False
    assert manifest["schema_version"] == replay_capsules.MANIFEST_SCHEMA_VERSION
    assert integrity["schema_version"] == replay_capsules.INTEGRITY_SCHEMA_VERSION
    assert manifest["builder_contract"]["builder_version"] == replay_capsules.BUILDER_VERSION
    assert manifest["builder_contract"]["capsule_schema_version"] == (
        replay_capsules.CAPSULE_SCHEMA_VERSION
    )
    assert manifest["builder_contract"]["manifest_schema_version"] == (
        replay_capsules.MANIFEST_SCHEMA_VERSION
    )
    assert manifest["builder_contract"]["integrity_schema_version"] == (
        replay_capsules.INTEGRITY_SCHEMA_VERSION
    )
    assert manifest["builder_contract"]["contract_amendment_version"] == (
        replay_capsules.CONTRACT_AMENDMENT_VERSION
    )
    assert integrity["report_phase"] == "FORMAL_PUBLICATION_READY"
    assert integrity["double_build"]["status"] == "PASSED"
    assert integrity["double_build"]["performed"] is True
    assert integrity["double_build"]["output_file_set_bytes_identical"] is True
    assert integrity["double_build"]["manifest_bytes_identical"] is True
    assert integrity["double_build"]["integrity_report_bytes_identical"] is True
    assert (
        integrity["source_immutability"]["pre_build_source_file_set_sha256"]
        == (integrity["source_immutability"]["post_build_source_file_set_sha256"])
    )
    assert manifest["files"]["final_root_file_count"] == len(real_formal_artifacts["file_payloads"])
    assert len(real_formal_artifacts["file_payloads"]) == 1600
    parser_refs_by_model = {
        envelope["capsule"]["unit"]["model_id"]: envelope["capsule"]["runtime"][
            "non_history_envelope"
        ]["replay_binding"]["parser"]["implementation_ref"]
        for envelope in real_formal_artifacts["capsules"]
    }
    assert set(parser_refs_by_model) == {"qwen3vl_8b", "mai_ui_8b"}
    assert len({ref["relative_path"] for ref in parser_refs_by_model.values()}) == 2
    for reference in parser_refs_by_model.values():
        parser_bytes = real_formal_artifacts["file_payloads"][reference["relative_path"]]
        replay_capsules._verify_file_summary(parser_bytes, reference, reference["relative_path"])
    assert manifest["safety"]["provider_invoked"] is False
    assert manifest["safety"]["provider_invocation_allowed"] is False
    assert manifest["safety"]["treatment_response_generation_allowed"] is False
    assert manifest["safety"]["gpu_used"] is False
    assert manifest["safety"]["raw_collector_mutated"] is False
    assert integrity["safety"]["provider_invoked"] is False
    assert integrity["safety"]["provider_invocation_allowed"] is False
    assert integrity["safety"]["treatment_response_generation_allowed"] is False
    assert integrity["safety"]["execution_ready"] is False
    assert all(
        envelope["schema_version"] == replay_capsules.CAPSULE_SCHEMA_VERSION
        and envelope["capsule"]["safety"]["execution_ready"] is False
        and envelope["capsule"]["safety"]["provider_invocation_allowed"] is False
        and envelope["capsule"]["safety"]["treatment_response_generation_allowed"] is False
        for envelope in real_formal_artifacts["capsules"]
    )


def test_real_formal_index_set_and_outer_body_hashes_are_exact(
    real_formal_artifacts: dict[str, Any],
) -> None:
    manifest = real_formal_artifacts["manifest"]
    payloads = real_formal_artifacts["file_payloads"]
    index_lines = payloads["capsule_index.jsonl"].splitlines(keepends=True)
    parsed_index = [
        replay_capsules._load_canonical_line_bytes(line, Path("capsule_index.jsonl"), line_number)
        for line_number, line in enumerate(index_lines, start=1)
    ]

    assert len(index_lines) == 190
    assert all(line.endswith(b"\n") for line in index_lines)
    assert parsed_index == manifest["units"]
    assert parsed_index == sorted(parsed_index, key=replay_capsules._manifest_unit_sort_key)
    assert len({unit["unit_id"] for unit in parsed_index}) == 190
    assert Counter(unit["unit_kind"] for unit in parsed_index) == {
        "STRICT_MHR": 152,
        "CLEAN_CONTROL": 38,
    }
    assert Counter(unit["model_id"] for unit in parsed_index) == {
        "qwen3vl_8b": 169,
        "mai_ui_8b": 21,
    }
    assert all(unit["disposition"] == "CAPSULED" for unit in parsed_index)
    assert replay_capsules._capsule_set_sha256(parsed_index) == (manifest["capsule_set_sha256"])

    capsules_by_unit = {
        envelope["capsule"]["unit"]["unit_id"]: envelope
        for envelope in real_formal_artifacts["capsules"]
    }
    for unit in parsed_index:
        envelope = capsules_by_unit[unit["unit_id"]]
        assert envelope["capsule_body_sha256"] == replay_capsules.canonical_sha256(
            envelope["capsule"]
        )
        assert envelope["capsule_body_sha256"] == unit["capsule_body_sha256"]
        capsule_bytes = payloads[unit["capsule_ref"]["relative_path"]]
        assert capsule_bytes == replay_capsules.canonical_json_line(envelope)
        assert _sha256(capsule_bytes) == unit["capsule_ref"]["sha256"]
        assert len(capsule_bytes) == unit["capsule_ref"]["byte_count"]
        assert b"/shared/linqiang/" not in capsule_bytes


def test_real_formal_artifacts_validate_with_offline_schema_store(
    real_formal_artifacts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbid_remote_resolution(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("formal schema validation attempted a network lookup")

    monkeypatch.setattr(
        replay_capsules.RefResolver,
        "resolve_remote",
        forbid_remote_resolution,
    )
    replay_capsules._validate_artifacts_against_schemas(REPOSITORY_ROOT, real_formal_artifacts)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("provider_invocation_allowed", None, id="provider-missing"),
        pytest.param("provider_invocation_allowed", True, id="provider-true"),
        pytest.param("provider_invocation_allowed", 0, id="provider-integer-zero"),
        pytest.param("provider_invocation_allowed", "false", id="provider-non-boolean"),
        pytest.param(
            "treatment_response_generation_allowed",
            None,
            id="treatment-missing",
        ),
        pytest.param(
            "treatment_response_generation_allowed",
            True,
            id="treatment-true",
        ),
        pytest.param(
            "treatment_response_generation_allowed",
            0,
            id="treatment-integer-zero",
        ),
        pytest.param(
            "treatment_response_generation_allowed",
            "false",
            id="treatment-non-boolean",
        ),
    ],
)
def test_active_capsule_rejects_missing_true_or_non_boolean_authorization_guard(
    real_formal_artifacts: dict[str, Any],
    field: str,
    invalid_value: Any,
) -> None:
    envelope = deepcopy(real_formal_artifacts["capsules"][0])
    safety = envelope["capsule"]["safety"]
    if invalid_value is None:
        safety.pop(field)
    else:
        safety[field] = invalid_value
    envelope["capsule_body_sha256"] = replay_capsules.canonical_sha256(envelope["capsule"])
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as schema_error:
        replay_capsules._validate_instance(
            validators["capsule"],
            envelope,
            label="tampered active replay capsule",
        )
    assert schema_error.value.code == "SCHEMA_VALIDATION_FAILED"

    tampered_artifacts = dict(real_formal_artifacts)
    tampered_artifacts["capsules"] = [envelope, *real_formal_artifacts["capsules"][1:]]
    with pytest.raises(replay_capsules.ReplayCapsuleError) as guard_error:
        replay_capsules._validate_active_authorization_guards(tampered_artifacts)
    assert guard_error.value.code == "SCHEMA_VALIDATION_FAILED"
    assert guard_error.value.json_path.endswith(f"/capsule/safety/{field}")


def test_safety_telemetry_and_authorization_guards_are_distinct() -> None:
    safety = replay_capsules._safety_flags()

    assert safety["provider_invoked"] is False
    assert safety["provider_invocation_allowed"] is False
    assert safety["treatment_response_generation_allowed"] is False
    assert safety["execution_ready"] is False
    assert "provider_invoked" != "provider_invocation_allowed"


def test_schema_dispatch_preserves_legacy_v1_without_reinterpreting_it(
    real_formal_artifacts: dict[str, Any],
) -> None:
    active = deepcopy(real_formal_artifacts["capsules"][0])
    legacy = deepcopy(active)
    legacy["schema_version"] = replay_capsules.LEGACY_CAPSULE_SCHEMA_VERSION
    legacy["capsule"]["safety"].pop("provider_invocation_allowed")
    legacy["capsule"]["safety"].pop("treatment_response_generation_allowed")
    legacy["capsule_body_sha256"] = replay_capsules.canonical_sha256(legacy["capsule"])
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)

    replay_capsules._validate_instance(
        replay_capsules._versioned_validator(
            validators,
            artifact_kind="capsule",
            instance=legacy,
        ),
        legacy,
        label="legacy replay capsule",
    )
    with pytest.raises(replay_capsules.ReplayCapsuleError):
        replay_capsules._validate_instance(
            validators["capsule"],
            legacy,
            label="legacy capsule under active schema",
        )
    with pytest.raises(replay_capsules.ReplayCapsuleError):
        replay_capsules._validate_instance(
            validators["legacy_capsule"],
            active,
            label="active capsule under legacy schema",
        )


def test_real_capsule_body_tamper_is_rejected(
    real_formal_artifacts: dict[str, Any],
) -> None:
    tampered_envelope = deepcopy(real_formal_artifacts["capsules"][0])
    tampered_envelope["capsule"]["unit"]["target_step"] += 1
    tampered_artifacts = dict(real_formal_artifacts)
    tampered_artifacts["capsules"] = [
        tampered_envelope,
        *real_formal_artifacts["capsules"][1:],
    ]

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_artifacts_against_schemas(REPOSITORY_ROOT, tampered_artifacts)
    assert raised.value.code == "CAPSULE_HASH_MISMATCH"


def test_real_capsule_payload_tamper_is_rejected(
    real_formal_artifacts: dict[str, Any],
) -> None:
    capsule_ref = real_formal_artifacts["manifest"]["units"][0]["capsule_ref"]
    capsule_name = capsule_ref["relative_path"]
    original = real_formal_artifacts["file_payloads"][capsule_name]
    tampered_payloads = dict(real_formal_artifacts["file_payloads"])
    tampered_payloads[capsule_name] = original[:-1] + b" "
    tampered_artifacts = dict(real_formal_artifacts)
    tampered_artifacts["file_payloads"] = tampered_payloads

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_artifacts_against_schemas(REPOSITORY_ROOT, tampered_artifacts)
    assert raised.value.code == "CAPSULE_HASH_MISMATCH"


def test_real_capsule_rejects_missing_direct_transitive_reference(
    real_formal_artifacts: dict[str, Any],
) -> None:
    capsule = deepcopy(real_formal_artifacts["capsules"][0]["capsule"])
    direct_ref = capsule["runtime"]["model_visible"]["semantic_request"][
        "canonical_semantic_request_ref"
    ]
    closure = capsule["artifact_closure"]
    original_count = len(closure["entries"])
    closure["entries"] = [
        entry
        for entry in closure["entries"]
        if not (entry["section"] == "FROZEN_MODEL_VISIBLE" and entry["reference"] == direct_ref)
    ]
    assert len(closure["entries"]) == original_count - 1
    _refresh_closure_metadata(closure)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_artifact_closure(capsule)
    assert raised.value.code == "BLOB_REFERENCE_INVALID"


def test_real_partition_rejects_duplicate_history_owner(
    real_formal_artifacts: dict[str, Any],
) -> None:
    envelope = deepcopy(_real_capsule_for_family(real_formal_artifacts, "flat_progress"))
    semantic_request, _request_view = _request_pair_for_capsule(real_formal_artifacts, envelope)
    model_visible = envelope["capsule"]["runtime"]["model_visible"]
    history = next(
        region for region in model_visible["region_partition"] if region["kind"] == "HISTORY"
    )
    model_visible["region_partition"].append(deepcopy(history))
    model_visible["partition_sha256"] = replay_capsules.canonical_sha256(
        model_visible["region_partition"]
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_semantic_request_partition(
            semantic_request, model_visible["region_partition"]
        )
    assert raised.value.code == "REQUEST_PARTITION_INCOMPLETE"


def test_real_qwen_target_rejects_character_utf8_disagreement(
    real_formal_artifacts: dict[str, Any], frozen_inputs: dict[str, Any]
) -> None:
    envelope = _real_capsule_for_family(real_formal_artifacts, "flat_progress")
    unit = _registry_unit_for_capsule(frozen_inputs, envelope)
    semantic_request, request_view = _request_pair_for_capsule(real_formal_artifacts, envelope)
    record = deepcopy(unit.record)
    record["target_histories"][0]["utf8_byte_end"] += 1

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._resolve_targets(
            replace(unit, record=record), semantic_request, request_view
        )
    assert raised.value.code == "TARGET_SPAN_COORDINATE_MISMATCH"


def test_real_target_resolution_rejects_duplicate_candidate_binding(
    real_formal_artifacts: dict[str, Any], frozen_inputs: dict[str, Any]
) -> None:
    envelope = _real_capsule_for_family(real_formal_artifacts, "flat_progress")
    unit = _registry_unit_for_capsule(frozen_inputs, envelope)
    semantic_request, request_view = _request_pair_for_capsule(real_formal_artifacts, envelope)
    record = deepcopy(unit.record)
    record["target_histories"].append(deepcopy(record["target_histories"][0]))
    record["frozen_capsule"]["resolved_target_spans"].append(
        deepcopy(record["frozen_capsule"]["resolved_target_spans"][0])
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._resolve_targets(
            replace(unit, record=record), semantic_request, request_view
        )
    assert raised.value.code == "TARGET_SPAN_AMBIGUOUS"


def test_real_mai_target_rejects_premature_focal_edit_materialization(
    real_formal_artifacts: dict[str, Any], frozen_inputs: dict[str, Any]
) -> None:
    envelope = _real_capsule_for_family(real_formal_artifacts, "raw_replay")
    unit = _registry_unit_for_capsule(frozen_inputs, envelope)
    semantic_request, request_view = _request_pair_for_capsule(real_formal_artifacts, envelope)
    record = deepcopy(unit.record)
    history = record["target_histories"][0]
    history["edit_span_status"] = "G1_1_FROZEN"
    history["focal_edit_spans"] = [
        {
            "char_start": history["char_start"],
            "char_end": history["char_end"],
            "utf8_byte_start": history["utf8_byte_start"],
            "utf8_byte_end": history["utf8_byte_end"],
            "span_sha256": history["span_sha256"],
        }
    ]

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._resolve_targets(
            replace(unit, record=record), semantic_request, request_view
        )
    assert raised.value.code == "TARGET_SPAN_COORDINATE_MISMATCH"


def test_real_capsule_schema_rejects_invalid_restore_union(
    real_formal_artifacts: dict[str, Any],
) -> None:
    envelope = deepcopy(real_formal_artifacts["capsules"][0])
    descriptor = envelope["capsule"]["runtime"]["non_history_envelope"]["restore_descriptor"]
    descriptor["external_state_consulted"] = not descriptor["external_state_consulted"]
    envelope["capsule_body_sha256"] = replay_capsules.canonical_sha256(envelope["capsule"])
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_instance(
            validators["capsule"], envelope, label="invalid restore union"
        )
    assert raised.value.code == "SCHEMA_VALIDATION_FAILED"


def test_real_capsule_rejects_post_cutoff_runtime_evidence(
    real_formal_artifacts: dict[str, Any],
) -> None:
    capsule = deepcopy(real_formal_artifacts["capsules"][0]["capsule"])
    cutoff = capsule["unit"]["request_cutoff"]["event_seq"]
    capsule["runtime"]["model_visible"]["semantic_request"]["request_event"]["seq"] = cutoff + 1

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_visibility_boundary(capsule)
    assert raised.value.code == "FUTURE_EVIDENCE_LEAKAGE"


def test_real_capsule_rejects_audit_suffix_reachability_from_runtime(
    real_formal_artifacts: dict[str, Any],
) -> None:
    capsule = deepcopy(real_formal_artifacts["capsules"][0]["capsule"])
    capsule["runtime"]["post_action_audit"] = {}

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_visibility_boundary(capsule)
    assert raised.value.code == "FIELD_VISIBILITY_INVALID"


def test_real_writer_and_source_bound_directory_validation(
    real_formal_artifacts: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_bytes = real_formal_artifacts["file_payloads"]["capsule_manifest.json"]
    destination = tmp_path / _sha256(manifest_bytes)
    source_rebuild_calls: list[dict[str, Any]] = []

    try:

        def cached_source_rebuild(**kwargs: Any) -> dict[str, Any]:
            source_rebuild_calls.append(kwargs)
            return real_formal_artifacts

        monkeypatch.setattr(
            replay_capsules,
            "build_verified_capsule_artifacts",
            cached_source_rebuild,
        )
        write_receipt = replay_capsules.write_capsule_artifacts(
            real_formal_artifacts,
            destination,
            repo_root=REPOSITORY_ROOT,
            registry_root=FROZEN_REGISTRY_ROOT,
            source_base=FROZEN_SOURCE_BASE,
        )
        assert write_receipt["valid"] is True
        assert write_receipt["artifact_schema_generation"] == "ACTIVE_V1_1"
        assert write_receipt["capsule_schema_version"] == replay_capsules.CAPSULE_SCHEMA_VERSION
        assert write_receipt["superseded_for_formal_g1"] is False
        assert write_receipt["validation_scope"] == "SOURCE_BOUND"
        assert write_receipt["structural_valid"] is True
        assert write_receipt["source_bound_valid"] is True
        assert write_receipt["formal_publication_valid"] is True
        assert write_receipt["source_rebuild_performed"] is True
        assert write_receipt["source_rebuild_byte_identical"] is True
        assert write_receipt["manifest_sha256"] == destination.name
        assert write_receipt["file_count"] == len(real_formal_artifacts["file_payloads"])
        assert destination.stat().st_mode & 0o222 == 0
        assert all(path.stat().st_mode & 0o222 == 0 for path in destination.iterdir())
        assert source_rebuild_calls == [
            {
                "repo_root": REPOSITORY_ROOT,
                "registry_root": FROZEN_REGISTRY_ROOT,
                "source_base": FROZEN_SOURCE_BASE,
            },
            {
                "repo_root": REPOSITORY_ROOT.resolve(),
                "registry_root": FROZEN_REGISTRY_ROOT.resolve(),
                "source_base": FROZEN_SOURCE_BASE.resolve(),
            },
        ]
        structural_receipt = replay_capsules.validate_capsule_directory(destination)
        assert structural_receipt["validation_scope"] == "STRUCTURAL_ONLY"
        assert structural_receipt["structural_valid"] is True
        assert structural_receipt["source_bound_valid"] is False
        assert structural_receipt["formal_publication_valid"] is False
        assert structural_receipt["source_rebuild_performed"] is False
        assert structural_receipt["source_rebuild_byte_identical"] is False
        assert structural_receipt["exact_file_set"] is True
        assert structural_receipt["regular_files_only"] is True
        assert structural_receipt["zero_symlinks"] is True
        assert structural_receipt["read_only"] is True
        assert structural_receipt["provider_invoked"] is False
        assert structural_receipt["provider_invocation_allowed"] is False
        assert structural_receipt["treatment_response_generation_allowed"] is False
        assert structural_receipt["execution_ready"] is False
        assert structural_receipt["gpu_used"] is False
    finally:
        if destination.exists():
            os.chmod(destination, 0o755)


def test_legacy_publication_is_structurally_identified_but_cannot_be_source_bound_formal(
    real_formal_artifacts: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not LEGACY_G1_3_PUBLICATION.is_dir():
        pytest.skip("immutable legacy G1.3 publication is not mounted")
    manifest_path = LEGACY_G1_3_PUBLICATION / "capsule_manifest.json"
    before = (
        manifest_path.read_bytes(),
        LEGACY_G1_3_PUBLICATION.stat().st_mode,
        manifest_path.stat().st_mode,
    )

    receipt = replay_capsules.validate_capsule_directory(LEGACY_G1_3_PUBLICATION)

    assert receipt["valid"] is True
    assert receipt["validation_scope"] == "STRUCTURAL_ONLY"
    assert receipt["artifact_schema_generation"] == "LEGACY_V1"
    assert receipt["capsule_schema_version"] == replay_capsules.LEGACY_CAPSULE_SCHEMA_VERSION
    assert receipt["superseded_for_formal_g1"] is True
    assert receipt["source_bound_valid"] is False
    assert receipt["formal_publication_valid"] is False

    monkeypatch.setattr(
        replay_capsules,
        "build_verified_capsule_artifacts",
        lambda **_kwargs: real_formal_artifacts,
    )
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.validate_capsule_directory(
            LEGACY_G1_3_PUBLICATION,
            repo_root=REPOSITORY_ROOT,
            registry_root=FROZEN_REGISTRY_ROOT,
            source_base=FROZEN_SOURCE_BASE,
        )
    assert raised.value.code == "NONDETERMINISTIC_BUILD"
    assert (
        manifest_path.read_bytes(),
        LEGACY_G1_3_PUBLICATION.stat().st_mode,
        manifest_path.stat().st_mode,
    ) == before


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_numbers(nonfinite: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        replay_capsules.canonical_json_line({"value": nonfinite})


def test_schema_registry_resolves_cross_file_reference_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_remote_resolution(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("schema validation attempted a network lookup")

    monkeypatch.setattr(
        replay_capsules.RefResolver,
        "resolve_remote",
        forbid_remote_resolution,
    )
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)
    replay_capsules._validate_instance(
        validators["visibility"],
        replay_capsules.field_visibility_policy(),
        label="field visibility",
    )
    with validators["capsule"].resolver.resolving("field_visibility.schema.json") as schema:
        assert schema["$id"].endswith("/field_visibility.schema.json")


def test_exact_visibility_schema_rejects_policy_drift() -> None:
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)
    tampered = deepcopy(replay_capsules.field_visibility_policy())
    tampered["rules"][0]["allowed_consumers"] = ["PROTOCOL_VALIDATOR"]

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._validate_instance(
            validators["visibility"], tampered, label="tampered visibility"
        )
    assert raised.value.code == "SCHEMA_VALIDATION_FAILED"


def test_exclusion_bytes_are_stable_and_machine_paths_are_not_embedded() -> None:
    unit = _registry_unit()
    first_error = replay_capsules.ReplayCapsuleError(
        "BLOB_HASH_MISMATCH",
        "volatile human detail is not serialized",
        stage="ARTIFACT",
        json_path="/runtime/model_visible",
        zeta=3,
        machine_path="/tmp/private/source",
        alpha="stable",
        expected_sha256="a" * 64,
        observed_sha256="b" * 64,
    )
    second_error = replay_capsules.ReplayCapsuleError(
        "BLOB_HASH_MISMATCH",
        "different detail still yields the same scientific exclusion",
        stage="ARTIFACT",
        json_path="/runtime/model_visible",
        observed_sha256="b" * 64,
        alpha="stable",
        machine_path="/another/private/source",
        expected_sha256="a" * 64,
        zeta=3,
    )

    first = replay_capsules._exclusion_for(unit, first_error)
    second = replay_capsules._exclusion_for(unit, second_error)
    encoded = replay_capsules.canonical_json_line(first)

    assert first == second
    assert replay_capsules.canonical_json_line(second) == encoded
    assert b"private" not in encoded
    assert first["primary_stage"] == "ARTIFACT"
    assert first["primary_reason_code"] == "BLOB_HASH_MISMATCH"
    assert first["failures"][0]["stable_context"] == [
        {"key": "alpha", "value": "stable"},
        {"key": "expected_sha256", "value": "a" * 64},
        {"key": "observed_sha256", "value": "b" * 64},
        {"key": "zeta", "value": 3},
    ]
    validators = replay_capsules._schema_validators(REPOSITORY_ROOT)
    replay_capsules._validate_instance(validators["exclusion"], first, label="exclusion")


@pytest.mark.parametrize(
    "value",
    ["", "/absolute", "../escape", "a/../escape", "./relative", "a\\b", "a//b"],
)
def test_source_relative_paths_fail_closed(value: str) -> None:
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._safe_relative(value, "source")
    assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"


def test_source_relative_path_and_child_stay_inside_declared_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "runs").mkdir(parents=True)
    outside.mkdir()

    relative = replay_capsules._safe_relative("runs/task.jsonl", "source")
    assert relative == PurePosixPath("runs/task.jsonl")
    assert replay_capsules._safe_child(root, relative) == root / "runs/task.jsonl"

    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._safe_child(root, PurePosixPath("escape/task.jsonl"))
    assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"


def test_source_reader_rejects_symlinks_and_directories(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"{}\n")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    assert replay_capsules._read_regular(target) == b"{}\n"
    for path in (link, tmp_path):
        with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
            replay_capsules._read_regular(path)
        assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"


def test_blob_reader_rejects_intermediate_directory_symlink_escape(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    outside_tree = tmp_path / "outside-blob-tree"
    run_root.mkdir()
    data = b"outside bytes must remain unreachable"
    digest = _sha256(data)
    outside_leaf = outside_tree / "sha256" / digest[:2] / digest
    outside_leaf.parent.mkdir(parents=True)
    outside_leaf.write_bytes(data)
    (run_root / "blobs").symlink_to(outside_tree, target_is_directory=True)
    reference = {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(data),
        "media_type": "application/octet-stream",
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._read_verified_blob(replay_capsules.BlobStore(run_root), reference)
    assert raised.value.code == "BLOB_REFERENCE_INVALID"


def test_blob_reader_keeps_invalid_relative_path_in_blob_reason_domain(tmp_path: Path) -> None:
    reference = {
        "algorithm": "sha256",
        "digest": "0" * 64,
        "byte_length": 1,
        "media_type": "application/octet-stream",
        "relative_path": "../x",
    }

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._read_verified_blob(replay_capsules.BlobStore(tmp_path), reference)
    assert raised.value.code == "BLOB_REFERENCE_INVALID"
    assert raised.value.stage == "BLOB"


def test_blob_reader_reports_missing_regular_leaf(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    data = b"never installed"
    digest = _sha256(data)
    (run_root / "blobs" / "sha256" / digest[:2]).mkdir(parents=True)
    reference = {
        "algorithm": "sha256",
        "digest": digest,
        "byte_length": len(data),
        "media_type": "application/octet-stream",
        "relative_path": f"blobs/sha256/{digest[:2]}/{digest}",
    }

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._read_verified_blob(replay_capsules.BlobStore(run_root), reference)
    assert raised.value.code == "BLOB_MISSING"


def test_blob_reader_accepts_matching_regular_leaf(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    store = replay_capsules.BlobStore(run_root)
    data = b"content-addressed immutable bytes"
    reference = store.put_bytes(data, "application/octet-stream")

    assert replay_capsules._read_verified_blob(store, reference) == data


def test_canonical_loader_rejects_duplicate_noncanonical_and_unterminated_json() -> None:
    label = Path("synthetic.json")
    assert replay_capsules._load_canonical_object_bytes(b'{"a":1}\n', label) == {"a": 1}
    for payload in (b'{"a":1,"a":2}\n', b'{ "a": 1 }\n', b'{"a":1}'):
        with pytest.raises(replay_capsules.ReplayCapsuleError):
            replay_capsules._load_canonical_object_bytes(payload, label)


def test_file_summary_rejects_single_byte_tamper() -> None:
    original = b"immutable\n"
    summary = replay_capsules._file_summary(original, "field_visibility.json")

    replay_capsules._verify_file_summary(original, summary, "field_visibility.json")
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules._verify_file_summary(b"Immutable\n", summary, "field_visibility.json")
    assert raised.value.code == "CAPSULE_HASH_MISMATCH"


def test_writer_requires_complete_source_bound_roots(tmp_path: Path) -> None:
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.write_capsule_artifacts({}, tmp_path / ("0" * 64))
    assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"


def test_writer_rejects_candidate_artifacts(tmp_path: Path) -> None:
    artifacts = {
        "manifest": {
            "publication_phase": "BUILD_CANDIDATE",
            "finalization": {"formal_publication_allowed": False},
        }
    }
    roots = [tmp_path / name for name in ("repo", "registry", "source")]
    output_parent = tmp_path / "output"
    for root in (*roots, output_parent):
        root.mkdir()
    destination = output_parent / ("0" * 64)

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.write_capsule_artifacts(
            artifacts,
            destination,
            repo_root=roots[0],
            registry_root=roots[1],
            source_base=roots[2],
        )
    assert raised.value.code == "SCHEMA_VALIDATION_FAILED"
    assert not destination.exists()


def test_writer_rejects_wrong_content_address_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {name: b"payload\n" for name in replay_capsules.BASE_OUTPUT_FILE_NAMES}
    artifacts = {
        "manifest": {
            "schema_version": replay_capsules.MANIFEST_SCHEMA_VERSION,
            "publication_phase": "FORMAL_PUBLICATION_READY",
            "finalization": {"formal_publication_allowed": True},
        },
        "integrity": {"schema_version": replay_capsules.INTEGRITY_SCHEMA_VERSION},
        "capsules": [],
        "file_payloads": payloads,
    }
    monkeypatch.setattr(replay_capsules, "_validate_artifacts_against_schemas", lambda *_: None)
    monkeypatch.setattr(
        replay_capsules, "build_verified_capsule_artifacts", lambda **_kwargs: artifacts
    )
    roots = [tmp_path / name for name in ("repo", "registry", "source")]
    output_parent = tmp_path / "output"
    for root in (*roots, output_parent):
        root.mkdir()

    destination = output_parent / ("0" * 64)
    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.write_capsule_artifacts(
            artifacts,
            destination,
            repo_root=roots[0],
            registry_root=roots[1],
            source_base=roots[2],
        )
    assert raised.value.code == "CAPSULE_HASH_MISMATCH"
    assert not destination.exists()


def test_writer_rejects_existing_target_before_any_mutation(tmp_path: Path) -> None:
    roots = [tmp_path / name for name in ("repo", "registry", "source")]
    output_parent = tmp_path / "output"
    for root in (*roots, output_parent):
        root.mkdir()
    destination = output_parent / ("0" * 64)
    destination.mkdir()
    marker = destination / "owned-by-someone-else"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.write_capsule_artifacts(
            {},
            destination,
            repo_root=roots[0],
            registry_root=roots[1],
            source_base=roots[2],
        )
    assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_writer_rejects_repository_local_publication_root(tmp_path: Path) -> None:
    destination = REPOSITORY_ROOT / "MobileWorld/tests/offline" / ("0" * 64)
    registry_root = tmp_path / "registry"
    source_base = tmp_path / "source"
    registry_root.mkdir()
    source_base.mkdir()
    assert not destination.exists()

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.write_capsule_artifacts(
            {},
            destination,
            repo_root=REPOSITORY_ROOT,
            registry_root=registry_root,
            source_base=source_base,
        )
    assert raised.value.code == "SOURCE_REFERENCE_UNRESOLVED"
    assert not destination.exists()


def test_candidate_phase_does_not_claim_a_double_build() -> None:
    receipt = replay_capsules._candidate_double_build_receipt()

    assert receipt["status"] == "NOT_PERFORMED"
    assert receipt["performed"] is False
    assert receipt["not_performed_reason"] == "SINGLE_BUILD_CANDIDATE"
    assert receipt["first_core_file_set_sha256"] is None
    assert receipt["output_file_set_bytes_identical"] is None


def test_verified_builder_requires_two_identical_core_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = {
        "capsules": [{} for _ in range(190)],
        "exclusions": [],
        "file_payloads": {
            "core": b"same",
            "capsule_integrity.json": b"candidate-integrity",
            "capsule_manifest.json": b"candidate-manifest",
        },
    }
    build_calls: list[dict[str, Any]] = []
    finalization_receipts: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> dict[str, Any]:
        build_calls.append(kwargs)
        return candidate

    def finalize(
        _candidate: dict[str, Any], receipt: dict[str, Any], _repo: Path
    ) -> dict[str, Any]:
        finalization_receipts.append(receipt)
        return {
            "manifest": {"publication_phase": "FORMAL_PUBLICATION_READY"},
            "file_payloads": {"formal": b"identical"},
        }

    monkeypatch.setattr(replay_capsules, "build_capsule_artifacts", build)
    monkeypatch.setattr(replay_capsules, "_finalize_artifacts", finalize)
    result = replay_capsules.build_verified_capsule_artifacts(
        repo_root=tmp_path,
        registry_root=tmp_path,
        source_base=tmp_path,
    )

    assert len(build_calls) == 2
    assert len(finalization_receipts) == 2
    assert all(receipt["status"] == "PASSED" for receipt in finalization_receipts)
    assert all(receipt["performed"] is True for receipt in finalization_receipts)
    assert all(
        receipt["first_core_file_set_sha256"] == receipt["second_core_file_set_sha256"]
        for receipt in finalization_receipts
    )
    assert result["manifest"]["publication_phase"] == "FORMAL_PUBLICATION_READY"


def test_verified_builder_rejects_core_byte_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidates = iter(
        [
            {"file_payloads": {"core": b"first"}},
            {"file_payloads": {"core": b"second"}},
        ]
    )
    monkeypatch.setattr(
        replay_capsules,
        "build_capsule_artifacts",
        lambda **_kwargs: next(candidates),
    )

    with pytest.raises(replay_capsules.ReplayCapsuleError) as raised:
        replay_capsules.build_verified_capsule_artifacts(
            repo_root=tmp_path,
            registry_root=tmp_path,
            source_base=tmp_path,
        )
    assert raised.value.code == "NONDETERMINISTIC_BUILD"
    assert replay_capsules._normalized_exclusion_stage(raised.value.stage) == "DETERMINISM"


def test_cli_summary_is_truthful_and_safety_explicit() -> None:
    artifacts = _cli_artifacts()
    summary = replay_capsules._cli_summary(artifacts)

    assert summary == {
        "valid": True,
        "builder_version": replay_capsules.BUILDER_VERSION,
        "contract_amendment_version": replay_capsules.CONTRACT_AMENDMENT_VERSION,
        "capsule_schema_version": replay_capsules.CAPSULE_SCHEMA_VERSION,
        "manifest_schema_version": replay_capsules.MANIFEST_SCHEMA_VERSION,
        "integrity_schema_version": replay_capsules.INTEGRITY_SCHEMA_VERSION,
        "publication_phase": "BUILD_CANDIDATE",
        "manifest_sha256": _sha256(b"manifest\n"),
        "capsule_set_sha256": "2" * 64,
        "capsuled_count": 190,
        "excluded_count": 0,
        "file_count": 2,
        "total_byte_count": len(b"manifest\n") + len(b"index\n"),
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
        "raw_collector_mutated": False,
    }


def test_candidate_cli_emits_one_canonical_json_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        replay_capsules,
        "build_capsule_artifacts",
        lambda **_kwargs: _cli_artifacts(),
    )

    result = replay_capsules.main(
        [
            "candidate",
            "--repo-root",
            str(tmp_path),
            "--registry-root",
            str(tmp_path),
            "--source-base",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert result == 0
    assert captured.err == ""
    assert summary["valid"] is True
    assert summary["publication_phase"] == "BUILD_CANDIDATE"
    assert summary["provider_invoked"] is False
    assert summary["provider_invocation_allowed"] is False
    assert summary["treatment_response_generation_allowed"] is False
    assert summary["execution_ready"] is False
    assert summary["gpu_used"] is False


def test_verify_cli_reports_formal_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        replay_capsules,
        "build_verified_capsule_artifacts",
        lambda **_kwargs: _cli_artifacts(phase="FORMAL_PUBLICATION_READY"),
    )

    result = replay_capsules.main(
        [
            "verify",
            "--repo-root",
            str(tmp_path),
            "--registry-root",
            str(tmp_path),
            "--source-base",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert result == 0
    assert captured.err == ""
    assert summary["publication_phase"] == "FORMAL_PUBLICATION_READY"
    assert summary["capsuled_count"] == 190
    assert summary["excluded_count"] == 0
    assert summary["provider_invoked"] is False
    assert summary["provider_invocation_allowed"] is False
    assert summary["treatment_response_generation_allowed"] is False
    assert summary["execution_ready"] is False


def test_validate_cli_structural_only_never_claims_formal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    receipt = {
        "valid": True,
        "validation_scope": "STRUCTURAL_ONLY",
        "structural_valid": True,
        "source_bound_valid": False,
        "formal_publication_valid": False,
        "source_rebuild_performed": False,
        "source_rebuild_byte_identical": False,
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
    }

    def validate(capsule_root: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((capsule_root, kwargs))
        return receipt

    monkeypatch.setattr(replay_capsules, "validate_capsule_directory", validate)
    result = replay_capsules.main(["validate", "--capsule-root", str(tmp_path / ("0" * 64))])
    captured = capsys.readouterr()

    assert result == 0
    assert captured.err == ""
    assert json.loads(captured.out) == receipt
    assert calls == [(str(tmp_path / ("0" * 64)), {})]


def test_cli_failure_is_machine_readable_and_never_claims_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_kwargs: Any) -> Any:
        raise replay_capsules.ReplayCapsuleError(
            "NONDETERMINISTIC_BUILD",
            "mismatch",
            stage="DETERMINISM",
            json_path="/units/0",
        )

    monkeypatch.setattr(replay_capsules, "build_verified_capsule_artifacts", fail)
    result = replay_capsules.main(
        [
            "verify",
            "--repo-root",
            str(tmp_path),
            "--registry-root",
            str(tmp_path),
            "--source-base",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    failure = json.loads(captured.err)

    assert result == 2
    assert captured.out == ""
    assert failure == {
        "valid": False,
        "reason_code": "NONDETERMINISTIC_BUILD",
        "stage": "DETERMINISM",
        "affected_json_pointer": "/units/0",
        "provider_invoked": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "execution_ready": False,
        "gpu_used": False,
        "gui_action_executed": False,
    }
