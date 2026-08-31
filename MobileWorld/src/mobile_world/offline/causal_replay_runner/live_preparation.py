"""Inert, CPU-only preparation records for a future G1.4 live proof.

This module deliberately has no provider client, socket, process, GPU probe, model
loader, or replay entrypoint.  It only validates the frozen G1.1 model manifest and
renders immutable data that a later, separately authorized contract may inspect.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import cast

from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_json_bytes

from .contracts import REPLAY_SEEDS, ReplayRunnerError

LIVE_PREPARATION_CONTRACT_VERSION = (
    "mobileworld.g1.exact-request-replay-live-preparation/contract-v1"
)
LIVE_PREPARATION_RECEIPT_SCHEMA_VERSION = (
    "mobileworld.g1.exact-request-replay-live-preparation-receipt/v1"
)
OPENAI_CHAT_CALL_DESCRIPTOR_SCHEMA_VERSION = "mobileworld.g1.inert-openai-chat-call-descriptor/v1"
OPENAI_CHAT_BLOCK_DESCRIPTOR_SCHEMA_VERSION = "mobileworld.g1.inert-openai-chat-block-descriptor/v1"
OPENAI_CHAT_RESPONSE_PROJECTION_SCHEMA_VERSION = "mobileworld.g1.openai-chat-response-projection/v1"
INJECTED_GPU_ASSESSMENT_SCHEMA_VERSION = "mobileworld.g1.injected-gpu-inventory-assessment/v1"
VLLM_LAUNCH_PLAN_SCHEMA_VERSION = "mobileworld.g1.inert-vllm-launch-plan/v1"
MODEL_CONFIG_SCHEMA_VERSION = "mobileworld.g1.causal-replay/model-config-v1"
PROTOCOL_VERSION = "mobileworld.g1.causal-replay/protocol-v1"
MODEL_CONFIG_MANIFEST_RELATIVE_PATH = "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
MODEL_CONFIG_MANIFEST_SHA256 = "7ba840b1b7c7f4539ec9b967a5b4029c3a0e3217f6bb8bc1e9eb7d04687c6c5f"
MODEL_CONFIG_MANIFEST_BYTE_COUNT = 29_618
LIVE_PREPARATION_RECEIPT_SHA256 = "4f3cbd2d0369288c7507b77c54766bf536b620156196df770c428891280b9819"
ACTIVE_G1_3_MANIFEST_SHA256 = "8b9fcc73630a12f6eb4ddc16b82ddfa3fcd5c7eed91451905fa0e3ae87f0e402"
ACTIVE_G1_3_CAPSULE_SET_SHA256 = "7d0e85c523c2b20b3f0b820c2e846cbb84957d4ae78e46d7090c6ce78ae9fbed"
LIVE_PREPARATION_STATUS = "CODE_ONLY_NOT_OBSERVED"
LIVE_PREPARATION_BLOCKERS = (
    "G1_5",
    "G1_6",
    "G1_7",
    "OWNER_GPU_AUTH",
    "LIVE_ENDPOINT",
)

_FORBIDDEN_SDK_PLAN_KEYS = {
    "api_key",
    "authorization",
    "authorization_header",
    "base_url",
    "callback",
    "client",
    "client_factory",
    "cookie",
    "cookies",
    "headers",
    "http_client",
    "default_headers",
    "extra_headers",
    "max_retries",
    "request_options",
    "timeout",
    "transport",
}

_ROOT_KEYS = {
    "artifact_type",
    "schema_version",
    "protocol_id",
    "manifest_phase",
    "curated",
    "deployment_prediction",
    "run_ready",
    "treatment_response_generation_allowed",
    "scientific_scope",
    "captured_vs_formal_replay_contract",
    "formal_preflight",
    "repository",
    "analysis_environment",
    "runtime_boundary",
    "models",
    "formal_serving_environment",
    "source_corpora",
    "source_platform_pins",
    "unavailable_historical_exact_values",
    "run_readiness",
}
_FORMAL_REQUEST: dict[str, dict[str, JsonValue]] = {
    "qwen3vl_8b": {
        "endpoint_origin": "http://127.0.0.1:18007",
        "endpoint_path": "/v1/chat/completions",
        "sdk": "openai.chat.completions.create",
        "sdk_version": "1.106.1",
        "sdk_max_retries": 0,
        "timeout_seconds": 120.0,
        "stream": False,
        "arguments_except_messages_and_seed": {
            "model": "Qwen3-VL-8B-Instruct",
            "temperature": 0.0,
        },
        "seed_required": True,
    },
    "mai_ui_8b": {
        "endpoint_origin": "http://127.0.0.1:18007",
        "endpoint_path": "/v1/chat/completions",
        "sdk": "openai.chat.completions.create",
        "sdk_version": "1.106.1",
        "sdk_max_retries": 0,
        "timeout_seconds": 120.0,
        "stream": False,
        "arguments_except_messages_and_seed": {
            "model": "MAI-UI-8B",
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 2048,
        },
        "seed_required": True,
    },
}
_FORMAL_LAUNCH: dict[str, JsonValue] = {
    "engine": "vllm",
    "engine_version": "0.11.0",
    "host": "127.0.0.1",
    "port": 18007,
    "dtype": "bfloat16",
    "max_model_len": 32768,
    "enforce_eager": True,
    "gpu_memory_utilization": 0.24,
    "swap_space_gib": 0,
    "limit_mm_per_prompt": {"image": 3, "video": 0},
    "mm_processor_cache_gib": 1,
    "max_num_batched_tokens": 8192,
    "max_num_seqs": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "data_parallel_size": 1,
    "engine_seed": 0,
    "enable_prefix_caching": False,
    "tokenizer_mode": "auto",
    "trust_remote_code": False,
    "load_format": "auto",
    "kv_cache_dtype": "auto",
    "generation_config_mode": "model",
}
_VLLM_GENERATION_CONFIG_MAPPING: dict[str, JsonValue] = {
    "manifest_mode": "model",
    "vllm_version": "0.11.0",
    "cli_value": "auto",
    "semantic": "LOAD_MODEL_GENERATION_CONFIG",
}
_MODEL_IDENTITY = {
    "qwen3vl_8b": (
        "PRIMARY",
        "flat_progress",
        "Qwen/Qwen3-VL-8B-Instruct",
        "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
        "Qwen3-VL-8B-Instruct",
        "5d6c5c1aa99aa13e8e153a19e6a0b1e8593cf2c32adaf8bca308fc76cea827e3",
    ),
    "mai_ui_8b": (
        "REPLICATION",
        "raw_replay",
        "Tongyi-MAI/MAI-UI-8B",
        "e00a0097abb9cc621cac5172d8c4809f0839c94e",
        "MAI-UI-8B",
        "c633cc272bca6e14ae788d90417c909d515fe1f22abb9c0bf5e02da78c8d7682",
    ),
}
_PARSER_BINDING: dict[str, dict[str, JsonValue]] = {
    "qwen3vl_8b": {
        "parser_implementation": {
            "path": "MobileWorld/src/mobile_world/agents/implementations/qwen3vl.py",
            "sha256": "202a04443eaa1d2f4c776b73bc315e65617d28df882ee2fac305849a7f79ac82",
            "symbols": [
                "parse_tagged_text",
                "parse_action_to_structure_output",
                "parsing_response_to_andoid_world_env_action",
                "Qwen3VLAgentMCP.predict",
            ],
            "mapping": {
                "path": "MobileWorld/src/mobile_world/agents/utils/agent_mapping.py",
                "sha256": "5e3d26f2c765ced61441a15c01d79945034ab355135029e65a3202da0bfcaaa3",
                "symbol": "QWENVL2AW_ACTION_MAP",
            },
        },
        "normalized_action_schema": {
            "path": "MobileWorld/src/mobile_world/runtime/utils/models.py",
            "sha256": "749d8931c8e3112444239c3642a63cf86a5374e5beb01ffe3dca6ea24d85f5b0",
            "symbol": "JSONAction",
        },
    },
    "mai_ui_8b": {
        "parser_implementation": {
            "path": "MobileWorld/src/mobile_world/agents/implementations/mai_ui_agent.py",
            "sha256": "0c18f8a5362d8e93fc9798882d12945d2aef152e38fbe9645470bfb6c74f549f",
            "symbols": [
                "parse_tagged_text",
                "parse_action_to_structure_output",
                "MAIUINaivigationAgent._normalize_coord_to_pixel",
                "MAIUINaivigationAgent._convert_to_json_action",
                "MAIUINaivigationAgent.predict",
            ],
            "helper": {
                "path": "MobileWorld/src/mobile_world/agents/utils/helpers.py",
                "sha256": "dd573f36d48d6a886420e31e9972cf7edec4709fad557423b26f6d1964a03ff1",
                "symbol": "reverse_swipe_direction",
            },
        },
        "normalized_action_schema": {
            "path": "MobileWorld/src/mobile_world/runtime/utils/models.py",
            "sha256": "749d8931c8e3112444239c3642a63cf86a5374e5beb01ffe3dca6ea24d85f5b0",
            "symbol": "JSONAction",
        },
    },
}
_MODEL_PROJECTION_HASHES = {
    "qwen3vl_8b": {
        "captured_application_request": (
            "f14fd9b4f383ee75fa0f3c79cba6fbd92d52799dd32d975056df0140195ad9cb"
        ),
        "actor_adapter": "4f814da955563dd568d3cd9c30b9796775a15dfc1175fa4fc135a379221d84b4",
        "tokenizer": "e97afc56a6ce6b1d0d78345efc2b27c9853e9251d1e2f2bb0ff60b9b99926efd",
        "tokenizer_artifact_count": 5,
        "tokenizer_artifact_byte_count": 11_497_442,
    },
    "mai_ui_8b": {
        "captured_application_request": (
            "f9cb0d3d3ff77803be2cd6eb1d7aa93a73aee87b3c030a640861be60f53a2b0a"
        ),
        "actor_adapter": "6aa3e69cc6ec49c83bb25d871c28abcd3efdfcb5b51a8a9b3d478389f9e58868",
        "tokenizer": "dac3c7c7da1bcb043402cb3571a0867f98153c4fd3f3c0614153a6ea27518d23",
        "tokenizer_artifact_count": 7,
        "tokenizer_artifact_byte_count": 15_883_397,
    },
}
_CHECKPOINTS: dict[str, dict[str, tuple[tuple[str, int, str], ...]]] = {
    "qwen3vl_8b": {
        "config_files": (
            (
                "config.json",
                1474,
                "5cd452860dc1e9c29dd71cc3cef7f39b338b7a40793f7a260655c2d3568f3661",
            ),
            (
                "generation_config.json",
                269,
                "8469742d1fce0de951c8909b26a2c0c0d8490837ce476efb114da9e0cefc4d44",
            ),
            (
                "preprocessor_config.json",
                390,
                "27225450ac9c6529872ee1924fcb0962ff5634834f817040f444118116f4e516",
            ),
            (
                "video_preprocessor_config.json",
                385,
                "7768af27c1fafa9cc9011c1dc20067e03f8915e03b63504550e11d5066986d13",
            ),
            (
                "model.safetensors.index.json",
                67759,
                "520b2e05079402e9468a8701d03d1154d14b2599593afb6effa7fb60c1bff070",
            ),
        ),
        "weight_shards": (
            (
                "model-00001-of-00004.safetensors",
                4902275944,
                "d5d0aef0eb170fc7453a296c43c0849a56f510555d3588e4fd662bb35490aefa",
            ),
            (
                "model-00002-of-00004.safetensors",
                4915962496,
                "8be88fb5501e4d5719a6d4cc212e6a13480330e74f3e8c77daa1a68f199106b5",
            ),
            (
                "model-00003-of-00004.safetensors",
                4999831048,
                "83de00eafe6e0d57ccd009dbcf71c9974d74df2f016c27afb7e95aafd16b2192",
            ),
            (
                "model-00004-of-00004.safetensors",
                2716270024,
                "0a88b98e9f96270973f567e6a2c103ede6ccdf915ca3075e21c755604d0377a5",
            ),
        ),
    },
    "mai_ui_8b": {
        "config_files": (
            (
                "config.json",
                1646,
                "51a52c0467c29ce8b31fd221187d5b624442feee29e803a017db772076f8959a",
            ),
            (
                "generation_config.json",
                169,
                "94579b5c48626613a27a77b3f1563ce20be320ad474609aa03aa56a920345d7b",
            ),
            (
                "preprocessor_config.json",
                782,
                "93585062a80db5e8ca038efc7726a3e6411d9db948472d81d63c6303993be8c5",
            ),
            (
                "video_preprocessor_config.json",
                817,
                "59c5c9eb52182eb14c06ffb10ca9effd29adce5f238a95de23ca14a38dbd2cb1",
            ),
            (
                "model.safetensors.index.json",
                64772,
                "cf463951743b3cda1bc63bb5cebcbdd411c5ba5c06869bc42aeaa222a078a029",
            ),
        ),
        "weight_shards": (
            (
                "model-00001-of-00004.safetensors",
                4670770960,
                "4383794d5ed9e374813fcfac478c62bcef74271b0c0f7e314a44f26e46fab735",
            ),
            (
                "model-00002-of-00004.safetensors",
                4988364944,
                "ceff1dec51699c7c060b80e6285b8c8d6995b93542fafc1afc791d5325caa558",
            ),
            (
                "model-00003-of-00004.safetensors",
                4996843088,
                "ef1c00864f7591b22e1b13654ac2fd4a96d28fd47818315cc0016da9b4a2f365",
            ),
            (
                "model-00004-of-00004.safetensors",
                2878360392,
                "f3f1cf24fdf1fb17f79cd0e7d94aeaae547580a926fa79b35443633257abeb13",
            ),
        ),
    },
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(value: JsonValue) -> bytes:
    return canonical_json_bytes(value)


def _from_snapshot(data: bytes) -> JsonValue:
    return cast(JsonValue, json.loads(data))


def _fail(code: str, message: str, path: str) -> None:
    raise ReplayRunnerError(code, message, json_path=path)


def _require_exact(actual: object, expected: JsonValue, path: str) -> None:
    try:
        actual_bytes = canonical_json_bytes(cast(JsonValue, actual))
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "critical manifest value is not canonical JSON",
            json_path=path,
        ) from exc
    if actual_bytes != canonical_json_bytes(expected):
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "critical frozen manifest value differs",
            path,
        )


def _require_sdk_exact(actual: object, expected: JsonValue, path: str) -> None:
    try:
        actual_bytes = canonical_json_bytes(cast(JsonValue, actual))
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "SDK argument value is not canonical JSON",
            json_path=path,
        ) from exc
    if actual_bytes != canonical_json_bytes(expected):
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "SDK argument differs from the frozen model configuration",
            path,
        )


def _require_projection_hash(actual: object, expected_sha256: str, path: str) -> bytes:
    try:
        canonical = canonical_json_bytes(cast(JsonValue, actual))
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "static projection is not canonical JSON",
            json_path=path,
        ) from exc
    if _sha256(canonical) != expected_sha256:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "static projection differs from the frozen model binding",
            path,
        )
    return canonical


def _read_regular_nofollow(path: str | os.PathLike[str]) -> bytes:
    try:
        raw_value = os.fspath(path)
    except TypeError as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_PATH_INVALID",
            "manifest path is not path-like text",
            json_path="$",
        ) from exc
    if type(raw_value) is not str:
        _fail("LIVE_PREPARATION_PATH_INVALID", "manifest path must be text", "$")
    raw_path = raw_value
    if not raw_path or "\x00" in raw_path:
        _fail("LIVE_PREPARATION_PATH_INVALID", "manifest path is invalid", "$")
    pure = PurePosixPath(raw_path)
    if (
        raw_path != str(pure)
        or any(component in {".", ".."} for component in pure.parts)
        or not pure.name
    ):
        _fail(
            "LIVE_PREPARATION_PATH_INVALID",
            "manifest path must be lexical and normalized",
            "$",
        )
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        _fail(
            "LIVE_PREPARATION_NOFOLLOW_UNAVAILABLE",
            "no-follow directory traversal is required for the frozen manifest read",
            "$",
        )
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    opened: list[int] = []
    try:
        current = os.open("/" if pure.is_absolute() else ".", directory_flags)
        opened.append(current)
        components = pure.parts[1:] if pure.is_absolute() else pure.parts
        for component in components[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            opened.append(current)
        fd = os.open(components[-1], file_flags, dir_fd=current)
        opened.append(fd)
    except OSError as exc:
        for opened_fd in reversed(opened):
            os.close(opened_fd)
        raise ReplayRunnerError(
            "LIVE_PREPARATION_SOURCE_UNSAFE",
            "manifest could not be traversed as a no-follow regular file",
            json_path="$",
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail(
                "LIVE_PREPARATION_SOURCE_UNSAFE",
                "manifest source is not a regular file",
                "$",
            )
        if before.st_size != MODEL_CONFIG_MANIFEST_BYTE_COUNT:
            _fail(
                "LIVE_PREPARATION_MANIFEST_SIZE_MISMATCH",
                "frozen manifest byte count differs",
                "$",
            )
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    finally:
        for opened_fd in reversed(opened):
            os.close(opened_fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        _fail("LIVE_PREPARATION_SOURCE_CHANGED", "manifest changed during read", "$")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        _fail("LIVE_PREPARATION_SOURCE_CHANGED", "manifest read was incomplete", "$")
    return data


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class CheckpointInventoryBinding:
    canonical_bytes: bytes
    sha256: str
    config_file_count: int
    config_byte_count: int
    weight_shard_count: int
    weight_byte_count: int

    @property
    def files(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.canonical_bytes))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "files": self.files,
            "inventory_sha256": self.sha256,
            "config_file_count": self.config_file_count,
            "config_byte_count": self.config_byte_count,
            "weight_shard_count": self.weight_shard_count,
            "weight_byte_count": self.weight_byte_count,
            "total_file_count": self.config_file_count + self.weight_shard_count,
            "total_byte_count": self.config_byte_count + self.weight_byte_count,
            "runtime_files_observed": False,
        }


@dataclass(frozen=True, slots=True)
class LiveModelBinding:
    model_id: str
    role: str
    history_family: str
    model_repository: str
    model_revision: str
    served_model_name: str
    model_config_record_sha256: str
    checkpoint_inventory: CheckpointInventoryBinding
    captured_request_canonical_bytes: bytes
    formal_request_canonical_bytes: bytes
    formal_launch_canonical_bytes: bytes
    actor_adapter_canonical_bytes: bytes
    parser_binding_canonical_bytes: bytes
    tokenizer_binding_canonical_bytes: bytes

    @property
    def formal_request(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.formal_request_canonical_bytes))

    @property
    def captured_request(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.captured_request_canonical_bytes))

    @property
    def formal_launch(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.formal_launch_canonical_bytes))

    @property
    def parser_binding(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.parser_binding_canonical_bytes))

    @property
    def actor_adapter(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.actor_adapter_canonical_bytes))

    @property
    def tokenizer_binding(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.tokenizer_binding_canonical_bytes))

    def to_dict(self) -> dict[str, JsonValue]:
        tokenizer_artifacts = cast(list[dict[str, JsonValue]], self.tokenizer_binding["artifacts"])
        return {
            "model_id": self.model_id,
            "role": self.role,
            "history_family": self.history_family,
            "model_repository": self.model_repository,
            "model_revision": self.model_revision,
            "served_model_name": self.served_model_name,
            "model_config_record_sha256": self.model_config_record_sha256,
            "checkpoint_inventory": self.checkpoint_inventory.to_dict(),
            "captured_request": self.captured_request,
            "captured_request_sha256": _sha256(self.captured_request_canonical_bytes),
            "formal_request": self.formal_request,
            "formal_request_sha256": _sha256(self.formal_request_canonical_bytes),
            "formal_serving_launch": self.formal_launch,
            "formal_serving_launch_sha256": _sha256(self.formal_launch_canonical_bytes),
            "actor_adapter": self.actor_adapter,
            "actor_adapter_sha256": _sha256(self.actor_adapter_canonical_bytes),
            "parser_binding": self.parser_binding,
            "parser_binding_sha256": _sha256(self.parser_binding_canonical_bytes),
            "tokenizer_binding": self.tokenizer_binding,
            "tokenizer_binding_sha256": _sha256(self.tokenizer_binding_canonical_bytes),
            "tokenizer_artifact_count": len(tokenizer_artifacts),
            "tokenizer_artifact_byte_count": sum(
                cast(int, artifact["byte_count"]) for artifact in tokenizer_artifacts
            ),
            "runtime_tokenizer_observed": False,
            "local_snapshot_path_exposed": False,
        }


def _readiness_false_state() -> dict[str, JsonValue]:
    return {
        "execution_ready": False,
        "live_transport_validation_complete": False,
        "live_history_codec_ready": False,
        "curated_transformations_ready": False,
        "run_ready_seal_present": False,
        "provider_invocation_allowed": False,
        "treatment_response_generation_allowed": False,
        "formal_replay_ready": False,
    }


def _safety_false_state() -> dict[str, JsonValue]:
    return {
        "client_factory_invoked": False,
        "network_used": False,
        "subprocess_started": False,
        "gpu_probed": False,
        "gpu_used": False,
        "model_loaded": False,
        "provider_invoked": False,
        "replay_executed": False,
        "generated_action_executed": False,
    }


@dataclass(frozen=True, slots=True)
class LivePreparationReceipt:
    models: tuple[LiveModelBinding, ...]
    serving_environment_canonical_bytes: bytes
    deferred_preconditions_canonical_bytes: bytes

    def __post_init__(self) -> None:
        if (
            type(self.models) is not tuple
            or any(
                type(model) is not LiveModelBinding
                or type(model.checkpoint_inventory) is not CheckpointInventoryBinding
                or type(model.checkpoint_inventory.canonical_bytes) is not bytes
                or type(model.captured_request_canonical_bytes) is not bytes
                or type(model.formal_request_canonical_bytes) is not bytes
                or type(model.formal_launch_canonical_bytes) is not bytes
                or type(model.actor_adapter_canonical_bytes) is not bytes
                or type(model.parser_binding_canonical_bytes) is not bytes
                or type(model.tokenizer_binding_canonical_bytes) is not bytes
                for model in self.models
            )
            or type(self.serving_environment_canonical_bytes) is not bytes
            or type(self.deferred_preconditions_canonical_bytes) is not bytes
        ):
            _fail(
                "LIVE_PREPARATION_RECEIPT_INVALID",
                "live preparation receipt snapshots are not deeply immutable",
                "$.receipt",
            )
        try:
            receipt_sha256 = self.sha256
        except Exception as exc:
            raise ReplayRunnerError(
                "LIVE_PREPARATION_RECEIPT_INVALID",
                "live preparation receipt cannot be canonically rehydrated",
                json_path="$.receipt",
            ) from exc
        if receipt_sha256 != LIVE_PREPARATION_RECEIPT_SHA256:
            _fail(
                "LIVE_PREPARATION_RECEIPT_INVALID",
                "live preparation receipt differs from the frozen static binding",
                "$.receipt",
            )

    def model(self, model_id: str) -> LiveModelBinding:
        for binding in self.models:
            if binding.model_id == model_id:
                return binding
        _fail("LIVE_PREPARATION_MODEL_UNKNOWN", "model is not frozen for G1", "$.model_id")
        raise AssertionError("unreachable")

    @property
    def serving_environment(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.serving_environment_canonical_bytes))

    @property
    def deferred_preconditions(self) -> dict[str, JsonValue]:
        return cast(
            dict[str, JsonValue], _from_snapshot(self.deferred_preconditions_canonical_bytes)
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": LIVE_PREPARATION_RECEIPT_SCHEMA_VERSION,
            "record_type": "g1_live_preparation_receipt",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": LIVE_PREPARATION_STATUS,
            "protocol_version": PROTOCOL_VERSION,
            "model_config_manifest": {
                "relative_path": MODEL_CONFIG_MANIFEST_RELATIVE_PATH,
                "sha256": MODEL_CONFIG_MANIFEST_SHA256,
                "byte_count": MODEL_CONFIG_MANIFEST_BYTE_COUNT,
            },
            "active_g1_3_publication": {
                "manifest_sha256": ACTIVE_G1_3_MANIFEST_SHA256,
                "capsule_set_sha256": ACTIVE_G1_3_CAPSULE_SET_SHA256,
            },
            "live_code_prepared": True,
            "static_configuration_valid": True,
            "models": [model.to_dict() for model in self.models],
            "formal_serving_environment": self.serving_environment,
            "deferred_preconditions": self.deferred_preconditions,
            "blockers": list(LIVE_PREPARATION_BLOCKERS),
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes)


def _require_live_preparation_receipt(receipt: object) -> LivePreparationReceipt:
    if type(receipt) is not LivePreparationReceipt:
        _fail(
            "LIVE_PREPARATION_RECEIPT_INVALID",
            "live preparation APIs require the exact frozen receipt type",
            "$.receipt",
        )
    typed = cast(LivePreparationReceipt, receipt)
    try:
        receipt_sha256 = typed.sha256
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_RECEIPT_INVALID",
            "live preparation receipt cannot be canonically rehydrated",
            json_path="$.receipt",
        ) from exc
    if receipt_sha256 != LIVE_PREPARATION_RECEIPT_SHA256:
        _fail(
            "LIVE_PREPARATION_RECEIPT_INVALID",
            "live preparation receipt differs from the frozen static binding",
            "$.receipt",
        )
    return typed


def _canonical_openai_request(
    model_id: str,
    application_request: object,
    replay_seed: object,
) -> tuple[bytes, bytes]:
    if model_id not in _FORMAL_REQUEST:
        _fail("LIVE_PREPARATION_MODEL_UNKNOWN", "model is not frozen for G1", "$.model_id")
    if type(application_request) is not dict:
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "application request must be a plain object",
            "$.application_request",
        )
    request = cast(dict[str, JsonValue], application_request)
    formal = _FORMAL_REQUEST[model_id]
    frozen = cast(dict[str, JsonValue], formal["arguments_except_messages_and_seed"])
    required = set(frozen) | {"messages"}
    if not all(type(key) is str for key in request):
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "SDK argument keys must be strings",
            "$.application_request",
        )
    keys = set(request)
    missing = required - keys
    forbidden = {key for key in keys if key.casefold() in _FORBIDDEN_SDK_PLAN_KEYS}
    if missing or forbidden or "seed" in keys:
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "request omits frozen fields or contains seed/transport/credential controls",
            "$.application_request",
        )
    if "stream" in request and request["stream"] is not False:
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "stream must remain exactly false",
            "$.application_request.stream",
        )
    messages = request.get("messages")
    if (
        type(messages) is not list
        or not messages
        or any(type(item) is not dict for item in messages)
    ):
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "messages must be a non-empty list of objects",
            "$.application_request.messages",
        )
    if type(replay_seed) is not int or replay_seed not in REPLAY_SEEDS:
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "seed is not preregistered",
            "$.replay_seed",
        )
    for key, expected in frozen.items():
        _require_sdk_exact(request.get(key), expected, f"$.application_request.{key}")
    try:
        application_request_bytes = canonical_json_bytes(cast(JsonValue, request))
        kwargs = cast(dict[str, JsonValue], _from_snapshot(application_request_bytes))
        kwargs["seed"] = cast(int, replay_seed)
        kwargs_bytes = canonical_json_bytes(kwargs)
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "SDK arguments are not canonical JSON",
            json_path="$.application_request",
        ) from exc
    return application_request_bytes, kwargs_bytes


@dataclass(frozen=True, slots=True)
class OpenAIChatCallDescriptor:
    model_id: str
    model_config_record_sha256: str
    formal_request_sha256: str
    endpoint_origin: str
    endpoint_path: str
    sdk_method: str
    sdk_version: str
    timeout_seconds: float
    sdk_max_retries: int
    stream: bool
    application_request_canonical_bytes: bytes
    application_request_sha256: str
    replay_seed: int
    kwargs_canonical_bytes: bytes
    kwargs_sha256: str

    def __post_init__(self) -> None:
        if self.model_id not in _MODEL_IDENTITY:
            _fail("LIVE_PREPARATION_RECORD_INVALID", "call model is not frozen", "$.model_id")
        identity = _MODEL_IDENTITY[self.model_id]
        formal = _FORMAL_REQUEST[self.model_id]
        if not (
            self.model_config_record_sha256 == identity[5]
            and self.formal_request_sha256 == _sha256(_snapshot(formal))
            and self.endpoint_origin == formal["endpoint_origin"]
            and self.endpoint_path == formal["endpoint_path"]
            and self.sdk_method == formal["sdk"]
            and self.sdk_version == formal["sdk_version"]
            and type(self.timeout_seconds) is float
            and self.timeout_seconds == formal["timeout_seconds"]
            and type(self.sdk_max_retries) is int
            and self.sdk_max_retries == 0
            and self.stream is False
        ):
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call metadata differs from the frozen model binding",
                "$",
            )
        if type(self.application_request_canonical_bytes) is not bytes:
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call application request is not frozen bytes",
                "$.application_request",
            )
        try:
            application_request = _from_snapshot(self.application_request_canonical_bytes)
        except Exception as exc:
            raise ReplayRunnerError(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call application request cannot be rehydrated",
                json_path="$.application_request",
            ) from exc
        expected_application_bytes, expected_kwargs_bytes = _canonical_openai_request(
            self.model_id, application_request, self.replay_seed
        )
        if (
            self.application_request_canonical_bytes != expected_application_bytes
            or self.application_request_sha256 != _sha256(expected_application_bytes)
            or type(self.kwargs_canonical_bytes) is not bytes
            or self.kwargs_canonical_bytes != expected_kwargs_bytes
            or self.kwargs_sha256 != _sha256(expected_kwargs_bytes)
        ):
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call request bytes or hashes are inconsistent",
                "$",
            )

    @property
    def kwargs(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.kwargs_canonical_bytes))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": OPENAI_CHAT_CALL_DESCRIPTOR_SCHEMA_VERSION,
            "record_type": "inert_openai_chat_call_descriptor",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": LIVE_PREPARATION_STATUS,
            "model_id": self.model_id,
            "live_preparation_receipt_sha256": LIVE_PREPARATION_RECEIPT_SHA256,
            "model_config_manifest_sha256": MODEL_CONFIG_MANIFEST_SHA256,
            "model_config_record_sha256": self.model_config_record_sha256,
            "formal_request_sha256": self.formal_request_sha256,
            "endpoint": {"origin": self.endpoint_origin, "path": self.endpoint_path},
            "sdk": {
                "method": self.sdk_method,
                "version": self.sdk_version,
                "timeout_seconds": self.timeout_seconds,
                "max_retries": self.sdk_max_retries,
                "stream": self.stream,
            },
            "application_request_sha256": self.application_request_sha256,
            "application_request_byte_count": len(self.application_request_canonical_bytes),
            "application_delta": {
                "json_pointer": "/seed",
                "value": self.replay_seed,
                "only_application_delta": True,
            },
            "source_invariance_validated": False,
            "kwargs": self.kwargs,
            "kwargs_canonical_sha256": self.kwargs_sha256,
            "kwargs_canonical_byte_count": len(self.kwargs_canonical_bytes),
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatBlockDescriptor:
    model_id: str
    replay_seed: int
    calls: tuple[OpenAIChatCallDescriptor, ...]
    call_set_sha256: str

    def __post_init__(self) -> None:
        if (
            self.model_id not in _MODEL_IDENTITY
            or type(self.replay_seed) is not int
            or self.replay_seed not in REPLAY_SEEDS
            or type(self.calls) is not tuple
            or not self.calls
            or any(
                type(call) is not OpenAIChatCallDescriptor
                or call.model_id != self.model_id
                or call.replay_seed != self.replay_seed
                for call in self.calls
            )
        ):
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call block does not bind one frozen model and seed",
                "$",
            )
        expected_sha256 = _sha256(canonical_json_bytes([call.to_dict() for call in self.calls]))
        if self.call_set_sha256 != expected_sha256:
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "call-set hash is inconsistent",
                "$.call_set_sha256",
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": OPENAI_CHAT_BLOCK_DESCRIPTOR_SCHEMA_VERSION,
            "record_type": "inert_openai_chat_block_descriptor",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": LIVE_PREPARATION_STATUS,
            "model_id": self.model_id,
            "live_preparation_receipt_sha256": LIVE_PREPARATION_RECEIPT_SHA256,
            "replay_seed": self.replay_seed,
            "call_count": len(self.calls),
            "calls": [call.to_dict() for call in self.calls],
            "call_set_sha256": self.call_set_sha256,
            "paired_seed_consistency_validated": True,
            "formal_plan_pairing_validated": False,
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }


@dataclass(frozen=True, slots=True)
class VllmLaunchPlan:
    model_id: str
    model_repository: str
    model_revision: str
    model_config_record_sha256: str
    checkpoint_inventory_sha256: str
    snapshot_path: str
    argv: tuple[str, ...]
    argv_sha256: str

    def __post_init__(self) -> None:
        _validate_vllm_launch_plan(self)

    def to_dict(self) -> dict[str, JsonValue]:
        argv_value: list[JsonValue] = [item for item in self.argv]
        argv_bytes = canonical_json_bytes(argv_value)
        return {
            "schema_version": VLLM_LAUNCH_PLAN_SCHEMA_VERSION,
            "record_type": "inert_vllm_launch_plan",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": LIVE_PREPARATION_STATUS,
            "model_id": self.model_id,
            "live_preparation_receipt_sha256": LIVE_PREPARATION_RECEIPT_SHA256,
            "model_config_manifest_sha256": MODEL_CONFIG_MANIFEST_SHA256,
            "model_config_record_sha256": self.model_config_record_sha256,
            "checkpoint_inventory_sha256": self.checkpoint_inventory_sha256,
            "snapshot_binding": {
                "model_repository": self.model_repository,
                "model_revision": self.model_revision,
                "lexical_snapshot_path": self.snapshot_path,
                "runtime_snapshot_observed": False,
                "model_weights_loaded": False,
            },
            "argv": argv_value,
            "argv_canonical_sha256": self.argv_sha256,
            "argv_canonical_byte_count": len(argv_bytes),
            "generation_config_mapping": dict(_VLLM_GENERATION_CONFIG_MAPPING),
            "environment_variable_names": [],
            "serving_image_digest": None,
            "serving_image_digest_status": "PENDING_G1_7_SEAL",
            "launch_compatibility_validated": False,
            "endpoint_health_validated": False,
            "isolation_validated": False,
            "port_reserved": False,
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }


@dataclass(frozen=True, slots=True)
class OpenAIChatResponseProjection:
    content: str
    host_parser_input: str
    finish_reason: str | None
    usage_canonical_bytes: bytes | None
    envelope_canonical_bytes: bytes
    envelope_sha256: str
    projection_sha256: str

    def __post_init__(self) -> None:
        _validate_response_projection(self)

    @property
    def usage(self) -> dict[str, JsonValue] | None:
        if self.usage_canonical_bytes is None:
            return None
        return cast(dict[str, JsonValue], _from_snapshot(self.usage_canonical_bytes))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": OPENAI_CHAT_RESPONSE_PROJECTION_SCHEMA_VERSION,
            "record_type": "openai_chat_response_projection",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": LIVE_PREPARATION_STATUS,
            "response_source": "CALLER_INJECTED_NOT_PROVIDER_OBSERVED",
            "content": self.content,
            "host_parser_input": self.host_parser_input,
            "host_parser_input_normalization": ("PYTHON_STRIP_MATCHES_MOBILE_WORLD_BASE_AGENT_V1"),
            "finish_reason": self.finish_reason,
            "usage": self.usage,
            "envelope_sha256": self.envelope_sha256,
            "projection_sha256": self.projection_sha256,
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }


@dataclass(frozen=True, slots=True)
class InjectedGpuAssessment:
    model_id: str
    inventory_canonical_bytes: bytes
    inventory_sha256: str
    total_memory_bytes: int
    free_memory_bytes: int
    required_free_memory_bytes: int
    capacity_sufficient: bool

    def __post_init__(self) -> None:
        _validate_injected_gpu_assessment(self)

    @property
    def inventory(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], _from_snapshot(self.inventory_canonical_bytes))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": INJECTED_GPU_ASSESSMENT_SCHEMA_VERSION,
            "record_type": "injected_gpu_inventory_assessment",
            "contract_version": LIVE_PREPARATION_CONTRACT_VERSION,
            "status": "INJECTED_DATA_ONLY_NOT_HOST_OBSERVED",
            "model_id": self.model_id,
            "live_preparation_receipt_sha256": LIVE_PREPARATION_RECEIPT_SHA256,
            "inventory": self.inventory,
            "inventory_sha256": self.inventory_sha256,
            "gpu_memory_utilization": 0.24,
            "required_free_memory_bytes": self.required_free_memory_bytes,
            "capacity_sufficient": self.capacity_sufficient,
            "resource_snapshot_injected": True,
            "isolation_validated": False,
            "run_ready": False,
            "gpu_reserved": False,
            "readiness": _readiness_false_state(),
            "safety": _safety_false_state(),
        }


def _checkpoint_inventory_sha256(model_id: str) -> str:
    expected: dict[str, JsonValue] = {
        kind: [
            {"path": path, "byte_count": byte_count, "sha256": digest}
            for path, byte_count, digest in entries
        ]
        for kind, entries in _CHECKPOINTS[model_id].items()
    }
    return _sha256(_snapshot(expected))


def _validate_snapshot_identity(model_id: str, raw: object) -> str:
    if model_id not in _MODEL_IDENTITY or type(raw) is not str or not raw.startswith("/"):
        _fail(
            "LIVE_PREPARATION_SNAPSHOT_PATH_INVALID",
            "snapshot path must be lexical absolute",
            "$.snapshot_path",
        )
    raw_text = cast(str, raw)
    pure = PurePosixPath(raw_text)
    if raw_text != str(pure) or any(part in {".", ".."} for part in pure.parts):
        _fail(
            "LIVE_PREPARATION_SNAPSHOT_PATH_INVALID",
            "snapshot path is not normalized",
            "$.snapshot_path",
        )
    repository = _MODEL_IDENTITY[model_id][2]
    revision = _MODEL_IDENTITY[model_id][3]
    expected_tail = (
        f"models--{repository.replace('/', '--')}",
        "snapshots",
        revision,
    )
    if tuple(pure.parts[-3:]) != expected_tail:
        _fail(
            "LIVE_PREPARATION_SNAPSHOT_MODEL_MISMATCH",
            "snapshot path does not bind the frozen repository and revision",
            "$.snapshot_path",
        )
    return raw_text


def _render_vllm_argv_values(model_id: str, snapshot: str) -> tuple[str, ...]:
    served_model_name = _MODEL_IDENTITY[model_id][4]
    return (
        "vllm",
        "serve",
        snapshot,
        "--served-model-name",
        served_model_name,
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


def _validate_vllm_launch_plan(plan: VllmLaunchPlan) -> None:
    if plan.model_id not in _MODEL_IDENTITY:
        _fail("LIVE_PREPARATION_RECORD_INVALID", "launch model is not frozen", "$.model_id")
    identity = _MODEL_IDENTITY[plan.model_id]
    snapshot = _validate_snapshot_identity(plan.model_id, plan.snapshot_path)
    expected_argv = _render_vllm_argv_values(plan.model_id, snapshot)
    if not (
        plan.model_repository == identity[2]
        and plan.model_revision == identity[3]
        and plan.model_config_record_sha256 == identity[5]
        and plan.checkpoint_inventory_sha256 == _checkpoint_inventory_sha256(plan.model_id)
        and type(plan.argv) is tuple
        and plan.argv == expected_argv
        and plan.argv_sha256 == _sha256(canonical_json_bytes(list(expected_argv)))
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "launch plan differs from the frozen model and argv binding",
            "$",
        )


def _validate_response_projection(projection: OpenAIChatResponseProjection) -> None:
    if type(projection.envelope_canonical_bytes) is not bytes:
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response envelope is not frozen bytes",
            "$.envelope",
        )
    try:
        envelope_value = _from_snapshot(projection.envelope_canonical_bytes)
        canonical_envelope = canonical_json_bytes(envelope_value)
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response envelope cannot be rehydrated",
            json_path="$.envelope",
        ) from exc
    if (
        type(envelope_value) is not dict
        or canonical_envelope != projection.envelope_canonical_bytes
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response envelope is not a canonical object",
            "$.envelope",
        )
    envelope = cast(dict[str, JsonValue], envelope_value)
    choices_value = envelope.get("choices")
    if (
        type(choices_value) is not list
        or len(choices_value) != 1
        or type(choices_value[0]) is not dict
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response choice binding is invalid",
            "$.envelope.choices",
        )
    choices = cast(list[JsonValue], choices_value)
    choice = cast(dict[str, JsonValue], choices[0])
    message_value = choice.get("message")
    if ("index" in choice and (type(choice["index"]) is not int or choice["index"] != 0)) or type(
        message_value
    ) is not dict:
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response message binding is invalid",
            "$.envelope.choices[0]",
        )
    message = cast(dict[str, JsonValue], message_value)
    if type(message.get("content")) is not str or (
        "role" in message and message["role"] != "assistant"
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response message binding is invalid",
            "$.envelope.choices[0].message",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and type(finish_reason) is not str:
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response finish reason is invalid",
            "$.envelope.choices[0].finish_reason",
        )
    usage_value = envelope.get("usage")
    usage_bytes: bytes | None = None
    usage: dict[str, JsonValue] | None = None
    if usage_value is not None:
        required = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if type(usage_value) is not dict or set(usage_value) != required:
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "response usage binding is invalid",
                "$.envelope.usage",
            )
        usage = cast(dict[str, JsonValue], usage_value)
        prompt = usage["prompt_tokens"]
        completion = usage["completion_tokens"]
        total = usage["total_tokens"]
        if (
            type(prompt) is not int
            or type(completion) is not int
            or type(total) is not int
            or prompt < 0
            or completion < 0
            or total != prompt + completion
        ):
            _fail(
                "LIVE_PREPARATION_RECORD_INVALID",
                "response usage values are inconsistent",
                "$.envelope.usage",
            )
        usage_bytes = _snapshot(usage)
    content = cast(str, message["content"])
    expected_projection: dict[str, JsonValue] = {
        "content": content,
        "host_parser_input": content.strip(),
        "finish_reason": finish_reason,
        "usage": usage,
    }
    usage_snapshot_valid = (
        projection.usage_canonical_bytes is None
        if usage_bytes is None
        else type(projection.usage_canonical_bytes) is bytes
    )
    if not (
        usage_snapshot_valid
        and projection.content == content
        and projection.host_parser_input == content.strip()
        and projection.finish_reason == finish_reason
        and projection.usage_canonical_bytes == usage_bytes
        and projection.envelope_sha256 == _sha256(canonical_envelope)
        and projection.projection_sha256 == _sha256(_snapshot(expected_projection))
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "response projection fields or hashes are inconsistent",
            "$",
        )


def _validate_injected_gpu_assessment(assessment: InjectedGpuAssessment) -> None:
    if (
        assessment.model_id not in _MODEL_IDENTITY
        or type(assessment.inventory_canonical_bytes) is not bytes
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "GPU assessment model or inventory bytes are invalid",
            "$",
        )
    try:
        inventory_value = _from_snapshot(assessment.inventory_canonical_bytes)
        canonical_inventory = canonical_json_bytes(inventory_value)
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_RECORD_INVALID",
            "GPU inventory cannot be rehydrated",
            json_path="$.inventory",
        ) from exc
    if type(inventory_value) is not dict or set(inventory_value) != {
        "accelerator",
        "total_memory_bytes",
        "free_memory_bytes",
    }:
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "GPU inventory is not the closed injected shape",
            "$.inventory",
        )
    inventory = cast(dict[str, JsonValue], inventory_value)
    total = inventory.get("total_memory_bytes")
    free = inventory.get("free_memory_bytes")
    if (
        inventory.get("accelerator") != "NVIDIA H200"
        or type(total) is not int
        or type(free) is not int
        or total <= 0
        or free < 0
        or free > total
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "GPU inventory values are invalid",
            "$.inventory",
        )
    total_int = cast(int, total)
    free_int = cast(int, free)
    required = (total_int * 24 + 99) // 100
    if not (
        canonical_inventory == assessment.inventory_canonical_bytes
        and assessment.inventory_sha256 == _sha256(canonical_inventory)
        and type(assessment.total_memory_bytes) is int
        and assessment.total_memory_bytes == total_int
        and type(assessment.free_memory_bytes) is int
        and assessment.free_memory_bytes == free_int
        and type(assessment.required_free_memory_bytes) is int
        and assessment.required_free_memory_bytes == required
        and assessment.capacity_sufficient is (free_int >= required)
    ):
        _fail(
            "LIVE_PREPARATION_RECORD_INVALID",
            "GPU assessment fields or hashes are inconsistent",
            "$",
        )


def _checkpoint_inventory(model_id: str, value: object) -> CheckpointInventoryBinding:
    expected = _CHECKPOINTS[model_id]
    expected_json: dict[str, JsonValue] = {
        kind: [
            {"path": path, "byte_count": byte_count, "sha256": digest}
            for path, byte_count, digest in entries
        ]
        for kind, entries in expected.items()
    }
    _require_exact(value, expected_json, f"$.models[{model_id}].checkpoint_artifacts")
    canonical = _snapshot(expected_json)
    configs = expected["config_files"]
    weights = expected["weight_shards"]
    return CheckpointInventoryBinding(
        canonical_bytes=canonical,
        sha256=_sha256(canonical),
        config_file_count=len(configs),
        config_byte_count=sum(item[1] for item in configs),
        weight_shard_count=len(weights),
        weight_byte_count=sum(item[1] for item in weights),
    )


def load_live_preparation(path: str | os.PathLike[str]) -> LivePreparationReceipt:
    """Validate and bind the exact frozen manifest without observing live resources."""

    data = _read_regular_nofollow(path)
    if _sha256(data) != MODEL_CONFIG_MANIFEST_SHA256:
        _fail(
            "LIVE_PREPARATION_MANIFEST_HASH_MISMATCH",
            "manifest is not the exact frozen G1.1 model configuration",
            "$",
        )
    try:
        decoded = data.decode("utf-8")
        manifest = json.loads(decoded, object_pairs_hook=_object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_MANIFEST_JSON_INVALID",
            "manifest is not duplicate-free UTF-8 JSON",
            json_path="$",
        ) from exc
    if type(manifest) is not dict or set(manifest) != _ROOT_KEYS:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "manifest root is not the closed frozen object",
            "$",
        )
    root_expected: dict[str, JsonValue] = {
        "artifact_type": "g1_model_configuration_manifest",
        "schema_version": MODEL_CONFIG_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_VERSION,
        "manifest_phase": "G1.1_FROZEN_PRE_RESPONSE",
        "curated": True,
        "deployment_prediction": False,
        "run_ready": False,
        "treatment_response_generation_allowed": False,
    }
    for key, expected in root_expected.items():
        _require_exact(manifest.get(key), expected, f"$.{key}")
    replay_contract = manifest.get("captured_vs_formal_replay_contract")
    if type(replay_contract) is not dict:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "replay contract missing",
            "$.captured_vs_formal_replay_contract",
        )
    _require_exact(
        replay_contract.get("allowed_application_argument_delta_from_capture"),
        ["seed"],
        "$.captured_vs_formal_replay_contract.allowed_application_argument_delta_from_capture",
    )
    _require_exact(
        replay_contract.get("provider_seed_values"),
        list(REPLAY_SEEDS),
        "$.captured_vs_formal_replay_contract.provider_seed_values",
    )
    retry = replay_contract.get("retry_contract")
    if type(retry) is not dict:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "retry contract missing",
            "$.captured_vs_formal_replay_contract.retry_contract",
        )
    _require_exact(
        retry.get("formal_sdk_max_retries"),
        0,
        "$.captured_vs_formal_replay_contract.retry_contract.formal_sdk_max_retries",
    )
    _require_exact(
        retry.get("explicit_replay_retries_after_first_attempt"),
        2,
        "$.captured_vs_formal_replay_contract.retry_contract.explicit_replay_retries_after_first_attempt",
    )
    models_value = manifest.get("models")
    if type(models_value) is not list or len(models_value) != 2:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "exactly two frozen models are required",
            "$.models",
        )
    bindings: list[LiveModelBinding] = []
    for index, value in enumerate(models_value):
        if type(value) is not dict:
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "model entry is not an object",
                f"$.models[{index}]",
            )
        model_id = value.get("model_id")
        if not isinstance(model_id, str) or model_id not in _MODEL_IDENTITY:
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "unexpected model ID",
                f"$.models[{index}].model_id",
            )
        identity = _MODEL_IDENTITY[model_id]
        projection_hashes = _MODEL_PROJECTION_HASHES[model_id]
        for key, expected in zip(
            ("role", "history_family", "model_repository", "model_revision", "served_model_name"),
            identity[:5],
            strict=True,
        ):
            _require_exact(value.get(key), cast(JsonValue, expected), f"$.models[{index}].{key}")
        formal_request = _FORMAL_REQUEST[model_id]
        _require_exact(
            value.get("formal_replay_request"),
            formal_request,
            f"$.models[{index}].formal_replay_request",
        )
        _require_exact(
            value.get("formal_serving_launch"),
            _FORMAL_LAUNCH,
            f"$.models[{index}].formal_serving_launch",
        )
        captured_request = value.get("captured_application_request")
        actor_adapter = value.get("actor_adapter")
        tokenizer = value.get("tokenizer")
        if (
            type(captured_request) is not dict
            or type(actor_adapter) is not dict
            or type(tokenizer) is not dict
        ):
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "captured request, actor adapter, and tokenizer bindings are required",
                f"$.models[{index}]",
            )
        captured_request_bytes = _require_projection_hash(
            captured_request,
            cast(str, projection_hashes["captured_application_request"]),
            f"$.models[{index}].captured_application_request",
        )
        actor_adapter_bytes = _require_projection_hash(
            actor_adapter,
            cast(str, projection_hashes["actor_adapter"]),
            f"$.models[{index}].actor_adapter",
        )
        tokenizer_bytes = _require_projection_hash(
            tokenizer,
            cast(str, projection_hashes["tokenizer"]),
            f"$.models[{index}].tokenizer",
        )
        tokenizer_artifacts = tokenizer.get("artifacts")
        if type(tokenizer_artifacts) is not list or any(
            type(artifact) is not dict
            or type(artifact.get("byte_count")) is not int
            or cast(int, artifact["byte_count"]) < 0
            for artifact in tokenizer_artifacts
        ):
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "tokenizer artifact inventory is invalid",
                f"$.models[{index}].tokenizer.artifacts",
            )
        if (
            len(tokenizer_artifacts) != projection_hashes["tokenizer_artifact_count"]
            or sum(
                cast(int, artifact["byte_count"])
                for artifact in cast(list[dict[str, JsonValue]], tokenizer_artifacts)
            )
            != projection_hashes["tokenizer_artifact_byte_count"]
        ):
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "tokenizer artifact count or byte count differs",
                f"$.models[{index}].tokenizer.artifacts",
            )
        parser = value.get("parser_implementation")
        normalized = value.get("normalized_action_schema")
        if type(parser) is not dict or type(normalized) is not dict:
            _fail(
                "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
                "parser binding is missing",
                f"$.models[{index}].parser_implementation",
            )
        parser_projection: dict[str, JsonValue] = {
            "parser_implementation": cast(JsonValue, parser),
            "normalized_action_schema": cast(JsonValue, normalized),
        }
        _require_exact(
            parser_projection,
            _PARSER_BINDING[model_id],
            f"$.models[{index}].parser_implementation",
        )
        bindings.append(
            LiveModelBinding(
                model_id=model_id,
                role=identity[0],
                history_family=identity[1],
                model_repository=identity[2],
                model_revision=identity[3],
                served_model_name=identity[4],
                model_config_record_sha256=identity[5],
                checkpoint_inventory=_checkpoint_inventory(
                    model_id, value.get("checkpoint_artifacts")
                ),
                captured_request_canonical_bytes=captured_request_bytes,
                formal_request_canonical_bytes=_snapshot(formal_request),
                formal_launch_canonical_bytes=_snapshot(_FORMAL_LAUNCH),
                actor_adapter_canonical_bytes=actor_adapter_bytes,
                parser_binding_canonical_bytes=_snapshot(_PARSER_BINDING[model_id]),
                tokenizer_binding_canonical_bytes=tokenizer_bytes,
            )
        )
    if tuple(model.model_id for model in bindings) != ("qwen3vl_8b", "mai_ui_8b"):
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID", "frozen model order differs", "$.models"
        )
    environment = manifest.get("formal_serving_environment")
    if type(environment) is not dict:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "serving environment is missing",
            "$.formal_serving_environment",
        )
    _require_exact(
        environment.get("packages"),
        {
            "openai_server_dependency": "2.15.0",
            "ray": "2.48.0",
            "safetensors": "0.7.0",
            "tokenizers": "0.22.2",
            "torch": "2.8.0+cu126",
            "transformers": "4.57.4",
            "vllm": "0.11.0",
            "xformers": "0.0.32.post1",
        },
        "$.formal_serving_environment.packages",
    )
    _require_exact(
        environment.get("serving_image_digest"),
        None,
        "$.formal_serving_environment.serving_image_digest",
    )
    _require_exact(
        environment.get("serving_image_digest_status"),
        "PENDING_G1_7_SEAL",
        "$.formal_serving_environment.serving_image_digest_status",
    )
    safe_environment: dict[str, JsonValue] = {
        "status": cast(JsonValue, environment.get("status")),
        "python": cast(JsonValue, environment.get("python")),
        "packages": cast(JsonValue, environment.get("packages")),
        "optional_package_state": cast(JsonValue, environment.get("optional_package_state")),
        "environment_artifact_hashes": [
            {"name": PurePosixPath(cast(str, item["path"])).name, "sha256": item["sha256"]}
            for item in cast(list[dict[str, JsonValue]], environment.get("environment_identity"))
        ],
        "declared_host_facts_not_observed": cast(JsonValue, environment.get("host_facts")),
        "serving_image_digest": None,
        "serving_image_digest_status": "PENDING_G1_7_SEAL",
    }
    readiness = manifest.get("run_readiness")
    if type(readiness) is not dict:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "run readiness is missing",
            "$.run_readiness",
        )
    _require_exact(readiness.get("run_ready"), False, "$.run_readiness.run_ready")
    _require_exact(
        readiness.get("treatment_response_generation_allowed"),
        False,
        "$.run_readiness.treatment_response_generation_allowed",
    )
    _require_exact(readiness.get("included_count"), 0, "$.run_readiness.included_count")
    _require_exact(
        readiness.get("failure_mode"),
        "FAIL_CLOSED_BEFORE_ANY_TREATMENT_RESPONSE",
        "$.run_readiness.failure_mode",
    )
    formal_preflight = manifest.get("formal_preflight")
    runtime_boundary = manifest.get("runtime_boundary")
    if type(formal_preflight) is not dict or type(runtime_boundary) is not dict:
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "formal preflight and runtime boundary declarations are required",
            "$",
        )
    seed_support = formal_preflight.get("seed_support")
    serving_image = formal_preflight.get("serving_image")
    backend_and_isolation = formal_preflight.get("backend_and_isolation")
    provider_codec_state = formal_preflight.get("provider_codec_and_response_normalization")
    if not all(
        type(item) is dict
        for item in (
            seed_support,
            serving_image,
            backend_and_isolation,
            provider_codec_state,
        )
    ):
        _fail(
            "LIVE_PREPARATION_MANIFEST_CONTRACT_INVALID",
            "formal preflight subrecords are required",
            "$.formal_preflight",
        )
    _require_exact(
        cast(dict[str, JsonValue], seed_support).get("status"),
        "NOT_RUN_G1_1",
        "$.formal_preflight.seed_support.status",
    )
    _require_exact(
        cast(dict[str, JsonValue], seed_support).get("support_claimed"),
        False,
        "$.formal_preflight.seed_support.support_claimed",
    )
    _require_exact(
        cast(dict[str, JsonValue], seed_support).get("unseeded_substitution_allowed"),
        False,
        "$.formal_preflight.seed_support.unseeded_substitution_allowed",
    )
    _require_exact(
        cast(dict[str, JsonValue], serving_image).get("status"),
        "NOT_SEALED_G1_1",
        "$.formal_preflight.serving_image.status",
    )
    _require_exact(
        cast(dict[str, JsonValue], backend_and_isolation).get("status"),
        "NOT_RUN_G1_1",
        "$.formal_preflight.backend_and_isolation.status",
    )
    _require_exact(
        cast(dict[str, JsonValue], provider_codec_state).get("status"),
        "DOWNSTREAM_LOCK_OWNED_G1_4",
        "$.formal_preflight.provider_codec_and_response_normalization.status",
    )
    for key, expected in (
        ("generated_action_execution_allowed", False),
        ("collector_mutation_allowed", False),
        ("live_endpoint_must_be_revalidated_before_run", True),
    ):
        _require_exact(
            runtime_boundary.get(key),
            expected,
            f"$.runtime_boundary.{key}",
        )
    deferred_preconditions: dict[str, JsonValue] = {
        "formal_preflight": cast(JsonValue, formal_preflight),
        "runtime_boundary": cast(JsonValue, runtime_boundary),
        "run_readiness": cast(JsonValue, readiness),
    }
    receipt = LivePreparationReceipt(
        tuple(bindings),
        _snapshot(safe_environment),
        _snapshot(deferred_preconditions),
    )
    return _require_live_preparation_receipt(receipt)


def _lexical_snapshot_path(model: LiveModelBinding, path: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_SNAPSHOT_PATH_INVALID",
            "snapshot path is not path-like text",
            json_path="$.snapshot_path",
        ) from exc
    return _validate_snapshot_identity(model.model_id, raw)


def render_vllm_launch_argv(
    receipt: LivePreparationReceipt,
    model_id: str,
    snapshot_path: str | os.PathLike[str],
) -> tuple[str, ...]:
    """Render an argv tuple; never inspect weights or start a process."""

    model = _require_live_preparation_receipt(receipt).model(model_id)
    snapshot = _lexical_snapshot_path(model, snapshot_path)
    return _render_vllm_argv_values(model_id, snapshot)


def prepare_vllm_launch_plan(
    receipt: LivePreparationReceipt,
    model_id: str,
    snapshot_path: str | os.PathLike[str],
) -> VllmLaunchPlan:
    """Build a closed inert launch record without touching the supplied path."""

    validated_receipt = _require_live_preparation_receipt(receipt)
    model = validated_receipt.model(model_id)
    snapshot = _lexical_snapshot_path(model, snapshot_path)
    argv = render_vllm_launch_argv(validated_receipt, model_id, snapshot)
    argv_bytes = canonical_json_bytes(list(argv))
    return VllmLaunchPlan(
        model_id=model_id,
        model_repository=model.model_repository,
        model_revision=model.model_revision,
        model_config_record_sha256=model.model_config_record_sha256,
        checkpoint_inventory_sha256=model.checkpoint_inventory.sha256,
        snapshot_path=snapshot,
        argv=argv,
        argv_sha256=_sha256(argv_bytes),
    )


def prepare_openai_chat_call(
    receipt: LivePreparationReceipt,
    model_id: str,
    application_request: dict[str, JsonValue],
    replay_seed: int,
) -> OpenAIChatCallDescriptor:
    """Add only the registered seed to a captured request and freeze the call data."""

    model = _require_live_preparation_receipt(receipt).model(model_id)
    formal = model.formal_request
    application_request_bytes, kwargs_bytes = _canonical_openai_request(
        model_id, application_request, replay_seed
    )
    return OpenAIChatCallDescriptor(
        model_id=model_id,
        model_config_record_sha256=model.model_config_record_sha256,
        formal_request_sha256=_sha256(model.formal_request_canonical_bytes),
        endpoint_origin=cast(str, formal["endpoint_origin"]),
        endpoint_path=cast(str, formal["endpoint_path"]),
        sdk_method=cast(str, formal["sdk"]),
        sdk_version=cast(str, formal["sdk_version"]),
        timeout_seconds=cast(float, formal["timeout_seconds"]),
        sdk_max_retries=cast(int, formal["sdk_max_retries"]),
        stream=False,
        application_request_canonical_bytes=application_request_bytes,
        application_request_sha256=_sha256(application_request_bytes),
        replay_seed=replay_seed,
        kwargs_canonical_bytes=kwargs_bytes,
        kwargs_sha256=_sha256(kwargs_bytes),
    )


def prepare_openai_chat_block(
    receipt: LivePreparationReceipt,
    model_id: str,
    application_requests: tuple[dict[str, JsonValue], ...],
    replay_seed: int,
) -> OpenAIChatBlockDescriptor:
    """Freeze a non-empty call block under one mechanically shared replay seed."""

    if type(application_requests) is not tuple or not application_requests:
        _fail(
            "LIVE_PREPARATION_SDK_ARGUMENTS_INVALID",
            "application request block must be a non-empty tuple",
            "$.application_requests",
        )
    calls = tuple(
        prepare_openai_chat_call(receipt, model_id, request, replay_seed)
        for request in application_requests
    )
    call_set_bytes = canonical_json_bytes([call.to_dict() for call in calls])
    return OpenAIChatBlockDescriptor(
        model_id=model_id,
        replay_seed=replay_seed,
        calls=calls,
        call_set_sha256=_sha256(call_set_bytes),
    )


def decode_openai_chat_envelope(
    envelope: dict[str, JsonValue],
) -> OpenAIChatResponseProjection:
    """Project a strict non-stream OpenAI-compatible envelope without host parsing."""

    if type(envelope) is not dict:
        _fail("LIVE_PREPARATION_RESPONSE_INVALID", "response envelope must be a plain object", "$")
    choices_value = envelope.get("choices")
    if (
        type(choices_value) is not list
        or len(choices_value) != 1
        or type(choices_value[0]) is not dict
    ):
        _fail(
            "LIVE_PREPARATION_RESPONSE_INVALID",
            "exactly one response choice is required",
            "$.choices",
        )
    choices = cast(list[JsonValue], choices_value)
    choice = cast(dict[str, JsonValue], choices[0])
    if "index" in choice and (type(choice["index"]) is not int or choice["index"] != 0):
        _fail(
            "LIVE_PREPARATION_RESPONSE_INVALID",
            "first choice index must be zero",
            "$.choices[0].index",
        )
    message_value = choice.get("message")
    if type(message_value) is not dict or type(message_value.get("content")) is not str:
        _fail(
            "LIVE_PREPARATION_RESPONSE_INVALID",
            "assistant content must be a string",
            "$.choices[0].message.content",
        )
    message = cast(dict[str, JsonValue], message_value)
    if "role" in message and message["role"] != "assistant":
        _fail(
            "LIVE_PREPARATION_RESPONSE_INVALID",
            "message role must be assistant",
            "$.choices[0].message.role",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None and type(finish_reason) is not str:
        _fail(
            "LIVE_PREPARATION_RESPONSE_INVALID",
            "finish_reason must be string or null",
            "$.choices[0].finish_reason",
        )
    usage_value_raw = envelope.get("usage")
    usage_bytes: bytes | None = None
    usage_value: dict[str, JsonValue] | None = None
    if usage_value_raw is not None:
        required = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if type(usage_value_raw) is not dict or set(usage_value_raw) != required:
            _fail(
                "LIVE_PREPARATION_RESPONSE_INVALID",
                "usage must be the closed token projection",
                "$.usage",
            )
        usage = cast(dict[str, JsonValue], usage_value_raw)
        token_values: dict[str, int] = {}
        for key in required:
            value = usage[key]
            if type(value) is not int or value < 0:
                _fail(
                    "LIVE_PREPARATION_RESPONSE_INVALID",
                    "usage tokens must be non-negative integers",
                    f"$.usage.{key}",
                )
            token_values[key] = cast(int, value)
        if token_values["total_tokens"] != (
            token_values["prompt_tokens"] + token_values["completion_tokens"]
        ):
            _fail(
                "LIVE_PREPARATION_RESPONSE_INVALID",
                "usage total is inconsistent",
                "$.usage.total_tokens",
            )
        usage_value = usage
        usage_bytes = _snapshot(usage_value)
    try:
        envelope_bytes = canonical_json_bytes(cast(JsonValue, envelope))
    except Exception as exc:
        raise ReplayRunnerError(
            "LIVE_PREPARATION_RESPONSE_INVALID", "response is not canonical JSON", json_path="$"
        ) from exc
    content = cast(str, message["content"])
    host_parser_input = content.strip()
    projection: dict[str, JsonValue] = {
        "content": content,
        "host_parser_input": host_parser_input,
        "finish_reason": finish_reason,
        "usage": cast(JsonValue, usage_value),
    }
    return OpenAIChatResponseProjection(
        content=content,
        host_parser_input=host_parser_input,
        finish_reason=cast(str | None, finish_reason),
        usage_canonical_bytes=usage_bytes,
        envelope_canonical_bytes=envelope_bytes,
        envelope_sha256=_sha256(envelope_bytes),
        projection_sha256=_sha256(_snapshot(projection)),
    )


def assess_injected_gpu_inventory(
    receipt: LivePreparationReceipt,
    model_id: str,
    inventory: dict[str, JsonValue],
) -> InjectedGpuAssessment:
    """Assess caller-supplied H200 bytes; never discover or reserve a device."""

    _require_live_preparation_receipt(receipt).model(model_id)
    required_keys = {"accelerator", "total_memory_bytes", "free_memory_bytes"}
    if type(inventory) is not dict or set(inventory) != required_keys:
        _fail(
            "LIVE_PREPARATION_GPU_INVENTORY_INVALID",
            "inventory is not the closed injected shape",
            "$.inventory",
        )
    if inventory.get("accelerator") != "NVIDIA H200":
        _fail(
            "LIVE_PREPARATION_GPU_INVENTORY_INVALID",
            "accelerator must be NVIDIA H200",
            "$.inventory.accelerator",
        )
    total_value = inventory.get("total_memory_bytes")
    free_value = inventory.get("free_memory_bytes")
    if (
        type(total_value) is not int
        or type(free_value) is not int
        or total_value <= 0
        or free_value < 0
        or free_value > total_value
    ):
        _fail(
            "LIVE_PREPARATION_GPU_INVENTORY_INVALID",
            "memory values must be exact valid integer bytes",
            "$.inventory",
        )
    total = cast(int, total_value)
    free = cast(int, free_value)
    inventory_bytes = _snapshot(cast(JsonValue, inventory))
    required_free = (total * 24 + 99) // 100
    return InjectedGpuAssessment(
        model_id=model_id,
        inventory_canonical_bytes=inventory_bytes,
        inventory_sha256=_sha256(inventory_bytes),
        total_memory_bytes=total,
        free_memory_bytes=free,
        required_free_memory_bytes=required_free,
        capacity_sufficient=free >= required_free,
    )


__all__ = [
    "ACTIVE_G1_3_CAPSULE_SET_SHA256",
    "ACTIVE_G1_3_MANIFEST_SHA256",
    "CheckpointInventoryBinding",
    "InjectedGpuAssessment",
    "LiveModelBinding",
    "LivePreparationReceipt",
    "OpenAIChatBlockDescriptor",
    "OpenAIChatCallDescriptor",
    "OpenAIChatResponseProjection",
    "VllmLaunchPlan",
    "LIVE_PREPARATION_BLOCKERS",
    "LIVE_PREPARATION_CONTRACT_VERSION",
    "LIVE_PREPARATION_RECEIPT_SCHEMA_VERSION",
    "LIVE_PREPARATION_RECEIPT_SHA256",
    "MODEL_CONFIG_MANIFEST_SHA256",
    "assess_injected_gpu_inventory",
    "decode_openai_chat_envelope",
    "load_live_preparation",
    "prepare_openai_chat_block",
    "prepare_openai_chat_call",
    "prepare_vllm_launch_plan",
    "render_vllm_launch_argv",
]
