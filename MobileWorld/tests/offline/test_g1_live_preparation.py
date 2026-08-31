"""CPU-only conformance for the inert G1.4 live-preparation boundary.

These tests deliberately exercise data validation and rendering only.  Every
capability that could contact a provider, inspect a GPU, or start a process is
replaced with a fail-fast bomb in the closure test below.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import importlib
import json
import os
import socket
import subprocess
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator, RefResolver

from mobile_world.offline.causal_replay.contracts import (
    JsonValue,
    canonical_json_bytes,
)
from mobile_world.offline.causal_replay_runner import (
    JsonActionParser,
    OpenAICompatibleProviderCodec,
    ReplayRunnerError,
    execute_live_arm,
)
from mobile_world.offline.causal_replay_runner import (
    live_preparation as live_preparation_module,
)
from mobile_world.offline.causal_replay_runner.cli import main as runner_cli_main
from mobile_world.offline.causal_replay_runner.live_preparation import (
    assess_injected_gpu_inventory,
    decode_openai_chat_envelope,
    load_live_preparation,
    prepare_openai_chat_block,
    prepare_openai_chat_call,
    prepare_vllm_launch_plan,
    render_vllm_launch_argv,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_CONFIG_PATH = REPOSITORY_ROOT / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
MODEL_CONFIG_SHA256 = "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f"
REPLAY_SEEDS = (1729, 2718, 31415)
G14_SCHEMA_ROOT = REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/g1_4"
LIVE_SCHEMA_SHA256 = {
    "live_preparation.schema.json": (
        "ce06e596df293ffa552ddc8d89371a41f4c81255f494011154a4b4729154ac90"
    ),
    "openai_chat_call_plan.schema.json": (
        "7d39d908b348cd3e0241aae80a28a103cf958994e4a16484778c538f626ac8bb"
    ),
    "openai_chat_call_block.schema.json": (
        "735f2d526d4868b0313dfaece6de5482fb6163ff8fb571fe69649c30a4c4f6b6"
    ),
    "vllm_launch_plan.schema.json": (
        "f98ab570fa6560708c9bb2f509e617621a51d01020d6da507ec6cafcfd35aa97"
    ),
    "openai_chat_response_projection.schema.json": (
        "5abebc89bcb5febffbf29bfc9e404950551dbb26dbac26b8ce8233c663ec7299"
    ),
    "injected_gpu_capacity_assessment.schema.json": (
        "e5de1cd6ce9e9d870e93f791e973b16b98905a9f22d99e6f6e24ae3afe9cc4f4"
    ),
}

QWEN_REVISION = "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b"
MAI_REVISION = "e00a0097abb9cc621cac5172d8c4809f0839c94e"

MODEL_EXPECTATIONS: dict[str, dict[str, JsonValue]] = {
    "qwen3vl_8b": {
        "model_repository": "Qwen/Qwen3-VL-8B-Instruct",
        "model_revision": QWEN_REVISION,
        "served_model_name": "Qwen3-VL-8B-Instruct",
        "model_config_record_sha256": (
            "5d6c5c1aa99aa13e8e153a19e6a0b1e8593cf2c32adaf8bca308fc76cea827e3"
        ),
        "formal_request_sha256": (
            "da78559b8c9875c912573ebffa673df0454b6b994a5115fb9e8b0ebbe3e79944"
        ),
        "formal_serving_launch_sha256": (
            "fd0b1ce77744bf95bd6a03a88aa3c76b11365f0def8b323aaa03c558a1b8e8f8"
        ),
        "parser_binding_sha256": (
            "762b231416ce54836b54c6bfac7918cc4d696876d670d7d7aa7a2c52a5585c6b"
        ),
        "captured_request_sha256": (
            "f14fd9b4f383ee75fa0f3c79cba6fbd92d52799dd32d975056df0140195ad9cb"
        ),
        "actor_adapter_sha256": (
            "4f814da955563dd568d3cd9c30b9796775a15dfc1175fa4fc135a379221d84b4"
        ),
        "tokenizer_binding_sha256": (
            "e97afc56a6ce6b1d0d78345efc2b27c9853e9251d1e2f2bb0ff60b9b99926efd"
        ),
        "tokenizer_artifact_count": 5,
        "tokenizer_artifact_byte_count": 11_497_442,
        "checkpoint_inventory_sha256": (
            "3e332c602f6ffcd11311412bf8a986c4e4f888135e719bfcb6ce2d255a9892dc"
        ),
        "config_file_count": 5,
        "config_byte_count": 70_277,
        "weight_shard_count": 4,
        "weight_byte_count": 17_534_339_512,
        "total_file_count": 9,
        "total_byte_count": 17_534_409_789,
    },
    "mai_ui_8b": {
        "model_repository": "Tongyi-MAI/MAI-UI-8B",
        "model_revision": MAI_REVISION,
        "served_model_name": "MAI-UI-8B",
        "model_config_record_sha256": (
            "c633cc272bca6e14ae788d90417c909d515fe1f22abb9c0bf5e02da78c8d7682"
        ),
        "formal_request_sha256": (
            "e33b22040b9c21aafe8b478db7b048e0e30e5737f082a8eec4aec5755f52ce67"
        ),
        "formal_serving_launch_sha256": (
            "fd0b1ce77744bf95bd6a03a88aa3c76b11365f0def8b323aaa03c558a1b8e8f8"
        ),
        "parser_binding_sha256": (
            "9842b43a31579ec280c766715f27d7dbf32877618b65e6b9bfb7d0f5304ca0cc"
        ),
        "captured_request_sha256": (
            "f9cb0d3d3ff77803be2cd6eb1d7aa93a73aee87b3c030a640861be60f53a2b0a"
        ),
        "actor_adapter_sha256": (
            "6aa3e69cc6ec49c83bb25d871c28abcd3efdfcb5b51a8a9b3d478389f9e58868"
        ),
        "tokenizer_binding_sha256": (
            "dac3c7c7da1bcb043402cb3571a0867f98153c4fd3f3c0614153a6ea27518d23"
        ),
        "tokenizer_artifact_count": 7,
        "tokenizer_artifact_byte_count": 15_883_397,
        "checkpoint_inventory_sha256": (
            "0f7124916fc6dce667cfffb1c49258eb6ed0b1cc7c0c3bea6521094394185a53"
        ),
        "config_file_count": 5,
        "config_byte_count": 68_186,
        "weight_shard_count": 4,
        "weight_byte_count": 17_534_339_384,
        "total_file_count": 9,
        "total_byte_count": 17_534_407_570,
    },
}

READINESS_FALSE_FIELDS = {
    "execution_ready",
    "live_transport_validation_complete",
    "live_history_codec_ready",
    "curated_transformations_ready",
    "run_ready_seal_present",
    "provider_invocation_allowed",
    "treatment_response_generation_allowed",
    "formal_replay_ready",
}
SAFETY_FALSE_FIELDS = {
    "client_factory_invoked",
    "network_used",
    "subprocess_started",
    "gpu_probed",
    "gpu_used",
    "model_loaded",
    "provider_invoked",
    "replay_executed",
    "generated_action_executed",
}
ALL_FALSE_FIELDS = READINESS_FALSE_FIELDS | SAFETY_FALSE_FIELDS


def _to_dict(value: object) -> dict[str, JsonValue]:
    method = getattr(value, "to_dict", None)
    assert callable(method), f"{type(value).__name__} must expose canonical to_dict()"
    result = method()
    assert isinstance(result, dict)
    return cast(dict[str, JsonValue], result)


def _reconstruct_dataclass(value: object, **changes: object) -> object:
    """Exercise the public constructor rather than mutating frozen instances."""

    arguments = {field.name: getattr(value, field.name) for field in fields(value)}
    arguments.update(changes)
    return type(value)(**arguments)


def _assert_public_record_forgery_rejected(
    value: object,
    expected_code: str = "LIVE_PREPARATION_RECORD_INVALID",
    **changes: object,
) -> None:
    _assert_error_code(expected_code, lambda: replace(value, **changes))
    _assert_error_code(
        expected_code,
        lambda: _reconstruct_dataclass(value, **changes),
    )


def _all_key_values(value: JsonValue, key: str) -> Iterator[JsonValue]:
    if isinstance(value, dict):
        for candidate_key, candidate_value in value.items():
            if candidate_key == key:
                yield candidate_value
            yield from _all_key_values(candidate_value, key)
    elif isinstance(value, list):
        for item in value:
            yield from _all_key_values(item, key)


def _assert_closed_no_execution_state(value: object) -> None:
    payload = _to_dict(value)
    for key in sorted(ALL_FALSE_FIELDS):
        observed = tuple(_all_key_values(payload, key))
        assert observed, f"required closed no-execution field is missing: {key}"
        assert all(item is False for item in observed), (key, observed)


def _load_manifest_json() -> dict[str, JsonValue]:
    payload = json.loads(MODEL_CONFIG_PATH.read_bytes())
    assert isinstance(payload, dict)
    return cast(dict[str, JsonValue], payload)


def _write_json(tmp_path: Path, payload: Mapping[str, JsonValue], name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(canonical_json_bytes(cast(JsonValue, dict(payload))) + b"\n")
    return path


def _set_nested(payload: dict[str, JsonValue], path: Sequence[str | int], value: JsonValue) -> None:
    cursor: JsonValue = payload
    for part in path[:-1]:
        if isinstance(part, int):
            assert isinstance(cursor, list)
            cursor = cursor[part]
        else:
            assert isinstance(cursor, dict)
            cursor = cursor[part]
    final = path[-1]
    if isinstance(final, int):
        assert isinstance(cursor, list)
        cursor[final] = value
    else:
        assert isinstance(cursor, dict)
        cursor[final] = value


def _assert_error_code(expected_code: str, call: Callable[[], object]) -> None:
    with pytest.raises(ReplayRunnerError) as raised:
        call()
    assert raised.value.code == expected_code
    assert raised.value.provider_invocation_allowed is False


def _live_schema_store() -> dict[str, dict[str, Any]]:
    store: dict[str, dict[str, Any]] = {}
    for name in LIVE_SCHEMA_SHA256:
        schema = json.loads((G14_SCHEMA_ROOT / name).read_bytes())
        assert isinstance(schema, dict)
        schema_id = schema.get("$id")
        assert isinstance(schema_id, str)
        store[schema_id] = schema
    return store


def _live_schema_validator(name: str) -> Draft202012Validator:
    schema = json.loads((G14_SCHEMA_ROOT / name).read_bytes())
    assert isinstance(schema, dict)
    return Draft202012Validator(
        schema,
        resolver=RefResolver.from_schema(schema, store=_live_schema_store()),
    )


def _live_schema_payloads(tmp_path: Path) -> dict[str, dict[str, JsonValue]]:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    application_request = _application_request("qwen3vl_8b")
    call = prepare_openai_chat_call(
        receipt,
        "qwen3vl_8b",
        application_request,
        1729,
    )
    block = prepare_openai_chat_block(
        receipt,
        "qwen3vl_8b",
        (application_request,),
        1729,
    )
    launch = prepare_vllm_launch_plan(
        receipt,
        "qwen3vl_8b",
        _snapshot_path(tmp_path, "qwen3vl_8b"),
    )
    response = decode_openai_chat_envelope(_valid_response_envelope())
    gpu = assess_injected_gpu_inventory(
        receipt,
        "qwen3vl_8b",
        {
            "accelerator": "NVIDIA H200",
            "total_memory_bytes": 143_771_762_688,
            "free_memory_bytes": 40_000_000_000,
        },
    )
    return {
        "live_preparation.schema.json": _to_dict(receipt),
        "openai_chat_call_plan.schema.json": _to_dict(call),
        "openai_chat_call_block.schema.json": _to_dict(block),
        "vllm_launch_plan.schema.json": _to_dict(launch),
        "openai_chat_response_projection.schema.json": _to_dict(response),
        "injected_gpu_capacity_assessment.schema.json": _to_dict(gpu),
    }


def _load_with_test_rebound_integrity(candidate: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    """Reach semantic validators after separately testing the frozen hash gate."""

    data = candidate.read_bytes()
    monkeypatch.setattr(
        live_preparation_module,
        "MODEL_CONFIG_MANIFEST_SHA256",
        hashlib.sha256(data).hexdigest(),
    )
    monkeypatch.setattr(
        live_preparation_module,
        "MODEL_CONFIG_MANIFEST_BYTE_COUNT",
        len(data),
    )
    monkeypatch.setattr(
        live_preparation_module,
        "_require_live_preparation_receipt",
        lambda receipt: receipt,
    )
    return load_live_preparation(candidate)


def _model_mapping(receipt: object) -> dict[str, dict[str, JsonValue]]:
    payload = _to_dict(receipt)
    models = payload.get("models")
    assert isinstance(models, list)
    result: dict[str, dict[str, JsonValue]] = {}
    for item in models:
        assert isinstance(item, dict)
        model_id = item.get("model_id")
        assert isinstance(model_id, str)
        result[model_id] = cast(dict[str, JsonValue], item)
    return result


def _snapshot_path(tmp_path: Path, model_id: str) -> Path:
    if model_id == "qwen3vl_8b":
        return tmp_path / "models--Qwen--Qwen3-VL-8B-Instruct" / "snapshots" / QWEN_REVISION
    assert model_id == "mai_ui_8b"
    return tmp_path / "models--Tongyi-MAI--MAI-UI-8B" / "snapshots" / MAI_REVISION


def _application_request(model_id: str) -> dict[str, JsonValue]:
    model_arguments = cast(
        dict[str, JsonValue],
        copy.deepcopy(
            {
                "model": MODEL_EXPECTATIONS[model_id]["served_model_name"],
                "temperature": 0.0,
            }
        ),
    )
    if model_id == "mai_ui_8b":
        model_arguments.update({"top_p": 1.0, "max_tokens": 2048})
    model_arguments.update(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "Keep every frozen field exact.",
                    "x_host_message_extension": {"opaque": [1, "two", False]},
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What is the next action?"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,AA==",
                                "detail": "high",
                                "x_image_extension": "preserve-me",
                            },
                        },
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "emit_action",
                        "description": "Return one inert structured action.",
                        "parameters": {
                            "type": "object",
                            "properties": {"action_type": {"type": "string"}},
                            "required": ["action_type"],
                            "x_unknown_schema_keyword": {"preserve": True},
                        },
                    },
                }
            ],
            "stream": False,
            "x_unknown_sdk_argument": {"nested": ["opaque", {"future_provider_option": 7}]},
        }
    )
    return model_arguments


def _valid_response_envelope() -> dict[str, JsonValue]:
    return {
        "id": "chatcmpl-inert-fixture",
        "object": "chat.completion",
        "created": 1_788_000_000,
        "model": "Qwen3-VL-8B-Instruct",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '<tool_call>{"action_type":"wait"}</tool_call>',
                    "x_unknown_message_field": ["preserve", 1],
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 101,
            "completion_tokens": 7,
            "total_tokens": 108,
        },
        "x_provider_extension": {"opaque": True},
    }


def test_real_frozen_manifest_loads_twice_with_canonical_byte_identical_receipts() -> None:
    assert hashlib.sha256(MODEL_CONFIG_PATH.read_bytes()).hexdigest() == MODEL_CONFIG_SHA256

    first = load_live_preparation(MODEL_CONFIG_PATH)
    second = load_live_preparation(os.fspath(MODEL_CONFIG_PATH))
    first_bytes = canonical_json_bytes(_to_dict(first))
    second_bytes = canonical_json_bytes(_to_dict(second))

    assert first_bytes == second_bytes
    assert first.canonical_bytes == second.canonical_bytes == first_bytes
    assert first.sha256 == second.sha256
    assert first.sha256 == "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()
    payload = _to_dict(first)
    manifest = payload["model_config_manifest"]
    assert isinstance(manifest, dict)
    assert manifest["sha256"] == MODEL_CONFIG_SHA256
    assert payload["static_configuration_valid"] is True
    assert payload["live_code_prepared"] is True
    assert payload["status"] == "CODE_ONLY_NOT_OBSERVED"
    assert payload["blockers"] == [
        "G1_5",
        "G1_6",
        "G1_7",
        "OWNER_GPU_AUTH",
        "LIVE_ENDPOINT",
    ]
    deferred = payload["deferred_preconditions"]
    assert isinstance(deferred, dict)
    formal_preflight = deferred["formal_preflight"]
    runtime_boundary = deferred["runtime_boundary"]
    run_readiness = deferred["run_readiness"]
    assert isinstance(formal_preflight, dict)
    assert isinstance(runtime_boundary, dict)
    assert isinstance(run_readiness, dict)
    assert formal_preflight["seed_support"] == {
        "status": "NOT_RUN_G1_1",
        "support_claimed": False,
        "required_test": (
            "A synthetic non-case canary must show that the pinned endpoint accepts and "
            "binds the seed field before any treatment response is generated."
        ),
        "on_rejection_or_nonbinding": "STOP_AND_REQUIRE_VERSIONED_AMENDMENT",
        "unseeded_substitution_allowed": False,
    }
    assert cast(dict[str, JsonValue], formal_preflight["serving_image"])["status"] == (
        "NOT_SEALED_G1_1"
    )
    assert (
        cast(dict[str, JsonValue], formal_preflight["backend_and_isolation"])["status"]
        == "NOT_RUN_G1_1"
    )
    assert (
        cast(dict[str, JsonValue], formal_preflight["provider_codec_and_response_normalization"])[
            "status"
        ]
        == "DOWNSTREAM_LOCK_OWNED_G1_4"
    )
    assert runtime_boundary["generated_action_execution_allowed"] is False
    assert runtime_boundary["collector_mutation_allowed"] is False
    assert runtime_boundary["live_endpoint_must_be_revalidated_before_run"] is True
    assert run_readiness["run_ready"] is False
    assert run_readiness["included_count"] == 0
    assert run_readiness["treatment_response_generation_allowed"] is False
    assert run_readiness["failure_mode"] == "FAIL_CLOSED_BEFORE_ANY_TREATMENT_RESPONSE"
    assert MODEL_CONFIG_PATH.as_posix() not in first_bytes.decode("utf-8")
    _assert_closed_no_execution_state(first)


def test_model_bindings_match_exact_qwen_and_mai_hashes_counts_and_bytes() -> None:
    models = _model_mapping(load_live_preparation(MODEL_CONFIG_PATH))

    assert tuple(models) == ("qwen3vl_8b", "mai_ui_8b")
    for model_id, expected in MODEL_EXPECTATIONS.items():
        for key in (
            "model_repository",
            "model_revision",
            "served_model_name",
            "model_config_record_sha256",
        ):
            expected_value = expected[key]
            assert models[model_id][key] == expected_value, (model_id, key)
        formal_request = models[model_id]["formal_request"]
        formal_launch = models[model_id]["formal_serving_launch"]
        inventory = models[model_id]["checkpoint_inventory"]
        assert isinstance(inventory, dict)
        assert (
            hashlib.sha256(canonical_json_bytes(formal_request)).hexdigest()
            == expected["formal_request_sha256"]
        )
        assert models[model_id]["formal_request_sha256"] == expected["formal_request_sha256"]
        assert (
            hashlib.sha256(canonical_json_bytes(formal_launch)).hexdigest()
            == expected["formal_serving_launch_sha256"]
        )
        assert (
            models[model_id]["formal_serving_launch_sha256"]
            == expected["formal_serving_launch_sha256"]
        )
        assert models[model_id]["parser_binding_sha256"] == expected["parser_binding_sha256"]
        assert models[model_id]["captured_request_sha256"] == expected["captured_request_sha256"]
        assert models[model_id]["actor_adapter_sha256"] == expected["actor_adapter_sha256"]
        assert models[model_id]["tokenizer_binding_sha256"] == expected["tokenizer_binding_sha256"]
        assert models[model_id]["tokenizer_artifact_count"] == expected["tokenizer_artifact_count"]
        assert (
            models[model_id]["tokenizer_artifact_byte_count"]
            == expected["tokenizer_artifact_byte_count"]
        )
        assert models[model_id]["runtime_tokenizer_observed"] is False
        assert inventory["inventory_sha256"] == expected["checkpoint_inventory_sha256"]
        for key in (
            "config_file_count",
            "config_byte_count",
            "weight_shard_count",
            "weight_byte_count",
            "total_file_count",
            "total_byte_count",
        ):
            assert inventory[key] == expected[key], (model_id, key)
        assert inventory["runtime_files_observed"] is False


def test_manifest_symlink_and_nonregular_paths_fail_closed(tmp_path: Path) -> None:
    manifest_link = tmp_path / "manifest-link.json"
    manifest_link.symlink_to(MODEL_CONFIG_PATH)
    _assert_error_code(
        "LIVE_PREPARATION_SOURCE_UNSAFE", lambda: load_live_preparation(manifest_link)
    )

    manifest_directory = tmp_path / "manifest-directory"
    manifest_directory.mkdir()
    _assert_error_code(
        "LIVE_PREPARATION_SOURCE_UNSAFE",
        lambda: load_live_preparation(manifest_directory),
    )


def test_manifest_intermediate_parent_symlink_is_never_followed(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_manifest = real_parent / "model_config_manifest.v1.json"
    real_manifest.write_bytes(MODEL_CONFIG_PATH.read_bytes())
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    _assert_error_code(
        "LIVE_PREPARATION_SOURCE_UNSAFE",
        lambda: load_live_preparation(linked_parent / real_manifest.name),
    )


def test_manifest_hash_drift_fails_before_static_binding(tmp_path: Path) -> None:
    drifted = tmp_path / "model-config-drift.json"
    frozen = MODEL_CONFIG_PATH.read_bytes()
    drifted_bytes = frozen.replace(b'"run_ready": false', b'"run_ready": true ', 1)
    assert len(drifted_bytes) == len(frozen)
    drifted.write_bytes(drifted_bytes)

    _assert_error_code(
        "LIVE_PREPARATION_MANIFEST_HASH_MISMATCH",
        lambda: load_live_preparation(drifted),
    )


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [
        ("run-ready", ("run_ready",), True),
        (
            "treatment-generation-guard",
            ("treatment_response_generation_allowed",),
            True,
        ),
        ("readiness-run-ready", ("run_readiness", "run_ready"), True),
        (
            "readiness-treatment-generation-guard",
            ("run_readiness", "treatment_response_generation_allowed"),
            True,
        ),
        ("readiness-included-count", ("run_readiness", "included_count"), 1),
        (
            "seed-support-claim",
            ("formal_preflight", "seed_support", "support_claimed"),
            True,
        ),
        (
            "generated-action-guard",
            ("runtime_boundary", "generated_action_execution_allowed"),
            True,
        ),
        (
            "collector-mutation-guard",
            ("runtime_boundary", "collector_mutation_allowed"),
            True,
        ),
        (
            "endpoint-revalidation-guard",
            ("runtime_boundary", "live_endpoint_must_be_revalidated_before_run"),
            False,
        ),
        ("model-id", ("models", 0, "model_id"), "mai_ui_8b"),
        ("model-repository", ("models", 0, "model_repository"), "wrong/repository"),
        ("model-revision", ("models", 0, "model_revision"), "0" * 40),
        ("served-name", ("models", 0, "served_model_name"), "wrong-model"),
        ("request-max-retries", ("models", 0, "formal_replay_request", "sdk_max_retries"), 1),
        ("request-timeout", ("models", 0, "formal_replay_request", "timeout_seconds"), 121.0),
        ("request-stream", ("models", 0, "formal_replay_request", "stream"), True),
        (
            "request-seed-rule",
            ("models", 0, "formal_replay_request", "seed_required"),
            False,
        ),
        ("launch-engine", ("models", 0, "formal_serving_launch", "engine"), "other"),
        (
            "launch-version",
            ("models", 0, "formal_serving_launch", "engine_version"),
            "0.11.1",
        ),
        ("launch-dtype", ("models", 0, "formal_serving_launch", "dtype"), "float16"),
        (
            "launch-context",
            ("models", 0, "formal_serving_launch", "max_model_len"),
            32_767,
        ),
        (
            "launch-eager",
            ("models", 0, "formal_serving_launch", "enforce_eager"),
            False,
        ),
        (
            "launch-memory-fraction",
            ("models", 0, "formal_serving_launch", "gpu_memory_utilization"),
            0.25,
        ),
        (
            "launch-tensor-parallel",
            ("models", 0, "formal_serving_launch", "tensor_parallel_size"),
            2,
        ),
        (
            "launch-pipeline-parallel",
            ("models", 0, "formal_serving_launch", "pipeline_parallel_size"),
            2,
        ),
        (
            "launch-data-parallel",
            ("models", 0, "formal_serving_launch", "data_parallel_size"),
            2,
        ),
        (
            "launch-prefix-cache",
            ("models", 0, "formal_serving_launch", "enable_prefix_caching"),
            True,
        ),
    ],
)
def test_manifest_safety_identity_request_and_launch_mutations_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    path: tuple[str | int, ...],
    value: JsonValue,
) -> None:
    payload = _load_manifest_json()
    _set_nested(payload, path, value)
    candidate = _write_json(tmp_path, payload, f"{name}.json")

    _assert_error_code(
        "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
        lambda: _load_with_test_rebound_integrity(candidate, monkeypatch),
    )


@pytest.mark.parametrize(
    "splice_key",
    [
        "formal_replay_request",
        "checkpoint_artifacts",
        "parser_implementation",
        "captured_application_request",
        "actor_adapter",
        "tokenizer",
    ],
)
def test_qwen_mai_cross_splicing_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, splice_key: str
) -> None:
    payload = _load_manifest_json()
    models = payload["models"]
    assert isinstance(models, list)
    assert isinstance(models[0], dict)
    assert isinstance(models[1], dict)
    models[0][splice_key] = copy.deepcopy(models[1][splice_key])
    candidate = _write_json(tmp_path, payload, f"cross-splice-{splice_key}.json")

    _assert_error_code(
        "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
        lambda: _load_with_test_rebound_integrity(candidate, monkeypatch),
    )


@pytest.mark.parametrize("model_id", ["qwen3vl_8b", "mai_ui_8b"])
def test_vllm_argv_is_exact_and_does_not_access_or_create_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model_id: str
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    snapshot_path = _snapshot_path(tmp_path, model_id)
    expected_model_name = cast(str, MODEL_EXPECTATIONS[model_id]["served_model_name"])
    assert snapshot_path.is_absolute()
    assert not snapshot_path.exists()

    filesystem_calls: list[str] = []

    def filesystem_bomb(*_args: object, **_kwargs: object) -> None:
        filesystem_calls.append("snapshot-filesystem-access")
        raise AssertionError("launch rendering must not access or create the snapshot")

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "open", filesystem_bomb)
        patcher.setattr(Path, "read_bytes", filesystem_bomb)
        patcher.setattr(Path, "exists", filesystem_bomb)
        patcher.setattr(Path, "is_file", filesystem_bomb)
        patcher.setattr(Path, "mkdir", filesystem_bomb)
        patcher.setattr(os, "open", filesystem_bomb)
        patcher.setattr(os, "stat", filesystem_bomb)
        patcher.setattr(os, "lstat", filesystem_bomb)
        patcher.setattr(os, "mkdir", filesystem_bomb)
        patcher.setattr(os, "makedirs", filesystem_bomb)
        argv = render_vllm_launch_argv(receipt, model_id, snapshot_path)
        launch_plan = prepare_vllm_launch_plan(receipt, model_id, snapshot_path)

    assert argv == (
        "vllm",
        "serve",
        os.fspath(snapshot_path),
        "--served-model-name",
        expected_model_name,
        "--host",
        "127.0.0.1",
        "--port",
        "18007",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "32768",
        "--enforce-eager",
        "--gpu-memory-utilization",
        "0.24",
        "--swap-space",
        "0",
        "--limit-mm-per-prompt",
        '{"image":3,"video":0}',
        "--mm-processor-cache-gb",
        "1",
        "--max-num-batched-tokens",
        "8192",
        "--max-num-seqs",
        "1",
        "--tensor-parallel-size",
        "1",
        "--pipeline-parallel-size",
        "1",
        "--data-parallel-size",
        "1",
        "--seed",
        "0",
        "--no-enable-prefix-caching",
        "--tokenizer-mode",
        "auto",
        "--no-trust-remote-code",
        "--load-format",
        "auto",
        "--kv-cache-dtype",
        "auto",
        "--generation-config",
        "auto",
    )
    launch_payload = _to_dict(launch_plan)
    assert launch_payload["live_preparation_receipt_sha256"] == (
        "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
    )
    assert launch_payload["model_config_manifest_sha256"] == MODEL_CONFIG_SHA256
    assert (
        launch_payload["model_config_record_sha256"]
        == MODEL_EXPECTATIONS[model_id]["model_config_record_sha256"]
    )
    assert (
        launch_payload["checkpoint_inventory_sha256"]
        == MODEL_EXPECTATIONS[model_id]["checkpoint_inventory_sha256"]
    )
    assert launch_payload["argv"] == list(argv)
    assert (
        launch_payload["argv_canonical_sha256"]
        == hashlib.sha256(canonical_json_bytes(list(argv))).hexdigest()
    )
    assert launch_payload["argv_canonical_byte_count"] == len(canonical_json_bytes(list(argv)))
    assert launch_payload["generation_config_mapping"] == {
        "manifest_mode": "model",
        "vllm_version": "0.11.0",
        "cli_value": "auto",
        "semantic": "LOAD_MODEL_GENERATION_CONFIG",
    }
    assert launch_payload["snapshot_binding"] == {
        "model_repository": MODEL_EXPECTATIONS[model_id]["model_repository"],
        "model_revision": MODEL_EXPECTATIONS[model_id]["model_revision"],
        "lexical_snapshot_path": os.fspath(snapshot_path),
        "runtime_snapshot_observed": False,
        "model_weights_loaded": False,
    }
    assert launch_payload["environment_variable_names"] == []
    assert launch_payload["serving_image_digest"] is None
    assert launch_payload["serving_image_digest_status"] == "PENDING_G1_7_SEAL"
    assert launch_payload["launch_compatibility_validated"] is False
    assert launch_payload["endpoint_health_validated"] is False
    assert launch_payload["isolation_validated"] is False
    assert launch_payload["port_reserved"] is False
    _assert_closed_no_execution_state(launch_plan)
    assert filesystem_calls == []
    assert not snapshot_path.exists()


def test_launch_plan_public_construction_paths_reject_rehashed_argv_forgery(
    tmp_path: Path,
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    launch_plan = prepare_vllm_launch_plan(
        receipt,
        "qwen3vl_8b",
        _snapshot_path(tmp_path, "qwen3vl_8b"),
    )
    forged_argv_values = list(launch_plan.argv)
    assert forged_argv_values[-2:] == ["--generation-config", "auto"]
    forged_argv_values[-1] = "model"
    forged_argv = tuple(forged_argv_values)
    forged_argv_bytes = canonical_json_bytes(forged_argv_values)

    _assert_public_record_forgery_rejected(
        launch_plan,
        argv=forged_argv,
        argv_sha256=hashlib.sha256(forged_argv_bytes).hexdigest(),
    )
    _assert_public_record_forgery_rejected(
        launch_plan,
        argv_sha256="0" * 64,
    )
    assert not _snapshot_path(tmp_path, "qwen3vl_8b").exists()


def test_receipt_public_construction_paths_reject_forgery() -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    reversed_models = tuple(reversed(receipt.models))

    _assert_public_record_forgery_rejected(
        receipt,
        "LIVE_PREPARATION_RECEIPT_INVALID",
        models=reversed_models,
    )
    _assert_public_record_forgery_rejected(
        receipt,
        "LIVE_PREPARATION_RECEIPT_INVALID",
        serving_environment_canonical_bytes=bytearray(receipt.serving_environment_canonical_bytes),
    )


def test_all_receipt_consumers_reject_non_receipt_objects(tmp_path: Path) -> None:
    forged = cast(Any, object())

    _assert_error_code(
        "LIVE_PREPARATION_RECEIPT_INVALID",
        lambda: render_vllm_launch_argv(
            forged,
            "qwen3vl_8b",
            _snapshot_path(tmp_path, "qwen3vl_8b"),
        ),
    )
    _assert_error_code(
        "LIVE_PREPARATION_RECEIPT_INVALID",
        lambda: prepare_vllm_launch_plan(
            forged,
            "qwen3vl_8b",
            _snapshot_path(tmp_path, "qwen3vl_8b"),
        ),
    )
    _assert_error_code(
        "LIVE_PREPARATION_RECEIPT_INVALID",
        lambda: prepare_openai_chat_call(
            forged,
            "qwen3vl_8b",
            _application_request("qwen3vl_8b"),
            1729,
        ),
    )
    _assert_error_code(
        "LIVE_PREPARATION_RECEIPT_INVALID",
        lambda: prepare_openai_chat_block(
            forged,
            "qwen3vl_8b",
            (_application_request("qwen3vl_8b"),),
            1729,
        ),
    )
    _assert_error_code(
        "LIVE_PREPARATION_RECEIPT_INVALID",
        lambda: assess_injected_gpu_inventory(
            forged,
            "qwen3vl_8b",
            {
                "accelerator": "NVIDIA H200",
                "total_memory_bytes": 1,
                "free_memory_bytes": 1,
            },
        ),
    )


@pytest.mark.parametrize("seed", REPLAY_SEEDS)
@pytest.mark.parametrize("model_id", ["qwen3vl_8b", "mai_ui_8b"])
def test_openai_call_descriptor_preserves_messages_tools_images_and_unknown_nested_fields(
    model_id: str, seed: int
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    arguments = _application_request(model_id)
    before = copy.deepcopy(arguments)

    descriptor = prepare_openai_chat_call(receipt, model_id, arguments, seed)
    payload = _to_dict(descriptor)

    assert arguments == before
    assert payload["model_id"] == model_id
    assert payload["live_preparation_receipt_sha256"] == (
        "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
    )
    assert payload["model_config_manifest_sha256"] == MODEL_CONFIG_SHA256
    assert (
        payload["model_config_record_sha256"]
        == MODEL_EXPECTATIONS[model_id]["model_config_record_sha256"]
    )
    assert payload["formal_request_sha256"] == MODEL_EXPECTATIONS[model_id]["formal_request_sha256"]
    assert payload["endpoint"] == {
        "origin": "http://127.0.0.1:18007",
        "path": "/v1/chat/completions",
    }
    assert payload["sdk"] == {
        "method": "openai.chat.completions.create",
        "version": "1.106.1",
        "timeout_seconds": 120.0,
        "max_retries": 0,
        "stream": False,
    }
    expected_kwargs = copy.deepcopy(arguments)
    expected_kwargs["seed"] = seed
    assert (
        payload["application_request_sha256"]
        == hashlib.sha256(canonical_json_bytes(cast(JsonValue, arguments))).hexdigest()
    )
    assert payload["application_request_byte_count"] == len(
        canonical_json_bytes(cast(JsonValue, arguments))
    )
    assert payload["application_delta"] == {
        "json_pointer": "/seed",
        "value": seed,
        "only_application_delta": True,
    }
    assert payload["source_invariance_validated"] is False
    assert payload["kwargs"] == expected_kwargs
    assert set(cast(dict[str, JsonValue], payload["kwargs"])) - set(arguments) == {"seed"}
    assert (
        payload["kwargs_canonical_sha256"]
        == hashlib.sha256(canonical_json_bytes(cast(JsonValue, expected_kwargs))).hexdigest()
    )
    assert payload["kwargs_canonical_byte_count"] == len(
        canonical_json_bytes(cast(JsonValue, expected_kwargs))
    )
    _assert_closed_no_execution_state(descriptor)


def test_call_descriptor_public_construction_paths_reject_coherent_kwargs_forgery() -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    descriptor = prepare_openai_chat_call(
        receipt,
        "qwen3vl_8b",
        _application_request("qwen3vl_8b"),
        1729,
    )
    forged_kwargs = descriptor.kwargs
    forged_kwargs["seed"] = 2718
    forged_kwargs_bytes = canonical_json_bytes(forged_kwargs)

    _assert_public_record_forgery_rejected(
        descriptor,
        kwargs_canonical_bytes=forged_kwargs_bytes,
        kwargs_sha256=hashlib.sha256(forged_kwargs_bytes).hexdigest(),
    )
    assert descriptor.kwargs["seed"] == 1729


@pytest.mark.parametrize("seed", REPLAY_SEEDS)
def test_openai_call_block_derives_every_call_under_one_shared_seed(seed: int) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    first = _application_request("qwen3vl_8b")
    second = copy.deepcopy(first)
    messages = second["messages"]
    assert isinstance(messages, list)
    messages.append({"role": "assistant", "content": "Earlier inert context."})
    requests = (first, second)
    before = copy.deepcopy(requests)

    block = prepare_openai_chat_block(receipt, "qwen3vl_8b", requests, seed)
    payload = _to_dict(block)

    assert requests == before
    assert payload["model_id"] == "qwen3vl_8b"
    assert payload["live_preparation_receipt_sha256"] == (
        "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
    )
    assert payload["replay_seed"] == seed
    assert payload["call_count"] == 2
    assert payload["paired_seed_consistency_validated"] is True
    assert payload["formal_plan_pairing_validated"] is False
    calls = payload["calls"]
    assert isinstance(calls, list)
    for source_request, call in zip(requests, calls, strict=True):
        assert isinstance(call, dict)
        kwargs = call["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["seed"] == seed
        without_seed = copy.deepcopy(kwargs)
        del without_seed["seed"]
        assert without_seed == source_request
        assert (
            call["application_request_sha256"]
            == hashlib.sha256(canonical_json_bytes(cast(JsonValue, source_request))).hexdigest()
        )
        assert cast(dict[str, JsonValue], call["application_delta"])["value"] == seed
    assert (
        payload["call_set_sha256"]
        == hashlib.sha256(canonical_json_bytes(cast(JsonValue, calls))).hexdigest()
    )
    _assert_closed_no_execution_state(block)


def test_call_block_public_construction_paths_reject_mixed_seed_with_rehashed_call_set() -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    request = _application_request("qwen3vl_8b")
    valid_block = prepare_openai_chat_block(receipt, "qwen3vl_8b", (request,), 1729)
    mixed_calls = (
        prepare_openai_chat_call(receipt, "qwen3vl_8b", request, 1729),
        prepare_openai_chat_call(receipt, "qwen3vl_8b", request, 2718),
    )
    mixed_call_set_bytes = canonical_json_bytes([call.to_dict() for call in mixed_calls])

    _assert_public_record_forgery_rejected(
        valid_block,
        calls=mixed_calls,
        call_set_sha256=hashlib.sha256(mixed_call_set_bytes).hexdigest(),
    )
    _assert_public_record_forgery_rejected(
        valid_block,
        call_set_sha256="0" * 64,
    )


@pytest.mark.parametrize("bad_requests", [(), [], {}, None])
def test_openai_call_block_rejects_empty_or_non_tuple_request_sets(
    bad_requests: object,
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    _assert_error_code(
        "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        lambda: prepare_openai_chat_block(
            receipt,
            "qwen3vl_8b",
            cast(tuple[dict[str, JsonValue], ...], bad_requests),
            1729,
        ),
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (
            lambda value: value.update({"timeout": 120.0}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"api_key": "forbidden"}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"headers": {"x": "forbidden"}}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"stream": True}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"model": "wrong-model"}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"temperature": 0.1}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.update({"seed": 1729}),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
        (
            lambda value: value.pop("messages"),
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        ),
    ],
)
def test_openai_call_descriptor_rejects_transport_credentials_drift_and_seed_present_input(
    mutation: Callable[[dict[str, JsonValue]], object], expected_code: str
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    arguments = _application_request("qwen3vl_8b")
    mutation(arguments)

    _assert_error_code(
        expected_code,
        lambda: prepare_openai_chat_call(receipt, "qwen3vl_8b", arguments, 1729),
    )


@pytest.mark.parametrize("bad_seed", [0, True, 1729.0, None, "1729"])
def test_openai_call_descriptor_rejects_bad_replay_seed_type_and_value(
    bad_seed: object,
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    arguments = _application_request("qwen3vl_8b")

    _assert_error_code(
        "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        lambda: prepare_openai_chat_call(
            receipt,
            "qwen3vl_8b",
            arguments,
            cast(int, bad_seed),
        ),
    )


@pytest.mark.parametrize("bad_arguments", [None, [], "request", 1, True])
def test_openai_call_descriptor_rejects_non_object_arguments(bad_arguments: object) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    _assert_error_code(
        "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
        lambda: prepare_openai_chat_call(
            receipt,
            "qwen3vl_8b",
            cast(dict[str, JsonValue], bad_arguments),
            1729,
        ),
    )


def test_openai_response_envelope_projects_content_finish_reason_usage_and_hashes() -> None:
    envelope = _valid_response_envelope()
    before = copy.deepcopy(envelope)

    projection = decode_openai_chat_envelope(envelope)
    payload = _to_dict(projection)

    assert envelope == before
    assert payload["content"] == '<tool_call>{"action_type":"wait"}</tool_call>'
    assert payload["host_parser_input"] == '<tool_call>{"action_type":"wait"}</tool_call>'
    assert (
        payload["host_parser_input_normalization"]
        == "PYTHON_STRIP_MATCHES_MOBILE_WORLD_BASE_AGENT_V1"
    )
    assert payload["finish_reason"] == "stop"
    assert payload["usage"] == {
        "prompt_tokens": 101,
        "completion_tokens": 7,
        "total_tokens": 108,
    }
    assert (
        payload["envelope_sha256"]
        == hashlib.sha256(canonical_json_bytes(cast(JsonValue, envelope))).hexdigest()
    )
    assert isinstance(payload["projection_sha256"], str)
    assert payload["status"] == "CODE_ONLY_NOT_OBSERVED"
    assert payload["response_source"] == "CALLER_INJECTED_NOT_PROVIDER_OBSERVED"
    _assert_closed_no_execution_state(projection)


def test_openai_response_envelope_accepts_explicitly_unavailable_usage() -> None:
    envelope = _valid_response_envelope()
    envelope.pop("usage")

    projection = decode_openai_chat_envelope(envelope)

    assert _to_dict(projection)["usage"] is None
    _assert_closed_no_execution_state(projection)


def test_openai_response_projection_preserves_exact_host_parser_input_whitespace() -> None:
    envelope = _valid_response_envelope()
    exact_content = ' \n<tool_call>{"action_type":"wait"}</tool_call>\t '
    choices = envelope["choices"]
    assert isinstance(choices, list)
    choice = choices[0]
    assert isinstance(choice, dict)
    message = choice["message"]
    assert isinstance(message, dict)
    message["content"] = exact_content

    projection = decode_openai_chat_envelope(envelope)
    payload = _to_dict(projection)

    assert projection.content == exact_content
    assert payload["content"] == exact_content
    assert projection.host_parser_input == exact_content.strip()
    assert payload["host_parser_input"] == exact_content.strip()
    assert (
        payload["host_parser_input_normalization"]
        == "PYTHON_STRIP_MATCHES_MOBILE_WORLD_BASE_AGENT_V1"
    )


def test_response_projection_public_construction_paths_reject_rehashed_projection_forgery() -> None:
    envelope = _valid_response_envelope()
    exact_content = ' \n<tool_call>{"action_type":"wait"}</tool_call>\t '
    choices = cast(list[JsonValue], envelope["choices"])
    choice = cast(dict[str, JsonValue], choices[0])
    message = cast(dict[str, JsonValue], choice["message"])
    message["content"] = exact_content
    projection = decode_openai_chat_envelope(envelope)
    forged_projection: dict[str, JsonValue] = {
        "content": projection.content,
        "host_parser_input": projection.content,
        "finish_reason": projection.finish_reason,
        "usage": cast(JsonValue, projection.usage),
    }
    forged_projection_bytes = canonical_json_bytes(forged_projection)

    _assert_public_record_forgery_rejected(
        projection,
        host_parser_input=projection.content,
        projection_sha256=hashlib.sha256(forged_projection_bytes).hexdigest(),
    )
    assert projection.usage_canonical_bytes is not None
    _assert_public_record_forgery_rejected(
        projection,
        usage_canonical_bytes=bytearray(projection.usage_canonical_bytes),
    )
    assert projection.host_parser_input == exact_content.strip()


@pytest.mark.parametrize(
    "bad_envelope",
    [
        None,
        [],
        {},
        {"choices": []},
        {"choices": "not-a-list"},
        {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}, {}]},
        {"choices": [{"message": {}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": 1}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "ok"}, "finish_reason": 1}]},
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": -1, "completion_tokens": 0, "total_tokens": 0},
        },
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 0},
        },
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": "0", "total_tokens": 1},
        },
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 1},
        },
    ],
)
def test_openai_response_envelope_rejects_malformed_multiple_choice_and_bad_usage(
    bad_envelope: object,
) -> None:
    _assert_error_code(
        "LIVE_PREPARATION_RESPONSE_INVALID",
        lambda: decode_openai_chat_envelope(cast(dict[str, JsonValue], bad_envelope)),
    )


@pytest.mark.parametrize(
    ("free_memory_bytes", "capacity_sufficient"),
    [(34_505_223_046, True), (34_505_223_045, False)],
)
def test_injected_h200_capacity_is_advisory_and_never_authorizes_execution(
    free_memory_bytes: int, capacity_sufficient: bool
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    total_memory_bytes = 143_771_762_688
    inventory: dict[str, JsonValue] = {
        "accelerator": "NVIDIA H200",
        "total_memory_bytes": total_memory_bytes,
        "free_memory_bytes": free_memory_bytes,
    }

    assessment = assess_injected_gpu_inventory(receipt, "qwen3vl_8b", inventory)
    payload = _to_dict(assessment)

    assert payload["live_preparation_receipt_sha256"] == (
        "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
    )
    assert payload["resource_snapshot_injected"] is True
    assert payload["required_free_memory_bytes"] == 34_505_223_046
    assert payload["capacity_sufficient"] is capacity_sufficient
    assert payload["inventory"] == inventory
    _assert_closed_no_execution_state(assessment)


def test_gpu_assessment_public_construction_paths_reject_derived_field_forgery() -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    assessment = assess_injected_gpu_inventory(
        receipt,
        "qwen3vl_8b",
        {
            "accelerator": "NVIDIA H200",
            "total_memory_bytes": 143_771_762_688,
            "free_memory_bytes": 40_000_000_000,
        },
    )
    assert assessment.capacity_sufficient is True

    _assert_public_record_forgery_rejected(
        assessment,
        capacity_sufficient=False,
    )
    _assert_public_record_forgery_rejected(
        assessment,
        inventory_sha256="0" * 64,
    )
    assert assessment.capacity_sufficient is True


@pytest.mark.parametrize(
    "inventory",
    [
        {},
        {"accelerator": "NVIDIA A100", "total_memory_bytes": 1, "free_memory_bytes": 1},
        {"accelerator": 1, "total_memory_bytes": 1, "free_memory_bytes": 1},
        {"accelerator": "NVIDIA H200", "total_memory_bytes": True, "free_memory_bytes": 1},
        {"accelerator": "NVIDIA H200", "total_memory_bytes": 1, "free_memory_bytes": False},
        {"accelerator": "NVIDIA H200", "total_memory_bytes": 0, "free_memory_bytes": 0},
        {"accelerator": "NVIDIA H200", "total_memory_bytes": 1, "free_memory_bytes": -1},
        {"accelerator": "NVIDIA H200", "total_memory_bytes": 1, "free_memory_bytes": 2},
        {
            "accelerator": "NVIDIA H200",
            "total_memory_bytes": 1,
            "free_memory_bytes": 1,
            "gpu_index": 0,
        },
    ],
)
def test_injected_gpu_inventory_rejects_bad_types_family_ranges_and_extra_fields(
    inventory: dict[str, JsonValue],
) -> None:
    receipt = load_live_preparation(MODEL_CONFIG_PATH)
    _assert_error_code(
        "LIVE_PREPARATION_GPU_INVENTORY_INVALID",
        lambda: assess_injected_gpu_inventory(receipt, "qwen3vl_8b", inventory),
    )


def test_live_preparation_schemas_have_exact_hashes_and_meta_validate() -> None:
    assert set(LIVE_SCHEMA_SHA256) == {
        "live_preparation.schema.json",
        "openai_chat_call_plan.schema.json",
        "openai_chat_call_block.schema.json",
        "vllm_launch_plan.schema.json",
        "openai_chat_response_projection.schema.json",
        "injected_gpu_capacity_assessment.schema.json",
    }
    for name, expected_sha256 in LIVE_SCHEMA_SHA256.items():
        path = G14_SCHEMA_ROOT / name
        frozen = path.read_bytes()
        assert hashlib.sha256(frozen).hexdigest() == expected_sha256
        schema = json.loads(frozen)
        assert isinstance(schema, dict)
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_every_live_preparation_record_matches_its_versioned_schema(tmp_path: Path) -> None:
    payloads = _live_schema_payloads(tmp_path)
    assert set(payloads) == set(LIVE_SCHEMA_SHA256)

    for name, payload in payloads.items():
        _live_schema_validator(name).validate(payload)
        for key in ALL_FALSE_FIELDS:
            observed = tuple(_all_key_values(payload, key))
            assert observed
            assert all(item is False for item in observed)


def test_all_live_schemas_reject_open_or_non_false_readiness_and_safety(
    tmp_path: Path,
) -> None:
    payloads = _live_schema_payloads(tmp_path)
    state_fields = {
        "readiness": READINESS_FALSE_FIELDS,
        "safety": SAFETY_FALSE_FIELDS,
    }

    for name, payload in payloads.items():
        validator = _live_schema_validator(name)
        for state_name, required_fields in state_fields.items():
            state = payload[state_name]
            assert isinstance(state, dict)
            assert set(state) == required_fields
            for field_name in required_fields:
                for mutation_name, mutation_value in (
                    ("true", True),
                    ("non-boolean", "false"),
                ):
                    candidate = copy.deepcopy(payload)
                    candidate_state = candidate[state_name]
                    assert isinstance(candidate_state, dict)
                    candidate_state[field_name] = mutation_value
                    assert not validator.is_valid(candidate), (
                        name,
                        state_name,
                        field_name,
                        mutation_name,
                    )
                missing = copy.deepcopy(payload)
                missing_state = missing[state_name]
                assert isinstance(missing_state, dict)
                del missing_state[field_name]
                assert not validator.is_valid(missing), (
                    name,
                    state_name,
                    field_name,
                    "missing",
                )

            opened_state = copy.deepcopy(payload)
            opened = opened_state[state_name]
            assert isinstance(opened, dict)
            opened["unexpected_authorization"] = False
            assert not validator.is_valid(opened_state), (name, state_name, "extra")

        missing_record_type = copy.deepcopy(payload)
        del missing_record_type["record_type"]
        assert not validator.is_valid(missing_record_type), (name, "missing record_type")
        open_record = copy.deepcopy(payload)
        open_record["unexpected"] = False
        assert not validator.is_valid(open_record), (name, "extra property")


def test_live_schemas_reject_model_seed_mapping_parser_and_gpu_gate_drift(
    tmp_path: Path,
) -> None:
    payloads = _live_schema_payloads(tmp_path)

    receipt = copy.deepcopy(payloads["live_preparation.schema.json"])
    models = receipt["models"]
    assert isinstance(models, list)
    models.reverse()
    assert not _live_schema_validator("live_preparation.schema.json").is_valid(receipt)

    call = copy.deepcopy(payloads["openai_chat_call_plan.schema.json"])
    application_delta = call["application_delta"]
    assert isinstance(application_delta, dict)
    application_delta["value"] = 2718
    assert not _live_schema_validator("openai_chat_call_plan.schema.json").is_valid(call)

    block = copy.deepcopy(payloads["openai_chat_call_block.schema.json"])
    calls = block["calls"]
    assert isinstance(calls, list)
    nested_call = calls[0]
    assert isinstance(nested_call, dict)
    nested_delta = nested_call["application_delta"]
    nested_kwargs = nested_call["kwargs"]
    assert isinstance(nested_delta, dict)
    assert isinstance(nested_kwargs, dict)
    nested_delta["value"] = 2718
    nested_kwargs["seed"] = 2718
    assert not _live_schema_validator("openai_chat_call_block.schema.json").is_valid(block)

    launch = copy.deepcopy(payloads["vllm_launch_plan.schema.json"])
    generation_mapping = launch["generation_config_mapping"]
    assert isinstance(generation_mapping, dict)
    generation_mapping["cli_value"] = "model"
    assert not _live_schema_validator("vllm_launch_plan.schema.json").is_valid(launch)

    response = copy.deepcopy(payloads["openai_chat_response_projection.schema.json"])
    response["host_parser_input_normalization"] = "RAW_CONTENT"
    assert not _live_schema_validator("openai_chat_response_projection.schema.json").is_valid(
        response
    )

    gpu = copy.deepcopy(payloads["injected_gpu_capacity_assessment.schema.json"])
    gpu["run_ready"] = True
    assert not _live_schema_validator("injected_gpu_capacity_assessment.schema.json").is_valid(gpu)


def test_every_live_preparation_api_has_zero_forbidden_capability_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counters: dict[str, int] = {}

    def bomb(name: str) -> Callable[..., Any]:
        counters[name] = 0

        def fail(*_args: object, **_kwargs: object) -> Any:
            counters[name] += 1
            raise AssertionError(f"forbidden capability reached: {name}")

        return fail

    original_import = builtins.__import__
    original_import_module = importlib.import_module

    def guarded_import(
        name: str,
        globals_: Mapping[str, object] | None = None,
        locals_: Mapping[str, object] | None = None,
        fromlist: Sequence[str] = (),
        level: int = 0,
    ) -> Any:
        root = name.split(".", maxsplit=1)[0]
        if root in {"openai", "torch", "vllm"}:
            counters[f"import:{root}"] += 1
            raise AssertionError(f"forbidden runtime import reached: {root}")
        return original_import(name, globals_, locals_, fromlist, level)

    def guarded_import_module(name: str, package: str | None = None) -> Any:
        root = name.split(".", maxsplit=1)[0]
        if root in {"openai", "torch", "vllm"}:
            counters[f"import:{root}"] += 1
            raise AssertionError(f"forbidden runtime import reached: {root}")
        return original_import_module(name, package)

    for module_name in ("openai", "torch", "vllm"):
        counters[f"import:{module_name}"] = 0

    socket_targets = (
        "socket",
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    )
    subprocess_targets = (
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
    )
    os_targets = (
        "system",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
    )

    with monkeypatch.context() as patcher:
        for name in socket_targets:
            patcher.setattr(socket, name, bomb(f"socket.{name}"))
        for name in subprocess_targets:
            patcher.setattr(subprocess, name, bomb(f"subprocess.{name}"))
        for name in os_targets:
            if hasattr(os, name):
                patcher.setattr(os, name, bomb(f"os.{name}"))
        patcher.setattr(builtins, "__import__", guarded_import)
        patcher.setattr(importlib, "import_module", guarded_import_module)

        receipt = load_live_preparation(MODEL_CONFIG_PATH)
        snapshot = _snapshot_path(tmp_path, "qwen3vl_8b")
        render_vllm_launch_argv(receipt, "qwen3vl_8b", snapshot)
        prepare_vllm_launch_plan(receipt, "qwen3vl_8b", snapshot)
        prepare_openai_chat_call(
            receipt,
            "qwen3vl_8b",
            _application_request("qwen3vl_8b"),
            1729,
        )
        prepare_openai_chat_block(
            receipt,
            "qwen3vl_8b",
            (_application_request("qwen3vl_8b"),),
            1729,
        )
        decode_openai_chat_envelope(_valid_response_envelope())
        assess_injected_gpu_inventory(
            receipt,
            "qwen3vl_8b",
            {
                "accelerator": "NVIDIA H200",
                "total_memory_bytes": 143_771_762_688,
                "free_memory_bytes": 40_000_000_000,
            },
        )
        assert runner_cli_main(["live-status"]) == 0

    assert counters
    assert set(counters.values()) == {0}


def test_existing_openai_send_and_live_arm_execution_remain_mechanical_hard_stops() -> None:
    codec = OpenAICompatibleProviderCodec(
        codec_id="mobileworld.g1.provider.openai-compatible-preparation-test/v1",
        endpoint_revision="http://127.0.0.1:18007/v1/chat/completions",
        parser=JsonActionParser(),
    )

    _assert_error_code("LIVE_TRANSPORT_DEFERRED", lambda: codec.send(cast(Any, object())))
    assert codec.send_calls == 1
    _assert_error_code("LIVE_EXECUTION_DEFERRED", lambda: execute_live_arm(cast(Any, object())))


def test_live_status_cli_is_a_complete_closed_fail_only_aggregate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert runner_cli_main(["live-status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert isinstance(payload, dict)
    assert set(payload) == {
        "status",
        "live_code_prepared",
        "static_model_configuration_validated",
        *ALL_FALSE_FIELDS,
    }
    assert payload["status"] == "DEFERRED_PENDING_OWNER_GPU_RESOURCE_REVIEW"
    assert payload["live_code_prepared"] is True
    assert payload["static_model_configuration_validated"] is False
    for key in ALL_FALSE_FIELDS:
        assert payload[key] is False
