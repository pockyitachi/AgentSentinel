"""CPU-only contract tests for the D-034 synthetic GPU live-smoke boundary.

Nothing in this module starts a model, imports a GPU runtime, opens a socket, or
signals a process.  Live behavior is exercised separately against the frozen
authority; these tests validate its fail-closed data boundary with synthetic
non-case bytes and hostile mutations.
"""

from __future__ import annotations

import builtins
import copy
import hashlib
import importlib
import importlib.util
import json
import os
import re
import socket
import stat
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from jsonschema import Draft202012Validator, RefResolver  # type: ignore[import-untyped]

from mobile_world.offline import gpu_live_smoke as gpu_live_smoke_module
from mobile_world.offline.causal_replay.contracts import JsonValue, canonical_json_bytes
from mobile_world.offline.causal_replay_runner import (
    JsonActionParser,
    OpenAICompatibleProviderCodec,
    ReplayRunnerError,
    execute_live_arm,
)
from mobile_world.offline.causal_replay_runner.live_preparation import (
    LIVE_PREPARATION_RECEIPT_SHA256,
    MODEL_CONFIG_MANIFEST_SHA256,
)
from mobile_world.offline.g1_history_codecs.codecs import (
    MaiRawReplayHistoryCodec,
    QwenFlatProgressHistoryCodec,
)
from mobile_world.offline.gpu_live_smoke import (
    G1_5_ARMS,
    MODEL_ORDER,
    REPLAY_SEEDS,
    GpuLiveSmokeError,
    GpuLiveSmokeOperations,
    compile_gpu_smoke_packet,
    execute_gpu_live_smoke,
    load_gpu_live_authority,
    load_gpu_smoke_packet,
    prepare_gpu_live_smoke,
    write_gpu_smoke_packet,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_CONFIG_PATH = REPOSITORY_ROOT / "mobileworld_audit_handoff/g1/model_config_manifest.v1.json"
FIXTURE_PATH = Path(__file__).parent / "fixtures/g1_gpu_smoke/synthetic_non_case.v1.json"
GPU_SMOKE_SCHEMA_ROOT = REPOSITORY_ROOT / "mobileworld_audit_handoff/schemas/g1_gpu_smoke"
GPU_SMOKE_RUNNER_CLI = REPOSITORY_ROOT / "MobileWorld/scripts/run_g1_gpu_live_smoke.py"
QWEN_CAPTURED_FIXTURE = (
    Path(__file__).parent / "fixtures/g1_5_history_codecs/qwen_flat_progress.captured.v1.json"
)
MAI_CAPTURED_FIXTURE = (
    Path(__file__).parent / "fixtures/g1_5_history_codecs/mai_raw_replay.captured.v1.json"
)
GPU0_UUID = "GPU-991ac45f-e9e9-1c25-590c-fb49ca752965"
G1_5_CPU_PUBLICATION_SHA256 = "cffd7f24bf09f2e18c012b2a96591064e8ba200378c7e9c920d6fdd8f068d018"
MODEL_REVISIONS = {
    "qwen3vl_8b": "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
    "mai_ui_8b": "e00a0097abb9cc621cac5172d8c4809f0839c94e",
}
MODEL_REPOSITORIES = {
    "qwen3vl_8b": "Qwen/Qwen3-VL-8B-Instruct",
    "mai_ui_8b": "Tongyi-MAI/MAI-UI-8B",
}
MODEL_SERVED_NAMES = {
    "qwen3vl_8b": "Qwen3-VL-8B-Instruct",
    "mai_ui_8b": "MAI-UI-8B",
}
MODEL_CODECS = {
    "qwen3vl_8b": "mobileworld.g1.history-codec.qwen-flat-progress",
    "mai_ui_8b": "mobileworld.g1.history-codec.mai-raw-replay",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: JsonValue) -> str:
    return _sha256(canonical_json_bytes(value))


def _load_runner_cli_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "g1_gpu_live_smoke_cli_cpu_test",
        GPU_SMOKE_RUNNER_CLI,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fixture() -> dict[str, Any]:
    fixture = json.loads(FIXTURE_PATH.read_bytes())
    assert isinstance(fixture, dict)
    return fixture


def _load_schema(name: str) -> dict[str, Any]:
    value = json.loads((GPU_SMOKE_SCHEMA_ROOT / name).read_bytes())
    assert isinstance(value, dict)
    return value


def _schema_validator(name: str) -> Draft202012Validator:
    schemas = [_load_schema(path.name) for path in sorted(GPU_SMOKE_SCHEMA_ROOT.glob("*.json"))]
    schema = _load_schema(name)
    resolver = RefResolver.from_schema(
        schema,
        store={cast(str, candidate["$id"]): candidate for candidate in schemas},
    )
    return Draft202012Validator(schema, resolver=resolver)


def _replace_tiny_image(value: JsonValue, data_url: str) -> JsonValue:
    if isinstance(value, dict):
        return {key: _replace_tiny_image(child, data_url) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_tiny_image(child, data_url) for child in value]
    if value == "__TINY_PNG_DATA_URL__":
        return data_url
    return value


def _source_request(fixture: dict[str, Any], model_id: str) -> dict[str, JsonValue]:
    source = copy.deepcopy(fixture["source_requests"][model_id])
    replaced = _replace_tiny_image(
        cast(JsonValue, source),
        cast(str, fixture["tiny_png_data_url"]),
    )
    assert isinstance(replaced, dict)
    return cast(dict[str, JsonValue], replaced)


def _rendered_request(
    fixture: dict[str, Any],
    model_id: str,
    arm: str | None,
) -> dict[str, JsonValue]:
    request = _source_request(fixture, model_id)
    if arm in {None, "ORIGINAL"}:
        return request
    rendered = cast(str, fixture["rendered_history_text_by_arm"][arm])
    messages = request["messages"]
    assert isinstance(messages, list)
    if model_id == "qwen3vl_8b":
        user = messages[1]
        assert isinstance(user, dict)
        content = user["content"]
        assert isinstance(content, list)
        text = content[0]
        assert isinstance(text, dict)
        old = text["text"]
        assert isinstance(old, str)
        text["text"] = old.replace("Synthetic calibration only.", rendered)
    else:
        assistant = messages[2]
        assert isinstance(assistant, dict)
        old = assistant["content"]
        assert isinstance(old, str)
        assistant["content"] = old.replace(
            "Synthetic calibration only.",
            rendered,
        )
    return request


def _gpu_smoke_packet() -> dict[str, JsonValue]:
    fixture = _load_fixture()
    source_hashes = {
        model_id: _canonical_sha256(
            cast(JsonValue, _rendered_request(fixture, model_id, "ORIGINAL"))
        )
        for model_id in MODEL_ORDER
    }
    calls: list[JsonValue] = []
    for descriptor in fixture["expected_calls"]:
        model_id = cast(str, descriptor["model_id"])
        phase = cast(str, descriptor["phase"])
        arm = cast(str | None, descriptor["arm"])
        request = _rendered_request(fixture, model_id, arm)
        if phase == "G1_4_CANARY":
            diff: JsonValue = None
            mapping: JsonValue = None
            render_evidence: JsonValue = None
        else:
            diff = cast(JsonValue, copy.deepcopy(fixture["diff_by_arm"][arm]))
            mapping = cast(JsonValue, copy.deepcopy(fixture["mapping_by_arm"][arm]))
            render_evidence = {
                "source_application_request_sha256": source_hashes[model_id],
                "rendered_application_request_sha256": _canonical_sha256(cast(JsonValue, request)),
                "diff_sha256": _canonical_sha256(diff),
                "mapping_sha256": _canonical_sha256(mapping),
                "target_only_diff": True,
                "source_mapping_reversible": True,
                "provider_invocation_allowed": False,
            }
        calls.append(
            {
                "call_id": descriptor["call_id"],
                "phase": phase,
                "model_id": model_id,
                "codec_id": descriptor["codec_id"],
                "seed": descriptor["seed"],
                "repeat_index": descriptor["repeat_index"],
                "arm": arm,
                "application_request": request,
                "diff": diff,
                "mapping": mapping,
                "render_evidence": render_evidence,
            }
        )
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-packet/v1",
        "packet_id": "synthetic-non-case-g14-g15-gpu-smoke-v1",
        "synthetic_non_case": True,
        "secret_free": True,
        "formal_capsule": False,
        "contains_real_task_data": False,
        "generated_action_execution_allowed": False,
        "source_bindings": {
            "g1_5_cpu_publication_sha256": G1_5_CPU_PUBLICATION_SHA256,
            "compiler_contract": "mobileworld.g1.gpu-live-smoke-packet-compiler/v1",
            "fixtures": {
                "qwen3vl_8b": {
                    "relative_path": (
                        "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
                        "qwen_flat_progress.captured.v1.json"
                    ),
                    "file_sha256": (
                        "60f19821f782cd20ded8a926ea4466dff151cc5163a4728f9d0c761ae08b34be"
                    ),
                    "fixture_id": "g15-qwen-flat-progress-captured-redacted-v1",
                    "fixture_request_sha256": (
                        "72f1396204e56c05b49a2a8564650f915c780d9bfa32f455f1cef3320abd6a33"
                    ),
                },
                "mai_ui_8b": {
                    "relative_path": (
                        "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
                        "mai_raw_replay.captured.v1.json"
                    ),
                    "file_sha256": (
                        "b9e025b0b4990e9e3fe259b7dd21e2919de6538c00b42f2936c7b9fe403e9b40"
                    ),
                    "fixture_id": "g15-mai-raw-replay-captured-redacted-v1",
                    "fixture_request_sha256": (
                        "c2ee086c6e5e659c4f904fbb74afefc9c89f7aabcfd47441ce65cce332d37a7d"
                    ),
                },
            },
        },
        "calls": calls,
    }


def _authority(packet_sha256: str) -> dict[str, JsonValue]:
    runtime_scratch_root = "/synthetic/runtime-scratch/g1-gpu-smoke"
    private_runtime_root = "/synthetic/private-runtime/g1-gpu-smoke"
    private_python = f"{private_runtime_root}/bin/python3.12"
    client_site_packages = f"{private_runtime_root}/site-packages/client"
    server_site_packages = f"{private_runtime_root}/site-packages/server"
    critical_files: dict[str, JsonValue] = {}
    for name, relative_path in gpu_live_smoke_module._CRITICAL_SOURCE_FILES.items():
        critical_files[name] = {
            "relative_path": relative_path,
            "sha256": _sha256((REPOSITORY_ROOT / relative_path).read_bytes()),
        }
    runner_module_sha256 = cast(
        str,
        cast(dict[str, JsonValue], critical_files["gpu_live_smoke"])["sha256"],
    )
    runner_cli_sha256 = cast(
        str,
        cast(dict[str, JsonValue], critical_files["runner_cli"])["sha256"],
    )
    source_tree_sha256 = "f" * 64
    source_tree_entry_count = len(critical_files)
    source_tree_byte_count = sum(
        len((REPOSITORY_ROOT / cast(str, binding["relative_path"])).read_bytes())
        for binding in critical_files.values()
        if type(binding) is dict
    )
    outer_bootstrap_code_sha256 = "9" * 64
    outer_bootstrap_code_byte_count = 1
    bootstrap_manifest_sha256 = _canonical_sha256(
        {
            "worktree_root": str(REPOSITORY_ROOT),
            "source_root": str(REPOSITORY_ROOT / "MobileWorld/src"),
            "client_site_packages_path": client_site_packages,
            "server_site_packages_path": server_site_packages,
            "python_flags": list(gpu_live_smoke_module._ISOLATED_PYTHON_FLAGS),
            "python_pycache_prefix": "/dev/null",
            "server_bootstrap_code_sha256": _sha256(
                gpu_live_smoke_module._server_bootstrap_code(server_site_packages).encode()
            ),
            "critical_files": critical_files,
            "source_tree_sha256": source_tree_sha256,
            "source_tree_entry_count": source_tree_entry_count,
            "source_tree_byte_count": source_tree_byte_count,
            "outer_bootstrap_code_sha256": outer_bootstrap_code_sha256,
            "outer_bootstrap_code_byte_count": outer_bootstrap_code_byte_count,
        }
    )
    outer_fd_closure_sha256 = _canonical_sha256(gpu_live_smoke_module._outer_fd_closure_receipt())
    models: dict[str, JsonValue] = {}
    for model_id in MODEL_ORDER:
        repository = MODEL_REPOSITORIES[model_id]
        revision = MODEL_REVISIONS[model_id]
        models[model_id] = {
            "snapshot_path": (
                f"/synthetic/hf-cache/models--{repository.replace('/', '--')}/snapshots/{revision}"
            ),
            "snapshot_tree_sha256": "3" * 64,
            "snapshot_tree_entry_count": 1,
            "snapshot_tree_byte_count": 1,
            "repository": repository,
            "revision": revision,
            "served_name": MODEL_SERVED_NAMES[model_id],
        }
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-authority/v1",
        "authority_id": "owner-d034-gpu0-shared-v1",
        "decision_id": "D-034",
        "authorized_scope": "SYNTHETIC_NON_CASE_GPU_LIVE_SMOKE_22_CALLS",
        "authorized": True,
        "issued_at_utc": "2026-08-01T00:00:00Z",
        "expires_at_utc": "2027-08-01T00:00:00Z",
        "owner_uid": 0,
        "gpu": {
            "physical_index": 0,
            "uuid": GPU0_UUID,
            "cuda_visible_devices": GPU0_UUID,
            "shared": True,
            "exclusive": False,
            "minimum_free_memory_bytes": 68_719_476_736,
            "foreign_process_signaling_allowed": False,
        },
        "endpoint": {
            "origin": "http://127.0.0.1:18007",
            "path": "/v1/chat/completions",
            "health_path": "/health",
            "host": "127.0.0.1",
            "port": 18007,
        },
        "model_order": list(MODEL_ORDER),
        "models": models,
        "outer_runtime": {
            "python_path": "/usr/bin/python3.10",
            "python_resolved_path": "/usr/bin/python3.10",
            "python_sha256": ("d6bca2b84e73c7775a0dd5e6a76899cfe4ee62863d7c8f88513811d1fda23f49"),
            "python_byte_count": 5_937_704,
            "python_version": "3.10.12",
            "python_flags": list(gpu_live_smoke_module._ISOLATED_PYTHON_FLAGS),
            "stdlib_root": "/usr/lib/python3.10",
            "stdlib_tree_sha256": "a" * 64,
            "stdlib_tree_entry_count": 1,
            "stdlib_tree_byte_count": 1,
            "required_owner_uid": 0,
            "required_owner_gid": 0,
            "directory_mode": 0o755,
            "regular_mode": 0o644,
            "executable_mode": 0o755,
            "symlinks_allowed": True,
            "hardlinks_allowed": True,
        },
        "private_runtime": {
            "root": private_runtime_root,
            "python_path": private_python,
            "python_resolved_path": private_python,
            "python_sha256": "b" * 64,
            "python_byte_count": 7_901_928,
            "python_version": "3.12.12",
            "python_flags": list(gpu_live_smoke_module._ISOLATED_PYTHON_FLAGS),
            "stdlib_root": f"{private_runtime_root}/lib/python3.12",
            "tree_sha256": "c" * 64,
            "tree_entry_count": 1,
            "tree_byte_count": 1,
            "owner_uid": 0,
            "owner_gid": 0,
            "directory_mode": 0o500,
            "regular_mode": 0o400,
            "executable_mode": 0o500,
            "symlinks_allowed": False,
            "hardlinks_allowed": False,
        },
        "client_runtime": {
            "python_path": private_python,
            "python_resolved_path": private_python,
            "python_sha256": "b" * 64,
            "site_packages_path": client_site_packages,
            "openai_version": "1.106.1",
            "site_packages_tree_sha256": "d" * 64,
            "site_packages_tree_entry_count": 1,
            "site_packages_tree_byte_count": 1,
            "site_packages_owner_uid": 0,
            "site_packages_owner_gid": 0,
            "site_packages_directory_mode": 0o500,
            "site_packages_regular_mode": 0o400,
            "site_packages_executable_mode": 0o500,
            "site_packages_symlinks_allowed": False,
            "site_packages_hardlinks_allowed": False,
        },
        "server_runtime": {
            "python_path": private_python,
            "python_resolved_path": private_python,
            "python_sha256": "b" * 64,
            "site_packages_path": server_site_packages,
            "openai_version": "2.15.0",
            "vllm_version": "0.11.0",
            "torch_version": "2.8.0+cu126",
            "site_packages_tree_sha256": "e" * 64,
            "site_packages_tree_entry_count": 1,
            "site_packages_tree_byte_count": 1,
            "site_packages_owner_uid": 0,
            "site_packages_owner_gid": 0,
            "site_packages_directory_mode": 0o500,
            "site_packages_regular_mode": 0o400,
            "site_packages_executable_mode": 0o500,
            "site_packages_symlinks_allowed": False,
            "site_packages_hardlinks_allowed": False,
        },
        "bindings": {
            "smoke_packet_sha256": packet_sha256,
            "model_config_manifest_sha256": MODEL_CONFIG_MANIFEST_SHA256,
            "live_preparation_receipt_sha256": LIVE_PREPARATION_RECEIPT_SHA256,
            "g1_5_cpu_publication_sha256": G1_5_CPU_PUBLICATION_SHA256,
            "runner_module_sha256": runner_module_sha256,
            "runner_cli_sha256": runner_cli_sha256,
            "source_git_commit": "6" * 40,
        },
        "matrix": {
            "total_calls": 22,
            "g1_4_calls": 12,
            "g1_5_calls": 10,
            "replay_seeds": list(REPLAY_SEEDS),
            "repeats_per_seed": 2,
            "g1_5_seed": 1729,
            "arms": list(G1_5_ARMS),
        },
        "policies": {
            "hf_hub_offline": True,
            "transformers_offline": True,
            "local_files_only": True,
            "loopback_only": True,
            "sdk_hidden_retries": 0,
            "stream": False,
            "sequential_models": True,
            "model_co_residency_allowed": False,
            "generated_action_execution_allowed": False,
            "replay_allowed": False,
            "backend_restore_allowed": False,
            "mobileworld_action_allowed": False,
            "broad_process_signaling_allowed": False,
        },
        "source": {
            "worktree_root": str(REPOSITORY_ROOT),
            "source_root": str(REPOSITORY_ROOT / "MobileWorld/src"),
            "git_path": "/usr/bin/git",
            "git_sha256": _sha256(Path("/usr/bin/git").read_bytes()),
            "head_commit": "6" * 40,
            "critical_files": critical_files,
            "bootstrap_manifest_sha256": bootstrap_manifest_sha256,
            "source_tree_sha256": source_tree_sha256,
            "source_tree_entry_count": source_tree_entry_count,
            "source_tree_byte_count": source_tree_byte_count,
            "outer_bootstrap_code_sha256": outer_bootstrap_code_sha256,
            "outer_bootstrap_code_byte_count": outer_bootstrap_code_byte_count,
        },
        "network_namespace": {
            "required": True,
            "implementation": "LINUX_USER_NETNS_MAP_ROOT_V1",
            "host_owner_uid": os.getuid(),
            "host_owner_gid": os.getgid(),
            "inside_owner_uid": 0,
            "inside_owner_gid": 0,
            "uid_map_line": f"0 {os.getuid()} 1",
            "gid_map_line": f"0 {os.getgid()} 1",
            "env_path": "/usr/bin/env",
            "env_sha256": ("854a8d7f147ff1bf3562edd1aa0b2f2ac28ef432811533f03c43dc9162fe3af3"),
            "unshare_path": "/usr/bin/unshare",
            "unshare_sha256": ("72a34e6ba98a59f1da0c7b4d8c9722b746b5ade54e4d7e8de8e519c2993858ad"),
            "ip_path": "/usr/bin/ip",
            "ip_sha256": ("40cd6fd071451ae104d23783b6ae22efff4f1099167d73b41ce900fc49c8abaa"),
            "setpriv_path": "/usr/bin/setpriv",
            "setpriv_sha256": _sha256(Path("/usr/bin/setpriv").read_bytes()),
            "nvidia_smi_path": "/usr/bin/nvidia-smi",
            "nvidia_smi_sha256": (
                "cd4dc7637cd3ef30002cbf97afcc66f111eb90c0c615f37e264c392242eb51b6"
            ),
            "nvidia_smi_byte_count": 1_243_808,
            "pre_namespace_environment": {"LC_CTYPE": "C.UTF-8"},
            "launcher_environment": {
                "PATH": "/usr/bin:/bin",
                "CUDA_VISIBLE_DEVICES": GPU0_UUID,
                "LD_LIBRARY_PATH": "",
                "HOME": f"{runtime_scratch_root}/namespace-launcher/home",
                "HF_HOME": f"{runtime_scratch_root}/namespace-launcher/hf-home",
                "XDG_CACHE_HOME": (f"{runtime_scratch_root}/namespace-launcher/xdg-cache"),
                "TORCH_HOME": f"{runtime_scratch_root}/namespace-launcher/torch-home",
                "TRITON_CACHE_DIR": (f"{runtime_scratch_root}/namespace-launcher/triton-cache"),
                "VLLM_CACHE_ROOT": (f"{runtime_scratch_root}/namespace-launcher/vllm-cache"),
                "TMPDIR": f"{runtime_scratch_root}/namespace-launcher/tmp",
                "PYTHONNOUSERSITE": "1",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
                "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256": outer_fd_closure_sha256,
            },
            "expected_interfaces": ["lo"],
            "loopback_up_required": True,
            "default_route_allowed": False,
            "external_network_allowed": False,
            "python_pycache_prefix": "/dev/null",
            "fd_close_upper_bound_exclusive": (
                gpu_live_smoke_module._FD_CLOSE_UPPER_BOUND_EXCLUSIVE
            ),
            "outer_fd_closure_receipt_sha256": outer_fd_closure_sha256,
        },
        "evidence_root": "/synthetic/evidence/g1-gpu-smoke",
        "runtime_scratch_root": runtime_scratch_root,
    }


def _write_canonical_json(tmp_path: Path, name: str, value: JsonValue) -> tuple[Path, str]:
    data = canonical_json_bytes(value)
    path = tmp_path / name
    path.write_bytes(data)
    return path, _sha256(data)


def _loaded_inputs(tmp_path: Path) -> tuple[Any, Any]:
    packet = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
        g1_5_seed=1729,
    )
    packet_path = tmp_path / "compiled-smoke-packet.json"
    packet_path.write_bytes(packet.canonical_bytes)
    authority_value = _authority(packet.sha256)
    authority_path, authority_sha = _write_canonical_json(
        tmp_path, "gpu-smoke-authority.json", authority_value
    )
    reloaded_packet = load_gpu_smoke_packet(
        packet_path,
        packet.sha256,
        g1_5_seed=1729,
    )
    authority = load_gpu_live_authority(authority_path, authority_sha)
    return authority, reloaded_packet


def _process_identity(
    *,
    uid: int | None = None,
    pid: int = 41000,
    ppid: int = 1,
    pgid: int = 41000,
    sid: int = 41000,
    starttime_ticks: int = 90_000,
    argv: tuple[str, ...] = (
        "/synthetic/server/bin/python",
        "-I",
        "-m",
        "vllm.entrypoints.cli.main",
        "/synthetic/hf-cache/models--Qwen--Qwen3-VL-8B-Instruct/snapshots/"
        "0c351dd01ed87e9c1b53cbc748cba10e6187ff3b",
        "--served-model-name",
        "Qwen3-VL-8B-Instruct",
        "--host",
        "127.0.0.1",
        "--port",
        "18007",
    ),
) -> Any:
    return gpu_live_smoke_module.ProcessIdentity(
        uid=os.getuid() if uid is None else uid,
        pid=pid,
        ppid=ppid,
        pgid=pgid,
        sid=sid,
        starttime_ticks=starttime_ticks,
        executable_path="/synthetic/server/bin/python",
        executable_sha256="a" * 64,
        argv=argv,
    )


def _ownership_guard(root: Any) -> Any:
    return gpu_live_smoke_module.ProcessOwnershipGuard(
        root=root,
        model_id="qwen3vl_8b",
        snapshot_path=(
            "/synthetic/hf-cache/models--Qwen--Qwen3-VL-8B-Instruct/"
            f"snapshots/{MODEL_REVISIONS['qwen3vl_8b']}"
        ),
        served_name="Qwen3-VL-8B-Instruct",
        host="127.0.0.1",
        port=18007,
        expected_argv=root.argv,
        environment_sha256="b" * 64,
    )


def _execution_files(tmp_path: Path) -> tuple[Path, str, Path, Path]:
    packet = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
        g1_5_seed=1729,
    )
    packet_path = tmp_path / "compiled-execution-packet.json"
    packet_path.write_bytes(packet.canonical_bytes)
    evidence_root = tmp_path / "evidence"
    authority_value = _authority(packet.sha256)
    authority_value["evidence_root"] = str(evidence_root)
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "execution-authority.json",
        cast(JsonValue, authority_value),
    )
    return authority_path, authority_sha, packet_path, evidence_root


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


class _FakeGpuLiveOperations(GpuLiveSmokeOperations):
    def __init__(self, *, fail_at_call: int | None = None) -> None:
        self.fail_at_call = fail_at_call
        self.trace: list[str] = []
        self.invoke_count = 0
        self.capacity_count = 0
        self.port_check_count = 0
        self.socket_census_count = 0
        self.snapshot_counts = {model_id: 0 for model_id in MODEL_ORDER}
        self.active_pids: set[int] = set()
        self.stopped_models: set[str] = set()
        self.release_verified_models: set[str] = set()
        self.release_port_verified_models: set[str] = set()
        self.post_snapshot_models: set[str] = set()
        self.pid_by_model = {"qwen3vl_8b": 41_000, "mai_ui_8b": 42_000}
        self.model_by_pid = {pid: model_id for model_id, pid in self.pid_by_model.items()}
        self.foreign_pid = 51_000

    def assert_authority_active(self, authority: Any) -> None:
        del authority
        self.trace.append("authority-active")

    def verify_network_namespace(self, authority: Any) -> dict[str, JsonValue]:
        namespace = cast(dict[str, JsonValue], authority.network_namespace)
        self.trace.append("network-namespace")
        return {
            "schema_version": "mobileworld.g1.gpu-live-smoke-network-namespace/v1",
            "implementation": namespace["implementation"],
            "host_owner_uid": namespace["host_owner_uid"],
            "host_owner_gid": namespace["host_owner_gid"],
            "inside_owner_uid": 0,
            "inside_owner_gid": 0,
            "external_network_mechanically_unavailable": True,
            "synthetic_cpu_fake": True,
        }

    def prepare_runtime_scratch(self, authority: Any, run_id: str) -> Any:
        root = cast(str, authority.runtime_scratch_root)
        self.trace.append("runtime-scratch-prepare")
        model_directories = {model_id: f"{root}/{run_id}/{model_id}" for model_id in MODEL_ORDER}
        pre_censuses = {
            model_id: self._scratch_census(run_id, model_id, "PRE") for model_id in MODEL_ORDER
        }
        return gpu_live_smoke_module._RuntimeScratch(
            run_id=run_id,
            run_root=f"{root}/{run_id}",
            model_directories=model_directories,
            pre_censuses=pre_censuses,
        )

    def inspect_runtime_scratch(
        self,
        authority: Any,
        run_id: str,
        model_id: str,
    ) -> dict[str, JsonValue]:
        del authority
        self.trace.append(f"runtime-scratch-post:{model_id}")
        return self._scratch_census(run_id, model_id, "POST")

    def inspect_launcher_scratch(
        self,
        authority: Any,
        run_id: str,
        phase: str,
    ) -> dict[str, JsonValue]:
        del authority
        self.trace.append(f"launcher-scratch:{phase}")
        return self._scratch_census(run_id, "LAUNCHER", phase)

    @staticmethod
    def _scratch_census(
        run_id: str,
        model_id: str,
        phase: str,
    ) -> dict[str, JsonValue]:
        return {
            "schema_version": "mobileworld.g1.gpu-live-smoke-runtime-scratch-census/v1",
            "run_id": run_id,
            "model_id": model_id,
            "phase": phase,
            "model_scratch_root": f"/synthetic/runtime-scratch/{run_id}/{model_id}",
            "entry_count": 7,
            "regular_file_byte_count": 0,
            "entries_sha256": _canonical_sha256([]),
            "entries": [],
            "symlink_count": 0,
            "foreign_owner_entry_count": 0,
        }

    def server_environment(
        self,
        authority: Any,
        model_scratch_root: str,
    ) -> dict[str, str]:
        namespace = cast(dict[str, JsonValue], authority.network_namespace)
        environment = {
            "PATH": "/usr/bin:/bin",
            "CUDA_VISIBLE_DEVICES": GPU0_UUID,
            "LD_LIBRARY_PATH": cast(
                str,
                cast(dict[str, JsonValue], namespace["launcher_environment"])["LD_LIBRARY_PATH"],
            ),
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "VLLM_USE_MODELSCOPE": "False",
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONNOUSERSITE": "1",
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "VLLM_HOST_IP": "127.0.0.1",
            "NCCL_SOCKET_IFNAME": "lo",
            "GLOO_SOCKET_IFNAME": "lo",
        }
        for key, name in gpu_live_smoke_module._SERVER_SCRATCH_DIRECTORY_NAMES.items():
            environment[key] = f"{model_scratch_root}/{name}"
        self.trace.append(f"server-environment:{model_scratch_root}")
        return environment

    def verify_runtime_bindings(self, authority: Any) -> dict[str, JsonValue]:
        del authority
        self.trace.append("runtime")
        return {
            "client": {"openai_version": "1.106.1"},
            "server": {
                "openai_version": "2.15.0",
                "vllm_version": "0.11.0",
                "torch_version": "2.8.0+cu126",
            },
            "synthetic_cpu_fake": True,
        }

    def verify_runtime_trees_post(
        self,
        authority: Any,
        *,
        phase: str,
    ) -> dict[str, JsonValue]:
        del authority
        assert phase in {"PASS_POSTFLIGHT", "FAIL_POSTFLIGHT"}
        self.trace.append(f"runtime-tree:{phase}")
        return {
            "schema_version": "mobileworld.g1.gpu-live-smoke-runtime-tree-census/v1",
            "phase": phase,
            "authority_aggregate_match": True,
            "pre_post_digest_required": True,
            "same_owner_in_place_mutation_residual_disclosed": True,
            "native_dt_needed_dependency_closure_proven": False,
            "toctou_free_runtime_binding_proven": False,
            "formal_execution_closure_proven": False,
            "synthetic_cpu_fake": True,
        }

    def verify_snapshot(
        self,
        authority: Any,
        receipt: Any,
        model_id: str,
    ) -> dict[str, JsonValue]:
        del authority, receipt
        self.snapshot_counts[model_id] += 1
        ordinal = self.snapshot_counts[model_id]
        self.trace.append(f"snapshot:{model_id}:{ordinal}")
        if ordinal == 2:
            assert model_id in self.stopped_models
            assert model_id in self.release_verified_models
            assert model_id in self.release_port_verified_models
            self.post_snapshot_models.add(model_id)
        return {
            "model_id": model_id,
            "snapshot_tree_sha256": "8" * 64,
            "entry_count": 3,
            "snapshot_tree_byte_count": 3,
            "entries": [],
            "formal_model_immutability_proven": False,
            "toctou_free_model_binding_proven": False,
        }

    def inspect_gpu(self, authority: Any) -> dict[str, JsonValue]:
        del authority
        self.capacity_count += 1
        self.trace.append(f"capacity:{self.capacity_count}")
        return {
            "physical_index": 0,
            "uuid": GPU0_UUID,
            "free_memory_bytes": 70_000 * 1024 * 1024,
            "minimum_free_memory_bytes": 68_719_476_736,
            "shared": True,
            "exclusive": False,
            "foreign_processes_signaled": 0,
        }

    def inspect_gpu_processes(
        self,
        authority: Any,
        *,
        owned_pids: set[int],
    ) -> dict[str, JsonValue]:
        del authority
        active_owned = sorted(owned_pids.intersection(self.active_pids))
        self.trace.append(
            "gpu-process:owned="
            + ",".join(str(pid) for pid in sorted(owned_pids))
            + ":active="
            + ",".join(str(pid) for pid in active_owned)
        )
        processes: list[JsonValue] = [
            {
                "pid": self.foreign_pid,
                "uid": os.getuid() + 1,
                "starttime_ticks": 510_000,
                "used_gpu_memory_bytes": 1024,
                "classification": "BASELINE_OR_FOREIGN",
            }
        ]
        processes.extend(
            {
                "pid": pid,
                "uid": os.getuid(),
                "starttime_ticks": pid * 10,
                "used_gpu_memory_bytes": 2048,
                "classification": "OWN_LAUNCH",
            }
            for pid in active_owned
        )
        return {
            "gpu_uuid": GPU0_UUID,
            "processes": processes,
            "process_count": len(processes),
            "foreign_cmdlines_read": 0,
            "foreign_environments_read": 0,
            "signals_sent": 0,
        }

    def assert_gpu_isolation(
        self,
        baseline: dict[str, JsonValue],
        current: dict[str, JsonValue],
        *,
        owned_pids: set[int],
        require_owned_absent: bool,
    ) -> dict[str, JsonValue]:
        self.trace.append(
            "isolation:owned="
            + ",".join(str(pid) for pid in sorted(owned_pids))
            + f":absent={require_owned_absent}"
        )
        result = gpu_live_smoke_module._assert_gpu_service_isolation(
            baseline,
            current,
            owned_pids=owned_pids,
            require_owned_absent=require_owned_absent,
        )
        if require_owned_absent and owned_pids:
            models = {self.model_by_pid[pid] for pid in owned_pids if pid in self.model_by_pid}
            self.release_verified_models.update(models)
        return result

    def assert_port_free(self) -> None:
        self.port_check_count += 1
        self.trace.append(f"port-free:{self.port_check_count}")
        self.release_port_verified_models.update(self.stopped_models)

    def validate_immediate_launch_preflight(
        self,
        authority: Any,
        baseline_gpu_processes: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Mirror every shared-GPU guard immediately before fake Popen."""

        self.assert_authority_active(authority)
        capacity = self.inspect_gpu(authority)
        gpu_processes = self.inspect_gpu_processes(authority, owned_pids=set())
        isolation = self.assert_gpu_isolation(
            baseline_gpu_processes,
            gpu_processes,
            owned_pids=set(),
            require_owned_absent=True,
        )
        self.assert_port_free()
        self.assert_authority_active(authority)
        return {
            "schema_version": ("mobileworld.g1.gpu-live-smoke-immediate-launch-preflight/v1"),
            "capacity": capacity,
            "gpu_processes": gpu_processes,
            "gpu_isolation": isolation,
            "port_free": True,
            "authority_active_at_launch_boundary": True,
            "model_process_started": False,
        }

    def start_server(
        self,
        authority: Any,
        plan: Any,
        log_handle: Any,
        model_scratch_root: str,
        environment: dict[str, str],
    ) -> tuple[Any, Any]:
        del model_scratch_root
        model_id = cast(str, plan.model_id)
        if model_id == "qwen3vl_8b":
            assert self.capacity_count >= 2
        else:
            assert self.capacity_count >= 3
            assert "qwen3vl_8b" in self.stopped_models
            assert "qwen3vl_8b" in self.release_verified_models
            assert "qwen3vl_8b" in self.release_port_verified_models
            assert "qwen3vl_8b" in self.post_snapshot_models
        self.trace.append(f"start:{model_id}")
        root_pid = self.pid_by_model[model_id]
        served_name = MODEL_SERVED_NAMES[model_id]
        snapshot_path = cast(str, plan.snapshot_path)
        server = cast(dict[str, JsonValue], authority.value["server_runtime"])
        argv = (
            cast(str, server["python_path"]),
            *gpu_live_smoke_module._ISOLATED_PYTHON_FLAGS,
            "-c",
            gpu_live_smoke_module._server_bootstrap_code(cast(str, server["site_packages_path"])),
            *plan.argv[1:],
        )
        root = _process_identity(
            pid=root_pid,
            pgid=root_pid,
            sid=root_pid,
            starttime_ticks=root_pid * 10,
            argv=argv,
        )
        self.active_pids = {root_pid, root_pid + 1}
        log_handle.write(f"synthetic {model_id} log\n".encode())
        guard = gpu_live_smoke_module.ProcessOwnershipGuard(
            root=root,
            model_id=model_id,
            snapshot_path=snapshot_path,
            served_name=served_name,
            host="127.0.0.1",
            port=18007,
            expected_argv=argv,
            environment_sha256=_canonical_sha256(cast(JsonValue, environment)),
            gpu_uuid=GPU0_UUID,
        )
        return _FakeProcess(root_pid), replace(guard, service_tree=(root,))

    def bind_service_tree(self, guard: Any) -> Any:
        self.trace.append(f"bind:{guard.model_id}")
        if len(guard.service_tree) > 1:
            return guard
        root = guard.root
        child = _process_identity(
            pid=root.pid + 1,
            ppid=root.pid,
            pgid=root.pgid,
            sid=root.sid,
            starttime_ticks=root.starttime_ticks + 1,
            argv=root.argv,
        )
        return replace(guard, service_tree=(root, child))

    def wait_server_ready(
        self,
        authority: Any,
        process: Any,
        guard: Any,
    ) -> dict[str, JsonValue]:
        del authority
        assert process.pid == guard.root.pid
        assert guard.service_tree
        self.trace.append(f"ready:{guard.model_id}")
        return {
            "served_model_name": guard.served_name,
            "listener_owned": True,
            "owned_process_count": len(guard.service_tree),
        }

    def assert_listener_owned(self, guard: Any) -> tuple[Any, ...]:
        assert guard.service_tree
        self.trace.append(f"listener:{guard.model_id}")
        return cast(tuple[Any, ...], guard.service_tree)

    def inspect_owned_inet_sockets(self, guard: Any) -> dict[str, JsonValue]:
        assert guard.service_tree
        self.socket_census_count += 1
        self.trace.append(f"socket-census:{guard.model_id}:{self.socket_census_count}")
        return {
            "owned_pids": sorted(item.pid for item in guard.service_tree),
            "inet_sockets": [
                {
                    "protocol": "tcp4",
                    "state_hex": "0A",
                    "local_port": 18007,
                    "remote_port": 0,
                    "socket_inode": str(70_000 + self.socket_census_count),
                    "local_scope": "LOOPBACK",
                    "remote_scope": "UNSPECIFIED",
                    "loopback_only": True,
                }
            ],
            "inet_socket_count": 1,
            "non_loopback_inet_socket_count": 0,
            "foreign_process_fds_read": 0,
        }

    def invoke_call(
        self,
        authority: Any,
        descriptor: Any,
    ) -> dict[str, JsonValue]:
        del authority
        self.invoke_count += 1
        self.trace.append(
            f"invoke:{self.invoke_count}:{descriptor.model_id}:{descriptor.replay_seed}"
        )
        if self.fail_at_call == self.invoke_count:
            error = GpuLiveSmokeError(
                "GPU_SMOKE_PROVIDER_CALL_FAILED",
                "CPU fake requested a single terminal call failure",
            )
            error.application_visible_attempt_count = 1
            error.physical_request_count = 1
            error.physical_request_count_upper_bound = 1
            error.execution_detail = {
                "request_observations": [
                    {
                        "ordinal": 1,
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": 18007,
                        "path": "/v1/chat/completions",
                        "query_recorded": False,
                        "headers_recorded": False,
                        "exact_loopback_allowed": True,
                    }
                ],
                "non_loopback_connection_count": 0,
            }
            raise error
        content = f"synthetic:{descriptor.model_id}:seed={descriptor.replay_seed}"
        raw = canonical_json_bytes(
            {
                "synthetic_non_case": True,
                "ordinal": self.invoke_count,
                "content_sha256": _sha256(content.encode()),
            }
        )
        return {
            "status_code": 200,
            "latency_ns": self.invoke_count,
            "raw_response": raw,
            "raw_response_sha256": _sha256(raw),
            "content": content,
            "content_sha256": _sha256(content.encode()),
            "sdk_hidden_retries": 0,
            "application_visible_attempt_count": 1,
            "physical_request_count": 1,
            "request_observations": [
                {
                    "ordinal": 1,
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": 18007,
                    "path": "/v1/chat/completions",
                    "query_recorded": False,
                    "headers_recorded": False,
                    "exact_loopback_allowed": True,
                }
            ],
        }

    def parse_inert(self, model_id: str, content: str) -> dict[str, JsonValue]:
        self.trace.append(f"parse:{model_id}")
        return {
            "classification": "HOST_PARSEABLE_INERT_ACTION",
            "error_class": None,
            "parsed_action": {
                "synthetic_non_case": True,
                "content_sha256": _sha256(content.encode()),
            },
            "generated_action_executed": False,
        }

    def stop_service(self, guard: Any, process: Any) -> dict[str, JsonValue]:
        assert process.pid == guard.root.pid
        assert guard.service_tree
        self.trace.append(f"stop:{guard.model_id}")
        pids = sorted((item.pid for item in guard.service_tree), reverse=True)
        self.active_pids.clear()
        self.stopped_models.add(guard.model_id)
        signal_trace: list[JsonValue] = []
        for pid in pids:
            starttime_ticks = next(
                item.starttime_ticks for item in guard.service_tree if item.pid == pid
            )
            for state in ("INTENDED", "PIDFD_OPENED", "IDENTITY_REVALIDATED", "SENT"):
                signal_trace.append(
                    {
                        "sequence": len(signal_trace) + 1,
                        "pid": pid,
                        "starttime_ticks": starttime_ticks,
                        "signal": "SIGTERM",
                        "state": state,
                        "signal_api": "PIDFD",
                        "ownership": "RECORDED_OWN",
                    }
                )
        return {
            "term_pids": pids,
            "kill_pids": [],
            "foreign_processes_signaled": 0,
            "broad_signal_used": False,
            "port_released": True,
            "process_group_released": True,
            "process_session_released": True,
            "signal_trace": signal_trace,
        }


class _FailedLaunchGpuLiveOperations(_FakeGpuLiveOperations):
    def __init__(self, event_order: list[str]) -> None:
        super().__init__()
        self.event_order = event_order
        self.failed_process = _FakeProcess(self.pid_by_model["qwen3vl_8b"])
        self.close_count = 0

    def start_server(
        self,
        authority: Any,
        plan: Any,
        log_handle: Any,
        model_scratch_root: str,
        environment: dict[str, str],
    ) -> tuple[Any, Any]:
        del log_handle, model_scratch_root
        model_id = cast(str, plan.model_id)
        assert model_id == "qwen3vl_8b"
        pid = self.failed_process.pid
        server = cast(dict[str, JsonValue], authority.value["server_runtime"])
        argv = (
            cast(str, server["python_path"]),
            *gpu_live_smoke_module._ISOLATED_PYTHON_FLAGS,
            "-c",
            gpu_live_smoke_module._server_bootstrap_code(cast(str, server["site_packages_path"])),
            *plan.argv[1:],
        )
        root = _process_identity(
            pid=pid,
            ppid=os.getpid(),
            pgid=pid,
            sid=pid,
            starttime_ticks=pid * 10,
            argv=argv,
        )
        minimal = gpu_live_smoke_module._MinimalDirectChildIdentity(
            uid=root.uid,
            pid=root.pid,
            ppid=root.ppid,
            pgid=root.pgid,
            sid=root.sid,
            starttime_ticks=root.starttime_ticks,
        )
        handle = gpu_live_smoke_module._FailedLaunchCleanupHandle(
            process=cast(Any, self.failed_process),
            acquisition_pidfd=91,
            root_minimal=minimal,
            recorded_tree=(root,),
            model_id=model_id,
            expected_argv=argv,
            gpu_uuid=GPU0_UUID,
            host="127.0.0.1",
            port=18007,
            environment_sha256=_canonical_sha256(cast(JsonValue, environment)),
            evidence_frozen=False,
        )
        error = GpuLiveSmokeError(
            "GPU_SMOKE_SERVER_GUARD_CAPTURE_FAILED",
            "synthetic provisional acquisition",
        )
        error.failed_launch_cleanup_handle = handle
        raise error

    def freeze_failed_launch_acquisition(
        self,
        authority: Any,
        handle: Any,
    ) -> tuple[Any, dict[str, JsonValue]]:
        del authority
        self.event_order.append("FREEZE_RETURNED")
        frozen = replace(handle, evidence_frozen=True)
        return frozen, {
            "schema_version": "mobileworld.g1.gpu-live-smoke-provisional-acquisition/v1",
            "model_id": handle.model_id,
            "recorded_tree": [item.to_dict() for item in handle.recorded_tree],
            "recorded_tree_count": len(handle.recorded_tree),
            "live_recorded_member_count": len(handle.recorded_tree),
            "root_live_at_freeze": True,
            "root_exited_before_freeze": False,
            "service_launched": False,
            "synthetic_cpu_fake": True,
        }

    def cleanup_failed_launch(
        self,
        authority: Any,
        handle: Any,
    ) -> dict[str, JsonValue]:
        del authority
        assert handle.evidence_frozen is True
        assert self.event_order[-1] == "EVENT:PROVISIONAL_ACQUISITION_FROZEN"
        self.event_order.append("SIGNAL")
        root = handle.recorded_tree[0]
        self.active_pids.discard(root.pid)
        self.stopped_models.add(handle.model_id)
        self.release_verified_models.add(handle.model_id)
        self.release_port_verified_models.add(handle.model_id)
        signal_trace: list[JsonValue] = []
        for state in ("INTENDED", "PIDFD_OPENED", "IDENTITY_REVALIDATED", "SENT"):
            signal_trace.append(
                {
                    "sequence": len(signal_trace) + 1,
                    "pid": root.pid,
                    "starttime_ticks": root.starttime_ticks,
                    "signal": "SIGTERM",
                    "state": state,
                    "signal_api": "PIDFD",
                    "ownership": "RECORDED_OWN",
                }
            )
        return {
            "term_pids": [root.pid],
            "kill_pids": [],
            "direct_child_already_exited": False,
            "foreign_processes_signaled": 0,
            "port_released": True,
            "process_session_released": True,
            "gpu_allocation_released": True,
            "signal_trace": signal_trace,
        }

    def close_failed_launch_acquisition(self, handle: Any) -> None:
        assert handle.evidence_frozen is True
        self.close_count += 1


class _UnfrozenFailedLaunchGpuLiveOperations(_FailedLaunchGpuLiveOperations):
    def __init__(self, event_order: list[str]) -> None:
        super().__init__(event_order)
        self.freeze_count = 0
        self.cleanup_count = 0

    def freeze_failed_launch_acquisition(
        self,
        authority: Any,
        handle: Any,
    ) -> tuple[Any, dict[str, JsonValue]]:
        del authority, handle
        self.freeze_count += 1
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "synthetic freeze refusal",
        )

    def cleanup_failed_launch(
        self,
        authority: Any,
        handle: Any,
    ) -> dict[str, JsonValue]:
        del authority, handle
        self.cleanup_count += 1
        raise AssertionError("cleanup must never run without persisted frozen evidence")

    def close_failed_launch_acquisition(self, handle: Any) -> None:
        del handle
        self.close_count += 1


def _read_content_ref(
    root: Path,
    reference: JsonValue,
    *,
    schema_name: str | None = None,
) -> dict[str, JsonValue]:
    value = cast(dict[str, JsonValue], reference)
    data = (root / cast(str, value["relative_path"])).read_bytes()
    assert _sha256(data) == value["sha256"]
    assert len(data) == value["byte_count"]
    parsed = json.loads(data)
    assert isinstance(parsed, dict)
    assert canonical_json_bytes(cast(JsonValue, parsed)) == data
    if schema_name is not None:
        _schema_validator(schema_name).validate(parsed)
    return cast(dict[str, JsonValue], parsed)


def _assert_schema_validation_receipt(
    evidence_root: Path,
    terminal: dict[str, JsonValue],
    *,
    status: str,
    event_count: int,
    call_count: int,
    lifecycle_count: int,
) -> None:
    receipt = _read_content_ref(
        evidence_root,
        terminal["schema_validation"],
    )
    assert set(receipt) == {
        "schema_version",
        "run_id",
        "status",
        "schemas",
        "schema_count",
        "authority_valid",
        "packet_valid",
        "preparation_valid",
        "event_count_validated",
        "call_count_validated",
        "lifecycle_count_validated",
        "owned_command_receipt_count_validated",
        "owned_command_receipts_validated",
        "manifest_validated_before_terminal_publish",
        "stored_execution_validated_before_terminal_publish",
        "actual_content_bytes_read",
        "manifest_self_exclusion_rule_validated",
        "run_directory_exact_census_required_after_publish",
        "validation_failure_is_terminal",
    }
    assert receipt["schema_version"] == ("mobileworld.g1.gpu-live-smoke-schema-validation/v1")
    assert receipt["run_id"] == terminal["run_id"]
    assert receipt["status"] == status
    assert receipt["schema_count"] == 10
    assert receipt["event_count_validated"] == event_count
    assert receipt["call_count_validated"] == call_count
    assert receipt["lifecycle_count_validated"] == lifecycle_count
    operation_ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    owned_command_refs = cast(
        list[JsonValue],
        operation_ledger["owned_command_receipts"],
    )
    assert receipt["owned_command_receipt_count_validated"] == len(owned_command_refs)
    assert receipt["owned_command_receipts_validated"] == owned_command_refs
    assert operation_ledger["owned_command_receipt_count"] == len(owned_command_refs)
    for reference in owned_command_refs:
        _read_content_ref(
            evidence_root,
            reference,
            schema_name="gpu_smoke_owned_command.schema.json",
        )
    for key in (
        "authority_valid",
        "packet_valid",
        "preparation_valid",
        "manifest_validated_before_terminal_publish",
        "stored_execution_validated_before_terminal_publish",
        "actual_content_bytes_read",
        "manifest_self_exclusion_rule_validated",
        "run_directory_exact_census_required_after_publish",
        "validation_failure_is_terminal",
    ):
        assert receipt[key] is True
    expected_names = [
        "authority",
        "packet",
        "preparation",
        "event",
        "call",
        "lifecycle",
        "owned_command",
        "manifest",
        "execution",
        "error",
    ]
    schemas = cast(list[dict[str, JsonValue]], receipt["schemas"])
    assert [item["name"] for item in schemas] == expected_names
    for item in schemas:
        assert set(item) == {"name", "relative_path", "sha256", "byte_count"}
        path = REPOSITORY_ROOT / cast(str, item["relative_path"])
        data = path.read_bytes()
        assert path.parent == GPU_SMOKE_SCHEMA_ROOT
        assert _sha256(data) == item["sha256"]
        assert len(data) == item["byte_count"]


def _assert_pass_operation_evidence(
    evidence_root: Path,
    terminal: dict[str, JsonValue],
) -> None:
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    signal_receipt = _read_content_ref(evidence_root, ledger["signal_trace"])
    assert signal_receipt["schema_version"] == ("mobileworld.g1.gpu-live-smoke-signal-trace/v1")
    assert signal_receipt["run_id"] == terminal["run_id"]
    for key in (
        "signal_target_pids",
        "signal_sent_pids",
        "signal_intent_count",
        "signal_sent_count",
        "foreign_process_target_count",
        "broad_process_signal_count",
    ):
        assert signal_receipt[key] == ledger[key]
    assert signal_receipt["pidfd_only"] is True
    signal_events = cast(list[dict[str, JsonValue]], signal_receipt["events"])
    assert [item["global_sequence"] for item in signal_events] == list(
        range(1, len(signal_events) + 1)
    )
    assert all(
        item["signal_api"] == "PIDFD"
        and item["ownership"] == "RECORDED_OWN"
        and cast(str, item["cleanup_attempt"]).endswith("-primary")
        for item in signal_events
    )

    network_receipt = _read_content_ref(
        evidence_root,
        ledger["network_observations"],
    )
    assert network_receipt["schema_version"] == (
        "mobileworld.g1.gpu-live-smoke-network-observations/v1"
    )
    assert network_receipt["run_id"] == terminal["run_id"]
    assert network_receipt["request_observation_count"] == 22
    assert network_receipt["exact_loopback_request_count"] == 22
    assert network_receipt["non_loopback_connection_count"] == 0
    network_observations = cast(
        list[dict[str, JsonValue]],
        network_receipt["observations"],
    )
    assert [item["global_sequence"] for item in network_observations] == list(range(1, 23))
    assert all(
        item["scheme"] == "http"
        and item["host"] == "127.0.0.1"
        and item["port"] == 18007
        and item["path"] == "/v1/chat/completions"
        and item["query_recorded"] is False
        and item["headers_recorded"] is False
        and item["exact_loopback_allowed"] is True
        for item in network_observations
    )

    socket_receipt = _read_content_ref(
        evidence_root,
        ledger["socket_observations"],
    )
    assert socket_receipt["schema_version"] == (
        "mobileworld.g1.gpu-live-smoke-socket-observations/v1"
    )
    assert socket_receipt["run_id"] == terminal["run_id"]
    assert socket_receipt["socket_observation_count"] == 46
    assert socket_receipt["non_loopback_inet_socket_count"] == 0
    socket_observations = cast(
        list[dict[str, JsonValue]],
        socket_receipt["observations"],
    )
    assert [item["ordinal"] for item in socket_observations] == list(range(1, 47))
    assert [item["phase"] for item in socket_observations].count("SERVICE_READY") == 2
    assert [item["phase"] for item in socket_observations].count("BEFORE_CALL") == 22
    assert [item["phase"] for item in socket_observations].count("AFTER_CALL") == 22
    for item in socket_observations:
        census = cast(dict[str, JsonValue], item["census"])
        assert census["foreign_process_fds_read"] == 0
        assert census["non_loopback_inet_socket_count"] == 0
        for row in cast(list[dict[str, JsonValue]], census["inet_sockets"]):
            assert row["local_scope"] == "LOOPBACK"
            assert row["remote_scope"] == "UNSPECIFIED"
            assert row["loopback_only"] is True

    credential_receipts = [
        _read_content_ref(evidence_root, reference)
        for reference in cast(list[JsonValue], ledger["credential_scan_receipts"])
    ]
    assert len(credential_receipts) == 24
    assert [item["artifact_kind"] for item in credential_receipts].count(
        "RAW_PROVIDER_RESPONSE"
    ) == 22
    assert [item["artifact_kind"] for item in credential_receipts].count("SERVER_LOG") == 2
    assert all(
        item["schema_version"] == "mobileworld.g1.gpu-live-smoke-credential-scan/v1"
        and item["pattern_set"]
        == ["bearer", "openai_key", "aws_access_key", "credential_assignment"]
        and item["match_count"] == 0
        and item["secret_material_persisted"] is False
        for item in credential_receipts
    )


def _assert_error_code(
    code: str,
    call: Callable[[], object],
) -> GpuLiveSmokeError:
    with pytest.raises(GpuLiveSmokeError) as raised:
        call()
    assert raised.value.code == code
    return raised.value


def _synthetic_owned_command_receipt(
    command: list[str],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> dict[str, JsonValue]:
    gate_argv = ["/synthetic/private/bin/python3.12", "-c", "synthetic-owned-gate"]
    gate_identity: dict[str, JsonValue] = {
        "uid": os.getuid(),
        "pid": 61_000,
        "ppid": os.getpid(),
        "pgid": 61_000,
        "sid": 61_000,
        "starttime_ticks": 610_000,
        "executable_path": gate_argv[0],
        "executable_sha256": "a" * 64,
        "argv": gate_argv,
        "argv_sha256": _canonical_sha256(gate_argv),
    }
    return {
        "schema_version": "mobileworld.g1.gpu-live-smoke-owned-command/v1",
        "command": command,
        "command_sha256": _canonical_sha256(command),
        "executable_path": command[0],
        "timeout_milliseconds": 15_000,
        "stdout_byte_cap": 4 * 1024 * 1024,
        "stderr_byte_cap": 4 * 1024 * 1024,
        "stdout_byte_count": len(stdout),
        "stderr_byte_count": len(stderr),
        "stdout_sha256": _sha256(stdout),
        "stderr_sha256": _sha256(stderr),
        "initial_pidfd_acquired": True,
        "launch_gate_identity": gate_identity,
        "launch_gate_code_sha256": _sha256(
            gpu_live_smoke_module._OWNED_COMMAND_GATE_CODE.encode("utf-8")
        ),
        "launch_gate_released_after_identity_proof": True,
        "target_executable_path": command[0],
        "target_executable_sha256": "b" * 64,
        "descendant_policy": "FORBIDDEN",
        "observed_descendant_count": 0,
        "observed_descendant_identities": [],
        "descendant_census_max_interval_milliseconds": 10,
        "completion_reason": "EXITED",
        "returncode": returncode,
        "signal_trace": [],
        "release_proven": True,
        "numeric_pid_signal_count": 0,
        "popen_kill_count": 0,
        "popen_send_signal_count": 0,
        "communicate_timeout_count": 0,
    }


def _synthetic_owned_command_result(
    command: list[str],
    *,
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> Any:
    return gpu_live_smoke_module._OwnedCommandResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        receipt=_synthetic_owned_command_receipt(
            command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
        ),
    )


def _mock_network_namespace_probe(
    monkeypatch: pytest.MonkeyPatch,
    authority: Any,
) -> dict[str, str]:
    namespace = cast(dict[str, JsonValue], authority.network_namespace)
    expected_environment = cast(
        dict[str, str],
        copy.deepcopy(namespace["launcher_environment"]),
    )
    ipv6_unreachable = (
        f"{'0' * 32} 00 {'0' * 32} 00 {'0' * 32} ffffffff 00000001 00000000 00200200 lo"
    )
    ipv6_loopback = (
        f"{'0' * 31}1 80 {'0' * 32} 00 {'0' * 32} 00000000 00000001 00000000 80200001 lo"
    )
    readings = {
        "/proc/self/status": (
            "CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
            "CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n"
            "CapAmb:\t0000000000000000\nNoNewPrivs:\t1\nGroups:\t\n"
        ),
        "/proc/self/uid_map": f"{namespace['uid_map_line']}\n",
        "/proc/self/gid_map": f"{namespace['gid_map_line']}\n",
        "/proc/net/dev": (
            "Inter-| Receive | Transmit\n face |bytes|bytes\n"
            "    lo: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        ),
        "/proc/net/route": "Iface Destination Gateway Flags RefCnt Use Metric Mask\n",
        "/proc/net/ipv6_route": (f"{ipv6_unreachable}\n{ipv6_unreachable}\n{ipv6_loopback}\n"),
    }
    monkeypatch.setattr(gpu_live_smoke_module.os, "getuid", lambda: 0)
    monkeypatch.setattr(gpu_live_smoke_module.os, "getgid", lambda: 0)
    monkeypatch.setattr(gpu_live_smoke_module.os, "getgroups", lambda: [])
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "sys",
        SimpleNamespace(
            flags=SimpleNamespace(
                isolated=1,
                ignore_environment=1,
                no_site=1,
                dont_write_bytecode=1,
            ),
            pycache_prefix="/dev/null",
            modules={},
        ),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "environ",
        expected_environment.copy(),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "readlink",
        lambda path: {
            "/proc/self/ns/user": "user:[4026533001]",
            "/proc/self/ns/net": "net:[4026533002]",
        }[cast(str, path)],
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_initial_environment",
        lambda: expected_environment.copy(),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_runner_file_descriptor_census",
        lambda: {
            "schema_version": "mobileworld.g1.gpu-live-smoke-fd-census/v1",
            "open_fd_numbers": [0, 1, 2],
            "descriptors": [
                {
                    "fd": fd,
                    "descriptor_type": "FIFO",
                    "socket": False,
                    "inet_socket": False,
                }
                for fd in (0, 1, 2)
            ],
            "open_fd_count": 3,
            "open_fd_count_above_stderr": 0,
            "standard_fd_socket_count": 0,
            "standard_fd_inet_socket_count": 0,
            "all_fds_above_stderr_closed": True,
            "standard_fds_non_inet": True,
            "foreign_process_fds_read": 0,
        },
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_small_text",
        lambda path: readings[path],
    )

    binary_hashes = {
        cast(str, namespace["env_path"]): cast(str, namespace["env_sha256"]),
        cast(str, namespace["unshare_path"]): cast(str, namespace["unshare_sha256"]),
        cast(str, namespace["ip_path"]): cast(str, namespace["ip_sha256"]),
        cast(str, namespace["setpriv_path"]): cast(str, namespace["setpriv_sha256"]),
        cast(str, namespace["nvidia_smi_path"]): cast(
            str,
            namespace["nvidia_smi_sha256"],
        ),
    }

    def fake_hash(path: str, *, expected_sha256: str) -> tuple[str, int]:
        assert binary_hashes[path] == expected_sha256
        byte_count = (
            cast(int, namespace["nvidia_smi_byte_count"])
            if path == namespace["nvidia_smi_path"]
            else 123
        )
        return expected_sha256, byte_count

    monkeypatch.setattr(gpu_live_smoke_module, "_hash_regular_file", fake_hash)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_loopback_self_connect_receipt",
        lambda: {
            "address": "127.0.0.1",
            "protocol": "TCP",
            "self_connect_succeeded": True,
            "external_endpoint_contacted": False,
        },
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_scratch_census",
        lambda *args, **kwargs: {
            "entry_count": len(gpu_live_smoke_module._SERVER_SCRATCH_DIRECTORY_NAMES),
            "regular_file_byte_count": 0,
            "entries": [],
        },
    )
    return readings


@pytest.mark.parametrize(
    "schema_path",
    sorted(GPU_SMOKE_SCHEMA_ROOT.glob("*.schema.json")),
    ids=lambda path: cast(Path, path).name,
)
def test_gpu_smoke_schemas_are_valid_draft_2020_12(schema_path: Path) -> None:
    schema = json.loads(schema_path.read_bytes())
    Draft202012Validator.check_schema(schema)


def test_error_schema_exactly_matches_production_runner_and_cli_vocabulary() -> None:
    emitted = set(
        re.findall(
            r"GPU_SMOKE_[A-Z0-9_]+",
            (REPOSITORY_ROOT / "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py").read_text(
                encoding="utf-8"
            )
            + GPU_SMOKE_RUNNER_CLI.read_text(encoding="utf-8"),
        )
    )
    emitted.remove("GPU_SMOKE_OUTER_FD_CLOSURE_SHA256")
    schema = json.loads((GPU_SMOKE_SCHEMA_ROOT / "gpu_smoke_error.schema.json").read_bytes())
    assert schema["enum"] == sorted(emitted)
    assert len(emitted) == 128


def test_event_schema_exactly_matches_every_production_event_emission() -> None:
    source = (REPOSITORY_ROOT / "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py").read_text(
        encoding="utf-8"
    )
    emitted = set(
        re.findall(
            r'store\.event\(\s*"([A-Z][A-Z0-9_]*)"',
            source,
        )
    )
    schema = json.loads((GPU_SMOKE_SCHEMA_ROOT / "gpu_smoke_event.schema.json").read_bytes())
    allowed = set(schema["properties"]["event_kind"]["enum"])
    assert emitted == allowed
    assert len(allowed) == 20


def test_authority_packet_and_preparation_match_published_schemas(
    tmp_path: Path,
) -> None:
    authority, packet = _loaded_inputs(tmp_path)
    preparation = prepare_gpu_live_smoke(authority, packet, MODEL_CONFIG_PATH)
    _schema_validator("gpu_smoke_authority.schema.json").validate(authority.value)
    _schema_validator("gpu_smoke_packet.schema.json").validate(packet.value)
    _schema_validator("gpu_smoke_preparation.schema.json").validate(preparation)


def test_fixture_freezes_secret_free_non_case_22_call_matrix() -> None:
    fixture = _load_fixture()
    assert {
        "synthetic_non_case": fixture["synthetic_non_case"],
        "secret_free": fixture["secret_free"],
        "formal_capsule": fixture["formal_capsule"],
        "contains_real_task_data": fixture["contains_real_task_data"],
        "intended_use": fixture["intended_use"],
        "g1_5_live_proof": fixture["g1_5_live_proof"],
        "handwritten_render_evidence_authoritative": fixture[
            "handwritten_render_evidence_authoritative"
        ],
    } == {
        "synthetic_non_case": True,
        "secret_free": True,
        "formal_capsule": False,
        "contains_real_task_data": False,
        "intended_use": "NEGATIVE_LOADER_MUTATION_ONLY",
        "g1_5_live_proof": False,
        "handwritten_render_evidence_authoritative": False,
    }
    calls = fixture["expected_calls"]
    assert len(calls) == 22
    expected_ids: list[str] = []
    for short_model in ("qwen", "mai"):
        for seed in REPLAY_SEEDS:
            for repeat in (1, 2):
                expected_ids.append(f"g14-{short_model}-s{seed}-r{repeat}")
        for arm in G1_5_ARMS:
            expected_ids.append(f"g15-{short_model}-{arm.lower().replace('_', '-')}-s1729")
    assert [call["call_id"] for call in calls] == expected_ids
    assert [call["model_id"] for call in calls[:11]] == ["qwen3vl_8b"] * 11
    assert [call["model_id"] for call in calls[11:]] == ["mai_ui_8b"] * 11
    assert sum(call["phase"] == "G1_4_CANARY" for call in calls) == 12
    assert sum(call["phase"] == "G1_5_CODEC" for call in calls) == 10
    serialized = canonical_json_bytes(cast(JsonValue, fixture)).lower()
    for forbidden in (
        b"api_key",
        b"authorization",
        b"password",
        b'formal_capsule":true',
        b'contains_real_task_data":true',
    ):
        assert forbidden not in serialized


def test_packet_loader_accepts_exact_matrix_and_content_bindings(tmp_path: Path) -> None:
    packet_value = _gpu_smoke_packet()
    packet_path, packet_sha = _write_canonical_json(tmp_path, "packet.json", packet_value)
    loaded = load_gpu_smoke_packet(packet_path, packet_sha, g1_5_seed=1729)

    assert loaded.sha256 == packet_sha
    assert len(loaded.calls) == 22
    assert [call.model_id for call in loaded.calls[:11]] == ["qwen3vl_8b"] * 11
    assert [call.model_id for call in loaded.calls[11:]] == ["mai_ui_8b"] * 11
    assert [call.phase for call in loaded.calls].count("G1_4_CANARY") == 12
    assert [call.phase for call in loaded.calls].count("G1_5_CODEC") == 10
    for model_id in MODEL_ORDER:
        model_calls = [
            call
            for call in loaded.calls
            if call.model_id == model_id and call.phase == "G1_5_CODEC"
        ]
        assert [call.value["arm"] for call in model_calls] == list(G1_5_ARMS)
        original = next(call for call in model_calls if call.value["arm"] == "ORIGINAL")
        source_sha = _sha256(original.application_request_bytes)
        assert {
            cast(dict[str, JsonValue], call.value["render_evidence"])[
                "source_application_request_sha256"
            ]
            for call in model_calls
        } == {source_sha}


def test_compiler_uses_frozen_real_codec_fixtures_for_all_five_arms() -> None:
    packet = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
        g1_5_seed=1729,
    )
    repeated = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
        g1_5_seed=1729,
    )
    assert repeated.canonical_bytes == packet.canonical_bytes
    assert repeated.sha256 == packet.sha256
    assert len(packet.calls) == 22
    assert packet.value["source_bindings"] == {
        "g1_5_cpu_publication_sha256": G1_5_CPU_PUBLICATION_SHA256,
        "compiler_contract": "mobileworld.g1.gpu-live-smoke-packet-compiler/v1",
        "fixtures": {
            "qwen3vl_8b": {
                "relative_path": (
                    "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
                    "qwen_flat_progress.captured.v1.json"
                ),
                "file_sha256": ("60f19821f782cd20ded8a926ea4466dff151cc5163a4728f9d0c761ae08b34be"),
                "fixture_id": "g15-qwen-flat-progress-captured-redacted-v1",
                "fixture_request_sha256": (
                    "72f1396204e56c05b49a2a8564650f915c780d9bfa32f455f1cef3320abd6a33"
                ),
            },
            "mai_ui_8b": {
                "relative_path": (
                    "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
                    "mai_raw_replay.captured.v1.json"
                ),
                "file_sha256": ("b9e025b0b4990e9e3fe259b7dd21e2919de6538c00b42f2936c7b9fe403e9b40"),
                "fixture_id": "g15-mai-raw-replay-captured-redacted-v1",
                "fixture_request_sha256": (
                    "c2ee086c6e5e659c4f904fbb74afefc9c89f7aabcfd47441ce65cce332d37a7d"
                ),
            },
        },
    }

    for model_id, fixture_path in (
        ("qwen3vl_8b", QWEN_CAPTURED_FIXTURE),
        ("mai_ui_8b", MAI_CAPTURED_FIXTURE),
    ):
        fixture = json.loads(fixture_path.read_bytes())
        calls = [
            call
            for call in packet.calls
            if call.model_id == model_id and call.phase == "G1_5_CODEC"
        ]
        assert [call.value["arm"] for call in calls] == list(G1_5_ARMS)
        for call in calls:
            arm = cast(str, call.value["arm"])
            request = copy.deepcopy(call.value["application_request"])
            assert isinstance(request, dict)
            request["model"] = fixture["application_request"]["model"]
            assert canonical_json_bytes(cast(JsonValue, request))
            assert (
                _canonical_sha256(cast(JsonValue, request))
                == fixture["expected_rendered_request_sha256"][arm]
            )
            evidence = cast(dict[str, JsonValue], call.value["render_evidence"])
            assert evidence["target_only_diff"] is True
            assert evidence["source_mapping_reversible"] is True
            assert evidence["provider_invocation_allowed"] is False
            assert evidence["rendered_application_request_sha256"] == _canonical_sha256(
                cast(JsonValue, call.value["application_request"])
            )
            assert evidence["diff_sha256"] == _canonical_sha256(call.value["diff"])
            assert evidence["mapping_sha256"] == _canonical_sha256(call.value["mapping"])
            diff = cast(list[JsonValue], call.value["diff"])
            if arm == "ORIGINAL":
                assert diff == []
            else:
                assert diff
            mapping = cast(dict[str, JsonValue], call.value["mapping"])
            assert isinstance(mapping["source_mappings"], list)


def test_compiler_calls_real_extract_render_pre_send_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts = {
        "qwen_extract": 0,
        "mai_extract": 0,
        "qwen_render": 0,
        "mai_render": 0,
        "validate_plan_set": 0,
        "validate_pre_send": 0,
        "restore_original": 0,
    }
    qwen_extract = QwenFlatProgressHistoryCodec.extract
    mai_extract = MaiRawReplayHistoryCodec.extract
    qwen_render = QwenFlatProgressHistoryCodec.render
    mai_render = MaiRawReplayHistoryCodec.render
    validate_plan_set = gpu_live_smoke_module.validate_plan_set
    validate_pre_send = gpu_live_smoke_module.validate_pre_send
    restore_original = gpu_live_smoke_module.restore_original

    def observed_qwen_extract(self: object, *args: object, **kwargs: object) -> object:
        counts["qwen_extract"] += 1
        return qwen_extract(self, *args, **kwargs)  # type: ignore[arg-type]

    def observed_mai_extract(self: object, *args: object, **kwargs: object) -> object:
        counts["mai_extract"] += 1
        return mai_extract(self, *args, **kwargs)  # type: ignore[arg-type]

    def observed_qwen_render(self: object, *args: object, **kwargs: object) -> object:
        counts["qwen_render"] += 1
        return qwen_render(self, *args, **kwargs)  # type: ignore[arg-type]

    def observed_mai_render(self: object, *args: object, **kwargs: object) -> object:
        counts["mai_render"] += 1
        return mai_render(self, *args, **kwargs)  # type: ignore[arg-type]

    def observed_validate_plan_set(*args: object, **kwargs: object) -> object:
        counts["validate_plan_set"] += 1
        return validate_plan_set(*args, **kwargs)  # type: ignore[arg-type]

    def observed_validate_pre_send(*args: object, **kwargs: object) -> object:
        counts["validate_pre_send"] += 1
        return validate_pre_send(*args, **kwargs)  # type: ignore[arg-type]

    def observed_restore_original(*args: object, **kwargs: object) -> object:
        counts["restore_original"] += 1
        return restore_original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(QwenFlatProgressHistoryCodec, "extract", observed_qwen_extract)
    monkeypatch.setattr(MaiRawReplayHistoryCodec, "extract", observed_mai_extract)
    monkeypatch.setattr(QwenFlatProgressHistoryCodec, "render", observed_qwen_render)
    monkeypatch.setattr(MaiRawReplayHistoryCodec, "render", observed_mai_render)
    monkeypatch.setattr(gpu_live_smoke_module, "validate_plan_set", observed_validate_plan_set)
    monkeypatch.setattr(gpu_live_smoke_module, "validate_pre_send", observed_validate_pre_send)
    monkeypatch.setattr(gpu_live_smoke_module, "restore_original", observed_restore_original)

    packet = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
    )
    assert len(packet.calls) == 22
    assert counts == {
        # The compiler extracts once, the paired-plan validator re-extracts,
        # and each independent pre-send validation re-extracts twice.
        "qwen_extract": 12,
        "mai_extract": 12,
        "qwen_render": 5,
        "mai_render": 5,
        "validate_plan_set": 2,
        "validate_pre_send": 10,
        "restore_original": 10,
    }


def test_compiler_derives_target_only_and_reversibility_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "restore_original",
        lambda _result: {"not": "the source request"},
    )
    _assert_error_code(
        "GPU_SMOKE_FIXTURE_RENDER_MISMATCH",
        lambda: compile_gpu_smoke_packet(
            QWEN_CAPTURED_FIXTURE,
            MAI_CAPTURED_FIXTURE,
        ),
    )

    monkeypatch.undo()
    validate_pre_send = gpu_live_smoke_module.validate_pre_send

    def without_target_only(*args: object, **kwargs: object) -> object:
        receipt = validate_pre_send(*args, **kwargs)  # type: ignore[arg-type]
        return replace(
            receipt,
            checks=tuple(item for item in receipt.checks if item != "independent_target_only_diff"),
        )

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "validate_pre_send",
        without_target_only,
    )
    _assert_error_code(
        "GPU_SMOKE_FIXTURE_RENDER_MISMATCH",
        lambda: compile_gpu_smoke_packet(
            QWEN_CAPTURED_FIXTURE,
            MAI_CAPTURED_FIXTURE,
        ),
    )


def test_compiled_packet_write_is_content_addressed_and_write_once(
    tmp_path: Path,
) -> None:
    packet = compile_gpu_smoke_packet(
        QWEN_CAPTURED_FIXTURE,
        MAI_CAPTURED_FIXTURE,
    )
    output_root = tmp_path / "repo-external-packet-root"
    first = write_gpu_smoke_packet(packet, output_root)
    second = write_gpu_smoke_packet(packet, output_root)
    assert first == second
    assert first["sha256"] == packet.sha256
    target = output_root / cast(str, first["relative_path"])
    assert target.read_bytes() == packet.canonical_bytes
    assert _sha256(target.read_bytes()) == packet.sha256

    target.write_bytes(b"foreign-collision")
    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_COLLISION",
        lambda: write_gpu_smoke_packet(packet, output_root),
    )


def test_compiler_rejects_fixture_path_and_byte_drift(tmp_path: Path) -> None:
    _assert_error_code(
        "GPU_SMOKE_FIXTURE_PATH_INVALID",
        lambda: compile_gpu_smoke_packet(
            tmp_path / "qwen.json",
            MAI_CAPTURED_FIXTURE,
        ),
    )

    copied = (
        tmp_path / "MobileWorld/tests/offline/fixtures/g1_5_history_codecs/"
        "qwen_flat_progress.captured.v1.json"
    )
    copied.parent.mkdir(parents=True)
    copied.write_bytes(QWEN_CAPTURED_FIXTURE.read_bytes() + b" ")
    _assert_error_code(
        "GPU_SMOKE_FIXTURE_HASH_MISMATCH",
        lambda: compile_gpu_smoke_packet(copied, MAI_CAPTURED_FIXTURE),
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        (
            lambda value: cast(list[JsonValue], value["calls"]).reverse(),
            "GPU_SMOKE_MATRIX_INVALID",
        ),
        (
            lambda value: cast(dict[str, JsonValue], value).__setitem__("unexpected", False),
            "GPU_SMOKE_CLOSED_SHAPE_INVALID",
        ),
        (
            lambda value: cast(
                dict[str, JsonValue],
                cast(list[JsonValue], value["calls"])[0],
            )["application_request"].__setitem__("api_key", "forbidden"),
            "GPU_SMOKE_SECRET_FIELD_FORBIDDEN",
        ),
    ),
)
def test_packet_loader_rejects_matrix_shape_and_secret_drift(
    tmp_path: Path,
    mutation: Callable[[dict[str, JsonValue]], object],
    expected_code: str,
) -> None:
    value = _gpu_smoke_packet()
    mutation(value)
    path, digest = _write_canonical_json(tmp_path, "bad-packet.json", value)
    _assert_error_code(
        expected_code,
        lambda: load_gpu_smoke_packet(path, digest, g1_5_seed=1729),
    )


def test_packet_loader_rejects_original_source_binding_drift(tmp_path: Path) -> None:
    value = _gpu_smoke_packet()
    calls = cast(list[JsonValue], value["calls"])
    g1_5_mask = cast(dict[str, JsonValue], calls[7])
    evidence = cast(dict[str, JsonValue], g1_5_mask["render_evidence"])
    evidence["source_application_request_sha256"] = "f" * 64
    path, digest = _write_canonical_json(tmp_path, "drifted-source.json", value)
    _assert_error_code(
        "GPU_SMOKE_SOURCE_BINDING_INVALID",
        lambda: load_gpu_smoke_packet(path, digest, g1_5_seed=1729),
    )


def test_packet_loader_requires_exact_file_hash_and_duplicate_free_json(
    tmp_path: Path,
) -> None:
    value = _gpu_smoke_packet()
    path, digest = _write_canonical_json(tmp_path, "packet.json", value)
    _assert_error_code(
        "GPU_SMOKE_PACKET_HASH_MISMATCH",
        lambda: load_gpu_smoke_packet(path, "0" * 64, g1_5_seed=1729),
    )

    duplicate = (
        b'{"schema_version":"mobileworld.g1.gpu-live-smoke-packet/v1","schema_version":"duplicate"}'
    )
    duplicate_path = tmp_path / "duplicate.json"
    duplicate_path.write_bytes(duplicate)
    _assert_error_code(
        "GPU_SMOKE_JSON_INVALID",
        lambda: load_gpu_smoke_packet(
            duplicate_path,
            _sha256(duplicate),
            g1_5_seed=1729,
        ),
    )
    assert _sha256(path.read_bytes()) == digest


@pytest.mark.parametrize(
    ("path", "value", "expected_code"),
    (
        (("gpu", "physical_index"), 1, "GPU_SMOKE_GPU_AUTHORITY_INVALID"),
        (
            ("gpu", "uuid"),
            "GPU-00000000-0000-0000-0000-000000000000",
            "GPU_SMOKE_GPU_AUTHORITY_INVALID",
        ),
        (("gpu", "cuda_visible_devices"), "0", "GPU_SMOKE_GPU_AUTHORITY_INVALID"),
        (("gpu", "minimum_free_memory_bytes"), 68_719_476_735, "GPU_SMOKE_GPU_AUTHORITY_INVALID"),
        (("gpu", "foreign_process_signaling_allowed"), True, "GPU_SMOKE_GPU_AUTHORITY_INVALID"),
        (("endpoint", "port"), 18008, "GPU_SMOKE_ENDPOINT_INVALID"),
        (("client_runtime", "openai_version"), "2.15.0", "GPU_SMOKE_RUNTIME_INVALID"),
        (("server_runtime", "vllm_version"), "0.11.1", "GPU_SMOKE_RUNTIME_INVALID"),
        (("policies", "sdk_hidden_retries"), 1, "GPU_SMOKE_POLICY_INVALID"),
        (("policies", "sequential_models"), False, "GPU_SMOKE_POLICY_INVALID"),
        (("policies", "model_co_residency_allowed"), True, "GPU_SMOKE_POLICY_INVALID"),
        (("policies", "broad_process_signaling_allowed"), True, "GPU_SMOKE_POLICY_INVALID"),
        (
            ("network_namespace", "uid_map_line"),
            "0 1036 1",
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
        ),
        (
            ("network_namespace", "expected_interfaces"),
            ["lo", "eth0"],
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
        ),
        (
            ("network_namespace", "external_network_allowed"),
            True,
            "GPU_SMOKE_NETWORK_NAMESPACE_AUTHORITY_INVALID",
        ),
        (
            ("bindings", "runner_module_sha256"),
            "not-a-digest",
            "GPU_SMOKE_BINDING_INVALID",
        ),
    ),
)
def test_authority_rejects_gpu0_runtime_port_retry_and_process_policy_drift(
    tmp_path: Path,
    path: tuple[str, str],
    value: JsonValue,
    expected_code: str,
) -> None:
    packet_value = _gpu_smoke_packet()
    _, packet_sha = _write_canonical_json(tmp_path, "packet.json", packet_value)
    authority = _authority(packet_sha)
    container = cast(dict[str, JsonValue], authority[path[0]])
    container[path[1]] = value
    authority_path, authority_sha = _write_canonical_json(
        tmp_path, f"bad-{path[0]}-{path[1]}.json", authority
    )
    _assert_error_code(
        expected_code,
        lambda: load_gpu_live_authority(authority_path, authority_sha),
    )


def test_prepare_is_deterministic_cpu_only_and_owner_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, packet = _loaded_inputs(tmp_path)
    counters: dict[str, int] = {}

    def bomb(name: str) -> Callable[..., object]:
        counters[name] = 0

        def blocked(*args: object, **kwargs: object) -> object:
            counters[name] += 1
            raise AssertionError(f"forbidden CPU-test path reached: {name}")

        return blocked

    original_import = builtins.__import__
    original_import_module = importlib.import_module

    def guarded_import(
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name.split(".", maxsplit=1)[0] in {"openai", "torch", "vllm", "pynvml"}:
            return bomb(f"import:{name}")()
        return original_import(name, globals_, locals_, fromlist, level)

    def guarded_import_module(name: str, package: str | None = None) -> Any:
        if name.split(".", maxsplit=1)[0] in {"openai", "torch", "vllm", "pynvml"}:
            return bomb(f"import_module:{name}")()
        return original_import_module(name, package)

    with monkeypatch.context() as patcher:
        for name in (
            "socket",
            "create_connection",
            "getaddrinfo",
            "gethostbyname",
            "gethostbyname_ex",
            "gethostbyaddr",
        ):
            patcher.setattr(socket, name, bomb(f"socket.{name}"))
        for name in ("Popen", "run", "call", "check_call", "check_output"):
            patcher.setattr(subprocess, name, bomb(f"subprocess.{name}"))
        for name in ("kill", "killpg", "posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                patcher.setattr(os, name, bomb(f"os.{name}"))
        patcher.setattr(builtins, "__import__", guarded_import)
        patcher.setattr(importlib, "import_module", guarded_import_module)
        first = prepare_gpu_live_smoke(authority, packet, MODEL_CONFIG_PATH)
        second = prepare_gpu_live_smoke(authority, packet, MODEL_CONFIG_PATH)

    assert first == second
    assert first["call_count"] == 22
    assert first["g1_4_call_count"] == 12
    assert first["g1_5_call_count"] == 10
    assert first["model_order"] == list(MODEL_ORDER)
    assert first["validated"] is first["prepared"] is True
    for key in (
        "execution_started",
        "client_created",
        "socket_opened",
        "subprocess_started",
        "gpu_probed",
        "gpu_used",
        "model_loaded",
        "provider_invoked",
        "generated_action_executed",
        "replay_executed",
        "provider_invocation_allowed",
    ):
        assert first[key] is False
    assert len(cast(str, first["call_descriptors_sha256"])) == 64
    assert len(cast(str, first["launch_plans_sha256"])) == 64
    assert counters
    assert set(counters.values()) == {0}

    forged = copy.deepcopy(authority.value)
    forged_namespace = cast(dict[str, JsonValue], forged["network_namespace"])
    forged_namespace["host_owner_uid"] = os.getuid() + 1
    forged_namespace["uid_map_line"] = f"0 {os.getuid() + 1} 1"
    forged_path, forged_sha = _write_canonical_json(
        tmp_path, "foreign-owner-authority.json", cast(JsonValue, forged)
    )
    foreign_authority = load_gpu_live_authority(forged_path, forged_sha)
    _assert_error_code(
        "GPU_SMOKE_AUTHORITY_OWNER_MISMATCH",
        lambda: prepare_gpu_live_smoke(
            foreign_authority,
            packet,
            MODEL_CONFIG_PATH,
        ),
    )


def test_network_namespace_receipt_closes_identity_routes_tools_and_environment_cpu_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _packet = _loaded_inputs(tmp_path)
    _mock_network_namespace_probe(monkeypatch, authority)
    receipt = gpu_live_smoke_module._verify_network_namespace(authority)
    namespace = cast(dict[str, JsonValue], authority.network_namespace)
    assert receipt["schema_version"] == ("mobileworld.g1.gpu-live-smoke-network-namespace/v1")
    assert receipt["implementation"] == "LINUX_USER_NETNS_MAP_ROOT_V1"
    assert receipt["host_owner_uid"] == 1035
    assert receipt["host_owner_gid"] == 1035
    assert receipt["inside_owner_uid"] == 0
    assert receipt["inside_owner_gid"] == 0
    assert receipt["supplementary_groups"] == []
    assert receipt["supplementary_groups_empty"] is True
    assert receipt["uid_map_line"] == "0 1035 1"
    assert receipt["gid_map_line"] == "0 1035 1"
    assert receipt["interfaces"] == ["lo"]
    assert receipt["ipv4_route_interfaces"] == []
    assert receipt["ipv6_route_interfaces"] == ["lo"]
    assert receipt["default_route_count"] == 0
    assert receipt["ipv6_unreachable_default_sentinel_count"] == 2
    assert receipt["ipv6_loopback_host_route_count"] == 1
    assert receipt["external_network_mechanically_unavailable"] is True
    assert receipt["loopback"] == {
        "address": "127.0.0.1",
        "protocol": "TCP",
        "self_connect_succeeded": True,
        "external_endpoint_contacted": False,
    }
    launcher_binaries = cast(list[dict[str, JsonValue]], receipt["launcher_binaries"])
    assert [item["role"] for item in launcher_binaries] == [
        "ENV",
        "UNSHARE",
        "IP",
        "SETPRIV",
        "NVIDIA_SMI",
    ]
    assert [item["sha256"] for item in launcher_binaries] == [
        namespace["env_sha256"],
        namespace["unshare_sha256"],
        namespace["ip_sha256"],
        namespace["setpriv_sha256"],
        namespace["nvidia_smi_sha256"],
    ]
    assert launcher_binaries[-1] == {
        "role": "NVIDIA_SMI",
        "path": namespace["nvidia_smi_path"],
        "sha256": namespace["nvidia_smi_sha256"],
        "byte_count": namespace["nvidia_smi_byte_count"],
        "resolved_regular_executable": True,
        "nofollow_revalidated_immediately_before_probe": True,
    }
    assert receipt["environment_keys"] == sorted(
        cast(dict[str, JsonValue], namespace["launcher_environment"])
    )
    assert receipt["inherited_file_descriptors"] == {
        "schema_version": "mobileworld.g1.gpu-live-smoke-fd-census/v1",
        "open_fd_numbers": [0, 1, 2],
        "descriptors": [
            {
                "fd": fd,
                "descriptor_type": "FIFO",
                "socket": False,
                "inet_socket": False,
            }
            for fd in (0, 1, 2)
        ],
        "open_fd_count": 3,
        "open_fd_count_above_stderr": 0,
        "standard_fd_socket_count": 0,
        "standard_fd_inet_socket_count": 0,
        "all_fds_above_stderr_closed": True,
        "standard_fds_non_inet": True,
        "foreign_process_fds_read": 0,
    }


@pytest.mark.parametrize(
    "mutation",
    ("uid", "uid_map", "environment", "interface", "ipv4_route"),
)
def test_network_namespace_probe_fails_closed_on_identity_or_network_drift_cpu_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    authority, _packet = _loaded_inputs(tmp_path)
    readings = _mock_network_namespace_probe(monkeypatch, authority)
    if mutation == "uid":
        monkeypatch.setattr(gpu_live_smoke_module.os, "getuid", lambda: 1035)
    elif mutation == "uid_map":
        readings["/proc/self/uid_map"] = "0 1036 1\n"
    elif mutation == "environment":
        cast(dict[str, str], gpu_live_smoke_module.os.environ)["HTTP_PROXY"] = (
            "http://forbidden.invalid"
        )
    elif mutation == "interface":
        readings["/proc/net/dev"] += "  eth0: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
    else:
        readings["/proc/net/route"] += "lo 00000000 00000000 0001 0 0 0 00000000 0 0 0\n"
    _assert_error_code(
        "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
        lambda: gpu_live_smoke_module._verify_network_namespace(authority),
    )


def test_outer_fd_closure_removes_synthetic_inheritable_inet_socket_before_unshare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    upper_bound = 1_048_576
    inherited_inet_fd = 9
    descriptors: dict[int, Any] = {
        0: SimpleNamespace(st_mode=stat.S_IFIFO),
        1: SimpleNamespace(st_mode=stat.S_IFIFO),
        2: SimpleNamespace(st_mode=stat.S_IFIFO),
        inherited_inet_fd: SimpleNamespace(
            st_mode=stat.S_IFSOCK,
            family=socket.AF_INET,
            inheritable=True,
        ),
    }
    close_calls: list[tuple[int, int]] = []

    def fake_fstat(fd: int) -> Any:
        if fd not in descriptors:
            raise OSError("synthetic closed descriptor")
        return descriptors[fd]

    def fake_closerange(lower: int, upper: int) -> None:
        close_calls.append((lower, upper))
        for fd in tuple(descriptors):
            if lower <= fd < upper:
                del descriptors[fd]

    monkeypatch.setattr(
        runner_cli.resource,
        "getrlimit",
        lambda _name: (upper_bound, upper_bound),
    )
    monkeypatch.setattr(runner_cli.os, "fstat", fake_fstat)
    monkeypatch.setattr(runner_cli.os, "closerange", fake_closerange)
    monkeypatch.setattr(runner_cli, "sys", SimpleNamespace(modules={}))
    monkeypatch.setattr(
        runner_cli.os,
        "listdir",
        lambda _path: ["0", "1", "2", str(inherited_inet_fd)],
    )
    receipt = runner_cli._close_inherited_file_descriptors(upper_bound)
    assert close_calls == [(3, upper_bound)]
    assert inherited_inet_fd not in descriptors
    assert receipt == runner_cli._expected_outer_fd_closure_receipt()
    assert receipt["all_inherited_fds_closed"] is True
    assert receipt["remaining_fd_count_above_stderr"] == 0
    assert receipt["standard_fd_socket_count"] == 0
    assert receipt["foreign_process_fds_read"] == 0


def test_three_stage_production_candidate_hash_sentinel() -> None:
    runner_cli = _load_runner_cli_module()
    runner_module_path = REPOSITORY_ROOT / "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py"
    assert _sha256(runner_module_path.read_bytes()) == (
        "616303e34d3158ea2ff7e5b376cb1177d1e058da7fcb8cc97cfc9fe9b2679f02"
    )
    assert _sha256(GPU_SMOKE_RUNNER_CLI.read_bytes()) == (
        "9e763c9d776795836bb71c4ef2a2311b0d1e4a016749cc37409f2e19fc1b4504"
    )
    bootstrap_bytes = runner_cli._OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
    assert len(bootstrap_bytes) == 4_645
    assert _sha256(bootstrap_bytes) == (
        "70ac78cc43407933ff72b43925c309823fc852e654367d8576fb74b18811e63b"
    )
    gate_bytes = runner_cli._OWNED_COMMAND_GATE_CODE.encode("utf-8")
    assert len(gate_bytes) == 228
    assert _sha256(gate_bytes) == (
        "70c01194e4ed6ad7cf54a5ffb0caa72bb9d8fa1694544665d53707b90279b061"
    )


def test_outer_execute_is_stdlib_only_and_closes_fds_before_first_unshare_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    order: list[str] = []
    environment = {
        "GPU_SMOKE_OUTER_FD_CLOSURE_SHA256": "a" * 64,
        "PATH": "/usr/bin:/bin",
    }
    namespace = {
        "env_path": "/usr/bin/env",
        "unshare_path": "/usr/bin/unshare",
        "fd_close_upper_bound_exclusive": 1_048_576,
        "outer_fd_closure_receipt_sha256": "a" * 64,
        "launcher_environment": environment,
    }
    authority = {"private_runtime": {"python_path": "/synthetic/private/bin/python3.12"}}
    pre_namespace_environment = {"LC_CTYPE": "C.UTF-8"}
    synthetic_sys = SimpleNamespace(
        argv=[],
        executable="/usr/bin/python3.10",
        flags=SimpleNamespace(
            isolated=1,
            ignore_environment=1,
            no_site=1,
            dont_write_bytecode=1,
        ),
        pycache_prefix="/dev/null",
        modules={},
        path=["/synthetic/stdlib"],
    )
    monkeypatch.setattr(runner_cli, "sys", synthetic_sys)
    monkeypatch.setattr(
        runner_cli,
        "_outer_execute_context",
        lambda _args: order.append("OUTER_CONTEXT")
        or (authority, namespace, environment, pre_namespace_environment),
    )
    monkeypatch.setattr(
        runner_cli,
        "_bootstrap_import_paths",
        lambda _args: pytest.fail("MobileWorld/site bootstrap ran before unshare"),
    )
    expected_receipt = runner_cli._expected_outer_fd_closure_receipt()
    monkeypatch.setattr(
        runner_cli,
        "_close_inherited_file_descriptors",
        lambda upper: order.append(f"FD_CLOSE:{upper}") or expected_receipt,
    )
    monkeypatch.setattr(
        runner_cli,
        "_canonical_json_bytes_stdlib",
        lambda _value: b"synthetic-closure-receipt",
    )
    closure_sha = hashlib.sha256(b"synthetic-closure-receipt").hexdigest()
    namespace["outer_fd_closure_receipt_sha256"] = closure_sha
    environment["GPU_SMOKE_OUTER_FD_CLOSURE_SHA256"] = closure_sha

    class ExecveIntercept(BaseException):
        pass

    execve_calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        order.append("EXECVE")
        execve_calls.append((path, argv, env))
        raise ExecveIntercept

    monkeypatch.setattr(runner_cli.os, "execve", fake_execve)
    argv = [
        "execute",
        "--authority",
        "/synthetic/authority.json",
        "--authority-sha256",
        "b" * 64,
        "--smoke-packet",
        "/synthetic/packet.json",
        "--model-config-manifest",
        "/synthetic/model-config.json",
        "--confirm-execute",
        "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE",
        "--stage0-bootstrap-sha256",
        "9" * 64,
        "--pinned-bootstrap-stage",
        "STAGE0",
    ]
    with pytest.raises(ExecveIntercept):
        runner_cli.main(argv)
    assert order == ["OUTER_CONTEXT", "FD_CLOSE:1048576", "EXECVE"]
    assert synthetic_sys.modules == {}
    assert len(execve_calls) == 1
    path, reexec_argv, empty_environment = execve_calls[0]
    assert path == "/usr/bin/env"
    assert empty_environment == {}
    assert reexec_argv[:2] == ["/usr/bin/env", "-i"]
    unshare_index = reexec_argv.index("/usr/bin/unshare")
    assert reexec_argv[unshare_index : unshare_index + 8] == [
        "/usr/bin/unshare",
        "--user",
        "--map-root-user",
        "--net",
        "/usr/bin/env",
        "-i",
        "LC_CTYPE=C.UTF-8",
        "/usr/bin/python3.10",
    ]
    assert reexec_argv[unshare_index + 8 : unshare_index + 13] == [
        "-I",
        "-S",
        "-B",
        "-X",
        "pycache_prefix=/dev/null",
    ]
    assert reexec_argv[unshare_index + 13 : unshare_index + 16] == [
        "-c",
        runner_cli._OUTER_STDLIB_BOOTSTRAP_CODE,
        "STAGE1",
    ]
    assert "/synthetic/private/bin/python3.12" not in reexec_argv
    assert "--inside-network-namespace" not in reexec_argv


def test_stage1_system_python_censuses_private_runtime_before_private_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    authority = _authority("4" * 64)
    namespace = cast(dict[str, JsonValue], authority["network_namespace"])
    environment = cast(dict[str, str], namespace["launcher_environment"])
    pre_namespace_environment = cast(
        dict[str, str],
        namespace["pre_namespace_environment"],
    )
    bootstrap_bytes = runner_cli._OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
    bootstrap_sha256 = _sha256(bootstrap_bytes)
    source = cast(dict[str, JsonValue], authority["source"])
    source["outer_bootstrap_code_sha256"] = bootstrap_sha256
    source["outer_bootstrap_code_byte_count"] = len(bootstrap_bytes)
    authority_sha256 = "8" * 64
    runner_cli._PINNED_BOOTSTRAP = {
        "stage": "STAGE1",
        "authority_sha256": authority_sha256,
        "bootstrap_sha256": bootstrap_sha256,
        "cli_opened_nofollow": True,
        "cli_compiled_from_verified_bytes": True,
    }
    synthetic_sys = SimpleNamespace(
        argv=[],
        executable="/usr/bin/python3.10",
        flags=SimpleNamespace(
            isolated=1,
            ignore_environment=1,
            no_site=1,
            dont_write_bytecode=1,
        ),
        pycache_prefix="/dev/null",
        modules={},
        path=["/usr/lib/python3.10"],
    )
    monkeypatch.setattr(runner_cli, "sys", synthetic_sys)
    monkeypatch.setattr(runner_cli.os, "environ", pre_namespace_environment.copy())
    monkeypatch.setattr(runner_cli.os, "getuid", lambda: 0)
    monkeypatch.setattr(runner_cli.os, "getgid", lambda: 0)
    monkeypatch.setattr(
        runner_cli,
        "_outer_authority_namespace_values",
        lambda _args: (
            authority,
            namespace,
            environment,
            pre_namespace_environment,
        ),
    )
    monkeypatch.setattr(
        runner_cli,
        "_normalized_id_map_stdlib",
        lambda path: namespace["uid_map_line"]
        if path.endswith("uid_map")
        else namespace["gid_map_line"],
    )
    order: list[str] = []
    path_to_role = {
        namespace["env_path"]: "ENV",
        namespace["ip_path"]: "IP",
        namespace["setpriv_path"]: "SETPRIV",
    }
    monkeypatch.setattr(
        runner_cli,
        "_hash_regular_nofollow_stdlib",
        lambda path, _expected: order.append(f"HASH:{path_to_role[path]}") or 1,
    )
    preexec_receipt: dict[str, JsonValue] = {
        "schema_version": "mobileworld.g1.gpu-live-smoke-preimport-runtime-census/v1",
        "phase": "PRE_PRIVATE_EXEC",
        "private_runtime": {"tree_sha256": "c" * 64},
        "private_python": {"sha256": "b" * 64},
        "complete_stdlib_tree_censused": True,
        "encodings_included_in_complete_tree": True,
    }
    monkeypatch.setattr(
        runner_cli,
        "_preexec_private_runtime_census_stdlib",
        lambda _authority: order.append("PRIVATE_RUNTIME_CENSUS") or preexec_receipt,
    )
    scratch_receipt = {
        "schema_version": "mobileworld.g1.gpu-live-smoke-stage1-scratch/v1",
        "created_exclusively": True,
    }
    monkeypatch.setattr(
        runner_cli,
        "_prepare_stage1_launcher_scratch_stdlib",
        lambda _authority, _environment: order.append("SCRATCH") or scratch_receipt,
    )

    def fake_loopback_command(command: list[str], **kwargs: object) -> dict[str, object]:
        assert command == ["/usr/bin/ip", "link", "set", "dev", "lo", "up"]
        assert kwargs == {
            "env": pre_namespace_environment,
            "timeout_seconds": 10,
            "error_code": "GPU_SMOKE_NETWORK_NAMESPACE_INVALID",
        }
        order.append("LOOPBACK_UP")
        return {
            "returncode": 0,
            "stdout": b"",
            "stderr": b"",
            "receipt": _synthetic_owned_command_receipt(command),
        }

    monkeypatch.setattr(
        runner_cli,
        "_run_owned_command_stdlib",
        fake_loopback_command,
    )
    monkeypatch.setattr(
        runner_cli,
        "_bootstrap_import_paths",
        lambda _args: pytest.fail("private/source imports ran in system stage1"),
    )

    class ExecveIntercept(BaseException):
        pass

    execve_calls: list[tuple[str, list[str], dict[str, str]]] = []

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        order.append("PRIVATE_EXEC")
        execve_calls.append((path, argv, env))
        raise ExecveIntercept

    monkeypatch.setattr(runner_cli.os, "execve", fake_execve)
    argv = [
        "execute",
        "--authority",
        "/synthetic/authority.json",
        "--authority-sha256",
        authority_sha256,
        "--smoke-packet",
        "/synthetic/packet.json",
        "--model-config-manifest",
        "/synthetic/model-config.json",
        "--confirm-execute",
        "EXECUTE-D034-SYNTHETIC-22-CALL-SMOKE",
        "--inside-network-namespace-stage1",
        "--stage0-bootstrap-sha256",
        bootstrap_sha256,
        "--pinned-bootstrap-stage",
        "STAGE1",
    ]
    with pytest.raises(ExecveIntercept):
        runner_cli.main(argv)
    assert order == [
        "HASH:ENV",
        "HASH:IP",
        "HASH:SETPRIV",
        "PRIVATE_RUNTIME_CENSUS",
        "SCRATCH",
        "LOOPBACK_UP",
        "PRIVATE_EXEC",
    ]
    path, sandbox_argv, inherited_environment = execve_calls[0]
    assert path == "/usr/bin/env"
    assert inherited_environment == pre_namespace_environment
    assert sandbox_argv[:2] == ["/usr/bin/env", "-i"]
    setpriv_index = sandbox_argv.index("/usr/bin/setpriv")
    assert sandbox_argv[setpriv_index : setpriv_index + 7] == [
        "/usr/bin/setpriv",
        "--no-new-privs",
        "--bounding-set=-all",
        "--ambient-caps=-all",
        "--inh-caps=-all",
        "--clear-groups",
        "/synthetic/private-runtime/g1-gpu-smoke/bin/python3.12",
    ]
    assert sandbox_argv[setpriv_index + 7 : setpriv_index + 12] == [
        "-I",
        "-S",
        "-B",
        "-X",
        "pycache_prefix=/dev/null",
    ]
    assert "STAGE2" in sandbox_argv


def test_stage1_encodings_tree_drift_fails_before_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    authority = _authority("4" * 64)
    namespace = cast(dict[str, JsonValue], authority["network_namespace"])
    environment = cast(dict[str, str], namespace["launcher_environment"])
    pre_namespace_environment = cast(
        dict[str, str],
        namespace["pre_namespace_environment"],
    )
    bootstrap_bytes = runner_cli._OUTER_STDLIB_BOOTSTRAP_CODE.encode("utf-8")
    bootstrap_sha256 = _sha256(bootstrap_bytes)
    source = cast(dict[str, JsonValue], authority["source"])
    source["outer_bootstrap_code_sha256"] = bootstrap_sha256
    source["outer_bootstrap_code_byte_count"] = len(bootstrap_bytes)
    authority_sha256 = "8" * 64
    runner_cli._PINNED_BOOTSTRAP = {
        "stage": "STAGE1",
        "authority_sha256": authority_sha256,
        "bootstrap_sha256": bootstrap_sha256,
        "cli_opened_nofollow": True,
        "cli_compiled_from_verified_bytes": True,
    }
    monkeypatch.setattr(
        runner_cli,
        "sys",
        SimpleNamespace(executable="/usr/bin/python3.10"),
    )
    monkeypatch.setattr(runner_cli.os, "environ", pre_namespace_environment.copy())
    monkeypatch.setattr(runner_cli.os, "getuid", lambda: 0)
    monkeypatch.setattr(runner_cli.os, "getgid", lambda: 0)
    monkeypatch.setattr(
        runner_cli,
        "_outer_authority_namespace_values",
        lambda _args: (
            authority,
            namespace,
            environment,
            pre_namespace_environment,
        ),
    )
    monkeypatch.setattr(
        runner_cli,
        "_normalized_id_map_stdlib",
        lambda path: namespace["uid_map_line"]
        if path.endswith("uid_map")
        else namespace["gid_map_line"],
    )
    monkeypatch.setattr(
        runner_cli,
        "_hash_regular_nofollow_stdlib",
        lambda _path, _expected: 1,
    )
    census_calls: list[str] = []

    def fail_encodings_census(_authority: dict[str, object]) -> dict[str, object]:
        census_calls.append("private-runtime/lib/python3.12/encodings")
        raise RuntimeError("GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH")

    monkeypatch.setattr(
        runner_cli,
        "_preexec_private_runtime_census_stdlib",
        fail_encodings_census,
    )
    forbidden = {"scratch": 0, "ip": 0, "private_exec": 0}

    def block(name: str) -> Callable[..., object]:
        def blocked(*_args: object, **_kwargs: object) -> object:
            forbidden[name] += 1
            raise AssertionError(f"{name} occurred after encodings drift")

        return blocked

    monkeypatch.setattr(
        runner_cli,
        "_prepare_stage1_launcher_scratch_stdlib",
        block("scratch"),
    )
    monkeypatch.setattr(runner_cli.subprocess, "run", block("ip"))
    monkeypatch.setattr(runner_cli.os, "execve", block("private_exec"))
    with pytest.raises(RuntimeError, match="GPU_SMOKE_RUNTIME_TREE_AUTHORITY_MISMATCH"):
        runner_cli._stage1_preexec_context(
            SimpleNamespace(
                authority_sha256=authority_sha256,
                stage0_bootstrap_sha256=bootstrap_sha256,
                pinned_bootstrap_stage="STAGE1",
            )
        )
    assert census_calls == ["private-runtime/lib/python3.12/encodings"]
    assert forbidden == {"scratch": 0, "ip": 0, "private_exec": 0}


def test_inner_namespace_bootstrap_inserts_exact_bound_paths_without_executing_pth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    site_packages = tmp_path / "lib/python3.12/site-packages"
    site_packages.mkdir(parents=True)
    (site_packages / "forbidden-bootstrap.pth").write_text(
        "import builtins; builtins.GPU_SMOKE_PTH_EXECUTED = True\n",
        encoding="utf-8",
    )
    monkeypatch.delattr(builtins, "GPU_SMOKE_PTH_EXECUTED", raising=False)
    synthetic_sys = SimpleNamespace(
        executable="/synthetic/client/bin/python",
        path=["/synthetic/stdlib"],
        modules={},
    )
    monkeypatch.setattr(runner_cli, "sys", synthetic_sys)
    authority_value = _authority("4" * 64)
    client_runtime = cast(dict[str, JsonValue], authority_value["client_runtime"])
    client_runtime["python_path"] = synthetic_sys.executable
    client_runtime["python_resolved_path"] = synthetic_sys.executable
    client_runtime["site_packages_path"] = str(site_packages)
    order: list[str] = []
    stage1_receipt: dict[str, JsonValue] = {
        "private_runtime_preexec": {
            "phase": "PRE_PRIVATE_EXEC",
            "private_runtime": {"tree_sha256": "c" * 64},
            "private_python": {"sha256": "b" * 64},
        }
    }
    preimport_receipt: dict[str, JsonValue] = {
        "phase": "PRE_IMPORT",
        "private_runtime": {"tree_sha256": "c" * 64},
        "private_python": {"sha256": "b" * 64},
    }
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "inner-bootstrap-authority.json",
        authority_value,
    )
    monkeypatch.setattr(
        runner_cli,
        "_PINNED_BOOTSTRAP",
        {
            "stage": "STAGE2",
            "authority_sha256": authority_sha,
            "bootstrap_sha256": "9" * 64,
            "cli_opened_nofollow": True,
            "cli_compiled_from_verified_bytes": True,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner_cli,
        "_read_authority_stdlib",
        lambda _args: order.append("READ_AUTHORITY") or authority_value,
    )
    monkeypatch.setattr(
        runner_cli,
        "_decode_stage1_receipt",
        lambda _args: order.append("DECODE_STAGE1") or stage1_receipt,
    )
    monkeypatch.setattr(
        runner_cli,
        "_verify_source_closure_stdlib",
        lambda _authority: order.append("SOURCE_CLOSURE") or {"tree_sha256": "f" * 64},
    )
    monkeypatch.setattr(
        runner_cli,
        "_preimport_runtime_census_stdlib",
        lambda _authority, *, inside_network_namespace: (
            order.append(f"PREIMPORT_CENSUS:{inside_network_namespace}")
            or copy.deepcopy(preimport_receipt)
        ),
    )
    runner_cli._bootstrap_import_paths(
        SimpleNamespace(
            command="execute",
            authority=str(authority_path),
            authority_sha256=authority_sha,
            inside_network_namespace=True,
            inside_network_namespace_stage1=False,
            namespace_sandboxed=True,
            pinned_bootstrap_stage="STAGE2",
            stage0_bootstrap_sha256="9" * 64,
            stage1_receipt_b64="synthetic",
        )
    )
    assert order == [
        "READ_AUTHORITY",
        "DECODE_STAGE1",
        "SOURCE_CLOSURE",
        "PREIMPORT_CENSUS:True",
    ]
    assert synthetic_sys.path[:3] == [
        str(REPOSITORY_ROOT / "MobileWorld/src"),
        str(site_packages),
        "/synthetic/stdlib",
    ]
    assert not hasattr(builtins, "GPU_SMOKE_PTH_EXECUTED")
    assert runner_cli._PREIMPORT_RUNTIME_RECEIPT["stage1_preexec_receipt_sha256"] == (
        _canonical_sha256(stage1_receipt)
    )


def test_stage2_source_controller_disables_fsmonitor_and_hooks_and_requires_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    authority = _authority("4" * 64)
    source = cast(dict[str, JsonValue], authority["source"])
    bindings = cast(dict[str, JsonValue], authority["bindings"])
    inventory: dict[str, object] = {
        "root": source["source_root"],
        "tree_sha256": source["source_tree_sha256"],
        "tree_entry_count": source["source_tree_entry_count"],
        "tree_byte_count": source["source_tree_byte_count"],
        "ignored_bytecode_cache_entries_not_importable": True,
        "symlink_count": 0,
        "hardlink_count": 0,
        "all_files_nofollow_revalidated": True,
    }
    monkeypatch.setattr(
        runner_cli,
        "_hash_regular_nofollow_stdlib",
        lambda path, expected: (
            pytest.fail("source controller hashed an unbound Git executable")
            if (path, expected) != (source["git_path"], source["git_sha256"])
            else 1
        ),
    )
    monkeypatch.setattr(
        runner_cli,
        "_enumerate_source_tree_stdlib",
        lambda root: (
            pytest.fail("source controller enumerated an unbound source root")
            if root != source["source_root"]
            else copy.deepcopy(inventory)
        ),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []
    dirty = False

    def fake_run(command: list[str], **kwargs: object) -> dict[str, object]:
        calls.append((command, kwargs))
        if command[-2:] == ["rev-parse", "HEAD"]:
            stdout = (cast(str, bindings["source_git_commit"]) + "\n").encode()
        else:
            assert command[-3:] == ["status", "--porcelain=v1", "--untracked-files=all"]
            stdout = b"?? drift.py\n" if dirty else b""
        return {
            "returncode": 0,
            "stdout": stdout,
            "stderr": b"",
            "receipt": _synthetic_owned_command_receipt(command, stdout=stdout),
        }

    monkeypatch.setattr(runner_cli, "_run_owned_command_stdlib", fake_run)
    receipt = runner_cli._verify_source_closure_stdlib(cast(dict[str, object], authority))
    assert receipt["porcelain_v1_empty"] is True
    assert receipt["untracked_files_all_empty"] is True
    assert receipt["project_or_third_party_module_imported_before_closure"] is False
    assert len(cast(list[object], receipt["owned_commands"])) == 2
    expected_prefix = [
        source["git_path"],
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        source["worktree_root"],
    ]
    assert [command[:7] for command, _kwargs in calls] == [expected_prefix, expected_prefix]
    expected_environment = {
        "HOME": cast(
            str,
            cast(
                dict[str, JsonValue],
                cast(dict[str, JsonValue], authority["network_namespace"])["launcher_environment"],
            )["HOME"],
        ),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
    }
    for _command, kwargs in calls:
        assert kwargs == {
            "env": expected_environment,
            "timeout_seconds": 10 if _command[-2:] == ["rev-parse", "HEAD"] else 30,
            "error_code": "GPU_SMOKE_SOURCE_BINDING_INVALID",
        }

    dirty = True
    with pytest.raises(RuntimeError, match="GPU_SMOKE_SOURCE_BINDING_INVALID"):
        runner_cli._verify_source_closure_stdlib(cast(dict[str, object], authority))


class _SyntheticOwnedCommandStream:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.closed = False

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        self.closed = True


def _exercise_owned_command_cpu_fake(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> dict[str, object]:
    root_pid = 62_000
    root_pidfd = 620
    stdout_stream = _SyntheticOwnedCommandStream(621)
    stderr_stream = _SyntheticOwnedCommandStream(622)
    order: list[str] = []
    sent_pidfds: list[int] = []
    alive = {root_pidfd: True}

    class FakeProcess:
        pid = root_pid
        stdout = stdout_stream
        stderr = stderr_stream

        @staticmethod
        def wait() -> int:
            order.append("WAIT")
            return 0

        @staticmethod
        def kill() -> NoReturn:
            pytest.fail("Popen.kill is forbidden")

        @staticmethod
        def send_signal(_signal: int) -> NoReturn:
            pytest.fail("Popen.send_signal is forbidden")

        @staticmethod
        def communicate(*_args: object, **_kwargs: object) -> NoReturn:
            pytest.fail("Popen.communicate is forbidden")

    class FakeSelector:
        def __init__(self) -> None:
            self.registered: dict[int, _SyntheticOwnedCommandStream] = {}
            self.select_count = 0

        def register(self, stream: _SyntheticOwnedCommandStream, *_args: object) -> None:
            self.registered[stream.fileno()] = stream

        def unregister(self, stream: _SyntheticOwnedCommandStream) -> None:
            self.registered.pop(stream.fileno())

        def select(self, _timeout: float) -> list[tuple[object, int]]:
            self.select_count += 1
            if scenario == "NORMAL" and self.select_count == 2:
                alive[root_pidfd] = False
            return [
                (SimpleNamespace(fileobj=stream), 1) for stream in tuple(self.registered.values())
            ]

        @staticmethod
        def close() -> None:
            return None

    root_identity = gpu_live_smoke_module.ProcessIdentity(
        uid=os.getuid(),
        pid=root_pid,
        ppid=os.getpid(),
        pgid=root_pid,
        sid=root_pid,
        starttime_ticks=620_000,
        executable_path="/synthetic/gate-python",
        executable_sha256="a" * 64,
        argv=("/synthetic/gate-python", "synthetic-gate"),
    )
    root_member = gpu_live_smoke_module._PinnedOwnedCommandMember(
        identity=root_identity,
        pidfd=root_pidfd,
        procdir_fd=623,
        depth=0,
    )

    monkeypatch.setattr(gpu_live_smoke_module.os, "pipe2", lambda _flags: (624, 625))
    monkeypatch.setattr(
        gpu_live_smoke_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: order.append("POPEN") or FakeProcess(),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_open",
        lambda pid: order.append(f"PIDFD:{pid}") or root_pidfd,
    )

    def fake_capture(
        _process: object,
        _pidfd: int,
        _gate_command: tuple[str, ...],
    ) -> Any:
        order.append("ROOT_PROOF")
        if scenario in {"ROOT_UNPROVEN", "ROOT_FOREIGN"}:
            code = (
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE"
                if scenario == "ROOT_UNPROVEN"
                else "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH"
            )
            raise GpuLiveSmokeError(code, "synthetic root proof failure")
        return root_member

    monkeypatch.setattr(gpu_live_smoke_module, "_capture_owned_command_root", fake_capture)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_hash_regular_file",
        lambda _path, **_kwargs: ("b" * 64, 1),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "write",
        lambda fd, data: order.append(f"GATE_WRITE:{fd}") or len(data),
    )
    monkeypatch.setattr(gpu_live_smoke_module.os, "close", lambda _fd: None)

    def fake_set_blocking(*_args: object) -> None:
        if scenario == "POST_GATE_SETUP_FAILURE":
            raise OSError("synthetic post-gate stream setup failure")

    monkeypatch.setattr(gpu_live_smoke_module.os, "set_blocking", fake_set_blocking)
    monkeypatch.setattr(gpu_live_smoke_module.selectors, "DefaultSelector", FakeSelector)
    output_chunks = {
        stdout_stream.fileno(): [b"ok", b""] if scenario == "NORMAL" else [b"12345"],
        stderr_stream.fileno(): [b""],
    }
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "read",
        lambda fd, _size: output_chunks[fd].pop(0),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_process_is_live",
        lambda pidfd: alive.get(pidfd, False),
    )
    child_member: Any = None

    def fake_observe(members: dict[int, Any]) -> tuple[Any, ...]:
        nonlocal child_member
        if scenario == "CENSUS_FAILURE":
            raise GpuLiveSmokeError(
                "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
                "synthetic descendant census failure",
            )
        if scenario != "LATE_DESCENDANT":
            return ()
        if child_member is None:
            child_identity = gpu_live_smoke_module.ProcessIdentity(
                uid=os.getuid(),
                pid=root_pid + 1,
                ppid=root_pid,
                pgid=root_pid,
                sid=root_pid,
                starttime_ticks=620_001,
                executable_path="/synthetic/late-child",
                executable_sha256="c" * 64,
                argv=("/synthetic/late-child",),
            )
            child_member = gpu_live_smoke_module._PinnedOwnedCommandMember(
                identity=child_identity,
                pidfd=root_pidfd + 1,
                procdir_fd=626,
                depth=1,
            )
            members[child_identity.pid] = child_member
            alive[root_pidfd + 1] = True
            return (child_member,)
        return ()

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_observe_owned_command_descendants",
        fake_observe,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pinned_command_minimal",
        lambda member, **_kwargs: member.identity if alive.get(member.pidfd, False) else None,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pinned_command_full_identity",
        lambda *_args, **_kwargs: pytest.fail(
            "auxiliary cleanup incorrectly required post-fork executable identity"
        ),
    )

    def fake_pidfd_signal(pidfd: int, sent_signal: object) -> bool:
        assert sent_signal == gpu_live_smoke_module.signal.SIGKILL
        sent_pidfds.append(pidfd)
        alive[pidfd] = False
        return True

    monkeypatch.setattr(gpu_live_smoke_module, "_pidfd_send_signal", fake_pidfd_signal)
    monotonic_values = iter((0.0, 2.0)) if scenario == "TIMEOUT" else None
    monkeypatch.setattr(
        gpu_live_smoke_module.time,
        "monotonic",
        (lambda: next(monotonic_values, 3.0)) if monotonic_values is not None else (lambda: 0.0),
    )
    command = ["/usr/bin/true", "--synthetic"]
    try:
        result = gpu_live_smoke_module._run_owned_command(
            command,
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=1,
            stdout_byte_cap=4,
            stderr_byte_cap=4,
            error_code="GPU_SMOKE_RUNTIME_INVALID",
        )
        error = None
    except GpuLiveSmokeError as caught:
        result = None
        error = caught
    return {
        "result": result,
        "error": error,
        "order": order,
        "sent_pidfds": sent_pidfds,
        "root_pidfd": root_pidfd,
        "child_pidfd": root_pidfd + 1,
    }


def test_owned_command_normal_path_releases_gate_only_after_root_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, "NORMAL")
    assert outcome["error"] is None
    result = cast(Any, outcome["result"])
    assert result.stdout == b"ok"
    assert result.returncode == 0
    assert cast(list[str], outcome["order"])[:4] == [
        "POPEN",
        "PIDFD:62000",
        "ROOT_PROOF",
        "GATE_WRITE:625",
    ]
    assert outcome["sent_pidfds"] == []
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(result.receipt)


def test_owned_command_schema_has_exact_production_and_stdlib_receipt_key_parity() -> None:
    runner_cli = _load_runner_cli_module()
    command = ("/usr/bin/true", "--synthetic")
    production_receipt = gpu_live_smoke_module._owned_command_receipt(
        command=command,
        timeout_seconds=1,
        stdout_byte_cap=4,
        stderr_byte_cap=4,
        stdout=b"",
        stderr=b"",
        root=None,
        members={},
        initial_pidfd_acquired=False,
        launch_gate_released=False,
        completion_reason="POPEN_FAILED",
        returncode=None,
        signal_trace=[],
        release_proven=True,
    )
    stdlib_receipt = runner_cli._owned_command_receipt_stdlib(
        command,
        1,
        4,
        4,
        b"",
        b"",
        None,
        {},
        False,
        "POPEN_FAILED",
        None,
        [],
        False,
    )
    assert set(production_receipt) == set(stdlib_receipt)
    validator = _schema_validator("gpu_smoke_owned_command.schema.json")
    validator.validate(production_receipt)
    validator.validate(stdlib_receipt)


def test_owned_command_receipts_are_deduplicated_content_objects_before_enclosing_events(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "owned-command-materialization"
    store = gpu_live_smoke_module._EvidenceStore(
        str(evidence_root),
        "owned-command-materialization-run",
    )
    receipt = _synthetic_owned_command_receipt(
        ["/usr/bin/true", "--synthetic"],
        stdout=b"ok",
    )
    store.event("PREFLIGHT_VALIDATED", {"owned_command": receipt})
    store.event("MODEL_PREFLIGHT_VALIDATED", {"duplicate_owned_command": receipt})
    references = store.owned_command_references()
    assert len(references) == 1
    stored_receipt = _read_content_ref(
        evidence_root,
        references[0],
        schema_name="gpu_smoke_owned_command.schema.json",
    )
    assert stored_receipt == receipt
    for event_name in store._event_files:
        event = cast(
            dict[str, JsonValue],
            json.loads((store.run_dir / event_name).read_bytes()),
        )
        payload = cast(dict[str, JsonValue], event["payload"])
        assert receipt not in payload.values()
        assert references[0] in payload.values()


@pytest.mark.parametrize(
    ("scenario", "reason"),
    (("TIMEOUT", "TIMEOUT"), ("OUTPUT_LIMIT", "STDOUT_LIMIT")),
)
def test_owned_command_timeout_and_output_cap_cleanup_use_only_pidfd(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    reason: str,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, scenario)
    error = cast(GpuLiveSmokeError, outcome["error"])
    receipt = cast(
        dict[str, JsonValue], cast(dict[str, JsonValue], error.execution_detail)["owned_command"]
    )
    assert receipt["completion_reason"] == reason
    assert receipt["release_proven"] is True
    assert receipt["numeric_pid_signal_count"] == 0
    assert receipt["popen_kill_count"] == 0
    assert receipt["popen_send_signal_count"] == 0
    assert receipt["communicate_timeout_count"] == 0
    assert outcome["sent_pidfds"] == [outcome["root_pidfd"]]
    assert [
        item["state"] for item in cast(list[dict[str, JsonValue]], receipt["signal_trace"])
    ] == [
        "INTENDED",
        "IDENTITY_REVALIDATED",
        "SENT",
    ]
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


def test_owned_command_late_fork_exec_changed_descendant_is_cleaned_child_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, "LATE_DESCENDANT")
    error = cast(GpuLiveSmokeError, outcome["error"])
    receipt = cast(
        dict[str, JsonValue], cast(dict[str, JsonValue], error.execution_detail)["owned_command"]
    )
    assert receipt["completion_reason"] == "UNEXPECTED_DESCENDANT"
    assert receipt["observed_descendant_count"] == 1
    assert outcome["sent_pidfds"] == [outcome["child_pidfd"], outcome["root_pidfd"]]
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


@pytest.mark.parametrize("scenario", ("ROOT_UNPROVEN", "ROOT_FOREIGN"))
def test_owned_command_unproven_or_foreign_root_never_releases_gate_or_signals(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, scenario)
    error = cast(GpuLiveSmokeError, outcome["error"])
    receipt = cast(
        dict[str, JsonValue], cast(dict[str, JsonValue], error.execution_detail)["owned_command"]
    )
    assert "GATE_WRITE:625" not in outcome["order"]
    assert outcome["sent_pidfds"] == []
    assert receipt["launch_gate_released_after_identity_proof"] is False
    assert receipt["release_proven"] is False
    assert receipt["numeric_pid_signal_count"] == 0
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


def test_owned_command_proven_root_census_exception_triggers_exact_pidfd_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, "CENSUS_FAILURE")
    error = cast(GpuLiveSmokeError, outcome["error"])
    receipt = cast(
        dict[str, JsonValue], cast(dict[str, JsonValue], error.execution_detail)["owned_command"]
    )
    assert outcome["sent_pidfds"] == [outcome["root_pidfd"]]
    assert receipt["completion_reason"] == "INTERNAL_FAILURE"
    assert receipt["release_proven"] is True
    assert receipt["launch_gate_released_after_identity_proof"] is True
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


def test_owned_command_post_gate_pre_block_exception_retains_root_for_pidfd_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _exercise_owned_command_cpu_fake(monkeypatch, "POST_GATE_SETUP_FAILURE")
    error = cast(GpuLiveSmokeError, outcome["error"])
    receipt = cast(
        dict[str, JsonValue],
        cast(dict[str, JsonValue], error.execution_detail)["owned_command"],
    )
    assert "GATE_WRITE:625" in outcome["order"]
    assert outcome["sent_pidfds"] == [outcome["root_pidfd"]]
    assert receipt["launch_gate_released_after_identity_proof"] is True
    assert receipt["completion_reason"] == "INTERNAL_FAILURE"
    assert receipt["release_proven"] is True
    assert receipt["numeric_pid_signal_count"] == 0
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


def test_cli_owned_command_post_gate_close_failure_retains_root_for_pidfd_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_cli = _load_runner_cli_module()
    root_pid = 63_000
    root_pidfd = 630
    gate_read_fd = 631
    gate_write_fd = 632
    procdir_fd = 633
    gate_released = False
    live = True
    captured_gate_command: list[str] = []
    sent_pidfds: list[int] = []

    class FakeProcess:
        pid = root_pid
        stdout = _SyntheticOwnedCommandStream(634)
        stderr = _SyntheticOwnedCommandStream(635)

        @staticmethod
        def wait() -> int:
            return 0

        @staticmethod
        def kill() -> NoReturn:
            pytest.fail("CLI owned-command cleanup used Popen.kill")

        @staticmethod
        def send_signal(_signal: int) -> NoReturn:
            pytest.fail("CLI owned-command cleanup used Popen.send_signal")

        @staticmethod
        def communicate(*_args: object, **_kwargs: object) -> NoReturn:
            pytest.fail("CLI owned-command cleanup used communicate(timeout)")

    def fake_popen(command: list[str], **_kwargs: object) -> FakeProcess:
        captured_gate_command[:] = command
        return FakeProcess()

    monkeypatch.setattr(runner_cli.os, "pipe2", lambda _flags: (gate_read_fd, gate_write_fd))
    monkeypatch.setattr(runner_cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner_cli, "_owned_pidfd_open_stdlib", lambda _pid: root_pidfd)
    monkeypatch.setattr(runner_cli.os, "open", lambda *_args, **_kwargs: procdir_fd)
    monkeypatch.setattr(
        runner_cli.os,
        "fstat",
        lambda fd: SimpleNamespace(st_uid=os.getuid())
        if fd == procdir_fd
        else pytest.fail(f"unexpected fstat fd {fd}"),
    )
    minimal: dict[str, object] = {
        "uid": os.getuid(),
        "pid": root_pid,
        "ppid": os.getpid(),
        "pgid": root_pid,
        "sid": root_pid,
        "starttime_ticks": 630_000,
    }
    monkeypatch.setattr(
        runner_cli,
        "_minimal_proc_identity_stdlib",
        lambda _fd, _pid: copy.deepcopy(minimal),
    )

    def fake_full_identity(_fd: int, _minimal: dict[str, object]) -> dict[str, object]:
        argv = captured_gate_command.copy()
        return {
            **minimal,
            "executable_path": os.path.realpath(argv[0]),
            "executable_sha256": "a" * 64,
            "argv": argv,
            "argv_sha256": _canonical_sha256(argv),
        }

    monkeypatch.setattr(runner_cli, "_full_proc_identity_stdlib", fake_full_identity)
    monkeypatch.setattr(
        runner_cli,
        "_digest_regular_nofollow_stdlib",
        lambda _path: "b" * 64,
    )

    def fake_write(fd: int, data: bytes) -> int:
        nonlocal gate_released
        assert fd == gate_write_fd
        gate_released = True
        return len(data)

    def fake_close(fd: int) -> None:
        if fd == gate_write_fd and gate_released:
            raise OSError("synthetic close failure after gate release")

    monkeypatch.setattr(runner_cli.os, "write", fake_write)
    monkeypatch.setattr(runner_cli.os, "close", fake_close)
    monkeypatch.setattr(runner_cli, "_owned_pidfd_live_stdlib", lambda _pidfd: live)
    monkeypatch.setattr(
        runner_cli,
        "_revalidate_owned_member_stdlib",
        lambda _member, **_kwargs: copy.deepcopy(minimal) if live else None,
    )

    def fake_pidfd_signal(pidfd: int, sent_signal: object) -> bool:
        nonlocal live
        assert sent_signal == runner_cli.signal.SIGKILL
        sent_pidfds.append(pidfd)
        live = False
        return True

    monkeypatch.setattr(runner_cli, "_owned_pidfd_send_signal_stdlib", fake_pidfd_signal)
    monkeypatch.setattr(runner_cli.time, "monotonic", lambda: 0.0)
    with pytest.raises(runner_cli._OwnedCommandStdlibError) as raised:
        runner_cli._run_owned_command_stdlib(
            ["/usr/bin/true", "--synthetic"],
            env={"PATH": "/usr/bin:/bin"},
            timeout_seconds=1,
            error_code="GPU_SMOKE_RUNTIME_INVALID",
        )
    receipt = cast(dict[str, JsonValue], raised.value.receipt)
    assert sent_pidfds == [root_pidfd]
    assert receipt["completion_reason"] == "ACQUISITION_FAILURE"
    assert receipt["launch_gate_released_after_identity_proof"] is True
    assert receipt["release_proven"] is True
    assert receipt["numeric_pid_signal_count"] == 0
    assert [
        item["reason"] for item in cast(list[dict[str, JsonValue]], receipt["signal_trace"])
    ] == ["ACQUISITION_FAILURE_AFTER_GATE_RELEASE"] * 3
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(receipt)


def test_owned_command_sources_have_no_numeric_or_popen_timeout_kill_path() -> None:
    production_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            REPOSITORY_ROOT / "MobileWorld/src/mobile_world/offline/gpu_live_smoke.py",
            GPU_SMOKE_RUNNER_CLI,
        )
    )
    for forbidden in (
        r"\bos\.kill\b",
        r"\.kill\(",
        r"\.send_signal\(",
        r"communicate\([^\n]*timeout",
        r"subprocess\.run\(",
    ):
        assert re.search(forbidden, production_sources) is None


def _synthetic_snapshot_inputs(tmp_path: Path) -> tuple[Any, Any, dict[str, Path]]:
    authority_value = _authority("4" * 64)
    models = cast(dict[str, JsonValue], authority_value["models"])
    bindings: dict[str, Any] = {}
    mutable_blobs: dict[str, Path] = {}
    for model_id in MODEL_ORDER:
        repository = MODEL_REPOSITORIES[model_id]
        revision = MODEL_REVISIONS[model_id]
        model_root = tmp_path / f"models--{repository.replace('/', '--')}"
        blobs_root = model_root / "blobs"
        snapshot = model_root / "snapshots" / revision
        blobs_root.mkdir(parents=True)
        snapshot.mkdir(parents=True)
        grouped_specs = {
            "config_files": (("config.json", b"synthetic-config:" + model_id.encode()),),
            "weight_shards": (("model.safetensors", b"synthetic-weight:" + model_id.encode()),),
            "tokenizer_artifacts": (
                ("tokenizer.json", b"synthetic-tokenizer:" + model_id.encode()),
            ),
        }
        if model_id == "mai_ui_8b":
            grouped_specs["authority_extra"] = (
                (".gitattributes", b"*.bin filter=lfs diff=lfs merge=lfs -text\n"),
                ("README.md", b"# Synthetic MAI snapshot metadata\n"),
            )
        expected_groups: dict[str, list[JsonValue]] = {
            "config_files": [],
            "weight_shards": [],
        }
        tokenizer_artifacts: list[JsonValue] = []
        observed: list[JsonValue] = []
        for group, specs in grouped_specs.items():
            for relative, data in specs:
                digest = _sha256(data)
                blob = blobs_root / digest
                blob.write_bytes(data)
                link_target = f"../../blobs/{digest}"
                (snapshot / relative).symlink_to(link_target)
                entry: JsonValue = {
                    "path": relative,
                    "byte_count": len(data),
                    "sha256": digest,
                }
                if group == "tokenizer_artifacts":
                    tokenizer_artifacts.append(entry)
                elif group != "authority_extra":
                    expected_groups[group].append(entry)
                observed.append(
                    {
                        "path": relative,
                        "entry_type": "SYMLINK",
                        "symlink_target": link_target,
                        "resolved_target": str(blob.resolve()),
                        "byte_count": len(data),
                        "sha256": digest,
                        "group": group,
                    }
                )
                if relative == "model.safetensors":
                    mutable_blobs[model_id] = blob
        observed.sort(key=lambda item: cast(dict[str, JsonValue], item)["path"])
        model = cast(dict[str, JsonValue], models[model_id])
        model.update(
            {
                "snapshot_path": str(snapshot),
                "snapshot_tree_sha256": _canonical_sha256(cast(JsonValue, observed)),
                "snapshot_tree_entry_count": len(observed),
                "snapshot_tree_byte_count": sum(
                    cast(int, cast(dict[str, JsonValue], item)["byte_count"]) for item in observed
                ),
            }
        )
        bindings[model_id] = SimpleNamespace(
            checkpoint_inventory=SimpleNamespace(files=expected_groups),
            tokenizer_binding={"artifacts": tokenizer_artifacts},
        )
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "snapshot-authority.json",
        cast(JsonValue, authority_value),
    )
    authority = load_gpu_live_authority(authority_path, authority_sha)
    receipt = SimpleNamespace(model=lambda model_id: bindings[model_id])
    return authority, receipt, mutable_blobs


def test_snapshot_pre_and_post_inventory_is_complete_content_addressed_and_stable(
    tmp_path: Path,
) -> None:
    authority, receipt, mutable_blobs = _synthetic_snapshot_inputs(tmp_path)
    before = gpu_live_smoke_module._verify_local_snapshots(authority, receipt)
    after = gpu_live_smoke_module._verify_local_snapshots(authority, receipt)
    assert before == after
    assert set(before) == set(MODEL_ORDER)
    for model_id in MODEL_ORDER:
        inventory = cast(dict[str, JsonValue], before[model_id])
        entries = cast(list[JsonValue], inventory["entries"])
        expected_groups = {
            "config_files",
            "weight_shards",
            "tokenizer_artifacts",
        }
        expected_count = 3
        expected_extra_count = 0
        if model_id == "mai_ui_8b":
            expected_groups.add("authority_extra")
            expected_count = 5
            expected_extra_count = 2
            assert {
                cast(dict[str, JsonValue], item)["path"]
                for item in entries
                if cast(dict[str, JsonValue], item)["group"] == "authority_extra"
            } == {".gitattributes", "README.md"}
        assert inventory["entry_count"] == expected_count
        assert inventory["additional_artifact_count"] == expected_extra_count
        assert {cast(dict[str, JsonValue], item)["group"] for item in entries} == expected_groups
        assert all(
            cast(dict[str, JsonValue], item)["entry_type"] == "SYMLINK"
            and cast(str, cast(dict[str, JsonValue], item)["symlink_target"]).startswith(
                "../../blobs/"
            )
            for item in entries
        )

    mutable_blobs["qwen3vl_8b"].write_bytes(b"post-run snapshot drift")
    _assert_error_code(
        "GPU_SMOKE_SNAPSHOT_REQUIRED_MEMBER_MISMATCH",
        lambda: gpu_live_smoke_module._verify_local_snapshots(authority, receipt),
    )


def test_snapshot_inventory_rejects_unbound_extra_tree_entry(tmp_path: Path) -> None:
    authority, receipt, _ = _synthetic_snapshot_inputs(tmp_path)
    qwen = cast(dict[str, JsonValue], authority.models["qwen3vl_8b"])
    snapshot = Path(cast(str, qwen["snapshot_path"]))
    data = b"not authority-bound"
    digest = _sha256(data)
    blob = snapshot.parent.parent / "blobs" / digest
    blob.write_bytes(data)
    (snapshot / "unbound.json").symlink_to(f"../../blobs/{digest}")
    _assert_error_code(
        "GPU_SMOKE_SNAPSHOT_TREE_AUTHORITY_MISMATCH",
        lambda: gpu_live_smoke_module._verify_local_snapshots(authority, receipt),
    )


def test_evidence_store_is_content_addressed_append_only_and_collision_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "evidence"
    store = gpu_live_smoke_module._EvidenceStore(str(evidence_root), "synthetic-run")
    payload = b"synthetic non-case evidence\n"
    reference = store.object(payload, "application/octet-stream")
    assert reference == store.object(payload, "application/octet-stream")
    digest = _sha256(payload)
    assert reference == {
        "relative_path": f"objects/sha256/{digest[:2]}/{digest}",
        "sha256": digest,
        "byte_count": len(payload),
        "media_type": "application/octet-stream",
    }
    assert (evidence_root / cast(str, reference["relative_path"])).read_bytes() == payload

    first = store.event("PREFLIGHT", {"synthetic": True})
    second = store.event("MODEL_CLOSED", {"model_id": "qwen3vl_8b"})
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert first["generated_action_executed"] is False
    assert first["replay_executed"] is False
    assert len(tuple((evidence_root / "runs" / "synthetic-run").iterdir())) == 2

    terminal = store.seal(
        "FAIL",
        {
            "failure_code": "SYNTHETIC_CPU_TEST_TERMINAL",
            "generated_action_executed": False,
            "replay_executed": False,
        },
    )
    manifest_path = (
        evidence_root
        / cast(str, terminal["run_relative_path"])
        / cast(str, terminal["manifest_file"])
    )
    manifest = cast(dict[str, JsonValue], json.loads(manifest_path.read_bytes()))
    manifest_subject = {
        key: value for key, value in manifest.items() if key != "manifest_subject_sha256"
    }
    assert manifest["manifest_subject_sha256"] == _canonical_sha256(
        cast(JsonValue, manifest_subject)
    )
    assert manifest["exact_event_files"] == sorted(
        path.name
        for path in (evidence_root / "runs" / "synthetic-run").iterdir()
        if path.name[:4] in {"0001", "0002"}
    )
    assert manifest["event_count"] == 2
    assert manifest["content_object_count"] == 3
    assert manifest["self_excluded_content_object_roles"] == [
        "MANIFEST_OBJECT",
        "TERMINAL_RECEIPT_OBJECT",
    ]
    assert manifest["self_excluded_content_object_count"] == 2
    assert manifest["content_object_census_rule"] == (
        "EXACT_PRESEAL_OBJECTS_PLUS_TWO_EXPLICIT_SELF_EXCLUSIONS"
    )
    listed_digests = {
        cast(str, cast(dict[str, JsonValue], item)["sha256"])
        for item in cast(list[JsonValue], manifest["exact_content_objects"])
    }
    excluded_digests = {
        cast(str, cast(dict[str, JsonValue], terminal[key])["sha256"])
        for key in ("manifest", "terminal_receipt")
    }
    assert listed_digests.isdisjoint(excluded_digests)
    assert set(store._object_refs) == listed_digests | excluded_digests
    terminal_path = (
        evidence_root
        / cast(str, terminal["run_relative_path"])
        / cast(str, terminal["terminal_file"])
    )
    assert (
        _sha256(terminal_path.read_bytes())
        == cast(dict[str, JsonValue], terminal["terminal_receipt"])["sha256"]
    )
    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_ALREADY_SEALED",
        lambda: store.event("AFTER_TERMINAL", {}),
    )
    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_ALREADY_SEALED",
        lambda: store.object(b"after-terminal", "application/octet-stream"),
    )

    collision_root = tmp_path / "collision-evidence"
    collision_store = gpu_live_smoke_module._EvidenceStore(
        str(collision_root),
        "synthetic-collision-run",
    )
    monkeypatch.setattr(gpu_live_smoke_module, "_sha256", lambda _data: "d" * 64)
    collision_store.object(b"first", "application/octet-stream")
    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_COLLISION",
        lambda: collision_store.object(b"second", "application/octet-stream"),
    )


@pytest.mark.parametrize(
    "mutation",
    ("orphan", "named_extra", "missing", "symlink", "external_hardlink"),
)
def test_evidence_seal_rejects_non_exact_run_directory_census(
    tmp_path: Path,
    mutation: str,
) -> None:
    evidence_root = tmp_path / mutation
    store = gpu_live_smoke_module._EvidenceStore(str(evidence_root), "closure-run")
    store.event("PREFLIGHT", {"synthetic": True})
    event_name = store._event_files[0]
    if mutation == "orphan":
        (store.run_dir / "orphan.bin").write_bytes(b"unlisted")
    elif mutation == "named_extra":
        digest = "a" * 64
        (store.run_dir / f"0002-{digest}.json").write_bytes(b"{}")
    elif mutation == "missing":
        (store.run_dir / event_name).unlink()
    elif mutation == "symlink":
        (store.run_dir / "unlisted-link").symlink_to(event_name)
    else:
        os.link(
            store.run_dir / event_name,
            evidence_root / "outside-run-hardlink",
        )

    expected_code = (
        "GPU_SMOKE_EVIDENCE_ROOT_INVALID"
        if mutation == "external_hardlink"
        else "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID"
    )
    _assert_error_code(
        expected_code,
        lambda: store.seal(
            "FAIL",
            {
                "failure_code": "SYNTHETIC_CPU_TEST_TERMINAL",
                "generated_action_executed": False,
                "replay_executed": False,
            },
        ),
    )
    assert store._sealed is False
    assert not tuple(store.run_dir.glob("manifest-*.json"))
    assert not tuple(store.run_dir.glob("terminal-*.json"))


def test_evidence_content_object_hardlink_is_rejected_before_reuse(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "content-hardlink"
    store = gpu_live_smoke_module._EvidenceStore(str(evidence_root), "hardlink-run")
    payload = b"synthetic-content"
    reference = store.object(payload, "application/octet-stream")
    object_path = evidence_root / cast(str, reference["relative_path"])
    os.link(object_path, evidence_root / "outside-object-hardlink")

    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_ROOT_INVALID",
        lambda: store.object(payload, "application/octet-stream"),
    )


def test_server_environment_is_offline_proxy_free_and_disables_telemetry(
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    model_scratch = scratch_root / "synthetic-model"
    scratch_root.mkdir(mode=0o700)
    model_scratch.mkdir(mode=0o700)
    for name in gpu_live_smoke_module._SERVER_SCRATCH_DIRECTORY_NAMES.values():
        (model_scratch / name).mkdir(mode=0o700)
    authority = SimpleNamespace(
        runtime_scratch_root=str(scratch_root),
        network_namespace={"launcher_environment": {"LD_LIBRARY_PATH": "/synthetic/cuda/lib64"}},
        gpu={"uuid": GPU0_UUID},
    )
    environment = gpu_live_smoke_module._server_environment(
        authority,
        str(model_scratch),
    )
    assert environment["CUDA_VISIBLE_DEVICES"] == GPU0_UUID
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert environment["HF_DATASETS_OFFLINE"] == "1"
    assert environment["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"
    assert environment["VLLM_NO_USAGE_STATS"] == "1"
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"
    assert environment["no_proxy"] == "127.0.0.1,localhost"
    assert environment["NCCL_SOCKET_IFNAME"] == "lo"
    assert environment["GLOO_SOCKET_IFNAME"] == "lo"
    assert environment["HOME"] == str(model_scratch / "home")
    forbidden_proxy_keys = {
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    assert forbidden_proxy_keys.isdisjoint(key.casefold() for key in environment)


def test_runtime_tree_digest_normalizes_host_1035_and_inner_root_owner_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same mapped tree has one digest on both sides of the user namespace."""

    assert (os.getuid(), os.getgid()) == (1035, 1035)
    runtime_root = tmp_path / "private-runtime"
    runtime_root.mkdir(mode=0o700)
    runtime_file = runtime_root / "python3.12"
    runtime_file.write_bytes(b"synthetic private runtime bytes")
    runtime_file.chmod(0o400)
    runtime_root.chmod(0o500)
    arguments = {
        "directory_mode": 0o500,
        "regular_mode": 0o400,
        "executable_mode": 0o500,
        "symlinks_allowed": False,
        "hardlinks_allowed": False,
        "include_entries": True,
    }
    _assert_error_code(
        "GPU_SMOKE_RUNTIME_TREE_INVALID",
        lambda: gpu_live_smoke_module._enumerate_bound_runtime_tree(
            str(runtime_root),
            owner_uid=0,
            owner_gid=0,
            recorded_owner_uid=0,
            recorded_owner_gid=0,
            **arguments,
        ),
    )
    host_census = gpu_live_smoke_module._enumerate_bound_runtime_tree(
        str(runtime_root),
        owner_uid=1035,
        owner_gid=1035,
        recorded_owner_uid=0,
        recorded_owner_gid=0,
        **arguments,
    )

    real_stat = os.stat
    real_fstat = os.fstat

    def mapped_root(metadata: os.stat_result) -> SimpleNamespace:
        return SimpleNamespace(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_uid=0,
            st_gid=0,
            st_size=metadata.st_size,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns,
        )

    with monkeypatch.context() as mapped_namespace:
        mapped_namespace.setattr(
            gpu_live_smoke_module.os,
            "stat",
            lambda *args, **kwargs: mapped_root(real_stat(*args, **kwargs)),
        )
        mapped_namespace.setattr(
            gpu_live_smoke_module.os,
            "fstat",
            lambda fd: mapped_root(real_fstat(fd)),
        )
        inner_census = gpu_live_smoke_module._enumerate_bound_runtime_tree(
            str(runtime_root),
            owner_uid=0,
            owner_gid=0,
            recorded_owner_uid=0,
            recorded_owner_gid=0,
            **arguments,
        )

    assert host_census["tree_sha256"] == inner_census["tree_sha256"]
    assert host_census["tree_entry_count"] == inner_census["tree_entry_count"]
    assert host_census["tree_byte_count"] == inner_census["tree_byte_count"]
    assert host_census["owner_uid"] == inner_census["owner_uid"] == 0
    assert host_census["owner_gid"] == inner_census["owner_gid"] == 0
    assert host_census["aggregate_owner_identity"] == "AUTHORIZED_OWNER"
    assert host_census["numeric_owner_excluded_from_tree_digest"] is True
    entries = cast(list[dict[str, JsonValue]], host_census["entries"])
    assert entries
    assert all(
        entry["owner_role"] == "AUTHORIZED_OWNER" and "uid" not in entry and "gid" not in entry
        for entry in entries
    )

    runtime_file.chmod(0o600)
    runtime_root.chmod(0o700)


def test_private_runtime_reflink_failure_has_no_byte_copy_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source-python"
    destination = tmp_path / "private-python"
    source_bytes = b"synthetic sealed Python ELF bytes"
    source.write_bytes(source_bytes)
    source.chmod(0o500)
    ioctl_calls: list[int] = []

    def unavailable_reflink(_destination_fd: int, request: int, _source_fd: int) -> None:
        ioctl_calls.append(request)
        raise OSError("synthetic FICLONE unavailable")

    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", unavailable_reflink)
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module._copy_regular_file_by_reflink(
            source,
            destination,
        ),
    )
    assert ioctl_calls == [gpu_live_smoke_module._FICLONE]
    assert source.read_bytes() == source_bytes
    assert destination.read_bytes() == b""


def _cpu_fake_reflink(destination_fd: int, request: int, source_fd: int) -> None:
    """Copy synthetic bytes through pinned FDs while exercising reflink guards."""

    assert request == gpu_live_smoke_module._FICLONE
    os.lseek(source_fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            break
        written = 0
        while written < len(chunk):
            written += os.write(destination_fd, chunk[written:])
    os.lseek(source_fd, 0, os.SEEK_SET)


def _minimal_private_runtime_builder_inputs(root: Path) -> dict[str, Path]:
    source_root = root / "source-runtime"
    python = source_root / "bin/python3.12"
    stdlib = source_root / "lib/python3.12"
    client_site = root / "client-site"
    server_site = root / "server-site"
    python.parent.mkdir(parents=True)
    stdlib.mkdir(parents=True)
    client_site.mkdir(parents=True)
    server_site.mkdir(parents=True)
    python.write_bytes(b"synthetic Python 3.12 ELF")
    python.chmod(0o500)
    (stdlib / "stdlib.py").write_bytes(b"STDLIB = True\n")
    (client_site / "client.py").write_bytes(b"CLIENT = True\n")
    (server_site / "server.py").write_bytes(b"SERVER = True\n")
    return {
        "python": python,
        "stdlib": stdlib,
        "stdlib_file": stdlib / "stdlib.py",
        "client_site": client_site,
        "client_file": client_site / "client.py",
        "server_site": server_site,
        "server_file": server_site / "server.py",
        "output": root / "private-runtime",
    }


def test_reflink_copy_accepts_explicit_source_nlink3_for_copy_only_and_seals_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "python3.12"
    source_bytes = b"synthetic hardlinked Python ELF bytes"
    source.write_bytes(source_bytes)
    source.chmod(0o500)
    os.link(source, tmp_path / "python-alias-1")
    os.link(source, tmp_path / "python-alias-2")
    source_metadata = source.stat()
    assert source_metadata.st_nlink == 3
    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)

    rejected_destination = tmp_path / "rejected-private-python"
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module._copy_regular_file_by_reflink(
            source,
            rejected_destination,
            expected_source_identity=gpu_live_smoke_module._tree_entry_identity(source_metadata),
        ),
    )
    assert not rejected_destination.exists()

    destination = tmp_path / "private-python"
    binding = gpu_live_smoke_module._copy_regular_file_by_reflink(
        source,
        destination,
        expected_source_identity=gpu_live_smoke_module._tree_entry_identity(source_metadata),
        allow_source_hardlinks=True,
    )
    assert binding == {
        "byte_count": len(source_bytes),
        "sha256": _sha256(source_bytes),
        "executable": True,
        "source_link_count": 3,
        "source_hardlink_observed": True,
        "source_hardlinks_accepted_for_copy_only": True,
        "destination_link_count": 1,
        "source_destination_distinct_inodes": True,
        "source_pre_equals_source_post": True,
        "source_equals_destination": True,
    }
    assert source.stat().st_nlink == 3
    assert destination.stat().st_nlink == 1
    assert stat.S_IMODE(destination.stat().st_mode) == 0o500
    assert destination.read_bytes() == source_bytes


def test_private_runtime_tree_records_source_hardlink_census_but_output_is_single_linked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source-tree"
    source_root.mkdir()
    source = source_root / "module.py"
    source_bytes = b"value = 'synthetic'\n"
    source.write_bytes(source_bytes)
    source.chmod(0o400)
    os.link(source, tmp_path / "module-alias-1")
    os.link(source, tmp_path / "module-alias-2")
    assert source.stat().st_nlink == 3
    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)

    destination_root = tmp_path / "private-tree"
    binding = gpu_live_smoke_module._reflink_runtime_tree(
        source_root,
        destination_root,
        exclude_site_packages=False,
        allow_source_hardlinks=True,
    )
    source_link_census = [{"path": source.name, "nlink": 3}]
    assert binding["source_hardlinked_entry_count"] == 1
    assert binding["source_max_nlink"] == 3
    assert binding["source_link_census_sha256"] == _canonical_sha256(
        cast(JsonValue, source_link_census)
    )
    assert binding["source_hardlinks_accepted_for_copy_only"] is True
    assert binding["destination_regular_files_single_linked"] is True
    assert binding["source_pre_equals_source_post"] is True
    assert binding["source_equals_destination"] is True
    assert (
        binding["source_pre_content_sha256"]
        == binding["source_post_content_sha256"]
        == binding["destination_content_sha256"]
    )
    destination = destination_root / source.name
    assert destination.read_bytes() == source_bytes
    assert destination.stat().st_nlink == 1
    inventory = gpu_live_smoke_module._enumerate_bound_runtime_tree(
        str(destination_root),
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        recorded_owner_uid=0,
        recorded_owner_gid=0,
        directory_mode=0o500,
        regular_mode=0o400,
        executable_mode=0o500,
        symlinks_allowed=False,
        hardlinks_allowed=False,
        forbid_bytecode_and_pth=True,
        include_entries=True,
    )
    assert inventory["hardlinks_allowed"] is False


def test_private_runtime_builder_allows_hardlinks_only_for_stdlib_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _minimal_private_runtime_builder_inputs(tmp_path)
    aliases = tmp_path / "stdlib-aliases"
    aliases.mkdir()
    os.link(paths["stdlib_file"], aliases / "stdlib-alias-1")
    os.link(paths["stdlib_file"], aliases / "stdlib-alias-2")
    assert paths["stdlib_file"].stat().st_nlink == 3
    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)

    receipt = gpu_live_smoke_module.build_private_runtime(
        source_python_path=str(paths["python"]),
        client_site_packages_path=str(paths["client_site"]),
        server_site_packages_path=str(paths["server_site"]),
        output_root=str(paths["output"]),
    )
    assert receipt["stdlib_source_hardlinks_allowed_for_copy_only"] is True
    assert receipt["python_and_site_source_hardlinks_allowed"] is False
    assert receipt["destination_hardlinks_allowed"] is False
    bindings = cast(
        dict[str, dict[str, JsonValue]],
        receipt["source_destination_content_bindings"],
    )
    python_binding = bindings["python"]
    assert python_binding["source_link_count"] == 1
    assert python_binding["source_hardlink_observed"] is False
    assert python_binding["source_hardlinks_accepted_for_copy_only"] is False
    assert python_binding["destination_link_count"] == 1
    assert python_binding["source_destination_distinct_inodes"] is True
    stdlib_binding = bindings["stdlib"]
    expected_census = [{"path": "stdlib.py", "nlink": 3}]
    assert stdlib_binding["source_hardlinked_entry_count"] == 1
    assert stdlib_binding["source_max_nlink"] == 3
    assert stdlib_binding["source_link_census_sha256"] == _canonical_sha256(
        cast(JsonValue, expected_census)
    )
    assert stdlib_binding["source_hardlinks_accepted_for_copy_only"] is True
    for name in ("client_site_packages", "server_site_packages"):
        site_binding = bindings[name]
        assert site_binding["source_hardlinked_entry_count"] == 0
        assert site_binding["source_max_nlink"] == 1
        assert site_binding["source_hardlinks_accepted_for_copy_only"] is False
        assert site_binding["destination_regular_files_single_linked"] is True
    for inventory_name in (
        "private_runtime_inventory",
        "client_site_packages_inventory",
        "server_site_packages_inventory",
    ):
        inventory = cast(dict[str, JsonValue], receipt[inventory_name])
        assert inventory["hardlinks_allowed"] is False
    destination_python = paths["output"] / "bin/python3.12"
    assert (destination_python.stat().st_dev, destination_python.stat().st_ino) != (
        paths["python"].stat().st_dev,
        paths["python"].stat().st_ino,
    )
    for destination in paths["output"].rglob("*"):
        if destination.is_file():
            assert destination.stat().st_nlink == 1
            assert stat.S_IMODE(destination.stat().st_mode) in {0o400, 0o500}


@pytest.mark.parametrize("hardlinked_source", ("python", "client_file", "server_file"))
def test_private_runtime_builder_rejects_hardlinked_python_and_site_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hardlinked_source: str,
) -> None:
    paths = _minimal_private_runtime_builder_inputs(tmp_path)
    os.link(paths[hardlinked_source], tmp_path / f"{hardlinked_source}-alias")
    assert paths[hardlinked_source].stat().st_nlink == 2
    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module.build_private_runtime(
            source_python_path=str(paths["python"]),
            client_site_packages_path=str(paths["client_site"]),
            server_site_packages_path=str(paths["server_site"]),
            output_root=str(paths["output"]),
        ),
    )
    assert not paths["output"].exists()


@pytest.mark.parametrize("restore_source", (False, True))
def test_private_runtime_builder_rejects_same_fd_source_drift_during_reflink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restore_source: bool,
) -> None:
    source = tmp_path / "source-runtime-file"
    original = b"stable-source-bytes"
    mutated = b"mutated-source-byte"
    assert len(original) == len(mutated)
    source.write_bytes(original)
    source.chmod(0o400)
    source_alias = tmp_path / "source-runtime-alias"
    os.link(source, source_alias)
    source_metadata = source.stat()
    assert source_metadata.st_nlink == 2

    def drift_during_clone(destination_fd: int, request: int, source_fd: int) -> None:
        if restore_source:
            source_alias.write_bytes(mutated)
            _cpu_fake_reflink(destination_fd, request, source_fd)
            source_alias.write_bytes(original)
        else:
            _cpu_fake_reflink(destination_fd, request, source_fd)
            source_alias.write_bytes(mutated)

    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", drift_during_clone)
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module._copy_regular_file_by_reflink(
            source,
            tmp_path / "private-runtime-file",
            expected_source_identity=gpu_live_smoke_module._tree_entry_identity(source_metadata),
            allow_source_hardlinks=True,
        ),
    )


def test_private_runtime_builder_rejects_source_alias_unlink_relink_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source-runtime-file"
    source.write_bytes(b"stable-source-bytes")
    source.chmod(0o400)
    source_alias = tmp_path / "source-runtime-alias"
    os.link(source, source_alias)
    source_metadata = source.stat()
    assert source_metadata.st_nlink == 2

    def relink_during_clone(destination_fd: int, request: int, source_fd: int) -> None:
        _cpu_fake_reflink(destination_fd, request, source_fd)
        for _ in range(4096):
            source_alias.unlink()
            os.link(source, source_alias)
            if os.fstat(source_fd).st_ctime_ns != source_metadata.st_ctime_ns:
                break
        assert os.fstat(source_fd).st_ctime_ns != source_metadata.st_ctime_ns

    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", relink_during_clone)
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module._copy_regular_file_by_reflink(
            source,
            tmp_path / "private-runtime-file",
            expected_source_identity=gpu_live_smoke_module._tree_entry_identity(source_metadata),
            allow_source_hardlinks=True,
        ),
    )
    assert source.stat().st_nlink == 2


@pytest.mark.parametrize("destination_mutation", ("content", "hardlink"))
def test_private_runtime_builder_rejects_destination_drift_before_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_mutation: str,
) -> None:
    source = tmp_path / "source-runtime-file"
    source.write_bytes(b"stable-source-bytes")
    source.chmod(0o400)
    destination = tmp_path / "private-runtime-file"
    external_alias = tmp_path / "external-private-runtime-alias"

    def mutate_destination(destination_fd: int, request: int, source_fd: int) -> None:
        _cpu_fake_reflink(destination_fd, request, source_fd)
        if destination_mutation == "content":
            assert os.pwrite(destination_fd, b"X", 0) == 1
        else:
            os.link(destination, external_alias)

    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", mutate_destination)
    _assert_error_code(
        "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
        lambda: gpu_live_smoke_module._copy_regular_file_by_reflink(
            source,
            destination,
        ),
    )


def test_private_runtime_tree_records_exact_mixed_and_empty_source_link_census(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "mixed-source-tree"
    source_root.mkdir()
    sources = {
        "nlink1.py": b"ONE = 1\n",
        "nlink2.py": b"TWO = 2\n",
        "nlink3.py": b"THREE = 3\n",
    }
    for name, content in sources.items():
        path = source_root / name
        path.write_bytes(content)
        path.chmod(0o400)
    os.link(source_root / "nlink2.py", tmp_path / "nlink2-alias")
    os.link(source_root / "nlink3.py", tmp_path / "nlink3-alias-1")
    os.link(source_root / "nlink3.py", tmp_path / "nlink3-alias-2")
    monkeypatch.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)

    destination_root = tmp_path / "mixed-private-tree"
    binding = gpu_live_smoke_module._reflink_runtime_tree(
        source_root,
        destination_root,
        exclude_site_packages=False,
        allow_source_hardlinks=True,
    )
    expected_census = [
        {"path": "nlink1.py", "nlink": 1},
        {"path": "nlink2.py", "nlink": 2},
        {"path": "nlink3.py", "nlink": 3},
    ]
    assert binding["source_hardlinked_entry_count"] == 2
    assert binding["source_max_nlink"] == 3
    assert binding["source_link_census_sha256"] == _canonical_sha256(
        cast(JsonValue, expected_census)
    )
    assert binding["source_pre_equals_source_post"] is True
    assert binding["source_equals_destination"] is True
    assert (
        binding["source_pre_content_sha256"]
        == binding["source_post_content_sha256"]
        == binding["destination_content_sha256"]
    )
    for name, content in sources.items():
        destination = destination_root / name
        assert destination.read_bytes() == content
        assert destination.stat().st_nlink == 1

    empty_source = tmp_path / "empty-source-tree"
    empty_source.mkdir()
    empty_binding = gpu_live_smoke_module._reflink_runtime_tree(
        empty_source,
        tmp_path / "empty-private-tree",
        exclude_site_packages=False,
        allow_source_hardlinks=True,
    )
    assert empty_binding["source_hardlinked_entry_count"] == 0
    assert empty_binding["source_max_nlink"] == 1
    assert empty_binding["source_link_census_sha256"] == _canonical_sha256([])
    assert empty_binding["destination_regular_files_single_linked"] is True


def test_private_runtime_tree_rejects_source_path_swap_before_and_after_pinned_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_copy = gpu_live_smoke_module._copy_regular_file_by_reflink

    before_root = tmp_path / "before-open-source"
    before_root.mkdir()
    before_source = before_root / "module.py"
    before_source.write_bytes(b"pinned-before-open")
    before_source.chmod(0o400)
    before_replacement = tmp_path / "before-open-replacement"
    before_replacement.write_bytes(b"replacement-before")
    before_replacement.chmod(0o400)
    before_backup = tmp_path / "before-open-backup"
    ioctl_count = 0

    def unexpected_ioctl(destination_fd: int, request: int, source_fd: int) -> None:
        nonlocal ioctl_count
        ioctl_count += 1
        _cpu_fake_reflink(destination_fd, request, source_fd)

    def swap_before_open(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> dict[str, JsonValue]:
        before_source.rename(before_backup)
        before_replacement.rename(before_source)
        return original_copy(source, destination, **kwargs)

    with monkeypatch.context() as before_context:
        before_context.setattr(gpu_live_smoke_module.fcntl, "ioctl", unexpected_ioctl)
        before_context.setattr(
            gpu_live_smoke_module,
            "_copy_regular_file_by_reflink",
            swap_before_open,
        )
        _assert_error_code(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            lambda: gpu_live_smoke_module._reflink_runtime_tree(
                before_root,
                tmp_path / "before-open-destination",
                exclude_site_packages=False,
                allow_source_hardlinks=True,
            ),
        )
    assert ioctl_count == 0

    after_root = tmp_path / "after-open-source"
    after_root.mkdir()
    after_source = after_root / "module.py"
    original_bytes = b"pinned-after-open"
    after_source.write_bytes(original_bytes)
    after_source.chmod(0o400)
    after_replacement = tmp_path / "after-open-replacement"
    after_replacement.write_bytes(b"replacement-after")
    after_replacement.chmod(0o400)
    after_backup = tmp_path / "after-open-backup"
    after_destination_root = tmp_path / "after-open-destination"

    def swap_after_pinned_copy(
        source: Path,
        destination: Path,
        **kwargs: Any,
    ) -> dict[str, JsonValue]:
        binding = original_copy(source, destination, **kwargs)
        after_source.rename(after_backup)
        after_replacement.rename(after_source)
        return binding

    with monkeypatch.context() as after_context:
        after_context.setattr(gpu_live_smoke_module.fcntl, "ioctl", _cpu_fake_reflink)
        after_context.setattr(
            gpu_live_smoke_module,
            "_copy_regular_file_by_reflink",
            swap_after_pinned_copy,
        )
        _assert_error_code(
            "GPU_SMOKE_PRIVATE_RUNTIME_BUILD_FAILED",
            lambda: gpu_live_smoke_module._reflink_runtime_tree(
                after_root,
                after_destination_root,
                exclude_site_packages=False,
                allow_source_hardlinks=True,
            ),
        )
    assert (after_destination_root / "module.py").read_bytes() == original_bytes
    assert after_source.read_bytes() == b"replacement-after"


def test_builder_source_hardlink_exception_does_not_relax_private_authority_schema() -> None:
    validator = _schema_validator("gpu_smoke_authority.schema.json")
    authority = _authority("4" * 64)
    validator.validate(authority)
    fields = (
        ("private_runtime", "hardlinks_allowed"),
        ("client_runtime", "site_packages_hardlinks_allowed"),
        ("server_runtime", "site_packages_hardlinks_allowed"),
    )
    for section, field in fields:
        mutated = copy.deepcopy(authority)
        cast(dict[str, JsonValue], mutated[section])[field] = True
        errors = list(validator.iter_errors(mutated))
        assert any(
            list(error.path) == [section, field] and error.validator == "const" for error in errors
        )


def test_gpu_capacity_probe_rechecks_exact_gpu0_uuid_and_64_gib_floor_cpu_fake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_value = _authority("4" * 64)
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "capacity-authority.json",
        cast(JsonValue, authority_value),
    )
    authority = load_gpu_live_authority(authority_path, authority_sha)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU0_UUID)
    nvidia_binding_calls: list[tuple[str, str]] = []

    def fake_bound_executable(path: str, *, expected_sha256: str) -> tuple[str, int]:
        nvidia_binding_calls.append((path, expected_sha256))
        return expected_sha256, 1_243_808

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_hash_bound_executable",
        fake_bound_executable,
    )
    free_mib = iter((70_000, 65_535))
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Any:
        commands.append(command)
        assert kwargs == {
            "env": {"PATH": "/usr/bin:/bin"},
            "timeout_seconds": 15,
            "error_code": "GPU_SMOKE_GPU_PROBE_FAILED",
        }
        free = next(free_mib)
        return _synthetic_owned_command_result(
            command,
            stdout=f"0, {GPU0_UUID}, NVIDIA H200, 143771, {free}\n".encode(),
        )

    monkeypatch.setattr(gpu_live_smoke_module, "_run_owned_command", fake_run)
    first = gpu_live_smoke_module._inspect_gpu(authority)
    assert first["physical_index"] == 0
    assert first["uuid"] == GPU0_UUID
    assert first["minimum_free_memory_bytes"] == 68_719_476_736
    assert first["foreign_processes_signaled"] == 0
    assert first["nvidia_smi"] == {
        "path": "/usr/bin/nvidia-smi",
        "sha256": ("cd4dc7637cd3ef30002cbf97afcc66f111eb90c0c615f37e264c392242eb51b6"),
        "byte_count": 1_243_808,
        "resolved_regular_executable": True,
        "nofollow_revalidated_immediately_before_probe": True,
    }
    _assert_error_code(
        "GPU_SMOKE_GPU_CAPACITY_INSUFFICIENT",
        lambda: gpu_live_smoke_module._inspect_gpu(authority),
    )
    assert len(commands) == 2
    assert nvidia_binding_calls == [
        (
            "/usr/bin/nvidia-smi",
            "cd4dc7637cd3ef30002cbf97afcc66f111eb90c0c615f37e264c392242eb51b6",
        ),
        (
            "/usr/bin/nvidia-smi",
            "cd4dc7637cd3ef30002cbf97afcc66f111eb90c0c615f37e264c392242eb51b6",
        ),
    ]
    assert all(
        command[-1] == f"--id={GPU0_UUID}"
        and "--query-gpu=index,uuid,name,memory.total,memory.free" in command
        for command in commands
    )


@pytest.mark.parametrize("probe", ("device", "processes"))
def test_nvidia_smi_binding_tamper_fails_before_probe_launch_or_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: str,
) -> None:
    authority_value = _authority("4" * 64)
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "tampered-nvidia-smi-authority.json",
        cast(JsonValue, authority_value),
    )
    authority = load_gpu_live_authority(authority_path, authority_sha)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", GPU0_UUID)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_hash_bound_executable",
        lambda _path, *, expected_sha256: (expected_sha256, 1_243_809),
    )
    boundaries = {"subprocess": 0, "popen": 0, "provider": 0}

    def forbidden_boundary(name: str) -> Callable[..., object]:
        def blocked(*_args: object, **_kwargs: object) -> object:
            boundaries[name] += 1
            raise AssertionError(f"{name} boundary reached after tool-binding mismatch")

        return blocked

    monkeypatch.setattr(
        gpu_live_smoke_module.subprocess,
        "run",
        forbidden_boundary("subprocess"),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.subprocess,
        "Popen",
        forbidden_boundary("popen"),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_invoke_openai_call",
        forbidden_boundary("provider"),
    )
    call: Callable[[], object] = (
        (lambda: gpu_live_smoke_module._inspect_gpu(authority))
        if probe == "device"
        else (
            lambda: gpu_live_smoke_module._inspect_gpu_processes(
                authority,
                owned_pids=set(),
            )
        )
    )
    _assert_error_code("GPU_SMOKE_GPU_PROBE_BINDING_INVALID", call)
    assert boundaries == {"subprocess": 0, "popen": 0, "provider": 0}


def test_authority_input_inspection_reports_exact_nvidia_smi_binding_without_gpu_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_runtime = tmp_path / "private-runtime"
    python_path = private_runtime / "bin/python3.12"
    python_path.parent.mkdir(parents=True)
    python_path.write_bytes(b"synthetic executable bytes")
    python_path.chmod(0o700)
    client_site_packages = private_runtime / "site-packages/client"
    server_site_packages = private_runtime / "site-packages/server"
    client_site_packages.mkdir(parents=True)
    server_site_packages.mkdir(parents=True)

    def fake_inventory(_receipt: Any, model_id: str, path: str) -> dict[str, JsonValue]:
        return {
            "snapshot_path": path,
            "resolved_snapshot_path": path,
            "entry_count": 1,
            "snapshot_tree_sha256": ("1" if model_id == "qwen3vl_8b" else "2") * 64,
            "snapshot_tree_byte_count": 1,
            "entries": [],
            "required_member_count": 1,
            "additional_artifact_count": 0,
            "hf_hub_offline": True,
            "local_files_only": True,
            "formal_model_immutability_proven": False,
            "toctou_free_model_binding_proven": False,
        }

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "load_live_preparation",
        lambda _path: SimpleNamespace(sha256=LIVE_PREPARATION_RECEIPT_SHA256),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_enumerate_snapshot_tree",
        fake_inventory,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_enumerate_bound_runtime_tree",
        lambda root, **_kwargs: {
            "root": root,
            "tree_sha256": _sha256(cast(str, root).encode()),
            "tree_entry_count": 1,
            "tree_byte_count": 1,
            "owner_uid": 0,
            "owner_gid": 0,
            "aggregate_owner_identity": "AUTHORIZED_OWNER",
            "numeric_owner_excluded_from_tree_digest": True,
            "directory_mode": 0o500,
            "regular_mode": 0o400,
            "executable_mode": 0o500,
            "symlinks_allowed": False,
            "hardlinks_allowed": False,
            "all_entries_nofollow_revalidated": True,
        },
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_distribution_versions",
        lambda _python, names: {
            name: {
                "openai": "1.106.1" if len(names) == 1 else "2.15.0",
                "vllm": "0.11.0",
                "torch": "2.8.0+cu126",
            }[name]
            for name in names
        },
    )
    subprocess_calls: list[list[str]] = []
    subprocess_environments: list[dict[str, str]] = []

    def fake_run(command: list[str], **kwargs: object) -> Any:
        subprocess_calls.append(command)
        subprocess_environments.append(cast(dict[str, str], kwargs["env"]))
        assert kwargs["timeout_seconds"] == 10
        assert kwargs["error_code"] == "GPU_SMOKE_SOURCE_BINDING_INVALID"
        stdout = ("6" * 40 + "\n").encode()
        return _synthetic_owned_command_result(command, stdout=stdout)

    monkeypatch.setattr(gpu_live_smoke_module, "_run_owned_command", fake_run)
    inspection = gpu_live_smoke_module.inspect_authority_inputs(
        model_config_manifest_path="/synthetic/model-config.json",
        qwen_snapshot_path="/synthetic/qwen-snapshot",
        mai_snapshot_path="/synthetic/mai-snapshot",
        client_python_path=str(python_path),
        server_python_path=str(python_path),
        client_site_packages_path=str(client_site_packages),
        server_site_packages_path=str(server_site_packages),
        outer_bootstrap_code_sha256="9" * 64,
        outer_bootstrap_code_byte_count=1,
    )
    implementation = cast(dict[str, dict[str, JsonValue]], inspection["implementation"])
    assert implementation["nvidia_smi"] == {
        "path": "/usr/bin/nvidia-smi",
        "sha256": ("cd4dc7637cd3ef30002cbf97afcc66f111eb90c0c615f37e264c392242eb51b6"),
        "byte_count": 1_243_808,
    }
    assert subprocess_calls == [
        [
            "/usr/bin/git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(REPOSITORY_ROOT),
            "rev-parse",
            "HEAD",
        ]
    ]
    assert subprocess_environments == [
        {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
        }
    ]
    assert inspection["gpu_probed"] is False
    assert inspection["socket_opened"] is False
    assert inspection["subprocess_started"] is True
    assert inspection["model_loaded"] is False
    source_inspection = cast(dict[str, JsonValue], inspection["source_inspection"])
    _schema_validator("gpu_smoke_owned_command.schema.json").validate(
        source_inspection["owned_command"]
    )


def test_gpu_process_census_reads_only_uid_starttime_and_never_signals_foreign_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_value = _authority("4" * 64)
    authority_path, authority_sha = _write_canonical_json(
        tmp_path,
        "gpu-process-authority.json",
        cast(JsonValue, authority_value),
    )
    authority = load_gpu_live_authority(authority_path, authority_sha)
    owned_pid = 41000
    foreign_pid = 51000
    stat_reads: list[str] = []
    starttime_reads: list[str] = []

    class UidAndStarttimeOnlyPath:
        def __init__(self, value: str) -> None:
            self.value = value

        def __truediv__(self, child: object) -> UidAndStarttimeOnlyPath:
            return UidAndStarttimeOnlyPath(f"{self.value}/{child}")

        def stat(self) -> Any:
            stat_reads.append(self.value)
            if self.value == f"/proc/{owned_pid}":
                return SimpleNamespace(st_uid=os.getuid())
            if self.value == f"/proc/{foreign_pid}":
                return SimpleNamespace(st_uid=os.getuid() + 1)
            pytest.fail(f"unexpected proc path: {self.value}")

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            starttime_reads.append(self.value)
            if self.value == f"/proc/{owned_pid}/stat":
                pid = owned_pid
                ticks = 410_000
            elif self.value == f"/proc/{foreign_pid}/stat":
                pid = foreign_pid
                ticks = 510_000
            else:
                pytest.fail(f"unexpected proc metadata path: {self.value}")
            fields = ["R", *("0" for _ in range(18)), str(ticks)]
            return f"{pid} (synthetic) " + " ".join(fields)

    def fake_run(command: list[str], **kwargs: object) -> Any:
        assert command == [
            "/usr/bin/nvidia-smi",
            f"--id={GPU0_UUID}",
            "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
            "--format=csv,noheader,nounits",
        ]
        assert kwargs == {
            "env": {"PATH": "/usr/bin:/bin"},
            "timeout_seconds": 15,
            "error_code": "GPU_SMOKE_GPU_PROCESS_PROBE_FAILED",
        }
        return _synthetic_owned_command_result(
            command,
            stdout=(
                f"{GPU0_UUID}, {owned_pid}, 1024\n"
                f"{GPU0_UUID}, {foreign_pid}, 2048\n"
                "GPU-00000000-0000-0000-0000-000000000000, 61000, 1\n"
            ).encode(),
        )

    signals: list[object] = []
    monkeypatch.setattr(gpu_live_smoke_module, "Path", UidAndStarttimeOnlyPath)
    monkeypatch.setattr(gpu_live_smoke_module, "_run_owned_command", fake_run)
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    snapshot = gpu_live_smoke_module._inspect_gpu_processes(
        authority,
        owned_pids={owned_pid},
    )
    assert snapshot["foreign_cmdlines_read"] == 0
    assert snapshot["foreign_environments_read"] == 0
    assert snapshot["signals_sent"] == 0
    assert snapshot["process_count"] == 2
    assert stat_reads == [
        f"/proc/{owned_pid}",
        f"/proc/{owned_pid}",
        f"/proc/{foreign_pid}",
        f"/proc/{foreign_pid}",
    ]
    assert starttime_reads == [
        f"/proc/{owned_pid}/stat",
        f"/proc/{owned_pid}/stat",
        f"/proc/{foreign_pid}/stat",
        f"/proc/{foreign_pid}/stat",
    ]
    assert cast(list[JsonValue], snapshot["processes"]) == [
        {
            "pid": owned_pid,
            "uid": os.getuid(),
            "starttime_ticks": 410_000,
            "used_gpu_memory_bytes": 1024 * 1024 * 1024,
            "classification": "OWN_LAUNCH",
        },
        {
            "pid": foreign_pid,
            "uid": os.getuid() + 1,
            "starttime_ticks": 510_000,
            "used_gpu_memory_bytes": 2048 * 1024 * 1024,
            "classification": "BASELINE_OR_FOREIGN",
        },
    ]
    assert signals == []


def test_gpu_service_isolation_requires_foreign_invariance_and_owned_release() -> None:
    own_pid = 41000
    foreign_pid = 51000
    foreign_uid = os.getuid() + 1
    baseline: dict[str, JsonValue] = {
        "processes": [
            {
                "pid": foreign_pid,
                "uid": foreign_uid,
                "starttime_ticks": 510_000,
                "used_gpu_memory_bytes": 2048,
                "classification": "BASELINE_OR_FOREIGN",
            }
        ]
    }
    with_own: dict[str, JsonValue] = {
        "processes": [
            *cast(list[JsonValue], baseline["processes"]),
            {
                "pid": own_pid,
                "uid": os.getuid(),
                "starttime_ticks": 410_000,
                "used_gpu_memory_bytes": 1024,
                "classification": "OWN_LAUNCH",
            },
        ]
    }
    active = gpu_live_smoke_module._assert_gpu_service_isolation(
        baseline,
        with_own,
        owned_pids={own_pid},
        require_owned_absent=False,
    )
    assert active["owned_gpu_pids_present"] == [own_pid]
    assert active["foreign_process_target_count"] == 0
    _assert_error_code(
        "GPU_SMOKE_OWN_GPU_ALLOCATION_REMAINS",
        lambda: gpu_live_smoke_module._assert_gpu_service_isolation(
            baseline,
            with_own,
            owned_pids={own_pid},
            require_owned_absent=True,
        ),
    )

    released = gpu_live_smoke_module._assert_gpu_service_isolation(
        baseline,
        baseline,
        owned_pids={own_pid},
        require_owned_absent=True,
    )
    assert released["owned_gpu_allocation_absent"] is True
    assert released["baseline_process_identities_preserved"] is True

    missing_foreign: dict[str, JsonValue] = {"processes": []}
    _assert_error_code(
        "GPU_SMOKE_FOREIGN_PROCESS_INVARIANCE_UNPROVEN",
        lambda: gpu_live_smoke_module._assert_gpu_service_isolation(
            baseline,
            missing_foreign,
            owned_pids={own_pid},
            require_owned_absent=True,
        ),
    )
    unexpected_current_uid: dict[str, JsonValue] = {
        "processes": [
            *cast(list[JsonValue], baseline["processes"]),
            {
                "pid": 42000,
                "uid": os.getuid(),
                "starttime_ticks": 420_000,
                "used_gpu_memory_bytes": 512,
                "classification": "BASELINE_OR_FOREIGN",
            },
        ]
    }
    _assert_error_code(
        "GPU_SMOKE_FOREIGN_PROCESS_INVARIANCE_UNPROVEN",
        lambda: gpu_live_smoke_module._assert_gpu_service_isolation(
            baseline,
            unexpected_current_uid,
            owned_pids={own_pid},
            require_owned_absent=True,
        ),
    )


def _block_real_live_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("CPU fake execution reached a real live boundary")

    for name in (
        "socket",
        "create_connection",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
    ):
        monkeypatch.setattr(socket, name, blocked)
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, blocked)
    for name in ("kill", "killpg", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, blocked)


def test_injected_executor_closes_exact_sequential_22_call_pass_evidence_cpu_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = _FakeGpuLiveOperations()
    _block_real_live_boundaries(monkeypatch)
    terminal = execute_gpu_live_smoke(
        authority_path=authority_path,
        authority_sha256=authority_sha,
        smoke_packet_path=packet_path,
        model_config_manifest_path=MODEL_CONFIG_PATH,
        operations=operations,
    )

    assert terminal["status"] == "PASS"
    assert terminal["logical_call_count"] == 22
    assert terminal["physical_http_request_count"] == 22
    assert terminal["sdk_hidden_retry_count"] == 0
    assert terminal["model_order"] == list(MODEL_ORDER)
    assert len(cast(list[JsonValue], terminal["call_receipts"])) == 22
    assert len(cast(list[JsonValue], terminal["lifecycle_receipts"])) == 2
    assert len(cast(list[JsonValue], terminal["snapshot_pre_receipts"])) == 2
    assert len(cast(list[JsonValue], terminal["snapshot_post_receipts"])) == 2
    assert len(cast(list[JsonValue], terminal["runtime_scratch_pre_receipts"])) == 2
    assert len(cast(list[JsonValue], terminal["runtime_scratch_post_receipts"])) == 2
    assert len(cast(list[JsonValue], terminal["server_environment_receipts"])) == 2
    assert terminal["network_namespace"] == terminal["client_environment"]
    assert terminal["launcher_scratch_post"] is not None
    assert terminal["evidence_closure_proven"] is True
    assert terminal["own_service_release_proven"] is True
    assert terminal["generated_action_executed"] is False
    assert terminal["replay_executed"] is False
    assert terminal["formal_model_immutability_proven"] is False
    assert terminal["toctou_free_model_binding_proven"] is False
    assert terminal["formal_runtime_immutability_proven"] is False
    assert terminal["toctou_free_runtime_binding_proven"] is False
    assert terminal["native_dt_needed_dependency_closure_proven"] is False
    assert terminal["runtime_tree_post"] is not None
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)
    assert set(terminal) == {
        "schema_version",
        "run_id",
        "status",
        "manifest",
        "decision_id",
        "authority_id",
        "authority_sha256",
        "smoke_packet_sha256",
        "started_at_utc",
        "finished_at_utc",
        "model_order",
        "logical_call_count",
        "physical_http_request_count",
        "sdk_hidden_retry_count",
        "lifecycle_receipts",
        "call_receipts",
        "snapshot_pre_receipts",
        "snapshot_post_receipts",
        "network_namespace",
        "runtime_scratch_pre_receipts",
        "runtime_scratch_post_receipts",
        "launcher_scratch_post",
        "runtime_tree_post",
        "stage1_preexec",
        "stage2_preimport",
        "client_environment",
        "server_environment_receipts",
        "gpu_preflight",
        "gpu_postflight",
        "operation_ledger",
        "schema_validation",
        "formal_model_immutability_proven",
        "toctou_free_model_binding_proven",
        "formal_runtime_immutability_proven",
        "toctou_free_runtime_binding_proven",
        "native_dt_needed_dependency_closure_proven",
        "generated_action_executed",
        "replay_executed",
        "evidence_closure_proven",
        "own_service_release_proven",
        "receipt_subject_sha256",
        "terminal_receipt",
        "run_relative_path",
        "terminal_file",
        "manifest_file",
    }
    locator_keys = {
        "terminal_receipt",
        "run_relative_path",
        "terminal_file",
        "manifest_file",
    }
    stored_terminal = _read_content_ref(
        evidence_root,
        cast(JsonValue, terminal["terminal_receipt"]),
        schema_name="gpu_smoke_execution.schema.json",
    )
    assert locator_keys.isdisjoint(stored_terminal)
    assert stored_terminal == {
        key: value for key, value in terminal.items() if key not in locator_keys
    }
    terminal_alias = (
        evidence_root
        / cast(str, terminal["run_relative_path"])
        / cast(str, terminal["terminal_file"])
    )
    terminal_bytes = terminal_alias.read_bytes()
    assert terminal_bytes == canonical_json_bytes(cast(JsonValue, stored_terminal))
    assert (
        _sha256(terminal_bytes)
        == cast(dict[str, JsonValue], terminal["terminal_receipt"])["sha256"]
    )
    _assert_schema_validation_receipt(
        evidence_root,
        cast(dict[str, JsonValue], terminal),
        status="PASS",
        event_count=33,
        call_count=22,
        lifecycle_count=2,
    )
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    assert ledger["model_launch_count"] == 2
    assert ledger["logical_call_count"] == 22
    assert ledger["physical_http_request_count"] == 22
    assert ledger["sdk_hidden_retry_count"] == 0
    assert ledger["signal_targets_subset_of_launch_trees"] is True
    assert ledger["network_namespace"] == terminal["network_namespace"]
    assert ledger["client_environment"] == terminal["client_environment"]
    assert ledger["runtime_scratch_pre_receipts"] == terminal["runtime_scratch_pre_receipts"]
    assert ledger["runtime_scratch_post_receipts"] == terminal["runtime_scratch_post_receipts"]
    assert ledger["server_environment_receipts"] == terminal["server_environment_receipts"]
    assert ledger["runtime_scratch_preserved_for_audit"] is True
    assert ledger["external_network_mechanically_unavailable"] is True
    assert ledger["server_log_capture_complete"] is True
    for key in (
        "foreign_process_target_count",
        "broad_process_signal_count",
        "non_loopback_connection_count",
        "credential_read_count",
        "generated_action_execution_count",
        "mobileworld_action_count",
        "replay_count",
        "backend_restore_count",
        "response_feedback_count",
    ):
        assert ledger[key] == 0
    _assert_pass_operation_evidence(
        evidence_root,
        cast(dict[str, JsonValue], terminal),
    )

    calls = [
        _read_content_ref(
            evidence_root,
            reference,
            schema_name="gpu_smoke_call.schema.json",
        )
        for reference in cast(list[JsonValue], terminal["call_receipts"])
    ]
    fixture = _load_fixture()
    assert [call["call_id"] for call in calls] == [
        descriptor["call_id"] for descriptor in fixture["expected_calls"]
    ]
    assert [call["ordinal"] for call in calls] == list(range(1, 23))
    call_keys = {
        "schema_version",
        "run_id",
        "call_id",
        "ordinal",
        "model_id",
        "phase",
        "seed",
        "repeat_index",
        "arm",
        "status",
        "application_request",
        "transmitted_request",
        "render_evidence",
        "raw_response",
        "response",
        "host_parser",
        "physical_request_count",
        "sdk_hidden_retry_count",
        "independent_client_state",
        "response_feedback_used",
        "generated_action_executed",
    }
    assert all(set(call) == call_keys for call in calls)
    assert all(
        call["status"] == "PASS"
        and call["physical_request_count"] == 1
        and call["sdk_hidden_retry_count"] == 0
        and call["independent_client_state"] is True
        and call["response_feedback_used"] is False
        and call["generated_action_executed"] is False
        for call in calls
    )
    assert all(call["render_evidence"] is None for call in calls if call["phase"] == "G1_4_CANARY")
    assert all(
        isinstance(call["render_evidence"], dict) for call in calls if call["phase"] == "G1_5_CODEC"
    )

    lifecycles = [
        _read_content_ref(
            evidence_root,
            reference,
            schema_name="gpu_smoke_lifecycle.schema.json",
        )
        for reference in cast(list[JsonValue], terminal["lifecycle_receipts"])
    ]
    lifecycle_keys = {
        "schema_version",
        "run_id",
        "model_ordinal",
        "model_id",
        "started_at_utc",
        "finished_at_utc",
        "status",
        "launch_plan_sha256",
        "guard",
        "readiness",
        "gpu_during",
        "cleanup",
        "gpu_after",
        "snapshot_pre",
        "snapshot_post",
        "runtime_scratch_pre",
        "runtime_scratch_post",
        "server_log",
        "server_environment",
        "immediate_launch_preflight",
        "foreign_process_target_count",
        "broad_signal_used",
    }
    assert [item["model_id"] for item in lifecycles] == list(MODEL_ORDER)
    assert all(set(item) == lifecycle_keys for item in lifecycles)
    assert all(
        item["status"] == "PASS"
        and item["foreign_process_target_count"] == 0
        and item["broad_signal_used"] is False
        and cast(dict[str, JsonValue], item["cleanup"])["port_released"] is True
        and cast(dict[str, JsonValue], item["cleanup"])["snapshot_pre_post_identical"] is True
        for item in lifecycles
    )

    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    event_files = sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    event_bytes = [path.read_bytes() for path in event_files]
    events = [cast(dict[str, JsonValue], json.loads(data)) for data in event_bytes]
    event_validator = _schema_validator("gpu_smoke_event.schema.json")
    for path, data, event in zip(event_files, event_bytes, events, strict=True):
        assert canonical_json_bytes(cast(JsonValue, event)) == data
        assert path.name == f"{cast(int, event['seq']):04d}-{_sha256(data)}.json"
        event_validator.validate(event)
    expected_model_events = [
        "MODEL_PREFLIGHT_VALIDATED",
        "SERVICE_LAUNCHED",
        "SERVICE_READY",
        *(["CALL_COMPLETED"] * 11),
        "SERVICE_LIFECYCLE_CLOSED",
    ]
    assert [event["event_kind"] for event in events] == [
        "RUN_STARTED",
        "PREFLIGHT_VALIDATED",
        *expected_model_events,
        *expected_model_events,
        "RUN_PASS_VALIDATED",
    ]
    assert [event["seq"] for event in events] == list(range(1, 34))
    manifest_path = run_dir / cast(str, terminal["manifest_file"])
    manifest_bytes = manifest_path.read_bytes()
    manifest = cast(dict[str, JsonValue], json.loads(manifest_bytes))
    assert canonical_json_bytes(cast(JsonValue, manifest)) == manifest_bytes
    assert manifest_path.name == f"manifest-{_sha256(manifest_bytes)}.json"
    assert _sha256(manifest_bytes) == cast(dict[str, JsonValue], terminal["manifest"])["sha256"]
    _schema_validator("gpu_smoke_manifest.schema.json").validate(manifest)
    assert manifest["exact_event_files"] == [path.name for path in event_files]
    assert manifest["event_count"] == 33
    assert manifest["content_object_count"] == len(
        cast(list[JsonValue], manifest["exact_content_objects"])
    )
    assert manifest["self_excluded_content_object_roles"] == [
        "MANIFEST_OBJECT",
        "TERMINAL_RECEIPT_OBJECT",
    ]
    assert manifest["self_excluded_content_object_count"] == 2
    assert manifest["content_object_census_rule"] == (
        "EXACT_PRESEAL_OBJECTS_PLUS_TWO_EXPLICIT_SELF_EXCLUSIONS"
    )
    listed_content_digests = {
        cast(str, cast(dict[str, JsonValue], item)["sha256"])
        for item in cast(list[JsonValue], manifest["exact_content_objects"])
    }
    excluded_content_digests = {
        cast(str, cast(dict[str, JsonValue], terminal[key])["sha256"])
        for key in ("manifest", "terminal_receipt")
    }
    assert listed_content_digests.isdisjoint(excluded_content_digests)
    for reference in cast(list[JsonValue], manifest["exact_content_objects"]):
        value = cast(dict[str, JsonValue], reference)
        data = (evidence_root / cast(str, value["relative_path"])).read_bytes()
        assert _sha256(data) == value["sha256"]
        assert len(data) == value["byte_count"]

    forbidden_secret_markers = (
        b"authorization:",
        b"bearer ",
        b'"api_key"',
        b'"api-key"',
        b'"x-api-key"',
        b'"access_token"',
    )
    for path in sorted(item for item in evidence_root.rglob("*") if item.is_file()):
        assert not path.is_symlink()
        data = path.read_bytes()
        lowered = data.lower()
        assert all(marker not in lowered for marker in forbidden_secret_markers)
        try:
            value = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        gpu_live_smoke_module._scan_for_secrets(cast(JsonValue, value))

    assert operations.invoke_count == 22
    assert operations.capacity_count == 6
    assert operations.port_check_count == 8
    assert operations.snapshot_counts == {
        "qwen3vl_8b": 2,
        "mai_ui_8b": 2,
    }
    assert (
        operations.trace.index("capacity:3")
        < operations.trace.index("start:qwen3vl_8b")
        < operations.trace.index("stop:qwen3vl_8b")
        < operations.trace.index("snapshot:qwen3vl_8b:2")
        < operations.trace.index("capacity:4")
        < operations.trace.index("capacity:5")
        < operations.trace.index("start:mai_ui_8b")
    )


def test_injected_executor_failure_is_terminal_no_retry_and_still_sealed_cpu_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = _FakeGpuLiveOperations(fail_at_call=3)
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.code == "GPU_SMOKE_PROVIDER_CALL_FAILED"
    terminal = raised.value.terminal_receipt
    assert terminal is not None
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)
    stored_terminal = _read_content_ref(
        evidence_root,
        cast(JsonValue, terminal["terminal_receipt"]),
        schema_name="gpu_smoke_execution.schema.json",
    )
    locator_keys = {
        "terminal_receipt",
        "run_relative_path",
        "terminal_file",
        "manifest_file",
    }
    assert locator_keys.isdisjoint(stored_terminal)
    assert stored_terminal == {
        key: value for key, value in terminal.items() if key not in locator_keys
    }
    _assert_schema_validation_receipt(
        evidence_root,
        cast(dict[str, JsonValue], terminal),
        status="FAIL",
        event_count=10,
        call_count=3,
        lifecycle_count=1,
    )
    assert terminal["status"] == "FAIL"
    assert terminal["error_code"] == "GPU_SMOKE_PROVIDER_CALL_FAILED"
    assert terminal["logical_call_count"] == 3
    assert terminal["physical_http_request_count"] == 3
    assert terminal["sdk_hidden_retry_count"] == 0
    assert len(cast(list[JsonValue], terminal["call_receipts"])) == 3
    assert len(cast(list[JsonValue], terminal["lifecycle_receipts"])) == 1
    assert operations.invoke_count == 3
    assert "stop:qwen3vl_8b" in operations.trace
    assert not any(item == "start:mai_ui_8b" for item in operations.trace)

    calls = [
        _read_content_ref(
            evidence_root,
            reference,
            schema_name="gpu_smoke_call.schema.json",
        )
        for reference in cast(list[JsonValue], terminal["call_receipts"])
    ]
    assert [call["status"] for call in calls] == ["PASS", "PASS", "FAIL"]
    failure = calls[-1]
    assert failure["failure_stage"] == "PRE_RESPONSE"
    assert failure["application_visible_attempt_count"] == 1
    assert failure["physical_request_count"] == 1
    assert failure["physical_request_count_upper_bound"] == 1
    assert failure["sdk_hidden_retry_count"] == 0
    assert failure["error_code"] == "GPU_SMOKE_PROVIDER_CALL_FAILED"
    assert failure["generated_action_executed"] is False
    assert set(failure) == {
        "schema_version",
        "run_id",
        "call_id",
        "ordinal",
        "model_id",
        "phase",
        "seed",
        "repeat_index",
        "arm",
        "status",
        "failure_stage",
        "application_request",
        "transmitted_request",
        "raw_response",
        "application_visible_attempt_count",
        "physical_request_count",
        "physical_request_count_upper_bound",
        "sdk_hidden_retry_count",
        "error_code",
        "host_parser",
        "response_feedback_used",
        "generated_action_executed",
    }
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    events = [
        cast(dict[str, JsonValue], json.loads(path.read_bytes()))
        for path in sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    ]
    assert [event["event_kind"] for event in events] == [
        "RUN_STARTED",
        "PREFLIGHT_VALIDATED",
        "MODEL_PREFLIGHT_VALIDATED",
        "SERVICE_LAUNCHED",
        "SERVICE_READY",
        "CALL_COMPLETED",
        "CALL_COMPLETED",
        "CALL_FAILED",
        "SERVICE_LIFECYCLE_CLOSED",
        "RUN_FAILED",
    ]


def test_provisional_failed_launch_freeze_is_persisted_before_any_cleanup_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    order: list[str] = []
    operations = _FailedLaunchGpuLiveOperations(order)
    original_event = gpu_live_smoke_module._EvidenceStore.event

    def ordered_event(
        store: Any,
        event_kind: str,
        payload: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        result = original_event(store, event_kind, payload)
        order.append(f"EVENT:{event_kind}")
        return result

    monkeypatch.setattr(gpu_live_smoke_module._EvidenceStore, "event", ordered_event)
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.code == "GPU_SMOKE_SERVER_GUARD_CAPTURE_FAILED"
    assert raised.value.terminal_receipt is not None, (
        raised.value.code,
        str(raised.value),
        raised.value.execution_detail,
    )
    terminal = cast(dict[str, JsonValue], raised.value.terminal_receipt)
    assert terminal["status"] == "FAIL"
    assert terminal["evidence_closure_proven"] is True
    assert terminal["own_service_release_proven"] is True
    assert operations.close_count == 1
    assert order.index("EVENT:PROVISIONAL_ACQUISITION_FROZEN") < order.index("SIGNAL")
    assert "EVENT:SERVICE_LAUNCHED" not in order
    assert order[-1] == "EVENT:RUN_FAILED"
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    events = [
        cast(dict[str, JsonValue], json.loads(path.read_bytes()))
        for path in sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    ]
    kinds = [cast(str, item["event_kind"]) for item in events]
    assert kinds.index("PROVISIONAL_ACQUISITION_FROZEN") < kinds.index(
        "PROVISIONAL_ACQUISITION_CLEANUP"
    )
    for event in events:
        _schema_validator("gpu_smoke_event.schema.json").validate(event)


def test_failed_launch_freeze_failure_sends_nothing_and_marks_closure_unproven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = _UnfrozenFailedLaunchGpuLiveOperations([])
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.terminal_receipt is not None, (
        raised.value.code,
        str(raised.value),
        raised.value.execution_detail,
    )
    terminal = cast(dict[str, JsonValue], raised.value.terminal_receipt)
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    assert operations.freeze_count == 2
    assert operations.cleanup_count == 0
    assert operations.close_count == 1
    assert ledger["signal_intent_count"] == 0
    assert ledger["signal_sent_count"] == 0
    assert ledger["signal_target_pids"] == []
    assert ledger["server_log_capture_complete"] is False
    assert terminal["evidence_closure_proven"] is False
    assert terminal["own_service_release_proven"] is False
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    events = [
        cast(dict[str, JsonValue], json.loads(path.read_bytes()))
        for path in sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    ]
    kinds = [item["event_kind"] for item in events]
    assert "PROVISIONAL_ACQUISITION_FROZEN" not in kinds
    assert "PROVISIONAL_ACQUISITION_CLEANUP" not in kinds
    assert "SERVER_LOG_CAPTURE_SKIPPED_UNCLOSED_WRITER" in kinds
    credential_receipts = [
        _read_content_ref(evidence_root, reference)
        for reference in cast(list[JsonValue], ledger["credential_scan_receipts"])
    ]
    assert all(item["artifact_kind"] != "SERVER_LOG" for item in credential_receipts)
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)


def test_primary_partial_cleanup_failure_is_retained_and_outer_retry_closes_exact_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PartialCleanupFailureOperations(_FakeGpuLiveOperations):
        def __init__(self) -> None:
            super().__init__()
            self.stop_attempt_count = 0

        def stop_service(self, guard: Any, process: Any) -> dict[str, JsonValue]:
            assert process.pid == guard.root.pid
            assert len(guard.service_tree) == 2
            self.stop_attempt_count += 1
            root, child = guard.service_tree
            if self.stop_attempt_count == 1:
                self.active_pids.discard(child.pid)
                partial_trace: list[JsonValue] = []
                for state in (
                    "INTENDED",
                    "PIDFD_OPENED",
                    "IDENTITY_REVALIDATED",
                    "SENT",
                ):
                    partial_trace.append(
                        {
                            "sequence": len(partial_trace) + 1,
                            "pid": child.pid,
                            "starttime_ticks": child.starttime_ticks,
                            "signal": "SIGTERM",
                            "state": state,
                            "signal_api": "PIDFD",
                            "ownership": "RECORDED_OWN",
                        }
                    )
                error = GpuLiveSmokeError(
                    "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED",
                    "synthetic failure after one exact pidfd signal",
                )
                error.execution_detail = {
                    "term_pids": [child.pid],
                    "kill_pids": [],
                    "signal_trace": partial_trace,
                }
                raise error

            self.active_pids.clear()
            self.stopped_models.add(guard.model_id)
            retry_trace: list[JsonValue] = []
            for state in (
                "INTENDED",
                "PIDFD_OPENED",
                "IDENTITY_REVALIDATED",
                "SENT",
            ):
                retry_trace.append(
                    {
                        "sequence": len(retry_trace) + 1,
                        "pid": root.pid,
                        "starttime_ticks": root.starttime_ticks,
                        "signal": "SIGTERM",
                        "state": state,
                        "signal_api": "PIDFD",
                        "ownership": "RECORDED_OWN",
                    }
                )
            return {
                "term_pids": [root.pid],
                "kill_pids": [],
                "foreign_processes_signaled": 0,
                "broad_signal_used": False,
                "port_released": True,
                "process_group_released": True,
                "process_session_released": True,
                "signal_trace": retry_trace,
            }

    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = PartialCleanupFailureOperations()
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.code == "GPU_SMOKE_OWN_SERVICE_CLEANUP_FAILED"
    assert operations.stop_attempt_count == 2
    terminal = cast(dict[str, JsonValue], raised.value.terminal_receipt)
    assert terminal["status"] == "FAIL"
    assert terminal["evidence_closure_proven"] is True
    assert terminal["own_service_release_proven"] is True
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    assert ledger["signal_target_pids"] == [41000, 41001]
    assert ledger["signal_sent_pids"] == [41000, 41001]
    assert ledger["signal_intent_count"] == 2
    assert ledger["signal_sent_count"] == 2
    assert ledger["foreign_process_target_count"] == 0
    assert ledger["broad_process_signal_count"] == 0
    assert ledger["server_log_capture_complete"] is True
    trace_receipt = _read_content_ref(evidence_root, ledger["signal_trace"])
    trace_events = cast(list[dict[str, JsonValue]], trace_receipt["events"])
    assert [item["global_sequence"] for item in trace_events] == list(
        range(1, len(trace_events) + 1)
    )
    assert {item["cleanup_attempt"] for item in trace_events} == {
        "model-1-primary-failed",
        "emergency",
    }
    assert all(item["signal_api"] == "PIDFD" for item in trace_events)
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    event_kinds = [
        cast(dict[str, JsonValue], json.loads(path.read_bytes()))["event_kind"]
        for path in sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    ]
    assert event_kinds.index("SERVICE_CLEANUP_ATTEMPT_FAILED") < event_kinds.index(
        "EMERGENCY_OWN_SERVICE_CLEANUP"
    )
    assert event_kinds[-1] == "RUN_FAILED"
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)


def test_model_stage_owned_command_signal_trace_is_consumed_once_across_outer_rethrow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ModelStageOwnedCommandFailureOperations(_FakeGpuLiveOperations):
        def validate_immediate_launch_preflight(
            self,
            authority: Any,
            baseline_gpu_processes: dict[str, JsonValue],
        ) -> dict[str, JsonValue]:
            del authority, baseline_gpu_processes
            receipt = _synthetic_owned_command_receipt(
                ["/usr/bin/nvidia-smi", "--synthetic-model-stage"],
            )
            receipt["completion_reason"] = "TIMEOUT"
            receipt["returncode"] = None
            identity = cast(dict[str, JsonValue], receipt["launch_gate_identity"])
            trace: list[JsonValue] = []
            for state in ("INTENDED", "IDENTITY_REVALIDATED", "SENT"):
                trace.append(
                    {
                        "sequence": len(trace) + 1,
                        "pid": identity["pid"],
                        "starttime_ticks": identity["starttime_ticks"],
                        "signal": "SIGKILL",
                        "state": state,
                        "signal_api": "PIDFD",
                        "ownership": "RECORDED_OWN_ACQUISITION",
                        "reason": "TIMEOUT",
                        "scope": "OWNED_AUXILIARY_COMMAND",
                    }
                )
            receipt["signal_trace"] = trace
            error = GpuLiveSmokeError(
                "GPU_SMOKE_GPU_PROBE_FAILED",
                "synthetic model-stage auxiliary timeout",
            )
            error.execution_detail = {"owned_command": receipt}
            raise error

    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = ModelStageOwnedCommandFailureOperations()
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    terminal = cast(dict[str, JsonValue], raised.value.terminal_receipt)
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    assert ledger["signal_intent_count"] == 1
    assert ledger["signal_sent_count"] == 1
    assert ledger["signal_target_pids"] == [61_000]
    assert ledger["signal_sent_pids"] == [61_000]
    signal_receipt = _read_content_ref(evidence_root, ledger["signal_trace"])
    signal_events = cast(list[dict[str, JsonValue]], signal_receipt["events"])
    assert [item["global_sequence"] for item in signal_events] == [1, 2, 3]
    assert [item["state"] for item in signal_events] == [
        "INTENDED",
        "IDENTITY_REVALIDATED",
        "SENT",
    ]
    owned_refs = cast(list[JsonValue], ledger["owned_command_receipts"])
    assert ledger["owned_command_receipt_count"] == len(owned_refs) == 1
    stored_owned_receipt = _read_content_ref(
        evidence_root,
        owned_refs[0],
        schema_name="gpu_smoke_owned_command.schema.json",
    )
    assert stored_owned_receipt["completion_reason"] == "TIMEOUT"
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    event_count = len(tuple(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json")))
    _assert_schema_validation_receipt(
        evidence_root,
        terminal,
        status="FAIL",
        event_count=event_count,
        call_count=0,
        lifecycle_count=0,
    )
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)


def test_unpersisted_launch_and_cleanup_freeze_never_reach_stop_or_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoUnfrozenStopOperations(_FakeGpuLiveOperations):
        def __init__(self) -> None:
            super().__init__()
            self.stop_attempt_count = 0

        def stop_service(self, guard: Any, process: Any) -> dict[str, JsonValue]:
            del guard, process
            self.stop_attempt_count += 1
            raise AssertionError("no signal-capable cleanup may run without persisted freeze")

    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = NoUnfrozenStopOperations()
    original_event = gpu_live_smoke_module._EvidenceStore.event

    def fail_cleanup_eligibility_persistence(
        store: Any,
        event_kind: str,
        payload: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        if event_kind in {
            "SERVICE_LAUNCHED",
            "SERVICE_TREE_FROZEN_FOR_FAILED_START_CLEANUP",
        }:
            raise GpuLiveSmokeError(
                "GPU_SMOKE_EVIDENCE_WRITE_FAILED",
                "synthetic cleanup-eligibility persistence failure",
            )
        return original_event(store, event_kind, payload)

    monkeypatch.setattr(
        gpu_live_smoke_module._EvidenceStore,
        "event",
        fail_cleanup_eligibility_persistence,
    )
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.code == "GPU_SMOKE_EVIDENCE_WRITE_FAILED"
    assert operations.stop_attempt_count == 0
    terminal = cast(dict[str, JsonValue], raised.value.terminal_receipt)
    ledger = cast(dict[str, JsonValue], terminal["operation_ledger"])
    assert ledger["signal_target_pids"] == []
    assert ledger["signal_sent_pids"] == []
    assert ledger["signal_intent_count"] == 0
    assert ledger["signal_sent_count"] == 0
    assert ledger["server_log_capture_complete"] is False
    assert terminal["evidence_closure_proven"] is False
    assert terminal["own_service_release_proven"] is False
    run_dir = evidence_root / cast(str, terminal["run_relative_path"])
    events = [
        cast(dict[str, JsonValue], json.loads(path.read_bytes()))
        for path in sorted(run_dir.glob("[0-9][0-9][0-9][0-9]-*.json"))
    ]
    kinds = [event["event_kind"] for event in events]
    assert "SERVICE_LAUNCHED" not in kinds
    assert "SERVICE_TREE_FROZEN_FOR_FAILED_START_CLEANUP" not in kinds
    assert "EMERGENCY_OWN_SERVICE_CLEANUP" not in kinds
    assert "EMERGENCY_CLEANUP_FAILED" in kinds
    assert "SERVER_LOG_CAPTURE_SKIPPED_UNCLOSED_WRITER" in kinds
    assert kinds[-1] == "RUN_FAILED"
    credential_receipts = [
        _read_content_ref(evidence_root, reference)
        for reference in cast(list[JsonValue], ledger["credential_scan_receipts"])
    ]
    assert all(item["artifact_kind"] != "SERVER_LOG" for item in credential_receipts)
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)


def test_preseal_schema_validation_reads_referenced_on_disk_call_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_path, authority_sha, packet_path, _evidence_root = _execution_files(tmp_path)
    operations = _FakeGpuLiveOperations()
    _block_real_live_boundaries(monkeypatch)
    original_seal = gpu_live_smoke_module._EvidenceStore.seal
    invalid_references: list[dict[str, JsonValue]] = []

    def seal_with_invalid_call_object(
        store: Any,
        status: str,
        terminal_payload: dict[str, JsonValue],
        *,
        validation_documents: dict[str, dict[str, JsonValue]] | None = None,
    ) -> dict[str, JsonValue]:
        if status == "PASS" and not invalid_references:
            invalid_reference = store.object(
                canonical_json_bytes(
                    {
                        "schema_version": "mobileworld.g1.gpu-live-smoke-call/v1",
                        "status": "PASS",
                    }
                ),
                "application/json",
            )
            invalid_references.append(invalid_reference)
            call_references = cast(
                list[JsonValue],
                terminal_payload["call_receipts"],
            )
            call_references[0] = invalid_reference
        return original_seal(
            store,
            status,
            terminal_payload,
            validation_documents=validation_documents,
        )

    monkeypatch.setattr(
        gpu_live_smoke_module._EvidenceStore,
        "seal",
        seal_with_invalid_call_object,
    )
    _assert_error_code(
        "GPU_SMOKE_EVIDENCE_TERMINAL_INVALID",
        lambda: execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        ),
    )
    assert len(invalid_references) == 1


def test_post_response_parser_failure_retains_raw_http_evidence_and_validates_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PostResponseFailureOperations(_FakeGpuLiveOperations):
        def parse_inert(self, model_id: str, content: str) -> dict[str, JsonValue]:
            self.trace.append(f"parse:{model_id}")
            return {
                "classification": "HOST_UNPARSEABLE_INERT_TEXT",
                "error_class": None,
                "parsed_action": None,
                "content_sha256": _sha256(content.encode()),
                "generated_action_executed": False,
            }

    authority_path, authority_sha, packet_path, evidence_root = _execution_files(tmp_path)
    operations = PostResponseFailureOperations()
    _block_real_live_boundaries(monkeypatch)
    with pytest.raises(GpuLiveSmokeError) as raised:
        execute_gpu_live_smoke(
            authority_path=authority_path,
            authority_sha256=authority_sha,
            smoke_packet_path=packet_path,
            model_config_manifest_path=MODEL_CONFIG_PATH,
            operations=operations,
        )
    assert raised.value.code == "GPU_SMOKE_HOST_PARSE_FAILED"
    terminal = raised.value.terminal_receipt
    assert terminal is not None
    _schema_validator("gpu_smoke_execution_locator.schema.json").validate(terminal)
    references = cast(list[JsonValue], terminal["call_receipts"])
    assert len(references) == 1
    failure = _read_content_ref(
        evidence_root,
        references[0],
        schema_name="gpu_smoke_call.schema.json",
    )
    assert failure["status"] == "FAIL"
    assert failure["failure_stage"] == "POST_RESPONSE"
    assert isinstance(failure["raw_response"], dict)
    assert isinstance(failure["response"], dict)
    assert isinstance(failure["host_parser"], dict)
    assert failure["independent_client_state"] is True
    assert failure["physical_request_count"] == 1
    assert failure["physical_request_count_upper_bound"] == 1
    assert failure["sdk_hidden_retry_count"] == 0
    assert failure["generated_action_executed"] is False
    raw_reference = cast(dict[str, JsonValue], failure["raw_response"])
    raw_bytes = (evidence_root / cast(str, raw_reference["relative_path"])).read_bytes()
    assert _sha256(raw_bytes) == raw_reference["sha256"]
    assert len(raw_bytes) == raw_reference["byte_count"]
    assert operations.invoke_count == 1


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("uid", os.getuid() + 1),
        ("pid", 41001),
        ("pgid", 41001),
        ("sid", 41001),
        ("starttime_ticks", 90_001),
        ("executable_path", "/synthetic/other/python"),
        ("executable_sha256", "b" * 64),
        ("argv", ("/synthetic/other/python",)),
    ),
)
def test_exact_process_identity_rejects_every_pid_reuse_or_binding_drift(
    field: str,
    replacement: object,
) -> None:
    expected = _process_identity()
    actual = replace(expected, **{field: replacement})
    assert gpu_live_smoke_module._same_process(expected, expected)
    assert not gpu_live_smoke_module._same_process(actual, expected)


def test_popen_success_then_pidfd_open_failure_retains_exact_handle_without_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _packet = _loaded_inputs(tmp_path)
    process = _FakeProcess(41000)
    environment = {
        "PATH": "/usr/bin:/bin",
        "CUDA_VISIBLE_DEVICES": GPU0_UUID,
        "SYNTHETIC_CPU_TEST": "1",
    }
    plan = SimpleNamespace(
        model_id="qwen3vl_8b",
        snapshot_path=(
            "/synthetic/hf-cache/models--Qwen--Qwen3-VL-8B-Instruct/snapshots/"
            f"{MODEL_REVISIONS['qwen3vl_8b']}"
        ),
        argv=(
            "/synthetic/server/bin/python",
            "-m",
            "vllm.entrypoints.cli.main",
            "--synthetic",
        ),
    )
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakeProcess:
        popen_calls.append((command, kwargs))
        return process

    monkeypatch.setattr(gpu_live_smoke_module, "_assert_port_free", lambda: None)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_verify_scratch_directory",
        lambda _path, *, create: None,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_server_environment",
        lambda _authority, _scratch: environment.copy(),
    )
    monkeypatch.setattr(gpu_live_smoke_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_open",
        lambda _pid: (_ for _ in ()).throw(OSError("synthetic pidfd_open failure")),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_send_signal",
        lambda *_args: pytest.fail("no signal is authorized before a persisted freeze"),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "kill",
        lambda *_args: pytest.fail("numeric PID signaling is forbidden"),
    )
    with pytest.raises(GpuLiveSmokeError) as raised:
        gpu_live_smoke_module._start_server(
            authority,
            plan,
            cast(Any, object()),
            "/synthetic/runtime-scratch/g1-gpu-smoke/run/qwen3vl_8b",
            environment,
        )
    assert raised.value.code == "GPU_SMOKE_SERVER_GUARD_CAPTURE_FAILED"
    handle = raised.value.failed_launch_cleanup_handle
    assert handle is not None
    assert handle.process is process
    assert handle.acquisition_pidfd == -1
    assert handle.root_minimal is None
    assert handle.recorded_tree == ()
    assert handle.evidence_frozen is False
    assert raised.value.execution_detail == {
        "cause_code": "GPU_SMOKE_UNCLASSIFIED_FAILURE",
        "acquisition_pid": process.pid,
        "acquisition_pidfd_open": False,
        "minimal_identity_captured": False,
        "recorded_tree_count": 0,
        "service_launched": False,
    }
    assert len(popen_calls) == 1
    command, kwargs = popen_calls[0]
    assert command[:6] == [
        "/synthetic/private-runtime/g1-gpu-smoke/bin/python3.12",
        "-I",
        "-S",
        "-B",
        "-X",
        "pycache_prefix=/dev/null",
    ]
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert kwargs["shell"] is False
    assert kwargs["env"] == environment


def test_signal_guard_targets_only_exact_current_uid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _process_identity()
    pidfd_signals: list[tuple[int, object]] = []
    closed_fds: list[int] = []
    trace: list[dict[str, JsonValue]] = []
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pin_exact_owned_process",
        lambda actual: (77, 78, actual),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_send_signal",
        lambda pidfd, sig: pidfd_signals.append((pidfd, sig)) or True,
    )
    monkeypatch.setattr(os, "close", lambda fd: closed_fds.append(fd))
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill fallback is forbidden"))
    assert gpu_live_smoke_module._signal_exact_member(
        expected,
        gpu_live_smoke_module.signal.SIGTERM,
        trace,
    )
    assert pidfd_signals == [(77, gpu_live_smoke_module.signal.SIGTERM)]
    assert closed_fds == [78, 77]
    assert [item["state"] for item in trace] == [
        "INTENDED",
        "PIDFD_OPENED",
        "IDENTITY_REVALIDATED",
        "SENT",
    ]

    pidfd_signals.clear()
    closed_fds.clear()
    trace.clear()

    def reject_reused_pid(_actual: Any) -> NoReturn:
        raise GpuLiveSmokeError(
            "GPU_SMOKE_PROCESS_TREE_DRIFT",
            "synthetic pidfd/procdir identity mismatch",
        )

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pin_exact_owned_process",
        reject_reused_pid,
    )
    _assert_error_code(
        "GPU_SMOKE_PROCESS_TREE_DRIFT",
        lambda: gpu_live_smoke_module._signal_exact_member(
            expected,
            gpu_live_smoke_module.signal.SIGTERM,
            trace,
        ),
    )
    assert pidfd_signals == []
    assert closed_fds == []
    assert [item["state"] for item in trace] == ["INTENDED"]

    foreign_expected = replace(expected, uid=os.getuid() + 1)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pin_exact_owned_process",
        lambda _actual: pytest.fail("foreign target reached procdir-sensitive pinning"),
    )
    _assert_error_code(
        "GPU_SMOKE_FOREIGN_PROCESS_SIGNAL_FORBIDDEN",
        lambda: gpu_live_smoke_module._signal_exact_member(
            foreign_expected,
            gpu_live_smoke_module.signal.SIGTERM,
            [],
        ),
    )
    assert pidfd_signals == []


def test_pidfd_signal_negative_paths_never_fall_back_to_numeric_pid_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _process_identity()
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill fallback is forbidden"))

    sends: list[tuple[int, object]] = []
    closed: list[int] = []
    trace: list[dict[str, JsonValue]] = []
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pin_exact_owned_process",
        lambda _expected: None,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pidfd_send_signal",
        lambda pidfd, sig: sends.append((pidfd, sig)) or True,
    )
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    assert not gpu_live_smoke_module._signal_exact_member(
        expected,
        gpu_live_smoke_module.signal.SIGTERM,
        trace,
    )
    assert sends == []
    assert closed == []
    assert [item["state"] for item in trace] == ["INTENDED", "EXITED_BEFORE_PIDFD_OPEN"]

    trace.clear()
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_pin_exact_owned_process",
        lambda _expected: (91, 92, expected),
    )
    monkeypatch.setattr(gpu_live_smoke_module, "_pidfd_send_signal", lambda _fd, _sig: False)
    assert not gpu_live_smoke_module._signal_exact_member(
        expected,
        gpu_live_smoke_module.signal.SIGKILL,
        trace,
    )
    assert closed == [92, 91]
    assert [item["state"] for item in trace] == [
        "INTENDED",
        "PIDFD_OPENED",
        "IDENTITY_REVALIDATED",
        "EXITED_BEFORE_SIGNAL",
    ]


def test_owned_tree_validation_rejects_foreign_member_before_any_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    child = _process_identity(
        pid=41001,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = replace(_ownership_guard(root), service_tree=(root, child))
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_current_recorded_process_tree",
        lambda _tree, **_kwargs: (root, child),
    )
    assert gpu_live_smoke_module._validate_owned_members(guard, require_root=True) == (root, child)

    foreign = replace(
        child,
        pid=41002,
        uid=os.getuid() + 1,
        ppid=999,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_current_recorded_process_tree",
        lambda _tree, **_kwargs: (root, foreign),
    )
    signals: list[object] = []
    monkeypatch.setattr(
        os,
        "kill",
        lambda *args: signals.append(args),
    )
    _assert_error_code(
        "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
        lambda: gpu_live_smoke_module._validate_owned_members(guard, require_root=True),
    )
    assert signals == []


def test_owned_child_foreign_uid_is_rejected_before_command_line_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid = 41002
    sensitive_reads: list[str] = []
    closed: list[int] = []
    monkeypatch.setattr(gpu_live_smoke_module, "_pidfd_open", lambda candidate: 91)

    def open_procdir(path: str, *_args: object, **_kwargs: object) -> int:
        assert path == f"/proc/{pid}"
        return 92

    monkeypatch.setattr(gpu_live_smoke_module.os, "open", open_procdir)
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "fstat",
        lambda fd: SimpleNamespace(st_uid=os.getuid() + 1)
        if fd == 92
        else pytest.fail("unexpected descriptor"),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_minimal_identity_from_procdir",
        lambda *_args: sensitive_reads.append("stat")
        or pytest.fail("foreign stat must not be read"),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_full_identity_from_procdir",
        lambda *_args: sensitive_reads.append("cmdline/exe")
        or pytest.fail("foreign cmdline/executable must not be read"),
    )
    monkeypatch.setattr(os, "close", lambda fd: closed.append(fd))
    _assert_error_code(
        "GPU_SMOKE_PROCESS_OWNERSHIP_MISMATCH",
        lambda: gpu_live_smoke_module._open_owned_process_handles(pid, os.getuid()),
    )
    assert sensitive_reads == []
    assert closed == [92, 91]


def test_service_tree_binding_requires_root_ancestry_and_then_closes_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    worker = _process_identity(
        pid=41001,
        ppid=root.pid,
        pgid=41001,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = _ownership_guard(root)
    current_recorded_process_tree = gpu_live_smoke_module._current_recorded_process_tree
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_discover_owned_process_tree",
        lambda _root: (root, worker),
    )
    bound = gpu_live_smoke_module._bind_service_tree(guard)
    assert bound.service_tree == (root, worker)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_current_recorded_process_tree",
        lambda tree, **_kwargs: tree,
    )
    assert gpu_live_smoke_module._validate_owned_members(
        bound,
        require_root=True,
    ) == (root, worker)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_current_recorded_process_tree",
        current_recorded_process_tree,
    )

    unrecorded = _process_identity(
        pid=41002,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 2,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_exact_owned_process_identity",
        lambda expected, **_kwargs: {
            root.pid: root,
            worker.pid: worker,
        }[expected.pid],
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_owned_task_children",
        lambda member: ((worker.pid, unrecorded.pid) if member.pid == root.pid else ()),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_new_owned_child_identity",
        lambda child_pid, _parent, _launch_root: (
            unrecorded if child_pid == unrecorded.pid else worker
        ),
    )
    _assert_error_code(
        "GPU_SMOKE_PROCESS_TREE_DRIFT",
        lambda: gpu_live_smoke_module._current_recorded_process_tree(bound.service_tree),
    )


def test_service_tree_binding_rejects_same_session_non_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    unrelated = _process_identity(
        pid=41001,
        ppid=39999,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = _ownership_guard(root)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_exact_owned_process_identity",
        lambda expected, **_kwargs: root if expected.pid == root.pid else unrelated,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_owned_task_children",
        lambda member: (unrelated.pid,) if member.pid == root.pid else (),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_read_new_owned_child_identity",
        lambda _child_pid, _parent, _launch_root: unrelated,
    )
    _assert_error_code(
        "GPU_SMOKE_PROCESS_TREE_INVALID",
        lambda: gpu_live_smoke_module._bind_service_tree(guard),
    )


def test_owned_service_cleanup_refuses_unbound_tree_before_any_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    guard = _ownership_guard(root)
    signals: list[object] = []
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))

    class FakeProcess:
        def poll(self) -> None:
            return None

    _assert_error_code(
        "GPU_SMOKE_PROCESS_TREE_UNBOUND",
        lambda: gpu_live_smoke_module._stop_owned_service(
            guard,
            cast(Any, FakeProcess()),
        ),
    )
    assert signals == []


def test_owned_service_cleanup_signals_only_recorded_tree_and_releases_before_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    child = _process_identity(
        pid=41001,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = replace(_ownership_guard(root), service_tree=(root, child))
    validation_call_count = 0

    def fake_validate(
        _guard: Any,
        *,
        require_root: bool,
        allow_reparented_descendants: bool = False,
    ) -> tuple[Any, ...]:
        nonlocal validation_call_count
        del require_root, allow_reparented_descendants
        validation_call_count += 1
        return (root, child) if validation_call_count == 1 else ()

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_validate_owned_members",
        fake_validate,
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_listening_socket_inodes",
        lambda: set(),
    )
    signaled: list[tuple[int, object]] = []

    def fake_signal(member: Any, sig: Any, trace: list[dict[str, JsonValue]]) -> bool:
        signaled.append((member.pid, sig))
        for state in ("INTENDED", "PIDFD_OPENED", "IDENTITY_REVALIDATED", "SENT"):
            gpu_live_smoke_module._signal_trace_event(trace, member, sig, state)
        return True

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_signal_exact_member",
        fake_signal,
    )
    port_checks: list[bool] = []
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_assert_port_free",
        lambda: port_checks.append(True),
    )

    class FakeProcess:
        def poll(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            assert timeout == 10
            return 0

    receipt = gpu_live_smoke_module._stop_owned_service(
        guard,
        cast(Any, FakeProcess()),
    )
    assert signaled == [
        (child.pid, gpu_live_smoke_module.signal.SIGTERM),
        (root.pid, gpu_live_smoke_module.signal.SIGTERM),
    ]
    signal_trace = cast(list[JsonValue], receipt.pop("signal_trace"))
    assert receipt == {
        "term_pids": [child.pid, root.pid],
        "kill_pids": [],
        "foreign_processes_signaled": 0,
        "broad_signal_used": False,
        "port_released": True,
        "process_group_released": True,
        "process_session_released": True,
    }
    assert len(signal_trace) == 8
    assert [cast(dict[str, JsonValue], item)["state"] for item in signal_trace] == [
        "INTENDED",
        "PIDFD_OPENED",
        "IDENTITY_REVALIDATED",
        "SENT",
        "INTENDED",
        "PIDFD_OPENED",
        "IDENTITY_REVALIDATED",
        "SENT",
    ]
    assert port_checks == [True]


def test_owned_service_cleanup_handles_exited_root_and_reparented_recorded_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    child = _process_identity(
        pid=root.pid + 1,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = replace(_ownership_guard(root), service_tree=(root, child))
    validations: list[tuple[bool, bool]] = []

    def fake_validate(
        _guard: Any,
        *,
        require_root: bool,
        allow_reparented_descendants: bool = False,
    ) -> tuple[Any, ...]:
        validations.append((require_root, allow_reparented_descendants))
        return (child,) if len(validations) == 1 else ()

    monkeypatch.setattr(gpu_live_smoke_module, "_validate_owned_members", fake_validate)
    monkeypatch.setattr(gpu_live_smoke_module, "_listening_socket_inodes", lambda: set())
    monkeypatch.setattr(gpu_live_smoke_module, "_assert_port_free", lambda: None)
    signaled: list[int] = []

    def fake_signal(
        member: Any,
        sig: Any,
        trace: list[dict[str, JsonValue]],
    ) -> bool:
        assert sig == gpu_live_smoke_module.signal.SIGTERM
        signaled.append(member.pid)
        for state in ("INTENDED", "PIDFD_OPENED", "IDENTITY_REVALIDATED", "SENT"):
            gpu_live_smoke_module._signal_trace_event(trace, member, sig, state)
        return True

    monkeypatch.setattr(gpu_live_smoke_module, "_signal_exact_member", fake_signal)

    class ExitedRootProcess:
        def poll(self) -> int:
            return 0

        def wait(self, timeout: int) -> int:
            assert timeout == 10
            return 0

    cleanup = gpu_live_smoke_module._stop_owned_service(
        guard,
        cast(Any, ExitedRootProcess()),
    )
    assert signaled == [child.pid]
    assert root.pid not in cast(list[int], cleanup["term_pids"])
    assert validations[0] == (False, True)
    assert cleanup["port_released"] is True


def test_root_dead_provisional_tree_freezes_receipt_before_reparented_child_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity(
        ppid=os.getpid(),
        pgid=41000,
        sid=41000,
    )
    recorded_child = _process_identity(
        pid=root.pid + 1,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    reparented_child = replace(recorded_child, ppid=1)

    class ExitedRootProcess:
        pid = root.pid

        def wait(self, timeout: int) -> int:
            assert timeout == 10
            return 0

    minimal = gpu_live_smoke_module._MinimalDirectChildIdentity(
        uid=root.uid,
        pid=root.pid,
        ppid=root.ppid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks,
    )
    handle = gpu_live_smoke_module._FailedLaunchCleanupHandle(
        process=cast(Any, ExitedRootProcess()),
        acquisition_pidfd=91,
        root_minimal=minimal,
        recorded_tree=(root, recorded_child),
        model_id="qwen3vl_8b",
        expected_argv=root.argv,
        gpu_uuid=GPU0_UUID,
        host="127.0.0.1",
        port=18007,
        environment_sha256="a" * 64,
        evidence_frozen=False,
    )
    authority = SimpleNamespace(owner_uid=os.getuid())
    monkeypatch.setattr(gpu_live_smoke_module, "_pidfd_process_is_live", lambda _fd: False)
    tree_checks = 0

    def current_tree(
        _recorded: tuple[Any, ...],
        *,
        allow_reparented_descendants: bool = False,
    ) -> tuple[Any, ...]:
        nonlocal tree_checks
        assert allow_reparented_descendants is True
        tree_checks += 1
        return (reparented_child,) if tree_checks <= 2 else ()

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_current_recorded_process_tree",
        current_tree,
    )
    frozen, receipt = gpu_live_smoke_module._freeze_failed_launch_acquisition(
        cast(Any, authority),
        handle,
    )
    assert receipt["live_recorded_member_count"] == 1
    assert receipt["root_live_at_freeze"] is False
    assert receipt["root_exited_before_freeze"] is True
    assert frozen.evidence_frozen is True
    order = ["EVENT:PROVISIONAL_ACQUISITION_FROZEN"]

    def exact_child_signal(
        member: Any,
        sig: Any,
        trace: list[dict[str, JsonValue]],
    ) -> bool:
        assert order[-1] == "EVENT:PROVISIONAL_ACQUISITION_FROZEN"
        assert member == reparented_child
        assert sig == gpu_live_smoke_module.signal.SIGTERM
        order.append(f"SIGNAL:{member.pid}")
        for state in ("INTENDED", "PIDFD_OPENED", "IDENTITY_REVALIDATED", "SENT"):
            gpu_live_smoke_module._signal_trace_event(trace, member, sig, state)
        return True

    monkeypatch.setattr(gpu_live_smoke_module, "_signal_exact_member", exact_child_signal)
    monkeypatch.setattr(gpu_live_smoke_module, "_assert_port_free", lambda: None)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_inspect_gpu_processes",
        lambda _authority, *, owned_pids: {
            "processes": [],
            "owned_pids": sorted(owned_pids),
        },
    )
    monkeypatch.setattr(
        gpu_live_smoke_module.os,
        "kill",
        lambda *_args: pytest.fail("numeric PID signal is forbidden"),
    )
    cleanup = gpu_live_smoke_module._cleanup_failed_launch(
        cast(Any, authority),
        frozen,
    )
    assert order == [
        "EVENT:PROVISIONAL_ACQUISITION_FROZEN",
        f"SIGNAL:{recorded_child.pid}",
    ]
    assert cleanup["term_pids"] == [recorded_child.pid]
    assert root.pid not in cast(list[int], cleanup["term_pids"])
    assert cleanup["direct_child_already_exited"] is True
    assert cleanup["process_session_released"] is True


def test_root_dead_provisional_empty_tree_never_signals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity(ppid=os.getpid())
    minimal = gpu_live_smoke_module._MinimalDirectChildIdentity(
        uid=root.uid,
        pid=root.pid,
        ppid=root.ppid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks,
    )
    handle = gpu_live_smoke_module._FailedLaunchCleanupHandle(
        process=cast(Any, SimpleNamespace(pid=root.pid)),
        acquisition_pidfd=91,
        root_minimal=minimal,
        recorded_tree=(),
        model_id="qwen3vl_8b",
        expected_argv=root.argv,
        gpu_uuid=GPU0_UUID,
        host="127.0.0.1",
        port=18007,
        environment_sha256="a" * 64,
        evidence_frozen=False,
    )
    monkeypatch.setattr(gpu_live_smoke_module, "_pidfd_process_is_live", lambda _fd: False)
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_signal_exact_member",
        lambda *_args: pytest.fail("no signal is authorized without a recorded tree"),
    )
    _assert_error_code(
        "GPU_SMOKE_PROCESS_IDENTITY_UNAVAILABLE",
        lambda: gpu_live_smoke_module._freeze_failed_launch_acquisition(
            cast(Any, SimpleNamespace(owner_uid=os.getuid())),
            handle,
        ),
    )


def test_partial_pidfd_signal_trace_distinguishes_intent_from_sent_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _process_identity()
    child = _process_identity(
        pid=41001,
        ppid=root.pid,
        pgid=root.pgid,
        sid=root.sid,
        starttime_ticks=root.starttime_ticks + 1,
    )
    guard = replace(_ownership_guard(root), service_tree=(root, child))
    validation_call_count = 0

    def fake_validate(
        _guard: Any,
        *,
        require_root: bool,
        allow_reparented_descendants: bool = False,
    ) -> tuple[Any, ...]:
        nonlocal validation_call_count
        del require_root, allow_reparented_descendants
        validation_call_count += 1
        return (root, child) if validation_call_count == 1 else ()

    monkeypatch.setattr(gpu_live_smoke_module, "_validate_owned_members", fake_validate)
    monkeypatch.setattr(gpu_live_smoke_module, "_listening_socket_inodes", lambda: set())
    monkeypatch.setattr(gpu_live_smoke_module, "_assert_port_free", lambda: None)

    def fake_signal(
        member: Any,
        sig: Any,
        trace: list[dict[str, JsonValue]],
    ) -> bool:
        gpu_live_smoke_module._signal_trace_event(trace, member, sig, "INTENDED")
        gpu_live_smoke_module._signal_trace_event(trace, member, sig, "PIDFD_OPENED")
        if member.pid == child.pid:
            gpu_live_smoke_module._signal_trace_event(
                trace,
                member,
                sig,
                "EXITED_BEFORE_REVALIDATION",
            )
            return False
        gpu_live_smoke_module._signal_trace_event(
            trace,
            member,
            sig,
            "IDENTITY_REVALIDATED",
        )
        gpu_live_smoke_module._signal_trace_event(trace, member, sig, "SENT")
        return True

    monkeypatch.setattr(gpu_live_smoke_module, "_signal_exact_member", fake_signal)

    class FakeProcess:
        def poll(self) -> None:
            return None

        def wait(self, timeout: int) -> int:
            assert timeout == 10
            return 0

    cleanup = gpu_live_smoke_module._stop_owned_service(guard, cast(Any, FakeProcess()))
    assert cleanup["term_pids"] == [root.pid]
    assert cleanup["kill_pids"] == []
    trace = cast(list[dict[str, JsonValue]], cleanup["signal_trace"])
    summary = gpu_live_smoke_module._signal_trace_summary(trace)
    assert summary["signal_target_pids"] == [root.pid, child.pid]
    assert summary["signal_sent_pids"] == [root.pid]
    assert summary["signal_intent_count"] == 2
    assert summary["signal_sent_count"] == 1
    assert summary["foreign_process_target_count"] == 0
    assert summary["broad_process_signal_count"] == 0
    assert summary["pidfd_only"] is True


@pytest.mark.parametrize(
    ("protocol", "local_address", "expected_scope", "expected_non_loopback"),
    (
        ("udp4", "00000000", "UNSPECIFIED", 1),
        ("udp6", "00000000000000000000000000000000", "UNSPECIFIED", 1),
        ("udp4", "0100007F", "LOOPBACK", 0),
        ("udp6", "00000000000000000000000001000000", "LOOPBACK", 0),
    ),
)
def test_owned_udp_socket_census_rejects_wildcard_and_accepts_loopback_only(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    local_address: str,
    expected_scope: str,
    expected_non_loopback: int,
) -> None:
    root = _process_identity()
    guard = replace(_ownership_guard(root), service_tree=(root,))
    inode = "73125"
    table_path = f"/proc/net/{protocol.replace('4', '').replace('6', '6')}"
    remote_address = "00000000" if protocol.endswith("4") else "00000000000000000000000000000000"
    row = (
        f"0: {local_address}:4657 {remote_address}:0000 07 "
        f"00000000:00000000 00:00000000 00000000 1000 0 {inode}"
    )
    tables = {
        "/proc/net/tcp": "header\n",
        "/proc/net/tcp6": "header\n",
        "/proc/net/udp": "header\n",
        "/proc/net/udp6": "header\n",
    }
    tables[table_path] += f"{row}\n"

    class FakeTablePath:
        def __init__(self, value: str) -> None:
            self.value = value

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "ascii"
            return tables[self.value]

    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_validate_owned_members",
        lambda _guard, **_kwargs: (root,),
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "_socket_inodes_for_members",
        lambda members: {inode},
    )
    monkeypatch.setattr(
        gpu_live_smoke_module,
        "Path",
        lambda value: FakeTablePath(cast(str, value)),
    )

    census = gpu_live_smoke_module._inspect_owned_inet_sockets(guard)
    assert census["inet_socket_count"] == 1
    assert census["non_loopback_inet_socket_count"] == expected_non_loopback
    row_receipt = cast(list[dict[str, JsonValue]], census["inet_sockets"])[0]
    assert row_receipt["protocol"] == protocol
    assert row_receipt["local_scope"] == expected_scope
    assert row_receipt["remote_scope"] == "UNSPECIFIED"
    assert row_receipt["loopback_only"] is (expected_non_loopback == 0)
    if expected_non_loopback:
        _assert_error_code(
            "GPU_SMOKE_NON_LOOPBACK_REQUEST_FORBIDDEN",
            lambda: gpu_live_smoke_module._append_socket_observation(
                [],
                census,
                model_id="qwen3vl_8b",
                phase="BEFORE_CALL",
                call_id="qwen-canary-seed-11-repeat-1",
            ),
        )
    else:
        observations: list[dict[str, JsonValue]] = []
        gpu_live_smoke_module._append_socket_observation(
            observations,
            census,
            model_id="qwen3vl_8b",
            phase="BEFORE_CALL",
            call_id="qwen-canary-seed-11-repeat-1",
        )
        assert len(observations) == 1


def test_occupied_port_fails_without_signaling_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[object] = []

    class OccupiedSocket:
        def setsockopt(self, *args: object) -> None:
            del args

        def bind(self, address: object) -> None:
            assert address == ("127.0.0.1", 18007)
            raise OSError("occupied")

        def close(self) -> None:
            return None

    monkeypatch.setattr(socket, "socket", lambda *_args: OccupiedSocket())
    monkeypatch.setattr(os, "kill", lambda *args: signals.append(args))
    _assert_error_code(
        "GPU_SMOKE_PORT_OCCUPIED",
        gpu_live_smoke_module._assert_port_free,
    )
    assert signals == []


def test_existing_generic_live_entrypoints_remain_blocked() -> None:
    codec = OpenAICompatibleProviderCodec(
        codec_id="mobileworld.g1.provider.d034-test/v1",
        endpoint_revision="http://127.0.0.1:18007/v1/chat/completions",
        parser=JsonActionParser(),
    )
    with pytest.raises(ReplayRunnerError) as send_error:
        codec.send(cast(Any, object()))
    assert send_error.value.code == "LIVE_TRANSPORT_DEFERRED"
    with pytest.raises(ReplayRunnerError) as runner_error:
        execute_live_arm(cast(Any, object()))
    assert runner_error.value.code == "LIVE_EXECUTION_DEFERRED"
